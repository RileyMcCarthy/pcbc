"""`layout/<name>/schematic.gen.py`: every object the schematic auto-layout resolved, one line each (docs/schematic-layout.md
§4). Build output: never edited, never read back. Any line is a valid `schematic.core.py` line, copied verbatim: a copied
line is a lock, and copying every line reproduces the sheet byte for byte (the fixed point).

Order: the sheet, the nets whose wire limit differs from the sheet's, then each region in grid order; under each region
its parts in the order its SchRegion line lists them (a declared region's as written, an inferred one's in placement
order), each followed by its text, labels and power symbols. Then the drawn sheet, one KiCad primitive per line
(`sch_prims`, docs/schematic-layout.md PR 6): what the sch stage emits schematic.kicad_sch from. Those lines are no
locks (wires, junctions, no-connect marks and PWR_FLAGs follow from the lock lines and `board.py`'s netlist).
"""

from __future__ import annotations

from .sch_core import SCH_CORE_FILE

GEN_FILE = "schematic.gen.py"


def _n(v: float) -> str:
    s = f"{float(v):.4f}".rstrip("0").rstrip(".")
    return "0" if s in ("", "-0") else s


def _pt(p) -> str:
    return f"({_n(p[0])}, {_n(p[1])})"


def _q(s) -> str:
    return '"' + str(s).replace("\\", "\\\\").replace('"', '\\"') + '"'


def _pin(part, pin) -> str:
    names = [p.name for p in part.pins]
    return f"{part.ref}.{pin.name if names.count(pin.name) == 1 else pin.number}"


def _place_form(spec) -> str:
    kw = []
    kind = spec.anchor_kind()
    if kind == "to":
        kw.append(f"to={_q(spec.to)}")
        if spec.pin:
            kw.append(f"pin={_q(spec.pin)}")
    elif kind == "along":
        kw.append(f"along={_q(spec.along)}")
        if spec.align:
            kw.append(f"align={_q(spec.align)}")
    elif kind == "below":
        kw.append(f"below={_q(spec.below)}")
    if kind in ("to", "along") and spec.side:
        kw.append(f"side={_q(spec.side)}")
    if kind in ("to", "along") and spec.gap is not None:
        kw.append(f"gap={_n(spec.gap)}")
    if spec.rotate_set:
        kw.append(f"rotate={int(spec.rot)}")
    if spec.mirror:
        kw.append(f"mirror={_q(spec.mirror)}")
    return f"SchPlace({_q(spec.ref)}{''.join(', ' + k for k in kw)})"


def gen_text(design, report: dict, name: str, vector_line: str = "") -> str:
    """The gen file of the pass `emit_from_design` kept (`report` carries it)."""
    layout, sheet, parts = report["_layout"], report["_sheet"], report["_parts"]
    out = [
        f"# {GEN_FILE} - written by `pcbc build`'s sch stage for {name}: the schematic auto-layout. BUILD OUTPUT.",
        "# Never edit this file and never read it as design input: it is rewritten on every build.",
        f"# To lock an object, copy its line into {SCH_CORE_FILE} next to board.py and rebuild. A SchPlace with at=",
        "# needs its SchRegion line too. A '# auto:' comment is itself a core line (an attach lock instead of a pose).",
        "# The lines after '# The sheet' are the drawn sheet, one KiCad primitive per line: the sch stage emits",
        "# schematic.kicad_sch from them. They are build output, never locks (wires, junctions, no-connect marks and",
        "# PWR_FLAGs follow from the lines above and board.py's netlist); one copied into schematic.core.py is refused.",
    ]
    if vector_line:
        out.append(f"# readability: {vector_line}")
    sheet_spec = getattr(design, "sch_sheet", None)
    paper = report.get("readability", {}).get("paper")
    wire = sheet_spec.wire_mm if sheet_spec is not None else 25.4
    line = f"SchSheet(paper={_q(paper)}, wire_mm={_n(wire)})"
    out.append(line + (f"  # locked: {SCH_CORE_FILE}:{sheet_spec.line}" if sheet_spec is not None and sheet_spec.line else ""))
    for n in getattr(design, "sch_nets", ()):
        if abs(n.wire_mm - wire) > 1e-9:
            out.append(f"SchNet({_q(n.net)}, wire_mm={_n(n.wire_mm)})  # locked: {SCH_CORE_FILE}:{n.line}")
    for _key, body, comment in object_lines(design, layout, sheet, parts):
        out.append(body + (f"  # {comment}" if comment else ""))
    prims = report.get("_prims")
    if prims is not None:
        from .sch_prims import gen_lines

        out.append(PRIMS_HEAD)
        out.extend(gen_lines(prims))
    return "\n".join(out) + "\n"


