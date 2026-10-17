"""Core library: parametric lift / stair / lobby cores for residential towers, and a selector.

A *core* is the vertical-circulation heart of a typical floor: staircases, lifts, a fire lift, the lift lobby, the
smoke-check lobby, AVS / pressurisation shafts and (on refuge floors) the refuge area. Many valid cores exist; what
makes a plan efficient is picking the right one for the plate type, the flats per floor and the height. This module
provides six parametric types seen in the approved Mumbai plans and in common practice:

==================  ==============================================================================================
``central_compact`` two stairs at the ends of the core, lifts between them in one row (two facing rows above four
                    lifts), lobby in front, smoke-check lobbies and a pressurisation shaft: internal core of
                    point towers, crosses, H plates (approved plan 28/32: 2 stairs + 2 lifts + fire lift + lobby)
``linear_corridor`` double-loaded corridor spine: lift bank and one stair widen the corridor at mid length, the
                    second stair is a satellite at the far end (travel distance); slabs and long wings
``scissor``         one scissor stair (two exits interleaved in one enclosure) + lifts: smallest footprint, needs
                    the authority to accept a scissor pair as two exits
``split_junction``  two clusters at two wing junctions: A = stair + all lifts + lobby, B = stair + service lift;
                    H / T / cruciform plates with long arms
``side_core``       like the compact core but on the façade: stairs get natural ventilation, no pressurisation
                    shaft; L plates, wings, slabs with the core on the long face
``end_core``        lifts stacked along the short end of a slab, stair beside them, satellite stair at the far end
==================  ==============================================================================================

Every type returns typed part polygons (``stair, lift, fire_lift, lobby, smoke_lobby, shaft, refuge``) in a local frame
(+x along the lobby, y = 0 at the lobby edge where the core opens onto the floor, +y into the core), with dimensions from
the rules the repository already carries (NBC 2016 Part 4 / DCPR 2034, ``data/rules``) and the office design defaults
(``data/rules/design.yaml``). Constants that are NOT in the rule files are marked ``# ASSUMPTION`` and listed in
``ASSUMPTIONS``; they are the first thing the architect should confirm.

``choose_core(plate_type, flats_per_floor, height_m, footprint)`` ranks the types by code compliance, core
efficiency (core area / plate area) and suitability for the plate type, and returns the reasons.
This module is deliberately NOT wired into ``plate.py`` yet (Phase 2).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from shapely import affinity
from shapely.geometry import Polygon, box
from shapely.ops import unary_union

# --------------------------------------------------------------------------------------------------
# code constants
# --------------------------------------------------------------------------------------------------

#: height above which two staircases are required (rules/nbc_2016/part4_egress.yaml NBC-4-4.4.2.1)
TWO_STAIRS_ABOVE_M = 15.0
#: height above which a fire lift, smoke-check lobbies and refuge floors apply (NBC-4-C1.12, Maharashtra fire norm)
FIRE_LIFT_ABOVE_M = 24.0
#: stair flight width for high-rise residential (NBC-4-T6); below the two-stair height 1.2 m is used (ASSUMPTION)
STAIR_W_HIGH_M = 1.5
STAIR_W_LOW_M = 1.2
RISER_MAX_M = 0.19  # NBC residential riser
TREAD_MIN_M = 0.25  # NBC residential tread
STAIR_WELL_M = 0.2  # open well between the two flights of a dogleg stair
TRAVEL_MAX_M = 22.5  # NBC-4-4.4.2.4: travel distance to an exit, residential
LOBBY_MIN_W_M = 1.5  # DCPR Reg 37(12) clear passage / lobby floor
SMOKE_LOBBY_M = 2.2  # side of the smoke-check lobby between stair and fire lift (approved plans: 2.20 x 2.20)
PERSONS_PER_FLAT = 4.5  # ASSUMPTION: occupants per flat for the lift traffic estimate
UPPEAK_5MIN = 0.07  # ASSUMPTION: share of the population arriving in the 5 min up-peak (residential 5-7 %)
LIFT_CAR_PASSENGERS = 13  # ASSUMPTION: 13-person car, 80 % usable in the traffic estimate
REFUGE_EVERY_FLOORS = 7  # approved plans: refuge floors at 7th, 14th, 21st ...
REFUGE_SHARE = 0.04  # ASSUMPTION: refuge area >= 4 % of the floor area (>= 15 m2)
REFUGE_MIN_M2 = 15.0
FIRE_LIFT_PER_M2 = 1200.0  # ASSUMPTION: one fire lift per 1200 m2 of floor area above 24 m
MAX_LIFTS_PER_BANK = 4

ASSUMPTIONS = {
    "stair_w_low_m": "1.2 m flight width at or below 15 m (NBC Table 6 minimum is 1.0 m)",
    "persons_per_flat": PERSONS_PER_FLAT,
    "uppeak_5min": UPPEAK_5MIN,
    "lift_car_passengers": LIFT_CAR_PASSENGERS,
    "refuge_share": f"{REFUGE_SHARE:.0%} of the floor area, at least {REFUGE_MIN_M2:.0f} m2, every {REFUGE_EVERY_FLOORS}th floor",
    "fire_lift_per_m2": FIRE_LIFT_PER_M2,
    "smoke_lobby_m": SMOKE_LOBBY_M,
    "scissor": "a scissor stair counts as two exits only if the authority accepts the pair (flagged on the core)",
}


def _design() -> dict[str, Any]:
    from . import rules

    return rules.design()


# --------------------------------------------------------------------------------------------------
# requirements
# --------------------------------------------------------------------------------------------------


@dataclass
class Requirements:
    height_m: float
    floors: int
    flats_per_floor: int
    plate_area_m2: float
    floor_h: float
    n_stairs: int
    stair_flight_w: float
    n_lifts: int  # passenger lifts
    n_fire_lifts: int
    lift_w: float  # clear shaft width (along the lobby)
    lift_d: float  # clear shaft depth
    lobby_depth: float
    smoke_lobby: bool
    refuge_floors: list[int]
    refuge_area_m2: float
    clauses: list[str] = field(default_factory=list)

    @property
    def n_lifts_total(self) -> int:
        return self.n_lifts + self.n_fire_lifts


def lift_round_trip_s(height_m: float, floors: int) -> float:
    """Indicative round-trip time of one car: travel + door/stop time (traffic estimate, not a vendor calculation)."""
    v = 1.0 if height_m <= 24 else 1.5 if height_m <= 45 else 2.5 if height_m <= 70 else 3.5  # m/s
    stops = min(floors, 10)
    return 2.0 * height_m / v + 10.0 + 5.0 * stops


def passenger_lifts(height_m: float, flats_per_floor: float, floors: int | None = None, floor_h: float = 3.0) -> int:
    """Passenger lifts (excluding the fire lift) for the flats a core serves.

    Up-peak traffic: ``7 % x occupants`` must be carried in 5 minutes by cars of ``13 x 0.8`` passengers with the
    round-trip time of ``lift_round_trip_s``. At least 1 lift above ~11 m (4 storeys) and 2 above 15 m so a
    breakdown does not strand the building."""
    floors = floors or max(1, round(height_m / floor_h))
    if height_m <= 11.0:
        return 0
    pop = flats_per_floor * floors * PERSONS_PER_FLAT
    demand = UPPEAK_5MIN * pop
    per_lift = 300.0 / lift_round_trip_s(height_m, floors) * (LIFT_CAR_PASSENGERS * 0.8)
    n = math.ceil(demand / per_lift)
    return max(2 if height_m > TWO_STAIRS_ABOVE_M else 1, n)


def requirements(height_m: float, flats_per_floor: int, plate_area_m2: float = 600.0, floor_h: float | None = None,
                 lift_size: tuple[float, float] | None = None) -> Requirements:
    """Everything a core must provide for a tower of this height and this many flats per floor."""
    d = _design()
    floor_h = float(floor_h or d.get("floor_to_floor", 3.0))
    ls = lift_size or tuple(d["core"]["lift_size"])
    floors = max(1, round(height_m / floor_h))
    clauses: list[str] = []
    n_st = 2 if height_m > TWO_STAIRS_ABOVE_M else 1
    clauses.append("NBC 4.4.2.1: two staircases above 15 m" if n_st == 2 else "one staircase up to 15 m")
    fw = STAIR_W_HIGH_M if height_m > TWO_STAIRS_ABOVE_M else STAIR_W_LOW_M
    clauses.append(f"stair flight width >= {fw} m (NBC Table 6)")
    n_l = passenger_lifts(height_m, flats_per_floor, floors, floor_h)
    n_fl = 0
    refuges: list[int] = []
    refuge_area = 0.0
    if height_m > FIRE_LIFT_ABOVE_M:
        n_fl = max(1, math.ceil(plate_area_m2 / FIRE_LIFT_PER_M2))
        clauses.append("NBC C-1.12: fire lift and smoke-check lobbies above 24 m")
        refuges = list(range(REFUGE_EVERY_FLOORS, floors, REFUGE_EVERY_FLOORS))
        refuge_area = max(REFUGE_MIN_M2, REFUGE_SHARE * plate_area_m2)
        clauses.append(f"refuge area >= {refuge_area:.0f} m2 every {REFUGE_EVERY_FLOORS}th floor")
    lobby_d = max(float(d["core"]["lobby_depth"]), float(ls[1]))
    clauses.append(f"lift lobby depth >= lift depth ({ls[1]:.1f} m)")
    return Requirements(height_m, floors, int(flats_per_floor), plate_area_m2, floor_h, n_st, fw, n_l, n_fl, float(ls[0]),
                        float(ls[1]), lobby_d, height_m > FIRE_LIFT_ABOVE_M, refuges, refuge_area, clauses)


# --------------------------------------------------------------------------------------------------
# geometry primitives
# --------------------------------------------------------------------------------------------------


@dataclass
class StairDims:
    flight_w: float
    risers: int  # per floor
    riser_h: float
    tread_d: float
    run: float  # horizontal length of one flight
    clear_w: float
    clear_d: float
    outer_w: float
    outer_d: float


def stair_dims(flight_w: float, floor_h: float, wall: float | None = None, scissor: bool = False) -> StairDims:
    """Dogleg stair: two flights of ``risers/2`` risers around a 0.2 m well, a landing of one flight width at both ends
    (floor landing and half landing). Scissor: two straight flights per floor side by side behind a fire wall."""
    wall = float(wall if wall is not None else _design()["walls"]["external"])
    n = math.ceil(floor_h / 0.175)  # ~175 mm risers, even number for a dogleg
    n += n % 2
    riser_h = floor_h / n
    tread = TREAD_MIN_M
    per_flight = n // 2
    run = (per_flight - 1) * tread
    if scissor:
        clear_w = 2 * flight_w + wall  # two stairs separated by a fire-rated wall
        clear_d = n * tread - tread + 2 * flight_w * 0 + 2 * flight_w  # one straight flight per floor + two landings
    else:
        clear_w = 2 * flight_w + STAIR_WELL_M
        clear_d = run + 2 * flight_w
    return StairDims(flight_w, n, riser_h, tread, run, clear_w, clear_d, clear_w + 2 * wall, clear_d + 2 * wall)


@dataclass
class CorePart:
    kind: str  # stair | lift | fire_lift | lobby | smoke_lobby | shaft | refuge
    polygon: Polygon
    name: str = ""
    meta: dict[str, float] = field(default_factory=dict)

    @property
    def area(self) -> float:
        return self.polygon.area


@dataclass
class Check:
    id: str
    label: str
    ok: bool
    value: float | str
    limit: float | str
    clause: str = ""
    severity: str = "error"  # error | warning


@dataclass
class Core:
    type: str
    parts: list[CorePart]
    req: Requirements
    notes: list[str] = field(default_factory=list)
    n_exits: int = 0  # staircases counted as exits
    needs_authority_acceptance: bool = False
    satellites: list[str] = field(default_factory=list)  # names of parts that sit away from the main cluster
    facade_edge: str = ""  # "back" when the back of the core is an exterior wall
    lobby_edge: tuple[tuple[float, float], tuple[float, float]] = ((0.0, 0.0), (1.0, 0.0))

    # -- geometry --------------------------------------------------------------------------------
    def of(self, *kinds: str) -> list[CorePart]:
        return [p for p in self.parts if p.kind in kinds]

    @property
    def footprint(self) -> Polygon:
        """Convex hull of the main cluster (satellites excluded)."""
        main = [p.polygon for p in self.parts if p.name not in self.satellites] or [p.polygon for p in self.parts]
        return unary_union(main).convex_hull

    @property
    def bbox(self) -> tuple[float, float, float, float]:
        return unary_union([p.polygon for p in self.parts]).bounds

    @property
    def width(self) -> float:
        b = self.bbox
        return b[2] - b[0]

    @property
    def depth(self) -> float:
        b = self.bbox
        return b[3] - b[1]

    def area(self, *kinds: str) -> float:
        """Area of the parts of these kinds (all kinds if none given); corridor strips are circulation of the plate, not core."""
        return sum(p.area for p in (self.of(*kinds) if kinds else self.parts) if not p.meta.get("corridor"))

    @property
    def core_area(self) -> float:
        """Area the core takes from the plate: every part (satellites included)."""
        return self.area()

    def efficiency(self, plate_area: float | None = None) -> float:
        """core area / plate area (lower is better: more saleable area)."""
        return self.core_area / (plate_area or self.req.plate_area_m2)

    def place(self, x: float = 0.0, y: float = 0.0, angle_deg: float = 0.0, mirror: bool = False) -> Core:
        """A copy moved to (x, y) and rotated about the local origin (mirror flips x before rotating)."""
        parts = []
        for p in self.parts:
            g = affinity.scale(p.polygon, -1.0, 1.0, origin=(0, 0)) if mirror else p.polygon
            g = affinity.translate(affinity.rotate(g, angle_deg, origin=(0, 0)), x, y)
            parts.append(CorePart(p.kind, g, p.name, dict(p.meta)))
        out = Core(self.type, parts, self.req, list(self.notes), self.n_exits, self.needs_authority_acceptance,
                   list(self.satellites), self.facade_edge, self.lobby_edge)
        return out

    def to_model_parts(self) -> list[dict[str, Any]]:
        """Parts in the shape of ``models.CorePart`` (kind limited to stair/lift/fire_lift/lobby/service) for Phase 2."""
        from . import geo

        kmap = {"smoke_lobby": "lobby", "shaft": "service", "refuge": "service"}
        return [{"kind": kmap.get(p.kind, p.kind), "polygon": geo.ring_of(p.polygon), "width": p.meta.get("width", 0.0)}
                for p in self.parts]

    # -- checks ----------------------------------------------------------------------------------
    def check(self, req: Requirements | None = None) -> list[Check]:
        """Dimensional and code checks of this core against ``req`` (default: the requirements it was built for)."""
        r = req or self.req
        out: list[Check] = []
        stairs = self.of("stair")
        n_exits = sum(int(p.meta.get("exits", 1)) for p in stairs)
        out.append(Check("stairs", "number of staircases (exits)", n_exits >= r.n_stairs, n_exits, f">= {r.n_stairs}",
                         "NBC 4.4.2.1"))
        for p in stairs:
            fw = p.meta["flight_w"]
            out.append(Check(f"stair_w:{p.name}", f"stair flight width {p.name}", fw >= r.stair_flight_w - 1e-9,
                             round(fw, 2), f">= {r.stair_flight_w}", "NBC Table 6"))
            out.append(Check(f"stair_riser:{p.name}", f"riser {p.name}", p.meta["riser_h"] <= RISER_MAX_M + 1e-9,
                             round(p.meta["riser_h"], 3), f"<= {RISER_MAX_M}", "NBC 4.4.2.4"))
            out.append(Check(f"stair_tread:{p.name}", f"tread {p.name}", p.meta["tread_d"] >= TREAD_MIN_M - 1e-9,
                             round(p.meta["tread_d"], 3), f">= {TREAD_MIN_M}", "NBC 4.4.2.4"))
        lifts = self.of("lift")
        out.append(Check("lifts", "passenger lifts", len(lifts) >= r.n_lifts, len(lifts), f">= {r.n_lifts}",
                         "traffic estimate (NBC Part 8 style)"))
        fl = self.of("fire_lift")
        out.append(Check("fire_lift", "fire lift above 24 m", len(fl) >= r.n_fire_lifts, len(fl), f">= {r.n_fire_lifts}",
                         "NBC C-1.12"))
        for p in lifts + fl:
            ok = p.meta["clear_w"] >= r.lift_w - 1e-9 and p.meta["clear_d"] >= r.lift_d - 1e-9
            out.append(Check(f"lift_size:{p.name}", f"lift shaft clear size {p.name}", ok,
                             f"{p.meta['clear_w']:.2f} x {p.meta['clear_d']:.2f}", f">= {r.lift_w:.2f} x {r.lift_d:.2f}",
                             "design.yaml core.lift_size"))
        lobbies = self.of("lobby")
        for p in lobbies:
            x0, y0, x1, y1 = p.polygon.bounds
            short = min(x1 - x0, y1 - y0)
            out.append(Check(f"lobby_w:{p.name}", f"lobby clear width {p.name}", short >= LOBBY_MIN_W_M - 1e-9,
                             round(short, 2), f">= {LOBBY_MIN_W_M}", "DCPR Reg 37(12)"))
        if lifts or fl:
            # lobby depth in front of every lift >= lift depth
            worst = min((self._lobby_depth_in_front(p) for p in lifts + fl), default=0.0)
            out.append(Check("lobby_depth", "lift lobby depth in front of lifts", worst >= r.lift_d - 0.02, round(worst, 2),
                             f">= {r.lift_d:.2f} (lift depth)", "NBC Part 8 / design"))
        if r.smoke_lobby:
            sm = self.of("smoke_lobby")
            ok = len(sm) >= len(stairs) and all(p.area >= SMOKE_LOBBY_M * SMOKE_LOBBY_M * 0.95 for p in sm)
            out.append(Check("smoke_lobby", "smoke-check lobby per stair above 24 m", ok,
                             f"{len(sm)} x {min((p.area for p in sm), default=0):.1f} m2",
                             f">= {len(stairs)} x {SMOKE_LOBBY_M * SMOKE_LOBBY_M:.1f} m2", "NBC Part 4 (ventilated lobby)"))
        # geometry sanity: no overlaps
        ov = 0.0
        for i, a in enumerate(self.parts):
            for b in self.parts[i + 1:]:
                ov += a.polygon.intersection(b.polygon).area
        out.append(Check("overlap", "parts do not overlap", ov < 1e-4, round(ov, 4), "0"))
        # every lift and stair opens onto a lobby / smoke lobby (shares at least 0.8 m of wall with one)
        acc = self.of("lobby", "smoke_lobby")
        accu = unary_union([p.polygon for p in acc]).buffer(0.02) if acc else None
        touch_bad = [p.name for p in lifts + fl + stairs
                     if accu is None or p.polygon.boundary.intersection(accu).length < 0.8]
        out.append(Check("access", "every lift and stair opens onto a lobby", not touch_bad, ", ".join(touch_bad) or "all",
                         "all"))
        if r.refuge_floors:
            ref = self.of("refuge")
            if ref:
                out.append(Check("refuge", "refuge area (refuge floors)", ref[0].area >= r.refuge_area_m2 - 1e-6,
                                 round(ref[0].area, 1), f">= {r.refuge_area_m2:.0f} m2", "NBC Annex C"))
        if self.needs_authority_acceptance:
            out.append(Check("authority", "scissor pair accepted as two exits", False, "needs approval", "accepted",
                             "authority", severity="warning"))
        return out

    def _lobby_depth_in_front(self, lift: CorePart) -> float:
        """Free lobby depth measured from the lift's door edge, perpendicular to it (the door is the side that touches a
        lobby; the longest such run wins)."""
        from shapely.geometry import LineString

        acc = [p.polygon for p in self.of("lobby", "smoke_lobby")]
        if not acc:
            return 0.0
        u = unary_union(acc)
        x0, y0, x1, y1 = lift.polygon.bounds
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        best = 0.0
        for (px, py, dx, dy) in ((cx, y0, 0, -1), (cx, y1, 0, 1), (x0, cy, -1, 0), (x1, cy, 1, 0)):
            seg = LineString([(px + dx * 1e-4, py + dy * 1e-4), (px + dx * 12, py + dy * 12)])
            inter = seg.intersection(u)
            if inter.is_empty:
                continue
            first = inter.geoms[0] if hasattr(inter, "geoms") else inter
            if LineString([(px, py), (first.coords[0][0], first.coords[0][1])]).length < 0.05:
                best = max(best, first.length if first.length < 11 else 0.0)
        return best + 1e-4  # the 1e-4 offset of the ray start

    @property
    def compliant(self) -> bool:
        return all(c.ok for c in self.check() if c.severity == "error")

    def failures(self) -> list[Check]:
        return [c for c in self.check() if not c.ok]

    def metrics(self) -> dict[str, float]:
        return {"width_m": round(self.width, 2), "depth_m": round(self.depth, 2), "core_area_m2": round(self.core_area, 1),
                "stair_m2": round(self.area("stair"), 1), "lift_m2": round(self.area("lift", "fire_lift"), 1),
                "lobby_m2": round(self.area("lobby", "smoke_lobby"), 1), "shaft_m2": round(self.area("shaft"), 1),
                "efficiency": round(self.efficiency(), 3)}


# --------------------------------------------------------------------------------------------------
# builders
# --------------------------------------------------------------------------------------------------


class _B:
    """Small helper that adds boxes in the local frame."""

    def __init__(self, req: Requirements):
        self.req = req
        d = _design()
        self.wall = float(d["walls"]["external"])
        self.parts: list[CorePart] = []
        w = 0.2  # lift shaft wall
        self.lw = req.lift_w + w  # outer width of one lift
        self.ld = req.lift_d + w
        self.sd = stair_dims(req.stair_flight_w, req.floor_h, self.wall)

    def add(self, kind: str, x0: float, y0: float, x1: float, y1: float, name: str = "", **meta) -> CorePart:
        p = CorePart(kind, box(x0, y0, x1, y1), name or kind, {k: float(v) for k, v in meta.items()})
        self.parts.append(p)
        return p

    def stair(self, x0: float, y0: float, name: str, scissor_dims: StairDims | None = None, rotated: bool = False) -> None:
        sd = scissor_dims or self.sd
        w, d = (sd.outer_d, sd.outer_w) if rotated else (sd.outer_w, sd.outer_d)
        self.add("stair", x0, y0, x0 + w, y0 + d, name, flight_w=sd.flight_w, riser_h=sd.riser_h, tread_d=sd.tread_d,
                 exits=2 if scissor_dims else 1, width=sd.flight_w)

    def lift(self, x0: float, y0: float, name: str, fire: bool = False, vertical: bool = False) -> None:
        w, d = (self.ld, self.lw) if vertical else (self.lw, self.ld)
        self.add("fire_lift" if fire else "lift", x0, y0, x0 + w, y0 + d, name, clear_w=self.req.lift_w,
                 clear_d=self.req.lift_d, width=self.req.lift_w)

    def lifts_row(self, x0: float, y0: float, n_pass: int, n_fire: int, prefix: str = "L", fire_first: bool = True) -> float:
        """Lifts side by side along x; the fire lift goes first so it sits next to the stair's smoke-check lobby."""
        x = x0
        order = [(f"F{i + 1}", True) for i in range(n_fire)] if fire_first else []
        order += [(f"{prefix}{i + 1}", False) for i in range(n_pass)]
        if not fire_first:
            order += [(f"F{i + 1}", True) for i in range(n_fire)]
        for name, fire in order:
            self.lift(x, y0, name, fire)
            x += self.lw
        return x if order else x0 + LOBBY_MIN_W_M  # no lift (low-rise): the stair still needs a landing lobby

    def lobby(self, x0: float, y0: float, x1: float, y1: float, smoke: list[tuple[float, float]], name: str = "LOBBY",
              corridor: bool = False) -> None:
        """A lobby rectangle with smoke-check lobbies (side SMOKE_LOBBY_M) carved out at the given lower-left corners."""
        lob = box(x0, y0, x1, y1)
        for sx, sy in smoke:
            g = box(sx, sy, sx + SMOKE_LOBBY_M, sy + SMOKE_LOBBY_M)
            self.parts.append(CorePart("smoke_lobby", g, f"SL{len(self.of('smoke_lobby')) + 1}"))
            lob = lob.difference(g)
        for g in getattr(lob, "geoms", [lob]):
            if isinstance(g, Polygon) and g.area > 0.4:
                self.parts.append(CorePart("lobby", g, name, {"corridor": 1.0} if corridor else {}))

    def of(self, kind: str) -> list[CorePart]:
        return [p for p in self.parts if p.kind == kind]

    def finish(self, type_: str, **kw) -> Core:
        core = Core(type_, self.parts, self.req, **kw)
        core.n_exits = sum(int(p.meta.get("exits", 1)) for p in core.of("stair"))
        return core


