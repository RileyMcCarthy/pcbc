"""The emitted-board checks of paths (`docs/net-path-physics.md` §4.6, PR 8), proven by mutation.

A gate that cannot fail is not a gate. The board is the migrated buck with its switch path left out
and VIN as a draws-Path (`docs/physics-subjects.md` PR 2-3) that completes: `J_IN.1 -> C_IN1.1 -> U1.VIN
-> R_EN.1`, 1 A drawn at U1.VIN and 1 A at R_EN.1, so the link U1.3~R_EN.1 changes layer through two 2-via
groups at 0.8/0.4 (KD 13). Each mutation corrupts one piece of copper on the emitted board and pcbc's own
gates must name it: `copper.power_ampacity_failures` (each piece against its link's width on its layer) and
`route_verify.path_copper_failures` (vias, layers, every via group's worst barrel, a lone via where a group
belongs) and the off-path copper of a net with paths. KiCad is asked too, and what it cannot see is pinned:
KiCad selects copper by net or class, never the copper between two pads (KD 12), so a link narrower than
its width is pcbc's own measurement; the class width is the floor, so an off-path branch under the floor is
the one KiCad sees.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path

import pytest

from pcbc.compile import compile_design
from pcbc.copper import power_ampacity_failures
from pcbc.fab import _owned_copper, board_roles
from pcbc.language import load_board
from pcbc.route_verify import path_copper_failures, path_junctions

from conftest import declared_examples, edit_board, write_split

ROOT = Path(__file__).resolve().parent.parent
SW_PATH = '# The switch current: the regulator\'s SW pin to the inductor. The boot cap\'s branch stays thin.\nPath("SW", "U1.SW", "L1.1", amps=2)\n'

VIN_PATH = 'Path("VIN", "J_IN.1", "C_IN2.1", "C_IN1.1", "U1.VIN", amps=1.36)'
VIN_DRAWS = 'Path("VIN", "J_IN.1", "C_IN1.1", "U1.VIN", "R_EN.1", amps={"U1.VIN": 1, "R_EN.1": 1})'

SEG = re.compile(r'\(segment\s*\(start ([-0-9.]+) ([-0-9.]+)\)\s*\(end ([-0-9.]+) ([-0-9.]+)\)\s*\(width ([-0-9.]+)\)\s*(?:\(locked yes\)\s*)?\(layer "([^"]+)"\)\s*\(net "?([^")]*)"?\)(?:.|\n)*?\(uuid "([^"]+)"\)')
VIA = re.compile(r'\(via\s*\(at ([-0-9.]+) ([-0-9.]+)\)(?:.|\n)*?\(uuid "([^"]+)"\)\s*\)')


PRE_D14_PLACES = (
    ('\nPlace("C_IN1", to="U1.VIN")', '\nPlace("C_IN1", at=(16.46, 11.676), rot=180)'),
    ('\nPlace("C_IN2", to="U1.VIN")', '\nPlace("C_IN2", at=(19.82, 9.46), rot=0)'),
    ('\nPlace("L1", to="U1.SW")', '\nPlace("L1", at=(22.84, 13.996), rot=180)'),
    ('\nPlace("C_OUT1", to="L1.2")', '\nPlace("C_OUT1", at=(29.082, 13.996), rot=0)'),
    ('\nPlace("C_OUT2", to="L1.2")', '\nPlace("C_OUT2", at=(29.082, 16.246), rot=0)'),
    ('\nPlace("C_BOOT", to="U1.BOOT")', '\nPlace("C_BOOT", at=(15.99, 6.464), rot=0)'),
    ('\nPlace("R_EN", to="U1.EN")', '\nPlace("R_EN", at=(18.22, 6.454), rot=180)'),
    ('\nPlace("R_FB_TOP", to="U1.FB")', '\nPlace("R_FB_TOP", at=(19.05, 7.66), rot=180)'),
    ('\nPlace("R_FB_BOT", to="U1.FB")', '\nPlace("R_FB_BOT", at=(20.42, 6.454), rot=0)'),
)
"""buck's relation-placed parts where the checkpoint build of 2026-10-01 put them (before decision 4)."""


