"""Generator v4: case-based layouts from approved Mumbai flats (docs/quality/generator-v4.md).

``data/casebase_v1.json.gz`` (built by ``engine/tools/build_casebase.py``) holds the clean approved flats of the graph dataset
(``data/mined/graphs_v2.jsonl.gz``) that the critic scores well, each in a canonical *entrance frame*:

* ``v`` (depth) runs from the flat's entrance side (the bounding-box side the front door faces out of) to the opposite side,
* ``u`` (along) runs along the entrance side, to the right of a person standing in the door and looking in,
* both normalised to [0, 1] on the flat's bounding box, so a case stretches onto any flat of similar proportions.

``retrieve`` ranks cases for a flat (same BHK, carpet within +-20 %, bounding-box aspect, door position along the entrance
side, façade coverage of the four sides; mirrored cases are tried too) and is deterministic. ``adapt`` maps one case into a
flat's solver grid, matches its rooms to the programme rooms (Hungarian assignment on type and size, attached toilets
following their bedroom) and returns a *guide* for ``solver.solve``: soft target rectangles, soft relative-order pairs
(which room lies left/right/in front of/behind which) and solution hints. The solver keeps the hard rules (geometry,
minimum sizes, DCPR, windows); the guide only steers the arrangement towards the approved one.
"""

from __future__ import annotations

import gzip
import json
import math
from functools import lru_cache
from importlib import resources
from typing import Any

from shapely.geometry import Point, Polygon

CASEBASE = "data/casebase_v1.json.gz"
SIDES = ("front", "back", "left", "right")
AREA_TOL = 0.20  # carpet within +-20 % of the flat's expected carpet
NET_SHARE = 0.92  # room floor / net interior (= diagram.NET_SHARE)
ORDER_MIN = 1.0  # m: rooms whose centres are closer than this along an axis get no order pair on that axis
MAJOR = {"living", "dining", "kitchen", "master", "bedroom", "study", "toilet", "bath", "wc", "foyer", "passage", "utility"}
WET = {"toilet", "bath", "wc"}
BEDS = {"master", "bedroom"}


# ------------------------------------------------------------------------------------------------ frames


def _axis(vx: float, vy: float) -> tuple[int, int]:
    """Snap a direction to the nearest axis."""
    return (int(math.copysign(1, vx)), 0) if abs(vx) >= abs(vy) else (0, int(math.copysign(1, vy)))


def _right(d: tuple[int, int]) -> tuple[int, int]:
    return (d[1], -d[0])


def side_of(direction: tuple[int, int], d_in: tuple[int, int]) -> str:
    """Canonical side ('front' = the entrance side) an outward direction points to."""
    if direction == (-d_in[0], -d_in[1]):
        return "front"
    if direction == d_in:
        return "back"
    return "right" if direction == _right(d_in) else "left"


def entrance_inward(room: Polygon, p: tuple[float, float]) -> tuple[int, int]:
    """Axis direction pointing into the flat at the front door ``p`` on the boundary of the entrance room."""
    pts = list(room.exterior.coords)
    best, bn = 1e18, (0.0, 1.0)
    for a, b in zip(pts[:-1], pts[1:], strict=False):
        ab = (b[0] - a[0], b[1] - a[1])
        L = math.hypot(*ab)
        if L < 1e-6:
            continue
        t = max(0.0, min(1.0, ((p[0] - a[0]) * ab[0] + (p[1] - a[1]) * ab[1]) / (L * L)))
        d = math.hypot(a[0] + t * ab[0] - p[0], a[1] + t * ab[1] - p[1])
        if d < best:
            best, bn = d, (ab[1] / L, -ab[0] / L)
    n = _axis(*bn)
    q = Point(p[0] + 0.15 * n[0], p[1] + 0.15 * n[1])
    if room.buffer(0.02).contains(q):
        return n
    return (-n[0], -n[1])


