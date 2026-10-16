import { useEffect, useMemo, useRef, useState } from "react";
import { Circle, Group, Layer, Line, Rect, Stage, Text } from "react-konva";
import { cssColor, ROOM_COLORS, roomColor } from "../lib/geom";
import type { Diagram } from "../lib/types";
import { Kbd, Led } from "../ui";

interface Props {
  diagram: Diagram;
  onChange: (d: { rooms: Diagram["rooms"]; links: Diagram["links"] }) => void;
}

// ------------------------------------------------------------------------------------------- styles (bb- prefix)
const CSS = `
.bb { position: absolute; inset: 0; overflow: hidden;
  background-image: radial-gradient(circle at 1px 1px, var(--hairline) 1px, transparent 1.5px); background-size: 22px 22px; }
.bb-hud { position: absolute; left: 12px; top: 12px; right: 12px; z-index: 4; display: flex; flex-direction: column; gap: 8px; align-items: flex-start; pointer-events: none; }
.bb-hud > * { pointer-events: auto; }
.bb-status { display: inline-flex; align-items: center; gap: 8px; padding: 6px 12px; border-radius: var(--r-pill); background: var(--neo-bg); box-shadow: var(--neo-latched); border: 1px solid var(--chamfer-inner); font: 600 12px var(--f-sans); color: var(--ink-1); max-width: min(100%, 300px); line-height: 1.35; }
.bb-status kbd { margin-left: 2px; }
.bb-rooms { max-width: 100%; }
.bb-rooms > summary { list-style: none; cursor: pointer; display: inline-flex; align-items: center; padding: 6px 12px; min-height: 30px; border-radius: var(--r-pill); font: 600 12px var(--f-sans); color: var(--ink-1); background: var(--neo-convex-bg); box-shadow: var(--neo-raised-xs); border: 1px solid var(--chamfer); }
.bb-rooms > summary::-webkit-details-marker { display: none; }
.bb-rooms[open] > summary { box-shadow: var(--neo-latched); background: var(--neo-bg); }
.bb-rooms .bb-rail { margin-top: 8px; padding: 8px; border-radius: var(--r-md); background: var(--neo-bg); box-shadow: var(--neo-raised-sm); }
.bb-rail { display: flex; gap: 6px; flex-wrap: wrap; margin: 0; padding: 0; list-style: none; max-width: 100%; }
.bb-room { display: inline-flex; align-items: stretch; border-radius: var(--r-pill); background: var(--neo-convex-bg); box-shadow: var(--neo-raised-xs); border: 1px solid var(--chamfer); overflow: hidden; }
.bb-room.is-pick { box-shadow: var(--neo-latched), 0 0 0 2px var(--cobalt); background: var(--neo-bg); }
.bb-room.is-off { background: var(--neo-bg); box-shadow: var(--neo-latched); }
.bb-room button { appearance: none; background: transparent; border: 0; cursor: pointer; font: 600 11.5px var(--f-sans); color: var(--ink-1); padding: 5px 10px; display: inline-flex; align-items: center; gap: 6px; min-height: 28px; }
.bb-room button + button { border-left: 1px solid var(--hairline); padding: 5px 9px; color: var(--ink-2); font: 600 10.5px var(--f-mono); letter-spacing: 0.04em; }
.bb-room button:hover { color: var(--cobalt-ink); }
.bb-room.is-off > button:first-child { color: var(--ink-2); text-decoration: line-through; text-decoration-thickness: 1px; }
.bb-sw { width: 9px; height: 9px; border-radius: 50%; box-shadow: inset 0 0 0 1px var(--chamfer-inner); flex: none; }
.bb-legend { position: absolute; left: 12px; bottom: 12px; z-index: 4; display: flex; flex-wrap: wrap; gap: 6px; max-width: calc(100% - 24px); }
.bb-legend > span { display: inline-flex; align-items: center; gap: 6px; padding: 3px 9px; font: 600 11px var(--f-mono); color: var(--ink-2); background: var(--neo-bg); border: 1px solid var(--chamfer); border-radius: var(--r-pill); box-shadow: var(--neo-raised-xs); }
.bb-key-line { width: 20px; height: 0; border-top: 2px solid var(--ink-2); flex: none; }
.bb-key-line.opt { border-top-style: dashed; }
.bb-key-ring { width: 12px; height: 12px; border-radius: 50%; border: 2px solid var(--cobalt); flex: none; }
.bb-key-dome { width: 12px; height: 12px; border-radius: 50%; background: radial-gradient(circle at 35% 30%, var(--neo-grad-light), var(--room-bedroom)); box-shadow: var(--neo-raised-xs); flex: none; }
.bb-empty { position: absolute; inset: 0; display: grid; place-items: center; text-align: center; padding: 24px; color: var(--ink-2); font-size: 13px; pointer-events: none; }
.bb-sr { position: absolute; width: 1px; height: 1px; overflow: hidden; clip: rect(0 0 0 0); white-space: nowrap; }
@media (max-width: 640px) { .bb-legend { display: none; } }
`;
function useInjectedCss(id: string, css: string) {
  useEffect(() => {
    if (document.getElementById(id)) return;
    const el = document.createElement("style");
    el.id = id;
    el.textContent = css;
    document.head.appendChild(el);
  }, [id, css]);
}

