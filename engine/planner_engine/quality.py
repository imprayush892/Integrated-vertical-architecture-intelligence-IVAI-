"""Plan critic: scores a solved flat the way an Indian architect would (deterministic, no learning at run time).

``critique(plan, unit, plate)`` returns ``{score 0-100, grade, metrics, subscores, issues, furniture, ...}``.

How the score is made (details, sources and calibration in docs/quality/critic.md):

* every metric has a measured value, a ``good`` and a ``bad`` threshold and a weight; satisfaction is 1 at
  ``good``, 0 at ``bad`` and linear between; ``base`` is the weighted mean of satisfactions x 100 and the score is
  ``100 - STRICTNESS x (100 - base)`` (an architect does not average away a sealed 30 m2 store), then critical penalties,
  then multiplied by the weakest-link factor from ``GATE``;
* a few findings are *critical* (enter a flat through a bedroom, a bedroom reached through another bedroom or a
  toilet, a room nobody can reach, ...): each takes ``CRITICAL_PENALTY`` points off, capped at ``CRITICAL_CAP``;
* metrics that cannot be measured (no unit/plate supplied, room type absent) are left out and the weights renormalised.

Everything is metres, in the plan's own frame (any rotation is fine: all geometry is derived from the room edges).
Five groups: circulation, waste, indian sequence, furniture fit, daylight/ventilation, doors.
"""

from __future__ import annotations

import heapq
import itertools
import math
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import shapely
from shapely.geometry import LineString, Point, Polygon
from shapely.geometry.polygon import orient
from shapely.ops import nearest_points

from .models import Opening, Plan, PlanRoom, Plate, Unit

# ------------------------------------------------------------------------------------------------ constants

BED = {"master", "bedroom"}
HAB = {"living", "master", "bedroom", "study", "kitchen"}  # DCPR habitable + kitchen: need daylight
WET = {"toilet", "bath", "wc"}
CIRC = {"passage", "foyer"}
LIVING_LIKE = {"living", "dining"}
SKIP_SHAPE = {"passage", "foyer", "balcony", "store", "dress", "pooja"}  # passages are meant to be long

STRICTNESS = 1.5  # score = 100 - STRICTNESS x (100 - weighted satisfaction): one bad group must show
# "weakest link": the worst gate metric multiplies the score by (1 - G) + G x satisfaction, G = GATE[key]
# (a flat with 57 % wasted floor is not a 70 because its doors are fine; a bed that does not fit costs less than that).
GATE = {"waste_share": 0.70, "circ_share": 0.30, "entry_seq": 0.55, "privacy": 0.55, "through_wet": 0.55, "kitchen_near": 0.45,
        "fit_sleep": 0.40, "fit_living": 0.35, "fit_kitchen": 0.40, "fit_toilet": 0.30, "hab_window": 0.40, "room_size": 0.30,
        "room_shape": 0.30, "room_aspect": 0.25, "corridor_len": 0.25, "common_toilet": 0.25, "efficiency": 0.25, "hub_spread": 0.20}
CRITICAL_PENALTY = 10.0
CRITICAL_CAP = 40.0
GRADES = [(85, "A"), (70, "B"), (55, "C"), (40, "D"), (0, "F")]


@dataclass(frozen=True)
class Metric:
    key: str
    cat: str
    weight: float
    good: float  # satisfaction 1 at/beyond this value
    bad: float  # satisfaction 0 at/beyond this value (bad < good means higher is better)
    unit: str
    what: str
    source: str


def _m(key, cat, weight, good, bad, unit, what, source):
    return Metric(key, cat, weight, good, bad, unit, what, source)


METRICS: dict[str, Metric] = {m.key: m for m in [
    # ---- circulation (20)
    _m("circ_share", "circulation", 6, 6.0, 15.0, "% of net", "passage + foyer area as % of net room area",
       "approved plans: p90 = 5 % (topology_v1, 986 clean flats; median 0 %); practice: <= 8 %"),
    _m("corridor_len", "circulation", 3, 2.5, 7.0, "m", "length of corridor-like circulation (area / clear width)",
       "practice: a flat passage over ~3 m is a corridor; > 7 m is a gallery"),
    _m("hub_spread", "circulation", 3, 4.5, 10.0, "m", "max distance between bedroom / common-toilet doors",
       "approved plans: bedroom centroids p50 = 3.7 m, p75 = 6.7 m apart; compact hub wanted by the owner"),
    _m("hub_share", "circulation", 3, 0.75, 0.40, "share", "share of private doors that open onto one space",
       "owner brief: one compact hub where bedroom/toilet doors open"),
    _m("dead_end", "circulation", 2, 1.0, 2.5, "m", "passage length beyond its last door (dead end)",
       "practice: dead-end stub <= 1 m"),
    _m("travel_to_exit", "circulation", 3, 22.5, 40.0, "m", "farthest habitable room -> flat door -> nearest stair",
       "NBC 2016 Part 4: residential travel distance 22.5 m (30 m sprinklered)"),
    # ---- wasted space (18)
    _m("room_aspect", "waste", 3, 1.0, 0.5, "score", "area-weighted aspect-ratio score of rooms", "practice; see ASPECT"),
    _m("room_shape", "waste", 4, 1.0, 0.5, "score", "area-weighted rectangularity (no notches / odd corners)", "practice; see SHAPE"),
    _m("waste_share", "waste", 9, 4.0, 30.0, "% of net", "leftover rooms + area beyond 1.3 x the room's target",
       "owner brief: no wasted space; approved area_ratio p50 = 1.0"),
    _m("efficiency", "waste", 3, 0.80, 0.65, "ratio", "net room area / gross flat area",
       "practice: 0.80-0.85 carpet/gross for flats excluding the core"),
    _m("room_size", "waste", 3, 1.0, 0.5, "score", "rooms below the comfortable size for their type",
       "approved plans p10 and p25 per type (topology_v1)"),
    # ---- indian sequence (27)
    _m("entry_seq", "sequence", 6, 1.0, 0.0, "score", "front door opens into living (or foyer -> living)",
       "approved plans: 95 % of entrances are the living room (topology_v1)"),
    _m("kitchen_near", "sequence", 4, 1.0, 0.0, "score", "kitchen next to / one step from living-dining",
       "approved plans: kitchen touches living/dining/foyer in 98 %, door-linked in 82 %"),
    _m("common_toilet", "sequence", 4, 1.0, 0.0, "score", "powder / common toilet reachable from living without a bedroom",
       "approved plans: 80 % of flats have one"),
    _m("privacy", "sequence", 4, 1.0, 0.0, "score", "bedrooms not entered from kitchen, another bedroom or a toilet",
       "practice; approved plans: 94 % of bedrooms reachable without crossing another bedroom (rest mostly link-extraction gaps)"),
    _m("through_wet", "sequence", 2, 1.0, 0.0, "score", "no room reachable only through a toilet", "practice; NBC habitability"),
    _m("attached_share", "sequence", 2, 0.60, 0.20, "share", "bedrooms with an attached toilet",
       "approved plans: master 88 %, other bedrooms 53 % (1BHK 83 %, 2BHK 51 %, 3BHK 62 %)"),
    _m("master_attached", "sequence", 2, 1.0, 0.0, "score", "master bedroom has an attached toilet", "approved plans: 88 %"),
    _m("wet_cluster", "sequence", 2, 0.60, 0.30, "share", "share of wet rooms in the largest wet cluster (shared shaft/wall)",
       "approved plans: median 0.5-0.67 (topology_v1)"),
    # ---- furniture fit (22)
    _m("fit_sleep", "furniture", 6, 1.0, 0.0, "score", "bed + wardrobe fit with clearances", "practice; see FURNITURE"),
    _m("fit_living", "furniture", 4, 1.0, 0.0, "score", "sofa set + TV wall + dining table fit", "practice"),
    _m("fit_kitchen", "furniture", 5, 1.0, 0.0, "score", "counter run >= 2.4 m with a 0.9 m aisle", "practice (NBC kitchen 5.5 m2 x 1.8 m)"),
    _m("fit_toilet", "furniture", 5, 1.0, 0.0, "score", "WC + basin + shower zone with clearances", "practice; NBC 2016 Part 3 sanitation"),
    # ---- daylight / ventilation (8)
    _m("hab_window", "daylight", 3, 1.0, 0.5, "share", "habitable rooms with a window on the facade", "DCPR 2034 Reg 39(1)"),
    _m("window_ratio", "daylight", 1, 0.1667, 0.10, "ratio", "smallest window / floor area among habitable rooms", "DCPR 2034 Reg 39(1): 1/6"),
    _m("wet_vent", "daylight", 3, 1.0, 0.5, "share", "toilets ventilated to facade or shaft", "DCPR 2034 Reg 39(2)"),
    # ---- doors (5)
    _m("swing_clash", "doors", 2, 0, 3, "count", "door swings that overlap each other", "practice"),
    _m("swing_free", "doors", 1, 0, 3, "count", "doors whose swing does not fit in the room", "practice"),
    _m("passage_door", "doors", 1, 0, 3, "count", "doors onto a passage narrower than 0.9 m / swinging into one < 1.2 m", "NBC 2016 Part 4"),
    _m("door_width", "doors", 1, 0, 3, "count", "doors narrower than the minimum for their use", "NBC 2016 Part 4: 1.0 m flat door, 0.75 m WC"),
]}
CATEGORIES = ["circulation", "waste", "sequence", "furniture", "daylight", "doors"]

# (good, bad) aspect ratio per type: satisfaction 1 up to good, 0 from bad
ASPECT = {"living": (2.0, 3.2), "dining": (2.0, 3.2), "kitchen": (2.3, 3.6), "master": (1.6, 2.5), "bedroom": (1.6, 2.5),
          "study": (1.8, 2.8), "toilet": (2.3, 3.4), "bath": (2.3, 3.4), "wc": (2.6, 4.0), "utility": (2.6, 4.0),
          "pooja": (2.0, 3.0), "dress": (2.8, 4.5)}
# (good, bad) rectangularity = area / minimum-rotated-rectangle area
SHAPE = {"living": (0.82, 0.62), "dining": (0.85, 0.65), "kitchen": (0.9, 0.7), "master": (0.93, 0.78), "bedroom": (0.93, 0.78),
         "study": (0.93, 0.78), "toilet": (0.95, 0.8), "bath": (0.95, 0.8), "wc": (0.95, 0.8), "utility": (0.93, 0.75)}
# generous upper size (m2) per type: beyond it the extra area is counted as waste (approved p90 x ~1.3)
OVERSIZE = {"master": 20.0, "bedroom": 16.0, "living": 32.0, "dining": 16.0, "kitchen": 11.0, "toilet": 5.5, "bath": 4.0,
            "wc": 2.5, "foyer": 5.5, "passage": 6.5, "utility": 5.0, "study": 10.0, "dress": 6.0, "pooja": 3.0}
# (comfortable, unacceptable) area m2: p25 / p10 of the approved plans, rounded
SIZE = {"master": (11.0, 9.0), "bedroom": (9.5, 7.5), "living": (13.0, 9.5), "kitchen": (5.8, 4.5), "toilet": (2.5, 1.8),
        "dining": (4.5, 2.5), "study": (5.5, 3.5)}


