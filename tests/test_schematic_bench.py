"""The saved schematic pictures are what the tool draws now.

A change to the sheet updates tests/benchmarks/schematics/<board>.png. Vibes
posts that picture on the pull request as current, new, and difference. A
failing run also writes those three under tests/benchmarks/schematics/out/
and an index.html that lays them out.
"""

import os

import pytest

from pcbc.sch_bench import BOARDS, check_board

pytestmark = pytest.mark.kicad


@pytest.mark.parametrize("name,board", BOARDS, ids=[name for name, _ in BOARDS])
def test_schematic_picture_matches_the_saved_one(name: str, board):
    err = check_board(name, board, update=os.environ.get("PCBC_UPDATE_SCHEMATICS") == "1")
    if err:
        from pcbc.sch_bench import OUT, _page

        _page([(n, n == name) for n, _ in BOARDS])
    assert err is None, err
