"""Spatial graph dataset v2 from the approved Mumbai plans (one JSON line per plan page).

Stage 1 (extract): every PDF page goes through extract_pdf_topology.process_page (rooms, links, flats,
building outline, core). Raw page rows are appended to a JSONL file with a page marker, so the run is
resumable; at most 2 worker processes (the CPU is shared).
Stage 2 (build): each page's flats become graph records in a frame tied to the flat's entrance and
facade; only flats passing the quality checks are kept. Output: gzip JSONL, one record per page:

  {"type": "page", "pdf", "page", "title", "scale_m_per_pt", "page_h_m", "rotation_deg",
   "outline": [{"polygon", "area", "clipped"}], "cores": [{"id", "polygon", "centroid", "bbox",
   "n_lifts", "n_fire_lifts", "n_stairs", "elements": [{"type", "polygon", "centroid", ...}]}],
   "common_rooms": [...], "flats": [<flat graph>], "flats_relaxed": [<flat graph failing only the
   25 % dimension test, all rooms within 40 %>], "rejected": [{"flat_id", "failed": [...]}]}

Flat graph: nodes (rooms: type, area, centroid, polygon, facade_sides, hop depth, centroid in the
flat frame), edges (door | open | wall), entrance (room, point, core room), frame (origin = entrance
door, x = towards the deepest facade, y = x turned 90 degrees anticlockwise), vectors from the living
centroid to every room, and the flat's relation to the core it opens on.

Coordinates: metres in the rotation-corrected drawing frame, x to the right, y up (drawing "top" is
+y; the sheets carry no reliable north, so compass directions are not claimed).

Usage: python -I build_graph_dataset.py <dataset_root> <out.jsonl.gz> --raw <raw.jsonl> [--workers 2]
       [--limit N] [--skip-extract]
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import math
import os
import re
import sys
from collections import Counter, defaultdict, deque
from pathlib import Path

os.environ.setdefault("OMP_THREAD_LIMIT", "1")
sys.path.insert(0, str(Path(__file__).resolve().parent))

MAX_WORKERS = 2
DIMS_TOL = 0.25  # spec: every room with printed dimensions within 25 % of its printed area
RELAXED_TOL = 0.40  # flats failing only the 25 % test but within 40 % go to "flats_relaxed"
ENTRY_TYPES = {"foyer", "living", "dining", "passage", "unlabeled", "unknown"}
BED = {"master", "bedroom"}
SIDES = ("top", "bottom", "left", "right")
SIDE_VEC = {"top": (0.0, 1.0), "bottom": (0.0, -1.0), "left": (-1.0, 0.0), "right": (1.0, 0.0)}


# --------------------------------------------------------------------------------------------------
# stage 1: extraction driver
# --------------------------------------------------------------------------------------------------


def _work(job):
    import extract_pdf_topology as T

    return T.process_page(job)


def extract(root: Path, raw: Path, workers: int, limit: int | None) -> None:
    from concurrent.futures import ProcessPoolExecutor

    import pymupdf

    jobs = []
    for pdf in sorted(root.glob("*/*.pdf")):
        n = pymupdf.open(str(pdf)).page_count
        jobs += [(str(pdf), i) for i in range(n)]
    done = set()
    if raw.exists():
        with open(raw) as fh:
            for line in fh:
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue  # a line cut by an interrupted run; the page has no marker and is redone
                if r.get("type") == "_page_done":
                    done.add((r["pdf"], r["page"]))
    todo = [j for j in jobs if (Path(j[0]).parent.name, j[1]) not in done]
    if limit:
        todo = todo[:limit]
    print(f"{len(jobs)} pages, {len(done)} done, {len(todo)} to do, {workers} workers", file=sys.stderr, flush=True)
    with ProcessPoolExecutor(workers) as ex, open(raw, "a") as fh:
        for k, rows in enumerate(ex.map(_work, todo, chunksize=1)):
            for r in rows:  # flats first, the page marker last: a page is done once its marker is written
                fh.write(json.dumps(r) + "\n")
            fh.flush()
            if k % 10 == 0:
                print(f"{k}/{len(todo)}", file=sys.stderr, flush=True)


# --------------------------------------------------------------------------------------------------
# stage 2: graphs
# --------------------------------------------------------------------------------------------------


def _ang(dx, dy):
    return round(math.degrees(math.atan2(dy, dx)), 1)


def facade_points(room):
    """One point per facade side of a room: the middle of the room's extreme edge on that side."""
    p = room.get("poly") or []
    if not p:
        return []
    xs = [q[0] for q in p]
    ys = [q[1] for q in p]
    cx, cy = room["cxm"], room["cym"]
    out = []
    for side in room.get("facade_sides") or []:
        ln = (room.get("facade") or {}).get(side, 0.0)
        pt = {"top": (cx, max(ys)), "bottom": (cx, min(ys)), "left": (min(xs), cy), "right": (max(xs), cy)}[side]
        out.append((pt, ln, side))
    return out


