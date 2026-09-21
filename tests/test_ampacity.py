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
        "VIN: J1.1->U1.1 carries 1.21 A of 2 A through its narrowest 0.3905 mm track "
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
