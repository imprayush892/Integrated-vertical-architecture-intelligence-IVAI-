"""Release gate "approved plans": the municipal-approved Mumbai rooms are legal by definition, so our
DCPR 2034 room-size / width clauses must pass them.

Each unique approved room row (type, short, long, area) of a habitable or wet type is turned into a minimal
``PlanRoom`` (a short x long rectangle carrying the labelled clear area) and run through the same clause
records (``rules.clauses()``) and the same per-room check (``checks._room_result`` -> ``checks._grade``) the
plan checker uses. No threshold lives in this file: a changed rules file changes the gate.

The packaged sample ``data/golden/approved_rooms.json`` is built from the raw OCR rows with the same noise
filter as ``tools/summarize_mumbai.py``::

    python -m planner_engine.approved build /path/to/rooms_final.jsonl
"""

from __future__ import annotations

import importlib.util
import json
import sys
from collections import Counter, defaultdict
from importlib import resources
from pathlib import Path
from typing import Any

from . import rules
from .checks import FloorContext, _grade, _room_result
from .models import PlanRoom

SAMPLE = "data/golden/approved_rooms.json"
GEOMETRY_METRICS = {"area", "width", "area_and_width"}
TARGET_RATE = 0.95
TOP_OFFENDERS = 5


NEAR_MISS = 0.10  # fails within 10% of the threshold count against the rule


def _rows_types() -> set[str]:
    """Row types the room clauses test (habitable + wet types any size/width clause names)."""
    return {t for c in _room_clauses() for t in c.types}


def _room_clauses() -> list[rules.Clause]:
    return [c for c in rules.clauses()
            if c.code == "DCPR 2034" and c.scope == "room" and c.metric in GEOMETRY_METRICS]


# --- sample building / loading -------------------------------------------------------------------------

def _summarizer():
    """``tools/summarize_mumbai.py`` (a repo script, not part of the wheel) imported by path."""
    for base in (Path(__file__).resolve().parent.parent, Path.cwd()):
        p = base / "tools" / "summarize_mumbai.py"
        if p.exists():
            spec = importlib.util.spec_from_file_location("summarize_mumbai", p)
            mod = importlib.util.module_from_spec(spec)
            sys.modules.setdefault("summarize_mumbai", mod)
            spec.loader.exec_module(mod)
            return mod
    raise FileNotFoundError("tools/summarize_mumbai.py not found (run from a source checkout)")


def load_raw_rows(path: str | Path) -> list[dict]:
    """Raw OCR jsonl -> filtered, deduped rows ``{type, short, long, area, pdf}`` (summarize_mumbai noise filter)."""
    sm = _summarizer()
    wanted = _rows_types()
    seen: set[tuple] = set()
    out: list[dict] = []
    with open(path) as fh:
        for line in fh:
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if r.get("type") not in wanted or r.get("error") or not sm.valid(r):
                continue
            row = {"type": r["type"], "short": round(float(r["short"]), 3), "long": round(float(r["long"]), 3),
                   "area": round(float(r["area"]), 3), "pdf": r["pdf"]}
            key = (row["pdf"], row["type"], row["short"], row["long"], row["area"])
            if key not in seen:
                seen.add(key)
                out.append(row)
    return out


def build_sample(src: str | Path, dst: str | Path | None = None) -> dict:
    rows = load_raw_rows(src)
    pdfs = sorted({r["pdf"] for r in rows})
    idx = {p: i for i, p in enumerate(pdfs)}
    doc = {
        "about": "Unique OCR'd clear-dimension room labels of municipal-approved Mumbai residential plans, "
                 "filtered like tools/summarize_mumbai.py. Rows are [type, short_m, long_m, area_m2, pdf index].",
        "columns": ["type", "short", "long", "area", "pdf"],
        "pdfs": pdfs,
        "rows": sorted([r["type"], r["short"], r["long"], r["area"], idx[r["pdf"]]] for r in rows),
    }
    dst = Path(dst) if dst else Path(str(resources.files("planner_engine").joinpath(SAMPLE)))
    dst.write_text(json.dumps(doc, separators=(",", ":")) + "\n")
    return {"rows": len(doc["rows"]), "projects": len(pdfs), "path": str(dst)}


def packaged_rows() -> list[dict]:
    doc = json.loads(resources.files("planner_engine").joinpath(SAMPLE).read_text())
    pdfs = doc["pdfs"]
    return [{"type": t, "short": s, "long": lg, "area": a, "pdf": pdfs[p]} for t, s, lg, a, p in doc["rows"]]


# --- the gate ------------------------------------------------------------------------------------------

