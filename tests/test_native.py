"""The native generator's proofs (docs/native-plan.md §5): no KiCad board file is an intermediate in
generation, and nothing invokes the old router.

- **P1, no board before emit.** A build is traced (`trace.Trace.events`: every open of a `.kicad_pcb`,
  every subprocess, every `gen.decompile`, every `mark`), and before the route stage's `emit` mark
  nothing opens a `.kicad_pcb`, no subprocess is handed one, and nothing is decompiled — except the
  M1 core probe (`core_probe`), a validator whose only output is pass or fail naming a core line.
  The exemption is by the **calling module**, not by a temp directory's name.
- **P2, the old router is gone.** Every subprocess a build runs is `kicad-cli`; no identifier or
  string literal in `src/pcbc` (outside docstrings and comments) names it; `pcbc.route` does not
  import; the `krt` pytest marker is gone (`--strict-markers` makes a leftover mark an error).
"""

from __future__ import annotations

import ast
import importlib.util
import re
import shutil
from pathlib import Path

import pytest

from pcbc import trace
from pcbc.build import build_job

ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = ROOT / "examples"
DS2 = Path.home() / "Documents" / "MaD" / "Hardware" / "DS2Addon" / "pcbc"
BOARDS = ["blinky", "buck", "c3_usb", "node"] + (["ds2"] if (DS2 / "ds2_addon.py").exists() else [])


def _copy(name: str, tmp: Path) -> Path:
    src = DS2 if name == "ds2" else EXAMPLES / name
    stem = "ds2_addon" if name == "ds2" else name
    shutil.copytree(src, tmp / name, ignore=shutil.ignore_patterns("layout"))
    return tmp / name / f"{stem}.py"


def _traced_build(board: Path, **kw):
    with trace.tracing("proof", board.parent) as t:
        result = build_job(board, force=True, **kw)
    return result, list(t.events)


def _before_emit(events):
    marks = [e[0] for e in events if e[1] == "mark" and e[2] == "emit"]
    assert marks, "the route stage marks its emit"
    first = marks[0]
    return [e for e in events if e[0] < first], first


@pytest.mark.kicad
@pytest.mark.parametrize("name", BOARDS)
def test_no_board_file_is_opened_run_or_decompiled_before_emit(name: str, tmp_path: Path):
    """P1 on every board: the first `.kicad_pcb` the build touches is the one emit writes."""
    board = _copy(name, tmp_path)
    result, events = _traced_build(board, upto="route")
    assert result.get("error") is None or str(result["error"]).startswith("unrouted:"), result.get("error")
    before, _emit = _before_emit(events)
    opened = [e for e in before if e[1] in ("open-r", "open-w") and e[3] != "core_probe"]
    assert opened == [], ("a board file opened before emit", opened[:5])
    ran = [e for e in before if e[1] == "popen" and any(str(a).endswith(".kicad_pcb") for a in e[2]) and e[3] != "core_probe"]
    assert ran == [], ("a subprocess handed a board file before emit", ran[:5])
    decompiled = [e for e in before if e[1] == "decompile" and e[3] != "core_probe"]
    assert decompiled == [], ("decompile before emit", decompiled[:5])
    # The first board written is the emitted one, and nothing under `placed/` is read at all.
    writes = [e for e in events if e[1] == "open-w"]
    assert writes and writes[0][2].endswith("/routed/layout.kicad_pcb"), writes[:3]
    assert not [e for e in events if e[1] in ("open-r", "open-w") and "/placed/" in e[2]], "the route stage reads no placed board"


@pytest.mark.kicad
def test_the_core_probe_is_the_one_board_before_emit_and_it_is_named_by_its_module(tmp_path: Path):
    """The exemption P1 makes is the M1 probe's own module, and it is used: with a core line, the probe
    writes and KiCad saves one probe board before the emit, and those events are `core_probe`'s."""
    board = _copy("blinky", tmp_path)
    (board.parent / "layout.core.py").write_text('Line((2.0, 2.0), (6.0, 2.0), width=0.15, layer="F.SilkS")\n')
    result, events = _traced_build(board, upto="route")
    assert result.get("error") is None, result.get("error")
    before, _emit = _before_emit(events)
    board_file = lambda e: any(str(a).endswith(".kicad_pcb") for a in (e[2] if e[1] == "popen" else [e[2]]))  # noqa: E731
    probe = [e for e in before if (e[1] in ("open-w", "open-r", "popen") and board_file(e)) or e[1] == "decompile"]
    assert probe and {e[3] for e in probe} == {"core_probe"}, probe
    assert any(e[1] == "popen" and "upgrade" in e[2] for e in probe), probe


