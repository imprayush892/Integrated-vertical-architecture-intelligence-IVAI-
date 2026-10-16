import { useCallback, useEffect, useRef, useState } from "react";
import * as THREE from "three";
import { OrbitControls } from "three/examples/jsm/controls/OrbitControls.js";
import { cssColor, ROOM_COLORS } from "../lib/geom";
import type { Plan, Plate, Ring } from "../lib/types";
import { EmptyState, Icon, Kbd, LatchButton, ProvenanceTag, Readout } from "../ui";

function shapeOf(rings: Ring[]): THREE.Shape {
  const [outer, ...holes] = rings;
  const s = new THREE.Shape(outer.map(([x, y]) => new THREE.Vector2(x, y)));
  for (const h of holes) s.holes.push(new THREE.Path(h.map(([x, y]) => new THREE.Vector2(x, y))));
  return s;
}

// ------------------------------------------------------------------------------------------- styles (tv- prefix)
const CSS = `
.tv { position: absolute; inset: 0; overflow: hidden; }
.tv .three { outline: none; }
.tv .three:focus-visible { box-shadow: inset 0 0 0 3px var(--cobalt); border-radius: inherit; }
.tv-hud { position: absolute; z-index: 4; display: flex; gap: 8px; flex-wrap: wrap; align-items: center; }
.tv-hud.tl { left: 12px; top: 12px; right: 12px; justify-content: space-between; pointer-events: none; }
.tv-hud.tl > * { pointer-events: auto; }
.tv-readouts { display: flex; gap: 8px; align-items: flex-end; flex-wrap: wrap; padding: 8px 10px; border-radius: var(--r-md); box-shadow: var(--neo-raised-sm); background: var(--neo-bg); border: 1px solid var(--chamfer); }
.tv-views { display: flex; gap: 6px; }
.tv-hint { position: absolute; left: 12px; bottom: 12px; z-index: 4; max-width: calc(100% - 24px); display: flex; flex-wrap: wrap; gap: 6px 14px; align-items: center; padding: 7px 12px; border-radius: var(--r-pill); background: var(--neo-bg); box-shadow: var(--neo-latched); border: 1px solid var(--chamfer-inner); font: 500 11.5px var(--f-sans); color: var(--ink-2); transition: opacity var(--dur-3) var(--ease); }
.tv-hint.is-quiet { opacity: 0.55; }
.tv-hint:hover, .tv-hint:focus-within { opacity: 1; }
.tv-hint > span { display: inline-flex; align-items: center; gap: 6px; }
.tv-legend { position: absolute; right: 12px; bottom: 12px; z-index: 4; display: flex; flex-wrap: wrap; gap: 6px; justify-content: flex-end; max-width: 46%; }
.tv-legend > span { display: inline-flex; align-items: center; gap: 6px; padding: 3px 9px; font: 600 11px var(--f-mono); color: var(--ink-2); background: var(--neo-bg); border: 1px solid var(--chamfer); border-radius: var(--r-pill); box-shadow: var(--neo-raised-xs); }
.tv-legend i { width: 10px; height: 10px; border-radius: 3px; display: inline-block; box-shadow: inset 0 0 0 1px var(--chamfer-inner); }
.tv-note { position: absolute; left: 50%; top: 64px; transform: translateX(-50%); z-index: 4; max-width: calc(100% - 24px); padding: 6px 12px; border-radius: var(--r-pill); background: var(--neo-bg); box-shadow: var(--neo-latched); font-size: 12px; color: var(--ink-2); text-align: center; }
@media (max-width: 700px) { .tv-legend { display: none; } .tv-hint { right: 12px; } }
`;
function useInjectedCss(id: string, css: string) {
  useEffect(() => {
    if (document.getElementById(id)) return;
    const el = document.createElement("style");
    el.id = id;
    el.textContent = css;
    document.head.appendChild(el);
  }, [id, css]);
}

type ViewApi = { iso: () => void; top: () => void; front: () => void };
const STOREY = 3.0; // metres per floor (as drawn)

