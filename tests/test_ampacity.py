"""What a routed power net actually carries, end to end (`src/pcbc/ampacity.py`).

The S7 review's first finding, and the four that restate it: `power_ampacity_failures` read the net's
**widest** track, so one wide segment satisfied a whole net and the gate could not fail. These tests
put the replacement to boards built by hand, where every number is chosen rather than measured.
"""

from __future__ import annotations

from pcbc.ampacity import CURVE_EPS, VIA_PARALLEL_MM, bottleneck_lines, power_bottlenecks
from pcbc.compile import compile_design
from pcbc.language import load_board
from pcbc.stackup import current_width_mm, get_stackup, ipc2221_amps, track_amps, via_amps

from pathlib import Path

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"


def test_the_current_a_width_carries_is_the_inverse_of_the_width_a_current_asks_for():
    """`stackup.track_amps` against `stackup.current_width_mm`, round-tripped.

    `current_width_mm` is `max(IPC-2221 external, IPC-2152 with modifiers)`, so the current a width is
    good for is the `min` of the two curves — the binding one in the same direction. Widening to what
    a current asks and asking the current back must never give less than went in, and the only slack
    it needs is the 3 dp the forward function rounds to (`CURVE_EPS`).
    """
    stack = get_stackup("jlcpcb_4l_1oz")
    for amps in (0.1, 0.274, 0.5, 1.0, 2.0, 5.0):
        for plane_h in (None, 0.2104, 1.53):
            w = current_width_mm(amps, 10.0, stack, plane_h).value
            back = track_amps(w, 10.0, stack, plane_h)
            assert back >= amps * (1.0 - CURVE_EPS), (amps, w, back, plane_h, "IPC-2221B eq. 6-2 and IPC-2152 Fig 5-1, inverted")
    # The three numbers the review pinned by hand, at 1 oz and a 10 C rise on the external curve.
    assert ipc2221_amps(0.3905) == 1.21, "buck's VIN necks to half its class width: 1.21 A of 2 A"
    assert ipc2221_amps(0.532) == 1.513, "and _rib_width used to hand U1.3 a 0.532 mm rib for its whole 2 A"
    assert ipc2221_amps(0.127) == 0.536, "c3_usb's VBUS scrapes its 0.5 A on the curve, 7 % over, at a third of its class"
    assert ipc2221_amps(0.2) == 0.745 and via_amps(0.2) == 0.527, "node's VBUS: 0.2 mm of track, and a 0.2 mm drill under it"
    assert track_amps(0.0, 10.0, stack, None) == 0.0


BOARD = """(kicad_pcb
\t(net 0 "")
\t(net 1 "VBUS")
\t(footprint "J"
\t\t(layer "F.Cu")
\t\t(at 0 0)
\t\t(property "Reference" "J1")
\t\t(pad "1" smd rect (at 1 1) (size 1 1) (layers "F.Cu") (net 1 "VBUS"))
\t)
\t(footprint "U"
\t\t(layer "F.Cu")
\t\t(at 0 0)
\t\t(property "Reference" "U1")
\t\t(pad "1" smd rect (at 20 1) (size 1 1) (layers "F.Cu") (net 1 "VBUS"))
\t)
\t(segment (start 1 1) (end 10 1) (width 0.781) (layer "F.Cu") (net 1))
\t(segment (start 10 1) (end 20 1) (width WIDE) (layer "F.Cu") (net 1))
)
"""


def _board(width: str) -> str:
    return BOARD.replace("WIDE", width)


