"""Paths compiled (`docs/net-path-physics.md` §2-§3, §7; PRs 2, 3, 5 and 7's compile half).

Everything here is `compile_constraints` on a scratch copy of buck: the §5 file (the migrated example)
and one-line variants of it, each pinned to the message §7 prints. No geometry, no KiCad.
"""

from __future__ import annotations

import math
import shutil
from pathlib import Path

import pytest

from conftest import edit_board, write_split
from pcbc.constraints import compile_constraints
from pcbc.language import check_board, load_board

ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = ROOT / "examples"

SECTION5 = '''NetReq("VIN", "GND", kind="power", volts=12, amps=2)
NetReq("5V", kind="power", volts=5)
NetReq("SW", kind="switch_node", max_mm=6)
NetReq("FB", kind="analog")
# The switch current: the regulator's SW pin to the inductor. The boot cap's branch stays thin.
Path("SW", "U1.SW", "L1.1", amps=2)
# The load current: the inductor's output, through the first output cap, to the connector.
Path("5V", "L1.2", "C_OUT1.1", "J_OUT.1", amps=2)
# The divider's top: leaves the rail inside the output cap's pad and touches 5V nowhere else.
Path("5V", "C_OUT1.1", "R_FB_TOP.1", kind="feedback")
# The input hot loop, checked at placement.
Loop("C_IN1.1", "U1.VIN", "U1.GND", "C_IN1.2")
'''
SHIPPED = '''NetReq("VIN", "5V", "GND", kind="power", volts=12, amps=2)
NetReq("SW", kind="switch_node", max_mm=6)
NetReq("FB", kind="analog")
'''


def section5_text() -> str:
    """The §5 buck file: the migrated example as shipped, or the pre-migration example with lines 55-57
    replaced by §5's (so the tests read one file either way)."""
    text = (EXAMPLES / "buck" / "buck.py").read_text()
    if 'Path("5V"' in text:
        return text
    text = text.replace(SHIPPED, SECTION5)
    return text.replace("from pcbc import Board, Capacitor, Ground, Net, NetReq, Place,", "from pcbc import Board, Capacitor, Ground, Loop, Net, NetReq, Path, Place,")


def _buck(tmp_path: Path, edits: list[tuple[str, str]] = (), *, name: str = "buck") -> Path:
    work = tmp_path / name
    shutil.copytree(EXAMPLES / "buck", work, ignore=shutil.ignore_patterns("layout"))
    write_split(work / "buck.py", section5_text())
    edit_board(work / "buck.py", edits)
    return work / "buck.py"


def _cs(board: Path):
    return compile_constraints(load_board(board))


def test_the_section5_file_reports_what_the_doc_predicts(tmp_path: Path):
    cs = _cs(_buck(tmp_path))
    assert cs.refusals == ()
    want = [
        "5V: path L1.2 -> C_OUT1.1 -> J_OUT.1 (Path line 48): width 0.781 mm on F.Cu (ipc2221_ext 2 A 10 C 1 oz jlcpcb_2l_1oz; plane 0.593 at 1.53 mm B.Cu pour), "
        "1.09 mm on B.Cu (ipc2152_fit 1 oz jlcpcb_2l_1oz, no plane); 3 vias per layer change, flow-through at 1.3 mm pitch, worst barrel 0.750 A of 0.871 "
        "(via_barrel 0.4/0.018 mm at 10 C; lumped DC model)",
        "5V: path C_OUT1.1 -> R_FB_TOP.1 (Path line 50, kind feedback): width 0.2 mm (preset feedback), no vias, layers F.Cu, length 15 mm; "
        "meets 5V only inside C_OUT1.1, its first station",
        # docs/physics-subjects.md §6 (PR 3): a branch no Path names is the class floor and carries no declared current.
        "5V: C_OUT2.1 is on no Path; its branch is 0.16 mm (class floor) and carries no declared current; a 0.16 mm branch carries 0.633 A on F.Cu "
        "(over the B.Cu pour) and 0.633 A on B.Cu at 10 C, not Path line 48's 2 A",
        "SW: path U1.SW -> L1.1 (Path line 46): width 0.781 mm on F.Cu (ipc2221_ext 2 A 10 C 1 oz jlcpcb_2l_1oz; plane 0.593 at 1.53 mm B.Cu pour), "
        "1.09 mm on B.Cu (ipc2152_fit 1 oz jlcpcb_2l_1oz, no plane); 4 vias per layer change, flow-through at 1.1 mm pitch, worst barrel 0.622 A of 0.707 "
        "(via_barrel 0.3/0.018 mm at 10 C; lumped DC model)",
        # PR 5: the switch node's 0.3 mm width folds into KD 10 (a net kind sets no width): its unnamed branch is the floor.
        "SW: C_BOOT.2 is on no Path; its branch is 0.16 mm (class floor) and carries no declared current; a 0.16 mm branch carries 0.633 A on F.Cu "
        "(over the B.Cu pour) and 0.633 A on B.Cu at 10 C, not Path line 46's 2 A",
        # PR 3: VIN's current is its Path's (line 69), so the via group is the link's, not the net's.
        "VIN: via 0.5/0.3 mm (stackup jlcpcb_2l_1oz)",  # PR 5: a net kind sets no via; the current path keeps 0.8/0.4 (KD 13)
        "VIN: path J_IN.1 -> C_IN2.1 -> C_IN1.1 -> U1.VIN (Path line 53): width 0.459 mm on F.Cu (ipc2221_ext 1.36 A 10 C 1 oz jlcpcb_2l_1oz; plane 0.593 at 1.53 mm B.Cu pour), "
        "0.541 mm on B.Cu (ipc2152_fit 1 oz jlcpcb_2l_1oz, no plane); 2 vias per layer change, flow-through at 1.3 mm pitch, worst barrel 0.680 A of 0.871 "
        "(via_barrel 0.4/0.018 mm at 10 C; lumped DC model)",
        # KD 12 / O3: what KiCad does not check is listed.
        "VIN: KiCad does not check Path line 53's copper (width, vias, layers): a KiCad rule selects copper by net or class, never the copper between two pads; "
        "pcbc's copper gate checks it on the emitted board",
        "loop C_IN1.1 -> U1.VIN -> U1.GND -> C_IN1.2 (Loop line 52): 20 mm2 (pcbc default; U1 carries SW)",
    ]
    for line in want:
        assert line in cs.lines, (line, [x for x in cs.lines if x.split(":")[0] == line.split(":")[0]])


def test_links_and_widths_are_queried_by_pad(tmp_path: Path):
    cs = _cs(_buck(tmp_path))
    trunk = cs.link("5V", "L1.2", "C_OUT1.1")
    assert trunk is not None and trunk.key == "L1.2~C_OUT1.1" and trunk.source == "Path line 48"
    assert [(lay, d.value) for lay, d in trunk.width] == [("F.Cu", 0.781), ("B.Cu", 1.09)]
    assert trunk.group is not None and (trunk.group.count, trunk.group.pitch_mm, trunk.group.worst_a) == (3, 1.3, 0.75)
    assert cs.link("5V", "C_OUT1.1", "L1.2") is trunk, "either order"
    tap = cs.link("5V", "C_OUT1.1", "R_FB_TOP.1")
    assert tap.sense and not tap.via.allowed and tap.layers == ("F.Cu",) and tap.width_on("F.Cu") == 0.2
    assert cs.link("5V", "L1.2", "J_OUT.1") is None, "non-consecutive stations are no link"
    assert cs.link_by_key("C_OUT1.1~R_FB_TOP.1") is tap
    assert cs.pad_width("5V", "C_OUT1.1", "B.Cu") == 1.09 and cs.pad_width("5V", "C_OUT2.1", "F.Cu") == 0.16, "PR 3: an off-path pad is the class floor"
    assert cs.widest("5V") == 1.09 and cs.narrowest("5V") == 0.16, "the class floor is the narrowest (PR 3)"
    assert cs.by_net("5V").off_path == ("C_OUT2.1",) and cs.by_net("SW").off_path == ("C_BOOT.2",)


def test_a_field_less_link_costs_what_its_net_does():
    """PR 2: `link_cost == net_cost` on every field-less link of the boards (before PR 10 none wrote a field)."""
    from pcbc.route_cost import link_cost, net_cost

    for name in ("blinky", "buck", "c3_usb", "node"):
        cs = compile_constraints(load_board(EXAMPLES / name / f"{name}.py"))
        for c in cs.constraints:
            for p in c.paths:
                if p.fielded:
                    continue  # the migrated buck's paths carry fields: their links cost their own numbers
                for lk in p.links:
                    assert link_cost(cs, c.net, lk.a[0], lk.b[0]) == net_cost(cs, c.net), (name, lk.key)


REFUSALS = [
    (
        'station_off_net',
        [('Path("5V", "C_OUT1.1", "R_FB_TOP.1", kind="feedback")', 'Path("5V", "C_OUT1.1", "R_FB_TOP.2", kind="feedback")')],
        'Path("5V") line 50: R_FB_TOP.2 is on FB, not 5V; R_FB_TOP\'s 5V pad is R_FB_TOP.1',
    ),
    (
        'net_kind_on_a_path',
        [('Path("5V", "L1.2", "C_OUT1.1", "J_OUT.1", amps=2)', 'Path("5V", "L1.2", "C_OUT1.1", "J_OUT.1", amps=2, kind="power")')],
        'Path("5V") line 48: kind="power" is a net kind, and 5V is kind power (NetReq line 42); on a Path, kind= names a sense branch and takes "feedback" or "sense"',
    ),
    (
        'sense_with_amps',
        [('Path("5V", "C_OUT1.1", "R_FB_TOP.1", kind="feedback")', 'Path("5V", "C_OUT1.1", "R_FB_TOP.1", kind="feedback", amps=0.1)')],
        'Path("5V") line 50: kind="feedback" takes no amps=; a sense branch carries a measurement, not a load',
    ),
    (
        # docs/physics-subjects.md PR 1: a net takes current where a Path with amps= says so. FB carries
        # 0.1 A through R_FB_TOP, so the feedback tap that ends on R_FB_TOP.1 ends on that load.
        'feedback_ends_on_a_part_a_current_path_crosses',
        [('Loop("C_IN1.1"', 'Path("FB", "R_FB_TOP.2", "U1.FB", amps=0.1)\nLoop("C_IN1.1"')],
        'Path("5V") line 50, kind feedback: its far station R_FB_TOP.1 is a pad of R_FB_TOP, whose other pad R_FB_TOP.2 is a station of Path line 52, which carries current on FB, so it sits on the load; a sense Path starts where it leaves the rail',
    ),
    (
        'temp_rise_without_amps',
        [('Path("5V", "L1.2", "C_OUT1.1", "J_OUT.1", amps=2)', 'Path("5V", "L1.2", "C_OUT1.1", "J_OUT.1", temp_rise_c=20)')],
        'Path("5V") line 48: temp_rise_c= sizes a current, and this Path gives no amps=',
    ),
    (
        'feedback_backwards_a',
        [('Path("5V", "C_OUT1.1", "R_FB_TOP.1", kind="feedback")', 'Path("5V", "R_FB_TOP.1", "C_OUT1.1", kind="feedback")')],
        # PR 1: GND carries no Path with amps= (a poured return path is not built), so (a) does not
        # fire on C_OUT1's GND pad; the 5V Path that names C_OUT1.1 and not R_FB_TOP.1 does, as (c).
        "Path(\"5V\") line 50, kind feedback: Path line 48 names C_OUT1.1, this Path's last station, and not R_FB_TOP.1, its first; a sense Path starts at the station it shares",
    ),
    (
        'sense_backwards_b',
        [('Loop("C_IN1.1"', 'Path("VIN", "R_EN.1", "U1.VIN", kind="sense")\nLoop("C_IN1.1"')],
        'Path("VIN") line 52, kind sense: Path line 54 names U1.VIN, this Path\'s last station, and not R_EN.1, its first; a sense Path starts at the station it shares',
    ),
    (
        'field_on_a_poured_net',
        [('Loop("C_IN1.1"', 'Path("GND", "C_OUT1.2", "J_OUT.2", amps=1)\nLoop("C_IN1.1"')],
        'Path("GND") line 52: GND is poured (B.Cu; route_scene.plane_targets: NetReq line 41 is kind power and names GND), and a Path with amps= on a poured ground is a return path, which version 1 does not build',
    ),
    (
        'sense_on_a_poured_net_is_not_offered_as_kelvin',
        [('Loop("C_IN1.1"', 'Path("GND", "C_OUT1.2", "U1.GND", kind="sense")\nLoop("C_IN1.1"')],
        'Path("GND") line 52: GND is poured (B.Cu; route_scene.plane_targets: NetReq line 41 is kind power and names GND), and version 1 does not build a sense path on a poured net: the pour joins U1.GND to GND wherever it reaches, so nothing can keep it off the rest of GND (a Kelvin tap); without kind= the line is not a Kelvin tap either, since U1.GND still reaches the pour (net-path-physics.md §10 A: a sense path on a poured net is after version 1)',
    ),
    (
        'layer_not_on_the_stackup',
        [('Loop("C_IN1.1"', 'Path("FB", "R_FB_TOP.2", "U1.FB", layers=("In1.Cu",))\nLoop("C_IN1.1"')],
        'Path("FB") line 52: In1.Cu: jlcpcb_2l_1oz has F.Cu, B.Cu',
    ),
    (
        'a_link_named_twice',
        [('Loop("C_IN1.1"', 'Path("5V", "C_OUT2.1", "C_OUT1.1", "J_OUT.1", amps=0.5)\nLoop("C_IN1.1"')],
        '5V: Path line 48 and Path line 52 both name C_OUT1.1 -> J_OUT.1; a link carries one width',
    ),
    (
        'currents_must_balance',
        [('Loop("C_IN1.1"', 'Path("5V", "J_OUT.1", "C_OUT2.1", amps=3)\nLoop("C_IN1.1"')],
        "5V: Path line 52 leaves J_OUT.1 with 3 A, and Path line 48 brings 2 A to it, the only Path that enters J_OUT.1; currents at a station must balance, and an off-path branch carries none of a Path's",
    ),
    (
        'loop_step_crosses_nets',
        [('Loop("C_IN1.1", "U1.VIN", "U1.GND", "C_IN1.2")', 'Loop("C_IN1.2", "U1.VIN", "U1.GND", "C_IN1.1")')],
        "Loop line 52: C_IN1.2 -> U1.VIN is neither one net (C_IN1.2 is on GND, U1.VIN on VIN) nor one part; each step of a loop stays on one net or crosses one part; C_IN1's VIN pad is C_IN1.1, station 4 of this loop",
    ),
    (
        'loop_step_crosses_nets_from_the_ic_side',
        [('Loop("C_IN1.1", "U1.VIN", "U1.GND", "C_IN1.2")', 'Loop("C_IN1.1", "U1.GND", "U1.VIN", "C_IN1.2")')],
        "Loop line 52: C_IN1.1 -> U1.GND is neither one net (C_IN1.1 is on VIN, U1.GND on GND) nor one part; each step of a loop stays on one net or crosses one part; C_IN1's GND pad is C_IN1.2, station 4 of this loop",
    ),
    (
        'loop_that_crosses_no_part',
        [('Loop("C_IN1.1", "U1.VIN", "U1.GND", "C_IN1.2")', 'Loop("C_IN1.1", "U1.VIN", "J_IN.1")')],
        'Loop line 52: its steps cross 0 parts and stay on VIN; a loop goes out through one part and back through another, so its pads are on two nets at least and it crosses two parts at least; C_IN1.2 (GND), J_IN.2 (GND) are the other side',
    ),
    (
        'loop_names_a_pad_twice',
        [('Loop("C_IN1.1", "U1.VIN", "U1.GND", "C_IN1.2")', 'Loop("C_IN1.1", "U1.VIN", "U1.GND", "C_IN1.1")')],
        'Loop line 52: C_IN1.1 is pad C_IN1.1, which is already station 1 (C_IN1.1); a loop passes each pad once, and a pad named twice closes the loop early around nothing',
    ),
    # Refutation round 2: each of these was accepted silently, and the build went on as if it had been honoured.
    (
        'station_named_twice',
        [('Path("5V", "L1.2", "C_OUT1.1", "J_OUT.1", amps=2)', 'Path("5V", "L1.2", "C_OUT1.1", "L1.2", amps=2)')],
        'Path("5V") line 48: L1.2 is station 1 (L1.2) again; a Path names each station once',
    ),
    (
        'netreq_keep_clear_of_names_no_net',
        [('NetReq("FB", kind="analog")', 'NetReq("FB", kind="analog", keep_clear_of="SWN")')],
        "NetReq(\"FB\") line 44: keep_clear_of names 'SWN', and no net on this board matches it",
    ),
]


