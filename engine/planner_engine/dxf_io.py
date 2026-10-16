"""DXF export (R2007, named layers, metres) and import (stage 8a).

Import pipeline: read every entity -> work out units (header vs. size) -> map layers to meaning
(exact name, then fuzzy, then a Claude ticket, then the user confirms once and the office map learns) ->
find rooms (closed room polylines, else polygonize walls with door gaps closed) -> label with the nearest
text -> group rooms into flats behind each entry door -> build Plans for the rule checks.
"""

from __future__ import annotations

import difflib
import json
import math
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import ezdxf
import shapely
from ezdxf import bbox as ezbbox
from ezdxf.enums import TextEntityAlignment
from shapely.geometry import LineString, Point as SPoint, Polygon, box

from . import geo
from .models import DrawingReport, LayerMapping, Opening, Plan, PlanRoom, Plate, Wall

APPID = "IVAI"
LAYERS: dict[str, tuple[int, str]] = {  # layer -> (ACI colour, meaning)
    "A-PLATE": (8, "outline"),
    "A-CORE": (9, "core"),
    "A-PASSAGE": (254, "passage"),
    "A-SHAFT": (4, "shaft"),
    "A-UNIT": (1, "unit"),
    "A-AREA": (3, "room"),
    "A-WALL": (7, "wall"),
    "A-WALL-RCC": (5, "rcc_wall"),
    "S-COLS": (5, "column"),
    "A-STAIR": (2, "stair"),
    "A-LIFT": (6, "lift"),
    "A-DOOR": (30, "door"),
    "A-GLAZ": (140, "window"),
    "A-FIN": (8, "fin"),
    "A-RAIL": (8, "railing"),
    "A-ANNO-TEXT": (7, "text"),
}
MEANINGS = ["wall", "rcc_wall", "column", "stair", "lift", "shaft", "window", "door", "fin", "railing", "text",
            "room", "unit", "core", "passage", "outline", "ignore", "unmapped"]

EXACT: dict[str, str] = {name.lower(): meaning for name, (_, meaning) in LAYERS.items()} | {
    "wall": "wall", "walls": "wall", "a-wall-full": "wall", "a-wall-prht": "wall", "partition": "wall",
    "brick": "wall", "brickwork": "wall", "masonry": "wall", "blockwork": "wall",
    "rcc": "rcc_wall", "rcc wall": "rcc_wall", "shear wall": "rcc_wall", "s-wall": "rcc_wall", "concrete": "rcc_wall",
    "column": "column", "columns": "column", "col": "column", "s-col": "column", "s-cols": "column", "pillar": "column",
    "stair": "stair", "stairs": "stair", "staircase": "stair", "a-flor-strs": "stair",
    "lift": "lift", "elevator": "lift", "a-flor-evtr": "lift",
    "shaft": "shaft", "duct": "shaft", "ots": "shaft", "a-shaft": "shaft",
    "window": "window", "windows": "window", "glazing": "window", "a-glaz": "window", "win": "window",
    "door": "door", "doors": "door", "a-door": "door",
    "fin": "fin", "fins": "fin", "railing": "railing", "rail": "railing", "a-flor-hral": "railing",
    "text": "text", "anno": "text", "a-anno-text": "text", "room names": "text", "labels": "text",
    "room": "room", "rooms": "room", "a-area": "room", "space": "room", "spaces": "room",
    "unit": "unit", "units": "unit", "flat": "unit", "flats": "unit", "apartment": "unit",
    "core": "core", "passage": "passage", "corridor": "passage", "lobby": "passage",
    "outline": "outline", "a-plate": "outline", "dim": "ignore", "dims": "ignore", "a-anno-dims": "ignore",
    "defpoints": "ignore", "furniture": "ignore", "a-furn": "ignore", "0": "unmapped",
}
TOKENS: list[tuple[str, str]] = [
    (r"rcc|shear|concrete|conc\b", "rcc_wall"), (r"wall|partition|brick|masonry|block", "wall"),
    (r"col(umn)?s?\b|pillar", "column"), (r"stair|steps?\b", "stair"), (r"lift|elev", "lift"),
    (r"shaft|duct|\bots\b", "shaft"), (r"win(dow)?|glaz|glass", "window"), (r"door|\bdr\b", "door"),
    (r"\bfins?\b", "fin"), (r"rail", "railing"), (r"text|anno|label|name|tag", "text"),
    (r"room|area|space", "room"), (r"\bunit|flat|apart|\bapt", "unit"), (r"core", "core"),
    (r"passage|corridor|lobby", "passage"), (r"dim|furn|hatch-pattern|defpoints|viewport|xref", "ignore"),
]
ROOM_WORDS: list[tuple[str, str]] = [
    (r"toil|\bwc\b|w\.c|bath|wash\s*room|t\s*&\s*b", "toilet"),
    (r"master|m\.?\s*bed|mbr", "master"), (r"bed|br\b|bedroom", "bedroom"), (r"liv|drawing|hall|lounge", "living"),
    (r"din", "dining"), (r"kit", "kitchen"),
    (r"foyer|entr", "foyer"), (r"pass|corr|lobby", "passage"), (r"bal|deck|sit.?out", "balcony"),
    (r"util|wash area|dry", "utility"), (r"pooja|puja|prayer", "pooja"), (r"study|office", "study"),
    (r"store", "store"), (r"dress|wardrobe", "dress"),
]
COMMON_TYPES = {"passage"}
ROOM_TYPES_ALL = {"living", "dining", "kitchen", "master", "bedroom", "study", "pooja", "toilet", "bath", "wc",
                  "foyer", "passage", "utility", "balcony", "dress", "store"}
