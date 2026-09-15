# pcbc

**Python in, fab-ready KiCad out.** One AI-writable board file compiles to a KiCad 10 project and a JLCPCB zip.

Zener is not used. There is no `SOURCE.json`, no `pcb.toml`, no pin lockfile. Pin **names** live on the `.kicad_sym`; pad **numbers** live on the `.kicad_mod`; `part.py` is MPN / LCSC / which CAD files.

```
pcbc source search|import     LCSC + CAD → part.py + .kicad_mod + .kicad_sym
      ↓
board.py                      Net / Part / Place / NetReq
      ↓
pcbc build board.py           check → seed pcb → sch → place → route → fab
```

```bash
pip install -e ".[dev]"
pcbc check examples/blinky/blinky.py
```

KiCad 10 `kicad-cli` and KiCadRoutingTools (`KRT_HOME`) are required for place / route / fab. `pcb` (Zener) is not.

This is a sibling of [pcb-space](https://github.com/RileyMcCarthy/pcb-space), which stays the Zener-era tool.
