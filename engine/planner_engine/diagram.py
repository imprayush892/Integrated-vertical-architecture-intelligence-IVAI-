"""Stage 7a - bubble diagram and pre-check (before the solver runs).

The pre-check is cheap arithmetic and graph work. It catches diagrams that cannot become a plan and names
the smallest set of rooms that clash, in plain words, with 2-4 candidate fixes each.
"""

from __future__ import annotations

import math
from collections import deque
from functools import lru_cache

from . import rules
from .models import Conflict, Diagram, DiagramRoom, Programme, Unit
from .units import statutory_minimums

WALL_SHARE = 0.10  # walls take ~10% of a unit
HABITABLE_WINDOW = {"living", "master", "bedroom", "study", "kitchen"}
WINDOW_MIN_RUN = {"living": 2.4, "master": 2.4, "bedroom": 2.4, "study": 2.4, "kitchen": 1.8, "dining": 1.5}


# ------------------------------------------------------------------------------------------ topology priors
# Counted from municipal-approved Mumbai plans (data/rules/mumbai_topology_priors.json, built by
# tools/derive_topology_priors.py). A prior is used only when strong (thresholds in layout_priors.yaml `topology`);
# otherwise the programme JSON default stands. Priors only ever add a window or a link, they never remove one.


def _cfg() -> dict:
    c = rules.priors().get("topology", {})
    return {"min_projects": int(c.get("min_projects", 5)), "link": float(c.get("link_default_min", 0.6)),
            "facade": float(c.get("facade_needs_window_min", 0.8))}


def _strong(fig: dict | None, floor: float) -> bool:
    """Pooled p and the median across projects both reach the floor, with enough projects behind it."""
    if not fig or fig.get("n_projects", 0) < _cfg()["min_projects"]:
        return False
    return fig["p"] >= floor and fig.get("p_project_median", fig["p"]) >= floor


def facade_figure(room_type: str) -> dict | None:
    return rules.topology_priors().get("exterior", {}).get(room_type, {}).get("facade")


def link_figure(a: str, b: str) -> dict | None:
    """P(door arc or open gap between a room of type a and one of type b), flat level (pair key is sorted)."""
    return rules.topology_priors().get("pairs", {}).get("|".join(sorted((a, b))), {}).get("link")


def prior_needs_window(room_type: str, default: bool) -> bool:
    """Rooms that touch the façade in >= 80% of approved plans (bedroom, master, study) need a window by default."""
    return default or _strong(facade_figure(room_type), _cfg()["facade"])


def prior_default_link(a: str, b: str) -> bool:
    """True when the pair is linked in >= 60% of approved flats that contain both."""
    return _strong(link_figure(a, b), _cfg()["link"])


def complete_links(prog: Programme) -> list[tuple[str, str]]:
    """The programme's links plus, for a room the links leave unreachable from the entry, the strongest prior link.

    Stock programmes are fully connected so this adds nothing; it only helps a custom programme (a study, a dress
    room) arrive with a sensible door instead of a "cannot be reached" conflict.
    """
    links = [tuple(x) for x in prog.links]
    ids = {r.id: r for r in prog.rooms}
    if prog.entry not in ids:
        return links
    adj: dict[str, set[str]] = {k: set() for k in ids}
    for a, b in links:
        if a in adj and b in adj:
            adj[a].add(b)
            adj[b].add(a)

    def reach() -> set[str]:
        seen, q = {prog.entry}, deque([prog.entry])
        while q:
            for nb in adj[q.popleft()]:
                if nb not in seen:
                    seen.add(nb)
                    q.append(nb)
        return seen

    for rid in [r.id for r in prog.rooms]:
        seen = reach()
        if rid in seen:
            continue
        best: tuple[float, str] | None = None
        for other in prog.rooms:
            if other.id in seen and not other.optional and prior_default_link(ids[rid].type, other.type):
                p = link_figure(ids[rid].type, other.type)["p"]
                if best is None or p > best[0]:
                    best = (p, other.id)
        if best:
            links.append((best[1], rid))
            adj[best[1]].add(rid)
            adj[rid].add(best[1])
    return links


