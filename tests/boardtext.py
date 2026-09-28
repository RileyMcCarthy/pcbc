"""Test-only: board text written from pieces, so a checker (which reads a board file) can be tested on a
hand-made board. The build never writes a board from pieces: the route stage emits layout objects
(`route_native.to_objects`, `layout_emit.emit_board`). These are the writers `route_emit` carried
before the native generator, kept here byte for byte for the tests that build fixtures with them."""

from __future__ import annotations

import json
import re
from pathlib import Path

from pcbc.route_emit import Piece, Sidecar, _jsonable, census, census_by_net, piece_key, seg_key
from pcbc.sexp import matching_paren

def segment(x1: float, y1: float, x2: float, y2: float, w: float, layer: str, net: str, uid: str, *, locked: bool = True) -> str:
    """One `(segment ...)`, locked. Lifted verbatim from `fanout.py`; the byte positions are the
    contract (`test_fanout.py`, and C.3's end-to-end check that the lock survives the gate).

    `locked=False` writes the same segment without the lock line, and it exists for exactly one
    caller: `route_relax` rewrites the geometry of KRT's own `leftover` copper and that copper is
    not pcbc's to claim. Locking it would say in the file that pcbc owns a route it did not choose,
    and it would leave a board whose every track is undraggable in KiCad's editor. The default is
    `True` and the bytes it writes are unchanged, so every existing caller is byte-identical.
    """
    lock = "\t\t(locked yes)\n" if locked else ""
    return (
        f"\n\t(segment\n\t\t(start {x1:.6f} {y1:.6f})\n\t\t(end {x2:.6f} {y2:.6f})\n\t\t(width {w:g})\n{lock}"
        f'\t\t(layer "{layer}")\n\t\t(net "{net}")\n\t\t(uuid "{uid}")\n\t)\n'
    )


def via(x: float, y: float, size: float, drill: float, net: str, uid: str) -> str:
    """One `(via ...)`, locked, always `F.Cu`..`B.Cu` — no example turns blind and buried vias on,
    so any other pair is a `blind/buried via not allowed` error (B.0). Verbatim from `fanout.py`."""
    return (
        f"\n\t(via\n\t\t(at {x:.6f} {y:.6f})\n\t\t(size {size:g})\n\t\t(drill {drill:g})\n"
        f'\t\t(layers "F.Cu" "B.Cu")\n\t\t(locked yes)\n\t\t(net "{net}")\n\t\t(uuid "{uid}")\n\t)\n'
    )


def piece_text(p: Piece, *, locked: bool = True) -> str:
    """One piece as the board file writes it. `locked=False` only reaches `segment` (see there)."""
    if p.kind == "seg":
        return segment(p.a[0], p.a[1], p.b[0], p.b[1], p.w, p.layer, p.net, p.uuid, locked=locked)
    return via(p.a[0], p.a[1], p.w, p.drill, p.net, p.uuid)


def append_items(text: str, items: list[str]) -> str:
    """Splice rendered items in before the board's closing paren. `fanout.py`'s own tail, moved."""
    if not items:
        return text
    body = text.rstrip()
    if not body.endswith(")"):
        raise ValueError("not a board file")
    return body[:-1].rstrip() + "\n" + "".join(items) + ")\n"


_SEG_SPAN = re.compile(r"\r?\n[ \t]*(\(segment\b)")
"""Where a `(segment ...)` block begins, **counting its own indentation as part of it**.

The indentation is `[ \\t]*` and not `\\t` because the reader that decides *which* segments to strip
is `copper_bar._SEG`, which is whitespace-agnostic, and an anchor stricter than that reader is a
silent no-op rather than an error. `re.sub(r"\\n\\t\\(segment\\b", "\\n  (segment", text)` — the same
s-expression with the indentation KiCad <= 7 writes — made `strip_segments` remove nothing at all,
which degraded `replace_segments` to `strip(0) + append(all)` and left buck at **203 segments /
242.21 mm** where a correct replace gives 71 / 145.7: every staircase still on the board, overlapping
the taut run that was supposed to have replaced it. Nothing caught it — not `stats`, which counts
what the relaxer decided rather than what the file got, and not `route_scene.components`, which
cannot see it in principle, because extra copper only ever merges groups. Hence both halves of the
fix: this anchor matches whatever the other reader matches, and `strip_segments` now proves it
removed every key it was given."""
_SEG_START = re.compile(r"\(start ([-0-9.]+) ([-0-9.]+)\)")
_SEG_END = re.compile(r"\(end ([-0-9.]+) ([-0-9.]+)\)")
_SEG_WIDTH = re.compile(r"\(width ([-0-9.]+)\)")
_SEG_LAYER = re.compile(r'\(layer "([^"]+)"\)')


