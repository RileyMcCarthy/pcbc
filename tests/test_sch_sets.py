"""docs/schematic-layout.md §3.3, §4: SETS of gen lines and '# auto:' attach lines pasted into schematic.core.py are a
fixed point, and the item-4 sheet facts of refute:sheet / refute:lock (2026-10-09).

Each test fails when the behaviour it pins is removed (the mutation that kills it is named in its docstring).
"""

from __future__ import annotations

import itertools
import shutil
from pathlib import Path

import pytest

from pcbc.language import load_board
from pcbc.sch_emit import _parts_from_design, emit_from_design
from pcbc.sch_gen import gen_text

ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = ROOT / "examples"


def _copy(tmp_path: Path, name: str, core: bool = True) -> Path:
    dst = tmp_path / name
    shutil.copytree(EXAMPLES / name, dst, ignore=shutil.ignore_patterns("layout"))
    board = dst / f"{name}.py"
    assert not board.is_symlink()
    if not core and (dst / "schematic.core.py").exists():
        (dst / "schematic.core.py").unlink()
    return board


def _ds2_copy(tmp_path: Path, core: bool = True) -> Path:
    from conftest import ds2_dir

    src = ds2_dir()
    if not (src / "ds2_addon.py").exists():
        pytest.skip("the DS2 Addon is not checked out here")
    dst = tmp_path / "ds2"
    shutil.copytree(src, dst, ignore=shutil.ignore_patterns("layout"))
    if not core and (dst / "schematic.core.py").exists():
        (dst / "schematic.core.py").unlink()
    return dst / "ds2_addon.py"


def _emit(board: Path) -> tuple[str, dict]:
    rep: dict = {}
    return emit_from_design(load_board(board), title="x", report=rep), rep


def _gen(board: Path, rep: dict) -> list[str]:
    return [ln for ln in gen_text(load_board(board), rep, "x").split("# The sheet, primitive by primitive")[0].splitlines() if ln.startswith("Sch")]


def _body(line: str) -> str:
    return line.split("  #")[0]


def _attach(line: str) -> str | None:
    c = line.split("  # ", 1)[1] if "  # " in line else ""
    return c[len("auto: "):] if c.startswith("auto: SchPlace(") else None


def _key(line: str) -> tuple:
    import ast

    call = ast.parse(_body(line).strip()).body[0].value
    f = call.func.id
    a = [ast.literal_eval(x) for x in call.args]
    kw = {k.arg: ast.literal_eval(k.value) for k in call.keywords}
    if f == "SchSheet":
        return (f,)
    if f in ("SchPlace", "SchRegion", "SchNet"):
        return (f, a[0])
    if f == "SchText":
        return (f, a[0], a[1])
    return (f, a[0], kw.get("pin"))


def _paste(core: Path, lines: list[str]) -> None:
    """Lines pasted as an author locks what he sees: over the line with the same key, in its place; a new key at the end."""
    new = {_key(ln): _body(ln) for ln in lines}
    old = core.read_text().splitlines() if core.exists() else []
    out, used = [], set()
    for ln in old:
        k = _key(ln) if ln.startswith("Sch") else None
        if k in new:
            if k not in used:
                out.append(new[k])
                used.add(k)
            continue
        out.append(ln)
    core.write_text("\n".join(out + [new[k] for k in new if k not in used]) + "\n")


# ------------------------------------------------------------------ inferred region lines


def _regions(board: Path, core_text: str | None):
    from pcbc import sch_auto

    if core_text is not None:
        (board.parent / "schematic.core.py").write_text(core_text)
    elif (board.parent / "schematic.core.py").exists():
        (board.parent / "schematic.core.py").unlink()
    d = load_board(board)
    kinds = {n.name: n.kind for n in d.nets.values()}
    parts = _parts_from_design(d, kinds)
    regs = sch_auto.infer_regions(d, parts, kinds)
    sch_auto._grid(regs, {p.ref: p for p in parts}, kinds)
    return sorted((r.name, tuple(r.members), tuple(r.anchors), r.hub, r.regulator, r.row, r.col, r.side) for r in regs)