UNITS_TO_M = {0: None, 1: 0.0254, 2: 0.3048, 4: 0.001, 5: 0.01, 6: 1.0, 14: 0.1}
UNIT_NAMES = {0: "unitless", 1: "inches", 2: "feet", 4: "mm", 5: "cm", 6: "metres", 14: "dm"}


def _config_dir() -> Path:
    d = Path(os.environ.get("PLANNER_HOME", Path.home() / ".apartment-planner"))
    d.mkdir(parents=True, exist_ok=True)
    return d


def office_layer_map() -> dict[str, str]:
    p = _config_dir() / "layer_map.json"
    if p.exists():
        try:
            return json.loads(p.read_text())
        except json.JSONDecodeError:
            return {}
    return {}


def learn_layer(layer: str, meaning: str) -> None:
    """Save a confirmed mapping so the office never has to answer it again."""
    if meaning not in MEANINGS:
        raise ValueError(f"unknown meaning {meaning!r}")
    m = office_layer_map()
    m[layer.lower()] = meaning
    (_config_dir() / "layer_map.json").write_text(json.dumps(m, indent=1, sort_keys=True))


def map_layer(name: str) -> tuple[str, str, float]:
    """Return (meaning, method, confidence)."""
    low = name.strip().lower()
    learned = office_layer_map()
    if low in learned:
        return learned[low], "user", 1.0
    if low in EXACT:
        return EXACT[low], "exact", 1.0
    for pat, meaning in TOKENS:
        if re.search(pat, low):
            return meaning, "fuzzy", 0.8
    close = difflib.get_close_matches(low, list(EXACT), n=1, cutoff=0.75)
    if close:
        return EXACT[close[0]], "fuzzy", 0.6
    return "unmapped", "default", 0.0


def room_type_from_text(text: str) -> str | None:
    low = text.strip().lower()
    for pat, t in ROOM_WORDS:
        if re.search(pat, low):
            return t
    return None


def guess_room_type(poly: Polygon) -> str:
    """T4 safe default: label by size and shape."""
    a = poly.area
    w = geo.clear_width(poly)
    minx, miny, maxx, maxy = poly.bounds
    aspect = max(maxx - minx, maxy - miny) / max(0.1, min(maxx - minx, maxy - miny))
    if aspect > 3.5 and w < 1.6:
        return "passage"
    if a < 2.0:
        return "store"
    if a < 5.0:
        return "toilet"
    if a < 8.0:
        return "kitchen"
    if a < 16.0:
        return "bedroom"
    return "living"


# ------------------------------------------------------------------------------------------- export


