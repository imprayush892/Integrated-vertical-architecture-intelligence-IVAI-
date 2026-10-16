import { useId } from "react";
import { boundsOf, fitView, flat } from "../lib/geom";
import type { Climate, Site } from "../lib/types";
import "./sunwind.css";

// Geometry shared by the two dials. 0 deg = north, clockwise. Altitude 0 sits on the horizon ring.
const R = 82;
const polar = (az: number, alt: number): [number, number] => {
  const r = R * (1 - alt / 90);
  const a = ((az - 90) * Math.PI) / 180;
  return [r * Math.cos(a), r * Math.sin(a)];
};
const ring = (az: number, r: number): [number, number] => {
  const a = ((az - 90) * Math.PI) / 180;
  return [r * Math.cos(a), r * Math.sin(a)];
};
const f = (n: number) => n.toFixed(2);

/** Chart colour + dash per season. The dash keeps the three arcs apart without colour. */
export const SUN_STYLE: Record<string, { color: string; dash?: string; short: string }> = {
  "Jun 21": { color: "var(--sw-jun)", short: "Jun 21" },
  "Mar/Sep 21": { color: "var(--sw-eq)", dash: "7 4", short: "Equinox" },
  "Dec 21": { color: "var(--sw-dec)", dash: "1.5 4.5", short: "Dec 21" },
};
const FALLBACK = { color: "var(--ink-3)", short: "" };
export const styleOf = (label: string) => SUN_STYLE[label] ?? { ...FALLBACK, short: label };

/** Kept for older callers: label -> CSS colour. */
export const SUN_COLORS: Record<string, string> = Object.fromEntries(Object.entries(SUN_STYLE).map(([k, v]) => [k, v.color]));

// 16-point compass names -> bearing, for marking the hot facades on the rim
const COMPASS: Record<string, number> = {
  N: 0, NNE: 22.5, NE: 45, ENE: 67.5, E: 90, ESE: 112.5, SE: 135, SSE: 157.5,
  S: 180, SSW: 202.5, SW: 225, WSW: 247.5, W: 270, WNW: 292.5, NW: 315, NNW: 337.5,
};
/** Facades whose normal falls in this range count as "west glass" in the placement score (engine HOT_RANGE). */
export const HOT_RANGE: [number, number] = [225, 315];

export interface SunPathProps {
  climate: Climate;
  /** Season label whose sun marker is shown (e.g. "Jun 21"). The other arcs dim. */
  season?: string;
  /** Sample index on that season's arc for the sun marker and needle. */
  index?: number;
}

function wedge(a1: number, a2: number, r: number): string {
  const [x1, y1] = ring(a1, r), [x2, y2] = ring(a2, r);
  return `M0 0 L${f(x1)} ${f(y1)} A${r} ${r} 0 0 1 ${f(x2)} ${f(y2)} Z`;
}

