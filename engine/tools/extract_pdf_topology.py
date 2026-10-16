"""Recover room geometry and topology (flats, adjacency, doors) from vector municipal floor-plan PDFs.

Pipeline per PDF page (pages are independent, so the driver runs up to 3 pages in parallel):

1. OCR the page (labels are outlined text, see mine_mumbai_pdfs.py) and turn labels into "seeds"
   (room name + optional clear dimensions "a x b" + position in PDF points).
2. Vector drawing -> axis-aligned stroke segments (page rotation estimated from the segment angles).
3. Wall mask = parallel line pairs 0.08-0.36 m apart, filled between the lines (single lines such as
   dimension lines, grid lines, door leaves, hatch and furniture are not walls).
4. Free space is split among the seeds with a seeded watershed on the distance transform, which cuts at
   narrow necks, so door openings need no explicit closing. Pt-to-metre scale is calibrated per page
   from the OCR dimensions of the rooms (median over seeds).
5. Door swing arcs (dashed quarter circles) are clustered and turned into room-to-room door links; open
   gaps (watershed necks) give "open" links; shared walls (a gap of at most 0.4 m) give adjacency.
6. Rooms are grouped into flats (components of the link graph without common lobby/lift/stair rooms).

Usage: python -I extract_pdf_topology.py <dataset_root> <out.jsonl> [workers<=3] [--limit N]
Parallelism: pages are independent, so a ProcessPoolExecutor runs up to 3 pages at once (OCR, raster
and flood are all single-threaded per page).
Page-level markers {"type": "_page_done", ...} make the run resumable; flat records have no "type".
"""

from __future__ import annotations

import json
import math
import os
import re
import sys
from collections import Counter, defaultdict, deque
from pathlib import Path

os.environ.setdefault("OMP_THREAD_LIMIT", "1")
sys.path.insert(0, str(Path(__file__).resolve().parent))

import numpy as np  # noqa: E402
from mine_mumbai_pdfs import DIM, classify, group_lines  # noqa: E402

MAX_WORKERS = 3
TEXT_H_M = 0.24
MAX_PIXELS = 9e6
_DEBUG: dict = {}
OCR_DPI = 150
TARGET_TEXT_PX = 40
ROOM_TYPES_COMMON = {"lobby", "lift", "fire_lift", "stair"}
HABITABLE = {"master", "bedroom", "living", "kitchen", "dining", "study", "pooja"}
BED_TYPES = {"master", "bedroom"}
WALL_MIN_M, WALL_MAX_M = 0.07, 0.36
EXTRA_TYPES = [
    (r"EVACU|FIRE\s*(MAN)?\s*LIFT", "fire_lift"),
    (r"SM[OA]CK\s*CHECK|SMOKE\s*CHECK", "lobby"),
    (r"MULTI\s*PURPOSE|M\.?P\.?R\b|REHAB\s*ROOM", "multipurpose"),
    (r"ALCOVE", "alcove"),
    (r"FOYER|ENTRANCE", "foyer"),
    (r"CHAJJ?A", "chajja"),
]


def classify2(text: str) -> str | None:
    t = text.upper()
    for pat, ty in EXTRA_TYPES:
        if re.search(pat, t):
            return ty
    return classify(text)


# --------------------------------------------------------------------------------------------------
# 1. OCR -> seeds
# --------------------------------------------------------------------------------------------------


def _tess_words(img):
    import pytesseract

    data = pytesseract.image_to_data(img, config="--psm 11", output_type=pytesseract.Output.DICT)
    words = []
    for i, w in enumerate(data["text"]):
        w = w.strip()
        if not w or float(data["conf"][i]) < 30:
            continue
        words.append((w, data["left"][i], data["top"][i], data["width"][i], data["height"][i]))
    return words


def _raster_words(page, dpi):
    import pymupdf
    from PIL import Image

    pix = page.get_pixmap(dpi=dpi, colorspace=pymupdf.csGRAY)
    return _tess_words(Image.frombytes("L", (pix.width, pix.height), pix.samples)), (0.0, 0.0)


def text_polygons(drawings):
    """Filled outline paths (the text converted to outlines) as lists of sub-polygons in points."""
    out = []
    for d in drawings:
        if d["type"] not in ("f", "fs"):
            continue
        r = d["rect"]
        if (r.width > 500 or r.height > 500) and len(d["items"]) < 40:  # big solid fills, not text
            continue
        if r.width < 0.3 and r.height < 0.3:
            continue
        fill = d.get("fill")
        if fill is not None and min(fill) > 0.9:
            continue
        polys, cur, last = [], [], None
        for it in d["items"]:
            k = it[0]
            if k == "l":
                p, q = it[1], it[2]
                pts = [(p.x, p.y), (q.x, q.y)]
            elif k == "c":
                p, q = it[1], it[4]
                pts = []
                for t in (0.0, 0.2, 0.4, 0.6, 0.8, 1.0):
                    u = 1 - t
                    pts.append((u**3 * it[1].x + 3 * u * u * t * it[2].x + 3 * u * t * t * it[3].x + t**3 * it[4].x,
                                u**3 * it[1].y + 3 * u * u * t * it[2].y + 3 * u * t * t * it[3].y + t**3 * it[4].y))
            elif k == "re":
                rr = it[1]
                p = q = rr.tl
                pts = [(rr.x0, rr.y0), (rr.x1, rr.y0), (rr.x1, rr.y1), (rr.x0, rr.y1)]
            else:
                continue
            if last is not None and (abs(p.x - last.x) > 1e-3 or abs(p.y - last.y) > 1e-3):
                polys.append(cur)
                cur = []
            cur += pts
            last = q
        if cur:
            polys.append(cur)
        polys = [np.asarray(p_, dtype=np.float64) for p_ in polys if len(p_) > 2]
        if polys:
            out.append(polys)
    return out


def _signed_area(p):
    return 0.5 * float(np.sum(p[:, 0] * np.roll(p[:, 1], -1) - np.roll(p[:, 0], -1) * p[:, 1]))


def render_text(polys, bbox, dpi):
    """Binary text-only image (black glyphs on white) of bbox=(x0, y0, x1, y1) at dpi."""
    from PIL import Image, ImageDraw

    x0, y0, x1, y1 = bbox
    k = dpi / 72.0
    img = Image.new("L", (int(math.ceil((x1 - x0) * k)) + 1, int(math.ceil((y1 - y0) * k)) + 1), 255)
    dr = ImageDraw.Draw(img)
    for subs in polys:
        areas = [_signed_area(p) for p in subs]
        sg = math.copysign(1.0, max(areas, key=abs))
        for hole in (False, True):
            for p, a_ in zip(subs, areas, strict=True):
                if (math.copysign(1.0, a_) != sg) == hole:
                    dr.polygon([((x - x0) * k, (y - y0) * k) for x, y in p], fill=255 if hole else 0)
    return img


def _text_words(polys, bbox, dpi, rotate=False):
    from PIL import Image

    img = render_text(polys, bbox, dpi)
    if rotate:
        img = img.transpose(Image.Transpose.ROTATE_270)  # 90 degrees clockwise
    return _tess_words(img)


