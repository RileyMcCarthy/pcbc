"""S5 of `docs/stitch-plan.md`: the return-via classifier, and **zero copper is the finding**.

`docs/router-plan.md` tagged R-Z4 **R, V** — route it, verify it — until this slice. S1 built the V
(`route_verify.return_vias`, `tests/test_route_verify_stitch.py`) and measured the population it was
supposed to serve: eleven vias on a referenced net across five boards, **not one of them a distance
question**. node changes the reference *net* across a layer change (`GND` on In1.Cu -> `3V3` on
In2.Cu) and a via joins one net to itself; c3_usb **loses** the reference, because B.Cu is the pour's
own layer and a two-layer board has no second plane to reach. A placer for that population would have
to widen a tolerance until something landed, or drop a via that connects nothing while the tool's own
report blessed it (`docs/stitch-plan.md` §8 item 2).

So the tag is short by one and this file is the missing letter. **C** is the statement the *compiler*
can make, off `board.py`, before a millimetre of copper exists: these two pairs must not change
layers. Nothing said that before this slice, and both boards ship with the pair allowed on both
layers. A via that has already been routed is expensive news; a line in `pcbc check --constraints` is
free and arrives first.

Everything here is pure — `load_board` + `compile_constraints`, no PCB file, no KiCad, no KRT, no
`layout/` directory — which is the property under test as much as the verdicts are. The DS2 Addon is
read-only and copied out of `~/Documents/MaD` before anything touches it.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from pcbc.compile import compile_design
from pcbc.constraints import RETURN_VERDICTS, compile_constraints, return_rule, return_rules
from pcbc.language import load_board
from pcbc.route_scene import plane_targets
from pcbc.stackup import get_stackup

ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = ROOT / "examples"
DS2 = Path.home() / "Documents" / "MaD" / "Hardware" / "DS2Addon" / "pcbc"

# The verdict of every net the compiler gave a `Constraint.reference`, on all five boards, computed
# from `board.py` alone. Measured 2026-09-21. blinky, buck and ds2 are **empty and stay empty** —
# `wants_reference` is set only for `kind="usb_hs"` or a declared `z_se_ohm`, so they declare no
# controlled-impedance net at all, and that is the entry rather than a missing row.
RULES = {
    "blinky": {},
    "buck": {},
    "c3_usb": {"USB_DN": "lost", "USB_DP": "lost"},
    "node": {"USB_DN": "net_change", "USB_DP": "net_change"},
    "ds2": {},
}

# The two moves, verbatim. These are the sentences the slice exists to produce: each one is a
# `board.py` edit that would change the verdict, on a board that ships today with the pair on both
# layers and nothing saying so.
NODE_MOVE = 'Board(planes=[("GND", "In1.Cu"), ("GND", "In2.Cu")])'
C3_MOVE = 'NetReq(layers=["F.Cu"])'


def _src(tmp_path: Path, name: str) -> Path:
    """The board copied into a directory with **no build output in it at all**, so a test that
    accidentally reached for `layout/` would fail rather than read a stale artifact. ds2 is the user's
    real board and is copied out of `~/Documents/MaD`, never opened in place."""
    if name == "ds2":
        if not DS2.exists():
            pytest.skip(f"DS2 Addon not at {DS2}")
        shutil.copytree(DS2, tmp_path / "ds2", ignore=shutil.ignore_patterns("layout"))
        return tmp_path / "ds2" / "ds2_addon.py"
    src = EXAMPLES / name
    shutil.copytree(src, tmp_path / name, ignore=shutil.ignore_patterns("layout"))
    return tmp_path / name / f"{name}.py"


BOARDS = ["blinky", "buck", "c3_usb", "node", "ds2"]


@pytest.mark.parametrize("name", BOARDS)
def test_return_rules_are_pure(tmp_path: Path, name: str):
    """The acceptance: every verdict computed from `board.py`, with no PCB file anywhere near it.

    The directory is a fresh copy with `layout/` excluded, so there is nothing on disk this could
    read even if it wanted to. `compile_constraints` is documented pure ("a `Design` in, a
    `ConstraintSet` out; it never reads a board file"); this is that promise turned into the one
    assertion that would notice if it stopped being true.
    """
    board = _src(tmp_path, name)
    assert not list(board.parent.rglob("*.kicad_pcb")), (name, "the fixture must carry no PCB file")
    cs = compile_constraints(load_board(board))
    assert {r.net: r.verdict for r in return_rules(cs)} == RULES[name], name
    assert all(r.verdict in RETURN_VERDICTS for r in return_rules(cs)), name


@pytest.mark.parametrize("name", BOARDS)
def test_the_compiled_planes_are_the_route_plans_own(tmp_path: Path, name: str):
    """`ConstraintSet.planes` and `route_scene.plane_targets` are two readings of one idea, held
    together here so they cannot drift.

    They ask the same question of two different objects: the compiler asks "did `GND` compile to
    `kind="power"`?", `plane_targets` asks "does a `kind="power"` class's pattern list match `GND`?".
    Measured 2026-09-21 they agree on all five boards — `(("GND","B.Cu"),)` on the four two-layer
    boards and node's declared pair on four. If they ever disagreed, a return verdict would be
    computed against planes the router does not pour, which is the one way this classifier could lie
    without anybody noticing.
    """
    board = _src(tmp_path, name)
    job = compile_design(load_board(board))
    assert job.constraints.planes == plane_targets(job), name


@pytest.mark.parametrize("name", BOARDS)
def test_the_verdicts_are_deterministic(tmp_path: Path, name: str):
    """Same input, byte-identical output — including the moves, which are built from sorted layer
    orders and a carried-through plane list precisely so they are."""
    board = _src(tmp_path, name)
    once = [r.to_dict() for r in return_rules(compile_constraints(load_board(board)))]
    for _ in range(3):
        assert [r.to_dict() for r in return_rules(compile_constraints(load_board(board)))] == once, name


def test_node_prints_the_move_that_puts_one_net_under_both_layers(tmp_path: Path):
    """node's verdict is `net_change`, and the move is the plane declaration that would end it.

    Measured 2026-09-21: `Board(planes=[("GND","In1.Cu"),("3V3","In2.Cu")])` means F.Cu is referenced
    to `GND` on In1.Cu and B.Cu to `3V3` on In2.Cu, both at 0.2104 mm. The *impedance* survives the
    layer change — same height, same 90 ohm — and the *return* does not, because a via joins one net
    to itself. That is the whole reason this is a compile-time sentence and not a via.

    The refusal hands the remaining case over **by name** (`docs/stitch-plan.md` §8 item 2): with two
    nets there, what the return wants is a capacitor between the planes, which is technique 6's
    `Bridge()` and not a stitch's. And it says the honest half too — measured this session,
    `Bridge("GND", "3V3")` is refused today, because `Bridge()` ties two `Ground()` nets and `3V3` is
    declared with `Power()`.
    """
    cs = compile_constraints(load_board(_src(tmp_path, "node")))
    rules = {r.net: r for r in return_rules(cs)}
    assert set(rules) == {"USB_DN", "USB_DP"}
    for net, r in rules.items():
        assert r.verdict == "net_change" and r.change == ("F.Cu", "B.Cu"), (net, r)
        assert r.refs == (("F.Cu", ("In1.Cu", "GND")), ("B.Cu", ("In2.Cu", "3V3"))), (net, r.refs)
        assert NODE_MOVE in r.move, (net, r.move)
        assert "Bridge()" in r.move and "ties two Ground() nets" in r.move, (net, "the net_change case belongs to the ground bridge and is handed over by name")
        assert r.line() in cs.lines, (net, "the rule prints in the compile report")
    assert sum(1 for line in cs.lines if NODE_MOVE in line) == 2, "one line per referenced net"


def test_c3_usb_prints_the_move_that_keeps_the_pair_where_its_reference_is(tmp_path: Path):
    """c3_usb's verdict is `lost`, and the move is the layer list that would end it.

    Two layers, one `GND` pour on B.Cu. F.Cu is referenced to it at 1.53 mm; B.Cu **is** it, and a
    pour cannot be its own reference, so a via takes the pair off its reference and there is no second
    plane anywhere on the board to return to. `NetReq(layers=["F.Cu"])` is the one-line edit; four
    layers is the other answer and the refusal says outright that it is a different board.
    """
    cs = compile_constraints(load_board(_src(tmp_path, "c3_usb")))
    rules = {r.net: r for r in return_rules(cs)}
    assert set(rules) == {"USB_DN", "USB_DP"}
    for net, r in rules.items():
        assert r.verdict == "lost" and r.change == ("F.Cu", "B.Cu"), (net, r)
        assert r.refs == (("F.Cu", ("B.Cu", "GND")), ("B.Cu", None)), (net, r.refs)
        assert C3_MOVE in r.move and "on line 263" in r.move, (net, r.move)
        assert 'Board(layers=4, stackup="jlcpcb_4l_1oz"' in r.move, (net, "the second answer, named as the different board it is")
        assert r.line() in cs.lines, (net, "the rule prints in the compile report")
    assert sum(1 for line in cs.lines if C3_MOVE in line) == 2, "one line per referenced net"


@pytest.mark.parametrize("name,spelling,tail", [
    ("node", "USB_DN/USB_DP", "on B.Cu it is 3V3 on In2.Cu"),
    ("c3_usb", "USB_DN/USB_DP", "on B.Cu there is no reference at all"),
])
def test_the_caveat_on_constraint_reference_prints_once_for_the_pair(tmp_path: Path, name: str, spelling: str, tail: str):
    """`Constraint.reference` is **one layer's** plane, and until this slice nothing said so.

    It is taken from `layers[0]`, it is what the `z_se` line prints "over", and it is what R3/R4 will
    read to pick a rule — while the net is allowed on a second layer where the answer is different on
    both boards that carry one. The caveat is spelled with the **sorted** pair name so both members
    write byte-identical text and `_report`'s note de-duplication prints it once rather than per
    member, the way the width note beside it already behaves.
    """
    cs = compile_constraints(load_board(_src(tmp_path, name)))
    hits = [line for line in cs.lines if line.startswith(f"{spelling}: Constraint.reference is ")]
    assert len(hits) == 1, (name, hits, "printed once, not per member")
    assert tail in hits[0], (name, hits[0])
    for net in ("USB_DN", "USB_DP"):
        c = cs.by_net(net)
        assert c is not None and any(n.startswith(f"{spelling}: Constraint.reference is ") for n in c.notes), (name, net, c.notes if c else None)


@pytest.mark.parametrize("name", ["blinky", "buck", "ds2"])
def test_the_three_boards_with_nothing_to_classify_say_nothing(tmp_path: Path, name: str):
    """Empty, and empty for a reason that is written down rather than inferred from silence.

    `wants_reference` is only set for `kind="usb_hs"` or a declared `z_se_ohm`, so these three carry
    no `Constraint.reference` at all — which also keeps `route_verify`'s D.1 item 6 correct with no
    change: `cs.by_net("GND").reference` is `None` on all five boards, so a return via's net *is* the
    reference net and item 6 never had one to block.
    """
    cs = compile_constraints(load_board(_src(tmp_path, name)))
    assert return_rules(cs) == ()
    assert [c.net for c in cs.constraints if c.reference] == []
    assert cs.by_net("GND") is not None and cs.by_net("GND").reference is None
    assert not [line for line in cs.lines if "the return current crossing" in line]


# --- the two verdicts no real board reaches, on boards built to reach them -------------------------
#
# `kept` and `pinned` are not dead code and must not become it: `kept` is the only verdict under which
# a return-via *placer* would have anything to do, so the day a board reaches it is the day
# `docs/stitch-plan.md` §8 item 2 stops being the right refusal. Neither is reachable from the five,
# which is exactly why they get a fixture.

_HEAD = '''from pcbc import *
VCC = Power("VCC"); GND = Ground("GND")
Resistor("R1", "1k", package="0603", mpn="X", lcsc="C1", p1="D_P", p2="D_N")
Resistor("R2", "1k", package="0603", mpn="X", lcsc="C1", p1=VCC, p2="Z50")
Capacitor("C1", "1uF", package="0603", mpn="X", lcsc="C1", p1=VCC, p2=GND)
'''
_TAIL = '''
for i, r in enumerate(["R1", "R2", "C1"]):
    Place(r, at=(5 + 6 * i, 5)); SchPlace(r, left=20 + 30 * i, top=20)
'''


def _fixture(tmp_path: Path, name: str, board_line: str, reqs: str) -> Path:
    p = tmp_path / f"{name}.py"
    p.write_text(_HEAD + board_line + reqs + _TAIL)
    return p


def test_one_ground_under_both_outer_layers_is_the_only_verdict_a_placer_could_serve(tmp_path: Path):
    """`GND` on In1.Cu **and** In2.Cu: the reference net is the same either side of a via, so a `GND`
    via beside the signal's would carry the return across — `kept`, and no move, because there is
    nothing to move.

    Measured 2026-09-21: no board in this repo reaches it. `route_scene.plane_targets` gives four
    boards `(("GND","B.Cu"),)` and node `(("GND","In1.Cu"),("3V3","In2.Cu"))` — **no board pours one
    net on two facing layers**, which is also why `docs/stitch-plan.md` §8 item 1 ships no plane
    stitch. This fixture is what one would look like.
    """
    board = _fixture(
        tmp_path,
        "kept",
        'Board(width=40, height=25, layers=4, stackup="jlcpcb_4l_1oz", planes=[("GND", "In1.Cu"), ("GND", "In2.Cu")])\n',
        'NetReq("VCC", "GND", kind="power", volts=5, amps=1)\nNetReq("D_P", "D_N", kind="usb_hs")\n',
    )
    cs = compile_constraints(load_board(board))
    rules = {r.net: r for r in return_rules(cs)}
    assert {n: r.verdict for n, r in rules.items()} == {"D_N": "kept", "D_P": "kept"}
    for r in rules.values():
        assert r.refs == (("F.Cu", ("In1.Cu", "GND")), ("B.Cu", ("In2.Cu", "GND"))), r.refs
        assert r.move == "", "a verdict with nothing wrong asks for no edit"
        assert r.line() == f"{r.net}: returns kept, F.Cu and B.Cu are both referenced to GND, so a GND via beside the signal's carries the return across"
        assert r.line() in cs.lines


def test_a_net_that_cannot_change_layer_is_pinned_and_says_which_reason(tmp_path: Path):
    """"Nothing to classify" and "nothing went wrong" read the same in an empty report and are not the
    same fact, so `pinned` is printed and its `why` names which of its two causes fired: one layer, or
    vias refused on a net that has two."""
    one = _fixture(
        tmp_path,
        "one_layer",
        'Board(width=40, height=25, layers=4, stackup="jlcpcb_4l_1oz", planes=[("GND", "In1.Cu")])\n',
        'NetReq("VCC", "GND", kind="power", volts=5, amps=1)\nNetReq("Z50", z_se_ohm=50, layers=["F.Cu"])\n',
    )
    cs = compile_constraints(load_board(one))
    (r,) = return_rules(cs)
    assert (r.net, r.verdict, r.change) == ("Z50", "pinned", None)
    assert r.why == "Z50 may only use F.Cu, so no via changes its reference" and r.move == ""
    assert f"{r.net}: returns pinned, {r.why}" in cs.lines

    no_via = _fixture(
        tmp_path,
        "no_via",
        'Board(width=40, height=25, layers=4, stackup="jlcpcb_4l_1oz", planes=[("GND", "In1.Cu")])\n',
        'NetReq("VCC", "GND", kind="power", volts=5, amps=1)\nNetReq("Z50", z_se_ohm=50, vias=False)\n',
    )
    (r2,) = return_rules(compile_constraints(load_board(no_via)))
    assert (r2.net, r2.verdict) == ("Z50", "pinned") and "vias are refused on Z50" in r2.why


def test_reference_of_names_the_net_and_refuses_the_pours_own_layer():
    """`Stackup.reference_of` is `height_to_reference` with the net named, and the net is the whole
    answer to a return question: two layers referenced to two nets cannot be joined by one via.

    Measured 2026-09-21, and both halves matter. On node's four layers the two outer layers have
    references at the same 0.2104 mm and **different nets**, so the impedance survives a layer change
    and the return does not. On two layers B.Cu comes back `None`, because the pour's own layer cannot
    be its own reference — which is the `lost` verdict's entire mechanism.
    """
    four, two = get_stackup("jlcpcb_4l_1oz"), get_stackup("jlcpcb_2l_1oz")
    node_planes = (("GND", "In1.Cu"), ("3V3", "In2.Cu"))
    assert four.reference_of("F.Cu", node_planes) == ("In1.Cu", "GND")
    assert four.reference_of("B.Cu", node_planes) == ("In2.Cu", "3V3")
    assert four.dielectric_between("F.Cu", "In1.Cu")[0] == four.dielectric_between("B.Cu", "In2.Cu")[0] == 0.2104
    pour = (("GND", "B.Cu"),)
    assert two.reference_of("F.Cu", pour) == ("B.Cu", "GND")
    assert two.reference_of("B.Cu", pour) is None
    assert two.dielectric_between("F.Cu", "B.Cu")[0] == 1.53
    assert two.reference_of("F.Cu", ()) is None, "a two-layer board whose GND is not a power net gets no pour and no reference"


def test_return_rule_takes_the_worse_of_two_verdicts_and_the_order_is_written_down(tmp_path: Path):
    """A net allowed on three layers can produce two different verdicts; the net's is the worse, and
    "worse" is `RETURN_VERDICTS`' order rather than whichever pair the loop reached first.

    `lost` outranks `net_change` because it is the larger statement: no reference at all on that side,
    so the impedance is uncontrolled there too. Measured 2026-09-21 the ordering is **never exercised
    by a real board** — every referenced net on all five is allowed on exactly two layers — so it is
    tested here rather than left to be discovered by the first board that has three.
    """
    board = _fixture(
        tmp_path,
        "three",
        'Board(width=40, height=25, layers=4, stackup="jlcpcb_4l_1oz", planes=[("GND", "In1.Cu")])\n',
        'NetReq("VCC", "GND", kind="power", volts=5, amps=1)\n'
        'NetReq("Z50", z_se_ohm=50, layers=["F.Cu", "In2.Cu", "B.Cu"])\n',
    )
    cs = compile_constraints(load_board(board))
    (r,) = return_rules(cs)
    stack = cs.stackup
    assert stack.reference_of("F.Cu", cs.planes) == ("In1.Cu", "GND")
    assert stack.reference_of("In2.Cu", cs.planes) == ("In1.Cu", "GND"), "In1.Cu is the plane above In2.Cu"
    assert stack.reference_of("B.Cu", cs.planes) is None, "In2.Cu carries no plane, so B.Cu has none"
    assert r.verdict == "lost", "F.Cu->In2.Cu alone would be `kept`; the pair that reaches B.Cu is `lost` and `lost` wins"
    assert RETURN_VERDICTS.index("lost") < RETURN_VERDICTS.index("net_change") < RETURN_VERDICTS.index("kept")
    assert 'NetReq(layers=["F.Cu", "In2.Cu"])' in r.move, r.move


def test_a_rule_is_only_made_for_a_net_the_compiler_gave_a_reference(tmp_path: Path):
    """The population is `route_verify.referenced_nets`', and `return_rule` says so by returning
    `None` rather than inventing a verdict for a net with no impedance target."""
    cs = compile_constraints(load_board(_src(tmp_path, "node")))
    gnd = cs.by_net("GND")
    assert gnd is not None and gnd.reference is None
    assert return_rule(cs.stackup, cs.planes, gnd) is None


def test_a_two_layer_board_whose_ground_is_not_a_power_net_is_told_its_reference_will_never_exist(tmp_path: Path):
    """The classifier's one unlooked-for finding, and it is about a number rather than a return.

    `_reference_for` asks `height_to_reference(..., two_layer_pour=True)` **unconditionally**, so any
    two-layer board's F.Cu gets `Constraint.reference = "B.Cu pour"` — while `krt_plan` only writes
    that pour when `GND` is one of the power nets. Measured 2026-09-21 on the board below: `GND` is
    declared with `Ground()` and named by no `NetReq`, so it compiles to `kind="generic"`,
    `plane_targets` is `()` and **no pour is ever written** — yet the compiler prints
    `reference "B.Cu pour"` and solves the 50 ohm width as coplanar-with-ground against it.

    The four boards in this repo all name `GND` in a `kind="power"` NetReq, so none of them is in this
    state and nothing changes for them. The rule is what says so out loud: `lost`, with both layers
    unreferenced, and the move is the `NetReq("GND", kind="power")` line that would make the pour real
    — the same trap `docs/stitch-plan.md` §7.1 names for blinky one stage later, caught here before
    any copper exists at all.
    """
    board = _fixture(
        tmp_path,
        "no_pour",
        'Board(width=40, height=25, layers=2, stackup="jlcpcb_2l_1oz")\n',
        'NetReq("Z50", z_se_ohm=50)\n',
    )
    cs = compile_constraints(load_board(board))
    assert cs.planes == (), "GND is kind=generic here, so krt_plan writes no gnd_pour"
    z = cs.by_net("Z50")
    assert z is not None and z.reference == "B.Cu pour", "the compiler names a pour this board will not have"
    (r,) = return_rules(cs)
    assert r.verdict == "lost" and r.refs == (("F.Cu", None), ("B.Cu", None))
    assert r.why == "neither F.Cu nor B.Cu has a plane above or below it, so there is no reference on this board to return to"
    assert 'NetReq("GND", kind="power")' in r.move, r.move
