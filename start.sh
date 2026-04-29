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
# websockify forks children per connection; SIGKILL reaches all of them
kill -9 $(pgrep -f "websockify" 2>/dev/null) 2>/dev/null || true
pkill -9 -f "novnc" 2>/dev/null || true
pkill -f "fluxbox" 2>/dev/null || true
pkill -9 -f "chromium" 2>/dev/null || true
pkill -f "pulseaudio" 2>/dev/null || true
pkill -9 -f "audio_proxy.py" 2>/dev/null || true
sleep 2
rm -f /tmp/.X${DISPLAY_NUM}-lock /tmp/.X11-unix/X${DISPLAY_NUM} 2>/dev/null || true

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

echo "Configuring Fluxbox (with visible taskbar)..."
mkdir -p ~/.fluxbox
cat > ~/.fluxbox/init <<'FLUX_EOF'
session.screen0.toolbar.visible:                true
session.screen0.toolbar.autoHide:               false
session.screen0.toolbar.placement:              BottomCenter
session.screen0.toolbar.widthPercent:           100
session.screen0.toolbar.height:                 28
session.screen0.toolbar.alpha:                  230
session.screen0.toolbar.tools:                  prevworkspace, workspacename, nextworkspace, iconbar, systemtray, clock
session.screen0.toolbar.onhead:                 1
session.screen0.iconbar.mode:                   {static groups} (workspace)
session.screen0.iconbar.iconTextPadding:        10
session.screen0.iconbar.alignment:              Relative
session.screen0.iconbar.usePixmap:              true
session.screen0.workspaces:                     1
session.screen0.workspaceNames:                 BlobeVM,
session.screen0.clockFormat:                    %H:%M
session.screen0.fullMaximization:               false
session.screen0.focusModel:                     ClickFocus
session.screen0.windowMenu:
session.screen0.defaultDeco:                    NORMAL
session.screen0.allowRemoteActions:             false
session.screen0.tabs.usePixmap:                 true
session.screen0.tab.placement:                  TopLeft
session.screen0.tab.width:                      64
session.menuFile:                               ~/.fluxbox/menu
FLUX_EOF

export CHROME_DEVEL_SANDBOX=/nix/store/c5mij30612sfy40hl94yr5vcrhw17nwb-chromium-138.0.7204.100-sandbox/bin/__chromium-suid-sandbox
# Route audio through PulseAudio
export PULSE_SERVER=/var/run/pulse/native
# Use a dedicated user-data-dir so we never collide with Replit's screenshot Chromium
CHROMIUM_PROFILE=/tmp/blobevm-chrome
mkdir -p "$CHROMIUM_PROFILE"

# Launcher scripts: same command used at startup and from the right-click menu / keys
cat > /tmp/blobevm-launch-chromium.sh <<EOF
#!/bin/bash
export DISPLAY=:${DISPLAY_NUM}
export PULSE_SERVER=/var/run/pulse/native
export CHROME_DEVEL_SANDBOX="$CHROME_DEVEL_SANDBOX"
# Clear singleton locks in case a previous Chromium left them behind
rm -f "$CHROMIUM_PROFILE/SingletonLock" "$CHROMIUM_PROFILE/SingletonCookie" "$CHROMIUM_PROFILE/SingletonSocket" 2>/dev/null
exec "$CHROMIUM_BIN" \\
  --no-sandbox \\
  --disable-dev-shm-usage \\
  --disable-setuid-sandbox \\
  --disable-gpu \\
  --no-first-run \\
  --no-default-browser-check \\
  --disable-background-networking \\
  --disable-features=Translate,MediaRouter \\
  --user-data-dir="$CHROMIUM_PROFILE" \\
  --window-position=0,0 \\
  --window-size=1280,680 \\
  "https://www.google.com" >/dev/null 2>&1
EOF
chmod +x /tmp/blobevm-launch-chromium.sh

cat > /tmp/blobevm-launch-terminal.sh <<EOF
#!/bin/bash
export DISPLAY=:${DISPLAY_NUM}
# Paste support:
#   Ctrl+V          -> paste from CLIPBOARD (what noVNC syncs from your host)
#   Shift+Insert    -> paste from PRIMARY (xterm default, kept)
#   Ctrl+Shift+C    -> copy selection to CLIPBOARD (so it syncs back to host)
# selectToClipboard=true: highlighting text in xterm auto-copies to CLIPBOARD
exec "$XTERM" -fa Monospace -fs 12 -title Terminal \\
  -bg '#0d1117' -fg '#e6edf3' -geometry 100x20+50+400 \\
  -xrm 'XTerm*selectToClipboard: true' \\
  -xrm 'XTerm.vt100.translations: #override \\
      Ctrl <Key> V: insert-selection(CLIPBOARD) \\n\\
      Ctrl Shift <Key> V: insert-selection(CLIPBOARD) \\n\\
      Ctrl Shift <Key> C: copy-selection(CLIPBOARD) \\n\\
      Shift <Key> Insert: insert-selection(PRIMARY)' \\
  -hold -e bash
EOF
chmod +x /tmp/blobevm-launch-terminal.sh

# Right-click menu
cat > ~/.fluxbox/menu <<'MENU_EOF'
[begin] (BlobeVM)
  [exec] (Chromium Browser)  {/tmp/blobevm-launch-chromium.sh}
  [exec] (Terminal)          {/tmp/blobevm-launch-terminal.sh}
  [separator]
  [submenu] (Window Manager)
    [restart] (Restart Fluxbox)
    [reconfig] (Reload Config)
  [end]
  [separator]
  [exit] (Log Out)
[end]
MENU_EOF

# Keyboard shortcuts: Alt+B = Browser, Alt+T = Terminal, Alt+F2 = command launcher
cat > ~/.fluxbox/keys <<'KEYS_EOF'
Mod1 b :Exec /tmp/blobevm-launch-chromium.sh
Mod1 t :Exec /tmp/blobevm-launch-terminal.sh
Mod1 Tab :NextWindow
Mod1 Shift Tab :PrevWindow
Mod1 F4 :Close
KEYS_EOF

echo "Starting Fluxbox window manager..."
fluxbox -rc ~/.fluxbox/init -log /tmp/fluxbox.log &>/tmp/fluxbox-stderr.log &
sleep 2

echo "Launching Chromium browser..."
/tmp/blobevm-launch-chromium.sh &
sleep 3

echo "Launching terminal..."
/tmp/blobevm-launch-terminal.sh &
sleep 1

echo "Starting noVNC on internal port ${NOVNC_INTERNAL_PORT}..."
$NOVNC --listen ${NOVNC_INTERNAL_PORT} --vnc localhost:${VNC_PORT} --web /tmp/novnc-web &>/tmp/novnc.log &
sleep 2

echo "Starting audio proxy on port ${WEB_PORT}..."
exec python3 audio_proxy.py