@pytest.mark.parametrize("name,edits,message", REFUSALS, ids=[r[0] for r in REFUSALS])
def test_each_refusal_states_the_fact_and_proposes_no_edit(tmp_path: Path, name, edits, message):
    """docs/direction.md §7b (owner, 2026-10-01): each refusal of §7 is the whole message, a statement of
    what failed and why; none ends in a rewritten line any more."""
    cs = _cs(_buck(tmp_path, edits, name=name))
    assert cs.refusals == (message,), cs.refusals


def test_a_current_path_needs_no_net_kind_and_may_name_every_pad(tmp_path: Path):
    """docs/physics-subjects.md PR 1 (KD 2): `amps=` and `temp_rise_c=` on a Path are accepted whatever its
    net's kind, and a Path with a current may name every pad of its net. Each was refused before; the
    sizes are the one current floor (KD 10) whatever the kind."""
    cs = _cs(_buck(tmp_path, [('Loop("C_IN1.1"', 'Path("FB", "R_FB_BOT.1", "U1.FB", amps=0.1)\nLoop("C_IN1.1"')], name="analog"))
    assert cs.refusals == (), cs.refusals
    (lk,) = cs.links_of("FB")
    assert lk.current is not None and lk.current.amps == 0.1 and lk.width_on("F.Cu") == 0.25, "the floor below 0.2 A on an analog net"
    cs = _cs(_buck(tmp_path, [('Path("SW", "U1.SW", "L1.1", amps=2)', 'Path("SW", "U1.SW", "L1.1", amps=2, temp_rise_c=20)')], name="sw20"))
    assert cs.refusals == (), cs.refusals
    assert cs.link("SW", "U1.2", "L1.1").current.temp_rise_c == 20
    every = 'Path("VIN", "J_IN.1", "C_IN1.1", "C_IN2.1", "U1.VIN", "R_EN.1", amps=1.5)'
    cs = _cs(_buck(tmp_path, [('Path("VIN", "J_IN.1", "C_IN2.1", "C_IN1.1", "U1.VIN", amps=1.36)', every)], name="every"))
    assert cs.refusals == (), cs.refusals
    assert cs.by_net("VIN").off_path == () and cs.link("VIN", "U1.3", "R_EN.1").current.amps == 1.5


def test_loosening_a_default_is_reported_not_refused(tmp_path: Path):
    board = _buck(tmp_path, [('Loop("C_IN1.1"', 'Path("FB", "R_FB_TOP.2", "U1.FB", layers=("F.Cu", "B.Cu"), vias=True)\nLoop("C_IN1.1"')])
    cs = _cs(board)
    assert cs.refusals == ()
    (lk,) = cs.links_of("FB")
    assert lk.layers == ("F.Cu", "B.Cu") and lk.via.allowed and lk.layers_src == "Path line 52" and lk.vias_src == "Path line 52"


def test_a_net_field_on_a_path_is_refused_naming_where_it_belongs(tmp_path: Path):
    board = _buck(tmp_path, [('Path("5V", "L1.2", "C_OUT1.1", "J_OUT.1", amps=2)', 'Path("5V", "L1.2", "C_OUT1.1", "J_OUT.1", amps=2, volts=5)')])
    assert check_board(board) == [
        'Path("5V") line 48: volts= is a property of the whole net: every pad on 5V is one potential; it is a NetReq field (NetReq line 42, which already says volts=5)'
    ]
    board = _buck(tmp_path, [('Path("5V", "L1.2", "C_OUT1.1", "J_OUT.1", amps=2)', 'Path("5V", "L1.2", "C_OUT1.1", "J_OUT.1", amps=2, class_name="Rail")')], name="b2")
    assert check_board(board) == [
        "Path(\"5V\") line 48: class_name= names the net's class, one per net; it is a NetReq field (NetReq line 42)"
    ]


def test_a_pair_path_takes_no_field(tmp_path: Path):
    work = tmp_path / "c3"
    shutil.copytree(EXAMPLES / "c3_usb", work, ignore=shutil.ignore_patterns("layout"))
    board = work / "c3_usb.py"
    write_split(board, board.read_text().replace('Path("USB_DP", "J1.A6", "U3.1", "U1.27")', 'Path("USB_DP", "J1.A6", "U3.1", "U1.27", layers=("F.Cu",))'))
    cs = _cs(board)
    assert cs.refusals == ('Path("USB_DP") line 214: layers= on half of the pair USB_DN/USB_DP; the pair\'s numbers are on Pair line 205, and a Path on a pair net names stations only',), cs.refusals  # E17 (PR 6)


def test_the_series_note_does_not_ask_for_a_path_on_a_poured_net(tmp_path: Path):
    """Refutation round 1, defect 4: the note asked for `Path("GND", ..., amps=)`, which §7 refuses on a
    poured net one step earlier. On a pour it says where the current goes and that v1 does not size it."""
    cs = _cs(_buck(tmp_path, [('Path("VIN", "J_IN.1", "C_IN2.1", "C_IN1.1", "U1.VIN", amps=1.36)', 'Path("VIN", "J_IN.1", "C_IN2.1", "C_IN1.1", "U1.VIN", amps=3)')]))
    assert cs.refusals == (), cs.refusals
    notes = [x for x in cs.lines if x.startswith("GND: J_IN is a two-pad part")]
    assert notes == [
        "GND: J_IN is a two-pad part between VIN and GND; Path line 53 carries 3 A out of J_IN.1, so if J_IN passes DC, J_IN.2 carries 3 A too, "
        "into the GND pour; version 1 does not size a return in a pour (net-path-physics.md §10 A), so nothing checks that J_IN.2's joint to the pour carries 3 A"
    ], [x for x in cs.lines if "two-pad" in x]
    assert not any('Path("GND"' in x for x in cs.lines)


DRAWS = '''from pcbc import *
V3 = Power("3V3"); GND = Ground("GND")
Resistor("R0", "0R", package="1206", mpn="X", lcsc="C1", p1="VIN", p2=V3)
Resistor("R1", "10", package="1206", mpn="X", lcsc="C1", p1=V3, p2=GND)
Resistor("R2", "27", package="1206", mpn="X", lcsc="C1", p1=V3, p2=GND)
Resistor("R3", "68", package="1206", mpn="X", lcsc="C1", p1=V3, p2=GND)
Board(width=40, height=20, layers=2, stackup="jlcpcb_2l_1oz")
NetReq("3V3", kind="power", volts=3.3)
{path}
for i, r in enumerate(["R0", "R1", "R2", "R3"]):
    Place(r, at=(5 + 8 * i, 10))
'''


def _draws(tmp_path: Path, path: str, name: str = "draws") -> Path:
    board = tmp_path / f"{name}.py"
    write_split(board, DRAWS.replace("{path}", path))
    return board


def test_draws_give_each_link_the_sum_beyond_it(tmp_path: Path):
    """docs/physics-subjects.md §5, PR 2: `amps={station: A}` — each named station draws its current, the
    first supplies their sum, and by KCL each link carries the sum of the draws beyond it, each sized, and
    printed, at its own current (the KD 10 floor: 0.4 mm at 0.2 A or more, 0.25 below)."""
    from pcbc.paths import link_amps

    cs = _cs(_draws(tmp_path, 'Path("3V3", "R0.2", "R1.1", "R2.1", "R3.1", amps={"R1.1": 0.3, "R2.1": 0.12, "R3.1": 0.05})'))
    assert cs.refusals == (), cs.refusals
    (p,) = cs.by_net("3V3").paths
    assert p.amps == 0.47 and p.draws == (("R1.1", 0.3), ("R2.1", 0.12), ("R3.1", 0.05))
    assert link_amps(p) == (0.47, 0.17, 0.05)
    got = [(lk.key, lk.current.amps, lk.width_on("F.Cu")) for lk in p.links]
    assert got == [("R0.2~R1.1", 0.47, 0.4), ("R1.1~R2.1", 0.17, 0.25), ("R2.1~R3.1", 0.05, 0.25)], got
    line = next(x for x in cs.lines if x.startswith("3V3: path R0.2 -> R1.1"))
    assert line.startswith("3V3: path R0.2 -> R1.1 -> R2.1 -> R3.1 (Path line 9): draws R1.1 0.3 A, R2.1 0.12 A, R3.1 0.05 A, 0.47 A from R0.2; link R0.2 -> R1.1 0.47 A: width 0.4 mm"), line
    assert "; link R2.1 -> R3.1 0.05 A: width 0.25 mm" in line, line
    # A number is the one-draw case: its last station draws it all, on every link.
    cs = _cs(_draws(tmp_path, 'Path("3V3", "R0.2", "R1.1", "R2.1", "R3.1", amps=0.47)', name="number"))
    (p,) = cs.by_net("3V3").paths
    assert link_amps(p) == (0.47, 0.47, 0.47) and {lk.current.amps for lk in p.links} == {0.47}


@pytest.mark.parametrize(
    "path,message",
    [
        (
            'Path("3V3", "R0.2", "R1.1", "R2.1", amps={"R3.1": 0.1})',
            'Path("3V3") line 9: amps= names R3.1, which is not a station of this Path (R0.2, R1.1, R2.1)',
        ),
        (
            'Path("3V3", "R0.2", "R1.1", "R2.1", amps={"R0.2": 0.1, "R2.1": 0.1})',
            'Path("3V3") line 9: amps= names R0.2, its first station, where the current enters; a station in amps= is one the current leaves by',
        ),
    ],
    ids=["not_a_station", "the_first_station"],
)
def test_e11_a_draw_is_at_a_station_other_than_the_first(tmp_path: Path, path: str, message: str):
    cs = _cs(_draws(tmp_path, path))
    assert cs.refusals == (message,), cs.refusals


def test_a_draw_is_greater_than_zero(tmp_path: Path):
    assert check_board(_draws(tmp_path, 'Path("3V3", "R0.2", "R1.1", "R2.1", amps={"R1.1": 0.1, "R2.1": 0})')) == [
        "line 9: Path(\"3V3\"): amps['R2.1']=0 is not physical; amps['R2.1'] must be greater than 0"
    ]


