"""The fifth adversarial review's defects, one test per structural fix (docs/layout-properties.md,
"Fifth review"). C1: every file a stage reads or writes is stamped, stale from the stage that wrote it.
C2: fab is held to the routed board node for node, in order, uuids included. C3: a core line is
refused at load naming the line, or KiCad keeps it as written. C4: adoption is equality with a piece
the build draws."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from conftest import routed_board
from pcbc.gen import Unmapped, decompile
from pcbc.language import load_board
from pcbc.sexp import board_footprint_spans, footprint_reference, parse_tree

ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = ROOT / "examples"
PNG = "iVBORw0KGgoAAAANSUhEUgAAAAwAAAADCAIAAAAoQXllAAAAUklEQVR4nA3KMQEAIRADwYhABCKuTk29NSIQERGIQAQiEPM/9UiiiS5KDDHFEhFbHHHFE5Jpppsyw0yzTMw2x1zz/KfQQg8VRphhhYQdTrjhhQ/ngSX5Bo9g+gAAAABJRU5ErkJggg=="


def _copy(name: str, tmp_path: Path) -> Path:
    shutil.copytree(EXAMPLES / name, tmp_path / name, ignore=shutil.ignore_patterns("layout"))
    return tmp_path / name / f"{name}.py"


# ----------------------------------------------------------------------------------------- C1


def test_pcb_job_never_vouches_for_a_schematic_and_a_changed_board_py_redraws_it(tmp_path: Path):
    """`pcbc pcb` then `pcbc build` never ran the sch stage: no schematic, or yesterday's with R1 1k
    while the BOM said 4k7, and the build said ok (fifth review, MAJOR). The schematic has its own
    record, written by the sch stage alone; without it, or drawn from another board.py, the build is
    stale from check and the sch stage runs."""
    from pcbc.build import SCH_INPUTS, pcb_job, planned, sch_stage, stale_reason

    board = _copy("blinky", tmp_path)
    layout = board.parent / "layout" / "blinky"
    assert not pcb_job(board).get("error")
    assert not (layout / "schematic.kicad_sch").exists() and not (layout / SCH_INPUTS).exists()
    stage, why = stale_reason(layout, board)
    assert stage is None and "the sch stage has not run" in why and "sch" in planned(stage, upto="fab", force=False)
    _step, err = sch_stage(load_board(board), board, layout, name="blinky")
    assert err is None and stale_reason(layout, board) == ("place", None)
    board.write_text(board.read_text().replace('"1k"', '"4k7"', 1))
    assert not pcb_job(board).get("error")  # re-places from the new board.py
    assert stale_reason(layout, board) == (None, "board.py, a module it imports or a part file changed since the schematic was drawn")


def test_the_place_stage_keeps_no_sidecar_it_did_not_write(tmp_path: Path):
    """A hand-written sidecar beside a stage's board was re-stamped as the stage's own (fifth review,
    on the old seed stage). The place stage writes its board and its `.kicad_pro`/`.kicad_dru` into a
    fresh `placed/`, so a `.kicad_prl` lying there is not its own: stale, and gone on the rebuild."""
    from pcbc.build import INPUTS, NEVER_EDITED, build_job, stale_reason

    board = _copy("blinky", tmp_path)
    layout = board.parent / "layout" / "blinky"
    assert build_job(board, upto="place", force=True).get("ok")
    prl = layout / "placed" / "layout.kicad_prl"
    prl.write_text("{}\n")
    assert stale_reason(layout, board) == ("sch", f"placed/layout.kicad_prl is not the file the place stage wrote (edited or deleted): {NEVER_EDITED}")
    assert build_job(board, upto="place").get("ok")
    assert not prl.exists() and set(json.loads((layout / "placed" / INPUTS).read_text())["sidecars"]) == {"layout.kicad_pro", "layout.kicad_dru"}


def test_fab_outputs_stamps_every_file_but_its_own_record(tmp_path: Path):
    """`fab_outputs` skipped every file named `inputs.json` at any depth, so `fab/gerbers/inputs.json`
    shipped unstamped (fifth review). Only the stamp itself is left out, by its path."""
    from pcbc.build import fab_outputs

    fab = tmp_path / "fab"
    (fab / "gerbers").mkdir(parents=True)
    (fab / "inputs.json").write_text("{}")
    (fab / "gerbers" / "inputs.json").write_text("{}")
    assert set(fab_outputs(tmp_path)) == {"gerbers/inputs.json"}


@pytest.mark.kicad
def test_a_fab_only_rerun_clears_fab_and_review_reads_only_fresh_stages(tmp_path: Path):
    """Two fifth-review MAJORs on one build. (1) A hand-added `fab/gerbers/layout-In1_Cu.g2` survived a
    fab-only rerun, was counted as an eleventh Gerber and stamped as fab's own: the fab stage now
    clears `fab/` first. (2) `pcbc review` read a hand-edited fab board and a hand-edited `bom.csv` and
    called them "KiCad DRC clean ... as board.py says": review reads a stage's files only when that
    stage's stamp matches, prints the stale reason in place of the verdicts, and draws its own
    schematic under `review/`, never over the build's."""
    from pcbc.build import build_job, stale_reason
    from pcbc.review import review_job

    board = _copy("blinky", tmp_path)
    layout = board.parent / "layout" / "blinky"
    assert build_job(board, upto="fab", force=True).get("ok")
    extra = layout / "fab" / "gerbers" / "layout-In1_Cu.g2"
    extra.write_text("G04 r5*\nM02*\n")
    assert stale_reason(layout, board)[0] == "route"
    again = build_job(board, upto="fab")
    assert again.get("ok") and again["plan"] == ["fab"] and not extra.exists() and stale_reason(layout, board) == ("fab", None)
    report = json.loads((layout / "fab" / "report.json").read_text())
    assert report["gerbers"] == 10
    sch_before = (layout / "schematic.kicad_sch").read_bytes()
    fab_board = layout / "fab" / "layout.kicad_pcb"
    fab_board.write_text(re.sub(r"\(width [0-9.]+\)", "(width 0.9)", fab_board.read_text(), count=1))
    got = review_job(board, open_html=False)
    assert got["stage"] == "route" and "fab/layout.kicad_pcb is not the file the fab stage wrote" in got["stale"]
    assert got["pcb"].endswith("routed/layout.kicad_pcb") and got["schematic"].endswith("review/schematic.kicad_sch")
    html = (layout / "review" / "index.html").read_text()
    assert "Copper: not judged" in html and "BOM: not shown" in html
    assert (layout / "schematic.kicad_sch").read_bytes() == sch_before and stale_reason(layout, board)[0] == "route"