def _split_lifts(req: Requirements) -> list[tuple[int, int]]:
    """(passenger, fire) lifts for two facing rows; the fire lift goes in the first row."""
    n_p, n_f = req.n_lifts, req.n_fire_lifts
    first = math.ceil((n_p + n_f) / 2)
    f1 = min(n_f, first)
    p1 = first - f1
    return [(p1, f1), (n_p - p1, n_f - f1)]


def _compact(req: Requirements, kind: str, refuge: bool) -> Core:
    """Compact core shared by ``central_compact`` (internal: pressurisation shaft) and ``side_core`` (façade)."""
    b = _B(req)
    sd = b.sd
    internal = kind == "central_compact"
    two_stairs = req.n_stairs == 2
    shaft_w = 1.2 if internal else 0.0
    smoke: list[tuple[float, float]] = []
    if req.n_lifts_total <= MAX_LIFTS_PER_BANK:
        # one row: the lobby fills the depth beside the stair, so stairs, lobby and lifts form one rectangle
        lobby_d = max(req.lobby_depth, sd.outer_d - b.ld)
        b.stair(0.0, 0.0, "S1")
        x_end = b.lifts_row(sd.outer_w, lobby_d, req.n_lifts, req.n_fire_lifts)
        if shaft_w:
            b.add("shaft", x_end, lobby_d, x_end + shaft_w, lobby_d + b.ld, "AVS")
            x_end += shaft_w
        if two_stairs:
            b.stair(x_end, 0.0, "S2")
        if req.smoke_lobby:
            smoke.append((sd.outer_w, lobby_d - SMOKE_LOBBY_M))
            if two_stairs:
                smoke.append((x_end - SMOKE_LOBBY_M, lobby_d - SMOKE_LOBBY_M))
        b.lobby(sd.outer_w, 0.0, x_end, lobby_d, smoke)
        edge = ((sd.outer_w, 0.0), (x_end, 0.0))
    else:
        # two facing rows, lobby between them, stairs at the back corners
        lobby_d = req.lobby_depth
        depth = 2 * b.ld + lobby_d
        n_a, n_b = _split_lifts(req)
        xa = b.lifts_row(sd.outer_w, b.ld + lobby_d, *n_a, prefix="A")
        xb = b.lifts_row(sd.outer_w, 0.0, *n_b, prefix="B")
        x_end = max(xa, xb)
        b.stair(0.0, depth - sd.outer_d, "S1")
        if shaft_w:
            b.add("shaft", x_end, b.ld + lobby_d, x_end + shaft_w, depth, "AVS")
            x_end += shaft_w
        if two_stairs:
            b.stair(x_end, depth - sd.outer_d, "S2")
        if req.smoke_lobby:
            smoke.append((sd.outer_w, b.ld))
            if two_stairs:
                smoke.append((x_end - SMOKE_LOBBY_M, b.ld))
        b.lobby(sd.outer_w, b.ld, x_end, b.ld + lobby_d, smoke)
        lf = depth - sd.outer_d
        if lf > 0.5:  # front corners under the stairs: pressurisation / duct shafts
            b.add("shaft", 0.0, 0.0, sd.outer_w, lf, "SH-W")
            if two_stairs:
                b.add("shaft", x_end, 0.0, x_end + sd.outer_w, lf, "SH-E")
        edge = ((sd.outer_w, b.ld), (x_end, b.ld))
    core = b.finish(kind, lobby_edge=edge, facade_edge="" if internal else "back")
    if internal:
        core.notes.append("internal core: stairs need pressurisation / smoke-check lobbies above 24 m")
    else:
        core.notes.append("façade core: stairs and lift lobby can borrow daylight; back wall is exterior")
    if refuge:
        _add_refuge(core)
    return core


