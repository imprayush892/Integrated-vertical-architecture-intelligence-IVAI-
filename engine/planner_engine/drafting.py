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

MERGE_PRIORITY = ["passage", "foyer", "dining", "living", "master", "bedroom", "study", "kitchen", "utility",
                  "balcony", "dress", "store", "pooja", "toilet", "bath", "wc"]
DOOR_W = {"toilet": 0.75, "bath": 0.75, "wc": 0.75, "utility": 0.75, "balcony": 0.9}
ENTRY_W = 1.0
VENT_W = 0.6
VENT_H = 0.45


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


def _merge_leftover(cells: dict[str, Polygon], interior: Polygon, types: dict[str, str],
                    max_area: dict[str, float] | None = None) -> dict[str, Polygon]:
    """Give leftover grid space to neighbours. ``max_area`` caps window rooms so their 1/6 glazing holds;
    leftover nobody can take (>= 1 m2) becomes a store, smaller bits become wall."""
    if not cells:
        return cells
    max_area = max_area or {}
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
        # never grow a balcony into the flat, nor a window room past what its window can light
        nbrs = [t for t in nbrs if types.get(t[0]) != "balcony"
                and cells[t[0]].area + comp.area <= max_area.get(t[0], 1e9)]
        if not nbrs:
            if comp.area >= 1.0:
                k = 1
                while f"store{k}" in cells:
                    k += 1
                cells[f"store{k}"] = comp
                types[f"store{k}"] = "store"
            continue
        if comp.area < 0.6:
            rid = max(nbrs, key=lambda t: t[1])[0]
        else:
            def rank(t: tuple[str, float]) -> tuple[int, float]:
                ty = types.get(t[0], "")
                pr = MERGE_PRIORITY.index(ty) if ty in MERGE_PRIORITY else len(MERGE_PRIORITY)
                return (pr, -t[1])

            rid = min(nbrs, key=rank)[0]
        merged = shapely.union(cells[rid], comp)
        parts = geo.polygons_of(merged.buffer(0))
        cells[rid] = max(parts, key=lambda p: p.area) if parts else cells[rid]
    return cells


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


def _cut(a, b, depth: float, both: bool = True) -> Polygon:
    """Rectangle around segment a-b, `depth` either side (to cut a wall opening)."""
    return LineString([a, b]).buffer(depth, cap_style="flat") if both else LineString([a, b]).buffer(depth)


def draft(ug: UnitGrid, specs: list[RoomSpec], rects: dict[str, tuple[int, int, int, int]],
          links: list[tuple[str, str]], entry: str, overrides: dict[str, dict] | None = None) -> Plan:
    overrides = overrides or {}
    spec = {s.id: s for s in specs}
    types = {s.id: s.type for s in specs}
    cells = {rid: rect_to_poly(ug, r) for rid, r in rects.items()}
    base = dict(cells)  # the solver's rectangles: openings are placed on these (they are on the façade by construction)
    if entry not in cells:
        entry = entry_fallback(specs, entry) or entry
    caps: dict[str, float] = {}
    if True:
        fl = []
        for e in ug.edges:
            if e.kind == "facade":
                fl.append(LineString([(ug.ox + e.a * G, ug.oy + e.pos * G), (ug.ox + e.b * G, ug.oy + e.pos * G)])
                          if e.horizontal else
                          LineString([(ug.ox + e.pos * G, ug.oy + e.a * G), (ug.ox + e.pos * G, ug.oy + e.b * G)]))
        fac_all = shapely.union_all(fl) if fl else LineString()
        for rid, c in cells.items():
            if spec[rid].window_run and spec[rid].type != "balcony" and not fac_all.is_empty:
                segs = _segments(c.boundary.intersection(fac_all))
                L = max((math.dist(*sg) for sg in segs), default=0.0)
                caps[rid] = max(c.area, (L - 0.4) * 9.0)  # window (L-0.25) x 1.5 m >= area / 6, with margin
    cells = _merge_leftover(cells, ug.interior, types, caps)
    for rid in cells:
        if rid not in spec:  # leftover turned into a store
            spec[rid] = RoomSpec(id=rid, type="store", name="Store", tgt=0, amin=0, wmin=0, aspect=99,
                                 window_run=0, vent_run=0, optional=True, target_area=round(cells[rid].area, 2))
            base[rid] = cells[rid]
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
        nets[rid] = max(parts, key=lambda p: p.area) if parts else c
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
    for k, (ra, rb) in enumerate(links):
        key = (ra, rb) if (ra, rb) in shared else (rb, ra)
        if key not in shared:
            continue
        seg = max(shared[key], key=lambda s: math.dist(*s))
        w = min(DOOR_W.get(types.get(ra, ""), 0.9), DOOR_W.get(types.get(rb, ""), 0.9))
        placed = _place_on_segment(seg, w)
        if not placed:
            continue
        a, b = placed
        into = rb if types.get(rb) in ("toilet", "bath", "wc", "master", "bedroom", "study", "utility") else ra
        doors.append(Opening(id=f"D{k + 1}", kind="door", a=a, b=b, width=round(math.dist(a, b), 3),
                             rooms=[ra, rb], hinge=a, swing_into=into))
        cut_geoms.append(_cut(a, b, INT_WALL))
        linked.update((ra, rb))
    # rooms with no door get one onto their best neighbour
    for rid in ids:
        if rid in linked:
            continue
        cand = [(k, s) for k, segs in shared.items() if rid in k for s in segs]
        if not cand:
            continue
        k, seg = max(cand, key=lambda t: math.dist(*t[1]))
        placed = _place_on_segment(seg, DOOR_W.get(types.get(rid, ""), 0.9))
        if placed:
            other = k[0] if k[1] == rid else k[1]
            doors.append(Opening(id=f"D-{rid}", kind="door", a=placed[0], b=placed[1], width=round(math.dist(*placed), 3),
                                 rooms=[other, rid], hinge=placed[0], swing_into=rid))
            cut_geoms.append(_cut(placed[0], placed[1], INT_WALL))
            linked.add(rid)
    # windows and ventilators
    plan_rooms: list[PlanRoom] = []
    for rid in ids:
        s = spec[rid]
        net = nets[rid]
        area = net.area
        has_window = False
        win_area = 0.0
        ventilated = False
        vent_via = None
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
                    win_area = math.dist(*placed) * VENT_H
        if overrides.get(rid, {}).get("vent") == "duct" and not ventilated:
            vent_via = "duct"  # reported as "review" by the checks
        plan_rooms.append(
            PlanRoom(
                id=rid, type=s.type, name=s.name,
                polygon=[(round(x, 3), round(y, 3)) for x, y in geo.ring_of(net)],
                area=round(area, 2), width=geo.clear_width(net), has_window=has_window,
                window_area=round(win_area, 3), ventilated=ventilated, vent_via=vent_via, unit_id=ug.unit_id,
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
