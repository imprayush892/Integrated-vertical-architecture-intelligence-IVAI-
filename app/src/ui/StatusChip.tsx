import type { ReactNode } from "react";
import { cx } from "./cx";
import { Led, type LedStatus } from "./Led";

/** Statutory check outcome (mirrors CheckResult.result; "n/a" accepted too). */
export type CheckStatus = "pass" | "review" | "fail" | "na" | "n/a";

export interface StatusChipProps {
  status: CheckStatus;
  /** Override the default text (PASS / REVIEW / FAIL / N/A). */
  children?: ReactNode;
  /** Optional count shown after the label. */
  count?: number;
  /** `inset` = latched-looking chip (for summaries inside wells). */
  inset?: boolean;
  className?: string;
}

const MAP: Record<"pass" | "review" | "fail" | "na", { led: LedStatus; text: string }> = {
  pass: { led: "pass", text: "PASS" },
  review: { led: "warn", text: "REVIEW" },
  fail: { led: "fail", text: "FAIL" },
  na: { led: "idle", text: "N/A" },
};

/** Statutory pass / review / fail chip. Colour is backed by text, so it never relies on colour alone. */
export function StatusChip({ status, children, count, inset, className }: StatusChipProps) {
  const key = status === "n/a" ? "na" : status;
  const m = MAP[key];
  return (
    <span className={cx("ui-chip", `ui-chip-${key}`, inset && "is-inset", className)}>
      <Led status={m.led} />
      <span>{children ?? m.text}{count != null ? ` ${count}` : ""}</span>
    </span>
  );
}

/** Generic neutral chip with optional LED - for tags that are not statutory results. */
export function Chip({ led, children, inset, className, title }: { led?: LedStatus; children: ReactNode; inset?: boolean; className?: string; title?: string }) {
  return (
    <span className={cx("ui-chip", inset && "is-inset", className)} title={title}>
      {led && <Led status={led} />}<span>{children}</span>
    </span>
  );
}