def central_compact(req: Requirements, refuge: bool = False) -> Core:
    """Two stairs at the ends, lifts between them (one row up to 4 lifts, two facing rows above), lobby in front,
    pressurisation shaft; the internal core of point towers, crosses and T/H junctions."""
    return _compact(req, "central_compact", refuge)


def side_core(req: Requirements, refuge: bool = False) -> Core:
    """Compact core on the façade: the stairs are on the exterior wall so they are naturally ventilated (no
    pressurisation shaft); still one smoke-check lobby per stair above 24 m."""
    return _compact(req, "side_core", refuge)


def scissor(req: Requirements, refuge: bool = False) -> Core:
    """One scissor stair (two interleaved exits) lying along the back of the core beside the lift bank."""
    b = _B(req)
    sc = stair_dims(req.stair_flight_w, req.floor_h, b.wall, scissor=True)
    lobby_d = req.lobby_depth
    b.stair(0.0, lobby_d, "SCISSOR", scissor_dims=sc, rotated=True)  # long side along x, door on the lobby
    x_end = b.lifts_row(sc.outer_d, lobby_d, req.n_lifts, req.n_fire_lifts)
    b.add("shaft", x_end, lobby_d, x_end + 1.2, lobby_d + b.ld, "AVS")
    x_end += 1.2
    # one smoke-check lobby straddling the stair door and the adjacent fire lift
    smoke = [(sc.outer_d - 1.1, lobby_d - SMOKE_LOBBY_M)] if req.smoke_lobby else []
    x_door = max(0.0, sc.outer_d - 2.4)  # the lobby only needs to reach the stair door, not run under the whole stair
    b.lobby(x_door, 0.0, x_end, lobby_d, smoke)
    core = b.finish("scissor", lobby_edge=((x_door, 0.0), (x_end, 0.0)))
    core.n_exits = 2
    core.needs_authority_acceptance = True
    core.notes.append("scissor pair: both exits start in the same place; accepted only if fire-separated and approved")
    if refuge:
        _add_refuge(core)
    return core


