"""Stage 1 - find the plot: geocode a place name (Mapbox / Google / Nominatim), cached on disk.

Provider order: ``PLANNER_GEOCODER`` env (mapbox|google|nominatim|offline) or the first one with a key
(``MAPBOX_TOKEN``, ``GOOGLE_MAPS_KEY``), else Nominatim. The bundled gazetteer answers when offline.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from importlib import resources
from pathlib import Path

import httpx

from .models import GeocodeResult

UA = "ApartmentPlanner/1.0 (+https://github.com/imprayush892/Integrated-vertical-architecture-intelligence-IVAI-)"
_lock = threading.Lock()
_last_nominatim = 0.0


def _cache_dir() -> Path:
    d = Path(os.environ.get("PLANNER_HOME", Path.home() / ".apartment-planner")) / "geocode-cache"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _cache_get(key: str):
    p = _cache_dir() / (hashlib.sha1(key.encode()).hexdigest() + ".json")
    if p.exists() and time.time() - p.stat().st_mtime < 90 * 86400:
        return [GeocodeResult(**r) for r in json.loads(p.read_text())]
    return None


def _cache_put(key: str, res: list[GeocodeResult]) -> None:
    p = _cache_dir() / (hashlib.sha1(key.encode()).hexdigest() + ".json")
    p.write_text(json.dumps([r.model_dump() for r in res]))


def offline(query: str, limit: int = 5) -> list[GeocodeResult]:
    data = json.loads(resources.files("planner_engine").joinpath("data/gazetteer.json").read_text())
    q = query.strip().lower()
    scored = []
    for p in data["places"]:
        name = p["name"].lower()
        full = p["display_name"].lower()
        if q in full:
            score = 0 if name.startswith(q) else 1 if q in name else 2
            scored.append((score, p))
    scored.sort(key=lambda t: (t[0], t[1]["name"]))
    return [GeocodeResult(name=p["name"], display_name=p["display_name"], lat=p["lat"], lng=p["lng"], provider="offline")
            for _, p in scored[:limit]]


def _nominatim(query: str, limit: int) -> list[GeocodeResult]:
    global _last_nominatim
    with _lock:  # usage policy: max 1 request per second
        wait = 1.0 - (time.time() - _last_nominatim)
        if wait > 0:
            time.sleep(wait)
        _last_nominatim = time.time()
    r = httpx.get("https://nominatim.openstreetmap.org/search",
                  params={"q": query, "format": "jsonv2", "limit": limit, "addressdetails": 0},
                  headers={"User-Agent": UA}, timeout=8.0)
    r.raise_for_status()
    out = []
    for it in r.json():
        bb = it.get("boundingbox")
        out.append(GeocodeResult(name=it.get("name") or it["display_name"].split(",")[0], display_name=it["display_name"],
                                 lat=float(it["lat"]), lng=float(it["lon"]), provider="nominatim",
                                 bbox=(float(bb[2]), float(bb[0]), float(bb[3]), float(bb[1])) if bb else None))
    return out


def _mapbox(query: str, limit: int, token: str) -> list[GeocodeResult]:
    r = httpx.get("https://api.mapbox.com/search/geocode/v6/forward",
                  params={"q": query, "limit": limit, "access_token": token}, timeout=8.0)
    r.raise_for_status()
    out = []
    for f in r.json().get("features", []):
        lng, lat = f["geometry"]["coordinates"]
        pr = f.get("properties", {})
        out.append(GeocodeResult(name=pr.get("name", query), display_name=pr.get("full_address") or pr.get("name", query),
                                 lat=lat, lng=lng, provider="mapbox"))
    return out


def _google(query: str, limit: int, key: str) -> list[GeocodeResult]:
    r = httpx.get("https://maps.googleapis.com/maps/api/geocode/json", params={"address": query, "key": key}, timeout=8.0)
    r.raise_for_status()
    out = []
    for it in r.json().get("results", [])[:limit]:
        loc = it["geometry"]["location"]
        out.append(GeocodeResult(name=it["address_components"][0]["long_name"], display_name=it["formatted_address"],
                                 lat=loc["lat"], lng=loc["lng"], provider="google"))
    return out


def geocode(query: str, limit: int = 5) -> list[GeocodeResult]:
    query = query.strip()
    if not query:
        return []
    provider = os.environ.get("PLANNER_GEOCODER", "").lower()
    mapbox = os.environ.get("MAPBOX_TOKEN")
    google = os.environ.get("GOOGLE_MAPS_KEY")
    if not provider:
        provider = "mapbox" if mapbox else "google" if google else "nominatim"
    key = f"{provider}:{limit}:{query.lower()}"
    cached = _cache_get(key)
    if cached is not None:
        return cached
    res: list[GeocodeResult] = []
    if provider != "offline":
        try:
            if provider == "mapbox" and mapbox:
                res = _mapbox(query, limit, mapbox)
            elif provider == "google" and google:
                res = _google(query, limit, google)
            else:
                res = _nominatim(query, limit)
        except (httpx.HTTPError, ValueError, KeyError):
            res = []
    off = offline(query, limit)
    if not res:
        return off
    # bundled surveyed points win for known golden places (exact name match)
    for o in off:
        if o.name.lower() == query.lower():
            res.insert(0, o)
    _cache_put(key, res[:limit])
    return res[:limit]