def _digit_heights(words, k):
    return [w[4] * k for w in words if re.fullmatch(r"[\d.,]+", w[0]) and len(w[0]) >= 3]


def text_height(polys):
    """Typical text height in points: the mode of the short side of elongated glyph runs."""
    hs = []
    for subs in polys:
        pp = np.concatenate(subs)
        w_, h_ = float(np.ptp(pp[:, 0])), float(np.ptp(pp[:, 1]))
        lo, hi = min(w_, h_), max(w_, h_)
        if lo > 0.8 and hi >= 3 * lo:
            hs.append(lo)
    if len(hs) < 5:
        return None
    hs = np.log(np.asarray(hs))
    hist, edges = np.histogram(hs, bins=np.arange(hs.min(), hs.max() + 0.07, 0.07))
    k = int(np.argmax(hist))
    return float(math.exp((edges[k] + edges[k + 1]) / 2))


def ocr_lines(page, drawings):
    """OCR as (lines, text_height_pt, vertical).

    Labels are outlined text, so when the page has filled glyph paths only those are rendered (no wall
    lines to confuse Tesseract, 5-10x faster) at a dpi that makes the text ~28 px tall. Vertical labels
    (text rotated 90 degrees) are read from a second, rotated render when present.
    Lines are (text, x0, y0, w, h) in PDF points. vertical is None or (lines, bbox) with the lines in
    the frame rotated 90 degrees clockwise (x' = bbox_h - (y - y0), y' = x - x0); see unrotate_seed.
    """
    polys = text_polygons(drawings)
    h_txt = text_height(polys) if len(polys) >= 40 else None
    if h_txt is None:
        probe, _ = _raster_words(page, 100)
        hs = _digit_heights(probe, 0.72)
        dpi = int(np.clip(100 * 21.6 / float(np.median(hs)), 70, 300)) if len(hs) >= 5 else OCR_DPI
        words, _off = _raster_words(page, dpi)
        k = 72.0 / dpi
        dh = _digit_heights(words, k)
        return ([(t, x * k, y * k, w * k, h * k) for t, x, y, w, h in group_lines(words)],
                float(np.median(dh)) / 0.72 * 0.72 if len(dh) >= 5 else None, None)
    allp = np.concatenate([p for subs in polys for p in subs])
    lo, hi = np.percentile(allp, 0.2, axis=0), np.percentile(allp, 99.8, axis=0)
    bbox = (float(lo[0]) - 10, float(lo[1]) - 10, float(hi[0]) + 10, float(hi[1]) + 10)
    dpi = int(np.clip(72.0 * TARGET_TEXT_PX / h_txt, 60, 330))
    words = _text_words(polys, bbox, dpi)
    k = 72.0 / dpi
    lines = [(t, x * k + bbox[0], y * k + bbox[1], w * k, h * k) for t, x, y, w, h in group_lines(words)]
    tall = wide = 0
    for subs in polys:
        pp = np.concatenate(subs)
        w_, h_ = np.ptp(pp[:, 0]), np.ptp(pp[:, 1])
        if h_ > 2.0 * w_ and h_ > 4 * h_txt:
            tall += 1
        elif w_ > 2.0 * h_ and w_ > 4 * h_txt:
            wide += 1
    vert = None
    if tall >= 5 and tall >= 0.1 * (tall + wide):
        vw = _text_words(polys, bbox, dpi, rotate=True)
        vert = ([(t, x * k, y * k, w * k, h * k) for t, x, y, w, h in group_lines(vw)], bbox)
    return lines, h_txt, vert


def unrotate_seed(sd, bbox):
    """Map a seed found in the 90-degrees-clockwise frame back to page coordinates."""
    x0, y0, x1, y1 = bbox
    out = dict(sd)
    out["x"] = x0 + sd["y"]
    out["y"] = y0 + (y1 - y0) - sd["x"]
    out["w"], out["h"] = sd["h"], sd["w"]
    return out


def combine_seeds(seeds, vseeds):
    """Add vertical-label seeds unless an upright seed with the same dimensions is already nearby."""
    out = list(seeds)
    for v in vseeds:
        dup = any(abs(s["x"] - v["x"]) < 80 and abs(s["y"] - v["y"]) < 80 and
                  (s["dims"] == v["dims"] or (v["dims"] is None and s["type"] == v["type"])) for s in seeds)
        if not dup:
            out.append(v)
    return out


def stack_lines(lines):
    """Union lines stacked vertically and centred on each other (a wrapped label) into blocks."""
    n = len(lines)
    parent = list(range(n))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    order = sorted(range(n), key=lambda i: lines[i][2])
    for ii, i in enumerate(order):
        ti, xi, yi, wi, hi = lines[i]
        for j in order[ii + 1:]:
            tj, xj, yj, wj, hj = lines[j]
            h = max(hi, hj)
            gap = yj - (yi + hi)
            if gap > 0.7 * h:
                break
            if gap < -0.3 * h:
                continue
            if abs((xi + wi / 2) - (xj + wj / 2)) <= 0.22 * max(wi, wj) + 0.2 * h:
                parent[find(j)] = find(i)
    groups = defaultdict(list)
    for i in range(n):
        groups[find(i)].append(lines[i])
    return [sorted(g, key=lambda ln: ln[2]) for g in groups.values()]


WIDTH = re.compile(r"(\d[.,]\d{1,2})\s*M\.?\s*\.?\s*W", re.I)


def seeds_from_lines(lines):
    """Seeds: dicts(name, type, dims, x, y) with positions in PDF points."""
    seeds = []
    for blk in stack_lines(lines):
        text, spans = "", []
        for ln in blk:
            if text:
                text += " "
            spans.append((len(text), len(text) + len(ln[0]), ln))
            text += ln[0]
        flat = text.replace(",", ".")
        wm = WIDTH.search(flat)
        lwidth = float(wm.group(1)) if wm else None
        prev = 0
        found = False
        for m in DIM.finditer(flat):
            a, b = float(m.group(1)), float(m.group(2))
            name = flat[prev:m.start()].strip()
            ls = [sp[2] for sp in spans if sp[1] > prev and sp[0] < m.end()] or [blk[0]]
            prev = m.end()
            if not (0.6 <= a <= 20 and 0.6 <= b <= 20):
                continue
            ty = classify2(name) if name else None
            if ty is None and name and not found:  # name may sit just before an earlier dimension
                ty = None
            x0 = min(ln[1] for ln in ls)
            x1 = max(ln[1] + ln[3] for ln in ls)
            y0 = min(ln[2] for ln in ls)
            y1 = max(ln[2] + ln[4] for ln in ls)
            seeds.append({"name": name or None, "type": ty, "dims": (a, b), "x": (x0 + x1) / 2,
                          "y": (y0 + y1) / 2, "h": y1 - y0, "w": x1 - x0, "lwidth": lwidth})
            found = True
        if not found:
            ty = classify2(flat)
            if ty:
                x0 = min(ln[1] for ln in blk)
                x1 = max(ln[1] + ln[3] for ln in blk)
                y0 = min(ln[2] for ln in blk)
                y1 = max(ln[2] + ln[4] for ln in blk)
                seeds.append({"name": flat.strip()[:40], "type": ty, "dims": None, "x": (x0 + x1) / 2,
                              "y": (y0 + y1) / 2, "h": y1 - y0, "w": x1 - x0, "lwidth": lwidth})
    return merge_name_dims(seeds)


