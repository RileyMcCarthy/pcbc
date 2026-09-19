"""pcbc search / fetch / score: parts from LCSC or anywhere, measured by what pcbc needs."""

from __future__ import annotations

import io
import json
import os
import stat
import sys
from pathlib import Path

import pytest

from pcbc.cli import main
from pcbc.seed import _absolute_models
from pcbc.source import (
    fetch_lcsc,
    guess_prefix,
    part_py_text,
    score_footprint,
    score_part,
    score_symbol,
    search_lcsc,
    slug,
)


def _pin(num: str, name: str, x: float, y: float, rot: int, *, kind: str = "passive", length: float = 2.54) -> str:
    return (
        f'(pin {kind} line (at {x} {y} {rot}) (length {length})'
        f' (name "{name}" (effects (font (size 1.27 1.27))))'
        f' (number "{num}" (effects (font (size 1.27 1.27)))))'
    )


def _symbol(pins: list[str], *, name: str = "PART", units: int = 1) -> str:
    unit_blocks = "".join(
        f'\n  (symbol "{name}_{u}_1" {" ".join(pins)})' for u in range(1, units + 1)
    )
    return (
        "(kicad_symbol_lib (version 20251024) (generator \"t\")\n"
        f' (symbol "{name}" (in_bom yes) (on_board yes)\n'
        f'  (property "Reference" "U" (at 0 5.08 0) (effects (font (size 1.27 1.27))))\n'
        f'  (property "Value" "{name}" (at 0 -5.08 0) (effects (font (size 1.27 1.27))))\n'
        f'  (property "MPN" "{name}" (at 0 0 0) (effects (font (size 1.27 1.27)) hide))\n'
        f'  (property "Manufacturer" "Acme(阿克梅)" (at 0 0 0) (effects (font (size 1.27 1.27)) hide))\n'
        f'  (property "ki_keywords" "DC-DC Converters" (at 0 0 0) (effects (font (size 1.27 1.27)) hide))\n'
        f'  (symbol "{name}_0_1" (rectangle (start -5.08 5.08) (end 5.08 -5.08) (stroke (width 0.254) (type default)) (fill (type background))))'
        f"{unit_blocks}\n ))\n"
    )


def _footprint(pads: list[str], *, legacy: bool = False, courtyard: bool = True, fab: bool = True, model: bool = True) -> str:
    pad_lines = "".join(
        f'\n  (pad "{p}" smd rect (at {i * 1.27:.2f} 0) (size 0.6 1.0) (layers "F.Cu" "F.Paste" "F.Mask"))' for i, p in enumerate(pads)
    )
    crt = '\n  (fp_rect (start -2 -2) (end 2 2) (stroke (width 0.05) (type default)) (fill no) (layer "F.CrtYd"))' if courtyard else ""
    fabl = '\n  (fp_rect (start -1.5 -1.5) (end 1.5 1.5) (stroke (width 0.1) (type default)) (fill no) (layer "F.Fab"))' if fab else ""
    silk = '\n  (fp_line (start -1 -1) (end 1 -1) (stroke (width 0.12) (type default)) (layer "F.SilkS"))'
    mdl = '\n  (model "part.step" (offset (xyz 0 0 0)) (scale (xyz 1 1 1)) (rotate (xyz 0 0 0)))' if model else ""
    head = "(module test:PART (layer F.Cu) (tedit 0)" if legacy else '(footprint "PART" (version 20260206) (generator "t") (layer "F.Cu")'
    return f"{head}\n  (attr smd){crt}{fabl}{silk}{pad_lines}{mdl}\n)\n"


GOOD_PINS = [
    _pin("1", "VIN", -7.62, 2.54, 0, kind="power_in"),
    _pin("2", "GND", -7.62, 0, 0, kind="power_in"),
    _pin("3", "EN", -7.62, -2.54, 0, kind="input"),
    _pin("4", "SW", 7.62, 0, 180, kind="output"),
]


def _write_part(tmp: Path, sym: str, mod: str, part_py: str | None = None) -> Path:
    tmp.mkdir(parents=True, exist_ok=True)
    (tmp / "PART.kicad_sym").write_text(sym)
    (tmp / "PART.kicad_mod").write_text(mod)
    if part_py is not None:
        (tmp / "part.py").write_text(part_py)
    return tmp


def test_a_clean_part_scores_good(tmp_path: Path):
    d = _write_part(tmp_path / "p", _symbol(GOOD_PINS), _footprint(["1", "2", "3", "4"]))
    r = score_part(d)
    assert r["grade"] == "good", r
    assert r["score"] == 100
    assert r["findings"] == []


def test_symbol_pins_must_be_the_footprints_pads(tmp_path: Path):
    d = _write_part(tmp_path / "p", _symbol(GOOD_PINS), _footprint(["1", "2", "3", "5", "EP", ""]))
    r = score_part(d)
    assert r["grade"] == "bad"
    msgs = [m for sev, m in r["findings"] if sev == "fail"]
    assert any("symbol pins with no pad" in m and "4" in m for m in msgs), r["findings"]
    extra = [m for sev, m in r["findings"] if "pads with no symbol pin" in m]
    assert extra and "5, EP" in extra[0] and '""' not in extra[0]  # an unnumbered pad is mechanical


