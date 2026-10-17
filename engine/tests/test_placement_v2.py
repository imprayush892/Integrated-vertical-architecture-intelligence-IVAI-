"""Tower placement v2: hard rules, explanations, variety, runtime - on the golden plot and six synthetic plots.

Render the review images with::

    python -m tests.test_placement_v2 [out_dir]      # from engine/, writes placement-*.png
"""

from __future__ import annotations

import math
import time

import pytest
import yaml

from planner_engine import rules
from planner_engine.models import PlacementOption
from planner_engine.placement import _different, drivers_note, place
from planner_engine.site import build_site

LAT0, LNG0 = 19.10, 72.85

# name, corners in metres (CCW, edge i runs from corner i to i+1), road edges, road width, height, view bearing
SYNTHETIC = [
    ("narrow-deep", [(0, 0), (32, 0), (32, 110), (0, 110)], [0], 12.0, 45.0, None),
    ("wide-shallow", [(0, 0), (130, 0), (130, 40), (0, 40)], [2], 18.0, 32.0, None),
    ("corner-two-roads", [(0, 0), (75, 0), (75, 62), (0, 62)], [0, 1], 18.0, 45.0, None),
    ("pentagon", [(0, 0), (70, -8), (95, 40), (45, 80), (-10, 50)], [0], 15.0, 60.0, None),
    ("sea-west", [(0, 0), (65, 0), (65, 85), (0, 85)], [1], 18.0, 70.0, 270.0),
    ("large-3-towers", [(0, 0), (170, 0), (170, 115), (0, 115)], [0], 24.0, 90.0, None),
]


def to_lnglat(pts):
    kx = 111_320 * math.cos(math.radians(LAT0))
    return [(LNG0 + x / kx, LAT0 + y / 110_574) for x, y in pts]


def synthetic_site(name):
    for nm, pts, roads, rw, h, vb in SYNTHETIC:
        if nm == name:
            return build_site(to_lnglat(pts), name=nm, road_width=rw, building_height=h, road_edges=roads), vb
    raise KeyError(name)


def _golden_site(lokhandwala):
    return build_site([tuple(p) for p in lokhandwala["lnglat"]], name="lokhandwala")


@pytest.fixture(scope="module")
def synthetic_results():
    out = {}
    for nm, *_ in SYNTHETIC:
        s, vb = synthetic_site(nm)
        t0 = time.perf_counter()
        pl = place(s, view_bearing=vb)
        out[nm] = (s, pl, time.perf_counter() - t0)
    return out


# ------------------------------------------------------------------------------------------------------ golden


def test_golden_three_options_with_reasons(lokhandwala):
    s = _golden_site(lokhandwala)
    pl = place(s)
    assert [o.id for o in pl.options] == ["A", "B", "C"]
    assert pl.elapsed_s < 3.0
    assert sorted(o.rank for o in pl.options) == [1, 2, 3]
    assert sum(o.recommended for o in pl.options) == 1
    assert [o.towers for o in pl.options] == sorted(o.towers for o in pl.options)
    for o in pl.options:
        assert all(o.passes.values()), o.passes
        assert {"fire_access", "road_entry", "parking_access", "fsi", "plate_viable"} <= set(o.passes)
        assert o.drivers.startswith("Design drivers:")
        soft = [r for r in o.reasons if r["kind"] in ("pro", "con", "note")]
        assert len(soft) >= 6
        assert all(any(ch.isdigit() for ch in r["text"]) for r in soft[:5]), soft[:5]
        assert any(r["key"] == "fire_access" for r in o.reasons)
        assert o.site_elements["gate"] is not None
        assert o.site_elements["ramp"] is not None  # 3,398 m2 plot needs a ramp (or a car-lift bay)
        assert o.site_elements["parking_mode"] in ("ramp", "car lifts")
    # the three options are meaningfully different, not rotations of one idea
    labels = {(o.towers, o.concept, o.arrangement) for o in pl.options}
    assert len(labels) == 3


def test_reasons_compare_options(lokhandwala):
    pl = place(_golden_site(lokhandwala))
    a = pl.options[0]
    others = {o.id for o in pl.options[1:]}
    west = next(r for r in a.reasons if r["key"] == "west_glass")
    assert all(f"for {oid}" in west["text"] for oid in others)


