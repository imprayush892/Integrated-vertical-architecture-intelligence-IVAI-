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
from .diagram import room_minimums
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


@dataclass
class SolveResult:
    status: str
    rects: dict[str, tuple[int, int, int, int]] = field(default_factory=dict)
    dropped: list[str] = field(default_factory=list)
    objective: float | None = None
    time_s: float = 0.0
    core: list[str] = field(default_factory=list)  # infeasible-core labels


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


def aspect_x10(room_type: str) -> int:
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
    specs = []
    for r in diagram.rooms:
        if not r.placed:
            continue
        o = overrides.get(r.id, {})
        target = float(o.get("target_area", r.target_area)) * (k if "target_area" not in o else 1.0)
        amin, wmin_m = room_minimums(r.type)
        amin = min(amin, target) if r.type not in ("living", "master", "bedroom", "study", "kitchen", "toilet", "bath", "wc") else amin
        wmin = max(8, math.ceil((wmin_m + INT_WALL) / G - 1e-6))
        tgt = int(round(100 * target + 23 * math.sqrt(max(target, 0.5))))
        needs_window = o.get("needs_window", r.needs_window)
        win = 0
        if needs_window:
            win = math.ceil((max(1.0, target / (6 * WIN_HEIGHT)) + 0.3) / G)
        vent = 7 if r.type in ("toilet", "bath", "wc", "utility") and not needs_window else 0
        if r.type == "balcony":
            win = max(win, math.ceil(1.5 / G))  # balcony: >= 1.5 m on an outer façade
        specs.append(
            RoomSpec(
                id=r.id, type=r.type, name=r.name, tgt=tgt, amin=amin, wmin=wmin,
                aspect=aspect_x10(r.type), window_run=win, vent_run=vent,
                optional=r.optional, force=o.get("force"), target_area=target,
            )
        )
    return specs


# ------------------------------------------------------------------------------------------ model