class Frame:
    """Canonical entrance frame on an axis-aligned bounding box (x0, y0, x1, y1) with inward direction ``d_in``."""

    def __init__(self, bbox: tuple[float, float, float, float], d_in: tuple[int, int]):
        self.bbox, self.d_in, self.r = bbox, d_in, _right(d_in)
        x0, y0, x1, y1 = bbox
        cs = [(x0, y0), (x1, y0), (x0, y1), (x1, y1)]
        dep = [c[0] * d_in[0] + c[1] * d_in[1] for c in cs]
        alo = [c[0] * self.r[0] + c[1] * self.r[1] for c in cs]
        self.d0, self.a0 = min(dep), min(alo)
        self.D, self.W = max(dep) - self.d0, max(alo) - self.a0

    def uv(self, x: float, y: float) -> tuple[float, float]:
        u = (x * self.r[0] + y * self.r[1] - self.a0) / max(self.W, 1e-9)
        v = (x * self.d_in[0] + y * self.d_in[1] - self.d0) / max(self.D, 1e-9)
        return u, v

    def xy(self, u: float, v: float) -> tuple[float, float]:
        a, d = self.a0 + u * self.W, self.d0 + v * self.D
        # (x, y) = a * r + d * d_in (r and d_in are orthonormal axis vectors)
        return (a * self.r[0] + d * self.d_in[0], a * self.r[1] + d * self.d_in[1])


# ------------------------------------------------------------------------------------------------ building cases


def case_from_plan(plan, record: dict[str, Any], score: float, grade: str, project: str) -> dict[str, Any] | None:
    """Converted approved flat (``approved_bench.convert``) -> compact case, or None when it has no usable frame."""
    rooms = [r for r in plan.rooms if len(r.polygon) >= 3]
    polys = {r.id: Polygon(r.polygon).buffer(0) for r in rooms}
    entry = next((d for d in plan.doors if d.kind == "entry"), None)
    if entry is None or not entry.rooms or entry.rooms[0] not in polys:
        return None
    if not any(r.type == "living" for r in rooms) or not any(r.type == "kitchen" for r in rooms):
        return None
    ex = ((entry.a[0] + entry.b[0]) / 2, (entry.a[1] + entry.b[1]) / 2)
    d_in = entrance_inward(polys[entry.rooms[0]], ex)
    xs = [c[0] for p in polys.values() for c in p.exterior.coords]
    ys = [c[1] for p in polys.values() for c in p.exterior.coords]
    fr = Frame((min(xs), min(ys), max(xs), max(ys)), d_in)
    if fr.W < 3.0 or fr.D < 3.0:
        return None
    nodes = {n["id"]: n for n in record["nodes"]}
    idx = {r.id: i for i, r in enumerate(rooms)}
    cover: dict[str, list[tuple[float, float]]] = {s: [] for s in SIDES}
    out_rooms = []
    for r in rooms:
        p = polys[r.id]
        uv = [fr.uv(*c) for c in p.exterior.coords[:-1]]
        us, vs = [c[0] for c in uv], [c[1] for c in uv]
        sides = set()
        nid = int(r.id[1:].split("_", 1)[0])
        n = nodes.get(nid, {})
        if (n.get("facade_len") or 0) >= 0.7:
            for s in n.get("facade_sides", []):
                dvec = {"top": (0, 1), "bottom": (0, -1), "left": (-1, 0), "right": (1, 0)}.get(s)
                if dvec:
                    cs = side_of(dvec, d_in)
                    sides.add(cs)
                    cover[cs].append((min(us), max(us)) if cs in ("front", "back") else (min(vs), max(vs)))
        out_rooms.append({"t": r.type, "a": round(p.area, 2),
                          "r": [round(min(us), 3), round(min(vs), 3), round(max(us), 3), round(max(vs), 3)],
                          "p": [round(c, 3) for xy in uv for c in xy], "f": sorted(sides)})
    edges = []
    for d in plan.doors:
        if d.kind == "door" and len(d.rooms) == 2 and d.rooms[0] in idx and d.rooms[1] in idx:
            edges.append(sorted((idx[d.rooms[0]], idx[d.rooms[1]])))
    edges = sorted({tuple(e) for e in edges})
    fac = {}
    for s in SIDES:
        iv = sorted(cover[s])
        tot, cur = 0.0, None
        for a, b in iv:
            if cur is None or a > cur[1]:
                if cur:
                    tot += cur[1] - cur[0]
                cur = [a, b]
            else:
                cur[1] = max(cur[1], b)
        if cur:
            tot += cur[1] - cur[0]
        fac[s] = round(min(1.0, tot), 2)
    du, dv = fr.uv(*ex)
    carpet = sum(r.area for r in rooms if r.type != "balcony")
    return {"id": str(record["flat_id"]), "project": project, "bhk": sum(1 for r in rooms if r.type in BEDS),
            "carpet": round(carpet, 1), "W": round(fr.W, 2), "D": round(fr.D, 2), "door": [round(du, 3), round(dv, 3)],
            "facade": [fac[s] for s in SIDES], "entry": next(r.type for r in rooms if r.id == entry.rooms[0]),
            "score": score, "grade": grade, "rooms": out_rooms, "edges": [list(e) for e in edges]}