def audit_programme(prog: Programme) -> list[dict]:
    """Evidence behind each programme link: supported / weak / no data, with n and n_projects (for docs and review)."""
    types = {r.id: r.type for r in prog.rooms}
    out = []
    for a, b in prog.links:
        fig = link_figure(types.get(a, ""), types.get(b, ""))
        out.append({"a": a, "b": b, "pair": "|".join(sorted((types.get(a, ""), types.get(b, "")))),
                    "p_link": fig["p"] if fig else None, "n": fig["n"] if fig else 0,
                    "n_projects": fig["n_projects"] if fig else 0,
                    "verdict": "no data" if not fig else "supported" if _strong(fig, _cfg()["link"]) else "weak"})
    return out


BUBBLE_GAP = 10.0  # px of clear space wanted between linked bubbles
BUBBLE_MARGIN = 1.5  # px kept between any two bubbles after the overlap pass (survives 0.1 rounding)


def bubble_radius(target_area: float) -> float:
    """Bubble radius in the bubble canvas's own px (app/src/components/BubbleCanvas.tsx draws 16 + 6 * sqrt(area))."""
    return 16.0 + 6.0 * math.sqrt(max(1.0, target_area))


def from_programme(unit: Unit, prog: Programme) -> Diagram:
    links = complete_links(prog)
    rooms = [
        DiagramRoom(
            id=r.id,
            type=r.type,
            name=r.name,
            target_area=r.target_area,
            needs_window=prior_needs_window(r.type, r.needs_window),
            optional=r.optional,
            placed=True,
        )
        for r in prog.rooms
    ]
    _bubble_layout(rooms, links, prog.entry, _entrance_dir(unit))
    return Diagram(unit_id=unit.id, rooms=rooms, links=links, entry=prog.entry)


def _entrance_dir(unit: Unit) -> tuple[float, float, bool]:
    """Unit vector (bubble canvas: x right, y down) from the flat's centre toward its entrance, and whether the unit
    geometry gave one. The door normal points into the passage, so the façade lies on the opposite side. The plate
    frame is y-up, the canvas y-down, hence the flip."""
    n = unit.core_link_normal
    if n and math.hypot(n[0], n[1]) > 1e-9:
        d = math.hypot(n[0], n[1])
        return n[0] / d, -n[1] / d, True
    return 0.0, 1.0, False  # entrance at the bottom, as the UI has always drawn it


