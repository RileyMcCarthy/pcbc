from pcbc import Component

# N-channel MOSFET, SOT-23. Gate left, drain up, source down.
part = Component(name="AO3400A", prefix="Q", mpn="AO3400A", manufacturer="AOS", lcsc="C20917",
                 footprint="SOT-23-3_L2.9-W1.3-P1.90-LS2.4-BR.kicad_mod", symbol="AO3400A.kicad_sym", body_mm=(2.9, 1.3))