@pytest.fixture(scope="module")
def routed(tmp_path_factory):
    from conftest import routed_result

    work = tmp_path_factory.mktemp("buck_paths")
    shutil.copytree(declared_examples() / "buck", work / "buck", ignore=shutil.ignore_patterns("layout"))  # declared outline (a vehicle)
    board = work / "buck" / "buck.py"
    text = board.read_text()
    assert SW_PATH in text
    text = text.replace(SW_PATH, "# (the switch path left out: this board is for the gates, and it completes)\n")
    # docs/physics-subjects.md PR 3: VIN's 1.36 A through C_IN2.1 does not route on this placement (junction rule 2,
    # O5), so the gate vehicle's VIN is a draws-Path that does, and changes layer through via groups (line 50).
    assert VIN_PATH in text
    text = text.replace(VIN_PATH, VIN_DRAWS)
    # The gate vehicle is the migrated buck as it completed before the owner's decisions of 2026-10-01: VIN and GND
    # back on their shared 2 A NetReq (decision 5 split them, with a comment line: two lines more), and every part a
    # Place(to=) attaches declared where the relation placer put it then (decision 4 moves C_IN1 off U1.2's exit and
    # the board then leaves EN open; all nine, because a declared part is placed before every relation). Every line
    # number below is that file's: the 5V path is line 46 -> 45 with the switch path left out.
    i, j = text.index("# Currents at"), text.index('NetReq("5V"')
    text = text[:i] + 'NetReq("VIN", kind="power", volts=12); NetReq("GND", kind="power")\n' + text[j:]
    board.write_text(text)
    edit_board(board, PRE_D14_PLACES)  # the Place lines are layout.core.py's
    out, result = routed_result("buck_paths", board)
    assert result.get("error") is None, result.get("error")
    design = load_board(board)
    return out.read_text(), design, compile_design(design), out


def _gates(text, design, job):
    roles = board_roles(text)
    return power_ampacity_failures(job, text, owned=_owned_copper(roles)) + path_copper_failures(job, roles)


def _owner(text, uid):
    return next(((i.get("owner"), i.get("reason")) for i in board_roles(text).items if i["uuid"] == uid), (None, None))


def _seg(text, pred):
    for m in SEG.finditer(text):
        own, reason = _owner(text, m.group(8))
        if pred(m.group(7), m.group(6), float(m.group(5)), own or "", reason or ""):
            return m
    raise AssertionError("no such segment on the board")


@pytest.mark.kicad
def test_the_board_as_routed_passes_every_path_check(routed):
    text, design, job, _pcb = routed
    assert _gates(text, design, job) == []
    assert path_junctions(text, design, job.constraints) == []


@pytest.mark.kicad
def test_a_narrowed_trunk_segment_stops_the_build(routed):
    text, design, job, _pcb = routed
    m = _seg(text, lambda net, lay, w, own, r: net == "5V" and "~" in own and r == "route" and lay == "F.Cu" and w > 0.7)
    bad = text[: m.start(5)] + "0.5" + text[m.end(5) :]
    got = _gates(bad, design, job)
    assert any(g.startswith("5V route copper 0.5 mm on F.Cu < the 0.781 mm Path line 45's link") for g in got), got


@pytest.mark.kicad
def test_a_trunk_segment_on_b_cu_at_its_f_cu_width_stops_the_build(routed):
    text, design, job, _pcb = routed
    m = _seg(text, lambda net, lay, w, own, r: net == "5V" and "~" in own and r == "route" and lay == "F.Cu" and w > 0.7)
    bad = text[: m.start(6)] + "B.Cu" + text[m.end(6) :]
    got = _gates(bad, design, job)
    assert any("0.781 mm on B.Cu < the 1.09 mm Path line 45's link" in g for g in got), got


