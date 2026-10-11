# Refutation round 2 (2026-10-01): the language refuters' 24 V -> 5 V buck, written from the README and skill
# only. As placed, the off-path SW branch C5.2 -> U1.2 is walled in by its own net's path copper (U1.SW -> L1.1).
# 24 V in, 5 V / 1.5 A out: TPS54202 buck for a sensor hub. Written from the README and skill only.
from pcbc import Board, Capacitor, Ground, Loop, Net, NetReq, Path, Power, Resistor, SchPlace, SchRegion, load

VIN = Power("VIN")
V5 = Power("V5")
GND = Ground("GND")
SW = Net("SW")
FB = Net("FB")
BOOT = Net("BOOT")
EN = Net("EN")

REG = load("components/TI/TPS54202DDCR")
IND = load("components/Changjiang/FXL0630-150-M")
JST = load("components/JST/B2B-XH-A")

REG("U1", VIN=VIN, GND=GND, SW=SW, FB=FB, EN=EN, BOOT=BOOT)
IND("L1", p1=SW, p2=V5)
JST("J1", p1=VIN, p2=GND)
JST("J2", p1=V5, p2=GND)

cap = dict(package="0805", mpn="CL21A226MAQNNNE", lcsc="C45783", manufacturer="Samsung")
Capacitor("C1", "22uF", p1=VIN, p2=GND, **cap)
Capacitor("C2", "22uF", p1=VIN, p2=GND, **cap)
Capacitor("C3", "22uF", p1=V5, p2=GND, **cap)
Capacitor("C4", "22uF", p1=V5, p2=GND, **cap)
Capacitor("C5", "100nF", package="0402", mpn="CL05B104KO5NNNC", lcsc="C1525", manufacturer="Samsung", p1=BOOT, p2=SW)

res = dict(package="0402", manufacturer="UniOhm")
Resistor("R1", "100k", mpn="0402WGF1003TCE", lcsc="C25741", p1=V5, p2=FB, **res)
Resistor("R2", "13.3k", mpn="0402WGF1332TCE", lcsc="C25879", p1=FB, p2=GND, **res)
Resistor("R3", "100k", mpn="0402WGF1003TCE", lcsc="C25741", p1=VIN, p2=EN, **res)

Board(layers=2, stackup="jlcpcb_2l_1oz")

NetReq("VIN", kind="power", volts=24); NetReq("GND", kind="power")
NetReq("V5", kind="power", volts=5)
NetReq("SW", kind="switch_node")
NetReq("FB", kind="analog")
# switch current, regulator to inductor
Path("SW", "U1.SW", "L1.1", amps=1.5)
# load current: inductor, first output cap, connector
Path("V5", "L1.2", "C3.1", "J2.1", amps=1.5)
# divider top leaves the rail at the output cap
Path("V5", "C3.1", "R1.1", kind="feedback")
# input hot loop
Loop("C1.1", "U1.VIN", "U1.GND", "C1.2")














