import { useCallback, useEffect, useRef, useState, type KeyboardEvent } from "react";
import { rpc } from "../lib/api";
import { useStore } from "../lib/store";
import type { GeocodeResult } from "../lib/types";
import { EmptyState, Field, Icon, IconButton, Kbd, LatchButton, Panel, Readout, Skeleton, Spinner } from "../ui";
import "./stage14.css";

// ------------------------------------------------------------------------------------------- helpers
const RECENT_PLACES = "ivai.recentPlaces";
const RECENT_PROJECTS = "ivai.recentProjects";
const TRY = ["Lokhandwala Complex", "Bandra West", "Powai", "Worli", "Thane"];

function readList<T>(key: string): T[] {
  try {
    const v = JSON.parse(localStorage.getItem(key) ?? "[]");
    return Array.isArray(v) ? (v as T[]) : [];
  } catch { return []; }
}
function writeList<T>(key: string, list: T[]): void {
  try { localStorage.setItem(key, JSON.stringify(list)); } catch { /* storage may be blocked: the lists are a convenience only */ }
}
const sameSpot = (a: GeocodeResult, b: GeocodeResult) => a.lat === b.lat && a.lng === b.lng;
const baseName = (p: string) => p.split(/[\\/]/).pop() || p;

type Phase = "idle" | "loading" | "done" | "error";

