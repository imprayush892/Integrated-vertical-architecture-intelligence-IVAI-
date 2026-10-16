import { cx } from "./cx";

export interface ProgressProps {
  /** 0..1. Omit for an indeterminate sweep (solver running, unknown duration). */
  value?: number;
  /** Accessible name, e.g. "Solving T1-A". Shown above the bar when `showLabel`. */
  label: string;
  showLabel?: boolean;
  /** Fill colour: `det` rule engine work (default), `ai` waiting on Claude, `cobalt` generic. */
  tone?: "det" | "ai" | "cobalt";
  className?: string;
}

/** Carved groove with a glowing fill. Indeterminate sweeps stop under reduced motion. */
export function Progress({ value, label, showLabel, tone = "det", className }: ProgressProps) {
  const det = value != null;
  const v = det ? Math.max(0, Math.min(1, value)) : 0;
  return (
    <div className={cx("ui-progress", `tone-${tone}`, !det && "is-indeterminate", className)}>
      {showLabel && <div className="ui-progress-label"><span>{label}</span>{det && <span className="mono">{Math.round(v * 100)}%</span>}</div>}
      <div className="ui-progress-track" role="progressbar" aria-label={label}
        aria-valuemin={det ? 0 : undefined} aria-valuemax={det ? 100 : undefined} aria-valuenow={det ? Math.round(v * 100) : undefined}>
        <span className="ui-progress-fill" style={det ? { width: `${v * 100}%` } : undefined} />
      </div>
    </div>
  );
}

export interface SkeletonProps {
  /** CSS width; default 100%. */
  width?: number | string;
  /** CSS height; default 12px. */
  height?: number | string;
  /** Repeat as text lines (last line shorter). */
  lines?: number;
  radius?: number | string;
  className?: string;
}

/** Carved placeholder while data loads. aria-hidden; pair with a status message elsewhere. */
export function Skeleton({ width = "100%", height = 12, lines = 1, radius = 6, className }: SkeletonProps) {
  return (
    <span className={cx("ui-skeleton-group", className)} aria-hidden="true">
      {Array.from({ length: lines }, (_, i) => (
        <span key={i} className="ui-skeleton" style={{ width: lines > 1 && i === lines - 1 ? "62%" : width, height, borderRadius: radius }} />
      ))}
    </span>
  );
}
