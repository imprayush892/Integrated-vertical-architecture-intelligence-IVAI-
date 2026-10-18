"""Stage 7c - turn solved room cells into a drawn plan: rooms, walls, doors, windows.

* leftover grid slivers are merged into the best neighbouring room (passage > foyer > living > ...);
* rooms are shrunk by half an internal wall (115 mm walls) where two rooms meet; the flat's outer band
  (230 mm on façade/passage/shaft sides, 115 mm on party sides) is already outside the solver interior;
* doors go on the shared wall of every link; the entry door on the flat's passage edge;
* windows go on façade edges, sized for the 1/6 glazing rule; toilets get ventilators on façade/shaft.
"""

from __future__ import annotations

import math

import shapely
from shapely.geometry import LineString, Polygon, box
from shapely.ops import linemerge

from . import geo
from .models import Opening, Plan, PlanRoom, Wall
from .solver import EXT_WALL, INT_WALL, WIN_HEIGHT, G, RoomSpec, UnitGrid, entry_fallback, rect_to_poly

MERGE_PRIORITY = ["dining", "living", "master", "bedroom", "study", "dress", "kitchen", "utility", "pooja", "store",
                  "toilet", "bath", "wc", "foyer", "passage", "balcony"]  # v2: circulation takes leftovers last
DOOR_W = {"toilet": 0.75, "bath": 0.75, "wc": 0.75, "utility": 0.75, "balcony": 0.9}
ENTRY_W = 1.0
VENT_W = 0.6
VENT_H = 0.45


def _shaft_dims(shaft_lines, contact) -> tuple[float, float]:
    """(area, shorter side) of the AVS a room touches, from the shaft edges seen by the unit.

    Shafts are drawn square (plate.py); a single visible side is taken as the side of a square shaft. The
    flat only sees the part of the shaft it touches, so this is a lower bound (small result = "review")."""
    touching = [ln for ln in shaft_lines if any(ln.distance(LineString(seg)) < 0.05 for seg in contact)]
    if not touching:
        return 0.0, 0.0
    near = [ln for ln in shaft_lines if any(ln.distance(t) < 0.05 for t in touching)]
    x0, y0, x1, y1 = shapely.union_all(near).bounds
    w, h = x1 - x0, y1 - y0
    if min(w, h) < 0.3:
        w = h = max(w, h)
    return round(w * h, 2), round(min(w, h), 2)


def clear_width(net: Polygon) -> float:
    """Exact clear width of a rectilinear room (see ``geo.clear_width``)."""
    return geo.clear_width(net)


def _segments(geom) -> list[tuple[tuple[float, float], tuple[float, float]]]:
    if geom is None or geom.is_empty:
        return []
    lines = [g for g in getattr(geom, "geoms", [geom]) if g.geom_type in ("LineString", "MultiLineString")]
    if not lines:
        return []
    merged = linemerge(lines) if len(lines) > 1 or lines[0].geom_type == "MultiLineString" else lines[0]
    out = []
    for ln in getattr(merged, "geoms", [merged]):
        cs = list(ln.coords)
        # split at corners, merge collinear runs
        run = [cs[0]]
        for p in cs[1:]:
            if len(run) >= 2:
                (x0, y0), (x1, y1) = run[-2], run[-1]
                cross = (x1 - x0) * (p[1] - y1) - (y1 - y0) * (p[0] - x1)
                if abs(cross) < 1e-9:
                    run[-1] = p
                    continue
                out.append((run[0], run[-1]))
                run = [run[-1], p]
            else:
                run.append(p)
        if len(run) >= 2:
            out.append((run[0], run[-1]))
    return [s for s in out if math.dist(*s) > 1e-6]


def _stays_rect(room: Polygon, comp: Polygon, rtype: str) -> bool:
    """v2: merging ``comp`` keeps the room near-rectangular (else it becomes a store: an L-shaped dining with a
    1.5 m arm fits no table)."""
    u = shapely.union(room, comp).buffer(0)
    rect = u.area / max(u.minimum_rotated_rectangle.area, 1e-6)
    return rect >= (0.75 if rtype in ("living", "dining", "kitchen") else 0.85)


