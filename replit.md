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
3. Starts Xvnc on port 5000
4. Starts Fluxbox window manager

## Original Project
The original BlobeVM project (in `BlobeVM-main/`) uses Docker + KasmVNC to provide a full desktop environment. The Dockerfile builds an Ubuntu image with a choice of desktop environment (XFCE4, KDE, GNOME, etc.). This Docker approach does not work in Replit due to security restrictions on running privileged containers.

## Files
- `start.sh` — Workflow startup script (Xvnc + Fluxbox)
- `BlobeVM-main/` — Original BlobeVM project (Docker-based, for reference)
  - `Dockerfile` — Docker image definition
  - `installer.py` — TUI installer for selecting DE and apps
  - `options.json` — Selected options (XFCE4, no extra apps)
  - `root/` — Scripts for installing desktop environments and apps