/** Azimuth dial (kit logo D): vernier bezel, altitude rings, three season arcs, a needle on the chosen hour. */
export function SunPath({ climate, season, index }: SunPathProps) {
  const uid = useId().replace(/[^a-zA-Z0-9]/g, "");
  const hatch = `sw-hatch-${uid}`;
  const sel = climate.sun.find((p) => p.label === season);
  const sample = sel && index != null ? sel.samples[Math.max(0, Math.min(sel.samples.length - 1, index))] : undefined;
  const sunXY = sample ? polar(sample.azimuth, sample.altitude) : null;
  const summary = climate.sun.map((p) => `${p.label} noon altitude ${p.noon_altitude} degrees`).join("; ");
  const hot = climate.hot_facades.map((h) => COMPASS[h]).filter((b): b is number => b != null);
  return (
    <div className="sw-dial neo-dish">
      <svg className="sw-svg" viewBox="-122 -122 244 244" role="img"
        aria-label={`Sun path dial, north at the top. ${summary}.${climate.hot_facades.length ? ` Hot west sun on the ${climate.hot_facades.join(", ")} facades.` : ""}`}>
        <defs>
          <pattern id={hatch} width="5" height="5" patternUnits="userSpaceOnUse" patternTransform="rotate(45)">
            <line x1="0" y1="0" x2="0" y2="5" stroke="var(--fail)" strokeWidth="1.4" strokeOpacity="0.55" />
          </pattern>
        </defs>
        <circle className="sw-face" r={R + 12} />
        {/* vernier ticks: 5 / 10 / 30 degree */}
        {Array.from({ length: 72 }, (_, i) => {
          const az = i * 5;
          const major = az % 30 === 0, mid = az % 10 === 0;
          const len = major ? 8 : mid ? 5.5 : 3;
          const [x1, y1] = ring(az, R + 11 - len), [x2, y2] = ring(az, R + 11);
          return <line key={az} className={`sw-tick${major ? " major" : ""}`} strokeWidth={major ? 1.6 : 1} x1={f(x1)} y1={f(y1)} x2={f(x2)} y2={f(y2)} />;
        })}
        {/* hot west sector, hatched */}
        <path d={wedge(HOT_RANGE[0], HOT_RANGE[1], R)} fill={`url(#${hatch})`} />
        <path className="sw-hot" strokeOpacity="0.6" strokeWidth="1.2"
          d={`M${f(ring(HOT_RANGE[0], R)[0])} ${f(ring(HOT_RANGE[0], R)[1])} A${R} ${R} 0 0 1 ${f(ring(HOT_RANGE[1], R)[0])} ${f(ring(HOT_RANGE[1], R)[1])}`} />
        {/* altitude rings */}
        {[0, 30, 60].map((alt) => <circle key={alt} className={alt === 0 ? "sw-ring solid" : "sw-ring"} r={R * (1 - alt / 90)} />)}
        {[0, 90, 180, 270].map((az) => { const [x, y] = ring(az, R); return <line key={az} className="sw-spoke" x1={0} y1={0} x2={f(x)} y2={f(y)} />; })}
        <text className="sw-alt" x={3} y={-R * (1 - 30 / 90) + 8} textAnchor="start">30°</text>
        <text className="sw-alt" x={3} y={-R * (1 - 60 / 90) + 8} textAnchor="start">60°</text>
        {/* hot facade marks on the rim */}
        {hot.map((b) => {
          const [x1, y1] = ring(b - 5, R + 1), [x2, y2] = ring(b + 5, R + 1);
          return <path key={b} className="sw-hot-mark" fill="none" d={`M${f(x1)} ${f(y1)} A${R + 1} ${R + 1} 0 0 1 ${f(x2)} ${f(y2)}`} />;
        })}
        {/* labels */}
        {[30, 60, 120, 150, 210, 240, 300, 330].map((az) => { const [x, y] = ring(az, R + 19); return <text key={az} className="sw-num" x={f(x)} y={f(y + 2.5)} textAnchor="middle">{az}</text>; })}
        {(["N", "E", "S", "W"] as const).map((t, i) => { const [x, y] = ring(i * 90, R + 20); return <text key={t} className="sw-card" x={f(x)} y={f(y + 4)} textAnchor="middle">{t}</text>; })}
        {/* season arcs */}
        {climate.sun.map((p) => {
          const st = styleOf(p.label);
          const on = !season || p.label === season;
          const pts = p.samples.map((s) => polar(s.azimuth, s.altitude));
          const top = p.samples.reduce((b, s, i) => (s.altitude > p.samples[b].altitude ? i : b), 0);
          return (
            <g key={p.label}>
              <polyline className={`sw-arc${on ? "" : " is-dim"}${season && on ? " is-on" : ""}`} stroke={st.color} strokeDasharray={st.dash}
                points={pts.map(([x, y]) => `${f(x)},${f(y)}`).join(" ")} />
              {pts.length > 0 && <circle className="sw-end" r={2.6} fill={st.color} cx={f(pts[0][0])} cy={f(pts[0][1])} opacity={on ? 1 : 0.5} />}
              {pts.length > 1 && <circle className="sw-end" r={2.6} fill={st.color} cx={f(pts[pts.length - 1][0])} cy={f(pts[pts.length - 1][1])} opacity={on ? 1 : 0.5} />}
              {pts[top] && <rect className="sw-end" x={pts[top][0] - 2.6} y={pts[top][1] - 2.6} width={5.2} height={5.2} transform={`rotate(45 ${f(pts[top][0])} ${f(pts[top][1])})`} fill={st.color} opacity={on ? 1 : 0.5} />}
            </g>
          );
        })}
        {/* needle + sun */}
        {sunXY && sel && (
          <g>
            <line className="sw-needle" x1={0} y1={0} x2={f(sunXY[0])} y2={f(sunXY[1])} />
            <circle r={7} cx={f(sunXY[0])} cy={f(sunXY[1])} fill={styleOf(sel.label).color} />
            <circle className="sw-sun-ring" r={7} cx={f(sunXY[0])} cy={f(sunXY[1])} />
            <circle r={2.2} cx={f(sunXY[0])} cy={f(sunXY[1])} fill="var(--neo-bg)" />
          </g>
        )}
        <circle className="sw-hub" r={4.5} />
      </svg>
    </div>
  );
}

