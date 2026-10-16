"""Frozen data contracts shared by every stage, the UI (via JSON-RPC) and the Claude bridge (via MCP).

All geometry is in metres. Three frames are used:

* ``lnglat``   – WGS84 longitude/latitude, only for the map.
* ``site``     – a local metric frame: UTM coordinates minus ``SiteFrame.origin`` (east = +x, north = +y).
* ``plate``    – an axis-aligned frame per tower plate: site coords rotated by ``-Plate.angle_deg`` about
                 ``Plate.origin``. Units, diagrams and plans live in this frame so the solver works on an
                 integer grid.

Polygons are lists of ``[x, y]`` pairs, counter-clockwise, without a repeated closing vertex.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

Point = tuple[float, float]
Ring = list[Point]
PolyRings = list[Ring]  # [exterior, *holes]

SCHEMA_VERSION = 1


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


# --------------------------------------------------------------------------------------------- evidence


class EvidenceKind(StrEnum):
    observed = "observed"  # measured from an input (click, file, sensor)
    derived = "derived"  # computed by the engine from observed data
    inferred = "inferred"  # chosen by Claude (always re-checked by rules)
    unknown = "unknown"  # not known; shown as "–"


class Actor(StrEnum):
    engine = "engine"
    claude = "claude"
    user = "user"


class Evidence(Model):
    key: str
    value: Any = None
    kind: EvidenceKind
    source: str
    by: Actor = Actor.engine
    stage: int = 0
    ts: float = 0.0


# --------------------------------------------------------------------------------------------- stage 1-3


class GeocodeResult(Model):
    name: str
    display_name: str
    lat: float
    lng: float
    zoom: float = 18.4
    provider: str
    bbox: tuple[float, float, float, float] | None = None


class SiteFrame(Model):
    epsg: int
    origin: Point  # UTM easting/northing of site-frame (0,0)
    scale: float = 1.0  # UTM point scale factor at the origin; site metres = grid metres / scale


class SunSample(Model):
    time: str  # local solar-standard HH:MM
    azimuth: float  # degrees clockwise from north
    altitude: float  # degrees above horizon


class SunPath(Model):
    label: str
    date: str
    noon_altitude: float
    samples: list[SunSample]


class WindBin(Model):
    direction: float  # degrees the wind comes FROM, clockwise from north
    label: str
    frequency: float  # 0..1
    mean_speed: float  # m/s


class WindRose(Model):
    source: str
    indicative: bool
    bins: list[WindBin]
    prevailing: str
    notes: list[str] = Field(default_factory=list)


class Climate(Model):
    sun: list[SunPath]
    wind: WindRose
    hot_facades: list[str]  # e.g. ["W", "WSW"]
    sea_view: Literal["yes", "no", "unknown"] = "unknown"
    creek: Literal["yes", "no", "unknown"] = "unknown"


class Site(Model):
    name: str = "SITE 01"
    locality: str = ""
    lnglat: Ring
    frame: SiteFrame
    polygon: Ring  # site frame
    area: float
    geodesic_area: float
    perimeter: float
    edges: list[float]
    road_width: float = 12.0
    building_height: float = 24.0
    road_edges: list[int] = Field(default_factory=lambda: [0])
    setback: float = 0.0
    setback_clause: str = ""
    buildable: list[Ring] = Field(default_factory=list)  # may be several pieces
    buildable_area: float = 0.0
    climate: Climate | None = None


# --------------------------------------------------------------------------------------------- stage 4


class PlateFootprint(Model):
    id: str
    polygon: Ring  # site frame
    angle_deg: float
    center: Point  # site frame
    width: float  # along plate x (long axis)
    depth: float  # along plate y
    area: float


class PlacementOption(Model):
    id: Literal["A", "B", "C"]
    towers: int
    label: str
    plates: list[PlateFootprint]
    coverage: float
    total_area: float
    min_gap: float | None
    score: float
    score_terms: dict[str, float]
    drivers: str
    runners_up: list[dict[str, Any]] = Field(default_factory=list)
    passes: dict[str, bool] = Field(default_factory=dict)


class Placement(Model):
    options: list[PlacementOption]
    chosen: Literal["A", "B", "C"] | None = None
    elapsed_s: float = 0.0


# --------------------------------------------------------------------------------------------- stage 5-6


class ShapeKind(StrEnum):
    central_core = "central_core"
    cruciform = "cruciform"
    h_shape = "h_shape"
    t_shape = "t_shape"
    triangle = "triangle"
    side_core = "side_core"


class Zone(Model):
    kind: Literal["flats", "core", "passage", "shaft"]
    polygon: list[PolyRings]  # multipolygon parts, each [exterior, *holes], plate frame
    area: float


class CorePart(Model):
    kind: Literal["stair", "lift", "fire_lift", "lobby", "service"]
    polygon: Ring
    width: float = 0.0  # stair flight width / lift car width


class Plate(Model):
    tower_id: str
    shape: ShapeKind
    params: dict[str, float]
    angle_deg: float
    origin: Point  # site-frame position of plate-frame (0,0)
    outline: Ring  # plate frame
    corners: int
    width: float
    depth: float
    area: float
    zones: list[Zone]
    core_parts: list[CorePart] = Field(default_factory=list)
    passage_width: float = 1.8
    ratings: dict[str, int]
    imported_from: str | None = None


class ProgrammeRoom(Model):
    id: str
    type: str  # living, dining, kitchen, master, bedroom, toilet, bath, wc, foyer, passage, utility, balcony, pooja, study
    name: str
    target_area: float
    min_area: float = 0.0
    needs_window: bool = False
    wet: bool = False
    optional: bool = False


class Programme(Model):
    id: str
    name: str
    bhk: int
    area_range: tuple[float, float]
    rooms: list[ProgrammeRoom]
    links: list[tuple[str, str]]
    entry: str
    note: str = "A starting point, not the answer."


class Unit(Model):
    id: str
    polygon: Ring  # plate frame
    area: float
    core_link: Point | None  # door point on the passage, plate frame
    core_link_normal: Point | None = None  # unit-outward normal at the door (points into passage)
    flags: list[str] = Field(default_factory=list)
    programme: Programme | None = None


# --------------------------------------------------------------------------------------------- stage 7


class DiagramRoom(Model):
    id: str
    type: str
    name: str
    target_area: float
    needs_window: bool = False
    optional: bool = False
    placed: bool = True
    x: float = 0.0  # bubble position, UI only
    y: float = 0.0


class Conflict(Model):
    id: str
    rooms: list[str]
    reason: str
    kind: Literal["area", "window", "link", "geometry", "solver"]
    fixes: list[dict[str, Any]] = Field(default_factory=list)


class Diagram(Model):
    unit_id: str
    rooms: list[DiagramRoom]
    links: list[tuple[str, str]]
    entry: str
    conflicts: list[Conflict] = Field(default_factory=list)
    feasible: bool | None = None
    message: str = ""


class PlanRoom(Model):
    id: str
    type: str
    name: str
    polygon: Ring
    area: float
    width: float  # clear minimum width
    has_window: bool = False
    window_area: float = 0.0
    ventilated: bool = False
    vent_via: Literal["window", "shaft", "duct"] | None = None
    unit_id: str | None = None
    target_area: float | None = None


class Opening(Model):
    id: str
    kind: Literal["door", "window", "entry"]
    a: Point
    b: Point
    width: float
    rooms: list[str]
    hinge: Point | None = None
    swing_into: str | None = None


class Wall(Model):
    id: str
    polygon: PolyRings
    thickness: float
    kind: Literal["external", "internal", "rcc"]


class Plan(Model):
    unit_id: str
    rooms: list[PlanRoom]
    walls: list[Wall]
    doors: list[Opening]
    windows: list[Opening]
    carpet_area: float
    status: Literal["optimal", "feasible", "infeasible", "timeout", "imported"]
    solve_time_s: float = 0.0
    objective: float | None = None
    ticket_ids: list[str] = Field(default_factory=list)


# --------------------------------------------------------------------------------------------- stage 8


class CheckResult(Model):
    rule_id: str
    clause: str
    source: str
    title: str
    target: str  # room / unit / building id
    result: Literal["pass", "fail", "review", "n/a"]
    value: float | str | None
    threshold: float | str | None
    unit: str = ""
    message: str = ""
    proof: list[str] = Field(default_factory=list)  # entity handles / ids
    bbox: tuple[float, float, float, float] | None = None


class CheckSummary(Model):
    results: list[CheckResult]
    counts: dict[str, int]
    by_code: dict[str, dict[str, int]]
    disclaimer: str = "Automated pre-check. Confirm with the project's licensed consultant."


class LayerMapping(Model):
    layer: str
    meaning: str  # wall, rcc_wall, column, stair, shaft, window, door, fin, railing, text, room, unit, core, unmapped, ignore
    method: Literal["exact", "fuzzy", "claude", "user", "default"]
    confidence: float
    entities: int


class DrawingReport(Model):
    filename: str
    dxf_version: str
    entities: int
    header_units: str
    detected_units: str
    scale_to_m: float
    layers: list[LayerMapping]
    spaces: int
    flats: int
    outline_corners: int
    outline_area: float
    warnings: list[str] = Field(default_factory=list)


# --------------------------------------------------------------------------------------------- tickets


class TicketKind(StrEnum):
    T0 = "T0"  # stage 3 - sea view / creek nearby
    T1 = "T1"  # stage 8 - unknown layer name
    T2 = "T2"  # stage 8 - unit doubt
    T3 = "T3"  # stage 7 - room clash fix
    T4 = "T4"  # stage 8 - room name from messy text
    T5 = "T5"  # stage 4 - design-driver note
    T6 = "T6"  # stage 8 - explain review result
    T7 = "T7"  # stage 7 - edit in words
    T8 = "T8"  # stage 6 - brief to room list


class TicketOption(Model):
    id: str
    label: str
    payload: dict[str, Any] = Field(default_factory=dict)
    valid: bool = True


class TicketAnswer(Model):
    option_id: str | None = None
    text: str | None = None
    reason: str = ""
    by: Actor
    ts: float = 0.0
    accepted: bool | None = None  # user Keep (True) / Undo (False) / pending (None)


class Ticket(Model):
    id: str
    kind: TicketKind
    stage: int
    subject: str  # e.g. unit id, layer name
    question: str
    options: list[TicketOption]
    default_option: str | None
    evidence: list[str] = Field(default_factory=list)
    status: Literal["open", "defaulted", "answered", "kept", "undone"] = "defaulted"
    check_me: bool = True
    answer: TicketAnswer | None = None
    applied_option: str | None = None
    free_text: bool = False
    created: float = 0.0


# --------------------------------------------------------------------------------------------- project


class Project(Model):
    schema_version: int = SCHEMA_VERSION
    id: str
    name: str
    stage: int = 1
    site: Site | None = None
    placement: Placement | None = None
    plates: list[Plate] = Field(default_factory=list)
    units: list[Unit] = Field(default_factory=list)
    diagrams: list[Diagram] = Field(default_factory=list)
    plans: list[Plan] = Field(default_factory=list)
    checks: CheckSummary | None = None
    drawing: DrawingReport | None = None
    created: float = 0.0
    updated: float = 0.0
