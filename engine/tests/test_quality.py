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
    assert sum(mt.weight for mt in Q.METRICS.values()) == pytest.approx(119.0)  # relative weights (100 before the realism rules)
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
                                              (1.2, 2.4, "toilet", True),  # 4'x8': fits in the compact tier (approved flats have 2.0-2.4 m2 toilets)
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


# ------------------------------------------------------------------------------------------------ realism rules (generator-v2 review)


def swap(rooms, name, box):
    return [(r[0], r[1], box) if r[0] == name else r for r in rooms]


def test_good_plan_has_none_of_the_realism_defects():
    res = Q.critique(good_plan())
    for k in ("entry_privacy", "entry_sight", "zone_order", "toilet_door", "dining_kitchen", "orphan"):
        assert res["metrics"][k]["value"] == (0 if k == "orphan" else 1.0), k
    assert not codes(res) & {"bedroom_at_entry", "bedroom_in_sight", "zone_order", "toilet_door_public", "toilet_to_dining",
                             "dining_isolated", "orphan_pocket", "wet_duct", "kitchen_far", "kitchen_sliver"}


def test_rule1_bedroom_opening_off_the_entry_foyer_or_beside_the_front_door():
    rooms = GOOD_ROOMS + [("foyer", "foyer", (3.4, -2.0, 5.0, 0)), ("bed3", "bedroom", (5.0, -2.0, 8.6, 0))]
    doors = [d for d in GOOD_DOORS if d[0] != "D-entry"] + [
        ("D-entry", (3.7, -2.0), (4.7, -2.0), ["foyer"], "foyer"), ("DF1", (3.7, 0.0), (4.6, 0.0), ["foyer", "living"], "living"),
        ("DF2", (5.0, -1.5), (5.0, -0.6), ["foyer", "bed3"], "bed3")]
    res = Q.critique(build(rooms, doors, GOOD_WINDOWS + [("bed3", 8.6, -1.6, 8.6, -0.4)]))
    bad = [i for i in res["issues"] if i["code"] == "bedroom_at_entry"]
    assert bad and bad[0]["severity"] == "major" and "bed3" in bad[0]["rooms"]
    assert res["metrics"]["entry_privacy"]["value"] <= 0.3 + 1e-9
    ok = Q.critique(build(GOOD_ROOMS + [("foyer", "foyer", (3.4, -2.0, 5.0, 0))], doors[:-2] + [doors[-2]], GOOD_WINDOWS))
    assert ok["metrics"]["entry_privacy"]["value"] == 1.0 and "bedroom_at_entry" not in codes(ok)
    # a bedroom wall right at the front door, even with its door elsewhere (door into the dining room)
    rooms2 = GOOD_ROOMS + [("bed3", "bedroom", (5.0, -3.0, 8.6, 0))]
    doors2 = GOOD_DOORS + [("DB", (6.0, 0.0), (6.9, 0.0), ["dining", "bed3"], "bed3")]
    r2 = Q.critique(build(rooms2, doors2, GOOD_WINDOWS))
    assert any(i["code"] == "bedroom_at_entry" and i["severity"] == "minor" for i in r2["issues"])
    assert 0.5 < r2["metrics"]["entry_privacy"]["value"] < 1.0
    assert r2["score"] < Q.critique(good_plan())["score"]


def test_rule2_attached_toilet_must_open_into_its_bedroom():
    doors = [d for d in GOOD_DOORS if d[0] != "D7"] + [("D7", (0.4, 8.2), (1.15, 8.2), ["dining", "toilet_m"], "toilet_m")]
    res = Q.critique(build(GOOD_ROOMS, doors, GOOD_WINDOWS))
    bad = [i for i in res["issues"] if i["code"] == "toilet_door_public"]
    assert bad and bad[0]["severity"] == "major" and "toilet_m" in bad[0]["rooms"]
    assert res["metrics"]["toilet_door"]["value"] < 0.5
    assert res["score"] < Q.critique(good_plan())["score"] - 5


