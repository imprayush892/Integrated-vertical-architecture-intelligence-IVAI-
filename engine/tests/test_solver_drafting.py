
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


# generator v3: these two tests used to put a 4BHK into central_core APT-1, a 163 m2 flat behind a 15 m façade. v3 cuts
# an exterior chowk beside that flat (plate.py), which gives it 30 m of façade, and a 4BHK now fits there. The tests
# are about an impossible brief, so they now use flats where a 4BHK still cannot fit: h_shape APT-1 (100 m2, 15.8 m of
# façade) for the explanation, and the 87 m2 t_shape 40x22 APT-1 for the fix ladder (4BHK -> 3BHK -> 2BHK; in the
# 100 m2 h_shape flat the 3BHK attempt can use up the 9.5 s budget before the ladder reaches a programme that fits).
def test_infeasible_core_names_rooms(footprint):
    p = _plate(footprint, "h_shape")
    u = split_units(p)[0]
    d = from_programme(u, presets()["4bhk"])
    ug = unit_grid(p, u)
    core = explain_infeasible(ug, room_specs(d, ug=ug), d.links, d.entry, budget=6)
    assert core and all(":" in c for c in core)


def test_fix_ladder_finishes_the_plan(footprint):
    p = _plate(footprint, "t_shape", 40.0, 22.0)
    u = split_units(p)[0]
    u.programme = presets()["4bhk"]
    fr = solve_flat(p, u)
    assert fr.plan is not None
    assert fr.applied_fix is not None
    assert fr.conflicts and fr.conflicts[0].fixes


HABITABLE = ("living", "master", "bedroom", "study")
MIN_HABITABLE_WIDTH = 2.40  # DCPR 37(1)(c)


def test_clear_width_of_notched_room():
    """A corner nick from a neighbour's wall must not make the width read low (the raster measure read 2.47)."""
    from shapely.geometry import Polygon

    from planner_engine import geo
    from planner_engine.drafting import clear_width

    room = Polygon([(14.172, 9.058), (14.172, 5.515), (14.615, 5.515), (14.615, 5.573), (16.715, 5.573),
                    (16.715, 9.058)])
    assert clear_width(room) == 2.543
    assert clear_width(room) >= geo.clear_width(room)
    l_shape = Polygon([(0, 0), (4, 0), (4, 2.5), (2.5, 2.5), (2.5, 4), (0, 4)])
    assert clear_width(l_shape) == 2.5


def test_solver_minimums_survive_drafting(footprint):
    """Every habitable room of a solved 3BHK is drafted at >= 2.40 m, even from a short, rough solve.

    A short time limit gives the weakest incumbents (rooms right at the solver minimum), which is where
    drafting used to shave a room below the statutory width."""
    from planner_engine import layout

    p = _plate(footprint, "side_core", 34.0, 24.0)
    stairs = [(c.polygon, c.width) for c in p.core_parts if c.kind == "stair"]
    units = [u for u in split_units(p) if u.programme and u.programme.bhk == 3][:2]
    assert units
    for u in units:
        for limit in (1.0, 2.0):
            layout._CACHE.clear()
            fr = solve_flat(p, u, time_limit=limit, auto_fix=False)
            if fr.plan is None:
                continue
            habitable = [r for r in fr.plan.rooms if r.type in HABITABLE]
            assert habitable
            assert all(r.width >= MIN_HABITABLE_WIDTH for r in habitable), [(r.id, r.width) for r in habitable]
            sm = run_checks(FloorContext(plans=[fr.plan], height=24, passage_width=p.passage_width, stairs=stairs))
            assert not [r for r in sm.results if r.result == "fail" and r.rule_id == "DCPR-37-1-c"]


def test_flat_budget_bounds_fix_ladder(footprint):
    """A flat that needs the fix ladder still returns within the 9.5 s budget (plus slack for drafting)."""
    import time

    from planner_engine import layout

    p = _plate(footprint, "central_core")
    u = split_units(p)[0]
    u.programme = presets()["4bhk"]
    layout._CACHE.clear()
    t0 = time.perf_counter()
    fr = solve_flat(p, u, budget=6.0)
    assert time.perf_counter() - t0 < 6.0 + 1.0
    assert fr.plan is not None or fr.conflicts


# ---------------------------------------------------------------------------- drafting never shaves width

def test_clear_width_of_l_shaped_room_is_exact():
    from shapely.geometry import Polygon

    from planner_engine import geo
    from planner_engine.drafting import clear_width

    # an L whose arms are 2.485 m wide (the narrowest the solver allows: 2.6 m cell - 0.115 m wall):
    # the old raster measure (bbox/80) read it as 2.3 m, which showed up as a 2.39 m living room.
    for arm in (2.485, 2.5, 2.6):
        ell = Polygon([(0, 0), (8, 0), (8, arm), (arm, arm), (arm, 8), (0, 8)])
        assert abs(clear_width(ell) - arm) < 1e-6
    # every caller (drafting, DXF import, checks) gets the exact value
    assert abs(geo.clear_width(Polygon([(0, 0), (8, 0), (8, 2.485), (2.485, 2.485), (2.485, 8), (0, 8)])) - 2.485) < 1e-6


def test_solved_3bhk_habitable_rooms_at_least_statutory_width(footprint):
    p = _plate(footprint)
    u = next(u for u in split_units(p) if u.programme.bhk == 3)
    fr = solve_flat(p, u)
    assert fr.plan is not None, fr.message
    for r in fr.plan.rooms:
        if r.type in ("living", "master", "bedroom", "study"):
            assert r.width >= 2.40, (r.id, r.width)
            assert r.area >= 7.5, (r.id, r.area)


