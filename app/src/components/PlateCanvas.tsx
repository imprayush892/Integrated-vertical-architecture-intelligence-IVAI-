import type Konva from "konva";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Circle, Group, Layer, Line, Rect, Shape, Stage, Text } from "react-konva";
import { boundsOf, centroid, fitView, flat, fmt, roomColor, toPlan, toScreen, type Bounds, type View } from "../lib/geom";
import type { Plan, Plate, PolyRings, Pt, Ring, Unit } from "../lib/types";
import { EmptyState, Icon, IconButton } from "../ui";

export type CanvasMode = "shape" | "units" | "review";

interface Handle { key: string; label: string; at: Pt; axis: "x" | "y" | "xy"; apply: (p: Pt) => Record<string, number> }

interface Props {
  plate: Plate;
  mode: CanvasMode;
  units?: Unit[];
  plans?: Plan[];
  selectedUnit?: string | null;
  onSelectUnit?: (id: string) => void;
  onDrag?: (params: Record<string, number>) => void;
  onCommit?: (params: Record<string, number>) => void;
  focus?: [number, number, number, number] | null;
  showLabels?: boolean;
}

// ---------------------------------------------------------------------------------------------
// Zone colours. One recipe, two outputs: CSS (for legends and panels) and a resolved colour for
// Konva. Both read the design tokens, so light, dark and the theme switch all follow.
// ---------------------------------------------------------------------------------------------
export type ZoneKind = "flats" | "core" | "passage" | "shaft";
const ZONE_RECIPE: Record<ZoneKind, { token: string; pct: number }> = {
  flats: { token: "--det", pct: 22 },
  core: { token: "--wall", pct: 80 },
  passage: { token: "--room-passage", pct: 100 },
  shaft: { token: "--cobalt", pct: 42 },
};
/** CSS colour for a zone (use for legend chips and bars; resolves with the active theme). */
export const zoneCss = (k: ZoneKind): string => `color-mix(in srgb, var(${ZONE_RECIPE[k].token}) ${ZONE_RECIPE[k].pct}%, var(--plate-fill))`;

type RGBA = [number, number, number, number];
function parseColor(s: string, fallback: RGBA = [128, 128, 128, 1]): RGBA {
  const v = s.trim();
  if (v.startsWith("#")) {
    let h = v.slice(1);
    if (h.length === 3 || h.length === 4) h = h.split("").map((c) => c + c).join("");
    if (h.length === 6 || h.length === 8) {
      const n = (i: number) => parseInt(h.slice(i, i + 2), 16);
      return [n(0), n(2), n(4), h.length === 8 ? n(6) / 255 : 1];
    }
    return fallback;
  }
  const m = v.match(/rgba?\(([^)]+)\)/);
  if (m) {
    const p = m[1].split(/[\s,/]+/).filter(Boolean).map(Number);
    if (p.length >= 3 && p.every((x) => !Number.isNaN(x))) return [p[0], p[1], p[2], p[3] ?? 1];
  }
  return fallback;
}
const rgba = ([r, g, b, a]: RGBA) => `rgba(${Math.round(r)},${Math.round(g)},${Math.round(b)},${+a.toFixed(3)})`;
/** Opaque blend: `pct`% of `a` over `b`. */
const mix = (a: string, b: string, pct: number): string => {
  const A = parseColor(a), B = parseColor(b), t = pct / 100;
  return rgba([A[0] * t + B[0] * (1 - t), A[1] * t + B[1] * (1 - t), A[2] * t + B[2] * (1 - t), 1]);
};
const withAlpha = (c: string, a: number): string => { const p = parseColor(c); return rgba([p[0], p[1], p[2], p[3] * a]); };

/** Every colour the drawing uses, resolved once per render from the CSS tokens. */
function readTheme() {
  const root = getComputedStyle(document.documentElement);
  const t = (n: string, fb: string) => root.getPropertyValue(n).trim() || fb;
  const plateFill = t("--plate-fill", "#F4F6FA");
  const wall = t("--wall", "#1e232b");
  const out = {
    ink1: t("--ink-1", "#1E2229"), ink2: t("--ink-2", "#4A505C"), ink3: t("--ink-3", "#5C6370"),
    bg: t("--neo-bg", "#E6E9EF"), plate: plateFill, wall, hair: t("--hairline", "#CDD2DC"),
    det: t("--det", "#0D8A7B"), cobalt: t("--cobalt", "#0066CC"), cobaltInk: t("--cobalt-ink", "#0058B0"), fail: t("--fail", "#D33A3A"), failInk: t("--fail-ink", "#A82424"),
    gradLight: t("--neo-grad-light", "#F0F3F9"), gradDark: t("--neo-grad-dark", "#DCE0E8"), shDark: t("--sh-dark", "#C2C7D2"),
    cobaltGlow: t("--cobalt-glow", "rgba(0,102,204,0.28)"),
    foyer: t("--room-foyer", "#e2e2e2"),
    fMono: t("--f-mono", "monospace"), fSans: t("--f-sans", "sans-serif"),
    zone: {} as Record<ZoneKind, string>,
    tints: [] as string[],
  };
  for (const k of Object.keys(ZONE_RECIPE) as ZoneKind[]) out.zone[k] = mix(t(ZONE_RECIPE[k].token, "#888"), plateFill, ZONE_RECIPE[k].pct);
  out.tints = ["--room-living", "--room-dining", "--room-kitchen", "--room-master", "--room-toilet", "--room-balcony", "--room-utility", "--room-other"]
    .map((n) => mix(t(n, "#ddd"), plateFill, 88));
  return out;
}
type Theme = ReturnType<typeof readTheme>;