# ------------------------------------------------------------------------------------------------ library


@lru_cache(maxsize=1)
def load() -> list[dict[str, Any]]:
    """The packaged case library ([] when it is missing, so the generator falls back to v3)."""
    try:
        raw = resources.files("planner_engine").joinpath(CASEBASE).read_bytes()
    except (FileNotFoundError, OSError):
        return []
    return json.loads(gzip.decompress(raw))["cases"]


def by_id(case_id: str) -> dict[str, Any] | None:
    return next((c for c in load() if c["id"] == case_id), None)


def unit_frame(ug) -> dict[str, Any] | None:
    """Entrance frame of a solver grid (``solver.UnitGrid``): size, door position, façade coverage per side."""
    if ug is None or ug.door is None or ug.interior.is_empty:
        return None
    e, along = ug.door
    d_in = (0, -e.outward) if e.horizontal else (-e.outward, 0)
    fr = Frame((0.0, 0.0, float(ug.Wg), float(ug.Hg)), d_in)
    dp = (along, e.pos) if e.horizontal else (e.pos, along)
    du, dv = fr.uv(*dp)
    fac = dict.fromkeys(SIDES, 0.0)
    for ed in ug.edges:
        if ed.kind != "facade":
            continue
        s = side_of((0, ed.outward) if ed.horizontal else (ed.outward, 0), d_in)
        fac[s] += (ed.b - ed.a) / max(fr.W if s in ("front", "back") else fr.D, 1e-9)
    return {"frame": fr, "W": fr.W * 0.1, "D": fr.D * 0.1, "door": (du, dv),
            "facade": [round(min(1.0, fac[s]), 2) for s in SIDES], "carpet": ug.interior.area * NET_SHARE}


def _facade_m(fac: list[float], mirror: bool) -> list[float]:
    return [fac[0], fac[1], fac[3], fac[2]] if mirror else list(fac)


def case_distance(case: dict[str, Any], uf: dict[str, Any], mirror: bool) -> float:
    """Dissimilarity of a case (mirrored or not) to a flat frame; lower is closer."""
    asp = abs(math.log(max(case["D"], 0.5) / max(case["W"], 0.5)) - math.log(max(uf["D"], 0.5) / max(uf["W"], 0.5)))
    du = case["door"][0]
    du = 1.0 - du if mirror else du
    door = abs(du - uf["door"][0])
    cf, ff = _facade_m(case["facade"], mirror), uf["facade"]
    fac = sum(abs(a - b) for a, b in zip(cf, ff, strict=True))
    nside = abs(sum(1 for a in cf if a >= 0.25) - sum(1 for b in ff if b >= 0.25))
    area = abs(math.log(case["carpet"] / max(uf["carpet"], 1.0)))
    return 2.0 * asp + 1.5 * door + 1.0 * fac + 0.5 * nside + 2.0 * area - 0.01 * (case["score"] - 75.0)


