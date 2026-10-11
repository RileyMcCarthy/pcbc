"""Native placement: `board.py` + each part's `.kicad_mod` -> `Place` poses and the place-level layout
objects, in memory. No board file is written or read, and no board text is searched.

`place(design, name=, base=)` does what the place stage always did — the seed grid, `Place()`
resolved in file order (`pcb_place.resolve_places`), fiducials, the silkscreen references
(`silk.silk_plan`: a reference with no free spot is hidden and noted; no part moves for it), the placement reports (`pcb_place.layout_report`,
`route_checks.route_aware_report`) — over the **Python model**: every part is its `.kicad_mod` read
once (`foot_native.lib_foot`) and posed by a `Pose` (`foot_native.PosedFoot`), starting at its seed
spot (`seed_feet`). The `Pose`s come from the resolved `Place` objects (`poses_of_places`), never from
reading a board text back (the third refutation round, native major: a shifted `(at` in the text used
to move the router's part while the `Place` model stayed put). It hands back:

- `poses`: one `Pose` per footprint, as KiCad will hold it (angle in (-180, 180]);
- `feet`: one `foot_native.PosedFoot` per pose — what `route_scene.build_scene`, the placer's reports
  and the router all read the parts from (`pcb_place.posed_feet_of` is the one reading of a part);
- `graphics`: the outline `gr_rect` (`Board(width, height)`) and every `Isolation(slot=True)` slot;
- `copper`: every keepout (`Keepout()`, the fiducials' mask keepouts) and every compiled rule area,
  as `Pour(keepout=...)` objects, field for field what KiCad keeps of such a zone, each with the uuid
  its geometry derives (`geometry_uuids`);
- `text`: the **emit base** (`emit_base`), built on first use and for emit only — the seed board with
  every decision above written onto it; `layout_emit.emit_board` renders the layout objects onto it.
  Nothing in `place` or in the route stage reads it (`docs/direction.md` §1: "no KiCad file is an
  intermediate in generation"; `tests/test_native_refuted3.py` traps every board-text reader).

A part with no `Place()` stays at its seed-grid spot and is named in `notes`.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path

from .compile import CompiledJob, compile_design
from .model import Copper, Design, Graphic, Pose
from .sexp import stable_uuid


@dataclass
class Placement:
    poses: list[Pose]
    graphics: list[Graphic]
    copper: list[Copper]
    job: CompiledJob
    report: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    applied: dict = field(default_factory=dict)
    error: str | None = None
    feet: tuple = ()  # `foot_native.posed_feet(design, poses)`: what the router and the placer read the parts from
    marks: list = field(default_factory=list)  # the orientation marks the silk pass generated (`orient`): `gr_*` on F.SilkS
    orient_errors: list = field(default_factory=list)  # facts: a required mark with no legal spot, a core mark naming another pin

    @property
    def orient_error(self) -> str | None:
        """The build's refusal for `orient_errors` (docs/direction.md §4c), or None."""
        return ("orientation: " + "; ".join(self.orient_errors)) if self.orient_errors else None
    _emit: tuple | None = field(default=None, repr=False)
    _text: str | None = field(default=None, repr=False)

    @property
    def text(self) -> str:
        """The emit base (`emit_base`), built on first use: for emit only. Nothing in `place` or the route
        stage reads it (`tests/test_native_scene.py` traps it)."""
        if self._text is None:
            design, name, base, fids, decisions = self._emit
            self._text = emit_base(design, self.job, name=name, base=base, fids=fids, decisions=decisions)
        return self._text


def kicad_angle(rot: float) -> float:
    """An angle as KiCad writes a footprint's: in (-180, 180] (270 is -90, 180 stays 180)."""
    r = float(rot) % 360.0
    if r > 180.0:
        r -= 360.0
    return 0.0 if abs(r) < 1e-12 else r


def copper_layer_order(n: int) -> tuple[str, ...]:
    """The copper layers in the order KiCad writes a multi-layer zone's `(layers ...)`."""
    return ("F.Cu", "B.Cu") if n <= 2 else ("F.Cu", "B.Cu", *[f"In{i}.Cu" for i in range(1, n - 1)])


def _zone_layers(want, n: int) -> tuple[str, ...]:
    wide: list[str] = []
    for la in want:
        wide += ["F.Cu", "B.Cu"] if la == "F&B.Cu" else list(copper_layer_order(n)) if la == "*.Cu" else [la]
    have = copper_layer_order(n)
    return tuple(la for la in have if la in wide)


