import { useEffect, useMemo, useState } from "react";
import { focusOption } from "../components/MapView";
import { rpc } from "../lib/api";
import { fmt, pct } from "../lib/geom";
import { useStore } from "../lib/store";
import type { PlacementOption } from "../lib/types";
import { Chip, EmptyState, Icon, LatchButton, Panel, Progress, ProvenanceTag, Readout, Skeleton, StatusChip } from "../ui";
import "./stage14.css";

/** The five trade-off criteria of the placement search (engine score terms, 0 to 1, higher is better). */
const CRITERIA: { key: string; label: string; hint: string }[] = [
  { key: "coverage", label: "Coverage", hint: "plot used" },
  { key: "gap", label: "Gap", hint: "between towers" },
  { key: "breeze", label: "Breeze", hint: "faces the wind" },
  { key: "view", label: "View", hint: "faces the view" },
  { key: "west_glass", label: "West glass", hint: "less is better" },
];
const OPTION_NAME: Record<string, string> = { 1: "Single tower", 2: "Twin towers", 3: "Three towers" };
const nice = (k: string) => k.replace(/_/g, " ");
const cap = (s: string) => s.charAt(0).toUpperCase() + s.slice(1);

// ------------------------------------------------------------------------------------------- 4 Place
export function PlacePanel() {
  const pl = useStore((s) => s.project?.placement);
  const site = useStore((s) => s.project?.site);
  const run = useStore((s) => s.run);
  const setStep = useStore((s) => s.setStep);
  const setInbox = useStore((s) => s.setInbox);
  const tickets = useStore((s) => s.tickets);
  const busy = useStore((s) => s.busy);
  const [picked, setPicked] = useState<string | null>(null);
  const [committing, setCommitting] = useState(false);

  const options = pl?.options ?? [];
  const best = useMemo(() => options.reduce<PlacementOption | null>((b, o) => (!b || o.score > b.score ? o : b), null), [options]);
  const currentId = picked && options.some((o) => o.id === picked) ? picked : pl?.chosen ?? best?.id ?? null;
  const cur = options.find((o) => o.id === currentId) ?? null;

  // highlight the picked option on the map (stage 4 map), and clear it when this panel goes away
  useEffect(() => { focusOption(currentId); }, [currentId]);
  useEffect(() => () => focusOption(null), []);

  const placing = busy === "Placing towers";

  if (placing) {
    return (
      <div className="s14">
        <Panel profile="well" pad="sm" className="s14-busy" role="status" aria-live="polite">
          <Progress label="Trying every angle and tower count" showLabel />
          <Skeleton height={44} radius={12} />
          <Skeleton height={44} radius={12} />
          <Skeleton height={44} radius={12} />
        </Panel>
        <p className="s14-meta">Every angle in 1° steps and every plate size that fits is being tried.</p>
      </div>
    );
  }

  if (!pl || options.length === 0) {
    return (
      <div className="s14">
        <EmptyState icon={<Icon name="cube" size={26} />} title={pl ? "No tower fits" : "No options yet"}
          action={site
            ? <LatchButton variant="det" onClick={() => void run("Placing towers", () => rpc("place_towers"))}>{pl ? "Search again" : "Find options"}</LatchButton>
            : <LatchButton variant="primary" onClick={() => setStep("trace")}>Trace the plot</LatchButton>}>
          {pl ? "No tower fits the buildable area with the plate limits. Try a smaller setback, a lower height or a bigger plot."
            : site ? "The rules will try every angle and tower count on your buildable area and score them." : "Trace the plot first, then the rules can place towers on it."}
        </EmptyState>
      </div>
    );
  }

  const ticket = cur ? tickets.find((x) => x.id === `T5:${cur.id}`) : undefined;
  const byClaude = ticket?.answer?.by === "claude" && (ticket.status === "answered" || ticket.status === "kept");
  const byUser = ticket?.answer?.by === "user" && (ticket.status === "answered" || ticket.status === "kept") && ticket.answer?.text;
  const needsCheck = ticket?.status === "answered" && byClaude;

  const commit = async () => {
    if (!cur) return;
    if (pl.chosen === cur.id) { setStep("shape"); return; }
    setCommitting(true);
    const ok = await run("Shaping the floor", () => rpc("choose_placement", { option: cur.id }));
    setCommitting(false);
    if (ok) setStep("shape");
  };

  const sameAll = (key: string) => options.every((o) => Math.abs((o.score_terms[key] ?? 0) - (options[0].score_terms[key] ?? 0)) < 1e-6);
  // a term that is the same neutral 0.5 everywhere means the rules had no input for it
  const noInput = (key: string) => (key === "view" || key === "breeze") && options.every((o) => Math.abs((o.score_terms[key] ?? 0) - 0.5) < 1e-6);

  return (
    <div className="s14">
      <p className="s14-intro">Best option per tower count, found by trying every angle. Latch one to see it on the map.</p>

      <Panel profile="well" pad="none" aria-label="Trade-off matrix">
        <table className="s14-mx">
          <caption className="sr-only">Trade-off matrix: score per criterion for each placement option, 0 to 100, higher is better</caption>
          <thead>
            <tr>
              <th scope="col" className="s14-mx-corner">Option</th>
              {options.map((o) => (
                <th key={o.id} scope="col" className={o.id === currentId ? "is-sel" : undefined}>
                  <LatchButton className="s14-mx-head" latched={o.id === currentId} onClick={() => setPicked(o.id)}
                    led={o.id === pl.chosen ? "det" : o.id === currentId ? "cobalt" : "idle"}
                    aria-label={`Option ${o.id}, ${OPTION_NAME[o.towers] ?? o.label}${o.id === pl.chosen ? ", in use" : ""}`}>
                    {o.id}
                  </LatchButton>
                  <span className="s14-mx-sub">{o.towers === 1 ? "1 tower" : `${o.towers} towers`}</span>
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {CRITERIA.map((c) => {
              const vals = options.map((o) => o.score_terms[c.key] ?? 0);
              const unknown = noInput(c.key);
              const hi = Math.max(...vals);
              return (
                <tr key={c.key}>
                  <th scope="row" className="s14-mx-label">{c.label}<small>{unknown ? "no input yet" : c.hint}</small></th>
                  {options.map((o, i) => {
                    const v = vals[i];
                    const isBest = !unknown && !sameAll(c.key) && v >= hi - 1e-9;
                    return (
                      <td key={o.id} className={o.id === currentId ? "is-sel" : undefined}>
                        <div className="s14-cell">
                          <span className={`s14-meter${isBest ? " is-best" : ""}${unknown ? " is-unknown" : ""}`} aria-hidden="true"><i style={{ width: `${Math.round(v * 100)}%` }} /></span>
                          <span className={`s14-cell-num${isBest ? " is-best" : ""}${unknown ? " is-unknown" : ""}`}
                            aria-label={unknown ? `${c.label}, option ${o.id}: unknown` : `${c.label}, option ${o.id}: ${Math.round(v * 100)} of 100${isBest ? ", best" : ""}`}>
                            {unknown ? "–" : Math.round(v * 100)}
                          </span>
                        </div>
                      </td>
                    );
                  })}
                </tr>
              );
            })}
          </tbody>
          <tfoot>
            <tr>
              <th scope="row" className="s14-mx-label">Score<small>weighted</small></th>
              {options.map((o) => (
                <td key={o.id} className={`s14-mx-total${o.id === currentId ? " is-sel" : ""}`}>
                  <output className={`lcd ${best && o.id === best.id ? "det" : "ink"}`} aria-label={`Option ${o.id} total score ${(o.score * 100).toFixed(1)} of 100`}>{(o.score * 100).toFixed(1)}</output>
                </td>
              ))}
            </tr>
          </tfoot>
        </table>
      </Panel>
      <p className="s14-meta">Scores are 0 to 100 from the rule engine. Higher is better.{noInput("view") ? " View shows – until a sea view is set in stage 3." : ""}</p>

      {cur && (
        <Panel profile="flat" small pad="sm" className="s14-detail" aria-live="polite"
          title={`Option ${cur.id}`}
          actions={<span className="s14-row">
            <ProvenanceTag by="engine" kind="derived" compact source="Numbers on this card come from the rule engine" />
            {best && cur.id === best.id && <Chip led="det">Top score</Chip>}
            {pl.chosen === cur.id && <Chip led="cobalt" inset>In use</Chip>}
          </span>}>
          <div className="s14-grid2">
            <Readout label="Coverage" value={cur.coverage * 100} unit="%" digits={1} tone="det" size="sm" />
            <Readout label="Gap" value={cur.min_gap == null ? "n/a" : cur.min_gap} unit={cur.min_gap == null ? undefined : "m"} tone="det" size="sm" />
            <Readout label={cur.plates.length > 1 ? "Each plate" : "Plate"} value={fmt(cur.plates[0]?.area, 0)} unit="m²" tone="det" size="sm" />
            <Readout label="Axis" value={cur.plates[0]?.angle_deg} unit="°" digits={0} tone="det" size="sm" />
          </div>
          <p className="s14-meta"><b>{OPTION_NAME[cur.towers] ?? cap(cur.label)}.</b>{cur.plates[0] && <> Each plate {fmt(cur.plates[0].width)} × {fmt(cur.plates[0].depth)} m. Covers {pct(cur.coverage)} of the plot.</>}</p>

          <div className="s14-pass-row" role="list" aria-label="Rule checks">
            {Object.entries(cur.passes).map(([k, v]) => <span role="listitem" key={k}><StatusChip status={v ? "pass" : "fail"}>{nice(k)}</StatusChip></span>)}
          </div>

          <div className="neo-well neo-sm s14-driver" aria-label="Design drivers">
            <div className="s14-row between wrap">
              <span className="eyebrow">Design drivers</span>
              {byClaude ? <ProvenanceTag by="claude" kind="inferred" source="Written by Claude, re-checked by the rules" />
                : byUser ? <ProvenanceTag by="user" kind="observed" />
                : <ProvenanceTag by="engine" kind="derived" source="Template sentence from the score terms" />}
            </div>
            <p className={`s14-driver-text ${byClaude ? "is-claude" : "is-rule"}`}>{cur.drivers || "No note yet."}</p>
            {needsCheck && <p className="s14-meta">Claude wrote this. Check it against the scores above.</p>}
            <div className="s14-driver-actions">
              {needsCheck && ticket && (
                <>
                  <LatchButton size="sm" variant="ai" onClick={() => void run("Keeping", () => rpc("review_decision", { ticket_id: ticket.id, keep: true }))}>Keep</LatchButton>
                  <LatchButton size="sm" onClick={() => void run("Undoing", () => rpc("review_decision", { ticket_id: ticket.id, keep: false }))}>Undo</LatchButton>
                </>
              )}
              {ticket && (ticket.status === "open" || ticket.status === "defaulted") && (
                <LatchButton size="sm" variant="ghost" onClick={() => setInbox(true)}>Rewrite in the inbox</LatchButton>
              )}
            </div>
          </div>
        </Panel>
      )}

      <div>
        <LatchButton variant="primary" size="lg" block loading={committing} disabled={!cur} icon={<Icon name="layers" />} onClick={() => void commit()}>
          {cur ? `Use option ${cur.id} and shape the floor` : "Pick an option"} <Icon name="chevron-right" />
        </LatchButton>
        <p className="s14-why" style={{ marginTop: 8 }}>Searched in <b>{fmt(pl.elapsed_s, 1)} s</b>. Changing the choice later redoes the stages after it.</p>
        <div style={{ marginTop: 8 }}>
          <LatchButton size="sm" variant="ghost" onClick={() => void run("Placing towers", () => rpc("place_towers"))}>Search again</LatchButton>
        </div>
      </div>
    </div>
  );
}
