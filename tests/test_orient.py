"""Orientation marks on the front silkscreen (docs/direction.md §4c): which parts need one, which pad it names, the
library's kept when it is good and generated where it is missing, a library mark that names another pin left out,
a required part with no legal spot a fact that fails the build, and the emitted board judged again."""

from __future__ import annotations

import re
import shutil
from pathlib import Path

import pytest

from pcbc.orient import DOT_MIN, MARGIN_MM, check_text, dist, need, part_of, plan, prim_ink, box_ink
from pcbc.pads import PadSpec
from pcbc.silk import pad_box, silk_part, silk_plan
from pcbc.stackup import get_stackup, silk_limits

LIMITS = silk_limits(get_stackup("jlcpcb_2l_1oz"))
EXAMPLES = Path(__file__).resolve().parent.parent / "examples"
LAYERS = '"F.Cu" "F.Paste" "F.Mask"'


def _pad(num: str, x: float, y: float, w: float, h: float, *, kind: str = "smd", shape: str = "rect", mask: float = 0.0) -> PadSpec:
    layers = LAYERS if kind == "smd" else '"*.Cu" "*.Mask"'
    return PadSpec(num, kind, shape, x, y, 0.0, w, h, layers=layers, mask_margin=mask)


def _led_pads():
    """KiCad's LED_0603: pad 1 (the cathode) on the left."""
    return [_pad("1", -0.7875, 0.0, 0.875, 0.95), _pad("2", 0.7875, 0.0, 0.875, 0.95)]


def _sot23_6():
    xs = (-0.95, 0.0, 0.95)
    return [_pad(str(i + 1), xs[i], 1.2, 0.6, 0.7) for i in range(3)] + [_pad(str(6 - i), xs[i], -1.2, 0.6, 0.7) for i in range(3)]


def _op(ref, at, pads, *, pins, body, kind="ic", prefix="U"):
    op = part_of(ref, at, pads, body=body, pins=pins, kind=kind, prefix=prefix)
    op["layer"] = "F.Cu"
    return op


LED_PINS = {"K": ("1",), "A": ("2",)}
IC_PINS = {"GND": ("1",), "SW": ("2",), "VIN": ("3",), "FB": ("4",), "EN": ("5",), "BOOT": ("6",)}


# ------------------------------------------------------------------ G1: which parts need a mark, and where


def test_an_led_needs_a_cathode_bar_at_its_k_pin():
    n = need(_op("D1", (0, 0, 0), _led_pads(), pins=LED_PINS, body=(-0.8, -0.4, 0.8, 0.4), kind="led", prefix="D"))
    assert (n["need"], n["shape"], n["pad"], n["pin"]) == ("required", "bar", "1", "K")
    assert n["wrong"] == [(180, [("1", "K", "2", "A"), ("2", "A", "1", "K")])]


def test_the_cathode_is_found_by_its_pin_name_not_by_pad_1():
    n = need(_op("D2", (0, 0, 0), _led_pads(), pins={"A": ("1",), "K": ("2",)}, body=(-0.8, -0.4, 0.8, 0.4), kind="led", prefix="D"))
    assert (n["shape"], n["pad"]) == ("bar", "2")


def test_a_resistor_and_a_plain_header_need_none_but_a_keyed_housing_does():
    r = _op("R1", (0, 0, 0), _led_pads(), pins={"1": ("1",), "2": ("2",)}, body=(-0.8, -0.4, 0.8, 0.4), kind="generic", prefix="R")
    assert need(r)["need"] == "none"
    hdr = [_pad(str(i + 1), -2.54 + 2.54 * i, 0.0, 1.7, 1.7, kind="thru_hole", shape="circle") for i in range(3)]
    plain = _op("J1", (0, 0, 0), hdr, pins={str(i + 1): (str(i + 1),) for i in range(3)}, body=(-3.81, -1.27, 3.81, 1.27), kind="th", prefix="J")
    assert need(plain)["need"] == "none"
    # The same pads in a housing deeper on one side (JST XH): turned, it is not the same part.
    keyed = _op("J2", (0, 0, 0), hdr, pins={str(i + 1): (str(i + 1),) for i in range(3)}, body=(-4.9, -3.4, 4.9, 2.4), kind="th", prefix="J")
    n = need(keyed)
    assert (n["need"], n["shape"], n["pad"]) == ("required", "dot", "1")


def test_an_ic_whose_pads_fit_turned_needs_a_dot_one_that_fits_one_way_gets_an_optional_one():
    ic = _op("U1", (0, 0, 0), _sot23_6(), pins=IC_PINS, body=(-1.45, -0.8, 1.45, 0.8))
    n = need(ic)
    assert (n["need"], n["shape"], n["pad"]) == ("required", "dot", "1") and n["wrong"][0][0] == 180
    sot3 = [_pad("1", -0.95, 1.1, 0.6, 0.7), _pad("2", 0.95, 1.1, 0.6, 0.7), _pad("3", 0.0, -1.1, 0.6, 0.7)]
    q = _op("Q1", (0, 0, 0), sot3, pins={"G": ("1",), "S": ("2",), "D": ("3",)}, body=(-1.45, -0.65, 1.45, 0.65), prefix="Q")
    assert need(q)["need"] == "optional"
    # A connector whose pads fit one way only needs none (the USB-C).
    j = _op("J1", (0, 0, 0), sot3, pins={"A": ("1",), "B": ("2",), "C": ("3",)}, body=(-1.45, -0.65, 1.45, 0.65), prefix="J")
    assert need(j)["need"] == "none"


# ------------------------------------------------------------------ the plan


def _plan(*parts_and_silk, board=(0.0, 0.0, 30.0, 30.0)):
    """`silk_plan` over (op, library world shapes) pairs, as the place stage runs it."""
    parts = []
    for op, shapes in parts_and_silk:
        pads = [b for b in (pad_box(sp, op["at"]) for sp in op["_specs"]) if b is not None]
        parts.append(silk_part(op["ref"], op["at"], op["body"], shapes, pads, orient=op))
    return silk_plan(parts, board, LIMITS)


def _with(op, specs):
    op["_specs"] = specs
    return op


def _circle(cx, cy, r, w=0.3, fill=False, uid="c"):
    import math

    pts = tuple((cx + r * math.cos(2 * math.pi * k / 32), cy + r * math.sin(2 * math.pi * k / 32)) for k in range(32))
    return {"tag": "fp_circle", "index": 0, "width": w, "fill": fill, "pts": pts, "closed": True, "span": (0, 0), "uuid": uid}


