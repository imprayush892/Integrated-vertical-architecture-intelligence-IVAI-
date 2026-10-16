"""Summarize OCR'd room labels mined from Mumbai approved plans.

Usage: summarize_mumbai.py rooms.jsonl summary.json

Cleans OCR noise, dedupes repeats across floors, weights each project equally
within a room type, and reports percentiles for sizes, widths and BHK groups.
"""

import json
import re
import sys
from collections import Counter, defaultdict

PCTS = (5, 10, 25, 50, 75, 90, 95)
HABITABLE = {"bedroom", "master", "living", "dining", "study"}
WIDTH_TYPES = ("passage_width", "lobby_width", "chajja_width")
KEYWORDS = ("AVS", "DUCT", "SHAFT")
DIM_RANGE = (0.6, 12.0)
AREA_RANGE = (1.0, 80.0)
MAX_DECIMALS = 2  # dims with 3+ decimals look like merged OCR tokens (1.454)


def decimals(v):
    s = repr(float(v))
    return len(s.split(".")[1].rstrip("0")) if "." in s else 0


def clean_name(name):
    return re.sub(r"\s+", " ", (name or "").replace("|", " ")).strip().upper()


def row_width(r):
    for k in ("width", "short", "long"):
        v = r.get(k)
        if isinstance(v, (int, float)):
            return float(v)
    return None


def valid_width(r):
    w = row_width(r)
    if w is None or decimals(w) > MAX_DECIMALS:
        return False
    lo, hi = (0.15, 3.0) if r["type"] == "chajja_width" else (0.6, 12.0)
    return lo <= w <= hi


def valid(r):
    try:
        a, b, s, lg = (float(r[k]) for k in ("a", "b", "short", "long"))
        area = float(r["area"])
    except (KeyError, TypeError, ValueError):
        return False
    if any(not DIM_RANGE[0] <= v <= DIM_RANGE[1] for v in (a, b, s, lg)):
        return False
    if not AREA_RANGE[0] <= area <= AREA_RANGE[1]:
        return False
    if any(decimals(v) > MAX_DECIMALS for v in (a, b)):
        return False
    return not (r.get("type") in HABITABLE and s < 0.75)


def wpercentile(pairs, p):
    """Weighted percentile (linear interpolation on cumulative weight midpoints)."""
    pairs = sorted(pairs)
    total = sum(w for _, w in pairs)
    if len(pairs) == 1:
        return pairs[0][0]
    cum, pos = 0.0, []
    for _v, w in pairs:
        pos.append((cum + w / 2) / total)
        cum += w
    q = p / 100
    if q <= pos[0]:
        return pairs[0][0]
    if q >= pos[-1]:
        return pairs[-1][0]
    for i in range(1, len(pos)):
        if q <= pos[i]:
            t = (q - pos[i - 1]) / (pos[i] - pos[i - 1])
            return pairs[i - 1][0] + t * (pairs[i][0] - pairs[i - 1][0])
    return pairs[-1][0]


def pct_block(pairs):
    return {f"p{p}": round(wpercentile(pairs, p), 3) for p in PCTS}


def weighted(items, getter):
    """items: list of (project, row). Weight = 1/(rows of that project)."""
    per = Counter(p for p, _ in items)
    return [(getter(r), 1.0 / per[p]) for p, r in items]


def stats(items, fields):
    out = {"n": len(items), "n_projects": len({p for p, _ in items})}
    for name, getter in fields.items():
        out[name] = pct_block(weighted(items, getter))
    return out


SIZE_FIELDS = {
    "short": lambda r: r["short"],
    "long": lambda r: r["long"],
    "area": lambda r: r["area"],
    "aspect": lambda r: r["long"] / r["short"],
}


