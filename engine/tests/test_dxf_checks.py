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
