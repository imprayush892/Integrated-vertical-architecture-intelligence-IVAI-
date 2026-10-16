"""Stage 3 - sun and wind.

Sun: NOAA solar position algorithm (same equations as the NOAA solar calculator / pvlib's
``spa``-lite). Wind: EnergyPlus weather (EPW) files binned into 16 directions, with a bundled
*indicative* rose for when no EPW is available (shown as "indicative", as in the reference video).
"""

from __future__ import annotations

import datetime as dt
import json
import math
from importlib import resources
from pathlib import Path

from .geo import COMPASS16, compass
from .models import Climate, SunPath, SunSample, WindBin, WindRose

KEY_DATES = [("Jun 21", 6, 21), ("Mar/Sep 21", 3, 21), ("Dec 21", 12, 21)]


def default_tz(lat: float, lng: float) -> float:
    if 6.0 <= lat <= 37.5 and 68.0 <= lng <= 97.5:
        return 5.5  # IST
    return round(lng / 7.5) / 2.0


def _julian_day(d: dt.datetime) -> float:
    y, m = d.year, d.month
    day = d.day + (d.hour + d.minute / 60 + d.second / 3600) / 24
    if m <= 2:
        y -= 1
        m += 12
    a = y // 100
    b = 2 - a + a // 4
    return int(365.25 * (y + 4716)) + int(30.6001 * (m + 1)) + day + b - 1524.5


def solar_position(lat: float, lng: float, when_utc: dt.datetime) -> tuple[float, float, float]:
    """Return (azimuth deg from north clockwise, apparent altitude deg, declination deg)."""
    jd = _julian_day(when_utc)
    t = (jd - 2451545.0) / 36525.0
    l0 = (280.46646 + t * (36000.76983 + t * 0.0003032)) % 360
    m = 357.52911 + t * (35999.05029 - 0.0001537 * t)
    e = 0.016708634 - t * (0.000042037 + 0.0000001267 * t)
    mr = math.radians(m)
    c = (
        math.sin(mr) * (1.914602 - t * (0.004817 + 0.000014 * t))
        + math.sin(2 * mr) * (0.019993 - 0.000101 * t)
        + math.sin(3 * mr) * 0.000289
    )
    true_long = l0 + c
    omega = 125.04 - 1934.136 * t
    app_long = true_long - 0.00569 - 0.00478 * math.sin(math.radians(omega))
    seconds = 21.448 - t * (46.815 + t * (0.00059 - t * 0.001813))
    eps0 = 23 + (26 + seconds / 60) / 60
    eps = eps0 + 0.00256 * math.cos(math.radians(omega))
    decl = math.degrees(math.asin(math.sin(math.radians(eps)) * math.sin(math.radians(app_long))))
    y = math.tan(math.radians(eps / 2)) ** 2
    l0r = math.radians(l0)
    eq_time = 4 * math.degrees(
        y * math.sin(2 * l0r)
        - 2 * e * math.sin(mr)
        + 4 * e * y * math.sin(mr) * math.cos(2 * l0r)
        - 0.5 * y * y * math.sin(4 * l0r)
        - 1.25 * e * e * math.sin(2 * mr)
    )
    minutes = when_utc.hour * 60 + when_utc.minute + when_utc.second / 60
    true_solar = (minutes + eq_time + 4 * lng) % 1440
    ha = true_solar / 4 - 180 if true_solar / 4 >= 0 else true_solar / 4 + 180
    latr, decr, har = math.radians(lat), math.radians(decl), math.radians(ha)
    cos_zen = math.sin(latr) * math.sin(decr) + math.cos(latr) * math.cos(decr) * math.cos(har)
    zen = math.degrees(math.acos(max(-1.0, min(1.0, cos_zen))))
    alt = 90 - zen
    # atmospheric refraction (NOAA approximation)
    if alt > 85:
        refr = 0.0
    else:
        te = math.tan(math.radians(alt)) if abs(alt) > 1e-6 else 1e-6
        if alt > 5:
            refr = 58.1 / te - 0.07 / te**3 + 0.000086 / te**5
        elif alt > -0.575:
            refr = 1735 + alt * (-518.2 + alt * (103.4 + alt * (-12.79 + alt * 0.711)))
        else:
            refr = -20.772 / te
        refr /= 3600
    alt += refr
    zr = math.radians(zen)
    if abs(math.sin(zr)) < 1e-9:
        az = 180.0
    else:
        cos_az = (math.sin(latr) * math.cos(zr) - math.sin(decr)) / (math.cos(latr) * math.sin(zr))
        az0 = math.degrees(math.acos(max(-1.0, min(1.0, cos_az))))
        az = (az0 + 180) % 360 if ha > 0 else (540 - az0) % 360
    return az, alt, decl


