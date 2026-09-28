"""The first adversarial round on the native generator (2026-09-25): each major the refuters reproduced,
pinned by the property it broke. Every build here runs on a copy under `tmp_path`, never in `examples/`.

- D1: a net the router cannot finish has **no** generated copper in `layout.gen.py` (`docs/direction.md`
  §5 is per net, not per link).
- D2: the "allow vias" move names the `NetReq` line the net is already on, and is one `pcbc check` accepts.
- D3: a pattern that would put a via on a `vias=False` net is a move, not an internal error.
- D4: a free end of a locked core track is a routing terminal (the §7 loop: copy a gen line, edit board.py).
- D5: `--replay` routes with the core file the build had.
- D6: emit may change nothing but layout objects (the second review's m5, restored natively).
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

import pytest

from conftest import routed_result
from pcbc.build import build_job, non_layout_diff
from pcbc.gen import decompile

ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = ROOT / "examples"


def _copy(name: str, tmp: Path) -> Path:
    shutil.copytree(EXAMPLES / name, tmp / name, ignore=shutil.ignore_patterns("layout"))
    return tmp / name / f"{name}.py"


def _route(result: dict) -> dict:
    return next(s for s in result["steps"] if s["stage"] == "route")


# --- D1 ----------------------------------------------------------------------------------------------


@pytest.mark.kicad
def test_an_unrouted_net_has_no_generated_copper_in_layout_gen():
    """Refuted before: c3_usb's VBUS was reported unrouted with 13 `Seg`s of it in `layout.gen.py`
    (router links that did route, and fanout escapes). Now every generated piece of an unfinished net
    is dropped, the route stage says how many, and one move line says so."""
    final, result = routed_result("c3_usb", EXAMPLES / "c3_usb" / "c3_usb.py")
    unrouted = result.get("unrouted") or []
    if not unrouted:
        pytest.skip("c3_usb finishes now; D1 needs an unfinished board")
    gen = (final.parent / "layout.gen.py").read_text()
    for net in unrouted:
        assert not re.search(rf'^(Seg|Arc|Via|Pour)\("{re.escape(net)}"', gen, flags=re.M), net
        assert not [c for c in decompile(final.read_text()).copper if c.net == net and c.kind in ("seg", "arc", "via")], net
    dropped = _route(result)["route_stats"]["unfinished_dropped"]
    assert set(dropped) == set(unrouted) and all(sum(v.values()) > 0 for v in dropped.values()), dropped
    assert any(m.startswith(f"{unrouted[0]}: unfinished, so none of its generated copper") for m in result["moves"]), result["moves"]
    # One move per missing link, not per pair of components (the refuters counted 3 moves for 2 links on node).
    route = _route(result)
    missing = sum(t - d for d, t in route["links"].values())
    assert sum(1 for m in result["moves"] if "cannot reach" in m) == missing, (missing, result["moves"])


# --- D2 ----------------------------------------------------------------------------------------------


def _scene_and_cost(board: Path, net: str):
    from pcbc.language import load_board
    from pcbc.place_native import place
    from pcbc.route_cost import net_cost
    from pcbc.route_scene import build_scene

    design = load_board(board)
    pl = place(design, name=board.stem, base=board.parent / "layout")
    scene = build_scene(design, pl.job, pl.job.constraints, pl.feet)
    return design, pl.job, scene, net_cost(pl.job.constraints, net)


def test_the_via_move_names_the_netreq_line_the_net_is_on(tmp_path: Path):
    """Refuted before: "allow vias with NetReq('SW', vias=True)" — SW already has its own NetReq at
    board.py:56, so the move as printed is refused by `pcbc check` ("both name it"); and on a
    one-layer preset a via alone cannot help. Now the move names the line, and offers layers= with
    vias= when the net's layers leave one copper layer; the edit it describes passes `pcbc check`."""
    from pcbc.route_native import _via_move

    board = _copy("buck", tmp_path)
    design, job, scene, nc = _scene_and_cost(board, "SW")
    line = next(i for i, ln in enumerate(board.read_text().splitlines(), 1) if ln.startswith('NetReq("SW"'))
    move = _via_move(design, job, scene, "SW", nc)
    assert f"its NetReq at board.py:{line}" in move and "layers=('F.Cu', 'B.Cu') and vias=True" in move, move
    assert "NetReq('SW', vias=True)" not in move and "split" not in move, move
    # The edit, applied as described, is a board `pcbc check` accepts.
    lines = board.read_text().splitlines()
    lines[line - 1] = lines[line - 1][:-1] + ', layers=("F.Cu", "B.Cu"), vias=True)'
    board.write_text("\n".join(lines) + "\n")
    assert build_job(board, upto="check", force=True).get("error") is None


