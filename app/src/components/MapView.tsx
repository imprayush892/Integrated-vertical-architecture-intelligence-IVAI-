/// <reference types="geojson" />
import maplibregl, { type GeoJSONSource, type LngLatLike, type Map as MLMap, type RasterTileSource } from "maplibre-gl";
import "maplibre-gl/dist/maplibre-gl.css";
import { useCallback, useEffect, useRef, useState } from "react";
import { rpc } from "../lib/api";
import { centroid, cssColor, fmt, MAP_ROOM_COLORS } from "../lib/geom";
import { useStore } from "../lib/store";
import { Icon, IconButton, LatchButton, Led, Spinner, Tabs } from "../ui";
import "./mapview.css";

const IMAGERY = "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}";
const LABELS = "https://server.arcgisonline.com/ArcGIS/rest/services/Reference/World_Boundaries_and_Places/MapServer/tile/{z}/{y}/{x}";
const STREETS = "https://server.arcgisonline.com/ArcGIS/rest/services/World_Street_Map/MapServer/tile/{z}/{y}/{x}";
const BASEMAP_KEY = "ivai.basemap";
const HOME_VIEW = { center: [72.8, 19.1] as [number, number], zoom: 1.6 };

type Basemap = "satellite" | "streets";

// ------------------------------------------------------------------------------------------- option focus
// PlacePanel latches an option; the map highlights its footprints and dims the others.
const OPTION_FOCUS = "ivai:option-focus";
/** Highlight one placement option (A, B or C) on the map. Pass null to clear. */
export function focusOption(id: string | null): void {
  window.dispatchEvent(new CustomEvent<string | null>(OPTION_FOCUS, { detail: id }));
}

interface Props {
  imagery?: { provider: string; token: string };
  tracing: boolean;
  points: [number, number][];
  onPoints: (p: [number, number][]) => void;
  flyTo?: { lng: number; lat: number; zoom: number } | null;
}

const emptyFC = { type: "FeatureCollection", features: [] } as GeoJSON.FeatureCollection;

function readBasemap(): Basemap {
  try { return localStorage.getItem(BASEMAP_KEY) === "streets" ? "streets" : "satellite"; } catch { return "satellite"; }
}

/** Colours for every overlay, read from the --map-* tokens so the CSS stays the single source. */
function mapColors() {
  const c = (n: string) => cssColor(`var(--${n})`);
  return {
    plot: c("map-plot"), casing: c("map-casing"), setback: c("map-setback"), buildable: c("map-buildable"), trace: c("map-trace"),
    a: c("map-tower-a"), b: c("map-tower-b"), c: c("map-tower-c"), plate: c("map-plate"),
  };
}

/** The no-build band is the site polygon with the buildable areas cut out. Built here from the engine's geojson. */
function withSetbackBand(fc: GeoJSON.FeatureCollection): GeoJSON.FeatureCollection {
  const site = fc.features.find((f) => f.properties?.kind === "site");
  const holes = fc.features.filter((f) => f.properties?.kind === "buildable");
  if (!site || site.geometry.type !== "Polygon" || holes.length === 0) return fc;
  const outer = site.geometry.coordinates[0];
  const inner = holes.flatMap((h) => (h.geometry.type === "Polygon" ? [h.geometry.coordinates[0]] : []));
  const band: GeoJSON.Feature = { type: "Feature", properties: { kind: "setback" }, geometry: { type: "Polygon", coordinates: [outer, ...inner] } };
  return { ...fc, features: [band, ...fc.features] };
}

