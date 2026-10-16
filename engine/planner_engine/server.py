"""Local server: JSON-RPC 2.0 for the UI and the Claude bridge, live events (SSE), file up/download.

Binds to 127.0.0.1 only. Every state-changing call needs the per-launch token (``X-Planner-Token``), which
the UI fetches from ``/api/token`` (same-origin only) and the MCP bridge reads from the lock file. That
keeps random web pages from driving the planner through localhost.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import inspect
import json
import os
import secrets
import socket
import tempfile
import threading
import time
from importlib import resources
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import __version__
from .engine import Planner, PlannerError

RPC_METHODS = {
    "create_project", "open_project", "get_project", "rename", "undo", "history", "summary", "search_place",
    "set_site", "site_info", "set_site_params", "measure", "place_towers", "choose_placement", "list_shapes",
    "set_plate", "preview_plate", "import_plate_dxf", "split_units", "list_presets", "set_programme",
    "propose_programme", "get_diagram", "update_diagram", "precheck", "solve_unit", "solve_all", "get_plan",
    "apply_edit", "run_checks", "explain_check", "export_dxf", "import_drawing", "list_decisions",
    "answer_decision", "review_decision", "decision_stats", "compare_options", "geojson", "bridge_hello",
    "bridge_status",
}


def home() -> Path:
    d = Path(os.environ.get("PLANNER_HOME", Path.home() / ".apartment-planner"))
    d.mkdir(parents=True, exist_ok=True)
    return d


def lock_path() -> Path:
    return home() / "server.json"


def read_lock() -> dict | None:
    p = lock_path()
    if not p.exists():
        return None
    try:
        info = json.loads(p.read_text())
    except json.JSONDecodeError:
        return None
    return info


def last_project_path() -> Path:
    cfg = home() / "config.json"
    if cfg.exists():
        try:
            p = json.loads(cfg.read_text()).get("last_project")
            if p:
                return Path(p)
        except json.JSONDecodeError:
            pass
    return home() / "projects" / "default.aptplan"


def remember_project(path: str) -> None:
    cfg = home() / "config.json"
    data = {}
    if cfg.exists():
        try:
            data = json.loads(cfg.read_text())
        except json.JSONDecodeError:
            data = {}
    data["last_project"] = str(path)
    cfg.write_text(json.dumps(data, indent=1))


def free_port(preferred: int = 8765) -> int:
    for port in [preferred, *range(preferred + 1, preferred + 50)]:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _web_dir() -> Path | None:
    env = os.environ.get("PLANNER_WEB_DIR")
    if env and Path(env, "index.html").exists():
        return Path(env)
    d = Path(str(resources.files("planner_engine").joinpath("web")))
    return d if (d / "index.html").exists() else None


def create_app(planner: Planner, token: str | None = None) -> FastAPI:
    token = token or secrets.token_urlsafe(24)
    loop_holder: dict[str, asyncio.AbstractEventLoop] = {}

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        loop_holder["loop"] = asyncio.get_running_loop()
        yield

    app = FastAPI(title="Apartment Planner engine", version=__version__, docs_url=None, redoc_url=None, lifespan=lifespan)
    app.state.planner = planner
    app.state.token = token
    queues: set[asyncio.Queue] = set()

    def on_event(ev: dict) -> None:
        loop = loop_holder.get("loop")
        if not loop:
            return
        for q in list(queues):
            loop.call_soon_threadsafe(q.put_nowait, ev)

    planner.subscribe(on_event)

    def _check_token(request: Request) -> None:
        if request.headers.get("x-planner-token") != token:
            raise HTTPException(status_code=401, detail="missing or bad X-Planner-Token")

    def _same_origin(request: Request) -> None:
        site = request.headers.get("sec-fetch-site")
        origin = request.headers.get("origin")
        host = request.headers.get("host", "")
        if site and site not in ("same-origin", "none"):
            raise HTTPException(status_code=403, detail="cross-site request refused")
        if origin and not origin.endswith(host):
            raise HTTPException(status_code=403, detail="cross-origin request refused")

    @app.get("/api/health")
    def health() -> dict:
        return {"ok": True, "version": __version__, "project": planner.project.name, "file": planner.project_path()}

    @app.get("/api/token")
    def get_token(request: Request) -> dict:
        _same_origin(request)
        return {"token": token}

    @app.post("/rpc")
    async def rpc(request: Request) -> JSONResponse:
        _check_token(request)
        try:
            body = await request.json()
        except json.JSONDecodeError:
            return JSONResponse({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}})
        batch = isinstance(body, list)
        calls = body if batch else [body]
        out = []
        for call in calls:
            out.append(await _dispatch(planner, call))
        return JSONResponse(out if batch else out[0])

    @app.get("/api/events")
    async def events(request: Request, t: str = "") -> StreamingResponse:
        if t != token:
            raise HTTPException(status_code=401)
        q: asyncio.Queue = asyncio.Queue()
        queues.add(q)

        async def gen():
            try:
                yield f"data: {json.dumps({'kind': 'hello', 'bridge': planner.bridge_status()})}\n\n"
                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        ev = await asyncio.wait_for(q.get(), timeout=15)
                        yield f"data: {json.dumps(ev, default=str)}\n\n"
                    except TimeoutError:
                        yield ": keepalive\n\n"
            finally:
                queues.discard(q)

        return StreamingResponse(gen(), media_type="text/event-stream")

    @app.post("/api/upload/drawing")
    async def upload_drawing(request: Request, file: UploadFile = File(...)) -> JSONResponse:
        _check_token(request)
        name = Path(file.filename or "drawing.dxf").name
        if not name.lower().endswith(".dxf"):
            raise HTTPException(status_code=400, detail="Only .dxf files")
        dest_dir = (Path(planner.project_path()).parent if planner.project_path() else home()) / "drawings"
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / name
        data = await file.read()
        if len(data) > 200 * 1024 * 1024:
            raise HTTPException(status_code=413, detail="Drawing larger than 200 MB")
        dest.write_bytes(data)
        try:
            res = await asyncio.to_thread(planner.import_drawing, str(dest))
        except PlannerError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        return JSONResponse(res)

    @app.get("/api/download/dxf")
    async def download_dxf(t: str, tower: str = "T1") -> FileResponse:
        if t != token:
            raise HTTPException(status_code=401)
        tmp = Path(tempfile.mkdtemp()) / f"{planner.project.name.replace(' ', '_')}_{tower}.dxf"
        try:
            await asyncio.to_thread(planner.export_dxf, str(tmp), tower)
        except PlannerError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        return FileResponse(tmp, filename=tmp.name, media_type="application/dxf")

    @app.get("/api/doctor")
    async def doctor_ep() -> dict:
        from .doctor import run_doctor

        return await asyncio.to_thread(run_doctor, False)

    web = _web_dir()
    if web:
        app.mount("/assets", StaticFiles(directory=web / "assets"), name="assets") if (web / "assets").exists() else None

        @app.get("/{path:path}")
        def spa(path: str):
            f = web / path
            if path and f.is_file() and web in f.resolve().parents:
                return FileResponse(f)
            return FileResponse(web / "index.html")
    else:
        @app.get("/")
        def no_ui() -> HTMLResponse:
            return HTMLResponse("<h1>Apartment Planner engine</h1><p>The UI is not built. Run <code>npm run build</code> "
                                "in <code>app/</code>, or use the desktop app.</p>")

    return app


async def _dispatch(planner: Planner, call: Any) -> dict:
    cid = call.get("id") if isinstance(call, dict) else None
    if not isinstance(call, dict) or call.get("jsonrpc") != "2.0" or "method" not in call:
        return {"jsonrpc": "2.0", "id": cid, "error": {"code": -32600, "message": "invalid request"}}
    m = call["method"]
    if m not in RPC_METHODS:
        return {"jsonrpc": "2.0", "id": cid, "error": {"code": -32601, "message": f"unknown method {m}"}}
    params = call.get("params") or {}
    fn = getattr(planner, m)
    try:
        if isinstance(params, list):
            result = await asyncio.to_thread(fn, *params)
        else:
            sig = inspect.signature(fn)
            unknown = [k for k in params if k not in sig.parameters]
            if unknown:
                raise PlannerError(f"unknown parameters {unknown}", "bad_params")
            result = await asyncio.to_thread(fn, **params)
        if m in ("open_project", "create_project") and planner.project_path():
            remember_project(planner.project_path())  # type: ignore[arg-type]
        return {"jsonrpc": "2.0", "id": cid, "result": result}
    except PlannerError as e:
        return {"jsonrpc": "2.0", "id": cid, "error": {"code": -32000, "message": str(e), "data": {"code": e.code}}}
    except TypeError as e:
        return {"jsonrpc": "2.0", "id": cid, "error": {"code": -32602, "message": str(e)}}
    except Exception as e:  # noqa: BLE001 - report, never crash the server
        import traceback

        traceback.print_exc()
        return {"jsonrpc": "2.0", "id": cid, "error": {"code": -32603, "message": f"{type(e).__name__}: {e}"}}


def serve(project: str | None = None, port: int | None = None, open_browser: bool = False) -> None:
    import uvicorn

    path = Path(project) if project else last_project_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    planner = Planner(path, name=path.stem.replace("_", " ").title())
    remember_project(str(path))
    token = secrets.token_urlsafe(24)
    port = port or free_port(int(os.environ.get("PLANNER_PORT", "8765")))
    app = create_app(planner, token)
    lock = {"port": port, "pid": os.getpid(), "token": token, "url": f"http://127.0.0.1:{port}", "started": time.time(),
            "version": __version__, "project": str(path)}
    lock_path().write_text(json.dumps(lock))
    try:
        os.chmod(lock_path(), 0o600)
    except OSError:
        pass
    print(json.dumps({"event": "listening", **{k: v for k, v in lock.items() if k != "token"}}), flush=True)
    if open_browser:
        import webbrowser

        threading.Timer(1.0, lambda: webbrowser.open(lock["url"])).start()
    try:
        uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
    finally:
        try:
            info = read_lock()
            if info and info.get("pid") == os.getpid():
                lock_path().unlink()
        except OSError:
            pass
