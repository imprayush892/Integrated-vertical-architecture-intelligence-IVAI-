"""Approved-plan benchmark for the plan critic (``planner_engine.quality``).

The critic is only trustworthy if the flats Mumbai's authorities approved mostly come out as A/B. This module
turns a record of ``data/mined/graphs_v2.jsonl.gz`` (room polygons, door / open / wall edges, entrance point, facade
data; see ``docs/quality/graph-dataset.md``) into an approximate ``Plan`` the critic can score, draws a stratified
sample, scores it and tabulates which metrics fail approved flats.

What the conversion keeps and what it cannot know (drives ``EXCLUDE``):

* rooms: one ``PlanRoom`` per node, polygon as mined (5 cm simplification), area from the polygon. Untyped cells
  (``unknown`` / ``unlabeled``, 15 % of all nodes) are resolved by a rule: the entrance cell is a foyer, a cell joining two
  or more rooms is a passage (the label was lost), a small cell hanging off a bedroom is a dress/wardrobe niche, tiny or
  isolated cells are dropped (ledges, shafts, wardrobes). Door links the extractor missed are repaired through the best
  shared wall (a room without any door route; a bedroom / passage reachable only through a bedroom, kitchen or toilet).
  A flat is "usable" when at most 12 % of its floor was guessed, 8 % dropped and no room stays unreachable (or reachable
  only through a bedroom / kitchen / toilet) after the repair.
* doors: one opening per ``door`` / ``open`` edge at the edge's point, laid along the shared wall (0.9 m, 0.75 m for a
  toilet, an opening is as wide as the contact, 0.9-2.4 m). The extractor has no door hinge and no leaf side, so swing
  based metrics are meaningless; no ``Plan.walls``.
* entry: the entrance point on the boundary of the entrance room, a 1.0 m opening.
* windows: only "the room touches the facade" (``has_window``); no window widths, so ``window_ratio`` is not scored.
* ventilation: facade contact or the drawing's shaft flag; half of the toilets have neither because ducts and AVS are not
  drawn, so ``wet_vent`` / ``wet_duct`` are not scored.
* no gross area and no stair: ``efficiency`` and ``travel_to_exit`` are not scored.
"""

from __future__ import annotations

import gzip
import json
import random
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import numpy as np
from shapely.geometry import Point, Polygon

from .models import Opening, Plan, PlanRoom

DATA = Path(__file__).resolve().parents[2] / "data" / "mined" / "graphs_v2.jsonl.gz"

# metrics the converted record cannot support (see module docstring); removed from the score of approved flats
EXCLUDE: tuple[str, ...] = ("swing_clash", "swing_free", "passage_door", "door_width", "window_ratio", "wet_vent", "wet_duct",
                            "efficiency", "travel_to_exit", "hab_window", "privacy")

KNOWN = {"living", "dining", "kitchen", "bedroom", "master", "toilet", "passage", "balcony", "utility", "store", "study",
         "pooja", "dress", "foyer"}
ALIAS = {"servant": "utility", "alcove": "dress", "multipurpose": "living", "wc": "toilet", "bath": "toilet"}
PUBLIC = {"living", "dining", "passage", "foyer", "kitchen"}
WET = {"toilet"}


# ------------------------------------------------------------------------------------------------ loading


def load_flats(path: str | Path | None = None) -> list[dict[str, Any]]:
    """Every kept flat of the graph dataset as ``{"flat": record, "project": pdf, "page": n}``."""
    out = []
    with gzip.open(path or DATA, "rt") as f:
        for line in f:
            rec = json.loads(line)
            if rec.get("type") != "page":
                continue
            for fl in rec["flats"]:
                out.append({"flat": fl, "project": rec["pdf"], "page": rec["page"]})
    return out


# ------------------------------------------------------------------------------------------------ conversion


def _tangent_at(poly: Polygon, p: tuple[float, float]) -> np.ndarray:
    """Unit direction of the polygon edge nearest to ``p``."""
    pts = np.asarray(poly.exterior.coords)
    best, bt = 1e18, np.array([1.0, 0.0])
    q = np.array(p, float)
    for a, b in zip(pts[:-1], pts[1:], strict=False):
        ab = b - a
        L = float(np.hypot(*ab))
        if L < 1e-6:
            continue
        t = float(np.clip(np.dot(q - a, ab) / (L * L), 0, 1))
        d = float(np.hypot(*(a + t * ab - q)))
        if d < best:
            best, bt = d, ab / L
    return bt