# ------------------------------------------------------------------------------------------------ helpers


def _ramp(v: float, good: float, bad: float) -> float:
    if good == bad:
        return 1.0 if v >= good else 0.0
    t = (v - bad) / (good - bad)
    return float(min(1.0, max(0.0, t)))


def _clean(coords) -> Polygon:
    if len(coords) < 3:
        return Polygon()
    p = Polygon(coords)
    if not p.is_valid:
        p = shapely.make_valid(p)
    if p.geom_type != "Polygon":
        parts = [g for g in getattr(p, "geoms", []) if g.geom_type == "Polygon"]
        p = max(parts, key=lambda g: g.area) if parts else Polygon()
    if p.is_empty:
        return p
    p = p.buffer(0.004, join_style="mitre", mitre_limit=2.0).buffer(-0.004, join_style="mitre", mitre_limit=2.0)
    if p.geom_type == "MultiPolygon":
        p = max(p.geoms, key=lambda g: g.area)
    p = shapely.simplify(p, 0.01)
    if p.geom_type != "Polygon" or p.is_empty:
        return Polygon()
    return orient(p, 1.0)


def _mrr_dims(p: Polygon) -> tuple[float, float, np.ndarray]:
    """(long, short, long-axis unit vector) of the minimum rotated rectangle."""
    r = p.minimum_rotated_rectangle
    cs = np.array(r.exterior.coords)[:4]
    e1, e2 = cs[1] - cs[0], cs[2] - cs[1]
    l1, l2 = float(np.hypot(*e1)), float(np.hypot(*e2))
    if l1 >= l2:
        return l1, l2, e1 / max(l1, 1e-9)
    return l2, l1, e2 / max(l2, 1e-9)


def _rep_point(p: Polygon) -> tuple[float, float]:
    c = p.centroid
    if p.contains(c):
        return (c.x, c.y)
    rp = p.representative_point()
    return (rp.x, rp.y)


def _unit(v) -> np.ndarray:
    v = np.asarray(v, float)
    n = float(np.hypot(*v))
    return v / n if n > 1e-12 else v


def _fmt_room(r: PlanRoom) -> str:
    return r.name or r.id


@dataclass
class _Door:
    op: Opening
    a: np.ndarray
    b: np.ndarray
    w: float
    t: np.ndarray  # hinge -> latch direction
    n: np.ndarray  # leaf swing direction (into swing_into room)
    mid: np.ndarray
    rooms: list[str]
    swing_room: str | None
    swing: Polygon
    kind: str

    @property
    def id(self) -> str:
        return self.op.id


def _swing_poly(hinge: np.ndarray, t: np.ndarray, n: np.ndarray, w: float) -> Polygon:
    pts = [tuple(hinge)]
    for k in range(0, 7):
        th = math.radians(15 * k)
        pts.append(tuple(hinge + w * (math.cos(th) * t + math.sin(th) * n)))
    return Polygon(pts)


class _Ctx:
    """Parsed plan: cleaned room polygons, doors with swings, door graph, wall-touch map."""

    def __init__(self, plan: Plan, unit: Unit | None, plate: Plate | None):
        self.plan, self.unit, self.plate = plan, unit, plate
        self.room: dict[str, PlanRoom] = {r.id: r for r in plan.rooms}
        self.poly: dict[str, Polygon] = {r.id: _clean(r.polygon) for r in plan.rooms}
        self.type: dict[str, str] = {r.id: r.type for r in plan.rooms}
        self.rep: dict[str, tuple[float, float]] = {k: _rep_point(p) for k, p in self.poly.items() if not p.is_empty}
        self.doors: list[_Door] = [d for d in (self._door(o) for o in plan.doors) if d is not None]
        self.entry_door: _Door | None = next((d for d in self.doors if d.kind == "entry"), None)
        self.entry: str | None = None
        if self.entry_door:
            self.entry = next((r for r in self.entry_door.rooms if r in self.room), None)
        self.adj: dict[str, set[str]] = {k: set() for k in self.room}
        for d in self.doors:
            rs = [r for r in d.rooms if r in self.room]
            if d.kind != "entry" and len(rs) == 2 and rs[0] != rs[1]:
                self.adj[rs[0]].add(rs[1])
                self.adj[rs[1]].add(rs[0])
        self._touch: dict[tuple[str, str], float] = {}

    # ----- doors
    def _door(self, o: Opening) -> _Door | None:
        a, b = np.array(o.a, float), np.array(o.b, float)
        w = float(np.hypot(*(b - a)))
        if w < 0.2:
            return None
        rooms = [r for r in o.rooms if r in self.room]
        swing = o.swing_into if o.swing_into in self.room else (rooms[0] if rooms else None)
        poly = self.poly.get(swing) if swing else None
        t = (b - a) / w
        n = np.array([-t[1], t[0]])
        mid = (a + b) / 2
        if poly is not None and not poly.is_empty:
            # the entry door is drawn on the outer face of the wall: pull it to this room's edge
            near = nearest_points(poly.exterior, Point(*mid))[0]
            off = np.array([near.x, near.y]) - mid
            if float(np.hypot(*off)) < 0.45:
                a, b, mid = a + off, b + off, mid + off
            probe = mid + n * 0.35
            if not poly.buffer(0.02).contains(Point(*probe)):
                n = -n
        hinge = a
        if o.hinge is not None:
            hinge = a if math.dist(o.hinge, o.a) <= math.dist(o.hinge, o.b) else b
            if hinge is b:
                t = -t
        return _Door(o, a, b, w, t, n, mid, rooms, swing, _swing_poly(hinge, t, n, w), o.kind)

    def doors_of(self, rid: str) -> list[_Door]:
        return [d for d in self.doors if rid in d.rooms]

    # ----- wall contact
    def touch(self, a: str, b: str) -> float:
        k = (a, b) if a < b else (b, a)
        if k not in self._touch:
            pa, pb = self.poly.get(a), self.poly.get(b)
            if pa is None or pb is None or pa.is_empty or pb.is_empty or pa.distance(pb) > 0.4:
                self._touch[k] = 0.0
            else:
                inter = pa.exterior.buffer(0.3, cap_style="flat").intersection(pb.exterior)
                self._touch[k] = float(inter.length) / 2 if not inter.is_empty else 0.0
        return self._touch[k]

    # ----- graph paths
    def path_len(self, src: str, dst: str, forbid: set[str] | None = None) -> float | None:
        """Shortest walking distance room centre -> door -> room centre through the door graph."""
        forbid = forbid or set()
        dist = {src: 0.0}
        pq = [(0.0, src)]
        door_by_pair: dict[tuple[str, str], _Door] = {}
        for d in self.doors:
            rs = [r for r in d.rooms if r in self.room]
            if d.kind != "entry" and len(rs) == 2:
                door_by_pair[(rs[0], rs[1])] = d
                door_by_pair[(rs[1], rs[0])] = d
        while pq:
            dcur, u = heapq.heappop(pq)
            if u == dst:
                return dcur
            if dcur > dist.get(u, 1e18):
                continue
            if u != src and u in forbid:
                continue
            for v in self.adj.get(u, ()):
                d = door_by_pair.get((u, v))
                if d is None or u not in self.rep or v not in self.rep:
                    continue
                step = math.dist(self.rep[u], tuple(d.mid)) + math.dist(tuple(d.mid), self.rep[v])
                nd = dcur + step
                if nd < dist.get(v, 1e18):
                    dist[v] = nd
                    heapq.heappush(pq, (nd, v))
        return None

    def reach(self, start: str, no_transit: set[str]) -> set[str]:
        """Rooms reachable from ``start``; rooms in ``no_transit`` can be entered but not walked through."""
        seen, stack = {start}, [start]
        while stack:
            u = stack.pop()
            if u != start and u in no_transit:
                continue
            for v in self.adj.get(u, ()):
                if v not in seen:
                    seen.add(v)
                    stack.append(v)
        return seen


# ------------------------------------------------------------------------------------------------ furniture


@dataclass
class _Item:
    name: str
    w: float  # along the wall
    d: float  # depth from the wall
    mode: str = "wall"  # wall | free
    zones: list[tuple[float, float, float, float]] = field(default_factory=list)  # required clear zones (u0,u1,v0,v1)
    alts: list[tuple[float, float, float, float]] = field(default_factory=list)  # alternative zones
    alt_need: int = 0
    tall: bool = False  # may not hide a window
    optional: bool = False
    weight: float = 1.0
    facing: bool = False  # orientation matters (4 facings in free mode)


@dataclass
class _Cand:
    item: int
    body: Polygon
    inner: Polygon
    zones: list[Polygon]
    alts: list[Polygon]
    centre: np.ndarray
    n: np.ndarray  # front direction (away from the back)
    t: np.ndarray
    pref: float
    wall: int


def _local(A, t, n, u0, u1, v0, v1) -> np.ndarray:
    """Corner coordinates (N,4,2) of the local rectangle [u0,u1]x[v0,v1] for N origins A (N,2) with axes t, n."""
    return np.stack([A + t * u0 + n * v0, A + t * u1 + n * v0, A + t * u1 + n * v1, A + t * u0 + n * v1], axis=1)


def _polys(coords: np.ndarray) -> np.ndarray:
    return shapely.polygons(coords)


def _edges(poly: Polygon, min_len: float):
    cs = np.array(poly.exterior.coords)
    out = []
    for i in range(len(cs) - 1):
        a, b = cs[i], cs[i + 1]
        L = float(np.hypot(*(b - a)))
        if L >= min_len:
            t = (b - a) / L
            out.append((a, t, np.array([-t[1], t[0]]), L, i))
    return out