def test_draws_balance_at_a_shared_station_by_what_arrives(tmp_path: Path):
    """The balance rule (§3.3) with draws: a draws-Path brings to its last station only that station's draw."""
    two = 'Path("3V3", "R0.2", "R1.1", "R2.1", amps={"R1.1": 0.3, "R2.1": 0.1})\nPath("3V3", "R2.1", "R3.1", amps=0.2)'
    cs = _cs(_draws(tmp_path, two))
    assert cs.refusals == (
        "3V3: Path line 10 leaves R2.1 with 0.2 A, and Path line 9 brings 0.1 A to it, the only Path that enters R2.1; currents at a station must balance, "
        "and an off-path branch carries none of a Path's",
    ), cs.refusals


@pytest.mark.parametrize(
    "first,message",
    [
        (
            'Path("3V3", "R0.2", "R1.1", "R2.1", amps={"R1.1": 0.05, "R2.1": 0.1})',
            "3V3: Path line 10 leaves R1.1 with 1.5 A; R1.1 is a middle station of Path line 9, which draws 0.05 A there and sizes its link "
            "R0.2 -> R1.1 for 0.15 A, the draws beyond that link; currents at a station must balance",
        ),
        (
            'Path("3V3", "R0.2", "R1.1", "R2.1", amps=0.1)',
            "3V3: Path line 10 leaves R1.1 with 1.5 A; R1.1 is a middle station of Path line 9, which draws 0 A there and sizes its link "
            "R0.2 -> R1.1 for 0.1 A, the draws beyond that link; currents at a station must balance",
        ),
    ],
    ids=["draws", "number"],
)
def test_balance_holds_at_a_middle_station(tmp_path: Path, first: str, message: str):
    """§5: "the balance rule holds at every shared station", a middle one included. Refutation physics r1
    (`midbranch`): a Path leaving the middle station R1.1 with 1.5 A was accepted, the link R0.2 -> R1.1 was
    sized 0.25 mm for 0.15 A, and the emitted board's own DC solve put 1.65 A through it (0.875 A rated)."""
    cs = _cs(_draws(tmp_path, f'{first}\nPath("3V3", "R1.1", "R3.1", amps=1.5)'))
    assert cs.refusals == (message,), cs.refusals
    # Within the middle station's draw it balances: 0.05 A leaves where 0.05 A is drawn.
    cs = _cs(_draws(tmp_path, f'{first}\nPath("3V3", "R1.1", "R3.1", amps=0.05)', name="within"))
    if "amps=0.1)" in first:
        assert cs.refusals and "draws 0 A there" in cs.refusals[0], cs.refusals
    else:
        assert cs.refusals == (), cs.refusals


# docs/physics-subjects.md §7: a property written on a subject that is not its home is refused with the facts,
# naming the line (direction.md §7b). The NetReq twin of `_explain_path_keyword`; one row per error id.
NET_KINDS_TEXT = "generic, power, switch_node, analog, clock or i2c"  # the six net kinds (PR 7)

NETREQ_REFUSALS = [
    (
        "E1_amps",
        [('NetReq("VIN", kind="power", volts=12)', 'NetReq("VIN", kind="power", volts=12, amps=1.36)')],
        'NetReq("VIN") line 40: amps= is a current; a current flows in the copper between two pads, and a NetReq states what holds at every pad '
        "of VIN (U1.VIN, J_IN.1, C_IN1.1, C_IN2.1, R_EN.1); amps= is a Path field",
    ),
    # E4 (PR 5): the copper fields leave the net.
    *[
        (
            f"E4_{kw}",
            [('NetReq("FB", kind="analog")', f'NetReq("FB", kind="analog", {kw}={val})')],
            f'NetReq("FB") line 44: {kw}= is about the copper between two pads ({clause}), and FB\'s pads are joined by copper the router '
            f"lays link by link; {kw}= is a Path field",
        )
        for kw, val, clause in (
            ("vias", "False", "a via is a piece of that copper"),
            ("vias_max", "2", "the vias that copper passes"),
            ("layers", '("F.Cu",)', "the layers that copper runs on"),
            ("reference", '"B.Cu"', "the plane under that copper, one per layer"),
            ("z_se_ohm", "50", "an impedance is that copper's cross-section over its plane"),
            ("length_mm", "20", "a length runs between two pads"),
        )
    ],
    # E5 / E6 (PR 6): what holds for two or more paths at once leaves the net.
    *[
        (
            f"E5_{kw}",
            [('NetReq("FB", kind="analog")', f'NetReq("FB", kind="analog", {kw}={val})')],
            f'NetReq("FB") line 44: {kw}= is a Pair field: it holds for two paths at once; a NetReq takes kind {NET_KINDS_TEXT}',
        )
        for kw, val in (("pair", "True"), ("z_diff_ohm", "90"), ("uncoupled_mm", "2"))
    ],
    (
        "E6_match_mm",
        [('NetReq("FB", kind="analog")', 'NetReq("FB", kind="analog", match_mm=1)')],
        'NetReq("FB") line 44: match_mm= compares the lengths of two or more paths; it is a Pair or Bus field',
    ),
    (
        "E6_clock",
        [('NetReq("FB", kind="analog")', 'NetReq("FB", kind="analog", clock="FB")')],
        'NetReq("FB") line 44: clock= names the path a Bus is matched to; it is a Bus field',
    ),
    # PR 7: loops and keep-aways on their subjects (E7, E8, E12, E14, E15 autoroute).
    (
        "E7_loop_mm2",
        [('NetReq("SW", kind="switch_node", max_mm=6)', 'NetReq("SW", kind="switch_node", max_mm=6, loop_mm2=25)')],
        'NetReq("SW") line 43: loop_mm2= budgets the area of a loop, which runs through two nets and two parts at least; it is a Loop field',
    ),
    *[
        (
            f"E8_{kw}",
            [('Path("5V", "C_OUT1.1", "R_FB_TOP.1", kind="feedback")', 'Path("5V", "C_OUT1.1", "R_FB_TOP.1", kind="feedback", ' + kw + "=" + val + ")")],
            f'Path("5V") line 50: {kw}= keeps a node\'s copper from another node\'s, and every branch of 5V is the one node 5V; it is a NetReq field',
        )
        for kw, val in (("keep_clear_of", '"SW"'), ("keep_clear_mm", "2"))
    ],
    (
        "E12_autoroute",
        [('NetReq("SW", kind="switch_node", max_mm=6)', 'NetReq("SW", kind="switch_node", max_mm=6, autoroute=True)')],
        'NetReq("SW") line 43: autoroute= was the old router\'s switch, and nothing reads it as a requirement',
    ),
    (
        "E15_autoroute_on_a_path",
        [('Path("5V", "L1.2", "C_OUT1.1", "J_OUT.1", amps=2)', 'Path("5V", "L1.2", "C_OUT1.1", "J_OUT.1", amps=2, autoroute=False)')],
        'Path("5V") line 48: autoroute= was the old router\'s switch, and nothing reads it as a requirement',
    ),
    *[
        (
            f"E14_{kind}",
            [('NetReq("FB", kind="analog")', f'NetReq("FB", kind="{kind}")')],
            f'NetReq("FB") line 44: kind="{kind}" is a Path kind: it names a branch of a net that carries a measurement; a NetReq takes kind {NET_KINDS_TEXT}',
        )
        for kind in ("sense", "feedback")
    ],
    (
        "E2_temp_rise_c",
        [('NetReq("VIN", kind="power", volts=12)', 'NetReq("VIN", kind="power", volts=12, temp_rise_c=20)')],
        "NetReq(\"VIN\") line 40: temp_rise_c= is the rise of copper under a current, and a current is a Path's; temp_rise_c= is a Path field",
    ),
]


@pytest.mark.parametrize("name,edits,message", NETREQ_REFUSALS, ids=[r[0] for r in NETREQ_REFUSALS])
def test_a_field_on_the_wrong_subject_is_refused_with_the_facts(tmp_path: Path, name, edits, message):
    board = _buck(tmp_path, edits, name=name)
    assert check_board(board) == [message]
    from pcbc.build import build_job

    assert build_job(board, upto="check")["error"] == message, "the build stops on the same statement"


def test_e3_a_ground_net_takes_no_volts(tmp_path: Path):
    """docs/physics-subjects.md PR 4 (E3): a `Ground()` net is the 0 V its domain's voltages are measured from,
    so `volts=` on a NetReq that names one is false of it. One refusal per ground net the line names, citing
    where the board declares it Ground()."""
    cs = _cs(_buck(tmp_path, [('NetReq("GND", kind="power")', 'NetReq("GND", kind="power", volts=12)')], name="gnd"))
    assert cs.refusals == (
        'NetReq("GND") line 41: volts=12 holds for every net the line names, and GND is Ground("GND") (buck.py line 8), '
        "the 0 V its domain's voltages are measured from",
    ), cs.refusals
    blinky = tmp_path / "blinky"
    shutil.copytree(EXAMPLES / "blinky", blinky, ignore=shutil.ignore_patterns("layout"))
    text = (blinky / "blinky.py").read_text()
    old = 'NetReq("VCC", kind="power", volts=3.3)\nNetReq("GND", kind="power")\n'
    assert old in text
    write_split(blinky / "blinky.py", text.replace(old, 'NetReq("VCC", "GND", kind="power", volts=3.3)\n'))
    cs = _cs(blinky / "blinky.py")
    assert cs.refusals == (
        'NetReq("VCC", "GND") line 30: volts=3.3 holds for every net the line names, and GND is Ground("GND") (blinky.py line 4), '
        "the 0 V its domain's voltages are measured from",
    ), cs.refusals


def test_f1_creepage_holds_between_two_nets_of_one_class(tmp_path: Path):
    """docs/physics-subjects.md §3.9, finding F1 (PR 4): two nets of one NetReq at 60 V or more get the class's
    creepage between them, in KiCad's rules and in the router's clearance table. Before, the KiCad rule was
    `A.hasNetclass(X) && !B.hasNetclass(X)` and the table skipped a same-class pair, so 400 V nets that may be
    800 V apart had none between them."""
    from pcbc.compile import compile_design
    from pcbc.constraints import clearance_table

    board = tmp_path / "hv.py"
    write_split(board, "from pcbc import *\n"
        'GND = Ground("GND")\n'
        'Resistor("R1", "1M", package="1206", mpn="X", lcsc="C1", p1="HV_P", p2="HV_N")\n'
        'Resistor("R2", "1M", package="1206", mpn="X", lcsc="C1", p1="HV_N", p2=GND)\n'
        'Board(width=40, height=25, layers=2, stackup="jlcpcb_2l_1oz")\n'
        'NetReq("HV_P", "HV_N", kind="power", volts=400)\n'
        'Place("R1", at=(10, 10)); Place("R2", at=(25, 10))\n')
    job = compile_design(load_board(board))
    cs = job.constraints
    creep = cs.by_net("HV_P").voltage.creepage_mm.value
    assert creep > 0 and cs.by_net("HV_P").class_name == cs.by_net("HV_N").class_name == "Power"
    rules = {r.name: (r.constraint, r.condition) for r in job.dru}
    assert rules["creepage_power_within"] == (
        f"(constraint creepage (min {creep:g}mm))", "A.hasNetclass('Power') && B.hasNetclass('Power') && A.NetName != B.NetName"
    ), rules
    assert clearance_table(cs).between("HV_P", "HV_N") == (creep, "creepage Power")


def _node(tmp_path: Path, extra: str, name: str = "node") -> Path:
    work = tmp_path / name
    shutil.copytree(EXAMPLES / "node", work, ignore=shutil.ignore_patterns("layout"))
    board = work / "node.py"
    text = board.read_text()
    anchor = 'Thermal("U1.49", watts=0.35, across_planes=True)'
    assert anchor in text
    write_split(board, text.replace(anchor, extra + "\n" + anchor, 1))
    return board


def test_e9_a_paths_length_is_length_mm_and_its_span_is_the_nets(tmp_path: Path):
    """PR 5 (E9): `max_mm=` is the span of all of a net's pads, a NetReq field; on a Path it is refused."""
    cs = _cs(_buck(tmp_path, [('Path("5V", "C_OUT1.1", "R_FB_TOP.1", kind="feedback")', 'Path("5V", "C_OUT1.1", "R_FB_TOP.1", kind="feedback", max_mm=10)')]))
    assert cs.refusals == (
        "Path(\"5V\") line 50: max_mm= is the span of all of 5V's pads, a NetReq field; a Path's own length between its stations is its length_mm=",
    ), cs.refusals


def test_e10_a_reference_is_the_plane_under_one_layer(tmp_path: Path):
    """PR 5 (E10, N14): a return plane belongs to one layer's copper; on node F.Cu returns in In1.Cu and B.Cu in
    In2.Cu, so `reference=` on a Path that may run on both is true of one and false of the other."""
    cs = _cs(_node(tmp_path, 'Path("T_DIV", "R_NTC.2", "U5.+", reference="In1.Cu")'))
    assert cs.refusals == ('Path("T_DIV") line 76: reference="In1.Cu" is the plane under F.Cu only, and this Path may run on F.Cu and B.Cu',), cs.refusals
    cs = _cs(_node(tmp_path, 'Path("T_DIV", "R_NTC.2", "U5.+", reference="In1.Cu", layers=("F.Cu",))', name="one"))
    assert cs.refusals == (), cs.refusals
    (lk,) = cs.links_of("T_DIV")
    assert lk.reference == (("F.Cu", "In1.Cu"),)