def _rect_points(box) -> tuple:
    x0, y0, x1, y1 = (round(float(v), 2) for v in box)
    return ((x0, y0), (x1, y0), (x1, y1), (x0, y1))


def geometry_uuids(copper: list[Copper], board: str) -> list[Copper]:
    """Every object without a uuid of its own given the one its geometry derives:
    `stable_uuid(board, "cu", repr(signature), n)`, `n` counting objects of one signature in a fixed
    order — the rule the step-1 bridge re-keyed every router object by, so the same copper gets the
    same uuid and locking one object renumbers nothing else."""
    from .gen import signature

    seen: dict[tuple, int] = {}
    out: list[Copper] = []
    order = sorted(range(len(copper)), key=lambda i: repr(signature(copper[i])))
    got: dict[int, Copper] = {}
    for i in order:
        c = copper[i]
        if c.uuid:
            got[i] = c
            continue
        sig = signature(c)
        n = seen.get(sig, 0)
        seen[sig] = n + 1
        uid = stable_uuid(board, "cu", repr(sig), n)
        got[i] = replace(c, uuid=uid, id=c.id or f"{c.kind}-{uid[:8]}")
    out = [got[i] for i in range(len(copper))]
    return out


def _keepout_pour(name: str, layers: tuple[str, ...], box, keep: dict) -> Copper:
    """A rule area as KiCad keeps the zone `apply.py` wrote: its defaults filled the way KiCad 10's load
    fills them (clearance 0, min_thickness 0.25, thermal 0.5/0.5, island removal 0, placement off).
    Its uuid is its geometry's (`geometry_uuids`)."""
    return Copper(
        "pour",
        "",
        None,
        layer=None,
        layers=layers,
        points=_rect_points(box),
        name=name,
        keepout=keep,
        placement={"enabled": False, "sheetname": ""},
        clearance=0.0,
        min_thickness=0.25,
        filled=False,
        thermal_gap=0.5,
        thermal_bridge_width=0.5,
        island_removal_mode=0,
        source="gen",
    )


def place_objects(job: CompiledJob, name: str, outline=None) -> tuple[list[Graphic], list[Copper]]:
    """The outline, the slots, the keepouts and the rule areas of this compiled job, as layout objects. `outline`
    (x0, y0, x1, y1) is the rectangle they are drawn to; the job's own by default (docs/board-outline.md B.3 step 5)."""
    from .apply import SLOT_WEB_MM, SLOT_WIDTH_MM

    x0, y0, x1, y1 = outline if outline is not None else job.outline
    uid = stable_uuid(name, "edge")
    graphics = [
        Graphic(
            "gr_rect",
            f"rect-{uid[:8]}",
            {"start": (float(x0), float(y0)), "end": (float(x1), float(y1)), "radius": None, "width": 0.05, "stroke_type": "default", "stroke_color": None, "fill": False, "locked": False, "layer": "Edge.Cuts", "layers": None, "solder_mask_margin": None, "net": None, "uuid": uid},
            source="gen",
        )
    ]
    copper: list[Copper] = []
    for ko in job.keepouts:
        keep = {
            "tracks": "not_allowed" if ("copper" in ko.no or "track" in ko.no) else "allowed",
            "vias": "not_allowed" if "via" in ko.no else "allowed",
            "pads": "allowed",
            "copperpour": "not_allowed" if "copper" in ko.no else "allowed",
            "footprints": "allowed",
        }
        copper.append(_keepout_pour(ko.name, _zone_layers(("F&B.Cu", "In1.Cu", "In2.Cu"), job.layers), ko.box, keep))
    if job.constraints is not None:
        allowed = {k: "allowed" for k in ("tracks", "vias", "pads", "copperpour", "footprints")}
        cs = job.constraints
        if outline is not None and job.derived is not None:
            from .constraints import respan_rule_areas

            cs = respan_rule_areas(cs, (x0, y0, x1, y1))
        for area in cs.rule_areas:
            copper.append(_keepout_pour(area.name, _zone_layers(area.layers, job.layers), area.box, dict(allowed)))
        areas = {a.name: a for a in cs.rule_areas}
        for spec in job.constraints.isolation_specs:
            sname = f"ISO_{spec.req.a}_{spec.req.b}"
            area = areas.get(sname)
            if area is None or not spec.req.slot:
                continue
            ax0, ay0, ax1, ay1 = area.box
            # The slot is recomputed from the outline with its webs (docs/board-outline.md B.3 step 5).
            if spec.axis == "x":
                cx = (ax0 + ax1) / 2
                a, b = (cx - SLOT_WIDTH_MM / 2, y0 + SLOT_WEB_MM), (cx + SLOT_WIDTH_MM / 2, y1 - SLOT_WEB_MM)
            else:
                cy = (ay0 + ay1) / 2
                a, b = (x0 + SLOT_WEB_MM, cy - SLOT_WIDTH_MM / 2), (x1 - SLOT_WEB_MM, cy + SLOT_WIDTH_MM / 2)
            suid = stable_uuid("slot", sname)
            graphics.append(
                Graphic(
                    "gr_rect",
                    f"rect-{suid[:8]}",
                    {"start": (round(a[0], 4), round(a[1], 4)), "end": (round(b[0], 4), round(b[1], 4)), "radius": None, "width": 0.05, "stroke_type": "default", "stroke_color": None, "fill": False, "locked": False, "layer": "Edge.Cuts", "layers": None, "solder_mask_margin": None, "net": None, "uuid": suid},
                    source="gen",
                )
            )
    return graphics, geometry_uuids(copper, name)


