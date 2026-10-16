"""The one engine. The UI (JSON-RPC over HTTP) and Claude (MCP) call the same methods on ``Planner``.

Rules first: every stage runs deterministically. Where a choice is unclear the engine opens a *ticket*
with 2-4 options and a safe default, applies the default at once (marked "check me"), and lets Claude
or the user change it later. Any answer is re-checked by the same rules before it reaches the plan.
"""

from __future__ import annotations

import copy
import math
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import shapely
from shapely.geometry import Polygon

from . import __version__, geo, rules
from .checks import FloorContext, run_checks
from .diagram import from_programme, precheck
from .dxf_io import export_dxf, import_dxf, learn_layer
from .geocode import geocode
from .layout import apply_fix, solve_flat
from .models import (
    Actor,
    CheckSummary,
    Diagram,
    DiagramRoom,
    Evidence,
    EvidenceKind,
    Placement,
    Plan,
    Plate,
    Programme,
    Project,
    ShapeKind,
    Site,
    Ticket,
    TicketAnswer,
    TicketKind,
    TicketOption,
    Unit,
)
from .placement import drivers_note, place
from .plate import LABELS, RATINGS, all_shapes, build_plate, fit
from .site import build_site
from .store import ProjectStore
from .units import facade_length, presets, programme_from_rooms, split_units

STAGE_NAMES = {1: "Find", 2: "Trace", 3: "Climate", 4: "Place", 5: "Shape", 6: "Split", 7: "Wire", 8: "Check"}


class PlannerError(Exception):
    def __init__(self, message: str, code: str = "bad_request"):
        super().__init__(message)
        self.code = code


def _now() -> float:
    return time.time()


