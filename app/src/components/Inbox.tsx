import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { rpc } from "../lib/api";
import { useStore, waiting } from "../lib/store";
import type { Ticket, TicketOption } from "../lib/types";
import { STAGES } from "../shell/stages";
import {
  Chip, EmptyState, Field, Icon, IconButton, Kbd, LatchButton, Led, type LedStatus, Panel, ProvenanceTag, Readout, Skeleton, Tabs,
} from "../ui";
import "./agent.css";
import ClaudeLink, { CopyButton } from "./ClaudeLink";

/** Plain-words label for each ticket kind (T0-T8). */
const KIND_LABEL: Record<string, string> = {
  T0: "Site surroundings", T1: "CAD layer", T2: "Drawing units", T3: "Room clash", T4: "Room name",
  T5: "Placement note", T6: "Explain a check", T7: "Edit in words", T8: "Brief to rooms",
};

const ASK_CLAUDE = "Answer the waiting Apartment Planner decisions, with a short reason for each.";

type Filter = "needs" | "defaulted" | "answered" | "all";

/** Needs a human look: nothing chosen yet, a default awaiting a glance, or Claude's answer awaiting Keep/Undo. */
const needsMe = (t: Ticket) => t.status === "open" || t.status === "defaulted" || (t.status === "answered" && t.check_me);
const isAnswered = (t: Ticket) => t.status === "answered" || t.status === "kept" || t.status === "undone";
const isSettled = (t: Ticket) => t.status === "kept" || t.status === "undone";
const rank = (t: Ticket) => (t.status === "open" ? 0 : t.status === "answered" && t.check_me ? 1 : t.status === "defaulted" ? 2 : t.status === "answered" ? 3 : 4);

const FILTERS: Record<Filter, (t: Ticket) => boolean> = {
  needs: needsMe,
  defaulted: (t) => t.status === "defaulted",
  answered: isAnswered,
  all: () => true,
};

const stageText = (n: number) => { const s = STAGES[n - 1]; return s ? `Stage ${n} · ${s.label}` : `Stage ${n}`; };
const optionLabel = (t: Ticket, id: string | null | undefined) => (id ? t.options.find((o) => o.id === id)?.label ?? id : null);
const clip = (s: string, n: number) => (s.length > n ? `${s.slice(0, n - 1)}…` : s);
const isMore = (o: TicketOption) => o.id === "...";

interface CardState { word: string; led: LedStatus; pulse: boolean; cls: "needs" | "ok" | "settled" }
function cardState(t: Ticket): CardState {
  switch (t.status) {
    case "open": return { word: "Needs you", led: "ai", pulse: true, cls: "needs" };
    case "defaulted": return { word: "Default on", led: "ai", pulse: false, cls: "needs" };
    case "answered": return t.check_me
      ? { word: "Claude answered", led: "ai", pulse: true, cls: "needs" }
      : { word: "Answered", led: "cobalt", pulse: false, cls: "ok" };
    case "kept": return { word: "Kept", led: "pass", pulse: false, cls: "settled" };
    default: return { word: "Undone", led: "idle", pulse: false, cls: "settled" };
  }
}

type Pending = { id: string; kind: "answer" | "text" | "keep" | "undo"; opt?: string } | null;

interface CardProps {
  t: Ticket; selected: boolean; claudeLinked: boolean; pending: Pending; error?: string;
  setRef: (el: HTMLElement | null) => void; onSelect: () => void;
  onAnswer: (opt: string) => void; onText: (text: string) => void; onKeep: () => void; onUndo: () => void;
}

