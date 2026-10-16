"""Reference drawings for the stage-8 gate: 10 DXF floors with hand-derived expected rule results.

Each case is a 2BHK flat next to a common passage and two staircases, varied the way real files vary
(mm units, lying headers, line-only walls, odd layer names) or with one rule broken on purpose.
Expected results come from the clause thresholds by construction (margins are far from review bands).
"""

from __future__ import annotations

import os
from pathlib import Path

import ezdxf

ALL_PASS = {"DCPR-37-1-a": "pass", "DCPR-37-1-b": "pass", "DCPR-37-1-c": "pass", "DCPR-37-2": "pass",
            "DCPR-37-6-c": "pass", "DCPR-39-1": "pass", "DCPR-39-2": "pass", "DCPR-37-12": "pass",
            "NBC-4-4.4.2.1": "pass", "NBC-4-4.4.2.4.2": "pass", "NBC-4-4.4.2.4": "pass"}

# (name, label, x0, y0, x1, y1, window side, window length)
BASE = [  # rooms tile the 10 x 8.5 m flat exactly
    ("living", "LIVING", 0, 0, 5, 4.5, "S", 3.2),
    ("kitchen", "KITCHEN", 5, 0, 8, 3, "S", 1.4),
    ("toilet1", "TOILET", 8, 0, 10, 3, "E", 0.6),
    ("passage", "PASSAGE", 5, 3, 10, 4.5, None, 0),
    ("master", "MASTER BED", 0, 4.5, 4, 8.5, "N", 2.2),
    ("bed2", "BED 2", 4, 4.5, 7.5, 8.5, "N", 2.0),
    ("dining", "DINING", 7.5, 4.5, 10, 6.5, None, 0),
    ("toilet2", "TOILET", 7.5, 6.5, 10, 8.5, "E", 0.6),
]
DOORS = [((5, 3.4), (5, 4.3)), ((1.0, 4.5), (1.9, 4.5)), ((5.5, 4.5), (6.4, 4.5)), ((7.7, 4.5), (8.6, 4.5)),
         ((8.2, 6.5), (8.95, 6.5)), ((8.3, 3.0), (9.05, 3.0)), ((5.5, 3.0), (6.4, 3.0))]


def _window(x0, y0, x1, y1, side, L):
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    return {"S": ((cx - L / 2, y0), (cx + L / 2, y0)), "N": ((cx - L / 2, y1), (cx + L / 2, y1)),
            "E": ((x1, cy - L / 2), (x1, cy + L / 2)), "W": ((x0, cy - L / 2), (x0, cy + L / 2))}[side]


def _draw(path: Path, rooms, *, scale=1.0, insunits=6, walls_as_lines=False, layers=None, stairs=2, stair_w=3.2,
          passage_w=1.8, entry_w=1.0) -> None:
    L = {"room": "A-AREA", "wall": "A-WALL", "win": "A-GLAZ", "door": "A-DOOR", "text": "A-ANNO-TEXT", "unit": "A-UNIT",
         "stair": "A-STAIR", "passage": "A-PASSAGE"} | (layers or {})
    doc = ezdxf.new("R2007")
    doc.header["$INSUNITS"] = insunits
    msp = doc.modelspace()
    s = scale

    def P(pts):
        return [(x * s, y * s) for x, y in pts]

    for _rid, label, x0, y0, x1, y1, side, wl in rooms:
        rect = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
        if walls_as_lines:
            for i in range(4):
                msp.add_line(P([rect[i]])[0], P([rect[(i + 1) % 4]])[0], dxfattribs={"layer": L["wall"]})
        else:
            msp.add_lwpolyline(P(rect), close=True, dxfattribs={"layer": L["room"]})
        msp.add_text(label, dxfattribs={"layer": L["text"], "height": 0.2 * s}).set_placement(
            ((x0 + 0.3) * s, ((y0 + y1) / 2) * s))
        if side:
            a, b = _window(x0, y0, x1, y1, side, wl)
            msp.add_line(P([a])[0], P([b])[0], dxfattribs={"layer": L["win"]})
    if not walls_as_lines:
        msp.add_lwpolyline(P([(0, 0), (10, 0), (10, 8.5), (0, 8.5)]), close=True, dxfattribs={"layer": L["unit"]})
    for a, b in DOORS:
        msp.add_line(P([a])[0], P([b])[0], dxfattribs={"layer": L["door"]})
    # entry door from the dining room onto the common passage
    msp.add_line(P([(10, 5.0)])[0], P([(10, 5.0 + entry_w)])[0], dxfattribs={"layer": L["door"]})
    msp.add_lwpolyline(P([(10, 0), (10 + passage_w, 0), (10 + passage_w, 8.5), (10, 8.5)]), close=True,
                       dxfattribs={"layer": L["passage"]})
    x = 10 + passage_w
    for k in range(stairs):
        msp.add_lwpolyline(P([(x, k * 7.0), (x + stair_w, k * 7.0), (x + stair_w, k * 7.0 + 6.6), (x, k * 7.0 + 6.6)]),
                           close=True, dxfattribs={"layer": L["stair"]})
    doc.saveas(str(path))


def _mod(rid, **kw):
    out = []
    for r in BASE:
        if r[0] == rid:
            r = list(r)
            for k, v in kw.items():
                r[{"x1": 4, "y1": 5, "side": 6, "wl": 7}[k]] = v
            r = tuple(r)
        out.append(r)
    return out


def reference_cases() -> list[dict]:
    root = Path(os.environ.get("PLANNER_HOME", Path.home() / ".apartment-planner")) / "reference-drawings"
    root.mkdir(parents=True, exist_ok=True)
    cases = [
        ("01-clean-metres", BASE, {}, {}, 24.0),
        ("02-narrow-bedroom", _mod("bed2", x1=6.2), {}, {"DCPR-37-1-c": "fail"}, 24.0),
        ("03-small-kitchen", _mod("kitchen", y1=1.6), {}, {"DCPR-37-2": "fail"}, 24.0),
        ("04-toilet-no-air", _mod("toilet2", side=None), {}, {"DCPR-39-2": "fail"}, 24.0),
        ("05-small-window", _mod("master", wl=0.8), {}, {"DCPR-39-1": "fail"}, 24.0),
        ("06-millimetres", BASE, {"scale": 1000.0, "insunits": 4}, {}, 24.0),
        ("07-header-lies", BASE, {"insunits": 4}, {}, 24.0),
        ("08-line-walls-odd-layers", BASE, {"walls_as_lines": True,
                                             "layers": {"wall": "WALL hatch SPA", "text": "TXT-ROOM", "win": "WINDOWS-EXT",
                                                        "door": "DR-SWING", "stair": "STAIRCASE", "passage": "CORRIDOR"}},
         {}, 24.0),
        ("09-one-stair-high-rise", BASE, {"stairs": 1}, {"NBC-4-4.4.2.1": "fail"}, 30.0),
        ("10-narrow-passage", BASE, {"passage_w": 1.2}, {"DCPR-37-12": "fail"}, 24.0),
    ]
    out = []
    for name, rooms, kw, delta, height in cases:
        p = root / f"{name}.dxf"
        _draw(p, rooms, **kw)
        expect = dict(ALL_PASS) | delta
        out.append({"name": name, "path": str(p), "expect": expect, "height": height})
    return out
