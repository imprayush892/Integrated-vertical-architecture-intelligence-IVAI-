"""``planner doctor`` - test the engine, the Claude link, data files and the DXF path in one go.

(Fonts and GPU are checked by the desktop app's own doctor page, which calls this and adds the browser
side: WebGL for the map and 3D view.)
"""

from __future__ import annotations

import importlib
import platform
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from . import __version__


def _check(name: str, fn) -> dict[str, Any]:
    t0 = time.perf_counter()
    try:
        detail = fn()
        return {"name": name, "ok": True, "detail": detail, "ms": round((time.perf_counter() - t0) * 1000)}
    except Exception as e:  # noqa: BLE001 - doctor reports everything
        return {"name": name, "ok": False, "detail": f"{type(e).__name__}: {e}", "ms": round((time.perf_counter() - t0) * 1000)}


def run_doctor(verbose: bool = True) -> dict[str, Any]:
    checks = []

    def libs():
        out = {}
        for m in ("shapely", "pyproj", "numpy", "ortools", "ezdxf", "pydantic", "yaml", "fastapi", "mcp"):
            mod = importlib.import_module(m)
            out[m] = getattr(mod, "__version__", "ok")
        return out

    checks.append(_check("python libraries", libs))

    def rules_files():
        from . import rules

        rules.reload()
        cl = rules.clauses()
        return {"clauses": len(cl), "codes": sorted({c.code for c in cl}), "setback@24m": rules.setback_for(24, 12)[0],
                "priors": rules.priors().get("source", "")[:40]}

    checks.append(_check("rules files", rules_files))

    def projection():
        from . import geo

        f = geo.make_frame([(72.8275, 19.143)])
        return {"epsg": f.epsg, "scale": f.scale}

    checks.append(_check("projection (UTM)", projection))

    def solver():
        from ortools.sat.python import cp_model

        m = cp_model.CpModel()
        x = m.new_int_var(0, 10, "x")
        m.add(x >= 3)
        m.minimize(x)
        s = cp_model.CpSolver()
        s.parameters.max_time_in_seconds = 2
        st = s.solve(m)
        assert s.value(x) == 3, s.status_name(st)
        return "CP-SAT ok"

    checks.append(_check("CP-SAT solver", solver))

    def flat():
        from .layout import solve_flat
        from .models import PlateFootprint
        from .plate import fit
        from .units import presets, split_units

        p = fit("central_core", PlateFootprint(id="T1", polygon=[], angle_deg=0, center=(0, 0), width=30, depth=22, area=660))
        u = split_units(p, 4)[0]
        u.programme = presets()["1bhk"]
        fr = solve_flat(p, u, time_limit=6)
        assert fr.plan is not None, fr.message
        return {"rooms": len(fr.plan.rooms), "s": fr.plan.solve_time_s}

    checks.append(_check("layout solve (1BHK)", flat))

    def dxf():
        import ezdxf

        from .dxf_io import roundtrip_check

        d = Path(tempfile.mkdtemp())
        doc = ezdxf.new("R2007")
        doc.modelspace().add_lwpolyline([(0, 0), (4, 0), (4, 3), (0, 3)], close=True, dxfattribs={"layer": "A-AREA"})
        doc.saveas(d / "a.dxf")
        r = roundtrip_check(d / "a.dxf", d / "b.dxf")
        assert r["lost"] == 0
        return r

    checks.append(_check("DXF read/write", dxf))

    def claude():
        from .claude_link import status

        s = status()
        return {"installed": s["app"]["installed"], "linked": s["linked_in_config"], "config": s["config"]}

    checks.append(_check("Claude Desktop link", claude))

    def app_server():
        import httpx

        from .server import read_lock

        info = read_lock()
        if not info:
            return "app not running (bridge will run headless)"
        r = httpx.get(info["url"] + "/api/health", timeout=2)
        return r.json()

    checks.append(_check("app server", app_server))
    ok = all(c["ok"] for c in checks if c["name"] not in ("Claude Desktop link", "app server"))
    report = {"version": __version__, "python": sys.version.split()[0], "os": platform.platform(), "ok": ok,
              "checks": checks}
    if verbose:
        for c in checks:
            mark = "PASS" if c["ok"] else "FAIL"
            print(f"  [{mark}] {c['name']:<24} {c['ms']:>5} ms  {c['detail']}")
        print("  doctor:", "all good" if ok else "problems found")
    return report