def linear_corridor(req: Requirements, plate_length: float = 45.0, corridor_w: float | None = None,
                    refuge: bool = False, **_ignored) -> Core:
    """Double-loaded corridor spine: lift bank + stair 1 widen the corridor at mid length, stair 2 is a satellite
    ``span`` away so no flat is further than 22.5 m from a stair."""
    b = _B(req)
    cw = float(corridor_w or _design()["core"]["passage_width"])
    sd = b.sd
    lobby_d = max(req.lobby_depth, sd.outer_d - b.ld)
    b.stair(0.0, 0.0, "S1")
    x_end = b.lifts_row(sd.outer_w, lobby_d, req.n_lifts, req.n_fire_lifts)
    smoke = [(sd.outer_w, lobby_d - SMOKE_LOBBY_M)] if req.smoke_lobby else []
    b.lobby(sd.outer_w, 0.0, x_end, lobby_d, smoke)
    sat: list[str] = []
    if req.n_stairs == 2:
        span = min(max(plate_length * 0.5 - 2.0, TRAVEL_MAX_M * 0.5), 2 * TRAVEL_MAX_M - 6.0)
        x2 = x_end + span
        b.stair(x2, 0.0, "S2")
        n0 = len(b.parts)
        sm2 = [(x2 - SMOKE_LOBBY_M, 0.0)] if req.smoke_lobby else []
        b.lobby(x_end, 0.0, x2, cw, [], "CORRIDOR", corridor=True) if not sm2 else b.lobby(
            x_end, 0.0, x2 - SMOKE_LOBBY_M, cw, [], "CORRIDOR", corridor=True)
        if sm2:
            b.parts.append(CorePart("smoke_lobby", box(x2 - SMOKE_LOBBY_M, 0.0, x2, SMOKE_LOBBY_M), "SL-S2"))
        sat = ["S2", "CORRIDOR", "SL-S2"]
        del n0
    core = b.finish("linear_corridor", lobby_edge=((sd.outer_w, 0.0), (x_end, 0.0)))
    core.satellites = sat
    core.notes.append("stair 2 is a satellite along the corridor (corridor area is not charged to the core)")
    if refuge:
        _add_refuge(core)
    return core