OPEN_PLAN = {"living", "dining", "foyer", "passage", "alcove"}


def dims_deviation(f):
    """Largest deviation of a room's region area from its printed area (L x W), and the same with open-plan
    zones compared as a whole. An open-plan zone = living / dining / foyer / passage / alcove rooms joined by
    open links: no wall is drawn between them, so where the living region stops and the dining or passage
    region starts is arbitrary, while the zone's total area is measurable. Members without printed
    dimensions count with their own area on both sides. Returns (strict, zoned, per-room zoned deviation)."""
    rooms = {r["id"]: r for r in f["rooms"]}
    par = {i: i for i in rooms}

    def find(i):
        while par[i] != i:
            par[i] = par[par[i]]
            i = par[i]
        return i

    for ln in f.get("links", []):
        a, b = ln["a"], ln["b"]
        if ln["kind"] == "open" and a in rooms and b in rooms and \
                rooms[a]["type"] in OPEN_PLAN and rooms[b]["type"] in OPEN_PLAN:
            par[find(a)] = find(b)
    zones = defaultdict(list)
    for i in rooms:
        zones[find(i)].append(rooms[i])
    strict, zoned, per = 0.0, 0.0, {}
    for zs in zones.values():
        printed = [r for r in zs if r.get("area_ratio") is not None]
        for r in printed:
            strict = max(strict, abs(r["area_ratio"] - 1))
        if not printed:
            continue
        if len(zs) == 1:
            d = abs(zs[0]["area_ratio"] - 1)
        else:
            area = sum(r["area"] for r in zs)
            ref = sum(r["area"] / r["area_ratio"] if r.get("area_ratio") is not None else r["area"] for r in zs)
            d = abs(area / ref - 1)
        for r in zs:
            per[r["id"]] = round(d, 3)
        zoned = max(zoned, d)
    return strict, zoned, per


def qc_flat(f):
    """Quality checks of the dataset spec. Returns the list of failed checks (empty = kept)."""
    failed = []
    types = Counter(r["type"] for r in f["rooms"])
    ent = f.get("entrance")
    n_ent = f.get("n_entrance_rooms") or 0
    if ent and n_ent == 0 and any(r["id"] == ent["room"] and r.get("anchor_src") == "multipurpose" for r in f["rooms"]):
        n_ent = 1  # PAP tenement entered through its multipurpose (living) room
    if not ent or n_ent != 1 or ent.get("x") is None:
        failed.append("one_entrance")
    # PAP / rehab tenements: the multipurpose room is the living room and the alcove the kitchen
    pap = any(r.get("anchor_src") == "multipurpose" for r in f["rooms"])
    cook_alcove = any(r["type"] == "alcove" and (pap or re.search(r"K\.?\s?P\b|KITCH|COOK", r.get("name") or "", re.I))
                      for r in f["rooms"])
    has = {"living": types["living"] or pap, "kitchen": types["kitchen"] or cook_alcove, "toilet": types["toilet"]}
    for t in ("living", "kitchen", "toilet"):
        if not has[t]:
            failed.append(f"has_{t}")
    _strict, dev, _per = dims_deviation(f)
    if dev > DIMS_TOL:
        failed.append("dims_within_25pct" if dev <= RELAXED_TOL else "dims_within_40pct")
    area = sum(r["area"] for r in f["rooms"])
    if types["kitchen"] > 1 or types["living"] > 2 or area > 350 or len(f["rooms"]) > 30 \
            or (f.get("n_entrance_rooms") or 0) > 2:
        failed.append("no_merge")
    if any(not r.get("poly") for r in f["rooms"]):
        failed.append("polygons")
    return failed


