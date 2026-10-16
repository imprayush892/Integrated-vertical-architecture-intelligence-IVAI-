import { useCallback, useEffect, useRef, useState } from "react";
import { rpc, uploadDrawing } from "../lib/api";
import { fmt } from "../lib/geom";
import { useStore } from "../lib/store";
import type { Plate, Project } from "../lib/types";
import { zoneCss, type ZoneKind } from "../components/PlateCanvas";
import { Channel, Chip, EmptyState, Field, Icon, Led, LatchButton, Panel, ProvenanceTag, Readout, Select, Skeleton } from "../ui";

// ------------------------------------------------------------------------------------------- 5 Shape
export interface ShapeInfo { shape: string; label: string; ratings: Record<string, number>; efficiency: number; area: number; corners: number; outline: [number, number][] }

const CSS = `
.shp-root { display: flex; flex-direction: column; gap: 14px; min-width: 0; --zone-flats: ${zoneCss("flats")}; --zone-core: ${zoneCss("core")}; --zone-passage: ${zoneCss("passage")}; --zone-shaft: ${zoneCss("shaft")}; }
.shp-head { display: flex; align-items: center; gap: 10px; justify-content: space-between; }
.shp-head h2 { margin: 0; }
.shp-lede { font-size: 12.5px; color: var(--ink-2); line-height: 1.5; margin: 0; }
.shp-keys { display: grid; grid-template-columns: repeat(auto-fill, minmax(98px, 1fr)); gap: 10px; }
.shp-key {
  appearance: none; display: flex; flex-direction: column; gap: 7px; text-align: left; min-width: 0;
  padding: 8px 9px 9px; border-radius: var(--r-md); color: var(--ink-1); cursor: pointer;
  background: var(--neo-convex-bg); border: 1px solid var(--chamfer); box-shadow: var(--neo-raised-sm);
  transition: transform var(--dur-1) var(--ease), box-shadow var(--dur-2) var(--ease), background var(--dur-2);
}
.shp-key:hover:not(:disabled):not([aria-pressed="true"]) { transform: translateY(-1px); }
.shp-key[aria-pressed="true"] { background: var(--neo-bg); box-shadow: var(--neo-inset); border-color: var(--chamfer-inner); transform: none; }
.shp-key:disabled, .shp-key[aria-disabled="true"] { cursor: not-allowed; }
.shp-key:disabled { color: var(--ink-disabled); }
.shp-key-top { display: flex; align-items: center; gap: 6px; min-height: 14px; }
.shp-key-name { font: 700 12px/1.2 var(--f-display); letter-spacing: -0.01em; overflow-wrap: anywhere; }
.shp-key-screen { background: var(--neo-lcd-bg); box-shadow: var(--neo-latched); border-radius: 8px; height: 46px; display: grid; place-items: center; padding: 4px; }
.shp-key-screen svg { width: 100%; height: 100%; display: block; }
.shp-key-eff { font: 700 10.5px var(--f-mono); color: var(--ink-2); display: flex; justify-content: space-between; gap: 6px; }
.shp-key[aria-pressed="true"] .shp-key-eff { color: var(--det-ink); }
.shp-meters { display: flex; flex-direction: column; gap: 4px; }
.shp-meter { display: grid; grid-template-columns: 40px 1fr; align-items: center; gap: 6px; }
.shp-meter-l { font: 600 9.5px var(--f-mono); letter-spacing: 0.06em; color: var(--ink-3); }
.shp-pips { display: flex; gap: 2px; }
.shp-pip { flex: 1; height: 7px; border-radius: 2px; background: var(--led-off); opacity: 0.55; box-shadow: inset 0 1px 1px rgba(0,0,0,.25); }
.shp-pip.on { background: var(--det); opacity: 1; box-shadow: 0 0 6px var(--det-glow), inset 0 1px 1px rgba(255,255,255,.5); }
.shp-key-skel { padding: 10px; border-radius: var(--r-md); box-shadow: var(--neo-latched); display: flex; flex-direction: column; gap: 8px; }
.shp-note { font-size: 12px; color: var(--ink-3); line-height: 1.45; }
.shp-err { display: flex; flex-direction: column; gap: 10px; align-items: flex-start; font-size: 13px; color: var(--ink-1); }
.shp-err b { color: var(--fail-ink); }
.shp-zonebar { display: flex; gap: 2px; height: 16px; padding: 2px; border-radius: var(--r-pill); box-shadow: var(--neo-latched); background: var(--neo-bg); }
.shp-zonebar > span { min-width: 3px; border-radius: var(--r-pill); box-shadow: inset 0 0 0 1px color-mix(in srgb, var(--ink-3) 45%, transparent); }
.shp-zones { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px 12px; }
.shp-zones .ui-readout-label::before { content: ""; display: inline-block; width: 9px; height: 9px; border-radius: 3px; margin-right: 6px; vertical-align: -1px; background: var(--sw, transparent); box-shadow: inset 0 0 0 1px color-mix(in srgb, var(--ink-3) 50%, transparent); }
.shp-z-flats { --sw: var(--zone-flats); } .shp-z-core { --sw: var(--zone-core); } .shp-z-passage { --sw: var(--zone-passage); } .shp-z-shaft { --sw: var(--zone-shaft); }
.shp-sum { display: flex; align-items: center; gap: 8px; font: 600 11.5px var(--f-mono); color: var(--ink-2); }
.shp-sum.bad { color: var(--fail-ink); }
.shp-chan { display: flex; flex-direction: column; gap: 10px; }
.shp-chan .ui-channel { grid-template-columns: minmax(0, 1fr) auto; row-gap: 6px; }
.shp-chan .ui-channel-range { grid-column: 1 / -1; grid-row: 2; }
.shp-group { font: 600 10.5px var(--f-mono); letter-spacing: 0.12em; text-transform: uppercase; color: var(--ink-3); margin: 4px 2px 0; }
.shp-exact summary { cursor: pointer; font: 600 12px var(--f-sans); color: var(--ink-2); padding: 4px 2px; border-radius: 6px; }
.shp-exact summary:hover { color: var(--ink-1); }
.shp-exact-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px; margin-top: 10px; }
.shp-tools { display: flex; flex-direction: column; gap: 8px; }
`;

