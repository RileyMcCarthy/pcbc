"""Compile Place/NetReq intent into KiCad geometry and engine flags.

Since R1 the numbers live in `constraints.compile_constraints` (docs/r1-design.md section A.3):
`CompiledClass`, `CompiledNet` and the rule list are projections of the `ConstraintSet`, and every
existing consumer (`route.py`, `fanout.py`, `copper.py`, `apply.py`, `seed.py`) reads them unchanged.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace

from . import dru as _dru
from .constraints import (  # noqa: F401  (re-exported for today's importers)
    _KIND_CLASS,
    CompiledClass,
    Constraint,
    ConstraintSet,
    Derived,
    DruRule,
    RuleArea,
    Source,
    _canary_net,
    compile_constraints,
    slug,
)
from .layout import resolve_keepout, resolve_regions
from .model import Design, KeepoutSpec, PlaceSpec, RegionSpec

_slug = slug


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
    constraints: ConstraintSet | None = None

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
            "constraints": self.constraints.to_dict() if self.constraints is not None else None,
            "rule_areas": [asdict(a) for a in self.constraints.rule_areas] if self.constraints is not None else [],
        }


def compile_design(design: Design) -> CompiledJob:
    if design.board is None:
        raise ValueError("design has no Board()")
    board = design.board
    cs = compile_constraints(design)

    classes = [replace(c, patterns=list(c.patterns)) for c in cs.classes]
    class_by_name = {c.name: c for c in classes}
    compiled_nets: list[CompiledNet] = []
    skip: list[str] = []
    tolerances: list[float] = []

    def project(patterns: tuple[str, ...], c: Constraint, explicit_match: bool) -> None:
        if c.group is not None and c.group.kind == "bus":
            match_group: tuple[str, ...] | None = c.group.members
            tolerance = c.group.match_mm if c.group.match_mm is not None else 2.0
        elif c.pair is not None and explicit_match:
            match_group = patterns
            tolerance = c.pair.skew_mm.value
        else:
            match_group, tolerance = None, 2.0
        compiled_nets.append(
            CompiledNet(
                patterns=patterns,
                class_name=c.class_name,
                autoroute=c.autoroute,
                vias=c.via.allowed,
                layers=c.layers,
                max_length_mm=c.airwire_max_mm,
                match_group=match_group,
                keep_clear_of=c.keep_away[0].other if c.keep_away else None,
                keep_clear_mm=c.keep_away[0].mm if c.keep_away else None,
                kind=c.kind,
                amps=c.current.amps if c.kind == "power" and c.current is not None else None,
            )
        )
        tolerances.append(tolerance)
        if c.autoroute is False:
            skip.extend(patterns)

    by_req: dict[int, Constraint] = {}
    for c in cs.constraints:
        if c.req_index >= 0 and c.req_index not in by_req:
            by_req[c.req_index] = c
    for i, req in enumerate(design.netreqs):
        c = by_req.get(i)
        if c is None:
            continue  # every net of this NetReq was claimed by an earlier line (a refusal says so)
        project(req.nets, c, req.match_mm is not None and len(req.nets) > 1)
        # The user's literals and the kind as written ("digital"), as today's projection carried them.
        compiled_nets[-1].kind = req.kind
        compiled_nets[-1].amps = req.amps if req.kind == "power" else None
        compiled_nets[-1].max_length_mm = req.max_mm if req.max_mm is not None else c.airwire_max_mm
    seen = {n for net in compiled_nets for n in net.patterns}
    for c in cs.constraints:
        if c.req_index >= 0 or c.net in seen:
            continue
        if c.pair is not None:
            pair = (c.net, c.pair.partner) if c.net < c.pair.partner else (c.pair.partner, c.net)
            cls = class_by_name.get(c.class_name)
            patterns = tuple(cls.patterns) if cls is not None and set(cls.patterns) == set(pair) else pair
            project(patterns, c, True)
            seen.update(patterns)
        elif c.group is not None and c.group.kind == "bus" and all(m not in seen for m in c.group.members):
            project(c.group.members, c, True)
            seen.update(c.group.members)

    region_rects = resolve_regions(board, design.regions)
    keepouts = [replace(ko, box=resolve_keepout(ko, board, region_rects)) for ko in design.keepouts]

    krt = {
        "skip_patterns": skip,
        "planes": [{"net": n, "layer": l} for n, l in board.planes],
        "usb_pairs": [
            {"nets": list(n.patterns), "class": n.class_name}
            for n in compiled_nets
            if n.autoroute == "diff_pair"
        ],
        "length_match": [
            {"nets": list(n.match_group), "tolerance_mm": tol}
            for n, tol in zip(compiled_nets, tolerances)
            if n.match_group
        ],
        "power_nets": [n for n, _ in board.planes]
        + [
            p
            for req in design.netreqs
            if req.kind == "power"
            for p in req.nets
            if p not in {a for a, _ in board.planes}
        ],
        "sensitive": _sensitive_groups(compiled_nets, class_by_name),
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
        classes=classes,
        nets=compiled_nets,
        dru=_dru.rules(cs),
        skip_autoroute_patterns=skip,
        krt=krt,
        constraints=cs,
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


__all__ = [
    "CompiledClass",
    "CompiledJob",
    "CompiledNet",
    "Constraint",
    "ConstraintSet",
    "Derived",
    "DruRule",
    "RuleArea",
    "Source",
    "compile_constraints",
    "compile_design",
]
