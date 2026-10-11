"""`Path()` replaced `Chain()` (`docs/net-path-physics.md` §1.3, owner 2026-09-30: *"Call it path"*).

`Chain()` builds nothing and is not an alias: a board that calls it is refused at load, and the refusal
prints every `Chain` line of the file rewritten exactly, the import included, so one pass fixes it.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from conftest import DS2_REAL, ds2_part_a, write_split
from pcbc.language import chain_rewrite, check_board, load_board

ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = ROOT / "examples"


def _c3_with_chain(tmp_path: Path) -> Path:
    """c3_usb as it was before the rename: its multi-line import names `Chain` and two lines call it."""
    work = tmp_path / "c3"
    shutil.copytree(EXAMPLES / "c3_usb", work, ignore=shutil.ignore_patterns("layout"))
    board = work / "c3_usb.py"
    text = board.read_text()
    assert "    Path,\n" in text and 'Path("USB_DP", "J1.A6", "U3.1", "U1.27")' in text
    text = text.replace("    Path,\n", "    Chain,\n", 1).replace('Path("USB_D', 'Chain("USB_D')
    write_split(board, text)
    return board


def test_a_chain_board_is_refused_naming_every_chain_line(tmp_path: Path):
    """docs/direction.md §5b, §7b: a `Chain()` line is an error stating that `Chain()` is now `Path()`,
    every line of the board named, and no rewritten line printed."""
    board = _c3_with_chain(tmp_path)
    got = check_board(board)
    assert got == [
        "c3_usb.py line 214: Chain() is now Path(), with the same arguments\n"
        "c3_usb.py line 215: Chain() is now Path(), with the same arguments\n"
        "c3_usb.py line 9: the import names Chain, which pcbc no longer exports"
    ], got


def test_applying_the_refusal_gives_back_the_shipped_board(tmp_path: Path):
    """`chain_rewrite`, the library function the suite applies to temp copies (the refusal prints no
    rewritten line): applied to the pre-rename c3_usb it gives the shipped file byte for byte."""
    board = _c3_with_chain(tmp_path)
    new, _msgs = chain_rewrite(board.read_text(), board.name)
    assert new == (EXAMPLES / "c3_usb" / "c3_usb.py").read_text()


def test_an_import_that_already_names_path_drops_chain():
    text = 'from pcbc import Board, Chain, Path, NetReq\nChain("A", "R1.1", "R2.1")  # keep me\n'
    new, msgs = chain_rewrite(text, "b.py")
    assert new == 'from pcbc import Board, Path, NetReq\nPath("A", "R1.1", "R2.1")  # keep me\n'
    assert msgs == [
        "b.py line 2: Chain() is now Path(), with the same arguments",
        "b.py line 1: the import names Chain, which pcbc no longer exports",
    ]


def test_the_refusal_is_pinned_against_the_owners_file_read_only():
    """The DS2 Addon is the owner's file and is never edited here. Until he makes the two-line edit, the
    refusal names exactly these two lines; the suite reads his board through `conftest.ds2_dir`, which
    applies this very rewrite to a temp copy."""
    real = DS2_REAL / "ds2_addon.py"
    if not real.exists():
        pytest.skip("the DS2 Addon is not checked out here")
    text = real.read_text()
    if "Chain(" not in text:
        pytest.skip("the owner has made the edit: nothing to refuse")
    _new, msgs = chain_rewrite(text, real.name)
    assert msgs == [
        "ds2_addon.py line 97: Chain() is now Path(), with the same arguments",
        "ds2_addon.py line 15: the import names Chain, which pcbc no longer exports",
    ], msgs
    assert real.read_text() == text, "read only"


def test_a_one_argument_path_names_the_pathlib_clash(tmp_path: Path):
    board = tmp_path / "b.py"
    write_split(board, 'from pcbc import *\n\nPath("layout/out.txt")\n')
    assert check_board(board) == [
        'line 3: Path("layout/out.txt"): a Path needs a net and two or more stations; `from pcbc import *` binds Path to pcbc\'s, '
        "so a call meant for pathlib.Path reaches this one"
    ]


def test_a_field_less_path_is_what_a_chain_was():
    design = load_board(EXAMPLES / "node" / "node.py")
    assert [(p.net, p.pads, p.fields()) for p in design.paths if not p.fields()] == [
        ("USB_DP", ("J1.B6", "U3.6", "U1.27"), ()),
        ("USB_DN", ("J1.A7", "U3.4", "U1.26"), ()),
    ]


def test_the_owners_file_gets_each_subject_error_pinned_read_only(tmp_path: Path):
    """docs/physics-subjects.md §8: the DS2 Addon's line 95 writes a net current. It is refused with E1, the
    facts only, against a byte-identical copy of the owner's file (his file is never written)."""
    import shutil

    real = DS2_REAL / "ds2_addon.py"
    if not real.exists():
        pytest.skip("the DS2 Addon is not checked out here")
    text = real.read_text()
    if 'volts=3.3, amps=0.1)' not in text:
        pytest.skip("the owner has made the edit: nothing to refuse")
    shutil.copytree(DS2_REAL, tmp_path / "ds2")
    copy = tmp_path / "ds2" / "ds2_addon.py"
    assert copy.read_text() == text
    if "from pcbc import Board, Capacitor, Chain, Ground, Net, NetReq, Place," in text:
        # docs/board-outline.md A.3/A.6 (2026-10-03): `Place` left pcbc's exports, so his import line is the first
        # refusal, before line 95 runs. With the Part-A edit (A.6) applied to the copy, line 95's refusal stands.
        assert check_board(copy) == [
            "ds2_addon.py:15: pcbc exports no Place: Place is layout.core.py's (next to ds2_addon.py), and ds2_addon.py "
            "holds the circuit and its physics only"
        ]
        copy.write_text(ds2_part_a(text, copy.parent))
    assert check_board(copy) == [
        'NetReq("3V3") line 95: amps= is a current; a current flows in the copper between two pads, and a NetReq states what holds at every pad of '
        "3V3 (U1.DVDD, J1.1, C5.1, C6.1, R10.1), GND (U1.DGND, J1.2, J3.3, C5.2, C6.2), VDDA (U1.AVDD, J2.1, C4.1) and VSS (U1.AVSS, J2.2, C4.2, "
        "C7.2, C9.2, C10.2, C12.2, C1.2, C3.2); amps= is a Path field"
    ]
    assert real.read_text() == text, "read only"