# door repair preference: for a room with no door found, the neighbour it most plausibly opens on (higher = better)
_REPAIR: dict[str, dict[str, float]] = {
    "toilet": {"bedroom": 1.0, "master": 1.0, "passage": 0.8, "foyer": 0.7, "living": 0.5, "dining": 0.5, "dress": 0.4, "kitchen": 0.0},
    "bedroom": {"passage": 1.0, "foyer": 0.9, "living": 0.8, "dining": 0.7},
    "master": {"passage": 1.0, "foyer": 0.9, "living": 0.8, "dining": 0.7},
    "kitchen": {"dining": 1.0, "living": 0.9, "passage": 0.7, "foyer": 0.6},
    "passage": {"living": 1.0, "dining": 0.9, "foyer": 0.9, "passage": 0.5},
    "*": {"passage": 1.0, "living": 0.8, "dining": 0.8, "bedroom": 0.6, "master": 0.6, "kitchen": 0.4},
}


def _contact_point(pa: Polygon, pb: Polygon) -> tuple[float, float]:
    """Midpoint of the shared wall of two rooms (centre of the closest approach when they only almost touch)."""
    inter = pa.exterior.buffer(0.4).intersection(pb.exterior)
    if not inter.is_empty and inter.length > 0.05:
        geoms = list(inter.geoms) if hasattr(inter, "geoms") else [inter]
        g = max(geoms, key=lambda x: x.length)
        c = g.interpolate(0.5, normalized=True)
        return (c.x, c.y)
    from shapely.ops import nearest_points

    a, b = nearest_points(pa, pb)
    return ((a.x + b.x) / 2, (a.y + b.y) / 2)


def _contact_len(pa: Polygon, pb: Polygon, p: tuple[float, float], radius: float = 1.5) -> float:
    inter = pa.exterior.buffer(0.25).intersection(pb.exterior)
    if inter.is_empty:
        return 0.0
    return float(inter.intersection(Point(*p).buffer(radius)).length)


