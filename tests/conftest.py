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


_ROUTED: dict[str, Path] = {}


def routed_board(name: str, board: Path) -> Path:
    """The **routed** layout for `name`, built from its `board.py` and cached for the session.

    The twin of `placed_board`, and it exists for the same reason one step later. A placed board is
    a pure function of the source; a routed one needs KiCad and KRT and minutes, so the temptation is
    to read the one in `examples/**/layout/`. That directory is gitignored build output, a clean
    checkout does not have it, and **whatever is in it is whatever somebody last built** — which on
    this machine, 2026-09-21, was not what `pcbc build` produces: its buck carries 71 segments where
    a fresh build carries 73.

    Three dicts pinned against it were measurably wrong before anything in this session changed the
    router, and all three are re-recorded (`test_examples_fab.py`): `RETURNS["node"]` asserted seven
    `net_change` vias for a board whose USB pair **never leaves F.Cu** and has none;
    `RETURNS["c3_usb"]` asserted four where a build gives five; and
    `test_route_verify_stitch.py::test_the_planes_a_check_reads...` rested on blinky's routed board
    having **no `(zone ...)` block at all**, where a fresh one pours GND on B.Cu like every other
    two-layer board here. A test that pins numbers for a board pcbc no longer produces is worse than
    no test: it fails for the wrong reason, or passes for one.

    Only `kicad` + `krt` marked tests may call this; it costs a real build. It is cached per session
    and per name, so five boards cost five builds however many tests ask.
    """
    from pcbc.build import build_job

    if name in _ROUTED and _ROUTED[name].exists():
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
    if result.get("error"):
        raise RuntimeError(f"routing {name}: {result['error']}")
    out = work / "layout" / src.stem / "routed" / "layout.kicad_pcb"
    if not out.exists():
        raise RuntimeError(f"routing {name}: no routed board at {out}")
    _ROUTED[name] = out
    return out
