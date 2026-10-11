---
name: pcbc
description: Design a PCB with pcbc (board.py → KiCad 10 project → JLCPCB package). Use when asked to make, place, route, or fab a board, fetch parts, or fix a pcbc report.
---

# Designing a board with pcbc

pcbc is a compiler. You write one `board.py`: nets, parts, intent, and where things go.
The tool wires the schematic, places and routes the copper, and KiCad itself judges the
result. **You never draw a wire or a track, and you never decide anything from a picture.**
Every stage prints a numbered list of errors and warnings: facts (the file and line, the part,
pad, net or link, the measured numbers, what is in the way), never an edit. Deciding the edit is
your job; you edit `board.py` (or lock copper in `layout.core.py`) until the list is empty.

## The loop

```bash
pcbc search "TPS54202"            # LCSC hits: id, package, stock, price, basic/extended
pcbc fetch C191884 --into components   # → components/<Mfr>/<MPN>/{part.py, .kicad_sym, .kicad_mod, .step}, scored
pcbc check board.py               # loads it: unbound pins, missing lands, bad parts, bad relations, a NetReq line that does not compile
pcbc check board.py --constraints # every number a NetReq/Pair/Bus line became, one per line with its source
pcbc sch board.py                 # draws the sheet, proves the netlist with kicad-cli, ERC, lists warnings
pcbc pcb board.py                 # places the copper, lists warnings (--constraints adds the numbers it was checked against)
pcbc build board.py --force       # check → sch → place → route (native, in Python) → emit → gates → fab
pcbc review board.py              # two pages under layout/<board>/review/: index.html (full: tabs, interactive 3D) and static.html (pictures, no script)
```

`pcbc review` prints both paths (`html`, `static_html`). Hand the owner **both** as artifacts: static.html is
the one a phone app that runs no JavaScript shows whole (schematic, copper, silk, four 3D renders, BOM, a status
header); index.html is the full, interactive page.

Run them in that order. Do not skip `check`: it names the mistake before anything is drawn.
**Read `pcbc check --constraints` before placing**: it prints the width, clearance, via,
pair geometry, keep-away and loop budget every net got, with the formula and the standard
behind each, so you place knowing what the copper will need (a 0.78 mm VIN track wants a
channel; an analog net has no vias and one layer; a pair has a reference plane).
`pcbc sch` and `pcbc pcb` are the inner loops; `--json` on either gives every part's pose.

## Parts

- A library part is a directory with `part.py`, one `.kicad_sym`, one `.kicad_mod` (and a
  `.step`). `pcbc fetch` makes one from an LCSC id and scores it; `pcbc score <dir>` scores
  anything else (KiCad library, SnapEDA). Use `good` or `usable`; a `bad` part is refused
  by `check`, so pick another candidate or fix the library.
- `part.py` carries a `# Pins:` comment with the pin **names**. Bind pins by those names.
  Never open the `.kicad_sym` to find them, never guess them.
- Never mix a symbol from one source with a footprint from another. The pin numbers must
  be the pad names; the scorer checks it (the USB-C in the examples once had `A1B12` pins
  over `A1`/`B12` pads: the schematic was right and the copper would have lost VBUS and GND).
- Generics: `Resistor("R1", "10k", package="0402", mpn=..., lcsc=..., p1=NET, p2=NET)`,
  `Capacitor(...)`, `Led("D1", "red", ..., a=, k=)`. Lands are vendored for 0402/0603/0805/1206.

## board.py, in order

1. Nets: `Power("3V3")`, `Ground("GND")`, `Net("SDA")`. (A net's schematic wire limit is `SchNet("SDA", wire_mm=)`
   in `schematic.core.py`; `Net(wire_mm=)` is refused.)
2. Parts: `MCU = load("components/Espressif/ESP32-C3-MINI-1-N4")`, then
   `MCU("U1", **{"3V3": V33}, GND=GND, EN=EN, ...)`. Every non-optional pin gets a net.
