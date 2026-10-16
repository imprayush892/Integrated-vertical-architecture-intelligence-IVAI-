from planner_engine.checks import FloorContext, run_checks
from planner_engine.dxf_io import export_dxf, import_dxf, map_layer, roundtrip_check
from planner_engine.golden import gate_rules
from planner_engine.layout import solve_flat
from planner_engine.plate import fit
from planner_engine.units import split_units


def test_layer_mapping():
    assert map_layer("A-WALL")[:2] == ("wall", "exact")
    assert map_layer("WALL hatch SPA")[0] == "wall"
    assert map_layer("DR-SWING")[0] == "door"
    assert map_layer("WINDOWS-EXT")[0] == "window"
    assert map_layer("xyz-123")[0] == "unmapped"


def test_reference_drawings_match_expected():
    r = gate_rules()
    assert r["ok"], r["mismatches"]
    assert r["compared"] == 110


def test_export_roundtrip_and_reimport(tmp_path, footprint):
    p = fit("central_core", footprint(34, 24))
    plans = [fr.plan for u in split_units(p)[:2] if (fr := solve_flat(p, u)).plan]
    export_dxf(tmp_path / "f.dxf", p, plans)
    rt = roundtrip_check(tmp_path / "f.dxf", tmp_path / "g.dxf")
    assert rt["lost"] == 0 and rt["handles_lost"] == 0
    res = import_dxf(tmp_path / "f.dxf")
    assert res.report.spaces == sum(len(pl.rooms) for pl in plans)
    assert res.report.flats == len(plans)
    a = run_checks(FloorContext(plans=plans, height=24))
    b = run_checks(FloorContext(plans=res.plans, height=24))
    room_rules = lambda s: sorted((r.rule_id, r.result) for r in s.results if r.rule_id.startswith("DCPR-37"))  # noqa: E731
    assert room_rules(a) == room_rules(b)


def test_unit_doubt_ticket(tmp_path):
    import ezdxf

    doc = ezdxf.new("R2007")
    doc.header["$INSUNITS"] = 4  # mm, but drawn in metres
    msp = doc.modelspace()
    msp.add_lwpolyline([(0, 0), (4, 0), (4, 3), (0, 3)], close=True, dxfattribs={"layer": "A-AREA"})
    msp.add_text("BEDROOM", dxfattribs={"layer": "A-ANNO-TEXT"}).set_placement((1, 1.5))
    msp.add_line((0, 0), (1, 1), dxfattribs={"layer": "MYSTERY-7"})
    doc.saveas(tmp_path / "m.dxf")
    res = import_dxf(tmp_path / "m.dxf")
    kinds = {t["kind"] for t in res.tickets}
    assert "T2" in kinds and "T1" in kinds
    assert res.report.scale_to_m == 1.0  # default: trust the size


def _vent_plan(**kw):
    from planner_engine.models import Plan, PlanRoom

    room = PlanRoom(id="R1", type="toilet", name="Toilet", polygon=[(0, 0), (1.5, 0), (1.5, 2), (0, 2)],
                    area=3.0, width=1.5, unit_id="U1", **kw)
    return Plan(unit_id="U1", rooms=[room], walls=[], doors=[], windows=[], carpet_area=3.0, status="imported")


def _vent_results(**kw):
    s = run_checks(FloorContext(plans=[_vent_plan(**kw)], height=20))
    return {r.rule_id: r for r in s.results if r.rule_id in ("DCPR-39-2", "DCPR-39-3") and r.target.endswith("R1")}


def test_avs_shaft_ventilation_passes_and_duct_is_review():
    shaft = _vent_results(ventilated=True, vent_via="shaft", vent_shaft_area=9.0, vent_shaft_width=3.0)
    assert shaft["DCPR-39-2"].result == "pass"
    assert shaft["DCPR-39-3"].result == "pass"
    duct = _vent_results(ventilated=False, vent_via="duct")
    assert duct["DCPR-39-2"].result == "review"
    assert "DCPR-39-3" not in duct  # no shaft, no shaft-size test
    assert _vent_results(ventilated=False)["DCPR-39-2"].result == "fail"
    assert "DCPR-39-3" not in _vent_results(ventilated=True, vent_via="window")


def test_avs_minimum_size():
    small = _vent_results(ventilated=True, vent_via="shaft", vent_shaft_area=1.44, vent_shaft_width=1.2)
    assert small["DCPR-39-2"].result == "pass"
    assert small["DCPR-39-3"].result == "review"
    assert _vent_results(ventilated=True, vent_via="shaft")["DCPR-39-3"].result == "review"  # size unknown
    edge = _vent_results(ventilated=True, vent_via="shaft", vent_shaft_area=3.0, vent_shaft_width=1.5)
    assert edge["DCPR-39-3"].result == "pass"


def test_shaft_dims_from_visible_shaft_edges():
    from shapely.geometry import LineString

    from planner_engine.drafting import _shaft_dims

    contact = [((0.0, 0.0), (0.0, 2.0))]
    one_side = [LineString([(0, 0), (0, 2)])]  # one visible side of a square shaft
    assert _shaft_dims(one_side, contact) == (4.0, 2.0)
    two_sides = [LineString([(0, 0), (0, 2)]), LineString([(0, 2), (3, 2)])]  # L-shaped view: 3 x 2
    assert _shaft_dims(two_sides, contact) == (6.0, 2.0)
    assert _shaft_dims([LineString([(9, 9), (9, 11)])], contact) == (0.0, 0.0)  # not touching


def test_avs_size_measured_from_plate_shaft():
    from planner_engine.checks import FloorContext, _measured_shaft
    from planner_engine.models import PlanRoom

    room = PlanRoom(id="t1", type="toilet", name="Toilet", polygon=[(0, 0), (1.5, 0), (1.5, 2.4), (0, 2.4)], area=3.6,
                    width=1.5, vent_via="shaft", vent_shaft_area=0.25, vent_shaft_width=0.5)
    ctx = FloorContext(plans=[], height=24, shafts=[[(1.73, 0), (4.73, 0), (4.73, 3), (1.73, 3)]])
    assert _measured_shaft(room, ctx) == (9.0, 3.0)