def _layers(doc) -> None:
    for name, (color, _) in LAYERS.items():
        if name not in doc.layers:
            doc.layers.add(name, color=color)
    if APPID not in doc.appids:
        doc.appids.new(APPID)


def _poly(msp, ring, layer: str, close: bool = True, xdata: list | None = None):
    e = msp.add_lwpolyline([(float(x), float(y)) for x, y in ring], close=close, dxfattribs={"layer": layer})
    if xdata:
        e.set_xdata(APPID, xdata)
    return e


def export_dxf(path: str | Path, plate: Plate, plans: list[Plan], *, project: str = "", site_note: str = "") -> dict[str, int]:
    """Write one floor (plate frame, metres) as DXF R2007 with named layers."""
    doc = ezdxf.new("R2007", setup=True)
    doc.header["$INSUNITS"] = 6
    doc.header["$MEASUREMENT"] = 1
    _layers(doc)
    msp = doc.modelspace()
    _poly(msp, plate.outline, "A-PLATE", xdata=[(1000, f"plate:{plate.tower_id}"), (1000, plate.shape.value)])
    for z in plate.zones:
        if z.kind in ("core", "passage", "shaft"):
            for part in z.polygon:
                for ring in part:
                    _poly(msp, ring, {"core": "A-CORE", "passage": "A-PASSAGE", "shaft": "A-SHAFT"}[z.kind])
    for cp in plate.core_parts:
        if cp.kind == "stair":
            e = _poly(msp, cp.polygon, "A-STAIR", xdata=[(1000, "stair"), (1040, float(cp.width))])
            # flight lines
            minx, miny, maxx, maxy = Polygon(cp.polygon).bounds
            mid = (minx + maxx) / 2
            msp.add_line((mid, miny), (mid, maxy), dxfattribs={"layer": "A-STAIR"})
            n = max(2, int((maxy - miny) / 0.3))
            for i in range(1, n):
                y = miny + i * (maxy - miny) / n
                msp.add_line((minx, y), (maxx, y), dxfattribs={"layer": "A-STAIR"})
            _ = e
        elif cp.kind in ("lift", "fire_lift"):
            _poly(msp, cp.polygon, "A-LIFT", xdata=[(1000, cp.kind)])
            minx, miny, maxx, maxy = Polygon(cp.polygon).bounds
            msp.add_line((minx, miny), (maxx, maxy), dxfattribs={"layer": "A-LIFT"})
            msp.add_line((minx, maxy), (maxx, miny), dxfattribs={"layer": "A-LIFT"})
    # core RCC walls (230 mm ring inside the core outline)
    core_geom = shapely.union_all([geo.from_polyrings(z.polygon) for z in plate.zones if z.kind == "core"])
    rcc = core_geom.difference(core_geom.buffer(-0.23, join_style="mitre"))
    for part in geo.to_polyrings(rcc):
        h = msp.add_hatch(color=5, dxfattribs={"layer": "A-WALL-RCC"})
        h.paths.add_polyline_path(part[0], is_closed=True)
        for hole in part[1:]:
            h.paths.add_polyline_path(hole, is_closed=True)
        for ring in part:
            _poly(msp, ring, "A-WALL-RCC")
    # columns at plate corners
    ring = geo.ring_of(Polygon(plate.outline))
    for i, (x, y) in enumerate(ring):
        c = box(x - 0.3, y - 0.3, x + 0.3, y + 0.3).intersection(Polygon(plate.outline))
        if c.area > 0.05:
            _poly(msp, geo.ring_of(c), "S-COLS", xdata=[(1000, f"col:{i}")])
    for plan in plans:
        unit_rooms = [Polygon(r.polygon) for r in plan.rooms]
        unit_geom = shapely.union_all([g.buffer(0) for g in unit_rooms + [geo.from_polyrings([w.polygon]) for w in plan.walls]])
        for part in geo.to_polyrings(unit_geom.buffer(0)):
            _poly(msp, part[0], "A-UNIT", xdata=[(1000, f"unit:{plan.unit_id}")])
        for r in plan.rooms:
            _poly(msp, r.polygon, "A-AREA", xdata=[(1000, f"room:{plan.unit_id}/{r.id}"), (1000, r.type)])
            p = Polygon(r.polygon).representative_point()
            t = msp.add_mtext(f"{r.name.upper()}\\P{r.area:.2f} m²", dxfattribs={"layer": "A-ANNO-TEXT", "char_height": 0.22})
            t.set_location((p.x, p.y), attachment_point=5)
            if r.type == "balcony":
                bl = Polygon(r.polygon).exterior
                _poly(msp, list(bl.coords)[:-1], "A-RAIL")
        for w in plan.walls:
            h = msp.add_hatch(color=7, dxfattribs={"layer": "A-WALL"})
            h.paths.add_polyline_path(w.polygon[0], is_closed=True)
            for hole in w.polygon[1:]:
                h.paths.add_polyline_path(hole, is_closed=True)
            for ring_ in w.polygon:
                _poly(msp, ring_, "A-WALL", xdata=[(1000, f"wall:{w.id}")])
        for d in plan.doors:
            ax, ay = d.a
            bx, by = d.b
            L = math.dist(d.a, d.b)
            if L <= 0:
                continue
            ux, uy = (bx - ax) / L, (by - ay) / L
            # leaf drawn open at 90 degrees, arc from closed to open
            nx, ny = -uy, ux
            msp.add_line((ax, ay), (ax + nx * L, ay + ny * L), dxfattribs={"layer": "A-DOOR"}).set_xdata(
                APPID, [(1000, f"door:{plan.unit_id}/{d.id}"), (1000, d.kind), (1040, float(d.width))]
            )
            start = math.degrees(math.atan2(uy, ux))
            msp.add_arc((ax, ay), L, start, start + 90, dxfattribs={"layer": "A-DOOR"})
        for wnd in plan.windows:
            msp.add_line(wnd.a, wnd.b, dxfattribs={"layer": "A-GLAZ"}).set_xdata(
                APPID, [(1000, f"window:{plan.unit_id}/{wnd.id}"), (1040, float(wnd.width))]
            )
    if project or site_note:
        t = msp.add_text(f"{project} {site_note}".strip(), dxfattribs={"layer": "A-ANNO-TEXT", "height": 0.5})
        minx, miny, maxx, maxy = Polygon(plate.outline).bounds
        t.set_placement((minx, miny - 2.0), align=TextEntityAlignment.LEFT)
    doc.saveas(str(path))
    return layer_counts(doc)


