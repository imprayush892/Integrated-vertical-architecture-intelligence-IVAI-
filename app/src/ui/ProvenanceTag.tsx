import { cx } from "./cx";
import { Led, type LedStatus } from "./Led";

/** Who produced a value (mirrors engine `Actor`). */
export type Actor = "engine" | "claude" | "user";
/** How we know it (mirrors engine `EvidenceKind`). */
export type EvidenceKind = "observed" | "derived" | "inferred" | "unknown";

export interface ProvenanceTagProps {
  /** engine = engraved RULE (verdigris), claude = backlit CO-PILOT (amber), user = USER (cobalt). */
  by: Actor;
  /** Evidence class. `unknown` always renders an unlit LED. */
  kind?: EvidenceKind;
  /** Rule clause / file / ticket shown as a tooltip. */
  source?: string;
  /** Tighter chip for inline use next to numbers. */
  compact?: boolean;
  className?: string;
}

const BY: Record<Actor, { text: string; led: LedStatus; sr: string }> = {
  engine: { text: "RULE", led: "det", sr: "rule engine" },
  claude: { text: "CO-PILOT", led: "ai", sr: "Claude co-pilot" },
  user: { text: "USER", led: "cobalt", sr: "you" },
};

/**
 * Epistemic-honesty chip. Rule output is ENGRAVED (carved, exact); Claude output is BACKLIT (glows,
 * always re-checked); user input is plain. Use next to any number or choice whose origin matters.
 */
export function ProvenanceTag({ by, kind, source, compact, className }: ProvenanceTagProps) {
  const b = BY[by];
  const led: LedStatus = kind === "unknown" ? "idle" : b.led;
  const sr = `Source: ${b.sr}${kind ? `, ${kind}` : ""}${source ? `. ${source}` : ""}`;
  return (
    <span className={cx("ui-prov", `ui-prov-${by}`, kind && `ui-prov-${kind}`, compact && "is-compact", className)} title={source ?? sr} aria-label={sr} role="note">
      <Led status={led} />
      <span aria-hidden="true">{b.text}{kind ? <span className="ui-prov-kind"> · {kind.toUpperCase()}</span> : null}</span>
    </span>
  );
}
