from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class BoardSpec:
    size_mm: tuple[float, float]
    layers: int = 4
    stackup: str = "jlcpcb_4l_1oz"
    pcb: str | None = None
    planes: tuple[tuple[str, str], ...] = ()
    padding: tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)


@dataclass
class PlaceSpec:
    ref: str
    at: tuple[float, float] | None = None
    rot: float = 0.0
    side: str = "F"
    locked: bool = False
    reason: str = ""
    position: str = "static"
    top: object | None = None
    right: object | None = None
    bottom: object | None = None
    left: object | None = None
    width: object | None = None
    height: object | None = None
    margin_top: object = 0
    margin_right: object = 0
    margin_bottom: object = 0
    margin_left: object = 0
    translate_x: object | None = None
    translate_y: object | None = None
    from_box: str = "courtyard"
    parent: str | None = None
    # Relational placement (pcb_place): next to a pin, or on a board edge.
    to: str | None = None
    toward: str | None = None
    gap: float | None = None
    edge: str | None = None
    overhang: float = 0.0
    rot_set: bool = False

    def has_css(self) -> bool:
        return any(
            getattr(self, k) is not None
            for k in ("top", "right", "bottom", "left", "width", "height")
        ) or self.position == "absolute"


@dataclass
class KeepoutSpec:
    name: str
    box: tuple[float, float, float, float] | None = None
    no: tuple[str, ...] = ("copper", "via")
    position: str = "absolute"
    top: object | None = None
    right: object | None = None
    bottom: object | None = None
    left: object | None = None
    width: object | None = None
    height: object | None = None
    margin_top: object = 0
    margin_right: object = 0
    margin_bottom: object = 0
    margin_left: object = 0
    parent: str | None = None

    def has_css(self) -> bool:
        return self.box is None or any(
            getattr(self, k) is not None
            for k in ("top", "right", "bottom", "left", "width", "height")
        )


@dataclass
class RegionSpec:
    name: str
    position: str = "absolute"
    top: object | None = None
    right: object | None = None
    bottom: object | None = None
    left: object | None = None
    width: object | None = None
    height: object | None = None
    margin_top: object = 0
    margin_right: object = 0
    margin_bottom: object = 0
    margin_left: object = 0
    padding_top: object = 0
    padding_right: object = 0
    padding_bottom: object = 0
    padding_left: object = 0
    parent: str | None = None


@dataclass
class SchPlaceSpec:
    """Schematic pose. CSS is body-in-region; pin/to/gap attaches a pin to another."""

    ref: str
    pin: str | None = None
    to: str | None = None
    along: str | None = None
    gap: float | None = None  # None: the tool leaves room for the net's label
    align: str | None = None
    side: str | None = None
    mirror: str | None = None  # "x" (top/bottom) or "y" (left/right), KiCad sense
    rot: float = 0.0
    rotate_set: bool = False
    reason: str = ""
    position: str = "static"
    top: object | None = None
    right: object | None = None
    bottom: object | None = None
    left: object | None = None
    width: object | None = None
    height: object | None = None
    margin_top: object = 0
    margin_right: object = 0
    margin_bottom: object = 0
    margin_left: object = 0
    parent: str | None = None

    def has_css(self) -> bool:
        return any(
            getattr(self, k) is not None
            for k in ("top", "right", "bottom", "left", "width", "height")
        ) or self.position == "absolute"

    def has_attach(self) -> bool:
        return bool(self.to or self.along)


@dataclass
class NetReqSpec:
    nets: tuple[str, ...]
    kind: str
    z_diff_ohm: float | None = None
    z_se_ohm: float | None = None
    volts: float | None = None
    amps: float | None = None
    temp_rise_c: float = 10.0
    max_mm: float | None = None
    match_mm: float | None = None
    pair: bool = False
    vias: bool | None = None
    layers: tuple[str, ...] | None = None
    keep_clear_of: tuple[str, ...] = ()  # globs; constraints.Constraint expands them
    keep_clear_mm: float | None = None
    autoroute: bool | str | None = None
    class_name: str | None = None
    # R1 (docs/r1-design.md A.2)
    length_mm: float | None = None  # routed length -> KiCad length (max)
    uncoupled_mm: float | None = None
    vias_max: int | None = None
    reference: str | None = None  # "In1.Cu"
    clock: str | None = None  # spi: the net the others match to
    pf_max: float | None = None  # i2c bus capacitance budget
    loop_mm2: float | None = None
    rise_ps: float | None = None  # the fastest edge this net carries, ps; no default anywhere
    line: int = 0  # board.py line of the NetReq; 0 when not loaded from a board file