3. `Board(layers=4, stackup="jlcpcb_4l_1oz", planes=[("GND", "In1.Cu"), ("3V3", "In2.Cu")])`: physics only, no size.
   The outline is geometry: with none declared it is derived from the placement and the routing; a fixed one (an
   enclosure, a mating board) is `Outline(width=, height=, at=(0, 0))` in `layout.core.py`.
   The stackup is the one source of the fab's limits (track, clearance, vias, rings, holes).
   2 oz outer copper: `jlcpcb_2l_2oz`, `jlcpcb_4l_2oz` (JLC04162H-7628), `jlcpcb_4l_2oz_3313` — every
   current width then derives from 2 oz (buck's 2 A: 0.781 mm at 1 oz, 0.391 mm at 2 oz) and its line
   names the stackup; JLC's 2 oz limits apply (0.16/0.16 mm on 2 layers, 0.15/0.15 on 4, mask 0.20).
4. Intent, one line per net or group. You say what the net is; the tool derives the numbers
   from the stackup and the standards and prints them (`pcbc check --constraints`):

   ```python
   NetReq("VBUS", "3V3", kind="power", volts=5)                       # clearance row; a node carries no current
   NetReq("GND", kind="power")                                         # a Ground() net takes no volts=
   NetReq("HV+", "HV-", kind="power", volts=300)                       # clearance and creepage rows
   NetReq("SW", kind="switch_node")                                    # span, hot loop budget, routed first
   NetReq("FB", "AIN0", kind="analog", keep_clear_of="SW", keep_clear_mm=3)  # a keep-away is node to node
   NetReq("SDA", "SCL", kind="i2c", pf_max=400)                        # length from capacitance
   Pair("USB_DP", "USB_DN", kind="usb_hs", z_diff_ohm=90)              # 90 ohm +-15 %, skew 0.5, uncoupled 2
   Bus("SCK", "MOSI", "MISO", kind="spi", clock="SCK")                 # matched to the clock
   Bus("D0", "D1", "D2", "D3", match_mm=1.0, clock="CLK")
   Path("VDDA", "J2.1", "C4.1", "U1.12")                               # feed order: cap before pin
   Path("5V", "L1.2", "C_OUT1.1", "J_OUT.1", amps=2)                   # a current on these links only
   Path("5V", "U1.OUT", "U2.VDD", "J_OUT.1", amps={"U2.VDD": 0.3, "J_OUT.1": 1})  # draws: each link carries what is drawn beyond it
   Path("3V3", "U2.VOUT", "J_LOAD.1", amps=1)                          # on a pour: each station's joint is sized for its draw
   Path("5V", "C_OUT1.1", "R_FB_TOP.1", kind="feedback")               # a thin tap leaving the rail at the cap
   Loop("C_IN1.1", "U1.VIN", "U1.GND", "C_IN1.2", mm2=20)              # a hot-loop area check on named pads
   Isolation("primary", "secondary", volts=250, across=("U7",), slot=True)  # two Regions, the isolators that span them
   Guard("AIN0", stitch_mm=2.5)
   ```

   Net kinds: `generic`, `power`, `switch_node`, `analog`, `clock`, `i2c`; Pair kind `usb_hs`; Bus kind
   `spi`; Path kinds `feedback`, `sense`. **Never type a number the report can derive**: no widths, clearances,
   gaps, via sizes or lengths. Give what only you know (`amps=`, `volts=`, `keep_clear_of=`,
   `clock=`, `pf_max=`, a `Path` order, an `Isolation` voltage) and read the number back. A
   kwarg the kind does not take, a typo (`amp=`), an unknown kind, a `Path` pad off its net,
   an `Isolation` side that is not a `Region`: each fails `check` naming the line. Nothing is
   dropped silently. `max_mm` is an airwire budget for placement, never a KiCad rule;
   `length_mm=` is the routed-length rule.

   **One home per property** (`docs/physics-subjects.md`). `NetReq` says what is the same at every
   pad of a node (kind, `volts`, span `max_mm`, the keep-away, `pf_max`, `rise_ps`, `class_name`).
   `Path(net, *pads, ...)` says what belongs to the copper between named pads, in feed order:
   `amps=` (a number, or `{station: A}` draws), `temp_rise_c=`, `vias=`, `vias_max=`, `layers=`,
   `reference=`, `z_se_ohm=`, `length_mm=`, `kind="feedback"|"sense"`. A current is always a Path's:
   a net carries none, and a written `NetReq(amps=)` is refused naming every pad it covers. `Pair`
   holds what holds for two paths (`z_diff_ohm`, `match_mm`, `uncoupled_mm`, `layers`, `reference`,
   `length_mm`, `vias`, `vias_max`), and a `NetReq` naming both nets of a Pair (only those, kind
   generic) gives them their node facts (`volts`, `max_mm`, ...); `Bus` what times several (its
   `length_mm` is each member's; a member Path writes none); a net is timed by one Pair or one Bus;
   `Loop(mm2=)` a loop's area. A pair or bus member with three or more pads has exactly one
   Path. A `kind=` Path naming every pad of its net is that whole node's (a two-pad sense line). A sense path is written from where it leaves the rail; the tool refuses it written
   backwards. A pad no path names gets the class floor and the report says what its branch
   carries. A layer change on a current path places the via group the current needs; more vias
   are a cost, never a refusal. Where a path meets a pad narrower than its width the router may
   leave through a **pad neck** — at most the pad's own width, at most 1.0 mm past its edge
   (`PAD_NECK_MAX_MM`), into full-width copper, a via or a pour — only when no full-width route
   exists; every neck is listed (route step `necks`, `FAB_NOTES.md`) and any other narrower copper on a
   path fails. A switch node may change layer through the via group its current needs. `Chain()` is gone: a `Chain()` line is refused, stating that `Chain()` is now `Path()`.
   `Loop(*pads, mm2=)` checks the area named pads enclose; it lays no copper. On a poured rail a
   `Path` with `amps=` is its stations' joints: the pour is the copper between them (`not rated:
   pour`), an SMD station's tap gets the vias its draw needs and a through-hole station's barrel is
   rated (E23). Not in version 1: a `Path` with `amps=` on a poured ground (a return) or with `kind=`
   on a poured net (a Kelvin tap through the pour) is refused.
5. Schematic: nothing. The tool lays the sheet out from the netlist and writes every pose to
   `layout/<name>/schematic.gen.py`. Clean it up in `schematic.core.py` beside board.py (below); any `Sch*` line in
   board.py or layout.core.py is refused naming that file.
6. Copper: `Place(...)` — in `layout.core.py`, next to board.py, never in board.py. Every geometric decision
   lives there: `Place`, `Region`, `Keepout` and a declared `Outline()`. A `Place`, `Region`, `Keepout` or a
   `Board()` size in board.py is refused naming its line. To lock a pose from `layout.gen.py`, its `Place` line
   **replaces** that part's `Place` line (a part takes one); a fiducial's `Place`, a `pcbc:` group, the Edge.Cuts
   outline and a keepout's rule area cannot be locked.

## Placement: anchors by CSS, everything else by relation

Schematic, in `schematic.core.py` (optional; read `layout/<name>/schematic.gen.py` and the `vector:` line of
`pcbc sch` first, then lock only what you want kept — a gen `Sch*` line copied verbatim is a valid core line; the
`Sheet*` lines after `# The sheet` are the drawn KiCad primitives the sheet is emitted from, never locks, and one copied
into core is refused):

```python
SchRegion("mcu", title="MCU", members=("U1", "C_MCU", "R_EN"))  # titled group; closed membership
SchRegion("inputs", title="Inputs", col=0, members=("J2", "R6"))  # reading order: its grid column
SchPlace("C1", to="U1.3V3")                         # the pin of C1 on that net, in line with it
SchPlace("C2", along="C1.1", side="bottom")         # hang under that node, turned to face it
SchPlace("SW1", along="C2.1", side="left", align="U2.EN")
SchPlace("U1", at=(48.26, 29.21), rotate=0)         # a pose in its declared region's frame (1.27 mm grid)
SchPower("VSS", pin="U1.AVSS", side="left", stub=12.7)  # a power symbol's stub; jog=, rotate=180 too
SchLabel("EN", pin="R_EN.2", side="left", stub=2.54)     # a stub label: that pin takes no wire
SchText("R_EN", "Reference", at=(2.54, -1.27))      # a field as seen, relative to the symbol
SchSheet(paper="A3", wire_mm=25.4); SchNet("SDA", wire_mm=0)
```

A sheet with a hard readability number above 0 (body overlap, text collision on KiCad's SVG, wire through text, part
off the paper, region frames overlapping, a wire over a pin's drawn line) fails the sch stage — `pcbc sch` and
`pcbc build` both exit 1 — naming the objects. Editing schematic.core.py redraws the sheet only.

Copper, in `layout.core.py`:

```python
Place("J1", edge="bottom")                                   # a connector on that edge, face out
Place("U1", position="absolute", left=1, top=1, rot=90)      # an anchor: left/top (or at=) are frame coordinates
Place("C_MCU_HF", to="U1.3V3")                               # right outside U1, on the side the pin faces
Place("C_MCU", to="U1.3V3")                                  # the next cap on that pin sits beside the first
Place("D1", to="R_LED.2", toward="right", gap=0.5)           # toward= picks the side; gap= the clearance
```

Rules the tool applies, so you do not have to:
- The tool picks pose, distance and side. Give a number only for an anchor.
- **File order decides, and nothing else**: the first `Place()` written gets the closest spot.
  List the small decoupling cap before the bulk one, and the decoupling before bigger parts.
- A part with no clear spot stays where the collision is and is reported (what is in the way).
  Nothing is silently shoved out of the way.
- Fiducials are placed by the tool in free corners (with no `Outline()`, last, and the outline grows to keep
  them 4 mm from the edge). Leave the corners.
- **No `Outline()`** (the default): the outline is derived. An anchor is written in frame coordinates (`left=`,
  `top=`, `at=`); `right=`, `bottom=`, `margin: auto` and `%` need an `Outline()` and are refused without one. Each
  `edge=` connector and its `to=` tree is docked against the interior and fixes that side; a side the keepouts reach
  is fixed at the keepout; the free sides are fitted after routing. The build's `outline:` line names what set
  each side; to compact the board, move the part or copper it names. Freeze it with the line's numbers:
  `Outline(width=, height=, at=(x, y))`.
- A fine-pitch row (0.65 mm TSSOP, 0.5 mm QFN) keeps a fanout lane outside it (about 1.3 mm
  on two layers) where the tool drops an escape via on every pin before routing; parts placed
  `to=` its pins settle straight out past the lane, never beside the row, and a decoupling cap
  there is allowed that much further. Do not fight it with `gap=` or `toward=`.
- One `Place()` (layout.core.py) and one `SchPlace()` (schematic.core.py) per ref. A second one is refused, not an
  override.
- A part placed `to=` a pin keeps its copper out of the **exit corridor** of that part's other pads a
  `Path()` starts or ends on (path width, or the neck's at the pad, plus two clearances, straight out
  along each exit, on the pad's own copper layers, as far as the path needs to turn: the neck's end
  plus half its width and its clearance): buck's C_IN1 sits right of U1, not under the SW pin. Your
  CSS/absolute/edge `Place()` is never moved; if parts you placed close every exit of such a pad,
  placement stops with an error naming each side, the pads in it, their `Place` lines and distances.

## What the constraint report is, and what each number reaches

`pcbc check board.py --constraints` prints one number per line with where it came from: a
formula name, the stackup it was evaluated on, the JLC row it was fitted to, or `preset <kind>`
when pcbc chose it with nothing behind it. Read it before placing; never type a number it can
derive. Three things about scope, so you do not expect more than the tool does:

- `[soft: warning in R1]` marks a rule KiCad writes as a warning. It is counted, pinned per
  example and never fails the gate; a soft rule is promoted to an error in the PR that shows it
  hitting zero on every example. `width_usb` cannot reach zero yet: the router quantises the
  pair 0.0004 mm under its class.
- `spacing 3W` and `N per layer change` are printed and nothing consumes them: they are
  intent for the router of R2/R3, not a check.
- A `Path` line is read three times: at placement (order along the feed, what sits in each link's
  corridor at its own width, its airwire and keep-away), by the router (station links first, each
  at its own width per layer, a sense path met only at its first pad, an `amps=` path met only at
  its stations, a via group at a layer change), and on the emitted board (each link's width per
  layer, vias, layers and via groups are gated, and so are the junctions; copper you lock in
  `layout.core.py` is gated exactly like the router's).

## Reading the reports

Each line is a fact: what collides or fails, by how much, and the line that put each thing there.
No line proposes an edit (`docs/direction.md` §7b).
- `pcbc check --constraints`: one number per line, sorted by net, the source in parentheses:
  `VBUS: path J1.VBUS -> U2.VIN (Path line 147): width 0.4 mm on F.Cu (pcbc_floor amps >= 0.2; ...)`,
  `USB_DP: pair with USB_DN, 0.2288 mm wide, gap 0.15 mm on F.Cu over In1.Cu (GND): 90 ohm (hj_coupled_microstrip x 0.85 JLC04161H-7628; ...)`.
  `pcbc default` is a number with no standard behind it; `uncalibrated` / `formula only` is a
  formula off JLC's published rows; `[soft: warning in R1]` is a rule KiCad reports as a
  warning, counted by the copper bar; `[report only in R1]` (vias per layer change) is
  printed and not gated. A caveat is a line, not a failure: a 2-layer USB pair says it cannot
  reach 90 ohm (`within the USB full speed tolerance, outside the high speed one`);
  `FB: keep_clear_of none; this board has a switch node (SW)` says no keep-away is held. The last lines are `classes: ...` (what the `.kicad_pro` gets)
  and `rules: N written (E error, S soft), canary on net X` (what the `.kicad_dru` holds).
- `pcbc sch`: overlaps, wires through text, a part that had to slide far, a symbol boxed in.
  `style:` notes (a supply symbol that had to point down) are legal and not counted.
- `pcbc pcb`: courtyard overlaps, a part off the board, a part in a fine-pitch row's fanout
  lane, a decoupling cap farther than 2.5 mm (the next one 5 mm; plus the lane on a fine-pitch
  row), a connector off every edge, copper within 0.3 mm of the edge, a net past its `max_mm`;
  and, from the constraints, before any copper exists: a net's airwire over its budget, a pair
  or bus whose members differ by more than `match_mm`, a `Path` out of order or a stub on a
  high-speed net, no channel for a wide track (the pinch is named), a hot loop over its
  budget, a keep-away too close, a controlled pad off its reference plane, an `Isolation`
  gap too narrow or a part on the wrong side. Every one names refs, pins, the numbers and the
  `Place()` lines that put the parts there; none proposes an edit, a pose or a budget.
- `pcbc build`: the copper gate is KiCad's own DRC with nothing unconnected, plus the pads
  bound exactly as `board.py` says. A routing failure names the net, then one line per
  unreached pad: what is in its corridor, which step put it there, whose part it belongs to,
  and the rules that bound the link (`Rules: vias are not allowed on this link (preset
  switch_node); layers F.Cu (...)`). Do not tune router
  flags. The `copper:` lines are the bar: detours, vias, staircases; a long detour on a net
  usually means its parts sit on the wrong side of something.

The examples stay at zero warnings, with one recorded exception: buck's switch path (`Path("SW", "U1.SW", "L1.1", amps=2)`,
0.781 mm on F.Cu, no vias) has no channel past C_IN1 as placed, and `pcbc pcb` says so naming
Place line 72; the build leaves SW open with the same facts. Hold a new board to the zero bar before `build`.

## Do not

- Edit a `.kicad_sch`, `.kicad_pcb`, `.kicad_pro` or `.kicad_dru` by hand. `pcbc build --force` rewrites them.
- Type a number the report can derive. A track width, a clearance, a pair gap, a via size, a
  creepage distance: `NetReq(kind=, volts=)`, `Path(amps=)` and the stackup decide them and
  `pcbc check --constraints` prints them. If the printed number is wrong, the fix is in
  `stackup.py` / `constraints.py` with a test vector and its reference, not a literal in
  `board.py`. `class_name=` and `layers=` are intent; `z_diff_ohm=90` is intent; `0.2288` is not.
- Decide from a render. Renders are for a human at the end (`pcbc review`); the reports and
  the tests decide. If a report misses something a render shows, that is a missing rule in
  the tool: add the rule and its test, do not hand-place around it.
- Run `kicad-cli pcb drc` on a `.kicad_pcb` copied without its `.kicad_pro` and `.kicad_dru`:
  KiCad then judges by its defaults and shows phantom errors.
- Leave the routed directory dirty between experiments; `pcbc build` cleans it, ad-hoc runs do not.

## When you change the tool

- A rule is a test first (`tests/test_rules.py`, `test_pcb_place.py`, `test_copper_rules.py`,
  `test_route_plan.py`, `test_constraints.py`, `test_dru.py`, `test_route_checks.py`) and a
  row in the README tables; a formula is a vector in `test_stackup.py` with its reference in
  the assertion message and a row in `docs/constraints.md`; every report message is asserted
  as an exact string. `docs/copper-plan.md` keeps the seen/now table of what broke and what
  the tool does about it. Add to it.
- A number a `NetReq` becomes lives in one place: `constraints.py` derives it, `dru.py` writes
  the KiCad rule from it, `route_checks.py` checks placement against it, the report prints it.
  Never add a second copy in a consumer; read `job.constraints.by_net(name)`.
- `PCBC_REQUIRE_KICAD=1 pytest` must stay green. There is no external router: placement and
  routing are pcbc's own Python and no KiCad board file exists before emit (`tests/test_native.py`).
- Everything is deterministic: the same `board.py` gives the same files byte for byte. Keep it so.
- Map: `language.py` (the DSL) → `circuit.py` (check) → `sch_place.py`/`sch_emit.py` (sheet) →
  `pcb_place.py` (copper placement) → `place_native.py` (the placement as layout objects) →
  `route_native.py` (patterns, planes, `route_cost.py` router, `route_pair.py` pairs) → `layout_job.py`
  (gen, emit) → `netcheck.py` (both gates) → `fab.py`. Parts: `source.py`; fab limits: `stackup.py`.