const snap = (v: number) => Math.round(v * 10) / 10;

const HANDLE_LABEL: Record<string, string> = {
  W: "Plate width", D: "Plate depth", core: "Core size", arm_depth: "Arm depth", wing_width: "Wing width",
  bar_width: "Bar width", bridge_depth: "Bridge depth", bar_depth: "Bar depth", stem_width: "Stem width",
  core_w: "Core width", core_d: "Core depth",
};

function handlesFor(p: Plate): Handle[] {
  const pr = p.params;
  const W = pr.W, D = pr.D;
  const core = p.zones.find((z) => z.kind === "core")?.polygon?.[0]?.[0] ?? [];
  const cb = boundsOf([core as Ring]);
  const cx = (cb.minx + cb.maxx) / 2, cy = (cb.miny + cb.maxy) / 2;
  const hs: Handle[] = [
    { key: "W", label: HANDLE_LABEL.W, axis: "x", at: [W / 2, 0], apply: ([x]) => ({ W: snap(2 * Math.abs(x)) }) },
    { key: "D", label: HANDLE_LABEL.D, axis: "y", at: [0, D / 2], apply: ([, y]) => ({ D: snap(2 * Math.abs(y)) }) },
    { key: "core", label: HANDLE_LABEL.core, axis: "xy", at: [cb.maxx, cb.maxy], apply: ([x, y]) => ({ core_w: snap(2 * Math.abs(x - cx)), core_d: snap(2 * Math.abs(y - cy)) }) },
  ];
  if (p.shape === "cruciform") {
    hs.push({ key: "arm_depth", label: HANDLE_LABEL.arm_depth, axis: "y", at: [W / 2, pr.arm_depth / 2], apply: ([, y]) => ({ arm_depth: snap(2 * Math.abs(y)) }) });
    hs.push({ key: "wing_width", label: HANDLE_LABEL.wing_width, axis: "x", at: [pr.wing_width / 2, D / 2], apply: ([x]) => ({ wing_width: snap(2 * Math.abs(x)) }) });
  } else if (p.shape === "h_shape") {
    hs.push({ key: "bar_width", label: HANDLE_LABEL.bar_width, axis: "x", at: [-W / 2 + pr.bar_width, D / 2], apply: ([x]) => ({ bar_width: snap(x + W / 2) }) });
    hs.push({ key: "bridge_depth", label: HANDLE_LABEL.bridge_depth, axis: "y", at: [0, pr.bridge_depth / 2], apply: ([, y]) => ({ bridge_depth: snap(2 * Math.abs(y)) }) });
  } else if (p.shape === "t_shape") {
    hs.push({ key: "bar_depth", label: HANDLE_LABEL.bar_depth, axis: "y", at: [W / 2, D / 2 - pr.bar_depth], apply: ([, y]) => ({ bar_depth: snap(D / 2 - y) }) });
    hs.push({ key: "stem_width", label: HANDLE_LABEL.stem_width, axis: "x", at: [pr.stem_width / 2, -D / 2], apply: ([x]) => ({ stem_width: snap(2 * Math.abs(x)) }) });
  }
  return hs;
}

// ------------------------------------------------------------------------------ drawing helpers
function tracePath(c: CanvasRenderingContext2D, parts: PolyRings[], view: View) {
  c.beginPath();
  for (const part of parts) for (const ring of part) {
    ring.forEach((p, i) => { const [x, y] = toScreen(view, p); if (i === 0) c.moveTo(x, y); else c.lineTo(x, y); });
    c.closePath();
  }
}

/** Filled rings (even-odd, so holes work), optional outline and drafting hatch. */
function Rings({ parts, view, fill, stroke, strokeWidth = 1, opacity = 1, hatch }: {
  parts: PolyRings[]; view: View; fill: string; stroke?: string; strokeWidth?: number; opacity?: number;
  hatch?: { color: string; gap: number; cross?: boolean; width?: number };
}) {
  return (
    <Shape
      listening={false}
      opacity={opacity}
      sceneFunc={(ctx) => {
        const c = (ctx as unknown as { _context: CanvasRenderingContext2D })._context;
        tracePath(c, parts, view);
        c.fillStyle = fill;
        c.fill("evenodd");
        if (hatch) {
          let minx = Infinity, miny = Infinity, maxx = -Infinity, maxy = -Infinity;
          for (const part of parts) for (const ring of part) for (const p of ring) {
            const [x, y] = toScreen(view, p);
            if (x < minx) minx = x; if (x > maxx) maxx = x; if (y < miny) miny = y; if (y > maxy) maxy = y;
          }
          if (isFinite(minx)) {
            c.save();
            c.clip("evenodd");
            c.strokeStyle = hatch.color;
            c.lineWidth = hatch.width ?? 0.8;
            const h = maxy - miny;
            c.beginPath();
            for (let d = minx - h; d <= maxx; d += hatch.gap) { c.moveTo(d, maxy); c.lineTo(d + h, miny); }
            if (hatch.cross) for (let d = minx; d <= maxx + h; d += hatch.gap) { c.moveTo(d, maxy); c.lineTo(d - h, miny); }
            c.stroke();
            c.restore();
          }
        }
        if (stroke) { tracePath(c, parts, view); c.strokeStyle = stroke; c.lineWidth = strokeWidth; c.lineJoin = "miter"; c.stroke(); }
      }}
    />
  );
}

