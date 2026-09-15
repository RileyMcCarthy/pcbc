from pathlib import Path

from pcbc.language import load_board
from pcbc.seed import emit_pcb, pad_nets, seed_job

BLINKY = Path(__file__).resolve().parent.parent / "examples" / "blinky" / "blinky.py"


def test_blinky_seed_properties(tmp_path: Path):
    design = load_board(BLINKY)
    r1 = next(i for i in design.instances if i.ref == "R1")
    assert pad_nets(r1)["1"] == "VCC"
    assert pad_nets(r1)["2"] == "LED"
    pcb = emit_pcb(design, name="blinky")
    assert '(property "LCSC" "C21190"' in pcb
    assert '(property "Mpn" "0402WGF1001TCE"' in pcb
    assert '(property "Value" "1k"' in pcb
    assert '(property "Reference" "R1"' in pcb
    assert '(net "LED")' in pcb
    assert '(generator "pcbc")' in pcb
    out = tmp_path / "layout.kicad_pcb"
    seed_job(design, out, name="blinky")
    assert out.exists()
    assert out.with_suffix(".kicad_pro").exists()


def test_seed_uuid_stable():
    design = load_board(BLINKY)
    a = emit_pcb(design, name="blinky")
    b = emit_pcb(design, name="blinky")
    assert a == b