# ------------------------------------------------------------------ a plan that fails a check is never returned

def _narrow(plan):
    plan = plan.model_copy(deep=True)
    for r in plan.rooms:
        if r.type in ("living", "master", "bedroom"):
            r.width = 2.39
    return plan


def test_failing_drafted_plan_is_never_returned_as_solved(footprint, monkeypatch):
    from planner_engine import layout

    p = _plate(footprint)
    u = next(u for u in split_units(p) if u.programme.bhk == 2)
    real = layout.draft
    monkeypatch.setattr(layout, "draft", lambda *a, **k: _narrow(real(*a, **k)))
    fr = solve_flat(p, u, time_limit=2.0)
    assert fr.plan is None
    assert fr.result.status == "infeasible"
    assert fr.diagram.feasible is False
    assert fr.conflicts and fr.conflicts[-1].id == "validation" and "2.39" in fr.conflicts[-1].reason
    assert fr.validation["failed_checks"] >= 1 and fr.validation["extended"]


def test_failed_check_triggers_resolve_and_returns_valid_plan(footprint, monkeypatch):
    from planner_engine import layout

    p = _plate(footprint)
    u = next(u for u in split_units(p) if u.programme.bhk == 2)
    real = layout.draft
    calls = []

    def flaky(*a, **k):
        plan = real(*a, **k)
        calls.append(1)
        return _narrow(plan) if len(calls) == 1 else plan

    monkeypatch.setattr(layout, "draft", flaky)
    fr = solve_flat(p, u, time_limit=3.0)
    assert fr.plan is not None and len(calls) >= 2
    assert not layout.room_check_fails(fr.plan, p)
    assert fr.validation["extended"] and fr.validation["failed_checks"] == 1


def test_timed_out_first_search_gets_more_time_not_a_smaller_flat(footprint, monkeypatch):
    """A search that ran out of time (nothing proven impossible) is given more time before any fix changes
    the programme: slow is acceptable, a downgraded or failed flat is not."""
    from planner_engine import layout
    from planner_engine.solver import SolveResult

    p = _plate(footprint)
    u = next(u for u in split_units(p) if u.programme.bhk == 2)
    real = layout._solve
    calls = []

    def slow_first(*a, **k):
        calls.append(a[4])
        if len(calls) == 1:
            return SolveResult("timeout", time_s=a[4]), {}
        return real(*a, **k)

    monkeypatch.setattr(layout, "_solve", slow_first)
    fr = solve_flat(p, u, time_limit=2.0)
    assert fr.plan is not None and fr.applied_fix is None
    assert calls[1] > 2.0  # the retry got the extended budget
    assert fr.validation["extended"]


# ---------------------------------------------------------------------------------------------- generator v2

def test_v2_plan_enters_living_with_little_circulation(footprint):
    """Generator v2 on a 2BHK: the front door opens into the living-dining (or a short foyer before it), passage +
    foyer stay a small share of the flat, no big leftover store, every bedroom opens onto a public space."""
    from shapely.geometry import Polygon

    from planner_engine.quality import critique

    p = _plate(footprint)
    u = next(u for u in split_units(p) if u.programme.bhk == 2)
    fr = solve_flat(p, u)
    assert fr.plan is not None, fr.message
    res = critique(fr.plan, u, p, furniture=False)
    assert res["metrics"]["entry_seq"]["value"] >= 0.85  # living, foyer -> living, or dining -> living
    assert res["metrics"]["circ_share"]["value"] <= 12.0
    rooms = {r.id: r for r in fr.plan.rooms}
    assert all(r.area < 5.0 for r in fr.plan.rooms if r.type == "store")
    public = {"living", "dining", "passage", "foyer"}
    for r in fr.plan.rooms:
        if r.type in ("master", "bedroom"):
            assert any(r.id in d.rooms and any(rooms[o].type in public for o in d.rooms if o != r.id and o in rooms)
                       for d in fr.plan.doors), r.id
    assert all(Polygon(r.polygon).is_valid for r in fr.plan.rooms)


def test_grow_to_fill_keeps_rectangles_and_contacts():
    """Rooms push into free floor only across fully free strips: they stay rectangles and never shrink."""
    from shapely.geometry import box

    from planner_engine.drafting import grow_to_fill
    from planner_engine.solver import RoomSpec, UnitGrid

    ug = UnitGrid("u", 0.0, 0.0, 60, 40, box(0, 0, 6, 4), [], [], None, box(0, 0, 6, 4), box(0, 0, 0, 0))
    specs = [RoomSpec(id="a", type="living", name="a", tgt=1600, amin=7.5, wmin=26, aspect=23, window_run=0,
                      vent_run=0, optional=False, target_area=16, cap=2000, hard=3000, over_w=2),
             RoomSpec(id="b", type="bedroom", name="b", tgt=1000, amin=7.5, wmin=26, aspect=16, window_run=0,
                      vent_run=0, optional=False, target_area=10, cap=1300, hard=2000, over_w=3)]
    rects = {"a": (0, 0, 30, 30), "b": (30, 0, 26, 40)}
    out = grow_to_fill(ug, specs, rects)
    for rid, (x, y, w, h) in out.items():
        x0, y0, w0, h0 = rects[rid]
        assert x <= x0 and y <= y0 and x + w >= x0 + w0 and y + h >= y0 + h0
    covered = sum(w * h for _x, _y, w, h in out.values())
    assert covered > sum(w * h for _x, _y, w, h in rects.values())