def test_an_led_gets_one_cathode_bar_past_its_cathode_clear_of_every_opening():
    specs = _led_pads()
    op = _with(_op("D1", (10.0, 10.0, 0.0), specs, pins=LED_PINS, body=(-0.8, -0.4, 0.8, 0.4), kind="led", prefix="D"), specs)
    decisions, report = _plan((op, []))
    assert report["orient_errors"] == []
    (m,) = report["orient_marks"]
    assert (m["ref"], m["pad"], m["shape"], m["prim"]["kind"]) == ("D1", "1", "bar", "gr_poly") and m["prim"]["fill"] is True
    ink = prim_ink(m["prim"])
    xs = [p[0] for p in ink["pts"]]
    cathode, anode = (pad_box(sp, op["at"]) for sp in specs)
    # Past the cathode's outer end (left), across the pad's whole width, clear of both openings.
    assert max(xs) + ink["hw"] < cathode[0]
    assert dist(ink, box_ink(cathode)) >= LIMITS["clearance_mm"] - 1e-6
    assert dist(ink, box_ink(anode)) - dist(ink, box_ink(cathode)) >= MARGIN_MM
    ys = [p[1] for p in ink["pts"]]
    assert min(ys) - ink["hw"] <= cathode[1] and max(ys) + ink["hw"] >= cathode[3]
    assert [r["verdict"] for r in report["orient"]] == ["generated"]


def test_a_good_library_dot_is_kept_and_nothing_is_generated():
    specs = _sot23_6()
    # EasyEDA's outside dot: past pin 1 along the row, 0.15 mm from its pad.
    lib = _circle(-0.95 - 0.3 - 0.15 - 0.3, 1.2, 0.15, w=0.3)  # the footprint's own frame
    op = _with(_op("U1", (10.0, 10.0, 0.0), specs, pins=IC_PINS, body=(-1.45, -0.8, 1.45, 0.8)), specs)
    _dec, report = _plan((op, [lib]))
    assert report["orient_marks"] == [] and [r["verdict"] for r in report["orient"]] == ["kept"], report["orient"]


def test_a_mark_under_the_body_is_not_the_parts_mark():
    """G4: a dot under the package by pin 1 is hidden once the part is placed; it stays printed (it names pin 1) but a
    visible one is generated."""
    specs = _sot23_6()
    lib = _circle(-0.95, 0.45, 0.15, w=0.3)
    op = _with(_op("U1", (10.0, 10.0, 0.0), specs, pins=IC_PINS, body=(-1.45, -0.8, 1.45, 0.8)), specs)
    decisions, report = _plan((op, [lib]))
    assert decisions["U1"]["trim"].get(0, {}).get("pieces") != [] and [r["verdict"] for r in report["orient"]] == ["generated"], report["orient"]
    ink = prim_ink(report["orient_marks"][0]["prim"])
    body = (10.0 - 1.45, 10.0 - 0.8, 10.0 + 1.45, 10.0 + 0.8)
    assert dist(ink, box_ink(body)) >= 0.0


def test_a_dot_on_the_axis_between_the_rows_names_no_pin_and_is_left_out():
    """G5, ambiguity 9: a dot past the end of the part on the axis between its rows is as near pin 6 as pin 1. A dot says
    only "the pin nearest me", so one that names no pin is not printed (the fact says so: node's BME280 vent dot, ds2's
    under-body dimple dot), and a dot that names pin 1 is generated."""
    specs = _sot23_6()
    lib = _circle(-1.95, 0.0, 0.15, w=0.3)
    op = _with(_op("U1", (10.0, 10.0, 0.0), specs, pins=IC_PINS, body=(-1.45, -0.8, 1.45, 0.8)), specs)
    decisions, report = _plan((op, [lib]))
    assert decisions["U1"]["trim"][0]["pieces"] == [] and [r["verdict"] for r in report["orient"]] == ["generated"], report["orient"]
    fact = next(n for n in report["notes"] if "silkscreen mark not printed" in n)
    assert fact == "U1: silkscreen mark not printed: the library's circle 0.60 mm across (line 0.3 mm) mark names no pin: pads 1 and 6 are 0.80 and 0.80 mm from it (G5: 0.2 mm nearer one)", fact


def test_a_library_mark_naming_another_pin_is_left_out_with_its_fact_and_a_dot_is_generated_at_pin_1():
    """node's BME280: the library's only outside dot sits at pad 5's corner."""
    specs = _sot23_6()
    lib = _circle(0.95 + 0.3 + 0.15 + 0.3, -1.2, 0.15, w=0.3, uid="bad")  # beside pad 4, the footprint's own frame
    op = _with(_op("U1", (10.0, 10.0, 0.0), specs, pins=IC_PINS, body=(-1.45, -0.8, 1.45, 0.8)), specs)
    decisions, report = _plan((op, [lib]))
    assert decisions["U1"]["trim"][0]["pieces"] == []
    fact = next(n for n in report["notes"] if "silkscreen mark not printed" in n)
    assert fact.startswith("U1: silkscreen mark not printed: the library's circle 0.60 mm across (line 0.3 mm) mark is nearest pad 4") and "pin 1 is pad 1" in fact
    (m,) = report["orient_marks"]
    assert m["pad"] == "1"
    ink = prim_ink(m["prim"])
    pads = {sp.num: pad_box(sp, op["at"]) for sp in specs}
    d = sorted((dist(ink, box_ink(b)), num) for num, b in pads.items())
    assert d[0][1] == "1" and d[1][0] - d[0][0] >= MARGIN_MM - 1e-9
    assert 2 * ink["disc"][2] >= DOT_MIN - 1e-9


def test_no_legal_spot_is_a_fact_naming_the_part_the_pad_and_what_blocked_every_spot():
    specs = _led_pads()
    op = _with(_op("D1", (10.0, 10.0, 0.0), specs, pins=LED_PINS, body=(-0.8, -0.4, 0.8, 0.4), kind="led", prefix="D"), specs)
    # A neighbour's pads hard against the cathode end, and the board edge 0.3 mm beyond them.
    wall = [_pad(str(i + 1), 0.0, -1.0 + 0.5 * i, 0.4, 0.4) for i in range(5)]
    nb = _with(_op("R9", (8.55, 10.0, 0.0), wall, pins={str(i + 1): (str(i + 1),) for i in range(5)}, body=None, kind="generic", prefix="R"), wall)
    _dec, report = _plan((op, []), (nb, []), board=(8.05, 0.0, 30.0, 30.0))
    (err,) = report["orient_errors"]
    assert err.startswith("D1: no orientation mark: its pads fit turned 180 degrees pad 1 (K) lands on pad 2 (A)")
    assert "so the fab needs a cathode bar at pad 1 (K)" in err and "R9." in err and "spots" in err
    assert "should" not in err and "try" not in err  # docs/direction.md §7b: facts, no proposal
    assert report["orient_marks"] == []


