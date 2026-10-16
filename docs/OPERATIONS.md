# Operations

## Commands

| Command | Purpose |
|---|---|
| `planner serve [--project P] [--port N] [--no-browser]` | Engine + UI on 127.0.0.1 (default port 8765, next free port otherwise) |
| `planner mcp` | Claude bridge over stdio (what Claude Desktop starts) |
| `planner link-claude [--yes] [--dry-run] [--unlink]` | The 5-step Claude Desktop link from a terminal |
| `planner doctor [--json]` | Engine libraries, rules, projection, CP-SAT, a 1BHK solve, DXF, Claude link, app server |
| `planner golden [--quick] [--gate NAME] [--verbose]` | Release gates (see RELEASE_GATES.md) |
| `planner demo [--out DIR] [--shape S]` | The reference video's flow on the Lokhandwala plot |
| `planner check drawing.dxf [--height H]` | Check any DXF floor; exit code 2 when a rule fails |

## Environment

| Variable | Meaning |
|---|---|
| `PLANNER_HOME` | Config/cache folder (default `~/.apartment-planner`) |
| `PLANNER_PORT` | Preferred port for `planner serve` |
| `PLANNER_RULES_DIR` | Office rule-file overrides |
| `PLANNER_GEOCODER` | `mapbox`, `google`, `nominatim` or `offline` |
| `MAPBOX_TOKEN`, `GOOGLE_MAPS_KEY` | Commercial geocoding |
| `PLANNER_WEB_DIR` | Serve the UI from another folder (development) |
| `CLAUDE_DESKTOP_CONFIG` | Override the Claude Desktop config path (tests, unusual installs) |
| `PLANNER_SENTRY_DSN`, `PLANNER_CRASH_REPORTS=1` | Opt-in crash reports (stack traces only, no project data) |
| `PLANNER_PARENT_PID` | Exit when this process exits (set by the desktop shell) |

## Security

* The server binds to 127.0.0.1 only.
* State-changing endpoints require the per-launch token (`X-Planner-Token`). The UI gets it from
  `/api/token`, which refuses cross-site requests; the bridge reads it from `server.json` (mode 600).
* RPC calls are limited to a whitelist of engine methods with checked parameters.
* The Claude link never touches Claude credentials. It edits only `mcpServers.apartment-planner`,
  after a backup, and only when the user confirms.

## Troubleshooting

* **Badge stays "Claude not linked"** - quit Claude fully (tray / menu bar) and reopen it; ask it to
  "open the apartment planner". `planner doctor` shows whether the config has the entry.
* **"The planner engine could not start"** (desktop) - run `planner-engine doctor` from a terminal
  next to the app binary.
* **Map is blank** - the satellite tiles need internet access to `server.arcgisonline.com`. Tracing and
  everything after it still work; the measurements come from the engine.
* **A flat will not solve** - open the Decisions inbox: the T3 ticket lists the clash and the fixes the
  engine verified. Very narrow flats (< 7 m of façade) cannot light a living room and a bedroom; change
  the flat count or the plate shape.