def _bubble_layout(rooms: list[DiagramRoom], links: list[tuple[str, str]], entry: str,
                   entrance: tuple[float, float, bool] = (0.0, 1.0, False), steps: int = 90) -> None:
    """Deterministic force layout for the bubble view (UI only). No randomness, no dependencies, identical every run.

    Linked rooms are springs with a rest length of touching radii plus a gap; unlinked rooms push apart when closer
    than a wider gap; a weak pull to the centre keeps it compact. The entry is held toward the entrance side and rooms
    that need a window drift toward the opposite (façade) side when the unit gave an entrance direction. A fixed number
    of cooling steps runs first, then a pairwise pass removes any residual overlap, and a final uniform scale makes the
    no-overlap guarantee unconditional. Positions are in the canvas px frame (see bubble_radius).
    """
    n = len(rooms)
    if n == 0:
        return
    idx = {r.id: i for i, r in enumerate(rooms)}
    rad = [bubble_radius(r.target_area) for r in rooms]
    ux, uy, has_dir = entrance
    px, py = -uy, ux  # perpendicular to the entrance axis
    span = math.sqrt(sum(math.pi * q * q for q in rad) / 0.5)  # side of a square holding the bubbles at ~50% fill
    edges = sorted({(min(idx[a], idx[b]), max(idx[a], idx[b])) for a, b in links if a in idx and b in idx and a != b})
    linked = set(edges)
    e = idx.get(entry, -1)
    win = [i for i, r in enumerate(rooms) if r.needs_window and i != e and has_dir]

    # start: breadth-first layers from the entry, entrance side first, spread across the perpendicular
    adj: list[list[int]] = [[] for _ in range(n)]
    for a, b in edges:
        adj[a].append(b)
        adj[b].append(a)
    layer = [-1] * n
    if e >= 0:
        layer[e] = 0
        q = deque([e])
        while q:
            a = q.popleft()
            for b in adj[a]:
                if layer[b] < 0:
                    layer[b] = layer[a] + 1
                    q.append(b)
    top = max(layer) + 1
    layer = [top if v < 0 else v for v in layer]
    groups: dict[int, list[int]] = {}
    for i, v in enumerate(layer):
        groups.setdefault(v, []).append(i)
    x = [0.0] * n
    y = [0.0] * n
    nl = max(1, max(groups))
    for v, g in groups.items():
        axial = (0.4 - 0.8 * v / nl) * span
        for j, i in enumerate(g):
            lat = (j - (len(g) - 1) / 2) * 2.2 * (sum(rad[k] for k in g) / len(g)) + 3.0 * math.sin(2.399 * (i + 1))
            x[i] = ux * axial + px * lat
            y[i] = uy * axial + py * lat

    for it in range(steps):
        fx = [0.0] * n
        fy = [0.0] * n
        for a in range(n):
            xa, ya, ra = x[a], y[a], rad[a]
            for b in range(a + 1, n):
                dx, dy = xa - x[b], ya - y[b]
                d2 = dx * dx + dy * dy
                if d2 < 1e-12:
                    ang = 2.399 * (a * n + b + 1)  # coincident: separate along a fixed per-pair direction
                    dx, dy, d2 = math.cos(ang) * 1e-3, math.sin(ang) * 1e-3, 1e-6
                d = math.sqrt(d2)
                touch = ra + rad[b]
                if (a, b) in linked:
                    f = 0.35 * (touch + BUBBLE_GAP - d)  # spring toward touching-plus-gap, either way
                else:
                    wide = touch + 4 * BUBBLE_GAP
                    if d >= wide:
                        continue
                    f = 0.5 * (wide - d)  # positive: push apart
                ox, oy = dx / d * f, dy / d * f
                fx[a] += ox
                fy[a] += oy
                fx[b] -= ox
                fy[b] -= oy
        for i in range(n):
            fx[i] -= 0.012 * x[i]
            fy[i] -= 0.012 * y[i]
        if e >= 0:
            ax = 0.4 * span
            fx[e] += 0.25 * (ux * ax - x[e])
            fy[e] += 0.25 * (uy * ax - y[e])
        for i in win:
            axial = x[i] * ux + y[i] * uy
            lim = -0.25 * span
            if axial > lim:  # one-sided pull toward the façade side
                fx[i] -= 0.06 * (axial - lim) * ux
                fy[i] -= 0.06 * (axial - lim) * uy
        cap = 14.0 * (1 - it / steps) + 1.0
        for i in range(n):
            mx, my = fx[i] * 0.5, fy[i] * 0.5
            m = math.hypot(mx, my)
            if m > cap:
                mx, my = mx / m * cap, my / m * cap
            x[i] += mx
            y[i] += my

    # residual overlaps: push apart pairwise, then (unconditional guarantee) scale everything out from the centroid
    for _ in range(400):
        moved = False
        for a in range(n):
            for b in range(a + 1, n):
                dx, dy = x[a] - x[b], y[a] - y[b]
                d = math.hypot(dx, dy)
                need = rad[a] + rad[b] + BUBBLE_MARGIN
                if d < need - 1e-9:
                    if d < 1e-9:
                        ang = 2.399 * (a * n + b + 1)
                        dx, dy, d = math.cos(ang), math.sin(ang), 1.0
                    push = (need - d) / 2 + 1e-3
                    x[a] += dx / d * push
                    y[a] += dy / d * push
                    x[b] -= dx / d * push
                    y[b] -= dy / d * push
                    moved = True
        if not moved:
            break
    s = 1.0
    for a in range(n):
        for b in range(a + 1, n):
            d = math.hypot(x[a] - x[b], y[a] - y[b])
            need = rad[a] + rad[b] + BUBBLE_MARGIN
            if d < need:
                s = max(s, need / max(d, 1e-9))
    if s > 1.0:
        s *= 1.001
        mx, my = sum(x) / n, sum(y) / n
        x = [mx + (v - mx) * s for v in x]
        y = [my + (v - my) * s for v in y]

    # centre the bounding box of the bubbles on the origin
    x0 = (min(x[i] - rad[i] for i in range(n)) + max(x[i] + rad[i] for i in range(n))) / 2
    y0 = (min(y[i] - rad[i] for i in range(n)) + max(y[i] + rad[i] for i in range(n))) / 2
    for i, r in enumerate(rooms):
        r.x, r.y = round(x[i] - x0, 1), round(y[i] - y0, 1)


