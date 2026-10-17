"""Plan critic (planner_engine.quality) and plan renderer (planner_engine.render).

Synthetic plans are built from rectangles so every expectation can be checked by hand; one integration test runs the
real solver on a small flat.
"""

from __future__ import annotations

import copy

import pytest

from planner_engine import quality as Q
from planner_engine.models import Opening, Plan, PlanRoom

# ------------------------------------------------------------------------------------------------ builders


def build(rooms, doors, windows, uid="T") -> Plan:
    """rooms: [(id, type, (x0, y0, x1, y1))]; doors: [(id, a, b, [rooms], swing_into)]; windows: [(room, x0, y0, x1, y1)]."""
    wins, wl = [], {}
    for i, (rid, x0, y0, x1, y1) in enumerate(windows):
        w = ((x1 - x0) ** 2 + (y1 - y0) ** 2) ** 0.5
        wins.append(Opening(id=f"W{i}", kind="window", a=(x0, y0), b=(x1, y1), width=w, rooms=[rid]))
        wl[rid] = wl.get(rid, 0.0) + w
    prooms = []
    for rid, typ, (x0, y0, x1, y1) in rooms:
        prooms.append(PlanRoom(
            id=rid, type=typ, name=rid.replace("_", " ").title(), polygon=[(x0, y0), (x1, y0), (x1, y1), (x0, y1)],
            area=round((x1 - x0) * (y1 - y0), 2), width=min(x1 - x0, y1 - y0),
            has_window=rid in wl and typ not in ("toilet", "bath", "wc"), window_area=round(wl.get(rid, 0.0) * 1.5, 2),
            ventilated=rid in wl, vent_via="window" if rid in wl else None))
    ds = []
    for did, a, b, rs, sw in doors:
        w = ((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2) ** 0.5
        ds.append(Opening(id=did, kind="entry" if did == "D-entry" else "door", a=a, b=b, width=w, rooms=rs, hinge=a, swing_into=sw))
    return Plan(unit_id=uid, rooms=prooms, walls=[], doors=ds, windows=wins,
                carpet_area=sum(r.area for r in prooms if r.type != "balcony"), status="feasible")


GOOD_ROOMS = [
    ("living", "living", (0, 0, 5.0, 4.6)), ("dining", "dining", (5.0, 0, 8.6, 2.8)), ("kitchen", "kitchen", (5.0, 2.8, 8.6, 4.6)),
    ("hub", "passage", (3.4, 4.6, 5.0, 6.4)), ("master", "master", (0, 4.6, 3.4, 8.2)), ("toilet_m", "toilet", (0, 8.2, 2.2, 9.8)),
    ("bed2", "bedroom", (5.0, 4.6, 8.6, 8.2)), ("toilet_c", "toilet", (3.4, 6.4, 5.0, 8.8))]
GOOD_DOORS = [
    ("D-entry", (3.8, 0.0), (4.8, 0.0), ["living"], "living"), ("D1", (5.0, 0.8), (5.0, 1.7), ["living", "dining"], "dining"),
    ("D2", (6.0, 2.8), (6.9, 2.8), ["dining", "kitchen"], "dining"), ("D3", (3.7, 4.6), (4.6, 4.6), ["living", "hub"], "living"),
    ("D4", (3.4, 5.0), (3.4, 5.9), ["hub", "master"], "master"), ("D5", (5.0, 5.0), (5.0, 5.9), ["hub", "bed2"], "bed2"),
    ("D6", (3.7, 6.4), (4.45, 6.4), ["hub", "toilet_c"], "toilet_c"), ("D7", (0.4, 8.2), (1.15, 8.2), ["master", "toilet_m"], "master")]
GOOD_WINDOWS = [
    ("living", 0, 1.2, 0, 3.4), ("dining", 8.6, 0.8, 8.6, 2.0), ("kitchen", 8.6, 3.2, 8.6, 4.2), ("master", 0, 5.6, 0, 7.2),
    ("bed2", 8.6, 5.6, 8.6, 7.2), ("toilet_m", 0.5, 9.8, 1.3, 9.8), ("toilet_c", 4.0, 8.8, 4.6, 8.8)]


def good_plan() -> Plan:
    return build(GOOD_ROOMS, GOOD_DOORS, GOOD_WINDOWS)


def codes(res) -> set[str]:
    return {i["code"] for i in res["issues"]}


def msgs(res, code: str) -> list[str]:
    return [i["message"] for i in res["issues"] if i["code"] == code]


# ------------------------------------------------------------------------------------------------ whole-plan scoring


def test_good_plan_scores_high():
    res = Q.critique(good_plan())
    assert res["score"] >= 85, (res["score"], [i["message"] for i in res["issues"]])
    assert res["grade"] == "A"
    assert res["n_critical"] == 0
    assert not res["errors"]
    assert not [i for i in res["issues"] if i["severity"] in ("critical", "major")]
    m = res["metrics"]
    assert m["entry_seq"]["value"] == 1.0 and m["kitchen_near"]["value"] == 1.0 and m["privacy"]["value"] == 1.0
    assert m["circ_share"]["value"] < 6
    assert all(m[k]["satisfaction"] == 1.0 for k in ("fit_sleep", "fit_living", "fit_kitchen", "fit_toilet"))


def test_result_shape_and_determinism():
    a, b = Q.critique(good_plan()), Q.critique(good_plan())
    assert a == b
    assert set(a) >= {"score", "grade", "metrics", "subscores", "issues", "furniture", "stats", "errors"}
    for k, m in a["metrics"].items():
        assert k in Q.METRICS and 0 <= m["satisfaction"] <= 1
    assert set(a["subscores"]) == set(Q.CATEGORIES)
    assert sum(mt.weight for mt in Q.METRICS.values()) == pytest.approx(100.0)
    for i in a["issues"]:
        assert i["severity"] in Q.SEV_ORDER


def test_accepts_dicts_and_skips_unit_metrics_without_unit():
    res = Q.critique(good_plan().model_dump())
    assert "efficiency" not in res["metrics"] and "travel_to_exit" not in res["metrics"]
    assert res["score"] >= 85


def test_bedroom_entered_through_another_bedroom_is_flagged():
    rooms = [("living", "living", (0, 0, 5.0, 4.6)), ("dining", "dining", (5.0, 0, 8.6, 2.8)), ("kitchen", "kitchen", (5.0, 2.8, 8.6, 4.6)),
             ("master", "master", (0, 4.6, 3.6, 8.2)), ("bed2", "bedroom", (3.6, 4.6, 7.2, 8.2)), ("toilet_c", "toilet", (7.2, 4.6, 8.6, 6.9))]
    doors = [("D-entry", (3.8, 0.0), (4.8, 0.0), ["living"], "living"), ("D1", (5.0, 0.8), (5.0, 1.7), ["living", "dining"], "dining"),
             ("D2", (6.0, 2.8), (6.9, 2.8), ["dining", "kitchen"], "dining"), ("D3", (0.8, 4.6), (1.7, 4.6), ["living", "master"], "master"),
             ("D4", (3.6, 5.2), (3.6, 6.1), ["master", "bed2"], "bed2"), ("D5", (7.2, 5.0), (7.2, 5.75), ["bed2", "toilet_c"], "toilet_c")]
    wins = [("living", 0, 1.2, 0, 3.4), ("master", 0, 5.6, 0, 7.2), ("bed2", 4.5, 8.2, 6.5, 8.2), ("kitchen", 8.6, 3.2, 8.6, 4.2)]
    res = Q.critique(build(rooms, doors, wins))
    assert "bedroom_via" in codes(res)
    crit = [i for i in res["issues"] if i["code"] == "bedroom_via"][0]
    assert crit["severity"] == "critical" and "bed2" in crit["rooms"]
    assert res["metrics"]["privacy"]["value"] < 1.0
    assert res["score"] < Q.critique(good_plan())["score"] - 15


def test_front_door_into_a_bedroom_is_critical():
    doors = [d for d in GOOD_DOORS if d[0] != "D-entry"] + [("D-entry", (8.6, 5.0), (8.6, 6.0), ["bed2"], "bed2")]
    res = Q.critique(build(GOOD_ROOMS, doors, GOOD_WINDOWS))
    assert "entry_wrong" in codes(res)
    assert res["metrics"]["entry_seq"]["value"] == 0.0
    assert res["score"] < 60


def test_twenty_five_percent_corridor_is_flagged():
    rooms = [("living", "living", (0, 0, 5.0, 4.6)), ("dining", "dining", (5.0, 0, 8.6, 2.8)), ("kitchen", "kitchen", (5.0, 2.8, 8.6, 4.6)),
             ("spine", "passage", (0, 4.6, 8.6, 5.9)), ("master", "master", (0, 5.9, 3.6, 8.9)), ("toilet_m", "toilet", (3.6, 7.2, 5.4, 8.9)),
             ("toilet_c", "toilet", (3.6, 5.9, 5.4, 7.2)), ("bed2", "bedroom", (5.4, 5.9, 8.6, 8.9))]
    doors = [("D-entry", (3.8, 0.0), (4.8, 0.0), ["living"], "living"), ("D1", (5.0, 0.8), (5.0, 1.7), ["living", "dining"], "dining"),
             ("D2", (6.0, 2.8), (6.9, 2.8), ["dining", "kitchen"], "dining"), ("D3", (3.7, 4.6), (4.6, 4.6), ["living", "spine"], "living"),
             ("D4", (0.5, 5.9), (1.4, 5.9), ["spine", "master"], "master"), ("D5", (3.9, 5.9), (4.65, 5.9), ["spine", "toilet_c"], "toilet_c"),
             ("D6", (6.0, 5.9), (6.9, 5.9), ["spine", "bed2"], "bed2"), ("D7", (3.6, 7.6), (3.6, 8.35), ["master", "toilet_m"], "toilet_m")]
    wins = [("living", 0, 1.2, 0, 3.4), ("master", 0, 6.4, 0, 8.0), ("bed2", 8.6, 6.4, 8.6, 8.0), ("kitchen", 8.6, 3.2, 8.6, 4.2),
            ("toilet_m", 4.0, 8.9, 4.6, 8.9), ("toilet_c", 3.6, 6.4, 3.6, 7.0)]
    res = Q.critique(build(rooms, doors, wins))
    share = res["metrics"]["circ_share"]["value"]
    assert share > 14, share
    assert "circ_share_high" in codes(res) and "corridor_long" in codes(res)
    assert res["subscores"]["circulation"] < 75
    assert res["score"] < Q.critique(good_plan())["score"] - 20


def test_toilet_without_wc_space_is_flagged():
    rooms = [r if r[0] != "toilet_c" else ("toilet_c", "toilet", (4.1, 6.4, 5.0, 7.7)) for r in GOOD_ROOMS]
    doors = [d if d[0] != "D6" else ("D6", (4.12, 6.4), (4.87, 6.4), ["hub", "toilet_c"], "toilet_c") for d in GOOD_DOORS]
    wins = [w for w in GOOD_WINDOWS if w[0] != "toilet_c"] + [("toilet_c", 4.3, 7.7, 4.9, 7.7)]
    res = Q.critique(build(rooms, doors, wins))
    bad = [i for i in res["issues"] if i["code"] == "furniture_none" and "toilet_c" in i["rooms"]]
    assert bad and "WC" in bad[0]["message"]
    assert res["furniture"]["toilet_c"]["tier"] == "none"
    assert res["metrics"]["fit_toilet"]["value"] < 1.0
    assert res["score"] < Q.critique(good_plan())["score"]


def test_kitchen_far_from_living_and_through_toilet_rooms():
    # dining/kitchen reached only through the toilet: through_wet + kitchen issues
    rooms = [("living", "living", (0, 0, 5.0, 4.6)), ("toilet_c", "toilet", (5.0, 0, 6.8, 2.4)), ("kitchen", "kitchen", (6.8, 0, 9.6, 3.0)),
             ("master", "master", (0, 4.6, 3.6, 8.2))]
    doors = [("D-entry", (3.8, 0.0), (4.8, 0.0), ["living"], "living"), ("D1", (5.0, 0.8), (5.0, 1.55), ["living", "toilet_c"], "toilet_c"),
             ("D2", (6.8, 0.8), (6.8, 1.7), ["toilet_c", "kitchen"], "kitchen"), ("D3", (0.8, 4.6), (1.7, 4.6), ["living", "master"], "master")]
    res = Q.critique(build(rooms, doors, [("living", 0, 1.2, 0, 3.4), ("kitchen", 9.6, 0.6, 9.6, 2.2), ("master", 0, 5.6, 0, 7.2)]))
    assert "through_toilet" in codes(res)
    assert any(i["severity"] == "critical" for i in res["issues"] if i["code"] == "through_toilet")
    assert res["metrics"]["through_wet"]["value"] == 0.0


def test_wasted_leftover_and_bloated_rooms():
    p = good_plan()
    # a sealed 6 m2 store the brief never asked for + an oversized foyer
    p.rooms.append(PlanRoom(id="store1", type="store", name="Store", polygon=[(8.6, 4.6), (10.6, 4.6), (10.6, 7.6), (8.6, 7.6)],
                            area=6.0, width=2.0))
    res = Q.critique(p)
    assert "leftover" in codes(res)
    assert res["metrics"]["waste_share"]["value"] > 4


def test_door_swings_and_narrow_passage():
    rooms = [("living", "living", (0, 0, 5.0, 4.6)), ("pass", "passage", (0, 4.6, 5.0, 5.4)), ("master", "master", (0, 5.4, 3.6, 9.0)),
             ("bed2", "bedroom", (3.6, 5.4, 7.2, 9.0))]
    doors = [("D-entry", (3.8, 0.0), (4.8, 0.0), ["living"], "living"), ("D1", (1.0, 4.6), (1.9, 4.6), ["living", "pass"], "pass"),
             ("D2", (2.4, 5.4), (3.3, 5.4), ["pass", "master"], "master"), ("D3", (3.6, 5.5), (3.6, 6.4), ["master", "bed2"], "master")]
    res = Q.critique(build(rooms, doors, [("living", 0, 1.2, 0, 3.4), ("master", 0, 6.0, 0, 8.0), ("bed2", 7.2, 6.0, 7.2, 8.0)]))
    assert "narrow_passage_door" in codes(res)
    assert "swing_clash" in codes(res)
    assert res["metrics"]["swing_clash"]["value"] >= 1


def test_entry_must_lead_to_living_foyer_ok_and_passage_not():
    rooms = [("foyer", "foyer", (3.4, 0, 5.0, 2.0)), ("living", "living", (0, 2.0, 5.0, 6.6)), ("master", "master", (0, 6.6, 3.6, 10.0))]
    doors = [("D-entry", (3.8, 0.0), (4.7, 0.0), ["foyer"], "foyer"), ("D1", (3.6, 2.0), (4.5, 2.0), ["foyer", "living"], "living"),
             ("D2", (0.5, 6.6), (1.4, 6.6), ["living", "master"], "master")]
    ok = Q.critique(build(rooms, doors, [("living", 0, 3.0, 0, 5.0), ("master", 0, 7.0, 0, 9.0)]))
    assert ok["metrics"]["entry_seq"]["value"] == 1.0
    rooms2 = [("pass", "passage", (3.4, 0, 5.0, 2.0)), ("living", "living", (0, 2.0, 5.0, 6.6)), ("master", "master", (0, 6.6, 3.6, 10.0))]
    doors2 = [("D-entry", (3.8, 0.0), (4.7, 0.0), ["pass"], "pass"), ("D1", (3.6, 2.0), (4.5, 2.0), ["pass", "living"], "living"),
              ("D2", (0.5, 6.6), (1.4, 6.6), ["living", "master"], "master")]
    bad = Q.critique(build(rooms2, doors2, [("living", 0, 3.0, 0, 5.0), ("master", 0, 7.0, 0, 9.0)]))
    assert bad["metrics"]["entry_seq"]["value"] < 1.0 and "entry_passage" in codes(bad)


# ------------------------------------------------------------------------------------------------ furniture


def _single(rtype: str, w: float, h: float, door=None, window=None):
    """One room of w x h with a door (default 0.9 m on the south wall, swinging in) and optional window."""
    rid = rtype
    d = door or ((0.2, 0.0), (1.1, 0.0))
    doors = [("D1", d[0], d[1], ["out", rid], rid)]
    rooms = [("out", "passage", (-4.0, -1.5, w + 4.0, 0.0)), (rid, rtype, (0, 0, w, h))]
    wins = [(rid, *window)] if window else [(rid, 0, h * 0.3, 0, h * 0.7)]
    return Q._Ctx(build(rooms, doors, wins), None, None)


@pytest.mark.parametrize("w,h,tier_ok", [(3.6, 3.6, True), (3.2, 3.4, True), (2.4, 2.4, False), (2.0, 3.0, False)])
def test_master_bed_with_clearance(w, h, tier_ok):
    ctx = _single("master", w, h)
    fit = Q._fit_bedroom(ctx, "master", True, 2)
    assert (fit.tier in ("full", "tight")) == tier_ok, (fit.tier, fit.missing)
    if tier_ok:
        bed = next(i for i in fit.items if i["name"] == "bed")
        from shapely.geometry import Polygon

        assert Polygon(bed["polygon"]).area == pytest.approx(1.8 * 2.0, rel=1e-3)


def test_wardrobe_and_door_swing_clear():
    ctx = _single("bedroom", 3.4, 3.6)
    fit = Q._fit_bedroom(ctx, "bedroom", False, 2)
    names = {i["name"] for i in fit.items}
    assert "bed" in names
    from shapely.geometry import Polygon

    door = ctx.doors[0]
    for it in fit.items:
        assert not Polygon(it["polygon"]).buffer(-0.01).intersects(door.swing), it["name"]


@pytest.mark.parametrize("w,h,rtype,expect", [(1.5, 2.2, "toilet", True), (1.3, 2.4, "toilet", True), (1.5, 2.0, "toilet", True),
                                              (1.2, 2.4, "toilet", False),  # 4'x8': all three fixtures + clearances do not fit
                                              (0.9, 1.3, "toilet", False), (1.0, 1.2, "toilet", False), (1.0, 1.4, "wc", True)])
def test_toilet_fixtures(w, h, rtype, expect):
    ctx = _single(rtype, w, h, door=((0.1, 0.0), (0.85, 0.0)) if w >= 0.95 else ((0.1, 0.0), (0.8, 0.0)))
    fit = Q._fit_toilet(ctx, rtype, rtype)
    assert (fit.tier != "none") == expect, (w, h, fit.tier)


def test_kitchen_counter_run_and_aisle():
    ok = Q._fit_kitchen(_single("kitchen", 2.4, 3.2, window=(2.4, 1.0, 2.4, 2.2)), "kitchen")
    assert ok.tier == "full" and "counter run" in ok.note
    tiny = Q._fit_kitchen(_single("kitchen", 1.5, 1.6), "kitchen")
    assert tiny.tier in ("none", "tight")  # < 2.4 m of counter once the door is kept clear


def test_living_needs_tv_wall_and_sofa():
    big = Q._fit_living(_single("living", 4.2, 4.8), "living")
    assert big.tier in ("full", "tight") and {i["name"] for i in big.items} == {"tv unit", "sofa 3-seat"}
    small = Q._fit_living(_single("living", 2.6, 2.7), "living")
    assert small.tier == "none"


def test_dining_clearance():
    assert Q._fit_dining(_single("dining", 3.0, 3.0), "dining", 4).tier in ("full", "tight")
    assert Q._fit_dining(_single("dining", 2.0, 2.0), "dining", 4).tier == "none"


# ------------------------------------------------------------------------------------------------ calibration data


def test_thresholds_have_sources_and_valid_ramps():
    for k, m in Q.METRICS.items():
        assert m.source and m.what and m.cat in Q.CATEGORIES, k
        assert m.good != m.bad
        assert Q._ramp(m.good, m.good, m.bad) == 1.0 and Q._ramp(m.bad, m.good, m.bad) == 0.0
    for k in Q.GATE:
        assert k in Q.METRICS


def test_approved_stats_match_the_thresholds_they_calibrate():
    from pathlib import Path

    p = Path(__file__).resolve().parents[2] / "data" / "mined" / "topology_v1.jsonl.gz"
    if not p.exists():
        pytest.skip("mined data not present")
    s = Q.approved_stats(str(p))
    assert s["flats"] > 500
    assert s["circ_share_pct"][-1] <= Q.METRICS["circ_share"].good  # p90 of approved flats is within "good"
    assert s["entrance_type_share"]["living"] > 0.9
    assert s["kitchen_touches_living_dining_foyer"] > 0.9


# ------------------------------------------------------------------------------------------------ integration + render


def test_render_png(tmp_path):
    pytest.importorskip("matplotlib")
    from planner_engine.render import plan_png

    res = Q.critique(good_plan())
    out = plan_png(good_plan(), tmp_path / "good.png", furniture=True, issues=res)
    assert out.exists() and out.stat().st_size > 5000
    bad = build(GOOD_ROOMS, [d for d in GOOD_DOORS if d[0] != "D-entry"] + [("D-entry", (8.6, 5.0), (8.6, 6.0), ["bed2"], "bed2")], GOOD_WINDOWS)
    out2 = plan_png(bad, tmp_path / "bad.png", issues=Q.critique(bad))
    assert out2.exists()
    out3 = plan_png(good_plan(), tmp_path / "nofurn.png", furniture=False)
    assert out3.exists() and out3.stat().st_size > 1000


def test_real_solved_flat_is_critiqued():
    from planner_engine.layout import solve_flat
    from planner_engine.models import PlateFootprint
    from planner_engine.plate import fit
    from planner_engine.units import split_units

    fp = PlateFootprint(id="T1", polygon=[], angle_deg=0, center=(0, 0), width=40, depth=22, area=880)
    plate = fit("cruciform", fp)
    unit = split_units(plate)[0]
    fr = solve_flat(plate, unit)
    assert fr.plan is not None
    res = Q.critique(fr.plan, unit, plate)
    assert 0 <= res["score"] <= 100 and not res["errors"]
    assert "efficiency" in res["metrics"] and "travel_to_exit" in res["metrics"]
    assert res["furniture"]
    again = Q.critique(copy.deepcopy(fr.plan), unit, plate)
    assert again["score"] == res["score"]