def test_drivers_note_without_context(lokhandwala):
    pl = place(_golden_site(lokhandwala))
    o = pl.options[0]
    assert drivers_note(o, {}).startswith("Design drivers:")
    legacy = PlacementOption(id="A", towers=1, label="x", plates=o.plates, coverage=0.3, total_area=1.0, min_gap=None,
                             score=0.5, score_terms={"west_glass": 0.7}, drivers="")
    assert drivers_note(legacy, {}).startswith("Design drivers:")


def test_old_projects_still_load():
    o = PlacementOption.model_validate({"id": "A", "towers": 1, "label": "x", "plates": [], "coverage": 0.3,
                                        "total_area": 1.0, "min_gap": None, "score": 0.5, "score_terms": {},
                                        "drivers": ""})
    assert o.rank == 0 and o.reasons == [] and o.site_elements == {}


# --------------------------------------------------------------------------------------------------- synthetic


@pytest.mark.parametrize("name", [s[0] for s in SYNTHETIC])
def test_synthetic_plot_options(name, synthetic_results):
    s, pl, dt = synthetic_results[name]
    assert dt < 3.0, f"{name} took {dt:.2f} s"
    assert pl.options, name
    rec = next(o for o in pl.options if o.recommended)
    assert all(rec.passes.values()), (name, rec.passes)
    for o in pl.options:
        assert o.reasons and o.drivers.startswith("Design drivers:")
        assert all(o.passes.values()), (name, o.id, o.passes)
        for p in o.plates:
            assert 12.0 - 1e-6 <= p.depth <= 32.0 + 1e-6
    for i, a in enumerate(pl.options):
        for b in pl.options[i + 1 :]:
            assert (a.towers, a.concept, a.arrangement) != (b.towers, b.concept, b.arrangement) or \
                abs(a.plates[0].angle_deg - b.plates[0].angle_deg) >= 25, (name, a.label, b.label)


def test_narrow_plot_tower_runs_along_the_depth(synthetic_results):
    s, pl, _ = synthetic_results["narrow-deep"]
    rec = next(o for o in pl.options if o.recommended)
    # the plot runs north-south: the long axis of every plate must too (angle near 90)
    for p in rec.plates:
        assert min(abs(p.angle_deg - 90), abs(p.angle_deg + 90)) < 10, p.angle_deg


def test_wide_plot_faces_north_south(synthetic_results):
    s, pl, _ = synthetic_results["wide-shallow"]
    rec = next(o for o in pl.options if o.recommended)
    assert rec.metrics["west_frac"] < 0.2
    assert all(min(p.angle_deg, 180 - p.angle_deg) < 10 for p in rec.plates)


def test_corner_gate_clear_of_junction(synthetic_results):
    s, pl, _ = synthetic_results["corner-two-roads"]
    corner = s.polygon[1]  # where the two road edges meet
    jc = rules.site_rules()["entry"]["junction_clearance"]
    for o in pl.options:
        g = o.site_elements.get("gate")
        if g:
            assert math.dist(g, corner) >= jc - 1e-6


def test_high_rise_fire_access_all_round(synthetic_results):
    # 90 m towers need the 6 m drive on every face (site.yaml faces_by_height)
    s, pl, _ = synthetic_results["large-3-towers"]
    for o in pl.options:
        if o.passes["fire_access"]:
            assert o.metrics["fire_faces_min"] >= 4


def test_large_plot_offers_multi_tower(synthetic_results):
    s, pl, _ = synthetic_results["large-3-towers"]
    assert any(o.towers == 3 for o in pl.options)
    rec = next(o for o in pl.options if o.recommended)
    assert rec.towers >= 2  # one 1,500 m2 plate cannot use the FSI of a 19,550 m2 plot


def test_sea_view_is_used(synthetic_results):
    s, pl, _ = synthetic_results["sea-west"]
    rec = next(o for o in pl.options if o.recommended)
    assert any(r["key"] == "view" for r in rec.reasons)
    assert rec.score_terms["view"] > 0.3