def room_minimums(room_type: str, programme_min: float = 0.0) -> tuple[float, float]:
    mins = statutory_minimums()
    a, w = mins.get(room_type, (0.0, 0.0))
    defaults = {"dining": (5.0, 2.1), "foyer": (2.0, 1.2), "passage": (1.5, 1.0), "utility": (1.5, 1.0),
                "balcony": (1.5, 1.2), "pooja": (1.2, 1.0), "dress": (2.5, 1.2), "store": (1.2, 0.9)}
    da, dw = defaults.get(room_type, (0.0, 0.0))
    return max(a, da, programme_min), max(w, dw)


def precheck(diagram: Diagram, unit_area: float, facade_len: float, shaft_len: float = 0.0,
             has_door: bool = True) -> list[Conflict]:
    conflicts: list[Conflict] = []
    rooms = {r.id: r for r in diagram.rooms if r.placed}
    usable = unit_area * (1 - WALL_SHARE)
    # 1. area budget
    need = {rid: room_minimums(r.type)[0] for rid, r in rooms.items() if not r.optional}
    if sum(need.values()) > usable:
        big = sorted(need, key=lambda k: -rooms[k].target_area)
        conflicts.append(
            Conflict(
                id="area",
                rooms=big,
                kind="area",
                reason=f"Minimum room sizes need {sum(need.values()):.1f} m² but the flat has {usable:.1f} m² after walls.",
                fixes=[{"action": "drop_room", "room": k, "label": f"Drop {rooms[k].name}"} for k in big[-2:]],
            )
        )
    target = sum(r.target_area for r in rooms.values())
    if target > usable * 1.15 and not conflicts:
        opt = [k for k, r in rooms.items() if r.optional]
        conflicts.append(
            Conflict(
                id="area-target",
                rooms=opt or sorted(rooms, key=lambda k: -rooms[k].target_area)[:2],
                kind="area",
                reason=f"Target sizes add to {target:.1f} m², the flat holds about {usable:.1f} m². Rooms will be shrunk.",
                fixes=[{"action": "drop_room", "room": k, "label": f"Drop {rooms[k].name}"} for k in opt[:2]]
                + [{"action": "scale_targets", "factor": round(usable / target, 3), "label": "Shrink all rooms to fit"}],
            )
        )
    # 2. windows: each window room needs a run of facade
    win = [k for k, r in rooms.items() if r.needs_window]
    run = sum(WINDOW_MIN_RUN.get(rooms[k].type, 1.2) for k in win)
    if run > facade_len:
        conflicts.append(
            Conflict(
                id="windows",
                rooms=win,
                kind="window",
                reason=f"{len(win)} rooms need a window ({run:.1f} m of façade) but the flat has {facade_len:.1f} m.",
                fixes=[{"action": "no_window", "room": k, "label": f"{rooms[k].name}: borrowed light"} for k in win[-2:]],
            )
        )
    wet = [k for k, r in rooms.items() if r.type in ("toilet", "bath", "wc", "utility")]
    if wet and facade_len + shaft_len < 0.8 * len(wet):
        conflicts.append(
            Conflict(id="vent", rooms=wet, kind="window",
                     reason="Not enough façade or shaft for the toilets to be ventilated.",
                     fixes=[{"action": "add_shaft", "label": "Add an air shaft next to the toilets"}])
        )
    # 3. links: everything reachable from the entry
    adj: dict[str, set[str]] = {k: set() for k in rooms}
    for a, b in diagram.links:
        if a in adj and b in adj:
            adj[a].add(b)
            adj[b].add(a)
    if diagram.entry not in rooms:
        conflicts.append(Conflict(id="entry", rooms=[diagram.entry], kind="link", reason="The entry room is not placed.",
                                  fixes=[{"action": "set_entry", "room": "living", "label": "Enter through the living room"}]))
    else:
        seen = {diagram.entry}
        q = deque([diagram.entry])
        while q:
            for nb in adj[q.popleft()]:
                if nb not in seen:
                    seen.add(nb)
                    q.append(nb)
        lost = [k for k in rooms if k not in seen]
        if lost:
            hub = "passage" if "passage" in rooms else diagram.entry
            conflicts.append(
                Conflict(
                    id="reach",
                    rooms=lost,
                    kind="link",
                    reason=", ".join(rooms[k].name for k in lost) + " cannot be reached from the entry.",
                    fixes=[{"action": "add_link", "a": hub, "b": k, "label": f"Link {rooms[k].name} to {rooms[hub].name}"} for k in lost[:3]],
                )
            )
    # 4. a room cannot touch more neighbours than its walls allow
    for k, nbrs in adj.items():
        r = rooms[k]
        a_min, w_min = room_minimums(r.type)
        perim = 2 * (max(r.target_area, a_min) / max(w_min, 1.0) + max(w_min, 1.0))
        if len(nbrs) * 1.0 > perim * 0.8:
            conflicts.append(
                Conflict(id=f"degree-{k}", rooms=[k, *sorted(nbrs)], kind="link",
                         reason=f"{r.name} is linked to {len(nbrs)} rooms; its walls are too short for that many doors.",
                         fixes=[{"action": "remove_link", "a": k, "b": nb, "label": f"Unlink {rooms[nb].name}"} for nb in sorted(nbrs)[:3]])
            )
    if not has_door:
        conflicts.append(Conflict(id="door", rooms=[diagram.entry], kind="geometry",
                                  reason="This flat does not touch the common passage, so it has no entry door.",
                                  fixes=[{"action": "resplit", "label": "Re-split the plate so every flat meets the passage"}]))
    return conflicts


