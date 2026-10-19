"""Stage 7b - pack rooms into the flat with OR-Tools CP-SAT.

Rooms are rectangles on a 10 cm integer grid inside the flat's *net* interior (the flat minus the outer
wall band). Constraints:

* NoOverlap2D between rooms and the blocked parts of the bounding box (non-rectangular flats);
* min width and area from the rules file (plus the internal-wall allowance, so the drawn room passes);
* links = a shared wall of at least door width;
* window rooms touch a façade edge for at least the run their 1/6 glazing rule needs;
* wet rooms touch the façade or an air shaft;
* the entry room touches the flat's door on the common passage;
* aspect-ratio limits.

Objective: closest to the target areas, fill the flat, keep optional rooms when they fit.
If infeasible, the same model is solved with assumption literals; the infeasible core is shrunk to a
minimal set and reported as a conflict.
"""

from __future__ import annotations

import math
import os
import time
from dataclasses import dataclass, field

import shapely
from ortools.sat.python import cp_model
from shapely.geometry import Point as SPoint
from shapely.geometry import Polygon, box

from . import geo, rules
from .diagram import direction_cost, hub_door_cost, room_minimums, wants_foyer
from .models import Diagram, Plate, Unit
from .plate import zones_geom

G = 0.1  # grid, metres
EXT_WALL = 0.23
PARTY_WALL = 0.115
INT_WALL = 0.115
WIN_HEIGHT = 1.5  # sill 0.6 to head 2.1
TIME_CAP = 8.5  # CP-SAT wall-clock cap for one solve; layout.FLAT_BUDGET (9.5 s) bounds a whole flat incl. fix attempts
ASPECT_X10 = {"passage": 120, "foyer": 30, "balcony": 60, "utility": 40, "dress": 40, "toilet": 25, "bath": 25,
             "wc": 30, "kitchen": 25, "store": 40, "pooja": 25}  # max long/short side x10; default 20
CIRCULATION = {"foyer", "passage"}
SERVICE = {"toilet", "bath", "wc", "utility", "foyer", "passage", "balcony", "store", "pooja", "dress"}
DOOR_RUN = {"toilet": 9, "bath": 9, "wc": 9}  # cells of shared wall for a door (0.75 + wall) else 10

# ---- generator v2 (docs/quality/generator-v2.md)
# Furniture-aware minimum clear dimensions (short side m, net area m2), measured with the critic's furniture placer
# (quality.place_furniture) on rectangles with a door on one side: the smallest room where the full set fits
# (bed + wardrobe with clearances, sofa facing a TV, table for 4 with chair room, WC + basin + shower zone).
FURN_MIN = {"master": (3.0, 11.0), "bedroom": (2.8, 9.5), "living": (3.2, 12.0), "dining": (2.7, 7.5),
            "kitchen": (2.0, 6.0), "toilet": (1.5, 3.2), "bath": (1.5, 3.2), "wc": (1.0, 1.5), "study": (2.4, 7.5),
            "dress": (1.2, 2.2), "passage": (1.1, 0.0)}
# Size above which floor counts as wasted (net m2): min(OVERSIZE, 1.3 x target + 0.5), the critic's rule
# (quality.OVERSIZE = approved-plan p90 x ~1.3). Below the cap rooms may grow to fill the flat, above it they pay CAP_W.
OVERSIZE = {"master": 20.0, "bedroom": 16.0, "living": 32.0, "dining": 16.0, "kitchen": 11.0, "toilet": 5.5, "bath": 4.0,
            "wc": 2.5, "foyer": 5.5, "passage": 6.5, "utility": 5.0, "study": 10.0, "dress": 6.0, "pooja": 3.0}
# objective weights, per 0.01 m2 cell
OVER_W = {"living": 2, "dining": 3, "master": 3, "bedroom": 3, "study": 4, "kitchen": 6, "dress": 6, "balcony": 8,
          "toilet": 10, "bath": 10, "wc": 15, "utility": 10, "store": 10, "pooja": 10, "foyer": 30, "passage": 30}
UNDER_W = 15  # a room below its target
CAP_W = 100  # a room above its waste cap
# v5: floor above a room's cap beyond CAP_TIER cells (net) costs CAP_TIER_W more per cell, so the floor a too-large flat
# leaves over is spread over several rooms a little past their caps instead of bloating one (a 26 m2 dining room)
CAP_TIER = 180
CAP_TIER_W = 15
UNCOVERED_W = 120  # floor no room covers (becomes a leftover pocket)
PASSAGE_FIXED = 400  # having a passage / foyer at all (on top of OVER_W per cell)
CIRC_FREE = 0.05  # share of the floor passage + foyer may take at the light rate (approved plans p90 = 4.7 %)
CIRC_EXCESS_W = 100  # per cell of passage + foyer beyond that (priced like wasted floor)
DOOR_SLIDE = True  # v3: the solver places the front door along the lobby edge
DOOR_SLIDE_W = 0
HUB_PEN = 300  # per step down a room's hub preference list (dining > passage > living)
HUB_SPLIT = 12000  # each distinct space the private doors open onto (one compact hub wanted)
PRIVATE_DOOR = ("master", "bedroom", "study", "toilet", "bath", "wc")  # doors the critic's hub metrics look at
HUB_ZONE_R = 20  # cells (0 = off): private hub doors reach into a 4 m square around one point
HUB_ZONE_W = 6000  # a private room whose hub door is outside that cluster
BED_ENTRY_CLEAR = 22  # cells: bedrooms keep 2.2 m clear of the flat's front door
BED_ENTRY_W = 30000  # v3: critic entry_privacy (gate 0.40): a bedroom against the front door costs ~15 points
PASSAGE_MIN_DOORS = 2  # v3: a passage serves at least this many private doors (else it is an orphan pocket)
FOYER_COMPACT_W = 50000  # v3: a foyer in a flat below 3BHK / 110 m2 (premium flats only, owner brief)
ENTRY_DINING_PEN = 100000  # front door into the dining room (only when the flat's depth leaves no other way)
ENTRY_NO_DINING = True  # v5: the front door never opens into the dining room when a living room or foyer can take it
POWDER_ABSENT = 80000  # a flat without a guest toilet loses ~15 critic points (common_toilet gate)
NEAR_LIVING = 30  # cells: guest toilet within 3 m of the living room
NEAR_LIVING_W = 8000
WET_PAIR_W = 3000  # per pair of toilets that do not share a wall (critic wet_cluster)
VENT_DUCT_W = 90000  # a wet room ventilated by mechanical duct instead of façade/shaft (DCPR "review")
# a room too small for its furniture set: living / bedrooms are critic gates (a sofa or bed that does not fit
# costs 17-35 % of the score), so they pay like ~12 m2 of waste
FURN_W = {"living": 150000, "master": 150000, "bedroom": 150000}
FURN_W_OTHER = 40000
HARD_MIN_W = {"living": 2.8}  # m net: below this the critic finds no sofa set at all
SNAP = 1  # cells: >1 puts room edges on a SNAP grid or the flat's boundary lines (tried 3: worse on the golden set)
# ---- generator v4 (docs/quality/generator-v4.md): an approved case (casebase.adapt) as a soft guide
GUIDE_CENTRE_W = 12  # per cell (0.1 m) a room's centre lies away from its case room's centre, per axis
GUIDE_SIZE_W = 3  # per cell a room's side differs from its (stretched) case room's side
GUIDE_ORDER_W = 2500  # per pair of rooms in the opposite order to the case on one axis (left/right, front/back)
GUIDE_FOYER_W = 40000  # a foyer below FOYER_LARGE m2 carpet when the case enters straight into the living room
FOYER_LARGE = 110.0
GUIDE_HINT = "full"  # "", "xy" (room corners) or "full" (corners, sides and presence) from the case
GUIDE_VENT = True  # v5: guided solves keep every wet room on the façade or a shaft (no duct; the v3 layout may still duct)
GUIDE_KITCHEN_DINING = True  # guided solves: the kitchen door must open onto the dining room when the flat has one


