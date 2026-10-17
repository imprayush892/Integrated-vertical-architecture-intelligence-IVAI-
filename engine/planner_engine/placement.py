"""Stage 4 - place the towers (v2: reasons like an architect and explains itself).

Pipeline (deterministic, every number traceable to ``site.yaml`` / ``design.yaml``):

1. **Context.** Floors from height, permissible built-up area (FSI x fungible, ``site.yaml: fsi``), the plate area that
   uses it (capped by the coverage limits), setbacks, tower gap, fire-access faces by height, ramp and RG needs, wind
   rose, hot-sun azimuth and the view bearing (given, or the Arabian Sea to the west when the site says sea view).
2. **Frames.** A handful of plate angles an architect would defend: parallel to the long plot edges, true N-S, and the
   sun/wind compromise angle. No arbitrary 37 degree rotations.
3. **Concepts.** For every frame, tower count (1-3), kind (slab / point tower) and orientation, the largest plate of each
   standard depth that fits n identical towers in a row is found with summed-area-table fit maps; the towers are then
   placed by arrangement modes (pushed to either side or centred, packed / comfortable / spread, staggered).
4. **Scoring in three passes.** Orientation terms per concept (FSI use, plate efficiency, west glass, breeze, view);
   then per layout ray-cast daylight / privacy / wind shadow / view blocking and a raster garden; then a full
   geometric check of the finalists: 6 m fire-tender drive reaching the faces the height requires (with turning), a
   vehicle gate on the road clear of junctions, a parking ramp near the gate, usable garden and slivers.
5. **Options.** Best concept per tower count (the client's first decision), replaced by a structurally different
   concept when a count is infeasible or clearly worse; each option carries a ranked list of reasons with numbers and
   comparisons, the geometry of gate / ramp / drive / garden, and up to two runner-up concepts.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import shapely
from scipy import ndimage
from shapely import affinity
from shapely.geometry import LineString, Polygon, box
from shapely.geometry import Point as SPoint
from shapely.ops import nearest_points

from . import geo, rules
from .models import Placement, PlacementOption, PlateFootprint, Site
from .site import buildable_geom

FAMILY_LABEL = {1: "single tower", 2: "twin towers", 3: "three towers"}
OPTION_IDS = ("A", "B", "C")
HOT_RANGE = (225.0, 315.0)  # kept for callers of v1; v2 weights facades by cos(bearing - hot azimuth)
TERM_KEYS = ("fsi", "depth", "west_glass", "breeze", "view", "privacy", "daylight", "garden", "leftovers", "approach",
             "phasing", "alignment")
TERM_LABEL = {
    "fsi": "FSI use", "depth": "Plate efficiency", "west_glass": "West sun", "breeze": "Breeze", "view": "View",
    "privacy": "Privacy", "daylight": "Daylight", "garden": "Garden", "leftovers": "No slivers", "approach": "Approach",
    "phasing": "Phasing", "alignment": "Alignment",
}


def _lookup(table: list[list[float]], key: float) -> float:
    for limit, value in table:
        if key <= limit:
            return float(value)
    return float(table[-1][1])


def _sat(mask: np.ndarray) -> np.ndarray:
    s = np.zeros((mask.shape[0] + 1, mask.shape[1] + 1), dtype=np.int32)
    s[1:, 1:] = np.cumsum(np.cumsum(mask, axis=0, dtype=np.int32), axis=1)
    return s


def _fit(S: np.ndarray, hc: int, wc: int) -> np.ndarray:
    """F[r, c] = the hc x wc window with top-left cell (r, c) is all True."""
    R, C = S.shape[0] - 1, S.shape[1] - 1
    if hc > R or wc > C or hc <= 0 or wc <= 0:
        return np.zeros((0, 0), dtype=bool)
    win = S[hc:, wc:] - S[:-hc, wc:] - S[hc:, :-wc] + S[:-hc, :-wc]
    return win == hc * wc


def _opening(free: np.ndarray, k: int) -> np.ndarray:
    """Cells of ``free`` covered by some all-free k x k window (morphological opening by a k-cell square)."""
    R, C = free.shape
    if k <= 1:
        return free.copy()
    W = _fit(_sat(free), k, k)
    if W.size == 0:
        return np.zeros_like(free)
    pad = np.zeros((R + k - 1, C + k - 1), dtype=bool)
    pad[k - 1 : k - 1 + W.shape[0], k - 1 : k - 1 + W.shape[1]] = W
    S = _sat(pad)
    return (S[k:, k:] - S[:-k, k:] - S[k:, :-k] + S[:-k, :-k]) > 0


def _faces(ring: list[tuple[float, float]]) -> list[tuple[np.ndarray, np.ndarray, float, float, np.ndarray]]:
    """(a, b, length, outward-normal bearing, outward unit normal) for each edge of a CCW ring."""
    out = []
    n = len(ring)
    for i in range(n):
        a = np.array(ring[i], dtype=float)
        b = np.array(ring[(i + 1) % n], dtype=float)
        v = b - a
        L = float(math.hypot(*v))
        if L < 1e-9:
            continue
        nrm = np.array([v[1], -v[0]]) / L
        out.append((a, b, L, geo.bearing_of_normal(tuple(a), tuple(b)), nrm))
    return out


def _ray_hits(orig: np.ndarray, dirs: np.ndarray, segs: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Distance along each ray (k) to the first of m segments [ax, ay, bx, by]; (inf, -1) when none is hit."""
    if segs.size == 0 or orig.size == 0:
        return np.full(len(orig), np.inf), np.full(len(orig), -1)
    p = orig[:, None, :]
    r = dirs[:, None, :]
    a = segs[None, :, 0:2]
    s = segs[None, :, 2:4] - a
    rxs = r[..., 0] * s[..., 1] - r[..., 1] * s[..., 0]
    q = a - p
    with np.errstate(divide="ignore", invalid="ignore"):
        t = (q[..., 0] * s[..., 1] - q[..., 1] * s[..., 0]) / rxs
        u = (q[..., 0] * r[..., 1] - q[..., 1] * r[..., 0]) / rxs
    ok = (np.abs(rxs) > 1e-12) & (t > 1e-6) & (u >= -1e-9) & (u <= 1 + 1e-9)
    t = np.where(ok, t, np.inf)
    idx = np.argmin(t, axis=1)
    best = t[np.arange(len(orig)), idx]
    return best, np.where(np.isfinite(best), idx, -1)


def _fix(g):
    return shapely.make_valid(g).buffer(0) if g is not None and not g.is_empty else g


def _safe(op, *args):
    """GEOS overlay with fall-backs: as is; on valid inputs; on a 1 cm precision grid."""
    try:
        return op(*args)
    except shapely.errors.GEOSException:
        pass
    fixed = [_fix(x) if not isinstance(x, (list, tuple)) else [_fix(y) for y in x] for x in args]
    try:
        return op(*fixed)
    except shapely.errors.GEOSException:
        return op(*fixed, grid_size=0.01)


def _diff(a, b):
    return _safe(shapely.difference, a, b)


def _inter(a, b):
    return _safe(shapely.intersection, a, b)


def _union(parts):
    return _safe(shapely.union_all, list(parts))


def _clean(g) -> Any:
    """Polygonal part of a geometry, made valid (intersections can return slivers, lines or invalid rings)."""
    if g is None or g.is_empty:
        return Polygon()
    if not g.is_valid:
        g = shapely.make_valid(g)
    parts = [p.buffer(0) for p in geo.polygons_of(g) if p.area > 1e-3]
    return shapely.union_all(parts) if parts else Polygon()


def _lpath(a, b, theta: float, width: float, open_g) -> Any:
    """Drive from a to b as an L aligned with the plate frame (drives run orthogonally, not diagonally); of the two
    L routes the one lying most in the open ground wins. Returned clipped to the open ground."""
    pa, pb = geo.rotate_pts([(a.x, a.y), (b.x, b.y)], -theta)
    best, best_in = Polygon(), -1.0
    for corner in ((pa[0], pb[1]), (pb[0], pa[1])):
        pts = geo.rotate_pts([pa, corner, pb], theta)
        line = LineString(pts)
        if line.length < 0.5:
            continue
        band = line.buffer(width / 2.0, cap_style="square", join_style="mitre")
        ins = _inter(band, open_g).area / max(1e-9, band.area)
        if ins > best_in:
            best, best_in = band, ins
    return _clean(_inter(best, open_g)) if not best.is_empty else best


def _drive_strip(a: np.ndarray, b: np.ndarray, W: float, off: float, open_g) -> Any:
    """The 6 m fire drive along face a-b: a W-wide strip parallel to the face at the smallest offset (0 .. off) that
    lies in the open ground, run on past both corners so drives round a tower join up."""
    v = b - a
    L = float(np.hypot(*v))
    u = v / L
    nrm = np.array([u[1], -u[0]])
    ext = W
    for d0 in np.arange(0.0, off + 1e-9, 0.5):
        p0 = a - u * ext + nrm * d0
        p1 = b + u * ext + nrm * d0
        strip = Polygon([tuple(p0), tuple(p1), tuple(p1 + nrm * W), tuple(p0 + nrm * W)])
        core = Polygon([tuple(a + nrm * d0), tuple(b + nrm * d0), tuple(b + nrm * (d0 + W)), tuple(a + nrm * (d0 + W))])
        if _inter(core, open_g).area >= 0.9 * core.area:
            return _clean(_inter(strip, open_g))
    return Polygon()


def _bvec(bearing: float) -> np.ndarray:
    """Unit vector pointing toward a compass bearing (x east, y north)."""
    b = math.radians(bearing)
    return np.array([math.sin(b), math.cos(b)])


# ------------------------------------------------------------------------------------------------------- context


@dataclass
class Ctx:
    site: Site
    plot: Polygon
    buildable: Any
    H: float
    floors: int
    gap_min: float
    gap_clause: str
    cov_max: float
    cov_clause: str
    side: float
    front: float
    fsi: float
    fung: float
    fsi_clause: str
    bua_perm: float
    countable: float
    plate_total_target: float
    plate_total_fsi: float
    P: dict
    lim: dict
    fire: dict
    entry: dict
    ramp: dict
    rg_frac: float
    rg_clause: str
    fire_faces: int
    ramp_needed: bool
    ramp_w: float
    ramp_l: float
    core_area: float
    wind: list[tuple[float, float, str]]
    wind_from: float | None
    wind_label: str | None
    hot_az: float
    view: float | None
    view_src: str
    weights: dict[str, float]
    segs: np.ndarray
    seg_road: np.ndarray
    road_lines: list[LineString]
    road_normals: list[float]
    road_union: Any
    junction_pts: list[tuple[float, float]]
    edge_angles: list[float] = field(default_factory=list)
    core_cap: float = 0.20
    breeze_best: dict = field(default_factory=dict)

    def area_per_tower(self, n: int) -> float:
        return max(float(self.lim["min_area"]), min(float(self.lim["max_area_per_tower"]), self.plate_total_target / n))


