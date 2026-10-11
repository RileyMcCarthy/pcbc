"""The board the pair gate's fatal branch is proven on (the second refutation round, major 3).

`build._pair_gate` fails a board whose differential pair runs uncoupled for longer than its
`uncoupled_mm`. On the example boards it can only ever be an `also:` line — c3_usb and node stop at an
unrouted `VBUS` first — so without this board the fatal branch had no test. Four resistors stand in
for a connector and a receiver: the two halves of `D_P`/`D_N` start 4 mm apart at each end and must
converge to couple, so the uncoupled length is set by this geometry and not by what else is on the
board. `UNCOUPLED_MM` is the one line the test rewrites.
"""

from pcbc import Board, Path, Ground, Net, NetReq, Pair, Resistor, SchPlace

GND = Ground("GND")
DP = Net("D_P")
DN = Net("D_N")

r = dict(package="0402", mpn="0402WGF220JTCE", lcsc="C25092", manufacturer="UniOhm")
Resistor("R1", "22R", p1=DP, p2=GND, **r)
Resistor("R2", "22R", p1=DN, p2=GND, **r)
Resistor("R3", "22R", p1=DP, p2=GND, **r)
Resistor("R4", "22R", p1=DN, p2=GND, **r)

Board(layers=2, stackup="jlcpcb_2l_1oz")
UNCOUPLED_MM = 1.0
Pair("D_P", "D_N", kind="usb_hs", z_diff_ohm=90, uncoupled_mm=UNCOUPLED_MM)
NetReq("GND", kind="power")
# One chain of one length on each half: what makes the router route the pair as one object
# (`route_native.route_pairs`); without it the halves are two nets and nothing couples.
Path("D_P", "R1.1", "R3.1")
Path("D_N", "R2.1", "R4.1")










