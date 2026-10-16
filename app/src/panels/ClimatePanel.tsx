import { useMemo, useState } from "react";
import { rpc } from "../lib/api";
import { useStore } from "../lib/store";
import type { Climate, Ticket } from "../lib/types";
import { HOT_RANGE, SetbackSketch, SunPath, styleOf, WindRoseChart } from "../components/SunWind";
import { Channel, Chip, EmptyState, Icon, LatchButton, Panel, ProvenanceTag, Readout, Skeleton, Tabs } from "../ui";
import "./stage14.css";

const toMin = (t: string) => { const [h, m] = t.split(":").map(Number); return h * 60 + (m || 0); };

// ------------------------------------------------------------------------------------------- 3 Climate
export function ClimatePanel() {
  const site = useStore((s) => s.project?.site);
  const run = useStore((s) => s.run);
  const setStep = useStore((s) => s.setStep);
  const busy = useStore((s) => s.busy);
  const [placing, setPlacing] = useState(false);

  if (!site) {
    return (
      <EmptyState icon={<Icon name="sun" size={26} />} title="No plot yet"
        action={<LatchButton variant="primary" onClick={() => setStep("trace")}>Trace the plot</LatchButton>}>
        Sun, wind and setbacks need the plot boundary first.
      </EmptyState>
    );
  }
  if (!site.climate) {
    return (
      <EmptyState icon={<Icon name="sun" size={26} />} title="No climate data for this plot"
        action={<LatchButton variant="det" loading={busy === "Analysing site"} onClick={() => void run("Analysing site", () => rpc("set_site_params", {}))}>Analyse the site again</LatchButton>}>
        The engine did not return a sun path or wind rose. Running the site analysis again usually fixes it.
      </EmptyState>
    );
  }
  const c = site.climate;
  const shareBuildable = site.area > 0 ? (site.buildable_area / site.area) * 100 : null;
  return (
    <div className="s14">
      <SunSection climate={c} />
      <WindSection climate={c} />

      <Panel profile="flat" small pad="sm" title="Setback and buildable area" actions={<ProvenanceTag by="engine" kind="derived" compact source={site.setback_clause} />}>
        <div className="neo-well neo-sm" style={{ padding: 8 }}>
          <SetbackSketch site={site} />
        </div>
        <ul className="s14-legend" aria-label="Sketch legend">
          <li><svg className="sw-legend-swatch" width="16" height="10" aria-hidden="true"><rect width="16" height="10" rx="2" fill="none" stroke="var(--ink-3)" strokeDasharray="3 2" /></svg>Setback band</li>
          <li><svg className="sw-legend-swatch" width="16" height="10" aria-hidden="true"><rect width="16" height="10" rx="2" fill="var(--det)" fillOpacity="0.3" stroke="var(--det-ink)" /></svg>Buildable</li>
        </ul>
        <div className="s14-grid3">
          <Readout label="Site" value={site.area} unit="m²" tone="det" size="sm" digits={0} />
          <Readout label="Setback" value={site.setback} unit="m" tone="det" size="sm" />
          <Readout label="Buildable" value={site.buildable_area} unit="m²" tone="det" size="sm" digits={0} />
        </div>
        <div className="s14-grid2">
          <Readout label="Buildable share" value={shareBuildable} unit="%" tone="det" size="sm" digits={0} />
          <Readout label="Road · height" value={`${site.road_width} · ${site.building_height}`} unit="m" tone="ink" size="sm" />
        </div>
        <p className="s14-note"><b>Rule:</b> {site.setback_clause}</p>
      </Panel>

      <Surroundings climate={c} />

      <div>
        <LatchButton variant="primary" size="lg" block loading={placing || busy === "Placing towers"}
          icon={<Icon name="cube" />}
          onClick={async () => {
            setPlacing(true);
            const ok = await run("Placing towers", () => rpc("place_towers"));
            setPlacing(false);
            if (ok) setStep("place");
          }}>
          Place the towers <Icon name="chevron-right" />
        </LatchButton>
        <p className="s14-why" style={{ marginTop: 8 }}>The rules try every angle and tower count that fits. It takes a few seconds.</p>
      </div>
    </div>
  );
}