def test_copper_fields_are_a_paths_and_size_its_links(tmp_path: Path):
    """PR 5 (N4, N10, N12): z_se_ohm=, length_mm= and vias_max= on a Path size and budget its links; the
    net keeps the class floor on every layer no plane takes and the stackup via."""
    cs = _cs(_buck(tmp_path, [('Loop("C_IN1.1"', 'Path("FB", "R_FB_TOP.2", "U1.FB", z_se_ohm=50, layers=("F.Cu",), length_mm=12, vias_max=1)\nLoop("C_IN1.1"')]))
    assert cs.refusals == (), cs.refusals
    (lk,) = cs.links_of("FB")
    # C.5 on two layers: the CPWG pour gap is max(0.3, 2 x FB's 0.2 mm analog clearance) = 0.4.
    assert lk.width_on("F.Cu") == 1.7779 and lk.via.count_max == 1 and dict(lk.reference) == {"F.Cu": "B.Cu pour"}
    (p,) = cs.by_net("FB").paths
    assert p.length_mm == 12
    fb = cs.by_net("FB")
    assert (fb.width_mm.value, fb.via.allowed, fb.layers, (fb.via.diameter_mm, fb.via.drill_mm)) == (0.16, True, ("F.Cu", "B.Cu"), (0.5, 0.3)), "§3.5: analog keeps no width, via or layer"
    line = next(x for x in cs.lines if x.startswith("FB: path R_FB_TOP.2 -> U1.FB"))
    assert "length 12 mm (Path line 52)" in line and "F.Cu over B.Cu pour: 50 ohm" in line, line


def test_a_current_the_impedance_width_cannot_carry_is_refused(tmp_path: Path):
    """PR 5: the current-against-impedance check is per link of a Path that writes both."""
    cs = _cs(_buck(tmp_path, [('Loop("C_IN1.1"', 'Path("FB", "R_FB_BOT.1", "U1.FB", z_se_ohm=120, layers=("F.Cu",), amps=1.5)\nLoop("C_IN1.1"')]))
    assert len(cs.refusals) == 1 and cs.refusals[0].startswith('Path("FB") line 52: amps=1.5 needs '), cs.refusals
    assert "and z_se_ohm=120 on the same line holds " in cs.refusals[0], cs.refusals


def test_e22_a_via_on_a_link_whose_path_allows_none(monkeypatch, tmp_path: Path):
    """PR 5 (E22): a via on a no-via link is named by its link and its Path line (a net writes no vias=)."""
    from pcbc.compile import compile_design
    from pcbc.copper import vias_on_no_via_nets
    from pcbc.route_emit import Sidecar

    job = compile_design(load_board(_buck(tmp_path)))
    item = {"key": ["via", [20.0, 10.0], ["F.Cu", "B.Cu"]], "uuid": "u", "reason": "route", "net": "5V", "owner": "C_OUT1.1~R_FB_TOP.1", "w": 0.5, "drill": 0.3}
    monkeypatch.setattr("pcbc.layout_job.roles_doc", lambda text, refusals=(): Sidecar(items=[item], refusals=[]))
    assert vias_on_no_via_nets(job, "") == ["5V: link C_OUT1.1~R_FB_TOP.1 has 1 via(s), and Path line 50 is kind feedback, whose default is no vias"]


def test_a_paths_routed_length_is_measured_against_its_length_mm(monkeypatch, tmp_path: Path):
    """PR 5 (N10): the airwire bounds a path's length at placement; the copper its links own on the emitted board
    is measured against the same budget, and over it is a gate failure stating the numbers."""
    from pcbc.compile import compile_design
    from pcbc.route_emit import Sidecar
    from pcbc.route_verify import path_length_lines, path_lengths

    cs = compile_design(load_board(_buck(tmp_path))).constraints
    seg = lambda a, b: {"key": ["seg", "F.Cu", list(a), list(b), 0.2], "uuid": "s", "reason": "route", "net": "5V", "owner": "C_OUT1.1~R_FB_TOP.1", "w": 0.2, "drill": 0.0}  # noqa: E731
    monkeypatch.setattr("pcbc.layout_job.roles_doc", lambda text, refusals=(): Sidecar(items=[seg((0, 0), (10, 0)), seg((10, 0), (10, 6))], refusals=[]))
    rows = path_lengths("", cs)
    assert [(r["net"], r["line"], r["routed_mm"], r["budget_mm"]) for r in rows] == [("5V", 50, 16.0, 15.0)]
    assert path_length_lines(rows) == [
        "5V path C_OUT1.1 -> R_FB_TOP.1 (Path line 50): 16 mm of routed copper between its stations, over its length_mm=15 (kind feedback's default)"
    ]


# ---------------------------------------------------------------------------------------------
# PR 6: Pair and Bus own their numbers (docs/physics-subjects.md §7 E5, E6, E15, E17, E18, E19)
# ---------------------------------------------------------------------------------------------


def _c3(tmp_path: Path, edits: list[tuple[str, str]], name: str = "c3") -> Path:
    work = tmp_path / name
    shutil.copytree(EXAMPLES / "c3_usb", work, ignore=shutil.ignore_patterns("layout"))
    board = work / "c3_usb.py"
    edit_board(board, edits, once=True)
    return board


PAIR_LINE = 'Pair("USB_DP", "USB_DN", kind="usb_hs", z_diff_ohm=90)'


def test_the_usb_pair_on_a_netreq_is_refused_with_the_docs_text(tmp_path: Path):
    """E5, the example of §7 verbatim (c3_usb line 205 as it was written before PR 6)."""
    board = _c3(tmp_path, [(PAIR_LINE, 'NetReq("USB_DP", "USB_DN", kind="usb_hs", z_diff_ohm=90, pair=True)')])
    msg = (
        'NetReq("USB_DP", "USB_DN") line 205: kind="usb_hs" is a Pair kind and z_diff_ohm=, pair= are Pair fields: they hold for two '
        f"paths at once; a NetReq takes kind {NET_KINDS_TEXT}"
    )
    assert check_board(board) == [msg]
    from pcbc.build import build_job

    assert build_job(board, upto="check")["error"] == msg


def test_a_pair_or_bus_kind_on_a_netreq_is_refused(tmp_path: Path):
    """E5 / E6 with no pair field written: the kind alone is the pair's or the bus's (constraints)."""
    board = _c3(tmp_path, [(PAIR_LINE, 'NetReq("USB_DP", "USB_DN", kind="usb_hs")')])
    assert check_board(board) == [
        f'NetReq("USB_DP", "USB_DN") line 205: kind="usb_hs" is a Pair kind: it holds for two paths at once; a NetReq takes kind {NET_KINDS_TEXT}'
    ]
    board = _c3(tmp_path, [('NetReq("EN", "BOOT", "LED", "LED_A", "CC1", "CC2", kind="digital")', 'NetReq("EN", "BOOT", "LED", "LED_A", "CC1", "CC2", kind="spi")')], name="spi")
    assert check_board(board) == [
        f'NetReq("EN", "BOOT", "LED", "LED_A", "CC1", "CC2") line 211: kind="spi" is a Bus kind: it times two or more paths together; a NetReq takes kind {NET_KINDS_TEXT}'
    ]


def test_an_unknown_net_kind_lists_the_net_kinds_and_the_aliases(tmp_path: Path):
    """E19: the list is the net kinds only; usb_hs and spi are not among them."""
    board = _c3(tmp_path, [('NetReq("EN", "BOOT", "LED", "LED_A", "CC1", "CC2", kind="digital")', 'NetReq("EN", "BOOT", "LED", "LED_A", "CC1", "CC2", kind="usb")')])
    assert check_board(board) == [
        'NetReq("EN") line 211: unknown kind \'usb\'; known: analog, clock, generic, i2c, power, switch_node, and the aliases digital and default'
    ]


def test_a_pair_kind_on_a_path_is_refused(tmp_path: Path):
    """E18: `Path(kind="usb_hs")` names a Pair kind; a Path's kind names a sense branch. (On a pair
    member's own Path E17 speaks first: a Path on a pair net names stations only.)"""
    for kind, what in (("usb_hs", "Pair"), ("spi", "Bus")):
        board = _c3(tmp_path, [('Path("3V3", "U2.VOUT", "U1.3V3", amps=0.5)', f'Path("3V3", "U2.VOUT", "U1.3V3", kind="{kind}")')], name=kind)
        assert _cs(board).refusals == (
            f'Path("3V3") line 210: kind="{kind}" is a {what} kind; on a Path, kind= names a sense branch and takes "feedback" or "sense"',
        )


@pytest.mark.parametrize(
    "kw,val,tail",
    [
        ("z_diff_ohm", "90", "holds for two paths at once; it is a Pair field (Pair line 205)"),
        ("uncoupled_mm", "2", "holds for two paths at once; it is a Pair field (Pair line 205)"),
        ("match_mm", "0.5", "holds for two paths at once; it is a Pair or Bus field (Pair line 205)"),
        ("clock", '"USB_DP"', "names the path a Bus is matched to; it is a Bus field (Pair line 205)"),
    ],
)
def test_a_group_field_on_a_path_names_its_group(tmp_path: Path, kw, val, tail):
    """E15 (PR 6): a field that holds for two or more paths on a Path is refused naming the group line."""
    board = _c3(tmp_path, [('Path("USB_DP", "J1.A6", "U3.1", "U1.27")', f'Path("USB_DP", "J1.A6", "U3.1", "U1.27", {kw}={val})')], name=kw)
    assert check_board(board) == [f'Path("USB_DP") line 214: {kw}= {tail}']


def test_pf_max_on_a_path_is_the_nodes_charge_budget(tmp_path: Path):
    """E15 (N17): `pf_max=` is the charge budget of the whole node."""
    board = _buck(tmp_path, [('Path("5V", "L1.2", "C_OUT1.1", "J_OUT.1", amps=2)', 'Path("5V", "L1.2", "C_OUT1.1", "J_OUT.1", amps=2, pf_max=100)')])
    assert check_board(board) == [
        'Path("5V") line 48: pf_max= is the charge budget of the whole node: every pin and stub on 5V; it is a NetReq field (NetReq line 42)'
    ]


def test_a_preset_pair_line_compiles_as_the_usb_hs_netreq_did(tmp_path: Path):
    """PR 6: c3_usb's Pair line gives the numbers its NetReq line gave (target 90 +-15 %, skew 0.5 and
    uncoupled 2 from the preset, at most 2 vias, class USB), each now citing the Pair line; a field the
    line writes overrides the preset and says so."""
    cs = _cs(_c3(tmp_path, []))
    dp = cs.by_net("USB_DP")
    assert dp is not None and dp.class_name == "USB" and dp.pair is not None and dp.pair.partner == "USB_DN"
    assert dp.pair.tolerance_pct == 15 and dp.pair.skew_mm.value == 0.5 and dp.pair.uncoupled_mm.value == 2.0 and dp.pair.line == 205
    assert "USB_DP: skew 0.5 mm (preset usb_hs; TI usb_layout_basics) [soft: warning in R1]" in cs.lines
    cs = _cs(_c3(tmp_path, [(PAIR_LINE, PAIR_LINE[:-1] + ", match_mm=0.3, length_mm=40)")], name="over"))
    assert "USB_DP: skew 0.3 mm (Pair line 205; overrides preset usb_hs 0.5) [soft: warning in R1]" in cs.lines
    assert all(p.length_mm == 40 for p in cs.by_net("USB_DP").paths), "Pair(length_mm=) is each member Path's budget (G1)"


def test_a_bus_kind_spi_needs_its_clock_and_a_bare_bus_its_match(tmp_path: Path):
    from pcbc.language import Bus, reset

    reset()
    with pytest.raises(ValueError, match=r'Bus\("SCK"\): kind="spi" is matched to its clock, and this Bus gives no clock='):
        Bus("SCK", "MOSI", kind="spi")
    with pytest.raises(ValueError, match=r'Bus\("SCK"\): match_mm= is the skew budget a Bus holds its members to, and this Bus gives none'):
        Bus("SCK", "MOSI")
    assert Bus("SCK", "MOSI", kind="spi", clock="SCK").match_mm == 2.5


def test_a_bus_member_with_no_path_is_refused_e13(tmp_path: Path):
    """E13 for a Bus: GND has many pads and no Path, so the length the bus compares is not defined.
    3V3's one Path (line 210) defines its member, so it is not named."""
    board = _c3(tmp_path, [(PAIR_LINE, PAIR_LINE + '; Bus("3V3", "GND", match_mm=1)')])
    refusals = _cs(board).refusals
    assert len(refusals) == 1 and refusals[0].startswith("Bus line 205: GND has "), refusals
    assert refusals[0].endswith("and 0 Paths name its stations, so the length the bus compares is not defined"), refusals


# ---------------------------------------------------------------------------------------------
# PR 8: poured rails declare draws (docs/physics-subjects.md §5 "On a poured rail", E23)
# ---------------------------------------------------------------------------------------------

NODE_3V3 = 'Path("3V3", "U2.VOUT", "J_LOAD.1", amps=1)'


def _node_edit(tmp_path: Path, edits: list[tuple[str, str]] = (), name: str = "node") -> Path:
    work = tmp_path / name
    shutil.copytree(EXAMPLES / "node", work, ignore=shutil.ignore_patterns("layout"))
    board = work / "node.py"
    edit_board(board, edits, once=True)
    return board


