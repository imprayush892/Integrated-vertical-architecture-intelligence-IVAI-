import { type KeyboardEvent, type ReactNode, useRef } from "react";
import { cx } from "./cx";
import { Led, type LedStatus } from "./Led";

export interface TabItem<T extends string = string> {
  id: T;
  label: ReactNode;
  /** Optional status pip on the tab. */
  led?: LedStatus;
  pulse?: boolean;
  disabled?: boolean;
  /** id of the element this tab controls (sets aria-controls). */
  panelId?: string;
}

export interface TabsProps<T extends string = string> {
  items: TabItem<T>[];
  value: T;
  onChange: (id: T) => void;
  /** Accessible name of the tab list. */
  label: string;
  size?: "sm" | "md";
  /** Stretch tabs to fill the width. */
  block?: boolean;
  className?: string;
}

/**
 * Segmented "switch bank": raised tab sits on a carved rail.
 * Keyboard: Left/Right (or Up/Down) move + select, Home/End jump; roving tabindex.
 */
export function Tabs<T extends string = string>({ items, value, onChange, label, size = "md", block, className }: TabsProps<T>) {
  const refs = useRef<Array<HTMLButtonElement | null>>([]);
  const enabled = items.map((t, i) => (t.disabled ? -1 : i)).filter((i) => i >= 0);
  const onKey = (e: KeyboardEvent, i: number) => {
    const pos = enabled.indexOf(i);
    let next: number | undefined;
    if (e.key === "ArrowRight" || e.key === "ArrowDown") next = enabled[(pos + 1) % enabled.length];
    else if (e.key === "ArrowLeft" || e.key === "ArrowUp") next = enabled[(pos - 1 + enabled.length) % enabled.length];
    else if (e.key === "Home") next = enabled[0];
    else if (e.key === "End") next = enabled[enabled.length - 1];
    if (next === undefined) return;
    e.preventDefault();
    refs.current[next]?.focus();
    onChange(items[next].id);
  };
  return (
    <div role="tablist" aria-label={label} className={cx("ui-tabs", `ui-tabs-${size}`, block && "is-block", className)}>
      {items.map((t, i) => {
        const on = t.id === value;
        return (
          <button key={t.id} ref={(el) => { refs.current[i] = el; }} type="button" role="tab" aria-selected={on}
            aria-controls={t.panelId} tabIndex={on ? 0 : -1} disabled={t.disabled}
            className={cx("ui-tab", on && "is-active")} onClick={() => onChange(t.id)} onKeyDown={(e) => onKey(e, i)}>
            {t.led && <Led status={t.led} pulse={t.pulse} />}
            <span>{t.label}</span>
          </button>
        );
      })}
    </div>
  );
}
