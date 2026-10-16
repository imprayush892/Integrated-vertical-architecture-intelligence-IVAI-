import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import BubbleCanvas from "./components/BubbleCanvas";
import ClaudeLink from "./components/ClaudeLink";
import Settings, { type AppSettings } from "./components/Settings";
import Inbox from "./components/Inbox";
import MapView from "./components/MapView";
import PlateCanvas from "./components/PlateCanvas";
import ThreeView from "./components/ThreeView";
import { rpc, subscribe } from "./lib/api";
import { roomColor } from "./lib/geom";
import { type Step, useStore, waiting } from "./lib/store";
import type { BridgeStatus, CheckResult, Diagram, GeocodeResult, Plate } from "./lib/types";
import {
  ClimatePanel, DiagramPanel, FindPanel, PlacePanel, ProgramPanel, ReviewPanel, ShapePanel, TracePanel, UnitsPanel,
} from "./panels/Panels";

const STEPS: { id: Step; n: string; label: string; claude?: boolean }[] = [
  { id: "find", n: "1", label: "Find" },
  { id: "trace", n: "2", label: "Trace" },
  { id: "climate", n: "3", label: "Climate" },
  { id: "place", n: "4", label: "Place" },
  { id: "shape", n: "5", label: "Shape" },
  { id: "units", n: "6", label: "Split" },
  { id: "program", n: "6", label: "Program" },
  { id: "diagram", n: "7", label: "Wire", claude: true },
  { id: "review", n: "8", label: "Check", claude: true },
];