# ------------------------------------------------------------------------------------------ v2 zoning
# Generator v2 (docs/quality/generator-v2.md). An Indian flat reads PUBLIC (entry, living, dining, kitchen, powder,
# utility, balcony off the living) -> HUB (one small distribution space the private doors open onto: the dining
# area, a short passage, or the living room in a small flat) -> PRIVATE (bedrooms with attached toilets, dress).
# The programme's "X - passage" links mean "X opens onto the hub"; which space is the hub is left to the solver,
# in the preference order below, so the passage only appears when nothing else can serve the doors.

HUB_ORDER = ("dining", "passage", "living")
DOOR_FROM_HUB = {"master", "bedroom", "study", "toilet", "bath", "wc", "pooja"}
ZONE = {"foyer": "public", "living": "public", "dining": "public", "kitchen": "public", "utility": "public",
        "balcony": "public", "wc": "public", "passage": "hub", "master": "private", "bedroom": "private",
        "study": "private", "dress": "private", "toilet": "private", "bath": "private"}
# soft size caps used to judge whether the programme can fill the flat (= solver.OVERSIZE, the critic's waste rule)
_CAP = {"master": 20.0, "bedroom": 16.0, "living": 32.0, "dining": 16.0, "kitchen": 11.0, "toilet": 5.5, "bath": 4.0,
        "wc": 2.5, "foyer": 5.5, "passage": 6.5, "utility": 5.0, "study": 10.0, "dress": 6.0, "pooja": 3.0}
NET_SHARE = 0.92  # room floor / net interior once the internal walls are drawn