def test_a_core_drawing_that_is_a_good_mark_is_the_parts_mark():
    specs = _sot23_6()
    op = _with(_op("U1", (10.0, 10.0, 0.0), specs, pins=IC_PINS, body=(-1.45, -0.8, 1.45, 0.8)), specs)
    core = _circle(10.0 - 0.95 - 0.3 - 0.2 - 0.25, 10.0 + 1.2, 0.175, w=0.15, fill=True, uid="core")
    core["bbox"] = (core["pts"][16][0] - 0.075, 10.0 + 1.2 - 0.25, core["pts"][0][0] + 0.075, 10.0 + 1.2 + 0.25)
    parts = [silk_part("U1", op["at"], op["body"], [], [pad_box(sp, op["at"]) for sp in specs], orient=op)]
    _dec, report = silk_plan(parts, (0.0, 0.0, 30.0, 30.0), LIMITS, [("layout.core.py:3", core)])
    assert report["orient_marks"] == [] and report["orient"][0]["mark"].startswith("layout.core.py:3")


def test_a_core_drawing_naming_another_pin_fails():
    specs = _sot23_6()
    op = _with(_op("U1", (10.0, 10.0, 0.0), specs, pins=IC_PINS, body=(-1.45, -0.8, 1.45, 0.8)), specs)
    core = _circle(10.0 + 0.95 + 0.3 + 0.2 + 0.25, 10.0 - 1.2, 0.175, w=0.15, fill=True, uid="core")
    core["bbox"] = (0, 0, 0, 0)
    parts = [silk_part("U1", op["at"], op["body"], [], [pad_box(sp, op["at"]) for sp in specs], orient=op)]
    _dec, report = silk_plan(parts, (0.0, 0.0, 30.0, 30.0), LIMITS, [("layout.core.py:7", core)])
    assert any(e.startswith("layout.core.py:7: a ") and "is nearest its pad 4" in e for e in report["orient_errors"]), report["orient_errors"]


def test_the_plan_reads_no_board_text_and_is_deterministic():
    specs = _led_pads()
    op = _with(_op("D1", (10.0, 10.0, 90.0), specs, pins=LED_PINS, body=(-0.8, -0.4, 0.8, 0.4), kind="led", prefix="D"), specs)
    a = _plan((op, []))[1]["orient_marks"]
    b = _plan((op, []))[1]["orient_marks"]
    assert a == b and len(a) == 1
    # Turned 90 degrees, the bar turns with the part: past the cathode, which now sits below... or above the anode.
    ink = prim_ink(a[0]["prim"])
    cath = pad_box(specs[0], op["at"])
    assert dist(ink, box_ink(cath)) < dist(ink, box_ink(pad_box(specs[1], op["at"])))


# ------------------------------------------------------------------ the examples, placed


def _place(tmp_path: Path, name: str):
    from pcbc.language import load_board
    from pcbc.place_native import place

    dst = tmp_path / name
    shutil.copytree(EXAMPLES / name, dst, ignore=shutil.ignore_patterns("layout"))
    design = load_board(dst / f"{name}.py")
    return design, place(design, name=name)


def test_node_bme280_gets_its_pin_1_dot_and_loses_the_librarys_pad_5_dot(tmp_path: Path):
    from pcbc.place_native import emitted_text

    design, pl = _place(tmp_path, "node")
    assert pl.error is None, pl.error
    marks = {g.f["uuid"]: g for g in pl.marks}
    u4 = [g for g in pl.marks if g.kind == "gr_circle" and _owner(pl, g) == ("U4", "1")]
    assert len(u4) == 1, [(g.kind, _owner(pl, g)) for g in marks.values()]
    u4 = [n for n in pl.notes if n.startswith("U4: silkscreen mark not printed: the library's circle")]
    # The library's only outside dot (at pad 5's corner) and the 0.60 mm vent dot on the pin 4/5 edge both sit nearer a
    # pull-up's pad than any of U4's: each names another part's pin, so neither prints (refute:fab 2026-10-08).
    assert any("0.21 mm across" in n and "U4.5" in n and "R_SCL" in n and "another part's pin" in n for n in u4), u4
    assert any("0.60 mm across" in n and "R_SDA" in n and "another part's pin" in n for n in u4), u4
    # The emitted (place-only) board judged again: every part that needs a mark has one, none names another pin.
    got = check_text(emitted_text(design, pl, name="node"), design, LIMITS, board=pl.job.outline)
    assert got["fails"] == [], got["fails"]
    verdicts = {r["ref"]: r["verdict"] for r in got["rows"]}
    for ref in ("D1", "U1", "U3", "U4", "J_LOAD"):
        assert verdicts[ref] == "printed", (ref, got["rows"])


def _owner(pl, g):
    """The (part, pad) nearest a generated mark."""
    from pcbc.orient import prim_ink as pi
    from pcbc.silk import pad_box as pb

    prim = {"kind": g.kind, **g.f}
    ink = pi(prim)
    best = None
    for pf in pl.feet:
        at = (pf.at[0], pf.at[1], pf.rot)
        for sp in pf.lib.pads:
            b = pb(sp, at)
            if b is None or not sp.num:
                continue
            d = dist(ink, box_ink(b))
            if best is None or d < best[0]:
                best = (d, pf.ref, sp.num)
    return best[1:]


@pytest.mark.parametrize("name, need_marks", [("blinky", {"D1"}), ("buck", {"J_IN", "J_OUT", "U1"}), ("c3_usb", {"D1", "U1", "U3"})])
def test_every_required_part_of_the_examples_is_marked_on_the_placed_board(tmp_path: Path, name: str, need_marks: set):
    from pcbc.place_native import emitted_text

    design, pl = _place(tmp_path, name)
    assert pl.error is None, pl.error
    got = check_text(emitted_text(design, pl, name=name), design, LIMITS, board=pl.job.outline)
    assert got["fails"] == [], got["fails"]
    required = {r["ref"] for r in got["rows"] if r["need"] == "required"}
    assert required == need_marks and all(r["verdict"] == "printed" for r in got["rows"] if r["need"] == "required"), got["rows"]


# ------------------------------------------------------------------ the emitted board, judged (mutations)


@pytest.fixture(scope="module")
def blinky_built(tmp_path_factory):
    from pcbc.build import build_job

    work = tmp_path_factory.mktemp("orient_blinky")
    dst = work / "blinky"
    shutil.copytree(EXAMPLES / "blinky", dst, ignore=shutil.ignore_patterns("layout"))
    res = build_job(dst / "blinky.py", force=True)
    return dst, res