class Planner:
    def __init__(self, path: str | Path | None = None, name: str = "Untitled project"):
        self._lock = threading.RLock()
        self.listeners: list[Any] = []
        self.bridge: dict[str, Any] = {"linked": False, "client": None, "last_seen": 0.0}
        self.store: ProjectStore | None = None
        self.project = Project(id=uuid.uuid4().hex[:12], name=name, created=_now(), updated=_now())
        if path:
            self.open_project(str(path), create=True, name=name)

    # ================================================================= plumbing
    def _emit(self, kind: str, **body: Any) -> None:
        for fn in list(self.listeners):
            try:
                fn({"kind": kind, **body})
            except Exception:  # noqa: BLE001 - a broken listener must not break the engine
                pass

    def _save(self, action: str) -> None:
        if self.store:
            self.store.save(self.project, action)
        self._emit("project", action=action, stage=self.project.stage)

    def _evidence(self, key: str, value: Any, kind: EvidenceKind, source: str, by: Actor = Actor.engine, stage: int = 0):
        if self.store:
            self.store.add_evidence(Evidence(key=key, value=value, kind=kind, source=source, by=by, stage=stage, ts=_now()))

    def _decision(self, stage: int, what: str, by: str = "engine") -> None:
        """Log one decision (rules or Claude) for the 92% gate."""
        if self.store:
            self.store.event("decision", {"stage": stage, "what": what, "by": by})

    def _tickets(self) -> list[Ticket]:
        return self.store.tickets() if self.store else list(getattr(self, "_mem_tickets", {}).values())

    def _put_ticket(self, t: Ticket) -> None:
        if self.store:
            self.store.put_ticket(t)
        else:
            if not hasattr(self, "_mem_tickets"):
                self._mem_tickets: dict[str, Ticket] = {}
            self._mem_tickets[t.id] = t
        self._emit("ticket", id=t.id, status=t.status)

    def _get_ticket(self, tid: str) -> Ticket:
        t = self.store.ticket(tid) if self.store else getattr(self, "_mem_tickets", {}).get(tid)
        if not t:
            raise PlannerError(f"No decision {tid!r}", "not_found")
        return t

    def _drop_tickets(self, prefix: str) -> None:
        if self.store:
            self.store.delete_tickets(prefix)
        elif hasattr(self, "_mem_tickets"):
            for k in [k for k in self._mem_tickets if k.startswith(prefix)]:
                del self._mem_tickets[k]

    def _open_ticket(self, kind: TicketKind, stage: int, subject: str, question: str, options: list[dict],
                     default: str | None, evidence: list[str], free_text: bool = False) -> Ticket:
        tid = f"{kind.value}:{subject}"
        old = None
        try:
            old = self._get_ticket(tid)
        except PlannerError:
            pass
        t = Ticket(
            id=tid, kind=kind, stage=stage, subject=subject, question=question,
            options=[TicketOption(id=o["id"], label=o["label"], payload=o.get("payload", {}), valid=o.get("valid", True))
                     for o in options],
            default_option=default, evidence=evidence, status="defaulted", check_me=True,
            applied_option=default, free_text=free_text, created=old.created if old else _now(),
        )
        self._put_ticket(t)
        self._decision(stage, f"ticket {tid} defaulted", by="engine-default")
        return t

    def subscribe(self, fn) -> None:
        self.listeners.append(fn)

    def unsubscribe(self, fn) -> None:
        if fn in self.listeners:
            self.listeners.remove(fn)

    # ================================================================= projects
    def create_project(self, path: str, name: str = "Untitled project") -> dict:
        with self._lock:
            p = Path(path)
            if p.exists():
                raise PlannerError(f"{p} already exists", "exists")
            return self.open_project(str(p), create=True, name=name)

    def open_project(self, path: str, create: bool = False, name: str = "Untitled project") -> dict:
        with self._lock:
            p = Path(path).expanduser()
            if not p.exists() and not create:
                raise PlannerError(f"No project at {p}", "not_found")
            if self.store:
                self.store.close()
            self.store = ProjectStore(p)
            loaded = self.store.load()
            if loaded:
                self.project = loaded
            else:
                self.project = Project(id=uuid.uuid4().hex[:12], name=name, created=_now(), updated=_now())
                self.store.save(self.project, "create")
            self._emit("project", action="open", stage=self.project.stage)
            return self.summary()

    def project_path(self) -> str | None:
        return str(self.store.path) if self.store else None

    def get_project(self, full: bool = False) -> dict:
        if full:
            return self.project.model_dump(mode="json")
        return self.summary()

    def rename(self, name: str) -> dict:
        self.project.name = name.strip() or self.project.name
        self._save("rename")
        return self.summary()

    def undo(self) -> dict:
        with self._lock:
            if not self.store:
                raise PlannerError("No project file open")
            prev = self.store.undo()
            if prev is None:
                raise PlannerError("Nothing to undo")
            self.project = prev
            self._emit("project", action="undo", stage=self.project.stage)
            return self.summary()

    def history(self) -> list[dict]:
        return self.store.history() if self.store else []

    # ================================================================= summary (small answers for Claude)
    def summary(self) -> dict:
        pr = self.project
        open_t = [t for t in self._tickets() if t.status in ("open", "defaulted")]
        out: dict[str, Any] = {
            "project": pr.name, "id": pr.id, "file": self.project_path(), "stage": pr.stage,
            "stage_name": STAGE_NAMES.get(pr.stage, ""), "engine": __version__,
            "decisions_waiting": len(open_t),
        }
        if pr.site:
            s = pr.site
            out["site"] = {"name": s.name, "locality": s.locality, "area_m2": s.area, "perimeter_m": s.perimeter,
                           "setback_m": s.setback, "buildable_m2": s.buildable_area, "height_m": s.building_height,
                           "road_width_m": s.road_width}
        if pr.placement:
            out["placement"] = {"chosen": pr.placement.chosen,
                                "options": [{"id": o.id, "towers": o.towers, "coverage": o.coverage,
                                             "plate_m2": [p.area for p in o.plates], "gap_m": o.min_gap}
                                            for o in pr.placement.options]}
        if pr.plates:
            out["plates"] = [{"tower": p.tower_id, "shape": p.shape.value, "area_m2": p.area, "corners": p.corners,
                              "zones": {z.kind: z.area for z in p.zones}} for p in pr.plates]
        if pr.units:
            out["units"] = [{"id": u.id, "area_m2": u.area, "programme": u.programme.id if u.programme else None,
                             "flags": u.flags, "planned": any(pl.unit_id == u.id for pl in pr.plans)} for u in pr.units]
        if pr.plans:
            out["carpet_m2"] = round(sum(pl.carpet_area for pl in pr.plans), 1)
            out["mix"] = self._mix()
        if pr.checks:
            out["checks"] = {"counts": pr.checks.counts, "by_code": pr.checks.by_code}
        if pr.drawing:
            out["drawing"] = {"file": pr.drawing.filename, "spaces": pr.drawing.spaces, "flats": pr.drawing.flats}
        return out

    def _mix(self) -> dict[str, int]:
        mix: dict[str, int] = {}
        for pl in self.project.plans:
            bhk = sum(1 for r in pl.rooms if r.type in ("master", "bedroom"))
            k = f"{bhk}BHK"
            mix[k] = mix.get(k, 0) + 1
        return mix

    # ================================================================= stage 1
    def search_place(self, query: str, limit: int = 5) -> list[dict]:
        res = geocode(query, limit)
        self._decision(1, "geocode")
        return [r.model_dump() for r in res]

    # ================================================================= stages 2-3
    def set_site(self, lnglat: list[list[float]], name: str = "SITE 01", locality: str = "", road_width: float = 12.0,
                 building_height: float = 24.0, road_edges: list[int] | None = None, epw: str | None = None) -> dict:
        with self._lock:
            try:
                site = build_site([(float(a), float(b)) for a, b in lnglat], name=name, locality=locality,
                                  road_width=road_width, building_height=building_height, road_edges=road_edges, epw=epw)
            except geo.GeometryError as e:
                raise PlannerError(str(e), "bad_geometry") from e
            if site.buildable_area <= 0:
                raise PlannerError("Nothing is buildable after the setback", "no_buildable")
            pr = self.project
            pr.site = site
            pr.placement = None
            pr.plates, pr.units, pr.diagrams, pr.plans, pr.checks = [], [], [], [], None
            pr.stage = max(3, min(pr.stage, 3))
            for prefix in ("T0", "T3", "T5", "T6", "T7", "T8"):
                self._drop_tickets(prefix)
            self._evidence("site.polygon", site.lnglat, EvidenceKind.observed, "user clicks", Actor.user, 2)
            self._evidence("site.area", site.area, EvidenceKind.derived, "shoelace in UTM", stage=2)
            self._evidence("site.geodesic_area", site.geodesic_area, EvidenceKind.derived, "WGS84 geodesic", stage=2)
            self._evidence("site.setback", site.setback, EvidenceKind.derived, site.setback_clause, stage=3)
            if site.climate:
                self._evidence("climate.wind", site.climate.wind.source, EvidenceKind.derived if not site.climate.wind.indicative
                               else EvidenceKind.observed, "wind rose", stage=3)
                for what in ("sea_view", "creek"):
                    self._evidence(f"climate.{what}", "unknown", EvidenceKind.unknown, "not detected", stage=3)
                    label = "Sea view" if what == "sea_view" else "Creek"
                    self._open_ticket(
                        TicketKind.T0, 3, what, f"{label} near {site.locality or site.name}? Read the map names around the plot.",
                        [{"id": "yes", "label": "Yes"}, {"id": "no", "label": "No"}, {"id": "unknown", "label": "Unknown (–)"}],
                        "unknown", [f"plot centre {site.lnglat[0]}", "map labels within 1 km"],
                    )
            for d in ("setback", "sun", "wind"):
                self._decision(3, d)
            self._save("set_site")
            return self.site_info()

    def site_info(self) -> dict:
        s = self._need_site()
        return s.model_dump(mode="json")

    def set_site_params(self, road_width: float | None = None, building_height: float | None = None,
                        road_edges: list[int] | None = None) -> dict:
        s = self._need_site()
        return self.set_site(s.lnglat, s.name, s.locality, road_width if road_width is not None else s.road_width,
                             building_height if building_height is not None else s.building_height,
                             road_edges if road_edges is not None else s.road_edges)

    def measure(self, lnglat: list[list[float]]) -> dict:
        """Live measurement while tracing (no state change)."""
        if len(lnglat) < 2:
            return {"edges": [], "area": 0.0, "perimeter": 0.0, "valid": False}
        frame = geo.make_frame([tuple(p) for p in lnglat])  # type: ignore[misc]
        pts = geo.to_site(frame, [tuple(p) for p in lnglat])  # type: ignore[misc]
        edges = geo.edge_lengths(pts) if len(pts) >= 3 else [math.dist(pts[0], pts[1])]
        valid = True
        try:
            if len(pts) >= 3:
                geo.validate_ring(pts)
        except geo.GeometryError:
            valid = False
        area = abs(geo.shoelace(pts)) if len(pts) >= 3 else 0.0
        return {"edges": [round(e, 2) for e in edges], "area": round(area, 2), "perimeter": round(sum(edges), 2),
                "valid": valid}

    def _need_site(self) -> Site:
        if not self.project.site:
            raise PlannerError("Trace the plot first", "no_site")
        return self.project.site

    # ================================================================= stage 4
    def place_towers(self, view_bearing: float | None = None) -> dict:
        with self._lock:
            site = self._need_site()
            pl = place(site, view_bearing=view_bearing)
            if not pl.options:
                raise PlannerError("No tower fits the buildable area with the plate limits", "no_placement")
            self.project.placement = pl
            self.project.stage = max(self.project.stage, 4)
            for o in pl.options:
                self._open_ticket(TicketKind.T5, 4, o.id, f"Write the design-drivers note for option {o.id} in plain words.",
                                  [{"id": "template", "label": o.drivers}], "template",
                                  [f"score terms {o.score_terms}", f"coverage {o.coverage:.1%}", f"gap {o.min_gap}"],
                                  free_text=True)
                self._decision(4, f"placement {o.id}")
            self._save("place_towers")
            return pl.model_dump(mode="json")

    def choose_placement(self, option: str) -> dict:
        with self._lock:
            pl = self.project.placement
            if not pl:
                raise PlannerError("Place towers first", "no_placement")
            opt = next((o for o in pl.options if o.id == option), None)
            if not opt:
                raise PlannerError(f"No option {option}", "not_found")
            pl.chosen = opt.id  # type: ignore[assignment]
            h = self.project.site.building_height if self.project.site else 24.0
            self.project.plates = []
            for fp in opt.plates:
                # default shape by rule: best rated template whose flats ratio is reasonable
                best = max(all_shapes(fp, h), key=lambda s: (sum(s["ratings"].values()) + 10 * s["efficiency"]))
                self.project.plates.append(fit(best["shape"], fp, h))
                self._decision(5, f"shape {fp.id}")
            self.project.units, self.project.diagrams, self.project.plans, self.project.checks = [], [], [], None
            for plate in self.project.plates:
                self._resplit(plate)
            self.project.stage = max(self.project.stage, 5)
            self._save("choose_placement")
            return self.summary()

    # ================================================================= stage 5
    def _plate(self, tower_id: str) -> Plate:
        for p in self.project.plates:
            if p.tower_id == tower_id:
                return p
        raise PlannerError(f"No tower {tower_id}", "not_found")

    def _footprint(self, tower_id: str):
        pl = self.project.placement
        if not pl or not pl.chosen:
            return None
        opt = next(o for o in pl.options if o.id == pl.chosen)
        return next((f for f in opt.plates if f.id == tower_id), None)

    def _envelope(self, tower_id: str, shape: ShapeKind):
        """Footprint a template is fitted to. Rectangles use the placement plate; a triangle gets the largest
        triangle that fits the buildable area, clear of the other towers by the gap rule."""
        fp = self._footprint(tower_id)
        if shape != ShapeKind.triangle or fp is None or not self.project.site:
            return fp
        cache = getattr(self, "_tri_cache", {})
        key = (tower_id, self.project.placement.chosen if self.project.placement else None, self.project.site.area)
        if key in cache:
            return cache[key]
        from .placement import fit_triangle
        from .site import buildable_geom

        gap, _ = rules.tower_gap_for(self.project.site.building_height)
        opt = next(o for o in self.project.placement.options if o.id == self.project.placement.chosen)
        others = [Polygon(f.polygon).buffer(gap) for f in opt.plates if f.id != tower_id]
        obstacles = shapely.union_all(others) if others else None
        tri = fit_triangle(buildable_geom(self.project.site), obstacles)
        if tri is not None:
            tri.id = tower_id
        cache[key] = tri or fp
        self._tri_cache = cache
        return cache[key]

    def list_shapes(self, tower_id: str = "T1") -> list[dict]:
        fp = self._footprint(tower_id)
        if fp is None:
            raise PlannerError("Choose a placement first", "no_placement")
        h = self.project.site.building_height if self.project.site else 24.0
        out = all_shapes(fp, h)
        tri = self._envelope(tower_id, ShapeKind.triangle)
        if tri is not fp:
            from .plate import all_shapes as _all

            t = next(x for x in _all(tri, h) if x["shape"] == "triangle")
            out = [t if x["shape"] == "triangle" else x for x in out]
        return out

    def set_plate(self, tower_id: str = "T1", shape: str | None = None, params: dict | None = None) -> dict:
        """Build or edit a plate from template parameters (drag handles send new params here)."""
        with self._lock:
            cur = self._plate(tower_id)
            shape_k = ShapeKind(shape) if shape else cur.shape
            fp = self._envelope(tower_id, shape_k)
            env = (fp.width, fp.depth) if fp else (cur.params["W"], cur.params["D"])
            angle = fp.angle_deg if fp else cur.angle_deg
            origin = fp.center if fp else cur.origin
            base = dict(cur.params) if shape_k == cur.shape else {}
            base.update({k: float(v) for k, v in (params or {}).items()})
            h = self.project.site.building_height if self.project.site else 24.0
            plate = build_plate(shape_k, base, tower_id=tower_id, angle_deg=angle, origin=origin, env=env, height=h)
            idx = self.project.plates.index(cur)
            self.project.plates[idx] = plate
            keep_units = shape_k == cur.shape and int(plate.params["units"]) == int(cur.params.get("units", 0))
            self._resplit(plate, keep_programmes=keep_units)
            self._save("set_plate")
            return {"plate": plate.model_dump(mode="json"), "units": [u.model_dump(mode="json") for u in self._units(tower_id)]}

    def preview_plate(self, tower_id: str, shape: str, params: dict) -> dict:
        """Zones for a drag in progress (no save)."""
        cur = self._plate(tower_id)
        fp = self._envelope(tower_id, ShapeKind(shape))
        env = (fp.width, fp.depth) if fp else (cur.params["W"], cur.params["D"])
        base = dict(cur.params) if ShapeKind(shape) == cur.shape else {}
        base.update({k: float(v) for k, v in params.items()})
        plate = build_plate(shape, base, tower_id=tower_id, angle_deg=fp.angle_deg if fp else cur.angle_deg,
                            origin=fp.center if fp else cur.origin, env=env)
        return plate.model_dump(mode="json")

    def import_plate_dxf(self, tower_id: str, path: str) -> dict:
        """Use a DXF of the core and unit footprint (layers mapped like stage 8)."""
        res = import_dxf(path)
        if not res.outline:
            raise PlannerError("No outline found in the drawing", "bad_drawing")
        cur = self._plate(tower_id)
        outline = Polygon(res.outline)
        c = outline.centroid
        ring = [(round(x - c.x, 3), round(y - c.y, 3)) for x, y in geo.ring_of(outline)]
        plate = cur.model_copy(deep=True)
        plate.outline = ring
        plate.corners = len(ring)
        plate.area = round(outline.area, 2)
        plate.imported_from = Path(path).name
        idx = self.project.plates.index(cur)
        self.project.plates[idx] = plate
        self._save("import_plate")
        return plate.model_dump(mode="json")

    # ================================================================= stage 6
    def _units(self, tower_id: str) -> list[Unit]:
        return [u for u in self.project.units if _tower_of(u.id) == tower_id]

    def _resplit(self, plate: Plate, n: int | None = None, keep_programmes: bool = False) -> list[Unit]:
        prefix = "" if plate.tower_id == "T1" else f"{plate.tower_id}-"
        old = self._units(plate.tower_id)
        old_local = [u.model_copy(update={"id": u.id[len(prefix):]}) for u in old] if keep_programmes else None
        units = split_units(plate, n, keep_programmes=old_local)
        for u in units:
            u.id = prefix + u.id
            self._decision(6, f"split {u.id}")
            self._decision(6, f"programme {u.id}")
        old_ids = {u.id for u in old}
        self.project.units = [u for u in self.project.units if u.id not in old_ids] + units
        self.project.diagrams = [d for d in self.project.diagrams if d.unit_id not in old_ids]
        self.project.plans = [p for p in self.project.plans if p.unit_id not in old_ids]
        self.project.checks = None
        for uid in old_ids:
            self._drop_tickets(f"T3:{uid}")
            self._drop_tickets(f"T8:{uid}")
        self.project.stage = max(self.project.stage, 6)
        return units

    def split_units(self, tower_id: str = "T1", count: int | None = None) -> list[dict]:
        with self._lock:
            plate = self._plate(tower_id)
            if count:
                plate.params["units"] = float(count)
            units = self._resplit(plate, count)
            self._save("split_units")
            return [u.model_dump(mode="json") for u in units]

    def _unit(self, unit_id: str) -> Unit:
        for u in self.project.units:
            if u.id == unit_id:
                return u
        raise PlannerError(f"No unit {unit_id}", "not_found")

    def _plate_of(self, unit_id: str) -> Plate:
        return self._plate(_tower_of(unit_id))

    def list_presets(self) -> list[dict]:
        return [p.model_dump(mode="json") for p in presets().values()]

    def set_programme(self, unit_id: str, preset: str) -> dict:
        with self._lock:
            u = self._unit(unit_id)
            if preset not in presets():
                raise PlannerError(f"No preset {preset}", "not_found")
            u.programme = presets()[preset]
            self.project.diagrams = [d for d in self.project.diagrams if d.unit_id != unit_id]
            self.project.plans = [p for p in self.project.plans if p.unit_id != unit_id]
            self._save("set_programme")
            return u.model_dump(mode="json")

    def propose_programme(self, unit_id: str, brief: str, rooms: list[dict] | None = None,
                          links: list[list[str]] | None = None, entry: str | None = None, by: str = "claude") -> dict:
        """T8: brief in words -> room list. Without ``rooms`` the safe default (a preset) is used."""
        with self._lock:
            u = self._unit(unit_id)
            default = u.programme or presets()["2bhk"]
            t = self._open_ticket(TicketKind.T8, 6, unit_id, f"Turn this brief into a room list: {brief!r}",
                                  [{"id": "preset", "label": default.name}], "preset",
                                  [f"flat {u.area} m²", f"façade {facade_length(self._plate_of(unit_id), u)} m"],
                                  free_text=True)
            if rooms is None:
                return {"ticket": t.model_dump(mode="json"), "applied": "preset"}
            return self.answer_decision(t.id, option_id=None, payload={"rooms": rooms, "links": links or [],
                                                                         "entry": entry, "name": brief[:60]},
                                        reason=brief, by=by)

    # ================================================================= stage 7
    def get_diagram(self, unit_id: str) -> dict:
        d = self._diagram(unit_id)
        return d.model_dump(mode="json")

    def _diagram(self, unit_id: str) -> Diagram:
        for d in self.project.diagrams:
            if d.unit_id == unit_id:
                return d
        u = self._unit(unit_id)
        if not u.programme:
            raise PlannerError("Pick a room list first", "no_programme")
        d = from_programme(u, u.programme)
        self.project.diagrams.append(d)
        return d

    def update_diagram(self, unit_id: str, rooms: list[dict] | None = None, links: list[list[str]] | None = None,
                       entry: str | None = None) -> dict:
        with self._lock:
            d = self._diagram(unit_id)
            if rooms is not None:
                d.rooms = [DiagramRoom(**r) for r in rooms]
            if links is not None:
                ids = {r.id for r in d.rooms}
                bad = [l for l in links if l[0] not in ids or l[1] not in ids]
                if bad:
                    raise PlannerError(f"Links name unknown rooms: {bad}")
                d.links = [(a, b) for a, b in links]
            if entry is not None:
                d.entry = entry
            res = self.precheck(unit_id)
            self._save("update_diagram")
            return res

    def precheck(self, unit_id: str) -> dict:
        d = self._diagram(unit_id)
        u = self._unit(unit_id)
        plate = self._plate_of(unit_id)
        conflicts = precheck(d, u.area, facade_length(plate, u), 0.0, u.core_link is not None)
        d.conflicts = conflicts
        d.feasible = None if not conflicts else False
        d.message = "This diagram can become a plan." if not conflicts else conflicts[0].reason
        placed = sum(1 for r in d.rooms if r.placed)
        return {"unit_id": unit_id, "rooms": len(d.rooms), "placed": placed,
                "conflicts": [c.model_dump(mode="json") for c in conflicts], "message": d.message}

    def solve_unit(self, unit_id: str, time_limit: float = 7.5) -> dict:
        with self._lock:
            u = self._unit(unit_id)
            plate = self._plate_of(unit_id)
            d = self._diagram(unit_id)
        fr = solve_flat(plate, u, d, d.overrides, time_limit=time_limit)
        with self._lock:
            return self._store_flat(u, plate, d, fr)

    def _store_flat(self, u: Unit, plate: Plate, d: Diagram, fr) -> dict:
        newd = fr.diagram
        newd.unit_id = u.id
        # carry the overrides the fix produced
        if fr.applied_fix:
            steps = fr.applied_fix["steps"] if fr.applied_fix.get("action") == "multi" else [fr.applied_fix]
            dd, ov, uu = d, d.overrides, u
            for st in steps:
                dd, ov, uu = apply_fix(st, dd, ov, uu)
            if uu is not u and uu.programme:
                u.programme = uu.programme
            newd.overrides = ov
        self.project.diagrams = [x for x in self.project.diagrams if x.unit_id != u.id] + [newd]
        self.project.plans = [p for p in self.project.plans if p.unit_id != u.id]
        self._drop_tickets(f"T3:{u.id}")
        if fr.plan:
            fr.plan.unit_id = u.id
            for r in fr.plan.rooms:
                r.unit_id = u.id
            self.project.plans.append(fr.plan)
            self._decision(7, f"layout {u.id}")
        if fr.applied_fix or (fr.result.dropped and fr.plan):
            opts = []
            if fr.applied_fix:
                opts.append({"id": "applied", "label": fr.applied_fix["label"], "payload": fr.applied_fix})
            for f in fr.fixes:
                if f.get("valid") and f.get("label") != (fr.applied_fix or {}).get("label"):
                    opts.append({"id": f"fix{len(opts)}", "label": f["label"], "payload": f})
            for rid in fr.result.dropped:
                name = next((r.name for r in d.rooms if r.id == rid), rid)
                opts.append({"id": f"keep:{rid}", "label": f"Keep {name} (shrink others)",
                             "payload": {"action": "keep_room", "room": rid}})
            opts = opts[:4]
            if opts:
                q = (fr.conflicts[-1].reason + " Pick a fix.") if fr.conflicts else \
                    f"{', '.join(fr.result.dropped)} did not fit. Keep it and shrink others?"
                self._open_ticket(TicketKind.T3, 7, u.id, q, opts, opts[0]["id"],
                                  [c.reason for c in fr.conflicts][:3] or [f"dropped {fr.result.dropped}"])
        self.project.checks = None
        self.project.stage = max(self.project.stage, 7)
        self._save(f"solve {u.id}")
        return {"unit_id": u.id, "status": fr.result.status, "plan": fr.plan.model_dump(mode="json") if fr.plan else None,
                "message": fr.message, "applied_fix": fr.applied_fix, "time_s": fr.plan.solve_time_s if fr.plan else fr.result.time_s,
                "conflicts": [c.model_dump(mode="json") for c in fr.conflicts]}

    def solve_all(self, tower_id: str | None = None, parallel: int = 3) -> dict:
        ids = [u.id for u in self.project.units if tower_id is None or u in self._units(tower_id)]
        t0 = time.perf_counter()
        jobs = []
        with self._lock:
            for uid in ids:
                u = self._unit(uid)
                jobs.append((u, self._plate_of(uid), self._diagram(uid)))
        results = []

        def run(job):
            u, plate, d = job
            fr = solve_flat(plate, u, d, d.overrides)
            self._emit("solved", unit_id=u.id, ok=fr.plan is not None)
            return u, plate, d, fr

        with ThreadPoolExecutor(max_workers=max(1, parallel)) as ex:
            for u, plate, d, fr in ex.map(run, jobs):
                with self._lock:
                    results.append(self._store_flat(u, plate, d, fr))
        checks = self.run_checks() if any(r["plan"] for r in results) else None
        return {"solved": sum(1 for r in results if r["plan"]), "total": len(results),
                "time_s": round(time.perf_counter() - t0, 2),
                "units": [{k: r[k] for k in ("unit_id", "status", "message", "applied_fix", "time_s")} for r in results],
                "checks": checks["counts"] if checks else None}

    def get_plan(self, unit_id: str) -> dict:
        for p in self.project.plans:
            if p.unit_id == unit_id:
                return p.model_dump(mode="json")
        raise PlannerError(f"No plan for {unit_id} yet", "not_found")

    def apply_edit(self, unit_id: str, edit: dict, reason: str = "", by: str = "claude") -> dict:
        """T7: an edit in words, already turned into a constraint change by Claude. Engine checks, re-solves, diffs.

        Supported: {"action": "link"|"unlink", "a", "b"}, {"action": "resize", "room", "target_area"},
        {"action": "drop"|"keep", "room"}, {"action": "window", "room", "needs": bool},
        {"action": "add_room", "room": {...DiagramRoom}, "link_to": id}, {"action": "open_plan", "a", "b"}.
        """
        with self._lock:
            d = self._diagram(unit_id)
            before = next((p for p in self.project.plans if p.unit_id == unit_id), None)
            nd = copy.deepcopy(d)
            ids = {r.id for r in nd.rooms}
            a = edit.get("action")

            def need(*keys):
                for k in keys:
                    if k not in edit:
                        raise PlannerError(f"edit needs {k!r}")

            if a in ("link", "open_plan"):
                need("a", "b")
                if edit["a"] not in ids or edit["b"] not in ids:
                    raise PlannerError("unknown room in edit")
                if not any(set(l) == {edit["a"], edit["b"]} for l in nd.links):
                    nd.links.append((edit["a"], edit["b"]))
            elif a == "unlink":
                need("a", "b")
                nd.links = [l for l in nd.links if set(l) != {edit["a"], edit["b"]}]
            elif a == "resize":
                need("room", "target_area")
                if edit["room"] not in ids:
                    raise PlannerError("unknown room")
                ta = float(edit["target_area"])
                if not 0.5 <= ta <= 80:
                    raise PlannerError("target_area out of range")
                nd.overrides.setdefault(edit["room"], {})["target_area"] = ta
            elif a in ("drop", "keep"):
                need("room")
                nd.overrides.setdefault(edit["room"], {})["force"] = a == "keep"
            elif a == "window":
                need("room", "needs")
                nd.overrides.setdefault(edit["room"], {})["needs_window"] = bool(edit["needs"])
            elif a == "add_room":
                need("room")
                r = DiagramRoom(**edit["room"])
                if r.id in ids or r.type not in rules.ROOM_TYPES:
                    raise PlannerError("room id exists or bad type")
                nd.rooms.append(r)
                if edit.get("link_to") in ids:
                    nd.links.append((edit["link_to"], r.id))
            else:
                raise PlannerError(f"unsupported edit {a!r}")
            u = self._unit(unit_id)
            plate = self._plate_of(unit_id)
        fr = solve_flat(plate, u, nd, nd.overrides, auto_fix=False)
        with self._lock:
            tid = f"T7:{unit_id}:{uuid.uuid4().hex[:6]}"
            t = Ticket(id=tid, kind=TicketKind.T7, stage=7, subject=unit_id, question=f"Edit: {reason or edit}",
                       options=[TicketOption(id="edit", label=str(edit), payload=edit)], default_option=None,
                       status="answered" if fr.plan else "undone", check_me=False, applied_option="edit" if fr.plan else None,
                       answer=TicketAnswer(option_id="edit", reason=reason, by=Actor(by), ts=_now()), created=_now())
            self._put_ticket(t)
            self._decision(7, f"edit {unit_id}", by=by)
            if not fr.plan:
                return {"ok": False, "message": "The edit breaks the rules: " + fr.message, "ticket": tid}
            res = self._store_flat(u, plate, nd, fr)
            res["diff"] = _plan_diff(before, fr.plan)
            res["ticket"] = tid
            return res

    # ================================================================= stage 8
    def run_checks(self) -> dict:
        with self._lock:
            pr = self.project
            h = pr.site.building_height if pr.site else 24.0
            results = []
            for plate in pr.plates:
                plans = [p for p in pr.plans if p.unit_id in {u.id for u in self._units(plate.tower_id)}]
                if not plans:
                    continue
                stairs = [(c.polygon, c.width) for c in plate.core_parts if c.kind == "stair"]
                shafts = [list(map(tuple, ring[0])) for z in plate.zones if z.kind == "shaft" for ring in z.polygon]
                ctx = FloorContext(plans=plans, height=h, passage_width=plate.passage_width, stairs=stairs,
                                   shafts=shafts)
                results.append(run_checks(ctx))
            imported = [p for p in pr.plans if p.status == "imported"]
            if imported and pr.drawing and not results:
                ov = pr.drawing_overrides
                res = import_dxf(pr.drawing_path, ov.get("layers"), ov.get("units")) if pr.drawing_path else None
                if res:
                    results.append(run_checks(FloorContext(plans=imported, height=h, passage_width=res.passage_width,
                                                           stairs=res.stairs, handles=res.handles)))
            if not results:
                raise PlannerError("Nothing to check yet - solve a flat or import a drawing", "nothing")
            merged = _merge_summaries(results)
            pr.checks = merged
            self._drop_tickets("T6")
            for r in merged.results:
                self._decision(8, f"check {r.rule_id}")
                if r.result == "review":
                    clause = next((c for c in rules.clauses() if c.id == r.rule_id), None)
                    self._open_ticket(TicketKind.T6, 8, f"{r.rule_id}@{r.target}",
                                      f"Explain this review item in plain words: {r.title} ({r.target}).",
                                      [{"id": "clause", "label": f"{r.clause}: {clause.title if clause else r.title}. {r.message}"}],
                                      "clause", [f"value {r.value}", f"threshold {r.threshold}"], free_text=True)
            pr.stage = 8
            self._save("run_checks")
            return merged.model_dump(mode="json")

    def explain_check(self, rule_id: str, target: str | None = None) -> dict:
        pr = self.project
        if not pr.checks:
            raise PlannerError("Run the checks first", "nothing")
        hits = [r for r in pr.checks.results if r.rule_id == rule_id and (target is None or r.target == target)]
        if not hits:
            raise PlannerError(f"No result for {rule_id} {target or ''}", "not_found")
        clause = next((c for c in rules.clauses() if c.id == rule_id), None)
        r = hits[0]
        return {"rule_id": rule_id, "clause": r.clause, "source": clause.source if clause else r.source,
                "title": r.title, "target": r.target, "result": r.result, "value": r.value, "threshold": r.threshold,
                "unit": r.unit, "message": r.message, "proof": r.proof, "bbox": r.bbox,
                "explanation": pr.explanations.get(f"{rule_id}@{r.target}"),
                "verified_by_consultant": bool(clause.verified) if clause else False,
                "others": len(hits) - 1, "disclaimer": pr.checks.disclaimer}

    def export_dxf(self, path: str, tower_id: str = "T1") -> dict:
        plate = self._plate(tower_id)
        plans = [p for p in self.project.plans if p.unit_id in {u.id for u in self._units(tower_id)}]
        counts = export_dxf(path, plate, plans, project=self.project.name,
                            site_note=f"{self.project.site.name if self.project.site else ''}")
        self._evidence(f"export.{tower_id}", path, EvidenceKind.derived, "ezdxf R2007", stage=8)
        return {"path": path, "entities": sum(counts.values()), "layers": counts}

    def import_drawing(self, path: str) -> dict:
        with self._lock:
            ov = self.project.drawing_overrides
            res = import_dxf(path, ov.get("layers"), ov.get("units"))
            pr = self.project
            # T4 answers: room type overrides by space id
            for pl in res.plans:
                for r in pl.rooms:
                    t = ov.get("rooms", {}).get(r.id)
                    if t:
                        r.type, r.name = t, t.title()
            pr.drawing = res.report
            pr.drawing_path = str(path)
            pr.plans = [p for p in pr.plans if p.status != "imported"] + res.plans
            for prefix in ("T1", "T2", "T4"):
                self._drop_tickets(prefix)
            for t in res.tickets:
                kind = TicketKind(t["kind"])
                self._open_ticket(kind, 8, t["subject"], t["question"], t["options"], t["default"], t["evidence"])
            for lm in res.report.layers:
                self._decision(8, f"layer {lm.layer}", by="engine" if lm.method in ("exact", "fuzzy", "user") else "engine-default")
            h = pr.site.building_height if pr.site else 24.0
            checks = run_checks(FloorContext(plans=res.plans, height=h, passage_width=res.passage_width,
                                             stairs=res.stairs, handles=res.handles))
            pr.checks = checks
            pr.stage = 8
            self._save("import_drawing")
            return {"report": res.report.model_dump(mode="json"), "checks": checks.model_dump(mode="json")["counts"],
                    "by_code": checks.by_code, "tickets": len(res.tickets)}

    # ================================================================= decisions (tickets)
    def list_decisions(self, status: str = "waiting", kind: str | None = None) -> list[dict]:
        out = []
        for t in self._tickets():
            if status == "waiting" and t.status not in ("open", "defaulted"):
                continue
            if status not in ("waiting", "all") and t.status != status:
                continue
            if kind and t.kind.value != kind:
                continue
            d = t.model_dump(mode="json")
            if len(d["options"]) > 8:  # keep answers small
                d["options"] = d["options"][:8] + [{"id": "...", "label": f"{len(t.options) - 8} more"}]
            out.append(d)
        return out

    def answer_decision(self, ticket_id: str, option_id: str | None = None, text: str | None = None,
                        reason: str = "", by: str = "claude", payload: dict | None = None) -> dict:
        """Apply an answer. Everything is re-checked; bad answers are rejected and never reach the plan."""
        with self._lock:
            t = self._get_ticket(ticket_id)
            if option_id is not None and option_id not in {o.id for o in t.options} and not (
                t.kind == TicketKind.T1 and option_id in _meanings()
            ):
                raise PlannerError(f"{option_id!r} is not an option for {ticket_id}", "invalid_answer")
            if option_id is None and text is None and payload is None:
                raise PlannerError("Give an option_id, text or payload", "invalid_answer")
            result = self._apply_ticket(t, option_id, text, payload)
            t.answer = TicketAnswer(option_id=option_id, text=text, reason=reason[:500], by=Actor(by), ts=_now())
            t.applied_option = option_id or "custom"
            t.status = "answered"
            t.check_me = by != "user"
            self._put_ticket(t)
            self._decision(t.stage, f"answer {t.id}", by=by)
            self._evidence(f"ticket.{t.id}", option_id or text or "custom",
                           EvidenceKind.inferred if by == "claude" else EvidenceKind.observed, reason[:200], Actor(by), t.stage)
            self._save(f"answer {t.id}")
            return {"ticket": t.id, "status": t.status, "applied": t.applied_option, "result": result}

    def review_decision(self, ticket_id: str, keep: bool) -> dict:
        """User taps Keep or Undo on an answer."""
        with self._lock:
            t = self._get_ticket(ticket_id)
            if keep:
                t.status = "kept"
                t.check_me = False
                if t.kind == TicketKind.T1 and t.applied_option and t.applied_option != "unmapped":
                    learn_layer(t.subject, t.applied_option)
                if t.answer:
                    t.answer.accepted = True
                result: Any = None
            else:
                result = self._apply_ticket(t, t.default_option, None, None) if t.default_option else None
                t.status = "undone"
                t.applied_option = t.default_option
                if t.answer:
                    t.answer.accepted = False
            self._put_ticket(t)
            self._decision(t.stage, f"review {t.id}", by="user")
            self._save(f"review {t.id}")
            return {"ticket": t.id, "status": t.status, "result": result}

    def _apply_ticket(self, t: Ticket, option_id: str | None, text: str | None, payload: dict | None) -> Any:
        pr = self.project
        k = t.kind
        if k == TicketKind.T0:
            site = self._need_site()
            if site.climate:
                setattr(site.climate, t.subject, option_id or "unknown")
            return {t.subject: option_id}
        if k == TicketKind.T5:
            if not pr.placement:
                raise PlannerError("No placement")
            opt = next(o for o in pr.placement.options if o.id == t.subject)
            if option_id == "template" or (text is None and option_id):
                opt.drivers = drivers_note(opt, {})
            else:
                note = (text or "").strip()
                _check_note(note, opt)
                opt.drivers = note
            return {"drivers": opt.drivers}
        if k == TicketKind.T6:
            note = text or next((o.label for o in t.options if o.id == option_id), "")
            if len(note) > 1200:
                raise PlannerError("Explanation too long", "invalid_answer")
            pr.explanations[t.subject] = note
            return {"explanation": note}
        if k == TicketKind.T8:
            unit_id = t.subject
            u = self._unit(unit_id)
            if option_id == "preset" or payload is None:
                return {"programme": u.programme.id if u.programme else None}
            try:
                prog = programme_from_rooms(payload.get("name") or "Custom", payload["rooms"], payload.get("links") or [],
                                            payload.get("entry"))
            except (ValueError, KeyError, TypeError) as e:
                raise PlannerError(f"Room list rejected: {e}", "invalid_answer") from e
            # rules check: the statutory pre-check must pass on this flat
            probe = from_programme(u, prog)
            conflicts = precheck(probe, u.area, facade_length(self._plate_of(unit_id), u), 0.0, u.core_link is not None)
            hard = [c for c in conflicts if c.kind in ("area", "link") and c.id != "area-target"]
            usable = u.area * 0.9
            total = sum(r.target_area for r in prog.rooms)
            if any(r.target_area > usable for r in prog.rooms) or total > usable * 1.4:
                raise PlannerError(f"Room list rejected: targets add to {total:.1f} m² for a {u.area:.1f} m² flat",
                                   "invalid_answer")
            if hard:
                raise PlannerError("Room list rejected by the rules: " + hard[0].reason, "invalid_answer")
            u.programme = prog
            pr.diagrams = [d for d in pr.diagrams if d.unit_id != unit_id]
            pr.plans = [p for p in pr.plans if p.unit_id != unit_id]
            return {"programme": prog.model_dump(mode="json"), "warnings": [c.reason for c in conflicts]}
        if k == TicketKind.T3:
            unit_id = t.subject
            opt = next((o for o in t.options if o.id == option_id), None)
            if opt is None:
                raise PlannerError("unknown option", "invalid_answer")
            fix = opt.payload
            u = self._unit(unit_id)
            plate = self._plate_of(unit_id)
            d = self._diagram(unit_id)
            if fix.get("action") == "keep_room":
                d2, ov2, u2 = copy.deepcopy(d), copy.deepcopy(d.overrides), u
                ov2.setdefault(fix["room"], {})["force"] = True
            elif fix.get("action") == "multi":
                d2, ov2, u2 = d, d.overrides, u
                for step in fix["steps"]:
                    d2, ov2, u2 = apply_fix(step, d2, ov2, u2)
            else:
                d2, ov2, u2 = apply_fix(fix, d, d.overrides, u)
            fr = solve_flat(plate, u2, d2, ov2, auto_fix=False)
            if not fr.plan:
                raise PlannerError("That fix does not produce a valid plan: " + fr.message, "invalid_answer")
            fr.diagram.overrides = ov2
            if u2 is not u:
                u.programme = u2.programme
            fr.applied_fix = None
            self._store_flat(u, plate, fr.diagram, fr)
            return {"unit_id": unit_id, "status": fr.result.status}
        if k in (TicketKind.T1, TicketKind.T2, TicketKind.T4):
            ov = pr.drawing_overrides
            if k == TicketKind.T1:
                if option_id not in _meanings():
                    raise PlannerError("unknown layer meaning", "invalid_answer")
                ov.setdefault("layers", {})[t.subject] = option_id
            elif k == TicketKind.T2:
                ov["units"] = option_id
            else:
                if option_id not in rules.ROOM_TYPES:
                    raise PlannerError("unknown room type", "invalid_answer")
                ov.setdefault("rooms", {})[t.subject] = option_id
            if pr.drawing_path:
                keep = self._tickets()
                res = self.import_drawing(pr.drawing_path)
                # keep this ticket's state after the re-import
                for old in keep:
                    if old.id == t.id:
                        self._put_ticket(old)
                return res
            return {}
        raise PlannerError(f"Tickets of kind {k.value} cannot be answered here", "invalid_answer")

    def decision_stats(self) -> dict:
        ev = self.store.events("decision", limit=100000) if self.store else []
        total = len(ev)
        claude = sum(1 for e in ev if e.get("by") == "claude")
        user = sum(1 for e in ev if e.get("by") == "user")
        defaults = sum(1 for e in ev if e.get("by") == "engine-default")
        rules_n = total - claude - user - defaults
        return {"total": total, "rules": rules_n, "defaults": defaults, "claude": claude, "user": user,
                "rules_share": round((rules_n + defaults) / total, 4) if total else 1.0,
                "ticket_share": round(defaults / total, 4) if total else 0.0}

    # ================================================================= compare
    def compare_options(self, kind: str = "placement") -> list[dict]:
        pr = self.project
        if kind == "placement":
            if not pr.placement:
                raise PlannerError("Place towers first", "no_placement")
            return [{"id": o.id, "towers": o.towers, "coverage": o.coverage, "total_m2": o.total_area, "gap_m": o.min_gap,
                     "score": o.score, **{f"t_{k}": v for k, v in o.score_terms.items()}, "passes": o.passes}
                    for o in pr.placement.options]
        if kind == "plans":
            out = []
            for p in pr.plans:
                out.append({"unit_id": p.unit_id, "carpet_m2": p.carpet_area, "rooms": len(p.rooms),
                            "bhk": sum(1 for r in p.rooms if r.type in ("master", "bedroom")),
                            "status": p.status, "solve_s": p.solve_time_s})
            return out
        if kind == "shapes":
            return self.list_shapes(pr.plates[0].tower_id if pr.plates else "T1")
        raise PlannerError("kind must be placement, plans or shapes")

    # ================================================================= map geometry
    def geojson(self) -> dict:
        """Everything drawable on the map, in lng/lat."""
        pr = self.project
        feats: list[dict] = []
        if not pr.site:
            return {"type": "FeatureCollection", "features": feats}
        frame = pr.site.frame

        def ll(ring):
            r = geo.to_lnglat(frame, ring)
            return [list(p) for p in r] + [list(r[0])]

        def add(kind, ring, **props):
            feats.append({"type": "Feature", "properties": {"kind": kind, **props},
                          "geometry": {"type": "Polygon", "coordinates": [ll(ring)]}})

        add("site", pr.site.polygon, area=pr.site.area)
        for b in pr.site.buildable:
            add("buildable", b, area=pr.site.buildable_area)
        if pr.placement:
            for o in pr.placement.options:
                for p in o.plates:
                    add("option", p.polygon, option=o.id, tower=p.id, chosen=o.id == pr.placement.chosen)
        for plate in pr.plates:
            tf = lambda ring, pl=plate: geo.plate_to_site(ring, pl.angle_deg, pl.origin)  # noqa: E731
            add("plate", tf(plate.outline), tower=plate.tower_id, shape=plate.shape.value)
            for z in plate.zones:
                for part in z.polygon:
                    add(f"zone-{z.kind}", tf(part[0]), tower=plate.tower_id)
            for u in self._units(plate.tower_id):
                add("unit", tf(u.polygon), unit=u.id, tower=plate.tower_id)
            for p in pr.plans:
                if p.unit_id in {u.id for u in self._units(plate.tower_id)}:
                    for r in p.rooms:
                        add("room", tf(r.polygon), unit=p.unit_id, room=r.id, type=r.type, name=r.name, area=r.area)
        return {"type": "FeatureCollection", "features": feats}

    # ================================================================= bridge
    def bridge_hello(self, client: str = "claude-desktop", version: str = "") -> dict:
        self.bridge = {"linked": True, "client": client, "version": version, "last_seen": _now()}
        if self.store:
            self.store.event("bridge", {"client": client, "version": version})
        self._emit("bridge", **self.bridge)
        return {"ok": True, "project": self.project.name, "decisions_waiting": self.summary()["decisions_waiting"]}

    # ================================================================= Claude link (5 steps) + doctor
    def claude_status(self) -> dict:
        from . import claude_link

        st = claude_link.status()
        st["handshake"] = self.bridge_status()
        return st

    def claude_plan_link(self) -> dict:
        from . import claude_link

        plan = claude_link.plan_link()
        plan.pop("after", None)
        return plan

    def claude_link(self, confirm: bool = False) -> dict:
        from . import claude_link

        res = claude_link.link(confirm=confirm)
        res.pop("after", None)
        if self.store and res.get("written"):
            self.store.event("claude_link", {"path": res["path"], "backup": res.get("backup")})
        return res

    def get_settings(self) -> dict:
        from . import settings

        return settings.public()

    def set_settings(self, mapbox_token: str | None = None, crash_reports: bool | None = None,
                     map_provider: str | None = None) -> dict:
        from . import settings

        changes = {k: v for k, v in {"mapbox_token": mapbox_token, "crash_reports": crash_reports,
                                      "map_provider": map_provider}.items() if v is not None}
        try:
            return settings.save(**changes)
        except ValueError as e:
            raise PlannerError(str(e), "bad_setting") from e

    def doctor(self) -> dict:
        from .doctor import run_doctor

        return run_doctor(False)

    def bridge_status(self) -> dict:
        b = dict(self.bridge)
        b["fresh"] = bool(b.get("linked")) and _now() - float(b.get("last_seen", 0)) < 600
        return b


