from pcbc import Component

# Names come from the .kicad_sym. Unused GPIOs may float — this is not a pin map.
part = Component(
    name="ESP32-C3-MINI-1-N4",
    prefix="U",
    mpn="ESP32-C3-MINI-1-N4",
    manufacturer="Espressif",
    lcsc="C2838502",
    footprint="ESP32-C6-MINI-1.kicad_mod",
    symbol="ESP32-C3-MINI-1-N4.kicad_sym",
    body_mm=(13.2, 16.6),
    optional_pins=(
        "IO0",
        "IO1",
        "IO2",
        "IO3",
        "IO4",
        "IO5",
        "IO6",
        "IO7",
        "IO8",
        "RXD0",
        "TXD0",
    ),
)
