# Refutation round 2 (2026-10-01): the physics refuters' fixture. PWR carries a 2 A path R1 -> R2 -> R3 and a
# feedback tap R3 -> R5; R4 is an off-path load; W (layout.core.py) is an F.Cu wall at x = 20, so the 2 A
# link must change layers; GND is a Ground() named by no power NetReq, so a two-layer board has no pour.
from pcbc import *
Board(layers=2, stackup="jlcpcb_2l_1oz")
P = Power("PWR")
G = Ground("GND")
S = Net("S")
W = Net("W")
Resistor("R1", "0R", package="1206", mpn="X", lcsc="C1", p1=P, p2=G)
Resistor("R2", "0R", package="1206", mpn="X", lcsc="C1", p1=P, p2=G)
Resistor("R3", "0R", package="1206", mpn="X", lcsc="C1", p1=P, p2=G)
Resistor("R4", "10k", package="0402", mpn="X", lcsc="C1", p1=P, p2=G)
Resistor("R5", "10k", package="0402", mpn="X", lcsc="C1", p1=P, p2=S)
Resistor("R6", "10k", package="0402", mpn="X", lcsc="C1", p1=S, p2=G)
Resistor("RW1", "0R", package="0805", mpn="X", lcsc="C1", p1=W, p2=G)
Resistor("RW2", "0R", package="0805", mpn="X", lcsc="C1", p1=W, p2=G)
NetReq("PWR", kind="power", volts=5)
Path("PWR", "R1.1", "R2.1", "R3.1", amps=2)
Path("PWR", "R3.1", "R5.1", kind="feedback", length_mm=20)  # routes at 15.725 mm, over the kind's 15 mm default (PR 5 gate)

