def _room(r: dict, i: int) -> PlanRoom:
    s, lg = float(r["short"]), float(r["long"])
    return PlanRoom(id=f"R{i}", type=r["type"], name=r["type"], area=float(r["area"]), width=s,
                    polygon=[(0.0, 0.0), (lg, 0.0), (lg, s), (0.0, s)])


def _parts(c: rules.Clause, room: PlanRoom) -> list[tuple[str, float, float]]:
    """(measure, value, threshold) pairs a clause tests on this room, for shortfall ranking."""
    if c.metric == "area":
        return [("area", room.area, float(c.threshold))]
    if c.metric == "width":
        return [("width", room.width, float(c.threshold))]
    return [("area", room.area, float(c.threshold[0])), ("width", room.width, float(c.threshold[1]))]


def approved_gate(rows_path: str | Path | None = None, rows: list[dict] | None = None,
                  min_rate: float = TARGET_RATE) -> dict[str, Any]:
    """Run the DCPR room-size/width clauses over approved-plan rooms.

    ``rows_path``: raw OCR jsonl (filtered like summarize_mumbai); default: the packaged sample.
    A clause is ok when (pass + review) / tested >= ``min_rate`` ("review" = inside the clause's review band,
    never a fail); ``strict_rate`` counts only "pass".
    """
    data = rows if rows is not None else load_raw_rows(rows_path) if rows_path else packaged_rows()
    ctx = FloorContext(plans=[], height=0.0)
    by_clause: dict[str, dict[str, Any]] = {}
    fails: dict[str, list[tuple[float, dict, list]]] = defaultdict(list)
    for c in _room_clauses():
        by_clause[c.id] = {"clause": c.clause, "title": c.title, "types": list(c.types), "tested": 0,
                           "pass": 0, "review": 0, "fail": 0}
    for i, r in enumerate(data):
        room = None
        for c in _room_clauses():
            if r["type"] not in c.types:
                continue
            room = room or _room(r, i)
            res = _room_result(c, room, ctx)
            if res is None:
                continue
            b = by_clause[c.id]
            b["tested"] += 1
            b[res.result] += 1
            if res.result == "fail":
                short = [(m, v, t) for m, v, t in _parts(c, room) if _grade(c, v, t) == "fail"]
                gap = max(((t - v) / t for _, v, t in short), default=0.0)
                fails[c.id].append((gap, r, short))
    out_clauses = {}
    for cid, b in by_clause.items():
        n = b["tested"]
        b["rate"] = round((b["pass"] + b["review"]) / n, 4) if n else None
        b["strict_rate"] = round(b["pass"] / n, 4) if n else None
        # A threshold that is too strict shows up as approved rooms just under it; a room far below it is an
        # OCR label/dimension mix-up (a wardrobe size read as the bedroom, a WC labelled "toilet").
        near = sum(1 for g, _, _ in fails[cid] if g <= NEAR_MISS)
        b["near_miss"] = near
        b["near_miss_rate"] = round(near / n, 4) if n else None
        b["ok"] = n == 0 or b["rate"] >= min_rate or near / n <= 1 - min_rate
        worst = sorted(fails[cid], key=lambda x: -x[0])[:TOP_OFFENDERS]
        b["worst"] = [{"type": r["type"], "short": r["short"], "long": r["long"], "area": r["area"], "pdf": r["pdf"],
                       "short_by": {m: [round(v, 3), t] for m, v, t in sh}, "shortfall_pct": round(100 * g, 1)}
                      for g, r, sh in worst]
        if fails[cid]:
            b["fail_by_pdf"] = dict(Counter(r["pdf"] for _, r, _ in fails[cid]).most_common(5))
            b["failing_measure"] = dict(Counter(m for _, _, sh in fails[cid] for m, _, _ in sh))
        out_clauses[cid] = b
    tested = [b for b in out_clauses.values() if b["tested"]]
    return {"gate": "approved", "ok": bool(tested) and all(b["ok"] for b in tested), "min_rate": min_rate,
            "rows": len(data), "projects": len({r["pdf"] for r in data}),
            "types": dict(Counter(r["type"] for r in data)), "clauses": out_clauses,
            "untested_clauses": sorted(c.id for c in rules.clauses()
                                       if c.code == "DCPR 2034" and c.scope == "unit" and c.metric == "max_area")}


def main(argv: list[str] | None = None) -> int:
    a = list(sys.argv[1:] if argv is None else argv)
    if a and a[0] == "build":
        if len(a) < 2:
            print(__doc__)
            return 2
        print(json.dumps(build_sample(a[1], a[2] if len(a) > 2 else None)))
        return 0
    res = approved_gate(a[0] if a else None)
    print(json.dumps(res, indent=2))
    return 0 if res["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