def pose_body(p, origin) -> str:
    """A part's pose as gen writes it: its symbol origin relative to its region's origin, rotation and mirror."""
    rot = int(round(p.rot)) % 360
    mirror = _q(p.mirror) if p.mirror else "None"
    return f"SchPlace({_q(p.ref)}, at={_pt((p.x - origin[0], p.y - origin[1]))}, rotate={rot}, mirror={mirror})"


def object_lines(design, layout, sheet, parts) -> list[tuple[tuple, str, str]]:
    """Every lock-vocabulary object of a drawn pass, in gen order: `(key, body, comment)`. The key is what makes two lines
    one fact (docs/schematic-layout.md §3.1; a label's or power symbol's pin as its part and pin number); the body is the
    line a copy into schematic.core.py locks. gen writes these, and `sch_emit` compares a core lock with them."""
    by = {p.ref: p for p in parts}
    out: list[tuple[tuple, str, str]] = []
    texts = {(t.ref, t.field): t for t in getattr(design, "sch_texts", ())}
    by_site: dict[str, list[dict]] = {}
    for tag, rec in sorted(sheet.records.items(), key=lambda kv: (kv[1]["site"].part.ref, kv[1]["site"].pin.number, kv[0])):
        by_site.setdefault(rec["site"].part.ref, []).append(rec)
    for reg in layout.regions:
        ox, oy = reg.origin
        d = reg.declared
        # A declared region's members as its line writes them (membership is a set; the order is the author's), so a
        # copied gen region line comes back unchanged; an inferred one's in placement order.
        listed = [m for m in d.members if m in set(reg.order)] if d is not None else list(reg.order)
        members = ", ".join(_q(m) for m in listed)
        title = f", title={_q(reg.title)}" if reg.title is not None else ""
        comment = f"locked: {SCH_CORE_FILE}:{d.line}; {reg.note}" if d is not None else f"inferred; {reg.note}"
        out.append((("SchRegion", reg.name), f"SchRegion({_q(reg.name)}{title}, members=({members}{',' if len(listed) == 1 else ''}), at={_pt((ox, oy))}, outline={reg.outline})", comment))
        for m in listed:
            p = by[m]
            spec = layout.specs[m]
            rot = int(round(p.rot)) % 360
            mirror = _q(p.mirror) if p.mirror else "None"
            met = getattr(layout, "met", {}).get(m)
            if met is not None:
                # A core attach line equal to the auto-layout's own spec: met, and placed as that spec (sch_auto.met_attaches).
                note = f"locked: {SCH_CORE_FILE}:{met.line} ({_place_form(met)}, the auto-layout's own)"
            elif spec.source == "core":
                note = f"locked: {SCH_CORE_FILE}:{spec.line}"
                if spec.anchor_kind() != "at":
                    note += f" ({_place_form(spec)})"
            elif spec.anchor_kind() == "anchor" or m == (reg.anchors[0] if reg.anchors else None) and spec.anchor_kind() not in ("to", "along", "below"):
                note = "region anchor"
            else:
                note = f"auto: {_place_form(spec)}"
            out.append((("SchPlace", m), pose_body(p, reg.origin), note))
            seen = getattr(p, "text_seen", None)
            if seen:
                for field, spot, hide in (("Reference", seen[0], getattr(p, "hide_ref", False)), ("Value", seen[1], getattr(p, "hide_val", False))):
                    x, y, just, vis = spot
                    t = texts.get((m, field))
                    lock = f"locked: {SCH_CORE_FILE}:{t.line}" if t is not None else ""
                    out.append((("SchText", m, field), f"SchText({_q(m)}, {_q(field)}, at={_pt((x - p.x, y - p.y))}, rotate={int(vis)}, justify={_q(just) if just else 'None'}, hide={hide})", lock))
            for rec in by_site.get(m, ()):
                s = rec["site"]
                lock = f"locked: {SCH_CORE_FILE}:{rec['line']}" if rec.get("line") else ""
                key = ("SchLabel" if rec["kind"] == "label" else "SchPower", rec["net"], s.part.ref, s.pin.number)
                if rec["kind"] == "label":
                    if "at" in rec:
                        body = f"SchLabel({_q(rec['net'])}, pin={_q(_pin(s.part, s.pin))}, at={_pt(rec['at'])}, rotate={int(rec['rotate'])}, justify={_q(rec['justify'])})"
                    else:
                        body = f"SchLabel({_q(rec['net'])}, pin={_q(_pin(s.part, s.pin))}, side={_q(rec['side'])}, stub={_n(rec['stub'])})"
                else:
                    body = (
                        f"SchPower({_q(rec['net'])}, pin={_q(_pin(s.part, s.pin))}, side={_q(rec['side'])}, stub={_n(rec['stub'])}, "
                        f"jog={_n(rec['jog'])}, rotate={int(rec['rotate'])})"
                    )
                out.append((key, body, lock))
    return out


