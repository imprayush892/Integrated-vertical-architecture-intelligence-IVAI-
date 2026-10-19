"""Stage 7 orchestration: pre-check -> solve -> (explain -> fixes -> safe default) -> draft.

When a diagram cannot become a plan, the engine lists 2-4 fixes built from the infeasible core, verifies
each one by solving, and applies the safe default (drop optional rooms first, then duct ventilation for
toilets, then a smaller programme, ...). The choice is recorded as a T3 ticket that Claude (or the user)
may change later; the plan always finishes.
"""

from __future__ import annotations

import copy
import dataclasses
import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import Any

from . import casebase
from .checks import FloorContext, run_checks
from .diagram import flex_links, from_programme, precheck, zone_programme
from .drafting import draft
from .models import Conflict, Diagram, Plan, Plate, Unit
from .solver import TIME_CAP, SolveResult, room_specs, solve, unit_grid
from .units import facade_length, presets

FIX_ORDER = ["drop_room", "duct", "duct_all", "shrink", "smaller_programme", "remove_link", "no_window"]
_CACHE: dict[str, tuple[SolveResult, dict]] = {}
FLAT_BUDGET = 9.5  # s: wall clock for one flat - first solve, explanation, every fix and validation attempt, drafting.
# Product limit is 10 s per flat; nothing in solve_flat may run past this deadline.
HARD_CEILING = 30.0  # s: only two paths may run past FLAT_BUDGET, never past this: a first search that timed out
# without finding (or disproving) a layout, and a plan that failed a DCPR room check. Slow is fine; wrong is not.
DRAFT_RESERVE = 0.5  # s kept back for drafting and checking the plan once a layout exists
MIN_ATTEMPT = 0.5  # s: a solve attempt shorter than this cannot find a layout, so the ladder stops


@dataclass
class FlatResult:
    plan: Plan | None
    diagram: Diagram
    result: SolveResult
    applied_fix: dict | None = None
    fixes: list[dict] = field(default_factory=list)
    conflicts: list[Conflict] = field(default_factory=list)
    message: str = ""
    validation: dict = field(default_factory=dict)  # validated/failed_checks/retries/extended
    case_id: str | None = None  # v4: approved case whose arrangement the plan follows (None: the v3 layout won)
    candidates: list[dict] = field(default_factory=list)  # v4: [{"case_id", "score"}] of the layouts the critic compared


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
        # v2: the solver already leaves out optional rooms that do not fit; only rooms the user forced are worth a try
        if r.optional and overrides.get(rid, {}).get("force") is True:
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


HABITABLE = ("living", "master", "bedroom", "study", "kitchen")
# tighter-allowance levels for the re-solve after a failed check: (wmin cells, amin m2) added to the rooms
TIGHTEN = [(2, 0.5), (4, 1.0), (6, 1.5)]


def room_check_fails(plan: Plan, plate: Plate) -> list[tuple[str, str, str]]:
    """DCPR room-scope checks run on a drafted plan: [(rule_id, room_id, message)] for every fail.

    Same checks.py engine the release gate uses, so a plan that passes here passes the gate."""
    stairs = [(c.polygon, c.width) for c in plate.core_parts if c.kind == "stair"]
    sm = run_checks(FloorContext(plans=[plan], height=24, passage_width=plate.passage_width, stairs=stairs))
    return [(r.rule_id, r.target.split("/", 1)[1], r.message) for r in sm.results
            if r.result == "fail" and r.rule_id.startswith("DCPR") and "/" in r.target]


