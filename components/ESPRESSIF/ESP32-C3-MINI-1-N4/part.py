from pcbc import Component

# Wireless Modules. Fetched from LCSC C2838502 by pcbc fetch; library quality usable (87/100).
# Pins: GND(1,2,11,14,36,37,38,39,40,41,42,43,44,45,46,47,48,49,50,51,52,53) 3V3(3)
#       NC(4,7,9,10,15,17,24,25,28,29,32,33,34,35) IO2(5) IO3(6) EN(8) IO0(12) IO1(13) IO10(16)
#       IO4(18) IO5(19) IO6(20) IO7(21) IO8(22) IO9(23) IO18(26) IO19(27) RXD0(30) TXD0(31)
part = Component(
    name="ESP32-C3-MINI-1-N4",
    prefix="U",
    mpn="ESP32-C3-MINI-1-N4",
    manufacturer="ESPRESSIF",
    lcsc="C2838502",
    footprint="WIFIM-SMD_ESP32-C3-MINI-1.kicad_mod",
    symbol="ESP32-C3-MINI-1-N4.kicad_sym",
    body_mm=(13.20, 16.60),
)