def test_rule2_common_toilet_into_dining_or_kitchen():
    for room, code in (("dining", "toilet_to_dining"), ("kitchen", "toilet_to_kitchen")):
        doors = [d for d in GOOD_DOORS if d[0] != "D6"] + [("D6", (3.7, 6.4), (4.45, 6.4), [room, "toilet_c"], "toilet_c")]
        res = Q.critique(build(GOOD_ROOMS, doors, GOOD_WINDOWS))
        assert code in codes(res), code
        assert res["metrics"]["toilet_door"]["value"] <= 0.5 + 1e-9
    # the powder room off the living room is the approved pattern: no finding
    doors = [d for d in GOOD_DOORS if d[0] != "D6"] + [("D6", (3.7, 6.4), (4.45, 6.4), ["living", "toilet_c"], "toilet_c")]
    assert Q.critique(build(GOOD_ROOMS, doors, GOOD_WINDOWS))["metrics"]["toilet_door"]["value"] == 1.0


def test_rule3_kitchen_must_share_a_wall_or_opening_with_dining_or_living():
    rooms = swap(swap(GOOD_ROOMS, "kitchen", (5.0, 4.6, 8.6, 8.2)), "bed2", (5.0, 2.8, 8.6, 4.6))
    doors = [d for d in GOOD_DOORS if d[0] not in ("D2", "D5")] + [("D2", (5.5, 4.6), (6.4, 4.6), ["kitchen", "bed2"], "kitchen"),
                                                                  ("D5", (5.0, 5.0), (5.0, 5.9), ["hub", "kitchen"], "kitchen")]
    res = Q.critique(build(rooms, doors, GOOD_WINDOWS))
    assert "kitchen_far" in codes(res)
    assert res["metrics"]["kitchen_near"]["value"] <= 0.4
    # a sliver of shared wall (under 1 m) is not contact either
    rooms = swap(GOOD_ROOMS, "kitchen", (4.2, 4.6, 7.8, 6.4))
    rooms = swap(rooms, "bed2", (5.0, 6.4, 8.6, 9.0))
    doors = [d for d in GOOD_DOORS if d[0] not in ("D2", "D5")] + [("D2", (5.0, 5.0), (5.0, 5.9), ["hub", "kitchen"], "kitchen"),
                                                                  ("D5", (5.3, 6.4), (6.2, 6.4), ["kitchen", "bed2"], "bed2")]
    res = Q.critique(build(rooms, doors, GOOD_WINDOWS))
    assert "kitchen_sliver" in codes(res) and res["metrics"]["kitchen_near"]["value"] <= 0.5


def test_rule4_orphan_pockets():
    rooms = GOOD_ROOMS + [("store1", "store", (-2.5, 0, 0, 2.0)), ("pass2", "passage", (8.6, 4.6, 9.8, 8.2))]
    doors = GOOD_DOORS + [("DP", (8.6, 5.0), (8.6, 5.9), ["bed2", "pass2"], "pass2")]
    res = Q.critique(build(rooms, doors, GOOD_WINDOWS))
    why = {i["rooms"][0]: i["message"] for i in res["issues"] if i["code"] == "orphan_pocket"}
    assert "has no door" in why["store1"] and "connects nothing" in why["pass2"]
    assert res["metrics"]["orphan"]["value"] == 2 and res["metrics"]["orphan"]["satisfaction"] == 0.0
    assert res["score"] < Q.critique(good_plan())["score"] - 10
    # outside the flat outline (when the unit polygon is known): a passage that lies beyond it
    from planner_engine.models import Unit

    unit = Unit(id="U", polygon=[(0, 0), (8.6, 0), (8.6, 9.8), (0, 9.8)], area=84.0, core_link=(4.3, 0.0))
    rooms = GOOD_ROOMS + [("pass3", "passage", (12.0, 0, 14.0, 2.0))]
    doors = GOOD_DOORS + [("DQ", (12.0, 0.5), (12.0, 1.4), ["dining", "pass3"], "pass3"), ("DQ2", (12.5, 2.0), (13.4, 2.0), ["pass3", "kitchen"], "pass3")]
    res = Q.critique(build(rooms, doors, GOOD_WINDOWS), unit)
    assert any("outside the flat outline" in i["message"] for i in res["issues"] if i["code"] == "orphan_pocket")


