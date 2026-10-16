# Architecture

One desktop app. One engine. One small bridge to Claude.

```
┌──────────────── Apartment Planner (Tauri 2 shell) ────────────────┐
│  React UI: MapLibre globe/map · Konva plan editor · three.js 3D ·  │
│  bubble diagram · decision inbox · Claude link                     │
│            │ JSON-RPC 2.0 + SSE over http://127.0.0.1 (token)      │
│  ┌─────────▼───────── Rules engine (Python sidecar) ─────────────┐ │
│  │ geo (pyproj) · climate (NOAA) · placement (numpy/shapely)     │ │
│  │ plate templates · units · CP-SAT layout · drafting · ezdxf    │ │
│  │ rules (YAML) · checks · tickets · evidence · SQLite project   │ │
│  └─────────▲─────────────────────────────────────────────────────┘ │
└────────────┼───────────────────────────────────────────────────────┘
             │ local JSON-RPC (address + token from ~/.apartment-planner/server.json)
   planner-engine mcp  (MCP stdio server + MCP App panel)
             │ started by
   Claude Desktop / Claude Code  (the user's own account)
```

## One engine, two doors

`planner_engine.engine.Planner` is the only place where stages run. The UI calls it through
`server.py` (JSON-RPC, whitelisted methods). Claude calls it through `mcp_server.py`, which forwards to
the running app, or opens the last project headless when the app is closed. There is no second copy of
the logic.

## Frames

* `lnglat` - WGS84, only for the map.
* `site` - UTM metres minus an origin, divided by the UTM point scale factor at the origin (ground
  metres; corrects the ~0.025% grid distortion at Mumbai).
* `plate` - axis-aligned per tower (rotated by the placement angle). Units, diagrams, plans and DXF
  exports live here.
* Flats with a sloped façade are solved in their own frame aligned with their longest façade edge, then
  rotated back (`solver._solve_frame`, `drafting.rotate_plan`).

## Determinism

Stages 1–6 and 8 are pure functions of their inputs. The CP-SAT stage stops on time and stall limits;
solved flats are cached by an input hash (`layout._key`) and every plan is stored in the project file,
so the same input returns the same plan. Multi-threaded CP-SAT runs on different machines may find
different (equally valid) layouts for a fresh input.

## Tickets (where Claude may step in)

| Ticket | Stage | Question | Safe default |
|---|---|---|---|
| T0 | 3 | Sea view / creek nearby? | "–" unknown |
| T1 | 8 | Unknown DXF layer → meaning | unmapped (user picks; learned per office) |
| T2 | 8 | Header says mm, size says metres | trust the size |
| T3 | 7 | Room clash: pick a fix | drop optional rooms first, then duct, smaller programme… |
| T4 | 8 | Room name from messy text | label by size and shape |
| T5 | 4 | Design-driver note | template sentence (Claude's note may only quote engine numbers) |
| T6 | 8 | Explain a review result | clause text |
| T7 | 7 | Edit in words → constraint change | not available |
| T8 | 6 | Brief → room list | preset (proposal is schema- and rule-checked) |

Every ticket applies its default at once (`status: defaulted`, `check_me: true`). Answers are
validated, applied, re-solved, and wait for the user's *Keep* / *Undo*. All decisions (rules, defaults,
Claude, user) are logged to the project file for the 92% gate.

## Layout solver (stage 7)

* Net interior = flat − wall band (230 mm on façade/passage/shaft sides, 115 mm on party walls).
* Rooms: rectangles on a 10 cm grid; `NoOverlap2D` with fixed blocked boxes for non-rectangular flats.
* Min sizes from the rules files plus the internal-wall allowance; aspect limits from ResPlan priors.
* Links: shared wall ≥ door width + wall. Window rooms: whole edge on one façade span, with
  `90·side ≥ area + 360` (1/6 glazing with a 1.5 m window). Wet rooms: façade or shaft (or duct, review).
* Balconies only on *outer* façades (not notches facing the core), long side outside, ≥1.5:1.
* Objective: target deviation (service rooms penalised for growing) + 5 × uncovered area.
* Infeasible: deletion filter over requirement groups (presolve-friendly fixed literals) → minimal core
  → 2–4 verified fixes → safe default applied → T3 ticket.
* Time: one CP-SAT solve is capped at `TIME_CAP` 8.5 s (stall patience grows with the programme size,
  `solver.stall_patience`). A whole flat - first solve, infeasibility explanation, every fix attempt, validation
  retry and drafting - shares one deadline of `layout.FLAT_BUDGET` 9.5 s (product limit: any flat < 10 s);
  attempts get a share of what is left and the ladder stops when under 0.5 s remain.
* Validation: every drafted plan is run through the DCPR room checks (`layout.room_check_fails`, same engine
  as the release gate). A failing plan is never returned: while budget remains the flat is re-solved with a
  tighter allowance (+0.2/0.4/0.6 m on min widths, +0.5/1/1.5 m² on min areas), then the fix ladder continues;
  if it still fails (or time runs out) the result is `infeasible` with a `validation` conflict.
* Drafting: room width is measured exactly (`drafting.clear_width`); `geo.clear_width` rasterises L-shaped
  rooms at bbox/80 and under-reads a 2.485 m arm as ≈2.3–2.4 m. Leftover pockets merged by priority with glazing/size caps (thin slivers stay wall),
  internal walls on shared edges, doors on links, windows sized for the 1/6 rule.

## Files

* Project file (`*.aptplan`): SQLite with `project` (versions, last 200 kept, undo), `tickets`,
  `evidence` (observed / derived / inferred / unknown, by engine / claude / user) and `events`.
* `~/.apartment-planner/`: `server.json` (port, pid, token), `config.json` (last project, crash-report
  opt-in), `layer_map.json` (the office's learned layer names), geocode cache, reference drawings.