def _edge_angles(ring) -> list[float]:
    """Directions (mod 90) of the plot edges that carry at least a quarter of the longest edge direction's length."""
    bins: dict[float, float] = {}
    for i in range(len(ring)):
        a, b = ring[i], ring[(i + 1) % len(ring)]
        L = math.dist(a, b)
        if L < 8.0:
            continue
        ang = math.degrees(math.atan2(b[1] - a[1], b[0] - a[0])) % 90.0
        for k in list(bins):
            if geo.angle_diff(k * 4, ang * 4) / 4 < 2.0:
                bins[k] += L
                break
        else:
            bins[ang] = L
    if not bins:
        return [0.0]
    top = max(bins.values())
    return [a for a, L in sorted(bins.items(), key=lambda kv: -kv[1]) if L >= 0.25 * top]


def _context(site: Site, view_bearing: float | None) -> Ctx:
    d = rules.design()
    P = d["placement"]
    lim = d["plate"]
    sr = rules.site_rules()
    H = float(site.building_height)
    floors = max(1, int(math.floor(H / float(d.get("floor_to_floor", 3.0)) + 1e-9)))
    gap_min, gap_clause = rules.tower_gap_for(H)
    cov_max, cov_clause = rules.max_coverage()
    side, front, _ = rules.setback_for(H, site.road_width)
    fc = sr.get("fsi") or {}
    fsi = _lookup(fc["by_road_width"], site.road_width) if fc else 2.0
    fung = float(fc.get("fungible_factor", 1.0)) if fc else 1.0
    bua_perm = site.area * fsi * fung
    countable = float(P.get("fsi_countable_fraction", 0.88))
    plate_total_fsi = bua_perm / (floors * countable)
    cov_target = min(float(P.get("coverage_target", 0.40)), cov_max)
    plate_total_target = min(plate_total_fsi, cov_target * site.area)
    fire = sr.get("fire_access") or {"driveway_width": 6.0, "turning_radius": 9.0, "max_offset": 9.0,
                                     "faces_by_height": [[9999.0, 1]], "dead_end_max": 20.0, "clause": ""}
    entry = sr.get("entry") or {"gate_width": 6.0, "junction_clearance": 9.0, "clause": ""}
    ramp = sr.get("parking_ramp") or {"min_plot_area": 1e12, "width_by_plot": [[9e9, 6.0]], "length": 30.0, "clause": ""}
    rg = sr.get("recreational_open_space") or {"by_plot_area": [[9e9, 0.0]], "clause": ""}
    wind: list[tuple[float, float, str]] = []
    wind_from = wind_label = None
    hot_az = 270.0
    if site.climate:
        wind = [(b.direction, b.frequency, b.label) for b in site.climate.wind.bins if b.frequency > 0]
        if wind:
            top = max(wind, key=lambda b: b[1])
            wind_from, wind_label = top[0], top[2]
        if site.climate.hot_facades:
            cs = sum(math.cos(math.radians(geo.COMPASS16.index(h) * 22.5)) for h in site.climate.hot_facades)
            sn = sum(math.sin(math.radians(geo.COMPASS16.index(h) * 22.5)) for h in site.climate.hot_facades)
            hot_az = (math.degrees(math.atan2(sn, cs)) + 360) % 360
    view, view_src = None, "unknown"
    if view_bearing is not None:
        view, view_src = float(view_bearing) % 360, "given"
    elif site.climate and site.climate.sea_view == "yes":
        view, view_src = float(P.get("sea_bearing_default", 270.0)), "sea assumed to the west"
    w = {k: float(P["weights"].get(k, 0.0)) for k in TERM_KEYS}
    tot = sum(w.values()) or 1.0
    w = {k: v / tot for k, v in w.items()}
    ring = site.polygon
    n = len(ring)
    segs = np.array([[*ring[i], *ring[(i + 1) % n]] for i in range(n)], dtype=float)
    roads = sorted({i for i in site.road_edges if 0 <= i < n})
    seg_road = np.array([i in roads for i in range(n)])
    road_lines = [LineString([ring[i], ring[(i + 1) % n]]) for i in roads]
    road_normals = [geo.bearing_of_normal(ring[i], ring[(i + 1) % n]) for i in roads]
    junction = [ring[(i + 1) % n] for i in roads if ((i + 1) % n) in roads]
    ramp_needed = site.area >= float(ramp.get("min_plot_area", 1000.0))
    edge_angles = _edge_angles(ring)
    return Ctx(edge_angles=edge_angles,
        site=site, plot=Polygon(ring), buildable=buildable_geom(site), H=H, floors=floors, gap_min=gap_min,
        gap_clause=gap_clause, cov_max=cov_max, cov_clause=cov_clause, side=side, front=front, fsi=fsi, fung=fung,
        fsi_clause=fc.get("clause", ""), bua_perm=bua_perm, countable=countable, plate_total_target=plate_total_target,
        plate_total_fsi=plate_total_fsi, P=P, lim=lim, fire=fire, entry=entry, ramp=ramp,
        rg_frac=_lookup(rg["by_plot_area"], site.area), rg_clause=rg.get("clause", ""),
        fire_faces=int(_lookup(fire["faces_by_height"], H)), ramp_needed=ramp_needed,
        ramp_w=_lookup(ramp["width_by_plot"], site.area), ramp_l=float(ramp.get("length", 30.0)),
        core_area=_lookup(P.get("core_area_by_height", [[9999.0, 60.0]]), H), wind=wind, wind_from=wind_from,
        wind_label=wind_label, hot_az=hot_az, view=view, view_src=view_src, weights=w, segs=segs, seg_road=seg_road,
        road_lines=road_lines, road_normals=road_normals,
        road_union=shapely.union_all(road_lines) if road_lines else None, junction_pts=junction,
    )


# ------------------------------------------------------------------------------------------- orientation terms


def _g_incidence(inc: float) -> float:
    """Ventilation value of wind meeting a facade at ``inc`` degrees from its normal: oblique winds up to 60 degrees
    ventilate as well as normal ones (Givoni); parallel and leeward winds do not."""
    if inc <= 60.0:
        return 1.0
    if inc >= 90.0:
        return 0.0
    return (90.0 - inc) / 30.0


def _rect_face_set(theta_long: float, w: float, d: float) -> list[tuple[float, float]]:
    """(length, outward bearing) of a w x d rectangle whose long axis has math angle theta_long."""
    b_long = (-theta_long) % 360.0  # bearing of the normal of the 'upper' long face
    b_short = (90.0 - theta_long) % 360.0
    return [(w, b_long), (w, (b_long + 180) % 360), (d, b_short), (d, (b_short + 180) % 360)]


def _breeze_raw(ctx: Ctx, faces: list[tuple[float, float]]) -> float:
    if not ctx.wind:
        return 0.0
    per = sum(L for L, _ in faces)
    tot = 0.0
    for L, b in faces:
        tot += L * sum(f * _g_incidence(geo.angle_diff(b, wd)) for wd, f, _ in ctx.wind)
    return tot / per


def _orient(ctx: Ctx, theta_long: float, w: float, d: float, n: int) -> tuple[dict[str, float], dict[str, float]]:
    """Position-independent terms for a concept (same for every tower of identical size and angle)."""
    faces = _rect_face_set(theta_long, w, d)
    per = 2 * (w + d)
    west = sum(L * max(0.0, math.cos(math.radians(b - ctx.hot_az))) for L, b in faces) / per
    t_west = float(np.clip(1.0 - west / 0.40, 0.0, 1.0))
    if ctx.wind:
        key = round(w / d, 1)
        if key not in ctx.breeze_best:
            ctx.breeze_best[key] = max(_breeze_raw(ctx, _rect_face_set(a, w, d)) for a in range(0, 180, 3)) or 1.0
        braw = _breeze_raw(ctx, faces)
        t_breeze = braw / ctx.breeze_best[key]
    else:
        braw, t_breeze = 0.0, 0.5
    view_or = (sum(L * max(0.0, math.cos(math.radians(b - ctx.view))) for L, b in faces) / per) if ctx.view is not None else 0.0
    lo, hi = ctx.P.get("ideal_depth_range", [18.0, 22.0])
    tol = float(ctx.P.get("depth_tolerance", 6.0))
    q_d = 1.0 if lo <= d <= hi else max(0.0, 1.0 - (lo - d if d < lo else d - hi) / tol)
    cores = 1 if w <= float(ctx.P.get("one_core_max_length", 48.0)) else 2
    core_share = cores * ctx.core_area / (w * d)
    t_eff = float(np.clip((1.0 - core_share - 0.85) / 0.10, 0.0, 1.0))
    total = n * w * d
    bua = total * ctx.floors * ctx.countable
    util = bua / ctx.bua_perm if ctx.bua_perm > 0 else 0.0
    dev = min(geo.angle_diff(theta_long * 4, a * 4) / 4 for a in ctx.edge_angles) if ctx.edge_angles else 0.0
    t_align = float(np.clip(1.0 - (dev - 3.0) / 17.0, 0.0, 1.0))
    terms = {"fsi": float(min(1.0, util)), "depth": 0.6 * q_d + 0.4 * t_eff, "west_glass": t_west,
             "breeze": float(t_breeze), "alignment": t_align}
    metrics = {"west_frac": west, "breeze_raw": braw, "view_orient": view_or, "core_share": core_share,
               "cores_per_tower": cores, "bua": bua, "fsi_util": util, "depth_q": q_d, "edge_dev": dev}
    return terms, metrics


# --------------------------------------------------------------------------------------------- frames & layouts


@dataclass
class Frame:
    theta: float
    res: float
    x0: float
    y0: float
    mb: np.ndarray  # cells inside the buildable area
    mp: np.ndarray  # cells inside the plot
    Sb: np.ndarray
    fits: dict = field(default_factory=dict)

    def fit(self, hc: int, wc: int) -> np.ndarray:
        k = (hc, wc)
        if k not in self.fits:
            self.fits[k] = _fit(self.Sb, hc, wc)
        return self.fits[k]