# ----------------------------------------------------------------------------------------- C2


def test_the_fab_canonicaliser_keeps_order():
    """`_canon_node` sorted children and atoms, so `(size 1 1.45)` equalled `(size 1.45 1)`, `(at 29 14)`
    equalled `(at 14 29)`, and a square equalled a bowtie (fifth review BLOCKER)."""
    from pcbc.fab import _canon_node

    def c(text: str):
        return _canon_node(parse_tree(text))

    assert c("(size 1 1.45)") != c("(size 1.45 1)")
    assert c("(at 29 14 90)") != c("(at 14 29 90)")
    assert c("(drill oval 0.8 1.5)") != c("(drill oval 1.5 0.8)")
    square = "(pts (xy 0 0) (xy 1 0) (xy 1 1) (xy 0 1))"
    assert c(square) != c("(pts (xy 0 0) (xy 1 1) (xy 1 0) (xy 0 1))")
    assert c(square) == c(square) and c('(a "x" (uuid "1"))') != c('(a "x" (uuid "2"))') and c('(a "x" (uuid "1"))', ) == c('(a "x" (uuid "1"))')


def test_a_uuid_carried_twice_is_counted_by_item():
    """Fab refuses a uuid any two items carry (a stacked FID1 copy kept its original's), counting a
    dimension and its own text as one item: KiCad gives the text the dimension's uuid (the fix's first
    cut refused every board with a dimension, fifth review rerun `base_DIM`)."""
    from pcbc.fab import _uuid_counts, _uuid_owners

    u = "aaaaaaaa-1111-4111-8111-aaaaaaaaaaa1"
    dim = f'\t(dimension\n\t\t(type aligned)\n\t\t(uuid "{u}")\n\t\t(gr_text "x"\n\t\t\t(at 1 1 0)\n\t\t\t(uuid "{u}")\n\t\t)\n\t)\n'
    assert _uuid_counts("(kicad_pcb\n" + dim + ")\n")[u] == 1
    two = f'\t(gr_line\n\t\t(start 0 0)\n\t\t(end 1 0)\n\t\t(uuid "{u}")\n\t)\n'
    text = "(kicad_pcb\n" + dim + two + ")\n"
    assert _uuid_counts(text)[u] == 2 and _uuid_owners(text, u) == [f"dimension {u}", f"gr_line {u}"]


def test_insert_fiducials_reports_only_what_it_appended():
    """The existing fiducials were returned as if fab had inserted them, which armed the allowlist for a
    second FID1 stacked on the first (fifth review)."""
    from pcbc.fab import _fiducial_sexp, existing_fiducials, insert_fiducials

    bare = "(kicad_pcb\n\t(version 20260206)\n)\n"
    text, added = insert_fiducials(bare, (40.0, 30.0))
    assert [r for r, _x, _y in added] == ["FID1", "FID2", "FID3"] and existing_fiducials(text) == added
    again, none = insert_fiducials(text, (40.0, 30.0))
    assert none == [] and again == text
    one = _fiducial_sexp("FID1", 5, 5)
    uuids = re.findall(r'\(uuid "([^"]+)"\)', one)
    assert len(uuids) == len(set(uuids)) == 4 and "(hide yes)" in one


