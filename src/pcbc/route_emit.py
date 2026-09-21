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
from dataclasses import dataclass, field
from pathlib import Path

from .route_geom import Pt, q

__all__ = [
    "Piece",
    "census",
    "piece_key",
    "read_sidecar",
    "seg_piece",
    "segment",
    "via",
    "via_piece",
    "write_pieces",
    "write_sidecar",
]

REASONS = ("fanout", "hop", "chain", "tap", "spine", "bus", "guard", "stitch")
"""Every reason pcbc's own copper can carry (B.0). `leftover` is the router's, not a pattern's."""


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


def segment(x1: float, y1: float, x2: float, y2: float, w: float, layer: str, net: str, uid: str) -> str:
    """One `(segment ...)`, locked. Lifted verbatim from `fanout.py`; the byte positions are the
    contract (`test_fanout.py`, and C.3's end-to-end check that the lock survives the gate)."""
    return (
        f"\n\t(segment\n\t\t(start {x1:.6f} {y1:.6f})\n\t\t(end {x2:.6f} {y2:.6f})\n\t\t(width {w:g})\n\t\t(locked yes)\n"
        f'\t\t(layer "{layer}")\n\t\t(net "{net}")\n\t\t(uuid "{uid}")\n\t)\n'
    )


def via(x: float, y: float, size: float, drill: float, net: str, uid: str) -> str:
    """One `(via ...)`, locked, always `F.Cu`..`B.Cu` — no example turns blind and buried vias on,
    so any other pair is a `blind/buried via not allowed` error (B.0). Verbatim from `fanout.py`."""
    return (
        f"\n\t(via\n\t\t(at {x:.6f} {y:.6f})\n\t\t(size {size:g})\n\t\t(drill {drill:g})\n"
        f'\t\t(layers "F.Cu" "B.Cu")\n\t\t(locked yes)\n\t\t(net "{net}")\n\t\t(uuid "{uid}")\n\t)\n'
    )


def piece_text(p: Piece) -> str:
    """One piece as the board file writes it."""
    if p.kind == "seg":
        return segment(p.a[0], p.a[1], p.b[0], p.b[1], p.w, p.layer, p.net, p.uuid)
    return via(p.a[0], p.a[1], p.w, p.drill, p.net, p.uuid)


def append_items(text: str, items: list[str]) -> str:
    """Splice rendered items in before the board's closing paren. `fanout.py`'s own tail, moved."""
    if not items:
        return text
    body = text.rstrip()
    if not body.endswith(")"):
        raise ValueError("not a board file")
    return body[:-1].rstrip() + "\n" + "".join(items) + ")\n"


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

    def to_dict(self) -> dict:
        return {"items": self.items, "refusals": self.refusals, "notes": self.notes, "census": self.census}


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
    )


def write_sidecar(path: Path, doc: Sidecar) -> None:
    """Written with `sort_keys=False` and a trailing newline: the order is the copper's own."""
    Path(path).write_text(json.dumps(doc.to_dict(), indent=1) + "\n")


def read_sidecar(path: Path) -> Sidecar:
    raw = json.loads(Path(path).read_text())
    return Sidecar(items=raw.get("items", []), refusals=raw.get("refusals", []), notes=raw.get("notes", []), census=raw.get("census", {}))
