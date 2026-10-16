"""Mine deterministic layout priors from ResPlan (CC BY 4.0). Usage: python -I mine_resplan.py <pkl> <out.json> [n]"""
import json, math, pickle, sys, statistics as st
from collections import Counter, defaultdict
import shapely
from shapely.geometry import Polygon, MultiPolygon
data = pickle.load(open(sys.argv[1], "rb"))
N = int(sys.argv[3]) if len(sys.argv) > 3 else len(data)
TYPES = ["bedroom", "bathroom", "kitchen", "living", "balcony"]
def parts(g):
    if g is None or g.is_empty: return []
    if isinstance(g, Polygon): return [g]
    return [p for p in getattr(g, "geoms", []) if isinstance(p, Polygon) and p.area > 0]
def dims(p):
    r = p.minimum_rotated_rectangle
    c = list(r.exterior.coords)
    a, b = math.dist(c[0], c[1]), math.dist(c[1], c[2])
    return min(a, b), max(a, b)
area = defaultdict(lambda: defaultdict(list))   # bhk -> type -> [m2]
aspect = defaultdict(list); short = defaultdict(list)
window = defaultdict(lambda: [0, 0])  # type -> [with window, total]
doorpairs = Counter(); bal_long_out = []; bal_exposed = []; scale_ratio = []; counts = Counter()
for i, p in enumerate(data[:N]):
    rooms = []
    for t in TYPES:
        for q in parts(p.get(t)):
            rooms.append((t, q))
    if not rooms: continue
    tot = shapely.union_all([q for _, q in rooms]).area
    if not p.get("net_area") or tot <= 0: continue
    ka = p["net_area"] / tot          # m2 per dataset unit2 (per plan)
    kl = math.sqrt(ka)                # m per dataset unit
    scale_ratio.append(kl)
    wd = (p.get("wall_depth") or 4.0)
    # metric check: wall depth in dataset units
    bhk = sum(1 for t, _ in rooms if t == "bedroom")
    counts[bhk] += 1
    wins = parts(p.get("window")); doors = parts(p.get("door"))
    if p.get("front_door") is not None: doors = doors + parts(p["front_door"])
    solid = shapely.union_all([q for _, q in rooms] + parts(p.get("wall")))
    outer = solid.buffer(wd * 0.6).buffer(-wd * 0.6)
    for t, q in rooms:
        area[bhk][t].append(q.area * ka)
        s, l = dims(q)
        if s > 0: aspect[t].append(l / s); short[t].append(s * kl)
        window[t][1] += 1
        if any(w.distance(q) < wd * 1.2 for w in wins): window[t][0] += 1
        if t == "balcony":
            # which side of the balcony is exposed (not touching walls/rooms)
            others = shapely.union_all([r for tt, r in rooms if r is not q] + parts(p.get("wall")))
            ring = q.minimum_rotated_rectangle.exterior; cs = list(ring.coords)
            sides = [(math.dist(cs[k], cs[k + 1]), shapely.LineString([cs[k], cs[k + 1]])) for k in range(4)]
            exp = [(L, ln.difference(others.buffer(wd * 0.8)).length / max(L, 1e-9)) for L, ln in sides]
            long_exp = max(e for L, e in exp if L >= max(x[0] for x in sides) - 1e-6)
            short_exp = max(e for L, e in exp if L <= min(x[0] for x in sides) + 1e-6)
            bal_long_out.append(long_exp >= short_exp)
            bal_exposed.append(sum(L * e for L, e in exp) / sum(L for L, _ in exp))
    for d in doors:
        near = [t for t, q in rooms if q.distance(d) < wd * 0.8]
        if len(near) == 2: doorpairs[tuple(sorted(near))] += 1
        elif len(near) == 1: doorpairs[(near[0], "outside")] += 1
    if i % 2000 == 0: print(i, file=sys.stderr)
def q(xs, k): 
    xs = sorted(xs); return round(xs[min(len(xs) - 1, int(k * len(xs)))], 3) if xs else None
out = {
    "source": "ResPlan (m-agour/ResPlan), 17,000 residential plans, CC BY 4.0",
    "plans": N, "metres_per_unit_median": round(st.median(scale_ratio), 3) if scale_ratio else None,
    "bhk_counts": dict(counts),
    "area_by_bhk": {str(b): {t: {"p10": q(v, .1), "p50": q(v, .5), "p90": q(v, .9), "n": len(v)} for t, v in d.items()} for b, d in sorted(area.items()) if counts[b] >= 50},
    "aspect": {t: {"p50": q(v, .5), "p90": q(v, .9), "p95": q(v, .95)} for t, v in aspect.items()},
    "short_side": {t: {"p05": q(v, .05), "p50": q(v, .5)} for t, v in short.items()},
    "window_share": {t: round(a / b, 3) for t, (a, b) in window.items() if b},
    "door_pairs": {f"{a}-{b}": n for (a, b), n in doorpairs.most_common(25)},
    "balcony_long_side_exposed_share": round(sum(bal_long_out) / len(bal_long_out), 3) if bal_long_out else None,
    "balcony_exposed_perimeter_p50": q(bal_exposed, .5),
}
json.dump(out, open(sys.argv[2], "w"), indent=1)
print(json.dumps(out, indent=1)[:4000])
