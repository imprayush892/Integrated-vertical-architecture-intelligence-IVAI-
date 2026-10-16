import { type ReactNode, useEffect, useId, useRef } from "react";
import { createPortal } from "react-dom";
import { cx } from "./cx";
import { IconButton } from "./IconButton";
import { Icon } from "./icons";

const FOCUSABLE = 'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

/** Focus management shared by Modal and Drawer: focus in, trap Tab, Escape closes, restore focus on close. */
function useDialog(open: boolean, onClose: () => void) {
  const ref = useRef<HTMLDivElement>(null);
  const closeRef = useRef(onClose);
  closeRef.current = onClose;
  useEffect(() => {
    if (!open) return;
    const prev = document.activeElement as HTMLElement | null;
    const el = ref.current;
    const first = el?.querySelector<HTMLElement>("[data-autofocus]") ?? el?.querySelector<HTMLElement>(FOCUSABLE);
    (first ?? el)?.focus();
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") { e.stopPropagation(); closeRef.current(); return; }
      if (e.key !== "Tab" || !el) return;
      const nodes = Array.from(el.querySelectorAll<HTMLElement>(FOCUSABLE)).filter((n) => n.offsetParent !== null);
      if (!nodes.length) { e.preventDefault(); return; }
      const a = nodes[0], b = nodes[nodes.length - 1];
      if (e.shiftKey && document.activeElement === a) { e.preventDefault(); b.focus(); }
      else if (!e.shiftKey && document.activeElement === b) { e.preventDefault(); a.focus(); }
    };
    document.addEventListener("keydown", onKey, true);
    return () => { document.removeEventListener("keydown", onKey, true); prev?.focus?.(); };
  }, [open]);
  return ref;
}

export interface ModalProps {
  open: boolean;
  onClose: () => void;
  /** Dialog title (accessible name). */
  title: ReactNode;
  /** Mono eyebrow above the title. */
  eyebrow?: ReactNode;
  children: ReactNode;
  /** Button row at the bottom. */
  footer?: ReactNode;
  size?: "sm" | "md" | "lg";
  /** Close when the scrim is clicked. Default true. */
  dismissible?: boolean;
  className?: string;
}

/** Centered flat slab over a scrim. Escape / scrim / close button dismiss. Mark the first field with `data-autofocus`. */
export function Modal({ open, onClose, title, eyebrow, children, footer, size = "md", dismissible = true, className }: ModalProps) {
  const id = useId();
  const ref = useDialog(open, onClose);
  if (!open) return null;
  return createPortal(
    <div className="ui-scrim" onMouseDown={(e) => { if (dismissible && e.target === e.currentTarget) onClose(); }}>
      <div ref={ref} role="dialog" aria-modal="true" aria-labelledby={`${id}-t`} tabIndex={-1}
        className={cx("ui-modal", "neo-flat", `ui-modal-${size}`, className)}>
        <header className="ui-modal-head">
          <div>
            {eyebrow != null && <div className="eyebrow">{eyebrow}</div>}
            <h2 id={`${id}-t`} className="ui-modal-title">{title}</h2>
          </div>
          <IconButton label="Close" size="sm" icon={<Icon name="close" />} onClick={onClose} />
        </header>
        <div className="ui-modal-body">{children}</div>
        {footer && <footer className="ui-modal-foot">{footer}</footer>}
      </div>
    </div>,
    document.body,
  );
}

export interface DrawerProps extends Omit<ModalProps, "size"> {
  /** Edge to slide from. Default right. */
  side?: "left" | "right";
  /** CSS width. Default min(440px, 100vw). */
  width?: string;
}

/** Edge sheet (inbox, inspector on small screens). Same focus rules as Modal. */
export function Drawer({ open, onClose, title, eyebrow, children, footer, side = "right", width, dismissible = true, className }: DrawerProps) {
  const id = useId();
  const ref = useDialog(open, onClose);
  if (!open) return null;
  return createPortal(
    <div className="ui-scrim is-drawer" onMouseDown={(e) => { if (dismissible && e.target === e.currentTarget) onClose(); }}>
      <div ref={ref} role="dialog" aria-modal="true" aria-labelledby={`${id}-t`} tabIndex={-1}
        className={cx("ui-drawer", "neo-flat", `is-${side}`, className)} style={width ? { width } : undefined}>
        <header className="ui-modal-head">
          <div>
            {eyebrow != null && <div className="eyebrow">{eyebrow}</div>}
            <h2 id={`${id}-t`} className="ui-modal-title">{title}</h2>
          </div>
          <IconButton label="Close" size="sm" icon={<Icon name="close" />} onClick={onClose} />
        </header>
        <div className="ui-modal-body">{children}</div>
        {footer && <footer className="ui-modal-foot">{footer}</footer>}
      </div>
    </div>,
    document.body,
  );
}
