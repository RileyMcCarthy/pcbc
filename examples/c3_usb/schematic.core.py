# c3_usb: the schematic's locks (docs/schematic-layout.md: schematic layout is schematic.core.py's).
# The tool lays the sheet out from the netlist; these lines name its three groups. Everything else is solved.
SchRegion("usb", title="USB", members=("J1", "U3", "R_CC1", "R_CC2"))
SchRegion("pwr", title="3.3 V", members=("U2", "C_VBUS", "C_VBUS_HF", "C_3V3", "C_3V3_HF"))
SchRegion("mcu", title="MCU", members=("U1", "C_MCU", "C_MCU_HF", "R_EN", "C_EN", "SW_RST", "R_BOOT", "SW_BOOT", "R_LED", "D1"))
# The MCU's decoupling hangs off its 3V3 pin, the HF cap under the same node: no symbol of its own.
SchPlace("C_MCU", to="U1.3V3")
SchPlace("C_MCU_HF", along="C_MCU.1", side="bottom")
