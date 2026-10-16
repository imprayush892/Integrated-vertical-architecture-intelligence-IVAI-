// Mirrors engine/planner_engine/models.py (the frozen data contracts).
export type Pt = [number, number];
export type Ring = Pt[];
export type PolyRings = Ring[];

export interface SunSample { time: string; azimuth: number; altitude: number }
export interface SunPath { label: string; date: string; noon_altitude: number; samples: SunSample[] }
export interface WindBin { direction: number; label: string; frequency: number; mean_speed: number }
export interface WindRose { source: string; indicative: boolean; bins: WindBin[]; prevailing: string; notes: string[] }
export interface Climate { sun: SunPath[]; wind: WindRose; hot_facades: string[]; sea_view: string; creek: string }
export interface Site {
  name: string; locality: string; lnglat: Ring; polygon: Ring; area: number; geodesic_area: number;
  perimeter: number; edges: number[]; road_width: number; building_height: number; road_edges: number[];
  setback: number; setback_clause: string; buildable: Ring[]; buildable_area: number; climate: Climate | null;
}
export interface PlateFootprint { id: string; polygon: Ring; angle_deg: number; center: Pt; width: number; depth: number; area: number }
export interface PlacementOption {
  id: "A" | "B" | "C"; towers: number; label: string; plates: PlateFootprint[]; coverage: number; total_area: number;
  min_gap: number | null; score: number; score_terms: Record<string, number>; drivers: string;
  passes: Record<string, boolean>;
}
export interface Placement { options: PlacementOption[]; chosen: string | null; elapsed_s: number }
export interface Zone { kind: "flats" | "core" | "passage" | "shaft"; polygon: PolyRings[]; area: number }
export interface CorePart { kind: string; polygon: Ring; width: number }
export interface Plate {
  tower_id: string; shape: string; params: Record<string, number>; angle_deg: number; origin: Pt; outline: Ring;
  corners: number; width: number; depth: number; area: number; zones: Zone[]; core_parts: CorePart[];
  passage_width: number; ratings: Record<string, number>; imported_from: string | null;
}
export interface ProgrammeRoom { id: string; type: string; name: string; target_area: number; min_area: number; needs_window: boolean; optional: boolean }
export interface Programme { id: string; name: string; bhk: number; area_range: [number, number]; rooms: ProgrammeRoom[]; links: [string, string][]; entry: string; note: string }
export interface Unit { id: string; polygon: Ring; area: number; core_link: Pt | null; flags: string[]; programme: Programme | null }
export interface DiagramRoom { id: string; type: string; name: string; target_area: number; needs_window: boolean; optional: boolean; placed: boolean; x: number; y: number }
export interface Conflict { id: string; rooms: string[]; reason: string; kind: string; fixes: { label: string; valid?: boolean }[] }
export interface Diagram { unit_id: string; rooms: DiagramRoom[]; links: [string, string][]; entry: string; conflicts: Conflict[]; feasible: boolean | null; message: string }
export interface PlanRoom { id: string; type: string; name: string; polygon: Ring; area: number; width: number; has_window: boolean; window_area: number; ventilated: boolean; unit_id: string | null }
export interface Opening { id: string; kind: "door" | "window" | "entry"; a: Pt; b: Pt; width: number; rooms: string[]; hinge: Pt | null; swing_into: string | null }
export interface Wall { id: string; polygon: PolyRings; thickness: number; kind: string }
export interface Plan { unit_id: string; rooms: PlanRoom[]; walls: Wall[]; doors: Opening[]; windows: Opening[]; carpet_area: number; status: string; solve_time_s: number }
export interface CheckResult {
  rule_id: string; clause: string; source: string; title: string; target: string; result: "pass" | "fail" | "review" | "n/a";
  value: number | string | null; threshold: number | string | null; unit: string; message: string; proof: string[];
  bbox: [number, number, number, number] | null;
}
export interface CheckSummary { results: CheckResult[]; counts: Record<string, number>; by_code: Record<string, Record<string, number>>; disclaimer: string }
export interface LayerMapping { layer: string; meaning: string; method: string; confidence: number; entities: number }
export interface DrawingReport { filename: string; dxf_version: string; entities: number; header_units: string; detected_units: string; layers: LayerMapping[]; spaces: number; flats: number; outline_corners: number; outline_area: number; warnings: string[] }
export interface TicketOption { id: string; label: string; payload: Record<string, unknown>; valid: boolean }
export interface Ticket {
  id: string; kind: string; stage: number; subject: string; question: string; options: TicketOption[];
  default_option: string | null; evidence: string[]; status: string; check_me: boolean; applied_option: string | null;
  free_text: boolean; answer: { option_id: string | null; text: string | null; reason: string; by: string; accepted: boolean | null } | null;
}
export interface Project {
  id: string; name: string; stage: number; site: Site | null; placement: Placement | null; plates: Plate[]; units: Unit[];
  diagrams: Diagram[]; plans: Plan[]; checks: CheckSummary | null; drawing: DrawingReport | null; explanations: Record<string, string>;
}
export interface GeocodeResult { name: string; display_name: string; lat: number; lng: number; zoom: number; provider: string }
export interface BridgeStatus { linked: boolean; client: string | null; last_seen: number; fresh?: boolean }
