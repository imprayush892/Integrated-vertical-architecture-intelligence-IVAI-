# Design note: spatial graph dataset v2 (flat grouping, outline, core, flat frames)

Owner of: `engine/tools/extract_pdf_topology.py` (changes), `engine/tools/build_graph_dataset.py` (new),
`data/mined/graphs_v2.jsonl.gz` (4.1 MB), `engine/planner_engine/data/rules/approved_layout_patterns.json` (27 KB).

## What

A per-page, per-flat graph dataset the new generator can learn from. Each kept flat is a graph of rooms
(type, area, simplified polygon, centroid, facade sides, hop depth from the entrance) with door / open / wall
edges, its entrance door and entrance room, a local frame anchored on that door, every centroid and polygon
also in that frame, vectors from the living room to every room, and the flat's position relative to the
building core. Each page also carries the building outline polygon and the core (lifts, fire lifts, stairs,
lobbies, common passages, shafts) as typed polygons, in metres.

## Why

The audit (`docs/DATASET_VALIDATION.md`) found rooms and links good but flat grouping at 40 %: priors learned
from those flats mixed fragments (bedroom + toilet "1BHK") and merges (two tenements). A generator that has to
reproduce "enter into the living room, kitchen near it, bedrooms away from the entry, flat door off the lobby"
needs whole, correct flats with positions and directions, not only adjacency counts.

## How

### Extractor changes (`extract_pdf_topology.py`; the helper API other tools import is unchanged)

| Problem (audit item or sample page) | Change |
| --- | --- |
| Audit 1: narrow private passage read as common | `is_common` takes the width from the `M.W.` label, else the smaller printed dimension, else the inscribed width (distance transform), never the bounding box. Threshold 1.35 m (1.2 m for `M.W.` labels). |
| Audit 2: rooms with no link dropped | Living-less fragments without a kitchen attach to the flat they share most wall with, unless that gives it a second kitchen or living. Not attached: unnamed cells against the core or in the stair hall, cells with no label at all that touch the flat only through walls (ducts, ledges), and fragments whose only openings lead into a non-residential space (61_4: the fitness centre's toilet). |
| Audit 3: links across lifts / party walls | Links between room types never joined by a door are dropped (lift or stair to anything but lobby / passage, living-living, kitchen-kitchen, toilet-toilet, bedroom-bedroom, kitchen to bedroom or toilet). Each living room anchors one flat; rooms go to the anchor reached in the fewest hops, so one surviving bad link no longer merges two flats. |
| Audit 3: unlabelled lobby cells | An unlabelled cell off the lobby is common when it touches a lift or stair, serves two flats, or opens on to the lobby wider than a flat door, unless it opens over >= 1.3 m on exactly one flat's living / dining / alcove (then it is that flat's foyer: 54_8 T.62). An unnamed cell with printed dimensions >= 2.7 m wide and >= 12 m2 is a room whose name was lost, never a lobby nook. |
| Lift region flooding the lobby (74_3) | A lift region >= 1.4x its printed area has leaked into the lobby in front of it: links from it to entry-type rooms are kept and it counts as the lobby for the entrance (`common_type: lift_leaky`). |
| Living label lost (47_7) | Anchors also include large unnamed cells (>= 12 m2, >= 2.5 m clear) that open on the core and on >= 2 private rooms (`anchor_src: lost_label`), and multipurpose rooms of PAP / rehab tenements (`anchor_src: multipurpose`). Only real living rooms merge with each other through open links. |
| Audit 4: exterior test | Outside = free space reachable from the sheet border after bridging collinear wall gaps up to 2 m (windows); the watershed exterior marker is only placed there, so corner rooms are no longer clipped. Facade = room boundary facing that space across at most 0.42 m of wall, per side (top / bottom / left / right). |
| Audit 5: passages | Private passages and cells join the flat that reaches them; unnamed exterior strips, voids (>= 15 m2) and cells with no opening are pruned from flats (rooms with a lost label are exempt). |
| Audit 6: classifier | `BUILDING` no longer reads as dining; OCR splits recovered (`\| IVING`, `IVIN`, `TOI.`, `IFT-01`, `ECK`); `BEDROOM ... CHAJJA` keeps the room; `TOILET EVACUATION ...` (toilet label merged with the fire-lift label) keeps the toilet; `(MVS) P.TOIL` is a toilet, not a shaft; `DECK` is a balcony; `STRETCHER` is a lift; a room-sized (>= 6 m2, >= 2 m wide) "chajja" region opening into a room is a room whose own label was lost; non-residential spaces (refuge, parking tower, society office, school, fitness centre, substation, O.T.S.) are typed `nonres` and never join a flat. |

