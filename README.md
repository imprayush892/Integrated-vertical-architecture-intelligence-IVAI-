# Apartment Planner (IVAI)

Pick a plot on a map. Get a full apartment floor plan that passes Mumbai's building rules.
Rules do almost all the work. Claude helps only when a choice is unclear.

This repository implements the *Parametric Apartment Planner* PRD end to end:

| Stage | What happens | Where |
|---|---|---|
| 1 Find | Globe + satellite map, place search (Mapbox / Google / Nominatim, offline gazetteer fallback) | `geocode.py`, `MapView.tsx` |
| 2 Trace | Click corners, live edge lengths, UTM 43N metric grid with point-scale correction, shoelace vs. geodesic area | `geo.py`, `site.py` |
| 3 Climate | NOAA sun paths (Jun/Mar-Sep/Dec 21), wind rose (EPW or bundled climatology), hot west faces, setback from the rules file | `climate.py`, `site.py` |
| 4 Place | 1/2/3 tower options: every angle 0–179° in 1° steps, vectorised inscribed plates, gap rule, scored on coverage/gap/breeze/view/west glass | `placement.py` |
| 5 Shape | Six parametric plate templates with ratings; drag handles; zones always sum to the plate | `plate.py`, `PlateCanvas.tsx` |
| 6 Split | Flats cut with access-aware equal-area / frontage cuts, core links, air shafts, 1–4 BHK presets | `units.py` |
| 7 Wire | Bubble diagram, pre-check, OR-Tools CP-SAT layout on a 10 cm grid, infeasible-core explanation, fix ladder with safe defaults, walls/doors/windows | `diagram.py`, `solver.py`, `layout.py`, `drafting.py` |
| 8 Check | DXF R2007 export/import (layer mapping, unit detection, rooms, flats), DCPR 2034 + NBC 2016 Part 4 rule files with proof | `dxf_io.py`, `checks.py`, `data/rules/` |

Plus: the Claude bridge (MCP server with the 8 PRD tools and an in-chat panel), the 5-step Claude Desktop
link, decision tickets T0–T8 with an evidence ledger, a SQLite project file with undo, `planner doctor`,
the golden test set with release gates, a React desktop UI, a Tauri 2 shell, PyInstaller sidecar,
a `.mcpb` one-click Claude extension, and CI/release pipelines.

> **Automated pre-check. Confirm with the project's licensed consultant.**
> The DCPR 2034 / NBC 2016 values are encoded from the regulations as commonly applied and are marked
> `verified: false` in the rule files until a licensed consultant signs them off for your office.

## Quick start (from source)

```bash
# engine (Python 3.11+)
cd engine
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
planner doctor          # engine, solver, DXF, Claude link
planner demo            # the video's flow on the Lokhandwala plot -> planner-demo/lokhandwala_T1.dxf

# UI (Node 22)
cd ../app && npm ci && cd ..
./scripts/build_web.sh  # builds the UI into the engine package
planner serve           # opens http://127.0.0.1:8765
```

Development with hot reload: run `planner serve --no-browser` and `npm run dev` in `app/` (Vite proxies
`/rpc` and `/api` to the engine).

## Desktop app

`app/src-tauri` is a Tauri 2 shell. It starts the frozen engine as a sidecar (`planner-engine serve`),
waits for its `listening` line and opens the local UI. The engine exits when the app exits (parent-PID
watchdog, so even a hard kill leaves no orphan process).

```bash
cd engine/packaging && pyinstaller planner-engine.spec --noconfirm --distpath ../dist
cp ../dist/planner-engine ../../app/src-tauri/binaries/planner-engine-$(rustc -vV | sed -n 's/host: //p')
cd ../../app && npx tauri build
```

Tagging `v*` runs `.github/workflows/release.yml`: signed installers for macOS (arm64 + x64),
Windows and Linux, the updater manifest, and a `.mcpb` per platform. Signing secrets are optional;
without them the builds are unsigned.

## Claude

The app links to the Claude the user already has. It never reads or reuses Claude logins; MCP is the
only door.

* **Claude Desktop** - in the app click *Claude not linked* → *Add Apartment Planner to Claude*, or run
  `planner link-claude`. It shows the exact config change, backs up the file, adds one entry and never
  removes anything. Or install the one-click `apartment-planner-<version>-<platform>.mcpb`.
* **Claude Code** - `claude mcp add apartment-planner -- planner-engine mcp` (or
  `python -m planner_engine.mcp_server --stdio` from source).
* **No Claude** - every ticket keeps its safe default, marked *check me*. The plan still finishes.

Tools Claude can call: `open_planner` (in-chat panel), `get_project`, `list_decisions`, `answer_decision`,
`propose_programme`, `apply_edit`, `compare_options`, `explain_check`, `solve_flats`.
Every answer is schema-checked, then rule-checked, then the user taps *Keep* or *Undo*.

## Release gates

`planner golden` measures the PRD's "Done means" numbers; CI runs the quick set on every push and the
full set as a separate job. See [docs/RELEASE_GATES.md](docs/RELEASE_GATES.md) for the latest numbers and
what each gate measures.

## Repository layout

```
engine/                     Python engine (package planner_engine)
  planner_engine/data/      rules (YAML), programmes, climate, gazetteer, golden plots
  tests/                    pytest suite (unit, integration, MCP stdio, reference drawings)
  packaging/                PyInstaller spec, .mcpb builder
  tools/mine_resplan.py     derives layout priors from the ResPlan dataset
app/                        React + TypeScript UI (MapLibre, Konva, three.js)
  src-tauri/                Tauri 2 desktop shell
docs/                       architecture, rules, operations, gates
scripts/build_web.sh        builds the UI into the engine
```

## Data and licences

* Code: Apache-2.0 (see `LICENSE`).
* Layout priors (`layout_priors.yaml`, `resplan_priors.json`) are derived from
  [ResPlan](https://github.com/m-agour/ResPlan) (17,000 plans, CC BY 4.0) by `engine/tools/mine_resplan.py`.
* Satellite imagery: Esri World Imagery (credit shown on the map). Use a commercial tile/geocoding
  contract for production deployments (`MAPBOX_TOKEN` / `GOOGLE_MAPS_KEY` switch the geocoder).
* Geocoding default: OpenStreetMap Nominatim (rate-limited to 1 request/second, results cached on disk).
