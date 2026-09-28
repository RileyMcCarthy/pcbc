import os
import shutil as _shutil
import tempfile
from pathlib import Path

import pytest


def pytest_collection_modifyitems(config, items):
    skip_kicad = pytest.mark.skip(reason="kicad-cli (set PCBC_REQUIRE_KICAD=1)")
    for item in items:
        if item.get_closest_marker("kicad") and not os.environ.get("PCBC_REQUIRE_KICAD"):
            item.add_marker(skip_kicad)


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
# A **placed** board is a pure function of `board.py`: `pcb_job` is check + place (in memory) + the
# place-only emit, no subprocess, no KiCad, 0.02 s on blinky. So it is generated here rather than read, once per
# session, into pytest's own tmp directory. That removes the dependency on build state entirely —
# and removes a second bug with it, because the checked-in artifacts were *stale*: node's on-disk
# placed board differs from a fresh one at the placement level (`(at 17.1 17.3 -90)` against
# `(at 14.1 19.05 -90)`), so tests reading it were pinning numbers for a board pcbc no longer makes.
#
# A **routed** board needs KiCad (the emitted board is filled and judged by it) and up to a minute of
# routing. Tests that read one are marked `kicad`.
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


_ROUTED: dict[str, tuple[Path, dict]] = {}


def routed_result(name: str, board: Path) -> tuple[Path, dict]:
    """(the emitted board, the build result) for `name`, built natively from its `board.py` to the route
    stage and cached for the session. The board is returned **also when the only error is unrouted
    nets** (N0: the native router may leave a net open; the build still emits, fills and runs every
    gate on what it wrote). Any other error raises. Five boards cost five builds however many tests ask.

    Why generated and not read from `examples/**/layout/`: that directory is gitignored build output;
    whatever is in it is whatever somebody last built."""
    from pcbc.build import build_job

    if name in _ROUTED and _ROUTED[name][0].exists():
        return _ROUTED[name]
    global _ROOT
    if _ROOT is None:
        _ROOT = Path(tempfile.mkdtemp(prefix="pcbc-placed-"))
    work = _ROOT / "routed" / name
    work.mkdir(parents=True, exist_ok=True)
    src = work / board.name
    src.write_text(board.read_text())
    for extra in ("components",):
        if (board.parent / extra).exists() and not (work / extra).exists():
            _shutil.copytree(board.parent / extra, work / extra)
    result = build_job(src, upto="route", force=True)
    if result.get("error") and not str(result["error"]).startswith("unrouted:"):
        raise RuntimeError(f"routing {name}: {result['error']}")
    out = work / "layout" / src.stem / "routed" / "layout.kicad_pcb"
    if not out.exists():
        raise RuntimeError(f"routing {name}: no routed board at {out}")
    _ROUTED[name] = (out, result)
    return _ROUTED[name]


def routed_board(name: str, board: Path) -> Path:
    """The emitted board of `routed_result` (complete or not)."""
    return routed_result(name, board)[0]


def complete_board(name: str, board: Path) -> Path:
    """The emitted board, only when the native router finished every net; raises otherwise, so a test
    that needs a complete board says so (N0: c3_usb, node and the DS2 Addon may not complete yet)."""
    out, result = routed_result(name, board)
    if result.get("unrouted"):
        raise RuntimeError(f"N0: {name} has unrouted nets ({', '.join(result['unrouted'])})")
    return out