def convert(rec: dict[str, Any]) -> tuple[Plan, dict[str, Any]] | None:
    """Graph record (``{"flat", "project", "page"}`` or a bare flat) -> (Plan, info); None when it cannot be converted."""
    fl = rec["flat"] if "flat" in rec else rec
    nodes = {n["id"]: n for n in fl["nodes"]}
    polys: dict[int, Polygon] = {}
    for nid, n in nodes.items():
        try:
            p = Polygon(n["polygon"]).buffer(0)
        except Exception:
            continue
        if p.geom_type == "MultiPolygon":
            p = max(p.geoms, key=lambda g: g.area)
        if p.is_empty or p.area < 0.3:
            continue
        # the mined polygons are raster masks (door gaps, dimension text notches): close the notches and simplify so that
        # shape / furniture metrics see a drawn room (median 5 corners, rectangularity 0.96 on bedrooms, living, kitchen)
        q = p.buffer(0.35, join_style="mitre").buffer(-0.35, join_style="mitre").simplify(0.15)
        if q.geom_type == "MultiPolygon":
            q = max(q.geoms, key=lambda g: g.area)
        polys[nid] = q if (q.geom_type == "Polygon" and not q.is_empty and q.area > 0.3) else p
    ent = fl.get("entrance") or {}
    ent_room = ent.get("room")
    if ent_room not in polys or not ent.get("point"):
        return None
    link = [e for e in fl["edges"] if e["kind"] in ("door", "open") and e["a"] in polys and e["b"] in polys]
    deg: Counter = Counter()
    nbr: dict[int, set[int]] = defaultdict(set)
    for e in link:
        deg[e["a"]] += 1
        deg[e["b"]] += 1
        nbr[e["a"]].add(e["b"])
        nbr[e["b"]].add(e["a"])
    typ: dict[int, str] = {}
    for nid, n in nodes.items():
        if nid in polys and (n["type"] in KNOWN or n["type"] in ALIAS):
            typ[nid] = ALIAS.get(n["type"], n["type"])
    guessed = dropped = 0.0
    # untyped cells: entrance cell, then passage / niche / drop (two passes: passages may hang off passages)
    for _pass in range(2):
        for nid, n in nodes.items():
            if nid not in polys or nid in typ or n["type"] not in ("unknown", "unlabeled"):
                continue
            a = polys[nid].area
            nt = {typ.get(x) for x in nbr[nid]}
            if nid == ent_room:
                typ[nid] = "foyer" if a < 9 else "living" if "living" not in typ.values() else "passage"
                guessed += a
            elif deg[nid] >= 2 and a <= 14 and (nt & (PUBLIC | {"bedroom", "master"})):
                typ[nid] = "passage"
                guessed += a
            elif deg[nid] >= 1 and 2.0 <= a < 7 and nt & {"bedroom", "master"}:
                typ[nid] = "dress"
                guessed += a
            elif _pass == 1:
                typ[nid] = "?"
                dropped += a
    keep = {nid for nid, t in typ.items() if t != "?"}
    if ent_room not in keep:
        return None
    rooms: list[PlanRoom] = []
    rid: dict[int, str] = {}
    cnt: Counter = Counter()
    for nid in sorted(keep):
        n, p = nodes[nid], polys[nid]
        t = typ[nid]
        cnt[t] += 1
        rid[nid] = f"r{nid}_{t}"
        facade = n["facade_len"] >= 0.7 or bool(n.get("facade_via_balcony"))
        wetr = t in WET
        win = facade and not wetr
        vent = wetr and (n["facade_len"] >= 0.5 or bool(n.get("shaft")))
        width = float(n.get("clear_width") or 0) or float(min(*_mrr_wh(p)))
        rooms.append(PlanRoom(
            id=rid[nid], type=t, name=f"{t.title()} {cnt[t]}", polygon=[tuple(c) for c in p.exterior.coords[:-1]],
            area=round(p.area, 2), width=round(width, 2), has_window=win, window_area=0.0, ventilated=vent or win,
            vent_via="window" if (wetr and n["facade_len"] >= 0.5) else "shaft" if vent else None))
    # doors
    doors: list[Opening] = []
    seen: set[tuple[int, int]] = set()
    for e in sorted(link, key=lambda e: e["kind"] != "door"):
        a, b = e["a"], e["b"]
        if a not in keep or b not in keep or (min(a, b), max(a, b)) in seen:
            continue
        seen.add((min(a, b), max(a, b)))
        pa, pb = polys[a], polys[b]
        p = tuple(e["point"])
        t = _tangent_at(pa, p)
        wet = typ[a] in WET or typ[b] in WET
        if e["kind"] == "door":
            w = 0.75 if wet else 0.9
        else:
            w = float(np.clip(_contact_len(pa, pb, p), 0.9, 2.4))
        swing = b if typ[a] in PUBLIC and typ[b] not in PUBLIC else a if typ[b] in PUBLIC and typ[a] not in PUBLIC else b
        c = np.array(p, float)
        doors.append(Opening(id=f"D{len(doors) + 1}", kind="door", a=tuple(c - t * w / 2), b=tuple(c + t * w / 2), width=round(w, 2),
                             rooms=[rid[a], rid[b]], hinge=tuple(c - t * w / 2), swing_into=rid[swing]))
    # link-extraction gaps (the graph holds door / open links only where the extractor saw an opening): (1) a room with no
    # door route from the entry gets a door through its best shared wall; (2) a bedroom / passage / kitchen reachable only by
    # crossing a bedroom, kitchen or toilet gets a door through a shared wall with the public route. Counted in ``repaired``.
    repaired = 0
    adj: dict[int, set[int]] = defaultdict(set)
    for a, b in seen:
        adj[a].add(b)
        adj[b].add(a)
    walls: list[dict[str, Any]] = []  # candidate shared walls, built on demand from the polygons (the extractor lists only some)

    def _walls() -> list[dict[str, Any]]:
        if not walls:
            ks = sorted(keep)
            for i, a in enumerate(ks):
                for b in ks[i + 1:]:
                    if polys[a].distance(polys[b]) < 0.5:
                        ln = float(polys[a].exterior.buffer(0.3).intersection(polys[b].exterior).length) / 2
                        if ln >= 0.5:
                            walls.append({"a": a, "b": b, "len": ln})
            walls.append({"a": -1, "b": -1, "len": 0})  # marker: computed
        return walls

    no_transit = {x for x in keep if typ[x] in ("bedroom", "master", "kitchen", "toilet", "utility")}

    def _bfs(blocked: set[int]) -> set[int]:
        reach, stack = {ent_room}, [ent_room]
        while stack:
            u = stack.pop()
            if u != ent_room and u in blocked:
                continue
            for v in adj[u]:
                if v not in reach:
                    reach.add(v)
                    stack.append(v)
        return reach

    def _add(u: int, v: int) -> None:
        pu, pv = polys[u], polys[v]
        q = _contact_point(pu, pv)
        t = _tangent_at(pu, q)
        w = 0.75 if (typ[u] in WET or typ[v] in WET) else 0.9
        c = np.array(q, float)
        swing = u if typ[u] not in PUBLIC else v
        doors.append(Opening(id=f"D{len(doors) + 1}", kind="door", a=tuple(c - t * w / 2), b=tuple(c + t * w / 2), width=w,
                             rooms=[rid[u], rid[v]], hinge=tuple(c - t * w / 2), swing_into=rid[swing]))
        seen.add((min(u, v), max(u, v)))
        adj[u].add(v)
        adj[v].add(u)

    for phase in (1, 2):
        for _ in range(16):
            if phase == 1:
                reach = _bfs(set())
                lost = {x for x in keep if x not in reach and typ[x] != "balcony"}
                src = {v for v in reach if typ[v] != "balcony"}
            else:
                reach = _bfs(no_transit)
                lost = {x for x in keep if x not in reach and typ[x] in ("bedroom", "master", "passage", "kitchen", "dining", "study")}
                src = {v for v in reach if typ[v] in ("living", "dining", "foyer", "passage")}
            if not lost:
                break
            best = None
            for e in _walls():
                if e["a"] < 0 or (min(e["a"], e["b"]), max(e["a"], e["b"])) in seen or e["len"] < (0.5 if phase == 1 else 0.8):
                    continue
                for u, v in ((e["a"], e["b"]), (e["b"], e["a"])):
                    if u in lost and v in src:
                        pref = _REPAIR.get(typ[u], _REPAIR["*"]).get(typ[v])
                        if pref is None:
                            continue
                        sc = pref + min(e["len"], 3.0) / 10
                        if best is None or sc > best[0]:
                            best = (sc, u, v)
            if best is None:
                break
            _add(best[1], best[2])
            repaired += 1
    reach_all = _bfs(set())
    reach_pub = _bfs(no_transit)
    unresolved = len([x for x in keep if typ[x] != "balcony" and x not in reach_all]) + len(
        [x for x in keep if typ[x] in ("bedroom", "master", "passage", "kitchen", "dining", "study") and x in reach_all and x not in reach_pub])
    pe = tuple(ent["point"])
    te = _tangent_at(polys[ent_room], pe)
    c = np.array(pe, float)
    doors.append(Opening(id="D-entry", kind="entry", a=tuple(c - te * 0.5), b=tuple(c + te * 0.5), width=1.0,
                         rooms=[rid[ent_room]], hinge=tuple(c - te * 0.5), swing_into=rid[ent_room]))
    net = sum(r.area for r in rooms if r.type != "balcony")
    plan = Plan(unit_id=str(fl["flat_id"]), rooms=rooms, walls=[], doors=doors, windows=[], carpet_area=round(net, 2),
                status="imported")
    all_area = sum(p.area for p in polys.values())
    info = {"flat_id": fl["flat_id"], "bhk": fl["bhk"], "n_rooms": len(rooms), "net": round(net, 1),
            "repaired": repaired, "unresolved": unresolved, "guessed_share": round(guessed / max(all_area, 1), 3), "dropped_share": round(dropped / max(all_area, 1), 3),
            "entry_type": typ[ent_room]}
    return plan, info