def test_the_via_move_says_to_split_a_net_out_of_a_shared_netreq(tmp_path: Path):
    """A net on a multi-net NetReq: add vias=True there *after* splitting it out (adding a second line
    naming the net is refused by `pcbc check`)."""
    from pcbc.route_native import _via_move

    board = _copy("buck", tmp_path)
    text = board.read_text()
    old = 'NetReq("VIN", "5V", "GND", kind="power", volts=12, amps=2)'
    assert old in text
    board.write_text(text.replace(old, old[:-1] + ", vias=False)"))
    design, job, scene, nc = _scene_and_cost(board, "VIN")
    line = next(i for i, ln in enumerate(board.read_text().splitlines(), 1) if ln.startswith('NetReq("VIN"'))
    move = _via_move(design, job, scene, "VIN", nc)
    assert move == f"or let VIN change layers: add vias=True to its NetReq at board.py:{line} (that line also names 5V, GND: split VIN out into a NetReq of its own first)", move


# --- D3 ----------------------------------------------------------------------------------------------


@pytest.mark.kicad
def test_a_no_via_power_netreq_ends_in_moves_not_an_internal_error(tmp_path: Path):
    """Refuted before: `vias=False` on buck's power NetReq ended in "internal router error ...
    pattern copper failed its own self-check: tap GND: a via on a net whose NetReq forbids vias".
    Now the tap is not written, its refusal names the NetReq line, and the build stops on moves."""
    board = _copy("buck", tmp_path)
    old = 'NetReq("VIN", "5V", "GND", kind="power", volts=12, amps=2)'
    board.write_text(board.read_text().replace(old, old[:-1] + ", vias=False)"))
    line = next(i for i, ln in enumerate(board.read_text().splitlines(), 1) if ln.startswith('NetReq("VIN"'))
    result = build_job(board, upto="route", force=True)
    err = str(result.get("error") or "")
    assert "internal" not in err and "pcbc bug" not in err, err
    route = _route(result)
    taps = [m for m in route["pattern_moves"] if m.startswith("tap GND: not written")]
    assert taps and all(f"its NetReq at board.py:{line} forbids vias" in m for m in taps), route["pattern_moves"]
    gen = (board.parent / "layout" / "buck" / "routed" / "layout.gen.py").read_text()
    assert not re.search(r'^Via\("(VIN|5V|GND)"', gen, flags=re.M)
    assert err.startswith("unrouted: ") or result.get("error") is None, err


# --- D4 ----------------------------------------------------------------------------------------------


def _touches(c, pt, tol: float) -> bool:
    ends = [c.at] if c.kind == "via" else [c.a, c.b]
    return any(e is not None and abs(e[0] - pt[0]) <= tol and abs(e[1] - pt[1]) <= tol for e in ends)


@pytest.mark.kicad
def test_a_hand_stub_from_a_pad_is_continued_by_the_router(tmp_path: Path):
    """The refuters' E3: a locked stub off the J_OUT 5V pad was routed around and the build then
    refused it as "joins nothing". Its free end is now a terminal: the build finishes and other 5V
    copper starts at the stub's end."""
    board = _copy("buck", tmp_path)
    (board.parent / "layout.core.py").write_text('Seg("5V", (37.55, 13.025), (37.55, 11.0), width=0.781)\n')
    result = build_job(board, upto="fab", force=True)
    assert result.get("error") is None, result.get("error")
    got = decompile((board.parent / "layout" / "buck" / "routed" / "layout.kicad_pcb").read_text())
    stub = [c for c in got.copper if c.kind == "seg" and c.net == "5V" and {tuple(c.a), tuple(c.b)} == {(37.55, 13.025), (37.55, 11.0)}]
    assert len(stub) == 1
    joined = [c for c in got.copper if c.net == "5V" and c.uuid != stub[0].uuid and c.kind in ("seg", "via") and _touches(c, (37.55, 11.0), 1e-3)]
    assert joined, [c for c in got.copper if c.net == "5V"]


