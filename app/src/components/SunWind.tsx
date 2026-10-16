import type { Climate } from "../lib/types";

const R = 90;
const polar = (az: number, alt: number) => {
  const r = R * (1 - alt / 90);
  const a = ((az - 90) * Math.PI) / 180;
  return [r * Math.cos(a), r * Math.sin(a)];
};
const COLORS: Record<string, string> = { "Jun 21": "#e8890c", "Mar/Sep 21": "#d9b400", "Dec 21": "#3a8fc0" };

export function SunPath({ climate }: { climate: Climate }) {
  return (
    <svg viewBox="-110 -110 220 220" width="100%" role="img" aria-label="Sun path diagram">
      {[0, 30, 60].map((alt) => <circle key={alt} r={R * (1 - alt / 90)} fill="none" stroke="currentColor" opacity={0.18} />)}
      {[0, 90, 180, 270].map((az) => { const [x, y] = polar(az, 0); return <line key={az} x1={0} y1={0} x2={x} y2={y} stroke="currentColor" opacity={0.15} />; })}
      {["N", "E", "S", "W"].map((t, i) => { const [x, y] = polar(i * 90, -8); return <text key={t} x={x} y={y + 4} fontSize={10} textAnchor="middle" fill="currentColor" opacity={0.7}>{t}</text>; })}
      <path d={`M ${polar(240, 0).join(" ")} A ${R} ${R} 0 0 1 ${polar(300, 0).join(" ")}`} stroke="#c6373a" strokeWidth={5} fill="none" opacity={0.5} />
      {climate.sun.map((p) => (
        <g key={p.label}>
          <polyline fill="none" stroke={COLORS[p.label] ?? "#888"} strokeWidth={2}
            points={p.samples.map((s) => polar(s.azimuth, s.altitude).join(",")).join(" ")} />
        </g>
      ))}
      <circle r={2} fill="currentColor" />
    </svg>
  );
}

export function WindRoseChart({ climate }: { climate: Climate }) {
  const max = Math.max(...climate.wind.bins.map((b) => b.frequency), 0.01);
  return (
    <svg viewBox="-110 -110 220 220" width="100%" role="img" aria-label="Wind rose">
      {[0.33, 0.66, 1].map((k) => <circle key={k} r={R * k} fill="none" stroke="currentColor" opacity={0.15} />)}
      {climate.wind.bins.map((b) => {
        const r = (R * b.frequency) / max;
        const a1 = ((b.direction - 90 - 9) * Math.PI) / 180, a2 = ((b.direction - 90 + 9) * Math.PI) / 180;
        return <path key={b.label} d={`M0 0 L ${r * Math.cos(a1)} ${r * Math.sin(a1)} A ${r} ${r} 0 0 1 ${r * Math.cos(a2)} ${r * Math.sin(a2)} Z`}
          fill={b.label === climate.wind.prevailing ? "#0f8b8d" : "#7fc4c5"} opacity={0.9}>
          <title>{`${b.label}: ${(b.frequency * 100).toFixed(1)}% · ${b.mean_speed} m/s`}</title>
        </path>;
      })}
      {["N", "E", "S", "W"].map((t, i) => { const a = ((i * 90 - 90) * Math.PI) / 180; return <text key={t} x={(R + 10) * Math.cos(a)} y={(R + 10) * Math.sin(a) + 4} fontSize={10} textAnchor="middle" fill="currentColor" opacity={0.7}>{t}</text>; })}
    </svg>
  );
}

export const SUN_COLORS = COLORS;
