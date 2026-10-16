/** Join class names, skipping falsy values. `cx("a", on && "b")` -> "a b". */
export function cx(...parts: Array<string | false | null | undefined | 0>): string {
  return parts.filter(Boolean).join(" ");
}
