# pcbc

**Python in, fab-ready KiCad out.** One AI-writable board file compiles to a KiCad 10 project and a JLCPCB zip.

Zener is not used. There is no `SOURCE.json`, no `pcb.toml`, no pin lockfile.

```
pcbc build board.py           check → seed pcb → sch → place → route → fab
```

## One fact, one file

| Fact | Lives in |
|---|---|
| Nets, MPN, LCSC, Place, board size | `board.py` (every part needs `Place()` — CSS, not auto-place) |
| Pin **names** → pad numbers + artwork | `.kicad_sym` |
| Land | `.kicad_mod` |
| IC MPN / LCSC / which CAD files | `components/…/part.py` (no `pins=`) |

Generics (`Resistor("R1", "1k", package="0402", …)`) use vendored KiCad chip lands. Footprint properties: `Value` = electrical, `Mpn` = reel, `LCSC` = JLC code.

## Install

```bash
pip install -e ".[dev]"
pcbc check examples/blinky/blinky.py
pcbc build examples/blinky/blinky.py --force
pcbc review examples/blinky/blinky.py   # schematic, copper, 3D in one HTML page
pcbc check examples/c3_usb/c3_usb.py
pcbc build examples/c3_usb/c3_usb.py --upto place --force
```

KiCad 10 `kicad-cli` is required for DRC and Gerbers. `pcb` (Zener) is not.

Blinky is a 40×25 mm 2-layer LED + resistor. `pcbc build` writes:

- `layout/blinky/layout.kicad_pcb` — seed
- `layout/blinky/schematic.kicad_sch`
- `layout/blinky/placed/` / `routed/`
- `layout/blinky/fab/` — Gerbers, `bom.csv`, `cpl.csv`, `FAB_NOTES.md`

This is a sibling of [pcb-space](https://github.com/RileyMcCarthy/pcb-space), which stays the Zener-era tool.
