"""What the first end-to-end runs taught the tool, pinned so the next AI never meets them."""

from __future__ import annotations

from pathlib import Path

from pcbc.apply import _turn_pads
from pcbc.compile import compile_design
from pcbc.language import load_board
from pcbc.source import _touching_pads, repair_footprint, score_footprint

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"


def test_turning_a_footprint_turns_its_pads():
    """KiCad stores a pad's angle as footprint angle + pad angle in a board file (not in a
    library file). Leaving pads at their library angle in a footprint turned 90 drew the
    ESP32 module's 0.8 mm pitch pads unrotated, touching each other: DRC 'actual 0.0000 mm'."""
    block = (
        '(footprint "X"\n\t\t(at 10 10 0)\n'
        '\t\t(pad "1" smd rect\n\t\t\t(at -5.9 -4)\n\t\t\t(size 0.8 0.4)\n\t\t)\n'
        '\t\t(pad "12" smd rect\n\t\t\t(at -4.8 4.9 90)\n\t\t\t(size 0.8 0.4)\n\t\t)\n'
        '\t\t(pad "" np_thru_hole circle\n\t\t\t(at 2.9 -1.21 270)\n\t\t\t(size 0.6 0.6)\n\t\t)\n\t)'
    )
    turned = _turn_pads(block, 90.0)
    assert '(pad "1" smd rect\n\t\t\t(at -5.9 -4 90)' in turned
    assert '(pad "12" smd rect\n\t\t\t(at -4.8 4.9 180)' in turned
    assert '(at 2.9 -1.21)' in turned  # 270 + 90 = 360 = 0, written as no angle
    assert _turn_pads(block, 0.0) == block


def test_fetch_repairs_ringless_mounting_holes_and_the_scorer_flags_them():
    """EasyEDA draws a connector's mounting holes as plated pads whose drill equals their size.
    KiCad DRC rejects the zero annular ring; a mounting hole is an np_thru_hole."""
    text = (
        '(footprint "J"\n\t(pad "" thru_hole circle\n\t\t(at -2.9 -1.21)\n\t\t(size 0.6 0.6)\n\t\t(drill 0.6)\n\t)\n'
        '\t(pad "1" thru_hole circle\n\t\t(at 0 0)\n\t\t(size 1.0 1.0)\n\t\t(drill 0.6)\n\t)\n)\n'
    )
    assert any("no annular ring" in m for _s, m in score_footprint(text)["findings"])
    fixed, notes = repair_footprint(text)
    assert '(pad "" np_thru_hole circle' in fixed and '(pad "1" thru_hole' in fixed
    assert notes and "1 unnumbered plated hole" in notes[0]
    assert not any("no annular ring" in m for _s, m in score_footprint(fixed)["findings"])


def test_pads_that_touch_fail_and_tight_pads_warn():
    def mod(pitch: float) -> str:
        return (
            '(footprint "U"\n'
            + "".join(f'\t(pad "{i}" smd rect\n\t\t(at {i * pitch:.2f} 0 90)\n\t\t(size 0.8 0.4)\n\t)\n' for i in range(1, 4))
            + '\t(pad "1" smd rect\n\t\t(at 0 5)\n\t\t(size 0.4 0.4)\n\t)\n)\n'  # a second pad 1: same number never counts
        )
    touching = score_footprint(mod(0.4))["findings"]  # 0.4 wide pads at 0.4 pitch (the pad is 0.4 wide once turned 90)
    assert any("touch" in m and "1/2" in m for s, m in touching if s == "fail"), touching
    tight = score_footprint(mod(0.5))["findings"]  # 0.1 mm gaps: below JLC's 0.127 two-layer spacing
    assert not any(s == "fail" for s, _m in tight)
    assert any("0.127" in m for s, m in tight if s == "warn"), tight
    assert _touching_pads(mod(0.6)) == []


def test_design_rules_exempt_a_footprints_own_pads_down_to_the_floor():
    """KiCad applied the Power class clearance (0.2 mm) between a USB-C receptacle's own pads,
    which sit 0.1 mm apart; that is the land, not a routing choice."""
    job = compile_design(load_board(EXAMPLES / "c3_usb" / "c3_usb.py"))
    rule = job.dru[0]
    assert rule.name == "pads_of_one_footprint"
    assert "A.Reference == B.Reference" in rule.condition and "A.Type == 'Pad'" in rule.condition
    assert "min 0.1mm" in rule.constraint


def test_every_generic_needs_a_vendored_land(tmp_path: Path):
    board = tmp_path / "b.py"
    board.write_text(
        "from pcbc import *\n"
        'VCC = Power("VCC"); GND = Ground("GND")\n'
        'Capacitor("C1", "1uF", package="2220", mpn="X", lcsc="C1", p1=VCC, p2=GND)\n'
        'Capacitor("C2", "1uF", package="0805", mpn="X", lcsc="C1", p1=VCC, p2=GND)\n'
        'Board(width=20, height=20, layers=2, stackup="jlcpcb_2l_1oz")\n'
        'Place("C1", at=(5, 5)); Place("C2", at=(10, 5))\n'
        'SchPlace("C1", left=20, top=20); SchPlace("C2", left=60, top=20)\n'
    )
    from pcbc.language import check_board

    fails = check_board(board)
    assert any(f.startswith("C1: no vendored land for C package '2220'") for f in fails), fails
    assert not any(f.startswith("C2:") for f in fails)