@pytest.mark.kicad
def test_a_narrowed_landing_stops_the_build(routed):
    text, design, job, _pcb = routed
    m = _seg(text, lambda net, lay, w, own, r: r == "route_group")
    bad = text[: m.start(5)] + "0.3" + text[m.end(5) :]
    got = _gates(bad, design, job)
    assert "VIN route_group landing 0.3 mm on F.Cu < the 0.4 mm Path line 50's link U1.3~R_EN.1 needs on F.Cu (1 A)" in got, got


@pytest.mark.kicad
def test_a_landing_under_its_link_stops_the_build_and_kicad_cannot_see_it(routed):
    """The seventh mutation: a landing at 0.2 mm, under the 0.4 mm its link needs and over the 0.16 mm class floor.
    pcbc's gate names it; KiCad's rules select copper by net or class, never the copper between two pads
    (`docs/physics-subjects.md` KD 12, O3), so its DRC does not move."""
    text, design, job, pcb = routed
    m = _seg(text, lambda net, lay, w, own, r: r == "route_group")
    bad = text[: m.start(5)] + "0.2" + text[m.end(5) :]
    got = _gates(bad, design, job)
    assert "VIN route_group landing 0.2 mm on F.Cu < the 0.4 mm Path line 50's link U1.3~R_EN.1 needs on F.Cu (1 A)" in got, got
    assert _drc_widths(pcb, bad) == _drc_widths(pcb, text)


@pytest.mark.kicad
def test_a_dropped_barrel_stops_the_build(routed):
    text, design, job, _pcb = routed
    vm = next(v for v in VIA.finditer(text) if _owner(text, v.group(3))[1] == "route_group")
    bad = text[: vm.start()] + text[vm.end() :]
    bad = re.sub(r'\s*"' + re.escape(vm.group(3)) + '"', "", bad)
    got = _gates(bad, design, job)
    assert any(
        g.startswith("VIN Path line 50's link U1.3~R_EN.1: the via group at ") and "is 1 barrel" in g
        and "its worst barrel carries 1.000 A of 1 A, over the 0.871 A one holds (lumped DC model, jlcpcb_2l_1oz; the link needs 2)" in g
        for g in got
    ), got


@pytest.mark.kicad
def test_a_via_on_the_feedback_tap_stops_the_build(routed):
    text, design, job, _pcb = routed
    tap = _seg(text, lambda net, lay, w, own, r: "R_FB_TOP" in own)
    vm = next(iter(VIA.finditer(text)))
    mx, my = (float(tap.group(1)) + float(tap.group(3))) / 2, (float(tap.group(2)) + float(tap.group(4))) / 2
    block = text[vm.start() : vm.end()]
    block = re.sub(r"\(at [-0-9.]+ [-0-9.]+\)", f"(at {mx:g} {my:g})", block, count=1)
    block = re.sub(r'\(net "?[^")]*"?\)', '(net "5V")', block, count=1).replace(vm.group(3), "00000000-0000-4000-8000-00000000beef")
    bad = text[: tap.start()] + block + "\n\t" + text[tap.start() :]
    bad = re.sub(r'(\(members[^)]*"' + re.escape(tap.group(8)) + '")', r'\1 "00000000-0000-4000-8000-00000000beef"', bad, count=1)
    got = _gates(bad, design, job)
    assert any("C_OUT1.1~R_FB_TOP.1: a via at" in g and "on a link with no vias" in g for g in got), got


