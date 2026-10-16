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


def from_programme(unit: Unit, prog: Programme) -> Diagram:
    n = len(prog.rooms)
    links = complete_links(prog)
    rooms = []
    for i, r in enumerate(prog.rooms):
        a = 2 * math.pi * i / max(1, n)
        rooms.append(
            DiagramRoom(
                id=r.id,
                type=r.type,
                name=r.name,
                target_area=r.target_area,
                needs_window=prior_needs_window(r.type, r.needs_window),
                optional=r.optional,
                placed=True,
                x=round(math.cos(a) * 140, 1),
                y=round(math.sin(a) * 140, 1),
            )
        )
    _spring_layout(rooms, links, prog.entry)
    return Diagram(unit_id=unit.id, rooms=rooms, links=links, entry=prog.entry)


def _spring_layout(rooms: list[DiagramRoom], links: list[tuple[str, str]], entry: str, iters: int = 200) -> None:
    """Deterministic force layout for the bubble view (UI only)."""
    pos = {r.id: [r.x, r.y] for r in rooms}
    ids = list(pos)
    k = 90.0
    for it in range(iters):
        disp = {i: [0.0, 0.0] for i in ids}
        for a in range(len(ids)):
            for b in range(a + 1, len(ids)):
                pa, pb = pos[ids[a]], pos[ids[b]]
                dx, dy = pa[0] - pb[0], pa[1] - pb[1]
                d = max(1.0, math.hypot(dx, dy))
                f = k * k / d
                disp[ids[a]][0] += dx / d * f
                disp[ids[a]][1] += dy / d * f
                disp[ids[b]][0] -= dx / d * f
                disp[ids[b]][1] -= dy / d * f
        for a, b in links:
            if a not in pos or b not in pos:
                continue
            pa, pb = pos[a], pos[b]
            dx, dy = pa[0] - pb[0], pa[1] - pb[1]
            d = max(1.0, math.hypot(dx, dy))
            f = d * d / k
            disp[a][0] -= dx / d * f
            disp[a][1] -= dy / d * f
            disp[b][0] += dx / d * f
            disp[b][1] += dy / d * f
        t = 20.0 * (1 - it / iters) + 1
        for i in ids:
            dx, dy = disp[i]
            d = max(1e-9, math.hypot(dx, dy))
            pos[i][0] += dx / d * min(d, t)
            pos[i][1] += dy / d * min(d, t)
        if entry in pos:
            pos[entry] = [0.0, 200.0]
    for r in rooms:
        r.x, r.y = round(pos[r.id][0], 1), round(pos[r.id][1], 1)


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
