"""USB-C 5 V → AP2112 3.3 V → ESP32-C3-MINI-1-N4.

Pin kwargs are the names on each part's .kicad_sym (3V3, I/O1, …).
"""

from pcbc import (
    Board,
    Capacitor,
    Path,
    Ground,
    Led,
    Net,
    NetReq, Pair,
    Power,
    Resistor,
    Thermal,
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

Board(layers=2, stackup="jlcpcb_2l_1oz")

Pair("USB_DP", "USB_DN", kind="usb_hs", z_diff_ohm=90)
NetReq("VBUS", kind="power", volts=5)
NetReq("3V3", kind="power", volts=3.3)
NetReq("GND", kind="power")
Path("VBUS", "J1.VBUS", "U2.VIN", amps=0.5)
Path("3V3", "U2.VOUT", "U1.3V3", amps=0.5)
NetReq("EN", "BOOT", "LED", "LED_A", "CC1", "CC2", kind="digital")
# The pair's feed order: connector, ESD, module. Three pads on a high-speed net need the order
# said, else the third is a stub (`pcbc pcb` asks for these lines).
Path("USB_DP", "J1.A6", "U3.1", "U1.27")
Path("USB_DN", "J1.A7", "U3.3", "U1.26")
# The module's exposed pad is nine `pad_prop_heatsink` blocks on GND. Two layers and one pour, so
# there is no foreign plane to consent to and the pitch is the fab's hole-to-hole alone.
Thermal("U1.49", watts=0.35)
