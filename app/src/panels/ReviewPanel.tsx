import { useEffect, useMemo, useRef, useState, type DragEvent } from "react";
import { dxfUrl, rpc, uploadDrawing } from "../lib/api";
import { fmt } from "../lib/geom";
import { useStore } from "../lib/store";
import type { CheckResult, PlanQuality } from "../lib/types";
import {
  Chip, EmptyState, Icon, Led, LatchButton, Panel, ProvenanceTag, Readout, Skeleton, StatusChip, Tabs, type TabItem,
  type ReadoutTone,
} from "../ui";

// ------------------------------------------------------------------------------------------- styles (rv- prefix)
const CSS = `
.rv { display: flex; flex-direction: column; gap: 14px; min-width: 0; }
.rv-intro { margin: 0; font-size: 13px; line-height: 1.5; color: var(--ink-2); }
.rv-intro b { color: var(--ink-1); }
.rv-sec { font: 600 10.5px var(--f-mono); letter-spacing: 0.12em; text-transform: uppercase; color: var(--ink-3); margin: 0; display: flex; align-items: center; gap: 8px; }
.rv-sec::after { content: ""; flex: 1; height: 1px; background: var(--hairline); }
.rv-row { display: flex; align-items: center; gap: 8px; min-width: 0; flex-wrap: wrap; }
.rv-row.between { justify-content: space-between; }
.rv-grid3 { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 8px; }
.rv-grid3 .rv-mix { grid-column: 1 / -1; }
.rv-grid3 .rv-mix .lcd { white-space: normal; overflow-wrap: anywhere; justify-content: flex-start; }
.rv-grid2 { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px; }
.rv-meta { margin: 0; font-size: 12px; line-height: 1.45; color: var(--ink-3); }
.rv-tools { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 8px; }
.rv-tools .ui-btn-primary { grid-column: 1 / -1; }
.rv-drop { padding: 12px; display: flex; flex-direction: column; gap: 8px; border: 1px dashed var(--chamfer-inner); transition: box-shadow var(--dur-2) var(--ease), border-color var(--dur-2); }
.rv-drop.is-over { border-color: var(--cobalt); box-shadow: var(--neo-inset), 0 0 0 2px var(--cobalt); }
.rv-alert { display: flex; gap: 10px; align-items: flex-start; font-size: 12.5px; line-height: 1.45; color: var(--ink-1); padding: 10px 12px; border-radius: var(--r-md); background: var(--neo-bg); box-shadow: var(--neo-latched); border: 1px solid var(--chamfer-inner); border-left: 3px solid var(--fail); }
.rv-alert.warn { border-left-color: var(--warn); }
.rv-alert > svg { flex: none; margin-top: 1px; color: var(--fail-ink); }
.rv-alert.warn > svg { color: var(--warn-ink); }
.rv-alert-body { flex: 1; min-width: 0; overflow-wrap: anywhere; }
.rv-alert-title { font-weight: 700; color: var(--fail-ink); }
.rv-alert-actions { margin-top: 8px; display: flex; gap: 8px; }

/* plan quality (critic) */
.rv-ql { margin: 0; padding: 0; list-style: none; display: flex; flex-direction: column; gap: 8px; }
.rv-ql li { display: grid; grid-template-columns: auto auto minmax(0, 1fr); gap: 4px 10px; align-items: baseline; font-size: 12px; line-height: 1.4; color: var(--ink-1); }
.rv-ql .id { font: 700 11.5px var(--f-mono); }
.rv-ql .g { font: 800 14px var(--f-display); min-width: 18px; text-align: center; }
.rv-ql .g.gA, .rv-ql .g.gB { color: var(--pass-ink, var(--det-ink)); }
.rv-ql .g.gC { color: var(--warn-ink, var(--ink-1)); }
.rv-ql .g.gD, .rv-ql .g.gF { color: var(--fail-ink); }
.rv-ql .why { grid-column: 1 / -1; color: var(--ink-3); overflow-wrap: anywhere; }

/* drawing report */
.rv-dwg { padding: 12px; display: flex; flex-direction: column; gap: 10px; }
.rv-dwg-name { font: 700 13px var(--f-sans); color: var(--ink-1); overflow-wrap: anywhere; }
.rv-dwg-sub { font: 500 11.5px var(--f-mono); color: var(--ink-2); }
.rv-layers { margin-top: 2px; }
.rv-layers summary { cursor: pointer; font: 600 12px var(--f-sans); color: var(--ink-1); }
.rv-layers table { width: 100%; border-collapse: collapse; margin-top: 8px; font-size: 11.5px; }
.rv-layers th { text-align: left; font: 600 10px var(--f-mono); letter-spacing: 0.08em; text-transform: uppercase; color: var(--ink-3); padding: 2px 6px 4px 0; }
.rv-layers td { padding: 4px 6px 4px 0; border-top: 1px solid var(--hairline); color: var(--ink-1); vertical-align: top; overflow-wrap: anywhere; }
.rv-layers td.num { text-align: right; font-family: var(--f-mono); font-variant-numeric: tabular-nums; }
.rv-layers td.mute { color: var(--ink-2); }

/* statutory board */
.rv-board { padding: 12px; display: flex; flex-direction: column; gap: 12px; }
.rv-verdict { display: flex; align-items: flex-start; gap: 10px; font-size: 13px; line-height: 1.45; color: var(--ink-1); }
.rv-verdict .led { margin-top: 5px; }
.rv-verdict b { font-weight: 700; }
.rv-counts { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 8px; }
.rv-count { display: flex; flex-direction: column; gap: 6px; }
.rv-count-head { display: inline-flex; align-items: center; gap: 6px; font: 700 10.5px var(--f-mono); letter-spacing: 0.1em; color: var(--ink-2); }
.rv-codes { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 8px; }
.rv-code { padding: 10px 12px; display: flex; flex-direction: column; gap: 8px; border-radius: var(--r-md); }
.rv-code-name { font: 700 11px var(--f-mono); letter-spacing: 0.08em; color: var(--ink-1); }
.rv-code-chips { display: flex; flex-wrap: wrap; gap: 6px; }

/* results */
.rv-list { list-style: none; margin: 0; padding: 10px; display: flex; flex-direction: column; gap: 8px; max-height: none; }
.rv-item { border-radius: var(--r-md); background: var(--neo-convex-bg); box-shadow: var(--neo-raised-xs); border: 1px solid var(--chamfer); overflow: hidden; }
.rv-item.is-sel { background: var(--neo-bg); box-shadow: var(--neo-latched), 0 0 0 1.5px var(--cobalt); border-color: var(--chamfer-inner); }
.rv-item.is-fail { border-left: 3px solid var(--fail); }
.rv-item.is-review { border-left: 3px solid var(--warn); }
.rv-item.is-pass { border-left: 3px solid var(--pass); }
.rv-btn { appearance: none; width: 100%; text-align: left; cursor: pointer; background: transparent; border: 0; padding: 10px 12px; display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 6px 10px; align-items: center; color: var(--ink-1); font: inherit; }
.rv-btn:hover .rv-title { color: var(--cobalt-ink); }
.rv-head { display: flex; align-items: center; gap: 8px; min-width: 0; grid-column: 1 / -1; flex-wrap: wrap; }
.rv-title { font: 700 13px var(--f-sans); color: var(--ink-1); min-width: 0; overflow-wrap: anywhere; }
.rv-sub { grid-column: 1 / -1; font-size: 12px; line-height: 1.4; color: var(--ink-2); overflow-wrap: anywhere; }
.rv-id { font: 500 11px var(--f-mono); color: var(--ink-3); }
.rv-vals { grid-column: 1 / -1; display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 8px; }
.rv-vals .lcd { width: 100%; }
.rv-detail { padding: 4px 12px 12px; display: flex; flex-direction: column; gap: 10px; border-top: 1px solid var(--hairline); }
.rv-msg { margin: 8px 0 0; font-size: 12.5px; line-height: 1.5; color: var(--ink-1); overflow-wrap: anywhere; }
.rv-clause { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; font: 500 11.5px var(--f-mono); color: var(--ink-2); }
.rv-proof { margin: 0; padding-left: 18px; font: 500 11.5px/1.5 var(--f-mono); color: var(--ink-2); display: flex; flex-direction: column; gap: 2px; overflow-wrap: anywhere; }
.rv-explain { display: flex; flex-direction: column; gap: 6px; padding: 10px 12px; border-radius: var(--r-md); background: var(--neo-bg); box-shadow: var(--neo-latched); border-left: 3px solid var(--ai); }
.rv-explain p { margin: 0; font-size: 12.5px; line-height: 1.5; overflow-wrap: anywhere; }
.rv-actions { display: flex; gap: 8px; flex-wrap: wrap; }
.rv-more { margin: 0; font-size: 12px; color: var(--ink-3); text-align: center; padding: 2px 0 8px; }
.rv-disclaimer { display: flex; gap: 8px; align-items: flex-start; font-size: 12px; line-height: 1.5; color: var(--ink-2); padding: 10px 12px; border-radius: var(--r-md); box-shadow: var(--neo-latched); }
.rv-disclaimer > svg { flex: none; margin-top: 2px; }
@media (max-width: 400px) { .rv-counts { grid-template-columns: 1fr; } }
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

// ------------------------------------------------------------------------------------------- contracts
/** Mirrors the engine's explain_check result. */
interface Explain {
  rule_id: string; clause: string; source: string; proof: string[]; explanation: string | null;
  verified_by_consultant: boolean; others: number;
}
type Filter = "problems" | "fail" | "review" | "pass" | "all";
const ORDER = { fail: 0, review: 1, pass: 2, "n/a": 3 } as const;
const TONE: Record<CheckResult["result"], ReadoutTone> = { fail: "fail", review: "warn", pass: "pass", "n/a": "ink" };
const keyOf = (r: CheckResult) => `${r.rule_id}@${r.target}`;
const digitsOf = (v: unknown) => (typeof v === "number" && !Number.isInteger(v) ? 2 : 0);
const errText = (e: unknown) => (e instanceof Error ? e.message : String(e));
const MAX_ROWS = 300;

// ------------------------------------------------------------------------------------------- 8 Review
export function ReviewPanel({ onFocus, show3d, setShow3d }: { onFocus: (r: CheckResult | null) => void; show3d: boolean; setShow3d: (v: boolean) => void }) {
  useInjectedCss("rv-styles", CSS);
  const project = useStore((s) => s.project);
  const summary = useStore((s) => s.summary);
  const tower = useStore((s) => s.tower);
  const run = useStore((s) => s.run);
  const busy = useStore((s) => s.busy);
  const toast = useStore((s) => s.toast);
  const setStep = useStore((s) => s.setStep);
  const setInbox = useStore((s) => s.setInbox);
  const [filter, setFilter] = useState<Filter>("problems");
  const [sel, setSel] = useState<string | null>(null);
  const [explain, setExplain] = useState<Record<string, Explain | "loading" | "error">>({});
  const [checking, setChecking] = useState(false);
  const [importing, setImporting] = useState(false);
  const [exporting, setExporting] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [over, setOver] = useState(false);
  const file = useRef<HTMLInputElement>(null);
  const [quality, setQuality] = useState<Record<string, PlanQuality | "error">>({});
  const [scoring, setScoring] = useState(false);

  const checks = project?.checks ?? null;
  const planCount = project?.plans.length ?? 0;
  const hasWork = planCount > 0;
  const anyBusy = busy != null || checking || importing;

  const all = checks?.results ?? [];
  const tally = useMemo(() => {
    const t = { pass: 0, fail: 0, review: 0, na: 0 };
    for (const r of all) { if (r.result === "n/a") t.na++; else t[r.result]++; }
    return t;
  }, [all]);
  const results = useMemo(() => {
    const keep = (r: CheckResult) => filter === "all" || (filter === "problems" ? r.result === "fail" || r.result === "review" : r.result === filter);
    return all.filter(keep).sort((a, b) => ORDER[a.result] - ORDER[b.result]);
  }, [all, filter]);
  const counts = { pass: checks?.counts.pass ?? tally.pass, review: checks?.counts.review ?? tally.review, fail: checks?.counts.fail ?? tally.fail };

  // a fresh result set invalidates the open row
  useEffect(() => { setSel(null); setExplain({}); }, [checks]);

  const mix = (summary?.mix ?? {}) as Record<string, number>;
  const mixText = Object.entries(mix).map(([k, v]) => `${v}×${k}`).join(" ");

  const runChecks = async () => {
    setErr(null); setChecking(true);
    const r = await run("Running checks", async () => {
      try { return await rpc("run_checks"); } catch (e) { setErr(errText(e)); throw e; }
    });
    setChecking(false);
    if (r) toast("ok", "Checks finished.");
  };
  const exportDxf = async () => {
    setExporting(true);
    try {
      window.location.href = await dxfUrl(tower);
      toast("info", "Your DXF download is starting.");
    } catch (e) { toast("error", errText(e)); }
    finally { window.setTimeout(() => setExporting(false), 800); }
  };
  const importFile = async (f: File | undefined | null) => {
    if (!f) return;
    if (!/\.dxf$/i.test(f.name)) { toast("error", "That is not a .dxf file. Save the drawing as DXF and try again."); return; }
    setErr(null); setImporting(true);
    const r = await run("Reading drawing", async () => {
      try { return await uploadDrawing(f); } catch (e) { setErr(errText(e)); throw e; }
    });
    setImporting(false);
    if (r) toast("ok", `${f.name} read. Run the checks next.`);
  };
  const onDrop = (e: DragEvent) => { e.preventDefault(); setOver(false); if (!anyBusy) void importFile(e.dataTransfer.files?.[0]); };

  const pickRow = (r: CheckResult) => {
    const k = keyOf(r);
    if (sel === k) { setSel(null); onFocus(null); return; }
    setSel(k);
    if (r.bbox && show3d) setShow3d(false); // the plan is where the location is shown
    onFocus(r);
    if (explain[k] === undefined) {
      setExplain((m) => ({ ...m, [k]: "loading" }));
      rpc<Explain>("explain_check", { rule_id: r.rule_id, target: r.target })
        .then((x) => setExplain((m) => ({ ...m, [k]: x })))
        .catch(() => setExplain((m) => ({ ...m, [k]: "error" })));
    }
  };

  const tabs: TabItem<Filter>[] = [
    { id: "problems", label: `Problems ${counts.fail + counts.review}`, led: counts.fail ? "fail" : counts.review ? "warn" : "pass" },
    { id: "fail", label: `Fail ${counts.fail}`, led: "fail" },
    { id: "review", label: `Review ${counts.review}`, led: "warn" },
    { id: "pass", label: `Pass ${counts.pass}`, led: "pass" },
    { id: "all", label: `All ${all.length}` },
  ];

  // The critic takes ~0.2 s a flat, so it only runs when asked, one flat at a time (the engine caches each answer).
  const scoreFlats = async () => {
    setScoring(true);
    try {
      for (const p of project?.plans ?? []) {
        try {
          const q = await rpc<PlanQuality>("plan_quality", { unit_id: p.unit_id });
          setQuality((m) => ({ ...m, [p.unit_id]: q }));
        } catch { setQuality((m) => ({ ...m, [p.unit_id]: "error" })); }
      }
    } finally { setScoring(false); }
  };
  const scored = Object.values(quality).filter((q): q is PlanQuality => q !== "error");
  const meanScore = scored.length ? scored.reduce((a, q) => a + q.score, 0) / scored.length : null;

  const verdict = counts.fail > 0
    ? { led: "fail" as const, text: <><b>{counts.fail} rule{counts.fail === 1 ? "" : "s"} fail.</b> Fix them before you rely on this plan.</> }
    : counts.review > 0
      ? { led: "warn" as const, text: <><b>No failures.</b> {counts.review} item{counts.review === 1 ? " needs" : "s need"} a human look.</> }
      : { led: "pass" as const, text: <><b>All applicable rules pass.</b> A consultant still signs off.</> };

  return (
    <div className="rv">
      <p className="rv-intro">The <b>rule engine</b> checks every plan against DCPR 2034 and NBC 2016. Claude only explains the results.</p>

      <Panel profile="well" pad="md" title="Plan totals" aria-label="Plan totals"
        actions={<ProvenanceTag by="engine" kind="derived" source="Counted and summed from the solved plans" compact />}>
        <div className="rv-grid3">
          <Readout label="Flats" value={planCount} digits={0} tone="det" />
          <Readout label="Carpet" value={(summary?.carpet_m2 as number | undefined) ?? (hasWork ? 0 : null)} unit="m²" tone="det" />
          <Readout label="Mix" value={mixText || null} tone="ink" className="rv-mix" />
        </div>
      </Panel>

      {/* ---------------------------------------------------------------- plan quality */}
      {planCount > 0 && (
        <Panel profile="well" pad="md" title="Plan quality" aria-label="Plan quality of each flat"
          actions={<ProvenanceTag by="engine" kind="derived" source="The plan critic (quality.critique): circulation, waste, sequence, furniture, daylight, doors. A design-quality score, not a statutory result." compact />}>
          <div className="rv-row between">
            <Readout label="Mean score" value={meanScore} unit="/ 100" digits={0} tone="det" size="sm" />
            <LatchButton size="sm" loading={scoring} disabled={scoring || (anyBusy && !scoring)} onClick={() => void scoreFlats()}>
              {scoring ? "Scoring…" : scored.length ? "Score again" : "Score the flats"}
            </LatchButton>
          </div>
          {scored.length === 0 && !scoring && <p className="rv-meta">Scores each solved flat for how well it works as a home. About 0.2 s per flat. Open a flat in stage 7 for the full scorecard.</p>}
          <ul className="rv-ql" aria-label="Flat scores" aria-live="polite">
            {(project?.plans ?? []).map((p) => {
              const q = quality[p.unit_id];
              if (!q) return null;
              if (q === "error") return <li key={p.unit_id}><span className="id">{p.unit_id}</span><span className="g">–</span><span>Could not be scored.</span></li>;
              return (
                <li key={p.unit_id}>
                  <span className="id">{p.unit_id}</span>
                  <span className={`g g${q.grade}`} aria-label={`Grade ${q.grade}`}>{q.grade} <span className="mono" style={{ fontWeight: 600, fontSize: 11 }}>{Math.round(q.score)}</span></span>
                  <span>{q.top_issues[0]?.message ?? "No findings."}</span>
                  {q.weakest_link && <span className="why">Held back by {q.weakest_link.replace(/_/g, " ")}.</span>}
                </li>
              );
            })}
          </ul>
        </Panel>
      )}

      {/* ---------------------------------------------------------------- tools */}
      <div className="rv-tools">
        <LatchButton variant="primary" block loading={checking} disabled={!hasWork || (anyBusy && !checking)} icon={<Icon name="play" size={14} />}
          onClick={() => void runChecks()}>
          {checking ? "Checking…" : checks ? "Run the checks again" : "Run checks"}
        </LatchButton>
        <LatchButton block loading={exporting} disabled={!hasWork || exporting} icon={<Icon name="download" size={14} />} onClick={() => void exportDxf()}>
          Export DXF
        </LatchButton>
        <LatchButton block latched={show3d} icon={<Icon name="cube" size={14} />} onClick={() => setShow3d(!show3d)}>
          {show3d ? "3D on" : "3D view"}
        </LatchButton>
      </div>
      {!hasWork && <p className="rv-meta" role="status">Nothing to check yet. Generate flats in stage 7, or import a drawing below.</p>}

      {/* ---------------------------------------------------------------- import */}
      <Panel profile="well" pad="none" className={`rv-drop${over ? " is-over" : ""}`} aria-label="Import a DXF drawing"
        onDragOver={(e) => { e.preventDefault(); setOver(true); }} onDragLeave={() => setOver(false)} onDrop={onDrop}>
        <div className="rv-row between">
          <b style={{ fontSize: 13, color: "var(--ink-1)" }}>Check your own drawing</b>
          <ProvenanceTag by="engine" kind="derived" source="Layers, units and spaces are read by rules. Unclear ones become decisions." compact />
        </div>
        <p className="rv-meta">Drop a .dxf here, or pick one. Layers and units are read by rules; unclear ones come back as decisions.</p>
        <div>
          <LatchButton size="sm" loading={importing} disabled={anyBusy && !importing} icon={<Icon name="upload" size={14} />} onClick={() => file.current?.click()}>
            {importing ? "Reading…" : "Import DXF"}
          </LatchButton>
          <input ref={file} type="file" accept=".dxf" hidden tabIndex={-1} aria-hidden="true"
            onChange={(e) => { const f = e.target.files?.[0]; e.target.value = ""; void importFile(f); }} />
        </div>
      </Panel>

      {err && !checking && !importing && (
        <div className="rv-alert" role="alert">
          <Icon name="alert" size={16} />
          <div className="rv-alert-body">
            <div className="rv-alert-title">That did not work</div>
            {err}
            <div className="rv-alert-actions">
              {hasWork && <LatchButton size="sm" onClick={() => void runChecks()}>Try the checks again</LatchButton>}
              <LatchButton size="sm" variant="ghost" onClick={() => setErr(null)}>Dismiss</LatchButton>
            </div>
          </div>
        </div>
      )}

      {/* ---------------------------------------------------------------- drawing report */}
      {project?.drawing && (
        <Panel profile="well" pad="none" className="rv-dwg" aria-label="Imported drawing report">
          <div className="rv-row between">
            <div>
              <div className="rv-dwg-name">{project.drawing.filename}</div>
              <div className="rv-dwg-sub">DXF {project.drawing.dxf_version} · {project.drawing.entities.toLocaleString()} entities</div>
            </div>
            <ProvenanceTag by="engine" kind="observed" source="Read from the file" compact />
          </div>
          <div className="rv-grid2">
            <Readout label="Spaces" value={project.drawing.spaces} digits={0} tone="det" size="sm" />
            <Readout label="Flats" value={project.drawing.flats} digits={0} tone="det" size="sm" />
            <Readout label="Outline" value={project.drawing.outline_area} unit="m²" tone="det" size="sm" by="engine" kind="derived" />
            <Readout label="Corners" value={project.drawing.outline_corners} digits={0} tone="det" size="sm" />
          </div>
          <p className="rv-meta">
            Units: file header says <b>{project.drawing.header_units}</b>; the drawing size says <b>{project.drawing.detected_units}</b>.
          </p>
          {project.drawing.warnings.map((w) => (
            <div key={w} className="rv-alert warn" role="note"><Icon name="alert" size={16} /><div className="rv-alert-body">{w}</div></div>
          ))}
          <details className="rv-layers">
            <summary>Layer map ({project.drawing.layers.length})</summary>
            <table>
              <thead><tr><th scope="col">Layer</th><th scope="col">Meaning</th><th scope="col">How</th><th scope="col" style={{ textAlign: "right" }}>Items</th></tr></thead>
              <tbody>
                {project.drawing.layers.map((l) => (
                  <tr key={l.layer}>
                    <td className="mono">{l.layer}</td>
                    <td>{l.meaning}</td>
                    <td className="mute">{l.method}{l.confidence < 1 ? ` ${Math.round(l.confidence * 100)}%` : ""}</td>
                    <td className="num">{l.entities}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </details>
        </Panel>
      )}

      {/* ---------------------------------------------------------------- statutory board */}
      <h3 className="rv-sec">Statutory board</h3>
      {checking && !checks ? (
        <Panel profile="well" pad="md" aria-busy="true" aria-label="Running the checks">
          <Skeleton lines={4} height={14} />
          <p className="rv-meta" role="status" style={{ marginTop: 10 }}>Running the rules on every plan…</p>
        </Panel>
      ) : !checks ? (
        <EmptyState icon={<Icon name="ruler" size={26} />} title="No results yet"
          action={hasWork ? undefined : <LatchButton variant="det" onClick={() => setStep("diagram")}>Go to stage 7</LatchButton>}>
          {hasWork ? "Press Run checks above to see what passes, what needs a look, and what fails." : "Solve a flat or import a drawing, then run the checks."}
        </EmptyState>
      ) : (
        <>
          <Panel profile="well" pad="none" className="rv-board" aria-label="Check results summary">
            <div className="rv-row between">
              <Chip led="det">DCPR 2034.1 · NBC 2016</Chip>
              <ProvenanceTag by="engine" kind="derived" source="Deterministic rule engine. No AI decides a result." compact />
            </div>
            <div className="rv-verdict" role="status"><Led status={verdict.led} size="lg" /><div>{verdict.text}</div></div>
            <div className="rv-counts">
              {([["pass", "PASS", "pass", counts.pass], ["review", "REVIEW", "warn", counts.review], ["fail", "FAIL", "fail", counts.fail]] as const).map(([k, label, tone, n]) => (
                <div key={k} className="rv-count">
                  <div className="rv-count-head"><Led status={n > 0 ? tone : "idle"} />{label}</div>
                  <Readout value={n} digits={0} tone={n > 0 ? tone : "ink"} size="lg" label={undefined} />
                </div>
              ))}
            </div>
            <div className="rv-codes">
              {Object.entries(checks.by_code).map(([code, r]) => (
                <div key={code} className="rv-code neo-flat neo-sm neo-r-md">
                  <div className="rv-row between"><span className="rv-code-name">{code}</span>
                    <Readout value={`${r.pass}/${r.clauses - (r["n/a"] ?? 0)}`} tone={r.fail ? "fail" : r.review ? "warn" : "pass"} size="sm" /></div>
                  <div className="rv-code-chips">
                    <StatusChip status="pass" count={r.pass} />
                    {r.review > 0 && <StatusChip status="review" count={r.review} />}
                    {r.fail > 0 && <StatusChip status="fail" count={r.fail} />}
                  </div>
                </div>
              ))}
            </div>
          </Panel>

          <Tabs<Filter> items={tabs} value={filter} onChange={setFilter} label="Filter results" size="sm" block />

          <Panel profile="well" pad="none" aria-label="Rule results">
            {results.length === 0 ? (
              <div style={{ padding: 14 }}>
                <p className="rv-verdict" role="status" style={{ margin: 0 }}>
                  <Led status={filter === "problems" || filter === "fail" || filter === "review" ? "pass" : "idle"} size="lg" />
                  <span>{filter === "problems" ? "No problems found." : `Nothing under "${filter}".`}</span>
                </p>
                {filter !== "all" && <div style={{ marginTop: 10 }}><LatchButton size="sm" onClick={() => setFilter("all")}>Show all results</LatchButton></div>}
              </div>
            ) : (
              <ul className="rv-list">
                {results.slice(0, MAX_ROWS).map((r) => {
                  const k = keyOf(r);
                  const on = sel === k;
                  const ex = explain[k];
                  const exp = project?.explanations?.[k] ?? (typeof ex === "object" ? ex.explanation : null);
                  return (
                    <li key={`${k}#${r.clause}`} className={`rv-item is-${r.result === "n/a" ? "na" : r.result}${on ? " is-sel" : ""}`}>
                      <button type="button" className="rv-btn" aria-pressed={on} aria-expanded={on}
                        title={r.bbox ? "Show on the plan" : `${r.source} ${r.clause}`} onClick={() => pickRow(r)}>
                        <span className="rv-head">
                          <StatusChip status={r.result} />
                          <span className="rv-title">{r.title}</span>
                        </span>
                        <span className="rv-sub">{r.target} · {r.source} {r.clause} <span className="rv-id">{r.rule_id}</span></span>
                        <span className="rv-vals">
                          <Readout label="Value" value={r.value} unit={r.unit} digits={digitsOf(r.value)} tone={TONE[r.result]} size="sm" />
                          <Readout label="Limit" value={r.threshold} unit={r.unit} digits={digitsOf(r.threshold)} tone="ink" size="sm" />
                        </span>
                      </button>
                      {on && (
                        <div className="rv-detail">
                          <p className="rv-msg">{r.message}</p>
                          <div className="rv-clause">
                            <ProvenanceTag by="engine" kind="derived" source={`${r.source} ${r.clause}`} compact />
                            <span>{r.source} · {r.clause}</span>
                            {typeof ex === "object" && (ex.verified_by_consultant
                              ? <Chip led="det">Clause verified by a consultant</Chip>
                              : <Chip led="idle">Clause not yet verified</Chip>)}
                            {typeof ex === "object" && ex.others > 0 && <Chip>+{ex.others} more for this rule</Chip>}
                          </div>
                          {r.proof.length > 0 && (
                            <ol className="rv-proof" aria-label="How the rule got this result">
                              {r.proof.map((p, i) => <li key={i}>{p}</li>)}
                            </ol>
                          )}
                          {ex === "loading" && <Skeleton lines={2} height={12} />}
                          {exp ? (
                            <div className="rv-explain">
                              <div className="rv-row"><ProvenanceTag by="claude" kind="inferred" source="Written by Claude (T6). The rules decided the result." compact /><Chip led="ai">Check me</Chip></div>
                              <p className="backlit">{exp}</p>
                            </div>
                          ) : r.result === "review" ? (
                            <div className="rv-explain">
                              <div className="rv-row"><Chip led="ai">T6 · Waiting for an explanation</Chip></div>
                              <p>Claude has not explained this item yet. Ask it in chat, or write your own answer in the decisions list.</p>
                              <div className="rv-actions"><LatchButton size="sm" variant="ai" onClick={() => setInbox(true)}>Open decisions</LatchButton></div>
                            </div>
                          ) : null}
                          <div className="rv-actions">
                            <LatchButton size="sm" variant="det" disabled={!r.bbox} onClick={() => { if (show3d) setShow3d(false); onFocus(r); }}>
                              {r.bbox ? "Show on plan" : "No spot on the plan"}
                            </LatchButton>
                            <LatchButton size="sm" variant="ghost" onClick={() => { setSel(null); onFocus(null); }}>Whole floor</LatchButton>
                          </div>
                        </div>
                      )}
                    </li>
                  );
                })}
              </ul>
            )}
            {results.length > MAX_ROWS && <p className="rv-more">Showing {MAX_ROWS} of {fmt(results.length, 0)}. Filter to narrow the list.</p>}
          </Panel>

          <div className="rv-disclaimer"><Icon name="info" size={14} /><span>{checks.disclaimer}</span></div>
        </>
      )}
    </div>
  );
}