def layer_counts(doc) -> dict[str, int]:
    out: dict[str, int] = {}
    for e in doc.modelspace():
        out[e.dxf.layer] = out.get(e.dxf.layer, 0) + 1
    return out


def roundtrip_check(path: str | Path, tmp: str | Path) -> dict[str, Any]:
    """Save-and-reopen test: every entity must survive (0 lost)."""
    doc = ezdxf.readfile(str(path))
    before = layer_counts(doc)
    handles_before = {e.dxf.handle for e in doc.modelspace()}
    doc.saveas(str(tmp))
    doc2 = ezdxf.readfile(str(tmp))
    after = layer_counts(doc2)
    handles_after = {e.dxf.handle for e in doc2.modelspace()}
    lost = sum(max(0, before[k] - after.get(k, 0)) for k in before)
    return {"before": sum(before.values()), "after": sum(after.values()), "lost": lost,
            "handles_lost": len(handles_before - handles_after)}


# ------------------------------------------------------------------------------------------- import


@dataclass
class ImportResult:
    report: DrawingReport
    plans: list[Plan]
    stairs: list[tuple[list[tuple[float, float]], float]]
    passage_width: float | None
    handles: dict[str, list[str]]
    outline: list[tuple[float, float]]
    tickets: list[dict] = field(default_factory=list)
    rooms_geo: dict[str, Polygon] = field(default_factory=dict)


def _read(path: str | Path):
    try:
        return ezdxf.readfile(str(path))
    except ezdxf.DXFStructureError:
        from ezdxf import recover

        doc, _auditor = recover.readfile(str(path))
        return doc


