"""Build data/mined/typology_v1.json: typical-floor outline, plate typology and core data for the 50 approved projects.

For every project one typical-floor page is used (``PAGES``: page index and the wall-mask closing radius that gave the
best outline when the outlines were reviewed by eye, see docs/quality/typology-cores.md). Per page:
  * outline  : largest closed outer contour of the wall mask (tools/extract_plate_outlines.py), plan-frame metres,
  * seeds    : OCR labels (tools/extract_pdf_topology.py: ocr_lines/seeds_from_lines), registered into the same frame,
               giving lifts / stairs / lobbies (core clusters) and kitchen / living counts (flats per floor),
  * flats    : flat records of the page in data/mined/topology_v1.jsonl.gz (bhk, room-area sum),
  * typology : planner_engine.typology.classify() on the outline.
OCR is slow (~15 s per page) so seeds are cached in ``--cache`` (a json file).

Usage (two stages):
  python -I build_typology_dataset.py --pages typology_v1.json --cache seeds_cache.json --raw-out raw.json [--workers 2]
  python -I build_typology_dataset.py --raw-out raw.json --finalize typology_v1.json --out typology_v1.json
  python -I build_typology_dataset.py --tables typology_v1.json        # markdown tables used in the doc
The by-eye labels (eye_label_first / eye_label) and the excluded outlines live in typology_v1.json itself.
"""
from __future__ import annotations

import argparse
import glob
import gzip
import json
import os
import sys
from pathlib import Path

os.environ.setdefault("OMP_THREAD_LIMIT", "1")
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))

ROOT = "/tmp/claude-0/mainwt/Residential_Projects_28_to_77/"
TOPO = HERE.parent.parent / "data" / "mined" / "topology_v1.jsonl.gz"

# project number -> (page, closing radius m). Chosen by looking at every candidate outline (see the doc).
PAGES: dict[int, tuple[int, float]] = {}


def load_pages(path: Path) -> None:
    """``path`` is {project number: [page, close_m]} or a previous typology_v1.json (its projects carry both)."""
    data = json.loads(path.read_text())
    if "projects" in data:
        for p in data["projects"]:
            PAGES[int(p["num"])] = (int(p["page"]), float(p["close_m"]))
        return
    for k, v in data.items():
        PAGES[int(k)] = (int(v[0]), float(v[1]))


def seeds_of(pdf: str, pno: int) -> list[dict]:
    import extract_pdf_topology as T
    import pymupdf

    doc = pymupdf.open(pdf)
    page = doc[pno]
    drawings = page.get_drawings()
    lines, _dh, vert = T.ocr_lines(page, drawings)
    seeds = T.seeds_from_lines(lines)
    if vert:
        seeds = T.combine_seeds(seeds, [T.unrotate_seed(v, vert[1]) for v in T.seeds_from_lines(vert[0])])
    return [{k: s[k] for k in ("name", "type", "x", "y") if k in s} | {"dims": s.get("dims")} for s in seeds]


def job(args):
    num, pno, close_m, cache = args
    import extract_plate_outlines as X

    pdfdir = [p for p in glob.glob(ROOT + f"Residential_{num}_*")][0]
    name = os.path.basename(pdfdir)
    pdf = glob.glob(pdfdir + "/*.pdf")[0]
    scale, title = SCALES[(name, pno)]
    res, frame = X.outlines_for_page(pdf, pno, scale, close_m=close_m)
    comps = [{"raw_m2": round(a, 1), "xy": [[round(x, 2), round(y, 2)] for x, y in q.exterior.coords]} for a, q in res[:3]]
    seeds = cache.get(name)
    if seeds is None:
        seeds = seeds_of(pdf, pno)
    plan = X.page_pts_to_plan([[s["x"], s["y"]] for s in seeds], frame) if seeds else []
    for s, p in zip(seeds, plan, strict=True):
        s["px"], s["py"] = round(float(p[0]), 2), round(float(p[1]), 2)
    return {"num": num, "project": name, "page": pno, "title": title, "close_m": close_m, "scale_m_per_pt": scale,
            "frame": frame, "components": comps, "seeds": seeds}


