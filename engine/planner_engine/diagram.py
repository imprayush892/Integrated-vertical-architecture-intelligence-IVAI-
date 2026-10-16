"""Stage 7a - bubble diagram and pre-check (before the solver runs).

The pre-check is cheap arithmetic and graph work. It catches diagrams that cannot become a plan and names
the smallest set of rooms that clash, in plain words, with 2-4 candidate fixes each.
"""

from __future__ import annotations

import math
from collections import deque

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
