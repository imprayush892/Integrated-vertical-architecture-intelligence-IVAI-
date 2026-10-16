import { useEffect, useMemo, useState } from "react";
import { rpc, subscribe } from "../lib/api";
import { fmt } from "../lib/geom";
import { useStore, waiting } from "../lib/store";
import type { Conflict, Diagram, Plan, Ticket } from "../lib/types";
import {
  Chip, EmptyState, Icon, Led, LatchButton, Panel, Progress, ProvenanceTag, Readout, Select, Skeleton, StatusChip,
  type ReadoutTone,
} from "../ui";

// ------------------------------------------------------------------------------------------- styles
// Local, prefixed (dg-) and token-only so light and dark both work. Injected once.
const CSS = `
.dg { display: flex; flex-direction: column; gap: 14px; min-width: 0; }
.dg-intro { margin: 0; font-size: 13px; line-height: 1.5; color: var(--ink-2); }
.dg-intro b { color: var(--ink-1); }
.dg-sec { font: 600 10.5px var(--f-mono); letter-spacing: 0.12em; text-transform: uppercase; color: var(--ink-3); margin: 0; display: flex; align-items: center; gap: 8px; }
.dg-sec::after { content: ""; flex: 1; height: 1px; background: var(--hairline); }
.dg-row { display: flex; align-items: center; gap: 8px; min-width: 0; flex-wrap: wrap; }
.dg-row.between { justify-content: space-between; }
.dg-grid2 { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px; }
.dg-precheck { display: flex; gap: 10px; align-items: flex-start; padding: 10px 12px; border-radius: var(--r-md); box-shadow: var(--neo-latched); background: var(--neo-bg); border: 1px solid var(--chamfer-inner); font-size: 12.5px; line-height: 1.45; color: var(--ink-1); }
.dg-precheck .led { margin-top: 5px; }
.dg-precheck-body { flex: 1; min-width: 0; overflow-wrap: anywhere; }
.dg-precheck-head { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; margin-bottom: 4px; font: 700 10.5px var(--f-mono); letter-spacing: 0.08em; text-transform: uppercase; color: var(--ink-2); }

/* solver board */
.dg-board { display: flex; flex-direction: column; gap: 12px; }
.dg-solve-actions { display: flex; flex-direction: column; gap: 8px; }
.dg-telemetry { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px; }
.dg-telemetry .lcd { width: 100%; }
.dg-note { margin: 0; font-size: 12px; line-height: 1.5; color: var(--ink-2); }
.dg-note .mono { color: var(--ink-1); }
.dg-meta { margin: 0; font-size: 12px; line-height: 1.45; color: var(--ink-3); }

/* why card */
.dg-why { display: flex; flex-direction: column; gap: 10px; padding: 12px; border-radius: var(--r-md); background: var(--neo-bg); box-shadow: var(--neo-raised-sm); border: 1px solid var(--chamfer); border-left: 4px solid var(--fail); min-width: 0; }
.dg-why-head { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.dg-why-title { margin: 0; font: 700 13.5px var(--f-display); color: var(--fail-ink); display: inline-flex; align-items: center; gap: 6px; }
.dg-why-reason { margin: 0; font-size: 13px; line-height: 1.5; color: var(--ink-1); overflow-wrap: anywhere; }
.dg-why-rooms { display: flex; flex-wrap: wrap; gap: 6px; }
.dg-ladder { margin: 0; padding: 0; list-style: none; display: flex; flex-direction: column; gap: 8px; counter-reset: rung; }
.dg-rung { display: grid; grid-template-columns: 24px minmax(0, 1fr) auto; gap: 8px; align-items: center; padding: 8px 10px; border-radius: var(--r-md); box-shadow: var(--neo-latched); border: 1px solid var(--chamfer-inner); background: var(--neo-bg); counter-increment: rung; }
.dg-rung::before { content: counter(rung); font: 700 11px var(--f-mono); color: var(--ink-2); width: 22px; height: 22px; display: grid; place-items: center; border-radius: 50%; box-shadow: var(--neo-raised-xs); background: var(--neo-convex-bg); }
.dg-rung.is-bad { opacity: 0.85; }
.dg-rung-label { font-size: 12.5px; line-height: 1.4; color: var(--ink-1); overflow-wrap: anywhere; }
.dg-rung-tags { display: flex; gap: 6px; flex-wrap: wrap; margin-top: 4px; }
.dg-rung-act { display: flex; align-items: center; }
.dg-ticket { display: flex; flex-direction: column; gap: 10px; padding: 12px; border-radius: var(--r-md); background: var(--neo-bg); box-shadow: var(--neo-raised-sm); border: 1px solid var(--chamfer); border-left: 4px solid var(--ai); min-width: 0; }
.dg-ticket-q { margin: 0; font-size: 13px; line-height: 1.5; color: var(--ink-1); }
.dg-ticket-opts { display: flex; flex-direction: column; gap: 8px; }
.dg-ticket-opts .ui-btn { text-align: left; }
.dg-alert { display: flex; gap: 10px; align-items: flex-start; font-size: 12.5px; line-height: 1.45; color: var(--ink-1); padding: 10px 12px; border-radius: var(--r-md); background: var(--neo-bg); box-shadow: var(--neo-latched); border: 1px solid var(--chamfer-inner); border-left: 3px solid var(--fail); }
.dg-alert > svg { flex: none; margin-top: 1px; color: var(--fail-ink); }
.dg-alert-body { flex: 1; min-width: 0; overflow-wrap: anywhere; }
.dg-alert-title { font-weight: 700; color: var(--fail-ink); }
.dg-alert-actions { margin-top: 8px; display: flex; gap: 8px; }
.dg-result { display: flex; flex-direction: column; gap: 10px; padding: 12px; }
.dg-result-list { margin: 0; padding: 0; list-style: none; display: flex; flex-direction: column; gap: 6px; }
.dg-result-list li { display: flex; align-items: center; justify-content: space-between; gap: 8px; font-size: 12px; color: var(--ink-1); }
.dg-result-list .mono { color: var(--ink-2); }
.dg-fixnote { margin: 0; font-size: 12.5px; line-height: 1.5; color: var(--ink-1); }
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

// ------------------------------------------------------------------------------------------- engine contracts
/** Mirrors the engine's solve_unit result. */
interface SolveOut {
  unit_id: string;
  status: string;
  plan: Plan | null;
  message: string;
  applied_fix: { label?: string } | null;
  time_s: number;
  conflicts: Conflict[];
}
interface SolveAllOut {
  solved: number;
  total: number;
  time_s: number;
  units: { unit_id: string; status: string; message: string; applied_fix: { label?: string } | null; time_s: number }[];
}
type Running = { kind: "one" | "all"; label: string; t0: number } | null;

// Engine defaults (solver.py): time cap 7.5 s; the search stops early when the best layout has not improved
// for min(2.0, max(0.5, cap / 4)) seconds. They are shown as the budget, not measured.
const TIME_CAP_S = 7.5;
const STALL_S = Math.min(2.0, Math.max(0.5, TIME_CAP_S * 0.25));

const STATUS_TEXT: Record<string, { text: string; tone: ReadoutTone; hint: string }> = {
  optimal: { text: "OPTIMAL", tone: "det", hint: "The best possible layout was found." },
  feasible: { text: "FEASIBLE", tone: "det", hint: "A valid layout was found; the search stopped when it stopped improving." },
  timeout: { text: "TIME CAP", tone: "ink", hint: "The time cap was reached; this is the best layout so far." },
  infeasible: { text: "INFEASIBLE", tone: "fail", hint: "No layout can meet every requirement at once." },
  imported: { text: "IMPORTED", tone: "ink", hint: "Read from a drawing, not solved." },
};

// A fix the app can apply straight onto the diagram (the rest are applied by the solver itself).
type RawFix = { action?: string; label: string; valid?: boolean; room?: string; a?: string; b?: string };
type DiagramEdit = { rooms: Diagram["rooms"]; links: Diagram["links"]; entry?: string };
function diagramEdit(fix: RawFix, d: Diagram): DiagramEdit | null {
  const same = ([x, y]: [string, string], a?: string, b?: string) => (x === a && y === b) || (x === b && y === a);
  const has = (id?: string) => !!id && d.rooms.some((r) => r.id === id);
  switch (fix.action) {
    case "drop_room":
      return has(fix.room) ? { rooms: d.rooms.map((r) => (r.id === fix.room ? { ...r, placed: false } : r)), links: d.links } : null;
    case "no_window":
      return has(fix.room) ? { rooms: d.rooms.map((r) => (r.id === fix.room ? { ...r, needs_window: false } : r)), links: d.links } : null;
    case "add_link":
      return has(fix.a) && has(fix.b) && !d.links.some((l) => same(l, fix.a, fix.b))
        ? { rooms: d.rooms, links: [...d.links, [fix.a as string, fix.b as string]] } : null;
    case "remove_link":
      return d.links.some((l) => same(l, fix.a, fix.b)) ? { rooms: d.rooms, links: d.links.filter((l) => !same(l, fix.a, fix.b)) } : null;
    case "set_entry":
      return has(fix.room) && d.entry !== fix.room ? { rooms: d.rooms, links: d.links, entry: fix.room } : null;
    default:
      return null;
  }
}

const errText = (e: unknown) => (e instanceof Error ? e.message : String(e));

// ------------------------------------------------------------------------------------------- 7 Diagram
export function DiagramPanel({ diagram, onSolved }: { diagram: Diagram | null; onSolved: () => void }) {
  useInjectedCss("dg-styles", CSS);
  const project = useStore((s) => s.project);
  const tickets = useStore((s) => s.tickets);
  const unitId = useStore((s) => s.unit);
  const setUnit = useStore((s) => s.setUnit);
  const run = useStore((s) => s.run);
  const busy = useStore((s) => s.busy);
  const toast = useStore((s) => s.toast);
  const setStep = useStore((s) => s.setStep);
  const setInbox = useStore((s) => s.setInbox);
  const units = project?.units ?? [];
  const u = units.find((x) => x.id === unitId) ?? units[0];

  const [running, setRunning] = useState<Running>(null);
  const [now, setNow] = useState(0);
  const [doneFlats, setDoneFlats] = useState(0);
  const [last, setLast] = useState<SolveOut | null>(null);
  const [lastAll, setLastAll] = useState<SolveAllOut | null>(null);
  const [err, setErr] = useState<string | null>(null);

  // forget the last result when another flat is picked
  useEffect(() => { setLast(null); setErr(null); }, [u?.id]);

  // live clock while the solver runs
  useEffect(() => {
    if (!running) return;
    setNow(performance.now());
    const t = window.setInterval(() => setNow(performance.now()), 100);
    return () => window.clearInterval(t);
  }, [running]);

  // "all flats": the engine emits a `solved` event per flat; count them for a real progress bar
  useEffect(() => {
    if (running?.kind !== "all") return;
    setDoneFlats(0);
    return subscribe((ev) => { if (ev.kind === "solved") setDoneFlats((n) => n + 1); });
  }, [running?.kind]);

  const plan = project?.plans.find((p) => p.unit_id === u?.id);
  const placed = diagram?.rooms.filter((r) => r.placed).length ?? 0;
  const need = useMemo(() => (diagram?.rooms ?? []).filter((r) => r.placed).reduce((a, r) => a + r.target_area, 0), [diagram]);
  const roomName = (id: string) => diagram?.rooms.find((r) => r.id === id)?.name ?? id;
  const t3 = useMemo(() => waiting(tickets).filter((t) => t.kind === "T3" && u && t.subject === u.id), [tickets, u]);
  const anyBusy = busy != null || running != null;

  if (!u) {
    return (
      <div className="dg">
        <EmptyState icon={<Icon name="grid" size={26} />} title="No flats yet"
          action={<LatchButton variant="primary" onClick={() => setStep("units")}>Split the plate</LatchButton>}>
          Split the floor plate into flats, then wire their rooms here.
        </EmptyState>
      </div>
    );
  }

  const solveOne = async () => {
    setErr(null); setLast(null); setLastAll(null);
    setRunning({ kind: "one", label: `Solving ${u.id}`, t0: performance.now() });
    const r = await run(`Solving ${u.id}`, async () => {
      try {
        const out = await rpc<SolveOut>("solve_unit", { unit_id: u.id });
        setLast(out);
        return out;
      } catch (e) { setErr(errText(e)); throw e; }
    });
    setRunning(null);
    if (r?.plan && !r.applied_fix) { toast("ok", `${u.id} solved in ${fmt(r.time_s)} s.`); onSolved(); }
    // a fix was applied to get a plan: stay, so the change is visible and can be confirmed or undone
    else if (r?.plan) toast("info", `${u.id} solved after a fix. Review it below.`);
  };
  const solveAll = async () => {
    setErr(null); setLast(null); setLastAll(null);
    setRunning({ kind: "all", label: "Solving all flats", t0: performance.now() });
    const r = await run("Solving all flats", async () => {
      try {
        const out = await rpc<SolveAllOut>("solve_all", {});
        setLastAll(out);
        return out;
      } catch (e) { setErr(errText(e)); throw e; }
    });
    setRunning(null);
    if (r && r.solved > 0) { toast("ok", `${r.solved} of ${r.total} flats solved.`); setStep("review"); }
    else if (r) toast("error", "No flat could be solved. See why below.");
  };
  const applyFix = (fix: RawFix) => {
    if (!diagram) return;
    const d = diagramEdit(fix, diagram);
    if (!d) return;
    void run("Applying fix", () => rpc("update_diagram", { unit_id: diagram.unit_id, rooms: d.rooms, links: d.links, ...(d.entry ? { entry: d.entry } : {}) }));
  };
  const answer = (t: Ticket, optionId: string) =>
    void run("Applying answer", () => rpc("answer_decision", { ticket_id: t.id, option_id: optionId, reason: "picked in the app", by: "user" }));

  // ---- solver telemetry
  const cap = TIME_CAP_S;
  const elapsed = running ? Math.max(0, (now - running.t0) / 1000) : null;
  const solvingOne = running?.kind === "one";
  const stateKey = last?.status ?? plan?.status;
  const explaining = solvingOne && (elapsed ?? 0) > cap + 0.5; // past the cap: the engine is explaining an infeasible flat
  const st = running ? { text: explaining ? "EXPLAINING" : "SEARCHING", tone: "cobalt" as ReadoutTone, hint: "" } : stateKey ? STATUS_TEXT[stateKey] ?? { text: stateKey.toUpperCase(), tone: "ink" as ReadoutTone, hint: "" } : { text: "IDLE", tone: "ink" as ReadoutTone, hint: "" };
  const shownTime = running ? elapsed : last ? last.time_s : plan?.solve_time_s ?? null;
  const progress = running?.kind === "all" ? Math.min(1, doneFlats / Math.max(1, units.length)) : running && !explaining ? Math.min(0.97, (elapsed ?? 0) / cap) : undefined;

  // ---- why-cards: what the pre-check says now, plus what the last solve could not do
  const solveFailed = last != null && !last.plan;
  const whyConflicts: Conflict[] = solveFailed && last.conflicts.length ? last.conflicts : diagram?.conflicts ?? [];
  const missingList = diagram?.conflicts.length ?? 0;

  return (
    <div className="dg">
      <p className="dg-intro">Link the rooms, then the <b>rule engine</b> packs them into the flat.</p>

      <Select label="Flat" value={u.id} onChange={(e) => setUnit(e.target.value)} disabled={anyBusy}
        options={units.map((x) => ({ value: x.id, label: `${x.id} · ${x.programme?.bhk ?? "?"} BHK${project?.plans.some((p) => p.unit_id === x.id) ? " · planned" : ""}` }))} />

      {!u.programme ? (
        <EmptyState icon={<Icon name="layers" size={26} />} title="No room list for this flat"
          action={<LatchButton variant="primary" onClick={() => setStep("program")}>Pick a room list</LatchButton>}>
          Choose a programme first. Its rooms and links become the diagram.
        </EmptyState>
      ) : !diagram ? (
        <Panel profile="well" pad="md" aria-busy="true" aria-label="Loading the room diagram">
          <Skeleton lines={3} height={14} />
          <p className="dg-meta" role="status" style={{ marginTop: 10 }}>Reading the room graph for {u.id}…</p>
        </Panel>
      ) : (
        <>
          <Panel profile="well" pad="md" aria-label="Diagram figures" title="Figures"
            actions={<ProvenanceTag by="engine" kind="derived" source="Flat area and the sum of target areas of placed rooms" compact />}>
            <div className="dg-grid2">
              <Readout label="Flat area" value={u.area} unit="m²" tone="det" />
              <Readout label="Rooms need" value={need} unit="m²" tone="det" />
              <Readout label="Placed" value={`${placed} / ${diagram.rooms.length}`} tone="ink" />
              <Readout label="Links" value={diagram.links.length} digits={0} tone="ink" />
            </div>
          </Panel>

          <div className="dg-precheck" role="status" aria-live="polite">
            <Led status={missingList ? "fail" : "det"} />
            <div className="dg-precheck-body">
              <div className="dg-precheck-head">
                Pre-check
                <ProvenanceTag by="engine" kind="derived" source="Diagram rules: area, windows, links, entry" compact />
              </div>
              {diagram.message || "Edit the diagram on the canvas and the rules re-check it."}
            </div>
          </div>

          {whyConflicts.length > 0 && (
            <>
              <h3 className="dg-sec">{solveFailed ? "Why it did not solve" : "Why it will struggle"}</h3>
              {whyConflicts.map((c) => (
                <WhyCard key={c.id} c={c} roomName={roomName} diagram={diagram} onApply={applyFix} disabled={anyBusy} />
              ))}
            </>
          )}

          {t3.map((t) => (
            <section key={t.id} className="dg-ticket" aria-label="Decision T3, room clash">
              <div className="dg-why-head">
                <Chip led="ai">T3 · Room clash</Chip>
                {t.check_me && <Chip led="ai">Check me</Chip>}
                <ProvenanceTag by="engine" kind="derived" source="Fixes were checked by the solver before they were offered" compact />
              </div>
              <p className="dg-ticket-q">{t.question}</p>
              <div className="dg-ticket-opts">
                {t.options.slice(0, 4).map((o) => (
                  <LatchButton key={o.id} variant="ai" block disabled={!o.valid || anyBusy}
                    sub={!o.valid ? "Does not work here" : o.id === t.default_option ? "Safe default" : "Verified by the solver"}
                    onClick={() => answer(t, o.id)}>
                    {o.label.length > 90 ? `${o.label.slice(0, 88)}…` : o.label}
                  </LatchButton>
                ))}
              </div>
              <p className="dg-meta">Claude can also answer this in chat. Its pick is re-checked by the rules.</p>
            </section>
          ))}
        </>
      )}

      {/* ---------------------------------------------------------------- solver board */}
      <h3 className="dg-sec">Solver</h3>
      <div className="dg-board">
        <div className="dg-solve-actions">
          <LatchButton variant="primary" size="lg" block latched={solvingOne} loading={solvingOne}
            disabled={!diagram || (anyBusy && !solvingOne) || !u.programme} onClick={() => void solveOne()}
            sub={solvingOne ? (explaining ? "Explaining why it does not fit" : "Searching for a layout") : `Fit ${u.id} into ${fmt(u.area)} m²`}>
            {solvingOne ? "Solving…" : "Generate the plan"}
          </LatchButton>
          <LatchButton variant="det" block latched={running?.kind === "all"} loading={running?.kind === "all"}
            disabled={(anyBusy && running?.kind !== "all") || units.length === 0} onClick={() => void solveAll()}>
            {running?.kind === "all" ? `Solving… ${doneFlats} of ${units.length}` : `Generate all ${units.length} flats`}
          </LatchButton>
        </div>

        {running && (
          <Progress label={running.label} showLabel tone="det" value={progress} />
        )}

        <Panel profile="well" pad="md" title="CP-SAT telemetry" aria-label="Solver telemetry"
          actions={<ProvenanceTag by="engine" kind={running ? "observed" : "derived"} source="OR-Tools CP-SAT. Elapsed is timed in the app; the cap and stall limits are the engine defaults." compact />}>
          <div className="dg-telemetry">
            <Readout label="Status" value={st.text} tone={st.tone} />
            <Readout label={running ? "Elapsed" : "Last solve"} value={shownTime} unit="s" tone="det" />
            <Readout label="Time cap" value={cap} unit="s" tone="ink" />
            <Readout label="Stall stop" value={STALL_S} unit="s" tone="ink" />
          </div>
        </Panel>
        <p className="dg-note" role="status">
          {explaining
            ? <>No layout fits. Finding the smallest set of requirements that clash…</>
            : running
            ? <>Stops at the <span className="mono">{fmt(cap)} s</span> cap, or after <span className="mono">{fmt(STALL_S)} s</span> without a better layout.</>
            : st.hint || <>The search stops early when the layout stops improving, or at the time cap.</>}
        </p>

        {err && !running && (
          <div className="dg-alert" role="alert">
            <Icon name="alert" size={16} />
            <div className="dg-alert-body">
              <div className="dg-alert-title">The solver could not run</div>
              {err}
              <div className="dg-alert-actions"><LatchButton size="sm" onClick={() => void solveOne()}>Try again</LatchButton></div>
            </div>
          </div>
        )}

        {/* result of the last single solve */}
        {last && !running && (
          <Panel profile="well" pad="none" className="dg-result" aria-label="Solve result">
            <div className="dg-row between">
              <span className="dg-row" style={{ gap: 6 }}>
                <Led status={last.plan ? "det" : "fail"} />
                <b style={{ fontSize: 13, color: "var(--ink-1)" }}>{last.plan ? "Plan ready" : "No plan"}</b>
              </span>
              <ProvenanceTag by="engine" kind="derived" source="OR-Tools CP-SAT, then the rule engine" compact />
            </div>
            <p className="dg-fixnote">{last.message}</p>
            {last.applied_fix?.label && (
              <p className="dg-fixnote"><Chip led="det">Fix applied</Chip> {last.applied_fix.label}. Check it in the decisions list.</p>
            )}
            {last.plan && <Readout label="Carpet" value={last.plan.carpet_area} unit="m²" tone="det" by="engine" kind="derived" layout="inline" />}
            {last.plan && <LatchButton size="sm" variant="det" icon={<Icon name="check" size={14} />} onClick={() => setStep("review")}>Check this plan</LatchButton>}
          </Panel>
        )}

        {/* result of "all flats" */}
        {lastAll && !running && (
          <Panel profile="well" pad="none" className="dg-result" aria-label="Results for all flats">
            <div className="dg-row between">
              <b style={{ fontSize: 13, color: "var(--ink-1)" }}>{lastAll.solved} of {lastAll.total} flats solved</b>
              <Readout value={lastAll.time_s} unit="s" tone="det" size="sm" layout="inline" />
            </div>
            <ul className="dg-result-list">
              {lastAll.units.map((x) => (
                <li key={x.unit_id}>
                  <span><b>{x.unit_id}</b> <span className="mono">{x.status}</span></span>
                  <Chip led={x.status === "infeasible" ? "fail" : "det"}>{fmt(x.time_s)} s</Chip>
                </li>
              ))}
            </ul>
          </Panel>
        )}

        {/* the stored plan for this flat (from an earlier solve) */}
        {!running && !last && plan && (
          <Panel profile="well" pad="none" className="dg-result" aria-label="Stored plan">
            <div className="dg-row between">
              <b style={{ fontSize: 13, color: "var(--ink-1)" }}>Plan on file</b>
              <ProvenanceTag by="engine" kind="derived" compact />
            </div>
            <div className="dg-grid2">
              <Readout label="Carpet" value={plan.carpet_area} unit="m²" tone="det" />
              <Readout label="Solve time" value={plan.solve_time_s} unit="s" tone="det" />
            </div>
            <LatchButton size="sm" variant="det" icon={<Icon name="check" size={14} />} onClick={() => setStep("review")}>Check this plan</LatchButton>
          </Panel>
        )}
        {!running && !last && !plan && !err && diagram && (
          <p className="dg-meta">No plan yet for {u.id}. Generate it when the pre-check looks right.</p>
        )}

        {t3.length > 0 && (
          <LatchButton size="sm" variant="ai" pulse onClick={() => setInbox(true)}>Open decisions ({t3.length} waiting)</LatchButton>
        )}
      </div>
    </div>
  );
}

// ------------------------------------------------------------------------------------------- why card
function WhyCard({ c, roomName, diagram, onApply, disabled }: {
  c: Conflict; roomName: (id: string) => string; diagram: Diagram; onApply: (f: RawFix) => void; disabled: boolean;
}) {
  const fixes = c.fixes as RawFix[];
  return (
    <article className="dg-why" aria-label={`Clash: ${c.kind}`}>
      <div className="dg-why-head">
        <h4 className="dg-why-title"><Icon name="alert" size={15} />Why</h4>
        <StatusChip status="fail">{c.kind.toUpperCase()}</StatusChip>
        <ProvenanceTag by="engine" kind="derived" source="Found by the diagram rules and the solver, not by Claude" compact />
      </div>
      <p className="dg-why-reason">{c.reason}</p>
      {c.rooms.length > 0 && (
        <div className="dg-why-rooms" aria-label="Rooms involved">
          {c.rooms.slice(0, 6).map((id) => <Chip key={id} led="fail">{roomName(id)}</Chip>)}
          {c.rooms.length > 6 && <Chip>+{c.rooms.length - 6} more</Chip>}
        </div>
      )}
      {fixes.length > 0 && (
        <>
          <h4 className="dg-sec">Ways to fix it</h4>
          <ol className="dg-ladder">
            {fixes.map((f, i) => {
              const edit = diagramEdit(f, diagram);
              const bad = f.valid === false;
              return (
                <li key={`${f.label}-${i}`} className={`dg-rung${bad ? " is-bad" : ""}`}>
                  <div>
                    <div className="dg-rung-label">{f.label}</div>
                    <div className="dg-rung-tags">
                      {bad ? <Chip led="idle">Would not work</Chip> : f.valid ? <Chip led="det">Checked: works</Chip> : null}
                      {!edit && !bad && <Chip>Applied by the solver</Chip>}
                    </div>
                  </div>
                  <div className="dg-rung-act">
                    {edit && !bad && <LatchButton size="sm" variant="det" disabled={disabled} onClick={() => onApply(f)}>Apply</LatchButton>}
                  </div>
                </li>
              );
            })}
          </ol>
        </>
      )}
    </article>
  );
}
