// First-run coach marks over the sequencer. Non-modal: the app stays usable underneath, Esc skips.
import { type CSSProperties, useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { RULES_SHARE } from "../shell/stages";
import { Icon, Kbd, LatchButton, ProvenanceTag, Readout } from "../ui";
import { modKey, otherModalOpen, reducedMotion } from "./storage";
import "./flows.css";

interface TourStep {
  key: string;
  eyebrow: string;
  title: string;
  /** CSS selector of the element to point at; the card centres when it is missing. */
  anchor: string;
  body: string;
  extra?: "split" | "ticket" | "keys";
}

const STEPS: TourStep[] = [
  {
    key: "plot", eyebrow: "Stage 1", title: "Pick your plot", anchor: ".sequencer-pads li:nth-child(1)",
    body: "Search for the locality. The map flies there so you can see the real plot.",
  },
  {
    key: "trace", eyebrow: "Stage 2", title: "Trace its edge", anchor: ".sequencer-pads li:nth-child(2)",
    body: "Click the plot corners on the map. The rules turn them into area, perimeter and edge lengths in metres.",
  },
  {
    key: "rules", eyebrow: "Stages 3 to 8", title: "Let the rules run", anchor: ".sequencer-rail",
    body: "Setback, tower placement, floor shape, flats, rooms and checks follow DCPR 2034 and NBC 2016. Same plot, same answer, every time.",
    extra: "split",
  },
  {
    key: "claude", eyebrow: "Decisions", title: "Claude only for tickets", anchor: ".topbar-inbox",
    body: "When a rule cannot decide, Claude may propose a ticket. It waits here. The rules re-check it, and you press Keep or Undo.",
    extra: "ticket",
  },
];

interface Placed { top: number; left: number; arrow: number; place: "below" | "above" | "center"; rect: DOMRect | null }

const clamp = (n: number, lo: number, hi: number) => Math.min(Math.max(n, lo), Math.max(lo, hi));

export interface OnboardingProps {
  open: boolean;
  /** `completed` is true when the last step was finished, false when skipped. */
  onClose: (completed: boolean) => void;
  /** Called when the person finishes: lets the host open the palette hint or similar. */
  onDone?: () => void;
}

export function Onboarding({ open, onClose, onDone }: OnboardingProps) {
  const [i, setI] = useState(0);
  const [placed, setPlaced] = useState<Placed | null>(null);
  const cardRef = useRef<HTMLDivElement>(null);
  const nextRef = useRef<HTMLButtonElement>(null);
  const prevFocus = useRef<HTMLElement | null>(null);
  const step = STEPS[i];
  const last = i === STEPS.length - 1;

  useEffect(() => { if (open) setI(0); }, [open]);

  // remember where focus was and give it back afterwards
  useEffect(() => {
    if (!open) return;
    prevFocus.current = document.activeElement as HTMLElement | null;
    return () => { prevFocus.current?.focus?.(); };
  }, [open]);

  const measure = useCallback(() => {
    const card = cardRef.current;
    const w = card?.offsetWidth ?? 340;
    const h = card?.offsetHeight ?? 220;
    const vw = window.innerWidth, vh = window.innerHeight;
    const el = document.querySelector<HTMLElement>(step.anchor);
    const rect = el && el.getClientRects().length ? el.getBoundingClientRect() : null;
    if (!rect) {
      setPlaced({ top: Math.max(16, (vh - h) / 2), left: Math.max(12, (vw - w) / 2), arrow: 0, place: "center", rect: null });
      return;
    }
    const cx = rect.left + rect.width / 2;
    const left = clamp(cx - w / 2, 12, vw - w - 12);
    const below = rect.bottom + 16 + h <= vh - 12 || rect.top - 16 - h < 12;
    setPlaced({
      top: below ? rect.bottom + 16 : rect.top - 16 - h,
      left, arrow: clamp(cx - left, 22, w - 22), place: below ? "below" : "above", rect,
    });
  }, [step.anchor]);

  // bring the target into view, then keep the card glued to it
  useLayoutEffect(() => {
    if (!open) return;
    document.querySelector<HTMLElement>(step.anchor)?.scrollIntoView({ block: "nearest", inline: "nearest", behavior: reducedMotion() ? "auto" : "smooth" });
    measure();
    const t = setInterval(measure, 400); // catches layout shifts (inspector, fonts, docks)
    window.addEventListener("resize", measure);
    window.addEventListener("scroll", measure, true);
    return () => { clearInterval(t); window.removeEventListener("resize", measure); window.removeEventListener("scroll", measure, true); };
  }, [open, step.anchor, measure]);

  // the Next button gets focus on each step so Enter walks the tour
  useEffect(() => { if (open) nextRef.current?.focus({ preventScroll: true }); }, [open, i]);

  const finish = useCallback((completed: boolean) => {
    onClose(completed);
    if (completed) onDone?.();
  }, [onClose, onDone]);

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Escape" || e.defaultPrevented || otherModalOpen()) return;
      e.preventDefault();
      finish(false);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, finish]);

  if (!open) return null;

  const r = placed?.rect;
  const pad = 6;
  const sheet = typeof window !== "undefined" && window.innerWidth < 640;

  return createPortal(
    <>
      {r && (
        <div className="fx-spot" aria-hidden="true"
          style={{ top: r.top - pad, left: r.left - pad, width: r.width + pad * 2, height: r.height + pad * 2 }} />
      )}
      <div ref={cardRef} role="dialog" aria-modal="false" aria-labelledby="fx-tour-title" aria-describedby="fx-tour-body" data-fx-tour
        className={`fx-coach neo-flat${placed ? ` is-${placed.place}` : ""}${sheet ? " is-sheet" : ""}`}
        style={placed && !sheet ? ({ top: placed.top, left: placed.left, "--fx-arrow": `${placed.arrow}px` } as CSSProperties) : undefined}>
        <div className="fx-coach-head">
          <div>
            <div className="eyebrow">{step.eyebrow}</div>
            <h2 id="fx-tour-title" className="fx-coach-title">{step.title}</h2>
          </div>
          <span className="fx-coach-count" role="img" aria-label={`Step ${i + 1} of ${STEPS.length}`}>
            {STEPS.map((s, n) => <i key={s.key} className={n === i ? "is-on" : n < i ? "is-done" : undefined} />)}
          </span>
        </div>
        <div aria-live="polite">
          <p id="fx-tour-body" className="fx-coach-body">{step.body}</p>
          {step.extra === "split" && (
            <div className="fx-coach-split">
              <Readout label="Rules" value={RULES_SHARE} unit="%" digits={0} tone="det" size="sm" by="engine" kind="derived" source="Designed split across the 8 stages" />
              <Readout label="Co-pilot" value={100 - RULES_SHARE} unit="%" digits={0} tone="ai" size="sm" by="claude" kind="inferred" source="Always re-checked by the rules" />
            </div>
          )}
          {step.extra === "ticket" && (
            <p className="fx-coach-note"><ProvenanceTag by="claude" kind="inferred" compact /> <span>Amber means Claude. It never changes a plan on its own.</span></p>
          )}
        </div>
        {last && (
          <p className="fx-coach-tip">
            <Icon name="keyboard" size={14} />
            <span>Press <Kbd>{modKey()}</Kbd> <Kbd>K</Kbd> for commands and <Kbd>?</Kbd> for shortcuts.</span>
          </p>
        )}
        <div className="fx-coach-foot">
          <LatchButton variant="ghost" size="sm" onClick={() => finish(false)}>Skip tour</LatchButton>
          <span className="spacer" />
          {i > 0 && <LatchButton size="sm" onClick={() => setI(i - 1)}>Back</LatchButton>}
          <LatchButton ref={nextRef} variant="primary" size="sm" onClick={() => (last ? finish(true) : setI(i + 1))}>
            {last ? "Got it" : "Next"}
          </LatchButton>
        </div>
      </div>
    </>,
    document.body,
  );
}
