# Dataset validation: PDF topology extraction vs. the drawings

Scope: `engine/tools/extract_pdf_topology.py` (output `topology.jsonl`: 649 plan pages, 3 665 flat records) checked by eye against
the source drawings, and what that means for `engine/planner_engine/data/rules/mumbai_topology_priors.json`.
Nothing in the extractor or the priors was changed. This is an audit, not a fix.

## Method

* 10 pages from 10 different projects of `Residential_Projects_28_to_77`, chosen to mix 1/2/3 BHK, rehab tenement sheets, sale
  towers, pages with door arcs (5 of 10: projects 62, 69, 74, 76, plus 33 which has 19 arcs but 0 links) and without (the rest).
* Each page was re-run through the extractor's own `_process_page` (debug capture of the label image, room table and door arcs),
  rendered with pymupdf (100-200 dpi crop of the plan) and overlaid with the extracted rooms, links, entrance and exterior flags.
  I looked at the raw crop and the overlay for every page and zoomed in on doubtful wall openings.
* Scoring unit: every flat record emitted for the page (25 flats, 2 of them non-apartments), compared with the apartments I can
  see on the drawing (27 apartments on these 10 pages).
* Overlay legend: fill = room region (one colour per room), magenta = boundary between two labelled regions, red box = entrance room,
  `*` = extractor `exterior` flag, green dot = door-arc link, blue ring = open-gap link, `F#` = flat membership.
* Limits: ground truth is my reading of the drawing. Links are verified only where an opening or an arc is visible; counts are good
  to about +-1 per flat and the recall figures only count misses I could see. 10 pages is a sanity check, not a statistical sample.
  Dataset-wide figures further down are computed directly from `topology.jsonl` and are exact.

Overlay examples (jpeg, in `docs/dataset-validation/`):

| Image | Shows |
| --- | --- |
| `p10_76_clean_door_arcs.jpg` | Best case. Project 76 p4, 2BHK with door arcs: rooms, boundaries, doors right. One misassigned arc (bedroom-1/bedroom-2) and the entrance arc not linked. |
| `p02_40_fragmented_flats.jpg` | Project 40 p5, three 2BHK flats and no arcs. A 0.90 m passage is flagged "common", so each flat is cut in pieces and two flats are merged through an unlabelled lobby cell. |
| `p08_74_spurious_door_merge.jpg` | Project 74 p3, four 1BHK tenements. Arcs are assigned across party walls and lift doors, so flats are merged in pairs. Also shows `BUILDING LINE` text typed as dining. |

## Per-page results

Columns: rooms = rooms inside emitted flats; typed = rooms with a correct type (untyped = `unknown`/`unlabeled`, counted as not
correct); bounds = rooms whose region matches the real room (not merged, not clipped by more than about 15 % of its area);
group = emitted flats that are exactly one real apartment (rooms complete, no foreign rooms); entr = entrance room is a real
front-door room; links = true positives / false positives / false negatives of door and open links among the apartment's rooms and
to the lobby; ext = exterior flag correct for bedroom, living, kitchen and toilet rooms.

| # | Project / page (0-based) | Plan | Arcs found / links | Emitted flats (real apts) | rooms | typed | bounds | group | entr | links tp/fp/fn | ext |
| - | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | 33 CHEMBUR_W p3 | 3+3+2 BHK, tower | 19 / 0 | 3 (3) | 24 | 22 | 19 | 1/3 | 3/3 | 24/1/2 | 18/21 |
| 2 | 40 PAHADI_GOREGAON_W p5 | 3 x 2BHK | 0 / 0 | 3 (3) | 15 | 13 | 13 | 0/3 | 1/3 | 24/0/0 | 12/12 |
| 3 | 53 PAHADI_GOREGAON_W p1 | 3 x 2BHK | 1 / 0 | 3 (3) | 20 | 16 | 16 | 2/3 | 3/3 | 18/3/3 | 10/15 |
| 4 | 54 BHULESHWAR p8 | rehab tenements, 2BHK | 0 / 0 | 2 (3) | 28 | 12 | 24 | 1/2 | 1/2 | 8/1/1 (F1 only) | 9/11 |
| 5 | 61 DADAR_NAIGAON_F_S p4 | 4 x 1BHK rehab | 0 / 0 | 2 (4) | 17 | 12 | 13 | 1/2 | 2/2 | 14/4/3 | 8/11 |
| 6 | 62 BANDRA_C p0 | 2 x 3BHK podium (+ parking) | 23 / 12 | 3 (2) | 21 | 18 | 19 | 2/3 | 2/2 | 21/2/1 | 15/17 |
| 7 | 69 MATUNGA p0 | 3BHK + 2BHK rehab | 16 / 12 | 2 (2) | 18 | 15 | 15 | 2/2 | 1/2 | 17/6/1 | 12/12 |
| 8 | 74 BANDRA_B p3 | 4 x 1BHK rehab | 17 / 14 | 2 (4) | 21 | 19 | 16 | 0/2 | 2/2 | 16/7/3 | 12/16 |
| 9 | 75 BANDRA_B p9 | 2 x 3BHK | 0 / 0 | 3 (2) | 17 | 16 | 15 | 0/3 | 2/3 | 15/1/3 | 9/15 |
| 10 | 76 BHULESHWAR p4 | 2BHK sale tower (+ school) | 8 / 7 | 2 (1) | 8 | 8 | 7 | 1/2 | 1/1 | 7/1/1 | 7/8 |
| | **Total** | | | **25 (27)** | **189** | **151 (80 %)** | **157 (83 %)** | **10/25 (40 %)** | **18/23 (78 %)** | **164/26/18** | **112/138 (81 %)** |