def retrieve(unit, programme=None, k: int = 3, *, plate=None, ug=None, bhk: int | None = None) -> list[dict[str, Any]]:
    """The ``k`` approved cases most similar to a flat, best first: [{"case", "mirror", "distance"}].

    ``unit`` is a ``Unit`` (with ``plate``, or with a precomputed solver grid ``ug``) or a ``unit_frame`` dict. Filters: same
    BHK (the programme's), carpet within +-20 % (widened to +-35 %, then any BHK +-1, only when fewer than ``k`` cases pass).
    Ranks by bounding-box aspect, door position along the entrance side, façade coverage of the four sides, number of
    façade sides and area. Deterministic (ties broken by case id)."""
    uf = unit if isinstance(unit, dict) else None
    if uf is None:
        if ug is None:
            from .solver import unit_grid

            ug = unit_grid(plate, unit)
        uf = unit_frame(ug)
    if uf is None:
        return []
    if bhk is None:
        bhk = programme.bhk if programme is not None else getattr(getattr(unit, "programme", None), "bhk", None)
    cases = load()
    out: list[dict[str, Any]] = []
    for tol, dbhk in ((AREA_TOL, 0), (0.35, 0), (0.35, 1)):
        pool = [c for c in cases if (bhk is None or abs(c["bhk"] - bhk) <= dbhk)
                and abs(c["carpet"] / max(uf["carpet"], 1.0) - 1.0) <= tol]
        ranked = []
        for c in pool:
            dm = min((case_distance(c, uf, m), m) for m in (False, True))
            ranked.append((round(dm[0], 6), c["id"], dm[1], c))
        ranked.sort(key=lambda t: (t[0], t[1]))
        out = [{"case": c, "mirror": m, "distance": d} for d, _i, m, c in ranked[:k]]
        if len(out) >= k:
            break
    return out


# ------------------------------------------------------------------------------------------------ adaptation


def hungarian(cost: list[list[float]]) -> list[int]:
    """Minimum-cost assignment for an n x m matrix (n <= m): column index for each row (Kuhn-Munkres, O(n^2 m))."""
    n = len(cost)
    if n == 0:
        return []
    m = len(cost[0])
    INF = float("inf")
    u, v = [0.0] * (n + 1), [0.0] * (m + 1)
    p, way = [0] * (m + 1), [0] * (m + 1)
    for i in range(1, n + 1):
        p[0], j0 = i, 0
        minv, used = [INF] * (m + 1), [False] * (m + 1)
        while True:
            used[j0] = True
            i0, delta, j1 = p[j0], INF, 0
            for j in range(1, m + 1):
                if not used[j]:
                    cur = cost[i0 - 1][j - 1] - u[i0] - v[j]
                    if cur < minv[j]:
                        minv[j], way[j] = cur, j0
                    if minv[j] < delta:
                        delta, j1 = minv[j], j
            for j in range(m + 1):
                if used[j]:
                    u[p[j]] += delta
                    v[j] -= delta
                else:
                    minv[j] -= delta
            j0 = j1
            if p[j0] == 0:
                break
        while True:
            j1 = way[j0]
            p[j0] = p[j1]
            j0 = j1
            if j0 == 0:
                break
    res = [-1] * n
    for j in range(1, m + 1):
        if p[j]:
            res[p[j] - 1] = j - 1
    return res


TYPE_COST = {frozenset(("master", "bedroom")): 0.5, frozenset(("toilet", "bath")): 0.1, frozenset(("toilet", "wc")): 0.4,
             frozenset(("bath", "wc")): 0.4, frozenset(("foyer", "passage")): 0.8, frozenset(("study", "bedroom")): 0.9,
             frozenset(("store", "utility")): 0.6, frozenset(("dress", "store")): 0.8}
UNMATCHED = 2.5
NO_MATCH = 1e4


def _type_cost(a: str, b: str) -> float:
    return 0.0 if a == b else TYPE_COST.get(frozenset((a, b)), NO_MATCH)


