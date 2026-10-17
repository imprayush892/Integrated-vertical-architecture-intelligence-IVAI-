"""Plate typology: classify a building outline into an architect's bucket by *approximation*.

Buckets: ``linear_slab, L, T, H, cruciform, Y, point_square, courtyard, other``.

Why not exact shape matching: an approved typical floor never has the textbook outline. Balconies, chajjas, ducts,
light-wells, stepped corners and slightly different wing lengths are everywhere. So the classifier

1. *rectilinearises* the outline: a morphological opening (``open_r``) removes projections thinner than 4 m (balconies,
   ducts, chajjas) and a closing (``close_r``) fills slots thinner than 5 m (light wells); both with a mitre limit so
   no spikes appear; then it rotates the plate onto its dominant axes (robust histogram peak, not a mean);
2. measures robust descriptors (aspect, rectangularity, convexity, orthogonality, symmetry, direction families);
3. for a rectilinear plate *fits the textbook plans by area agreement*: a plan is the bounding box minus rectangular
   notches (corner notches make L / T / cross / Z, mid-side notches make U / H / S / comb). The best notch rectangles
   are found exactly with 2-D prefix sums on a 0.5 m raster, every notch must be big enough to be a wing (>= 4 m and
   18 % of the short side, 7 % of the bbox area, plus a per-family relative size rule) and every notch costs a
   complexity penalty, so a simpler plan wins unless a notch pays for itself. The winner is the label, the margin to
   the runner-up of another family and the IoU of the fit are the confidence;
4. an oblique plate with three edge-direction families 60 degrees apart is a Y when it is non-convex and a
   triangle (experimental, parked) when convex; an enclosed hole of >= 40 m2 makes a courtyard.

Everything returns plain dataclasses with the descriptors, the wings and the reasons, so the UI can explain it. The
thresholds are in ``CFG``; they were tuned against the 50 approved projects and checked on a blind hold-out (see
docs/quality/typology-cores.md for the measured agreement and the limits).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

import numpy as np
from shapely import affinity
from shapely.geometry import Polygon, box
from shapely.ops import unary_union

TYPES = ("linear_slab", "L", "T", "H", "cruciform", "Y", "point_square", "courtyard", "other")

CFG: dict[str, float] = {
    "open_r": 2.0,  # m: projections thinner than 2*open_r vanish (balconies, ducts, chajjas)
    "close_r": 2.5,  # m: slots thinner than 2*close_r are filled (light wells, narrow notches)
    "orth_min": 0.72,  # share of boundary length on the two dominant axes for "rectilinear"
    "slab_aspect": 1.9,  # long/short of the bounding box for a linear slab
    "notch_min_dim": 4.0,  # m: smallest side of a notch
    "notch_min_rel": 0.18,  # ... or this fraction of the short bbox side
    "rel_L": 0.25,  # an L notch spans at least this share of both bbox sides (else it is a stepped slab)
    "rel_T": 0.25,  # a T stem is at least this share of the bbox side it runs along
    "rel_Tw": 0.18,  # a T stem is at least this share of the bar length (else it is a comb with a short tooth)
    "rel_U": 0.25,  # a U notch is at least this deep relative to the bbox side it cuts
    "rel_H": 0.30,  # the two H notches together take at least this share of the bbox side
    "notch_min_gain": 0.07,  # a notch must improve the template fit by this share of the bbox area
    "notch_penalty": 0.03,  # complexity cost of every extra notch (share of the bbox area)
    "wing_min_w": 4.0,  # m: narrowest wing / stem the fit may leave
    "hole_min_area": 40.0,  # m2: interior hole that makes a courtyard
    "hole_min_width": 4.0,  # m
}


@dataclass
class Notch:
    """A part of the bounding box that the plate does not occupy and that is big enough to matter."""

    polygon: Polygon
    sides: tuple[str, ...]  # bbox sides it touches among W,E,S,N
    kind: str  # "corner" | "side" | "inner"
    width: float  # x extent
    depth: float  # y extent
    area: float
    centre_rel: tuple[float, float]  # centre in bbox-relative (0..1) coordinates

    @property
    def ew(self) -> float:
        """Width of the area-equivalent rectangle (a stepped notch is thinner than its bounding box)."""
        return self.width * math.sqrt(self.area / max(1e-6, self.width * self.depth))

    @property
    def ed(self) -> float:
        return self.depth * math.sqrt(self.area / max(1e-6, self.width * self.depth))


@dataclass
class Wing:
    kind: str  # bar | stem | arm
    length: float
    width: float

    @property
    def ratio(self) -> float:
        return self.length / self.width if self.width > 1e-6 else 0.0


@dataclass
class TypologyResult:
    label: str
    subtype: str = ""
    confidence: float = 0.0
    descriptors: dict[str, Any] = field(default_factory=dict)
    wings: list[Wing] = field(default_factory=list)
    notches: list[Notch] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    clean: Polygon | None = None  # rectilinearised outline, plate frame rotated onto its axes
    angle_deg: float = 0.0  # rotation applied to get ``clean`` from the input
    experimental: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "label": self.label, "subtype": self.subtype, "confidence": round(self.confidence, 2),
            "experimental": self.experimental, "descriptors": self.descriptors,
            "wings": [{"kind": w.kind, "length": round(w.length, 1), "width": round(w.width, 1)} for w in self.wings],
            "notches": [{"sides": "".join(n.sides), "kind": n.kind, "width": round(n.width, 1),
                         "depth": round(n.depth, 1), "area": round(n.area, 1)} for n in self.notches],
            "reasons": self.reasons, "angle_deg": round(self.angle_deg, 1),
        }


# --------------------------------------------------------------------------------------------------
# geometry helpers
# --------------------------------------------------------------------------------------------------


def _largest(g) -> Polygon:
    if isinstance(g, Polygon):
        return g
    polys = [p for p in getattr(g, "geoms", []) if isinstance(p, Polygon)]
    return max(polys, key=lambda p: p.area) if polys else Polygon()


def _mitre_buffer(g, d: float):
    return g.buffer(d, join_style=2, mitre_limit=1.45)


def rectilinearise(poly: Polygon, open_r: float | None = None, close_r: float | None = None) -> Polygon:
    """Opening then closing with a mitred buffer; interior holes are ignored (see ``holes_of``)."""
    open_r = CFG["open_r"] if open_r is None else open_r
    close_r = CFG["close_r"] if close_r is None else close_r
    q = Polygon(poly.exterior).buffer(0)
    q = _largest(q)
    r = open_r
    while r > 0.05:
        o = _largest(_mitre_buffer(_mitre_buffer(q, -r), r))
        if o.area > 0.5 * q.area:
            q = o
            break
        r *= 0.5
    c = _largest(_mitre_buffer(_mitre_buffer(q, close_r), -close_r))
    if c.area >= q.area * 0.98:
        q = c
    return Polygon(q.exterior).simplify(0.08, preserve_topology=True)


def dominant_axis_deg(poly: Polygon) -> float:
    """Dominant edge direction modulo 90 degrees: length-weighted histogram peak, refined by the mean of the
    edges within 3 degrees of it (robust to a few skewed walls, unlike a plain mean)."""
    xy = np.asarray(poly.exterior.coords)
    d = np.diff(xy, axis=0)
    ln = np.hypot(d[:, 0], d[:, 1])
    ok = ln > 0.2
    if not ok.any():
        return 0.0
    ang = np.degrees(np.arctan2(d[ok, 1], d[ok, 0])) % 90.0
    w = ln[ok]
    hist, edges = np.histogram(ang, bins=90, range=(0, 90), weights=w)
    sm = hist + np.roll(hist, 1) + np.roll(hist, -1)  # circular on the 90-degree period
    k = int(np.argmax(sm))
    c = (edges[k] + edges[k + 1]) / 2
    diff = (ang - c + 45) % 90 - 45
    sel = np.abs(diff) <= 3.0
    return float((c + np.average(diff[sel], weights=w[sel])) % 90.0) if sel.any() else float(c % 90.0)


def orthogonality(poly: Polygon, tol_deg: float = 8.0) -> float:
    """Share of the boundary length lying on the two dominant (perpendicular) axes."""
    a0 = dominant_axis_deg(poly)
    xy = np.asarray(poly.exterior.coords)
    d = np.diff(xy, axis=0)
    ln = np.hypot(d[:, 0], d[:, 1])
    ang = (np.degrees(np.arctan2(d[:, 1], d[:, 0])) - a0) % 90.0
    off = np.minimum(ang, 90.0 - ang)
    tot = ln.sum()
    return float(ln[off <= tol_deg].sum() / tot) if tot > 0 else 0.0


def direction_families(poly: Polygon, bin_deg: float = 5.0) -> list[tuple[float, float]]:
    """Boundary direction histogram modulo 180: [(angle_deg, share)] of peaks holding >= 8 % of length."""
    xy = np.asarray(poly.exterior.coords)
    d = np.diff(xy, axis=0)
    ln = np.hypot(d[:, 0], d[:, 1])
    ang = np.degrees(np.arctan2(d[:, 1], d[:, 0])) % 180.0
    nb = int(180 / bin_deg)
    hist = np.zeros(nb)
    for a, l_ in zip(ang, ln, strict=True):
        hist[int(a // bin_deg) % nb] += l_
    # smooth by a +-1 bin window (circular)
    sm = hist + np.roll(hist, 1) + np.roll(hist, -1)
    tot = hist.sum()
    peaks: list[tuple[float, float]] = []
    used = np.zeros(nb, bool)
    for k in np.argsort(-sm):
        if used[k] or sm[k] < 0.08 * tot:
            continue
        lo = [(k + j) % nb for j in range(-2, 3)]
        used[lo] = True
        # weighted mean angle of the window
        ws = np.array([hist[i] for i in lo])
        angs = np.array([((k + j) % nb + 0.5) * bin_deg for j in range(-2, 3)])
        # unwrap around k
        base = (k + 0.5) * bin_deg
        angs = base + ((angs - base + 90) % 180 - 90)
        peaks.append((float(np.average(angs, weights=ws) % 180.0), float(ws.sum() / tot)))
    return peaks


def holes_of(poly: Polygon) -> list[Polygon]:
    out = []
    for ring in poly.interiors:
        h = Polygon(ring)
        if h.area >= CFG["hole_min_area"] and not h.buffer(-CFG["hole_min_width"] / 2).is_empty:
            out.append(h)
    return out


def _mirror_iou(p: Polygon, axis: str) -> float:
    minx, miny, maxx, maxy = p.bounds
    m = affinity.scale(p, -1 if axis == "v" else 1, 1 if axis == "v" else -1,
                       origin=((minx + maxx) / 2, (miny + maxy) / 2))
    u = p.union(m).area
    return p.intersection(m).area / u if u > 0 else 0.0


_RES = 0.5  # m, raster pitch of the template fit
_CORNERS = ("SW", "SE", "NW", "NE")
_SIDES = ("S", "N", "W", "E")


@dataclass
class _Fit:
    name: str
    notches: list  # [(kind, sides, polygon)]
    gain: float  # sum of (empty - filled) cells over the notch rectangles / bbox cells
    score: float = 0.0


def _gain_tables(q: Polygon):
    """Raster of the bbox of q (+1 empty cell, -1 cell inside q) and the prefix-sum gain of every corner-anchored
    rectangle: ``gain = (#empty - #filled)`` inside the rectangle, i.e. how much cutting it out improves the fit."""
    import shapely

    minx, miny, maxx, maxy = q.bounds
    nx, ny = int(math.ceil((maxx - minx) / _RES)), int(math.ceil((maxy - miny) / _RES))
    xs = minx + (np.arange(nx) + 0.5) * _RES
    ys = miny + (np.arange(ny) + 0.5) * _RES
    gx, gy = np.meshgrid(xs, ys)
    inside = shapely.contains_xy(q, gx, gy)
    wt = np.where(inside, -1, 1).astype(np.int32)  # rows = y up, cols = x
    return wt, (minx, miny, maxx, maxy), nx, ny


def _corner_G(wt: np.ndarray, corner: str) -> np.ndarray:
    a = wt
    if "N" in corner:
        a = a[::-1]
    if "E" in corner:
        a = a[:, ::-1]
    return np.cumsum(np.cumsum(a, axis=0), axis=1)  # G[d-1, w-1]


def _corner_rect(corner: str, w: int, d: int, bb) -> Polygon:
    minx, miny, maxx, maxy = bb
    x0, x1 = (minx, minx + w * _RES) if "W" in corner else (maxx - w * _RES, maxx)
    y0, y1 = (miny, miny + d * _RES) if "S" in corner else (maxy - d * _RES, maxy)
    return box(x0, y0, x1, y1)


def _side_best(wt: np.ndarray, side: str, dm: int, wm: int, margin: int, dmax: int):
    """Best rectangle touching ``side`` away from its ends: returns per-depth arrays (gain, a, b) for depth 1..dmax."""
    b = wt
    if side == "N":
        b = b[::-1]
    elif side == "W":
        b = b.T
    elif side == "E":
        b = b.T[::-1]
    nd, n = b.shape
    cs = np.cumsum(b, axis=0)
    gains = np.full(dmax + 1, -1e9)
    spans = [(0, 0)] * (dmax + 1)
    for d in range(dm, min(dmax, nd) + 1):
        p = np.concatenate([[0], np.cumsum(cs[d - 1])])
        lo, hi = margin, n - margin
        if hi - lo < wm:
            continue
        seg = p[lo:hi + 1]
        diff = seg[None, :] - seg[:, None]  # [a, b] = p[b] - p[a]
        ii, jj = np.indices(diff.shape)
        diff = np.where(jj - ii >= wm, diff, -10**9)
        k = int(np.argmax(diff))
        ai, bj = divmod(k, diff.shape[1])
        gains[d] = diff[ai, bj]
        spans[d] = (lo + ai, lo + bj)
    return gains, spans


def _side_rect(side: str, a: int, b: int, d: int, bb) -> Polygon:
    minx, miny, maxx, maxy = bb
    if side in "SN":
        x0, x1 = minx + a * _RES, minx + b * _RES
        y0, y1 = (miny, miny + d * _RES) if side == "S" else (maxy - d * _RES, maxy)
    else:
        y0, y1 = miny + a * _RES, miny + b * _RES
        x0, x1 = (minx, minx + d * _RES) if side == "W" else (maxx - d * _RES, maxx)
    return box(x0, y0, x1, y1)


def _side_runs(wt: np.ndarray, side: str, wm: int, margin: int, dmax: int) -> list[tuple[int, int, int, float]]:
    """Separate mid-side notches along one side: runs of columns whose empty depth from the side is >= ``wm`` cells
    (and >= 3 m wide), not touching the ends. Returns (start, end, depth, gain) in cells."""
    b = wt
    if side == "N":
        b = b[::-1]
    elif side == "W":
        b = b.T
    elif side == "E":
        b = b.T[::-1]
    nd, n = b.shape
    h = np.zeros(n, int)
    for j in range(n):
        col = b[:, j]
        h[j] = int(np.argmin(col > 0)) if not (col > 0).all() else nd
    ok = h >= wm
    runs = []
    j = 0
    while j < n:
        if ok[j]:
            k = j
            while k < n and ok[k]:
                k += 1
            if j >= margin and k <= n - margin and (k - j) * _RES >= 3.0:
                d = min(int(np.percentile(h[j:k], 25)), dmax)
                if d >= wm:
                    g = float(b[:d, j:k].sum())
                    if g >= CFG["notch_min_gain"] * 0.5 * wt.size:
                        runs.append((j, k, d, g))
            j = k
        else:
            j += 1
    return runs


def fit_templates(q: Polygon) -> tuple[list[_Fit], dict[str, Any]]:
    """Fit the textbook plans to the outline by area agreement and return every candidate ranked best first.

    A plan is the bounding box minus rectangular notches (corner notches for L/T/cross/Z, mid-side notches for H/U);
    the best notch rectangles are found exactly with 2-D prefix sums on a 0.5 m raster, so wavy walls, balconies
    and skewed edges only change the score a little. Each notch must be at least ``max(4 m, 20 % of the short bbox
    side)`` each way, must pay for itself (``notch_min_gain`` of the bbox area) and leave wings at least
    ``wing_min_w`` thick; every notch costs ``notch_penalty`` so a simpler plan wins unless a notch is worth it."""
    wt, bb, nx, ny = _gain_tables(q)
    minx, miny, maxx, maxy = bb
    cells = float(nx * ny)
    short = min(nx, ny) * _RES
    wm = max(1, int(math.ceil(max(CFG["notch_min_dim"], CFG["notch_min_rel"] * short) / _RES)))
    stem = int(math.ceil(CFG["wing_min_w"] / _RES))
    min_gain = CFG["notch_min_gain"] * cells
    pen = CFG["notch_penalty"]
    G = {c: _corner_G(wt, c) for c in _CORNERS}
    fits: list[_Fit] = [_Fit("none", [], 0.0, 0.0)]

    def corner_best(c, wmax, dmax, relax=1.0):
        g = G[c][wm - 1:dmax, wm - 1:wmax]
        if g.size == 0:
            return None
        k = int(np.argmax(g))
        di, wi = divmod(k, g.shape[1])
        val = float(g[di, wi])
        if val < min_gain * relax:
            return None
        return val, wi + wm, di + wm

    def add(name, parts, gain):
        fits.append(_Fit(name, parts, gain / cells, gain / cells - pen * len(parts)))

    wcap, dcap = nx - stem, ny - stem
    # --- one corner: L
    for c in _CORNERS:
        r = corner_best(c, wcap, dcap)
        if r and r[1] / nx >= CFG["rel_L"] and r[2] / ny >= CFG["rel_L"]:
            val, w, d = r
            add(f"L:{c}", [("corner", tuple(c), _corner_rect(c, w, d, bb))], val)
    # --- two corners on one side: T (the shared side carries the stem), coupled along that side
    for ca, cb, axis in (("SW", "SE", "x"), ("NW", "NE", "x"), ("SW", "NW", "y"), ("SE", "NE", "y")):
        cap_main = wcap if axis == "x" else dcap
        cap_other = dcap if axis == "x" else wcap
        ga, gb = G[ca], G[cb]

        def prof(g, axis=axis, cap_other=cap_other):
            if axis == "x":  # best over depth for each width
                sub = g[wm - 1:cap_other, :]
                best = sub.max(axis=0) if sub.size else np.full(g.shape[1], -1e9)
                arg = sub.argmax(axis=0) + wm if sub.size else np.zeros(g.shape[1], int)
            else:
                sub = g[:, wm - 1:cap_other]
                best = sub.max(axis=1) if sub.size else np.full(g.shape[0], -1e9)
                arg = sub.argmax(axis=1) + wm if sub.size else np.zeros(g.shape[0], int)
            return best, arg

        pa, aa = prof(ga)
        pb, ab = prof(gb)
        n_main = len(pa)
        best = (-1e9, 0, 0)
        for ia in range(wm - 1, min(cap_main, n_main)):
            lim = cap_main - (ia + 1)  # width left for b so the stem keeps >= wing_min_w
            if lim < wm:
                continue
            jb = pb[wm - 1:lim]
            if jb.size == 0:
                continue
            k = int(np.argmax(jb)) + wm - 1
            tot = pa[ia] + pb[k]
            if pa[ia] >= min_gain and pb[k] >= min_gain and tot > best[0]:
                best = (float(tot), ia + 1, k + 1)
        if best[0] > -1e8:
            tot, la, lb = best
            if axis == "x":
                da_, db_ = int(aa[la - 1]), int(ab[lb - 1])
                ra = _corner_rect(ca, la, da_, bb)
                rb = _corner_rect(cb, lb, db_, bb)
                stem_ok = max(da_, db_) / ny >= CFG["rel_T"] and (nx - la - lb) / nx >= CFG["rel_Tw"]
            else:
                wa_, wb_ = int(aa[la - 1]), int(ab[lb - 1])
                ra = _corner_rect(ca, wa_, la, bb)
                rb = _corner_rect(cb, wb_, lb, bb)
                stem_ok = max(wa_, wb_) / nx >= CFG["rel_T"] and (ny - la - lb) / ny >= CFG["rel_Tw"]
            if stem_ok:
                add(f"T:{ca}{cb}", [("corner", tuple(ca), ra), ("corner", tuple(cb), rb)], tot)
    # --- corners with half caps: Z (opposite), 3 corners, cross (4 corners)
    half_w, half_d = max(wm, (nx - stem) // 2), max(wm, (ny - stem) // 2)
    rs = {c: corner_best(c, half_w + 1, half_d + 1) for c in _CORNERS}
    # three or four corner notches are one family (a cross is often drawn with one weak corner): once two corners pay
    # their way in full, the others only need half the gain
    n_full = sum(1 for r in rs.values() if r)
    rl = {c: (rs[c] or corner_best(c, half_w + 1, half_d + 1, 0.5)) for c in _CORNERS} if n_full >= 2 else rs
    avail = {c: r for c, r in rl.items() if r and r[1] / nx >= 0.15 and r[2] / ny >= 0.15}
    for ca, cb in (("SW", "NE"), ("SE", "NW")):
        ra_, rb_ = rs[ca], rs[cb]
        if ra_ and rb_ and min(ra_[1] / nx, rb_[1] / nx) >= 0.18 and min(ra_[2] / ny, rb_[2] / ny) >= 0.18:
            ra = _corner_rect(ca, ra_[1], ra_[2], bb)
            rb = _corner_rect(cb, rb_[1], rb_[2], bb)
            if not ra.intersects(rb) or ra.intersection(rb).area < 1e-6:
                add(f"Z:{ca}{cb}", [("corner", tuple(ca), ra), ("corner", tuple(cb), rb)], ra_[0] + rb_[0])
    import itertools

    for k in (3, 4):
        for combo in itertools.combinations(_CORNERS, k):
            if all(c in avail for c in combo):
                parts = [("corner", tuple(c), _corner_rect(c, avail[c][1], avail[c][2], bb)) for c in combo]
                polys = [p[2] for p in parts]
                if any(polys[i].intersection(polys[j]).area > 1e-6 for i in range(k) for j in range(i + 1, k)):
                    continue
                add(("cross" if k == 4 else "tri") + ":" + "".join(combo), parts, sum(avail[c][0] for c in combo))
    # --- mid-side notches: U (one) and H (two facing)
    margin = max(1, int(math.ceil(max(2.0, 0.08 * max(nx, ny) * _RES) / _RES)))
    sb = {}
    for sd in _SIDES:
        dmax = (ny if sd in "SN" else nx) - stem
        n_along = nx if sd in "SN" else ny
        if dmax >= wm and n_along > 2 * margin + wm:
            sb[sd] = (_side_best(wt, sd, wm, wm, margin, dmax), dmax)
    for sd, ((gains, spans), _dmax) in sb.items():
        d = int(np.argmax(gains))
        if gains[d] >= min_gain and d / (ny if sd in "SN" else nx) >= CFG["rel_U"]:
            a, b = spans[d]
            add(f"U:{sd}", [("side", (sd,), _side_rect(sd, a, b, d, bb))], float(gains[d]))
    for sa, sb_ in (("S", "N"), ("W", "E")):
        if sa in sb and sb_ in sb:
            (ga, spa), dmax_a = sb[sa]
            (gb, spb), dmax_b = sb[sb_]
            tot_len = ny if sa == "S" else nx
            for facing in (True, False):  # H (notches face each other) / S (they are offset)
                best = (-1e9, 0, 0)
                for da in range(wm, len(ga)):
                    if ga[da] < min_gain:
                        continue
                    lim = tot_len - da - stem
                    if lim < wm:
                        continue
                    gb_ = gb[wm:min(lim, len(gb) - 1) + 1]
                    if gb_.size == 0:
                        continue
                    k = int(np.argmax(gb_)) + wm
                    if gb[k] < min_gain:
                        continue
                    (a0, a1), (b0, b1) = spa[da], spb[k]
                    ov = max(0, min(a1, b1) - max(a0, b0))
                    if (ov >= 0.5 * min(a1 - a0, b1 - b0)) != facing:
                        continue
                    if facing and (da + k) / tot_len < CFG["rel_H"]:
                        continue
                    if ga[da] + gb[k] > best[0]:
                        best = (float(ga[da] + gb[k]), da, k)
                if best[0] > -1e8:
                    tot, da, k = best
                    (a0, a1), (b0, b1) = spa[da], spb[k]
                    add(f"{'H' if facing else 'S'}:{sa}{sb_}", [("side", (sa,), _side_rect(sa, a0, a1, da, bb)),
                                                              ("side", (sb_,), _side_rect(sb_, b0, b1, k, bb))], tot)
    # --- comb / E: two or more separate mid-side notches on one side (a slab with projecting wings)
    for sd in _SIDES:
        runs = _side_runs(wt, sd, wm, margin, (ny if sd in "SN" else nx) - stem)
        if len(runs) >= 2:
            parts = [("side", (sd,), _side_rect(sd, a, b, d, bb)) for a, b, d, _g in runs]
            add(f"comb:{sd}", parts, sum(g for *_x, g in runs))
    fits.sort(key=lambda f: (-f.score, len(f.notches)))
    return fits, {"bbox": bb, "nx": nx, "ny": ny, "cells": cells}


def _find_notches(q: Polygon) -> list[Notch]:
    """Notches of the best-fitting template (kept for the diagnostics / drawings)."""
    fits, info = fit_templates(q)
    return _notches_of(fits[0], info["bbox"])


def _notches_of(fit: _Fit, bb) -> list[Notch]:
    minx, miny, maxx, maxy = bb
    bw, bh = maxx - minx, maxy - miny
    out = []
    for kind, sides, poly in fit.notches:
        x0, y0, x1, y1 = poly.bounds
        c = poly.centroid
        out.append(Notch(poly, sides, kind, x1 - x0, y1 - y0, poly.area, ((c.x - minx) / bw, (c.y - miny) / bh)))
    return out

# --------------------------------------------------------------------------------------------------
# descriptors
# --------------------------------------------------------------------------------------------------


def describe(poly: Polygon, clean: Polygon | None = None) -> dict[str, Any]:
    """Robust shape descriptors of an outline (cleaned if ``clean`` is not given)."""
    q = clean if clean is not None else rectilinearise(poly)
    a0 = dominant_axis_deg(q)
    qr = affinity.rotate(q, -a0, origin=q.centroid)
    minx, miny, maxx, maxy = qr.bounds
    bw, bh = maxx - minx, maxy - miny
    mrr = q.minimum_rotated_rectangle
    hull = q.convex_hull
    fam = direction_families(q)
    d = {
        "area_m2": round(q.area, 1),
        "raw_area_m2": round(Polygon(poly.exterior).area, 1),
        "bbox_m": [round(max(bw, bh), 1), round(min(bw, bh), 1)],
        "aspect": round(max(bw, bh) / max(1e-6, min(bw, bh)), 2),
        "rectangularity": round(q.area / mrr.area, 3) if mrr.area else 0.0,
        "convexity": round(q.area / hull.area, 3) if hull.area else 0.0,
        "bbox_fill": round(q.area / (bw * bh), 3) if bw * bh else 0.0,
        "orthogonality": round(orthogonality(q), 3),
        "compactness": round(4 * math.pi * q.area / (q.length**2), 3) if q.length else 0.0,
        "symmetry": round(max(_mirror_iou(qr, "v"), _mirror_iou(qr, "h")), 3),
        "dir_families": [(round(a, 0), round(s, 2)) for a, s in fam],
        "holes_m2": [round(h.area, 1) for h in holes_of(poly)],
        "vertices": len(q.exterior.coords) - 1,
    }
    return d


# --------------------------------------------------------------------------------------------------
# classifier
# --------------------------------------------------------------------------------------------------


def classify(poly: Polygon, clean: bool = True, **overrides: float) -> TypologyResult:
    """Classify an outline. ``poly`` is in metres in any rotation; holes (courtyards) are honoured. A multi-part
    outline (two buildings, a detached stair) is classified on its largest part."""
    poly = _largest(poly)
    cfg_backup = dict(CFG)
    CFG.update(overrides)
    try:
        return _classify(poly, clean)
    finally:
        CFG.clear()
        CFG.update(cfg_backup)


def _classify(poly: Polygon, do_clean: bool) -> TypologyResult:
    q = rectilinearise(poly) if do_clean else Polygon(poly.exterior)
    if q.is_empty or q.area < 50:
        return TypologyResult("other", "degenerate", 0.2, reasons=["outline too small or empty"], clean=q)
    desc = describe(poly, q)
    res = TypologyResult("other", descriptors=desc, clean=q)
    why = res.reasons

    holes = holes_of(poly)
    if holes:
        res.label, res.subtype, res.confidence = "courtyard", "closed", 0.85
        why.append(f"{len(holes)} interior hole(s) of {', '.join(f'{h.area:.0f}' for h in holes)} m2: enclosed courtyard")
        return res

    ortho = desc["orthogonality"]
    a0 = dominant_axis_deg(q)
    if ortho >= CFG["orth_min"]:
        qr = affinity.rotate(q, -a0, origin=q.centroid)
        res.angle_deg = -a0
        res.clean = qr
        return _classify_rectilinear(res, qr, desc)
    return _classify_oblique(res, q, desc)


def _classify_rectilinear(res: TypologyResult, q: Polygon, desc: dict[str, Any]) -> TypologyResult:
    why = res.reasons
    minx, miny, maxx, maxy = q.bounds
    bw, bh = maxx - minx, maxy - miny
    aspect = max(bw, bh) / max(1e-6, min(bw, bh))
    fits, info = fit_templates(q)
    best = fits[0]
    bb = info["bbox"]
    notches = _notches_of(best, bb)
    res.notches = notches
    tpl = box(*bb)
    if notches:
        tpl = tpl.difference(unary_union([n.polygon for n in notches]))
    iou = tpl.intersection(q).area / max(1e-6, tpl.union(q).area)
    desc["fit_iou"] = round(iou, 3)
    desc["fit_name"] = best.name
    # margin to the best candidate of a *different* family (L:SW vs L:SE is not an alternative)
    fam = best.name.split(":")[0]
    others = [f for f in fits[1:] if f.name.split(":")[0] != fam]
    margin = best.score - (others[0].score if others else 0.0)
    desc["fit_margin"] = round(margin, 3)
    desc["fit_runner_up"] = others[0].name if others else ""
    why.append(f"rectilinear (orthogonality {desc['orthogonality']:.2f}), bbox {max(bw, bh):.1f} x {min(bw, bh):.1f} m; "
               f"best template {best.name} (fit IoU {iou:.2f}, margin {margin:.2f} over {desc['fit_runner_up'] or 'none'})")

    def done(label, sub, wings=(), note="", bonus=0.0):
        res.label, res.subtype = label, sub
        fit_t = max(0.0, min(1.0, (iou - 0.80) / 0.15))
        mar_t = max(0.0, min(1.0, margin / 0.12))
        res.confidence = float(max(0.1, min(0.99, 0.5 * fit_t + 0.5 * mar_t + bonus)))
        res.wings = list(wings)
        if note:
            why.append(note)
        return res

    if fam == "none":
        slab = aspect >= CFG["slab_aspect"]
        bonus = min(0.2, abs(aspect - CFG["slab_aspect"]) / 5.0)
        if slab:
            return done("linear_slab", "double-loaded" if min(bw, bh) >= 14 else "single-loaded",
                        [Wing("bar", max(bw, bh), min(bw, bh))], f"no notch pays off, aspect {aspect:.2f} >= "
                        f"{CFG['slab_aspect']:.1f}: slab", bonus)
        return done("point_square", "square" if aspect < 1.25 else "squat-rect", [Wing("bar", max(bw, bh), min(bw, bh))],
                    f"no notch pays off, aspect {aspect:.2f}: compact point tower", bonus)
    ns = notches
    if fam == "L":
        n = ns[0]
        w1, w2 = Wing("arm", bw, bh - n.depth), Wing("arm", bh, bw - n.width)
        return done("L", "", [w1, w2], f"one corner notch ({n.width:.1f} x {n.depth:.1f} m) at {''.join(n.sides)}: L, arms "
                    f"{w1.length:.0f}x{w1.width:.0f} and {w2.length:.0f}x{w2.width:.0f} m")
    if fam == "T":
        a, b = ns
        shared = (set(a.sides) & set(b.sides)).pop()
        if shared in "WE":  # notches stacked along W or E: bar on the opposite side runs N-S
            bar_t, stem_w, stem_len, bar_len = bw - max(a.width, b.width), bh - a.depth - b.depth, max(a.width, b.width), bh
        else:
            bar_t, stem_w, stem_len, bar_len = bh - max(a.depth, b.depth), bw - a.width - b.width, max(a.depth, b.depth), bw
        sym = abs(a.area - b.area) / max(a.area, b.area)
        return done("T", "symmetric" if sym < 0.25 else "offset-stem", [Wing("bar", bar_len, bar_t), Wing("stem", stem_len, stem_w)],
                    f"two corner notches on side {shared}: T, bar {bar_len:.0f}x{bar_t:.0f}, stem {stem_len:.0f}x{stem_w:.0f} m")
    if fam == "Z":
        return done("other", "Z/S", note="two corner notches on opposite corners: Z/S (stepped) plan")
    if fam == "tri":
        return done("other", "3-corner", note="three corner notches: no standard bucket")
    if fam == "cross":
        mw, md = float(np.mean([n.width for n in ns])), float(np.mean([n.depth for n in ns]))
        return done("cruciform", "", [Wing("arm", bw, bh - 2 * md), Wing("arm", bh, bw - 2 * mw)],
                    f"four corner notches (mean {mw:.1f} x {md:.1f} m): cruciform, arms {bw:.0f}x{bh - 2 * md:.0f} and "
                    f"{bh:.0f}x{bw - 2 * mw:.0f} m")
    if fam == "H":
        a, b = ns
        if a.sides[0] in "WE":
            link_len, link_w, bar_len, bar_t = max(a.depth, b.depth), bw - a.width - b.width, bw, (bh - max(a.depth, b.depth)) / 2
        else:
            link_len, link_w, bar_len, bar_t = max(a.width, b.width), bh - a.depth - b.depth, bh, (bw - max(a.width, b.width)) / 2
        return done("H", "", [Wing("bar", bar_len, bar_t), Wing("bar", bar_len, bar_t), Wing("link", link_len, link_w)],
                    f"two facing mid-side notches ({a.sides[0]}/{b.sides[0]}): H, bars {bar_len:.0f}x{bar_t:.0f}, "
                    f"link {link_len:.0f}x{link_w:.0f} m")
    if fam == "S":
        return done("other", "S/offset", note="two offset notches on opposite sides: S (stepped) plan")
    if fam == "comb":
        return done("other", "comb", note=f"{len(ns)} separate notches on side {ns[0].sides[0]}: comb / E plan (a slab with projecting wings)")
    if fam == "U":
        n = ns[0]
        return done("courtyard", "U", [], f"one deep mid-side notch ({n.width:.1f} x {n.depth:.1f} m) on side "
                    f"{n.sides[0]}: open courtyard (U)")
    return done("other", best.name, note="no standard bucket")

def _classify_oblique(res: TypologyResult, q: Polygon, desc: dict[str, Any]) -> TypologyResult:
    why = res.reasons
    fam = direction_families(q)
    hull = q.convex_hull
    conv = q.area / hull.area
    three = False
    if len(fam) >= 3:
        angs = sorted(a for a, _s in fam[:4])
        for i in range(len(angs)):
            for j in range(i + 1, len(angs)):
                for k in range(j + 1, len(angs)):
                    gaps = sorted([(angs[j] - angs[i]), (angs[k] - angs[j]), 180 - (angs[k] - angs[i])])
                    if all(abs(g - 60) <= 14 for g in gaps):
                        three = True
    why.append(f"oblique outline (orthogonality {desc['orthogonality']:.2f}); {len(fam)} direction families; "
               f"convexity {conv:.2f}")
    if three:
        if conv >= 0.88:
            hs = hull.simplify(q.length * 0.03)
            nv = len(hs.exterior.coords) - 1
            if nv <= 4:
                res.label, res.subtype, res.confidence, res.experimental = "other", "triangle", 0.7, True
                why.append("three 60-degree families, convex: triangle plate, parked as experimental")
                return res
            res.label, res.subtype, res.confidence = "point_square", "hex/oct", 0.55
            why.append("three 60-degree families, convex, more than 3 corners: compact hexagonal point tower")
            return res
        res.label, res.subtype, res.confidence = "Y", "", 0.7
        why.append("three direction families 60 degrees apart and a non-convex outline: Y plan")
        return res
    if conv >= 0.9 and desc["aspect"] < 1.6:
        res.label, res.subtype, res.confidence = "point_square", "round/chamfered", 0.6
        why.append("oblique but compact and convex: point tower")
        return res
    res.label, res.subtype, res.confidence = "other", "oblique", 0.4
    why.append("oblique, no three-family structure: not a standard bucket")
    return res


# --------------------------------------------------------------------------------------------------
# where the core sits in the plate
# --------------------------------------------------------------------------------------------------

CORE_POSITIONS = ("central", "side", "end", "reentrant", "outside")


def _inner_corners(qr: Polygon) -> list[tuple[float, float]]:
    """Inner (re-entrant) corners of the notches of the best-fitting template: notch vertices that are not on the bbox.
    Using the template instead of the raw outline ignores the many small steps of a real plan."""
    fits, info = fit_templates(qr)
    minx, miny, maxx, maxy = info["bbox"]
    out = []
    for _kind, _sides, poly in fits[0].notches:
        for x, y in list(poly.exterior.coords)[:-1]:
            on = min(abs(x - minx), abs(x - maxx), abs(y - miny), abs(y - maxy)) < 1e-6
            if not on:
                out.append((x, y))
    return out


def core_position(poly: Polygon, core_xy: tuple[float, float]) -> dict[str, Any]:
    """Where a core centroid sits in a plate: ``central`` (middle fifth of both bbox axes: the heart of a point tower or
    the crossing of a cross/H), ``reentrant`` (within 25 % of the long bbox side of an inner corner of a notched plate:
    the knee of an L, the junction of a T), ``end`` (outer third of the long axis of an elongated plate), else ``side``
    (pushed to one long face). ``poly`` and ``core_xy`` are in the same frame; rotation is handled here."""
    from shapely.geometry import Point

    q = rectilinearise(poly)
    a0 = dominant_axis_deg(q)
    org = q.centroid
    qr = affinity.rotate(q, -a0, origin=org)
    pt = affinity.rotate(Point(core_xy), -a0, origin=org)
    minx, miny, maxx, maxy = qr.bounds
    bw, bh = maxx - minx, maxy - miny
    u, v = (pt.x - minx) / bw, (pt.y - miny) / bh
    info: dict[str, Any] = {"u": round(u, 3), "v": round(v, 3)}
    if not q.buffer(2.0).contains(Point(core_xy)):
        info["position"] = "outside"
        return info
    long_is_x = bw >= bh
    t_long = (u if long_is_x else v) - 0.5
    t_short = (v if long_is_x else u) - 0.5
    aspect = max(bw, bh) / max(1e-6, min(bw, bh))
    inner = _inner_corners(qr)
    d_inner = min((math.hypot(pt.x - x, pt.y - y) for x, y in inner), default=None)
    info["d_inner_corner_m"] = round(d_inner, 1) if d_inner is not None else None
    if abs(t_long) <= 0.2 and abs(t_short) <= 0.2:
        info["position"] = "central"
    elif d_inner is not None and d_inner <= 0.25 * max(bw, bh):
        info["position"] = "reentrant"
    elif aspect >= 1.6 and abs(t_long) > 0.3:
        info["position"] = "end"
    else:
        info["position"] = "side"
    return info


# --------------------------------------------------------------------------------------------------
# synthetic outlines (tests, docs, and the planner's own plate templates)
# --------------------------------------------------------------------------------------------------


def make_outline(kind: str, a: float = 40.0, b: float = 14.0, c: float | None = None) -> Polygon:
    """Textbook outlines: ``a`` is the long dimension, ``b`` the wing width."""
    c = a if c is None else c
    if kind == "slab":
        return box(0, 0, a, b)
    if kind == "square":
        return box(0, 0, a, a)
    if kind == "L":
        return unary_union([box(0, 0, a, b), box(0, 0, b, c)])
    if kind == "T":
        return unary_union([box(0, c - b, a, c), box((a - b) / 2, 0, (a + b) / 2, c)])
    if kind == "H":
        return unary_union([box(0, 0, b, c), box(a - b, 0, a, c), box(0, (c - b) / 2, a, (c + b) / 2)])
    if kind == "cross":
        return unary_union([box(0, (c - b) / 2, a, (c + b) / 2), box((a - b) / 2, 0, (a + b) / 2, c)])
    if kind == "U":
        return unary_union([box(0, 0, a, b), box(0, 0, b, c), box(a - b, 0, a, c)])
    if kind == "ring":
        return Polygon(box(0, 0, a, c).exterior, [box(b, b, a - b, c - b).exterior])
    if kind == "Y":
        pts = []
        arms = []
        for ang in (90, 210, 330):
            t = math.radians(ang)
            d = (math.cos(t), math.sin(t))
            n = (-d[1], d[0])
            p0 = (-n[0] * b / 2, -n[1] * b / 2)
            p1 = (n[0] * b / 2, n[1] * b / 2)
            p2 = (p1[0] + d[0] * a, p1[1] + d[1] * a)
            p3 = (p0[0] + d[0] * a, p0[1] + d[1] * a)
            arms.append(Polygon([p0, p1, p2, p3]))
        del pts
        return unary_union(arms)
    if kind == "triangle":
        h = a * math.sqrt(3) / 2
        return Polygon([(0, 0), (a, 0), (a / 2, h)])
    raise ValueError(kind)