def merge_name_dims(seeds):
    """A dimension-only seed right under/over a name-only seed is the same room."""
    names = [s for s in seeds if s["dims"] is None and s["type"] not in (None, "shaft")]
    dimonly = [s for s in seeds if s["dims"] is not None and s["type"] is None]
    dead = set()
    for d in dimonly:
        best, bd = None, 1e9
        for nm in names:
            if id(nm) in dead:
                continue
            dy = abs(nm["y"] - d["y"])
            dx = abs(nm["x"] - d["x"])
            lim = 4.0 * max(nm["h"], d["h"])
            if dy <= lim and dx <= 0.8 * max(nm["w"], d["w"]) and dy + dx < bd:
                best, bd = nm, dy + dx
        if best is not None:
            d["type"], d["name"] = best["type"], best["name"]
            d["y"] = (d["y"] + best["y"]) / 2
            dead.add(id(best))
    return [s for s in seeds if id(s) not in dead]


# --------------------------------------------------------------------------------------------------
# 2. vector geometry
# --------------------------------------------------------------------------------------------------


def load_vectors(drawings):
    """Stroke segments (N,4) and door-arc candidate pieces (list of (n,2) point arrays)."""
    segs, pieces = [], []
    for d in drawings:
        if d["type"] not in ("s", "fs"):
            continue
        items = d["items"]
        pts, curved = [], False
        for it in items:
            k = it[0]
            if k == "l":
                p, q = it[1], it[2]
                segs.append((p.x, p.y, q.x, q.y))
                pts += [(p.x, p.y), (q.x, q.y)]
            elif k == "re":
                r = it[1]
                segs += [(r.x0, r.y0, r.x1, r.y0), (r.x1, r.y0, r.x1, r.y1), (r.x1, r.y1, r.x0, r.y1),
                         (r.x0, r.y1, r.x0, r.y0)]
            elif k == "qu":
                r = it[1].rect
                segs += [(r.x0, r.y0, r.x1, r.y0), (r.x1, r.y0, r.x1, r.y1), (r.x1, r.y1, r.x0, r.y1),
                         (r.x0, r.y1, r.x0, r.y0)]
            elif k == "c":
                curved = True
                pts += [(it[1].x, it[1].y), (it[2].x, it[2].y), (it[3].x, it[3].y), (it[4].x, it[4].y)]
        if 1 <= len(items) <= 24 and len(pts) >= 2:
            a = np.asarray(pts, dtype=np.float32)
            if np.ptp(a[:, 0]) + np.ptp(a[:, 1]) < 220:
                if curved:
                    pieces.append(a)
                else:
                    seg = a.reshape(-1, 2, 2)
                    dd = seg[:, 1] - seg[:, 0]
                    ang = np.abs(np.degrees(np.arctan2(dd[:, 1], dd[:, 0]))) % 90
                    if np.any(np.minimum(ang, 90 - ang) > 4):
                        pieces.append(a)
    return np.asarray(segs, dtype=np.float32).reshape(-1, 4), pieces


def dominant_angle(segs):
    """Dominant drawing direction in degrees (mod 90) from length-weighted segment angles."""
    d = segs[:, 2:] - segs[:, :2]
    ln = np.hypot(d[:, 0], d[:, 1])
    keep = ln > 15
    if keep.sum() < 20:
        return 0.0
    ang = np.degrees(np.arctan2(d[keep, 1], d[keep, 0])) % 90
    w = ln[keep]
    hist, edges = np.histogram(ang, bins=90, range=(0, 90), weights=w)
    k = int(np.argmax(hist))
    centre = (edges[k] + edges[k + 1]) / 2
    diff = (ang - centre + 45) % 90 - 45
    sel = np.abs(diff) < 2.5
    th = centre + float(np.average(diff[sel], weights=w[sel])) if sel.any() else centre
    th = (th + 45) % 90 - 45
    return 0.0 if abs(th) < 0.3 else th


def rotate_pts(a, theta_deg, c):
    th = math.radians(-theta_deg)
    ca, sa = math.cos(th), math.sin(th)
    x = a[..., 0] - c[0]
    y = a[..., 1] - c[1]
    return np.stack([ca * x - sa * y + c[0], sa * x + ca * y + c[1]], axis=-1)


# --------------------------------------------------------------------------------------------------
# 3-4. raster, walls, watershed
# --------------------------------------------------------------------------------------------------


class Grid:
    """Raster grid in rotated PDF space: pixel = (pt - origin) * rho."""

    def __init__(self, x0, y0, x1, y1, rho):
        self.x0, self.y0, self.rho = x0, y0, rho
        self.w = int(math.ceil((x1 - x0) * rho)) + 1
        self.h = int(math.ceil((y1 - y0) * rho)) + 1

    def px(self, pts):
        a = np.asarray(pts, dtype=np.float64)
        return (a[..., 0] - self.x0) * self.rho, (a[..., 1] - self.y0) * self.rho


def draw_segments(grid, segs, width=1):
    from PIL import Image, ImageDraw

    img = Image.new("L", (grid.w, grid.h), 0)
    dr = ImageDraw.Draw(img)
    x0, y0 = grid.px(segs[:, :2])
    x1, y1 = grid.px(segs[:, 2:])
    for a, b, c, d in zip(x0, y0, x1, y1, strict=True):
        dr.line((a, b, c, d), fill=1, width=width)
    return np.asarray(img, dtype=bool)


def orth_segments(segs, tol_deg=2.5, min_len=1.5):
    d = segs[:, 2:] - segs[:, :2]
    ln = np.hypot(d[:, 0], d[:, 1])
    ang = np.abs(np.degrees(np.arctan2(d[:, 1], d[:, 0]))) % 90
    off = np.minimum(ang, 90 - ang)
    m = (off < tol_deg) & (ln >= min_len)
    return segs[m]


def _closing_1d(a, k, axis):
    from scipy.ndimage import maximum_filter1d, minimum_filter1d

    return minimum_filter1d(maximum_filter1d(a, k, axis=axis, mode="constant"), k, axis=axis, mode="constant")


def _has_partner(a, kmin, kmax, axis):
    """True where another set pixel lies kmin..kmax pixels away along axis (either side)."""
    c = np.cumsum(a, axis=axis, dtype=np.int32)
    n = a.shape[axis]
    c = np.moveaxis(c, axis, 0)
    pad = np.concatenate([np.zeros((1,) + c.shape[1:], np.int32), c], axis=0)  # pad[i] = sum of [0, i)
    idx = np.arange(n)
    hi = np.minimum(idx + kmax + 1, n)
    lo = np.minimum(idx + kmin, n)
    up = pad[hi] - pad[lo]
    hi2 = np.maximum(idx - kmin + 1, 0)
    lo2 = np.maximum(idx - kmax, 0)
    dn = pad[hi2] - pad[lo2]
    out = (up > 0) | (dn > 0)
    return np.moveaxis(out, 0, axis)