def match_rooms(case: dict[str, Any], specs, links: list[tuple[str, str]], scale: float) -> dict[str, int]:
    """Programme room id -> case room index. Dry rooms first (type + size), then wet rooms, whose cost adds 1.5 when the
    case toilet's bedroom (door-linked) is not the room the programme attaches the toilet to."""
    crooms = case["rooms"]
    owner_c: dict[int, int] = {}
    for a, b in case["edges"]:
        for x, y in ((a, b), (b, a)):
            if crooms[x]["t"] in WET and crooms[y]["t"] in BEDS:
                owner_c[x] = y
    stypes = {s.id: s.type for s in specs}
    owner_s: dict[str, str] = {}
    for a, b in links:
        for x, y in ((a, b), (b, a)):
            if stypes.get(x) in WET and stypes.get(y) in BEDS:
                owner_s[x] = y
    out: dict[str, int] = {}
    for wet in (False, True):
        rows = [s for s in specs if (s.type in WET) == wet]
        cols = [i for i, r in enumerate(crooms) if (r["t"] in WET) == wet and i not in out.values()]
        if not rows or not cols:
            continue
        rev = {ci: sid for sid, ci in out.items()}
        cost = []
        for s in rows:
            row = []
            for ci in cols:
                r = crooms[ci]
                c = _type_cost(r["t"], s.type)
                if c < NO_MATCH:
                    c += abs(math.log(max(r["a"] * scale, 0.5) / max(s.target_area, 0.5)))
                    if wet:
                        oc, os_ = owner_c.get(ci), owner_s.get(s.id)
                        if (oc is None) != (os_ is None) or (oc is not None and rev.get(oc) != os_):
                            c += 1.5
                row.append(c)
            row += [UNMATCHED] * len(rows)  # dummy columns: a programme room may stay unmatched
            cost.append(row)
        for s, j in zip(rows, hungarian(cost), strict=True):
            if 0 <= j < len(cols) and cost[rows.index(s)][j] < NO_MATCH:
                out[s.id] = cols[j]
    return out


def adapt(case: dict[str, Any], ug, specs, links: list[tuple[str, str]] | None = None, mirror: bool = False,
          carpet: float | None = None) -> dict[str, Any] | None:
    """Map ``case`` into the flat's solver grid: {"case_id", "mirror", "rects": {room: (x0, y0, x1, y1) grid},
    "order": [(a, b, axis)] meaning room a's centre is below/left of b's on ``axis`` ('x' or 'y'), "entry": type}."""
    uf = unit_frame(ug)
    if uf is None:
        return None
    fr: Frame = uf["frame"]
    scale = (carpet or uf["carpet"]) / max(case["carpet"], 1.0)
    match = match_rooms(case, specs, links or [], scale)
    if not match:
        return None
    rects: dict[str, tuple[int, int, int, int]] = {}
    for sid, ci in match.items():
        u0, v0, u1, v1 = case["rooms"][ci]["r"]
        if mirror:
            u0, u1 = 1.0 - u1, 1.0 - u0
        (xa, ya), (xb, yb) = fr.xy(u0, v0), fr.xy(u1, v1)
        x0, x1 = sorted((xa, xb))
        y0, y1 = sorted((ya, yb))
        rects[sid] = (max(0, int(round(x0))), max(0, int(round(y0))), min(ug.Wg, int(round(x1))), min(ug.Hg, int(round(y1))))
    stype = {s.id: s.type for s in specs}
    ids = sorted(rid for rid in rects if stype.get(rid) in MAJOR)
    order: list[tuple[str, str, str]] = []
    gap = ORDER_MIN / 0.1
    for i, a in enumerate(ids):
        for b in ids[i + 1:]:
            ra, rb = rects[a], rects[b]
            for axis, k0, k1 in (("x", 0, 2), ("y", 1, 3)):
                ca, cb = (ra[k0] + ra[k1]) / 2, (rb[k0] + rb[k1]) / 2
                if abs(ca - cb) >= gap:
                    order.append((a, b, axis) if ca < cb else (b, a, axis))
    return {"case_id": case["id"], "mirror": mirror, "rects": rects, "order": order, "entry": case.get("entry"),
            "score": case.get("score")}
