"""T-shape plate at the golden 52.9 m envelope: no sliver flats, and the narrow-bay flats solve.

Regression for golden flats APT-3 and APT-7 (1BHK slivers 1.1 m and 1.8 m wide): eight flats on the 52.9 m bar
left 8-9 m bays, and the façade-balanced recut clamped the end cuts onto the passage ends, so the cuts piled up
into slivers that had no door or no room."""

import pytest

from planner_engine.layout import solve_flat
from planner_engine.plate import fit
from planner_engine.units import MIN_UNIT_AREA, split_units


@pytest.fixture
def tee(footprint):
    return fit("t_shape", footprint(52.9, 29.0))


def test_default_unit_count_fits_the_bar(footprint, tee):
    assert int(tee.params["units"]) == 6
    # other envelopes keep the area rule's count
    assert int(fit("t_shape", footprint(40.0, 22.0)).params["units"]) == 4
    assert int(fit("t_shape", footprint(34.0, 24.0)).params["units"]) == 4


@pytest.mark.parametrize("n", [None, 7, 8])
def test_no_sliver_flats(tee, n):
    us = split_units(tee, n)
    assert len(us) == int(n or tee.params["units"])
    for u in us:
        assert u.core_link is not None, (u.id, u.flags)
        assert u.flags == [], (u.id, u.flags)
        assert u.area >= MIN_UNIT_AREA * 2
        xs = [p[0] for p in u.polygon]
        assert max(xs) - min(xs) > 4.0  # no 1-2 m wide strips


def test_wing_flats_enter_at_the_passage_end(tee):
    us = split_units(tee, 8)  # more flats than bays: the overhanging ends still get a flat of their own
    wing_x = max(abs(u.core_link[0]) for u in us if u.core_link)
    assert wing_x == pytest.approx(17.65, abs=0.1)


def test_narrow_bay_flats_solve(tee):
    """The two flats that used to fail (now the middle bays): they solve with a room-rule-clean plan."""
    from planner_engine.checks import FloorContext, run_checks

    us = {u.id: u for u in split_units(tee)}
    for uid in ("APT-2", "APT-6"):
        fr = solve_flat(tee, us[uid])
        assert fr.plan, (uid, fr.message)
        stairs = [(c.polygon, c.width) for c in tee.core_parts if c.kind == "stair"]
        sm = run_checks(FloorContext(plans=[fr.plan], height=24, passage_width=tee.passage_width, stairs=stairs))
        assert [r.rule_id for r in sm.results if r.result == "fail" and r.rule_id.startswith("DCPR")] == []
