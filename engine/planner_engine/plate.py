"""Stage 5 - shape the floor.

Six parametric templates (central core, cruciform, H, T, triangle, side core). Each is a function of
named parameters with limits. ``fit`` scales the defaults to the tower envelope from stage 4; every edit
in the UI sends new parameters and gets the zones back. Zones are computed by geometric difference, so
flats + core + passage + shafts == plate, always.

Cores (Phase 2 of docs/quality/typology-cores.md): the core of a rectilinear template is chosen from the library in
``cores.py`` (``choose_core`` ranks six parametric types for the plate type, flats per floor and height), placed per
the template (central / at the wing junction / on the façade / at the slab end, with a satellite stair where the type
has one) and its typed parts become ``core_parts``. ``core_type`` (plate parameter, 0 = auto) lets the user pick another
type; ``core_info`` returns the ranked alternatives with reasons. The triangle keeps its own core (experimental).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import get_args

import shapely
from shapely import affinity
from shapely.geometry import LineString, Polygon, box
from shapely.ops import unary_union

from . import cores, geo, rules
from .models import CorePart, Plate, PlateFootprint, ShapeKind, Zone

# Ratings written by the architect: core efficiency, daylight, structure (1-5)
RATINGS: dict[ShapeKind, dict[str, int]] = {
    ShapeKind.central_core: {"core_efficiency": 5, "daylight": 3, "structure": 5},
    ShapeKind.cruciform: {"core_efficiency": 4, "daylight": 5, "structure": 3},
    ShapeKind.h_shape: {"core_efficiency": 3, "daylight": 4, "structure": 3},
    ShapeKind.t_shape: {"core_efficiency": 3, "daylight": 4, "structure": 3},
    ShapeKind.triangle: {"core_efficiency": 3, "daylight": 4, "structure": 2},
    ShapeKind.side_core: {"core_efficiency": 3, "daylight": 3, "structure": 4},
}

LABELS = {
    ShapeKind.central_core: "Central core",
    ShapeKind.cruciform: "Cruciform",
    ShapeKind.h_shape: "H-shape",
    ShapeKind.t_shape: "T-shape",
    ShapeKind.triangle: "Triangle",
    ShapeKind.side_core: "Side core",
}


@dataclass(frozen=True)
class Limit:
    lo: float
    hi: float
    step: float = 0.1


def core_size(height: float, flight: float | None = None) -> tuple[float, float, int]:
    """Default core (width, depth, lifts) from the rules/design files."""
    c = rules.design()["core"]
    flight = float(flight or c["stair_flight_width"])
    stair_w = 2 * flight + 0.2
    lift_w = float(c["lift_size"][0]) + 0.2
    lifts = 2 if height <= 45 else 3 if height <= 70 else 4
    lifts += 1 if height > 15 else 0  # fire lift
    width = 2 * stair_w + lifts * lift_w + 1.5  # + service shaft
    depth = float(c["stair_length"])
    return round(width, 1), round(depth, 1), lifts


def limits(shape: ShapeKind, env_w: float, env_d: float) -> dict[str, Limit]:
    base = {
        "W": Limit(12.0, max(12.0, env_w)),
        "D": Limit(10.0, max(10.0, env_d)),
        "core_w": Limit(4.0, max(4.0, env_w - 4.0)),
        "core_d": Limit(4.0, max(4.0, env_d - 4.0)),
        "passage": Limit(1.5, 3.0),
        "units": Limit(1, 16, 1),
        "shafts": Limit(0, 4, 1),
        "shaft_size": Limit(1.2, 6.0),
        "end_depth": Limit(4.0, 20.0),
        "chowks": Limit(0, 1, 1),
        "party_shafts": Limit(0, 1, 1),
        "core_type": Limit(0, 6, 1),  # 0 = auto, 1.. = cores.CORE_TYPES in order
        "refuge": Limit(0, 1, 1),  # 1 = draw the core of a refuge floor (refuge area against the core)
    }
    if shape == ShapeKind.cruciform:
        base |= {"arm_depth": Limit(8.0, env_d), "wing_width": Limit(8.0, env_w)}
    elif shape == ShapeKind.h_shape:
        base |= {"bar_width": Limit(6.0, env_w / 2), "bridge_depth": Limit(6.0, env_d)}
    elif shape == ShapeKind.t_shape:
        base |= {"bar_depth": Limit(8.0, env_d), "stem_width": Limit(8.0, env_w)}
    elif shape == ShapeKind.triangle:
        base |= {"corner_cut": Limit(0.0, 12.0), "unit_depth": Limit(7.0, 14.0)}
    return base


T_BAY = 8.8  # least bar façade per flat in a T-shape (m)


def default_params(shape: ShapeKind, env_w: float, env_d: float, height: float = 24.0) -> dict[str, float]:
    cw, cd, _ = core_size(height)
    W, D = math.floor(env_w * 10) / 10, math.floor(env_d * 10) / 10
    p: dict[str, float] = {
        "W": W,
        "D": D,
        "core_w": min(cw, W - 6.0),
        "core_d": min(cd, D - 6.0),
        "passage": float(rules.design()["core"]["passage_width"]),
        "shafts": 0,
        "shaft_size": 3.0,
        "end_depth": round(min(9.0, max(5.0, W / 6)), 1),
        "chowks": 1.0,  # v3: exterior chowks between flats deeper than their façade can light
        "party_shafts": 1.0,  # v3: AVS on the party walls between flats, for the wet rooms
        "core_type": 0.0,  # 0 = auto (best ranked by cores.choose_core), 1.. = cores.CORE_TYPES in order
        "refuge": 0.0,
    }
    if shape == ShapeKind.cruciform:
        p["arm_depth"] = round(max(10.0, D * 0.55), 1)
        p["wing_width"] = round(max(10.0, W * 0.40), 1)
        p["core_w"] = min(p["core_w"], p["wing_width"] - 1.0)
        p["shafts"] = 2
    elif shape == ShapeKind.h_shape:
        p["bar_width"] = round(max(8.0, W * 0.28), 1)
        p["bridge_depth"] = round(max(8.0, D * 0.5), 1)
        p["core_d"] = min(p["core_d"], p["bridge_depth"] - 2 * p["passage"])
        p["core_w"] = min(p["core_w"], W - 2 * p["bar_width"] - 1.0)
        p["shafts"] = 2
    elif shape == ShapeKind.t_shape:
        p["bar_depth"] = round(max(10.0, D * 0.5), 1)
        p["stem_width"] = round(max(10.0, W * 0.4), 1)
        p["core_w"] = min(p["core_w"], p["stem_width"] - 1.0)
        p["shafts"] = 2
    elif shape == ShapeKind.triangle:
        p["corner_cut"] = round(min(7.0, max(3.0, min(W, D) * 0.14)), 1)
        p["unit_depth"] = 9.5
        p["shafts"] = 1  # air shafts on the party walls between flats (0 = none)
        # compact core: two stairs either side of two lifts, lobby in front
        p["core_w"] = 11.8
        p["core_d"] = 8.0
    p["core_w"] = max(4.0, round(p["core_w"], 1))
    p["core_d"] = max(4.0, round(p["core_d"], 1))
    flats_guess = W * D * (0.75 if shape != ShapeKind.triangle else 0.33)
    n = max(2, int(round(flats_guess / 150.0)))
    p["units"] = float(n + (n % 2 if shape in (ShapeKind.central_core, ShapeKind.side_core) else 0))
    if shape == ShapeKind.triangle:
        # flats along every façade with ~11-12 m of frontage each (a 2BHK's window rooms)
        p["units"] = float(tri_default_units(p))
    elif shape == ShapeKind.t_shape:
        # W*D counts the two empty corner quadrants, so the area rule above over-fills a wide T (8 flats on a
        # 52.9 m bar leave 8-9 m wide bays that cannot light a 2BHK, and the end cuts crowd into slivers):
        # one flat per ~8.8 m of bar at most
        p["units"] = float(max(2, min(int(p["units"]), int(round(W / T_BAY)))))
    return p


def clamp_params(shape: ShapeKind, params: dict[str, float], env_w: float, env_d: float) -> dict[str, float]:
    lim = limits(shape, env_w, env_d)
    out = dict(params)
    for k, l in lim.items():
        if k in out:
            v = min(max(float(out[k]), l.lo), l.hi)
            out[k] = round(round(v / l.step) * l.step, 3)
    return out


def _core_center(shape: ShapeKind, p: dict[str, float]) -> tuple[float, float]:
    W, D = p["W"], p["D"]
    if shape == ShapeKind.side_core:
        return (-W / 2 + p["core_w"] / 2, 0.0)
    if shape == ShapeKind.t_shape:
        # core in the stem, just below the bar: bar flats stay single-loaded and deep
        return (0.0, D / 2 - p["bar_depth"] - p["core_d"] / 2 + p["passage"])
    if shape == ShapeKind.triangle:
        return tri_incenter(p)
    return (0.0, 0.0)


def _outline(shape: ShapeKind, p: dict[str, float]) -> Polygon:
    W, D = p["W"], p["D"]
    if shape in (ShapeKind.central_core, ShapeKind.side_core):
        return box(-W / 2, -D / 2, W / 2, D / 2)
    if shape == ShapeKind.cruciform:
        a, w = min(p["arm_depth"], D), min(p["wing_width"], W)
        return shapely.union(box(-W / 2, -a / 2, W / 2, a / 2), box(-w / 2, -D / 2, w / 2, D / 2))
    if shape == ShapeKind.h_shape:
        b, br = min(p["bar_width"], W / 2 - 1), min(p["bridge_depth"], D)
        return shapely.union_all(
            [box(-W / 2, -D / 2, -W / 2 + b, D / 2), box(W / 2 - b, -D / 2, W / 2, D / 2), box(-W / 2, -br / 2, W / 2, br / 2)]
        )
    if shape == ShapeKind.t_shape:
        bd, sw = min(p["bar_depth"], D), min(p["stem_width"], W)
        return shapely.union(box(-W / 2, D / 2 - bd, W / 2, D / 2), box(-sw / 2, -D / 2, sw / 2, D / 2 - bd + 0.001))
    if shape == ShapeKind.triangle:
        return _truncate(_tri_vertices(p), p.get("corner_cut", 5.0))
    raise ValueError(shape)


def _tri_vertices(p: dict[str, float]) -> list[tuple[float, float]]:
    W, D = p["W"], p["D"]
    return [(-W / 2, -D / 2), (W / 2, -D / 2), (0.0, D / 2)]


def _truncate(verts: list[tuple[float, float]], c: float) -> Polygon:
    """Cut each corner of a polygon by ``c`` metres along both edges (no unusable acute tips)."""
    if c <= 0:
        return Polygon(verts)
    out = []
    n = len(verts)
    for i in range(n):
        p0, p1, p2 = verts[i - 1], verts[i], verts[(i + 1) % n]
        for q in (p0, p2):
            L = math.dist(p1, q)
            t = min(c, L * 0.3) / L
            out.append((p1[0] + (q[0] - p1[0]) * t, p1[1] + (q[1] - p1[1]) * t))
        # keep CCW order: point toward previous vertex first, then toward next
    return Polygon(out).buffer(0)


def tri_incenter(p: dict[str, float]) -> tuple[float, float]:
    A, B, C = _tri_vertices(p)
    a, b, c = math.dist(B, C), math.dist(A, C), math.dist(A, B)
    s = a + b + c
    return ((a * A[0] + b * B[0] + c * C[0]) / s, (a * A[1] + b * B[1] + c * C[1]) / s)


def _offset_tri(verts: list[tuple[float, float]], offs: list[float]) -> list[tuple[float, float]] | None:
    """Triangle bounded by each side of ``verts`` (CCW) moved inwards by ``offs[i]`` (side i runs from vertex
    i to vertex i+1). Inner vertex i belongs to outer vertex i. None if the offsets swallow the triangle."""
    lines = []
    for i in range(3):
        (ax, ay), (bx, by) = verts[i], verts[(i + 1) % 3]
        L = math.hypot(bx - ax, by - ay)
        nx, ny = -(by - ay) / L, (bx - ax) / L  # left normal = inward for a CCW ring
        lines.append(((ax + nx * offs[i], ay + ny * offs[i]), (bx - ax, by - ay)))
    out = []
    for i in range(3):
        (p1, d1), (p2, d2) = lines[i - 1], lines[i]
        den = d1[0] * d2[1] - d1[1] * d2[0]
        if abs(den) < 1e-9:
            return None
        t = ((p2[0] - p1[0]) * d2[1] - (p2[1] - p1[1]) * d2[0]) / den
        out.append((p1[0] + d1[0] * t, p1[1] + d1[1] * t))
    area2 = sum(out[i][0] * out[(i + 1) % 3][1] - out[(i + 1) % 3][0] * out[i][1] for i in range(3))
    return out if area2 > 2.0 else None


TRI_MIN_DEPTH = 6.5  # shallowest façade band (small plates only: 1BHKs)
TRI_DOOR_RUN = 2.4  # lobby frontage a flat beside a façade core keeps for its entry


def tri_layout(p: dict[str, float]) -> dict:
    """Where the core, lobby and façade bands of a triangle plate go (pure function of the parameters).

    * ``central``: big plates. Every façade keeps a band of ``unit_depth``; the lobby is the inner triangle
      and the core sits in it, on its base where it is widest.
    * ``base``: plates too small for a central core at that depth (a ~45 m triangle). The bands keep their
      depth and the core moves onto the middle of the base façade instead (stairs get daylight); the lobby is
      the inner triangle in front of it. Flats on the base either side of the core reach the lobby along its
      base; a short stub widens that frontage when the inner triangle is narrow.
    Band depth only drops (towards TRI_MIN_DEPTH) when even the base layout does not leave a lobby."""
    verts = _tri_vertices(p)
    cw, cd, pw = p["core_w"], p["core_d"], p["passage"]
    ic = tri_incenter(p)
    depth = float(p.get("unit_depth", 9.5))
    floor_d = min(depth, TRI_MIN_DEPTH)
    while depth >= floor_d - 1e-9:
        inner = _offset_tri(verts, [depth] * 3)
        if inner is not None:
            ip = Polygon(inner)
            y0 = ip.bounds[1] + pw
            while y0 + cd <= ip.bounds[3]:
                c = box(ic[0] - cw / 2, y0, ic[0] + cw / 2, y0 + cd)
                if ip.contains(c):
                    return {"mode": "central", "depth": depth, "base_depth": depth, "inner": inner, "core": c,
                            "stub": None, "own": ["prev", "prev", "prev"]}
                y0 += 0.25
        # façade core: on the middle of the base, the base band as deep as the others (the strip in front of
        # the core is its lift lobby, part of the floor lobby)
        if inner is not None:
            ybase = verts[0][1]
            bd = max(depth, cd)
            core = box(-cw / 2, ybase, cw / 2, ybase + cd)
            if bd > depth:
                inner = _offset_tri(verts, [bd, depth, depth])
            if inner is not None and inner[1][0] - inner[0][0] >= cw - 1.0:
                A1, B1 = inner[0], inner[1]
                run = min(-cw / 2 - A1[0], B1[0] - cw / 2)
                top = ybase + max(bd, cd + 1.5)
                # flats beside the core enter from the lift lobby strip (bd - cd deep) or the lobby base (run);
                # when both are too short the strip runs on past the core
                half = cw / 2 + (TRI_DOOR_RUN if max(run, bd - cd) < 1.5 else 0.0)
                stub = box(-half, ybase + cd, half, top) if top > ybase + cd + 1e-6 else None
                return {"mode": "base", "depth": depth, "base_depth": bd, "inner": inner, "core": core, "stub": stub,
                        "own": ["next", "prev", "split"]}
        depth -= 0.25
    return {"mode": "small", "depth": floor_d, "base_depth": floor_d, "inner": None,
            "core": None, "stub": None, "own": ["split", "split", "split"]}


TRI_FLAT_FRONTAGE = 11.4  # deep façade a 2BHK needs (living, two bedrooms, kitchen side by side, on the grid)


def tri_default_units(p: dict[str, float]) -> int:
    """Flats per floor: each façade run (a band, or a base band on either side of a façade core) gets one
    flat per TRI_FLAT_FRONTAGE of deep façade (façade with a room's depth behind it), at least one."""
    from shapely import affinity

    from .units import ROOM_DEPTH

    lay = tri_layout(p)
    outline = _truncate(_tri_vertices(p), p.get("corner_cut", 5.0))
    taken = shapely.union_all([g for g in (lay["core"], lay["stub"]) if g is not None]
                              + ([Polygon(lay["inner"])] if lay["inner"] else []))
    outline = outline.difference(taken.buffer(0.001))  # (no float slivers joining the runs)
    n = 0
    for sec in tri_sectors(p, lay):
        a, b = sec["a"], sec["b"]
        L = math.dist(a, b)
        nx, ny = -(b[1] - a[1]) / L, (b[0] - a[0]) / L  # inward normal (CCW)
        face = LineString([a, b]).buffer(0.02)
        for part in geo.polygons_of(sec["poly"].intersection(outline)):
            if part.area < 1.0:
                continue
            deep = part.intersection(affinity.translate(part, -nx * ROOM_DEPTH, -ny * ROOM_DEPTH))
            run = part.boundary.intersection(face).intersection(deep.buffer(0.05)).length
            n += max(1, int(run / TRI_FLAT_FRONTAGE + 1e-6))
    return max(2, n)


def _line_x(p1, d1, p2, d2) -> tuple[float, float]:
    den = d1[0] * d2[1] - d1[1] * d2[0]
    t = ((p2[0] - p1[0]) * d2[1] - (p2[1] - p1[1]) * d2[0]) / den
    return (p1[0] + d1[0] * t, p1[1] + d1[1] * t)


def tri_sectors(p: dict[str, float], lay: dict | None = None) -> list[dict]:
    """The part of the plate each façade's band owns: [{"i", "a", "b", "poly"}] (side i runs a -> b).

    Bands meet at the corners by ownership (``lay["own"]``, per vertex): the owning band runs on to the
    other façade (its lobby line extended), so the other band starts with a 60° wedge instead of the 30°
    sliver a bisector split leaves; flats stay rectangles with a short slanted end. ``split`` cuts on the
    line from the corner to the lobby corner. Without a lobby triangle the sectors meet at the incentre."""
    lay = lay or tri_layout(p)
    verts = _tri_vertices(p)
    inner = lay["inner"]
    if inner is None:
        ic = tri_incenter(p)
        return [{"i": i, "a": verts[i], "b": verts[(i + 1) % 3],
                 "poly": Polygon([ic, verts[i], verts[(i + 1) % 3]]).buffer(0)} for i in range(3)]

    def side(i):
        a, b = verts[i % 3], verts[(i + 1) % 3]
        return a, (b[0] - a[0], b[1] - a[1])

    def off(i):  # lobby line of side i (through inner vertices i, i+1)
        a, b = inner[i % 3], inner[(i + 1) % 3]
        return a, (b[0] - a[0], b[1] - a[1])

    own = lay["own"]
    out = []
    for i in range(3):
        j0, j1 = i, (i + 1) % 3
        pts: list[tuple[float, float]] = []
        if own[j0] == "next":  # this band owns its first corner
            pts += [_line_x(*off(i), *side(i - 1)), verts[j0]]
        elif own[j0] == "prev":
            pts += [_line_x(*off(i - 1), *side(i))]
        else:
            pts += [verts[j0]]
        if own[j1] == "prev":  # this band owns its last corner
            pts += [verts[j1], _line_x(*off(i), *side(i + 1))]
        elif own[j1] == "next":
            pts += [_line_x(*off(i + 1), *side(i))]
        else:
            pts += [verts[j1]]
        pts += [inner[j1], inner[j0]]
        out.append({"i": i, "a": verts[j0], "b": verts[j1], "poly": Polygon(pts).buffer(0)})
    return out


TRI_SHAFT_LEAD = (1.6, 1.1)  # party-wall air shaft width either side of the party wall (larger, smaller share)
TRI_SHAFT_D = 2.0  # shaft depth into the band (AVS >= 3 m2, shorter side >= 1.5 m)


def tri_access_run(p: dict[str, float]) -> tuple[float, tuple[float, float]]:
    """(entry run, shaft shares) for cutting a triangle band: every flat keeps the entry run of lobby
    frontage, plus the share of each party-wall shaft that notches it (both flats get a shaft wall)."""
    from .units import TRI_ACCESS_RUN

    return TRI_ACCESS_RUN, (TRI_SHAFT_LEAD if int(p.get("shafts", 0)) > 0 else (0.0, 0.0))


def _triangle_shafts(rest, passage, p: dict[str, float]) -> list:
    """Air shafts (AVS) on the party walls between neighbouring flats of a triangle plate, against the lobby
    where the entries, toilets and kitchens cluster, so wet rooms ventilate without taking façade from the
    bedrooms. Each shaft straddles a cut, notching both flats (a room grid only sees a shaft wall where the
    flat's outline turns), with the larger share in the flat that has lobby frontage to spare.
    units.tri_runs is the single source of the cut positions."""
    from shapely import affinity

    from .units import tri_runs

    out = []
    for r in tri_runs(p, rest, int(p["units"]), passage, *tri_access_run(p)):
        rot = r["rot"]
        minx, miny, maxx, maxy = rot.bounds
        for c in r["cuts"]:
            x0, x1 = c - r["lb"], c + r["la"]
            ln = LineString([(c, miny - 1), (c, maxy + 1)]).intersection(rot)
            if ln.is_empty:
                continue
            y0, y1 = ln.bounds[1], ln.bounds[3]
            if y1 - y0 < TRI_SHAFT_D + 5.0:  # too shallow for rooms in front of a shaft
                continue
            sq = box(x0, y1 - TRI_SHAFT_D, x1, y1)
            g = affinity.rotate(sq, r["ang"], origin=(0, 0)).intersection(rest)
            if g.area > 0.5:
                out.append(g)
    return out


def _triangle_corner_shafts(rest, p: dict[str, float]) -> list:
    """Cut the sharp corners off the flats that own them: past the point where the façade is truncated (or
    where the other façade crosses the band's lobby line) a corner band is a sliver no room grid can use, so it
    becomes an open-to-sky shaft. The flat behind it stays a rectangle on its façade, and its wet rooms get a
    full-depth shaft wall to ventilate on."""
    from shapely import affinity

    lay = tri_layout(p)
    own = lay["own"]
    out = []
    for sec in tri_sectors(p, lay):
        i = sec["i"]
        ends = [own[i] == "next", own[(i + 1) % 3] == "prev"]  # owns its first / last corner
        if not any(ends):
            continue
        a, b = sec["a"], sec["b"]
        ang = math.degrees(math.atan2(b[1] - a[1], b[0] - a[0]))
        parts = [g for g in geo.polygons_of(rest.intersection(sec["poly"])) if g.area > 1.0]
        if not parts:
            continue
        rots = [affinity.rotate(g, -ang, origin=(0, 0)) for g in parts]
        for end, pick in ((0, min(range(len(rots)), key=lambda k: rots[k].bounds[0])),
                          (1, max(range(len(rots)), key=lambda k: rots[k].bounds[2]))):
            if not ends[end]:
                continue
            g = rots[pick]
            minx, miny, maxx, maxy = g.bounds
            bottom = g.intersection(box(minx - 1, miny - 0.01, maxx + 1, miny + 0.01))
            top = g.intersection(box(minx - 1, maxy - 0.01, maxx + 1, maxy + 0.01))
            if bottom.is_empty or top.is_empty:
                continue
            if end == 0:
                x = max(bottom.bounds[0], top.bounds[0])
                piece = g.intersection(box(minx - 1, miny - 1, x, maxy + 1))
            else:
                x = min(bottom.bounds[2], top.bounds[2])
                piece = g.intersection(box(x, miny - 1, maxx + 1, maxy + 1))
            piece = affinity.rotate(piece, ang, origin=(0, 0))
            if piece.area > 0.5:
                out.append(piece)
    return out


def _triangle_zones(outline: Polygon, p: dict[str, float]):
    """Triangle plate: a band of flats along every façade, core + lobby where ``tri_layout`` puts them.

    Zones never overlap: core and lobby are cut first, the flats are what is left (build_plate)."""
    lay = tri_layout(p)
    p["core_at_base"] = 1.0 if lay["mode"] == "base" else 0.0
    p["band_depth"] = round(lay["depth"], 2)  # what the plate got (``unit_depth`` stays the request)
    if lay["mode"] == "small":  # plate too small for a full core: shrink the core (flagged by NBC checks)
        ic = tri_incenter(p)
        cw, cd = p["core_w"], p["core_d"]
        inner = outline.buffer(-lay["depth"], join_style="mitre", mitre_limit=5.0)
        if inner.is_empty:
            inner = outline.buffer(-6.5, join_style="mitre", mitre_limit=5.0)
        core = None
        for k in (0.9, 0.8, 0.7, 0.6):
            c = box(ic[0] - cw * k / 2, ic[1] - cd * k / 2, ic[0] + cw * k / 2, ic[1] + cd * k / 2)
            if not inner.is_empty and inner.contains(c):
                core = c
                break
        if core is None:
            core = box(ic[0] - 3.0, ic[1] - 2.0, ic[0] + 3.0, ic[1] + 2.0).intersection(outline)
        passage = inner.difference(core) if not inner.is_empty else core.buffer(1.5).difference(core)
        return core, shapely.set_precision(passage.intersection(outline), 0.001)
    core = lay["core"].intersection(outline)
    lobby = Polygon(lay["inner"])
    if lay["stub"] is not None:
        lobby = shapely.union(lobby, lay["stub"])
    passage = lobby.intersection(outline).difference(core)
    return core, shapely.set_precision(passage, 0.001)


def _corridors(shape: ShapeKind, p: dict[str, float], core_c: tuple[float, float]) -> list[LineString]:
    W, D, e = p["W"], p["D"], p["end_depth"]
    cx, cy = core_c
    lines: list[LineString] = []
    if shape == ShapeKind.central_core:
        if W / 2 - e > p["core_w"] / 2:
            lines.append(LineString([(-(W / 2 - e), 0), (W / 2 - e, 0)]))
    elif shape == ShapeKind.side_core:
        x_end = W / 2 - e
        if x_end > cx + p["core_w"] / 2:
            lines.append(LineString([(cx, 0), (x_end, 0)]))
    elif shape == ShapeKind.cruciform:
        if W / 2 - e > p["core_w"] / 2:
            lines.append(LineString([(-(W / 2 - e), 0), (W / 2 - e, 0)]))
        if D / 2 - e > p["core_d"] / 2:
            lines.append(LineString([(0, -(D / 2 - e)), (0, D / 2 - e)]))
    elif shape == ShapeKind.h_shape:
        xb = W / 2 - p["bar_width"] / 2
        lines.append(LineString([(-xb, 0), (xb, 0)]))
        if D / 2 - e > p["passage"]:
            lines.append(LineString([(-xb, -(D / 2 - e)), (-xb, D / 2 - e)]))
            lines.append(LineString([(xb, -(D / 2 - e)), (xb, D / 2 - e)]))
    elif shape == ShapeKind.t_shape:
        yb = D / 2 - p["bar_depth"] + p["passage"] / 2  # corridor along the inner edge of the bar
        if W / 2 - e > p["core_w"] / 2:
            lines.append(LineString([(-(W / 2 - e), yb), (W / 2 - e, yb)]))
        y_end = -D / 2 + e
        if y_end < cy - p["core_d"] / 2:
            lines.append(LineString([(0, cy), (0, y_end)]))
    elif shape == ShapeKind.triangle:
        verts = [(-W / 2, -D / 2), (W / 2, -D / 2), (0.0, D / 2)]
        for vx, vy in verts:
            dx, dy = vx - cx, vy - cy
            L = math.hypot(dx, dy)
            t = max(0.0, (L - e * 1.4) / L)
            lines.append(LineString([(cx, cy), (cx + dx * t, cy + dy * t)]))
    return lines


# ---- core library wiring (docs/quality/cores-wired.md)
CORE_ORDER: tuple[str, ...] = tuple(cores.CORE_TYPES)
SLAB_ASPECT = 1.9  # bbox aspect from which a rectangle is a slab (typology.py uses the same)
MARGIN = 0.02  # m: a core box may touch the outline


def core_code(name: str | float | int | None) -> int:
    """Plate parameter value of a core type: 0 = auto, 1.. = ``CORE_ORDER``. Accepts the name, the code or None."""
    if name is None:
        return 0
    if isinstance(name, str):
        n = name.strip().lower()
        if n in ("", "auto", "0"):
            return 0
        if n not in CORE_ORDER:
            raise ValueError(f"unknown core type {name!r}; use auto or one of {', '.join(CORE_ORDER)}")
        return CORE_ORDER.index(n) + 1
    c = int(round(float(name)))
    return c if 0 <= c <= len(CORE_ORDER) else 0


def core_name(code: float | int) -> str | None:
    """Core type of a parameter value (None for auto)."""
    c = int(round(float(code)))
    return CORE_ORDER[c - 1] if 1 <= c <= len(CORE_ORDER) else None


def plate_type_of(shape: ShapeKind, p: dict[str, float]) -> str:
    """Typology name (``typology.classify`` vocabulary) of a template, for ``cores.choose_core``."""
    if shape == ShapeKind.cruciform:
        return "cruciform"
    if shape == ShapeKind.h_shape:
        return "H"
    if shape == ShapeKind.t_shape:
        return "T"
    if shape == ShapeKind.side_core:
        return "linear_slab"
    long_s, short_s = max(p["W"], p["D"]), min(p["W"], p["D"])
    return "linear_slab" if long_s / max(short_s, 1e-6) >= SLAB_ASPECT else "point_square"


@dataclass
class CorePlan:
    """A library core placed in a plate (plate frame)."""

    type: str
    core: cores.Core
    rot: int
    parts: list[tuple[str, str, Polygon, dict]]  # (kind, name, polygon, meta) with the library's own kinds
    boxes: list[Polygon]  # core zone: bbox of the main cluster + one box per satellite cluster
    base: tuple[float, float]  # where the template's own corridors expect the core centre
    dims: tuple[float, float]  # main cluster (width, depth) in the plate frame
    where: str = ""  # one phrase: where it sits


def _group(core: cores.Core):
    """(main cluster parts, satellite parts). A corridor strip of a satellite is passage, not core."""
    sat_names = set(core.satellites)
    main = [q for q in core.parts if q.name not in sat_names]
    sat = [q for q in core.parts if q.name in sat_names and not (q.kind == "lobby" and q.meta.get("corridor"))]
    return main, sat


def _rotated(parts, rot: int):
    return [affinity.rotate(q.polygon, rot, origin=(0.0, 0.0)) for q in parts]


def _anchor0(shape: ShapeKind, p: dict[str, float], mw: float, md: float) -> tuple[float, float]:
    """Where the template's own corridors expect the core (what ``_core_center`` was for the old box core)."""
    W, D = p["W"], p["D"]
    if shape == ShapeKind.side_core:
        return (-W / 2 + mw / 2, 0.0)
    if shape == ShapeKind.t_shape:
        return (0.0, D / 2 - p["bar_depth"] - md / 2 + p["passage"])
    return (0.0, 0.0)


def _slide(make_box, start, direction, ol: Polygon, limit: float = 80.0, step: float = 0.1):
    """Move a box from ``start`` along ``direction`` until it would leave ``ol``; the last position inside (None if
    it does not fit at ``start``)."""
    if not ol.covers(make_box(start)):
        return None
    dx, dy = direction
    last, t = start, step
    while t <= limit:
        c = (start[0] + dx * t, start[1] + dy * t)
        if not ol.covers(make_box(c)):
            break
        last, t = c, t + step
    return last


def _slide_in(make_box, start, direction, ol: Polygon, limit: float = 12.0, step: float = 0.1):
    """First position from ``start`` along ``direction`` where the box is inside ``ol`` (None within ``limit``)."""
    dx, dy = direction
    t = 0.0
    while t <= limit:
        c = (start[0] + dx * t, start[1] + dy * t)
        if ol.covers(make_box(c)):
            return c
        t += step
    return None


def _place_core(core: cores.Core, ctype: str, shape: ShapeKind, p: dict[str, float], outline: Polygon,
                rot: int) -> CorePlan | None:
    """Place a library core in the plate, or None if it does not fit this template at this rotation.

    Main cluster: ``central_compact`` / ``scissor`` / ``linear_corridor`` / ``split_junction`` at the template's core
    position (the centre; the junction of a T; the left end of a side-core slab), ``side_core`` slid against an
    exterior wall with its back to it, ``end_core`` slid to the end wall. A satellite cluster (second stair) goes to
    the far end of the template's longest corridor, or to the other wing junction of an H."""
    main, sat = _group(core)
    if not main:
        return None
    mg = unary_union(_rotated(main, rot))
    x0, y0, x1, y1 = mg.bounds
    mw, md = x1 - x0, y1 - y0
    W = p["W"]
    pq = {**p, "core_w": mw, "core_d": md}
    base = _anchor0(shape, pq, mw, md)
    ol = outline.buffer(MARGIN, join_style="mitre")

    def mbox(c, w=mw, d=md):
        return box(c[0] - w / 2, c[1] - d / 2, c[0] + w / 2, c[1] + d / 2)

    where = "at the template's core position"
    pos: tuple[float, float] | None = base
    xb = W / 2 - p.get("bar_width", 0.0) / 2
    if ctype == "side_core":
        if shape == ShapeKind.side_core:
            where = "on the end wall (façade core)"
        else:
            pos = _slide(mbox, base, (0.0, -1.0), ol)
            where = "against the south façade (back wall exterior)"
    elif ctype == "end_core":
        ay = p["D"] / 2 - p["bar_depth"] / 2 if shape == ShapeKind.t_shape else 0.0
        pos = _slide(mbox, (base[0], ay), (-1.0, 0.0), ol)
        where = "on the short end wall of the slab"
    elif ctype == "split_junction" and shape == ShapeKind.h_shape:
        pos = _slide_in(mbox, (-xb, 0.0), (1.0, 0.0), ol)
        where = "at the left wing junction (cluster B at the right one)"
    elif ctype == "split_junction":
        where = "at the wing junction (cluster B at the far end)"
    if pos is None or not ol.covers(mbox(pos)):
        return None
    cm = ((x0 + x1) / 2, (y0 + y1) / 2)
    shift = (pos[0] - cm[0], pos[1] - cm[1])
    parts = [(q.kind, q.name, affinity.translate(affinity.rotate(q.polygon, rot, origin=(0.0, 0.0)), *shift), dict(q.meta))
             for q in main]
    boxes = [mbox(pos)]
    if sat:
        sg = unary_union(_rotated(sat, rot))
        sx0, sy0, sx1, sy1 = sg.bounds
        sw, sd = sx1 - sx0, sy1 - sy0
        scm = ((sx0 + sx1) / 2, (sy0 + sy1) / 2)
        keep_out = boxes[0].buffer(max(p["passage"], 1.5))
        spos = None
        if ctype == "split_junction" and shape == ShapeKind.h_shape:
            spos = _slide_in(lambda c: mbox(c, sw, sd), (xb, 0.0), (-1.0, 0.0), ol)
        else:
            lines = _corridors(shape, pq, base)
            cands = [pt for ln in lines for pt in ln.coords]
            cands += [(W / 2 - sw / 2 - 0.5, pos[1]), (-W / 2 + sw / 2 + 0.5, pos[1])]
            cands.sort(key=lambda c: -math.dist(c, pos))
            for c in cands:
                if math.dist(c, pos) < 1e-6:
                    continue
                d = (pos[0] - c[0], pos[1] - c[1])
                n = math.hypot(*d)
                t = 0.0
                while t < n - 1e-6:
                    q = (c[0] + d[0] / n * t, c[1] + d[1] / n * t)
                    sb = mbox(q, sw, sd)
                    if ol.covers(sb) and not sb.intersects(keep_out):
                        spos = q
                        break
                    t += 0.25
                if spos is not None:
                    break
        if spos is None:
            return None
        sb = mbox(spos, sw, sd)
        if not ol.covers(sb) or sb.intersects(boxes[0]):
            return None
        sshift = (spos[0] - scm[0], spos[1] - scm[1])
        parts += [(q.kind, q.name, affinity.translate(affinity.rotate(q.polygon, rot, origin=(0.0, 0.0)), *sshift),
                   dict(q.meta)) for q in sat]
        boxes.append(sb)
    return CorePlan(ctype, core, rot, parts, boxes, base, (mw, md), where)


# rotations tried in order (degrees): the library frame has its lobby on the front (y = 0) and its back on +y
MIN_FLATS_SHARE = 0.30  # a placement must leave this share of the plate as flats ...
REL_FLATS_SHARE = 0.80  # ... and this share of the best placement's


def _rotations(ctype: str, shape: ShapeKind) -> tuple[int, ...]:
    if ctype == "side_core":
        return (90, 270, 0, 180) if shape == ShapeKind.side_core else (180, 0, 90, 270)
    if ctype == "end_core":
        return (0, 180)
    return (0, 90, 180, 270)


def _alt_row(o: cores.CoreOption, placed: CorePlan | None, why: str) -> dict:
    return {"type": o.type, "label": cores.TYPE_LABELS[o.type], "code": core_code(o.type), "score": round(o.score, 3),
            "core_share": round(o.efficiency, 4), "core_area_m2": round(o.core.core_area, 1), "compliant": o.compliant,
            "fits": bool(o.fits), "placed": placed is not None, "why_not": why, "reasons": list(o.reasons),
            "n_exits": o.core.n_exits, "needs_authority_acceptance": bool(o.core.needs_authority_acceptance)}


_PLAN_CACHE: dict[tuple, tuple] = {}
_NOT_KEYS = frozenset({"core_w", "core_d", "core_type_used", "height_m", "core_at_base", "band_depth"})


def plan_core(shape: ShapeKind, p: dict[str, float], height: float):
    """(chosen CorePlan or None, alternatives rows, info dict) for a template with these parameters. Cached: a drag
    sends many parameter sets and ``core_info`` repeats what ``build_plate`` did."""
    key = (shape.value, round(height, 2), tuple(sorted((k, round(float(v), 3)) for k, v in p.items() if k not in _NOT_KEYS)))
    hit = _PLAN_CACHE.get(key)
    if hit is None:
        outline = shapely.set_precision(_outline(shape, p).buffer(0), 0.001)
        hit = _plan_core_uncached(shape, p, outline, height)
        if len(_PLAN_CACHE) >= 96:
            _PLAN_CACHE.pop(next(iter(_PLAN_CACHE)))
        _PLAN_CACHE[key] = hit
    return hit


def _plan_core_uncached(shape: ShapeKind, p: dict[str, float], outline: Polygon, height: float):
    ptype = plate_type_of(shape, p)
    flats = max(1, int(round(p["units"])))
    refuge = bool(int(p.get("refuge", 0)))
    options = cores.choose_core(ptype, flats, height, outline, refuge=refuge)
    req_code = int(round(p.get("core_type", 0)))
    rows: list[dict] = []
    plans: dict[str, CorePlan] = {}
    for o in options:
        # a core is placed with the library builder run at the plate's size (the satellite stair uses it)
        plan = None
        for rot in _rotations(o.type, shape):
            plan = _place_core(o.core, o.type, shape, p, outline, rot)
            if plan:
                break
        why = "" if plan else ("does not fit this template (outline too small / wrong shape for the cluster)")
        share = None
        if plan:
            pq = {**p, "core_w": plan.dims[0], "core_d": plan.dims[1]}
            core = shapely.union_all(plan.boxes).intersection(outline)
            _pa, rest = _flat_zone(outline, core, _passage_for(shape, pq, outline, core, plan.base, plan))
            share = rest.area / max(outline.area, 1e-6)
            plans[o.type] = plan
        row = _alt_row(o, plan, why)
        row["flats_share"] = None if share is None else round(share, 3)
        rows.append(row)
    best = max((r["flats_share"] for r in rows if r["placed"]), default=0.0)
    for r in rows:  # a placement that leaves the plate mostly passage is not a usable core for this template
        if r["placed"] and (r["flats_share"] < MIN_FLATS_SHARE or r["flats_share"] < REL_FLATS_SHARE * best):
            r["placed"] = False
            r["why_not"] = (f"leaves only {r['flats_share']:.0%} of the plate as flats here "
                            f"(best placement {best:.0%}): the passage round it swallows the wings")
            plans.pop(r["type"], None)
    usable = [r for r in rows if r["placed"] and r["compliant"]]
    order = [r for r in usable if r["score"] > 0] or sorted(usable, key=lambda r: r["core_share"])
    auto = order[0]["type"] if order else None
    requested = core_name(req_code)
    chosen = auto
    note = ""
    if requested:
        if requested in plans and next(r for r in rows if r["type"] == requested)["compliant"]:
            chosen = requested
        else:
            note = (f"{cores.TYPE_LABELS[requested]} cannot be placed in this {ptype} plate "
                    f"({next(r for r in rows if r['type'] == requested)['why_not'] or 'not code compliant'}); "
                    f"using {cores.TYPE_LABELS[auto] if auto else 'the template core'} instead")
    for r in rows:
        r["recommended"] = r["type"] == auto
        r["chosen"] = r["type"] == chosen
    info = {"plate_type": ptype, "flats_per_floor": flats, "height_m": round(height, 1), "requested": requested or "auto",
            "auto": auto, "selected": chosen, "note": note}
    return (plans[chosen] if chosen else None), rows, info


def _model_kind(kind: str) -> str:
    """CorePart.kind as the frozen data contract (models.py) allows it. The library's smoke-check lobby is a lobby;
    shafts and refuge areas travel as 'service' until models.CorePart.kind lists them (the exact kinds are in
    ``core_info``)."""
    allowed = set(get_args(CorePart.model_fields["kind"].annotation))
    if kind in allowed:
        return kind
    return {"smoke_lobby": "lobby", "shaft": "service", "refuge": "service"}.get(kind, "service")


def _part_width(kind: str, meta: dict) -> float:
    if kind == "stair":
        return round(float(meta.get("flight_w", meta.get("width", 0.0))), 2)
    if kind in ("lift", "fire_lift"):
        return round(float(meta.get("clear_w", meta.get("width", 0.0))), 2)
    return 0.0


def _plan_core_parts(plan: CorePlan) -> list[CorePart]:
    out = []
    for kind, _name, poly, meta in plan.parts:
        if poly.is_empty or poly.geom_type != "Polygon":
            continue
        out.append(CorePart(kind=_model_kind(kind), polygon=[(round(x, 3), round(y, 3)) for x, y in geo.ring_of(poly)],
                            width=_part_width(kind, meta)))
    return out


def core_info(plate: Plate, height: float | None = None) -> dict:
    """The core of a built plate for the UI: chosen type, the ranked alternatives with efficiency and reasons, the typed
    parts (``stair, lift, fire_lift, lobby, smoke_lobby, shaft, refuge``) and the requirement counts."""
    shape = ShapeKind(plate.shape)
    if shape == ShapeKind.triangle or plate.imported_from:
        return {"mode": "template", "selected": None, "auto": None, "requested": "auto", "alternatives": [], "parts": [],
                "note": "the triangle plate keeps its own core (experimental)" if shape == ShapeKind.triangle
                else "outline imported from a drawing"}
    if "core_type_used" not in plate.params:
        return {"mode": "template", "selected": None, "auto": None, "requested": "auto", "alternatives": [], "parts": [],
                "note": "this plate was built before the core library: re-fit the template to choose a core"}
    h = float(height if height is not None else plate.params.get("height_m", 24.0))
    plan, rows, info = plan_core(shape, plate.params, h)
    if plan is None:
        return {"mode": "template", **info, "alternatives": rows, "parts": [],
                "note": "no library core fits this template: the template's own core is used"}
    req = plan.core.req
    parts = [{"kind": k, "name": n, "area_m2": round(g.area, 2), "polygon": [(round(x, 3), round(y, 3)) for x, y in geo.ring_of(g)]}
             for k, n, g, _m in plan.parts if g.geom_type == "Polygon" and not g.is_empty]
    chosen = next(r for r in rows if r["chosen"])
    reasons = list(chosen["reasons"])
    if plan.where:
        reasons.append(f"placed {plan.where}")
    counts: dict[str, int] = {}
    for q in parts:
        counts[q["kind"]] = counts.get(q["kind"], 0) + 1
    return {"mode": "library", **info, "label": cores.TYPE_LABELS[plan.type], "where": plan.where, "rotation_deg": plan.rot,
            "alternatives": rows, "reasons": reasons, "parts": parts, "counts": counts,
            "boxes": [[round(v, 2) for v in b.bounds] for b in plan.boxes],
            "dims_m": [round(plan.dims[0], 1), round(plan.dims[1], 1)], "notes": list(plan.core.notes),
            "requirements": {"stairs": req.n_stairs, "lifts": req.n_lifts, "fire_lifts": req.n_fire_lifts,
                             "smoke_lobby": req.smoke_lobby, "clauses": list(req.clauses)}}


def _passage_for(shape: ShapeKind, p: dict[str, float], outline: Polygon, core, cc, plan: CorePlan | None):
    """Passage: a ring round the core, the template's corridors and (library cores) a link to every core box."""
    pw = p["passage"]
    lines = _corridors(shape, p, cc)
    if plan is not None:
        lines += _core_links(plan, lines, pw)
    ring = core.buffer(pw, join_style="mitre", mitre_limit=2.0)
    corr = [ln.buffer(pw / 2, cap_style="flat", join_style="mitre") for ln in lines]
    passage = shapely.union_all([ring, *corr]).intersection(outline).difference(core)
    return shapely.set_precision(passage, 0.001)


def _flat_zone(outline: Polygon, core, passage):
    """(passage, flat zone): flat-zone slivers too thin for a room (< 5 m) become part of the lobby (daylit common space)."""
    rest = outline.difference(core).difference(passage)
    thick = rest.buffer(-2.5, join_style="mitre").buffer(2.5, join_style="mitre").intersection(rest)
    sliver = rest.difference(thick)
    sliver = shapely.union_all([g for g in geo.polygons_of(sliver) if g.area > 1.0 and g.distance(passage) < 0.01]) \
        if not sliver.is_empty else Polygon()
    if not sliver.is_empty:
        passage = shapely.set_precision(shapely.union(passage, sliver), 0.001)
        rest = outline.difference(core).difference(passage)
    return passage, rest


def _core_links(plan: CorePlan, lines: list[LineString], pw: float) -> list[LineString]:
    """Passage from every core box to the nearest template corridor when it does not touch one (a façade core slid to
    a wall, an end core): the ring passage around the box is then connected to the corridors."""
    out = []
    for b in plan.boxes:
        if lines and min(ln.distance(b) for ln in lines) <= pw / 2 + 0.01:
            continue
        if not lines:
            continue
        c = b.centroid
        ln = min(lines, key=lambda g: g.distance(c))
        q = ln.interpolate(ln.project(c))
        out.append(LineString([(c.x, c.y), (q.x, q.y)]))
    return out


def _core_parts(core: Polygon, height: float, p: dict[str, float], flip: bool = False) -> list[CorePart]:
    """Stairs and lifts against the core's top edge, lift lobby below them. ``flip`` mirrors that top to
    bottom (a core on the base façade of a triangle: stairs and lifts against the façade, lift lobby
    opening onto the floor lobby above)."""
    if flip:
        from shapely import affinity

        cy = (core.bounds[1] + core.bounds[3]) / 2
        return [part.model_copy(update={"polygon": geo.ring_of(affinity.scale(Polygon(part.polygon), 1.0, -1.0,
                                                                                origin=(0.0, cy)))})
                for part in _core_parts(core, height, p)]
    c = rules.design()["core"]
    minx, miny, maxx, maxy = core.bounds
    cw, cd = maxx - minx, maxy - miny
    flight_default = float(c["stair_flight_width"])
    lift_w = float(c["lift_size"][0]) + 0.2
    lift_d = float(c["lift_size"][1]) + 0.2
    stair_w = 2 * flight_default + 0.2
    parts: list[CorePart] = []
    n_stairs = 2 if cw >= 2 * stair_w + lift_w else 1
    if cw < stair_w * n_stairs:
        stair_w = cw / (n_stairs + 0.6)
    flight = round((stair_w - 0.2) / 2, 2)
    sd = min(cd, float(c["stair_length"]))
    parts.append(CorePart(kind="stair", polygon=geo.ring_of(box(minx, maxy - sd, minx + stair_w, maxy)), width=flight))
    x_mid0 = minx + stair_w
    x_mid1 = maxx
    if n_stairs == 2:
        parts.append(CorePart(kind="stair", polygon=geo.ring_of(box(maxx - stair_w, maxy - sd, maxx, maxy)), width=flight))
        x_mid1 = maxx - stair_w
    mid_w = x_mid1 - x_mid0
    want = 2 if height <= 45 else 3 if height <= 70 else 4
    want += 1 if height > 15 else 0
    n_lifts = max(0, min(want, int(mid_w // lift_w)))
    ld = min(lift_d, cd - 1.5)
    x = x_mid0
    for i in range(n_lifts):
        kind = "fire_lift" if (height > 15 and i == n_lifts - 1) else "lift"
        parts.append(CorePart(kind=kind, polygon=geo.ring_of(box(x, maxy - ld, x + lift_w, maxy)), width=lift_w - 0.2))
        x += lift_w
    if x_mid1 - x > 0.6:
        parts.append(CorePart(kind="service", polygon=geo.ring_of(box(x, maxy - ld, x_mid1, maxy)), width=x_mid1 - x))
    if cd - ld > 0.5 and mid_w > 0.5:
        parts.append(CorePart(kind="lobby", polygon=geo.ring_of(box(x_mid0, miny, x_mid1, maxy - ld)), width=cd - ld))
    return parts


# ---- generator v3 (docs/quality/generator-v3.md): features placed on the cuts between flats
CHOWK_RATIO = 10.0  # m: flat gross area / façade above which the flat is deeper than its façade can light
CHOWK_MIN_CUT = 10.5  # m: only party walls at least this deep get a chowk
CHOWK_W = 3.0  # m: exterior chowk (re-entrant notch open to the sky) width, half in each flat
CHOWK_KEEP = 2.4  # m: party wall kept behind the chowk, towards the passage (entries, wet rooms)
AVS_W, AVS_D = 1.6, 2.4  # m: party-wall air-ventilation shaft (DCPR AVS >= 3.0 m2, shorter side >= 1.5 m): 3.84 m2
AVS_FRONT = 4.6  # m behind the façade: a habitable room's depth (+ wall) stays in front of the shaft
AVS_MIN_CUT = 6.0  # m: party walls shorter than this keep no shaft (a flat that shallow reaches its façade)
AVS_RATIO = 0.0  # m: flats with this little area per metre of façade keep their wet rooms on the façade
AVS_DOOR_CLEAR = 3.5  # m: a shaft this close to a flat's entry door would cut the living room off from it


def base_outline(plate: Plate) -> Polygon:
    """The plate outline before v3 chowks were cut into it (pure function of the shape parameters)."""
    shape = ShapeKind(plate.shape)
    if shape == ShapeKind.triangle:
        return Polygon(plate.outline).buffer(0)
    return shapely.set_precision(_outline(shape, plate.params).buffer(0), 0.001)


def _cut_segments(a: Polygon, b: Polygon) -> list[tuple[tuple[float, float], tuple[float, float]]]:
    inter = a.boundary.intersection(b.boundary)
    out = []
    for g in getattr(inter, "geoms", [inter]):
        if g.geom_type == "LineString" and g.length >= 3.0:
            cs = list(g.coords)
            out.append((cs[0], cs[-1]))
        elif g.geom_type == "MultiLineString":
            from shapely.ops import linemerge

            m = linemerge(g)
            for ln in getattr(m, "geoms", [m]):
                if ln.length >= 3.0:
                    cs = list(ln.coords)
                    out.append((cs[0], cs[-1]))
    if len(out) > 1:
        from shapely.ops import linemerge

        m = linemerge([LineString(sg) for sg in out])
        out = [(list(ln.coords)[0], list(ln.coords)[-1]) for ln in getattr(m, "geoms", [m]) if ln.length >= 3.0]
    return out


def _cut_features(rest, passage, core, outline: Polygon, reflex: list, p: dict[str, float]) -> tuple[list, list]:
    """v3: chowks and party-wall shafts on the cuts between flats (units.split_pieces gives the cuts).

    * A cut whose flats are deeper than their façade can light (gross area / façade > CHOWK_RATIO) and that runs at
      least CHOWK_MIN_CUT from the façade gets an exterior chowk: a CHOWK_W slot open to the sky from the façade to
      CHOWK_KEEP short of the passage, half in each flat. The flats keep their party wall at the back and gain the
      chowk's two sides as façade for bedrooms, kitchen and toilets. Flats are served greedily, deepest first, until
      none is too deep.
    * Every other cut of at least AVS_MIN_CUT gets an air-ventilation shaft (AVS) straddling the party wall behind
      the façade rooms, so the wet rooms of both flats cluster on it instead of a mechanical duct (DCPR 39(2)).
    Returns (shafts, chowks) clipped to the flat zone."""
    from shapely.geometry import Point

    from .units import _door, split_pieces

    if int(p.get("chowks", 0)) <= 0 and int(p.get("party_shafts", 0)) <= 0:
        return [], []
    ring = outline.exterior
    ref = shapely.union_all(reflex) if reflex else Polygon()
    outer = ring.difference(ref.buffer(0.05)) if not ref.is_empty else ring
    pieces, _frag = split_pieces(rest, passage, core, int(p["units"]), outer)
    info = [{"area": g.area, "front": max(0.5, g.boundary.intersection(outer).length)} for g in pieces]
    access = shapely.union(passage, core)
    doors = []
    for g in pieces:
        dr = _door(g, access)
        doors.append(dr[0] if dr else None)
    cuts = []
    for i in range(len(pieces)):
        for j in range(i + 1, len(pieces)):
            if pieces[i].distance(pieces[j]) > 1e-6:
                continue
            for a, b in _cut_segments(pieces[i], pieces[j]):
                fa, fb = ring.distance(Point(a)) < 0.05, ring.distance(Point(b)) < 0.05
                if fa == fb:
                    continue
                f, q = (a, b) if fa else (b, a)
                cuts.append((i, j, f, q))

    def ratio(k: int) -> float:
        return info[k]["area"] / info[k]["front"]

    cuts.sort(key=lambda c: -max(ratio(c[0]), ratio(c[1])))
    shafts, chowks = [], []
    for i, j, f, q in cuts:
        L = math.dist(f, q)
        dx, dy = (q[0] - f[0]) / L, (q[1] - f[1]) / L  # inward along the cut
        nx, ny = -dy, dx

        def slot(d0: float, d1: float, w: float, f=f, dx=dx, dy=dy, nx=nx, ny=ny) -> Polygon:
            h = w / 2
            pts = [(f[0] + dx * d0 + nx * h, f[1] + dy * d0 + ny * h), (f[0] + dx * d1 + nx * h, f[1] + dy * d1 + ny * h),
                   (f[0] + dx * d1 - nx * h, f[1] + dy * d1 - ny * h), (f[0] + dx * d0 - nx * h, f[1] + dy * d0 - ny * h)]
            return Polygon(pts).buffer(0).intersection(rest)

        if int(p.get("chowks", 0)) > 0 and L >= CHOWK_MIN_CUT and max(ratio(i), ratio(j)) > CHOWK_RATIO:
            g = slot(-1.0, L - CHOWK_KEEP, CHOWK_W)
            if g.area > 1.0:
                chowks.append(g)
                for k in (i, j):
                    info[k]["area"] -= g.area / 2
                    info[k]["front"] += L - CHOWK_KEEP - CHOWK_W / 2
                continue
        if int(p.get("party_shafts", 0)) > 0 and L >= AVS_MIN_CUT and max(ratio(i), ratio(j)) >= AVS_RATIO:
            s0 = min(AVS_FRONT, L - AVS_D)
            if L - (s0 + AVS_D) < 1.5:
                s0 = L - AVS_D  # flush with the passage wall: no sliver behind the shaft
            g = slot(s0, s0 + AVS_D, AVS_W)
            # the shaft keeps clear of both flats' entry doors (a living room must still reach the door)
            if g.area > 0.5 and all(d is None or g.distance(Point(d)) >= AVS_DOOR_CLEAR for d in (doors[i], doors[j])):
                shafts.append(g)
    return shafts, chowks


def _reflex_corners(outline: Polygon) -> list[tuple[float, float, float, float]]:
    """Reflex vertices with the bisector direction pointing into the solid."""
    ring = geo.ring_of(outline)
    angs = geo.interior_angles(ring)
    out = []
    n = len(ring)
    for i, a in enumerate(angs):
        if a > 180.0 + 1e-6:
            p0, p1, p2 = ring[i - 1], ring[i], ring[(i + 1) % n]
            v1 = (p0[0] - p1[0], p0[1] - p1[1])
            v2 = (p2[0] - p1[0], p2[1] - p1[1])
            l1, l2 = math.hypot(*v1), math.hypot(*v2)
            bx, by = -(v1[0] / l1 + v2[0] / l2), -(v1[1] / l1 + v2[1] / l2)
            out.append((p1[0], p1[1], bx, by))
    return out


def build_plate(
    shape: ShapeKind | str,
    params: dict[str, float],
    *,
    tower_id: str = "T1",
    angle_deg: float = 0.0,
    origin: tuple[float, float] = (0.0, 0.0),
    env: tuple[float, float] | None = None,
    height: float = 24.0,
) -> Plate:
    shape = ShapeKind(shape)
    env_w, env_d = env or (params["W"], params["D"])
    p = clamp_params(shape, {**default_params(shape, env_w, env_d, height), **params}, env_w, env_d)
    outline = _outline(shape, p).buffer(0)
    outline = shapely.set_precision(outline, 0.001)
    cc = _core_center(shape, p)
    pw = p["passage"]
    plan: CorePlan | None = None
    p["height_m"] = round(float(height), 2)
    p["core_type_used"] = 0.0
    if shape == ShapeKind.triangle:
        core, passage = _triangle_zones(outline, p)
    else:
        plan, _rows, _info = plan_core(shape, p, height)
        if plan is not None:
            # library core: the zone is the box of each cluster; the template's corridors reach it
            p["core_type_used"] = float(core_code(plan.type))
            p["core_w"], p["core_d"] = round(plan.dims[0], 1), round(plan.dims[1], 1)
            cc = plan.base
            core = shapely.union_all(plan.boxes).intersection(outline)
        else:
            core = box(cc[0] - p["core_w"] / 2, cc[1] - p["core_d"] / 2, cc[0] + p["core_w"] / 2, cc[1] + p["core_d"] / 2)
            core = core.intersection(outline)
        passage = _passage_for(shape, p, outline, core, cc, plan)
    passage, rest = _flat_zone(outline, core, passage)
    shafts = []
    n_sh = int(p["shafts"])
    if shape == ShapeKind.triangle:
        if n_sh > 0:
            shafts = _triangle_shafts(rest, passage, p)
        shafts += _triangle_corner_shafts(rest, p)
    else:
        if n_sh > 0:
            s = p["shaft_size"]
            for x, y, bx, by in _reflex_corners(outline)[:n_sh]:
                sx = s if bx > 0 else -s
                sy = s if by > 0 else -s
                sq = box(min(x, x + sx), min(y, y + sy), max(x, x + sx), max(y, y + sy)).intersection(rest)
                if sq.area > 0.5:
                    shafts.append(sq)
        # v3: chowks for flats deeper than their façade, AVS on the other party walls
        avs, chowks = _cut_features(rest, passage, core, outline, list(shafts), p) if not rest.is_empty else ([], [])
        shafts += avs
        if chowks:
            cut = shapely.union_all(chowks)
            outline = shapely.set_precision(outline.difference(cut), 0.001)
            rest = rest.difference(cut)
    shaft_geom = shapely.union_all(shafts) if shafts else Polygon()
    flats = rest.difference(shaft_geom) if shafts else rest
    flats = shapely.set_precision(flats, 0.001)
    plate_area = outline.area
    core_a, pass_a, shaft_a = core.area, passage.area, shaft_geom.area
    flats_a = plate_area - core_a - pass_a - shaft_a  # exact by construction
    zones = [
        Zone(kind="flats", polygon=geo.to_polyrings(flats, 0.01), area=round(flats_a, 2)),
        Zone(kind="core", polygon=geo.to_polyrings(core), area=round(core_a, 2)),
        Zone(kind="passage", polygon=geo.to_polyrings(passage, 0.01), area=round(pass_a, 2)),
    ]
    if shafts:
        zones.append(Zone(kind="shaft", polygon=geo.to_polyrings(shaft_geom, 0.01), area=round(shaft_a, 2)))
    # make the rounded numbers add up exactly
    zones[0].area = round(round(plate_area, 2) - sum(z.area for z in zones[1:]), 2)
    ring_pts = geo.ring_of(outline)
    return Plate(
        tower_id=tower_id,
        shape=shape,
        params=p,
        angle_deg=angle_deg,
        origin=origin,
        outline=[(round(x, 3), round(y, 3)) for x, y in ring_pts],
        corners=len(ring_pts),
        width=round(outline.bounds[2] - outline.bounds[0], 2),
        depth=round(outline.bounds[3] - outline.bounds[1], 2),
        area=round(plate_area, 2),
        zones=zones,
        core_parts=_plan_core_parts(plan) if plan is not None
        else _core_parts(core, height, p, flip=shape == ShapeKind.triangle and bool(p.get("core_at_base"))),
        passage_width=pw,
        ratings=RATINGS[shape],
    )


def fit(shape: ShapeKind | str, fp: PlateFootprint, height: float = 24.0, overrides: dict | None = None) -> Plate:
    shape = ShapeKind(shape)
    params = default_params(shape, fp.width, fp.depth, height)
    if overrides:
        params.update(overrides)
    return build_plate(
        shape, params, tower_id=fp.id, angle_deg=fp.angle_deg, origin=fp.center, env=(fp.width, fp.depth), height=height
    )


def all_shapes(fp: PlateFootprint, height: float = 24.0) -> list[dict]:
    out = []
    for s in ShapeKind:
        pl = fit(s, fp, height)
        z = {zz.kind: zz.area for zz in pl.zones}
        out.append(
            {
                "shape": s.value,
                "label": LABELS[s],
                "ratings": RATINGS[s],
                "area": pl.area,
                "corners": pl.corners,
                "efficiency": round(z.get("flats", 0) / pl.area, 3) if pl.area else 0,
                "outline": pl.outline,
            }
        )
    return out


def zones_geom(plate: Plate, kind: str):
    geoms = [geo.from_polyrings(z.polygon) for z in plate.zones if z.kind == kind and z.polygon]
    return shapely.union_all(geoms) if geoms else Polygon()
