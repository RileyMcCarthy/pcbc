# 12 V in, 5 V / 2 A out: TPS54202 synchronous buck. The circuit is the
# netlist below; the tool draws the sheet from the netlist (schematic.core.py
# locks what it should not move).
from pcbc import Board, Capacitor, Ground, Loop, Net, NetReq, Path, Power, Resistor, load

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

# PCB: 2 layers, the outline derived from the placement. Input on the left edge, output on the right.
Board(layers=2, stackup="jlcpcb_2l_1oz")
# Where each part goes: layout.core.py, next to this file.

# Currents at 12 V -> 5 V / 2 A, TI TPS54202 (SLVSD26C Table 4-1 pin roles; L1 15 uH; fsw 500 kHz; dI = 7 D / (L fsw)): VIN 1.36 A rms = sqrt(D (Iout^2 + dI^2/12)) at D = 0.463 (~90 % efficient, 0.926 A in); GND 1.53 A rms = sqrt((1 - D) (Iout^2 + dI^2/12)) at the ideal D = 5/12; SW and the 5V load 2 A, Iout.
NetReq("VIN", kind="power", volts=12)
NetReq("GND", kind="power")
NetReq("5V", kind="power", volts=5)
NetReq("SW", kind="switch_node", max_mm=6)
NetReq("FB", kind="analog")
# The switch current: the regulator's SW pin to the inductor. The boot cap's branch stays thin.
Path("SW", "U1.SW", "L1.1", amps=2)
# The load current: the inductor's output, through the first output cap, to the connector.
Path("5V", "L1.2", "C_OUT1.1", "J_OUT.1", amps=2)
# The divider's top: leaves the rail inside the output cap's pad and touches 5V nowhere else.
Path("5V", "C_OUT1.1", "R_FB_TOP.1", kind="feedback")
# The input hot loop, checked at placement.
Loop("C_IN1.1", "U1.VIN", "U1.GND", "C_IN1.2")
Path("VIN", "J_IN.1", "C_IN2.1", "C_IN1.1", "U1.VIN", amps=1.36)  # the input current, through both input caps (their ripple)
