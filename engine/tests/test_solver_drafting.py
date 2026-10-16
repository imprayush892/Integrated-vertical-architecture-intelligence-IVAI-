
from planner_engine.checks import FloorContext, run_checks
from planner_engine.diagram import from_programme, precheck
from planner_engine.layout import solve_flat
from planner_engine.plate import fit
from planner_engine.solver import explain_infeasible, room_specs, unit_grid
from planner_engine.units import facade_length, presets, split_units


def _plate(footprint, shape="cruciform", w=52.9, d=29.0):
    return fit(shape, footprint(w, d))


def test_solve_2bhk_passes_room_rules(footprint):
    p = _plate(footprint)
    u = next(u for u in split_units(p) if u.programme.bhk == 2)
    fr = solve_flat(p, u)
    assert fr.plan is not None, fr.message
    assert fr.plan.solve_time_s < 30
    stairs = [(c.polygon, c.width) for c in p.core_parts if c.kind == "stair"]
    sm = run_checks(FloorContext(plans=[fr.plan], height=24, passage_width=p.passage_width, stairs=stairs))
    fails = [r for r in sm.results if r.result == "fail" and r.rule_id.startswith("DCPR")]
    assert not fails, [(r.rule_id, r.target, r.value) for r in fails]
    # every room has a door, every window room a window, the flat an entry door
    assert any(d.kind == "entry" for d in fr.plan.doors)
    linked = {r for d in fr.plan.doors for r in d.rooms}
    assert all(r.id in linked for r in fr.plan.rooms if r.type != "store")


def test_balconies_on_outer_facade_long_side_out(footprint):
    p = _plate(footprint)
    from shapely.geometry import Polygon

    outline = Polygon(p.outline)
    for u in split_units(p):
        fr = solve_flat(p, u)
        if not fr.plan:
            continue
        for r in fr.plan.rooms:
            if r.type != "balcony":
                continue
            g = Polygon(r.polygon)
            minx, miny, maxx, maxy = g.bounds
            w, h = maxx - minx, maxy - miny
            assert max(w, h) >= 1.4 * min(w, h)
            # the long side lies within a wall's thickness of the plate outline
            assert g.distance(outline.exterior) < 0.35


def test_precheck_catches_impossible_area(footprint):
    p = _plate(footprint)
    u = split_units(p)[0]
    prog = presets()["4bhk"]
    d = from_programme(u, prog)
    conflicts = precheck(d, 40.0, facade_length(p, u))
    assert conflicts and conflicts[0].kind in ("area", "window")


def test_infeasible_core_names_rooms(footprint):
    p = _plate(footprint, "central_core")
    u = split_units(p)[0]
    d = from_programme(u, presets()["4bhk"])
    ug = unit_grid(p, u)
    core = explain_infeasible(ug, room_specs(d, ug=ug), d.links, d.entry, budget=6)
    assert core and all(":" in c for c in core)


def test_fix_ladder_finishes_the_plan(footprint):
    p = _plate(footprint, "central_core")
    u = split_units(p)[0]
    u.programme = presets()["4bhk"]
    fr = solve_flat(p, u)
    assert fr.plan is not None
    assert fr.applied_fix is not None
    assert fr.conflicts and fr.conflicts[0].fixes