def test_rule5_wet_rooms_on_ducts_are_counted():
    plan = good_plan()
    for r in plan.rooms:
        if r.type == "toilet":
            r.ventilated, r.vent_via = False, "duct"
    res = Q.critique(plan)
    assert res["metrics"]["wet_duct"]["value"] == 2 and "wet_duct" in codes(res)
    assert res["metrics"]["wet_duct"]["satisfaction"] == pytest.approx(1 / 3, abs=0.01)
    assert res["score"] < Q.critique(good_plan())["score"] - 3
    assert "wet_duct" not in Q.critique(good_plan())["metrics"] or Q.critique(good_plan())["metrics"]["wet_duct"]["value"] == 0


def test_rule6_zone_order_and_bedroom_in_the_line_of_sight():
    # bedroom door straight ahead of the front door: entry -> living -> hub -> bed3
    rooms = [(r[0], "bedroom", r[2]) if r[0] == "toilet_c" else r for r in GOOD_ROOMS]
    doors = [d for d in GOOD_DOORS]
    res = Q.critique(build(rooms, doors, GOOD_WINDOWS))
    sight = [i for i in res["issues"] if i["code"] == "bedroom_in_sight"]
    assert sight and sight[0]["severity"] == "major" and sight[0]["rooms"] == ["toilet_c"] and sight[0]["line"]
    assert res["metrics"]["entry_sight"]["value"] <= 0.3 + 1e-9
    assert Q.critique(good_plan())["metrics"]["entry_sight"]["value"] == 1.0
    # zone order: a bedroom as shallow as the living room (it hangs off the entry foyer)
    rooms = GOOD_ROOMS + [("foyer", "foyer", (3.4, -2.0, 5.0, 0)), ("bed3", "bedroom", (5.0, -2.0, 8.6, 0))]
    doors = [d for d in GOOD_DOORS if d[0] != "D-entry"] + [
        ("D-entry", (3.7, -2.0), (4.7, -2.0), ["foyer"], "foyer"), ("DF1", (3.7, 0.0), (4.6, 0.0), ["foyer", "living"], "living"),
        ("DF2", (5.0, -1.5), (5.0, -0.6), ["foyer", "bed3"], "bed3")]
    res = Q.critique(build(rooms, doors, GOOD_WINDOWS))
    assert "zone_order" in codes(res) and res["metrics"]["zone_order"]["value"] < 1.0
    z = Q.critique(good_plan())["stats"]["zone_depth"]
    assert z["public"] < z["private"]  # living 0 / dining 1 < bedrooms 2


def test_rule7_dining_must_be_on_a_serving_route_to_the_kitchen():
    # no door between them but a shared wall: carried through another room
    doors = [d for d in GOOD_DOORS if d[0] != "D2"] + [("D2", (5.3, 2.8), (6.0, 2.8), ["dining", "kitchen"], "dining")]
    assert Q.critique(build(GOOD_ROOMS, doors, GOOD_WINDOWS))["metrics"]["dining_kitchen"]["value"] == 1.0
    doors = [d for d in GOOD_DOORS if d[0] != "D2"]
    res = Q.critique(build(GOOD_ROOMS, doors, GOOD_WINDOWS))
    assert res["metrics"]["dining_kitchen"]["value"] == 0.7 and "dining_kitchen_link" in codes(res)
    # a dining room reachable only through the master bedroom is cut off from the kitchen
    rooms = swap(GOOD_ROOMS, "dining", (-3.4, 4.6, 0, 8.2))
    doors = [d for d in GOOD_DOORS if d[0] not in ("D1", "D2")] + [("D1", (0.0, 5.0), (0.0, 5.9), ["master", "dining"], "dining"),
                                                                  ("D2", (5.0, 3.2), (5.0, 4.1), ["living", "kitchen"], "kitchen")]
    res = Q.critique(build(rooms, doors, GOOD_WINDOWS))
    assert res["metrics"]["dining_kitchen"]["value"] <= 0.1 + 1e-9 and "dining_isolated" in codes(res)