@pytest.mark.kicad
def test_the_generated_bar_is_a_gen_line_and_the_built_board_passes_the_gate(blinky_built):
    dst, res = blinky_built
    route = next(s for s in res["steps"] if s.get("stage") == "route")
    assert route["orientation"]["fails"] == [] and route["orientation"]["rows"][0]["ref"] == "D1"
    assert route["orientation"]["silk_mask"] == []
    gen = (dst / "layout" / "blinky" / "routed" / "layout.gen.py").read_text()
    lines = [ln for ln in gen.splitlines() if ln.startswith("Poly(") and 'layer="F.SilkS"' in ln]
    assert len(lines) == 1 and "fill=True" in lines[0]
    notes = (dst / "layout" / "blinky" / "fab" / "FAB_NOTES.md").read_text()
    assert "### Orientation marks" in notes and "- D1: orientation marked (required)" in notes
    assert "- All silkscreen ink, marks and text, is the fab's clearance or more from every mask opening" in notes


@pytest.mark.kicad
def test_fab_registers_blinkys_led_on_jlcs_footprint(blinky_built):
    """docs/direction.md §4c: the CPL gives each part's rotation in JLC's own footprint's frame. blinky's LED is KiCad's
    LED_0603; JLC's for C2286 (fetched into the checkout's components/) has its cathode on pad 2, and that lands on
    KiCad's cathode at the same rotation. R1, its own turned self, keeps KiCad's rotation, with the fact in the notes."""
    dst, res = blinky_built
    assert res.get("error") is None, res.get("error")
    notes = (dst / "layout" / "blinky" / "fab" / "FAB_NOTES.md").read_text()
    assert "## Placement (CPL)" in notes
    assert "D1 (C2286):" in notes and "on JLC's pad 2" in notes and "turned 0 degrees" in notes
    assert "- R1 (C21190): CPL rotation not registered: no JLC footprint for C21190 on disk" in notes and "turned 180 degrees the part is itself" in notes
    assert any(line.startswith("D1,") for line in (dst / "layout" / "blinky" / "fab" / "cpl.csv").read_text().splitlines())


@pytest.mark.kicad
def test_a_mark_moved_to_the_anode_or_deleted_fails_the_emitted_board_check(blinky_built):
    from pcbc.language import load_board

    dst, res = blinky_built
    text = (dst / "layout" / "blinky" / "routed" / "layout.kicad_pcb").read_text()
    design = load_board(dst / "blinky.py")
    assert check_text(text, design, LIMITS)["fails"] == []
    j = text.index("(gr_poly")
    end = j
    depth = 0
    for k in range(j, len(text)):
        depth += {"(": 1, ")": -1}.get(text[k], 0)
        if depth == 0:
            end = k + 1
            break
    poly = text[j:end]
    gone = check_text(text[:j] + text[end:], design, LIMITS)["fails"]
    assert len(gone) == 1 and gone[0].startswith("D1: no orientation mark printed: no cathode mark at pad 1"), gone
    # Mirrored about the part's centre: the same bar, past the anode.
    from pcbc.sexp import board_footprint_spans, footprint_at, footprint_reference

    cx = next(footprint_at(text[a:b])[0] for a, b in board_footprint_spans(text) if footprint_reference(text[a:b]) == "D1")
    moved = re.sub(r"\(xy ([-0-9.]+) ([-0-9.]+)\)", lambda q: f"(xy {2 * cx - float(q.group(1)):.4f} {q.group(2)})", poly)
    fails = check_text(text[:j] + moved + text[end:], design, LIMITS)["fails"]
    assert any("is nearest its pad 2" in f for f in fails), fails


@pytest.mark.kicad
def test_a_core_mark_naming_the_anode_fails_the_build(tmp_path: Path):
    from pcbc.build import build_job

    dst = tmp_path / "blinky"
    shutil.copytree(EXAMPLES / "blinky", dst, ignore=shutil.ignore_patterns("layout"))
    first = build_job(dst / "blinky.py", upto="route", force=True)
    assert first.get("error") is None, first.get("error")
    gen = (dst / "layout" / "blinky" / "routed" / "layout.gen.py").read_text()
    line = next(ln for ln in gen.splitlines() if ln.startswith("Poly(") and 'layer="F.SilkS"' in ln)
    # The gen line copied verbatim into layout.core.py is a lock: the build keeps it and generates no other.
    core = dst / "layout.core.py"
    core.write_text(core.read_text() + "\n" + line + "\n")
    again = build_job(dst / "blinky.py", upto="route", force=True)
    assert again.get("error") is None, again.get("error")
    gen2 = (dst / "layout" / "blinky" / "routed" / "layout.gen.py").read_text()
    assert not [ln for ln in gen2.splitlines() if ln.startswith("Poly(") and 'layer="F.SilkS"' in ln]
    # The same line moved past the anode names the wrong pin: refused, naming the line.
    pts = [tuple(map(float, p)) for p in re.findall(r"\(([-0-9.]+), ([-0-9.]+)\)", line.split("width=")[0])]
    cx = sum(p[0] for p in pts) / len(pts)
    d1 = next(ln for ln in gen.splitlines() if ln.startswith('Place("D1"'))
    px = float(re.search(r"at=\(([-0-9.]+),", d1).group(1))
    flipped = re.sub(r"\(([-0-9.]+), ([-0-9.]+)\)", lambda q: f"({2 * px - float(q.group(1)):.4f}, {q.group(2)})", line.split("width=")[0]) + "width=" + line.split("width=", 1)[1]
    assert abs(2 * px - cx - cx) > 1.0
    core.write_text(core.read_text().replace(line, flipped))
    bad = build_job(dst / "blinky.py", upto="route", force=True)
    assert not bad.get("ok")
    assert "orientation: layout.core.py:" in bad["error"] and "of D1 is nearest its pad 2" in bad["error"], bad["error"]


@pytest.mark.kicad
def test_the_route_gate_judges_the_emitted_board_not_the_plan(tmp_path: Path, monkeypatch):
    """An emitter that loses the generated mark (the plan believed it placed) is caught on the emitted board."""
    import pcbc.place_native as pn
    from pcbc.build import build_job

    dst = tmp_path / "blinky"
    shutil.copytree(EXAMPLES / "blinky", dst, ignore=shutil.ignore_patterns("layout"))
    monkeypatch.setattr(pn, "mark_graphics", lambda marks, name: [])
    res = build_job(dst / "blinky.py", upto="route", force=True)
    assert not res.get("ok")
    assert res["error"].startswith("a part's orientation is not marked on the silkscreen: D1: no orientation mark printed: no cathode mark at pad 1"), res["error"]


# ------------------------------------------------------------------ round 2: G2-G5 on a layout.core.py mark, each rule by itself


def _dot(cx: float, cy: float, D: float):
    """A filled board dot `D` across (a `layout.core.py` Circle on F.SilkS, 0.15 mm stroke), as a world silk shape."""
    import math

    from pcbc.orient import graphic_shape

    r = D / 2 - 0.075
    pts = tuple((cx + r * math.cos(2 * math.pi * k / 32), cy + r * math.sin(2 * math.pi * k / 32)) for k in range(32))
    return graphic_shape("fp_circle", pts, 0.15, True)