Notes on the table:

* Page 4 flat F2 (19 rooms, three tenements merged) is not link-scored; it is too fragmented to count by hand.
* Page 6: the third flat is a parking block (`BUILDING LINE ABOVE` typed `dining`); page 10: the first flat is a school.
  Both are counted as emitted-but-wrong in "group" and left out of "entr".
* BHK count equals the real BHK in 12 of 23 apartment flats (52 %). Failures are the fragments (2 rooms, "1BHK") and the merges.

### Per-flat detail

| Page | Flat | Extractor BHK / real | rooms | untyped | wrong type | bad bounds | group | entrance (extractor) | links tp/fp/fn | ext |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 33_3 | F1 | 3 / 3 | 8 | 1 (living read as `\| IVING`) | 0 | 1 | no: master toilet dropped | living, ok | 8/0/1 | 6/7 |
| 33_3 | F2 | 3 / 3 | 9 | 0 | 0 | 2 | yes | living, ok | 9/1/0 | 7/8 |
| 33_3 | F3 | 1 / 2 | 7 | 1 (master bedroom) | 0 | 2 | no: kitchen dropped | dining strip (the foyer), ok | 7/0/1 | 5/6 |
| 40_5 | F1 | 1 / 2 | 2 | 0 | 0 | 0 | no: 6 of 8 rooms | bedroom, wrong | 7/0/0 (apt A) | 2/2 |
| 40_5 | F2 | 1 / 2 | 2 | 0 | 0 | 0 | no: 5 of 7 rooms | bedroom, wrong | 8/0/0 (apt B) | 2/2 |
| 40_5 | F3 | 2 / 2 | 11 | 2 | 0 | 2 | no: B and C merged | living, ok | 9/0/0 (apt C) | 8/8 |
| 53_1 | F1 | 1 / 2 | 6 | 2 | 0 | 2 | no: bedroom and kitchen dropped, lift and lobby cell included | living, ok | 6/0/2 | 3/3 |
| 53_1 | F2 | 2 / 2 | 7 | 1 | 0 | 1 | yes | living, ok | 7/1/0 | 3/6 |
| 53_1 | F3 | 2 / 2 | 7 | 1 | 0 | 1 | yes | living, ok | 5/2/1 | 4/6 |
| 54_8 | F1 | 2 / 2 | 9 | 4 | 0 | 1 | yes | living, ok | 8/1/1 | 4/4 |
| 54_8 | F2 | 4 / 2+ | 19 | 12 | 0 | 3 | no: T.62 + T.65 + cells | lobby cell, wrong | not scored | 5/7 |
| 61_4 | F1 | 1 / 1 | 6 | 1 | 0 | 2 | yes | dining (open plan), ok | 7/0/0 | 3/4 |
| 61_4 | F2 | 1 / 1 | 11 | 4 | 0 | 2 | no: 601 + 602 + part of 604 | living, ok | 7/4/3 | 5/7 |
| 62_0 | F1 | 3 / 3 | 12 | 2 | 0 | 1 | yes | living, ok (via a lift door) | 12/1/1 | 8/9 |
| 62_0 | F2 | 3 / 3 | 9 | 1 | 0 | 1 | yes | foyer, ok | 9/1/0 | 7/8 |
| 69_0 | F1 | 3 / 3 | 9 | 0 | 0 | 2 | yes | living, ok | 9/3/1 | 6/6 |
| 69_0 | F2 | 2 / 2 | 9 | 3 | 0 | 1 | yes, but 2 lobby fragments inside | lobby fragment, wrong | 8/3/0 | 6/6 |
| 74_3 | F1 | 2 / 1 | 10 | 0 | 1 (`BUILDING` as dining) | 2 | no: two tenements merged | living, ok | 7/5/2 | 4/8 |
| 74_3 | F2 | 2 / 1 | 11 | 1 | 0 | 3 | no: two tenements merged | living, ok | 9/2/1 | 8/8 |
| 75_9 | F1 | 2 / 3 | 7 | 0 | 0 | 1 | no: third bedroom and its toilet split off | living, ok | 7/0/0 | 4/6 |
| 75_9 | F2 | 1 / (3) | 2 | 0 | 0 | 1 | no: orphan, no entrance | none, wrong | 1/0/1 | 1/2 |
| 75_9 | F3 | 2 / 3 | 8 | 1 | 0 | 0 | no: third bedroom typed `chajja` | living (via wall), ok | 7/1/2 | 4/7 |
| 76_4 | F2 | 2 / 2 | 8 | 0 | 0 | 1 | yes | living (via wall), ok | 7/1/1 | 7/8 |

