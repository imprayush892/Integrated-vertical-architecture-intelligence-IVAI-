"""Derive deterministic topology priors from the approved-plan topology extraction.

Input  : the JSONL written by ``tools/extract_pdf_topology.py`` (one record per flat plus ``_page_done`` markers)
Output : ``planner_engine/data/rules/mumbai_topology_priors.json``

Everything is counted, nothing is fitted: the same input file always gives the same output file.

Figures (every one carries ``n`` and ``n_projects``; a figure with fewer than ``MIN_PROJECTS`` projects is dropped):

* ``pairs``        unordered room-type pair, flat level: P(shared wall), P(door arc), P(link = door or open gap)
* ``room_links``   ordered type pair, room level: P(a room of type A touches / links to a room of type B | flat has B)
* ``exterior``     P(room touches the façade) and P(room next to an AVS/duct shaft) per type
* ``entrance_hops``mean / median link-hops from the entrance room, per type
* ``entrance``     what the entrance room is, and what it links to
* ``ventilation``  façade only / shaft only / both / neither, for toilets (and kitchens, utilities)
* ``toilet_attach``what a toilet opens onto (master, bedroom, passage, living ...)
* ``bhk``          typical room list per BHK (count of each type per flat)

Quality filter (the extractor is OCR based, so the data is noisy; see ``source.filters`` in the output):

* flats flagged ``suspect_merge`` are dropped, as are flats whose entrance was not found;
* flats where more than ``MAX_UNNAMED`` of the rooms are ``unknown`` / ``unlabeled`` are dropped;
* a room whose area disagrees with its own OCR dimensions by more than 25% (``dims_ok`` false) is not trusted
  for adjacency, façade or hop figures;
* only the 16 engine room types are counted; ``unknown``, ``unlabeled``, ``alcove``, ``multipurpose`` and
  ``servant`` rooms are ignored as neighbours.

``door`` (a drawn door arc) is only measured on pages where door arcs were found at all (about 22% of pages);
``link`` (door arc or open wall gap) is measured on every page but includes some spurious gaps.

Usage::

    .venv/bin/python tools/derive_topology_priors.py [--input /tmp/claude-0/mine/topology.jsonl] [--out PATH]
"""

from __future__ import annotations

import argparse
import json
import statistics
from collections import Counter, defaultdict
from itertools import combinations
from pathlib import Path

MIN_PROJECTS = 5
MAX_UNNAMED = 0.25
ENGINE_TYPES = (
    "living", "dining", "kitchen", "master", "bedroom", "study", "pooja", "toilet", "bath", "wc",
    "foyer", "passage", "utility", "balcony", "dress", "store",
)
TYPES = set(ENGINE_TYPES)
DEFAULT_IN = Path("/tmp/claude-0/mine/topology.jsonl")
DEFAULT_OUT = Path(__file__).resolve().parents[1] / "planner_engine" / "data" / "rules" / "mumbai_topology_priors.json"


# ------------------------------------------------------------------------------------------ counting helpers


class Tally:
    """k successes out of n instances, remembering which project each instance came from."""

    __slots__ = ("k", "n", "per")

    def __init__(self) -> None:
        self.k = 0
        self.n = 0
        self.per: dict[str, list[int]] = defaultdict(lambda: [0, 0])

    def add(self, project: str, hit: bool) -> None:
        self.n += 1
        self.k += int(hit)
        p = self.per[project]
        p[0] += int(hit)
        p[1] += 1

    def fig(self, min_projects: int = MIN_PROJECTS) -> dict | None:
        if len(self.per) < min_projects or self.n == 0:
            return None
        ps = [h / t for h, t in self.per.values() if t >= 3]
        out = {"p": round(self.k / self.n, 4), "n": self.n, "n_projects": len(self.per)}
        if len(ps) >= min_projects:
            out["p_project_median"] = round(statistics.median(ps), 4)
        return out


class Values:
    """A list of numbers with project provenance (for mean / median figures)."""

    __slots__ = ("xs", "projects")

    def __init__(self) -> None:
        self.xs: list[float] = []
        self.projects: set[str] = set()

    def add(self, project: str, x: float) -> None:
        self.xs.append(float(x))
        self.projects.add(project)

    def fig(self, min_projects: int = MIN_PROJECTS) -> dict | None:
        if len(self.projects) < min_projects or not self.xs:
            return None
        return {"mean": round(statistics.fmean(self.xs), 3), "median": round(statistics.median(self.xs), 3),
                "n": len(self.xs), "n_projects": len(self.projects)}