def test_a_net_is_as_good_as_its_narrowest_series_copper_and_not_its_widest():
    """The finding, as a board: 9 mm of 0.781 mm copper in series with 10 mm of 0.3905 mm, on a 2 A
    rail. The old gate read `max` and passed it; the bottleneck reads the path and says 1.21 A."""
    job = compile_design(load_board(EXAMPLES / "buck" / "buck.py"))
    rows = power_bottlenecks(job, _board("0.3905"))
    assert rows["VBUS"] if "VBUS" in rows else True  # buck has no VBUS: the fixture net is VIN's class
    rows = power_bottlenecks(job, _board("0.3905").replace("VBUS", "VIN"))
    r = rows["VIN"]
    assert (r["carries"], r["width_mm"], r["kind"], r["where"]) == (1.21, 0.3905, "track", "J1.1->U1.1")
    assert r["verdict"] == "under current" and r["amps"] == 2.0 and r["need_mm"] == 0.781
    assert r["under_mm"] == 10.0, "the millimetres under the class, so the ledger is a length and not a boolean"
    wide = power_bottlenecks(job, _board("0.781").replace("VBUS", "VIN"))["VIN"]
    assert wide["verdict"] == "ok" and wide["carries"] == 1.999 and wide["under_mm"] == 0.0
    assert bottleneck_lines({"VIN": r}) == [
        "VIN: J1.1->U1.1 carries 1.21 A of 2 A through its narrowest 0.3905 mm track at 15,1 on F.Cu "
        "(10 mm under the 0.781 mm class, IPC asks 0.781 mm) [under current]"
    ]


VIA_BOARD = """(kicad_pcb
\t(net 0 "")
\t(net 1 "VIN")
\t(footprint "J"
\t\t(layer "F.Cu")
\t\t(at 0 0)
\t\t(property "Reference" "J1")
\t\t(pad "1" smd rect (at 1 1) (size 1 1) (layers "F.Cu") (net 1 "VIN"))
\t)
\t(footprint "U"
\t\t(layer "F.Cu")
\t\t(at 0 0)
\t\t(property "Reference" "U1")
\t\t(pad "1" smd rect (at 20 1) (size 1 1) (layers "B.Cu") (net 1 "VIN"))
\t)
\t(segment (start 1 1) (end 10 1) (width 0.781) (layer "F.Cu") (net 1))
VIAS
\t(segment (start 10 OFF) (end 20 1) (width 0.781) (layer "B.Cu") (net 1))
)
"""


def _via_board(*offsets: float) -> str:
    vias = "\n".join(
        f'\t(via (at 10 {o:g}) (size 0.35) (drill 0.2) (layers "F.Cu" "B.Cu") (net 1))' for o in offsets
    )
    seg = "\n".join(
        f"\t(segment (start 10 1) (end 10 {o:g}) (width 0.781) (layer \"F.Cu\") (net 1))" for o in offsets if o != 1
    )
    return VIA_BOARD.replace("VIAS", vias + ("\n" + seg if seg else "")).replace("OFF", "1")


def test_a_via_on_a_power_path_carries_its_own_current_and_a_stitch_cluster_carries_together():
    """Finding 2. node's `VBUS` runs its declared 1 A through two single 0.2 mm vias, each rated
    0.527 A, and nothing in the build looked at a via's current at all: `power_ampacity_failures`
    never read `rec['via']`, `vias_on_no_via_nets` only checks `vias=False` nets, and `per_change`
    appears nowhere outside `constraints.py`.

    A lone via on the only path is a cut edge and carries one via's worth. Vias within
    `VIA_PARALLEL_MM` were placed as a group and carry together, which is what keeps a stitching
    field from reading as a bottleneck."""
    job = compile_design(load_board(EXAMPLES / "buck" / "buck.py"))
    one = power_bottlenecks(job, _via_board(1))["VIN"]
    assert (one["carries"], one["kind"], one["verdict"]) == (0.527, "via", "under current"), one
    assert one["width_mm"] == 0.0, "a via has no width a class can be compared against; `kind` says which number to read"
    pair = power_bottlenecks(job, _via_board(1, 1.7))["VIN"]
    assert (pair["carries"], pair["kind"]) == (1.054, "via"), (pair, "0.7 mm apart: a stitch pair, and 2 x 0.527 A")
    apart = power_bottlenecks(job, _via_board(1, 3.0))["VIN"]
    assert apart["carries"] == 0.527, (apart, f"{VIA_PARALLEL_MM:g} mm apart or more is two vias placed for two reasons")