const PARAM_LABEL: Record<string, string> = {
  W: "Plate width", D: "Plate depth", core_w: "Core width", core_d: "Core depth", passage: "Passage width",
  shaft_size: "Shaft size", end_depth: "End-flat depth", arm_depth: "Arm depth", wing_width: "Wing width",
  bar_width: "Bar width", bridge_depth: "Bridge depth", bar_depth: "Bar depth", stem_width: "Stem width",
  corner_cut: "Corner cut", unit_depth: "Flat depth", shafts: "Air shafts",
};
const PLATE_KEYS = ["W", "D", "arm_depth", "wing_width", "bar_width", "bridge_depth", "bar_depth", "stem_width", "corner_cut", "unit_depth", "end_depth"];
const CORE_KEYS = ["core_w", "core_d", "passage"];
const SHAFT_KEYS = ["shafts", "shaft_size"];
const RATING_ROWS: { key: string; short: string; long: string }[] = [
  { key: "core_efficiency", short: "CORE", long: "Core efficiency" },
  { key: "daylight", short: "DAY", long: "Daylight" },
  { key: "structure", short: "STRUCT", long: "Structure" },
];

/** Slider range for a template parameter. Mirrors the engine's `limits()`; the engine still clamps. */
function rangeFor(key: string, params: Record<string, number>, env: { w: number; d: number }): [number, number, number] {
  const W = Math.max(12, env.w), D = Math.max(10, env.d);
  const r: Record<string, [number, number, number]> = {
    W: [12, W, 0.1], D: [10, D, 0.1],
    core_w: [4, Math.max(4, W - 4), 0.1], core_d: [4, Math.max(4, D - 4), 0.1], passage: [1.5, 3, 0.1],
    shafts: [0, 4, 1], shaft_size: [1.2, 6, 0.1], end_depth: [4, 20, 0.1],
    arm_depth: [8, D, 0.1], wing_width: [8, W, 0.1], bar_width: [6, Math.max(6, W / 2), 0.1], bridge_depth: [6, D, 0.1],
    bar_depth: [8, D, 0.1], stem_width: [8, W, 0.1], corner_cut: [0, 12, 0.1], unit_depth: [7, 14, 0.1],
  };
  const known = r[key];
  const cur = Number(params[key]) || 0;
  if (known) return [Math.min(known[0], cur), Math.max(known[1], cur), known[2]];
  return [0, Math.max(10, cur * 2), 0.1];
}