class _Placer:
    """Finds a clash-free arrangement of items in one room (bounded depth-first search over wall/free candidates)."""

    BUDGET = 25000

    def __init__(self, room: Polygon, obstacles: list[Polygon], windows: list[LineString], landings: list[np.ndarray],
                 doors_mid: list[np.ndarray]):
        self.room = room
        self.room_in = room.buffer(0.012)
        self.obst = shapely.union_all(obstacles) if obstacles else Polygon()
        self.win = shapely.union_all([w.buffer(0.12, cap_style="flat") for w in windows]) if windows else Polygon()
        self.landings = landings
        self.door_pts = doors_mid
        self.edges = _edges(room, 0.35)
        self.nodes = 0

    # ----- candidates
    def candidates(self, ix: int, it: _Item, pref_fn, ignore_windows: bool = False) -> list[_Cand]:
        out: list[_Cand] = []
        arrays = []
        if it.mode == "wall":
            for (a, t, n, L, wi) in self.edges:
                if L < it.w - 1e-6:
                    continue
                s = np.append(np.arange(0.0, L - it.w + 1e-9, 0.1), L - it.w)
                s = np.unique(np.round(s, 4))
                A = a + np.outer(s, t)
                arrays.append((A, np.tile(t, (len(s), 1)), np.tile(n, (len(s), 1)), np.full(len(s), wi)))
        else:
            minx, miny, maxx, maxy = self.room.bounds
            ang = []
            for (_a, t, _n, _L, _wi) in self.edges[:6]:
                th = math.degrees(math.atan2(t[1], t[0])) % 90
                if all(abs(th - q) > 2 for q in ang):
                    ang.append(th)
            ang = ang[:2] or [0.0]
            step = 0.15
            gx, gy = np.meshgrid(np.arange(minx, maxx, step), np.arange(miny, maxy, step))
            cen = np.stack([gx.ravel(), gy.ravel()], axis=1)
            cen = cen[shapely.contains(self.room, shapely.points(cen))]
            facings = (0, 1, 2, 3) if it.facing else (0, 1)
            for th0 in ang:
                for f in facings:
                    th = math.radians(th0 + 90 * f)
                    t = np.array([math.cos(th), math.sin(th)])
                    n = np.array([-t[1], t[0]])
                    A = cen - t * it.w / 2 - n * it.d / 2
                    arrays.append((A, np.tile(t, (len(A), 1)), np.tile(n, (len(A), 1)), np.full(len(A), -1)))
        for A, T, N, W in arrays:
            if not len(A):
                continue
            coords = np.stack([A, A + T * it.w, A + T * it.w + N * it.d, A + N * it.d], axis=1)
            body = _polys(coords)
            ok = shapely.contains(self.room_in, body)
            inner0 = shapely.buffer(body, -0.004)
            if not self.obst.is_empty:
                ok &= ~shapely.intersects(inner0, self.obst)
            if it.tall and not self.win.is_empty and not ignore_windows:
                ok &= ~shapely.intersects(inner0, self.win)
            for i in np.nonzero(ok)[0]:
                a, t, n = A[i], T[i], N[i]
                zones = [Polygon(_local(a[None], t, n, *z)[0]) for z in it.zones]
                if not all(self.room_in.contains(z) for z in zones):
                    continue  # a required clear zone (wardrobe front, WC front, foot of the bed) must lie inside the room
                alts = [Polygon(_local(a[None], t, n, *z)[0]) for z in it.alts]
                centre = a + t * it.w / 2 + n * it.d / 2
                out.append(_Cand(ix, Polygon(coords[i]), inner0[i], zones, alts, centre, n, t, 0.0, int(W[i])))
        for c in out:
            c.pref = pref_fn(c)
        out.sort(key=lambda c: c.pref)
        return out

    # ----- search
    def zone_ok(self, z: Polygon, others: list[_Cand], me: _Cand) -> bool:
        if not self.room_in.contains(z):
            return False
        for o in others:
            if o is not me and z.intersects(o.inner):
                return False
        return True

    def compatible(self, c: _Cand, placed: list[_Cand], rel) -> bool:
        for p in placed:
            self.nodes += 1
            if c.inner.intersects(p.inner):
                return False
            for z in c.zones:
                if z.intersects(p.inner):
                    return False
            for z in p.zones:
                if z.intersects(c.inner):
                    return False
            if rel is not None and not rel(c, p):
                return False
        return True

    def solve(self, items: list[_Item], cands: list[list[_Cand]], rel=None, leaf=None, limit: int = 60):
        order = list(range(len(items)))
        order.sort(key=lambda i: (items[i].optional, len(cands[i])))
        # thin the candidates: per wall keep the best ones that are >= 0.35 m apart, plus the corner positions
        div = []
        for cl in cands:
            kept: dict[int, list[_Cand]] = {}
            for c in cl:
                lst = kept.setdefault(c.wall, [])
                if len(lst) < 10 and all(float(np.hypot(*(c.centre - o.centre))) >= 0.35 for o in lst):
                    lst.append(c)
            keep = [c for lst in kept.values() for c in lst]
            ids = {id(c) for c in keep}
            by_wall: dict[int, list[_Cand]] = {}
            for c in cl:
                by_wall.setdefault(c.wall, []).append(c)
            for w, lst in by_wall.items():
                if w < 0:
                    continue
                # corner positions are the natural place for wardrobes / WCs
                for c in (min(lst, key=lambda c: (c.centre[0], c.centre[1])), max(lst, key=lambda c: (c.centre[0], c.centre[1]))):
                    if id(c) not in ids:
                        keep.append(c)
                        ids.add(id(c))
            keep.sort(key=lambda c: c.pref)
            div.append(keep[:limit])
        cands = div
        self.nodes = 0

        def dfs(k: int, placed: list[_Cand]):
            if self.nodes > self.BUDGET:
                return None
            if k == len(order):
                for p in placed:
                    if p.alts and sum(self.zone_ok(z, placed, p) for z in p.alts) < items[p.item].alt_need:
                        return None
                if leaf is not None and not leaf(placed):
                    return None
                return list(placed)
            for c in cands[order[k]]:
                if self.compatible(c, placed, rel):
                    r = dfs(k + 1, placed + [c])
                    if r is not None:
                        return r
            return None

        return dfs(0, [])


def _door_obstacles(ctx: _Ctx, rid: str, land: float = 0.6, swing: bool = True):
    """Swing sectors / landings of every door of this room, as no-furniture polygons, plus landing points."""
    obs: list[Polygon] = []
    pts: list[np.ndarray] = []
    poly = ctx.poly[rid]
    for d in ctx.doors_of(rid):
        # normal pointing into THIS room
        n = d.n if d.swing_room == rid else -d.n
        if not poly.buffer(0.02).contains(Point(*(d.mid + n * 0.35))):
            n = -n
        rect = Polygon([d.a - n * 0.1, d.b - n * 0.1, d.b + n * land, d.a + n * land])
        obs.append(rect)
        if d.swing_room == rid and swing:
            obs.append(d.swing)
        pts.append(d.mid + n * 0.5)
    return obs, pts


def _windows_of(ctx: _Ctx, rid: str) -> list[LineString]:
    return [LineString([w.a, w.b]) for w in ctx.plan.windows if rid in w.rooms]


def _route_leaf(room: Polygon, pts: list[np.ndarray]):
    """Leaf check: with the furniture placed, all door landings must stay connected by a 0.7 m clear route."""
    if len(pts) < 2:
        return None

    def leaf(placed: list[_Cand]) -> bool:
        free = room.difference(shapely.union_all([c.body for c in placed]) if placed else Polygon())
        er = free.buffer(-0.33)
        if er.is_empty:
            return False
        comps = list(getattr(er, "geoms", [er]))
        lab = []
        for p in pts:
            dists = [(c.distance(Point(*p)), i) for i, c in enumerate(comps)]
            dm, im = min(dists)
            if dm > 0.7:
                continue  # landing swallowed by the room edge; judged by its own obstacle
            lab.append(im)
        return len(set(lab)) <= 1

    return leaf


def _bed_pref(win_union, door_pts, room: Polygon):
    cx = room.centroid

    def pref(c: _Cand) -> float:
        s = 0.0
        back = c.centre - c.n * 1.0  # point on the wall behind the headboard
        if win_union is not None and not win_union.is_empty:
            if win_union.distance(Point(*back)) < 0.9:
                s += 2.0
        for p in door_pts:
            s -= 0.15 * math.dist(p, tuple(c.centre))
        s += 0.2 * Point(*c.centre).distance(cx)
        return s

    return pref


def _simple_pref(room: Polygon, door_pts, away_from_door=False, win_union=None, avoid_win=False):
    def pref(c: _Cand) -> float:
        s = 0.0
        d = min((math.dist(p, tuple(c.centre)) for p in door_pts), default=3.0)
        s += (-0.1 * d) if away_from_door else (0.1 * d)
        if avoid_win and win_union is not None and not win_union.is_empty and win_union.distance(Polygon(c.body)) < 0.3:
            s += 1.0
        return s

    return pref


@dataclass
class RoomFit:
    room: str
    kind: str
    tier: str  # full | tight | none | n/a
    score: float
    items: list[dict] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    note: str = ""


def _poly_to_list(p: Polygon) -> list[list[float]]:
    return [[round(x, 3), round(y, 3)] for x, y in list(p.exterior.coords)[:-1]]


def _placed_json(items: list[_Item], placed: list[_Cand]) -> list[dict]:
    return [{"name": items[c.item].name, "polygon": _poly_to_list(c.body),
             "zones": [_poly_to_list(z) for z in c.zones],
             "alts": [_poly_to_list(z) for z in c.alts]} for c in placed]


def _run_tiers(ctx: _Ctx, rid: str, kind: str, build, tiers=(("full", 1.0), ("tight", 0.75)),
               tight_outward: bool = False) -> RoomFit:
    """build(scale, tier, placer, pts) -> (items, prefs, rel, leaf).

    Attempts in order: full clearances with every item, tight clearances with every item, full clearances without
    the optional items (wardrobe), tight without them; the first complete attempt wins, else the best-scoring one
    (score = share of item weight placed x 1.0 for full / 0.75 for tight clearances)."""
    poly = ctx.poly[rid]
    obs, pts = _door_obstacles(ctx, rid)
    wins = _windows_of(ctx, rid)
    placer = _Placer(poly, obs, wins, pts, [d.mid for d in ctx.doors_of(rid)])
    placers = {tiers[0][0]: placer}
    for tier, _ in tiers[1:]:
        if tight_outward:  # tight tier of a wet room: the door is assumed to open outward (only its landing is kept clear)
            o2, p2 = _door_obstacles(ctx, rid, land=0.35, swing=False)
            placers[tier] = _Placer(poly, o2, wins, p2, [d.mid for d in ctx.doors_of(rid)])
        else:
            placers[tier] = placer
    built = {}
    for tier, scale in tiers:
        built[tier] = build(scale, tier, placers[tier], pts)
    any_items = built[tiers[0][0]][0]
    has_opt = any(it.optional for it in any_items)
    attempts = [(t, False) for t, _ in tiers] + ([(t, True) for t, _ in tiers] if has_opt else [])
    best: RoomFit | None = None
    for tier, drop_opt in attempts:
        items, prefs, rel, leaf_fn = built[tier]
        placer = placers[tier]
        keep = [i for i, it in enumerate(items) if not (drop_opt and it.optional)]
        sub_items = [items[i] for i in keep]
        remap = {old: new for new, old in enumerate(keep)}
        sub_c = []
        for old in keep:
            cl = placer.candidates(remap[old], items[old], prefs[old])
            sub_c.append(cl)
        placer.nodes = 0
        leaf = leaf_fn(sub_items) if leaf_fn else None
        sol = placer.solve(sub_items, sub_c, rel=(lambda a, b, _r=rel, _s=sub_items: _r(a, b, _s)) if rel else None, leaf=leaf)
        if sol is None:
            continue
        total_w = sum(it.weight for it in items)
        got_w = sum(it.weight for it in sub_items)
        score = got_w / total_w * (1.0 if tier == "full" else 0.75)
        missing = [it.name for it in items if it not in sub_items]
        cur = RoomFit(rid, kind, "full" if (tier == "full" and not missing) else "tight", round(score, 3),
                      _placed_json(sub_items, sol), missing,
                      "door must open outward" if tier != tiers[0][0] and tight_outward else "")
        if score >= 0.999:
            return cur
        if best is None or cur.score > best.score + 1e-9:
            best = cur
    if best is not None:
        return best
    return RoomFit(rid, kind, "none", 0.0, [], [it.name for it in any_items])