def _mrr_wh(p: Polygon) -> tuple[float, float]:
    c = list(p.minimum_rotated_rectangle.exterior.coords)
    a = float(np.hypot(c[1][0] - c[0][0], c[1][1] - c[0][1]))
    b = float(np.hypot(c[2][0] - c[1][0], c[2][1] - c[1][1]))
    return (a, b)


def usable(info: dict[str, Any]) -> bool:
    """Conversion reliable enough to judge the critic on: little guessed or dropped floor."""
    return info["guessed_share"] <= 0.12 and info["dropped_share"] <= 0.08 and info["unresolved"] == 0


# ------------------------------------------------------------------------------------------------ sampling


def stratified_sample(recs: list[dict[str, Any]], n: int = 240, seed: int = 7, per_project: int = 10):
    """Convertible flats, one per (project, signature), drawn per BHK stratum (1, 2, 3, 4+ in proportion 25/40/28/7 %,
    at most ``per_project`` flats from one project) in a seeded random order. Returns [(record, (plan, info))]."""
    rng = random.Random(seed)
    seen, pool = set(), []
    for r in recs:
        sig = (r["project"], r["flat"]["signature"])
        if sig in seen:
            continue
        seen.add(sig)
        c = convert(r)
        if c is None or not usable(c[1]):
            continue
        pool.append((r, c))
    strata: dict[str, list] = defaultdict(list)
    for r, c in pool:
        b = r["flat"]["bhk"]
        strata["4+" if b >= 4 else str(max(b, 1))].append((r, c))
    for v in strata.values():
        rng.shuffle(v)
    taken, per = [], Counter()
    quota = {"1": 0.25, "2": 0.40, "3": 0.28, "4+": 0.07}
    for k in ("2", "1", "3", "4+"):
        want = min(len(strata[k]), max(1, round(n * quota[k])))
        got = 0
        for r, c in strata[k]:
            if got >= want:
                break
            if per[r["project"]] >= per_project:
                continue
            taken.append((r, c))
            per[r["project"]] += 1
            got += 1
    return taken


