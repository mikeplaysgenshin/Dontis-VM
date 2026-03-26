#!/usr/bin/env python3
"""
Proxy on port 5000:
  GET  /             -> wrapper HTML page with audio controls (Web Audio API)
  GET  /audio-ws     -> WebSocket: raw PCM s16le stereo 44100Hz frames
  GET  /audio.ogg    -> fallback HTTP Ogg/Vorbis stream
  *                  -> forwarded to noVNC/websockify on port 4998
"""
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
    #audio-toggle {
      background: #238636; border: none; border-radius: 6px;
      color: #fff; padding: 5px 14px; cursor: pointer; font-size: 13px;
      transition: background 0.15s;
    }
    #audio-toggle.active { background: #8250df; }
    #audio-toggle:hover { filter: brightness(1.15); }
    #volume-slider { accent-color: #238636; cursor: pointer; width: 90px; }
    #status { font-size: 12px; opacity: 0.5; margin-left: auto; }
    #vnc-frame { flex: 1; border: none; width: 100%; }
  </style>
</head>
<body>
  <div id="audio-bar">
    <span>&#128266; VM Audio</span>
    <button id="audio-toggle" onclick="toggleAudio()">Enable Sound</button>
    <input type="range" id="volume-slider" min="0" max="1" step="0.05" value="0.8"
           oninput="setVolume(this.value)" title="Volume" disabled>
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
        // Keep a 80ms lookahead buffer; if we're behind, catch up
        if (nextTime < now + 0.04) nextTime = now + 0.08;
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
         '-f', 'pulse', '-i', 'null.monitor',
         '-f', 's16le', '-ar', '44100', '-ac', '2',
         '-'],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    # Send ~46ms chunks (4096 bytes = 1024 stereo frames at 44100Hz)
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
    elif path == '/audio-ws' and is_ws:
        threading.Thread(target=_stream_pcm_ws, args=(client, buf), daemon=True).start()
    elif path in ('/audio', '/audio.ogg'):
        threading.Thread(target=_stream_audio_http, args=(client,), daemon=True).start()
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
