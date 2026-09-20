# Router plan: constraint-first copper

Written 2026-09-19, after the DS2 Addon became the first real board through `pcbc build`.
Companion to `copper-plan.md`, which records what KRT taught the tool. This plan is for
replacing the maze router with pcbc's own, built the way the schematic and placement passes
were built: the AI states intent, the tool makes deterministic geometry, KiCad judges, and
every failure comes back as an edit to `board.py`.

## 0. Why, in one paragraph

Routing a real board with KRT cost two days of workarounds that had nothing to do with finding
paths: a `--clearance` flag that is a ceiling on every class, a pour that places no taps and
lists every pad as unconnected, a same-net keepout recorded in the project file, 0.05 mm
staircases in a 0.4 mm corridor, failures reported in fields nobody reads. Every one of those
is an interface problem, and the fix each time was to move a rule into pcbc. The thing that
would make pcbc's copper special is the same thing that made its schematics work: **one
source of truth**. A net's intent (`NetReq`) compiled into numbers from the stackup, then
enforced three times with the same numbers: at placement (before any copper exists), in the
router (by construction), and in KiCad's own rule file (so the arbiter judges exactly what
the router obeyed). No commercial tool gives an AI that loop, because no commercial tool is
the placer, the router and the rule writer at once.

## 1. Goals and non-goals

Goals:
- Copper that is legal by construction: widths, clearances, 0/45/90 geometry, via and hole
  rules from the stackup and classes, so the gate almost never finds anything.
- Constraints the practice actually needs (section 4), each enforced where it is cheapest.
- Failures as moves: blocking analysis in pad terms, phrased as `Place()` edits.
- A copper bar in the report and the tests, like the schematic readability bar.
- Deterministic, byte for byte, with no hidden state between steps.
- A router that explains itself: every piece of copper has a reason (fanout, hop, chain, tap,
  bus, spine, signal) and its own failure mode.

Non-goals:
- Free-angle or arc routing. 0/45/90 only.
- Interactive push-and-shove. There is no human at the mouse.
- Beating commercial routers on completion rate for dense boards. The placer keeps boards
  routable; the router refuses to violate a rule to finish a net.
- Autoplacement. Placement stays the AI's intent plus pcbc's relations.

## 2. What the arbiter enforces today (measured, KiCad 10.0.6)

Custom rules probed one at a time against the built DS2 board (`kicad-cli pcb drc`), each
written into the board's `.kicad_dru`:

| constraint | condition tried | result |
|---|---|---|
| `length (max 10mm)` | `A.NetName == 'AIN0'` | `length_out_of_range`, works on whole nets |
| `skew (max 0.1mm)` | two nets by name | `skew_out_of_range`, works on a matched group |
| `via_count (max 0)` | one net | `too_many_vias` |
| `creepage (min 3mm)` | `A.NetName == 'VDDA' && B.NetName == 'GND'` | `creepage`, 2 hits |
| `track_segment_length (min 0.2mm)` | `A.Type == 'Track'` | 111 hits: exactly the router's micro-jogs |
| `track_angle (min 135deg)` | `A.Type == 'Track'` | parses; 0 hits (45 degree bends are 135 inside) |
| `clearance (min 1mm)` | `A.hasNetclass('Analog') && B.hasNetclass('Power')` | 303 hits: class conditions work |
| `disallow track` | `A.intersectsArea('FID1_mask')` | 0 hits: rule areas work, and the fiducial masks are clear |
| `physical_clearance (min 0.5mm)` | two nets by name | parses; 0 hits on this board |

Also available per the rule reference: `diff_pair_gap`, `diff_pair_uncoupled`,
`hole_clearance`, `hole_to_hole`, `edge_clearance`, `annular_width`, `hole`, `disallow` (track,
via, micro_via, buried_via, pad, zone, text, graphic, hole, footprint), `zone_connection`,
`thermal_relief_gap`, `thermal_spoke_width`, `courtyard_clearance`, `silk_clearance`,
`track_width`; conditions `A.NetClass`, `A.Net`, `A.Type`, `A.Layer`, `A.intersectsArea()`,
`A.enclosedByArea()`, `A.inDiffPair()`, `AB.isCoupledDiffPair()`, `A.memberOfGroup()`,
`A.memberOfFootprint()`, `A.existsOnLayer()`, `A.isPlated()`, `A.intersectsCourtyard()`.
Severities are per type in the project (`rule_severities`); pcbc already raises
`hole_to_hole` to an error.

