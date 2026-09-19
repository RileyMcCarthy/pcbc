# pcbc

**Python in, fab-ready KiCad out.** One AI-writable board file compiles to a KiCad 10 project and a JLCPCB zip.

Zener is not used. There is no `SOURCE.json`, no `pcb.toml`, no pin lockfile.

```
pcbc build board.py           check → seed pcb → sch → place → route → fab
```

## One fact, one file

| Fact | Lives in |
|---|---|
| Nets, MPN, LCSC, Place, board size | `board.py` — every part needs PCB `Place()` and schematic `SchPlace()` |
| Pin **names** → pad numbers + artwork | `.kicad_sym` |
| Land | `.kicad_mod` |
| IC MPN / LCSC / which CAD files | `components/…/part.py` (no `pins=`) |

Generics (`Resistor("R1", "1k", package="0402", …)`) use vendored KiCad chip lands. Footprint properties: `Value` = electrical, `Mpn` = reel, `LCSC` = JLC code.

Schematic symbols come from `.kicad_sym` **as-is** (no compact rewrite). `SchPlace` is how an AI lays them out — it only says *where*, never what connects:

```python
SchPlace("U1", parent="mcu", left=150, top=20)   # CSS: ICs / connectors
SchPlace("C1", to="U1.3V3")                      # the pin of C1 on U1.3V3's net, in line with it
SchPlace("C2", along="C1.1", side="bottom")      # hang under that node, turned to face it
SchPlace("SW1", along="C2.1", side="left")       # beside it, facing it
SchPlace("U3", to="J1.DP1", side="bottom")       # an IC parks on a face, upright
SchPlace("C3", along="C1.1", side="bottom", align="U2.EN")  # hang it so its pin sits on U2.EN's row
```

Parts keep out of each other: a part's keepout is its body plus the power symbols its pins will need, and an attached part that lands in another's keepout is shoved along its own attach axis until it clears everything (its wire just gets longer). Wires between symbols are short runs only — `SchStyle(wire_mm=25.4)` sets the sheet default, `Net("X", wire_mm=0)` a per-net cap (0 = labels only) — so a net that should be labelled never gets a wandering wire.