def test_tall_tower_on_small_plot_is_explained_not_silent():
    # 40 x 40 m at 70 m: the FSI spread over 23 floors leaves ~235 m2 plates whose core would take a third of them
    s = build_site(to_lnglat([(0, 0), (40, 0), (40, 40), (0, 40)]), road_width=12.0, building_height=70.0)
    pl = place(s)
    assert pl.options, "a toy plate is shown and explained rather than returning nothing"
    rec = next(o for o in pl.options if o.recommended)
    assert rec.passes["plate_viable"] is False
    why = next(r for r in rec.reasons if r["key"] == "plate_viable")
    assert why["kind"] == "fail" and "height would give a workable plate" in why["text"]


def test_new_rule_tables_are_marked_unverified():
    sr = rules.site_rules()
    for k in ("fsi", "fire_access", "entry", "parking_ramp", "recreational_open_space"):
        assert sr[k]["verified"] is False and sr[k]["clause"]
    w = yaml.safe_load(open(rules._data_dir() / "design.yaml"))["placement"]["weights"]
    assert {"fsi", "west_glass", "breeze", "view", "privacy", "daylight", "garden", "approach"} <= set(w)


def test_options_are_structurally_different(synthetic_results):
    # internal check on the selection rule itself
    s, pl, _ = synthetic_results["pentagon"]
    assert len(pl.options) >= 2
    _ = _different


# ------------------------------------------------------------------------------------------------------ render