def _u1_with_core(core, board=(0.0, 0.0, 30.0, 30.0), extra=()):
    """U1 (SOT-23-6 at 10, 10: pad 1's opening x 8.75-9.35, y 10.85-11.55) with one layout.core.py drawing."""
    specs = _sot23_6()
    op = _with(_op("U1", (10.0, 10.0, 0.0), specs, pins=IC_PINS, body=(-1.45, -0.8, 1.45, 0.8)), specs)
    parts = [silk_part("U1", op["at"], op["body"], [], [pad_box(sp, op["at"]) for sp in specs], orient=op)]
    for o in extra:
        parts.append(silk_part(o["ref"], o["at"], o["body"], [], [pad_box(sp, o["at"]) for sp in o["_specs"]], orient=o))
    return silk_plan(parts, board, LIMITS, [("layout.core.py:3", core)])[1]


def test_a_good_core_dot_0_2_mm_past_pin_1_is_the_parts_mark():
    report = _u1_with_core(_dot(8.75 - 0.2 - 0.25, 11.2, 0.5))
    assert report["orient_errors"] == [] and report["orient_marks"] == []
    assert report["orient"][0]["verdict"] == "kept" and report["orient"][0]["mark"].startswith("layout.core.py:3")


@pytest.mark.parametrize(
    "where, rule",
    [
        # 0.05 mm from pad 1's opening: G3, the fab's clearance to a mask opening.
        ((8.75 - 0.05 - 0.25, 11.2, 0.5, (0.0, 0.0, 30.0, 30.0)), "0.05 mm from U1.1's opening (G3: 0.15 mm)"),
        # 0.36 mm across: G2 (0.40 mm), which a 0.30 mm floor would let by.
        ((8.75 - 0.2 - 0.18, 11.2, 0.36, (0.0, 0.0, 30.0, 30.0)), "0.36 mm across (G2: 0.15 mm strokes, 0.4 mm across)"),
        # inside the board edge by less than the fab's clearance: G3.
        ((8.75 - 0.2 - 0.25, 11.2, 0.5, (8.0, 0.0, 30.0, 30.0)), "within 0.15 mm of the board edge (G3)"),
        # 1.5 mm from pad 1: G5's reach (1.0 mm), which a 3.0 mm reach would let by.
        ((8.75 - 1.5 - 0.25, 11.2, 0.5, (0.0, 0.0, 30.0, 30.0)), "1.50 mm from U1.1's opening (G5: 1 mm)"),
    ],
)
def test_a_core_mark_that_names_pin_1_but_breaks_a_rule_fails_naming_the_rule_and_nothing_is_generated(where, rule):
    """refute-rules 2026-10-08: such a line was ignored in silence — a second mark generated beside it, or an id clash. Now
    the build fails on the rule the line breaks, and no mark is generated next to it."""
    cx, cy, D, board = where
    report = _u1_with_core(_dot(cx, cy, D), board=board)
    (err,) = report["orient_errors"]
    assert err.startswith("layout.core.py:3: a filled circle") and "mark of U1 at pad 1 (pin 1) breaks" in err and rule in err, err
    assert report["orient_marks"] == [] and report["orient"][0]["verdict"] == "failed"


def _xh(ref, at):
    from pcbc.pads import pad_specs

    mod = (EXAMPLES / "buck" / "components" / "JST" / "B2B-XH-A" / "CONN-TH_B2B-XH-A-LF-SN.kicad_mod").read_text()
    from pcbc.orient import body_box

    specs = list(pad_specs(mod))
    return _with(_op(ref, at, specs, pins={"1": ("1",), "2": ("2",)}, body=body_box(mod), kind="th", prefix="J"), specs)


def test_a_mark_by_a_housing_belongs_to_it_however_far_its_pad_is():
    """A JST XH's pins sit under its housing, so its mark is read from the housing's edge (G5). A dot 0.43 mm off the
    housing's corner is 3.65 mm from pad 1's opening and 1.9 mm from a resistor's: it is the XH's (refute-rules: a 3 mm
    reach made a moved, locked J_IN dot no one's, and a second dot was generated or the ids clashed)."""
    j = _xh("J9", (20.0, 20.0, 0.0))
    r = _with(_op("R9", (24.275, 13.3, 0.0), _led_pads(), pins={"1": ("1",), "2": ("2",)}, body=(-0.8, -0.4, 0.8, 0.4), kind="generic", prefix="R"), _led_pads())
    parts = [silk_part(o["ref"], o["at"], o["body"], [], [pad_box(sp, o["at"]) for sp in o["_specs"]], orient=o) for o in (j, r)]
    dot = _dot(24.275, 16.025, 0.7)
    report = silk_plan(parts, (0.0, 0.0, 40.0, 40.0), LIMITS, [("layout.core.py:9", dot)])[1]
    assert report["orient_errors"] == [] and report["orient_marks"] == [], (report["orient_errors"], report["orient_marks"])
    (row,) = report["orient"]
    assert row["ref"] == "J9" and row["verdict"] == "kept" and row["mark"].startswith("layout.core.py:9"), row
    # Measured: the dot's own pad is far; it is the housing it sits by.
    ink = prim_ink({"kind": "gr_circle", "center": (24.275, 16.025), "end": (24.275 + 0.275, 16.025), "width": 0.15})
    assert dist(ink, box_ink(pad_box(j["_specs"][0], j["at"]))) > 3.0


def test_a_dot_by_a_resistor_is_not_an_leds_mark():
    """refute-rules 2026-10-08: a 0.40 mm core dot 0.33 mm from R1's pad and 1.6 mm from D1 was judged D1's mark naming its
    anode and failed the build with a false fact. It is R1's silk (R1 takes no mark): no error, and D1 gets its bar."""
    led = _with(_op("D1", (10.0, 10.0, 0.0), _led_pads(), pins=LED_PINS, body=(-0.8, -0.4, 0.8, 0.4), kind="led", prefix="D"), _led_pads())
    r1 = _with(_op("R1", (13.0, 10.0, 0.0), _led_pads(), pins={"1": ("1",), "2": ("2",)}, body=(-0.8, -0.4, 0.8, 0.4), kind="generic", prefix="R"), _led_pads())
    parts = [silk_part(o["ref"], o["at"], o["body"], [], [pad_box(sp, o["at"]) for sp in o["_specs"]], orient=o) for o in (led, r1)]
    report = silk_plan(parts, (0.0, 0.0, 30.0, 30.0), LIMITS, [("layout.core.py:5", _dot(13.0, 9.125, 0.4))])[1]
    assert report["orient_errors"] == [], report["orient_errors"]
    (m,) = report["orient_marks"]
    assert (m["ref"], m["pad"], m["shape"]) == ("D1", "1", "bar")


