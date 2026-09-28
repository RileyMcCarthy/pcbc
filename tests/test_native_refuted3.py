"""What the third refutation round found, pinned (`scratchpad/native/fix3/`).

- Locks: a spur with nothing left to join, a lock on a net the router leaves unfinished, and a locked via
  whose second layer nothing joined were each refused as "a pcbc bug". Now a core via's free layer is a
  routing terminal (`route_native.EndTerminal`, kind `via`), and the route stage records what became of
  every free end (`route_native.core_end_states`); the build words KiCad's `track_dangling` /
  `via_dangling` from that record (`build._dangling_core_move`).
- The router took a pad number drawn as several pads (the ESP32-C3-MINI's nine `49` blocks) for one
  terminal and called GND routed where KiCad found blocks open. Now each block is a terminal
  (`route_scene.Item.block`, `route_native.pad_terminals`), the router's own count of open links is
  checked on the copper it hands on (`route_native.open_links`) and held to KiCad's per net on every
  build (`build.py`, "the router's count of open links is not KiCad's").
- The thermal land: every pad of the land takes a barrel before any takes a second, a pad left bare is a
  refusal naming its blocker, and the fab note states what is on the board, not the budget.
- Placement read an in-memory board text and the router's `Pose`s were decompiled from it. Now the place
  stage reads the Python model only (`place_native`), and the `Pose`s are the resolved `Place` objects.
"""

from __future__ import annotations

import math
import shutil
import sys
from pathlib import Path

import pytest

from pcbc.build import build_job
from pcbc.language import load_board

ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = ROOT / "examples"
FIXTURES = ROOT / "tests" / "fixtures"
DS2 = Path.home() / "Documents/MaD/Hardware/DS2Addon/pcbc"


def _route(result: dict) -> dict:
    return next(s for s in result["steps"] if s.get("stage") == "route")


def _buck(tmp_path: Path, core: str) -> Path:
    src = EXAMPLES / "buck"
    board = tmp_path / "buck.py"
    board.write_text((src / "buck.py").read_text())
    shutil.copytree(src / "components", tmp_path / "components")
    (tmp_path / "layout.core.py").write_text(core)
    return board


def _thermal(tmp_path: Path, watts: float | None, core: str = "") -> Path:
    text = (FIXTURES / "thermal" / "thermal.py").read_text()
    assert 'Thermal("U1.49", watts=1.0)' in text
    if watts is None:
        text = text.replace('Thermal("U1.49", watts=1.0)\n', "")
    else:
        text = text.replace('Thermal("U1.49", watts=1.0)', f'Thermal("U1.49", watts={watts})')
    board = tmp_path / "thermal.py"
    board.write_text(text)
    shutil.copytree(EXAMPLES / "c3_usb" / "components" / "Espressif", tmp_path / "components" / "Espressif")
    if core:
        (tmp_path / "layout.core.py").write_text(core)
    return board


# --- locks ---------------------------------------------------------------------------------------------


@pytest.mark.kicad
def test_a_spur_with_nothing_left_to_join_is_named_a_spur(tmp_path: Path):
    """buck, `BOOT` locked pad to pad (line 1) plus a spur off it into open board (line 2). Every BOOT
    terminal is already the lock's own copper, so the spur's free end has nothing to join: the route
    stage says so in a note and in its record, and the build's refusal names the spur and the move — it
    used to say "the route stage reported every free end of it joined: this is a pcbc bug" (the third
    refutation round, locks major 1)."""
    board = _buck(tmp_path, 'Seg("BOOT", (15.51, 6.464), (15.51, 7.66), width=0.3, layer="F.Cu")\nSeg("BOOT", (15.51, 7.0), (13.5, 7.0), width=0.3, layer="F.Cu")\n')
    result = build_job(board, upto="fab", force=True)
    err = result["error"] or ""
    route = _route(result)
    assert "layout.core.py:2: track_dangling" in err and "a spur: this core track's free end at (13.5,7) has nothing left to join" in err, err
    assert "end the line on the copper it joins, or delete it" in err and "pcbc bug" not in err, err
    assert any(n.startswith("BOOT: layout.core.py:2's free end at (13.5,7) has nothing to join") for n in route["notes"]), route["notes"]
    states = [(e["line"], e["kind"], tuple(e["at"]), e["state"]) for e in route["route_stats"]["core_ends"]]
    assert states == [("layout.core.py:2", "track", (13.5, 7.0), "nothing")], states