# ================================================================= helpers


def _tower_of(unit_id: str) -> str:
    m = re.match(r"^(T\d+)-", unit_id)
    return m.group(1) if m else "T1"


def _meanings() -> set[str]:
    from .dxf_io import MEANINGS

    return set(MEANINGS)


def _check_note(note: str, opt) -> None:
    """T5 answers may only state numbers the engine computed (no invented facts)."""
    if not note or len(note) > 280:
        raise PlannerError("The note must be 1-280 characters", "invalid_answer")
    allowed: set[str] = set()
    for v in (opt.coverage * 100, opt.total_area, opt.min_gap or 0, opt.towers, *(p.area for p in opt.plates),
              *(p.width for p in opt.plates), *(p.depth for p in opt.plates)):
        for fmt in ("{:.0f}", "{:.1f}", "{:.2f}"):
            allowed.add(fmt.format(v))
    allowed |= {str(i) for i in range(0, 13)} | {"16", "21", "22", "180", "360"}
    for num in re.findall(r"\d+(?:\.\d+)?", note):
        if num not in allowed:
            raise PlannerError(f"The note states {num}, which the engine did not compute", "invalid_answer")


def _merge_summaries(items: list[CheckSummary]) -> CheckSummary:
    results = [r for s in items for r in s.results]
    counts = {k: sum(1 for r in results if r.result == k) for k in ("pass", "fail", "review", "n/a")}
    by_code: dict[str, dict[str, int]] = {}
    for code in sorted({r.source for r in results}):
        ids = sorted({r.rule_id for r in results if r.source == code})
        roll = {"clauses": len(ids), "pass": 0, "fail": 0, "review": 0, "n/a": 0}
        for rid in ids:
            res = {r.result for r in results if r.rule_id == rid}
            k = "fail" if "fail" in res else "review" if "review" in res else "pass" if "pass" in res else "n/a"
            roll[k] += 1
        by_code[code] = roll
    return CheckSummary(results=results, counts=counts, by_code=by_code, disclaimer=items[0].disclaimer)


def _plan_diff(before: Plan | None, after: Plan) -> dict:
    if before is None:
        return {"added": [r.id for r in after.rooms], "removed": [], "changed": []}
    b = {r.id: r for r in before.rooms}
    a = {r.id: r for r in after.rooms}
    changed = []
    for rid in set(a) & set(b):
        if abs(a[rid].area - b[rid].area) > 0.05:
            changed.append({"room": rid, "from_m2": b[rid].area, "to_m2": a[rid].area})
    return {"added": sorted(set(a) - set(b)), "removed": sorted(set(b) - set(a)), "changed": changed,
            "carpet_from": before.carpet_area, "carpet_to": after.carpet_area}


_ = (LABELS, RATINGS, Placement, Programme, shapely)
