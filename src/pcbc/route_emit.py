"""`Piece`: one segment or via pcbc's patterns or router wrote, with the reason it exists.

A piece is the in-memory model the route stage builds copper in; `route_native.to_objects` turns
each into the `Seg`/`Via` layout object `layout.gen.py` carries, with its reason and owner as a
`pcbc:<reason>:<owner>` KiCad group. `piece_key`/`seg_key` are the geometry keys the gates and the
census use (order-free, 4 dp). `Sidecar` is the in-memory roles document `layout_job.roles_doc` reads
off the emitted board's groups. Depends on `route_geom` only; it makes no geometric decision.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .route_geom import Pt, q

__all__ = [
    "Piece",
    "Sidecar",
    "census",
    "census_by_net",
    "piece_key",
    "seg_key",
    "seg_piece",
    "via_piece",
]

REASONS = ("fanout", "hop", "chain", "tap", "spine", "bus", "guard", "stitch", "thermal", "plane")
"""Every reason pcbc's patterns' copper can carry (B.0). `route` is the router's; `leftover` is copper no
group names (a core line's).

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


def seg_key(layer: str, a: Pt, b: Pt, w: float) -> tuple:
    """`copper_bar.bar_key`'s key for a segment, computed here so this module keeps its one
    dependency rule (`sexp` and `route_geom` only).

    Order-free and rounded to 4 dp: the board is rewritten by the gate's refill-and-save, and nothing
    says which end KiCad writes first."""
    ra, rb = (round(a[0], 4), round(a[1], 4)), (round(b[0], 4), round(b[1], 4))
    return ("seg", layer, min(ra, rb), max(ra, rb), round(w, 4))


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
    """What pcbc owns on a board, keyed by geometry: `layout_job.roles_doc`'s answer, read off the
    emitted board's role groups (D.4)."""

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
