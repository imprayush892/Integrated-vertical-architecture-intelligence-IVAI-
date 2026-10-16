import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { rpc } from "../lib/api";
import { fmt } from "../lib/geom";
import { useStore } from "../lib/store";
import type { Plate, Programme, Pt, Ring, Unit } from "../lib/types";
import { Chip, EmptyState, Icon, Led, LatchButton, Panel, ProvenanceTag, Readout, Select, Skeleton } from "../ui";

// ------------------------------------------------------------------------------------------- 6 Units
/** The engine picks presets on carpet = 0.80 x the flat's gross area (units.py CARPET_RATIO). */
const CARPET_RATIO = 0.8;
const MAX_FLATS = 16;

const FLAG_TEXT: Record<string, string> = {
  no_core_access: "No door to the core",
  outside_outline: "Sticks out of the plate",
  small: "Too small to plan",
};

const CSS = `
.unt-root { display: flex; flex-direction: column; gap: 14px; min-width: 0; }
.unt-root h2 { margin: 0; }
.unt-lede { font-size: 12.5px; color: var(--ink-2); line-height: 1.5; margin: 0; }
.unt-stepper { display: flex; align-items: center; gap: 10px; }
.unt-stepper-label { flex: 1; min-width: 0; display: flex; flex-direction: column; gap: 2px; }
.unt-stepper-label b { font-size: 13px; }
.unt-stepper-label span { font: 500 11px var(--f-mono); color: var(--ink-3); }
.unt-sum { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 8px; }
.unt-list { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 12px; }
.unt-row { display: flex; flex-direction: column; gap: 10px; padding: 12px; border-radius: var(--r-md); border: 1px solid var(--chamfer); background: var(--neo-convex-bg); box-shadow: var(--neo-raised-sm); transition: box-shadow var(--dur-2) var(--ease), background var(--dur-2); }
.unt-row.is-sel { background: var(--neo-bg); box-shadow: var(--neo-inset), 0 0 0 2px var(--cobalt); border-color: var(--chamfer-inner); }
.unt-row.is-bad { box-shadow: var(--neo-raised-sm), 0 0 0 1.5px var(--fail); }
.unt-row.is-sel.is-bad { box-shadow: var(--neo-inset), 0 0 0 2px var(--cobalt); }
.unt-main { appearance: none; background: transparent; border: 0; padding: 0; margin: 0; text-align: left; cursor: pointer; display: flex; align-items: center; gap: 8px; color: var(--ink-1); width: 100%; border-radius: 6px; }
.unt-id { font: 800 15px var(--f-display); letter-spacing: -0.01em; }
.unt-bhk { margin-left: auto; }
.unt-meters { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 8px; }
.unt-flags { display: flex; flex-wrap: wrap; gap: 6px; }
.unt-flag { display: inline-flex; align-items: center; gap: 6px; font: 600 11.5px var(--f-sans); color: var(--fail-ink); padding: 3px 9px; border-radius: var(--r-pill); box-shadow: var(--neo-latched); }
.unt-ok { display: inline-flex; align-items: center; gap: 6px; font: 600 11.5px var(--f-mono); color: var(--ink-2); }
.unt-hint { font-size: 12px; color: var(--ink-3); line-height: 1.4; margin: 0; }
.unt-err { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; font-size: 12.5px; color: var(--fail-ink); }
.unt-skel { padding: 12px; border-radius: var(--r-md); box-shadow: var(--neo-latched); display: flex; flex-direction: column; gap: 8px; }
`;

type Pt2 = Pt;
function inRing(p: Pt2, ring: Ring): boolean {
  let inside = false;
  for (let i = 0, j = ring.length - 1; i < ring.length; j = i++) {
    const [xi, yi] = ring[i], [xj, yj] = ring[j];
    if (yi > p[1] !== yj > p[1] && p[0] < ((xj - xi) * (p[1] - yi)) / (yj - yi) + xi) inside = !inside;
  }
  return inside;
}
/** Even-odd containment over a polygon-with-holes list (first ring is the shell). */
const inRings = (p: Pt2, parts: Ring[][]) => parts.some((rings) => rings.reduce((n, r) => n + (inRing(p, r) ? 1 : 0), 0) % 2 === 1);