def flex_links(d: Diagram) -> tuple[list[tuple[str, str]], list[tuple[str, tuple[str, ...]]]]:
    """Split the diagram's links into fixed door links and hub doors (room, candidate hubs in preference order)."""
    types = {r.id: r.type for r in d.rooms if r.placed}
    first = {t: next((k for k, v in types.items() if v == t), None) for t in HUB_ORDER}
    hubs = tuple(first[t] for t in HUB_ORDER if first[t])
    fixed: list[tuple[str, str]] = []
    alts: dict[str, tuple[str, ...]] = {}
    for a, b in d.links:
        ta, tb = types.get(a), types.get(b)
        if ta is None or tb is None or len(hubs) < 2:
            fixed.append((a, b))
            continue
        if tb in HUB_ORDER and ta in DOOR_FROM_HUB or ta in HUB_ORDER and tb in DOOR_FROM_HUB:
            x = a if ta in DOOR_FROM_HUB else b
            alts[x] = hubs
        elif {ta, tb} in ({"passage", "living"}, {"passage", "dining"}):
            p = a if ta == "passage" else b
            alts[p] = tuple(h for h in hubs if types[h] != "passage")
        elif "kitchen" in (ta, tb) and {ta, tb} & {"dining", "living"}:
            k = a if ta == "kitchen" else b
            alts[k] = tuple(h for h in (first["dining"], first["living"]) if h)
        elif "dress" in (ta, tb) and {ta, tb} & {"master", "bedroom"}:
            x, owner = (a, b) if ta == "dress" else (b, a)
            beds = [k for k, v in types.items() if v in ("master", "bedroom") and k != owner]
            alts[x] = (owner, *beds)  # a walk-in wardrobe off its own bedroom, or off another one
        elif "balcony" in (ta, tb) and {ta, tb} & {"dining", "living"}:
            k = a if ta == "balcony" else b
            alts[k] = tuple(h for h in (first["living"], first["dining"]) if h)
        else:
            fixed.append((a, b))
    return fixed, list(alts.items())


LIT_DEPTH = 5.0  # m: rooms lit from the façade rarely reach deeper than this (1/6 glazing, aspect limits)


def _caps_total(rooms: list[DiagramRoom], usable: float, facade: float | None = None) -> tuple[float, float]:
    """Floor the rooms can take before they count as oversized: window rooms limited by the façade band too."""
    live = [r for r in rooms if r.placed]
    total = sum(r.target_area for r in live) or 1.0
    k = max(0.7, min(1.6, usable / total))
    lit = dark = 0.0
    for r in live:
        if r.type in ("passage", "foyer"):
            continue
        c = 1.8 * r.target_area * k if r.type == "balcony" else min(_CAP.get(r.type, 1e9), 1.3 * k * r.target_area + 0.5)
        if r.needs_window or r.type == "balcony":
            lit += c
        else:
            dark += c
    if facade:
        lit = min(lit, facade * LIT_DEPTH)
    return lit + dark, k