def _fp(text: str, ref: str) -> tuple[int, int]:
    for s, e in board_footprint_spans(text):
        if footprint_reference(text[s:e]) == ref:
            return s, e
    raise AssertionError(ref)


def _in_fp(ref, old, new):
    def f(text):
        s, e = _fp(text, ref)
        block = text[s:e]
        assert old in block, (ref, old)
        return text[:s] + block.replace(old, new, 1) + text[e:]

    return f


def _stack_fid(text):
    """A second FID1, paste only and no courtyard, on top of the routed board's FID1."""
    s, e = _fp(text, "FID1")
    b = text[s:e]
    b = re.sub(r'\(layers "F\.Cu" "F\.Mask"\)', '(layers "F.Paste")', b, count=1)
    b = re.sub(r"\n\t\t\(fp_circle[\s\S]*?\n\t\t\)", "", b, count=1)
    b = re.sub(r'\(uuid "([0-9a-f]{8})', lambda m: f'(uuid "{m.group(1)[:7]}e', b)
    return text[:e] + "\n\t" + b + text[e:]


def _ref_huge(text):
    s, e = _fp(text, "C_IN2")
    b = text[s:e]
    i = b.find('(property "Reference" "C_IN2"')
    j = b.find("\n\t\t)", i)
    blk = re.sub(r"\(size [0-9.]+ [0-9.]+\)", "(size 4 4)", b[i:j], count=1)
    return text[:s] + b[:i] + blk + b[j:] + text[e:]


@pytest.mark.kicad
def test_fab_holds_the_board_to_the_routed_one_in_order_and_leaves_nothing_when_it_refuses(tmp_path: Path, monkeypatch):
    """The fifth review's arbiter mutations, applied inside fab after its own edits: a pad's size
    swapped, a silk line's coordinates swapped, the outline's corner swapped, a second FID1 stacked on
    the first, a reference's text blown up to 4 mm. Each shipped ten Gerbers `ok=True` before. Each
    is refused naming the node now, and a refused fab leaves no BOM, no CPL, no Gerber and no board
    where a package would be. A clean fab keeps every uuid the routed board carries and adds nothing."""
    from pcbc.compile import compile_design
    from pcbc.fab import all_uuids_of, fab_job

    from test_layout_roundtrip import _mutate_in_fab

    final = routed_board("buck", EXAMPLES / "buck" / "buck.py")
    design = load_board(EXAMPLES / "buck" / "buck.py")
    job = compile_design(design)
    cases = {
        "pad_size_swap": (_in_fp("C_IN2", "(at -0.95 0)\n\t\t\t(size 1 1.45)", "(at -0.95 0)\n\t\t\t(size 1.45 1)"), r"footprint C_IN2 is on the routed board and not on the fab board as it was"),
        "silk_line_swap": (_in_fp("C_IN2", "(start -0.261252 -0.735)", "(start -0.735 -0.261252)"), r"footprint C_IN2 is on the routed board"),
        "outline_swap": (lambda t: t.replace("\t\t(start 0 0)\n\t\t(end 40 25)\n", "\t\t(start 0 0)\n\t\t(end 25 40)\n", 1), r"gr_rect .* is on the routed board and not on the fab board as it was"),
        "fid_stacked": (_stack_fid, r"footprint FID1 is on the fab board and nothing routed it|carries the uuid .* 2 times \(footprint FID1, footprint FID1\)"),
        "ref_huge": (_ref_huge, r"footprint C_IN2 is on the routed board"),
    }
    for name, (fn, says) in cases.items():
        _mutate_in_fab(monkeypatch, fn)
        out = tmp_path / name
        result = fab_job(job, final, out_dir=out, design=design)
        monkeypatch.undo()
        assert result["error"] and result["error"].startswith("the fab board is not the routed board"), (name, result["error"])
        assert re.search(says, result["error"]), (name, result["error"])
        assert not (out / "bom.csv").exists() and not (out / "cpl.csv").exists() and not (out / "gerbers").exists(), name
        assert not (out / "layout.kicad_pcb").exists() and (out / "refused.kicad_pcb").exists(), name
    clean = fab_job(job, final, out_dir=tmp_path / "clean", design=design)
    assert clean["error"] is None and clean["copper_equal"] is True and clean["gerbers"] == 10
    assert all_uuids_of((tmp_path / "clean" / "layout.kicad_pcb").read_text()) == all_uuids_of(final.read_text())
    assert [f["by"] for f in clean["fiducials"]] == ["place"] * 3
    assert clean["silk"]["moved"] == [] and clean["dates_pinned"] > 0