def lock_bodies(design, parts) -> list[tuple[int, tuple, str, object]]:
    """The core locks that state a drawn value (the paper, a region origin, a pose, a label, a power symbol, a text), each as
    `(stage, key, body, spec)`: the line `object_lines` would write for it, so a lock equal to what a pass drew compares
    equal. A region's body is its origin alone (its members, title and outline are structure, honoured either way).
    Stage orders them as the layout decides them: the paper and origins, poses, names and symbols, texts."""
    by = {p.ref: p for p in parts}
    out: list[tuple[int, tuple, str, object]] = []

    def pin_key(tok: str):
        ref, _, name = tok.partition(".")
        part = by.get(ref)
        if part is None:
            return None, tok
        named = [p for p in part.pins if p.name == name]
        pin = named[0] if len(named) == 1 else next((p for p in part.pins if p.number == name), named[0] if named else None)
        return (ref, pin.number, _pin(part, pin)) if pin is not None else None, tok

    sheet = getattr(design, "sch_sheet", None)
    if sheet is not None and getattr(sheet, "paper", None):
        out.append((0, ("SchSheet",), sheet.paper, sheet))  # the paper: met when the extent picks the same one
    for r in getattr(design, "sch_regions", ()):
        if r.at is not None:
            out.append((0, ("SchRegion", r.name), _pt(r.at), r))
    for s in getattr(design, "sch_places", ()):
        if s.at is not None:
            rot = int(round(s.rot)) % 360 if s.rotate_set else 0
            out.append((1, ("SchPlace", s.ref), f"SchPlace({_q(s.ref)}, at={_pt(s.at)}, rotate={rot}, mirror={_q(s.mirror) if s.mirror else 'None'})", s))
    for kind, specs in (("SchLabel", getattr(design, "sch_labels", ())), ("SchPower", getattr(design, "sch_powers", ()))):
        for s in specs:
            pk, tok = pin_key(s.pin)
            if pk is None:
                continue  # an unknown pin: the drawing refuses it
            ref, number, canon = pk
            if kind == "SchLabel" and s.at is not None:
                body = f"SchLabel({_q(s.net)}, pin={_q(canon)}, at={_pt(s.at)}, rotate={int(s.rotate)}, justify={_q(s.justify) if s.justify else 'None'})"
            elif kind == "SchLabel":
                body = f"SchLabel({_q(s.net)}, pin={_q(canon)}, side={_q(s.side)}, stub={_n(s.stub) if s.stub is not None else 'None'})"
            else:
                body = f"SchPower({_q(s.net)}, pin={_q(canon)}, side={_q(s.side)}, stub={_n(s.stub)}, jog={_n(s.jog)}, rotate={int(s.rotate)})"
            out.append((2, (kind, s.net, ref, number), body, s))
    for s in getattr(design, "sch_texts", ()):
        body = f"SchText({_q(s.ref)}, {_q(s.field)}, at={_pt(s.at)}, rotate={int(s.rotate)}, justify={_q(s.justify) if s.justify else 'None'}, hide={bool(s.hide)})"
        out.append((3, ("SchText", s.ref, s.field), body, s))
    return out


PRIMS_HEAD = "# The sheet, primitive by primitive (what schematic.kicad_sch is emitted from):"