class _Builder:
    def __init__(self, ug: UnitGrid, specs: list[RoomSpec], links: list[tuple[str, str]], entry: str,
                 guards: bool):
        self.m = cp_model.CpModel()
        self.ug, self.specs, self.links, self.entry = ug, specs, links, entry
        self.guards: dict[str, cp_model.IntVar] = {}
        self.guard_mode = guards
        self.v: dict[str, dict] = {}
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
        for s in self.specs:
            pres = m.new_bool_var(f"p_{s.id}")
            if s.force is True or (not s.optional and s.force is None):
                if self.guard_mode:
                    m.add_implication(self.guard(f"room:{s.id}")[0], pres)
                else:
                    m.add(pres == 1)
            elif s.force is False:
                m.add(pres == 0)
            wmax = W
            hmax = H
            x = m.new_int_var(0, W, f"x_{s.id}")
            y = m.new_int_var(0, H, f"y_{s.id}")
            w = m.new_int_var(min(s.wmin, W), wmax, f"w_{s.id}")
            h = m.new_int_var(min(s.wmin, H), hmax, f"h_{s.id}")
            xe = m.new_int_var(0, W, f"xe_{s.id}")
            ye = m.new_int_var(0, H, f"ye_{s.id}")
            m.add(xe == x + w)
            m.add(ye == y + h)
            a = m.new_int_var(0, W * H, f"a_{s.id}")
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
            eff = m.new_int_var(0, W * H, f"e_{s.id}")
            m.add(eff == a).only_enforce_if(pres)
            m.add(eff == 0).only_enforce_if(pres.negated())
            # under-size costs 1, over-size costs 1 for main rooms and 4 for service rooms (so leftover
            # space goes to living rooms, not toilets); service rooms are also capped at 1.6x target
            over_w = 2 if s.type in CIRCULATION else 4 if s.type in SERVICE else 2 if s.type == "kitchen" else 1
            if s.type in CIRCULATION:
                m.add(a <= max(s.tgt * 3, 1500)).only_enforce_if(pres)
            elif s.type in SERVICE or s.type == "kitchen":
                m.add(a <= max(int(s.tgt * 1.6), s.tgt + 150)).only_enforce_if(pres)
            else:
                m.add(a <= int(s.tgt * 2.2)).only_enforce_if(pres)
            under = m.new_int_var(0, s.tgt, f"u_{s.id}")
            over = m.new_int_var(0, W * H, f"o_{s.id}")
            m.add(a - s.tgt == over - under).only_enforce_if(pres)
            m.add(under == s.tgt).only_enforce_if(pres.negated())
            m.add(over == 0).only_enforce_if(pres.negated())
            dev = m.new_int_var(0, 4 * W * H + s.tgt, f"d_{s.id}")
            m.add(dev == under + over_w * over)
            self.v[s.id] = {"p": pres, "x": x, "y": y, "w": w, "h": h, "a": a, "e": eff, "d": dev, "s": s}
        for i, (x0, y0, x1, y1) in enumerate(ug.blocked):
            xs.append(m.new_interval_var(x0, x1 - x0, x1, f"bx{i}"))
            ys.append(m.new_interval_var(y0, y1 - y0, y1, f"by{i}"))
        m.add_no_overlap_2d(xs, ys)
        for a_id, b_id in self.links:
            if a_id in self.v and b_id in self.v:
                self._adjacent(a_id, b_id)
        for s in self.specs:
            if s.type == "balcony":
                self._touch(s.id, s.window_run, ("facade",), f"window:{s.id}", balcony=True)
            elif s.window_run:
                self._touch(s.id, s.window_run, ("facade",), f"window:{s.id}", glazing=True)
            elif s.vent_run:
                self._touch(s.id, s.vent_run, ("facade", "shaft"), f"vent:{s.id}")
        if self.entry in self.v and ug.door is not None:
            fb = entry_fallback(self.specs, self.entry)
            self._door(self.entry, fallback=fb)
        # objective: target closeness (x2) + uncovered area + small bias against tall/skinny rooms
        total = W * H - sum((x1 - x0) * (y1 - y0) for x0, y0, x1, y1 in ug.blocked)
        covered = sum(v["e"] for v in self.v.values())
        m.add(covered <= total)  # tightens the bound: uncovered >= 0
        self.total = total
        m.minimize(sum(v["d"] for v in self.v.values()) + 5 * (total - covered))

    def _adjacent(self, i: str, j: str) -> None:
        m = self.m
        A, B = self.v[i], self.v[j]
        L = min(DOOR_RUN.get(A["s"].type, 10), DOOR_RUN.get(B["s"].type, 10))
        enf = [A["p"], B["p"], *self.guard(f"link:{i}-{j}")]
        cases = []
        for horiz_touch in (True, False):
            for order in (0, 1):
                P, Q = (A, B) if order == 0 else (B, A)
                c = m.new_bool_var("")
                if horiz_touch:  # P left of Q, sharing a vertical wall
                    m.add(P["x"] + P["w"] == Q["x"]).only_enforce_if(c)
                    m.add(P["y"] + P["h"] - Q["y"] >= L).only_enforce_if(c)
                    m.add(Q["y"] + Q["h"] - P["y"] >= L).only_enforce_if(c)
                    m.add(P["h"] >= L).only_enforce_if(c)
                    m.add(Q["h"] >= L).only_enforce_if(c)
                else:  # P below Q
                    m.add(P["y"] + P["h"] == Q["y"]).only_enforce_if(c)
                    m.add(P["x"] + P["w"] - Q["x"] >= L).only_enforce_if(c)
                    m.add(Q["x"] + Q["w"] - P["x"] >= L).only_enforce_if(c)
                    m.add(P["w"] >= L).only_enforce_if(c)
                    m.add(Q["w"] >= L).only_enforce_if(c)
                cases.append(c)
        m.add_bool_or(cases).only_enforce_if(enf)

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
                if glazing:
                    m.add(R["x"] >= e.a).only_enforce_if(c)
                    m.add(R["x"] + R["w"] <= e.b).only_enforce_if(c)
                    m.add(90 * R["w"] >= R["a"] + 360).only_enforce_if(c)
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
                    m.add(R["y"] >= e.a).only_enforce_if(c)
                    m.add(R["y"] + R["h"] <= e.b).only_enforce_if(c)
                    m.add(90 * R["h"] >= R["a"] + 360).only_enforce_if(c)
                else:
                    m.add(R["y"] <= e.b - run).only_enforce_if(c)
                    m.add(R["y"] + R["h"] >= e.a + run).only_enforce_if(c)
                m.add(R["h"] >= run).only_enforce_if(c)
            lits.append(c)
        enf = [R["p"], *self.guard(label)]
        if lits:
            m.add_bool_or(lits).only_enforce_if(enf)
        else:
            m.add_bool_or([R["p"].negated(), *[g.negated() for g in self.guard(label)]])

    def _door(self, rid: str, fallback: str | None = None) -> None:
        if fallback and fallback in self.v:
            # if the (optional) entry room is dropped, the fallback room takes the door
            self._door_one(fallback, [self.v[rid]["p"].negated(), self.v[fallback]["p"], *self.guard(f"entry:{rid}")])
        self._door_one(rid, [self.v[rid]["p"], *self.guard(f"entry:{rid}")])

    def _door_one(self, rid: str, enf: list) -> None:
        m, R = self.m, self.v[rid]
        e, along = self.ug.door  # type: ignore[misc]
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
          time_limit: float = TIME_CAP, explain: bool = True) -> SolveResult:
    t0 = time.perf_counter()
    b = _Builder(ug, specs, links, entry, guards=False)
    solver = cp_model.CpSolver()
    _params(solver, max(0.2, time_limit - (time.perf_counter() - t0)))  # model build counts against the cap
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
        return SolveResult(status, rects, dropped, solver.objective_value, round(elapsed, 3))
    if st == cp_model.INFEASIBLE or st == cp_model.MODEL_INVALID:
        core = explain_infeasible(ug, specs, links, entry, budget=min(3.0, max(0.3, time_limit - elapsed))) if explain else []
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


