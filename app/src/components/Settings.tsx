import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { rpc } from "../lib/api";
import { useStore } from "../lib/store";
import { Chip, Icon, LatchButton, Led, Modal, Readout, Select, Skeleton, Tabs, Toggle, Field } from "../ui";
import "./agent.css";

export interface AppSettings { mapbox_token: string; map_provider: string; imagery: string; geocoder: string; crash_reports: boolean }

type TokenCheck = { level: "empty" | "ok" | "error"; message: string };

/** Format check only (no network). A public Mapbox token looks like pk.<payload>.<signature>. */
export function checkToken(raw: string): TokenCheck {
  const v = raw.trim();
  if (!v) return { level: "empty", message: "No token. The app uses Esri imagery and OpenStreetMap search." };
  if (/\s/.test(v)) return { level: "error", message: "Remove spaces or line breaks from the token." };
  if (v.startsWith("sk.")) return { level: "error", message: "That is a secret token (sk.). Use a public one: it starts with pk." };
  if (!v.startsWith("pk.")) return { level: "error", message: "A public Mapbox token starts with pk." };
  if (v.length < 30 || !/^pk\.[\w-]+(\.[\w-]+)+$/.test(v)) return { level: "error", message: "This looks cut short or has odd characters. Copy the whole token." };
  return { level: "ok", message: "Format looks right." };
}

type ThemeChoice = "light" | "dark" | "system";
const THEME_ORDER: ThemeChoice[] = ["light", "dark", "system"];
const readTheme = (): ThemeChoice => {
  const a = typeof document !== "undefined" ? document.documentElement.getAttribute("data-theme") : null;
  return a === "light" || a === "dark" ? a : "system";
};
/** Drive the top-bar theme switch so the shell (canvas remount, switch state) stays in sync. Falls back to the DOM + storage. */
function applyTheme(p: ThemeChoice) {
  const btn = document.querySelectorAll<HTMLButtonElement>('.theme-switch button[role="radio"]')[THEME_ORDER.indexOf(p)];
  if (btn) { btn.click(); return; }
  if (p === "system") document.documentElement.removeAttribute("data-theme");
  else document.documentElement.setAttribute("data-theme", p);
  try { window.localStorage.setItem("ivai.theme", p); } catch { /* storage blocked */ }
}

const PROVIDERS = [
  { value: "auto", label: "Automatic: Mapbox when a token is set, else Esri" },
  { value: "mapbox", label: "Mapbox satellite with street names" },
  { value: "esri", label: "Esri World Imagery" },
];

type TestState = { s: "idle" } | { s: "testing" } | { s: "ok" } | { s: "fail"; msg: string };