def flat_graph(f, cores, common_by_id):
    """Graph record of one extracted flat (assumes qc_flat passed)."""
    rooms = {r["id"]: r for r in f["rooms"]}
    ent = f["entrance"]
    ox, oy = ent["x"], ent["y"]
    dstrict, dzoned, dper = dims_deviation(f)
    # ---- frame: x towards the deepest facade point (largest distance from the entrance door)
    fps = [(pt, ln, side, r["id"]) for r in f["rooms"] for pt, ln, side in facade_points(r)]
    if fps:
        (fx, fy), _ln, deep_side, deep_room = max(fps, key=lambda q: math.hypot(q[0][0] - ox, q[0][1] - oy))
    else:  # no facade found: deepest room centroid
        far = max(f["rooms"], key=lambda r: math.hypot(r["cxm"] - ox, r["cym"] - oy))
        fx, fy, deep_side, deep_room = far["cxm"], far["cym"], None, far["id"]
    raw = math.degrees(math.atan2(fy - oy, fx - ox))
    # plans are orthogonal, so the frame axes follow the walls: x is the wall direction along which the
    # facade reaches deepest from the entrance door (stable where the raw direction is near 45 degrees)
    pts = [q[0] for q in fps] or [(fx, fy)]
    snapped = max((0, 90, 180, 270), key=lambda a: max((px - ox) * math.cos(math.radians(a)) +
                                                       (py - oy) * math.sin(math.radians(a)) for px, py in pts))
    th = math.radians(snapped)
    ux, uy = math.cos(th), math.sin(th)
    vx, vy = -uy, ux

    def to_frame(x, y):
        dx, dy = x - ox, y - oy
        return round(dx * ux + dy * uy, 2), round(dx * vx + dy * vy, 2)

    # ---- nodes
    balcony_sides = defaultdict(set)
    links = [ln for ln in f.get("links", []) if ln["a"] in rooms and ln["b"] in rooms]
    for ln in links:
        a, b = rooms[ln["a"]], rooms[ln["b"]]
        for x, y in ((a, b), (b, a)):
            if y["type"] in ("balcony",) and x["type"] != "balcony":
                balcony_sides[x["id"]].update(y.get("facade_sides") or [])
    nodes = []
    for r in f["rooms"]:
        cf = to_frame(r["cxm"], r["cym"])
        nodes.append({
            "id": r["id"], "type": r["type"], "name": r.get("name"), "area": r["area"],
            "centroid": [r["cxm"], r["cym"]], "centroid_frame": list(cf), "polygon": r["poly"],
            "polygon_frame": [list(to_frame(x, y)) for x, y in r["poly"]],
            "facade_sides": r.get("facade_sides") or [], "facade_len": round(sum((r.get("facade") or {}).values()), 2),
            "facade_via_balcony": sorted(balcony_sides.get(r["id"], set()) - set(r.get("facade_sides") or [])),
            "ocr_dims": r.get("ocr_dims"), "area_ratio": r.get("area_ratio"), "dims_dev_zoned": dper.get(r["id"]),
            "clear_width": r.get("wclear"),
            "hops": r.get("hops"), "shaft": bool(r.get("shaft")), "anchor_src": r.get("anchor_src"),
        })
    # ---- edges
    edges = [{"kind": ln["kind"], "a": ln["a"], "b": ln["b"],
              "point": [ln["x"], ln["y"]] if ln.get("x") is not None else None} for ln in links]
    linked = {(min(e["a"], e["b"]), max(e["a"], e["b"])) for e in edges}
    for w in f.get("walls", []):
        if w["a"] in rooms and w["b"] in rooms and (min(w["a"], w["b"]), max(w["a"], w["b"])) not in linked:
            edges.append({"kind": "wall", "a": w["a"], "b": w["b"], "len": w["len"], "thick": w["thick"]})
    # ---- hop depth from the entrance over door / open edges (recomputed on the kept rooms)
    nbr = defaultdict(set)
    for e in edges:
        if e["kind"] in ("door", "open"):
            nbr[e["a"]].add(e["b"])
            nbr[e["b"]].add(e["a"])
    hops = {ent["room"]: 0}
    q = deque([ent["room"]])
    while q:
        u = q.popleft()
        for v in nbr[u]:
            if v not in hops:
                hops[v] = hops[u] + 1
                q.append(v)
    for n in nodes:
        n["hops"] = hops.get(n["id"])
    # ---- vectors from the living centroid
    liv = max((r for r in f["rooms"] if r["type"] == "living"), key=lambda r: r["area"], default=None) or \
        max((r for r in f["rooms"] if r.get("anchor_src") == "multipurpose"), key=lambda r: r["area"])
    lx, ly = to_frame(liv["cxm"], liv["cym"])
    living_vectors = []
    for n in nodes:
        if n["id"] == liv["id"]:
            continue
        dx, dy = n["centroid_frame"][0] - lx, n["centroid_frame"][1] - ly
        living_vectors.append({"room": n["id"], "type": n["type"], "angle_deg": _ang(dx, dy),
                               "dist_m": round(math.hypot(dx, dy), 2)})
    # ---- flat centroid (area-weighted room centroids)
    tot = sum(r["area"] for r in f["rooms"])
    fcx = sum(r["cxm"] * r["area"] for r in f["rooms"]) / tot
    fcy = sum(r["cym"] * r["area"] for r in f["rooms"]) / tot
    # ---- relation to the core
    core_rel = None
    core = None
    if ent.get("core_id") is not None:
        core = next((c for c in cores if c["id"] == ent["core_id"]), None)
    if core is None and cores:
        core = min(cores, key=lambda c: math.hypot(c["centroid"][0] - ox, c["centroid"][1] - oy))
    if core is not None:
        ccx, ccy = core["centroid"]
        x0, y0, x1, y1 = core["bbox"]
        hw, hh = max(0.5, (x1 - x0) / 2), max(0.5, (y1 - y0) / 2)
        nx, ny = (ox - ccx) / hw, (oy - ccy) / hh
        side = ("right" if nx > 0 else "left") if abs(nx) >= abs(ny) else ("top" if ny > 0 else "bottom")
        cfx, cfy = to_frame(ccx, ccy)
        core_rel = {
            "core_id": core["id"], "via_core_room": ent.get("core_id") is not None,
            "core_centroid": [ccx, ccy],
            "to_entrance": {"dx": round(ox - ccx, 2), "dy": round(oy - ccy, 2),
                            "dist_m": round(math.hypot(ox - ccx, oy - ccy), 2), "angle_deg": _ang(ox - ccx, oy - ccy)},
            "to_flat_centroid": {"dx": round(fcx - ccx, 2), "dy": round(fcy - ccy, 2),
                                 "dist_m": round(math.hypot(fcx - ccx, fcy - ccy), 2),
                                 "angle_deg": _ang(fcx - ccx, fcy - ccy)},
            "core_side": side,
            "core_in_flat_frame": {"x": cfx, "y": cfy, "angle_deg": _ang(cfx, cfy)},
        }
    beds = [n for n in nodes if n["type"] in BED]
    bed_y = sum(n["centroid_frame"][1] for n in beds) / len(beds) if beds else 0.0
    sig = "|".join(sorted(f"{n['type']}:{round(n['area'])}" for n in nodes))
    xs = [p[0] for n in nodes for p in n["polygon_frame"]]
    ys = [p[1] for n in nodes for p in n["polygon_frame"]]
    return {
        "flat_id": f["flat_id"], "bhk": f["bhk"],
        "kind": "pap_tenement" if any(r.get("anchor_src") == "multipurpose" for r in f["rooms"]) else "apartment", "n_rooms": len(nodes), "area_m2": round(tot, 2),
        "centroid": [round(fcx, 2), round(fcy, 2)],
        "entrance": {"room": ent["room"], "room_type": rooms[ent["room"]]["type"], "point": [ox, oy],
                     "kind": ent.get("kind"), "via": ent.get("via"), "common_room": ent.get("common"),
                     "common_type": ent.get("common_type"),
                     "common_polygon": (common_by_id.get(ent.get("common")) or {}).get("poly")},
        "frame": {"origin": [ox, oy], "x_axis": [round(ux, 4), round(uy, 4)], "y_axis": [round(vx, 4), round(vy, 4)],
                  "angle_deg": snapped, "raw_angle_deg": round(raw, 1), "deep_room": deep_room,
                  "deep_side": deep_side, "depth_m": round(max(xs), 2) if xs else None,
                  "extent": [round(min(xs), 2), round(min(ys), 2), round(max(xs), 2), round(max(ys), 2)] if xs else None,
                  "bedrooms_side": "left" if bed_y > 0.3 else "right" if bed_y < -0.3 else "centre"},
        "nodes": nodes, "edges": edges, "living": liv["id"], "living_vectors": living_vectors,
        "core": core_rel, "attached_rooms": f.get("attached", []), "pruned_cells": f.get("pruned", []),
        "signature": hashlib.sha1(sig.encode()).hexdigest()[:12],
        "qc": {"dims_dev_max_room": round(dstrict, 3), "dims_dev_max_zoned": round(dzoned, 3),
               "strict_dims_ok": dstrict <= DIMS_TOL, "n_rooms_with_dims": sum(1 for r in f["rooms"]
                                                                              if r.get("area_ratio") is not None)},
    }