@dataclass
class Edge:
    horizontal: bool
    pos: int  # grid y (horizontal) or x (vertical)
    a: int  # span start (grid)
    b: int  # span end (grid)
    outward: int  # +1 or -1 along the normal axis
    kind: str  # facade | shaft | access | party
    outer: bool = False  # facade that opens to the outside (not a notch facing the core or another wing)


@dataclass
class UnitGrid:
    unit_id: str
    ox: float
    oy: float
    Wg: int
    Hg: int
    interior: Polygon  # net interior (plate frame)
    blocked: list[tuple[int, int, int, int]]
    edges: list[Edge]
    door: tuple[Edge, int] | None  # access edge and grid position along it
    unit_poly: Polygon
    bands: Polygon
    rot: float = 0.0  # solve frame = plate frame rotated by -rot about pivot
    pivot: tuple[float, float] = (0.0, 0.0)


@dataclass
class RoomSpec:
    id: str
    type: str
    name: str
    tgt: int  # cells (0.01 m2) incl. wall allowance
    amin: float  # m2 net
    wmin: int  # cells
    aspect: int  # x10
    window_run: int  # cells of facade (0 = none)
    vent_run: int  # cells of facade or shaft (0 = none)
    optional: bool
    force: bool | None = None  # True force present, False force absent
    target_area: float = 0.0
    cap: int = 0  # v2: net cells above which area is waste (0 = none)
    hard: int = 0  # v2: gross cells hard upper bound (0 = legacy caps)
    over_w: int = 1  # v2: cost per cell above target (below cap)
    wfurn: int = 0  # v2: cells, side the furniture set needs (soft, FURN_W if missed)
    afurn: float = 0.0  # v2: net m2 the furniture set needs (soft, with wfurn)
    absent: int = 0  # v2: extra cost when an optional room is left out (e.g. the only guest toilet)


@dataclass
class SolveResult:
    status: str
    rects: dict[str, tuple[int, int, int, int]] = field(default_factory=dict)
    dropped: list[str] = field(default_factory=list)
    objective: float | None = None
    time_s: float = 0.0
    core: list[str] = field(default_factory=list)  # infeasible-core labels
    links: list[tuple[str, str]] | None = None  # v2: the door links the layout uses (fixed + chosen hub doors)
    entry: str | None = None  # v2: the room the front door opens into
    hub_point: tuple[int, int] | None = None  # v2: grid point the private doors cluster around
    door_along: int | None = None  # v3: where the front door ended up along the flat's lobby edge (grid)


# ------------------------------------------------------------------------------------- unit geometry


def _edge_bands(unit: Polygon, plate: Plate) -> tuple[Polygon, list[tuple[tuple, tuple, str]]]:
    outline = Polygon(plate.outline)
    access = shapely.union(zones_geom(plate, "passage"), zones_geom(plate, "core"))
    shafts = zones_geom(plate, "shaft")
    bands = []
    classified = []
    for ring in [unit.exterior, *unit.interiors]:
        cs = list(ring.coords)
        for i in range(len(cs) - 1):
            a, b = cs[i], cs[i + 1]
            L = math.dist(a, b)
            if L < 1e-6:
                continue
            mx, my = (a[0] + b[0]) / 2, (a[1] + b[1]) / 2
            nx, ny = (b[1] - a[1]) / L, -(b[0] - a[0]) / L  # right normal
            if unit.contains(SPoint(mx + nx * 0.05, my + ny * 0.05)):
                nx, ny = -nx, -ny  # make it outward
            probe = SPoint(mx + nx * 0.15, my + ny * 0.15)
            if not outline.contains(probe):
                kind, t = "facade", EXT_WALL
            elif not shafts.is_empty and shafts.buffer(1e-6).contains(probe):
                kind, t = "shaft", EXT_WALL
            elif access.buffer(1e-6).contains(probe):
                kind, t = "access", EXT_WALL
            else:
                kind, t = "party", PARTY_WALL
            seg = shapely.LineString([a, b])
            # band on the inner side only
            off = seg.buffer(t, cap_style="flat")
            bands.append(off.intersection(unit))
            classified.append(((a, b), (nx, ny), kind))
    return shapely.union_all(bands) if bands else Polygon(), classified


def _solve_frame(unit: Polygon, outline: Polygon) -> float:
    """Rotation that makes the flat's longest façade edge axis-aligned (0 for rectilinear flats)."""
    ring = geo.ring_of(unit)
    best, best_len = 0.0, 0.0
    for i in range(len(ring)):
        a, b = ring[i], ring[(i + 1) % len(ring)]
        L = math.dist(a, b)
        mx, my = (a[0] + b[0]) / 2, (a[1] + b[1]) / 2
        nx, ny = (b[1] - a[1]) / max(L, 1e-9), -(b[0] - a[0]) / max(L, 1e-9)
        if unit.contains(SPoint(mx + nx * 0.05, my + ny * 0.05)):
            nx, ny = -nx, -ny
        if not outline.contains(SPoint(mx + nx * 0.2, my + ny * 0.2)) and L > best_len:
            best_len = L
            best = math.degrees(math.atan2(b[1] - a[1], b[0] - a[0]))
    r = ((best + 45.0) % 90.0) - 45.0
    return 0.0 if abs(r) < 0.5 else r


def _rotated_plate(plate: Plate, rot: float, pivot: tuple[float, float]) -> Plate:
    from shapely import affinity

    def rr(ring):
        return geo.ring_of(affinity.rotate(Polygon(ring), -rot, origin=pivot))

    q = plate.model_copy(deep=True)
    q.outline = rr(plate.outline)
    for z in q.zones:
        z.polygon = geo.to_polyrings(affinity.rotate(geo.from_polyrings(z.polygon), -rot, origin=pivot))
    return q