## Overall precision and recall per relation

All figures are from the 10 pages above; read them as +-5 points.

| Relation | Result | Evidence |
| --- | --- | --- |
| Room type (rooms in emitted flats) | 151 correct, 37 untyped, 1 wrong in flats (99 % precision, 80 % coverage) | The type, when given, is right. Wrong types sit outside flats: `BUILDING LINE` typed `dining` (page 74: 5 rooms, page 62: 6), `TOILET` merged with a neighbouring `FIRE EVACUATION` label and typed `fire_lift` (53_1 room 11), `BEDROOM` next to `CHAJJA` typed `chajja` (75_9). |
| Room boundary | 157 of 189 correct (83 %) | See failure modes 4 and 5. |
| Flat grouping | 10 of 25 emitted flats correct (40 %); 10 of 27 real apartments recovered (37 %) | See failure modes 1 to 3. This is the weakest stage. |
| Entrance room | 18 of 23 (78 %) | All 5 misses are grouping side effects (entrance picked in a bedroom, or in a lobby fragment), not misreading of the door. |
| Any link (door or open) | precision 164/190 = 86 %, recall 164/182 = 90 % | Open-gap links are the workhorse; most "recall" losses are rooms left without any link. |
| Door-arc link only (4 pages with arcs: 62, 69, 74, 76) | precision 34/42 = 81 %, recall about 34/50 = 68 % | False links are arcs attached across a party wall, to a lift (62_0 living to lift, 74_3 lift to living), or to the wrong neighbour (bedroom to bedroom: 76_4, 62_0, 69_0). |
| Open-gap link on pages without usable arcs (pages 1-5, 9) | precision 103/113 = 91 % | Typical false links: toilet to kitchen, kitchen to bedroom (53_1), bedroom to bedroom. |
| Bedroom/master to attached toilet link | precision 25/27 = 93 %, recall 26/29 = 90 % | Misses: master toilet behind a 0.79 x 1.05 vestibule (33_3), two toilets in 61_4. False: 53_1 bedroom to WC, 69_0 master to WC. |
| Kitchen opens straight on living/dining/foyer | real in about 12 of 20 kitchens; found for 11 of those 12 | The rest open on a passage. |
| Exterior flag, bedroom and master | flagged on 44 of 47; all 47 really have a façade | Misses are hull clipping (failure mode 4). |
| Exterior flag, living | flagged on 11 of 25; about 22 of 25 really have a façade (windows, deck or balcony) | Strong bias against living rooms. |
| Exterior flag, kitchen and toilet | not reliable | The flag marks "near the seed hull", not "has an outer wall", and mixes façade with duct/shaft (a toilet on an A.V.S. duct is flagged exterior in 33_3). |

Cost note: extracting a page took 2.7 to 19.6 s on the shared CPU (mean 8.0 s, median 5.9 s; the 19.6 s page is the A1-size
62_0 sheet with 23 arcs). That is within the "under 10 s is fine" guideline and is not a concern for this audit.

## Dataset-wide diagnostics (exact, from `topology.jsonl`)

* Pages: 649 plan pages, 3 665 flat records, 189 rooms scored here out of 26 515.
* Door arcs: 380 of 649 pages (58.5 %) have none; 14 of 50 projects never; 125 of the 269 pages that do have arcs produce zero door
  links (for example 33_3: 19 arcs from the dashed X-hatch of an A.V.S. void, 0 links). Only 286 of 3 665 flats (7.8 %) contain
  any door link; 3 663 contain open links. Everything the priors call a "link" is therefore mostly an open wall gap (which, on these
  sheets, is where the door is).
