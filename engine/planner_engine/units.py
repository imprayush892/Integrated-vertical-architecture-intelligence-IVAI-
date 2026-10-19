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
# v5: the side a window room needs along its façade (furniture minimum + a wall), m; a room's window wall lies on ONE
# straight façade run, so the runs are packed room by room instead of summing the façade
WINDOW_SIDE = {"living": 3.3, "master": 3.1, "bedroom": 2.9, "kitchen": 2.3}
RUN_INSET = 0.35  # m a façade run loses to the walls at its ends
# v5: 130-140 m2 carpet is where the 3BHK and 4BHK market bands meet; there the brief stays a premium 3BHK (servant room,
# study, dress rooms): the measured 4BHK layouts of those flats were far worse and some found no layout in 30 s
BHK_MIN_CARPET = {4: 140.0}
BAND_MARGIN = 1.05  # the carpet is an estimate (gross x 0.8): step up a BHK only 5 % clear of the band's floor


def window_rooms_fit(runs: list[float], bhk: int) -> bool:
    """True when the window rooms of a ``bhk`` flat (living, kitchen, master, bhk - 1 bedrooms) can each take a stretch of
    one of the straight façade ``runs`` (first-fit decreasing)."""
    need = [WINDOW_SIDE["living"], WINDOW_SIDE["kitchen"], WINDOW_SIDE["master"]] + [WINDOW_SIDE["bedroom"]] * max(0, bhk - 1)
    free = sorted((max(0.0, r - RUN_INSET) for r in runs), reverse=True)
    for w in sorted(need, reverse=True):
        for i, f in enumerate(free):
            if f + 1e-6 >= w:
                free[i] = f - w
                break
        else:
            return False
    return True


def pick_preset(carpet_estimate: float, facade: float | None = None, shaft: bool = False,
                runs: list[float] | None = None) -> Programme:
    """Largest preset whose area range and façade need both fit the flat (v5: with ``runs``, the straight façade runs,
    every window room must find its own stretch of one run)."""
    ps = sorted(presets().values(), key=lambda p: p.bhk)
    best = ps[0]
    for p in ps:
        lo, _hi = p.area_range
        need = FRONTAGE_PER_BHK.get(p.bhk, 7.5 + 3.5 * (p.bhk - 1)) - (1.5 if shaft else 0.0)
        lo = max(lo * BAND_MARGIN, BHK_MIN_CARPET.get(p.bhk, 0.0)) if p.bhk > 1 else lo
        if carpet_estimate >= lo and (facade is None or facade >= need) and (runs is None or window_rooms_fit(runs, p.bhk)):
            best = p
    return best


def facade_runs(plate: Plate, unit: Unit) -> list[float]:
    """v5: lengths of the unit's straight runs of outer façade (collinear touching edges merged; air shafts excluded)."""
    edges = _facade_edges(plate, unit, shafts=False)
    runs: list[list] = []
    for L, ang, a, b in edges:
        if runs:
            L0, ang0, a0, b0 = runs[-1]
            if abs(((ang - ang0 + 180) % 360) - 180) < 1.0 and math.dist(b0, a) < 0.02:
                runs[-1] = [L0 + L, ang0, a0, b]
                continue
        runs.append([L, ang, a, b])
    if len(runs) > 1:  # the ring may start mid-run
        L0, ang0, a0, b0 = runs[0]
        L1, ang1, a1, b1 = runs[-1]
        if abs(((ang0 - ang1 + 180) % 360) - 180) < 1.0 and math.dist(b1, a0) < 0.02:
            runs[0] = [L0 + L1, ang0, a1, b0]
            runs.pop()
    return [round(r[0], 2) for r in runs]


# ------------------------------------------------------------------------------------------ v5 programme sizing
# Generator v5 (docs/quality/generator-v5.md): the brief is sized to the flat's carpet. The BHK follows the Mumbai market
# bands (carpet m2) and the façade; the floor the base preset leaves over goes into the rooms an architect adds (attached
# toilets, a dress room, a servant room with WC by the kitchen, a study, a pooja room, a store), not into bloated rooms.
MARKET_BANDS = {1: (35.0, 50.0), 2: (55.0, 80.0), 3: (85.0, 120.0), 4: (130.0, 180.0)}
BAND_STORES = 2  # v5: at most this many extra stores in a flat's dark band ...
BAND_STORE_EVERY = 6.0  # ... one per this many m2 of dark floor the brief's stores leave
BAND_STORE_M2 = 4.0
ALL_ATTACHED_2BHK = 70.0  # m2 carpet: above this every bedroom of a 2BHK has its own toilet (3BHK+: always)
SERVANT_CARPET = 110.0  # m2 carpet: a servant room with its WC by the kitchen (3BHK+)
INTERIOR_SHARE = 0.92  # interior (inside the outer / party walls) / flat gross
FILL_SHARE = 0.94 / 1.08  # brief total / interior: rooms may grow ~8 % past target before the solver's fill
# base rooms grow towards the approved plans' p75 when the extras are all in and floor is still left
GROW_TO = {"living": 24.0, "dining": 11.0, "master": 15.0, "bedroom": 12.5, "kitchen": 9.5}