def seg_key(layer: str, a: Pt, b: Pt, w: float) -> tuple:
    """`route.bar_key`'s and `copper_bar.bar_key`'s key for a segment, computed here so this module
    keeps its one dependency rule (`sexp` and `route_geom` only) and `strip_segments` can find a
    segment in the text without importing the two modules that sit on top of it.

    Order-free and rounded to 4 dp for the reason all three copies give: the board is rewritten by
    KRT and again by the gate's refill-and-save, and nothing says which end KiCad writes first.
    `test_route_emit.py` holds the three keys equal."""
    ra, rb = (round(a[0], 4), round(a[1], 4)), (round(b[0], 4), round(b[1], 4))
    return ("seg", layer, min(ra, rb), max(ra, rb), round(w, 4))


def strip_segments(text: str, keys) -> str:
    """The board with every `(segment ...)` whose `seg_key` is in `keys` removed, and nothing else
    touched — not a via, not a zone, not one byte of anything it keeps.

    This is the half of the R2 emitter that did not exist until slice 1. `write_pieces` only ever
    **appends**, which is what let `docs/stitch-plan.md` R-S1 argue that a pattern disturbs nothing;
    a pass that rewrites the geometry of copper already on the board needs the other half, and it is
    kept here rather than in the caller so there is one parser of a `(segment ...)` block and one
    definition of which segment is which. Matching by geometry rather than by uuid is the same
    decision `piece_key` records: uuids are re-keyed twice between writing a piece and finding it.

    **It raises when a named key is not on the board, and that is the load-bearing line.** This
    function and the reader that chose `keys` (`copper_bar.segments`, via `route_relax`) are two
    parsers of the same s-expression, and the one failure this pass cannot survive is the two of
    them disagreeing: a strip that removes nothing turns `replace_segments` into a plain append, so
    the old copper and its taut replacement are **both** on the board. That failure is invisible
    downstream by construction — `stats` reports what the relaxer decided, not what the file got,
    and `route_scene.components` can only ever be made *more* connected by extra copper, so the
    whole-board self-check passes. Measured, with KiCad <= 7's two-space indentation: 203 segments
    and 242.21 mm where a correct replace gives 71 and 145.7, `segments_out` still saying 71, and no
    exception anywhere. A key counted more than once is fine (a board may legally carry two
    identical segments and both are named and both go); a key counted **zero** times is the bug.
    """
    want = frozenset(keys)
    if not want:
        return text
    out: list[str] = []
    seen: set[tuple] = set()
    pos = 0
    for m in _SEG_SPAN.finditer(text):
        if m.start() < pos:
            continue
        end = matching_paren(text, m.start(1))
        block = text[m.start() : end + 1]
        a, b = _SEG_START.search(block), _SEG_END.search(block)
        w, layer = _SEG_WIDTH.search(block), _SEG_LAYER.search(block)
        if not (a and b and w and layer):
            continue  # not a segment this module can name; leave it exactly where it is, and let
            # the shortfall check below be what reports it if the caller had named it
        key = seg_key(layer.group(1), (float(a.group(1)), float(a.group(2))), (float(b.group(1)), float(b.group(2))), float(w.group(1)))
        out.append(text[pos : m.start()])
        pos = end + 1
        if key in want:
            seen.add(key)
        else:
            out.append(block)
    out.append(text[pos:])
    if seen != want:
        missing = sorted(want - seen)
        raise ValueError(
            f"strip_segments named {len(want)} segment(s) and found {len(seen)}: "
            f"{len(missing)} not on the board, first {missing[0]}. The caller's reader and this one "
            "disagree about where a (segment ...) begins or what is in it; removing fewer than were "
            "named would leave the replaced copper on the board alongside its replacement"
        )
    return "".join(out)


def replace_segments(text: str, keys, items) -> str:
    """`strip_segments` and then `append_items`: the segments named by `keys` leave the board and
    `items` take their place, spliced in where `write_pieces` splices.

    The two are one call because they are one edit — a board that has been stripped and not yet
    refilled is a board with a net cut in half, and no caller should ever hold one.
    """
    return append_items(strip_segments(text, keys), list(items))


def write_pieces(text: str, pieces) -> str:
    """The board with these pieces added, in the order given. Deterministic by construction: the
    caller owns the order and nothing here sorts, because a pattern's emission order is part of
    what makes two builds byte-identical."""
    return append_items(text, [piece_text(p) for p in pieces])



