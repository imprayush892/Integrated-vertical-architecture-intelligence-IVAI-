# Tower placement v2: reasons like an architect and explains itself

Owner: placement agent. Code: `engine/planner_engine/placement.py` (public API unchanged:
`place(site, families, view_bearing) -> Placement`). Rules: `data/rules/site.yaml` (statutory, new tables all
`verified: false`) and `data/rules/design.yaml` (`placement:` weights and design defaults). Tests:
`engine/tests/test_placement_v2.py`. Review images: `docs/quality/img/placement-*.png`. To regenerate them, run
`python -m tests.test_placement_v2` from `engine/`.

## What was wrong with v1

v1 swept the plate angle over 180 degrees and kept the angle that maximised capped area, plus small bonuses. It had no
fire access, gate, ramp, FSI, garden, daylight or privacy logic, and it gave no reasons. On the review plots
(`placement-v1-*.png`):

* Towers were rotated to arbitrary angles: 44 degrees on a rectangular plot, 171/44/66 degrees on Lokhandwala. Every
  rotated tower leaves triangular leftovers and a basement column grid that clashes with the parking grid.
* On the large plot (90 m height) the 3-tower option used 68 x 22 m plates: 3 x 1,496 m2 x 30 floors, about
  187-212% of the permissible built-up area. The search never asked what the FSI allowed.
* The plates sat centred in the free space, with two leftover strips each side instead of one usable garden.
* Runtime was 1.8 s, 2.9 s and **7.4 s** (over the 3 s budget) on the three plots rendered with v1.

## How v2 works

1. **Context** (`_context`):
   * floors = height / floor-to-floor;
   * permissible built-up area = plot x FSI(road width) x fungible 1.35;
   * plate area that uses this FSI: BUA / (floors x 0.88 countable), capped by coverage;
   * setbacks, tower gap, fire faces required by height, ramp need and size, RG fraction;
   * climate: wind rose, hot-sun azimuth (from `climate.hot_facades`), view bearing (given, or the Arabian Sea to the
     west when stage 3 says sea view = yes).
2. **Frames an architect would defend** (`_frame_angles`):
   * parallel to the long plot edges (up to three directions);
   * true north-south;
   * the sun/wind compromise angle, offered only when it is at least 12 degrees from those.
   Usually 3 to 5 angles. There are no 1-degree sweeps.
3. **Concepts** (`_sizes`, `_layouts_for`, `_cluster`):
   * For each frame, tower count (1-3), kind (slab, aspect >= 1.9; or point tower) and orientation, a summed-area-table
     fit map finds the largest plate at each standard depth (slab 16/18/20/22 m, point 24/28/32 m or square) that fits
     n identical towers.
   * Plates whose core would exceed 20% of the plate are not offered.
   * Arrangements:
     * rows pushed to either side or centred: packed at the legal gap, at a comfortable gap (0.6 x H), or spread to the
       ends;
     * staggered rows;
     * greedy 2-D clusters from 8 anchor directions.
   * Arrangement names come from the actual geometry.
4. **Three scoring passes**, so that every candidate costs no more than it needs to:
   1. Position-independent terms per concept: FSI use, plate efficiency, west sun, breeze, view, alignment with the
      plot. The best 10 concept groups per tower count are kept, at most 2 per kind and orientation class.
   2. Per layout (hundreds, numpy):
      * rays from every facade: daylight, privacy, wind shadow, view blocking, clear width;
      * a raster garden and sliver estimate (opening by an 8 m square);
      * a necessary-condition fire check.
   3. Exact shapely checks, in score order, until 6 layouts per tower count pass every hard rule (budget 24):
      * fire-tender access: a 6 m strip on each required face, connected to the road; turning by a loop, a dead end of
        20 m or less measured to the far end, a 9 m turning circle (reserved as paving) or a second exit gate;
      * a gate on a road edge, clear of road junctions;
      * a 1:8 ramp near the gate and along the boundary; if it does not fit, a car-lift bay with a penalty;
      * usable garden, slivers, gate-to-tower distance, phasing.
5. **Options** (`place`):
   * the best passing layout for every tower count the plot can legally take (tower count is the client's first
     question);
   * remaining slots filled by structurally different concepts (slab vs point, other arrangement, or clearly different
     angle and footprint);
   * ordered by tower count; `rank` and `recommended` mark the top score;
   * each option gets ranked `reasons` with numbers and comparisons to the other options;
   * `site_elements`: gate, exit gate, ramp, drive, garden;
   * `metrics`, a `typology_hint` for stage 5, and up to 2 runner-up concepts with "why not".