@pytest.mark.parametrize("name", ["ds2", "node", "c3_usb"])
def test_any_set_of_inferred_region_lines_infers_the_other_regions_as_before(tmp_path: Path, name: str):
    """refute:lock (blocker, 2026-10-09): four of DS2's inferred region lines pasted together left two undeclared
    regions, inferred over the rest alone; the hub cut no longer split them, they merged and five parts went off the
    paper. A region line named `auto-...` with the members the auto-layout infers for it is that inferred region
    (`sch_auto.infer_regions`), so every subset of them gives every region its members, anchors, hub flag and grid
    cell as with none. Mutation: `neutral = set()` in infer_regions."""
    board = _ds2_copy(tmp_path, core=False) if name == "ds2" else _copy(tmp_path, name, core=False)
    _sheet, rep = _emit(board)
    lines = [_body(ln) for ln in _gen(board, rep) if ln.startswith('SchRegion("auto-')]
    assert len(lines) >= 3
    base = _regions(board, None)
    for k in range(1, len(lines) + 1):
        for pick in itertools.combinations(lines, k):
            assert _regions(board, "\n".join(pick) + "\n") == base, pick


@pytest.mark.parametrize("name", ["ds2"])
def test_four_inferred_region_lines_pasted_together_give_the_same_sheet(tmp_path: Path, name: str):
    """The refuter's case itself, drawn: DS2 with no core, gen's auto-J3, auto-J1, auto-J4 and auto-J5 lines pasted into
    an empty core (it was hard 7, parts off the paper)."""
    board = _ds2_copy(tmp_path, core=False)
    first, rep = _emit(board)
    gen = _gen(board, rep)
    picked = [ln for ln in gen if ln.startswith(('SchRegion("auto-J3"', 'SchRegion("auto-J1"', 'SchRegion("auto-J4"', 'SchRegion("auto-J5"'))]
    assert len(picked) == 4
    _paste(board.parent / "schematic.core.py", picked)
    again, rep2 = _emit(board)
    assert rep2["readability"]["hard"] == 0, rep2["readability"]["detail"]
    assert again == first


# ------------------------------------------------------------------ '# auto:' attach lines


@pytest.mark.parametrize("core", [True, False])
def test_every_attach_comment_alone_and_all_together_changes_nothing(tmp_path: Path, core: bool):
    """gen's header: "A '# auto:' comment is itself a core line (an attach lock instead of a pose)". refute:lock (major,
    2026-10-09): 76 of 147 such lines changed the sheet and 8 failed the build (buck's R_FB_TOP: wire 5V through the 5V
    symbol), because a core attach line was placed before every auto spec. A line equal to the auto-layout's own spec is
    met (`sch_auto.met_attaches`) and the part is placed in its turn. Mutation: met_attaches returns (design, {})."""
    board = _copy(tmp_path, "buck", core=core)
    first, rep = _emit(board)
    attach = [a for a in (_attach(ln) for ln in _gen(board, rep)) if a]
    assert len(attach) >= 10
    core_file = board.parent / "schematic.core.py"
    keep = core_file.read_text() if core_file.exists() else ""
    for line in attach + ["ALL"]:
        core_file.write_text(keep)
        _paste(core_file, attach if line == "ALL" else [line])
        again, rep2 = _emit(board)
        assert rep2["readability"]["hard"] == 0, (line, rep2["readability"]["detail"])
        assert again == first, line
    assert set(rep2["_layout"].met) == {_key(a)[1] for a in attach}


def test_an_attach_line_whose_side_the_placer_does_not_give_is_the_lock_it_says(tmp_path: Path):
    """A `beside` spec's side is the placer's choice on each pass. gen's line with the side it chose is met and changes
    nothing; a line naming another side is a lock, and the part stands on that side (`sch_emit._settle` forces it).
    Mutation: the `wrong` check in _settle removed (the line is then met and ignored)."""
    board = _copy(tmp_path, "buck", core=False)
    first, rep = _emit(board)
    line = next(a for a in (_attach(ln) for ln in _gen(board, rep)) if a and a.startswith('SchPlace("C_IN2"'))
    assert 'side="left"' in line, line
    core_file = board.parent / "schematic.core.py"
    _paste(core_file, [line])
    same, _r = _emit(board)
    assert same == first
    other = line.replace('side="left"', 'side="right"')
    core_file.write_text(other + "\n")
    moved, rep3 = _emit(board)
    assert moved != first
    spec = rep3["_layout"].specs["C_IN2"]
    assert (spec.source, spec.side, spec.line) == ("core", "right", 1)
    assert "C_IN2" not in rep3["_layout"].met


