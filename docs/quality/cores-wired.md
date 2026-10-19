# Cores wired into the plate, and plan quality in the UI (Phase 2)

Before: `plate.py` drew one kind of core per template (a box of `core_w x core_d` with stairs, lifts and a lobby laid
into it). After: each rectilinear template takes the best-ranked core from the library (`cores.py`, see
`typology-cores.md`) for its plate type, height and flats per floor, places it where the template wants it, and the user
can switch to any other core type that fits. The triangle is unchanged (experimental). The plan critic
(`quality.critique`) is now visible as a per-flat scorecard.

## 1. Engine

### What `build_plate` does now (non-triangle templates)

1. `plate_type_of(shape, params)` maps the template to the typology vocabulary: `central_core` is `point_square` (or
   `linear_slab` when the envelope aspect is >= 1.9), `side_core` is `linear_slab`, `cruciform`, `h_shape` -> `H`,
   `t_shape` -> `T`.
2. `cores.choose_core(plate_type, flats_per_floor, height, outline)` ranks the six core types (code compliance,
   core share of the plate, suitability for the plate type; reasons per type).
3. Every ranked type is **placed** in the template (`_place_core`, rotations tried in order):

   | core type | main cluster | satellite cluster (second stair) |
   |---|---|---|
   | `central_compact`, `scissor` | at the template's own core position: centre; junction of a T; end of a side-core slab | none |
   | `linear_corridor` | same position | stair 2 at the far end of the template's longest corridor |
   | `split_junction` | same position (H: the left wing junction) | cluster B at the far end of the longest corridor (H: the right wing junction) |
   | `side_core` | slid against an exterior wall with its back to it (south façade; end wall of a side-core slab) | none |
   | `end_core` | slid to the short end wall of the slab (left end) | stair 2 at the far end of the corridor |

   The **core zone** is the bounding box of each cluster (so one box, or two when there is a satellite). The passage is
   the ring round the core boxes, the template's corridors, and a link from any box that does not touch a corridor (a
   façade core slid to a wall). Flats are what is left, as before, so `flats + core + passage + shaft == plate`
   holds by construction, and every flat still touches the passage ring, hence the lobby.
4. A placement is **rejected** when it does not fit inside the outline, or when it leaves the plate mostly passage:
   less than 30 % of the plate as flats, or less than 80 % of the best placement's flats share (an H with a
   split core in a 9 m bridge turned 100 % of the plate into passage in the first version). Each alternative reports
   why it was rejected.
5. `auto` = the best-ranked placed, code-compliant type. If the user asked for a type that cannot be placed, auto takes
   over and the answer carries a note. If **no** library core fits (a 36 x 20 m T at 45 m: stem 14.4 m wide and 10 m
   long, clusters 16 m), the template's own box core stays (`core_type_used = 0`), and the alternatives say why.

### Parameters and data

* `core_type` plate parameter: 0 = auto, 1..6 = `central_compact, linear_corridor, scissor, split_junction, side_core,
  end_core` (`plate.CORE_ORDER`). `set_plate` / `preview_plate` also accept the name (`"auto"`, `"scissor"` ...).
* `refuge` plate parameter (0/1): the core of a refuge floor (refuge area against the main cluster). Not in the UI.
* Written back to the params: `core_type_used` (what was placed, 0 = template box), `height_m` (the height the plate
  was built for), `core_w` / `core_d` (the main cluster in the plate frame; with a library core these are results, not
  inputs).
* `set_plate`, `preview_plate` return the core under the key `core`; the new RPC **`core_options(tower_id)`** returns the
  same for the saved plate (whitelisted in `server.py`). Shape of `core`: `mode` (`library` or `template`), `selected`,
  `auto`, `requested`, `note`, `alternatives[]` (type, label, score, `core_share` of the plate, `flats_share`, compliant,
  placed, `why_not`, reasons, authority-acceptance flag, recommended, chosen), `parts[]` with the library's exact kinds,
  `counts`, `dims_m`, `requirements` (stairs, lifts, fire lifts, clauses), `reasons`, `where`.