# ------------------------------------------------------------------------------------------------ scoring


def score_one(item) -> dict[str, Any]:
    """Process-pool worker: critique one converted flat. ``item`` = (project, plan_json, info)."""
    import time

    from .models import Plan as _Plan
    from .quality import critique

    project, plan_json, info = item
    plan = _Plan.model_validate(plan_json)
    t0 = time.perf_counter()
    res = critique(plan, exclude=EXCLUDE, imported=True)
    dt = time.perf_counter() - t0
    return {"flat_id": info["flat_id"], "project": project, "bhk": info["bhk"], "net": info["net"], "entry_type": info["entry_type"],
            "score": res["score"], "grade": res["grade"], "weakest_link": res["weakest_link"], "n_critical": res["n_critical"],
            "metrics": {k: [m["value"], m["satisfaction"]] for k, m in res["metrics"].items()},
            "issues": [(i["code"], i["severity"], i["metric"]) for i in res["issues"] if i["severity"] != "info"],
            "seconds": round(dt, 3), "errors": res["errors"]}


def run(n: int = 240, workers: int = 2, seed: int = 7, everything: bool = False, path: str | Path | None = None) -> dict[str, Any]:
    """Convert, sample and score. ``everything`` scores every usable flat (no de-duplication) instead of a sample."""
    from concurrent.futures import ProcessPoolExecutor

    recs = load_flats(path)
    if everything:
        chosen = []
        for r in recs:
            c = convert(r)
            if c is not None and usable(c[1]):
                chosen.append((r, c))
    else:
        chosen = stratified_sample(recs, n, seed)
    jobs = [(r["project"], c[0].model_dump(mode="json"), c[1]) for r, c in chosen]
    if workers <= 1:
        rows = [score_one(j) for j in jobs]
    else:
        with ProcessPoolExecutor(workers) as ex:
            rows = list(ex.map(score_one, jobs, chunksize=4))
    return {"n_flats_total": len(recs), "n_scored": len(rows), "rows": rows}


