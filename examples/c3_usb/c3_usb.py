"""USB-C 5 V → AP2112 3.3 V → ESP32-C3-MINI-1-N4.

Pin kwargs are the names on each part's .kicad_sym (3V3, I/O1, …).
"""

from pcbc import (
    Board,
    Capacitor,
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

Place(
    "J1",
    position="absolute",
    left=0,
    right=0,
    bottom=0,
    margin_left="auto",
    margin_right="auto",
    locked=True,
    reason="USB-C at south edge",
)
Place(
    "U1",
    position="absolute",
    left=1,
    top=1,
    rotate=90,
    locked=True,
    reason="ESP32-C3-MINI antenna at west",
)
Keepout("ANTENNA", position="absolute", left=0, top=0, width=4, height=15)

Place("U2", position="absolute", right=4, top=10, reason="LDO")
Place("U3", position="absolute", left=18, bottom=10, reason="USB ESD")
Place("SW_RST", position="absolute", right=4, bottom=12, reason="reset")
Place("SW_BOOT", position="absolute", right=9, bottom=12, reason="boot")
Place("D1", position="absolute", right=3, top=3, reason="status LED")
Place("R_LED", position="absolute", right=7, top=3)
Place("R_CC1", position="absolute", left=18, bottom=6)
Place("R_CC2", position="absolute", right=10, bottom=6)
Place("C_VBUS", position="absolute", left=22, bottom=10)
Place("C_VBUS_HF", position="absolute", left=26, bottom=10)
Place("C_3V3", position="absolute", right=8, top=10)
Place("C_3V3_HF", position="absolute", right=12, top=10)
Place("R_EN", position="absolute", right=8, bottom=16)
Place("C_EN", position="absolute", right=12, bottom=16)
Place("R_BOOT", position="absolute", left=18, top=12)
Place("C_MCU", position="absolute", left=18, top=8)
Place("C_MCU_HF", position="absolute", left=22, top=8)

SchRegion("usb", left=15, top=15, width=55, height=55)
SchPlace("J1", parent="usb", left=0, top=8)
SchPlace("U3", pin="I/O1", to="J1.DP1", gap=7.62)
SchPlace("R_CC1", pin="1", to="J1.CC1", gap=5.08)
SchPlace("R_CC2", pin="1", to="J1.CC2", gap=5.08)

SchRegion("pwr", left=80, top=15, width=45, height=45)
SchPlace("U2", parent="pwr", left=0, top=12)
SchPlace("C_VBUS", pin="1", to="U2.VIN", gap=5.08)
SchPlace("C_VBUS_HF", pin="1", along="C_VBUS.1", gap=5.08)
SchPlace("C_3V3", pin="1", to="U2.VOUT", gap=5.08)
SchPlace("C_3V3_HF", pin="1", along="C_3V3.1", gap=5.08)

SchRegion("mcu", left=15, top=85, width=110, height=90)
SchPlace("U1", parent="mcu", left=45, top=5)
SchPlace("C_MCU", pin="1", to="U1.3V3", gap=5.08)
SchPlace("C_MCU_HF", pin="1", along="C_MCU.1", gap=5.08)
SchPlace("R_EN", pin="2", to="U1.EN", gap=5.08)
SchPlace("C_EN", pin="1", along="R_EN.2", gap=5.08)
SchPlace("SW_RST", pin="1", along="C_EN.1", gap=7.62)
SchPlace("R_BOOT", pin="2", to="U1.IO9", gap=5.08)
SchPlace("SW_BOOT", pin="1", along="R_BOOT.2", gap=7.62)
SchPlace("R_LED", pin="1", to="U1.IO10", gap=5.08)
SchPlace("D1", pin="A", to="R_LED.2", gap=2.54)

NetReq("USB_DP", "USB_DN", kind="usb_hs", z_diff_ohm=90, pair=True)
NetReq("VBUS", "3V3", "GND", kind="power", volts=3.3, amps=0.5)
NetReq("EN", "BOOT", "LED", "LED_A", "CC1", "CC2", kind="digital")
