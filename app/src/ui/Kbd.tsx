import type { ReactNode } from "react";

/** Keyboard key cap, e.g. <Kbd>1</Kbd>–<Kbd>8</Kbd>. */
export function Kbd({ children }: { children: ReactNode }) {
  return <kbd className="ui-kbd">{children}</kbd>;
}
