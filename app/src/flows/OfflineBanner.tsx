// Engine-disconnected banner with automatic retry. The boot-time failure is handled by the shell's
// EngineOffline screen; this covers the engine dropping out while the app is already open.
import { useCallback, useEffect, useRef, useState } from "react";
import { RpcError } from "../lib/api";
import { useStore } from "../lib/store";
import { Icon, LatchButton, Led, Readout } from "../ui";
import "./flows.css";

type Link = "online" | "offline" | "reload";

const RETRY_STEPS = [4, 8, 15]; // seconds between automatic retries, then stays at the last value
const OK_INTERVAL_MS = 6000;
const FAILS_BEFORE_BANNER = 2;

async function ping(): Promise<boolean> {
  const ctl = new AbortController();
  const t = setTimeout(() => ctl.abort(), 3500);
  try {
    const r = await fetch("/api/health", { signal: ctl.signal, cache: "no-store" });
    if (!r.ok) return false;
    const body = (await r.json()) as { ok?: boolean };
    return !!body.ok;
  } catch {
    return false;
  } finally {
    clearTimeout(t);
  }
}

export function OfflineBanner() {
  const [link, setLink] = useState<Link>("online");
  const [retrying, setRetrying] = useState(false);
  const [countdown, setCountdown] = useState<number | null>(null);
  const fails = useRef(0);
  const attempt = useRef(0);
  const linkRef = useRef<Link>("online");
  const update = useCallback((l: Link) => { linkRef.current = l; setLink(l); }, []);

  const recover = useCallback(async () => {
    // Re-sync the store. A restarted engine issues a new session token, which needs a full reload.
    try {
      await useStore.getState().refresh();
      update("online");
      attempt.current = 0;
      useStore.getState().toast("ok", "Engine reconnected. Everything is back in sync.");
    } catch (e) {
      update(e instanceof RpcError && e.code === 401 ? "reload" : "offline");
    }
  }, [update]);

  const check = useCallback(async (manual = false) => {
    if (linkRef.current === "reload") return;
    if (manual) setRetrying(true);
    const ok = await ping();
    if (manual) setRetrying(false);
    if (ok) {
      fails.current = 0;
      if (linkRef.current === "offline") await recover();
    } else {
      fails.current += 1;
      if (manual || fails.current >= FAILS_BEFORE_BANNER) update("offline");
    }
  }, [recover, update]);

  // steady heartbeat while online, backed-off retries while offline
  useEffect(() => {
    let alive = true;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let tick: ReturnType<typeof setInterval> | undefined;
    const loop = async () => {
      if (!alive) return;
      await check();
      if (!alive) return;
      if (linkRef.current === "offline") {
        const secs = RETRY_STEPS[Math.min(attempt.current, RETRY_STEPS.length - 1)];
        attempt.current += 1;
        let left = secs;
        setCountdown(left);
        tick = setInterval(() => { left -= 1; setCountdown(Math.max(0, left)); }, 1000);
        timer = setTimeout(() => { if (tick) clearInterval(tick); setCountdown(null); void loop(); }, secs * 1000);
      } else {
        timer = setTimeout(() => void loop(), OK_INTERVAL_MS);
      }
    };
    timer = setTimeout(() => void loop(), OK_INTERVAL_MS);
    const onVisible = () => { if (document.visibilityState === "visible") void check(); };
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      alive = false;
      if (timer) clearTimeout(timer);
      if (tick) clearInterval(tick);
      document.removeEventListener("visibilitychange", onVisible);
    };
  }, [check]);

  const shown = link !== "online";
  // lift the toast stack above the banner while it is showing
  useEffect(() => {
    if (!shown) return;
    document.body.setAttribute("data-engine-banner", "1");
    return () => document.body.removeAttribute("data-engine-banner");
  }, [shown]);

  if (!shown) return null;

  if (link === "reload") {
    return (
      <div className="fx-banner neo-flat is-warn" role="alert">
        <Led status="warn" size="lg" />
        <div className="fx-banner-text">
          <strong>The engine restarted.</strong>
          <span>Reload to start a fresh session. Saved work is kept.</span>
        </div>
        <LatchButton variant="primary" size="sm" onClick={() => window.location.reload()}>Reload</LatchButton>
      </div>
    );
  }

  return (
    <div className="fx-banner neo-flat is-fail" role="alert">
      <Led status="fail" size="lg" pulse />
      <div className="fx-banner-text">
        <strong>Can't reach the planner engine.</strong>
        <span>Changes can't be saved until it is back. Trying again on its own.</span>
      </div>
      {countdown != null && !retrying && (
        <Readout label="Next try" value={countdown} digits={0} unit="s" tone="warn" size="sm" layout="inline" className="fx-banner-lcd" />
      )}
      <LatchButton size="sm" variant="primary" loading={retrying} icon={<Icon name="redo" size={14} />} onClick={() => void check(true)}>
        {retrying ? "Trying" : "Retry now"}
      </LatchButton>
    </div>
  );
}
