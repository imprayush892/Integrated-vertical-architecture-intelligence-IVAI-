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


PANEL_HTML = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="light dark"><title>Apartment Planner</title><style>
:root{--bg:#E6E9EF;--g1:#F0F3F9;--g2:#DCE0E8;--lcd:#E1E5EC;--shd:#C2C7D2;--shl:#FFFFFF;--hair:#CDD2DC;--edge:rgba(255,255,255,.65);--edgei:rgba(163,170,184,.25);
--ink1:#1E2229;--ink2:#4A505C;--ink3:#5C6370;--off:#9AA0AC;--engrave:0 1px 0 rgba(255,255,255,.8);
--cobalt:#0066CC;--cobaltink:#0058B0;--det:#0D8A7B;--detink:#08675C;--detglow:rgba(13,138,123,.26);
--ai:#E05A2B;--aiink:#A8401A;--aiglow:rgba(224,90,43,.28);--pass:#1B9E4B;--passink:#12703A;--passglow:rgba(27,158,75,.28);
--warn:#D98200;--warnink:#8A5200;--warnglow:rgba(217,130,0,.28);--fail:#D33A3A;--failink:#A82424;--failglow:rgba(211,58,58,.28);
--raised:6px 6px 12px var(--shd),-6px -6px 12px var(--shl);--rsm:3px 3px 7px var(--shd),-3px -3px 7px var(--shl);
--inset:inset 4px 4px 8px var(--shd),inset -4px -4px 8px var(--shl);--latch:inset 2px 2px 5px var(--shd),inset -2px -2px 5px var(--shl)}
@media (prefers-color-scheme:dark){:root:not([data-theme=light]){--bg:#1E2127;--g1:#23272E;--g2:#191C21;--lcd:#1A1D22;--shd:#121418;--shl:#2A2E36;--hair:#2E333C;--edge:rgba(255,255,255,.05);--edgei:rgba(0,0,0,.45);
--ink1:#ECEFF4;--ink2:#A9B0BC;--ink3:#8C93A1;--off:#4A505C;--engrave:0 1px 1px rgba(0,0,0,.7);--cobalt:#3B9EFF;--cobaltink:#5AAEFF;--det:#24C9B3;--detink:#3AD3BE;--detglow:rgba(36,201,179,.32);
--ai:#FF7A4D;--aiink:#FF8F66;--aiglow:rgba(255,122,77,.35);--pass:#32D768;--passink:#4BDC7B;--passglow:rgba(50,215,104,.32);--warn:#FFB340;--warnink:#FFB95A;--warnglow:rgba(255,179,64,.32);--fail:#FF6B6B;--failink:#FF8080;--failglow:rgba(255,107,107,.32)}}
:root[data-theme=dark]{--bg:#1E2127;--g1:#23272E;--g2:#191C21;--lcd:#1A1D22;--shd:#121418;--shl:#2A2E36;--hair:#2E333C;--edge:rgba(255,255,255,.05);--edgei:rgba(0,0,0,.45);
--ink1:#ECEFF4;--ink2:#A9B0BC;--ink3:#8C93A1;--off:#4A505C;--engrave:0 1px 1px rgba(0,0,0,.7);--cobalt:#3B9EFF;--cobaltink:#5AAEFF;--det:#24C9B3;--detink:#3AD3BE;--detglow:rgba(36,201,179,.32);
--ai:#FF7A4D;--aiink:#FF8F66;--aiglow:rgba(255,122,77,.35);--pass:#32D768;--passink:#4BDC7B;--passglow:rgba(50,215,104,.32);--warn:#FFB340;--warnink:#FFB95A;--warnglow:rgba(255,179,64,.32);--fail:#FF6B6B;--failink:#FF8080;--failglow:rgba(255,107,107,.32)}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink1);font:13px/1.45 Inter,system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
.w{padding:12px;display:flex;flex-direction:column;gap:12px;max-width:760px}
.top{display:flex;align-items:center;gap:10px}.logo{width:30px;height:30px;flex:none}
h1{font:800 15px/1.2 "Plus Jakarta Sans",Inter,system-ui,sans-serif;margin:0}h2{font:700 11px/1 ui-monospace,"JetBrains Mono",Menlo,monospace;letter-spacing:.12em;text-transform:uppercase;color:var(--ink3);margin:0}
.mut{color:var(--ink3);font-size:12px}.grow{flex:1;min-width:0}
.mono{font-family:ui-monospace,"JetBrains Mono",Menlo,Consolas,monospace}
.dot{width:8px;height:8px;border-radius:50%;display:inline-block;flex:none;background:var(--off);box-shadow:inset 0 1px 2px rgba(0,0,0,.4)}
.dot.det{background:var(--det);box-shadow:0 0 7px var(--detglow)}.dot.ai{background:var(--ai);box-shadow:0 0 7px var(--aiglow)}
.dot.pass{background:var(--pass);box-shadow:0 0 7px var(--passglow)}.dot.warn{background:var(--warn);box-shadow:0 0 7px var(--warnglow)}
.dot.fail{background:var(--fail);box-shadow:0 0 7px var(--failglow)}.dot.cobalt{background:var(--cobalt)}.dot.pulse{animation:p 1.6s ease-in-out infinite}
@keyframes p{50%{opacity:.35}}
.chip{display:inline-flex;align-items:center;gap:6px;padding:4px 9px;border-radius:99px;background:var(--bg);border:1px solid var(--edge);box-shadow:var(--rsm);font:700 10.5px/1 ui-monospace,"JetBrains Mono",Menlo,monospace;letter-spacing:.06em;text-transform:uppercase;color:var(--ink1);white-space:nowrap}
.chip.inset{box-shadow:var(--latch)}.chip.pass{color:var(--passink)}.chip.review{color:var(--warnink)}.chip.fail{color:var(--failink)}
.seq{list-style:none;margin:0;padding:8px;display:grid;grid-template-columns:repeat(8,minmax(0,1fr));gap:6px;border-radius:14px;background:var(--bg);box-shadow:var(--inset)}
.pad{position:relative;display:flex;flex-direction:column;align-items:center;gap:2px;padding:7px 2px 5px;border-radius:10px;background:linear-gradient(145deg,var(--g1),var(--g2));border:1px solid var(--edge);box-shadow:var(--rsm);color:var(--ink2);min-width:0}
.pad b{font:800 13px/1 ui-monospace,"JetBrains Mono",Menlo,monospace}.pad span{font-size:9.5px;font-weight:600;letter-spacing:.02em;max-width:100%;overflow:hidden;text-overflow:ellipsis}
.pad .dot{position:absolute;top:4px;right:4px;width:6px;height:6px}
.pad.done{background:var(--bg);box-shadow:var(--latch);color:var(--detink)}.pad.active{background:var(--bg);box-shadow:var(--latch),0 0 0 2px var(--cobalt);color:var(--cobaltink)}
.k{display:grid;grid-template-columns:repeat(auto-fit,minmax(104px,1fr));gap:8px}
.c{display:flex;flex-direction:column;gap:5px;min-width:0}.lb{display:flex;align-items:center;gap:6px;font:700 10px/1 ui-monospace,Menlo,monospace;letter-spacing:.1em;text-transform:uppercase;color:var(--ink3)}
.lcd{display:flex;align-items:baseline;justify-content:flex-start;gap:4px;padding:6px 10px;border-radius:8px;background:var(--lcd);box-shadow:var(--inset);font:700 15px/1.2 ui-monospace,"JetBrains Mono",Menlo,monospace;font-variant-numeric:tabular-nums;color:var(--detink)}
.lcd small{font-size:10.5px;font-weight:600;color:var(--ink3)}.lcd.unk{color:var(--ink3);font-weight:600}.lcd.ink{color:var(--ink1)}
.chk{display:flex;gap:6px;flex-wrap:wrap;align-items:center}
.sec{display:flex;flex-direction:column;gap:8px;padding:10px;border-radius:14px;background:var(--bg);box-shadow:var(--rsm);border:1px solid var(--edge)}
.sh{display:flex;align-items:center;gap:8px}
.ls{list-style:none;margin:0;padding:0;display:flex;flex-direction:column;gap:8px}
.dr{display:flex;flex-direction:column;gap:6px;padding:8px 10px;border-radius:10px;box-shadow:var(--latch);position:relative}
.dr:before{content:"";position:absolute;left:0;top:10px;bottom:10px;width:3px;border-radius:0 3px 3px 0;background:var(--ai);box-shadow:0 0 7px var(--aiglow)}
.dh{display:flex;align-items:center;gap:8px;flex-wrap:wrap}.id{font:800 11px/1 ui-monospace,Menlo,monospace;color:var(--aiink);background:var(--lcd);box-shadow:var(--latch);padding:4px 7px;border-radius:6px}
.q{margin:0;overflow-wrap:anywhere}.say{margin:0;color:var(--aiink);text-shadow:0 0 10px var(--aiglow);font-weight:600;overflow-wrap:anywhere}
.tag{display:inline-flex;align-items:center;gap:5px;padding:3px 7px;border-radius:99px;font:700 9.5px/1 ui-monospace,Menlo,monospace;letter-spacing:.08em;text-transform:uppercase}
.tag.ai{color:var(--aiink);border:1px solid color-mix(in srgb,var(--ai) 45%,transparent);background:color-mix(in srgb,var(--ai) 10%,var(--bg));box-shadow:0 0 8px var(--aiglow)}
.tag.rule{color:var(--detink);box-shadow:var(--latch);text-shadow:var(--engrave)}
.row{display:flex;gap:8px;flex-wrap:wrap;align-items:center}
.btn{display:inline-flex;align-items:center;justify-content:center;gap:7px;min-height:30px;padding:6px 12px;border-radius:12px;border:1px solid var(--edge);background:linear-gradient(145deg,var(--g1),var(--g2));box-shadow:var(--rsm);color:var(--ink1);font:700 12px/1 Inter,system-ui,sans-serif;cursor:pointer}
.btn:hover{color:var(--cobaltink)}.btn:active{background:var(--bg);box-shadow:var(--latch)}.btn[disabled]{color:var(--ink3);cursor:not-allowed;opacity:.7}
.btn.big{min-height:36px;padding:8px 14px;font-size:13px}
button:focus-visible{outline:2px solid var(--cobalt);outline-offset:2px}
.note{margin:0;font-size:12px;color:var(--ink2)}.empty{display:flex;align-items:center;gap:8px;color:var(--ink2);padding:4px 2px}
.fine{margin:0;font-size:11px;color:var(--ink3)}
.sk{height:30px;border-radius:8px;background:var(--lcd);box-shadow:var(--latch);animation:p 1.6s ease-in-out infinite}
@media (max-width:420px){.seq{grid-template-columns:repeat(4,minmax(0,1fr))}}
@media (prefers-reduced-motion:reduce){*{animation:none!important}}
</style></head><body><div class="w">
<div class="top"><svg class="logo" viewBox="0 0 32 32" aria-hidden="true"><rect x="2" y="23" width="28" height="6" rx="2" fill="none" stroke="currentColor" stroke-width="1.8" opacity=".55"/><rect x="6" y="15" width="20" height="6" rx="2" fill="none" stroke="currentColor" stroke-width="1.8" opacity=".8"/><rect x="10" y="7" width="12" height="6" rx="2" fill="#0D8A7B" stroke="#0D8A7B" stroke-width="1.8"/></svg>
<div class="grow"><h1>Apartment Planner</h1><div class="mut" id="sub" role="status" aria-live="polite">Loading…</div></div><span class="chip inset" id="mode" hidden></span></div>
<ol class="seq" id="seq" aria-label="Planner stages"></ol>
<div class="k" id="kpi" aria-label="Project figures"><div class="sk"></div><div class="sk"></div><div class="sk"></div></div>
<div class="chk" id="chk"></div>
<section class="sec" aria-labelledby="dh"><div class="sh"><h2 id="dh">Decisions</h2><span class="grow"></span><span id="dcount"></span></div><div id="dec"></div><div id="ans"></div></section>
<div class="row"><button class="btn big" id="ask" type="button"><i class="dot ai"></i>Answer waiting decisions</button></div>
<p class="fine">Automated pre-check. Confirm with the project's licensed consultant.</p></div>
<script>
let id=0;const pend={};
function rpc(method,params){return new Promise((res,rej)=>{const i=++id;pend[i]={res,rej};parent.postMessage({jsonrpc:"2.0",id:i,method,params},"*")})}
window.addEventListener("message",e=>{const m=e.data||{};if(m.id&&pend[m.id]){m.error?pend[m.id].rej(m.error):pend[m.id].res(m.result);delete pend[m.id];return}
 if(m.method==="ui/notifications/tool-result"){render(m.params&&(m.params.structuredContent||parse(m.params)))}});
function parse(p){try{return JSON.parse(p.content[0].text)}catch(_){return null}}
function esc(s){return String(s).replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]))}
const STAGES=["Find","Trace","Climate","Place","Shape","Split","Wire","Check"];
const $=i=>document.getElementById(i);
let got=false,canReview=true;
function num(v){return typeof v==="number"&&isFinite(v)?v.toLocaleString(undefined,{maximumFractionDigits:1}):null}
function lcd(label,v,unit,tone){const t=num(v);return '<div class="c"><span class="lb"><i class="dot '+(t==null?"":"det")+'"></i>'+esc(label)+'</span><output class="lcd '+(t==null?"unk":tone||"")+'" title="'+(t==null?"Not known yet":"Computed by the rule engine")+'">'+(t==null?"–":esc(t)+"<small>"+esc(unit)+"</small>")+"</output></div>"}
function chip(cls,led,txt,n){return '<span class="chip inset '+cls+'"><i class="dot '+led+'"></i>'+txt+" "+n+"</span>"}
function render(d){if(!d)return;got=true;const s=d.summary||d;
 $("sub").textContent=(s.project||"Project")+" · stage "+(s.stage||"?")+(s.stage_name?" "+s.stage_name:"");
 const md=$("mode");if(d.mode){md.hidden=false;md.innerHTML='<i class="dot '+(d.mode==="app"?"cobalt":"idle")+'"></i>'+(d.mode==="app"?"Live in app":"Headless")}
 const cur=Number(s.stage)||0;
 $("seq").innerHTML=STAGES.map((n,i)=>{const k=i+1,st=k<cur?"done":k===cur?"active":"todo";
  return '<li class="pad '+st+'" '+(st==="active"?'aria-current="step" ':"")+'aria-label="Stage '+k+" "+n+", "+(st==="done"?"done":st==="active"?"current":"not reached")+'"><i class="dot '+(st==="done"?"det":st==="active"?"cobalt":"")+'"></i><b>'+k+"</b><span>"+n+"</span></li>"}).join("");
 const site=s.site||{};
 $("kpi").innerHTML=lcd("Plot",site.area_m2,"m²")+lcd("Buildable",site.buildable_m2,"m²")+lcd("Carpet",s.carpet_m2,"m²");
 if(s.checks&&s.checks.counts){const c=s.checks.counts;$("chk").innerHTML='<span class="lb">Checks</span>'+chip("pass","pass","PASS",c.pass||0)+chip("review","warn","REVIEW",c.review||0)+chip("fail","fail","FAIL",c.fail||0)}
 else $("chk").innerHTML='<span class="lb">Checks</span><span class="mut">None yet. Solve a flat or import a DXF.</span>';
 const w=Number(s.decisions_waiting)||0;
 $("dcount").innerHTML=w?'<span class="chip"><i class="dot ai pulse"></i>'+w+" waiting</span>":'<span class="chip inset"><i class="dot det"></i>0 waiting</span>';
 const dec=(d.decisions||[]).slice(0,6);
 $("dec").innerHTML=dec.length?'<ul class="ls">'+dec.map(t=>'<li class="dr" data-id="'+esc(t.id)+'"><div class="dh"><span class="id">'+esc(t.id)+'</span><span class="tag rule"><i class="dot det"></i>Default on</span><span class="tag ai"><i class="dot ai"></i>Check me</span></div><p class="q">'+esc(t.question)+'</p><div class="row"><button class="btn" type="button" data-act="ask" data-id="'+esc(t.id)+'"><i class="dot ai"></i>Ask Claude</button>'+(canReview?'<button class="btn" type="button" data-act="keep" data-id="'+esc(t.id)+'"><i class="dot det"></i>Keep default</button>':"")+'</div><p class="note" data-note></p></li>').join("")+"</ul>"+(w>dec.length?'<p class="fine">+'+(w-dec.length)+" more in the planner.</p>":""):'<div class="empty"><i class="dot det"></i>Nothing waiting. Every choice is settled.</div>';
 $("ask").disabled=!w;
 loadAnswered()}