def seed_columns(design: Design) -> int:
    """The seed grid's columns: `outline width // 8` of a declared outline, 5 with none (derived mode refuses a part
    with no Place(), so no grid spot is ever a final pose there; docs/board-outline.md B.5-2)."""
    from .layout import declared_outline

    rect, _src, _line = declared_outline(design)
    w = rect[2] - rect[0] if rect is not None else 40.0
    return max(int(w // 8), 1)


def seed_feet(design: Design) -> tuple:
    """Every part at its seed pose — the grid `seed.emit_pcb` lays them on, in `design.instances`
    order, `(5 + 8 * col, 5 + 8 * row)` with `Board(width) // 8` columns, turned 0, on F.Cu — each with
    its `.kicad_mod` (`foot_native.lib_foot`). The Python model the place stage searches over."""
    from .foot_native import PosedFoot, lib_foot
    from .footprints import footprint_path
    from .pcb_place import _pins_of

    if design.board is None:
        raise ValueError("no Board()")
    cols = seed_columns(design)
    nets_of = _pins_of(design)
    out = []
    for i, inst in enumerate(design.instances):
        at = (5.0 + (i % cols) * 8.0, 5.0 + (i // cols) * 8.0)
        out.append(PosedFoot(inst.ref, at, 0.0, "F.Cu", lib_foot(footprint_path(inst.part)), tuple(sorted(nets_of.get(inst.ref, {}).items()))))
    return tuple(out)


def _written(v: float, fmt: str) -> float:
    """A number as the board text writes it and KiCad reads it back (`(at {x:.4f} {y:.4f} {rot:g})`)."""
    return float(format(v, fmt))


def poses_of_places(design: Design, job: CompiledJob, seeds, fids) -> tuple[list[Pose], list[str], list, dict]:
    """(one `Pose` per footprint, the Place() refs no part answers to, every Place() as resolved, each
    footprint's angle as the board text writes it — `270`, where its `Pose` holds KiCad's `-90`) —
    from the resolved `Place` objects (`job.places`), the seed poses of the parts no Place() names, and
    the fiducials' spots. The numbers are the ones the emitted board holds: a position to 4 decimals and
    an angle to `:g`, as KiCad reads them back (`poses` equal `layout.gen.py`'s and the board's)."""
    from .layout import resolve_place, resolve_regions
    from .model import BoardSpec

    from .pcb_place import _board_of

    board = _board_of(job)
    regions = resolve_regions(board, job.regions)
    libs = {pf.ref: pf.lib for pf in seeds}
    resolved = []
    by_ref = {}
    for p in job.places:
        rp = resolve_place(p, board, libs.get(p.ref), regions)
        resolved.append(rp)
        by_ref[p.ref] = rp
    missing = [p.ref for p in job.places if p.ref not in libs]
    poses: list[Pose] = []
    written: dict[str, float] = {}
    for pf in seeds:
        rp = by_ref.get(pf.ref)
        if rp is not None and rp.at is not None:
            at = (round(_written(rp.at[0], ".4f"), 6), round(_written(rp.at[1], ".4f"), 6))
            written[pf.ref] = _written(float(rp.rot), "g")
            poses.append(Pose(pf.ref, at, kicad_angle(written[pf.ref]), "F.Cu" if rp.side == "F" else "B.Cu", bool(rp.locked) or pf.lib.locked, source="gen"))
        else:
            written[pf.ref] = pf.rot
            poses.append(Pose(pf.ref, (round(pf.at[0], 6), round(pf.at[1], 6)), kicad_angle(pf.rot), pf.layer, pf.lib.locked, source="gen"))
    for ref, x, y in fids:
        written[ref] = 0.0
        poses.append(Pose(ref, (round(_written(x, ".4f"), 6), round(_written(y, ".4f"), 6)), 0.0, "F.Cu", False, source="gen"))
    return sorted(poses, key=lambda p: p.ref), missing, resolved, written


def silk_parts(posed, written: dict, order: list[str], design: Design | None = None) -> list[dict]:
    """The parts as the silkscreen pass reads them (`silk.silk_part`), in board order (`order`: the
    seed's instance order, then the fiducials), from each part's `.kicad_mod` and its pose — the angle
    as the board text writes it (`:g`); each pad's mask opening at its board angle (library + footprint).
    With `design`, each of its parts also as the orientation rules read it (`orient.part_of`: its symbol
    pins, kind and prefix from `board.py`, its body from its `.kicad_mod`)."""
    from .orient import part_of, pins_of
    from .silk import pad_box, silk_part

    by = {pf.ref: pf for pf in posed}
    insts = {i.ref: i for i in design.instances} if design is not None else {}
    out = []
    for ref in order:
        pf = by[ref]
        at = (pf.at[0], pf.at[1], written[ref])
        pads = [b for b in (pad_box(sp, at) for sp in pf.lib.pads) if b is not None]
        op = None
        inst = insts.get(ref)
        if inst is not None:
            op = part_of(ref, at, pf.lib.pads, body=pf.lib.body, pins=pins_of(inst), kind=inst.part.kind, prefix=inst.part.prefix)
            op["layer"] = pf.layer
        out.append(silk_part(ref, at, pf.lib.silk_crtyd, pf.lib.silk_shapes, pads, orient=op))
    return out


def core_silk(design: Design) -> list[tuple[str, dict]]:
    """Every `layout.core.py` drawing on `F.SilkS` (a circle, polygon, line, rectangle or arc) as (its line, a world
    silk shape): the silk pass keeps clear of it, and the orientation rules count it as a mark."""
    from .layout_prims import flat_points
    from .orient import graphic_shape
    from .silk import _arc_points
    import math

    out = []
    for g in design.graphics:
        f = g.f
        if g.source != "core" or f.get("layer") != "F.SilkS" or g.kind not in ("gr_circle", "gr_poly", "gr_line", "gr_rect", "gr_arc"):
            continue
        if g.kind == "gr_circle":
            (cx, cy), (ex, ey) = f["center"], f["end"]
            r = math.hypot(ex - cx, ey - cy)
            pts = tuple((cx + r * math.cos(2 * math.pi * k / 32), cy + r * math.sin(2 * math.pi * k / 32)) for k in range(32))
        elif g.kind == "gr_poly":
            pts = tuple(flat_points(f["pts"]))
        elif g.kind == "gr_rect":
            (x0, y0), (x1, y1) = f["start"], f["end"]
            pts = ((x0, y0), (x1, y0), (x1, y1), (x0, y1))
        elif g.kind == "gr_arc":
            pts = tuple(_arc_points(f["start"], f["mid"], f["end"]))
        else:
            pts = (f["start"], f["end"])
        tag = {"gr_circle": "fp_circle", "gr_poly": "fp_poly", "gr_rect": "fp_rect", "gr_arc": "fp_arc", "gr_line": "fp_line"}[g.kind]
        out.append((g.where or g.id or "layout.core.py", graphic_shape(tag, pts, float(f.get("width") or 0.0), f.get("fill") is True)))
    return out


def mark_graphics(marks: list[dict], name: str) -> list[Graphic]:
    """Each generated orientation mark (`orient.plan`'s) as the board primitive it is: a filled `gr_circle` or
    `gr_poly` on F.SilkS, every field explicit, its uuid derived from the board, the part and the pad."""
    out = []
    for m in marks:
        prim = m["prim"]
        uid = stable_uuid(name, "orient", m["ref"], m["pad"])
        common = {"width": prim["width"], "stroke_type": "solid", "stroke_color": None, "fill": True, "locked": False, "layer": "F.SilkS", "layers": None, "solder_mask_margin": None, "net": None, "uuid": uid}
        if prim["kind"] == "gr_circle":
            f = {"center": prim["center"], "end": prim["end"], **common}
        else:
            f = {"pts": tuple(prim["pts"]), **common}
        out.append(Graphic(prim["kind"], f"{prim['kind'].replace('gr_', '')}-{uid[:8]}", f, source="gen"))
    return out


def place(design: Design, *, name: str, base: Path | None = None) -> Placement:
    """See the module doc. `base` is the directory the emitted board will sit in (one level under
    `layout/<name>/`): the footprints' 3D model paths are written relative to it."""
    from .foot_native import posed_feet
    from .model import KeepoutSpec
    from .pcb_place import FID_HALF, layout_report, resolve_places
    from .route_checks import route_aware_report
    from .silk import silk_plan
    from .stackup import get_stackup, silk_limits

    job = compile_design(design)
    seeds = seed_feet(design)
    specs = list(job.places)
    declared = {s.ref for s in specs}
    # The silkscreen never moves a part: a reference with no free spot is not printed, and the note
    # says so (`silk.silk_plan`). Until 2026-10-03 a part whose reference found no room was placed again
    # with 0.6 mm more gap, up to three rounds, which spread the board for a label.
    from .outline import OutlineError

    try:
        job.places, moves, fids = resolve_places(design, job, seeds)
    except OutlineError as exc:
        # A B.5 fact (docs/board-outline.md): the derived outline cannot be made as layout.core.py is written.
        return Placement(poses=[], graphics=[], copper=[], job=job, error=str(exc))
    if job.derived is not None and job.constraints is not None:
        # The isolation corridors run to the routing box R while placing and routing (B.3 step 3).
        from .constraints import respan_rule_areas

        job.constraints = respan_rule_areas(job.constraints, job.outline)
    # The router keeps off a fiducial's copper dot, not its 2 mm mask opening: a track through the
    # opening is a solder-mask bridge. A keepout the size of its courtyard.
    for ref, x, y in fids:
        job.keepouts.append(KeepoutSpec(name=f"{ref}_mask", box=(x - FID_HALF, y - FID_HALF, x + FID_HALF, y + FID_HALF), no=("copper", "via")))
    poses, missing, resolved, written = poses_of_places(design, job, seeds, fids)
    feet = posed_feet(design, poses)
    order = [pf.ref for pf in seeds] + [ref for ref, _x, _y in fids]
    # The silkscreen stays inside the placement's own fit in derived mode (the routed outline only grows past it,
    # docs/board-outline.md B.3), and inside the declared outline otherwise.
    silk_box = (job.derived or {}).get("fit") or job.outline
    decisions, silk = silk_plan(silk_parts(feet, written, order, design), silk_box, silk_limits(get_stackup(job.stackup)), core_silk(design))
    # The placed board's Edge.Cuts: the declared outline, or the placement-only fit (`pcbc pcb`, B.3); the route stage
    # draws the derived one again from O after the fit.
    graphics, copper = place_objects(job, name, outline=job.derived.fit if job.derived is not None else None)
    report = list(moves) + layout_report(design, job, feet)
    notes = list(silk.get("notes", []))
    route_moves, route_notes = route_aware_report(design, job, feet)
    report += route_moves
    notes += route_notes
    if job.constraints is not None:
        from .pcb_place import posed_feet_of
        from .place_solve import facing_notes, locks_of, needs_of

        notes += facing_notes(list(posed_feet_of(job, feet).values()), needs_of(job.constraints), locks_of(job))
    for p in poses:
        if p.ref not in declared and not p.ref.startswith("FID"):
            notes.append(f"{p.ref} has no Place() in layout.core.py; it sits at the default grid spot ({p.at[0]:g}, {p.at[1]:g})")
    applied = {
        "placed": [p.ref for p in job.places if p.ref not in missing],
        "resolved": [{"ref": p.ref, "at": list(p.at), "rot": p.rot} for p in resolved if p.at is not None],
        "missing": missing,
        "classes": [c.name for c in job.classes],
    }
    applied["orientation"] = silk.get("orient_lines", [])
    out = Placement(poses=poses, graphics=graphics, copper=copper, job=job, report=report, notes=notes, applied=applied, feet=feet, marks=mark_graphics(silk.get("orient_marks", []), name))
    out._emit = (design, name, base, list(fids), dict(decisions))
    # A polarised part with no mark the fab can read is not a fab-ready board (docs/direction.md §4c): the build's place
    # stage refuses it (`orient_error`); `place` itself stays a placement (its outline and pose facts are `error`).
    out.orient_errors = list(silk.get("orient_errors", []))
    if missing:
        out.error = "missing refs: " + ", ".join(missing)
    else:
        # Decision 4: a declared placement that blocks every exit of a path's end pad is an error before routing.
        from .pcb_place import corridor_errors, posed_feet_of

        blocked = corridor_errors(job, posed_feet_of(job, feet))
        if blocked:
            out.error = "a path's exit is blocked by declared placement: " + "; ".join(blocked)
    return out


def foreign_uuids(design: Design, placement: "Placement", *, name: str) -> dict[str, str]:
    """`{uuid: what carries it}` for every uuid the emitted board's footprints will carry, from the
    Python model (the third refutation round: the route stage parsed the whole placed board text for
    this, on every build): each footprint's own (`stable_uuid(board, ref)`), its drawing's
    (`LibFoot.uuids`), the properties `seed.instantiate` adds and the pads it binds, and each
    fiducial's (`fab._fiducial_sexp`). A core line may not take one (`layout_job.validate_core`).
    In board order — the seed's instance order, then the fiducials — so a uuid two parts share (one
    `.kicad_mod`, two parts) is named by the first, as a reader of the board would."""
    from .seed import pad_nets

    from .silk import silk_gone_uuids

    out: dict[str, str] = {}
    insts = {i.ref: i for i in design.instances}
    by = {pf.ref: pf for pf in placement.feet}
    # A library silk drawing the silkscreen pass trimmed or left out is not on the board as the library has it.
    gone = silk_gone_uuids(placement._emit[4]) if placement._emit is not None else {}
    for inst in design.instances:
        pf = by.get(inst.ref)
        if pf is None:
            continue
        owner = f"footprint {inst.ref} (placed from layout.core.py)"
        mine = [stable_uuid(name, inst.ref), *(u for u in pf.lib.uuids if u not in gone.get(inst.ref, ()))]
        value = inst.value or inst.part.value or inst.part.mpn
        for prop, v in (("Reference", inst.ref), ("Value", value), ("Mpn", inst.part.mpn), ("LCSC", inst.part.lcsc), ("Manufacturer", inst.part.manufacturer)):
            if v and prop not in pf.lib.props:
                mine.append(stable_uuid("prop", prop, v))
        nets = pad_nets(insts[inst.ref])
        for sp in pf.lib.pads:
            if nets.get(sp.num) and not sp.net:
                mine.append(stable_uuid(name, inst.ref, "pad", sp.num))
        for u in mine:
            out.setdefault(u, owner)
    if placement._emit is not None:
        from .silk import silk_uuids

        # The library silk lines the silkscreen pass cut into pieces carry uuids of their own.
        for u, ref in silk_uuids(placement._emit[4]).items():
            out.setdefault(u, f"footprint {ref} (placed from layout.core.py)")
    for ref in sorted(r for r in by if r.startswith("FID") and r not in insts):
        for u in (stable_uuid("fiducial", ref), stable_uuid("fiducial", ref, "ref"), stable_uuid("fiducial", ref, "crtyd"), stable_uuid("fiducial", ref, "pad")):
            out.setdefault(u, f"footprint {ref} (a fiducial the build places)")
    return out


def emit_base(design: Design, job: CompiledJob, *, name: str, base: Path | None, fids, decisions: dict) -> str:
    """The board text `layout_emit.emit_board` renders the layout objects onto: the seed board
    (`seed.emit_pcb`), the fiducials appended, every `Place()` written onto its footprint
    (`apply._apply_places`), every reference where the silkscreen pass put it (`silk.apply_silk`).
    **Writes only**: every number in it was decided on the Python model by `place`; this is built for
    emit and nothing searches it (`docs/direction.md` §1)."""
    from .apply import _apply_places
    from .fab import insert_fiducials
    from .layout_emit import strip_layout
    from .seed import emit_pcb
    from .silk import apply_silk

    text = strip_layout(emit_pcb(design, name=name, base=base))
    if fids:
        text, _ = insert_fiducials(text, job.outline, spots=fids)
    text, _missing, _resolved = _apply_places(text, job)
    return apply_silk(text, decisions)


def emitted_text(design: Design, placement: Placement, *, name: str) -> str:
    """The place-only board, rendered in memory: the base with the place-level objects on it. What
    `check.check_text` judges before any routing (a bad placement never costs a route)."""
    from .layout_emit import emit_board, stamp

    return emit_board(placement.text, placement.poses, stamp(design, placement.copper), [*placement.graphics, *placement.marks], design=design, board=name)
