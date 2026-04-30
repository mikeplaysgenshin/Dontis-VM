# BlobeVM on Replit

## Overview
BlobeVM is a virtual desktop environment that runs in a web browser. In this Replit setup, it runs using Xvnc (TigerVNC) + Fluxbox window manager directly — without Docker, since Docker daemon privileges are not available in the Replit sandbox.

## Architecture
- **Display server**: Xvnc (TigerVNC) on display :1, port 5000
- **Window manager**: Fluxbox
- **Workflow output**: VNC (Replit native VNC preview)
- **VNC password**: `password`

## How to Run
The "Start application" workflow runs `start.sh` which:
1. Cleans up any previous VNC sessions
2. Sets up VNC authentication
3. Starts Xvnc on port 5901
4. Sets desktop background color
5. Starts Fluxbox window manager
6. Launches Chromium browser open to google.com
7. Launches an xterm terminal
8. Starts noVNC on port 5000 for browser access

## Installed Apps
- **Chromium 138** — Opens Google on startup (from Nix store)
- **xterm** — Terminal emulator (from Nix store)

## Top-Bar Features (`audio_proxy.py`)
The wrapper page on port 5000 adds a small toolbar above the noVNC iframe:
- **Enable Sound** — Connects a WebSocket to `/audio-ws`, streams raw PCM audio from the VM's PulseAudio null-sink monitor, and plays it through the browser's Web Audio API. Live byte-rate indicator shows whether anything is actually playing.
- **Test Tone** — `POST /test-tone` plays a 2-second 440 Hz sine into the VM's default sink so the user can verify the full audio pipeline.
- **Paste from Clipboard** — `POST /paste` pushes the browser clipboard into X11 CLIPBOARD and synthesizes Ctrl+V into the focused VM window. Falls back to a prompt dialog if the browser blocks `navigator.clipboard.readText()`.
- **Open Downloaded ▾** — Dropdown that lists files in `~/Downloads` via `GET /downloads` and runs them via `POST /run`. Detects file type (AppImage / shell script / ELF / archive / .deb) and chooses the right launcher (AppImages run with `--appimage-extract-and-run` since `/dev/fuse` is unavailable in the container). Captures stderr for ~2 s and surfaces real error messages (missing libraries, etc.) inline in the dropdown when a launch fails fast.

## Runtime Libraries for Downloaded Binaries
Random AppImages and downloaded ELF binaries dynamically link against system libraries that aren't on the default Nix path. The launcher merges `REPLIT_LD_LIBRARY_PATH` and `REPLIT_PYTHON_LD_LIBRARY_PATH` into `LD_LIBRARY_PATH` for spawned processes. We pre-install a common set so most apps work out of the box: `libdrm`, `alsa-lib`, `libxkbcommon`, `fontconfig`, `freetype`, `wayland`, `libelf`, `glib`, `pcre2`, `e2fsprogs`, `libgpg-error`, `zlib`, `gcc-unwrapped`, `xorg.libX11`, `xorg.libxcb`, `xorg.xcbutil*`, `xorg.libXrender`, `xorg.libXfixes`, `xorg.libXcomposite`, `xorg.libXdamage`, `xorg.libXtst`, `xorg.libXScrnSaver`, `dbus`, `nspr`, `nss`, `expat`, `libGL`, `SDL2`, `libpulseaudio`. If a downloaded app reports another missing `.so`, install the corresponding Nix package and restart the workflow.

## Original Project
The original BlobeVM project (in `BlobeVM-main/`) uses Docker + KasmVNC to provide a full desktop environment. The Dockerfile builds an Ubuntu image with a choice of desktop environment (XFCE4, KDE, GNOME, etc.). This Docker approach does not work in Replit due to security restrictions on running privileged containers.

## Files
- `start.sh` — Workflow startup script (Xvnc + Fluxbox)
- `BlobeVM-main/` — Original BlobeVM project (Docker-based, for reference)
  - `Dockerfile` — Docker image definition
  - `installer.py` — TUI installer for selecting DE and apps
  - `options.json` — Selected options (XFCE4, no extra apps)
  - `root/` — Scripts for installing desktop environments and apps
