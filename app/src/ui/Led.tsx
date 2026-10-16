import { cx } from "./cx";

/**
 * LED colour semantics:
 * - `det`  verdigris: deterministic rule engine produced / owns this
 * - `ai`   amber: Claude co-pilot (suggestion, waiting decision)
 * - `pass` / `warn` / `fail`: statutory result (DCPR 2034 / NBC 2016)
 * - `cobalt`: neutral UI state (selected, active, linked)
 * - `idle`: unlit (unknown, not started, blocked)
 */
export type LedStatus = "det" | "ai" | "pass" | "warn" | "fail" | "cobalt" | "idle";

export interface LedProps {
  /** Colour/meaning of the pip. Default `idle` (unlit). */
  status?: LedStatus;
  /** Slow blink to draw attention (e.g. a decision is waiting). Disabled under reduced motion. */
  pulse?: boolean;
  /** `lg` = 10px pip, default 8px. */
  size?: "sm" | "lg";
  /** Screen-reader text. Omit when the adjacent text already says the same thing (LED becomes decorative). */
  label?: string;
  className?: string;
}

/** Hardware status pip. Never the only carrier of meaning: pair it with text or a `label`. */
export function Led({ status = "idle", pulse, size = "sm", label, className }: LedProps) {
  return (
    <span className={cx("led", status, pulse && "pulse", size === "lg" && "lg", className)}
      role={label ? "img" : undefined} aria-label={label} aria-hidden={label ? undefined : true} />
  );
}
