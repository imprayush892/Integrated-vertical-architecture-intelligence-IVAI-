import pytest

from planner_engine import rules
from planner_engine.models import ShapeKind
from planner_engine.placement import place
from planner_engine.plate import build_plate, fit
from planner_engine.site import build_site
from planner_engine.units import pick_preset, programme_from_rooms, split_units, statutory_minimums


def test_rules_load_and_ids_unique():
    cl = rules.clauses()
    assert len(cl) >= 12
    assert len({c.id for c in cl}) == len(cl)
    assert {c.code for c in cl} == {"DCPR 2034", "NBC 2016 Part 4"}
    assert rules.setback_for(24, 12)[0] == 3.0
    assert rules.tower_gap_for(60)[0] == 12.0


def test_rules_override_dir(tmp_path, monkeypatch):
    (tmp_path / "site.yaml").write_text(
        "code: X\nsetback: {clause: c, by_height: [[9999, 7.0]], front_by_road_width: [[9999, 7.0]]}\n"
        "tower_gap: {clause: g, by_height: [[9999, 9.0]]}\ncoverage: {clause: k, max: 0.3}\n")
    monkeypatch.setenv("PLANNER_RULES_DIR", str(tmp_path))
    rules.reload()
    try:
        assert rules.setback_for(24, 12)[0] == 7.0
    finally:
        monkeypatch.delenv("PLANNER_RULES_DIR")
        rules.reload()


def test_statutory_minimums_from_rules():
    m = statutory_minimums()
    assert m["wc"] == (1.1, 0.9)
    assert m["toilet"] == (2.2, 1.2)


def test_placement_three_options_fast(lokhandwala):
    s = build_site([tuple(p) for p in lokhandwala["lnglat"]])
    pl = place(s)
    assert [o.id for o in pl.options] == ["A", "B", "C"]
    assert pl.elapsed_s < 3.0
    for o in pl.options:
        assert all(o.passes.values()), o.passes
        assert 0.2 < o.coverage <= 0.45
        assert o.drivers.startswith("Design drivers:")
    assert pl.options[1].min_gap >= rules.tower_gap_for(s.building_height)[0] - 1e-6


@pytest.mark.parametrize("shape", list(ShapeKind))
def test_zones_add_up_exactly(shape, footprint):
    for w, d in ((52.9, 29.0), (40.0, 22.0), (34.0, 24.0)):
        p = fit(shape, footprint(w, d))
        assert sum(z.area for z in p.zones) == pytest.approx(p.area, abs=0.005)
        assert any(c.kind == "stair" for c in p.core_parts)


def test_cruciform_has_12_corners(footprint):
    assert fit("cruciform", footprint()).corners == 12


def test_drag_params_keep_zones_exact(footprint):
    p = fit("cruciform", footprint())
    for wing in (12.0, 16.5, 20.0):
        q = build_plate("cruciform", {**p.params, "wing_width": wing}, env=(52.9, 29.0))
        assert sum(z.area for z in q.zones) == pytest.approx(q.area, abs=0.005)


def test_units_reach_core_and_never_deleted(footprint):
    for shape in ("central_core", "cruciform", "h_shape", "t_shape", "side_core"):
        p = fit(shape, footprint())
        us = split_units(p)
        assert len(us) == int(p.params["units"])
        assert all(u.core_link is not None for u in us), [u.flags for u in us]
        assert sum(u.area for u in us) == pytest.approx(next(z.area for z in p.zones if z.kind == "flats"), rel=1e-3)


def test_presets_and_custom_programme():
    assert pick_preset(100, facade=20).bhk == 3
    assert pick_preset(100, facade=9).bhk <= 1 or pick_preset(100, facade=9).bhk == 1
    prog = programme_from_rooms("3BHK pooja", [
        {"id": "foyer", "type": "foyer", "name": "Foyer", "target_area": 3},
        {"id": "living", "type": "living", "name": "Living", "target_area": 18, "needs_window": True},
        {"id": "pooja", "type": "pooja", "name": "Pooja", "target_area": 2},
    ], [["foyer", "living"], ["living", "pooja"]], "foyer")
    assert prog.entry == "foyer"
    with pytest.raises(ValueError):
        programme_from_rooms("bad", [{"id": "x", "type": "garage", "name": "X", "target_area": 3}], [])


# ---------------------------------------------------------------------------------------------- generator v5 briefs

def test_v5_brief_follows_market_bands_and_facade():
    """v5: the BHK follows the carpet bands (2BHK 55-80, 3BHK 85-120, 4BHK 130-180, stepped up 5 % clear of the floor,
    4BHK from 140) and only when every window room finds its own stretch of a straight façade run."""
    from planner_engine.units import size_programme, window_rooms_fit

    assert size_programme(45, runs=[12.0]).bhk == 1
    assert size_programme(70, runs=[14.0]).bhk == 2
    assert size_programme(100, runs=[18.0]).bhk == 3
    assert size_programme(100, runs=[12.5]).bhk == 2  # one 12.5 m run cannot light five rooms
    assert size_programme(135, runs=[20.0, 6.5]).bhk == 3  # 130-140 m2: a premium 3BHK
    assert size_programme(150, runs=[21.0, 9.0]).bhk == 4
    assert window_rooms_fit([12.0], 2) and not window_rooms_fit([11.0], 2)
    assert window_rooms_fit([6.5, 6.5, 6.5], 3)  # rooms spread over three runs


def test_v5_every_bedroom_attached_and_powder_by_living():
    from planner_engine.units import size_programme

    for carpet, runs in ((100, [18.0]), (125, [18.0, 8.0]), (75, [14.0])):
        p = size_programme(carpet, runs=runs)
        types = {r.id: r.type for r in p.rooms}
        beds = [r for r, t in types.items() if t in ("master", "bedroom")]
        for b in beds:
            assert any(b in l and types[l[0] if l[1] == b else l[1]] == "toilet" for l in p.links), (carpet, b)
        powder = [r for r in p.rooms if r.type == "wc"]
        assert powder and ("living", powder[0].id) in [tuple(l) for l in p.links]
    small = size_programme(62, runs=[14.0])  # a 2BHK up to 70 m2 keeps one common toilet
    assert small.bhk == 2 and sum(1 for r in small.rooms if r.type == "toilet") == 2


def test_v5_spare_floor_goes_into_rooms_not_bloat():
    """A large 3BHK brief adds a dress room, a servant room by the kitchen, a store... and its rooms stay within the
    approved p75 sizes; the brief's total tracks the carpet."""
    from planner_engine.units import GROW_TO, size_programme

    p = size_programme(128, runs=[17.0, 12.0])
    ids = {r.id for r in p.rooms}
    assert {"dress_master", "servant", "store"} <= ids
    assert all(r.target_area <= GROW_TO[r.type] + 1e-6 for r in p.rooms if r.type in GROW_TO)
    total = sum(r.target_area for r in p.rooms)
    assert 0.85 * 128 <= total <= 1.05 * 128
    lean = size_programme(92, runs=[18.0])
    assert "servant" not in {r.id for r in lean.rooms}