def _entity_geom(e, scale: float):
    t = e.dxftype()
    try:
        if t == "LWPOLYLINE":
            pts = [(p[0] * scale, p[1] * scale) for p in e.get_points("xy")]
            if len(pts) < 2:
                return None, False
            closed = bool(e.closed) or (len(pts) > 2 and math.dist(pts[0], pts[-1]) < 1e-6)
            if closed and len(pts) >= 3:
                pg = Polygon(pts).buffer(0)
                return pg, True
            return LineString(pts), False
        if t == "POLYLINE":
            pts = [(v.dxf.location.x * scale, v.dxf.location.y * scale) for v in e.vertices]
            if len(pts) < 2:
                return None, False
            if e.is_closed and len(pts) >= 3:
                return Polygon(pts).buffer(0), True
            return LineString(pts), False
        if t == "LINE":
            s, en = e.dxf.start, e.dxf.end
            return LineString([(s.x * scale, s.y * scale), (en.x * scale, en.y * scale)]), False
        if t == "ARC":
            c = e.dxf.center
            pts = [(p.x * scale, p.y * scale) for p in e.flattening(0.05)]
            _ = c
            return LineString(pts) if len(pts) > 1 else None, False
        if t == "CIRCLE":
            c = e.dxf.center
            return SPoint(c.x * scale, c.y * scale).buffer(e.dxf.radius * scale), True
        if t == "HATCH":
            polys = []
            for p in e.paths:
                if hasattr(p, "vertices"):
                    pts = [(v[0] * scale, v[1] * scale) for v in p.vertices]
                    if len(pts) >= 3:
                        polys.append(Polygon(pts).buffer(0))
            if polys:
                return shapely.union_all(polys), True
            return None, False
        if t == "INSERT":
            pts = []
            for ve in e.virtual_entities():
                g, _ = _entity_geom(ve, scale)
                if g is not None:
                    pts.append(g)
            return (shapely.union_all(pts), False) if pts else (None, False)
    except Exception:  # noqa: BLE001 - messy files: skip entities we cannot read, never crash
        return None, False
    return None, False


def _text_of(e) -> tuple[str, tuple[float, float]] | None:
    t = e.dxftype()
    if t == "TEXT":
        return e.dxf.text, (e.dxf.insert.x, e.dxf.insert.y)
    if t == "MTEXT":
        return e.plain_text(), (e.dxf.insert.x, e.dxf.insert.y)
    return None


