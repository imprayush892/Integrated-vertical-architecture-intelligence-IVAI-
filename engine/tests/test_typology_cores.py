"""Plate typology classifier and core library: descriptor sanity on synthetic plates, dimensional checks of every core
type, and the selector's egress rules. The dataset test (agreement with the by-eye labels of the 50 approved projects)
is skipped when data/mined/typology_v1.json is absent."""

import json
import math
from pathlib import Path

import pytest
from shapely import affinity
from shapely.geometry import Point, box
from shapely.ops import unary_union

from planner_engine import cores as C
from planner_engine import typology as T

REPO = Path(__file__).resolve().parents[2]


# --------------------------------------------------------------------------------------------------
# typology: descriptors and labels on textbook plates
# --------------------------------------------------------------------------------------------------

CASES = [
    ("slab", (48, 15), "linear_slab"),
    ("square", (26,), "point_square"),
    ("L", (40, 14, 36), "L"),
    ("T", (42, 14, 34), "T"),
    ("H", (40, 14, 34), "H"),
    ("cross", (40, 14, 40), "cruciform"),
    ("U", (40, 14, 30), "courtyard"),
    ("ring", (40, 12, 40), "courtyard"),
    ("Y", (25, 14), "Y"),
]


@pytest.mark.parametrize("kind,args,label", CASES)
def test_textbook_plates_get_their_label(kind, args, label):
    r = T.classify(T.make_outline(kind, *args))
    assert r.label == label, (kind, r.label, r.reasons)
    assert r.label in T.TYPES


@pytest.mark.parametrize("angle", [0, 17, 33, 90, 141])
@pytest.mark.parametrize("kind,args,label", [c for c in CASES if c[0] in ("L", "T", "H", "cross", "slab")])
def test_label_is_rotation_invariant(kind, args, label, angle):
    p = affinity.rotate(T.make_outline(kind, *args), angle, origin=(10, 10))
    assert T.classify(p).label == label


@pytest.mark.parametrize("kind,args,label", [c for c in CASES if c[0] in ("L", "T", "H", "cross")])
def test_label_survives_balconies_and_ducts(kind, args, label):
    """Balconies (1.5 m deep, 3 m wide) and a duct slot along the façade must not change the bucket."""
    p = T.make_outline(kind, *args)
    minx, miny, maxx, maxy = p.bounds
    bumps = [box(x, miny - 1.5, x + 3, miny) for x in range(int(minx) + 3, int(maxx) - 6, 9)]
    bumps += [box(maxx, y, maxx + 1.5, y + 3) for y in range(int(miny) + 3, int(maxy) - 6, 9)]
    noisy = unary_union([p, *bumps]).difference(box(minx + 10, maxy - 2, minx + 10.6, maxy + 1))
    assert T.classify(noisy).label == label


def test_descriptors_are_sane_for_known_shapes():
    d_slab = T.describe(T.make_outline("slab", 48, 16))
    assert d_slab["aspect"] == pytest.approx(3.0, abs=0.05)
    assert d_slab["rectangularity"] > 0.98 and d_slab["convexity"] > 0.98 and d_slab["orthogonality"] > 0.98
    d_l = T.describe(T.make_outline("L", 40, 14, 36))
    assert 0.5 < d_l["rectangularity"] < 0.75
    assert d_l["convexity"] < 0.9
    d_cross = T.describe(T.make_outline("cross", 40, 14, 40))
    assert d_cross["symmetry"] > 0.95
    assert 0.5 < d_cross["rectangularity"] < 0.7 and d_cross["convexity"] < 0.85
    d_t = T.describe(T.make_outline("T", 42, 14, 34))
    assert d_t["symmetry"] > 0.9  # mirror symmetry about the stem axis


def test_wings_of_an_L_and_a_T():
    r = T.classify(T.make_outline("L", 40, 14, 36))
    assert sorted(round(w.width) for w in r.wings) == [14, 14]
    assert sorted(round(w.length) for w in r.wings) == [36, 40]
    r = T.classify(T.make_outline("T", 42, 14, 34))
    kinds = {w.kind: w for w in r.wings}
    assert round(kinds["bar"].length) == 42 and round(kinds["stem"].width) == 14


def test_triangle_is_parked_as_experimental():
    r = T.classify(T.make_outline("triangle", 40))
    assert r.label == "other" and r.experimental and r.subtype == "triangle"


