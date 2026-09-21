from pathlib import Path

from pcbc.language import load_board
from pcbc.seed import emit_pcb, pad_nets, seed_job

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"
BLINKY = EXAMPLES / "blinky" / "blinky.py"


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


def test_the_seeded_board_declares_its_own_via_tenting():
    """Finding 3: every routed board carried `(tenting (front yes) (back yes))` and none of it was
    pcbc's — the stanza is KiCad 10's default, written the first time KiCad saves the file, and
    `grep -rn tent src/pcbc/` returned nothing.

    It matters because a tap's ring sits at exactly `stackup.clearance_min` from the pad it welds
    (0.0889 mm on node, 0.1270 mm on the two-layer rungs) and a pad's mask opening here **is** its
    copper (`pad_to_mask_clearance 0`, `solder_mask_margin 0`). With the vias open, the mask dam
    between a 0402's opening and the tap beside it is 0.0889 mm — under `mask_bridge_min`, the sliver
    a fab drops — and the pad and the barrel share one opening. That is the wicking failure B.3 is
    about, reached without a via ever being inside a pad.
    """
    from pcbc.language import load_board
    from pcbc.seed import emit_pcb
    from pcbc.stackup import get_stackup

    for name in ("blinky", "node"):
        design = load_board(EXAMPLES / name / f"{name}.py")
        assert get_stackup(design.board.stackup).via_tenting, name
        assert "\t\t(tenting\n\t\t\t(front yes)\n\t\t\t(back yes)\n\t\t)\n" in emit_pcb(design, name=name), name