def test_a_current_on_a_poured_rail_is_its_stations_joints(tmp_path: Path):
    """PR 8: node's 1 A on the 3V3 pour (In2.Cu). The pour is the copper between the stations (not rated: pour), so
    the Path's links carry nothing of their own; U2.VOUT's SMD joint is ceil(1 / 0.527) = 2 stackup vias and
    J_LOAD.1's is its own 1 mm barrel (1.693 A at 10 C)."""
    cs = _cs(_node_edit(tmp_path))
    assert cs.refusals == (), cs.refusals
    (p,) = [p for p in cs.by_net("3V3").paths]
    assert p.pour == "In2.Cu" and not p.fielded and all(lk.current is None for lk in p.links)
    assert [(j.station, j.amps, j.supplies, j.vias, j.barrel_mm) for j in p.joints] == [("U2.VOUT", 1.0, True, 2, None), ("J_LOAD.1", 1.0, False, 0, 1.0)]
    assert (
        "3V3: path U2.VOUT -> J_LOAD.1 (Path line 79): the 3V3 pour on In2.Cu is the copper between its stations (not rated: pour); joints to the pour: "
        "U2.VOUT supplies 1 A: 2 vias 0.35/0.2 mm (via_barrel 0.527 A each at 10 C); J_LOAD.1 draws 1 A: its own barrel (1 mm drill; via_barrel 1.693 A at 10 C)"
    ) in cs.lines


def test_draws_on_a_poured_rail_size_each_stations_joint(tmp_path: Path):
    """PR 8: `amps={station: A}` on a pour: each station's joint carries its own draw, the first station the sum."""
    cs = _cs(_node_edit(tmp_path, [(NODE_3V3, 'Path("3V3", "U2.VOUT", "U1.3V3", "J_LOAD.1", amps={"U1.3V3": 0.5, "J_LOAD.1": 1})')]))
    assert cs.refusals == (), cs.refusals
    (p,) = cs.by_net("3V3").paths
    assert [(j.station, j.amps, j.vias) for j in p.joints] == [("U2.VOUT", 1.5, 3), ("U1.3V3", 0.5, 1), ("J_LOAD.1", 1.0, 0)]


def test_e23_a_barrel_short_of_its_draw_is_refused(tmp_path: Path):
    """E23 (PR 8) at compile: a through-hole station's joint is its own barrel, which cannot take a second hole."""
    cs = _cs(_node_edit(tmp_path, [(NODE_3V3, 'Path("3V3", "U2.VOUT", "J_LOAD.1", amps=2)')]))
    assert cs.refusals == (
        'Path("3V3") line 79: J_LOAD.1 draws 2 A, and its joint to the 3V3 pour on In2.Cu is its own barrel (1 mm drill; via_barrel 1.693 A at 10 C)',
    ), cs.refusals


def test_e23_a_tap_short_of_its_draw_fails_the_gate(monkeypatch, tmp_path: Path):
    """E23 (PR 8) on the emitted board: U2.VOUT supplies 1 A and its joint (the `pcbc:tap:U2.5` vias) has one
    0.35/0.2 via, 0.527 A; with two it carries 1.054 A and passes."""
    from pcbc.compile import compile_design
    from pcbc.route_emit import Sidecar
    from pcbc.route_verify import pour_joint_lines, pour_joints

    cs = compile_design(load_board(_node_edit(tmp_path))).constraints
    via = lambda x: {"key": ["via", [x, 10.0], ["F.Cu", "B.Cu"]], "uuid": f"v{x}", "reason": "tap", "net": "3V3", "owner": "U2.5", "w": 0.35, "drill": 0.2}  # noqa: E731
    monkeypatch.setattr("pcbc.layout_job.roles_doc", lambda text, refusals=(): Sidecar(items=[via(20.0)], refusals=[]))
    assert pour_joint_lines(pour_joints("", cs)) == [
        'Path("3V3") line 79: U2.VOUT supplies 1 A, and its joint to the 3V3 pour on In2.Cu is 1 via (0.35/0.2 mm; via_barrel 0.527 A at 10 C)'
    ]
    monkeypatch.setattr("pcbc.layout_job.roles_doc", lambda text, refusals=(): Sidecar(items=[via(20.0), via(21.0)], refusals=[]))
    rows = pour_joints("", cs)
    assert pour_joint_lines(rows) == [] and [(r["station"], r["vias"], r["carries"]) for r in rows] == [("U2.VOUT", 2, 1.054)]


def test_a_return_path_in_a_poured_ground_stays_refused(tmp_path: Path):
    """PR 8: a rail's draws are declared; a ground's return current is not (`net-path-physics.md` §10 A)."""
    cs = _cs(_node_edit(tmp_path, [(NODE_3V3, NODE_3V3 + '\nPath("GND", "U2.GND", "C_3V3.2", amps=1)')]))
    gnd_refusal = [r for r in cs.refusals if r.startswith('Path("GND")')]
    assert gnd_refusal and gnd_refusal[0].endswith("and a Path with amps= on a poured ground is a return path, which version 1 does not build"), cs.refusals


def test_a_copper_field_on_a_poured_current_path_is_refused(tmp_path: Path):
    """PR 8: on a pour the copper between the stations is the pour, so a track field describes nothing."""
    cs = _cs(_node_edit(tmp_path, [(NODE_3V3, 'Path("3V3", "U2.VOUT", "J_LOAD.1", amps=1, layers=("F.Cu",))')]))
    assert cs.refusals == ('Path("3V3") line 79: layers= describes the copper between two stations, and on the 3V3 pour (In2.Cu) that copper is the pour',), cs.refusals


POUR_TWO = '''from pcbc import *
V3 = Power("3V3"); GND = Ground("GND")
Resistor("R0", "0R", package="1206", mpn="X", lcsc="C1", p1="VIN", p2=V3)
Resistor("R1", "10", package="1206", mpn="X", lcsc="C1", p1=V3, p2=GND)
Resistor("R2", "27", package="1206", mpn="X", lcsc="C1", p1=V3, p2=GND)
Resistor("R3", "68", package="1206", mpn="X", lcsc="C1", p1=V3, p2=GND)
Resistor("R5", "1k", package="1206", mpn="X", lcsc="C1", p1=V3, p2="SENSE")
Board(width=50, height=36, layers=4, stackup="jlcpcb_4l_1oz", planes=[("GND", "In1.Cu"), ("3V3", "In2.Cu")])
NetReq("3V3", kind="power", volts=3.3)
NetReq("GND", kind="power")
Path("3V3", "R0.2", "R1.1", "R2.1", "R3.1", amps={"R1.1": 0.1, "R2.1": 0.1, "R3.1": 0.6})
Place("R0", at=(8, 18))
Place("R1", at=(20, 8))
Place("R2", at=(34, 8))
Place("R3", at=(20, 28))
Place("R5", at=(40, 22))
for i, r in enumerate(["R0", "R1", "R2", "R3", "R5"]):
    SchPlace(r, left=20 + 20 * i, top=20)
'''
"""Refutation physics r1, `pour_two`: free 1206 pads on a 4-layer pour whose joints need 2 vias each (R0.2 supplies
0.8 A, R3.1 draws 0.6 A; one 0.35/0.2 via is 0.527 A). Before the fix the second tap via went on the same ray as the
first and the build stopped with "internal router error (... a turn sharper than 135 degrees ...)"."""


def test_a_joint_of_two_vias_takes_one_site_per_side_and_legal_copper(tmp_path: Path):
    """PR 8 multi-via tap: each via leaves the pad on its own side, two of them in opposite directions (one straight
    line through the pad), so the pattern copper passes its own self-check and no stub carries two vias' current."""
    from conftest import placed_board
    from boardtext import scene_from_text
    from pcbc.compile import compile_design
    from pcbc.patterns import pattern_copper
    from pcbc.route_verify import verify_copper

    def plan_of(name: str, draws: str, extra: str = ""):
        board = tmp_path / f"{name}.py"
        text = POUR_TWO.replace('amps={"R1.1": 0.1, "R2.1": 0.1, "R3.1": 0.6}', draws)
        write_split(board, text.replace('Place("R5", at=(40, 22))', 'Place("R5", at=(40, 22))\n' + extra))
        design = load_board(board)
        job = compile_design(design)
        assert job.constraints.refusals == (), job.constraints.refusals
        text = placed_board(name, board).read_text()
        plan = pattern_copper(design, job, job.constraints, name, stage="post", scene=scene_from_text(design, job, job.constraints, text))
        assert verify_copper(plan.scene, plan.pieces, job.constraints, ids=plan.ids) == []
        return job, plan

    def dirs_of(plan, owner: str) -> list[tuple[int, int]]:
        stubs = [p for p in plan.pieces if p.reason == "tap" and p.owner == owner and p.kind != "via"]
        assert len([p for p in plan.pieces if p.reason == "tap" and p.owner == owner and p.kind == "via"]) == len(stubs)
        return [((s.b[0] > s.a[0]) - (s.b[0] < s.a[0]), (s.b[1] > s.a[1]) - (s.b[1] < s.a[1])) for s in stubs]

    _job, plan = plan_of("pour_two", 'amps={"R1.1": 0.1, "R2.1": 0.1, "R3.1": 0.6}')
    for owner in ("R0.2", "R3.1"):
        (a, b) = dirs_of(plan, owner)
        assert a == (-b[0], -b[1]), (owner, a, b)
    # Three and four vias (R1.1 draws 1.2 A, R0.2 supplies 1.9 A): each via on a side of its own.
    job, plan = plan_of("pour_four", 'amps={"R1.1": 1.2, "R2.1": 0.1, "R3.1": 0.6}')
    need = {j.station: j.vias for p in job.constraints.by_net("3V3").paths for j in p.joints}
    assert (need["R0.2"], need["R1.1"]) == (4, 3), need
    for owner, n in (("R0.2", 4), ("R1.1", 3)):
        d = dirs_of(plan, owner)
        assert len(d) == n and len(set(d)) == n, (owner, d)
    # Via keepouts west and north of R1.1 leave south and east, a right angle: R1.1 keeps one via (a right-angle
    # pair out of one pad centre is a 90 degree corner), and the pattern says it is short (the gate's E23 states it).
    keep = 'Keepout("K1", box=(15.5, 4.0, 18.0, 12.0), no=("via",)); Keepout("K2", box=(17.0, 4.0, 20.0, 7.05), no=("via",))'
    _job, plan = plan_of("pour_walled", 'amps={"R1.1": 1.2, "R2.1": 0.1, "R3.1": 0.1}', keep)
    assert dirs_of(plan, "R1.1") == [(0, 1)], dirs_of(plan, "R1.1")
    assert "style: tap 3V3: R1.1 has 1 of the 3 vias the joint of R1.1 draws 1.2 A: 3 vias 0.35/0.2 mm (via_barrel 0.527 A each at 10 C)" in plan.notes


@pytest.mark.kicad
def test_a_board_whose_joints_need_two_vias_builds_and_every_joint_carries_its_draw(tmp_path: Path):
    """The same board through the build (route stage, emit, fill, gates): no internal error, and each station's
    `pcbc:tap` vias on the emitted board carry its draw (`route_verify.pour_joints`)."""
    from conftest import routed_result

    board = tmp_path / "pour_two.py"
    write_split(board, POUR_TWO)
    _out, result = routed_result("pour_two", board)
    assert result.get("error") is None, result.get("error")
    rows = next(s for s in result["steps"] if "pour_joints" in s)["pour_joints"]
    got = {(r["station"], r["amps"], r["vias"]) for r in rows}
    assert got == {("R0.2", 0.8, 2), ("R1.1", 0.1, 1), ("R2.1", 0.1, 1), ("R3.1", 0.6, 2)}, rows
    assert all(not r["error"] and r["carries"] >= r["amps"] for r in rows), rows


# ---------------------------------------------------------------------------------------------
# Refutation one-home r1: a property with two subjects accepted at once
# ---------------------------------------------------------------------------------------------

SDA_PATH = 'Path("SDA", "U1.IO4", "U4.SDI", "R_SDA.2"{})'
SCL_PATH = 'Path("SCL", "U1.IO5", "U4.SCK", "R_SCL.2")'


def _node_plus(tmp_path: Path, *lines: str, name: str = "node") -> Path:
    """node with `lines` appended after its last line (161 on), so no other line number moves."""
    board = _node_edit(tmp_path, name=name)
    write_split(board, board.read_text().rstrip("\n") + "\n" + "\n".join(lines) + "\n")
    return board


@pytest.mark.parametrize(
    "lines,message",
    [
        (
            ['Bus("USB_DP", "USB_DN", match_mm=1)'],
            "Bus line 80: USB_DP and USB_DN are timed by Pair line 62 already (USB_DP and USB_DN within 0.5 mm of each other); "
            "a net's skew budget is one Pair's or one Bus's",
        ),
        (
            ['Bus("SDA", "SCL", match_mm=1)', 'Bus("SDA", "SCL", match_mm=5)', SDA_PATH.format(""), SCL_PATH],
            "Bus line 81: SDA and SCL are timed by Bus line 80 already (within 1 mm); a net's skew budget is one Pair's or one Bus's",
        ),
        (
            ['Bus("SDA", "SCL", match_mm=1, clock="SCL")', 'Bus("SDA", "SCL", match_mm=1, clock="SDA")', SDA_PATH.format(""), SCL_PATH],
            "Bus line 81: SDA and SCL are timed by Bus line 80 already (within 1 mm); a net's skew budget is one Pair's or one Bus's",
        ),
    ],
    ids=["bus_on_a_pair", "two_buses", "two_clocks"],
)
def test_a_net_is_timed_by_one_pair_or_one_bus(tmp_path: Path, lines: list[str], message: str):
    """KD 8: the skew budget is a Pair's or a Bus's, once per net (refutation one-home r1, defect 4)."""
    assert _cs(_node_plus(tmp_path, *lines)).refusals == (message,)


