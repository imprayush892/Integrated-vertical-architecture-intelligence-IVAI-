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
from shapely import affinity
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
ROOM_DEPTH = 4.5  # a habitable room needs this much depth behind its window wall
RASTER_LOSS = 0.9  # façade the room grid loses on a flat that is not square to the plate axes (0.5 m cells)


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


def _door(unit: Polygon, passage, slanted: bool = False) -> tuple[tuple[float, float], tuple[float, float]] | None:
    """Door point on the longest straight edge shared with the passage, plus the normal into it.

    ``slanted`` (triangle plates): edges off the axes rarely coincide to the last digit after rounding, so
    the shared stretch is taken within 2 cm and nearly collinear pieces are merged."""
    inter = unit.boundary.intersection(passage.boundary)
    if slanted and (inter.is_empty or inter.length < DOOR_CLEAR):
        near = unit.boundary.intersection(passage.buffer(0.02))
        if not near.is_empty and near.length >= DOOR_CLEAR:
            inter = near
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
            start = cs[0]
            for i in range(1, len(cs)):
                if slanted and i < len(cs) - 1:  # merge nearly collinear runs (rounded slanted vertices)
                    d1 = math.atan2(cs[i][1] - start[1], cs[i][0] - start[0])
                    d2 = math.atan2(cs[i + 1][1] - cs[i][1], cs[i + 1][0] - cs[i][0])
                    if abs((d2 - d1 + math.pi) % (2 * math.pi) - math.pi) < 0.03:
                        continue
                segs.append((start, cs[i]))
                start = cs[i]
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


TRI_ACCESS_RUN = 2.4  # lobby frontage each triangle flat keeps for its entry (door + foyer)


def _tri_cuts(band, k: int, contact: tuple[float, float] | None, run: float, lb: float = 0.0,
              la: float = 0.0) -> list[float]:
    """Cut positions along x for a façade band (façade along x at the bottom), each flat keeping ``run`` of
    the lobby edge ``contact`` (an x-range) when the band is long enough for that; the shaft on each cut
    takes ``lb`` of it from the flat before the cut and ``la`` from the flat after.

    Flats get equal shares of *deep* façade (façade with a room's depth of flat behind it), so the flat that
    holds a tapering corner wedge is not shortchanged on window wall; equal area if the band has none."""
    minx, miny, maxx, maxy = band.bounds
    deep = band.intersection(affinity.translate(band, 0.0, -ROOM_DEPTH))
    face = band.boundary.intersection(box(minx - 1, miny - 0.01, maxx + 1, miny + 0.01))
    face = face.intersection(deep.buffer(0.05)) if not deep.is_empty else face

    def measure(x: float) -> float:
        return face.intersection(box(minx - 1, miny - 1, x, maxy + 1)).length

    total = face.length
    if total < 1.0:
        def measure(x: float) -> float:
            return band.intersection(box(minx - 1, miny - 1, x, maxy + 1)).area

        total = band.area
    cuts: list[float] = []
    prev = minx
    for i in range(1, k):
        target = total * i / k
        lo, hi = prev, maxx
        for _ in range(40):
            mid = (lo + hi) / 2
            if measure(mid) < target:
                lo = mid
            else:
                hi = mid
        cut = (lo + hi) / 2
        if contact is not None:
            c0, c1 = contact
            lo_ok = (max(c0, prev) + la if cuts else c0) + run + lb
            hi_ok = c1 - (run + la) - (run + la + lb) * (k - i - 1)
            if lo_ok <= hi_ok:
                cut = min(max(cut, lo_ok), hi_ok)
        cut = round(cut / geo.SNAP) * geo.SNAP
        cuts.append(cut)
        prev = cut
    return cuts


