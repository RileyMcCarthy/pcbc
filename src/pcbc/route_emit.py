"""Pieces to KiCad text, and the sidecar that remembers why each one is there.

`segment` and `via` are lifted verbatim from `fanout.py` (which now imports them), byte for byte,
including where `(locked yes)` sits: after `(width)` on a segment and after `(layers)` on a via, the
two positions KRT's own parser reads. That is not cosmetic — the lock is what tells every later KRT
step that pcbc's copper is an obstacle it may route around and may not move, and it survived the
gate's refill-and-save end to end (`docs/r2-design.md` C.3). The fanout's copper coming out
byte-identical through this module is S3's acceptance, and `test_fanout.py` is what holds it.

The sidecar exists because the reason cannot live in the file: KiCad rewrites the board during the
gate's refill-and-save and `pin_copper_ids` re-keys uuids, so `layout/<board>/routed/copper.json` is
keyed by **geometry** instead — the same `("seg", layer, a, b, w)` / `("via", at, layers)` key
`blocking._keys` already uses, which is stable through both rewrites because the copper is locked
(D.4).

Depends on `sexp` and `route_geom` only; it writes text and answers questions about text, and it
makes no geometric decision.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from .route_geom import Pt, q
from .sexp import matching_paren

__all__ = [
    "Piece",
    "census",
    "replace_segments",
    "seg_key",
    "strip_segments",
    "piece_key",
    "read_sidecar",
    "seg_piece",
    "segment",
    "via",
    "via_piece",
    "write_pieces",
    "write_sidecar",
]

REASONS = ("fanout", "hop", "chain", "tap", "spine", "bus", "guard", "stitch", "thermal", "plane")
"""Every reason pcbc's own copper can carry (B.0). `leftover` is the router's, not a pattern's.

`"stitch"` and `"thermal"` are both `patterns/stitch.py`'s and they are two entries on purpose: a
census keyed on the module would say "10 stitch vias" where a board has one parallel rung and nine
barrels under a heatsink land, and one of `route_verify`'s two size rules applies to each
(`patterns.stitch.THERMAL_REASON`).