def test_a_poured_net_is_exempt_because_the_plane_is_its_conductor():
    """And the exemption is the **zone in the file**, not `job.planes`, which is `()` on buck, c3_usb
    and ds2 while every one of those boards has a poured `GND` in its routed board (finding 1). Read
    the wrong one and a 2 A ground plane fires the gate for the 0.127 mm stub that welds a pad to it.
    """
    job = compile_design(load_board(EXAMPLES / "buck" / "buck.py"))
    bare = _board("0.127").replace("VBUS", "GND")
    assert power_bottlenecks(job, bare)["GND"]["verdict"] == "under current"
    poured = bare.replace('\t(segment (start 1 1)', '\t(zone (net 1) (net_name "GND") (layer "B.Cu"))\n\t(segment (start 1 1)')
    r = power_bottlenecks(job, poured)["GND"]
    assert r["zoned"] and r["verdict"] == "ok" and r["carries"] is None
    assert bottleneck_lines({"GND": r}) == ["GND: a zone carries 2 A; no series track to measure"]


SLOT_BOARD = """(kicad_pcb
\t(net 0 "")
\t(net 1 "GND")
\t(segment (start 0 0) (end 10 0) (width 0.4) (layer "F.Cu") (net 1))
\t(segment (start 0 0.45) (end 10 0.45) (width 0.4) (layer "F.Cu") (net 1))
)
"""


def test_same_net_copper_closer_than_the_floor_is_counted_unless_copper_fills_it():
    """Finding 16, and the false positive it has to survive.

    `route_scene._pair_clashes` skips the copper and mask rules for a same-net pair by design, KiCad
    exempts same-net pairs from clearance entirely, and KRT treats its own net as free — so nothing
    anywhere looks at how close a net's own copper comes to itself. Two parallel 0.4 mm tracks 0.45 mm
    centre to centre leave a **0.050 mm** slot, which is what node's leftover leaves beside its locked
    `VBUS` spine on a board whose process floor is 0.0889 mm.

    The gap has to be bare laminate to count, and the sampling is what makes that exact: a mitre
    corner and a continuous run are also two pieces of copper 0.05 mm apart, with copper between them.
    """
    from pcbc.route_verify import same_net_slots

    got = same_net_slots(SLOT_BOARD, 0.0889)
    assert len(got) == 1 and got[0]["gap"] == 0.05 and got[0]["layer"] == "F.Cu" and got[0]["net"] == "GND", got
    assert same_net_slots(SLOT_BOARD, 0.04) == [], "a gap wider than the floor is not a slot"
    # The false positive the sampling exists to remove: a mitred corner is also two pieces of one net
    # 0.05 mm apart, and the mitre leg between them is the copper that fills the gap. Every
    # sub-floor same-net pair on these five boards that is **not** a slot is one of these.
    mitred = (
        '(kicad_pcb\n\t(net 0 "")\n\t(net 1 "GND")\n'
        '\t(segment (start 0 0) (end 5 0) (width 0.4) (layer "F.Cu") (net 1))\n'
        '\t(segment (start 5 0) (end 5.2 0.2) (width 0.4) (layer "F.Cu") (net 1))\n'
        '\t(segment (start 5.2 0.2) (end 5.2 5) (width 0.4) (layer "F.Cu") (net 1))\n)\n'
    )
    assert same_net_slots(mitred, 0.0889) == [], "a mitre's own leg fills the gap between the two legs it joins"
    other = SLOT_BOARD.replace('\t(net 1 "GND")', '\t(net 1 "GND")\n\t(net 2 "VCC")').replace(
        "(start 0 0.45) (end 10 0.45) (width 0.4) (layer \"F.Cu\") (net 1)",
        "(start 0 0.45) (end 10 0.45) (width 0.4) (layer \"F.Cu\") (net 2)",
    )
    assert same_net_slots(other, 0.0889) == [], "a different-net pair is the clearance table's question, not this one"


def _row(**kw) -> dict:
    row = {
        "net": "VIN",
        "amps": 2.0,
        "carries": 1.21,
        "width_mm": 0.3905,
        "kind": "track",
        "where": "C_IN1.1->R_EN.1",
        "at_mm": "17.4,10.8 on F.Cu",
        "need_mm": 0.781,
        "class_mm": 0.781,
        "under_mm": 19.394,
        "zoned": False,
        "verdict": "under current",
    }
    row.update(kw)
    return row


