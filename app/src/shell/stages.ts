// The 8 hardware-sequencer stages and how they map onto the store's `Step`s.
// Stage 6 (Split) owns two steps: "units" (split the plate) and "program" (room lists).
import type { Step } from "../lib/store";
import type { Project } from "../lib/types";

export interface StageMeta {
  n: number;
  label: string;
  title: string;
  steps: Step[];
  /** Designed rules / co-pilot split for this stage (percent rules). Averages to ~92% across the 8 stages. */
  rules: number;
  /** What the deterministic engine does (verdigris). */
  engine: string;
  /** What Claude may do (amber), or null when the stage is 100% rules. */
  copilot: string | null;
}

export const STAGES: StageMeta[] = [
  { n: 1, label: "Find", title: "Find the plot", steps: ["find"], rules: 100,
    engine: "Place search (Mapbox or OpenStreetMap) flies the satellite map to the locality.", copilot: null },
  { n: 2, label: "Trace", title: "Trace the boundary", steps: ["trace"], rules: 100,
    engine: "Your clicks become a polygon in the local UTM grid: area, perimeter, edge lengths, validity.", copilot: null },
  { n: 3, label: "Climate", title: "Sun, wind and setback", steps: ["climate"], rules: 95,
    engine: "Sun paths (Jun / Equinox / Dec), wind rose, DCPR 2034 setback buffer and buildable area.",
    copilot: "T0 - reads the surroundings (sea view, creek) from the map. Unknown stays “–”." },
  { n: 4, label: "Place", title: "Place the towers", steps: ["place"], rules: 90,
    engine: "Tries every angle (1° steps) and plate size; scores coverage, gap, breeze, view and west glass.",
    copilot: "T5 - writes a one-line plain-English note on why an option wins." },
  { n: 5, label: "Shape", title: "Shape the floor", steps: ["shape"], rules: 100,
    engine: "Parametric plate templates; zones (flats, core, passage, shafts) always add up to the plate.", copilot: null },
  { n: 6, label: "Split", title: "Split into flats", steps: ["units", "program"], rules: 90,
    engine: "Divides the flats zone, links every flat to its core, applies BHK room-list presets.",
    copilot: "T8 - turns a brief in words into a room list, schema- and rule-checked before use." },
  { n: 7, label: "Wire", title: "Wire rooms and solve", steps: ["diagram"], rules: 80,
    engine: "Room graph pre-check, then packs rooms into the flat boundary.",
    copilot: "T3 / T7 - proposes how to relax a clash; you latch Keep or Undo." },
  { n: 8, label: "Check", title: "Check the rules", steps: ["review"], rules: 85,
    engine: "DCPR 2034 and NBC 2016 clauses on every room, door and window; DXF in and out.",
    copilot: "T1 / T2 / T4 / T6 - maps odd CAD layers, units and room names; explains a result in plain words." },
];

export const RULES_SHARE = Math.round(STAGES.reduce((a, s) => a + s.rules, 0) / STAGES.length);

export const stageOfStep = (step: Step): StageMeta => STAGES.find((s) => s.steps.includes(step)) ?? STAGES[0];

/** Why a stage cannot be opened yet (null = available). Mirrors the original `enabled()` gate in App. */
export function blockedReason(n: number, project: Project | null): string | null {
  switch (n) {
    case 1: case 2: return null;
    case 3: case 4: return project?.site ? null : "Trace a plot first.";
    case 5: return project?.plates.length ? null : "Choose a tower placement first.";
    case 6: case 7: return project?.units.length ? null : "Shape a plate and split it into flats first.";
    case 8: return project?.plans.length || project?.checks ? null : "Solve a flat or import a DXF first.";
    default: return null;
  }
}

/** UTM zone label for a WGS84 point, e.g. "UTM 43N · EPSG:32643". */
export function utmLabel(lng: number, lat: number): string {
  const zone = Math.floor((lng + 180) / 6) + 1;
  const north = lat >= 0;
  return `UTM ${zone}${north ? "N" : "S"} · EPSG:${(north ? 32600 : 32700) + zone}`;
}