def _merge_leftover(cells: dict[str, Polygon], interior: Polygon, types: dict[str, str],
                    max_area: dict[str, float] | None = None, waste_cap: dict[str, float] | None = None,
                    owner: dict[str, str] | None = None, soft: dict[str, float] | None = None) -> dict[str, Polygon]:
    """Give leftover grid space to neighbours. ``max_area`` caps window rooms so their 1/6 glazing holds;
    ``waste_cap`` (v2) is the size beyond which a room's floor counts as waste: a neighbour that stays under it is
    preferred, and circulation takes leftovers last. A usable pocket against a bedroom becomes a walk-in wardrobe
    opening off that bedroom (recorded in ``owner``); leftover nobody can take (>= 1 m2) becomes a store, smaller
    bits become wall."""
    if not cells:
        return cells
    max_area = max_area or {}
    waste_cap = waste_cap or {}
    soft = soft or {}
    owner = owner if owner is not None else {}
    used = shapely.union_all(list(cells.values())) if cells else Polygon()
    left = interior.difference(used)
    # slivers thinner than 0.3 m (grid rounding along the walls) stay wall; only real pockets are given away
    left = left.buffer(-0.15, join_style="mitre").buffer(0.15, join_style="mitre").intersection(left)
    for comp in sorted(geo.polygons_of(left), key=lambda g: -g.area):
        if comp.area < 1e-4:
            continue
        nbrs = []
        for rid, c in cells.items():
            L = c.boundary.intersection(comp.boundary).length if c.distance(comp) < 1e-6 else 0.0
            if L > 0.05:
                nbrs.append((rid, L))
        # never grow a balcony into the flat, nor a window room past what its window can light, nor (v2) a small
        # room (pooja, wardrobe, powder, toilet, store) past its own size: a 20 m2 pooja is worse than a store
        nbrs = [t for t in nbrs if types.get(t[0]) != "balcony"
                and cells[t[0]].area + comp.area <= max_area.get(t[0], 1e9)
                and not (types.get(t[0]) in ("passage", "foyer") and comp.area >= 1.0)  # v2: circulation stays lean
                and not (comp.area >= 1.0 and not _stays_rect(cells[t[0]], comp, types.get(t[0], "")))
                and not (types.get(t[0]) in SMALL_ROOMS and comp.area >= 0.6
                         and cells[t[0]].area + comp.area > min(soft.get(t[0], 1e9), waste_cap.get(t[0], 1e9)))]
        beds = [t for t in nbrs if types.get(t[0]) in ("master", "bedroom") and t[1] >= 0.8]
        if (beds and 1.5 <= comp.area <= 6.0 and comp.area / max(comp.minimum_rotated_rectangle.area, 1e-6) >= 0.8
                and geo.clear_width(comp) >= 0.9):
            k = 1
            while f"wardrobe{k}" in cells:
                k += 1
            nid = f"wardrobe{k}"
            cells[nid] = comp
            types[nid] = "dress"
            owner[nid] = max(beds, key=lambda t: t[1])[0]
            continue
        if not nbrs:
            if comp.area >= 1.0:
                k = 1
                while f"store{k}" in cells:
                    k += 1
                cells[f"store{k}"] = comp
                types[f"store{k}"] = "store"
            continue
        if comp.area < 0.6 or (not MERGE_SLIVERS and comp.area < SLIVER):
            if not MERGE_SLIVERS:
                continue  # v2: a small pocket stays solid (a duct), it does not notch a room
            rid = max(nbrs, key=lambda t: t[1])[0]
        else:
            def rank(t: tuple[str, float], extra: float = comp.area) -> tuple[bool, bool, int, float]:
                ty = types.get(t[0], "")
                pr = MERGE_PRIORITY.index(ty) if ty in MERGE_PRIORITY else len(MERGE_PRIORITY)
                after = cells[t[0]].area + extra
                return (after > waste_cap.get(t[0], 1e9), after > soft.get(t[0], 1e9), pr, -t[1])

            rid = min(nbrs, key=rank)[0]
        merged = shapely.union(cells[rid], comp)
        parts = geo.polygons_of(merged.buffer(0))
        cells[rid] = max(parts, key=lambda p: p.area) if parts else cells[rid]
    return cells


SMALL_ROOMS = {"pooja", "dress", "wc", "toilet", "bath", "store", "utility"}
NO_GROW = {"passage", "foyer", "balcony"}  # circulation never grows; balconies keep their depth
HUB_TYPES = ("passage", "foyer", "dining", "living")  # spaces doors open onto
PRIVATE_DOOR = ("master", "bedroom", "study", "toilet", "bath", "wc")
WET_ROOMS = ("toilet", "bath", "wc")
CLUSTER_DOORS = True
SLIVER = 0.6
DOOR_PREF = {"passage": 4, "foyer": 4, "dining": 4, "living": 3, "kitchen": 3, "utility": 3, "master": 2, "bedroom": 2,
             "pooja": 0, "dress": 0}  # v3: which neighbour a doorless room opens onto, best first
DUCT_MAX = 2.0  # m2: v3, a doorless pocket up to this size beside a toilet is drawn as a plumbing duct
MERGE_SLIVERS = False  # v2: pockets under SLIVER m2 stay solid (duct) instead of notching a room


def _on_grid(c: Polygon) -> Polygon:
    """The polygon with its coordinates rounded to 1 mm (no precision model attached: shapely keeps a set_precision
    grid on later results, which breaks the 1e-6 buffers geo.clear_width relies on)."""
    parts = geo.polygons_of(c)
    if not parts:
        return c
    p = max(parts, key=lambda q: q.area)

    def rr(ring):
        return [(round(x, 3), round(y, 3)) for x, y in ring.coords]

    g = Polygon(rr(p.exterior), [rr(h) for h in p.interiors]).buffer(0)
    gp = geo.polygons_of(g)
    return max(gp, key=lambda q: q.area) if gp else p


def _snap_pockets(cells: dict[str, Polygon], types: dict[str, str], interior: Polygon) -> dict[str, Polygon]:
    """v3: leftover pockets were opened by 0.15 m (slivers stay wall), which can leave a hair gap between a store and
    the rooms around it, so it shares no wall and gets no door. Grow each store / leftover wardrobe back into the free
    floor within 0.3 m so it meets its neighbours again."""
    for rid in [r for r in cells if types.get(r) in ("store", "dress") and (r.startswith("store") or r.startswith("wardrobe"))]:
        others = [g for o, g in cells.items() if o != rid]
        occ = shapely.union_all(others) if others else Polygon()
        fill = cells[rid].buffer(0.32, join_style="mitre").intersection(interior).difference(occ)
        u = shapely.union(cells[rid], fill).buffer(0)
        parts = geo.polygons_of(u)
        if parts:
            g = _on_grid(max(parts, key=lambda q: q.area))
            if g.geom_type == "Polygon" and g.area >= cells[rid].area:
                cells[rid] = g
    return cells