# ---- per-room furniture programmes ---------------------------------------------------------------------------------


def _fit_bedroom(ctx: _Ctx, rid: str, master: bool, bhk: int) -> RoomFit:
    poly = ctx.poly[rid]
    wins = shapely.union_all([w.buffer(0.1) for w in _windows_of(ctx, rid)]) if _windows_of(ctx, rid) else None

    def build(scale, tier, placer, pts):
        bw, bl = (1.8, 2.0) if master else (1.5, 1.9)
        side = 0.6
        bed = _Item("bed", bw, bl, zones=[(0, bw, bl, bl + 0.6)],
                    alts=[(-side, 0, 0, bl), (bw, bw + side, 0, bl)], alt_need=2 if tier == "full" else 1, weight=3.0)
        ww = 1.8 if master else 1.2
        wr = _Item("wardrobe", ww, 0.6, zones=[(0, ww, 0.6, 1.35)], tall=True, optional=True, weight=1.0)
        items = [bed, wr]
        prefs = [_bed_pref(wins, pts, poly), _simple_pref(poly, pts, away_from_door=False)]
        return items, prefs, None, lambda sub: _route_leaf(poly, pts)

    return _run_tiers(ctx, rid, "master" if master else "bedroom", build)


def _fit_living(ctx: _Ctx, rid: str) -> RoomFit:
    """TV unit on a wall + a 3-seat sofa facing it (back on a wall or floating), viewing distance 1.8-4.5 m
    (tight: 1.4-5.5 m, 0.3 m coffee-table gap), doors and their routes kept clear."""
    poly = ctx.poly[rid]
    obs, pts = _door_obstacles(ctx, rid)
    wl = _windows_of(ctx, rid)
    wins = shapely.union_all([w.buffer(0.1) for w in wl]) if wl else None
    placer = _Placer(poly, obs, wl, pts, [d.mid for d in ctx.doors_of(rid)])
    tv_item = _Item("tv unit", 1.8, 0.45, tall=True)
    sofa_item = _Item("sofa 3-seat", 2.1, 0.9)
    tvs = placer.candidates(0, tv_item, _simple_pref(poly, pts, True, wins, True))
    # tight tier only: a TV unit in front of a window (curtained) is common when the room has no other TV wall
    tvs_loose = placer.candidates(0, tv_item, _simple_pref(poly, pts, True, wins, True), ignore_windows=True) if wl else tvs
    leaf = _route_leaf(poly, pts)
    tried = 0
    for tier, (dmin, dmax), gap in (("full", (1.8, 4.5), 0.45), ("tight", (1.4, 5.5), 0.3)):
        for tv in (tvs if tier == "full" else tvs_loose)[:90]:
            for dist in np.arange(dmin, dmax + 1e-9, 0.2):
                for lat in (0.0, 0.4, -0.4, 0.8, -0.8):
                    tried += 1
                    n = -tv.n
                    t = np.array([n[1], -n[0]])
                    c = tv.centre + tv.n * (0.225 + dist + 0.45) + tv.t * lat
                    A = c - t * 1.05 - n * 0.45
                    body = Polygon(_local(A[None], t, n, 0, 2.1, 0, 0.9)[0])
                    if not placer.room_in.contains(body):
                        continue
                    inner = body.buffer(-0.004)
                    if not placer.obst.is_empty and inner.intersects(placer.obst):
                        continue
                    zone = Polygon(_local(A[None], t, n, 0, 2.1, 0.9, 0.9 + gap)[0])
                    if not placer.room_in.contains(zone) or zone.intersects(tv.inner):
                        continue
                    sofa = _Cand(1, body, inner, [zone], [], c, n, t, 0.0, -1)
                    if leaf is not None and not leaf([tv, sofa]):
                        continue
                    items = [tv_item, sofa_item]
                    return RoomFit(rid, "living", tier, 1.0 if tier == "full" else 0.75, _placed_json(items, [tv, sofa]), [],
                                   "" if tier == "full" else "viewing distance / coffee-table gap reduced (TV may stand in front of a window)")
    return RoomFit(rid, "living", "none", 0.0, [], ["sofa set + TV wall"])


def _fit_dining(ctx: _Ctx, rid: str, seats: int) -> RoomFit:
    poly = ctx.poly[rid]

    def build(scale, tier, placer, pts):
        w, d = (1.8, 0.9) if seats == 6 else (1.2, 0.8)
        c = 0.9 if tier == "full" else 0.75
        t = _Item(f"dining table {seats}", w, d, mode="free",
                  alts=[(-c, 0, 0, d), (w, w + c, 0, d), (0, w, -c, 0), (0, w, d, d + c)],
                  alt_need=4 if tier == "full" else 3, weight=1.0)
        return [t], [lambda cnd: Point(*cnd.centre).distance(poly.centroid)], None, lambda sub: _route_leaf(poly, pts)

    r = _run_tiers(ctx, rid, "dining", build, tiers=(("full", 1.0), ("tight", 1.0)))
    if r.tier == "none" and seats == 6:  # a 6-seater does not fit: 4 seats is the fallback (tight)
        r4 = _fit_dining(ctx, rid, 4)
        if r4.tier != "none":
            r4.tier, r4.score = "tight", min(r4.score, 0.6)
            r4.note = "only a 4-seat table fits"
            return r4
    return r


def _fit_toilet(ctx: _Ctx, rid: str, rtype: str) -> RoomFit:
    poly = ctx.poly[rid]

    def build(scale, tier, placer, pts):
        full = tier == "full"
        sd = 0.2 if full else 0.15
        fz = 0.6 if full else 0.45
        wc = _Item("WC", 0.4, 0.7, zones=[(-sd, 0.4 + sd, 0, 0.7 + fz)], weight=1.0)
        # (zones include the sides: they must lie in the room, i.e. WC centre >= 0.4 m from a side wall)
        wc.zones = [(-sd, 0.4 + sd, 0.7, 0.7 + fz), (-sd, 0, 0, 0.7), (0.4, 0.4 + sd, 0, 0.7)]
        bw = _Item("basin", 0.5, 0.4, zones=[(0, 0.5, 0.4, 0.4 + (0.5 if full else 0.4))], weight=0.5)
        sh = _Item("shower 0.9x0.9", 0.9, 0.9, weight=0.7)
        if rtype == "wc":
            items = [wc]
        elif rtype == "bath":
            items = [sh, bw]
        else:
            items = [wc, sh, bw]
        return items, [_simple_pref(poly, pts) for _ in items], None, None

    return _run_tiers(ctx, rid, rtype, build, tight_outward=True)


def _fit_utility(ctx: _Ctx, rid: str) -> RoomFit:
    poly = ctx.poly[rid]

    def build(scale, tier, placer, pts):
        wm = _Item("washing machine", 0.65, 0.65, zones=[(0, 0.65, 0.65, 1.15)], weight=1.0)
        return [wm], [_simple_pref(poly, pts)], None, None

    return _run_tiers(ctx, rid, "utility", build)


def _fit_study(ctx: _Ctx, rid: str) -> RoomFit:
    poly = ctx.poly[rid]

    def build(scale, tier, placer, pts):
        desk = _Item("desk", 1.2, 0.6, zones=[(0, 1.2, 0.6, 1.4)], weight=1.0)
        return [desk], [_simple_pref(poly, pts)], None, None

    return _run_tiers(ctx, rid, "study", build)


def _fit_kitchen(ctx: _Ctx, rid: str) -> RoomFit:
    """Counter run along wall edges (0.6 deep) chosen from edge subsets; an aisle >= 0.9 m must remain."""
    poly = ctx.poly[rid]
    obs, pts = _door_obstacles(ctx, rid)
    forb = shapely.union_all(obs).buffer(0.03) if obs else Polygon()
    edges = _edges(poly, 0.5)
    strips: list[Polygon] = []
    for (a, t, n, L, _wi) in edges:
        strip = Polygon(_local(a[None], t, n, 0, L, 0, 0.6)[0])
        if not forb.is_empty:
            strip = strip.difference(forb)
        # drop pieces shorter than a base unit (0.5 m)
        parts = [g for g in getattr(strip, "geoms", [strip]) if g.geom_type == "Polygon" and g.area > 0.5 * 0.6 * 0.9]
        strips.append(shapely.union_all(parts) if parts else Polygon())
    idx = [i for i, s in enumerate(strips) if not s.is_empty]
    valid = []  # (run, aisle, subset)
    for r in range(1, min(4, len(idx)) + 1):
        for sub in itertools.combinations(idx, r):
            u = shapely.union_all([strips[i] for i in sub])
            run = u.area / 0.6
            clear = poly.difference(u)
            if clear.is_empty:
                continue
            try:
                aisle = 2 * shapely.maximum_inscribed_circle(clear).length
            except Exception:  # pragma: no cover
                aisle = 0.0
            if aisle >= 0.9 - 1e-6:
                valid.append((run, aisle, sub))
    run, aisle, chosen = 0.0, 0.0, ()
    if valid:
        run, aisle, chosen = max(valid, key=lambda t: t[0])
        # draw the simplest layout that is already a full kitchen (fewest walls with >= 3.0 m of counter)
        full = [t for t in valid if t[0] >= 3.0]
        if full:
            _r, _a, chosen = min(full, key=lambda t: (len(t[2]), -t[0]))
    items = []
    for i in chosen:
        for g in getattr(strips[i], "geoms", [strips[i]]):
            if g.geom_type == "Polygon":
                items.append({"name": "counter", "polygon": _poly_to_list(g), "zones": [], "alts": []})
    note = f"counter run {run:.1f} m, aisle {aisle:.2f} m"
    if run >= 2.4:
        return RoomFit(rid, "kitchen", "full", 1.0, items, [], note)
    if run >= 1.8:
        return RoomFit(rid, "kitchen", "tight", 0.5, items, ["counter run >= 2.4 m"], note)
    return RoomFit(rid, "kitchen", "none", 0.0, items, ["counter run >= 2.4 m"], note)


def place_furniture(ctx_or_plan, unit: Unit | None = None, plate: Plate | None = None) -> dict[str, RoomFit]:
    """Furniture fit for every room that takes furniture: {room id: RoomFit}."""
    ctx = ctx_or_plan if isinstance(ctx_or_plan, _Ctx) else _Ctx(ctx_or_plan, unit, plate)
    bhk = sum(1 for r in ctx.plan.rooms if r.type in BED)
    out: dict[str, RoomFit] = {}
    for r in ctx.plan.rooms:
        p = ctx.poly[r.id]
        if p.is_empty or p.area < 0.8:
            continue
        try:
            if r.type in BED:
                out[r.id] = _fit_bedroom(ctx, r.id, r.type == "master", bhk)
            elif r.type == "living":
                out[r.id] = _fit_living(ctx, r.id)
            elif r.type == "dining":
                out[r.id] = _fit_dining(ctx, r.id, 6 if bhk >= 3 else 4)
            elif r.type == "kitchen":
                out[r.id] = _fit_kitchen(ctx, r.id)
            elif r.type in WET:
                out[r.id] = _fit_toilet(ctx, r.id, r.type)
            elif r.type == "utility" and r.area >= 1.0:
                out[r.id] = _fit_utility(ctx, r.id)
            elif r.type == "study":
                out[r.id] = _fit_study(ctx, r.id)
        except Exception as e:  # pragma: no cover - a crashing room must not hide the other rooms
            out[r.id] = RoomFit(r.id, r.type, "n/a", 1.0, [], [], f"furniture engine error: {e}")
    return out