def test_v3_bedroom_toilet_beside_the_entry_foyer_counts_as_private_zone_at_entry():
    # attached toilet shares a wall with the foyer; no door onto it, the bedroom itself is 1.6 m away from the front door
    rooms = GOOD_ROOMS + [("foyer", "foyer", (3.4, -2.0, 5.0, 0)), ("toilet_b3", "toilet", (5.0, -2.0, 6.6, 0)),
                          ("bed3", "bedroom", (6.6, -2.0, 10.2, 0))]
    doors = [d for d in GOOD_DOORS if d[0] != "D-entry"] + [
        ("D-entry", (3.7, -2.0), (4.7, -2.0), ["foyer"], "foyer"), ("DF1", (3.7, 0.0), (4.6, 0.0), ["foyer", "living"], "living"),
        ("DB", (6.6, -1.5), (6.6, -0.7), ["toilet_b3", "bed3"], "bed3"), ("DB2", (7.5, 0.0), (8.4, 0.0), ["dining", "bed3"], "bed3")]
    res = Q.critique(build(rooms, doors, GOOD_WINDOWS))
    bad = [i for i in res["issues"] if i["code"] == "bedroom_at_entry"]
    assert bad and "bed3" in bad[0]["rooms"] and "foyer" in bad[0]["message"]
    assert res["metrics"]["entry_privacy"]["value"] <= 1.0 - Q.ENTRY_SHARED_PEN + 1e-9
    far = [r if r[0] != "toilet_b3" else ("toilet_b3", "toilet", (7.2, -2.0, 8.8, 0)) for r in rooms]
    far = [r if r[0] != "bed3" else ("bed3", "bedroom", (8.8, -2.0, 12.4, 0)) for r in far]
    far_doors = [d for d in doors if d[0] not in ("DB", "DB2")] + [("DB", (8.8, -1.5), (8.8, -0.7), ["toilet_b3", "bed3"], "bed3"),
                                                                  ("DB2", (8.0, 0.0), (8.6, 0.0), ["dining", "toilet_b3"], "toilet_b3")]
    ok = Q.critique(build(far, far_doors, GOOD_WINDOWS))
    assert "bedroom_at_entry" not in codes(ok) and ok["metrics"]["entry_privacy"]["value"] == 1.0


def test_v3_dining_in_the_far_corner_costs_more_than_one_beside_the_kitchen():
    near = Q.critique(good_plan())["metrics"]["dining_kitchen"]["value"]
    rooms = swap(GOOD_ROOMS, "dining", (-3.6, 0.0, 0, 2.8))
    doors = [d for d in GOOD_DOORS if d[0] not in ("D1", "D2")] + [
        ("D1", (0.0, 0.8), (0.0, 1.7), ["living", "dining"], "dining"), ("D2", (5.0, 3.2), (5.0, 4.1), ["living", "kitchen"], "kitchen")]
    res = Q.critique(build(rooms, doors, GOOD_WINDOWS))
    v = res["metrics"]["dining_kitchen"]["value"]
    assert v < near and v < 0.6 and v >= 0.2  # no shared wall, route over 6 m through the living room
    assert res["score"] < Q.critique(good_plan())["score"]


