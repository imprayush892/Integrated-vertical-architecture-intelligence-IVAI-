import type { ReactNode } from "react";
import { cx } from "./cx";

export interface EmptyStateProps {
  /** Glyph shown in a carved dish. */
  icon?: ReactNode;
  title: ReactNode;
  /** One or two sentences: what is missing and how to get it. */
  children?: ReactNode;
  /** Primary way forward (usually one LatchButton). */
  action?: ReactNode;
  /** Fill the parent and centre. */
  fill?: boolean;
  className?: string;
}

/** "Nothing here yet" state that always points to the next step. */
export function EmptyState({ icon, title, children, action, fill, className }: EmptyStateProps) {
  return (
    <div className={cx("ui-empty", fill && "is-fill", className)}>
      {icon && <div className="ui-empty-icon neo-dish">{icon}</div>}
      <h3 className="ui-empty-title">{title}</h3>
      {children && <div className="ui-empty-body">{children}</div>}
      {action && <div className="ui-empty-action">{action}</div>}
    </div>
  );
}
