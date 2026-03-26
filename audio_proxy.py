#!/usr/bin/env python3
"""
TCP-level proxy on port 5000:
  /audio.ogg  -> streams PulseAudio via ffmpeg (Ogg/Opus)
  everything else -> proxied to novnc/websockify on internal port 4998
"""
import socket
import threading
import subprocess
import select
import sys
import os

LISTEN_PORT = 5000
NOVNC_PORT  = 4998

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

def _stream_audio(sock):
    """Send HTTP 200 then pipe ffmpeg Ogg/Opus from PulseAudio monitor."""
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

    if path in ('/audio', '/audio.ogg'):
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
