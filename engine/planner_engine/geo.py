"""Stage 2 - geometry helpers: projection to a local metric grid and polygon measurement.

Positions are stored in Web Mercator/WGS84 for the map, but every measurement happens in a local
UTM grid (EPSG:326xx / 327xx). For Mumbai that is UTM zone 43N (EPSG:32643).
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from functools import lru_cache

import numpy as np
import shapely
from pyproj import Geod, Proj, Transformer
from shapely.geometry import LinearRing, MultiPolygon, Polygon
from shapely.geometry.base import BaseGeometry

from .models import Point, Ring, SiteFrame

GEOD = Geod(ellps="WGS84")
SNAP = 0.1  # metres


class GeometryError(ValueError):
    """Raised for invalid user geometry (self-crossing, too few points, ...)."""


def utm_epsg(lat: float, lng: float) -> int:
    zone = int(math.floor((lng + 180.0) / 6.0)) % 60 + 1
    return (32600 if lat >= 0 else 32700) + zone


@lru_cache(maxsize=32)
def _transformers(epsg: int) -> tuple[Transformer, Transformer]:
    fwd = Transformer.from_crs(4326, epsg, always_xy=True)
    inv = Transformer.from_crs(epsg, 4326, always_xy=True)
    return fwd, inv


def make_frame(lnglat: Sequence[Point]) -> SiteFrame:
    if not lnglat:
        raise GeometryError("No points")
    lng = sum(p[0] for p in lnglat) / len(lnglat)
    lat = sum(p[1] for p in lnglat) / len(lnglat)
    epsg = utm_epsg(lat, lng)
    fwd, _ = _transformers(epsg)
    e, n = fwd.transform(lng, lat)
    # ground distance = grid distance / point scale factor (~1.0003 near a zone edge)
    k = float(Proj(f"EPSG:{epsg}").get_factors(lng, lat).meridional_scale)
    # round the origin so the site frame is reproducible across sessions
    return SiteFrame(epsg=epsg, origin=(round(e, 1), round(n, 1)), scale=round(k, 9))


def to_site(frame: SiteFrame, lnglat: Iterable[Point]) -> Ring:
    fwd, _ = _transformers(frame.epsg)
    out: Ring = []
    ox, oy = frame.origin
    k = frame.scale
    for lng, lat in lnglat:
        e, n = fwd.transform(lng, lat)
        out.append(((e - ox) / k, (n - oy) / k))
    return out


def to_lnglat(frame: SiteFrame, pts: Iterable[Point]) -> Ring:
    _, inv = _transformers(frame.epsg)
    ox, oy = frame.origin
    k = frame.scale
    out: Ring = []
    for x, y in pts:
        lng, lat = inv.transform(x * k + ox, y * k + oy)
        out.append((round(lng, 8), round(lat, 8)))
    return out


def snap(pts: Iterable[Point], step: float = SNAP) -> Ring:
    return [(round(round(x / step) * step, 6), round(round(y / step) * step, 6)) for x, y in pts]


def shoelace(pts: Sequence[Point]) -> float:
    """Signed area (CCW positive)."""
    n = len(pts)
    s = 0.0
    for i in range(n):
        x1, y1 = pts[i]
        x2, y2 = pts[(i + 1) % n]
        s += x1 * y2 - x2 * y1
    return s / 2.0


def edge_lengths(pts: Sequence[Point]) -> list[float]:
    n = len(pts)
    return [math.dist(pts[i], pts[(i + 1) % n]) for i in range(n)]


def geodesic_area(lnglat: Sequence[Point]) -> float:
    lons = [p[0] for p in lnglat]
    lats = [p[1] for p in lnglat]
    area, _ = GEOD.polygon_area_perimeter(lons, lats)
    return abs(area)


def ccw(pts: Sequence[Point]) -> Ring:
    ring = [(float(x), float(y)) for x, y in pts]
    if len(ring) > 1 and ring[0] == ring[-1]:
        ring = ring[:-1]
    if shoelace(ring) < 0:
        ring.reverse()
    return ring


def validate_ring(pts: Sequence[Point]) -> Ring:
    ring = ccw(pts)
    # drop consecutive duplicates
    cleaned: Ring = []
    for p in ring:
        if not cleaned or math.dist(cleaned[-1], p) > 1e-6:
            cleaned.append(p)
    if len(cleaned) > 1 and math.dist(cleaned[0], cleaned[-1]) < 1e-6:
        cleaned.pop()
    if len(cleaned) < 3:
        raise GeometryError("A plot needs at least 3 corners")
    lr = LinearRing(cleaned)
    if not lr.is_simple:
        raise GeometryError("The boundary crosses itself")
    poly = Polygon(cleaned)
    if not poly.is_valid or poly.area < 1.0:
        raise GeometryError("The boundary is not a valid area")
    return cleaned


def ring_of(geom: Polygon) -> Ring:
    return ccw(list(geom.exterior.coords))


def polygons_of(geom: BaseGeometry | None) -> list[Polygon]:
    if geom is None or geom.is_empty:
        return []
    if isinstance(geom, Polygon):
        return [geom]
    if isinstance(geom, MultiPolygon):
        return list(geom.geoms)
    if hasattr(geom, "geoms"):
        out: list[Polygon] = []
        for g in geom.geoms:
            out.extend(polygons_of(g))
        return out
    return []


def rings_of(geom: BaseGeometry | None, min_area: float = 1e-6) -> list[Ring]:
    return [ring_of(p) for p in polygons_of(geom) if p.area > min_area]


def to_polyrings(geom: BaseGeometry | None, min_area: float = 1e-6, nd: int = 3) -> list[list[Ring]]:
    """Polygon/MultiPolygon -> [[exterior, *holes], ...] (exterior CCW, holes CW)."""
    out = []
    for p in polygons_of(geom):
        if p.area <= min_area:
            continue
        ext = [(round(x, nd), round(y, nd)) for x, y in ccw(list(p.exterior.coords))]
        holes = []
        for h in p.interiors:
            r = ccw(list(h.coords))
            r.reverse()
            holes.append([(round(x, nd), round(y, nd)) for x, y in r])
        out.append([ext, *holes])
    return out


def from_polyrings(parts: Sequence[Sequence[Sequence[Point]]]) -> BaseGeometry:
    polys = [Polygon(p[0], [h for h in p[1:]]) for p in parts if p and len(p[0]) >= 3]
    if not polys:
        return Polygon()
    return polys[0] if len(polys) == 1 else shapely.union_all(polys)


def poly(ring: Sequence[Point]) -> Polygon:
    return Polygon(ring)


def rotate_pts(pts: Iterable[Point], angle_deg: float, origin: Point = (0.0, 0.0)) -> Ring:
    a = math.radians(angle_deg)
    c, s = math.cos(a), math.sin(a)
    ox, oy = origin
    return [(ox + (x - ox) * c - (y - oy) * s, oy + (x - ox) * s + (y - oy) * c) for x, y in pts]


def plate_to_site(pts: Iterable[Point], angle_deg: float, origin: Point) -> Ring:
    a = math.radians(angle_deg)
    c, s = math.cos(a), math.sin(a)
    ox, oy = origin
    return [(ox + x * c - y * s, oy + x * s + y * c) for x, y in pts]


def site_to_plate(pts: Iterable[Point], angle_deg: float, origin: Point) -> Ring:
    a = math.radians(-angle_deg)
    c, s = math.cos(a), math.sin(a)
    ox, oy = origin
    return [((x - ox) * c - (y - oy) * s, (x - ox) * s + (y - oy) * c) for x, y in pts]


def bearing_of_normal(p1: Point, p2: Point) -> float:
    """Compass bearing (deg, clockwise from north) of the outward normal of CCW edge p1->p2."""
    dx, dy = p2[0] - p1[0], p2[1] - p1[1]
    nx, ny = dy, -dx  # right-hand normal is outward for CCW rings
    return (math.degrees(math.atan2(nx, ny)) + 360.0) % 360.0


COMPASS16 = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE", "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]


def compass(bearing: float) -> str:
    return COMPASS16[int(((bearing % 360) + 11.25) // 22.5) % 16]


def angle_diff(a: float, b: float) -> float:
    d = abs((a - b) % 360.0)
    return min(d, 360.0 - d)


def interior_angles(pts: Sequence[Point]) -> list[float]:
    n = len(pts)
    out = []
    for i in range(n):
        p0, p1, p2 = pts[i - 1], pts[i], pts[(i + 1) % n]
        v1 = (p0[0] - p1[0], p0[1] - p1[1])
        v2 = (p2[0] - p1[0], p2[1] - p1[1])
        cross = v1[0] * v2[1] - v1[1] * v2[0]
        dot = v1[0] * v2[0] + v1[1] * v2[1]
        ang = math.degrees(math.atan2(abs(cross), dot))
        # for CCW ring, reflex when the turn is clockwise
        turn = (p1[0] - p0[0]) * (p2[1] - p1[1]) - (p1[1] - p0[1]) * (p2[0] - p1[0])
        out.append(ang if turn >= 0 else 360.0 - ang)
    return out


def setback_estimate(area: float, perimeter: float, d: float, pts: Sequence[Point]) -> float:
    """Closed-form inward-offset area for a convex polygon (A - P*d + d^2 * sum cot(theta/2))."""
    angs = interior_angles(pts)
    return area - perimeter * d + d * d * sum(1.0 / math.tan(math.radians(a) / 2.0) for a in angs)


# ------------------------------------------------------------------------------- rasters & rectangles


def raster_inside(geom: BaseGeometry, res: float, bounds: tuple[float, float, float, float] | None = None):
    """Boolean grid of cells whose full square lies inside ``geom``.

    Returns (mask[rows, cols], x0, y0) where cell (r, c) spans
    [x0 + c*res, x0 + (c+1)*res] x [y0 + r*res, y0 + (r+1)*res].
    """
    minx, miny, maxx, maxy = bounds or geom.bounds
    cols = max(1, int(math.floor((maxx - minx) / res + 1e-9)))
    rows = max(1, int(math.floor((maxy - miny) / res + 1e-9)))
    shrunk = geom.buffer(-res * 0.5 * math.sqrt(2) + 1e-9, join_style="mitre", mitre_limit=10)
    xs = minx + (np.arange(cols) + 0.5) * res
    ys = miny + (np.arange(rows) + 0.5) * res
    gx, gy = np.meshgrid(xs, ys)
    if shrunk.is_empty:
        return np.zeros((rows, cols), dtype=bool), minx, miny
    mask = shapely.contains_xy(shrunk, gx.ravel(), gy.ravel()).reshape(rows, cols)
    return mask, minx, miny


def longest_run(col: np.ndarray) -> tuple[int, int]:
    """Longest run of True in a 1-D bool array -> (start, length)."""
    if not col.any():
        return 0, 0
    padded = np.concatenate(([0], col.view(np.int8), [0]))
    d = np.diff(padded)
    starts = np.flatnonzero(d == 1)
    ends = np.flatnonzero(d == -1)
    lens = ends - starts
    k = int(np.argmax(lens))
    return int(starts[k]), int(lens[k])


def largest_inscribed_rect(geom: BaseGeometry, res: float = 0.25, max_dim: float | None = None,
                           objective: str = "area"):
    """Largest axis-aligned rectangle inside ``geom`` (raster approximation, conservative).

    Returns (minx, miny, maxx, maxy) or None.
    """
    mask, x0, y0 = raster_inside(geom, res)
    rows, cols = mask.shape
    best = (0.0, None)
    heights = np.zeros(cols, dtype=int)
    for r in range(rows):
        heights = np.where(mask[r], heights + 1, 0)
        # largest rectangle in histogram
        stack: list[int] = []
        h = list(heights) + [0]
        for c, hv in enumerate(h):
            start = c
            while stack and h[stack[-1]] >= hv:
                top = stack.pop()
                height = h[top]
                left = stack[-1] + 1 if stack else 0
                width = c - left
                if max_dim is not None:
                    width = min(width, int(max_dim / res))
                    height = min(height, int(max_dim / res))
                area = width * height if objective == "area" else min(width, height) * 1e6 + width * height
                if area > best[0]:
                    best = (area, (left, r - height + 1, left + width, r + 1))
                start = left
            stack.append(c)
            _ = start
    if best[1] is None:
        return None
    c0, r0, c1, r1 = best[1]
    return (x0 + c0 * res, y0 + r0 * res, x0 + c1 * res, y0 + r1 * res)


def clear_width(geom: BaseGeometry) -> float:
    """Clear minimum width of a room: short side of its largest inscribed axis-aligned rectangle.

    Exact for rectilinear rooms (the optimal rectangle has its edges on vertex coordinates, so those are
    enumerated); other shapes fall back to a raster search, which can read up to ``bbox/80`` low.
    """
    if geom.is_empty:
        return 0.0
    minx, miny, maxx, maxy = geom.bounds
    if abs(geom.area - (maxx - minx) * (maxy - miny)) < 1e-6:
        return round(min(maxx - minx, maxy - miny), 3)
    if isinstance(geom, Polygon) and not geom.interiors and is_rectilinear(list(geom.exterior.coords)[:-1]):
        xs = sorted({round(c[0], 6) for c in geom.exterior.coords})
        ys = sorted({round(c[1], 6) for c in geom.exterior.coords})
        if len(xs) <= 14 and len(ys) <= 14:
            grown = geom.buffer(1e-6, join_style="mitre")
            best = 0.0
            for i, x0 in enumerate(xs):
                for x1 in xs[i + 1:]:
                    for j, y0 in enumerate(ys):
                        for y1 in ys[j + 1:]:
                            side = min(x1 - x0, y1 - y0)
                            if side > best + 1e-9 and grown.contains(shapely.box(x0, y0, x1, y1)):
                                best = side
            return round(best, 3)
    res = max(0.02, min(maxx - minx, maxy - miny) / 80.0)
    r = largest_inscribed_rect(geom, res=res, objective="minside")
    if r is None:
        return 0.0
    return round(min(r[2] - r[0], r[3] - r[1]), 3)


def is_rectilinear(ring: Sequence[Point], tol: float = 1e-6) -> bool:
    n = len(ring)
    for i in range(n):
        (x1, y1), (x2, y2) = ring[i], ring[(i + 1) % n]
        if abs(x1 - x2) > tol and abs(y1 - y2) > tol:
            return False
    return True


def rectilinearize(geom: Polygon, res: float = 0.5) -> Polygon:
    """Inner rectilinear (staircase) approximation of a polygon on a ``res`` grid."""
    if is_rectilinear(ring_of(geom)):
        return geom
    mask, x0, y0 = raster_inside(geom, res)
    boxes = []
    rows, cols = mask.shape
    for r in range(rows):
        c = 0
        while c < cols:
            if mask[r, c]:
                c1 = c
                while c1 < cols and mask[r, c1]:
                    c1 += 1
                boxes.append(shapely.box(x0 + c * res, y0 + r * res, x0 + c1 * res, y0 + (r + 1) * res))
                c = c1
            else:
                c += 1
    if not boxes:
        return Polygon()
    u = shapely.union_all(boxes)
    parts = polygons_of(u)
    return max(parts, key=lambda p: p.area) if parts else Polygon()


def rect_decompose(geom: BaseGeometry) -> list[tuple[float, float, float, float]]:
    """Exact decomposition of a rectilinear (multi)polygon into axis-aligned boxes.

    Uses coordinate compression: cells between unique vertex x/y lines, merged into horizontal runs,
    then vertically identical runs merged.
    """
    if geom.is_empty:
        return []
    xs: set[float] = set()
    ys: set[float] = set()
    for p in polygons_of(geom):
        for ring in [p.exterior, *p.interiors]:
            for x, y in ring.coords:
                xs.add(round(x, 6))
                ys.add(round(y, 6))
    X = sorted(xs)
    Y = sorted(ys)
    if len(X) < 2 or len(Y) < 2:
        return []
    cx = [(X[i] + X[i + 1]) / 2 for i in range(len(X) - 1)]
    cy = [(Y[j] + Y[j + 1]) / 2 for j in range(len(Y) - 1)]
    gx, gy = np.meshgrid(cx, cy)
    inside = shapely.contains_xy(geom, gx.ravel(), gy.ravel()).reshape(len(cy), len(cx))
    runs: dict[tuple[int, int], list[int]] = {}
    for j in range(len(cy)):
        i = 0
        while i < len(cx):
            if inside[j, i]:
                i1 = i
                while i1 < len(cx) and inside[j, i1]:
                    i1 += 1
                runs.setdefault((i, i1), []).append(j)
                i = i1
            else:
                i += 1
    boxes = []
    for (i0, i1), js in runs.items():
        js.sort()
        start = prev = js[0]
        for j in js[1:] + [None]:  # type: ignore[list-item]
            if j is not None and j == prev + 1:
                prev = j
                continue
            boxes.append((X[i0], Y[start], X[i1], Y[prev + 1]))
            if j is not None:
                start = prev = j
    return boxes
