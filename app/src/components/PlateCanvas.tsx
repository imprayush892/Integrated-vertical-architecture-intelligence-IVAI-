import type Konva from "konva";
import { useEffect, useMemo, useRef, useState } from "react";
import { Circle, Layer, Line, Rect, Shape, Stage, Text } from "react-konva";
import { boundsOf, centroid, cssColor, fitView, flat, fmt, roomColor, toPlan, toScreen, type Bounds, type View } from "../lib/geom";
import type { Plan, Plate, PolyRings, Pt, Ring, Unit } from "../lib/types";

export type CanvasMode = "shape" | "units" | "review";

interface Handle { key: string; at: Pt; apply: (p: Pt) => Record<string, number> }

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

const snap = (v: number) => Math.round(v * 10) / 10;
const UNIT_TINTS = ["#d9ecf2", "#f6e3cf", "#e3f0d7", "#efdcef", "#f2efcf", "#dde3f6", "#f6dada", "#d8f1ea"];

function handlesFor(p: Plate): Handle[] {
  const pr = p.params;
  const W = pr.W, D = pr.D;
  const core = p.zones.find((z) => z.kind === "core")?.polygon?.[0]?.[0] ?? [];
  const cb = boundsOf([core as Ring]);
  const cx = (cb.minx + cb.maxx) / 2, cy = (cb.miny + cb.maxy) / 2;
  const hs: Handle[] = [
    { key: "W", at: [W / 2, 0], apply: ([x]) => ({ W: snap(2 * Math.abs(x)) }) },
    { key: "D", at: [0, D / 2], apply: ([, y]) => ({ D: snap(2 * Math.abs(y)) }) },
    { key: "core", at: [cb.maxx, cb.maxy], apply: ([x, y]) => ({ core_w: snap(2 * Math.abs(x - cx)), core_d: snap(2 * Math.abs(y - cy)) }) },
  ];
  if (p.shape === "cruciform") {
    hs.push({ key: "arm_depth", at: [W / 2, pr.arm_depth / 2], apply: ([, y]) => ({ arm_depth: snap(2 * Math.abs(y)) }) });
    hs.push({ key: "wing_width", at: [pr.wing_width / 2, D / 2], apply: ([x]) => ({ wing_width: snap(2 * Math.abs(x)) }) });
  } else if (p.shape === "h_shape") {
    hs.push({ key: "bar_width", at: [-W / 2 + pr.bar_width, D / 2], apply: ([x]) => ({ bar_width: snap(x + W / 2) }) });
    hs.push({ key: "bridge_depth", at: [0, pr.bridge_depth / 2], apply: ([, y]) => ({ bridge_depth: snap(2 * Math.abs(y)) }) });
  } else if (p.shape === "t_shape") {
    hs.push({ key: "bar_depth", at: [W / 2, D / 2 - pr.bar_depth], apply: ([, y]) => ({ bar_depth: snap(D / 2 - y) }) });
    hs.push({ key: "stem_width", at: [pr.stem_width / 2, -D / 2], apply: ([x]) => ({ stem_width: snap(2 * Math.abs(x)) }) });
  }
  return hs;
}

function Rings({ parts, view, fill, stroke, strokeWidth = 1, opacity = 1 }: { parts: PolyRings[]; view: View; fill: string; stroke?: string; strokeWidth?: number; opacity?: number }) {
  return (
    <Shape
      listening={false}
      opacity={opacity}
      sceneFunc={(ctx) => {
        const c = (ctx as unknown as { _context: CanvasRenderingContext2D })._context;
        c.beginPath();
        for (const part of parts) for (const ring of part) {
          ring.forEach((p, i) => { const [x, y] = toScreen(view, p); if (i === 0) c.moveTo(x, y); else c.lineTo(x, y); });
          c.closePath();
        }
        c.fillStyle = fill;
        c.fill("evenodd");
        if (stroke) { c.strokeStyle = stroke; c.lineWidth = strokeWidth; c.stroke(); }
      }}
    />
  );
}

