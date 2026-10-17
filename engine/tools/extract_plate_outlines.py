"""Building-outline (typical-floor footprint) extraction from vector floor-plan PDFs.

Reuses the wall-mask helpers of extract_pdf_topology.py (imported, not edited). Per page:
  1. vector strokes -> orthogonal segments (page rotated so the drawing is axis aligned),
  2. wall mask = paired parallel lines 0.07-0.36 m apart (same as the topology extractor),
  3. mask -> closed (bridges door/window gaps), holes filled, connected components,
  4. every component with a plausible footprint area is returned as a shapely polygon in metres
     (y up, origin at the component bbox corner), plus the unclean "raw" area for diagnostics.
The outline is the *outer* contour of each component (holes are kept separately, they are courtyards
or shafts and the typology classifier decides which).

Usage:  python -I extract_plate_outlines.py <pdf> <page> <scale_m_per_pt> > json
        (library use: outlines_for_page(pdf, page, scale))
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("OMP_THREAD_LIMIT", "1")
sys.path.insert(0, str(Path(__file__).resolve().parent))

import extract_pdf_topology as T  # noqa: E402
import numpy as np  # noqa: E402
from scipy import ndimage as ndi  # noqa: E402
from shapely.geometry import Polygon  # noqa: E402

PX_M = 0.05  # raster pitch, metres


def wall_raster(pdf: str, pno: int, scale: float):
    """(wall mask bool array, pitch m, origin offset) for one page, in the drawing's own axes."""
    import pymupdf

    doc = pymupdf.open(pdf)
    page = doc[pno]
    drawings = page.get_drawings()
    segs, _pieces = T.load_vectors(drawings)
    del drawings
    theta = T.dominant_angle(segs) if len(segs) > 30 else 0.0
    cen = (page.rect.width / 2, page.rect.height / 2)
    if theta:
        segs = np.concatenate([T.rotate_pts(segs[:, :2], theta, cen), T.rotate_pts(segs[:, 2:], theta, cen)], 1)
    osegs = T.orth_segments(segs)
    rho = scale and (1.0 / (PX_M / scale))  # px per point
    x0, y0 = float(osegs[:, [0, 2]].min()), float(osegs[:, [1, 3]].min())
    x1, y1 = float(osegs[:, [0, 2]].max()), float(osegs[:, [1, 3]].max())
    g = T.Grid(x0, y0, x1, y1, rho)
    walls = T.wall_mask(g, osegs, scale)
    frame = {"x0": float(g.x0), "y0": float(g.y0), "rho": float(rho), "h_px": int(g.h), "theta": float(theta),
             "cen": [float(cen[0]), float(cen[1])], "scale": float(scale), "px_m": PX_M}
    return walls, g, frame


def components(walls: np.ndarray, close_m: float = 0.5, min_area_m2: float = 120.0, max_area_m2: float = 6000.0):
    k = max(3, int(round(close_m / PX_M)))
    m = ndi.binary_closing(walls, structure=np.ones((k, k), bool))
    filled = ndi.binary_fill_holes(m)
    lab, n = ndi.label(filled)
    out = []
    px2 = PX_M * PX_M
    for i in range(1, n + 1):
        comp = lab == i
        a = comp.sum() * px2
        if a < min_area_m2 or a > max_area_m2:
            continue
        out.append((a, comp))
    out.sort(key=lambda t: -t[0])
    return out


def mask_to_polygon(comp: np.ndarray, simplify_m: float = 0.15) -> Polygon:
    """Outer contour (with holes) of a boolean mask as a polygon in metres, y up."""
    import contourpy

    h, w = comp.shape
    pad = np.pad(comp.astype(float), 1)
    gen = contourpy.contour_generator(z=pad, line_type=contourpy.LineType.Separate)
    paths = [seg for seg in gen.lines(0.5) if len(seg) >= 4]
    polys = []
    for seg in paths:
        pts = [((x - 1) * PX_M, (h - (y - 1)) * PX_M) for x, y in seg]
        p = Polygon(pts)
        if p.is_valid and p.area > 1:
            polys.append(p)
        elif p.area > 1:
            polys.append(p.buffer(0))
    if not polys:
        raise ValueError("no contour")
    polys.sort(key=lambda p: -p.area)
    outer = polys[0]
    holes = [p for p in polys[1:] if outer.contains(p.representative_point())]
    poly = Polygon(outer.exterior, [h_.exterior for h_ in holes])
    return poly.simplify(simplify_m, preserve_topology=True)


def outlines_for_page(pdf: str, pno: int, scale: float, **kw):
    walls, g, frame = wall_raster(pdf, pno, scale)
    res = []
    for a, comp in components(walls, **kw):
        try:
            res.append((a, mask_to_polygon(comp)))
        except ValueError:
            continue
    return res, frame


def to_page_pts(xy, frame):
    """Outline coordinates (metres, plan frame) -> original PDF page points (for overlays and label registration)."""
    a = np.asarray(xy, dtype=float)
    px = a[..., 0] / frame["px_m"]
    py = frame["h_px"] - a[..., 1] / frame["px_m"]
    pts = np.stack([px / frame["rho"] + frame["x0"], py / frame["rho"] + frame["y0"]], axis=-1)
    if frame["theta"]:
        pts = T.rotate_pts(pts, -frame["theta"], frame["cen"])
    return pts


def page_pts_to_plan(pts, frame):
    """Inverse of ``to_page_pts``: PDF page points -> plan-frame metres."""
    a = np.asarray(pts, dtype=float)
    if frame["theta"]:
        a = T.rotate_pts(a, frame["theta"], frame["cen"])
    px = (a[..., 0] - frame["x0"]) * frame["rho"]
    py = (a[..., 1] - frame["y0"]) * frame["rho"]
    return np.stack([px * frame["px_m"], (frame["h_px"] - py) * frame["px_m"]], axis=-1)
