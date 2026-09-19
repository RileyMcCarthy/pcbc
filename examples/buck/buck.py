# 12 V in, 5 V / 2 A out: TPS54202 synchronous buck. The circuit is the
# netlist below; every pose on the sheet is a SchPlace() line, and the tool
# draws the wires, names and power symbols from the netlist.
from pcbc import Board, Capacitor, Ground, Net, NetReq, Place, Power, Resistor, SchPlace, SchRegion, load

VIN = Power("VIN")
V5 = Power("5V")
GND = Ground("GND")
SW = Net("SW")
FB = Net("FB")
BOOT = Net("BOOT")
EN = Net("EN")

U1 = load("components/TI/TPS54202DDCR")
L1 = load("components/Changjiang/FXL0630-150-M")
JST = load("components/JST/B2B-XH-A")

U1("U1", VIN=VIN, GND=GND, SW=SW, FB=FB, EN=EN, BOOT=BOOT)
L1("L1", p1=SW, p2=V5)
JST("J_IN", p1=VIN, p2=GND)
JST("J_OUT", p1=V5, p2=GND)

cap = dict(package="0805", mpn="CL21A226MAQNNNE", lcsc="C45783", manufacturer="Samsung")
Capacitor("C_IN1", "22uF", p1=VIN, p2=GND, **cap)
Capacitor("C_IN2", "22uF", p1=VIN, p2=GND, **cap)
Capacitor("C_OUT1", "22uF", p1=V5, p2=GND, **cap)
Capacitor("C_OUT2", "22uF", p1=V5, p2=GND, **cap)
Capacitor("C_BOOT", "100nF", package="0402", mpn="CL05B104KO5NNNC", lcsc="C1525", manufacturer="Samsung", p1=BOOT, p2=SW)

res = dict(package="0402", manufacturer="UniOhm")
Resistor("R_FB_TOP", "100k", mpn="0402WGF1003TCE", lcsc="C25741", p1=V5, p2=FB, **res)
Resistor("R_FB_BOT", "13.3k", mpn="0402WGF1332TCE", lcsc="C25879", p1=FB, p2=GND, **res)
Resistor("R_EN", "100k", mpn="0402WGF1003TCE", lcsc="C25741", p1=VIN, p2=EN, **res)

# PCB: 40 x 25 mm, 2 layers. Input on the left edge, output on the right.
Board(width=40, height=25, layers=2, stackup="jlcpcb_2l_1oz")
Place("U1", position="absolute", left=15, top=8, locked=True, reason="buck IC in the middle")
Place("L1", position="absolute", left=24, top=6, locked=True, reason="inductor on SW, right of the IC")
Place("J_IN", position="absolute", left=1, top=9, rotate=90, locked=True, reason="input on the left edge")
Place("J_OUT", position="absolute", right=1, top=9, rotate=270, locked=True, reason="output on the right edge")
Place("C_IN1", position="absolute", left=9, top=5, locked=True, reason="input caps at VIN")
Place("C_IN2", position="absolute", left=9, top=12, locked=True, reason="input caps at VIN")
Place("C_OUT1", position="absolute", left=32, top=5, locked=True, reason="output caps at the inductor")
Place("C_OUT2", position="absolute", left=32, top=12, locked=True, reason="output caps at the inductor")
Place("C_BOOT", position="absolute", left=15, top=15, locked=True, reason="bootstrap cap by the IC")
Place("R_FB_TOP", position="absolute", left=21, top=17, locked=True, reason="feedback divider near FB")
Place("R_FB_BOT", position="absolute", left=21, top=20, locked=True, reason="feedback divider near FB")
Place("R_EN", position="absolute", left=15, top=2, locked=True, reason="EN pull-up by the IC")

# Schematic. This symbol has SW and VIN both on the left, so the output rail
# runs left from the inductor and the input stage sits under the IC.
SchRegion("sheet", left=12, top=12, width=380, height=200)
SchPlace("U1", parent="sheet", left=150, top=60)
SchPlace("L1", to="U1.SW")
SchPlace("C_OUT1", to="L1.2")
SchPlace("C_OUT2", along="C_OUT1.1", side="bottom")
SchPlace("J_OUT", to="C_OUT2.1", side="left")  # past the caps, at the end of the rail
SchPlace("C_IN1", to="U1.VIN", side="bottom")
SchPlace("C_IN2", along="C_IN1.1", side="left")
SchPlace("J_IN", to="C_IN2.1", side="left")
SchPlace("C_BOOT", to="U1.BOOT")
SchPlace("R_EN", to="U1.EN")
SchPlace("R_FB_TOP", to="U1.FB")
SchPlace("R_FB_BOT", along="R_FB_TOP.2", side="bottom")

NetReq("VIN", "5V", "GND", kind="power", volts=12, amps=2)
NetReq("SW", kind="switch_node", max_mm=6)
NetReq("FB", kind="analog")