@dataclass
class Layout:
    n: int
    kind: str
    theta: float  # frame angle
    long_x: bool
    w: float  # long side
    d: float  # short side
    cells: list[tuple[int, int]]  # top-left (row, col) per tower in the frame raster
    wc: int
    hc: int
    arrangement: str
    mode: str
    fr: Frame
    terms: dict[str, float] = field(default_factory=dict)
    metrics: dict[str, float] = field(default_factory=dict)
    passes: dict[str, bool] = field(default_factory=dict)
    elements: dict[str, Any] = field(default_factory=dict)
    score: float = 0.0
    _polys: list[Polygon] | None = None

    @property
    def theta_long(self) -> float:
        return self.theta % 180.0 if self.long_x else (self.theta + 90.0) % 180.0

    def frame_rects(self) -> list[tuple[float, float, float, float]]:
        r = self.fr.res
        return [(self.fr.x0 + c * r, self.fr.y0 + rr * r, self.fr.x0 + (c + self.wc) * r, self.fr.y0 + (rr + self.hc) * r)
                for rr, c in self.cells]

    @property
    def polys(self) -> list[Polygon]:
        if self._polys is None:
            out = []
            for x0, y0, x1, y1 in self.frame_rects():
                out.append(Polygon(geo.rotate_pts([(x0, y0), (x1, y0), (x1, y1), (x0, y1)], self.theta)))
            self._polys = out
        return self._polys

    def key(self) -> tuple:
        return (self.n, round(self.theta_long) % 180, tuple(sorted((round(p.centroid.x), round(p.centroid.y)) for p in self.polys)),
                round(self.w), round(self.d))


def _frame_angles(ctx: Ctx) -> list[float]:
    """Angles an architect would defend: the long plot edges, true north-south, and the sun/wind compromise."""
    ring = ctx.site.polygon
    bins: dict[float, float] = {}
    for i in range(len(ring)):
        a, b = ring[i], ring[(i + 1) % len(ring)]
        L = math.dist(a, b)
        if L < 8.0:
            continue
        ang = math.degrees(math.atan2(b[1] - a[1], b[0] - a[0])) % 90.0
        for k in list(bins):
            if geo.angle_diff(k * 4, ang * 4) / 4 < 2.0:
                bins[k] += L
                break
        else:
            bins[ang] = L
    base = [a for a, _ in sorted(bins.items(), key=lambda kv: -kv[1])[:3]]
    base.append(0.0)
    if ctx.wind or ctx.view is not None:
        w = ctx.weights
        best, best_v = 0.0, -1.0
        for a in range(0, 90, 3):
            for th in (a, a + 90):
                t, m = _orient(ctx, th, 60.0, 20.0, 1)
                v = w["west_glass"] * t["west_glass"] + w["breeze"] * t["breeze"] + w["view"] * min(1.0, m["view_orient"] / 0.35)
                if v > best_v:
                    best, best_v = float(a), v
        # offered only when it is clearly a different idea from the edge / north-south angles (no 8 degree tilts)
        if all(geo.angle_diff(best * 4, b * 4) / 4 >= 12.0 for b in base):
            base.append(best)
    out: list[float] = []
    for a in base:
        if all(geo.angle_diff(a * 4, b * 4) / 4 >= 3.0 for b in out):
            out.append(round(a % 90.0, 2))
    return out


def _make_frame(ctx: Ctx, theta: float, res: float) -> Frame:
    plot_r = affinity.rotate(ctx.plot, -theta, origin=(0, 0))
    bld_r = affinity.rotate(ctx.buildable, -theta, origin=(0, 0))
    bounds = plot_r.bounds
    mb, x0, y0 = geo.raster_inside(bld_r, res, bounds)
    mp, _, _ = geo.raster_inside(plot_r, res, bounds)
    return Frame(theta=theta, res=res, x0=x0, y0=y0, mb=mb, mp=mp, Sb=_sat(mb))


def _row_starts(colany: np.ndarray, n: int, p: int) -> np.ndarray:
    m = len(colany) - (n - 1) * p
    if m <= 0:
        return np.zeros(0, dtype=int)
    v = colany[:m].copy()
    for i in range(1, n):
        v &= colany[i * p : i * p + m]
    return np.flatnonzero(v)


def _ysel(F: np.ndarray, c: int, how: str) -> int | None:
    rows = np.flatnonzero(F[:, c])
    if rows.size == 0:
        return None
    if how == "low":
        return int(rows[0])
    if how == "high":
        return int(rows[-1])
    med = (rows[0] + rows[-1]) / 2
    return int(rows[np.argmin(np.abs(rows - med))])


def _cluster(F: np.ndarray, n: int, wc: int, hc: int, gap_cells: float, u: tuple[float, float],
             tight: bool) -> list[tuple[int, int]] | None:
    """Greedy 2-D arrangement on fit map F: the first tower goes to the extreme of direction u, each next one to the
    extreme of u (packs along that edge) or as close as possible to the first (tight cluster), never nearer than the
    tower gap (true rect-to-rect distance)."""
    rr, cc = np.nonzero(F)
    if rr.size == 0:
        return None
    proj = cc * u[0] + rr * u[1]
    k = int(np.argmax(proj))
    cells = [(int(rr[k]), int(cc[k]))]
    ok = np.ones(rr.size, dtype=bool)
    for _ in range(1, n):
        r0, c0 = cells[-1]
        dx = np.maximum(0, np.abs(cc - c0) - wc)
        dy = np.maximum(0, np.abs(rr - r0) - hc)
        ok &= np.hypot(dx, dy) >= gap_cells - 1e-9
        if not ok.any():
            return None
        idx = np.flatnonzero(ok)
        if tight:
            r1, c1 = cells[0]
            j = idx[np.argmin((cc[idx] - c1) ** 2 + (rr[idx] - r1) ** 2 - 1e-3 * proj[idx])]
        else:
            j = idx[np.argmax(proj[idx])]
        cells.append((int(rr[j]), int(cc[j])))
    return cells


DIRS8 = [(1, 0), (-1, 0), (0, 1), (0, -1), (0.7, 0.7), (-0.7, 0.7), (0.7, -0.7), (-0.7, -0.7)]


def _arrangement(cells: list[tuple[int, int]], n: int, kind: str, long_x: bool, wc: int, hc: int) -> str:
    """Name the arrangement from the actual positions: a row (aligned within a fifth of the plate), else staggered."""
    if n == 1:
        return "single"
    rows = [r for r, _ in cells]
    cols = [c for _, c in cells]
    if max(rows) - min(rows) <= 0.2 * hc:
        along_x = True
    elif max(cols) - min(cols) <= 0.2 * wc:
        along_x = False
    else:
        return "staggered" if n == 2 else "cluster"
    if along_x == long_x:
        return "end-to-end row"
    return "parallel slabs" if kind == "slab" else "row of point towers"


def _layouts_for(ctx: Ctx, fr: Frame, n: int, kind: str, long_x: bool, w: float, d: float) -> list[Layout]:
    res = fr.res
    Lc, Dc = int(round(w / res)), int(round(d / res))
    wc, hc = (Lc, Dc) if long_x else (Dc, Lc)
    F = fr.fit(hc, wc)
    if F.size == 0 or not F.any():
        return []
    colany = F.any(axis=0)
    cols = np.flatnonzero(colany)
    gap_c = int(math.ceil(ctx.gap_min / res - 1e-9))
    comfort = max(ctx.gap_min, float(ctx.P.get("daylight_ratio", 0.6)) * ctx.H)
    comf_c = int(math.ceil(comfort / res - 1e-9))
    xsets: list[tuple[str, list[int]]] = []
    if n == 1:
        mid = int(cols[np.argmin(np.abs(cols - (cols[0] + cols[-1]) / 2))])
        xsets = [("x-low", [int(cols[0])]), ("x-mid", [mid]), ("x-high", [int(cols[-1])])]
    else:
        p = wc + gap_c
        st = _row_starts(colany, n, p)
        if st.size:
            midst = int(st[np.argmin(np.abs(st - (st[0] + st[-1]) / 2))])
            xsets += [("packed-low", [int(st[0]) + i * p for i in range(n)]),
                      ("packed-high", [int(st[-1]) + i * p for i in range(n)]),
                      ("packed-mid", [midst + i * p for i in range(n)])]
            pc = wc + comf_c
            stc = _row_starts(colany, n, pc)
            if stc.size and pc > p:
                m2 = int(stc[np.argmin(np.abs(stc - (stc[0] + stc[-1]) / 2))])
                xsets.append(("comfort-mid", [m2 + i * pc for i in range(n)]))
            a, b = int(cols[0]), int(cols[-1])
            if b - a >= (n - 1) * p:
                xs = [a, b]
                if n == 3:
                    okm = cols[(cols - a >= p) & (b - cols >= p)]
                    if okm.size:
                        xs = [a, int(okm[np.argmin(np.abs(okm - (a + b) / 2))]), b]
                    else:
                        xs = []
                if len(xs) == n:
                    xsets.append(("spread", xs))
    ymodes = ["low", "mid", "high"] + (["stagger-lh", "stagger-hl"] if n >= 2 else [])
    out: list[Layout] = []
    seen: set[tuple] = set()
    for xname, xs in xsets:
        for ym in ymodes:
            cells = []
            for i, c in enumerate(xs):
                how = ym
                if ym.startswith("stagger"):
                    first, second = ("low", "high") if ym == "stagger-lh" else ("high", "low")
                    how = first if i % 2 == 0 else second
                r = _ysel(F, c, how)
                if r is None:
                    break
                cells.append((r, c))
            if len(cells) != n:
                continue
            k = tuple(cells)
            if k in seen:
                continue
            seen.add(k)
            arr = _arrangement(cells, n, kind, long_x, wc, hc)
            out.append(Layout(n=n, kind=kind, theta=fr.theta, long_x=long_x, w=w, d=d, cells=cells, wc=wc, hc=hc,
                              arrangement=arr, mode=f"{xname}/{ym}", fr=fr))
    if n >= 2:
        gcells = ctx.gap_min / res
        for u in DIRS8:
            for tight in (False, True):
                cells = _cluster(F, n, wc, hc, gcells, u, tight)
                if cells is None:
                    continue
                k = tuple(sorted(cells))
                if k in seen:
                    continue
                seen.add(k)
                arr = _arrangement(cells, n, kind, long_x, wc, hc)
                out.append(Layout(n=n, kind=kind, theta=fr.theta, long_x=long_x, w=w, d=d, cells=sorted(cells, key=lambda x: (x[1], x[0])),
                                  wc=wc, hc=hc, arrangement=arr, mode=f"{'tight' if tight else 'edge'}{u}", fr=fr))
    return out


