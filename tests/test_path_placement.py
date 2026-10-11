"""Placement checks of paths that were wrong in refutation round 1 (`docs/net-path-physics.md` §4.5).

Defect 6: a link whose pad no track could leave was found only by the router ("no free cell next to one
of the pads", naming nothing). Defect 7 (a path's own keep-away judged on the chord) left with the path
keep-away itself: since `docs/physics-subjects.md` PR 7 a keep-away is net to net, and `keep_clear_of=` on a
Path is refused (E8).
"""

from __future__ import annotations

import shutil
from pathlib import Path

from conftest import edit_board
from pcbc.build import pcb_job
from pcbc.language import check_board

ROOT = Path(__file__).resolve().parent.parent
FB_TAP = 'Path("5V", "C_OUT1.1", "R_FB_TOP.1", kind="feedback")'


def _buck(tmp_path: Path, edits) -> Path:
    work = tmp_path / "buck"
    shutil.copytree(ROOT / "examples" / "buck", work, ignore=shutil.ignore_patterns("layout"))
    board = work / "buck.py"
    edit_board(board, edits)
    return board


def test_a_keep_away_on_a_path_is_refused_e8(tmp_path: Path):
    """E8 (`docs/physics-subjects.md` §7, PR 7): every branch of 5V is the one node 5V, so a keep-away
    written on one of its Paths is the node's: it is a NetReq field."""
    for kw, val in (("keep_clear_of", '"SW"'), ("keep_clear_mm", "1")):
        board = _buck(tmp_path / kw, [(FB_TAP, FB_TAP[:-1] + f", {kw}={val})")])
        assert check_board(board) == [
            f'Path("5V") line 50: {kw}= keeps a node\'s copper from another node\'s, and every branch of 5V is the one node 5V; it is a NetReq field'
        ]


def test_a_station_no_track_can_leave_is_named_before_copper(tmp_path: Path):
    """Defect 6's board, on the example: `keep_clear_of="SW"` on the FB NetReq (kind analog since PR 7: feedback is a
    Path kind) holds 3 mm around U1's own
    FB pin, 1.30 mm from its SW pin, so no 0.781 mm track leaves U1.SW. Said at placement, with the rule
    and its line, and that no Place() edit can open it."""
    board = _buck(tmp_path, [('NetReq("FB", kind="analog")', 'NetReq("FB", kind="analog", keep_clear_of="SW")')])
    moves = [m for m in pcb_job(board)["layout_report"] if "can leave" in m]
    assert len(moves) == 1, moves
    m = moves[0]
    assert m.startswith("SW path U1.SW -> L1.1 (Path line 46): no 0.781 mm track can leave U1.2 (U1.SW) on F.Cu: U1.FB (FB) is 1.30 mm from it and needs 3 mm (keep_clear_of SW on NetReq line 44)"), m
    # Refutation round 2: the pad in the way is U1's own FB pin, and the warning used to name U1 as the
    # part to move. It names the real blocker and the rule, and proposes nothing.
    assert m.endswith("; U1 carries both SW and FB; layout.core.py:15 attaches R_FB_TOP to U1.FB"), m
    assert "move" not in m and "lower" not in m, m


def test_the_example_boards_stations_all_have_an_escape(tmp_path: Path):
    board = _buck(tmp_path, [])
    assert not [m for m in pcb_job(board)["layout_report"] if "can leave" in m]


def _route(board: Path):
    from pcbc.language import load_board
    from pcbc.layout_job import route_in_passes
    from pcbc.place_native import place
    from pcbc.route_native import move_lines

    design = load_board(board)
    pl = place(design, name=board.stem, base=board.parent / "layout" / board.stem / "routed")
    got, *_ = route_in_passes(design, pl.job, pl, name=board.stem, core_cu=[])
    return got, move_lines(design, pl.job, got)


def test_the_route_names_what_holds_every_cell_around_a_pad(tmp_path: Path):
    """Defect 6, the router's half, on the refuters' 3.3 V buck (`tests/fixtures/buck33`): the SW link's
    search found no free cell next to U1.2 and the move named nothing ("blockers": []). It now names
    the items holding the cells, the rule each holds them by and its line, and that U1 carries both."""
    work = tmp_path / "buck33"
    shutil.copytree(ROOT / "tests" / "fixtures" / "buck33", work, ignore=shutil.ignore_patterns("layout"))
    got, moves = _route(work / "buck33.py")
    f = next(x for x in got.failed if x.net == "SW" and {x.a, x.b} == {"U1.2", "L1.1"})
    assert f.blockers, "a stuck pad names what holds it"
    text = next(m for m in moves if m.startswith("SW: U1.2"))
    assert "No free cell next to U1.2" in text and "U1.4 [FB] at 3 mm (keep_clear_of SW on NetReq line 36" in text, text
    assert "U1 carries both nets, so the cells next to its own pad are held by its own pad at 3 mm (keep_clear_mm on NetReq line 36)" in text, text
    assert "lower" not in text and "Move:" not in text, text
    assert "In the way: U1.4 pad [FB]" in text, text
    # And pcbc pcb said it before any copper.
    pre = [m for m in pcb_job(work / "buck33.py")["layout_report"] if "can leave U1.2" in m]
    assert pre and "keep_clear_of SW on NetReq line 36" in pre[0], pre


def test_a_blocker_held_by_a_keep_away_names_its_line(tmp_path: Path):
    """When the search did reach a blocker, a keep-away (not the clearance) holding it is said too."""
    board = _buck(tmp_path, [('NetReq("FB", kind="analog")', 'NetReq("FB", kind="analog", keep_clear_of="SW")')])
    _got, moves = _route(board)
    text = next(m for m in moves if m.startswith("SW: U1.2"))
    assert "Held by U1.4 [FB] at 3 mm (keep_clear_of SW on NetReq line 44" in text, text
    assert "In the way: U1.4 pad [FB]" in text, text