# ------------------------------------------------------------------------------------------------ the critic

SEV_ORDER = {"critical": 0, "major": 1, "minor": 2, "info": 3}


class _Out:
    def __init__(self, ctx: _Ctx):
        self.ctx = ctx
        self.values: dict[str, float] = {}
        self.sat: dict[str, float] = {}
        self.issues: list[dict[str, Any]] = []
        self.stats: dict[str, Any] = {}
        self.errors: list[str] = []

    def set(self, key: str, value: float | None, sat: float | None = None):
        if value is None:
            return
        m = METRICS[key]
        self.values[key] = round(float(value), 3)
        self.sat[key] = _ramp(float(value), m.good, m.bad) if sat is None else float(sat)

    def issue(self, code: str, sev: str, metric: str | None, msg: str, rooms: list[str] | None = None, at=None):
        cat = METRICS[metric].cat if metric in METRICS else "general"
        pt = None
        if at is not None:
            pt = [round(float(at[0]), 3), round(float(at[1]), 3)]
        elif rooms:
            ps = [self.ctx.rep[r] for r in rooms if r in self.ctx.rep]
            if ps:
                pt = [round(sum(p[0] for p in ps) / len(ps), 3), round(sum(p[1] for p in ps) / len(ps), 3)]
        self.issues.append({"code": code, "severity": sev, "category": cat, "metric": metric, "message": msg,
                            "rooms": rooms or [], "at": pt})


def _name(ctx: _Ctx, rid: str) -> str:
    r = ctx.room.get(rid)
    return f"{r.name} ({rid})" if r and r.name and r.name.lower() != rid.lower() else (r.name if r else rid)


def _circulation(o: _Out, net: float):
    ctx = o.ctx
    circ = [r for r in ctx.plan.rooms if r.type in CIRC]
    ca = sum(r.area for r in circ)
    o.stats["circ_area"] = round(ca, 2)
    share = 100 * ca / net if net > 0 else 0.0
    o.set("circ_share", share)
    if share > 12:
        o.issue("circ_share_high", "major", "circ_share",
                f"Passage/foyer take {share:.0f}% of the flat ({ca:.1f} m2): too much circulation, aim for under 6%",
                [r.id for r in circ])
    elif share > 8:
        o.issue("circ_share_high", "minor", "circ_share", f"Passage/foyer take {share:.0f}% of the flat ({ca:.1f} m2)",
                [r.id for r in circ])
    # corridor length and dead ends
    clen = 0.0
    worst_dead = 0.0
    for r in circ:
        p = ctx.poly[r.id]
        if p.is_empty:
            continue
        L, S, u = _mrr_dims(p)
        if S < 1e-6 or L / S < 2.0:
            continue
        clen += r.area / max(r.width, 0.8)
        pos = []
        for d in ctx.doors_of(r.id):
            pos.append(float(np.dot(d.mid, u)))
        if pos:
            proj = np.asarray(p.exterior.coords) @ u
            lo, hi = float(proj.min()), float(proj.max())
            dead = max(min(pos) - lo, hi - max(pos)) - 0.3
            if dead > worst_dead:
                worst_dead = dead
                o.stats["dead_end_room"] = r.id
    o.set("corridor_len", clen)
    if clen > 4.5:
        o.issue("corridor_long", "major" if clen > 6 else "minor", "corridor_len",
                f"Corridor-like circulation about {clen:.1f} m long: doors should open on a compact hub",
                [r.id for r in circ])
    if circ:
        o.set("dead_end", max(0.0, worst_dead))
        if worst_dead > 1.5:
            rid = o.stats.get("dead_end_room")
            o.issue("dead_end", "major" if worst_dead > 2.5 else "minor", "dead_end",
                    f"Dead-end passage: {worst_dead:.1f} m beyond the last door is wasted floor", [rid] if rid else None)
    else:
        o.set("dead_end", 0.0)
    # hub: doors of private rooms
    priv = []
    for d in ctx.doors:
        rs = [r for r in d.rooms if r in ctx.room]
        if d.kind == "entry" or len(rs) != 2:
            continue
        ta, tb = ctx.type[rs[0]], ctx.type[rs[1]]
        for _me, other, tm, to in ((rs[0], rs[1], ta, tb), (rs[1], rs[0], tb, ta)):
            if tm in BED | {"study"} | WET and to not in BED | WET | {"dress", "study"}:
                priv.append((d, other))
                break
    if len(priv) >= 2:
        pts = [p[0].mid for p in priv]
        spread = max(float(np.hypot(*(a - b))) for a, b in itertools.combinations(pts, 2))
        cnt: dict[str, int] = {}
        for _d, other in priv:
            cnt[other] = cnt.get(other, 0) + 1
        top, k = max(cnt.items(), key=lambda kv: kv[1])
        share_h = k / len(priv)
        o.set("hub_spread", spread)
        o.set("hub_share", share_h)
        o.stats.update(hub_room=top, hub_doors=len(priv))
        if spread > 8:
            o.issue("hub_spread", "major" if spread > 10 else "minor", "hub_spread",
                    f"Bedroom/toilet doors are spread over {spread:.1f} m: no compact hub", list(cnt))
        if share_h < 0.5:
            o.issue("hub_split", "minor", "hub_share",
                    f"Private doors open on {len(cnt)} different spaces (best hub serves {share_h:.0%})", list(cnt))
    elif len(priv) == 1:
        o.set("hub_spread", 0.0)
        o.set("hub_share", 1.0)
    # travel to exit
    if ctx.entry and (ctx.plate is not None or ctx.unit is not None):
        far, far_room = 0.0, None
        for r in ctx.plan.rooms:
            if r.type in HAB | BED:
                dpath = ctx.path_len(ctx.entry, r.id)
                if dpath is not None and ctx.entry_door is not None:
                    dpath += math.dist(ctx.rep.get(ctx.entry, tuple(ctx.entry_door.mid)), tuple(ctx.entry_door.mid))
                    if dpath > far:
                        far, far_room = dpath, r.id
        ext = None
        if ctx.plate is not None:
            stairs = [Polygon(c.polygon) for c in ctx.plate.core_parts if c.kind == "stair" and len(c.polygon) >= 3]
            if not stairs:
                stairs = [LineString(c.polygon) for c in ctx.plate.core_parts if c.kind == "stair" and len(c.polygon) == 2]
            src = ctx.unit.core_link if ctx.unit is not None and ctx.unit.core_link else tuple(ctx.entry_door.mid)
            if stairs:
                ext = 1.25 * min(s.distance(Point(*src)) for s in stairs)
        if ext is not None:
            o.stats.update(inflat_travel=round(far, 1), exit_distance=round(ext, 1))
            o.set("travel_to_exit", far + ext)
            if far + ext > 30:
                o.issue("travel_far", "major" if far + ext > 40 else "minor", "travel_to_exit",
                        f"Travel from {_name(ctx, far_room) if far_room else 'the farthest room'} to the stair is "
                        f"{far + ext:.0f} m (NBC 22.5 m, 30 m sprinklered)", [far_room] if far_room else None)


def _waste(o: _Out, net: float):
    ctx = o.ctx
    asp_num = asp_den = shp_num = shp_den = 0.0
    prog_ids = {r.id for r in ctx.unit.programme.rooms} if ctx.unit is not None and ctx.unit.programme else None
    waste = 0.0
    size_num = size_den = 0.0
    for r in ctx.plan.rooms:
        p = ctx.poly[r.id]
        if p.is_empty:
            continue
        L, S, _u = _mrr_dims(p)
        asp = L / max(S, 1e-6)
        rect = p.area / max(p.minimum_rotated_rectangle.area, 1e-6)
        verts = len(p.exterior.coords) - 1
        if r.type in ASPECT:
            g, b = ASPECT[r.type]
            s = _ramp(asp, g, b)
            asp_num += s * r.area
            asp_den += r.area
            if s < 0.45:
                o.issue("aspect", "major" if s < 0.15 else "minor", "room_aspect",
                        f"{_name(ctx, r.id)}: {L:.1f} x {S:.1f} m is a long thin room (ratio {asp:.1f}:1)", [r.id])
        if r.type in SHAPE:
            g, b = SHAPE[r.type]
            s = _ramp(rect, g, b)
            if verts > 8:
                s = min(s, 0.5)
            shp_num += s * r.area
            shp_den += r.area
            if s < 0.5:
                o.issue("room_shape", "major" if s < 0.2 else "minor", "room_shape",
                        f"{_name(ctx, r.id)}: odd shape ({verts} corners, fills {rect:.0%} of its bounding rectangle): notch or pocket",
                        [r.id])
        if r.type in SIZE:
            good, bad = SIZE[r.type]
            s = _ramp(r.area, good, bad)
            size_num += s * r.area
            size_den += r.area
            if r.area < bad:
                o.issue("room_small", "major", "room_size",
                        f"{_name(ctx, r.id)}: {r.area:.1f} m2 is below the {bad:g} m2 the approved plans rarely go under", [r.id])
            elif r.area < good - 0.5 and r.type in BED | {"living"}:
                o.issue("room_small", "minor", "room_size", f"{_name(ctx, r.id)}: {r.area:.1f} m2 is tight (usual {good:g} m2+)", [r.id])
        # waste: leftover rooms and bloat vs target
        leftover = (r.id not in prog_ids) if prog_ids is not None else (r.type == "store" and r.id.startswith("store"))
        if r.type == "balcony":
            continue
        if leftover and r.type in ("store", "passage", "foyer", "utility"):
            waste += r.area
            if r.area >= 1.0:
                o.issue("leftover", "major" if r.area > 3 else "minor", "waste_share",
                        f"{_name(ctx, r.id)}: {r.area:.1f} m2 of leftover floor that is not in the brief", [r.id])
        else:
            cap = OVERSIZE.get(r.type, 1e9)
            if r.target_area:
                cap = min(cap, 1.3 * r.target_area + 0.5)
            ex = r.area - cap
            if ex > 0:
                waste += ex
                if ex > 2.0:
                    ref = f"target {r.target_area:.1f} m2" if r.target_area and cap < OVERSIZE.get(r.type, 1e9) else f"usual maximum {OVERSIZE.get(r.type, 0):g} m2"
                    o.issue("bloated", "major" if ex > 5 else "minor", "waste_share",
                            f"{_name(ctx, r.id)}: {r.area:.1f} m2 ({ref}): {ex:.1f} m2 of it is wasted", [r.id])
    o.set("room_aspect", asp_num / asp_den if asp_den else None)
    o.set("room_shape", shp_num / shp_den if shp_den else None)
    o.set("room_size", size_num / size_den if size_den else None)
    wp = 100 * waste / net if net > 0 else 0.0
    o.stats["waste_area"] = round(waste, 2)
    o.set("waste_share", wp)
    if ctx.unit is not None and ctx.unit.area > 0:
        eff = net / ctx.unit.area
        o.stats["gross"] = round(ctx.unit.area, 1)
        o.set("efficiency", eff)
        if eff < 0.7:
            o.issue("efficiency", "major" if eff < 0.65 else "minor", "efficiency",
                    f"Net rooms are {eff:.0%} of the {ctx.unit.area:.0f} m2 flat: walls, voids or unassigned floor take the rest")
        # voids: floor in the flat that is neither room nor wall
        try:
            rooms_u = shapely.union_all([p for p in ctx.poly.values() if not p.is_empty])
            walls_u = shapely.union_all([Polygon(w.polygon[0]).buffer(0) for w in ctx.plan.walls if w.polygon and len(w.polygon[0]) >= 3]) \
                if ctx.plan.walls else Polygon()
            up = Polygon(ctx.unit.polygon).buffer(0)
            cuts = [LineString([w.a, w.b]).buffer(0.32, cap_style="flat") for w in ctx.plan.windows]
            cuts += [LineString([d.a, d.b]).buffer(0.2, cap_style="flat") for d in ctx.doors]
            void = up.difference(rooms_u).difference(walls_u)
            if cuts:
                void = void.difference(shapely.union_all(cuts))
            void = void.buffer(-0.13, join_style="mitre").buffer(0.13, join_style="mitre")
            if void.area > 0.5:
                o.stats["void_area"] = round(void.area, 2)
                o.issue("void", "minor", "efficiency", f"{void.area:.1f} m2 of the flat is neither room nor wall (unusable void)",
                        at=(void.centroid.x, void.centroid.y))
        except Exception:  # pragma: no cover
            pass