def _solve(plate: Plate, unit: Unit, diagram: Diagram, overrides: dict, time_limit: float, explain: bool,
           tight: tuple[int, float, frozenset | None] | None = None, seed: int = 7, case: dict | None = None):
    key = _key(plate, unit, diagram, overrides) + (repr((tight[0], tight[1], sorted(tight[2] or ())) if tight else None))
    key += f"|seed={seed}"
    if case is not None:
        key += f"|case={case['case']['id']}|m={case['mirror']}"
    if key in _CACHE:
        return _CACHE[key]
    ug = unit_grid(plate, unit)
    fac_len = sum((e.b - e.a) * 0.1 for e in ug.edges if e.kind == "facade")
    diagram = zone_programme(diagram, ug.interior.area, overrides, unit, fac_len)  # v2 brief: entry, hub, extra rooms
    specs = room_specs(diagram, overrides, ug)
    # duct-ventilated rooms drop their façade/shaft requirement
    for s in specs:
        if overrides.get(s.id, {}).get("vent") == "duct":
            s.vent_run = 0
        if overrides.get(s.id, {}).get("needs_window") is False:
            s.window_run = 0
        if tight and (tight[2] is None or s.type in tight[2]):
            s.wmin += tight[0]  # tighter allowance: drafting may take more off a room than the solver assumed
            s.amin += tight[1]
    fixed, alts = flex_links(diagram)
    guide = None
    if case is not None:  # v4: the approved case's arrangement as soft targets (casebase.adapt)
        guide = casebase.adapt(case["case"], ug, specs, [tuple(l) for l in diagram.links], case["mirror"])
    res = solve(ug, specs, fixed, diagram.entry, time_limit=time_limit, explain=explain, alts=alts, seed=seed, guide=guide)
    if res.door_along is not None and ug.door is not None:  # v3: draft the front door where the solver put it
        ug = dataclasses.replace(ug, door=(ug.door[0], res.door_along))
    out = (res, {"ug": ug, "specs": specs, "diagram": diagram, "case_id": guide["case_id"] if guide else None})
    if res.status != "timeout" or res.rects:
        _CACHE[key] = out
    return out


CANDIDATES = 2  # v2: layouts solved per flat (different seeds), the critic picks one
PICK_RESERVE = 1.0  # s kept for drafting and critiquing the candidates
CASE_K = 3  # v4: approved cases tried per flat (casebase.retrieve), next to the v3 layout
V3_SHARE = 0.4  # v4: share of the solving time the unguided v3 layout gets; the cases split the rest
MIN_CASE = 0.8  # s: a guided solve shorter than this is not started
SCORE_RESERVE = 0.35  # v4: s per candidate to draft it and run the full critic (furniture included) on it


def _score(plate: Plate, unit: Unit, overrides: dict, res: SolveResult, ctx: dict, furniture: bool = False) -> float:
    """Draft a candidate and score it with the critic (recorded as ``ctx["pick_score"]``). Without the furniture pass
    (v2/v3 speed path) a room below its furniture size costs the candidate 10 points instead."""
    from .quality import critique

    if "pick_score" in ctx and ctx.get("pick_furniture") == furniture:
        return ctx["pick_score"]
    zd = ctx["diagram"]
    links = res.links if res.links is not None else [tuple(l) for l in zd.links]
    try:
        plan = draft(ctx["ug"], ctx["specs"], res.rects, links, res.entry or zd.entry, overrides, hub_point=res.hub_point)
        sc = critique(plan, unit, plate, furniture=furniture)["score"]
        if not furniture:
            for sp in ctx["specs"]:
                if sp.id in res.rects and (sp.wfurn > sp.wmin or sp.afurn > sp.amin):
                    _x, _y, w, h = res.rects[sp.id]
                    if min(w, h) < sp.wfurn or w * h - 1.15 * (w + h) < 100 * sp.afurn - 1.32:
                        sc -= 10.0
    except Exception:  # pragma: no cover - a candidate that cannot be drafted is not chosen
        sc = -1.0
    ctx["pick_score"], ctx["pick_furniture"] = round(sc, 2), furniture
    return sc


def _pick(plate: Plate, unit: Unit, overrides: dict, cands: list, furniture: bool = False):
    """Keep the candidate the critic scores highest (first one on a tie)."""
    best, best_s = cands[0], -2.0
    for res, ctx in cands:
        sc = _score(plate, unit, overrides, res, ctx, furniture)
        if sc > best_s:
            best, best_s = (res, ctx), sc
    return best


# ---------------------------------------------------------------- persistent plan cache (same input -> same plan)
# CP-SAT's parallel search is time-sliced, so two solves of the same flat can differ. The deterministic modes we
# measured (interleaved search / deterministic time) overshoot their budget on these models and give worse plans,
# so instead every solved flat is stored by an input hash plus a fingerprint of the generator code and data: the
# second time a flat is asked for - in any session - it comes back identical. PLANNER_PLAN_CACHE=0 turns it off.
_CODE_FP: str | None = None