def wall_mask(grid, osegs, s, paired=True):
    """Wall pixels: parallel line pairs WALL_MIN_M..WALL_MAX_M apart, filled between the lines."""
    dx = np.abs(osegs[:, 2] - osegs[:, 0])
    dy = np.abs(osegs[:, 3] - osegs[:, 1])
    hor = draw_segments(grid, osegs[dx >= dy])
    ver = draw_segments(grid, osegs[dx < dy])
    if not paired:
        return hor | ver
    kmin = max(2, int(round(WALL_MIN_M / s * grid.rho)))
    kmax = max(kmin + 2, int(round(WALL_MAX_M / s * grid.rho)))
    hp = hor & _has_partner(hor, kmin, kmax, 0)
    vp = ver & _has_partner(ver, kmin, kmax, 1)
    kk = kmax + 1
    return _closing_1d(hp, kk, 0) | _closing_1d(vp, kk, 1) | hp | vp


def open_free(free, k):
    from scipy.ndimage import maximum_filter1d, minimum_filter1d

    f = free
    for ax in (0, 1):
        f = minimum_filter1d(f.view(np.uint8), k, axis=ax, mode="constant")
    for ax in (0, 1):
        f = maximum_filter1d(f, k, axis=ax, mode="constant")
    return f.astype(bool)


def snap_free(free, px, py, rad):
    """Nearest free pixel to (px, py) within rad pixels, or None."""
    h, w = free.shape
    x, y = int(round(px)), int(round(py))
    if 0 <= x < w and 0 <= y < h and free[y, x]:
        return x, y
    x0, x1 = max(0, x - rad), min(w, x + rad + 1)
    y0, y1 = max(0, y - rad), min(h, y + rad + 1)
    if x0 >= x1 or y0 >= y1:
        return None
    win = free[y0:y1, x0:x1]
    ys, xs = np.nonzero(win)
    if not len(xs):
        return None
    d = (xs + x0 - x) ** 2 + (ys + y0 - y) ** 2
    k = int(np.argmin(d))
    if d[k] > rad * rad:
        return None
    return int(xs[k] + x0), int(ys[k] + y0)


def flood_labels(dt, markers, free, tmin):
    """Marker-controlled watershed on the distance transform (high DT floods first).

    scipy's watershed_ift uses a gradient cost, not altitude, so this is a bucket-queue priority
    flood (Meyer): pixels are taken in order of decreasing integer DT, each inheriting the label of
    the labelled neighbour that reached it. Work is vectorised per wave of the flood. The thin band
    near walls (DT < tmin) is finally given the label of the nearest labelled pixel.
    """
    from scipy.ndimage import distance_transform_edt

    h, w = free.shape
    wp = w + 2
    lab = np.zeros((h + 2, wp), np.int32)
    lab[1:-1, 1:-1] = markers
    fr = np.zeros((h + 2, wp), bool)
    fr[1:-1, 1:-1] = free
    dq = np.zeros((h + 2, wp), np.int16)
    dq[1:-1, 1:-1] = np.floor(dt)
    labf, frf, dqf = lab.ravel(), fr.ravel(), dq.ravel()
    offs = np.array([-wp, wp, -1, 1])
    tmax = int(dqf.max())
    buckets: dict[int, list] = defaultdict(list)

    def push(idx, lbl, level):
        """Queue neighbours of freshly labelled pixels; returns those eligible at this level."""
        nidx = (idx[:, None] + offs[None, :]).ravel()
        nl = np.repeat(lbl, 4)
        ok = frf[nidx] & (labf[nidx] == 0)
        nidx, nl = nidx[ok], nl[ok]
        lv = dqf[nidx]
        now = lv >= level
        for t in np.unique(lv[~now]):
            if t >= tmin:
                sel = (~now) & (lv == t)
                buckets[int(t)].append((nidx[sel], nl[sel]))
        return nidx[now], nl[now]

    start = np.nonzero(labf > 0)[0]
    ci, cl = push(start, labf[start], tmax + 1)
    buckets[tmax].append((ci, cl))
    for t in range(tmax, tmin - 1, -1):
        parts = buckets.pop(t, [])
        if not parts:
            continue
        ci = np.concatenate([p_[0] for p_ in parts])
        cl = np.concatenate([p_[1] for p_ in parts])
        while ci.size:
            ok = labf[ci] == 0
            ci, cl = ci[ok], cl[ok]
            if not ci.size:
                break
            ci, first = np.unique(ci, return_index=True)
            cl = cl[first]
            labf[ci] = cl
            ci, cl = push(ci, cl, t)
    lab = lab[1:-1, 1:-1].copy()
    rest = free & (lab == 0)
    if rest.any():
        d, (iy, ix) = distance_transform_edt(lab == 0, return_indices=True)
        take = rest & (d <= 2 * tmin + 2)
        lab[take] = lab[iy[take], ix[take]]
    return lab


def segment_rooms(free, marker_px, ext_mask, kghost, tmin):
    """Label image: 0 = wall/unreached, i+1 = seed i, n+1 = exterior, n+2.. = unlabelled rooms (ghosts).

    marker_px: per seed a (ys, xs) pixel set or None. Unseeded rooms (cores of the free space that
    contain no seed) get ghost labels so seeded regions cannot flood into them through a door.
    """
    from scipy.ndimage import distance_transform_edt, label

    dt = distance_transform_edt(free)
    markers = np.zeros(free.shape, dtype=np.int32)
    n = len(marker_px)
    for i, m in enumerate(marker_px):
        if m is not None:
            ys, xs = m
            sel = markers[ys, xs] == 0
            markers[ys[sel], xs[sel]] = i + 1
    markers[ext_mask & free & (markers == 0)] = n + 1
    core = open_free(free, kghost)
    comp, nc = label(core)
    seeded = np.zeros(nc + 1, bool)
    seeded[np.unique(comp[markers > 0])] = True
    seeded[0] = True
    ghost_ids = np.nonzero(~seeded)[0]
    if len(ghost_ids):
        gl = np.zeros(nc + 1, np.int32)
        gl[ghost_ids] = np.arange(len(ghost_ids), dtype=np.int32) + n + 2
        gm = gl[comp]
        markers = np.where((markers == 0) & (gm > 0), gm, markers)
    lab = flood_labels(dt, markers, free, tmin)
    return lab, dt


def region_stats(mask_bool):
    """Hole-filled pixel area and bbox of a region mask. Returns (area_px, w, h, y0, x0) or None."""
    from scipy.ndimage import binary_fill_holes

    ys, xs = np.nonzero(mask_bool)
    if not len(xs):
        return None
    x0, x1, y0, y1 = xs.min(), xs.max(), ys.min(), ys.max()
    sub = binary_fill_holes(mask_bool[y0:y1 + 1, x0:x1 + 1])
    return int(sub.sum()), int(x1 - x0 + 1), int(y1 - y0 + 1), int(y0), int(x0)


# --------------------------------------------------------------------------------------------------
# 5. door arcs
# --------------------------------------------------------------------------------------------------