SCALES: dict = {}


def init_scales():
    for ln in gzip.open(TOPO, "rt"):
        r = json.loads(ln)
        if r.get("type") == "_page_done" and r.get("scale_m_per_pt"):
            SCALES[(r["pdf"], r["page"])] = (r["scale_m_per_pt"], r["title"])


def flats_of(name: str, pno: int) -> list[dict]:
    out = []
    for ln in gzip.open(TOPO, "rt"):
        r = json.loads(ln)
        if r.get("type") == "_page_done" or r["pdf"] != name or r["page"] != pno:
            continue
        rooms = [x for x in r["rooms"] if not x.get("common")]
        out.append({"bhk": r.get("bhk"), "rooms": len(rooms), "area_m2": round(sum(x["area"] for x in rooms), 1),
                    "suspect_merge": bool(r.get("suspect_merge"))})
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pages", help="json {project number: [page, close_m]}")
    ap.add_argument("--cache", help="json cache of OCR seeds")
    ap.add_argument("--raw-out", default="", help="intermediate json with outlines + seeds (no classification)")
    ap.add_argument("--workers", type=int, default=2)
    ap.add_argument("--finalize", help="labels json {first, final, excluded}: classify raw-out and write --out")
    ap.add_argument("--out", help="final data/mined/typology_v1.json (with --finalize)")
    ap.add_argument("--tables", help="print the markdown tables of this typology_v1.json and exit")
    a = ap.parse_args()
    if a.tables:
        print(markdown_tables(a.tables))
        return
    if a.finalize:
        lab = json.loads(Path(a.finalize).read_text())
        if "projects" in lab:  # a previous typology_v1.json carries the by-eye labels
            ps = lab["projects"]
            lab = {"first": {str(p["num"]): p.get("eye_label_first") for p in ps},
                   "final": {str(p["num"]): p.get("eye_label") for p in ps},
                   "excluded": {str(p["num"]): p["extraction"] for p in ps if p.get("extraction") != "ok"}}
        finalize(a.raw_out, lab["first"], lab["final"], lab["excluded"], a.out)
        return
    load_pages(Path(a.pages))
    init_scales()
    cpath = Path(a.cache)
    cache = json.loads(cpath.read_text()) if cpath.exists() else {}
    from concurrent.futures import ProcessPoolExecutor

    jobs = [(n, p, c, {k: v for k, v in cache.items() if k.startswith(f"Residential_{n}_")}) for n, (p, c) in sorted(PAGES.items())]
    out = []
    with ProcessPoolExecutor(min(2, a.workers), initializer=init_scales) as ex:
        for r in ex.map(job, jobs):
            cache[r["project"]] = [{k: v for k, v in s.items() if k not in ("px", "py")} for s in r["seeds"]]
            cpath.write_text(json.dumps(cache))
            out.append(r)
            print(r["num"], r["page"], len(r["seeds"]), flush=True)
    Path(a.raw_out).write_text(json.dumps(out))



# ---------------------------------------------------------------------------------------------------------------
# finalize: classify, derive core clusters / flats per floor, write data/mined/typology_v1.json
# ---------------------------------------------------------------------------------------------------------------
import math  # noqa: E402
import re  # noqa: E402

CORE_TYPES_SEED = {"lift", "fire_lift", "stair"}
ORD = re.compile(r"(\d{1,2})\s*(?:ST|ND|RD|TH)", re.I)


def floors_from_title(title: str) -> int | None:
    """Highest floor ordinal mentioned in the sheet title (a lower bound for the building's storeys)."""
    nums = [int(x) for x in ORD.findall(title or "")]
    return max(nums) if nums else None