def zone_programme(diagram: Diagram, interior_area: float, overrides: dict | None = None,
                   unit: Unit | None = None, facade: float | None = None) -> Diagram:
    """The v2 brief for one flat: enter into the living room, passage only if needed, and, when the flat is much
    bigger than its programme, the rooms an architect would add instead of oversizing (powder room, attached
    toilets, walk-in wardrobes, a study). Rooms the user placed are never removed; added rooms are optional."""
    import copy

    overrides = overrides or {}
    d = copy.deepcopy(diagram)
    rooms = {r.id: r for r in d.rooms}
    forced = {rid for rid, o in overrides.items() if o.get("force") is True}
    living = next((r.id for r in d.rooms if r.type == "living" and r.placed), None)
    # 1. enter straight into the living room (approved plans: 95 %): the foyer is an optional connector the solver
    #    pays for by area, used only when the front door is too far from the façade for a lit living room to reach it
    # 2. the passage likewise: a hub door onto the dining/living serves when it can
    for r in d.rooms:
        if r.type in ("foyer", "passage") and r.id not in forced:
            r.optional = True
    usable = interior_area * NET_SHARE
    bhk = sum(1 for r in d.rooms if r.placed and r.type in ("master", "bedroom"))
    toilets_of = {}
    for a, b in d.links:
        for x, y in ((a, b), (b, a)):
            if x in rooms and y in rooms and rooms[x].type in ("master", "bedroom") and rooms[y].type in ("toilet", "bath"):
                toilets_of[x] = y
    has_common = any(r.type in ("toilet", "bath", "wc") and r.placed and r.id not in toilets_of.values() for r in d.rooms)
    hub = next((r.id for r in d.rooms if r.type == "passage" and r.placed), living)
    extras: list[tuple[DiagramRoom, tuple[str, str]]] = []
    if not has_common and living:  # a guest toilet off the living room (approved plans: 80 % of flats)
        extras.append((DiagramRoom(id="powder", type="wc", name="Powder", target_area=1.8, optional=True), (living, "powder")))
    beds = [r for r in d.rooms if r.placed and r.type in ("master", "bedroom")]
    beds.sort(key=lambda r: (r.type != "master", r.id))
    big: list[tuple[DiagramRoom, tuple[str, str]]] = []
    for b in beds:
        if b.id not in toilets_of:
            big.append((DiagramRoom(id=f"toilet_{b.id}", type="toilet", name=f"Toilet ({b.name.lower()})",
                                    target_area=3.4, optional=True), (b.id, f"toilet_{b.id}")))
    if beds:
        big.append((DiagramRoom(id=f"dress_{beds[0].id}", type="dress", name="Dress", target_area=4.0, optional=True),
                    (beds[0].id, f"dress_{beds[0].id}")))
    # a study needs its own window: only when the façade has a free 2.6 m beyond the window rooms' minimum widths
    win_need = sum(max(2.6, WINDOW_MIN_RUN.get(r.type, 1.2)) for r in d.rooms if r.placed and r.needs_window)
    if hub and bhk >= 2 and (facade is None or facade - win_need >= 2.8):
        big.append((DiagramRoom(id="study", type="study", name="Study", target_area=8.0, needs_window=True, optional=True),
                    (hub, "study")))
    for b in beds[1:]:
        big.append((DiagramRoom(id=f"dress_{b.id}", type="dress", name="Dress", target_area=3.5, optional=True),
                    (b.id, f"dress_{b.id}")))
    if hub:
        big.append((DiagramRoom(id="pooja", type="pooja", name="Pooja", target_area=2.0, optional=True), (hub, "pooja")))
    for room, link in extras:
        if room.id not in rooms:
            d.rooms.append(room)
            d.links.append(link)
            rooms[room.id] = room
    for room, link in big:
        cap, _k = _caps_total(d.rooms, usable, facade)
        if usable - cap < 2.0:
            break
        if room.id in rooms or any(r.id == link[0] and not r.placed for r in d.rooms):
            continue
        d.rooms.append(room)
        d.links.append(link)
        rooms[room.id] = room
    if unit is not None and len(d.rooms) != len(diagram.rooms):
        _bubble_layout(d.rooms, d.links, d.entry, _entrance_dir(unit))
    return d


# ------------------------------------------------------------------------------------------ v3 layout patterns
# Generator v3 (docs/quality/generator-v3.md). data/rules/approved_layout_patterns.json counts, on 967 approved Mumbai
# flats, where rooms lie relative to the living room (entrance-façade frame), how many doors deep they are and what
# bedrooms open onto. A figure is used only when it is backed by PATTERN_MIN_PROJECTS projects; it then becomes a soft
# cost in the solver of PATTERN_W x ln(p_best / p) - the cost of choosing a pattern p times rarer than the usual one.

