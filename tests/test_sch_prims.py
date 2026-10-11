"""docs/schematic-layout.md PR 6: the sheet is emitted from gen objects (docs/direction.md §4 applied to the sheet).

One Python object per KiCad primitive, field for field (`sch_prims`); `sheet_text` is the one writer, in KiCad's own
order and layout, so `kicad-cli sch upgrade` saves the body back byte for byte; `decompile` reads a sheet back into
the same objects and refuses a token it has no row for; the sch stage writes the primitives into schematic.gen.py,
loads them back and emits the sheet from what it loaded. Each test fails when the behaviour it pins is removed.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from pcbc.language import load_board
from pcbc.sch_emit import emit_from_design
from pcbc.sch_gen import PRIMS_HEAD, gen_text
from pcbc.sch_prims import census, decompile, gen_lines, load_gen, sheet_text, sort_key, unmapped

ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = ROOT / "examples"
CASES = [(b, core) for b in ("blinky", "buck", "c3_usb", "node") for core in (True, False)]
IDS = [f"{b}-{'core' if c else 'auto'}" for b, c in CASES]


def _sheet(name: str, core: bool):
    design = load_board(EXAMPLES / name / f"{name}.py", schematic_core=core)
    rep: dict = {}
    text = emit_from_design(design, title=name, report=rep)
    return design, text, rep


@pytest.fixture(scope="module")
def sheets():
    return {(b, c): _sheet(b, c) for b, c in CASES}


@pytest.mark.parametrize("case", CASES, ids=IDS)
def test_the_sheet_decompiles_to_the_primitives_it_was_written_from_and_back(sheets, case):
    design, text, rep = sheets[case]
    header, prims = decompile(text)
    assert sorted(prims, key=sort_key) == sorted(rep["_prims"], key=sort_key), "field for field"
    assert sheet_text(header, prims) == text, "KiCad -> Python -> KiCad is the same file"
    assert unmapped(census(text)) == []


@pytest.mark.parametrize("case", CASES, ids=IDS)
def test_gen_holds_every_primitive_and_the_sheet_decompiles_to_gen_byte_for_byte(sheets, case):
    design, text, rep = sheets[case]
    gen = gen_text(design, rep, case[0])
    held = gen.split(PRIMS_HEAD + "\n", 1)[1].splitlines()
    assert held == gen_lines(decompile(text)[1])
    assert gen_lines(load_gen(gen)) == held, "the lines load back as the same objects"
    assert sheet_text(rep["_header"], load_gen(gen)) == text, "the sheet is what gen's lines emit"


@pytest.mark.kicad
@pytest.mark.parametrize("case", CASES, ids=IDS)
def test_kicad_saves_the_body_back_byte_for_byte(sheets, case, tmp_path: Path):
    """`sheet_text` writes KiCad's own order and layout and every field KiCad writes (a label's direction in its angle,
    the Footprint/Datasheet/Description fields, body_style, in_pos_files): KiCad's save of the sheet has the same body."""
    from pcbc.fab import kicad_cli

    _design, text, _rep = sheets[case]
    f = tmp_path / "s.kicad_sch"
    f.write_text(text)
    subprocess.run([str(kicad_cli()), "sch", "upgrade", "--force", str(f)], capture_output=True, check=True)
    saved = f.read_text()

    def body(t):
        from pcbc.sch_prims import _lib_text

        a = t.index("\t(lib_symbols\n") + len("\t(lib_symbols\n")
        start = a + len(_lib_text(t, a)) + len("\t)\n")
        return t[start : t.index("\t(sheet_instances")]

    assert body(saved) == body(text)
    assert sorted(decompile(saved)[1], key=sort_key) == sorted(decompile(text)[1], key=sort_key)
    assert unmapped(census(saved)) == [], "every token KiCad writes for these primitives has a field"