def noon_altitude_simple(lat: float, decl: float) -> float:
    """Textbook noon sun height: 90 - |lat - decl|."""
    return 90.0 - abs(lat - decl)


def sun_paths(lat: float, lng: float, year: int = 2026, tz: float | None = None, step_min: int = 30) -> list[SunPath]:
    tz = default_tz(lat, lng) if tz is None else tz
    paths = []
    for label, month, day in KEY_DATES:
        samples: list[SunSample] = []
        decl = 0.0
        for k in range(0, 24 * 60, step_min):
            local = dt.datetime(year, month, day) + dt.timedelta(minutes=k)
            utc = local - dt.timedelta(hours=tz)
            az, alt, decl = solar_position(lat, lng, utc)
            if alt > 0:
                samples.append(SunSample(time=local.strftime("%H:%M"), azimuth=round(az, 2), altitude=round(alt, 2)))
        # noon altitude: textbook formula with the day's declination
        noon = noon_altitude_simple(lat, decl)
        paths.append(
            SunPath(label=label, date=f"{year}-{month:02d}-{day:02d}", noon_altitude=round(noon, 1), samples=samples)
        )
    return paths


def hot_facades(paths: list[SunPath], min_alt: float = 5.0, max_alt: float = 50.0) -> list[str]:
    """Compass sectors that receive low, hot afternoon sun (after 13:00) on summer/equinox dates."""
    hits: dict[str, int] = {}
    for p in paths:
        if p.label.startswith("Dec"):
            continue
        for s in p.samples:
            if s.time >= "13:00" and min_alt <= s.altitude <= max_alt:
                hits[compass(s.azimuth)] = hits.get(compass(s.azimuth), 0) + 1
    order = sorted(hits, key=lambda k: -hits[k])
    return [k for k in order if hits[k] >= 2]


# ------------------------------------------------------------------------------------------------ wind


def _bundled_roses() -> dict:
    data = resources.files("planner_engine").joinpath("data/climate/wind_roses.json").read_text()
    return json.loads(data)


def parse_epw_wind(path: str | Path) -> WindRose:
    """Bin an EPW file's hourly wind (field 21 direction, 22 speed) into 16 sectors."""
    counts = [0] * 16
    speeds = [0.0] * 16
    calm = 0
    total = 0
    location = ""
    with open(path, encoding="latin-1") as fh:
        for i, line in enumerate(fh):
            parts = line.rstrip("\n").split(",")
            if i == 0 and parts[0] == "LOCATION":
                location = parts[1]
                continue
            if i < 8:
                continue
            if len(parts) < 22:
                continue
            try:
                d = float(parts[20])
                s = float(parts[21])
            except ValueError:
                continue
            if d >= 999 or s >= 999:
                continue
            total += 1
            if s < 0.5:
                calm += 1
                continue
            k = int(((d % 360) + 11.25) // 22.5) % 16
            counts[k] += 1
            speeds[k] += s
    if total == 0:
        raise ValueError("EPW file has no wind records")
    bins = [
        WindBin(
            direction=k * 22.5,
            label=COMPASS16[k],
            frequency=round(counts[k] / total, 4),
            mean_speed=round(speeds[k] / counts[k], 2) if counts[k] else 0.0,
        )
        for k in range(16)
    ]
    prevailing = max(bins, key=lambda b: b.frequency).label
    return WindRose(
        source=f"EPW: {location or Path(path).name}",
        indicative=False,
        bins=bins,
        prevailing=prevailing,
        notes=[f"{total} hourly records, calm {calm / total:.0%}"],
    )


def wind_rose(lat: float, lng: float, epw: str | Path | None = None) -> WindRose:
    if epw:
        return parse_epw_wind(epw)
    roses = _bundled_roses()
    best, best_d = None, 1e9
    for r in roses["stations"]:
        d = math.hypot(r["lat"] - lat, (r["lng"] - lng) * math.cos(math.radians(lat)))
        if d < best_d:
            best, best_d = r, d
    assert best is not None
    km = best_d * 111.0
    bins = [
        WindBin(direction=k * 22.5, label=COMPASS16[k], frequency=f, mean_speed=s)
        for k, (f, s) in enumerate(zip(best["frequency"], best["speed"], strict=True))
    ]
    notes = list(best.get("notes", []))
    if km > 150:
        notes.append(f"Nearest bundled station is {km:.0f} km away - load a local EPW file for accuracy.")
    return WindRose(
        source=f"{best['name']} (bundled climatology, {km:.0f} km)",
        indicative=True,
        bins=bins,
        prevailing=max(bins, key=lambda b: b.frequency).label,
        notes=notes,
    )


def analyse(lat: float, lng: float, epw: str | Path | None = None, year: int = 2026) -> Climate:
    paths = sun_paths(lat, lng, year=year)
    return Climate(sun=paths, wind=wind_rose(lat, lng, epw), hot_facades=hot_facades(paths))
