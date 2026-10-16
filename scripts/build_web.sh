#!/usr/bin/env bash
# Build the React UI and bundle it into the engine package (served by `planner serve` and the desktop shell).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT/app"
npm ci --no-audit --no-fund
npm run build
rm -rf "$ROOT/engine/planner_engine/web"
mkdir -p "$ROOT/engine/planner_engine/web"
cp -R dist/. "$ROOT/engine/planner_engine/web/"
touch "$ROOT/engine/planner_engine/web/.gitkeep"
echo "UI bundled into engine/planner_engine/web"
