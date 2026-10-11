# Direction

The decisions that govern pcbc's next stretch, in the order they were made, each with the reason and
the evidence. Written 2026-09-23 from the owner's conversation with the tool's authors that day. When
another document disagrees with this one, this one wins; the others are listed under "Superseded" at
the end so nobody has to guess.

Read this before `docs/roadmap.md`, `docs/fp-router-plan.md`, `docs/layout-properties.md` or
`docs/tscircuit-notes.md`.

---

## 1. Three files, and what each one is

| File | Who writes it | What it holds |
|---|---|---|
| `board.py` | the AI | The circuit and its physics only: parts, pins, nets, `NetReq` (voltage, impedance, pair, length, isolation, planes), `Path`, the stackup, the copper count and which net is a plane on which layer. No geometry (§2, 2026-10-03). |
| `layout.core.py` | the AI | Locks: every geometric decision the AI makes (`Place()` with its anchors and rotation, `Region`, `Keepout`, a declared `Outline()`) and the copper and drawings the AI refuses to let the generator move. Every board that has parts has one: it is at least as long as the board's placement list. |
| `layout.gen.py` | the generator, every build | One Python object per KiCad board primitive — segment, arc, via, zone, footprint pose, `gr_*` — with **every field explicit**. Regenerated every build. Never edited. |
| `schematic.core.py` | the AI | Schematic locks (§2, 2026-10-03; `docs/schematic-layout.md`): regions with titles and members, region order, explicit poses, attachments, label, power-symbol and text placement, sheet defaults. Optional and partial: with none, the sheet is pure auto-layout. |
| `schematic.gen.py` | the sch stage, every build | One line per object the schematic auto-layout resolved, written beside `schematic.kicad_sch` under `layout/<name>/`, then the sheet's KiCad primitives one per line with every field (`sch_prims`): the sheet is emitted from them, and decompiles back to them byte for byte (§4 applied to the sheet, 2026-10-08). Never edited, never read back as design input; any `Sch*` line copied into `schematic.core.py` is a lock, and a primitive line is refused there. |

The final `.kicad_pcb` is `emit(merge(core, gen))`. KiCad fills the pours, runs DRC and exports the
fab package from that one file.

The owner's words, which fixed this: *"we keep auto layout and auto router creating python for gen
and have it output kicad as final step."*

**The generator writes Python natively.** Owner, 2026-09-25: *"I want the auto layout and placement to
generate directly python natively. That is why we need to build our own tool."* Placement and routing
read the Python model (parts from their `.kicad_mod`, `board.py`, `layout.core.py`) and write `Place`,
`Seg`, `Arc`, `Via`, `Pour` objects into `layout.gen.py`. **No KiCad file is an intermediate in
generation**: the first `.kicad_pcb` is the one `emit` writes. Running an external router and
decompiling its board into Python (what step 1 built, 2026-09-23..25) is a bridge, not the design; the
decompiler stays only as an importer and a test tool.

## 2. Sources of truth: exactly three

