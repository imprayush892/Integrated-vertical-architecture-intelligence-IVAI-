// Polls the engine's unauthenticated health endpoint so the status bar can show a live connection LED.
import { useEffect, useState } from "react";

export type EngineState = "checking" | "online" | "offline";

export function useEngineHealth(intervalMs = 10000): { state: EngineState; version: string | null } {
  const [state, setState] = useState<EngineState>("checking");
  const [version, setVersion] = useState<string | null>(null);
  useEffect(() => {
    let alive = true;
    const ping = async () => {
      const ctl = new AbortController();
      const t = setTimeout(() => ctl.abort(), 4000);
      try {
        const r = await fetch("/api/health", { signal: ctl.signal, cache: "no-store" });
        const body = r.ok ? await r.json() : null;
        if (!alive) return;
        setState(body?.ok ? "online" : "offline");
        if (body?.version) setVersion(String(body.version));
      } catch {
        if (alive) setState("offline");
      } finally {
        clearTimeout(t);
      }
    };
    void ping();
    const id = setInterval(() => void ping(), intervalMs);
    return () => { alive = false; clearInterval(id); };
  }, [intervalMs]);
  return { state, version };
}