export default function MapView({ tracing, points, onPoints, flyTo, imagery }: Props) {
  const el = useRef<HTMLDivElement>(null);
  const map = useRef<MLMap | null>(null);
  const markers = useRef<maplibregl.Marker[]>([]);
  const labels = useRef<maplibregl.Marker[]>([]);
  const towers = useRef<{ marker: maplibregl.Marker; option: string }[]>([]);
  const refocus = useRef<number | null>(null);
  const focusId = useRef<string | null>(null);
  const failures = useRef(0);
  const [ready, setReady] = useState(false);
  const [crashed, setCrashed] = useState<string | null>(null);
  const [tilesDown, setTilesDown] = useState(false);
  const [hud, setHud] = useState<{ lat: number; lng: number; z: number } | null>(null);
  const [view, setView] = useState({ bearing: 0, pitch: 0 });
  const [basemap, setBasemap] = useState<Basemap>(readBasemap);
  const [measure, setMeasure] = useState<{ edges: number[]; area: number; perimeter: number; valid: boolean } | null>(null);
  const project = useStore((s) => s.project);
  const tracingRef = useRef(tracing);
  const pointsRef = useRef(points);
  tracingRef.current = tracing;
  pointsRef.current = points;
  const usingMapbox = imagery?.provider === "mapbox" && !!imagery.token;

  // ---- paint helpers (colours from tokens, option focus)
  const applyColors = useCallback(() => {
    const m = map.current;
    if (!m || !m.getLayer("trace-line")) return;
    const k = mapColors();
    const set = (id: string, prop: string, v: unknown) => { if (m.getLayer(id)) m.setPaintProperty(id, prop, v as never); };
    const tower = ["match", ["get", "option"], "A", k.a, "B", k.b, "C", k.c, k.plot];
    set("setback", "fill-color", k.setback);
    set("buildable", "fill-color", k.buildable);
    set("buildable-casing", "line-color", k.casing);
    set("buildable-line", "line-color", k.buildable);
    set("site-casing", "line-color", k.casing);
    set("site-line", "line-color", k.plot);
    set("options-fill", "fill-color", tower);
    set("options-casing", "line-color", k.casing);
    set("options", "line-color", tower);
    set("plate-casing", "line-color", k.casing);
    set("plate", "line-color", k.plate);
    set("trace-fill", "fill-color", k.trace);
    set("trace-casing", "line-color", k.casing);
    set("trace-line", "line-color", k.trace);
  }, []);

  const applyFocus = useCallback(() => {
    const m = map.current;
    if (!m || !m.getLayer("options")) return;
    const id = focusId.current;
    const is = ["==", ["get", "option"], id ?? ""];
    const dim = (on: number, off: number) => (id ? ["case", is, on, off] : on);
    m.setPaintProperty("options", "line-opacity", dim(1, 0.3) as never);
    m.setPaintProperty("options-casing", "line-opacity", dim(0.75, 0.2) as never);
    m.setPaintProperty("options-fill", "fill-opacity", (id ? ["case", is, 0.3, 0.04] : ["case", ["get", "chosen"], 0.3, 0.12]) as never);
    m.setPaintProperty("options", "line-width", (id ? ["case", is, 3.5, 1.8] : ["case", ["get", "chosen"], 3.5, 2.2]) as never);
    towers.current.forEach((t) => t.marker.getElement().classList.toggle("is-dim", !!id && t.option !== id));
  }, []);

  // ---- create the map once
  useEffect(() => {
    if (!el.current || map.current) return;
    let m: MLMap;
    try {
      m = new maplibregl.Map({
        container: el.current,
        style: {
          version: 8,
          projection: { type: "globe" },
          sources: usingMapbox ? {
            imagery: { type: "raster", tileSize: 512, maxzoom: 22,
              tiles: [`https://api.mapbox.com/styles/v1/mapbox/satellite-streets-v12/tiles/512/{z}/{x}/{y}@2x?access_token=${encodeURIComponent(imagery!.token)}`],
              attribution: "© Mapbox © OpenStreetMap © Maxar" },
            streets: { type: "raster", tileSize: 512, maxzoom: 22,
              tiles: [`https://api.mapbox.com/styles/v1/mapbox/streets-v12/tiles/512/{z}/{x}/{y}@2x?access_token=${encodeURIComponent(imagery!.token)}`],
              attribution: "© Mapbox © OpenStreetMap" },
          } : {
            imagery: { type: "raster", tiles: [IMAGERY], tileSize: 256, maxzoom: 19, attribution: "Imagery © Esri, Maxar, Earthstar Geographics" },
            labels: { type: "raster", tiles: [LABELS], tileSize: 256, maxzoom: 19 },
            streets: { type: "raster", tiles: [STREETS], tileSize: 256, maxzoom: 19, attribution: "Streets © Esri, HERE, Garmin, OpenStreetMap contributors" },
          },
          layers: [
            { id: "bg", type: "background", paint: { "background-color": "#0b1d2a" } },
            { id: "imagery", type: "raster", source: "imagery", layout: { visibility: basemap === "satellite" ? "visible" : "none" } },
            { id: "streets", type: "raster", source: "streets", layout: { visibility: basemap === "streets" ? "visible" : "none" } },
            ...(usingMapbox ? [] : [{ id: "labels", type: "raster" as const, source: "labels", paint: { "raster-opacity": 0.9 }, layout: { visibility: basemap === "satellite" ? "visible" as const : "none" as const } }]),
          ],
          sky: { "atmosphere-blend": ["interpolate", ["linear"], ["zoom"], 0, 1, 6, 0.4, 9, 0] },
        } as maplibregl.StyleSpecification,
        center: HOME_VIEW.center,
        zoom: HOME_VIEW.zoom,
        attributionControl: { compact: false },
        maxPitch: 70,
      });
    } catch (e) {
      setCrashed(e instanceof Error ? e.message : String(e));
      return;
    }
    m.addControl(new maplibregl.ScaleControl({ unit: "metric" }), "bottom-right");
    m.on("load", () => {
      m.addSource("project", { type: "geojson", data: emptyFC });
      m.addSource("trace", { type: "geojson", data: emptyFC });
      const kind = (k: string) => ["==", ["get", "kind"], k] as never;
      m.addLayer({ id: "setback", type: "fill", source: "project", filter: kind("setback"), paint: { "fill-color": "#FF6A5C", "fill-opacity": 0.3 } });
      m.addLayer({ id: "buildable", type: "fill", source: "project", filter: kind("buildable"), paint: { "fill-color": "#2BE0C4", "fill-opacity": 0.2 } });
      m.addLayer({ id: "buildable-casing", type: "line", source: "project", filter: kind("buildable"), paint: { "line-color": "#0A0F16", "line-width": 4, "line-opacity": 0.6 } });
      m.addLayer({ id: "buildable-line", type: "line", source: "project", filter: kind("buildable"), paint: { "line-color": "#2BE0C4", "line-width": 1.8 } });
      m.addLayer({ id: "site-casing", type: "line", source: "project", filter: kind("site"), paint: { "line-color": "#0A0F16", "line-width": 5.5, "line-opacity": 0.7 } });
      m.addLayer({ id: "site-line", type: "line", source: "project", filter: kind("site"), paint: { "line-color": "#FFFFFF", "line-width": 2.5, "line-dasharray": [2, 1] } });
      m.addLayer({ id: "options-fill", type: "fill", source: "project", filter: kind("option"), paint: { "fill-color": "#FFFFFF", "fill-opacity": 0.12 } });
      m.addLayer({ id: "options-casing", type: "line", source: "project", filter: kind("option"), paint: { "line-color": "#0A0F16", "line-width": 5.5, "line-opacity": 0.75 } });
      m.addLayer({ id: "options", type: "line", source: "project", filter: kind("option"), paint: { "line-color": "#FFFFFF", "line-width": 2.2 } });
      m.addLayer({ id: "zones", type: "fill", source: "project",
        filter: ["in", ["get", "kind"], ["literal", ["zone-core", "zone-passage", "zone-shaft"]]],
        paint: { "fill-color": ["match", ["get", "kind"], "zone-core", "#7c8796", "zone-shaft", "#7cc4f5", "#c9ced6"], "fill-opacity": 0.85 } });
      m.addLayer({ id: "units", type: "line", source: "project", filter: kind("unit"), paint: { "line-color": "#ffffff", "line-width": 1.5 } });
      m.addLayer({ id: "rooms", type: "fill", source: "project", filter: kind("room"),
        paint: { "fill-color": ["match", ["get", "type"], ...Object.entries(MAP_ROOM_COLORS).flat(), "#d9d2ee"] as never, "fill-opacity": 0.92 } });
      m.addLayer({ id: "plate-casing", type: "line", source: "project", filter: kind("plate"), paint: { "line-color": "#0A0F16", "line-width": 5.5, "line-opacity": 0.7 } });
      m.addLayer({ id: "plate", type: "line", source: "project", filter: kind("plate"), paint: { "line-color": "#2BE0C4", "line-width": 3 } });
      m.addLayer({ id: "trace-fill", type: "fill", source: "trace", paint: { "fill-color": "#6CB8FF", "fill-opacity": 0.22 } });
      m.addLayer({ id: "trace-casing", type: "line", source: "trace", paint: { "line-color": "#0A0F16", "line-width": 5.5, "line-opacity": 0.7 } });
      m.addLayer({ id: "trace-line", type: "line", source: "trace", paint: { "line-color": "#6CB8FF", "line-width": 2.5 } });
      applyColors();
      applyFocus();
      const c = m.getCenter();
      setHud({ lat: c.lat, lng: c.lng, z: m.getZoom() });
      setReady(true);
    });
    m.on("moveend", () => { const c = m.getCenter(); setHud({ lat: c.lat, lng: c.lng, z: m.getZoom() }); });
    m.on("rotate", () => setView({ bearing: m.getBearing(), pitch: m.getPitch() }));
    m.on("pitch", () => setView({ bearing: m.getBearing(), pitch: m.getPitch() }));
    m.on("click", (e) => {
      if (!tracingRef.current) return;
      onPoints([...pointsRef.current, [e.lngLat.lng, e.lngLat.lat]]);
    });
    // tile health: a run of failed tile requests with no success in between means the basemap cannot be reached
    const isTile = (id?: string) => id === "imagery" || id === "streets" || id === "labels";
    m.on("error", (e) => {
      const sid = (e as unknown as { sourceId?: string }).sourceId;
      if (!isTile(sid)) return;
      failures.current += 1;
      if (failures.current >= 3) setTilesDown(true);
    });
    m.on("sourcedata", (e) => {
      const ev = e as unknown as { sourceId?: string; tile?: unknown; isSourceLoaded?: boolean };
      if (isTile(ev.sourceId) && ev.tile && failures.current > 0) { failures.current = 0; setTilesDown(false); }
    });
    map.current = m;
    return () => { m.remove(); map.current = null; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // re-read overlay colours when the theme changes (data-theme attribute or the system setting)
  useEffect(() => {
    const redo = () => applyColors();
    const mo = new MutationObserver(redo);
    mo.observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
    const mq = window.matchMedia("(prefers-color-scheme: dark)");
    mq.addEventListener("change", redo);
    return () => { mo.disconnect(); mq.removeEventListener("change", redo); };
  }, [applyColors]);

  // satellite / streets
  useEffect(() => {
    try { localStorage.setItem(BASEMAP_KEY, basemap); } catch { /* storage may be blocked */ }
    const m = map.current;
    if (!m || !ready) return;
    const sat = basemap === "satellite" ? "visible" : "none";
    const str = basemap === "streets" ? "visible" : "none";
    if (m.getLayer("imagery")) m.setLayoutProperty("imagery", "visibility", sat);
    if (m.getLayer("labels")) m.setLayoutProperty("labels", "visibility", sat);
    if (m.getLayer("streets")) m.setLayoutProperty("streets", "visibility", str);
  }, [basemap, ready]);

  // option focus from the Place panel
  useEffect(() => {
    const on = (e: Event) => { focusId.current = (e as CustomEvent<string | null>).detail ?? null; applyFocus(); };
    window.addEventListener(OPTION_FOCUS, on);
    return () => window.removeEventListener(OPTION_FOCUS, on);
  }, [applyFocus]);

  // crosshair cursor while tracing
  useEffect(() => { const c = map.current?.getCanvas(); if (c) c.style.cursor = tracing ? "crosshair" : ""; }, [tracing, ready]);

  // fly to search result / site (jumps instead of flying when the person asked for reduced motion)
  useEffect(() => {
    if (!map.current || !flyTo) return;
    map.current.flyTo({ center: [flyTo.lng, flyTo.lat] as LngLatLike, zoom: flyTo.zoom, speed: 1.6, curve: 1.5, essential: false });
  }, [flyTo]);

  // project overlays
  useEffect(() => {
    if (!ready || !map.current) return;
    let cancelled = false;
    void rpc<GeoJSON.FeatureCollection>("geojson").then((fc) => {
      const m = map.current;
      if (cancelled || !m) return;
      (m.getSource("project") as GeoJSONSource | undefined)?.setData(withSetbackBand(fc));
      // A / B / C chips at the centre of each footprint (not drawn as map text: the style has no glyph server)
      towers.current.forEach((t) => t.marker.remove());
      towers.current = [];
      const perOption: Record<string, number> = {};
      fc.features.forEach((f) => { if (f.properties?.kind === "option") perOption[f.properties.option] = (perOption[f.properties.option] ?? 0) + 1; });
      fc.features.forEach((f) => {
        if (f.properties?.kind !== "option" || f.geometry.type !== "Polygon") return;
        const opt = String(f.properties.option);
        const [lng, lat] = centroid(f.geometry.coordinates[0] as [number, number][]);
        const div = document.createElement("div");
        div.className = `mv-tower is-${opt.toLowerCase()}`;
        div.textContent = perOption[opt] > 1 ? `${opt}${String(f.properties.tower).replace(/^T/, "")}` : opt;
        div.setAttribute("aria-hidden", "true");
        towers.current.push({ option: opt, marker: new maplibregl.Marker({ element: div }).setLngLat([lng, lat]).addTo(m) });
      });
      applyFocus();
    }).catch(() => undefined);
    return () => { cancelled = true; };
  }, [ready, project, applyFocus]);

  // trace geometry, markers and live measurement
  useEffect(() => {
    const m = map.current;
    if (!ready || !m) return;
    markers.current.forEach((x) => x.remove());
    labels.current.forEach((x) => x.remove());
    markers.current = [];
    labels.current = [];
    const ring = points.length >= 3 ? [...points, points[0]] : points;
    const data: GeoJSON.FeatureCollection = { type: "FeatureCollection", features: points.length >= 2 ? [{
      type: "Feature", properties: {},
      geometry: points.length >= 3 ? { type: "Polygon", coordinates: [ring] } : { type: "LineString", coordinates: points },
    }] : [] };
    (m.getSource("trace") as GeoJSONSource | undefined)?.setData(data);
    if (!tracing) { setMeasure(null); return; }

    const nudge = (i: number, dx: number, dy: number, big: boolean) => {
      const step = big ? 2 : 0.5;
      const [lng, lat] = points[i];
      const next = [...points] as [number, number][];
      next[i] = [lng + (dx * step) / (111320 * Math.cos((lat * Math.PI) / 180)), lat + (dy * step) / 110574];
      refocus.current = i;
      onPoints(next);
    };
    points.forEach((p, i) => {
      const dot = document.createElement("div");
      dot.className = `vertex${i === 0 ? " is-first" : ""}`;
      dot.textContent = String(i + 1);
      dot.title = "Drag to move · double-click to remove";
      dot.tabIndex = 0;
      dot.setAttribute("role", "button");
      dot.setAttribute("aria-label", `Corner ${i + 1}${i === 0 ? ", start" : ""}. Arrow keys move it 0.5 metres, Shift for 2 metres, Delete removes it.`);
      dot.addEventListener("dblclick", (ev) => { ev.stopPropagation(); onPoints(points.filter((_, k) => k !== i)); });
      dot.addEventListener("keydown", (ev) => {
        const dir: Record<string, [number, number]> = { ArrowLeft: [-1, 0], ArrowRight: [1, 0], ArrowUp: [0, 1], ArrowDown: [0, -1] };
        if (dir[ev.key]) { ev.preventDefault(); ev.stopPropagation(); nudge(i, dir[ev.key][0], dir[ev.key][1], ev.shiftKey); }
        else if (ev.key === "Delete" || ev.key === "Backspace") {
          ev.preventDefault(); ev.stopPropagation();
          refocus.current = points.length > 1 ? Math.min(i, points.length - 2) : null;
          onPoints(points.filter((_, k) => k !== i));
        }
      });
      const mk = new maplibregl.Marker({ element: dot, draggable: true }).setLngLat(p).addTo(m);
      mk.on("dragend", () => {
        const ll = mk.getLngLat();
        const next = [...points];
        next[i] = [ll.lng, ll.lat];
        onPoints(next);
      });
      markers.current.push(mk);
    });
    if (refocus.current != null) {
      const idx = refocus.current;
      refocus.current = null;
      markers.current[idx]?.getElement().focus({ preventScroll: true });
    }
    if (points.length < 2) { setMeasure(null); return; }
    let cancelled = false;
    const t = setTimeout(() => {
      void rpc<{ edges: number[]; area: number; perimeter: number; valid: boolean }>("measure", { lnglat: points })
        .then((r) => {
          if (cancelled) return;
          setMeasure(r);
          const n = points.length >= 3 ? points.length : 1;
          for (let i = 0; i < n; i++) {
            const a = points[i], b = points[(i + 1) % points.length];
            const lab = document.createElement("div");
            lab.className = "edge-label";
            lab.innerHTML = `${fmt(r.edges[i])}<span class="u">m</span>`;
            labels.current.push(new maplibregl.Marker({ element: lab }).setLngLat([(a[0] + b[0]) / 2, (a[1] + b[1]) / 2]).addTo(m));
          }
        }).catch(() => undefined);
    }, 120);
    return () => { cancelled = true; clearTimeout(t); };
  }, [points, tracing, ready, onPoints]);

  // ---- chrome actions
  const mp = () => map.current;
  const fit = () => {
    const site = project?.site;
    if (!mp() || !site) return;
    const b = new maplibregl.LngLatBounds();
    site.lnglat.forEach((p) => b.extend([p[0], p[1]]));
    mp()!.fitBounds(b, { padding: 70, maxZoom: 19.5, essential: false });
  };
  const retryTiles = () => {
    const mm = mp();
    if (!mm) return;
    failures.current = 0;
    setTilesDown(false);
    (["imagery", "streets", "labels"] as const).forEach((id) => {
      const src = mm.getSource(id) as RasterTileSource | undefined;
      if (src?.tiles) src.setTiles([...src.tiles]);
    });
  };
  const dropAtCentre = () => {
    const c = mp()?.getCenter();
    if (c) onPoints([...pointsRef.current, [c.lng, c.lat]]);
  };

  if (crashed) {
    return (
      <div className="mv-root">
        <div className="mv-overlay mv-fail">
          <div className="ui-empty">
            <div className="ui-empty-icon neo-dish"><Icon name="alert" size={26} /></div>
            <h3 className="ui-empty-title">The map cannot start</h3>
            <div className="ui-empty-body">This browser could not set up WebGL. Turn on hardware acceleration, or try another browser. You can still use the other stages.</div>
            <div className="ui-empty-body" style={{ fontSize: 11, color: "var(--ink-3)" }}>{crashed}</div>
          </div>
        </div>
      </div>
    );
  }

  const closed = points.length >= 3;
  return (
    <div className={`mv-root${tracing ? " is-tracing" : ""}`}>
      <div ref={el} className="map" />
      {tracing && <div className="mv-cross" aria-hidden="true" />}

      <div className="mv-tl">
        <div className="mv-slab mv-tabs">
          <Tabs label="Base map" size="sm" value={basemap} onChange={(v) => setBasemap(v as Basemap)}
            items={[{ id: "satellite", label: "Satellite" }, { id: "streets", label: "Streets" }]} />
        </div>
      </div>

      {tracing && (
        <div className="mv-tc">
          <div className="mv-slab mv-trace-hint" role="status">
            <Led status="cobalt" pulse={points.length === 0} />
            <span>{points.length === 0 ? "Click the map to drop corner 1" : closed ? `${points.length} corners · click to add more` : `${points.length} corner${points.length > 1 ? "s" : ""} · need ${3 - points.length} more`}</span>
            <LatchButton size="sm" icon={<Icon name="plus" size={14} />} onClick={dropAtCentre} aria-label="Drop a corner at the crosshair in the map centre">
              At crosshair
            </LatchButton>
          </div>
        </div>
      )}

      <div className="mv-tr">
        <div className="mv-slab mv-rail" role="group" aria-label="Map controls">
          <IconButton label="Zoom in" icon={<Icon name="plus" />} onClick={() => mp()?.zoomIn({ essential: false })} />
          <IconButton label="Zoom out" icon={<Icon name="minus" />} onClick={() => mp()?.zoomOut({ essential: false })} />
          <IconButton label={`Reset north (now ${Math.round(((view.bearing % 360) + 360) % 360)}°)`}
            icon={<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" style={{ transform: `rotate(${-view.bearing}deg)`, transition: "transform 120ms" }}><path d="M12 3 7 14h10L12 3Z" fill="var(--det)" stroke="currentColor" /><path d="M7 14l5 7 5-7" /></svg>}
            onClick={() => mp()?.easeTo({ bearing: 0, pitch: 0, essential: false })} />
          <IconButton label={view.pitch > 10 ? "Flat view" : "Tilt view"} pressed={view.pitch > 10} icon={<Icon name="cube" />}
            onClick={() => mp()?.easeTo({ pitch: view.pitch > 10 ? 0 : 55, essential: false })} />
          <IconButton label="Fit the plot" icon={<Icon name="ruler" />} disabled={!project?.site} onClick={fit} />
          <IconButton label="Back to the globe" icon={<Icon name="map" />} onClick={() => mp()?.flyTo({ ...HOME_VIEW, bearing: 0, pitch: 0, essential: false })} />
        </div>
      </div>

      <div className="mv-bl">
        {hud && <span className="mv-chip" aria-label={`Map centre latitude ${hud.lat.toFixed(6)}, longitude ${hud.lng.toFixed(6)}, zoom ${hud.z.toFixed(2)}`}>
          <b>LAT</b>{hud.lat.toFixed(6)}° <b>LNG</b>{hud.lng.toFixed(6)}° <b>Z</b>{hud.z.toFixed(2)}
        </span>}
        {tracing && measure && closed && (
          <span className={`mv-chip ${measure.valid ? "det" : "fail"}`} role="status">
            {measure.valid
              ? <><b>AREA</b>{fmt(measure.area)}<span className="lcd-unit">m²</span> <b>PERIM</b>{fmt(measure.perimeter)}<span className="lcd-unit">m</span></>
              : <><Led status="fail" />Boundary crosses itself</>}
          </span>
        )}
      </div>

      {!ready && (
        <div className="mv-overlay" aria-live="polite">
          <span className="mv-slab mv-loading"><Spinner size={16} />Loading the map…</span>
        </div>
      )}
      {tilesDown && (
        <div className="mv-overlay" style={{ placeItems: "start center", paddingTop: 64 }}>
          <div className="mv-slab mv-banner" role="alert" style={{ pointerEvents: "auto" }}>
            <Icon name="alert" size={16} />
            <span>Map tiles are not loading. Check your connection. You can still trace.</span>
            <LatchButton size="sm" onClick={retryTiles}>Retry</LatchButton>
          </div>
        </div>
      )}
    </div>
  );
}