def _code_fingerprint() -> str:
    global _CODE_FP
    if _CODE_FP is None:
        from importlib import resources
        h = hashlib.sha256()
        root = resources.files("planner_engine")
        for name in ("solver.py", "layout.py", "drafting.py", "diagram.py", "casebase.py", "quality.py", "units.py"):
            f = root.joinpath(name)
            if f.is_file():
                h.update(f.read_bytes())
        for sub in ("data/programmes", "data/rules"):
            d = root.joinpath(sub)
            if d.is_dir():
                for f in sorted(d.iterdir(), key=lambda x: x.name):
                    if f.is_file():
                        h.update(f.name.encode() + f.read_bytes())
        _CODE_FP = h.hexdigest()[:16]
    return _CODE_FP


def _cache_db():
    import os
    import sqlite3
    from pathlib import Path

    if os.environ.get("PLANNER_PLAN_CACHE", "1") == "0":
        return None
    d = Path(os.environ.get("PLANNER_HOME", Path.home() / ".apartment-planner"))
    try:
        d.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(d / "plan_cache.sqlite", timeout=5)
        db.execute("CREATE TABLE IF NOT EXISTS flats (k TEXT PRIMARY KEY, v BLOB)")
        return db
    except Exception:  # noqa: BLE001 - a broken cache must never stop a solve
        return None


def solve_flat(plate: Plate, unit: Unit, diagram: Diagram | None = None, overrides: dict | None = None,
               time_limit: float = TIME_CAP, auto_fix: bool = True, max_fix_rounds: int = 4,
               budget: float = FLAT_BUDGET) -> FlatResult:
    """Cached front of :func:`_solve_flat`: an identical request returns the identical stored plan."""
    import pickle

    d0 = diagram if diagram is not None else (from_programme(unit, unit.programme) if unit.programme else None)
    key = None
    db = _cache_db() if d0 is not None else None
    if db is not None:
        key = hashlib.sha256(json.dumps({
            "k": _key(plate, unit, d0, dict(overrides or {})), "fp": _code_fingerprint(), "tl": time_limit,
            "fix": auto_fix, "rounds": max_fix_rounds, "budget": budget,
            "prog": unit.programme.model_dump() if unit.programme else None}, sort_keys=True, default=str).encode()).hexdigest()
        try:
            row = db.execute("SELECT v FROM flats WHERE k = ?", (key,)).fetchone()
            if row:
                return pickle.loads(row[0])
        except Exception:  # noqa: BLE001
            pass
    fr = _solve_flat(plate, unit, diagram, overrides, time_limit, auto_fix, max_fix_rounds, budget)
    if db is not None and key is not None and (fr.plan is not None or fr.result.status == "infeasible"):
        try:
            db.execute("INSERT OR REPLACE INTO flats (k, v) VALUES (?, ?)", (key, pickle.dumps(fr)))
            db.commit()
        except Exception:  # noqa: BLE001
            pass
    if db is not None:
        db.close()
    return copy.deepcopy(fr)


