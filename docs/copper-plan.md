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

**Every board through the whole pipeline (2026-09-19).** What broke, and what the tool
now does about it, so the next AI never meets it:

| Seen | Now |
|---|---|
| `pcbc pcb buck.py` died with a traceback: no vendored 0805 land | 0805/1206 lands vendored (R, C, L, LED); `check` reports a generic with no land |
| ESP32 module pads "actual 0.0000 mm" in DRC after U1 was turned 90° | KiCad stores pad angles as footprint + pad angle in a board file; `apply` turns the pads with the footprint |
| KiCad DRC: USB-C mounting holes have no annular ring | EasyEDA draws them as plated pads with drill = size; `fetch` repairs them to `np_thru_hole`, the scorer fails what is left |
| KiCad applied the Power class clearance (0.2) between the USB-C's own pads (0.1 apart) | a generated rule holds a footprint's own pads to the 0.1 mm fab floor |
| SW_BOOT's pad on the board edge (copper_edge_clearance) | relations keep 0.3 mm off the edge; the report names pads within it |
| the input caps of the buck ended 3.6 mm from VIN because the inductor and output caps, listed later, took the free side first | file order decides: the first `Place()` on a pin gets the closest spot |
| `NetReq("SW", max_mm=6)` was never checked on the copper | the report names the pad farthest from its nearest neighbour on such a net |
| the fab stage re-checked placements from a fresh compile and saw `edge=` parts "moved" | the fab check skips relations it has no pose for; the place stage already verified them |
| buck carried its old hand grid under the new relational lines; every part was placed twice and the fab check saw them all "moved" | `check` refuses a ref with two `Place()` (or `SchPlace()`) lines |
| fab dropped fiducials onto the finished node board: FID1 on U4's pad and the 3V3 tracks (11 DRC errors after a clean route) | fiducials are placed in the place stage after the anchors, in the free corners (then edge middles); relations keep off them; the router routes around them; the report says when fewer than 3 corners are free |
| the router put vias on the switch node and the analog nets (`NetReq vias=False`), and routed the USB pair single-ended | the route stage compiles a plan from `NetReq`: constrained nets first on their layer, pairs as pairs, planes, then the rest at power widths, GND pour on two layers (`tests/test_route_plan.py`) |
| KRT quietly rewrote the board's minimum clearance ("FAB FLOOR RELAXED 0.16 → 0.1517") to make its own copper read clean | every step runs with `--no-fix-drc-settings`; pcbc's rules are what KiCad judges by |
| once KRT stopped rewriting them, KiCad's defaults judged the vias: 0.2 mm drills on a 2-layer board (min 0.3), 0.075 mm rings (min 0.1), tracks 0.236 from holes (min 0.25); pcbc had only ever written min clearance and track width into the project | the `Stackup` carries the fab's limits (track, clearance, via drill/diameter, annular ring, hole clearance, hole-to-hole, edge) and is the one source for the project's constraints, the net classes' vias, the router's clearances, and the copper gate |
| KRT keeps a track `clearance` from a via's ring; the fab wants `hole_clearance` from the hole itself, so tracks sat 0.24 mm from holes that need 0.254 | every routing step's `--clearance` is at least `hole_clearance - annular_ring` |
| the 90 Ω pair geometry on 1.6 mm FR4 came out at 0.10 mm, under the fab's 0.127 mm trace | pair width and gap are floored at the stackup, like every class |
| KRT writes pours without fills; KiCad judged the unfilled planes as no copper (17 unconnected items on node) and the Gerbers would have carried no plane | the copper gate runs DRC with `--refill-zones --save-board`: what it judged is what the fab gets |
| a track ran through a fiducial's 2 mm mask opening (solder-mask bridge) | each fiducial gets a keepout the size of its courtyard before routing |
| at the ESP32's 0.8 mm pitch pads KRT escalated to "advanced" vias (0.25/0.15) and 0.0889 mm tracks; the board's own constraints then rejected them | every step reads a `fab_overrides.txt` written from the stackup, which pins KRT's floor and disables the escalation; the 4-layer track and clearance floor is 3.5 mil exactly (0.0889) |
| `--clearance` is a ceiling: passing the hole floor capped the Power class from 0.2 to 0.184 and KiCad then flagged Power pads 0.17 from other nets' vias | every net class's clearance is at least `hole_clearance - annular ring`, the classes carry the numbers, and the signals step names no ceiling |
| once the gate saved the refilled board, two builds of one `board.py` differed: KiCad invents random ids for pads, fields and graphics that have none when it saves | every uuid is re-keyed by position after KiCad's save, in the route stage and in fab; `test_blinky_routes_clean_and_the_same_twice` holds |
| a build killed mid-route left `01_analog_nets.kicad_pro` behind; KRT records settings in a step's sibling project file and the next build's step inherited a stale one: FB routed twice, shorted into the JST's GND pad, 83 clearance errors | the route stage deletes every step file in its work dir before it starts |

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

### Phase 2. Routing as a compiled plan, `pcbc route` (about a week) — first cut 2026-09-19

