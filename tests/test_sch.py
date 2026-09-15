from pathlib import Path

from pcbc.language import load_board
from pcbc.sch_emit import emit_from_design

BLINKY = Path(__file__).resolve().parent.parent / "examples" / "blinky" / "blinky.py"


def test_blinky_schematic_has_led_net():
    design = load_board(BLINKY)
    sch = emit_from_design(design, title="blinky")
    assert sch.startswith("(kicad_sch")
    assert '(label "LED"' in sch or "(label LED" in sch
    assert "(symbol" in sch
    assert "default.net" not in sch
