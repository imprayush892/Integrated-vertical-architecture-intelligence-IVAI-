import { useEffect, useMemo, useState } from "react";
import { dxfUrl, rpc, uploadDrawing } from "../lib/api";
import { fmt, pct } from "../lib/geom";
import { useStore } from "../lib/store";
import type { CheckResult, Diagram, GeocodeResult, PlacementOption, Programme } from "../lib/types";
import { SUN_COLORS, SunPath, WindRoseChart } from "../components/SunWind";

const Kpi = ({ label, value }: { label: string; value: string }) => (
  <div className="kpi"><span>{label}</span><b>{value}</b></div>
);

// ------------------------------------------------------------------------------------------- 1 Find
export function FindPanel({ onFly, onStartTrace }: { onFly: (r: GeocodeResult) => void; onStartTrace: () => void }) {
  const [q, setQ] = useState("");
  const [res, setRes] = useState<GeocodeResult[]>([]);
  const toast = useStore((s) => s.toast);
  const search = async () => {
    if (!q.trim()) return;
    try {
      const r = await rpc<GeocodeResult[]>("search_place", { query: q });
      setRes(r);
      if (r[0]) onFly(r[0]);
      else toast("info", "No place found. Try a locality name.");
    } catch (e) { toast("error", String(e)); }
  };
  return (
    <div className="col">
      <h2>1 · Find the plot</h2>
      <p className="muted small">Search a place. The camera flies there. Then trace the plot on the satellite map.</p>
      <div className="row">
        <input className="field" style={{ flex: 1, border: "1px solid var(--line)", borderRadius: 8, padding: "8px 10px" }}
          placeholder="e.g. Lokhandwala" value={q} onChange={(e) => setQ(e.target.value)} onKeyDown={(e) => e.key === "Enter" && search()} />
        <button className="btn primary" onClick={search}>Search</button>
      </div>
      {res.length > 0 && (
        <div className="col">
          {res.map((r) => (
            <button key={`${r.lat},${r.lng}`} className="option" onClick={() => onFly(r)}>
              <b>{r.name}</b><div className="small muted">{r.display_name}</div>
              <div className="mono muted">{r.lat.toFixed(6)}, {r.lng.toFixed(6)} · {r.provider}</div>
            </button>
          ))}
        </div>
      )}
      <button className="btn block" onClick={onStartTrace}>Trace the boundary →</button>
      <ProjectBox />
    </div>
  );
}

function ProjectBox() {
  const run = useStore((s) => s.run);
  const summary = useStore((s) => s.summary);
  const [path, setPath] = useState("");
  return (
    <div className="card soft col">
      <h3>Project file</h3>
      <div className="mono small muted" style={{ wordBreak: "break-all" }}>{String(summary?.file ?? "")}</div>
      <div className="row">
        <input className="field" style={{ flex: 1, border: "1px solid var(--line)", borderRadius: 7, padding: "5px 8px" }}
          placeholder="/path/to/project.aptplan" value={path} onChange={(e) => setPath(e.target.value)} />
      </div>
      <div className="row">
        <button className="btn small" disabled={!path} onClick={() => run("Opening", () => rpc("open_project", { path }))}>Open</button>
        <button className="btn small" disabled={!path} onClick={() => run("Creating", () => rpc("create_project", { path, name: path.split(/[\\/]/).pop()?.replace(".aptplan", "") }))}>New</button>
      </div>
    </div>
  );
}