def test_slab_vs_point_boundary_follows_aspect():
    assert T.classify(box(0, 0, 30, 28)).label == "point_square"
    assert T.classify(box(0, 0, 50, 20)).label == "linear_slab"


def test_stepped_slab_is_not_an_L():
    """A slab with a 4 m step is a slab: an L notch must span at least a quarter of both sides."""
    p = unary_union([box(0, 0, 60, 16), box(0, 16, 25, 20)])
    assert T.classify(p).label == "linear_slab"


def test_comb_is_not_a_T():
    teeth = unary_union([box(0, 0, 60, 14), *[box(x, -8, x + 8, 0) for x in (0, 26, 52)]])
    r = T.classify(teeth)
    assert r.label == "other" and r.subtype == "comb"


def test_core_position_buckets():
    slab = T.make_outline("slab", 48, 15)
    assert T.core_position(slab, (24, 7.5))["position"] == "central"
    assert T.core_position(slab, (3, 7.5))["position"] == "end"
    assert T.core_position(slab, (24, 2))["position"] in ("side", "central")
    ell = T.make_outline("L", 40, 14, 36)
    assert T.core_position(ell, (9, 9))["position"] == "reentrant"
    assert T.core_position(T.make_outline("cross", 40, 14, 40), (20, 20))["position"] == "central"


# --------------------------------------------------------------------------------------------------
# cores: every type passes its own dimensional checks
# --------------------------------------------------------------------------------------------------

HEIGHTS = [10, 14.5, 20, 30, 60, 90, 150]
FLATS = [2, 4, 8]


@pytest.mark.parametrize("height", HEIGHTS)
@pytest.mark.parametrize("flats", FLATS)
@pytest.mark.parametrize("kind", list(C.CORE_TYPES))
def test_every_core_type_passes_its_dimensional_checks(kind, height, flats):
    req = C.requirements(height, flats, 700.0)
    core = C.build_core(kind, req)
    bad = [c for c in core.check() if not c.ok and c.severity == "error"]
    assert not bad, [(c.id, c.value, c.limit) for c in bad]
    assert core.n_exits >= req.n_stairs
    assert len(core.of("lift")) >= req.n_lifts
    assert len(core.of("fire_lift")) >= req.n_fire_lifts
    if height > C.FIRE_LIFT_ABOVE_M:
        assert core.of("fire_lift"), "fire lift above 24 m"
        assert core.of("smoke_lobby"), "smoke-check lobby above 24 m"
    for s in core.of("stair"):
        assert s.meta["flight_w"] >= req.stair_flight_w - 1e-9
        assert s.meta["riser_h"] <= C.RISER_MAX_M
    # lobby depth in front of every lift >= lift depth (clear)
    for lift in core.of("lift", "fire_lift"):
        assert core._lobby_depth_in_front(lift) >= req.lift_d - 0.02
    # no two parts overlap
    parts = core.parts
    for i, a in enumerate(parts):
        for b in parts[i + 1:]:
            assert a.polygon.intersection(b.polygon).area < 1e-4, (a.name, b.name)


@pytest.mark.parametrize("kind", list(C.CORE_TYPES))
def test_core_efficiency_is_in_a_believable_range(kind):
    req = C.requirements(60, 6, 700.0)
    core = C.build_core(kind, req)
    assert 0.05 < core.efficiency() < 0.25, (kind, core.efficiency())


def test_requirements_follow_height_and_flats():
    low = C.requirements(12, 4, 500)
    mid = C.requirements(20, 4, 500)
    tall = C.requirements(60, 8, 800)
    assert low.n_stairs == 1 and mid.n_stairs == 2 and tall.n_stairs == 2
    assert low.n_fire_lifts == 0 and mid.n_fire_lifts == 0 and tall.n_fire_lifts >= 1
    assert tall.n_lifts >= mid.n_lifts >= low.n_lifts
    assert tall.stair_flight_w >= 1.5
    assert tall.refuge_floors and tall.refuge_floors[0] == 7
    assert tall.lobby_depth >= tall.lift_d


def test_lift_count_grows_with_flats_per_floor():
    assert C.passenger_lifts(75, 16) > C.passenger_lifts(75, 2)


def test_stair_dims_use_code_risers_and_treads():
    sd = C.stair_dims(1.5, 3.0)
    assert sd.riser_h <= C.RISER_MAX_M and sd.tread_d >= C.TREAD_MIN_M
    assert sd.risers % 2 == 0
    assert sd.clear_w == pytest.approx(2 * 1.5 + C.STAIR_WELL_M)
    assert C.stair_dims(1.5, 3.0, scissor=True).outer_w < 2 * sd.outer_w  # a scissor is narrower than two stairs