export default function App() {
  const s = useStore();
  const { project, step, setStep, tower, unit, busy, toasts, tickets, bridge, inboxOpen } = s;
  const [points, setPoints] = useState<[number, number][]>([]);
  const [fly, setFly] = useState<{ lng: number; lat: number; zoom: number } | null>(null);
  const [preview, setPreview] = useState<Plate | null>(null);
  const [draftParams, setDraftParams] = useState<Record<string, number> | null>(null);
  const [diagram, setDiagram] = useState<Diagram | null>(null);
  const [focus, setFocus] = useState<CheckResult | null>(null);
  const [show3d, setShow3d] = useState(false);
  const [linkOpen, setLinkOpen] = useState(false);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [settings, setSettings] = useState<AppSettings | null>(null);
  useEffect(() => { void rpc<AppSettings>("get_settings").then(setSettings).catch(() => undefined); }, []);
  const [error, setError] = useState<string | null>(null);
  const previewSeq = useRef(0);

  // boot + live events
  useEffect(() => {
    s.refresh().catch((e) => setError(String(e)));
    void rpc<BridgeStatus>("bridge_status").then(s.setBridge).catch(() => undefined);
    let t: ReturnType<typeof setTimeout> | null = null;
    const off = subscribe((ev) => {
      if (ev.kind === "bridge" || ev.kind === "hello") {
        const b = (ev.kind === "hello" ? ev.bridge : ev) as BridgeStatus;
        s.setBridge({ ...b, linked: !!b.linked });
        if (ev.kind === "bridge") s.toast("ok", "Claude linked - the planner's tools are available in Claude.");
      }
      if (ev.kind === "project" || ev.kind === "ticket" || ev.kind === "solved") {
        if (t) clearTimeout(t);
        t = setTimeout(() => void useStore.getState().refresh().catch(() => undefined), 250);
      }
    });
    return off;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // first visit: land on the furthest stage reached
  const landed = useRef(false);
  useEffect(() => {
    if (!project || landed.current) return;
    landed.current = true;
    const st = project.stage;
    setStep(st >= 8 ? "review" : st >= 7 ? "diagram" : st >= 6 ? "units" : st >= 5 ? "shape" : st >= 4 ? "place" : st >= 3 ? "climate" : "find");
    if (project.site) {
      const [lng, lat] = project.site.lnglat[0];
      setFly({ lng, lat, zoom: 18.4 });
      setPoints(project.site.lnglat.map((p) => [p[0], p[1]] as [number, number]));
    }
  }, [project, setStep]);

  const plate = project?.plates.find((p) => p.tower_id === tower) ?? project?.plates[0] ?? null;
  const unitsOfTower = useMemo(() => (project?.units ?? []).filter((u) => (tower === "T1" ? !/^T\d+-/.test(u.id) : u.id.startsWith(`${tower}-`))), [project, tower]);
  const plansOfTower = useMemo(() => {
    const ids = new Set(unitsOfTower.map((u) => u.id));
    const own = (project?.plans ?? []).filter((p) => ids.has(p.unit_id));
    return own.length ? own : (project?.plans ?? []).filter((p) => p.status === "imported");
  }, [project, unitsOfTower]);
  const unitId = unit ?? unitsOfTower[0]?.id ?? null;

  // diagram for the selected flat
  useEffect(() => {
    if (step !== "diagram" || !unitId) return;
    void rpc<Diagram>("get_diagram", { unit_id: unitId }).then(setDiagram).catch(() => setDiagram(null));
  }, [step, unitId, project]);

  const onDrag = useCallback((params: Record<string, number>) => {
    if (!plate) return;
    setDraftParams(params);
    const seq = ++previewSeq.current;
    void rpc<Plate>("preview_plate", { tower_id: plate.tower_id, shape: plate.shape, params: { ...plate.params, ...params } })
      .then((p) => { if (seq === previewSeq.current) setPreview(p); }).catch(() => undefined);
  }, [plate]);
  const onCommit = useCallback(async (params: Record<string, number>) => {
    if (!plate) return;
    await s.run("Updating plate", () => rpc("set_plate", { tower_id: plate.tower_id, params }));
    setPreview(null);
    setDraftParams(null);
  }, [plate, s]);

  const enabled = (id: Step) => {
    switch (id) {
      case "find": case "trace": return true;
      case "climate": case "place": return !!project?.site;
      case "shape": return !!project?.plates.length;
      case "units": case "program": case "diagram": return !!project?.units.length;
      case "review": return !!(project?.plans.length || project?.checks);
    }
  };
  const done = (id: Step) => {
    const st = project?.stage ?? 1;
    return ({ find: 1, trace: 2, climate: 3, place: 4, shape: 5, units: 6, program: 6, diagram: 7, review: 8 } as const)[id] < st;
  };
  const onFlyTo = (r: GeocodeResult) => setFly({ lng: r.lng, lat: r.lat, zoom: r.zoom || 18.4 });

  const mapStage = ["find", "trace", "climate", "place"].includes(step);
  const fresh = bridge.linked && (bridge.fresh ?? true);
  const waitingN = waiting(tickets).length;

  if (error) {
    return <div className="empty"><div><h2>The planner engine is not running</h2><p className="muted">{error}</p>
      <p className="small">Start it with <span className="mono">planner serve</span>, or reopen the desktop app.</p>
      <button className="btn primary" onClick={() => window.location.reload()}>Retry</button></div></div>;
  }

  return (
    <div className="app">
      <header className="topbar">
        <div className="brand"><span className="brand-mark">⌂</span><span>Apartment Planner</span></div>
        <input className="project-name" defaultValue={project?.name} key={project?.id}
          onBlur={(e) => e.target.value !== project?.name && void s.run("Renaming", () => rpc("rename", { name: e.target.value }))} aria-label="Project name" />
        <span className="badge grey">Stage {project?.stage ?? 1}</span>
        <span className="spacer" />
        <button className="btn small ghost" title="Undo the last change" onClick={() => s.run("Undo", () => rpc("undo"))}>↶ Undo</button>
        <button className={`btn small ${waitingN ? "amber" : ""}`} onClick={() => s.setInbox(!inboxOpen)}>
          Decisions {waitingN ? `· ${waitingN}` : ""}
        </button>
        <button className="btn small ghost" onClick={() => setSettingsOpen(true)} title="Settings" aria-label="Settings">⚙ Settings</button>
        <button className="btn small" onClick={() => setLinkOpen(true)} title="Link Claude">
          <span className="dot" style={{ background: fresh ? "var(--teal)" : "var(--muted)" }} /> {fresh ? "Claude linked" : "Claude not linked"}
        </button>
      </header>
      <div className="body">
        <nav className="rail" aria-label="Steps">
          {STEPS.map((st) => (
            <button key={st.id} className={`${step === st.id ? "active" : ""} ${done(st.id) ? "done" : ""} ${st.claude ? "claude" : ""}`}
              disabled={!enabled(st.id)} onClick={() => setStep(st.id)}>
              <span className="n">{st.n}</span>{st.label}
            </button>
          ))}
        </nav>
        <main className="stage">
          {mapStage && settings && (
            <MapView key={`${settings.imagery}:${settings.mapbox_token}`} imagery={{ provider: settings.imagery, token: settings.mapbox_token }}
              tracing={step === "trace"} points={points} onPoints={setPoints} flyTo={fly} />
          )}
          {!mapStage && plate && (step === "shape" || step === "units" || step === "program") && (
            <PlateCanvas plate={preview ?? plate} mode={step === "shape" ? "shape" : "units"} units={step === "shape" && preview ? [] : unitsOfTower}
              selectedUnit={unitId} onSelectUnit={(id) => s.setUnit(id)} onDrag={onDrag} onCommit={onCommit} />
          )}
          {step === "diagram" && diagram && (
            <BubbleCanvas diagram={diagram} onChange={async (d) => {
              const r = await rpc<{ conflicts: Diagram["conflicts"]; message: string }>("update_diagram", { unit_id: diagram.unit_id, rooms: d.rooms, links: d.links })
                .catch((e) => { s.toast("error", String(e)); return null; });
              if (r) setDiagram({ ...diagram, rooms: d.rooms, links: d.links, conflicts: r.conflicts, message: r.message });
            }} />
          )}
          {step === "review" && plate && !show3d && (
            <>
              <PlateCanvas plate={plate} mode="review" plans={plansOfTower} focus={focus?.bbox ?? null} />
              <div className="canvas-legend">
                {["living", "dining", "kitchen", "master", "bedroom", "toilet", "foyer", "passage", "balcony"].map((t) => (
                  <span key={t}><i style={{ background: roomColor(t) }} />{t}</span>
                ))}
                <span><i style={{ background: "var(--wall)" }} />wall</span><span><i style={{ background: "#8b95a5" }} />core / RCC</span>
                <span><i style={{ background: "#8fd0f6" }} />shaft</span><span><i style={{ background: "#4aa8e0" }} />window</span>
                <span><i style={{ background: "#c0392b" }} />door</span>
              </div>
              {focus && <div className="canvas-tools"><button className="btn small" onClick={() => setFocus(null)}>Show whole floor</button></div>}
            </>
          )}
          {step === "review" && plate && show3d && (
            <ThreeView plate={plate} plans={plansOfTower} floors={Math.max(1, Math.round((project?.site?.building_height ?? 24) / 3))} />
          )}
          {step === "review" && !plate && (project?.plans.length ?? 0) === 0 && <div className="empty">Import a DXF drawing to check it.</div>}
          {busy && <div className="busy"><span className="spin" />{busy}…</div>}
          {inboxOpen && <Inbox />}
        </main>
        <aside className="side">
          {step === "find" && <FindPanel onFly={onFlyTo} onStartTrace={() => setStep("trace")} />}
          {step === "trace" && <TracePanel points={points} setPoints={setPoints} />}
          {step === "climate" && <ClimatePanel />}
          {step === "place" && <PlacePanel />}
          {step === "shape" && <ShapePanel draft={draftParams} />}
          {step === "units" && <UnitsPanel />}
          {step === "program" && <ProgramPanel />}
          {step === "diagram" && <DiagramPanel diagram={diagram} onSolved={() => setStep("review")} />}
          {step === "review" && <ReviewPanel onFocus={setFocus} show3d={show3d} setShow3d={setShow3d} />}
        </aside>
      </div>
      {linkOpen && <ClaudeLink onClose={() => setLinkOpen(false)} />}
      {settingsOpen && <Settings onClose={() => setSettingsOpen(false)} onSaved={setSettings} />}
      <div className="toasts" aria-live="polite">
        {toasts.map((t) => <div key={t.id} className={`toast ${t.kind}`} onClick={() => s.dismiss(t.id)}>{t.text}</div>)}
      </div>
    </div>
  );
}