def build(raw: Path, out: Path) -> dict:
    pages, flats = {}, defaultdict(list)
    with open(raw) as fh:
        for line in fh:
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if r.get("type") == "_page_done":
                pages[(r["pdf"], r["page"])] = r
            else:
                flats[(r["pdf"], r["page"])].append(r)
    stats = Counter()
    fail = Counter()
    bhk = Counter()
    bhk_rel = Counter()
    sigs = set()
    proj_kept = set()
    with gzip.open(out, "wt") as fh:
        for key in sorted(pages):
            m = pages[key]
            stats["pages"] += 1
            stats[f"status_{m.get('status')}"] += 1
            if m.get("status") != "ok":
                continue
            fl = flats.get(key, [])
            stats["flats_raw"] += len(fl)
            stats["pages_with_outline"] += bool(m.get("outline"))
            stats["pages_with_core"] += bool(m.get("cores"))
            stats["pages_core_with_lift"] += any(c["n_lifts"] + c["n_fire_lifts"] for c in m.get("cores", []))
            stats["pages_core_with_stair"] += any(c["n_stairs"] for c in m.get("cores", []))
            common_by_id = {c["id"]: c for c in m.get("common_rooms", [])}
            kept, rej, relaxed = [], [], []
            for f in fl:
                failed = qc_flat(f)
                for x in failed:
                    fail[x] += 1
                if failed == ["dims_within_25pct"]:
                    g = flat_graph(f, m.get("cores", []), common_by_id)
                    g["qc_failed"] = failed
                    relaxed.append(g)
                    bhk_rel[g["bhk"]] += 1
                    continue
                if failed:
                    rej.append({"flat_id": f["flat_id"], "bhk": f["bhk"], "n_rooms": len(f["rooms"]), "failed": failed})
                    continue
                g = flat_graph(f, m.get("cores", []), common_by_id)
                kept.append(g)
                bhk[g["bhk"]] += 1
                sigs.add((key[0], g["signature"]))
                proj_kept.add(key[0])
            stats["flats_kept"] += len(kept)
            stats["flats_relaxed"] += len(relaxed)
            stats["pages_with_kept_flat"] += bool(kept)
            rec = {"type": "page", "pdf": key[0], "page": key[1], "title": m.get("title"),
                   "scale_m_per_pt": m.get("scale_m_per_pt"), "page_h_m": m.get("page_h_m"),
                   "rotation_deg": m.get("rotation_deg"), "outline": m.get("outline", []), "cores": m.get("cores", []),
                   "common_rooms": m.get("common_rooms", []), "flats": kept, "flats_relaxed": relaxed,
                   "rejected": rej}
            fh.write(json.dumps(rec, separators=(",", ":")) + "\n")
    stats["flats_kept_unique_per_project"] = len(sigs)
    stats["projects_with_kept_flat"] = len(proj_kept)
    return {"stats": dict(stats), "kept_by_bhk": dict(sorted(bhk.items())),
            "relaxed_by_bhk": dict(sorted(bhk_rel.items())), "qc_failures": dict(fail)}