def _sequence(o: _Out):
    ctx = o.ctx
    rooms = ctx.plan.rooms
    types = ctx.type
    by_type: dict[str, list[str]] = {}
    for r in rooms:
        by_type.setdefault(r.type, []).append(r.id)
    living = by_type.get("living", [])
    beds = [r.id for r in rooms if r.type in BED]
    kitchens = by_type.get("kitchen", [])
    toilets = [r.id for r in rooms if r.type in WET]
    if not living:
        o.issue("no_living", "critical", "entry_seq", "The flat has no living room")
    if not kitchens:
        o.issue("no_kitchen", "critical", "kitchen_near", "The flat has no kitchen")
    if not beds:
        o.issue("no_bedroom", "critical", "privacy", "The flat has no bedroom")
    if not toilets:
        o.issue("no_toilet", "critical", "common_toilet", "The flat has no toilet")

    # --- entry sequence
    e = ctx.entry
    if e is None:
        o.issue("no_entry", "critical", "entry_seq", "No entry door from the lobby")
        o.set("entry_seq", 0.0)
    else:
        te = types[e]
        nbr = {types[x] for x in ctx.adj[e]}
        if te == "living":
            s = 1.0
        elif te == "foyer":
            s = 1.0 if "living" in nbr else 0.85 if "dining" in nbr else 0.5 if "passage" in nbr else 0.3
            if s < 0.85:
                o.issue("entry_foyer", "major", "entry_seq", f"The entrance foyer does not lead to the living room (it leads to {sorted(nbr)})", [e])
        elif te == "dining":
            s = 0.85 if "living" in nbr else 0.6
        elif te == "passage":
            s = 0.45 if "living" in nbr else 0.15
            o.issue("entry_passage", "major", "entry_seq", "The front door opens into a passage, not the living room or a foyer", [e])
        else:
            s = 0.0
            o.issue("entry_wrong", "critical", "entry_seq",
                    f"The front door opens into the {_name(ctx, e)}: you must enter into the living room or a foyer", [e])
        o.set("entry_seq", s)

    # --- kitchen near living/dining
    if kitchens:
        scores = []
        for k in kitchens:
            best = 0.0
            lv = living + by_type.get("dining", [])
            if any(x in lv for x in ctx.adj[k]):
                best = 1.0
            elif any(ctx.touch(k, x) >= 0.6 for x in lv):
                best = 0.7
            else:
                for x in lv:
                    pl = ctx.path_len(k, x, forbid=set(beds) | set(toilets))
                    if pl is not None:
                        best = max(best, 0.55 if pl <= 8 else 0.25)
            # kitchen only reachable through a bedroom
            if e is not None and k not in ctx.reach(e, set(beds) | set(toilets)) - {e} and k != e:
                best = 0.0
                o.issue("kitchen_via_room", "critical", "kitchen_near",
                        f"{_name(ctx, k)} can only be reached through a bedroom or toilet", [k])
            elif best < 0.6:
                o.issue("kitchen_far", "major", "kitchen_near",
                        f"{_name(ctx, k)} is not next to the living/dining room (needs to be close, with the door to dining or living)", [k])
            elif best < 1.0:
                o.issue("kitchen_door", "minor", "kitchen_near",
                        f"{_name(ctx, k)} shares a wall with living/dining but its door opens elsewhere", [k])
            scores.append(best)
        o.set("kitchen_near", min(scores))

    # --- powder / common toilet
    if toilets and living:
        cands = [t for t in toilets if any(types[x] not in BED | {"dress", "study"} | WET for x in ctx.adj[t])]
        best = 0.0
        if cands and e is not None:
            for t in cands:
                reach = ctx.reach(living[0], set(beds) | set(kitchens))
                if t in reach:
                    pl = ctx.path_len(living[0], t, forbid=set(beds) | set(kitchens))
                    s = 1.0 if pl is not None and pl <= 9.0 else 0.6
                    best = max(best, s)
        if best == 0.0:
            o.issue("no_common_toilet", "major", "common_toilet",
                    "No toilet a guest can reach from the living room without entering a bedroom (add a powder room or open a toilet to the passage)")
            best = 0.2 if toilets else 0.0
        elif best < 1.0:
            o.issue("common_toilet_far", "minor", "common_toilet", "The common toilet is far from the living room (more than 9 m of walking)")
        o.set("common_toilet", best)

    # --- privacy and reachability
    if e is not None and (beds or rooms):
        reach_all = ctx.reach(e, set())
        for r in rooms:
            if r.id not in reach_all and r.id != e and r.type != "balcony":
                sealed = r.type == "store"  # a leftover pocket: waste, reported by the waste metric as well
                o.issue("unreachable", "major" if sealed else "critical", "through_wet",
                        f"{_name(ctx, r.id)} has no door route from the flat entry", [r.id])
        ok_priv = 0
        priv_pen = 0.0
        entry_pt = ctx.entry_door.mid if ctx.entry_door is not None else None
        no_bed = set(beds) | set(kitchens) | set(toilets) | set(by_type.get("utility", []))
        reach_priv = ctx.reach(e, no_bed) | {e}
        for b in beds:
            if b in reach_priv:
                ok_priv += 1
                # bedroom door right next to the front door
                if entry_pt is not None:
                    for d in ctx.doors_of(b):
                        if d.kind != "entry" and ctx.entry in d.rooms and float(np.hypot(*(d.mid - entry_pt))) < 2.0:
                            priv_pen += 0.25
                            o.issue("bedroom_at_entry", "minor", "privacy",
                                    f"{_name(ctx, b)} door is within 2 m of the front door: bedrooms should be away from the entry", [b])
                            break
            elif b in reach_all:
                before = ctx.reach(e, {b})  # rooms reachable without walking through this bedroom
                via = [x for x in ctx.adj[b] if x in before and (types[x] in BED or types[x] == "kitchen" or x in toilets)]
                o.issue("bedroom_via", "critical", "privacy",
                        f"{_name(ctx, b)} is entered only through {', '.join(_name(ctx, x) for x in via) or 'another room'} "
                        f"(bedroom behind a bedroom, kitchen or toilet)", [b] + via)
        if beds:
            o.set("privacy", max(0.0, (ok_priv - priv_pen) / len(beds)))
        # rooms that can only be reached through a toilet (dress rooms and balconies off a bedroom are fine)
        reach_dry = ctx.reach(e, set(toilets))
        bad = [r.id for r in rooms if r.id in reach_all and r.id not in reach_dry and r.type not in WET | {"dress", "balcony"}]
        for rid in bad:
            o.issue("through_toilet", "major" if ctx.type[rid] == "store" else "critical", "through_wet",
                    f"{_name(ctx, rid)} can only be reached through a toilet", [rid])
        o.set("through_wet", 0.0 if bad else 1.0)
        # a toilet that connects two non-wet spaces is a through-route
        for t in toilets:
            others = [x for x in ctx.adj[t] if types[x] not in WET | {"dress"}]
            if len(others) >= 2 and not (all(types[x] in BED for x in others) and len(others) == 2):
                o.issue("toilet_through", "minor", "through_wet", f"{_name(ctx, t)} has doors to {len(others)} different rooms", [t])
            # toilet door straight into the kitchen / dining
            if any(types[x] in {"kitchen"} for x in ctx.adj[t]):
                o.issue("toilet_to_kitchen", "major", "through_wet", f"{_name(ctx, t)} opens directly into the kitchen", [t])

    # --- attached toilets
    if beds:
        att = [b for b in beds if any(types[x] in WET for x in ctx.adj[b])]
        o.set("attached_share", len(att) / len(beds))
        for b in beds:
            if b not in att and types[b] == "master":
                o.issue("master_no_toilet", "major", "master_attached", f"{_name(ctx, b)} has no attached toilet", [b])
        masters = by_type.get("master", [])
        if masters:
            o.set("master_attached", 1.0 if all(m in att for m in masters) else 0.0)
        elif len(beds) >= 2:
            pass
        if len(beds) >= 2 and len(att) / len(beds) < 0.4:
            o.issue("few_attached", "minor", "attached_share",
                    f"Only {len(att)} of {len(beds)} bedrooms have an attached toilet (usually each bedroom has one)", [b for b in beds if b not in att])

    # --- wet cluster
    wet = [r.id for r in rooms if r.type in WET | {"kitchen", "utility"}]
    if len(wet) >= 2:
        par = {w: w for w in wet}

        def f(a):
            while par[a] != a:
                par[a] = par[par[a]]
                a = par[a]
            return a

        for a, b in itertools.combinations(wet, 2):
            if ctx.touch(a, b) >= 0.4 or (ctx.poly[a].distance(ctx.poly[b]) < 0.35 and ctx.poly[a].buffer(0.35).intersection(ctx.poly[b]).length > 0.3):
                par[f(a)] = f(b)
        comp: dict[str, list[str]] = {}
        for w in wet:
            comp.setdefault(f(w), []).append(w)
        share = max(len(v) for v in comp.values()) / len(wet)
        o.set("wet_cluster", share)
        o.stats["wet_clusters"] = len(comp)
        if share < 0.45 and len(wet) >= 3:
            o.issue("wet_scatter", "minor", "wet_cluster",
                    f"Wet rooms form {len(comp)} separate groups: cluster toilets and kitchen on shared shafts/walls", wet)