def test_power_moves_says_only_what_is_wrong_and_what_to_edit():
    """The half the measurement was missing: the build printed `copper: verified` and exited 0 while
    `fab/report.json` held a 2 A rail carrying 1.21 A. `bottleneck_lines` is the ledger — every power
    net, passing or not. `power_moves` is the move — only the nets that fail, each naming the edits."""
    from pcbc.ampacity import power_moves

    rows = {
        "VIN": _row(),
        "5V": _row(net="5V", carries=1.999, amps=2.0, under_mm=0.0, verdict="ok"),
        "GND": _row(net="GND", zoned=True, verdict="under current"),
    }
    got = power_moves(rows, ("F.Cu", "In1.Cu", "In2.Cu", "B.Cu"))
    assert len(got) == 1, got
    assert got[0] == (
        "VIN: declared 2 A, carries 1.21 A through its narrowest 0.3905 mm track at 17.4,10.8 on F.Cu "
        "(19.394 mm of this net is narrower than its 0.781 mm class; path C_IN1.1->R_EN.1). "
        "Place() the parts either side of that copper closer together, or pour it: "
        'add ("VIN", "In1.Cu") to your Board(planes=...), or change amps= on the NetReq that '
        'already declares "VIN"'
    ), got[0]
    # Every edit is an edit to a line the board already has. A second `NetReq("VIN", ...)` is
    # refused twice over — `kind="generic"` takes no `amps=`, and the net is already named by
    # another NetReq — and a second bare `Board()` raises at load, before `check` runs.
    assert "NetReq(\"VIN\", amps=" not in got[0] and "Board(planes=[(" not in got[0], got[0]
    # A poured net is exempt for the same reason `power_bottlenecks` does not walk it: the plane is
    # the conductor. A passing net says nothing at all, which is what makes the list readable.
    assert power_moves({"GND": _row(net="GND", zoned=True)}, ("F.Cu", "B.Cu")) == []
    assert power_moves({"5V": _row(net="5V", verdict="ok")}, ("F.Cu", "B.Cu")) == []


def test_the_pour_move_is_only_offered_where_pcbc_actually_pours():
    """Three ways this move was wrong before it was right, and the third is the one that matters.

    It named `In1.Cu` to a two-layer board, which is a second error to debug. It named `In1.Cu` to
    node, which is where node's `GND` plane is — a second plane over the ground plane, on a board
    whose own D.5 check then asks why `GND` is in two pieces. And on two layers it named `B.Cu` at
    all: `route.krt_plan` reads `planes=` only when `job.layers > 2`, so that edit pours **no
    copper whatever** while `power_bottlenecks` would have marked the net zoned and stopped
    measuring it. A move that makes the warning go away and the board no better is worse than none.
    """
    from pcbc.ampacity import power_moves

    two = power_moves({"VIN": _row()}, ("F.Cu", "B.Cu"))[0]
    assert "Board(planes" not in two and "pour" not in two, two
    assert "Place()" in two and 'change amps= on the NetReq that already declares "VIN"' in two, two
    four = power_moves({"VIN": _row()}, ("F.Cu", "In1.Cu", "In2.Cu", "B.Cu"))[0]
    assert 'add ("VIN", "In1.Cu") to your Board(planes=...)' in four, four
    # node's own stackup: both inner layers already poured, so there is no plane layer to offer.
    node = power_moves(
        {"VBUS": _row(net="VBUS")},
        ("F.Cu", "In1.Cu", "In2.Cu", "B.Cu"),
        (("GND", "In1.Cu"), ("3V3", "In2.Cu")),
    )[0]
    assert "Board(planes" not in node and "In1.Cu" not in node and "In2.Cu" not in node, node
    # One inner free: that one, and never an outer layer of a four-layer board, where the parts are.
    one = power_moves({"VIN": _row()}, ("F.Cu", "In1.Cu", "In2.Cu", "B.Cu"), (("GND", "In1.Cu"),))[0]
    assert 'add ("VIN", "In2.Cu") to your Board(planes=...)' in one, one


