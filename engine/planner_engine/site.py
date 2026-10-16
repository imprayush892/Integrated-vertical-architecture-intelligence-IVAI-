"""Stages 2-3 - trace the boundary, then derive setback and buildable area."""

from __future__ import annotations

import shapely
from shapely.geometry import LineString, Point as SPoint, Polygon

from . import geo, rules
from .climate import analyse
from .models import Point, Site


def build_site(
    lnglat: list[Point],
    *,
    name: str = "SITE 01",
    locality: str = "",
    road_width: float = 12.0,
    building_height: float = 24.0,
    road_edges: list[int] | None = None,
    with_climate: bool = True,
    epw: str | None = None,
) -> Site:
    if len(lnglat) < 3:
        raise geo.GeometryError("A plot needs at least 3 corners")
    ll = list(lnglat)
    if ll[0] == ll[-1]:
        ll = ll[:-1]
    frame = geo.make_frame(ll)
    pts = geo.snap(geo.to_site(frame, ll))
    ring = geo.validate_ring(pts)
    # keep lnglat in the same (CCW-in-metres) order as the metric ring
    if geo.shoelace(geo.to_site(frame, ll)) < 0:
        ll = list(reversed(ll))
    ll = geo.to_lnglat(frame, ring)
    area = abs(geo.shoelace(ring))
    edges = geo.edge_lengths(ring)
    site = Site(
        name=name,
        locality=locality,
        lnglat=ll,
        frame=frame,
        polygon=ring,
        area=round(area, 2),
        geodesic_area=round(geo.geodesic_area(ll), 2),
        perimeter=round(sum(edges), 2),
        edges=[round(e, 2) for e in edges],
        road_width=road_width,
        building_height=building_height,
        road_edges=road_edges if road_edges is not None else [0],
    )
    apply_setback(site)
    if with_climate:
        lng = sum(p[0] for p in ll) / len(ll)
        lat = sum(p[1] for p in ll) / len(ll)
        site.climate = analyse(lat, lng, epw=epw)
    return site


def buildable_polygon(ring: list[Point], side: float, front: float, road_edges: list[int]):
    plot = Polygon(ring)
    if abs(side - front) < 1e-9 or not road_edges:
        return plot.buffer(-side, join_style="mitre", mitre_limit=5.0)
    n = len(ring)
    strips = []
    for i in range(n):
        d = front if i in road_edges else side
        seg = LineString([ring[i], ring[(i + 1) % n]])
        strips.append(seg.buffer(d, cap_style="flat", join_style="mitre"))
    for i in range(n):
        d_prev = front if ((i - 1) % n) in road_edges else side
        d_next = front if i in road_edges else side
        strips.append(SPoint(ring[i]).buffer(min(d_prev, d_next), quad_segs=16))
    return plot.difference(shapely.union_all(strips))


def apply_setback(site: Site) -> Site:
    side, front, clause = rules.setback_for(site.building_height, site.road_width)
    b = buildable_polygon(site.polygon, side, front, site.road_edges)
    parts = [p for p in geo.polygons_of(b) if p.area > 25.0]
    site.setback = side
    site.setback_clause = clause + (f"; front {front:.1f} m on road edges" if front != side else "")
    site.buildable = [geo.ring_of(p) for p in parts]
    site.buildable_area = round(sum(p.area for p in parts), 2)
    return site


def buildable_geom(site: Site):
    return shapely.union_all([Polygon(r) for r in site.buildable]) if site.buildable else Polygon()
