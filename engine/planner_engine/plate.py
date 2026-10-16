"""Stage 5 - shape the floor.

Six parametric templates (central core, cruciform, H, T, triangle, side core). Each is a function of
named parameters with limits. ``fit`` scales the defaults to the tower envelope from stage 4; every edit
in the UI sends new parameters and gets the zones back. Zones are computed by geometric difference, so
flats + core + passage + shafts == plate, always.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import shapely
from shapely.geometry import LineString, Polygon, box

from . import geo, rules
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
        p["unit_depth"] = 9.0
        # compact core: two stairs either side of two lifts, lobby in front
        p["core_w"] = 11.8
        p["core_d"] = 8.0
    p["core_w"] = max(4.0, round(p["core_w"], 1))
    p["core_d"] = max(4.0, round(p["core_d"], 1))
    flats_guess = W * D * (0.75 if shape != ShapeKind.triangle else 0.33)
    n = max(2, int(round(flats_guess / 150.0)))
    p["units"] = float(n + (n % 2 if shape in (ShapeKind.central_core, ShapeKind.side_core) else 0))
    if shape == ShapeKind.triangle:
        # flats along every façade with ~11 m of frontage each
        sides = [W, math.hypot(W / 2, D), math.hypot(W / 2, D)]
        p["units"] = float(sum(max(1, round((L - 2 * p["corner_cut"]) / 11.0)) for L in sides))
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


def _triangle_zones(outline: Polygon, p: dict[str, float]):
    """Triangle plate: a band of flats along every façade, core + lobby in the middle.

    The core keeps its full size (two stairs, lifts): it sits on the inner triangle's base, where the
    triangle is widest, with a passage strip in front. The band depth shrinks (to 6.5 m) until it fits.
    The lobby is the inner triangle minus the core, so every flat meets it along a straight edge."""
    ic = tri_incenter(p)
    cw, cd, pw = p["core_w"], p["core_d"], p["passage"]
    depth = p.get("unit_depth", 9.0)
    core = inner = None
    while depth >= 6.5 and core is None:
        inner = outline.buffer(-depth, join_style="mitre", mitre_limit=5.0)
        if not inner.is_empty:
            y0 = inner.bounds[1] + pw
            while y0 + cd <= inner.bounds[3]:
                c = box(ic[0] - cw / 2, y0, ic[0] + cw / 2, y0 + cd)
                if inner.contains(c):
                    core = c
                    break
                y0 += 0.25
        if core is None:
            depth -= 0.25
    if core is None:  # plate too small for a full core: keep the depth, shrink the core (flagged by NBC checks)
        depth = 6.5
        inner = outline.buffer(-depth, join_style="mitre", mitre_limit=5.0)
        for k in (0.9, 0.8, 0.7, 0.6):
            c = box(ic[0] - cw * k / 2, ic[1] - cd * k / 2, ic[0] + cw * k / 2, ic[1] + cd * k / 2)
            if inner.contains(c):
                core = c
                break
        if core is None:
            core = box(ic[0] - 3.0, ic[1] - 2.0, ic[0] + 3.0, ic[1] + 2.0).intersection(outline)
    p["unit_depth"] = round(depth, 2)
    passage = inner.difference(core) if inner is not None and not inner.is_empty else core.buffer(1.5).difference(core)
    return core, shapely.set_precision(passage.intersection(outline), 0.001)


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


def _core_parts(core: Polygon, height: float, p: dict[str, float]) -> list[CorePart]:
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
    if shape == ShapeKind.triangle:
        core, passage = _triangle_zones(outline, p)
    else:
        core = box(cc[0] - p["core_w"] / 2, cc[1] - p["core_d"] / 2, cc[0] + p["core_w"] / 2, cc[1] + p["core_d"] / 2)
        core = core.intersection(outline)
        ring = core.buffer(pw, join_style="mitre", mitre_limit=2.0)
        corr = [ln.buffer(pw / 2, cap_style="flat", join_style="mitre") for ln in _corridors(shape, p, cc)]
        passage = shapely.union_all([ring, *corr]).intersection(outline).difference(core)
        passage = shapely.set_precision(passage, 0.001)
    rest = outline.difference(core).difference(passage)
    # flat-zone slivers too thin for a room (< 5 m) become part of the lobby (daylit common space)
    thick = rest.buffer(-2.5, join_style="mitre").buffer(2.5, join_style="mitre").intersection(rest)
    sliver = rest.difference(thick)
    sliver = shapely.union_all([g for g in geo.polygons_of(sliver) if g.area > 1.0 and g.distance(passage) < 0.01]) \
        if not sliver.is_empty else Polygon()
    if not sliver.is_empty:
        passage = shapely.set_precision(shapely.union(passage, sliver), 0.001)
        rest = outline.difference(core).difference(passage)
    shafts = []
    n_sh = int(p["shafts"])
    if n_sh > 0:
        s = p["shaft_size"]
        for x, y, bx, by in _reflex_corners(outline)[:n_sh]:
            sx = s if bx > 0 else -s
            sy = s if by > 0 else -s
            sq = box(min(x, x + sx), min(y, y + sy), max(x, x + sx), max(y, y + sy)).intersection(rest)
            if sq.area > 0.5:
                shafts.append(sq)
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
        core_parts=_core_parts(core, height, p),
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
