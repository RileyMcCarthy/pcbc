"""The board a thermal via-in-pad array reaches fab on (the second refutation round, major 5).

`Thermal()` puts vias **inside** an exposed land on purpose, and the fab package must then order those
holes epoxy-filled and capped (IPC-4761 Type VII) — `fab._thermal_notes`. The only boards with a land
(`pad_prop_heatsink`) are c3_usb and node, and neither reaches fab while `VBUS` is unrouted at its
USB-C connector, so this board carries just the module and its decoupling cap: the ESP32-C3-MINI's
nine GND blocks, its 3V3 pin, one cap. The test copies the module's part directory in beside it from
`examples/c3_usb/components`.

`watts=1.0`, not c3_usb's 0.35, keeps the test this board was made for (24 barrels) as it was measured.
What 0.35 W showed, corrected by the third refutation round: the array asked for 9 barrels and got 9,
but in 6 of the 9 blocks — a 3V3 track on B.Cu under the top row (y 11.275) refused every first-rank
site there, the rank-major walk placed its 9 before it tried their second-rank sites, and 3 blocks got
none, 2 of them left with nothing joining them. The router took the nine blocks of pad `49` for one
terminal (KiCad joins copper, not pad numbers), so the build blamed itself: "KiCad reports GND
unconnected and the router reported them routed: this is a pcbc router bug". Both are fixed
(`scratchpad/native/fix3/`): every block is a routing terminal (`route_native.pad_terminals`), and
every block of the land takes a barrel before any takes a second (`patterns.stitch.run`) — at 0.35 W
this board reaches fab with 9 barrels in 9 blocks (`tests/test_native_refuted3.py`).
"""

from pcbc import Board, Capacitor, Ground, Net, NetReq, Place, Power, SchPlace, Thermal, load

ESP32 = load("./components/Espressif/ESP32-C3-MINI-1-N4")

V3V3 = Power("3V3")
GND = Ground("GND")

# The module's other bound pins go to nets of their own, one pad each: nothing to route.
ESP32("U1", **{"3V3": V3V3, "GND": GND, "EN": Net("EN"), "IO9": Net("IO9"), "IO10": Net("IO10"), "IO18": Net("IO18"), "IO19": Net("IO19")})
Capacitor("C1", "10uF", package="0603", mpn="CL10A106KP8NNNC", lcsc="C19702", manufacturer="Samsung", p1=V3V3, p2=GND)

Board(width=30, height=25, layers=2, stackup="jlcpcb_2l_1oz")
Place("U1", position="absolute", left=2, top=2, locked=True, reason="the module and its land")
Place("C1", position="absolute", right=3, top=10, locked=True, reason="decoupling east of the module")
NetReq("3V3", "GND", kind="power", volts=3.3, amps=0.5)
Thermal("U1.49", watts=1.0)

SchPlace("U1", left=40, top=40)
SchPlace("C1", left=100, top=40)