New per page: building outline (not-outside space plus labelled rooms, opened and hole-filled, components
holding >= 2 labels and >= 25 m2); stair flights from tread-line runs (>= 6 parallel equal lines at
0.18-0.38 m pitch); lifts from labels, from a corner-to-corner X in an unnamed cell, or from an unnamed
lift-sized cell opening only on the lobby next to a named lift; cores = common rooms, stairs and shafts within
0.7 m (1.6 m for stairs) of each other, holding a lift or stair. Room polygons are vectorised from the label
image (run-length union, simplified at 5 cm), metres, y up.

### Dataset builder (`build_graph_dataset.py`)

Stage 1 runs the extractor on all pages: resumable (a page is done once its marker row is in the raw JSONL;
an interrupted run restarts where it stopped), at most 2 processes. Stage 2 applies the quality checks and
writes one record per page.

* Frame: origin at the entrance door point (the opening between the entrance room and the lobby), x along the
  wall direction (0 / 90 / 180 / 270 degrees) in which the flat's facade reaches deepest from that door,
  y = x turned 90 degrees anticlockwise. Plans are orthogonal, so snapping keeps the frame stable where the
  raw door-to-facade direction is near 45 degrees.
* Core relation: the core holding the lobby room the door opens on (else the nearest core), vectors core
  centroid -> entrance and -> flat centroid, the side of the core bounding box the door is on, and the core
  direction seen in the flat frame.
* Quality checks: exactly one entrance; has living (or PAP multipurpose), kitchen (or a cooking alcove in a
  PAP / `K.P.` alcove) and toilet; no merge (<= 1 kitchen, <= 2 living, <= 350 m2, <= 30 rooms); polygons for
  every room; and the 25 % dimension test.
* Dimension test, per room with printed L x W, except open-plan zones: living / dining / foyer / passage /
  alcove rooms joined by open links are compared as one area (sum of regions vs. sum of printed areas),
  because no wall separates them and the split between their regions is arbitrary. Each flat record carries
  the strict per-room maximum too (`qc.dims_dev_max_room`, `qc.strict_dims_ok`) so a learner can use the
  strict subset. Flats failing only this test but within 40 % are kept apart as `flats_relaxed`.

## Measured

Scoring unit: an emitted flat is **grouping-correct** when it holds every major room of one real apartment
(living, kitchen, bedrooms, toilets) and no room of another apartment; missing small decks / balconies /
service cells and stray non-room cells (a shaft, a ledge) are counted as defects, not failures. Ground truth is my
reading of the overlay renders (fill = room region, red box = core, star = entrance, blue / green dot = open / door link).

### Re-audit of the 10 audited pages (final code, all 10 overlays looked at by eye)

| # | Project / page | Plan | Emitted flats (v1 audit) | Emitted flats (v2) | Grouping-correct v2 | Notes |
| - | --- | --- | --- | --- | --- | --- |
| 1 | 33 CHEMBUR_W p3 | 3+3+1 BHK tower | 3 (1 ok) | 3 | 3 / 3 | master toilets and kitchens now kept |
| 2 | 40 PAHADI_GOREGAON_W p5 | 3 x 2BHK | 3 (0 ok) | 3 | 3 / 3 | the 0.91 m passages no longer cut the flats |
| 3 | 53 PAHADI_GOREGAON_W p1 | 3 x 2BHK | 3 (2 ok) | 3 | 3 / 3 | F1 includes its study room |
| 4 | 54 BHULESHWAR p8 | rehab tenements | 2 (1 ok) | 3 | 2 / 3 | F2 (13 rooms, three bedrooms) is possibly two tenements; counted as wrong |
| 5 | 61 DADAR_NAIGAON_F_S p4 | 1BHK rehab | 2 (1 ok) | 3 | 3 / 3 | fitness centre excluded; the third flat has no entrance found, so QC drops it |
| 6 | 62 BANDRA_C p0 | 2 x 3BHK | 3 (2 ok) | 2 | 2 / 2 | parking block no longer a flat |
| 7 | 69 MATUNGA p0 | 3BHK + 2BHK rehab | 2 (2 ok) | 2 | 2 / 2 | |
| 8 | 74 BANDRA_B p3 | 4 x 1BHK rehab | 2 (0 ok) | 4 | 4 / 4 | F4 has an unlabelled ledge attached (defect) |
| 9 | 75 BANDRA_B p9 | 2 x 3BHK | 3 (0 ok) | 2 | 2 / 2 | |
| 10 | 76 BHULESHWAR p4 | 2BHK + school | 2 (1 ok) | 1 | 1 / 1 | school typed non-residential |
| | **Total** | | **25 (10 ok, 40 %)** | **26** | **25 / 26 (96 %)** | real apartments recovered: 25 of 26 |