def test_v3_notched_public_room_costs_more_than_a_rectangle():
    plan = good_plan()
    liv = next(r for r in plan.rooms if r.id == "living")
    liv.polygon = [(0, 0), (5.0, 0), (5.0, 2.3), (2.5, 2.3), (2.5, 4.6), (0, 4.6)]
    liv.area = 17.25
    res = Q.critique(plan)
    old = (Q.PUBLIC_NOTCH_MULT, Q.PUBLIC_NOTCH_WEIGHT)
    try:
        Q.PUBLIC_NOTCH_MULT = Q.PUBLIC_NOTCH_WEIGHT = 1.0
        plain = Q.critique(plan)
    finally:
        Q.PUBLIC_NOTCH_MULT, Q.PUBLIC_NOTCH_WEIGHT = old
    assert res["metrics"]["room_shape"]["value"] < plain["metrics"]["room_shape"]["value"]
    assert res["metrics"]["room_shape"]["value"] < Q.critique(good_plan())["metrics"]["room_shape"]["value"]


def test_hub_spread_limit_grows_with_the_number_of_bedrooms():
    res = Q.critique(good_plan())
    assert res["stats"]["hub_spread_limits"] == [4.5, 8.0]
    plan = good_plan()
    extra = [("bed3", "bedroom", (9.0, 0, 11.0, 3.0))]
    plan2 = build(GOOD_ROOMS + extra, GOOD_DOORS + [("DX", (8.6, 1.0), (8.6, 1.9), ["dining", "bed3"], "bed3")], GOOD_WINDOWS)
    assert Q.critique(plan2)["stats"]["hub_spread_limits"] == [8.5, 14.0]
    assert plan.rooms


def test_compact_toilet_tier_and_imported_plans():
    # 1.2 x 2.2 m (2.6 m2): fits only with the compact tier (0.8 m shower tray, 0.3 m in front of the WC); 1.2 x 2.0 does not
    fit = Q._fit_toilet(_single("toilet", 1.2, 2.2, door=((0.1, 0.0), (0.85, 0.0))), "toilet", "toilet")
    assert fit.tier == "tight" and fit.score == pytest.approx(0.6)
    assert Q._fit_toilet(_single("toilet", 1.2, 2.0, door=((0.1, 0.0), (0.85, 0.0))), "toilet", "toilet").tier == "none"
    assert Q._fit_toilet(_single("toilet", 1.5, 2.4), "toilet", "toilet").tier == "full"
    # imported plans: no door swings in the furniture placement, no dining table fit, excluded metrics removed
    res = Q.critique(good_plan(), exclude=("hab_window", "privacy"), imported=True)
    assert "hab_window" not in res["metrics"] and "privacy" not in res["metrics"]
    assert not [r for r in res["furniture"] if r == "dining"]
    assert res["score"] >= 85


# ------------------------------------------------------------------------------------------------ approved-plan benchmark


