import os
import shutil as _shutil
import tempfile
from pathlib import Path

import pytest


def pytest_collection_modifyitems(config, items):
    skip_kicad = pytest.mark.skip(reason="kicad-cli (set PCBC_REQUIRE_KICAD=1)")
    skip_krt = pytest.mark.skip(reason="KiCadRoutingTools (set PCBC_REQUIRE_KRT=1, KRT_HOME)")
    for item in items:
        if item.get_closest_marker("kicad") and not os.environ.get("PCBC_REQUIRE_KICAD"):
            item.add_marker(skip_kicad)
        if item.get_closest_marker("krt") and not os.environ.get("PCBC_REQUIRE_KRT"):
            item.add_marker(skip_krt)


# ---------------------------------------------------------------------------------------------
# Build artifacts a test may read, and where they come from.
#
# `examples/**/layout/` is in `.gitignore`: it is build output and nothing in it is tracked. Six
# test files read it anyway, so on a clean checkout 45 tests in `test_patterns.py` alone raised
# `FileNotFoundError` — measured with `git archive HEAD | tar -x` into an empty directory — and CI
# runs `pytest` **before** its first `pcbc build` in both jobs. The suite was green only on a
# machine that happened to have built the boards before, which makes every count it ever printed a
# statement about that machine rather than about the code.
#
# A **placed** board is a pure function of `board.py`: `pcb_job` is check + seed + place, no
# subprocess, no KiCad, 0.02 s on blinky. So it is generated here rather than read, once per
# session, into pytest's own tmp directory. That removes the dependency on build state entirely —
# and removes a second bug with it, because the checked-in artifacts were *stale*: node's on-disk
# placed board differs from a fresh one at the placement level (`(at 17.1 17.3 -90)` against
# `(at 14.1 19.05 -90)`), so tests reading it were pinning numbers for a board pcbc no longer makes.
#
# A **routed** board is not a pure function of anything: it needs KiCad and KRT and minutes. Tests
# that read one are marked `kicad` + `krt` and skip when the artifact is absent, which is what the
# markers already promise.
# ---------------------------------------------------------------------------------------------

_PLACED: dict[str, Path] = {}
_ROOT: Path | None = None


def placed_board(name: str, board: Path) -> Path:
    """The placed layout for `name`, generated from its `board.py` and cached for the session."""
    from pcbc.build import pcb_job

    global _ROOT

    if name in _PLACED and _PLACED[name].exists():
        return _PLACED[name]
    if _ROOT is None:
        _ROOT = Path(tempfile.mkdtemp(prefix="pcbc-placed-"))
    work = _ROOT / name
    work.mkdir(parents=True, exist_ok=True)
    src = work / board.name
    src.write_text(board.read_text())
    for extra in ("components",):
        if (board.parent / extra).exists() and not (work / extra).exists():
            _shutil.copytree(board.parent / extra, work / extra)
    result = pcb_job(src)
    if result.get("error"):
        raise RuntimeError(f"placing {name}: {result['error']}")
    _PLACED[name] = Path(result["placed"])
    return _PLACED[name]