Target: >= 75 % on these pages: met. These pages were used to tune the fixes, so 96 % is an optimistic number.
Overlays: `img/dataset-audit-40_5.png`, `img/dataset-audit-74_3.png`, `img/dataset-audit-33_3.png`.

### Pages not used for tuning

| Sample | Emitted flats grouping-correct | Real apartments recovered |
| --- | --- | --- |
| Held-out 1 (6 random pages), first look | 18 / 26 (69 %) | 18 / 30 (60 %) |
| Held-out 1, final code | 25 / 30 (83 %) | 25 / 30 (no longer held out) |
| Held-out 2 (6 random pages, untouched), final code | **25 / 32 (78 %)** | **25 / 37 (68 %)** |

The 78 % figure on untouched pages is the honest generalisation estimate (+-8 points: 32 flats, one reader).
I did not re-score the kept set of the full run flat by flat; its quality rests on these samples plus the QC filters below.

## Dataset (full run)

`graphs_v2.jsonl.gz`: 655 pages processed with 2 workers (resumable; about 65 min wall on the shared CPU), 649 pages usable
(3 segmentation failures, 2 non-plan sheets, 1 low-fidelity), 649 with a building outline and a core (645 with a lift, 634 with a stair).

| | Count |
| --- | --- |
| Raw emitted flats | 3 137 |
| Kept (clean flat: one entrance, living + kitchen + toilet, no merge, rooms within 25 % of printed dims) | **1 264** on 442 pages, 47 of 50 projects (1 257 apartments, 7 PAP tenements) |
| Kept, unique per project and signature | 974 (967 apartments) |
| Kept by BHK (bedroom + master) | 0: 16, 1: 211, 2: 731, 3: 266, 4: 38, 6: 2 |
| `flats_relaxed` (fail only the 25 % dims test, within 40 %) | 444 |
| Rejected, by failed check (a flat can fail several) | dims within 40 %: 887, one entrance: 527, no living: 286, dims within 25 %: 185, no kitchen: 154, no toilet: 141, no merge: 59 |
| Entrance room of kept flats | living 1 130 (89 %), unknown 66, unlabeled 29, passage 23, dining 11, multipurpose 3, foyer 2 |
| Flat door opens on | lobby 809, common passage 304, unlabelled 92, unknown 43, stair 13 |
| Strict per-room dims within 25 % | 1 009 of 1 264 |

Record per page: `flats[]` (kept), `flats_relaxed[]`, `rejected[]`, `outline`, `cores`, `common_rooms`. Per kept flat: `nodes`
(type, area, centroid, simplified polygon, facade sides and length, hop depth, centroid and polygon in the frame), `edges`
(`door` / `open` / `wall`), `entrance` (room, point, door kind, lobby), `frame`, `living_vectors` (angle, distance from the
living centroid to every room), `core` (vector core -> entrance, core -> flat centroid, side of the core the door is on, core
direction in the flat frame), `signature`, `qc`. `img/dataset-flat-frames.png` shows eight kept 2BHK flats in their frame.

## Pattern summary for the generator (`approved_layout_patterns.json`)

`python -I engine/tools/build_graph_dataset.py x data/mined/graphs_v2.jsonl.gz --raw x --patterns-only --patterns-out
engine/planner_engine/data/rules/approved_layout_patterns.json` recomputes it from the dataset (about 2 s).

* Basis: the 1 257 kept apartments (PAP tenements excluded), one flat per project and signature (967), grouped by BHK
  `1`, `2`, `3`, `4+`, `all`. Every figure carries `n` (rooms for direction and hop figures, flats otherwise) and `n_projects`;
  a figure resting on fewer than 5 projects is dropped, and so is a BHK group below 5 projects. Group sizes: 1BHK 152 flats / 34 projects,
  2BHK 524 / 40, 3BHK 242 / 37, 4+ 40 / 15, all 967 / 47.