* `preview_plate` used to build at the default 24 m; it now passes the site height like `set_plate`.
* **`plan_quality(unit_id)`** runs `quality.critique` on one solved flat and returns score, grade,
  base score, sub-scores per category, weakest link, number of critical findings and the six costliest findings
  (severity, message, rooms, spot). It is its own call, not part of solving: the critic takes about 0.15-0.2 s per flat. The
  engine caches each answer until the plan, the unit or the plate changes (second call measured at 1.3 ms). Whitelisted
  in `server.py`; not added to `mcp_server.py` (not needed).

### `core_parts` kinds and a contract limit (needs a one-line change in `models.py`)

`models.CorePart.kind` is a frozen `Literal["stair", "lift", "fire_lift", "lobby", "service"]` and `models.py` is outside the
allowed files for this task. So `plate.core_parts` carries the library's `smoke_lobby` as `lobby` and its `shaft` and
`refuge` parts as `service`. The **exact** kinds (`stair, lift, fire_lift, lobby, smoke_lobby, shaft, refuge`) are in
`core_info(...)["parts"]`, which the UI uses for its legend. `plate._model_kind` reads the Literal at import time,
so extending the Literal in `models.py` (and `types.ts`) is the only change needed to make `core_parts` exact too.

## 2. UI

* `ShapePanel`: a **Core** panel. A select with *Auto: (best ranked)* plus the six types, each with its core share of the
  plate and "does not fit" when it cannot be placed here; a row per alternative (core share, flats share, score bar, why
  not); reasons for the chosen core; core box size, stairs / lifts, and a legend of its parts with counts. Choosing a
  type calls `set_plate` with `core_type`. With a library core, the `core_w` / `core_d` sliders and the core drag
  handle on the plan are hidden (the code sizes the core).
* `PlateCanvas`: the legend names the core in use ("core: central compact (auto)"), and the dashed cross is explained for
  shafts and refuge. Parts are drawn as before (stair treads, lift crosses, lobby tint).
* `DiagramPanel`: a **Plan quality** scorecard under the stored / last plan of the selected flat: grade, score out of
  100, a bar per sub-score (circulation, waste, sequence, furniture, daylight, doors), what holds the score back, the top
  five issues with severity. It loads after the plan is shown (skeleton while waiting), and retries on error.
* `ReviewPanel`: a **Plan quality** panel with "Score the flats" (one RPC per flat, in turn): grade and score per flat, the
  costliest finding, and the mean. Nothing runs until the button is pressed.

## 3. Measured

Verification run on this branch (numbers are what I measured):

* Engine tests for this change: `tests/test_rules_plate_units.py`, `tests/test_tshape.py`, `tests/test_settings_triangle.py`,
  `tests/test_engine_server_bridge.py`: all pass. New `tests/test_cores_wired.py`: 56 tests pass (5 templates x 4 envelopes x
  heights 24-60 m: zones add up, library core, typed parts inside the core zone, code counts, every flat reaches the
  lobby; every core type forced on a slab; satellites; refuge; triangle untouched; the engine RPCs; RPC whitelist).
* `npx tsc -b --noEmit`: clean. `npx vite build`: built.
* Build time of one plate (varied parameters, so uncached, other jobs on the machine): 46-140 ms per `build_plate`; the
  old box core was a few ms cheaper. Repeats of the same parameters hit a small cache.

Flats share of the plate, old box core -> library core (zone of flats / plate; `split_units` count unchanged in every row; no flat lost
its access to the lobby):

| envelope (m), height | central_core | cruciform | h_shape | t_shape | side_core |
|---|---|---|---|---|---|
| 44 x 24, 45 m | 78 -> 80 % | 70 -> 71 % | 63 -> 65 % | 72 -> 74 % | 78 -> 76 % |
| 36 x 20, 24 m | 45 -> 75 % | 25 -> 54 % | 37 -> 42 % | 57 -> 56 % | 48 -> 73 % |
| 52 x 18, 60 m | 55 -> 65 % | 25 -> 47 % | 66 -> 66 % | 60 -> 60 % | 56 -> 65 % |
| 30 x 30, 24 m | 75 -> 79 % | 62 -> 58 % | crash -> 15 % | 72 -> 57 % | 77 -> 80 % |