/** Millimetre-paper grid: 1 m minor / 5 m major lines in plan space (coarser when zoomed out). */
function DraftGrid({ view, w, h, th }: { view: View; w: number; h: number; th: Theme }) {
  return (
    <Shape listening={false} sceneFunc={(ctx) => {
      const c = (ctx as unknown as { _context: CanvasRenderingContext2D })._context;
      const minor = view.s >= 11 ? 1 : view.s >= 4 ? 5 : 10;
      const major = minor * 5;
      const [x0, y1] = toPlan(view, [0, 0]);
      const [x1, y0] = toPlan(view, [w, h]);
      const lines = (step: number, col: string, lw: number, skip?: number) => {
        c.beginPath();
        for (let x = Math.ceil(x0 / step) * step; x <= x1; x += step) {
          if (skip && Math.abs(x / skip - Math.round(x / skip)) < 1e-6) continue;
          const sx = Math.round(toScreen(view, [x, 0])[0]) + 0.5; c.moveTo(sx, 0); c.lineTo(sx, h);
        }
        for (let y = Math.ceil(y0 / step) * step; y <= y1; y += step) {
          if (skip && Math.abs(y / skip - Math.round(y / skip)) < 1e-6) continue;
          const sy = Math.round(toScreen(view, [0, y])[1]) + 0.5; c.moveTo(0, sy); c.lineTo(w, sy);
        }
        c.strokeStyle = col; c.lineWidth = lw; c.stroke();
      };
      if ((x1 - x0) / minor > 400 || (y1 - y0) / minor > 400) return;
      lines(minor, withAlpha(th.hair, 0.4), 1, major);
      lines(major, withAlpha(th.hair, 0.95), 1);
    }} />
  );
}

/** One dimension line with extension lines, oblique ticks and a plated label (architectural style). */
function Dimension({ a, b, offset, view, th, label }: { a: Pt; b: Pt; offset: number; view: View; th: Theme; label: string }) {
  const [ax, ay] = toScreen(view, a), [bx, by] = toScreen(view, b);
  const L = Math.hypot(bx - ax, by - ay);
  if (L < 24) return null;
  const ux = (bx - ax) / L, uy = (by - ay) / L;
  // normal pointing away from the plate; offset is in screen px
  const nx = uy * Math.sign(offset), ny = -ux * Math.sign(offset);
  const o = Math.abs(offset);
  const p1: Pt = [ax + nx * o, ay + ny * o], p2: Pt = [bx + nx * o, by + ny * o];
  const ext = 4;
  const tick = 4;
  let ang = (Math.atan2(uy, ux) * 180) / Math.PI;
  if (ang > 90 || ang < -90) ang += 180;
  const mx = (p1[0] + p2[0]) / 2, my = (p1[1] + p2[1]) / 2;
  const tw = label.length * 6.4 + 12;
  return (
    <Group listening={false}>
      <Line points={[ax + nx * 3, ay + ny * 3, p1[0] + nx * ext, p1[1] + ny * ext]} stroke={th.ink3} strokeWidth={0.8} />
      <Line points={[bx + nx * 3, by + ny * 3, p2[0] + nx * ext, p2[1] + ny * ext]} stroke={th.ink3} strokeWidth={0.8} />
      <Line points={[p1[0], p1[1], p2[0], p2[1]]} stroke={th.ink2} strokeWidth={1} />
      {[p1, p2].map((p, i) => (
        <Line key={i} points={[p[0] - (ux + nx) * tick, p[1] - (uy + ny) * tick, p[0] + (ux + nx) * tick, p[1] + (uy + ny) * tick]} stroke={th.ink1} strokeWidth={1.5} />
      ))}
      <Group x={mx} y={my} rotation={ang}>
        <Rect x={-tw / 2} y={-8} width={tw} height={16} cornerRadius={4} fill={withAlpha(th.bg, 0.92)} />
        <Text x={-tw / 2} y={-6} width={tw} align="center" text={label} fontSize={10.5} fontFamily={th.fMono} fontStyle="bold" fill={th.ink1} />
      </Group>
    </Group>
  );
}

function NorthArrow({ x, y, angle, th }: { x: number; y: number; angle: number; th: Theme }) {
  return (
    <Group x={x} y={y} listening={false}>
      <Circle radius={22} fill={withAlpha(th.bg, 0.92)} stroke={th.ink3} strokeWidth={1} />
      <Group rotation={angle}>
        <Line points={[0, -16, 6, 9, 0, 4, -6, 9]} closed fill="transparent" stroke={th.ink1} strokeWidth={1.2} lineJoin="round" />
        <Line points={[0, -16, 0, 4, -6, 9]} closed fill={th.ink1} stroke={th.ink1} strokeWidth={1} lineJoin="round" />
        <Text x={-8} y={-37} width={16} align="center" text="N" fontSize={11} fontFamily={th.fMono} fontStyle="bold" fill={th.ink1} />
      </Group>
    </Group>
  );
}