function loadAnswered(){rpc("tools/call",{name:"list_decisions",arguments:{status:"answered"}}).then(r=>{const a=parse(r);if(!Array.isArray(a)||!a.length){$("ans").innerHTML="";return}
  $("ans").innerHTML='<h2 style="margin:6px 0 8px">Claude answered</h2><ul class="ls">'+a.slice(0,6).map(t=>{const o=(t.options||[]).find(x=>x.id===t.applied_option);const say=(t.answer&&t.answer.text)||(o&&o.label)||t.applied_option||"";
   return '<li class="dr" data-id="'+esc(t.id)+'"><div class="dh"><span class="id">'+esc(t.id)+'</span><span class="tag ai"><i class="dot ai"></i>Co-pilot</span></div><p class="q">'+esc(t.question)+'</p><p class="say">'+esc(say)+"</p>"+(t.answer&&t.answer.reason?'<p class="note">“'+esc(t.answer.reason)+'”</p>':"")+(canReview?'<div class="row"><button class="btn" type="button" data-act="keep" data-id="'+esc(t.id)+'"><i class="dot det"></i>Keep</button><button class="btn" type="button" data-act="undo" data-id="'+esc(t.id)+'">Undo</button></div>':"")+'<p class="note" data-note></p></li>'}).join("")+"</ul>"}).catch(()=>{$("ans").innerHTML=""})}