def import_dxf(path: str | Path, overrides: dict[str, str] | None = None, unit_choice: str | None = None) -> ImportResult:
    """Read a DXF floor plan and build Plans for checking. ``overrides`` maps layer -> meaning (answers to T1);
    ``unit_choice`` is "header" or "size" (answer to T2)."""
    overrides = {k.lower(): v for k, v in (overrides or {}).items()}
    doc = _read(path)
    msp = doc.modelspace()
    entities = list(msp)
    tickets: list[dict] = []
    warnings: list[str] = []
    ins = int(doc.header.get("$INSUNITS", 0) or 0)
    header_scale = UNITS_TO_M.get(ins)
    try:
        ext = ezbbox.extents(entities, fast=True)
        span = max(ext.size.x, ext.size.y) if ext.has_data else 0.0
    except Exception:  # noqa: BLE001
        span = 0.0
    size_scale = 1.0 if span < 1500 else 0.001  # a floor plate is 10-200 m across
    detected = "metres" if size_scale == 1.0 else "mm"
    if header_scale is None:
        scale = size_scale
        warnings.append(f"Header has no units; size says {detected}.")
    elif abs(header_scale - size_scale) > 1e-12 and span > 0:
        msg = f"Units: {UNIT_NAMES.get(ins, ins)} in header, but the drawing size says {detected}."
        warnings.append(msg)
        scale = header_scale if unit_choice == "header" else size_scale
        tickets.append({
            "kind": "T2", "subject": Path(path).name, "question": msg + " Which is right?",
            "options": [{"id": "size", "label": f"Trust the size ({detected})"},
                        {"id": "header", "label": f"Trust the header ({UNIT_NAMES.get(ins, ins)})"}],
            "default": "size", "evidence": [f"extent {span:.1f} drawing units", f"$INSUNITS={ins}"],
        })
    else:
        scale = header_scale
    # layer mapping
    counts: dict[str, int] = {}
    for e in entities:
        counts[e.dxf.layer] = counts.get(e.dxf.layer, 0) + 1
    mapping: dict[str, LayerMapping] = {}
    for layer, n in sorted(counts.items()):
        if layer.lower() in overrides:
            meaning, method, conf = overrides[layer.lower()], "user", 1.0
        else:
            meaning, method, conf = map_layer(layer)
        mapping[layer] = LayerMapping(layer=layer, meaning=meaning, method=method, confidence=conf, entities=n)
        if meaning == "unmapped" and n > 0:
            sample = [e.dxftype() for e in entities if e.dxf.layer == layer][:5]
            guess = None
            if all(s in ("HATCH",) for s in sample):
                guess = "wall"
            tickets.append({
                "kind": "T1", "subject": layer, "question": f"What is layer '{layer}' ({n} entities: {', '.join(sorted(set(sample)))})?",
                "options": [{"id": m, "label": m.replace("_", " ")} for m in MEANINGS if m != "unmapped"],
                "default": "unmapped" if guess is None else guess, "evidence": [f"{n} entities", f"types {sorted(set(sample))}"],
            })
    by_meaning: dict[str, list] = {}
    texts: list[tuple[str, tuple[float, float], str]] = []
    for e in entities:
        meaning = mapping[e.dxf.layer].meaning
        tx = _text_of(e)
        if tx is not None:
            txt, (x, y) = tx
            texts.append((txt, (x * scale, y * scale), e.dxf.handle))
            continue
        g, closed = _entity_geom(e, scale)
        if g is None or g.is_empty:
            continue
        by_meaning.setdefault(meaning, []).append((g, closed, e.dxf.handle, e))
    # rooms: explicit room polylines first
    rooms: list[tuple[Polygon, list[str]]] = []
    xtype: dict[int, tuple[str, str]] = {}  # room index -> (type, id) from our own XDATA
    for g, closed, h, e in by_meaning.get("room", []):
        if closed:
            for p in geo.polygons_of(g):
                if p.area > 0.5:
                    if e.has_xdata(APPID):
                        vals = [str(v) for c, v in e.get_xdata(APPID) if c == 1000]
                        rid = next((v[5:] for v in vals if v.startswith("room:")), "")
                        rt = next((v for v in vals if v in ROOM_TYPES_ALL), None)
                        if rt:
                            xtype[len(rooms)] = (rt, rid)
                    rooms.append((p, [h]))
    barrier_parts = []
    for key in ("wall", "rcc_wall", "column"):
        for g, closed, _h, _e in by_meaning.get(key, []):
            barrier_parts.append(g if closed else g.buffer(0.06))
    for g, closed, _h, _e in by_meaning.get("window", []):
        barrier_parts.append(g.buffer(0.08))
    barrier = shapely.union_all(barrier_parts) if barrier_parts else Polygon()
    if not rooms and not barrier.is_empty:
        closed_b = barrier.buffer(0.55, join_style="mitre").buffer(-0.55, join_style="mitre")  # close door gaps <= 1.1 m
        minx, miny, maxx, maxy = closed_b.bounds
        free = box(minx - 1, miny - 1, maxx + 1, maxy + 1).difference(closed_b)
        frame = box(minx - 1, miny - 1, maxx + 1, maxy + 1).exterior
        for p in geo.polygons_of(free):
            if p.area < 0.8 or p.intersects(frame):
                continue
            # give back the door-gap closing: rooms are bounded by the real walls
            near = [h for g, c, h, _e in by_meaning.get("wall", []) + by_meaning.get("rcc_wall", []) if g.distance(p) < 0.3]
            rooms.append((p, near[:6]))
    # outline
    everything = [g if c else g.buffer(0.06) for k, lst in by_meaning.items() if k not in ("ignore", "text") for g, c, _h, _e in lst]
    allg = shapely.union_all(everything) if everything else Polygon()
    shell = allg.buffer(0.3, join_style="mitre").buffer(-0.3, join_style="mitre") if not allg.is_empty else allg
    shell_parts = geo.polygons_of(shell)
    outline_poly = max(shell_parts, key=lambda p: p.area) if shell_parts else Polygon()
    outline_poly = Polygon(outline_poly.exterior) if not outline_poly.is_empty else outline_poly
    outline_ring = geo.ring_of(outline_poly.simplify(0.02)) if not outline_poly.is_empty else []
    # labels
    named: list[dict] = []
    for i, (p, handles) in enumerate(rooms):
        inside = [(t, xy, h) for t, xy, h in texts if p.contains(SPoint(xy))]
        label, rtype, lh = None, None, []
        if i in xtype:
            rtype = xtype[i][0]
            label = inside[0][0].split("\n")[0].strip().title() if inside else rtype.title()
            lh = [inside[0][2]] if inside else []
        for t, _xy, h in ([] if rtype else inside):
            rt = room_type_from_text(t)
            if rt:
                label, rtype, lh = t.split("\n")[0].strip().title(), rt, [h]
                break
        if rtype is None:
            rtype = guess_room_type(p)
            label = rtype.title()
            if inside:
                tickets.append({
                    "kind": "T4", "subject": f"S{i + 1}", "question": f"What room is labelled '{inside[0][0][:40]}'?",
                    "options": [{"id": t, "label": t} for t in ("living", "dining", "kitchen", "master", "bedroom", "toilet",
                                                                "passage", "foyer", "balcony", "utility", "store")],
                    "default": rtype, "evidence": [f"{p.area:.1f} m²", f"text '{inside[0][0][:40]}'"],
                })
        named.append({"id": f"S{i + 1}", "poly": p, "type": rtype, "name": label, "handles": handles + lh})
    # units: explicit flat outlines, else door connectivity
    units: list[tuple[str, Polygon, list[str]]] = []
    for g, closed, h, e in by_meaning.get("unit", []):
        if closed:
            uid = None
            xd = e.get_xdata(APPID) if e.has_xdata(APPID) else None
            if xd:
                for code, val in xd:
                    if code == 1000 and str(val).startswith("unit:"):
                        uid = str(val)[5:]
            for p in geo.polygons_of(g):
                units.append((uid or f"FLAT-{len(units) + 1}", p, [h]))
    door_geoms = [(g, h, e) for g, c, h, e in by_meaning.get("door", [])]
    door_pts: list[tuple[Any, str, float, bool]] = []
    for g, h, e in door_geoms:
        width, is_entry = 0.9, False
        if e.has_xdata(APPID):
            for code, val in e.get_xdata(APPID):
                if code == 1040:
                    width = float(val)
                if code == 1000 and val == "entry":
                    is_entry = True
        else:
            if g.geom_type == "LineString":
                width = round(g.length, 3)
        door_pts.append((g, h, width, is_entry))
    room_unit: dict[str, str] = {}
    if units:
        for r in named:
            c = r["poly"].representative_point()
            for uid, up, _h in units:
                if up.buffer(0.05).contains(c):
                    room_unit[r["id"]] = uid
                    break
    else:
        common = {r["id"] for r in named if r["type"] in COMMON_TYPES and r["poly"].area > 12}
        adj: dict[str, set[str]] = {r["id"]: set() for r in named}
        for g, _h, _w, _ent in door_pts:
            near = [r["id"] for r in named if r["poly"].distance(g) < 0.35]
            for a in near:
                for b in near:
                    if a != b:
                        adj[a].add(b)
        seen: set[str] = set()
        k = 0
        for r in named:
            if r["id"] in seen or r["id"] in common:
                continue
            k += 1
            stack = [r["id"]]
            while stack:
                cur = stack.pop()
                if cur in seen:
                    continue
                seen.add(cur)
                room_unit[cur] = f"FLAT-{k}"
                stack.extend(n for n in adj[cur] if n not in seen and n not in common)
    plans: list[Plan] = []
    handles: dict[str, list[str]] = {}
    windows_g = [(g, h) for g, c, h, _e in by_meaning.get("window", [])]
    shaft_g = [g for g, c, h, _e in by_meaning.get("shaft", [])]
    for uid in sorted({u for u in room_unit.values()}):
        prs, doors, wins = [], [], []
        for r in named:
            if room_unit.get(r["id"]) != uid:
                continue
            p = r["poly"]
            near_w = [(g, h) for g, h in windows_g if g.distance(p) < 0.45]
            win_len = sum(g.length if g.geom_type == "LineString" else 0.0 for g, _h in near_w)
            has_window = win_len > 0.3
            vent = has_window or any(s.distance(p) < 0.45 for s in shaft_g)
            prs.append(PlanRoom(id=r["id"], type=r["type"], name=r["name"],
                                polygon=[(round(x, 3), round(y, 3)) for x, y in geo.ring_of(p)], area=round(p.area, 2),
                                width=geo.clear_width(p), has_window=has_window, window_area=round(win_len * 1.5, 3),
                                ventilated=vent, unit_id=uid))
            handles[r["id"]] = r["handles"] + [h for _g, h in near_w]
            for g, h in near_w:
                if g.geom_type == "LineString":
                    cs = list(g.coords)
                    wins.append(Opening(id=f"W{h}", kind="window", a=cs[0], b=cs[-1], width=round(g.length, 3), rooms=[r["id"]]))
        uroom_ids = {pr.id for pr in prs}
        upoly = shapely.union_all([Polygon(pr.polygon) for pr in prs]).buffer(0.3)
        for g, h, w, is_entry in door_pts:
            if g.distance(upoly) > 0.4:
                continue
            near = [r["id"] for r in named if r["poly"].distance(g) < 0.35]
            inside = [n for n in near if n in uroom_ids]
            outside = [n for n in near if n not in uroom_ids]
            entry = is_entry or (bool(inside) and (bool(outside) or len(near) == 1))
            cs = list(g.coords) if g.geom_type == "LineString" else [g.centroid.coords[0]] * 2
            doors.append(Opening(id=f"D{h}", kind="entry" if entry else "door", a=cs[0], b=cs[-1],
                                 width=w, rooms=inside))
        handles[uid] = [h for pr in prs for h in handles.get(pr.id, [])][:50]
        carpet = sum(pr.area for pr in prs if pr.type != "balcony")
        plans.append(Plan(unit_id=uid, rooms=prs, walls=[], doors=doors, windows=wins, carpet_area=round(carpet, 2),
                          status="imported"))
    # stairs
    stairs = []
    for g, closed, h, _e in by_meaning.get("stair", []):
        if closed:
            for p in geo.polygons_of(g):
                if p.area < 4:
                    continue
                mrr = p.minimum_rotated_rectangle
                xs = list(mrr.exterior.coords)
                sides = sorted({round(math.dist(xs[i], xs[i + 1]), 3) for i in range(4)})
                short = sides[0]
                flight = round((short - 0.2) / 2, 3) if short > 2.4 else round(short, 3)
                stairs.append((geo.ring_of(p), flight))
                handles.setdefault("floor", []).append(h)
    pw = None
    common_rooms = [r for r in named if r["type"] == "passage" and r["id"] not in room_unit]
    for g, closed, h, _e in by_meaning.get("passage", []):
        if closed:
            for p in geo.polygons_of(g):
                common_rooms.append({"poly": p})
    if common_rooms:
        widths = [geo.clear_width(r["poly"]) for r in common_rooms]
        pw = round(min(widths), 3) if widths else None
    report = DrawingReport(
        filename=Path(path).name,
        dxf_version=f"{doc.dxfversion} ({ezdxf.const.acad_release.get(doc.dxfversion, '?')})",
        entities=len(entities),
        header_units=UNIT_NAMES.get(ins, str(ins)),
        detected_units="metres" if scale == 1.0 else UNIT_NAMES.get(ins, "mm") if scale == header_scale else detected,
        scale_to_m=scale,
        layers=list(mapping.values()),
        spaces=len(named),
        flats=len(plans),
        outline_corners=len(outline_ring),
        outline_area=round(outline_poly.area, 2) if not outline_poly.is_empty else 0.0,
        warnings=warnings,
    )
    return ImportResult(report=report, plans=plans, stairs=stairs, passage_width=pw, handles=handles,
                        outline=outline_ring, tickets=tickets, rooms_geo={r["id"]: r["poly"] for r in named})


def walls_from_plan(plan: Plan) -> list[Wall]:
    return plan.walls
