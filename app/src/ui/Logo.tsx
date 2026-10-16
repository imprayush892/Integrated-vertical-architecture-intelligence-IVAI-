import { useId } from "react";
import { cx } from "./cx";

export interface LogoProps {
  /** Pixel size of the mark (square). Default 28. */
  size?: number;
  /** `mark` = bare relief mark, `tile` = mark set into a convex app tile (top bar, splash). */
  variant?: "mark" | "tile";
  /** Show the "Apartment Planner" wordmark next to the mark. */
  wordmark?: boolean;
  /** Accessible name. Default "Apartment Planner". Pass "" to make it decorative. */
  title?: string;
  className?: string;
}

/**
 * Brand mark B - "Stepped Ziggurat" from the IVAI kit.
 * Three concentric plinths stepping up in Z: plot boundary -> setback envelope -> verdigris core.
 * Chosen over A (Push/Pull IV), C (Cruciform die) and D (Azimuth dial) because it
 *  1. stays legible at 16-28px (nested squares survive downscaling; A's chevron + pillar and D's needle blur),
 *  2. literally draws the product's pipeline (Trace the plot -> setback -> Shape the core),
 *  3. carries the epistemic colour code: the core is verdigris = the deterministic rule engine,
 *  4. has no letterforms, so it works for "Apartment Planner" and the IVAI umbrella alike.
 */
export function Logo({ size = 28, variant = "mark", wordmark, title = "Apartment Planner", className }: LogoProps) {
  const uid = useId().replace(/:/g, "");
  const f = `lg-${uid}`;
  const svg = (
    <svg width={size} height={size} viewBox="0 0 100 100" role={title ? "img" : undefined} aria-label={title || undefined}
      aria-hidden={title ? undefined : true} focusable="false" className="ui-logo-svg">
      <defs>
        <filter id={f} x="-25%" y="-25%" width="150%" height="150%">
          <feDropShadow dx="3.5" dy="3.5" stdDeviation="3" style={{ floodColor: "var(--sh-dark)" }} floodOpacity="0.95" />
          <feDropShadow dx="-3" dy="-3" stdDeviation="3" style={{ floodColor: "var(--sh-light)" }} floodOpacity="0.95" />
        </filter>
      </defs>
      {/* plot boundary */}
      <rect x="8" y="8" width="84" height="84" rx="21" filter={`url(#${f})`} style={{ fill: "var(--neo-grad-dark)", stroke: "var(--ink-3)" }} strokeOpacity="0.45" strokeWidth="2.5" />
      {/* setback envelope */}
      <rect x="23" y="23" width="54" height="54" rx="14" filter={`url(#${f})`} style={{ fill: "var(--neo-grad-light)", stroke: "var(--ink-3)" }} strokeOpacity="0.45" strokeWidth="2.5" />
      {/* core = deterministic rule engine */}
      <rect x="37" y="37" width="26" height="26" rx="7" style={{ fill: "var(--det)" }} />
      <rect x="40.5" y="39.5" width="19" height="5.5" rx="2.75" style={{ fill: "#FFFFFF" }} opacity="0.35" />
    </svg>
  );
  return (
    <span className={cx("ui-logo", className)}>
      {variant === "tile" ? <span className="ui-logo-tile" style={{ width: size + 8, height: size + 8 }}>{svg}</span> : svg}
      {wordmark && (
        <span className="ui-logo-word">
          <span className="ui-logo-name">Apartment Planner</span>
          <span className="ui-logo-sub">IVAI</span>
        </span>
      )}
    </span>
  );
}
