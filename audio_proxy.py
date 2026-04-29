#!/usr/bin/env python3
"""
Proxy on port 5000:
  GET  /             -> wrapper HTML page with audio controls (Web Audio API)
  GET  /audio-ws     -> WebSocket: raw PCM s16le stereo 44100Hz frames
  GET  /audio.ogg    -> fallback HTTP Ogg/Vorbis stream
  POST /paste        -> body text -> X11 CLIPBOARD + Ctrl+V into focused window
  *                  -> forwarded to noVNC/websockify on port 4998
"""
import os
import socket
import threading
import subprocess
import select
import hashlib
import base64
import struct
import textwrap

LISTEN_PORT = 5000
NOVNC_PORT  = 4998
WS_MAGIC    = b'258EAFA5-E914-47DA-95CA-C5AB0DC85B11'

WRAPPER_HTML = textwrap.dedent("""\
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>BlobeVM Desktop</title>
  <style>
    * { margin: 0; padding: 0; box-sizing: border-box; }
    body { background: #0d1117; display: flex; flex-direction: column; height: 100vh; overflow: hidden; font-family: sans-serif; }
    #audio-bar {
      display: flex; align-items: center; gap: 12px;
      background: #161b22; border-bottom: 1px solid #30363d;
      padding: 6px 16px; height: 40px; flex-shrink: 0;
      color: #e6edf3; font-size: 13px; user-select: none;
    }
    #audio-bar span { opacity: 0.7; }
    .bar-btn {
      background: #238636; border: none; border-radius: 6px;
      color: #fff; padding: 5px 14px; cursor: pointer; font-size: 13px;
      transition: background 0.15s;
    }
    #audio-toggle.active { background: #8250df; }
    .bar-btn:hover { filter: brightness(1.15); }
    .bar-btn.secondary { background: #30363d; }
    #volume-slider { accent-color: #238636; cursor: pointer; width: 90px; }
    #status { font-size: 12px; opacity: 0.5; margin-left: auto; }
    #paste-status { font-size: 11px; opacity: 0.6; min-width: 80px; }
    #vnc-frame { flex: 1; border: none; width: 100%; }
  </style>
</head>
<body>
  <div id="audio-bar">
    <span>&#128266; VM Audio</span>
    <button id="audio-toggle" class="bar-btn" onclick="toggleAudio()">Enable Sound</button>
    <input type="range" id="volume-slider" min="0" max="1" step="0.05" value="0.8"
           oninput="setVolume(this.value)" title="Volume" disabled>
    <span style="opacity:0.3;">|</span>
    <span>&#128203; Clipboard</span>
    <button id="paste-btn" class="bar-btn secondary" onclick="pasteToVM()"
            title="Paste your computer's clipboard into the focused VM window">Paste from Clipboard</button>
    <span id="paste-status"></span>
    <span id="status">Sound off</span>
  </div>
  <iframe id="vnc-frame"
    src="/vnc.html?autoconnect=true&password=password&resize=scale"
    allow="fullscreen">
  </iframe>
  <script>
    // Raw PCM Web Audio API player
    // Server sends s16le stereo 44100Hz binary frames over WebSocket
    var RATE = 44100, CH = 2, BPS = 2;
    var audioCtx = null, gainNode = null, ws = null;
    var nextTime = 0, on = false;

    var btn  = document.getElementById('audio-toggle');
    var vol  = document.getElementById('volume-slider');
    var stat = document.getElementById('status');

    function startAudio() {
      audioCtx  = new (window.AudioContext || window.webkitAudioContext)({ sampleRate: RATE });
      gainNode  = audioCtx.createGain();
      gainNode.gain.value = parseFloat(vol.value);
      gainNode.connect(audioCtx.destination);
      nextTime  = audioCtx.currentTime + 0.1;

      var proto = location.protocol === 'https:' ? 'wss' : 'ws';
      ws = new WebSocket(proto + '://' + location.host + '/audio-ws');
      ws.binaryType = 'arraybuffer';

      ws.onopen = function() {
        stat.textContent = 'Connected — listening...';
      };

      ws.onmessage = function(e) {
        var raw    = new Int16Array(e.data);
        var frames = Math.floor(raw.length / CH);
        if (frames === 0) return;

        var buf  = audioCtx.createBuffer(CH, frames, RATE);
        var ch0  = buf.getChannelData(0);
        var ch1  = buf.getChannelData(1);
        for (var i = 0; i < frames; i++) {
          ch0[i] = raw[i * 2]     / 32768.0;
          ch1[i] = raw[i * 2 + 1] / 32768.0;
        }

        var src = audioCtx.createBufferSource();
        src.buffer = buf;
        src.connect(gainNode);

        var now = audioCtx.currentTime;
        // 30ms lookahead — enough to avoid glitches, small enough to feel live
        if (nextTime < now + 0.015) nextTime = now + 0.03;
        src.start(nextTime);
        nextTime += frames / RATE;
      };

      ws.onerror = function() {
        stat.textContent = 'Connection error — retrying...';
      };

      ws.onclose = function() {
        if (on) {
          stat.textContent = 'Reconnecting...';
          setTimeout(startAudio, 2000);
        }
      };
    }

    function stopAudio() {
      if (ws) { ws.onclose = null; ws.close(); ws = null; }
      if (audioCtx) { audioCtx.close(); audioCtx = null; }
      gainNode = null; nextTime = 0;
    }

    function toggleAudio() {
      if (!on) {
        on = true;
        btn.textContent  = 'Mute';
        btn.classList.add('active');
        vol.disabled     = false;
        stat.textContent = 'Connecting...';
        startAudio();
      } else {
        on = false;
        btn.textContent  = 'Enable Sound';
        btn.classList.remove('active');
        vol.disabled     = true;
        stat.textContent = 'Sound off';
        stopAudio();
      }
    }

    function setVolume(v) {
      if (gainNode) gainNode.gain.value = parseFloat(v);
    }

    var pasteStat = document.getElementById('paste-status');
    var pasteBtn  = document.getElementById('paste-btn');

    function flashPaste(msg, color) {
      pasteStat.textContent = msg;
      pasteStat.style.color = color || '';
      setTimeout(function() {
        if (pasteStat.textContent === msg) pasteStat.textContent = '';
      }, 2500);
    }

    function sendPaste(text) {
      if (!text) { flashPaste('Clipboard empty', '#f85149'); return; }
      pasteBtn.disabled = true;
      pasteStat.textContent = 'Sending ' + text.length + ' chars...';
      fetch('/paste', { method: 'POST', body: text, headers: { 'Content-Type': 'text/plain' } })
        .then(function(r) {
          if (r.ok) flashPaste('Pasted ' + text.length + ' chars', '#3fb950');
          else      flashPaste('Paste failed (' + r.status + ')', '#f85149');
        })
        .catch(function(e) { flashPaste('Paste error', '#f85149'); })
        .finally(function() { pasteBtn.disabled = false; });
    }

    function pasteToVM() {
      // Try the modern Clipboard API first (needs HTTPS + user gesture, both true here)
      if (navigator.clipboard && navigator.clipboard.readText) {
        navigator.clipboard.readText()
          .then(sendPaste)
          .catch(function() {
            // Permission denied or unsupported — fall back to a prompt
            promptPaste();
          });
      } else {
        promptPaste();
      }
    }

    function promptPaste() {
      var text = prompt('Your browser blocked clipboard access.\\nPaste your text here and click OK:');
      if (text != null) sendPaste(text);
    }

    // Also intercept Ctrl+Shift+V on the wrapper page itself for power users
    document.addEventListener('keydown', function(e) {
      if (e.ctrlKey && e.shiftKey && e.key.toLowerCase() === 'v') {
        e.preventDefault();
        pasteToVM();
      }
    });
  </script>
</body>
</html>
""").encode('utf-8')


