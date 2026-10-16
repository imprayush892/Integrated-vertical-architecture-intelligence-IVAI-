import json
from importlib import resources

import pytest
from shapely.geometry import Polygon

from planner_engine import geo, settings
from planner_engine.engine import Planner, PlannerError
from planner_engine.models import PlateFootprint
from planner_engine.placement import fit_triangle
from planner_engine.plate import fit, zones_geom
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


def _fixes(applied: dict | None) -> list[str]:
    if not applied:
        return []
    steps = applied["steps"] if applied.get("action") == "multi" else [applied]
    return [s["action"] for s in steps]


def test_triangle_flats_are_2bhk_and_solve_without_ducts(tmp_path, lokhandwala):
    """Golden plot, triangle plate end to end: bands stay deep (the core moves onto the base façade), every
    flat is at least a 2BHK and solves without the duct-all fallback; balconies only on the outer façade."""
    P = Planner(tmp_path / "p.aptplan", "Tri")
    P.set_site(lokhandwala["lnglat"], locality="Andheri W")
    P.place_towers()
    P.choose_placement("A")
    r = P.set_plate("T1", "triangle", {})
    plate = P.project.plates[0]
    assert plate.params["unit_depth"] >= 9.0
    assert plate.params["core_at_base"] == 1.0  # a ~47 m triangle is too small for a central core
    assert sum(z.area for z in plate.zones) == pytest.approx(plate.area, abs=0.005)
    assert len(r["units"]) >= 6
    assert all(u["programme"]["bhk"] >= 2 for u in r["units"])
    res = P.solve_all("T1")
    assert res["solved"] == res["total"]
    for u in res["units"]:
        assert "duct_all" not in _fixes(u["applied_fix"]), u
    assert all(u.programme.bhk >= 2 for u in P.project.units)
    outer = Polygon(plate.outline).exterior
    for plan in P.project.plans:
        for room in plan.rooms:
            if room.type == "balcony":
                assert Polygon(room.polygon).distance(outer) < 0.4, (plan.unit_id, room.id)


def test_45m_triangle_keeps_deep_bands_and_2bhk_presets():
    L = 45.0
    H = round(L * 0.866, 2)
    fp = PlateFootprint(id="T1", polygon=[(-L / 2, -H / 2), (L / 2, -H / 2), (0.0, H / 2)], angle_deg=0.0,
                        center=(0.0, 0.0), width=L, depth=H, area=L * H / 2)
    p = fit("triangle", fp)
    assert p.params["unit_depth"] >= 9.0 and p.params["core_at_base"] == 1.0
    assert sum(z.area for z in p.zones) == pytest.approx(p.area, abs=0.005)
    us = split_units(p)
    assert len(us) >= 6
    assert all(u.core_link is not None and not u.flags for u in us)
    assert all(u.programme.bhk >= 2 for u in us)
    # party-wall shafts sit between two flats; corner shafts against one
    flats = [Polygon(u.polygon) for u in us]
    shafts = geo.polygons_of(zones_geom(p, "shaft"))
    assert shafts
    for sh in shafts:
        assert 1 <= sum(1 for f in flats if f.distance(sh) < 0.05) <= 2
    party = [sh for sh in shafts if sum(1 for f in flats if f.distance(sh) < 0.05) == 2]
    assert len(party) >= 2 and all(sh.area >= 3.0 for sh in party)


def test_big_triangle_keeps_central_core():
    L = 62.0
    H = round(L * 0.866, 2)
    fp = PlateFootprint(id="T1", polygon=[(-L / 2, -H / 2), (L / 2, -H / 2), (0.0, H / 2)], angle_deg=0.0,
                        center=(0.0, 0.0), width=L, depth=H, area=L * H / 2)
    p = fit("triangle", fp)
    assert p.params["core_at_base"] == 0.0 and p.params["unit_depth"] >= 9.0
    assert sum(z.area for z in p.zones) == pytest.approx(p.area, abs=0.005)
    us = split_units(p)
    assert len(us) >= 9 and all(u.core_link is not None for u in us)
    assert all(u.programme.bhk >= 2 for u in us)
