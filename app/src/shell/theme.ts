// Theme preference: light / dark / system, persisted in localStorage (guarded - storage can throw).
import { useCallback, useEffect, useState } from "react";

export type ThemePref = "light" | "dark" | "system";
const KEY = "ivai.theme";

function read(): ThemePref {
  try {
    const v = window.localStorage.getItem(KEY);
    if (v === "light" || v === "dark" || v === "system") return v;
  } catch { /* private mode / blocked storage */ }
  return "system";
}

function apply(p: ThemePref) {
  const root = document.documentElement;
  if (p === "system") root.removeAttribute("data-theme");
  else root.setAttribute("data-theme", p);
}

// Apply before first render to avoid a flash of the wrong theme.
if (typeof document !== "undefined") apply(read());

const systemDark = () => typeof window !== "undefined" && !!window.matchMedia?.("(prefers-color-scheme: dark)").matches;

export function useTheme() {
  const [pref, setPrefState] = useState<ThemePref>(read);
  const [sysDark, setSysDark] = useState(systemDark);
  useEffect(() => {
    const mq = window.matchMedia?.("(prefers-color-scheme: dark)");
    if (!mq) return;
    const on = () => setSysDark(mq.matches);
    mq.addEventListener("change", on);
    return () => mq.removeEventListener("change", on);
  }, []);
  const setPref = useCallback((p: ThemePref) => {
    setPrefState(p);
    apply(p);
    try { window.localStorage.setItem(KEY, p); } catch { /* ignore */ }
  }, []);
  const resolved: "light" | "dark" = pref === "system" ? (sysDark ? "dark" : "light") : pref;
  return { pref, resolved, setPref };
}

/** Small persisted boolean for shell UI (e.g. inspector open). */
export function usePersistedFlag(key: string, initial: () => boolean): [boolean, (v: boolean) => void] {
  const [v, setV] = useState<boolean>(() => {
    try {
      const s = window.localStorage.getItem(key);
      if (s === "1" || s === "0") return s === "1";
    } catch { /* ignore */ }
    return initial();
  });
  const set = useCallback((nv: boolean) => {
    setV(nv);
    try { window.localStorage.setItem(key, nv ? "1" : "0"); } catch { /* ignore */ }
  }, [key]);
  return [v, set];
}