def _seal_pockets(cells: dict[str, Polygon], types: dict[str, str]) -> dict[str, Polygon]:
    """v3: a leftover store that no dry room can open onto (only toilets around it) would be a sealed pocket
    (critic orphan). Up to DUCT_MAX it is a plumbing duct (solid); larger, it joins the toilet it backs onto when the
    toilet stays near-rectangular (a bath with a dressing area), else it stays a store."""
    for rid in [r for r in cells if types.get(r) == "store"]:
        c = cells[rid]
        nb = []
        for o, g in cells.items():
            if o != rid and g.distance(c) < 1e-6:
                L = g.boundary.intersection(c.boundary).length
                if L > 0.05:
                    nb.append((o, L))
        if any(types.get(o) not in WET_ROOMS + ("balcony",) and L >= 0.8 for o, L in nb):
            continue
        if c.area <= DUCT_MAX:
            del cells[rid]
            continue
        wet = sorted((t for t in nb if types.get(t[0]) in WET_ROOMS), key=lambda t: -t[1])
        for o, _L in wet:
            u = shapely.union(cells[o], c).buffer(0)
            if u.geom_type == "Polygon" and u.area / max(u.minimum_rotated_rectangle.area, 1e-6) >= 0.75:
                cells[o] = u
                del cells[rid]
                break
    return cells


def _facade_side(ug: UnitGrid, x: int, y: int, w: int, h: int) -> int:
    """Longest run (cells) of this rectangle's sides that lies on a façade edge."""
    best = 0
    for e in ug.edges:
        if e.kind != "facade":
            continue
        if e.horizontal:
            if (e.outward > 0 and y + h == e.pos) or (e.outward < 0 and y == e.pos):
                best = max(best, min(x + w, e.b) - max(x, e.a))
        elif (e.outward > 0 and x + w == e.pos) or (e.outward < 0 and x == e.pos):
            best = max(best, min(y + h, e.b) - max(y, e.a))
    return best


def grow_to_fill(ug: UnitGrid, specs: list[RoomSpec], rects: dict[str, tuple[int, int, int, int]],
                 max_steps: int = 60) -> dict[str, tuple[int, int, int, int]]:
    """v2: push room sides out into floor the solver left uncovered, keeping every room a rectangle.

    A side moves only across a strip that is entirely free, so no wall contact (door) is lost. Each step is priced
    with the solver's own objective (uncovered floor costs more than a room above its target or cap), within the
    room's aspect limit, 1.2 x its hard cap and the 1/6 glazing rule; circulation never grows. Whatever stays
    uncovered is handed to ``_merge_leftover``."""
    import numpy as np

    from .solver import CAP_W, UNCOVERED_W, UNDER_W

    W, H = ug.Wg, ug.Hg
    occ = np.zeros((max(W, 1), max(H, 1)), dtype=np.int16)
    for x0, y0, x1, y1 in ug.blocked:
        occ[x0:x1, y0:y1] = -1
    out = dict(rects)
    ids = list(out)
    for k, rid in enumerate(ids):
        x, y, w, h = out[rid]
        occ[x:x + w, y:y + h] = k + 1
    spec = {s.id: s for s in specs}

    def cost(s: RoomSpec, w: int, h: int) -> float:
        a = w * h
        net = a - 1.15 * (w + h)
        c = UNDER_W * max(0, s.tgt - a) + s.over_w * max(0, a - s.tgt)
        if s.cap:
            c += CAP_W * max(0.0, net - s.cap)
        return c

    for _ in range(max_steps):
        best = None
        for k, rid in enumerate(ids):
            s = spec.get(rid)
            if s is None or s.type in NO_GROW:
                continue
            x, y, w, h = out[rid]
            base = cost(s, w, h)
            for side in ("l", "r", "b", "t"):
                d = 0
                if side == "l":
                    while x - d - 1 >= 0 and not occ[x - d - 1, y:y + h].any():
                        d += 1
                    nr = (x - d, y, w + d, h)
                elif side == "r":
                    while x + w + d < W and not occ[x + w + d, y:y + h].any():
                        d += 1
                    nr = (x, y, w + d, h)
                elif side == "b":
                    while y - d - 1 >= 0 and not occ[x:x + w, y - d - 1].any():
                        d += 1
                    nr = (x, y - d, w, h + d)
                else:
                    while y + h + d < H and not occ[x:x + w, y + h + d].any():
                        d += 1
                    nr = (x, y, w, h + d)
                if d == 0:
                    continue
                nx, ny, nw, nh = nr
                if 10 * max(nw, nh) > s.aspect * min(nw, nh):
                    continue
                if s.hard and nw * nh > 1.2 * max(s.hard, s.tgt):
                    continue
                if s.window_run and s.type != "balcony" and 90 * _facade_side(ug, nx, ny, nw, nh) < nw * nh + 360:
                    continue
                gain = nw * nh - w * h
                delta = cost(s, nw, nh) - base - UNCOVERED_W * gain
                if delta < 0 and (best is None or delta < best[0]):
                    best = (delta, k, rid, nr)
        if best is None:
            break
        _, k, rid, nr = best
        x, y, w, h = nr
        occ[x:x + w, y:y + h] = k + 1
        out[rid] = nr
    return out


