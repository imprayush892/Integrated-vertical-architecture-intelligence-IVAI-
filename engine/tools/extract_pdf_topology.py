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
    (r"STRETCHER", "lift"),  # "STRETCHER LIFT" with the second word lost
    (r"SM[OA]CK\s*CHECK|SMOKE\s*CHECK", "lobby"),
    (r"MULTI\s*PURPOSE|M\.?P\.?R\b|REHAB\s*ROOM", "multipurpose"),
    (r"ALCOVE", "alcove"),
    (r"FOYER|ENTRANCE", "foyer"),
    (r"\bDECK\b", "balcony"),
    (r"\bM\.?\s*V\.?\s*S\b|VENTILATION\s*SHAFT|\bO\.?T\.?S\b", "shaft"),
]
# non-residential spaces drawn on residential floors (refuge floors, podium amenities, schools on rehab
# floors, parking towers): never part of a flat and not part of the core
NONRES = re.compile(r"REFUGE|PARKING|CAR\s*LIFT|STILT|SOCIETY|FITNESS|\bGYM|CLUB\s*HOUSE|SCHOOL|CLASS\s*ROOM|"
                    r"PRINCIPAL|RECEPTION|WAITING|STAFF|SHOP|SUB\s*-?\s*STATION|METER\s*(ROOM|PANEL)|"
                    r"ELECTRICAL\s*METER|D\.?\s*G\.?\s*SET|PUMP\s*ROOM|SECURITY|WATCHMAN|DRIVER|ROOF|"
                    r"CUT\s*-?\s*OUT|\bVOID\b|\bO\.?T\.?S\b|SPACE\s*FOR", re.I)
ROOM_WORD = re.compile(r"BED|KIT|LIV|TOIL|BATH|DIN|DRAW|HALL|PASS", re.I)


# OCR splits of common labels ("| IVING", "ILET", "TOI.") tried only when nothing else matched
FALLBACK_TYPES = [
    (r"IVING|LIVIN|\bIVIN\b", "living"), (r"ILET\b|\bTOI\b|OILET", "toilet"), (r"ITCHEN|KITCH", "kitchen"),
    (r"EDROOM|EDRM", "bedroom"), (r"\bIFT\b", "lift"), (r"^\W*ECK\b", "balcony"),
]
NOT_ROOM = re.compile(r"BUILDING|BLDG|BOUNDARY|SET\s*BACK|ROAD|PLOT", re.I)


def classify2(text: str) -> str | None:
    t = text.upper()
    if NOT_ROOM.search(t):  # "BUILDING LINE ABOVE" matched DIN -> dining in v1
        t = NOT_ROOM.sub(" ", t)
        if not re.search(r"[A-Z]{3,}", t):
            return None
    if NONRES.search(t) and not re.search(r"DUCT|SHAFT", t) and not (ROOM_WORD.search(t) and "ROOF" in t):
        return "nonres"
    core_m = re.search(r"EVACU|FIRE|LIFT|LOBBY|STAIR|SM[OA]CK", t)
    room_m = [m for m in ROOM_WORD.finditer(t) if core_m and m.start() > core_m.end()]
    if room_m:
        t = t[room_m[-1].start():]
    elif core_m and re.match(r"[\W\d]*(TOIL|BATH|BED|KITCH)", t[:core_m.start()]):
        # "TOILET EVACUATION ...": a room label with the neighbouring fire-lift label merged after it
        t = t[:core_m.start()]
    if re.search(r"TOIL|\bW\.?\s?C\b", t):  # "(MVS) P.TOIL": a powder toilet on a ventilation shaft
        t = re.sub(r"\bM\.?\s*V\.?\s*S\b", " ", t)
    for pat, ty in EXTRA_TYPES:
        if re.search(pat, t):
            return ty
    if re.search(r"CHAJJ?A", t):  # "BEDROOM-1 ... CHAJJA": a neighbouring chajja label merged in
        if not ROOM_WORD.search(t):
            return "chajja"
        t = re.sub(r"CHAJJ?A", " ", t)
    ty = classify(t)
    if ty == "dining" and not re.search(r"\bDIN", t):
        ty = None
    if ty is None:
        for pat, ty2 in FALLBACK_TYPES:
            if re.search(pat, t):
                return ty2
    return ty


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


def door_links(arcs, lab, grid, s, n, pos=None):
    """Room pairs joined by door arcs. lab is the watershed label image (1..n rooms, n+1 exterior,
    n+2.. unlabelled cells). Keys are label-1. pos (optional dict) receives the door point (rotated pt)."""
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
            if a and b and a != b and a != n + 1 and b != n + 1:
                links[(min(a, b) - 1, max(a, b) - 1)] += 1
                if pos is not None:
                    pos[(min(a, b) - 1, max(a, b) - 1)] = (float(mx), float(my))
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


def facade_reach(lab, ext, block, k_px):
    """fac[label, d] = boundary pixels of each label from which the outside (ext) is reached within k_px steps
    in direction d (top, bottom, left, right of the image) across non-blocking pixels only (walls, chajjas,
    ledges, ducts). Rooms block, so a room behind another room never gets that facade."""
    free = ~block[lab]
    nl = int(lab.max()) + 2
    fac = np.zeros((nl, 4), np.int64)
    roomish = block[lab]

    def shift(g, j):  # value at the neighbour in direction j
        out = np.zeros_like(g)
        if j == 0:
            out[1:, :] = g[:-1, :]
        elif j == 1:
            out[:-1, :] = g[1:, :]
        elif j == 2:
            out[:, 1:] = g[:, :-1]
        else:
            out[:, :-1] = g[:, 1:]
        return out

    for j in range(4):
        g = ext.copy()
        for _ in range(k_px):
            g = ext | (free & shift(g, j))
        m = shift(g, j) & roomish
        if m.any():
            fac[:, j] += np.bincount(lab[m].astype(np.int64), minlength=nl)[:nl]
    return fac


