"""Start (or reopen) Apartment Planner: reuse a running engine, else start one, then open the browser."""

import json
import os
import subprocess
import sys
import time
import urllib.request
import webbrowser
from pathlib import Path

home = Path(os.environ.get("PLANNER_HOME", Path.home() / ".apartment-planner"))
lock = home / "server.json"


def running_url():
    try:
        info = json.loads(lock.read_text())
        with urllib.request.urlopen(info["url"] + "/api/health", timeout=1.5) as r:
            if r.status == 200:
                return info["url"]
    except Exception:
        return None
    return None


url = running_url()
if not url:
    flags = 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW
    log = open(home.mkdir(parents=True, exist_ok=True) or home / "engine.log", "ab")
    subprocess.Popen([sys.executable, "-m", "planner_engine.cli", "serve", "--no-browser"],
                     stdout=log, stderr=log, creationflags=flags, close_fds=True)
    for _ in range(120):
        time.sleep(0.5)
        url = running_url()
        if url:
            break
if url:
    webbrowser.open(url)
else:
    import ctypes

    msg = f"The planner engine did not start. See {home / 'engine.log'}"
    if os.name == "nt":
        ctypes.windll.user32.MessageBoxW(0, msg, "Apartment Planner", 0x10)
    else:
        print(msg)
