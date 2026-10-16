import type { Pt, Ring } from "./types";

export const ROOM_COLORS: Record<string, string> = {
  living: "var(--room-living)", dining: "var(--room-dining)", kitchen: "var(--room-kitchen)",
  master: "var(--room-master)", bedroom: "var(--room-bedroom)", study: "var(--room-bedroom)",
  toilet: "var(--room-toilet)", bath: "var(--room-toilet)", wc: "var(--room-toilet)", foyer: "var(--room-foyer)",
  passage: "var(--room-passage)", balcony: "var(--room-balcony)", utility: "var(--room-utility)",
};
// Konva needs concrete colours: resolve CSS variables once per theme.
export function cssColor(v: string): string {
  if (!v.startsWith("var(")) return v;
  const name = v.slice(4, -1);
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim() || "#ccc";
}
export function roomColor(type: string): string {
  return cssColor(ROOM_COLORS[type] ?? "var(--room-other)");
}
export const MAP_ROOM_COLORS: Record<string, string> = {
  living: "#f6a5c4", dining: "#fbc98e", kitchen: "#fcd95c", master: "#93d184", bedroom: "#b4dc91",
  toilet: "#8ccbf0", foyer: "#cfcfcf", passage: "#e5e5e5", balcony: "#bdf0b5", utility: "#a8e3ea",
};

export interface Bounds { minx: number; miny: number; maxx: number; maxy: number }
export function boundsOf(rings: Ring[]): Bounds {
  let minx = Infinity, miny = Infinity, maxx = -Infinity, maxy = -Infinity;
  for (const r of rings) for (const [x, y] of r) {
    if (x < minx) minx = x; if (y < miny) miny = y; if (x > maxx) maxx = x; if (y > maxy) maxy = y;
  }
  if (!isFinite(minx)) return { minx: -10, miny: -10, maxx: 10, maxy: 10 };
  return { minx, miny, maxx, maxy };
}
export interface View { s: number; tx: number; ty: number }
/** Fit plan coordinates (y up) into a w x h canvas (y down) with padding. */
export function fitView(b: Bounds, w: number, h: number, pad = 40): View {
  const bw = Math.max(1, b.maxx - b.minx), bh = Math.max(1, b.maxy - b.miny);
  const s = Math.min((w - 2 * pad) / bw, (h - 2 * pad) / bh);
  const tx = (w - bw * s) / 2 - b.minx * s;
  const ty = (h + bh * s) / 2 + b.miny * s;
  return { s, tx, ty };
}
export const toScreen = (v: View, [x, y]: Pt): Pt => [x * v.s + v.tx, -y * v.s + v.ty];
export const toPlan = (v: View, [x, y]: Pt): Pt => [(x - v.tx) / v.s, -(y - v.ty) / v.s];
export const flat = (v: View, r: Ring): number[] => r.flatMap((p) => toScreen(v, p));
export function centroid(r: Ring): Pt {
  let a = 0, cx = 0, cy = 0;
  for (let i = 0; i < r.length; i++) {
    const [x1, y1] = r[i], [x2, y2] = r[(i + 1) % r.length];
    const f = x1 * y2 - x2 * y1; a += f; cx += (x1 + x2) * f; cy += (y1 + y2) * f;
  }
  if (Math.abs(a) < 1e-9) return r[0] ?? [0, 0];
  return [cx / (3 * a), cy / (3 * a)];
}
export const fmt = (n: number | null | undefined, d = 1) => (n == null || Number.isNaN(n) ? "–" : n.toLocaleString(undefined, { minimumFractionDigits: d, maximumFractionDigits: d }));
export const pct = (n: number) => `${Math.round(n * 100)}%`;