def split_junction(req: Requirements, span: float = 24.0, refuge: bool = False, **_ignored) -> Core:
    """Two clusters at two wing junctions: A = stair + all lifts + lobby, B = stair + AVS shaft + lobby."""
    b = _B(req)
    sd = b.sd
    lobby_d = max(req.lobby_depth, sd.outer_d - b.ld)
    b.stair(0.0, 0.0, "S1")
    x_end = b.lifts_row(sd.outer_w, lobby_d, req.n_lifts, req.n_fire_lifts)
    b.add("shaft", x_end, lobby_d, x_end + 1.2, lobby_d + b.ld, "AVS-A")
    x_end += 1.2
    smoke = [(sd.outer_w, lobby_d - SMOKE_LOBBY_M)] if req.smoke_lobby else []
    b.lobby(sd.outer_w, 0.0, x_end, lobby_d, smoke, "LOBBY-A")
    sat: list[str] = []
    if req.n_stairs == 2:
        xb = x_end + span
        b.stair(xb, 0.0, "S2")
        wb = 1.2 + 2.2 + 1.0
        b.add("shaft", xb + sd.outer_w, lobby_d, xb + sd.outer_w + 1.2, lobby_d + b.ld, "AVS-B")
        sm = [(xb + sd.outer_w, lobby_d - SMOKE_LOBBY_M)] if req.smoke_lobby else []
        b.lobby(xb + sd.outer_w, 0.0, xb + sd.outer_w + max(wb, SMOKE_LOBBY_M + 1.6), lobby_d, sm, "LOBBY-B")
        sat = [p.name for p in b.parts if p.polygon.bounds[0] >= xb - 1e-6]
    core = b.finish("split_junction", lobby_edge=((sd.outer_w, 0.0), (x_end, 0.0)))
    core.satellites = sat
    core.notes.append(f"cluster B (stair 2) sits {span:.0f} m from cluster A at the other wing junction")
    if refuge:
        _add_refuge(core)
    return core