The tool picks the distance (room for the net's label on the wire) and the pose — a 2-pin part turns or mirrors so the attached pin faces its target, a bigger symbol only mirrors, text stays readable either way. `gap=`, `rotate=`, `mirror="x"|"y"` override; `pin=` is only for hanging a pin that is *not* on the target's net.

Library `.kicad_sym` artwork is used as-is. Collision uses the real symbol (graphics, pin text, Reference/Value).

The AI never draws a wire. pcbc wires every net from `board.py`: pins of one symbol in a line share a rail (a bus bar just outside the pins when others sit between); a signal net gets a short Manhattan wire only where it crosses nothing, else labels; a supply net gets a wire only as one short straight segment, else a power symbol per pin. Wires never cross — a crossing reads as a connection — so what cannot be drawn cleanly is named instead. Every connected group is then named: a label on signal nets, a power symbol on `Power()`/`Ground()` nets. Labels, power symbols and each passive's Reference/Value then take the spot that overlaps least of what is already drawn. `pcbc build` asks `kicad-cli sch export netlist` for what KiCad actually reads and fails the `sch` stage if it is not exactly the board's netlist (`tests/test_netcheck.py` does the same). Placement changes how it looks, never what it connects.

Power symbols and labels are placed after the wires, each where it touches nothing already drawn, in three passes so drawing order does not decide who gets the clean spot; two symbols on one part (an LDO's GND between VIN and EN) are placed as a pair. What KiCad actually connects was checked with `kicad-cli`, not assumed: a wire ending part-way along another wire does not connect (no junction), a symbol pin sitting on a wire's interior does not connect, a label anchored on a wire or a pin end does. pcbc only ever draws the connecting kinds.

`pcbc sch board.py` is the inner loop: it draws the sheet, proves with `kicad-cli` that it connects exactly what `board.py` says, and prints what still collides as a numbered list of moves — edit the `SchPlace()` lines, run it again. `--json` adds every part's pose. `pcbc check` catches a `SchPlace` that names a missing pin or hangs a part off a pin it shares no net with, before anything is drawn. The output is deterministic: the same `board.py` gives the same `.kicad_sch` byte for byte (ids are keyed by what they name), so the report never moves between runs and the file does not churn in git.

### Drawing rules

What the sheet is held to, and the test that pins each one (`tests/`):

| Rule | Test |
|---|---|
| The sheet connects exactly `board.py`'s netlist, as `kicad-cli` reads it | `test_netcheck.py::test_kicad_reads_the_board_netlist` |
| Only connecting geometry is drawn: no wire ends on another wire's interior, no symbol pin on a wire's interior, no T without a junction | `test_rules.py::test_only_connecting_geometry` |
| Wires never cross | `test_rules.py::test_no_two_wires_cross` |
| Wires are short runs; past `wire_mm` a net is joined by labels | `test_sch.py::test_wire_limit_per_net_and_sheet_default` |
| A supply net is wired only as one short straight segment, else a symbol per pin; ground is always symbols | `test_rules.py::test_power_is_symbols_or_one_short_wire` |
| Net names lie along their wire, on top of it, never hung beneath | `test_rules.py::test_labels_sit_on_their_wire`, `test_sch.py::test_wire_label_sits_mid_wire_not_at_the_pin` |
| A power symbol never sits on its own jog; a ground points down and a supply up unless every upright spot collides | `test_rules.py::test_symbols_upright_where_they_can_be` |
| Symbol bodies never overlap; a part keeps out of the room another's symbols need | `test_c3_usb.py::test_c3_usb_sch_no_symbol_overlap`, `test_sch.py::test_keepouts_shove_along_the_attach_axis_without_marching` |
| Attached pins line up with their target, and the tool picks the gap | `test_sch.py::test_attach_picks_the_pin_on_the_target_net`, `test_gap_defaults_to_room_for_the_label`, `test_align_lands_the_hanging_pin_on_another_pins_row` |
| 2-pin parts turn or mirror to face their node; ICs stay upright; field text stays horizontal | `test_sch.py::test_switch_mirrors_to_face_its_node`, `test_ic_stays_upright_when_attached`, `test_rotated_symbol_fields_stay_horizontal` |
| The same `board.py` gives the same file, byte for byte | `test_sch.py::test_the_same_board_gives_the_same_file` |
| Everything left is reported as a move; the examples stay under the bar | `test_rules.py::test_readability_bar` |

What still collides comes back as a list — in the `sch` step of `pcbc build` (`"readability"`), from `pcbc sch`, and on the review page — phrased as moves: `wire 3V3 runs through C_MCU_HF value`, `EN: R_EN.2 and C_EN.1 are 19 mm apart but joined by labels`. The AI iterates on that list, not on a picture.

## Install

```bash
pip install -e ".[dev]"
pcbc check examples/blinky/blinky.py
pcbc build examples/blinky/blinky.py --force
pcbc review examples/blinky/blinky.py   # schematic, copper, 3D in one HTML page
pcbc check examples/c3_usb/c3_usb.py
pcbc build examples/c3_usb/c3_usb.py --upto place --force
pcbc review examples/c3_usb/c3_usb.py
```

KiCad 10 `kicad-cli` is required for DRC and Gerbers. `pcb` (Zener) is not.

Blinky is a 40×25 mm 2-layer LED + resistor. `pcbc build` writes:

- `layout/blinky/layout.kicad_pcb` — seed
- `layout/blinky/schematic.kicad_sch`
- `layout/blinky/placed/` / `routed/`
- `layout/blinky/fab/` — Gerbers, `bom.csv`, `cpl.csv`, `FAB_NOTES.md`

This is a sibling of [pcb-space](https://github.com/RileyMcCarthy/pcb-space), which stays the Zener-era tool.