def _sizes(ctx: Ctx, fr: Frame, n: int, kind: str, long_x: bool, depths: list[float]) -> list[tuple[float, float]]:
    """Largest long side for each depth that fits n identical towers in a row (binary search on fit maps)."""
    res = fr.res
    A_t = ctx.area_per_tower(n)
    lim = ctx.lim
    asp = float(ctx.P.get("slab_min_aspect", 1.9))
    gap_c = int(math.ceil(ctx.gap_min / res - 1e-9))
    out = []
    for d in depths:
        if kind == "slab":
            Lmin, Lmax = max(float(lim["min_length"]), d * asp), min(float(lim["max_length"]), A_t / d)
        else:
            Lmin, Lmax = max(d, float(lim["min_length"])), min(d * asp - res, A_t / d)
        if Lmax < Lmin - 1e-9 or d * Lmin < float(lim["min_area"]) - 1e-6:
            continue
        cores = 1 if Lmax <= float(ctx.P.get("one_core_max_length", 48.0)) else 2
        if cores * ctx.core_area / (d * Lmax) > ctx.core_cap:
            continue  # the core would eat the plate: not a buildable tower at this height
        Dc = int(round(d / res))
        lo, hi = int(math.ceil(Lmin / res - 1e-9)), int(math.floor(Lmax / res + 1e-9))

        def ok(Lc: int, Dc: int = Dc) -> bool:
            wc, hc = (Lc, Dc) if long_x else (Dc, Lc)
            F = fr.fit(hc, wc)
            if F.size == 0:
                return False
            ca = F.any(axis=0)
            if n == 1:
                return bool(ca.any())
            if _row_starts(ca, n, wc + gap_c).size > 0:
                return True
            return any(_cluster(F, n, wc, hc, ctx.gap_min / res, u, False) is not None for u in DIRS8[:4])

        if lo > hi or not ok(lo):
            continue
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if ok(mid):
                lo = mid
            else:
                hi = mid - 1
        out.append((lo * res, d))
    return out


# ----------------------------------------------------------------------------------------------- mid evaluation


def _approach_scale(ctx: Ctx) -> tuple[float, float]:
    """Gate-to-tower distance scoring 1 and 0; the zero point grows with the plot (deep towers are normal on big plots)."""
    minx, miny, maxx, maxy = ctx.plot.bounds
    diag = math.hypot(maxx - minx, maxy - miny)
    return float(ctx.P.get("approach_good", 15.0)), max(float(ctx.P.get("approach_bad", 80.0)), 0.6 * diag)


def _privacy_target(ctx: Ctx) -> float:
    return max(float(ctx.P.get("privacy_distance", 18.0)), float(ctx.P.get("privacy_height_ratio", 0.3)) * ctx.H)


def _rays_eval(ctx: Ctx, lay: Layout) -> None:
    """Daylight, privacy, wind shadow, view blocking and approach by casting rays from every facade."""
    polys = lay.polys
    rings = [geo.ring_of(p) for p in polys]
    tower_segs = []
    owner = []
    for ti, r in enumerate(rings):
        for i in range(4):
            tower_segs.append([*r[i], *r[(i + 1) % 4]])
            owner.append(ti)
    tsegs = np.array(tower_segs, dtype=float)
    allsegs = np.vstack([ctx.segs, tsegs])
    m = len(ctx.segs)
    owner_arr = np.array([-1] * m + owner)
    samples = (0.15, 0.5, 0.85)
    orig, dirs, flen, fown, fface = [], [], [], [], []
    faces_all = []
    for ti, r in enumerate(rings):
        for a, b, L, brg, nrm in _faces(r):
            faces_all.append((ti, a, b, L, brg, nrm))
            for s in samples:
                orig.append(a + s * (b - a) + nrm * 0.05)
                dirs.append(nrm)
                flen.append(L / len(samples))
                fown.append(ti)
                fface.append(len(faces_all) - 1)
    orig_a, dirs_a = np.array(orig), np.array(dirs)
    t, idx = _ray_hits(orig_a, dirs_a, allsegs)
    flen_a = np.array(flen)
    own = owner_arr[np.maximum(idx, 0)]
    hit_tower = (idx >= m) & (own >= 0)
    hit_road = (idx >= 0) & (idx < m) & ctx.seg_road[np.minimum(np.maximum(idx, 0), m - 1)]
    D = np.where(hit_tower, t, np.where(hit_road, t + ctx.site.road_width + ctx.front, t + ctx.side))
    D = np.where(np.isfinite(D), D, 1e3)
    ratio = float(ctx.P.get("daylight_ratio", 0.6))
    priv = _privacy_target(ctx)
    day = np.minimum(1.0, D / (ratio * ctx.H))
    pr = np.minimum(1.0, D / priv)
    W = flen_a.sum()
    # half the mean, half the worst quarter of the facade: an architect judges a scheme by its worst flats
    lay.terms["daylight"] = float(0.5 * (day * flen_a).sum() / W + 0.5 * np.percentile(day, 25))
    lay.terms["privacy"] = float(0.5 * (pr * flen_a).sum() / W + 0.5 * np.percentile(pr, 25))
    inter = t[hit_tower]
    lay.metrics["facing_min"] = float(inter.min()) if inter.size else -1.0
    nb = D[~hit_tower & ~hit_road]
    lay.metrics["neighbour_min"] = float(nb.min()) if nb.size else -1.0
    lay.metrics["spacing_ratio"] = float(np.percentile(D, 25) / ctx.H)
    # clear distance in front of each face (fire drive width, setback use)
    clear = np.full(len(faces_all), np.inf)
    for k, fi in enumerate(fface):
        clear[fi] = min(clear[fi], t[k])
    lay.elements["face_clear"] = clear.tolist()
    # necessary condition for fire access (exact check in _full): enough faces per tower that either look onto a
    # road within reach or have a drive-wide clear strip in front
    W = float(ctx.fire.get("driveway_width", 6.0))
    off = float(ctx.fire.get("max_offset", 9.0))
    face_road = np.zeros(len(faces_all), dtype=bool)
    face_road_all = np.ones(len(faces_all), dtype=bool)
    for k, fi in enumerate(fface):
        face_road_all[fi] &= bool(hit_road[k] and t[k] <= off)
    face_road = face_road_all
    need = min(ctx.fire_faces, 4)
    ok_all = True
    for ti in range(lay.n):
        cnt = sum(1 for fi, f in enumerate(faces_all) if f[0] == ti and (face_road[fi] or clear[fi] >= W - 1e-6))
        ok_all &= cnt >= need
    lay.metrics["fire_pre"] = 1.0 if ok_all else 0.0
    lay.elements["faces"] = [(ti, a.tolist(), b.tolist(), L, brg) for ti, a, b, L, brg, _ in faces_all]
    # wind shadow: windward samples cast upwind; another tower within k x H steals the breeze
    if ctx.wind_from is not None and lay.n > 1:
        up = _bvec(ctx.wind_from)
        o2, f2, tw = [], [], []
        for ti, a, b, L, brg, nrm in faces_all:
            if geo.angle_diff(brg, ctx.wind_from) < 90.0:
                for s in samples:
                    o2.append(a + s * (b - a) + nrm * 0.05)
                    f2.append(L)
                    tw.append(ti)
        if o2:
            t2, i2 = _ray_hits(np.array(o2), np.tile(up, (len(o2), 1)), tsegs)
            lim_d = float(ctx.P.get("wind_shadow_heights", 3.0)) * ctx.H
            blocked = np.isfinite(t2) & (t2 <= lim_d)
            f2a, twa = np.array(f2), np.array(tw)
            fac = []
            for ti in range(lay.n):
                sel = twa == ti
                fac.append(float((blocked[sel] * f2a[sel]).sum() / max(1e-9, f2a[sel].sum())) if sel.any() else 0.0)
            lay.metrics["wind_blocked"] = float(np.mean(fac))
            lay.metrics["wind_blocked_max"] = float(max(fac))
        else:
            lay.metrics["wind_blocked"] = lay.metrics["wind_blocked_max"] = 0.0
    else:
        lay.metrics["wind_blocked"] = lay.metrics["wind_blocked_max"] = 0.0
    lay.terms["breeze"] = lay.terms["breeze"] * (1.0 - 0.5 * lay.metrics["wind_blocked"])
    # view: share of facade looking toward the view bearing, unobstructed by another tower
    if ctx.view is not None:
        vd = _bvec(ctx.view)
        o3, wts = [], []
        per = 0.0
        for _ti, a, b, L, brg, nrm in faces_all:
            per += L
            c = math.cos(math.radians(brg - ctx.view))
            if c <= 0:
                continue
            for s in samples:
                o3.append(a + s * (b - a) + nrm * 0.05)
                wts.append(L * c / len(samples))
        if o3 and lay.n > 1:
            t3, _ = _ray_hits(np.array(o3), np.tile(vd, (len(o3), 1)), tsegs)
            free = ~(np.isfinite(t3) & (t3 < 250.0))
        else:
            free = np.ones(len(o3), dtype=bool)
        vf = float((np.array(wts) * free).sum() / per) if o3 else 0.0
        lay.metrics["view_frac"] = vf
        lay.metrics["view_blocked"] = float(1.0 - free.mean()) if o3 else 0.0
        lay.terms["view"] = float(min(1.0, vf / 0.35))
    else:
        lay.terms["view"] = 0.5
    # approach proxy: farthest tower from the nearest road edge
    if ctx.road_union is not None:
        lay.metrics["road_dist"] = float(max(p.distance(ctx.road_union) for p in polys))
    else:
        lay.metrics["road_dist"] = 1e3
    good, bad = _approach_scale(ctx)
    lay.terms["approach"] = float(np.clip(1.0 - (lay.metrics["road_dist"] + 6.0 - good) / (bad - good), 0.0, 1.0))


def _raster_open(ctx: Ctx, lay: Layout) -> None:
    """Fast garden / sliver estimate on the frame raster (refined geometrically for finalists)."""
    fr = lay.fr
    free = fr.mp.copy()
    for r, c in lay.cells:
        free[r : r + lay.hc, c : c + lay.wc] = False
    k = max(2, int(math.ceil(float(ctx.P.get("garden_min_width", 8.0)) / fr.res)))
    usable = _opening(free, k)
    cell = fr.res * fr.res
    garden = float(usable.sum() * cell)
    dead = float((free & fr.mb & ~usable).sum() * cell)
    lab, nlab = ndimage.label(usable)
    areas = np.bincount(lab.ravel())[1:] * cell if nlab else np.zeros(0)
    _garden_terms(ctx, lay, garden, float(areas.max()) if areas.size else 0.0, dead,
                  float(areas[areas >= _big_piece(ctx)].sum()) if areas.size else 0.0)


def _big_piece(ctx: Ctx) -> float:
    """A garden piece this big is a real lawn / play area (several such pieces are as good as one)."""
    return max(300.0, 0.25 * max(ctx.rg_frac, 0.10) * ctx.site.area)


def _garden_terms(ctx: Ctx, lay: Layout, garden: float, largest: float, dead: float, big: float) -> None:
    A = ctx.site.area
    target = max(ctx.rg_frac, 0.10) * A
    in_big = big / garden if garden > 0 else 0.0
    lay.terms["garden"] = float(np.clip(big / target, 0.0, 1.0) * (0.6 + 0.4 * in_big))
    lay.terms["leftovers"] = float(np.clip(1.0 - (dead / A) / 0.12, 0.0, 1.0))
    lay.metrics.update(garden_m2=garden, garden_largest_m2=largest, dead_m2=dead, garden_big_m2=big)