@dataclass(frozen=True)
class PairReq:
    p: str
    n: str
    z_diff_ohm: float = 90.0
    match_mm: float = 0.5
    uncoupled_mm: float = 2.0
    gap_mm: float | None = None
    layers: tuple[str, ...] | None = None
    reference: str | None = None
    line: int = 0


@dataclass(frozen=True)
class BusReq:
    nets: tuple[str, ...]
    match_mm: float
    clock: str | None = None
    length_mm: float | None = None
    line: int = 0


@dataclass(frozen=True)
class ChainReq:
    net: str
    pads: tuple[str, ...]  # ("J2.1", "C4.1", "U1.12"), pin names as Place(to=) takes them
    line: int = 0


@dataclass(frozen=True)
class IsolationReq:
    a: str
    b: str
    volts: float
    slot: bool = False
    across: tuple[str, ...] = ()
    reinforced: bool = False
    line: int = 0


@dataclass(frozen=True)
class BridgeReq:
    """`Bridge(a, b, at=...)`: the one point at which two declared grounds are tied, and what ties it.

    `at` is a tuple because `kind="off_board"` names the two pads that leave the board and every
    other kind names one part. pcbc writes no copper for any of them (`docs/stitch-plan.md` §8 item
    4), so this carries no geometry at all — the tie is a part the board already places.
    """

    a: str
    b: str
    at: tuple[str, ...]
    kind: str = "short"  # short | cap | bead | off_board
    why: str = ""
    line: int = 0


@dataclass(frozen=True)
class ThermalReq:
    """`Thermal(pad, watts=...)`: a via array under one exposed pad (R-T1).

    A board fact and not a library fact, which is why it is a statement and not `thermal=` on a
    `Part` — see `language.Thermal`. Everything here is what the author wrote; the arithmetic (the
    barrel's K/W, the count, the pitch) is `constraints.ThermalSpec`'s and the sites are
    `patterns.stitch.sites_lattice`'s.
    """

    pad: str  # "U1.49" — REF.PADNUM or REF.PINNAME, `_refpin_net`'s resolution
    watts: float
    rise_c: float = 10.0
    across_planes: bool = False
    fill: bool = False
    line: int = 0


@dataclass(frozen=True)
class GuardReq:
    net: str
    stitch_mm: float = 2.5
    ground: str = "GND"
    line: int = 0


@dataclass
class Net:
    name: str
    kind: str = "net"  # net | power | ground
    wire_mm: float | None = None  # longest schematic wire before a label; None: SchStyle default

    def __str__(self) -> str:
        return self.name


@dataclass
class Pin:
    """One symbol pin name, possibly several pads (GND on 1 and 44)."""

    name: str
    pads: tuple[str, ...]
    optional: bool = False
    etype: str = "unspecified"


@dataclass
class Part:
    name: str
    prefix: str = "U"
    mpn: str = ""
    manufacturer: str = ""
    lcsc: str | None = None
    footprint: str = ""
    symbol: str | None = None
    body_mm: tuple[float, float] | None = None
    package: str = ""
    value: str = ""
    kind: str = "ic"  # ic | generic | th
    origin: Path | None = None
    pins: dict[str, Pin] = field(default_factory=dict)
    optional_pins: tuple[str, ...] = ()

    def __call__(self, ref: str, **pin_nets: object) -> Instance:
        from .circuit import instantiate

        return instantiate(self, ref, pin_nets)


@dataclass
class Instance:
    ref: str
    part: Part
    pins: dict[str, str]  # symbol pin name → net name
    value: str = ""


@dataclass
class Design:
    board: BoardSpec | None = None
    places: list[PlaceSpec] = field(default_factory=list)
    keepouts: list[KeepoutSpec] = field(default_factory=list)
    regions: list[RegionSpec] = field(default_factory=list)
    netreqs: list[NetReqSpec] = field(default_factory=list)
    pairs: list[PairReq] = field(default_factory=list)
    buses: list[BusReq] = field(default_factory=list)
    chains: list[ChainReq] = field(default_factory=list)
    isolations: list[IsolationReq] = field(default_factory=list)
    guards: list[GuardReq] = field(default_factory=list)
    bridges: list[BridgeReq] = field(default_factory=list)
    thermals: list[ThermalReq] = field(default_factory=list)
    nets: dict[str, Net] = field(default_factory=dict)
    instances: list[Instance] = field(default_factory=list)
    sch_places: list[SchPlaceSpec] = field(default_factory=list)
    sch_regions: list[RegionSpec] = field(default_factory=list)
    sch_wire_mm: float = 25.4
    source: str | None = None