@pytest.mark.kicad
@pytest.mark.parametrize(
    "core",
    [
        # a pad escape with its fanout via, on 5V (no pour): the via's B.Cu is free
        'Seg("5V", (28.132, 16.246), (28.132, 19.5), width=0.8, layer="F.Cu")\nVia("5V", (28.132, 19.5), size=0.8, drill=0.4)\n',
        # a track between two locked vias, on 5V: both vias free on B.Cu
        'Via("5V", (24.0, 20.0), size=0.8, drill=0.4)\nSeg("5V", (24.0, 20.0), (28.0, 20.0), width=0.8, layer="F.Cu")\nVia("5V", (28.0, 20.0), size=0.8, drill=0.4)\n',
        # a lone via: free on both layers, joined on two
        'Via("5V", (32.0, 19.5), size=0.8, drill=0.4)\n',
    ],
    ids=["escape_via", "via_track_via", "lone_via"],
)
def test_a_locked_via_is_joined_on_its_free_layer(tmp_path: Path, core: str):
    """A core via joined on fewer than two layers is a routing terminal on each layer nothing of its net
    touches. On buck's `5V`, which has no pour, these locks were refused as `via_dangling` ("move it onto
    the copper it should join", with nothing to point at: the router never continued from the via; the
    third refutation round, locks major 3). Now each builds to fab, every free end the locks had is
    `joined`, and KiCad finds no dangling via."""
    board = _buck(tmp_path, core)
    result = build_job(board, upto="fab", force=True)
    assert result["error"] is None and result.get("ok"), result["error"]
    route = _route(result)
    assert not [k for k in route["drc_by_type"] if k.endswith(("track_dangling", "via_dangling"))], route["drc_by_type"]
    ends = route["route_stats"]["core_ends"]
    assert ends and all(e["state"] == "joined" for e in ends), ends
    assert any(e["kind"] == "via" for e in ends), ends


@pytest.mark.kicad
def test_a_lock_no_pad_reaches_is_the_locks_finding_not_an_unfinished_net(tmp_path: Path):
    """A lone `5V` via locked in buck's corner by `FID3`, whose mask keepout the router may not cross:
    the via is a component of its own with no pad, and no link reaches it. The net's pads are all joined,
    so `5V` is routed and keeps its copper; the lock is the finding — the build quotes the router's note
    and names the moves. (Counted as a failed link of the net, it made `5V` unfinished and dropped all
    of its generated copper for one via.)"""
    board = _buck(tmp_path, 'Via("5V", (2.5, 22.5), size=0.5, drill=0.3)\n')
    result = build_job(board, upto="fab", force=True)
    err = result["error"] or ""
    route = _route(result)
    assert not result.get("unrouted") and route["links"]["5V"][0] == route["links"]["5V"][1], (result.get("unrouted"), route["links"])
    assert "layout.core.py:1: via_dangling" in err and "the router could not join this core via's free end" in err and "pcbc bug" not in err, err
    assert [e["state"] for e in route["route_stats"]["core_ends"]] == ["failed"], route["route_stats"]["core_ends"]


@pytest.mark.kicad
def test_a_lock_on_an_unfinished_net_is_not_blamed(tmp_path: Path):
    """c3_usb with a stub locked on `VBUS` from `C_VBUS.1`. `VBUS` is unfinished at baseline, so its
    generated copper — the router's link to the stub's free end with it — is dropped (`drop_unfinished`),
    and KiCad finds the stub's end open. That is the unfinished net, not the line and not pcbc: the refusal
    carries VBUS's moves and says the link went with the net (it used to add "this is a pcbc bug"; the
    third refutation round, locks major 2)."""
    src = EXAMPLES / "c3_usb"
    board = tmp_path / "c3_usb.py"
    board.write_text((src / "c3_usb.py").read_text())
    shutil.copytree(src / "components", tmp_path / "components")
    (tmp_path / "layout.core.py").write_text('Seg("VBUS", (30.618, 10.25), (30.618, 7.5), width=0.4, layer="F.Cu")\n')
    result = build_job(board, upto="fab", force=True)
    err = result["error"] or ""
    assert result.get("unrouted") == ["VBUS"], result.get("unrouted")
    assert "layout.core.py:1: track_dangling" in err and "was joined by a link that was dropped with the unfinished net VBUS" in err, err
    assert "pcbc bug" not in err, err
    states = {(e["line"], e["state"]) for e in _route(result)["route_stats"]["core_ends"]}
    assert states == {("layout.core.py:1", "dropped")}, states