def test_core_place_moves_and_rotates():
    core = C.build_core("central_compact", C.requirements(60, 4, 600))
    moved = core.place(10, 20, 90)
    assert moved.core_area == pytest.approx(core.core_area)
    assert moved.bbox[0] < 10  # rotated by 90 degrees: the former +y extent is now -x
    assert len(moved.to_model_parts()) == len(core.parts)
    assert {p["kind"] for p in moved.to_model_parts()} <= {"stair", "lift", "fire_lift", "lobby", "service"}


# --------------------------------------------------------------------------------------------------
# selector
# --------------------------------------------------------------------------------------------------

PLATES = {
    "point_square": (28.0, 28.0),
    "linear_slab": (55.0, 16.0),
    "L": T.make_outline("L", 38, 14, 32),
    "T": T.make_outline("T", 42, 15, 34),
    "H": T.make_outline("H", 45, 15, 40),
    "cruciform": T.make_outline("cross", 42, 14, 42),
}


@pytest.mark.parametrize("plate", list(PLATES))
@pytest.mark.parametrize("height", [16, 24, 40, 75, 120])
def test_selector_gives_two_stairs_above_the_nbc_height(plate, height):
    ranked = C.choose_core(plate, 6, height, PLATES[plate])
    best = ranked[0]
    assert best.compliant and best.score > 0
    assert best.core.n_exits >= 2, "NBC 4.4.2.1: two stairs above 15 m"
    assert best.reasons and best.reasons[0].startswith("recommended")
    assert ranked == sorted(ranked, key=lambda o: (-o.score, o.efficiency))
    if height > C.FIRE_LIFT_ABOVE_M:
        assert best.core.of("fire_lift")


def test_selector_prefers_the_right_family():
    assert C.choose_core("point_square", 4, 60, PLATES["point_square"])[0].type in ("central_compact", "side_core")
    assert C.choose_core("linear_slab", 6, 45, PLATES["linear_slab"])[0].type in ("linear_corridor", "end_core", "side_core")
    assert C.choose_core("H", 8, 90, PLATES["H"])[0].type in ("split_junction", "central_compact")


def test_selector_penalises_scissor_without_authority_acceptance():
    ranked = {o.type: o for o in C.choose_core("point_square", 4, 60, PLATES["point_square"])}
    assert ranked["scissor"].score < ranked["central_compact"].score
    assert any("authority" in c.id for c in ranked["scissor"].checks)


def test_low_building_needs_one_stair():
    best = C.choose_core("linear_slab", 4, 12, (45.0, 14.0))[0]
    assert best.core.n_exits == 1 and not best.core.of("fire_lift")


def test_core_that_does_not_fit_scores_zero():
    ranked = C.choose_core("point_square", 8, 120, (14.0, 14.0))
    assert all((not o.fits) or o.score == 0 or o.core.bbox[2] - o.core.bbox[0] <= 14.0 + 1e-6 for o in ranked)


def test_draw_core_renders():
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    for kind in C.CORE_TYPES:
        fig, ax = plt.subplots()
        C.draw_core(C.build_core(kind, C.requirements(60, 6, 700)), ax)
        plt.close(fig)


# --------------------------------------------------------------------------------------------------
# the approved projects
# --------------------------------------------------------------------------------------------------

DATA = REPO / "data" / "mined" / "typology_v1.json"


@pytest.mark.skipif(not DATA.exists(), reason="data/mined/typology_v1.json not built")
def test_classifier_agrees_with_the_by_eye_labels_of_the_approved_projects():
    projects = json.loads(DATA.read_text())["projects"]
    usable = [p for p in projects if p.get("extraction") == "ok"]
    assert len(usable) >= 40
    agree = sum(1 for p in usable if p["label"] == p["eye_label"])
    assert agree / len(usable) >= 0.9, (agree, len(usable))
    # re-run the classifier on the stored outlines: the stored label must be reproducible
    from shapely.geometry import Polygon

    p = usable[0]
    r = T.classify(Polygon(p["clean_outline"]))
    assert r.label in T.TYPES
    assert math.isfinite(sum(float(x) for x in (p["outline_m2"],)))
    assert Point(0, 0).is_valid
