"""The board a plane stitch can be proven on, because none of the five can prove it.

`docs/stitch-plan.md` section 8 item 1, R-E1, S8. Until S8 **no board in this repo could pour one net
on two facing layers at all**: `language.Board` refused `planes=` below three layers outright, and
above three it was the only way to declare a pour — so measured 2026-09-21 the five boards were four
at `(('GND','B.Cu'),)` and node at `(('GND','In1.Cu'),('3V3','In2.Cu'))`, two pours of two *different*
nets, between which a via is a short. That measurement is section 2(l) and it is why the whole
technique was deferred. The router now reads `planes=` on both stackups
(`route_scene.plane_targets`), so the declaration on the `Board()` line below is the population.

**Two layers on purpose, and it is the harder of the two proofs.** Four layers would only exercise
the branch that already worked (`krt_plan`'s `planes` step); two layers exercises the branch S8
created — a declared pour on a stackup whose pours used to be hardcoded as `GND` on `B.Cu` — and it
puts both pours on **outer** layers, where they are written by `gnd_pour` after the signals and are
still unfilled when the `final` stage runs. That is the one place a lattice barrel can be welded to
one plane and to nothing on the other, and it is what `patterns.stitch._raster` asks two rasters
about instead of one.

What each line is here for:

- `Board(planes=[("GND", "F.Cu"), ("GND", "B.Cu")])` — the statement that could not be written before
  this slice, and the whole subject of the board.
- `NetReq("GND", "3V3", kind="power")` — without a power `GND` there is no `gnd_pour` step at all and
  the lattice lands in nothing, which is the blinky trap of section 7.1 (blinky's routed board has
  **zero** `(zone ...)` blocks).
- `rise_ps=500` on `CLK` — the edge rate the pitch is derived from, and the one number here that is a
  *declaration* rather than a measurement. 500 ps makes the knee exactly `0.5/0.5 ns` = 1 GHz and the
  arithmetic legible on the page; a real board takes this off the datasheet of whatever drives its
  fastest edge, which is why `NetReq(rise_ps=)` has no default anywhere in pcbc and why a board that
  says nothing gets a refusal instead of a lattice (`docs/stitch-plan.md` section 8 item 3).
  At Dk 4.6 between F.Cu and B.Cu that is 139.779 mm/ns, lambda 139.779 mm, pitch **6.98895 mm**.
- `U1`/`R*`/`C*` — enough copper for the pours to have to flow around, so `clear`-of-everything is a
  measurement rather than an empty board agreeing with itself. `C3` sits in the middle of the board
  precisely so one interior lattice site is refused and the partial note has something to report.
- Every part `locked=True` at an absolute position: the lattice's geometry is the subject, and a
  placer that moved a part would move the measurement with it.
"""

from pcbc import Board, Capacitor, Ground, Net, NetReq, Place, Power, Resistor, SchPlace

V33 = Power("3V3")
GND = Ground("GND")
CLK = Net("CLK")
SIG = Net("SIG")

r = dict(package="0805", manufacturer="UniOhm", mpn="0805W8F1002T5E", lcsc="C17513")
c805 = dict(package="0805", manufacturer="Yageo", mpn="CC0805KRX7R9BB104", lcsc="C49678")

Resistor("R1", "33R", p1=CLK, p2=V33, **r)
Resistor("R2", "33R", p1=CLK, p2=GND, **r)
Resistor("R3", "10k", p1=SIG, p2=V33, **r)
Resistor("R4", "10k", p1=SIG, p2=GND, **r)
Capacitor("C1", "100nF", p1=V33, p2=GND, **c805)
Capacitor("C2", "100nF", p1=V33, p2=GND, **c805)
Capacitor("C3", "100nF", p1=V33, p2=GND, **c805)

Board(width=40, height=30, layers=2, stackup="jlcpcb_2l_1oz", planes=[("GND", "F.Cu"), ("GND", "B.Cu")])
NetReq("3V3", "GND", kind="power", volts=3.3, amps=0.1)
NetReq("CLK", kind="clock", rise_ps=500)

Place("R1", position="absolute", left=5, top=5, locked=True, reason="CLK, the net that declares the edge")
Place("R2", position="absolute", left=11, top=5, locked=True, reason="CLK's other end")
Place("R3", position="absolute", left=5, top=24, locked=True, reason="SIG, a second net for the pours to flow around")
Place("R4", position="absolute", left=11, top=24, locked=True, reason="SIG's other end")
Place("C1", position="absolute", left=30, top=5, locked=True, reason="decoupling, north-east")
Place("C2", position="absolute", left=30, top=24, locked=True, reason="decoupling, south-east")
Place("C3", position="absolute", left=22.74, top=14.02, locked=True, reason="its 3V3 pad sits on the interior lattice site at (23.4945, 15.0), so one site is refused by copper and the note has a blocker to name")

SchPlace("R1", left=20, top=20)
SchPlace("R2", left=60, top=20)
SchPlace("R3", left=20, top=60)
SchPlace("R4", left=60, top=60)
SchPlace("C1", left=100, top=20)
SchPlace("C2", left=100, top=60)
SchPlace("C3", left=140, top=20)