* `direction_from_living`: angle histogram (8 sectors of 45 degrees, flat frame: 0 = towards the deepest facade, 180 = back towards
  the entrance door) and distance percentiles from the living centroid to kitchen, master, bedroom, any bedroom, toilet, dining.
* `hops_from_entrance`: histogram and median of the hop depth per room type.
* `entrance_room`: share of flats whose entrance room is living, foyer, dining, passage or other.
* `hub_patterns`: which room each bedroom's door or opening leads to (the hub nearest the entrance): living / dining / foyer / passage /
  `unnamed` (an unlabelled cell, in practice a passage whose label was lost) / `none` (no link to a hub found); plus the commonest
  per-flat combinations (projects >= 5).
* `passage`: share of flats with a labelled passage, and the passage area share of the flat area (percentiles, all flats and flats with a passage).
* `area_m2`, `n_rooms`: percentiles.

Headline values (all-BHK, 47 projects): the flat's front door opens into the living room in 89 % of flats (1BHK 85 %, 2BHK 95 %,
3BHK 81 %); a foyer entrance is almost absent in this dataset (0.2 % of flats), so the generator should not treat "foyer entry" as a common Mumbai pattern
from this evidence. Bedrooms lie towards the deep
facade: 55 % of bedrooms in the 0-degree sector from the living centroid (2BHK 61 %), median distance 6.3 m; kitchens split between the two
forward-diagonal sectors (NE 30 %, SE 30 %, median 3.5 m, i.e. beside the living room, not behind it); toilets follow the bedrooms (47 % at 0
degrees, median 5.7 m). Hop depth medians: living 0, dining 1, passage 1, kitchen 2, bedroom 2, master 3, toilet 3. Bedroom doors open onto a
passage in 34 % of bedrooms, an unlabelled cell (probably a passage) in 29 %, the living room in 17 %, dining in 9 %, no hub found in 11 %.
Labelled passages take a median 0 % of the flat (48 % of flats have one; where present: median 4 %, p90 6 %).

## Re-audit limits of the patterns

* The living label anchors the frame vectors; an `unknown` entrance room (66 flats) is kept but is probably a mislabelled living.
* `unnamed` hubs hide a lost label, so the passage share is a lower bound; treat `passage` + `unnamed` (63 % of bedrooms) as "bedroom door opens
  on a passage".
* Direction bins depend on the frame rule (x to the deepest facade); in a flat whose deepest facade is the living side the bedroom angles
  shift. The histograms are frame-relative, not compass.
* Rooms in a flat are not independent and typical floors repeat inside a project; the `n_projects` figure is the honest sample size.

## Limits

* The >= 75 % target is met on the audited pages (96 %) and on a fresh untouched sample (78 %), but recall on
  the fresh sample is 68 %: whole apartments are still missed when their living label is lost on a sheet
  without door arcs, or when PAP rooms are labelled `MP ROOM` (not recognised), in which case neighbouring
  tenements merge (49_13).
* Labels drive everything: a room whose label OCR fails is typed `unknown` / `unlabeled`, and a room flooded
  by a neighbour's label (36_13 toilet under a passage label) is lost.
* Two flats sharing an unlabelled vestibule can swap a bedroom (29_7).
* Stairs are tread-line hulls (no landings); core polygons are the union of the parts buffered 0.35 m, so they
  are approximate shapes, good for position / side / size, not for drafting.
* Outline: openings wider than 2 m in the outer wall let the outside in (rare); balconies are inside,
  chajjas outside.
* Facade sides are in the drawing frame; sheets have no reliable north arrow, so no compass claims.
* Typical floors repeat across pages; `signature` (types + rounded areas) lets a learner de-duplicate.
* Ground truth is one reader's judgement on 22 pages; read the percentages as +-8 points.
* 3 of 50 projects have no kept flat; 1 429 raw flats were rejected, mainly on the dimension test (a printed-dimension mismatch beyond 40 %) and on a missing or double entrance.
* BHK label vs bedroom nodes (reviewer check): in 228 of 1 264 kept flats (18 %) the `bhk` value exceeds the number of nodes typed `bedroom` (197 by one, 31 by two or more), mostly master bedrooms labelled "UNK" or unlabelled (e.g. 33_3 F3). Treat `bhk` as the drawing's own count, not as a bedroom-node count, when training.
