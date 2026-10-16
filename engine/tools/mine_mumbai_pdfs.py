"""Harvest labelled rooms (name + clear dimensions) from municipal-approval floor-plan PDFs.

Each approved plan labels rooms like "BEDROOM-2 / 3.05 x 3.05" (metres, clear inside dimensions).
Text in these PDFs is vector outlines, so pages are rendered and OCR'd (Tesseract), then every
"a x b" dimension is paired with the nearest room name above it.

Usage: python -I mine_mumbai_pdfs.py <dataset_root> <out.jsonl> [workers]
"""

from __future__ import annotations

import json
import re
import sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

DIM = re.compile(r"(\d{1,2}[.,]\d{1,3})\s*[xX×*]\s*(\d{1,2}[.,]\d{1,3})")
WIDTH = re.compile(r"(\d[.,]\d{1,2})\s*M\.?\s*W", re.I)
TYPES = [
    (r"M\.?\s*BED|MASTER", "master"), (r"BED\s*R|BEDRO|BED-|^BED\b|B\.R", "bedroom"), (r"KIT", "kitchen"),
    (r"LIV|DRAWING|HALL", "living"), (r"DIN", "dining"), (r"TOIL|BATH|W\.?C\b|T\s*&\s*B", "toilet"),
    (r"DECK|BALC|SIT.?OUT|TERR", "balcony"), (r"UTIL|WASH", "utility"), (r"DRESS|WARD", "dress"),
    (r"POOJA|PUJA|PRAY", "pooja"), (r"STUDY|OFFICE", "study"), (r"FOYER|ENTR", "foyer"), (r"STORE", "store"),
    (r"SERV", "servant"), (r"LOBBY", "lobby"), (r"FIRE\s*LIFT", "fire_lift"), (r"LIFT", "lift"),
    (r"STAIR", "stair"), (r"PASS", "passage"), (r"A\.?\s*V\.?\s*S|DUCT|SHAFT", "shaft"),
]


def classify(text: str) -> str | None:
    t = text.upper()
    for pat, ty in TYPES:
        if re.search(pat, t):
            return ty
    return None


def group_lines(words):
    """Union words that sit on one visual line close together ("3.05" "x" "3.05" -> "3.05 x 3.05")."""
    n = len(words)
    parent = list(range(n))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    order = sorted(range(n), key=lambda i: words[i][1])
    for ii, i in enumerate(order):
        wi = words[i]
        cyi = wi[2] + wi[4] / 2
        for j in order[ii + 1:]:
            wj = words[j]
            h = max(wi[4], wj[4])
            gap = wj[1] - (wi[1] + wi[3])
            if gap > 1.6 * h:
                break
            if abs((wj[2] + wj[4] / 2) - cyi) < 0.5 * h and gap > -wi[3]:
                parent[find(j)] = find(i)
    groups = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(words[i])
    lines = []
    for g in groups.values():
        g.sort(key=lambda w: w[1])
        x0 = min(w[1] for w in g)
        y0 = min(w[2] for w in g)
        x1 = max(w[1] + w[3] for w in g)
        y1 = max(w[2] + w[4] for w in g)
        lines.append((" ".join(w[0] for w in g), x0, y0, x1 - x0, y1 - y0))
    return lines


def page_rooms(args):
    pdf, pno = args
    import pymupdf
    import pytesseract
    from PIL import Image

    out = []
    try:
        doc = pymupdf.open(pdf)
        page = doc[pno]
        pix = page.get_pixmap(dpi=150, colorspace=pymupdf.csGRAY)
        img = Image.frombytes("L", (pix.width, pix.height), pix.samples)
        data = pytesseract.image_to_data(img, config="--psm 11", output_type=pytesseract.Output.DICT)
    except Exception as e:  # noqa: BLE001
        return [{"pdf": Path(pdf).parent.name, "page": pno, "error": str(e)}]
    words = []
    for i, w in enumerate(data["text"]):
        w = w.strip()
        if not w or float(data["conf"][i]) < 30:
            continue
        words.append((w, data["left"][i], data["top"][i], data["width"][i], data["height"][i]))
    lines = group_lines(words)
    names = [(ln, classify(ln[0])) for ln in lines]
    names = [(ln, t) for ln, t in names if t]
    title = " ".join(ln[0] for ln in lines if "PLAN" in ln[0].upper())[:120]
    for ln in lines:
        m = DIM.search(ln[0].replace(",", "."))
        if not m:
            continue
        a, b = float(m.group(1).replace(",", ".")), float(m.group(2).replace(",", "."))
        if not (0.5 <= a <= 15 and 0.5 <= b <= 15):
            continue
        # room name: same line before the dimension, else nearest label just above
        own = classify(ln[0][: m.start()]) if m.start() > 0 else None
        name, ty = (ln[0][: m.start()].strip(), own) if own else (None, None)
        if not ty:
            cx = ln[1] + ln[3] / 2
            best = None
            for nl, t in names:
                dy = ln[2] - (nl[2] + nl[4])
                dx = abs((nl[1] + nl[3] / 2) - cx)
                if -ln[4] <= dy <= ln[4] * 2.5 and dx < max(ln[3], nl[3]) * 1.2:
                    score = dy + dx * 0.3
                    if best is None or score < best[0]:
                        best = (score, nl[0], t)
            if best:
                name, ty = best[1], best[2]
        out.append({"pdf": Path(pdf).parent.name, "page": pno, "name": name, "type": ty or "unknown",
                    "a": a, "b": b, "short": min(a, b), "long": max(a, b), "area": round(a * b, 3), "title": title})
    for ln in lines:
        m = WIDTH.search(ln[0].replace(",", "."))
        if m and re.search(r"PASS|LOBBY|CORR|CHAJJA", ln[0], re.I):
            kind = "lobby" if re.search(r"LOBBY", ln[0], re.I) else "chajja" if re.search(r"CHAJJA", ln[0], re.I) \
                else "passage"
            out.append({"pdf": Path(pdf).parent.name, "page": pno, "name": ln[0], "type": f"{kind}_width",
                        "width": float(m.group(1).replace(",", "."))})
    return out


def main() -> None:
    import pymupdf

    root, outp = Path(sys.argv[1]), Path(sys.argv[2])
    workers = int(sys.argv[3]) if len(sys.argv) > 3 else 4
    jobs = []
    for pdf in sorted(root.glob("*/*.pdf")):
        n = pymupdf.open(str(pdf)).page_count
        jobs += [(str(pdf), i) for i in range(n)]
    done: set[tuple[str, int]] = set()
    if outp.exists():  # resume: a page is done once its marker line is written
        for line in outp.read_text().splitlines():
            r = json.loads(line)
            if r.get("type") == "_page_done":
                done.add((r["pdf"], r["page"]))
    jobs = [j for j in jobs if (Path(j[0]).parent.name, j[1]) not in done]
    print(len(jobs), "pages to do,", len(done), "done", file=sys.stderr)
    with ProcessPoolExecutor(workers) as ex, open(outp, "a") as fh:
        for k, (job, rows) in enumerate(zip(jobs, ex.map(page_rooms, jobs, chunksize=2))):
            for r in rows:
                fh.write(json.dumps(r) + "\n")
            fh.write(json.dumps({"pdf": Path(job[0]).parent.name, "page": job[1], "type": "_page_done"}) + "\n")
            fh.flush()
            if k % 25 == 0:
                print(k, file=sys.stderr, flush=True)

if __name__ == "__main__":
    main()