def test_a_declared_plane_never_exempts_a_net_that_has_no_zone():
    """The other half of the same trap, in the measurement rather than the move: `zoned` is the zone
    in the routed **file** and not `Board(planes=...)`. A predictive exemption would have let the
    pour move silence a two-layer rail that was never poured — and on a four-layer board a pour that
    failed would go unmeasured, which is the case the exemption exists to catch."""
    job = compile_design(load_board(EXAMPLES / "node" / "node.py"))
    assert job.planes, "node declares two planes, so this test is asking something"
    bare = _board("0.127").replace("VBUS", "3V3")
    assert power_bottlenecks(job, bare)["3V3"]["verdict"] == "under current", "declared, not poured, still measured"


def test_power_moves_separates_the_curve_from_the_floor_and_the_open():
    """The three verdicts are three different faults and the move has to read as three different
    sentences. c3_usb's `VBUS` is `under floor` — 0.127 mm carries 0.536 A of its 0.5 A on the IPC
    curve with 7 % margin, and is under pcbc's own 0.150 mm manufacturability floor. An `open` is a
    tool bug: the copper gate already refuses an unrouted net, so nothing an author edits fixes it."""
    from pcbc.ampacity import power_moves

    floor = power_moves(
        {"VBUS": _row(net="VBUS", amps=0.5, carries=0.536, width_mm=0.127, need_mm=0.15, class_mm=0.4, under_mm=24.068, verdict="under floor")},
        ("F.Cu", "B.Cu"),
    )[0]
    assert "carries 0.536 A of 0.5 A on the curve" in floor and "under pcbc's 0.15 mm floor" in floor, floor
    assert "declared 0.5 A, carries" not in floor, floor
    opened = power_moves({"VIN": _row(carries=None, kind="open", verdict="open")}, ("F.Cu", "B.Cu"))[0]
    assert "no copper joins C_IN1.1->R_EN.1" in opened and "report it" in opened, opened
    assert "Place()" not in opened, "an open rail is not something a Place() line fixes"


def test_power_moves_survives_a_row_with_no_verdict_key():
    """`bottleneck_lines` reads `r['verdict']` and would KeyError; a row that predates the field is
    `ok` by the dataclass's own default, and a move that raises is worse than a move that is quiet."""
    from pcbc.ampacity import power_moves

    row = _row()
    row.pop("verdict")
    assert power_moves({"VIN": row}, ("F.Cu", "B.Cu")) == []


def test_an_unmeasured_net_is_not_a_shortfall():
    """A net with fewer than two pads is never walked, so `carries` stays `math.inf` and `width_mm`
    stays 0.0 — and the floor test, `0.0 < need_mm`, was always true, stamping it `under floor`.
    `bottleneck_lines` papered over it ("carries 0 A" inside a ledger where every net appears);
    `power_moves` turned it into a claim about copper nobody measured, with an empty location, and
    under `--strict-power` it failed the build on a net with no measured shortfall at all."""
    import math
    from dataclasses import asdict

    from pcbc.ampacity import Bottleneck, _verdict, power_moves

    unwalked = Bottleneck(
        net="VREF", amps=0.5, carries=math.inf, width_mm=0.0, kind="track", where="",
        need_mm=0.4, class_mm=0.4, under_mm=0.0, zoned=False,
    )
    assert _verdict(unwalked).verdict == "ok", "nothing was measured, so nothing is wrong"
    assert power_moves({"VREF": asdict(_verdict(unwalked))}, ("F.Cu", "B.Cu")) == []
    # And the branch still fires on a row that WAS walked and really is under the floor.
    walked = Bottleneck(**{**asdict(unwalked), "carries": 0.6, "width_mm": 0.127, "where": "A.1->B.1"})
    assert _verdict(walked).verdict == "under floor", walked


