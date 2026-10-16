import { type ReactNode, useEffect, useId, useRef, useState } from "react";
import { cx } from "./cx";

export interface ChannelProps {
  /** Engraved parameter name, e.g. "Setback buffer". */
  label: ReactNode;
  /** Mono helper line under the label, e.g. "Shapely inward mitre offset". */
  hint?: ReactNode;
  value: number;
  min: number;
  max: number;
  /** Default 1. */
  step?: number;
  /** Unit on the LCD, e.g. "m", "°". */
  unit?: string;
  /** Live updates while dragging / pressing keys. */
  onChange: (v: number) => void;
  /** Fires once when the user lets go (pointer up, key up, blur). Use it for expensive RPCs. */
  onCommit?: (v: number) => void;
  /** LCD text; default: value with as many decimals as `step`. */
  format?: (v: number) => string;
  /** Accent: `cobalt` (UI), `det` (rule parameter), `ai` (co-pilot proposed). */
  tone?: "cobalt" | "det" | "ai";
  disabled?: boolean;
  /** Extra slot under the hint (e.g. a ProvenanceTag). */
  meta?: ReactNode;
  className?: string;
}

const decimals = (step: number) => (String(step).split(".")[1]?.length ?? 0);
const COMMIT_KEYS = new Set(["ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight", "PageUp", "PageDown", "Home", "End"]);

/**
 * Recessed LCD parameter channel: label + slider in a carved groove + LCD readout.
 * Keyboard: arrows = 1 step, PageUp/PageDown = 10 steps, Home/End = min/max (native range behaviour).
 */
export function Channel({ label, hint, value, min, max, step = 1, unit, onChange, onCommit, format, tone = "cobalt", disabled, meta, className }: ChannelProps) {
  const id = useId();
  const last = useRef(value);
  const [local, setLocal] = useState(value);
  useEffect(() => { setLocal(value); last.current = value; }, [value]);
  const shown = format ? format(local) : local.toFixed(decimals(step));
  const commit = () => { if (onCommit && local !== last.current) { last.current = local; onCommit(local); } };
  const pct = max > min ? ((local - min) / (max - min)) * 100 : 0;
  return (
    <div className={cx("ui-channel", `tone-${tone}`, disabled && "is-disabled", className)}>
      <div className="ui-channel-text">
        <label htmlFor={id} className="ui-channel-label">{label}</label>
        {hint != null && <span id={`${id}-h`} className="ui-channel-hint">{hint}</span>}
        {meta}
      </div>
      <input id={id} type="range" className="ui-channel-range" min={min} max={max} step={step} value={local} disabled={disabled}
        style={{ ["--fill" as string]: `${pct}%` }}
        aria-valuetext={`${shown}${unit ? ` ${unit}` : ""}`} aria-describedby={hint != null ? `${id}-h` : undefined}
        onChange={(e) => { const v = Number(e.target.value); setLocal(v); onChange(v); }}
        onPointerUp={commit} onBlur={commit} onKeyUp={(e) => { if (COMMIT_KEYS.has(e.key)) commit(); }} />
      <output htmlFor={id} className={cx("lcd", tone === "cobalt" ? undefined : tone)} aria-hidden="true">
        {shown}{unit && <span className="lcd-unit">{unit}</span>}
      </output>
    </div>
  );
}

/** Alias: the kit calls this a slider/knob channel. */
export const Slider = Channel;