def _feasible_with(ug, specs, links, entry, active: set[str], t: float) -> bool | None:
    b = _Builder(ug, specs, links, entry, guards=True)
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


def explain_infeasible(ug: UnitGrid, specs, links, entry, budget: float = 6.0) -> list[str]:
    """Smallest set of requirements (rooms, sizes, links, windows) that cannot hold together.

    Each test fixes the requirement literals as constants (so presolve can refute quickly), first
    dropping whole categories, then single requirements (deletion filter). Undecided tests keep the
    requirement, so the result is always a sufficient (if not always minimal) explanation.
    """
    t0 = time.perf_counter()
    probe = _Builder(ug, specs, links, entry, guards=True)
    labels = list(probe.guards)
    if _feasible_with(ug, specs, links, entry, set(labels), min(2.0, budget)) is not False:
        return []
    active = set(labels)

    def left() -> float:
        return budget - (time.perf_counter() - t0)

    for cat in CATEGORY_ORDER:
        if left() <= 0:
            break
        trial = {lab for lab in active if not lab.startswith(cat + ":")}
        if trial != active and _feasible_with(ug, specs, links, entry, trial, min(0.6, max(0.1, left()))) is False:
            active = trial
    order = sorted(active, key=lambda lab: (CATEGORY_ORDER.index(lab.split(":")[0]), lab))
    for lab in order:
        if left() <= 0:
            break
        trial = active - {lab}
        if _feasible_with(ug, specs, links, entry, trial, min(0.5, max(0.1, left()))) is False:
            active = trial
    return sorted(active, key=lambda lab: (CATEGORY_ORDER.index(lab.split(":")[0]), lab))


def rect_to_poly(ug: UnitGrid, r: tuple[int, int, int, int]) -> Polygon:
    x, y, w, h = r
    return box(ug.ox + x * G, ug.oy + y * G, ug.ox + (x + w) * G, ug.oy + (y + h) * G)
