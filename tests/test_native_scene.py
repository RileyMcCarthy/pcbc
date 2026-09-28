"""The router reads the Python model, not a board text (`docs/direction.md` §1; the second refutation
round's major 1).

`route_scene.build_scene` is built from `Placement.feet` — each part's `.kicad_mod` parsed once
(`foot_native.lib_foot`) and posed by its `Pose` — plus the design's keepouts, rule areas and outline.
Until 2026-09-25 it parsed the footprints out of an in-memory KiCad board text (`Placement.text`), and
fanout, the relaxer and the dangling sweep each took that text as a parameter.

The switch was proved before the text path was deleted (`scratchpad/native/fix2/scene_eq.py`,
`scene_eq2.py`, and a native build of all five boards compared file by file with the text-path build):
the two scenes were equal item for item, exact shapes, on all five boards; every file under
`layout/<board>/` came out byte-identical but the stamps' pcbc code hash. The only difference found
was in `Foot.box`, which no item and no piece depends on: the text path read a rotated part's pad box
with the pad's *board* angle (library + footprint) in the footprint's own frame — c3_usb's and node's
`SW_BOOT`/`SW_RST` (4.8 mm wide where the pads span 4.8, read as 5.1), node's `U5`, and buck's `L1`
(4e-16 mm of float noise). Native reads the library angle, which is the frame the box is in.

What stays pinned here: the emitted place-only board read back into the model equals the native scene
item for item on all five boards (a posing bug — a pad not turned with its part — changes a pad's
shape and fails it), and the route stage opens no board text at all.
"""

from __future__ import annotations

import dataclasses
import sys
from pathlib import Path

import pytest

from boardtext import scene_from_text
from pcbc.language import load_board
from pcbc.place_native import emitted_text, place
from pcbc.route_scene import build_scene

ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = ROOT / "examples"
DS2 = Path.home() / "Documents/MaD/Hardware/DS2Addon/pcbc"
BOARDS = {n: EXAMPLES / n / f"{n}.py" for n in ("blinky", "buck", "c3_usb", "node")}
if (DS2 / "ds2_addon.py").exists():
    BOARDS["ds2"] = DS2 / "ds2_addon.py"

ITEMS = {"blinky": 11, "buck": 35, "c3_usb": 136, "node": 167, "ds2": 85}
"""Items of each placed board's scene (pads, holes, lanes, keepouts, the edge), measured 2026-09-25."""


@pytest.mark.parametrize("name", sorted(BOARDS))
def test_the_native_scene_is_the_emitted_board_read_back(name: str):
    """Item for item, with exact shapes: the scene built from `Placement.feet` and the scene read back
    out of the place-only board `emit` writes. Two readings of the same parts — one from the
    `.kicad_mod` and the `Pose`, one from what KiCad will hold — must agree to the nanometre."""
    board = BOARDS[name]
    design = load_board(board)
    pl = place(design, name=board.stem)
    assert pl.error is None, pl.error
    job = pl.job
    native = build_scene(design, job, job.constraints, pl.feet)
    back = scene_from_text(design, job, job.constraints, emitted_text(design, pl, name=board.stem))
    assert len(native.items) == ITEMS[name], (name, len(native.items))
    bad = [(a, b) for a, b in zip(native.items, back.items) if a != b]
    assert len(native.items) == len(back.items) and not bad, (name, bad[:3])
    assert sorted(native.feet) == sorted(back.feet) == sorted(p.ref for p in pl.poses)
    for ref in sorted(native.feet):
        assert dataclasses.asdict(native.feet[ref]) == dataclasses.asdict(back.feet[ref]), (name, ref)
    assert (native.outline, native.plane_of, native.layers, native.grid) == (back.outline, back.plane_of, back.layers, back.grid)
    # And every pad against the reader the checkers use on an emitted board (`pads.pad_geoms` over the
    # footprint block, KiCad's board convention), which shares no posing code with `foot_native`.
    from pcbc.foot_native import pad_geoms_of
    from pcbc.layout import footprints_by_ref
    from pcbc.pads import pad_geoms
    from pcbc.sexp import footprint_at

    blocks = footprints_by_ref(emitted_text(design, pl, name=board.stem))
    for pf in pl.feet:
        want = pad_geoms(blocks[pf.ref], footprint_at(blocks[pf.ref]), ref=pf.ref, nets=pf.net_map(), layers=native.layers)
        assert pad_geoms_of(pf, native.layers) == want, (name, pf.ref)


def test_the_route_stage_reads_no_board_text(monkeypatch):
    """Every board-text reader the scene used to go through raises while buck is routed; the route
    stage (patterns, fanout, the router, the relaxer, the dangling sweep) completes anyway. The
    footprints come from the `.kicad_mod` files, parsed by `pads.pad_specs`, which is allowed."""
    import pcbc.layout
    import pcbc.pads
    import pcbc.pcb_place
    import pcbc.sexp
    from pcbc import route_native

    board = BOARDS["buck"]
    design = load_board(board)
    pl = place(design, name=board.stem)
    hits: list[str] = []

    def trap(name):
        def f(*a, **k):
            hits.append(name)
            raise AssertionError(f"the route stage read a board text through {name}")

        return f

    readers = [(pcbc.layout, "footprints_by_ref"), (pcbc.pcb_place, "parse_foot"), (pcbc.pads, "pad_geoms"), (pcbc.sexp, "board_footprint_spans"), (pcbc.sexp, "footprint_at")]
    for mod, attr in readers:
        f = trap(f"{mod.__name__}.{attr}")
        for m in list(sys.modules.values()):
            if getattr(m, "__name__", "").startswith("pcbc") and getattr(m, attr, None) is getattr(mod, attr):
                monkeypatch.setattr(m, attr, f)
    monkeypatch.setattr(type(pl), "text", property(lambda self: (_ for _ in ()).throw(AssertionError("Placement.text read by the route stage"))), raising=False)
    rt = route_native.route_stage(design, pl.job, pl, name=board.stem)
    assert hits == [] and not rt.failed and rt.links, (hits, rt.unrouted)
