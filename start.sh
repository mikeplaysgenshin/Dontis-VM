#!/bin/bash
set -e

NOVNC=/nix/store/0a18wyirbc3ls9yvlw33lrmql94n2hmc-novnc-1.5.0/bin/novnc
NOVNC_SHARE=/nix/store/0a18wyirbc3ls9yvlw33lrmql94n2hmc-novnc-1.5.0/share/webapps/novnc
VNC_PORT=5901
WEB_PORT=5000
DISPLAY_NUM=1

echo "Cleaning up any previous sessions..."
pkill -f "Xvnc :${DISPLAY_NUM}" 2>/dev/null || true
pkill -f "novnc" 2>/dev/null || true
pkill -f "fluxbox" 2>/dev/null || true
rm -f /tmp/.X${DISPLAY_NUM}-lock /tmp/.X11-unix/X${DISPLAY_NUM} 2>/dev/null || true
sleep 1

echo "Setting up custom noVNC web directory..."
mkdir -p /tmp/novnc-web
ln -sf $NOVNC_SHARE/* /tmp/novnc-web/ 2>/dev/null || true
cat > /tmp/novnc-web/index.html <<'EOF'
<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <title>BlobeVM Desktop</title>
  <meta http-equiv="refresh" content="0; url=vnc.html?autoconnect=true&password=password&resize=scale">
  <script>window.location.href = 'vnc.html?autoconnect=true&password=password&resize=scale';</script>
</head>
<body>
  <p>Loading BlobeVM Desktop... <a href="vnc.html?autoconnect=true&password=password&resize=scale">Click here if not redirected</a></p>
</body>
</html>
EOF

echo "Setting up VNC password..."
mkdir -p ~/.vnc
echo "password" | vncpasswd -f > ~/.vnc/passwd
chmod 600 ~/.vnc/passwd

echo "Starting Xvnc on port ${VNC_PORT}..."
Xvnc :${DISPLAY_NUM} \
  -rfbport ${VNC_PORT} \
  -rfbauth ~/.vnc/passwd \
  -geometry 1280x720 \
  -depth 24 \
  -SecurityTypes VncAuth \
  &>/tmp/xvnc.log &

echo "Waiting for Xvnc to start..."
sleep 3

echo "Starting Fluxbox window manager..."
DISPLAY=:${DISPLAY_NUM} fluxbox &>/tmp/fluxbox.log &
sleep 1

echo "Starting noVNC web interface on port ${WEB_PORT}..."
exec $NOVNC --listen ${WEB_PORT} --vnc localhost:${VNC_PORT} --web /tmp/novnc-web