`board.py`, `layout.core.py` and `schematic.core.py` (amended 2026-10-03: the schematic follows the PCB, owner:
*"lets make another workflow for the schematic change too, This way it can auto layout then we can clean it up
with regions and other nice to haves for visualization?"*; the design is `docs/schematic-layout.md`). Everything
else — `layout.gen.py`, `schematic.gen.py`, the `.kicad_sch`, the `.kicad_pcb`, Gerbers — is build output. The
rules that keep it that way:

- **Never edited.** A fix always lands in `board.py` or `layout.core.py` and the outputs come out new.
- **Never read back as design input, and every output the build does read is stamped.** The route
  stage reads the placed board; the build records its sha so a hand edit is stale, never shipped.
  Same for the seed board, the `.kicad_pro`/`.kicad_dru` sidecars every stage reads, gen, the routed
  board and the fab package: **if a stage reads a file the build wrote, that file is stamped.**
  No stage may take a fact out of the `.kicad_pcb` or out of
  `layout.gen.py` to decide anything for this build. Checkers read the `.kicad_pcb`; fab reads it;
  nothing else does. (Today's route stage still chains the router's own `.kicad_pcb` files internally.
  That goes away when the in-house router replaces KRT; until then it is contained inside the route
  step; the emitted board's copper and drawings come only from Python, and its footprints,
  outline and setup come from the placed board, which is itself a stamped output.)
- **One writer of design content.** `emit` writes the final board from the two sources. Two later
  rewrites of the same file carry derived data only — KiCad's zone refill-and-save, and the uuid
  re-pin — and the build checks afterwards, field for field, that every layout object in the file
  is still the Python object, so neither can smuggle a design fact in. (Measured 2026-09-24: three
  writers, four writes.)
- **One home per fact.** Defaults chosen 2026-09-23, each reversible by changing one refusal:
  - **Placement lives in `layout.core.py`, not `board.py`.** Owner, 2026-10-03: *"why is node.py selecting
    orientation and placement? shouldnt that be in the core and gen.place files?"* `board.py` is the circuit
    and its physics only; every geometric decision the AI makes (Place with its anchors and rotation, Region,
    a declared outline) is a lock in `layout.core.py`; `layout.gen.py` holds the resolved pose of every part.
    A `Place()` in `board.py` is refused (a fact, naming the file it belongs in). Owner, 2026-10-03: *"ensure we
    remove the functionality so the tool doesnt support board.py layout changes"* — no compatibility path, no
    deprecation window: `Place`, `Region`, placement keep-outs and the outline size are removed from `board.py`'s
    vocabulary, and the code that read them from there is deleted, not bypassed. (Superseded 2026-09-23
    default: placement in `board.py`, chosen only to avoid migrating the boards.)
  - The outline lives in `layout.core.py` when the author declares it (`Outline(width, height)`), and is then
    fixed; `Board()` keeps the stackup, layers and planes (physics) and takes no size. **It is optional** (owner, 2026-10-03: *"lets make board outline optional and we can make it
    based on the design, if we need an outline we can specify it"*): with no size declared, the tool derives
    the smallest outline that fits the design (parts, routing room, edge clearance) and writes it to
    `layout.gen.py` as build output. `Edge.Cuts` drawings in core are refused. If cutouts are needed,
    `Outline()` grows a field for them. The design is `docs/board-outline.md`.
  - Nets live in `board.py` only. Core copper names an existing net or says `net=None` (KiCad's
    no-net, for a floating spreader, a coupon, a rule area). A net `board.py` does not have is
    refused at load, naming the line. Splitting `GND`/`AGND` is a circuit decision and goes in
    `board.py`. Owner's words: *"Does it make sense for it be able to invent nets?"* — no.
  - `layout.gen.py` is written under `layout/<name>/`, not next to `board.py`, so it never lands in
    a source tree or in the owner's real board checkout.
  - **Schematic layout lives in `schematic.core.py`, not `board.py`** (2026-10-03, the same decision for the
    sheet). The tool lays the sheet out from the netlist alone and writes every resolved pose to
    `layout/<name>/schematic.gen.py`; the AI locks what it wants kept in `schematic.core.py`. `SchPlace`,
    `SchRegion`, `SchSheet`, `SchNet` (and the later `SchText`, `SchLabel`, `SchPower`) in `board.py` or in
    `layout.core.py` are refused naming `schematic.core.py`; `Net(wire_mm=)` and `SchStyle()` are refused. No
    compatibility path. Editing `schematic.core.py` redraws the sheet only; editing `layout.core.py` never
    redraws it.
  - **The board has no padding** (2026-10-09). `BoardSpec.padding` inset the box placement and routing fit the
    parts into (`layout.content_rect`), and no file could set it: `Board(padding=)` is refused, `Outline()` takes
    `width`, `height` and `at`, and nothing in `layout.core.py` wrote it. The only way to give it a value was to
    assign it after `Board()` (refute:split 2026-10-08: blinky's outline and R1 moved 15 mm). What it would express
    already has a home: the derived outline is fitted with the stackup's edge clearance, a declared one is the
    `Outline()` the author chose, and a part kept off an edge is a `Keepout` or a `Place` in `layout.core.py`. So
    the field is removed, not given a file: the content box is the outline, and an attribute `board.py` adds to the
    `BoardSpec` (`Board(...).padding = ...`) is refused because nothing reads it.

**What pcbc defends against in `board.py`** (2026-10-09, the lead's decision; the owner may override it). `board.py`
is trusted Python, run in-process. The guard refuses every layout that the pcbc API lets `board.py` express: a layout
constructor by any import path (`Place`, `Region`, `Outline`, `SchPlace`, ...), an attribute of a pcbc object
(`Board(...).size_mm = ...`, an attribute a `BoardSpec` has no field for), and a design field written directly
(`pcbc.language._doc().sch_regions.append(...)`, `.keepouts`, `.outline`, `.board = BoardSpec(...)`); each is a fact
naming the file the layout belongs in, and the API-level guard is kept complete. Deliberately patching pcbc's
internals from `board.py` is outside what pcbc defends against: rebinding a module global (`pcbc.language._refuse_layout_state
= ...`), changing a module constant (`pcbc.pcb_place.GAP`, `sch_auto.GUTTER`, a list in `sch_place`), an object whose
`__eq__` lies to the comparison, or a thread that writes after the check has run (refute:split 2026-10-09 showed all
four). No defence is built for those: a board author who does it has left the language, and a sandbox would cost the
in-process load every stage relies on.

## 3. KiCad stays: backend and arbiter, never a source

The owner asked whether to drop KiCad and have Python write the fab files directly, to reduce the
number of places truth lives. Decision: keep it.

- **It is the only check we did not write.** The worst defects in this project have been our own
  checks grading the wrong thing: a pair's skew improved while the pair was destroyed; an orphan
  guard whose slack grew with how well the pass worked; a build reporting `copper: verified` on a
  board with no copper. If Python writes the Gerbers and Python checks them, nothing independent
  looks at the board before JLCPCB does.
- **It already does the hard geometry**: zone fill (clearances, thermal spokes, island removal),
  every footprint's pad shapes and mask openings, silk text in a real font, the 3D model.
- **tscircuit ran the other experiment** (`docs/tscircuit-notes.md` §4): ~30 in-house checks, a WASM
  pour solver, a Gerber writer, a second short-checker because the first missed shorts — and the
  fab-fatal bugs that still shipped are the ones KiCad catches (dropped via drills, dropped inner
  layers, an outline the fab could not read). When their own checkers disagreed, their contributors
  exported to KiCad and ran `kicad-cli pcb drc`.

The `.kicad_pcb` is **the thing that gets checked, not a source of truth**: information flows into
it from the two sources and out of it only as a verdict. DRC runs on it because it is exactly what
becomes the Gerbers; a check on the Python model would let an emitter bug ship. Zone fills exist only
in the KiCad file, but they are derived from fields Python holds, so checks like "is this plane one
island" run there and that is fine.

## 4. The layout Python is a direct image of the KiCad primitives

Owner's words: *"make sure the layout python is directly migrated to kicad primitives."*

- One Python object = one KiCad primitive, field for field. No composite objects, no primitive
  split across objects, no intermediate format (not `copper.json`, not a router's own model).
- Every field KiCad 10 writes has a Python field. The bar is **zero unmapped**, measured by a census
  over the five boards and the probe fixtures, pinned by a test. Python names may stay
  (`Seg`/`Pour`/`DrawArc`) but the 1:1 table lives in `docs/layout-properties.md`.
- Lossless both ways: KiCad → Python → KiCad gives the same primitive; the emitted board decompiles
  back to `layout.gen.py` byte for byte.
- `layout.gen.py` writes every field. A hand-written `layout.core.py` line may omit values
  `constraints.py` derives (width, gap, drill, clearance); a value below the compiled minimum is
  refused, never silently widened — tscircuit's `TraceWidthSolver` is the cautionary example.

## 4c. Orientation marks: every part whose orientation matters carries one the fab can read

Owner, 2026-10-08: *"can we also look at adding the pin 1 dots and directions for diodes and whatever other
silkscreen is needed for fab to determine orientation?"* JLCPCB places by the silkscreen (their assembly FAQ:
placement "according to the silkscreen layer by default"); a missing mark holds the order, and a mark at the wrong
pin is worse than none (node's BME280 library printed its only visible dot at pad 5's corner). `src/pcbc/orient.py`
holds the rules; the numbers are its constants. Decided:

- **Who needs one (G1), measured, not assumed.** A part needs a mark when a turn of 90/180/270 degrees about its
  pads' centre lands every pad (pegs included) on a pad of the same kind and size and some numbered pad then sits on
  a pad of another symbol pin — unless the part is its own turned self: every pin named only by its pad number, a
  body outline the turn maps onto itself, not a diode (resistors, ceramic caps, inductors, two-terminal switches,
  plain headers). A keyed housing (JST XH) is deeper on one side, so it is not its turned self. An IC or transistor
  (prefix U/IC/Q) whose pads fit one way only gets a pin-1 dot where there is room (JLC recommends one); no room is
  a note, not an error.
- **Which pin.** A two-terminal part with a pin named `K` is marked at the cathode (a bar), one with a `+` pin at the
  `+` (a `+`), every other at pad `1` (else `A1`). Read from `board.py`'s pin names, never from "pad 1".
- **A good mark (G2-G6).** Ink at least the fab's silk line and 0.40 mm across; 0.20 mm (else the fab's 0.15) from
  every mask opening and other silk, inside the board edge; outside every part's body (`F.Fab`'s box, else the
  `F.CrtYd` box: an EasyEDA courtyard is the body), so it is seen after assembly; within 1.0 mm of its pad's opening —
  of the body's edge when the pad is under the body (an LGA, a through-hole housing, a module); and it names its pad,
  twice over. *Within its part*: the nearest of the part's pads by 0.2 mm, or in line with its pad (its centre within
  the pad's span, as a chevron or a dot past the end of a row sits) and 0.2 mm nearer it than any other in-line pad of
  the part, none nearer. *Across parts*: its part 0.2 mm nearer than any other — each part's distance its nearest
  pad's opening, or its body's edge for a pad under the body (the nearest a visible mark can be). A **dot** (or a `+`)
  says nothing but "the pin nearest me", so it is read against every pad on the board — a resistor's, a fiducial's
  (round 2, refute:fab: node U5's and Q1's optional dots sat nearer a resistor's pad than their own pin 1). A
  **directional** mark — a triangle's apex, a cathode bar across its pad — names its pin by its shape, and is read
  only against the parts that take a mark themselves: the decap hard by the ESP32's pin-1 chevron is not its pin 1
  (measured: by every pad, c3_usb's and node's U1 then have no legal spot at all).
- **Whose mark it is, measured.** A footprint's drawing is its footprint's. A board drawing (a `layout.core.py` or
  `layout.gen.py` line) is the part's it sits by — the across-parts measure above, over every part (a directional
  mark: every part that takes a mark), within 3.0 mm *so measured*: round 1's 3 mm from the pads made buck's J_IN
  dot, 2.9 mm from its pad but 0.35 mm from its housing, no one's once moved 0.1 mm. Farther from every part, a
  drawing is decoration, no part's mark. A dot by a resistor is the resistor's silk, not the nearest LED's mark.
- **What is not printed, and what fails.** A library mark that names another pin of its part, a library dot that
  names another part's pin or no part, and a library dot that names no pin of its part (two pads within 0.2 mm: the
  BME280's vent dot) are left out, each with its fact; fab's notes state the same facts, measured where the silk pass
  judged the mark. A `layout.core.py` mark of a part that names another pin, names none (a dot), or names its pin and
  breaks G2-G5 fails the build with a fact naming the line and the rule it breaks — nothing is generated beside it.
- **Kept, generated, failed.** A good printed mark (the library's, or a core drawing) is kept and nothing is added.
  Otherwise the silk pass generates one, after the library silk and before the references (a reference is hidden
  before a mark is): a filled `gr_circle` dot (0.40-1.00 mm, the pad's narrower side first), a filled `gr_poly`
  cathode bar (0.30/0.25/0.20 mm wide, the pad's width + 0.2 long, past the cathode's outer end), or a filled
  `gr_poly` `+`. **New primitive use, no new primitive**: these are ordinary board `gr_*` objects on F.SilkS in
  `layout.gen.py` (one object, one KiCad primitive, §4), uuid from (board, part, pad); copying one into
  `layout.core.py` locks it — the pass then finds the part marked and generates nothing. The search is a grid
  (0.05 mm) over rings around the pad (or the body), ranked: clearance tier, biggest mark, the clearest of every
  other pin (to 0.6 mm), the nearest its pad. A required part with no legal spot fails the place stage with the fact
  (which part, which pad and pin, which turn fits, and how many of the spots tried each obstacle blocked, named); an
  optional one gets no dot, and a note says why.
- **Judged again on the emitted board.** The route stage measures the same rules on the board it emitted (each
  footprint's own silk plus the board's drawings) and fails on a part with no good mark or a mark naming another pin
  or part; fab writes the result under "Orientation marks" in `FAB_NOTES.md`.
- **Silkscreen to mask, all of it.** KiCad's DRC flags silk on a mask opening only where the two overlap
  (`silk_over_copper`; measured, refute-rules: a dot 0.05 mm from an opening passes it), so it is **not** the
  arbiter of JLC's 0.15 mm. `orient.silk_mask` measures every F.SilkS ink on the board — every drawing, mark or not,
  and every printed text's box — against every pad's mask opening; the route gate and fab fail on any nearer than
  the fab's clearance. KiCad's silk-to-silk check (`silk_overlap`) stays an arbiter: a mark moved onto its LED's
  bracket is three errors.
- **The placement file is JLC's frame, registered per part.** JLC turns a part from the zero of *its* footprint for
  the part's LCSC number (the EasyEDA footprint `easyeda2kicad` writes with `(property "LCSC Part" "C…")`; its
  origin is EasyEDA's footprint origin). KiCad's library footprints have their own zero: measured on the on-disk
  pairs, KiCad's SOT-23-5 lands on JLC's (the LMV321's, C7972) turned 180 degrees, KiCad's SOT-23-6 on JLC's (the
  TPS54202's, C191884) turned 270, KiCad's SOT-23 on the AO3400A's 180, KiCad's TSSOP-16 on the ADS122U04's 270, and
  KiCad's JST XH sits 180 degrees and 1.25 mm off JLC's. So `cpl.py` registers each part: JLC's footprint for its
  LCSC number (the board footprint itself when it records that number, else one on disk under the board's
  `components/` or the checkout's `components/` — what `pcbc fetch C… --into components` writes); for each quarter
  turn, every numbered pad turned and shifted (least squares) must land
  inside the board pad of the same pin, and every board pad be landed on; exactly one turn may land. Pads pair by
  number (a package's pin numbers are its datasheet's in both libraries), except a two-terminal part marked at its
  cathode or `+`, whose numbering is a library's choice: its cathode pairs with the pad JLC's own symbol (beside
  JLC's footprint, same `LCSC Part`) names the cathode. The CPL's rotation is KiCad's plus the turn, its centre
  KiCad's plus the shift turned with the part. **No JLC footprint on disk, pads that land at no turn or at two, a JLC
  symbol naming no cathode: a fact naming the part, its LCSC number and what is missing** (§7b). For a part whose
  orientation matters (it needs or gets a mark) fab fails on it. A part that is its own turned self (G1: a
  resistor, a ceramic capacitor, a plain header, a two-terminal switch) keeps KiCad's rotation with the fact in the
  notes: turned 180 degrees it is the same part, and turned 90 no terminal lands on a pad (G1 measures both), so a
  zero 90 degrees off is a misfit JLC's placement preview shows, not a wrong part. (Refinement of the round-2 spec,
  which failed every part: no JLC footprint for any chip resistor or capacitor is on disk, so every board, the DS2
  Addon included, would fail fab on its passives.)
- **3D.** Each KiCad-library footprint names KiCad's own model by its library path (`${KICAD10_3DMODEL_DIR}/…`,
  as the KiCad library does): the review's 3D view shows the package, pin-1 bevel and all. A model the installed
  KiCad does not ship (the ESP32-C6-MINI-1's) is shown as no body.
- **Out, for now:** back-side parts (pcbc prints no back silkscreen; noted, not judged; their CPL is a fact); arms
  of a cathode `⊔` (the library bracket supplies them; the bar alone is one-ended).

## 5. Routing: in-house, deterministic, driven by the net's requirements

- **KRT is deleted, now, and the build is native only.** Owner, 2026-09-25: *"Do delete today's path. Use
  native only."* No `Board(router="krt")` fallback, no parity gate. Until the native router can finish a
  board, the nets it cannot route are absent from `layout.gen.py` and the build fails with an error; the example boards
  and the DS2 Addon may not complete for a while, and that is accepted. (Superseded plan, kept for the
  reasoning:) KRT was to go once the in-house router reached parity — as a **whole-board switch**
  (`Board(router=...)`), never class by class. Reason, measured: pcbc passes `--keep-input-copper`
  on every KRT step, so `!NET` is a target-list filter and cannot remove copper's presence; the
  safe-mixing rule the tree already records (R-S1) is "safe iff no later router step is left
  contesting the space you took". Five pcbc pattern classes mix safely today because they obey it;
  `chain` and wide `spine` broke ds2 because they did not.
- **The relaxer is not a repair pass, it is the embedder.** Its string-pull is the second half of
  the router. It stops running last on someone else's copper.
- **The corridor / gate-word "phase A" is not the plan.** The proof run (2026-09-22/23) showed node's
  38.97 mm elbow and the 30.62 mm good path cross the *identical* gate word — phase A cannot tell them
  apart; the defect is a shape choice inside a wide-open corridor. The spike that built the corridor
  did worst of three (failed the elbow, illegal copper). `docs/fp-router-plan.md` §3 is superseded
  on this point.
- **What replaces it: a cost field made only of derived numbers.**
  `cost = length + Σ pitch·tan(θ/2) + vias × via_price`, with `pitch = width + clearance` from
  `constraints.py`. It prices the disputed corner at 0.41 mm against the 8.35 mm it bought. The
  cost-field spike routed **buck 21/21, 0 DRC errors, 15.36 deg/mm** — better than KRT+relaxer
  (18.32) and the hand-routed reference (17.2) — with neither KRT nor the relaxer.
- **Pairs are routed as one object**, never as two nets. Every spike that routed a pair half alone
  beat the length and destroyed the coupling (99.5 % → 59.7 %). Embedded as one object the same path
  saved 16.56 mm and held coupling at 99.4 %. Before pairs are owned, **coupled length becomes a
  first-class verified number**: every KiCad pair metric improves when a pair stops being one.
- **The hard part is the dense connector.** On node the cost-field spike completed 105/110 links;
  4 of the 5 failures are at the USB-C connector. That is what KRT's special-case rescues exist for,
  and where the AI's locks earn their keep.
- **Status: the spike results are unverified.** The adversarial verifiers never ran (usage limit).
  Nothing is built from them until they do.

## 5b. Physics: a property of the net, or of a path between pads

Owner, 2026-09-28: *"I think it needs to be more specific than per net and should be instead between nodes
on net. for example high current path on net between nodes needs to have high current but the voltage
sensing is low current and just needs other properties."* The design is `docs/net-path-physics.md`.

- **Net (`NetReq`)**: what is the same at every pad of a node — voltage and the clearance/creepage it implies,
  the span (`max_mm`), keep-away from another net (net to net only), the node's charge budget (`pf_max`), its
  edge rate (`rise_ps`), its class. (O6, owner 2026-10-01: rewritten to match `docs/physics-subjects.md` §2.)
- **Path (`Path(net, *pads, ...)`)**: what belongs to the copper between named pads — current (a number or
  `{station: A}` draws, and so width and via count), layers, vias allowed and their budget, the reference
  plane (one per layer the copper runs on), single-ended impedance, length budget, `kind="feedback"`/`"sense"`.
  On a poured rail a Path's current is its stations' joints to the pour.
- **Pair / Bus / Loop**: what holds for two paths at once is a `Pair`'s (differential impedance, skew,
  uncoupled length, gap); what times two or more is a `Bus`'s (skew, clock); a loop's area is a `Loop`'s.
  Owner, 2026-09-30: *"Call it path"* — `Path()` replaces `Chain()` outright, no alias; a `Chain()` line
  is an error stating that `Chain()` is now `Path()`.
- **Each property has exactly one subject, and the tool accepts it nowhere else.** Owner, 2026-10-01:
  *"current is path and voltage is net ... make the tool only work for that, for example no current number
  for nets."* Current (and the width and vias it implies) is never written on a net. The full table is
  `docs/physics-subjects.md`. A branch no path names carries no declared current; the SPICE stage (§5c)
  solves where current really flows and checks every piece of copper against what it can carry.
- (superseded 2026-10-01) A branch no path names gets the net's numbers, never a path's current, and is listed in the report.
  A net-wide `NetReq(amps=)` keeps meaning "every branch can carry this" until a path names a branch.
  Since `docs/physics-subjects.md` PR 3 a branch no path names is the class floor and `NetReq(amps=)` is refused.
- **The router picks the most efficient route that meets every criterion of every path.** Owner,
  2026-09-30: *"if we need to change layers and 3 vias meets criteria then we do it"* — a layer change
  on a 2 A path places the via group its current needs; the via count is a cost like length, not a
  reason to refuse.
- Two junction rules in version 1: a sense path meets its net only inside its first station's pad, and
  a path that writes `amps=` is met by the rest of its net only inside its station pads (one current
  per link). (Version 1 also sized a net's own `NetReq(amps=)` links; that left with the net's current.)
- **Pad neck.** Owner, 2026-10-01 (*"ok do 1-4"*): where a path meets a pad narrower than the path's width,
  the copper may run at no more than the pad's own width for at most about 1 mm, and must end in
  full-width copper, a via group or a pour. Every neck is listed in the report; any other copper narrower
  than its path fails. (IPC widths come from long conductors; a short neck between a wide trace and the
  part's pad dumps its heat into both — TI's TPS54202 EVM leaves SW through a pad-width neck into vias.)
- **Switch-node copper may change layer** through a via group sized for its current; its copper area stays
  priced and measured. **2 oz stackups** exist alongside the 1 oz ones. **Placement keeps a path's exit
  corridor clear** of parts it attaches with `Place(to=)`.
- Version 1 leaves out return paths on a pour, router-enforced Kelvin junction rules beyond "a sense
  path starts at its first pad", and routed `Loop()` hops (a `Loop()` is a check on named pads only).
  Also out (refutation round 1, 2026-10-01): a sense path on a poured net (refused; the pour joins its
  far pad, so it is not Kelvin). A return current in a poured ground is not declared. A poured rail's
  draws are (`docs/physics-subjects.md` PR 8): each station's joint to the pour is sized for its own draw,
  and the pour itself stays `not rated: pour`, never `ok`.
  The examples migrate to the new form (owner: *"Yes migrate"*).

## 5c. SPICE is the last stage: proof the board meets its physical requirements

Owner, 2026-10-01: *"spice is the last interaction stage for ensuring we actually meet physical
requirements. We should be able to run spice without more info besides the physical requirements we
already defined."*

- **Design stays with the formulas** (`constraints.py`: IPC-2152, the impedance formulas): they turn a
  requirement into a width directly, deterministically, with a source. SPICE does not synthesise geometry.
- **SPICE runs after emit**, on the finished board: the copper the board actually carries, extracted to
  R/L/C, plus the parts' values, driven only by what `board.py` already declares (path currents, volts,
  loops, pairs). It is an independent check, like KiCad DRC — a check we did not write the physics of.
- **No new inputs.** If a requirement cannot be checked from what is declared (an IC's internal model, an
  edge rate nobody wrote), the stage says which requirement it did not check and why; it never asks for
  or invents a number. Failures are errors under §7b (facts, no suggestions).
- **Vendor models, optimistically.** Owner, 2026-10-01: *"We should optimistically use spice models for components
  if we can find them."* The tool looks for a part's SPICE model from its manufacturer (by MPN), keeps it
  beside the part (`components/<Mfr>/<MPN>/`), and uses it when it loads and runs in ngspice. A part with no
  usable model falls back to what the requirements alone can check; the report names, per part, the model
  used (source, licence) or why there is none. The AI never has to supply one. Only manufacturer sources;
  a model file is text and is checked for control commands before ngspice reads it.
- Decided 2026-10-01: a via or track is judged by the **solved** current, never a declared net current.
- **A load nobody declared, on a part with no model** (O1, owner 2026-10-01): the solve sees only declared
  currents (Path `amps=` and draws) and the loads of parts with a usable vendor model. A branch feeding a
  model-less part's power pin that no Path declares is reported "unmodelled part, not checked", never as an
  error; the author is not required to declare every load.
  Proposed, not yet confirmed by the owner: an
  unplaned stretch at a pad takes the pad-neck allowance; capacitor models are biased at their net's declared
  `volts=`; a vendor model is not used when the part's maker or MPN differs from its LCSC listing; vendor
  model files are not committed (a hash lock is). Details: `docs/spice-stage.md`, "Defaults".
- The engine is ngspice (installed; KiCad ships the same library). The design is `docs/spice-stage.md`.

## 6. Placement: declared is authoritative, undeclared is solved

- A `Place()` the AI wrote is never moved by the tool. Greedy, file order, fail with an error —
  unchanged.
- Parts with no `Place()` are placed by the generator, and the objective is **the router itself,
  not a proxy** (judged 3–0). Measured: the channel map costs 12–70 ms per candidate and the search
  0.08–0.6 ms, so the router is a <2 % surcharge on a map any proxy also needs; an incremental map
  update runs in **2 ms median** on node with 0 mismatched gates in 81,907. This reverses the
  earlier "keep it greedy and fail" for undeclared parts only.
- First real test case, already measured: the shipped `rotate=90` on node and c3_usb is the worst of
  four rotations for the USB pair; `rotate=0` halves c3_usb's pair (20.7 → 9.3 mm) at the price of
  eight other moves. The machinery prices every alternative; nothing asks.

## 7. The AI is the driver; the generator does the bulk

The loop the tool exists for:

1. The AI writes `board.py`.
2. `pcbc build` places, routes, writes `layout.gen.py`, emits, and either passes every gate or
   fails with **errors** — each naming the file:line, the object and the measured numbers, never "look at the
   picture".
3. The AI reads `layout.gen.py` and the errors. Where judgement is needed — the connector escape, which
   way a pair goes, the micro's rotation — it copies a gen line into `layout.core.py` (or edits
   `board.py`) and rebuilds. The generator fills in around the locks.

The measurements say this split is right: the rule-based router handled the bulk of buck and failed
exactly where judgement is needed.

## 7b. Errors, not suggestions

Owner, 2026-10-01: *"Remove suggestions in tool, just throw errors if unable to complete."* When the tool
cannot complete something it stops with an error that states **what** failed and **why**, as facts: the
file and line, the part, pad, net or link, the measured numbers (width needed, gap available, current,
distance), and the object in the way. It never proposes an edit — no rewritten line to paste, no "try
`vias=True`", no `Place()` to change, no value to lower. Deciding the fix is the AI's job. A warning is the
same: facts about a placement the tool did not refuse, no proposal. An error must be **true**: it names the
real cause, never a guess (an error that blames the wrong part is a defect).

## 8. How anything gets believed

The recurring defect in this project is not geometry, it is instrumentation: a check that measures
something adjacent to what it claims. Standing rules:

- Only a KiCad-backed check earns the word `verified`.
- Every gate judges the **emitted** board. Proven by mutation: corrupt one object in
  `layout.gen.py` and the build must fail, naming the line and field.
- Every number carries the command that produced it. A moved recorded number needs its measurement
  and its sentence.
- Nothing built from a spike or a panel is believed until independent verifiers have tried to refute
  it with their own comparators, not the implementer's.
- Determinism byte for byte, including from two different build paths.
- A serialized routing-problem file is written next to every refusal (borrowed from tscircuit), so a
  failure becomes a fixture in one command.

## 9. Where things stand (2026-09-25)

**Step 1 finished and verified (bridge):** KRT still routes; its board is decompiled to `layout.gen.py`
and the final board is emitted from that Python. Seven adversarial rounds; 1011 tests green under
KiCad+KRT (independently re-run). Mechanisms that carry over to the native generator unchanged: every
KiCad field as a Python field, the emitter, load-time validation of core lines by a KiCad round trip,
stamps built by tracing what each stage opens, every gate on the emitted board, fab equality with exact
fills. Open small fixes: a timeout on every kicad-cli call (a 1e20 text angle hangs DRC), refuse a
rotated text box (crashes KiCad), classify "written by this stage" by what the stage wrote not by mtime,
stamp KRT by a hash of its source, check the fill the Gerber export makes. Open owner question: may
`board.py` / `layout.core.py` do I/O beyond importing sibling modules (recommended: no).

**Next: the native generator** — placement and routing that read the Python model and write Python
objects, with the cost-field router (§5) as its starting point.

### Earlier status (2026-09-23)

**Built and independently checked:** step 1 of §1 — route as before, decompile to `layout.gen.py`,
emit from Python, every gate on the emitted board, Seg/Via locks working, byte-identical builds from
different paths, 834 tests green under KiCad+KRT.

**Open, each with a reproduction, fix round running:** fab's ampacity gate still reading the router's
sidecar; core able to invent nets; the fab copy of the board losing Python uuids; two unmapped zone
fields (per-layer hatch offset, arcs inside polygon outlines); a pour lock on the plane net not
holding against KRT's pour step; a refused route leaving the build stuck until `--force`; a dangling
core via not traced to its line.

**Next:** verify the three router spikes adversarially; then step 2 — the generator becomes the
router, KRT swapped out underneath without changing the file format; then solved placement.

## Superseded

- `docs/fp-router-plan.md` §3 (corridor phase A) and its stage list built on it — see §5. Its §2
  (what pcbc does today, measured) and §6 open problems remain useful; §6.1's "29 %" has no source
  (measured 11.4 %, 6.6 % with the per-face test) and §6.2's ds2 figure does not reproduce (buck
  13/21 at 44.2 unrelaxed, ds2 37/51 at 30.1; relaxed, both beat the shipped boards).
- `docs/roadmap.md` item order — the layout language and the generator come before hierarchy and
  capability limits.
- Grok's session plan (`~/.grok/sessions/.../01a09872-.../plan.md`, 2026-09-22): its three-file model
  is adopted; its "out of scope: reusing route_geom / route_scene / route_relax / patterns" is
  rejected (the owner asked for the opposite, and those modules are the clearance judge and the
  embedder); its one-pass L-bend generator with no search is rejected (it would route almost nothing).
- Grok's `build.py` edit that disconnected the router and deleted the gates — restored in step 1.
