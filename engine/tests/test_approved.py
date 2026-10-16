"""Release gate "approved plans": DCPR room clauses vs. municipal-approved Mumbai rooms."""

import inspect
import json
import time
from pathlib import Path

import pytest

from planner_engine import approved, golden, rules


def _row(t, short, long, pdf="p"):
    return {"type": t, "short": short, "long": long, "area": round(short * long, 3), "pdf": pdf}


def test_packaged_sample_shape():
    rows = approved.packaged_rows()
    assert len(rows) > 1000
    assert {r["type"] for r in rows} <= {t for c in approved._room_clauses() for t in c.types}
    assert {"bedroom", "living", "kitchen", "toilet"} <= {r["type"] for r in rows}
    assert len({r["pdf"] for r in rows}) >= 40
    assert len({(r["pdf"], r["type"], r["short"], r["long"], r["area"]) for r in rows}) == len(rows)
    assert all(r["short"] <= r["long"] and r["area"] > 0 for r in rows)


def test_gate_report_structure_and_speed():
    t0 = time.perf_counter()
    res = approved.approved_gate()
    assert time.perf_counter() - t0 < 2.0  # quick-set budget
    assert res["gate"] == "approved" and res["rows"] > 1000
    tested = {k: v for k, v in res["clauses"].items() if v["tested"]}
    assert {"DCPR-37-1-b", "DCPR-37-1-c", "DCPR-37-2", "DCPR-37-6-c"} <= set(tested)
    for v in tested.values():
        assert v["pass"] + v["review"] + v["fail"] == v["tested"]
        assert v["ok"] == (v["rate"] >= res["min_rate"] or v["near_miss_rate"] <= 1 - res["min_rate"])
        assert len(v["worst"]) == min(approved.TOP_OFFENDERS, v["fail"])
        assert [w["shortfall_pct"] for w in v["worst"]] == sorted((w["shortfall_pct"] for w in v["worst"]), reverse=True)
    assert res["ok"] == all(v["ok"] for v in tested.values())
    assert res["untested_clauses"] == ["DCPR-37-1-a"]  # per-flat clause, needs a whole flat


def test_gate_uses_rule_file_thresholds():
    """Rooms sitting just either side of the clause thresholds flip pass/fail: nothing is hard-coded here."""
    by_id = {c.id: c for c in rules.clauses()}
    area_k, width_k = (float(x) for x in by_id["DCPR-37-2"].threshold)
    rows = [_row("kitchen", width_k + 0.5, area_k / (width_k + 0.5) + 0.5),  # comfortably legal
            _row("kitchen", width_k - 0.2, 4.0),  # too narrow
            _row("kitchen", width_k + 0.5, (area_k - 1.0) / (width_k + 0.5))]  # too small
    res = approved.approved_gate(rows=rows)
    k = res["clauses"]["DCPR-37-2"]
    assert (k["tested"], k["fail"]) == (3, 2)
    # both misses are >10% under: OCR label mix-ups, not evidence that the threshold is too strict
    assert k["near_miss"] == 0 and k["ok"]
    assert set(k["failing_measure"]) == {"area", "width"}
    assert k["worst"][0]["shortfall_pct"] >= k["worst"][1]["shortfall_pct"]
    # a habitable room tested by size and width clauses only; toilet never hits the kitchen clause
    legal = approved.approved_gate(rows=[_row("bedroom", 3.0, 4.0), _row("toilet", 1.5, 2.5)])
    assert legal["ok"] and legal["clauses"]["DCPR-37-1-b"]["tested"] == 1
    assert legal["clauses"]["DCPR-37-6-c"]["tested"] == 1 and legal["clauses"]["DCPR-37-2"]["tested"] == 0


def test_min_rate_is_per_clause():
    rows = [_row("bedroom", 3.0, 4.0, f"p{i}") for i in range(19)] + [_row("bedroom", 2.3, 3.0)]
    # 19/20 = 95% >= 0.95; the 2.3 x 3.0 room (6.9 m2, 2.3 m) is a near miss on both clauses
    res = approved.approved_gate(rows=rows)
    assert res["clauses"]["DCPR-37-1-c"]["rate"] == 0.95 and res["ok"]
    assert not approved.approved_gate(rows=rows, min_rate=0.96)["ok"]


def test_golden_wiring():
    g = golden.gate_approved()
    assert g["gate"] == "approved" and g["seconds"] < 2.0
    assert "gate_approved" in inspect.getsource(golden.run_all)  # in the always-run (quick) set


def test_build_sample_roundtrip(tmp_path):
    pytest.importorskip("yaml")
    if not (Path(approved.__file__).resolve().parent.parent / "tools" / "summarize_mumbai.py").exists():
        pytest.skip("source checkout only (tools/)")
    good = {"pdf": "A", "page": 0, "name": "BED ROOM", "type": "bedroom", "a": 3.0, "b": 3.5, "short": 3.0,
            "long": 3.5, "area": 10.5}
    lines = [good, dict(good), {**good, "type": "lift"}, {**good, "short": 0.5, "a": 0.5},  # dup, other type, noise
             {**good, "pdf": "B", "type": "kitchen", "a": 1.9, "b": 3.0, "short": 1.9, "long": 3.0, "area": 5.7},
             {"type": "_page_done", "pdf": "A"}]
    src = tmp_path / "rows.jsonl"
    src.write_text("\n".join(json.dumps(x) for x in lines) + "\nnot json\n")
    out = tmp_path / "sample.json"
    info = approved.build_sample(src, out)
    assert info["rows"] == 2 and info["projects"] == 2
    doc = json.loads(out.read_text())
    assert doc["columns"] == ["type", "short", "long", "area", "pdf"]
    assert approved.approved_gate(src)["rows"] == 2


def test_kitchen_slightly_under_minimum_is_review():
    res = approved.approved_gate(rows=[_row("kitchen", 2.15, 2.4)])  # 5.16 m2: sanctioned in approved plans
    k = res["clauses"]["DCPR-37-2"]
    assert (k["review"], k["fail"]) == (1, 0)
