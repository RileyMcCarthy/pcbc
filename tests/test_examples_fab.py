"""Every example board goes from board.py to a JLC package: placed, routed, judged by KiCad, fabbed."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from pcbc.build import build_job

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"


@pytest.mark.kicad
@pytest.mark.krt
@pytest.mark.parametrize("name", ["buck", "c3_usb", "node"])
def test_example_builds_to_fab(tmp_path: Path, name: str):
    src = EXAMPLES / name
    board = tmp_path / f"{name}.py"
    board.write_text((src / f"{name}.py").read_text())
    shutil.copytree(src / "components", tmp_path / "components")
    result = build_job(board, upto="fab", force=True)
    assert result.get("error") is None, result.get("error")
    route = next(s for s in result["steps"] if s.get("stage") == "route")
    assert route["router"] == "krt" and route["unrouted"] == [] and route["copper"] == "verified", route
    place = next(s for s in result["steps"] if s.get("stage") == "place")
    assert place["layout"] == [], place["layout"]
    fab = tmp_path / "layout" / name / "fab"
    assert (fab / "bom.csv").exists() and (fab / "cpl.csv").exists()
    assert any((fab / "gerbers").glob("*")), "no gerbers"
    routed = (tmp_path / "layout" / name / "routed" / "layout.kicad_pcb").read_text()
    assert "filled_polygon" in routed, "the gate judged unfilled pours"