def unit_grid(plate: Plate, unit: Unit) -> UnitGrid:
    from shapely import affinity

    up0 = Polygon(unit.polygon).buffer(0)
    rot = _solve_frame(up0, Polygon(plate.outline))
    pivot = (round(up0.centroid.x, 3), round(up0.centroid.y, 3))
    if rot:
        plate = _rotated_plate(plate, rot, pivot)
        unit = unit.model_copy(deep=True)
        unit.polygon = geo.ring_of(affinity.rotate(up0, -rot, origin=pivot))
        if unit.core_link:
            unit.core_link = tuple(affinity.rotate(SPoint(unit.core_link), -rot, origin=pivot).coords[0])  # type: ignore[assignment]
    up = Polygon(unit.polygon).buffer(0)
    bands, classified = _edge_bands(up, plate)
    interior = up.difference(bands)
    parts = geo.polygons_of(interior)
    interior = max(parts, key=lambda p: p.area) if parts else Polygon()
    interior = geo.rectilinearize(interior, 0.5) if not interior.is_empty else interior
    interior = shapely.set_precision(interior, 0.001)
    # merge collinear vertices (raster unions leave one every 0.5 m), so a straight façade is one edge
    interior = interior.simplify(0.0) if not interior.is_empty else interior
    minx, miny, maxx, maxy = interior.bounds
    ox, oy = minx, miny
    Wg = int(math.floor((maxx - minx) / G + 1e-6))
    Hg = int(math.floor((maxy - miny) / G + 1e-6))
    # blocked = bbox - interior, rounded outward
    blocked_geom = box(minx, miny, minx + Wg * G, miny + Hg * G).difference(interior)
    grid_boxes = []
    for x0, y0, x1, y1 in geo.rect_decompose(blocked_geom):
        gx0 = max(0, math.floor((x0 - ox) / G + 1e-6))
        gy0 = max(0, math.floor((y0 - oy) / G + 1e-6))
        gx1 = min(Wg, math.ceil((x1 - ox) / G - 1e-6))
        gy1 = min(Hg, math.ceil((y1 - oy) / G - 1e-6))
        if gx1 > gx0 and gy1 > gy0:
            grid_boxes.append(box(gx0, gy0, gx1, gy1))
    # outward rounding can make boxes overlap; fixed intervals must be disjoint for NoOverlap2D
    blocked = [
        (int(round(a)), int(round(b)), int(round(c)), int(round(d)))
        for a, b, c, d in geo.rect_decompose(shapely.union_all(grid_boxes))
    ] if grid_boxes else []
    # classify interior boundary edges by probing outward past the wall band
    outline = Polygon(plate.outline)
    access = shapely.union(zones_geom(plate, "passage"), zones_geom(plate, "core"))
    shafts = zones_geom(plate, "shaft")
    edges: list[Edge] = []
    for ring in [interior.exterior, *interior.interiors]:
        cs = list(ring.coords)
        for i in range(len(cs) - 1):
            a, b = cs[i], cs[i + 1]
            L = math.dist(a, b)
            if L < 0.3:
                continue
            horiz = abs(a[1] - b[1]) < 1e-6
            mx, my = (a[0] + b[0]) / 2, (a[1] + b[1]) / 2
            if horiz:
                nx, ny = 0.0, (1.0 if not interior.contains(SPoint(mx, my + 0.05)) else -1.0)
            else:
                nx, ny = (1.0 if not interior.contains(SPoint(mx + 0.05, my)) else -1.0), 0.0
            kind = "party"
            for dist in (0.35, 0.5, 0.8):
                probe = SPoint(mx + nx * dist, my + ny * dist)
                if not outline.contains(probe):
                    kind = "facade"
                    break
                if not shafts.is_empty and shafts.contains(probe):
                    kind = "shaft"
                    break
                if access.contains(probe):
                    kind = "access"
                    break
                if not up.contains(probe):
                    kind = "party"
                    break
            if horiz:
                pos_f = (my - oy) / G
                pos = math.floor(pos_f + 1e-6) if ny > 0 else math.ceil(pos_f - 1e-6)
                lo, hi = sorted(((a[0] - ox) / G, (b[0] - ox) / G))
            else:
                pos_f = (mx - ox) / G
                pos = math.floor(pos_f + 1e-6) if nx > 0 else math.ceil(pos_f - 1e-6)
                lo, hi = sorted(((a[1] - oy) / G, (b[1] - oy) / G))
            ga, gb = math.ceil(lo - 1e-6), math.floor(hi + 1e-6)
            outer = kind == "facade" and _opens_outward(outline, (mx, my), (nx, ny), a, b, horiz)
            if gb - ga >= 3:
                edges.append(Edge(horiz, int(pos), int(ga), int(gb), int(ny if horiz else nx), kind, outer))
    door = None
    if unit.core_link:
        dp = SPoint(*unit.core_link)
        best = None
        long_enough = [e for e in edges if e.kind == "access" and e.b - e.a >= 12]
        for e in (long_enough or edges):
            if e.kind != "access":
                continue
            if e.horizontal:
                ex0, ex1 = ox + e.a * G, ox + e.b * G
                seg = shapely.LineString([(ex0, oy + e.pos * G), (ex1, oy + e.pos * G)])
            else:
                ey0, ey1 = oy + e.a * G, oy + e.b * G
                seg = shapely.LineString([(ox + e.pos * G, ey0), (ox + e.pos * G, ey1)])
            d = seg.distance(dp)
            if best is None or d < best[0]:
                proj = seg.interpolate(seg.project(dp))
                along = (proj.x - ox) / G if e.horizontal else (proj.y - oy) / G
                best = (d, e, int(round(along)))
        if best is not None:
            _, e, along = best
            along = min(max(along, e.a + 5), e.b - 5)
            door = (e, along)
    return UnitGrid(unit.id, ox, oy, Wg, Hg, interior, blocked, edges, door, up, bands, rot, pivot)


OUTER_CLEAR = 9.0  # metres of open air a balcony needs in front of it


def _opens_outward(outline: Polygon, mid, normal, a, b, horiz: bool) -> bool:
    """True if the strip in front of this façade edge is free of the building for OUTER_CLEAR metres.

    Edges inside a re-entrant notch (facing the core or another wing of the plate) fail this test.
    """
    nx, ny = normal
    if horiz:
        x0, x1 = sorted((a[0], b[0]))
        y0 = mid[1] + ny * 0.5
        y1 = mid[1] + ny * OUTER_CLEAR
        strip = box(x0 + 0.3, min(y0, y1), x1 - 0.3, max(y0, y1)) if x1 - x0 > 0.6 else None
    else:
        y0_, y1_ = sorted((a[1], b[1]))
        x0 = mid[0] + nx * 0.5
        x1 = mid[0] + nx * OUTER_CLEAR
        strip = box(min(x0, x1), y0_ + 0.3, max(x0, x1), y1_ - 0.3) if y1_ - y0_ > 0.6 else None
    if strip is None:
        return False
    return strip.intersection(outline).area < 0.05


# ------------------------------------------------------------------------------------------ specs


V2_ASPECT = {"passage": 35, "foyer": 35}  # v2: a hub, not a gallery (critic corridor_len counts long passages)


def aspect_x10(room_type: str) -> int:
    if room_type in V2_ASPECT:
        return V2_ASPECT[room_type]
    pri = rules.priors().get("aspect_max", {})
    if room_type in pri:
        return int(round(float(pri[room_type]) * 10))
    return ASPECT_X10.get(room_type, 20)


def fill_scale(diagram: Diagram, ug: UnitGrid | None, overrides: dict[str, dict] | None = None) -> float:
    """Scale factor that makes the programme's targets fill the flat's net interior."""
    if ug is None or ug.interior.is_empty:
        return 1.0
    overrides = overrides or {}
    total = sum(float(overrides.get(r.id, {}).get("target_area", r.target_area)) for r in diagram.rooms if r.placed)
    if total <= 0:
        return 1.0
    usable = ug.interior.area * 0.94  # internal walls
    return max(0.7, min(1.6, usable / total))


