import { useEffect, useRef } from "react";
import * as THREE from "three";
import { OrbitControls } from "three/examples/jsm/controls/OrbitControls.js";
import { MAP_ROOM_COLORS } from "../lib/geom";
import type { Plan, Plate, Ring } from "../lib/types";

function shapeOf(rings: Ring[]): THREE.Shape {
  const [outer, ...holes] = rings;
  const s = new THREE.Shape(outer.map(([x, y]) => new THREE.Vector2(x, y)));
  for (const h of holes) s.holes.push(new THREE.Path(h.map(([x, y]) => new THREE.Vector2(x, y))));
  return s;
}

export default function ThreeView({ plate, plans, floors }: { plate: Plate; plans: Plan[]; floors: number }) {
  const el = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const host = el.current;
    if (!host) return;
    const w = host.clientWidth, h = host.clientHeight;
    const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });
    renderer.setPixelRatio(Math.min(2, window.devicePixelRatio));
    renderer.setSize(w, h);
    host.appendChild(renderer.domElement);
    const scene = new THREE.Scene();
    const cam = new THREE.PerspectiveCamera(40, w / h, 0.5, 2000);
    const H = 3.0;
    const top = floors * H;
    cam.position.set(plate.width * 1.1, -plate.depth * 1.6, top * 1.3 + 30);
    cam.up.set(0, 0, 1);
    const controls = new OrbitControls(cam, renderer.domElement);
    controls.target.set(0, 0, top * 0.55);
    controls.enableDamping = true;
    scene.add(new THREE.HemisphereLight(0xffffff, 0x445566, 1.1));
    const sun = new THREE.DirectionalLight(0xffffff, 1.4);
    sun.position.set(-60, -40, 120);
    scene.add(sun);
    const glass = new THREE.MeshStandardMaterial({ color: 0x9ccfd8, transparent: true, opacity: 0.35, roughness: 0.2 });
    const slabMat = new THREE.MeshStandardMaterial({ color: 0xe9ecef, roughness: 0.8 });
    const coreMat = new THREE.MeshStandardMaterial({ color: 0x8b95a5 });
    const wallMat = new THREE.MeshStandardMaterial({ color: 0x2b3038 });
    const outline = shapeOf([plate.outline]);
    const core = plate.zones.filter((z) => z.kind === "core").flatMap((z) => z.polygon);
    for (let f = 0; f < floors; f++) {
      const slab = new THREE.Mesh(new THREE.ExtrudeGeometry(outline, { depth: 0.2, bevelEnabled: false }), slabMat);
      slab.position.z = f * H;
      scene.add(slab);
      if (f < floors - 1) {
        const g = new THREE.Mesh(new THREE.ExtrudeGeometry(outline, { depth: H - 0.2, bevelEnabled: false }), glass);
        g.position.z = f * H + 0.2;
        scene.add(g);
      }
    }
    for (const part of core) {
      const m = new THREE.Mesh(new THREE.ExtrudeGeometry(shapeOf(part), { depth: top + 3, bevelEnabled: false }), coreMat);
      scene.add(m);
    }
    // typical floor on top: walls and coloured rooms
    const z0 = (floors - 1) * H + 0.2;
    for (const pl of plans) {
      for (const r of pl.rooms) {
        const m = new THREE.Mesh(new THREE.ShapeGeometry(shapeOf([r.polygon])),
          new THREE.MeshStandardMaterial({ color: MAP_ROOM_COLORS[r.type] ?? "#d9d2ee", side: THREE.DoubleSide }));
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
      new THREE.MeshStandardMaterial({ color: 0x7d8a76 }));
    ground.position.z = -0.05;
    scene.add(ground);
    let raf = 0;
    const loop = () => { controls.update(); renderer.render(scene, cam); raf = requestAnimationFrame(loop); };
    loop();
    const ro = new ResizeObserver(() => {
      const W = host.clientWidth, Hh = host.clientHeight;
      renderer.setSize(W, Hh);
      cam.aspect = W / Hh;
      cam.updateProjectionMatrix();
    });
    ro.observe(host);
    return () => {
      cancelAnimationFrame(raf);
      ro.disconnect();
      controls.dispose();
      renderer.dispose();
      scene.traverse((o) => { if (o instanceof THREE.Mesh) { o.geometry.dispose(); (o.material as THREE.Material).dispose(); } });
      host.removeChild(renderer.domElement);
    };
  }, [plate, plans, floors]);
  return <div ref={el} className="three" />;
}