def _solve_flat(plate: Plate, unit: Unit, diagram: Diagram | None = None, overrides: dict | None = None,
                time_limit: float = TIME_CAP, auto_fix: bool = True, max_fix_rounds: int = 4,
                budget: float = FLAT_BUDGET) -> FlatResult:
    """Solve, draft and VALIDATE a flat inside ``budget`` seconds of wall time (fix ladder and validation
    retries included; ``time_limit`` caps a single CP-SAT run). A plan that fails a DCPR room check is never
    returned: the flat is re-solved with a tighter allowance / further fixes while budget remains, else it
    comes back infeasible."""
    t0 = time.perf_counter()
    limit = [budget]  # the validation-failure path may stretch this to HARD_CEILING; nothing else does

    def remaining() -> float:
        """Seconds a solve may still use, leaving room to draft and check the plan."""
        return t0 + limit[0] - DRAFT_RESERVE - time.perf_counter()

    def tl(x: float) -> float:
        return max(0.2, min(x, remaining()))

    overrides = dict(overrides or {})
    if diagram is None:
        assert unit.programme is not None
        diagram = from_programme(unit, unit.programme)
    fac = facade_length(plate, unit)
    conflicts = precheck(diagram, unit.area, fac, 0.0, unit.core_link is not None)
    two = CANDIDATES > 1 and budget >= 8.0 and time_limit >= 6.0
    hits: list[dict] = []
    if two and CASE_K:
        bhk = sum(1 for r in diagram.rooms if r.placed and r.type in ("master", "bedroom"))
        try:
            hits = casebase.retrieve(unit, None, CASE_K, plate=plate, bhk=bhk)
        except Exception:  # pragma: no cover - a broken case library must not stop the v3 generator
            hits = []
    cand_log: list[dict] = []
    if hits:
        # v4: the v3 layout and the same brief guided by each retrieved approved case, in equal time slices; each
        # candidate is drafted and scored by the full critic, and the best one is kept. A proven-infeasible brief stops
        # the loop (the cases add only soft costs); a v3 search that timed out empty still lets the cases try.
        n = 1 + len(hits)
        per = (budget - DRAFT_RESERVE - n * SCORE_RESERVE) / n
        res, ctx = _solve(plate, unit, diagram, overrides, tl(min(time_limit, per)), explain=True)
        cands = [(res, ctx)] if res.rects else []
        if cands:
            _score(plate, unit, overrides, res, ctx, furniture=True)
        for i, h in enumerate(hits if res.rects or res.status == "timeout" else []):
            left = len(hits) - i
            share = (remaining() - left * SCORE_RESERVE) / left
            if share < MIN_CASE:
                break
            r2, c2 = _solve(plate, unit, diagram, overrides, tl(min(time_limit, share)), explain=False, case=h)
            if r2.rects:
                _score(plate, unit, overrides, r2, c2, furniture=True)
                cands.append((r2, c2))
        if cands:
            res, ctx = _pick(plate, unit, overrides, cands, furniture=True)
        cand_log = [{"case_id": c.get("case_id"), "score": c.get("pick_score")} for _r, c in cands]
    else:
        per = (budget - DRAFT_RESERVE - PICK_RESERVE) / 2 if two else time_limit
        res, ctx = _solve(plate, unit, diagram, overrides, tl(min(time_limit, per)), explain=True)
    if not hits and two and res.rects and remaining() > per - 0.3:
        # v2: a second candidate from another seed; the critic (no furniture pass, ~0.1 s) keeps the better plan
        r2, c2 = _solve(plate, unit, diagram, overrides, tl(per), explain=False, seed=11)
        if r2.rects:
            res, ctx = _pick(plate, unit, overrides, [(res, ctx), (r2, c2)])
    slow = False
    if not res.rects and res.status == "timeout":
        # The search ran out of time, not out of options (nothing was proven impossible): a correct plan is worth
        # waiting for, so take time up to HARD_CEILING before any fix changes the programme.
        slow = True
        limit[0] = max(budget, HARD_CEILING)
        res, ctx = _solve(plate, unit, diagram, overrides, tl(HARD_CEILING), explain=True)
    applied: dict | None = None
    all_fixes: list[dict] = []
    cur_d, cur_ov, cur_u = diagram, overrides, unit
    rounds = 0

    def chain(step: dict) -> dict:
        if applied is None:
            return step
        prev = applied["steps"] if applied.get("action") == "multi" else [applied]
        return {"action": "multi", "steps": [*prev, step], "label": " + ".join(x["label"] for x in [*prev, step])}

    while not res.rects and auto_fix and rounds < max_fix_rounds and remaining() > MIN_ATTEMPT:
        rounds += 1
        core = res.core
        conf = Conflict(id=f"solver-{rounds}", rooms=sorted({c.split(":", 1)[1] for c in core}), kind="solver",
                        reason=_describe(core, cur_d, fac))
        fixes = candidate_fixes(core, cur_d, cur_ov, cur_u)
        verified = []
        winner = None
        for f in fixes:
            # attempts share what is left; once a fix works, further checks (they only fill the fix list)
            # run short and only while time is plentiful
            if remaining() < (MIN_ATTEMPT if winner is None else 3.0):
                break
            cap = min(time_limit, 4.0, remaining() / 2) if winner is None else min(2.0, remaining() - 1.0)
            d2, ov2, u2 = apply_fix(f, cur_d, cur_ov, cur_u)
            r2, c2 = _solve(plate, u2, d2, ov2, tl(cap), explain=False)
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
            applied = chain(f)
            break
        # nothing single worked: apply the strongest fix and go round again
        if not fixes:
            break
        strength = {"smaller_programme": 0, "duct_all": 1, "duct": 2, "drop_room": 3}
        step = min(fixes, key=lambda f: strength.get(f["action"], 9))
        cur_d, cur_ov, cur_u = apply_fix(step, cur_d, cur_ov, cur_u)
        applied = chain(step)
        if remaining() < MIN_ATTEMPT:
            break
        res, ctx = _solve(plate, cur_u, cur_d, cur_ov, tl(time_limit), explain=remaining() > 3.0)

    # ---- validate the drafted plan; never hand back one that fails a DCPR room check
    plan: Plan | None = None
    fails: list[tuple[str, str, str]] = []
    stats = {"validated": 0, "failed_checks": 0, "retries": 0, "extended": slow}

    def draft_checked() -> bool:
        nonlocal plan, fails
        stats["validated"] += 1
        zd = ctx.get("diagram", cur_d)
        links = res.links if res.links is not None else [tuple(l) for l in zd.links]
        plan = draft(ctx["ug"], ctx["specs"], res.rects, links, res.entry or zd.entry, cur_ov, hub_point=res.hub_point)
        fails = room_check_fails(plan, plate)
        if fails:
            stats["failed_checks"] += 1
        return not fails

    ok = bool(res.rects) and draft_checked()
    if not ok and res.rects:
        limit[0] = max(budget, HARD_CEILING)  # a plan that fails a statutory check is never returned: take the time
    steps = [("tight", t) for t in TIGHTEN] + [("fix", None)] * 2
    for kind, t in steps:
        if ok or not res.rects or remaining() < MIN_ATTEMPT:
            break
        stats["extended"] = True
        stats["retries"] += 1
        if kind == "tight":
            bad_types = frozenset(ctx_type for ctx_type in
                                  {sp.type for sp in ctx["specs"] if sp.id in {f[1] for f in fails}}) or None
            tight = (t[0], t[1], bad_types if t is TIGHTEN[0] else frozenset(HABITABLE))
            r2, c2 = _solve(plate, cur_u, cur_d, cur_ov, tl(time_limit), explain=False, tight=tight)
            if r2.rects:
                res, ctx = r2, c2
                ok = draft_checked()
        else:
            fixes = candidate_fixes([], cur_d, cur_ov, cur_u)
            if not fixes:
                break
            strength = {"smaller_programme": 0, "duct_all": 1, "duct": 2, "drop_room": 3}
            step = min(fixes, key=lambda f: strength.get(f["action"], 9))
            cur_d, cur_ov, cur_u = apply_fix(step, cur_d, cur_ov, cur_u)
            applied = chain(step)
            r2, c2 = _solve(plate, cur_u, cur_d, cur_ov, tl(time_limit), explain=False)
            if r2.rects:
                res, ctx = r2, c2
                ok = draft_checked()
            else:
                res = r2
    if res.rects and ctx.get("diagram") is not None:
        cur_d = ctx["diagram"].model_copy(deep=True)  # the v2 brief that was actually planned
    cur_d.conflicts = conflicts
    if fails and not ok:
        names = _names(cur_d)
        why = "; ".join(sorted({f"{names.get(rid, rid)}: {msg} ({rule})" for rule, rid, msg in fails}))
        conflicts.append(Conflict(id="validation", rooms=sorted({f[1] for f in fails}), kind="geometry",
                                  reason="The drafted plan breaks a statutory room check, so it was not used. " + why))
        res = SolveResult("infeasible", time_s=round(time.perf_counter() - t0, 3),
                          core=sorted({f"size:{f[1]}" for f in fails}))
    cur_d.feasible = bool(res.rects)
    if not res.rects:
        cur_d.message = "No layout found. " + (conflicts[-1].reason if conflicts else "")
        return FlatResult(None, cur_d, res, applied, all_fixes, conflicts, cur_d.message, stats)
    assert plan is not None
    if res.dropped:  # v2: an optional room that does not fit is by design, not a conflict to ask the user about
        opt = {r.id for r in cur_d.rooms if getattr(r, "optional", False)}
        res.dropped = [rid for rid in res.dropped if rid not in opt]
    plan.status = res.status if res.status in ("optimal", "feasible", "timeout") else "feasible"  # type: ignore[assignment]
    plan.solve_time_s = round(time.perf_counter() - t0, 3)
    plan.objective = res.objective
    cur_d.message = "This diagram can become a plan." if not applied else f"Solved after: {applied['label']}."
    case_id = ctx.get("case_id")
    if case_id:
        plan.case_id = case_id
        cur_d.message += f" Arrangement after approved case {case_id}."
    return FlatResult(plan, cur_d, res, applied, all_fixes, conflicts, cur_d.message, stats, case_id=case_id,
                      candidates=cand_log)


def flat_overrides_from(diagram: Diagram) -> dict[str, Any]:
    return {}
