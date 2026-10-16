"""Stage 7 orchestration: pre-check -> solve -> (explain -> fixes -> safe default) -> draft.

When a diagram cannot become a plan, the engine lists 2-4 fixes built from the infeasible core, verifies
each one by solving, and applies the safe default (drop optional rooms first, then duct ventilation for
toilets, then a smaller programme, ...). The choice is recorded as a T3 ticket that Claude (or the user)
may change later; the plan always finishes.
"""

from __future__ import annotations

import copy
import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import Any

from .diagram import from_programme, precheck
from .drafting import draft
from .models import Conflict, Diagram, Plan, Plate, Unit
from .solver import SolveResult, room_specs, solve, unit_grid
from .units import facade_length, presets

FIX_ORDER = ["drop_room", "duct", "duct_all", "shrink", "smaller_programme", "remove_link", "no_window"]
_CACHE: dict[str, tuple[SolveResult, dict]] = {}


@dataclass
class FlatResult:
    plan: Plan | None
    diagram: Diagram
    result: SolveResult
    applied_fix: dict | None = None
    fixes: list[dict] = field(default_factory=list)
    conflicts: list[Conflict] = field(default_factory=list)
    message: str = ""


def _key(plate: Plate, unit: Unit, diagram: Diagram, overrides: dict) -> str:
    payload = {
        "unit": unit.polygon, "door": unit.core_link, "zones": [z.model_dump() for z in plate.zones],
        "outline": plate.outline, "rooms": [r.model_dump(exclude={"x", "y"}) for r in diagram.rooms],
        "links": diagram.links, "entry": diagram.entry, "ov": overrides,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


def _names(diagram: Diagram) -> dict[str, str]:
    return {r.id: r.name for r in diagram.rooms}


def _describe(core: list[str], diagram: Diagram, facade: float) -> str:
    names = _names(diagram)
    win = [names.get(c.split(":")[1], c) for c in core if c.startswith("window:")]
    vent = [names.get(c.split(":")[1], c) for c in core if c.startswith("vent:")]
    links = [c.split(":")[1] for c in core if c.startswith("link:")]
    sizes = [names.get(c.split(":")[1], c) for c in core if c.startswith("size:")]
    parts = []
    if win or vent:
        s = ""
        if win:
            s += ", ".join(win) + " need windows"
        if vent:
            s += (" and " if s else "") + ", ".join(vent) + " need fresh air"
        parts.append(s + f" on {facade:.1f} m of façade")
    if sizes:
        parts.append(", ".join(sizes) + " cannot reach their minimum size")
    if links:
        parts.append("links " + ", ".join(l.replace("-", "–") for l in links) + " cannot all share walls")
    if any(c.startswith("entry:") for c in core):
        parts.append("the entry room must meet the flat door")
    return "; ".join(parts) or "The rooms do not fit together in this flat."


def candidate_fixes(core: list[str], diagram: Diagram, overrides: dict, unit: Unit) -> list[dict]:
    rooms = {r.id: r for r in diagram.rooms if r.placed}
    names = _names(diagram)
    fixes: list[dict] = []
    core_rooms = {c.split(":", 1)[1] for c in core if c.split(":")[0] in ("room", "window", "vent", "size", "entry")}
    # 1. drop optional rooms (anywhere: they free façade and area)
    for rid, r in rooms.items():
        if r.optional and overrides.get(rid, {}).get("force") is not False:
            fixes.append({"action": "drop_room", "room": rid, "label": f"Drop {names[rid]}"})
    # 2. toilets in the core: ventilate by duct
    for c in core:
        if c.startswith("vent:"):
            rid = c.split(":")[1]
            fixes.append({"action": "duct", "room": rid, "label": f"{names.get(rid, rid)}: mechanical duct (review)"})
    # 3. shrink the biggest room in the core by 1 m2 (or 2)
    big = sorted((rid for rid in core_rooms if rid in rooms), key=lambda k: -rooms[k].target_area)
    for rid in big[:2]:
        t = float(overrides.get(rid, {}).get("target_area", rooms[rid].target_area))
        fixes.append({"action": "shrink", "room": rid, "by": 1.0, "label": f"Shrink {names[rid]} by 1 m²", "to": round(t - 1, 2)})
    # 4. links in the core
    for c in core:
        if c.startswith("link:"):
            a, b = c.split(":")[1].split("-", 1)
            fixes.append({"action": "remove_link", "a": a, "b": b, "label": f"Unlink {names.get(a, a)} and {names.get(b, b)}"})
    # 4b. no explanation found in time: duct all toilets (common in towers), then drop minor links
    if not core:
        wet = [rid for rid, r in rooms.items() if r.type in ("toilet", "bath", "wc", "utility")
               and overrides.get(rid, {}).get("vent") != "duct"]
        if wet:
            fixes.append({"action": "duct_all", "rooms": wet, "label": "Ventilate toilets by mechanical duct (review)"})
        for a, b in diagram.links:
            ta = rooms.get(a).type if a in rooms else ""
            tb = rooms.get(b).type if b in rooms else ""
            if {ta, tb} & {"balcony", "utility", "dining", "passage"}:
                fixes.append({"action": "remove_link", "a": a, "b": b,
                              "label": f"Unlink {names.get(a, a)} and {names.get(b, b)}"})
    # 5. a smaller programme
    if unit.programme:
        smaller = [p for p in presets().values() if p.bhk == unit.programme.bhk - 1]
        if smaller:
            fixes.append({"action": "smaller_programme", "programme": smaller[0].id, "label": f"Use {smaller[0].name}"})
    order = {k: i for i, k in enumerate(FIX_ORDER)}
    fixes.sort(key=lambda f: order.get(f["action"], 99))
    # de-duplicate
    seen, out = set(), []
    for f in fixes:
        k = json.dumps(f, sort_keys=True)
        if k not in seen:
            seen.add(k)
            out.append(f)
    return out


def apply_fix(fix: dict, diagram: Diagram, overrides: dict, unit: Unit) -> tuple[Diagram, dict, Unit]:
    d = copy.deepcopy(diagram)
    ov = copy.deepcopy(overrides)
    u = unit
    a = fix["action"]
    if a == "drop_room":
        ov.setdefault(fix["room"], {})["force"] = False
    elif a == "duct":
        ov.setdefault(fix["room"], {})["vent"] = "duct"
    elif a == "duct_all":
        for rid in fix["rooms"]:
            ov.setdefault(rid, {})["vent"] = "duct"
    elif a == "shrink":
        ov.setdefault(fix["room"], {})["target_area"] = fix["to"]
    elif a == "remove_link":
        d.links = [l for l in d.links if set(l) != {fix["a"], fix["b"]}]
    elif a == "smaller_programme":
        u = unit.model_copy(deep=True)
        u.programme = presets()[fix["programme"]]
        d = from_programme(u, u.programme)
        ov = {}
    elif a == "no_window":
        ov.setdefault(fix["room"], {})["needs_window"] = False
    return d, ov, u


def _solve(plate: Plate, unit: Unit, diagram: Diagram, overrides: dict, time_limit: float, explain: bool):
    key = _key(plate, unit, diagram, overrides)
    if key in _CACHE:
        return _CACHE[key]
    ug = unit_grid(plate, unit)
    specs = room_specs(diagram, overrides, ug)
    # duct-ventilated rooms drop their façade/shaft requirement
    for s in specs:
        if overrides.get(s.id, {}).get("vent") == "duct":
            s.vent_run = 0
        if overrides.get(s.id, {}).get("needs_window") is False:
            s.window_run = 0
    res = solve(ug, specs, [tuple(l) for l in diagram.links], diagram.entry, time_limit=time_limit, explain=explain)
    out = (res, {"ug": ug, "specs": specs})
    if res.status != "timeout" or res.rects:
        _CACHE[key] = out
    return out


def solve_flat(plate: Plate, unit: Unit, diagram: Diagram | None = None, overrides: dict | None = None,
               time_limit: float = 7.5, auto_fix: bool = True, max_fix_rounds: int = 3) -> FlatResult:
    t0 = time.perf_counter()
    overrides = dict(overrides or {})
    if diagram is None:
        assert unit.programme is not None
        diagram = from_programme(unit, unit.programme)
    fac = facade_length(plate, unit)
    conflicts = precheck(diagram, unit.area, fac, 0.0, unit.core_link is not None)
    res, ctx = _solve(plate, unit, diagram, overrides, time_limit, explain=True)
    applied: dict | None = None
    all_fixes: list[dict] = []
    cur_d, cur_ov, cur_u = diagram, overrides, unit
    rounds = 0
    while not res.rects and auto_fix and rounds < max_fix_rounds:
        rounds += 1
        core = res.core
        conf = Conflict(id=f"solver-{rounds}", rooms=sorted({c.split(":", 1)[1] for c in core}), kind="solver",
                        reason=_describe(core, cur_d, fac))
        fixes = candidate_fixes(core, cur_d, cur_ov, cur_u)
        verified = []
        winner = None
        for f in fixes:
            if time.perf_counter() - t0 > 40:
                break
            d2, ov2, u2 = apply_fix(f, cur_d, cur_ov, cur_u)
            r2, c2 = _solve(plate, u2, d2, ov2, min(time_limit, 4.0), explain=False)
            f = {**f, "valid": bool(r2.rects)}
            verified.append(f)
            if r2.rects and winner is None:
                winner = (f, d2, ov2, u2, r2, c2)
            if len([v for v in verified if v["valid"]]) >= 3:
                break
        conf.fixes = verified
        conflicts.append(conf)
        all_fixes.extend(verified)
        if winner:
            f, cur_d, cur_ov, cur_u, res, ctx = winner
            applied = f if applied is None else {"action": "multi", "steps": [applied, f], "label": applied["label"] + " + " + f["label"]}
            break
        # nothing single worked: apply the first fix and go round again
        if not fixes:
            break
        cur_d, cur_ov, cur_u = apply_fix(fixes[0], cur_d, cur_ov, cur_u)
        applied = fixes[0] if applied is None else {"action": "multi", "steps": [applied, fixes[0]], "label": applied["label"] + " + " + fixes[0]["label"]}
        res, ctx = _solve(plate, cur_u, cur_d, cur_ov, time_limit, explain=True)
    cur_d.conflicts = conflicts
    cur_d.feasible = bool(res.rects)
    if not res.rects:
        cur_d.message = "No layout found. " + (conflicts[-1].reason if conflicts else "")
        return FlatResult(None, cur_d, res, applied, all_fixes, conflicts, cur_d.message)
    plan = draft(ctx["ug"], ctx["specs"], res.rects, [tuple(l) for l in cur_d.links], cur_d.entry, cur_ov)
    plan.status = res.status if res.status in ("optimal", "feasible", "timeout") else "feasible"  # type: ignore[assignment]
    plan.solve_time_s = round(time.perf_counter() - t0, 3)
    plan.objective = res.objective
    cur_d.message = "This diagram can become a plan." if not applied else f"Solved after: {applied['label']}."
    return FlatResult(plan, cur_d, res, applied, all_fixes, conflicts, cur_d.message)


def flat_overrides_from(diagram: Diagram) -> dict[str, Any]:
    return {}
