import pytest
from pathlib import Path

from pcbc.language import load_board
from pcbc.seed import emit_pcb, pad_nets

from conftest import write_split

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
    # The native placer builds the same footprints in memory and writes no file: its base carries the
    # footprints and no layout primitive (the outline is a `Rect` object, never board text).
    from pcbc.place_native import place

    pl = place(design, name="blinky")
    assert '(property "Reference" "R1"' in pl.text and "(gr_rect" not in pl.text and "(zone" not in pl.text
    assert not list(tmp_path.iterdir()), "placement wrote nothing"


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


def _example_on(tmp_path: Path, name: str, old: str, stackup: str) -> Path:
    import shutil

    work = tmp_path / stackup
    shutil.copytree(EXAMPLES / name, work, ignore=shutil.ignore_patterns("layout", "fab"))
    board = work / f"{name}.py"
    text = board.read_text()
    assert f'stackup="{old}"' in text
    write_split(board, text.replace(f'stackup="{old}"', f'stackup="{stackup}"'))
    return board


def test_the_seeded_board_carries_pcbcs_stackup(tmp_path: Path):
    """Round 3 (refute-twooz): no board carried a `(stackup)`, so KiCad's default (35 um copper, 1.51 mm dielectric)
    went into the Gerber job file of a `jlcpcb_2l_2oz` board. Every copper and dielectric thickness is now pcbc's,
    and FAB_NOTES states the copper to order."""
    import re

    from pcbc.compile import compile_design
    from pcbc.fab import _copper_order
    from pcbc.stackup import Copper, get_stackup

    for name, old, stackup in (("blinky", "jlcpcb_2l_1oz", "jlcpcb_2l_1oz"), ("blinky", "jlcpcb_2l_1oz", "jlcpcb_2l_2oz"), ("node", "jlcpcb_4l_1oz", "jlcpcb_4l_2oz")):
        design = load_board(_example_on(tmp_path, name, old, stackup))
        stack = get_stackup(stackup)
        pcb = emit_pcb(design, name=name)
        block = pcb[pcb.index("\t\t(stackup\n"):pcb.index("\t\t(pad_to_mask_clearance")]
        got = re.findall(r'\(layer "([^"]+)"\n\t+\(type "([^"]+)"\)\n\t+\(thickness ([0-9.]+)\)', block)
        want = [(x.layer, "copper", f"{x.thickness_mm:g}") if isinstance(x, Copper) else (None, x.kind, f"{x.thickness_mm:g}") for x in stack.stack]
        assert [(n if t == "copper" else None, t, th) for n, t, th in got if "Mask" not in t] == want, (stackup, got)
        assert f"\t\t(thickness {stack.board_mm:g})\n" in pcb
        order = _copper_order(compile_design(design))
        outer = 0.07 if "2oz" in stackup else 0.035
        assert order[2].startswith(f"- Order outer copper {stack.copper_oz:g} oz ({outer:g} mm finished on F.Cu and B.Cu)"), order
        assert ("inner In1.Cu, In2.Cu 0.0152 mm" in order[2]) == (stack.layers == 4), order


@pytest.mark.kicad
def test_a_two_ounce_fab_package_states_two_ounces(tmp_path: Path):
    """Round 3 (refute-twooz, blinky2oz): the Gerber job file of a `jlcpcb_2l_2oz` build listed F.Cu and B.Cu at 0.035
    mm over a 1.51 mm dielectric (KiCad's default), and FAB_NOTES named only the stackup. KiCad now reads pcbc's
    stackup off the board, and the notes carry the order line."""
    import json

    from pcbc.build import build_job

    board = _example_on(tmp_path, "blinky", "jlcpcb_2l_1oz", "jlcpcb_2l_2oz")
    result = build_job(board, upto="fab", force=True)
    assert result.get("error") is None, result.get("error")
    fab = board.parent / "layout" / "blinky" / "fab"
    job = json.loads((fab / "gerbers" / "layout-job.gbrjob").read_text())
    rows = [(r.get("Name"), r.get("Thickness")) for r in job["MaterialStackup"] if r["Type"] in ("Copper", "Dielectric")]
    assert rows == [("F.Cu", 0.07), ("F.Cu/B.Cu", 1.46), ("B.Cu", 0.07)], rows
    notes = (fab / "FAB_NOTES.md").read_text()
    assert "- Order outer copper 2 oz (0.07 mm finished on F.Cu and B.Cu), board 1.6 mm." in notes, notes