# ------------------------------------------------------------------ poses over their attach lines, with others


def test_a_pose_over_its_attach_line_with_a_pose_of_a_part_placed_after_it_changes_nothing(tmp_path: Path):
    """refute:lock (blocker, 2026-10-09): node's C_MCU pose pasted over its attach line `to="U1.3V3"` with R_LED's pose
    moved SW_RST and C_EN; on c3_usb the same with C_EN's pose moved six parts. Both differ from the sheet laid out
    without them; only C_MCU's explains the other (`sch_emit._causal_poses`), and applied alone it makes C_EN's come
    out as locked. Mutation: `_causal_poses` returns `now` (every differing pose applied at once)."""
    board = _copy(tmp_path, "c3_usb")
    first, rep = _emit(board)
    gen = _gen(board, rep)
    picked = [ln for ln in gen if ln.startswith(('SchPlace("C_MCU", at=', 'SchPlace("C_EN", at='))]
    assert len(picked) == 2
    _paste(board.parent / "schematic.core.py", picked)
    again, rep2 = _emit(board)
    assert rep2["readability"]["hard"] == 0
    assert again == first
    assert rep2["_applied_poses"] == {"C_MCU": next(i for i, ln in enumerate((board.parent / "schematic.core.py").read_text().splitlines(), 1) if ln.startswith('SchPlace("C_MCU"'))}


# ------------------------------------------------------------------ the paper


def test_the_paper_is_a_lock_of_a_drawn_value_met_when_the_extent_picks_it(tmp_path: Path):
    """gen writes `SchSheet(paper=...)`; a paper lock fixed for every pass of the layout loop changes how the passes rank
    (parts off a smaller paper). It is laid out without the lock and met when the extent picks the same paper.
    Mutation: the SchSheet entry dropped from `sch_gen.lock_bodies`."""
    from pcbc.sch_emit import relaxed
    from pcbc.sch_gen import lock_bodies

    board = _copy(tmp_path, "c3_usb")
    first, rep = _emit(board)
    sheet_line = next(ln for ln in _gen(board, rep) if ln.startswith("SchSheet("))
    _paste(board.parent / "schematic.core.py", [sheet_line])
    d = load_board(board)
    locks = lock_bodies(d, _parts_from_design(d, {n.name: n.kind for n in d.nets.values()}))
    assert [(stage, key) for stage, key, _b, _s in locks if key == ("SchSheet",)] == [(0, ("SchSheet",))]
    assert relaxed(d, locks, set()).sch_sheet.paper is None and d.sch_sheet.paper == rep["readability"]["paper"]
    again, _rep2 = _emit(board)
    assert again == first


# ------------------------------------------------------------------ junction dots (refute:sheet, minor)


def test_a_t_whose_third_arm_is_a_power_symbol_pin_gets_a_dot():
    """refute:sheet (2026-10-09): every T of two wire ends and a part pin had a dot, every T whose third arm was a power
    symbol's pin had none (buck (77.47, 34.29): the GND symbol on the PWR_FLAG-to-U1.1 wire). A power symbol's pin is a
    symbol pin like a part's (`_Sheet.hat_pins`). Mutation: `junction_points` back to `n >= 3 or (on_pin and n >= 2)`."""
    from pcbc.sch_emit import _Sheet, junction_points, wire_overlaps

    sheet = _Sheet([])
    sheet.add([(0.0, 0.0), (5.08, 0.0)], "GND")
    sheet.add([(5.08, 0.0), (10.16, 0.0)], "GND")
    assert junction_points(sheet) == []
    sheet.hat_pins.append((5.08, 0.0, "hat:GND:X.1"))
    assert junction_points(sheet) == [(5.08, 0.0)]
    assert wire_overlaps(sheet, []) == []  # three ways meet there: no stray dot
    _text, rep = _emit(EXAMPLES / "buck" / "buck.py")
    assert (77.47, 34.29) in [(round(x, 2), round(y, 2)) for x, y in junction_points(rep["_sheet"])]


# ------------------------------------------------------------------ names cut off from their part (refute:sheet, minor)