def _weighted(ctx: Ctx, terms: dict[str, float]) -> float:
    return float(sum(ctx.weights[k] * terms.get(k, 0.5) for k in TERM_KEYS))


# ---------------------------------------------------------------------------------------------- full evaluation


def _full(ctx: Ctx, lay: Layout) -> None:
    """Fire-tender access, gate, ramp, drive, garden and slivers on exact geometry."""
    polys = lay.polys
    towers = shapely.union_all(polys)
    plot = ctx.plot
    open_g = _diff(plot, towers)
    W = float(ctx.fire.get("driveway_width", 6.0))
    off = float(ctx.fire.get("max_offset", 9.0))
    turn_r = float(ctx.fire.get("turning_radius", 9.0))
    dead_end = float(ctx.fire.get("dead_end_max", 20.0))
    gate_w = float(ctx.entry.get("gate_width", 6.0))
    jclear = float(ctx.entry.get("junction_clearance", 9.0))
    centre = open_g.buffer(-W / 2.0, quad_segs=4)
    reach = None
    if ctx.road_union is not None and not centre.is_empty:
        zone = ctx.road_union.buffer(W / 2.0 + 0.3)
        parts = [p for p in geo.polygons_of(centre) if p.intersects(zone)]
        reach = shapely.union_all(parts) if parts else None
    faces = lay.elements["faces"]
    clear = lay.elements["face_clear"]
    # ---- fire faces
    per_tower: list[dict] = [{"road": 0, "drive": 0, "faces": []} for _ in polys]
    reach_buf = reach.buffer(off + W / 2.0, quad_segs=4) if reach is not None else None
    for fi, (ti, a, b, L, brg) in enumerate(faces):
        line = LineString([a, b])
        via_road = False
        for rl, rn in zip(ctx.road_lines, ctx.road_normals, strict=True):
            if geo.angle_diff(brg, rn) <= 30.0 and line.intersection(rl.buffer(off + 0.01)).length >= 0.5 * L:
                via_road = True
                break
        via_drive = False
        if not via_road and reach_buf is not None and clear[fi] >= W - 1e-6:
            via_drive = line.intersection(reach_buf).length >= 0.6 * L
        per_tower[ti]["faces"].append({"face": fi, "L": L, "road": via_road, "drive": via_drive, "clear": clear[fi]})
    need = min(ctx.fire_faces, 4)
    drive_parts = []
    fire_ok = True
    fire_clear_min = math.inf
    turning_needed = False
    gate_targets = []
    for pt in per_tower:
        fl = sorted(pt["faces"], key=lambda f: (-(f["road"] or f["drive"]), not f["road"], -f["L"]))
        used = [f for f in fl if f["road"] or f["drive"]][:need] if need > 0 else []
        pt["reached"] = sum(1 for f in fl if f["road"] or f["drive"])
        pt["used"] = used
        if need > 0 and len(used) < need:
            fire_ok = False
        for f in used:
            if f["drive"]:
                turning_needed = True
                a, b = faces[f["face"]][1], faces[f["face"]][2]
                strip = _drive_strip(np.array(a), np.array(b), W, off, open_g)
                drive_parts.append(strip)
                band = strip if not strip.is_empty else _clean(_inter(
                    reach, LineString([a, b]).buffer(off + W / 2.0, cap_style="flat")))
                fire_clear_min = min(fire_clear_min, f["clear"])
                gate_targets.append(band)
            elif f["road"]:
                fire_clear_min = min(fire_clear_min, f["clear"] if f["clear"] < 1e3 else 99.0)
    # ---- gate: on a road edge, opening onto >= 6 m deep open ground, clear of junctions; nearest the towers
    gate = None
    gate_cost = math.inf
    for rl in ctx.road_lines:
        L = rl.length
        if L < gate_w:
            continue
        steps = np.arange(gate_w / 2.0, L - gate_w / 2.0 + 1e-9, 1.0)
        pts = [rl.interpolate(s) for s in steps]
        for p in pts:
            if any(math.dist((p.x, p.y), j) < jclear for j in ctx.junction_pts):
                continue
            if reach is None or p.distance(reach) > W / 2.0 + 0.6:
                continue
            cost = max(p.distance(q) for q in polys)
            if gate_targets:
                cost += 0.25 * max(p.distance(g) for g in gate_targets)
            if cost < gate_cost - 1e-9:
                gate_cost, gate = cost, p
    road_entry_ok = gate is not None
    # connect the gate to the fire bands and to every tower entrance with orthogonal drives clipped to the open ground
    if gate is not None:
        for g in gate_targets:
            if not g.is_empty:
                q = nearest_points(gate, g)[1]
                drive_parts.append(_lpath(gate, q, lay.theta, W, open_g))
        for p in polys:
            if gate.distance(p) > 0.5:
                q = nearest_points(gate, p)[1]
                drive_parts.append(_lpath(gate, q, lay.theta, W * 0.75, open_g))
    drive_parts = [_clean(x) for x in drive_parts]
    drive = _clean(_union([x for x in drive_parts if not x.is_empty])) if drive_parts else Polygon()
    # turning: loop round a tower, a short dead end (measured to the FAR end of the drive), a 9 m turning circle
    # (reserved as hard paving), or a second road connection (exit gate, reserved and drawn)
    turning_ok, turning_how = True, "road"
    exit_gate = None
    if turning_needed:
        turning_ok, turning_how = False, ""
        if reach is not None:
            for part in geo.polygons_of(reach):
                if any(Polygon(r).contains(p.centroid) for r in part.interiors for p in polys):
                    turning_ok, turning_how = True, "loop round the tower"
                    break
        if not turning_ok and gate is not None and gate_targets:
            far = max(gate.hausdorff_distance(g) for g in gate_targets if not g.is_empty)
            if far <= dead_end:
                turning_ok, turning_how = True, f"dead end {far:.0f} m (<= {dead_end:.0f} m)"
        if not turning_ok and not drive.is_empty:
            circ = open_g.buffer(-turn_r, quad_segs=4)
            if not circ.is_empty and circ.distance(drive) <= turn_r:
                far_pt = max((g.centroid for g in gate_targets if not g.is_empty),
                             key=lambda c: gate.distance(c) if gate is not None else 0.0, default=drive.centroid)
                ctr = nearest_points(circ, far_pt)[0]
                disc = _clean(_inter(ctr.buffer(turn_r, quad_segs=8), open_g))
                drive = _clean(_union([drive, disc]))
                turning_ok, turning_how = True, f"{turn_r:.0f} m turning circle"
        if not turning_ok and reach is not None and gate is not None:
            z = _inter(ctx.road_union.buffer(W / 2.0 + 0.3), reach)
            pieces = geo.polygons_of(z)
            if len(pieces) >= 2:
                far_piece = max(pieces, key=lambda q: q.distance(gate))
                if far_piece.distance(gate) >= 12.0:
                    exit_gate = nearest_points(ctx.road_union, far_piece.centroid)[0]
                    turning_ok, turning_how = True, "drive-through to a second (exit) gate"
    fire_pass = fire_ok and turning_ok and (need == 0 or road_entry_ok or all(
        all(f["road"] for f in pt["used"]) for pt in per_tower))
    # ---- ramp near the gate, outside towers, drive and gate throat
    ramp_poly = None
    ramp_ok = True
    if ctx.ramp_needed:
        ramp_ok = False
        avail = _diff(_diff(open_g, towers.buffer(1.5)), drive)
        for gp in (gate, exit_gate):
            if gp is not None:
                avail = _diff(avail, gp.buffer(gate_w * 0.75))
        ramp_poly = _fit_ramp(ctx, avail, gate, reach, drive, ctx.ramp_w, ctx.ramp_l)
        if ramp_poly is not None:
            parking_mode = "ramp"
        else:  # no room for a ramp: reserve a car-lift bay beside the gate instead (costlier, slower)
            bw, bl = (ctx.ramp.get("car_lift_bay") or [6.0, 7.5])[:2]
            ramp_poly = _fit_ramp(ctx, avail, gate, reach, drive, float(bw), float(bl))
            parking_mode = "car lifts" if ramp_poly is not None else "none"
        ramp_ok = ramp_poly is not None
    else:
        parking_mode = "stilt"
    lay.metrics["parking_ramp"] = 1.0 if parking_mode == "ramp" else 0.0
    lay.elements["parking_mode"] = parking_mode
    # ---- garden and slivers (exact)
    g0 = _diff(open_g, drive)
    if ramp_poly is not None:
        g0 = _diff(g0, ramp_poly)
    for gp in (gate, exit_gate):
        if gp is not None:
            g0 = _diff(g0, gp.buffer(gate_w * 0.75))
    r = float(ctx.P.get("garden_min_width", 8.0)) / 2.0
    garden_g = _inter(g0.buffer(-r, join_style="mitre").buffer(r, join_style="mitre"), g0)
    pieces = [p for p in geo.polygons_of(garden_g) if p.area >= 20.0]
    garden = sum(p.area for p in pieces)
    largest = max((p.area for p in pieces), default=0.0)
    dead = _diff(_inter(g0, ctx.buildable), garden_g).area
    _garden_terms(ctx, lay, garden, largest, dead, sum(p.area for p in pieces if p.area >= _big_piece(ctx)))
    # ---- approach (gate to farthest tower) and phasing
    good, bad = _approach_scale(ctx)
    if gate is not None:
        gd = max(gate.distance(p) for p in polys)
        lay.metrics["gate_dist"] = gd
        lay.terms["approach"] = float(np.clip(1.0 - (gd - good) / (bad - good), 0.0, 1.0))
    else:
        lay.metrics["gate_dist"] = -1.0
        lay.terms["approach"] = 0.0
    if parking_mode == "car lifts":
        lay.terms["approach"] *= 0.7  # every car waits for a lift at the gate
    if ctx.site.area < float(ctx.P.get("phasing_min_plot", 6000.0)):
        lay.terms["phasing"] = 0.5
        lay.metrics["phasing_independent"] = -1.0
    elif lay.n == 1:
        lay.terms["phasing"] = 0.3
        lay.metrics["phasing_independent"] = 0.0
    else:
        indep = all(any(f["road"] or f["drive"] for f in pt["faces"]) for pt in per_tower)
        lay.terms["phasing"] = 1.0 if indep else 0.6
        lay.metrics["phasing_independent"] = 1.0 if indep else 0.0
    # ---- hard rules
    total = sum(p.area for p in polys)
    gaps = [polys[i].distance(polys[j]) for i in range(len(polys)) for j in range(i + 1, len(polys))]
    mg = min(gaps) if gaps else None
    inside = all(ctx.buildable.buffer(1e-6).contains(p) for p in polys)
    lay.passes = {
        "inside_buildable": bool(inside),
        "gap": mg is None or mg >= ctx.gap_min - 1e-6,
        "coverage": total / ctx.site.area <= ctx.cov_max + 1e-9,
        "fsi": lay.metrics["bua"] <= ctx.bua_perm + 1e-6,
        "fire_access": bool(fire_pass),
        "road_entry": bool(road_entry_ok),
        "parking_access": bool(ramp_ok),
        "plate_viable": lay.metrics["core_share"] <= float(ctx.P.get("max_core_share", 0.20)) + 1e-9,
    }
    lay.metrics.update(
        min_gap=mg if mg is not None else -1.0, coverage=total / ctx.site.area, total_area=total,
        fire_faces_need=need, fire_faces_min=min(len(pt["used"]) for pt in per_tower) if per_tower else 0,
        fire_faces_reached_min=min(pt["reached"] for pt in per_tower) if per_tower else 0,
        fire_clear_min=fire_clear_min if math.isfinite(fire_clear_min) else -1.0,
        fire_internal=1.0 if turning_needed else 0.0,
    )
    lay.elements.update(
        gate=[round(gate.x, 2), round(gate.y, 2)] if gate is not None else None,
        exit_gate=[round(exit_gate.x, 2), round(exit_gate.y, 2)] if exit_gate is not None else None,
        ramp=[[round(x, 2), round(y, 2)] for x, y in geo.ring_of(ramp_poly)] if ramp_poly is not None else None,
        drive=[[[round(x, 2), round(y, 2)] for x, y in rr] for rr in geo.rings_of(drive, 1.0)],
        garden=[[[round(x, 2), round(y, 2)] for x, y in geo.ring_of(p)] for p in pieces],
        turning=turning_how,
        fire_by_tower=[{"tower": f"T{i + 1}", "reached": pt["reached"],
                        "used": [("road" if f["road"] else "drive") for f in pt["used"]]} for i, pt in enumerate(per_tower)],
    )
    lay.score = _weighted(ctx, lay.terms)