function ScaleBar({ x, y, s, th }: { x: number; y: number; s: number; th: Theme }) {
  const nice = [1, 2, 5, 10, 20, 50, 100].find((n) => n * s >= 70) ?? 100;
  const len = nice * s;
  const seg = len / 4;
  return (
    <Group x={x - len} y={y} listening={false}>
      <Rect x={-12} y={-18} width={len + 54} height={44} cornerRadius={8} fill={withAlpha(th.bg, 0.92)} />
      {[0, 1, 2, 3].map((i) => (
        <Rect key={i} x={i * seg} y={0} width={seg} height={5} fill={i % 2 === 0 ? th.ink1 : th.plate} stroke={th.ink1} strokeWidth={0.8} />
      ))}
      {[0, 2, 4].map((i) => (
        <Text key={i} x={i * seg - 20} y={8} width={40} align="center" text={`${(nice * i) / 4}`} fontSize={10} fontFamily={th.fMono} fill={th.ink2} />
      ))}
      <Text x={len + 6} y={-4} width={32} text="m" fontSize={11} fontFamily={th.fMono} fontStyle="bold" fill={th.ink1} />
    </Group>
  );
}

/** Stair treads, lift crosses and a lobby tint inside the core poché. */
function CoreParts({ parts, view, th }: { parts: Plate["core_parts"]; view: View; th: Theme }) {
  return (
    <Shape listening={false} sceneFunc={(ctx) => {
      const c = (ctx as unknown as { _context: CanvasRenderingContext2D })._context;
      const line = th.plate;
      for (const part of parts) {
        const pts = part.polygon.map((p) => toScreen(view, p));
        if (pts.length < 3) continue;
        const xs = pts.map((p) => p[0]), ys = pts.map((p) => p[1]);
        const x0 = Math.min(...xs), x1 = Math.max(...xs), y0 = Math.min(...ys), y1 = Math.max(...ys);
        const w = x1 - x0, h = y1 - y0;
        if (part.kind === "lobby") {
          c.fillStyle = th.foyer; c.fillRect(x0, y0, w, h);
          c.strokeStyle = line; c.lineWidth = 1; c.strokeRect(x0 + 0.5, y0 + 0.5, w - 1, h - 1);
          continue;
        }
        c.strokeStyle = line; c.lineWidth = 1;
        c.strokeRect(x0 + 0.5, y0 + 0.5, w - 1, h - 1);
        if (part.kind === "stair") {
          const vertical = h >= w;
          const run = vertical ? h : w;
          const n = Math.max(2, Math.round(run / (view.s * 0.28)));
          const every = Math.max(1, Math.ceil(3 / (run / n)));
          c.beginPath();
          for (let i = 1; i < n; i += every) {
            const t = (i / n) * run;
            if (vertical) { c.moveTo(x0, y0 + t); c.lineTo(x1, y0 + t); } else { c.moveTo(x0 + t, y0); c.lineTo(x0 + t, y1); }
          }
          c.globalAlpha = 0.7; c.stroke(); c.globalAlpha = 1;
          // up arrow along the flight
          c.beginPath();
          const cxm = (x0 + x1) / 2, cym = (y0 + y1) / 2;
          if (vertical) { c.moveTo(cxm, y1 - 4); c.lineTo(cxm, y0 + 6); c.moveTo(cxm - 3, y0 + 10); c.lineTo(cxm, y0 + 5); c.lineTo(cxm + 3, y0 + 10); }
          else { c.moveTo(x0 + 4, cym); c.lineTo(x1 - 6, cym); c.moveTo(x1 - 10, cym - 3); c.lineTo(x1 - 5, cym); c.lineTo(x1 - 10, cym + 3); }
          c.lineWidth = 1.4; c.stroke();
        } else {
          // lift / fire lift / service shaft: crossed box
          c.beginPath();
          c.moveTo(x0, y0); c.lineTo(x1, y1); c.moveTo(x1, y0); c.lineTo(x0, y1);
          c.lineWidth = part.kind === "fire_lift" ? 1.8 : 1;
          if (part.kind === "service") c.setLineDash([3, 3]);
          c.stroke(); c.setLineDash([]);
        }
      }
    }} />
  );
}

/** Convex "dome" puck with an LED core, matching the kit's draggable hardware. */
function HandlePuck({ x, y, active, hot, th, label, axis, onEnter, onLeave, onMove, onStart, onEnd }: {
  x: number; y: number; active: boolean; hot: boolean; th: Theme; label: string; axis: Handle["axis"];
  onEnter: () => void; onLeave: () => void; onMove: (x: number, y: number) => void; onStart: () => void; onEnd: () => void;
}) {
  const r = active ? 11 : 10;
  return (
    <Group x={x} y={y} draggable
      dragBoundFunc={(p) => ({ x: axis === "y" ? x : p.x, y: axis === "x" ? y : p.y })}
      onMouseEnter={(e) => { e.target.getStage()!.container().style.cursor = axis === "x" ? "ew-resize" : axis === "y" ? "ns-resize" : "move"; onEnter(); }}
      onMouseLeave={(e) => { e.target.getStage()!.container().style.cursor = "default"; onLeave(); }}
      onDragStart={onStart}
      onDragMove={(e) => onMove(e.target.x(), e.target.y())}
      onDragEnd={onEnd}
      name={`handle ${label}`}>
      <Circle radius={r + 6} fill="transparent" />
      {(hot || active) && <Circle radius={r + 5} stroke={th.cobalt} strokeWidth={2} opacity={0.55} shadowColor={th.cobalt} shadowBlur={10} shadowOpacity={0.8} />}
      <Circle radius={r} fillLinearGradientStartPoint={{ x: -r, y: -r }} fillLinearGradientEndPoint={{ x: r, y: r }}
        fillLinearGradientColorStops={[0, th.gradLight, 1, th.gradDark]}
        stroke={withAlpha(th.ink3, 0.55)} strokeWidth={1}
        shadowColor={th.shDark} shadowBlur={active ? 4 : 9} shadowOffset={{ x: 2, y: active ? 1 : 3 }} shadowOpacity={1} />
      <Circle radius={r - 4.5} fill={th.bg} stroke={withAlpha(th.ink3, 0.35)} strokeWidth={1} />
      <Circle radius={3} fill={th.cobalt} shadowColor={th.cobalt} shadowBlur={active || hot ? 9 : 5} shadowOpacity={0.9} />
    </Group>
  );
}

