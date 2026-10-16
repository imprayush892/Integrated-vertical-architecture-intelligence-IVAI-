import { type KeyboardEvent, useRef } from "react";
import { Icon, type IconName } from "../ui";
import type { ThemePref } from "./theme";

const OPTS: { id: ThemePref; label: string; icon: IconName }[] = [
  { id: "light", label: "Light (Anodized Silver)", icon: "sun" },
  { id: "dark", label: "Dark (Machined Graphite)", icon: "moon" },
  { id: "system", label: "Match system", icon: "monitor" },
];

/** Three-position rotary-style switch (radiogroup). Arrow keys move and select. */
export function ThemeSwitch({ value, onChange }: { value: ThemePref; onChange: (p: ThemePref) => void }) {
  const refs = useRef<Array<HTMLButtonElement | null>>([]);
  const onKey = (e: KeyboardEvent, i: number) => {
    const d = e.key === "ArrowRight" || e.key === "ArrowDown" ? 1 : e.key === "ArrowLeft" || e.key === "ArrowUp" ? -1 : 0;
    if (!d) return;
    e.preventDefault();
    const n = (i + d + OPTS.length) % OPTS.length;
    refs.current[n]?.focus();
    onChange(OPTS[n].id);
  };
  return (
    <div role="radiogroup" aria-label="Colour theme" className="theme-switch">
      {OPTS.map((o, i) => (
        <button key={o.id} ref={(el) => { refs.current[i] = el; }} type="button" role="radio" aria-checked={value === o.id}
          tabIndex={value === o.id ? 0 : -1} title={o.label} aria-label={o.label}
          className={value === o.id ? "is-active" : undefined} onClick={() => onChange(o.id)} onKeyDown={(e) => onKey(e, i)}>
          <Icon name={o.icon} size={15} />
        </button>
      ))}
    </div>
  );
}
