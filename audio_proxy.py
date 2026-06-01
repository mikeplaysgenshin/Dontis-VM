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
      display: flex; align-items: center; gap: 8px;
      background: #161b22; border-bottom: 1px solid #30363d;
      padding: 4px 12px; height: 40px; flex-shrink: 0;
      color: #e6edf3; font-size: 12px; user-select: none;
      overflow-x: auto; overflow-y: hidden;
      white-space: nowrap; scrollbar-width: none;
    }
    #audio-bar::-webkit-scrollbar { display: none; }
    #audio-bar > * { flex-shrink: 0; }
    #audio-bar span { opacity: 0.7; }
    .bar-btn {
      background: #238636; border: none; border-radius: 6px;
      color: #fff; padding: 4px 10px; cursor: pointer; font-size: 12px;
      transition: background 0.15s; white-space: nowrap;
    }
    #audio-toggle.active { background: #8250df; }
    .bar-btn:hover { filter: brightness(1.15); }
    .bar-btn.secondary { background: #30363d; }
    #volume-slider { accent-color: #238636; cursor: pointer; width: 90px; }
    #status { font-size: 12px; opacity: 0.5; margin-left: auto; }
    #paste-status { font-size: 11px; opacity: 0.6; min-width: 80px; }
    #vnc-frame { flex: 1; border: none; width: 100%; min-height: 0; }
    /* ── Toolbar hide / fullscreen ─────────────────────────────────────────── */
    #audio-bar {
      transition: transform 0.22s ease, opacity 0.22s ease;
    }
    /* When bar is hidden, slide it up off-screen */
    body.bar-hidden #audio-bar {
      transform: translateY(-100%);
      opacity: 0;
      pointer-events: none;
      position: fixed; top: 0; left: 0; right: 0; z-index: 200;
    }
    /* VNC frame fills full height when bar is hidden */
    body.bar-hidden #vnc-frame { position: fixed; inset: 0; }
    /* Reveal strip — invisible 10px hot zone at top edge,
       visible only when bar is hidden so user can drag it back */
    #bar-reveal {
      display: none;
      position: fixed; top: 0; left: 0; right: 0; height: 10px;
      z-index: 9999; cursor: n-resize;
      background: linear-gradient(to bottom, rgba(35,134,54,0.55), transparent);
    }
    body.bar-hidden #bar-reveal { display: block; }
    /* Collapse / expand button */
    #bar-toggle-btn { margin-left: auto; opacity: 0.6; font-size: 11px; padding: 2px 7px; }
    #bar-toggle-btn:hover { opacity: 1; }
    /* Fullscreen button */
    #fs-btn { font-size: 13px; padding: 3px 8px; }
    /* ──────────────────────────────────────────────────────────────────────── */
    /* Game cursor mode ---------------------------------------------------- */
    /* In game mode the browser cursor is hidden so only the VM cursor shows. */
    body.game-mode #vnc-frame { cursor: none; }
    #game-mode-btn.active { background: #da3633 !important; color: #fff; }
    /* Touch overlay — always present, forwards finger gestures as mouse events */
    #touch-overlay {
      position: fixed;
      inset: 40px 0 0 0;
      z-index: 10;
      background: transparent;
      touch-action: none;
    }
    body.bar-hidden #touch-overlay { inset: 0; }
    /* Transparent overlay sits over the VNC iframe when game mode is on.
       It owns the pointer-lock and forwards all input to the iframe below.  */
    #game-overlay {
      display: none;
      position: fixed;
      /* sits just below the 40px toolbar, covering the whole VNC frame */
      inset: 40px 0 0 0;
      z-index: 50;
      cursor: none;
      background: transparent;
    }
    body.game-mode #game-overlay { display: block; }
    /* Hint banner shown in the centre of the VNC area */
    #game-hint {
      display: none;
      position: fixed;
      top: 50%; left: 50%;
      transform: translate(-50%, -50%);
      background: rgba(0,0,0,0.75);
      color: #fff;
      padding: 14px 28px;
      border-radius: 10px;
      font-size: 15px;
      pointer-events: none;
      z-index: 60;
      text-align: center;
      line-height: 1.6;
    }
    /* -------------------------------------------------------------------- */
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
    <button id="audio-toggle" class="bar-btn" onclick="toggleAudio()" title="Enable/mute VM audio">&#128266; Sound</button>
    <input type="range" id="volume-slider" min="0" max="1" step="0.05" value="0.8"
           oninput="setVolume(this.value)" title="Volume" disabled>
    <button id="test-tone-btn" class="bar-btn secondary" onclick="playTestTone()"
            title="Play a 2-second beep to verify sound is working">Tone</button>
    <span style="opacity:0.3;">|</span>
    <button id="paste-btn" class="bar-btn secondary" onclick="pasteToVM()"
            title="Paste your clipboard into the focused VM window">&#128203; Paste</button>
    <span style="opacity:0.3;">|</span>
    <button id="mgba-btn" class="bar-btn secondary" onclick="launchMgba()"
            title="Open the mGBA Game Boy Advance emulator (also: Alt+G inside the VM)">&#127918; mGBA</button>
    <button id="game-mode-btn" class="bar-btn secondary" onclick="toggleGameMode()"
            title="Game Cursor Mode: hides the browser cursor so only the game&#39;s cursor shows, and locks the mouse inside the VM so it can&#39;t escape. Click again or press Esc to exit.">&#127918; Game Cursor</button>
    <input type="range" id="sens-slider" min="0.1" max="2.0" step="0.05" value="0.5"
           oninput="gameSens=parseFloat(this.value);document.getElementById('sens-label').textContent=Math.round(gameSens*100)+'%'"
           title="Game cursor sensitivity" style="width:70px;accent-color:#da3633;cursor:pointer;flex-shrink:0;">
    <span id="sens-label" style="font-size:11px;opacity:0.6;min-width:30px;">50%</span>
    <span style="opacity:0.3;">|</span>
    <div class="menu-wrap">
      <button id="files-btn" class="bar-btn secondary" onclick="toggleFilesMenu()"
              title="List files in ~/Downloads inside the VM and open them">&#128193; Downloads &#9662;</button>
      <div id="files-menu" class="menu-pop"></div>
    </div>
    <span id="paste-status"></span>
    <span id="status">Sound off</span>
    <button id="fs-btn" class="bar-btn secondary" onclick="toggleFullscreen()"
            title="Fullscreen (keeps toolbar visible). Ctrl+B = hide/show toolbar.">&#x26F6;</button>
    <button id="bar-toggle-btn" class="bar-btn secondary" onclick="toggleBar()"
            title="Hide toolbar (Ctrl+B). When hidden, click the green strip at the top to show it again.">&#9650;</button>
  </div>
  <!-- Reveal strip: click or hover to bring toolbar back when it is hidden -->
  <div id="bar-reveal" onclick="showBar()" title="Click to show toolbar (or press Ctrl+B)"></div>
  <iframe id="vnc-frame"
    src="/vnc.html?autoconnect=true&password=password&resize=scale"
    allow="fullscreen">
  </iframe>
  <!-- Touch overlay — forwards finger gestures to the noVNC canvas as mouse events -->
  <div id="touch-overlay"></div>
  <!-- Game cursor overlay — owns pointer-lock; forwards events to iframe -->
  <div id="game-overlay"></div>
  <!-- Hint shown to user when game mode is active -->
  <div id="game-hint"></div>
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

    function launchMgba() {
      var b = document.getElementById('mgba-btn');
      b.disabled = true;
      var orig = b.textContent;
      b.textContent = 'Launching...';
      fetch('/launch-mgba', { method: 'POST' })
        .then(function(r) { return r.text().then(function(t){ return {ok:r.ok, text:t}; }); })
        .then(function(res) {
          if (res.ok) {
            b.textContent = 'Opened!';
          } else {
            b.textContent = 'Failed';
            alert('mGBA failed to start:\\n\\n' + (res.text || '(no error output)'));
          }
        })
        .catch(function(err) { b.textContent = 'Error'; alert('Network error: ' + err); })
        .finally(function() {
          setTimeout(function() { b.textContent = orig; b.disabled = false; }, 2500);
        });
    }

    // ── Game Cursor Mode ─────────────────────────────────────────────────────
    // Solves two problems for action/RPG games like Genshin:
    //   1. Double cursor: browser arrow on top of the game's own cursor.
    //      Fix: cursor:none on the VNC frame so only the VM cursor is visible.
    //   2. Mouse escaping: fast camera swings send the cursor outside the VNC
    //      window. Fix: Pointer Lock on the overlay div keeps the mouse captured
    //      and we forward all mouse events (with accumulated deltas) to the
    //      noVNC canvas via synthetic events (same-origin = allowed).
    var gameMode   = false;   // true while game cursor mode is on
    var gameLocked = false;   // true while browser has granted Pointer Lock
    var gameCurX   = 0;       // virtual cursor X relative to VNC frame
    var gameCurY   = 0;       // virtual cursor Y relative to VNC frame
    var gameSens   = 0.5;     // sensitivity multiplier (slider default = 50%)
    var gameHintTimer = null;

    var gameOverlay = document.getElementById('game-overlay');
    var gameHint    = document.getElementById('game-hint');

    function showHint(msg, autohideMs) {
      if (gameHintTimer) { clearTimeout(gameHintTimer); gameHintTimer = null; }
      if (!msg) { gameHint.style.display = 'none'; gameHint.textContent = ''; return; }
      gameHint.textContent = msg;
      gameHint.style.display = 'block';
      if (autohideMs) {
        gameHintTimer = setTimeout(function() {
          gameHint.style.display = 'none';
          gameHint.textContent   = '';
          gameHintTimer = null;
        }, autohideMs);
      }
    }

    // Get the noVNC canvas from the same-origin iframe.
    function getVncCanvas() {
      try {
        var frame = document.getElementById('vnc-frame');
        return frame.contentDocument && frame.contentDocument.querySelector('canvas');
      } catch (e) { return null; }
    }

    // Dispatch a synthetic MouseEvent to the noVNC canvas.
    // x, y are already IFRAME-relative (relative to the top-left of the VNC
    // frame), which is what noVNC's mouse handler expects when it subtracts
    // the canvas's getBoundingClientRect() from clientX/clientY.
    // We pass view:frame.contentWindow so the event lives in the iframe's
    // coordinate space — do NOT add rect.left/top here.
    function fwdMouse(type, srcEvt, x, y) {
      var canvas = getVncCanvas();
      if (!canvas) return;
      var frame = document.getElementById('vnc-frame');
      try {
        canvas.dispatchEvent(new MouseEvent(type, {
          bubbles: true, cancelable: true,
          view: frame.contentWindow,
          screenX: x, screenY: y,
          clientX: x, clientY: y,
          movementX: srcEvt.movementX || 0,
          movementY: srcEvt.movementY || 0,
          button:  srcEvt.button  || 0,
          buttons: srcEvt.buttons || 0,
          shiftKey: srcEvt.shiftKey, ctrlKey: srcEvt.ctrlKey,
          altKey:   srcEvt.altKey,  metaKey:  srcEvt.metaKey,
        }));
      } catch (e) {}
    }

    function toggleGameMode() {
      if (gameMode) { exitGameMode(); } else { enterGameMode(); }
    }

    function enterGameMode() {
      gameMode = true;
      document.body.classList.add('game-mode');
      var gbtn = document.getElementById('game-mode-btn');
      gbtn.textContent = '\\u{1F3AE} Exit Game Mode';
      gbtn.classList.add('active');
      showHint('\\u{1F3AE} Game Cursor ON\\nClick the VM to lock mouse\\n(Esc = release lock, click button again = exit)', 0);
    }

    function exitGameMode() {
      gameMode   = false;
      gameLocked = false;
      document.body.classList.remove('game-mode');
      var gbtn = document.getElementById('game-mode-btn');
      gbtn.textContent = '\\u{1F3AE} Game Cursor';
      gbtn.classList.remove('active');
      if (document.exitPointerLock) document.exitPointerLock();
      showHint('');
    }

    // Clicking the overlay (which is over the VNC frame in game mode)
    // requests pointer lock.  preventDefault() stops the overlay from
    // stealing focus away from the iframe so keyboard input keeps going to the VM.
    gameOverlay.addEventListener('mousedown', function(e) {
      e.preventDefault();
      if (!gameLocked) {
        // Initialise virtual cursor to wherever the user clicked
        var frame = document.getElementById('vnc-frame');
        var rect  = frame.getBoundingClientRect();
        gameCurX  = Math.max(0, Math.min(rect.width  - 1, e.clientX - rect.left));
        gameCurY  = Math.max(0, Math.min(rect.height - 1, e.clientY - rect.top));
        gameOverlay.requestPointerLock();
      } else {
        // Already locked: forward the click to noVNC
        fwdMouse('mousedown', e, gameCurX, gameCurY);
      }
    });

    gameOverlay.addEventListener('mouseup', function(e) {
      if (gameLocked) { e.preventDefault(); fwdMouse('mouseup', e, gameCurX, gameCurY); }
    });

    // Suppress right-click context menu in game mode; forward the button events
    gameOverlay.addEventListener('contextmenu', function(e) { e.preventDefault(); });

    // Pointer Lock state changes
    document.addEventListener('pointerlockchange', function() {
      if (document.pointerLockElement === gameOverlay) {
        // Lock just acquired
        gameLocked = true;
        // Give keyboard focus to the iframe so game keys work
        try { document.getElementById('vnc-frame').contentWindow.focus(); } catch(e) {}
        showHint('\\u{1F512} Mouse locked — Esc to release', 2500);
      } else if (gameLocked) {
        // Lock was released (user pressed Esc or browser forced it)
        gameLocked = false;
        if (gameMode) {
          showHint('\\u{1F513} Mouse released — click VM to re-lock', 0);
        }
      }
    });

    document.addEventListener('pointerlockerror', function() {
      showHint('\\u26A0 Pointer Lock denied — try clicking inside the VM first', 2500);
    });

    // Forward mouse MOVEMENT when locked, accumulating deltas into virtual pos
    document.addEventListener('mousemove', function(e) {
      if (!gameLocked) return;
      var frame = document.getElementById('vnc-frame');
      var rect  = frame.getBoundingClientRect();
      gameCurX  = Math.max(0, Math.min(rect.width  - 1, gameCurX + e.movementX * gameSens));
      gameCurY  = Math.max(0, Math.min(rect.height - 1, gameCurY + e.movementY * gameSens));
      fwdMouse('mousemove', e, gameCurX, gameCurY);
    });

    // Forward scroll/wheel events (gameCurX/Y are already iframe-relative)
    document.addEventListener('wheel', function(e) {
      if (!gameLocked) return;
      var canvas = getVncCanvas();
      if (!canvas) return;
      var frame = document.getElementById('vnc-frame');
      try {
        canvas.dispatchEvent(new WheelEvent('wheel', {
          bubbles: true, cancelable: true,
          view: frame.contentWindow,
          clientX: gameCurX, clientY: gameCurY,
          deltaX: e.deltaX, deltaY: e.deltaY, deltaZ: e.deltaZ,
          deltaMode: e.deltaMode,
        }));
      } catch (er) {}
    }, { passive: true });
    // ── End Game Cursor Mode ─────────────────────────────────────────────────

    // ── Touch Control ────────────────────────────────────────────────────────
    // Maps finger gestures to mouse events forwarded to the noVNC canvas.
    //   1-finger tap/drag  → left mouse button
    //   2-finger tap        → right-click (context menu)
    //   2-finger drag       → scroll wheel
    //   long-press (600 ms) → right-click
    (function() {
      var overlay   = document.getElementById('touch-overlay');
      var lastX = 0, lastY = 0;       // last iframe-relative pos
      var twoStart  = false;          // did a 2-finger gesture start?
      var twoMoved  = false;          // did the 2-finger gesture move (scroll)?
      var prev2MidY = 0;              // previous 2-finger midpoint Y (iframe-rel)
      var longTimer = null;           // long-press timer handle
      var LONG_MS   = 600;            // ms threshold for long-press

      function getFrame() { return document.getElementById('vnc-frame'); }

      function toIframe(touch) {
        var r = getFrame().getBoundingClientRect();
        return {
          x: Math.max(0, Math.min(r.width  - 1, touch.clientX - r.left)),
          y: Math.max(0, Math.min(r.height - 1, touch.clientY - r.top))
        };
      }

      // Dispatch a mouse event directly to the noVNC canvas.
      // mousedown/mouseup/mousemove MUST go here — noVNC's setCapture polyfill
      // creates a full-screen capture proxy div (z-index 10000) on mousedown,
      // which would fool elementFromPoint on every subsequent call.
      function sendToCanvas(type, x, y, button, buttons) {
        var frame = getFrame();
        var canvas = getVncCanvas();
        if (!frame || !canvas) return;
        try {
          var ME = frame.contentWindow.MouseEvent || MouseEvent;
          canvas.dispatchEvent(new ME(type, {
            bubbles: true, cancelable: true,
            view: frame.contentWindow,
            screenX: x, screenY: y, clientX: x, clientY: y,
            button: button || 0, buttons: buttons !== undefined ? buttons : 0,
          }));
        } catch(e) {}
      }

      // Dispatch a click to whatever element actually sits at (x,y) inside the
      // iframe — needed for noVNC sidebar buttons and other overlay UI that
      // respond to click, not raw mousedown/mouseup.
      function sendClick(x, y) {
        var frame = getFrame();
        if (!frame || !frame.contentDocument) return;
        var target = frame.contentDocument.elementFromPoint(x, y) || getVncCanvas();
        if (!target) return;
        try {
          var ME = frame.contentWindow.MouseEvent || MouseEvent;
          target.dispatchEvent(new ME('click', {
            bubbles: true, cancelable: true,
            view: frame.contentWindow,
            screenX: x, screenY: y, clientX: x, clientY: y,
            button: 0, buttons: 0,
          }));
        } catch(e) {}
      }

      // Keep sendMouse as alias used by touch scroll / long-press paths
      function sendMouse(type, x, y, button, buttons) {
        if (type === 'click') { sendClick(x, y); }
        else { sendToCanvas(type, x, y, button, buttons); }
      }

      function rightClick(x, y) {
        sendToCanvas('mousedown', x, y, 2, 2);
        sendToCanvas('mouseup',   x, y, 2, 0);
      }

      function cancelLong() {
        if (longTimer) { clearTimeout(longTimer); longTimer = null; }
      }

      overlay.addEventListener('touchstart', function(e) {
        e.preventDefault();
        cancelLong();
        if (e.touches.length === 1) {
          var p = toIframe(e.touches[0]);
          lastX = p.x; lastY = p.y;
          twoStart = false; twoMoved = false;
          // Start long-press timer
          longTimer = setTimeout(function() {
            longTimer = null;
            rightClick(lastX, lastY);
            // Release any ongoing left-drag
            sendMouse('mouseup', lastX, lastY, 0, 0);
          }, LONG_MS);
          sendMouse('mousemove', p.x, p.y, 0, 0);
          sendMouse('mousedown', p.x, p.y, 0, 1);
        } else if (e.touches.length === 2) {
          // Cancel any in-progress 1-finger press
          sendMouse('mouseup', lastX, lastY, 0, 0);
          twoStart = true; twoMoved = false;
          var r = getFrame().getBoundingClientRect();
          prev2MidY = ((e.touches[0].clientY + e.touches[1].clientY) / 2) - r.top;
          var midX = ((e.touches[0].clientX + e.touches[1].clientX) / 2) - r.left;
          lastX = Math.max(0, Math.min(r.width  - 1, midX));
          lastY = Math.max(0, Math.min(r.height - 1, prev2MidY));
        }
      }, { passive: false });

      overlay.addEventListener('touchmove', function(e) {
        e.preventDefault();
        cancelLong();
        if (e.touches.length === 1 && !twoStart) {
          var p = toIframe(e.touches[0]);
          sendMouse('mousemove', p.x, p.y, 0, 1);
          lastX = p.x; lastY = p.y;
        } else if (e.touches.length === 2 && twoStart) {
          twoMoved = true;
          var r = getFrame().getBoundingClientRect();
          var midY = ((e.touches[0].clientY + e.touches[1].clientY) / 2) - r.top;
          var delta = prev2MidY - midY;   // positive = scroll down
          prev2MidY = midY;
          var canvas = getVncCanvas();
          if (canvas) {
            try {
              canvas.dispatchEvent(new WheelEvent('wheel', {
                bubbles: true, cancelable: true,
                view: getFrame().contentWindow,
                clientX: lastX, clientY: lastY,
                deltaY: delta * 4, deltaMode: 0,
              }));
            } catch(er) {}
          }
        }
      }, { passive: false });

      overlay.addEventListener('touchend', function(e) {
        e.preventDefault();
        cancelLong();
        if (e.touches.length === 0) {
          if (twoStart && !twoMoved) {
            // 2-finger tap = right-click
            rightClick(lastX, lastY);
          } else if (!twoStart) {
            sendMouse('mouseup', lastX, lastY, 0, 0);
            sendMouse('click',   lastX, lastY, 0, 0);
          }
          twoStart = false; twoMoved = false;
        } else if (e.touches.length === 1 && twoStart) {
          // Lifted one finger: resume 1-finger tracking
          twoStart = false; twoMoved = false;
          var p = toIframe(e.touches[0]);
          lastX = p.x; lastY = p.y;
          sendMouse('mousedown', p.x, p.y, 0, 1);
        }
      }, { passive: false });

      overlay.addEventListener('touchcancel', function(e) {
        cancelLong();
        sendMouse('mouseup', lastX, lastY, 0, 0);
        twoStart = false; twoMoved = false;
      }, { passive: false });

      // ── Mouse pass-through ──────────────────────────────────────────────────
      // The overlay sits on top of the iframe so it intercepts all mouse events.
      // Forward them to the noVNC canvas so regular mouse users are unaffected.
      // (Game mode has its own higher-z overlay that handles this path instead.)
      function mousePos(e) {
        var r = getFrame().getBoundingClientRect();
        return {
          x: Math.max(0, Math.min(r.width  - 1, e.clientX - r.left)),
          y: Math.max(0, Math.min(r.height - 1, e.clientY - r.top))
        };
      }

      overlay.addEventListener('mousedown', function(e) {
        if (gameMode) return;
        var p = mousePos(e);
        try { getFrame().contentWindow.focus(); } catch(ex) {}
        sendToCanvas('mousedown', p.x, p.y, e.button, e.buttons);
      });
      overlay.addEventListener('mouseup', function(e) {
        if (gameMode) return;
        var p = mousePos(e);
        sendToCanvas('mouseup', p.x, p.y, e.button, e.buttons);
      });
      // click → elementFromPoint so noVNC sidebar buttons / UI panels fire too
      overlay.addEventListener('click', function(e) {
        if (gameMode) return;
        var p = mousePos(e);
        sendClick(p.x, p.y);
      });
      overlay.addEventListener('mousemove', function(e) {
        if (gameMode) return;
        var p = mousePos(e);
        sendToCanvas('mousemove', p.x, p.y, e.button, e.buttons);
      });
      overlay.addEventListener('dblclick', function(e) {
        if (gameMode) return;
        var p = mousePos(e);
        sendToCanvas('dblclick', p.x, p.y, e.button, e.buttons);
      });
      overlay.addEventListener('contextmenu', function(e) {
        e.preventDefault();
        if (gameMode) return;
        var p = mousePos(e);
        rightClick(p.x, p.y);
      });
      overlay.addEventListener('wheel', function(e) {
        if (gameMode) return;
        var p = mousePos(e);
        var canvas = getVncCanvas();
        if (!canvas) return;
        try {
          canvas.dispatchEvent(new WheelEvent('wheel', {
            bubbles: true, cancelable: true,
            view: getFrame().contentWindow,
            clientX: p.x, clientY: p.y,
            deltaX: e.deltaX, deltaY: e.deltaY, deltaZ: e.deltaZ,
            deltaMode: e.deltaMode,
          }));
        } catch(er) {}
      }, { passive: true });
    })();
    // ── End Touch Control ────────────────────────────────────────────────────

    // ── Keyboard forwarding ───────────────────────────────────────────────────
    // The touch overlay sits over the iframe so mouse clicks land on the
    // overlay div, not the iframe — meaning the iframe never gets natural
    // keyboard focus.  Capture key events in the parent window and re-dispatch
    // them into the iframe's document so noVNC receives every keystroke.
    //
    // Exemptions (handled in the parent window, must NOT be forwarded):
    //   Ctrl+B        → toggle toolbar
    //   Ctrl+Shift+V  → paste clipboard to VM
    //
    // Skip forwarding when a real input element in the toolbar has focus
    // (only the sensitivity range slider exists today, but belt-and-suspenders).
    (function() {
      function fwdKey(e) {
        // Let parent-window shortcuts through without forwarding
        if (e.ctrlKey && !e.shiftKey && e.key === 'b') return;
        if (e.ctrlKey &&  e.shiftKey && e.key.toLowerCase() === 'v') return;

        // Don't steal keys from real toolbar inputs
        var a = document.activeElement;
        if (a && (a.tagName === 'INPUT' || a.tagName === 'TEXTAREA' || a.tagName === 'SELECT')) return;

        // noVNC attaches its keyboard handler to the canvas element (not document),
        // so we must dispatch directly to the canvas.
        var canvas = getVncCanvas();
        if (!canvas) return;

        // If the iframe already has focus, events reach the canvas naturally — skip
        if (a && a.id === 'vnc-frame') return;

        try {
          canvas.dispatchEvent(new KeyboardEvent(e.type, {
            bubbles: true, cancelable: true,
            key: e.key, code: e.code,
            keyCode: e.keyCode, charCode: e.charCode, which: e.which,
            shiftKey: e.shiftKey, ctrlKey: e.ctrlKey,
            altKey: e.altKey, metaKey: e.metaKey,
            repeat: e.repeat, location: e.location,
          }));
          e.preventDefault();
        } catch(ex) {}
      }

      document.addEventListener('keydown',  fwdKey, true);
      document.addEventListener('keyup',    fwdKey, true);
    })();
    // ── End Keyboard forwarding ───────────────────────────────────────────────

    // ── Toolbar hide / fullscreen ────────────────────────────────────────────
    var barHidden = false;

    function showBar() {
      barHidden = false;
      document.body.classList.remove('bar-hidden');
      document.getElementById('bar-toggle-btn').textContent = '\u25b2'; // ▲
      // Game overlay must sit below the 40px toolbar when bar is visible
      document.getElementById('game-overlay').style.top = '';
    }

    function hideBar() {
      barHidden = true;
      document.body.classList.add('bar-hidden');
      document.getElementById('bar-toggle-btn').textContent = '\u25bc'; // ▼
      // Game overlay should cover full viewport when bar is hidden
      document.getElementById('game-overlay').style.top = '0';
    }

    function toggleBar() {
      if (barHidden) { showBar(); } else { hideBar(); }
    }

    function toggleFullscreen() {
      if (!document.fullscreenElement) {
        // Fullscreen the WHOLE page so our toolbar stays visible.
        // (noVNC's own fullscreen button only fullscreens the iframe,
        //  which hides our toolbar — this is the alternative.)
        document.documentElement.requestFullscreen().catch(function(e) {
          console.warn('Fullscreen request failed:', e);
        });
      } else {
        document.exitFullscreen();
      }
    }

    // Update ⛶ icon to reflect actual fullscreen state
    document.addEventListener('fullscreenchange', function() {
      document.getElementById('fs-btn').textContent =
        document.fullscreenElement ? '\u29c9' : '\u26f6'; // ⧉ vs ⛶
    });

    // Ctrl+B = toggle bar from anywhere (keyboard-only UX)
    document.addEventListener('keydown', function(e) {
      if (e.ctrlKey && e.key === 'b') { e.preventDefault(); toggleBar(); }
    });
    // ── End toolbar hide / fullscreen ────────────────────────────────────────

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


