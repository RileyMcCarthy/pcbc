"""The saved schematic pictures are what the tool draws now, two per board (docs/schematic-layout.md §7).

A change to a sheet updates tests/benchmarks/schematics/<board>.png (with the board's schematic.core.py) or
<board>.auto.png (the bare auto-layout, no core). Vibes posts each picture on the pull request as current, new, and
difference. Each picture's readability vector is saved beside it (<board>[.auto].score.json): a hard number above 0
fails, and so does a soft number worse than the saved one. A failing run also writes the pictures under
tests/benchmarks/schematics/out/ and an index.html that lays them out.
"""

import os

import pytest

from pcbc.sch_bench import BOARDS, MODES, check_board

pytestmark = pytest.mark.kicad

CASES = [(name, board, suffix, core) for name, board in BOARDS for suffix, core in MODES]


@pytest.mark.parametrize("name,board,suffix,core", CASES, ids=[name + suffix for name, _b, suffix, _c in CASES])
def test_schematic_picture_matches_the_saved_one(name: str, board, suffix: str, core: bool):
    err = check_board(name, board, update=os.environ.get("PCBC_UPDATE_SCHEMATICS") == "1", schematic_core=core)
    if err:
        from pcbc.sch_bench import OUT, _page

        _page([(n + s, n + s == name + suffix) for n, _b, s, _c in CASES])
    assert err is None, err
