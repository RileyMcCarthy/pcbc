"""USB-C 5 V → AP2112 3.3 V → ESP32-C3-MINI-1-N4.

Pin kwargs are the names on each part's .kicad_sym (3V3, I/O1, …).
"""

from pcbc import (
    Board,
    Capacitor,
    Chain,
    Ground,
    Keepout,
    Led,
    Net,
    NetReq,
    Place,
    Power,
    Resistor,
    SchPlace,
    SchRegion,
    load,
)

USBC = load("./components/HRO/TYPE-C-31-M-12")
LDO = load("./components/Diodes_Inc/AP2112K-3.3TRG1")
ESP32 = load("./components/Espressif/ESP32-C3-MINI-1-N4")
ESD = load("./components/ST/USBLC6-2SC6")
Button = load("./components/Omron/B3U-1000P")

VBUS = Power("VBUS")
V3V3 = Power("3V3")
GND = Ground("GND")
USB_DP = Net("USB_DP")
USB_DN = Net("USB_DN")
EN = Net("EN")
BOOT = Net("BOOT")
LED = Net("LED")
CC1 = Net("CC1")
CC2 = Net("CC2")
LED_A = Net("LED_A")

USBC(
    "J1",
    VBUS=VBUS,
    GND=GND,
    EH=GND,
    CC1=CC1,
    CC2=CC2,
    DP1=USB_DP,
    DP2=USB_DP,
    DN1=USB_DN,
    DN2=USB_DN,
)
ESD("U3", **{"I/O1": USB_DP, "I/O2": USB_DN, "GND": GND, "VBUS": VBUS})

Resistor(
    "R_CC1",
    "5.1k",
    package="0402",
    mpn="0402WGF5101TCE",
    lcsc="C25905",
    manufacturer="UniOhm",
    p1=CC1,
    p2=GND,
)
Resistor(
    "R_CC2",
    "5.1k",
    package="0402",
    mpn="0402WGF5101TCE",
    lcsc="C25905",
    manufacturer="UniOhm",
    p1=CC2,
    p2=GND,
)
Capacitor(
    "C_VBUS",
    "10uF",
    package="0603",
    mpn="CL10A106KO8NQNC",
    lcsc="C962136",
    manufacturer="Samsung",
    p1=VBUS,
    p2=GND,
)
Capacitor(
    "C_VBUS_HF",
    "100nF",
    package="0402",
    mpn="CL05B104KO5NNNC",
    lcsc="C1525",
    manufacturer="Samsung",
    p1=VBUS,
    p2=GND,
)

LDO("U2", VIN=VBUS, VOUT=V3V3, GND=GND, EN=VBUS)
Capacitor(
    "C_3V3",
    "10uF",
    package="0603",
    mpn="CL10A106KP8NNNC",
    lcsc="C19702",
    manufacturer="Samsung",
    p1=V3V3,
    p2=GND,
)
Capacitor(
    "C_3V3_HF",
    "100nF",
    package="0402",
    mpn="CL05B104KO5NNNC",
    lcsc="C1525",
    manufacturer="Samsung",
    p1=V3V3,
    p2=GND,
)

Resistor(
    "R_EN",
    "10k",
    package="0402",
    mpn="0402WGF1002TCE",
    lcsc="C25744",
    manufacturer="UniOhm",
    p1=V3V3,
    p2=EN,
)
Capacitor(
    "C_EN",
    "1uF",
    package="0402",
    mpn="CL05A105KA5NQNC",
    lcsc="C52923",
    manufacturer="Samsung",
    p1=EN,
    p2=GND,
)
Button("SW_RST", p1=EN, p2=GND)

Resistor(
    "R_BOOT",
    "10k",
    package="0402",
    mpn="0402WGF1002TCE",
    lcsc="C25744",
    manufacturer="UniOhm",
    p1=V3V3,
    p2=BOOT,
)
Button("SW_BOOT", p1=BOOT, p2=GND)

ESP32(
    "U1",
    **{
        "3V3": V3V3,
        "GND": GND,
        "EN": EN,
        "IO9": BOOT,
        "IO10": LED,
        "IO18": USB_DN,
        "IO19": USB_DP,
    },
)
Capacitor(
    "C_MCU",
    "10uF",
    package="0603",
    mpn="CL10A106KP8NNNC",
    lcsc="C19702",
    manufacturer="Samsung",
    p1=V3V3,
    p2=GND,
)
Capacitor(
    "C_MCU_HF",
    "100nF",
    package="0402",
    mpn="CL05B104KO5NNNC",
    lcsc="C1525",
    manufacturer="Samsung",
    p1=V3V3,
    p2=GND,
)