`"plane"` is `patterns/stitch.py`'s fourth and last, and it is separate for the same reason: a
lattice barrel tying two pours of one net has no anchor and no link, so counting it as `"stitch"`
would put it in front of `route_verify.parallel_joined`'s "is this twin joined to its anchor" check,
which is fatal (`patterns.stitch.PLANE_REASON`)."""


@dataclass(frozen=True)
class Piece:
    """One segment or one via pcbc wrote, with the reason it exists.

    B.0 gives this dataclass to `patterns/__init__.py`, which does not exist until S4 and which
    will import it from here: `route_emit` cannot import `patterns` (patterns is built on top of
    it), so the type lives at the bottom of the stack and S4 re-exports it.

    Coordinates are quantised at construction — once, here, and never again on the way out — so the
    text written is the geometry that was checked (`route_geom.q`).
    """

    kind: str  # "seg" | "via"
    net: str
    reason: str
    layer: str | tuple[str, str]  # seg: one layer; via: (from, to), always ("F.Cu", "B.Cu") in R2
    a: Pt
    b: Pt | None = None  # seg: the far end; via: None
    w: float = 0.0  # seg width, or via diameter
    drill: float | None = None  # via only
    owner: str = ""  # "U1.4", for the report and the sidecar
    uuid: str = ""

    def __post_init__(self) -> None:
        if self.kind not in ("seg", "via"):
            raise ValueError(f"a Piece is a seg or a via, not {self.kind!r}")
        object.__setattr__(self, "a", (q(self.a[0]), q(self.a[1])))
        if self.b is not None:
            object.__setattr__(self, "b", (q(self.b[0]), q(self.b[1])))
        if self.kind == "seg":
            if self.b is None:
                raise ValueError("a seg Piece needs both ends")
            if self.a == self.b:
                raise ValueError(f"a seg Piece has zero length at {self.a!r}")
            if not isinstance(self.layer, str):
                raise ValueError(f"a seg is on one layer, got {self.layer!r}")
        else:
            if self.b is not None:
                raise ValueError("a via Piece has one point")
            if not (isinstance(self.layer, tuple) and len(self.layer) == 2):
                raise ValueError(f"a via spans a pair of layers, got {self.layer!r}")
            if not self.drill:
                raise ValueError("a via Piece needs a drill")

    @property
    def mm(self) -> float:
        """The copper length this piece carries; a via carries none (D.4's census counts vias)."""
        if self.kind != "seg":
            return 0.0
        return ((self.b[0] - self.a[0]) ** 2 + (self.b[1] - self.a[1]) ** 2) ** 0.5


def seg_piece(net: str, reason: str, layer: str, a: Pt, b: Pt, w: float, *, owner: str = "", uuid: str = "") -> Piece:
    return Piece("seg", net, reason, layer, a, b, w, owner=owner, uuid=uuid)


def via_piece(net: str, reason: str, at: Pt, size: float, drill: float, *, layers: tuple[str, str] = ("F.Cu", "B.Cu"), owner: str = "", uuid: str = "") -> Piece:
    return Piece("via", net, reason, layers, at, None, size, drill, owner=owner, uuid=uuid)


def piece_key(p: Piece) -> tuple:
    """`blocking._keys`' key, which is stable through KiCad's rewrite because the copper is locked.

    Rounded to 4 dp for the same reason `blocking` rounds: KiCad writes 6 decimals and re-reads
    them, and 4 dp is a tenth of the finest grid anything here uses.
    """
    if p.kind == "seg":
        return ("seg", p.layer, (round(p.a[0], 4), round(p.a[1], 4)), (round(p.b[0], 4), round(p.b[1], 4)), round(p.w, 4))
    return ("via", (round(p.a[0], 4), round(p.a[1], 4)), tuple(p.layer))


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


def census(pieces, *, leftover: dict | None = None) -> dict:
    """D.4's census: segments, vias and mm per reason, sorted so the JSON is a function of the copper.

    `mm` is rounded to 4 dp — it is a report number, and an unrounded float would put a different
    17-digit tail in `copper.json` on a different platform for the same board.
    """
    out: dict[str, dict] = {}
    for p in pieces:
        row = out.setdefault(p.reason, {"segments": 0, "vias": 0, "mm": 0.0})
        row["segments" if p.kind == "seg" else "vias"] += 1
        row["mm"] += p.mm
    doc = {r: {"segments": v["segments"], "vias": v["vias"], "mm": round(v["mm"], 4)} for r, v in sorted(out.items())}
    if leftover is not None:
        doc["leftover"] = leftover
    return doc


def census_by_net(pieces) -> dict:
    """The census of `census()` split by net as well as by reason.

    Every piece already carries its net, so this is the same loop asked one key deeper — and it is the
    number the S7 ledger got wrong by hand: c3_usb's spine census is 9 segments and 25.13 mm, all of
    it `3V3`, while the table recorded `VBUS` as spined too (findings 5, 8, 15, 17). Keyed by reason
    then net, both sorted, so the JSON is a function of the copper.
    """
    out: dict[str, dict[str, dict]] = {}
    for p in pieces:
        row = out.setdefault(p.reason, {}).setdefault(p.net, {"segments": 0, "vias": 0, "mm": 0.0})
        row["segments" if p.kind == "seg" else "vias"] += 1
        row["mm"] += p.mm
    return {
        r: {n: {**v, "mm": round(v["mm"], 4)} for n, v in sorted(nets.items())}
        for r, nets in sorted(out.items())
    }


def _jsonable(v):
    """A key as JSON: tuples become lists, all the way down, so `read_sidecar(write_sidecar(x))`
    compares equal to `piece_key` after the same conversion and a diff of the file is readable."""
    return [_jsonable(x) for x in v] if isinstance(v, (tuple, list)) else v


@dataclass
class Sidecar:
    """`layout/<board>/routed/copper.json` (D.4): what pcbc owns, keyed by geometry."""

    items: list[dict] = field(default_factory=list)
    refusals: list[dict] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    census: dict = field(default_factory=dict)
    census_nets: dict = field(default_factory=dict)
    channels: dict = field(default_factory=dict)
    """`route_channel.channels_doc` of the **placed** board this route started from.

    Not of the routed one: it is the board the router was *handed*, and it is the half of a routing
    failure this file has never carried. A net that comes back unrouted now has, beside it, the
    census that says whether a channel for it ever existed. It is a pure function of the placement
    and carries no wall clock, so `copper.json` stays byte-identical run to run."""

    def to_dict(self) -> dict:
        return {"items": self.items, "refusals": self.refusals, "notes": self.notes, "census": self.census, "census_nets": self.census_nets, "channels": self.channels}


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
