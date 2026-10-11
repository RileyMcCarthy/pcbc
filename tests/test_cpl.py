"""The placement file (CPL) in JLC's own footprint's frame (docs/direction.md §4c, `cpl.py`): each part registered against
JLC's footprint for its LCSC number, pad for pad; a part with no such footprint on disk is a fact, which fails fab when
its orientation matters."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from pcbc.cpl import cpl_csv, jlc_library, library_roots, plan, register, repo_components
from pcbc.pads import PadSpec, pad_specs

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"
KICAD_SOT23_5 = EXAMPLES / "node" / "components" / "Diodes_Inc" / "AP2112K-3.3TRG1" / "SOT-23-5.kicad_mod"  # KiCad's library footprint
KICAD_SOT23_6 = EXAMPLES / "node" / "components" / "ST" / "USBLC6-2SC6" / "SOT-23-6.kicad_mod"  # KiCad's library footprint
JLC_LMV321 = EXAMPLES / "node" / "components" / "TI" / "LMV321IDBVR" / "SOT-23-5_L3.0-W1.7-P0.95-LS2.8-BR.kicad_mod"  # JLC's, C7972
JLC_TPS54202 = EXAMPLES / "buck" / "components" / "TI" / "TPS54202DDCR" / "SOT-23-6_L2.9-W1.6-P0.95-LS2.8-BL.kicad_mod"  # JLC's, C191884
JLC_XH = EXAMPLES / "buck" / "components" / "JST" / "B2B-XH-A" / "CONN-TH_B2B-XH-A-LF-SN.kicad_mod"  # JLC's, C158012


def _specs(path: Path):
    return list(pad_specs(path.read_text()))


def test_kicads_sot23_5_registers_on_jlcs_180_degrees_round():
    """The on-disk pair: KiCad's SOT-23-5 has pad 1 top-left, JLC's (the LMV321's, C7972) bottom-right."""
    got = register(_specs(KICAD_SOT23_5), _specs(JLC_LMV321))
    assert got["theta"] == 180 and got["landed"] == [180] and got["why"] is None, got
    assert abs(got["t"][0]) < 0.05 and abs(got["t"][1]) < 0.05
    # At every other quarter turn some pad lands a millimetre or more off its pad.
    assert all(d >= 1.0 for th, (_n, d) in got["worst"].items() if th != 180), got["worst"]


def test_kicads_sot23_6_registers_on_jlcs_270_degrees_round():
    """KiCad's SOT-23-6 has its pins in columns; JLC's (the TPS54202's, C191884) in rows."""
    got = register(_specs(KICAD_SOT23_6), _specs(JLC_TPS54202))
    assert got["theta"] == 270 and got["landed"] == [270], got


def test_a_footprint_registers_on_itself_unturned():
    got = register(_specs(JLC_LMV321), _specs(JLC_LMV321))
    assert got["theta"] == 0 and got["t"] == (0.0, 0.0)


def test_pads_that_land_nowhere_or_differ_are_a_fact():
    got = register(_specs(KICAD_SOT23_5), _specs(JLC_TPS54202))
    assert got["theta"] is None and got["why"] == "its pads are not JLC's: pad 6 only on JLC's"


def _xh_kicad_frame():
    """JLC's XH with its origin moved to pad 1, as KiCad's own XH footprint draws it."""
    return [PadSpec(sp.num, sp.kind, sp.shape, sp.x - 1.25, sp.y, sp.angle, sp.w, sp.h, layers=sp.layers) for sp in _specs(JLC_XH)]


def test_the_centre_is_jlcs_footprint_origin_turned_with_the_part():
    got = register(_xh_kicad_frame(), _specs(JLC_XH))
    assert got["theta"] == 0 and got["t"] == (-1.25, 0.0)
    (row,) = plan([_part("J1", "C158012", (10.0, 20.0, 90.0), _xh_kicad_frame(), need="required")], jlc_library([JLC_XH]))
    assert row["fact"] is None and row["theta"] == 0
    # (-1.25, 0) in the footprint's frame, turned 90 degrees as KiCad turns a footprint: (0, +1.25) on the board.
    assert row["jlc"] == (10.0, 21.25, 90.0), row


def _part(ref, lcsc, at, specs, *, need="required", own=None, pins=None, shape="dot"):
    return {"ref": ref, "lcsc": lcsc, "at": at, "side": "Top", "specs": specs, "own": own, "pins": pins or {}, "need": need, "shape": shape}


def test_jlcs_rotation_is_the_parts_kicad_rotation_plus_the_registered_turn():
    """An LMV321 (C7972) on KiCad's SOT-23-5 at 90 degrees: JLC places it at 270."""
    (row,) = plan([_part("U5", "C7972", (30.0, 15.0, 90.0), _specs(KICAD_SOT23_5))], jlc_library([JLC_LMV321]))
    assert row["theta"] == 180 and row["jlc"][2] == 270.0 and row["fact"] is None and not row["fails"], row
    assert "JLC's footprint SOT-23-5_L3.0-W1.7-P0.95-LS2.8-BR.kicad_mod for C7972 lands pad for pad turned 180 degrees" in row["evidence"]
    csv = cpl_csv([row], {})
    assert csv.splitlines()[1] == f"U5,{row['jlc'][0]:.6f},{-row['jlc'][1]:.6f},270.000000,Top"


def test_a_board_footprint_that_is_jlcs_own_is_written_as_it_is():
    (row,) = plan([_part("U5", "C7972", (30.0, 15.0, 90.0), _specs(JLC_LMV321), own="C7972")], {})
    assert row["jlc"] == (30.0, 15.0, 90.0) and row["evidence"] == "the board footprint is JLC's own for C7972 (its LCSC Part)"


def test_no_jlc_footprint_on_disk_fails_a_part_whose_orientation_matters_and_notes_one_that_is_its_own_turned_self():
    rows = plan(
        [
            _part("U2", "C51118", (5.0, 5.0, 0.0), _specs(KICAD_SOT23_5), need="optional"),
            _part("R1", "C21190", (8.0, 5.0, 0.0), _specs(KICAD_SOT23_5)[:2], need="none"),
        ],
        jlc_library([JLC_LMV321]),
    )
    r1, u2 = rows
    assert u2["fails"] and u2["jlc"] is None
    assert u2["fact"] == "U2 (C51118): CPL rotation not registered: no JLC footprint for C51118 on disk (a .kicad_mod whose LCSC Part is C51118); the board footprint is not JLC's (it records no LCSC Part)"
    assert not r1["fails"] and r1["jlc"] == (8.0, 5.0, 0.0)
    assert r1["fact"].endswith("KiCad's rotation is written: turned 180 degrees the part is itself, and turned 90 no terminal lands on a pad (§4c G1)")


def _led(tmp_path: Path, jlc_cathode_pad: str) -> Path:
    """A JLC LED footprint and symbol for 'C99999' (test data, not a real part): pads at -0.8 and +0.8, the symbol's
    cathode on `jlc_cathode_pad`."""
    d = tmp_path / "jlc"
    d.mkdir()
    pads = "".join(f'\t(pad "{n}" smd rect\n\t\t(at {x} 0)\n\t\t(size 0.8 0.8)\n\t\t(layers "F.Cu" "F.Paste" "F.Mask")\n\t)\n' for n, x in (("1", -0.8), ("2", 0.8)))
    (d / "LED0603.kicad_mod").write_text(f'(footprint "LED0603"\n\t(layer "F.Cu")\n\t(property "LCSC Part" "C99999")\n{pads})\n')
    other = "2" if jlc_cathode_pad == "1" else "1"
    pins = f'(pin passive line (at 0 0 0) (length 1) (name "K") (number "{jlc_cathode_pad}"))\n(pin passive line (at 2 0 180) (length 1) (name "A") (number "{other}"))'
    (d / "LED.kicad_sym").write_text(f'(kicad_symbol_lib\n(symbol "LED"\n(property "LCSC Part" "C99999")\n(symbol "LED_1_1"\n{pins}\n)))\n')
    return d


KICAD_LED = [PadSpec("1", "smd", "roundrect", -0.7875, 0.0, 0.0, 0.875, 0.95, layers='"F.Cu" "F.Paste" "F.Mask"'), PadSpec("2", "smd", "roundrect", 0.7875, 0.0, 0.0, 0.875, 0.95, layers='"F.Cu" "F.Paste" "F.Mask"')]


@pytest.mark.parametrize("jlc_k, theta", [("1", 0), ("2", 180)])
def test_an_leds_cathode_pairs_with_the_pad_jlcs_symbol_calls_the_cathode(tmp_path: Path, jlc_k: str, theta: int):
    """A diode's pad numbering is its library's choice: KiCad's LED_0603 has the cathode on pad 1, a JLC footprint may
    have it on pad 2, and pairing by number would place the LED backwards."""
    lib = jlc_library([_led(tmp_path, jlc_k)])
    (row,) = plan([_part("D1", "C99999", (10.0, 10.0, 0.0), KICAD_LED, pins={"K": ("1",), "A": ("2",)}, shape="bar")], lib)
    assert row["fact"] is None and row["theta"] == theta, row
    assert f"(its K, pad 1, on JLC's pad {jlc_k})" in row["evidence"]


def test_an_led_whose_jlc_symbol_names_no_cathode_is_a_fact(tmp_path: Path):
    d = _led(tmp_path, "1")
    (d / "LED.kicad_sym").write_text((d / "LED.kicad_sym").read_text().replace('(name "K")', '(name "X")'))
    (row,) = plan([_part("D1", "C99999", (10.0, 10.0, 0.0), KICAD_LED, pins={"K": ("1",), "A": ("2",)}, shape="bar")], jlc_library([d]))
    assert row["fails"] and row["fact"] == "D1 (C99999): CPL rotation not registered: D1's K is pad 1, and which of the pads of JLC's footprint LED0603.kicad_mod is the cathode is not known: JLC's symbol LED.kicad_sym names no cathode pin"


def test_the_jlc_library_is_every_file_that_records_its_lcsc_number():
    lib = jlc_library([EXAMPLES / "node" / "components", EXAMPLES / "buck" / "components"])
    assert {"C7972", "C191884", "C158012", "C20917", "C92489", "C165948", "C177243"} <= set(lib)
    # The AP2112K's and the USBLC6's symbols are JLC's (they record their LCSC numbers); their footprints are KiCad's.
    assert lib["C51118"]["mod"] == [] and lib["C51118"]["sym"] and not (lib.get("C2687116") or {}).get("mod")
    assert [p.name for p in lib["C7972"]["mod"]] == [JLC_LMV321.name]
    assert re.search(r"LMV321IDBVR\.kicad_sym$", str(lib["C7972"]["sym"][0]))


def test_the_notes_state_one_fact_once_for_the_parts_it_is_about():
    from pcbc.cpl import note_lines

    two = [_part(r, "C25744", (5.0 + i, 5.0, 0.0), _specs(KICAD_SOT23_5)[:2], need="none") for i, r in enumerate(("R_BOOT", "R_EN"))]
    lines = note_lines(plan(two + [_part("U2", "C51118", (9.0, 5.0, 0.0), _specs(KICAD_SOT23_5), need="optional")], {}))
    assert lines[0].startswith("U2 (C51118): CPL rotation not registered")
    assert lines[1].startswith("R_BOOT, R_EN (C25744): CPL rotation not registered: no JLC footprint for C25744 on disk") and len(lines) == 2, lines


def test_a_part_fetched_into_the_checkout_is_found_beside_a_board_with_its_own_components(tmp_path: Path):
    """A copied board brings its module's `components/` and not the LED. The checkout's `components/` is still read."""
    local = tmp_path / "components"
    local.mkdir()
    roots = library_roots(local)
    assert roots == [local, repo_components()]
    lib = jlc_library(roots)
    assert lib["C2286"]["mod"] and lib["C72043"]["mod"] and lib["C2838502"]["mod"]


def test_kicads_led_registers_on_the_fetched_0603_leds():
    """C2286 (Kento) numbers its cathode 2; C72043 (Everlight) numbers it 1 and names the pin C. Both land on KiCad's
    LED_0603, cathode on cathode, at the same rotation."""
    lib = jlc_library(library_roots())
    for lc, jlc_pad in (("C2286", "2"), ("C72043", "1")):
        (row,) = plan([_part("D1", lc, (0.0, 0.0, 0.0), KICAD_LED, pins={"K": ("1",), "A": ("2",)}, shape="bar")], lib)
        assert row["fact"] is None and row["theta"] == 0 and not row["fails"], row
        assert f"on JLC's pad {jlc_pad}" in row["evidence"]


def test_the_esp32_c3_mini_registers_on_jlcs_footprint_without_a_turn():
    """The board's ESP32-C3-MINI footprint (KiCad's C6 land, no LCSC property) and JLC's for C2838502 are the same pads."""
    board = EXAMPLES / "c3_usb" / "components" / "Espressif" / "ESP32-C3-MINI-1-N4" / "ESP32-C6-MINI-1.kicad_mod"
    (row,) = plan([_part("U1", "C2838502", (1.0, 2.0, 90.0), _specs(board), shape="triangle")], jlc_library(library_roots()))
    assert row["fact"] is None and row["theta"] == 0 and row["jlc"] == (1.0, 2.0, 90.0), row


def test_pads_that_land_at_two_turns_do_not_tell_jlcs_zero_apart():
    """A shield or a pad pair that shares one number lands turned 0 and 180 degrees alike: that is a fact, not a guess."""
    same = [PadSpec("1", "smd", "rect", x, 0.0, 0.0, 0.8, 0.8, layers='"F.Cu" "F.Paste" "F.Mask"') for x in (-0.8, 0.8)]
    got = register(same, same)
    assert got["theta"] is None and got["landed"] == [0, 180]
    assert got["why"] == "JLC's pads land on the board footprint's at 0 and 180 degrees alike, so its zero is not told apart"