def test_pin_dates_rewrites_every_wall_clock_kicad_stamps(tmp_path: Path):
    """Two builds of one board differed in 20 of blinky's 60 files, every one a KiCad date (fifth
    review, direction.md section 8). `pin_dates` rewrites them in their own spelling."""
    from pcbc.fab import pin_dates

    (tmp_path / "gerbers").mkdir()
    (tmp_path / "gerbers" / "a.gtl").write_text("%TF.CreationDate,2026-09-24T15:55:02-07:00*%\nG04 Created by KiCad (PCBNEW 10.0.6) date 2026-09-24 15:55:02*\n")
    (tmp_path / "gerbers" / "job.gbrjob").write_text('{"CreationDate": "2026-09-24T15:55:02-07:00"}')
    (tmp_path / "layout-PTH.drl").write_text("; DRILL file KiCad 10.0.6 date 2026-09-24T15:55:02\n; #@! TF.CreationDate,2026-09-24T15:55:02-07:00\n")
    (tmp_path / "drc.json").write_text('{"date": "2026-09-24T15:55:02"}')
    (tmp_path / "m.pdf").write_bytes(b"/CreationDate (D:2026:09:24:15:55:02)\n")
    assert sorted(pin_dates(tmp_path)) == ["a.gtl", "drc.json", "job.gbrjob", "layout-PTH.drl", "m.pdf"]
    blob = "".join(p.read_text() for p in [*tmp_path.rglob("*")] if p.is_file())
    assert "2026" not in blob and blob.count("1980-01-01") == 6 and "D:1980:01:01:00:00:00" in blob


# ----------------------------------------------------------------------------------------- C3


def _load(tmp_path: Path, line: str):
    from pcbc.layout_job import load_core
    from pcbc.seed import emit_pcb

    board = _copy("blinky", tmp_path)
    design = load_board(board)
    (board.parent / "layout.core.py").write_text("# fifth review\n" + line + "\n")
    placed = emit_pcb(design, name="blinky")
    return load_core(design, board, placed, name="blinky")