def room_specs(diagram: Diagram, overrides: dict[str, dict] | None = None, ug: UnitGrid | None = None) -> list[RoomSpec]:
    overrides = overrides or {}
    k = fill_scale(diagram, ug, overrides)
    interior = ug.interior.area if ug is not None and not ug.interior.is_empty else 0.0
    specs = []
    for r in diagram.rooms:
        if not r.placed:
            continue
        o = overrides.get(r.id, {})
        target = float(o.get("target_area", r.target_area)) * (k if "target_area" not in o else 1.0)
        amin, wmin_m = room_minimums(r.type)
        amin = min(amin, target) if r.type not in ("living", "master", "bedroom", "study", "kitchen", "toilet", "bath", "wc") else amin
        fw, fa = FURN_MIN.get(r.type, (0.0, 0.0))
        wmin = max(8, math.ceil((wmin_m + INT_WALL) / G - 1e-6))
        wfurn = max(wmin, math.ceil((fw + INT_WALL) / G - 1e-6))
        if r.type in ("toilet", "bath", "wc"):  # small rooms: the WC/basin/shower set is a hard minimum
            wmin, amin = wfurn, max(amin, fa)
        elif r.type in HARD_MIN_W:  # the critic's tight tier (sofa facing a TV) never fails below this
            wmin = max(wmin, math.ceil((HARD_MIN_W[r.type] + INT_WALL) / G - 1e-6))
        tgt = int(round(100 * target + 23 * math.sqrt(max(target, 0.5))))
        cap_net = min(OVERSIZE.get(r.type, 1e9), 1.3 * target + 0.5)
        if r.type == "balcony":
            cap_net, hard_net = 0.0, max(1.8 * target, 4.0)
        elif r.type == "passage":
            hard_net = max(3.5, 0.06 * interior)
        elif r.type == "foyer":
            hard_net = max(4.5, 0.05 * interior)
        elif r.type in ("living", "dining", "master", "bedroom"):
            hard_net = 1.6 * cap_net
        else:
            hard_net = max(1.3 * cap_net, amin + 1.0)
        hard = int(100 * hard_net + 23 * math.sqrt(hard_net))
        if r.type in ("passage", "foyer"):
            tgt = 0  # circulation is a cost, not a room to reach a size
        needs_window = o.get("needs_window", r.needs_window)
        win = 0
        if needs_window:
            # v2: the run a window needs at the room's minimum size; the 1/6 glazing constraint (_touch) then
            # holds for the room's final area, so a big target no longer reserves façade it does not need
            win = math.ceil((max(1.0, min(target, max(amin, 9.0)) / (6 * WIN_HEIGHT)) + 0.3) / G)
        vent = 7 if r.type in ("toilet", "bath", "wc", "utility") and not needs_window else 0
        if r.type == "balcony":
            win = max(win, math.ceil(1.5 / G))  # balcony: >= 1.5 m on an outer façade
        specs.append(
            RoomSpec(
                id=r.id, type=r.type, name=r.name, tgt=tgt, amin=amin, wmin=wmin,
                aspect=aspect_x10(r.type), window_run=win, vent_run=vent,
                optional=r.optional, force=o.get("force"), target_area=target,
                cap=int(100 * cap_net), hard=hard, over_w=OVER_W.get(r.type, 4), wfurn=wfurn, afurn=max(fa, amin),
                absent=POWDER_ABSENT if r.type == "wc" and r.optional else 0,
            )
        )
    return specs


# ------------------------------------------------------------------------------------------ model


