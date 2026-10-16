import { type ReactNode, useId } from "react";
import { cx } from "./cx";

export interface ToggleProps {
  checked: boolean;
  onChange: (checked: boolean) => void;
  /** Visible label (also the accessible name). */
  label: ReactNode;
  /** Small helper line under the label. */
  description?: ReactNode;
  disabled?: boolean;
  /** LED colour when on. Default `det`. Use `ai` for switches that hand work to Claude. */
  tone?: "det" | "ai" | "cobalt";
  className?: string;
}

/** On/off hardware switch: a dome thumb sliding in a carved track. role="switch", Space/Enter toggles. */
export function Toggle({ checked, onChange, label, description, disabled, tone = "det", className }: ToggleProps) {
  const id = useId();
  return (
    <div className={cx("ui-toggle", disabled && "is-disabled", className)}>
      <button id={id} type="button" role="switch" aria-checked={checked} disabled={disabled}
        aria-describedby={description ? `${id}-d` : undefined}
        className={cx("ui-toggle-track", checked && "is-on", `tone-${tone}`)} onClick={() => onChange(!checked)}>
        <span className="ui-toggle-thumb"><span className={cx("led", checked ? tone : "idle")} aria-hidden="true" /></span>
      </button>
      <label htmlFor={id} className="ui-toggle-text">
        <span className="ui-toggle-label">{label}</span>
        {description && <span id={`${id}-d`} className="ui-toggle-desc">{description}</span>}
      </label>
    </div>
  );
}