* Entrance room type among flats with at least one bedroom and an entrance (2 957): living 1 576 (53 %), bedroom 356, master 146,
  kitchen 48, toilet 40 (together 20 % bogus), unknown/unlabeled 648 (22 %), foyer 62, dining 37, passage 28.
* Flats with at least one bedroom and an entrance that have no kitchen: 854 (29 %); no living/dining/foyer: 872 (29 %); no toilet:
  166 (6 %). Only 1 850 (63 %) contain bedroom + kitchen + living-like + toilet.
* 421 flats have no bedroom type (parking, schools, offices, lobbies), 313 have no entrance, 348 are flagged `suspect_merge`.
* `BUILDING` typed as `dining`: 29 rooms in 23 flats.
* Rooms in flats whose type is `unknown` or `unlabeled`: 21 %.
* Area vs. own printed dimensions (21 215 rooms with dimensions): within 10 % for 69 %, within 25 % for 90 %. By type, within 10 %:
  toilet 87 %, bedroom 77 %, kitchen 77 %, master 79 %, living 47 %.

## Failure modes

1. **A narrow passage is classed as "common", which cuts a flat into pieces.** `is_common()` uses the bounding-box short side of the
   region when the label gives no `M.W.` width. A 0.90 m passage labelled `PASS 0.90 x 3.60` has a region bounding box of 1.25 m
   (L-shaped, with the vestibule), so it passes the `>= 1.25` test and is dropped from the flat graph. Examples: project 40 p5
   (flats F1 and F2 are one bedroom + one toilet each, entrance = a bedroom), project 53 p1 F1 (bedroom 5 and kitchen 8 lost).
   Scale: 20 % of flats with an entrance have it in a bedroom, master, kitchen or toilet; 29 % lack a kitchen.
2. **Spurious or mis-assigned links merge flats.** Components of the link graph become flats, so one bad link merges two
   apartments. Causes seen: a door arc whose centre lies near a party wall (74_3 living 10 to living 13, living 24 to living 25), an
   arc attached to the adjacent lift instead of the lobby (62_0, 74_3), and unlabelled lobby cells that are treated as private rooms
   (40_5 room 21, 54_8 room 26). Merged flats are rarely flagged: `suspect_merge` only fires above 14 rooms or 260 m2 (it caught only
   the 19-room case); the 10- and 11-room merges pass.
3. **Rooms with no link are dropped.** A room whose only contact is a wall (no drawn door, no gap of at least 0.55 m) is not in
   the flat graph and is silently lost: master toilet (33_3 F1), kitchen (33_3 F3), bedroom + toilet (75_9), bedroom and kitchen
   (53_1). The room is correct; the flat is incomplete.
4. **Convex-hull exterior mask clips rooms and biases the exterior flag.** The "exterior" label is everything outside the convex
   hull of the label seeds plus 1.5 m. The hull and its rounded buffer cut the outer corners of corner rooms (rounded, missing
   corner in 33_3 master bedroom, 62_0 bedroom 0: 13.7 m2 against 16.4 m2 printed). The flag means "region is within about 1.5 m
   of the hull", so large rooms whose seed is deeper than that (living rooms, kitchens behind balconies) are not flagged even
   though they have a façade (living flagged 11 of 25), and re-entrant façades (courtyards) are flagged unevenly.
5. **Labels merge free space.** A passage with no label (or whose label seed lands on a neighbouring cell) is flooded by the next room:
   33_3 living absorbs the 1.05 m passage (+28 % area), 53_1 bedroom 0 and living 15 absorb the 0.91 m corridor, 61_4 living is
   cut to 0.76 m2 by the dining seed. Door-swing arcs act as walls in the mask, so lobby cells get arc-shaped boundaries (69_0 rooms
   16 and 19).
6. **Text problems.** OCR splits (`| IVING`, `ILET`, `TOI.`) leave 21 % of flat rooms untyped and make the entrance an `unknown` room
   in 360 flats. The `DIN` pattern in `classify()` matches `BUILDING`. Neighbouring labels are merged
   (`TOILET` + `FIRE EVACUATION`, `BEDROOM` + `CHAJJA`).
7. **Non-residential and junk flats.** Parking stalls (62_0), a school (76_4), refuge and rehab composites (54_8): 421 flats with no
   bedroom, 313 without an entrance. They are emitted as flats and rely on later filters.
8. **Door arcs are rare and noisy.** See the diagnostics above: 58 % of pages have none, 46 % of arc pages yield no link, and where
   links exist they are right about 81 % of the time.

## Which priors in `mumbai_topology_priors.json` can be trusted