def test_cut_off_finds_a_wire_between_a_text_and_its_body():
    from pcbc.sch_emit import cut_off

    body = (289.56, 36.8, 297.18, 39.4)  # DS2's R4
    below = (291.0, 40.9, 295.7, 42.4)  # its Reference under the ADC_DRDY wire at y 40.64
    wire = (299.72, 40.64, 289.56, 40.64, "ADC_DRDY")
    assert cut_off([wire], below, body) == wire
    assert cut_off([wire], (291.0, 33.0, 295.7, 34.6), body) is None  # above: nothing between
    assert cut_off([(299.72, 45.0, 289.56, 45.0, "X")], below, body) is None  # the wire beyond the text


def test_ambiguous_names_counts_a_name_cut_off_from_its_part_by_a_wire():
    """refute:sheet: DS2's R4 Reference sat under the ADC_DRDY wire ("R4 47R | R5 47R") and ambiguous_names said 0,
    because the text was still nearest R4's body. A wire drawn between a name and its body counts the part.
    Mutation: the cut_off branch in sch_score.score removed."""
    from pcbc.sch_place import body_aabb
    from pcbc.sch_score import score

    _text, rep = _emit(EXAMPLES / "blinky" / "blinky.py")
    sheet, parts = rep["_sheet"], rep["_parts"]
    before = score(sheet, parts, rep, paper="A4")
    part = next(p for p in parts if p.ref == "R1")
    ref = next(o for o in sheet.occupants if o.owner == "R1" and o.kind == "ref")
    x0, y0, x1, y1 = body_aabb(part)
    bx0, by0, bx1, by1 = ref.box
    if bx0 >= x1:  # the name right of the body: a vertical wire between
        mx = (x1 + bx0) / 2.0
        sheet.segs.append((mx, min(y0, by0) - 1, mx, max(y1, by1) + 1, "ZZ"))
    elif bx1 <= x0:
        mx = (bx1 + x0) / 2.0
        sheet.segs.append((mx, min(y0, by0) - 1, mx, max(y1, by1) + 1, "ZZ"))
    elif by0 >= y1:
        my = (y1 + by0) / 2.0
        sheet.segs.append((min(x0, bx0) - 1, my, max(x1, bx1) + 1, my, "ZZ"))
    else:
        my = (by1 + y0) / 2.0
        sheet.segs.append((min(x0, bx0) - 1, my, max(x1, bx1) + 1, my, "ZZ"))
    after = score(sheet, parts, rep, paper="A4")
    assert after["ambiguous_names"] == before["ambiguous_names"] + 1, after["_ambiguous"]
    assert any("runs between R1 and its" in a for a in after["_ambiguous"])


def test_a_two_pin_name_is_placed_and_repaired_clear_of_wires_between_it_and_its_body():
    """buck's R_EN Reference and Value were drawn beyond the 5V wire, beside R_FB_TOP. A cut spot gives way to a clean
    one, and a name with none is a lint fact the layout loop repairs by pushing the part and the wire's owner.
    Mutation: the cut-off block in `_lint` removed (R_EN's name stays beyond the wire)."""
    from pcbc.sch_emit import cut_off
    from pcbc.sch_place import body_aabb

    _text, rep = _emit(EXAMPLES / "buck" / "buck.py")
    sheet = rep["_sheet"]
    parts = {p.ref: p for p in rep["_parts"]}
    for o in sheet.occupants:
        if o.kind in ("ref", "value") and o.owner in ("R_EN",):
            assert cut_off(sheet.segs, o.box, body_aabb(parts[o.owner])) is None, (o.owner, o.kind, o.box)
    assert not any("is cut off from" in i for i in rep["issues"])


# ------------------------------------------------------------------ one fact once; the lock a real move applies


def test_a_label_over_both_boxes_of_a_two_pin_symbol_is_one_collision(tmp_path: Path):
    """refute:lock (minor): buck's L1 posed onto U1's SW label reported `symbol L1 overlaps label SW at U1.2` twice (a
    two-pin symbol is drawn as its core and its body with the pins). Mutation: the `once` dedupe in sch_score removed."""
    board = _copy(tmp_path, "buck", core=False)
    _first, rep = _emit(board)
    region = next(ln for ln in _gen(board, rep) if ln.startswith("SchRegion("))
    (board.parent / "schematic.core.py").write_text(_body(region) + '\nSchPlace("L1", at=(5.08, 20.32), rotate=270, mirror=None)\n')
    _text, rep2 = _emit(board)
    facts = rep2["readability"]["detail"]["text_collisions"]
    assert len(facts) == len(set(facts)) == rep2["readability"]["text_collisions"], facts