# --- the router's connectivity against KiCad's -----------------------------------------------------------


@pytest.mark.kicad
def test_a_pad_number_drawn_as_nine_pads_is_nine_terminals(tmp_path: Path):
    """The thermal fixture with no `Thermal()`: the ESP32-C3-MINI's exposed land is nine `(pad "49")`
    blocks and nothing but the router joins them. The router took them for one terminal (GND links
    [0, 0]) and the build stopped with "KiCad reports GND unconnected and the router reported them
    routed: this is a pcbc router bug" (`scratchpad/native/fix3/after/thnone_old`). Now each block is a
    terminal: 8 links join the nine, and the router's open-link count equals KiCad's on every net."""
    board = _thermal(tmp_path, None)
    result = build_job(board, upto="fab", force=True)
    assert result["error"] is None and result.get("ok"), result["error"]
    route = _route(result)
    assert route["links"]["GND"] == [8, 8], route["links"]
    assert route["open_links"] == {"router": {}, "kicad": {}}, route["open_links"]


def test_the_router_joins_pad_blocks_by_copper_not_by_number(tmp_path: Path):
    """`route_native.pad_terminals` and `_net_groups` on the fixture's placed scene: nine `U1.49`
    terminals, one per block (the first keeps the owner's name), in nine groups before routing."""
    from pcbc.place_native import place
    from pcbc.route_native import _net_groups, pad_terminals
    from pcbc.route_scene import build_scene

    design = load_board(_thermal(tmp_path, 1.0))
    pl = place(design, name="thermal")
    scene = build_scene(design, pl.job, pl.job.constraints, pl.feet)
    pads, _vias = _net_groups(scene, "GND")
    terms = [t for t in pad_terminals(scene, "GND", pads) if t.owner.startswith("U1.49")]
    assert len(terms) == 9 and terms[0].owner == "U1.49" and all(t.owner.startswith("U1.49@(") for t in terms[1:]), [t.owner for t in terms]
    assert len({pads[(t.item.owner, t.item.block)] for t in terms}) == 9


@pytest.mark.kicad
def test_the_build_holds_the_routers_count_to_kicads(tmp_path: Path, monkeypatch):
    """The comparison runs on every build: a KiCad count that is not the router's stops the build as a
    pcbc bug, count for count. Proved by feeding the build a KiCad that counts one open GND link blinky's
    router does not have."""
    import pcbc.netcheck

    src = EXAMPLES / "blinky"
    board = tmp_path / "blinky.py"
    board.write_text((src / "blinky.py").read_text())
    real = pcbc.netcheck._unconnected_by_net
    monkeypatch.setattr(pcbc.netcheck, "_unconnected_by_net", lambda u: {**real(u), "GND": 1})
    result = build_job(board, upto="route", force=True)
    err = result["error"] or ""
    assert "the router's count of open links is not KiCad's: GND: the router's copper leaves 0, KiCad counts 1" in err and "pcbc bug" in err, err


# --- the thermal land -----------------------------------------------------------------------------------


@pytest.mark.kicad
def test_every_pad_of_a_thermal_land_takes_a_barrel(tmp_path: Path):
    """At 0.35 W the land asks for 9 barrels. Placed rank-major they went 9 into 6 pads: a 3V3 track on
    B.Cu under the top row blocked every first-rank site there, the second-rank sites were never tried,
    and the gate said "served" (the third refutation round). Now every pad takes one before any takes a
    second, and the board reaches fab with 9 barrels in 9 pads."""
    board = _thermal(tmp_path, 0.35)
    result = build_job(board, upto="fab", force=True)
    assert result["error"] is None and result.get("ok"), result["error"]
    (row,) = _route(result)["thermal"]["arrays"]
    assert (row["want"], row["got"], row["in_pad"], row["blocks"], row["bare"], row["verdict"]) == (9, 9, 9, 9, [], "served"), row