def adjacency_pos(lab, gap_px, xlab):
    """Label pairs facing each other across at most gap_px unlabelled (wall) pixels, with positions.

    Returns (acc, fac): acc[(a, b)] = [n_touch, n_wall, sx_touch, sy_touch, sx_wall, sy_wall, sum_k_wall]
    for raw label values a < b (pixel pairs at distance 1 are "touch", 2..gap_px are "wall"; midpoints
    are summed in image x/y, sum_k_wall / n_wall is the wall thickness in pixels); fac[label] = pixel counts of the label's
    boundary facing xlab (the outside) towards top, bottom, left, right of the drawing.
    """
    acc: dict = {}
    nl = int(max(int(lab.max()), xlab)) + 2
    fac = np.zeros((nl, 4), np.int64)
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
            rr, cc = np.nonzero(sel)
            la, lb = left[rr, cc].astype(np.int64), right[rr, cc].astype(np.int64)
            if axis == 1:
                xs, ys = cc + k / 2.0, rr.astype(np.float64)
            else:
                xs, ys = rr.astype(np.float64), cc + k / 2.0
            lo, hi = np.minimum(la, lb), np.maximum(la, lb)
            key = lo * nl + hi
            uk, inv, cnt = np.unique(key, return_inverse=True, return_counts=True)
            sx = np.bincount(inv, weights=xs)
            sy = np.bincount(inv, weights=ys)
            for q, kk in enumerate(uk.tolist()):
                e = acc.setdefault((kk // nl, kk % nl), [0, 0, 0.0, 0.0, 0.0, 0.0, 0.0])
                j = 0 if k == 1 else 1
                e[j] += int(cnt[q])
                e[2 + 2 * j] += float(sx[q])
                e[3 + 2 * j] += float(sy[q])
                if k > 1:
                    e[6] += float(k * cnt[q])
            # facing the outside: room on the left side and outside on the right -> faces +axis
            plus = 3 if axis == 1 else 1  # right / bottom
            minus = 2 if axis == 1 else 0  # left / top
            m1 = (lb == xlab) & (la != xlab)
            m2 = (la == xlab) & (lb != xlab)
            if m1.any():
                np.add.at(fac[:, plus], la[m1], 1)
            if m2.any():
                np.add.at(fac[:, minus], lb[m2], 1)
    return acc, fac


# --------------------------------------------------------------------------------------------------
# flats
# --------------------------------------------------------------------------------------------------

LIFTS = {"lift", "fire_lift"}
NOT_PRIVATE = {"shaft", "chajja", "nonres", "shaft_cell"}
CORE_OK = {"lobby", "passage", "unlabeled", "unknown", "foyer", "lift", "fire_lift", "stair"}
ENTRY_TYPES = {"foyer", "living", "dining", "passage", "unlabeled", "unknown"}
ENTRY_PREF = {"foyer": 0, "living": 1, "dining": 2, "passage": 3, "unlabeled": 4, "unknown": 4}


def is_common(room):
    """Lobby, lift, stair and wide passages are common. v2: the passage width comes from the label
    (M.W. width, else the smaller printed dimension) or the inscribed width, never the bounding box."""
    ty = room["type"]
    if ty in ROOM_TYPES_COMMON:
        return True
    if ty == "passage":
        if re.search(r"COMMON|CORRIDOR|\bCORR\b", (room.get("name") or "").upper()):
            return True
        if room.get("label_width"):
            return room["label_width"] >= 1.2
        if room.get("ocr_dims"):
            return min(room["ocr_dims"]) >= 1.35
        w = room.get("wclear") or room["short"]
        return w >= 1.35 or room["area"] >= 14
    return False


def _width(room):
    """Clear width: the smaller printed dimension, else the inscribed width of the region."""
    if room.get("label_width"):
        return room["label_width"]
    if room.get("ocr_dims"):
        return min(room["ocr_dims"])
    return room.get("wclear") or room["short"]


def bad_link(ta, tb):
    """Room-type pairs that are not joined by a door in these plans (audit: links across party walls,
    from lifts into flats, bedroom to bedroom, toilet or bedroom to kitchen)."""
    s = {ta, tb}
    if s & LIFTS:
        return not s <= CORE_OK
    if "stair" in s:
        return not s <= CORE_OK
    if ta == tb and ta in ("living", "kitchen", "toilet"):
        return True
    if s <= BED_TYPES:
        return True
    if "kitchen" in s and s & (BED_TYPES | {"toilet"}):
        return True
    return False


def mark_common(rooms, links, touch_len):
    """Set room["common"]. Unlabelled cells and passages that hang off the core are common when they
    touch a lift or stair, serve two living rooms, or open on to the lobby over a width no flat door has."""
    by_id = {r["id"]: r for r in rooms}
    nbr = defaultdict(set)
    for a, b in links:
        nbr[a].add(b)
        nbr[b].add(a)
    for r in rooms:
        r["common"] = is_common(r)
    for _ in range(4):
        changed = False
        for r in rooms:
            if r["common"] or r["type"] not in ("unlabeled", "unknown", "passage"):
                continue
            if r["type"] == "unknown" and r.get("ocr_dims") and min(r["ocr_dims"]) >= 2.7 and \
                    r["area"] >= 12 and r.get("area_ratio") and abs(r["area_ratio"] - 1) <= 0.25:
                continue  # printed "5.05 X 3.05" with the name lost: a room (lobbies are < 2.7 m wide)
            lk = [by_id[v] for v in nbr[r["id"]]]
            com = [v for v in lk if v["common"]]
            if not com:
                continue
            why = None
            if any(v["type"] in LIFTS | {"stair"} for v in lk):
                why = "touches_lift_or_stair"
            elif sum(1 for v in lk if v["type"] in ("living", "foyer")) >= 2:
                why = "serves_two_flats"
            elif max(touch_len.get((min(r["id"], v["id"]), max(r["id"], v["id"])), 0.0) for v in com) >= 1.3 \
                    and (_width(r) >= 1.4 or not any(v["type"] in BED_TYPES | {"toilet", "kitchen"} for v in lk)) \
                    and sum(1 for v in lk if not v["common"] and v["type"] in ("living", "dining", "alcove", "foyer")
                            and touch_len.get((min(r["id"], v["id"]), max(r["id"], v["id"])), 0.0) >= 1.3) != 1:
                # (a cell open over >= 1.3 m on exactly one flat's living / dining / alcove is its foyer)
                why = "wide_open_to_lobby"
            if why:
                r["common"], r["common_why"], changed = True, why, True
        if not changed:
            break


def _centroid(r):
    return (r.get("cxm", r["cx"]), r.get("cym", r["cy"]))


def build_flats(rooms, links, pdfname, page, wall_info=None, link_pos=None):
    """Group private rooms into flats. Returns (flats, n_dropped).

    v2 (audit fixes): links of implausible room-type pairs are ignored; each living room (or living
    rooms joined by an open link) anchors one flat and every private room goes to the anchor it reaches
    in the fewest link hops (ties by distance), so one spurious link no longer merges two apartments;
    link-less fragments without a living room are attached to the flat they share most wall with
    (unless that would give the flat a second kitchen or living room); private passages and cells
    are absorbed the same way.
    """
    wall_info = wall_info or {}
    link_pos = link_pos or {}
    by_id = {r["id"]: r for r in rooms}
    nbr = defaultdict(set)
    for a, b in links:
        nbr[a].add(b)
        nbr[b].add(a)
    private = {r["id"] for r in rooms if not r.get("common") and r["type"] not in NOT_PRIVATE}
    # ---- anchors: living rooms, merged when joined by an open link (living + drawing / dining hall).
    # Also anchors: multipurpose rooms of PAP / rehab tenements (their living room), and large unnamed cells
    # that open on the core and on private rooms (a living room whose label was lost); without an anchor
    # such a flat is swallowed by its neighbour through any surviving link.
    def lost_living(r):
        if r["type"] not in ("unlabeled", "unknown") or r["area"] < 12 or (r.get("wclear") or r["short"]) < 2.5:
            return False
        lk = [by_id[v] for v in nbr[r["id"]]]
        return any(v.get("common") and v["type"] not in LIFTS | {"stair"} for v in lk) and \
            not any(v["type"] in LIFTS | {"stair"} for v in lk) and \
            sum(1 for v in lk if not v.get("common") and v["type"] not in ("living", "chajja", "nonres")) >= 2

    livings = sorted(i for i in private if by_id[i]["type"] == "living"
                     or (by_id[i]["type"] == "multipurpose" and by_id[i]["area"] <= 25) or lost_living(by_id[i]))
    for i in livings:
        if by_id[i]["type"] != "living":
            by_id[i]["anchor_src"] = "multipurpose" if by_id[i]["type"] == "multipurpose" else "lost_label"
    par = {i: i for i in livings}

    def find(i):
        while par[i] != i:
            par[i] = par[par[i]]
            i = par[i]
        return i

    for a in livings:
        for b in livings:
            if a < b and links.get((a, b)) == "open" and not (by_id[a].get("anchor_src") or by_id[b].get("anchor_src")):
                par[find(a)] = find(b)
    owner = {i: find(i) for i in livings}
    acen = defaultdict(list)
    for i in livings:
        acen[owner[i]].append(_centroid(by_id[i]))
    acen = {k: np.mean(v, axis=0) for k, v in acen.items()}
    frontier = list(livings)
    while frontier:
        prop = defaultdict(set)
        for u in frontier:
            for v in nbr[u]:
                if v in private and v not in owner:
                    prop[v].add(owner[u])
        nxt = []
        for v, cands in prop.items():
            c = np.asarray(_centroid(by_id[v]))
            owner[v] = min(cands, key=lambda k: float(np.hypot(*(acen[k] - c))))
            nxt.append(v)
        frontier = nxt
    # ---- living-less components: a flat of their own if they look like one, else orphans
    rest = sorted(i for i in private if i not in owner)
    seen = set()
    orphans = []
    for rid in rest:
        if rid in seen:
            continue
        comp, q = [], deque([rid])
        seen.add(rid)
        while q:
            u = q.popleft()
            comp.append(u)
            for v in nbr[u]:
                if v in private and v not in owner and v not in seen:
                    seen.add(v)
                    q.append(v)
        types = {by_id[i]["type"] for i in comp}
        has_ent = any(by_id[v].get("common") and by_id[v]["type"] not in LIFTS for i in comp for v in nbr[i])
        if "kitchen" in types and (types & BED_TYPES or (has_ent and len(comp) >= 3)):
            for i in comp:
                owner[i] = ("c", min(comp))
        else:
            orphans.append(comp)
    # ---- attach orphans to the flat they share most wall with
    attached = defaultdict(list)
    for _ in range(4):
        left = []
        for comp in orphans:
            cs = set(comp)
            score = defaultdict(float)
            core_wall = 0.0
            if all(by_id[i]["type"] in ("unlabeled", "unknown") for i in comp) and \
                    any(by_id[i].get("_near_stair") for i in comp):
                left.append(comp)  # unnamed cells in the stair hall
                continue
            out_links = {v for i in comp for v in nbr[i] if v not in cs}
            if out_links and all(by_id[v]["type"] == "nonres" for v in out_links):
                left.append(comp)  # e.g. the toilet of a fitness centre: its door opens into that space
                continue
            for i in comp:
                for v in by_id[i]["neighbours"]:
                    if v in cs:
                        continue
                    ln, th = wall_info.get((min(i, v), max(i, v)), (0.0, 0.0))
                    if by_id[v].get("common"):
                        core_wall += ln
                    elif v in owner:
                        score[owner[v]] += ln * (1.0 if th <= 0.2 else 0.5)
            named = {by_id[i]["type"] for i in comp} - {"unlabeled", "unknown"}
            if not named and core_wall >= max(score.values(), default=0.0):
                left.append(comp)  # an unnamed cell against the core (lift well, duct, lobby nook)
                continue
            if all(by_id[i]["type"] == "unlabeled" and not by_id[i].get("rejected_dims") for i in comp) and \
                    not any(v in owner for v in out_links):
                left.append(comp)  # cells with no label at all touching a flat only through walls: ducts, strips
                continue
            best = None
            for k, sc in sorted(score.items(), key=lambda kv: -kv[1]):
                if sc < 0.5:
                    break
                members = [j for j, o in owner.items() if o == k]
                tt = Counter(by_id[j]["type"] for j in members + comp)
                if tt["kitchen"] > 1 or tt["living"] > max(1, Counter(by_id[j]["type"] for j in members)["living"]):
                    continue
                best = k
                break
            if best is None:
                left.append(comp)
                continue
            for i in comp:
                owner[i] = best
            attached[best] += comp
        if len(left) == len(orphans):
            break
        orphans = left
    groups = defaultdict(list)
    for i, k in owner.items():
        groups[k].append(i)
    flats = sorted((sorted(g) for g in groups.values()), key=lambda g: g[0])
    out, dropped = [], 0
    for comp in flats:
        cs = set(comp)
        # unnamed cells that are not rooms of this flat: chajja / ledge strips outside the outer wall
        # (exterior, reached from at most one room), voids and terraces (large), cells with no opening
        pruned = []
        changed = True
        while changed:
            changed = False
            for i in sorted(cs):
                r = by_id[i]
                if r["type"] != "unlabeled":
                    continue
                lk = [v for v in nbr[i] if v in cs]
                named = [v for v in lk if by_id[v]["type"] != "unlabeled"]
                was_room = bool(r.get("rejected_dims") or r.get("demoted_from") or r.get("anchor_src"))  # printed room / lost label
                strip = (r.get("wclear") or r["short"]) < 1.05 or \
                    sum((r.get("facade") or {}).values()) >= 0.6 * (r.get("perim") or 1e9)  # mostly open to outside
                if (r["area"] >= (25 if was_room else 15)
                        or (not lk and (r.get("exterior") or r["area"] >= 4) and not was_room)
                        or (r.get("exterior") and len(named) <= 1 and strip and not was_room)):
                    cs.discard(i)
                    pruned.append(i)
                    changed = True
        comp = sorted(cs)
        types = [by_id[i]["type"] for i in comp]
        if not any(t in HABITABLE for t in types) or len(comp) < 2:
            continue
        dr = [by_id[i] for i in comp if by_id[i]["area_ratio"] is not None]
        if len(dr) >= 2 and sum(1 for r in dr if abs(r["area_ratio"] - 1) > 0.25) > 0.5 * len(dr):
            dropped += 1  # geometry disagrees with most of its own labels: garbage, not emitted
            continue
        cand = []  # (room, common room) door/open links from the flat to the core
        def entry_ok(v):
            return by_id[v].get("common") and (by_id[v]["type"] not in LIFTS or by_id[v].get("leaky"))

        for i in comp:
            for v in nbr[i]:
                if entry_ok(v):
                    cand.append((i, v))
        via = "link" if cand else None
        if not cand:  # fall back to a shared wall with a common room
            for i in comp:
                for v in by_id[i]["neighbours"]:
                    if entry_ok(v):
                        cand.append((i, v))
            via = "wall" if cand else None
        cpref = {"lobby": 0, "passage": 1, "unlabeled": 2, "unknown": 2, "stair": 3}
        cand.sort(key=lambda iv: (ENTRY_PREF.get(by_id[iv[0]]["type"], 9), cpref.get(by_id[iv[1]]["type"], 5),
                                  -by_id[iv[0]]["area"]))
        ent_rooms = sorted({i for i, _v in cand if by_id[i]["type"] in ENTRY_TYPES})
        entrance = cand[0][0] if cand else None
        ent = None
        if cand:
            i, v = cand[0]
            key = (min(i, v), max(i, v))
            p = link_pos.get(key)
            ent = {"room": i, "common": v,
                   "common_type": by_id[v]["type"] + ("_leaky" if by_id[v].get("leaky") else ""), "via": via,
                   "kind": links.get(key, "wall"), "x": p[0] if p else None, "y": p[1] if p else None}
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
            r = {k: v for k, v in by_id[i].items() if not k.startswith("_")}
            r["hops"] = hops.get(i)
            r["neighbours"] = sorted(r["neighbours"])
            rr.append(r)
        commons = sorted({v for i in comp for v in nbr[i] if by_id[v].get("common")}
                         | {v for i in comp for v in by_id[i]["neighbours"] if by_id[v].get("common")})
        with_dims = [r for r in rr if r.get("area_ratio") is not None]
        ok = [r for r in with_dims if abs(r["area_ratio"] - 1) <= 0.10]
        tc = Counter(types)
        flat_links = []
        for (a, b), kind in links.items():
            if (a in cs or b in cs) and (a in cs or by_id[a].get("common")) and (b in cs or by_id[b].get("common")):
                p = link_pos.get((a, b))
                flat_links.append({"a": a, "b": b, "kind": kind, "x": p[0] if p else None, "y": p[1] if p else None})
        walls = []
        for i in comp:
            for v in by_id[i]["neighbours"]:
                if v in cs and i < v:
                    ln, th = wall_info.get((i, v), (0.0, 0.0))
                    walls.append({"a": i, "b": v, "len": round(ln, 2), "thick": round(th, 3)})
        out.append({
            "pdf": pdfname, "page": page, "flat_id": None, "rooms": rr, "entrance_room": entrance,
            "entrance_via": via, "entrance": ent, "n_entrance_rooms": len(ent_rooms),
            "bhk": sum(1 for t in types if t in BED_TYPES),
            "common_rooms": [{"id": c, "type": by_id[c]["type"], "name": by_id[c]["name"]} for c in commons],
            "links": flat_links, "walls": walls,
            "attached": sorted(set(attached.get(owner[comp[0]], [])) & cs) if comp else [], "pruned": sorted(pruned),
            "n_dims": len(with_dims), "n_dims_ok10": len(ok),
            "n_unlabeled": sum(1 for t in types if t == "unlabeled"),
            "suspect_merge": len(comp) > 30 or sum(by_id[i]["area"] for i in comp) > 350 or tc["kitchen"] > 1
            or tc["living"] > 2 or len(ent_rooms) > 2,
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
    ctx = {"lines": lines, "vert": vert, "theta": theta, "cen": cen, "page_h": page.rect.height, "osegs": osegs,
           "segs": segs}
    return _finish_page(name, pno, marker, seeds, sp, res, pieces, s, ctx)


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
    # v2: the exterior marker is only placed where the space is really outside the outer walls (free
    # space reachable from the sheet border); the hull test alone clipped corner rooms (audit mode 4)
    # Inside the hull, outside space far from any label (re-entrant notches) is exterior too; space
    # near a label is left to the label (balconies with a single-line railing leak to the outside).
    outside = outside_mask(walls, s, rho)
    img2 = Image.new("L", (grid.w, grid.h), 0)
    dr2 = ImageDraw.Draw(img2)
    rs = 2.5 / s * rho
    for x, y in zip(px, py, strict=True):
        dr2.ellipse((float(x) - rs, float(y) - rs, float(x) + rs, float(y) + rs), fill=1)
    ext = outside & (ext | ~np.asarray(img2, dtype=bool))
    kghost = max(5, int(round(1.1 / s * rho)))
    tmin = max(2, int(round(0.18 / s * rho)))
    lab, dt = segment_rooms(free, marker_px, ext, kghost, tmin)
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
            "grid": grid, "walls": walls, "free": free, "n": n, "rho": rho, "outside": outside, "dt": dt}


def outside_mask(walls, s, rho, close_m=0.35):
    """Free space outside the building: reachable from the raster border through gaps wider than
    2 x close_m (window and door openings narrower than that are treated as closed)."""
    from scipy.ndimage import distance_transform_edt, label

    r = max(2.0, close_m / s * rho)
    # window and door openings are bridged first: collinear wall pieces up to 2 m apart are joined
    # (this also fills narrow rooms, which does not matter here: only the outside is wanted)
    kl = max(3, int(round(2.0 / s * rho)))
    sealed = walls | _closing_1d(walls, kl, 0) | _closing_1d(walls, kl, 1)
    dw = distance_transform_edt(~sealed)
    del sealed
    comp, _nc = label(dw > r)
    border = np.unique(np.concatenate([comp[0], comp[-1], comp[:, 0], comp[:, -1]]))
    border = border[border > 0]
    core = np.isin(comp, border)
    del comp, dw
    if not core.any():
        return core
    d2 = distance_transform_edt(~core)
    return (d2 <= r + 1) & ~walls


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


def _ghost_rooms(lab, n, px_m, rooms, idmap, outside=None):
    """Enclosed free-space cores with no label (OCR missed it): nodes of type 'unlabeled'.
    v2: cells that lie mostly in the space outside the outer walls are not rooms."""
    from scipy.ndimage import find_objects

    objs = find_objects(lab)
    for lb in range(n + 2, len(objs) + 1):
        sl = objs[lb - 1]
        if sl is None:
            continue
        m = lab[sl] == lb
        if outside is not None and m.any() and float(outside[sl][m].mean()) > 0.6:
            continue
        st = region_stats(m)
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


def region_polygon(mask, tol_px=0.0):
    """Outer boundary of the largest connected part of a boolean mask as a shapely Polygon (pixel
    units, x = column, y = row); holes are dropped."""
    import shapely
    from shapely.geometry import Polygon

    h, w = mask.shape
    m8 = np.zeros((h, w + 2), np.int8)
    m8[:, 1:-1] = mask
    d = np.diff(m8, axis=1)
    ys, x0 = np.nonzero(d == 1)
    _, x1 = np.nonzero(d == -1)
    if not len(ys):
        return None
    g = shapely.union_all(shapely.box(x0, ys, x1, ys + 1))
    if g.geom_type == "MultiPolygon":
        g = max(g.geoms, key=lambda p: p.area)
    elif g.geom_type != "Polygon":
        return None
    g = Polygon(g.exterior)
    if tol_px:
        g = g.simplify(tol_px, preserve_topology=True)
    return g if g.is_valid and not g.is_empty else g.buffer(0)


def stair_flights(osegs, s):
    """Stair flights from the tread lines: runs of >= 6 parallel lines of equal length (0.7-2.4 m)
    at an even 0.18-0.38 m pitch. Returns boxes (x0, y0, x1, y1) in rotated pt. Treads are often
    paired closely enough to land in the wall mask, so stairs rarely appear as free space."""
    out = []
    if not len(osegs):
        return out
    d = osegs[:, 2:] - osegs[:, :2]
    hmask = np.abs(d[:, 0]) >= np.abs(d[:, 1])
    tol = 0.08 / s
    for horiz in (True, False):
        sg = osegs[hmask if horiz else ~hmask]
        if not len(sg):
            continue
        if horiz:
            a0, a1, pos = np.minimum(sg[:, 0], sg[:, 2]), np.maximum(sg[:, 0], sg[:, 2]), (sg[:, 1] + sg[:, 3]) / 2
        else:
            a0, a1, pos = np.minimum(sg[:, 1], sg[:, 3]), np.maximum(sg[:, 1], sg[:, 3]), (sg[:, 0] + sg[:, 2]) / 2
        ln = (a1 - a0) * s
        keep = (ln >= 0.7) & (ln <= 2.4)
        a0, a1, pos = a0[keep], a1[keep], pos[keep]
        order = np.argsort(a0)
        a0, a1, pos = a0[order], a1[order], pos[order]
        n = len(a0)
        par = list(range(n))

        def find(i, par=par):
            while par[i] != i:
                par[i] = par[par[i]]
                i = par[i]
            return i

        for i in range(n):
            j = i + 1
            while j < n and a0[j] - a0[i] < tol:
                if abs(a1[j] - a1[i]) < tol:
                    par[find(j)] = find(i)
                j += 1
        groups = defaultdict(list)
        for i in range(n):
            groups[find(i)].append(i)
        for g in groups.values():
            if len(g) < 6:
                continue
            p = np.unique(np.round(np.sort(pos[g]) * s / 0.03).astype(int)) * 0.03 / s  # dedupe double lines
            if len(p) < 6:
                continue
            dif = np.diff(p) * s
            ok = (dif >= 0.18) & (dif <= 0.38)
            start = 0
            for k in range(len(ok) + 1):
                if k == len(ok) or not ok[k] or (k > start and abs(dif[k] - np.median(dif[start:k])) > 0.06):
                    if k - start >= 5:  # >= 6 treads
                        lo, hi = float(np.median(a0[g])), float(np.median(a1[g]))
                        if horiz:
                            out.append((lo, float(p[start]), hi, float(p[k])))
                        else:
                            out.append((float(p[start]), lo, float(p[k]), hi))
                    start = k + 1
    return out


FACADE_DIRS = ("top", "bottom", "left", "right")
UPDN = re.compile(r"^(UP|DN|DOWN|U\.P|D\.N)\b\.?", re.I)


def _poly_coords(poly):
    return [[round(float(x), 2), round(float(y), 2)] for x, y in list(poly.exterior.coords)[:-1]]


def _finish_page(name, pno, marker, seeds, sp, res, pieces, s, ctx=None):
    from scipy.ndimage import binary_fill_holes, find_objects, label
    from shapely import affinity
    from shapely.geometry import Polygon as _P
    from shapely.geometry import box
    from shapely.ops import unary_union

    ctx = ctx or {}
    lab, n, regions, grid, rho = res["lab"], res["n"], res["regions"], res["grid"], res["rho"]
    outside, dt = res.get("outside"), res.get("dt")
    px_m = s / rho  # metres per pixel
    page_h = float(ctx.get("page_h", 0.0))

    def m_poly(poly_px):
        """Pixel polygon -> page metres (rotation-corrected drawing frame, x right, y up)."""
        p = affinity.affine_transform(poly_px, [s / rho, 0, 0, -s / rho, grid.x0 * s, (page_h - grid.y0) * s])
        return p

    def m_pt(x, y):
        return round((x / rho + grid.x0) * s, 2), round((page_h - (y / rho + grid.y0)) * s, 2)

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
    n_seed_rooms = len(rooms)
    _ghost_rooms(lab, n, px_m, rooms, idmap, outside)
    lab_of = {rid: key + 1 for key, rid in idmap.items()}
    objs = find_objects(lab)

    # ---- room polygons (page metres) and inscribed clear width
    tol = 0.05 / px_m
    for r in rooms:
        lb = lab_of[r["id"]]
        sl = objs[lb - 1] if lb - 1 < len(objs) else None
        if sl is None:
            continue
        m = lab[sl] == lb
        pg = region_polygon(m, tol)
        if pg is None or pg.is_empty or pg.geom_type != "Polygon":
            continue
        pg = affinity.translate(pg, sl[1].start, sl[0].start)
        r["_poly_px"] = pg
        pm = m_poly(pg)
        r["poly"] = _poly_coords(pm)
        r["perim"] = round(pm.length, 2)
        c = pm.centroid
        r["cxm"], r["cym"] = round(c.x, 2), round(c.y, 2)
        if dt is not None:
            r["wclear"] = round(2.0 * float(dt[sl][m].max()) * px_m, 2)

    # ---- building outline: everything not reachable from outside the outer walls, plus labelled rooms
    outline = []
    xlab = int(lab.max()) + 1
    if outside is not None:
        seedroom = np.zeros(xlab + 1, bool)
        for r in rooms[:n_seed_rooms]:
            if r["type"] not in ("chajja",):
                seedroom[lab_of[r["id"]]] = True
        fp = ~outside | seedroom[lab]
        k = max(3, int(round(0.25 / px_m)) | 1)
        fp = binary_fill_holes(open_free(fp, k))
        comp, _nc = label(fp)
        spx, spy = grid.px(sp)
        hh, ww = fp.shape
        cnt = Counter()
        for x, y in zip(spx, spy, strict=True):
            x, y = int(round(float(x))), int(round(float(y)))
            if 0 <= x < ww and 0 <= y < hh and comp[y, x]:
                cnt[int(comp[y, x])] += 1
        cobj = find_objects(comp)
        for cid, c in cnt.items():
            sl = cobj[cid - 1]
            m = comp[sl] == cid
            area = float(m.sum()) * px_m * px_m
            if c < 2 or area < 25:
                continue
            f = max(1, int(round(0.08 / px_m)))
            ms = m[::f, ::f]
            pg = region_polygon(ms, 0.12 / (px_m * f))
            if pg is None or pg.geom_type != "Polygon" or pg.is_empty:
                continue
            pg = affinity.affine_transform(pg, [f, 0, 0, f, sl[1].start, sl[0].start])
            pm = m_poly(pg)
            clipped = sl[0].start == 0 or sl[1].start == 0 or sl[0].stop >= hh or sl[1].stop >= ww
            outline.append({"polygon": _poly_coords(pm), "area": round(pm.area, 1), "n_labels": c,
                            "clipped": bool(clipped)})
        outline.sort(key=lambda o: -o["area"])
        del comp, fp

    # ---- unlabelled cells holding an UP / DN arrow text are stairs
    toks = []
    for t, x, y, w, h in ctx.get("lines") or []:
        if UPDN.match(t.strip()):
            toks.append((x + w / 2, y + h / 2))
    vert = ctx.get("vert")
    if vert:
        vx0, vy0, vx1, vy1 = vert[1]
        for t, x, y, w, h in vert[0]:
            if UPDN.match(t.strip()):
                toks.append((vx0 + (y + h / 2), vy0 + (vy1 - vy0) - (x + w / 2)))
    if toks:
        tp = np.asarray(toks, dtype=np.float64)
        if ctx.get("theta"):
            tp = rotate_pts(tp, ctx["theta"], ctx["cen"])
        txs, tys = grid.px(tp)
        rev = {lb: rid for rid, lb in lab_of.items()}
        hh, ww = lab.shape
        for x, y in zip(txs, tys, strict=True):
            x, y = int(round(float(x))), int(round(float(y)))
            if 0 <= x < ww and 0 <= y < hh and int(lab[y, x]) in rev:
                r = rooms[rev[int(lab[y, x])]]
                if r["type"] in ("unlabeled", "unknown"):
                    r["type"], r["type_src"] = "stair", "up_dn_text"
    marker["n_updn_tokens"] = len(toks)

    # ---- stairs from tread runs; unnamed cells mostly covered by a stair become the stair
    stair_polys = []
    flights = stair_flights(ctx.get("osegs", np.zeros((0, 4))), s)
    if flights:
        boxes = [box(x0_ * s, (page_h - y1_) * s, x1_ * s, (page_h - y0_) * s) for x0_, y0_, x1_, y1_ in flights]
        if outline:
            ol = unary_union([_P(o["polygon"]).buffer(0.5) for o in outline])
            boxes = [b for b in boxes if b.intersection(ol).area >= 0.5 * b.area]
        grp = list(range(len(boxes)))

        def gfind(i):
            while grp[i] != i:
                grp[i] = grp[grp[i]]
                i = grp[i]
            return i

        for i in range(len(boxes)):
            for j in range(i + 1, len(boxes)):
                if boxes[i].distance(boxes[j]) <= 0.8:
                    grp[gfind(i)] = gfind(j)
        sg = defaultdict(list)
        for i, b in enumerate(boxes):
            sg[gfind(i)].append(b)
        stair_polys = [unary_union(g).convex_hull for g in sg.values()]
        stair_polys = [p for p in stair_polys if 2.0 <= p.area <= 40.0]
        for r in rooms:
            if r["type"] in ("unlabeled", "unknown") and r.get("poly"):
                rp = _P(r["poly"])
                if rp.area > 0 and sum(rp.intersection(p).area for p in stair_polys) >= 0.4 * rp.area:
                    r["type"], r["type_src"] = "stair", "tread_lines"
    for r in rooms:
        if r.get("poly") and stair_polys and r["type"] in ("unlabeled", "unknown"):
            rp = _P(r["poly"])
            r["_near_stair"] = any(rp.distance(p) <= 0.5 for p in stair_polys)
    marker["n_stair_flights"] = len(flights)
    # ---- adjacency, openings and outer-wall (facade) contact
    gap_px = max(2, int(round(0.42 / s * rho)))
    xlab = int(lab.max()) + 1
    isroom = np.zeros(xlab + 1, bool)
    ischajja = np.zeros(xlab + 1, bool)
    for r in rooms:
        if r["type"] != "chajja":
            isroom[lab_of[r["id"]]] = True
        elif r["area"] < 6.0 or r["short"] < 2.0:  # a strip; a room-sized "chajja" is a room with a lost label
            ischajja[lab_of[r["id"]]] = True
    lab2 = lab.copy()
    if outside is not None:
        lab2[outside & ~isroom[lab]] = xlab
    # a chajja (weather shade outside the window) is drawn as a closed strip, so its free space is not reached
    # from the sheet border; the wall behind it is still a facade
    lab2[ischajja[lab] & (lab > 0)] = xlab
    acc, fac = adjacency_pos(lab2, gap_px, xlab)
    ext_mask = lab2 == xlab
    del lab2
    # facades: the outside reached within 1.6 m across walls, chajjas, ledges and ducts, but never through a
    # room (the window wall often has a chajja, boxing or ODU ledge cell in front of it)
    block = np.zeros(int(lab.max()) + 2, bool)
    for r in rooms:
        if r["type"] != "chajja" and not (r["type"] == "unlabeled" and r["short"] < 1.25):
            block[lab_of[r["id"]]] = True
    fac = facade_reach(lab, ext_mask, block, max(2, int(round(1.6 / px_m))))
    del ext_mask
    min_wall_px = max(3, int(round(0.30 / px_m)))
    min_open_px = max(3, int(round(0.55 / px_m)))
    links, link_pos, wall_info, touch_len = {}, {}, {}, {}
    for (la_, lb_), e in acc.items():
        if la_ == xlab or lb_ == xlab or (la_ - 1) not in idmap or (lb_ - 1) not in idmap:
            continue
        nt, nw = e[0], e[1]
        ra, rb = rooms[idmap[la_ - 1]], rooms[idmap[lb_ - 1]]
        key = (min(ra["id"], rb["id"]), max(ra["id"], rb["id"]))
        if nw >= min_wall_px or nt >= min_open_px:
            ra["neighbours"].append(rb["id"])
            rb["neighbours"].append(ra["id"])
            thick = (e[6] / nw - 1) * px_m if nw else 0.0
            wall_info[key] = ((nt + nw) * px_m, max(0.0, thick))
        touch_len[key] = nt * px_m
        if nt >= min_open_px:
            ra["open"].append(rb["id"])
            rb["open"].append(ra["id"])
            links[key] = "open"
            link_pos[key] = m_pt(e[2] / nt, e[3] / nt)
        elif nw >= min_wall_px:
            link_pos.setdefault(("wall",) + key, m_pt(e[4] / nw, e[5] / nw))
    for r in rooms:
        cnt = fac[lab_of[r["id"]]] if lab_of[r["id"]] < len(fac) else np.zeros(4)
        lens = {d: round(float(c) * px_m, 2) for d, c in zip(FACADE_DIRS, cnt, strict=True) if c * px_m >= 0.3}
        r["facade"] = lens
        r["facade_sides"] = [d for d, v in lens.items() if v >= 0.6]
        r["exterior"] = bool(r["facade_sides"])
    # ---- unnamed cells crossed corner to corner by a diagonal (the drawn lift-well / duct X)
    segs_all = ctx.get("segs")
    n_x = 0
    if segs_all is not None and len(segs_all):
        d = segs_all[:, 2:] - segs_all[:, :2]
        ln = np.hypot(d[:, 0], d[:, 1]) * s
        ang = np.degrees(np.arctan2(np.abs(d[:, 1]), np.abs(d[:, 0])))
        ds = segs_all[(ln >= 1.0) & (ln <= 5.0) & (ang > 20) & (ang < 70)]
        dP = np.stack([ds[:, 0] * s, (page_h - ds[:, 1]) * s, ds[:, 2] * s, (page_h - ds[:, 3]) * s], 1)
        core_t = {"lobby", "lift", "fire_lift", "stair", "passage"}
        for r in rooms:
            if r["type"] not in ("unlabeled", "unknown") or r.get("name") or not r.get("poly") or not len(dP):
                continue
            pg = _P(r["poly"])
            if not 1.0 <= pg.area <= 12.0:
                continue
            x0_, y0_, x1_, y1_ = pg.bounds
            hit = False
            for (ax_, ay_), (bx_, by_) in (((x0_, y0_), (x1_, y1_)), ((x0_, y1_), (x1_, y0_))):
                da = np.hypot(dP[:, 0] - ax_, dP[:, 1] - ay_) < 0.45
                db = np.hypot(dP[:, 2] - bx_, dP[:, 3] - by_) < 0.45
                da2 = np.hypot(dP[:, 0] - bx_, dP[:, 1] - by_) < 0.45
                db2 = np.hypot(dP[:, 2] - ax_, dP[:, 3] - ay_) < 0.45
                hit |= bool(((da & db) | (da2 & db2)).any())
            if hit:
                near_core = any(rooms[v]["type"] in core_t for v in r["neighbours"])
                r["type"] = "lift" if pg.area >= 2.5 and near_core else "shaft_cell"
                r["type_src"] = "x_cross"
                n_x += 1
    marker["n_x_cells"] = n_x
    # ---- doors from swing arcs
    arcs = fit_door_arcs(cluster_pieces(pieces), s)
    dpos: dict = {}
    dl = door_links(arcs, lab, grid, s, n, dpos)
    n_doors = 0
    for (a, b), _c in dl.items():
        if a in idmap and b in idmap:
            ra, rb = rooms[idmap[a]], rooms[idmap[b]]
            key = (min(ra["id"], rb["id"]), max(ra["id"], rb["id"]))
            ra["doors"].append(rb["id"])
            rb["doors"].append(ra["id"])
            if rb["id"] not in ra["neighbours"]:
                ra["neighbours"].append(rb["id"])
                rb["neighbours"].append(ra["id"])
            links[key] = "door"
            px_, py_ = grid.px(dpos[(a, b)])
            link_pos[key] = m_pt(float(px_), float(py_))
            n_doors += 1
    marker["n_door_arcs"] = len(arcs)
    marker["n_door_links"] = n_doors
    # ---- a lift region much larger than its printed size has flooded the lobby in front of it (no wall
    # drawn across the lift door): it stands for that lobby when a flat door opens on it
    for r in rooms:
        if r["type"] in LIFTS and (r.get("area_ratio") or 0) >= 1.4:
            r["leaky"] = True
    # ---- a "chajja" region the size of a room that opens into a room is a room whose own label was lost
    # (the chajja label outside its window flooded it): keep it as an unnamed room
    for r in rooms:
        if r["type"] == "chajja" and r["area"] >= 6.0 and r["short"] >= 2.0 and \
                any(rooms[v]["type"] not in NOT_PRIVATE | ROOM_TYPES_COMMON for v in r["open"] + r["doors"]):
            r["type"], r["demoted_from"] = "unlabeled", "chajja"
    # ---- drop links between room types that are never joined by a door (audit failure mode 2)
    n_rej = 0
    for key in list(links):
        ra, rb = rooms[key[0]], rooms[key[1]]
        leaky = (ra.get("leaky") and rb["type"] in ENTRY_TYPES) or (rb.get("leaky") and ra["type"] in ENTRY_TYPES)
        if bad_link(ra["type"], rb["type"]) and not leaky:
            del links[key]
            n_rej += 1
            for x, y in ((ra, rb), (rb, ra)):
                x["doors"] = [v for v in x["doors"] if v != y["id"]]
                x["open"] = [v for v in x["open"] if v != y["id"]]
    marker["n_links_rejected"] = n_rej
    # ---- shaft / duct labels near rooms
    wrad = max(2, int(round(0.75 / px_m)))
    shafts = []
    for i in range(n):
        if seeds[i]["type"] != "shaft":
            continue
        x, y = _px_of(grid, sp[i])
        x, y = int(round(x)), int(round(y))
        win = lab[max(0, y - wrad): y + wrad + 1, max(0, x - wrad): x + wrad + 1]
        for lb in np.unique(win):
            if lb > 0 and (int(lb) - 1) in idmap:
                rooms[idmap[int(lb) - 1]]["shaft"] = True
        st = regions.get(i)
        sl = objs[i] if i < len(objs) else None
        if st is not None and sl is not None and st["area_px"] * px_m * px_m <= 8.0:
            pg = region_polygon(lab[sl] == i + 1, tol)
            if pg is not None and pg.geom_type == "Polygon" and not pg.is_empty:
                shafts.append({"name": seeds[i]["name"],
                               "poly_m": m_poly(affinity.translate(pg, sl[1].start, sl[0].start))})
    for r in rooms:
        r["neighbours"] = sorted(set(r["neighbours"]))
        r["doors"] = sorted(set(r["doors"]))
        r["open"] = sorted(set(r["open"]))
    mark_common(rooms, links, touch_len)
    # unnamed lift-sized cells (1.4-3.3 m a side) opening only on to the lobby are lifts whose label was lost
    # (one pass over the lifts named before it, so corridor cells cannot chain into a row of "lifts";
    # a lift opens on the lobby through a door, never over more than 1.25 m)
    named_lifts = {r["id"] for r in rooms if r["type"] in LIFTS}
    for r in rooms:
        if r["type"] not in ("unknown", "unlabeled") or r.get("common") or not r.get("poly"):
            continue
        a, b = (sorted(r["ocr_dims"]) if r.get("ocr_dims") else (r["short"], r["long"]))
        lk = set(r["doors"]) | set(r["open"])
        if 1.4 <= a <= 3.3 and 1.4 <= b <= 3.3 and lk and \
                all(rooms[v]["type"] in ("lobby",) or (rooms[v].get("common") and rooms[v]["type"] not in LIFTS)
                    for v in lk) and any(v in named_lifts for v in r["neighbours"]) and \
                max(touch_len.get((min(r["id"], v), max(r["id"], v)), 0.0) for v in lk) <= 1.25:
            r["type"], r["type_src"], r["common"] = "lift", "size_next_to_lifts", True
    lp = {k: v for k, v in link_pos.items() if len(k) == 2}
    for k, v in link_pos.items():  # wall midpoints serve as the entrance point of wall-only entrances
        if len(k) == 3:
            lp.setdefault(k[1:], v)
    flats, dropped = build_flats(rooms, links, name, pno, wall_info, lp)

    # ---- cores: common rooms, stairs and shafts lying within 0.7 m of each other, holding a lift or stair
    els = []
    for r in rooms:
        if r.get("common") and r.get("poly"):
            els.append({"type": r["type"], "room_id": r["id"], "poly_m": _P(r["poly"]).buffer(0), "name": r["name"]})
    for p in stair_polys:
        if not any(e["type"] == "stair" and e["poly_m"].intersection(p).area > 0.3 * p.area for e in els):
            els.append({"type": "stair", "room_id": None, "poly_m": p, "name": "tread_lines"})
    lift_like = [e for e in els if e["type"] in LIFTS | {"stair", "lobby"}]
    for sh in shafts:
        if any(sh["poly_m"].distance(e["poly_m"]) <= 0.6 for e in lift_like):
            els.append({"type": "shaft", "room_id": None, "poly_m": sh["poly_m"], "name": sh["name"]})
    grp = list(range(len(els)))

    def cfind(i):
        while grp[i] != i:
            grp[i] = grp[grp[i]]
            i = grp[i]
        return i

    for i in range(len(els)):
        for j in range(i + 1, len(els)):
            # tread hulls stop short of the landings, so a stair joins the core over up to 1.6 m
            lim = 1.6 if "stair" in (els[i]["type"], els[j]["type"]) else 0.7
            if els[i]["poly_m"].distance(els[j]["poly_m"]) <= lim:
                grp[cfind(i)] = cfind(j)
    cgroups = defaultdict(list)
    for i, e in enumerate(els):
        cgroups[cfind(i)].append(e)
    cores = []
    for g in sorted(cgroups.values(), key=lambda g: -sum(e["poly_m"].area for e in g)):
        types = Counter(e["type"] for e in g)
        if not (types["lift"] + types["fire_lift"] + types["stair"]):
            continue
        un = unary_union([e["poly_m"].buffer(0.35, join_style="mitre") for e in g]).buffer(-0.35, join_style="mitre")
        parts = [un] if un.geom_type == "Polygon" else sorted(getattr(un, "geoms", []), key=lambda p: -p.area)
        parts = [p for p in parts if p.geom_type == "Polygon" and p.area > 0.5]
        if not parts:
            continue
        cen = un.centroid
        k = len(cores)
        stairs = [e["poly_m"] for e in g if e["type"] == "stair"]  # flights within 1.5 m = one stair
        sp_ = list(range(len(stairs)))
        for i in range(len(stairs)):
            for j in range(i + 1, len(stairs)):
                if stairs[i].distance(stairs[j]) <= 1.5:
                    a_, b_ = sp_[i], sp_[j]
                    sp_ = [a_ if x == b_ else x for x in sp_]
        types["stair"] = len(set(sp_))
        for e in g:
            if e["room_id"] is not None:
                rooms[e["room_id"]]["core_id"] = k
        x0, y0, x1, y1 = un.bounds
        cores.append({"id": k, "polygon": _poly_coords(parts[0]), "parts": [_poly_coords(p) for p in parts[1:]],
                      "centroid": [round(cen.x, 2), round(cen.y, 2)], "area": round(un.area, 1),
                      "bbox": [round(x0, 2), round(y0, 2), round(x1, 2), round(y1, 2)],
                      "n_lifts": types["lift"], "n_fire_lifts": types["fire_lift"], "n_stairs": types["stair"],
                      "n_lobby": types["lobby"] + types["passage"] + types["unlabeled"] + types["unknown"],
                      "elements": [{"type": e["type"], "room_id": e["room_id"], "name": e["name"],
                                    "area": round(e["poly_m"].area, 2),
                                    "polygon": _poly_coords(e["poly_m"]) if e["poly_m"].geom_type == "Polygon" else None,
                                    "centroid": [round(e["poly_m"].centroid.x, 2), round(e["poly_m"].centroid.y, 2)]}
                                   for e in g]})
    for f in flats:  # core id of the rooms, now that cores are known
        for c in f["common_rooms"]:
            c["core_id"] = rooms[c["id"]].get("core_id")
        if f.get("entrance"):
            f["entrance"]["core_id"] = rooms[f["entrance"]["common"]].get("core_id")
    common_out = [{"id": r["id"], "type": r["type"], "name": r["name"], "area": r["area"], "poly": r.get("poly"),
                   "c": [r.get("cxm"), r.get("cym")], "core_id": r.get("core_id"), "why": r.get("common_why")}
                  for r in rooms if r.get("common")]
    if os.environ.get("TOPO_DEBUG"):
        _DEBUG.update(rooms=rooms, lab_of=lab_of, outside=outside, outline=outline, cores=cores, links=links,
                      link_pos=link_pos, m_pt=m_pt)
    for k, f in enumerate(flats):
        f["flat_id"] = f"{name}:p{pno}:F{k + 1}"
        f["title"] = marker["title"]
        f["scale_m_per_pt"] = marker["scale_m_per_pt"]
    marker["status"] = "ok"
    marker["n_flats"] = len(flats)
    marker["n_flats_dropped"] = dropped
    marker["page_h_m"] = round(page_h * s, 2)
    marker["outline"] = outline
    marker["cores"] = cores
    marker["common_rooms"] = common_out
    marker["n_rooms_total"] = len(rooms)
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
