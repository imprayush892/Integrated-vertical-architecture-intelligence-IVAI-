"""The core library (cores.py) wired into plate.py: best-ranked core per template, placed per the template, typed parts,
ranked alternatives, a user-selectable core type, and the zone invariant (flats + core + passage + shaft == plate)."""

import pytest

from planner_engine import cores
from planner_engine import plate as plate_mod
from planner_engine.engine import Planner, PlannerError
from planner_engine.models import CorePart
from planner_engine.plate import CORE_ORDER, core_code, core_info, core_name, fit
from planner_engine.units import split_units

RECT_SHAPES = ["central_core", "cruciform", "h_shape", "t_shape", "side_core"]
ENVELOPES = [(44.0, 24.0, 24.0), (61.5, 22.0, 24.0), (52.0, 18.0, 60.0), (36.0, 20.0, 45.0)]
ALLOWED = set(plate_mod.get_args(CorePart.model_fields["kind"].annotation))


def _sum_ok(pl):
    return abs(sum(z.area for z in pl.zones) - pl.area) < 0.011


@pytest.mark.parametrize("shape", RECT_SHAPES)
@pytest.mark.parametrize("w,d,h", ENVELOPES)
def test_every_template_gets_a_library_core(footprint, shape, w, d, h):
    pl = fit(shape, footprint(w, d), h)
    assert _sum_ok(pl)
    info = core_info(pl)
    if (shape, w, d, h) == ("t_shape", 36.0, 20.0, 45.0):
        # a 14.4 m stem 10 m long holds none of the 16 m clusters: the template's own box core stays, and says so
        assert pl.params["core_type_used"] == 0 and info["mode"] == "template" and info["alternatives"]
        assert not any(a["placed"] for a in info["alternatives"])
        return
    assert pl.params["core_type_used"] >= 1
    assert info["mode"] == "library" and info["selected"] == core_name(pl.params["core_type_used"])
    assert len(info["alternatives"]) == len(CORE_ORDER)
    assert sum(a["chosen"] for a in info["alternatives"]) == 1
    # the chosen core is the best ranked one that can be placed (auto)
    placed = [a for a in info["alternatives"] if a["placed"] and a["compliant"] and a["score"] > 0]
    assert info["selected"] == placed[0]["type"]
    # code: two stairs above 15 m, a fire lift above 24 m
    kinds = [p["kind"] for p in info["parts"]]
    assert kinds.count("stair") == (2 if h > 15 else 1)
    assert (kinds.count("fire_lift") >= 1) == (h > 24)
    assert all(k in cores.PART_STYLE for k in kinds)
    assert all(p.kind in ALLOWED for p in pl.core_parts)
    assert sum(p.kind == "stair" for p in pl.core_parts) == kinds.count("stair")
    # core parts lie in the core zone
    core_zone = plate_mod.zones_geom(pl, "core").buffer(0.02)
    for p in pl.core_parts:
        assert core_zone.contains(plate_mod.Polygon(p.polygon)), p.kind


@pytest.mark.parametrize("shape", RECT_SHAPES)
@pytest.mark.parametrize("w,d,h", ENVELOPES)
def test_flats_reach_the_lobby(footprint, shape, w, d, h):
    pl = fit(shape, footprint(w, d), h)
    us = split_units(pl)
    assert us, "no flats"
    for u in us:
        assert u.core_link is not None and "no_core_access" not in u.flags, (u.id, u.flags)


def test_templates_use_the_ranking(footprint):
    assert core_name(fit("central_core", footprint(30, 30), 24).params["core_type_used"]) == "central_compact"
    assert core_name(fit("cruciform", footprint(44, 24), 45).params["core_type_used"]) == "central_compact"
    # the H plate is a split core in a wing junction when the wings are wide enough to hold it
    assert core_name(fit("h_shape", footprint(44, 24), 45).params["core_type_used"]) == "split_junction"


@pytest.mark.parametrize("ctype", CORE_ORDER)
def test_core_type_param_switches_the_core(footprint, ctype):
    fp = footprint(61.5, 22.0)
    pl = fit("side_core", fp, 24, {"core_type": core_code(ctype)})
    info = core_info(pl)
    row = next(a for a in info["alternatives"] if a["type"] == ctype)
    assert _sum_ok(pl)
    if row["placed"]:
        assert info["selected"] == ctype and not info["note"]
    else:  # cannot be placed: auto takes over and says why
        assert info["selected"] == info["auto"] and "cannot be placed" in info["note"]
    for u in split_units(pl):
        assert u.core_link is not None


def test_satellite_cores_have_a_second_cluster(footprint):
    pl = fit("side_core", footprint(61.5, 22.0), 24, {"core_type": core_code("linear_corridor")})
    boxes = core_info(pl)["boxes"]
    assert len(boxes) == 2 and boxes[1][0] - boxes[0][2] > 20  # stair 2 at the far end of the corridor
    assert plate_mod.zones_geom(pl, "core").geom_type == "MultiPolygon"


def test_auto_and_back(footprint):
    fp = footprint(61.5, 22.0)
    a = fit("t_shape", fp, 24)
    b = fit("t_shape", fp, 24, {"core_type": 0})
    assert a.params["core_type_used"] == b.params["core_type_used"]


def test_refuge_floor_variant_has_a_refuge_area(footprint):
    pl = fit("central_core", footprint(44, 24), 45, {"refuge": 1})
    assert any(p["kind"] == "refuge" for p in core_info(pl)["parts"])
    assert _sum_ok(pl)


def test_triangle_is_left_alone(footprint):
    pl = fit("triangle", footprint(46, 40), 24)
    assert pl.params["core_type_used"] == 0
    info = core_info(pl)
    assert info["mode"] == "template" and info["alternatives"] == []
    assert _sum_ok(pl)


def test_core_code_names():
    assert core_code("auto") == 0 and core_code(None) == 0 and core_name(0) is None
    assert [core_name(core_code(n)) for n in CORE_ORDER] == list(CORE_ORDER)
    with pytest.raises(ValueError):
        core_code("nonsense")


def test_h_shape_that_swallows_the_plate_does_not_crash(footprint):
    pl = fit("h_shape", footprint(30.0, 30.0), 24)  # used to raise (empty flat zone)
    assert _sum_ok(pl)


@pytest.fixture
def planner(lokhandwala):
    p = Planner(None, "t")
    p.set_site(lokhandwala["lnglat"], locality="Andheri W")
    p.place_towers()
    p.choose_placement("A")
    return p


def test_engine_returns_core_alternatives(planner):
    r = planner.set_plate("T1", "cruciform", {})
    assert r["core"]["alternatives"] and r["core"]["selected"] == r["core"]["auto"]
    r2 = planner.set_plate("T1", params={"core_type": "scissor"})
    assert r2["core"]["requested"] == "scissor"
    assert planner.core_options("T1")["selected"] == r2["core"]["selected"]
    pv = planner.preview_plate("T1", "cruciform", {"core_type": "auto"})
    assert pv["core"]["selected"] is not None
    assert pv["params"]["height_m"] == planner.project.site.building_height
    with pytest.raises(PlannerError):
        planner.set_plate("T1", params={"core_type": "nonsense"})


def test_plan_quality_needs_a_solved_flat(planner):
    planner.set_plate("T1", "central_core", {})
    with pytest.raises(PlannerError):
        planner.plan_quality("APT-1")


def test_rpc_whitelist():
    from planner_engine.server import RPC_METHODS

    assert {"core_options", "plan_quality"} <= RPC_METHODS
