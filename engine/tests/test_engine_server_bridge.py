import json
import os
import sys

import pytest

from planner_engine import claude_link
from planner_engine.engine import Planner, PlannerError


@pytest.fixture
def planner(tmp_path, lokhandwala):
    P = Planner(tmp_path / "p.aptplan", "Test")
    P.set_site(lokhandwala["lnglat"], locality="Andheri W")
    P.place_towers()
    P.choose_placement("A")
    P.set_plate("T1", "cruciform", {})
    return P


def test_flow_and_tickets(planner):
    s = planner.summary()
    assert s["stage"] >= 6 and s["plates"][0]["corners"] == 12
    ids = [d["id"] for d in planner.list_decisions()]
    assert "T0:sea_view" in ids and "T5:A" in ids
    # Claude answers a T0 ticket; a bad option is rejected
    with pytest.raises(PlannerError):
        planner.answer_decision("T0:sea_view", option_id="maybe")
    r = planner.answer_decision("T0:sea_view", option_id="no", reason="no coast names within 1 km")
    assert r["status"] == "answered"
    assert planner.project.site.climate.sea_view == "no"
    # T5 note with an invented number is rejected; a faithful one is accepted
    with pytest.raises(PlannerError):
        planner.answer_decision("T5:A", text="Coverage is 77% on this plot.")
    ok = planner.answer_decision("T5:A", text="One slim tower takes the WNW breeze and keeps the west short.")
    assert ok["status"] == "answered"
    planner.review_decision("T5:A", keep=False)
    assert planner.project.placement.options[0].drivers.startswith("Design drivers")


def test_solve_check_export_and_stats(planner, tmp_path):
    uid = planner.project.units[0].id
    res = planner.solve_unit(uid)
    assert res["plan"]
    chk = planner.run_checks()
    assert chk["counts"]["pass"] > 0
    rid = chk["results"][0]["rule_id"]
    ex = planner.explain_check(rid)
    assert ex["disclaimer"].startswith("Automated pre-check")
    out = planner.export_dxf(str(tmp_path / "x.dxf"))
    assert out["entities"] > 20
    st = planner.decision_stats()
    assert st["total"] > 0 and 1 - st["defaults"] / st["total"] >= 0.9
    # edit in words -> constraint change -> diff
    e = planner.apply_edit(uid, {"action": "resize", "room": "living", "target_area": 20}, reason="bigger living")
    assert "diff" in e or e.get("ok") is False


def test_programme_proposal_checked(planner):
    uid = planner.project.units[0].id
    bad = [{"id": "living", "type": "living", "name": "Living", "target_area": 400, "needs_window": True}]
    with pytest.raises(PlannerError):
        planner.propose_programme(uid, "palace", rooms=bad, links=[], entry="living")
    good = [
        {"id": "foyer", "type": "foyer", "name": "Foyer", "target_area": 3},
        {"id": "living", "type": "living", "name": "Living", "target_area": 18, "needs_window": True},
        {"id": "kitchen", "type": "kitchen", "name": "Kitchen", "target_area": 7, "needs_window": True},
        {"id": "master", "type": "master", "name": "Master", "target_area": 13, "needs_window": True},
        {"id": "pooja", "type": "pooja", "name": "Pooja", "target_area": 2},
        {"id": "toilet", "type": "toilet", "name": "Toilet", "target_area": 3.5},
    ]
    links = [["foyer", "living"], ["living", "kitchen"], ["living", "master"], ["living", "pooja"], ["living", "toilet"]]
    r = planner.propose_programme(uid, "1BHK with pooja", rooms=good, links=links, entry="foyer")
    assert r["status"] == "answered"


def test_undo_and_reopen(planner, tmp_path):
    before = planner.summary()["plates"][0]["shape"]
    planner.set_plate("T1", "central_core", {})
    planner.undo()
    assert planner.summary()["plates"][0]["shape"] == before
    P2 = Planner(tmp_path / "p.aptplan")
    assert P2.summary()["plates"][0]["shape"] == before


