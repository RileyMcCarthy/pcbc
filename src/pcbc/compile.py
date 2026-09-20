"""Compile Place/NetReq intent into KiCad geometry and engine flags."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace

from .layout import resolve_keepout, resolve_regions
from .model import Design, KeepoutSpec, PlaceSpec, RegionSpec
from .stackup import diff_pair_geometry, get_stackup, hole_floor, ipc2221_width_mm, width_for_z0


@dataclass
class CompiledClass:
    name: str
    track_width_mm: float
    clearance_mm: float
    via_diameter_mm: float = 0.45
    via_drill_mm: float = 0.20
    diff_pair_width_mm: float | None = None
    diff_pair_gap_mm: float | None = None
    patterns: list[str] = field(default_factory=list)


@dataclass
class CompiledNet:
    patterns: tuple[str, ...]
    class_name: str
    autoroute: bool | str
    vias: bool
    layers: tuple[str, ...]
    max_length_mm: float | None = None
    match_group: tuple[str, ...] | None = None
    keep_clear_of: str | None = None
    keep_clear_mm: float | None = None
    kind: str = ""
    amps: float | None = None


@dataclass
class DruRule:
    name: str
    constraint: str
    condition: str


@dataclass
class CompiledJob:
    board_size_mm: tuple[float, float]
    layers: int
    stackup: str
    pcb: str | None
    planes: tuple[tuple[str, str], ...]
    places: list[PlaceSpec]
    keepouts: list[KeepoutSpec]
    regions: list[RegionSpec]
    padding: tuple[float, float, float, float]
    classes: list[CompiledClass]
    nets: list[CompiledNet]
    dru: list[DruRule]
    skip_autoroute_patterns: list[str]
    krt: dict

    def to_dict(self) -> dict:
        return {
            "board_size_mm": list(self.board_size_mm),
            "layers": self.layers,
            "stackup": self.stackup,
            "pcb": self.pcb,
            "planes": [list(p) for p in self.planes],
            "places": [asdict(p) for p in self.places],
            "keepouts": [asdict(k) for k in self.keepouts],
            "regions": [asdict(r) for r in self.regions],
            "padding": list(self.padding),
            "classes": [asdict(c) for c in self.classes],
            "nets": [
                {
                    **asdict(n),
                    "patterns": list(n.patterns),
                    "layers": list(n.layers),
                    "match_group": list(n.match_group) if n.match_group else None,
                }
                for n in self.nets
            ],
            "dru": [asdict(r) for r in self.dru],
            "skip_autoroute_patterns": self.skip_autoroute_patterns,
            "krt": self.krt,
        }


_KIND_CLASS = {
    "usb_hs": "USB",
    "power": "Power",
    "analog": "Analog",
    "clock": "Clock",
    "switch_node": "SwitchNode",
    "digital": "Default",
    "default": "Default",
}


def compile_design(design: Design) -> CompiledJob:
    if design.board is None:
        raise ValueError("design has no Board()")
    board = design.board
    stack = get_stackup(board.stackup)

    # Every class clears at least what a via's hole needs from the copper beside it: KiCad checks
    # copper-to-hole, the router keeps copper-to-ring, and the difference is the annular ring.
    floor_clear = max(stack.clearance_min, hole_floor(stack))
    classes: dict[str, CompiledClass] = {
        "Default": CompiledClass("Default", max(0.16, stack.track_min), max(0.16, floor_clear), stack.via_diameter, stack.via_drill),
    }
    compiled_nets: list[CompiledNet] = []
    dru: list[DruRule] = [
        # A connector's own pads sit closer than a power class asks (USB-C: 0.1 mm); that is the
        # land, not a routing choice. Inside one footprint only the fab floor applies.
        DruRule(
            name="pads_of_one_footprint",
            constraint="(constraint clearance (min 0.1mm))",
            condition="A.Type == 'Pad' && B.Type == 'Pad' && A.Reference == B.Reference",
        )
    ]
    skip: list[str] = []

    for req in design.netreqs:
        cls_name = req.class_name or _KIND_CLASS.get(req.kind, req.kind.title().replace(" ", ""))
        width, clearance = max(0.16, stack.track_min), max(0.16, floor_clear)
        via_d, via_h = stack.via_diameter, stack.via_drill
        dp_w = dp_g = None
        autoroute: bool | str = True
        vias = True
        layers: tuple[str, ...] = ("F.Cu", "B.Cu")

        if req.kind == "usb_hs":
            z = req.z_diff_ohm or 90.0
            dp_w, dp_g = diff_pair_geometry(z, stack)
            # Loosely-coupled 90 Ω on 1.6 mm 2-layer wants ~2 mm members.
            # USB-C pad pitch is ~0.5 mm. Clamp to a tightly-coupled pair at
            # the board floor; true 90 Ω needs 4-layer (or thinner dielectric).
            # Pair gap is not other-net clearance — keep class clearance at
            # the board floor so DRC is not graded at 1.5 mm.
            if dp_w > 0.25:
                dp_w, dp_g = 0.10, 0.10
            width = dp_w
            clearance = min(0.16, dp_w)
            autoroute = "diff_pair"
            layers = req.layers or ("F.Cu", "B.Cu")
        elif req.kind == "power":
            amps = req.amps or 0.2
            width = ipc2221_width_mm(amps, req.temp_rise_c, stack.copper_oz)
            width = max(width, 0.4 if amps >= 0.2 else 0.25)
            clearance = 0.20
            via_d, via_h = 0.80, 0.40
            layers = req.layers or ("F.Cu", "B.Cu", "In1.Cu", "In2.Cu")
        elif req.kind == "analog":
            width, clearance = 0.20, 0.20
            via_d, via_h = 0.60, 0.30
            autoroute = False
            vias = False
            layers = req.layers or ("F.Cu",)
        elif req.kind == "clock":
            width, clearance = 0.15, 0.20
            via_d, via_h = 0.60, 0.30
            layers = req.layers or ("F.Cu", "B.Cu")
        elif req.kind == "switch_node":
            width, clearance = 0.30, 0.20
            via_d, via_h = 0.60, 0.30
            autoroute = False
            vias = False
            layers = req.layers or ("F.Cu",)
        elif req.z_se_ohm:
            width = width_for_z0(req.z_se_ohm, stack)
            clearance = 0.16

        via_d, via_h = max(via_d, stack.via_diameter), max(via_h, stack.via_drill)
        width, clearance = max(width, stack.track_min), max(clearance, floor_clear)
        if dp_w is not None:
            dp_w = max(dp_w, stack.track_min)  # a 90 ohm pair on 1.6 mm FR4 wants less than the fab can etch
        if dp_g is not None:
            dp_g = max(dp_g, stack.clearance_min)
        if req.autoroute is not None:
            autoroute = req.autoroute
        if req.vias is not None:
            vias = req.vias
        if req.layers is not None:
            layers = tuple(req.layers)

        if cls_name not in classes:
            classes[cls_name] = CompiledClass(
                name=cls_name,
                track_width_mm=width,
                clearance_mm=clearance,
                via_diameter_mm=via_d,
                via_drill_mm=via_h,
                diff_pair_width_mm=dp_w,
                diff_pair_gap_mm=dp_g,
            )
        else:
            c = classes[cls_name]
            c.track_width_mm = width
            c.clearance_mm = clearance
            c.via_diameter_mm = via_d
            c.via_drill_mm = via_h
            if dp_w is not None:
                c.diff_pair_width_mm = dp_w
                c.diff_pair_gap_mm = dp_g
        classes[cls_name].patterns.extend(req.nets)

        match_group = None
        if req.kind == "clock" and len(req.nets) > 1:
            match_group = req.nets
        if req.match_mm is not None and len(req.nets) > 1:
            match_group = req.nets
        if req.pair:
            autoroute = "diff_pair"

        max_mm = req.max_mm
        if max_mm is None and req.kind == "analog":
            max_mm = 25.0
        if max_mm is None and req.kind == "switch_node":
            max_mm = 8.0

        compiled_nets.append(
            CompiledNet(
                patterns=req.nets,
                class_name=cls_name,
                autoroute=autoroute,
                vias=vias,
                layers=layers,
                max_length_mm=max_mm,
                match_group=match_group,
                keep_clear_of=req.keep_clear_of,
                keep_clear_mm=req.keep_clear_mm,
                kind=req.kind,
                amps=req.amps if req.kind == "power" else None,
            )
        )
        if autoroute is False:
            skip.extend(req.nets)

        if req.keep_clear_of and req.keep_clear_mm:
            dru.append(
                DruRule(
                    name=f"{cls_name.lower()}_away_from_{_slug(req.keep_clear_of)}",
                    constraint=f"(constraint clearance (min {req.keep_clear_mm}mm))",
                    condition=(
                        f"A.NetClass == '{cls_name}' && "
                        f"B.NetName == '{req.keep_clear_of}'"
                    ),
                )
            )
        # max_mm is an airwire / cluster budget (pcbc check). Do not
        # emit a KiCad length rule: the maze path is longer than the
        # airwire, and Analog nets with different max_mm share one class.
        if dp_g is not None:
            dru.append(
                DruRule(
                    name=f"{cls_name.lower()}_pair_gap",
                    constraint=(
                        f"(constraint diff_pair_gap (min {max(0.1, dp_g - 0.03):.2f}mm) "
                        f"(opt {dp_g:.2f}mm))"
                    ),
                    condition=f"A.NetClass == '{cls_name}'",
                )
            )

    region_rects = resolve_regions(board, design.regions)
    keepouts = [
        replace(ko, box=resolve_keepout(ko, board, region_rects))
        for ko in design.keepouts
    ]

    krt = {
        "skip_patterns": skip,
        "planes": [{"net": n, "layer": l} for n, l in board.planes],
        "usb_pairs": [
            {"nets": list(n.patterns), "class": n.class_name}
            for n in compiled_nets
            if n.autoroute == "diff_pair"
        ],
        "length_match": [
            {"nets": list(n.match_group), "tolerance_mm": next(
                (r.match_mm or 2.0 for r in design.netreqs if r.nets == n.patterns),
                2.0,
            )}
            for n in compiled_nets
            if n.match_group
        ],
        "power_nets": [
            n
            for n, _ in board.planes
        ]
        + [
            p
            for req in design.netreqs
            if req.kind == "power"
            for p in req.nets
            if p not in {a for a, _ in board.planes}
        ],
        "sensitive": _sensitive_groups(compiled_nets, classes),
    }

    return CompiledJob(
        board_size_mm=board.size_mm,
        layers=board.layers,
        stackup=board.stackup,
        pcb=board.pcb,
        planes=board.planes,
        places=list(design.places),
        keepouts=keepouts,
        regions=list(design.regions),
        padding=board.padding,
        classes=list(classes.values()),
        nets=compiled_nets,
        dru=dru,
        skip_autoroute_patterns=skip,
        krt=krt,
    )


def _sensitive_groups(
    compiled_nets: list[CompiledNet],
    classes: dict[str, CompiledClass],
) -> list[dict]:
    by_kind: dict[str, dict] = {}
    for net in compiled_nets:
        if net.autoroute is not False:
            continue
        kind = net.kind or "analog"
        cls = classes.get(net.class_name)
        group = by_kind.setdefault(
            kind,
            {
                "kind": kind,
                "nets": [],
                "width": cls.track_width_mm if cls else 0.20,
                "clearance": cls.clearance_mm if cls else 0.20,
                "layers": list(net.layers) or ["F.Cu"],
            },
        )
        group["nets"].extend(net.patterns)
    ordered: list[dict] = []
    for kind in ("switch_node", "analog"):
        if kind in by_kind:
            ordered.append(by_kind.pop(kind))
    ordered.extend(by_kind.values())
    return ordered


def _slug(s: str) -> str:
    return "".join(ch.lower() if ch.isalnum() else "_" for ch in s).strip("_")