# --------------------------------------------------------------------------------------------------
# stage 3: compact per-BHK pattern summary for the generator
# --------------------------------------------------------------------------------------------------

MIN_PROJECTS = 5  # a figure resting on fewer projects is dropped
HUBS = ("living", "dining", "foyer", "passage")
ANGLE_BINS = (0, 45, 90, 135, 180, -135, -90, -45)  # bin centres, degrees in the flat frame
ANGLE_NAMES = {0: "E_deep", 45: "NE", 90: "N", 135: "NW", 180: "W_entrance", -135: "SW", -90: "S", -45: "SE"}


def _pct(v, q):
    v = sorted(v)
    if not v:
        return None
    k = (len(v) - 1) * q
    lo = int(math.floor(k))
    hi = min(lo + 1, len(v) - 1)
    return round(v[lo] + (v[hi] - v[lo]) * (k - lo), 2)


def _pcts(v, qs=(0.1, 0.25, 0.5, 0.75, 0.9)):
    return {f"p{int(q * 100)}": _pct(v, q) for q in qs}


def _abin(a):
    return min(ANGLE_BINS, key=lambda c: abs((a - c + 180) % 360 - 180))


def _fig(items, payload):
    """items = [(project, flat_id)] the figure rests on; None when it rests on < MIN_PROJECTS projects."""
    proj = {p for p, _ in items}
    if len(proj) < MIN_PROJECTS:
        return None
    return {"n": len(items), "n_projects": len(proj), **payload}


