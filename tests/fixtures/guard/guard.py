"""The board a guard can be proven on, because none of the five can prove it.

`docs/stitch-plan.md` S7 and section 7.1. A guard is the one pattern with no population at all on the
real boards: measured 2026-09-21, pcbc routes something end to end on four of the five, and on the one
board that declares an analog net the analog nets are *KRT's* — so the intersection of "a net pcbc
routed" and "a net worth guarding" is empty, and the best flank any pcbc-routed net has is 8 to 65 %
clear (blinky's near-empty board reaches 90 %). A fixture is the only way to put the copper on a board
and run it through check, place, route, KiCad's own DRC and fab.

It must be a board and not a unit test, and section 7.1 says why: a unit test cannot pin a report
string, cannot exercise the `final` stage inside `route_job`, and cannot prove a stitch via lands in a
pour that only `gnd_pour` writes.

What each line is here for:

- `NetReq("GND", kind="power")` beside `Ground("GND")` — without it `krt_plan` schedules no
  `gnd_pour` on a two-layer board, the pour is never written, and every stitch via is an orphan. That
  is the blinky trap of section 7.1 measured: blinky's routed board has **zero** `(zone ...)` blocks.
- `SIG` — two pads 16 mm apart on one straight line, so `hop._allowed_shapes` takes its
  straight-line branch and **pcbc** routes it. This is the guarded run, and `Guard("SIG")` is the
  whole point of the board.
- `C1` — a 0402 parked against the north flank, the one part here that exists to be in the way. It
  interrupts that side and nothing else, which is what makes the partial rule (R-S3, B.6) a
  measurement instead of an assertion. Its pads are on `3V3`, not `GND`: a same-net obstacle
  contributes nothing to `clear_runs` by construction, so a `GND` part parked here would block
  nothing at all.
- `SIG2` — a second guarded net whose two pads are diagonal from one another and 5 mm apart, so the
  hop is inside `HOP_MM`, takes a `Z` shape, and its path has a **45 degree leg and a corner**. That
  is the branch where the offset polyline's vertices come from intersecting two offset lines rather
  than from translating one, and it is the only construction in the guard that can land off the
  nanometre grid if it is done the obvious way.
- `BUS` — a third net running parallel 1.5 mm from `SIG`, so `clear_runs` has a real obstacle to
  report rather than an empty board to agree with.
- `AIN` — `kind="analog"` with **three** pads, which no pattern registered in R2 can route: `hop`
  takes two-pad nets only, `spine` takes power nets only, and `chain` is written and deliberately not
  registered (`patterns.MID`). So `AIN` is KRT's for a structural reason rather than a tuned one —
  which is the DS2 Addon's situation exactly, where every declared-analog net is KRT's — and
  `Guard("AIN")` is then B.6's deferral, printed verbatim, with no copper. Both provenances on one
  board, and the one that defers cannot start passing by accident.

Every part is `locked=True` at an absolute position: the guard's geometry is the whole subject, and a
placer that moved a part would move the measurement with it.
"""

from pcbc import Board, Capacitor, Ground, Guard, Net, NetReq, Power, Resistor, SchPlace

V33 = Power("3V3")
GND = Ground("GND")
SIG = Net("SIG")
SIG2 = Net("SIG2")
BUS = Net("BUS")
AIN = Net("AIN")

r = dict(package="0805", manufacturer="UniOhm", mpn="0805W8F1002T5E", lcsc="C17513")
r402 = dict(package="0402", manufacturer="UniOhm", mpn="0402WGF1002TCE", lcsc="C25744")
c402 = dict(package="0402", manufacturer="Yageo", mpn="CC0402KRX7R7BB104", lcsc="C60478")
c805 = dict(package="0805", manufacturer="Yageo", mpn="CC0805KRX7R9BB104", lcsc="C49678")

# The guarded run: R1.2 -> R2.1, one straight line.
Resistor("R1", "10k", p1=V33, p2=SIG, **r)
Resistor("R2", "10k", p1=SIG, p2=GND, **r)
# The obstacles on the north flank. Two 0402s, and their 3V3 pads at opposite ends of the pair, so
# the flank comes back in THREE stretches rather than two: the second blocked stretch is what proves
# a guard reassembles a run it has been cut out of, instead of merely stopping at the first thing in
# its way.
Capacitor("C1", "100nF", p1=V33, p2=GND, **c402)
Capacitor("C2", "100nF", p1=GND, p2=V33, **c402)
# The second guarded run: a Z with a 45 degree leg.
Resistor("R3", "10k", p1=V33, p2=SIG2, **r)
Resistor("R4", "10k", p1=SIG2, p2=GND, **r)
# The parallel neighbour 1.5 mm south of SIG.
Resistor("R6", "10k", p1=V33, p2=BUS, **r402)
Resistor("R7", "10k", p1=BUS, p2=GND, **r402)
# The net KRT routes: R5 sits on the straight line between R8 and R9.
Resistor("R8", "10k", p1=V33, p2=AIN, **r)
Resistor("R9", "10k", p1=AIN, p2=GND, **r)
Resistor("R5", "10k", p1=AIN, p2=GND, **r)  # AIN's third pad: three pads is never a hop
Capacitor("C5", "100nF", p1=V33, p2=GND, **c805)

Board(layers=2, stackup="jlcpcb_2l_1oz")
NetReq("3V3", kind="power", volts=3.3); NetReq("GND", kind="power")
NetReq("AIN", kind="analog", max_mm=30)
Guard("SIG", stitch_mm=2.5)
Guard("SIG2", stitch_mm=2.5)
Guard("AIN", stitch_mm=2.5)


