export default function PlateCanvas(props: Props) {
  const { plate, mode, units = [], plans = [], selectedUnit, onSelectUnit, onDrag, onCommit, focus, showLabels = true } = props;
  const wrap = useRef<HTMLDivElement>(null);
  const [size, setSize] = useState({ w: 800, h: 600 });
  const [zoom, setZoom] = useState<{ k: number; ox: number; oy: number }>({ k: 1, ox: 0, oy: 0 });
  const [hover, setHover] = useState<{ x: number; y: number; html: string[] } | null>(null);
  const dragParams = useRef<Record<string, number>>({});

  useEffect(() => {
    const el = wrap.current;
    if (!el) return;
    const ro = new ResizeObserver(() => setSize({ w: el.clientWidth, h: el.clientHeight }));
    ro.observe(el);
    setSize({ w: el.clientWidth, h: el.clientHeight });
    return () => ro.disconnect();
  }, []);

  const baseBounds: Bounds = useMemo(() => boundsOf([plate.outline]), [plate.outline]);
  const view: View = useMemo(() => {
    const b = focus ? { minx: focus[0] - 1.5, miny: focus[1] - 1.5, maxx: focus[2] + 1.5, maxy: focus[3] + 1.5 } : baseBounds;
    const v = fitView(b, size.w, size.h, 48);
    return { s: v.s * zoom.k, tx: v.tx * zoom.k + zoom.ox, ty: v.ty * zoom.k + zoom.oy };
  }, [baseBounds, size, zoom, focus]);

  useEffect(() => { setZoom({ k: 1, ox: 0, oy: 0 }); }, [focus]);

  const onWheel = (e: Konva.KonvaEventObject<WheelEvent>) => {
    e.evt.preventDefault();
    const stage = e.target.getStage();
    const p = stage?.getPointerPosition();
    if (!p) return;
    const f = e.evt.deltaY < 0 ? 1.12 : 1 / 1.12;
    setZoom((z) => ({ k: z.k * f, ox: p.x - (p.x - z.ox) * f, oy: p.y - (p.y - z.oy) * f }));
  };

  const ink = cssColor("var(--fg)");
  const muted = cssColor("var(--muted)");
  const wall = cssColor("var(--wall)");
  const teal = "#0f8b8d";
  const z = (k: string) => plate.zones.filter((x) => x.kind === k).flatMap((x) => x.polygon);
  const fontPx = Math.max(9, Math.min(13, view.s * 0.35));

  return (
    <div ref={wrap} className="canvas-wrap">
      <Stage width={size.w} height={size.h} onWheel={onWheel} draggable={false}
        onMouseLeave={() => setHover(null)}>
        <Layer>
          {/* plate outline and zones */}
          <Line points={flat(view, plate.outline)} closed fill={cssColor("var(--panel)")} stroke={ink} strokeWidth={1.5} listening={false} />
          {mode !== "review" && <Rings parts={z("flats")} view={view} fill="#0f8b8d" opacity={0.08} />}
          <Rings parts={z("passage")} view={view} fill={cssColor("var(--room-passage)")} stroke={muted} strokeWidth={0.6} />
          <Rings parts={z("core")} view={view} fill="#8b95a5" opacity={0.85} />
          <Rings parts={z("shaft")} view={view} fill="#8fd0f6" stroke="#3a8fc0" strokeWidth={0.8} />
          {plate.core_parts.map((c, i) => (
            <Line key={i} points={flat(view, c.polygon)} closed listening={false}
              stroke={c.kind === "stair" ? "#ffffff" : "#dfe3e8"} strokeWidth={c.kind === "lobby" ? 0 : 1}
              dash={c.kind === "stair" ? [4, 3] : undefined} fill={c.kind === "lobby" ? "#a9b1bd" : undefined} />
          ))}

          {/* units */}
          {(mode === "units" || mode === "shape") && units.map((u, i) => {
            const c = centroid(u.polygon);
            const [sx, sy] = toScreen(view, c);
            const sel = selectedUnit === u.id;
            const flagged = u.flags.length > 0;
            return (
              <Line key={u.id} points={flat(view, u.polygon)} closed
                fill={UNIT_TINTS[i % UNIT_TINTS.length]} opacity={mode === "shape" ? 0.55 : 0.95}
                stroke={flagged ? "#c6373a" : sel ? teal : "#5b6475"} strokeWidth={sel ? 3 : flagged ? 2 : 1}
                onClick={() => onSelectUnit?.(u.id)} onTap={() => onSelectUnit?.(u.id)}
                onMouseEnter={(e) => { e.target.getStage()!.container().style.cursor = "pointer"; setHover({ x: sx, y: sy, html: [u.id, `${fmt(u.area)} m²`, u.programme?.name ?? "", ...u.flags.map((f) => `⚠ ${f.replace(/_/g, " ")}`)] }); }}
                onMouseLeave={(e) => { e.target.getStage()!.container().style.cursor = "default"; setHover(null); }}
              />
            );
          })}
          {mode === "units" && units.map((u) => {
            const [sx, sy] = toScreen(view, centroid(u.polygon));
            return <Text key={`t${u.id}`} x={sx - 50} y={sy - 12} width={100} align="center" listening={false}
              text={`${u.id}\n${fmt(u.area)} m²`} fontSize={fontPx} fill={cssColor("#1d2433")} fontStyle="bold" />;
          })}
          {mode === "units" && units.map((u) => u.core_link && (
            <Circle key={`d${u.id}`} x={toScreen(view, u.core_link)[0]} y={toScreen(view, u.core_link)[1]} radius={5}
              fill="#d94b4b" stroke="#fff" strokeWidth={1.5} listening={false} />
          ))}

          {/* drawn plans */}
          {mode === "review" && plans.map((pl) => (
            <PlanShapes key={pl.unit_id} plan={pl} view={view} wall={wall} fontPx={fontPx} showLabels={showLabels}
              onHover={(x, y, html) => setHover(html ? { x, y, html } : null)} />
          ))}
          {mode === "review" && focus && (
            <Rect x={toScreen(view, [focus[0], focus[3]])[0]} y={toScreen(view, [focus[0], focus[3]])[1]}
              width={(focus[2] - focus[0]) * view.s} height={(focus[3] - focus[1]) * view.s}
              stroke="#c97d00" strokeWidth={2.5} dash={[6, 4]} listening={false} />
          )}

          {/* parametric handles */}
          {mode === "shape" && handlesFor(plate).map((h) => {
            const [hx, hy] = toScreen(view, h.at);
            return (
              <Circle key={h.key} x={hx} y={hy} radius={8} fill="#ffffff" stroke={teal} strokeWidth={3} draggable
                onMouseEnter={(e) => { e.target.getStage()!.container().style.cursor = "grab"; }}
                onMouseLeave={(e) => { e.target.getStage()!.container().style.cursor = "default"; }}
                onDragMove={(e) => {
                  const p = toPlan(view, [e.target.x(), e.target.y()]);
                  dragParams.current = h.apply(p);
                  onDrag?.(dragParams.current);
                }}
                onDragEnd={() => onCommit?.(dragParams.current)}
              />
            );
          })}
        </Layer>
      </Stage>
      {hover && (
        <div className="hover-card" style={{ left: Math.min(hover.x + 14, size.w - 200), top: Math.max(8, hover.y - 10) }}>
          <b>{hover.html[0]}</b>
          {hover.html.slice(1).filter(Boolean).map((l, i) => <div key={i} className="muted">{l}</div>)}
        </div>
      )}
    </div>
  );
}

