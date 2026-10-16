import math

import pytest

from planner_engine import geo
from planner_engine.climate import noon_altitude_simple, parse_epw_wind, solar_position, sun_paths
from planner_engine.site import build_site


def test_utm_zone_mumbai():
    assert geo.utm_epsg(19.14, 72.83) == 32643


def test_shoelace_and_validation():
    sq = [(0, 0), (10, 0), (10, 10), (0, 10)]
    assert geo.shoelace(sq) == 100
    with pytest.raises(geo.GeometryError):
        geo.validate_ring([(0, 0), (10, 10), (10, 0), (0, 10)])  # bow-tie
    with pytest.raises(geo.GeometryError):
        geo.validate_ring([(0, 0), (1, 1)])


def test_site_lokhandwala_matches_video(lokhandwala):
    s = build_site([tuple(p) for p in lokhandwala["lnglat"]])
    assert abs(s.area - 3397.8) / 3397.8 < 0.005
    assert abs(s.area - s.geodesic_area) / s.geodesic_area < 0.001
    assert abs(s.perimeter - 233.1) < 0.5
    assert s.setback == 3.0
    assert abs(s.buildable_area - 2733.5) / 2733.5 < 0.005
    noon = {p.label: p.noon_altitude for p in s.climate.sun}
    assert noon["Jun 21"] == pytest.approx(85.7, abs=0.15)
    assert noon["Dec 21"] == pytest.approx(47.4, abs=0.15)
    assert s.climate.wind.prevailing in ("WNW", "W", "NW")
    assert s.climate.wind.indicative
    assert any(h in s.climate.hot_facades for h in ("W", "WSW", "WNW"))


def test_noon_formula():
    assert noon_altitude_simple(19.143, 23.44) == pytest.approx(85.703, abs=1e-3)


def test_solar_position_against_pvlib():
    pvlib = pytest.importorskip("pvlib")
    import datetime as dt

    import pandas as pd

    when = dt.datetime(2026, 6, 21, 7, 0)
    az, alt, _ = solar_position(19.143, 72.8275, when)
    ref = pvlib.solarposition.get_solarposition(pd.DatetimeIndex([pd.Timestamp(when, tz="UTC")]), 19.143, 72.8275)
    assert az == pytest.approx(float(ref["azimuth"].iloc[0]), abs=0.3)
    assert alt == pytest.approx(float(ref["apparent_elevation"].iloc[0]), abs=0.3)


def test_sun_paths_have_daylight():
    p = sun_paths(19.143, 72.8275)
    assert len(p) == 3 and all(len(x.samples) > 10 for x in p)


def test_epw_parser(tmp_path):
    lines = ["LOCATION,Mumbai Santacruz,MH,IND,ISHRAE,430030,19.12,72.85,5.5,14.0"] + ["X"] * 7
    for h in range(100):
        row = ["2026", "1", "1", str(h % 24 + 1), "0"] + ["0"] * 15 + [str(292.5 if h < 70 else 180), "4.0"] + ["0"] * 13
        lines.append(",".join(row))
    p = tmp_path / "m.epw"
    p.write_text("\n".join(lines))
    rose = parse_epw_wind(p)
    assert rose.prevailing == "WNW" and not rose.indicative
    assert sum(b.frequency for b in rose.bins) == pytest.approx(1.0, abs=1e-6)


def test_setback_front_and_side():
    # 40 x 30 m rectangle at Mumbai; road on edge 0 with a wider front setback
    lat, lng = 19.1, 72.85
    dx = 40 / (111_320 * math.cos(math.radians(lat)))
    dy = 30 / 110_574
    pts = [(lng, lat), (lng + dx, lat), (lng + dx, lat + dy), (lng, lat + dy)]
    s1 = build_site(pts, road_width=12, building_height=24, with_climate=False)
    s2 = build_site(pts, road_width=24, building_height=24, with_climate=False)
    assert s2.buildable_area < s1.buildable_area  # 6 m front instead of 3 m
