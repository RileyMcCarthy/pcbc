"""Facing warnings name a pad on the far side of a part. They do not propose a pose."""

from pathlib import Path

from pcbc.compile import compile_design
from pcbc.language import load_board
from pcbc.pcb_place import Foot, Pad
from pcbc.place_solve import NetNeed, facing_notes, needs_of, pull

ROOT = Path(__file__).resolve().parents[1]
BLINKY = ROOT / "tests" / "fixtures" / "declared" / "blinky" / "blinky.py"  # as shipped with a declared outline: conftest.declared_examples


def need(net, width=0.2, clearance=0.2, **kw) -> NetNeed:
    base = dict(
        kind="generic",
        vias=True,
        layers=("F.Cu", "B.Cu"),
        keep_away=(),
        pair_with=None,
        pair_gap_mm=None,
        loop_mm2=None,
        airwire_max_mm=None,
    )
    base.update(kw)
    return NetNeed(net=net, width_mm=width, clearance_mm=clearance, **base)


def foot(ref, at, rot, pads, box=(-1.0, -0.6, 1.0, 0.6)) -> Foot:
    part = Foot(ref, box, pads)
    part.at, part.rot = at, rot
    return part


def test_a_pad_on_the_far_side_is_a_warning_without_a_new_pose():
    resistor = foot("R", (10.0, 10.0), 0.0, [Pad("1", -0.5, 0.0, 0.3, 0.3, "VCC"), Pad("2", 0.5, 0.0, 0.3, 0.3, "SIG")])
    diode = foot("D", (16.0, 10.0), 0.0, [Pad("1", -0.7, 0.0, 0.3, 0.3, "GND"), Pad("2", 0.7, 0.0, 0.3, 0.3, "SIG")])
    notes = facing_notes([resistor, diode], [need("SIG", width=0.25, clearance=0.1)], {"R": (True, False), "D": (True, False)})
    assert len(notes) == 1
    assert notes[0].startswith("D: SIG pad is")
    assert "far side" in notes[0]
    assert "0.25 mm wide" in notes[0]
    assert "rot " not in notes[0]
    assert "Place(" not in notes[0]


def test_a_pad_already_facing_the_net_is_silent():
    resistor = foot("R", (10.0, 10.0), 0.0, [Pad("1", -0.5, 0.0, 0.3, 0.3, "VCC"), Pad("2", 0.5, 0.0, 0.3, 0.3, "SIG")])
    diode = foot("D", (16.0, 10.0), 180.0, [Pad("1", -0.7, 0.0, 0.3, 0.3, "GND"), Pad("2", 0.7, 0.0, 0.3, 0.3, "SIG")])
    assert facing_notes([resistor, diode], [need("SIG")], {"D": (True, False)}) == []


def test_a_written_rotation_is_not_warned():
    resistor = foot("R", (10.0, 10.0), 0.0, [Pad("2", 0.5, 0.0, 0.3, 0.3, "SIG")])
    diode = foot("D", (16.0, 10.0), 0.0, [Pad("2", 0.7, 0.0, 0.3, 0.3, "SIG")])
    assert facing_notes([resistor, diode], [need("SIG")], {"D": (True, True), "R": (True, True)}) == []


def test_a_power_rail_pulls_harder_than_a_signal_on_blinky():
    design = load_board(BLINKY)
    needs = {n.net: n for n in needs_of(compile_design(design).constraints)}
    assert pull(needs["VCC"]) > pull(needs["LED"])
    assert needs["VCC"].clearance_mm >= 0.2


def test_blinky_warns_that_the_led_pad_faces_away_and_does_not_move_it():
    from pcbc.build import pcb_job

    result = pcb_job(BLINKY)
    notes = [n for n in result["layout_notes"] if n.startswith("D1:")]
    assert len(notes) == 1, result["layout_notes"]
    assert "LED" in notes[0] and "far side" in notes[0]
    assert "Place(" not in notes[0]
    assert result["poses"]["D1"]["rot"] == 0
