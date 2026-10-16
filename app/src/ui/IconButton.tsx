import { type ButtonHTMLAttributes, forwardRef, type ReactNode } from "react";
import { cx } from "./cx";
import { Led, type LedStatus } from "./Led";

export interface IconButtonProps extends Omit<ButtonHTMLAttributes<HTMLButtonElement>, "aria-label"> {
  /** Required accessible name; also used as the native tooltip unless `title` is given. */
  label: string;
  /** The glyph (usually `<Icon name="..." />`). */
  icon: ReactNode;
  size?: "sm" | "md";
  /** Toggle state -> aria-pressed + latched (inset) look. */
  pressed?: boolean;
  /** Small counter badge (e.g. waiting decisions). Hidden when 0/undefined. */
  badge?: number | string;
  /** Status pip in the corner. */
  led?: LedStatus;
  pulse?: boolean;
  /** `round` (default) or `square` puck. */
  shape?: "round" | "square";
}

/** Round convex puck holding a single icon. Always has an accessible label. */
export const IconButton = forwardRef<HTMLButtonElement, IconButtonProps>(function IconButton(
  { label, icon, size = "md", pressed, badge, led, pulse, shape = "round", className, title, type = "button", ...rest },
  ref,
) {
  const showBadge = badge !== undefined && badge !== 0 && badge !== "";
  return (
    <button ref={ref} type={type} aria-label={showBadge ? `${label} (${badge})` : label} title={title ?? label}
      aria-pressed={pressed === undefined ? undefined : pressed}
      className={cx("ui-icon-btn", `ui-icon-btn-${size}`, shape === "square" && "is-square", pressed && "is-latched", className)} {...rest}>
      {icon}
      {led && <Led status={led} pulse={pulse} className="ui-icon-btn-led" />}
      {showBadge && <span className="ui-icon-btn-badge" aria-hidden="true">{badge}</span>}
    </button>
  );
});