// ------------------------------------------------------------------------------------------- 2 Trace
export function TracePanel({ points, setPoints }: { points: [number, number][]; setPoints: (p: [number, number][]) => void }) {
  const run = useStore((s) => s.run);
  const site = useStore((s) => s.project?.site);
  const setStep = useStore((s) => s.setStep);
  const [road, setRoad] = useState(12);
  const [height, setHeight] = useState(24);
  const [name, setName] = useState("SITE 01");
  const [locality, setLocality] = useState("");
  useEffect(() => {
    if (site) { setRoad(site.road_width); setHeight(site.building_height); setName(site.name); setLocality(site.locality); }
  }, [site]);
  return (
    <div className="col">
      <h2>2 · Trace the boundary</h2>
      <p className="muted small">Click the plot corners on the map. Drag a corner to fix it, double-click to remove. Edge lengths show live.
        Land outside the line is treated as neighbour for setbacks and daylight.</p>
      <div className="grid2">
        <Kpi label="Corners" value={String(points.length)} />
        <Kpi label="Site" value={site ? `${fmt(site.area)} m²` : "–"} />
      </div>
      <div className="grid2">
        <label className="field">Site name<input value={name} onChange={(e) => setName(e.target.value)} /></label>
        <label className="field">Locality<input value={locality} onChange={(e) => setLocality(e.target.value)} /></label>
        <label className="field">Road width (m)<input type="number" min={6} max={60} step={0.5} value={road} onChange={(e) => setRoad(+e.target.value)} /></label>
        <label className="field">Building height (m)<input type="number" min={9} max={200} step={3} value={height} onChange={(e) => setHeight(+e.target.value)} /></label>
      </div>
      <p className="small muted">Edge 1 (first two corners) is taken as the road edge. Setbacks come from the rules file for this road width and height.</p>
      <div className="row">
        <button className="btn" onClick={() => setPoints(points.slice(0, -1))} disabled={!points.length}>Undo corner</button>
        <button className="btn" onClick={() => setPoints([])} disabled={!points.length}>Clear</button>
        <span className="spacer" />
        <button className="btn primary" disabled={points.length < 3}
          onClick={async () => {
            const ok = await run("Analysing site", () => rpc("set_site", { lnglat: points, name, locality, road_width: road, building_height: height }));
            if (ok) setStep("climate");
          }}>Continue</button>
      </div>
    </div>
  );
}

// ------------------------------------------------------------------------------------------- 3 Climate
export function ClimatePanel() {
  const site = useStore((s) => s.project?.site);
  const run = useStore((s) => s.run);
  const setStep = useStore((s) => s.setStep);
  if (!site?.climate) return <div className="empty">Trace a plot first.</div>;
  const c = site.climate;
  return (
    <div className="col">
      <h2>3 · Sun, wind and setback</h2>
      <div className="grid3">
        <Kpi label="Site" value={`${fmt(site.area)} m²`} />
        <Kpi label="Setback" value={`${fmt(site.setback)} m`} />
        <Kpi label="Buildable" value={`${fmt(site.buildable_area)} m²`} />
      </div>
      <div className="small muted">{site.setback_clause}</div>
      <div className="card">
        <div className="row"><h3>Sun path</h3><span className="spacer" />{c.hot_facades.length > 0 && <span className="badge red">Hot west sun: {c.hot_facades.join(", ")}</span>}</div>
        <SunPath climate={c} />
        <div className="row wrap small">
          {c.sun.map((p) => <span key={p.label} className="badge grey"><span className="dot" style={{ background: SUN_COLORS[p.label] }} />{p.label} · noon {p.noon_altitude}°</span>)}
        </div>
      </div>
      <div className="card">
        <div className="row"><h3>Wind</h3><span className="spacer" />{c.wind.indicative && <span className="badge amber">indicative</span>}</div>
        <WindRoseChart climate={c} />
        <div className="small"><b>{c.wind.prevailing}</b> prevailing · <span className="muted">{c.wind.source}</span></div>
        {c.wind.notes.map((n) => <div key={n} className="small muted">{n}</div>)}
      </div>
      <div className="grid2">
        <Kpi label="Sea view" value={c.sea_view === "unknown" ? "–" : c.sea_view} />
        <Kpi label="Creek" value={c.creek === "unknown" ? "–" : c.creek} />
      </div>
      <button className="btn primary" onClick={async () => { if (await run("Placing towers", () => rpc("place_towers"))) setStep("place"); }}>
        Place the towers →
      </button>
    </div>
  );
}