def _hist(counter, keys=None):
    tot = sum(counter.values())
    keys = keys if keys is not None else sorted(counter, key=lambda k: -counter[k])
    return {str(k): round(counter[k] / tot, 3) for k in keys if counter.get(k)} if tot else {}


def patterns(graphs: Path, out: Path) -> dict:
    """Per-BHK distributions from the kept flats of graphs_v2.jsonl.gz (apartments, one per project signature)."""
    uniq, raw_n, pap_n = {}, 0, 0
    with gzip.open(graphs, "rt") as fh:
        for line in fh:
            pg = json.loads(line)
            for f in pg["flats"]:
                raw_n += 1
                if f.get("kind", "apartment") != "apartment":
                    pap_n += 1
                    continue
                uniq.setdefault((pg["pdf"], f["signature"]), (pg["pdf"], f))
    flats = list(uniq.values())
    groups = defaultdict(list)
    for pdf, f in flats:
        b = f["bhk"]
        groups[str(b) if b in (1, 2, 3) else "4+" if b >= 4 else None].append((pdf, f))
        groups["all"].append((pdf, f))
    groups.pop(None, None)

    def summarise(fl):
        ids = [(p, f["flat_id"]) for p, f in fl]
        res = {"n": len(fl), "n_projects": len({p for p, _ in fl})}
        # direction + distance of each room type from the living centroid, in the flat frame
        direction = {}
        for label, types in (("kitchen", {"kitchen"}), ("master", {"master"}), ("bedroom", {"bedroom"}),
                             ("any_bedroom", {"master", "bedroom"}), ("toilet", {"toilet"}), ("dining", {"dining"})):
            ang, dist, items, fl_ids = Counter(), [], [], set()
            for p, f in fl:
                for v in f["living_vectors"]:
                    if v["type"] in types:
                        ang[_abin(v["angle_deg"])] += 1
                        dist.append(v["dist_m"])
                        items.append((p, f["flat_id"]))
            g = _fig(items, {"n_flats": len({i[1] for i in items}),
                             "angle_hist_8": {ANGLE_NAMES[k]: round(ang[k] / sum(ang.values()), 3) for k in ANGLE_BINS if ang[k]},
                             "dist_m": _pcts(dist)}) if items else None
            if g:
                direction[label] = g
        res["direction_from_living"] = direction
        # hop depth from the entrance, per type
        hop = {}
        for t in ("living", "dining", "foyer", "passage", "kitchen", "master", "bedroom", "toilet"):
            vals, items = [], []
            for p, f in fl:
                for n in f["nodes"]:
                    if n["type"] == t and n["hops"] is not None:
                        vals.append(n["hops"])
                        items.append((p, f["flat_id"]))
            g = _fig(items, {"hist": _hist(Counter(vals), sorted(set(vals))), "median": _pct(vals, 0.5),
                             "mean": round(sum(vals) / len(vals), 2)}) if items else None
            if g:
                hop[t] = g
        res["hops_from_entrance"] = hop
        # entrance
        ec = Counter("living" if f["entrance"]["room_type"] == "living" else
                     "foyer" if f["entrance"]["room_type"] == "foyer" else
                     "dining" if f["entrance"]["room_type"] == "dining" else
                     "passage" if f["entrance"]["room_type"] == "passage" else "other"
                     for _, f in fl)
        res["entrance_room"] = _fig(ids, {"share": _hist(ec)})
        # which room the bedroom doors open onto (hub), per bedroom and per flat
        hub_bed, hub_flat, hb_items, hf_items = Counter(), Counter(), [], []
        for p, f in fl:
            per_flat = _bed_hubs(f)
            for h in per_flat:
                hub_bed[h] += 1
                hb_items.append((p, f["flat_id"]))
            if per_flat:
                hub_flat["+".join(sorted(per_flat))] += 1
                hf_items.append((p, f["flat_id"]))
        hubs_pat = {}
        if hb_items:
            hubs_pat["bedroom_opens_onto"] = _fig(hb_items, {"share": _hist(hub_bed)})
            top = []
            for pat, c in hub_flat.most_common(10):
                its = [(p, f["flat_id"]) for p, f in fl if "+".join(sorted(_bed_hubs(f))) == pat]
                g = _fig(its, {"pattern": pat, "share": round(c / len(hf_items), 3)})
                if g:
                    top.append(g)
            hubs_pat["flat_patterns"] = top
        res["hub_patterns"] = {k: v for k, v in hubs_pat.items() if v}
        # passage share of the flat area
        shares_all, shares_with, it_all, it_with = [], [], [], []
        for p, f in fl:
            ps = sum(n["area"] for n in f["nodes"] if n["type"] == "passage") / f["area_m2"]
            shares_all.append(ps)
            it_all.append((p, f["flat_id"]))
            if ps > 0:
                shares_with.append(ps)
                it_with.append((p, f["flat_id"]))
        pas = {}
        g = _fig(it_all, {"share_of_flats_with_passage": round(len(shares_with) / len(shares_all), 3),
                          "passage_area_share": _pcts(shares_all)})
        if g:
            pas["all_flats"] = g
        g = _fig(it_with, {"passage_area_share": _pcts(shares_with)}) if shares_with else None
        if g:
            pas["flats_with_passage"] = g
        res["passage"] = pas
        res["area_m2"] = _fig(ids, {"percentiles": _pcts([f["area_m2"] for _, f in fl])})
        res["n_rooms"] = _fig(ids, {"percentiles": _pcts([f["n_rooms"] for _, f in fl])})
        return {k: v for k, v in res.items() if v is not None}

    out_d = {
        "_meta": {
            "source": "data/mined/graphs_v2.jsonl.gz (kept flats, apartments only, one flat per project and signature)",
            "built_by": "engine/tools/build_graph_dataset.py --patterns-only",
            "min_projects_per_figure": MIN_PROJECTS,
            "frame": "origin = flat entrance door, +x = wall direction towards the flat's deepest facade, +y = x turned "
                     "90 deg anticlockwise; angles in degrees (0 = towards the deep facade, 180 = back towards the entrance)",
            "angle_bins": "8 sectors of 45 deg, centre angle per name: " + ", ".join(f"{ANGLE_NAMES[k]}={k}" for k in ANGLE_BINS) + "; names are frame-relative (x = deep facade), not compass",
            "kept_flats_total": raw_n, "pap_tenements_excluded": pap_n, "unique_apartments_used": len(flats),
            "n": "figure basis: rooms for direction/hops, flats for the rest (n_flats where both apply)",
            "hub_types": "living / dining / foyer / passage / unnamed (unlabelled cell, mostly a passage whose label was lost) / none (no link to a hub found; link not detected or room reached through another room)",
            "bhk_groups": "bedroom + master count; '4+' = 4 or more; 'all' = every group",
        },
        "bhk": {k: summarise(v) for k, v in sorted(groups.items())},
    }
    out_d["bhk"] = {k: v for k, v in out_d["bhk"].items() if v["n_projects"] >= MIN_PROJECTS}
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(out_d, indent=1) + "\n")
    return out_d