@pytest.mark.kicad
def test_a_gen_line_locked_verbatim_survives_a_placement_edit(tmp_path: Path):
    """The refuters' E10, the §7 loop: copy the 5V gen line that leaves J_OUT into `layout.core.py`,
    move U1 in `board.py`, rebuild. Refuted before: the router reached J_OUT from the other side and
    the build refused the lock as joining nothing. Now the route continues from the lock's free end."""
    final, _ = routed_result("buck", EXAMPLES / "buck" / "buck.py")
    got0 = decompile(final.read_text())
    _design, _job, scene, _nc = _scene_and_cost(EXAMPLES / "buck" / "buck.py", "5V")
    pad_pts = {tuple(round(v, 3) for v in it.at()) for it in scene.items if it.kind == "pad" and it.net == "5V" and it.owner.startswith("J_OUT.")}
    assert pad_pts
    gen = (final.parent / "layout.gen.py").read_text().splitlines()
    # A 5V segment one of whose ends is a J_OUT 5V pad centre on the baseline board: the escape.
    on_pad = lambda pt: tuple(round(v, 3) for v in pt) in pad_pts  # noqa: E731
    lock = next(c for c in got0.copper if c.net == "5V" and c.kind == "seg" and (on_pad(c.a) or on_pad(c.b)))
    line = next(ln for ln in gen if ln.startswith('Seg("5V"') and f'uuid="{lock.uuid}"' in ln)
    board = _copy("buck", tmp_path)
    (board.parent / "layout.core.py").write_text(line + "\n")
    text = board.read_text()
    assert "left=15, top=8, locked=True" in text
    board.write_text(text.replace("left=15, top=8, locked=True", "left=15, top=10, locked=True"))
    result = build_job(board, upto="fab", force=True)
    assert result.get("error") is None, result.get("error")
    route = _route(result)
    assert route["locked"] == [re.search(r', id="([^"]+)"', line).group(1)], route["lock_passes"]
    got = decompile((board.parent / "layout" / "buck" / "routed" / "layout.kicad_pcb").read_text())
    free = lock.b if on_pad(lock.a) else lock.a
    assert [c for c in got.copper if c.net == "5V" and c.source != "core" and c.uuid != lock.uuid and _touches(c, free, 1e-3)], free


# --- D5 ----------------------------------------------------------------------------------------------


@pytest.mark.kicad
def test_replay_routes_with_the_core_file_the_build_had(tmp_path: Path):
    """The refuters' E8: four core rule areas round C_BOOT leave BOOT and SW unrouted; the problem
    files' own replay command said `still_fails: false`, because replay never loaded layout.core.py."""
    from pcbc.route_native import replay

    board = _copy("buck", tmp_path)
    ko = '{"tracks": "not_allowed", "vias": "not_allowed", "pads": "allowed", "copperpour": "allowed", "footprints": "allowed"}'
    rects = [(11.0, 6.85, 16.9, 7.15), (11.0, 4.0, 11.3, 7.15), (11.0, 4.0, 16.9, 4.3), (16.6, 4.0, 16.9, 7.15)]
    core = "\n".join(
        f'Pour(None, layers=("F.Cu", "B.Cu"), name="ko{k}", keepout={ko}, points=[({x0}, {y0}), ({x1}, {y0}), ({x1}, {y1}), ({x0}, {y1})], id="ko{k}")'
        for k, (x0, y0, x1, y1) in enumerate(rects, 1)
    )
    (board.parent / "layout.core.py").write_text(core + "\n")
    result = build_job(board, upto="route", force=True)
    assert str(result.get("error") or "").startswith("unrouted: "), result.get("error")
    files = sorted((board.parent / "layout" / "buck" / "routed" / "unrouted").glob("*.json"))
    assert files
    for f in files:
        doc = json.loads(f.read_text())
        got = replay(f, board)
        assert got["still_fails"] is True and got["core_lines"] == 4, (f.name, got)
        assert doc["net"] in got["unrouted"]