function envOf(project: Project | null, plate: Plate): { w: number; d: number } {
  const pl = project?.placement;
  const opt = pl?.options.find((o) => o.id === pl.chosen);
  const fp = opt?.plates.find((p) => p.id === plate.tower_id);
  if (fp) return { w: Math.max(fp.width, plate.params.W ?? 0), d: Math.max(fp.depth, plate.params.D ?? 0) };
  return { w: (plate.params.W ?? 30) * 1.4, d: (plate.params.D ?? 20) * 1.4 };
}

/** Mini plan of a template outline, drawn in the plate's own proportions. */
function MiniPlan({ outline }: { outline: [number, number][] }) {
  if (outline.length < 3) return null;
  const xs = outline.map((p) => p[0]), ys = outline.map((p) => p[1]);
  const minx = Math.min(...xs), maxx = Math.max(...xs), miny = Math.min(...ys), maxy = Math.max(...ys);
  const pad = Math.max(maxx - minx, maxy - miny) * 0.06;
  return (
    <svg viewBox={`${minx - pad} ${-maxy - pad} ${maxx - minx + 2 * pad} ${maxy - miny + 2 * pad}`} preserveAspectRatio="xMidYMid meet" aria-hidden="true" focusable="false">
      <polygon points={outline.map(([x, y]) => `${x},${-y}`).join(" ")} fill={zoneCss("flats")} stroke="var(--wall)" strokeWidth={1.6} strokeLinejoin="miter" vectorEffect="non-scaling-stroke" />
    </svg>
  );
}

function Meter({ label, short, value }: { label: string; short: string; value: number }) {
  const n = Math.max(0, Math.min(5, Math.round(value || 0)));
  return (
    <div className="shp-meter" role="img" aria-label={`${label}: ${n} of 5`} title={`${label}: ${n} of 5`}>
      <span className="shp-meter-l" aria-hidden="true">{short}</span>
      <span className="shp-pips" aria-hidden="true">{Array.from({ length: 5 }, (_, i) => <span key={i} className={`shp-pip${i < n ? " on" : ""}`} />)}</span>
    </div>
  );
}

/** Number box that commits on Enter or blur (so typing "12.5" does not fire an RPC per key). */
function ExactField({ label, value, step, min, max, unit, onCommit }: {
  label: string; value: number; step: number; min: number; max: number; unit?: string; onCommit: (v: number) => void;
}) {
  const [txt, setTxt] = useState(value.toFixed(step >= 1 ? 0 : 1));
  useEffect(() => { setTxt(value.toFixed(step >= 1 ? 0 : 1)); }, [value, step]);
  const parsed = Number(txt);
  const bad = txt.trim() === "" || Number.isNaN(parsed) || parsed < min - 1e-9 || parsed > max + 1e-9;
  const commit = () => {
    if (bad) { setTxt(value.toFixed(step >= 1 ? 0 : 1)); return; }
    if (Math.abs(parsed - value) > 1e-9) onCommit(parsed);
  };
  return (
    <Field label={label} type="number" inputMode="decimal" step={step} min={min} max={max} unit={unit} value={txt}
      error={bad && txt !== "" ? `${fmt(min, step >= 1 ? 0 : 1)} to ${fmt(max, step >= 1 ? 0 : 1)}` : undefined}
      onChange={(e) => setTxt(e.target.value)} onBlur={commit}
      onKeyDown={(e) => { if (e.key === "Enter") { e.preventDefault(); commit(); } if (e.key === "Escape") setTxt(value.toFixed(step >= 1 ? 0 : 1)); }} />
  );
}