@pytest.mark.kicad
def test_a_bare_thermal_pad_is_refused_with_its_blocker(tmp_path: Path):
    """A 3V3 track locked on B.Cu under the land's ninth pad takes every site in it. The pad carries no
    barrel, and the build stops with the gate's sentence, the pattern's note naming the locked line that
    emptied it, and the moves (it is a lock, so moving it is the author's edit)."""
    board = _thermal(tmp_path, 0.35, 'Seg("3V3", (10.0, 15.225), (11.65, 15.225), width=0.3, layer="B.Cu")\n')
    result = build_job(board, upto="fab", force=True)
    err = result["error"] or ""
    assert err.startswith("a thermal land is not served: thermal U1.49: 9 of 9 barrel(s)"), err
    assert "8 of its 9 pads with one" in err and "1 of its 9 pads carry no barrel ((10.825,15.225))" in err, err
    assert "worst blocker layout.core.py:1 [3V3]" in err and "Moves: move layout.core.py:1 off the land or delete it" in err, err
    (row,) = _route(result)["thermal"]["arrays"]
    assert row["verdict"] == "bare" and row["bare"] == [[10.825, 15.225]], row


@pytest.mark.kicad
def test_a_short_array_fab_note_states_the_board_not_the_budget(tmp_path: Path):
    """At 2 W the land asks for 47 barrels and has room for 30. The fab note said "47 x 0.5/0.3 mm ... 47
    in parallel are 4.92 K/W and 2 W raises the copper 9.83 C" — the budget, not the board (the third
    refutation round). Now it states the 30 on the board, their 7.7 K/W and 15.41 C, and that the land
    is short and over its budget."""
    board = _thermal(tmp_path, 2.0)
    result = build_job(board, upto="fab", force=True)
    assert result["error"] is None and result.get("ok"), result["error"]
    whole = (tmp_path / "layout" / "thermal" / "fab" / "FAB_NOTES.md").read_text()
    # The thermal section; the constraints section above it quotes the budget as a budget ("wants").
    notes = whole[whole.index("## Thermal vias") :].split("\n## ", 1)[0]
    assert "30 via(s) are placed **inside** a pad on purpose" in notes, notes
    assert "- **U1.49** on `GND`: 30 x 0.5/0.3 mm through vias" in notes and "so 30 in parallel are 7.7 K/W and 2 W raises the copper 15.41 C against a 10 C budget." in notes, notes
    assert "**Short:** the budget asks for 47 barrel(s) (9.83 C) and the land had room for 30, so this land runs +5.41 C over its budget" in notes, notes
    assert "47 x 0.5/0.3" not in notes and "47 in parallel" not in notes, notes


# --- placement reads the Python model -------------------------------------------------------------------


def _trap_text_readers(monkeypatch, hits: list[str]):
    """Every board-text reader raises: the s-expression board readers, the footprint-block cutters, the
    board-file footprint parsers, the placed board's uuid table and alias index, and `Placement.text`."""
    import pcbc.layout
    import pcbc.layout_job
    import pcbc.pads
    import pcbc.pcb_place
    import pcbc.refs
    import pcbc.sexp
    from pcbc.place_native import Placement

    def trap(name):
        def f(*a, **k):
            hits.append(name)
            raise AssertionError(f"a board text was read through {name}")

        return f

    readers = [
        (pcbc.sexp, "parse_tree"),
        (pcbc.sexp, "board_footprint_spans"),
        (pcbc.sexp, "footprint_at"),
        (pcbc.sexp, "footprint_reference"),
        (pcbc.layout, "footprints_by_ref"),
        (pcbc.pcb_place, "parse_foot"),
        (pcbc.pads, "pad_geoms"),
        (pcbc.layout_job, "board_uuids"),
        (pcbc.refs, "build_alias_index"),
    ]
    for mod, attr in readers:
        f = trap(f"{mod.__name__}.{attr}")
        orig = getattr(mod, attr)
        for m in list(sys.modules.values()):
            if getattr(m, "__name__", "").startswith("pcbc") and getattr(m, attr, None) is orig:
                monkeypatch.setattr(m, attr, f)
    monkeypatch.setattr(Placement, "text", property(lambda self: (_ for _ in ()).throw(AssertionError("Placement.text read"))))


@pytest.mark.parametrize("name", ["buck", "c3_usb"])
def test_place_and_the_core_read_no_board_text(monkeypatch, tmp_path: Path, name: str):
    """`place_native.place` — the placer's search (`resolve_places`, the silkscreen pass), the fiducials,
    the reports (`layout_report`, `route_aware_report`) — and `layout_job.load_core` (the core lines'
    uuid and layer checks) complete with every board-text reader raising. The third refutation round
    counted 1823 (buck) and 3529 (c3_usb) calls on board text inside `place` alone
    (`scratchpad/native/refute-native-r3/mtsum2.log`); `scratchpad/native/fix3/native/mtsum2.log` counts
    0 on all five boards (the fiducial's own definition, `fab._fiducial_sexp`, is its `.kicad_mod`)."""
    from pcbc.layout_job import load_core
    from pcbc.place_native import place

    src = EXAMPLES / name
    design = load_board(src / f"{name}.py")
    hits: list[str] = []
    _trap_text_readers(monkeypatch, hits)
    pl = place(design, name=name)
    assert pl.error is None and pl.poses and pl.feet, pl.error
    (tmp_path / "layout.core.py").write_text('Seg("GND", (1.0, 1.0), (2.0, 1.0), width=0.3, layer="F.Cu")\n')
    cu, _gr = load_core(design, tmp_path / f"{name}.py", pl, name=name)
    assert len(cu) == 1 and hits == [], hits


