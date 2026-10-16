"""Stage 6 - split the flat zone into units, link each to the core, attach room programmes.

Units are cut from the flat zone with equal-area cuts on either side of the core line. A unit that ends
up without access to the passage, or outside the plate outline, is flagged - never deleted.
Programmes (room lists) are JSON presets; statutory minimums live only in the rules files.
"""

from __future__ import annotations

import json
import math
from functools import lru_cache
from importlib import resources

import shapely
from shapely.geometry import LineString, Polygon, box
from shapely.geometry import Point as SPoint
from shapely.ops import linemerge

from . import geo, rules
from .models import Plate, Programme, ProgrammeRoom, Unit
from .plate import zones_geom

MIN_UNIT_AREA = 25.0
CARPET_RATIO = 0.80  # carpet / flat gross (flat polygons exclude core and passage)
DOOR_CLEAR = 1.2  # need this much straight shared edge for an entry door
ACCESS_RUN = 1.8  # passage frontage each flat keeps when the plate is cut
MIN_FRONTAGE = 7.0  # below this a flat cannot light a living room and a bedroom


def outline_ring_free(plate: Plate):
    """The plate's outer face minus the stretches that face a re-entrant notch (shared with shafts)."""
    ring = Polygon(plate.outline).exterior
    sh = zones_geom(plate, "shaft")
    return ring.difference(sh.buffer(0.05)) if not sh.is_empty else ring


@lru_cache(maxsize=1)
def presets() -> dict[str, Programme]:
    out = {}
    root = resources.files("planner_engine").joinpath("data/programmes")
    for f in sorted(root.iterdir(), key=lambda p: p.name):
        if f.name.endswith(".json"):
            p = Programme(**json.loads(f.read_text()))
            out[p.id] = p
    return out


def statutory_minimums() -> dict[str, tuple[float, float]]:
    """(min area m2, min width m) per room type, read from the rules files."""
    mins: dict[str, list[float]] = {}
    for c in rules.clauses():
        if c.scope != "room" or c.metric not in ("area", "width", "area_and_width"):
            continue
        for t in c.types:
            a, w = mins.get(t, [0.0, 0.0])
            if c.metric == "area":
                a = max(a, float(c.threshold))
            elif c.metric == "width":
                w = max(w, float(c.threshold))
            else:
                a = max(a, float(c.threshold[0]))
                w = max(w, float(c.threshold[1]))
            mins[t] = [a, w]
    return {k: (v[0], v[1]) for k, v in mins.items()}


FRONTAGE_PER_BHK = {1: 7.5, 2: 10.5, 3: 14.0, 4: 17.5}  # façade a flat needs for its window rooms


def pick_preset(carpet_estimate: float, facade: float | None = None, shaft: bool = False) -> Programme:
    """Largest preset whose area range and façade need both fit the flat."""
    ps = sorted(presets().values(), key=lambda p: p.bhk)
    best = ps[0]
    for p in ps:
        lo, _hi = p.area_range
        need = FRONTAGE_PER_BHK.get(p.bhk, 7.5 + 3.5 * (p.bhk - 1)) - (1.5 if shaft else 0.0)
        if carpet_estimate >= lo and (facade is None or facade >= need):
            best = p
    return best