### Hard rules (never traded)

| rule | source (`site.yaml`) | check |
|---|---|---|
| setbacks | `setback` (existing) | plate inside the buildable polygon |
| tower gap | `tower_gap` (existing) | min rect-to-rect distance |
| coverage | `coverage` (existing) | total plate / plot <= 45% |
| FSI | **new** `fsi`: 1.0 / 2.0 / 2.2 / 2.4 / 2.5 by road width, x 1.35 fungible | plates x floors x 0.88 <= permissible |
| fire access | **new** `fire_access`: 6 m, 9 m radius, 9 m max offset, faces 0/1/2/4 at 15/24/45/>45 m, 20 m dead end | see pass 3 |
| road entry | **new** `entry`: 6 m gate, 9 m junction clearance | gate onto >= 6 m deep open ground |
| parking access | **new** `parking_ramp`: plots >= 1,000 m2, 3.5 m (<= 4,000 m2) or 6 m x 30 m, else 6 x 7.5 m car-lift bay | reserved, linked to the drive |
| plate viable | `design.yaml max_core_share` 0.20 | when nothing else fits, toy plates are shown failing this rule, with a height that would work |

New statutory values are the author's reading of DCPR 2034 / NBC 2016 / Mumbai CFO practice and are marked
`verified: false`. The fire faces-by-height table and the 20 m dead end (borrowed from UK ADB B5) are the least
certain.

### Soft criteria (weights in `design.yaml`, normalised)

FSI use 0.20, plate efficiency 0.12, view 0.12 (constant, so no effect, when no view is known), west sun 0.10,
breeze 0.10, garden 0.10, privacy 0.08, alignment 0.08, daylight 0.07, no slivers 0.07, approach 0.06,
phasing 0.02.

Breeze uses the whole wind rose. Wind up to 60 degrees off the facade normal ventilates fully (Givoni), and a tower
within 3 x H downwind of another loses half its exposure. Daylight and privacy blend the mean with the worst quarter of
the facade, because an architect judges a scheme by its worst flats. Neighbours are assumed to build to their own
setback.

## What looking at the images changed (iteration log)

Every one of these came from reading the rendered PNGs, not from the tests:

1. Diagonal drive corridors: replaced by orthogonal L-shaped drives in the plate frame.
2. Fire drive reserved 15 m wide (offset band + buffer): now a true 6 m strip at the nearest clear offset.
3. No 3-tower option on Lokhandwala:
   * the square point-tower size was rounded up;
   * rows cannot fit an irregular plot, so 2-D clusters were added;
   * all 3-tower layouts failed only the ramp, so the car-lift fallback was added (a real Mumbai practice, penalised
     and stated).
4. 16 x 16 m point towers with a 25% core offered at 45 m: core-share cap added.
5. Ramp placed in the middle of the garden: its cost now includes distance from the boundary.
6. Sea plot ignored the sea (16% of the facade toward it): view weight raised to 0.12. Then every option turned
   45 degrees, so the **alignment** criterion was added (basement grid, straight drives, no wedges). The sea-west
   recommendation now faces west across the garden, with the drive on the road side.
7. Large plot: towers jammed against the road and a 3-tower cluster at the minimum gap. Fixes: garden counts all lawns
   of 300 m2 or more, not only the largest piece; approach scales with the plot; privacy target scales with height
   (0.3 x H); daylight/privacy use the worst quarter. Result: staggered twin slabs and a spread triangle of three.
8. "Dead end 0 m" was wrong: it is now measured to the far end of the drive. "Two gates" claimed a gate that was not
   drawn: the exit gate is now reserved, and kept clear of the ramp and garden.
9. Pros marked "+" while another option was better: pro/con now means best/worst of the options shown.

## Measured results (final code)

Review plots (`docs/quality/img/placement-<name>.png`; the recommended option is listed):