def test_a_library_dot_nearer_another_parts_pad_is_left_out_and_the_generated_dot_names_its_own_pin():
    """refute:fab 2026-10-08: dots that sat nearer a resistor's pad than their own pin 1. A dot says "the pin nearest me",
    read against every pad on the board."""
    specs = _sot23_6()
    lib = _circle(-0.95 - 0.3 - 0.15 - 0.3, 1.2, 0.15, w=0.3)  # EasyEDA's outside dot, 0.15 mm past pad 1
    op = _with(_op("U1", (10.0, 10.0, 0.0), specs, pins=IC_PINS, body=(-1.45, -0.8, 1.45, 0.8)), specs)
    r9 = _with(_op("R9", (7.325, 11.2, 90.0), _led_pads(), pins={"1": ("1",), "2": ("2",)}, body=(-0.8, -0.4, 0.8, 0.4), kind="generic", prefix="R"), _led_pads())
    decisions, report = _plan((op, [lib]), (r9, []))
    assert decisions["U1"]["trim"][0]["pieces"] == []
    fact = next(n for n in report["notes"] if n.startswith("U1: silkscreen mark not printed"))
    assert "mark names no part: R9.1 (" in fact and "as near as U1's nearest pad, U1.1 (" in fact and fact.endswith("(G5)"), fact
    (m,) = report["orient_marks"]
    ink = prim_ink(m["prim"])
    every = sorted((dist(ink, box_ink(pad_box(sp, o["at"]))), o["ref"], sp.num) for o in (op, r9) for sp in o["_specs"])
    assert every[0][1:] == ("U1", "1") and every[1][0] - every[0][0] >= MARGIN_MM - 1e-9, every[:3]


def test_a_dot_is_read_against_every_pad_but_a_chevron_only_against_the_parts_that_take_a_mark():
    """An ESP32 module's pin-1 chevron points at its pin; a decoupling capacitor 0.05 mm nearer it is not its pin 1. A
    dot in the same spot says nothing but "nearest me", and is refused."""
    from pcbc.orient import Scene, judge, need, poly_ink

    specs = _sot23_6()
    op = _op("U1", (10.0, 10.0, 0.0), specs, pins=IC_PINS, body=(-1.45, -0.8, 1.45, 0.8))
    c9 = [(("C9", "1"), (6.985, 10.85, 7.685, 11.55))]
    pads = [("U1", q["num"], q["box"]) for q in op["pads"]] + [(r, n, b) for (r, n), b in c9]
    sc = Scene(pads, [("U1", ((8.55, 9.2), (11.45, 9.2), (11.45, 10.8), (8.55, 10.8)))], [], (0, 0, 30, 30), LIMITS)
    sc.marked = {"U1"}
    n = need(op)
    tri = poly_ink(((7.9, 10.95), (7.9, 11.45), (8.52, 11.2)), 0.06)  # apex 0.17 mm from pad 1; C9 0.155 mm from its base
    dotk = prim_ink({"kind": "gr_circle", "center": (8.2, 11.2), "end": (8.2 + 0.225, 11.2), "width": 0.15})
    assert judge(tri, op, n, sc)["ok"], judge(tri, op, n, sc)["why"]
    j = judge(dotk, op, n, sc)
    assert not j["ok"] and any("C9.1" in w and "nearer its own part than any other part's pad" in w for w in j["why"]), j["why"]


def test_a_diode_by_its_prefix_needs_a_mark_whatever_its_pins_are_called():
    d = _op("D5", (0, 0, 0), _led_pads(), pins={"1": ("1",), "2": ("2",)}, body=(-0.8, -0.4, 0.8, 0.4), kind="generic", prefix="D")
    assert need(d)["need"] == "required"


def test_a_polarised_capacitor_gets_a_plus_at_its_plus_pad():
    specs = _led_pads()
    op = _with(_op("C7", (10.0, 10.0, 0.0), specs, pins={"+": ("2",), "-": ("1",)}, body=(-0.8, -0.4, 0.8, 0.4), kind="generic", prefix="C"), specs)
    n = need(op)
    assert (n["need"], n["shape"], n["pad"]) == ("required", "plus", "2")
    _dec, report = _plan((op, []))
    (m,) = report["orient_marks"]
    assert m["shape"] == "plus" and m["prim"]["kind"] == "gr_poly" and len(m["prim"]["pts"]) == 12
    ink = prim_ink(m["prim"])
    d = {sp.num: dist(ink, box_ink(pad_box(sp, op["at"]))) for sp in specs}
    assert d["2"] + MARGIN_MM <= d["1"] + 1e-9, d


def test_a_part_on_the_back_is_noted_not_judged():
    specs = _led_pads()
    op = _with(_op("D1", (10.0, 10.0, 0.0), specs, pins=LED_PINS, body=(-0.8, -0.4, 0.8, 0.4), kind="led", prefix="D"), specs)
    op["layer"] = "B.Cu"
    _dec, report = _plan((op, []))
    assert report["orient_marks"] == [] and report["orient_errors"] == [] and report["orient"] == []
    assert "D1: orientation mark not judged: the part is on the back, and pcbc prints no back silkscreen" in report["notes"]


def test_a_generated_mark_keeps_0_2_mm_from_every_opening_where_there_is_room():
    """G3: AIM_MM (0.20) first, the fab's 0.15 only where no spot keeps 0.20."""
    specs = _led_pads()
    op = _with(_op("D1", (10.0, 10.0, 0.0), specs, pins=LED_PINS, body=(-0.8, -0.4, 0.8, 0.4), kind="led", prefix="D"), specs)
    _dec, report = _plan((op, []))
    (row,) = report["orient"]
    assert row["clearance"] == 0.2
    ink = prim_ink(report["orient_marks"][0]["prim"])
    assert min(dist(ink, box_ink(pad_box(sp, op["at"]))) for sp in specs) >= 0.2 - 1e-6


# ------------------------------------------------------------------ round 2: silkscreen to mask, all of it


def test_every_silkscreen_drawing_and_text_is_held_to_the_fabs_clearance_from_mask_openings():
    """KiCad's DRC flags silk on a mask opening only where they overlap; JLC's 0.15 mm is this gate's (refute-rules)."""
    from pcbc.orient import graphic_shape, silk_mask

    pad = ("R1", "1", (10.0, 10.0, 11.0, 11.0))

    def scene(line_y=None, text=None):
        printed = []
        if line_y is not None:
            printed.append(("layout.core.py:4", None, graphic_shape("fp_line", ((9.0, line_y), (12.0, line_y)), 0.15, False)))
        return {"pads": [pad], "printed": printed, "texts": [] if text is None else [("R1's reference \"R1\"", text)]}

    assert silk_mask(scene(11.0 + 0.075 + 0.15), LIMITS) == []
    (f,) = silk_mask(scene(11.0 + 0.075 + 0.10), LIMITS)
    assert f == "layout.core.py:4: 0.100 mm from R1.1's mask opening (the fab's silk clearance 0.15 mm)", f
    (g,) = silk_mask(scene(text=(11.05, 10.2, 12.0, 10.8)), LIMITS)
    assert g.startswith("R1's reference \"R1\": 0.050 mm from R1.1's mask opening"), g
    # A pad's own mask margin widens its opening.
    assert silk_mask(scene(11.0 + 0.075 + 0.16), LIMITS, mask_extra=0.05) != []


