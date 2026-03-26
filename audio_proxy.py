#!/usr/bin/env python3
"""
TCP-level proxy on port 5000:
  GET /          -> serves a wrapper page with audio controls + noVNC iframe
  GET /audio.ogg -> streams PulseAudio via ffmpeg (Ogg/Opus)
  everything else -> proxied to novnc/websockify on internal port 4998
"""
import socket
import threading
import subprocess
import select
import textwrap

LISTEN_PORT = 5000
NOVNC_PORT  = 4998

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
      color: #e6edf3; font-size: 13px;
    }
    #audio-bar span { opacity: 0.7; }
    #audio-toggle {
      background: #238636; border: none; border-radius: 6px;
      color: #fff; padding: 5px 14px; cursor: pointer; font-size: 13px;
      transition: background 0.15s;
    }
    #audio-toggle.muted { background: #6e4040; }
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
  <audio id="vm-audio" src="/audio.ogg" preload="none"></audio>
  <script>
    var audio = document.getElementById('vm-audio');
    var btn   = document.getElementById('audio-toggle');
    var vol   = document.getElementById('volume-slider');
    var stat  = document.getElementById('status');
    var on    = false;

    function toggleAudio() {
      if (!on) {
        audio.volume = parseFloat(vol.value);
        audio.play().then(function() {
          on = true;
          btn.textContent = 'Mute';
          btn.classList.add('muted');
          vol.disabled = false;
          stat.textContent = 'Sound on';
        }).catch(function(e) {
          stat.textContent = 'Error: ' + e.message;
        });
      } else {
        audio.pause();
        on = false;
        btn.textContent = 'Enable Sound';
        btn.classList.remove('muted');
        vol.disabled = true;
        stat.textContent = 'Sound off';
      }
    }

    function setVolume(v) {
      audio.volume = parseFloat(v);
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

def _serve_wrapper(sock):
    resp = (
        b'HTTP/1.1 200 OK\r\n'
        b'Content-Type: text/html; charset=utf-8\r\n'
        b'Cache-Control: no-cache\r\n'
        b'Connection: close\r\n'
        b'Content-Length: ' + str(len(WRAPPER_HTML)).encode() + b'\r\n'
        b'\r\n'
    ) + WRAPPER_HTML
    try:
        sock.sendall(resp)
    except Exception:
        pass
    finally:
        try:
            sock.close()
        except Exception:
            pass

def _stream_audio(sock):
    headers = (
        b'HTTP/1.1 200 OK\r\n'
        b'Content-Type: audio/ogg\r\n'
        b'Cache-Control: no-cache\r\n'
        b'Access-Control-Allow-Origin: *\r\n'
        b'Connection: close\r\n'
        b'\r\n'
    )
    try:
        sock.sendall(headers)
    except Exception:
        sock.close()
        return

    proc = subprocess.Popen(
        ['ffmpeg', '-f', 'pulse', '-i', 'virtual_speaker.monitor',
         '-c:a', 'libopus', '-b:a', '64k', '-ar', '44100',
         '-f', 'ogg', '-'],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    try:
        while True:
            chunk = proc.stdout.read(4096)
            if not chunk:
                break
            sock.sendall(chunk)
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

    first = buf.split(b'\r\n')[0].decode('utf-8', errors='replace')
    parts = first.split(' ')
    path  = parts[1].split('?')[0] if len(parts) >= 2 else '/'
    method = parts[0] if parts else 'GET'

    if method == 'GET' and path in ('/', '/index.html'):
        threading.Thread(target=_serve_wrapper, args=(client,), daemon=True).start()
    elif path in ('/audio', '/audio.ogg'):
        threading.Thread(target=_stream_audio, args=(client,), daemon=True).start()
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
    print(f'Audio proxy listening on port {LISTEN_PORT}, forwarding other traffic to novnc on {NOVNC_PORT}', flush=True)
    while True:
        client, _ = srv.accept()
        threading.Thread(target=_handle, args=(client,), daemon=True).start()

if __name__ == '__main__':
    main()