def _bed_hubs(f):
    """Per bedroom / master: the hub room its door or opening leads to (the one nearest the entrance). 'unnamed' =
    an unlabelled cell (in practice a passage whose label was lost), 'none' = no door / opening to any hub found
    (the bedroom's link was not detected, or it is reached only through another room)."""
    ty = {n["id"]: n for n in f["nodes"]}
    res = []
    for n in f["nodes"]:
        if n["type"] not in BED:
            continue
        best = None
        for e in f["edges"]:
            if e["kind"] in ("door", "open") and n["id"] in (e["a"], e["b"]):
                o = e["b"] if e["a"] == n["id"] else e["a"]
                t = ty[o]["type"]
                if t in HUBS or t in ("unknown", "unlabeled"):
                    h = ty[o]["hops"] if ty[o]["hops"] is not None else 99
                    if best is None or h < best[0]:
                        best = (h, t if t in HUBS else "unnamed")
        res.append(best[1] if best else "none")
    return res


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("root", type=Path)
    ap.add_argument("out", type=Path)
    ap.add_argument("--raw", type=Path, required=True, help="raw extractor rows (JSONL, appended, resumable)")
    ap.add_argument("--workers", type=int, default=MAX_WORKERS)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--skip-extract", action="store_true")
    ap.add_argument("--patterns-out", type=Path, default=None, help="also write the per-BHK pattern summary JSON")
    ap.add_argument("--patterns-only", action="store_true", help="only (re)compute the pattern summary from <out>")
    a = ap.parse_args()
    if a.patterns_only:
        patterns(a.out, a.patterns_out)
        return
    if not a.skip_extract:
        extract(a.root, a.raw, max(1, min(a.workers, MAX_WORKERS)), a.limit)
    rep = build(a.raw, a.out)
    print(json.dumps(rep, indent=1))
    if a.patterns_out:
        patterns(a.out, a.patterns_out)


if __name__ == "__main__":
    main()