def _cut_equal(geom, k: int, axis: str = "x", contact: tuple[float, float] | None = None,
               measure=None) -> list:
    """Cut geom into k pieces of equal area with lines perpendicular to ``axis``.

    ``contact`` is the x-range where this half meets the passage; cuts are clamped so every piece keeps
    at least ACCESS_RUN of it (every flat gets an entry door)."""
    if k <= 1:
        return [geom]
    minx, miny, maxx, maxy = geom.bounds
    lo_b, hi_b = (minx, maxx) if axis == "x" else (miny, maxy)
    if measure is None:
        def measure(g):
            return g.area
    else:
        _m = measure

        def measure(g):
            parts = geo.polygons_of(g)
            return _m(shapely.union_all(parts)) if parts else 0.0
    total = measure(geom)
    pieces = []
    prev = lo_b
    for i in range(1, k):
        target = total * i / k
        lo, hi = prev, hi_b
        for _ in range(50):
            mid = (lo + hi) / 2
            clip = box(minx - 1, miny - 1, mid, maxy + 1) if axis == "x" else box(minx - 1, miny - 1, maxx + 1, mid)
            if measure(geom.intersection(clip)) < target:
                lo = mid
            else:
                hi = mid
        cut = round((lo + hi) / 2 / geo.SNAP) * geo.SNAP
        if contact is not None:
            c0, c1 = contact
            lo_ok = max(c0 + ACCESS_RUN * i, prev + ACCESS_RUN if prev > c0 else c0 + ACCESS_RUN)
            hi_ok = c1 - ACCESS_RUN * (k - i)
            if lo_ok <= hi_ok:
                cut = min(max(cut, lo_ok), hi_ok)
            cut = round(cut / geo.SNAP) * geo.SNAP
        slab = box(prev, miny - 1, cut, maxy + 1) if axis == "x" else box(minx - 1, prev, maxx + 1, cut)
        pieces.append(geom.intersection(slab))
        prev = cut
    last = box(prev, miny - 1, maxx + 1, maxy + 1) if axis == "x" else box(minx - 1, prev, maxx + 1, maxy + 1)
    pieces.append(geom.intersection(last))
    # keep polygonal parts only (slab cuts can leave stray lines/points)
    return [shapely.union_all(geo.polygons_of(pc)) if geo.polygons_of(pc) else Polygon() for pc in pieces]


def _shared_length(a, b) -> float:
    inter = a.boundary.intersection(b.boundary) if not a.is_empty and not b.is_empty else None
    return inter.length if inter is not None and not inter.is_empty else 0.0


def _merge_fragments(units: list[Polygon], fragments: list[Polygon]) -> list[Polygon]:
    units = list(units)
    for frag in sorted(fragments, key=lambda g: -g.area):
        best, best_len = None, 0.0
        for i, u in enumerate(units):
            L = _shared_length(frag, u)
            if L > best_len:
                best, best_len = i, L
        if best is not None:
            merged = shapely.union(units[best], frag)
            parts = geo.polygons_of(merged)
            units[best] = max(parts, key=lambda p: p.area) if parts else units[best]
    return units


def _door(unit: Polygon, passage) -> tuple[tuple[float, float], tuple[float, float]] | None:
    """Door point on the longest straight edge shared with the passage, plus the normal into it."""
    inter = unit.boundary.intersection(passage.boundary)
    if inter.is_empty:
        return None
    segs = []
    if inter.geom_type in ("LineString", "MultiLineString", "GeometryCollection"):
        merged = linemerge([g for g in getattr(inter, "geoms", [inter]) if g.geom_type == "LineString"]) if (
            inter.geom_type != "LineString"
        ) else inter
        for ln in getattr(merged, "geoms", [merged]):
            if ln.geom_type != "LineString":
                continue
            cs = list(ln.coords)
            for i in range(len(cs) - 1):
                segs.append((cs[i], cs[i + 1]))
    if not segs:
        return None
    a, b = max(segs, key=lambda s: math.dist(*s))
    L = math.dist(a, b)
    if L < DOOR_CLEAR:
        return None
    mid = ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2)
    dx, dy = (b[0] - a[0]) / L, (b[1] - a[1]) / L
    for nx, ny in ((dy, -dx), (-dy, dx)):
        probe = SPoint(mid[0] + nx * 0.3, mid[1] + ny * 0.3)
        if passage.buffer(1e-6).contains(probe):
            return (round(mid[0], 3), round(mid[1], 3)), (round(nx, 6), round(ny, 6))
    return None