def _fit_ramp(ctx: Ctx, avail, gate, reach, drive, rw: float, rl: float) -> Polygon | None:
    """A ramp rectangle in open ground, its upper end touching the drive / gate, as close to the gate as possible
    (cars stay out of the garden)."""
    if avail.is_empty:
        return None
    res = 1.0
    ring = ctx.site.polygon
    angs = []
    for i in range(len(ring)):
        a, b = ring[i], ring[(i + 1) % len(ring)]
        if math.dist(a, b) >= 8.0:
            ang = math.degrees(math.atan2(b[1] - a[1], b[0] - a[0])) % 90.0
            if all(geo.angle_diff(ang * 4, x * 4) / 4 >= 3.0 for x in angs):
                angs.append(ang)
    target = gate if gate is not None else ctx.plot.centroid
    link = drive if (drive is not None and not drive.is_empty) else None
    if reach is not None:
        link = reach if link is None else link.union(reach)
    best, best_cost = None, math.inf
    for th in angs[:4]:
        rot = affinity.rotate(avail, -th, origin=(0, 0))
        if rot.is_empty:
            continue
        m, x0, y0 = geo.raster_inside(rot, res)
        S = _sat(m)
        for wc, hc in ((int(math.ceil(rl)), int(math.ceil(rw))), (int(math.ceil(rw)), int(math.ceil(rl)))):
            F = _fit(S, hc, wc)
            if F.size == 0 or not F.any():
                continue
            rr, cc = np.nonzero(F)
            xa, ya = x0 + cc * res, y0 + rr * res
            xb, yb = xa + wc * res, ya + hc * res
            if wc >= hc:  # ends on the left/right
                e1 = np.stack([xa, (ya + yb) / 2], 1)
                e2 = np.stack([xb, (ya + yb) / 2], 1)
            else:
                e1 = np.stack([(xa + xb) / 2, ya], 1)
                e2 = np.stack([(xa + xb) / 2, yb], 1)
            c, s = math.cos(math.radians(th)), math.sin(math.radians(th))
            R = np.array([[c, -s], [s, c]])
            e1s, e2s = e1 @ R.T, e2 @ R.T
            d1 = np.hypot(e1s[:, 0] - target.x, e1s[:, 1] - target.y)
            d2 = np.hypot(e2s[:, 0] - target.x, e2s[:, 1] - target.y)
            dmin = np.minimum(d1, d2)
            mids = np.stack([(xa + xb) / 2, (ya + yb) / 2], 1) @ R.T
            edge_d = shapely.distance(shapely.points(mids), ctx.plot.exterior) - min(rw, rl) / 2
            cost_all = dmin + 1.5 * np.maximum(edge_d, 0.0)
            order = np.argsort(cost_all)[:60]
            for k in order:
                end = e1s[k] if d1[k] <= d2[k] else e2s[k]
                if link is not None and SPoint(end).distance(link) > 4.0:
                    continue
                cost = float(cost_all[k])
                if cost < best_cost:
                    pts = [(xa[k], ya[k]), (xb[k], ya[k]), (xb[k], yb[k]), (xa[k], yb[k])]
                    best_cost, best = cost, Polygon(geo.rotate_pts(pts, th))
                break
    return best


# ------------------------------------------------------------------------------------------- reasons & options


def _structure(lay: Layout) -> tuple:
    return (lay.n, lay.kind, lay.arrangement)


def _different(a: Layout, b: Layout) -> bool:
    """Meaningfully different: tower count, slab vs point, arrangement, or a clearly different footprint/angle."""
    if _structure(a) != _structure(b):
        return True
    ang = geo.angle_diff(a.theta_long * 2, b.theta_long * 2) / 2
    ua, ub = shapely.union_all(a.polys), shapely.union_all(b.polys)
    iou = ua.intersection(ub).area / max(1e-9, ua.union(ub).area)
    return ang >= 25.0 and iou < 0.5


def _typology_hint(lay: Layout) -> str:
    asp = lay.w / lay.d
    if lay.kind == "point":
        return "cruciform" if lay.d >= 24 and lay.w * lay.d >= 600 else "central_core"
    if lay.d >= 20 and lay.w >= 40:
        return "h_shape"
    if asp >= 3.0 and lay.d <= 18:
        return "side_core"
    return "t_shape" if lay.d >= 20 else "central_core"


def _pct(x: float) -> str:
    return f"{x * 100:.0f}%"


def _texts(ctx: Ctx, lay: Layout) -> dict[str, tuple[str, float]]:
    """(text, comparable metric) per soft term."""
    m = lay.metrics
    face_long = geo.compass((-lay.theta_long) % 360)
    face_back = geo.compass((180 - lay.theta_long) % 360)
    out: dict[str, tuple[str, float]] = {}
    out["fsi"] = (f"built-up {m['bua']:,.0f} m2 = {_pct(m['fsi_util'])} of permissible {ctx.bua_perm:,.0f} m2 "
                  f"(FSI {ctx.fsi:.2f} x {ctx.fung:.2f} fungible, {ctx.floors} floors)", m["fsi_util"])
    core = f"; {int(m['cores_per_tower'])} cores per tower" if m["cores_per_tower"] > 1 else ""
    out["depth"] = (f"plate {lay.w:.1f} x {lay.d:.1f} m, depth {'within' if m['depth_q'] >= 1 else 'outside'} the "
                    f"18-22 m ideal; core about {_pct(m['core_share'])} of the plate{core}", m["core_share"])
    out["west_glass"] = (f"west-facing facade {_pct(m['west_frac'])} of the perimeter (long faces {face_long}/{face_back})",
                         m["west_frac"])
    if ctx.wind:
        sh = f"; {_pct(m['wind_blocked'])} of windward facade in another tower's wind shadow" if m.get("wind_blocked", 0) > 0.05 else ""
        out["breeze"] = (f"faces catch {_pct(lay.terms['breeze'])} of the best breeze exposure for this plate "
                         f"(prevailing {ctx.wind_label}){sh}", lay.terms["breeze"])
    else:
        out["breeze"] = ("no wind data for this site", 0.5)
    if ctx.view is not None:
        out["view"] = (f"{_pct(m['view_frac'])} of the facade looks toward the view ({geo.compass(ctx.view)}, {ctx.view_src})"
                       + (f"; {_pct(m['view_blocked'])} of it blocked by another tower" if m.get("view_blocked", 0) > 0.05 else ""),
                       m["view_frac"])
    else:
        out["view"] = ("no view bearing given (set the sea view in stage 3)", 0.0)
    fm, nm = m.get("facing_min", -1), m.get("neighbour_min", -1)
    bits = []
    if fm >= 0:
        bits.append(f"facing towers {fm:.1f} m apart")
    if nm >= 0:
        bits.append(f"neighbours at least {nm:.1f} m away if they build to their setback")
    out["privacy"] = ("; ".join(bits) + f" (privacy target {_privacy_target(ctx):.0f} m)",
                      fm if fm >= 0 else nm)
    out["daylight"] = (f"clear distance in front of the facades is {m['spacing_ratio']:.2f} x the {ctx.H:.0f} m height "
                       f"for the lowest quarter (target {float(ctx.P.get('daylight_ratio', 0.6)):.1f})", m["spacing_ratio"])
    rg = ctx.rg_frac * ctx.site.area
    out["garden"] = (f"usable garden {m['garden_m2']:,.0f} m2 ({_pct(m['garden_m2'] / ctx.site.area)} of the plot), "
                     f"{m['garden_big_m2']:,.0f} m2 of it in lawns of {_big_piece(ctx):,.0f} m2 or more" + (f"; RG needed {rg:,.0f} m2" if rg > 0 else ""),
                     m["garden_m2"])
    out["leftovers"] = (f"{m['dead_m2']:,.0f} m2 of buildable land left as strips under "
                        f"{float(ctx.P.get('garden_min_width', 8.0)):.0f} m wide ({_pct(m['dead_m2'] / ctx.site.area)} of the plot)",
                        m["dead_m2"])
    if m["edge_dev"] <= 3.0:
        out["alignment"] = ("parallel to the plot edges: basement parking grid, drives and garden stay rectangular",
                            m["edge_dev"])
    else:
        out["alignment"] = (f"turned {m['edge_dev']:.0f} deg from the plot edges: basement column grid clashes with the "
                            "parking grid and leaves wedge-shaped leftovers", m["edge_dev"])
    gd = m.get("gate_dist", -1)
    out["approach"] = ((f"gate to the farthest tower {gd:.0f} m" if gd >= 0 else "no gate position on the road"), gd)
    if m.get("phasing_independent", -1) < 0:
        out["phasing"] = ("plot too small for phasing to matter", 0.5)
    elif lay.n == 1:
        out["phasing"] = ("one tower: cannot be phased", 0.0)
    else:
        out["phasing"] = (("each tower has its own access: can be built in phases" if m["phasing_independent"] else
                           "towers share one access: phasing is harder"), m["phasing_independent"])
    return out


