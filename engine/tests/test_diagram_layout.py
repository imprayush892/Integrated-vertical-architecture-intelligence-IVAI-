"""Stage 7 bubble diagram layout: no overlaps, deterministic, fast, linked rooms close, entry/window sides."""

import itertools
import math
import time

import pytest

from planner_engine.diagram import bubble_radius, from_programme
from planner_engine.models import Programme, ProgrammeRoom, Unit
from planner_engine.units import presets


def _unit(normal=(0.0, -1.0)):
    return Unit(id="u1", polygon=[(0, 0), (9, 0), (9, 7), (0, 7)], area=63.0, core_link=(4.5, 0.0),
                core_link_normal=normal)


def _dist(a, b):
    return math.hypot(a.x - b.x, a.y - b.y)


def _crowded() -> Programme:
    """Eighteen rooms, one hub - a harder case than the stock programmes."""
    rooms = [ProgrammeRoom(id=f"r{i}", type="bedroom" if i % 2 else "toilet", name=f"Room {i}",
                           target_area=3.0 + (i * 7) % 14, needs_window=bool(i % 2)) for i in range(18)]
    return Programme(id="x", name="x", bhk=3, area_range=(40, 90), rooms=rooms,
                     links=[("r0", r.id) for r in rooms[1:]], entry="r0")


PROGS = list(presets().items()) + [("crowded", _crowded())]


@pytest.mark.parametrize("pid,prog", PROGS)
def test_no_pair_overlaps(pid, prog):
    for normal in [(0.0, -1.0), (1.0, 0.0), (-0.6, 0.8), None]:
        d = from_programme(_unit(normal), prog)
        assert len(d.rooms) == len(prog.rooms)
        for a, b in itertools.combinations(d.rooms, 2):
            need = bubble_radius(a.target_area) + bubble_radius(b.target_area)
            assert _dist(a, b) >= need - 1e-6, (pid, normal, a.id, b.id, _dist(a, b), need)


@pytest.mark.parametrize("pid,prog", PROGS)
def test_deterministic_and_fast(pid, prog):
    u = _unit()
    runs, times = [], []
    for _ in range(3):
        t = time.perf_counter()
        d = from_programme(u, prog)
        times.append(time.perf_counter() - t)
        runs.append([(r.id, r.x, r.y) for r in d.rooms])
    assert runs[0] == runs[1] == runs[2]
    assert min(times) < 0.05, times  # fastest of three: shared CI CPUs spike


@pytest.mark.parametrize("pid,prog", [(k, v) for k, v in PROGS if k != "crowded"])
def test_linked_rooms_closer_than_unlinked(pid, prog):
    d = from_programme(_unit(), prog)
    linked = {frozenset(e) for e in d.links}
    near = [_dist(a, b) for a, b in itertools.combinations(d.rooms, 2) if frozenset((a.id, b.id)) in linked]
    far = [_dist(a, b) for a, b in itertools.combinations(d.rooms, 2) if frozenset((a.id, b.id)) not in linked]
    assert near and far
    assert sum(near) / len(near) < sum(far) / len(far)


def test_entry_sits_toward_the_entrance_and_windows_toward_the_facade():
    prog = presets()["3bhk"]
    for normal in [(0.0, -1.0), (1.0, 0.0), (0.0, 1.0), (-1.0, 0.0)]:
        d = from_programme(_unit(normal), prog)
        ux, uy = normal[0], -normal[1]  # plate frame is y-up, canvas y-down
        axial = {r.id: r.x * ux + r.y * uy for r in d.rooms}
        assert axial[d.entry] > 0, (normal, axial[d.entry])
        win = [axial[r.id] for r in d.rooms if r.needs_window and r.id != d.entry]
        rest = [axial[r.id] for r in d.rooms if not r.needs_window and r.id != d.entry]
        assert sum(win) / len(win) < sum(rest) / len(rest), normal


def test_model_shape_and_centering():
    d = from_programme(_unit(), presets()["2bhk"])
    assert d.entry == "foyer" and all(r.placed for r in d.rooms)
    assert {r.id for r in d.rooms} == {r.id for r in presets()["2bhk"].rooms}
    lo = min(r.x - bubble_radius(r.target_area) for r in d.rooms)
    hi = max(r.x + bubble_radius(r.target_area) for r in d.rooms)
    assert abs(lo + hi) < 0.5


# ---------------------------------------------------------------------------------------------- generator v2 zoning

def test_zone_programme_guest_toilet_and_optional_connectors():
    """v2 brief: a 1BHK gains a powder room off the living room; foyer / passage become optional connectors;
    nothing the user placed is removed."""
    from planner_engine.diagram import zone_programme

    prog = presets()["1bhk"]
    d = from_programme(_unit(), prog)
    z = zone_programme(d, 48.0, {}, _unit(), facade=14.0)
    ids = {r.id for r in z.rooms}
    assert {r.id for r in d.rooms} <= ids
    powder = next(r for r in z.rooms if r.id == "powder")
    assert powder.type == "wc" and powder.optional
    assert ("living", "powder") in [tuple(l) for l in z.links]
    assert all(r.optional for r in z.rooms if r.type in ("foyer", "passage"))
    assert len(d.rooms) == len(prog.rooms)  # the input diagram is not mutated


def test_zone_programme_adds_rooms_only_when_the_flat_is_big():
    from planner_engine.diagram import zone_programme

    d = from_programme(_unit(), presets()["2bhk"])
    small = zone_programme(d, 70.0, {}, _unit(), facade=16.0)
    big = zone_programme(d, 150.0, {}, _unit(), facade=12.0)
    assert len(small.rooms) <= len(big.rooms)
    extra = {r.type for r in big.rooms} - {r.type for r in d.rooms}
    assert extra & {"dress", "pooja", "study"} and all(r.optional for r in big.rooms if r.id not in {x.id for x in d.rooms})


def test_flex_links_open_private_rooms_onto_one_hub():
    """Programme links to the passage become 'door onto the hub' choices (dining > passage > living)."""
    from planner_engine.diagram import flex_links

    d = from_programme(_unit(), presets()["2bhk"])
    fixed, alts = flex_links(d)
    alt = dict(alts)
    for rid in ("master", "bed2", "toilet_c"):
        assert alt[rid] == ("dining", "passage", "living")
    assert alt["kitchen"] == ("dining", "living")
    assert ("master", "toilet_m") in fixed  # attached toilets stay attached
    assert not any("passage" in l and ("master" in l or "bed2" in l) for l in fixed)