# --- D6 ----------------------------------------------------------------------------------------------


def test_non_layout_diff_sees_a_pad_and_ignores_uuids_and_layout_objects():
    """m5's unit half, restored: uuids and layout objects are free to differ, a pad is not."""
    a = '(kicad_pcb\n\t(version 1)\n\t(footprint "x"\n\t\t(uuid "1")\n\t\t(at 1 2)\n\t\t(pad "1" smd rect\n\t\t\t(at 0 0)\n\t\t\t(size 1 1)\n\t\t\t(uuid "2")\n\t\t)\n\t)\n\t(segment\n\t\t(start 0 0)\n\t\t(end 1 1)\n\t\t(uuid "3")\n\t)\n)\n'
    b = a.replace('(uuid "1")', '(uuid "9")').replace('(uuid "2")', '(uuid "8")').replace("(end 1 1)", "(end 2 2)")
    assert non_layout_diff(a, b) == []
    c = b.replace("(size 1 1)", "(size 1 1.2)")
    got = non_layout_diff(a, c)
    assert len(got) == 2 and "(footprint)" in got[0] and "(footprint)" in got[1], got


def _shrink_r1_pad(text: str) -> str:
    i = text.index('(property "Reference" "R1"')
    fp = text.rindex("(footprint", 0, i)
    m = re.compile(r'\(pad "1" smd (\w+)(.*?)\(size ([0-9.]+) ([0-9.]+)\)', re.S).search(text, fp)
    s = m.start() + m.group(0).rindex("(size")
    return text[:s] + f"(size {float(m.group(3)) * 0.8:g} {float(m.group(4)) * 0.8:g})" + text[m.end():]


@pytest.mark.kicad
def test_an_emit_only_pad_change_is_refused(tmp_path: Path, monkeypatch):
    """m5 at build level: an emitter that shrinks R1's pad 1 shipped to fab (`build ok: True`, fab pad
    (size 0.432 0.512)). Now emit is compared with the placed board before the board is written."""
    import pcbc.layout_job as lj

    real = lj.emit_board
    monkeypatch.setattr(lj, "emit_board", lambda *a, **k: _shrink_r1_pad(real(*a, **k)))
    board = _copy("blinky", tmp_path)
    result = build_job(board, upto="fab", force=True)
    assert "emit changed the board outside the layout objects" in str(result.get("error")), result.get("error")
    assert not (board.parent / "layout" / "blinky" / "fab").exists()


@pytest.mark.kicad
def test_a_pad_changed_after_kicads_save_is_refused(tmp_path: Path, monkeypatch):
    """The same after KiCad's refill-and-save: the saved board against KiCad's save of the placed board.
    The mutation rides on the uuid re-pin, the one pcbc rewrite after the save."""
    import pcbc.sexp as sx

    real = sx.pin_all_uuids
    monkeypatch.setattr(sx, "pin_all_uuids", lambda *a, **k: _shrink_r1_pad(real(*a, **k)))
    board = _copy("blinky", tmp_path)
    result = build_job(board, upto="fab", force=True)
    assert "the emitted board differs from the placed board outside the layout objects" in str(result.get("error")), result.get("error")
    assert not (board.parent / "layout" / "blinky" / "fab").exists()


# --- minors --------------------------------------------------------------------------------------------


def test_board_net_order_is_refused(tmp_path: Path):
    """`Board(net_order=)` was the old router's flag; refused at load, naming the fix."""
    from pcbc.language import load_board

    board = _copy("blinky", tmp_path)
    text = board.read_text()
    m = re.search(r"^Board\((.*)\)\s*$", text, flags=re.M)
    assert m
    board.write_text(text.replace(m.group(0), f"Board({m.group(1)}, net_order=['GND'])"))
    with pytest.raises(ValueError, match="net_order was the old router's flag"):
        load_board(board)