// ------------------------------------------------------------------------------------------- 4 Place
export function PlacePanel() {
  const pl = useStore((s) => s.project?.placement);
  const run = useStore((s) => s.run);
  const setStep = useStore((s) => s.setStep);
  const tickets = useStore((s) => s.tickets);
  if (!pl) return <div className="col"><h2>4 · Place the towers</h2><button className="btn primary" onClick={() => run("Placing towers", () => rpc("place_towers"))}>Find options</button></div>;
  const card = (o: PlacementOption) => {
    const t = tickets.find((x) => x.id === `T5:${o.id}`);
    return (
      <button key={o.id} className={`option ${pl.chosen === o.id ? "on" : ""}`}
        onClick={async () => { if (await run("Shaping the floor", () => rpc("choose_placement", { option: o.id }))) setStep("shape"); }}>
        <div className="row"><span className="big">{o.id}</span><b>{o.towers === 1 ? "Single tower" : o.label}</b><span className="spacer" />
          <span className="badge teal">{pct(o.coverage)}</span></div>
        <div className="small">{o.plates.map((p) => `${fmt(p.area, 0)} m²`).join(" + ")}{o.min_gap != null ? ` · gap ${fmt(o.min_gap)} m` : ""}</div>
        <div className="row wrap small">{Object.entries(o.passes).map(([k, v]) => <span key={k} className={`badge ${v ? "green" : "red"}`}>{k.replace(/_/g, " ")}</span>)}</div>
        <div className="small muted" style={{ marginTop: 4 }}>
          {t?.answer?.by === "claude" && <span className="badge amber" style={{ marginRight: 4 }}>Claude</span>}{o.drivers}
        </div>
      </button>
    );
  };
  return (
    <div className="col">
      <h2>4 · Place the towers</h2>
      <p className="muted small">Every angle (1° steps) and plate size that fits was tried. Best option per family, scored on coverage,
        gap, breeze, view and west glass. Searched in {pl.elapsed_s} s.</p>
      {pl.options.map(card)}
    </div>
  );
}

// ------------------------------------------------------------------------------------------- 5 Shape
interface ShapeInfo { shape: string; label: string; ratings: Record<string, number>; efficiency: number; area: number; corners: number; outline: [number, number][] }

export function ShapePanel({ draft }: { draft: Record<string, number> | null }) {
  const project = useStore((s) => s.project);
  const tower = useStore((s) => s.tower);
  const setTower = useStore((s) => s.setTower);
  const run = useStore((s) => s.run);
  const setStep = useStore((s) => s.setStep);
  const [shapes, setShapes] = useState<ShapeInfo[]>([]);
  const plate = project?.plates.find((p) => p.tower_id === tower);
  useEffect(() => { if (plate) void rpc<ShapeInfo[]>("list_shapes", { tower_id: tower }).then(setShapes).catch(() => setShapes([])); }, [tower, plate?.tower_id]);
  if (!plate) return <div className="empty">Choose a placement first.</div>;
  const zones = Object.fromEntries(plate.zones.map((z) => [z.kind, z.area]));
  const params = { ...plate.params, ...(draft ?? {}) };
  const stars = (n: number) => "★".repeat(n) + "☆".repeat(5 - n);
  const editable = Object.keys(plate.params).filter((k) => !["units", "shafts"].includes(k));
  return (
    <div className="col">
      <div className="row"><h2>5 · Shape the floor</h2><span className="spacer" />
        {(project?.plates.length ?? 0) > 1 && <select value={tower} onChange={(e) => setTower(e.target.value)}>{project!.plates.map((p) => <option key={p.tower_id}>{p.tower_id}</option>)}</select>}
      </div>
      <div className="shapes">
        {shapes.map((s) => (
          <button key={s.shape} className={`shape-card ${plate.shape === s.shape ? "on" : ""}`}
            onClick={() => run("Fitting shape", () => rpc("set_plate", { tower_id: tower, shape: s.shape, params: {} }))}>
            <svg viewBox="-35 -20 70 40" height={34}><polygon points={s.outline.map(([x, y]) => `${x},${-y}`).join(" ")} fill="var(--teal-soft)" stroke="var(--teal)" strokeWidth={1.2} vectorEffect="non-scaling-stroke" transform={`scale(${60 / Math.max(1, Math.max(...s.outline.map((p) => Math.abs(p[0]))) * 2)})`} /></svg>
            <b className="small">{s.label}</b>
            <span className="stars" title="core efficiency">{stars(s.ratings.core_efficiency)}</span>
            <span className="stars" title="daylight" style={{ color: "var(--amber)" }}>{stars(s.ratings.daylight)}</span>
            <span className="stars" title="structure" style={{ color: "var(--muted)" }}>{stars(s.ratings.structure)}</span>
          </button>
        ))}
      </div>
      <div className="small muted">Rows: core efficiency · daylight · structure.</div>
      <div className="grid2">
        <Kpi label="Plate" value={`${fmt(plate.area)} m²`} />
        <Kpi label="Corners" value={`${plate.corners} · ${fmt(plate.width, 2)}×${fmt(plate.depth, 2)}`} />
        <Kpi label="Flats zone" value={`${fmt(zones.flats, 1)} m²`} />
        <Kpi label="Core" value={`${fmt(zones.core, 1)} m²`} />
        <Kpi label="Passage" value={`${fmt(zones.passage, 1)} m²`} />
        <Kpi label="Shafts" value={`${fmt(zones.shaft ?? 0, 1)} m²`} />
      </div>
      <div className="small muted">Zones always add up to the plate. Drag the handles on the plan, or type values.</div>
      <div className="grid2">
        {editable.map((k) => (
          <label key={k} className="field">{k.replace(/_/g, " ")} (m)
            <input type="number" step={0.1} value={Number(params[k]).toFixed(1)}
              onChange={(e) => run("Updating plate", () => rpc("set_plate", { tower_id: tower, params: { [k]: +e.target.value } }))} />
          </label>
        ))}
        <label className="field">air shafts
          <input type="number" min={0} max={4} step={1} value={params.shafts}
            onChange={(e) => run("Updating plate", () => rpc("set_plate", { tower_id: tower, params: { shafts: +e.target.value } }))} />
        </label>
      </div>
      <label className="btn small" style={{ textAlign: "center" }}>
        Import a DXF of the core and unit footprint
        <input type="file" accept=".dxf" hidden onChange={(e) => { const f = e.target.files?.[0]; if (f) void run("Reading DXF", () => uploadDrawing(f, "plate", tower)); }} />
      </label>
      <button className="btn primary" onClick={() => setStep("units")}>Split into flats →</button>
    </div>
  );
}