@pytest.mark.parametrize("name", ["blinky", "buck", "c3_usb", "node"])
def test_every_pose_is_its_place_object(name: str):
    """The `Pose`s the router reads are the resolved `Place` objects, with no text between: a part a
    `Place()` names sits where `job.places` resolved it (to the 4 decimals and the `:g` angle the board
    holds), every other part at its seed-grid spot, and every `PosedFoot` is its `Pose`. The third
    refutation round shifted R1's `(at` in the text the silkscreen pass returned and the router's R1 moved
    with it while the `Place` model stayed put (`refute-native-r3/textauth.log`); with no text in the
    place stage there is nothing to shift."""
    from pcbc.place_native import kicad_angle, place, seed_feet

    design = load_board(EXAMPLES / name / f"{name}.py")
    pl = place(design, name=name)
    poses = {p.ref: p for p in pl.poses}
    placed = {p.ref: p for p in pl.job.places if p.at is not None}
    seeds = {pf.ref: pf for pf in seed_feet(design)}
    for ref, pose in poses.items():
        if ref in placed:
            p = placed[ref]
            want = ((round(float(f"{p.at[0]:.4f}"), 6), round(float(f"{p.at[1]:.4f}"), 6)), kicad_angle(float(f"{float(p.rot):g}")), "B.Cu" if p.side == "B" else "F.Cu")
        elif ref in seeds:
            want = (seeds[ref].at, 0.0, "F.Cu")
        else:
            assert ref.startswith("FID"), ref
            continue
        assert (pose.at, pose.rot, pose.layer) == want, (name, ref, pose, want)
    for pf in pl.feet:
        pose = poses[pf.ref]
        assert (pf.at, pf.rot, pf.layer) == (pose.at, pose.rot % 360.0, pose.layer), (name, pf.ref)


@pytest.mark.parametrize("name", ["buck", "c3_usb"])
def test_the_core_checks_uuids_from_the_model_as_the_board_holds_them(name: str):
    """`place_native.foreign_uuids` (the model) equals `layout_job.board_uuids` of the emit base (the
    text): every uuid the emitted board's footprints carry, named by the same owner. Measured on all
    seven boards before the text reading left the build (`scratchpad/native/fix3/native/uuid_eq.py`)."""
    from pcbc.layout_job import board_uuids
    from pcbc.place_native import foreign_uuids, place

    design = load_board(EXAMPLES / name / f"{name}.py")
    pl = place(design, name=name)
    assert foreign_uuids(design, pl, name=name) == board_uuids(pl.text)


# --- minors ------------------------------------------------------------------------------------------------


def test_pair_coupling_counts_only_partner_copper_on_the_same_layer():
    """`route_verify._coupled_mm`: two halves 0.2 mm apart in plan are coupled on one layer and not at all
    across two (the third refutation round, A3b: the same-layer rule could be deleted with the suite green)."""
    from pcbc.route_verify import _coupled_mm

    a = [{"start": (0.0, 0.0), "end": (10.0, 0.0), "layer": "F.Cu"}]
    same = [{"start": (0.0, 0.2), "end": (10.0, 0.2), "layer": "F.Cu"}]
    other = [{"start": (0.0, 0.2), "end": (10.0, 0.2), "layer": "B.Cu"}]
    assert _coupled_mm(a, same, 0.3) == (10.0, 10.0)
    assert _coupled_mm(a, other, 0.3) == (10.0, 0.0)