def core_clusters(seeds: list[dict], outline) -> list[dict]:
    from planner_engine import typology

    pts = [(s["px"], s["py"], s["type"]) for s in seeds if s.get("type") in CORE_TYPES_SEED]
    from shapely.geometry import Point

    area = outline.buffer(2.0)
    pts = [p for p in pts if area.contains(Point(p[0], p[1]))]
    clusters: list[list[tuple]] = []
    for p in pts:
        hit = [c for c in clusters if any(math.hypot(p[0] - q[0], p[1] - q[1]) <= 10.0 for q in c)]
        merged = [p] + [q for c in hit for q in c]
        clusters = [c for c in clusters if c not in hit] + [merged]
    out = []
    for c in clusters:
        lifts = [q for q in c if q[2] in ("lift", "fire_lift")]
        if not lifts:
            continue
        cx = sum(q[0] for q in c) / len(c)
        cy = sum(q[1] for q in c) / len(c)
        pos = typology.core_position(outline, (cx, cy))
        out.append({"xy": [round(cx, 1), round(cy, 1)], "n_lifts": sum(1 for q in c if q[2] == "lift"),
                    "n_fire_lifts": sum(1 for q in c if q[2] == "fire_lift"), "n_stairs": sum(1 for q in c if q[2] == "stair"),
                    "position": pos["position"], "u": pos["u"], "v": pos["v"]})
    out.sort(key=lambda d: -(d["n_lifts"] + d["n_fire_lifts"]))
    return out


def finalize(raw_path: str, labels_first: dict, labels_final: dict, excluded: dict, out_path: str) -> dict:
    from shapely.geometry import Point, Polygon

    from planner_engine import typology

    init_scales()
    raw = json.loads(Path(raw_path).read_text())
    projects = []
    for r in sorted(raw, key=lambda r: r["num"]):
        if not r["components"]:
            projects.append({"num": r["num"], "project": r["project"], "extraction": "no outline"})
            continue
        poly = Polygon(r["components"][0]["xy"])
        res = typology.classify(poly)
        buf = poly.buffer(1.0)
        seeds = r["seeds"]
        inside = [s for s in seeds if buf.contains(Point(s["px"], s["py"]))]
        n_k = sum(1 for s in inside if s.get("type") == "kitchen")
        n_l = sum(1 for s in inside if s.get("type") == "living")
        cores = core_clusters(seeds, poly)
        flats = flats_of(r["project"], r["page"])
        good = [f for f in flats if not f["suspect_merge"] and f["rooms"] >= 4]
        num = str(r["num"])
        projects.append({
            "num": r["num"], "project": r["project"], "page": r["page"], "title": r["title"], "close_m": r["close_m"],
            "floors_title": floors_from_title(r["title"]),
            "outline_m2": round(poly.area, 1),
            "extraction": excluded.get(num, "ok"),
            "label": res.label, "subtype": res.subtype, "confidence": round(res.confidence, 2),
            "reasons": res.reasons, "descriptors": res.descriptors, "wings": res.to_dict()["wings"],
            "notches": res.to_dict()["notches"],
            "notch_rects": [[round(v, 1) for v in n.polygon.bounds] for n in res.notches],
            "eye_label_first": labels_first.get(num), "eye_label": labels_final.get(num),
            "agrees": (res.label == labels_final.get(num)) if num not in excluded else None,
            "clean_outline": [[round(x, 1), round(y, 1)] for x, y in (res.clean.exterior.coords if res.clean else [])],
            "n_kitchen": n_k, "n_living": n_l, "flats_per_floor_est": max(n_k, n_l),
            "topology_flats_on_page": len(flats), "topology_flats_ok": len(good),
            "flat_areas_m2": [f["area_m2"] for f in good], "flat_bhk": [f["bhk"] for f in good],
            "cores": cores,
        })
    Path(out_path).write_text(json.dumps({"version": 1, "n_projects": len(projects), "projects": projects}, indent=1))
    return {"projects": projects}


