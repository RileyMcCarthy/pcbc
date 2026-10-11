from pcbc import Board, Ground, Led, Net, NetReq, Power, Resistor

VCC = Power("VCC")
GND = Ground("GND")
LED = Net("LED")

Resistor(
    "R1",
    "1k",
    package="0402",
    mpn="0402WGF1001TCE",
    lcsc="C21190",
    manufacturer="UniOhm",
    p1=VCC,
    p2=LED,
)
Led(
    "D1",
    "red",
    package="0603",
    mpn="KT-0603R",
    lcsc="C2286",  # KT-0603R, Kento red 0603 (C72043 is an Everlight 19-217/GHC: docs/spice-stage.md survey 4)
    manufacturer="Kento",
    a=LED,
    k=GND,
)

Board(layers=2, stackup="jlcpcb_2l_1oz")

NetReq("VCC", kind="power", volts=3.3)
NetReq("GND", kind="power")
NetReq("LED", kind="digital")