def end_core(req: Requirements, plate_depth: float = 15.0, plate_length: float = 45.0, refuge: bool = False,
             **_ignored) -> Core:
    """Core on the short end wall of a slab: lifts stacked along the wall with their doors on a lobby, stair beside them,
    satellite stair at the far end. Frame: x = depth into the slab from the lobby, y = along the end wall; the lobby
    edge is the side that opens onto the slab corridor."""
    b = _B(req)
    sd = b.sd
    lobby_d = req.lobby_depth
    y = 0.0
    order = [(f"F{i + 1}", True) for i in range(req.n_fire_lifts)] + [(f"L{i + 1}", False) for i in range(req.n_lifts)]
    # stair first so the fire lift (next in the column) sits beside its smoke-check lobby
    b.stair(lobby_d, 0.0, "S1", rotated=True)
    y = sd.outer_w
    for name, fire in order:
        b.lift(lobby_d, y, name, fire, vertical=True)
        y += b.lw
    smoke = [(0.0, sd.outer_w)] if req.smoke_lobby else []
    b.lobby(0.0, 0.0, lobby_d, y, smoke)
    sat: list[str] = []
    if req.n_stairs == 2:
        span = max(plate_length - lobby_d - sd.outer_d - 2.0, 10.0)
        x2 = lobby_d + sd.outer_d + span
        b.stair(x2, 0.0, "S2", rotated=True)
        sat = ["S2", "CORRIDOR"]
        b.lobby(lobby_d + sd.outer_d, 0.0, x2, 1.5, [], "CORRIDOR", corridor=True)
        if req.smoke_lobby:
            b.parts.append(CorePart("smoke_lobby", box(x2 - SMOKE_LOBBY_M, 1.5, x2, 1.5 + SMOKE_LOBBY_M), "SL-S2"))
            sat.append("SL-S2")
    core = b.finish("end_core", lobby_edge=((0.0, 0.0), (0.0, y)))
    core.satellites = sat
    core.notes.append("core on the slab end wall: lobby opens along the corridor; stair 2 at the far end")
    if refuge:
        _add_refuge(core)
    return core