Landed: `route.krt_plan` compiles `NetReq` into the ordered KRT chain (constrained nets,
pairs, planes, signals at power widths, GND pour + finalize on two layers), every step
pinned to the stackup's fab rung, the copper gate judging filled pours, and all four
examples building to a JLC package (`tests/test_examples_fab.py`, CI). Failures still
come back as KRT's own log rather than as part moves; that, and the second-opinion
graders, are what is left of this phase.

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

### Phase 4. A real board: the MaD DS2 Addon (started 2026-09-19)

`Hardware/DS2Addon/pcbc/ds2_addon.py` in the MaD repo: rev 1's netlist (exported with
kicad-cli), the ~RESET pull-up, the input and reference RC filters populated instead of
DNP-behind-jumpers, every part with an LCSC id. First pass: check clean, netlist proven,
ERC clean, copper placed with nothing to move, 17 schematic moves. What it taught the tool:
- a series resistor off a header pin went up (the free-side rule is for hangers); now a
  resistor whose far end goes on to another part lies in line with its pin;
- the schematic report should say when a part lies past the sheet's edge (the headers'
  pins pointed left, so their hangers ran off the sheet; the fix was `mirror="y"`) — not done yet;
- two filter caps on adjacent 2.54 mm header rows collide; the idiom that draws clean is
  the upper resistor further out (`gap=12.7`) so its cap hangs past the lower node;
- left at 3 moves, all tool work: two hats on the DIP-style ADC's adjacent GND/AVSS and
  DVDD/AVDD pins collide with the neighbouring pins' stub wires, and a resistor's text on
  a 2.54 mm row meets the wire of the row beside it.

Copper, the same board:

