import { forwardRef, type InputHTMLAttributes, type ReactNode, type TextareaHTMLAttributes, useId } from "react";
import { cx } from "./cx";

interface FieldChrome {
  /** Visible label (engraved). Strongly recommended; otherwise pass aria-label. */
  label?: ReactNode;
  /** Helper text under the input (linked via aria-describedby). */
  hint?: ReactNode;
  /** Error text; sets aria-invalid and turns the well's rim red. */
  error?: ReactNode;
  /** Unit suffix engraved inside the well (e.g. "m"). */
  unit?: string;
  /** Leading adornment inside the well (e.g. a search icon). */
  leading?: ReactNode;
  /** Trailing slot outside the well (e.g. a Show/Hide button). */
  trailing?: ReactNode;
  /** Class on the outer wrapper. */
  className?: string;
}

export type FieldProps = FieldChrome & Omit<InputHTMLAttributes<HTMLInputElement>, "className">;

/** Text / number input carved into the slab. */
export const Field = forwardRef<HTMLInputElement, FieldProps>(function Field(
  { label, hint, error, unit, leading, trailing, className, id, ...rest }, ref,
) {
  const auto = useId();
  const fid = id ?? auto;
  const desc = [hint && `${fid}-h`, error && `${fid}-e`].filter(Boolean).join(" ") || undefined;
  return (
    <div className={cx("ui-field", error != null && "is-invalid", className)}>
      {label != null && <label className="ui-field-label" htmlFor={fid}>{label}</label>}
      <div className="ui-field-row">
        <div className="ui-field-well">
          {leading && <span className="ui-field-leading">{leading}</span>}
          <input ref={ref} id={fid} className="ui-field-input" aria-invalid={error != null || undefined} aria-describedby={desc} {...rest} />
          {unit && <span className="ui-field-unit" aria-hidden="true">{unit}</span>}
        </div>
        {trailing}
      </div>
      {hint != null && <span id={`${fid}-h`} className="ui-field-hint">{hint}</span>}
      {error != null && <span id={`${fid}-e`} className="ui-field-error" role="alert">{error}</span>}
    </div>
  );
});

export type TextAreaProps = Omit<FieldChrome, "unit" | "leading" | "trailing"> & Omit<TextareaHTMLAttributes<HTMLTextAreaElement>, "className">;

/** Multi-line text in a carved well (briefs, notes). */
export const TextArea = forwardRef<HTMLTextAreaElement, TextAreaProps>(function TextArea(
  { label, hint, error, className, id, rows = 3, ...rest }, ref,
) {
  const auto = useId();
  const fid = id ?? auto;
  const desc = [hint && `${fid}-h`, error && `${fid}-e`].filter(Boolean).join(" ") || undefined;
  return (
    <div className={cx("ui-field", error != null && "is-invalid", className)}>
      {label != null && <label className="ui-field-label" htmlFor={fid}>{label}</label>}
      <div className="ui-field-well is-multiline">
        <textarea ref={ref} id={fid} rows={rows} className="ui-field-input" aria-invalid={error != null || undefined} aria-describedby={desc} {...rest} />
      </div>
      {hint != null && <span id={`${fid}-h`} className="ui-field-hint">{hint}</span>}
      {error != null && <span id={`${fid}-e`} className="ui-field-error" role="alert">{error}</span>}
    </div>
  );
});