def _relay(src, dst):
    try:
        while True:
            r, _, _ = select.select([src, dst], [], [], 2.0)
            if src in r:
                d = src.recv(32768)
                if not d:
                    break
                dst.sendall(d)
            if dst in r:
                d = dst.recv(32768)
                if not d:
                    break
                src.sendall(d)
    except Exception:
        pass
    finally:
        for s in (src, dst):
            try:
                s.close()
            except Exception:
                pass


def _ws_accept_key(key_bytes):
    digest = hashlib.sha1(key_bytes.strip() + WS_MAGIC).digest()
    return base64.b64encode(digest).decode()


def _ws_send_binary(sock, data):
    length = len(data)
    if length <= 125:
        header = bytes([0x82, length])
    elif length <= 65535:
        header = struct.pack('>BBH', 0x82, 126, length)
    else:
        header = struct.pack('>BBQ', 0x82, 127, length)
    sock.sendall(header + data)


def _stream_pcm_ws(sock, headers_buf):
    """WebSocket handler: stream raw s16le stereo 44100Hz PCM from PulseAudio."""
    key = b''
    for line in headers_buf.split(b'\r\n'):
        if line.lower().startswith(b'sec-websocket-key:'):
            key = line.split(b':', 1)[1].strip()
            break

    accept = _ws_accept_key(key)
    handshake = (
        b'HTTP/1.1 101 Switching Protocols\r\n'
        b'Upgrade: websocket\r\n'
        b'Connection: Upgrade\r\n'
        b'Sec-WebSocket-Accept: ' + accept.encode() + b'\r\n'
        b'\r\n'
    )
    try:
        sock.sendall(handshake)
    except Exception:
        sock.close()
        return

    proc = subprocess.Popen(
        ['ffmpeg',
         # Reduce ffmpeg's own input/output buffering
         '-fflags', 'nobuffer',
         '-flags', 'low_delay',
         # PulseAudio source — fragment_size limits how much PA buffers per read
         '-f', 'pulse', '-fragment_size', '2048', '-i', 'null.monitor',
         '-f', 's16le', '-ar', '44100', '-ac', '2',
         # Flush output every frame (no mux delay)
         '-flush_packets', '1',
         '-'],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    # ~23ms chunks (4096 bytes = 1024 stereo frames at 44100Hz)
    try:
        while True:
            chunk = proc.stdout.read(4096)
            if not chunk:
                break
            _ws_send_binary(sock, chunk)
    except Exception:
        pass
    finally:
        try:
            proc.kill()
        except Exception:
            pass
        try:
            sock.close()
        except Exception:
            pass


def _stream_audio_http(sock):
    """Fallback: plain HTTP Ogg/Vorbis stream."""
    headers = (
        b'HTTP/1.1 200 OK\r\n'
        b'Content-Type: audio/ogg\r\n'
        b'Cache-Control: no-cache\r\n'
        b'Access-Control-Allow-Origin: *\r\n'
        b'Transfer-Encoding: chunked\r\n'
        b'Connection: close\r\n'
        b'\r\n'
    )
    try:
        sock.sendall(headers)
    except Exception:
        sock.close()
        return

    proc = subprocess.Popen(
        ['ffmpeg', '-f', 'pulse', '-i', 'null.monitor',
         '-c:a', 'libvorbis', '-b:a', '96k', '-f', 'ogg', '-'],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    try:
        while True:
            chunk = proc.stdout.read(4096)
            if not chunk:
                break
            sock.sendall(('%x\r\n' % len(chunk)).encode() + chunk + b'\r\n')
    except Exception:
        pass
    finally:
        try:
            proc.kill()
        except Exception:
            pass
        try:
            sock.close()
        except Exception:
            pass


def _serve_html(sock, body):
    resp = (
        b'HTTP/1.1 200 OK\r\n'
        b'Content-Type: text/html; charset=utf-8\r\n'
        b'Cache-Control: no-cache\r\n'
        b'Connection: close\r\n'
        b'Content-Length: ' + str(len(body)).encode() + b'\r\n'
        b'\r\n'
    ) + body
    try:
        sock.sendall(resp)
    except Exception:
        pass
    finally:
        try:
            sock.close()
        except Exception:
            pass


def _handle_paste(client, headers_buf):
    """POST /paste: body is raw text. Set X11 CLIPBOARD then send Ctrl+V to focused window."""
    try:
        # Parse Content-Length to know how much body to read
        content_length = 0
        for line in headers_buf.split(b'\r\n'):
            if line.lower().startswith(b'content-length:'):
                try:
                    content_length = int(line.split(b':', 1)[1].strip())
                except Exception:
                    content_length = 0
                break

        # The body may already be in headers_buf after the \r\n\r\n
        header_end = headers_buf.find(b'\r\n\r\n') + 4
        body = headers_buf[header_end:]
        # Read the rest of the body if not all received yet
        while len(body) < content_length:
            chunk = client.recv(min(65536, content_length - len(body)))
            if not chunk:
                break
            body += chunk

        # Cap at 1MB to avoid abuse
        body = body[:1048576]

        env = dict(os.environ, DISPLAY=':1')
        # 1) Push text into X11 CLIPBOARD selection
        p1 = subprocess.Popen(
            ['xclip', '-selection', 'clipboard'],
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            env=env,
        )
        p1.communicate(body, timeout=3)
        # 2) Synthesize Ctrl+V to whichever window currently has keyboard focus
        subprocess.run(
            ['xdotool', 'key', '--clearmodifiers', 'ctrl+v'],
            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=3,
        )

        resp = (b'HTTP/1.1 200 OK\r\n'
                b'Content-Type: text/plain\r\n'
                b'Content-Length: 2\r\n'
                b'Access-Control-Allow-Origin: *\r\n'
                b'Connection: close\r\n\r\nOK')
        client.sendall(resp)
    except Exception as e:
        try:
            msg = f'paste failed: {e}'.encode()
            resp = (b'HTTP/1.1 500 Internal Server Error\r\n'
                    b'Content-Type: text/plain\r\n'
                    b'Content-Length: ' + str(len(msg)).encode() + b'\r\n'
                    b'Connection: close\r\n\r\n') + msg
            client.sendall(resp)
        except Exception:
            pass
    finally:
        try:
            client.close()
        except Exception:
            pass


def _handle(client):
    buf = b''
    try:
        while b'\r\n\r\n' not in buf:
            chunk = client.recv(4096)
            if not chunk:
                client.close()
                return
            buf += chunk
    except Exception:
        client.close()
        return

    first_line = buf.split(b'\r\n')[0].decode('utf-8', errors='replace')
    parts  = first_line.split(' ')
    path   = parts[1].split('?')[0] if len(parts) >= 2 else '/'
    method = parts[0] if parts else 'GET'
    is_ws  = (b'Upgrade: websocket' in buf or b'upgrade: websocket' in buf)

    if method == 'GET' and path in ('/', '/index.html'):
        threading.Thread(target=_serve_html, args=(client, WRAPPER_HTML), daemon=True).start()
    elif method == 'GET' and path == '/favicon.ico':
        # Serve a minimal 1x1 transparent ICO to stop browser 404 noise
        ICO = (b'\x00\x00\x01\x00\x01\x00\x01\x01\x00\x00\x01\x00\x18\x00'
               b'\x30\x00\x00\x00\x16\x00\x00\x00\x28\x00\x00\x00\x01\x00'
               b'\x00\x00\x02\x00\x00\x00\x01\x00\x18\x00\x00\x00\x00\x00'
               b'\x04\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00'
               b'\x00\x00\x00\x00\x00\x00\x1a\x3a\x5c\x00\x00\x00\x00\x00')
        resp = (b'HTTP/1.1 200 OK\r\nContent-Type: image/x-icon\r\n'
                b'Cache-Control: max-age=86400\r\nContent-Length: '
                + str(len(ICO)).encode() + b'\r\n\r\n' + ICO)
        try:
            client.sendall(resp)
        except Exception:
            pass
        finally:
            try:
                client.close()
            except Exception:
                pass
    elif path == '/audio-ws' and is_ws:
        threading.Thread(target=_stream_pcm_ws, args=(client, buf), daemon=True).start()
    elif path in ('/audio', '/audio.ogg'):
        threading.Thread(target=_stream_audio_http, args=(client,), daemon=True).start()
    elif method == 'POST' and path == '/paste':
        threading.Thread(target=_handle_paste, args=(client, buf), daemon=True).start()
    else:
        try:
            backend = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            backend.connect(('127.0.0.1', NOVNC_PORT))
            backend.sendall(buf)
            _relay(client, backend)
        except Exception:
            try:
                client.close()
            except Exception:
                pass


def main():
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(('0.0.0.0', LISTEN_PORT))
    srv.listen(128)
    print(f'Audio proxy on :{LISTEN_PORT} | noVNC on :{NOVNC_PORT}', flush=True)
    while True:
        client, _ = srv.accept()
        threading.Thread(target=_handle, args=(client,), daemon=True).start()


if __name__ == '__main__':
    main()