function PlanShapes({ plan, view, wall, fontPx, showLabels, onHover }: {
  plan: Plan; view: View; wall: string; fontPx: number; showLabels: boolean;
  onHover: (x: number, y: number, html: string[] | null) => void;
}) {
  const rooms = plan.rooms;
  const bhk = rooms.filter((r) => r.type === "master" || r.type === "bedroom").length;
  return (
    <>
      {rooms.map((r) => (
        <Line key={r.id} points={flat(view, r.polygon)} closed fill={roomColor(r.type)} strokeEnabled={false}
          onMouseEnter={() => {
            const [x, y] = toScreen(view, centroid(r.polygon));
            onHover(x, y, [`${plan.unit_id} · ${bhk}BHK`, `Carpet ${fmt(plan.carpet_area)} m²`, `${r.name}: ${fmt(r.area, 2)} m²`,
              `Clear width ${fmt(r.width, 2)} m`, r.has_window ? `Window ${fmt(r.window_area, 2)} m²` : r.ventilated ? "Ventilated" : "No opening"]);
          }}
          onMouseLeave={() => onHover(0, 0, null)} />
      ))}
      <Rings parts={plan.walls.map((w) => w.polygon)} view={view} fill={wall} />
      {plan.windows.map((w) => {
        const [ax, ay] = toScreen(view, w.a), [bx, by] = toScreen(view, w.b);
        return <Line key={w.id} points={[ax, ay, bx, by]} stroke="#4aa8e0" strokeWidth={Math.max(2, view.s * 0.16)} listening={false} />;
      })}
      {plan.doors.map((d) => <Door key={d.id} a={d.a} b={d.b} view={view} entry={d.kind === "entry"} />)}
      {showLabels && rooms.map((r) => {
        const [x, y] = toScreen(view, centroid(r.polygon));
        const w = Math.max(40, Math.sqrt(r.area) * view.s);
        if (w < 34) return null;
        return <Text key={`l${r.id}`} x={x - w / 2} y={y - fontPx} width={w} align="center" listening={false}
          text={`${r.name}\n${fmt(r.area)}`} fontSize={fontPx} fill="#1d2433" />;
      })}
    </>
  );
}

function Door({ a, b, view, entry }: { a: Pt; b: Pt; view: View; entry: boolean }) {
  return (
    <Shape listening={false} sceneFunc={(ctx) => {
      const c = (ctx as unknown as { _context: CanvasRenderingContext2D })._context;
      const [ax, ay] = toScreen(view, a), [bx, by] = toScreen(view, b);
      const L = Math.hypot(bx - ax, by - ay);
      if (L < 1) return;
      const ux = (bx - ax) / L, uy = (by - ay) / L;
      // clear the wall at the opening, then draw the leaf (open 90 degrees) and the swing arc
      c.strokeStyle = entry ? "#d94b4b" : "#c0392b";
      c.lineWidth = entry ? 2 : 1.2;
      c.beginPath();
      c.moveTo(ax, ay);
      c.lineTo(ax - uy * L, ay + ux * L);
      c.stroke();
      c.beginPath();
      const start = Math.atan2(uy, ux);
      c.arc(ax, ay, L, start, start + Math.PI / 2, false);
      c.setLineDash([3, 2]);
      c.stroke();
      c.setLineDash([]);
    }} />
  );
}