def summarise(res: dict[str, Any]) -> dict[str, Any]:
    """Grade distribution, per-BHK scores, per-metric failure rates and per-issue-code rates."""
    from .quality import METRICS

    rows = res["rows"]
    n = len(rows)
    grades = Counter(r["grade"] for r in rows)
    sc = np.array([r["score"] for r in rows])
    by_bhk: dict[str, list[float]] = defaultdict(list)
    for r in rows:
        by_bhk["4+" if r["bhk"] >= 4 else str(r["bhk"])].append(r["score"])
    mfail: dict[str, dict[str, Any]] = {}
    for k in METRICS:
        vals = [(r["metrics"][k][0], r["metrics"][k][1]) for r in rows if k in r["metrics"]]
        if not vals:
            continue
        v = np.array([x[0] for x in vals])
        s = np.array([x[1] for x in vals])
        mfail[k] = {"n": len(vals), "fail_lt_0.5": round(float((s < 0.5).mean()), 3), "not_full": round(float((s < 0.999).mean()), 3),
                    "mean_sat": round(float(s.mean()), 3), "p10": round(float(np.percentile(v, 10)), 3),
                    "p50": round(float(np.percentile(v, 50)), 3), "p90": round(float(np.percentile(v, 90)), 3)}
    codes: dict[str, Counter] = defaultdict(Counter)
    for r in rows:
        for code, sev in {(c, s) for c, s, _m in r["issues"]}:
            codes[code][sev] += 1
    code_rate = {c: {"flats": sum(v.values()), "share": round(sum(v.values()) / n, 3), "major_or_critical": v["major"] + v["critical"]}
                 for c, v in codes.items()}
    gates = Counter(r["weakest_link"] for r in rows if r["weakest_link"])
    return {"n": n, "projects": len({r["project"] for r in rows}), "grades": {g: grades.get(g, 0) for g in "ABCDF"},
            "ab_share": round((grades["A"] + grades["B"]) / n, 3), "mean": round(float(sc.mean()), 1), "median": round(float(np.median(sc)), 1),
            "p10": round(float(np.percentile(sc, 10)), 1), "p25": round(float(np.percentile(sc, 25)), 1),
            "by_bhk": {k: {"n": len(v), "mean": round(float(np.mean(v)), 1)} for k, v in sorted(by_bhk.items())},
            "metric_fail": mfail, "issue_codes": dict(sorted(code_rate.items(), key=lambda kv: -kv[1]["share"])),
            "weakest_link": dict(gates.most_common()), "seconds_mean": round(float(np.mean([r["seconds"] for r in rows])), 3),
            "seconds_max": round(float(max(r["seconds"] for r in rows)), 3), "errors": sum(1 for r in rows if r["errors"])}


def render_examples(rows: list[dict[str, Any]], out_dir: str | Path, k: int = 6, path: str | Path | None = None) -> list[str]:
    """Render approved flats (worst, low, middle, high, best of the sample by score) as the critic sees them."""
    from .quality import critique
    from .render import plan_png

    by_id = {r["flat"]["flat_id"]: r for r in load_flats(path)}
    order = sorted(rows, key=lambda r: r["score"])
    m = len(order)
    pick = [order[0], order[m // 8], order[m // 3], order[m // 2], order[3 * m // 4], order[-1]][:k]
    outs = []
    for r in pick:
        c = convert(by_id[r["flat_id"]])
        if c is None:
            continue
        res = critique(c[0], exclude=EXCLUDE, imported=True)
        name = r["flat_id"].replace(":", "_")
        plan_png(c[0], Path(out_dir) / f"{name}.png", issues=res, title=f"{name} ({r['bhk']} BHK, approved)")
        outs.append(name)
    return outs


if __name__ == "__main__":  # python -m planner_engine.approved_bench [--n 240] [--all] [--json OUT] [--render DIR]
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=240)
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--workers", type=int, default=2)
    ap.add_argument("--json")
    ap.add_argument("--render")
    a = ap.parse_args()
    res = run(a.n, a.workers, everything=a.all)
    s = summarise(res)
    print(json.dumps({k: v for k, v in s.items() if k not in ("metric_fail", "issue_codes")}, indent=1))
    for k, v in s["metric_fail"].items():
        print(f"{k:16s} n={v['n']:4d} fail<0.5 {v['fail_lt_0.5']:.0%}  notfull {v['not_full']:.0%}  mean_sat {v['mean_sat']:.2f}  "
              f"p10/50/90 {v['p10']}/{v['p50']}/{v['p90']}")
    for c, v in list(s["issue_codes"].items())[:40]:
        print(f"{c:22s} {v['share']:.0%} (major+ {v['major_or_critical']})")
    if a.json:
        Path(a.json).write_text(json.dumps({"summary": s, "rows": res["rows"]}, indent=1))
    if a.render:
        print(render_examples(res["rows"], a.render))
