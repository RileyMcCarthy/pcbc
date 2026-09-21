"""Two `Ground()` nets tied at one point, which is the board the DS2 Addon should be.

The DS2 Addon is where technique 6 found its problem — two grounds, an ADC referenced to both, and
nothing joining them (`docs/stitch-plan.md` S3) — and it is read-only, outside this repo, and cannot
be edited to prove the fix. This is that board's shape with the fix in it: a digital domain and an
analog domain, each with its own rail and its own decoupling, and one 0R where they meet.

What it exists to prove, none of which a unit test can:

- `Bridge()` survives the whole pipeline — check, schematic, placement, routing, KiCad's own DRC,
  fab — and `netcheck.check_copper` passes it with **no gate change at all**. `copper_nets` reads
  pad bindings inside footprint blocks and never a `(segment)` or a `(via)`, so a tie made of a part
  changes no binding: `expected_nets` and `copper_nets` agree, and KiCad sees two nets and a part.
- `build._bridge_gate` reads `tied` off the finished copper, through the plane branch of
  `route_verify._pad_fed` — `GND` is poured here, so `R11.2` is fed by the pour and by no track at
  all, which is exactly the DS2 Addon's situation and is invisible to a track-and-via flood.
- `route_checks.check_bridge` stays **silent**: `R11` sits between the two domains, so it already is
  their closest cross-footprint approach and there is no `Place()` move to print.

`NetReq("GND", kind="power")` beside `Ground("GND")` is load-bearing and not decoration: without it
`krt_plan` schedules no `gnd_pour` on a two-layer board, the pour is never written, and the plane
branch this fixture exists to exercise would never run — the blinky trap of
`docs/stitch-plan.md` §7.1, which has no `(zone ...)` block on its routed board at all.
"""

from pcbc import Board, Bridge, Capacitor, Ground, NetReq, Place, Power, Resistor, SchPlace

# Digital domain, east.
V33 = Power("3V3")
GND = Ground("GND")
# Analog domain, west.
VDDA = Power("VDDA")
VSS = Ground("VSS")

c = dict(package="0805", manufacturer="Yageo", mpn="CC0805KRX7R9BB104", lcsc="C49678")

# The tie: one part, two pads, one on each ground. pcbc writes no copper for it.
Resistor("R11", "0R", package="0805", manufacturer="UniOhm", mpn="0805W8F0000T5E", lcsc="C17888", p1=VSS, p2=GND)
Capacitor("C1", "100nF", p1=VDDA, p2=VSS, **c)
Capacitor("C2", "100nF", p1=VDDA, p2=VSS, **c)
Capacitor("C3", "100nF", p1=V33, p2=GND, **c)
Capacitor("C4", "100nF", p1=V33, p2=GND, **c)

Board(width=40, height=25, layers=2, stackup="jlcpcb_2l_1oz")
NetReq("3V3", "GND", "VDDA", "VSS", kind="power", volts=3.3, amps=0.1)
Bridge("GND", "VSS", at="R11", why="the analog return meets the digital one under the ADC")

# The analog pair west, the digital pair east, the tie in the middle: R11 is the two domains' own
# closest approach, which is what `route_checks.check_bridge` asks of a declared tie.
Place("C1", position="absolute", left=6, top=6, locked=True, reason="analog decoupling, west")
Place("C2", position="absolute", left=6, top=15, locked=True, reason="analog decoupling, west")
Place("R11", position="absolute", left=18.5, top=10.5, locked=True, reason="the one tie, between the domains")
Place("C3", position="absolute", right=6, top=6, locked=True, reason="digital decoupling, east")
Place("C4", position="absolute", right=6, top=15, locked=True, reason="digital decoupling, east")

SchPlace("R11", left=60, top=40)
SchPlace("C1", left=20, top=20)
SchPlace("C2", left=20, top=60)
SchPlace("C3", left=110, top=20)
SchPlace("C4", left=110, top=60)
