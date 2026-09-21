from pathlib import Path

import pytest

from pcbc.build import build_job

BLINKY = Path(__file__).resolve().parent.parent / "examples" / "blinky" / "blinky.py"


@pytest.mark.kicad
@pytest.mark.krt
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
    assert result["steps"][-1]["router"] == "krt" and result["steps"][-1]["copper"] == "verified"
    sch = (layout / "schematic.kicad_sch").read_text()
    assert "(kicad_sch" in sch
    assert '(property "LCSC" "C72043"' in (layout / "placed" / "layout.kicad_pcb").read_text()


@pytest.mark.kicad
@pytest.mark.krt
def test_blinky_fab(tmp_path: Path):
    board = tmp_path / "blinky.py"
    board.write_text(BLINKY.read_text())
    result = build_job(board, upto="fab", force=True)
    assert result.get("error") is None, result
    # The soft-rule counts, as the other examples pin them (E's promotion procedure needs every
    # example at zero before a soft rule becomes an error). blinky writes width_power.
    from pcbc.netcheck import check_copper
    from pcbc.language import load_board

    gate = check_copper(load_board(board), tmp_path / "layout" / "blinky" / "routed" / "layout.kicad_pcb", refill=False)
    assert gate["soft"] == {"width_power": 0}, gate["soft"]
    fab = tmp_path / "layout" / "blinky" / "fab"
    assert (fab / "bom.csv").exists()
    bom = (fab / "bom.csv").read_text()
    assert "C21190" in bom
    assert "C72043" in bom
    assert (fab / "cpl.csv").exists()
    gerbers = list((fab / "gerbers").glob("*"))
    assert gerbers, "kicad-cli wrote no gerbers"
    assert (fab / "FAB_NOTES.md").exists()


@pytest.mark.kicad
@pytest.mark.krt
def test_the_step_after_the_last_router_step_writes_a_byte_copy_of_the_board(tmp_path: Path):
    """`docs/stitch-plan.md` S2's acceptance, on the cheapest real build in the repo.

    With `patterns.FINAL == ()` the `final` stage runs — it builds the scene over the finished copper
    and runs `pattern_copper`'s self-check — and writes nothing, so the board it hands on is the board
    it was handed, byte for byte. That is the architectural hypothesis in the only form a test can
    hold it: `patterns_final` is the **last** step file of the route, and it differs from the step
    before it in not one byte. If a no-op step after the last router step ever moved a board, this is
    where it would say so.

    The board-level half of the acceptance — a fresh build of all five boards byte-identical to the
    same build without the stage — is a before/after measurement a single run cannot make; it is
    recorded in `patterns.FINAL`'s docstring with the five step files it added.
    """
    board = tmp_path / "blinky.py"
    board.write_text(BLINKY.read_text())
    result = build_job(board, upto="route", force=True)
    assert result.get("error") is None, result
    work = tmp_path / "layout" / "blinky" / "routed"
    steps = sorted(work.glob("[0-9][0-9]_*.kicad_pcb"))
    assert steps[-1].name.endswith("_patterns_final.kicad_pcb"), [p.name for p in steps]
    assert steps[-1].read_bytes() == steps[-2].read_bytes(), (steps[-2].name, steps[-1].name)
    route = result["steps"][-1]
    assert [p["step"] for p in route["plan"]][-1] == "patterns_final", route["plan"]
    final = [s for s in route["steps"] if s["step"] == "patterns_final"]
    assert len(final) == 1 and final[0]["summary"]["pieces"] == 0 and final[0]["summary"]["refused"] == {}, final
