from pathlib import Path

from pcbc.cli import main

BLINKY = Path(__file__).resolve().parent.parent / "examples" / "blinky" / "blinky.py"


def test_cli_check_blinky(capsys):
    assert main(["check", str(BLINKY)]) == 0
    out = capsys.readouterr().out
    assert '"ok": true' in out


def test_cli_check_missing(tmp_path: Path):
    assert main(["check", str(tmp_path / "nope.py")]) == 2