def test_the_first_place_on_a_pin_gets_the_closest_spot(tmp_path: Path):
    """Buck: L1 and the output caps, listed after the input caps, used to take U1.VIN's
    free side first; the input caps ended 3.6 mm from the pin. File order decides."""
    from pcbc.build import pcb_job

    board = EXAMPLES / "buck" / "buck.py"
    result = pcb_job(board)
    assert result.get("error") is None, result
    assert result["layout_report"] == [], result["layout_report"]
    poses = result["poses"]
    u1 = poses["U1"]["at"]
    for cap in ("C_IN1", "C_IN2"):
        at = poses[cap]["at"]
        assert ((at[0] - u1[0]) ** 2 + (at[1] - u1[1]) ** 2) ** 0.5 < 4.5, (cap, at, u1)


def test_report_holds_copper_off_the_edge_and_nets_to_their_max_mm(tmp_path: Path):
    from pcbc.build import pcb_job

    src = (EXAMPLES / "buck" / "buck.py").read_text()
    src = src.replace('Place("L1", to="U1.SW")', 'Place("L1", position="absolute", right=6, bottom=1)')
    src = src.replace('Place("R_EN", to="U1.EN")', 'Place("R_EN", position="absolute", left=12, top=0)')
    board = tmp_path / "buck.py"
    board.write_text(src)
    import shutil

    shutil.copytree(EXAMPLES / "buck" / "components", tmp_path / "components")
    result = pcb_job(board)
    text = "\n".join(result["layout_report"])
    assert "SW: " in text and "NetReq max_mm=6" in text, result["layout_report"]
    assert "R_EN's pads come within 0.3 mm of the board edge" in text, result["layout_report"]


def test_a_part_placed_twice_is_refused(tmp_path: Path):
    """Buck carried its old hand grid under the new relational lines; the second Place() did not win,
    the fab check saw every part 'moved'."""
    from pcbc.language import check_board

    board = tmp_path / "b.py"
    board.write_text(
        "from pcbc import *\n"
        'VCC = Power("VCC"); GND = Ground("GND")\n'
        'Resistor("R1", "1k", package="0402", mpn="X", lcsc="C1", p1=VCC, p2=GND)\n'
        'Board(width=20, height=20, layers=2, stackup="jlcpcb_2l_1oz")\n'
        'Place("R1", at=(5, 5)); Place("R1", at=(10, 5))\n'
        'SchPlace("R1", left=20, top=20); SchPlace("R1", left=60, top=20)\n'
    )
    fails = check_board(board)
    assert "R1: Place() 2 times; keep one (the last one does not win, the check does)" in fails, fails
    assert "R1: SchPlace() 2 times; keep one (the last one does not win, the check does)" in fails, fails


def test_fiducials_take_free_corners_and_are_kept_clear(tmp_path: Path):
    """Fab used to drop fiducials onto a finished board: on node one landed on U4's pad and
    the 3V3 tracks. They go in at the place stage, after the anchors and before the relations,
    so U4 keeps its corner, the relations keep off them, and the router routes around."""
    import shutil

    from pcbc.build import pcb_job
    from pcbc.layout import footprints_by_ref
    from pcbc.pcb_place import _overlap, parse_foot
    from pcbc.sexp import footprint_at

    board = tmp_path / "node.py"
    board.write_text((EXAMPLES / "node" / "node.py").read_text())
    shutil.copytree(EXAMPLES / "node" / "components", tmp_path / "components")
    result = pcb_job(board)
    assert result.get("error") is None, result
    text = Path(result["placed"]).read_text()
    blocks = footprints_by_ref(text)
    fids = [r for r in blocks if r.startswith("FID")]
    assert len(fids) == 3, fids
    feet = {}
    for ref, blk in blocks.items():
        f = parse_foot(ref, blk)
        at = footprint_at(blk)
        f.at, f.rot = (at[0], at[1]), at[2]
        feet[ref] = f
    for fid in fids:
        for ref, f in feet.items():
            if ref == fid:
                continue
            assert not _overlap(feet[fid].world_box(), f.world_box()), (fid, ref)


def test_the_stackup_is_the_one_source_of_fab_limits(tmp_path: Path):
    """KiCad's defaults judged the vias once KRT stopped rewriting the rules: 0.2 mm drills on a
    2-layer board, 0.075 mm rings, tracks 0.236 from holes. The project, the classes, the router
    and the gate now all read the same Stackup."""
    import json

    from pcbc.route import krt_plan
    from pcbc.seed import seed_job
    from pcbc.stackup import board_rules, get_stackup

    two = get_stackup("jlcpcb_2l_1oz")
    four = get_stackup("jlcpcb_4l_1oz")
    assert (two.via_drill, two.via_diameter, two.clearance_min) == (0.3, 0.5, 0.127)
    assert (four.via_drill, four.via_diameter, four.clearance_min) == (0.2, 0.35, 0.0889)  # 3.5 mil
    for board, stack in (("buck", two), ("node", four)):
        design = load_board(EXAMPLES / board / f"{board}.py")
        job = compile_design(design)
        default = next(c for c in job.classes if c.name == "Default")
        assert (default.via_diameter_mm, default.via_drill_mm) == (stack.via_diameter, stack.via_drill)
        assert all(c.via_drill_mm >= stack.via_drill and c.clearance_mm >= stack.clearance_min for c in job.classes)
        out = tmp_path / board / "layout.kicad_pcb"
        out.parent.mkdir(parents=True)
        seed_job(design, out, name=board)
        rules = json.loads(out.with_suffix(".kicad_pro").read_text())["board"]["design_settings"]["rules"]
        assert rules == board_rules(stack)
        plan = krt_plan(job, design, Path("p.kicad_pcb"), Path("r"), Path("/krt"))
        for _name, cmd in plan:
            assert cmd[cmd.index("--via-drill") + 1] == f"{stack.via_drill:g}"
            if "--routing-clearance-margin" in cmd:
                assert float(cmd[cmd.index("--routing-clearance-margin") + 1]) >= 1.0