@pytest.mark.parametrize("bus", ['Bus("SDA", "SCL", match_mm=1, length_mm=50)', 'Bus("SDA", "SCL", match_mm=1)'], ids=["bus_writes_one", "bus_writes_none"])
def test_a_bus_members_length_is_its_bus_s(tmp_path: Path, bus: str):
    """KD 8 / G3: a Bus owns each member's length budget, as a Pair does (E17's Bus twin; defect 5)."""
    cs = _cs(_node_plus(tmp_path, bus, SDA_PATH.format(", length_mm=30"), SCL_PATH))
    assert cs.refusals == (
        'Path("SDA") line 81: length_mm= on SDA, a member of Bus line 80 (SDA, SCL); a Bus holds the length budget of each of its members (Bus length_mm=)',
    ), cs.refusals
    cs = _cs(_node_plus(tmp_path, 'Bus("SDA", "SCL", match_mm=1, length_mm=50)', SDA_PATH.format(""), SCL_PATH, name="home"))
    assert cs.refusals == () and "SDA: path U1.IO4 -> U4.SDI -> R_SDA.2 (Path line 81): length 50 mm (Bus line 80)" in cs.lines, (cs.refusals, cs.lines)


def test_a_bus_length_is_each_member_paths_budget_not_the_nets(tmp_path: Path):
    """Round 3 (refute-onehome): `Bus(length_mm=10)` was compiled as SDA's whole-net length (`length_max_mm`, formula
    "Bus"), and dru wrote it as a KiCad length rule on the net, which sums every stub: SDA has 3 pads, the R_SDA pull-up
    among them, and the member Path line 81 got `length_mm` None. Pair(length_mm=) already put its budget on each member
    Path. Now the Bus does too: the path carries 10 mm (Bus line 80), the net none, and KiCad gets no length rule for a
    net whose Path is not the whole node; the path-length gate times it."""
    from pcbc.compile import compile_design
    from pcbc.dru import rules

    board = _node_plus(tmp_path, 'Bus("SDA", "SCL", match_mm=2, length_mm=10)', 'Path("SDA", "U1.IO4", "U4.SDI")', 'Path("SCL", "U1.IO5", "U4.SCK")')
    cs = compile_design(load_board(board)).constraints
    assert cs.refusals == (), cs.refusals
    sda = cs.by_net("SDA")
    assert sda.length_max_mm is None and [(p.line, p.length_mm, p.length_src) for p in sda.paths] == [(81, 10.0, "Bus line 80")]
    assert "SDA: path U1.IO4 -> U4.SDI (Path line 81): length 10 mm (Bus line 80)" in cs.lines
    assert not [r for r in rules(cs) if r.name in ("length_sda", "length_scl")]
    # A member Path on every pad is the whole node: KiCad's per-net rule is then exact, and it is written (KD 12).
    whole = _node_plus(tmp_path, 'Bus("SDA", "SCL", match_mm=2, length_mm=10)', SDA_PATH.format(""), SCL_PATH, name="whole")
    cs = compile_design(load_board(whole)).constraints
    assert cs.by_net("SDA").length_max_mm is None
    assert [(r.name, r.constraint) for r in rules(cs) if r.name == "length_sda"] == [("length_sda", "(constraint length (max 10mm))")]


@pytest.mark.parametrize(
    "line,net",
    [
        ('Path("DRV", "U1.IO1", "R_G.1", kind="sense")', "DRV"),
        ('Path("T_OUT", "U5.OUT", "U5.-", "U1.IO0", kind="sense")', "T_OUT"),
    ],
    ids=["two_pads", "three_pads"],
)
def test_a_path_kind_on_every_pad_is_the_whole_nodes(tmp_path: Path, line: str, net: str):
    """Refutation one-home r1, defect 6: a measurement line on a two-pad net (node's DRV: U1.IO1 to R_G.1) had no
    home: the Path was refused for naming every pad, and `NetReq(kind="sense")` is E14. A Path on every pad is the
    whole node, so its kind's copper is the node's; it is accepted, and `NetReq(kind="sense")` stays E14."""
    cs = _cs(_node_plus(tmp_path, line))
    assert cs.refusals == (), cs.refusals
    (p,) = [p for p in cs.by_net(net).paths if p.kind == "sense"]
    assert p.links and all(not lk.via.allowed and lk.layers == ("F.Cu",) for lk in p.links), p.links
    cs = _cs(_node_plus(tmp_path, f'NetReq("{net}", kind="sense")', name="netreq"))
    assert any("kind=\"sense\" is a Path kind" in r for r in cs.refusals), cs.refusals


NODE_PAIR = 'Pair("USB_DP", "USB_DN", kind="usb_hs", z_diff_ohm=90)'  # node line 143


def test_a_netreq_gives_a_pairs_two_nets_their_node_facts(tmp_path: Path):
    """Refutation one-home r1, defect 1: volts=, max_mm=, rise_ps=, keep_clear_of= and class_name= are NET fields
    (§2.1 N1, N9, N15, N19, N21), and a pair net had no home for them (the NetReq was refused for naming a Pair's
    net, the Pair for the keyword). A NetReq that names both nets of a Pair, and no other, gives them its node facts;
    the Pair keeps its own numbers."""
    cs = _cs(_node_plus(tmp_path, 'NetReq("USB_DP", "USB_DN", volts=3.3, max_mm=60, rise_ps=500, keep_clear_of="VBUS", class_name="USBX")'))
    assert cs.refusals == (), cs.refusals
    dp = cs.by_net("USB_DP")
    assert dp.pair is not None and dp.pair.partner == "USB_DN" and dp.pair.line == 62 and dp.pair.tolerance_pct == 15
    assert dp.voltage is not None and dp.voltage.volts == 3.3 and dp.airwire_max_mm == 60 and dp.rise_ps == 500
    assert [(k.other, k.mm) for k in dp.keep_away] == [("VBUS", 3.0)] and dp.class_name == "USBX"
    assert cs.by_net("USB_DN").voltage.volts == 3.3
    assert "USB_DP: airwire 60 mm (NetReq line 80)" in cs.lines
    assert "USB_DP: keep_clear_of VBUS 3 mm (NetReq line 80; 3 mm when keep_clear_of is given, pcbc default)" in cs.lines
    assert "USB_DP: clearance 0.18 mm (class_floor hole_clearance 0.25 - ring 0.075 + 0.005; ipc2221_6_1 row 0-15 V B2 0.1)" in cs.lines


@pytest.mark.parametrize(
    "line,message",
    [
        (
            'NetReq("USB_DP", volts=3.3)',
            "USB_DP: NetReq line 80 names it and not USB_DN, its partner on Pair line 62; the two nets of a Pair are one class (their "
            "width and gap are the pair's), so a NetReq names both of them or neither",
        ),
        (
            'NetReq("USB_DP", "USB_DN", "SDA", volts=3.3)',
            'NetReq("USB_DP", "USB_DN", "SDA") line 80: names USB_DP and USB_DN, the nets of Pair line 62, and SDA; a Pair\'s nets are a '
            "class of their own (width and gap from its impedance), and every net of one NetReq shares one class",
        ),
        (
            'NetReq("USB_DP", "USB_DN", kind="analog", keep_clear_of="VBUS")',
            'NetReq("USB_DP", "USB_DN") line 80: kind="analog" says what a node is, and USB_DP and USB_DN are the coupled pair of Pair line '
            "62; a NetReq that names a Pair's nets states their node facts (volts=, max_mm=, rise_ps=, keep_clear_of=, class_name=) and "
            "takes kind generic",
        ),
    ],
    ids=["one_member", "with_another_net", "a_net_kind"],
)
def test_a_netreq_on_a_pair_names_both_nets_alone_with_kind_generic(tmp_path: Path, line: str, message: str):
    assert _cs(_node_plus(tmp_path, line)).refusals == (message,)


@pytest.mark.parametrize(
    "kw,message",
    [
        ("volts=3.3", "volts= holds at every pad of a node, a NetReq field; a NetReq that names both nets of a Pair (and no other net) gives them its node facts"),
        ("pf_max=200", "pf_max= is the capacitance budget of an I2C bus line (UM10204 7.1), a NetReq field of kind i2c"),
        ("amps=0.1", "amps= is about a current, a Path field; version 1 does not size or check a current on a coupled pair"),
        ("z_se_ohm=45", "z_se_ohm= is one conductor's impedance, a Path field; a Pair's members are sized by its z_diff_ohm="),
    ],
    ids=["volts", "pf_max", "amps", "z_se_ohm"],
)
def test_a_node_or_current_field_on_a_pair_line_states_its_home(tmp_path: Path, kw: str, message: str):
    board = _node_edit(tmp_path, [(NODE_PAIR, NODE_PAIR[:-1] + f", {kw})")])
    assert check_board(board) == [f'Pair("USB_DP") line 62: {message}']


def test_a_bare_pair_is_its_own_line_and_owns_its_via_fields(tmp_path: Path):
    """Defect 2: a Pair with no kind was compiled with the usb_hs preset, so it carried "at most 2 vias (preset usb_hs)",
    a number nobody wrote and nobody could change, and its layers and spacing cited a kind the line never wrote."""
    bare = 'Pair("USB_DP", "USB_DN", z_diff_ohm=100)'
    cs = _cs(_node_edit(tmp_path, [(NODE_PAIR, bare)]))
    assert cs.refusals == (), cs.refusals
    dp = cs.by_net("USB_DP")
    assert dp.via.allowed and dp.via.count_max is None and dp.class_name == "Pair_USB_DP"
    assert "USB_DP: via 0.35/0.2 mm (stackup jlcpcb_4l_1oz)" in cs.lines
    assert "USB_DP: layers F.Cu, B.Cu (a pair's default: the outer layers no plane takes)" in cs.lines
    assert "USB_DP: spacing 3W (a bare Pair: 3W, pcbc default)" in cs.lines
    assert not any("usb_hs" in x for x in cs.lines if x.startswith("USB_DP:")), [x for x in cs.lines if x.startswith("USB_DP:")]
    # Pair(vias=, vias_max=): the pair's copper, both members, cited to the Pair line.
    cs = _cs(_node_edit(tmp_path, [(NODE_PAIR, 'Pair("USB_DP", "USB_DN", z_diff_ohm=100, vias_max=0)')], name="vmax"))
    assert cs.refusals == () and cs.by_net("USB_DN").via.count_max == 0
    assert "USB_DP: via 0.35/0.2 mm (stackup jlcpcb_4l_1oz), at most 0 (Pair line 62) [soft: warning in R1]" in cs.lines
    cs = _cs(_node_edit(tmp_path, [(NODE_PAIR, NODE_PAIR[:-1] + ", vias=False)")], name="novia"))
    assert cs.refusals == () and not cs.by_net("USB_DP").via.allowed and "USB_DP: no vias (Pair line 62)" in cs.lines
    (p,) = cs.by_net("USB_DP").paths
    assert all(not lk.via.allowed and lk.vias_src == "Pair line 62" for lk in p.links)
    # the usb_hs kind keeps its preset budget, cited to the preset
    cs = _cs(_node_edit(tmp_path, [], name="usb"))
    assert "USB_DP: via 0.35/0.2 mm (stackup jlcpcb_4l_1oz), at most 2 (preset usb_hs) [soft: warning in R1]" in cs.lines


@pytest.mark.parametrize(
    "field,message",
    [
        ("vias=False", "vias= on half of the pair USB_DN/USB_DP; the pair's numbers are on Pair line 62, and a Path on a pair net names stations only"),
        ("vias_max=1", "vias_max= on half of the pair USB_DN/USB_DP; the pair's numbers are on Pair line 62, and a Path on a pair net names stations only"),
        (
            "z_se_ohm=45",
            "z_se_ohm= on half of the pair USB_DN/USB_DP: each member's width and gap are sized for the pair's 90 ohm on Pair line 62, "
            "and one width is not sized for two impedances",
        ),
        (
            "amps=0.01",
            "amps= on half of the pair USB_DN/USB_DP: each member is 0.2288 mm wide, the width its pair's 90 ohm on Pair line 62 sets, "
            "and version 1 does not size or check a current on a coupled pair",
        ),
    ],
    ids=["vias", "vias_max", "z_se_ohm", "amps"],
)
def test_e17_names_the_true_home_of_a_field_on_a_pair_members_path(tmp_path: Path, field: str, message: str):
    """Defect 2: E17 sent every field to the Pair line, which refused amps=, vias=, vias_max= and z_se_ohm= in turn.
    vias= and vias_max= are now Pair fields; z_se_ohm= and amps= are refused with their real reason."""
    member = 'Path("USB_DP", "J1.B6", "U3.6", "U1.27")'
    cs = _cs(_node_edit(tmp_path, [(member, member[:-1] + f", {field})")]))
    assert cs.refusals == (f'Path("USB_DP") line 70: {message}',), cs.refusals


