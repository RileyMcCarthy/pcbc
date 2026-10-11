# 12 V in, 5 V / 2 A out: TPS54202 synchronous buck. The circuit is the
# netlist below; every pose on the sheet is a SchPlace() line, and the tool
# draws the wires, names and power symbols from the netlist.
from pcbc import Board, Capacitor, Ground, Net, NetReq, Power, Resistor, SchPlace, SchRegion, load

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
Board(layers=2, stackup="jlcpcb_2l_1oz")


# Schematic. This symbol has SW and VIN both on the left, so the output rail
# runs left from the inductor and the input stage sits under the IC.














NetReq("VIN", kind="power", volts=12); NetReq("5V", kind="power", volts=5); NetReq("GND", kind="power")  # no current: docs/physics-subjects.md O2 (b)
NetReq("SW", kind="switch_node", max_mm=6)
NetReq("FB", kind="analog")

# Copper. Input on the west edge, output on the east; the regulator in the middle and
# every passive says which pin it serves. `pcbc pcb buck.py` lists what to move.
