/// <reference types="geojson" />
import maplibregl, { type GeoJSONSource, type LngLatLike, type Map as MLMap } from "maplibre-gl";
import "maplibre-gl/dist/maplibre-gl.css";
import { useEffect, useRef, useState } from "react";
import { rpc } from "../lib/api";
import { fmt, MAP_ROOM_COLORS } from "../lib/geom";
import { useStore } from "../lib/store";

const IMAGERY = "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}";
const LABELS = "https://server.arcgisonline.com/ArcGIS/rest/services/Reference/World_Boundaries_and_Places/MapServer/tile/{z}/{y}/{x}";

interface Props {
  tracing: boolean;
  points: [number, number][];
  onPoints: (p: [number, number][]) => void;
  flyTo?: { lng: number; lat: number; zoom: number } | null;
}

const emptyFC = { type: "FeatureCollection", features: [] } as GeoJSON.FeatureCollection;

export default function MapView({ tracing, points, onPoints, flyTo }: Props) {
  const el = useRef<HTMLDivElement>(null);
  const map = useRef<MLMap | null>(null);
  const markers = useRef<maplibregl.Marker[]>([]);
  const labels = useRef<maplibregl.Marker[]>([]);
  const [ready, setReady] = useState(false);
  const [hud, setHud] = useState<{ lat: number; lng: number; z: number } | null>(null);
  const [measure, setMeasure] = useState<{ edges: number[]; area: number; perimeter: number; valid: boolean } | null>(null);
  const project = useStore((s) => s.project);
  const tracingRef = useRef(tracing);
  const pointsRef = useRef(points);
  tracingRef.current = tracing;
  pointsRef.current = points;

  useEffect(() => {
    if (!el.current || map.current) return;
    const m = new maplibregl.Map({
      container: el.current,
      style: {
        version: 8,
        projection: { type: "globe" },
        sources: {
          imagery: { type: "raster", tiles: [IMAGERY], tileSize: 256, maxzoom: 19,
            attribution: "Imagery © Esri, Maxar, Earthstar Geographics" },
          labels: { type: "raster", tiles: [LABELS], tileSize: 256, maxzoom: 19 },
        },
        layers: [
          { id: "bg", type: "background", paint: { "background-color": "#0b1d2a" } },
          { id: "imagery", type: "raster", source: "imagery" },
          { id: "labels", type: "raster", source: "labels", paint: { "raster-opacity": 0.9 } },
        ],
        sky: { "atmosphere-blend": ["interpolate", ["linear"], ["zoom"], 0, 1, 6, 0.4, 9, 0] },
      } as maplibregl.StyleSpecification,
      center: [72.8, 19.1],
      zoom: 1.6,
      attributionControl: { compact: false },
      maxPitch: 70,
    });
    m.addControl(new maplibregl.NavigationControl({ visualizePitch: true }), "top-right");
    m.addControl(new maplibregl.ScaleControl({ unit: "metric" }), "bottom-right");
    m.on("load", () => {
      m.addSource("project", { type: "geojson", data: emptyFC });
      m.addSource("trace", { type: "geojson", data: emptyFC });
      m.addLayer({ id: "buildable", type: "fill", source: "project", filter: ["==", ["get", "kind"], "buildable"],
        paint: { "fill-color": "#0f8b8d", "fill-opacity": 0.18 } });
      m.addLayer({ id: "site-line", type: "line", source: "project", filter: ["==", ["get", "kind"], "site"],
        paint: { "line-color": "#ffffff", "line-width": 2.5, "line-dasharray": [2, 1] } });
      m.addLayer({ id: "options", type: "line", source: "project", filter: ["==", ["get", "kind"], "option"],
        paint: { "line-color": ["case", ["get", "chosen"], "#0f8b8d", "#f2a900"], "line-width": 2 } });
      m.addLayer({ id: "zones", type: "fill", source: "project",
        filter: ["in", ["get", "kind"], ["literal", ["zone-core", "zone-passage", "zone-shaft"]]],
        paint: { "fill-color": ["match", ["get", "kind"], "zone-core", "#7c8796", "zone-shaft", "#7cc4f5", "#c9ced6"], "fill-opacity": 0.85 } });
      m.addLayer({ id: "units", type: "line", source: "project", filter: ["==", ["get", "kind"], "unit"],
        paint: { "line-color": "#ffffff", "line-width": 1.5 } });
      m.addLayer({ id: "rooms", type: "fill", source: "project", filter: ["==", ["get", "kind"], "room"],
        paint: { "fill-color": ["match", ["get", "type"], ...Object.entries(MAP_ROOM_COLORS).flat(), "#d9d2ee"] as never, "fill-opacity": 0.92 } });
      m.addLayer({ id: "plate", type: "line", source: "project", filter: ["==", ["get", "kind"], "plate"],
        paint: { "line-color": "#0f8b8d", "line-width": 3 } });
      m.addLayer({ id: "trace-fill", type: "fill", source: "trace", paint: { "fill-color": "#0f8b8d", "fill-opacity": 0.22 } });
      m.addLayer({ id: "trace-line", type: "line", source: "trace", paint: { "line-color": "#2ee6e9", "line-width": 2.5 } });
      setReady(true);
    });
    m.on("moveend", () => { const c = m.getCenter(); setHud({ lat: c.lat, lng: c.lng, z: m.getZoom() }); });
    m.on("click", (e) => {
      if (!tracingRef.current) return;
      onPoints([...pointsRef.current, [e.lngLat.lng, e.lngLat.lat]]);
    });
    map.current = m;
    return () => { m.remove(); map.current = null; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // fly to search result / site
  useEffect(() => {
    if (!map.current || !flyTo) return;
    map.current.flyTo({ center: [flyTo.lng, flyTo.lat] as LngLatLike, zoom: flyTo.zoom, speed: 1.6, curve: 1.5, essential: true });
  }, [flyTo]);

  // project overlays
  useEffect(() => {
    if (!ready || !map.current) return;
    let cancelled = false;
    void rpc<GeoJSON.FeatureCollection>("geojson").then((fc) => {
      if (cancelled || !map.current) return;
      (map.current.getSource("project") as GeoJSONSource | undefined)?.setData(fc);
    }).catch(() => undefined);
    return () => { cancelled = true; };
  }, [ready, project]);

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
    points.forEach((p, i) => {
      const dot = document.createElement("div");
      dot.className = "vertex";
      dot.title = "Drag to move · double-click to remove";
      dot.addEventListener("dblclick", (ev) => { ev.stopPropagation(); onPoints(points.filter((_, k) => k !== i)); });
      const mk = new maplibregl.Marker({ element: dot, draggable: true }).setLngLat(p).addTo(m);
      mk.on("dragend", () => {
        const ll = mk.getLngLat();
        const next = [...points];
        next[i] = [ll.lng, ll.lat];
        onPoints(next);
      });
      markers.current.push(mk);
    });
    if (points.length < 2) { setMeasure(null); return; }
    const t = setTimeout(() => {
      void rpc<{ edges: number[]; area: number; perimeter: number; valid: boolean }>("measure", { lnglat: points })
        .then((r) => {
          setMeasure(r);
          const n = points.length >= 3 ? points.length : 1;
          for (let i = 0; i < n; i++) {
            const a = points[i], b = points[(i + 1) % points.length];
            const lab = document.createElement("div");
            lab.className = "edge-label";
            lab.textContent = `${fmt(r.edges[i])} m`;
            labels.current.push(new maplibregl.Marker({ element: lab }).setLngLat([(a[0] + b[0]) / 2, (a[1] + b[1]) / 2]).addTo(m));
          }
        }).catch(() => undefined);
    }, 120);
    return () => clearTimeout(t);
  }, [points, tracing, ready, onPoints]);

  return (
    <>
      <div ref={el} className="map" />
      <div className="map-hud">
        {hud && <span className="chip">LAT {hud.lat.toFixed(6)} LNG {hud.lng.toFixed(6)} Z {hud.z.toFixed(2)}</span>}
        {measure && points.length >= 3 && (
          <span className="chip" style={{ background: measure.valid ? undefined : "#c6373a" }}>
            {measure.valid ? `${fmt(measure.area)} m² · perimeter ${fmt(measure.perimeter)} m` : "Boundary crosses itself"}
          </span>
        )}
      </div>
    </>
  );
}