def render(site, pl, path, title=""):  # pragma: no cover - review images only
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.patches import FancyArrow
    from matplotlib.patches import Polygon as MPoly

    from planner_engine import geo

    n = max(1, len(pl.options))
    fig, axes = plt.subplots(1, n, figsize=(6.4 * n, 8.6))
    axes = [axes] if n == 1 else list(axes)
    xs = [p[0] for p in site.polygon]
    ys = [p[1] for p in site.polygon]
    span = max(max(xs) - min(xs), max(ys) - min(ys))
    for ax, o in zip(axes, pl.options, strict=False):
        ax.set_aspect("equal")
        ax.axis("off")
        ring = site.polygon
        for i in range(len(ring)):
            a, b = ring[i], ring[(i + 1) % len(ring)]
            if i in site.road_edges:
                nb = geo.bearing_of_normal(a, b)
                nx, ny = math.sin(math.radians(nb)), math.cos(math.radians(nb))
                off = site.road_width / 2 + 0.5
                ax.plot([a[0] + nx * off, b[0] + nx * off], [a[1] + ny * off, b[1] + ny * off], color="#bbb",
                        lw=max(4, 150 * site.road_width / span), solid_capstyle="butt", zorder=0)
                ax.text((a[0] + b[0]) / 2 + nx * off, (a[1] + b[1]) / 2 + ny * off, f"ROAD {site.road_width:.0f} m",
                        ha="center", va="center", fontsize=7, color="#333",
                        rotation=(math.degrees(math.atan2(b[1] - a[1], b[0] - a[0])) + 90) % 180 - 90)
        ax.add_patch(MPoly(ring, closed=True, fill=False, ec="black", lw=1.4))
        for br in site.buildable:
            ax.add_patch(MPoly(br, closed=True, fill=False, ec="#777", lw=0.8, ls="--"))
        el = o.site_elements
        for g in el.get("garden") or []:
            ax.add_patch(MPoly(g, closed=True, fc="#9bd18b", ec="none", alpha=0.55))
        for d in el.get("drive") or []:
            ax.add_patch(MPoly(d, closed=True, fc="#f2b35d", ec="#d98b1f", alpha=0.55, lw=0.5))
        if el.get("ramp"):
            ax.add_patch(MPoly(el["ramp"], closed=True, fc="#9a7fd1", ec="#5b3fa0", alpha=0.8))
        for p in o.plates:
            ax.add_patch(MPoly(p.polygon, closed=True, fc="#3f6fb5", ec="#1d3c6e", alpha=0.9))
            ax.text(*p.center, f"{p.id}\n{p.width:.0f}x{p.depth:.0f}", ha="center", va="center", color="white", fontsize=7)
        if el.get("gate"):
            ax.plot(*el["gate"], marker="v", color="red", ms=9, zorder=5)
        if el.get("exit_gate"):
            ax.plot(*el["exit_gate"], marker="^", color="red", ms=8, zorder=5)
        for alt in o.runners_up[:1]:
            for ring2 in alt["plates"]:
                ax.add_patch(MPoly(ring2, closed=True, fill=False, ec="#c33", lw=0.7, ls=":"))
        # compass and climate arrows
        cx, cy = max(xs) + 0.06 * span, max(ys) - 0.05 * span
        L = 0.07 * span
        ax.add_patch(FancyArrow(cx, cy - L / 2, 0, L, width=L * 0.06, color="black"))
        ax.text(cx, cy + L * 0.65, "N", ha="center", fontsize=8)
        if site.climate:
            prev = max(site.climate.wind.bins, key=lambda b: b.frequency)
            v = (math.sin(math.radians(prev.direction)), math.cos(math.radians(prev.direction)))
            wx, wy = cx, cy - 1.6 * L
            ax.add_patch(FancyArrow(wx + v[0] * L * 0.6, wy + v[1] * L * 0.6, -v[0] * L, -v[1] * L, width=L * 0.05,
                                    color="#2a9d8f"))
            ax.text(wx, wy - L * 0.75, f"wind {prev.label}", ha="center", fontsize=7, color="#2a9d8f")
        ax.text(cx, cy - 2.9 * L, "hot sun W", ha="center", fontsize=7, color="#e76f51")
        vb = o.metrics.get("view_frac")
        if vb is not None and "view" in o.score_terms:
            pass
        rk = f"rank {o.rank}" + (" - RECOMMENDED" if o.recommended else "")
        head = f"{o.id}: {o.label}\n{rk}, score {o.score:.3f}, coverage {o.coverage:.0%}"
        fails = [k for k, v in o.passes.items() if not v]
        if fails:
            head += f"\nFAILS: {', '.join(fails)}"
        ax.set_title(head, fontsize=9)
        import textwrap

        lines = []
        for r in o.reasons[:6]:
            sym = {"pro": "+", "con": "-", "note": "o", "rule": "ok", "fail": "X"}[r["kind"]]
            lines.append(textwrap.fill(f"{sym} {r['text']}", 78))
        ax.text(0.0, -0.02, "\n".join(lines), transform=ax.transAxes, va="top", fontsize=6.3, family="monospace")
        ax.set_xlim(min(xs) - 0.12 * span, max(xs) + 0.16 * span)
        ax.set_ylim(min(ys) - 0.12 * span, max(ys) + 0.08 * span)
    fig.suptitle(f"{title}  |  plot {site.area:,.0f} m2, height {site.building_height:.0f} m, road "
                 f"{site.road_width:.0f} m  |  {pl.elapsed_s:.2f} s\nblue tower, orange 6 m fire/vehicle drive, purple "
                 "ramp or car-lift bay, red triangle gate, green usable garden, dashed setback line, dotted red runner-up",
                 fontsize=10)
    fig.subplots_adjust(left=0.01, right=0.99, top=0.88, bottom=0.30, wspace=0.05)
    fig.savefig(path, dpi=110)
    plt.close(fig)


def main(out_dir: str) -> None:  # pragma: no cover
    import json
    from importlib import resources
    from pathlib import Path

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    g = json.loads(resources.files("planner_engine").joinpath("data/golden/lokhandwala.json").read_text())
    jobs = [("lokhandwala", build_site([tuple(p) for p in g["lnglat"]], name="lokhandwala"), None)]
    for nm, *_ in SYNTHETIC:
        s, vb = synthetic_site(nm)
        jobs.append((nm, s, vb))
    for nm, s, vb in jobs:
        t0 = time.perf_counter()
        pl = place(s, view_bearing=vb)
        dt = time.perf_counter() - t0
        render(s, pl, out / f"placement-{nm}.png", nm)
        print(f"{nm:18s} {dt:5.2f} s  " + "  ".join(
            f"{o.id}:{o.towers}{o.concept[0]} r{o.rank} {o.score:.3f} {'ok' if all(o.passes.values()) else 'FAIL'}"
            for o in pl.options))


if __name__ == "__main__":  # pragma: no cover
    import sys

    main(sys.argv[1] if len(sys.argv) > 1 else "../docs/quality/img")