Two gotchas, both requirements:
- **One malformed rule silently disables every rule**, and `kicad-cli` prints nothing. The
  combined probe file returned zero violations for every rule. pcbc must write a canary rule
  that is known to fire on every board (a `track_segment_length (min 0.001mm)` on a
  deliberately named test track is one option; simpler: a rule that must produce exactly one
  `items_not_allowed` against a one-off graphic pcbc places for that purpose) and fail the gate
  if the canary is missing from the report.
- KiCad checks copper against the **filled** zones only; the gate already refills and saves.

What KiCad does not check and pcbc must: a high-speed track crossing a split in its reference
plane; return vias at a reference change; the loop area of a decoupling cap or a hot loop;
current per via and vias in parallel; a chain order along a signal; parallel-run length between
neighbours (crosstalk); serpentine geometry; the copper bar.

## 3. Fab facts to build against (JLCPCB, September 2026)

Limits (their capabilities page; pcbc's `Stackup` carries the ones in bold, the rest are new):

| item | JLC | pcbc today |
|---|---|---|
| track / space, 1 oz | 0.10 / 0.10 (2L), 0.09 / 0.09 (4L+) | **0.127 / 0.127** (2L), **0.0889** (4L): held at the free rung |
| track / space, 2 oz | 0.16 (2L), 0.15 (4L+) | not modelled: `copper_oz` exists, limits do not follow it |
| via hole / diameter | 0.15 / 0.25 min; 0.3 / 0.5 standard | **0.3 / 0.5** (2L), **0.2 / 0.35** (4L) |
| annular ring | 0.15 min, 0.2 multilayer | **0.1** (2L), **0.075** (4L): to reconcile |
| via to via (hole to hole) | 0.2 stated; pads 0.45 | **0.5**: JLC's number for pad holes; keep, it is what KiCad's default judged |
| hole to copper | 0.2 (NPTH), 0.28 PTH-to-track | **0.25** (what KRT holds on its grid) |
| copper to edge | 0.2 | **0.3** (routing wants the lane) |
| solder mask bridge | 0.10 (1 oz) | not modelled; KiCad's `solder_mask_bridge` check exists |
| via-in-pad | filled and capped, 0.15 to 0.55 | refused on passives at fab, allowed on IC pins |

The annular ring row matters: a 0.5/0.3 via has a 0.1 ring, under JLC's stated 0.15 minimum
for a 2-layer board. Their standard via is nonetheless 0.3/0.5 (they cap the drill, not the
ring). The stackup should say which rung it is on and the router must never invent a via size.

Impedance stackups (their impedance page), all 4-layer variants at 0.8 to 2.0 mm, core Dk 4.6,
solder mask Dk 3.8 (0.03 mm above substrate, 0.015 above trace):

| code | prepreg | thickness | Dk |
|---|---|---|---|
| JLC04161H-7628 | 7628 | 0.2104 mm | 4.4 |
| JLC04161H-3313 | 3313 | 0.0994 mm | 4.1 |
| JLC04161H-2116 | 2116 | 0.1164 mm | 4.16 |
| JLC04161H-1080 | 1080 | 0.0764 mm | 3.91 |

pcbc's `jlcpcb_4l_1oz` says `h_mm 0.12, er 4.5`, which is none of these. Requirement R-Z1:
a stackup names its JLC code and carries every dielectric, so microstrip, stripline and
coplanar widths come from real numbers, and the compile report says which.

## 4. Requirements, by practice

Each requirement says what the practice demands, where the number comes from, and where pcbc
enforces it: **L** language, **C** compile (numbers), **P** placement check, **R** router,
**K** KiCad rule written by pcbc, **V** pcbc's own verification, **B** copper bar.

### 4.1 Current

- R-I1 Track width from current. IPC-2152 replaces IPC-2221: external k about 0.089, internal
  0.063, with plane-proximity correction (a track over a plane carries 20 to 40 % more).
  `ipc2221_width_mm` stays as the conservative floor; add IPC-2152 with the plane term when a
  plane is declared under the track. **C, K** (`track_width (min)` per class), **V** (a
  track narrower than its class is a KiCad error already via the class; a neck at a pad is
  allowed by a rule area around the pad).
- R-I2 Vias in parallel. A 0.3 mm drill 1 oz via carries roughly 1 A at 10 C rise; a net over
  its rating gets `n = ceil(amps / via_amps)` vias per layer change. **C, R, V**.
- R-I3 Neck-down. A power track may narrow to the pad's width for at most the fanout lane's
  length. **R**, **K** (rule area per closed row, `track_width (min)` relaxed inside it).
- R-I4 Corridor. A wide track needs a channel: placement checks that a channel of the class
  width plus two clearances exists between the pads on at least one layer, ignoring other
  nets (rasterised, cheap). **P**.

### 4.2 Voltage

- R-V1 Clearance and creepage by voltage. IPC-2221 table 6-1 for clearance (B2, external,
  uncoated: 0.6 mm below 50 V rising to 3.2 mm at 500 V) and IEC 62368-1 for mains creepage.
  `NetReq(volts=)` picks the row; the class clearance and a `creepage (min)` rule follow.
  **C, K** (`clearance`, `creepage` by `hasNetclass`).
- R-V2 Isolation line. `Isolation("primary", "secondary", volts=)` in `board.py` becomes a
  rule area with `disallow` for both sides' nets, a slot if asked, and a placement check that
  no part straddles it. **L, P, K**.

### 4.3 Decoupling and loops

- R-D1 Decap loop. Pin to cap to plane with the smallest loop: cap within 2.5 mm (plus the
  fanout lane) of the pin, the cap's ground pad tapped to the plane at the cap, not routed to
  the pin's ground. **P** (exists), **R** (the chain pattern: supply feed enters the cap pad
  first, then the pin; the tap via sits at the cap's ground pad), **V** (loop area reported).
- R-D2 Hot loop. For `kind="switch_node"` nets the input cap, switch and low-side element
  form the loop; its area is measured from placement and held to a budget. **P, V, B**.
- R-D3 Feedback. `kind="analog"` or a `feedback` preset: far from the switch node (a
  keep-away distance), no vias, one layer. **P** (exists as max_mm; add keep-away), **K**
  (`clearance` between the two classes, `via_count (max 0)`).

### 4.4 Controlled impedance

- R-Z1 Real dielectrics (section 3). **C**.
- R-Z2 Formulas: IPC-2141 microstrip (exists), symmetric and asymmetric stripline for inner
  layers, coplanar with ground for 2-layer boards where a pour flanks the pair. Calibrated
  against JLC's calculator on the four stackups; the compile report prints width, gap,
  layer and the computed impedance with its formula. **C**.
- R-Z3 Reference plane. A controlled-impedance net names its reference layer (default: the
  nearest plane in `planes=`); the router keeps the track over that plane and pcbc verifies no
  crossing of a split in the filled zone (KiCad has no such check). **R, V**.
- R-Z4 Layer changes on a pair carry a return via within 1 mm on the reference net. **R, V**.
- R-Z5 USB 2.0 high speed as the first preset: 90 ohm differential, tolerance 15 %, pair
  matched (intra-pair skew) to 0.5 mm, uncoupled length at most 2 mm, no stubs, ESD at the
  connector in line, series termination at the source when the IC asks. Sources agree on the
  impedance and disagree on the skew (0.5 to 3.8 mm); pcbc takes the tight number. **L, C, K**
  (`diff_pair_gap`, `diff_pair_uncoupled`, `skew`, `length`).

### 4.5 Length and skew

- R-L1 Maximum length per interface (`max_mm` exists as a nearest-pad check; becomes a
  routed-length rule). **K** (`length (max)`).
- R-L2 Intra-pair skew and inter-lane match: `Pair(...)` and `Bus(..., match_mm=)`. **L, K**
  (`skew (max)`), **R** (tuning).
- R-L3 Serpentines: only in the slack after routing, 45 degree meanders, amplitude at least
  three widths from the pair's own track, never over a split. **R** (tuning pass), **V**.
- R-L4 Placement knows first: the airwire length against the budget is a move before a track
  exists. **P**.

### 4.6 Crosstalk and return path

- R-X1 3W spacing between signal tracks of different nets on the same layer, 5W for clocks
  and analog inputs; parallel runs longer than 10 mm at less than 3W are reported. **R** (cost
  term), **V, B**.
- R-X2 Adjacent signal layers route orthogonally (a per-layer preferred direction in the
  cost). **R**.
- R-X3 Stitching vias along a guard or a plane edge every 2.5 mm (the "100 mil" rule) when a
  net asks for a guard. **R** (pattern).
- R-X4 No stubs: a track that branches to a third pad on a high-speed net is a chain order
  violation. **P** (chain), **V**.

### 4.7 Analog and sense

- R-A1 Same layer, no vias, short (exists: `kind="analog"`). Add keep-away from switch nodes
  and heat sources (`keep_clear_of` exists in the model, unused). **P, K**.
- R-A2 Kelvin sense: `Chain` from the load pad, not the supply track; symmetric pair routing
  for bridge inputs (same layer, same length within 1 mm). **L, R, K** (`skew`).
- R-A3 Guard: optional ground guard around an input, stitched (R-X3). **L, R**.

### 4.8 Manufacturing

- R-M1 Every limit in section 3 from the stackup, none in the router. **C, R, K**.
- R-M2 Geometry: 0/45/90 by construction; no segment under 0.2 mm except pad entries; no
  acute angle. **R**, **K** (`track_segment_length (min 0.2mm)`, `track_angle (min 135deg)`),
  **B**.
- R-M3 Teardrops on tracks entering vias and pads thinner than the via: KiCad can generate
  them; the router leaves room. Deferred.
- R-M4 Via-in-pad only on IC pins and only when the fab notes say filled; never on passives
  (exists at fab; becomes a router rule so it never happens). **R**.
- R-M5 Thermal reliefs on through-hole pads to pours, solid on SMD (exists via the zone).

### 4.9 Thermal and EMC

- R-T1 Thermal via arrays under exposed pads (`thermal=` on a part): a grid of vias at the
  stackup's hole-to-hole. **L, R**.
- R-E1 Plane edges stitched every 5 mm when two ground pours face each other across layers.
  **R** (pattern). Deferred until a board needs it.

## 5. The language

`NetReq` stays the unit of intent. Presets expand into constraint sets; overrides are numbers;
coordinates never. Groups relate nets.

```python
NetReq("USB_DP", "USB_DN", kind="usb_hs")                 # 90 ohm, pair, skew 0.5, uncoupled 2, no stubs
NetReq("SCK", "MOSI", "MISO", kind="spi", clock="SCK")    # match to the clock, 3W, max length
NetReq("SDA", "SCL", kind="i2c", pf_max=400)              # length from capacitance per mm
NetReq("VBUS", kind="power", volts=5, amps=1)             # width, vias in parallel, clearance row
NetReq("HV+", "HV-", kind="power", volts=300)             # clearance and creepage rows
NetReq("SW", kind="switch_node")                          # one layer, no vias, hot loop budget
NetReq("AIN0", "AIN1", kind="analog", keep_clear_of="SW", keep_clear_mm=3)
Pair("USB_DP", "USB_DN", z_diff_ohm=90, match_mm=0.5)     # explicit form of the preset
Bus("D0", "D1", "D2", "D3", match_mm=1.0, clock="CLK")    # routed as a ribbon, matched
Chain("VDDA", "J2.1", "C4.1", "U1.12")                    # feed order: cap before pin
Isolation("primary", "secondary", volts=250, slot=True)   # a line in the board, both sides named
Guard("AIN0", stitch_mm=2.5)                              # ground guard around the net
```

Every preset's expansion is printed by `pcbc check --constraints`, one line per number with its
source: "USB_DP: 0.19 mm, gap 0.15 mm on F.Cu over In1.Cu, 90.4 ohm (IPC-2141 microstrip,
JLC04161H-3313)". The AI reads numbers it did not have to know.