@pytest.mark.parametrize(
    "pair,message",
    [
        (
            'Pair("USB_DP", "USB_DN", kind="usb_hs", z_diff_ohm=90, layers=["B.Cu"], reference="In1.Cu")',
            'Pair line 62: reference="In1.Cu" is not the plane under B.Cu, the one layer this Pair may run on',
        ),
        (
            'Pair("USB_DP", "USB_DN", z_diff_ohm=90, reference="In1.Cu")',
            'Pair line 62: reference="In1.Cu" is the plane under F.Cu only, and this Pair may run on F.Cu and B.Cu',
        ),
    ],
    ids=["wrong_plane", "two_layers"],
)
def test_e10_holds_on_a_pair_as_on_a_path(tmp_path: Path, pair: str, message: str):
    """Defect 3: G2 makes a Pair's reference per layer (N14) as a Path's is; node's B.Cu sits over In2.Cu (3V3), and
    `Pair(layers=["B.Cu"], reference="In1.Cu")` was accepted and its impedance computed over In1.Cu."""
    assert _cs(_node_edit(tmp_path, [(NODE_PAIR, pair)])).refusals == (message,)
    ok = _cs(_node_edit(tmp_path, [(NODE_PAIR, 'Pair("USB_DP", "USB_DN", kind="usb_hs", layers=["B.Cu"], reference="In2.Cu")')], name="ok"))
    assert ok.refusals == () and ok.by_net("USB_DP").reference == "In2.Cu"


# --- postfix (refutation one-home r2, defects 2-4) ------------------------------------------------------------

C3_PAIR = 'Pair("USB_DP", "USB_DN", kind="usb_hs", z_diff_ohm=90)'  # c3_usb line 263


@pytest.mark.parametrize("ref", ["Banana", "In1.Cu", "GND"])
def test_a_pair_reference_no_plane_takes_is_refused_on_two_layers(tmp_path: Path, ref: str):
    """Postfix A2: the not-a-plane refusal held on four layers only, so c3_usb's Pair printed "over Banana"."""
    pair = f'Pair("USB_DP", "USB_DN", kind="usb_hs", z_diff_ohm=90, layers=["F.Cu"], reference="{ref}")'
    cs = _cs(_c3(tmp_path, [(C3_PAIR, pair)]))
    assert cs.refusals == (f'Pair line 205: reference="{ref}" is not a plane this board pours (B.Cu (GND))',), cs.refusals


@pytest.mark.parametrize("ref", ["B.Cu", "B.Cu pour"])
def test_the_b_cu_pour_is_named_b_cu_on_two_layers(tmp_path: Path, ref: str):
    """Postfix A3: the plane list prints the two-layer pour as "B.Cu (GND)" and the compiler says F.Cu runs over the
    B.Cu pour, so reference="B.Cu" names it, on a Path and on a Pair; E10 used to refuse it as not under F.Cu."""
    cs = _cs(_buck(tmp_path, [('Loop("C_IN1.1"', f'Path("EN", "R_EN.2", "U1.EN", layers=["F.Cu"], reference="{ref}")\nLoop("C_IN1.1"')]))
    assert cs.refusals == (), cs.refusals
    pair = f'Pair("USB_DP", "USB_DN", kind="usb_hs", z_diff_ohm=90, layers=["F.Cu"], reference="{ref}")'
    cs = _cs(_c3(tmp_path, [(C3_PAIR, pair)], name="c3ref"))
    assert cs.refusals == (), cs.refusals
    assert any(x.startswith("USB_DP: pair with USB_DN") and "on F.Cu over the B.Cu pour" in x for x in cs.lines), cs.lines
    both = _cs(_buck(tmp_path, [('Loop("C_IN1.1"', f'Path("EN", "R_EN.2", "U1.EN", reference="{ref}")\nLoop("C_IN1.1"')], name="both"))
    assert both.refusals == (f'Path("EN") line 52: reference="{ref}" is the plane under F.Cu only, and this Path may run on F.Cu and B.Cu',), both.refusals


def test_an_impedance_on_the_pours_own_layer_says_so(tmp_path: Path):
    """Postfix A4: buck line 41 makes GND a power net, so B.Cu is poured; an impedance on B.Cu has no plane because
    B.Cu is the pour's own layer. The text said "no NetReq names GND with kind power", which line 41 does."""
    cs = _cs(_buck(tmp_path, [('Loop("C_IN1.1"', 'Path("EN", "R_EN.2", "U1.EN", z_se_ohm=50)\nLoop("C_IN1.1"')]))
    assert cs.refusals == (
        "EN: 50 ohm on B.Cu (Path line 52) has no reference plane: B.Cu is the GND pour's own layer, and a two-layer board has no plane under it",
    ), cs.refusals
    nopour = _cs(_buck(tmp_path, [('NetReq("GND", kind="power")', 'NetReq("GND")'), ('Loop("C_IN1.1"', 'Path("EN", "R_EN.2", "U1.EN", z_se_ohm=50)\nLoop("C_IN1.1"')], name="nopour"))
    assert any("no NetReq names GND with kind power" in r for r in nopour.refusals), nopour.refusals


def test_a_current_into_a_three_pad_part_names_its_unchecked_pour_joint():
    """Postfix A5 (refutation migration r2): node's LOAD Path (line 69, 1 A) enters Q1.D; what leaves Q1.S goes into the
    GND pour through a 0.16 mm stub and one via, and the two-pad note skipped any part with more pads, so no line of the
    build named that joint. The note bounds the current (the part decides how it splits) and states what is unchecked."""
    cs = _cs(EXAMPLES / "node" / "node.py")
    assert cs.refusals == ()
    assert (
        "GND: Q1 has 3 pads; Path line 69 carries 1 A into Q1.3, and up to 1 A of it can pass through Q1.2 on GND, into the GND pour; "
        "version 1 does not size a return in a pour (net-path-physics.md §10 A), so nothing checks that its joint to the pour carries 1 A"
    ) in cs.lines, [x for x in cs.lines if "Q1" in x]


def test_a_station_two_current_paths_share_on_a_pour_is_sized_for_their_sum(tmp_path: Path):
    """Postfix A6 (refutation physics r2, pourtree): two current Paths leave R0.2 on the 3V3 pour, 1 A each. R0.2's
    pad meets the pour through one joint, which carries both: 2 A needs ceil(2 / 0.527) = 4 vias. Each Path's joint
    was sized for 1 A (2 vias, 1.054 A) and the build passed; now every Path's joint at R0.2 is the summed one, and the
    tap (a dict by pad) and the gate read the same."""
    from pcbc.patterns.tap import joints_of

    board = tmp_path / "pourtree.py"
    write_split(board, POUR_TWO.replace(
        'Path("3V3", "R0.2", "R1.1", "R2.1", "R3.1", amps={"R1.1": 0.1, "R2.1": 0.1, "R3.1": 0.6})',
        'Path("3V3", "R0.2", "R1.1", amps=1.0)\nPath("3V3", "R0.2", "R2.1", amps=1.0)',
    ))
    cs = _cs(board)
    assert cs.refusals == (), cs.refusals
    got = {(p.line, j.station, j.amps, j.vias, j.lines) for p in cs.by_net("3V3").paths for j in p.joints}
    assert got == {(11, "R0.2", 2.0, 4, (11, 12)), (11, "R1.1", 1.0, 2, ()), (12, "R0.2", 2.0, 4, (11, 12)), (12, "R2.1", 1.0, 2, ())}, got
    assert joints_of(cs, "3V3")["R0.2"].vias == 4
    line = next(x for x in cs.lines if x.startswith("3V3: path R0.2 -> R1.1"))
    assert "R0.2 supplies 2 A in all, for Path lines 11 and 12: 4 vias 0.35/0.2 mm" in line, line


P2_POUR = POUR_TWO.replace(
    'Board(width=50, height=36, layers=4, stackup="jlcpcb_4l_1oz", planes=[("GND", "In1.Cu"), ("3V3", "In2.Cu")])',
    'Board(width=50, height=36, layers=2, stackup="jlcpcb_2l_1oz", planes=[("3V3", "B.Cu")])',
).replace('NetReq("GND", kind="power")\n', "").replace(
    'Path("3V3", "R0.2", "R1.1", "R2.1", "R3.1", amps={"R1.1": 0.1, "R2.1": 0.1, "R3.1": 0.6})',
    'Path("3V3", "R0.2", "R1.1", "R2.1", amps={"R1.1": 0.6, "R2.1": 0.3})',
)


def test_a_two_layer_declared_pour_is_the_compilers_as_the_routers(tmp_path: Path):
    """Postfix A7 (refutation physics r2, p2sense): `Board(layers=2, planes=[("3V3", "B.Cu")])` is poured by the route
    stage (`route_scene.plane_targets`) and was invisible to the compiler: the draws Path got no joints (R0.2 supplied
    0.9 A through one 0.707 A tap via, no E23) and a sense Path whose far pad the pour joins was accepted."""
    from pcbc.compile import compile_design
    from pcbc.route_scene import plane_targets

    board = tmp_path / "p2.py"
    write_split(board, P2_POUR)
    design = load_board(board)
    job = compile_design(design)
    cs = job.constraints
    assert cs.refusals == () and cs.planes == (("3V3", "B.Cu"),) == plane_targets(job), (cs.refusals, cs.planes)
    got = {(j.station, j.amps, j.vias) for p in cs.by_net("3V3").paths for j in p.joints}
    assert got == {("R0.2", 0.9, 2), ("R1.1", 0.6, 1), ("R2.1", 0.3, 1)}, got
    write_split(board, P2_POUR.replace('Place("R0"', 'Path("3V3", "R2.1", "R5.1", kind="sense")\nPlace("R0"'))
    (refusal,) = _cs(board).refusals
    assert refusal.startswith('Path("3V3") line 11: 3V3 is poured (B.Cu; Board(planes=)), and version 1 does not build a sense path on a poured net'), refusal


def test_declared_links_that_close_a_loop_are_refused(tmp_path: Path):
    """Postfix A8 (refutation physics r2, loop.py): Path line A runs R0.2 -> R1.1 -> R2.1 and Path line B R0.2 -> R2.1.
    The router lays one route between copper already joined, so B's link got no copper and its 0.9 A ran through A's
    links (1.9 A in 0.4 mm rated 1.152 A) while the build said "copper verified". A tree is what version 1 sizes."""
    text = DRAWS.replace("{path}", 'Path("3V3", "R0.2", "R1.1", "R2.1", amps={"R1.1": 0.1, "R2.1": 0.9})\nPath("3V3", "R0.2", "R2.1", amps=0.9)')
    board = tmp_path / "loop.py"
    write_split(board, text)
    (refusal,) = _cs(board).refusals
    assert refusal.startswith(
        "3V3: Path line 10 (R0.2 -> R2.1) joins two stations that Path line 9 (R0.2 -> R1.1) and Path line 9 (R1.1 -> R2.1) already join, "
        "so the declared links of 3V3 close a loop"
    ), refusal
    # A branch is a tree: no refusal.
    write_split(board, DRAWS.replace("{path}", 'Path("3V3", "R0.2", "R1.1", "R2.1", amps={"R1.1": 0.1, "R2.1": 0.9})\nPath("3V3", "R1.1", "R3.1", amps=0.05)'))
    assert not any("close a loop" in r for r in _cs(board).refusals), _cs(board).refusals


# --- Round 3 (refute-current f1c, f2c, f4c): a station is its pads; a field-less Path brings no current ---

F1C = """from pcbc import *
VBUS = Power("VBUS"); GND = Ground("GND")
USBC = load("components/HRO/TYPE-C-31-M-12")
USBC("J1", GND=GND, VBUS=VBUS, CC1=Net("CC1"), CC2=Net("CC2"), DP1=Net("DP"), DP2=Net("DP"), DN1=Net("DN"), DN2=Net("DN"), EH=GND)
Resistor("R1", "5", package="1206", mpn="X", lcsc="C1", p1=VBUS, p2=GND)
Resistor("R2", "5", package="1206", mpn="X", lcsc="C1", p1=VBUS, p2=GND)
Resistor("R3", "5k", package="0402", mpn="X", lcsc="C1", p1="CC1", p2=GND)
Resistor("R4", "5k", package="0402", mpn="X", lcsc="C1", p1="CC2", p2=GND)
Board(width=40, height=30, layers=4, stackup="jlcpcb_4l_1oz", planes=[("GND", "In1.Cu"), ("VBUS", "In2.Cu")])
NetReq("VBUS", kind="power", volts=5)
NetReq("GND", kind="power")
Path("VBUS", "J1.VBUS", "R1.1", amps=1)
Path("VBUS", "J1.A4B9", "R2.1", amps=0.5)
Place("J1", edge="bottom")
Place("R1", at=(10, 10))
Place("R2", at=(30, 10))
Place("R3", at=(15, 18))
Place("R4", at=(25, 18))
SchPlace("J1", left=20, top=20)
SchPlace("R1", left=80, top=20)
SchPlace("R2", left=100, top=20)
SchPlace("R3", left=80, top=60)
SchPlace("R4", left=100, top=60)"""

F2C = """from pcbc import *
X = Power("X"); GND = Ground("GND")
ESD = load("components/ST/USBLC6-2SC6")
ESD("U3", GND=GND, VBUS="VB", **{"I/O1": X, "I/O2": "IO2"})
Resistor("R0", "0R", package="1206", mpn="X", lcsc="C1", p1="VIN", p2=X)
Resistor("R1", "1.6", package="1206", mpn="X", lcsc="C1", p1=X, p2=GND)
Resistor("R5", "10k", package="0603", mpn="X", lcsc="C1", p1="VB", p2="IO2")
Board(width=50, height=36, layers=2, stackup="jlcpcb_2l_1oz")
NetReq("X", kind="power", volts=3.3)
Path("X", "R0.2", "U3.I/O1", amps=0.1)
Path("X", "U3.1", "R1.1", amps=1)
Place("R0", at=(8, 18))
Place("U3", at=(25, 18))
Place("R1", at=(42, 18))
Place("R5", at=(25, 28))
SchPlace("U3", left=20, top=20)
SchPlace("R0", left=60, top=20)
SchPlace("R1", left=80, top=20)
SchPlace("R5", left=100, top=20)"""

