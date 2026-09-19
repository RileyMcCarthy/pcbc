# Copper plan

The schematic pass ended with a tool an AI can drive without looking: it places, the tool
wires, `kicad-cli` proves the netlist, and what is left comes back as a list of moves.
Copper gets the same shape. This is the plan, what carries over, and the order.

## What carries over from the schematic pass

| Schematic learning | Copper form |
|---|---|
| The AI never draws a wire; the tool wires; KiCad's own netlist is the arbiter | The AI never draws a track. The tool routes; `kicad-cli pcb drc` (0 violations, 0 unconnected) plus "copper connects exactly `board.py`'s nets" is the gate |
| Manual numbers for anchors only; everything else by relation (`to=`, `along=`, `side=`, `align=`, `gap=`) and the tool picks pose and distance | `Place()` CSS stays for anchors (connectors on edges, the MCU's region); passives get `Place("C1", to="U1.VIN")`, `Place("R1", along="U1.EN", side="left")`; the tool picks position, rotation, side, gap |
| Engineer's pose rules (a cap to ground hangs down, a pull-up stands up, a series part lies along) | Layout rules: a decap sits on its pin's side within 2.5 mm with the GND pad toward the plane; a series part lies in line between its ends; a hot loop (VIN cap, switch, inductor, out cap) is adjacent; ESD sits at the connector on the pair; a connector claims an edge |
| Keepouts as boxes, shove along the attach axis, stagger before changing pose, stuck parts stay put and are reported, rigid parts first, no rip-up | Same engine on courtyards plus a fanout lane per pin; connectors and ICs first, passives after |
| A numbered report phrased as moves, a bar per example board, a rules table mapped to tests, renders only during development | `pcbc pcb board.py` prints moves ("C_VIN is 9 mm from U1.VIN: `to="U1.VIN"` puts it at 1.8 mm"); `test_layout_bar` per board; a rules table in the README |
| Byte-for-byte determinism (ids keyed by what they name) | Placement is pure Python. KRT's routed geometry is bit-identical run to run (measured on blinky); only its uuids differ, and pcbc already pins those |
| Library quality gate before drawing | `pcbc check` refuses a `fail`-grade part; the USB-C `A1B12` symbol/footprint mismatch is fixed before the first layout |

## What exists today

**pcbc.** `Place()` is CSS absolute inside the board or a `Region`; `Keepout`; `check_job`
proves each part sits where CSS says, no pad-to-pad short, no sensitive airwire crossing
a switch node. `NetReq` compiles to net classes, DRU rules, planes and a `krt` dict
(USB pairs, power nets, sensitive groups, length-match groups). `route.py` is a
placeholder that draws straight F.Cu segments between pads; it fails DRC, which is
why `test_blinky_fab` is red. `silk.py` legalizes reference text. `fab.py` runs DRC and
writes Gerbers, BOM, CPL.

**KRT** (KiCadRoutingTools, `~/Downloads/KiCadRoutingTools` at `3244726`, venv built,
`KRT_HOME` overrides). Rust A* router (`route.py`), differential pairs (`route_diff.py`),
planes with pad welding and return vias (`route_planes.py`), fine-pitch fanout
(`qfn_fanout.py`), `check_drc.py`, `check_connected.py`, routing plans as JSON
(`make_plan.py` / `run_plan.py`), blocking analysis that names what walls a net in.
Its placer (`place_seed.py`, `place_optimize.py`) is seeded and byte-for-byte, and
`check_floorplan.py` grades a board against a declared intent JSON (zones, edge
connectors, decap distance, keepouts; exit 4 on violations).

Measured 2026-09-19: the blinky seed through `route.py` gives 0 DRC violations and
0 unconnected items on the first try, with identical geometry on a second run.

**pcb-space's lessons with KRT** (the Grok session): `--anchors-first` locked the wrong
parts and the cluster step shoved ICs off the south edge, so KRT is not the placer;
a via lands in an 0603 unless `--same-net-pad-clearance` is passed; GND stayed open
until a plane-finalize step, so the pour is an explicit step with its own check;
pad-row shorts (NTC headers on the Teensy pin row) belong in `check`, before routing.
KRT's own placement doc says why from-scratch placement fails: constraint capture.
`board.py` is the constraint capture.

## Phases

### Phase 0. Gates, and copper that passes (1 to 2 days) — done 2026-09-19

Landed: `check_design` refuses `fail`-grade parts through `pcbc score`; the c3_usb/node
USB-C part now uses EasyEDA's own footprint (pads `A1B12`… match its symbol; the KiCad
footprint pcb-space had paired with it did not); `netcheck.check_copper` (DRC 0/0 plus
pads bound as `board.py` says) runs in the `route` stage; `route.py` calls KRT and
re-keys its ids; `test_blinky_fab` is green; CI installs KRT at `KRT_SHA`.

- `pcbc check` fails on a `fail`-grade part (pin/pad mismatch, multi-unit, KiCad 5
  format). Fix the USB-C part: stacked `A1`/`B12` pins in the symbol, or a footprint
  whose pads are `A1B12`.
