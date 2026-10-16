# Rules files

All statutory values live in YAML under `engine/planner_engine/data/rules/`. Nothing statutory is
hard-coded in Python.

| File | Contents |
|---|---|
| `site.yaml` | DCPR 2034 marginal open spaces (by height, front by road width), distance between buildings, maximum ground coverage |
| `dcpr_2034/reg37_rooms.yaml` | Habitable rooms (9.5 / 7.5 m², 2.40 m), kitchen, WC, bath, bath+WC |
| `dcpr_2034/reg39_light.yaml` | Openings ≥ 1/6 floor area, toilet ventilation (window or AVS shaft = pass, mechanical duct = review), AVS minimum size (3.0 m², 1.5 m side, `DCPR-39-3`), common passage width |
| `nbc_2016/part4_egress.yaml` | Travel distance, number of staircases, stair flight width, exit door width, fire lift / refuge advisory |
| `design.yaml` | Architect-authored defaults (plate limits, core sizes, wall thicknesses) - not statutory |
| `layout_priors.yaml` | Shape priors from ResPlan (aspect ratios, balcony rules, door affinities), with aspect limits tightened to the approved-Mumbai p95 (`mumbai_approved_stats.json`) for master, kitchen, foyer and passage; balcony rules unchanged (long side on the façade, aspect >= 1.5) - not statutory |
| `mumbai_topology_priors.json` | Counted topology priors of municipal-approved Mumbai plans: room-type pair adjacency / door-link rates, façade and shaft share per type, entrance depth, toilet attachment, typical room list per BHK. Built by `tools/derive_topology_priors.py` from the output of `tools/extract_pdf_topology.py`; every figure carries `n` and `n_projects` and is dropped below 5 projects. Not statutory |
| `../programmes/*.json` | Flat room programmes (target / minimum areas per BHK). Targets are calibrated to the p50 (towards p75 for larger flats) of municipal-approved Mumbai plans, `planner_engine/data/mumbai_approved_stats.json` (50 projects, 655 pages); `min_area` is the legal floor and is never lowered to an approved p5-p10 value (measurement noise) |

## A clause record

```yaml
- id: DCPR-37-6-c            # unique, stable; shown in the UI and DXF proof
  clause: "Reg 37(6)(c)"
  title: "Bath + WC combined at least 2.20 m2, width 1.20 m"
  scope: room                 # room | unit | floor | building
  metric: area_and_width      # area, width, area_and_width, max_area, window_ratio, ventilated,
                              # passage_width, travel_distance, stair_count, stair_width,
                              # entry_door_width, advisory
  types: [toilet]             # room types it applies to
  op: ">="
  threshold: [2.2, 1.2]
  unit: "m2 / m"
  review_band: 0.0            # results this close to the threshold are "review", never silent "pass"
  when: "height > 15"         # optional applicability
```

Every file carries `code`, `source`, `version` and `verified`. Ship with `verified: false` until the
office's licensed consultant has reviewed the values; the UI and `explain_check` report it.

## Ventilation: window, shaft, duct

`PlanRoom.vent_via` is `window`, `shaft` or `duct`. A toilet or kitchen opening onto a plate air-ventilation
shaft (AVS) is `shaft` and passes `DCPR-39-2`, as in approved Mumbai plans; a mechanical duct only
is `duct` and stays `review`. `DCPR-39-3` (metric `ventilated`, list threshold `[min area, min side]`) tests
the AVS size of shaft-ventilated rooms; a smaller or unmeasured shaft is `review`. The shaft size is a lower-bound
estimate from the shaft edges the flat sees (shafts are square), so confirm a `review` on the drawing.

## Topology priors (Mumbai approved plans)

`data/rules/mumbai_topology_priors.json` is counted, not fitted, from 2,153 clean flats in 48 approved projects
(3,665 extracted; flats flagged `suspect_merge`, without a found entrance, or with more than 25% unnamed rooms are
dropped, and a room whose area is more than 25% off its own printed dimensions is not trusted for adjacency).
Rebuild it with `.venv/bin/python tools/derive_topology_priors.py --input <topology.jsonl>`; the same input always
gives the same file. Door arcs are drawn on only about 22% of pages, so `door` is measured on those pages only and
`link` (door arc or open wall gap, includes some spurious gaps) on all pages.

The engine uses a figure only when it is strong (`layout_priors.yaml` `topology:`): at least 5 projects, and both the
pooled value and the median across projects reach the threshold. Weak figures change nothing; the programme default stays.
Priors only ever add a window or a link, never remove a statutory requirement.

| Use | Threshold | Where | Result |
|---|---|---|---|
| Default window | P(room touches façade) >= 0.8 | `diagram.prior_needs_window` | bedroom 0.89, master 0.91, study 0.93 need a window. Living 0.75 and kitchen 0.76 are below the line but stay `needs_window` (statutory); toilet 0.40 / dining 0.12 / passage 0.03 stay off |
| Default link | P(link) >= 0.6 | `diagram.complete_links` | A room the programme links leave unreachable from the entry gets the strongest prior link. Safety net only: stock programmes are already connected, and no figure for a study (best: living 0.33) or dress room (bedroom 0.57) reaches 0.6, so those still need a link from the user |
| Evidence per link | | `diagram.audit_programme` | supported / weak / no data, with n and n_projects, for every programme link |
| Entrance | named entrance room | programmes | Living is the entrance room in 73% of flats (n=1,797, 48 projects), a named foyer in 0.4%: the foyer is `optional` and the entry falls back to the living room (solver `entry_fallback`) |
| Toilet attachment | 1BHK single toilet next to the bedroom in 62% | `programmes/1bhk.json` | `living-toilet` (12%) became `master-toilet` (87%) |

Figures that were read and left alone because they are weak or not usable by the solver: toilet ventilation (façade only 30%,
shaft only 12%, both 10%, neither 48% - the engine's `duct` fix covers "neither"; where a toilet is ventilated at all it
is by façade 3 times in 4), the common-toilet link `passage-toilet_c` (55%, weak but nothing better), `dining` in 2-3BHK (present in
about 40% because "living / dining" halls are labelled as living), `passage` in 2BHK (43%), and 4BHK lists (23 flats, 9 projects).
The 2BHK and 3BHK toilet patterns agree with the programmes (3BHK: 3 toilets, 2 next to bedrooms, 46% of flats; 2BHK is split
between one attached toilet and two attached toilets, 28% each).

## Office overrides

Copy any file to a folder and point `PLANNER_RULES_DIR` at it; the same relative path wins over the
bundled copy (e.g. `PLANNER_RULES_DIR/site.yaml`, `PLANNER_RULES_DIR/dcpr_2034/reg37_rooms.yaml`).
Clause ids must stay unique across all files; the loader refuses duplicates.

## Proof

Each check result stores the measured value, the threshold, the clause id and the proof: room ids for
generated plans, DXF entity handles for imported drawings, plus a bounding box. Clicking a result in
the UI zooms to it. `planner check drawing.dxf` prints the same for CI or batch use.

## Reference drawings

`engine/planner_engine/tools_reference.py` generates 10 DXF floors (clean, mm units, a lying header,
line-only walls with odd layer names, and one broken rule each) with hand-derived expected results.
`planner golden --gate rules` must match all 110 expectations. Add your office's real drawings and the
consultant's results to the same list to extend the gate.