def sidecar(pieces, *, step: str, refusals=(), notes=(), leftover: dict | None = None) -> Sidecar:
    return Sidecar(
        items=[
            {
                "key": _jsonable(piece_key(p)),
                "uuid": p.uuid,
                "reason": p.reason,
                "net": p.net,
                "owner": p.owner,
                "step": step,
                "mm": round(p.mm, 4),
                # The size, because the key does not carry it and a reader that needs it had to
                # invent one: `test_examples_fab.py` rebuilt every via at diameter 0.0 to ask
                # `plane_checks` about it, which is exactly why that check could only ever test the
                # centre (`docs/r2-measurements.md` S5r, finding 12). `w` is the track width on a
                # segment and the ring diameter on a via; `drill` is 0.0 on a segment.
                "w": round(p.w, 4),
                "drill": round(p.drill or 0.0, 4),
            }
            for p in pieces
        ],
        refusals=[dict(r) for r in refusals],
        notes=list(notes),
        census=census(pieces, leftover=leftover),
        census_nets=census_by_net(pieces),
    )


def write_sidecar(path: Path, doc: Sidecar) -> None:
    """Written with `sort_keys=False` and a trailing newline: the order is the copper's own."""
    Path(path).write_text(json.dumps(doc.to_dict(), indent=1) + "\n")


def read_sidecar(path: Path) -> Sidecar:
    raw = json.loads(Path(path).read_text())
    return Sidecar(
        items=raw.get("items", []),
        refusals=raw.get("refusals", []),
        notes=raw.get("notes", []),
        census=raw.get("census", {}),
        census_nets=raw.get("census_nets", {}),
        channels=raw.get("channels", {}),
    )


def bar_key(p) -> tuple:
    """A piece as the routed board will name it: the geometry, with the two ends in a fixed order.

    `copper_bar` reads a board file and cannot know which end KiCad wrote first, and the board is
    rewritten twice (KRT's steps, then the gate's refill-and-save), so the key is order-free. It is
    the one place a reason survives those rewrites: the reason cannot live in the file, because
    `pin_copper_ids` re-keys uuids and KiCad invents its own (D.4).
    """
    if p.kind == "seg":
        a, b = (round(p.a[0], 4), round(p.a[1], 4)), (round(p.b[0], 4), round(p.b[1], 4))
        return ("seg", p.layer, min(a, b), max(a, b), round(p.w, 4))
    return ("via", (round(p.a[0], 4), round(p.a[1], 4)))




def fanout_copper(design, job, text: str, board: str = "board", scene=None, claimed=frozenset()):
    """The placed board with the fanout's escapes written into it, and its notes."""
    from pcbc.fanout import fanout_pieces

    pieces, notes = fanout_pieces(design, job, board, scene if scene is not None else scene_from_text(design, job, job.constraints, text), claimed)
    if not pieces:
        return text, []
    return write_pieces(text, pieces), notes


def seed_job(design, out_pcb, *, name: str) -> dict:
    """The old seed board (`seed.emit_pcb`) and its `.kicad_pro`, written to a file: a fixture."""
    from pcbc.seed import emit_pcb, emit_pro

    out_pcb = Path(out_pcb)
    out_pcb.parent.mkdir(parents=True, exist_ok=True)
    out_pcb.write_text(emit_pcb(design, name=name, base=out_pcb.parent))
    out_pcb.with_suffix(".kicad_pro").write_text(emit_pro(design, name=name))
    return {"pcb": str(out_pcb), "instances": len(design.instances)}


class WithText:
    """A `PatternPlan` and the board text its copper would make, for tests whose checker reads a
    board file. The build never writes one; `pattern_copper` hands back pieces only."""

    def __init__(self, plan, text: str):
        self._plan = plan
        self.text = text

    def __getattr__(self, name):
        return getattr(self._plan, name)


def with_text(plan, base: str, extra=()) -> WithText:
    """`plan` with `.text` = `base` plus `extra` pieces plus the plan's own pieces, in that order."""
    return WithText(plan, write_pieces(base, list(extra) + list(plan.pieces)) if (extra or plan.pieces) else base)


def _blocks(text: str) -> list[tuple[int, int, str]]:
    """Every top-level node of a KiCad-saved board as (start, end, head), `end` exclusive: KiCad writes
    each one on its own line at one tab."""
    out = []
    pos = 0
    while True:
        j = text.find("\n\t(", pos)
        if j < 0:
            break
        start = j + 2
        end = matching_paren(text, start) + 1
        head = text[start + 1 : start + 41].split(None, 1)[0].rstrip(")")
        out.append((start, end, head))
        pos = end
    return out