def markdown_tables(json_path: str) -> str:
    """Per-project table and per-type statistics (flats per floor, flat sizes, core position) as markdown."""
    import statistics as st
    from collections import Counter

    d = json.loads(Path(json_path).read_text())["projects"]
    ok = [p for p in d if p.get("extraction") == "ok"]
    out = ["| # | project | page / sheet | outline m2 | bbox m | classifier | conf | eye (final) | eye (first pass) | flats/floor est | cores (lifts+fire, position) |",
           "|---|---|---|---|---|---|---|---|---|---|---|"]
    for p in d:
        if "label" not in p:
            out.append(f"| {p['num']} | {p['project']} | - | - | - | no outline | - | - | - | - | - |")
            continue
        bb = p["descriptors"]["bbox_m"]
        cores = "; ".join(f"{c['n_lifts']}+{c['n_fire_lifts']} {c['position']}" for c in p["cores"][:3]) or "-"
        mark = "" if p["extraction"] == "ok" else f" (excluded: {p['extraction']})"
        out.append(f"| {p['num']} | {p['project'].split('_', 2)[2]} | p{p['page']} {p['title'][:26]} | {p['outline_m2']:.0f} | "
                   f"{bb[0]:.0f} x {bb[1]:.0f} | {p['label']}{('/' + p['subtype']) if p['subtype'] else ''} | {p['confidence']:.2f} | "
                   f"{p['eye_label']}{mark} | {p['eye_label_first']} | {p['flats_per_floor_est']} | {cores} |")
    rows = ["| type | n | outline m2 (p50, range) | bbox long x short m (p50) | flats/floor est (p50, range) | flat area m2 (p50, p10-p90, n) | "
            "core: central / reentrant / end / side | passenger+fire lifts per core (p50) | cores per plate (p50) |",
            "|---|---|---|---|---|---|---|---|---|"]
    for lab in ("point_square", "linear_slab", "L", "T", "H", "cruciform", "courtyard", "Y", "other"):
        ps = [p for p in ok if p["label"] == lab]
        if not ps:
            continue
        areas = sorted(a for p in ps for a in p["flat_areas_m2"] if 25 <= a <= 400)
        fpf = [p["flats_per_floor_est"] for p in ps]
        pos = Counter(c["position"] for p in ps for c in p["cores"][:1])
        lifts = [c["n_lifts"] + c["n_fire_lifts"] for p in ps for c in p["cores"][:1]]
        ncores = [len(p["cores"]) for p in ps if p["cores"]]
        long_ = [p["descriptors"]["bbox_m"][0] for p in ps]
        short_ = [p["descriptors"]["bbox_m"][1] for p in ps]
        q = lambda xs, f: xs[min(len(xs) - 1, int(f * len(xs)))] if xs else float("nan")  # noqa: E731
        rows.append(
            f"| {lab} | {len(ps)} | {st.median(p['outline_m2'] for p in ps):.0f} ({min(p['outline_m2'] for p in ps):.0f}-"
            f"{max(p['outline_m2'] for p in ps):.0f}) | {st.median(long_):.0f} x {st.median(short_):.0f} | "
            f"{st.median(fpf):.0f} ({min(fpf)}-{max(fpf)}) | "
            f"{(st.median(areas) if areas else float('nan')):.0f} ({q(areas, .1):.0f}-{q(areas, .9):.0f}, n={len(areas)}) | "
            f"{pos.get('central', 0)} / {pos.get('reentrant', 0)} / {pos.get('end', 0)} / {pos.get('side', 0)} | "
            f"{st.median(lifts) if lifts else float('nan'):.0f} | {st.median(ncores) if ncores else float('nan'):.0f} |")
    return "\n".join(out) + "\n\n" + "\n".join(rows) + "\n"


if __name__ == "__main__":
    main()
