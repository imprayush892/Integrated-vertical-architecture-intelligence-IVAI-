import { useEffect, useMemo, useRef, useState } from "react";
import { rpc } from "../lib/api";
import { fmt } from "../lib/geom";
import { useStore } from "../lib/store";
import { Chip, EmptyState, Field, Icon, Kbd, LatchButton, Panel, ProvenanceTag, Readout, Spinner } from "../ui";
import "./stage14.css";

type Pt = [number, number];
interface Measure { edges: number[]; area: number; perimeter: number; valid: boolean }

const ROAD_MIN = 6, ROAD_MAX = 60, HEIGHT_MIN = 9, HEIGHT_MAX = 200;

// WGS84 ellipsoid: area of a small plot by local curvature radii. Used as the live cross-check for the
// engine's UTM shoelace area. The engine's own geodesic value replaces it once the site is analysed.
function geodesicApprox(pts: Pt[]): number | null {
  if (pts.length < 3) return null;
  const a = 6378137, e2 = 0.00669437999014;
  const lat0 = pts.reduce((s, p) => s + p[1], 0) / pts.length;
  const lng0 = pts.reduce((s, p) => s + p[0], 0) / pts.length;
  const phi = (lat0 * Math.PI) / 180;
  const w = Math.sqrt(1 - e2 * Math.sin(phi) ** 2);
  const N = a / w, M = (a * (1 - e2)) / (w * w * w);
  const xy = pts.map(([lng, lat]) => [((lng - lng0) * Math.PI / 180) * N * Math.cos(phi), ((lat - lat0) * Math.PI / 180) * M]);
  let s = 0;
  for (let i = 0; i < xy.length; i++) {
    const [x1, y1] = xy[i], [x2, y2] = xy[(i + 1) % xy.length];
    s += x1 * y2 - x2 * y1;
  }
  return Math.abs(s) / 2;
}
const samePoints = (a: Pt[], b: Pt[]) => a.length === b.length && a.every((p, i) => Math.abs(p[0] - b[i][0]) < 1e-9 && Math.abs(p[1] - b[i][1]) < 1e-9);

/** Live measurement through the engine (debounced). Stale replies are dropped. */
function useMeasure(points: Pt[]) {
  const [m, setM] = useState<Measure | null>(null);
  const [state, setState] = useState<"idle" | "loading" | "error">("idle");
  const seq = useRef(0);
  useEffect(() => {
    if (points.length < 2) { setM(null); setState("idle"); return; }
    const mine = ++seq.current;
    setState("loading");
    const t = setTimeout(() => {
      rpc<Measure>("measure", { lnglat: points })
        .then((r) => { if (mine === seq.current) { setM(r); setState("idle"); } })
        .catch(() => { if (mine === seq.current) setState("error"); });
    }, 140);
    return () => clearTimeout(t);
  }, [points]);
  return { m, state };
}

