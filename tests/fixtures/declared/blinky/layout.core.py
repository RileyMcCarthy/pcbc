# blinky: the outline and where each part goes (docs/direction.md §2: placement is layout.core.py's).
Outline(width=40, height=25)
Place(
    "R1",
    position="absolute",
    left=8,
    top=0,
    bottom=0,
    margin_top="auto",
    margin_bottom="auto",
    locked=True,
    reason="resistor west of LED",
)
Place(
    "D1",
    position="absolute",
    right=8,
    top=0,
    bottom=0,
    margin_top="auto",
    margin_bottom="auto",
    locked=True,
    reason="LED at east edge",
)
