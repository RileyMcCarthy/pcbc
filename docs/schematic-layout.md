# Schematic layout: auto-layout, `schematic.core.py`, `schematic.gen.py`

Written 2026-10-03, revised the same day after three reviews (revision summary at the end). The
schematic follows the PCB decision in `docs/direction.md` §2: `board.py` is the circuit and its physics
only; layout never lives in `board.py`, and the vocabulary that put it there is removed, not deprecated.

The owner's words, which fixed this: *"lets make another workflow for the schematic change too, This way
it can auto layout then we can clean it up with regions and other nice to haves for visualization?"*

What this means:

- **The tool lays out the sheet from the netlist alone.** With no schematic input at all, `pcbc sch` and
  `pcbc build` draw a sheet that passes the hard readability numbers in §2.6. Every pose and every other
  choice the layout makes is written to `schematic.gen.py`, which is build output and is never edited.
- **The AI improves the sheet with locks in `schematic.core.py`** beside `board.py`: regions with titles,
  which parts go in each region, region order, explicit poses, attachments, label and text placement and
  page size. The auto-layout places everything that is not locked, working around the locks.
- **`SchPlace`, `SchRegion` and `SchStyle` in `board.py` are refused**, and so is `Net(wire_mm=)`. Each
  refusal is a fact that names `schematic.core.py`.

Grok's schematic work stays and is the base for all of this: `sch_place.py` (the attach, along and below
solvers, the `_separate` push loop and `_pack_groups`), `sch_emit.py` (wires only where a clean path
exists, labels and power symbols, the lint, the region boxes fitted around their members, row alignment,
and the 4-pass loop that lengthens a wire when a net name does not fit), and `sch_bench.py` with
`vibes.images.json` (the current, new and difference pictures that Vibes posts on a pull request). The
auto-layout produces the same `SchPlaceSpec`s that a person writes by hand today, and the solvers that
exist now resolve them.

This plan is coordinated with `docs/board-outline.md` (the PCB move of `Place`, `Region`, `Keepout` and
the outline into `layout.core.py`). Both change `language.load_board` and the same example import lines;
§8 says how they meet.

---

## 1. Files

| File | Who writes it | What it holds |
|---|---|---|
| `board.py` | the AI | The circuit and its physics. No schematic geometry. |
| `schematic.core.py` (beside `board.py`) | the AI | Schematic locks. Optional and partial: with no file, the sheet is pure auto-layout. |
| `layout/<name>/schematic.gen.py` (beside `schematic.kicad_sch`) | the sch stage, every build | One line per object the layout resolved, in the gen form of §3.1. Never edited, never read back. |

Why these names and places:

1. **They follow the PCB files.** The PCB pair is `layout.core.py` beside `board.py` and `layout.gen.py`
   under `layout/<name>/`. `direction.md` §2 keeps gen out of the source tree so it never lands in the
   owner's board checkout.
2. **The `schematic.` prefix matches what the sch stage already writes.** `build.sch_stage` writes
   `layout/<name>/schematic.kicad_sch` and stamps it in `schematic.inputs.json`. The gen file sits next to
   the sheet it describes, and its sha is added to that stamp.
3. **It is a separate file from `layout.core.py` because a different stage reads it.** Today the sch stamp
   (`build._stamp_sch`) hashes `board.py`, the part files and the modules; the route stamp
   (`build._inputs`) hashes `layout.core.py`. Keeping the two core files apart is meant to give:
   - tidying the schematic never makes the routed board stale;
   - locking copper never redraws the sheet.

   **Neither holds with today's stage ladder**, and PR 1 owns the change (§1.1).
4. **Each file has its own vocabulary, so a line in the wrong file is refused by name** (§1.2):
   - a PCB `Place` in `schematic.core.py` is refused;
   - a `SchPlace` in `layout.core.py` is refused;
   - either one in `board.py` is refused.

**Amendment needed in `direction.md`.** §2 says "exactly two" sources of truth. That becomes three:
`board.py` and the two core files. The rules under §2 apply unchanged (never edited; never read back;
every file a stage reads is stamped; one writer; one home per fact). A row for `schematic.core.py` and
`schematic.gen.py` is added to the table in §1 of `direction.md`.

### 1.1 Loading and staleness (PR 1)

**One loader for every caller.** There are 12 calls to `load_board` in `src/` (cli.py 4, build.py 3,
sch_bench.py, `language.check_board`, review.py, route_native.py, layout_job), counted with
`grep -rn "load_board(" src | wc -l` on the frozen copy. `load_board(path)` runs, in order:

1. `board.py`, under `tracing("load")`, as today;
2. `layout.core.py`, under the same trace (`docs/board-outline.md` A.2);
3. **`schematic.core.py`, after the load trace has closed**, under its own `tracing("sch-core")`.

Its sha and its trace go into `design.sch_core` (a new field), which only `_stamp_sch` records. So
editing `schematic.core.py` never enters `design.reads`, and the place, route and fab stamps never see it
(`_load_drift` checks `design.reads` for every stage; the build review found that loading core inside the
load trace would make the route stale a second way).

`sch_bench.render_sheet` and the bench test take `load_board(path, schematic_core=False)` for the
`<board>.auto` pictures (§7). No other caller may skip the schematic core.

**The sch stage becomes a side branch of the ladder.** Today `STAGES = ("check","sch","place","route","fab")`
(build.py:19), and `stale_reason` returns `(None, …)` on any sch-stamp mismatch (build.py:886–896), so
`planned(None)` reruns every stage from check. PR 1 splits the sch check in two:

| What changed | Stages rerun |
|---|---|
| `board.py`, a module it imports or a part file | all, as today |
| `schematic.core.py`, `schematic.kicad_sch` or `schematic.gen.py` (edited or deleted) | sch only; place, route and fab stay current |
| `layout.core.py` | place, route, fab as today; sch stays current |

Test (PR 1): build node; edit a `SchPlace` in `schematic.core.py`; rebuild; the routed board's sha and
`layout/node/inputs.json` are unchanged and only `schematic.kicad_sch` is rewritten. And the converse:
editing `layout.core.py` leaves `schematic.kicad_sch` byte-identical.

### 1.2 The three vocabularies and how a misplaced line is refused

The mechanism is the one `docs/board-outline.md` A.3 uses: **each file is executed, then the design is
inspected**, so a refusal catches every import path (`from pcbc import SchPlace`, `import pcbc.language as
L`). `pcbc` keeps exporting `SchPlace`, `SchRegion`, `SchSheet`, `SchNet`, `SchText`, `SchLabel` and
`SchPower`; they record into the design, and the loader decides whether the file may hold them.

| Loader | Snapshot diff before/after the file | Result for a schematic object |
|---|---|---|
| board loader | after `board.py` runs, any of `design.sch_places`, `sch_regions`, `sch_sheet`, `sch_nets`, `sch_texts`, `sch_labels` is non-empty | refused, one fact per line (§5), naming `schematic.core.py` |
| PCB core loader (`load_layout`) | `_BOARD_FIELDS` **keeps** every `sch_*` field, so a schematic object written in `layout.core.py` changes the snapshot | refused: `layout.core.py:N: SchPlace("R1") is schematic layout; it lives in schematic.core.py beside board.py` |
| schematic core loader | every `Design` field **except** the `sch_*` fields | a board or PCB object written in `schematic.core.py` is refused naming the field and `board.py` or `layout.core.py` |

This replaces the earlier sentence "`_BOARD_NAMES` loses these names": `_BOARD_NAMES` (language.py:2896)
is used only by `load_layout` (language.py:2949) to stub board names inside a layout file, and dropping
`SchPlace` from it would turn today's named refusal into `NameError: name 'SchPlace' is not defined`. Its
stub for the `Sch*` names now names `schematic.core.py` instead of `board.py`.

`SchPlaceSpec` and `RegionSpec` gain `line` and `source` (as `PlaceSpec` has), so every refusal names its
line. The schematic core namespace injects the `Sch*` names, so a core file needs no import; an import
written by habit (`from pcbc import SchPlace`) imports the same function and is not refused.

`SchNet` writes `design.sch_nets` (new), never `Net.wire_mm`: `nets` is in the snapshot, so writing the net
would trip "changed board.py's part (nets)". `Net.wire_mm` is deleted in PR 1.

## 2. Auto-layout with no core

### 2.1 What it reads

The auto-layout reads only these inputs:

- `board.py`'s parts, pins and nets, with each net's kind from `Power`, `Ground` or `Net`;
- each symbol's pin geometry from its `.kicad_sym`: which side of the body each pin is drawn on;
- the station order of `Path()` lines, a physics fact that names which pads are adjacent.

It does not read the PCB core, the PCB gen or any build output.

**It does not read pin electrical types.** 22 of the 23 symbol files the five boards use type every pin
`unspecified`; the exception is LMV321 (2 `input`, 1 `output`), and no symbol has a `power_out` pin
(`grep -oE '\(pin (power_in|power_out|input|output|passive|bidirectional|unspecified)' <sym>`, both
reviews). `sch_emit.PinDef` carries no type either. A rule keyed on types would never fire.

### 2.2 Clusters and regions

These rules are proposed and measured (`regions.py v2`, commands in §2.8). The earlier `naive` and
`refined` rules stay in the script for comparison.

**Anchors.** A symbol with more than two pins is an anchor. A connector (ref `J*` or `P*`) that carries a
signal net is an anchor whatever its pin count. Two-pin switches are not anchors (treating them as
anchors split c3_usb into 5 regions against 3 by hand).

**Passive units.** Two-pin parts joined to each other by signal nets form one unit, and a unit joins one
anchor as a whole. Its candidates are the anchors on any of its signal nets: a connector wins, else the
anchor with the fewest pins (the part the passives serve), ties in `board.py` order.

- DS2's RC filters (`R6` with `C7`, `C8` between `J2` and `U1`) stay together, with their connector.
  The earlier breadth-first rule gave `R6` to `J2` and `C7`, `C8` to `U1` and cut every filter across two
  dashed boxes (review-readability M4).
- node's I2C pull-ups go with the sensor (`U4`, 8 pins), not the MCU; the gate pair `R_G`, `R_PD` goes
  with `Q1`.

**Decoupling.** A two-pin part whose pins are all power or ground has nothing in the netlist that says
which IC it serves.