class _Builder:
    def __init__(self, ug: UnitGrid, specs: list[RoomSpec], links: list[tuple[str, str]], entry: str,
                 guards: bool, alts: list[tuple[str, tuple[str, ...]]] | None = None, guide: dict | None = None):
        self.m = cp_model.CpModel()
        self.guide = guide
        self.ug, self.specs, self.links, self.entry = ug, specs, links, entry
        self.alts = alts or []
        self.zoned = alts is not None  # v3: the zoned brief (layout.solve_flat) even when no hub door is flexible (1BHK)
        self.guards: dict[str, cp_model.IntVar] = {}
        self.guard_mode = guards
        self.v: dict[str, dict] = {}
        self.choice: dict[str, list[tuple[str, cp_model.IntVar]]] = {}
        self.extra_cost: list = []
        self.duct: dict[str, cp_model.IntVar] = {}
        self.bhk = sum(1 for s in specs if s.type in ("master", "bedroom"))
        self.door_pos = None
        if self.zoned and ug.door is not None and DOOR_SLIDE:
            # v3: the front door may sit anywhere along the flat's frontage on the lobby (not only at the edge's middle),
            # so the living room can reach it instead of a foyer or the dining room
            e, along = ug.door
            lo, hi = e.a + 5, e.b - 5
            self.door_pos = self.m.new_int_var(min(lo, along), max(hi, along), "door_pos")
        self.build()

    def guard(self, label: str):
        if not self.guard_mode:
            return []
        if label not in self.guards:
            self.guards[label] = self.m.new_bool_var(label)
        return [self.guards[label]]

    def build(self) -> None:
        m, ug = self.m, self.ug
        W, H = ug.Wg, ug.Hg
        xs, ys = [], []
        if SNAP > 1:
            vx = set(range(0, W + 1, SNAP)) | {0, W}
            vy = set(range(0, H + 1, SNAP)) | {0, H}
            for x0, y0, x1, y1 in ug.blocked:
                vx |= {x0, x1}
                vy |= {y0, y1}
            for e in ug.edges:
                (vy if e.horizontal else vx).add(e.pos)
                (vx if e.horizontal else vy).update((e.a, e.b))
            if ug.door is not None:
                e, along = ug.door
                (vx if e.horizontal else vy).update((along - 5, along + 5))
            self.dom_x = cp_model.Domain.from_values(sorted(v for v in vx if 0 <= v <= W))
            self.dom_y = cp_model.Domain.from_values(sorted(v for v in vy if 0 <= v <= H))
        for s in self.specs:
            pres = m.new_bool_var(f"p_{s.id}")
            if s.force is True or (not s.optional and s.force is None):
                if self.guard_mode:
                    m.add_implication(self.guard(f"room:{s.id}")[0], pres)
                else:
                    m.add(pres == 1)
            elif s.force is False:
                m.add(pres == 0)
            # domain: no side longer than the hard area cap allows at the room's aspect limit
            dmax = W if not s.hard else int(math.ceil(math.sqrt(s.hard * s.aspect / 10.0))) + 1
            wmax = max(min(s.wmin, W), min(W, dmax))
            hmax = max(min(s.wmin, H), min(H, dmax))
            if SNAP > 1:  # v2: room edges on a SNAP grid or on one of the flat's own boundary lines
                x = m.new_int_var_from_domain(self.dom_x, f"x_{s.id}")
                y = m.new_int_var_from_domain(self.dom_y, f"y_{s.id}")
                xe = m.new_int_var_from_domain(self.dom_x, f"xe_{s.id}")
                ye = m.new_int_var_from_domain(self.dom_y, f"ye_{s.id}")
            else:
                x = m.new_int_var(0, W, f"x_{s.id}")
                y = m.new_int_var(0, H, f"y_{s.id}")
                xe = m.new_int_var(0, W, f"xe_{s.id}")
                ye = m.new_int_var(0, H, f"ye_{s.id}")
            w = m.new_int_var(min(s.wmin, W), wmax, f"w_{s.id}")
            h = m.new_int_var(min(s.wmin, H), hmax, f"h_{s.id}")
            m.add(xe == x + w)
            m.add(ye == y + h)
            a = m.new_int_var(0, wmax * hmax, f"a_{s.id}")
            m.add_multiplication_equality(a, [w, h])
            xi = m.new_optional_interval_var(x, w, xe, pres, f"xi_{s.id}")
            yi = m.new_optional_interval_var(y, h, ye, pres, f"yi_{s.id}")
            xs.append(xi)
            ys.append(yi)
            # min area net of internal walls: 100a - 115(w+h) >= 10000*amin - 132
            m.add(100 * a - 115 * w - 115 * h >= int(10000 * s.amin) - 132).only_enforce_if(
                [pres, *self.guard(f"size:{s.id}")]
            )
            m.add(10 * w <= s.aspect * h)
            m.add(10 * h <= s.aspect * w)
            eff = m.new_int_var(0, wmax * hmax, f"e_{s.id}")
            m.add(eff == a).only_enforce_if(pres)
            m.add(eff == 0).only_enforce_if(pres.negated())
            if s.hard:
                m.add(a <= max(s.hard, s.tgt)).only_enforce_if(pres)
            under = m.new_int_var(0, s.tgt, f"u_{s.id}")
            over = m.new_int_var(0, wmax * hmax, f"o_{s.id}")
            m.add(a - s.tgt == over - under).only_enforce_if(pres)
            m.add(under == s.tgt).only_enforce_if(pres.negated())
            m.add(over == 0).only_enforce_if(pres.negated())
            cost = UNDER_W * under + s.over_w * over
            if s.cap:
                oc = m.new_int_var(0, wmax * hmax, f"oc_{s.id}")
                # net area (gross less half the internal walls) above the waste cap
                m.add(100 * oc >= 100 * a - 115 * w - 115 * h - 100 * s.cap).only_enforce_if(pres)
                cost = cost + CAP_W * oc
                if CAP_TIER_W:
                    oc2 = m.new_int_var(0, wmax * hmax, f"oc2_{s.id}")
                    m.add(oc2 >= oc - CAP_TIER)
                    cost = cost + CAP_TIER_W * oc2
            if s.type in ("passage", "foyer"):
                cost = cost + PASSAGE_FIXED * pres
            if s.type == "foyer" and self.zoned and not wants_foyer(self.bhk, 0.92 * ug.interior.area):
                # v3 owner brief: enter into the living room; a foyer in a compact flat only where the living room cannot
                # reach the front door (cheaper than entering through the dining room, ENTRY_DINING_PEN)
                cost = cost + FOYER_COMPACT_W * pres
            if s.absent:
                cost = cost + s.absent * (1 - pres)
            if s.wfurn > s.wmin or s.afurn > s.amin:
                fit = m.new_bool_var(f"fit_{s.id}")  # furniture fits with clearances (soft: façade may forbid it)
                m.add(w >= s.wfurn).only_enforce_if(fit)
                m.add(h >= s.wfurn).only_enforce_if(fit)
                m.add(100 * a - 115 * w - 115 * h >= int(10000 * s.afurn) - 132).only_enforce_if(fit)
                cost = cost + FURN_W.get(s.type, FURN_W_OTHER) * (pres - fit)
                m.add_implication(fit, pres)
            self.v[s.id] = {"p": pres, "x": x, "y": y, "w": w, "h": h, "a": a, "e": eff, "cost": cost, "s": s}
        for i, (x0, y0, x1, y1) in enumerate(ug.blocked):
            xs.append(m.new_interval_var(x0, x1 - x0, x1, f"bx{i}"))
            ys.append(m.new_interval_var(y0, y1 - y0, y1, f"by{i}"))
        m.add_no_overlap_2d(xs, ys)
        for a_id, b_id in self.links:
            if a_id in self.v and b_id in self.v:
                self._adjacent(a_id, b_id)
        self.hub_zone = None
        if HUB_ZONE_R and self.alts and sum(1 for s in self.specs if s.type in PRIVATE_DOOR) >= 2:
            self.hub_zone = (m.new_int_var(0, W, "hub_cx"), m.new_int_var(0, H, "hub_cy"), HUB_ZONE_R)
        for rid, cands in self.alts:
            self._adjacent_any(rid, cands)
        self._one_hub()
        self._useful_passage()
        for s in self.specs:
            if s.type == "balcony":
                self._touch(s.id, s.window_run, ("facade",), f"window:{s.id}", balcony=True)
            elif s.window_run:
                self._touch(s.id, s.window_run, ("facade",), f"window:{s.id}", glazing=True)
            elif s.vent_run:
                self._touch(s.id, s.vent_run, ("facade", "shaft"), f"vent:{s.id}")
        if self.zoned and ug.door is not None and self.entry in self.v:
            self._door_any()
        elif self.entry in self.v and ug.door is not None:
            fb = entry_fallback(self.specs, self.entry)
            self._door(self.entry, fallback=fb)
        if self.zoned and ug.door is not None and self.entry in self.v:  # v2 zoning: the private zone keeps away from the front door
                for s in self.specs:
                    if s.type in ("master", "bedroom") and s.id != self.entry:
                        self._clear_of_door(s.id, BED_ENTRY_CLEAR)
                self._direction()
        if self.guide and not self.guard_mode:
            self._guide(self.guide)
        # objective: waste (uncovered floor, rooms above their cap) >> circulation >> target closeness
        total = W * H - sum((x1 - x0) * (y1 - y0) for x0, y0, x1, y1 in ug.blocked)
        covered = sum(v["e"] for v in self.v.values())
        m.add(covered <= total)  # tightens the bound: uncovered >= 0
        self.total = total
        circ = [v["e"] for v in self.v.values() if v["s"].type in CIRCULATION]
        if circ and self.zoned:
            # v2: circulation up to 5 % of the flat is nearly free (approved plans p90 = 4.7 %); beyond it each cell
            # costs like wasted floor (the critic's circ_share gate)
            excess = m.new_int_var(0, total, "circ_excess")
            m.add(excess >= sum(circ) - int(CIRC_FREE * total))
            self.extra_cost.append(CIRC_EXCESS_W * excess)
            self._near_living()
            self._wet_pairs()
        m.minimize(sum(v["cost"] for v in self.v.values()) + UNCOVERED_W * (total - covered) + sum(self.extra_cost))

    def _guide(self, g: dict) -> None:
        """v4: keep an approved case's arrangement (casebase.adapt): soft target rectangles, soft relative order of room
        pairs, solution hints; a compact flat whose case enters into the living room pays for a foyer."""
        m, W, H = self.m, self.ug.Wg, self.ug.Hg
        cw, sw, ow = g.get("centre_w", GUIDE_CENTRE_W), g.get("size_w", GUIDE_SIZE_W), g.get("order_w", GUIDE_ORDER_W)
        for rid, (x0, y0, x1, y1) in g.get("rects", {}).items():
            if rid not in self.v or not (cw or sw):
                continue
            V = self.v[rid]
            for a, sz, lo, hi, L in (("x", "w", x0, x1, W), ("y", "h", y0, y1, H)):
                dev = m.new_int_var(0, 4 * L, f"gc_{a}_{rid}")
                m.add_abs_equality(dev, 2 * V[a] + V[sz] - (lo + hi))
                dsz = m.new_int_var(0, 2 * L, f"gs_{a}_{rid}")
                m.add_abs_equality(dsz, V[sz] - (hi - lo))
                self.extra_cost.append((cw // 2) * dev + sw * dsz)
            if GUIDE_HINT:  # the case layout as the search's starting point
                xs0, ys0 = max(0, min(W, x0)), max(0, min(H, y0))
                m.add_hint(V["x"], xs0)
                m.add_hint(V["y"], ys0)
                if GUIDE_HINT == "full":
                    m.add_hint(V["w"], max(1, min(W - xs0, x1 - x0)))
                    m.add_hint(V["h"], max(1, min(H - ys0, y1 - y0)))
                    m.add_hint(V["p"], 1)
        for a, b, axis in g.get("order", []):
            if a not in self.v or b not in self.v:
                continue
            A, B = self.v[a], self.v[b]
            k, sz = ("x", "w") if axis == "x" else ("y", "h")
            viol = m.new_bool_var(f"go_{a}_{b}_{axis}")
            m.add(2 * A[k] + A[sz] + 2 <= 2 * B[k] + B[sz]).only_enforce_if(viol.negated())
            self.extra_cost.append(ow * viol)
        if GUIDE_KITCHEN_DINING:
            # owner brief: the kitchen opens onto the dining room (serving route); a guided layout that cannot do it fails
            # and the v3 layout or another case is kept
            for rid, lits in self.choice.items():
                if self.v[rid]["s"].type != "kitchen" or not any(self.v[c]["s"].type == "dining" for c, _ in lits):
                    continue
                for c, ch in lits:
                    if self.v[c]["s"].type != "dining":
                        m.add(ch == 0)
        if g.get("entry") == "living" and 0.92 * self.ug.interior.area <= FOYER_LARGE:
            for v in self.v.values():
                if v["s"].type == "foyer":
                    self.extra_cost.append(GUIDE_FOYER_W * v["p"])

    def _wet_pairs(self) -> None:
        """v2: toilets back to back on a shared wall / shaft (soft reward per touching pair)."""
        wet = [rid for rid, v in self.v.items() if v["s"].type in ("toilet", "bath")]
        for i in range(len(wet)):
            for j in range(i + 1, len(wet)):
                t = self.m.new_bool_var(f"wet_{wet[i]}_{wet[j]}")
                self.m.add_implication(t, self.v[wet[i]]["p"])
                self.m.add_implication(t, self.v[wet[j]]["p"])
                self._cases(wet[i], wet[j], [t], min_run=4)
                self.extra_cost.append(WET_PAIR_W * (1 - t))

    def _near_living(self) -> None:
        """v2: the guest toilet (a toilet that opens onto the hub, not attached to a bedroom) within 3 m of the
        living room, so a guest walks a few steps, not across the flat (soft)."""
        m = self.m
        liv = next((v for v in self.v.values() if v["s"].type == "living"), None)
        if liv is None:
            return
        for rid in self.choice:
            T = self.v[rid]
            if T["s"].type not in ("toilet", "bath", "wc"):
                continue
            near = m.new_bool_var(f"near_{rid}")
            g = NEAR_LIVING
            m.add(T["x"] <= liv["x"] + liv["w"] + g).only_enforce_if(near)
            m.add(liv["x"] <= T["x"] + T["w"] + g).only_enforce_if(near)
            m.add(T["y"] <= liv["y"] + liv["h"] + g).only_enforce_if(near)
            m.add(liv["y"] <= T["y"] + T["h"] + g).only_enforce_if(near)
            self.extra_cost.append(NEAR_LIVING_W * (T["p"] - near))
            m.add_implication(near, T["p"])

    def _cases(self, i: str, j: str, enf: list, zone=None, min_run: int | None = None) -> None:
        """Rooms i and j share a wall of at least a door's width whenever all of ``enf`` hold. With ``zone``
        (cx, cy, r, z) the shared wall also reaches into the square of half-side r around (cx, cy) when z holds."""
        m = self.m
        A, B = self.v[i], self.v[j]
        L = min(DOOR_RUN.get(A["s"].type, 10), DOOR_RUN.get(B["s"].type, 10)) if min_run is None else min_run
        cases = []
        for horiz_touch in (True, False):
            for order in (0, 1):
                P, Q = (A, B) if order == 0 else (B, A)
                c = m.new_bool_var("")
                if horiz_touch:  # P left of Q, sharing a vertical wall
                    m.add(P["x"] + P["w"] == Q["x"]).only_enforce_if(c)
                    m.add(P["y"] + P["h"] - Q["y"] >= L).only_enforce_if(c)
                    m.add(Q["y"] + Q["h"] - P["y"] >= L).only_enforce_if(c)
                else:  # P below Q
                    m.add(P["y"] + P["h"] == Q["y"]).only_enforce_if(c)
                    m.add(P["x"] + P["w"] - Q["x"] >= L).only_enforce_if(c)
                    m.add(Q["x"] + Q["w"] - P["x"] >= L).only_enforce_if(c)
                if zone is not None:
                    cx, cy, r, z = zone
                    # wall line inside the band, and the shared stretch overlaps the square by a door's width
                    if horiz_touch:
                        a_, b_, wall, ctr, ctr2 = "y", "h", P["x"] + P["w"], cx, cy
                    else:
                        a_, b_, wall, ctr, ctr2 = "x", "w", P["y"] + P["h"], cy, cx
                    m.add(wall <= ctr + r).only_enforce_if([c, z])
                    m.add(wall >= ctr - r).only_enforce_if([c, z])
                    for U in (P[a_] + P[b_], Q[a_] + Q[b_], ctr2 + r):
                        for Lo in (P[a_], Q[a_], ctr2 - r):
                            m.add(U - Lo >= L).only_enforce_if([c, z])
                cases.append(c)
        m.add_bool_or(cases).only_enforce_if(enf)

    def _adjacent(self, i: str, j: str) -> None:
        A, B = self.v[i], self.v[j]
        self._cases(i, j, [A["p"], B["p"], *self.guard(f"link:{i}-{j}")])

    def _adjacent_any(self, rid: str, cands: tuple[str, ...]) -> None:
        """v2 hub door: room ``rid`` opens onto exactly one of ``cands`` (preference order, HUB_PEN per step)."""
        if rid not in self.v:
            return
        m, R = self.m, self.v[rid]
        opts = [c for c in cands if c in self.v and c != rid]
        if not opts:
            return
        lits = []
        zone = None
        if self.hub_zone is not None and R["s"].type in PRIVATE_DOOR:
            z = m.new_bool_var(f"zone_{rid}")  # soft: this room's hub door lies in the flat's door cluster
            self.extra_cost.append(HUB_ZONE_W * (R["p"] - z))
            m.add_implication(z, R["p"])
            zone = (*self.hub_zone, z)
        for k, c in enumerate(opts):
            ch = m.new_bool_var(f"ch_{rid}_{c}")
            m.add_implication(ch, self.v[c]["p"])
            m.add_implication(ch, R["p"])
            self._cases(rid, c, [ch], zone)
            # v3: approved-plan pattern / critic price of this door (bedroom onto passage > living > dining, guest
            # toilet never onto the dining, kitchen onto the dining); the v2 list order where there is no figure
            pc = hub_door_cost(R["s"].type, self.v[c]["s"].type, self.bhk)
            cost = HUB_PEN * k if pc is None else pc
            if cost:
                self.extra_cost.append(cost * ch)
            lits.append((c, ch))
        self.choice[rid] = lits
        g = self.guard(f"link:{rid}-hub")
        m.add(sum(ch for _, ch in lits) == 1).only_enforce_if([R["p"], *g])

    def _one_hub(self) -> None:
        """v2: private doors (bedrooms, study, common toilet) should open onto ONE hub: each extra hub used costs."""
        m = self.m
        used: dict[str, list] = {}
        for rid, lits in self.choice.items():
            if self.v[rid]["s"].type not in ("master", "bedroom", "study", "toilet", "bath", "wc"):
                continue
            for c, ch in lits:
                used.setdefault(c, []).append(ch)
        if len(used) < 2:
            return
        for c, chs in used.items():
            y = m.new_bool_var(f"hub_{c}")
            for ch in chs:
                m.add_implication(ch, y)
            self.extra_cost.append(HUB_SPLIT * y)

    def _useful_passage(self) -> None:
        """v3: a passage exists only to serve private doors: at least PASSAGE_MIN_DOORS of them open onto it (fewer
        if the flat has fewer), otherwise it is a one-door pocket (critic orphan)."""
        m = self.m
        for pid, v in self.v.items():
            if v["s"].type != "passage":
                continue
            chs = [ch for rid, lits in self.choice.items() if self.v[rid]["s"].type in PRIVATE_DOOR + ("pooja",)
                   for c, ch in lits if c == pid]
            if not chs:
                continue
            m.add(sum(chs) >= min(PASSAGE_MIN_DOORS, len(chs))).only_enforce_if(v["p"])

    def _direction(self) -> None:
        """v3: rooms approved flats put further from the entrance than the living room (bedrooms, kitchen, toilets,
        data/rules/approved_layout_patterns.json direction_from_living) pay when they lie no deeper than it."""
        m = self.m
        liv = next((v for v in self.v.values() if v["s"].type == "living"), None)
        if liv is None or self.ug.door is None:
            return
        e, _along = self.ug.door
        sgn = -e.outward  # inward along the door edge's normal axis
        ax, sz = ("y", "h") if e.horizontal else ("x", "w")

        def depth(v):
            return sgn * (2 * v[ax] + v[sz])

        for rid, v in self.v.items():
            t = v["s"].type
            if t not in ("master", "bedroom", "kitchen", "toilet", "bath") or v is liv:
                continue
            c = direction_cost(t, self.bhk)
            if not c:
                continue
            deeper = m.new_bool_var(f"deep_{rid}")
            m.add(depth(v) >= depth(liv) + 1).only_enforce_if(deeper)
            m.add_implication(deeper, v["p"])
            self.extra_cost.append(c * (v["p"] - deeper))

    def _clear_of_door(self, rid: str, d: int) -> None:
        """Room ``rid`` stays at least ``d`` cells from the flat's front door (either along or across the edge)."""
        m, R = self.m, self.v[rid]
        e, along = self.ug.door  # type: ignore[misc]
        if self.door_pos is not None:
            along = self.door_pos
        px, py = (along, e.pos) if e.horizontal else (e.pos, along)
        bs = [m.new_bool_var("") for _ in range(4)]
        m.add(R["x"] >= px + d).only_enforce_if(bs[0])
        m.add(R["x"] + R["w"] <= px - d).only_enforce_if(bs[1])
        m.add(R["y"] >= py + d).only_enforce_if(bs[2])
        m.add(R["y"] + R["h"] <= py - d).only_enforce_if(bs[3])
        viol = m.new_bool_var("")  # soft: a tight flat may still put a bedroom by the door, at a price
        self.extra_cost.append(BED_ENTRY_W * viol)
        m.add_bool_or([*bs, viol]).only_enforce_if(R["p"])

    def _door_any(self) -> None:
        """v2: the front door opens into the foyer, the living room or (at a price: the critic scores it 0.85)
        the dining room next to the living room - whichever the flat's depth allows."""
        types = {s.id: s.type for s in self.specs}
        order = [self.entry] + [rid for t in ("foyer", "living", "dining") for rid, ty in types.items()
                                if ty == t and rid != self.entry]
        if ENTRY_NO_DINING and any(types[r] in ("foyer", "living") for r in order):
            # v5 owner brief: never enter through the dining room - the living room or a foyer takes the door
            order = [r for r in order if types[r] != "dining"]
        lits = []
        for rid in order:
            ch = self.m.new_bool_var(f"door_{rid}")
            self.m.add_implication(ch, self.v[rid]["p"])
            self._door_one(rid, [ch])
            if types[rid] == "dining":
                self.extra_cost.append(ENTRY_DINING_PEN * ch)
            if types[rid] == "foyer":  # v3: a foyer that is not the entrance is a one-door pocket (critic orphan)
                self.m.add_implication(self.v[rid]["p"], ch)
            lits.append((rid, ch))
        self.entry_choice = lits
        self.m.add(sum(ch for _, ch in lits) == 1)

    def realized_entry(self, solver: cp_model.CpSolver) -> str | None:
        for rid, ch in getattr(self, "entry_choice", []):
            if solver.value(ch):
                return rid
        return None

    def realized_links(self, solver: cp_model.CpSolver) -> list[tuple[str, str]]:
        """Door links of the solved layout: fixed links between present rooms plus each hub room's chosen door."""
        out = []
        for a_id, b_id in self.links:
            if a_id in self.v and b_id in self.v and solver.value(self.v[a_id]["p"]) and solver.value(self.v[b_id]["p"]):
                out.append((a_id, b_id))
        for rid, lits in self.choice.items():
            for c, ch in lits:
                if solver.value(ch):
                    out.append((c, rid))
        return out

    def _touch(self, rid: str, run: int, kinds: tuple[str, ...], label: str, glazing: bool = False,
               balcony: bool = False) -> None:
        """Room edge on a façade/shaft span. With ``glazing`` the whole room edge sits on the span and its
        length covers the 1/6 opening rule for the room's *final* area (window 1.5 m high:
        L >= area/9 + 0.5 m  ->  90*side >= a + 450 in grid units)."""
        m, R = self.m, self.v[rid]
        lits = []
        for e in self.ug.edges:
            if e.kind not in kinds or e.b - e.a < run:
                continue
            if balcony and not e.outer:
                continue  # balconies never face the core / a re-entrant notch
            c = m.new_bool_var("")
            if balcony:
                # long side along the façade (>= 1.5x the depth), whole side on the open span
                bp = rules.priors().get("balcony", {})
                k15 = int(round(float(bp.get("min_aspect", 1.5)) * 10))
                dmax = int(round(float(bp.get("max_depth", 2.0)) / G))
                if e.horizontal:
                    m.add(10 * R["w"] >= k15 * R["h"]).only_enforce_if(c)
                    m.add(R["h"] <= dmax).only_enforce_if(c)
                    m.add(R["x"] >= e.a).only_enforce_if(c)
                    m.add(R["x"] + R["w"] <= e.b).only_enforce_if(c)
                else:
                    m.add(10 * R["h"] >= k15 * R["w"]).only_enforce_if(c)
                    m.add(R["w"] <= dmax).only_enforce_if(c)
                    m.add(R["y"] >= e.a).only_enforce_if(c)
                    m.add(R["y"] + R["h"] <= e.b).only_enforce_if(c)
            if e.horizontal:
                if e.outward > 0:
                    m.add(R["y"] + R["h"] == e.pos).only_enforce_if(c)
                else:
                    m.add(R["y"] == e.pos).only_enforce_if(c)
                if glazing:  # the part of the side on the façade carries the 1/6 window (v2: may overhang the span)
                    ov = self._overlap(R["x"], R["w"], e.a, e.b, c)
                    m.add(90 * ov >= R["a"] + 360).only_enforce_if(c)
                else:
                    m.add(R["x"] <= e.b - run).only_enforce_if(c)
                    m.add(R["x"] + R["w"] >= e.a + run).only_enforce_if(c)
                m.add(R["w"] >= run).only_enforce_if(c)
            else:
                if e.outward > 0:
                    m.add(R["x"] + R["w"] == e.pos).only_enforce_if(c)
                else:
                    m.add(R["x"] == e.pos).only_enforce_if(c)
                if glazing:
                    ov = self._overlap(R["y"], R["h"], e.a, e.b, c)
                    m.add(90 * ov >= R["a"] + 360).only_enforce_if(c)
                else:
                    m.add(R["y"] <= e.b - run).only_enforce_if(c)
                    m.add(R["y"] + R["h"] >= e.a + run).only_enforce_if(c)
                m.add(R["h"] >= run).only_enforce_if(c)
            lits.append(c)
        enf = [R["p"], *self.guard(label)]
        if label.startswith("vent:") and not self.guard_mode and not (self.guide and GUIDE_VENT and lits):
            # v2: a toilet the façade/shaft cannot reach is ventilated by a mechanical duct (DCPR "review"), at a
            # price worth ~2.5 m2 of wasted floor, instead of failing the flat into the fix ladder
            duct = m.new_bool_var(f"duct_{rid}")
            self.extra_cost.append(VENT_DUCT_W * duct)
            self.duct[rid] = duct
            lits = [*lits, duct]
        if lits:
            m.add_bool_or(lits).only_enforce_if(enf)
        else:
            m.add_bool_or([R["p"].negated(), *[g.negated() for g in self.guard(label)]])

    def _overlap(self, lo, size, a: int, b: int, c):
        """Length of [lo, lo + size] inside [a, b] (a lower bound that the solver pushes up to the true value)."""
        m = self.m
        ov = m.new_int_var(0, b - a, "")
        m.add(ov <= lo + size - a).only_enforce_if(c)
        m.add(ov <= b - lo).only_enforce_if(c)
        m.add(ov <= size).only_enforce_if(c)
        return ov

    def _door(self, rid: str, fallback: str | None = None) -> None:
        if fallback and fallback in self.v:
            # if the (optional) entry room is dropped, the fallback room takes the door
            self._door_one(fallback, [self.v[rid]["p"].negated(), self.v[fallback]["p"], *self.guard(f"entry:{rid}")])
        self._door_one(rid, [self.v[rid]["p"], *self.guard(f"entry:{rid}")])

    def _door_one(self, rid: str, enf: list) -> None:
        m, R = self.m, self.v[rid]
        e, along = self.ug.door  # type: ignore[misc]
        if self.door_pos is not None:
            along = self.door_pos
        half = 5
        if e.horizontal:
            if e.outward > 0:
                m.add(R["y"] + R["h"] == e.pos).only_enforce_if(enf)
            else:
                m.add(R["y"] == e.pos).only_enforce_if(enf)
            m.add(R["x"] <= along - half).only_enforce_if(enf)
            m.add(R["x"] + R["w"] >= along + half).only_enforce_if(enf)
        else:
            if e.outward > 0:
                m.add(R["x"] + R["w"] == e.pos).only_enforce_if(enf)
            else:
                m.add(R["x"] == e.pos).only_enforce_if(enf)
            m.add(R["y"] <= along - half).only_enforce_if(enf)
            m.add(R["y"] + R["h"] >= along + half).only_enforce_if(enf)


class _Stall(cp_model.CpSolverSolutionCallback):
    """Stop when the incumbent has not improved for a while (best-so-far within the time cap)."""

    def __init__(self, patience: float, gap: float = 0.02):
        super().__init__()
        self.t0 = time.perf_counter()
        self.last = self.t0
        self.best = None
        self.patience = patience
        self.gap = gap
        self.count = 0

    def on_solution_callback(self) -> None:
        now = time.perf_counter()
        obj = self.objective_value
        if self.best is None or obj < self.best - 1e-9:
            self.best = obj
            self.last = now
        self.count += 1
        bound = self.best_objective_bound
        if self.best is not None and self.best - bound <= self.gap * max(1.0, abs(self.best)):
            self.stop_search()


def entry_fallback(specs: list[RoomSpec], entry: str) -> str | None:
    by = {s.id: s for s in specs}
    if entry not in by or not by[entry].optional:
        return None
    for t in ("living", "passage", "dining"):
        for s in specs:
            if s.type == t and not s.optional and s.id != entry:
                return s.id
    return None


def stall_patience(time_limit: float, n_rooms: int) -> float:
    """Seconds without a better incumbent before the search stops early.

    Big programmes (3BHK and up, 12+ rooms) improve in rarer, larger steps, so they get more patience."""
    scale = 1.0 + 0.08 * max(0, n_rooms - 8)
    return min(3.5, max(0.5, time_limit * 0.25 * scale))


def _params(solver: cp_model.CpSolver, time_limit: float, workers: int | None = None) -> None:
    p = solver.parameters
    p.max_time_in_seconds = time_limit
    p.num_workers = workers or min(8, max(1, os.cpu_count() or 1))
    p.random_seed = 7
    p.log_search_progress = False


def solve(ug: UnitGrid, specs: list[RoomSpec], links: list[tuple[str, str]], entry: str,
          time_limit: float = TIME_CAP, explain: bool = True,
          alts: list[tuple[str, tuple[str, ...]]] | None = None, seed: int = 7, guide: dict | None = None) -> SolveResult:
    t0 = time.perf_counter()
    b = _Builder(ug, specs, links, entry, guards=False, alts=alts, guide=guide)
    solver = cp_model.CpSolver()
    _params(solver, max(0.2, time_limit - (time.perf_counter() - t0)))  # model build counts against the cap
    solver.parameters.random_seed = seed
    cb = _Stall(patience=stall_patience(time_limit, len(specs)))
    st = _solve_with_stall(solver, b.m, cb, time_limit)
    elapsed = time.perf_counter() - t0
    if st in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        rects, dropped = {}, []
        for rid, v in b.v.items():
            if solver.value(v["p"]):
                rects[rid] = (solver.value(v["x"]), solver.value(v["y"]), solver.value(v["w"]), solver.value(v["h"]))
            else:
                dropped.append(rid)
        status = "optimal" if st == cp_model.OPTIMAL else ("feasible" if elapsed < time_limit * 0.98 else "timeout")
        return SolveResult(status, rects, dropped, solver.objective_value, round(elapsed, 3),
                           links=b.realized_links(solver), entry=b.realized_entry(solver),
                           hub_point=(solver.value(b.hub_zone[0]), solver.value(b.hub_zone[1])) if b.hub_zone else None,
                           door_along=solver.value(b.door_pos) if b.door_pos is not None else None)
    if st == cp_model.INFEASIBLE or st == cp_model.MODEL_INVALID:
        core = explain_infeasible(ug, specs, links, entry, budget=min(3.0, max(0.3, time_limit - elapsed)),
                                  alts=alts) if explain else []
        return SolveResult("infeasible", time_s=round(time.perf_counter() - t0, 3), core=core)
    return SolveResult("timeout", time_s=round(elapsed, 3))


def _solve_with_stall(solver: cp_model.CpSolver, model: cp_model.CpModel, cb: _Stall, time_limit: float):
    """Solve in slices so a stalled search can stop early (callbacks only fire on new solutions)."""
    import threading

    done = threading.Event()

    def watchdog() -> None:
        while not done.wait(0.1):
            if cb.best is not None and time.perf_counter() - cb.last > cb.patience:
                solver.stop_search()
                return

    th = threading.Thread(target=watchdog, daemon=True)
    th.start()
    try:
        return solver.solve(model, cb)
    finally:
        done.set()
        th.join(timeout=1)


def _feasible_with(ug, specs, links, entry, active: set[str], t: float, alts=None) -> bool | None:
    b = _Builder(ug, specs, links, entry, guards=True, alts=alts)
    for label, lit in b.guards.items():
        b.m.add(lit == (1 if label in active else 0))
    b.m.clear_objective()
    s = cp_model.CpSolver()
    _params(s, t)
    st = s.solve(b.m)
    if st in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return True
    if st == cp_model.INFEASIBLE:
        return False
    return None


CATEGORY_ORDER = ("link", "size", "entry", "window", "vent", "room")


def explain_infeasible(ug: UnitGrid, specs, links, entry, budget: float = 6.0, alts=None) -> list[str]:
    """Smallest set of requirements (rooms, sizes, links, windows) that cannot hold together.

    Each test fixes the requirement literals as constants (so presolve can refute quickly), first
    dropping whole categories, then single requirements (deletion filter). Undecided tests keep the
    requirement, so the result is always a sufficient (if not always minimal) explanation.
    """
    t0 = time.perf_counter()
    probe = _Builder(ug, specs, links, entry, guards=True, alts=alts)
    labels = list(probe.guards)
    if _feasible_with(ug, specs, links, entry, set(labels), min(2.0, budget), alts) is not False:
        return []
    active = set(labels)

    def left() -> float:
        return budget - (time.perf_counter() - t0)

    for cat in CATEGORY_ORDER:
        if left() <= 0:
            break
        trial = {lab for lab in active if not lab.startswith(cat + ":")}
        if trial != active and _feasible_with(ug, specs, links, entry, trial, min(0.6, max(0.1, left())), alts) is False:
            active = trial
    order = sorted(active, key=lambda lab: (CATEGORY_ORDER.index(lab.split(":")[0]), lab))
    for lab in order:
        if left() <= 0:
            break
        trial = active - {lab}
        if _feasible_with(ug, specs, links, entry, trial, min(0.5, max(0.1, left())), alts) is False:
            active = trial
    return sorted(active, key=lambda lab: (CATEGORY_ORDER.index(lab.split(":")[0]), lab))


def rect_to_poly(ug: UnitGrid, r: tuple[int, int, int, int]) -> Polygon:
    x, y, w, h = r
    return box(ug.ox + x * G, ug.oy + y * G, ug.ox + (x + w) * G, ug.oy + (y + h) * G)