@pytest.mark.kicad
def test_a_silk_line_nearer_a_pad_than_0_15_mm_fails_the_emitted_board(blinky_built):
    from pcbc.language import load_board
    from pcbc.sexp import board_footprint_spans, footprint_reference
    from pcbc.pads import pad_specs

    dst, _res = blinky_built
    text = (dst / "layout" / "blinky" / "routed" / "layout.kicad_pcb").read_text()
    design = load_board(dst / "blinky.py")
    assert check_text(text, design, LIMITS)["silk_mask"] == []
    r1 = next(text[a:b] for a, b in board_footprint_spans(text) if footprint_reference(text[a:b]) == "R1")
    from pcbc.sexp import footprint_at

    at = footprint_at(r1)
    box = pad_box(pad_specs(r1)[0], at, board_angle=True)
    y = box[3] + 0.075 + 0.10
    line = f'\t(gr_line\n\t\t(start {box[0]:.4f} {y:.4f})\n\t\t(end {box[2]:.4f} {y:.4f})\n\t\t(stroke\n\t\t\t(width 0.15)\n\t\t\t(type solid)\n\t\t)\n\t\t(layer "F.SilkS")\n\t\t(uuid "00000000-0000-4000-8000-0000000000aa")\n\t)\n'
    k = text.rindex(")")
    got = check_text(text[:k] + line + text[k:], design, LIMITS)["silk_mask"]
    assert len(got) == 1 and "0.100 mm from R1.1's mask opening" in got[0], got


# ------------------------------------------------------------------ round 2: the build refuses a required part with no legal spot


def _noroom(tmp_path: Path) -> Path:
    """blinky with R1 stood hard against D1's cathode end (refute-rules b/noroom): no spot for D1's bar."""
    dst = tmp_path / "blinky"
    shutil.copytree(EXAMPLES / "blinky", dst, ignore=shutil.ignore_patterns("layout"))
    (dst / "layout.core.py").write_text(
        'Place("D1", at=(11.54, 12.5), rot=180, locked=True, reason="LED")\n'
        'Place("R1", at=(13.5, 12.5), rot=90, locked=True, reason="resistor against the LED\'s cathode end")\n'
    )
    return dst / "blinky.py"


@pytest.mark.kicad
def test_the_place_stage_refuses_a_required_part_with_no_legal_spot(tmp_path: Path):
    from pcbc.build import build_job

    res = build_job(_noroom(tmp_path), upto="place", force=True)
    assert res.get("error", "").startswith("orientation: D1: no orientation mark: its pads fit turned 180 degrees pad 1 (K) lands on pad 2 (A)"), res.get("error")
    assert "R1.1" in res["error"]


def test_the_layout_job_refuses_a_required_part_with_no_legal_spot(tmp_path: Path):
    from pcbc.build import pcb_job

    res = pcb_job(_noroom(tmp_path))
    assert (res.get("error") or "").startswith("orientation: D1: no orientation mark"), res.get("error")


@pytest.mark.kicad
def test_buck_j_ins_generated_dot_locked_and_moved_stays_its_mark(tmp_path: Path):
    """refute-rules 2026-10-08 (b/bucklock): J_IN's generated dot copied into layout.core.py and moved 0.2 mm sat 3 mm
    from its pad, outside a 3 mm ownership reach: a second dot was generated and the full build failed on an id clash.
    Measured by the housing's edge it is J_IN's: kept, and nothing is generated beside it."""
    from pcbc.build import build_job

    dst = tmp_path / "buck"
    shutil.copytree(EXAMPLES / "buck", dst, ignore=shutil.ignore_patterns("layout"))
    first = build_job(dst / "buck.py", upto="place", force=True)
    assert first.get("error") is None, first.get("error")
    gen = (dst / "layout" / "buck" / "placed" / "layout.gen.py").read_text()
    from pcbc.sexp import stable_uuid

    uid = stable_uuid("buck", "orient", "J_IN", "1")
    line = next(ln for ln in gen.splitlines() if ln.startswith("Circle(") and uid in ln)
    x, y = map(float, re.match(r"Circle\(\(([-0-9.]+), ([-0-9.]+)\)", line).groups())
    moved = line.replace(f"(({x:g}, {y:g})", f"(({x:g}, {y + 0.2:g})").replace(f", {y:g}), width", f", {y + 0.2:g}), width")
    assert moved != line and "Circle((" in moved
    core = dst / "layout.core.py"
    core.write_text(core.read_text() + "\n" + moved + "\n")
    again = build_job(dst / "buck.py", upto="place", force=True)
    assert again.get("error") is None, again.get("error")
    gen2 = (dst / "layout" / "buck" / "placed" / "layout.gen.py").read_text()
    assert uid not in gen2 and sum(ln.startswith("Circle(") and 'layer="F.SilkS"' in ln for ln in gen2.splitlines()) == 2
    place = next(s for s in again["steps"] if s.get("stage") == "place")
    rows = [ln for ln in (place.get("applied") or {}).get("orientation", []) if ln.startswith("J_IN")]
    assert rows and rows[0].startswith("J_IN: orientation mark kept (required): layout.core.py:"), rows


def _r1_pad_line(text: str, gap: float):
    """(y, x0, x1) of a 0.15 mm silk line `gap` mm below R1's pad 1 opening on a board text."""
    from pcbc.pads import pad_specs
    from pcbc.sexp import board_footprint_spans, footprint_at, footprint_reference

    r1 = next(text[a:b] for a, b in board_footprint_spans(text) if footprint_reference(text[a:b]) == "R1")
    box = pad_box(pad_specs(r1)[0], footprint_at(r1), board_angle=True)
    return box[3] + 0.075 + gap, box[0], box[2]