def _add_refuge(core: Core) -> None:
    """Refuge floor variant: a refuge area (open to the air) attached to the end of the main cluster."""
    r = core.req
    if not r.refuge_floors:
        return
    x0, y0, x1, y1 = unary_union([p.polygon for p in core.parts if p.name not in core.satellites]).bounds
    depth = max(3.0, min(y1 - y0, 6.0))
    width = max(3.0, r.refuge_area_m2 / depth)
    core.parts.append(CorePart("refuge", box(x1, y0, x1 + width, y0 + depth), "REFUGE", {"width": width}))
    core.notes.append(f"refuge floor variant: {width * depth:.0f} m2 refuge area against the core")


CORE_TYPES = {
    "central_compact": central_compact,
    "linear_corridor": linear_corridor,
    "scissor": scissor,
    "split_junction": split_junction,
    "side_core": side_core,
    "end_core": end_core,
}

TYPE_LABELS = {
    "central_compact": "Compact central core (2 stairs + lift bank + lobby)",
    "linear_corridor": "Linear corridor core (mid lift bank, satellite stair)",
    "scissor": "Scissor-stair core",
    "split_junction": "Split core at two wing junctions",
    "side_core": "Façade (side) core",
    "end_core": "End core on a slab",
}


def build_core(kind: str, req: Requirements, **kw) -> Core:
    """Build one core type for these requirements (extra keyword arguments go to the builder)."""
    import inspect

    fn = CORE_TYPES[kind]
    ok = set(inspect.signature(fn).parameters)
    return fn(req, **{k: v for k, v in kw.items() if k in ok})


# --------------------------------------------------------------------------------------------------
# selector
# --------------------------------------------------------------------------------------------------

#: How well each core type suits each plate type, 0..1. Architect priors (what the approved plans and Mumbai
#: practice show) blended with the counted core positions of the 50 approved typical floors; provenance and
#: the counts are in docs/quality/typology-cores.md. Missing entries default to 0.3.
PLATE_SUITABILITY: dict[str, dict[str, float]] = {
    "point_square": {"central_compact": 1.0, "scissor": 0.7, "side_core": 0.5, "split_junction": 0.1,
                     "linear_corridor": 0.1, "end_core": 0.1},
    "linear_slab": {"linear_corridor": 1.0, "end_core": 0.8, "side_core": 0.7, "central_compact": 0.6, "split_junction": 0.5,
                    "scissor": 0.4},
    "L": {"central_compact": 0.8, "split_junction": 0.8, "side_core": 0.7, "linear_corridor": 0.6, "scissor": 0.5,
          "end_core": 0.3},
    "T": {"central_compact": 1.0, "split_junction": 0.8, "side_core": 0.4, "scissor": 0.5, "linear_corridor": 0.5,
          "end_core": 0.3},
    "H": {"split_junction": 1.0, "central_compact": 0.9, "linear_corridor": 0.5, "side_core": 0.3, "scissor": 0.4,
          "end_core": 0.2},
    "cruciform": {"central_compact": 1.0, "scissor": 0.7, "split_junction": 0.6, "side_core": 0.2, "linear_corridor": 0.3,
                  "end_core": 0.1},
    "Y": {"central_compact": 1.0, "scissor": 0.6, "split_junction": 0.4, "side_core": 0.2, "linear_corridor": 0.2,
          "end_core": 0.1},
    "courtyard": {"linear_corridor": 0.8, "split_junction": 0.8, "side_core": 0.6, "central_compact": 0.4, "scissor": 0.3,
                  "end_core": 0.3},
    "other": {"central_compact": 0.7, "split_junction": 0.6, "side_core": 0.6, "linear_corridor": 0.6, "scissor": 0.4,
              "end_core": 0.4},
}


@dataclass
class CoreOption:
    type: str
    core: Core
    score: float
    efficiency: float  # core area / plate area
    compliant: bool
    fits: bool
    suitability: float
    reasons: list[str]
    checks: list[Check] = field(default_factory=list)

    def summary(self) -> str:
        return (f"{self.type}: score {self.score:.2f}, core {self.efficiency:.1%} of plate, "
                f"{'compliant' if self.compliant else 'NOT compliant'}, suitability {self.suitability:.2f}")


def _plate_dims(footprint) -> tuple[float, float, float, Polygon | None]:
    """(long side, short side, area, polygon) from a polygon or a (w, d) pair."""
    if isinstance(footprint, Polygon):
        from . import typology

        r = typology.rectilinearise(footprint) if footprint.area > 100 else footprint
        mrr = r.minimum_rotated_rectangle
        xy = list(mrr.exterior.coords)
        e = sorted([math.dist(xy[0], xy[1]), math.dist(xy[1], xy[2])])
        return e[1], e[0], footprint.area, footprint
    w, d = footprint
    return max(w, d), min(w, d), float(w) * float(d), None