// -------------------------------------------------------------------------------------- component
export default function PlateCanvas(props: Props) {
  const { plate, mode, units = [], plans = [], selectedUnit, onSelectUnit, onDrag, onCommit, focus, showLabels = true } = props;
  const wrap = useRef<HTMLDivElement>(null);
  const [size, setSize] = useState({ w: 800, h: 600 });
  const [zoom, setZoom] = useState<{ k: number; ox: number; oy: number }>({ k: 1, ox: 0, oy: 0 });
  const [hover, setHover] = useState<{ x: number; y: number; html: string[] } | null>(null);
  const [hoverUnit, setHoverUnit] = useState<string | null>(null);
  const [hoverRoom, setHoverRoom] = useState<string | null>(null);
  const [hotHandle, setHotHandle] = useState<string | null>(null);
  const [drag, setDrag] = useState<{ key: string; x: number; y: number; text: string } | null>(null);
  const [announce, setAnnounce] = useState("");
  const panning = useRef<{ x: number; y: number; ox: number; oy: number } | null>(null);
  const dragParams = useRef<Record<string, number>>({});

  useEffect(() => {
    const el = wrap.current;
    if (!el) return;
    const ro = new ResizeObserver(() => setSize({ w: el.clientWidth, h: el.clientHeight }));
    ro.observe(el);
    setSize({ w: el.clientWidth, h: el.clientHeight });
    return () => ro.disconnect();
  }, []);

  const hasOutline = plate.outline.length >= 3;
  // While a handle is dragged the preview plate changes size every frame. Refitting the view to it would
  // move the handle out from under the pointer, so the fit is frozen until shortly after the drop.
  const [frozen, setFrozen] = useState<Bounds | null>(null);
  const thaw = useRef<ReturnType<typeof setTimeout> | null>(null);
  useEffect(() => () => { if (thaw.current) clearTimeout(thaw.current); }, []);
  const liveBounds: Bounds = useMemo(() => boundsOf([plate.outline]), [plate.outline]);
  const baseBounds: Bounds = frozen ?? liveBounds;
  const view: View = useMemo(() => {
    const b = focus ? { minx: focus[0] - 1.5, miny: focus[1] - 1.5, maxx: focus[2] + 1.5, maxy: focus[3] + 1.5 } : baseBounds;
    const v = fitView(b, size.w, size.h, focus ? 48 : 68);
    return { s: v.s * zoom.k, tx: v.tx * zoom.k + zoom.ox, ty: v.ty * zoom.k + zoom.oy };
  }, [baseBounds, size, zoom, focus]);

  useEffect(() => { setZoom({ k: 1, ox: 0, oy: 0 }); }, [focus]);

  const zoomBy = useCallback((f: number, at?: { x: number; y: number }) => {
    const px = at?.x ?? size.w / 2, py = at?.y ?? size.h / 2;
    setZoom((z) => {
      const k = Math.min(12, Math.max(0.4, z.k * f)), r = k / z.k;
      return { k, ox: px - (px - z.ox) * r, oy: py - (py - z.oy) * r };
    });
  }, [size]);
  const onWheel = (e: Konva.KonvaEventObject<WheelEvent>) => {
    e.evt.preventDefault();
    const p = e.target.getStage()?.getPointerPosition();
    if (p) zoomBy(e.evt.deltaY < 0 ? 1.12 : 1 / 1.12, p);
  };
  const zoomed = zoom.k !== 1 || zoom.ox !== 0 || zoom.oy !== 0;

  const themeKey = `${document.documentElement.getAttribute("data-theme") ?? ""}|${window.matchMedia?.("(prefers-color-scheme: dark)").matches ? "d" : "l"}`;
  // eslint-disable-next-line react-hooks/exhaustive-deps
  const th = useMemo(() => readTheme(), [themeKey]);
  const z = (k: string) => plate.zones.filter((x) => x.kind === k).flatMap((x) => x.polygon);
  const fontPx = Math.max(9, Math.min(13, view.s * 0.35));
  const wallPx = Math.max(2, Math.min(7, view.s * 0.3));
  const handles = mode === "shape" ? handlesFor(plate) : [];
  const orderedUnits = useMemo(() => [...units].sort((a, b) => Number(a.id === selectedUnit) - Number(b.id === selectedUnit)), [units, selectedUnit]);
  const unitIndex = useMemo(() => new Map(units.map((u, i) => [u.id, i])), [units]);
  const showDims = !focus && hasOutline;
  const b = liveBounds;

  const startPan = (e: Konva.KonvaEventObject<MouseEvent>) => {
    if (e.target.name() !== "pan-bg") return;
    const p = e.target.getStage()!.getPointerPosition();
    if (!p) return;
    panning.current = { x: p.x, y: p.y, ox: zoom.ox, oy: zoom.oy };
    e.target.getStage()!.container().style.cursor = "grabbing";
  };
  const movePan = (e: Konva.KonvaEventObject<MouseEvent>) => {
    const st = panning.current;
    const stage = e.target.getStage();
    const p = stage?.getPointerPosition();
    if (!st || !p) return;
    setZoom((zz) => ({ ...zz, ox: st.ox + p.x - st.x, oy: st.oy + p.y - st.y }));
  };
  const endPan = (e: Konva.KonvaEventObject<MouseEvent>) => {
    if (panning.current) { panning.current = null; e.target.getStage()!.container().style.cursor = "default"; }
  };

  const describe = `Floor plan of ${plate.tower_id}, ${fmt(plate.width, 1)} by ${fmt(plate.depth, 1)} metres, ${fmt(plate.area, 1)} square metres${units.length ? `, ${units.length} flats` : ""}.`;

  if (!hasOutline) {
    return (
      <div ref={wrap} className="canvas-wrap">
        <EmptyState fill icon={<Icon name="layers" size={26} />} title="No outline to draw">
          This plate has no outline yet. Pick a shape in the left panel.
        </EmptyState>
      </div>
    );
  }

  return (
    <>
    <div ref={wrap} className="canvas-wrap" role="img" aria-label={describe}>
      <Stage width={size.w} height={size.h} onWheel={onWheel} draggable={false}
        onMouseDown={startPan} onMouseMove={movePan} onMouseUp={endPan}
        onMouseLeave={(e) => { setHover(null); endPan(e); }}>
        <Layer>
          <Rect name="pan-bg" x={0} y={0} width={size.w} height={size.h} fill="transparent"
            onMouseEnter={(e) => { if (!panning.current) e.target.getStage()!.container().style.cursor = "grab"; }}
            onMouseLeave={(e) => { if (!panning.current) e.target.getStage()!.container().style.cursor = "default"; }} />
          <DraftGrid view={view} w={size.w} h={size.h} th={th} />

          {/* plate sheet: fill, zones, then the wall poché on top */}
          <Line points={flat(view, plate.outline)} closed fill={th.plate} listening={false}
            shadowColor={th.shDark} shadowBlur={14} shadowOffset={{ x: 3, y: 4 }} shadowOpacity={0.7} />
          {mode !== "review" && <Rings parts={z("flats")} view={view} fill={th.zone.flats} />}
          <Rings parts={z("passage")} view={view} fill={th.zone.passage} stroke={withAlpha(th.ink3, 0.6)} strokeWidth={0.6} />
          <Rings parts={z("core")} view={view} fill={th.zone.core} />
          <CoreParts parts={plate.core_parts} view={view} th={th} />
          <Rings parts={z("shaft")} view={view} fill={th.zone.shaft} stroke={th.cobalt} strokeWidth={1}
            hatch={{ color: withAlpha(th.cobalt, 0.7), gap: 5, cross: true, width: 0.7 }} />

          {/* flats */}
          {(mode === "units" || mode === "shape") && orderedUnits.map((u) => {
            const i = unitIndex.get(u.id) ?? 0;
            const sel = mode === "units" && selectedUnit === u.id;
            const hot = hoverUnit === u.id;
            const flagged = u.flags.length > 0;
            const [sx, sy] = toScreen(view, centroid(u.polygon));
            return (
              <Line key={u.id} points={flat(view, u.polygon)} closed lineJoin="miter"
                fill={th.tints[i % th.tints.length]} opacity={mode === "shape" ? 0.55 : hot && !sel ? 0.8 : 0.96}
                stroke={sel ? th.cobalt : flagged ? th.fail : th.wall} strokeWidth={sel ? 3.5 : flagged ? 2.4 : Math.max(1.4, wallPx * 0.55)}
                dash={flagged && !sel ? [7, 4] : undefined}
                shadowEnabled={sel} shadowColor={th.cobalt} shadowBlur={12} shadowOpacity={0.6}
                onClick={() => onSelectUnit?.(u.id)} onTap={() => onSelectUnit?.(u.id)}
                onMouseEnter={(e) => {
                  e.target.getStage()!.container().style.cursor = "pointer";
                  setHoverUnit(u.id);
                  setHover({ x: sx, y: sy, html: [u.id, `${fmt(u.area)} m²`, u.programme ? `${u.programme.bhk} BHK · ${u.programme.name}` : "", ...u.flags.map((f) => `Flag: ${f.replace(/_/g, " ")}`)] });
                }}
                onMouseLeave={(e) => { e.target.getStage()!.container().style.cursor = "default"; setHoverUnit(null); setHover(null); }}
              />
            );
          })}
          {mode === "units" && units.map((u) => {
            const [sx, sy] = toScreen(view, centroid(u.polygon));
            const bhk = u.programme?.bhk;
            const sub = `${fmt(u.area)} m²${bhk ? ` · ${bhk}BHK` : ""}`;
            const bw = Math.max(u.id.length * (fontPx + 1) * 0.66, sub.length * (fontPx - 1) * 0.62) + 14;
            return (
              <Group key={`t${u.id}`} x={sx - 55} y={sy - fontPx * 1.5} listening={false}>
                <Rect x={55 - bw / 2} y={-4} width={bw} height={fontPx * 2 + 10} cornerRadius={6} fill={withAlpha(th.plate, 0.8)} />
                <Text width={110} align="center" text={u.id} fontSize={fontPx + 1} fontFamily={th.fSans} fontStyle="bold" fill={th.ink1} />
                <Text y={fontPx + 3} width={110} align="center" text={sub} fontSize={fontPx - 1} fontFamily={th.fMono} fill={th.ink2} />
              </Group>
            );
          })}
          {mode === "units" && units.map((u) => u.core_link && (
            <Group key={`d${u.id}`} x={toScreen(view, u.core_link)[0]} y={toScreen(view, u.core_link)[1]} listening={false}>
              <Circle radius={6.5} fill={th.plate} stroke={th.fail} strokeWidth={1.5} />
              <Circle radius={3} fill={th.fail} />
            </Group>
          ))}

          {/* outer wall poché */}
          <Line points={flat(view, plate.outline)} closed stroke={th.wall} strokeWidth={wallPx} lineJoin="miter" listening={false} />

          {/* drawn plans */}
          {mode === "review" && plans.map((pl) => (
            <PlanShapes key={pl.unit_id} plan={pl} view={view} th={th} fontPx={fontPx} showLabels={showLabels} hoverRoom={hoverRoom}
              onHover={(x, y, html, id) => { setHover(html ? { x, y, html } : null); setHoverRoom(html ? id ?? null : null); }} />
          ))}
          {mode === "review" && focus && (
            <Rect x={toScreen(view, [focus[0], focus[3]])[0]} y={toScreen(view, [focus[0], focus[3]])[1]}
              width={(focus[2] - focus[0]) * view.s} height={(focus[3] - focus[1]) * view.s}
              stroke={th.cobalt} strokeWidth={2.5} dash={[6, 4]} listening={false} shadowColor={th.cobalt} shadowBlur={8} shadowOpacity={0.5} />
          )}

          {/* dimensions */}
          {showDims && (
            <>
              <Dimension a={[b.minx, b.miny]} b={[b.maxx, b.miny]} offset={-30} view={view} th={th} label={`${fmt(b.maxx - b.minx, 2)} m`} />
              <Dimension a={[b.minx, b.miny]} b={[b.minx, b.maxy]} offset={30} view={view} th={th} label={`${fmt(b.maxy - b.miny, 2)} m`} />
            </>
          )}

          {/* parametric handles */}
          {handles.map((h) => {
            const [hx, hy] = toScreen(view, h.at);
            const active = drag?.key === h.key;
            return (
              <HandlePuck key={h.key} x={hx} y={hy} active={active} hot={hotHandle === h.key} th={th} label={h.label} axis={h.axis}
                onEnter={() => { setHotHandle(h.key); setHover({ x: hx, y: hy, html: [h.label, h.axis === "xy" ? "Drag to resize" : "Drag along the axis"] }); }}
                onLeave={() => { setHotHandle(null); setHover(null); }}
                onStart={() => { setHover(null); if (thaw.current) clearTimeout(thaw.current); setFrozen((f) => f ?? liveBounds); }}
                onMove={(x, y) => {
                  const p = toPlan(view, [x, y]);
                  dragParams.current = h.apply(p);
                  const text = Object.entries(dragParams.current).map(([k, v]) => `${HANDLE_LABEL[k] ?? k.replace(/_/g, " ")} ${v.toFixed(1)} m`).join("  ·  ");
                  setDrag({ key: h.key, x, y, text });
                  onDrag?.(dragParams.current);
                }}
                onEnd={() => {
                  setAnnounce(Object.entries(dragParams.current).map(([k, v]) => `${HANDLE_LABEL[k] ?? k} ${v.toFixed(1)} metres`).join(", "));
                  setDrag(null);
                  if (thaw.current) clearTimeout(thaw.current);
                  thaw.current = setTimeout(() => setFrozen(null), 1600);
                  onCommit?.(dragParams.current);
                }} />
            );
          })}

          {/* north arrow + scale bar */}
          <NorthArrow x={46} y={64} angle={plate.angle_deg || 0} th={th} />
          <ScaleBar x={size.w - 28 - 30} y={size.h - 38} s={view.s} th={th} />
        </Layer>
      </Stage>

      {/* floating LCD readout while a handle is dragged */}
      {drag && (
        <output className="lcd det" style={{ position: "absolute", left: Math.max(8, Math.min(drag.x + 16, size.w - 260)), top: Math.max(8, drag.y - 40), zIndex: 7, pointerEvents: "none", fontSize: "0.78rem" }}>
          {drag.text}
        </output>
      )}
      {hover && !drag && (
        <div className="hover-card" style={{ left: Math.max(8, Math.min(hover.x + 14, size.w - 210)), top: Math.max(8, hover.y - 10) }}>
          <b>{hover.html[0]}</b>
          {hover.html.slice(1).filter(Boolean).map((l, i) => <div key={i} className="muted" style={{ fontFamily: "var(--f-mono)", fontSize: 11.5 }}>{l}</div>)}
        </div>
      )}

      {/* view controls (right edge, clear of the app's top-right and bottom-left overlays) */}
      <div role="group" aria-label="Plan view" style={{ position: "absolute", right: 12, top: "50%", transform: "translateY(-50%)", display: "flex", flexDirection: "column", gap: 8, zIndex: 5 }}>
        <IconButton size="sm" label="Zoom in" icon={<Icon name="plus" size={14} />} onClick={() => zoomBy(1.25)} />
        <IconButton size="sm" label="Zoom out" icon={<Icon name="minus" size={14} />} onClick={() => zoomBy(1 / 1.25)} />
        <IconButton size="sm" label="Fit plan to view" icon={<Icon name="layers" size={14} />} disabled={!zoomed} onClick={() => setZoom({ k: 1, ox: 0, oy: 0 })} />
      </div>
      {mode !== "review" && (
        <div className="canvas-legend" aria-label="Zone legend">
          {(["flats", "core", "passage", "shaft"] as ZoneKind[]).map((k) => (
            <span key={k}><i style={{ background: zoneCss(k), boxShadow: "inset 0 0 0 1px var(--ink-3)" }} />{k}</span>
          ))}
          {mode === "shape" && <span><i style={{ background: "var(--cobalt)", borderRadius: "50%" }} />drag handle</span>}
        </div>
      )}
    </div>
    <span className="sr-only" role="status" aria-live="polite">{announce}</span>
    </>
  );
}