def test_symbol_faults_pcbc_cannot_draw(tmp_path: Path):
    pins = [
        _pin("1", "A", -7.62, 2.54, 0),
        _pin("1", "B", -7.62, 0, 0),  # number repeats
        _pin("A1B12", "C", -7.62, -2.5, 0),  # off grid + number wider than the pin
        _pin("3", "3", 7.62, 0, 180),  # unnamed
        _pin("4", "D", 7.62, 0, 180),  # drawn on top of pin 3
        _pin("5", "E", 7.62, 1.27, 180),  # 1.27 from pin 4
    ]
    r = score_symbol(_symbol(pins))
    text = "\n".join(m for _s, m in r["findings"])
    for expected in ("pin numbers repeat: 1", "off the 1.27 mm grid: A1B12", "on top of each other: 3/4", "wider than", "closer than 2.54", "without names: 3"):
        assert expected in text, (expected, text)
    untyped = score_symbol(_symbol([_pin("1", "A", -7.62, 0, 0, kind="unspecified"), _pin("2", "B", 7.62, 0, 180, kind="unspecified")]))
    assert any("every pin is 'unspecified'" in m for _s, m in untyped["findings"]), untyped
    two = score_symbol(_symbol(GOOD_PINS, units=2))
    assert any("2 units" in m for _s, m in two["findings"]), two


def test_footprint_faults(tmp_path: Path):
    r = score_footprint(_footprint(["1", "2"], legacy=True, courtyard=False, fab=False, model=False))
    text = "\n".join(m for _s, m in r["findings"])
    assert "KiCad 5" in text and "no courtyard" in text and "no fabrication outline" in text and "no 3D model" in text
    assert r["legacy"] is True
    assert score_footprint(_footprint([]))["findings"][0][1] == "no pads"


def test_body_mm_in_part_py_is_checked_against_the_outline(tmp_path: Path):
    d = _write_part(tmp_path / "p", _symbol(GOOD_PINS), _footprint(["1", "2", "3", "4"]), "part = Component(name='PART', body_mm=(9.0, 9.0))\n")
    r = score_part(d)
    assert any("body_mm (9 x 9)" in m for _s, m in r["findings"]), r


def test_part_py_text_names_the_pins_and_guesses_the_prefix():
    from pcbc.symbol import _pins_geom, extract_main_symbol

    pins = _pins_geom(extract_main_symbol(_symbol(GOOD_PINS))[1])
    text = part_py_text(
        mpn="TPS1", manufacturer="Acme", lcsc="C1", category="DC-DC Converters", symbol="TPS1.kicad_sym",
        footprint="SOT.kicad_mod", mod_text=_footprint(["1", "2", "3", "4"]), pins=pins,
        report={"grade": "good", "score": 100},
    )
    assert "# Pins: VIN(1) GND(2) EN(3) SW(4)" in text
    assert 'prefix="U"' in text and 'lcsc="C1"' in text and "body_mm=(3.00, 3.00)" in text
    assert "kind=" not in text
    ns: dict = {}
    exec(text.replace("from pcbc import Component", "from pcbc.circuit import _library_component as Component"), ns)  # noqa: S102
    assert ns["part"].mpn == "TPS1"
    assert guess_prefix("USB Connectors") == "J"
    assert guess_prefix("MOSFETs") == "Q"
    assert guess_prefix("Power Inductors") == "L"
    assert guess_prefix("Tactile Switches") == "SW"
    assert guess_prefix("Humidity Sensors") == "U"
    assert slug("TI(德州仪器)") == "TI" and slug("B2B-XH-A(LF)(SN)") == "B2B-XH-A" and slug("韩国韩荣") == "unknown"


def test_seed_resolves_a_model_beside_the_footprint(tmp_path: Path):
    block = '(footprint "X"\n\t(model "X.step"\n\t\t(offset (xyz 0 0 0))))'
    out = _absolute_models(block, tmp_path)
    assert f'(model "{(tmp_path / "X.step").resolve()}"' in out
    keep = '(model "/abs/X.step")'
    assert _absolute_models(keep, tmp_path) == keep
    env = '(model "${KICAD9_3DMODEL_DIR}/x.step")'
    assert _absolute_models(env, tmp_path) == env


def test_search_reads_the_jlcsearch_rows():
    payload = json.dumps({"components": [{"lcsc": 191884, "mfr": "TPS54202DDCR", "package": "SOT-23-6", "stock": 5, "price": 0.22, "is_basic": False}]}).encode()

    class Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    hits = search_lcsc("tps54202", opener=lambda req, timeout: Resp(payload))
    assert hits == [
        {"lcsc": "C191884", "mpn": "TPS54202DDCR", "package": "SOT-23-6", "description": "", "stock": 5, "price_usd": 0.22, "basic": False, "preferred": False}
    ]