# --- a board text read back into the router's model (test-only) ------------------------------------
#
# The build never reads a board text to route (docs/direction.md §1): `route_scene.build_scene` is built
# from `Placement.feet` (each `.kicad_mod` posed by its `Pose`). Many tests drive the patterns over a
# fixture that is a board *file* (a placed board, or one with hand-written copper), so this reads one
# back into the same model: every footprint block as a `foot_native.PosedFoot` (its pads' board angles
# turned back into the library frame by the footprint's), every track and via as an `Item`, every pour's
# fill numbers as a `ZoneRule`. `tests/test_native_scene.py` proves the reading: the scene of the placed
# board's text equals the native scene item for item on all five boards.


def feet_of_text(design, text: str):
    """Every footprint of a board text as a `PosedFoot`: library-frame pads, the courtyard, the pose,
    and the design's pad-to-net binding. A pad keeps the net its block binds (the file wins, as KiCad
    judges it)."""
    from dataclasses import replace

    from pcbc.foot_native import LibFoot, PosedFoot
    from pcbc.geom import _graphics_bbox
    from pcbc.pads import pad_specs
    from pcbc.pcb_place import _pins_of
    from pcbc.sexp import board_footprint_spans, footprint_at, footprint_reference

    nets_of = _pins_of(design)
    out = []
    for a, b in board_footprint_spans(text):
        block = text[a:b]
        ref = footprint_reference(block)
        at = footprint_at(block)
        if ref is None or at is None:
            continue
        rot = float(at[2] or 0.0)
        lm = re.search(r'\n\t\t\(layer "([^"]*)"\)', block)
        specs = []
        for sp in pad_specs(block):
            local = (sp.angle - rot) % 360.0
            specs.append(replace(sp, angle=0.0 if abs(local) < 1e-9 or abs(local - 360.0) < 1e-9 else local))
        lib = LibFoot(tuple(specs), _graphics_bbox(block, "CrtYd"))
        out.append(PosedFoot(ref, (at[0], at[1]), rot % 360.0, lm.group(1) if lm else "F.Cu", lib, tuple(sorted(nets_of.get(ref, {}).items()))))
    return tuple(sorted(out, key=lambda f: f.ref))


def copper_items(text: str, stack):
    """Every track and via of a board text, as scene items. A via's hole is its drill; its copper the ring."""
    from pcbc.copper_bar import segments as _segments
    from pcbc.route_geom import circle_shape, qp, track_shape, via_shape
    from pcbc.route_scene import Item
    from pcbc.route_verify import board_vias

    out = []
    for s in _segments(text):
        a, b = qp(s["start"]), qp(s["end"])
        if a == b:
            continue  # a zero-length segment is not copper anything can clash with
        out.append(Item(0, "track", s["net"], frozenset({s["layer"]}), track_shape(a, b, s["width"]), None, None, f"{s['net'] or 'no net'} track on {s['layer']}", locked=s["locked"]))
    for v in board_vias(text):
        at = qp(v["at"])
        out.append(Item(0, "via", v["net"], frozenset(stack.copper_layers()), via_shape(at, v["size"]), circle_shape(at[0], at[1], v["drill"]), None, f"{v['net'] or 'no net'} via at ({at[0]:g},{at[1]:g})", locked=v["locked"]))
    return out


_ZONE_HEAD = re.compile(r"\n\t\(zone\b")
_ZONE_NET_RE = re.compile(r'\(net_name "([^"]*)"\)|\(net "([^"]*)"\)')
_ZONE_LAYER_RE = re.compile(r'\(layers? "([^"]+)"')
_ZONE_CLEAR_RE = re.compile(r"\(connect_pads[^()]*(?:\s*\(clearance ([-0-9.]+)\))")
_ZONE_MINTHICK_RE = re.compile(r"\(min_thickness ([-0-9.]+)\)")