export default function Settings({ onClose, onSaved }: { onClose: () => void; onSaved: (s: AppSettings) => void }) {
  const [s0, setS0] = useState<AppSettings | null>(null);
  const [loadErr, setLoadErr] = useState<string | null>(null);
  const [provider, setProvider] = useState("auto");
  const [crash, setCrash] = useState(false);
  const [token, setToken] = useState("");
  const [touched, setTouched] = useState(false);
  const [show, setShow] = useState(false);
  const [saving, setSaving] = useState(false);
  const [saveErr, setSaveErr] = useState<string | null>(null);
  const [test, setTest] = useState<TestState>({ s: "idle" });
  const [theme, setTheme] = useState<ThemeChoice>(readTheme);
  const toast = useStore((x) => x.toast);
  const tokenRef = useRef<HTMLInputElement>(null);

  const load = useCallback(() => {
    setLoadErr(null);
    rpc<AppSettings>("get_settings").then((v) => {
      setS0(v); setToken(v.mapbox_token); setProvider(v.map_provider); setCrash(v.crash_reports);
    }).catch((e) => setLoadErr(e instanceof Error ? e.message : String(e)));
  }, []);
  useEffect(() => { load(); }, [load]);

  const check = useMemo(() => checkToken(token), [token]);
  const hasToken = check.level === "ok";
  const dirty = !!s0 && (token.trim() !== s0.mapbox_token || provider !== s0.map_provider || crash !== s0.crash_reports);
  const showTokenErr = check.level === "error" && (touched || token.trim() === s0?.mapbox_token);
  const providerWarn = provider === "mapbox" && !hasToken;

  // What the app will use after Save (mirrors the engine's rule).
  const nextImagery = hasToken && provider !== "esri" ? "Mapbox" : "Esri";
  const nextSearch = hasToken ? "Mapbox" : "OpenStreetMap";

  const runTest = async () => {
    if (!hasToken) return;
    setTest({ s: "testing" });
    const ctl = new AbortController();
    const timer = setTimeout(() => ctl.abort(), 8000);
    try {
      const r = await fetch(`https://api.mapbox.com/tokens/v2?access_token=${encodeURIComponent(token.trim())}`, { signal: ctl.signal });
      if (r.ok) setTest({ s: "ok" });
      else if (r.status === 401 || r.status === 403) setTest({ s: "fail", msg: "Mapbox did not accept this token. Check it in your Mapbox account." });
      else setTest({ s: "fail", msg: `Mapbox answered with status ${r.status}. Try again in a moment.` });
    } catch {
      setTest({ s: "fail", msg: "Could not reach Mapbox. Check your connection. You can still save the token." });
    } finally { clearTimeout(timer); }
  };

  const save = async () => {
    setTouched(true);
    if (check.level === "error") { tokenRef.current?.focus(); return; }
    setSaving(true); setSaveErr(null);
    try {
      const v = await rpc<AppSettings>("set_settings", { mapbox_token: token.trim(), map_provider: provider, crash_reports: crash });
      onSaved(v);
      toast("ok", v.imagery === "mapbox" ? "Saved. Map and search now use Mapbox." : "Saved.");
      onClose();
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e);
      setSaveErr(msg);
      toast("error", msg);
    } finally { setSaving(false); }
  };

  const onTheme = (t: ThemeChoice) => { setTheme(t); applyTheme(t); };

  const footer = (
    <>
      {dirty && <span className="ax-inline" style={{ marginRight: "auto" }}><Led status="ai" /><span className="ax-hint">Unsaved changes</span></span>}
      <LatchButton onClick={onClose} led={false}>Cancel</LatchButton>
      <LatchButton variant="primary" loading={saving} disabled={!s0 || !dirty || (touched && check.level === "error")} onClick={() => void save()}>
        {dirty || !s0 ? "Save" : "Saved"}
      </LatchButton>
    </>
  );

  return (
    <Modal open onClose={onClose} size="md" className="ax-modal" eyebrow="Settings" title="Settings" footer={footer}>
      {loadErr && (
        <div className="ax-alert" role="alert">
          <Led status="fail" />
          <div className="ax-alert-body"><b>Could not load settings.</b> {loadErr}{" "}
            <LatchButton size="sm" variant="ghost" led={false} onClick={load}>Try again</LatchButton></div>
        </div>
      )}
      {!s0 && !loadErr && (
        <div className="ax-set" role="status" aria-label="Loading settings">
          <Skeleton height={16} width="40%" /><Skeleton height={40} /><Skeleton height={40} /><Skeleton lines={2} />
        </div>
      )}
      {s0 && (
        <div className="ax-set">
          <section className="ax-section" aria-labelledby="set-map">
            <div className="ax-section-head">
              <Icon name="map" size={16} /><h3 id="set-map">Map and place search</h3>
              <span className="spacer" style={{ flex: 1 }} />
              <Chip led={hasToken ? "det" : "idle"} inset>{hasToken ? "Mapbox token set" : "No token"}</Chip>
            </div>
            <p className="ax-note">
              A free public Mapbox token adds satellite imagery with street names and Mapbox place search. The free tier covers normal use,
              and results are cached on this computer. Create one at{" "}
              <a href="https://account.mapbox.com/access-tokens/" target="_blank" rel="noreferrer">account.mapbox.com</a>.
            </p>
            <div className="ax-token-row">
              <Field ref={tokenRef} label="Mapbox public token" type={show ? "text" : "password"} value={token} placeholder="pk.eyJ1Ijoi…"
                autoComplete="off" spellCheck={false} data-autofocus
                onChange={(e) => { setToken(e.target.value); setTest({ s: "idle" }); }} onBlur={() => setTouched(true)}
                error={showTokenErr ? check.message : undefined}
                hint={showTokenErr ? undefined : check.level === "ok" ? "Format looks right. Stored on this computer only." : "Starts with pk. Leave empty to skip."} />
              <div className="ax-token-btns">
                <LatchButton latched={show} led={false} onClick={() => setShow(!show)} aria-label="Show token text">{show ? "Hide" : "Show"}</LatchButton>
                <LatchButton variant="det" disabled={!hasToken} loading={test.s === "testing"} onClick={() => void runTest()}>Test</LatchButton>
                {token && <LatchButton variant="ghost" led={false} onClick={() => { setToken(""); setTest({ s: "idle" }); setTouched(false); tokenRef.current?.focus(); }}>Clear</LatchButton>}
              </div>
            </div>
            {test.s === "ok" && <div className="ax-alert is-ok" role="status"><Led status="det" /><div className="ax-alert-body">Mapbox accepted this token.</div></div>}
            {test.s === "fail" && <div className="ax-alert is-warn" role="alert"><Led status="warn" /><div className="ax-alert-body">{test.msg}</div></div>}

            <Select label="Satellite imagery" value={provider} options={PROVIDERS} onChange={(e) => setProvider(e.target.value)} />
            {providerWarn && (
              <div className="ax-alert is-warn" role="status"><Led status="warn" />
                <div className="ax-alert-body">Mapbox imagery needs a valid token. Until you add one, the map uses Esri.</div></div>
            )}

            <div className="ax-effect" aria-label="What the app will use">
              <Readout label="Imagery" value={nextImagery} tone={nextImagery === "Mapbox" ? "cobalt" : "ink"} size="sm" />
              <Readout label="Place search" value={nextSearch} tone={nextSearch === "Mapbox" ? "cobalt" : "ink"} size="sm" />
            </div>
            <p className="ax-hint">Now saved: imagery from <b>{s0.imagery}</b>, search with <b>{s0.geocoder}</b>.</p>
          </section>

          <section className="ax-section" aria-labelledby="set-crash">
            <div className="ax-section-head"><Icon name="info" size={16} /><h3 id="set-crash">Privacy</h3></div>
            <Toggle checked={crash} onChange={setCrash} tone="cobalt" label="Send anonymous crash reports"
              description="Stack traces only. No project data, no plot locations. Off by default." />
          </section>

          <section className="ax-section" aria-labelledby="set-theme">
            <div className="ax-section-head"><Icon name="sun" size={16} /><h3 id="set-theme">Theme</h3><span className="ax-hint">Applies now</span></div>
            <div className="ax-theme">
              <Tabs label="Colour theme" value={theme} onChange={onTheme}
                items={[{ id: "light", label: "Light" }, { id: "dark", label: "Dark" }, { id: "system", label: "System" }]} />
              <span className="ax-hint">Light is Anodized Silver, dark is Machined Graphite.</span>
            </div>
          </section>

          {saveErr && <div className="ax-alert" role="alert"><Led status="fail" /><div className="ax-alert-body"><b>Not saved.</b> {saveErr}</div></div>}
        </div>
      )}
    </Modal>
  );
}