| plot | area m2 | H m | time s | options | recommended | FSI use | fire faces, turning | parking | garden m2 | west facade |
|---|---|---|---|---|---|---|---|---|---|---|
| lokhandwala (golden) | 3,400 | 24 | 0.79 | A 1 slab, B 2 point, C 3 point | A: one slab 62x22 | 94% | 1/1, road | ramp | 1,597 | 21% |
| narrow-deep | 3,525 | 45 | 0.24 | A 1 slab, B 2 point, C 2 point | A: one slab 44x18 | 100% | 2/2, 9 m turning circle | ramp | 1,511 | 36% |
| wide-shallow | 5,214 | 32 | 0.77 | A 1 slab, B 2 slab, C 3 slab | B: two slabs, staggered 48x20 | 99% | 2/2, exit gate | ramp | 1,515 | 15% |
| corner-two-roads | 4,658 | 45 | 0.60 | A 1 slab, B 2 slab, C 3 point | A: one slab 57x20 | 100% | 2/2, 9 m turning circle | ramp | 2,544 | 13% |
| pentagon | 6,215 | 60 | 0.96 | A 1 slab, B 1 point, C 2 point | A: one slab 48x22 | 100% | 4/4, loop | ramp | 3,870 | 20% |
| sea-west (view 270) | 5,532 | 70 | 0.65 | A 1 slab, B 1 point, C 2 point | A: one slab 44x20 facing the sea | 99% | 4/4, loop | ramp | 3,470 | 35% |
| large-3-towers | 19,578 | 90 | 1.21 | A 1 slab, B 2 slab, C 3 slab | B: two slabs, staggered 66x18 | 99% | 4/4, loop | ramp | 13,450 | 11% |

All 21 options shown pass every hard rule. Runtime is at most 1.25 s (budget 3 s).

Random-plot benchmark: 4-7 corners, 1,500-20,000 m2, heights 24-90 m, road 9-24 m, 1-2 road edges, 60 plots per seed.
Every option was independently re-checked for setback containment, gap, FSI and coverage.

| seed | mean / p95 / max s | 3 options | no option | recommended passes all rules | invariant violations |
|---|---|---|---|---|---|
| 7 | 1.07 / 1.86 / 2.20 | 47 | 3 | 53/60 | 0 |
| 11 | 1.01 / 1.69 / 2.56 | 46 | 1 | 55/60 | 0 |

The failures are honest and explained:
* `plate_viable` (7 plots): tall towers on small plots, where the FSI over 23-30 floors leaves plates whose core would
  take 22-69% of them. The reason names a height that works.
* `fire_access` (2 plots): no drive with turning fits.
* `parking_access` (1 plot).
* "No option" (4 plots): plots of 1,300-1,700 m2 at 70-90 m, where the FSI allows a plate under the 200 m2 minimum.
  The engine then raises its generic "No tower fits" error. Recommended FSI use averaged 92-94%.

Tests:
* `test_placement_v2.py` (19 tests): golden plot, six synthetic plots, explanation content, legacy model loading,
  unverified-rule flags, the toy-plate path;
* the existing `test_rules_plate_units`, `test_engine_server_bridge`, `test_settings_triangle` and the
  `golden.gate_place` gate still pass;
* the full engine suite passed (356 tests).

UI: `PlacePanel.tsx` shows all criteria present in `score_terms` (v1 projects show "–" for missing terms), the
concept and rank in each column header, the ranked reasons with + / − / ✓ / ✗ marks, the typology hint, FSI use, and
the runners-up with "why not". It type-checks (`tsc -b`) and was smoke-rendered server-side with real engine output.

## Limits (honest)

* **Rectangles only.** Plates are rectangles. L/T/H/cruciform shapes come from stage 5 inside the rectangle (the
  `typology_hint` suggests which). Towers in one option share one size, and more than 3 towers is not explored.
* **Approximate drives.** Drives are straight strips and L-connectors clipped to open ground, not routed paths. A
  connector may cross a lawn corner, and no turning path for the fire tender is simulated.
* **Approximate climate.** No sun-path shadow casting: daylight is spacing / height. The wind is a rose, not CFD. The
  view is a bearing, with no knowledge of what neighbours block.
* **Unknown neighbours.** They are assumed to build to their own setback. Real neighbour buildings, trees, nalas and
  reservations are not known.
* **Simplified statutory model.** FSI is the suburbs table only (island city, 33(7)/33(9)/SRA differ). RG is scored,
  not enforced (DCPR allows part on the podium). Podium/stilt parking counts are not computed: only the ramp or
  car-lift reservation.
* **Policy and weights.** The weights are the author's judgement, tuned on 7 plots by looking at them. An architect
  should review them, and ideally they should be fitted to approved site plans (the 655 mined pages hold floor plans,
  not site plans).
* **Empty results.** When nothing fits, `Placement` cannot carry a diagnostic (the model may not change), so the engine
  error stays generic.