| Seen | Now |
|---|---|
| all 22 passives were placed `to=` the TSSOP; its 0.65 mm pad rows had no escape and KRT left three pads unreached while pcbc's open-net check saw copper and said nothing | KRT's own `failed_single` list is read from every step and reported as a move with the net's parts and its constraint; the filters were moved to their connectors, the series resistors to the MCU header (an intent the AI decides) |
| a pin header flush with the edge put its pads 0.27 mm from it | `edge=` steps a part in by what its copper needs (`_edge_css`); a USB-C whose pads sit deep inside still goes flush |
| grouping relations by pin put DVDD's bulk cap where AVDD's cap had to go | relations are placed in file order and nothing else; the skill says to list the small decoupling cap first |
| the lean of a cap's ground pad toward a ground pad outweighed its distance to its own pin (3.35 mm) | the attach distance dominates the score (lean at 0.15) |
| the silk retry gave a decoupling cap more gap and the decap rule then failed it | decoupling caps never move for silk; a reference with no room is hidden and noted (`style:`) |
| the second cap on a shared 0.65 mm pad row can physically sit no nearer than 3 mm | supply pins side by side share one decoupling row: the nearest cap 2.5 mm, the next 5 |
| the signal step re-routed the no-via analog net with four vias after its own step; `--nets * !AIN0` did not keep KRT's rip-up off it | a constrained step's copper is KiCad-locked before the next step (`lock_copper`); the fab check then holds |
| `NetReq(max_mm=20)` was a guess that failed on a 47 mm board | `max_mm=30`; the check reports the airwire it measured |
| with the analog copper locked, two short hops (a header pin to the resistor beside it, 0.5 mm apart) found no path: long nets routed before them walled them in and rip-up could no longer move the locked copper | short hops (every pad of the net within 5 mm, `LOCAL_MM`) are routed right after the constrained nets, before anything long: they cost nothing and block nothing |
| even routed first, the 0.2 mm hop from a header pin to its resistor found no path: `--same-net-pad-clearance` (pcb-space's fix for vias landing in 0603 pads) also keeps a track off a same-net pad | the flag stays on the plane step only, where the tap vias are; track steps route without it |
| `(locked yes)` written between `(end)` and `(width)` made KRT's regex parser skip those segments entirely: they stopped being obstacles and the signal step routed straight through them (shorts, crossings) | the lock goes where KiCad writes it and KRT reads it: after `(width)` on a segment, after `(layers)` on a via (`test_route_plan.py`) |
| the TSSOP's AVDD pad found no path on any layer: its decaps sat 0.48 mm above the 0.65 mm row (courtyard + 0.2), the locked analog copper ran under the body south of it, and the 0.65 mm neighbours left 0.31 mm on each side, less than a track and two clearances; the one corridor held one via, and 3V3's took it | a pad row nothing can pass between is *closed* and keeps a fanout lane outside it, one via plus the widest class clearance wide (0.9 mm on 2L): relations settle past it, the report says who sits in it, and a decoupling cap on such a row may sit that much further (`Foot.lane`, `stackup.fanout_lane`) |
| the same rule read the turned ESP32 module's 0.8 mm rows as 0.4 mm gaps: in a board file a pad's angle includes the footprint's, and the parser swapped width and height in the wrong frame | pads are read in the footprint's own frame (`parse_foot` subtracts the footprint's angle); the edge rule turns them to world by the footprint's rotation as before |
| with the lane the board routed, and the fab stage refused it: the signals step had dropped a VSS via inside the analog cap's pad | `--same-net-pad-clearance` goes on the long-net signals step as well as the pour (KRT's keepout blocks via placement only); the short-hop and constrained steps stay without it |
| then the ground pin of the TSSOP stayed open and pcbc said `unrouted: []`: KRT named it only in `pad_pairs_open` (route.py) and as `unconnected pad U1 on 'GND'` (route_planes.py), and pcbc read only `failed_single` | both route-step fields are read; the pour's list only annotates a net a route step left open, since KRT's bare pour defers every tap to the step after it (node listed 55 GND pads, all welded a step later) (`_unreached_pads`, `test_route.py`) |
| the short-hop step's copper was locked too (`lock_copper` keyed on step names ending in `_nets`, and the step was `local_nets`); a 2 mm header-to-resistor hop whose straight path the locked analog copper had cut, with vias costing 100000, became a 65 mm F.Cu detour around the board, locked, under the ADC | the step is `local_hops` and only `*_nets` (constrained) copper is locked |
| unlocked and allowed a via, the cut hop dropped its via inside the resistor's pad and the fab stage refused the board; with the same-net keepout on the hop step instead, the 0.5 mm hop A0 was `boxed_in_static` by the locked analog copper: the analog step, first on the empty board, had run REFP_F through the 0.54 mm slot between J4.1 and R1.1 and AIN1 through the slot between J2.3 and R6.1 | order: hops first on the empty board (one path each; a long net goes around a hop for free), then the constrained nets, then the rest; the hop step carries the same-net keepout too, since nothing cuts a hop any more |
| the pull-up on the TSSOP's ~RESET pin was placed beside the row at the row's own height, so its 2.5 mm hop ran along the lane under pins 1 and 2 and those had no escape (GND, GPIO0, GPIO1 open) | a part attached to a closed-row pad goes straight out through the pad's lane and nowhere else (`Foot.escape`, `_attach`); `toward=` still overrides |
| with the lane 0.9 mm wide the analog nets, first on the empty board, ran along it (AIN0 at 8.15, AIN1 at 7.7, locked) and walled AVDD, DVDD and the UART pins in; before the lane they had gone under the body | the lane is spent before anything routes: a stub and a staggered via just past every unconstrained pad of a closed row, locked; constrained nets and pairs are left on their pads and escape between the vias or under the body |
| the examples then broke: c3_usb's USB pair passed a VBUS pad at 0.195 (Power asks 0.2) because the pair step passed `--clearance 0.16`, and `--clearance` is a ceiling on every class; node's planes welded 4 of 59 GND pads because KRT's pour (its "bare pour", #562) places no tap vias, the route step welds pads to the plane, and the same-net keepout on that step stopped it; c3_usb's GND tracks sat 0.25 from the USB-C's mounting peg against a 0.254 rule KRT cannot hold on its 0.05 mm grid | no `--clearance` on the pair and constrained steps (the classes carry it); on four layers the plane nets get their own `plane_taps` step with the keepout off explicitly (`-1`, KRT records the keepout in the sibling project) and the signals step leaves them out; the stackup's hole clearance is 0.25 |
| the first rule file with a `track_angle (min 135deg)` constraint reported nothing wrong on any board: KiCad 10 rejects the unit suffix and, with one bad rule, silently applies no custom rule at all; kicad-cli prints nothing | a canary rule (`length (max 0.001mm)` on one net) must fire on every board or the gate fails; the angle rule is `(min 135)`; both geometry rules are warnings that feed the copper bar (router-plan R0) |
| node's 1 A LOAD net was routed at 0.09 mm: its two pads sit within 5 mm, so it is a hop, and the hop step routed everything at the minimum width; the fab stage caught it (`LOAD copper 0.09 mm < 0.30 mm`) | every step that may route a power net carries `--power-nets` and their widths, the hop step included |
| KRT's `qfn_fanout.py --escape-method underpad` did that, but staggered neighbouring vias by copper clearance alone (holes 0.42 mm apart on the 0.65 mm row; JLC wants 0.5, KiCad only warns) and, when its second via row did not fit the 0.9 mm lane, hunted past the decaps with 3.5 mm stubs that grazed their pads (Power clearance 0.2, actual 0.01) | the fanout is pcbc's own (`fanout.py`): the stackup's numbers and nothing else; the lane is sized for two via rows staggered for hole-to-hole (`fanout_lane`, `fanout_stagger`: 1.29 mm on a 0.65 mm row at 2L), vias on KRT's grid with the second row snapped from the first, and `hole_to_hole` is an error in pcbc's project so the gate sees it |

### Phase 3. Silk, fab, review (days) — landed 2026-09-19

References are placed at the place stage (`silk.legalize_silk`): 16 spots around the part
including four turned 90°, and on the body of a part 8 mm or bigger away from its pads;
a reference with no room comes back as a move, and `place_job` first re-places that part
with 0.6 mm more gap (two rounds). `pcbc review` shows the fab board (filled pours, silk)
and a copper note from the gate. What the node render had shown (cramped C_EN, the
module's reference nowhere) is what the report now says and the tool now fixes.

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
