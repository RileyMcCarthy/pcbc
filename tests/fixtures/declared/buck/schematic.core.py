# buck: the schematic's locks (docs/schematic-layout.md: schematic layout is schematic.core.py's).
SchRegion("sheet", title="Buck", members=("U1", "L1", "J_IN", "J_OUT", "C_IN1", "C_IN2", "C_OUT1", "C_OUT2", "C_BOOT", "R_FB_TOP", "R_FB_BOT", "R_EN"))
SchPlace("U1")
SchPlace("L1", to="U1.SW")
SchPlace("C_OUT1", to="L1.2")
SchPlace("C_OUT2", along="C_OUT1.1", side="bottom")
SchPlace("J_OUT", to="C_OUT2.1", side="left")  # past the caps, at the end of the rail
SchPlace("C_IN1", to="U1.VIN", side="bottom")
SchPlace("C_IN2", along="C_IN1.1", side="left")
SchPlace("J_IN", to="C_IN2.1", side="left")
SchPlace("C_BOOT", to="U1.BOOT")
SchPlace("R_EN", to="U1.EN")
SchPlace("R_FB_TOP", to="U1.FB")
SchPlace("R_FB_BOT", along="R_FB_TOP.2", side="bottom")