def _split_by_sides(plate: Plate, flats, n: int) -> list:
    """Façade-band plates (triangle): each long façade owns the band between it and the lobby (split at
    the corner bisectors); each band is cut perpendicular to its façade, so every flat has one straight
    façade and one straight lobby edge."""
    from shapely import affinity

    from .plate import _tri_vertices, tri_incenter

    verts = _tri_vertices(plate.params)
    ic = tri_incenter(plate.params)
    sides = []
    for i in range(3):
        a, b = verts[i], verts[(i + 1) % 3]
        sector = Polygon([ic, a, b]).buffer(0)
        band = flats.intersection(sector)
        if band.area > 1.0:
            sides.append((a, b, band))
    total = sum(sd[2].area for sd in sides)
    counts = [max(1, int(round(n * sd[2].area / total))) for sd in sides]
    while sum(counts) > n and max(counts) > 1:
        counts[counts.index(max(counts))] -= 1
    while sum(counts) < n:
        counts[counts.index(min(counts))] += 1
    out = []
    for (a, b, band), k in zip(sides, counts, strict=True):
        ang = math.degrees(math.atan2(b[1] - a[1], b[0] - a[0]))
        rot = affinity.rotate(band, -ang, origin=(0, 0))
        for piece in _cut_equal(rot, k, "x"):
            if not piece.is_empty:
                out.append(affinity.rotate(piece, ang, origin=(0, 0)))
    return out


def split_units(plate: Plate, n: int | None = None, keep_programmes: list[Unit] | None = None) -> list[Unit]:
    flats = zones_geom(plate, "flats")
    passage = zones_geom(plate, "passage")
    core = zones_geom(plate, "core")
    access = shapely.union(passage, core)
    n = int(n or plate.params.get("units", 6))
    if flats.is_empty or n <= 0:
        return []
    if plate.shape.value == "triangle":
        return _finish_units(plate, _split_by_sides(plate, flats, n), [], flats, access, keep_programmes)
    # core line: horizontal through the core centre
    cy = core.centroid.y if not core.is_empty else 0.0
    minx, miny, maxx, maxy = flats.bounds
    upper = flats.intersection(box(minx - 1, cy, maxx + 1, maxy + 1))
    lower = flats.intersection(box(minx - 1, miny - 1, maxx + 1, cy))
    halves = [h for h in (upper, lower) if h.area > 1.0]
    total = sum(h.area for h in halves)
    counts = [max(1, int(round(n * h.area / total))) for h in halves]
    while sum(counts) > n and max(counts) > 1:
        counts[counts.index(max(counts))] -= 1
    while sum(counts) < n:
        counts[counts.index(min(counts))] += 1
    raw: list[Polygon] = []
    fragments: list[Polygon] = []
    for h, k in zip(halves, counts, strict=True):
        touch = h.boundary.intersection(access.buffer(0.01))
        contact = None
        if not touch.is_empty and touch.length > 0:
            tb = touch.bounds
            contact = (tb[0], tb[2])
        pieces = _cut_equal(h, k, "x", contact)
        if k > 1:
            outer = outline_ring_free(plate)
            fronts = [p.boundary.intersection(outer).length if not p.is_empty else 0.0 for p in pieces]
            if min(fronts) < MIN_FRONTAGE and sum(fronts) >= MIN_FRONTAGE * k:
                pieces = _cut_equal(h, k, "x", contact,
                                    measure=lambda g, o=outer: 0.0 if g.is_empty else g.boundary.intersection(o).length)
        for piece in pieces:
            parts = sorted(geo.polygons_of(piece), key=lambda p: -p.area)
            if not parts:
                continue
            raw.append(parts[0])
            fragments.extend(parts[1:])
    return _finish_units(plate, raw, fragments, flats, access, keep_programmes)


