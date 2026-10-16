"""Build the one-click Claude Desktop extension (.mcpb) around the frozen engine binary.

Usage: python packaging/build_mcpb.py <path/to/planner-engine[.exe]> <out_dir> [--platform darwin|win32|linux]
"""

from __future__ import annotations

import argparse
import json
import platform
import shutil
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from planner_engine import __version__  # noqa: E402

TOOLS = [
    ("open_planner", "Show the live Apartment Planner panel in the chat."),
    ("get_project", "Short summary of the current project and stage."),
    ("list_decisions", "Open decision tickets with options, safe default and evidence."),
    ("answer_decision", "Pick an option; the engine re-checks it with the rules and re-solves."),
    ("propose_programme", "Brief in words to a schema- and rule-checked room list."),
    ("apply_edit", "An edit in words as one constraint change; solve and diff."),
    ("compare_options", "Side-by-side numbers for placements, plans or shapes."),
    ("explain_check", "One rule result with clause, value and proof."),
    ("solve_flats", "Solve flat layouts and run the checks."),
]


def build(binary: Path, out: Path, plat: str) -> Path:
    exe_name = "planner-engine.exe" if plat == "win32" else "planner-engine"
    manifest = {
        "manifest_version": "0.2",
        "name": "apartment-planner",
        "display_name": "Apartment Planner",
        "version": __version__,
        "description": "Pick a plot, get a checked apartment floor plan. Rules do the work; Claude helps with unclear choices.",
        "long_description": "Connects Claude to the Apartment Planner desktop app (DCPR 2034 / NBC 2016 Part 4 pre-checks). "
                            "Claude can list and answer the planner's decision tickets, turn briefs into room lists and "
                            "apply edits in words. Every answer is re-checked by the rules engine.",
        "author": {"name": "Integrated Vertical Architecture Intelligence (IVAI)"},
        "license": "Apache-2.0",
        "keywords": ["architecture", "floor plan", "DCPR", "NBC", "apartments"],
        "server": {
            "type": "binary",
            "entry_point": f"server/{exe_name}",
            "mcp_config": {"command": "${__dirname}/server/" + exe_name, "args": ["mcp"], "env": {}},
        },
        "tools": [{"name": n, "description": d} for n, d in TOOLS],
        "compatibility": {"platforms": [plat]},
    }
    stage = out / f"mcpb-{plat}"
    if stage.exists():
        shutil.rmtree(stage)
    (stage / "server").mkdir(parents=True)
    shutil.copy2(binary, stage / "server" / exe_name)
    (stage / "manifest.json").write_text(json.dumps(manifest, indent=2))
    target = out / f"apartment-planner-{__version__}-{plat}.mcpb"
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as z:
        for f in sorted(stage.rglob("*")):
            if f.is_file():
                info = zipfile.ZipInfo.from_file(f, f.relative_to(stage).as_posix())
                if f.name.startswith("planner-engine"):
                    info.external_attr = 0o755 << 16
                with open(f, "rb") as fh:
                    z.writestr(info, fh.read(), zipfile.ZIP_DEFLATED)
    shutil.rmtree(stage)
    return target


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("binary", type=Path)
    ap.add_argument("out", type=Path)
    ap.add_argument("--platform", default={"Darwin": "darwin", "Windows": "win32"}.get(platform.system(), "linux"))
    a = ap.parse_args()
    a.out.mkdir(parents=True, exist_ok=True)
    print(build(a.binary, a.out, a.platform))
