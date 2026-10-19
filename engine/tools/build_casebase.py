"""Build the generator-v4 case library (engine/planner_engine/data/casebase_v1.json.gz).

    engine/.venv/bin/python engine/tools/build_casebase.py [--min-score 70] [--workers 2] [--out PATH]

Every kept flat of data/mined/graphs_v2.jsonl.gz, one per (project, signature), converted with
``approved_bench.convert`` and kept when the conversion is reliable (``approved_bench.usable``), it has a living room and a
kitchen, 1-4 bedrooms, and the current critic (approved-plan exclusions) scores it at least ``--min-score``. Each case is
stored in the canonical entrance frame of ``planner_engine.casebase`` (rooms as normalised rectangles and polygons, door
graph, façade coverage, critic score).
"""

from __future__ import annotations

import argparse
import gzip
import json
import sys
import time
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from planner_engine import approved_bench as ab  # noqa: E402
from planner_engine import casebase  # noqa: E402


def _job(item):
    project, plan_json, info, rec = item
    row = ab.score_one((project, plan_json, info))
    from planner_engine.models import Plan

    case = casebase.case_from_plan(Plan.model_validate(plan_json), rec, row["score"], row["grade"], project)
    return row, case


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-score", type=float, default=70.0)
    ap.add_argument("--workers", type=int, default=2)
    ap.add_argument("--out", default=str(ROOT / "planner_engine" / casebase.CASEBASE))
    a = ap.parse_args()
    t0 = time.perf_counter()
    recs = ab.load_flats()
    seen, jobs, stats = set(), [], Counter()
    for r in recs:
        sig = (r["project"], r["flat"]["signature"])
        if sig in seen:
            stats["duplicate"] += 1
            continue
        seen.add(sig)
        c = ab.convert(r)
        if c is None:
            stats["not_convertible"] += 1
            continue
        if not ab.usable(c[1]):
            stats["not_usable"] += 1
            continue
        jobs.append((r["project"], c[0].model_dump(mode="json"), c[1], r["flat"]))
    print(f"{len(recs)} flats, {len(jobs)} to score ({dict(stats)})", flush=True)
    with ProcessPoolExecutor(a.workers) as ex:
        res = list(ex.map(_job, jobs, chunksize=4))
    cases, grades = [], Counter()
    for row, case in res:
        grades[row["grade"]] += 1
        if case is None:
            stats["no_frame"] += 1
        elif row["score"] < a.min_score:
            stats["below_min_score"] += 1
        elif not 1 <= case["bhk"] <= 4:
            stats["bhk_out_of_range"] += 1
        else:
            cases.append(case)
    cases.sort(key=lambda c: c["id"])
    meta = {"version": 1, "source": "data/mined/graphs_v2.jsonl.gz", "min_score": a.min_score, "n_scored": len(res),
            "grades_scored": dict(sorted(grades.items())), "filtered": dict(stats), "n_cases": len(cases),
            "by_bhk": dict(sorted(Counter(c["bhk"] for c in cases).items())),
            "projects": len({c["project"] for c in cases})}
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(out, "wt", compresslevel=9) as f:
        json.dump({"meta": meta, "cases": cases}, f, separators=(",", ":"))
    print(json.dumps(meta, indent=1))
    print(f"wrote {out} ({out.stat().st_size / 1e6:.2f} MB) in {time.perf_counter() - t0:.0f} s")


if __name__ == "__main__":
    main()