def _record():
    """A minimal graph-dataset flat: living (entrance) - dining - kitchen, passage - 2 bedrooms - toilet, in metres."""
    def node(i, t, x0, y0, x1, y1, fac=1.0):
        return {"id": i, "type": t, "name": t, "area": (x1 - x0) * (y1 - y0), "centroid": [(x0 + x1) / 2, (y0 + y1) / 2],
                "polygon": [[x0, y0], [x1, y0], [x1, y1], [x0, y1]], "facade_sides": ["top"], "facade_len": fac,
                "facade_via_balcony": [], "clear_width": min(x1 - x0, y1 - y0), "shaft": False}
    nodes = [node(1, "living", 0, 0, 5, 4.6), node(2, "dining", 5.1, 0, 8.6, 2.8), node(3, "kitchen", 5.1, 2.9, 8.6, 4.6),
             node(4, "passage", 3.4, 4.7, 5, 6.4, 0), node(5, "master", 0, 4.7, 3.3, 8.2), node(6, "bedroom", 5.1, 4.7, 8.6, 8.2),
             node(7, "toilet", 3.4, 6.5, 5, 8.8, 0), node(8, "unknown", 0.2, 8.3, 1.0, 8.9, 0)]
    edges = [{"kind": "open", "a": 1, "b": 2, "point": [5.05, 1.4]}, {"kind": "door", "a": 2, "b": 3, "point": [6.5, 2.85]},
             {"kind": "door", "a": 1, "b": 4, "point": [4.2, 4.65]}, {"kind": "door", "a": 4, "b": 5, "point": [3.35, 5.5]},
             {"kind": "door", "a": 4, "b": 6, "point": [5.05, 5.5]}, {"kind": "door", "a": 4, "b": 7, "point": [4.2, 6.45]},
             {"kind": "wall", "a": 3, "b": 6, "len": 3.5, "thick": 0.15}]
    return {"flat": {"flat_id": "T:p0:F1", "bhk": 2, "nodes": nodes, "edges": edges, "signature": "x",
                     "entrance": {"room": 1, "room_type": "living", "point": [4.3, 0.0], "kind": "door"}}, "project": "T", "page": 0}


def test_approved_bench_converts_a_graph_record_and_scores_it():
    from planner_engine import approved_bench as B

    plan, info = B.convert(_record())
    assert info["unresolved"] == 0 and info["dropped_share"] < 0.08 and B.usable(info)
    types = {r.type for r in plan.rooms}
    assert {"living", "dining", "kitchen", "passage", "master", "bedroom", "toilet"} <= types
    assert len(plan.rooms) == 7  # the 0.5 m2 untyped sliver is dropped
    assert sum(1 for d in plan.doors if d.kind == "entry") == 1 and sum(1 for d in plan.doors if d.kind == "door") == 6
    res = Q.critique(plan, exclude=B.EXCLUDE, imported=True)
    assert not res["errors"] and res["score"] >= 70
    for k in B.EXCLUDE:
        assert k not in res["metrics"]
    for k in ("entry_privacy", "entry_sight", "zone_order", "toilet_door", "orphan"):
        assert k in res["metrics"], k


def test_approved_bench_repairs_a_missing_link_and_rejects_untrustworthy_flats():
    from planner_engine import approved_bench as B

    rec = _record()
    rec["flat"]["edges"] = [e for e in rec["flat"]["edges"] if not (e["a"] == 4 and e["b"] == 6)] + [
        {"kind": "wall", "a": 4, "b": 6, "len": 1.7, "thick": 0.15}]
    plan, info = B.convert(rec)
    assert info["repaired"] >= 1 and info["unresolved"] == 0  # bedroom 6 gets its door to the passage back
    rec2 = _record()
    rec2["flat"]["nodes"].append({**rec2["flat"]["nodes"][5], "id": 9, "polygon": [[20, 20], [23, 20], [23, 23], [20, 23]], "centroid": [21.5, 21.5]})
    plan2, info2 = B.convert(rec2)
    assert info2["unresolved"] >= 1 and not B.usable(info2)


def test_approved_flats_mostly_grade_a_or_b():
    from pathlib import Path

    from planner_engine import approved_bench as B

    if not B.DATA.exists() or not Path(B.DATA).stat().st_size:
        pytest.skip("graph dataset not present")
    import time

    recs = B.stratified_sample(B.load_flats(), n=30, seed=3)
    assert len(recs) >= 25
    grades, worst = [], 0.0
    for _r, (plan, _info) in recs:
        t0 = time.perf_counter()
        res = Q.critique(plan, exclude=B.EXCLUDE, imported=True)
        worst = max(worst, time.perf_counter() - t0)
        grades.append(res["grade"])
        assert not res["errors"]
    assert sum(g in "AB" for g in grades) / len(grades) >= 0.6, grades
    assert "F" not in grades
    assert worst < 1.5  # 1 s budget with a margin for a shared CPU