def choose_core(plate_type: str, flats_per_floor: int, height_m: float, footprint, refuge: bool = False,
                floor_h: float | None = None) -> list[CoreOption]:
    """Rank the core types for this plate. ``footprint`` is a shapely polygon (metres) or a ``(length, depth)`` pair.

    Score (0..1) = 0.45 core efficiency + 0.35 suitability for the plate type + 0.20 egress quality, and 0 when the
    core is not code compliant or does not fit in the plate. Core efficiency maps core/plate area 8 % -> 1.0 and
    20 % -> 0.0. Returns every type, best first; ``reasons`` explains each ranking in words (the first entry is the
    recommended core)."""
    long_s, short_s, area, poly = _plate_dims(footprint)
    req = requirements(height_m, flats_per_floor, area, floor_h)
    opts: list[CoreOption] = []
    for kind in CORE_TYPES:
        core = build_core(kind, req, plate_length=long_s, plate_depth=short_s, refuge=refuge and bool(req.refuge_floors))
        checks = core.check()
        errs = [c for c in checks if not c.ok and c.severity == "error"]
        warns = [c for c in checks if not c.ok and c.severity == "warning"]
        eff = core.efficiency(area)
        eff_score = max(0.0, min(1.0, 1.0 - (eff - 0.08) / 0.12))
        suit = PLATE_SUITABILITY.get(plate_type, PLATE_SUITABILITY["other"]).get(kind, 0.3)
        main = unary_union([p.polygon for p in core.parts if p.name not in core.satellites])
        mx0, my0, mx1, my1 = main.bounds
        cw, cd = mx1 - mx0, my1 - my0
        # the main cluster must fit the plate: its long side along the plate length, short side within the depth
        fits = (cw <= long_s + 1e-6 and cd <= short_s * 0.75 + 1e-6) or (cd <= long_s and cw <= short_s * 0.75 + 1e-6)
        if kind == "end_core":
            fits = cd <= short_s + 1e-6 and cw <= long_s * 0.5
        egress = 1.0
        reasons: list[str] = []
        if req.n_stairs == 2:
            if kind in ("linear_corridor", "split_junction", "end_core"):
                egress = 1.0
                reasons.append("stairs are remote from each other (satellite / second cluster)")
            elif kind == "scissor":
                egress = 0.2
                reasons.append("both exits are in one enclosure: only acceptable with authority approval")
            else:
                egress = 0.7
                reasons.append(f"two stairs {core.width - 2 * stair_dims(req.stair_flight_w, req.floor_h).outer_w:.0f} m "
                               "apart inside the core: remoteness depends on the core width")
        if kind == "side_core" and req.smoke_lobby:
            egress = min(1.0, egress + 0.15)
            reasons.append("stairs on the façade: natural ventilation, no pressurisation")
        compliant = not errs
        score = 0.0
        if compliant and fits:
            score = 0.45 * eff_score + 0.35 * suit + 0.20 * egress
            if warns:
                score *= 0.85
        reasons.insert(0, f"core {eff:.1%} of the plate ({core.core_area:.0f} m2), bbox {cw:.1f} x {cd:.1f} m")
        if not compliant:
            reasons.append("fails: " + "; ".join(f"{c.label} ({c.value} vs {c.limit})" for c in errs))
        if not fits:
            reasons.append(f"does not fit the plate ({long_s:.0f} x {short_s:.0f} m)")
        if warns:
            reasons.append("needs the authority's acceptance (scissor pair)")
        reasons.append(f"suitability for a {plate_type} plate {suit:.1f}")
        opts.append(CoreOption(kind, core, score, eff, compliant, fits, suit, reasons, checks))
    opts.sort(key=lambda o: (-o.score, o.efficiency))
    if opts:
        best = opts[0]
        best.reasons.insert(0, f"recommended: {TYPE_LABELS[best.type]} for a {plate_type} plate, {flats_per_floor} flats/floor, "
                               f"{height_m:.0f} m ({req.n_stairs} stairs, {req.n_lifts}+{req.n_fire_lifts} lifts)")
    return opts


# --------------------------------------------------------------------------------------------------
# drawing
# --------------------------------------------------------------------------------------------------

PART_STYLE = {
    "stair": ("#8ecae6", "stair"),
    "lift": ("#ffb703", "lift"),
    "fire_lift": ("#e63946", "fire lift"),
    "lobby": ("#d8f3dc", "lobby"),
    "smoke_lobby": ("#95d5b2", "smoke lobby"),
    "shaft": ("#adb5bd", "shaft / AVS"),
    "refuge": ("#cdb4db", "refuge"),
}


def draw_core(core: Core, ax=None, title: str | None = None, dims: bool = True):
    """Draw a core with its labelled parts (matplotlib)."""
    import matplotlib.pyplot as plt

    if ax is None:
        _fig, ax = plt.subplots(figsize=(7, 5))
    for p in core.parts:
        col, _lab = PART_STYLE[p.kind]
        x, y = p.polygon.exterior.xy
        ax.fill(x, y, color=col, ec="#222", lw=0.9)
        c = p.polygon.centroid
        txt = p.name if p.kind in ("lift", "fire_lift", "stair") else ""
        if p.kind in ("lobby", "smoke_lobby", "shaft", "refuge"):
            txt = f"{p.name}\n{p.area:.1f} m2" if p.area >= 4 else p.name
        if txt:
            ax.text(c.x, c.y, txt, ha="center", va="center", fontsize=6.5)
        if p.kind in ("lift", "fire_lift"):
            x0, y0, x1, y1 = p.polygon.bounds
            ax.text(c.x, y0 + 0.15, f"{p.meta['clear_w']:.1f}x{p.meta['clear_d']:.1f}", ha="center", va="bottom", fontsize=5)
    ax.set_aspect("equal")
    x0, y0, x1, y1 = core.bbox
    if dims:
        ax.annotate("", (x0, y0 - 0.6), (x1, y0 - 0.6), arrowprops=dict(arrowstyle="<->", lw=0.7))
        ax.text((x0 + x1) / 2, y0 - 0.9, f"{x1 - x0:.1f} m", ha="center", va="top", fontsize=7)
        ax.annotate("", (x1 + 0.6, y0), (x1 + 0.6, y1), arrowprops=dict(arrowstyle="<->", lw=0.7))
        ax.text(x1 + 0.9, (y0 + y1) / 2, f"{y1 - y0:.1f} m", ha="left", va="center", fontsize=7, rotation=90)
    (lx0, ly0), (lx1, ly1) = core.lobby_edge
    ax.plot([lx0, lx1], [ly0, ly1], color="#1d3557", lw=2.5)
    ax.text((lx0 + lx1) / 2, ly0 - 0.2 if ly0 == ly1 else ly0, "opens to floor", fontsize=6, color="#1d3557",
            ha="center", va="top" if ly0 == ly1 else "center", rotation=0 if ly0 == ly1 else 90)
    ax.set_xlim(x0 - 1.5, x1 + 2.5)
    ax.set_ylim(y0 - 2.0, y1 + 1.5)
    ax.axis("off")
    m = core.metrics()
    ax.set_title(title or f"{TYPE_LABELS.get(core.type, core.type)}\n{m['core_area_m2']} m2 core, "
                 f"{core.req.n_lifts}+{core.req.n_fire_lifts} lifts, {core.n_exits} exits", fontsize=9)
    return ax
