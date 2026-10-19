"""Generator v4: the approved-case library, retrieval, adaptation and the guided solve (docs/quality/generator-v4.md)."""

from __future__ import annotations

import itertools
from importlib import resources

import pytest

from planner_engine import casebase
from planner_engine.diagram import flex_links, from_programme, zone_programme
from planner_engine.models import PlateFootprint, ShapeKind
from planner_engine.plate import fit
from planner_engine.solver import room_specs, unit_grid
from planner_engine.units import split_units


@pytest.fixture(scope="module")
def flat():
    fp = PlateFootprint(id="T1", polygon=[], angle_deg=0, center=(0, 0), width=40.0, depth=22.0, area=880.0)
    plate = fit(ShapeKind.t_shape, fp)
    unit = split_units(plate)[0]
    return plate, unit


def test_library_is_packaged_small_and_normalised():
    raw = resources.files("planner_engine").joinpath(casebase.CASEBASE).read_bytes()
    assert len(raw) < 3_000_000
    cases = casebase.load()
    assert len(cases) >= 200
    assert {c["bhk"] for c in cases} >= {1, 2, 3}
    for c in cases[:50]:
        assert c["score"] >= 70 and c["entry"] and len(c["facade"]) == 4
        assert any(r["t"] == "living" for r in c["rooms"]) and any(r["t"] == "kitchen" for r in c["rooms"])
        for r in c["rooms"]:
            u0, v0, u1, v1 = r["r"]
            assert -0.01 <= u0 <= u1 <= 1.01 and -0.01 <= v0 <= v1 <= 1.01
        n = len(c["rooms"])
        assert all(0 <= a < n and 0 <= b < n for a, b in c["edges"])


def test_hungarian_finds_the_minimum_assignment():
    cost = [[4, 1, 3, 9], [2, 0, 5, 9], [3, 2, 2, 9]]
    got = casebase.hungarian(cost)
    best = min(itertools.permutations(range(4), 3), key=lambda p: sum(cost[i][j] for i, j in enumerate(p)))
    assert sum(cost[i][j] for i, j in enumerate(got)) == sum(cost[i][j] for i, j in enumerate(best))
    assert len(set(got)) == 3


def test_retrieve_is_deterministic_and_filters_bhk_and_area(flat):
    plate, unit = flat
    a = casebase.retrieve(unit, unit.programme, 3, plate=plate)
    b = casebase.retrieve(unit, unit.programme, 3, plate=plate)
    assert [h["case"]["id"] for h in a] == [h["case"]["id"] for h in b] and len(a) == 3
    uf = casebase.unit_frame(unit_grid(plate, unit))
    for h in a:
        assert h["case"]["bhk"] == unit.programme.bhk
        assert abs(h["case"]["carpet"] / uf["carpet"] - 1) <= 0.35
    assert [h["distance"] for h in a] == sorted(h["distance"] for h in a)


def test_adapt_maps_case_rooms_into_the_grid(flat):
    plate, unit = flat
    ug = unit_grid(plate, unit)
    d = zone_programme(from_programme(unit, unit.programme), ug.interior.area, {}, unit)
    specs = room_specs(d, {}, ug)
    h = casebase.retrieve(unit, unit.programme, 1, plate=plate)[0]
    g = casebase.adapt(h["case"], ug, specs, [tuple(x) for x in d.links], h["mirror"])
    assert g is not None and g["case_id"] == h["case"]["id"]
    types = {s.id: s.type for s in specs}
    matched = {types[r] for r in g["rects"]}
    assert {"living", "kitchen"} <= matched
    for x0, y0, x1, y1 in g["rects"].values():
        assert 0 <= x0 <= x1 <= ug.Wg and 0 <= y0 <= y1 <= ug.Hg
    assert g["order"] and all(a in g["rects"] and b in g["rects"] and ax in "xy" for a, b, ax in g["order"])
    fixed, _alts = flex_links(d)
    assert fixed is not None


def test_entrance_frame_puts_the_door_on_the_front_side(flat):
    plate, unit = flat
    uf = casebase.unit_frame(unit_grid(plate, unit))
    assert uf["door"][1] <= 0.1  # the lobby door sits on the entrance side
    cases = casebase.load()
    near_front = sum(1 for c in cases if c["door"][1] <= 0.35)
    assert near_front / len(cases) >= 0.9


def test_v5_wet_stack_prefers_cases_whose_toilets_land_on_the_flats_vents(flat):
    """v5: a case whose toilets, stretched onto the flat, lie on the flat's façade / shaft edges ranks closer than the
    same case with its toilets moved to the middle of the flat."""
    plate, unit = flat
    uf = casebase.unit_frame(unit_grid(plate, unit))
    assert uf["vents"]
    u0, v0, u1, v1, _k = uf["vents"][0]
    on = {"rooms": [{"t": "toilet", "r": [max(0.0, u0 - 0.05), max(0.0, v0 - 0.05), min(1.0, u1 + 0.05), min(1.0, v1 + 0.05)]}]}
    off = {"rooms": [{"t": "toilet", "r": [0.45, 0.45, 0.55, 0.55]}]}
    assert casebase.wet_misfit(on, uf, False) == 0.0
    assert casebase.wet_misfit(off, uf, False) > 1.0
