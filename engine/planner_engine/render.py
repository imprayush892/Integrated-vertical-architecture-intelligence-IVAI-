"""Draw a solved flat as a PNG: walls, doors with swings, windows, room labels and areas, placed furniture and the
critic's findings highlighted. For review by eye (the baseline report) and for tests; the UI draws its own plans.

``plan_png(plan, path, furniture=True, issues=critique(plan, unit, plate))``

matplotlib is imported lazily (it is a dev/report dependency, not needed by the engine itself).
"""

from __future__ import annotations

import io
import textwrap
from pathlib import Path
from typing import Any

import numpy as np

from .models import Plan, Plate, Unit
from .quality import _Ctx, critique

ROOM_FILL = {
    "living": "#f6ecd6", "dining": "#f3e3c0", "kitchen": "#f7dcc8", "master": "#dfe9f5", "bedroom": "#e5eef7",
    "study": "#e6e6f5", "toilet": "#d3ecea", "bath": "#d3ecea", "wc": "#d3ecea", "foyer": "#ece8f2", "passage": "#efefef",
    "utility": "#e9e3d8", "balcony": "#e3f0df", "store": "#f2d9d9", "dress": "#ebe4f1", "pooja": "#f6e8c8",
}
ITEM_FILL = {"bed": "#b08968", "wardrobe": "#8d7b68", "sofa 3-seat": "#5f8a8b", "tv unit": "#444444", "WC": "#ffffff",
             "basin": "#ffffff", "shower 0.9x0.9": "#bfe3ee", "counter": "#c9a27e", "washing machine": "#cccccc", "desk": "#9c8a6e"}
SEV_COLOR = {"critical": "#c0182b", "major": "#d9541e", "minor": "#e0a21b", "info": "#7a8a99"}
SEV_RANK = {"critical": 0, "major": 1, "minor": 2, "info": 3}


def _ring_xy(ring) -> tuple[list[float], list[float]]:
    xs = [p[0] for p in ring] + [ring[0][0]]
    ys = [p[1] for p in ring] + [ring[0][1]]
    return xs, ys


def _label_pos(poly) -> tuple[float, float]:
    from shapely.geometry import Polygon

    p = Polygon(poly).buffer(0)
    q = p.representative_point() if not p.contains(p.centroid) else p.centroid
    return q.x, q.y


