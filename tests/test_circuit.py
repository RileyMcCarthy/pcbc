from pathlib import Path

from pcbc.circuit import check_design, load_part
from pcbc.language import check_board, load_board, reset
from pcbc.symbol import parse_symbol_pins

ROOT = Path(__file__).resolve().parent
FIX = ROOT / "fixtures"
BLINKY = ROOT.parent / "examples" / "blinky" / "blinky.py"


def test_symbol_pins_group_and_optional():
    pins = parse_symbol_pins(FIX / "ldo" / "AP2112K.kicad_sym")
    assert set(pins) == {"VIN", "VOUT", "GND", "EN", "NC"}
    assert pins["VIN"].pads == ("1",)
    assert pins["NC"].optional
    assert not pins["EN"].optional


def test_load_part_reads_symbol_not_python_pins():
    part = load_part(FIX / "ldo")
    assert part.lcsc == "C51118"
    assert "EN" in part.pins
    assert "pins" not in Path(FIX / "ldo" / "part.py").read_text()


def test_blinky_check_ok():
    assert check_board(BLINKY) == []
    d = load_board(BLINKY)
    assert {i.ref for i in d.instances} == {"R1", "D1"}
    assert d.board is not None
    assert d.board.size_mm == (40.0, 25.0)


def test_unbound_required_pin(tmp_path: Path):
    board = tmp_path / "board.py"
    ldo = FIX / "ldo"
    board.write_text(
        f"""
from pcbc import Board, Ground, Power, load
U = load({str(ldo)!r})
VCC = Power("VCC")
GND = Ground("GND")
U("U1", VIN=VCC, VOUT=VCC, GND=GND)
Board(width=20, height=10, layers=2, stackup="jlcpcb_2l_1oz")
"""
    )
    fails = check_board(board)
    assert any("U1.EN unbound" in f for f in fails)


def test_unknown_pin_name(tmp_path: Path):
    board = tmp_path / "board.py"
    ldo = FIX / "ldo"
    board.write_text(
        f"""
from pcbc import Board, Ground, Power, load
U = load({str(ldo)!r})
VCC = Power("VCC")
GND = Ground("GND")
U("U1", VIN=VCC, VOUT=VCC, GND=GND, EN=VCC, FOO=VCC)
Board(width=20, height=10, layers=2, stackup="jlcpcb_2l_1oz")
"""
    )
    fails = check_board(board)
    assert any("U1.FOO" in f for f in fails)


def test_nc_may_float(tmp_path: Path):
    board = tmp_path / "board.py"
    ldo = FIX / "ldo"
    board.write_text(
        f"""
from pcbc import Board, Ground, Place, Power, load
U = load({str(ldo)!r})
VCC = Power("VCC")
GND = Ground("GND")
U("U1", VIN=VCC, VOUT=VCC, GND=GND, EN=VCC)
Board(width=20, height=10, layers=2, stackup="jlcpcb_2l_1oz")
Place("U1", at=(5, 5))
"""
    )
    assert check_board(board) == []


def test_missing_board():
    reset()
    from pcbc.language import Ground, Power, Resistor
    from pcbc.circuit import check_design
    from pcbc.language import _doc

    VCC = Power("VCC")
    GND = Ground("GND")
    Resistor("R1", "1k", package="0402", mpn="X", lcsc="C1", p1=VCC, p2=GND)
    fails = check_design(_doc())
    assert any("no Board()" in f for f in fails)


def test_missing_place(tmp_path: Path):
    board = tmp_path / "board.py"
    board.write_text(
        """
from pcbc import Board, Ground, Power, Resistor
VCC = Power("VCC")
GND = Ground("GND")
Resistor("R1", "1k", package="0402", mpn="X", lcsc="C1", p1=VCC, p2=GND)
Board(width=10, height=10, layers=2, stackup="jlcpcb_2l_1oz")
"""
    )
    fails = check_board(board)
    assert any("R1: no Place()" in f for f in fails)


def test_missing_lcsc(tmp_path: Path):
    board = tmp_path / "board.py"
    board.write_text(
        """
from pcbc import Board, Ground, Place, Power, Resistor
VCC = Power("VCC")
GND = Ground("GND")
Resistor("R1", "1k", package="0402", mpn="X", p1=VCC, p2=GND)
Board(width=10, height=10, layers=2, stackup="jlcpcb_2l_1oz")
Place("R1", at=(5, 5))
"""
    )
    fails = check_board(board)
    assert any("R1 missing lcsc" in f for f in fails)


def test_usb_c_both_orientations(tmp_path: Path):
    usbc = FIX / "usbc"
    board = tmp_path / "board.py"
    board.write_text(
        f"""
from pcbc import Board, Ground, Net, Power, Resistor, load
J = load({str(usbc)!r})
VBUS = Power("VBUS")
GND = Ground("GND")
DP = Net("USB_DP")
DN = Net("USB_DN")
CC1 = Net("CC1")
CC2 = Net("CC2")
J("J1", VBUS=VBUS, GND=GND, DP1=DP, DP2=Net("OTHER"), DN1=DN, DN2=DN, CC1=CC1, CC2=CC2)
Resistor("R1", "5.1k", package="0402", mpn="R", lcsc="C1", p1=CC1, p2=GND)
Resistor("R2", "5.1k", package="0402", mpn="R", lcsc="C1", p1=CC2, p2=GND)
Board(width=20, height=10, layers=2, stackup="jlcpcb_2l_1oz")
"""
    )
    fails = check_board(board)
    assert any("DP1" in f and "DP2" in f for f in fails)


def test_usb_c_rd_required(tmp_path: Path):
    usbc = FIX / "usbc"
    board = tmp_path / "board.py"
    board.write_text(
        f"""
from pcbc import Board, Ground, Net, Power, load
J = load({str(usbc)!r})
VBUS = Power("VBUS")
GND = Ground("GND")
DP = Net("USB_DP")
DN = Net("USB_DN")
CC1 = Net("CC1")
CC2 = Net("CC2")
J("J1", VBUS=VBUS, GND=GND, DP1=DP, DP2=DP, DN1=DN, DN2=DN, CC1=CC1, CC2=CC2)
Board(width=20, height=10, layers=2, stackup="jlcpcb_2l_1oz")
"""
    )
    fails = check_board(board)
    assert any("5.1" in f for f in fails)


def test_usb_c_ok(tmp_path: Path):
    usbc = FIX / "usbc"
    board = tmp_path / "board.py"
    board.write_text(
        f"""
from pcbc import Board, Ground, Net, Place, Power, Resistor, load
J = load({str(usbc)!r})
VBUS = Power("VBUS")
GND = Ground("GND")
DP = Net("USB_DP")
DN = Net("USB_DN")
CC1 = Net("CC1")
CC2 = Net("CC2")
J("J1", VBUS=VBUS, GND=GND, DP1=DP, DP2=DP, DN1=DN, DN2=DN, CC1=CC1, CC2=CC2)
Resistor("R1", "5.1k", package="0402", mpn="R", lcsc="C1", p1=CC1, p2=GND)
Resistor("R2", "5.1k", package="0402", mpn="R", lcsc="C1", p1=CC2, p2=GND)
Board(width=20, height=10, layers=2, stackup="jlcpcb_2l_1oz")
Place("J1", at=(10, 8))
Place("R1", at=(4, 4))
Place("R2", at=(16, 4))
"""
    )
    assert check_board(board) == []
