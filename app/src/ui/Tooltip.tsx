import { cloneElement, isValidElement, type ReactElement, type ReactNode, useId, useState } from "react";
import { cx } from "./cx";

export interface TooltipProps {
  /** Tooltip text/content. Keep it short; it is also exposed via aria-describedby. */
  content: ReactNode;
  /** One focusable child (button, link, input). */
  children: ReactElement;
  side?: "top" | "bottom" | "left" | "right";
  className?: string;
}

/** Hover/focus tooltip. Escape hides it. The child gets aria-describedby. */
export function Tooltip({ content, children, side = "top", className }: TooltipProps) {
  const id = useId();
  const [open, setOpen] = useState(false);
  const child = isValidElement(children)
    ? cloneElement(children as ReactElement<Record<string, unknown>>, { "aria-describedby": open ? id : undefined })
    : children;
  return (
    <span className={cx("ui-tip-anchor", className)}
      onMouseEnter={() => setOpen(true)} onMouseLeave={() => setOpen(false)}
      onFocus={() => setOpen(true)} onBlur={() => setOpen(false)}
      onKeyDown={(e) => { if (e.key === "Escape") setOpen(false); }}>
      {child}
      {open && <span role="tooltip" id={id} className={cx("ui-tip", `is-${side}`)}>{content}</span>}
    </span>
  );
}
