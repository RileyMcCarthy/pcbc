"""Silkscreen references: placed by the tool, reported as moves, never printed over a neighbour."""

from __future__ import annotations

from pathlib import Path

from pcbc.silk import legalize_silk


def _fp(ref: str, x: float, y: float, w: float, h: float, pads: list[tuple[float, float, float, float]] | None = None) -> str:
    """A footprint block: courtyard w x h centred on (x, y), optional pads (px, py, pw, ph)."""
    pad_text = "".join(
        f'\t\t(pad "{i + 1}" smd rect\n\t\t\t(at {px} {py})\n\t\t\t(size {pw} {ph})\n\t\t\t(layers "F.Cu")\n\t\t)\n'
        for i, (px, py, pw, ph) in enumerate(pads or [])
    )
    return (
        f'\t(footprint "X"\n\t\t(layer "F.Cu")\n\t\t(at {x} {y})\n'
        f'\t\t(property "Reference" "{ref}"\n\t\t\t(at 0 0 0)\n\t\t\t(layer "F.SilkS")\n\t\t\t(effects\n\t\t\t\t(font\n\t\t\t\t\t(size 1 1)\n\t\t\t\t)\n\t\t\t)\n\t\t)\n'
        f'\t\t(fp_rect\n\t\t\t(start {-w / 2} {-h / 2})\n\t\t\t(end {w / 2} {h / 2})\n\t\t\t(layer "F.CrtYd")\n\t\t)\n'
        f"{pad_text}\t)\n"
    )


def _board(*fps: str) -> str:
    return "(kicad_pcb\n" + "".join(fps) + ")\n"


def _ref_at(text: str, ref: str) -> tuple[float, float, float]:
    import re

    m = re.search(rf'\(property "Reference" "{ref}"\n\t\t\t\(at ([0-9.+-]+) ([0-9.+-]+) ([0-9.+-]+)\)', text)
    assert m, ref
    return float(m.group(1)), float(m.group(2)), float(m.group(3))


def test_a_reference_turns_to_fit_a_narrow_slot():
    """A tall part with a long name, neighbours close on both sides, wide parts above and below:
    "C_MCU_HF" is 2.3 mm long even at 0.4 mm and only fits turned, in the 0.9 mm slot beside it."""
    text = _board(
        _fp("C_MCU_HF", 10, 10, 1.0, 3.2),
        _fp("L1", 8.0, 10, 1.0, 3.2),  # 0.9 mm to the left
        _fp("R1", 12.0, 10, 1.0, 3.2),  # 0.9 mm to the right
        _fp("U9", 10, 7.0, 8.0, 2.4),  # wide, right above
        _fp("U8", 10, 13.0, 8.0, 2.4),  # wide, right below
    )
    new, report = legalize_silk(text, (30, 30))
    assert not any(m.startswith("C_MCU_HF") for m in report["issues"]), report["issues"]
    _x, _y, rot = _ref_at(new, "C_MCU_HF")
    assert rot in (90.0, 270.0), "the slot beside C_MCU_HF only fits turned text"


def test_a_big_part_carries_its_reference_on_its_body():
    """A module in the board corner, a neighbour flush on each free side; its pads are on the edges."""
    pads = [(-5.5, y, 0.8, 0.4) for y in (-4, -3.2, -2.4)] + [(5.5, y, 0.8, 0.4) for y in (-4, -3.2, -2.4)]
    text = _board(
        _fp("U1", 7.0, 7.0, 13.0, 13.0, pads),
        _fp("C1", 14.4, 7.0, 1.6, 13.0),
        _fp("C2", 7.0, 14.4, 13.0, 1.6),
        _fp("C3", 14.4, 14.4, 1.6, 1.6),  # the corner too
    )
    new, report = legalize_silk(text, (15.6, 15.6))
    assert not any(m.startswith("U1") for m in report["issues"]), report["issues"]
    x, y, _rot = _ref_at(new, "U1")
    assert abs(x) < 4 and abs(y) < 4, (x, y, "the reference sits on the body, near its centre")


def test_no_room_is_reported_as_a_move():
    """In the board corner, a wide part below and a part touching on the right."""
    text = _board(
        _fp("C1", 1.2, 1.0, 1.6, 1.0),
        _fp("U9", 3.0, 2.5, 6.0, 2.4),
        _fp("U8", 3.0, 1.0, 2.4, 1.0),
    )
    _new, report = legalize_silk(text, (8, 8))
    assert any(m.startswith("C1: no clear spot for its silkscreen reference") for m in report["issues"]), report["issues"]


def test_the_examples_references_all_fit():
    from pcbc.build import pcb_job

    root = Path(__file__).resolve().parent.parent / "examples"
    for name in ("blinky", "buck", "c3_usb", "node"):
        result = pcb_job(root / name / f"{name}.py")
        assert result["layout_report"] == [], (name, result["layout_report"])
