"""Probe 5 (third review): the primitives the fourth round found the decompiler could not read, saved
by KiCad itself (`kicad-cli pcb upgrade --force` on the hand-written text below, so every field is
spelled KiCad 10's way):

- a `table` whose border (`external`, `header`) and separators (`rows`, `cols`) are all off: KiCad
  writes no `(stroke)` under either (P4);
- a `gr_text`, a `gr_text_box` and a zone `name` carrying a newline, a carriage return, a tab, a
  double quote and a backslash: KiCad writes `\\n`, `\\r`, `\\"` and `\\\\`, and the tab as itself (P2);
- a zone carrying `(attr (teardrop (type track_end)))`, the zone KiCad's teardrop generator writes
  around a via or a pad: KiCad-derived like `filled_polygon`, dropped by the decompiler and regenerated
  by KiCad's refill (P1).

Rerun from the repo root: `python tests/fixtures/kicad10/make_kicad10_primitives5.py`.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent.parent.parent / "src"))

from pcbc.layout_emit import splice, strip_layout  # noqa: E402

BASE = strip_layout((HERE / "kicad10_primitives3.kicad_pcb").read_text())


def U(n: int) -> str:
    return f"55555555-0000-4000-8000-{n:012d}"


ITEMS = f'''\t(gr_text "a\\nb\\rc\\ttab \\"q\\" back\\\\slash"
\t\t(at 10 10 0)
\t\t(layer "F.SilkS")
\t\t(uuid "{U(1)}")
\t\t(effects (font (size 1 1) (thickness 0.15)))
\t)
\t(gr_text_box "line1\\nline2"
\t\t(start 20 20)
\t\t(end 40 30)
\t\t(layer "F.SilkS")
\t\t(uuid "{U(2)}")
\t\t(effects (font (size 1 1) (thickness 0.15)))
\t\t(border yes)
\t\t(stroke (width 0.15) (type solid))
\t)
\t(zone
\t\t(net "GND")
\t\t(layer "B.Cu")
\t\t(uuid "{U(3)}")
\t\t(name "zone\\nname \\"q\\"")
\t\t(hatch edge 0.5)
\t\t(connect_pads (clearance 0.5))
\t\t(min_thickness 0.25)
\t\t(fill (thermal_gap 0.5) (thermal_bridge_width 0.5) (island_removal_mode 0))
\t\t(polygon (pts (xy 0 0) (xy 20 0) (xy 20 20) (xy 0 20)))
\t)
\t(zone
\t\t(net "GND")
\t\t(layer "F.Cu")
\t\t(hatch edge 0.5)
\t\t(attr (teardrop (type track_end)))
\t\t(connect_pads (clearance 0))
\t\t(min_thickness 0.25)
\t\t(filled_areas_thickness no)
\t\t(fill yes (thermal_gap 0.5) (thermal_bridge_width 0.5) (island_removal_mode 1) (island_area_min 0))
\t\t(polygon (pts (xy 50 50) (xy 51 50) (xy 50.5 51)))
\t)
\t(table
\t\t(column_count 2)
\t\t(uuid "{U(4)}")
\t\t(layer "F.SilkS")
\t\t(border (external no) (header no) (stroke (width 0.15) (type solid)))
\t\t(separators (rows no) (cols no) (stroke (width 0.1) (type solid)))
\t\t(column_widths 10 10)
\t\t(row_heights 2.54)
\t\t(cells
\t\t\t(table_cell "a" (start 50 60) (end 60 62.54) (margins 0.749999 0.749999 0.749999 0.749999) (span 1 1) (layer "F.SilkS") (uuid "{U(5)}") (effects (font (size 1 1) (thickness 0.15))))
\t\t\t(table_cell "b" (start 60 60) (end 70 62.54) (margins 0.749999 0.749999 0.749999 0.749999) (span 1 1) (layer "F.SilkS") (uuid "{U(6)}") (effects (font (size 1 1) (thickness 0.15))))
\t\t)
\t)
'''


def main() -> None:
    out = HERE / "kicad10_primitives5.kicad_pcb"
    out.write_text(splice(BASE, ITEMS))
    subprocess.run(["kicad-cli", "pcb", "upgrade", "--force", str(out)], check=True, capture_output=True, text=True)
    text = out.read_text()
    assert "(attr" in text and "(teardrop" in text, "KiCad dropped the teardrop zone"
    assert '"a\\nb\\rc\\t' in text or '"a\\nb\\rc\t' in text, "KiCad did not keep the escapes"
    print(f"wrote {out} ({len(text.splitlines())} lines)")


if __name__ == "__main__":
    main()
