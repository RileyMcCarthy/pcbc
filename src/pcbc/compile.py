"""Compile Place/NetReq intent into KiCad geometry and engine flags.

Since R1 the numbers live in `constraints.compile_constraints` (docs/r1-design.md section A.3):
`CompiledClass`, `CompiledNet` and the rule list are projections of the `ConstraintSet`, and every
consumer (`route_cost.py`, `fanout.py`, `copper.py`, `apply.py`, `seed.py`) reads them.
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
from .layout import declared_outline, frame_board, resolve_keepout, resolve_regions, unframed_lengths
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


@dataclass
class CompiledJob:
    # The declared outline's (width, height) (`Outline()` in layout.core.py), or None: derived (docs/board-outline.md B).
    board_size_mm: tuple[float, float] | None
    layers: int
    stackup: str
    pcb: str | None
    planes: tuple[tuple[str, str], ...]
    places: list[PlaceSpec]
    keepouts: list[KeepoutSpec]
    regions: list[RegionSpec]
    classes: list[CompiledClass]
    nets: list[CompiledNet]
    dru: list[DruRule]
    constraints: ConstraintSet | None = None
    # The board outline every reader of the board's extent reads (docs/board-outline.md PR 2), (x0, y0, x1, y1) in the
    # frame: the declared `Outline()`, or, in derived mode, None until placement sets the routing box R, and O after
    # the fit. `outline_source` is "declared" or "derived"; `outline_line` the Outline()'s layout.core.py line.
    outline: tuple[float, float, float, float] | None = None
    outline_source: str = "declared"
    outline_line: int = 0
    # Derived mode: what placement fixed (sides, lines, the placement box P, ...), filled by `pcb_place.resolve_places`.
    derived: dict | None = None

    def to_dict(self) -> dict:
        return {
            "board_size_mm": list(self.board_size_mm) if self.board_size_mm is not None else None,
            "layers": self.layers,
            "stackup": self.stackup,
            "pcb": self.pcb,
            "planes": [list(p) for p in self.planes],
            "places": [{k: v for k, v in asdict(p).items() if k not in ("source", "line")} for p in self.places],
            "keepouts": [{k: v for k, v in asdict(ko).items() if k not in ("source", "line")} for ko in self.keepouts],
            "regions": [{k: v for k, v in asdict(r).items() if k not in ("source", "line")} for r in self.regions],
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
            "constraints": self.constraints.to_dict() if self.constraints is not None else None,
            "rule_areas": [asdict(a) for a in self.constraints.rule_areas] if self.constraints is not None else [],
        }


def _refuse_unframed(design: Design) -> None:
    """B.5-1 for Regions and Keepouts (docs/board-outline.md): with no `Outline()`, a box is given in frame
    coordinates (`left`, `top`, `width`, `height`, or `box=`); `right`/`bottom`, `auto` and `%` measure from an edge the
    placement itself derives. Facts, naming the core line."""
    fails: list[str] = []
    for spec in [*design.regions, *design.keepouts]:
        kind = "Region" if isinstance(spec, RegionSpec) else "Keepout"
        if kind == "Keepout" and spec.box is not None and not spec.has_css():
            continue
        if spec.parent:
            continue  # its parent Region's box, which is checked itself
        why = unframed_lengths(spec, need_size=True)
        if why:
            where = f"layout.core.py:{spec.line}: " if spec.line else ""
            fails.append(f"{where}{kind}({spec.name!r}) {why}")
    if fails:
        raise ValueError("; ".join(fails))


def compile_design(design: Design) -> CompiledJob:
    if design.board is None:
        raise ValueError("design has no Board()")
    board = design.board
    cs = compile_constraints(design)

    classes = [replace(c, patterns=list(c.patterns)) for c in cs.classes]
    class_by_name = {c.name: c for c in classes}
    compiled_nets: list[CompiledNet] = []
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
            )
        )
        tolerances.append(tolerance)

    by_req: dict[int, Constraint] = {}
    for c in cs.constraints:
        if c.req_index >= 0 and c.req_index not in by_req:
            by_req[c.req_index] = c
    # Board order (docs/physics-subjects.md PR 6): a Pair line takes its place among the NetReq lines.
    entries: list[tuple[int, int, int, Constraint | None]] = [(req.line, 0, i, None) for i, req in enumerate(design.netreqs)]
    entries += [(c.line, 1, k, c) for k, c in enumerate(cs.constraints) if c.req_index < 0 and (c.pair is not None or (c.group is not None and c.group.kind == "bus"))]
    pair_reqs = {pr.line: pr for pr in design.pairs}
    seen: set[str] = set()
    for _line, what, i, pc in sorted(entries, key=lambda e: (e[0], e[1], e[2])):
        if what == 0:
            req = design.netreqs[i]
            c = by_req.get(i)
            if c is None:
                continue  # every net of this NetReq was claimed by an earlier line (a refusal says so)
            project(req.nets, c, False)  # a NetReq matches nothing: match_mm= is a Pair or Bus field (physics-subjects PR 6)
            # The user's literals and the kind as written ("digital"), as today's projection carried them.
            compiled_nets[-1].kind = req.kind
            compiled_nets[-1].max_length_mm = req.max_mm if req.max_mm is not None else c.airwire_max_mm
            seen.update(n for n in compiled_nets[-1].patterns)
            continue
        c = pc
        assert c is not None
        if c.net in seen:
            continue
        if c.pair is not None:
            pr = pair_reqs.get(c.line)
            if pr is not None and pr.kind is not None:
                patterns: tuple[str, ...] = (pr.p, pr.n)  # as the line wrote them
            else:
                pair = (c.net, c.pair.partner) if c.net < c.pair.partner else (c.pair.partner, c.net)
                cls = class_by_name.get(c.class_name)
                patterns = tuple(cls.patterns) if cls is not None and set(cls.patterns) == set(pair) else pair
            # A preset pair is matched only when its line writes match_mm= (as the usb_hs NetReq was); a bare Pair always.
            project(patterns, c, pr is None or pr.kind is None or "match_mm" in pr.written)
            if pr is not None and pr.kind is not None:
                compiled_nets[-1].kind = pr.kind
            seen.update(patterns)
        elif c.group is not None and c.group.kind == "bus" and all(m not in seen for m in c.group.members):
            project(c.group.members, c, True)
            seen.update(c.group.members)

    rect, source, line = declared_outline(design)
    frame = frame_board(board, rect)
    if rect is None:
        _refuse_unframed(design)
    region_rects = resolve_regions(frame, design.regions)
    keepouts = [replace(ko, box=resolve_keepout(ko, frame, region_rects)) for ko in design.keepouts]

    return CompiledJob(
        board_size_mm=(round(rect[2] - rect[0], 6), round(rect[3] - rect[1], 6)) if rect is not None else None,
        outline=rect,
        outline_source=source,
        outline_line=line,
        layers=board.layers,
        stackup=board.stackup,
        pcb=board.pcb,
        planes=board.planes,
        places=list(design.places),
        keepouts=keepouts,
        regions=list(design.regions),
        classes=classes,
        nets=compiled_nets,
        dru=_dru.rules(cs),
        constraints=cs,
    )


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