REFUSED_AT_LOAD = [
    # yes/no given as the string "no" was bool("no") == True (BLOCKER: a mask opening shipped)
    ('Seg("LED", (9.44, 12.5), (10.1951, 13.2551), width=0.16, solder_mask="no", id="s1")', r"solder_mask is True or False, got 'no'"),
    ('Seg("LED", (9.44, 12.5), (10.1951, 13.2551), width=0.16, locked="no", id="s1")', r"locked is True or False, got 'no'"),
    ('Via("GND", (20, 5), free="no", id="v1")', r"free is True or False, got 'no'"),
    ('Line((1, 1), (4, 1), layer="F.SilkS", locked="no", id="l1")', r"locked is True or False, got 'no'"),
    ('Text("x", (2, 2), bold="no", id="t1")', r"bold is True or False, got 'no'"),
    ('Barcode("X", (5, 5), hide="no", id="b1")', r"hide is True or False, got 'no'"),
    # words inside a dict field (a kicad-cli traceback after routing before)
    ('Dimension((0, 20), (10, 20), style={"thickness": 0.15, "arrow_length": 1.27, "text_position_mode": 0, "extension_offset": 0.5, "arrow_direction": "sideways", "extension_height": 0.5}, id="d1")', r"style\['arrow_direction'\] must be one of 'inward', 'outward', got 'sideways'"),
    ('Table((0, 20), [["a"]], border={"external": True, "header": True, "width": 0.15, "type": "dashed"}, id="tb")', r"border\['type'\] must be one of 'solid'"),
    ('Dimension((0, 20), (10, 20), format={"prefix": "", "suffix": "", "units": 9, "units_format": 1, "precision": 4}, id="d1")', r"format\['units'\] must be one of 0, 1, 2, 3, got 9"),
    # layers the board does not have, anywhere a field names one
    ('Line((1, 1), (4, 1), layer="F.Silks", id="l1")', r"gr_line names 'F.Silks', which is not a layer of this board"),
    ('Text("x", (2, 2), layer="In1.Cu", id="t1")', r"gr_text names 'In1.Cu', which is not a layer of this board"),
    ('Line((1, 1), (4, 1), layer="User.46", id="l1")', r"names 'User.46', which is not a layer of this board"),
    ('Via("GND", (20, 5), remove_unused_layers=True, zone_layer_connections=("F.Cu", "Nope.Cu"), id="v1")', r"zone_layer_connections names 'Nope.Cu', which is not a copper layer"),
    ('Via("GND", (20, 5), backdrill=(0.7, ("B.Cu", "Nope")), id="v1")', r"backdrill names 'Nope', which is not a copper layer"),
    ('Pour("GND", layer="B.Cu", points=[(1, 1), (9, 1), (9, 9)], hatch_position={"F.Silk": (0.1, 0.1)}, id="p1")', r"hatch_position names 'F.Silk'"),
    ('Seg("LED", (9.44, 12.5), (10.1951, 13.2551), width=0.16, layer="In1.Cu", id="s1")', r"not a copper layer of this stackup"),
    # drawing layers= is (copper, its own mask)
    ('Line((1, 1), (4, 1), layers=("F.Cu", "B.Mask"), id="l1")', r"layers=\('F.Cu', 'B.Mask'\): a drawing's layers= is KiCad's form"),
    ('Line((1, 1), (4, 1), layers=("F.Cu", "F.Mask", "B.Mask"), id="l1")', r"KiCad's form for copper with its mask opened"),
    ('Line((1, 1), (4, 1), layer="F.Cu", layers=("F.Cu", "F.Mask"), id="l1")', r"give layer= or layers=, not both"),
    # values KiCad's save rewrites
    ('Text("x", (2, 2), justify=("left", "right"), id="t1")', r"justify names both 'left' and 'right'"),
    ('Text("x", (2, 2), justify=("left", "left"), id="t1")', r"justify names 'left' twice"),
    ('Text("x", (2, 2), hide=True, id="t1")', r"hide=True is not kept on board text"),
    ('Text("x", (2, 2), size=(0.0001, 1), id="t1")', r"a text's height and width are at least 0.001 mm"),
    ('Rect((1, 1), (5, 4), radius=2.1, id="r1")', r"KiCad's save clamps a corner radius to half the short side \(1.5\)"),
    ('Poly([(1, 1), (5, 1), (5, 1), (5, 5)], id="p1")', r"the point \(5.0, 1.0\) is written twice in a row"),
    ('Line((1, 1), (4, 1), layer="F.SilkS", stroke_color=(1, 0, 0), id="l1")', r"stroke_color is \(r, g, b, a\), four numbers"),
    (f'Image((1, 1), "{PNG[:-4]}!!!!", id="i1")', r"data is not base64"),
    ('Image((1, 1), "aGVsbG8gd29ybGQ=", id="i1")', r"data is not a PNG or JPEG file"),
    ('Dimension((0, 20), (10, 20), orientation=1, id="d1")', r"orientation is a field of orthogonal dimensions only"),
    ('Dimension((0, 20), (10, 20), kind="radial", height=3, id="d1")', r"height is a field of aligned and orthogonal dimensions only"),
    ('Dimension((0, 20), (10, 20), locked=False, gr_text={"text": "x", "at": (5, 20), "locked": True}, id="d1")', r"KiCad's save locks both when either is locked"),
    ('Dimension((0, 20), (10, 20), uuid="aaaaaaaa-1111-4111-8111-aaaaaaaaaaa1", gr_text={"text": "x", "at": (5, 20), "uuid": "aaaaaaaa-1111-4111-8111-aaaaaaaaaaa2"}, id="d1")', r"its text's uuid .* is not the dimension's own"),
    ('Barcode("X", (5, 5), kind="code39", ecc="H", id="b1")', r"a code39 code has no error-correction level"),
    ('Barcode("X", (5, 5), kind="microqr", ecc="H", id="b1")', r"KiCad's save turns a micro QR's H into Q"),
    ('Pour("GND", layer="B.Cu", points=[(1, 1), (9, 1), (9, 9)], teardrop_type="padvia", id="p1")', r"teardrop_type='padvia' marks KiCad's own teardrop zone"),
    ('Pour("GND", layer="B.Cu", points=[(1, 1), (9, 1), (9, 9)], filled_areas_thickness=True, id="p1")', r"filled_areas_thickness is a KiCad 5 field"),
    ('Pour(None, layer="B.Cu", points=[(1, 1), (9, 1), (9, 9)], keepout={"tracks": "not_allowed"}, placement={"enabled": True, "sheetname": 3}, id="k1")', r"placement\['sheetname'\] is a string, got 3"),
    ('Via("GND", (20, 5), zone_layer_connections=("F.Cu", "B.Cu"), id="v1")', r"zone_layer_connections is kept by KiCad only on a via with remove_unused_layers=True"),
    ('Via("GND", (20, 5), padstack=("custom", (("F.Cu", 0.6), ("B.Cu", 0.7))), id="v1")', r"a custom padstack has no F.Cu entry"),
    ('Via("GND", (20, 5), padstack=("front_inner_back", (("In1.Cu", 0.5), ("B.Cu", 0.7))), id="v1")', r"holds the 'Inner' and 'B.Cu' sizes"),
    ('Via("GND", (20, 5), teardrops={"bogus": 1.0}, id="v1")', r"teardrops has no field \['bogus'\]"),
    ('Via("GND", (20, 5), teardrops={"enabled": "yes"}, id="v1")', r"teardrops\['enabled'\] is True or False, got 'yes'"),
    ('Seg("LED", (9.44, 12.5), (10.1951, 13.2551), width="0.16", id="s1")', r"width is a number, got '0.16'"),
    ('Seg("GND", (1, 1), (5, 1), width=0.2, layer="B.Cu", solder_mask=True, id="s1")\nSeg("GND", (1, 2), (5, 2), width=0.2, layer="In1.Cu", solder_mask=True, id="s2")', r"(not a copper layer|only an outer layer)"),
    # strings KiCad cannot keep
    ('Text("a\\x00b", (2, 2), id="t1")', r"holds a NUL character"),
    ('Text("a\\ud800b", (2, 2), id="t1")', r"holds the lone surrogate U\+D800"),
    ('Line((1, 1), (4, 1), layer="Cmts.User", id="l1")\nGroup("g", ["l1"], lib_id="nocolon", id="g1")', r"lib_id 'nocolon' is not a KiCad library link"),
    ('Line((1, 1), (4, 1), layer="Cmts.User", id="l1")\nGroup("g", ["l1"], lib_id=\'a"b:c\', id="g1")', r"holds '\"', which KiCad refuses in a library link"),
    # groups: duplicates and transitive locks
    ('Line((1, 1), (4, 1), layer="Cmts.User", id="l1")\nGroup("g", ["l1", "l1"], id="g1")', r"member 'l1' is named twice"),
    ('Line((1, 1), (4, 1), layer="Cmts.User", id="l1")\nGroup("g", ["l1"], locked=True, id="g1")', r"'l1' is a member of the locked group 'g'"),
    ('Seg("LED", (5, 10), (9, 10), width=0.16, locked=True, id="s1")\nGroup("in", ["s1"], id="g1")\nGroup("out", ["g1"], locked=True, id="g2")', r"'g1' is a member of the locked group 'out'"),
    ('Seg("LED", (5, 10), (9, 10), width=0.16, layer="B.Cu", id="s1")\nGenerated("tuning_pattern", "t", ["s1"], layer="F.Cu", id="gen1")', r"KiCad's save moves this one from 'F.Cu' to 'B.Cu'"),
    ('Seg("LED", (5, 10), (9, 10), width=0.16, id="s1")\nGenerated("tuning_pattern", "t", ["s1"], props={"initial_side": "up"}, id="gen1")', r"props\['initial_side'\] must be one of 'default', 'left', 'right'"),
    # uuids: one object each, never the build's
    ('Line((1, 1), (4, 1), layer="Cmts.User", uuid="aaaaaaaa-1111-4111-8111-aaaaaaaaaaa1", id="l1")\nText("x", (2, 2), uuid="aaaaaaaa-1111-4111-8111-aaaaaaaaaaa1", id="t1")', r"uuid aaaaaaaa-1111-4111-8111-aaaaaaaaaaa1 is already"),
    ('Table((0, 20), [["a"]], id="tb")\nLine((1, 1), (4, 1), layer="Cmts.User", uuid="{table}", id="l1")', r"is already"),
    ('Line((1, 1), (4, 1), layer="Cmts.User", uuid="{outline}", id="l1")', r"is the board outline \(Board\(\) in board.py\)'s"),
    ('Line((1, 1), (4, 1), layer="Cmts.User", uuid="{r1}", id="l1")', r"is footprint R1 \(placed from board.py\)'s"),
    # words through `_word`, one form for every refusal
    ('Pour("GND", layer="B.Cu", points=[(1, 1), (9, 1), (9, 9)], connect="bogus", id="p1")', r"connect must be one of 'thermal', 'solid', 'none', 'thru_hole_only', got 'bogus'"),
    ('Target((5, 5), shape="o", id="t1")', r"shape must be one of 'plus', 'x', got 'o'"),
    ('Barcode("X", (5, 5), kind="ean13", id="b1")', r"kind must be one of 'qr', 'microqr', 'code39', 'code128', 'datamatrix', got 'ean13'"),
]