1. A cap that a `Path()` names next to an anchor's pad goes with that anchor; an IC beats a connector
   (DS2's `Path("VDDA", "J2.1", "C4.1", "U1.12")` puts `C4` with `U1`).
2. The other caps form **one bank per set of rails** and the bank joins as a whole: the rail's regulator
   (an IC on this rail and on another non-ground rail), else the IC on the rail with most pins, else an
   anchor on the rail, else the region of the first placed part the rail reaches. A bulk cap and its HF
   partner are never split (the round-robin rule split them every time: review-readability M3).
3. The netlist cannot say that `C_MCU` decouples the MCU. A board that wants it drawn there states the
   adjacency with a `Path()`, the physics the PCB placement needs as well. node and c3_usb have no
   decoupling `Path()` today, so their MCU caps land in the 3.3 V bank (measured below).

The regulator test also flags DS2's `U1`, an ADC with AVDD and DVDD pins. That is harmless here because
`U1` is a hub (below), and hubs are never moved to the power row.

**Power-only connectors.** A connector with only power and ground nets (buck's `J_IN`, `J_OUT`) is not a
region: it joins the region of the first part its rail reaches. The earlier rule made each a region and put
them in a power row above their own IC (review-readability M5).

**Regions.** Start from the connected components of the anchor graph, where two anchors are joined when
they share a signal net. A component with more than two anchors is split by cutting out its anchor with
the most signal neighbours (ties go to the most pins) until no component has more than two. Each cut
anchor is a region on its own: the hub.

**Measured** (`regions.py <board> {naive|refined|v2}`; seed-independent, checked under
`PYTHONHASHSEED=1` and unset). "Vote" is the old score: each inferred region takes the hand region most of
its members are in, so splitting one hand region into many costs nothing. "Pairwise" scores pairs of parts
that share a region: precision = shared in both / shared in inferred, recall = shared in both / shared by
hand.

| Board | Rule | Regions (hand) | Vote | Pairwise precision | Pairwise recall | In no region |
|---|---|---|---|---|---|---|
| buck | naive | 1 (1) | 12/12 | 1.00 | 1.00 | |
| buck | refined | 3 (1) | 11/12 | 1.00 | 0.35 | C_OUT2 |
| buck | **v2** | **1 (1)** | **12/12** | **1.00** | **1.00** | |
| c3_usb | naive | 2 (3) | 11/19 | 0.39 | 0.64 | |
| c3_usb | refined | 3 (3) | 16/19 | 0.74 | 0.74 | |
| c3_usb | **v2** | **3 (3)** | **17/19** | **0.82** | **0.74** | |
| node | naive | 6 (6) | 21/31 | 0.46 | 0.57 | |
| node | refined | 6 (6) | 21/31 | 0.43 | 0.56 | |
| node | **v2** | **6 (6)** | **27/31** | **0.70** | **0.72** | |
| DS2 | naive | 4 (3) | 22/28 | 0.61 | 0.66 | |
| DS2 | refined | 6 (3) | 22/28 | 0.56 | 0.30 | |
| DS2 | **v2** | **6 (3)** | **27/28** | **0.95** | **0.32** | |

What the v2 misses are:

- **c3_usb (2) and node (4)** are all decoupling caps with no `Path()`: `C_MCU`, `C_MCU_HF` (both boards),
  `C_BME`, `C_OPA` (node). They sit in the 3.3 V bank under `U2`, together.
- **DS2's recall of 0.32** is grouping, not membership: v2 makes `J2`, `J5`, `J4` (each with its filters)
  three regions where the owner draws one "Inputs" region, and `J1`, `J3` two where he draws "EdgeBoard".
  Nothing in the netlist says those connectors belong together; that is what a core
  `SchRegion(members=…)` line is for, and it is the clean-up step the owner described. Its one vote miss
  is `J1`, whose series resistors `R3`–`R5` the hand sheet draws beside the ADC.

**Titles.** An inferred region is titled with its anchors' refs and values, for example
`U2 AP2112K-3.3TRG1`, and named `auto-<first anchor ref>` (`auto-U2`). A declared region name may not
start with `auto-`, so a region name never reads as a part ref and never collides with an inferred one.

**Outlines.**

- Each region is outlined by the existing `_group_graphics`: a dashed box fitted around everything drawn
  for its members. Rows are evened out by `_align_region_boxes`.
- A sheet with one region draws no outline.

**A pasted inferred region is the inferred region (2026-10-09).** A declared region named `auto-…` whose members are
the members the auto-layout infers for the region of that name *is* that inferred region: its anchors, hub flag,
regulator flag and grid slot are the inference's, and its title, outline and origin are the line's. The inference
always runs over every part outside the *other* declared regions, so declaring an inferred region never changes how
the rest are inferred (refute:lock 2026-10-09: four of DS2's six inferred region lines pasted together left two
regions to be inferred over what remained; with two anchors left the hub cut no longer split them, they merged, and
five parts went off the paper). An `auto-…` line whose members differ is an ordinary declared region (closed
membership), and the inference is run again without its members until the set is stable.

### 2.3 Inside a region

The region's anchor is placed at the region origin (`_apply_origin`, defined in §3.2). Each other member
gets a synthesised `SchPlaceSpec`, in the same form a person writes by hand:

| Case | Synthesised spec |
|---|---|
| Passive on an anchor pin | `to="<anchor>.<pin>"` (the pin it shares a net with) |
| Second part on the same node | `along="<first>.<pin>", side="bottom"` (node's `C_3V3_HF` form) |
| Next part in a unit | `to="<previous>.<far pin>"` (DS2's `C7` to `R6.2`) |
| First cap of a bank | `to=` the anchor's pin on that rail |
| Next cap of a bank | `along="<previous cap>.<rail pin>"`, so the bank stays one row |
| Second connector in a region | `below="<first connector>"` |

Because units and banks are never split, a synthesised attach never crosses a region boundary. Hangers
are ordered rigid first (`_rigidity`), as today, and resolved by `_apply_attach`, `_apply_along` and
`_apply_below`. The pose rules already in `_apply_attach` put power at the top and ground at the bottom.
`wrong_way_up` (§2.6) counts the cases where they lose.

### 2.4 Order on the sheet

The auto-layout orders regions to keep wires short and labels few. **It does not claim signal flow.**
Which side is "input" cannot be read from these symbols (§2.1), and the region graphs of the example
boards are stars: every non-hub region on DS2 and node is one hop from the hub (`regions.py v2` prints
each one as "beside hub"), so graph distance from the connectors gives no order either. Reading order is a
judgement, and the AI locks it in core with `SchRegion(row=, col=)`.

**Grid.** Regions sit on a grid of rows and columns:

1. **Row 0** holds regulator regions (§2.2) that are not hubs. Every other region is in row 1. With the
   power-only connectors joined to their rail (§2.2), buck is one region in one row, as the hand sheet is.
2. **The hub** is in the middle column of row 1. With two or more hubs, they are ordered left to right by
   the number of signal nets they share, most-connected in the middle.
3. **A region beside a hub** goes to the side of the hub on which most of the hub pins it reaches are
   drawn (pin `x < 0` in the library is the left side). **A tie** (the region reaches as many pins on each
   side, for example node's USB region on `IO18` left and `IO19` right) goes left.
4. **Within a column**, regions are ordered top to bottom by the mean height of the hub pins their nets
   reach, so a region sits level with the pins it talks to.

Measured against the hand sheets (`regions.py v2`, the "beside hub" lines):

| Board | Region | Hub pins left / right | Rule | Hand sheet |
|---|---|---|---|---|
| node | J1 + U3 | 1 / 1 | left (tie) | left |
| node | U5 | 1 / 0 | left | left |
| node | Q1 + J_LOAD | 1 / 0 | left | left |
| node | U4 | 2 / 0 | left | left |
| DS2 | J5 | 2 / 0 | left | left |
| DS2 | J1 | 0 / 3 | right | right |
| DS2 | J4 | 1 / 1 | left (tie) | left |
| DS2 | J3 | 2 / 0 | left | right |
| DS2 | J2 | 0 / 2 | right | left |

The rule agrees with the hand sheet on 7 of 9. DS2's `J2` and `J3` are the owner reading the sheet as
Inputs → ADC → Edge, against the symbol's pin sides; a core `col=` line says so.

**Connectors face inward.** A connector in a column left of its hub is mirrored `y` so its pins point
right, which is what DS2's hand lines do.

**Ground.** Ground symbols point down. A ground region is never formed, because ground is never a signal
net. `upside_down_power` (§2.6) counts power symbols that do not point the right way.

### 2.5 Overlaps and crossings: the push-pull loop

**Push.**

- Inside a region, `_separate` runs: overlapping keepouts (the body plus the power symbols it will need)
  move apart along their attach axis, and the part that needs the smaller shove moves.
- Between regions, region frames (§3.2) are packed on the grid with today's frame gutter, `_PACK_GUTTER`
  = 25.4 mm. (The drawn boxes end up closer, because they are fitted inside the frames.)
- A part with a pose lock (§3.3) is never moved.

**Pull (new).** After the push:

- each hanger slides back toward its target, one 1.27 mm step at a time, while it keeps 2.54 mm between
  bodies and 0.5 mm between texts (an overlap-only test would undo the push's clearance: the hand sheets
  already have 3–8 body pairs closer than 2.54 mm);
- each region frame slides toward the hub along its row or column, the same way.

**Wires.** `_annotate` draws a wire only where a Manhattan path exists that hits no symbol and crosses no
other net's wire (`segment_ok`). Every other link is named with a label or a power symbol. So crossings and
wire-through-symbol are zero by construction, and a poor layout shows up as more labels, longer label
hunts and text with nowhere to go. That is why §2.6 scores those.

**Loop.**

- Today's 4-pass loop stays: when a net name does not fit on its wire, the downstream part slides 2.54 mm,
  up to 5.08 mm. It moves attach-locked parts along their axis and never a pose-locked part (§3.3).
- The pass count goes up to 8.
- The loop keeps the best pass by the order in §2.6. **Ties keep the later pass**, and the loop stops at
  the first pass that is worse than the one before, both as today (`best` uses `<=`).

**Determinism.** The output may not depend on `PYTHONHASHSEED`. Today the packer does
(`_passive_chains` iterates a `set` of refs, sch_emit.py:595 and :600). PR 2 sorts every set iteration in
`sch_emit`, `sch_place` and the new layout code, and the bench and fixed-point tests run under two seeds.
With the two lines sorted, all five packer sheets are byte-identical under seeds 0, 1 and 2 (`cmp` on the
`.kicad_sch`; §2.8).

### 2.6 What "readable" means

"Readable" is a vector. Every sheet is scored by `sch_score.py` (PR 2, from `measure.py`). The order
below is lexicographic, so the loop needs no invented weights.

**Generator invariants (asserted, not scored).** `crossings` and `wire_through_symbol` cannot be non-zero,
because `segment_ok` refuses those wires. They are asserted on every sheet with an exact segment-box test
(`measure.py`'s `_seg_through_box` samples only t = 0.35, 0.5 and 0.65, and misses a wire through a small
body near its end). A failure is a generator bug, not a layout score. Netlist and ERC stay as today:
`kicad-cli` reads exactly `board.py`'s netlist and ERC is clean.

**Hard: must be 0.**

| Number | What it counts |
|---|---|
| `body_overlaps` | pairs of symbol bodies (`body_aabb`) with positive overlap area |
| `text_collisions` | pairs in which a label, power symbol, Reference or Value overlaps anything else by at least 0.6 mm² (the lint's threshold), **with every text box taken from KiCad's SVG of the emitted sheet** (below) |
| `wire_through_text` | a wire of another net through a label, Reference, Value, pin text or power-symbol graphic (today a lint line only, e.g. node's `wire CC2 runs through VBUS symbol at J1`) |
| `parts_off_paper` | part bodies outside the paper frame, or inside the title block |
| `region_frames` | region boxes that overlap each other, and non-member bodies inside a region box |
| `wire_over_pin` | a wire that crosses, runs along or ends on a pin's drawn line anywhere but the pin's electrical end (added 2026-10-08: node's LED wire across SW_RST pin 2 read as LED tied to the switch and GND, and no number above saw it; the router refuses such a wire for every part, box or two-pin) |
| `wire_overlaps` | two wires on one stretch (collinear, sharing more than a point), and a junction dot where fewer than three ways meet (wires leaving the point, a pin ending there) (added 2026-10-09: a power symbol's or PWR_FLAG's stub was laid along its pin's own bus bar on c3_usb and DS2, which drew a dot on the bare pin end; the router refuses a wire along its own net's wire, and a symbol's stub starts where that wire turns away) |

**Soft: reported, and ratcheted per board in the benchmark (§7).**

| Number | What it counts |
|---|---|
| `lint_issues` | today's readability warnings, minus the library facts below |
| `labels` | net links joined by name instead of a wire |
| `label_partner_mm` | the largest distance between two labels of one net (how far a reader hunts) |
| `power_symbols` | power symbols placed |
| `wrong_way_up` | parts the lint reports as standing the wrong way up |
| `upside_down_power` | power symbols the lint notes as pointing the wrong way (today in `notes`, uncounted) |
| `text_crowding` | text pairs closer than 0.25 mm that do not reach the collision threshold ("VBUS" + "A5" reads "VBUSA5" on node's J1) |
| `ambiguous_names` | parts whose Reference is nearer (by 0.5 mm) to another part's body than to their own (buck: `R_EN` sits on `R_FB_TOP`'s column), or whose Reference or Value has a wire between it and its body (added 2026-10-09: DS2's `R4` under the `ADC_DRDY` wire read "R4 47R \| R5 47R"; `sch_emit.cut_off`). One per part. A two-pin part's name is placed clear of such a wire when a clean spot exists, and one with none is a lint fact the loop repairs by pushing the part and the wire's owner |
| `min_gap_mm`, `tight_pairs` | the smallest gap between bodies, and the pairs closer than 2.54 mm (compared with a 0.001 mm tolerance, printed to 3 dp) |
| `extent_mm`, `fill_pct` | the width and height of everything drawn, and body area as a percentage of it |
| `wire_mm` | total wire length |

**Library facts are not scored.** "lengthen the pins or hide numbers" (USB-C pin numbers like `A1B12`) is a
fact about the symbol file that no layout can change, so it stays a report line but is not counted in
`lint_issues`; otherwise it would hold every USB-C sheet off parity forever.

**Text boxes come from KiCad.** pcbc's own text model is wrong for rotated and mirrored box symbols:
on node's hand sheet the model puts `J_LOAD`'s Reference and Value right of the body, and KiCad's SVG draws
both across it (review-readability B3). Model boxes against KiCad's SVG: References outside the model box
buck 0/12, c3_usb 0/19, node 1/31, DS2 2/28 (`$S/review/field_fidelity.py`). So:

- the scored `text_collisions` of the kept sheet is computed on KiCad's SVG export, once per build (0.39–
  0.42 s for node over three runs: `kicad-cli sch export svg`);
- inside the loop, the model ranks passes;
- PR 2 fixes the field model for rotated and mirrored box symbols and adds a fidelity test: every
  Reference and Value on every bench board within 0.5 mm of KiCad's SVG.

**Paper.** `_emit` picks the paper by width only after A4 (`"A3" if max_x < 400`, sch_emit.py:2615), so
node's packer sheet is A3 with 24 of 31 bodies below the paper edge. PR 2 picks the paper by width and
height, and `parts_off_paper` is hard.

**Order.** Sheets compare first on the sum of the hard numbers, then on `lint_issues`, then `labels`,
then `label_partner_mm`, then extent area.

**Where the numbers appear.** `pcbc sch` prints the vector. `schematic.gen.py` writes it in its header.
`build`'s sch step carries it in its JSON.

### 2.7 Baseline: today's tool with every SchPlace and SchRegion removed (frozen copy, 2026-10-03)

**With the lines removed, today's tool refuses all five boards.** `sch_place.apply_sch_places` raises
`no SchPlace() for R1, D1` (blinky), and the same for buck's 12 parts, c3_usb's 19, node's 31 and DS2's
28. `circuit.check_design` (circuit.py:104, reached through `language.check_board`) refuses the same
boards earlier: `"<ref>: no SchPlace() — schematic pose is CSS, not auto-layout"`.

**The only no-input layout in the tree is unused.** It is `sch_emit._layout`, a column packer for passive
chains. Nothing calls it: `grep -n "_layout(" src/pcbc/sch_emit.py` finds only its `def`. Below it is
scored next to today's hand-placed sheets, **with its set iteration sorted** (the two lines in §2.5), so
the rows reproduce: every row is identical under `PYTHONHASHSEED=0` and `1`, and the packer sheets also
under `2`. The first version of this table took the packer unsorted and could not be reproduced (both
reviews). Netlist "verified" and ERC "clean" on all ten sheets.

**Hard numbers** (and the two invariants, 0 on all ten sheets):

| Board | Sheet | Body overlap | Text collisions (model) | Wire through text | Off paper | Paper |
|---|---|---|---|---|---|---|
| blinky | hand | 0 | 0 | 0 | 0 | A4 |
| blinky | packer | 0 | 0 | 0 | 0 | A4 |
| buck | hand | 0 | 0 | 0 | 0 | A4 |
| buck | packer | 0 | 1 | 0 | 0 | A4 |
| c3_usb | hand | 0 | 0 | 1 | 0 | A3 |
| c3_usb | packer | 0 | 0 | 1 | 1 | A3 |
| node | hand | 0 | 0 | 1 | 0 | A3 |
| node | packer | 0 | 0 | 2 | 24 | A3 |
| DS2 | hand | 1 | 12 | 5 | 0 | A3 |
| DS2 | packer | 0 | 10 | 3 | 0 | A3 |

**Soft numbers:**

| Board | Sheet | Lint | Labels | Label partner (mm) | Power symbols | Wrong way up | Upside-down power | Text crowding | Ambiguous names | Extent (mm) | Fill % | Min gap (mm) | Pairs < 2.54 | Wire (mm) |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| blinky | hand | 0 | 1 | 0 | 4 | 0 | 0 | 0 | 0 | 25.3 × 27.4 | 6.1 | 5.08 | 0 | 20.3 |
| blinky | packer | 0 | 1 | 0 | 4 | 0 | 0 | 0 | 0 | 25.3 × 26.1 | 6.4 | 3.81 | 0 | 19.1 |
| buck | hand | 0 | 5 | 41.3 | 16 | 0 | 1 | 2 | 1 | 122.3 × 39.4 | 15.9 | 1.98 | 3 | 152.4 |
| buck | packer | 3 | 9 | 92.7 | 21 | 1 | 1 | 0 | 0 | 192.2 × 62.9 | 6.3 | 2.54 | 0 | 88.9 |
| c3_usb | hand | 2 | 16 | 268.3 | 32 | 0 | 2 | 11 | 2 | 281.6 × 90.9 | 14.1 | 1.21 | 4 | 309.9 |
| c3_usb | packer | 2 | 23 | 266.6 | 37 | 0 | 1 | 4 | 1 | 219.3 × 294.4 | 5.6 | 3.81 | 0 | 242.6 |
| node | hand | 2 | 28 | 268.3 | 50 | 0 | 6 | 12 | 2 | 282.1 × 183.6 | 9.4 | 1.21 | 6 | 476.3 |
| node | packer | 3 | 40 | 427.7 | 55 | 0 | 3 | 5 | 1 | 315.9 × 444.3 | 3.5 | 2.54 | 0 | 383.5 |
| DS2 | hand | 29 | 36 | 158.6 | 24 | 2 | 4 | 4 | 1 | 283.1 × 78.2 | 9.7 | overlap | 8 | 407.7 |
| DS2 | packer | 15 | 48 | 217.2 | 26 | 0 | 1 | 2 | 0 | 237.5 × 244.0 | 3.7 | 3.81 | 0 | 228.6 |

The hand rows are the first version's rows unchanged, except `tight_pairs` on c3_usb (5 → 4) and node
(7 → 6): the first version counted an on-grid 2.54 mm gap held as 2.5399… as tight.

**How to read the tables.**

- **The packer is connected but unreadable.** It stacks the symbols in one tall column. For node that is
  316 × 444 mm against 282 × 184 mm by hand, off the A3 paper, with 40 labels against 28 and the two labels
  of one net up to 428 mm apart.
- **No hand sheet passes the new hard set.** c3_usb and node each have one wire through text (CC2 through
  the VBUS symbol at J1); DS2 has 1 body overlap, 12 text collisions and 5 wires through text. Its lint
  begins `C4 overlaps C5 at every distance along its attach`.

**Targets for the auto sheet with no core (PR 4 gate),** per board, against the hand sheet:

- every hard number 0 (text collisions on KiCad's SVG);
- `labels` ≤ hand (1, 5, 16, 28, 36);
- `lint_issues` ≤ hand (0, 0, 2, 2, 29), library facts excluded;
- `label_partner_mm` ≤ hand (0, 41.3, 268.3, 268.3, 158.6);
- extent area ≤ 1.5 × hand. This replaces "fill ≥ 8 %", which blinky cannot reach (hand 6.1 %, packer
  6.4 %) and which allows buck twice its hand area (hand 15.9 %). The 1.5 is a proposal for the owner.

### 2.8 Commands for every number in §2

`S` is the scratch directory of the session that took them; `PCBC_SRC` points the scripts at the tool's
source; `PY` is `/Users/rileymccarthy/pcbc/.venv/bin/python`.

```
S=/private/tmp/claude-501/-Users-rileymccarthy-Documents-MaD/53e19ae4-b53e-47b5-bcd4-d03eaa58410c/scratchpad/schcore
rsync -a /Users/rileymccarthy/pcbc/ $S/frozen/ --exclude .venv --exclude layout     # tree at 9225fd3 + working tree
rsync -a <scratchpad>/review_now/ds2/ $S/ds2/ --exclude layout                        # DS2, migrated to Path()
cp -R $S/frozen/src $S/rev/src                                                        # then sort the two lines:
#   sch_emit.py:595  leaves = [r for r in sorted(remaining) if ...]
#   sch_emit.py:600  start = next(iter(leaves or sorted(remaining)))
$S/rev/run_all.sh > $S/rev/baseline.jsonl     # §2.7: for seed in 0 1, each board:
#   PYTHONHASHSEED=$seed PCBC_SRC=$S/frozen/src $PY docs/schematic-layout/measure.py current <board.py> <name> <out>
#   PYTHONHASHSEED=$seed PCBC_SRC=$S/rev/src    $PY docs/schematic-layout/measure.py packer  <board.py> <name> <out>
$S/rev/run_packer.sh                          # packer under seeds 0 1 2; then cmp the .kicad_sch files
PCBC_SRC=$S/frozen/src $PY docs/schematic-layout/regions.py <board.py> {naive|refined|v2}       # §2.2, §2.4
/opt/homebrew/bin/kicad-cli sch export svg -o <dir> $S/rev/current_s0/node.kicad_sch           # §2.6 timing
```

A re-taken frozen copy (`$S/frozen2`, 2026-10-03 later the same day) differs from `$S/frozen` in no file
under `src/pcbc/sch_*`, `language.py`, `model.py`, `build.py` or `examples/*/*.py` (`diff -rq`), so the
numbers stand.

What the modes mean:

- `current` is the hand sheet.
- `none` clears `design.sch_places` and `design.sch_regions` and runs today's emitter.
- `packer` does the same clearing and swaps `apply_sch_places` for `sch_emit._layout`.

`measure.py` scores the pass that `emit_from_design` kept (matched by part poses), then runs `kicad-cli`
through `check_schematic` and `check_erc`. Its docstring and comments define every number. `regions.py`'s
docstring defines the three inference rules and both scores.

## 3. The core vocabulary

`schematic.core.py` is executed into the loaded design (§1.1). It needs no imports because the names are
injected. Every object takes `reason=""`. **Every keyword not listed for an object is refused**, naming
the line and the keywords the object takes (today `SchPlace(**css)` passes leftovers to
`css.box_style_from_kwargs`, which accepts and drops `at`, `rot`, `locked`, `display`: measured by the
AI review on the frozen copy). No board in the tree uses one of those keywords today
(`grep -rhoE "SchPlace\([^)]*\b(rot|at|locked|display|layer)=" examples tests` finds nothing), so the
refusal lands in PR 1.

### 3.1 The whole vocabulary after PR 5

This table is the AI's complete reference. "Key" is what makes two lines the same fact; two lines with
one key are refused, naming both.

| Object | Keywords | Key | Gen form (what gen writes) |
|---|---|---|---|
| `SchSheet` | `paper=None` (`"A4"`, `"A3"`, `"A2"`; `None` = by extent, width and height), `wire_mm=25.4` | one per file | both, resolved |
| `SchNet(net)` | `wire_mm` (0 = labels only) | `net` | only nets whose limit differs from the sheet's |
| `SchRegion(name)` | `members` (required), `title=None`, one of `at=(x, y)` or `row=`/`col=`, `outline=True` | `name`; each part in at most one region | `name`, `title`, `members`, `at`, `outline`; the grid cell as a comment |
| `SchPlace(ref)` | at most one anchor: `to=` (+ `pin=`, `side=`, `gap=`), `along=` (+ `side=`, `align=`, `gap=`), `below=`, `at=(x, y)`, or none; and `rotate=`, `mirror=` | `ref` | `at`, `rotate`, `mirror`; the attachment that produced the pose as a comment |
| `SchText(ref, field)` | `at=(dx, dy)`, `rotate=0`, `justify=None`, `hide=False` | `(ref, field)` | all |
| `SchLabel(net, pin=)` | `side`, `stub` | `(net, pin)` | both |
| `SchPower(net, pin=)` | `side`, `stub` | `(net, pin)` | both |

`side` is `"left"`, `"right"`, `"top"` or `"bottom"` (CSS sense: top is the smaller sheet y). `rotate` is
0, 90, 180 or 270. `mirror` is `"x"` or `"y"`. `field` is `"Reference"` or `"Value"`. `along=` and
`align=` both take `"REF.PIN"`: `along` is what the part stacks beside, `align` is the pin whose row or
column the hanging pin lands on.

The sheet title is the layout name the build passes (`emit_schematic_file(design, sch, title=name)`,
build.py:724). It is not a `board.py` field and not a core keyword.

### 3.2 Frames: one origin per region

There is one point per region, its **origin**: the top-left corner of the region's frame, which is where
`_pack_groups` puts its cursor today (`_PACK_ORIGIN` 12.7 for the first frame; members start at
origin + `_PACK_PAD` 10.16 across and origin + `_PACK_HEADER` 6.35 + `_PACK_PAD` down).

- `SchRegion(at=(x, y))` locks the origin, in sheet mm.
- `SchPlace(at=(x, y))` locks the symbol origin (KiCad's `(at x y)`), relative to its region's origin. So
  a part's sheet position is region `at` + place `at`, and a locked part moves with its region.
- `SchPlace` with no anchor is the **region anchor**: its body's top-left sits at the first member slot
  (today's `_apply_origin`). A region has at most one; a second is refused, naming both lines. Measured:
  only DS2 uses the form today, once per region (`J2`, `U1`, `J1`), on all 16 boards with `SchPlace`
  lines that load standalone (`$S/rev/zero_anchor.py`; the DS2 baseline fixture and `thermal` need their
  test harness to load and were not checked).
- The dashed box drawn around the members is fitted after layout (`_group_graphics` adds 2.54 mm padding
  and a 5.08 mm header, and `_align_region_boxes` stretches a row to its tallest box). Its corner is not
  the origin, and its size is never a lock.
- `SchPlace(at=)` is allowed only for a member of a declared region; for any other part it is refused,
  naming the gen `SchRegion` line that holds the part. Copying a pose without its region would otherwise
  silently change its frame.

### 3.3 What a lock locks

| Line | Locks | Solved by the tool |
|---|---|---|
| `SchPlace(at=)` | the pose in its region | nothing; `_separate`, the pull and the 4-pass loop never move it |
| `SchPlace(to= / along= / below=)` | the relation: target pin, `side`, `pin`, `rotate`, `mirror` when given | the gap, along the attach axis; `_separate` and the 4-pass loop may slide it, as today. A line that states what the auto-layout writes for the part (gen's `# auto:` comment) is met and changes nothing: the part is placed by that spec in its turn (2026-10-09). A side the auto-layout leaves to the placer (`beside`) is met only when the placer gives the line's side on the kept sheet; otherwise the line is a lock |
| `SchPlace(ref)` with no anchor | the part is its region's anchor | the region's place |
| `SchRegion(members=)` | membership, and it is closed | everything about the members not locked by their own lines |
| `SchRegion(title=)` | the text | nothing else |
| `SchRegion(at=)` | the origin | nothing else |
| `SchRegion(row=, col=)` | the grid cell | the origin, packed around the cell |
| `SchText`, `SchLabel`, `SchPower` | that text or symbol | nothing else |

Each keyword locks only its own fact. Leaving a keyword out leaves that fact solved: a region declared
with `members` and `title` only stays on the §2.4 grid. **File order never places a region.**

**Membership is closed and has one home**, `SchRegion(members=)`. A part in no declared region's
`members` is placed in an inferred region; it never joins a declared one. A `SchPlace` whose `to=`,
`along=` or `below=` target is in another region is refused, naming both regions. `SchPlace` has no
`region=`: membership would then have two homes, and moving a part would be two edits (review-ai M1
proposed `region=` as the one home instead; `members=` is chosen because it is one line per region, which
is the owner's "grouping", and because gen then writes membership once, which the fixed-point test needs).

This is why "locked" means a pose and not every core line: today `_separate` moves only parts outside
`css_refs` (`{specs without to/along}`, sch_place.py:769), and its first pass moves attached parts on the
hand sheets (buck `J_IN` −5.08 mm; DS2 `C5` 25.4, `C7` 17.78, `C9` 30.48, `C12` 8.89, `C3` 6.35 mm;
`$S/rb/sep.py`). Freezing attaches would change the PR 1 pictures and add overlaps to DS2.

### 3.4 Labels and power symbols

`SchLabel(net, pin="R6.2", side=, stub=)` and `SchPower(net, pin="U1.3V3", side=, stub=)`:

- the pin must be on that net; `net` is checked against the pin, never free;
- `side` is the side of the pin the label or symbol stands on; `stub` is the length of wire, in mm on the
  1.27 mm grid, drawn from the pin end to it. There is no absolute position, so a label can never be
  stranded away from its pin when the part or its region moves;
- a `SchLabel` lock makes that pin's link a label even where a wire would fit. That is the AI's choice to
  make;
- `SchLabel` on a `Power` or `Ground` net is refused (those are drawn with power symbols), and so is
  `SchPower` on a signal net.

`SchText(ref, field, at=(dx, dy))` is relative to the symbol origin, in sheet axes (after the symbol's
rotation and mirror).

### 3.5 How locks combine with the auto-layout

1. Locks resolve first, in file order (an attach line equal to the auto-layout's own spec is no lock here: it is
   met, and its part is placed in the auto-layout's order, §3.3; placing it first moved the parts after it, refute:lock
   2026-10-09):
   1. pose locks and region anchors;
   2. `below=`;
   3. attaches whose target is placed. A target may be an unlocked part: the auto-layout places it first.
2. The auto-layout then places every unlocked part into its region, declared or inferred.
3. `_separate`, the pull step and the 4-pass loop move only parts without a pose lock (§3.3).
4. Regions without `at=` are packed on the grid around the regions that have it.

The rule is: **a pose the AI wrote is authoritative, and everything else is solved.** This is
`direction.md` §6 applied to the sheet.

### 3.6 PR 1 to PR 5: the forms that exist only for the move

PR 1 is a cut and paste, so it keeps today's forms in `schematic.core.py`: CSS on `SchPlace` and
`SchRegion` (`left=, top=, width=, height=`), `parent=`, and membership by file order (a `SchPlace`
written after a `SchRegion` belongs to it). Today a region's CSS box is the containing block that
positions its anchor while the group is built; the drawn box is fitted later around the members
(`_pack_groups` docstring, sch_place.py:775). So in PR 1–4 `width=`/`height=` size that containing block
and never the drawn box.

**PR 5 removes all three**, with refusals that state the fact and propose nothing:

- `SchPlace("R1", left=20)`: `schematic.core.py:N: SchPlace("R1") takes no CSS; a pose is at=(x, y) in its region`;
- `parent=`: `…: SchPlace("R6", parent="inputs"): membership is SchRegion(members=)`;
- a `SchPlace` after a `SchRegion` that does not list it: placed by the auto-layout in an inferred region
  (the line carries no membership).

`SchStyle` is refused from PR 1 in any file: `…: SchStyle() is now SchSheet(), in schematic.core.py`
(the `Chain()` precedent, `direction.md` §5b). `SchSheet` and `SchNet` arrive in PR 1, so the wire limits
`test_sch.py:192,195` set have a home from the start.

## 4. What gen contains

Here is a sample of `layout/<name>/schematic.gen.py`. The numbers in it are illustrative (the vector is
node's hand sheet from §2.7).

```
# schematic.gen.py - written by `pcbc build`'s sch stage for node: the schematic auto-layout. BUILD OUTPUT.
# Never edit this file and never read it as design input: it is rewritten on every build.
# To lock an object, copy its line into schematic.core.py next to board.py and rebuild. A SchPlace with at=
# needs its SchRegion line too. A '# auto:' comment is itself a core line (an attach lock instead of a pose).
# The lines after '# The sheet' are the drawn sheet, one KiCad primitive per line: the sch stage emits
# schematic.kicad_sch from them. They are build output, never locks (wires, junctions, no-connect marks and
# PWR_FLAGs follow from the lines above and board.py's netlist); one copied into schematic.core.py is refused.
# readability: hard 0 0 1 0 0 0 | lint 2, labels 28, partner 268.3 mm, power symbols 50, extent 282.1 x 183.6
SchSheet(paper="A3", wire_mm=25.4)
SchRegion("auto-U1", title="U1 ESP32-C3-MINI-1-N4", members=("U1", "R_EN", "C_EN", ...), at=(200.66, 12.7), outline=True)  # inferred; grid row 1, col 2
SchPlace("U1", at=(95.25, 60.96), rotate=0, mirror=None)  # region anchor
SchPlace("R_EN", at=(73.66, 71.12), rotate=90, mirror=None)  # auto: to="U1.EN"
SchText("R_EN", "Reference", at=(2.54, -1.27), rotate=0, justify="left", hide=False)
SchLabel("EN", pin="R_EN.2", side="left", stub=2.54)
SchPower("GND", pin="C_EN.2", side="bottom", stub=0)
```

How gen is written:

- **One object per line, in its gen form (§3.1), in a fixed sort order:** the sheet, then `SchNet`
  lines, then regions in grid order; under each region its parts in placement order, each followed by its
  text, labels and power symbols.
- **Locked objects are written too**, with the comment `# locked: schematic.core.py:N`, so gen is the
  whole resolved sheet.
- **Any gen line is a valid core line, copied verbatim.** The comment on a `SchPlace` is also a valid core
  line in place of the pose (`SchPlace("R_EN", to="U1.EN")`); writing both on one line is refused (two
  anchors).
- **Membership is written once**, in `SchRegion(members=)`, so copying every gen line restates no fact
  twice.
- **A lock equal to gen is a fixed point, alone or in any set (2026-10-09).** Copying any gen line or `# auto:`
  comment, or any set of them in any order, into core, over the line with the same key or anywhere new, is accepted,
  draws with hard 0 and leaves the whole sheet byte-identical: locking one part never moves another. How:
  - **Structure is unchanged by an equal line.** An `auto-…` region line with its inferred members is the inferred
    region (§2.2); an attach line equal to the auto-layout's spec for its part is met and left out (§3.3,
    `sch_auto.met_attaches`, found by elimination over the set). So the layout the locks are compared with is the one
    the lines were copied from.
  - **Drawn values are compared, not imposed** (`sch_emit._settle`): the sheet is laid out with every lock of a drawn
    value (the paper, a region's `at=`, a `SchPlace(at=)`, `SchLabel`, `SchPower`, `SchText`) left out; a lock equal to
    the result is met, and gen names its line. Otherwise the locks that differ, of the earliest stage (paper and
    origins, poses, names and symbols, texts), are applied, and the rest compared again.
  - **Of the poses that differ, the causes first** (`sch_emit._causal_poses`): in each region one is tried alone,
    the one whose pose put in place makes most of the region's other differing poses come out as locked (placement
    alone, with the pass's pushes; one `resolve` each), then one its part's own spec cannot reach, then placement order.
    Laid out, it stays if another of them now comes out as locked; otherwise it is taken back and the next is tried.
    When every one of a region's has been tried, or after six steps, the rest are applied at once. A pose pasted over a
    real attach line (`to="U4.VDD", gap=15.24`) differs from the sheet without it, and the parts placed after it differ
    because it moved; applying them too pinned parts the repair loop had moved and took it another road (refute:lock
    2026-10-09: node's C_MCU with R_LED, c3_usb's C_MCU with C_EN, node's C_BME with R_SCL).
  - An applied pose is placed with its region's core lines, at its pose, with the record of the attach that reproduces
    it (`sch_auto._explain`). A pose is relative to the region origin, and the region's anchor sits at the first member
    slot (§3.2): the frame never moves with the other members.

  Measured with real copies of the ten sheets (`schsets/h/sets.py`): every line alone, every pair within a region,
  500 random subsets per sheet; see "Status 2026-10-09 (fourth round)".
- **Fixed-point test.** Copy every gen line into core, rebuild, and the `.kicad_sch` must come out
  byte-identical, under two `PYTHONHASHSEED` values. **Single-line variant:** for each object kind, copy
  one gen line (with its region line for a pose), rebuild, and that object's gen line is unchanged.
- **Wires, junctions and no-connect marks are written to gen only as drawn primitives (PR 6)**, never as locks.
  They are connectivity, which is `board.py`'s, and they are re-derived from the poses on every build.

Gen is stamped in `schematic.inputs.json`. If it has been edited or deleted, the build reports it as
stale (`NEVER_EDITED`), exactly as for `layout.gen.py`, and only the sch stage reruns (§1.1).

**PR 6 (done 2026-10-08): the sheet itself is emitted from gen objects**, the PCB's `emit(merge(core, gen))`
with one writer. After its lock lines gen lists, under `# The sheet, primitive by primitive`, every KiCad primitive
of the sheet with every field (`sch_prims`: `SheetSymbol` with its `SheetField`s and pin ids, `SheetWire`,
`SheetJunction`, `SheetNoConnect`, `SheetLabel`, `SheetRect`, `SheetText`). These are no locks: a primitive line
in `schematic.core.py` is refused. The sch stage writes gen, loads its primitive lines back from the file and
writes `schematic.kicad_sch` from what it loaded with `sch_prims.sheet_text`, the one writer, in KiCad's own
order and layout; the written sheet must decompile to gen's primitive lines byte for byte, or the stage stops on
a generator bug. `kicad-cli sch upgrade` saves the body back byte for byte (tested on all ten bench sheets), and
`decompile` refuses a token it has no field for (`Unmapped`), so the census of unmapped fields is 0.

## 5. Refusals

Each refusal is a fact, in the format of `direction.md` §7b: the file and line, the object, and the
numbers. None proposes a value or a rewritten line.

**In `board.py`** (from PR 1; mechanism in §1.2):

- `SchPlace`, `SchRegion`, `SchSheet`, `SchNet`, `SchText`, `SchLabel`, `SchPower`:
  `board.py:51: SchPlace("R1") is schematic layout; board.py holds the circuit and its physics only, and
  schematic layout lives in schematic.core.py beside board.py.` Several lines are one error listing every
  line, as the PCB refusal does.
- `Net(wire_mm=)`: `Net_` keeps the keyword only to refuse it: `board.py:40: Net("LED", wire_mm=0): the
  schematic wire limit is schematic layout; it is SchNet() in schematic.core.py beside board.py.`
- `SchStyle` anywhere: §3.6.
- The import line is not refused (`from pcbc import SchPlace` still resolves); the call is.

**In `layout.core.py`:** any `Sch*` object (§1.2).

**In `schematic.core.py`:**

- **Wrong file.** A PCB `Place`, `Seg`, `Via` or any `board.py` object is refused, naming the field it
  changed and the file it belongs in.
- **Unknown keyword** on any object: refused, naming the keyword and the keywords the object takes (§3).
- **Unknown objects**, naming the line: a ref, a net or a region that does not exist; a pin a part does
  not have, listed with the pins it does have; a declared region name starting with `auto-`.
- **Connectivity.** `SchLabel` or `SchPower` on a pin that is not on that net, or on the wrong kind of
  net (§3.4). There is no wire, junction or no-connect vocabulary at all.
- **One home per fact**, naming both lines: two lines with one key (§3.1: `SchPlace` per ref, `SchRegion`
  per name, `SchText` per (ref, field), `SchLabel`/`SchPower` per (net, pin), `SchNet` per net, two
  `SchSheet`); a part in two regions' `members`; two region anchors in one region.
- **One anchor per line.** A `SchPlace` with two anchors (`at=` with `to=`, `below=` with `to=`), or a
  `SchRegion` with `at=` and `row=`/`col=`.
- **Frames.** `SchPlace(at=)` for a part in no declared region (§3.2); an attach whose target is in another
  region (§3.3).
- **Cycles.** An attach cycle (`to=` or `below=`) is refused with the chain of refs, as today.
- **Grid.** An explicit `at=` off the 1.27 mm grid is refused with the value and the grid pitch. (All hand
  poses on the five boards are on the grid: 0 off-grid, `$S/rb/grid.py`.)
- **Overlap.** Two pose-locked symbols in one region whose bodies overlap are refused at load with both
  lines and the overlap in mm (from PR 3, when `at=` exists). Attach locks are never refused for overlap;
  they are pushed, and an overlap left after the push is the build's hard `body_overlaps`.

**In the build:**

- A pose-locked part outside the paper, which can only be known once its region is placed, is a build
  error naming the line, the paper and the distance outside.
- A non-zero hard number (§2.6) on a sheet with or without core is a build error from PR 5, naming the
  two objects and the overlap in mm (or the wire and the text). PR 4 enforces hard 0 only on the bench's
  auto pictures, because DS2's verbatim PR 1 core fails it (§2.7).
- Before that, the lint messages that carry advice are rewritten as facts (PR 2):
  `"lengthen the pins or hide numbers"` (also removed from the score, §2.6) and
  `"that pin's free side is the other way"`.

## 6. Migration

PR 1 moves the lines without changing them: the pictures do not move. PR 5 replaces the moved lines with
the trimmed form. Line numbers are from the frozen copy (all checked by the build review). No `import` is
needed in core.

**Tests that pin example line numbers** shift when PR 1 removes the blocks. PR 1 re-pins every one:

- node (block 98–141, later lines move up 44): `test_constraints.py:226` (NetReq lines
  `[144,145,146,148,149]`), the "Pair line 143", "Path line 147/150" and "Thermal line 157" citations, and
  `test_dru.py:438` ("NetReq line 144");
- buck (block 39–53): `test_loops.py:36` (`splitlines()[67]`, "Loop line 68") and
  `test_copper_rules.py:102` ("Path line 62/69");
- c3_usb (block 233–261): `test_route.py:99` ("line 263") and the two "Path line 268" citations.

The PCB move (`docs/board-outline.md` PR 1) removes `Place` lines and edits the same import lines, so
the same tests shift again. Whichever PR lands second rebases onto the other and re-pins once more.

**DS2 in the tests.** `tests/conftest.py:ds2_dir()` copies the owner's real `ds2_addon.py` and patches
the copy (`chain_rewrite`, `DS2_MIGRATIONS`). Until the owner applies the DS2 patch, the real file still
has the `Sch*` lines at 99–133, which PR 1 refuses. PR 1 adds a `DS2_MIGRATIONS` step that moves lines
99–133 into `schematic.core.py` beside the temp copy and leaves blank lines in their place, so every later
line keeps its number, as the existing migrations do. Without it, test_bridge, test_cli, test_guard,
test_native, test_dru, test_constraints, test_native_scene and test_native_refuted3 fail.

**Tests that edit a board's text.** `test_sch.py:240–242` and `325–329` rewrite `SchPlace` lines in a
copied example's `board.py`; they edit the copied `schematic.core.py` instead. Inline board strings
(`test_paths_compile.py` 14, `test_route_checks.py` 7, `test_sch.py` 4, and others; `grep -c`) move their
`Sch*` calls into a core string written beside them.

**AI-facing docs.** `README.md` (11 mentions of `SchPlace`/`SchRegion`/`SchStyle`) and
`.claude/skills/pcbc/SKILL.md` (6, lines 115–150 teach `SchPlace` in `board.py`) are rewritten in PR 1.

### blinky (`examples/blinky/blinky.py`)

**Removed:** line 1: `SchPlace` from the import; lines 51–52.

**PR 1 `examples/blinky/schematic.core.py`:**

```
SchPlace("R1", left=20, top=20)
SchPlace("D1", pin="A", to="R1.2", gap=2.54)
```

**PR 5:** the file is deleted. The packer alone already scores 0 on every hard number on blinky.

### buck (`examples/buck/buck.py`)

**Removed:** line 4: `SchPlace, SchRegion` from the import; lines 39–53 (two comment lines and 13 calls).

**Edited:** lines 2–3 become `# netlist below; the tool draws the sheet from the netlist (schematic.core.py
locks what it should not move).`

**PR 1 `examples/buck/schematic.core.py`:** lines 39–53 verbatim:

```
# Schematic. This symbol has SW and VIN both on the left, so the output rail
# runs left from the inductor and the input stage sits under the IC.
SchRegion("sheet", title="Buck", left=12, top=12, width=380, height=200)
SchPlace("U1", parent="sheet", left=150, top=60)
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
```

**PR 5:** no file (v2 infers buck's one region exactly, §2.2), unless the benchmark shows the output
rail is better locked. In that case:

```
SchPlace("J_OUT", to="C_OUT2.1", side="left", reason="past the caps, at the end of the rail")
```

### c3_usb (`examples/c3_usb/c3_usb.py`)

**Removed:** lines 18–19: `SchPlace,` and `SchRegion,` in the import; lines 233–261.

**PR 1 `examples/c3_usb/schematic.core.py`:** lines 233–261 verbatim, the five comment lines and the
three `SchRegion` blocks.

**PR 5:**

```
SchRegion("usb", title="USB", members=("J1", "U3", "R_CC1", "R_CC2"))
SchRegion("pwr", title="3.3 V", members=("U2", "C_VBUS", "C_VBUS_HF", "C_3V3", "C_3V3_HF"))
SchRegion("mcu", title="MCU", members=("U1", "C_MCU", "C_MCU_HF", "R_EN", "C_EN", "SW_RST", "R_BOOT", "SW_BOOT", "R_LED", "D1"))
```

### node (`examples/node/node.py`)

**Removed:** line 5: `SchPlace, SchRegion` from the import; lines 98–141.

**Edited:** lines 3–4 become `# 33 parts - the hardest sheet so far. The AI writes the netlist; the tool
draws the sheet.`

**PR 1 `examples/node/schematic.core.py`:** lines 98–141 verbatim.

**PR 5:**

```
SchRegion("usb", title="USB", members=("J1", "U3", "R_CC1", "R_CC2"))
SchRegion("pwr", title="3.3 V", members=("U2", "C_VBUS", "C_VBUS_HF", "C_3V3", "C_3V3_HF"))
SchRegion("mcu", title="MCU", members=("U1", "C_MCU", "C_MCU_HF", "R_EN", "C_EN", "SW_RST", "R_BOOT", "SW_BOOT", "R_LED", "D1"))
SchRegion("sensor", title="Sensor", members=("U4", "C_BME", "R_SDA", "R_SCL"))
SchRegion("analog", title="Analog", members=("U5", "C_OPA", "R_NTC", "R_TDIV"))
SchRegion("load", title="Load", members=("R_G", "R_PD", "Q1", "J_LOAD"))
```

### DS2 (the owner's `~/Documents/MaD/Hardware/DS2Addon/pcbc/ds2_addon.py`)

This file is read-only to the tool's authors. It is delivered as a patch that the owner applies. The
migrated copy has the same line numbers.

**Removed:** line 15: `SchPlace, SchRegion` from the import; lines 99–133 (the comment, 3 regions, 28
`SchPlace`).

**PR 1 `Hardware/DS2Addon/pcbc/schematic.core.py`:** lines 99–133 verbatim:

```
# Schematic: groups in order, parts hung off pins. The tool stacks the
# headers, picks a free side, and fits each box.
SchRegion("inputs", title="Inputs")
SchPlace("J2", mirror="y")
SchPlace("J5", below="J2", mirror="y")
SchPlace("J4", below="J5", mirror="y")
SchPlace("R6", to="J2.3")
SchPlace("R7", to="J2.4")
SchPlace("C7", to="R6.2")
SchPlace("C8", to="R6.2")
SchPlace("C9", to="R7.2")
SchPlace("R8", to="J5.1")
SchPlace("R9", to="J5.2")
SchPlace("C10", to="R8.2")
SchPlace("C11", to="R8.2")
SchPlace("C12", to="R9.2")
SchPlace("R1", to="J4.1")
SchPlace("R2", to="J4.2")
SchPlace("C1", to="R1.2")
SchPlace("C2", to="R1.2")
SchPlace("C3", to="R2.2")

SchRegion("adc", title="ADC")
SchPlace("U1")
SchPlace("C5", to="U1.DVDD")
SchPlace("C6", along="C5.1", side="bottom")
SchPlace("C4", to="U1.AVDD")
SchPlace("R10", to="U1.~{RESET}")
SchPlace("R3", to="U1.RX")
SchPlace("R4", to="U1.TX")
SchPlace("R5", to="U1.GPIO2/~{DRDY}")

SchRegion("edge", title="EdgeBoard")
SchPlace("J1", mirror="y")
SchPlace("J3", below="J1", mirror="y")
```

**PR 5** (regions in reading order, which the §2.4 pin-side rule gets wrong for `J2` and `J3`):

```
SchRegion("inputs", title="Inputs", col=0, members=("J2", "J5", "J4", "R6", "R7", "C7", "C8", "C9", "R8", "R9", "C10", "C11", "C12", "R1", "R2", "C1", "C2", "C3"))
SchRegion("adc", title="ADC", col=1, members=("U1", "C4", "C5", "C6", "R10", "R3", "R4", "R5"))
SchRegion("edge", title="EdgeBoard", col=2, members=("J1", "J3"))
```

### Test fixtures

`SchPlace(` and `SchRegion(` calls move verbatim into a `schematic.core.py` beside each fixture in PR 1
(counts from `grep -cE 'SchPlace\(|SchRegion\('`, all confirmed by the build review):

| Fixture | Calls moved |
|---|---|
| `tests/fixtures/ds2_addon.baseline.py` | 31 |
| `buck_netreq/buck.py` | 14 |
| `r2_b1` | 13 |
| `guard` | 12 |
| `buck33` | 11 |
| `r2/fxa.py` | 9 |
| `planes` | 7 |
| `bridge` | 5 |
| `d14/neck.py` | 4 |
| `d14/qfn16` | 4 |
| `pair` | 4 |
| `thermal` | 2 |

**Fixtures keep their core files.** They run the sch stage, so dropping their cores would make each auto
sheet subject to the hard gate. Their hand sheets scored hard 0 on the first version's hard set (10
loadable fixtures, build review); PR 5's gate re-scores them on the §2.6 set.

## 7. Picture benchmark

The existing report is used: `sch_bench.py`, `tests/benchmarks/schematics/<board>.{svg,png}`,
`vibes.images.json`, and the `pictures` job in `.github/workflows/test.yml`. Vibes posts the current
picture, the new picture and their difference on the pull request. The changes:

1. **Two pictures per board.**
   - `<board>.auto.png` is drawn with no `schematic.core.py` (`load_board(path, schematic_core=False)`):
     the bare auto-layout.
   - `<board>.png` is drawn with the board's core: the cleaned-up sheet.

   Both have entries in `vibes.images.json`, with ids `<board>-auto` and `<board>` and titles such as
   `"Node — auto, no core"`. A lock can never hide a regression in the auto-layout, because the auto
   picture moves. `tests/test_schematic_manifest.py` hard-codes the ids
   `("blinky","buck","c3_usb","node")`, `row["board"] == row["id"]` and `path == <id>.png`; PR 2 changes
   it to accept the `-auto` entries, and `sch_bench.BOARDS` gains the no-core mode.
2. **A "before" for the auto picture.** PR 2 commits the sorted packer sheets as the first
   `<board>.auto.png` (deterministic, §2.5). On PR 4, Vibes then shows packer → auto-layout with the
   difference, for every board.
3. **Scores.** `<board>.score.json` and `<board>.auto.score.json` are saved next to each picture.
   `test_schematic_bench.py` runs under two `PYTHONHASHSEED` values and fails when:
   - a hard number is above 0 on an auto sheet (from PR 4) or on any sheet (from PR 5);
   - a soft number gets worse than the saved one, unless it is updated with
     `PCBC_UPDATE_SCHEMATICS=1`.

   The PR body quotes the score diff.
4. **DS2 joins the report** from a committed copy beside its components, the way
   `tests/fixtures/ds2_addon.baseline.py` is already committed. This is an owner question (§8). Until it
   is answered, DS2's before and after pictures are rendered locally and attached to the PR.

The baseline pictures of §2.7 (hand and packer, all five boards) are at
`$S/png/<board>_{current,packer}.png` (the packer pictures there are from the unsorted first run; the
sorted sheets are `$S/rev/packer_s0/<board>.kicad_sch`).

## 8. PR plan

Every PR ends with the full suite, run last with nothing else running:

```
cd /Users/rileymccarthy/pcbc && PCBC_REQUIRE_KICAD=1 .venv/bin/python -m pytest -q -p no:cacheprovider
```

Two failures are already known: `test_bridge` (DS2 two grounds) and `test_route_geom` (KiCad air).

| PR | What | Gate |
|---|---|---|
| 1 | The `schematic.core.py` loader in `load_board`, outside the load trace (§1.1), with today's `SchPlace`/`SchRegion` keywords plus `SchSheet` and `SchNet`. The three-file refusals by design inspection (§1.2), `Net(wire_mm=)` and `SchStyle` refused, unknown keywords refused, `line`/`source` on the specs. The sch stage becomes a side branch of the ladder. The examples and fixtures move verbatim (§6), with the test re-pins, the conftest DS2 step, `test_sch.py`'s text edits, README and SKILL.md. `direction.md` §1 and §2 amended. | The four committed pictures are byte-identical. The DS2 copy's `schematic.kicad_sch` sha is unchanged. Each refusal is tested by name and through each import path. Editing `schematic.core.py` reruns only sch, and editing `layout.core.py` leaves the sheet untouched (§1.1). |
| 2 | Determinism: every set iteration in `sch_emit`/`sch_place` sorted. `sch_score.py` (the `measure.py` vector, §2.6) with text collisions from KiCad's SVG, the field-model fix for rotated/mirrored box symbols and its fidelity test, paper by width and height. `pcbc sch` prints the vector; the `best` loop is keyed on the order (ties keep the later pass). Lint messages become facts; library facts leave the score. The sorted packer sheets are committed as `<board>.auto.png` with the manifest test and `sch_bench` no-core mode; score files saved. | Pictures with core unchanged (if the new key keeps a different pass on some board, the PR names the board and both vectors). Scores match the §2.7 tables. Bench passes under two seeds. Fidelity test passes. |
| 3 | `layout/<name>/schematic.gen.py`. The vocabulary of §3.1: `at=`, `members=` (closed), `row=`/`col=`, `outline=`, `SchText`, `SchLabel`, `SchPower`, `auto-` names. The frame (§3.2) and lock (§3.3) rules with their refusals. Gen is stamped. | Fixed point under two seeds: every gen line copied into core gives the same sheet byte for byte; the single-line variant for each object kind. Editing gen is reported as stale and reruns only sch. |
| 4 | Auto-layout (§2.2–§2.5) with the v2 inference. The "no SchPlace()" refusals are removed. The dead `sch_emit._layout` is deleted. | All five auto sheets: hard 0, `labels`, `lint_issues` and `label_partner_mm` ≤ hand, extent area ≤ 1.5 × hand (§2.7). Vibes posts packer → auto for every board. |
| 5 | Cores are trimmed to the §6 PR 5 form. CSS, `parent=` and file-order membership are removed with their refusals (§3.6). A non-zero hard number becomes a build error on every sheet. | With-core pictures at least as good as the hand sheets on every number, and hard 0 on all five boards (none of c3_usb, node and DS2 is today) and on every fixture. Vibes posts hand → trimmed. |
| 6 (done 2026-10-08) | The sheet is emitted from gen objects (one writer, decompile round trip). | `direction.md` §4, applied to the sheet: met, see §4 and the status of 2026-10-08. |

**Order with the PCB move.** `docs/board-outline.md` PR 1 and this PR 1 both change `load_board` and the
example import lines. Either may land first; the second rebases, re-pins the line-number tests once more
(§6), and keeps both loaders in the order of §1.1.

**Owner questions.**

1. Do you accept a third source of truth, `schematic.core.py`, in `direction.md` §2?
2. May DS2's netlist be committed to pcbc so it appears in the picture report?
3. Should hard readability failures be build errors on every sheet (recommended, from PR 5)?
4. The auto-layout orders regions for short wires, not signal flow (§2.4: the symbols carry no pin types,
   and pin sides disagree with your DS2 reading order on 2 of 5 connectors). Reading order is then a core
   `col=` line. Is that the split you want?
5. The PR 4 extent target is 1.5 × the hand sheet's area. Is 1.5 the number?

---

## Revision 2026-10-03 (after three reviews)

The reviews are `$S/review-ai.md` (can an AI use it), `$S/review-build.md` (buildable) and
`$S/review-readability.md` (does it give a clean sheet). Every issue is addressed below; none is wontfix
except where stated with evidence.

**New measurements.**

- The packer, with its set iteration sorted, is byte-identical under seeds 0, 1, 2 on all five boards;
  the §2.7 packer rows are re-taken (`$S/rev/run_all.sh`, `run_packer.sh`). Hand rows reproduce
  unchanged under seeds 0 and 1.
- `measure.py` gained the numbers the readability review showed were missing (wire through text, text
  crowding, ambiguous names, paper and parts off it, label partner distance, upside-down power, wrong way
  up), a 0.001 mm tolerance on `tight_pairs` and 3 dp on `min_gap`.
- `regions.py` gained the v2 rule (units, banks, power-only connectors), pairwise scoring, region counts
  for every board, and the hub-side comparison against the hand sheets.

**Blockers.**

| Issue | Resolution |
|---|---|
| AI B1: `SchRegion` has no single lock meaning | §3.3: one fact per keyword; omitted keywords stay solved; file order never places a region; `members=` is closed; gen writes the full form and a core line may be partial. |
| Build 5 / Readability B1: packer depends on the hash seed | §2.5, §2.7: sorted in PR 2, rows re-taken and checked under 3 seeds; bench and fixed-point tests under two seeds. |
| Readability B2: §2.4 relies on pin types the libraries never set | §2.1, §2.4: types dropped; pin-side rule with the tie case defined; measured 7/9 against the hand sheets. Ordering by graph distance from the connectors is **wontfix**: every non-hub region on DS2 and node is one hop from the hub, so it gives no order (`regions.py v2`). Signal-flow order becomes a core lock (owner question 4). |
| Readability B3: text collisions scored on a wrong text model | §2.6: the scored number comes from KiCad's SVG (0.4 s per export, measured); PR 2 fixes the model and adds a fidelity test. |

**Major.**

| Issue | Resolution |
|---|---|
| AI M1 / Build 12: membership has three homes; fixed point restates it | §3.3, §4: `members=` is the one home; gen writes it once; `parent=` and file order removed in PR 5. `region=` (the review's choice) not adopted, with the reason in §3.3. |
| AI M2 / Build 11: two `at=` frames; region origin undefined; gutter | §3.2: one origin (the `_pack_groups` frame corner); place `at=` relative to it; drawn box never a lock; gutter corrected to 25.4 mm. |
| AI M3 / Build 6: what an attach lock locks; "locked" has two meanings; overlap refusal would refuse DS2 | §3.3: lock = pose; attach = relation with the gap solved, still pushed (measured shoves cited); overlap refusal only between pose locks, from PR 3. |
| AI M4: label `at=` has no frame | §3.4: labels and power symbols take `side` and `stub` from their pin, no absolute position; label-vs-wire and wrong-kind rules defined. |
| AI M5 / Build 8: refusal mechanism names the wrong list; field diffs | §1.2: design inspection per loader; `_BOARD_FIELDS` keeps the `sch_*` fields for the PCB loader; the schematic loader diffs everything else; `SchNet` writes `design.sch_nets`. |
| AI M6: how `board.py`'s refusal is reached | §1.2, §5: `pcbc` keeps exporting; the call is refused after execution, through every import path; tested per path. |
| AI M7: unknown keywords swallowed | §3: refused from PR 1; measured no board uses one. |
| AI M8: CSS has no removal date; `width=`/`height=` | §3.6: removed in PR 5; until then they size the anchor's containing block, never the drawn box. |
| AI M9: inferred region names unstable | §2.2, §3.1: `auto-` names, refused for declared regions; copying a gen region line declares it, which makes it stable; no `region=` to dangle. |
| AI M10: "one anchor per line" refuses DS2's zero-anchor lines | §3.2: no anchor is the region-anchor form, at most one per region; measured only DS2 uses it, once per region. |
| Build 1: tidying the schematic re-routes | §1.1: sch side branch and core outside the load trace, owned by PR 1, with its test. |
| Build 2: where core is loaded; 12 callers | §1.1: in `load_board`, so every caller; bench opts out for the auto picture. |
| Build 3: PR 1 shifts pinned line numbers | §6: every pinned test listed and re-pinned in PR 1; order with the PCB move in §8. |
| Build 4: conftest DS2 copy breaks | §6: PR 1 adds the `DS2_MIGRATIONS` step, numbering kept. |
| Build 7: pin types | as Readability B2. |
| Build 9: hard error covers only five boards | §6, §8: fixtures keep their cores; hard error moves to PR 5 and its gate includes the fixtures. |
| Readability M1: hard numbers that cannot fail; visible defects not hard | §2.6: crossings and wire-through-symbol become exact-tested invariants; hard set gains wire through text, off paper, region frames; soft set gains crowding, ambiguous names, upside-down power, label partner distance; paper chosen by height too. |
| Readability M2: vote score overstates; counts omitted; orphan | §2.2: pairwise precision/recall and region counts for every board and rule; v2 has no orphan (buck `C_OUT2` joins its rail). |
| Readability M3: round-robin splits bulk/HF pairs | §2.2: banks; `Path()` for IC-specific decoupling; remaining misses named. |
| Readability M4: RC filters cut across regions | §2.2: passive units join as one (DS2 vote 22 → 27/28, precision 0.56 → 0.95). |
| Readability M5: power row moves power connectors up; long nets unscored | §2.2, §2.4: power-only connectors join their rail (buck one region); `label_partner_mm` scored and gated. |

**Minor.**

- §-references fixed (gates are §2.6).
- Gen form listed per object (§3.1), with duplicate keys for every object; `SchStyle`, `parent=`, CSS and
  `Net(wire_mm=)` refusals worded (§3.6, §5); grid refusal states value and pitch only; page check is a
  build error; one keyword table is the AI's reference; sheet title source stated; gen comment as a core
  line, and both on one line refused.
- `circuit.check_design` named correctly; naive beats refined on buck, now shown in the table; the node
  miss list replaced by v2's four named caps; "fill ≥ 8 %" replaced (blinky cannot reach it); gen sample
  marked illustrative.
- Manifest test, `sch_bench.BOARDS`, README, SKILL.md, `test_sch.py` text edits, `SchSheet`/`SchNet` in
  PR 1, and the loop tie rule are in the PR plan.
- Region outlines are scored (`region_frames`, hard); fill is a per-board ratchet; the pull keeps
  2.54 mm / 0.5 mm clearance; min gap printed to 3 dp with a tolerance; USB-C pin-number advice is a
  library fact, out of the score.

---

## Implementation status (2026-10-08)

PR 1 to PR 5 are implemented in the working tree, in one change set; PR 6 is not. Where the code differs
from the plan above, it is listed here with the reason.

**Layout of the change.** `sch_core.py` (loader, vocabulary, facts), `sch_auto.py` (regions, synthesized
attaches, clusters, grid, pack), `sch_score.py` and `sch_font.py` (the §2.6 vector and the stroke-font
widths of Reference/Value), `sch_gen.py` (the gen file), `sch_emit.py` (drawing, the repair loop, locks of
text, labels and power symbols). `sch_place.py` keeps the attach primitives only; `apply_sch_places`,
`_pack_groups`, `_apply_css`, `SHEET`, `sch_emit._layout` and `_passive_chains` are deleted.

**Deviations.**

1. **PR 3, 4 and 5 landed together.** The gen file needs the auto-layout's frames, and the PR 5 cores
   need the gen vocabulary; staging them separately would have built the PR 3 forms on the packer only to
   delete them.
2. **The packer "before" pictures are not committed** (§7 item 2). The packer is deleted, so the first
   `<board>.auto.png` is the auto-layout's.
3. **Spacing.** Region gutter 12.7 mm (not 25.4: the frames are drawn boxes, measured, so the wider
   gutter only spread the sheet); part pad inside a frame 5.08 mm; title header 6.35 mm.
4. **Vocabulary extensions** found necessary on DS2: `SchLabel(at=, rotate=, justify=)` (a label placed
   on its group's wire, checked on the finished sheet) and `SchPower(jog=, rotate=)`. Both are written by
   gen, so the fixed point covers them.
5. **Repair loop.** Hard findings move the movable hangers they involve: first a flip, then a push of
   2.54 mm per pass up to 10.16 mm, with frames re-measured; 12 passes, stopping when nothing changes.
   Core lines are placed before any part the auto-layout chose in the same region.
6. **Determinism.** Uuids are keyed by content, positions quantized to 4 dp; the fixed point is
   byte-identical on all five boards (`sheet identical`); the gen file of the second round lists locked
   members in placement order, which may differ from the first round's order.
7. **Cores.** blinky and buck have no core (the auto sheet is hard 0 and matches the hand numbers).
   c3_usb and node keep two attach lines for the MCU decoupling (`C_MCU to U1.3V3`, `C_MCU_HF along
   C_MCU.1`), node one more (`C_BME to U4.VDD, gap=15.24`). DS2's core puts R3-R5 in `edge` (where v2
   groups them) and adds three `SchPower` locks; its auto sheet is not hard 0 (gate below).
8. **Locks against the auto direction.** A core line that hangs a part off another member counts as placed only
   once that member is, so the auto-layout never hangs anything off it first (buck: `J_OUT to C_OUT2.1` no longer
   closes a loop with the auto attaches `C_OUT1 to J_OUT.1`, `C_OUT2 along C_OUT1.1`).
9. **Repair of in-line labels.** A part standing on the straight run between two pins of one net within its wire
   limit is flipped or pushed by the repair loop, as a hard finding's parts are; a region of two-pin parts only
   (d14, r2) draws its bank along the anchor's pin.
10. **Fixtures.** CSS-only cores (bridge, guard, pair, planes, thermal, declared/blinky) are deleted; the
   rest become one region with `members=`, an anchor and their attach lines.

**Measured (2026-10-08).** Each board built from a fresh copy (`pcbc build`, then `pcbc review --no-open`), the
vector read from the build's sch step; hand = §2.7. Netlist verified and ERC clean on all ten sheets.

| Board | Sheet | hard | lint | labels | partner mm | extent mm |
|---|---|---|---|---|---|---|
| blinky | hand / auto (no core exists) | 0 0 0 0 0 / 0 0 0 0 0 | 0 / 0 | 1 / 1 | 0 / 0 | 25.3x27.4 / 24.9x28.0 |
| buck | hand / auto (no core exists) | 0 0 0 0 0 / 0 0 0 0 0 | 0 / 0 | 5 / 5 | 41.3 / 41.1 | 122.3x39.4 / 122.2x39.1 |
| c3_usb | hand / auto / core | 0 0 1 0 0 / 0 / 0 | 2 / 1 / 1 | 16 / 16 / 16 | 268.3 / 145.4 / 140.4 | 281.6x90.9 / 220.5x139.8 / 183.1x139.8 |
| node | hand / auto / core | 0 0 1 0 0 / 0 / 0 | 2 / 1 / 1 | 28 / 29 / 28 | 268.3 / 244.5 / 197.3 | 282.1x183.6 / 341.0x198.2 / 258.4x160.1 |
| DS2 | hand / auto / core | 1 12 5 0 0 / 0 1 1 0 0 / 0 | 29 / 4 / 3 | 36 / 32 / 32 | 158.6 / 199.5 / 141.3 | 283.1x78.2 / 307.5x96.6 / 235.4x125.8 |

Gates not met, named: PR 4 on DS2's auto sheet (hard 0 1 1: a 0.63 mm2 collision of C7 with the AIN0 label, and the
VDDA PWR_FLAG stacked on J2's VDDA arrow; `label_partner_mm` 199.5 > 158.6) and on node's auto sheet (labels 29 > 28).
The sch stage refuses DS2's auto sheet, so with no core DS2 does not build. With core, every board meets PR 5 except
DS2's extent (235.4x125.8 = 29613 mm2 against the hand 22138 mm2, 1.34x; the hand sheet is 1 12 5 hard). The PCB
stages of buck, c3_usb and node fail to route (VIN / VBUS); the frozen 2026-10-07 code fails identically.

## Status 2026-10-08 (second round: refutation fixes and PR 6)

The first status above overstated one result: it said DS2 with its core was "at least as good as hand on every number
except extent". The vector said otherwise (wrong way up 3 against 2, upside-down power 7 against 4, wire 425.4 against
407.7 mm), and c3_usb and node regressed on numbers its table left out. From here on the table is generated from the
build's own vector (every number, `spfix/tools/table.py`) and the regressions are listed by the same script, number by
number, never written by hand.

**Fixed.**

1. **A wire over a pin's drawn line is a hard finding** (`wire_over_pin`, §2.6) and the router refuses one for every
   part (`sch_emit.over_pin_line`, `_Sheet.segment_ok`; box symbols only had a 0.6 mm2 text-overlap test before, which
   a crossing never reached). Node's LED wire across SW_RST pin 2 is gone; the LED link is a label pair instead (labels
   29, hand 28), and the in-line lint names SW_RST, which the repair loop flips and pushes without clearing the run.
2. **One gen line copied into core.** Three causes, all fixed: declared regions were laid out in file order (now in the
   order of their first member in `board.py`, then name: file order never places a region); a pose-locked member became
   its region's anchor and skipped the normalize shift (the anchor is now the auto-layout's, and a region with pose
   locks is laid out as if they were absent and adopted exactly when every lock equals that pose, else from that layout
   with the locks put in and the others pushed clear); a region with `at=` left the grid, so the others packed into the
   hole (every region keeps its grid cell; the passes pack on the grid and the kept sheet is drawn once more at a locked
   origin that differs). Gen writes a region's places in the order its line lists members. A label or power-symbol lock is judged on the
   kept pass only: a pass whose lock does not hold ranks below every pass whose locks do, and hands the parts on the
   lock's net to the repair loop (a gen `SchLabel(at=)` line was written on a pass after the loop had flipped them).
   Measured with every gen line
   of every sheet copied alone over its core line in place (`spfix/tools/lockcheck.py`): no refusal, no rise in the hard
   number, the copied line comes back unchanged, and the sheet is byte-identical, on all 1091 cases (with core: DS2 145, c3_usb 106, node 177, buck 56,
   blinky 11; with no core: DS2 147, c3_usb 106, node 176, buck 56, blinky 11).
3. **`board.py` writing the design directly** (`pcbc.language._doc().sch_regions.append(...)`, `.keepouts`, `.outline`,
   `.board.size_mm`) is refused after `board.py` runs, by inspecting the design (§1.2: every import path).
4. **`pcbc sch` exits 1** on the error `pcbc build` stops on (hard findings included) and prints it; an off-paper part names
   the distance outside, and a pose-locked one its `schematic.core.py` line.
5. **DS2 with no core is hard 0** and builds: the pin-line rule cleared C7 on the AIN0 label; a PWR_FLAG with no clean spot
   from any pin may now hang off the end of a power symbol already drawn on that pin (`_flag_branches`). DS2's migrated
   core no longer needs its three `SchPower` locks (`tests/conftest.py DS2_SCH_CORE`: three `SchRegion` lines).
6. **PR 6**, §4: the sheet is emitted from gen objects through one writer; KiCad saves the body back byte for byte.

**Measured (2026-10-08, after).** Each board copied fresh, built with `pcbc build --force` and reviewed; the vector is the
build's sch step (text boxes from KiCad's SVG). `-auto` is the same board with no `schematic.core.py`.

| Sheet | hard (body_overlaps, text_collisions, wire_through_text, parts_off_paper, region_frames, wire_over_pin) | lint | labels | partner mm | power sym | wrong way up | upside-down power | crowding | ambiguous | tight pairs | extent mm | wire mm | netlist / ERC |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| blinky-auto | 0 0 0 0 0 0 | 0 | 1 | 0.0 | 4 | 0 | 0 | 0 | 0 | 0 | 24.9 x 28.0 | 21.6 | verified / clean |
| blinky | 0 0 0 0 0 0 | 0 | 1 | 0.0 | 4 | 0 | 0 | 0 | 0 | 0 | 24.9 x 28.0 | 21.6 | verified / clean |
| buck-auto | 0 0 0 0 0 0 | 0 | 5 | 41.1 | 16 | 0 | 1 | 1 | 1 | 3 | 121.2 x 39.1 | 152.4 | verified / clean |
| buck | 0 0 0 0 0 0 | 0 | 5 | 41.1 | 16 | 0 | 1 | 1 | 1 | 3 | 121.2 x 39.1 | 152.4 | verified / clean |
| c3_usb-auto | 0 0 0 0 0 0 | 1 | 16 | 145.4 | 32 | 1 | 4 | 6 | 1 | 4 | 220.5 x 139.8 | 340.4 | verified / clean |
| c3_usb | 0 0 0 0 0 0 | 1 | 16 | 140.4 | 32 | 1 | 4 | 6 | 2 | 5 | 183.1 x 139.8 | 340.4 | verified / clean |
| ds2-auto | 0 0 0 0 0 0 | 3 | 35 | 208.7 | 25 | 0 | 3 | 1 | 0 | 4 | 321.5 x 95.4 | 408.9 | verified / clean |
| ds2 | 0 0 0 0 0 0 | 3 | 36 | 170.8 | 25 | 0 | 3 | 2 | 0 | 4 | 240.3 x 151.2 | 430.5 | verified / clean |
| node-auto | 0 0 0 0 0 0 | 2 | 29 | 238.6 | 50 | 1 | 7 | 8 | 3 | 7 | 341.0 x 198.2 | 508.0 | verified / clean |
| node | 0 0 0 0 0 0 | 1 | 29 | 192.3 | 51 | 1 | 6 | 8 | 4 | 8 | 258.4 x 160.1 | 501.6 | verified / clean |

Worse than the hand sheet (§2.7), number by number, as the vector says:
- blinky-auto: wire_mm 21.6 > hand 20.3; extent area 697 mm2 = 1.01 x hand 693 mm2
- blinky: wire_mm 21.6 > hand 20.3; extent area 697 mm2 = 1.01 x hand 693 mm2
- buck-auto: none
- buck: none
- c3_usb-auto: wrong_way_up 1 > hand 0; upside_down_power 4 > hand 2; wire_mm 340.4 > hand 309.9; extent area 30826 mm2 = 1.20 x hand 25597 mm2
- c3_usb: wrong_way_up 1 > hand 0; upside_down_power 4 > hand 2; tight_pairs 5 > hand 4; wire_mm 340.4 > hand 309.9
- ds2-auto: label_partner_mm 208.7 > hand 158.6; wire_mm 408.9 > hand 407.7; extent area 30671 mm2 = 1.39 x hand 22138 mm2
- ds2: label_partner_mm 170.8 > hand 158.6; wire_mm 430.5 > hand 407.7; extent area 36333 mm2 = 1.64 x hand 22138 mm2
- node-auto: labels 29 > hand 28; wrong_way_up 1 > hand 0; upside_down_power 7 > hand 6; ambiguous_names 3 > hand 2; tight_pairs 7 > hand 6; wire_mm 508.0 > hand 476.3; extent area 67586 mm2 = 1.30 x hand 51794 mm2
- node: labels 29 > hand 28; wrong_way_up 1 > hand 0; ambiguous_names 4 > hand 2; tight_pairs 8 > hand 6; wire_mm 501.6 > hand 476.3

Gates not met, named: with core, every board is hard 0, but none of c3_usb, node and DS2 is at least as good as hand on
every number (the list above); DS2's extent with its core is 1.64 x the hand sheet's area (PR 5's 1.5), and node has
29 labels against 28 on both sheets (the LED pair, see 1). An independent parser of the emitted sheets
(`refute-sheet/sheetscan.py`, with KiCad's label-angle convention) finds 0 wires over pin lines on all ten; on DS2 it
reports Reference texts crossed by wires (C12, C3, C7) that KiCad's own SVG draws clear of the wire (C12 ends at
x=38.56, the wire is at 39.37), so those are its field model, not the sheet.

Routing (`pcbc build`): blinky and DS2 (both variants) route fully and build; buck (VIN), c3_usb (VBUS) and node (VBUS)
leave one net unrouted, as on the frozen code of 2026-10-07.

## Status 2026-10-09 (third round: the lock fixed point measured with real copies)

The second round's "1091 cases, all hold" was measured by a harness that symlinked `board.py`; `load_board` resolves the
link, so every case read the original `schematic.core.py` (refute:split, blocker). With real copies: 2 gen lines were
refused, 6 raised the hard number (node's U1 0 -> 19, C_BME 0 -> 18) and 117 changed the sheet.

**Fixed.**

1. **A lock equal to gen is a fixed point** (§4): `sch_emit._settle` lays the sheet out with the drawn-value locks left
   out and applies only those that differ; a posed part is placed in its turn with the record of the attach that
   reproduces its pose (`sch_auto._explain`), the other members' specs are written around its auto spec, and the region
   frame is fixed by its anchor (§3.2; the frame is still measured as before, so every sheet is byte-identical to the
   previous round's). The one-region outline rule counts declared regions too (two inferred region lines pasted into
   core used to take the last inferred region's outline away).
2. **`board.py` cannot change a BoardSpec field after `Board()`** (`size_mm`, `origin`, `padding`, `layers`,
   `stackup`, `planes`, `pcb`, each named in the refusal), nor set `design.board` to a BoardSpec it built itself.
3. **No wire is drawn along another** (`wire_overlaps`, §2.6, hard).
4. **Refusals state the facts**: a label lock off its wire names the group's wires as drawn, the locks the sheet
   applies, and where the point was on the net's wire without them; a hard finding naming a part posed by an applied
   lock names the lock's line (a region-frame finding, the region's posed members).
5. **A region's clusters stack two to a column** (DS2's inputs: 1.64 -> 1.42 times the hand sheet's area).

**Measured** (scratch `schfp`): every gen line of all ten sheets pasted over its core line (a pose alone and with its
region line) and 12 random sets, all texts, all names, all lines, per region, with real copies (`tools/lc.py`, a probe
case proves the case's core is read): 1250 cases on the code without the cluster columns, all but one held (sheet
byte-identical, hard 0, every pasted line back unchanged); the one (c3_usb-auto, two inferred region lines pasted) is
fixed by the outline rule above, and c3_usb-auto (124) and DS2 with its core (191) rerun on the final code all hold.
`pcbc sch` itself on the ten named refute cases (node U1, C_BME, R_BOOT, node-auto U1, c3_usb C_MCU, buck's L1 Value,
DS2's AIN0 label and R4 Reference, DS2-auto's AIN1 label and R1): exit 0, hard 0, sheet byte-identical to the base. Sheets, with KiCad's SVG text boxes (`pcbc build --force` on fresh copies):

| Sheet | hard (7) | lint | labels | partner mm | extent mm |
|---|---|---|---|---|---|
| blinky / -auto | 0 / 0 | 0 / 0 | 1 / 1 | 0 / 0 | 24.9 x 28.0 / 24.9 x 28.0 |
| buck / -auto | 0 / 0 | 0 / 0 | 5 / 5 | 41.1 / 41.1 | 121.2 x 39.1 / 121.2 x 39.1 |
| c3_usb / -auto | 0 / 0 | 1 / 1 | 16 / 16 | 140.4 / 145.4 | 183.1 x 139.8 / 220.5 x 139.8 |
| node / -auto | 0 / 0 | 1 / 2 | 29 / 29 | 192.3 / 238.6 | 258.4 x 160.1 / 341.0 x 198.2 |
| ds2 / -auto | 0 / 0 | 3 / 3 | 36 / 35 | 197.4 / 208.7 | 313.9 x 100.4 / 321.5 x 95.4 |

**Not met**, named: node has 29 labels against the hand sheet's 28 on both sheets (the LED link is a label pair: the
straight run from U1.IO10 to R_LED.1 crosses SW_RST's column); DS2's label partner distance is 197.4 mm with its core
(hand 158.6; it was 170.8 before the cluster columns); c3_usb, node and DS2 are worse than hand on some soft numbers
(wrong way up, upside-down power, tight pairs, ambiguous names, wire length), listed per sheet by `schfp/tools/table.py`.

## Status 2026-10-09 (fourth round: sets of gen lines, attach lines, sheet facts)

The third round held for single gen lines (1253 cases), not for sets: two poses that each held alone moved node's
SW_RST and C_EN together (C_MCU over its attach line, with R_LED), four of DS2's inferred region lines pasted together
failed the build (hard 7), and gen's `# auto:` comment, which gen's header calls a core line, changed the sheet in 76 of
147 cases (refute:lock).

**Fixed.**

1. **Sets of gen lines are a fixed point** (§4): an `auto-…` region line with its inferred members is the inferred
   region, and the inference runs over every part outside the other declared regions (§2.2); of the poses that
   differ from the sheet laid out without them, one is tried at a time and kept only if it explains another
   (`_causal_poses`); the paper is a lock of a drawn value, met when the extent picks it.
2. **An attach line equal to the auto-layout's spec is met** (§3.3): left out of the layout (`met_attaches`), so its
   part is placed in its turn; a `beside` side is met only when the placer gives it on the kept sheet, else the line
   is the lock it says. node's core line `SchPlace("R_BOOT", to="U1.IO9")` is such a line; it changes nothing now.
3. **The board has no padding** (`direction.md` §2): `BoardSpec.padding` is removed; an attribute `board.py` adds to a
   `BoardSpec` is refused. `direction.md` §2 also records what pcbc defends against in `board.py` (the API, not
   patched internals).
4. **Sheet facts.** A T whose third arm is a power symbol's pin gets its dot (an independent parser of the ten built
   sheets finds no point where three ways meet without one); a Reference or Value with a wire between it and its body counts in `ambiguous_names`, is placed clear of such
   a wire when a clean spot exists, and is otherwise a lint fact the loop repairs once a pass has no hard finding (DS2's
   R4 and buck's R_EN are clear now); one overlap is one hard finding however many boxes a symbol is drawn as; a hard
   finding on a sheet that applies locks names those locks and the hard count without them; a packed region moved past
   a locked origin moves past every frame it lands on.
5. **node: 28 labels on both sheets** (hand 28): of two parts that would take the same spot at a node, the shorter
   takes it, so C_EN hangs under the EN node and SW_RST beside it, as the hand sheet hangs them; IO10 to R_LED and IO9
   to R_BOOT are both wires.

**The invariant, measured** (`schsets/h/sets.py`, real copies of the ten sheets, a probe per sheet proving the case's
own core is read): 31,196 cases, no failure (accepted, hard 0, sheet byte-identical, every pasted line back). Every gen
line and every `# auto:` line alone (1,136), run in full; every pair within one region (25,060): 2,990 run in full, the
other 22,070 proven because the design `_settle` first lays out (drawn-value locks left out, met attach lines left out,
regions inferred) is the base sheet's, a proof checked by running 8,406 such cases in full anyway; 500 random subsets
per sheet (sizes 2 to all, shuffled, in place or at the top of the file, a third under `PYTHONHASHSEED=1`), all run in
full. `pcbc sch` itself (KiCad included) on the refuters' named sets: rc 0, hard 0, identical.

Measured, sheets built with `pcbc build --force` on fresh copies (KiCad's SVG text boxes), every number and every one
worse than hand from `schsets/tools/table.py`:

| Sheet | hard (7) | lint | labels | partner mm | extent mm | wire mm |
|---|---|---|---|---|---|---|
| blinky / -auto | 0 / 0 | 0 / 0 | 1 / 1 | 0 / 0 | 24.9 x 28.0 / 24.9 x 28.0 | 21.6 / 21.6 |
| buck / -auto | 0 / 0 | 0 / 0 | 5 / 5 | 41.1 / 41.1 | 123.7 x 39.1 / 123.7 x 39.1 | 157.5 / 157.5 |
| c3_usb / -auto | 0 / 0 | 1 / 1 | 16 / 16 | 140.4 / 145.4 | 183.1 x 139.8 / 220.5 x 139.8 | 337.8 / 337.8 |
| node / -auto | 0 / 0 | 1 / 1 | 28 / 28 | 206.1 / 250.0 | 260.9 x 160.1 / 341.0 x 198.2 | 523.2 / 532.1 |
| ds2 / -auto | 0 / 0 | 3 / 3 | 36 / 35 | 197.4 / 208.7 | 313.9 x 100.4 / 321.5 x 98.0 | 415.3 / 398.8 |

**Worse than hand**, named: blinky wire 21.6 > 20.3; buck wire 157.5 > 152.4 (R_EN's name, moved off the 5V wire, took
the room); c3_usb wrong way up 1 > 0, upside-down power 4 > 2, wire 337.8 > 309.9, tight pairs 5 > 4 (core), area 1.20 x
(auto); node wrong way up 1 > 0, wire 523.2 / 532.1 > 476.3, area 1.30 x (auto); DS2 label partner 197.4 / 208.7 > 158.6,
wire 415.3 > 407.7 (core), area 1.42 x on both.
