# blinky: where each part goes (docs/direction.md §2: placement is layout.core.py's).
# No Outline(): the outline is derived from the placement and the routing (docs/board-outline.md B).
Place("R1", position="absolute", left=8, top=12.03, locked=True, reason="resistor west of LED")
Place("D1", to="R1.2", reason="LED beside its resistor")
