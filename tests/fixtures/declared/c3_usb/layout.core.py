# c3_usb: the outline and where each part goes (docs/direction.md §2: placement is layout.core.py's).
Outline(width=40, height=30)
# Copper: two anchors say where the board's shape is decided (the connector on its
# edge, the module with its antenna at the west edge); everything else says what it
# belongs to and the tool puts it there. `pcbc pcb c3_usb.py` lists what to move.
Place("J1", edge="bottom", reason="USB-C at south edge")
Place("U1", position="absolute", left=1, top=1, rot=90, reason="ESP32-C3-MINI antenna at west")
Keepout("ANTENNA", position="absolute", left=0, top=0, width=4, height=15)
Place("U2", position="absolute", right=4, top=10, reason="LDO")
Place("U3", to="J1.DP1", reason="USB ESD at the connector")
Place("R_CC1", to="J1.CC1")
Place("R_CC2", to="J1.CC2")
Place("C_VBUS_HF", to="U2.VIN")  # the small cap first: the first Place() on a pin sits nearest
Place("C_VBUS", to="U2.VIN")
Place("C_3V3_HF", to="U2.VOUT")
Place("C_3V3", to="U2.VOUT")
Place("C_MCU_HF", to="U1.3V3")  # decoupling before the switches: the first Place() written sits nearest
Place("C_MCU", to="U1.3V3")
Place("R_EN", to="U1.EN")
Place("C_EN", to="U1.EN")
Place("SW_RST", to="U1.EN")
Place("R_BOOT", to="U1.IO9")
Place("SW_BOOT", to="U1.IO9")
Place("R_LED", to="U1.IO10")
Place("D1", to="R_LED.2")