def tri_runs(params: dict, flats, n: int, passage, run: float = TRI_ACCESS_RUN,
             lead: tuple[float, float] = (0.0, 0.0)) -> list[dict]:
    """Façade runs of a triangle plate and where each is cut into flats.

    Each façade owns the band between it and the lobby (``plate.tri_sectors``: corners go to one band or are
    split); a façade core splits the base band into two runs. Flats per run follow area; each run is cut
    perpendicular to its façade into equal shares of deep façade (``cuts`` are x positions in the frame where
    the façade runs along +x, ``ang`` degrees; ``lb``/``la`` the shaft shares either side of a cut). Shared
    by plate.py (air shafts sit on the cuts) and the unit split, so both cut in the same places."""
    from .plate import tri_layout, tri_sectors

    lay = tri_layout(params)
    runs = []
    for sec in tri_sectors(params, lay):
        a, b = sec["a"], sec["b"]
        # a shaft straddles each cut, its larger share in the flat away from a bisector-split corner (the
        # flat at that corner has the least lobby frontage to spare)
        after = lay["own"][sec["i"]] == "split" and lay["own"][(sec["i"] + 1) % 3] != "split"
        lb, la = (lead[1], lead[0]) if after else lead
        for part in geo.polygons_of(flats.intersection(sec["poly"])):
            if part.area > 1.0:
                runs.append({"a": a, "b": b, "band": part, "lb": lb, "la": la,
                             "ang": math.degrees(math.atan2(b[1] - a[1], b[0] - a[0]))})
    if not runs:
        return []
    total = sum(r["band"].area for r in runs)
    counts = [max(1, int(round(n * r["band"].area / total))) for r in runs]
    while sum(counts) > n and max(counts) > 1:
        j = max((k for k in range(len(runs)) if counts[k] > 1), key=lambda k: runs[k]["band"].area / (counts[k] - 1))
        counts[j] -= 1
    while sum(counts) < n:
        j = max(range(len(runs)), key=lambda k: runs[k]["band"].area / counts[k])
        counts[j] += 1
    for r, k in zip(runs, counts, strict=True):
        rot = affinity.rotate(r["band"], -r["ang"], origin=(0, 0))
        contact = None
        if passage is not None and not passage.is_empty:
            touch = rot.boundary.intersection(affinity.rotate(passage, -r["ang"], origin=(0, 0)).buffer(0.01))
            if not touch.is_empty and touch.length > 0:
                tb = touch.bounds
                contact = (tb[0], tb[2])
        r["k"] = k
        r["rot"] = rot
        r["cuts"] = _tri_cuts(rot, k, contact, run, r["lb"], r["la"])
    return runs


def _split_by_sides(plate: Plate, flats, n: int) -> list:
    """Façade-band plates (triangle): cut every façade run (``tri_runs``) perpendicular to its façade, so
    every flat has one straight façade and one straight lobby edge. Cuts go through the plate's air shafts
    (computed on flats + shafts, the same geometry plate.py cut), so the shafts sit between two flats."""
    from .plate import tri_access_run

    shafts = zones_geom(plate, "shaft")
    whole = shapely.union(flats, shafts) if not shafts.is_empty else flats
    passage = zones_geom(plate, "passage")
    out = []
    for r in tri_runs(plate.params, whole, n, passage, *tri_access_run(plate.params)):
        rot_sh = affinity.rotate(shafts, -r["ang"], origin=(0, 0)) if not shafts.is_empty else None
        cuts = list(r["cuts"])
        if rot_sh is not None:  # snap each cut onto the line it was drawn through its shaft
            sides = [g.bounds[0] + r["lb"] for g in geo.polygons_of(rot_sh)
                     if g.intersects(r["rot"].buffer(0.05)) and abs(g.bounds[2] - g.bounds[0] - r["lb"] - r["la"]) < 0.05]
            for j, c in enumerate(cuts):
                near = min(sides, key=lambda x: abs(x - c), default=None)
                if near is not None and abs(near - c) < 0.75:
                    cuts[j] = near
        minx, miny, maxx, maxy = r["rot"].bounds
        edges = [minx - 1, *cuts, maxx + 1]
        rot_flats = affinity.rotate(flats, -r["ang"], origin=(0, 0))
        for x0, x1 in zip(edges, edges[1:], strict=False):
            piece = r["rot"].intersection(box(x0, miny - 1, x1, maxy + 1)).intersection(rot_flats)
            parts = geo.polygons_of(piece)
            if parts:
                out.append(affinity.rotate(shapely.union_all(parts), r["ang"], origin=(0, 0)))
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
        return _finish_units(plate, _split_by_sides(plate, flats, n), [], flats, access, keep_programmes,
                             lobby=passage)
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