// ------------------------------------------------------------------------------------------- sun
function SunSection({ climate: c }: { climate: Climate }) {
  const [season, setSeason] = useState(() => c.sun.find((p) => p.label === "Jun 21")?.label ?? c.sun[0]?.label ?? "");
  const [minutes, setMinutes] = useState(15 * 60);
  const path = c.sun.find((p) => p.label === season) ?? c.sun[0];
  const idx = useMemo(() => {
    if (!path || !path.samples.length) return 0;
    let best = 0;
    path.samples.forEach((s, i) => { if (Math.abs(toMin(s.time) - minutes) < Math.abs(toMin(path.samples[best].time) - minutes)) best = i; });
    return best;
  }, [path, minutes]);
  const sample = path?.samples[idx];

  if (!path || !sample) {
    return (
      <Panel profile="flat" small pad="sm" title="Sun path">
        <EmptyState icon={<Icon name="sun" size={22} />} title="No sun samples">The engine returned no sun positions for this plot.</EmptyState>
      </Panel>
    );
  }
  const hot = c.hot_facades;
  const inHot = sample.azimuth >= HOT_RANGE[0] && sample.azimuth <= HOT_RANGE[1] && sample.altitude > 0;
  return (
    <Panel profile="flat" small pad="sm" title="Sun path" actions={<ProvenanceTag by="engine" kind="derived" compact source="Solar position for this latitude and longitude, on three key dates" />}>
      <Tabs label="Season" size="sm" block value={path.label} onChange={(id) => setSeason(id)}
        items={c.sun.map((p) => ({ id: p.label, label: styleOf(p.label).short }))} />
      <SunPath climate={c} season={path.label} index={idx} />
      <Channel label="Time of day" hint="Move the sun along its arc" value={idx} min={0} max={Math.max(0, path.samples.length - 1)} step={1}
        format={(v) => path.samples[Math.round(v)]?.time ?? "–"}
        onChange={(v) => { const s = path.samples[Math.round(v)]; if (s) setMinutes(toMin(s.time)); }} />
      <div className="s14-grid3">
        <Readout label="Azimuth" value={sample.azimuth} unit="°" tone="det" size="sm" />
        <Readout label="Altitude" value={sample.altitude} unit="°" tone="det" size="sm" />
        <Readout label="Noon alt." value={path.noon_altitude} unit="°" tone="det" size="sm" />
      </div>
      <p className="s14-meta" role="status" aria-live="polite">
        {sample.time}: the sun is {inHot ? "in the hot west sector." : "outside the hot west sector."}
      </p>

      <ul className="s14-legend" aria-label="Sun path legend">
        {c.sun.map((p) => {
          const st = styleOf(p.label);
          return (
            <li key={p.label}>
              <svg width="26" height="8" aria-hidden="true"><line x1="1" y1="4" x2="25" y2="4" stroke={st.color} strokeWidth="2.4" strokeLinecap="round" strokeDasharray={st.dash} /></svg>
              {p.label} · noon {p.noon_altitude}°
            </li>
          );
        })}
        <li>
          <svg width="16" height="10" aria-hidden="true"><rect width="16" height="10" rx="2" fill="none" stroke="var(--fail)" strokeDasharray="3 2" /></svg>
          Hot west {HOT_RANGE[0]}° to {HOT_RANGE[1]}°
        </li>
      </ul>

      <div className="s14-hot-list">
        {hot.length > 0 ? (
          <>
            <Chip led="fail" title="Facades that get low afternoon sun">Hot west sun</Chip>
            {hot.map((h) => <Chip key={h} inset>{h}</Chip>)}
          </>
        ) : <Chip led="det">No hot west faces</Chip>}
        <ProvenanceTag by="engine" kind="derived" compact source="Sun between 5° and 50° altitude on a west-facing facade" />
      </div>
      <p className="s14-meta">Glass facing {HOT_RANGE[0]}° to {HOT_RANGE[1]}° takes the afternoon sun. The placement score keeps it small.</p>

      <details className="s14-details">
        <summary>Show sun data</summary>
        <table className="s14-table">
          <caption className="sr-only">Sun position by hour for {path.label}</caption>
          <thead><tr><th scope="col">Time</th><th scope="col">Azimuth °</th><th scope="col">Altitude °</th></tr></thead>
          <tbody>
            {path.samples.map((s) => <tr key={s.time}><td>{s.time}</td><td>{s.azimuth.toFixed(1)}</td><td>{s.altitude.toFixed(1)}</td></tr>)}
          </tbody>
        </table>
      </details>
    </Panel>
  );
}

// ------------------------------------------------------------------------------------------- wind
function WindSection({ climate: c }: { climate: Climate }) {
  const w = c.wind;
  const prev = w.bins.find((b) => b.label === w.prevailing);
  return (
    <Panel profile="flat" small pad="sm" title="Wind"
      actions={w.indicative
        ? <ProvenanceTag by="engine" kind="inferred" compact source="Bundled regional climatology, not measured at the plot" />
        : <ProvenanceTag by="engine" kind="observed" compact source={w.source} />}>
      <WindRoseChart climate={c} />
      <div className="s14-grid3">
        <Readout label="Prevailing" value={w.prevailing} tone="det" size="sm" />
        <Readout label="Share" value={prev ? prev.frequency * 100 : null} unit="%" tone="det" size="sm" digits={1} />
        <Readout label="Mean speed" value={prev?.mean_speed} unit="m/s" tone="det" size="sm" />
      </div>
      <p className="s14-meta">{w.source}{w.indicative ? ". Indicative: regional data, not measured at the plot." : "."}</p>
      {w.notes.map((n) => <p key={n} className="s14-meta">{n}</p>)}
      <details className="s14-details">
        <summary>Show wind data</summary>
        <table className="s14-table">
          <caption className="sr-only">Wind frequency by direction</caption>
          <thead><tr><th scope="col">From</th><th scope="col">Share %</th><th scope="col">Speed m/s</th></tr></thead>
          <tbody>
            {w.bins.map((b) => <tr key={b.label}><td>{b.label}</td><td>{(b.frequency * 100).toFixed(1)}</td><td>{b.mean_speed}</td></tr>)}
          </tbody>
        </table>
      </details>
    </Panel>
  );
}

