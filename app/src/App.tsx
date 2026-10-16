import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import BubbleCanvas from "./components/BubbleCanvas";
import ClaudeLink from "./components/ClaudeLink";
import Settings, { type AppSettings } from "./components/Settings";
import Inbox from "./components/Inbox";
import MapView from "./components/MapView";
import PlateCanvas from "./components/PlateCanvas";
import ThreeView from "./components/ThreeView";
import { ErrorBoundary, FlowsHost } from "./flows";
import { rpc, subscribe } from "./lib/api";
import { roomColor } from "./lib/geom";
import { useStore, waiting } from "./lib/store";
import type { BridgeStatus, CheckResult, Diagram, GeocodeResult, Plate } from "./lib/types";
import {
  ClimatePanel, DiagramPanel, FindPanel, PlacePanel, ProgramPanel, ReviewPanel, ShapePanel, TracePanel, UnitsPanel,
} from "./panels/Panels";
import { EngineOffline } from "./shell/EngineOffline";
import { Inspector } from "./shell/Inspector";
import { type PadInfo, Sequencer } from "./shell/Sequencer";
import { StageDock } from "./shell/StageDock";
import { blockedReason, STAGES, stageOfStep, utmLabel } from "./shell/stages";
import { StatusBar } from "./shell/StatusBar";
import { usePersistedFlag, useTheme } from "./shell/theme";
import { TopBar } from "./shell/TopBar";
import { useEngineHealth } from "./shell/useEngineHealth";
import { EmptyState, Icon, LatchButton, Spinner, ToastViewport } from "./ui";

export default function App() {
  return <ErrorBoundary><AppShell /></ErrorBoundary>;
}