def _prune(d):
    """Drop None figures and the empty dicts they leave behind."""
    if isinstance(d, dict):
        out = {k: _prune(v) for k, v in d.items()}
        return {k: v for k, v in out.items() if v not in (None, {}, [])}
    return d


# ------------------------------------------------------------------------------------------ loading


def load(path: Path) -> tuple[list[dict], dict[tuple[str, int], dict]]:
    flats, pages = [], {}
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        r = json.loads(line)
        if r.get("type") == "_page_done":
            pages[(r["pdf"], r["page"])] = r
        elif "rooms" in r:
            flats.append(r)
    return flats, pages


def clean_flat(f: dict) -> bool:
    if f.get("suspect_merge") or f.get("entrance_via") not in ("link", "wall") or not f["rooms"]:
        return False
    unnamed = sum(1 for r in f["rooms"] if r["type"] in ("unknown", "unlabeled"))
    return unnamed / len(f["rooms"]) <= MAX_UNNAMED


def reliable(r: dict) -> bool:
    return r["type"] in TYPES and r.get("dims_ok") is not False


def edges(f: dict) -> tuple[dict[int, set[int]], dict[int, set[int]], dict[int, set[int]]]:
    """Symmetric adjacency, door-arc and link (door or open gap) maps keyed by room id."""
    ids = {r["id"] for r in f["rooms"]}
    adj: dict[int, set[int]] = {i: set() for i in ids}
    door: dict[int, set[int]] = {i: set() for i in ids}
    link: dict[int, set[int]] = {i: set() for i in ids}
    for r in f["rooms"]:
        a = r["id"]
        for b in r["doors"]:
            if b in ids:
                door[a].add(b)
                door[b].add(a)
        for b in r["doors"] + r["open"]:
            if b in ids:
                link[a].add(b)
                link[b].add(a)
        for b in r["neighbours"] + r["doors"] + r["open"]:
            if b in ids:
                adj[a].add(b)
                adj[b].add(a)
    return adj, door, link


# ------------------------------------------------------------------------------------------ derivation


