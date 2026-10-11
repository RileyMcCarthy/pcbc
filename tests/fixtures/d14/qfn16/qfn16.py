# Postfix B13 fixture (refutation neck r2, qfn16_3a_solo): QFN16 pin 10 feeds R2 through a 3 A Path; every other pin on GND; R3 an off-path load.
from pcbc import *
Board(layers=2, stackup="jlcpcb_2l_1oz")
P = Power("PWR")
G = Ground("GND")
N1 = Net("N1")
N2 = Net("N2")
N3 = Net("N3")
N4 = Net("N4")
N5 = Net("N5")
N6 = Net("N6")
N7 = Net("N7")
N8 = Net("N8")
N9 = Net("N9")
N11 = Net("N11")
N12 = Net("N12")
N13 = Net("N13")
N14 = Net("N14")
N15 = Net("N15")
N16 = Net("N16")
N17 = Net("N17")
U = load("components/X/QFN16")
U("U1", P1=N1, P2=N2, P3=N3, P4=N4, P5=N5, P6=N6, P7=N7, P8=N8, P9=N9, P10=(P), P11=N11, P12=N12, P13=N13, P14=N14, P15=N15, P16=N16, P17=N17)
Resistor("R2", "0R", package="1206", mpn="X", lcsc="C1", p1=P, p2=G)
Resistor("R3", "10k", package="0402", mpn="X", lcsc="C1", p1=P, p2=G)
NetReq("PWR", kind="power", volts=5)
Path("PWR", "U1.P10", "R2.1", amps=3)







