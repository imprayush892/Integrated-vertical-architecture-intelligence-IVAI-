"""User settings in ~/.apartment-planner/config.json (map provider token, crash-report opt-in).

The Mapbox token is a *public* (pk.) token from a free Mapbox account: it is used for satellite tiles in
the UI and for geocoding, both inside Mapbox's free tier. It never leaves this computer except in the
requests to Mapbox itself.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

KEYS = {"mapbox_token": str, "crash_reports": bool, "map_provider": str}


def _path() -> Path:
    d = Path(os.environ.get("PLANNER_HOME", Path.home() / ".apartment-planner"))
    d.mkdir(parents=True, exist_ok=True)
    return d / "config.json"


def load() -> dict[str, Any]:
    try:
        return json.loads(_path().read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def save(**changes: Any) -> dict[str, Any]:
    data = load()
    for k, v in changes.items():
        if k not in KEYS:
            raise ValueError(f"unknown setting {k!r}")
        if v is None or v == "":
            data.pop(k, None)
        else:
            if not isinstance(v, KEYS[k]):
                raise ValueError(f"{k} must be {KEYS[k].__name__}")
            if k == "mapbox_token" and not v.startswith("pk."):
                raise ValueError("Use a public Mapbox token (it starts with 'pk.')")
            if k == "map_provider" and v not in ("auto", "mapbox", "esri"):
                raise ValueError("map_provider must be auto, mapbox or esri")
            data[k] = v
    p = _path()
    p.write_text(json.dumps(data, indent=1))
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass
    return public()


def mapbox_token() -> str | None:
    return os.environ.get("MAPBOX_TOKEN") or load().get("mapbox_token")


def public() -> dict[str, Any]:
    d = load()
    tok = mapbox_token()
    provider = d.get("map_provider", "auto")
    use_mapbox = bool(tok) and provider in ("auto", "mapbox")
    return {
        "mapbox_token": tok or "",
        "map_provider": provider,
        "imagery": "mapbox" if use_mapbox else "esri",
        "geocoder": "mapbox" if tok else "nominatim",
        "crash_reports": bool(d.get("crash_reports", False)),
    }