def _fake_easyeda2kicad(tmp_path: Path) -> Path:
    """A stand-in that writes easyeda2kicad's layout: e.kicad_sym, e.pretty/, e.3dshapes/."""
    script = tmp_path / "easyeda2kicad"
    sym = _symbol(GOOD_PINS, name="TPS1").replace("(version 20251024)", "(version 20211014)")
    mod = _footprint(["1", "2", "3", "4"], legacy=True, model=True).replace('(model "part.step"', '(model "/somewhere/e.3dshapes/SOT-23-4.wrl"')
    script.write_text(
        "#!/usr/bin/env python3\nimport sys, pathlib\n"
        "out = pathlib.Path(sys.argv[sys.argv.index('--output') + 1])\n"
        f"out.write_text({sym!r})\n"
        "pretty = out.with_name(out.stem + '.pretty'); pretty.mkdir()\n"
        f"(pretty / 'SOT-23-4.kicad_mod').write_text({mod!r})\n"
        "shapes = out.with_name(out.stem + '.3dshapes'); shapes.mkdir()\n"
        "(shapes / 'SOT-23-4.step').write_text('ISO-10303-21;')\n"
        "(shapes / 'SOT-23-4.wrl').write_text('#VRML')\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return script


def test_fetch_splits_upgrades_and_writes_part_py(tmp_path: Path):
    fake = _fake_easyeda2kicad(tmp_path)
    into = tmp_path / "components"
    r = fetch_lcsc("191884", into, easyeda2kicad=str(fake))
    d = Path(r["dir"])
    assert d == into / "Acme" / "TPS1"
    assert r["lcsc"] == "C191884" and r["wrote_part_py"]
    names = sorted(p.name for p in d.iterdir())
    assert names == ["SOT-23-4.kicad_mod", "SOT-23-4.step", "TPS1.kicad_sym", "part.py"]  # STEP only, no .wrl twin
    mod = (d / "SOT-23-4.kicad_mod").read_text()
    assert '(model "SOT-23-4.step"' in mod  # relinked beside the footprint
    from pcbc.source import _kicad_cli

    if _kicad_cli() is not None:
        assert mod.startswith("(footprint") and "(uuid" not in mod  # upgraded, deterministic
        assert "(version 2025" in (d / "TPS1.kicad_sym").read_text() or "(version 2026" in (d / "TPS1.kicad_sym").read_text()
        assert r["score"]["grade"] == "good", r["score"]
    else:
        assert any("kicad-cli not found" in m for _s, m in r["score"]["findings"])
    part_py = (d / "part.py").read_text()
    assert 'lcsc="C191884"' in part_py and 'manufacturer="Acme"' in part_py and "# Pins: VIN(1) GND(2) EN(3) SW(4)" in part_py
    # A hand-edited part.py is kept unless --force.
    (d / "part.py").write_text("# mine\n" + part_py)
    r2 = fetch_lcsc("C191884", into, easyeda2kicad=str(fake))
    assert not r2["wrote_part_py"] and (d / "part.py").read_text().startswith("# mine")
    r3 = fetch_lcsc("C191884", into, easyeda2kicad=str(fake), force=True)
    assert r3["wrote_part_py"] and not (d / "part.py").read_text().startswith("# mine")
    # And the part loads.
    from pcbc.circuit import load_part

    part = load_part(d)
    assert set(part.pins) == {"VIN", "GND", "EN", "SW"} and part.lcsc == "C191884"


def test_cli_score_and_fetch(tmp_path: Path, capsys):
    d = _write_part(tmp_path / "good", _symbol(GOOD_PINS), _footprint(["1", "2", "3", "4"]))
    assert main(["score", str(d)]) == 0
    assert "good (100/100)" in capsys.readouterr().out
    bad = _write_part(tmp_path / "bad", _symbol(GOOD_PINS), _footprint(["1", "2"]))
    assert main(["score", str(bad), "--json"]) == 1
    rows = json.loads(capsys.readouterr().out)
    assert rows[0]["grade"] == "bad"
    fake = _fake_easyeda2kicad(tmp_path)
    old = os.environ.get("PATH", "")
    os.environ["PATH"] = f"{tmp_path}{os.pathsep}{old}"
    try:
        assert main(["fetch", "C191884", "--into", str(tmp_path / "c")]) == 0
    finally:
        os.environ["PATH"] = old
    out = capsys.readouterr().out
    assert "C191884 -> " in out and "wrote part.py" in out and "# Pins" not in out


def test_the_example_parts_report_their_known_faults():
    root = Path(__file__).resolve().parent.parent / "examples"
    usb = score_part(root / "c3_usb" / "components" / "HRO" / "TYPE-C-31-M-12")
    assert usb["grade"] == "bad"
    assert any("A1B12" in m and "no pad" in m for _s, m in usb["findings"]), usb
    ldo = score_part(root / "c3_usb" / "components" / "Diodes_Inc" / "AP2112K-3.3TRG1")
    assert ldo["grade"] != "bad"
