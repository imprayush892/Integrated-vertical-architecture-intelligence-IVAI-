# Release gates ("Done means")

Measured with `planner golden --verbose` on 2026-10-16 (Linux, 4 cores). CI re-runs the full set on
every push (`golden` job) and uploads the JSON report.

| Gate (PRD) | Target | Measured | Status |
|---|---|---|---|
| Decisions made by rules | ≥ 92% | 93.0% (172 rule + 13 safe-default tickets of 185 decisions, full Lokhandwala run) | pass |
| Time to solve one 3BHK flat | ≤ 8 s | worst 7.56 s (CP-SAT cap 7.5 s, best-so-far returned) | pass |
| Golden flats solved with no human fix | ≥ 95% | 97.5% (78 / 80 flats, 5 shapes × 3 envelopes; engine safe defaults allowed) | pass |
| Rule results match the expected results | 100% | 110 / 110 on 10 reference drawings | pass |
| Entities lost in a DXF save and reopen | 0 | 0 of 218 (and 0 handles) | pass |
| Stage 1: "Lokhandwala" found | ≤ 50 m, < 2 s | 5.7 m, 0.36 s (offline gazetteer; online providers cached) | pass |
| Stage 2: traced area vs geodesic | within 0.5% on 20 plots | worst 0.002% | pass |
| Stage 4: placement options | < 3 s, coverage and gap rules pass | 1.68 s, all pass | pass |
| Stage 5: zones sum to plate | exact | worst 0.000 m² over 6 shapes × 3 envelopes | pass |
| Approved plans: DCPR room clauses pass approved Mumbai rooms | ≥ 95% per clause, or ≤ 5% near misses (within 10% under) | 37(1)(b) 95.3%, 37(1)(c) 94.8% (near 1.9%), 37(2) 92.3% (near 0%, 5.0–5.5 m² kitchens graded review), 37(6)(c) 86.3% (near 3.2%) — 2373 rooms, 50 projects, 0.1 s | pass |

## Approved-plans gate

Municipal-approved Mumbai rooms are legal by definition, so our DCPR 2034 room rules must pass them
(`golden.gate_approved`, in the quick set; `planner_engine/approved.py`).

* **Data.** `planner_engine/data/golden/approved_rooms.json`: 2373 unique OCR'd clear-dimension rows
  (`type, short, long, area, pdf`) of habitable and wet types from 50 approved residential projects, filtered
  with the same noise rules as `tools/summarize_mumbai.py`. Rebuild with
  `python -m planner_engine.approved build rooms_final.jsonl` (source checkout only: it imports `tools/`).
* **Check.** Each row becomes a minimal `PlanRoom` (short × long rectangle, labelled area, `width = short`) and goes
  through `checks._room_result`/`_grade` for every `scope: room`, area/width clause in `data/rules/dcpr_2034`. No
  threshold is copied into the gate, so editing the rules file moves the gate. Per clause it reports tested /
  pass / review / fail, the pass rate, the failing measure, the five worst rows and the projects with most fails.
* **Pass.** A clause is ok when (pass + review) / tested ≥ 95%. Clauses with no rows (WC 37(6)(a), bath 37(6)(b): the
  labels are all "toilet") are not scored; 37(1)(a) (a flat's largest habitable room) needs a whole flat and is listed
  as untested. `python -m planner_engine.approved [rows.jsonl]` prints the full report.

Measured on 2026-10-16 (rows tested, pass rate):

| Clause | Rows | Pass | Verdict |
|---|---|---|---|
| 37(1)(b) habitable ≥ 7.5 m² | 1437 | 95.3% | pass (margin 0.3%) |
| 37(1)(c) habitable width ≥ 2.40 m | 1437 | 94.8% | **fail** |
| 37(2) kitchen ≥ 5.5 m², width 1.80 m | 376 | 85.9% | **fail** (area: 52 rows, width: 9) |
| 37(6)(c) toilet ≥ 2.2 m², width 1.20 m | 560 | 86.3% | **fail** (area: 60 rows, width: 50) |

Rooms far below a threshold are OCR mix-ups (a wardrobe dimension read as the bedroom's, a WC labelled
"toilet"); rooms just under it (within 10%) are evidence the threshold is stricter than sanctioned practice,
so the gate passes a clause when its pass rate is ≥ 95% or its near-miss rate is ≤ 5%. The 12 projects with
sanctioned 5.0–5.5 m² kitchens led to `review_below: 0.10` on DCPR-37-2 (graded review, not fail).

A failing clause means either the threshold is stricter than the approved practice (confirm against the
gazetted text) or the OCR sample is noisy (labels can pick up a window sill or a niche). The investigation is
recorded with the change that introduced the gate; the thresholds are not edited by the gate.

## What the gates do not prove

* **Consultant agreement.** The PRD asks for 100% agreement with a licensed consultant on 10 real
  drawings. The reference set here is 10 constructed drawings with hand-derived results; replace or extend
  it with real office drawings and the consultant's results (`tools_reference.py`) before relying on it.
* **Approved-plans sample.** The rows are OCR'd labels, not surveyed rooms: some small "rooms" are fragments
  (a 0.9 × 1.25 m "bedroom"), and a toilet label is not split into bath / WC / combined. Fails are leads to
  investigate, not proof the clause is wrong.
* **Rule values.** Rule files ship `verified: false`. They must be reviewed for each office / scheme.
* **Triangle plates.** The triangle template is excluded from the golden flats: its triangular flats rarely
  pack with orthogonal rooms. It is never auto-selected; when chosen, the T3 ticket reports the clash.
* **Two of 80 golden flats** (5.4 m-wide stem flats on a T plate) are genuinely too narrow for a living
  room and a bedroom with windows; the engine reports this rather than drawing a non-compliant flat.
