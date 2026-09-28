"""The sixth adversarial review's findings, closed by three mechanisms instead of more cases
(docs/layout-properties.md, "Seventh round: three mechanisms"). One test pins each mechanism.

M1: every core line is read back by KiCad at load (`core_probe.round_trip`): one probe board, one
    `kicad-cli pcb upgrade`, every field compared; a line KiCad cannot load is bisected to.
M2: every stage's stamp is what the stage was seen to read (`trace`): files the process opened, the
    directories and argv handed to `kicad-cli`/KRT, the environment it read, the tool versions.
M3: fab ships exactly the fill the gates judged, from the compiled job's sidecars, byte for byte."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import routed_board
from pcbc.language import load_board, load_layout
from pcbc.model import BoardSpec, Copper, Design

ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = ROOT / "examples"
FIX = Path(__file__).resolve().parent / "fixtures" / "kicad10"
PROBE_BASE = FIX / "kicad10_primitives3.kicad_pcb"


def _core(tmp_path: Path, text: str, layers: int = 4) -> tuple[Design, list, list]:
    """A core file loaded as the route stage loads it (the explicit checks included), stamped."""
    from pcbc.layout_emit import stamp

    f = tmp_path / "layout.core.py"
    f.write_text(text)
    d = Design()
    d.board = BoardSpec((60.0, 60.0), layers=layers)
    load_layout(f, d, source="core")
    return d, stamp(d, list(d.copper)), list(d.graphics)


# ------------------------------------------------------------------------------------------ M1

# One line of each class the sixth review found reaching the router: KiCad rewrites it (a through via
# on an inner layer, a colour stored as 0-255 integers, a width clamped to its floor, a precision it
# does not keep), completes it (a partial keepout), drops it on another field's say (a hatch gap on a
# solid pour, a group that holds itself), cannot parse it (a list of the wrong length), or the emitter
# cannot write it (a required yes/no left None). Not one of these has an explicit rule of its own.
M1_CASES = [
    ('Via("GND", (10.0, 10.0), size=0.6, drill=0.3, layers=("F.Cu", "In1.Cu"), kind="through")', r"layers \(written \('F.Cu', 'In1.Cu'\), KiCad's load of the line has \('F.Cu', 'B.Cu'\): write that\)"),
    ('Line((1.0, 1.0), (5.0, 1.0), layer="F.SilkS", width=0.1, stroke_color=(0.5, 0.5, 0.5, 0.5))', r"stroke_color \(written \(0.5, 0.5, 0.5, 0.5\), KiCad's load of the line has \(0.0, 0.0, 0.0, 0.5\)"),
    ('Line((1.0, 1.0), (5.0, 1.0), layer="F.SilkS", width=0.0)', r"width \(written 0.0, KiCad's load of the line has 0.1: write that\)"),
    ('Pour("GND", layer="F.Cu", points=[(1.0, 1.0), (5.0, 1.0), (5.0, 5.0), (1.0, 5.0)], island_removal_mode=2, island_area_min=1.23456789)', r"island_area_min \(written 1.23456789, KiCad's load of the line has 1.234568"),
    ('Pour(None, layer="F.Cu", points=[(1.0, 1.0), (5.0, 1.0), (5.0, 5.0), (1.0, 5.0)], keepout={"tracks": "not_allowed"})', r"keepout.pads \(written None, KiCad's load of the line has 'allowed'"),
    ('Pour("GND", layer="F.Cu", points=[(1.0, 1.0), (5.0, 1.0), (5.0, 5.0), (1.0, 5.0)], hatch_gap=0.4)', r"hatch_gap \(written 0.4, KiCad's load of the line has None"),
    ('Line((1.0, 1.0), (5.0, 1.0), layer="Cmts.User", width=0.1, id="l1")\nGroup("g", ("g1",), id="g1")', r"layout.core.py:2: KiCad's load of the line has no such object"),
    ('TextBox("b", (2.0, 2.0), (8.0, 6.0), layer="F.SilkS", margins=(1.0, 2.0))', r"layout.core.py:1: KiCad refuses to load the line \(Failed to load board: need a number for 'right margin'"),
    ('Line((1.0, 1.0), (5.0, 1.0), layer="F.SilkS", width=0.1, stroke_type=None)', r"layout.core.py:1: \(gr_line\) \(stroke\): stroke_type is required"),
]


@pytest.mark.kicad
@pytest.mark.parametrize(("line", "says"), M1_CASES, ids=[f"m1_{i}" for i in range(len(M1_CASES))])
def test_kicad_reads_every_core_line_at_load_and_any_difference_names_the_line(tmp_path: Path, line: str, says: str):
    """M1 (sixth review: 17 build probes and 5 majors, each refused only after the whole board was
    routed, or not at all): the core objects go on one probe board, KiCad loads and saves it, and each
    object is compared field for field; the refusal names `layout.core.py:N`, the field, what was
    written and what KiCad kept."""
    from pcbc.core_probe import round_trip

    d, cu, gr = _core(tmp_path, line + "\n")
    refusals, rec = round_trip(d, cu, gr, PROBE_BASE.read_text(), name="probe")
    assert refusals, (line, rec)
    assert any(re.search(says, r) for r in refusals), refusals
    assert all(r.startswith("layout.core.py:") for r in refusals), refusals


@pytest.mark.kicad
def test_a_line_kicad_cannot_load_is_bisected_to_among_good_ones(tmp_path: Path):
    """M1's bisection: two lines KiCad refuses to load, among forty it loads, are each named with
    KiCad's own message, in a handful of probe boards rather than one per line."""
    from pcbc.core_probe import round_trip

    good = [f'Line(({x}.0, 1.0), ({x}.0, 5.0), layer="F.SilkS", width=0.1)' for x in range(1, 41)]
    lines = good[:13] + ['TextBox("b", (2.0, 2.0), (8.0, 6.0), layer="F.SilkS", margins=(1.0, 2.0, 3.0))'] + good[13:30] + ['Barcode("q", (5.0, 21.0), layer="F.SilkS", margins=(0.1,))'] + good[30:]
    d, cu, gr = _core(tmp_path, "\n".join(lines) + "\n")
    refusals, rec = round_trip(d, cu, gr, PROBE_BASE.read_text(), name="probe")
    assert len(refusals) == 2, refusals
    assert refusals[0].startswith("layout.core.py:14: KiCad refuses to load the line") and "bottom margin" in refusals[0], refusals
    assert refusals[1].startswith("layout.core.py:32: KiCad refuses to load the line") and "margin Y" in refusals[1], refusals
    assert rec["bisected"] and rec["kicad_calls"] <= 1 + 2 * 7 * 2, rec


