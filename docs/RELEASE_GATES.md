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

## What the gates do not prove

* **Consultant agreement.** The PRD asks for 100% agreement with a licensed consultant on 10 real
  drawings. The reference set here is 10 constructed drawings with hand-derived results; replace or extend
  it with real office drawings and the consultant's results (`tools_reference.py`) before relying on it.
* **Rule values.** Rule files ship `verified: false`. They must be reviewed for each office / scheme.
* **Triangle plates.** The triangle template is excluded from the golden flats: its triangular flats rarely
  pack with orthogonal rooms. It is never auto-selected; when chosen, the T3 ticket reports the clash.
* **Two of 80 golden flats** (5.4 m-wide stem flats on a T plate) are genuinely too narrow for a living
  room and a bedroom with windows; the engine reports this rather than drawing a non-compliant flat.