def _handle_launch_mgba(client):
    """POST /launch-mgba: spawn the mGBA emulator on display :1.

    Uses the launcher script created by start.sh, which sets DISPLAY and
    PULSE_SERVER. Captures stderr for ~1s so a failure (missing lib, bad
    display, etc.) is surfaced to the user instead of vanishing into the void.
    """
    launcher = '/tmp/blobevm-launch-mgba.sh'
    try:
        if not os.path.exists(launcher):
            raise FileNotFoundError(
                f'{launcher} not found. The "Start application" workflow '
                'creates this on boot — try restarting it.'
            )
        proc = subprocess.Popen(
            [launcher],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
        )
        # Quick sanity check: if mGBA dies in the first ~1s it almost certainly
        # failed to start (missing display, bad lib, etc.); grab the stderr.
        try:
            _, err = proc.communicate(timeout=1.2)
            err_msg = (err or b'').decode('utf-8', errors='replace').strip()
            msg = err_msg.encode() if err_msg else b'mGBA exited immediately with no error output.'
            resp = (b'HTTP/1.1 500 Internal Server Error\r\nContent-Type: text/plain\r\n'
                    b'Content-Length: ' + str(len(msg)).encode() +
                    b'\r\nConnection: close\r\n\r\n') + msg
            client.sendall(resp)
            return
        except subprocess.TimeoutExpired:
            # Still running after 1.2s -> launched successfully.
            pass
        resp = (b'HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\n'
                b'Content-Length: 2\r\nConnection: close\r\n\r\nOK')
        client.sendall(resp)
    except Exception as e:
        try:
            msg = f'launch-mgba failed: {e}'.encode()
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
    elif method == 'POST' and path == '/launch-mgba':
        threading.Thread(target=_handle_launch_mgba, args=(client,), daemon=True).start()
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
