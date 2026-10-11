# Sensor node: USB-C in, 3.3 V LDO, ESP32-C3, BME280 on I2C, a thermistor
# buffered by an op-amp into an ADC pin, and a MOSFET switching a load.
# 33 parts - the hardest sheet so far. The AI writes the netlist; the tool
# draws the sheet (schematic.core.py locks what it should not move).
from pcbc import Board, Capacitor, Path, Ground, Led, Net, NetReq, Pair, Power, Resistor, Thermal, load

VBUS, V33, GND = Power("VBUS"), Power("3V3"), Ground("GND")
USB_DP, USB_DN, CC1, CC2 = Net("USB_DP"), Net("USB_DN"), Net("CC1"), Net("CC2")
EN, BOOT, LED, LED_A = Net("EN"), Net("BOOT"), Net("LED"), Net("LED_A")
SDA, SCL = Net("SDA"), Net("SCL")
T_DIV, T_OUT = Net("T_DIV"), Net("T_OUT")
GATE, DRV, LOAD = Net("GATE"), Net("DRV"), Net("LOAD")

USBC = load("components/HRO/TYPE-C-31-M-12")
ESD = load("components/ST/USBLC6-2SC6")
LDO = load("components/Diodes_Inc/AP2112K-3.3TRG1")
MCU = load("components/Espressif/ESP32-C3-MINI-1-N4")
SW = load("components/Omron/B3U-1000P")
BME = load("components/Bosch/BME280")
OPA = load("components/TI/LMV321IDBVR")
FET = load("components/AOS/AO3400A")
JST = load("components/JST/B2B-XH-A")

USBC("J1", GND=GND, VBUS=VBUS, CC1=CC1, CC2=CC2, DP1=USB_DP, DP2=USB_DP, DN1=USB_DN, DN2=USB_DN, EH=GND)
ESD("U3", GND=GND, VBUS=VBUS, **{"I/O1": USB_DP, "I/O2": USB_DN})
LDO("U2", VIN=VBUS, GND=GND, EN=VBUS, VOUT=V33)
MCU("U1", **{"3V3": V33}, GND=GND, EN=EN, IO9=BOOT, IO10=LED, IO18=USB_DN, IO19=USB_DP, IO0=T_OUT, IO1=DRV, IO4=SDA, IO5=SCL)
SW("SW_RST", **{"1": EN, "2": GND})
SW("SW_BOOT", **{"1": BOOT, "2": GND})
BME("U4", VDD=V33, VDDIO=V33, GND=GND, CSB=V33, SDO=GND, SDI=SDA, SCK=SCL)
OPA("U5", **{"+": T_DIV, "-": T_OUT, "OUT": T_OUT, "V+": V33, "GND": GND})
FET("Q1", G=GATE, D=LOAD, S=GND)
JST("J_LOAD", p1=V33, p2=LOAD)

r = dict(package="0402", manufacturer="UniOhm")
Resistor("R_CC1", "5.1k", mpn="0402WGF5101TCE", lcsc="C25905", p1=CC1, p2=GND, **r)
Resistor("R_CC2", "5.1k", mpn="0402WGF5101TCE", lcsc="C25905", p1=CC2, p2=GND, **r)
Resistor("R_EN", "10k", mpn="0402WGF1002TCE", lcsc="C25744", p1=V33, p2=EN, **r)
Resistor("R_BOOT", "10k", mpn="0402WGF1002TCE", lcsc="C25744", p1=V33, p2=BOOT, **r)
Resistor("R_LED", "1k", mpn="0402WGF1001TCE", lcsc="C11702", p1=LED, p2=LED_A, **r)
Resistor("R_SDA", "4.7k", mpn="0402WGF4701TCE", lcsc="C25900", p1=V33, p2=SDA, **r)
Resistor("R_SCL", "4.7k", mpn="0402WGF4701TCE", lcsc="C25900", p1=V33, p2=SCL, **r)
Resistor("R_NTC", "10k NTC", mpn="NCP15XH103F03RC", lcsc="C13564", manufacturer="Murata", package="0402", p1=V33, p2=T_DIV)
Resistor("R_TDIV", "10k", mpn="0402WGF1002TCE", lcsc="C25744", p1=T_DIV, p2=GND, **r)
Resistor("R_G", "100R", mpn="0402WGF1000TCE", lcsc="C25076", p1=DRV, p2=GATE, **r)
Resistor("R_PD", "100k", mpn="0402WGF1003TCE", lcsc="C25741", p1=GATE, p2=GND, **r)

