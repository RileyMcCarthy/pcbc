---
name: pcbc
description: Design a PCB with pcbc (board.py → KiCad 10 project → JLCPCB package). Use when asked to make, place, route, or fab a board, fetch parts, or fix a pcbc report.
---

# Designing a board with pcbc

pcbc is a compiler. You write one `board.py`: nets, parts, intent, and where things go.
The tool wires the schematic, places and routes the copper, and KiCad itself judges the
result. **You never draw a wire or a track, and you never decide anything from a picture.**
Every stage prints a numbered list of moves; you edit `board.py` until the list is empty.

## The loop

```bash
pcbc search "TPS54202"            # LCSC hits: id, package, stock, price, basic/extended
pcbc fetch C191884 --into components   # → components/<Mfr>/<MPN>/{part.py, .kicad_sym, .kicad_mod, .step}, scored
pcbc check board.py               # loads it: unbound pins, missing lands, bad parts, bad relations
pcbc sch board.py                 # draws the sheet, proves the netlist with kicad-cli, ERC, lists moves
pcbc pcb board.py                 # places the copper, lists moves
pcbc build board.py --force       # check → seed → sch → place → route (KRT) → copper gate → fab
pcbc review board.py              # one HTML page: schematic, copper, 3D, BOM, notes
```

Run them in that order. Do not skip `check`: it names the mistake before anything is drawn.
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

1. Nets: `Power("3V3")`, `Ground("GND")`, `Net("SDA")`. `Net(wire_mm=)` allows a longer wire.
2. Parts: `MCU = load("components/Espressif/ESP32-C3-MINI-1-N4")`, then
   `MCU("U1", **{"3V3": V33}, GND=GND, EN=EN, ...)`. Every non-optional pin gets a net.
3. `Board(width=60, height=45, layers=4, stackup="jlcpcb_4l_1oz", planes=[("GND", "In1.Cu"), ("3V3", "In2.Cu")])`.
   The stackup is the one source of the fab's limits (track, clearance, vias, rings, holes).
4. Intent: `NetReq("VBUS", "3V3", "GND", kind="power", volts=5, amps=1)` sets track widths;
   `kind="switch_node"` and `kind="analog"` route first, on one layer, without vias, and are
   held to `max_mm`; `NetReq("USB_DP", "USB_DN", kind="usb_hs", z_diff_ohm=90, pair=True)`
   routes as a pair.
5. Schematic: `SchRegion("mcu", left=, top=, width=, height=)` and `SchPlace(...)`.
6. Copper: `Place(...)`.

## Placement: anchors by CSS, everything else by relation

Schematic:

```python
SchPlace("U1", parent="mcu", left=150, top=20)      # an IC or connector: CSS
SchPlace("C1", to="U1.3V3")                         # the pin of C1 on that net, in line with it
SchPlace("C2", along="C1.1", side="bottom")         # hang under that node, turned to face it
SchPlace("SW1", along="C2.1", side="left", align="U2.EN")
```

Copper:

```python
Place("J1", edge="bottom")                                   # a connector on that edge, face out
Place("U1", position="absolute", left=1, top=1, rotate=90)   # an anchor: CSS
Place("C_MCU_HF", to="U1.3V3")                               # right outside U1, on the side the pin faces
Place("C_MCU", to="U1.3V3")                                  # the next cap on that pin sits beside the first
Place("D1", to="R_LED.2", toward="right", gap=0.5)           # toward= picks the side; gap= the clearance
```

Rules the tool applies, so you do not have to:
- The tool picks pose, distance and side. Give a number only for an anchor.
- **File order decides, and nothing else**: the first `Place()` written gets the closest spot.
  List the small decoupling cap before the bulk one, and the decoupling before bigger parts.
- A part with no clear spot stays where the collision is and is reported; move a neighbour
  or give it `toward=`. Nothing is silently shoved out of the way.
- Fiducials are placed by the tool in free corners after the anchors. Leave the corners.
- A fine-pitch row (0.65 mm TSSOP, 0.5 mm QFN) keeps a fanout lane outside it (about 1.3 mm
  on two layers) where the tool drops an escape via on every pin before routing; parts placed
  `to=` its pins settle straight out past the lane, never beside the row, and a decoupling cap
  there is allowed that much further. Do not fight it with `gap=` or `toward=`.
- One `Place()` and one `SchPlace()` per ref. A second one is refused, not an override.

## Reading the reports

Each line is a move: what collides, and the `Place()`/`SchPlace()` edit that fixes it.
- `pcbc sch`: overlaps, wires through text, a part that had to slide far, a symbol boxed in.
  `style:` notes (a supply symbol that had to point down) are legal and not counted.
- `pcbc pcb`: courtyard overlaps, a part off the board, a part in a fine-pitch row's fanout
  lane, a decoupling cap farther than 2.5 mm (the next one 5 mm; plus the lane on a fine-pitch
  row), a connector off every edge, copper within 0.3 mm of the edge, a net past its `max_mm`.
- `pcbc build`: the copper gate is KiCad's own DRC with nothing unconnected, plus the pads
  bound exactly as `board.py` says. A routing failure names the nets; move the parts on them
  closer or give them a free side. Do not tune router flags.

The examples stay at zero moves; hold a new board to the same bar before `build`.

## Do not

- Edit a `.kicad_sch`, `.kicad_pcb` or `.kicad_pro` by hand. `pcbc build --force` rewrites them.
- Decide from a render. Renders are for a human at the end (`pcbc review`); the reports and
  the tests decide. If a report misses something a render shows, that is a missing rule in
  the tool: add the rule and its test, do not hand-place around it.
- Run `kicad-cli pcb drc` on a `.kicad_pcb` copied without its `.kicad_pro` and `.kicad_dru`:
  KiCad then judges by its defaults and shows phantom errors.
- Leave the routed directory dirty between experiments; `pcbc build` cleans it, ad-hoc runs do not.

## When you change the tool

- A rule is a test first (`tests/test_rules.py`, `test_pcb_place.py`, `test_copper_rules.py`,
  `test_route_plan.py`) and a row in the README tables; `docs/copper-plan.md` keeps the
  seen/now table of what broke and what the tool does about it. Add to it.
- `PCBC_REQUIRE_KICAD=1 PCBC_REQUIRE_KRT=1 pytest` must stay green. KRT lives at
  `KRT_HOME` (default `~/Downloads/KiCadRoutingTools`, pinned to `pcbc.route.KRT_SHA`).
- Everything is deterministic: the same `board.py` gives the same files byte for byte. Keep it so.
- Map: `language.py` (the DSL) → `circuit.py` (check) → `sch_place.py`/`sch_emit.py` (sheet) →
  `pcb_place.py` (copper placement) → `route.py` (the KRT plan from `compile.py`'s classes) →
  `netcheck.py` (both gates) → `fab.py`. Parts: `source.py`; fab limits: `stackup.py`.