function TicketCard({ t, selected, claudeLinked, pending, error, setRef, onSelect, onAnswer, onText, onKeep, onUndo }: CardProps) {
  const st = cardState(t);
  const settled = isSettled(t);
  const [showOpts, setShowOpts] = useState(!settled);
  const [text, setText] = useState("");
  const mine = pending?.id === t.id ? pending : null;
  const lock = !!pending; // one write at a time
  const by = t.answer?.by;
  const defLabel = optionLabel(t, t.default_option);
  const ansLabel = t.answer ? (t.answer.text?.trim() || optionLabel(t, t.answer.option_id) || null) : null;
  const canKeep = t.status === "defaulted" || t.status === "answered" || t.status === "undone";
  const canUndo = t.status === "answered" || t.status === "kept";
  const visible = t.options.slice(0, 8);
  const more = visible.filter(isMore);
  const real = visible.filter((o) => !isMore(o));
  const evidence = t.evidence.filter((e) => e && e.trim());
  const steps = evidence.length + 1 + (t.answer ? 2 : 0) + (settled ? 1 : 0);
  const headId = `ax-${t.id.replace(/[^\w-]/g, "_")}`;

  return (
    <article ref={setRef} tabIndex={selected ? 0 : -1} onClick={onSelect} onFocus={onSelect}
      className={`ax-card is-${t.status} ${settled ? "is-settled" : ""} ${selected ? "is-selected" : ""}`}
      aria-labelledby={`${headId}-q`} aria-describedby={`${headId}-s`} data-ticket={t.id}>
      <div className="ax-card-head">
        <span className="ax-id">{t.kind}</span>
        <span className="ax-stage">{stageText(t.stage)}</span>
        <span id={`${headId}-s`} className={`ax-state ${st.cls === "needs" ? "is-needs" : t.status === "kept" ? "is-kept" : ""}`}>
          <Led status={st.led} pulse={st.pulse} />{st.word}
        </span>
      </div>
      <p className="ax-subject">{KIND_LABEL[t.kind] ?? "Decision"} · {t.subject}</p>
      <h4 className="ax-q" id={`${headId}-q`}>{t.question}</h4>

      {/* Safe default: engraved (the rule engine chose it) */}
      <section className="ax-slot is-default" aria-label="Safe default">
        <div className="ax-slot-head">
          <span className="ax-slot-title">Safe default</span>
          <ProvenanceTag by="engine" kind={defLabel ? "derived" : "unknown"} source="Chosen by the rule engine as the safest option" compact />
          {t.check_me && t.status !== "kept" && <Chip led="ai">Check me</Chip>}
        </div>
        <p className="ax-slot-text">{defLabel ?? "No safe default. Pick an option."}</p>
      </section>

      {/* Claude / user answer: backlit amber for Claude */}
      {t.answer ? (
        <section className={`ax-slot ${by === "claude" ? "is-claude" : "is-user"}`} aria-label={by === "claude" ? "Claude's answer" : "Your answer"}>
          <div className="ax-slot-head">
            <span className="ax-slot-title">{by === "claude" ? "Claude's answer" : "Your answer"}</span>
            <ProvenanceTag by={by === "claude" ? "claude" : "user"} kind={by === "claude" ? "inferred" : "observed"} compact />
          </div>
          <p className="ax-slot-text">{ansLabel ? clip(ansLabel, 220) : "Answer applied."}</p>
          {t.answer.reason && <p className="ax-slot-reason">“{clip(t.answer.reason, 240)}”</p>}
        </section>
      ) : (t.status === "open" || t.status === "defaulted") && (
        <p className="ax-hint">{claudeLinked ? "Waiting for Claude or you." : "Claude is not linked. Pick an option, or keep the default."}</p>
      )}

      {/* Options (answer by hand) */}
      {(real.length > 0 || (t.free_text && !settled)) && (
        <>
          <button type="button" className="ax-opts-toggle" aria-expanded={showOpts} aria-controls={`${headId}-o`} onClick={() => setShowOpts(!showOpts)}>
            <Icon name="chevron-down" size={13} />{settled ? "Change answer" : "Options"}{real.length ? ` · ${real.length}` : ""}
          </button>
          {showOpts && (
            <div id={`${headId}-o`} className="ax-opts">
              {real.map((o) => {
                const applied = t.applied_option === o.id;
                const isDef = o.id === t.default_option;
                const led: LedStatus | false = applied ? (t.answer ? (by === "claude" ? "ai" : "cobalt") : "det") : false;
                const sub = !o.valid ? "Rules reject this here" : applied && t.answer ? (by === "claude" ? "Chosen by Claude" : "Chosen by you") : isDef ? "Safe default" : undefined;
                return (
                  <LatchButton key={o.id} size="sm" latched={applied} led={led} sub={sub} disabled={!o.valid || (lock && !(mine?.kind === "answer" && mine.opt === o.id))}
                    loading={mine?.kind === "answer" && mine.opt === o.id} title={o.label}
                    onClick={() => onAnswer(o.id)}>
                    {clip(o.label, 90)}
                  </LatchButton>
                );
              })}
              {more.map((o) => <p key="more" className="ax-opt-more">{o.label} not shown here. Ask Claude to pick from them.</p>)}
              {t.free_text && !settled && (
                <form className="ax-free" onSubmit={(e) => { e.preventDefault(); if (text.trim() && !lock) onText(text.trim()); }}>
                  <Field label="Or write your own" value={text} onChange={(e) => setText(e.target.value)} placeholder="Short, plain words" maxLength={280}
                    hint="The rules re-check it before it reaches the plan." />
                  <LatchButton type="submit" size="sm" variant="primary" disabled={!text.trim() || lock} loading={mine?.kind === "text"}>Use</LatchButton>
                </form>
              )}
            </div>
          )}
        </>
      )}

      {error && <div className="ax-alert" role="alert"><Led status="fail" /><div className="ax-alert-body"><b>Not applied.</b> {error}</div></div>}

      {/* Keep / Undo */}
      <div className="ax-actions" role="group" aria-label={`Review ${t.id}`}>
        <LatchButton variant="det" latched={t.status === "kept"} led={t.status === "kept" ? "det" : false} disabled={!canKeep || (lock && mine?.kind !== "keep")} loading={mine?.kind === "keep"}
          title={canKeep ? undefined : t.status === "kept" ? "Already kept" : "Pick an option first"} onClick={onKeep}
          icon={<Icon name="check" size={14} />}>
          {t.status === "defaulted" ? "Keep default" : t.status === "kept" ? "Kept" : "Keep"}
        </LatchButton>
        <LatchButton latched={t.status === "undone"} led={t.status === "undone" ? "idle" : false} disabled={!canUndo || (lock && mine?.kind !== "undo")} loading={mine?.kind === "undo"}
          title={canUndo ? "Go back to the safe default" : t.status === "undone" ? "Already undone" : "Nothing to undo: this is the safe default"} onClick={onUndo}
          icon={<Icon name="undo" size={14} />}>
          {t.status === "undone" ? "Undone" : "Undo"}
        </LatchButton>
      </div>

      {/* Evidence / provenance chain */}
      <details className="ax-evidence">
        <summary><Icon name="layers" size={13} />Evidence chain · {steps} {steps === 1 ? "step" : "steps"}</summary>
        <ol className="ax-chain">
          {evidence.map((e, i) => (
            <li key={`e${i}`}>
              <span className="ax-chain-dot"><Led status="det" /></span>
              <div className="ax-chain-body">
                <ProvenanceTag by="engine" kind="derived" compact />
                <p className="ax-chain-text mono">{e}</p>
              </div>
            </li>
          ))}
          <li>
            <span className="ax-chain-dot"><Led status={defLabel ? "det" : "idle"} /></span>
            <div className="ax-chain-body">
              <ProvenanceTag by="engine" kind={defLabel ? "derived" : "unknown"} compact />
              <p className="ax-chain-text">Safe default applied: {defLabel ?? "none"}.</p>
            </div>
          </li>
          {t.answer && (
            <>
              <li>
                <span className="ax-chain-dot"><Led status={by === "claude" ? "ai" : "cobalt"} /></span>
                <div className="ax-chain-body">
                  <ProvenanceTag by={by === "claude" ? "claude" : "user"} kind={by === "claude" ? "inferred" : "observed"} compact />
                  <p className="ax-chain-text">{by === "claude" ? "Claude" : "You"} answered{t.answer.reason ? `: “${clip(t.answer.reason, 160)}”` : "."}</p>
                </div>
              </li>
              <li>
                <span className="ax-chain-dot"><Led status="det" /></span>
                <div className="ax-chain-body">
                  <ProvenanceTag by="engine" kind="derived" compact />
                  <p className="ax-chain-text">Rules re-checked the answer before it reached the plan.</p>
                </div>
              </li>
            </>
          )}
          {settled && (
            <li>
              <span className="ax-chain-dot"><Led status={t.status === "kept" ? "pass" : "idle"} /></span>
              <div className="ax-chain-body">
                <ProvenanceTag by="user" kind="observed" compact />
                <p className="ax-chain-text">{t.status === "kept" ? "You kept it." : "You undid it. The safe default is back."}</p>
              </div>
            </li>
          )}
        </ol>
      </details>
    </article>
  );
}

