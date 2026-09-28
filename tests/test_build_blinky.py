from pathlib import Path

import pytest

from pcbc.build import build_job

BLINKY = Path(__file__).resolve().parent.parent / "examples" / "blinky" / "blinky.py"


@pytest.mark.kicad
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
    assert result["steps"][-1]["router"] == "native" and result["steps"][-1]["copper"] == "verified"
    sch = (layout / "schematic.kicad_sch").read_text()
    assert "(kicad_sch" in sch
    assert '(property "LCSC" "C72043"' in pcb
    assert not (layout / "placed").exists() and not (layout / "seed").exists(), "no board file before the emitted one"


@pytest.mark.kicad
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
def test_the_relaxer_runs_last_and_its_self_checks_hold_on_blinky(tmp_path: Path):
    """`route_relax.relax_pieces` is the route stage's last step (docs/native-plan.md §3.4), after the
    `final` patterns, and on blinky — whose one link is the LED hop — it pulls the hop taut: the numbers
    the old step recorded, measured again natively 2026-09-25 (6 segments in, 4 out, 0.3252 mm
    shorter, no copper orphaned). blinky's `final` stage writes nothing (no declaration asks it to)."""
    board = tmp_path / "blinky.py"
    board.write_text(BLINKY.read_text())
    result = build_job(board, upto="route", force=True)
    assert result.get("error") is None, result
    route = result["steps"][-1]
    relax = route["route_stats"]["relax"]
    got = {k: relax[k] for k in ("chains", "moved", "segments_in", "segments_out", "orphans_in", "orphans_out", "mm_saved")}
    assert got == {"chains": 2, "moved": 1, "segments_in": 6, "segments_out": 4, "orphans_in": 0.0, "orphans_out": 0.0, "mm_saved": 0.3252}, got
    assert route["refused"] == {} and not [n for n in route["notes"] if n.startswith("style: stitch")], route["notes"]
