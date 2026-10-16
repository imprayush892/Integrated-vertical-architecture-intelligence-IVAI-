import { cx } from "./cx";
import { Icon } from "./icons";
import { Led, type LedStatus } from "./Led";

export interface ToastItem { id: number; kind: "info" | "error" | "ok"; text: string }

const LED: Record<ToastItem["kind"], LedStatus> = { info: "cobalt", ok: "pass", error: "fail" };

export interface ToastViewportProps {
  toasts: ToastItem[];
  onDismiss: (id: number) => void;
}

/**
 * Stack of floating slabs, bottom-centre, above the status bar. Errors are announced assertively
 * (role="alert"), the rest politely. Feed it `useStore().toasts`.
 */
export function ToastViewport({ toasts, onDismiss }: ToastViewportProps) {
  return (
    <div className="ui-toasts" aria-live="polite" aria-relevant="additions">
      {toasts.map((t) => (
        <div key={t.id} role={t.kind === "error" ? "alert" : "status"} className={cx("ui-toast", `is-${t.kind}`)}>
          <Led status={LED[t.kind]} size="lg" />
          <span className="ui-toast-text">{t.text}</span>
          <button type="button" className="ui-toast-x" aria-label="Dismiss notification" onClick={() => onDismiss(t.id)}>
            <Icon name="close" size={14} />
          </button>
        </div>
      ))}
    </div>
  );
}