def _room(rid: str, rtype: str, name: str, area: float, optional: bool = True, window: bool = False) -> ProgrammeRoom:
    wet = rtype in ("toilet", "bath", "wc", "utility")
    return ProgrammeRoom(id=rid, type=rtype, name=name, target_area=area, min_area=0.0, needs_window=window, wet=wet,
                         optional=optional)


DARK_DEPTH = 8.0  # m: a daylit room's depth from its window wall (v5)


def dark_area(ug) -> float:
    """v5: m2 of a flat's floor (``solver.UnitGrid``) farther than DARK_DEPTH from every façade edge: where no window
    room can reach (a side-core flat's band along the lobby), so only service rooms - or leftover floor - go there."""
    lines = [LineString([(e.a, e.pos), (e.b, e.pos)] if e.horizontal else [(e.pos, e.a), (e.pos, e.b)])
             for e in ug.edges if e.kind == "facade"]
    if not lines:
        return 0.0
    free = box(0, 0, ug.Wg, ug.Hg).difference(shapely.union_all([box(x0, y0, x1, y1) for x0, y0, x1, y1 in ug.blocked]))
    return free.difference(shapely.union_all(lines).buffer(DARK_DEPTH * 10)).area / 100.0


def _interior(plate: Plate, unit: Unit) -> tuple[float | None, float]:
    """Net interior of a flat (inside its outer / party walls) as the room solver sees it, and its dark area."""
    try:
        from .solver import unit_grid

        ug = unit_grid(plate, unit)
        a = ug.interior.area
        return (a if a > 1.0 else None), dark_area(ug)
    except Exception:  # noqa: BLE001 - a flat the grid cannot read is sized from its gross area
        return None, 0.0


