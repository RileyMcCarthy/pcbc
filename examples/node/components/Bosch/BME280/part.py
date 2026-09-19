from pcbc import Component

# Temperature / humidity / pressure, I2C. GND is on pads 1 and 7 (one name).
part = Component(name="BME280", prefix="U", mpn="BME280", manufacturer="Bosch", lcsc="C92489",
                 footprint="LGA-8_BME280_BL.kicad_mod", symbol="BME280.kicad_sym", body_mm=(2.5, 2.5))