export default function Inbox() {
  const tickets = useStore((s) => s.tickets);
  const project = useStore((s) => s.project);
  const bridge = useStore((s) => s.bridge);
  const setInbox = useStore((s) => s.setInbox);
  const run = useStore((s) => s.run);
  const toast = useStore((s) => s.toast);
  const [filter, setFilter] = useState<Filter>("needs");
  const [selId, setSelId] = useState<string | null>(null);
  const [pending, setPending] = useState<Pending>(null);
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [linkOpen, setLinkOpen] = useState(false);
  const refs = useRef<Record<string, HTMLElement | null>>({});
  const kbd = useRef(false);
  const idxRef = useRef(0);

  const linked = bridge.linked && (bridge.fresh ?? true);
  const loading = project === null && tickets.length === 0;
  const counts = useMemo(() => ({
    needs: tickets.filter(needsMe).length,
    defaulted: tickets.filter(FILTERS.defaulted).length,
    answered: tickets.filter(isAnswered).length,
    all: tickets.length,
  }), [tickets]);
  const toDecide = waiting(tickets).length;
  const toReview = tickets.filter((t) => t.status === "answered").length;
  const settled = tickets.filter(isSettled).length;

  const list = useMemo(
    () => tickets.filter(FILTERS[filter]).map((t, i) => ({ t, i })).sort((a, b) => rank(a.t) - rank(b.t) || a.i - b.i).map((x) => x.t),
    [tickets, filter],
  );

  // Selected index survives the selected card leaving the list (it falls to the next card).
  const found = selId ? list.findIndex((t) => t.id === selId) : -1;
  const vanished = !!selId && found < 0;
  const cur = list.length === 0 ? -1 : found >= 0 ? found : Math.min(idxRef.current, list.length - 1);
  idxRef.current = Math.max(cur, 0);
  const selected = cur >= 0 ? list[cur] : null;

  useEffect(() => {
    // After an action removes the focused card, hand focus to the new selection (keyboard users only).
    if (!kbd.current || !selected) return;
    const a = document.activeElement;
    if (!a || a === document.body) refs.current[selected.id]?.focus();
  }, [selected?.id, tickets]); // eslint-disable-line react-hooks/exhaustive-deps

  // Opening the inbox moves focus to the first card so j / k / Enter / u work straight away.
  const didFocus = useRef(false);
  useEffect(() => {
    if (didFocus.current || loading || !list.length) return;
    didFocus.current = true;
    kbd.current = true;
    refs.current[list[0].id]?.focus({ preventScroll: true });
  }, [loading, list]);

  const act = useCallback(async (t: Ticket, p: NonNullable<Pending>, label: string, fn: () => Promise<unknown>, okMsg: string) => {
    setPending(p);
    setErrors((e) => ({ ...e, [t.id]: "" }));
    const r = await run(label, async () => {
      try { return await fn(); } catch (e) { setErrors((x) => ({ ...x, [t.id]: e instanceof Error ? e.message : String(e) })); throw e; }
    });
    setPending(null);
    if (r !== undefined) toast("ok", okMsg);
  }, [run, toast]);

  const answer = (t: Ticket, opt: string) => void act(t, { id: t.id, kind: "answer", opt }, "Applying answer",
    () => rpc("answer_decision", { ticket_id: t.id, option_id: opt, reason: "picked in the app", by: "user" }), `${t.id} answered. Rules re-checked it.`);
  const answerText = (t: Ticket, text: string) => void act(t, { id: t.id, kind: "text" }, "Checking answer",
    () => rpc("answer_decision", { ticket_id: t.id, text, reason: "typed in the app", by: "user" }), `${t.id} answered. Rules re-checked it.`);
  const keep = (t: Ticket) => void act(t, { id: t.id, kind: "keep" }, "Keeping",
    () => rpc("review_decision", { ticket_id: t.id, keep: true }), `${t.id} kept.`);
  const undo = (t: Ticket) => void act(t, { id: t.id, kind: "undo" }, "Undoing",
    () => rpc("review_decision", { ticket_id: t.id, keep: false }), `${t.id} undone. Safe default is back.`);

  // Keyboard: j / k move, Enter keeps, u undoes. Ignored while typing or when a dialog is open.
  const keyState = useRef({ list, cur, vanished, pending });
  keyState.current = { list, cur, vanished, pending };
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.metaKey || e.ctrlKey || e.altKey || e.defaultPrevented) return;
      const el = e.target as HTMLElement | null;
      if (el?.closest("input, textarea, select, [contenteditable='true'], [role='tab'], [role='switch']")) return;
      if (document.querySelector('[aria-modal="true"]')) return;
      const { list: L, cur: c, vanished: v, pending: p } = keyState.current;
      if (!L.length) return;
      const move = (to: number) => {
        const n = Math.max(0, Math.min(L.length - 1, to));
        kbd.current = true;
        setSelId(L[n].id);
        requestAnimationFrame(() => { refs.current[L[n].id]?.focus(); refs.current[L[n].id]?.scrollIntoView({ block: "nearest" }); });
      };
      if (e.key === "j" || (e.key === "ArrowDown" && el?.classList.contains("ax-card"))) { e.preventDefault(); move(v ? c : c + 1); return; }
      if (e.key === "k" || (e.key === "ArrowUp" && el?.classList.contains("ax-card"))) { e.preventDefault(); move(c - 1); return; }
      const t = L[c];
      if (!t) return;
      if (e.key === "u") {
        e.preventDefault();
        if (p) return;
        if (t.status === "answered" || t.status === "kept") undo(t);
        else toast("info", t.status === "undone" ? `${t.id} is already undone.` : `${t.id}: nothing to undo. It is on the safe default.`);
        return;
      }
      // Enter keeps only when the card (or the page) has focus, so buttons still do their own thing.
      if (e.key === "Enter" && (el === document.body || el?.classList.contains("ax-card") || !el)) {
        e.preventDefault();
        if (p) return;
        if (t.status === "defaulted" || t.status === "answered" || t.status === "undone") keep(t);
        else toast("info", t.status === "kept" ? `${t.id} is already kept.` : `${t.id}: pick an option first.`);
      }
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }); // eslint-disable-line react-hooks/exhaustive-deps

  const tab = (id: Filter, label: string) => ({ id, label: <>{label}<span className="ax-count">{counts[id]}</span></> });

  const empty = (() => {
    if (counts.all === 0) {
      return (
        <EmptyState icon={<Icon name="inbox" size={24} />} title="No decisions yet"
          action={<LatchButton variant="ghost" led={false} onClick={() => setInbox(false)}>Back to the plan</LatchButton>}>
          The rules raise a decision when they should not guess, like a room clash or an unknown CAD layer. They show up here.
        </EmptyState>
      );
    }
    if (filter === "needs") {
      return (
        <EmptyState icon={<Icon name="check" size={24} />} title="All caught up"
          action={counts.answered ? <LatchButton variant="det" onClick={() => setFilter("answered")}>See answered ({counts.answered})</LatchButton> : undefined}>
          Nothing needs you right now. Every choice so far is settled.
        </EmptyState>
      );
    }
    return (
      <EmptyState icon={<Icon name="inbox" size={24} />} title={filter === "defaulted" ? "No defaults waiting" : "Nothing answered yet"}
        action={<LatchButton variant="ghost" led={false} onClick={() => setFilter("all")}>Show all ({counts.all})</LatchButton>}>
        {filter === "defaulted" ? "Every decision has been answered or kept." : "Answers from Claude or you will appear here, ready to Keep or Undo."}
      </EmptyState>
    );
  })();

  return (
    <aside className="inbox ax-inbox" aria-label="Decision inbox">
      <header className="ax-inbox-head">
        <div>
          <div className="eyebrow">Co-pilot · tickets T0-T8</div>
          <h2>Decisions</h2>
        </div>
        <span className="spacer" />
        <IconButton label="Close decision inbox" size="sm" icon={<Icon name="close" />} onClick={() => setInbox(false)} />
      </header>

      <Panel profile="well" pad="sm" small className="ax-meters" aria-label="Decision counts">
        <Readout label="To decide" value={toDecide} digits={0} tone="ai" size="sm" />
        <Readout label="To review" value={toReview} digits={0} tone="ai" size="sm" />
        <Readout label="Settled" value={settled} digits={0} tone="det" size="sm" />
      </Panel>

      <Panel profile="flat" pad="sm" small className="ax-claude-line">
        <div className={`ax-claude-state ${linked ? "is-linked" : ""}`}>
          <Led status={linked ? "ai" : "idle"} pulse={linked && toDecide > 0} />
          {linked ? "Claude is linked" : "Claude is not linked"}
        </div>
        <p className="ax-note">
          {linked
            ? <>Ask it in the chat to <i>answer the waiting planner decisions</i>. Its answers are re-checked by the rules before they reach the plan.</>
            : <>Every decision already has a safe default, marked <b>check me</b>. Pick an option here, or link Claude to have it answer.</>}
        </p>
        <div className="ax-inline">
          {linked
            ? <CopyButton text={ASK_CLAUDE} what="the prompt for Claude" label="Copy prompt" />
            : <LatchButton variant="ai" size="sm" onClick={() => setLinkOpen(true)}>Link Claude</LatchButton>}
        </div>
      </Panel>

      <div className="ax-filters">
        <Tabs label="Filter decisions" size="sm" block value={filter} onChange={(f) => { setFilter(f); setSelId(null); idxRef.current = 0; }}
          items={[tab("needs", "Needs me"), tab("defaulted", "Default"), tab("answered", "Answered"), tab("all", "All")]} />
      </div>

      <div className="sr-only" role="status" aria-live="polite">
        {loading ? "Loading decisions" : `${toDecide} decisions to decide, ${toReview} to review`}
      </div>

      {loading ? (
        <div className="list ax-list" aria-busy="true">
          {[0, 1].map((i) => (
            <div key={i} className="ax-skel-card" aria-hidden="true">
              <Skeleton width="45%" height={14} /><Skeleton lines={2} height={14} /><Skeleton height={44} radius={12} />
            </div>
          ))}
        </div>
      ) : list.length === 0 ? (
        <div className="list">{empty}</div>
      ) : (
        <ol className="list ax-list" aria-label={`${list.length} decisions`}>
          {list.map((t) => (
            <li key={t.id}>
              <TicketCard t={t} selected={selected?.id === t.id} claudeLinked={linked} pending={pending} error={errors[t.id] || undefined}
                setRef={(el) => { refs.current[t.id] = el; }} onSelect={() => { if (selId !== t.id) setSelId(t.id); }}
                onAnswer={(o) => answer(t, o)} onText={(x) => answerText(t, x)} onKeep={() => keep(t)} onUndo={() => undo(t)} />
            </li>
          ))}
        </ol>
      )}

      <div className="ax-foot" aria-label="Keyboard shortcuts">
        <span><Kbd>j</Kbd><Kbd>k</Kbd> move</span>
        <span><Kbd>Enter</Kbd> keep</span>
        <span><Kbd>u</Kbd> undo</span>
      </div>
      {linkOpen && <ClaudeLink onClose={() => setLinkOpen(false)} />}
    </aside>
  );
}