def size_programme(carpet: float, facade: float | None = None, shaft: bool = False,
                   interior: float | None = None, runs: list[float] | None = None, bhk: int | None = None,
                   dark: float = 0.0) -> Programme:
    """v5 brief for a flat of ``carpet`` m2 (RERA estimate) with ``facade`` m of window wall: the market-band preset
    (``pick_preset``), every bedroom with its own toilet in a 3BHK+ (2BHK above 70 m2) and the common toilet as a powder
    room off the living, then - while the floor allows - dress, servant room + WC (3BHK+ above 110 m2), study, pooja,
    store and a second dress room; what is still left grows the main rooms towards their p75 sizes."""
    base = pick_preset(carpet, facade, shaft, runs)
    if bhk is not None:  # a forced BHK (the fix ladder's smaller programme)
        base = next((q for q in presets().values() if q.bhk == bhk), base)
    p = base.model_copy(deep=True)
    rooms = {r.id: r for r in p.rooms}
    links = [tuple(l) for l in p.links]
    usable = (interior if interior else carpet / CARPET_RATIO * INTERIOR_SHARE) * FILL_SHARE

    def total() -> float:
        return sum(r.target_area for r in rooms.values())

    def add(room: ProgrammeRoom, link: tuple[str, str] | None, force: bool = False) -> bool:
        if room.id in rooms or (not force and total() + room.target_area > usable):
            return False
        rooms[room.id] = room
        if link:
            links.append(link)
        return True

    beds = [r for r in p.rooms if r.type in ("master", "bedroom")]
    attached = {a if rooms[b].type in ("toilet", "bath") else b for a, b in links
                if {rooms[a].type, rooms[b].type} & {"toilet", "bath"} and {rooms[a].type, rooms[b].type} & {"master", "bedroom"}}
    all_att = p.bhk >= 3 or (p.bhk == 2 and carpet > ALL_ATTACHED_2BHK)
    if all_att:
        for b in beds:
            if b.id not in attached:
                add(_room(f"toilet_{b.id}", "toilet", f"Toilet ({b.name.lower()})", 3.3, optional=False), (b.id, f"toilet_{b.id}"),
                    force=True)
        # the common toilet becomes a powder room by the living room (guests never walk into the private zone)
        tc = rooms.get("toilet_c")
        if tc is not None:
            rooms["toilet_c"] = _room("toilet_c", "wc", "Powder", 2.0, optional=False)
            links = [l for l in links if "toilet_c" not in l] + [("living", "toilet_c")]
    hub = "passage" if "passage" in rooms else "living"
    win_need = sum(max(2.6, 1.8 if r.type == "kitchen" else 2.4) for r in rooms.values() if r.needs_window)
    master = next((b for b in beds if b.type == "master"), beds[0] if beds else None)
    extras: list[tuple[ProgrammeRoom, tuple[str, str] | None]] = []
    if master is not None:
        extras.append((_room(f"dress_{master.id}", "dress", "Dress", 4.0), (master.id, f"dress_{master.id}")))
    if p.bhk >= 3 and carpet > SERVANT_CARPET and "kitchen" in rooms:
        add(_room("servant", "store", "Servant room", 6.0), ("kitchen", "servant"), force=True)  # drawn in a pocket too
    if p.bhk >= 2 and (facade is None or facade - win_need >= 2.8):
        extras.append((_room("study", "study", "Study", 8.0, window=True), (hub, "study")))
    extras.append((_room("pooja", "pooja", "Pooja", 2.0), (hub, "pooja")))
    if p.bhk >= 2:  # a store of the brief: drafting only draws it where the layout leaves a dark pocket (POCKET_STORES)
        add(_room("store", "store", "Store", 3.5), ("kitchen" if "utility" not in rooms else "utility", "store"), force=True)
    # v5: a dark band along the lobby (``dark`` m2 beyond DARK_DEPTH of the façade) is the flat's service band: past what
    # the servant room and store take, it holds more store rooms with a door (linen, luggage), not leftover floor
    band = dark - sum(r.target_area for r in rooms.values() if r.type == "store")
    for k in range(min(BAND_STORES, int(band // BAND_STORE_EVERY))):
        add(_room(f"store_b{k + 1}", "store", "Store", BAND_STORE_M2), (hub, f"store_b{k + 1}"), force=True)
    for b in beds:
        if master is not None and b.id != master.id and p.bhk >= 3:
            extras.append((_room(f"dress_{b.id}", "dress", "Dress", 3.5), (b.id, f"dress_{b.id}")))
    def grow(limit: dict[str, float]) -> None:
        for _ in range(3):
            left = usable - total()
            grow = [r for r in rooms.values() if r.type in limit and r.target_area < limit[r.type]]
            if left < 1.0 or not grow:
                break
            room_sum = sum(r.target_area for r in grow)
            for r in grow:
                r.target_area = round(min(limit[r.type], r.target_area * (1 + left / room_sum)), 1)

    if extras and extras[0][0].type == "dress":
        add(*extras.pop(0))
    grow(GROW_TO)  # the main rooms first grow to the usual (p75) sizes
    for room, link in extras:
        add(room, link)
    order = [r.id for r in p.rooms if r.id in rooms] + [rid for rid in rooms if rid not in {r.id for r in p.rooms}]
    p.rooms = [rooms[rid] for rid in order]
    p.links = [tuple(l) for l in links if l[0] in rooms and l[1] in rooms]
    p.name = f"{p.bhk} BHK (brief for {carpet:.0f} m² carpet)"
    return p


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


def _slivers(pieces: list, access) -> bool:
    """True when a cut left a piece too small to be a flat, or one that cannot reach the passage/core."""
    for pc in pieces:
        if pc.is_empty or pc.area < MIN_UNIT_AREA:
            return True
        if _door(max(geo.polygons_of(pc), key=lambda q: q.area), access) is None:
            return True
    return False


def _cut_wings(h, k: int, access, outer) -> list | None:
    """Cut a half whose flat band runs past both ends of the passage (T/L wings): each overhanging end is one
    flat entered at the passage end, and the remaining ``k - ends`` flats share the band along the passage,
    equal by area (equal by façade when the cut leaves a flat too narrow to light two rooms). Returns None
    when this does not give a valid cut, so the caller keeps its own."""
    touch = h.boundary.intersection(access)
    if touch.is_empty or touch.length <= 0:
        return None
    t0, _, t1, _ = touch.bounds
    t0, t1 = round(t0 / geo.SNAP) * geo.SNAP, round(t1 / geo.SNAP) * geo.SNAP
    minx, miny, maxx, maxy = h.bounds
    ends = [e for e, over in (("l", minx < t0 - ACCESS_RUN), ("r", maxx > t1 + ACCESS_RUN)) if over]
    mid_k = k - len(ends)
    if not ends or mid_k < 1:
        return None
    lo = t0 if "l" in ends else minx - 1
    hi = t1 if "r" in ends else maxx + 1
    mid = h.intersection(box(lo, miny - 1, hi, maxy + 1))
    mids = _cut_equal(mid, mid_k, "x", (max(t0, lo), min(t1, hi)))
    if mid_k > 1:
        fr = [p.boundary.intersection(outer).length if not p.is_empty else 0.0 for p in mids]
        if min(fr) < MIN_FRONTAGE and sum(fr) >= MIN_FRONTAGE * mid_k:
            mids = _cut_equal(mid, mid_k, "x", (max(t0, lo), min(t1, hi)),
                              measure=lambda g, o=outer: 0.0 if g.is_empty else g.boundary.intersection(o).length)
    out = []
    if "l" in ends:
        out.append(h.intersection(box(minx - 1, miny - 1, lo, maxy + 1)))
    out += mids
    if "r" in ends:
        out.append(h.intersection(box(hi, miny - 1, maxx + 1, maxy + 1)))
    out = [shapely.union_all(geo.polygons_of(pc)) if geo.polygons_of(pc) else Polygon() for pc in out]
    return None if _slivers(out, access) else out


def split_pieces(flats, passage, core, n: int, outer) -> tuple[list, list]:
    """Cut the flat zone of a core plate into ``n`` flats: equal-area cuts either side of the core line, balanced by
    façade (``outer``) when a cut leaves a flat too narrow to light two rooms, wings cut at the passage ends.
    Returns (pieces, fragments). Shared by plate.py (chowks and party-wall shafts sit on these cuts) and
    ``split_units``, so both cut in the same places."""
    access = shapely.union(passage, core)
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
            fronts = [p.boundary.intersection(outer).length if not p.is_empty else 0.0 for p in pieces]
            if min(fronts) < MIN_FRONTAGE and sum(fronts) >= MIN_FRONTAGE * k:
                pieces = _cut_equal(h, k, "x", contact,
                                    measure=lambda g, o=outer: 0.0 if g.is_empty else g.boundary.intersection(o).length)
            if _slivers(pieces, access):  # clamped cuts crowded into a sliver: give the wings their own flats
                pieces = _cut_wings(h, k, access, outer) or pieces
        for piece in pieces:
            parts = sorted(geo.polygons_of(piece), key=lambda p: -p.area)
            if not parts:
                continue
            raw.append(parts[0])
            fragments.extend(parts[1:])
    return raw, fragments


def cut_geometry(plate: Plate):
    """(flat zone as it was cut, outer face the cut balances on): the flats plus the shafts and chowks that plate.py
    carved out of them after placing them on the cuts (v3), so the cuts land where the plate drew them."""
    from .plate import base_outline

    flats = zones_geom(plate, "flats")
    shafts = zones_geom(plate, "shaft")
    outline0 = base_outline(plate)
    chowks = outline0.difference(Polygon(plate.outline).buffer(0))
    parts = [flats] + ([shafts] if not shafts.is_empty else []) + ([chowks] if chowks.area > 1e-3 else [])
    whole = shapely.union_all(parts) if len(parts) > 1 else flats
    ring = outline0.exterior
    outer = ring.difference(shafts.buffer(0.05)) if not shafts.is_empty else ring
    return whole, outer


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
    whole, outer = cut_geometry(plate)
    raw0, frag0 = split_pieces(whole, passage, core, n, outer)
    raw: list[Polygon] = []
    fragments: list[Polygon] = []
    for i, g in enumerate(raw0 + frag0):
        parts = sorted(geo.polygons_of(g.intersection(flats)), key=lambda q: -q.area)
        if not parts:
            continue
        if i < len(raw0):
            raw.append(parts[0])
            fragments.extend(parts[1:])
        else:
            fragments.extend(parts)
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
            u.programme = size_programme(poly.area * CARPET_RATIO, tri_frontage(plate, u))
        else:
            fac = facade_length(plate, u)
            has_shaft = not zones_geom(plate, "shaft").is_empty and poly.distance(zones_geom(plate, "shaft")) < 0.05
            interior, dark = _interior(plate, u)
            u.programme = size_programme(poly.area * CARPET_RATIO, fac, has_shaft, interior=interior,
                                         runs=facade_runs(plate, u), dark=dark)
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
