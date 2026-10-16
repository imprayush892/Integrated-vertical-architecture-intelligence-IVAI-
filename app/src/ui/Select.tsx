import { forwardRef, type ReactNode, type SelectHTMLAttributes, useId } from "react";
import { cx } from "./cx";
import { Icon } from "./icons";

export interface SelectOption { value: string; label: string; disabled?: boolean }

export interface SelectProps extends Omit<SelectHTMLAttributes<HTMLSelectElement>, "className"> {
  /** Visible label; otherwise pass aria-label. */
  label?: ReactNode;
  hint?: ReactNode;
  /** Options as data. You can pass `<option>` children instead (or both: data first). */
  options?: SelectOption[];
  /** Compact inline select (no label row), e.g. a tower picker in a panel header. */
  compact?: boolean;
  className?: string;
}

/** Native select (keeps OS keyboard behaviour) carved into a well, with an engraved chevron. */
export const Select = forwardRef<HTMLSelectElement, SelectProps>(function Select(
  { label, hint, options, compact, className, id, children, ...rest }, ref,
) {
  const auto = useId();
  const fid = id ?? auto;
  return (
    <div className={cx("ui-field", "ui-select", compact && "is-compact", className)}>
      {label != null && <label className="ui-field-label" htmlFor={fid}>{label}</label>}
      <div className="ui-field-well">
        <select ref={ref} id={fid} className="ui-field-input" aria-describedby={hint ? `${fid}-h` : undefined} {...rest}>
          {options?.map((o) => <option key={o.value} value={o.value} disabled={o.disabled}>{o.label}</option>)}
          {children}
        </select>
        <Icon name="chevron-down" size={14} className="ui-select-chevron" />
      </div>
      {hint != null && <span id={`${fid}-h`} className="ui-field-hint">{hint}</span>}
    </div>
  );
});