def test_rpc_server(planner):
    from fastapi.testclient import TestClient

    from planner_engine.server import create_app

    app = create_app(planner, token="tok")
    c = TestClient(app)
    assert c.get("/api/health").json()["ok"]
    assert c.post("/rpc", json={"jsonrpc": "2.0", "id": 1, "method": "summary"}).status_code == 401
    h = {"X-Planner-Token": "tok"}
    r = c.post("/rpc", json={"jsonrpc": "2.0", "id": 1, "method": "summary"}, headers=h).json()
    assert r["result"]["project"] == "Test"
    r = c.post("/rpc", json={"jsonrpc": "2.0", "id": 2, "method": "os_system", "params": {}}, headers=h).json()
    assert r["error"]["code"] == -32601
    r = c.post("/rpc", json={"jsonrpc": "2.0", "id": 3, "method": "measure",
                             "params": {"lnglat": [[72.8, 19.1], [72.8005, 19.1], [72.8005, 19.1005]]}}, headers=h).json()
    assert r["result"]["area"] > 0
    assert c.get("/api/token", headers={"sec-fetch-site": "cross-site"}).status_code == 403
    assert c.get("/api/token").json()["token"] == "tok"


def test_claude_link_backs_up_and_only_adds(tmp_path):
    cfg = tmp_path / "claude" / "claude_desktop_config.json"
    cfg.parent.mkdir(parents=True)
    cfg.write_text(json.dumps({"mcpServers": {"other": {"command": "x"}}, "theme": "dark"}))
    plan = claude_link.plan_link(cfg)
    assert "apartment-planner" in plan["diff"] and not plan["already_linked"]
    assert claude_link.link(cfg, confirm=False)["written"] is False
    res = claude_link.link(cfg, confirm=True)
    assert res["written"] and res["backup"] and os.path.exists(res["backup"])
    data = json.loads(cfg.read_text())
    assert data["mcpServers"]["other"] == {"command": "x"} and data["theme"] == "dark"
    assert "apartment-planner" in data["mcpServers"]
    assert claude_link.plan_link(cfg)["already_linked"]
    assert "claude mcp add apartment-planner" in claude_link.claude_code_command()


@pytest.mark.anyio
async def test_mcp_tools_in_memory(planner, monkeypatch):
    from mcp.client.client import Client

    from planner_engine import mcp_server

    class B:
        mode = "headless"

        def __init__(self, p):
            self.p = p

        def call(self, method, **kw):
            return getattr(self.p, method)(**{k: v for k, v in kw.items() if v is not None})

    monkeypatch.setattr(mcp_server, "_backend", B(planner))
    server = mcp_server.build_server()
    async with Client(server) as c:
        tools = {t.name for t in (await c.list_tools()).tools}
        assert {"open_planner", "get_project", "list_decisions", "answer_decision", "propose_programme",
                "apply_edit", "compare_options", "explain_check"} <= tools
        r = await c.call_tool("list_decisions", {"status": "waiting"})
        data = json.loads(r.content[0].text)
        assert any(d["id"] == "T0:creek" for d in data)
        r = await c.call_tool("answer_decision", {"ticket_id": "T0:creek", "option_id": "no", "reason": "test"})
        assert json.loads(r.content[0].text)["status"] == "answered"
        res = await c.list_resources()
        assert any(str(x.uri).startswith("ui://apartment-planner") for x in res.resources)


def test_mcp_stdio_subprocess(tmp_path):
    """The way Claude Desktop starts the bridge: a child process speaking MCP on stdio."""
    import anyio
    from mcp.client.session import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    async def go():
        params = StdioServerParameters(command=sys.executable, args=["-m", "planner_engine.mcp_server", "--stdio"],
                                       env={**os.environ})
        async with stdio_client(params) as (r, w):
            async with ClientSession(r, w) as s:
                await s.initialize()
                tools = await s.list_tools()
                assert "answer_decision" in {t.name for t in tools.tools}
                out = await s.call_tool("get_project", {"detail": "summary"})
                assert json.loads(out.content[0].text)["engine"]

    anyio.run(go)


@pytest.fixture
def anyio_backend():
    return "asyncio"


def test_bridge_uses_console_python_not_pythonw(tmp_path):
    from planner_engine import claude_link

    scripts = tmp_path / "Scripts"
    scripts.mkdir()
    (scripts / "python.exe").write_text("")
    assert claude_link.console_python(str(scripts / "pythonw.exe")) == str(scripts / "python.exe")
    assert claude_link.console_python(str(scripts / "python.exe")) == str(scripts / "python.exe")
    assert "claude_code_installed" in claude_link.status()
