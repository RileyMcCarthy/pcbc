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
    gap: float = 5.08
    align: str | None = None
    side: str | None = None
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
    keep_clear_of: str | None = None
    keep_clear_mm: float | None = None
    autoroute: bool | str | None = None
    class_name: str | None = None


@dataclass
class Net:
    name: str
    kind: str = "net"  # net | power | ground

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
    nets: dict[str, Net] = field(default_factory=dict)
    instances: list[Instance] = field(default_factory=list)
    sch_places: list[SchPlaceSpec] = field(default_factory=list)
    sch_regions: list[RegionSpec] = field(default_factory=list)
    source: str | None = None