def _finish_units(plate: Plate, raw: list, fragments: list, flats, access, keep_programmes,
                  lobby=None) -> list[Unit]:
    """``lobby``: when given, the entry door goes on the lobby if the flat reaches it, not on a core wall."""
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
        if poly.geom_type != "Polygon":  # snapping can pinch off a hair-thin neck: keep the body
            poly = max(geo.polygons_of(poly), key=lambda q: q.area)
        flags = []
        door = (_door(poly, lobby, slanted=True) or _door(poly, access, slanted=True)) if lobby is not None \
            else _door(poly, access)
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
        elif plate.shape.value == "triangle":
            u.programme = pick_preset(poly.area * CARPET_RATIO, tri_frontage(plate, u))
        else:
            fac = facade_length(plate, u)
            has_shaft = not zones_geom(plate, "shaft").is_empty and poly.distance(zones_geom(plate, "shaft")) < 0.05
            u.programme = pick_preset(poly.area * CARPET_RATIO, fac, has_shaft)
        units.append(u)
    return units


def check_units(plate: Plate, units: list[Unit]) -> list[Unit]:
    """Re-flag units (e.g. after an import or a manual edit) without changing geometry."""
    outline = Polygon(plate.outline)
    lobby = zones_geom(plate, "passage")
    access = shapely.union(lobby, zones_geom(plate, "core"))
    tri = plate.shape.value == "triangle"
    for u in units:
        poly = Polygon(u.polygon)
        flags = [f for f in u.flags if f not in ("no_core_access", "outside_outline", "small")]
        door = (_door(poly, lobby, slanted=True) or _door(poly, access, slanted=True)) if tri else _door(poly, access)
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


def _facade_edges(plate: Plate, unit: Unit, shafts: bool = True) -> list[tuple[float, float, tuple, tuple]]:
    """(length, direction in degrees, a, b) of each unit edge on the plate's outer face (or on an air shaft)."""
    poly = Polygon(unit.polygon)
    outline = Polygon(plate.outline)
    sh = zones_geom(plate, "shaft") if shafts else Polygon()
    out = []
    ring = geo.ring_of(poly)
    for i in range(len(ring)):
        a, b = ring[i], ring[(i + 1) % len(ring)]
        L = math.dist(a, b)
        if L < 1e-6:
            continue
        mx, my = (a[0] + b[0]) / 2, (a[1] + b[1]) / 2
        nx, ny = (b[1] - a[1]) / L, -(b[0] - a[0]) / L
        probe = SPoint(mx + nx * 0.2, my + ny * 0.2)
        if not outline.contains(probe) or (not sh.is_empty and sh.contains(probe)):
            out.append((L, math.degrees(math.atan2(b[1] - a[1], b[0] - a[0])), a, b))
    return out


def straight_frontage(plate: Plate, unit: Unit, min_depth: float = ROOM_DEPTH) -> float:
    """Outer façade the room solver can glaze: edges square to the flat's longest façade edge (the solver
    works in that frame). On rectilinear plates this is the outer façade; on a triangle it leaves out the
    second, 60°-slanted façade of a corner flat. Air-shaft edges ventilate but do not light rooms. Only the
    stretches with ``min_depth`` of flat behind them count (the tapering wedge at a sharp corner takes a
    toilet or a balcony, not a bedroom)."""
    edges = _facade_edges(plate, unit, shafts=False)
    if not edges:
        return 0.0
    ref = max(edges)[1]
    poly = Polygon(unit.polygon).buffer(0)
    total = 0.0
    for L, ang, a, b in edges:
        d = (ang - ref) % 90.0
        if min(d, 90.0 - d) >= 2.0:
            continue
        if min_depth <= 0:
            total += L
            continue
        nx, ny = -(b[1] - a[1]) / L, (b[0] - a[0]) / L  # left normal; flip to point into the flat
        mid = ((a[0] + b[0]) / 2 + nx * 0.05, (a[1] + b[1]) / 2 + ny * 0.05)
        if not poly.contains(SPoint(mid)):
            nx, ny = -nx, -ny
        deep = poly.intersection(affinity.translate(poly, -nx * min_depth, -ny * min_depth))
        total += LineString([a, b]).intersection(deep.buffer(0.05)).length
    return round(total, 2)


def tri_frontage(plate: Plate, unit: Unit) -> float:
    """Frontage a triangle flat's programme is picked on: straight façade with room depth behind it, less what
    the room grid loses on a slanted flat. Shafts give no credit here: on a triangle every flat already has
    one (party-wall or corner shaft) and the window rooms still need the full run."""
    fac = straight_frontage(plate, unit)
    edges = _facade_edges(plate, unit, shafts=False)
    if edges:
        ang = max(edges)[1] % 90.0
        if min(ang, 90.0 - ang) > 0.01:
            fac -= RASTER_LOSS
    return max(0.0, round(fac, 2))


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
