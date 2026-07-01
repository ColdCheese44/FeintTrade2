"""
FeintTrade — Desktop App
Wraps the dashboard and Discord into a native windowed app with system tray.
Close the window (X) or choose Quit from the tray to kill everything.
"""

import os
import sys
import subprocess
import threading
import time
from pathlib import Path
from dotenv import load_dotenv
from scripts.browser import open_urls_in_feint_browser

ROOT = Path(__file__).parent
load_dotenv(ROOT / ".env", override=True)

SERVER_ID   = os.getenv("DISCORD_SERVER_ID", "")
# Open the operator's primary channel — ft-command-center (where the bot listens and the
# per-routine !status pulse posts). Fall back to legacy variable names for older configs.
CHANNEL_ID  = (os.getenv("DISCORD_CH_COMMAND_CENTER")
               or os.getenv("DISCORD_CH_COMMAND_POST")
               or os.getenv("DISCORD_MINDHUB_CHANNEL_ID", ""))
DISCORD_URL = f"https://discord.com/channels/{SERVER_ID}/{CHANNEL_ID}"
ALPACA_URL  = "https://app.alpaca.markets/paper/dashboard/overview"

PROCS = []


# ---------------------------------------------------------------------------
# Service management
# ---------------------------------------------------------------------------

def start_services():
    sl = subprocess.Popen(
        [sys.executable, "-m", "streamlit", "run", "dashboard.py",
         "--server.headless=true", "--server.port=8501"],
        cwd=ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    PROCS.append(sl)
    # Discord bot is managed by Task Scheduler — do not start it here.

    import requests
    print("Waiting for Streamlit...")
    for _ in range(40):
        try:
            if requests.get("http://localhost:8501/_stcore/health", timeout=1).ok:
                print("Streamlit ready.")
                return
        except Exception:
            pass
        time.sleep(1)
    print("Streamlit slow to start — opening anyway.")


def kill_all():
    for p in PROCS:
        try:
            p.terminate()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# System tray icon
# ---------------------------------------------------------------------------

def make_icon():
    from PIL import Image, ImageDraw
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse([2, 2, 62, 62], fill=(16, 163, 127))
    # Chart bars
    d.rectangle([12, 40, 21, 54], fill="white")
    d.rectangle([26, 30, 35, 54], fill="white")
    d.rectangle([40, 18, 49, 54], fill="white")
    return img


_tray_icon = None


def start_tray(main_window):
    global _tray_icon
    import pystray

    def on_show(icon, item):
        main_window.show()

    def on_quit(icon, item):
        icon.stop()
        kill_all()
        try:
            main_window.destroy()
        except Exception:
            pass

    _tray_icon = pystray.Icon(
        "FeintTrade",
        make_icon(),
        "FeintTrade",
        menu=pystray.Menu(
            pystray.MenuItem("Show Dashboard", on_show, default=True),
            pystray.MenuItem("Quit", on_quit),
        ),
    )
    _tray_icon.run()


# ---------------------------------------------------------------------------
# JavaScript API exposed to the webview
# ---------------------------------------------------------------------------

class AppAPI:
    def __init__(self):
        self._discord_window = None

    def open_discord(self):
        """Open Discord and Alpaca as tabs in Feint's preferred Brave window."""
        open_urls_in_feint_browser([DISCORD_URL, ALPACA_URL])

    def minimize_to_tray(self):
        pass  # handled by OS minimize button


# ---------------------------------------------------------------------------
# HTML shell
# ---------------------------------------------------------------------------

SHELL = """<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
* { margin:0; padding:0; box-sizing:border-box; }
html, body { height:100%; overflow:hidden; background:#0d1117; }

.titlebar {
  display:flex;
  align-items:center;
  background:#161b22;
  border-bottom:1px solid #21262d;
  height:44px;
  padding:0 16px;
  gap:12px;
  flex-shrink:0;
  -webkit-app-region: drag;
  user-select: none;
}

.logo { color:#10a37f; font-weight:700; font-size:14px; letter-spacing:0.06em; }

.tabs {
  display:flex;
  gap:4px;
  -webkit-app-region: no-drag;
}

.tab {
  padding:6px 18px;
  border-radius:6px;
  border:none;
  cursor:pointer;
  font-size:13px;
  font-family:inherit;
  font-weight:500;
  transition:all 0.15s;
  background:transparent;
  color:#8b949e;
  -webkit-app-region: no-drag;
}
.tab:hover { background:#21262d; color:#e6edf3; }
.tab.active { background:#10a37f22; color:#10a37f; }

.status-dot {
  width:8px; height:8px; border-radius:50%;
  background:#10a37f;
  margin-left:auto;
  box-shadow:0 0 6px #10a37f;
  flex-shrink:0;
}

.frame-wrap {
  position:absolute;
  top:44px; left:0; right:0; bottom:0;
}

iframe {
  width:100%; height:100%; border:none;
}
</style>
</head>
<body>
<div class="titlebar">
  <span class="logo">📈 FEINTTRADE</span>
  <div class="tabs">
    <button class="tab active" id="tab-dash" onclick="activateTab('dash')">Dashboard</button>
    <button class="tab" id="tab-discord" onclick="openDiscord()">Command Center/Alpaca</button>
  </div>
  <div class="status-dot" title="Agent running"></div>
</div>
<div class="frame-wrap">
  <iframe id="dash" src="http://localhost:8501" allow="*"></iframe>
</div>

<script>
function activateTab(name) {
  document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
  document.getElementById('tab-' + name).classList.add('active');
}

function openDiscord() {
  activateTab('discord');
  if (window.pywebview) {
    window.pywebview.api.open_discord();
  }
}
</script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import webview

    print("Starting FeintTrade...")
    start_services()

    api = AppAPI()

    main_win = webview.create_window(
        "FeintTrade",
        html=SHELL,
        js_api=api,
        width=1440,
        height=900,
        min_size=(1000, 600),
    )

    def on_closed():
        if _tray_icon:
            try:
                _tray_icon.stop()
            except Exception:
                pass
        kill_all()

    main_win.events.closed += on_closed

    tray_thread = threading.Thread(target=start_tray, args=(main_win,), daemon=True)
    tray_thread.start()

    def on_start():
        try:
            main_win.maximize()
        except Exception:
            pass
        # Auto-launch Discord + Alpaca as two tabs in one Brave window on startup.
        try:
            api.open_discord()
        except Exception:
            pass

    webview.start(on_start, debug=False)
    kill_all()
    sys.exit(0)