def derive(flats: list[dict], pages: dict[tuple[str, int], dict]) -> dict:
    use = [f for f in flats if clean_flat(f)]
    door_page = {k for k, v in pages.items() if (v.get("n_door_links") or 0) > 0}

    pair = {k: defaultdict(Tally) for k in ("adj", "door", "link")}
    room_ord = {k: defaultdict(Tally) for k in ("adj", "door", "link")}
    exterior: dict[str, Tally] = defaultdict(Tally)
    shaft: dict[str, Tally] = defaultdict(Tally)
    hops: dict[str, Values] = defaultdict(Values)
    hop1: dict[str, Tally] = defaultdict(Tally)
    ent_type: dict[str, Tally] = defaultdict(Tally)
    ent_link: dict[tuple[str, str], Tally] = defaultdict(Tally)
    vent: dict[str, dict[str, Tally]] = defaultdict(lambda: defaultdict(Tally))
    attach: dict[str, Tally] = defaultdict(Tally)
    bhk_counts: dict[int, dict[str, Counter]] = defaultdict(lambda: defaultdict(Counter))
    bhk_flats: dict[int, list[str]] = defaultdict(list)
    pattern: dict[int, dict[tuple[int, int, int], list[str]]] = defaultdict(lambda: defaultdict(list))
    named_entrance = 0

    for f in use:
        proj = f["pdf"]
        on_door_page = (proj, f["page"]) in door_page
        rooms = {r["id"]: r for r in f["rooms"]}
        ok = {i: r for i, r in rooms.items() if reliable(r)}
        adj, door, link = edges(f)
        by_type: dict[str, list[int]] = defaultdict(list)
        for i, r in ok.items():
            by_type[r["type"]].append(i)

        # -- unordered pairs, flat level
        for a, b in combinations(sorted(by_type), 2):
            hit = {
                "adj": any(j in adj[i] for i in by_type[a] for j in by_type[b]),
                "link": any(j in link[i] for i in by_type[a] for j in by_type[b]),
                "door": any(j in door[i] for i in by_type[a] for j in by_type[b]),
            }
            for k in ("adj", "link"):
                pair[k][f"{a}|{b}"].add(proj, hit[k])
            if on_door_page:
                pair["door"][f"{a}|{b}"].add(proj, hit["door"])
        for a, ids in by_type.items():  # same-type pairs (toilet next to toilet ...)
            if len(ids) >= 2:
                pair["adj"][f"{a}|{a}"].add(proj, any(j in adj[i] for i in ids for j in ids if i != j))
                pair["link"][f"{a}|{a}"].add(proj, any(j in link[i] for i in ids for j in ids if i != j))

        # -- ordered pairs, room level: a room of type A, flat has a B
        for i in ok:
            ta = ok[i]["type"]
            for tb, ids in by_type.items():
                cand = [j for j in ids if j != i]
                if not cand:
                    continue
                room_ord["adj"][f"{ta}>{tb}"].add(proj, any(j in adj[i] for j in cand))
                room_ord["link"][f"{ta}>{tb}"].add(proj, any(j in link[i] for j in cand))
                if on_door_page:
                    room_ord["door"][f"{ta}>{tb}"].add(proj, any(j in door[i] for j in cand))

        # -- exterior / shaft / entrance depth per type
        ent = rooms.get(f["entrance_room"])
        for r in ok.values():
            exterior[r["type"]].add(proj, bool(r.get("exterior")))
            shaft[r["type"]].add(proj, bool(r.get("shaft")))
            if f["entrance_via"] == "link" and r.get("hops") is not None:
                hops[r["type"]].add(proj, r["hops"])
                hop1[r["type"]].add(proj, r["hops"] <= 1)

        # -- entrance room
        if ent is not None and f["entrance_via"] == "link":
            named_entrance += ent["type"] in TYPES
            if ent["type"] in TYPES:
                for t in ("living", "foyer", "bedroom", "master", "kitchen", "dining", "passage"):
                    ent_type[t].add(proj, ent["type"] == t)
                if ent["id"] in ok:
                    for tb, ids in by_type.items():
                        cand = [j for j in ids if j != ent["id"]]
                        if cand:
                            ent_link[(ent["type"], tb)].add(proj, any(j in link[ent["id"]] for j in cand))

        # -- toilet ventilation and what a toilet opens onto
        for i, r in ok.items():
            if r["type"] in ("toilet", "utility", "kitchen", "bath", "wc"):
                ex, sh = bool(r.get("exterior")), bool(r.get("shaft"))
                cls = "both" if ex and sh else "facade_only" if ex else "shaft_only" if sh else "neither"
                for c in ("facade_only", "shaft_only", "both", "neither"):
                    vent[r["type"]][c].add(proj, cls == c)
                vent[r["type"]]["any_facade"].add(proj, ex)
                vent[r["type"]]["any_shaft"].add(proj, sh)
            if r["type"] == "toilet":
                lt = {ok[j]["type"] for j in link[i] if j in ok}
                for t in ("master", "bedroom", "passage", "living", "dining", "kitchen"):
                    attach[f"opens_on_{t}"].add(proj, t in lt)
                attach["opens_on_a_bedroom_or_master"].add(proj, bool(lt & {"master", "bedroom"}))
                attach["opens_on_common_area_only"].add(
                    proj, bool(lt) and not lt & {"master", "bedroom"} and bool(lt & {"passage", "living", "dining", "kitchen", "foyer"}))
                # a toilet reachable from two bedrooms-type rooms (jack-and-jill)
                attach["shared_by_two_bedrooms"].add(proj, sum(1 for j in link[i] if j in ok and ok[j]["type"] in ("master", "bedroom")) >= 2)
        for i, r in ok.items():  # per sleeping room: does it have an attached toilet?
            if r["type"] in ("master", "bedroom") and by_type.get("toilet"):
                has = any(j in link[i] for j in by_type["toilet"])
                attach[f"{r['type']}_has_toilet"].add(proj, has)
                attach[f"{r['type']}_has_toilet_adjacent"].add(proj, any(j in adj[i] for j in by_type["toilet"]))

        # -- per-BHK room list (all labelled rooms, reliable or not: this is a count, not a geometry)
        b = f.get("bhk", 0)
        if 1 <= b <= 4:
            cnt = Counter(r["type"] for r in f["rooms"] if r["type"] in TYPES)
            bhk_flats[b].append(proj)
            for t in TYPES:
                bhk_counts[b][t][cnt.get(t, 0)] += 1
            # toilet pattern: (toilets, toilets sharing a wall with a sleeping room, sleeping rooms with such a toilet)
            sleep = [i for i, r in ok.items() if r["type"] in ("master", "bedroom")]
            toil = by_type.get("toilet", [])
            if len(sleep) == b:
                att = sum(1 for t in toil if any(j in adj[t] for j in sleep))
                sw = sum(1 for s_ in sleep if any(j in adj[s_] for j in toil))
                pattern[b][(len(toil), att, sw)].append(proj)

    # -------------------------------------------------------------------------------- assemble
    out: dict = {
        "source": {
            "what": "Topology of municipal-approved Mumbai residential plans, extracted from vector PDFs by tools/extract_pdf_topology.py",
            "dataset": "Residential_Projects_28_to_77 (50 projects, 655 pages)",
            "flats_extracted": len(flats),
            "flats_used": len(use),
            "projects_used": len({f["pdf"] for f in use}),
            "min_projects_per_figure": MIN_PROJECTS,
            "filters": {
                "drop_suspect_merge": True, "require_entrance": True, "max_unnamed_room_share": MAX_UNNAMED,
                "room_trusted_if": "area within 25% of its own OCR dimensions (or no dimensions printed)",
                "door": "door arcs; measured only on pages where any door arc was found",
                "link": "door arc or open wall gap (includes some spurious gaps)",
            },
            "licence_note": "Counts derived from public municipal approvals; no plan geometry is stored.",
            "generated_by": "tools/derive_topology_priors.py",
        },
    }
    out["pairs"] = {}
    keys = sorted(set().union(*[set(pair[k]) for k in pair]))
    for key in keys:
        row = {k: pair[k][key].fig() for k in ("adj", "door", "link") if key in pair[k]}
        out["pairs"][key] = row
    out["room_links"] = {}
    for key in sorted(set().union(*[set(room_ord[k]) for k in room_ord])):
        out["room_links"][key] = {k: room_ord[k][key].fig() for k in ("adj", "door", "link") if key in room_ord[k]}
    out["exterior"] = {t: {"facade": exterior[t].fig(), "shaft": shaft[t].fig()} for t in sorted(exterior)}
    out["entrance_hops"] = {t: {**(hops[t].fig() or {}), "p_within_1": (hop1[t].fig() or {}).get("p")} for t in sorted(hops)
                            if hops[t].fig()}
    out["entrance"] = {
        "room_type": {t: ent_type[t].fig() for t in sorted(ent_type)},
        "links_to": {f"{a}>{b}": ent_link[(a, b)].fig() for (a, b) in sorted(ent_link)},
        "note": "room_type is among flats whose entrance room carries an engine room type; "
                "'foyer' is rare because most Mumbai plans open the front door straight into the living room",
    }
    out["ventilation"] = {t: {c: v.fig() for c, v in sorted(cl.items())} for t, cl in sorted(vent.items())}
    out["toilet_attach"] = {k: v.fig() for k, v in sorted(attach.items())}

    bhk: dict = {}
    for b in sorted(bhk_flats):
        projs = bhk_flats[b]
        if len(set(projs)) < MIN_PROJECTS:
            continue
        n = len(projs)
        types = {}
        for t in ENGINE_TYPES:
            c = bhk_counts[b][t]
            present = sum(v for k, v in c.items() if k > 0)
            if not present:
                continue
            nz = [k for k, v in c.items() if k > 0 for _ in range(v)]
            types[t] = {
                "p_present": round(present / n, 3),
                "mean": round(sum(k * v for k, v in c.items()) / n, 2),
                "mode_when_present": Counter(nz).most_common(1)[0][0],
                "hist": {str(k): c[k] for k in sorted(c)},
            }
        typical = {t: v["mode_when_present"] for t, v in types.items() if v["p_present"] >= 0.5}
        pats = pattern[b]
        n_pat = sum(len(v) for v in pats.values())
        top = sorted(pats.items(), key=lambda kv: (-len(kv[1]), kv[0]))[:5]
        toilet_pattern = [
            {"toilets": k[0], "toilets_next_to_sleeping_room": k[1], "sleeping_rooms_with_toilet": k[2],
             "share": round(len(v) / n_pat, 3), "n": len(v), "n_projects": len(set(v))}
            for k, v in top if len(set(v)) >= MIN_PROJECTS
        ] if n_pat else []
        bhk[str(b)] = {"n": n, "n_projects": len(set(projs)), "sleeping_rooms": b, "typical": typical,
                       "toilet_pattern": toilet_pattern, "toilet_pattern_n": n_pat, "types": types}
    out["bhk"] = bhk
    return _prune(out)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--input", type=Path, default=DEFAULT_IN)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    a = ap.parse_args()
    flats, pages = load(a.input)
    res = derive(flats, pages)
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(json.dumps(res, indent=1, sort_keys=True) + "\n")
    s = res["source"]
    print(f"{a.out}: {s['flats_used']}/{s['flats_extracted']} flats, {s['projects_used']} projects, "
          f"{len(res.get('pairs', {}))} pairs, {len(res.get('room_links', {}))} ordered pairs, {len(res.get('bhk', {}))} BHK lists")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