@pytest.mark.kicad
@pytest.mark.parametrize("name", ["blinky", "buck"])
def test_every_gen_line_passes_kicad_s_reading_in_one_call(name: str, tmp_path: Path):
    """M1 is exact, not strict: every copper and drawing line a build wrote into `layout.gen.py` (the
    lines the header invites an author to lock) comes back from the probe as written, in one
    `kicad-cli` call. Measured on all five boards' gen, 9 to 366 lines: 0 refusals."""
    from pcbc.core_probe import round_trip
    from pcbc.layout_job import load_core
    from pcbc.layout_emit import stamp

    final = routed_board(name, EXAMPLES / name / f"{name}.py")
    lines = [ln for ln in (final.parent / "layout.gen.py").read_text().splitlines() if re.match(r"[A-Z][A-Za-z]*\(", ln) and not ln.startswith("Place(") and 'Group("pcbc:' not in ln and 'layer="Edge.Cuts"' not in ln]
    shutil.copytree(EXAMPLES / name, tmp_path / name, ignore=shutil.ignore_patterns("layout"))
    board = tmp_path / name / f"{name}.py"
    (board.parent / "layout.core.py").write_text("\n".join(lines) + "\n")
    from pcbc.place_native import place

    design = load_board(board)
    placed = place(load_board(board), name=name).text
    cu, gr = load_core(design, board, placed, name=name)
    refusals, rec = round_trip(design, stamp(design, cu), gr, placed, name=name)
    assert refusals == [] and rec["kicad_calls"] == 1 and rec["objects"] == len(lines) > 5, (refusals[:3], rec)


@pytest.mark.kicad
def test_the_route_stage_refuses_a_line_kicad_rewrites_before_it_routes(tmp_path: Path):
    """M1 is wired into the route stage before the router runs: the refusal comes back before any
    routing and with no board on disk (sixth review: `stroke_color=(0.5, ...)` routed blinky, then was
    refused)."""
    from pcbc.build import build_job

    shutil.copytree(EXAMPLES / "blinky", tmp_path / "blinky", ignore=shutil.ignore_patterns("layout"))
    board = tmp_path / "blinky" / "blinky.py"
    (board.parent / "layout.core.py").write_text('Line((15.0, 3.0), (20.0, 3.0), layer="F.SilkS", width=0.1, stroke_color=(0.5, 0.5, 0.5, 0.5))\n')
    result = build_job(board, upto="route", force=True)
    route = next(s for s in result["steps"] if s["stage"] == "route")
    assert result["error"].startswith("a core line is refused: layout.core.py:1: KiCad's load of the line differs in stroke_color"), result["error"]
    assert "links" not in route and route["core_probe"]["kicad_calls"] == 1
    assert not list((board.parent / "layout" / "blinky" / "routed").glob("*.kicad_pcb")), "no board file of any kind"


