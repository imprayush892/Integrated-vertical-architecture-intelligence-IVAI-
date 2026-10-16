# PyInstaller spec - builds dist/planner-engine (onefile) used as the Tauri sidecar and the Claude bridge.
# Usage: pyinstaller packaging/planner-engine.spec --noconfirm
from PyInstaller.utils.hooks import collect_all, collect_data_files, collect_submodules

datas, binaries, hidden = [], [], []
for pkg in ("ortools", "pyproj", "shapely", "ezdxf", "mcp", "mcp_types", "pydantic", "fastapi", "starlette",
            "uvicorn", "anyio", "httpx", "sse_starlette"):
    try:
        d, b, h = collect_all(pkg)
        datas += d
        binaries += b
        hidden += h
    except Exception:
        pass
datas += collect_data_files("planner_engine", includes=["data/**/*", "web/**/*"])
hidden += collect_submodules("planner_engine") + ["uvicorn.logging", "uvicorn.loops.auto", "uvicorn.protocols.http.auto",
                                                   "uvicorn.lifespan.on", "multipart"]

a = Analysis(["planner_entry.py"], pathex=[".."], binaries=binaries, datas=datas, hiddenimports=hidden,
             excludes=["matplotlib", "pvlib", "scipy", "tkinter", "pytest", "PIL"], noarchive=False)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, a.binaries, a.datas, [], name="planner-engine", debug=False, strip=False, upx=False,
          console=True, onefile=True)
