"""``planner`` command line: serve the app, run the bridge, link Claude, doctor, golden gates, demo, check."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__


def _print(obj) -> None:
    print(json.dumps(obj, indent=2, default=str))


def cmd_serve(a) -> None:
    from .server import serve

    serve(a.project, a.port, open_browser=not a.no_browser, parent_pid=a.parent_pid)


def cmd_mcp(a) -> None:
    from .mcp_server import main

    main(["--stdio"])


def cmd_doctor(a) -> int:
    from .doctor import run_doctor

    print(f"Apartment Planner {__version__} - doctor")
    rep = run_doctor(verbose=not a.json)
    if a.json:
        _print(rep)
    return 0 if rep["ok"] else 1


def cmd_link(a) -> int:
    from . import claude_link as cl

    if a.unlink:
        _print(cl.unlink())
        return 0
    st = cl.status()
    print("1 Look:", "Claude Desktop found" if st["app"]["installed"] else "Claude Desktop not found", "-", st["config"])
    plan = cl.plan_link()
    if "error" in plan:
        print("  ", plan["error"])
        return 1
    if plan["already_linked"]:
        print("Already linked. Restart Claude if the planner tools are missing.")
        return 0
    print("2 Ask: this change will be made:\n")
    print(plan["diff"] or "(new file)")
    if not a.yes:
        if a.dry_run:
            return 0
        ans = input("\nAdd Apartment Planner to Claude? [y/N] ").strip().lower()
        if ans not in ("y", "yes"):
            print("Nothing changed.")
            return 0
    res = cl.link(confirm=True)
    print("3 Back up, then add:", "backup " + str(res.get("backup")) if res.get("backup") else "new config written")
    print("4 Restart Claude:", res["message"])
    print("5 Handshake: open Claude and ask it to 'open the apartment planner'. The badge in the app turns green.")
    print("\nClaude Code instead:", cl.claude_code_command())
    return 0


def cmd_golden(a) -> int:
    from . import golden

    if a.gate:
        fn = getattr(golden, f"gate_{a.gate}")
        res = fn()
    else:
        res = golden.run_all(quick=a.quick)
    if not a.verbose and isinstance(res, dict):
        if "rows" in res:
            res = {k: v for k, v in res.items() if k != "rows"}
        for v in res.values():
            if isinstance(v, dict):
                v.pop("rows", None)
    _print(res)
    return 0 if res.get("ok") else 1


def cmd_demo(a) -> int:
    """Run the video's flow end to end on the golden Lokhandwala plot and write a DXF."""
    from .engine import Planner
    from .golden import lokhandwala

    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    proj = out / "lokhandwala.aptplan"
    if proj.exists():
        proj.unlink()
    P = Planner(proj, "Lokhandwala demo")
    g = lokhandwala()
    print("1 Find:", P.search_place(g["search"])[0]["display_name"])
    s = P.set_site(g["lnglat"], locality="Andheri W")
    print(f"2 Trace: {s['area']:.1f} m², perimeter {s['perimeter']:.1f} m, edges {s['edges']}")
    print(f"3 Climate: setback {s['setback']} m, buildable {s['buildable_area']:.1f} m², noon sun "
          f"{[p['noon_altitude'] for p in s['climate']['sun']]}, wind {s['climate']['wind']['prevailing']}")
    pl = P.place_towers()
    for o in pl["options"]:
        print(f"4 Place {o['id']}: {o['towers']} x {[p['area'] for p in o['plates']]} m², {o['coverage']:.0%}, gap {o['min_gap']}")
    P.choose_placement("A")
    r = P.set_plate("T1", a.shape, {})
    pz = r["plate"]
    print(f"5 Shape: {pz['shape']} {pz['corners']} corners, {pz['width']} x {pz['depth']} m, {pz['area']} m², zones "
          f"{ {z['kind']: z['area'] for z in pz['zones']} }")
    print("6 Split:", [(u["id"], u["area"], u["programme"]["id"]) for u in r["units"]])
    res = P.solve_all()
    print(f"7 Wire+solve: {res['solved']}/{res['total']} flats in {res['time_s']} s")
    sm = P.summary()
    print(f"8 Check: {sm.get('checks')}, carpet {sm.get('carpet_m2')} m², mix {sm.get('mix')}")
    dx = P.export_dxf(str(out / "lokhandwala_T1.dxf"))
    print("DXF:", dx["path"], dx["entities"], "entities")
    print("Decisions waiting:", sm["decisions_waiting"], "| decision stats:", P.decision_stats())
    return 0


def cmd_check(a) -> int:
    from .checks import FloorContext, run_checks
    from .dxf_io import import_dxf

    res = import_dxf(a.dxf)
    sm = run_checks(FloorContext(plans=res.plans, height=a.height, passage_width=res.passage_width, stairs=res.stairs,
                                 handles=res.handles))
    _print({"drawing": res.report.model_dump(exclude={"layers"}), "counts": sm.counts, "by_code": sm.by_code,
            "problems": [r.model_dump() for r in sm.results if r.result in ("fail", "review")],
            "tickets": res.tickets, "disclaimer": sm.disclaimer})
    return 0 if sm.counts.get("fail", 0) == 0 else 2


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="planner", description="Parametric Apartment Planner")
    ap.add_argument("--version", action="version", version=__version__)
    sub = ap.add_subparsers(dest="cmd")
    s = sub.add_parser("serve", help="run the engine + UI on 127.0.0.1")
    s.add_argument("--project")
    s.add_argument("--port", type=int)
    s.add_argument("--no-browser", action="store_true")
    s.add_argument("--parent-pid", type=int, help="exit when this process exits (set by the desktop app)")
    s.set_defaults(fn=cmd_serve)
    m = sub.add_parser("mcp", help="run the Claude bridge (MCP over stdio)")
    m.set_defaults(fn=cmd_mcp)
    d = sub.add_parser("doctor", help="test the engine, Claude link and data files")
    d.add_argument("--json", action="store_true")
    d.set_defaults(fn=cmd_doctor)
    lk = sub.add_parser("link-claude", help="add the planner to Claude Desktop (shows the change, backs up first)")
    lk.add_argument("--yes", action="store_true")
    lk.add_argument("--dry-run", action="store_true")
    lk.add_argument("--unlink", action="store_true")
    lk.set_defaults(fn=cmd_link)
    g = sub.add_parser("golden", help="run the release gates on the golden set")
    g.add_argument("--gate", choices=["geocode", "trace", "place", "zones", "flats", "dxf", "rules", "decisions"])
    g.add_argument("--quick", action="store_true")
    g.add_argument("--verbose", action="store_true")
    g.set_defaults(fn=cmd_golden)
    dm = sub.add_parser("demo", help="run the full flow on the golden Lokhandwala plot")
    dm.add_argument("--out", default="planner-demo")
    dm.add_argument("--shape", default="cruciform")
    dm.set_defaults(fn=cmd_demo)
    ck = sub.add_parser("check", help="check a DXF floor plan against DCPR 2034 / NBC 2016")
    ck.add_argument("dxf")
    ck.add_argument("--height", type=float, default=24.0)
    ck.set_defaults(fn=cmd_check)
    a = ap.parse_args(argv)
    from .telemetry import init as init_crash_reports

    init_crash_reports()
    if not getattr(a, "fn", None):
        ap.print_help()
        return 0
    rc = a.fn(a)
    return int(rc or 0)


if __name__ == "__main__":
    sys.exit(main())
