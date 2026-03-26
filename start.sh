#!/bin/bash
set -e

export DISPLAY=:1

echo "Cleaning up any previous VNC sessions..."
pkill -f "Xvnc :1" 2>/dev/null || true
pkill -f "fluxbox" 2>/dev/null || true
rm -f /tmp/.X1-lock /tmp/.X11-unix/X1 2>/dev/null || true
sleep 1

echo "Setting up VNC password..."
mkdir -p ~/.vnc
echo "password" | vncpasswd -f > ~/.vnc/passwd
chmod 600 ~/.vnc/passwd

echo "Starting Xvnc on display :1..."
Xvnc :1 \
  -rfbport 5000 \
  -rfbauth ~/.vnc/passwd \
  -geometry 1280x720 \
  -depth 24 \
  -SecurityTypes VncAuth \
  &>/tmp/xvnc.log &

echo "Waiting for Xvnc to be ready..."
for i in $(seq 1 15); do
  if xdpyinfo -display :1 &>/dev/null; then
    echo "Xvnc is ready."
    break
  fi
  sleep 1
done

echo "Starting fluxbox window manager..."
DISPLAY=:1 fluxbox &>/tmp/fluxbox.log &

echo "Desktop is running. Connect via VNC on port 5000."
echo "Password: password"

wait