def cluster_pieces(pieces):
    """Arc candidates: long single paths as they are, short dash pieces clustered by proximity."""
    from scipy.spatial import cKDTree

    big = [p for p in pieces if len(p) >= 8]
    small = [p for p in pieces if len(p) < 8]
    n = len(small)
    if n == 0:
        return big
    diag = np.array([np.ptp(p[:, 0]) + np.ptp(p[:, 1]) for p in small])
    eps = float(np.clip(0.9 * np.median(diag), 4, 20))
    ends = np.array([[p[0], p[-1]] for p in small]).reshape(-1, 2)
    tree = cKDTree(ends)
    parent = list(range(n))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for a, b in tree.query_pairs(eps):
        ra, rb = find(a // 2), find(b // 2)
        if ra != rb:
            parent[ra] = rb
    groups = defaultdict(list)
    for i in range(n):
        groups[find(i)].append(small[i])
    return big + [np.concatenate(g) for g in groups.values()]


def fit_door_arcs(clusters, s):
    """Quarter-circle fits. Returns list of (hx, hy, qx, qy, R) in rotated pt coordinates."""
    out = []
    for pts in clusters:
        if len(pts) < 5:
            continue
        x0, y0 = pts.min(0)
        x1, y1 = pts.max(0)
        w, h = x1 - x0, y1 - y0
        if w < 1 or h < 1 or not 0.6 <= w / h <= 1.7:
            continue
        r_m = 0.5 * (w + h) * s
        if not 0.4 <= r_m <= 1.7:
            continue
        best = None
        for cx, cy in ((x0, y0), (x1, y0), (x0, y1), (x1, y1)):
            d = np.hypot(pts[:, 0] - cx, pts[:, 1] - cy)
            r90 = float(np.percentile(d, 90))
            sel = d > 0.6 * r90
            if sel.sum() < 4:
                continue
            ang = np.degrees(np.arctan2(np.abs(pts[sel, 1] - cy), np.abs(pts[sel, 0] - cx)))
            if ang.min() > 25 or ang.max() < 65:
                continue
            sc = float(d[sel].std() / d[sel].mean())
            if best is None or sc < best[0]:
                best = (sc, cx, cy, float(d[sel].mean()))
        if best is None or best[0] > 0.09:
            continue
        _, cx, cy, r = best
        qx = 1 if cx == x0 else -1
        qy = 1 if cy == y0 else -1
        out.append((cx, cy, qx, qy, r))
    return out


def door_links(arcs, lab, grid, s, n):
    """Room pairs joined by door arcs. lab is the watershed label image (1..n rooms, n+1 exterior)."""
    links = Counter()
    h, w = lab.shape
    offs = [0.35, 0.5, 0.7, 0.95]

    def at(x, y):
        px, py = grid.px((x, y))
        px, py = int(round(float(px))), int(round(float(py)))
        if 0 <= px < w and 0 <= py < h:
            return int(lab[py, px])
        return 0

    def first(mx, my, nx, ny):
        for d in offs:
            v = at(mx + nx * d / s, my + ny * d / s)
            if v:
                return v
        return 0

    for hx, hy, qx, qy, r in arcs:
        for mx, my, nx, ny in ((hx + qx * r / 2, hy, 0, qy), (hx, hy + qy * r / 2, qx, 0)):
            a = first(mx, my, nx, ny)
            b = first(mx, my, -nx, -ny)
            if a and b and a != b and a <= n and b <= n:
                links[(min(a, b) - 1, max(a, b) - 1)] += 1
                break
    return links


# --------------------------------------------------------------------------------------------------
# adjacency from the label image
# --------------------------------------------------------------------------------------------------


def adjacency(lab, gap_px, ncode):
    """Pixel-pair counts between different labels: (touch[k=1], wall[k<=gap_px] shared across wall)."""
    touch, wall = Counter(), Counter()
    for axis in (0, 1):
        a = lab if axis == 1 else lab.T
        h, w = a.shape
        clear = np.ones((h, w), bool)
        for k in range(1, gap_px + 1):
            if k >= w:
                break
            left, right = a[:, : w - k], a[:, k:]
            if k > 1:
                clear[:, : w - k] &= a[:, k - 1: w - 1] == 0
            sel = clear[:, : w - k] & (left > 0) & (right > 0) & (left != right)
            if not sel.any():
                continue
            la, lb = left[sel], right[sel]
            lo, hi = np.minimum(la, lb).astype(np.int64), np.maximum(la, lb).astype(np.int64)
            keys, cnt = np.unique(lo * ncode + hi, return_counts=True)
            tgt = touch if k == 1 else wall
            for key, c in zip(keys.tolist(), cnt.tolist(), strict=True):
                tgt[(key // ncode - 1, key % ncode - 1)] += c
    return touch, wall


# --------------------------------------------------------------------------------------------------
# flats
# --------------------------------------------------------------------------------------------------


def is_common(room):
    ty = room["type"]
    if ty in ROOM_TYPES_COMMON:
        return True
    if ty == "passage":
        w = room.get("label_width") or room["short"]  # "1.05 M.W. PASSAGE" labels give the clear width
        return w >= 1.25 or (not room.get("label_width") and room["area"] >= 14)
    return False


def build_flats(rooms, links, pdfname, page):
    """Flats = components of the link graph without common rooms. Returns (flats, n_dropped)."""
    by_id = {r["id"]: r for r in rooms}
    nbr = defaultdict(set)
    for a, b in links:
        nbr[a].add(b)
        nbr[b].add(a)
    private = {r["id"] for r in rooms if not is_common(r) and r["type"] not in ("shaft", "chajja")}
    seen, flats = set(), []
    for rid in sorted(private):
        if rid in seen:
            continue
        comp, q = [], deque([rid])
        seen.add(rid)
        while q:
            u = q.popleft()
            comp.append(u)
            for v in nbr[u]:
                if v in private and v not in seen:
                    seen.add(v)
                    q.append(v)
        flats.append(sorted(comp))
    out, dropped = [], 0
    for comp in flats:
        cs = set(comp)
        types = [by_id[i]["type"] for i in comp]
        if not any(t in HABITABLE for t in types) or len(comp) < 2:
            continue
        dr = [by_id[i] for i in comp if by_id[i]["area_ratio"] is not None]
        if len(dr) >= 2 and sum(1 for r in dr if abs(r["area_ratio"] - 1) > 0.25) > 0.5 * len(dr):
            dropped += 1  # geometry disagrees with most of its own labels: garbage, not emitted
            continue
        ent, via = [], None
        for i in comp:
            for v in nbr[i]:
                if by_id[v].get("common"):
                    ent.append(i)
                    break
        if ent:
            via = "link"
        else:  # fall back to a shared wall with a common room
            for i in comp:
                if any(by_id[v].get("common") for v in by_id[i]["neighbours"]):
                    ent.append(i)
            via = "wall" if ent else None
        pref = {"foyer": 0, "living": 1, "passage": 2}
        ent.sort(key=lambda i: (pref.get(by_id[i]["type"], 5), -by_id[i]["area"]))
        entrance = ent[0] if ent else None
        hops = {}
        if entrance is not None:
            hops = {entrance: 0}
            q = deque([entrance])
            while q:
                u = q.popleft()
                for v in nbr[u]:
                    if v in cs and v not in hops:
                        hops[v] = hops[u] + 1
                        q.append(v)
        rr = []
        for i in comp:
            r = dict(by_id[i])
            r["hops"] = hops.get(i)
            r["neighbours"] = sorted(r["neighbours"])
            rr.append(r)
        commons = sorted({v for i in comp for v in nbr[i] if by_id[v].get("common")}
                         | {v for i in comp for v in by_id[i]["neighbours"] if by_id[v].get("common")})
        with_dims = [r for r in rr if r.get("area_ratio") is not None]
        ok = [r for r in with_dims if abs(r["area_ratio"] - 1) <= 0.10]
        out.append({
            "pdf": pdfname, "page": page, "flat_id": None, "rooms": rr, "entrance_room": entrance,
            "entrance_via": via, "bhk": sum(1 for t in types if t in BED_TYPES),
            "common_rooms": [{"id": c, "type": by_id[c]["type"], "name": by_id[c]["name"]} for c in commons],
            "n_dims": len(with_dims), "n_dims_ok10": len(ok),
            "n_unlabeled": sum(1 for t in types if t == "unlabeled"),
            "suspect_merge": len(comp) > 14 or sum(by_id[i]["area"] for i in comp) > 260,
        })
    return out, dropped


# --------------------------------------------------------------------------------------------------
# page driver
# --------------------------------------------------------------------------------------------------


def calibrate(regions, seeds, rho):
    """Median pt->m ratio from sqrt(area) ratios; also returns the sample list."""
    vals = []
    for i, st in regions.items():
        sd = seeds[i]
        if sd["dims"] is None or st is None or st["leaky"]:
            continue
        a, b = sd["dims"]
        area_pt = st["area_px"] / (rho * rho)
        if area_pt > 0:
            vals.append(math.sqrt(a * b / area_pt))
    return vals


def run_regions(lab, n):
    """Per-seed hole-filled pixel area and bounding box from the label image."""
    from scipy.ndimage import find_objects

    objs = find_objects(lab, max_label=n + 1)
    regions = {}
    for i in range(n):
        sl = objs[i]
        st = region_stats(lab[sl] == i + 1) if sl is not None else None
        if st is None:
            regions[i] = None
            continue
        area, w, h, y0, x0 = st
        regions[i] = {"area_px": area, "w": w, "h": h, "y0": y0 + sl[0].start, "x0": x0 + sl[1].start,
                      "leaky": False}
    return regions


def process_page(args):
    pdf, pno = args
    name = Path(pdf).parent.name
    try:
        return _process_page(pdf, pno, name)
    except Exception as e:  # noqa: BLE001
        return [{"type": "_page_done", "pdf": name, "page": pno, "status": "error", "error": repr(e)[:200]}]


def _process_page(pdf, pno, name):
    import pymupdf

    doc = pymupdf.open(pdf)
    page = doc[pno]
    drawings = page.get_drawings()
    lines, digit_h, vert = ocr_lines(page, drawings)
    title = " ".join(ln[0] for ln in lines if "PLAN" in ln[0].upper())[:80]
    seeds = seeds_from_lines(lines)
    if vert:
        seeds = combine_seeds(seeds, [unrotate_seed(v, vert[1]) for v in seeds_from_lines(vert[0])])
    n_dim = sum(1 for s in seeds if s["dims"])
    marker = {"type": "_page_done", "pdf": name, "page": pno, "title": title, "n_seeds": len(seeds),
              "n_dim_seeds": n_dim}
    if n_dim < 3:
        return [{**marker, "status": "not_plan"}]
    segs, pieces = load_vectors(drawings)
    del drawings
    if len(segs) < 200:
        return [{**marker, "status": "no_vectors"}]
    theta = dominant_angle(segs)
    cen = (page.rect.width / 2, page.rect.height / 2)
    sp = np.array([[s["x"], s["y"]] for s in seeds])
    if theta:
        segs = np.concatenate([rotate_pts(segs[:, :2], theta, cen), rotate_pts(segs[:, 2:], theta, cen)], 1)
        pieces = [rotate_pts(p, theta, cen) for p in pieces]
        sp = rotate_pts(sp, theta, cen)
    osegs = orth_segments(segs)
    if len(osegs) < 100:
        return [{**marker, "status": "no_orth_walls"}]
    margin = 150.0
    bx0, by0 = sp.min(0) - margin
    bx1, by1 = sp.max(0) + margin
    inside = (osegs[:, [0, 2]].max(1) >= bx0) & (osegs[:, [0, 2]].min(1) <= bx1) & \
             (osegs[:, [1, 3]].max(1) >= by0) & (osegs[:, [1, 3]].min(1) <= by1)
    osegs = osegs[inside]

    # Digit height x scale is ~0.245 m on these sheets (CAD text height is fixed in drawing units),
    # which gives the first scale guess; the dimension labels then calibrate it.
    s = TEXT_H_M / digit_h if digit_h else 0.02
    s0 = s
    res = None
    for _ in range(3):
        res = segment_page(osegs, seeds, sp, (bx0, by0, bx1, by1), s)
        if res is None:
            return [{**marker, "status": "seg_failed"}]
        if abs(res["scale"] / s - 1) < 0.10:
            s = res["scale"]
            break
        s = res["scale"]
        res = None
    if res is None:  # did not settle: segment once more at the last estimate
        res = segment_page(osegs, seeds, sp, (bx0, by0, bx1, by1), s)
        if res is None:
            return [{**marker, "status": "seg_failed"}]
        s = res["scale"]
    samples = res["samples"]
    marker.update(scale_m_per_pt=round(s, 5), scale_guess=round(s0, 5), text_h_pt=digit_h and round(digit_h, 2), rotation_deg=round(theta, 2),
                  n_scale_samples=len(samples))
    med = float(np.median(samples))
    marker["scale_mad"] = round(float(np.median(np.abs(np.asarray(samples) - med)) / med), 3)
    if os.environ.get("TOPO_DEBUG"):
        _DEBUG.update(walls=res["walls"], free=res["free"], lab=res["lab"], snapped=res["snapped"],
                      grid=res["grid"], seeds=seeds, s=s)
    return _finish_page(name, pno, marker, seeds, sp, res, pieces, s)


def seed_marker(free, m, sd, s, rho):
    """Free pixels under the label text box (clipped to ~1 m) connected to the snapped point."""
    from scipy.ndimage import label

    if m is None:
        return None
    hw = int(min(sd["w"] / 2, 0.5 / s) * rho) + 1
    hh = int(min(sd["h"] / 2, 0.3 / s) * rho) + 1
    h, w = free.shape
    x0, x1 = max(0, m[0] - hw), min(w, m[0] + hw + 1)
    y0, y1 = max(0, m[1] - hh), min(h, m[1] + hh + 1)
    sub = free[y0:y1, x0:x1]
    comp, _ = label(sub)
    c = comp[m[1] - y0, m[0] - x0]
    if c == 0:
        return np.array([m[1]]), np.array([m[0]])
    ys, xs = np.nonzero(comp == c)
    return ys + y0, xs + x0


def rebalance(lab, n, seeds, snapped, s_px, min_touch_px):
    """Split open-plan space between touching seeds by a size-weighted Voronoi.

    The DT flood hands a whole open living/dining space to whichever seed sits on the DT peak. For
    touching seed pairs where one region is much bigger than its OCR dimensions and the other much
    smaller, their union is re-split by distance / sqrt(expected area).
    """
    touch, _ = adjacency(lab, 1, n + 3)
    exp_px = {i: sd["dims"][0] * sd["dims"][1] / (s_px * s_px) for i, sd in enumerate(seeds) if sd["dims"]}
    for _ in range(2):
        areas = np.bincount(lab.ravel(), minlength=n + 2)
        changed = False
        for (a, b), c in sorted(touch.items(), key=lambda kv: -kv[1]):
            if c < min_touch_px or a not in exp_px or b not in exp_px or snapped[a] is None or snapped[b] is None:
                continue
            ra, rb = areas[a + 1] / exp_px[a], areas[b + 1] / exp_px[b]
            if not ((ra > 1.3 and rb < 0.8) or (rb > 1.3 and ra < 0.8)):
                continue
            m = (lab == a + 1) | (lab == b + 1)
            ys, xs = np.nonzero(m)
            da = np.hypot(xs - snapped[a][0], ys - snapped[a][1]) / math.sqrt(exp_px[a])
            db = np.hypot(xs - snapped[b][0], ys - snapped[b][1]) / math.sqrt(exp_px[b])
            lab[ys, xs] = np.where(da <= db, a + 1, b + 1)
            areas = np.bincount(lab.ravel(), minlength=n + 2)
            changed = True
        if not changed:
            break
    return lab


def segment_page(osegs, seeds, sp, bbox, s):
    """Walls, free space and seeded room segmentation at provisional scale s (m per pt)."""
    from PIL import Image, ImageDraw
    from shapely.geometry import MultiPoint

    bx0, by0, bx1, by1 = bbox
    rho = float(np.clip(s / 0.03, 0.3, 1.5))
    rho = min(rho, math.sqrt(MAX_PIXELS / max(1.0, (bx1 - bx0) * (by1 - by0))))  # bound memory and time
    grid = Grid(bx0, by0, bx1, by1, rho)
    walls = wall_mask(grid, osegs, s)
    free = open_free(~walls, max(3, int(round(0.10 / s * rho)) | 1))
    n = len(seeds)
    rad = max(2, int(round(0.5 / s * rho)))
    px, py = grid.px(sp)
    snapped = [snap_free(free, x, y, rad) for x, y in zip(px, py, strict=True)]
    if sum(m is not None for m in snapped) < 3:
        return None
    marker_px = [seed_marker(free, m, sd, s, rho) for m, sd in zip(snapped, seeds, strict=True)]
    hull = MultiPoint([(float(x), float(y)) for x, y in zip(px, py, strict=True)]).convex_hull
    hull = hull.buffer(1.5 / s * rho)
    img = Image.new("L", (grid.w, grid.h), 0)
    if hull.geom_type == "Polygon":
        ImageDraw.Draw(img).polygon(list(hull.exterior.coords), fill=1)
    ext = ~np.asarray(img, dtype=bool)
    kghost = max(5, int(round(1.1 / s * rho)))
    tmin = max(2, int(round(0.18 / s * rho)))
    lab, _dt = segment_rooms(free, marker_px, ext, kghost, tmin)
    regions = run_regions(lab, n)
    leaky = _ext_leaky(lab, n)
    for i, st in regions.items():
        if st is not None:
            st["leaky"] = i in leaky
    vals = calibrate(regions, seeds, rho)
    if len(vals) < 3:
        return None
    s_cal = float(np.median(vals))
    lab = rebalance(lab, n, seeds, snapped, s_cal / rho, max(3, int(round(0.55 / s_cal * rho))))
    regions = run_regions(lab, n)
    for i, st in regions.items():
        if st is not None:
            st["leaky"] = i in leaky
    vals = calibrate(regions, seeds, rho)
    if len(vals) < 3:
        return None
    return {"scale": float(np.median(vals)), "samples": vals, "lab": lab, "regions": regions, "snapped": snapped,
            "grid": grid, "walls": walls, "free": free, "n": n, "rho": rho}


def _ext_leaky(lab, n):
    """Seeds whose region is directly adjacent (no wall) to the exterior label."""
    ext = n + 1
    out = set()
    for axis in (0, 1):
        a = lab if axis == 1 else lab.T
        l, r = a[:, :-1], a[:, 1:]
        sel = ((l == ext) & (r > 0) & (r <= n)) | ((r == ext) & (l > 0) & (l <= n))
        if sel.any():
            vals = np.where(l[sel] == ext, r[sel], l[sel])
            out.update((np.unique(vals) - 1).tolist())
    return out


def _ghost_rooms(lab, n, px_m, rooms, idmap):
    """Enclosed free-space cores with no label (OCR missed it): nodes of type 'unlabeled'."""
    from scipy.ndimage import find_objects

    objs = find_objects(lab)
    for lb in range(n + 2, len(objs) + 1):
        sl = objs[lb - 1]
        if sl is None:
            continue
        st = region_stats(lab[sl] == lb)
        if st is None:
            continue
        area_px, w, h, y0, x0 = st
        area = area_px * px_m * px_m
        if not (1.2 <= area <= 60) or min(w, h) * px_m < 0.9:
            continue
        r = {"id": len(rooms), "type": "unlabeled", "name": None, "area": round(area, 2),
             "short": round(min(w, h) * px_m, 2), "long": round(max(w, h) * px_m, 2), "ocr_dims": None,
             "area_ratio": None, "dims_ok": None, "label_width": None, "exterior": False, "neighbours": [],
             "doors": [], "open": [], "shaft": False, "common": False,
             "cx": round(float((x0 + sl[1].start + w / 2) * px_m), 2),
             "cy": round(float((y0 + sl[0].start + h / 2) * px_m), 2)}
        idmap[lb - 1] = r["id"]
        rooms.append(r)


def _finish_page(name, pno, marker, seeds, sp, res, pieces, s):
    lab, n, regions, grid, rho = res["lab"], res["n"], res["regions"], res["grid"], res["rho"]
    px_m = s / rho  # metres per pixel
    # ---- per-room geometry
    rooms = []
    idmap = {}
    ratios_all = []
    n_demoted = 0
    for i, sd in enumerate(seeds):
        st = regions.get(i)
        if sd["dims"] and st is not None:
            ratios_all.append((sd["type"], st["area_px"] * px_m * px_m / (sd["dims"][0] * sd["dims"][1])))
        if st is None or sd["type"] == "shaft":
            continue
        area = st["area_px"] * px_m * px_m
        w, h = st["w"] * px_m, st["h"] * px_m
        r = {"id": len(rooms), "type": sd["type"] or "unknown", "name": sd["name"],
             "area": round(area, 2), "short": round(min(w, h), 2), "long": round(max(w, h), 2),
             "ocr_dims": list(sd["dims"]) if sd["dims"] else None, "area_ratio": None, "dims_ok": None,
             "label_width": sd.get("lwidth"), "exterior": False, "neighbours": [], "doors": [], "open": [],
             "shaft": False, "common": False,
             "cx": round(float((st["x0"] + st["w"] / 2) * px_m), 2),
             "cy": round(float((st["y0"] + st["h"] / 2) * px_m), 2)}
        if sd["dims"]:
            a, b = sd["dims"]
            ratio = area / (a * b)
            if sd["type"] is None and not 0.75 <= ratio <= 1.3:
                # a dimension string with no name whose region disagrees is more likely a misread or a
                # wall dimension than a room: keep the region as an anonymous node, drop the claim
                r["type"], r["rejected_dims"] = "unlabeled", [a, b]
                r["ocr_dims"] = None
                n_demoted += 1
            else:
                r["area_ratio"] = round(ratio, 3)
                r["dims_ok"] = bool(abs(ratio - 1) <= 0.10)
                r["dims_ok_wh"] = bool(abs(r["short"] / min(a, b) - 1) <= 0.10 and
                                       abs(r["long"] / max(a, b) - 1) <= 0.10)
        idmap[i] = r["id"]
        rooms.append(r)
    dimrooms = [r for r in rooms if r["area_ratio"] is not None]
    fid25 = sum(1 for r in dimrooms if abs(r["area_ratio"] - 1) <= 0.25) / max(1, len(dimrooms))
    marker.update(fidelity25=round(fid25, 3), n_rooms=len(rooms), n_unknown_demoted=n_demoted,
                  n_dim_seeds_seg=len(ratios_all),
                  n_seed_ok10=sum(1 for _t, rt in ratios_all if abs(rt - 1) <= 0.10),
                  n_rooms_dims=len(dimrooms), n_rooms_ok10=sum(1 for r in dimrooms if r["dims_ok"]))
    if len(rooms) < 3 or len(dimrooms) < 3:
        return [{**marker, "status": "too_few_rooms"}]
    if fid25 < 0.5:  # page-level fidelity gate: skip garbage pages
        return [{**marker, "status": "low_fidelity"}]
    _ghost_rooms(lab, n, px_m, rooms, idmap)
    for r in rooms:
        r["common"] = is_common(r)

    # ---- adjacency
    gap_px = max(2, int(round(0.42 / s * rho)))
    ncode = int(lab.max()) + 2
    touch, wall = adjacency(lab, gap_px, ncode)
    ext_id = n  # label n+1 -> index n
    min_wall_px = max(3, int(round(0.30 / px_m)))
    min_open_px = max(3, int(round(0.55 / px_m)))
    links = {}
    for (a, b), c in wall.items():
        if c < min_wall_px:
            continue
        if b == ext_id and a in idmap:
            rooms[idmap[a]]["exterior"] = True
        elif a in idmap and b in idmap:
            ra, rb = rooms[idmap[a]], rooms[idmap[b]]
            ra["neighbours"].append(rb["id"])
            rb["neighbours"].append(ra["id"])
    for (a, b), c in touch.items():
        if b == ext_id and a in idmap:
            rooms[idmap[a]]["exterior"] = True
            continue
        if a in idmap and b in idmap and c >= min_open_px:
            ra, rb = rooms[idmap[a]], rooms[idmap[b]]
            ra["open"].append(rb["id"])
            rb["open"].append(ra["id"])
            ra["neighbours"].append(rb["id"])
            rb["neighbours"].append(ra["id"])
            links[(min(ra["id"], rb["id"]), max(ra["id"], rb["id"]))] = "open"
    # ---- doors from swing arcs
    arcs = fit_door_arcs(cluster_pieces(pieces), s)
    dl = door_links(arcs, lab, grid, s, n)
    n_doors = 0
    for (a, b), _c in dl.items():
        if a in idmap and b in idmap:
            ra, rb = rooms[idmap[a]], rooms[idmap[b]]
            ra["doors"].append(rb["id"])
            rb["doors"].append(ra["id"])
            links[(min(ra["id"], rb["id"]), max(ra["id"], rb["id"]))] = "door"
            n_doors += 1
    marker["n_door_arcs"] = len(arcs)
    marker["n_door_links"] = n_doors
    # ---- shaft / duct labels near rooms
    wrad = max(2, int(round(0.75 / px_m)))
    for i in range(n):
        if seeds[i]["type"] != "shaft":
            continue
        x, y = _px_of(grid, sp[i])
        x, y = int(round(x)), int(round(y))
        win = lab[max(0, y - wrad): y + wrad + 1, max(0, x - wrad): x + wrad + 1]
        for lb in np.unique(win):
            if lb > 0 and (int(lb) - 1) in idmap:
                rooms[idmap[int(lb) - 1]]["shaft"] = True
    for r in rooms:
        r["neighbours"] = sorted(set(r["neighbours"]))
        r["doors"] = sorted(set(r["doors"]))
        r["open"] = sorted(set(r["open"]))
    flats, dropped = build_flats(rooms, links, name, pno)
    for k, f in enumerate(flats):
        f["flat_id"] = f"{name}:p{pno}:F{k + 1}"
        f["title"] = marker["title"]
        f["scale_m_per_pt"] = marker["scale_m_per_pt"]
    marker["status"] = "ok"
    marker["n_flats"] = len(flats)
    marker["n_flats_dropped"] = dropped
    return [*flats, marker]


def _px_of(grid, pt):
    x, y = grid.px(pt)
    return float(x), float(y)


# --------------------------------------------------------------------------------------------------
# driver
# --------------------------------------------------------------------------------------------------


def main() -> None:
    import argparse
    from concurrent.futures import ProcessPoolExecutor

    import pymupdf

    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("root", type=Path, help="dataset root containing <project>/<project>.pdf")
    ap.add_argument("out", type=Path, help="output JSONL (appended to; resumable)")
    ap.add_argument("workers", type=int, nargs="?", default=MAX_WORKERS)
    ap.add_argument("--limit", type=int, default=None, help="process at most N pending pages")
    a = ap.parse_args()
    workers = max(1, min(a.workers, MAX_WORKERS))
    jobs = []
    for pdf in sorted(a.root.glob("*/*.pdf")):
        n = pymupdf.open(str(pdf)).page_count
        jobs += [(str(pdf), i) for i in range(n)]
    done: set[tuple[str, int]] = set()
    if a.out.exists():
        for line in a.out.read_text().splitlines():
            r = json.loads(line)
            if r.get("type") == "_page_done":
                done.add((r["pdf"], r["page"]))
    jobs = [j for j in jobs if (Path(j[0]).parent.name, j[1]) not in done]
    if a.limit:
        jobs = jobs[: a.limit]
    print(len(jobs), "pages to do,", len(done), "done,", workers, "workers", file=sys.stderr)
    with ProcessPoolExecutor(workers) as ex, open(a.out, "a") as fh:
        for k, rows in enumerate(ex.map(process_page, jobs, chunksize=1)):
            for r in rows:
                fh.write(json.dumps(r) + "\n")
            fh.flush()
            if k % 10 == 0:
                print(k, file=sys.stderr, flush=True)


if __name__ == "__main__":
    main()
