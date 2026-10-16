import json
from importlib import resources

import pytest

from planner_engine import settings
from planner_engine.engine import Planner, PlannerError
from planner_engine.placement import fit_triangle
from planner_engine.plate import fit
from planner_engine.site import build_site, buildable_geom
from planner_engine.units import split_units


def test_settings_roundtrip_and_validation():
    assert settings.public()["imagery"] == "esri"
    with pytest.raises(ValueError):
        settings.save(mapbox_token="sk.secret")  # secret tokens must never be stored
    s = settings.save(mapbox_token="pk.test123", map_provider="auto")
    assert s["imagery"] == "mapbox" and s["geocoder"] == "mapbox"
    assert settings.save(map_provider="esri")["imagery"] == "esri"
    assert settings.save(mapbox_token="")["mapbox_token"] == ""


def test_engine_settings_rpc(tmp_path):
    P = Planner(tmp_path / "p.aptplan")
    with pytest.raises(PlannerError):
        P.set_settings(map_provider="google")
    assert P.set_settings(mapbox_token="pk.abc")["imagery"] == "mapbox"


def test_triangle_fits_buildable_and_every_flat_reaches_the_lobby():
    g = json.loads(resources.files("planner_engine").joinpath("data/golden/lokhandwala.json").read_text())
    s = build_site([tuple(p) for p in g["lnglat"]], with_climate=False)
    fp = fit_triangle(buildable_geom(s))
    assert fp is not None and fp.width > 40
    p = fit("triangle", fp)
    assert sum(z.area for z in p.zones) == pytest.approx(p.area, abs=0.005)
    assert sum(1 for c in p.core_parts if c.kind == "stair") == 2
    us = split_units(p)
    assert len(us) >= 6
    assert all(u.core_link is not None for u in us)