F4C = """from pcbc import *
V3 = Power("3V3"); GND = Ground("GND")
JST = load("components/JST/B2B-XH-A")
JST("J1", p1=V3, p2=GND)
Resistor("R1", "1.6", package="1206", mpn="X", lcsc="C1", p1=V3, p2=GND)
Resistor("R2", "10k", package="0603", mpn="X", lcsc="C1", p1=V3, p2="SIG")
Resistor("R3", "10k", package="0603", mpn="X", lcsc="C1", p1="SIG", p2=GND)
Board(width=50, height=36, layers=2, stackup="jlcpcb_2l_1oz")
NetReq("3V3", kind="power", volts=3.3)
Path("3V3", "J1.1", "R2.1", layers=["F.Cu"])
Path("3V3", "R2.1", "R1.1", amps=2)
Place("J1", at=(8, 18))
Place("R2", at=(25, 18))
Place("R1", at=(40, 18))
Place("R3", at=(25, 28))
SchPlace("J1", left=20, top=20)
SchPlace("R1", left=40, top=20)
SchPlace("R2", left=60, top=20)
SchPlace("R3", left=80, top=20)"""


def _with_parts(tmp_path: Path, name: str, text: str, parts: list[tuple[str, str]]) -> Path:
    """`text` as `<name>.py` beside copies of the example part folders it loads (`(example, "Vendor/PART")`)."""
    work = tmp_path / name
    for ex, part in parts:
        shutil.copytree(EXAMPLES / ex / "components" / part, work / "components" / part)
    board = work / f"{name}.py"
    write_split(board, text)
    return board


def test_a_pin_and_one_of_its_pads_on_a_pour_are_one_joint(tmp_path: Path, monkeypatch):
    """Round 3 (refute-current f1c): `J1.VBUS` is pads A4B9 and B4A9; Path line 12 supplies 1 A from it and Path line
    13 0.5 A from `J1.A4B9`. A6 merged only identical pad tuples, so each joint was sized alone (1 via each), the tap
    kept the last (pad -> joint) and the gate passed: pad A4B9 carried 0.5 + 0.5 = 1 A on one 0.527 A via. Now the two
    are one joint over both pads; A4B9's share is 1 A, so every pad needs ceil(1 / 0.527) = 2 vias, and the gate
    reads the worst pad's share, not an even split."""
    from types import SimpleNamespace

    import pcbc.layout_job as layout_job
    from pcbc.paths import joint_pad_amps
    from pcbc.patterns.tap import joints_of
    from pcbc.route_verify import pour_joints

    cs = _cs(_with_parts(tmp_path, "f1c", F1C, [("c3_usb", "HRO")]))
    assert cs.refusals == (), cs.refusals
    got = {(p.line, j.station, j.pads, j.amps, j.vias, j.lines, j.pad_amps) for p in cs.by_net("VBUS").paths for j in p.joints if j.station.startswith("J1")}
    merged = ("J1.VBUS (also written J1.A4B9)", ("J1.A4B9", "J1.B4A9"), 1.5, 2, (12, 13), (("J1.A4B9", 1.0), ("J1.B4A9", 0.5)))
    assert got == {(12, *merged), (13, *merged)}, got
    joints = joints_of(cs, "VBUS")
    assert joints["J1.A4B9"] == joints["J1.B4A9"] and joint_pad_amps(joints["J1.A4B9"]) == 1.0
    line = next(x for x in cs.lines if x.startswith("VBUS: path J1.A4B9 -> R2.1"))
    assert "J1.VBUS (also written J1.A4B9) supplies 1.5 A in all, for Path lines 12 and 13, 1 A of it through pad J1.A4B9: 2 vias 0.35/0.2 mm per pad" in line, line

    # The gate: one tap via on each J1 pad carries 0.527 A, under pad A4B9's 1 A (an even split, 0.75 A, would also
    # fail; two vias each pass).
    def board_with(n: int):
        items = [{"reason": "tap", "key": ("via", (i, k)), "owner": pad} for i, pad in enumerate(("J1.A4B9", "J1.B4A9", "R1.1", "R2.1")) for k in range(n)]
        monkeypatch.setattr(layout_job, "roles_doc", lambda _t: SimpleNamespace(items=items))
        return {(r["line"], r["station"]): r for r in pour_joints("", cs)}

    one = board_with(1)
    assert one[(12, merged[0])]["error"].startswith(
        'Path("VBUS") line 12: J1.VBUS (also written J1.A4B9) supplies 1.5 A in all, for Path lines 12 and 13, 1 A of it through pad J1.A4B9, '
        "and its joint to the VBUS pour on In2.Cu is 1 via"
    ), one
    assert all(r["error"] == "" for r in board_with(2).values())

    # Uneven shares: J1.VBUS supplies 1.5 A (0.75 A a pad) and J1.A4B9 0.5 A, so pad A4B9 carries 1.25 A. Two vias a
    # pad (1.054 A) hold the even split (1 A) and not pad A4B9's share: the joint needs 3, and 2 are refused.
    cs = _cs(_with_parts(tmp_path, "f1d", F1C.replace('"R1.1", amps=1)', '"R1.1", amps=1.5)'), [("c3_usb", "HRO")]))
    (j,) = {j for p in cs.by_net("VBUS").paths for j in p.joints if j.station.startswith("J1")}
    assert (j.amps, j.vias, j.pad_amps) == (2.0, 3, (("J1.A4B9", 1.25), ("J1.B4A9", 0.75))), j
    two = board_with(2)
    assert "1.25 A of it through pad J1.A4B9" in two[(12, j.station)]["error"], two


def test_a_path_with_no_current_into_a_station_brings_none(tmp_path: Path):
    """Round 3 (refute-current f4c): Path line 10 runs J1.1 -> R2.1 with no `amps=` (0.16 mm, the class width) and
    Path line 11 leaves R2.1 with 2 A. `_balance` skipped field-less Paths, so the station had nothing entering and
    the rule passed: 2 A ran in 0.16 mm (0.633 A). With `amps=0.1` the same board was refused."""
    (refusal,) = _cs(_with_parts(tmp_path, "f4c", F4C, [("buck", "JST")])).refusals
    assert refusal == (
        "3V3: Path line 11 leaves R2.1 with 2 A; Path line 10 enters R2.1 with no amps=, so its link J1.1 -> R2.1 is sized for no "
        "current and brings none; currents at a station must balance"
    ), refusal
    # The feeder declared at 2 A balances.
    ok = _with_parts(tmp_path, "f4ok", F4C.replace('Path("3V3", "J1.1", "R2.1", layers=["F.Cu"])', 'Path("3V3", "J1.1", "R2.1", layers=["F.Cu"], amps=2)'), [("buck", "JST")])
    assert _cs(ok).refusals == ()


def test_the_balance_rule_reads_a_station_by_its_pads_not_its_spelling(tmp_path: Path):
    """Round 3 (refute-current f2c): U3's pin I/O1 is pads 1 and 6. Path line 10 brings 0.1 A to `U3.I/O1` and Path
    line 11 leaves `U3.1` with 1 A. The balance rule keyed stations by their written pad tuples, so ("U3.1",) and
    ("U3.1", "U3.6") were two stations and 1 A ran in Path line 10's 0.25 mm link. Writing `U3.I/O1` both times was
    already refused; both spellings now are."""
    (refusal,) = _cs(_with_parts(tmp_path, "f2c", F2C, [("c3_usb", "ST")])).refusals
    assert refusal.startswith(
        "X: Path line 11 leaves U3.I/O1 (also written U3.1, which shares its pads) with 1 A, and Path line 10 brings 0.1 A to it, "
        "the only Path that enters U3.I/O1; currents at a station must balance"
    ), refusal


@pytest.mark.parametrize(
    "second",
    [
        'Loop("U1.VIN", "U1.GND", "C_IN1.2", "C_IN1.1", mm2=5)',  # rotated
        'Loop("C_IN1.1", "U1.VIN", "U1.GND", "C_IN1.2", mm2=5)',  # the same line again
        'Loop("C_IN1.2", "U1.GND", "U1.VIN", "C_IN1.1", mm2=5)',  # reversed
        'Loop("C_IN1.1", "U1.3", "U1.GND", "C_IN1.2", mm2=5)',  # U1.3 is U1.VIN
    ],
)
def test_one_loop_gets_one_budget(tmp_path: Path, second: str):
    """Round 3 (refute-onehome): buck's input loop written a second time (rotated, reversed, repeated, or with a pad
    number for a pin) compiled two budgets, 10 and 5 mm2, for one current loop; compile_loops had no duplicate check.
    The later line is refused and the first keeps its budget."""
    first = 'Loop("C_IN1.1", "U1.VIN", "U1.GND", "C_IN1.2")'
    board = _buck(tmp_path, [(first, first[:-1] + ", mm2=10)\n" + second)])
    cs = _cs(board)
    (refusal,) = cs.refusals
    line = next(i for i, x in enumerate(board.read_text().splitlines(), 1) if x == second)
    written = " -> ".join(x.strip('"') for x in second[5:].split(", mm2")[0].split(", "))
    assert refusal == (
        f"Loop line {line}: {written} is the loop of Loop line {line - 1} (C_IN1.1 -> U1.VIN -> U1.GND -> C_IN1.2): the same pads "
        "(C_IN1.1, C_IN1.2, U1.1, U1.3); one loop, one budget"
    ), refusal
    assert [(lp.line, lp.budget.value) for lp in cs.loops] == [(line - 1, 10.0)]


@pytest.mark.parametrize(
    "path, want",
    [
        ('Path("EN", "R_EN.2", "U1.EN", layers=["B.Cu"], reference="F.Cu")', None),
        ('Path("EN", "R_EN.2", "U1.EN", layers=["B.Cu"], z_se_ohm=50)', "B.Cu over F.Cu pour: 50 ohm"),
        ('Path("EN", "R_EN.2", "U1.EN", layers=["F.Cu"], z_se_ohm=50)',
         "EN: 50 ohm on F.Cu (Path line 52) has no reference plane: F.Cu is the GND pour's own layer, and a two-layer board has no plane under it"),
    ],
)
def test_a_declared_f_cu_pour_on_two_layers_is_the_plane_over_b_cu(tmp_path: Path, path: str, want):
    """Round 3 (refute-onehome): buck with `Board(..., planes=[("GND", "F.Cu")])`. The router pours GND on F.Cu, the
    only plane B.Cu copper faces, but `_reference_for` read a two-layer pour only on B.Cu: `reference="F.Cu"` on a
    B.Cu Path was "not the plane under B.Cu", and `z_se_ohm=50` on B.Cu said the board "pours B.Cu only when GND is
    a power net, and no NetReq names GND with kind power" (line 41 does; the pour was the declared F.Cu one)."""
    board = 'Board(layers=2, stackup="jlcpcb_2l_1oz")'
    b = _buck(tmp_path, [(board, board[:-1] + ', planes=[("GND", "F.Cu")])'), ('Loop("C_IN1.1"', path + '\nLoop("C_IN1.1"')])
    cs = _cs(b)
    assert cs.planes == (("GND", "F.Cu"),)
    if want is None or "over" in want:
        assert cs.refusals == (), cs.refusals
        line = next(x for x in cs.lines if x.startswith("EN: path R_EN.2 -> U1.EN"))
        assert want is None or want in line, line
    else:
        assert cs.refusals == (want,), cs.refusals


@pytest.mark.parametrize("layers, want", [("", "no reference"), (', layers=["B.Cu"]', "on B.Cu over the F.Cu pour"), (', layers=["B.Cu"], reference="F.Cu"', "on B.Cu over the F.Cu pour")])
def test_a_pair_reads_a_declared_f_cu_pour_on_two_layers(tmp_path: Path, layers: str, want: str):
    """Round 3 (refute-onehome), the Pair home of the same defect: c3_usb with `planes=[("GND", "F.Cu")]`. A B.Cu pair
    runs over the F.Cu pour; an F.Cu pair has no plane facing it, and the refusal says that, not the GND-kind rule."""
    work = tmp_path / "c3"
    shutil.copytree(EXAMPLES / "c3_usb", work, ignore=shutil.ignore_patterns("layout"))
    text = (work / "c3_usb.py").read_text()
    board, pair = 'Board(layers=2, stackup="jlcpcb_2l_1oz")', 'Pair("USB_DP", "USB_DN", kind="usb_hs", z_diff_ohm=90)'
    text = text.replace(board, board[:-1] + ', planes=[("GND", "F.Cu")])').replace('Thermal("U1.49", watts=0.35)\n', "").replace(pair, pair[:-1] + layers + ")")
    write_split(work / "c3_usb.py", text)
    cs = _cs(work / "c3_usb.py")
    if want == "no reference":
        assert "USB_DP: 90 ohm on F.Cu (Pair line 205) has no reference plane: B.Cu, the layer facing F.Cu, is not poured (this board pours F.Cu (GND))" in cs.refusals, cs.refusals
    else:
        assert cs.refusals == (), cs.refusals
        assert any(x.startswith("USB_DP: pair with USB_DN") and want in x for x in cs.lines)