## 6. Architecture

```
board.py ── language.py ──► model (NetReq, Pair, Bus, Chain, Isolation, Guard)
                               │
                       constraints.py  (presets → numbers from stackup.py; the report)
                               │
              ┌────────────────┼──────────────────────┐
        pcb_place.py      dru.py (KiCad rules)   route/ (pcbc-route)
        route-aware       + canary               patterns → maze core → tuning
        checks (P)        (K)                    (R)
              │                                      │
              └──────────► netcheck.py (gate: KiCad DRC + netlist + verify.py) ◄──┘
                               │
                          report: moves + copper bar (B); blocking analysis on failure
```

### 6.1 Data model (Python, `pcbc.model`)

- `Stackup`: code, layers, per-dielectric thickness and Dk, copper per layer, every limit.
- `Constraint` per net: width, clearance, via (size, drill, count per change, allowed),
  layers, reference layer, max length, group (pair / bus / chain), skew budget, keep-away,
  spacing multiple (3W), crosstalk class, current, voltage row.
- `Copper`: segments (start, end, width, layer, net, reason, locked), vias (at, size, drill,
  layers, net, reason), zones (from KiCad's fill).
- `Reason`: fanout, hop, chain, tap, spine, bus, signal, tuning, guard, stitch. Every item
  carries one; the bar and the blocking analysis speak in reasons.

### 6.2 Compile (`constraints.py`)

Pure. Presets to numbers with the stackup, IPC tables, impedance formulas. Emits the
per-net `Constraint`, the class table for the project file, the `.kicad_dru` rules (section
6.6), and the constraint report. Tests pin every number against a known answer (JLC's
calculator for impedance, IPC tables for clearance and width).

### 6.3 Route-aware placement (`pcb_place.py`)

Before any copper, as moves:
- airwire length against `max_mm` and skew budgets (R-L4);
- chain order: the pads of a chain lie in order along the path, no part between (R-X4);
- corridor test for wide tracks (R-I4): rasterise pads and courtyards on each layer, dilate by
  the class clearance, flood from pad to pad at the class width;
- decap and hot loop areas (R-D1, R-D2);
- keep-away distances (R-D3, R-A1);
- reference plane under a controlled-impedance net's parts (R-Z3): the plane's rule area
  must cover the pads;
- isolation line (R-V2).

### 6.4 The router (`route/`, a Rust crate `pcbc-route` with a JSON interface)

Called with the placed board's pads, outline, zones, keepouts, the `Constraint` table and the
copper already present; returns copper with reasons, or a failure with blocking analysis.
JSON in, JSON out, one process call per stage, no files of its own, nothing recorded anywhere.
Python owns the order and the stages; Rust owns geometry and search.

Stages, in the order the plan already runs:
1. **Fanout** (exists in Python; moves to Rust unchanged): closed rows, staggered vias.
2. **Patterns**: hops (straight, L, Z at 45 degrees), chains (feed through the cap), plane
   taps (via beside the pad at the stagger distance, on the pad's escape side), power spines
   (a trunk between the two farthest pads of a power net with taps off it), bus ribbons
   (parallel tracks at the bus pitch), guards and stitches. Each pattern either fits exactly or
   fails with the item in its way; no search.
3. **Maze**: what is left. Octilinear grid A* with exact-clearance dilation per class
   (Minkowski of each obstacle by width/2 + clearance, rasterised once per class), layer
   changes with via cost and the return-via requirement, negotiated congestion (PathFinder:
   first pass ignores overuse, later passes price shared cells and rip up the worst) with a
   fixed iteration cap, most constrained net first, deterministic tie-breaks (net order, then
   lexicographic direction). Cost terms: length, vias, parallel run beside another net at
   under 3W, off-preferred direction on a layer, crossing a plane split on a reference layer
   (forbidden, not priced, for controlled-impedance nets).
4. **Pairs**: routed as one path at pair width plus gap, split symmetrically, uncoupled only
   in the pad approach; skew measured; layer changes as via pairs with a return via.
5. **Tuning**: serpentines in the slack region for matched groups; fails as a move when the
   slack is not there (the placement has to give it).
6. **Cleanup**: merge collinear segments, replace stairs by 0/45/90 within the same cleared
   corridor, verify every change against the exact geometry check before keeping it.

Exact geometry, not grid geometry, is the check: segment-to-segment distance with widths,
rounded rectangles for pads, circles for vias and round pads, polygons for zones and
outlines; the grid is only the search space. A route is accepted only if the exact check
passes; the grid dilation is chosen so it always does (dilate by the ceiling of the true
distance in cells).

Grid: 0.05 mm on 2 layers, 0.1 mm on 4, as now; pads off-grid are entered by a short
pad-centre stub of the class width, straight, never a jog.

Performance target: node (31 parts, 18 nets, 4 layers) in under 60 s single-threaded, blinky
in under 2 s. `rstar` for spatial queries, `geo` for exact geometry, `i_overlay` for polygon
booleans (zone dilation), `pyo3` or a CLI: the CLI keeps the process boundary that made
determinism easy to prove.

### 6.5 Blocking analysis (on every failure, both engines)

For a failed pad or net: the corridor between the unreached pad and its nearest same-net
copper, on each allowed layer; every item in it (pads, tracks, vias, zones, keepouts) with
its net, reason and owner ref; sorted by the share of the corridor it blocks. Reported as:

```
GND: U1.4 could not leave its row. In the way: VSS via at (21.75, 16.40) [R-tap of C4.2],
REFN_F track y=17.3 [analog, locked]. Moves: Place("R10", toward="down") frees the lane;
NetReq("REFN_F", layers=["B.Cu"]) takes the analog copper off this side.
```

Today this was done by hand three times (dump a box, read the step files). It is mechanical.

### 6.6 KiCad rules (`dru.py`)

For every constraint that KiCad can check, pcbc writes the rule, so the arbiter judges what
the router obeyed: class clearances and widths (project classes), `creepage` by class pair,
`length (max)` per net, `skew (max)` per group, `via_count (max)` for no-via nets,
`diff_pair_gap` / `diff_pair_uncoupled` per pair, `disallow` in rule areas (isolation,
fiducial masks, keep-aways), `track_segment_length (min 0.2mm)` and `track_angle (min
135deg)` for geometry, `hole_to_hole` as an error, and the canary. Severities: everything
pcbc writes is an error.

### 6.7 Verification beyond KiCad (`verify.py`)

Plane-split crossing (from the filled zone polygons the gate already saves), return vias at
reference changes, vias per amp, loop areas, chain order, parallel-run length, serpentine
geometry, the copper bar. Each finding is a move or a `style:` note; the tests hold the
examples to the bar.

### 6.8 The copper bar (`report`)

Per net: routed length over airwire (detour ratio), vias, off-45 segments, segments under
0.2 mm, tightest clearance margin, parallel run at under 3W, skew for groups, loop area for
decaps. Per board: totals and the worst net. The examples must stay at their recorded bar;
a change that worsens it fails the tests, as with schematic readability.

## 7. Evaluation and the bake-off

Boards: blinky, buck, c3_usb, node, DS2 Addon, plus a sixth built for this: a 4-layer board
with USB 2.0 through an ESD diode into an MCU, an SPI flash at 40 MHz, an I2C sensor, a 1 A
switching regulator, a bridge-input ADC with reference filters, and a 250 V isolation line.
Every requirement in section 4 has at least one net on it.

Metrics, per engine, per board: gate pass; unrouted; KiCad errors; the copper bar; wall time;
determinism (build twice, byte-diff); flags and workarounds needed. Engines: KRT as it is
today; Freerouting through `pcbnew.ExportSpecctraDSN` / `ImportSpecctraSES` (kicad-cli has no
DSN export; the Python API does); tscircuit's autorouter through its SimpleRouteJson (MIT,
pairs and buses with skew, determinism unstated); pcbc's own as it comes up. The bake-off is
not a gate for building pcbc-route; it is the evidence for when KRT can be removed.

## 8. Phases

Effort is one person, part time, with the AI doing the typing; each phase ends green on the
examples and with its rows in the README tables.

**R0. The bar and the analysis (3 to 4 days).** Copper bar in the report and tests;
`track_segment_length` and `track_angle` rules with the canary; blocking analysis on every
failure from KRT; `Reason` on copper pcbc writes. Pays off immediately with KRT still routing.

**R1. Constraints and rules (1 week).** `constraints.py` with the presets in section 5, real
JLC dielectrics, stripline and coplanar formulas calibrated, IPC-2152, the voltage rows;
`dru.py` writing every rule KiCad can check; `pcbc check --constraints`; route-aware
placement checks (section 6.3). Tests pin the numbers.

**R2. Patterns (1 to 2 weeks).** Hops, chains, taps, spines, buses as patterns in Python with
exact geometry checks; KRT routes only what is left; measure the leftover on the six boards.
Expect the leftover to be the long signals, under a quarter of the copper.

**R3. The maze core (4 to 6 weeks).** `pcbc-route` in Rust: exact geometry, obstacle
dilation, octilinear A*, PathFinder negotiation, via rules, cleanup, blocking analysis native.
Replace KRT stage by stage, board by board, holding the bar. KRT stays selectable until the
six boards pass without it.

**R4. High speed (3 to 4 weeks).** Pairs, return vias, plane-split verification, skew tuning
with serpentines, 3W and orthogonality costs, the sixth board green.

**R5. Removal (1 week).** The bake-off numbers in `docs/`, KRT out of the default plan, the
skill and README rewritten for the new language.

## 9. Risks and open questions

- **Exact geometry versus KiCad's.** KiCad's clearance uses the true pad outlines
  (roundrect corners, custom shapes). The router must use the same shapes or a strict
  superset. A conformance test: every routed board through `kicad-cli pcb drc` must be clean
  by construction; any error is a router bug, never a board edit.
- **Performance.** PathFinder on a 1200 x 900 x 4 grid is fine in Rust; the risk is the exact
  check on every candidate. Check once per accepted path, not per expansion.
- **Impedance accuracy.** Closed-form microstrip is within a few percent; stripline and
  coplanar less so. Calibrate against JLC's calculator and state the formula in the report;
  the 15 % tolerance of USB 2.0 leaves room, 100 ohm Ethernet at 10 % less.
- **Pours.** KiCad fills them; the router must model the zone's clearance and its thermal
  spokes when a track passes near a tapped pad. The gate refills; a fill that retreats from a
  via and isolates it was seen once already.
- **Back-side placement.** Not in the placer yet; the router should not assume F.Cu for SMD.
- **Scope creep.** The sixth board defines "done" for R4; anything a real board does not need
  waits for the real board that needs it.

## Sources

- KiCad custom rules reference (constraint types and functions, older snapshot; 9.0 added
  `creepage`, `track_angle`, `track_segment_length`, verified live in section 2):
  https://gitlab.com/kicad/code/kicad/-/blob/f75c72ebb547987ec856d8e9b58561dcf2f99db5/pcbnew/dialogs/panel_setup_rules_help.md
- KiCad 9.0 release notes (creepage): https://www.kicad.org/blog/2025/02/Version-9.0.0-Released/
- JLCPCB capabilities: https://jlcpcb.com/capabilities/pcb-capabilities
- JLCPCB impedance stackups: https://jlcpcb.com/impedance
- IPC-2152 versus IPC-2221: https://resources.altium.com/p/using-ipc-2152-calculator-designing-standards ,
  https://pcbsync.com/ipc-2152/
- USB 2.0 layout numbers: https://resources.altium.com/p/routing-requirements-usb-20-2-layer-pcb ,
  https://www.ti.com/content/dam/videos/external-videos/en-us/4/3816841626001/6087491555001.mp4/subassets/usb_layout_basics.pdf ,
  https://embeddedhardwaredesign.com/usb2-0-pcb-layout-guidelines/
- 3W rule, plane splits, stitching: https://www.protoexpress.com/blog/crosstalk-high-speed-pcb-design/ ,
  https://www.flux.ai/p/blog/high-speed-pcb-design-layout-rules
- PathFinder negotiated congestion, as applied to PCB routing:
  https://engineering.flexcompute.com/articles/electrical-routing-agents/ ,
  https://github.com/bbenchoff/OrthoRoute
- tscircuit autorouter (MIT, SimpleRouteJson): https://github.com/tscircuit/tscircuit-autorouter
- Freerouting (DSN/SES, 45 degree maze, push and shove): https://freerouting.org/freerouting/manual/routing-options
- Rust geometry: https://docs.rs/geo/latest/geo/ , https://github.com/georust/rstar , https://lib.rs/crates/i_overlay