@pytest.mark.kicad
def test_the_route_gate_refuses_a_core_silk_line_nearer_a_pad_than_the_fab_prints(tmp_path: Path, blinky_built):
    """KiCad's DRC passes a silk line 0.10 mm from a mask opening (it flags only an overlap); the route gate does not."""
    from pcbc.build import build_job

    src, _res = blinky_built
    y, x0, x1 = _r1_pad_line((src / "layout" / "blinky" / "routed" / "layout.kicad_pcb").read_text(), 0.10)
    dst = tmp_path / "blinky"
    shutil.copytree(EXAMPLES / "blinky", dst, ignore=shutil.ignore_patterns("layout"))
    core = dst / "layout.core.py"
    core.write_text(core.read_text() + f'\nLine(({x0:.4f}, {y:.4f}), ({x1:.4f}, {y:.4f}), width=0.15, layer="F.SilkS", uuid="00000000-0000-4000-8000-0000000000ab")\n')
    res = build_job(dst / "blinky.py", upto="route", force=True)
    assert (res.get("error") or "").startswith("the silkscreen is nearer a pad's mask opening than the fab prints: "), res.get("error")
    assert "0.100 mm from R1.1's mask opening (the fab's silk clearance 0.15 mm)" in res["error"]


@pytest.mark.kicad
def test_fab_measures_the_silkscreen_to_mask_on_the_board_it_sends(tmp_path: Path, blinky_built):
    """The fab board is the routed board plus fiducials, so fab measures it again (an inserted fiducial's opening too)."""
    from pcbc.compile import compile_design
    from pcbc.fab import fab_job
    from pcbc.language import load_board

    src, _res = blinky_built
    routed = src / "layout" / "blinky" / "routed" / "layout.kicad_pcb"
    text = routed.read_text()
    y, x0, x1 = _r1_pad_line(text, 0.10)
    line = f'\t(gr_line\n\t\t(start {x0:.4f} {y:.4f})\n\t\t(end {x1:.4f} {y:.4f})\n\t\t(stroke\n\t\t\t(width 0.15)\n\t\t\t(type solid)\n\t\t)\n\t\t(layer "F.SilkS")\n\t\t(uuid "00000000-0000-4000-8000-0000000000ac")\n\t)\n'
    work = tmp_path / "routed"
    work.mkdir()
    k = text.rindex(")")
    (work / "layout.kicad_pcb").write_text(text[:k] + line + text[k:])
    design = load_board(src / "blinky.py")
    res = fab_job(compile_design(design), work / "layout.kicad_pcb", out_dir=tmp_path / "fab", design=design)
    assert (res.get("error") or "").startswith("the silkscreen is nearer a pad's mask opening than the fab prints: board drawing"), res.get("error")


def test_an_optional_dot_with_no_spot_nearer_its_pin_than_any_other_pad_is_not_generated():
    """refute:fab 2026-10-08: node Q1's and U5's optional dots sat nearer a resistor's pad than their pin 1. Q1's pin 1
    boxed in by three resistors and its own body: every spot clear of the openings is as near a resistor's pad, so no
    dot is printed, and the note says what blocked each spot."""
    sot3 = [_pad("1", -0.95, 1.1, 0.6, 0.7), _pad("2", 0.95, 1.1, 0.6, 0.7), _pad("3", 0.0, -1.1, 0.6, 0.7)]
    q = _with(_op("Q1", (10.0, 10.0, 0.0), sot3, pins={"G": ("1",), "S": ("2",), "D": ("3",)}, body=(-1.45, -0.65, 1.45, 0.65), prefix="Q"), sot3)

    def r(ref, at):
        return _with(_op(ref, at, _led_pads(), pins={"1": ("1",), "2": ("2",)}, body=None, kind="generic", prefix="R"), _led_pads())

    _dec, report = _plan((q, []), (r("R7", (9.05, 12.2, 0.0)), []), (r("R8", (8.0, 11.1, 90.0)), []), (r("R9", (10.12, 11.1, 90.0)), []))
    assert report["orient_marks"] == [] and report["orient_errors"] == [] and report["orient"][0]["verdict"] == "none"
    (note,) = [n for n in report["notes"] if n.startswith("Q1:")]
    assert note.startswith("Q1: no pin-1 dot printed (its pads fit one way only, so none is required): no spot names pad 1: of ") and "are as near another pad (R" in note, note


# ------------------------------------------------------------------ round 2: every marked part's package in the 3D view

KICAD_MODELS = Path("/Applications/KiCad/KiCad.app/Contents/SharedSupport/3dmodels")
NO_MODEL = {"ESP32-C6-MINI-1.kicad_mod"}  # KiCad's footprint names RF_Module.3dshapes/ESP32-C6-MINI-1.step, which KiCad does not ship


def _footprints():
    from pcbc.footprints import SHARE as share
    return sorted([*EXAMPLES.glob("*/components/*/*/*.kicad_mod"), *share.glob("*.kicad_mod")])


def test_every_example_and_bundled_footprint_names_a_3d_model():
    """refute:assembly 2026-10-08: nine of the seventeen marked parts (KiCad's LEDs, SOT-23s, the ESP32) had no body in
    the review's 3D view — their footprints' models had been stripped. Each names KiCad's own again."""
    bare = [f.name for f in _footprints() if "(model " not in f.read_text() and f.name not in NO_MODEL]
    assert bare == [], bare


@pytest.mark.skipif(not KICAD_MODELS.is_dir(), reason="KiCad's 3dmodels directory is not installed here")
def test_every_kicad_library_model_a_footprint_names_is_on_disk():
    missing = []
    for f in _footprints():
        for m in re.findall(r'\(model "\$\{KICAD10_3DMODEL_DIR\}/([^"]+)"', f.read_text()):
            if not (KICAD_MODELS / m).exists():
                missing.append((f.name, m))
    assert [m for m in missing if m[0] not in NO_MODEL] == [], missing
    assert {m[0] for m in missing} <= NO_MODEL


def test_the_route_stages_layout_job_refuses_a_placement_with_no_legal_spot(tmp_path: Path):
    """The route stage places again (`layout_job`): a placement whose required mark has no spot is refused there too,
    before any routing."""
    from pcbc.language import load_board
    from pcbc.layout_job import layout_job

    board = _noroom(tmp_path)
    res = layout_job(load_board(board), out=tmp_path / "out" / "layout.kicad_pcb", board=board)
    assert (res.get("error") or "").startswith("orientation: D1: no orientation mark"), res.get("error")


def test_a_board_drawing_far_from_every_part_is_no_parts_mark():
    """A 1 mm arc 5 mm from U1 (round 2's full suite: a hand-written core arc 22 mm from blinky's LED was judged the LED's
    mark and failed the build). Beyond OWN_MM from every part a drawing is decoration: no error, and U1's own mark is
    generated."""
    from pcbc.orient import graphic_shape
    from pcbc.silk import _arc_points

    arc = graphic_shape("fp_arc", tuple(_arc_points((3.0, 11.2), (3.5, 11.7), (4.0, 11.2))), 0.15, False)
    report = _u1_with_core(arc)
    assert report["orient_errors"] == [] and [r["verdict"] for r in report["orient"]] == ["generated"], report["orient_errors"]
