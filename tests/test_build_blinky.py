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
    hold it: `patterns_final` differs from the step before it in not one byte. If a no-op step after
    the last router step ever moved a board, this is where it would say so.

    **`patterns_final` is no longer the last step file, and the two halves of that sentence are
    tested separately now.** `docs/quality-plan.md` slice 1 puts `relax` after it, and the position
    is that step's whole safety argument — `route.krt_plan` schedules nothing after it, so no router
    and no pattern can react to copper it rewrote (`route_relax`'s module docstring). So this asserts
    both: the byte copy, still, of the stage that adds nothing, and that the thing scheduled after it
    is `relax` and nothing else. On blinky `relax` does move the board — 6 segments to 4 — which is
    why it cannot be folded into the byte-copy assertion.

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
    assert steps[-1].name.endswith("_relax.kicad_pcb"), [p.name for p in steps]
    assert steps[-2].name.endswith("_patterns_final.kicad_pcb"), [p.name for p in steps]
    assert steps[-2].read_bytes() == steps[-3].read_bytes(), (steps[-3].name, steps[-2].name)
    route = result["steps"][-1]
    assert [p["step"] for p in route["plan"]][-2:] == ["patterns_final", "relax"], route["plan"]
    final = [s for s in route["steps"] if s["step"] == "patterns_final"]
    assert len(final) == 1 and final[0]["summary"]["pieces"] == 0 and final[0]["summary"]["refused"] == {}, final
    # And the relaxer, on the cheapest board there is: two chains, one pulled taut, 6 segments -> 4,
    # 0.3252 mm shorter, and both halves of its own self-check at zero. Measured 2026-09-21.
    relax = [s for s in route["steps"] if s["step"] == "relax"]
    assert len(relax) == 1, route["steps"]
    got = {k: relax[0]["summary"][k] for k in ("chains", "moved", "segments_in", "segments_out", "orphans_in", "orphans_out", "skipped_segments")}
    assert got == {"chains": 2, "moved": 1, "segments_in": 6, "segments_out": 4, "orphans_in": 0, "orphans_out": 0, "skipped_segments": 0}, relax[0]["summary"]
