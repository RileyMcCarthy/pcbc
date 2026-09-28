"""The copper bar and the blocking analysis: numbers and moves from a routed board."""

from __future__ import annotations

import shutil
from pathlib import Path

from pcbc.build import pcb_job
from pcbc.copper import pads_by_net
from pcbc.copper_bar import airwire_mm, copper_bar, off_45, segments
from pcbc.language import load_board

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"

BOARD = """(kicad_pcb (version 20240108) (generator "pcbc")
\t(net 0 "")
\t(net 1 "A")
\t(net 2 "B")
\t(footprint "pcbc:X"
\t\t(layer "F.Cu")
\t\t(at 0 0 0)
\t\t(property "Reference" "U1" (at 0 0 0))
\t\t(pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu") (net "A"))
\t\t(pad "2" smd rect (at 10 0) (size 1 1) (layers "F.Cu") (net "A"))
\t\t(pad "3" smd rect (at 5 3) (size 1 1) (layers "F.Cu") (net "B"))
\t)
\t(segment
\t\t(start 0.000000 0.000000)
\t\t(end 4.000000 0.000000)
\t\t(width 0.2)
\t\t(layer "F.Cu")
\t\t(net "A")
\t\t(uuid "s1")
\t)
\t(segment
\t\t(start 4.000000 0.000000)
\t\t(end 4.050000 0.050000)
\t\t(width 0.2)
\t\t(layer "F.Cu")
\t\t(net "A")
\t\t(uuid "s2")
\t)
\t(segment
\t\t(start 4.050000 0.050000)
\t\t(end 9.000000 0.400000)
\t\t(width 0.2)
\t\t(locked yes)
\t\t(layer "F.Cu")
\t\t(net "A")
\t\t(uuid "s3")
\t)
\t(via
\t\t(at 9.000000 0.400000)
\t\t(size 0.5)
\t\t(drill 0.3)
\t\t(layers "F.Cu" "B.Cu")
\t\t(net "A")
\t\t(uuid "v1")
\t)
)
"""


def test_the_bar_counts_detour_vias_stairs_and_odd_angles():
    segs = segments(BOARD)
    assert [s["length"] > 0 for s in segs] == [True, True, True] and segs[2]["locked"] and not segs[0]["locked"]
    assert [off_45(s) for s in segs] == [False, False, True]  # 0 degrees, 45 degrees, 4 degrees
    bar = copper_bar(BOARD)
    a = bar["nets"]["A"]
    assert a["airwire_mm"] == 10.0 and a["segments"] == 3 and a["vias"] == 1 and a["micro"] == 1 and a["off45"] == 1
    assert a["routed_mm"] == round(4 + 0.05 * 2**0.5 + ((9 - 4.05) ** 2 + 0.35**2) ** 0.5, 3)
    assert a["detour"] == round(a["routed_mm"] / 10.0, 2)
    assert "B" not in bar["nets"]  # one pad: nothing to route
    t = bar["totals"]
    assert (t["segments"], t["vias"], t["off45"], t["micro"], t["nets"]) == (3, 1, 1, 1, 1)
    assert t["worst_detour"] == ["A", a["detour"]]
    assert bar["lines"][0].startswith("copper: 3 segments, 1 vias") and "1 off 0/45/90, 1 under 0.2 mm" in bar["lines"][0]


def test_airwire_is_the_spanning_tree_of_the_pads():
    assert airwire_mm([(0, 0), (3, 0), (10, 0)]) == 10.0
    assert airwire_mm([(0, 0), (3, 4), (0, 8)]) == 10.0  # two 5 mm legs, not the 8 mm hypotenuse plus one
    assert airwire_mm([(1, 1)]) == 0.0
