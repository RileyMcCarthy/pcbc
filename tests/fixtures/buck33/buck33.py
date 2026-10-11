# 12 V in, 3.3 V / 1.5 A out: TPS54202 buck with a divider feedback tap. Refutation round 1 (defects 6 and 7): the FB NetReq keeps 3 mm from SW, U1 carries both.
from pcbc import Board, Capacitor, Ground, Loop, Net, NetReq, Path, Power, Resistor, SchPlace, SchRegion, load

VIN = Power("VIN")
V33 = Power("3V3")
GND = Ground("GND")
SW = Net("SW")
FB = Net("FB")
BOOT = Net("BOOT")
EN = Net("EN")

U1 = load("components/TI/TPS54202DDCR")
L1 = load("components/Changjiang/FXL0630-150-M")
JST = load("components/JST/B2B-XH-A")

U1("U1", VIN=VIN, GND=GND, SW=SW, FB=FB, EN=EN, BOOT=BOOT)
L1("L1", p1=SW, p2=V33)
JST("J_IN", p1=VIN, p2=GND)
JST("J_OUT", p1=V33, p2=GND)

cap = dict(package="0805", mpn="CL21A226MAQNNNE", lcsc="C45783", manufacturer="Samsung")
Capacitor("C_IN", "22uF", p1=VIN, p2=GND, **cap)
Capacitor("C_OUT", "22uF", p1=V33, p2=GND, **cap)
Capacitor("C_BOOT", "100nF", package="0402", mpn="CL05B104KO5NNNC", lcsc="C1525", manufacturer="Samsung", p1=BOOT, p2=SW)

res = dict(package="0402", manufacturer="UniOhm")
Resistor("R_TOP", "33k", mpn="0402WGF3302TCE", lcsc="C25779", p1=V33, p2=FB, **res)
Resistor("R_BOT", "10k", mpn="0402WGF1002TCE", lcsc="C25744", p1=FB, p2=GND, **res)
Resistor("R_EN", "100k", mpn="0402WGF1003TCE", lcsc="C25741", p1=VIN, p2=EN, **res)

Board(layers=2, stackup="jlcpcb_2l_1oz")

NetReq("VIN", kind="power", volts=12); NetReq("GND", kind="power")
NetReq("3V3", kind="power", volts=3.3)
NetReq("SW", kind="switch_node")
NetReq("FB", kind="analog", keep_clear_of="SW")
Path("SW", "U1.SW", "L1.1", amps=1.5)
Path("3V3", "L1.2", "C_OUT.1", "J_OUT.1", amps=1.5)
Path("3V3", "C_OUT.1", "R_TOP.1", kind="feedback")
Loop("C_IN.1", "U1.VIN", "U1.GND", "C_IN.2")












