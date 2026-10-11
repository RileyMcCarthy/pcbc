from pcbc import Component

# Light Emitting Diodes (LED). Fetched from LCSC C2286 by pcbc fetch; library quality usable (85/100).
# Pins: A(1) K(2)
part = Component(
    name="KT-0603R",
    prefix="D",
    mpn="KT-0603R",
    manufacturer="KENTO",
    lcsc="C2286",
    footprint="LED-SMD_L1.6-W0.8-R-RD.kicad_mod",
    symbol="KT-0603R.kicad_sym",
    body_mm=(1.60, 0.80),
)