- `netcheck.check_copper`: `kicad-cli pcb drc --format json` must report 0 violations
  and 0 unconnected; pads-by-net read from the `.kicad_pcb` must equal `Design.nets`
  by (ref, pad). Runs in the `route` stage of `pcbc build`, in `pcbc review`, and in
  the `kicad`-marked tests.
- Replace the MST placeholder with a minimal KRT call (`route.py`, all nets, board
  layers) and pin the uuids in its output with `stable_uuid`, so `pcbc build` stays
  byte-for-byte. `test_blinky_fab` goes green. Fail loudly when KRT is missing; CI
  clones the pinned SHA.

### Phase 1. Placement language, `pcbc pcb` (the big one, like the schematic pass) — first cut 2026-09-19

Landed: `Place(to=, toward=, gap=, edge=, overhang=)` in `language.py`/`model.py`,
`pcb_place.py` (anchors by CSS, relations in dependency order, smallest cap nearest,
search outward then beside earlier takers, keepout = courtyard ∪ pads, stuck parts
reported), `layout_report` (overlaps, off-board, decap 2.5/5 mm, connector edge),
`pcbc pcb board.py [--json]`. c3_usb and node are fully relational (2–5 anchors each)
and report nothing to move. What the first render showed and the report did not:
silkscreen references on top of each other (Phase 3), crowding without overlap.
Not yet: `side="B"` by relation, hot-loop and in-line rules, airwire crossings, the
KRT floorplan grader as a second opinion.

- **Anchors keep CSS.** `Place("J1", edge="left")` puts a connector on an edge with its
  overhang; `Place("U1", parent="mcu", left=…, top=…)`; `Region`, `Keepout` as now.
- **Everything else is relational.** `Place("C_VIN", to="U1.VIN")` (decap),
  `Place("R_FB", along="U1.FB", side="right")`, `Place("L1", to="U1.SW")`,
  `Place("D_ESD", to="J1.DP1")`, `align="U2.VIN"`, `side="B"` for passives that may go
  underneath. `gap=` is optional; the tool picks courtyard clearance plus a trace lane.
- **Engine** (`pcb_place.py`, the shape of `sch_place.py`): candidate poses (four
  rotations, two sides) scored by the attach net's pad-to-pad distance and airwire
  crossings; keepouts are courtyards plus fanout lanes; shove along the attach axis,
  stagger, stuck parts stay put and are reported; connectors and ICs placed before
  passives; snapped to the 0.05 mm grid; pure and deterministic.
- **Rules as tests**, one table row each: decap within 2.5 mm on the pin's side, GND pad
  toward the plane; series part in line; hot-loop parts adjacent; connector on its edge;
  nothing outside the outline; no courtyard overlap; no pad-row short; airwire crossings
  counted; both sides legal.
- **Report as moves** and a bar per example (blinky 0, buck ≤ 1, c3_usb ≤ 2, node ≤ 2),
  `--json` with every part's pose. Renders during development only; the tests decide.
- **Second opinion, cheap:** compile `board.py` into KRT's floorplan intent JSON
  (Regions → zones, `edge=` → edge_connectors, `to=` → decaps, Keepouts → keepouts) and run
  `check_floorplan.py` in the tests. Same philosophy, independent grader. Not the placer.

### Phase 2. Routing as a compiled plan, `pcbc route` (about a week)

- pcbc emits an ordered KRT plan from `NetReq` and the `krt` dict, never a hand-drawn
  track: fanout for fine-pitch connectors and QFNs; differential pairs; sensitive and
  analog nets with their keep-clear; signals (MPS order); power nets at the width IPC-2221
  gives for `amps`; GND pour last (B.Cu on two layers, plane finalize, return vias);
  cleanup; `check_connected`; `check_drc`; then pcbc's own gate (Phase 0) plus ampacity,
  `max_mm`, vias on no-via nets.
- Failures come back as moves: an unrouted net plus KRT's blocking analysis becomes
  "VIN cannot leave C1: walled in by U1 and J1; put C1 on the other side of U1". The AI
  moves parts. It never touches copper.
- Determinism: the plan is a pure function of `board.py`; KRT geometry is bit-exact and
  the uuids are pinned; the KRT SHA is pinned and checked.
- Tests: blinky, buck, c3_usb (a USB pair), node fab-clean.

### Phase 3. Silk, fab, review (days)

- References placed like labels were (`silk.py` exists), reported as moves.
- CPL rotation check against the JLC convention, the review page's copper and 3D
  (STEP models are in place now).

## Decisions

- KRT routes. The evidence is above. KRT does not place; `board.py` plus pcbc's own
  engine does, for the reason KRT's own doc gives.
- Two-layer JLC defaults (0.10 mm floor) stay the baseline; four layers when a board asks.
- Passives may go on the back only when `board.py` says `side="B"`.
- Phase 0 first: it is small, it turns the one red test green, and every later phase
  runs behind its gate.