def zone_rules(text: str):
    """Every copper pour's `(net, layer, connect_pads clearance, min_thickness)` of a board text, in file
    order; a rule area, a teardrop and a multi-layer zone are skipped (no antipads to merge)."""
    from pcbc.route_scene import ZoneRule

    out = []
    for m in _ZONE_HEAD.finditer(text):
        end = matching_paren(text, m.start() + 2)
        head = text[m.start() : end + 1].split("(polygon", 1)[0]
        if "(keepout" in head or ("(attr" in head and "(teardrop" in head):
            continue
        nm = _ZONE_NET_RE.search(head)
        lm = _ZONE_LAYER_RE.search(head)
        net = (nm.group(1) or nm.group(2)) if nm else ""
        layers = lm.group(1) if lm else ""
        if not net or " " in layers or not layers:
            continue
        cm = _ZONE_CLEAR_RE.search(head)
        tm = _ZONE_MINTHICK_RE.search(head)
        out.append(ZoneRule(net=net, layer=layers, pad_clearance=float(cm.group(1)) if cm else 0.0, min_thickness=float(tm.group(1)) if tm else 0.0))
    return tuple(out)


def scene_from_text(design, job, cs, text: str):
    """`route_scene.build_scene` over a board text read back into the model (`feet_of_text`, its copper
    and its pours' numbers)."""
    from pcbc.route_scene import build_scene
    from pcbc.stackup import get_stackup

    return build_scene(design, job, cs, feet_of_text(design, text), extra=copper_items(text, get_stackup(job.stackup)), zones=zone_rules(text))


def plant_errors(design, job, text: str) -> tuple[str, list[str]]:
    """The board text with three copper errors KiCad reports planted in free space, and what they are:

    - two F.Cu tracks of two different signal nets 0.1 mm apart (`clearance`, actual 0.1 mm);
    - a GND via and a signal via whose holes are `hole_to_hole - 0.2` mm apart (`hole_to_hole`, and a
      `clearance` between their rings; KiCad 10.0.6 does not report two holes of one net);
    - a GND via `edge_clearance / 2` from the left board edge (`copper_edge_clearance`).

    Each spot is the first point, row-major on a 0.5 mm grid, with no pad, hole, track, via or keepout
    within 2.5 mm (the edge via: 2 mm, scanning down the left edge), so the planted copper clashes only
    with itself and the edge. GND is used for the vias because on every example it is the net of the
    pour(s) the via passes through; KiCad is asked to refill before judging. Deterministic: the same
    board gives the same bytes."""
    from pcbc.route_geom import circle_shape, gap

    scene = scene_from_text(design, job, job.constraints, text)
    stack = scene.stack
    w_mm, h_mm = job.board_size_mm
    signals = sorted({it.net for it in scene.items if it.kind == "pad" and it.net and it.net != "GND"})
    n1, n2 = signals[0], signals[1]
    solid = [s for it in scene.items if it.kind in ("pad", "hole", "track", "via", "keepout") for s in (it.copper, it.hole) if s is not None]

    def free(p, r) -> bool:
        dot = circle_shape(p[0], p[1], 2.0 * r)
        return all(gap(dot, s) > 0.0 for s in solid)

    spots: list[tuple[float, float]] = []
    for y in [k * 0.5 for k in range(12, int((h_mm - 4.0) / 0.5))]:
        for x in [k * 0.5 for k in range(12, int((w_mm - 6.0) / 0.5))]:
            if free((x, y), 2.5) and all(abs(x - a) + abs(y - b) > 6.0 for a, b in spots):
                spots.append((x, y))
        if len(spots) >= 2:
            break
    if len(spots) < 2:
        raise ValueError("no free spot to plant in")
    (ax, ay), (bx, by) = spots[:2]
    e = stack.edge_clearance
    vd, dr = stack.via_diameter, stack.via_drill
    ex = round(vd / 2.0 + e / 2.0, 4)
    ey = next(y for y in [k * 0.5 for k in range(8, int((h_mm - 4.0) / 0.5))] if free((ex, y), 2.0))
    pitch = round(dr + stack.hole_to_hole - 0.2, 4)
    items = [
        segment(ax, ay, ax + 1.5, ay, 0.2, "F.Cu", n1, "plant-a1", locked=False),
        segment(ax, ay + 0.3, ax + 1.5, ay + 0.3, 0.2, "F.Cu", n2, "plant-a2", locked=False),
        via(bx, by, vd, dr, "GND", "plant-b1"),
        via(bx + pitch, by, vd, dr, n1, "plant-b2"),
        via(ex, ey, vd, dr, "GND", "plant-c1"),
    ]
    what = [
        f"clearance: {n1} and {n2} tracks 0.1 mm apart at ({ax:g},{ay:g})",
        f"hole_to_hole: a GND and a {n1} via {pitch:g} mm apart at ({bx:g},{by:g})",
        f"copper_edge_clearance: a GND via {e / 2:g} mm from the left edge at ({ex:g},{ey:g})",
    ]
    return append_items(text, items), what