@pytest.mark.kicad
def test_a_lone_via_where_a_group_belongs_stops_the_build(routed):
    """Refutation round 1, defect 8: with the lone-via loop disabled every test stayed green. VIN's 2-via groups
    become one plain `route` via: the router's old one-via layer change."""
    text, design, job, _pcb = routed
    roles = board_roles(text)
    gv = [i["uuid"] for i in roles.items if i["net"] == "VIN" and i["reason"] == "route_group" and i["key"][0] == "via"]
    assert len(gv) >= 3, gv
    keep = gv[0]
    bad = text
    for vm in reversed(list(VIA.finditer(text))):
        if vm.group(3) in gv and vm.group(3) != keep:
            bad = bad[: vm.start()] + bad[vm.end() :]
    # A path link's copper is grouped under its link key (`pcbc:route_group:U1.3~R_EN.1`), not the net's name.
    gs = bad.index('(group "pcbc:route_group:U1.3~R_EN.1"')
    ge = bad.index("\n\t)", gs)
    block = bad[gs:ge]
    for u in gv:
        block = re.sub(r'\s*"' + re.escape(u) + '"', "", block, count=1)
    bad = bad[:gs] + block + bad[ge:]
    bad = re.sub(r'(\(group "pcbc:route:U1.3~R_EN.1"\s*\(uuid "[^"]+"\)\s*\(members )', r'\1"' + keep + '" ', bad, count=1)
    assert [i["reason"] for i in board_roles(bad).items if i["uuid"] == keep] == ["route"]
    got = _gates(bad, design, job)
    assert any("a lone via at" in g and "over the 0.871 A one barrel holds" in g for g in got), got


def _drc_widths(pcb, text):
    import tempfile

    from pcbc.netcheck import kicad_drc

    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        for f in pcb.parent.iterdir():
            if f.suffix in (".kicad_pro", ".kicad_dru"):
                shutil.copy(f, d / f.name)
        (d / pcb.name).write_text(text)
        got = kicad_drc(d / pcb.name, refill=True)
    return sorted(str(v.get("description", "")) for v in got.get("violations", []) if v.get("type") == "track_width")


@pytest.mark.kicad
def test_a_narrowed_b_cu_vin_segment_stops_the_build_and_kicad_cannot_see_it(routed):
    """Refutation round 1, defect 9: a link's width on B.Cu was unpinned. A B.Cu VIN track of link U1.3~R_EN.1
    under the 0.4 mm its 1 A needs: pcbc's gate names it. The net carries no current since
    `docs/physics-subjects.md` PR 3, so there is no per-layer class rule, and KiCad's DRC does not move."""
    text, design, job, pcb = routed
    m = _seg(text, lambda net, lay, w, own, r: net == "VIN" and r == "route" and lay == "B.Cu")
    bad = text[: m.start(5)] + "0.25" + text[m.end(5) :]
    got = _gates(bad, design, job)
    assert "VIN route copper 0.25 mm on B.Cu < the 0.4 mm Path line 50's link U1.3~R_EN.1 needs on B.Cu (1 A)" in got, got
    assert _drc_widths(pcb, bad) == _drc_widths(pcb, text)


@pytest.mark.kicad
def test_a_narrowed_off_path_branch_stops_the_build_and_kicad_sees_it(routed):
    """Refutation round 1, defect 10: the off-path default (C_OUT2.1's branch) was enforced by the router only.
    Since `docs/physics-subjects.md` PR 3 that default is the class floor (0.16 mm, no declared current), which is
    also KiCad's class width, so narrowed to 0.14 mm pcbc's gate names it and KiCad's class rule sees it too."""
    text, design, job, pcb = routed
    m = _seg(text, lambda net, lay, w, own, r: net == "5V" and own == "5V" and r == "route" and abs(w - 0.16) < 1e-9)
    bad = text[: m.start(5)] + "0.14" + text[m.end(5) :]
    got = _gates(bad, design, job)
    assert "5V route copper 0.14 mm on F.Cu < the 0.16 mm 5V needs on F.Cu off its paths (on no Path: the net's own numbers, NetReq line 40)" in got, got
    base, after = _drc_widths(pcb, text), _drc_widths(pcb, bad)
    new = [v for v in after if v not in base]
    assert new and all("actual 0.1400 mm" in v for v in new), (base, after)
