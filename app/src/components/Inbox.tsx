import { useState } from "react";
import { rpc } from "../lib/api";
import { useStore, waiting } from "../lib/store";
import type { Ticket } from "../lib/types";

const KIND_LABEL: Record<string, string> = {
  T0: "Site · surroundings", T1: "Drawing · layer", T2: "Drawing · units", T3: "Layout · room clash", T4: "Drawing · room name",
  T5: "Placement · note", T6: "Checks · explain", T7: "Layout · edit", T8: "Programme · brief",
};

function TicketCard({ t }: { t: Ticket }) {
  const run = useStore((s) => s.run);
  const [text, setText] = useState("");
  const done = !(t.status === "open" || t.status === "defaulted");
  const by = t.answer?.by;
  return (
    <div className={`ticket ${done ? "done" : ""}`}>
      <div className="row">
        <span className={`badge ${done ? "teal" : "amber"}`}>{t.kind}</span>
        <span className="small muted">{KIND_LABEL[t.kind] ?? ""}</span>
        <span className="spacer" />
        {t.check_me && <span className="badge amber">check me</span>}
        {t.status === "kept" && <span className="badge green">kept</span>}
        {t.status === "undone" && <span className="badge grey">undone</span>}
      </div>
      <div>{t.question}</div>
      <div className="opts">
        {t.options.slice(0, 8).map((o) => (
          <button key={o.id} className={`opt ${t.applied_option === o.id ? "applied" : ""}`} disabled={!o.valid}
            title={o.id === t.default_option ? "Safe default" : ""}
            onClick={() => run("Applying answer", () => rpc("answer_decision", { ticket_id: t.id, option_id: o.id, reason: "picked in the app", by: "user" }))}>
            {o.label.length > 90 ? o.label.slice(0, 88) + "…" : o.label}{o.id === t.default_option ? " · default" : ""}
          </button>
        ))}
      </div>
      {t.free_text && !done && (
        <div className="row">
          <input className="field" style={{ flex: 1, border: "1px solid var(--line)", borderRadius: 7, padding: "5px 8px" }}
            value={text} onChange={(e) => setText(e.target.value)} placeholder="Write your own answer…" />
          <button className="btn small" disabled={!text.trim()} onClick={() => run("Checking answer", () => rpc("answer_decision", { ticket_id: t.id, text, reason: "typed in the app", by: "user" }))}>Use</button>
        </div>
      )}
      {t.answer && (
        <div className="small muted">
          Answered by <b style={{ color: by === "claude" ? "var(--amber)" : "var(--teal)" }}>{by}</b>
          {t.answer.reason ? ` - “${t.answer.reason}”` : ""}
        </div>
      )}
      {t.status === "answered" && (
        <div className="row">
          <button className="btn small primary" onClick={() => run("Keeping", () => rpc("review_decision", { ticket_id: t.id, keep: true }))}>Keep</button>
          <button className="btn small" onClick={() => run("Undoing", () => rpc("review_decision", { ticket_id: t.id, keep: false }))}>Undo</button>
        </div>
      )}
      {t.evidence.length > 0 && <div className="small muted mono">{t.evidence.slice(0, 3).join(" · ")}</div>}
    </div>
  );
}

export default function Inbox() {
  const tickets = useStore((s) => s.tickets);
  const setInbox = useStore((s) => s.setInbox);
  const bridge = useStore((s) => s.bridge);
  const [all, setAll] = useState(false);
  const list = all ? tickets : waiting(tickets);
  return (
    <aside className="inbox" aria-label="Decision inbox">
      <header>
        <h2>Decisions</h2>
        <span className="badge amber">{waiting(tickets).length} waiting</span>
        <span className="spacer" />
        <button className="btn small ghost" onClick={() => setAll(!all)}>{all ? "Waiting only" : "Show all"}</button>
        <button className="btn small" onClick={() => setInbox(false)} aria-label="Close">✕</button>
      </header>
      <div className="list">
        <div className="card soft small">
          Every ticket already has a safe default applied, marked <b>check me</b>. {bridge.linked
            ? <>Claude is linked: ask it in the chat to <i>answer the waiting planner decisions</i>.</>
            : <>Link Claude to have it answer them, or pick an option here.</>} Answers are re-checked by the rules before they reach the plan.
        </div>
        {list.length === 0 && <div className="empty">Nothing waiting.</div>}
        {list.map((t) => <TicketCard key={t.id} t={t} />)}
      </div>
    </aside>
  );
}