c = dict(package="0402", manufacturer="Samsung")
Capacitor("C_VBUS", "10uF", package="0603", mpn="CL10A106KO8NQNC", lcsc="C962136", manufacturer="Samsung", p1=VBUS, p2=GND)
Capacitor("C_VBUS_HF", "100nF", mpn="CL05B104KO5NNNC", lcsc="C1525", p1=VBUS, p2=GND, **c)
Capacitor("C_3V3", "10uF", package="0603", mpn="CL10A106KO8NQNC", lcsc="C962136", manufacturer="Samsung", p1=V33, p2=GND)
Capacitor("C_3V3_HF", "100nF", mpn="CL05B104KO5NNNC", lcsc="C1525", p1=V33, p2=GND, **c)
Capacitor("C_MCU", "10uF", package="0603", mpn="CL10A106KO8NQNC", lcsc="C962136", manufacturer="Samsung", p1=V33, p2=GND)
Capacitor("C_MCU_HF", "100nF", mpn="CL05B104KO5NNNC", lcsc="C1525", p1=V33, p2=GND, **c)
Capacitor("C_EN", "1uF", mpn="CL05A105KA5NQNC", lcsc="C52923", p1=EN, p2=GND, **c)
Capacitor("C_BME", "100nF", mpn="CL05B104KO5NNNC", lcsc="C1525", p1=V33, p2=GND, **c)
Capacitor("C_OPA", "100nF", mpn="CL05B104KO5NNNC", lcsc="C1525", p1=V33, p2=GND, **c)
Led("D1", "red", package="0603", mpn="KT-0603R", lcsc="C72043", manufacturer="Kento", a=LED_A, k=GND)

Board(layers=4, stackup="jlcpcb_4l_1oz", planes=[("GND", "In1.Cu"), ("3V3", "In2.Cu")])

Pair("USB_DP", "USB_DN", kind="usb_hs", z_diff_ohm=90)
NetReq("VBUS", kind="power", volts=5)
NetReq("3V3", kind="power", volts=3.3)
NetReq("GND", kind="power")
Path("VBUS", "J1.VBUS", "U2.VIN", amps=1)
NetReq("T_DIV", "T_OUT", kind="analog")
NetReq("LOAD", volts=3.3)
Path("LOAD", "J_LOAD.2", "Q1.D", amps=1)
Path("USB_DP", "J1.B6", "U3.6", "U1.27")  # the pair's feed order: connector, ESD, module; a third pad off the line is a stub
Path("USB_DN", "J1.A7", "U3.4", "U1.26")
# The module's exposed pad is nine `pad_prop_heatsink` blocks on GND; 0.35 W is the C3's own
# figure for a Wi-Fi duty cycle this board runs. `across_planes=True` consents to the twelve
# antipads a through array cuts in the 3V3 plane on In2.Cu, which the derived pitch keeps a
# web between: see `patterns.stitch.thermal_pitch`.
Thermal("U1.49", watts=0.35, across_planes=True)
# The load's 1 A on the 3V3 pour (In2.Cu): the pour is the copper between the stations, and each station's joint
# to it (the LDO's tap vias, the connector's own barrel) is sized for its draw (physics-subjects PR 8).
Path("3V3", "U2.VOUT", "J_LOAD.1", amps=1)
