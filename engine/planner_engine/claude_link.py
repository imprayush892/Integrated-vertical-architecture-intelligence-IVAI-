"""Find Claude on this computer and plug the planner's MCP bridge into it - in 5 steps.

1. Look: is Claude Desktop installed? Where is its config (macOS, Windows, Windows Store/MSIX, Linux)?
2. Ask: show the exact change.
3. Back up, then add: copy the config, add one entry, never remove anything.
4. Restart Claude (the user quits Claude fully and reopens it), or install the one-click .mcpb.
5. Handshake: Claude starts our bridge; the bridge calls ``bridge_hello``; the badge turns green.

We never read or reuse Claude login tokens. MCP is the only door.
"""

from __future__ import annotations

import difflib
import json
import os
import platform
import shutil
import sys
import time
from pathlib import Path
from typing import Any

SERVER_NAME = "apartment-planner"
MSIX_FAMILY = "Claude_pzs8sxrjxfjjc"


def config_candidates() -> list[Path]:
    system = platform.system()
    home = Path.home()
    out: list[Path] = []
    if system == "Darwin":
        out.append(home / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json")
    elif system == "Windows":
        appdata = Path(os.environ.get("APPDATA", home / "AppData" / "Roaming"))
        local = Path(os.environ.get("LOCALAPPDATA", home / "AppData" / "Local"))
        out.append(local / "Packages" / MSIX_FAMILY / "LocalCache" / "Roaming" / "Claude" / "claude_desktop_config.json")
        out.append(appdata / "Claude" / "claude_desktop_config.json")
    else:
        xdg = Path(os.environ.get("XDG_CONFIG_HOME", home / ".config"))
        out.append(xdg / "Claude" / "claude_desktop_config.json")
    override = os.environ.get("CLAUDE_DESKTOP_CONFIG")
    if override:
        out.insert(0, Path(override))
    return out


def app_installed() -> dict[str, Any]:
    system = platform.system()
    home = Path.home()
    hits: list[str] = []
    if system == "Darwin":
        for p in (Path("/Applications/Claude.app"), home / "Applications" / "Claude.app"):
            if p.exists():
                hits.append(str(p))
    elif system == "Windows":
        local = Path(os.environ.get("LOCALAPPDATA", home / "AppData" / "Local"))
        for p in (local / "AnthropicClaude", local / "Programs" / "Claude", local / "Packages" / MSIX_FAMILY):
            if p.exists():
                hits.append(str(p))
    else:
        for name in ("claude-desktop", "claude"):
            w = shutil.which(name)
            if w:
                hits.append(w)
    cfg_dir = [str(p.parent) for p in config_candidates() if p.parent.exists()]
    return {"installed": bool(hits or cfg_dir), "paths": hits, "config_dirs": cfg_dir, "os": system}


def config_path() -> Path:
    """The config file Claude actually reads: the first candidate whose folder exists."""
    for p in config_candidates():
        if p.exists() or p.parent.exists():
            return p
    return config_candidates()[-1]


def bridge_command() -> dict[str, Any]:
    """How Claude should start our bridge."""
    if getattr(sys, "frozen", False):  # PyInstaller build: planner-mcp sits next to the engine binary
        exe = Path(sys.executable).with_name("planner-mcp" + (".exe" if os.name == "nt" else ""))
        return {"command": str(exe), "args": ["--stdio"]}
    env_cmd = os.environ.get("PLANNER_MCP_COMMAND")
    if env_cmd:
        return {"command": env_cmd, "args": ["--stdio"]}
    return {"command": sys.executable, "args": ["-m", "planner_engine.mcp_server", "--stdio"]}


def _load(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        return {}
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError(f"{path} is not a JSON object")
    return data


def plan_link(path: Path | None = None) -> dict[str, Any]:
    """Step 2: the exact change, as a unified diff. Nothing is written."""
    path = path or config_path()
    try:
        before = _load(path)
    except (ValueError, json.JSONDecodeError) as e:
        return {"path": str(path), "error": f"Claude's config is not valid JSON ({e}). Fix it or move it aside first."}
    after = json.loads(json.dumps(before))
    servers = after.setdefault("mcpServers", {})
    entry = bridge_command()
    already = servers.get(SERVER_NAME) == entry
    servers[SERVER_NAME] = entry
    a = json.dumps(before, indent=2, sort_keys=True).splitlines()
    b = json.dumps(after, indent=2, sort_keys=True).splitlines()
    diff = "\n".join(difflib.unified_diff(a, b, "claude_desktop_config.json (now)", "claude_desktop_config.json (after)",
                                          lineterm=""))
    replacing = SERVER_NAME in before.get("mcpServers", {}) and not already
    return {"path": str(path), "exists": path.exists(), "already_linked": already, "replacing_our_entry": replacing,
            "entry": {SERVER_NAME: entry}, "diff": diff, "after": after}


def link(path: Path | None = None, confirm: bool = False) -> dict[str, Any]:
    """Steps 3-4. ``confirm`` must be True (the UI asks first)."""
    plan = plan_link(path)
    if "error" in plan:
        return plan
    if plan["already_linked"]:
        return {**plan, "written": False, "message": "Already linked. Restart Claude if the tools are missing."}
    if not confirm:
        return {**plan, "written": False, "message": "Not written: confirm=True required."}
    p = Path(plan["path"])
    p.parent.mkdir(parents=True, exist_ok=True)
    backup = None
    if p.exists():
        backup = p.with_name(p.name + f".bak-{time.strftime('%Y%m%d-%H%M%S')}")
        shutil.copy2(p, backup)
    # only add: merge into the freshest copy on disk in case Claude rewrote it meanwhile
    current = _load(p)
    current.setdefault("mcpServers", {})[SERVER_NAME] = plan["entry"][SERVER_NAME]
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(current, indent=2), encoding="utf-8")
    os.replace(tmp, p)
    return {**plan, "written": True, "backup": str(backup) if backup else None,
            "message": "Linked. Quit Claude fully (from the tray / menu bar) and open it again."}


def unlink(path: Path | None = None) -> dict[str, Any]:
    p = path or config_path()
    data = _load(p)
    if SERVER_NAME not in data.get("mcpServers", {}):
        return {"path": str(p), "removed": False}
    backup = p.with_name(p.name + f".bak-{time.strftime('%Y%m%d-%H%M%S')}")
    shutil.copy2(p, backup)
    del data["mcpServers"][SERVER_NAME]
    p.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return {"path": str(p), "removed": True, "backup": str(backup)}


def claude_code_command() -> str:
    e = bridge_command()
    args = " ".join(json.dumps(a) if " " in a else a for a in e["args"])
    cmd = json.dumps(e["command"]) if " " in e["command"] else e["command"]
    return f"claude mcp add {SERVER_NAME} -- {cmd} {args}"


def status() -> dict[str, Any]:
    p = config_path()
    try:
        data = _load(p)
        linked = SERVER_NAME in data.get("mcpServers", {})
        err = None
    except (ValueError, json.JSONDecodeError) as e:
        linked, err = False, str(e)
    return {"app": app_installed(), "config": str(p), "config_exists": p.exists(), "linked_in_config": linked,
            "error": err, "entry": bridge_command(), "claude_code": claude_code_command()}
