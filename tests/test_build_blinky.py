from pathlib import Path

import pytest

from pcbc.build import build_job

BLINKY = Path(__file__).resolve().parent.parent / "examples" / "blinky" / "blinky.py"


def test_blinky_upto_route(tmp_path: Path, monkeypatch):
    # Copy board into tmp so layout/ does not dirty the example until --force CI.
    board = tmp_path / "blinky.py"
    board.write_text(BLINKY.read_text())
    result = build_job(board, upto="route", force=True)
    assert result.get("error") is None, result
    layout = tmp_path / "layout" / "blinky"
    pcb = (layout / "routed" / "layout.kicad_pcb").read_text()
    assert '(net "LED")' in pcb
    assert "(segment" in pcb
    sch = (layout / "schematic.kicad_sch").read_text()
    assert "(kicad_sch" in sch
    assert '(property "LCSC" "C72043"' in (layout / "placed" / "layout.kicad_pcb").read_text()


@pytest.mark.kicad
def test_blinky_fab(tmp_path: Path):
    board = tmp_path / "blinky.py"
    board.write_text(BLINKY.read_text())
    result = build_job(board, upto="fab", force=True)
    assert result.get("error") is None, result
    fab = tmp_path / "layout" / "blinky" / "fab"
    assert (fab / "bom.csv").exists()
    bom = (fab / "bom.csv").read_text()
    assert "C21190" in bom
    assert "C72043" in bom
    assert (fab / "cpl.csv").exists()
    gerbers = list((fab / "gerbers").glob("*"))
    assert gerbers, "kicad-cli wrote no gerbers"
    assert (fab / "FAB_NOTES.md").exists()