def _viable_height(ctx: Ctx, n: int) -> float:
    """Tallest height (floor multiples) at which n towers sharing the permissible BUA keep the core share under the
    limit - the advice when the requested height leaves only toy plates."""
    f2f = float(rules.design().get("floor_to_floor", 3.0))
    cap = float(ctx.P.get("max_core_share", 0.20))
    best = f2f
    for floors in range(1, 80):
        h = floors * f2f
        core = _lookup(ctx.P.get("core_area_by_height", [[9999.0, 60.0]]), h)
        plate = ctx.bua_perm / (floors * ctx.countable) / n
        plate = min(plate, float(ctx.lim["max_area_per_tower"]), ctx.cov_max * ctx.site.area / n)
        if core / plate <= cap:
            best = h
    return best


def _rule_reasons(ctx: Ctx, lay: Layout) -> list[dict]:
    m, e, p = lay.metrics, lay.elements, lay.passes
    out = []

    def add(key: str, ok: bool, text: str):
        out.append({"key": key, "kind": "rule" if ok else "fail", "text": text, "value": 1.0 if ok else 0.0, "impact": 0.0})

    add("inside_buildable", p["inside_buildable"],
        f"inside the setbacks ({ctx.side:.1f} m side/rear, {ctx.front:.1f} m front for a {ctx.site.road_width:.0f} m road)")
    if lay.n > 1:
        add("gap", p["gap"], f"towers {m['min_gap']:.1f} m apart, rule {ctx.gap_min:.1f} m at {ctx.H:.0f} m height")
    add("coverage", p["coverage"], f"ground coverage {_pct(m['coverage'])} (limit {_pct(ctx.cov_max)})")
    need = int(m["fire_faces_need"])
    if need == 0:
        add("fire_access", p["fire_access"], f"fire tender works from the road (no drive needed up to {ctx.H:.0f} m)")
    else:
        how = sorted({u for t in e["fire_by_tower"] for u in t["used"]})
        clear = m["fire_clear_min"]
        txt = (f"{float(ctx.fire.get('driveway_width', 6.0)):.0f} m fire-tender access reaches "
               f"{int(m['fire_faces_min'])} of {need} required face{'s' if need > 1 else ''} of every tower "
               f"(via {' and '.join(how) or 'nothing'}")
        if m["fire_internal"] and clear > 0:
            txt += f", {clear:.1f} m clear"
        txt += ")"
        if m["fire_internal"]:
            txt += f"; turning: {e['turning'] or 'none'}"
        add("fire_access", p["fire_access"], txt)
    cap = float(ctx.P.get("max_core_share", 0.20))
    if not p.get("plate_viable", True):
        add("plate_viable", False, f"core takes {_pct(m['core_share'])} of each {lay.w * lay.d:,.0f} m2 plate (limit "
            f"{_pct(cap)}): the FSI spreads over {ctx.floors} floors; about {_viable_height(ctx, lay.n):.0f} m height "
            "would give a workable plate")
    add("road_entry", p["road_entry"], "vehicle gate on the road, clear of the junction" if p["road_entry"] else
        "no 6 m gate onto open ground on any road edge")
    if ctx.ramp_needed:
        mode = e.get("parking_mode")
        if mode == "ramp":
            add("parking_access", True, f"{ctx.ramp_w:.1f} x {ctx.ramp_l:.0f} m basement ramp fits beside the gate")
        elif mode == "car lifts":
            bw, bl = (ctx.ramp.get("car_lift_bay") or [6.0, 7.5])[:2]
            out.insert(0, {"key": "parking_access", "kind": "con", "value": 0.5, "impact": 0.0,
                           "text": f"no room for a {ctx.ramp_w:.1f} x {ctx.ramp_l:.0f} m ramp: {bw:.0f} x {bl:.1f} m "
                                   "car-lift bay off the drive instead (slower, costlier)"})
        else:
            add("parking_access", False, "no room for a basement ramp or a car-lift bay near the gate")
    return out


def _option_reasons(ctx: Ctx, lay: Layout, others: list[tuple[str, Layout]]) -> list[dict]:
    """Soft reasons ranked by weighted advantage over the other options, with their numbers quoted."""
    texts = _texts(ctx, lay)
    oth_texts = [(oid, _texts(ctx, o), o) for oid, o in others]
    soft = []
    for k in TERM_KEYS:
        v = lay.terms.get(k, 0.5)
        if others:
            mean_o = sum(o.terms.get(k, 0.5) for _, o in others) / len(others)
        else:
            mean_o = 0.5
        impact = ctx.weights[k] * (v - mean_o)
        text, metric = texts[k]
        if k in ("view", "breeze") and text.startswith("no "):
            continue
        cmp_bits = []
        for oid, ot, _o in oth_texts:
            om = ot[k][1]
            if k in ("west_glass",):
                cmp_bits.append(f"{_pct(om)} for {oid}")
            elif k in ("fsi",):
                cmp_bits.append(f"{_pct(om)} for {oid}")
            elif k in ("garden", "leftovers"):
                cmp_bits.append(f"{om:,.0f} m2 for {oid}")
            elif k == "approach" and om >= 0:
                cmp_bits.append(f"{om:.0f} m for {oid}")
            elif k == "view":
                cmp_bits.append(f"{_pct(om)} for {oid}")
            elif k == "breeze":
                cmp_bits.append(f"{_pct(om)} for {oid}")
            elif k == "daylight":
                cmp_bits.append(f"{om:.2f} for {oid}")
        full = text + (f" (vs {', '.join(cmp_bits)})" if cmp_bits else "")
        ov = [o.terms.get(k, 0.5) for _, o in others]
        if others:  # pro = best (or tied) of the options shown, con = worst (or tied), with a real weighted difference
            kind = ("pro" if impact > 0.002 and v >= max(ov) - 0.02 else
                    "con" if impact < -0.002 and v <= min(ov) + 0.02 else "note")
        else:
            kind = "pro" if v >= 0.75 else "con" if v < 0.4 else "note"
        soft.append({"key": k, "kind": kind, "text": full, "short": text, "value": round(v, 4), "impact": round(impact, 4)})
    soft.sort(key=lambda r: -abs(r["impact"]))
    rules_r = _rule_reasons(ctx, lay)
    fails = [r for r in rules_r if r["kind"] in ("fail", "con")]
    oks = [r for r in rules_r if r["kind"] == "rule"]
    return fails + soft + oks


def _short_concept(lay: Layout) -> str:
    kind = "slab" if lay.kind == "slab" else "point tower"
    if lay.n == 1:
        return f"one {kind}"
    word = {2: "two", 3: "three"}[lay.n]
    return f"{word} {kind}s, {lay.arrangement}"


def drivers_note(opt: PlacementOption, ctx: dict) -> str:
    """Template design-drivers sentence (T5 safe default), built from the ranked reasons when present."""
    if opt.reasons:
        pros = [r.get("short", r["text"]) for r in opt.reasons if r.get("kind") == "pro"][:3]
        cons = [r.get("short", r["text"]) for r in opt.reasons if r.get("kind") in ("con", "fail")][:1]
        bits = pros or [r.get("short", r["text"]) for r in opt.reasons if r.get("kind") == "note"][:2]
        s = "Design drivers: " + "; ".join(bits)
        if cons:
            s += ". Watch: " + cons[0]
        return s + "."
    t = opt.score_terms
    bits = []
    if opt.plates:
        fb = (-opt.plates[0].angle_deg) % 360
        bits.append(f"long faces {geo.compass(fb)}/{geo.compass((fb + 180) % 360)}")
    if t.get("west_glass", 0) >= 0.6:
        bits.append("little glass on the hot west")
    else:
        bits.append("west faces need shading fins")
    if opt.towers > 1 and opt.min_gap is not None:
        bits.append(f"{opt.min_gap:.1f} m between towers")
    bits.append(f"{opt.coverage:.0%} coverage")
    return "Design drivers: " + "; ".join(bits) + "."


def _to_option(ctx: Ctx, lay: Layout, oid: str) -> PlacementOption:
    plates = []
    for i, ((x0, y0, x1, y1), p) in enumerate(zip(lay.frame_rects(), lay.polys, strict=True)):
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        center = geo.rotate_pts([(cx, cy)], lay.theta)[0]
        plates.append(PlateFootprint(
            id=f"T{i + 1}", polygon=[(round(x, 3), round(y, 3)) for x, y in geo.ring_of(p)],
            angle_deg=round(lay.theta_long, 3), center=(round(center[0], 3), round(center[1], 3)),
            width=round(lay.w, 2), depth=round(lay.d, 2), area=round(p.area, 2)))
    total = sum(p.area for p in lay.polys)
    mg = lay.metrics.get("min_gap", -1.0)
    label = f"{FAMILY_LABEL[lay.n]}: {_short_concept(lay)}"
    return PlacementOption(
        id=oid,  # type: ignore[arg-type]
        towers=lay.n, label=label, plates=plates, coverage=round(total / ctx.site.area, 4), total_area=round(total, 2),
        min_gap=round(mg, 2) if mg >= 0 else None, score=round(lay.score, 4),
        score_terms={k: round(lay.terms.get(k, 0.5), 4) for k in TERM_KEYS}, drivers="", passes=dict(lay.passes),
        concept=lay.kind, arrangement=lay.arrangement, typology_hint=_typology_hint(lay),
        metrics={k: round(float(v), 4) for k, v in lay.metrics.items()},
        site_elements={k: v for k, v in lay.elements.items() if k not in ("faces", "face_clear")},
    )


def _runner(ctx: Ctx, lay: Layout, ref: Layout) -> dict:
    diffs = sorted((ctx.weights[k] * (lay.terms.get(k, .5) - ref.terms.get(k, .5)), k) for k in TERM_KEYS)
    worse = [TERM_LABEL[k].lower() for d, k in diffs if d < -0.005][:2]
    better = [TERM_LABEL[k].lower() for d, k in reversed(diffs) if d > 0.005][:1]
    why = f"scores {ref.score - lay.score:.3f} lower" + (f": weaker on {', '.join(worse)}" if worse else "")
    if better:
        why += f"; better on {better[0]}"
    return {
        "concept": _short_concept(lay), "arrangement": lay.arrangement, "angle_deg": round(lay.theta_long, 1),
        "score": round(lay.score, 4), "coverage": round(sum(p.area for p in lay.polys) / ctx.site.area, 4),
        "plate": [round(lay.w, 1), round(lay.d, 1)], "passes": all(lay.passes.values()), "why_not": why,
        "plates": [[[round(x, 3), round(y, 3)] for x, y in geo.ring_of(p)] for p in lay.polys],
    }