// ------------------------------------------------------------------------------------------- T0 questions
const SUBJECT_TITLE: Record<string, string> = { sea_view: "Sea view", creek: "Creek" };

function Surroundings({ climate: c }: { climate: Climate }) {
  const tickets = useStore((s) => s.tickets);
  const subjects = ["sea_view", "creek"] as const;
  return (
    <Panel profile="flat" small pad="sm" title="Surroundings" actions={<ProvenanceTag by="claude" compact source="Claude may answer from the map labels around the plot. The rules re-check it." />}>
      <p className="s14-meta">The rules cannot see the sea. They leave these unknown until someone answers.</p>
      <div className="neo-well neo-sm">
        {subjects.map((sub) => (
          <T0Question key={sub} subject={sub} value={c[sub]} ticket={tickets.find((t) => t.id === `T0:${sub}`)} />
        ))}
      </div>
    </Panel>
  );
}

function T0Question({ subject, value, ticket: t }: { subject: "sea_view" | "creek"; value: string; ticket?: Ticket }) {
  const run = useStore((s) => s.run);
  const bridge = useStore((s) => s.bridge);
  const title = SUBJECT_TITLE[subject];
  const by = t?.answer?.by;
  const answered = !!t && !!t.answer && (t.status === "answered" || t.status === "kept");
  const unknown = value === "unknown" || !value;
  const actor: "engine" | "claude" | "user" = answered ? (by === "claude" ? "claude" : "user") : "engine";
  const kind = unknown ? "unknown" : actor === "claude" ? "inferred" : actor === "user" ? "observed" : "unknown";
  const options = t?.options ?? [{ id: "yes", label: "Yes", payload: {}, valid: true }, { id: "no", label: "No", payload: {}, valid: true }, { id: "unknown", label: "Unknown (–)", payload: {}, valid: true }];
  const pick = (id: string) => {
    if (!t || id === value) return;
    void run("Applying answer", () => rpc("answer_decision", { ticket_id: t.id, option_id: id, reason: "picked in the app", by: "user" }));
  };
  const review = (keep: boolean) => { if (t) void run(keep ? "Keeping" : "Undoing", () => rpc("review_decision", { ticket_id: t.id, keep })); };
  return (
    <div className="s14-q" role="group" aria-label={`${title} question`}>
      <div className="s14-q-head">
        <h4 className="s14-q-title">{title}</h4>
        <ProvenanceTag by={actor} kind={kind} compact source={t?.evidence?.join(" · ")} />
      </div>
      <p className="s14-q-text">{t?.question ?? `Is there ${subject === "sea_view" ? "a sea view" : "a creek"} near this plot?`}</p>
      <div className="s14-trio" role="group" aria-label={`${title} answer`}>
        {options.slice(0, 3).map((o) => (
          <LatchButton key={o.id} size="sm" latched={value === o.id} led={value === o.id ? (actor === "claude" ? "ai" : actor === "user" ? "cobalt" : "idle") : "idle"}
            disabled={!t || !o.valid} onClick={() => pick(o.id)}>
            {o.id === "unknown" ? "Unknown" : o.label}
          </LatchButton>
        ))}
      </div>
      {!t ? (
        <div className="s14-q-answer"><Skeleton height={12} /> <span className="sr-only">Loading the question</span></div>
      ) : answered ? (
        <div className="s14-q-answer">
          {actor === "claude"
            ? <><span className="backlit">Claude says {value === "unknown" ? "unknown" : value}</span>{t.answer?.reason ? ` · “${t.answer.reason}”` : ""}. Check it against the map.</>
            : <>You answered <b>{value}</b>.</>}
        </div>
      ) : (
        <div className="s14-q-answer">
          Not answered yet. {bridge.linked ? "Claude can answer from the map labels." : "Pick one, or link Claude to answer from the map labels."}
        </div>
      )}
      {t && t.status === "answered" && actor === "claude" && (
        <div className="s14-driver-actions">
          <LatchButton size="sm" variant="ai" onClick={() => review(true)}>Keep</LatchButton>
          <LatchButton size="sm" onClick={() => review(false)}>Undo</LatchButton>
        </div>
      )}
    </div>
  );
}