def _furniture(o: _Out, fits: dict[str, RoomFit]):
    ctx = o.ctx
    def avg(rows):
        w = sum(x[1] for x in rows)
        return sum(x[0] * x[1] for x in rows) / w if w else None

    sleep = [(fits[r.id].score, 1.0) for r in ctx.plan.rooms if r.type in BED and r.id in fits]
    living = [(fits[r.id].score, 2.0 if r.type == "living" else 1.0) for r in ctx.plan.rooms if r.type in ("living", "dining") and r.id in fits]
    kit = [(fits[r.id].score, 1.0) for r in ctx.plan.rooms if r.type == "kitchen" and r.id in fits]
    toi = [(fits[r.id].score, 1.0) for r in ctx.plan.rooms if r.type in WET and r.id in fits]
    o.set("fit_sleep", avg(sleep))
    o.set("fit_living", avg(living))
    o.set("fit_kitchen", avg(kit))
    o.set("fit_toilet", avg(toi))
    for rid, f in fits.items():
        r = ctx.room[rid]
        if f.tier == "none":
            sev = "major"
            what = {"master": "bed 1.8 x 2.0 m with 0.6 m side clearance", "bedroom": "bed 1.5 x 1.9 m with 0.6 m side clearance",
                    "living": "sofa set facing a TV wall", "dining": "dining table with 0.9 m clearance",
                    "kitchen": "2.4 m counter run with a 0.9 m aisle", "toilet": "WC + basin + shower zone",
                    "bath": "basin + shower zone", "wc": "WC with 0.6 m in front", "utility": "washing machine", "study": "desk and chair"}.get(f.kind, "furniture")
            if f.kind == "wc" or f.kind == "utility":
                sev = "minor"
            o.issue("furniture_none", sev, _fit_metric(r.type), f"{_name(ctx, rid)} ({r.area:.1f} m2): {what} does not fit" + (f" ({f.note})" if f.note else ""), [rid])
        elif f.missing:
            sev = "minor"
            o.issue("furniture_partial", sev, _fit_metric(r.type),
                    f"{_name(ctx, rid)} ({r.area:.1f} m2): no room for {', '.join(f.missing)} with clearances" + (f" ({f.note})" if f.note else ""), [rid])
        elif f.tier == "tight" and r.type in BED | {"dining"}:
            o.issue("furniture_tight", "info", _fit_metric(r.type), f"{_name(ctx, rid)}: furniture fits only with reduced clearances", [rid])


def _fit_metric(t: str) -> str:
    return {"master": "fit_sleep", "bedroom": "fit_sleep", "living": "fit_living", "dining": "fit_living", "kitchen": "fit_kitchen"}.get(t, "fit_toilet")


def _daylight(o: _Out):
    ctx = o.ctx
    hab = [r for r in ctx.plan.rooms if r.type in HAB]
    if hab:
        have = [r for r in hab if r.has_window]
        o.set("hab_window", len(have) / len(hab))
        for r in hab:
            if not r.has_window:
                o.issue("no_window", "major", "hab_window", f"{_name(ctx, r.id)} has no window on the facade", [r.id])
        ratios = [r.window_area / r.area for r in hab if r.has_window and r.area > 0]
        if ratios:
            o.set("window_ratio", min(ratios))
            rr = min(((r.window_area / r.area, r) for r in hab if r.has_window and r.area > 0), key=lambda t: t[0])
            if rr[0] < 0.1:
                o.issue("window_small", "minor", "window_ratio", f"{_name(ctx, rr[1].id)}: window is {rr[0]:.0%} of the floor (DCPR wants 1/6)", [rr[1].id])
    wet = [r for r in ctx.plan.rooms if r.type in WET]
    if wet:
        s = 0.0
        for r in wet:
            if r.ventilated:
                s += 1.0
            elif r.vent_via == "duct":
                s += 0.5
                o.issue("duct_vent", "info", "wet_vent", f"{_name(ctx, r.id)} is ventilated by a mechanical duct (needs review)", [r.id])
            else:
                o.issue("wet_unvent", "major", "wet_vent", f"{_name(ctx, r.id)} has no window or shaft", [r.id])
        o.set("wet_vent", s / len(wet))


def _doors(o: _Out):
    ctx = o.ctx
    ds = [d for d in ctx.doors]
    clash = 0
    for a, b in itertools.combinations(ds, 2):
        if a.swing.intersects(b.swing) and a.swing.intersection(b.swing).area > 0.05:
            clash += 1
            o.issue("swing_clash", "major", "swing_clash", f"Doors {a.id} and {b.id} swing into each other",
                    at=tuple((a.mid + b.mid) / 2))
    o.set("swing_clash", clash)
    blocked = 0
    narrow = 0
    thin = 0
    for d in ds:
        poly = ctx.poly.get(d.swing_room) if d.swing_room else None
        if poly is not None and not poly.is_empty:
            out = d.swing.difference(poly.buffer(0.05)).area
            if out > 0.1:
                blocked += 1
                o.issue("swing_blocked", "minor", "swing_free", f"Door {d.id} cannot swing fully open in {_name(ctx, d.swing_room)}", at=tuple(d.mid))
        circ_rooms = [r for r in d.rooms if r in ctx.room and ctx.type[r] in CIRC]
        for r in circ_rooms:
            w = ctx.room[r].width
            if w < 0.9 - 1e-6 or (d.swing_room == r and w < 1.2 - 1e-6):
                narrow += 1
                o.issue("narrow_passage_door", "major" if w < 0.9 else "minor", "passage_door",
                        f"Door {d.id} opens {'onto' if d.swing_room != r else 'into'} {_name(ctx, r)} which is only {w:.2f} m wide", [r], at=tuple(d.mid))
                break
        mn = 1.0 if d.kind == "entry" else 0.75 if any(ctx.type.get(r) in WET | {"utility"} for r in d.rooms) else 0.9
        if d.w < mn - 0.02:
            thin += 1
            o.issue("door_narrow", "minor", "door_width", f"Door {d.id} is {d.w:.2f} m wide (minimum {mn:g} m)", at=tuple(d.mid))
    # the front door should sit where the flat meets the lobby (unit.core_link)
    if ctx.entry_door is not None and ctx.unit is not None and ctx.unit.core_link:
        gap = float(np.hypot(*(ctx.entry_door.mid - np.array(ctx.unit.core_link))))
        o.stats["entry_to_lobby_door"] = round(gap, 2)
        if gap > 1.5:
            o.issue("entry_off_core", "minor", None, f"The front door is {gap:.1f} m from the lobby door point of the flat", at=tuple(ctx.entry_door.mid))
    o.set("swing_free", blocked)
    o.set("passage_door", narrow)
    o.set("door_width", thin)
    # furniture vs swings is enforced during placement; report doors whose landing is blocked by furniture
    return None


def critique(plan: Plan | dict, unit: Unit | dict | None = None, plate: Plate | dict | None = None, *,
             furniture: bool = True) -> dict[str, Any]:
    """Score one solved flat. ``unit`` (gross area, programme, core door) and ``plate`` (stairs) are optional:
    metrics that need them are skipped and the weights renormalised."""
    if isinstance(plan, dict):
        plan = Plan.model_validate(plan)
    if isinstance(unit, dict):
        unit = Unit.model_validate(unit)
    if isinstance(plate, dict):
        plate = Plate.model_validate(plate)
    ctx = _Ctx(plan, unit, plate)
    o = _Out(ctx)
    net = sum(r.area for r in plan.rooms if r.type != "balcony")
    o.stats["net_area"] = round(net, 2)
    fits: dict[str, RoomFit] = {}
    steps = [("circulation", lambda: _circulation(o, net)), ("waste", lambda: _waste(o, net)),
             ("sequence", lambda: _sequence(o)), ("daylight", lambda: _daylight(o)), ("doors", lambda: _doors(o))]
    for name, fn in steps:
        try:
            fn()
        except Exception as e:  # a failing section must not hide the others
            o.errors.append(f"{name}: {type(e).__name__}: {e}")
    if furniture:
        try:
            fits = place_furniture(ctx)
            _furniture(o, fits)
        except Exception as e:
            o.errors.append(f"furniture: {type(e).__name__}: {e}")
    # score
    num = den = 0.0
    cat_n: dict[str, float] = {}
    cat_d: dict[str, float] = {}
    for k, s in o.sat.items():
        w = METRICS[k].weight
        num += w * s
        den += w
        c = METRICS[k].cat
        cat_n[c] = cat_n.get(c, 0.0) + w * s
        cat_d[c] = cat_d.get(c, 0.0) + w
    base = 100.0 * num / den if den else 0.0
    ncrit = sum(1 for i in o.issues if i["severity"] == "critical")
    linear = 100.0 - STRICTNESS * (100.0 - base) - min(CRITICAL_CAP, CRITICAL_PENALTY * ncrit)
    factors = {k: (1 - GATE[k]) + GATE[k] * o.sat[k] for k in GATE if k in o.sat}
    gate_key = min(factors, key=factors.get) if factors else None
    gate_f = factors[gate_key] if gate_key else 1.0
    score = min(100.0, max(0.0, linear * gate_f))
    grade = next(g for t, g in GRADES if score >= t)
    metrics = {k: {"value": o.values[k], "satisfaction": round(o.sat[k], 3), "good": METRICS[k].good, "bad": METRICS[k].bad,
                   "weight": METRICS[k].weight, "unit": METRICS[k].unit, "category": METRICS[k].cat} for k in o.values}
    # an issue's weight in the score = lost satisfaction x weight (for ranking "top issues")
    for i in o.issues:
        m = i.get("metric")
        i["cost"] = round(METRICS[m].weight * (1 - o.sat.get(m, 1.0)), 2) if m in METRICS and den else 0.0
        if i["severity"] == "critical":
            i["cost"] += CRITICAL_PENALTY
    o.issues.sort(key=lambda i: (SEV_ORDER[i["severity"]], -i["cost"]))
    return {
        "unit_id": plan.unit_id,
        "score": round(score, 1),
        "base_score": round(base, 1),
        "weakest_link": gate_key if gate_f < 0.95 else None,
        "weakest_factor": round(gate_f, 3),
        "grade": grade,
        "n_critical": ncrit,
        "subscores": {c: round(100 * cat_n[c] / cat_d[c], 1) for c in CATEGORIES if c in cat_d},
        "metrics": metrics,
        "issues": o.issues,
        "furniture": {rid: {"kind": f.kind, "tier": f.tier, "score": f.score, "missing": f.missing, "note": f.note,
                            "items": f.items} for rid, f in fits.items()},
        "stats": o.stats,
        "errors": o.errors,
    }


def top_issues(result: dict[str, Any], n: int = 3) -> list[str]:
    """The n most costly findings as short sentences (for scorecards)."""
    iss = sorted(result["issues"], key=lambda i: (SEV_ORDER[i["severity"]] > 1, -i["cost"]))
    return [i["message"] for i in iss if i["severity"] != "info"][:n]


# ------------------------------------------------------------------------------------------------ calibration