// ------------------------------------------------------------------------------------------- colour helpers
/** Mix two "#rrggbb" colours (t = share of b). Falls back to `a` for anything else (rgb(), names). */
function mix(a: string, b: string, t: number): string {
  const p = (h: string) => (/^#[0-9a-f]{6}$/i.test(h) ? [1, 3, 5].map((i) => parseInt(h.slice(i, i + 2), 16)) : null);
  const x = p(a), y = p(b);
  if (!x || !y) return a;
  return `rgb(${x.map((v, i) => Math.round(v + (y[i] - v) * t)).join(",")})`;
}

export default function BubbleCanvas({ diagram, onChange }: Props) {
  useInjectedCss("bb-styles", CSS);
  const wrap = useRef<HTMLDivElement>(null);
  const [size, setSize] = useState({ w: 800, h: 600 });
  const [pick, setPick] = useState<string | null>(null);
  const [hoverLink, setHoverLink] = useState<string | null>(null);
  const [hoverRoom, setHoverRoom] = useState<string | null>(null);
  const [dragging, setDragging] = useState<string | null>(null);
  const [rooms, setRooms] = useState(diagram.rooms);
  useEffect(() => setRooms(diagram.rooms), [diagram]);
  useEffect(() => {
    const el = wrap.current;
    if (!el) return;
    const ro = new ResizeObserver(() => setSize({ w: el.clientWidth, h: el.clientHeight }));
    ro.observe(el);
    setSize({ w: el.clientWidth, h: el.clientHeight });
    return () => ro.disconnect();
  }, []);
  // Escape cancels a half-made link
  useEffect(() => {
    if (!pick) return;
    const on = (e: KeyboardEvent) => { if (e.key === "Escape") setPick(null); };
    window.addEventListener("keydown", on);
    return () => window.removeEventListener("keydown", on);
  }, [pick]);

  const clash = useMemo(() => new Set(diagram.conflicts.flatMap((c) => c.rooms)), [diagram.conflicts]);
  const byId = useMemo(() => new Map(rooms.map((r) => [r.id, r])), [rooms]);
  // fit the bubble layout's bounding box into the free area between the HUD (top) and the legend (bottom);
  // room positions stay in the engine's own coordinates
  const PAD = 44; // approx. bubble radius at k = 1
  const xs = rooms.map((r) => r.x), ys = rooms.map((r) => r.y);
  const minX = Math.min(0, ...xs) - PAD, maxX = Math.max(0, ...xs) + PAD;
  const minY = Math.min(0, ...ys) - PAD, maxY = Math.max(0, ...ys) + PAD;
  const freeTop = 64, freeBottom = 120;
  const k = Math.max(0.2, Math.min(1.4, (size.w - 24) / (maxX - minX), (size.h - freeTop - freeBottom) / (maxY - minY)));
  const cx = size.w / 2 - ((minX + maxX) / 2) * k;
  const cy = freeTop + (size.h - freeTop - freeBottom) / 2 - ((minY + maxY) / 2) * k;
  const pos = (id: string) => {
    const r = byId.get(id);
    return r ? [cx + r.x * k, cy + r.y * k] : [cx, cy];
  };

  // Konva needs concrete colours; the shell remounts this canvas on theme change, so read them once
  const C = useMemo(() => ({
    ink: cssColor("var(--ink-1)"), ink2: cssColor("var(--ink-2)"), ink3: cssColor("var(--ink-3)"),
    shDark: cssColor("var(--sh-dark)"), shLight: cssColor("var(--sh-light)"), bg: cssColor("var(--neo-bg)"),
    hair: cssColor("var(--hairline)"), cobalt: cssColor("var(--cobalt)"), fail: cssColor("var(--fail)"),
    det: cssColor("var(--det)"), white: cssColor("var(--neo-grad-light)"), shade: cssColor("var(--neo-grad-dark)"),
    sans: "Inter, -apple-system, 'Segoe UI', sans-serif", mono: "'JetBrains Mono', ui-monospace, Menlo, monospace",
  }), []);

  const roomVar = (type: string) => ROOM_COLORS[type] ?? "var(--room-other)";
  const same = (x: string, y: string, a: string, b: string) => (x === a && y === b) || (x === b && y === a);
  const toggleLink = (a: string, b: string) => {
    const has = diagram.links.some(([x, y]) => same(x, y, a, b));
    const links = has ? diagram.links.filter(([x, y]) => !same(x, y, a, b)) : [...diagram.links, [a, b] as [string, string]];
    onChange({ rooms, links });
  };
  const clickRoom = (id: string) => {
    if (pick && pick !== id) { toggleLink(pick, id); setPick(null); } else setPick(pick === id ? null : id);
  };
  const togglePlaced = (id: string) => {
    const next = rooms.map((q) => q.id === id ? { ...q, placed: !q.placed } : q);
    setRooms(next);
    onChange({ rooms: next, links: diagram.links });
  };
  const cursor = (e: { target: { getStage: () => { container: () => HTMLElement } | null } }, v: string) => {
    const c = e.target.getStage()?.container();
    if (c) c.style.cursor = v;
  };

  const pickName = pick ? byId.get(pick)?.name : null;
  const hoverL = hoverLink ? hoverLink.split("|") : null;
  const hoverLName = hoverL ? `${byId.get(hoverL[0])?.name ?? hoverL[0]} to ${byId.get(hoverL[1])?.name ?? hoverL[1]}` : null;

  return (
    <div ref={wrap} className="bb">
      <Stage width={size.w} height={size.h} aria-hidden="true">
        {/* links: engraved channels. Required = solid inner line, optional (either end optional) = dashed */}
        <Layer>
          {diagram.links.map(([a, b]) => {
            const [x1, y1] = pos(a), [x2, y2] = pos(b);
            const bad = clash.has(a) && clash.has(b);
            const optional = !!(byId.get(a)?.optional || byId.get(b)?.optional);
            const id = `${a}|${b}`;
            const hot = hoverLink === id;
            const dx = x2 - x1, dy = y2 - y1, len = Math.hypot(dx, dy) || 1;
            const nx = (-dy / len) * 1.2, ny = (dx / len) * 1.2; // light rim offset (sun top-left)
            return (
              <Group key={id}>
                <Line points={[x1 + nx, y1 + ny, x2 + nx, y2 + ny]} stroke={C.shLight} strokeWidth={9} lineCap="round" opacity={0.9} listening={false} />
                <Line points={[x1 - nx, y1 - ny, x2 - nx, y2 - ny]} stroke={C.shDark} strokeWidth={9} lineCap="round" opacity={0.75} listening={false} />
                <Line points={[x1, y1, x2, y2]} stroke={C.bg} strokeWidth={6} lineCap="round" listening={false} />
                <Line points={[x1, y1, x2, y2]} stroke={bad ? C.fail : hot ? C.cobalt : C.ink2} strokeWidth={bad || hot ? 3 : 2} lineCap="round"
                  dash={optional ? [6, 5] : undefined} opacity={bad || hot ? 1 : 0.85}
                  hitStrokeWidth={16}
                  onClick={() => toggleLink(a, b)} onTap={() => toggleLink(a, b)}
                  onMouseEnter={(e) => { setHoverLink(id); cursor(e, "pointer"); }}
                  onMouseLeave={(e) => { setHoverLink(null); cursor(e, ""); }} />
              </Group>
            );
          })}
        </Layer>
        {/* rooms: convex domes sized by target area */}
        <Layer>
          {rooms.map((r) => {
            const rad = 16 + Math.sqrt(Math.max(1, r.target_area)) * 6;
            const [x, y] = pos(r.id);
            const entry = diagram.entry === r.id;
            const isPick = pick === r.id;
            const isClash = clash.has(r.id);
            const lifted = dragging === r.id || hoverRoom === r.id;
            const base = roomColor(r.type);
            const hi = mix(base, "#ffffff", 0.55), lo = mix(base, "#000000", 0.18);
            const ring = isPick ? C.cobalt : isClash ? C.fail : entry ? C.det : null;
            const label = `${r.name}\n${Math.round(r.target_area * 10) / 10} m²${r.needs_window ? " ☀" : ""}`;
            return (
              <Group key={r.id} x={x} y={y} draggable
                onMouseEnter={(e) => { setHoverRoom(r.id); cursor(e, "grab"); }}
                onMouseLeave={(e) => { setHoverRoom(null); cursor(e, ""); }}
                onDragStart={(e) => { setDragging(r.id); cursor(e, "grabbing"); }}
                onDragEnd={(e) => {
                  setDragging(null);
                  cursor(e, "grab");
                  const next = rooms.map((q) => q.id === r.id ? { ...q, x: (e.target.x() - cx) / k, y: (e.target.y() - cy) / k } : q);
                  setRooms(next);
                  onChange({ rooms: next, links: diagram.links });
                }}
                onClick={() => clickRoom(r.id)} onTap={() => clickRoom(r.id)}
                onDblClick={() => togglePlaced(r.id)} onDblTap={() => togglePlaced(r.id)}>
                {r.placed ? (
                  <>
                    {/* dome body: light from the top-left */}
                    <Circle radius={rad}
                      fillRadialGradientStartPoint={{ x: -rad * 0.38, y: -rad * 0.42 }} fillRadialGradientStartRadius={rad * 0.05}
                      fillRadialGradientEndPoint={{ x: 0, y: 0 }} fillRadialGradientEndRadius={rad * 1.05}
                      fillRadialGradientColorStops={[0, hi, 0.55, base, 1, lo]}
                      shadowColor={C.shDark} shadowBlur={lifted ? 22 : 14} shadowOffset={{ x: lifted ? 9 : 6, y: lifted ? 9 : 6 }} shadowOpacity={1}
                      stroke={C.shLight} strokeWidth={1} />
                  </>
                ) : (
                  <>
                    {/* not placed: sunk into the slab */}
                    <Circle radius={rad} fill={C.bg} stroke={C.shDark} strokeWidth={2} opacity={0.9}
                      shadowColor={C.shDark} shadowBlur={10} shadowOffset={{ x: 3, y: 3 }} shadowOpacity={0.6} />
                    <Circle radius={rad - 5} stroke={C.shLight} strokeWidth={1.5} opacity={0.9} listening={false} />
                  </>
                )}
                {r.optional && <Circle radius={rad + 4} stroke={C.ink2} strokeWidth={1.5} dash={[5, 4]} listening={false} />}
                {ring && <Circle radius={rad + (r.optional ? 8 : 4)} stroke={ring} strokeWidth={isPick || isClash ? 3 : 2.5} listening={false} />}
                <Text text={label} width={rad * 1.9} height={rad * 1.7} x={-rad * 0.95} y={-rad * 0.85} align="center" verticalAlign="middle"
                  fontFamily={C.sans} fontStyle="600" fontSize={11.5} lineHeight={1.25} fill={r.placed ? C.ink : C.ink2}
                  ellipsis wrap="word" listening={false} />
                {entry && (
                  <Group y={rad + 12} listening={false}>
                    <Rect x={-22} y={-8} width={44} height={16} cornerRadius={8} fill={C.bg} stroke={C.det} strokeWidth={1.5} />
                    <Text text="ENTRY" x={-22} y={-5} width={44} align="center" fontFamily={C.mono} fontStyle="700" fontSize={9} fill={C.ink} />
                  </Group>
                )}
              </Group>
            );
          })}
        </Layer>
      </Stage>

      {/* HUD: state of the interaction */}
      <div className="bb-hud">
        <div className="bb-status" role="status" aria-live="polite">
          <Led status={pick ? "cobalt" : "idle"} pulse={!!pick} />
          {pick
            ? <>Linking from <b>{pickName}</b>. Pick another room, or press <Kbd>Esc</Kbd></>
            : hoverLName
              ? <>Click to unlink {hoverLName}</>
              : <>Click two rooms to link or unlink</>}
        </div>
        {rooms.length > 0 && (
          <details className="bb-rooms">
            <summary>Rooms ({rooms.length}) · keyboard controls</summary>
            <ul className="bb-rail" aria-label="Rooms. Select one room, then another, to link or unlink them.">
              {rooms.map((r) => (
                <li key={r.id} className={`bb-room${pick === r.id ? " is-pick" : ""}${r.placed ? "" : " is-off"}`}>
                  <button type="button" aria-pressed={pick === r.id} onClick={() => clickRoom(r.id)}
                    aria-label={`${r.name}, ${Math.round(r.target_area * 10) / 10} square metres${r.optional ? ", optional" : ""}${r.placed ? "" : ", not placed"}${diagram.entry === r.id ? ", entry" : ""}`}>
                    <span className="bb-sw" style={{ background: roomVar(r.type) }} />
                    {r.name}
                  </button>
                  <button type="button" aria-pressed={r.placed} aria-label={`${r.placed ? "Unplace" : "Place"} ${r.name}`}
                    title="Place or unplace (double-click on the canvas)" onClick={() => togglePlaced(r.id)}>
                    {r.placed ? "PLACED" : "OFF"}
                  </button>
                </li>
              ))}
            </ul>
          </details>
        )}
      </div>

      {rooms.length === 0 && <div className="bb-empty"><div>No rooms in this flat yet.<br />Pick a room list in stage 6.</div></div>}

      <div className="bb-legend" aria-label="Diagram legend">
        <span><i className="bb-key-dome" />Dome size = target area</span>
        <span><i className="bb-key-line" />required link</span>
        <span><i className="bb-key-line opt" />optional link</span>
        <span><i className="bb-key-ring" />selected</span>
        <span>Drag to move</span>
        <span>Double-click: place / unplace</span>
        <span>☀ needs a window</span>
      </div>
    </div>
  );
}