Verdicts use the sample above. The priors were derived after dropping `suspect_merge`, flats with no entrance and flats with more than
25 % unnamed rooms (2 153 of 3 665 flats). Fragments (failure mode 1) and unflagged merges (failure mode 2) pass those filters.

| Prior | Value | Verdict | Why |
| --- | --- | --- | --- |
| Bedroom façade (`exterior.bedroom.facade`, master) | 0.89 (master 0.91) | **Trustworthy** | Every bedroom and master I could judge (47) has a façade; the extractor flagged 44 of 47. The misses are clipping artefacts, so the true rate is higher (about 0.95+). Safe to use as a strong preference, not an exact figure. |
| Master toilet link (`master_has_toilet` 0.82, adjacent 0.875, `master>toilet` link) | 0.82 / 0.875 | **Trustworthy as a lower bound** | 7 of 7 master bedrooms in the sample have an attached toilet; the extractor found 6 (the miss is a toilet behind a vestibule with no drawn door). Bedroom-to-toilet link precision was 93 %. The true rate is about 0.9 to 1.0. |
| Entrance = living (`entrance.room_type.living`) | 0.73 | **Trustworthy in direction, too low in size** | On the drawings the front door opens into the living or living-dining room in about 22 of 27 apartments and into a small foyer or dining strip in the other 5; never into a bedroom, kitchen or toilet. The other mass in the prior (bedroom 0.148, master 0.074, kitchen 0.018, about 24 %) comes from failure mode 1 and should be treated as noise, not as a design option. Use living about 0.8 to 0.9, foyer or dining about 0.1. |
| `entrance_hops` (living median 0, bedroom median 2) | | **Doubtful** | The hop counts start from an entrance that is wrong in about 20 % of flats and cross merged flats. |
| Bedroom has toilet (`bedroom_has_toilet` 0.50, adjacent 0.67) | 0.50 / 0.67 | **Plausible** | About 45 % of non-master bedrooms in the sample have their own toilet, and the link finder is 90 % recall. Treat as an order of magnitude. |
| `kitchen>living`, `living>kitchen` link | 0.41-0.46 | **Plausible** | Real kitchen to living/dining/foyer is about 60 % in the sample, the rest open on a passage. The adjacency figures (0.93-0.94) are walls, not openings: do not use them as connectivity. |
| `living>bedroom` link (0.13-0.20), `passage>bedroom` (0.86) | | **Doubtful** | When the passage region is absorbed by the living (failure mode 5) the bedroom link goes to the living instead. |
| Living façade (0.75), kitchen façade (0.76), toilet façade (0.40) and shaft (0.23), all `ventilation.*` | | **Doubtful** | The flag is a seed-to-hull proximity test, not a measured outer wall (failure mode 4): living flagged 11 of 25 where about 22 have a façade; toilet and kitchen conflate façade with duct. Do not use as hard rules. |
| `pairs`/`room_links` `door` fields | n = 31-263 | **Doubtful** | Door links exist for only 7.8 % of flats, with precision about 81 % and recall about 68 %. Use qualitatively. |
| Passage counts and `living>passage` | | **Doubtful** | Passages are mis-classified common (failure mode 1) or absorbed by living (failure mode 5). |
| Per-BHK room counts (`bhk.*.types`, `toilet_pattern`, `typical`) | 1BHK n = 761 | **Doubtful** | Only 52 % of flats in the sample carry the right BHK. Two-room fragments pass as "1BHK" (for 1BHK, the `bedroom` histogram has 184 zeros out of 761). Regenerate after the grouping fix. |

## What to fix, and what it would change (not done here)

Order by effect on the priors; every item is in the extractor or the prior-derivation script, outside this change.

1. Passage width from the inscribed width (distance transform) or from the label's smaller number, never from the region
   bounding box. Fixes the largest fragment class (about 20 % of flats with an entrance have it in a bedroom, master, kitchen or toilet).
2. Merge a link-less room into the flat of its only private wall neighbour (master toilet, kitchen, bedroom + toilet cases).
3. Reject door links that cross a lift, a lobby or a party wall, and break flats at rooms that touch two entrances; treat unlabelled
   cells next to a lobby as common.
4. Replace the convex-hull exterior test by an outer-wall test (region wall touching free space outside the building outline)
   so that the exterior flag and room corners are right.
5. In `derive_topology_priors.py`, keep only flats with bedroom + kitchen + living-like + toilet and an entrance in
   {living, foyer, dining, passage, unknown}. In the dataset that keeps about 60 % of bedroom-bearing flats with entrance (1 778 of 2 957).
6. Classifier: anchor `DIN` as a word and ignore `BUILDING`; keep a `TOI.`/`ILET` fallback.