def plan_png(plan: Plan | dict, path: str | Path, furniture: bool = True, issues: dict[str, Any] | None = None,
             unit: Unit | None = None, plate: Plate | None = None, title: str | None = None, dpi: int = 80,
             max_issues: int = 9, show_score: bool = True) -> Path:
    """Render ``plan`` to ``path`` (PNG). ``issues`` is a ``critique()`` result (computed here when omitted and
    ``furniture`` is on). Returns the path."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.patches import PathPatch
    from matplotlib.patches import Polygon as MPoly
    from matplotlib.path import Path as MplPath

    if isinstance(plan, dict):
        plan = Plan.model_validate(plan)
    if issues is None and furniture:
        issues = critique(plan, unit, plate)
    ctx = _Ctx(plan, unit, plate)
    xs = [p[0] for r in plan.rooms for p in r.polygon]
    ys = [p[1] for r in plan.rooms for p in r.polygon]
    for w in plan.walls:
        for ring in w.polygon:
            xs += [p[0] for p in ring]
            ys += [p[1] for p in ring]
    x0, x1, y0, y1 = min(xs) - 0.8, max(xs) + 0.8, min(ys) - 1.4, max(ys) + 1.4
    w_m, h_m = x1 - x0, y1 - y0
    fig_w = 11.0
    plan_w = 7.0
    fig_h = max(4.8, min(9.0, plan_w * h_m / w_m + 1.0))
    fig = plt.figure(figsize=(fig_w, fig_h), dpi=dpi)
    ax = fig.add_axes([0.01, 0.02, plan_w / fig_w - 0.01, 0.90])
    ax.set_aspect("equal")
    ax.set_xlim(x0, x1)
    ax.set_ylim(y0, y1)
    ax.axis("off")

    # rooms
    bad_rooms: dict[str, str] = {}
    marks: list[tuple[int, dict]] = []
    shown = []
    if issues:
        shown = [i for i in issues["issues"] if i["severity"] != "info"][:max_issues]
        for n, i in enumerate(shown, 1):
            marks.append((n, i))
            for rid in i.get("rooms", []):
                cur = bad_rooms.get(rid)
                if cur is None or SEV_RANK[i["severity"]] < SEV_RANK[cur]:
                    bad_rooms[rid] = i["severity"]
    for r in plan.rooms:
        ax.add_patch(MPoly(r.polygon, closed=True, fc=ROOM_FILL.get(r.type, "#f0f0f0"), ec="none", zorder=1))
    # furniture
    if furniture and issues:
        for f in issues["furniture"].values():
            for it in f["items"]:
                fc = ITEM_FILL.get(it["name"], "#d8c9b0")
                if it["name"].startswith("dining table"):
                    fc = "#a47551"
                ax.add_patch(MPoly(it["polygon"], closed=True, fc=fc, ec="#555555", lw=0.6, zorder=3, alpha=0.95))
                px = np.mean([p[0] for p in it["polygon"]])
                py = np.mean([p[1] for p in it["polygon"]])
                short = {"bed": "bed", "wardrobe": "wdrb", "sofa 3-seat": "sofa", "tv unit": "TV", "WC": "wc", "basin": "",
                         "shower 0.9x0.9": "shwr", "counter": "", "washing machine": "wm", "desk": "desk"}.get(it["name"], "table")
                if short:
                    ax.text(px, py, short, fontsize=4.5, ha="center", va="center", color="#222222", zorder=4)
    # walls
    for w in plan.walls:
        if not w.polygon:
            continue
        rings = [r for r in w.polygon if len(r) >= 3]
        if rings:
            verts, codes = [], []
            for ring in rings:  # exterior then holes (even-odd fill leaves the holes empty)
                pts = [tuple(p) for p in ring] + [tuple(ring[0])]
                verts += pts
                codes += [MplPath.MOVETO] + [MplPath.LINETO] * (len(pts) - 2) + [MplPath.CLOSEPOLY]
            ax.add_patch(PathPatch(MplPath(verts, codes), fc="#2f2f2f", ec="#2f2f2f", lw=0.3, zorder=5))
    # windows
    for o in plan.windows:
        ax.plot([o.a[0], o.b[0]], [o.a[1], o.b[1]], color="#3b8fd9", lw=2.4, solid_capstyle="butt", zorder=6)
    # doors: leaf + swing
    for d in ctx.doors:
        hinge = np.array(d.swing.exterior.coords[0])
        end = hinge + d.w * d.n
        col = "#8a2be2" if d.kind == "entry" else "#555555"
        ax.plot([hinge[0], end[0]], [hinge[1], end[1]], color=col, lw=1.0, zorder=7)
        sw = np.array(d.swing.exterior.coords)[1:-1]
        ax.plot(sw[:, 0], sw[:, 1], color=col, lw=0.5, ls=(0, (2, 2)), zorder=7)
        ax.plot([d.a[0], d.b[0]], [d.a[1], d.b[1]], color="#ffffff", lw=1.2, zorder=6)
    # front door marker
    if ctx.entry_door is not None:
        d = ctx.entry_door
        out = -d.n  # away from the flat, towards the lobby
        tip = d.mid + out * 0.15
        base = d.mid + out * 1.1
        ax.annotate("ENTRY", xy=(tip[0], tip[1]), xytext=(base[0], base[1]), fontsize=5.5, color="#6a1b9a", ha="center", va="center",
                    arrowprops={"arrowstyle": "-|>", "color": "#6a1b9a", "lw": 0.9}, zorder=11)
    # labels
    for r in plan.rooms:
        lx, ly = _label_pos(r.polygon)
        txt = f"{r.name}\n{r.area:.1f} m²"
        ax.text(lx, ly, txt, fontsize=5.5, ha="center", va="center", color="#111111", zorder=8,
                bbox={"fc": "white", "ec": "none", "alpha": 0.55, "pad": 0.6})
    # highlights
    for rid, sev in bad_rooms.items():
        r = ctx.room.get(rid)
        if r is None:
            continue
        ax.add_patch(MPoly(r.polygon, closed=True, fc=SEV_COLOR[sev], ec=SEV_COLOR[sev], lw=1.6, alpha=0.20, zorder=2))
        xs_, ys_ = _ring_xy(r.polygon)
        ax.plot(xs_, ys_, color=SEV_COLOR[sev], lw=1.4, ls=(0, (4, 2)), zorder=9)
    for _n, i in marks:  # sight lines / routes of an issue (bedroom visible from the front door ...)
        ln = i.get("line")
        if ln:
            ax.annotate("", xy=tuple(ln[1]), xytext=tuple(ln[0]), zorder=9,
                        arrowprops={"arrowstyle": "-|>", "color": SEV_COLOR[i["severity"]], "lw": 1.1, "ls": (0, (3, 2)), "shrinkA": 0, "shrinkB": 0})
    for n, i in marks:
        if i.get("at"):
            ax.text(i["at"][0], i["at"][1], str(n), fontsize=6.5, ha="center", va="center", color="white", fontweight="bold",
                    zorder=10, bbox={"boxstyle": "circle,pad=0.25", "fc": SEV_COLOR[i["severity"]], "ec": "white", "lw": 0.6})
    # unit outline and the lobby door
    if unit is not None:
        from shapely.geometry import Polygon

        ring = Polygon(unit.polygon).exterior.coords
        ax.plot([p[0] for p in ring], [p[1] for p in ring], color="#999999", lw=0.5, ls=":", zorder=0)
    # header and issue panel
    head = title or plan.unit_id
    if issues and show_score:
        head += f"   score {issues['score']:.0f}/100 ({issues['grade']})"
    fig.text(0.012, 0.965, head, fontsize=10, fontweight="bold", va="center")
    if issues and show_score:
        subs = "  ".join(f"{k[:5]} {v:.0f}" for k, v in issues["subscores"].items())
        fig.text(0.012, 0.935, subs, fontsize=6.5, va="center", color="#444444")
    panel = fig.add_axes([plan_w / fig_w, 0.02, 1 - plan_w / fig_w - 0.005, 0.93])
    panel.axis("off")
    panel.set_xlim(0, 1)
    panel.set_ylim(1, 0)
    yy = 0.01
    if issues and show_score:
        for n, i in marks:
            lines = textwrap.wrap(i["message"], 52)[:4]
            panel.text(0.0, yy, str(n), fontsize=6.5, ha="center", va="top", color="white", fontweight="bold",
                       bbox={"boxstyle": "circle,pad=0.25", "fc": SEV_COLOR[i["severity"]], "ec": "none"})
            panel.text(0.05, yy, "\n".join(lines), fontsize=6.2, va="top", color="#111111")
            yy += 0.022 + 0.0165 * len(lines) + 0.012
            if yy > 0.96:
                break
        if not marks:
            panel.text(0.0, yy, "No findings.", fontsize=7, va="top")
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=dpi, facecolor="white")
    plt.close(fig)
    buf.seek(0)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:  # a 64-colour palette keeps a plan under ~60 KB (the baseline folder has to stay small)
        from PIL import Image

        im = Image.open(buf).convert("RGB").quantize(colors=64, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE)
        im.save(path, format="PNG", optimize=True)
    except Exception:  # pragma: no cover
        path.write_bytes(buf.getvalue())
    return path
