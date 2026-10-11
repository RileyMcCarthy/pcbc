# buck: the outline and where each part goes (docs/direction.md §2: placement is layout.core.py's).
Outline(width=40, height=25)
Place("U1", position="absolute", left=15, top=8, locked=True, reason="buck IC in the middle")
# Copper. Input on the west edge, output on the east; the regulator in the middle and
# every passive says which pin it serves. `pcbc pcb buck.py` lists what to move.
Place("J_IN", edge="left", reason="input connector")
Place("J_OUT", edge="right", reason="output connector")
Place("C_IN1", to="U1.VIN")
Place("C_IN2", to="U1.VIN")
Place("L1", to="U1.SW")
Place("C_OUT1", to="L1.2")
Place("C_OUT2", to="L1.2")
Place("C_BOOT", to="U1.BOOT")
Place("R_EN", to="U1.EN")
Place("R_FB_TOP", to="U1.FB")
Place("R_FB_BOT", to="U1.FB")