function AppShell() {
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

  // shell state
  const theme = useTheme();
  const health = useEngineHealth();
  const [inspectorOpen, setInspectorOpen] = usePersistedFlag("ivai.inspector", () => typeof window === "undefined" || window.innerWidth >= 1280);

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

  const onFlyTo = (r: GeocodeResult) => setFly({ lng: r.lng, lat: r.lat, zoom: r.zoom || 18.4 });

  const mapStage = ["find", "trace", "climate", "place"].includes(step);
  const fresh = bridge.linked && (bridge.fresh ?? true);
  const waitingList = waiting(tickets);
  const waitingN = waitingList.length;

  // ---- sequencer model (the 8 stages; stage 6 owns "units" + "program")
  const stage = stageOfStep(step);
  const pads: PadInfo[] = STAGES.map((meta) => {
    const blocked = blockedReason(meta.n, project);
    const active = meta.n === stage.n;
    const done = meta.n < (project?.stage ?? 1);
    const w = waitingList.filter((t) => t.stage === meta.n).length;
    const state: PadInfo["state"] = blocked ? "blocked" : w > 0 ? "decision" : active ? "active" : done ? "done" : "ready";
    return { meta, state, waiting: w, blocked, done, active };
  });
  const selectStage = (n: number) => {
    const meta = STAGES[n - 1];
    if (!meta || meta.steps.includes(step)) return;
    setStep(meta.steps[0]);
  };

  const site0 = project?.site?.lnglat[0];
  const crs = site0 ? utmLabel(site0[0], site0[1]) : "WGS84 · EPSG:4326";
  const inspectorShown = inspectorOpen || inboxOpen;
  const toggleInspector = () => {
    if (inspectorShown) { setInspectorOpen(false); if (inboxOpen) s.setInbox(false); }
    else setInspectorOpen(true);
  };
  const canvasKey = theme.resolved; // Konva/three resolve CSS colours once; remount on theme change

  if (error) return <EngineOffline error={error} />;

  return (
    <div className="shell" data-inspector={inspectorShown ? "open" : "closed"}>
      <a className="skip-link" href="#stage-panel">Skip to stage panel</a>
      <TopBar projectName={project?.name} projectKey={project?.id}
        onRename={(name) => void s.run("Renaming", () => rpc("rename", { name }))}
        onUndo={() => void s.run("Undo", () => rpc("undo"))}
        waiting={waitingN} inboxOpen={inboxOpen} onToggleInbox={() => s.setInbox(!inboxOpen)}
        claudeLinked={!!fresh} onOpenLink={() => setLinkOpen(true)} onOpenSettings={() => setSettingsOpen(true)}
        theme={theme.pref} onTheme={theme.setPref} inspectorOpen={inspectorShown} onToggleInspector={toggleInspector} />
      <Sequencer pads={pads} onSelect={selectStage} onBlocked={(r) => s.toast("info", r)} />
      <div className="shell-body">
        <aside id="stage-panel" className="dock dock-left neo-flat" aria-label={`Stage ${stage.n}: ${stage.title}`} tabIndex={-1}>
          <StageDock stage={stage} step={step} onStep={setStep}>
            <div className="side">
              {step === "find" && <FindPanel onFly={onFlyTo} onStartTrace={() => setStep("trace")} />}
              {step === "trace" && <TracePanel points={points} setPoints={setPoints} />}
              {step === "climate" && <ClimatePanel />}
              {step === "place" && <PlacePanel />}
              {step === "shape" && <ShapePanel draft={draftParams} />}
              {step === "units" && <UnitsPanel />}
              {step === "program" && <ProgramPanel />}
              {step === "diagram" && <DiagramPanel diagram={diagram} onSolved={() => setStep("review")} />}
              {step === "review" && <ReviewPanel onFocus={setFocus} show3d={show3d} setShow3d={setShow3d} />}
            </div>
          </StageDock>
        </aside>

        <main className="stage-well neo-well-deep" aria-label="Canvas">
          <div className="stage">
            {mapStage && settings && (
              <MapView key={`${settings.imagery}:${settings.mapbox_token}`} imagery={{ provider: settings.imagery, token: settings.mapbox_token }}
                tracing={step === "trace"} points={points} onPoints={setPoints} flyTo={fly} />
            )}
            {mapStage && !settings && <div className="stage-loading"><Spinner size={18} label="Loading map" /></div>}
            {!mapStage && plate && (step === "shape" || step === "units" || step === "program") && (
              <PlateCanvas key={`plate-${canvasKey}`} plate={preview ?? plate} mode={step === "shape" ? "shape" : "units"} units={step === "shape" && preview ? [] : unitsOfTower}
                selectedUnit={unitId} onSelectUnit={(id) => s.setUnit(id)} onDrag={onDrag} onCommit={onCommit} />
            )}
            {!mapStage && !plate && step !== "diagram" && step !== "review" && (
              <EmptyState fill icon={<Icon name="layers" size={26} />} title="No floor plate yet"
                action={<LatchButton variant="primary" onClick={() => setStep(project?.site ? "place" : "find")}>{project?.site ? "Place the towers" : "Find the plot"}</LatchButton>}>
                Choose a tower placement to get a plate to shape and split.
              </EmptyState>
            )}
            {step === "diagram" && diagram && (
              <BubbleCanvas key={`bubble-${canvasKey}`} diagram={diagram} onChange={async (d) => {
                const r = await rpc<{ conflicts: Diagram["conflicts"]; message: string }>("update_diagram", { unit_id: diagram.unit_id, rooms: d.rooms, links: d.links })
                  .catch((e) => { s.toast("error", String(e)); return null; });
                if (r) setDiagram({ ...diagram, rooms: d.rooms, links: d.links, conflicts: r.conflicts, message: r.message });
              }} />
            )}
            {step === "diagram" && !diagram && (
              <EmptyState fill icon={<Icon name="grid" size={26} />} title={unitId ? "Loading the room diagram" : "No flats yet"}>
                {unitId ? "Reading the room graph for this flat…" : "Split the plate into flats first."}
              </EmptyState>
            )}
            {step === "review" && plate && !show3d && (
              <>
                <PlateCanvas key={`review-${canvasKey}`} plate={plate} mode="review" plans={plansOfTower} focus={focus?.bbox ?? null} />
                <div className="canvas-legend" aria-label="Plan legend">
                  {["living", "dining", "kitchen", "master", "bedroom", "toilet", "foyer", "passage", "balcony"].map((t) => (
                    <span key={t}><i style={{ background: roomColor(t) }} />{t}</span>
                  ))}
                  <span><i style={{ background: "var(--wall)" }} />wall</span><span><i style={{ background: "#8b95a5" }} />core / RCC</span>
                  <span><i style={{ background: "#8fd0f6" }} />shaft</span><span><i style={{ background: "#4aa8e0" }} />window</span>
                  <span><i style={{ background: "#c0392b" }} />door</span>
                </div>
                {focus && <div className="canvas-tools"><LatchButton size="sm" onClick={() => setFocus(null)}>Show whole floor</LatchButton></div>}
              </>
            )}
            {step === "review" && plate && show3d && (
              <ThreeView key={`three-${canvasKey}`} plate={plate} plans={plansOfTower} floors={Math.max(1, Math.round((project?.site?.building_height ?? 24) / 3))} />
            )}
            {step === "review" && !plate && (project?.plans.length ?? 0) === 0 && (
              <EmptyState fill icon={<Icon name="upload" size={26} />} title="Nothing to check yet">
                Import a DXF drawing to check it, or generate flats in stage 7.
              </EmptyState>
            )}
            {busy && <div className="busy" role="status"><Spinner />{busy}…</div>}
          </div>
        </main>

        {inspectorShown && (
          <aside className="dock dock-right neo-flat" aria-label={inboxOpen ? "Decision inbox" : "Inspector"}>
            {inboxOpen ? <Inbox /> : (
              <Inspector stage={stage} project={project} summary={s.summary} waiting={waitingList} claudeLinked={!!fresh}
                onOpenInbox={() => s.setInbox(true)} onClose={() => setInspectorOpen(false)} />
            )}
          </aside>
        )}
      </div>
      <StatusBar engine={health.state} engineVersion={health.version ?? (s.summary?.engine as string | undefined) ?? null} busy={busy} crs={crs} />
      {linkOpen && <ClaudeLink onClose={() => setLinkOpen(false)} />}
      {settingsOpen && <Settings onClose={() => setSettingsOpen(false)} onSaved={setSettings} />}
      <ToastViewport toasts={toasts} onDismiss={s.dismiss} />
      <FlowsHost theme={theme} inspectorOpen={inspectorShown} onToggleInspector={toggleInspector}
        onOpenLink={() => setLinkOpen(true)} onOpenSettings={() => setSettingsOpen(true)} />
    </div>
  );
}