/** Length of a flat's boundary that faces outside or an air shaft. Same rule as the engine's `facade_length`. */
export function frontageOf(plate: Plate, unit: Unit): number {
  const shafts = plate.zones.filter((z) => z.kind === "shaft").flatMap((z) => z.polygon);
  const ring = unit.polygon;
  let total = 0;
  for (let i = 0; i < ring.length; i++) {
    const a = ring[i], b = ring[(i + 1) % ring.length];
    const L = Math.hypot(b[0] - a[0], b[1] - a[1]);
    if (L < 1e-6) continue;
    const probe: Pt2 = [(a[0] + b[0]) / 2 + ((b[1] - a[1]) / L) * 0.2, (a[1] + b[1]) / 2 - ((b[0] - a[0]) / L) * 0.2];
    if (!inRing(probe, plate.outline) || (shafts.length > 0 && inRings(probe, shafts))) total += L;
  }
  return Math.round(total * 100) / 100;
}

export function UnitsPanel() {
  const project = useStore((s) => s.project);
  const tower = useStore((s) => s.tower);
  const unit = useStore((s) => s.unit);
  const setUnit = useStore((s) => s.setUnit);
  const run = useStore((s) => s.run);
  const setStep = useStore((s) => s.setStep);
  const busy = useStore((s) => s.busy);
  const [presets, setPresets] = useState<Programme[]>([]);
  const [presetState, setPresetState] = useState<"loading" | "ready" | "error">("loading");
  const [said, setSaid] = useState("");
  const rowRefs = useRef(new Map<string, HTMLLIElement>());

  const loadPresets = useCallback(() => {
    setPresetState("loading");
    rpc<Programme[]>("list_presets").then((p) => { setPresets(p); setPresetState("ready"); }).catch(() => setPresetState("error"));
  }, []);
  useEffect(() => { loadPresets(); }, [loadPresets]);

  const plate = project?.plates.find((p) => p.tower_id === tower);
  const units = useMemo(() => (project?.units ?? []).filter((u) => (tower === "T1" ? !/^T\d+-/.test(u.id) : u.id.startsWith(`${tower}-`))), [project, tower]);
  const planOf = (id: string) => project?.plans.find((p) => p.unit_id === id);

  // keep the selected row in view when the plan canvas selects a flat
  useEffect(() => {
    if (unit) rowRefs.current.get(unit)?.scrollIntoView({ block: "nearest" });
  }, [unit]);

  if (!plate) {
    return (
      <div className="unt-root">
        <style>{CSS}</style>
        <h2>Split into flats</h2>
        <EmptyState icon={<Icon name="layers" size={24} />} title="No floor plate yet"
          action={<LatchButton variant="primary" onClick={() => setStep("shape")}>Shape the floor</LatchButton>}>
          Shape a plate first. The flats are cut from its flats zone.
        </EmptyState>
      </div>
    );
  }

  const n = Math.round(plate.params.units);
  const isBusy = busy !== null;
  const split = (count: number) => {
    void run("Splitting", () => rpc("split_units", { tower_id: tower, count })).then((out) => {
      if (out !== undefined) setSaid(`${count} flats on this floor.`);
    });
  };
  const flaggedN = units.filter((u) => u.flags.length > 0).length;
  const avg = units.length ? units.reduce((a, u) => a + u.area, 0) / units.length : null;
  const selected = unit ?? units[0]?.id ?? null;

  return (
    <div className="unt-root">
      <style>{CSS}</style>
      <h2>Split into flats</h2>

      <Panel profile="well" pad="sm" title="Flats on this floor"
        actions={<ProvenanceTag by="engine" kind="derived" source="The flats zone is divided by rules. Every flat is linked to the core it opens off." compact />}>
        <div className="unt-stepper">
          <LatchButton size="sm" aria-label="One flat fewer" disabled={n <= 1 || isBusy} onClick={() => split(n - 1)} icon={<Icon name="minus" size={14} />} />
          <Readout label="Count" value={n} digits={0} tone="det" size="lg" />
          <LatchButton size="sm" aria-label="One flat more" disabled={n >= MAX_FLATS || isBusy} onClick={() => split(n + 1)} icon={<Icon name="plus" size={14} />} />
          <div className="unt-stepper-label"><b>{n === 1 ? "1 flat" : `${n} flats`}</b><span>1 to {MAX_FLATS}</span></div>
        </div>
        <div className="unt-sum">
          <Readout label="Flats zone" value={plate.zones.find((z) => z.kind === "flats")?.area} unit="m²" tone="det" size="sm" />
          <Readout label="Average" value={avg} unit="m²" tone="ink" size="sm" />
          <Readout label="Flagged" value={flaggedN} digits={0} tone={flaggedN ? "fail" : "ink"} size="sm" />
        </div>
        <p className="unt-hint">A flat without a door to the core, or outside the plate, is flagged. It is never deleted.</p>
      </Panel>

      <Panel profile="flat" pad="sm" small title="Flats" actions={units.length > 0 ? <Chip led="cobalt" inset>{units.length}</Chip> : undefined}>
        {presetState === "error" && (
          <div className="unt-err" role="alert">
            <span>Could not load the room-list presets.</span>
            <LatchButton size="sm" onClick={loadPresets}>Try again</LatchButton>
          </div>
        )}
        {units.length === 0 ? (
          <EmptyState icon={<Icon name="grid" size={24} />} title="No flats yet"
            action={<LatchButton variant="det" loading={isBusy} disabled={isBusy} onClick={() => split(Math.max(1, n))}>Split into {Math.max(1, n)} flats</LatchButton>}>
            The plate is shaped but not split. Run the split to get flats.
          </EmptyState>
        ) : (
          <ul className="unt-list" aria-label="Flats on this floor">
            {units.map((u) => {
              const sel = selected === u.id;
              const front = frontageOf(plate, u);
              const plan = planOf(u.id);
              const carpet = plan ? plan.carpet_area : u.area * CARPET_RATIO;
              const bhk = u.programme?.bhk;
              const bad = u.flags.length > 0;
              const range = u.programme?.area_range;
              const outOfRange = !!range && (carpet < range[0] || carpet > range[1]);
              return (
                <li key={u.id} ref={(el) => { if (el) rowRefs.current.set(u.id, el); else rowRefs.current.delete(u.id); }}
                  className={`unt-row${sel ? " is-sel" : ""}${bad ? " is-bad" : ""}`}>
                  <button type="button" className="unt-main" aria-pressed={sel} onClick={() => setUnit(u.id)}
                    aria-label={`Flat ${u.id}${bhk ? `, ${bhk} BHK` : ""}, ${fmt(u.area)} square metres${bad ? ", flagged" : ""}. ${sel ? "Selected" : "Select"}`}>
                    <Led status={bad ? "fail" : sel ? "cobalt" : "det"} />
                    <span className="unt-id">{u.id}</span>
                    <span className="unt-bhk"><Chip inset>{bhk ? `${bhk} BHK` : "No list"}</Chip></span>
                  </button>
                  <div className="unt-meters">
                    <Readout label="Flat" value={u.area} unit="m²" tone="det" size="sm" />
                    <Readout label={plan ? "Carpet" : "Carpet est."} value={carpet} unit="m²" tone={plan ? "det" : "ink"} size="sm" />
                    <Readout label="Frontage" value={front} unit="m" tone="det" size="sm" />
                  </div>
                  <div className="unt-flags">
                    {bad ? u.flags.map((f) => <span key={f} className="unt-flag"><Led status="fail" />{FLAG_TEXT[f] ?? f.replace(/_/g, " ")}</span>)
                      : <span className="unt-ok"><Led status={u.core_link ? "det" : "idle"} />{u.core_link ? "Door to core" : "Core link unknown"}</span>}
                  </div>
                  {presetState === "loading" ? <Skeleton height={34} radius={12} /> : (
                    <Select compact label="Room list" value={u.programme?.id ?? ""} disabled={isBusy || presetState === "error"}
                      onChange={(e) => { const v = e.target.value; if (v) void run("Setting room list", () => rpc("set_programme", { unit_id: u.id, preset: v })); }}
                      hint={u.programme ? undefined : "Pick a preset to continue."}>
                      {!u.programme && <option value="">Choose a preset</option>}
                      {presets.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
                      {u.programme && !presets.some((p) => p.id === u.programme!.id) && <option value={u.programme.id}>{u.programme.name}</option>}
                    </Select>
                  )}
                  {u.programme && outOfRange && range && (
                    <p className="unt-hint">Carpet {fmt(carpet)} m² is outside this list's {range[0]}–{range[1]} m² range. Pick another, or keep it and check in stage 8.</p>
                  )}
                </li>
              );
            })}
          </ul>
        )}
        {units.length > 0 && <p className="unt-hint">Carpet is estimated at {Math.round(CARPET_RATIO * 100)}% of the flat until the room solver draws it.</p>}
      </Panel>

      <LatchButton block variant="primary" size="lg" disabled={units.length === 0 || isBusy} onClick={() => setStep("program")}>
        Pick room lists <Icon name="chevron-right" size={14} />
      </LatchButton>
      <span className="sr-only" role="status" aria-live="polite">{said}</span>
    </div>
  );
}