def approved_stats(path: str | None = None) -> dict[str, Any]:
    """Re-derive from the mined approved plans (data/mined/topology_v1.jsonl.gz) the figures the thresholds above
    are calibrated on: shares and quantiles over "clean" flats (not a suspected merge, 1-4 BHK, kitchen + living +
    toilet + bedroom present, at most one untyped room, entrance known, 28-260 m2). Graph facts only: the mined data
    has room areas, links and neighbours but no polygons, so geometric metrics are calibrated from areas/aspects only."""
    import gzip
    import json
    from collections import Counter, defaultdict
    from pathlib import Path

    p = Path(path) if path else Path(__file__).resolve().parents[2] / "data" / "mined" / "topology_v1.jsonl.gz"
    rows = [json.loads(line) for line in gzip.open(p, "rt")]
    flats = [r for r in rows if "rooms" in r]

    def clean(r):
        ts = [x["type"] for x in r["rooms"]]
        if r["suspect_merge"] or not 1 <= r["bhk"] <= 4 or r["entrance_room"] is None:
            return False
        if sum(t in ("unknown", "unlabeled") for t in ts) > 1:
            return False
        if not all(t in ts for t in ("kitchen", "living", "toilet")) or not any(t in ("bedroom", "master") for t in ts):
            return False
        return 28 <= sum(x["area"] for x in r["rooms"]) <= 260

    C = [r for r in flats if clean(r)]

    def q(a, ps=(10, 25, 50, 75, 90)):
        a = list(a)
        return [round(float(np.percentile(a, x)), 2) for x in ps] if a else None

    out: dict[str, Any] = {"flats": len(C), "projects": len({r["pdf"] for r in C}), "quantiles_at": [10, 25, 50, 75, 90]}
    circ = []
    area_t: dict[str, list[float]] = defaultdict(list)
    asp: dict[str, list[float]] = defaultdict(list)
    att_m, att_o, kit_l, kit_touch, common, behind = [], [], [], [], [], []
    ent = Counter()
    spread = []
    wet_share = []
    for r in C:
        ids = {x["id"]: x for x in r["rooms"]}
        adj: dict[int, set[int]] = defaultdict(set)
        for x in r["rooms"]:
            for y in set(x["doors"]) | set(x["open"]):
                if y in ids:
                    adj[x["id"]].add(y)
                    adj[y].add(x["id"])
        tot = sum(x["area"] for x in r["rooms"] if x["type"] != "balcony")
        circ.append(100 * sum(x["area"] for x in r["rooms"] if x["type"] in ("passage", "foyer", "alcove")) / tot)
        for x in r["rooms"]:
            area_t[x["type"]].append(x["area"])
            if x["short"] and x["short"] > 0.3:
                asp[x["type"]].append(x["long"] / x["short"])
        beds = [i for i, x in ids.items() if x["type"] in ("bedroom", "master")]
        for b in beds:
            a = any(ids[n]["type"] == "toilet" for n in adj[b])
            (att_m if ids[b]["type"] == "master" else att_o).append(a)
        lv = [i for i, x in ids.items() if x["type"] in ("living", "dining", "foyer")]
        for k in [i for i, x in ids.items() if x["type"] == "kitchen"]:
            kit_l.append(any(n in lv for n in adj[k]))
            kit_touch.append(any(n in lv for n in ids[k]["neighbours"]))
        tl = [i for i, x in ids.items() if x["type"] == "toilet"]
        common.append(any(any(ids[n]["type"] not in ("bedroom", "master", "toilet", "dress") for n in adj[t]) for t in tl))
        e = r["entrance_room"]
        ent[ids[e]["type"] if e in ids else "none"] += 1
        if e in ids:
            seen, st = {e}, [e]
            while st:
                u = st.pop()
                if ids[u]["type"] in ("bedroom", "master") and u != e:
                    continue
                for n in adj[u]:
                    if n not in seen:
                        seen.add(n)
                        st.append(n)
            behind.extend(b not in seen and b != e for b in beds)
        if len(beds) >= 2:
            pts = [(ids[b]["cx"], ids[b]["cy"]) for b in beds]
            spread.append(max(math.dist(a, b) for a, b in itertools.combinations(pts, 2)))
        wet = [i for i, x in ids.items() if x["type"] in ("toilet", "kitchen", "utility")]
        if wet:
            par = {i: i for i in wet}

            def f(a, par=par):
                while par[a] != a:
                    par[a] = par[par[a]]
                    a = par[a]
                return a

            for i in wet:
                for n in ids[i]["neighbours"]:
                    if n in par:
                        par[f(i)] = f(n)
            comp = Counter(f(i) for i in wet)
            wet_share.append(max(comp.values()) / len(wet))
    out["circ_share_pct"] = q(circ)
    out["circ_share_zero_pct_of_flats"] = round(100 * float(np.mean([c == 0 for c in circ])), 1)
    out["area_by_type"] = {t: q(area_t[t]) for t in ("living", "dining", "kitchen", "master", "bedroom", "toilet", "passage")}
    out["aspect_by_type"] = {t: q(asp[t]) for t in ("living", "dining", "kitchen", "master", "bedroom", "toilet", "passage")}
    out["entrance_type_share"] = {k: round(v / len(C), 3) for k, v in ent.most_common(6)}
    out["master_attached_toilet"] = round(float(np.mean(att_m)), 3)
    out["other_bedroom_attached_toilet"] = round(float(np.mean(att_o)), 3)
    out["kitchen_linked_living_dining_foyer"] = round(float(np.mean(kit_l)), 3)
    out["kitchen_touches_living_dining_foyer"] = round(float(np.mean(kit_touch)), 3)
    out["flat_has_toilet_reachable_without_bedroom"] = round(float(np.mean(common)), 3)
    out["bedroom_only_behind_another_room"] = round(float(np.mean(behind)), 3)
    out["bedroom_centroid_spread_m"] = q(spread)
    out["largest_wet_cluster_share"] = q(wet_share)
    return out


# ------------------------------------------------------------------------------------------------ baseline report


def _solve_job(job):
    """Process-pool worker: solve one golden flat (module level so it can be pickled)."""
    import time

    from .layout import solve_flat
    from .models import PlateFootprint
    from .plate import fit
    from .units import split_units

    sh, W, D, idx = job
    fp = PlateFootprint(id="T1", polygon=[], angle_deg=0, center=(0, 0), width=W, depth=D, area=W * D)
    plate = fit(sh, fp)
    unit = split_units(plate)[idx]
    t0 = time.perf_counter()
    fr = solve_flat(plate, unit)
    return {"name": f"{sh}_{int(W)}x{int(D)}_{unit.id}", "shape": sh, "W": W, "D": D, "unit": unit.model_dump(mode="json"),
            "plan": fr.plan.model_dump(mode="json") if fr.plan else None, "fix": (fr.applied_fix or {}).get("label"),
            "solve_s": round(time.perf_counter() - t0, 2), "message": fr.message}


def solve_golden(workers: int = 2) -> dict[str, Any]:
    """Solve every golden flat (planner_engine.golden): {"plates": {"shape_WxD": Plate}, "flats": [...]}."""
    from concurrent.futures import ProcessPoolExecutor

    from .golden import golden_flats
    from .models import PlateFootprint
    from .plate import fit
    from .units import split_units

    jobs, plates = [], {}
    for sh, W, D in golden_flats():
        fp = PlateFootprint(id="T1", polygon=[], angle_deg=0, center=(0, 0), width=W, depth=D, area=W * D)
        plate = fit(sh, fp)
        plates[f"{sh}_{int(W)}x{int(D)}"] = plate.model_dump(mode="json")
        jobs += [(sh, W, D, i) for i in range(len(split_units(plate)))]
    if workers <= 1:
        flats = [_solve_job(j) for j in jobs]
    else:
        with ProcessPoolExecutor(workers) as ex:
            flats = list(ex.map(_solve_job, jobs))
    return {"plates": plates, "flats": flats}


def baseline(out_dir: str, resolve: bool = False, workers: int = 2, render: bool = True) -> list[dict[str, Any]]:
    """Critique (and render) every golden flat into ``out_dir``; returns one row per flat (also written to scores.json).

    Solved plans are cached in ``out_dir/golden_plans.json.gz`` so the report can be rebuilt without the solver;
    ``resolve=True`` solves again (CP-SAT with several workers is not bit-for-bit repeatable)."""
    import gzip
    import json
    from pathlib import Path

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    cache = out / "golden_plans.json.gz"
    if cache.exists() and not resolve:
        data = json.loads(gzip.open(cache, "rt").read())
    else:
        data = solve_golden(workers)
        with gzip.open(cache, "wt", compresslevel=9) as f:
            json.dump(data, f, separators=(",", ":"))
    rows = []
    for fl in data["flats"]:
        row: dict[str, Any] = {"name": fl["name"], "shape": fl["shape"], "envelope": f"{int(fl['W'])}x{int(fl['D'])}",
                               "solve_s": fl["solve_s"], "fix": fl["fix"]}
        if fl["plan"] is None:
            row.update(solved=False, score=None, grade=None, message=fl["message"])
            rows.append(row)
            continue
        plate = Plate.model_validate(data["plates"][f"{fl['shape']}_{int(fl['W'])}x{int(fl['D'])}"])
        unit = Unit.model_validate(fl["unit"])
        plan = Plan.model_validate(fl["plan"])
        res = critique(plan, unit, plate)
        if render:
            from .render import plan_png

            plan_png(plan, out / f"{fl['name']}.png", issues=res, unit=unit, title=fl["name"])
        row.update(solved=True, score=res["score"], grade=res["grade"], subscores=res["subscores"], n_critical=res["n_critical"],
                   weakest_link=res["weakest_link"], bhk=sum(1 for r in plan.rooms if r.type in BED), net_area=res["stats"]["net_area"],
                   gross=res["stats"].get("gross"), top_issues=top_issues(res, 3),
                   metrics={k: m["value"] for k, m in res["metrics"].items()},
                   issue_codes=sorted({i["code"] for i in res["issues"] if i["severity"] != "info"}))
        rows.append(row)
    (out / "scores.json").write_text(json.dumps(rows, indent=1))
    return rows


if __name__ == "__main__":  # python -m planner_engine.quality [--approved | --baseline DIR [--resolve] | plan.json]
    import json
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "--approved":
        print(json.dumps(approved_stats(*sys.argv[2:3]), indent=1))
    elif len(sys.argv) > 2 and sys.argv[1] == "--baseline":
        rows = baseline(sys.argv[2], resolve="--resolve" in sys.argv)
        ok = [r for r in rows if r["solved"]]
        print(f"{len(ok)}/{len(rows)} flats solved; mean score {sum(r['score'] for r in ok) / max(1, len(ok)):.1f}")
    elif len(sys.argv) > 1:
        d = json.load(open(sys.argv[1]))
        r = critique(d["plan"] if "plan" in d else d, d.get("unit"), d.get("plate"))
        print(json.dumps({k: r[k] for k in ("score", "grade", "subscores", "weakest_link")}, indent=1))
        for i in r["issues"]:
            print(f"{i['severity']:8s} {i['message']}")
    else:
        print(__doc__)