# ------------------------------------------------------------------------------------------------------- driver


def _generate(ctx: Ctx, families: tuple[int, ...], res: float, depths_slab: list[float]) -> dict[int, list[Layout]]:
    angles = _frame_angles(ctx)
    frames: dict[float, Frame] = {}
    by_n: dict[int, list[Layout]] = {n: [] for n in families}
    P = ctx.P
    point_depths = list(P.get("point_depths", [24.0, 28.0, 32.0]))
    for n in families:
        A_t = ctx.area_per_tower(n)
        sq = math.floor(math.sqrt(A_t) / res) * res  # square point tower that uses the area target
        pd = sorted({*[x for x in point_depths if x <= sq + 1e-9],
                     *[x for x in (sq, sq - 3.0) if x >= float(ctx.lim["min_depth"])]})
        thetas = angles if n == 1 else sorted({a for b in angles for a in (b, (b + 90.0) % 180.0)})
        groups = []
        for th in thetas:
            if th not in frames:
                frames[th] = _make_frame(ctx, th, res)
            fr = frames[th]
            for long_x in (True, False):
                for kind, dl in (("slab", depths_slab), ("point", pd)):
                    for w, d in _sizes(ctx, fr, n, kind, long_x, dl):
                        theta_long = th % 180.0 if long_x else (th + 90.0) % 180.0
                        terms, metrics = _orient(ctx, theta_long, w, d, n)
                        if ctx.view is not None:
                            terms["view"] = min(1.0, metrics["view_orient"] / 0.35)
                        part = sum(ctx.weights[k] * terms.get(k, 0.5) for k in ("fsi", "depth", "west_glass", "breeze", "view", "alignment"))
                        groups.append((part, fr, kind, long_x, w, d, terms, metrics))
        # keep the strongest concept groups, at most 2 per (kind, orientation class) so variety survives
        groups.sort(key=lambda g: -g[0])
        kept, per_class = [], {}
        for g in groups:
            part, fr, kind, long_x, w, d, terms, metrics = g
            tl = fr.theta % 180.0 if long_x else (fr.theta + 90.0) % 180.0
            cls = (kind, int(((tl + 15) % 180) // 30), long_x if n > 1 else None)
            if per_class.get(cls, 0) >= 2:
                continue
            per_class[cls] = per_class.get(cls, 0) + 1
            kept.append(g)
            if len(kept) >= 10:
                break
        seen: set[tuple] = set()
        for _part, fr, kind, long_x, w, d, terms, metrics in kept:
            for lay in _layouts_for(ctx, fr, n, kind, long_x, w, d):
                k = lay.key()
                if k in seen:
                    continue
                seen.add(k)
                lay.terms = dict(terms)
                lay.metrics = dict(metrics)
                by_n[n].append(lay)
    return by_n


def place(site: Site, families: tuple[int, ...] = (1, 2, 3), view_bearing: float | None = None) -> Placement:
    t0 = time.perf_counter()
    if not site.buildable:
        return Placement(options=[], elapsed_s=round(time.perf_counter() - t0, 3))
    ctx = _context(site, view_bearing)
    if ctx.buildable.is_empty:
        return Placement(options=[], elapsed_s=round(time.perf_counter() - t0, 3))
    P = ctx.P
    res = float(P.get("raster", 1.0))
    if site.buildable_area < float(P.get("raster_fine_below", 6000.0)):
        res = min(res, 0.5)
    slab = list(P.get("slab_depths", [16.0, 18.0, 20.0, 22.0]))
    by_n = _generate(ctx, families, res, slab)
    if not any(by_n.values()):  # narrow plots: allow shallow single-loaded slabs down to the plate minimum
        lo = float(ctx.lim["min_depth"])
        by_n = _generate(ctx, families, res, [x for x in (lo, lo + 2.0, 14.0) if x < min(slab)] or [lo])
    if not any(by_n.values()):  # height and FSI leave only toy plates: show them, failing plate_viable, and say why
        ctx.core_cap = 1.0
        by_n = _generate(ctx, families, res, slab + [float(ctx.lim["min_depth"])])
    finalists: dict[int, list[Layout]] = {}
    for n, lays in by_n.items():
        for lay in lays:
            _rays_eval(ctx, lay)
            _raster_open(ctx, lay)
            lay.terms.setdefault("phasing", 0.5)
            lay.score = _weighted(ctx, lay.terms)
        lays.sort(key=lambda x: (-x.metrics.get("fire_pre", 1.0), -x.score))
        # full geometric check in score order until enough layouts pass the hard rules (or the budget is spent);
        # at most 3 passing layouts per structure, near-duplicates of an evaluated layout skipped
        top: list[Layout] = []
        per_struct: dict[tuple, int] = {}
        n_pass, budget = 0, int(P.get("full_eval_budget", 24))
        want = int(P.get("full_eval_passing", 6))
        for lay in lays:
            if len(top) >= budget or n_pass >= want:
                break
            st = _structure(lay)
            if per_struct.get(st, 0) >= 3:
                continue
            ul = shapely.union_all(lay.polys)
            if any(ul.symmetric_difference(shapely.union_all(t.polys)).area < 0.2 * sum(p.area for p in t.polys)
                   for t in top):
                continue
            _full(ctx, lay)
            top.append(lay)
            if all(lay.passes.values()):
                n_pass += 1
                per_struct[st] = per_struct.get(st, 0) + 1
        finalists[n] = sorted(top, key=lambda x: (not all(x.passes.values()), -x.score))
    pool = [lay for n in finalists for lay in finalists[n]]
    if not pool:
        return Placement(options=[], elapsed_s=round(time.perf_counter() - t0, 3))
    passing = [lay for lay in pool if all(lay.passes.values())]
    cand = passing or pool
    best = max(cand, key=lambda x: x.score)
    share = float(P.get("min_family_share", 0.8))
    chosen: list[Layout] = []
    # 1) best passing concept per tower count, unless clearly worse than the overall best
    for n in sorted(finalists):
        fam = [lay for lay in finalists[n] if lay in cand]
        if fam and fam[0].score >= share * best.score:
            chosen.append(fam[0])
    # 2) fill with structurally different concepts (slab vs point, other arrangements) by score
    for lay in sorted(cand, key=lambda x: -x.score):
        if len(chosen) >= int(P.get("options", 3)):
            break
        if lay in chosen or not all(_different(lay, c) for c in chosen):
            continue
        chosen.append(lay)
    chosen = chosen[: int(P.get("options", 3))]
    ranked = sorted(chosen, key=lambda x: -x.score)
    chosen.sort(key=lambda x: (x.n, -x.score))
    ids = {id(lay): OPTION_IDS[i] for i, lay in enumerate(chosen)}
    options = []
    for lay in chosen:
        oid = ids[id(lay)]
        opt = _to_option(ctx, lay, oid)
        opt.rank = ranked.index(lay) + 1
        opt.recommended = opt.rank == 1
        opt.reasons = _option_reasons(ctx, lay, [(ids[id(o)], o) for o in chosen if o is not lay])
        alts: list[Layout] = []
        for x in finalists.get(lay.n, []):
            if len(alts) >= int(P.get("runners_up", 2)):
                break
            if x is lay or x in chosen or not all(x.passes.values()):
                continue
            if _different(x, lay) and all(_different(x, a) for a in alts):
                alts.append(x)
        opt.runners_up = [_runner(ctx, x, lay) for x in alts]
        opt.drivers = drivers_note(opt, {})
        options.append(opt)
    return Placement(options=options, elapsed_s=round(time.perf_counter() - t0, 3))


def fit_triangle(geom, obstacles=None, angle_step: int = 4, grid: float = 1.5,
                 aspect: float = 0.866) -> PlateFootprint | None:
    """Largest triangle (base B, height aspect*B; equilateral by default) inside ``geom`` minus obstacles.

    Sweeps the orientation (0-119 deg; a triangle repeats every 120), and for each one binary-searches the
    size with a vectorised containment test over a grid of candidate positions."""
    region = geom.difference(obstacles) if obstacles is not None and not obstacles.is_empty else geom
    parts = geo.polygons_of(region)
    if not parts:
        return None
    region = max(parts, key=lambda p: p.area)
    best = None
    for theta in range(0, 120, angle_step):
        rot = shapely.affinity.rotate(region, -theta, origin=(0, 0))
        minx, miny, maxx, maxy = rot.bounds
        xs = np.arange(minx, maxx, grid)
        ys = np.arange(miny, maxy, grid)
        gx, gy = np.meshgrid(xs, ys)
        cx, cy = gx.ravel(), gy.ravel()
        inside = shapely.contains_xy(rot, cx, cy)
        cx, cy = cx[inside], cy[inside]
        if cx.size == 0:
            continue

        def fits(B: float, cx=cx, cy=cy, rot=rot):
            H = aspect * B
            # triangle with its bounding-box centre at (cx, cy): base at cy - H/2
            coords = np.stack([
                np.stack([cx - B / 2, cy - H / 2], 1), np.stack([cx + B / 2, cy - H / 2], 1),
                np.stack([cx, cy + H / 2], 1), np.stack([cx - B / 2, cy - H / 2], 1)], 1)
            tris = shapely.polygons(coords)
            ok = shapely.contains(rot, tris)
            return np.flatnonzero(ok)

        lo, hi = 0.0, math.hypot(maxx - minx, maxy - miny)
        hit = None
        for _ in range(14):
            mid = (lo + hi) / 2
            idx = fits(mid)
            if idx.size:
                lo, hit = mid, (mid, idx)
            else:
                hi = mid
        if hit and (best is None or hit[0] > best[0]):
            B, idx = hit
            # among fitting positions take the one closest to the region centroid (keeps margins balanced)
            c = rot.centroid
            k = idx[np.argmin((cx[idx] - c.x) ** 2 + (cy[idx] - c.y) ** 2)]
            best = (B, theta, cx[k], cy[k])
    if best is None:
        return None
    B, theta, x, y = best
    B = math.floor(B * 10) / 10
    H = round(aspect * B, 2)
    tri = Polygon([(x - B / 2, y - H / 2), (x + B / 2, y - H / 2), (x, y + H / 2)])
    tri_site = shapely.affinity.rotate(tri, theta, origin=(0, 0))
    center = geo.rotate_pts([(x, y)], theta)[0]
    return PlateFootprint(id="T1", polygon=[(round(a, 3), round(b, 3)) for a, b in geo.ring_of(tri_site)],
                          angle_deg=float(theta), center=(round(center[0], 3), round(center[1], 3)),
                          width=B, depth=H, area=round(tri.area, 2))


_ = (box,)