# ------------------------------------------------------------------------------------------ M2


def test_a_stage_stamps_what_it_was_seen_to_read_and_nothing_it_wrote(tmp_path: Path, monkeypatch):
    """M2's tracer, alone: a file the stage opens for reading is stamped by content (a `.pyc` as its
    source), a file it wrote is not, Python's library and pcbc's code are not, the environment it read
    is, and a subprocess's directory is split into inputs (there before) and outputs (written now);
    `drift` names the first that changed, a file that appeared beside a tool's input included."""
    from pcbc.trace import drift, tracing

    (tmp_path / "helper_mod_m2.py").write_text("X = 5\n")
    (tmp_path / "data.json").write_text('{"x": 1}\n')
    tool_dir = tmp_path / "handed"
    tool_dir.mkdir()
    (tool_dir / "input.txt").write_text("in\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setenv("PCBC_M2_PROBE", "a")
    sys.modules.pop("helper_mod_m2", None)
    with tracing("route", tmp_path) as seen:
        import helper_mod_m2  # noqa: F401
        json.loads((tmp_path / "data.json").read_text())
        (tmp_path / "out.txt").write_text("mine")
        (tmp_path / "out.txt").read_text()
        os.environ.get("PCBC_M2_PROBE")
        os.environ.get("HOME")
        subprocess.run(["/bin/cat", str(tool_dir / "input.txt")], capture_output=True, check=True)
        (tool_dir / "written.txt").write_text("out")
    sys.modules.pop("helper_mod_m2", None)
    rec = seen.record()
    assert set(rec["reads"]) == {"helper_mod_m2.py", "data.json"}, rec["reads"]
    assert rec["env"] == {"PCBC_M2_PROBE": "a"}, rec["env"]
    assert rec["dirs"] == {"handed": {"inputs": {"input.txt": rec["dirs"]["handed"]["inputs"]["input.txt"]}, "outputs": ["written.txt"]}}, rec["dirs"]
    assert rec["argv"] == [["/bin/cat", "./handed/input.txt"]], rec["argv"]
    assert drift(rec, tmp_path, "route") is None
    (tmp_path / "helper_mod_m2.py").write_text("X = 7\n")
    assert drift(rec, tmp_path, "route") == "helper_mod_m2.py changed since the route stage read it (a file the route stage opened; edited)"
    (tmp_path / "helper_mod_m2.py").write_text("X = 5\n")
    (tool_dir / "input.kicad_pro").write_text("{}")
    assert drift(rec, tmp_path, "route").startswith("handed/input.kicad_pro is beside a file a route-stage tool was handed and was not there")
    (tool_dir / "input.kicad_pro").unlink()
    monkeypatch.setenv("PCBC_M2_PROBE", "b")
    assert drift(rec, tmp_path, "route") == "the environment changed since the route stage read it: PCBC_M2_PROBE was 'a', now 'b'"
    assert drift(None, tmp_path, "route").startswith("the route stage has no record of what it read")


def test_a_net_name_kicad_cannot_hold_is_refused_at_load_naming_the_line(tmp_path: Path):
    """Sixth review: `LED\\b` and `LED\\nX` escaped `build_job` as tracebacks out of the sch stage.
    Refused when board.py is loaded, naming the line; the build returns the refusal."""
    from pcbc.build import build_job

    for bad, says in (("LED\\b", "carries a backslash"), ("LED\nX", "carries the control character '\\n'")):
        board = tmp_path / bad.encode().hex() / "blinky.py"
        board.parent.mkdir()
        text = (EXAMPLES / "blinky" / "blinky.py").read_text()
        board.write_text(text.replace('"LED"', repr(bad)))
        with pytest.raises(ValueError, match=r"board.py line 5: the net name .* " + re.escape(says)):
            load_board(board)
        result = build_job(board, upto="fab", force=True)
        assert result["error"].startswith("board.py line 5: the net name") and says in result["error"], result["error"]
    ok = tmp_path / "tab" / "blinky.py"
    ok.parent.mkdir()
    ok.write_text((EXAMPLES / "blinky" / "blinky.py").read_text().replace('"LED"', repr("LED\tX")))
    assert "LED\tX" in load_board(ok).nets


@pytest.mark.kicad
def test_an_input_outside_board_py_and_core_is_stale_from_the_stage_that_read_it(tmp_path: Path, monkeypatch):
    """M2 on a real build (sixth review: three majors, one mechanism). A helper module and a data file
    `layout.core.py` reads, `PCBC_STRICT_PATTERNS`, a `schematic.kicad_pro` beside the schematic, and
    `SOURCE_DATE_EPOCH` each make a plain build stale from the stage that read them, named; none was
    listed anywhere."""
    from pcbc.build import build_job, stale_reason

    monkeypatch.delenv("PCBC_STRICT_PATTERNS", raising=False)
    monkeypatch.delenv("SOURCE_DATE_EPOCH", raising=False)
    shutil.copytree(EXAMPLES / "blinky", tmp_path / "blinky", ignore=shutil.ignore_patterns("layout"))
    board = tmp_path / "blinky" / "blinky.py"
    (board.parent / "corehelp_m2.py").write_text("X = 5.0\n")
    (board.parent / "corehelp.json").write_text('{"Y": 2.0}\n')
    (board.parent / "layout.core.py").write_text(
        "import json, os, sys\nsys.path.insert(0, os.path.dirname(__file__))\nfrom corehelp_m2 import X\n"
        "Y = json.load(open(os.path.join(os.path.dirname(__file__), 'corehelp.json')))['Y']\n"
        'Line((2.0, Y), (X, Y), width=0.15, layer="F.SilkS")\n'
    )
    assert build_job(board, upto="fab", force=True).get("ok")
    layout = board.parent / "layout" / "blinky"
    assert stale_reason(layout, board) == ("fab", None)
    assert "corehelp_m2" not in sys.modules and str(board.parent) not in sys.path  # the core's import and path went with its load

    (board.parent / "corehelp_m2.py").write_text("X = 7.0\n")
    assert stale_reason(layout, board) == ("place", "corehelp_m2.py changed since the route stage read it (a file the route stage opened; edited)")
    (board.parent / "corehelp_m2.py").write_text("X = 5.0\n")
    (board.parent / "corehelp.json").write_text('{"Y": 3.0}\n')
    assert stale_reason(layout, board) == ("place", "corehelp.json changed since the route stage read it (a file the route stage opened; edited)")
    (board.parent / "corehelp.json").write_text('{"Y": 2.0}\n')
    assert stale_reason(layout, board) == ("fab", None)

    monkeypatch.setenv("PCBC_STRICT_PATTERNS", "1")
    assert stale_reason(layout, board) == ("place", "the environment changed since the route stage read it: PCBC_STRICT_PATTERNS was unset, now '1'")
    monkeypatch.delenv("PCBC_STRICT_PATTERNS")
    monkeypatch.setenv("SOURCE_DATE_EPOCH", "1700000000")
    assert stale_reason(layout, board) == ("route", "the environment changed since the fab stage read it: SOURCE_DATE_EPOCH was unset, now '1700000000'")
    monkeypatch.delenv("SOURCE_DATE_EPOCH")

    (layout / "schematic.kicad_pro").write_text('{"erc": {"rule_severities": {"lib_symbol_issues": "ignore"}}}\n')
    assert stale_reason(layout, board)[0] is None and stale_reason(layout, board)[1].startswith("layout/blinky/schematic.kicad_pro is beside a file a sch-stage tool was handed")
    result = build_job(board, upto="fab")
    assert result.get("ok") and result["plan"][0] == "check" and not (layout / "schematic.kicad_pro").exists(), result.get("error")
    assert stale_reason(layout, board) == ("fab", None)
    stamp = json.loads((layout / "inputs.json").read_text())["traced"]
    from pcbc.trace import code_sha

    assert set(stamp["reads"]) >= {"corehelp_m2.py", "corehelp.json"} and stamp["tools"]["pcbc"] == code_sha() and "krt" not in stamp["tools"]
    assert stamp["tools"]["kicad-cli"]["version"] and all(not a.startswith(str(tmp_path)) for argv in stamp["argv"] for a in argv)


# ------------------------------------------------------------------------------------------ M3


@pytest.mark.kicad
def test_fab_ships_the_judged_fill_exactly_from_the_compiled_sidecars(tmp_path: Path, monkeypatch):
    """M3 (sixth review BLOCKER): an area-neutral `.kicad_dru` edit made inside fab moved buck's GND
    pour to 0.0105 mm from a 5V pad and shipped `ok=True`, because the fill was compared as (islands,
    area) to a tolerance and fab's sidecars were never checked. Now fab's sidecars must be the compiled
    job's byte for byte, and a fill vertex moved by a nanometre is a refusal naming the zone."""
    import pcbc.fab as FAB
    from pcbc.compile import compile_design
    from pcbc.fab import fab_board_diff, fab_job

    final = routed_board("buck", EXAMPLES / "buck" / "buck.py")
    design = load_board(EXAMPLES / "buck" / "buck.py")
    job = compile_design(design)
    txt = final.read_text()
    m = re.search(r"\(filled_polygon[^(]*\(layer \"[^\"]+\"\)\s*\(pts\s*\(xy ([-0-9.]+) ([-0-9.]+)\)", txt)
    assert m
    nudged = txt[: m.start(1)] + f"{float(m.group(1)) + 1e-6:.6f}" + txt[m.end(1) :]
    said = fab_board_diff(txt, nudged)
    assert len(said) == 1 and re.match(r"zone \S+'s fill on \S+ is not the routed board's vertex for vertex", said[0]), said

    rules = ("\n(rule \"evVIN\" (constraint clearance (min 0.36113mm)) (condition \"A.Type == 'Zone' && B.NetName == 'VIN'\"))\n"
             "(rule \"ev5V\" (constraint clearance (min 0.01mm)) (condition \"A.Type == 'Zone' && B.NetName == '5V'\"))\n")
    orig = FAB.check_job

    def tamper(job_, work, *a, **k):
        dru = Path(work).with_suffix(".kicad_dru")
        dru.write_text(dru.read_text() + rules)
        return orig(job_, work, *a, **k)

    monkeypatch.setattr(FAB, "check_job", tamper)
    bad = fab_job(job, final, out_dir=tmp_path / "tampered", design=design)
    assert bad["error"] == "fab's sidecars are not the compiled job's: fab/layout.kicad_dru is not the file the compiled job writes (fab never edits its rules)", bad["error"]
    assert not list((tmp_path / "tampered" / "gerbers").glob("*"))
    monkeypatch.setattr(FAB, "check_job", orig)
    clean = fab_job(job, final, out_dir=tmp_path / "clean", design=design)
    assert clean["error"] is None and clean["copper_equal"] is True, clean["error"]


# ------------------------------------------------------------------------------------------ minors


def test_adoption_reads_a_segment_s_two_ends_as_a_set():
    """Sixth review: a KRT segment locked with its ends swapped is the same copper, but was not
    adopted, became a real lock and re-routed 10 of buck's 84 objects."""
    from pcbc.layout_job import adopt_key

    a = Copper("seg", "s1", net="GND", layer="F.Cu", a=(1.0, 2.0), b=(3.0, 4.0), width=0.2, uuid="u1")
    b = Copper("seg", "s2", net="GND", layer="F.Cu", a=(3.0, 4.0), b=(1.0, 2.0), width=0.2, uuid="u2")
    c = Copper("seg", "s3", net="GND", layer="F.Cu", a=(3.0, 4.0), b=(1.0, 2.5), width=0.2, uuid="u3")
    assert adopt_key(a) == adopt_key(b) != adopt_key(c)


def test_a_rebuild_leaves_no_sidecar_of_an_earlier_build_behind(tmp_path: Path):
    """Sixth review: `routed/core.kicad_pro` and `.kicad_dru` survived a rebuild with the lock removed,
    so the tree was not a function of the sources. The native route stage writes no lock board at all;
    every file it writes — gen, the emitted board and its three sidecars — is a stage output, unlinked
    before it starts."""
    from pcbc.layout_job import stage_outputs

    names = {p.name for p in stage_outputs(tmp_path / "routed" / "layout.kicad_pcb")}
    assert names == {"layout.gen.py", "layout.kicad_pcb", "layout.kicad_pro", "layout.kicad_dru", "layout.kicad_prl"}


def test_a_pour_outline_needs_three_points_enclosing_an_area(tmp_path: Path):
    for pts, n in (("[(1.0, 1.0), (5.0, 1.0)]", 2), ("[(1.0, 1.0), (5.0, 1.0), (9.0, 1.0)]", 3)):
        f = tmp_path / "layout.core.py"
        f.write_text(f'Pour("GND", layer="F.Cu", points={pts})\n')
        with pytest.raises(ValueError, match=rf"layout.core.py:1: .*a pour outline needs at least three points enclosing an area, got {n} distinct"):
            load_layout(f, Design(), source="core")