export function ShapePanel({ draft }: { draft: Record<string, number> | null }) {
  const project = useStore((s) => s.project);
  const tower = useStore((s) => s.tower);
  const setTower = useStore((s) => s.setTower);
  const run = useStore((s) => s.run);
  const setStep = useStore((s) => s.setStep);
  const busy = useStore((s) => s.busy);
  const toast = useStore((s) => s.toast);
  const [shapes, setShapes] = useState<ShapeInfo[]>([]);
  const [load, setLoad] = useState<"loading" | "ready" | "error">("loading");
  const [loadErr, setLoadErr] = useState("");
  const [pending, setPending] = useState<string | null>(null);
  const [said, setSaid] = useState("");
  const fileRef = useRef<HTMLInputElement>(null);
  const plate = project?.plates.find((p) => p.tower_id === tower);
  const plateId = plate?.tower_id;

  const fetchShapes = useCallback(() => {
    setLoad("loading");
    rpc<ShapeInfo[]>("list_shapes", { tower_id: tower })
      .then((r) => { setShapes(r); setLoad("ready"); })
      .catch((e) => { setShapes([]); setLoadErr(e instanceof Error ? e.message : String(e)); setLoad("error"); });
  }, [tower]);
  useEffect(() => { if (plateId) fetchShapes(); }, [fetchShapes, plateId]);

  if (!plate) {
    return (
      <div className="shp-root">
        <style>{CSS}</style>
        <div className="shp-head"><h2>Shape the floor</h2></div>
        <EmptyState icon={<Icon name="layers" size={24} />} title="No floor plate yet"
          action={<LatchButton variant="primary" onClick={() => setStep(project?.site ? "place" : "find")}>{project?.site ? "Place the towers" : "Find the plot"}</LatchButton>}>
          Choose a tower placement first. The plate templates are fitted to it.
        </EmptyState>
      </div>
    );
  }

  const zones = Object.fromEntries(plate.zones.map((z) => [z.kind, z.area])) as Partial<Record<ZoneKind, number>>;
  const params = { ...plate.params, ...(draft ?? {}) };
  const env = envOf(project, plate);
  const zoneSum = (zones.flats ?? 0) + (zones.core ?? 0) + (zones.passage ?? 0) + (zones.shaft ?? 0);
  const adds = Math.abs(zoneSum - plate.area) < 0.06;
  const efficiency = plate.area ? ((zones.flats ?? 0) / plate.area) * 100 : null;
  const isBusy = busy !== null || pending !== null;
  const has = (k: string) => k in plate.params;
  const plateKeys = PLATE_KEYS.filter(has), coreKeys = CORE_KEYS.filter(has), shaftKeys = SHAFT_KEYS.filter(has);
  const towers = project?.plates ?? [];

  const setParam = (k: string, v: number) => { void run("Updating plate", () => rpc("set_plate", { tower_id: tower, params: { [k]: v } })); };

  const pickShape = async (s: ShapeInfo) => {
    if (isBusy || plate.shape === s.shape) return;
    setPending(s.shape);
    try {
      const out = await run("Fitting shape", () => rpc("set_plate", { tower_id: tower, shape: s.shape, params: {} }));
      if (out !== undefined) setSaid(`${s.label} fitted. Flats were re-split.`);
    } finally { setPending(null); }
  };

  const renderChannel = (k: string) => {
    const [lo, hi, step] = rangeFor(k, plate.params, env);
    const isInt = step >= 1;
    return (
      <Channel key={k} label={PARAM_LABEL[k] ?? k.replace(/_/g, " ")} tone="det" value={Number(params[k])} min={lo} max={hi} step={step}
        unit={isInt ? undefined : "m"} hint={isInt ? `${lo}–${hi}` : `${fmt(lo, 1)}–${fmt(hi, 1)} m`}
        onChange={() => undefined} onCommit={(v) => setParam(k, v)} />
    );
  };

  return (
    <div className="shp-root">
      <style>{CSS}</style>
      <div className="shp-head">
        <h2>Shape the floor</h2>
        {towers.length > 1 && (
          <Select compact aria-label="Tower" value={tower} onChange={(e) => setTower(e.target.value)} options={towers.map((p) => ({ value: p.tower_id, label: p.tower_id }))} />
        )}
      </div>

      <Panel profile="well" pad="sm" title="Plate template"
        actions={<ProvenanceTag by="engine" kind="derived" source="Ratings come from the template table in the rules files, not from Claude." compact />}>
        <p className="shp-lede">Pick a template. Each key shows the plan and three ratings: core efficiency, daylight and structure.</p>
        {load === "loading" && (
          <div className="shp-keys" role="status" aria-label="Loading plate templates">
            {Array.from({ length: 6 }, (_, i) => (
              <div key={i} className="shp-key-skel"><Skeleton height={12} width="70%" /><Skeleton height={40} radius={8} /><Skeleton height={8} lines={3} /></div>
            ))}
          </div>
        )}
        {load === "error" && (
          <div className="shp-err" role="alert">
            <span><b>Could not load the templates.</b> {loadErr || "The engine did not answer."}</span>
            <LatchButton size="sm" onClick={fetchShapes}>Try again</LatchButton>
          </div>
        )}
        {load === "ready" && shapes.length === 0 && (
          <EmptyState title="No templates fit this placement">Try another tower placement in stage 4.</EmptyState>
        )}
        {load === "ready" && shapes.length > 0 && (
          <div className="shp-keys" role="group" aria-label="Plate templates">
            {shapes.map((s) => {
              const on = plate.shape === s.shape;
              const loading = pending === s.shape;
              return (
                <button key={s.shape} type="button" className="shp-key" aria-pressed={on} aria-busy={loading || undefined}
                  aria-disabled={isBusy && !on ? true : undefined} onClick={() => void pickShape(s)}
                  aria-label={`${s.label}. Core efficiency ${s.ratings.core_efficiency} of 5, daylight ${s.ratings.daylight} of 5, structure ${s.ratings.structure} of 5. ${Math.round(s.efficiency * 100)} percent flats.`}>
                  <span className="shp-key-top"><Led status={on || loading ? "det" : "idle"} pulse={loading} /><span className="shp-key-name">{s.label}</span></span>
                  <span className="shp-key-screen"><MiniPlan outline={s.outline} /></span>
                  <span className="shp-meters">
                    {RATING_ROWS.map((r) => <Meter key={r.key} label={r.long} short={r.short} value={s.ratings[r.key]} />)}
                  </span>
                  <span className="shp-key-eff" aria-hidden="true"><span>FLATS</span><span>{Math.round(s.efficiency * 100)}%</span></span>
                </button>
              );
            })}
          </div>
        )}
        <p className="shp-note">Switching template refits the plate and re-splits the flats. Room lists are kept when the flat count stays the same.</p>
        {plate.imported_from && <Chip led="cobalt" title="The outline comes from your drawing">Outline from {plate.imported_from}</Chip>}
      </Panel>

      <Panel profile="flat" pad="sm" small title="Zones"
        actions={<ProvenanceTag by="engine" kind="derived" source="Zones are cut from the plate by geometry. They always add up to the plate." compact />}>
        <div className="shp-zonebar" role="img" aria-label={`Zone split: flats ${fmt(zones.flats, 0)}, core ${fmt(zones.core, 0)}, passage ${fmt(zones.passage, 0)}, shafts ${fmt(zones.shaft ?? 0, 0)} square metres`}>
          {(["flats", "core", "passage", "shaft"] as ZoneKind[]).map((k) => {
            const a = zones[k] ?? 0;
            return a > 0 ? <span key={k} style={{ flex: `${a} 0 0`, background: `var(--zone-${k})` }} /> : null;
          })}
        </div>
        <div className="shp-zones">
          <Readout className="shp-z-flats" label="Flats" value={zones.flats} unit="m²" tone="det" size="sm" />
          <Readout className="shp-z-core" label="Core" value={zones.core} unit="m²" tone="det" size="sm" />
          <Readout className="shp-z-passage" label="Passage" value={zones.passage} unit="m²" tone="det" size="sm" />
          <Readout className="shp-z-shaft" label="Shafts" value={zones.shaft ?? 0} unit="m²" tone="det" size="sm" />
          <Readout label="Plate" value={plate.area} unit="m²" tone="ink" size="sm" />
          <Readout label="Flats share" value={efficiency} unit="%" digits={0} tone="ink" size="sm" />
          <Readout label="Corners" value={plate.corners} digits={0} tone="ink" size="sm" />
          <Readout label="Bounding box" value={`${fmt(plate.width, 2)} × ${fmt(plate.depth, 2)}`} unit="m" tone="ink" size="sm" />
        </div>
        <div className={`shp-sum${adds ? "" : " bad"}`} role="status">
          <Led status={adds ? "det" : "fail"} />
          {adds ? "Zones add up to the plate." : `Zones differ from the plate by ${fmt(Math.abs(zoneSum - plate.area), 2)} m².`}
        </div>
      </Panel>

      <Panel profile="well" pad="sm" title="Parameters"
        actions={draft ? <Chip led="cobalt" title="The plan shows your drag. Release to save.">Previewing</Chip> : <ProvenanceTag by="user" compact source="Values you set. The rules clamp them to what fits." />}>
        <p className="shp-lede">Drag the round handles on the plan, or use these channels. Arrow keys move one step, Page Up and Down move ten.</p>
        <div className="shp-chan">
          {plateKeys.length > 0 && <div className="shp-group">Plate</div>}
          {plateKeys.map(renderChannel)}
          {coreKeys.length > 0 && <div className="shp-group">Core</div>}
          {coreKeys.map(renderChannel)}
          {shaftKeys.length > 0 && <div className="shp-group">Air shafts</div>}
          {shaftKeys.map(renderChannel)}
        </div>
        <details className="shp-exact">
          <summary>Type exact values</summary>
          <div className="shp-exact-grid">
            {[...plateKeys, ...coreKeys, ...shaftKeys].map((k) => {
              const [lo, hi, step] = rangeFor(k, plate.params, env);
              return <ExactField key={k} label={PARAM_LABEL[k] ?? k.replace(/_/g, " ")} value={Number(params[k])} min={lo} max={hi} step={step}
                unit={step >= 1 ? undefined : "m"} onCommit={(v) => setParam(k, v)} />;
            })}
          </div>
        </details>
      </Panel>

      <div className="shp-tools">
        <input ref={fileRef} type="file" accept=".dxf" hidden aria-label="DXF file of the core and flat footprint"
          onChange={async (e) => {
            const f = e.target.files?.[0];
            e.target.value = "";
            if (!f) return;
            setPending("dxf");
            try {
              const out = await run("Reading DXF", () => uploadDrawing(f, "plate", tower));
              if (out !== undefined) toast("ok", `Outline read from ${f.name}.`);
            } finally { setPending(null); }
          }} />
        <LatchButton block icon={<Icon name="upload" size={14} />} loading={pending === "dxf"} disabled={isBusy && pending !== "dxf"}
          onClick={() => fileRef.current?.click()}>Import a DXF of the core and flat footprint</LatchButton>
        <LatchButton block variant="primary" size="lg" disabled={isBusy} onClick={() => setStep("units")}>
          Split into flats <Icon name="chevron-right" size={14} />
        </LatchButton>
      </div>
      <span className="sr-only" role="status" aria-live="polite">{said}</span>
    </div>
  );
}