function PlanShapes({ plan, view, th, fontPx, showLabels, hoverRoom, onHover }: {
  plan: Plan; view: View; th: Theme; fontPx: number; showLabels: boolean; hoverRoom: string | null;
  onHover: (x: number, y: number, html: string[] | null, id?: string) => void;
}) {
  const rooms = plan.rooms;
  const bhk = rooms.filter((r) => r.type === "master" || r.type === "bedroom").length;
  const wallPoche = th.wall;
  return (
    <>
      {rooms.map((r) => (
        <Line key={r.id} points={flat(view, r.polygon)} closed fill={roomColor(r.type)}
          stroke={hoverRoom === r.id ? th.cobalt : undefined} strokeWidth={hoverRoom === r.id ? 2 : 0} strokeEnabled={hoverRoom === r.id}
          onMouseEnter={() => {
            const [x, y] = toScreen(view, centroid(r.polygon));
            onHover(x, y, [`${plan.unit_id} · ${bhk}BHK`, `Carpet ${fmt(plan.carpet_area)} m²`, `${r.name}: ${fmt(r.area, 2)} m²`,
              `Clear width ${fmt(r.width, 2)} m`, r.has_window ? `Window ${fmt(r.window_area, 2)} m²` : r.ventilated ? "Ventilated" : "No opening"], r.id);
          }}
          onMouseLeave={() => onHover(0, 0, null)} />
      ))}
      <Rings parts={plan.walls.map((w) => w.polygon)} view={view} fill={wallPoche} />
      {plan.windows.map((w) => {
        const [ax, ay] = toScreen(view, w.a), [bx, by] = toScreen(view, w.b);
        const t = Math.max(3, view.s * 0.2);
        return (
          <Group key={w.id} listening={false}>
            <Line points={[ax, ay, bx, by]} stroke={th.plate} strokeWidth={t + 1.5} lineCap="butt" />
            <Line points={[ax, ay, bx, by]} stroke={th.cobalt} strokeWidth={Math.max(1, t * 0.4)} lineCap="butt" />
          </Group>
        );
      })}
      {plan.doors.map((d) => <Door key={d.id} a={d.a} b={d.b} view={view} entry={d.kind === "entry"} th={th} />)}
      {showLabels && rooms.map((r) => {
        const [x, y] = toScreen(view, centroid(r.polygon));
        const raw = Math.sqrt(r.area) * view.s;
        // a label needs room: skip rooms too small to carry it at this zoom (it would overprint its neighbours)
        if (raw < 40) return null;
        const w = raw;
        return (
          <Group key={`l${r.id}`} x={x - w / 2} y={y - fontPx} listening={false}>
            <Text width={w} align="center" text={r.name} fontSize={fontPx} fontFamily={th.fSans} fontStyle="bold" fill={th.ink1} />
            <Text y={fontPx + 2} width={w} align="center" text={`${fmt(r.area)} m²`} fontSize={fontPx - 1} fontFamily={th.fMono} fill={th.ink2} />
          </Group>
        );
      })}
    </>
  );
}

function Door({ a, b, view, entry, th }: { a: Pt; b: Pt; view: View; entry: boolean; th: Theme }) {
  return (
    <Shape listening={false} sceneFunc={(ctx) => {
      const c = (ctx as unknown as { _context: CanvasRenderingContext2D })._context;
      const [ax, ay] = toScreen(view, a), [bx, by] = toScreen(view, b);
      const L = Math.hypot(bx - ax, by - ay);
      if (L < 1) return;
      const ux = (bx - ax) / L, uy = (by - ay) / L;
      // clear the wall at the opening, then draw the leaf (open 90 degrees) and the swing arc
      c.strokeStyle = entry ? th.cobalt : th.ink2;
      c.lineWidth = entry ? 2.2 : 1.3;
      c.beginPath();
      c.moveTo(ax, ay);
      c.lineTo(ax - uy * L, ay + ux * L);
      c.stroke();
      c.beginPath();
      const start = Math.atan2(uy, ux);
      c.arc(ax, ay, L, start, start + Math.PI / 2, false);
      c.lineWidth = 1;
      c.setLineDash([3, 2]);
      c.stroke();
      c.setLineDash([]);
    }} />
  );
}