export default function ThreeView({ plate, plans, floors }: { plate: Plate; plans: Plan[]; floors: number }) {
  useInjectedCss("tv-styles", CSS);
  const el = useRef<HTMLDivElement>(null);
  const api = useRef<ViewApi | null>(null);
  const [failed, setFailed] = useState<string | null>(null);
  const [touched, setTouched] = useState(false);

  useEffect(() => {
    const host = el.current;
    if (!host) return;
    setFailed(null);
    setTouched(false);
    const w = Math.max(1, host.clientWidth), h = Math.max(1, host.clientHeight);
    let renderer: THREE.WebGLRenderer;
    try {
      renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });
    } catch (e) {
      setFailed(e instanceof Error ? e.message : "WebGL is not available");
      return;
    }
    const reduced = typeof window.matchMedia === "function" && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    // Theme-aware materials: read the design tokens once (the shell remounts this view when the theme changes)
    const col = (v: string) => new THREE.Color(cssColor(v));
    const dark = (() => {
      const c = col("var(--neo-bg)");
      return c.r * 0.3 + c.g * 0.59 + c.b * 0.11 < 0.4;
    })();
    renderer.setPixelRatio(Math.min(2, window.devicePixelRatio));
    renderer.setSize(w, h);
    host.appendChild(renderer.domElement);
    const scene = new THREE.Scene();
    const bg = col("var(--neo-bg)");
    scene.fog = new THREE.Fog(bg, Math.max(plate.width, plate.depth) * 4, Math.max(plate.width, plate.depth) * 12);
    const cam = new THREE.PerspectiveCamera(40, w / h, 0.5, 2000);
    const top = floors * STOREY;
    const iso = () => { cam.position.set(plate.width * 1.1, -plate.depth * 1.6, top * 1.3 + 30); controls.target.set(0, 0, top * 0.55); controls.update(); };
    const topView = () => { cam.position.set(0.001, -0.001, Math.max(plate.width, plate.depth) * 2.4 + top); controls.target.set(0, 0, 0); controls.update(); };
    const front = () => { cam.position.set(0, -Math.max(plate.width, plate.depth) * 2.6, top * 0.7); controls.target.set(0, 0, top * 0.5); controls.update(); };
    cam.up.set(0, 0, 1);
    const controls = new OrbitControls(cam, renderer.domElement);
    controls.enableDamping = !reduced;
    controls.maxPolarAngle = Math.PI * 0.495; // stay above the ground
    controls.listenToKeyEvents(host); // arrow keys pan once the view has focus
    controls.addEventListener("start", () => setTouched(true));
    iso();
    api.current = { iso, top: topView, front };

    scene.add(new THREE.HemisphereLight(0xffffff, dark ? 0x222a36 : 0x445566, dark ? 0.9 : 1.1));
    const sun = new THREE.DirectionalLight(dark ? 0xdbe6ff : 0xffffff, dark ? 1.1 : 1.4);
    sun.position.set(-60, -40, 120);
    scene.add(sun);

    const glass = new THREE.MeshStandardMaterial({ color: col("var(--cobalt)"), transparent: true, opacity: dark ? 0.22 : 0.3, roughness: 0.2, depthWrite: false });
    const slabMat = new THREE.MeshStandardMaterial({ color: col("var(--plate-fill)"), roughness: 0.8 });
    const coreMat = new THREE.MeshStandardMaterial({ color: col("var(--ink-3)"), roughness: 0.7 });
    const wallMat = new THREE.MeshStandardMaterial({ color: col("var(--wall)") });
    const outline = shapeOf([plate.outline]);
    const core = plate.zones.filter((z) => z.kind === "core").flatMap((z) => z.polygon);
    for (let f = 0; f < floors; f++) {
      const slab = new THREE.Mesh(new THREE.ExtrudeGeometry(outline, { depth: 0.2, bevelEnabled: false }), slabMat);
      slab.position.z = f * STOREY;
      scene.add(slab);
      if (f < floors - 1) {
        const g = new THREE.Mesh(new THREE.ExtrudeGeometry(outline, { depth: STOREY - 0.2, bevelEnabled: false }), glass);
        g.position.z = f * STOREY + 0.2;
        scene.add(g);
      }
    }
    for (const part of core) {
      const m = new THREE.Mesh(new THREE.ExtrudeGeometry(shapeOf(part), { depth: top + 3, bevelEnabled: false }), coreMat);
      scene.add(m);
    }
    // typical floor on top: walls and coloured rooms (same room tokens as the plan, so both themes match)
    const z0 = (floors - 1) * STOREY + 0.2;
    const roomMats = new Map<string, THREE.MeshStandardMaterial>();
    const roomMat = (type: string) => {
      let m = roomMats.get(type);
      if (!m) { m = new THREE.MeshStandardMaterial({ color: col(ROOM_COLORS[type] ?? "var(--room-other)"), side: THREE.DoubleSide }); roomMats.set(type, m); }
      return m;
    };
    for (const pl of plans) {
      for (const r of pl.rooms) {
        const m = new THREE.Mesh(new THREE.ShapeGeometry(shapeOf([r.polygon])), roomMat(r.type));
        m.position.z = z0 + 0.02;
        scene.add(m);
      }
      for (const wl of pl.walls) {
        const m = new THREE.Mesh(new THREE.ExtrudeGeometry(shapeOf(wl.polygon), { depth: 2.8, bevelEnabled: false }), wallMat);
        m.position.z = z0;
        scene.add(m);
      }
    }
    const ground = new THREE.Mesh(new THREE.CircleGeometry(Math.max(plate.width, plate.depth) * 2, 64),
      new THREE.MeshStandardMaterial({ color: col("var(--neo-grad-dark)"), roughness: 1 }));
    ground.position.z = -0.05;
    scene.add(ground);
    let raf = 0;
    const loop = () => { controls.update(); renderer.render(scene, cam); raf = requestAnimationFrame(loop); };
    loop();
    const ro = new ResizeObserver(() => {
      const W = Math.max(1, host.clientWidth), Hh = Math.max(1, host.clientHeight);
      renderer.setSize(W, Hh);
      cam.aspect = W / Hh;
      cam.updateProjectionMatrix();
    });
    ro.observe(host);
    return () => {
      api.current = null;
      cancelAnimationFrame(raf);
      ro.disconnect();
      controls.stopListenToKeyEvents();
      controls.dispose();
      renderer.dispose();
      const seen = new Set<THREE.Material>();
      scene.traverse((o) => {
        if (o instanceof THREE.Mesh) {
          o.geometry.dispose();
          const m = o.material as THREE.Material;
          if (!seen.has(m)) { seen.add(m); m.dispose(); }
        }
      });
      if (renderer.domElement.parentNode === host) host.removeChild(renderer.domElement);
    };
  }, [plate, plans, floors]);

  const view = useCallback((k: keyof ViewApi) => api.current?.[k](), []);
  const rooms = plans.reduce((n, p) => n + p.rooms.length, 0);
  const legend: [string, string][] = [
    ["var(--plate-fill)", "slab"], ["var(--cobalt)", "glass"], ["var(--ink-3)", "core"], ["var(--wall)", "walls"], ["var(--room-living)", "rooms"],
  ];

  if (failed) {
    return (
      <div className="tv">
        <EmptyState fill icon={<Icon name="cube" size={26} />} title="3D view unavailable"
          action={<LatchButton variant="primary" onClick={() => window.location.reload()}>Reload</LatchButton>}>
          This browser could not start WebGL. The plan view still works. ({failed})
        </EmptyState>
      </div>
    );
  }

  return (
    <div className="tv">
      <div ref={el} className="three" tabIndex={0} role="application"
        aria-label="3D massing view. Drag to orbit, scroll to zoom, arrow keys to pan." />
      <div className="tv-hud tl">
        <div className="tv-readouts" role="group" aria-label="Massing figures">
          <Readout label="Floors" value={floors} digits={0} tone="det" size="sm" />
          <Readout label="Height" value={floors * STOREY} unit="m" tone="det" size="sm" />
          <Readout label="Plate" value={plate.area} unit="m²" tone="det" size="sm" />
          <ProvenanceTag by="engine" kind="derived" compact source="Floors = building height / 3.0 m. Drawn height, not the sanctioned height." />
        </div>
        <div className="tv-views" role="group" aria-label="Camera presets">
          <LatchButton size="sm" onClick={() => view("iso")}>Reset</LatchButton>
          <LatchButton size="sm" onClick={() => view("top")}>Top</LatchButton>
          <LatchButton size="sm" onClick={() => view("front")}>Front</LatchButton>
        </div>
      </div>
      {plans.length === 0 && <div className="tv-note" role="status">No flat plans yet, so only the massing is shown.</div>}
      <div className={`tv-hint${touched ? " is-quiet" : ""}`} aria-hidden="true">
        <span><Kbd>Drag</Kbd> orbit</span>
        <span><Kbd>Right-drag</Kbd> pan</span>
        <span><Kbd>Scroll</Kbd> zoom</span>
        <span>Typical floor on top{rooms ? ` · ${rooms} rooms` : ""}</span>
      </div>
      <div className="tv-legend" aria-label="Materials">
        {legend.map(([c, t]) => <span key={t}><i style={{ background: c }} />{t}</span>)}
      </div>
    </div>
  );
}
