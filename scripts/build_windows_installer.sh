#!/usr/bin/env bash
# Rebuild the UI bundle and ApartmentPlanner-Setup.exe (needs node + makensis; works on Linux, macOS, Windows/Git Bash).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
"$ROOT/scripts/build_web.sh"
VERSION=$(sed -n 's/^version = "\(.*\)"/\1/p' "$ROOT/engine/pyproject.toml" | head -1)
cd "$ROOT/windows" && makensis -V2 -DVERSION="$VERSION" installer.nsi
echo "Built $ROOT/ApartmentPlanner-Setup.exe ($VERSION)"