def test_a_hard_finding_on_a_sheet_that_applies_locks_names_them(tmp_path: Path):
    """refute:lock (minor): buck's L1 locked near C_BOOT failed on `wire FB runs through 5V symbol at C_OUT2`, naming
    neither L1 nor its line. The error names every lock the sheet applies and the hard count without them.
    Mutation: `_applied_locks_fact` returns ""."""
    from pcbc.build import _applied_locks_fact

    board = _copy(tmp_path, "buck", core=False)
    _first, rep = _emit(board)
    region = next(ln for ln in _gen(board, rep) if ln.startswith("SchRegion("))
    (board.parent / "schematic.core.py").write_text(_body(region) + '\nSchPlace("L1", at=(5.08, 20.32), rotate=270, mirror=None)\n')
    _text, rep2 = _emit(board)
    assert rep2["readability"]["hard"] > 0
    fact = _applied_locks_fact(rep2)
    assert "schematic.core.py:2 (SchPlace('L1'))" in fact and "laid out without them it has 0 hard findings" in fact, fact
    assert _applied_locks_fact({}) == ""


# ------------------------------------------------------------------ node: 28 labels (item 5)


@pytest.mark.parametrize("core", [True, False])
def test_node_joins_the_led_and_the_boot_pins_by_wire(tmp_path: Path, core: bool):
    """Target (2026-10-09): node drew 29 labels against 28 by hand, because the switch under the EN node reached down
    across IO10's run to R_LED (or, with R_LED inside it, R_BOOT's). Of two parts that would take the same spot at a
    node the shorter takes it (C_EN under the node, SW_RST beside it, as the hand sheet hangs them), and both runs are
    wires. Mutation: `_span(by[m])` dropped from the hanger order in `sch_auto.region_specs`."""
    board = _copy(tmp_path, "node", core=core)
    _text, rep = _emit(board)
    labels = [o for o in rep["_sheet"].occupants if o.kind == "label"]
    assert rep["readability"]["labels"] <= 28
    assert sum(1 for o in labels if o.net == "LED") <= 1 and sum(1 for o in labels if o.net == "BOOT") <= 1
    assert rep["readability"]["hard"] == 0


def test_a_region_moved_down_past_a_locked_origin_never_lands_on_another_region(tmp_path: Path):
    """node with U1's pose 20.32 mm left of gen's: the mcu frame widens, the packed analog region moved down past it
    and onto the load region (20 hard findings, region frames overlapping). A packed frame moves past every frame it
    lands on, locked or packed (`sch_auto._pack`). Mutation: the `settled` list back to the locked frames only."""
    board = _copy(tmp_path, "node")
    _first, rep = _emit(board)
    gen = _gen(board, rep)
    reg = _body(next(ln for ln in gen if ln.startswith('SchRegion("mcu"')))
    pose = _body(next(ln for ln in gen if ln.startswith('SchPlace("U1"')))
    x = float(pose.split("at=(")[1].split(",")[0])
    pose = pose.replace(f"at=({pose.split('at=(')[1].split(',')[0]},", f"at=({x - 20.32:g},")
    _paste(board.parent / "schematic.core.py", [reg, pose])
    _text, rep2 = _emit(board)
    assert rep2["readability"]["region_frames"] == 0 and rep2["readability"]["hard"] == 0, rep2["readability"]["detail"]


def test_a_pose_tried_alone_that_explains_no_other_is_taken_back(tmp_path: Path):
    """node's C_BME pose pasted over its attach line `to="U4.VDD", gap=15.24` with R_SCL's pose: both differ from the
    sheet laid out without them, and placement alone picked R_SCL (pinned, it pushed the auto-placed C_BME onto its
    pose). Laid out, R_SCL made C_BME come out as locked no more than before, so it is taken back and C_BME is tried:
    then R_SCL comes out as locked (`sch_emit._settle`'s trials). Mutation: `back = set()` in _settle."""
    board = _copy(tmp_path, "node")
    first, rep = _emit(board)
    gen = _gen(board, rep)
    picked = [ln for ln in gen if ln.startswith(('SchPlace("C_BME", at=', 'SchPlace("R_SCL", at='))]
    assert len(picked) == 2
    _paste(board.parent / "schematic.core.py", picked)
    again, rep2 = _emit(board)
    assert again == first
    assert set(rep2["_applied_poses"]) == {"C_BME"}