// ------------------------------------------------------------------------------------------- 1 Find
export function FindPanel({ onFly, onStartTrace }: { onFly: (r: GeocodeResult) => void; onStartTrace: () => void }) {
  const [q, setQ] = useState("");
  const [res, setRes] = useState<GeocodeResult[]>([]);
  const [phase, setPhase] = useState<Phase>("idle");
  const [err, setErr] = useState("");
  const [asked, setAsked] = useState("");
  const [active, setActive] = useState<GeocodeResult | null>(null);
  const [recent, setRecent] = useState<GeocodeResult[]>(() => readList<GeocodeResult>(RECENT_PLACES));
  const site = useStore((s) => s.project?.site);
  const inputRef = useRef<HTMLInputElement>(null);
  const listRef = useRef<HTMLUListElement>(null);
  const seq = useRef(0);

  const remember = useCallback((r: GeocodeResult) => {
    setRecent((prev) => {
      const next = [r, ...prev.filter((x) => !sameSpot(x, r))].slice(0, 4);
      writeList(RECENT_PLACES, next);
      return next;
    });
  }, []);

  const fly = useCallback((r: GeocodeResult) => {
    setActive(r);
    remember(r);
    onFly(r);
  }, [onFly, remember]);

  const search = useCallback(async (text: string) => {
    const query = text.trim();
    if (!query) { inputRef.current?.focus(); return; }
    const mine = ++seq.current;
    setPhase("loading");
    setErr("");
    setAsked(query);
    try {
      const r = await rpc<GeocodeResult[]>("search_place", { query });
      if (mine !== seq.current) return;
      setRes(r);
      setPhase("done");
      if (r[0]) fly(r[0]); else setActive(null);
    } catch (e) {
      if (mine !== seq.current) return;
      setRes([]);
      setErr(e instanceof Error ? e.message : String(e));
      setPhase("error");
    }
  }, [fly]);

  // "/" jumps to the search box from anywhere in the page (ignored while typing or when a dialog is open)
  useEffect(() => {
    const onKey = (e: globalThis.KeyboardEvent) => {
      if (e.key !== "/" || e.ctrlKey || e.metaKey || e.altKey || e.defaultPrevented) return;
      const t = e.target as HTMLElement | null;
      if (t && (t.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName))) return;
      if (document.querySelector('[aria-modal="true"]')) return;
      e.preventDefault();
      inputRef.current?.focus();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  const onInputKey = (e: KeyboardEvent<HTMLInputElement>) => {
    if (e.key === "Enter") { e.preventDefault(); void search(q); }
    else if (e.key === "ArrowDown" && res.length) { e.preventDefault(); listRef.current?.querySelector<HTMLElement>("button")?.focus(); }
    else if (e.key === "Escape" && q) { setQ(""); }
  };
  const onListKey = (e: KeyboardEvent<HTMLUListElement>) => {
    if (e.key !== "ArrowDown" && e.key !== "ArrowUp") return;
    const items = Array.from(listRef.current?.querySelectorAll<HTMLElement>("button") ?? []);
    const i = items.indexOf(document.activeElement as HTMLElement);
    if (i < 0) return;
    e.preventDefault();
    if (e.key === "ArrowUp" && i === 0) inputRef.current?.focus();
    else items[Math.max(0, Math.min(items.length - 1, i + (e.key === "ArrowDown" ? 1 : -1)))]?.focus();
  };

  const loading = phase === "loading";
  const showRecent = recent.length > 0 && phase !== "loading";

  return (
    <div className="s14">
      <p className="s14-intro">Search a place. The globe flies there. Then trace the plot on the satellite map.</p>

      <div className="s14-search" role="search">
        <Field ref={inputRef} label="Place" type="search" placeholder="e.g. Lokhandwala" autoComplete="off" spellCheck={false}
          leading={<Icon name="search" size={15} />} value={q} onChange={(e) => setQ(e.target.value)} onKeyDown={onInputKey}
          aria-keyshortcuts="/" hint={<>Press <Kbd>/</Kbd> to jump here, <Kbd>Enter</Kbd> to search.</>} />
        <LatchButton variant="primary" loading={loading} disabled={!q.trim() && !loading} onClick={() => void search(q)}
          aria-label="Search place">Search</LatchButton>
      </div>

      <div aria-live="polite" className="sr-only">
        {phase === "loading" ? `Searching for ${asked}` : phase === "done" ? (res.length ? `${res.length} places found. Flying to ${res[0].name}.` : `No place found for ${asked}.`) : ""}
      </div>

      {phase === "error" && (
        <div className="s14-alert" role="alert">
          <Icon name="alert" size={16} />
          <div className="s14-alert-body">
            <div className="s14-alert-title">Search failed</div>
            {err}
            <div className="s14-alert-actions">
              <LatchButton size="sm" onClick={() => void search(asked)}>Try again</LatchButton>
            </div>
          </div>
        </div>
      )}

      {loading && (
        <div className="s14-skel-list" aria-hidden="true">
          <Skeleton height={64} radius={12} />
          <Skeleton height={64} radius={12} />
        </div>
      )}

      {phase === "done" && res.length === 0 && (
        <Panel profile="well" pad="none">
          <EmptyState icon={<Icon name="map" size={24} />} title="No place found">
            Nothing matches “{asked}”. Try the locality or a nearby landmark.
          </EmptyState>
        </Panel>
      )}

      {phase === "done" && res.length > 0 && (
        <>
          <h3 className="s14-sec-title">Results · {res.length}</h3>
          <Panel profile="well" pad="none" aria-label="Search results">
            <ul className="s14-results" ref={listRef} onKeyDown={onListKey}>
              {res.map((r) => {
                const on = !!active && sameSpot(active, r);
                return (
                  <li key={`${r.lat},${r.lng}`}>
                    <button type="button" className="s14-result" aria-pressed={on} onClick={() => fly(r)}>
                      <span className="s14-result-top">
                        <span className="s14-result-name">{r.name}</span>
                        {on && <span className="s14-flying"><Icon name="check" size={14} />Here</span>}
                      </span>
                      <span className="s14-result-sub">{r.display_name}</span>
                      <span className="s14-result-mono">{r.lat.toFixed(6)}°, {r.lng.toFixed(6)}° · {r.provider}</span>
                    </button>
                  </li>
                );
              })}
            </ul>
          </Panel>
        </>
      )}

      {active && (
        <Panel profile="well" pad="sm" small title="Camera target" aria-label="Camera target"
          actions={<IconButton size="sm" label={`Fly to ${active.name} again`} icon={<Icon name="map" />} onClick={() => onFly(active)} />}>
          <div className="s14-grid2">
            <Readout label="Latitude" value={active.lat} unit="°" digits={5} tone="det" size="sm" by="engine" kind="observed" source={`Geocoder: ${active.provider}`} />
            <Readout label="Longitude" value={active.lng} unit="°" digits={5} tone="det" size="sm" by="engine" kind="observed" source={`Geocoder: ${active.provider}`} />
          </div>
          <p className="s14-meta">{active.provider === "offline" ? "From the bundled place list (offline)." : `From the ${active.provider} geocoder.`} Check the pin against the imagery before you trace.</p>
        </Panel>
      )}

      {phase === "idle" && !active && (
        <div className="s14-chips-wrap">
          <h3 className="s14-sec-title">Try</h3>
          <div className="s14-chips" style={{ marginTop: 8 }}>
            {TRY.map((t) => (
              <button key={t} type="button" className="s14-chip-btn" onClick={() => { setQ(t); void search(t); }}>{t}</button>
            ))}
          </div>
        </div>
      )}

      {showRecent && (
        <div>
          <h3 className="s14-sec-title">Recent places</h3>
          <ul className="s14-recent" style={{ marginTop: 6 }}>
            {recent.map((r) => (
              <li key={`${r.lat},${r.lng}`}>
                <button type="button" className="s14-recent-open" onClick={() => { setQ(r.name); fly(r); }} title={r.display_name}>
                  <span className="s14-recent-name">{r.name}</span>
                  <span className="s14-recent-path">{r.display_name}</span>
                </button>
              </li>
            ))}
          </ul>
        </div>
      )}

      <LatchButton variant="primary" block size="lg" onClick={onStartTrace} icon={<Icon name="ruler" />}
        aria-label={site ? "Edit the plot boundary" : "Trace the boundary"}>
        {site ? "Edit the boundary" : "Trace the boundary"} <Icon name="chevron-right" />
      </LatchButton>
      <p className="s14-meta" style={{ marginTop: -6 }}>Press <Kbd>2</Kbd> to jump to Trace at any time.</p>

      <ProjectBox />
    </div>
  );
}

// ------------------------------------------------------------------------------------------- project file
interface RecentProject { path: string; name: string }

export function ProjectBox() {
  const run = useStore((s) => s.run);
  const toast = useStore((s) => s.toast);
  const summary = useStore((s) => s.summary);
  const projectName = useStore((s) => s.project?.name);
  const [path, setPath] = useState("");
  const [pending, setPending] = useState<"open" | "new" | null>(null);
  const [recent, setRecent] = useState<RecentProject[]>(() => readList<RecentProject>(RECENT_PROJECTS));
  const file = String(summary?.file ?? "");

  // the project the engine has open is always the first "recent" entry
  useEffect(() => {
    if (!file) return;
    setRecent((prev) => {
      if (prev[0]?.path === file) return prev;
      const next = [{ path: file, name: projectName || baseName(file) }, ...prev.filter((x) => x.path !== file)].slice(0, 5);
      writeList(RECENT_PROJECTS, next);
      return next;
    });
  }, [file, projectName]);

  const trimmed = path.trim();
  const bad = trimmed !== "" && !trimmed.toLowerCase().endsWith(".aptplan");

  const go = async (kind: "open" | "new", p: string) => {
    if (!p) return;
    setPending(kind);
    const name = baseName(p).replace(/\.aptplan$/i, "");
    const out = kind === "open"
      ? await run("Opening", () => rpc("open_project", { path: p }))
      : await run("Creating", () => rpc("create_project", { path: p, name }));
    setPending(null);
    if (out !== undefined) {
      toast("ok", kind === "open" ? `Opened ${name}.` : `Created ${name}.`);
      setPath("");
    }
  };
  const forget = (p: string) => setRecent((prev) => { const next = prev.filter((x) => x.path !== p); writeList(RECENT_PROJECTS, next); return next; });

  return (
    <Panel profile="flat" small pad="sm" title="Project file" actions={projectName ? <span className="s14-meta" title="Open project">{projectName}</span> : undefined}>
      <p className={`s14-proj-file neo-well neo-sm${file ? "" : " is-empty"}`} aria-label="Current project file">{file || "No file yet. Create one below."}</p>
      <Field label="File path" placeholder="/path/to/project.aptplan" value={path} autoComplete="off" spellCheck={false}
        onChange={(e) => setPath(e.target.value)} onKeyDown={(e) => { if (e.key === "Enter" && trimmed && !bad) void go("open", trimmed); }}
        error={bad ? "The file should end in .aptplan." : undefined} hint={bad ? undefined : "Full path to a .aptplan file."} />
      <div className="s14-actions">
        <LatchButton size="sm" disabled={!trimmed || bad || pending !== null} loading={pending === "open"} onClick={() => void go("open", trimmed)}>Open</LatchButton>
        <LatchButton size="sm" disabled={!trimmed || bad || pending !== null} loading={pending === "new"} onClick={() => void go("new", trimmed)}>New</LatchButton>
      </div>
      {recent.length > 0 && (
        <>
          <h4 className="s14-sec-title">Recent projects</h4>
          <ul className="s14-recent">
            {recent.map((r) => (
              <li key={r.path}>
                <button type="button" className="s14-recent-open" disabled={pending !== null || r.path === file} title={r.path}
                  aria-label={r.path === file ? `${r.name}, open now` : `Open ${r.name}`} onClick={() => void go("open", r.path)}>
                  <span className="s14-recent-name">{r.name}{r.path === file ? " · open now" : ""}</span>
                  <span className="s14-recent-path">{r.path}</span>
                </button>
                {r.path !== file && <IconButton size="sm" label={`Forget ${r.name}`} icon={<Icon name="close" size={13} />} onClick={() => forget(r.path)} />}
              </li>
            ))}
          </ul>
        </>
      )}
      {pending && <span className="s14-flying"><Spinner /> Working…</span>}
    </Panel>
  );
}