function note(li,t){const n=li&&li.querySelector("[data-note]");if(n)n.textContent=t}
function review(btn,keep){const li=btn.closest("li"),tid=btn.dataset.id;note(li,"Working…");li.querySelectorAll("button").forEach(b=>b.disabled=true);
 rpc("tools/call",{name:"review_decision",arguments:{ticket_id:tid,keep:keep}}).then(r=>{if(r&&r.isError)throw new Error("x");note(li,keep?"Kept.":"Undone. Safe default is back.");
  rpc("tools/call",{name:"open_planner",arguments:{}}).then(r2=>render(parse(r2))).catch(()=>{})})
 .catch(()=>{canReview=false;li.querySelectorAll("[data-act=keep],[data-act=undo]").forEach(b=>b.remove());li.querySelectorAll("button").forEach(b=>b.disabled=false);note(li,"Keep and Undo are done in the planner app. Open it to decide.")})}
document.addEventListener("click",e=>{const b=e.target.closest&&e.target.closest("button[data-act]");if(!b)return;
 if(b.dataset.act==="ask")rpc("ui/message",{role:"user",content:[{type:"text",text:"Please answer the Apartment Planner decision "+b.dataset.id+" with a short reason."}]}).catch(()=>{});
 else review(b,b.dataset.act==="keep")});
document.getElementById("ask").onclick=()=>rpc("ui/message",{role:"user",content:[{type:"text",text:"Please list the waiting Apartment Planner decisions and answer them, with a short reason for each."}]}).catch(()=>{});
setTimeout(()=>{if(!got){$("sub").textContent="No data yet. Ask Claude to open the planner.";$("kpi").innerHTML="";$("seq").innerHTML=""}},9000);
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
