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
    .menu-wrap { position: relative; display: inline-block; }
    .menu-pop {
      display: none; position: absolute; top: 110%; left: 0; z-index: 1000;
      background: #161b22; border: 1px solid #30363d; border-radius: 6px;
      min-width: 320px; max-width: 480px; max-height: 60vh; overflow: auto;
      box-shadow: 0 8px 24px rgba(0,0,0,0.4); padding: 6px;
    }
    .menu-pop.open { display: block; }
    .menu-pop .empty { padding: 12px; opacity: 0.6; font-size: 12px; text-align: center; }
    .menu-pop .row {
      display: flex; align-items: center; gap: 8px;
      padding: 6px 8px; border-radius: 4px; font-size: 12px; color: #e6edf3;
    }
    .menu-pop .row:hover { background: #21262d; }
    .menu-pop .name { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .menu-pop .meta { opacity: 0.5; font-size: 11px; min-width: 60px; text-align: right; }
    .menu-pop .run {
      background: #238636; border: none; border-radius: 4px;
      color: #fff; padding: 3px 10px; cursor: pointer; font-size: 11px;
    }
    .menu-pop .run:hover { filter: brightness(1.15); }
    .menu-pop .run:disabled { opacity: 0.5; cursor: wait; }
  </style>
</head>
<body>
  <div id="audio-bar">
    <span>&#128266; VM Audio</span>
    <button id="audio-toggle" class="bar-btn" onclick="toggleAudio()">Enable Sound</button>
    <input type="range" id="volume-slider" min="0" max="1" step="0.05" value="0.8"
           oninput="setVolume(this.value)" title="Volume" disabled>
    <button id="test-tone-btn" class="bar-btn secondary" onclick="playTestTone()"
            title="Play a 2-second beep inside the VM so you can verify sound is reaching your speakers">Test Tone</button>
    <span style="opacity:0.3;">|</span>
    <span>&#128203; Clipboard</span>
    <button id="paste-btn" class="bar-btn secondary" onclick="pasteToVM()"
            title="Paste your computer's clipboard into the focused VM window">Paste from Clipboard</button>
    <span style="opacity:0.3;">|</span>
    <span>&#128193; Files</span>
    <div class="menu-wrap">
      <button id="files-btn" class="bar-btn secondary" onclick="toggleFilesMenu()"
              title="List files in ~/Downloads inside the VM and open them">Open Downloaded &#9662;</button>
      <div id="files-menu" class="menu-pop"></div>
    </div>
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
    var bytesIn = 0, rateTimer = null;

    function startAudio() {
      audioCtx  = new (window.AudioContext || window.webkitAudioContext)({ sampleRate: RATE });
      // Chrome's autoplay policy keeps the context suspended until a user gesture;
      // calling resume() here (we're inside the click handler) guarantees playback.
      if (audioCtx.state === 'suspended') audioCtx.resume();
      gainNode  = audioCtx.createGain();
      gainNode.gain.value = parseFloat(vol.value);
      gainNode.connect(audioCtx.destination);
      nextTime  = audioCtx.currentTime + 0.1;

      var proto = location.protocol === 'https:' ? 'wss' : 'ws';
      ws = new WebSocket(proto + '://' + location.host + '/audio-ws');
      ws.binaryType = 'arraybuffer';

      ws.onopen = function() {
        stat.textContent = 'Connected — waiting for audio...';
        bytesIn = 0;
        if (rateTimer) clearInterval(rateTimer);
        rateTimer = setInterval(function() {
          var kbps = (bytesIn / 1024).toFixed(0);
          stat.textContent = bytesIn > 0
            ? 'Streaming ' + kbps + ' KB/s'
            : 'Connected — VM is silent';
          bytesIn = 0;
        }, 1000);
      };

      ws.onmessage = function(e) {
        bytesIn += e.data.byteLength;
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
      if (rateTimer) { clearInterval(rateTimer); rateTimer = null; }
      if (ws) { ws.onclose = null; ws.close(); ws = null; }
      if (audioCtx) { audioCtx.close(); audioCtx = null; }
      gainNode = null; nextTime = 0;
    }

    function playTestTone() {
      var b = document.getElementById('test-tone-btn');
      b.disabled = true;
      var orig = b.textContent;
      b.textContent = 'Playing...';
      fetch('/test-tone', { method: 'POST' })
        .then(function(r) { b.textContent = r.ok ? 'Sent!' : 'Failed'; })
        .catch(function() { b.textContent = 'Error'; })
        .finally(function() {
          setTimeout(function() { b.textContent = orig; b.disabled = false; }, 2500);
        });
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

    // ---- Downloaded-files menu --------------------------------------------
    var filesBtn  = document.getElementById('files-btn');
    var filesMenu = document.getElementById('files-menu');

    function fmtSize(n) {
      if (n < 1024) return n + ' B';
      if (n < 1048576) return (n / 1024).toFixed(0) + ' KB';
      if (n < 1073741824) return (n / 1048576).toFixed(1) + ' MB';
      return (n / 1073741824).toFixed(2) + ' GB';
    }

    function escapeHtml(s) {
      return s.replace(/[&<>"']/g, function(c) {
        return { '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;' }[c];
      });
    }

    function renderFiles(list) {
      if (!list || list.length === 0) {
        filesMenu.innerHTML = '<div class="empty">No files in ~/Downloads yet.<br>Download something in the VM browser first.</div>';
        return;
      }
      var html = '';
      list.forEach(function(f) {
        var safe = escapeHtml(f.name);
        html += '<div class="row">'
              +   '<span class="name" title="' + safe + '">' + safe + '</span>'
              +   '<span class="meta">' + fmtSize(f.size) + '</span>'
              +   '<button class="run" data-name="' + safe + '">Open</button>'
              + '</div>';
      });
      filesMenu.innerHTML = html;
      filesMenu.querySelectorAll('button.run').forEach(function(b) {
        b.addEventListener('click', function() { runFile(b, b.dataset.name); });
      });
    }

    function refreshFiles() {
      filesMenu.innerHTML = '<div class="empty">Loading...</div>';
      fetch('/downloads')
        .then(function(r) { return r.json(); })
        .then(renderFiles)
        .catch(function() {
          filesMenu.innerHTML = '<div class="empty" style="color:#f85149">Failed to load file list</div>';
        });
    }

    function toggleFilesMenu() {
      var opening = !filesMenu.classList.contains('open');
      filesMenu.classList.toggle('open');
      if (opening) refreshFiles();
    }

    function showRunError(name, msg) {
      // Render a small error block inline in the menu so the user can read why it failed
      var box = document.createElement('div');
      box.style.cssText = 'margin: 6px 4px; padding: 8px 10px; border-radius: 4px;'
                       + 'background: #2d1117; border: 1px solid #f85149; color: #ffa198;'
                       + 'font-size: 11px; font-family: monospace; white-space: pre-wrap;'
                       + 'word-break: break-word; max-height: 180px; overflow: auto;';
      box.textContent = msg;
      filesMenu.appendChild(box);
      filesMenu.scrollTop = filesMenu.scrollHeight;
    }

    function runFile(btn, name) {
      btn.disabled = true;
      var orig = btn.textContent;
      btn.textContent = 'Opening...';
      // Clear any previous error block
      var prev = filesMenu.querySelector('.run-error');
      if (prev) prev.remove();
      fetch('/run', {
        method: 'POST',
        headers: { 'Content-Type': 'text/plain' },
        body: name,
      })
        .then(function(r) { return r.text().then(function(t) { return { ok: r.ok, text: t }; }); })
        .then(function(res) {
          if (res.ok) {
            btn.textContent = 'Launched';
            btn.style.background = '#238636';
            // Auto-close the menu so the launched app gets focus
            setTimeout(function() { filesMenu.classList.remove('open'); }, 800);
          } else {
            btn.textContent = 'Failed';
            btn.style.background = '#f85149';
            showRunError(name, res.text);
          }
        })
        .catch(function(e) {
          btn.textContent = 'Error'; btn.style.background = '#f85149';
          showRunError(name, 'Network error: ' + e.message);
        })
        .finally(function() {
          setTimeout(function() {
            btn.textContent = orig; btn.disabled = false; btn.style.background = '';
          }, 3500);
        });
    }

    // Close the menu when clicking outside it
    document.addEventListener('click', function(e) {
      if (!filesMenu.classList.contains('open')) return;
      if (e.target === filesBtn || filesMenu.contains(e.target)) return;
      filesMenu.classList.remove('open');
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


DOWNLOADS_DIR = os.path.realpath(os.path.expanduser('~/Downloads'))


def _read_post_body(client, headers_buf, max_bytes=4096):
    """Helper: parse Content-Length and read the full POST body, returning bytes."""
    content_length = 0
    for line in headers_buf.split(b'\r\n'):
        if line.lower().startswith(b'content-length:'):
            try:
                content_length = int(line.split(b':', 1)[1].strip())
            except Exception:
                content_length = 0
            break
    header_end = headers_buf.find(b'\r\n\r\n') + 4
    body = headers_buf[header_end:]
    while len(body) < content_length and len(body) < max_bytes:
        chunk = client.recv(min(4096, content_length - len(body)))
        if not chunk:
            break
        body += chunk
    return body[:max_bytes]


def _send_simple(client, status, body, ctype='text/plain'):
    if isinstance(body, str):
        body = body.encode('utf-8')
    head = (f'HTTP/1.1 {status}\r\nContent-Type: {ctype}\r\n'
            f'Content-Length: {len(body)}\r\nCache-Control: no-cache\r\n'
            f'Access-Control-Allow-Origin: *\r\nConnection: close\r\n\r\n').encode()
    try:
        client.sendall(head + body)
    except Exception:
        pass


def _handle_list_downloads(client):
    """GET /downloads: return JSON list of files in ~/Downloads (newest first)."""
    try:
        os.makedirs(DOWNLOADS_DIR, exist_ok=True)
        entries = []
        for name in os.listdir(DOWNLOADS_DIR):
            full = os.path.join(DOWNLOADS_DIR, name)
            try:
                st = os.stat(full)
            except OSError:
                continue
            # Skip directories and Chromium's in-progress download files
            if not os.path.isfile(full):
                continue
            if name.endswith('.crdownload') or name.endswith('.part'):
                continue
            entries.append({'name': name, 'size': st.st_size, 'mtime': st.st_mtime})
        entries.sort(key=lambda e: e['mtime'], reverse=True)
        # Strip mtime from response (only used for sorting)
        for e in entries:
            e.pop('mtime', None)
        import json
        _send_simple(client, '200 OK', json.dumps(entries), 'application/json')
    except Exception as e:
        _send_simple(client, '500 Internal Server Error', f'list failed: {e}')
    finally:
        try:
            client.close()
        except Exception:
            pass


def _launch_in_vm(argv, settle=2.0):
    """Spawn `argv` on DISPLAY=:1 with PulseAudio wired up. Wait up to `settle`
    seconds and return (proc, stderr_text). If the process is still running
    after `settle`, stderr_text is empty and the caller treats it as a success.
    If the process dies fast (typical for a missing-library error), we collect
    stderr and return it so the user sees the real reason in the UI."""
    env = dict(os.environ,
               DISPLAY=':1',
               PULSE_SERVER='/var/run/pulse/native',
               HOME=os.path.expanduser('~'))
    # Replit ships needed system libs across two env vars; downloaded binaries
    # don't inherit them, so merge both into LD_LIBRARY_PATH.
    parts = [p for p in (env.get('REPLIT_LD_LIBRARY_PATH', ''),
                         env.get('REPLIT_PYTHON_LD_LIBRARY_PATH', ''),
                         env.get('LD_LIBRARY_PATH', '')) if p]
    if parts:
        env['LD_LIBRARY_PATH'] = ':'.join(parts)
    proc = subprocess.Popen(
        argv,
        env=env,
        cwd=DOWNLOADS_DIR,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        # Brief wait so we can report fast-fail errors back to the caller.
        # Long-running apps will time out here, which we treat as success.
        _, err = proc.communicate(timeout=settle)
        return proc, (err or b'').decode('utf-8', 'replace').strip()
    except subprocess.TimeoutExpired:
        # Still running — drain stderr asynchronously into /dev/null so the
        # pipe doesn't fill up and block the app later.
        threading.Thread(
            target=lambda: proc.stderr and proc.stderr.read(),
            daemon=True,
        ).start()
        return proc, ''


def _handle_run_file(client, headers_buf):
    """POST /run: body is a filename inside ~/Downloads. chmod +x and launch it
    in the VM under DISPLAY=:1, choosing a sensible launcher for the file type."""
    try:
        name = _read_post_body(client, headers_buf, max_bytes=512).decode('utf-8', 'replace').strip()
        if not name:
            _send_simple(client, '400 Bad Request', 'empty filename'); return

        # Resolve and confine to ~/Downloads (block path traversal)
        target = os.path.realpath(os.path.join(DOWNLOADS_DIR, name))
        if not target.startswith(DOWNLOADS_DIR + os.sep) or not os.path.isfile(target):
            _send_simple(client, '404 Not Found', f'no such file: {name}'); return

        # Make sure the file is executable (downloads come without +x)
        try:
            st = os.stat(target)
            os.chmod(target, st.st_mode | 0o755)
        except Exception:
            pass

        lower = name.lower()
        # Sniff the first few bytes so we handle text scripts and ELFs correctly
        try:
            with open(target, 'rb') as f:
                head = f.read(8)
        except Exception:
            head = b''

        if lower.endswith('.appimage'):
            # --appimage-extract-and-run avoids needing FUSE, which is missing in containers
            argv = [target, '--appimage-extract-and-run']
        elif lower.endswith(('.sh', '.bash')) or head.startswith(b'#!'):
            # Shell script (or anything with a shebang) — let the kernel pick the interpreter
            argv = [target]
        elif head.startswith(b'\x7fELF'):
            argv = [target]
        elif lower.endswith('.deb'):
            _send_simple(client, '400 Bad Request',
                         '.deb packages need apt/dpkg with sudo, which is not available in this VM. '
                         'Look for an AppImage or static binary instead.'); return
        elif lower.endswith(('.zip', '.tar', '.tar.gz', '.tgz', '.7z', '.rar')):
            _send_simple(client, '400 Bad Request',
                         'This is an archive, not an executable. Open a terminal in the VM '
                         '(right-click the desktop) and extract it first.'); return
        else:
            # Best-effort: assume it's a binary the user wants to run
            argv = [target]

        proc, err = _launch_in_vm(argv)
        if proc.poll() is None:
            _send_simple(client, '200 OK', f'launched: {os.path.basename(target)}')
        else:
            # Process died fast — return the captured stderr so the user can see
            # the real failure reason (missing library, segfault, etc.).
            tail = err[-1500:] if err else f'process exited with code {proc.returncode} and produced no error output'
            _send_simple(client, '500 Internal Server Error',
                         f'{os.path.basename(target)} exited immediately:\n\n{tail}')
    except Exception as e:
        _send_simple(client, '500 Internal Server Error', f'run failed: {e}')
    finally:
        try:
            client.close()
        except Exception:
            pass


def _handle_test_tone(client):
    """POST /test-tone: pipe a 2-second 440Hz sine into the default PulseAudio sink."""
    try:
        env = dict(os.environ, PULSE_SERVER='/var/run/pulse/native')
        # Run detached so the request returns immediately; ffmpeg writes into the
        # null sink, which is exactly the same path Chromium audio takes.
        subprocess.Popen(
            ['ffmpeg', '-hide_banner', '-loglevel', 'error',
             '-f', 'lavfi', '-i', 'sine=frequency=440:duration=2',
             '-f', 'pulse', 'default'],
            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
        )
        resp = (b'HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\n'
                b'Content-Length: 2\r\nConnection: close\r\n\r\nOK')
        client.sendall(resp)
    except Exception as e:
        try:
            msg = f'test-tone failed: {e}'.encode()
            resp = (b'HTTP/1.1 500 Internal Server Error\r\nContent-Type: text/plain\r\n'
                    b'Content-Length: ' + str(len(msg)).encode() +
                    b'\r\nConnection: close\r\n\r\n') + msg
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
    elif method == 'POST' and path == '/test-tone':
        threading.Thread(target=_handle_test_tone, args=(client,), daemon=True).start()
    elif method == 'GET' and path == '/downloads':
        threading.Thread(target=_handle_list_downloads, args=(client,), daemon=True).start()
    elif method == 'POST' and path == '/run':
        threading.Thread(target=_handle_run_file, args=(client, buf), daemon=True).start()
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
