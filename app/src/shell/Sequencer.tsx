import { type KeyboardEvent, useEffect, useRef } from "react";
import { Icon, Led, type LedStatus } from "../ui";
import { RULES_SHARE, type StageMeta } from "./stages";

export type PadState = "done" | "active" | "blocked" | "decision" | "ready";

export interface PadInfo {
  meta: StageMeta;
  state: PadState;
  /** Waiting decisions (tickets) raised at this stage. */
  waiting: number;
  /** Reason the stage is locked (null when available). */
  blocked: string | null;
  done: boolean;
  active: boolean;
}

export interface SequencerProps {
  pads: PadInfo[];
  onSelect: (n: number) => void;
  onBlocked: (reason: string) => void;
}

const LED: Record<PadState, LedStatus> = { done: "det", active: "cobalt", blocked: "idle", decision: "ai", ready: "idle" };
const STATE_TEXT: Record<PadState, string> = {
  done: "done", active: "current stage", blocked: "locked", decision: "decision waiting", ready: "ready",
};

const isTyping = (t: EventTarget | null) => {
  const el = t as HTMLElement | null;
  return !!el && (el.isContentEditable || ["INPUT", "TEXTAREA", "SELECT"].includes(el.tagName));
};

/**
 * The 8-stage hardware step sequencer - primary navigation.
 * Keys: 1-8 jump to a stage, [ and ] step back/forward (anywhere outside text fields);
 * Left/Right/Home/End move focus between pads, Enter/Space opens.
 */
export function Sequencer({ pads, onSelect, onBlocked }: SequencerProps) {
  const refs = useRef<Array<HTMLButtonElement | null>>([]);
  const activeIdx = Math.max(0, pads.findIndex((p) => p.active));

  const go = (i: number) => {
    const p = pads[i];
    if (!p) return;
    if (p.blocked) onBlocked(p.blocked);
    else onSelect(p.meta.n);
  };

  // global hotkeys
  const goRef = useRef(go);
  goRef.current = go;
  const padsRef = useRef(pads);
  padsRef.current = pads;
  useEffect(() => {
    const onKey = (e: globalThis.KeyboardEvent) => {
      if (e.defaultPrevented || e.altKey || e.ctrlKey || e.metaKey || isTyping(e.target)) return;
      if (document.querySelector('[aria-modal="true"]')) return;
      if (/^[1-8]$/.test(e.key)) { e.preventDefault(); goRef.current(Number(e.key) - 1); return; }
      if (e.key === "[" || e.key === "]") {
        e.preventDefault();
        const list = padsRef.current;
        const cur = list.findIndex((p) => p.active);
        const step = e.key === "]" ? 1 : -1;
        for (let i = cur + step; i >= 0 && i < list.length; i += step) {
          if (!list[i].blocked) { goRef.current(i); return; }
        }
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  const onPadKey = (e: KeyboardEvent, i: number) => {
    let n: number | undefined;
    if (e.key === "ArrowRight") n = (i + 1) % pads.length;
    else if (e.key === "ArrowLeft") n = (i - 1 + pads.length) % pads.length;
    else if (e.key === "Home") n = 0;
    else if (e.key === "End") n = pads.length - 1;
    if (n === undefined) return;
    e.preventDefault();
    refs.current[n]?.focus();
  };

  return (
    <nav className="sequencer" aria-label="Pipeline stages">
      <div className="sequencer-rail">
        <ol className="sequencer-pads">
          {pads.map((p, i) => {
            const ai = 100 - p.meta.rules;
            const label = `Stage ${p.meta.n}, ${p.meta.label}, ${STATE_TEXT[p.state]}${p.waiting ? `, ${p.waiting} decision${p.waiting > 1 ? "s" : ""} waiting` : ""}. Rules ${p.meta.rules}%, co-pilot ${ai}%. Shortcut ${p.meta.n}.`;
            return (
              <li key={p.meta.n}>
                <button ref={(el) => { refs.current[i] = el; }} type="button"
                  className={`seq-pad is-${p.state}${p.active ? " is-active" : ""}${p.done ? " is-done" : ""}`}
                  aria-current={p.active ? "step" : undefined} aria-disabled={p.blocked ? true : undefined}
                  aria-label={label} title={p.blocked ?? `${p.meta.title} · press ${p.meta.n}`}
                  tabIndex={i === activeIdx ? 0 : -1} onClick={() => go(i)} onKeyDown={(e) => onPadKey(e, i)}>
                  <span className="seq-top">
                    {p.state === "blocked" ? <Icon name="lock" size={11} className="seq-lock" /> : <Led status={LED[p.state]} pulse={p.state === "decision"} />}
                    <span className="seq-num">{String(p.meta.n).padStart(2, "0")}</span>
                    {p.waiting > 0 && <span className="seq-badge" aria-hidden="true">{p.waiting}</span>}
                    {p.done && p.state !== "decision" && <Icon name="check" size={12} className="seq-check" />}
                  </span>
                  <span className="seq-name">{p.meta.label}</span>
                  <span className="seq-share" aria-hidden="true">
                    <span className="seq-share-det" style={{ width: `${p.meta.rules}%` }} />
                    <span className="seq-share-ai" style={{ width: `${ai}%` }} />
                  </span>
                </button>
              </li>
            );
          })}
        </ol>
        <div className="sequencer-legend" aria-label={`Across all stages: rules ${RULES_SHARE}%, co-pilot ${100 - RULES_SHARE}%`}>
          <span><Led status="det" /> Rules {RULES_SHARE}%</span>
          <span><Led status="ai" /> Co-pilot {100 - RULES_SHARE}%</span>
        </div>
      </div>
    </nav>
  );
}

