# node: the schematic's locks (docs/schematic-layout.md: schematic layout is schematic.core.py's).
# The tool lays the sheet out from the netlist; these lines name its six groups. Everything else is solved.
SchRegion("usb", title="USB", members=("J1", "U3", "R_CC1", "R_CC2"))
SchRegion("pwr", title="3.3 V", members=("U2", "C_VBUS", "C_VBUS_HF", "C_3V3", "C_3V3_HF"))
SchRegion("mcu", title="MCU", members=("U1", "C_MCU", "C_MCU_HF", "R_EN", "C_EN", "SW_RST", "R_BOOT", "SW_BOOT", "R_LED", "D1"))
SchRegion("sensor", title="Sensor", members=("U4", "C_BME", "R_SDA", "R_SCL"))
SchRegion("analog", title="Analog", members=("U5", "C_OPA", "R_NTC", "R_TDIV"))
SchRegion("load", title="Load", members=("R_G", "R_PD", "Q1", "J_LOAD"))
# The sensor's four left pins need three symbols and this cap: it sits out at 15.24 so the symbols have room.
SchPlace("C_BME", to="U4.VDD", gap=15.24)
# The MCU's decoupling hangs off its 3V3 pin, the HF cap under the same node: no symbol of its own.
SchPlace("C_MCU", to="U1.3V3")
SchPlace("C_MCU_HF", along="C_MCU.1", side="bottom")
# The boot pull-up hangs straight off IO9: one wire joins IO9, R_BOOT and SW_BOOT. It is what the auto-layout writes
# for R_BOOT, so it changes nothing today and keeps the relation if the auto-layout changes (docs/schematic-layout.md §3.3).
SchPlace("R_BOOT", to="U1.IO9")
