# Rules files

All statutory values live in YAML under `engine/planner_engine/data/rules/`. Nothing statutory is
hard-coded in Python.

| File | Contents |
|---|---|
| `site.yaml` | DCPR 2034 marginal open spaces (by height, front by road width), distance between buildings, maximum ground coverage |
| `dcpr_2034/reg37_rooms.yaml` | Habitable rooms (9.5 / 7.5 m², 2.40 m), kitchen, WC, bath, bath+WC |
| `dcpr_2034/reg39_light.yaml` | Openings ≥ 1/6 floor area, toilet ventilation, common passage width |
| `nbc_2016/part4_egress.yaml` | Travel distance, number of staircases, stair flight width, exit door width, fire lift / refuge advisory |
| `design.yaml` | Architect-authored defaults (plate limits, core sizes, wall thicknesses) - not statutory |
| `layout_priors.yaml` | Shape priors from ResPlan (aspect ratios, balcony rules, door affinities) - not statutory |

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
