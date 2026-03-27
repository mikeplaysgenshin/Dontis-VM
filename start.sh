#!/bin/bash
set -e

NOVNC=/nix/store/0a18wyirbc3ls9yvlw33lrmql94n2hmc-novnc-1.5.0/bin/novnc
NOVNC_SHARE=/nix/store/0a18wyirbc3ls9yvlw33lrmql94n2hmc-novnc-1.5.0/share/webapps/novnc
XTERM=/nix/store/ai4gqjimfc2ji48y3v0b2z7f9av6xwfn-xterm-397/bin/xterm
XSETROOT=/nix/store/21rcnlwxh0qvlc12whjiscb5qmf5nq8a-xsetroot-1.1.3/bin/xsetroot
CHROMIUM_BIN=/nix/store/884ygjschxqkrkpkrhq83bicvzgj7vb8-chromium-unwrapped-138.0.7204.100/libexec/chromium/chromium
PULSEAUDIO=/nix/store/px08h5pmb6vr98y751ck1gwn0852iqqq-pulseaudio-17.0/bin/pulseaudio
PACTL=/nix/store/px08h5pmb6vr98y751ck1gwn0852iqqq-pulseaudio-17.0/bin/pactl
VNC_PORT=5901
NOVNC_INTERNAL_PORT=4998
WEB_PORT=5000
DISPLAY_NUM=1
export DISPLAY=:${DISPLAY_NUM}

echo "Cleaning up any previous sessions..."
pkill -f "Xvnc :${DISPLAY_NUM}" 2>/dev/null || true
pkill -f "novnc" 2>/dev/null || true
pkill -f "fluxbox" 2>/dev/null || true
pkill -f "chromium" 2>/dev/null || true
pkill -f "pulseaudio" 2>/dev/null || true
pkill -f "audio_proxy.py" 2>/dev/null || true
rm -f /tmp/.X${DISPLAY_NUM}-lock /tmp/.X11-unix/X${DISPLAY_NUM} 2>/dev/null || true
sleep 1

echo "Setting up PulseAudio..."
mkdir -p ~/.config/pulse
cat > ~/.config/pulse/daemon.conf <<'PULSE_EOF'
default-sample-rate = 44100
default-sample-channels = 2
default-sample-format = s16le
# Reduce PulseAudio's internal buffer to cut latency from ~100ms to ~10ms
default-fragments = 2
default-fragment-size-msec = 5
PULSE_EOF

$PULSEAUDIO --start --log-target=file:/tmp/pulse.log --exit-idle-time=-1 2>/dev/null || true
sleep 2

# Load null sink so ffmpeg can capture from null.monitor
$PACTL load-module module-null-sink 2>/dev/null || true
$PACTL set-default-sink null 2>/dev/null || true

echo "Setting up noVNC web directory..."
mkdir -p /tmp/novnc-web
ln -sf $NOVNC_SHARE/* /tmp/novnc-web/ 2>/dev/null || true
# Use a clean vnc.html (no audio injection — audio controls are in the wrapper page)
rm -f /tmp/novnc-web/vnc.html
cp "$NOVNC_SHARE/vnc.html" /tmp/novnc-web/vnc.html

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

echo "Setting up CJK fonts..."
NOTO_CJK_SANS=/nix/store/6jh0rswqwn4bif41mvyyyc49fvnfwr89-noto-fonts-cjk-sans-2.004/share/fonts/opentype/noto-cjk
NOTO_CJK_SERIF=/nix/store/1xabh12b0c3v4c69n094ny813d606wsx-noto-fonts-cjk-serif-2.002/share/fonts/opentype/noto-cjk
mkdir -p ~/.fonts
cp -f $NOTO_CJK_SANS/*.ttc ~/.fonts/ 2>/dev/null || true
cp -f $NOTO_CJK_SERIF/*.ttc ~/.fonts/ 2>/dev/null || true
fc-cache -f ~/.fonts/ 2>/dev/null || true

echo "Setting desktop background..."
$XSETROOT -solid '#1a3a5c'

echo "Starting Fluxbox window manager..."
fluxbox &>/tmp/fluxbox.log &
sleep 2

echo "Launching Chromium browser..."
export CHROME_DEVEL_SANDBOX=/nix/store/c5mij30612sfy40hl94yr5vcrhw17nwb-chromium-138.0.7204.100-sandbox/bin/__chromium-suid-sandbox
# Tell Chromium explicitly where PulseAudio is so it routes audio through the null sink
export PULSE_SERVER=/var/run/pulse/native
# Remove stale Chromium lock files to prevent "profile in use" startup failures.
# Chromium resolves its user-data-dir relative to the working directory, so the locks
# end up in workspace/.config/chromium/, not $HOME/.config/chromium/.
rm -f .config/chromium/SingletonLock .config/chromium/SingletonCookie .config/chromium/SingletonSocket 2>/dev/null || true
rm -f ~/.config/chromium/SingletonLock ~/.config/chromium/SingletonCookie ~/.config/chromium/SingletonSocket 2>/dev/null || true
rm -rf /tmp/.org.chromium.Chromium.* 2>/dev/null || true
$CHROMIUM_BIN \
  --no-sandbox \
  --disable-dev-shm-usage \
  --disable-setuid-sandbox \
  --no-first-run \
  --disable-background-networking \
  "https://www.google.com" \
  &>/tmp/chromium.log &
sleep 2

echo "Launching terminal..."
$XTERM -fa 'Monospace' -fs 12 -title 'Terminal' -bg '#0d1117' -fg '#e6edf3' -geometry 100x20+50+400 &
sleep 1

echo "Starting noVNC on internal port ${NOVNC_INTERNAL_PORT}..."
$NOVNC --listen ${NOVNC_INTERNAL_PORT} --vnc localhost:${VNC_PORT} --web /tmp/novnc-web &>/tmp/novnc.log &
sleep 2

echo "Starting audio proxy on port ${WEB_PORT}..."
exec python3 audio_proxy.py