def test_the_owners_file_without_its_current_gets_e3_for_each_ground(tmp_path: Path):
    """PR 4 (E3), against the owner's line 95 with only its `amps=0.1` taken out (so the load reaches the
    compiler): volts=3.3 is refused once for GND and once for VSS, each declared Ground() in his file."""
    import shutil

    real = DS2_REAL / "ds2_addon.py"
    if not real.exists():
        pytest.skip("the DS2 Addon is not checked out here")
    text = real.read_text()
    line = 'NetReq("3V3", "GND", "VDDA", "VSS", kind="power", volts=3.3, amps=0.1)'
    if line not in text:
        pytest.skip("the owner has made the edit: nothing to refuse")
    shutil.copytree(DS2_REAL, tmp_path / "ds2")
    copy = tmp_path / "ds2" / "ds2_addon.py"
    write_split(copy, chain_rewrite(text.replace(line, line.replace(", amps=0.1", "")), copy.name)[0])
    assert check_board(copy) == [
        'NetReq("3V3", "GND", "VDDA", "VSS") line 95: volts=3.3 holds for every net the line names, and GND is Ground("GND") (ds2_addon.py line 19), '
        "the 0 V its domain's voltages are measured from",
        'NetReq("3V3", "GND", "VDDA", "VSS") line 95: volts=3.3 holds for every net the line names, and VSS is Ground("VSS") (ds2_addon.py line 22), '
        "the 0 V its domain's voltages are measured from",
    ]
    assert real.read_text() == text, "read only"