Where it got worse: the 30 x 30 T (the library cluster is 13.7 m wide and does not fit the 12 m stem, so it lies along
the stem: 57 % flats against 72 %), the 30 x 30 cruciform (-4 points). The 30 x 30 H crashed before (empty flat zone in
`_cut_features`); it no longer crashes, but the plate is nearly all passage (15 % flats): a 30 m H has no room for a
core. That is what the table says; the template ratings do not warn about it.

Lokhandwala demo (`planner demo` flow, cruciform, 61.5 x 22 m plate, 24 m, `parallel=2` instead of the CLI's 3 because
the machine has 4 shared cores): core `central_compact` (core 7.6 % of the plate); zones flats 746.5 / core 74.9 / passage
129.1 / shaft 37.2 m2 = 987.7 m2; 7 flats, 7 / 7 solved in 32.1 s; rule checks 145 pass, 0 fail, 3 review; carpet 670.5 m2 (4 x 2BHK, 3 x 3BHK). The critic scored the seven flats 62.3, 66.7, 75.2, 55.6, 89.3,
52.5, 75.7 (mean 68.2); 0.15 s for the first one, 1.3 ms when repeated. I did not run the same demo on the old core, so I
make no before / after claim on the solved plans.

Images (matplotlib, flats cut by `split_units`, core parts coloured by exact kind):

* `img/cores-wired-cruciform.png`, `img/cores-wired-t_shape.png`, `img/cores-wired-side_core.png`: the Lokhandwala plate (61.5 x 22 m, 24 m).
  Cruciform and T take `central_compact`; the slab takes `linear_corridor` (stair 2 at the far end of the corridor).
* `img/cores-wired-h_shape-45m.png`: 44 x 24 m H at 45 m: `split_junction`, one cluster in each wing junction, fire lift in cluster A.
* `img/cores-wired-alternatives.png`: the six core types forced on the same slab (all six place; core share 5.1-7.3 %, flats 72-83 %).

## 4. What is not done, and honest caveats

* **Two tests in `tests/test_solver_drafting.py` now describe a different plan** (both outside the files I may edit; the full
  engine suite otherwise passes, those are the only two failures):
  * `test_fix_ladder_finishes_the_plan` uses the 40 x 22 m T-shape APT-1 as "a flat where a 4BHK cannot fit" (87 m2). With the
    smaller library core that flat is 120 m2 (measured, old vs new), the 4BHK solves directly, `applied_fix` is `None`.
  * `test_v2_plan_enters_living_with_little_circulation` (52.9 x 29 m cruciform, first 2BHK): the flat grows from 88 to 105 m2
    (measured), and the solver now leaves a 5.2 m2 store (the test allows < 5.0 m2) plus two small ones. Re-solved with the
    old plate in the same session: no store at all. So it is the larger flat, not a solver change.
  Neither is an engine bug; both want a new premise (another flat of about 87 m2; a threshold or a different flat).
* `models.CorePart.kind` limit above.
* The core library's rules (`# ASSUMPTION` constants in `cores.py`: persons per flat, lift car, refuge share) are unchanged and still
  the first thing the architect should confirm; the wiring inherits them.
* Placement is geometric and rule-of-thumb (slide to a wall, far end of the corridor); it does not check the 22.5 m
  travel distance from every flat door to a stair on the plate. The core library checks it on the core alone. The 'linear' and 'split'
  satellites put stair 2 at the far end, which is where that rule wants it, but no number is computed.
* Old saved projects keep their box core until the template is refitted; `core_options` says so (`mode: template`).
* Imported-DXF plates keep their own outline and core.
* UI components were type-checked and built, not exercised in a browser in this session.