def _largest_rect(free) -> tuple[int, int, int, int, int]:
    """Largest all-True axis-aligned rectangle in a boolean grid: (area, x, y, w, h)."""
    W, H = free.shape
    heights = [0] * H
    best = (0, 0, 0, 0, 0)
    for x in range(W):
        col = free[x]
        for y in range(H):
            heights[y] = heights[y] + 1 if col[y] else 0
        stack: list[int] = []  # largest rectangle in the histogram along y (bar = run of free cells ending at x)
        for y in range(H + 1):
            hgt = heights[y] if y < H else 0
            start = y
            while stack and heights[stack[-1]] >= hgt:
                top = stack.pop()
                left = stack[-1] + 1 if stack else 0
                area = heights[top] * (y - left)
                if area > best[0]:
                    best = (area, x - heights[top] + 1, left, heights[top], y - left)
                start = left
            if y < H:
                stack.append(y)
        del start
    return best


POCKET_KINDS = {"toilet": (3.0, 5.0, 15), "dress": (1.8, 5.5, 12), "pooja": (1.4, 2.8, 12), "utility": (1.5, 4.5, 10)}  # m2 min, m2 max, min side


def fill_pockets(ug: UnitGrid, specs: list[RoomSpec], rects: dict[str, tuple[int, int, int, int]],
                 links: list[tuple[str, str]], max_rooms: int = 4):
    """v2: floor the solver could not cover becomes the rooms an architect would put there instead of a sealed
    store: an attached toilet for a bedroom that has none, a walk-in wardrobe off a bedroom, a pooja room off the
    living/dining/hub. Each is cut as a rectangle from the largest free pocket, sized within the critic's waste caps;
    returns (rects, new specs, new links)."""
    import numpy as np

    W, H = ug.Wg, ug.Hg
    occ = np.zeros((max(W, 1), max(H, 1)), dtype=np.int16)
    for x0, y0, x1, y1 in ug.blocked:
        occ[x0:x1, y0:y1] = -1
    out = dict(rects)
    ids = list(out)
    for k, rid in enumerate(ids):
        x, y, w, h = out[rid]
        occ[x:x + w, y:y + h] = k + 1
    types = {s.id: s.type for s in specs}
    has_toilet = {a if types.get(b) in ("toilet", "bath") else b for a, b in links
                  if {types.get(a), types.get(b)} & {"toilet", "bath"} and {types.get(a), types.get(b)} & {"master", "bedroom"}}
    has_dress = {a if types.get(b) == "dress" else b for a, b in links if "dress" in (types.get(a), types.get(b))}
    has_pooja = any(t == "pooja" for t in types.values())
    # the programme's optional utility the solver left out may take a pocket beside the kitchen
    utility = next((s.id for s in specs if s.type == "utility" and s.id not in rects), None)
    new_specs: list[RoomSpec] = []
    new_links: list[tuple[str, str]] = []
    for _ in range(max_rooms):
        area, x, y, w, h = _largest_rect(occ == 0)
        if area < 180 or min(w, h) < 10:
            break
        # rooms along the pocket's sides, with the length of wall they share with it
        contact: dict[str, int] = {}
        for xs, ys in (((x - 1,), range(y, y + h)), ((x + w,), range(y, y + h)), (range(x, x + w), (y - 1,)),
                       (range(x, x + w), (y + h,))):
            for cx in xs:
                for cy in ys:
                    if 0 <= cx < W and 0 <= cy < H and occ[cx, cy] > 0:
                        rid = ids[occ[cx, cy] - 1]
                        contact[rid] = contact.get(rid, 0) + 1
        beds = sorted((r for r, L in contact.items() if types.get(r) in ("master", "bedroom") and L >= 10),
                      key=lambda r: -contact[r])
        hubs = [r for r, L in contact.items() if types.get(r) in ("dining", "living", "passage", "foyer") and L >= 10]
        kitchens = [r for r, L in contact.items() if types.get(r) == "kitchen" and L >= 10]
        kind = owner = None
        if utility and kitchens:
            kind, owner = "utility", kitchens[0]
        for b in ([] if kind else beds):
            if b not in has_toilet and area >= 300 and min(w, h) >= 15:
                kind, owner = "toilet", b
                break
            if b not in has_dress:
                kind, owner = "dress", b
                break
        if kind is None and hubs and not has_pooja:
            kind, owner = "pooja", hubs[0]
        if kind is None:
            occ[x:x + w, y:y + h] = -2  # nobody can use it: leave it to the leftover merge
            continue
        amin, amax, side = POCKET_KINDS[kind]
        # trim to the size cap, keeping the side against the owner
        ox, oy, ow, oh = out[owner]
        cap = int(amax * 100)
        if w * h > cap:
            if ox + ow == x or ox == x + w:  # owner beside the pocket: keep the full height, cut the depth
                nw = max(side, min(w, cap // max(h, 1)))
                x = x if ox + ow == x else x + w - nw
                w = nw
                if w * h > cap:
                    nh = max(side, cap // w)
                    y = max(y, min(oy, y + h - nh)) if oy + oh >= y + nh else y
                    h = nh
            else:  # owner above/below
                nh = max(side, min(h, cap // max(w, 1)))
                y = y if oy + oh == y else y + h - nh
                h = nh
                if w * h > cap:
                    nw = max(side, cap // h)
                    x = max(x, min(ox, x + w - nw)) if ox + ow >= x + nw else x
                    w = nw
        if w * h < amin * 100 or min(w, h) < side or max(w, h) > (2 if kind == "pooja" else 3) * min(w, h):
            occ[x:x + w, y:y + h] = -2
            continue
        k = len(ids) + 1
        nid = f"{kind}_{owner}" if f"{kind}_{owner}" not in out else f"{kind}{k}"
        if kind == "utility":
            nid, utility = utility, None
        ids.append(nid)
        occ[x:x + w, y:y + h] = len(ids)
        out[nid] = (x, y, w, h)
        a_m2 = w * h / 100
        name = {"toilet": "Toilet", "dress": "Dress", "pooja": "Pooja", "utility": "Utility"}[kind]
        new_specs.append(RoomSpec(id=nid, type=kind, name=name, tgt=w * h, amin=0, wmin=0, aspect=30, window_run=0,
                                  vent_run=0, optional=True, target_area=round(a_m2, 2),
                                  cap=int(100 * min(amax + 0.5, 1.3 * a_m2 + 0.5)), over_w=10,
                                  hard=int(100 * (amax + 0.5) / 1.2)))
        new_links.append((owner, nid))
        types[nid] = kind
        if kind == "toilet":
            has_toilet.add(owner)
        elif kind == "dress":
            has_dress.add(owner)
        else:
            has_pooja = True
    return out, new_specs, new_links


def _place_on_segment(seg, width: float, at: float | None = None, margin: float = 0.15):
    (ax, ay), (bx, by) = seg
    L = math.dist(seg[0], seg[1])
    if L < width + 2 * 0.05:
        width = max(0.0, L - 0.1)
    if width <= 0:
        return None
    ux, uy = (bx - ax) / L, (by - ay) / L
    if at is None:
        t0 = min(max(margin, 0.0), L - width)
    else:
        t0 = min(max(at - width / 2, 0.05), L - width - 0.05)
    a = (ax + ux * t0, ay + uy * t0)
    b = (ax + ux * (t0 + width), ay + uy * (t0 + width))
    return (round(a[0], 3), round(a[1], 3)), (round(b[0], 3), round(b[1], 3))


BED = {"master", "bedroom"}
SIGHT_OFF = 0.3  # m: the critic's sight rays either side of the front door's centre line
SIGHT_CLEAR = 0.5  # m: a bedroom door keeps this far from every sight ray (critic reach 0.35 m)


def _out_of_sight(seg, width: float, placed, sight: list[LineString]):
    """v3: slide a bedroom door along its wall until it is out of the front door's line of sight (the position nearest
    to where it was put); unchanged when the wall offers no such position."""
    def clear(pl) -> float:
        d = LineString(pl)
        return min(d.distance(r) for r in sight)

    if clear(placed) >= SIGHT_CLEAR:
        return placed
    L = math.dist(*seg)
    t0 = LineString(seg).project(shapely.geometry.Point((placed[0][0] + placed[1][0]) / 2, (placed[0][1] + placed[1][1]) / 2))
    best = None
    k = 0.15 + width / 2
    while k <= L - 0.15 - width / 2 + 1e-9:
        pl = _place_on_segment(seg, width, at=k)
        if pl and clear(pl) >= SIGHT_CLEAR and (best is None or abs(k - t0) < best[0]):
            best = (abs(k - t0), pl)
        k += 0.1
    return best[1] if best else placed


def _cut(a, b, depth: float, both: bool = True) -> Polygon:
    """Rectangle around segment a-b, `depth` either side (to cut a wall opening)."""
    return LineString([a, b]).buffer(depth, cap_style="flat") if both else LineString([a, b]).buffer(depth)


def draft(ug: UnitGrid, specs: list[RoomSpec], rects: dict[str, tuple[int, int, int, int]],
          links: list[tuple[str, str]], entry: str, overrides: dict[str, dict] | None = None,
          hub_point: tuple[int, int] | None = None) -> Plan:
    overrides = overrides or {}
    spec = {s.id: s for s in specs}
    types = {s.id: s.type for s in specs}
    rects = grow_to_fill(ug, specs, rects)
    rects, more, more_links = fill_pockets(ug, specs, rects, list(links))
    if more:
        specs = [*specs, *more]
        spec.update({s.id: s for s in more})
        types.update({s.id: s.type for s in more})
        links = [*links, *more_links]
        rects = grow_to_fill(ug, specs, rects)
    cells = {rid: rect_to_poly(ug, r) for rid, r in rects.items()}
    base = dict(cells)  # the solver's rectangles: openings are placed on these (they are on the façade by construction)
    if entry not in cells:
        entry = entry_fallback(specs, entry) or entry
    caps: dict[str, float] = {}  # hard: a window room's 1/6 glazing
    soft: dict[str, float] = {}  # v2 preference only: better an oversized toilet than a store reached through it
    if True:
        fl = []
        for e in ug.edges:
            if e.kind == "facade":
                fl.append(LineString([(ug.ox + e.a * G, ug.oy + e.pos * G), (ug.ox + e.b * G, ug.oy + e.pos * G)])
                          if e.horizontal else
                          LineString([(ug.ox + e.pos * G, ug.oy + e.a * G), (ug.ox + e.pos * G, ug.oy + e.b * G)]))
        fac_all = shapely.union_all(fl) if fl else LineString()
        for rid, c in cells.items():
            # service rooms may grow at most 60% (or 1.5 m2) past their target when absorbing leftovers
            if spec[rid].type in ("toilet", "bath", "wc", "utility", "store", "pooja", "dress", "kitchen", "foyer"):
                soft[rid] = max(c.area, spec[rid].target_area * 1.6, spec[rid].target_area + 1.5)
            if spec[rid].window_run and spec[rid].type != "balcony" and not fac_all.is_empty:
                segs = _segments(c.boundary.intersection(fac_all))
                L = max((math.dist(*sg) for sg in segs), default=0.0)
                cap_w = max(c.area, (L - 0.4) * 9.0)  # window (L-0.25) x 1.5 m >= area / 6, with margin
                caps[rid] = cap_w
    waste_cap = {rid: spec[rid].cap / 100 + 0.12 * math.sqrt(max(spec[rid].cap, 1) / 100) * 4
                 for rid in cells if spec[rid].cap}  # net cap -> gross (half walls on the perimeter)
    owner: dict[str, str] = {}
    cells = _merge_leftover(cells, ug.interior, types, caps, waste_cap, owner, soft)
    # v3: one coordinate grid for every room (1 mm): solver rectangles carry float noise (x.219999...) that leftover
    # pockets do not, and two rooms whose walls differ by 1e-15 share no wall - hence no door (sealed stores in v2)
    cells = {rid: _on_grid(c) for rid, c in cells.items()}
    cells = _snap_pockets(cells, types, ug.interior)
    cells = _seal_pockets(cells, types)
    links = list(links)
    for rid in cells:
        if rid not in spec:  # leftover turned into a wardrobe (off a bedroom) or a store
            kind = types.get(rid, "store")
            spec[rid] = RoomSpec(id=rid, type=kind, name="Wardrobe" if kind == "dress" else "Store", tgt=0, amin=0,
                                 wmin=0, aspect=99, window_run=0, vent_run=0, optional=True,
                                 target_area=round(cells[rid].area, 2))
            base[rid] = cells[rid]
            if rid in owner:
                links.append((owner[rid], rid))
    ids = list(cells)
    # internal walls on shared edges
    shared: dict[tuple[str, str], list] = {}
    wall_lines = []
    for i in range(len(ids)):
        for j in range(i + 1, len(ids)):
            a, b = cells[ids[i]], cells[ids[j]]
            if a.distance(b) > 1e-6:
                continue
            inter = a.boundary.intersection(b.boundary)
            segs = _segments(inter)
            if segs:
                shared[(ids[i], ids[j])] = segs
                wall_lines.extend(LineString(s) for s in segs)
    band = shapely.union_all([ln.buffer(INT_WALL / 2, cap_style="flat") for ln in wall_lines]) if wall_lines else Polygon()
    nets = {}
    for rid, c in cells.items():
        n = c.difference(band) if not band.is_empty else c
        parts = geo.polygons_of(n)
        nets[rid] = _on_grid(max(parts, key=lambda p: p.area)) if parts else c
    # façade / shaft contact on the interior boundary
    facade_lines, shaft_lines = [], []
    for e in ug.edges:
        if e.kind not in ("facade", "shaft"):
            continue
        if e.horizontal:
            ln = LineString([(ug.ox + e.a * G, ug.oy + e.pos * G), (ug.ox + e.b * G, ug.oy + e.pos * G)])
        else:
            ln = LineString([(ug.ox + e.pos * G, ug.oy + e.a * G), (ug.ox + e.pos * G, ug.oy + e.b * G)])
        (facade_lines if e.kind == "facade" else shaft_lines).append(ln)
    facade = shapely.union_all(facade_lines) if facade_lines else LineString()
    shafts = shapely.union_all(shaft_lines) if shaft_lines else LineString()
    doors: list[Opening] = []
    windows: list[Opening] = []
    cut_geoms = []
    # entry door
    if ug.door is not None and entry in cells:
        e, along = ug.door
        if e.horizontal:
            y_in = ug.oy + e.pos * G
            x = ug.ox + along * G
            y_out = y_in - e.outward * EXT_WALL  # through the band to the passage face
            a, b = (round(x - ENTRY_W / 2, 3), round(y_out, 3)), (round(x + ENTRY_W / 2, 3), round(y_out, 3))
            cut_geoms.append(box(x - ENTRY_W / 2, min(y_in, y_out) - 0.05, x + ENTRY_W / 2, max(y_in, y_out) + 0.05))
        else:
            x_in = ug.ox + e.pos * G
            y = ug.oy + along * G
            x_out = x_in - e.outward * EXT_WALL
            a, b = (round(x_out, 3), round(y - ENTRY_W / 2, 3)), (round(x_out, 3), round(y + ENTRY_W / 2, 3))
            cut_geoms.append(box(min(x_in, x_out) - 0.05, y - ENTRY_W / 2, max(x_in, x_out) + 0.05, y + ENTRY_W / 2))
        doors.append(Opening(id="D-entry", kind="entry", a=a, b=b, width=ENTRY_W, rooms=[entry], hinge=a, swing_into=entry))
    # internal doors on links
    linked: set[str] = {entry}
    # v3: the straight line of sight from the front door (critic entry_sight): rays 0.3 m either side of its centre
    sight: list[LineString] = []
    if ug.door is not None and entry in cells:
        e, along = ug.door
        if e.horizontal:
            p0, nv, tv = (ug.ox + along * G, ug.oy + e.pos * G), (0.0, -float(e.outward)), (1.0, 0.0)
        else:
            p0, nv, tv = (ug.ox + e.pos * G, ug.oy + along * G), (-float(e.outward), 0.0), (0.0, 1.0)
        for off in (-SIGHT_OFF, 0.0, SIGHT_OFF):
            q = (p0[0] + tv[0] * off, p0[1] + tv[1] * off)
            sight.append(LineString([q, (q[0] + nv[0] * 30.0, q[1] + nv[1] * 30.0)]))
    if hub_point is None and CLUSTER_DOORS:
        # v2: cluster the private doors (bedrooms, study, guest toilet onto the hub): the point their walls are
        # closest to, found by two rounds of projecting a centre onto each wall
        segs = []
        for ra, rb in links:
            key = (ra, rb) if (ra, rb) in shared else (rb, ra)
            hub_r = next((r for r in (ra, rb) if types.get(r) in HUB_TYPES), None)
            oth = rb if hub_r == ra else ra
            if key in shared and hub_r is not None and types.get(oth) in PRIVATE_DOOR:
                segs.append(LineString(max(shared[key], key=lambda s: math.dist(*s))))
        if len(segs) >= 2:
            cx = sum(sg.centroid.x for sg in segs) / len(segs)
            cy = sum(sg.centroid.y for sg in segs) / len(segs)
            for _ in range(2):
                pts = [sg.interpolate(sg.project(shapely.geometry.Point(cx, cy))) for sg in segs]
                cx, cy = sum(p.x for p in pts) / len(pts), sum(p.y for p in pts) / len(pts)
            hub_point = (round((cx - ug.ox) / G), round((cy - ug.oy) / G))
    for k, (ra, rb) in enumerate(links):
        key = (ra, rb) if (ra, rb) in shared else (rb, ra)
        if key not in shared:
            continue
        seg = max(shared[key], key=lambda s: math.dist(*s))
        w = min(DOOR_W.get(types.get(ra, ""), 0.9), DOOR_W.get(types.get(rb, ""), 0.9))
        # v2: a door onto the hub sits at the end of the wall nearest the hub's centre, so the private doors
        # cluster on one compact hub (and stay in the room's corner, clear of the bed wall)
        hub = next((r for r in (ra, rb) if types.get(r) in HUB_TYPES), None)
        other = rb if hub == ra else ra
        at = None
        if hub is not None and hub_point is not None and types.get(other) in PRIVATE_DOOR:
            # v2: the solver clustered the private doors around hub_point: put this door as close to it as the wall allows
            hp = (ug.ox + hub_point[0] * G, ug.oy + hub_point[1] * G)
            L = math.dist(*seg)
            t = LineString(seg).project(shapely.geometry.Point(hp))
            at = min(max(t, 0.15 + w / 2), L - 0.15 - w / 2) if L > w + 0.3 else None
        elif hub is not None:
            c = cells[hub].centroid
            L = math.dist(*seg)
            if math.dist(seg[1], (c.x, c.y)) < math.dist(seg[0], (c.x, c.y)) and L > w + 0.3:
                at = L - 0.15 - w / 2
        placed = _place_on_segment(seg, w, at=at)
        if not placed:
            continue
        if sight and BED & {types.get(ra), types.get(rb)}:
            placed = _out_of_sight(seg, w, placed, sight)
        a, b = placed
        into = rb if types.get(rb) in ("toilet", "bath", "wc", "master", "bedroom", "study", "utility", "dress", "pooja") else ra
        doors.append(Opening(id=f"D{k + 1}", kind="door", a=a, b=b, width=round(math.dist(a, b), 3),
                             rooms=[ra, rb], hinge=a, swing_into=into))
        cut_geoms.append(_cut(a, b, INT_WALL))
        linked.update((ra, rb))
    # rooms with no door get one onto their best neighbour
    ducts: set[str] = set()
    for rid in ids:
        if rid in linked:
            continue
        cand = [(k, s) for k, segs in shared.items() if rid in k for s in segs]
        if types.get(rid) not in WET_ROOMS:
            # v2: never a door through a toilet (a store reached only through a toilet is worse than a sealed one)
            cand = [(k, s) for k, s in cand if types.get(k[0] if k[1] == rid else k[1]) not in WET_ROOMS]
        if not cand:
            # v3: a small doorless pocket against a toilet is a plumbing duct (solid), not a sealed store
            if types.get(rid) == "store" and cells[rid].area <= DUCT_MAX and any(
                    rid in k and types.get(k[0] if k[1] == rid else k[1]) in WET_ROOMS for k in shared):
                ducts.add(rid)
            continue
        def pref(t, rid=rid):  # v3: a store opens off the hub / kitchen before a bedroom, never off a pooja room first
            o = t[0][0] if t[0][1] == rid else t[0][1]
            L = math.dist(*t[1])
            return (L >= 0.8, DOOR_PREF.get(types.get(o, ""), 1), L)

        k, seg = max(cand, key=pref)
        placed = _place_on_segment(seg, DOOR_W.get(types.get(rid, ""), 0.9))
        if placed:
            other = k[0] if k[1] == rid else k[1]
            doors.append(Opening(id=f"D-{rid}", kind="door", a=placed[0], b=placed[1], width=round(math.dist(*placed), 3),
                                 rooms=[other, rid], hinge=placed[0], swing_into=rid))
            cut_geoms.append(_cut(placed[0], placed[1], INT_WALL))
            linked.add(rid)
    # windows and ventilators
    plan_rooms: list[PlanRoom] = []
    ids = [rid for rid in ids if rid not in ducts]
    for rid in ids:
        s = spec[rid]
        net = nets[rid]
        area = net.area
        has_window = False
        win_area = 0.0
        ventilated = False
        vent_via = None
        shaft_area = shaft_width = None
        f_contact = _segments(base[rid].boundary.intersection(facade)) if not facade.is_empty else []
        s_contact = _segments(base[rid].boundary.intersection(shafts)) if not shafts.is_empty else []
        needs = overrides.get(rid, {}).get("needs_window", s.window_run > 0)
        if f_contact and (needs or s.type in ("living", "master", "bedroom", "study", "kitchen", "dining")):
            seg = max(f_contact, key=lambda t: math.dist(*t))
            want = max(1.0, area / (6 * WIN_HEIGHT) + 0.35)
            L = min(want, math.dist(*seg) - 0.25)
            if L >= 0.6:
                mid = math.dist(*seg) / 2
                placed = _place_on_segment(seg, L, at=mid)
                if placed:
                    windows.append(Opening(id=f"W-{rid}", kind="window", a=placed[0], b=placed[1],
                                           width=round(math.dist(*placed), 3), rooms=[rid]))
                    cut_geoms.append(_cut(placed[0], placed[1], EXT_WALL + 0.05))
                    has_window = True
                    win_area = math.dist(*placed) * WIN_HEIGHT
                    ventilated = True
                    vent_via = "window"
        if not ventilated and s.type in ("toilet", "bath", "wc", "utility", "kitchen", "passage", "foyer", "dining", "store", "dress", "pooja"):
            contact = f_contact or s_contact
            if contact:
                seg = max(contact, key=lambda t: math.dist(*t))
                placed = _place_on_segment(seg, min(VENT_W, math.dist(*seg) - 0.2), at=math.dist(*seg) / 2)
                if placed:
                    windows.append(Opening(id=f"V-{rid}", kind="window", a=placed[0], b=placed[1],
                                           width=round(math.dist(*placed), 3), rooms=[rid]))
                    cut_geoms.append(_cut(placed[0], placed[1], EXT_WALL + 0.05))
                    ventilated = True
                    vent_via = "window" if f_contact else "shaft"
                    if vent_via == "shaft":
                        shaft_area, shaft_width = _shaft_dims(shaft_lines, contact)
                    win_area = math.dist(*placed) * VENT_H
        if not ventilated and (overrides.get(rid, {}).get("vent") == "duct" or s.type in ("toilet", "bath", "wc", "utility")):
            vent_via = "duct"  # reported as "review" by the checks
        plan_rooms.append(
            PlanRoom(
                id=rid, type=s.type, name=s.name,
                polygon=[(round(x, 3), round(y, 3)) for x, y in geo.ring_of(net)],
                area=round(area, 2), width=clear_width(net), has_window=has_window,
                window_area=round(win_area, 3), ventilated=ventilated, vent_via=vent_via,
                vent_shaft_area=shaft_area, vent_shaft_width=shaft_width, unit_id=ug.unit_id,
                target_area=s.target_area,
            )
        )
    # walls = flat minus rooms minus openings
    rooms_u = shapely.union_all([nets[r] for r in ids]) if ids else Polygon()
    walls_geom = ug.unit_poly.difference(rooms_u)
    if cut_geoms:
        walls_geom = walls_geom.difference(shapely.union_all(cut_geoms))
    walls = []
    for i, p in enumerate(geo.polygons_of(walls_geom)):
        if p.area < 0.005:
            continue
        thick = EXT_WALL if p.intersects(ug.unit_poly.exterior.buffer(0.01)) else INT_WALL
        walls.append(Wall(id=f"{ug.unit_id}-W{i + 1}", polygon=geo.to_polyrings(p)[0],
                          thickness=thick, kind="external" if thick == EXT_WALL else "internal"))
    carpet = sum(r.area for r in plan_rooms if r.type != "balcony")
    # RERA carpet includes internal partition walls inside the flat
    carpet += max(0.0, band.intersection(ug.interior).area) if not band.is_empty else 0.0
    plan = Plan(unit_id=ug.unit_id, rooms=plan_rooms, walls=walls, doors=doors, windows=windows,
                carpet_area=round(carpet, 2), status="feasible")
    return rotate_plan(plan, ug.rot, ug.pivot) if ug.rot else plan


def rotate_plan(plan: Plan, rot: float, pivot: tuple[float, float]) -> Plan:
    """Rotate a plan drawn in a flat's solve frame back into the plate frame."""
    a = math.radians(rot)
    c, s_ = math.cos(a), math.sin(a)
    px, py = pivot

    def pt(p):
        x, y = p[0] - px, p[1] - py
        return (round(px + x * c - y * s_, 3), round(py + x * s_ + y * c, 3))

    for r in plan.rooms:
        r.polygon = [pt(p) for p in r.polygon]
    for w in plan.walls:
        w.polygon = [[pt(p) for p in ring] for ring in w.polygon]
    for o in plan.doors + plan.windows:
        o.a, o.b = pt(o.a), pt(o.b)
        if o.hinge:
            o.hinge = pt(o.hinge)
    return plan
