import { cx } from "./cx";

/** Small rotating ring. Decorative unless `label` is given. Static under reduced motion. */
export function Spinner({ size = 14, label, className }: { size?: number; label?: string; className?: string }) {
  return <span className={cx("ui-spinner", className)} style={{ width: size, height: size }}
    role={label ? "status" : undefined} aria-label={label} aria-hidden={label ? undefined : true} />;
}