// ------------------------------------------------------------------------------------------- 2 Trace
export function TracePanel({ points, setPoints }: { points: [number, number][]; setPoints: (p: [number, number][]) => void }) {
  const run = useStore((s) => s.run);
  const site = useStore((s) => s.project?.site);
  const setStep = useStore((s) => s.setStep);
  const [road, setRoad] = useState("12");
  const [height, setHeight] = useState("24");
  const [name, setName] = useState("SITE 01");
  const [locality, setLocality] = useState("");
  const [analysing, setAnalysing] = useState(false);
  const [redo, setRedo] = useState<Pt[]>([]);
  const lastSet = useRef<Pt[] | null>(null);
  const { m, state } = useMeasure(points);

  useEffect(() => {
    if (site) { setRoad(String(site.road_width)); setHeight(String(site.building_height)); setName(site.name); setLocality(site.locality); }
  }, [site]);

  // a change that did not come from our own undo/redo (map click, drag) ends the redo history
  useEffect(() => {
    if (lastSet.current !== points) setRedo([]);
  }, [points]);

  const apply = (next: Pt[]) => { lastSet.current = next; setPoints(next); };
  const undo = () => {
    if (!points.length) return;
    setRedo((r) => [...r, points[points.length - 1]]);
    apply(points.slice(0, -1));
  };
  const redoOne = () => {
    if (!redo.length) return;
    const p = redo[redo.length - 1];
    setRedo((r) => r.slice(0, -1));
    apply([...points, p]);
  };

  // Ctrl/Cmd+Z = undo corner, Shift+Ctrl/Cmd+Z (or Ctrl+Y) = redo. Ignored while typing in a field.
  const kb = useRef({ undo, redoOne });
  kb.current = { undo, redoOne };
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (!(e.ctrlKey || e.metaKey) || e.altKey || e.defaultPrevented) return;
      const t = e.target as HTMLElement | null;
      if (t && (t.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName))) return;
      if (document.querySelector('[aria-modal="true"]')) return;
      const k = e.key.toLowerCase();
      if (k === "z" && !e.shiftKey) { e.preventDefault(); kb.current.undo(); }
      else if ((k === "z" && e.shiftKey) || k === "y") { e.preventDefault(); kb.current.redoOne(); }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  const roadN = Number(road), heightN = Number(height);
  const roadErr = road.trim() === "" || !Number.isFinite(roadN) || roadN < ROAD_MIN || roadN > ROAD_MAX ? `Between ${ROAD_MIN} and ${ROAD_MAX} m.` : undefined;
  const heightErr = height.trim() === "" || !Number.isFinite(heightN) || heightN < HEIGHT_MIN || heightN > HEIGHT_MAX ? `Between ${HEIGHT_MIN} and ${HEIGHT_MAX} m.` : undefined;
  const nameErr = name.trim() === "" ? "Give the site a name." : undefined;

  const closed = points.length >= 3;
  const crossing = closed && m != null && !m.valid;
  const needMore = Math.max(0, 3 - points.length);
  const blockedWhy = needMore > 0 ? `Add ${needMore} more corner${needMore > 1 ? "s" : ""} on the map.`
    : crossing ? "The boundary crosses itself. Move a corner."
    : nameErr || roadErr || heightErr ? "Fix the highlighted fields." : "";

  const matchesSite = !!site && samePoints(points, site.lnglat as Pt[]);
  const geoApprox = useMemo(() => geodesicApprox(points), [points]);
  const geo = matchesSite ? site!.geodesic_area : geoApprox;
  const shoelace = closed ? (matchesSite ? site!.area : m?.area ?? null) : null;
  const delta = shoelace != null && geo ? ((shoelace - geo) / geo) * 100 : null;
  const roadEdges = new Set(matchesSite ? site!.road_edges : [0]);
  const edgeCount = closed ? points.length : Math.max(0, points.length - 1);

  const submit = async () => {
    if (blockedWhy) return;
    setAnalysing(true);
    const ok = await run("Analysing site", () => rpc("set_site", { lnglat: points, name: name.trim(), locality, road_width: roadN, building_height: heightN }));
    setAnalysing(false);
    if (ok) setStep("climate");
  };

  return (
    <div className="s14">
      <p className="s14-intro">Click the plot corners on the map. Land outside the line counts as neighbour for setbacks and daylight.</p>

      <Panel profile="well" pad="sm" title="Live measure" aria-label="Live measure"
        actions={closed
          ? (crossing ? <Chip led="fail">Crosses itself</Chip> : <Chip led="det">Closed</Chip>)
          : <Chip led="idle">Open</Chip>}>
        <div className="s14-grid2">
          <Readout label="Corners" value={points.length} digits={0} tone="ink" size="sm" />
          <Readout label="Perimeter" value={closed ? m?.perimeter : null} unit="m" tone="det" size="sm" />
          <Readout label="Area · shoelace" value={shoelace} unit="m²" tone="det" size="sm" />
          <Readout label={matchesSite ? "Area · geodesic" : "Area · geodesic ≈"} value={geo} unit="m²" tone="det" size="sm"
            source={matchesSite ? "WGS84 geodesic from the engine" : "Quick WGS84 check in this app. The engine confirms it when you analyse."} />
        </div>
        {closed && <div><ProvenanceTag by="engine" kind="derived" compact source="Perimeter and shoelace area in the local UTM grid; geodesic area on the WGS84 ellipsoid" /></div>}
        {delta != null && (
          <div className="s14-delta" role="status">
            Difference {Math.abs(delta) < 0.005 ? "0.00" : `${delta >= 0 ? "+" : ""}${delta.toFixed(2)}`}%. {Math.abs(delta) <= 0.5 ? "The two methods agree." : "More than 0.5%. Check the corner positions."}
          </div>
        )}
        {state === "error" && (
          <div className="s14-delta" role="status">Could not measure just now. Keep tracing, it will retry on the next change.</div>
        )}
      </Panel>

      <div>
        <h3 className="s14-sec-title">Edges {state === "loading" && <Spinner size={11} label="Measuring" />}</h3>
        <Panel profile="well" pad="none" style={{ marginTop: 8 }} aria-label="Edge lengths">
          {points.length < 2 ? (
            <EmptyState icon={<Icon name="ruler" size={22} />} title={points.length === 0 ? "No corners yet" : "One corner down"}>
              {points.length === 0 ? "Click the map to drop the first corner." : "Click again to see the first edge length."}
            </EmptyState>
          ) : (
            <ul className="s14-edges">
              {Array.from({ length: edgeCount }, (_, i) => {
                const v = m?.edges[i];
                return (
                  <li key={i} className="s14-edge">
                    <span className="s14-edge-id">E{i + 1}</span>
                    <span className="s14-edge-tags">
                      {roadEdges.has(i) && <Chip led="cobalt" title="Setbacks use the road width on this edge">Road</Chip>}
                      {!roadEdges.has(i) && <span className="s14-meta">C{i + 1} to C{((i + 1) % points.length) + 1}</span>}
                    </span>
                    <output className={`lcd det${v == null ? " unknown" : ""}${state === "loading" ? " is-pending" : ""}`} aria-label={`Edge ${i + 1}: ${v == null ? "measuring" : `${fmt(v)} metres`}`}>
                      {v == null ? "–" : fmt(v)}{v != null && <span className="lcd-unit">m</span>}
                    </output>
                  </li>
                );
              })}
            </ul>
          )}
        </Panel>
        <p className="s14-meta" style={{ marginTop: 8 }}>Edge 1 (corners 1 to 2) is the road edge.</p>
      </div>

      <div className="s14-hints" aria-label="Keyboard and mouse hints">
        <span>Click <b>add</b></span>
        <span>Drag <b>move</b></span>
        <span>Double-click <b>remove</b></span>
        <span><Kbd>Ctrl</Kbd><Kbd>Z</Kbd> undo</span>
        <span><Kbd>Shift</Kbd><Kbd>Ctrl</Kbd><Kbd>Z</Kbd> redo</span>
        <span>Tab to a corner, <Kbd>←</Kbd><Kbd>↑</Kbd><Kbd>→</Kbd><Kbd>↓</Kbd> nudge 0.5 m</span>
      </div>

      <div className="s14-actions">
        <LatchButton size="sm" icon={<Icon name="undo" size={14} />} onClick={undo} disabled={!points.length} aria-keyshortcuts="Control+Z">Undo corner</LatchButton>
        <LatchButton size="sm" icon={<Icon name="redo" size={14} />} onClick={redoOne} disabled={!redo.length} aria-keyshortcuts="Control+Shift+Z">Redo</LatchButton>
        <span className="spacer" />
        <LatchButton size="sm" variant="danger" onClick={() => { setRedo([...points].reverse()); apply([]); }} disabled={!points.length}>Clear</LatchButton>
      </div>

      <Panel profile="flat" small pad="sm" title="Site details"
        actions={<ProvenanceTag by="user" kind="observed" compact />}>
        <div className="s14-form">
          <Field label="Site name" value={name} onChange={(e) => setName(e.target.value)} error={nameErr} maxLength={60} />
          <Field label="Locality" value={locality} onChange={(e) => setLocality(e.target.value)} placeholder="e.g. Andheri West" maxLength={80} />
          <Field label="Road width" type="number" inputMode="decimal" unit="m" min={ROAD_MIN} max={ROAD_MAX} step={0.5} value={road}
            onChange={(e) => setRoad(e.target.value)} error={roadErr} />
          <Field label="Building height" type="number" inputMode="decimal" unit="m" min={HEIGHT_MIN} max={HEIGHT_MAX} step={3} value={height}
            onChange={(e) => setHeight(e.target.value)} error={heightErr} />
        </div>
        <p className="s14-meta">
          Setbacks come from the rules file for this road width and height <ProvenanceTag by="engine" kind="derived" compact source="DCPR 2034 setback table" />
        </p>
      </Panel>

      <div className="s14">
        <LatchButton variant="primary" size="lg" block loading={analysing} disabled={!!blockedWhy}
          icon={<Icon name="check" />} onClick={() => void submit()}>
          Close polygon and analyse
        </LatchButton>
        <p className="s14-why" role="status">{blockedWhy || (site ? "This redoes the site and clears the stages after it." : "Sun, wind and setbacks come next.")}</p>
      </div>
    </div>
  );
}
