"""Stage 4 - place the towers.

For each family (1, 2 or 3 plates) sweep the plate rotation over 0-179 degrees in 1 degree steps. At each
angle the buildable polygon is rasterised in the rotated frame; for every column range the longest
free vertical run gives the largest inscribed plate in that range (vectorised over all ranges at once).
Plates are then chosen left-to-right with the rule-file tower gap between them by a small DP that
maximises capped area. Every angle's best layout is scored on coverage, gap, breeze, west glass and
balance; the best per family plus 3 runners-up are returned. Everything is deterministic.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass

import numpy as np
import shapely
from shapely.geometry import Polygon

from . import geo, rules
from .models import Placement, PlacementOption, PlateFootprint, Site
from .site import buildable_geom

FAMILY_LABEL = {1: "single tower", 2: "twin staggered", 3: "three slender"}
FAMILY_ID = {1: "A", 2: "B", 3: "C"}
HOT_RANGE = (225.0, 315.0)


@dataclass
class Cand:
    xa: float
    xb: float
    ya: float
    yb: float
    area: float
    value: float
    c0: int
    c1: int  # inclusive column range


def _bearing_for_axis(theta_deg: float) -> float:
    """Compass bearing of the outward normal of the plate's 'north' long face for a plate whose long
    axis points at math angle theta (CCW from east)."""
    normal_math = theta_deg + 90.0
    return (90.0 - normal_math) % 360.0


def _candidates(
    mask: np.ndarray, res: float, x0: float, y0: float, cap: float, lim: dict
) -> list[Cand]:
    """Best plate for every column range, fully vectorised, then pruned to 3 per start column."""
    rows, cols = mask.shape
    kmax = min(cols, int(lim["max_length"] / res))
    if kmax <= 0 or rows == 0:
        return []
    padded = np.concatenate([mask, np.zeros((rows, kmax), dtype=bool)], axis=1)  # (rows, cols+kmax)
    # M[a, k, r] = all(mask[r, a..a+k])
    M = np.empty((cols, kmax, rows), dtype=bool)
    cur = mask.T.copy()
    M[:, 0, :] = cur
    for k in range(1, kmax):
        cur = cur & padded[:, k : k + cols].T
        M[:, k, :] = cur
    h = np.zeros((cols, kmax), dtype=np.int32)
    best_len = np.zeros((cols, kmax), dtype=np.int32)
    best_end = np.zeros((cols, kmax), dtype=np.int32)
    for r in range(rows):
        h = np.where(M[:, :, r], h + 1, 0)
        upd = h > best_len
        best_len = np.where(upd, h, best_len)
        best_end = np.where(upd, r, best_end)
    a_idx, k_idx = np.meshgrid(np.arange(cols), np.arange(kmax), indexing="ij")
    w_av = (k_idx + 1) * res
    d_av = best_len * res
    # a plate may run either way: short side within [min_depth, max_depth], long side >= min_length
    short_av = np.minimum(w_av, d_av).astype(float)
    long_av = np.maximum(w_av, d_av).astype(float)
    valid = (a_idx + k_idx < cols) & (short_av >= lim["min_depth"]) & (long_av >= lim["min_length"])
    sh = np.minimum(short_av, lim["max_depth"])
    safe = np.where(sh > 0, sh, 1.0)
    lg = np.minimum(np.minimum(long_av, lim["max_length"]), np.maximum(lim["min_length"], cap / safe))
    over = lg * sh > cap
    sh = np.where(over, np.maximum(lim["min_depth"], cap / np.where(lg > 0, lg, 1.0)), sh)
    sh = np.floor(sh * 10) / 10
    lg = np.floor(lg * 10) / 10
    x_is_long = w_av >= d_av
    w = np.where(x_is_long, lg, sh)
    d = np.where(x_is_long, sh, lg)
    ideal = float(lim.get("ideal_depth", 22.0))
    q = np.clip(1.0 - np.abs(np.minimum(w, d) - ideal) / 10.0, 0.0, 1.0)
    area = np.where(valid, w * d * (0.8 + 0.2 * q), 0.0)  # DP value: area, nudged toward good depths
    out: list[Cand] = []
    karange = np.arange(kmax)
    for a in np.flatnonzero(area.any(axis=1)):
        row = area[a]
        order = np.lexsort((karange, -np.round(row, 1)))
        seen: set[float] = set()
        for k in order:
            if row[k] <= 0:
                break
            key = round(float(row[k]), 0)
            if key in seen:
                continue
            seen.add(key)
            L = int(best_len[a, k])
            cx = x0 + (a + (k + 1) / 2) * res
            cy = y0 + (int(best_end[a, k]) + 1 - L / 2) * res
            ww, dd = float(w[a, k]), float(d[a, k])
            out.append(
                Cand(cx - ww / 2, cx + ww / 2, cy - dd / 2, cy + dd / 2, ww * dd, float(row[k]), int(a), int(a + k))
            )
            if len(seen) >= 3:
                break
    return out


def _choose(cands: list[Cand], n: int, gap_cells: int, cols: int) -> list[Cand] | None:
    """Pick n plates left-to-right maximising total area with column gaps >= gap_cells."""
    if not cands:
        return None
    by_start: dict[int, list[Cand]] = {}
    for c in cands:
        by_start.setdefault(c.c0, []).append(c)
    NEG = -1.0
    # S[m][c] = best area using m plates all starting at column >= c ; P = choice
    S = [[0.0] * (cols + 2) for _ in range(n + 1)]
    P: list[list[Cand | None]] = [[None] * (cols + 2) for _ in range(n + 1)]
    for m in range(1, n + 1):
        S[m][cols] = S[m][cols + 1] = NEG
        for c in range(cols - 1, -1, -1):
            S[m][c], P[m][c] = S[m][c + 1], None
            for cand in by_start.get(c, []):
                nxt = cand.c1 + 1 + gap_cells
                rest = 0.0 if m == 1 else (S[m - 1][min(nxt, cols)] if nxt <= cols else NEG)
                if rest < 0:
                    continue
                v = cand.value + rest
                if v > S[m][c] + 1e-9:
                    S[m][c], P[m][c] = v, cand
    if S[n][0] <= 0:
        return None
    # walk back
    picks: list[Cand] = []
    c, m = 0, n
    while m > 0 and c <= cols:
        while c <= cols and P[m][c] is None:
            c += 1
        if c > cols:
            return None
        cand = P[m][c]
        assert cand is not None
        picks.append(cand)
        c = cand.c1 + 1 + gap_cells
        m -= 1
    return picks if len(picks) == n else None


def _rect_poly(c: Cand, theta: float) -> Polygon:
    pts = [(c.xa, c.ya), (c.xb, c.ya), (c.xb, c.yb), (c.xa, c.yb)]
    return Polygon(geo.rotate_pts(pts, theta))


def _depth_quality(depth: float, ideal: float) -> float:
    return max(0.0, 1.0 - abs(depth - ideal) / 10.0)


def _long_axis(c: Cand, theta: float) -> tuple[float, float]:
    """(long-axis math angle, plate depth) for a candidate at raster angle theta."""
    w, d = c.xb - c.xa, c.yb - c.ya
    return (theta, d) if w >= d else ((theta + 90.0) % 180.0, w)


def _score(picks: list[Cand], plates: list[Polygon], theta: float, site_area: float, gap_min: float, ctx: dict, w: dict):
    total = sum(p.area for p in plates)
    target = ctx["coverage_target"] * site_area
    coverage = min(1.0, total / target) if target > 0 else 0.0
    if len(plates) > 1:
        gaps = [plates[i].distance(plates[j]) for i in range(len(plates)) for j in range(i + 1, len(plates))]
        mg = min(gaps)
        gap = min(1.0, mg / (2.0 * gap_min)) if gap_min > 0 else 1.0
        balance = min(p.area for p in plates) / max(p.area for p in plates)
    else:
        mg, gap, balance = None, 1.0, 1.0
    wind = ctx.get("wind_from")
    view_b = ctx.get("view_bearing")
    breeze = view = depth_q = 0.0
    for c in picks:
        axis, depth = _long_axis(c, theta)
        face = _bearing_for_axis(axis)
        breeze += abs(math.cos(math.radians(geo.angle_diff(face, wind)))) if wind is not None else 0.5
        view += abs(math.cos(math.radians(geo.angle_diff(face, view_b)))) if view_b is not None else 0.5
        depth_q += _depth_quality(depth, ctx["ideal_depth"])
    breeze, view, depth_q = breeze / len(picks), view / len(picks), depth_q / len(picks)
    hot_len = per = 0.0
    for p in plates:
        ring = geo.ring_of(p)
        for i in range(len(ring)):
            a, b = ring[i], ring[(i + 1) % len(ring)]
            L = math.dist(a, b)
            per += L
            brg = geo.bearing_of_normal(a, b)
            if HOT_RANGE[0] <= brg <= HOT_RANGE[1]:
                hot_len += L
    west = max(0.0, 1.0 - 2.0 * hot_len / per) if per else 0.0
    terms = {
        "coverage": coverage, "gap": gap, "breeze": breeze, "view": view, "west_glass": west,
        "balance": balance, "depth": depth_q,
    }
    score = sum(w.get(k, 0.0) * v for k, v in terms.items())
    return score, terms, mg, hot_len / per if per else 0.0


def drivers_note(opt: PlacementOption, ctx: dict) -> str:
    """Template design-drivers sentence (T5 safe default)."""
    t = opt.score_terms
    bits = []
    face = geo.compass(_bearing_for_axis(opt.plates[0].angle_deg)) if opt.plates else "-"
    back = geo.compass((_bearing_for_axis(opt.plates[0].angle_deg) + 180) % 360) if opt.plates else "-"
    if ctx.get("wind_from") is not None and opt.plates:
        # name the long face that actually meets the wind first
        fb = _bearing_for_axis(opt.plates[0].angle_deg)
        if geo.angle_diff((fb + 180) % 360, ctx["wind_from"]) < geo.angle_diff(fb, ctx["wind_from"]):
            face, back = back, face
    if ctx.get("wind_label") and t["breeze"] >= 0.7:
        bits.append(f"long faces open to the {ctx['wind_label']} breeze ({face}/{back})")
    else:
        bits.append(f"long faces {face}/{back}")
    if t["west_glass"] >= 0.6:
        bits.append("little glass on the hot west")
    else:
        bits.append("west faces need shading fins")
    if opt.towers > 1 and opt.min_gap is not None:
        bits.append(f"{opt.min_gap:.1f} m between towers")
    bits.append(f"{opt.coverage:.0%} coverage")
    return "Design drivers: " + "; ".join(bits) + "."


def place(site: Site, families: tuple[int, ...] = (1, 2, 3), view_bearing: float | None = None) -> Placement:
    t0 = time.perf_counter()
    d = rules.design()
    lim = d["plate"]
    pl = d["placement"]
    weights = pl["weights"]
    res = float(pl["raster"])
    gap_min, gap_clause = rules.tower_gap_for(site.building_height)
    cov_max, cov_clause = rules.max_coverage()
    geom = buildable_geom(site)
    wind_from = wind_label = None
    if site.climate:
        prev = max(site.climate.wind.bins, key=lambda b: b.frequency)
        wind_from, wind_label = prev.direction, prev.label
    ctx = {
        "coverage_target": min(float(pl["coverage_target"]), cov_max),
        "wind_from": wind_from,
        "wind_label": wind_label,
        "view_bearing": view_bearing,
        "ideal_depth": float(lim.get("ideal_depth", 22.0)),
    }
    options: list[PlacementOption] = []
    if geom.is_empty:
        return Placement(options=[], elapsed_s=time.perf_counter() - t0)

    for n in families:
        cap = min(float(lim["max_area_per_tower"]), ctx["coverage_target"] * site.area / n)
        gap_cells = int(math.ceil(gap_min / res))
        results = []
        for theta in range(0, 180, int(pl["angle_step_fine"])):
            rot = shapely.affinity.rotate(geom, -theta, origin=(0, 0))
            mask, x0, y0 = geo.raster_inside(rot, res)
            cands = _candidates(mask, res, x0, y0, cap, lim)
            picks = _choose(cands, n, gap_cells, mask.shape[1])
            if not picks:
                continue
            polys = [_rect_poly(c, theta) for c in picks]
            score, terms, mg, west = _score(picks, polys, theta, site.area, gap_min, ctx, weights)
            results.append((score, theta, picks, polys, terms, mg))
        if not results:
            continue
        results.sort(key=lambda r: -r[0])
        best = results[0]
        runners = []
        for r in results[1:]:
            if all(geo.angle_diff(r[1] * 2, x["angle_deg"] * 2) / 2 >= 10 for x in runners) and geo.angle_diff(
                r[1] * 2, best[1] * 2
            ) / 2 >= 10:
                runners.append(
                    {
                        "angle_deg": float(r[1]),
                        "score": round(r[0], 4),
                        "coverage": round(sum(p.area for p in r[3]) / site.area, 4),
                        "plates": [[list(map(lambda v: round(v, 3), xy)) for xy in geo.ring_of(p)] for p in r[3]],
                    }
                )
            if len(runners) >= 3:
                break
        score, theta, picks, polys, terms, mg = best
        plates = []
        for i, (c, p) in enumerate(zip(picks, polys, strict=True)):
            cx, cy = (c.xa + c.xb) / 2, (c.ya + c.yb) / 2
            center = geo.rotate_pts([(cx, cy)], theta)[0]
            w_x, d_y, ang = c.xb - c.xa, c.yb - c.ya, float(theta)
            if w_x < d_y:  # plate frame x always runs along the long side
                w_x, d_y, ang = d_y, w_x, (ang + 90.0) % 180.0
            plates.append(
                PlateFootprint(
                    id=f"T{i + 1}",
                    polygon=[(round(x, 3), round(y, 3)) for x, y in geo.ring_of(p)],
                    angle_deg=ang,
                    center=(round(center[0], 3), round(center[1], 3)),
                    width=round(w_x, 2),
                    depth=round(d_y, 2),
                    area=round(p.area, 2),
                )
            )
        total = sum(p.area for p in polys)
        inside = all(geom.buffer(1e-6).contains(p) for p in polys)
        opt = PlacementOption(
            id=FAMILY_ID[n],  # type: ignore[arg-type]
            towers=n,
            label=FAMILY_LABEL[n],
            plates=plates,
            coverage=round(total / site.area, 4),
            total_area=round(total, 2),
            min_gap=round(mg, 2) if mg is not None else None,
            score=round(score, 4),
            score_terms={k: round(v, 4) for k, v in terms.items()},
            drivers="",
            runners_up=runners,
            passes={
                "coverage": total / site.area <= cov_max + 1e-9,
                "gap": mg is None or mg >= gap_min - 1e-6,
                "inside_buildable": inside,
            },
        )
        opt.drivers = drivers_note(opt, ctx)
        options.append(opt)
    _ = (gap_clause, cov_clause)
    return Placement(options=options, elapsed_s=round(time.perf_counter() - t0, 3))