def test_a_token_no_primitive_maps_is_refused():
    from pcbc.sch_prims import Unmapped

    text = sheet_text(*decompile(_sheet("blinky", True)[1]))
    bad = text.replace("\t\t(diameter 0)\n", "\t\t(diameter 0)\n\t\t(radius 1)\n", 1) if "(junction" in text else text.replace("\t(wire\n", "\t(wire\n\t\t(bulge 1)\n", 1)
    with pytest.raises(Unmapped):
        decompile(bad)
    with pytest.raises(Unmapped):
        decompile(text.replace("\t(sheet_instances", "\t(bus_entry\n\t\t(at 0 0)\n\t)\n\t(sheet_instances", 1))


@pytest.mark.parametrize("line", ['SheetWire((10, 10), (20, 10), width=0, style="default", uuid="x")', 'SheetJunction((10, 10), diameter=0, color=(0, 0, 0, 0), uuid="x")'])
def test_a_primitive_line_copied_into_schematic_core_is_refused_as_no_lock(tmp_path: Path, line: str):
    board = tmp_path / "blinky"
    shutil.copytree(EXAMPLES / "blinky", board, ignore=shutil.ignore_patterns("layout"))
    (board / "schematic.core.py").write_text(line + "\n")
    with pytest.raises(ValueError) as got:
        load_board(board / "blinky.py")
    msg = str(got.value)
    assert msg.startswith("schematic.core.py:1: ") and "is a primitive of the drawn sheet" in msg and "it is not a lock" in msg, msg


@pytest.mark.kicad
def test_the_sch_stage_emits_the_sheet_from_the_gen_file(tmp_path: Path, monkeypatch):
    """The sheet is what schematic.gen.py's primitive lines say: a line added to the gen text the stage writes lands in
    the sheet, and a sheet that does not decompile to gen's lines is refused as a generator bug."""
    import pcbc.sch_gen as G
    import pcbc.sch_prims as P
    from pcbc.build import sch_stage
    from pcbc.project import layout_dir

    board = tmp_path / "blinky"
    shutil.copytree(EXAMPLES / "blinky", board, ignore=shutil.ignore_patterns("layout"))
    path = (board / "blinky.py").resolve()
    real = G.gen_text
    monkeypatch.setattr(G, "gen_text", lambda *a, **k: real(*a, **k) + 'SheetText("FROM GEN", at=(20, 20), angle=0, exclude_from_sim=False, size=2.54, justify=("left", "top"), uuid="00000000-0000-4000-8000-000000000001")\n')
    step, err = sch_stage(load_board(path, core=False), path, layout_dir(path), name="blinky")
    assert err is None, err
    assert '(text "FROM GEN"' in (layout_dir(path) / "schematic.kicad_sch").read_text()
    monkeypatch.setattr(G, "gen_text", real)
    writer = P.sheet_text
    monkeypatch.setattr(P, "sheet_text", lambda h, prims: writer(h, sorted(prims, key=sort_key)[:-1]))
    step, err = sch_stage(load_board(path, core=False), path, layout_dir(path), name="blinky")
    assert err is not None and "does not decompile to schematic.gen.py's primitives" in err, err


def test_every_uuid_is_carried_once_in_drawing_order():
    """`sch_emit._dedupe_ids`: ids are keyed by what they name, so two objects drawn with one key (a wire drawn twice by
    two placements, a symbol's pin) would share it; the second gets the id of (that id, 1), the third (that id, 2), in
    drawing order, symbols' pin ids included (refute:tests: no test pinned it)."""
    from pcbc.sch_emit import _dedupe_ids
    from pcbc.sch_prims import SheetSymbol, SheetWire
    from pcbc.sexp import stable_uuid

    u = "11111111-1111-5111-8111-111111111111"
    w = SheetWire((0.0, 0.0), (1.27, 0.0), uuid=u)
    sym = SheetSymbol("R", (5.08, 5.08), uuid="s", pins=(("1", u), ("2", "p2")))
    out = _dedupe_ids([w, w, sym], "t")
    assert [out[0].uuid, out[1].uuid] == [u, stable_uuid("t", u, 1)]
    assert out[2].pins == (("1", stable_uuid("t", u, 2)), ("2", "p2")) and out[2].uuid == "s"
