// Guarded localStorage + small platform helpers for the cross-cutting flows.
// Storage can throw (private mode, blocked site data), so every access is wrapped.

export function readStore(key: string): string | null {
  try { return window.localStorage.getItem(key); } catch { return null; }
}

export function writeStore(key: string, value: string): void {
  try { window.localStorage.setItem(key, value); } catch { /* ignore */ }
}

export const ONBOARDED_KEY = "ivai.onboarded";
const RECENT_KEY = "ivai.cmd.recent";

export const isOnboarded = () => readStore(ONBOARDED_KEY) === "1";
export const markOnboarded = () => writeStore(ONBOARDED_KEY, "1");

export function readRecent(): string[] {
  const raw = readStore(RECENT_KEY);
  if (!raw) return [];
  try {
    const v: unknown = JSON.parse(raw);
    return Array.isArray(v) ? v.filter((x): x is string => typeof x === "string").slice(0, 4) : [];
  } catch { return []; }
}

export function pushRecent(id: string): void {
  const next = [id, ...readRecent().filter((x) => x !== id)].slice(0, 4);
  writeStore(RECENT_KEY, JSON.stringify(next));
}

export const isMac = (): boolean =>
  typeof navigator !== "undefined" && /Mac|iPhone|iPad/i.test(navigator.platform || navigator.userAgent || "");

/** "Ctrl" or "⌘" for display. */
export const modKey = (): string => (isMac() ? "⌘" : "Ctrl");

export const isTyping = (t: EventTarget | null): boolean => {
  const el = t as HTMLElement | null;
  return !!el && (el.isContentEditable || ["INPUT", "TEXTAREA", "SELECT"].includes(el.tagName));
};

/** True when a modal dialog other than one carrying `data-fx-own` is open. */
export const otherModalOpen = (): boolean =>
  Array.from(document.querySelectorAll('[aria-modal="true"]')).some((n) => !(n as HTMLElement).hasAttribute("data-fx-own"));

export const reducedMotion = (): boolean =>
  typeof window !== "undefined" && !!window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;