// ------------------------------------------------------------------------------------------- 6 Units
export function UnitsPanel() {
  const project = useStore((s) => s.project);
  const tower = useStore((s) => s.tower);
  const unit = useStore((s) => s.unit);
  const setUnit = useStore((s) => s.setUnit);
  const run = useStore((s) => s.run);
  const setStep = useStore((s) => s.setStep);
  const plate = project?.plates.find((p) => p.tower_id === tower);
  const units = (project?.units ?? []).filter((u) => (tower === "T1" ? !/^T\d+-/.test(u.id) : u.id.startsWith(`${tower}-`)));
  if (!plate) return <div className="empty">Shape a plate first.</div>;
  const n = plate.params.units;
  return (
    <div className="col">
      <h2>6 · Split into flats</h2>
      <div className="row">
        <span>Flats on this floor</span><span className="spacer" />
        <button className="btn small" disabled={n <= 1} onClick={() => run("Splitting", () => rpc("split_units", { tower_id: tower, count: n - 1 }))}>−</button>
        <b style={{ minWidth: 24, textAlign: "center" }}>{n}</b>
        <button className="btn small" onClick={() => run("Splitting", () => rpc("split_units", { tower_id: tower, count: n + 1 }))}>+</button>
      </div>
      <p className="small muted">Every flat links to the core it opens off (red dot). A flat without access or outside the outline is flagged, never deleted.</p>
      <table className="t">
        <thead><tr><th>Flat</th><th>m²</th><th>Room list</th><th /></tr></thead>
        <tbody>
          {units.map((u) => (
            <tr key={u.id} onClick={() => setUnit(u.id)} style={{ cursor: "pointer", background: unit === u.id ? "var(--teal-soft)" : undefined }}>
              <td><b>{u.id}</b></td><td>{fmt(u.area)}</td><td>{u.programme?.name ?? "–"}</td>
              <td>{u.flags.map((f) => <span key={f} className="badge red">{f.replace(/_/g, " ")}</span>)}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <button className="btn primary" onClick={() => setStep("program")}>Pick room lists →</button>
    </div>
  );
}

// ------------------------------------------------------------------------------------------- 6b Programme
export function ProgramPanel() {
  const project = useStore((s) => s.project);
  const unitId = useStore((s) => s.unit);
  const setUnit = useStore((s) => s.setUnit);
  const run = useStore((s) => s.run);
  const setStep = useStore((s) => s.setStep);
  const [presets, setPresets] = useState<Programme[]>([]);
  const [brief, setBrief] = useState("");
  useEffect(() => { void rpc<Programme[]>("list_presets").then(setPresets); }, []);
  const units = project?.units ?? [];
  const u = units.find((x) => x.id === unitId) ?? units[0];
  if (!u) return <div className="empty">Split the plate first.</div>;
  return (
    <div className="col">
      <h2>6 · Room lists</h2>
      <label className="field">Flat
        <select value={u.id} onChange={(e) => setUnit(e.target.value)}>{units.map((x) => <option key={x.id} value={x.id}>{x.id} · {fmt(x.area)} m²</option>)}</select>
      </label>
      <label className="field">Preset
        <select value={u.programme?.id ?? ""} onChange={(e) => run("Setting room list", () => rpc("set_programme", { unit_id: u.id, preset: e.target.value }))}>
          {presets.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
          {u.programme && !presets.some((p) => p.id === u.programme!.id) && <option value={u.programme.id}>{u.programme.name}</option>}
        </select>
      </label>
      {u.programme && (
        <table className="t">
          <thead><tr><th>Room</th><th>Target m²</th><th>Window</th><th /></tr></thead>
          <tbody>{u.programme.rooms.map((r) => <tr key={r.id}><td>{r.name}</td><td>{r.target_area}</td><td>{r.needs_window ? "yes" : ""}</td><td className="muted small">{r.optional ? "optional" : ""}</td></tr>)}</tbody>
        </table>
      )}
      <div className="small muted">{u.programme?.note} Statutory minimums are checked later, from the rules files.</div>
      <div className="card soft col">
        <h3>Brief in words <span className="badge amber">Claude</span></h3>
        <textarea rows={2} value={brief} onChange={(e) => setBrief(e.target.value)} placeholder="3BHK with a pooja room for a joint family" />
        <button className="btn small amber" disabled={!brief.trim()}
          onClick={() => run("Asking Claude", () => rpc("propose_programme", { unit_id: u.id, brief, by: "user" }))}>Send to Claude</button>
        <div className="small muted">Creates a decision for Claude. Its room list is schema- and rule-checked before use. Until then the preset stays.</div>
      </div>
      <button className="btn primary" onClick={() => setStep("diagram")}>Wire the rooms →</button>
    </div>
  );
}

// ------------------------------------------------------------------------------------------- 7 Diagram
export function DiagramPanel({ diagram, onSolved }: { diagram: Diagram | null; onSolved: () => void }) {
  const project = useStore((s) => s.project);
  const unitId = useStore((s) => s.unit);
  const setUnit = useStore((s) => s.setUnit);
  const run = useStore((s) => s.run);
  const setStep = useStore((s) => s.setStep);
  const units = project?.units ?? [];
  const u = units.find((x) => x.id === unitId) ?? units[0];
  const placed = diagram?.rooms.filter((r) => r.placed).length ?? 0;
  const plan = project?.plans.find((p) => p.unit_id === u?.id);
  if (!u) return <div className="empty">No flats yet.</div>;
  return (
    <div className="col">
      <h2>7 · Wire rooms, then solve</h2>
      <label className="field">Flat
        <select value={u.id} onChange={(e) => setUnit(e.target.value)}>{units.map((x) => <option key={x.id} value={x.id}>{x.id} · {x.programme?.bhk ?? "?"}BHK{project?.plans.some((p) => p.unit_id === x.id) ? " ✓" : ""}</option>)}</select>
      </label>
      {diagram && <div className="grid2"><Kpi label="Rooms" value={`${placed} placed / ${diagram.rooms.length}`} /><Kpi label="Links" value={String(diagram.links.length)} /></div>}
      {diagram?.conflicts.map((c) => (
        <div key={c.id} className="card" style={{ borderColor: "var(--red)" }}>
          <div className="row"><span className="badge red">{c.kind}</span><b className="small">{c.rooms.slice(0, 5).join(", ")}</b></div>
          <div className="small">{c.reason}</div>
          {c.fixes.length > 0 && <div className="small muted">Fixes: {c.fixes.map((f) => f.label + (f.valid === false ? " (✗)" : "")).join(" · ")}</div>}
        </div>
      ))}
      <div className={`badge ${diagram?.conflicts.length ? "amber" : "teal"}`}>{diagram?.message || "Run the pre-check by editing the diagram."}</div>
      <div className="row">
        <button className="btn primary" onClick={async () => { const r = await run(`Solving ${u.id}`, () => rpc("solve_unit", { unit_id: u.id })); if (r) onSolved(); }}>Generate the plan</button>
        <button className="btn" onClick={async () => { const r = await run("Solving all flats", () => rpc("solve_all", {})); if (r) setStep("review"); }}>Generate all flats</button>
      </div>
      {plan && <div className="small">Last solve: {plan.status} in {plan.solve_time_s} s · carpet {fmt(plan.carpet_area)} m²
        <button className="btn small ghost" onClick={() => setStep("review")}>View →</button></div>}
    </div>
  );
}

// ------------------------------------------------------------------------------------------- 8 Review
export function ReviewPanel({ onFocus, show3d, setShow3d }: { onFocus: (r: CheckResult | null) => void; show3d: boolean; setShow3d: (v: boolean) => void }) {
  const project = useStore((s) => s.project);
  const summary = useStore((s) => s.summary);
  const tower = useStore((s) => s.tower);
  const run = useStore((s) => s.run);
  const [filter, setFilter] = useState<"problems" | "all">("problems");
  const checks = project?.checks;
  const results = useMemo(() => {
    const rs = checks?.results ?? [];
    const order = { fail: 0, review: 1, pass: 2, "n/a": 3 } as const;
    return [...rs].filter((r) => filter === "all" || r.result === "fail" || r.result === "review").sort((a, b) => order[a.result] - order[b.result]);
  }, [checks, filter]);
  const mix = (summary?.mix ?? {}) as Record<string, number>;
  return (
    <div className="col">
      <h2>8 · Check the rules</h2>
      <div className="grid3">
        <Kpi label="Flats" value={String(project?.plans.length ?? 0)} />
        <Kpi label="Carpet" value={`${fmt((summary?.carpet_m2 as number) ?? 0)} m²`} />
        <Kpi label="Mix" value={Object.entries(mix).map(([k, v]) => `${v}×${k}`).join(" ") || "–"} />
      </div>
      <div className="row wrap">
        <button className="btn small primary" onClick={() => run("Running checks", () => rpc("run_checks"))}>Run checks</button>
        <button className="btn small" onClick={async () => { window.location.href = await dxfUrl(tower); }}>Export DXF</button>
        <label className="btn small">Import DXF<input type="file" accept=".dxf" hidden onChange={(e) => { const f = e.target.files?.[0]; if (f) void run("Reading drawing", () => uploadDrawing(f)); }} /></label>
        <button className={`btn small ${show3d ? "primary" : ""}`} onClick={() => setShow3d(!show3d)}>3D</button>
      </div>
      {project?.drawing && (
        <div className="card soft small">
          <b>{project.drawing.filename}</b> · {project.drawing.dxf_version} · {project.drawing.entities} entities<br />
          Units: header {project.drawing.header_units}, size says {project.drawing.detected_units} · {project.drawing.spaces} spaces · {project.drawing.flats} flats
          · outline {project.drawing.outline_corners} corners, {fmt(project.drawing.outline_area)} m²
          {project.drawing.warnings.map((w) => <div key={w} className="badge amber">{w}</div>)}
          <details><summary>Layer map ({project.drawing.layers.length})</summary>
            <table className="t"><tbody>{project.drawing.layers.map((l) => <tr key={l.layer}><td className="mono">{l.layer}</td><td>{l.meaning}</td><td className="muted">{l.method}</td><td>{l.entities}</td></tr>)}</tbody></table>
          </details>
        </div>
      )}
      {checks ? (
        <>
          <div className="row wrap">
            {Object.entries(checks.by_code).map(([code, r]) => (
              <span key={code} className={`badge ${r.fail ? "red" : r.review ? "amber" : "green"}`}>
                {code} {r.pass}/{r.clauses - r["n/a"]} pass{r.review ? ` · ${r.review} review` : ""}{r.fail ? ` · ${r.fail} fail` : ""}
              </span>
            ))}
          </div>
          <div className="row"><span className="small muted">{checks.counts.pass} pass · {checks.counts.fail} fail · {checks.counts.review} review</span><span className="spacer" />
            <button className="btn small ghost" onClick={() => setFilter(filter === "all" ? "problems" : "all")}>{filter === "all" ? "Problems only" : "Show all"}</button></div>
          <div className="checks">
            {results.length === 0 && <div className="badge green">No problems found.</div>}
            {results.slice(0, 300).map((r, i) => (
              <div key={i} className="check" onClick={() => onFocus(r)} title={`${r.source} ${r.clause}`}>
                <span className={`r ${r.result === "n/a" ? "na" : r.result}`}>{r.result.toUpperCase()}</span>
                <div><div className="small"><b>{r.title}</b></div><div className="small muted">{r.target} · {r.message}</div></div>
                <span className="mono muted">{r.rule_id}</span>
              </div>
            ))}
          </div>
          <div className="small muted">{checks.disclaimer}</div>
        </>
      ) : <div className="muted small">Solve flats or import a drawing, then run the checks.</div>}
    </div>
  );
}
