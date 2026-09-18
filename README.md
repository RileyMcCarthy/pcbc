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
```

The tool picks the distance (room for the net's label on the wire); `gap=` only to override it. `pin=` is only for hanging a pin that is *not* on the target's net; `rotate=` only to override the orientation the tool picks.

Library `.kicad_sym` artwork is used as-is. Collision uses the real symbol (graphics, pin text, Reference/Value).

The AI never draws a wire. pcbc wires every net from `board.py`: pins of one symbol in a line share a rail (a bus bar just outside the pins when others sit between), nearby symbols get the least-cluttered clean Manhattan path, and every connected group is then named — a label on signal nets, a power symbol on `Power()`/`Ground()` nets. Labels, power symbols and each passive's Reference/Value then take the spot that overlaps least of what is already drawn. `pcbc build` asks `kicad-cli sch export netlist` for what KiCad actually reads and fails the `sch` stage if it is not exactly the board's netlist (`tests/test_netcheck.py` does the same). Placement changes how it looks, never what it connects.

What still collides comes back as a list — in the `sch` step of `pcbc build` (`"readability"`) and on the review page — phrased as moves: `wire 3V3 runs through C_MCU_HF value`, `EN: R_EN.2 and C_EN.1 are 19 mm apart but joined by labels`. The AI iterates on that list, not on a picture.

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