/** Wind rose: petal length is how often the wind blows from that direction. The longest petal is solid. */
export function WindRoseChart({ climate }: { climate: Climate }) {
  const bins = climate.wind.bins;
  const max = Math.max(...bins.map((b) => b.frequency), 0.01);
  const prev = bins.find((b) => b.label === climate.wind.prevailing);
  const rings = [0.25, 0.5, 0.75, 1];
  return (
    <div className="sw-dial is-rose neo-dish">
      <svg className="sw-svg" viewBox="-122 -122 244 244" role="img"
        aria-label={`Wind rose, north at the top. Prevailing wind from the ${climate.wind.prevailing}${prev ? `, ${(prev.frequency * 100).toFixed(1)} percent of the time` : ""}.`}>
        <circle className="sw-face" r={R + 12} />
        {Array.from({ length: 36 }, (_, i) => {
          const az = i * 10;
          const major = az % 90 === 0;
          const [x1, y1] = ring(az, R + 11 - (major ? 7 : 3.5)), [x2, y2] = ring(az, R + 11);
          return <line key={az} className={`sw-tick${major ? " major" : ""}`} strokeWidth={major ? 1.6 : 1} x1={f(x1)} y1={f(y1)} x2={f(x2)} y2={f(y2)} />;
        })}
        {rings.map((k) => <circle key={k} className={k === 1 ? "sw-ring solid" : "sw-ring"} r={R * k} />)}
        {[0, 90, 180, 270].map((az) => { const [x, y] = ring(az, R); return <line key={az} className="sw-spoke" x1={0} y1={0} x2={f(x)} y2={f(y)} />; })}
        {[0.5, 1].map((k) => (
          <text key={k} className="sw-alt" x={f(Math.cos(Math.PI / 4) * R * k + 2)} y={f(Math.sin(Math.PI / 4) * R * k + 7)}>{Math.round(max * k * 100)}%</text>
        ))}
        {bins.map((b) => {
          const r = (R * b.frequency) / max;
          const [x1, y1] = ring(b.direction - 9, r), [x2, y2] = ring(b.direction + 9, r);
          const main = b.label === climate.wind.prevailing;
          return (
            <path key={b.label} className={`sw-petal${main ? " is-main" : ""}`} d={`M0 0 L${f(x1)} ${f(y1)} A${f(r)} ${f(r)} 0 0 1 ${f(x2)} ${f(y2)} Z`}>
              <title>{`${b.label}: ${(b.frequency * 100).toFixed(1)}% · ${b.mean_speed} m/s`}</title>
            </path>
          );
        })}
        {prev && (() => {
          const [tx, ty] = ring(prev.direction, R + 2);
          const [lx, ly] = ring(prev.direction - 4.5, R + 12), [rx, ry] = ring(prev.direction + 4.5, R + 12);
          return <path className="sw-from" d={`M${f(tx)} ${f(ty)} L${f(lx)} ${f(ly)} L${f(rx)} ${f(ry)} Z`} />;
        })()}
        {(["N", "E", "S", "W"] as const).map((t, i) => { const [x, y] = ring(i * 90, R + 24); return <text key={t} className="sw-card" x={f(x)} y={f(y + 4)} textAnchor="middle">{t}</text>; })}
        <circle className="sw-hub" r={3.5} style={{ fill: "var(--ink-1)" }} />
      </svg>
    </div>
  );
}

/** Plan sketch of the plot: setback band (hatched) and the buildable area that is left. Local metres, north up. */
export function SetbackSketch({ site }: { site: Site }) {
  const uid = useId().replace(/[^a-zA-Z0-9]/g, "");
  const hatch = `sw-band-${uid}`;
  const W = 240, H = 170;
  const rings = [site.polygon, ...site.buildable];
  const view = fitView(boundsOf(rings), W, H, 22);
  const d = (r: Site["polygon"]) => { const pts = flat(view, r); let s = ""; for (let i = 0; i < pts.length; i += 2) s += `${i === 0 ? "M" : "L"}${f(pts[i])} ${f(pts[i + 1])} `; return `${s}Z`; };
  return (
    <svg className="sw-sketch" viewBox={`0 0 ${W} ${H}`} role="img"
      aria-label={`Plot sketch. Site ${site.area.toFixed(0)} square metres, buildable ${site.buildable_area.toFixed(0)} square metres after a ${site.setback} metre setback.`}>
      <defs>
        <pattern id={hatch} width="5" height="5" patternUnits="userSpaceOnUse" patternTransform="rotate(-45)">
          <line x1="0" y1="0" x2="0" y2="5" stroke="var(--ink-3)" strokeWidth="1.2" strokeOpacity="0.7" />
        </pattern>
      </defs>
      <path className="sw-band" fill={`url(#${hatch})`} fillRule="evenodd" d={`${d(site.polygon)} ${site.buildable.map(d).join(" ")}`} />
      {site.buildable.map((b, i) => <path key={i} className="sw-build" d={d(b)} />)}
      <path className="sw-site" d={d(site.polygon)} />
      {/* north arrow */}
      <g transform={`translate(${W - 16} 18)`}>
        <path d="M0 -9 L4 4 L0 1.5 L-4 4 Z" fill="var(--ink-1)" />
        <text x={0} y={16} textAnchor="middle" style={{ fontWeight: 800 }}>N</text>
      </g>
    </svg>
  );
}