Resistor(
    "R_LED",
    "1k",
    package="0402",
    mpn="0402WGF1001TCE",
    lcsc="C11702",
    manufacturer="UniOhm",
    p1=LED,
    p2=LED_A,
)
Led(
    "D1",
    "red",
    package="0603",
    mpn="KT-0603R",
    lcsc="C2286",
    manufacturer="Kento",
    a=LED_A,
    k=GND,
)

Board(width=40, height=30, layers=2, stackup="jlcpcb_2l_1oz")

# Copper: two anchors say where the board's shape is decided (the connector on its
# edge, the module with its antenna at the west edge); everything else says what it
# belongs to and the tool puts it there. `pcbc pcb c3_usb.py` lists what to move.
Place("J1", edge="bottom", reason="USB-C at south edge")
Place("U1", position="absolute", left=1, top=1, rotate=90, reason="ESP32-C3-MINI antenna at west")
Keepout("ANTENNA", position="absolute", left=0, top=0, width=4, height=15)
Place("U2", position="absolute", right=4, top=10, reason="LDO")
Place("U3", to="J1.DP1", reason="USB ESD at the connector")
Place("R_CC1", to="J1.CC1")
Place("R_CC2", to="J1.CC2")
Place("C_VBUS_HF", to="U2.VIN")  # the small cap first: the first Place() on a pin sits nearest
Place("C_VBUS", to="U2.VIN")
Place("C_3V3_HF", to="U2.VOUT")
Place("C_3V3", to="U2.VOUT")
Place("C_MCU_HF", to="U1.3V3")  # decoupling before the switches: the first Place() written sits nearest
Place("C_MCU", to="U1.3V3")
Place("R_EN", to="U1.EN")
Place("C_EN", to="U1.EN")
Place("SW_RST", to="U1.EN")
Place("R_BOOT", to="U1.IO9")
Place("SW_BOOT", to="U1.IO9")
Place("R_LED", to="U1.IO10")
Place("D1", to="R_LED.2")

# Schematic. CSS parks the multi-pin symbols; everything else hangs off a pin:
# to="U1.EN" puts the pin that shares that net in line with it, along= stacks
# beside a sibling, side= says which way. The tool picks the distance (room
# for the net's label) and draws wires, labels and power symbols from the
# netlist - nothing here can change what connects.
SchRegion("usb", left=12, top=10, width=180, height=115)
SchPlace("J1", parent="usb", left=90, top=12)
SchPlace("R_CC1", to="J1.CC1")
SchPlace("R_CC2", to="J1.CC2")
SchPlace("U3", to="J1.DP1", side="bottom", gap=15.24)

SchRegion("pwr", left=210, top=10, width=155, height=115)
SchPlace("U2", parent="pwr", left=55, top=28)
SchPlace("C_VBUS", to="U2.VIN")
SchPlace("C_VBUS_HF", along="C_VBUS.1", side="bottom")
SchPlace("C_3V3", to="U2.VOUT")
SchPlace("C_3V3_HF", along="C_3V3.1", side="bottom")

SchRegion("mcu", left=12, top=140, width=380, height=145)
SchPlace("U1", parent="mcu", left=185, top=10)
SchPlace("C_MCU", to="U1.3V3")
SchPlace("C_MCU_HF", along="C_MCU.1", side="bottom")  # under the 3V3 node: no extra symbol
SchPlace("R_EN", to="U1.EN")
SchPlace("C_EN", along="R_EN.2", side="bottom")
SchPlace("SW_RST", along="C_EN.1", side="left")
SchPlace("R_BOOT", to="U1.IO9")
SchPlace("SW_BOOT", along="R_BOOT.2", side="bottom")
SchPlace("R_LED", to="U1.IO10")
SchPlace("D1", to="R_LED.2")

NetReq("USB_DP", "USB_DN", kind="usb_hs", z_diff_ohm=90, pair=True)
NetReq("VBUS", "3V3", "GND", kind="power", volts=3.3, amps=0.5)
NetReq("EN", "BOOT", "LED", "LED_A", "CC1", "CC2", kind="digital")
# The pair's feed order: connector, ESD, module. Three pads on a high-speed net need the order
# said, else the third is a stub (`pcbc pcb` asks for these lines).
Chain("USB_DP", "J1.A6", "U3.1", "U1.27")
Chain("USB_DN", "J1.A7", "U3.3", "U1.26")
