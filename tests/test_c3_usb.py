from pathlib import Path

import pytest

from pcbc.build import build_job
from pcbc.language import check_board, load_board
from pcbc.review import review_job

BOARD = Path(__file__).resolve().parent.parent / "examples" / "c3_usb" / "c3_usb.py"


def test_c3_usb_check():
    assert check_board(BOARD) == []
    d = load_board(BOARD)
    refs = {i.ref for i in d.instances}
    assert {"J1", "U1", "U2", "U3", "D1", "R_CC1", "R_CC2"} <= refs
    assert len(d.instances) == 19
    j1 = next(i for i in d.instances if i.ref == "J1")
    assert j1.pins["DP1"] == j1.pins["DP2"] == "USB_DP"
    u1 = next(i for i in d.instances if i.ref == "U1")
    assert u1.pins["3V3"] == "3V3"
    assert u1.pins["IO19"] == "USB_DP"


def test_c3_usb_upto_place(tmp_path: Path):
    board = tmp_path / "c3_usb.py"
    board.write_text(BOARD.read_text())
    # Components are loaded relative to the board file; copy the tree.
    import shutil

    src = BOARD.parent / "components"
    shutil.copytree(src, tmp_path / "components")
    result = build_job(board, upto="place", force=True)
    assert result.get("error") is None, result
    layout = tmp_path / "layout" / "c3_usb"
    pcb = (layout / "placed" / "layout.kicad_pcb").read_text()
    assert "(footprint" in pcb
    assert "J1" in pcb and "U1" in pcb
    sch = (layout / "schematic.kicad_sch").read_text()
    assert "USB_DP" in sch
    assert "power:GND" in sch


@pytest.mark.kicad
def test_c3_usb_review(tmp_path: Path):
    import shutil

    board = tmp_path / "c3_usb.py"
    board.write_text(BOARD.read_text())
    shutil.copytree(BOARD.parent / "components", tmp_path / "components")
    built = build_job(board, upto="place", force=True)
    assert built.get("error") is None, built
    result = review_job(board, open_html=False)
    html = Path(result["html"])
    assert html.exists()
    page = html.read_text()
    assert 'data-tab="sch"' in page
    assert 'data-tab="three"' in page
    assert "USB_DP" in page or "J1" in page
