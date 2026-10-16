"""Claude bridge: an MCP server (stdio) + an in-chat planner panel (MCP App).

Claude Desktop / Claude Code start this process. It talks to the running Apartment Planner app over
local JSON-RPC (address and token from the lock file). If the app is closed it opens the last project
itself, headless, so Claude's answers are never lost. Tools return the smallest useful answer plus
evidence. Claude never draws: it picks from options the engine made, or proposes a small change that the
engine re-checks with the same rules.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

import httpx
from mcp.server.apps import Apps
from mcp.server.mcpserver import MCPServer
from mcp_types import ToolAnnotations

from . import __version__
from .server import last_project_path, read_lock

PANEL_URI = "ui://apartment-planner/panel.html"

INSTRUCTIONS = """Apartment Planner turns a plot in Mumbai into checked apartment floor plans. The engine does
almost all the work with fixed rules (DCPR 2034, NBC 2016 Part 4). You help only with unclear choices:
1. Call list_decisions to see waiting tickets (each has options, a safe default and evidence).
2. Call answer_decision with one option id (or text for note/explain tickets) and a short reason.
   The engine re-checks every answer; rejected answers return an error - pick another option.
3. Use propose_programme to turn a brief into a room list, apply_edit for edits in words.
Never invent measurements. Rule results are an automated pre-check, not approval."""


class Backend:
    """Calls the running app; falls back to an in-process engine on the last project."""

    def __init__(self) -> None:
        self.mode = "none"
        self.planner = None
        self.url = None
        self.token = None
        self._connect()

    def _connect(self) -> None:
        info = read_lock()
        if info:
            try:
                r = httpx.get(f"{info['url']}/api/health", timeout=1.5)
                if r.status_code == 200:
                    self.mode, self.url, self.token = "app", info["url"], info["token"]
                    return
            except httpx.HTTPError:
                pass
        from .engine import Planner

        self.planner = Planner(last_project_path())
        self.mode = "headless"

    def call(self, method: str, **params: Any) -> Any:
        params = {k: v for k, v in params.items() if v is not None}
        if self.mode == "app":
            try:
                r = httpx.post(f"{self.url}/rpc", json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
                               headers={"X-Planner-Token": self.token or ""}, timeout=120)
                body = r.json()
            except httpx.HTTPError:
                self._connect()  # app closed meanwhile -> headless
                return self.call(method, **params)
            if "error" in body:
                raise RuntimeError(body["error"]["message"])
            return body["result"]
        from .engine import PlannerError

        try:
            return getattr(self.planner, method)(**params)
        except PlannerError as e:
            raise RuntimeError(str(e)) from e


_backend: Backend | None = None


def backend() -> Backend:
    global _backend
    if _backend is None:
        _backend = Backend()
        try:
            _backend.call("bridge_hello", client="claude", version=__version__)
        except Exception:  # noqa: BLE001 - handshake failure must not stop the tools
            pass
    return _backend


def _j(obj: Any) -> str:
    return json.dumps(obj, separators=(",", ":"), default=str)


PANEL_HTML = """<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>Apartment Planner</title><style>
:root{--bg:#fff;--fg:#1d2433;--mut:#5b6475;--teal:#0f8b8d;--amber:#d98a00;--line:#e3e6eb;--card:#f6f7f9}
@media (prefers-color-scheme:dark){:root{--bg:#14171c;--fg:#e8ebf0;--mut:#9aa3b2;--line:#2a2f37;--card:#1c2027}}
body{margin:0;font:14px/1.45 system-ui,-apple-system,Segoe UI,sans-serif;background:var(--bg);color:var(--fg)}
.w{padding:14px}h1{font-size:15px;margin:0 0 4px}.mut{color:var(--mut);font-size:12px}
.k{display:flex;gap:8px;flex-wrap:wrap;margin:10px 0}.c{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:8px 10px}
.c b{display:block;font-size:16px}.badge{display:inline-block;border-radius:99px;padding:2px 8px;font-size:12px;color:#fff}
.t{background:var(--teal)}.a{background:var(--amber)}ul{padding-left:18px;margin:6px 0}button{background:var(--teal);color:#fff;border:0;
border-radius:6px;padding:7px 12px;font-weight:600;cursor:pointer}</style></head><body><div class="w">
<h1>Apartment Planner</h1><div class="mut" id="sub">Loading…</div><div class="k" id="kpi"></div>
<div id="dec"></div><button id="ask">Answer waiting decisions</button>
<p class="mut">Automated pre-check. Confirm with the project's licensed consultant.</p></div>
<script>
let id=0;const pend={};
function rpc(method,params){return new Promise((res,rej)=>{const i=++id;pend[i]={res,rej};parent.postMessage({jsonrpc:"2.0",id:i,method,params},"*")})}
window.addEventListener("message",e=>{const m=e.data||{};if(m.id&&pend[m.id]){m.error?pend[m.id].rej(m.error):pend[m.id].res(m.result);delete pend[m.id];return}
 if(m.method==="ui/notifications/tool-result"){render(m.params&&(m.params.structuredContent||parse(m.params)))}});
function parse(p){try{return JSON.parse(p.content[0].text)}catch(_){return null}}
function esc(s){return String(s).replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]))}
function render(d){if(!d)return;const s=d.summary||d;document.getElementById("sub").textContent=(s.project||"")+" · stage "+(s.stage||"")+" "+(s.stage_name||"");
 const k=[];if(s.site)k.push(["Plot",s.site.area_m2+" m²"]);if(s.carpet_m2)k.push(["Carpet",s.carpet_m2+" m²"]);
 if(s.checks){const c=s.checks.counts;k.push(["Checks",c.pass+" pass · "+c.fail+" fail · "+c.review+" review"])}
 k.push(["Decisions",'<span class="badge '+(s.decisions_waiting?"a":"t")+'">'+s.decisions_waiting+" waiting</span>"]);
 document.getElementById("kpi").innerHTML=k.map(x=>'<div class="c"><span class="mut">'+x[0]+"</span><b>"+x[1]+"</b></div>").join("");
 const dec=(d.decisions||[]).slice(0,6);document.getElementById("dec").innerHTML=dec.length?"<ul>"+dec.map(t=>"<li>"+esc(t.id)+": "+esc(t.question)+"</li>").join("")+"</ul>":""}
document.getElementById("ask").onclick=()=>rpc("ui/message",{role:"user",content:[{type:"text",text:"Please list the waiting Apartment Planner decisions and answer them, with a short reason for each."}]}).catch(()=>{});
rpc("ui/initialize",{appInfo:{name:"apartment-planner-panel",version:"1"},appCapabilities:{},protocolVersion:"2026-01-26"}).then(()=>parent.postMessage({jsonrpc:"2.0",method:"ui/notifications/initialized"},"*")).catch(()=>{});
</script></body></html>"""


def build_server() -> MCPServer:
    apps = Apps()
    ro = ToolAnnotations(readOnlyHint=True, openWorldHint=False)
    rw = ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False)

    @apps.tool(resource_uri=PANEL_URI, name="open_planner", title="Open the planner panel",
               description="Show the live Apartment Planner panel in the chat: project, stage, checks and "
                           "how many decisions are waiting.", annotations=ro)
    def open_planner() -> str:
        b = backend()
        s = b.call("summary")
        dec = b.call("list_decisions", status="waiting")
        return _j({"summary": s, "decisions": [{"id": d["id"], "question": d["question"]} for d in dec[:10]],
                   "mode": b.mode, "message": f"{s.get('decisions_waiting', 0)} decisions waiting"})

    apps.add_html_resource(PANEL_URI, PANEL_HTML, name="Apartment Planner panel", prefers_border=True)

    mcp = MCPServer("apartment-planner", title="Apartment Planner", version=__version__, instructions=INSTRUCTIONS,
                    extensions=[apps])

    @mcp.tool(description="Short summary of the current project and stage (site, placement, plates, flats, "
                          "checks, decisions waiting). detail: summary | site | plan:<unit id> | geojson",
              annotations=ro)
    def get_project(detail: str = "summary") -> str:
        b = backend()
        if detail == "site":
            s = b.call("site_info")
            s.pop("lnglat", None)
            if s.get("climate"):
                s["climate"]["sun"] = [{"label": p["label"], "noon_altitude": p["noon_altitude"]} for p in s["climate"]["sun"]]
            return _j(s)
        if detail.startswith("plan:"):
            p = b.call("get_plan", unit_id=detail[5:])
            return _j({"unit": p["unit_id"], "carpet_m2": p["carpet_area"],
                       "rooms": [{"id": r["id"], "type": r["type"], "m2": r["area"], "width_m": r["width"],
                                  "window": r["has_window"]} for r in p["rooms"]]})
        if detail == "geojson":
            return _j(b.call("geojson"))
        return _j(b.call("summary"))

    @mcp.tool(description="Open decision tickets (status: waiting | all | answered | kept | undone; kind: T0..T8). "
                          "Each has a question, 2-4 options, the safe default already applied, and evidence.",
              annotations=ro)
    def list_decisions(status: str = "waiting", kind: str | None = None) -> str:
        return _j(backend().call("list_decisions", status=status, kind=kind))

    @mcp.tool(description="Answer one decision: pick option_id (or give text for free-text tickets such as "
                          "T5 notes / T6 explanations) with a short reason. The engine re-checks the answer with "
                          "the rules, re-solves and returns the change. A rejected answer returns an error.",
              annotations=rw)
    def answer_decision(ticket_id: str, option_id: str | None = None, text: str | None = None, reason: str = "") -> str:
        return _j(backend().call("answer_decision", ticket_id=ticket_id, option_id=option_id, text=text,
                                 reason=reason, by="claude"))

    @mcp.tool(description="Turn a brief in words (e.g. '3BHK with a pooja room for a joint family') into a room "
                          "list for one flat. rooms: [{id, type, name, target_area, needs_window, optional}], "
                          "links: [[a, b]], entry: room id. Types: living dining kitchen master bedroom study "
                          "pooja toilet bath wc foyer passage utility balcony dress store. Schema- and rule-checked.",
              annotations=rw)
    def propose_programme(unit_id: str, brief: str, rooms: list[dict] | None = None,
                          links: list[list[str]] | None = None, entry: str | None = None) -> str:
        return _j(backend().call("propose_programme", unit_id=unit_id, brief=brief, rooms=rooms, links=links,
                                 entry=entry, by="claude"))

    @mcp.tool(description="An edit in words, turned into one constraint change, e.g. 'make the kitchen open to "
                          "living' -> {action: 'link', a: 'kitchen', b: 'living'}. Actions: link, unlink, "
                          "open_plan, resize {room, target_area}, drop/keep {room}, window {room, needs}, "
                          "add_room {room, link_to}. The engine solves and returns the diff.", annotations=rw)
    def apply_edit(unit_id: str, edit: dict, reason: str = "") -> str:
        return _j(backend().call("apply_edit", unit_id=unit_id, edit=edit, reason=reason, by="claude"))

    @mcp.tool(description="Side-by-side numbers. kind: placement (tower options A/B/C), plans (flats), shapes.",
              annotations=ro)
    def compare_options(kind: str = "placement") -> str:
        return _j(backend().call("compare_options", kind=kind))

    @mcp.tool(description="One rule result with its clause, measured value, threshold and proof (entity ids).",
              annotations=ro)
    def explain_check(rule_id: str, target: str | None = None) -> str:
        return _j(backend().call("explain_check", rule_id=rule_id, target=target))

    @mcp.tool(description="Solve flat layouts (one unit_id, or all flats when omitted) and run the rule checks.",
              annotations=rw)
    def solve_flats(unit_id: str | None = None) -> str:
        b = backend()
        if unit_id:
            r = b.call("solve_unit", unit_id=unit_id)
            return _j({k: r.get(k) for k in ("unit_id", "status", "message", "applied_fix", "time_s")})
        return _j(b.call("solve_all"))

    return mcp


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="planner-mcp", description="Apartment Planner MCP bridge")
    ap.add_argument("--stdio", action="store_true", help="serve MCP over stdio (default)")
    ap.parse_args(argv)
    server = build_server()
    server.run("stdio")


if __name__ == "__main__":
    main(sys.argv[1:])
