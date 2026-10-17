# Design note: spatial graph dataset v2 (flat grouping, outline, core, flat frames)

Owner of: `engine/tools/extract_pdf_topology.py` (changes), `engine/tools/build_graph_dataset.py` (new),
`data/mined/graphs_v2.jsonl.gz`. Results, counts and example renders: [dataset-v2.md](dataset-v2.md).

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
service cells and stray non-room cells (a shaft, a ledge) are counted as defects, not failures. **Exact** =
no defect at all. Ground truth is my reading of the drawings.

| Sample | Emitted flats grouping-correct | Real apartments recovered | Notes |
| --- | --- | --- | --- |
| Audit pages (10, same as DATASET_VALIDATION.md), before (v1) | 10 / 25 (40 %) | 10 / 27 (37 %) | from the audit |
| Audit pages, final code | **25 / 26 (96 %)**, exact 21 / 26 | 25 / 26 | fixes were tuned on these pages |
| Held-out 1 (6 random pages, other projects), first look | 18 / 26 (69 %) | 18 / 30 (60 %) | true held-out number |
| Held-out 1, final code | 25 / 30 (83 %) | 25 / 30 | then used for tuning: no longer held out |
| Held-out 2 (6 random pages, untouched), final code | **25 / 32 (78 %)** | **25 / 37 (68 %)** | honest generalisation estimate |

Dataset counts (flats kept by BHK, pages with outline / core, QC failures) are in
[dataset-v2.md](dataset-v2.md).

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