def main(src, dst):
    pages_done = defaultdict(int)
    rows, bad_json, errors = [], 0, 0
    kw_raw, kw_proj = Counter(), defaultdict(set)
    with open(src) as fh:
        for line in fh:
            try:
                r = json.loads(line)
            except ValueError:
                bad_json += 1
                continue
            if r.get("type") == "_page_done":
                pages_done[r.get("pdf")] += 1
                continue
            if r.get("error"):
                errors += 1
                continue
            nm = clean_name(r.get("name"))
            for k in KEYWORDS:
                if k in nm:
                    kw_raw[k] += 1
                    kw_proj[k].add(r.get("pdf"))
            rows.append(r)

    kept = [r for r in rows if (valid_width(r) if r.get("type") in WIDTH_TYPES else valid(r))]
    seen, uniq = set(), []
    for r in kept:
        key = (r["pdf"], clean_name(r.get("name")), r.get("a"), r.get("b"), r["type"], row_width(r))
        if key not in seen:
            seen.add(key)
            uniq.append(r)

    by_type, widths = defaultdict(list), defaultdict(list)
    for r in uniq:
        t = r["type"]
        (widths if t in WIDTH_TYPES else by_type)[t].append((r["pdf"], r))
    types = {t: stats(v, SIZE_FIELDS) for t, v in sorted(by_type.items())}
    width_out = {
        t: stats(v, {"width": row_width}) for t, v in sorted(widths.items())
    }

    # BHK inference: bedrooms+masters per (pdf, page), typical = mode over pages
    page_beds = defaultdict(int)
    page_rows = defaultdict(list)
    for r in uniq:
        k = (r["pdf"], r["page"])
        page_rows[k].append(r)
        if r["type"] in ("bedroom", "master"):
            page_beds[k] += 1
    proj_counts = defaultdict(list)
    for (pdf, _), n in page_beds.items():
        proj_counts[pdf].append(n)
    typical = {}
    for pdf, counts in proj_counts.items():
        c = Counter(counts)
        top = max(c.values())
        typical[pdf] = min(n for n, v in c.items() if v == top)
    bhk_dist = Counter(typical.values())

    bhk_items = defaultdict(lambda: defaultdict(list))
    for (pdf, page), rs in page_rows.items():
        if pdf not in typical or page_beds.get((pdf, page)) != typical[pdf]:
            continue
        bhk = min(typical[pdf], 5)
        for r in rs:
            if r["type"] in ("living", "kitchen", "bedroom"):
                bhk_items[bhk][r["type"]].append((pdf, r))
    per_bhk = {}
    for bhk, d in sorted(bhk_items.items()):
        per_bhk[f"{bhk}BHK" if bhk < 5 else "5+BHK"] = {
            "n_projects": sum(1 for v in typical.values() if min(v, 5) == bhk),
            **{t: stats(v, {"area": SIZE_FIELDS["area"]}) for t, v in sorted(d.items())},
        }

    summary = {
        "source": src,
        "rows_read": len(rows),
        "rows_error": errors,
        "rows_bad_json": bad_json,
        "rows_after_noise_filter": len(kept),
        "rows_unique": len(uniq),
        "n_projects": len({r["pdf"] for r in uniq}),
        "pages_done_total": sum(pages_done.values()),
        "pages_done_by_project": dict(sorted(pages_done.items())),
        "types": types,
        "widths": width_out,
        "typical_bhk_per_project": dict(sorted(typical.items())),
        "typical_bhk_distribution": {str(k): v for k, v in sorted(bhk_dist.items())},
        "per_bhk_area_medians": per_bhk,
        "keyword_mentions": {
            k: {"labels": kw_raw[k], "projects": len(kw_proj[k])} for k in KEYWORDS
        },
    }
    with open(dst, "w") as fh:
        json.dump(summary, fh, indent=2)
    return summary


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    s = main(sys.argv[1], sys.argv[2])
    print(f"projects={s['n_projects']} unique_rows={s['rows_unique']} pages={s['pages_done_total']}")
    for t, v in s["types"].items():
        print(
            f"{t:10s} n={v['n']:4d} proj={v['n_projects']:2d} short50={v['short']['p50']} "
            f"long50={v['long']['p50']} area50={v['area']['p50']} aspect50={v['aspect']['p50']}"
        )
    for t, v in s["widths"].items():
        print(f"{t:14s} n={v['n']:4d} proj={v['n_projects']:2d} w50={v['width']['p50']}")
    print("bhk dist", s["typical_bhk_distribution"], "kw", s["keyword_mentions"])
    for b, d in s["per_bhk_area_medians"].items():
        print(b, {t: d[t]["area"]["p50"] for t in ("living", "kitchen", "bedroom") if t in d})
