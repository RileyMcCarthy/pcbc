from pcbc import Board, Ground, Led, Net, NetReq, Place, Power, Resistor

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
    lcsc="C72043",
    manufacturer="Kento",
    a=LED,
    k=GND,
)

Board(width=20, height=10, layers=2, stackup="jlcpcb_2l_1oz")
Place(
    "D1",
    position="absolute",
    right=2,
    top=0,
    bottom=0,
    margin_top="auto",
    margin_bottom="auto",
    locked=True,
    reason="LED at east edge",
)
NetReq("VCC", "GND", kind="power", volts=3.3, amps=0.05)
