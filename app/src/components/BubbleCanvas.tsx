import { useEffect, useMemo, useRef, useState } from "react";
import { Circle, Group, Layer, Line, Stage, Text } from "react-konva";
import { cssColor, roomColor } from "../lib/geom";
import type { Diagram } from "../lib/types";

interface Props {
  diagram: Diagram;
  onChange: (d: { rooms: Diagram["rooms"]; links: Diagram["links"] }) => void;
}

export default function BubbleCanvas({ diagram, onChange }: Props) {
  const wrap = useRef<HTMLDivElement>(null);
  const [size, setSize] = useState({ w: 800, h: 600 });
  const [pick, setPick] = useState<string | null>(null);
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
  const clash = useMemo(() => new Set(diagram.conflicts.flatMap((c) => c.rooms)), [diagram.conflicts]);
  const cx = size.w / 2, cy = size.h / 2;
  // fit the bubble layout into the canvas
  const ext = Math.max(1, ...rooms.map((r) => Math.max(Math.abs(r.x), Math.abs(r.y)) + 40));
  const k = Math.min(1.4, (Math.min(size.w, size.h) / 2 - 30) / ext);
  const pos = (id: string) => {
    const r = rooms.find((x) => x.id === id);
    return r ? [cx + r.x * k, cy + r.y * k] : [cx, cy];
  };
  const ink = cssColor("var(--fg)");
  const toggleLink = (a: string, b: string) => {
    const has = diagram.links.some(([x, y]) => (x === a && y === b) || (x === b && y === a));
    const links = has ? diagram.links.filter(([x, y]) => !((x === a && y === b) || (x === b && y === a))) : [...diagram.links, [a, b] as [string, string]];
    onChange({ rooms, links });
  };
  return (
    <div ref={wrap} className="canvas-wrap">
      <Stage width={size.w} height={size.h}>
        <Layer>
          {diagram.links.map(([a, b]) => {
            const [x1, y1] = pos(a), [x2, y2] = pos(b);
            const bad = clash.has(a) && clash.has(b);
            return <Line key={`${a}-${b}`} points={[x1, y1, x2, y2]} stroke={bad ? "#c6373a" : "#7c8796"} strokeWidth={bad ? 3 : 2}
              hitStrokeWidth={12} onClick={() => toggleLink(a, b)} />;
          })}
          {rooms.map((r) => {
            const rad = 16 + Math.sqrt(Math.max(1, r.target_area)) * 6;
            const [x, y] = pos(r.id);
            const entry = diagram.entry === r.id;
            return (
              <Group key={r.id} x={x} y={y} draggable
                onDragEnd={(e) => {
                  const next = rooms.map((q) => q.id === r.id ? { ...q, x: (e.target.x() - cx) / k, y: (e.target.y() - cy) / k } : q);
                  setRooms(next);
                  onChange({ rooms: next, links: diagram.links });
                }}
                onClick={() => {
                  if (pick && pick !== r.id) { toggleLink(pick, r.id); setPick(null); } else setPick(pick === r.id ? null : r.id);
                }}
                onDblClick={() => {
                  const next = rooms.map((q) => q.id === r.id ? { ...q, placed: !q.placed } : q);
                  setRooms(next);
                  onChange({ rooms: next, links: diagram.links });
                }}>
                <Circle radius={rad} fill={roomColor(r.type)} opacity={r.placed ? 1 : 0.35}
                  stroke={pick === r.id ? "#0f8b8d" : clash.has(r.id) ? "#c6373a" : entry ? "#d94b4b" : ink}
                  strokeWidth={pick === r.id || clash.has(r.id) ? 3.5 : entry ? 2.5 : 1} dash={r.optional ? [5, 4] : undefined} />
                <Text text={`${r.name}\n${r.target_area} m²${r.needs_window ? " ☀" : ""}`} width={rad * 2.4} x={-rad * 1.2} y={-12}
                  align="center" fontSize={11} fill="#1d2433" listening={false} />
              </Group>
            );
          })}
        </Layer>
      </Stage>
      <div className="canvas-legend">
        <span>Click two rooms to link / unlink</span><span>Double-click: place / unplace</span>
        <span><i style={{ border: "2px dashed #555", background: "transparent" }} />optional</span>
        <span><i style={{ background: "#d94b4b" }} />entry</span><span>☀ needs a window</span>
      </div>
    </div>
  );
}