@pytest.mark.parametrize("line, says", REFUSED_AT_LOAD, ids=[f"r{i}" for i in range(len(REFUSED_AT_LOAD))])
def test_a_core_line_kicad_would_not_keep_is_refused_at_load_naming_the_line(tmp_path: Path, line: str, says: str):
    """C3: at least twenty core lines loaded, routed the board, and then died in a `kicad-cli`
    traceback or a refusal naming nothing (fifth review). Each is refused at load naming its line."""
    from pcbc.language import load_board as lb
    from pcbc.seed import emit_pcb
    from pcbc.sexp import stable_uuid

    if any(k in line for k in ("{table}", "{outline}", "{r1}")):
        placed = emit_pcb(lb(_copy("blinky", tmp_path / "probe")), name="blinky")
        r1 = next(t for s, e in board_footprint_spans(placed) if footprint_reference(t := placed[s:e]) == "R1")
        # the table's uuid is derived from its id; the outline's and R1's are the placed board's
        line = line.replace("{table}", stable_uuid("blinky", "layout", "tb")).replace("{outline}", stable_uuid("blinky", "edge"))
        line = line.replace("{r1}", re.search(r'\(uuid "([^"]+)"\)', r1).group(1))
    with pytest.raises(ValueError, match=r"layout\.core\.py:\d+.*" + says):
        _load(tmp_path, line)