def test_a_via_bottleneck_is_not_a_placement_move_and_says_what_it_is_instead():
    """`docs/stitch-plan.md` §5's one sentence: the move a **via** bottleneck gets.

    Every edit `power_moves` offers is about width, and a barrel has none — `stackup.via_amps` is the
    drill and the plating, both the fab's — so "move the parts either side closer together" is the
    same class of wrong answer as naming the pad pair instead of the neck's coordinate was: it names
    an edit that cannot move a single ampere. What a via bottleneck wants is a second via beside it,
    which is technique 1 — and since `docs/stitch-plan.md` S4 `patterns/stitch.py` places one
    wherever the ring around the anchor has room, so the sentence points at the `stitch` move that
    names what took the room rather than at a slice that has not shipped.
    `route_verify.via_parallelism` is where the count lives.

    The edits that still apply do still apply: the pour, where the board has a free inner layer, and
    `amps=` on the `NetReq` the net already has. A via row can never be `under floor` — `_verdict`
    only stamps that on a track — so the move is never left with no edit at all.
    """
    from pcbc.ampacity import power_moves

    row = _row(net="VBUS", amps=1.0, carries=0.527, width_mm=0.0, kind="via", where="C_VBUS.1->U3.5",
               at_mm="32.3,34.9 on B.Cu/F.Cu/In1.Cu/In2.Cu", need_mm=0.4, class_mm=0.4, under_mm=6.476)
    got = power_moves({"VBUS": row}, ("F.Cu", "In1.Cu", "In2.Cu", "B.Cu"), (("GND", "In1.Cu"), ("3V3", "In2.Cu")))
    assert got == [
        "VBUS: declared 1 A, carries 0.527 A through its narrowest single via at "
        "32.3,34.9 on B.Cu/F.Cu/In1.Cu/In2.Cu (6.476 mm of this net is narrower than its 0.4 mm class; "
        "path C_VBUS.1->U3.5). A barrel's rating is a count of vias and not a width — `stackup.via_amps` "
        "is the drill and the plating, both the fab's — so no Place() and no shorter path moves another "
        "ampere through it; what this wants is a second via beside it, which `patterns/stitch.py` "
        "places where the ring has room, so a `stitch` move above this line names what took the room. "
        "Change amps= on the NetReq that already declares \"VBUS\""
    ], got
    # The *edit* is gone; the word survives only inside the sentence that says why it is gone.
    assert "Place() the parts either side" not in got[0] and "no Place() and no shorter path" in got[0], got[0]
    assert "Place() the parts either side" in power_moves({"VIN": _row()}, ("F.Cu", "B.Cu"))[0], "and a track keeps the edit it always had"
    # One free inner layer: the pour is still an edit that pours copper, so it is still offered.
    poured = power_moves({"VBUS": row}, ("F.Cu", "In1.Cu", "In2.Cu", "B.Cu"), (("GND", "In1.Cu"),))[0]
    assert 'add ("VBUS", "In2.Cu") to your Board(planes=...)' in poured and "Place() the parts" not in poured, poured


def test_the_move_names_where_the_copper_is_and_not_the_pair_that_found_it():
    """`where` is a pad pair, and every pair whose path crosses one neck ties at that neck's amps —
    so the reported pair is the first in sorted order among the tied ones. buck's is
    `C_IN1.1->R_EN.1`, a 100 k enable pull-up drawing 0.12 mA, while its 2 A path is `J_IN.1->U1.3`.
    "Move those two parts closer" named two parts that are not on the rail; the coordinate is."""
    from pcbc.ampacity import power_moves

    got = power_moves({"VIN": _row()}, ("F.Cu", "B.Cu"))[0]
    assert "at 17.4,10.8 on F.Cu" in got and "path C_IN1.1->R_EN.1" in got, got
    assert "those two parts" not in got, got
    # A row from before the field, or a via with no hull: the pair is all there is, and it says so.
    row = _row()
    row.pop("at_mm")
    old = power_moves({"VIN": row}, ("F.Cu", "B.Cu"))[0]
    assert "on the C_IN1.1->R_EN.1 path" in old and "path C_IN1.1->R_EN.1)" not in old, old
