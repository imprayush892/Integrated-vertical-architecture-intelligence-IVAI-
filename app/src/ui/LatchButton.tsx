import { type ButtonHTMLAttributes, forwardRef, type ReactNode } from "react";
import { cx } from "./cx";
import { Led, type LedStatus } from "./Led";
import { Spinner } from "./Spinner";

export type ButtonVariant = "default" | "primary" | "det" | "ai" | "danger" | "ghost";

export interface LatchButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  /**
   * - `default` convex chassis button
   * - `primary` the one main action of a panel (cobalt LED + ink)
   * - `det` runs/accepts a deterministic rule-engine step (verdigris)
   * - `ai` asks Claude / acts on a co-pilot suggestion (amber)
   * - `danger` destructive or failing (red)
   * - `ghost` flat text button for tertiary actions
   */
  variant?: ButtonVariant;
  size?: "sm" | "md" | "lg";
  /**
   * Latching state. When set (true/false) the button is a toggle: it renders `aria-pressed`,
   * physically sinks into the slab when latched and its LED lights. Leave undefined for momentary buttons.
   */
  latched?: boolean;
  /** Override the LED. `false` hides it. Default: variant colour (lit unless `latched === false`). */
  led?: LedStatus | false;
  /** Blink the LED (e.g. "needs you"). */
  pulse?: boolean;
  /** Busy: shows a spinner in place of the LED, sets aria-busy and blocks clicks. */
  loading?: boolean;
  /** Second line in mono (turns the button into a tall latch tile, as in the trade-off matrix). */
  sub?: ReactNode;
  /** Leading icon. */
  icon?: ReactNode;
  /** Full width. */
  block?: boolean;
}

const VARIANT_LED: Record<ButtonVariant, LedStatus | null> = {
  default: null, ghost: null, primary: "cobalt", det: "det", ai: "ai", danger: "fail",
};

/** Tactile hardware push-button with optional latch + LED telemetry. */
export const LatchButton = forwardRef<HTMLButtonElement, LatchButtonProps>(function LatchButton(
  { variant = "default", size = "md", latched, led, pulse, loading, sub, icon, block, className, children, disabled, type = "button", onClick, ...rest },
  ref,
) {
  const base = led === false ? null : led ?? VARIANT_LED[variant] ?? (latched !== undefined ? "cobalt" : null);
  const ledStatus: LedStatus | null = base == null ? null : latched === false && led === undefined ? "idle" : base;
  return (
    <button ref={ref} type={type}
      className={cx("ui-btn", `ui-btn-${variant}`, `ui-btn-${size}`, sub != null && "ui-btn-tile", block && "ui-btn-block", latched && "is-latched", className)}
      aria-pressed={latched === undefined ? undefined : latched} aria-busy={loading || undefined}
      disabled={disabled} aria-disabled={loading || undefined}
      onClick={loading ? (e) => e.preventDefault() : onClick} {...rest}>
      {sub != null ? (
        <>
          <span className="ui-btn-topline">
            <span className="ui-btn-label">{icon}{children}</span>
            {loading ? <Spinner /> : ledStatus && <Led status={ledStatus} pulse={pulse} />}
          </span>
          <span className="ui-btn-sub">{sub}</span>
        </>
      ) : (
        <>
          {loading ? <Spinner /> : ledStatus && <Led status={ledStatus} pulse={pulse} />}
          {icon}
          {children != null && <span className="ui-btn-label">{children}</span>}
        </>
      )}
    </button>
  );
});
