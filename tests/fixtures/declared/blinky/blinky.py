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
    lcsc="C72043",
    manufacturer="Kento",
    a=LED,
    k=GND,
)

Board(layers=2, stackup="jlcpcb_2l_1oz")

NetReq("VCC", kind="power", volts=3.3)
NetReq("GND", kind="power")
NetReq("LED", kind="digital")