NORMALISED = [
    ('Text("x", (2, 2), justify=("top", "left"), id="t1")', lambda gr, cu: gr[0].f["justify"] == ("left", "top")),
    ('Text("x", (2, 2), justify=("mirror", "bottom", "right"), id="t1")', lambda gr, cu: gr[0].f["justify"] == ("right", "bottom", "mirror")),
    ('Line((1, 1), (4, 1), layers=("F.Mask", "F.Cu"), id="l1")', lambda gr, cu: (gr[0].f["layer"], gr[0].f["layers"]) == (None, ("F.Cu", "F.Mask"))),
    ('Line((1, 1), (4, 1), layers=("Cmts.User",), id="l1")', lambda gr, cu: (gr[0].f["layer"], gr[0].f["layers"]) == ("Cmts.User", None)),
    ('Via("GND", (20, 5), tenting={"front": "yes"}, covering={"back": "no"}, id="v1")', lambda gr, cu: cu[0].tenting == {"front": "yes", "back": "none"} and cu[0].covering == {"front": "none", "back": "no"}),
    ('Via("GND", (20, 5), teardrops={"enabled": True, "filter_ratio": 0.123456789012345}, id="v1")', lambda gr, cu: cu[0].teardrops["filter_ratio"] == 0.123456789 and cu[0].teardrops["max_width"] == 2.0),
    ('Pour(None, layers=("F&B.Cu",), points=[(1, 1), (9, 1), (9, 9)], keepout={"tracks": "not_allowed"}, id="k1")', lambda gr, cu: cu[0].layers == ("F.Cu", "B.Cu") and cu[0].layer is None),
    ('Pour(None, layers=("*.Cu",), points=[(1, 1), (9, 1), (9, 9)], keepout={"tracks": "not_allowed"}, id="k1")', lambda gr, cu: cu[0].layers == ("F.Cu", "B.Cu")),
    ('Pour(None, layers=("B.Cu",), points=[(1, 1), (9, 1), (9, 9)], keepout={"tracks": "not_allowed"}, id="k1")', lambda gr, cu: (cu[0].layer, cu[0].layers) == ("B.Cu", None)),
    ('Pour("GND", layer="B.Cu", points=[(1, 1), (9, 1), (9, 9)], priority=0, name="", id="p1")', lambda gr, cu: cu[0].priority is None and cu[0].name is None),
    ('Pour("", layer="B.Cu", points=[(1, 1), (9, 1), (9, 9)], keepout={"tracks": "not_allowed"}, id="k1")', lambda gr, cu: cu[0].net is None),
    ('Seg("LED", (9.44000049, 12.5), (10.1951, 13.2551), width=0.1600004, id="s1")', lambda gr, cu: cu[0].width == 0.16 and cu[0].a == (9.44, 12.5)),
    ('Text("x", (2, 2), angle=12.345678901234567, id="t1")', lambda gr, cu: gr[0].f["angle"] == 12.3456789),
    ('Dimension((0, 20), (10, 20), kind="radial", style={"thickness": 0.15, "arrow_length": 1.27, "text_position_mode": 0, "extension_offset": 0.5, "keep_text_aligned": False}, id="d1")', lambda gr, cu: gr[0].f["style"]["keep_text_aligned"] is None),
    ('Dimension((0, 20), (10, 20), layer="Cmts.User", locked=True, gr_text={"text": "x", "at": (5, 20)}, id="d1")', lambda gr, cu: gr[0].f["gr_text"]["layer"] == "Cmts.User" and gr[0].f["gr_text"]["locked"] is True),
    ('Rect((1, 1), (5, 4), radius=0, id="r1")', lambda gr, cu: gr[0].f["radius"] is None),
    ('Text("(b", (2, 2), layer="F.SilkS", id="t1")', lambda gr, cu: gr[0].f["text"] == "(b"),
]


@pytest.mark.parametrize("line, check", NORMALISED, ids=[f"n{i}" for i in range(len(NORMALISED))])
def test_a_core_line_is_read_as_kicad_keeps_it(tmp_path: Path, line: str, check):
    """C3: the values KiCad's save rewrites that are one fact spelled another way are read at load as
    KiCad keeps them (measured with `kicad-cli pcb upgrade`, `scratchpad/step1b/fix6/facts.py`), so the
    line is never refused after routing for KiCad's spelling: justify in (horizontal, vertical,
    mirror) order, a drawing's layers as (copper, mask), one layer as `layer=`, a via's missing side
    as `none`, teardrops with KiCad's defaults, a pour's `*.Cu`/`F&B.Cu` spelled out, priority 0 and
    an empty name or net as unset, a length at 1 nm and a double to ten digits, `keep_text_aligned`
    False as unset, a dimension's text on its layer and lock, a zero radius as none."""
    cu, gr = _load(tmp_path, line)
    assert check(gr, cu), (cu, [g.f for g in gr])