def _finish_units(plate: Plate, raw: list, fragments: list, flats, access, keep_programmes) -> list[Unit]:
    parts = []
    for g in raw:
        ps = sorted(geo.polygons_of(g), key=lambda q: -q.area)
        if ps:
            parts.append(ps[0])
            fragments = list(fragments) + ps[1:]
    raw = parts
    raw = _merge_fragments(raw, [f for f in fragments if f.area > 1e-4])
    # order units clockwise from the top-left like the reference (APT-1..)
    cx0, cy0 = flats.centroid.x, flats.centroid.y
    raw.sort(key=lambda p: (-(math.atan2(p.centroid.y - cy0, p.centroid.x - cx0)) + math.pi * 0.75) % (2 * math.pi))
    outline = Polygon(plate.outline)
    units: list[Unit] = []
    old = {u.id: u for u in (keep_programmes or [])}
    for i, poly in enumerate(raw):
        poly = shapely.set_precision(poly, 0.001)
        flags = []
        door = _door(poly, access)
        if door is None:
            flags.append("no_core_access")
        if poly.area < MIN_UNIT_AREA:
            flags.append("small")
        if not outline.buffer(0.01).contains(poly):
            flags.append("outside_outline")
        uid = f"APT-{i + 1}"
        u = Unit(
            id=uid,
            polygon=[(round(x, 3), round(y, 3)) for x, y in geo.ring_of(poly)],
            area=round(poly.area, 2),
            core_link=door[0] if door else None,
            core_link_normal=door[1] if door else None,
            flags=flags,
        )
        if uid in old and old[uid].programme:
            u.programme = old[uid].programme
        else:
            fac = facade_length(plate, u)
            has_shaft = not zones_geom(plate, "shaft").is_empty and poly.distance(zones_geom(plate, "shaft")) < 0.05
            u.programme = pick_preset(poly.area * CARPET_RATIO, fac, has_shaft)
        units.append(u)
    return units


def check_units(plate: Plate, units: list[Unit]) -> list[Unit]:
    """Re-flag units (e.g. after an import or a manual edit) without changing geometry."""
    outline = Polygon(plate.outline)
    access = shapely.union(zones_geom(plate, "passage"), zones_geom(plate, "core"))
    for u in units:
        poly = Polygon(u.polygon)
        flags = [f for f in u.flags if f not in ("no_core_access", "outside_outline", "small")]
        door = _door(poly, access)
        u.core_link, u.core_link_normal = (door[0], door[1]) if door else (None, None)
        if door is None:
            flags.append("no_core_access")
        if not outline.buffer(0.01).contains(poly):
            flags.append("outside_outline")
        if poly.area < MIN_UNIT_AREA:
            flags.append("small")
        u.flags = flags
    return units


def programme_from_rooms(name: str, rooms: list[dict], links: list[list[str]], entry: str | None = None) -> Programme:
    """Build and schema-check a programme (used for T8 'brief in words -> room list')."""
    prs = [ProgrammeRoom(**r) for r in rooms]
    ids = [r.id for r in prs]
    if len(set(ids)) != len(ids):
        raise ValueError("room ids must be unique")
    for r in prs:
        if r.type not in rules.ROOM_TYPES:
            raise ValueError(f"unknown room type {r.type!r}")
        if r.target_area <= 0:
            raise ValueError(f"{r.id}: target area must be positive")
    for a, b in links:
        if a not in ids or b not in ids:
            raise ValueError(f"link {a}-{b} names an unknown room")
    entry = entry or next((r.id for r in prs if r.type in ("foyer", "living")), ids[0])
    if entry not in ids:
        raise ValueError("entry room not in programme")
    bhk = sum(1 for r in prs if r.type in ("master", "bedroom"))
    total = sum(r.target_area for r in prs)
    return Programme(
        id=f"custom-{bhk}bhk",
        name=name,
        bhk=bhk,
        area_range=(round(total * 0.9, 1), round(total * 1.1, 1)),
        rooms=prs,
        links=[(a, b) for a, b in links],
        entry=entry,
    )


def facade_length(plate: Plate, unit: Unit) -> float:
    """Length of the unit boundary that is on the plate's outer face (excluding party/core walls)."""
    poly = Polygon(unit.polygon)
    outline = Polygon(plate.outline)
    shafts = zones_geom(plate, "shaft")
    total = 0.0
    ring = geo.ring_of(poly)
    for i in range(len(ring)):
        a, b = ring[i], ring[(i + 1) % len(ring)]
        L = math.dist(a, b)
        if L < 1e-6:
            continue
        mx, my = (a[0] + b[0]) / 2, (a[1] + b[1]) / 2
        nx, ny = (b[1] - a[1]) / L, -(b[0] - a[0]) / L
        probe = SPoint(mx + nx * 0.2, my + ny * 0.2)
        if not outline.contains(probe) or (not shafts.is_empty and shafts.contains(probe)):
            total += L
    return round(total, 2)


_ = LineString
