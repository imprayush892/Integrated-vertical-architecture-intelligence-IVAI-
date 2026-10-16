import { type HTMLAttributes, type ReactNode, useId } from "react";
import { cx } from "./cx";

/** The four kit surface profiles (+ a deeper well for viewports). */
export type SurfaceProfile = "flat" | "well" | "well-deep" | "dome" | "dish";

export interface PanelProps extends Omit<HTMLAttributes<HTMLElement>, "title"> {
  /**
   * Surface curvature:
   * - `flat` extruded plinth: cards, docks, modal slabs (default)
   * - `well` / `well-deep` carved: viewports, readout groups, lists that "hold" content
   * - `dome` convex: tactile/draggable things
   * - `dish` concave: dial wells, charts (sun path, wind rose)
   */
  profile?: SurfaceProfile;
  /** Element to render. Default `section` when a title is given, else `div`. */
  as?: "div" | "section" | "article" | "aside" | "header" | "footer" | "nav";
  /** Heading text; also labels the region for screen readers. */
  title?: ReactNode;
  /** Mono uppercase line above the title (e.g. "STAGE 03"). */
  eyebrow?: ReactNode;
  /** Right-aligned header slot (chips, icon buttons). */
  actions?: ReactNode;
  /** Inner padding. Default `md` (16px). */
  pad?: "none" | "sm" | "md" | "lg";
  /** Corner radius. Default `lg`. */
  radius?: "md" | "lg" | "xl";
  /** Use the small shadow (for nested surfaces). */
  small?: boolean;
  children?: ReactNode;
}

/** A neumorphic surface. Nest `well` inside `flat`; avoid flat-in-flat more than one level deep. */
export function Panel({ profile = "flat", as, title, eyebrow, actions, pad = "md", radius = "lg", small, className, children, ...rest }: PanelProps) {
  const id = useId();
  const Tag = as ?? (title ? "section" : "div");
  const hasHead = title != null || eyebrow != null || actions != null;
  return (
    <Tag className={cx("ui-panel", `neo-${profile}`, `ui-pad-${pad}`, radius !== "lg" && `neo-r-${radius}`, small && "neo-sm", className)}
      aria-labelledby={title != null ? `${id}-t` : undefined} {...rest}>
      {hasHead && (
        <div className="ui-panel-head">
          <div className="ui-panel-titles">
            {eyebrow != null && <div className="eyebrow">{eyebrow}</div>}
            {title != null && <h3 id={`${id}-t`} className="ui-panel-title">{title}</h3>}
          </div>
          {actions != null && <div className="ui-panel-actions">{actions}</div>}
        </div>
      )}
      {children}
    </Tag>
  );
}