def test_a_group_holding_a_footprint_is_a_documented_refusal():
    """A KiCad-saved group whose member is a footprint (`hw1b_fpgroup`, `n1x_fpmember` in the fifth
    review) does not decompile: a footprint is not a layout object, and `layout.gen.py` has no line
    that could name one. Pinned, with the message, and recorded in docs/layout-properties.md."""
    from pcbc.gen import assign_ids

    text = (
        "(kicad_pcb\n\t(version 20260206)\n"
        '\t(footprint "x:y"\n\t\t(layer "F.Cu")\n\t\t(uuid "aaaaaaaa-1111-4111-8111-aaaaaaaaaaa1")\n\t\t(at 1 1)\n'
        '\t\t(property "Reference" "R1"\n\t\t\t(at 0 0 0)\n\t\t\t(layer "F.SilkS")\n\t\t\t(uuid "aaaaaaaa-1111-4111-8111-aaaaaaaaaaa2")\n\t\t\t(effects (font (size 1 1)))\n\t\t)\n\t)\n'
        '\t(group "g"\n\t\t(uuid "aaaaaaaa-1111-4111-8111-aaaaaaaaaaa3")\n\t\t(members "aaaaaaaa-1111-4111-8111-aaaaaaaaaaa1")\n\t)\n'
        ")\n"
    )
    with pytest.raises(Unmapped, match=r"member aaaaaaaa-1111-4111-8111-aaaaaaaaaaa1 is not a layout object \(a footprint or a pad cannot be a member here\)"):
        board = decompile(text)
        assign_ids(board.copper, set(), board.graphics)


def test_a_footprint_s_degenerate_lines_are_dropped_on_the_board():
    """The DS2 Addon's header footprint carries the same zero-length F.CrtYd line twice, whose uuids
    swapped on every KiCad save, so the shipped board was not a fixed point of KiCad's save (fifth
    review). The copy on the board drops zero-length and repeated `fp_line`s."""
    from pcbc.seed import drop_degenerate_lines

    body = (
        '(footprint "x"\n'
        '\t(fp_line (start -6.35 0.67) (end -6.35 0.67) (stroke (width 0.05) (type solid)) (layer "F.CrtYd") (uuid "1"))\n'
        '\t(fp_line (start -6.35 0.67) (end -6.35 0.67) (stroke (width 0.05) (type solid)) (layer "F.CrtYd") (uuid "2"))\n'
        '\t(fp_line (start 0 0) (end 1 0) (stroke (width 0.05) (type solid)) (layer "F.CrtYd") (uuid "3"))\n'
        '\t(fp_line (start 0 0) (end 1 0) (stroke (width 0.05) (type solid)) (layer "F.CrtYd") (uuid "4"))\n'
        '\t(fp_line (start 0 0) (end 1 0) (stroke (width 0.05) (type solid)) (layer "F.SilkS") (uuid "5"))\n'
        ")"
    )
    out = drop_degenerate_lines(body)
    assert re.findall(r'\(uuid "(\d)"\)', out) == ["3", "5"]


# ----------------------------------------------------------------------------------------- C4


@pytest.mark.kicad
def test_a_pour_is_adopted_only_when_every_field_is_the_build_s(tmp_path: Path):
    """A core pour was adopted by signature — kind, net, layers, outline — and then shipped its own
    clearance, connect mode and thermal gap, which the build had never drawn (fifth review: buck
    E1..E4, c3_usb C1). Adoption is equality with the piece the build drew, every KiCad field; a pour
    that differs in one is a real lock, and a plane gate that fails on it names the line."""
    from pcbc.build import build_job

    final = routed_board("buck", EXAMPLES / "buck" / "buck.py")
    pour = next(ln for ln in (final.parent / "layout.gen.py").read_text().splitlines() if ln.startswith('Pour("GND"'))
    wider = re.sub(r"clearance=[0-9.]+", "clearance=0.6", pour, count=1)
    assert wider != pour
    board = _copy("buck", tmp_path / "e1")
    (board.parent / "layout.core.py").write_text(wider + "\n")
    result = build_job(board, upto="route", force=True)
    route = next(s for s in result["steps"] if s["stage"] == "route")
    pid = wider.rsplit('id="', 1)[1].split('"')[0]
    assert route["lock_passes"][0]["reproduced"] == [] and pid in route["lock_passes"][0]["missing"], route["lock_passes"]
    assert route["adopted"] == [] and route["locked"] == [pid]
    # c3_usb: the build's own outline as a minimal core pour is a lock, and the plane gate names it
    board = _copy("c3_usb", tmp_path / "c1")
    (board.parent / "layout.core.py").write_text('Pour("GND", layer="B.Cu", points=[(0.3, 0.3), (39.7, 0.3), (39.7, 29.7), (0.3, 29.7)], id="gndplane")\n')
    result = build_job(board, upto="route", force=True)
    route = next(s for s in result["steps"] if s["stage"] == "route")
    assert route["lock_passes"][0]["reproduced"] == [], route["lock_passes"]
    if result.get("error") and not str(result["error"]).startswith("unrouted:"):
        assert "layout.core.py:1 (Pour 'GND' on B.Cu)" in result["error"], result["error"]