def test_prune_keeps_a_track_end_in_its_own_nets_pour():
    """`route_native.prune_dangling`: a GND stub from a GND pad whose other end touches nothing is removed
    — unless a GND pour covers that layer, which joins the end (the third refutation round, A7b: the
    exemption was dead on every board and could be deleted with the suite green)."""
    from pcbc.model import Copper
    from pcbc.place_native import place
    from pcbc.route_emit import seg_piece
    from pcbc.route_native import prune_dangling
    from pcbc.route_scene import build_scene

    design = load_board(EXAMPLES / "buck" / "buck.py")
    pl = place(design, name="buck")
    scene = build_scene(design, pl.job, pl.job.constraints, pl.feet)
    pad = next(it for it in scene.items if it.kind == "pad" and it.net == "GND" and it.layers == frozenset({"F.Cu"}))
    x, y = pad.at()
    far = (round(x, 4), round(y - 3.0, 4))
    stub = seg_piece("GND", "route", "F.Cu", (x, y), far, 0.3, owner="GND")
    kept, removed = prune_dangling(design, pl.job, pl.feet, [stub])
    assert kept == [] and [r["why"] for r in removed] == [f"its end at ({far[0]:g},{far[1]:g}) touches nothing"], removed
    w, h = pl.job.board_size_mm
    pour = Copper("pour", "", "GND", layer="F.Cu", points=((0.0, 0.0), (w, 0.0), (w, h), (0.0, h)), source="gen")
    kept, removed = prune_dangling(design, pl.job, pl.feet, [stub], (), [pour])
    assert kept == [stub] and removed == [], removed


def test_same_net_copper_laid_over_itself_is_merged():
    """`route_native.merge_overlaps`: a segment inside another of its net and layer at least as wide goes
    (a core one included); a fold — the inner one ends where the container does and nothing else is there
    — takes the container's tail with it; two of one width that overlap partly are cut back to meet;
    nothing else moves. c3_usb's USB_DP ran 3.695 mm over itself and USB_DN folded back on itself, ds2's
    3V3 route ran over a locked stub, node's two GND taps overlapped (the third refutation round,
    `tools/overlap.py`); a first version that dropped a fold's inner segment alone left node's USB_DP with
    a dangling tail (`scratchpad/native/fix3/snapacc/b`)."""
    from pcbc.route_emit import seg_piece, via_piece
    from pcbc.route_native import merge_overlaps, overlaps

    fold = [seg_piece("N", "route", "F.Cu", (0.0, 0.0), (2.0, 0.0), 0.3), seg_piece("N", "route", "F.Cu", (2.0, 0.0), (-1.0, 0.0), 0.3)]
    got, done = merge_overlaps(fold)
    assert [(p.a, p.b) for p in got] == [((0.0, 0.0), (-1.0, 0.0))] and done[0]["folded"]["at"] == [2.0, 0.0], (got, done)
    held = fold + [via_piece("N", "route", (2.0, 0.0), 0.6, 0.3)]  # a via at the turn joins it: no fold
    got, _ = merge_overlaps(held)
    assert sorted((p.kind, p.a, p.b) for p in got) == [("seg", (2.0, 0.0), (-1.0, 0.0)), ("via", (2.0, 0.0), None)], got
    core = [seg_piece("N", "core", "F.Cu", (0.0, 0.0), (0.0, 5.0), 0.3, owner="layout.core.py:1")]
    over = [seg_piece("N", "route", "F.Cu", (0.0, 1.0), (0.0, 4.0), 0.3), seg_piece("N", "route", "F.Cu", (0.0, 4.0), (3.0, 4.0), 0.3)]
    got, _ = merge_overlaps(over, core)
    assert [(p.a, p.b) for p in got] == [((0.0, 4.0), (3.0, 4.0))]
    taps = [seg_piece("N", "tap", "B.Cu", (0.0, 0.0), (2.0, 0.0), 0.3, uuid="u1"), seg_piece("N", "tap", "B.Cu", (1.0, 0.0), (3.0, 0.0), 0.3, uuid="u2")]
    got, _ = merge_overlaps(taps)
    assert [(p.a, p.b, p.uuid) for p in got] == [((0.0, 0.0), (2.0, 0.0), "u1"), ((2.0, 0.0), (3.0, 0.0), "u2")] and overlaps(got) == []
    other = [seg_piece("N", "route", "F.Cu", (0.0, 0.0), (2.0, 0.0), 0.3), seg_piece("M", "route", "F.Cu", (0.0, 0.0), (2.0, 0.0), 0.3), seg_piece("N", "route", "B.Cu", (0.0, 0.0), (2.0, 0.0), 0.3)]
    got, done = merge_overlaps(other)
    assert got == other and done == []
    assert math.isclose(sum(o[-1] for o in overlaps(fold)), 2.0)
