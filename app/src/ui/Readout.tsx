import type { ReactNode } from "react";
import { cx } from "./cx";
import { ProvenanceTag, type Actor, type EvidenceKind } from "./ProvenanceTag";

export type ReadoutTone = "cobalt" | "det" | "ai" | "pass" | "warn" | "fail" | "ink";

export interface ReadoutProps {
  /** Engraved label above (stack) or left of (inline) the LCD. */
  label?: ReactNode;
  /** Number or text. `null`/`undefined`/NaN render as an unlit "–" (unknown) - never invent a value. */
  value: number | string | null | undefined;
  /** Unit after the number, e.g. "m²", "m", "°". */
  unit?: string;
  /** Decimals for numeric values. Default 1. */
  digits?: number;
  /** LCD ink colour. Default `cobalt`; `det` for rule-derived values, `ai` for co-pilot values. */
  tone?: ReadoutTone;
  size?: "sm" | "md" | "lg";
  layout?: "stack" | "inline";
  /** Optional provenance chip (who produced the number and how we know it). */
  by?: Actor;
  kind?: EvidenceKind;
  /** Free-form source shown as tooltip on the provenance chip. */
  source?: string;
  className?: string;
}

const fmtNum = (n: number, d: number) => n.toLocaleString(undefined, { minimumFractionDigits: d, maximumFractionDigits: d });

/** Sunken LCD numeric telemetry with unit and optional provenance. */
export function Readout({ label, value, unit, digits = 1, tone = "cobalt", size = "md", layout = "stack", by, kind, source, className }: ReadoutProps) {
  const unknown = value == null || (typeof value === "number" && Number.isNaN(value)) || value === "";
  const text = unknown ? "–" : typeof value === "number" ? fmtNum(value, digits) : value;
  return (
    <div className={cx("ui-readout", `ui-readout-${layout}`, `ui-readout-${size}`, className)}>
      {(label != null || by) && (
        <div className="ui-readout-head">
          {label != null && <span className="ui-readout-label">{label}</span>}
          {by && <ProvenanceTag by={by} kind={unknown ? "unknown" : kind} source={source} compact />}
        </div>
      )}
      <output className={cx("lcd", unknown ? "unknown" : tone)} aria-label={typeof label === "string" ? `${label}: ${unknown ? "unknown" : `${text}${unit ? ` ${unit}` : ""}`}` : undefined}>
        {text}{!unknown && unit && <span className="lcd-unit">{unit}</span>}
      </output>
    </div>
  );
}
