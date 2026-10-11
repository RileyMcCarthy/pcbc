# Decision 1 (owner, 2026-10-01): the pad neck. A 3 A path leaves R1.1, an 0402 pad 0.48 mm from R1.2 [GND]:
# its 2.276 mm width cannot start at R1.1's centre on any side (the cap reaches R1.2 inside 0.2 mm), so the
# link leaves through a neck at R1.1's own width and widens within constraints.PAD_NECK_MAX_MM of its edge.
# R3 is an off-path load (a path naming every pad would be the net's). GND is a Ground() no power NetReq names, so the two-layer board has no back pour.
from pcbc import *
Board(layers=2, stackup="jlcpcb_2l_1oz")
P = Power("PWR")
G = Ground("GND")
Resistor("R1", "10k", package="0402", mpn="X", lcsc="C1", p1=P, p2=G)
Resistor("R2", "0R", package="1206", mpn="X", lcsc="C1", p1=P, p2=G)
Resistor("R3", "10k", package="0402", mpn="X", lcsc="C1", p1=P, p2=G)
NetReq("PWR", kind="power", volts=5)
Path("PWR", "R1.1", "R2.1", amps=3)







