"""Stage 8b - run the DCPR 2034 and NBC 2016 Part 4 rule files against a plan.

Every result stores the measured value, the threshold, the clause, and the proof (room ids / DXF entity
handles) plus a bounding box so the UI can zoom to it. Results within a clause's ``review_band`` of the
threshold are "review", never silently "pass".
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from shapely.geometry import Point as SPoint, Polygon

from . import rules
from .models import CheckResult, CheckSummary, Plan, PlanRoom

DISCLAIMER = "Automated pre-check. Confirm with the project's licensed consultant."


@dataclass
class FloorContext:
    plans: list[Plan]
    height: float
    passage_width: float | None = None
    stairs: list[tuple[list[tuple[float, float]], float]] = field(default_factory=list)  # (polygon, flight width)
    handles: dict[str, list[str]] = field(default_factory=dict)  # room/unit id -> DXF handles
    ducts: dict[str, bool] = field(default_factory=dict)  # room id -> ventilated by duct


def _cmp(op: str, v: float, t: float) -> bool:
    return {">=": v >= t - 1e-9, "<=": v <= t + 1e-9, ">": v > t, "<": v < t, "==": abs(v - t) < 1e-9}[op]


def _grade(c: rules.Clause, value: float, threshold: float) -> str:
    ok = _cmp(c.op, value, threshold)
    if not ok:
        return "fail"
    if c.review_band and abs(value - threshold) <= c.review_band:
        return "review"
    return "pass"


def _bbox(poly: list[tuple[float, float]]) -> tuple[float, float, float, float]:
    xs = [p[0] for p in poly]
    ys = [p[1] for p in poly]
    return (round(min(xs), 3), round(min(ys), 3), round(max(xs), 3), round(max(ys), 3))


def _room_result(c: rules.Clause, room: PlanRoom, ctx: FloorContext) -> CheckResult | None:
    proof = ctx.handles.get(room.id, [room.id])
    base = dict(rule_id=c.id, clause=c.clause, source=c.code, title=c.title, target=f"{room.unit_id}/{room.id}",
                unit=c.unit, proof=proof, bbox=_bbox(room.polygon))
    if c.metric == "area":
        g = _grade(c, room.area, float(c.threshold))
        return CheckResult(**base, result=g, value=room.area, threshold=float(c.threshold),
                           message=f"{room.name}: {room.area:.2f} m²")
    if c.metric == "width":
        g = _grade(c, room.width, float(c.threshold))
        return CheckResult(**base, result=g, value=room.width, threshold=float(c.threshold),
                           message=f"{room.name}: clear width {room.width:.2f} m")
    if c.metric == "area_and_width":
        ta, tw = float(c.threshold[0]), float(c.threshold[1])
        ga, gw = _grade(c, room.area, ta), _grade(c, room.width, tw)
        g = "fail" if "fail" in (ga, gw) else "review" if "review" in (ga, gw) else "pass"
        return CheckResult(**base, result=g, value=f"{room.area:.2f} m² / {room.width:.2f} m",
                           threshold=f"{ta} m² / {tw} m", message=f"{room.name}")
    if c.metric == "window_ratio":
        ratio = room.window_area / room.area if room.area > 0 else 0.0
        g = _grade(c, ratio, float(c.threshold))
        return CheckResult(**base, result=g, value=round(ratio, 4), threshold=float(c.threshold),
                           message=f"{room.name}: openings {room.window_area:.2f} m² for {room.area:.2f} m²")
    if c.metric == "ventilated":
        if room.ventilated:
            g = "pass"
        elif ctx.ducts.get(room.id) or room.vent_via == "duct":
            g = "review"
        else:
            g = "fail"
        msg = "opens to façade or shaft" if room.ventilated else ("mechanical duct - confirm" if g == "review" else "no ventilation")
        return CheckResult(**base, result=g, value="yes" if room.ventilated else ("duct" if g == "review" else "no"),
                           threshold="yes", message=f"{room.name}: {msg}")
    return None


def _entry_point(plan: Plan) -> tuple[float, float] | None:
    for d in plan.doors:
        if d.kind == "entry":
            return ((d.a[0] + d.b[0]) / 2, (d.a[1] + d.b[1]) / 2)
    return None


def run_checks(ctx: FloorContext) -> CheckSummary:
    out: list[CheckResult] = []
    env = {"height": ctx.height}
    for c in rules.clauses():
        if not c.applies(env):
            out.append(CheckResult(rule_id=c.id, clause=c.clause, source=c.code, title=c.title, target="building",
                                   result="n/a", value=None, threshold=None, message=f"Applies when {c.when}"))
            continue
        if c.scope == "room":
            for plan in ctx.plans:
                for room in plan.rooms:
                    if c.types and room.type not in c.types:
                        continue
                    if c.id == "DCPR-37-1-b" and room == max(
                        (r for r in plan.rooms if r.type in c.types), key=lambda r: r.area, default=None
                    ):
                        continue  # the largest habitable room is tested by 37(1)(a)
                    r = _room_result(c, room, ctx)
                    if r:
                        out.append(r)
        elif c.scope == "unit":
            for plan in ctx.plans:
                proof = ctx.handles.get(plan.unit_id, [plan.unit_id])
                allpts = [p for r in plan.rooms for p in r.polygon]
                bb = _bbox(allpts) if allpts else None
                base = dict(rule_id=c.id, clause=c.clause, source=c.code, title=c.title, target=plan.unit_id,
                            unit=c.unit, proof=proof, bbox=bb)
                if c.metric == "max_area":
                    rooms_t = [r for r in plan.rooms if r.type in c.types]
                    v = max((r.area for r in rooms_t), default=0.0)
                    out.append(CheckResult(**base, result=_grade(c, v, float(c.threshold)), value=round(v, 2),
                                           threshold=float(c.threshold), message=f"Largest habitable room {v:.2f} m²"))
                elif c.metric == "entry_door_width":
                    w = max((d.width for d in plan.doors if d.kind == "entry"), default=0.0)
                    out.append(CheckResult(**base, result=_grade(c, w, float(c.threshold)), value=round(w, 3),
                                           threshold=float(c.threshold), message="Flat entrance door"))
                elif c.metric == "travel_distance":
                    ep = _entry_point(plan)
                    if ep is None or not ctx.stairs:
                        out.append(CheckResult(**base, result="review", value=None, threshold=float(c.threshold),
                                               message="Entry door or staircase not found - measure manually"))
                        continue
                    best = math.inf
                    for poly, _w in ctx.stairs:
                        sp = Polygon(poly)
                        # walking path along corridors ~ Manhattan distance to the stair enclosure
                        nearest = sp.exterior.interpolate(sp.exterior.project(SPoint(ep)))
                        best = min(best, abs(nearest.x - ep[0]) + abs(nearest.y - ep[1]))
                    out.append(CheckResult(**base, result=_grade(c, best, float(c.threshold)), value=round(best, 2),
                                           threshold=float(c.threshold),
                                           message="From the flat entrance door to the nearest staircase"))
        else:  # floor / building
            base = dict(rule_id=c.id, clause=c.clause, source=c.code, title=c.title, target="floor", unit=c.unit,
                        proof=ctx.handles.get("floor", []))
            if c.metric == "passage_width":
                if ctx.passage_width is None:
                    out.append(CheckResult(**base, result="review", value=None, threshold=float(c.threshold),
                                           message="Passage width not measured"))
                else:
                    out.append(CheckResult(**base, result=_grade(c, ctx.passage_width, float(c.threshold)),
                                           value=ctx.passage_width, threshold=float(c.threshold),
                                           message="Common passage clear width"))
            elif c.metric == "stair_count":
                n = len(ctx.stairs)
                out.append(CheckResult(**base, result=_grade(c, n, float(c.threshold)), value=n,
                                       threshold=float(c.threshold), message=f"{n} staircase(s)"))
            elif c.metric == "stair_width":
                if not ctx.stairs:
                    out.append(CheckResult(**base, result="fail", value=0.0, threshold=float(c.threshold),
                                           message="No staircase found"))
                else:
                    w = min(wd for _, wd in ctx.stairs)
                    out.append(CheckResult(**base, result=_grade(c, w, float(c.threshold)), value=round(w, 3),
                                           threshold=float(c.threshold), message="Narrowest stair flight"))
            elif c.metric == "advisory":
                out.append(CheckResult(**base, result="review", value=None, threshold=None,
                                       message=c.advice or c.title))
    counts = {k: sum(1 for r in out if r.result == k) for k in ("pass", "fail", "review", "n/a")}
    by_code: dict[str, dict[str, int]] = {}
    # per-clause roll-up (a clause passes when all of its targets pass), as "DCPR 7/7"
    for code in sorted({r.source for r in out}):
        ids = sorted({r.rule_id for r in out if r.source == code})
        roll = {"clauses": len(ids), "pass": 0, "fail": 0, "review": 0, "n/a": 0}
        for rid in ids:
            res = {r.result for r in out if r.rule_id == rid}
            k = "fail" if "fail" in res else "review" if "review" in res else "pass" if "pass" in res else "n/a"
            roll[k] += 1
        by_code[code] = roll
    return CheckSummary(results=out, counts=counts, by_code=by_code, disclaimer=DISCLAIMER)