@pytest.mark.kicad
def test_every_subprocess_a_build_runs_is_kicad_cli(tmp_path: Path):
    """P2, dynamically: a full build to fab runs `kicad-cli` and nothing else."""
    board = _copy("buck", tmp_path)
    result, events = _traced_build(board, upto="fab")
    assert result.get("ok"), result.get("error")
    exes = {Path(str(e[2][0])).name for e in events if e[1] == "popen"}
    assert exes and all(x.startswith("kicad-cli") for x in exes), exes


_KRT = re.compile(r"krt|kicadroutingtools", re.I)


def _docstrings(tree) -> set[int]:
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            out.add(id(node.value))
    return out


def test_no_identifier_or_string_in_the_package_names_the_old_router():
    """P2, statically: outside docstrings and comments (history may keep the word), no identifier,
    attribute, argument, import or string literal in `src/pcbc` names KRT."""
    hits = []
    for f in sorted((ROOT / "src" / "pcbc").rglob("*.py")):
        tree = ast.parse(f.read_text())
        docs = _docstrings(tree)
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and _KRT.search(node.id):
                hits.append((f.name, node.lineno, node.id))
            elif isinstance(node, ast.Attribute) and _KRT.search(node.attr):
                hits.append((f.name, node.lineno, node.attr))
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and _KRT.search(node.name):
                hits.append((f.name, node.lineno, node.name))
            elif isinstance(node, ast.arg) and _KRT.search(node.arg):
                hits.append((f.name, node.lineno, node.arg))
            elif isinstance(node, ast.alias) and _KRT.search(node.name):
                hits.append((f.name, node.lineno, node.name))
            elif isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docs and _KRT.search(node.value):
                hits.append((f.name, node.lineno, node.value[:80]))
    assert hits == [], hits


def test_the_old_router_module_and_its_switches_are_gone():
    assert importlib.util.find_spec("pcbc.route") is None and importlib.util.find_spec("pcbc.blocking") is None
    toml = (ROOT / "pyproject.toml").read_text()
    assert "krt" not in toml.lower(), "the pytest marker is gone; --strict-markers makes a leftover mark an error"
    for env in ("KRT_HOME", "PCBC_REQUIRE_KRT", "PCBC_KRT_TIMEOUT", "PCBC_PATTERNS"):
        for f in (ROOT / "src" / "pcbc").rglob("*.py"):
            tree = ast.parse(f.read_text())
            docs = _docstrings(tree)
            assert not [n for n in ast.walk(tree) if isinstance(n, ast.Constant) and n.value == env and id(n) not in docs], (env, f.name)


def test_the_route_stage_writes_no_board_but_the_emitted_one():
    """The files the route stage may leave are its gen, the emitted board and its sidecars, and the
    problem files: nothing else (`layout_job.stage_outputs`)."""
    from pcbc.layout_job import UNROUTED_DIR, stage_outputs

    names = {p.name for p in stage_outputs(Path("routed") / "layout.kicad_pcb")}
    assert names == {"layout.gen.py", "layout.kicad_pcb", "layout.kicad_pro", "layout.kicad_dru", "layout.kicad_prl"}
    assert UNROUTED_DIR == "unrouted"


@pytest.mark.kicad
def test_an_unfinished_board_is_byte_identical_from_two_absolute_paths(tmp_path: Path):
    """P3 on a board N0 does not finish: c3_usb built from two directories gives the same
    `layout.gen.py`, the same emitted board and the same problem files, byte for byte (an unrouted
    net is as deterministic as a routed one: the router's order, ties and budget are all counts)."""
    import hashlib

    dirs = [tmp_path / "a", tmp_path / "some" / "deeper dir" / "b"]
    trees = []
    for d in dirs:
        d.mkdir(parents=True)
        board = _copy("c3_usb", d)
        result = build_job(board, upto="route", force=True)
        assert str(result["error"]).startswith("unrouted:"), result["error"]
        routed = board.parent / "layout" / "c3_usb" / "routed"
        trees.append({p.relative_to(routed).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in routed.rglob("*") if p.is_file()})
    assert trees[0] == trees[1], sorted(k for k in trees[0] if trees[0].get(k) != trees[1].get(k))
    assert any(k.startswith("unrouted/") for k in trees[0]) and "layout.gen.py" in trees[0]