PATTERN_MIN_PROJECTS = 5
PATTERN_W = 1500  # objective units per natural-log step of rarity (~0.15 m2 of wasted floor)
PATTERN_FLOOR = 0.03  # shares below this count as this (one rare flat must not make a choice forbidden)
TOILET_DINING_COST = 40000  # critic toilet_door: a toilet door facing the dining table costs ~10-15 points
KITCHEN_LIVING_COST = 40000  # critic dining_kitchen: the kitchen should open onto the dining (serving route)
FORWARD = ("E_deep", "NE", "SE")  # sectors further from the entrance than the living room


@lru_cache(maxsize=1)
def layout_patterns() -> dict:
    """approved_layout_patterns.json (rules override directory first); {} when missing, so v2 costs stand."""
    import json

    rel = "approved_layout_patterns.json"
    o = rules._override_dir()
    p = o / rel if o and (o / rel).exists() else rules._data_dir() / rel
    return json.loads(p.read_text()) if p.exists() else {}


def _group(bhk: int) -> dict | None:
    g = layout_patterns().get("bhk", {})
    key = "4+" if bhk >= 4 else str(max(1, bhk))
    grp = g.get(key) or g.get("all")
    return grp if grp and grp.get("n_projects", 0) >= PATTERN_MIN_PROJECTS else None


def _rarity(p: float, best: float) -> int:
    return int(round(PATTERN_W * math.log(max(best, PATTERN_FLOOR) / max(p, PATTERN_FLOOR))))


def bedroom_hub_costs(bhk: int) -> dict[str, int]:
    """Cost of a bedroom door onto each hub type, from what approved bedrooms open onto (an unnamed hub is a passage
    whose label was lost, so it counts as passage). {} when the figure is not strong enough."""
    grp = _group(bhk)
    fig = (grp or {}).get("hub_patterns", {}).get("bedroom_opens_onto")
    if not fig or fig.get("n_projects", 0) < PATTERN_MIN_PROJECTS:
        return {}
    sh = fig["share"]
    p = {"passage": sh.get("passage", 0.0) + sh.get("unnamed", 0.0), "living": sh.get("living", 0.0),
         "dining": sh.get("dining", 0.0)}
    best = max(p.values())
    return {k: _rarity(v, best) for k, v in p.items()}


def hub_door_cost(room_type: str, hub_type: str, bhk: int) -> int | None:
    """v3 price of room ``room_type`` opening onto hub ``hub_type`` (None: no opinion, keep the v2 order cost)."""
    if room_type in ("master", "bedroom", "study"):
        c = bedroom_hub_costs(bhk)
        return c.get(hub_type) if c else None
    if room_type in ("toilet", "bath", "wc"):
        # guest toilet off the living room or the passage, never facing the dining table (critic toilet_door)
        return TOILET_DINING_COST if hub_type == "dining" else 0
    if room_type == "kitchen":
        return KITCHEN_LIVING_COST if hub_type == "living" else 0
    return None


def direction_cost(room_type: str, bhk: int) -> int:
    """Cost of a room lying no further from the entrance than the living room, when approved flats put it further
    (towards the deep façade: E_deep / NE / SE sectors) in a clear majority. 0 when the figure is weak."""
    grp = _group(bhk)
    key = "any_bedroom" if room_type in ("master", "bedroom") else room_type
    fig = (grp or {}).get("direction_from_living", {}).get(key)
    if not fig or fig.get("n_projects", 0) < PATTERN_MIN_PROJECTS:
        return 0
    fwd = sum(fig["angle_hist_8"].get(s, 0.0) for s in FORWARD)
    if fwd < 0.6:
        return 0
    return _rarity(1.0 - fwd, fwd)


def entry_room_share(bhk: int) -> dict[str, float]:
    grp = _group(bhk)
    fig = (grp or {}).get("entrance_room")
    return dict(fig["share"]) if fig and fig.get("n_projects", 0) >= PATTERN_MIN_PROJECTS else {}


FOYER_CARPET = 110.0  # m2: an entrance foyer only for large / premium flats (owner brief)


def wants_foyer(bhk: int, carpet: float) -> bool:
    """Owner brief: enter into the living room; a short foyer only in large / premium flats (3BHK+ or > 110 m2)."""
    return bhk >= 3 or carpet > FOYER_CARPET
