"""`schematic.core.py`: the schematic's locks, beside `board.py` (docs/schematic-layout.md).

`board.py` is the circuit and its physics only (docs/direction.md §2). The sheet is laid out by the tool from the
netlist (`sch_auto`); what the AI decides about the drawing — regions with titles and members, region order, poses,
attachments, label, power-symbol and text placement, sheet defaults — is written here, in `schematic.core.py`, and
nowhere else:

- a `Sch*` call made while `board.py` runs is refused once the file has run, naming its line and this file;
- a `Sch*` call made while `layout.core.py` (or `layout.gen.py`) runs is refused naming this file;
- a board or PCB object made while `schematic.core.py` runs is refused naming the file it belongs in, however it
  was imported (the constructors refuse, and the design is inspected before and after the file runs).

`schematic.core.py` is executed after `board.py` and `layout.core.py`, under its own trace (`tracing("sch-core")`),
so its reads never enter `design.reads`: editing it redraws the sheet and never makes the placed or routed board
stale (§1.1). Every gen line (`layout/<name>/schematic.gen.py`, `sch_gen`) is a valid line here, copied verbatim.
"""

from __future__ import annotations

import math
import pathlib
import sys

from .model import SchLabelSpec, SchNetSpec, SchPlaceSpec, SchPowerSpec, SchRegionSpec, SchSheetSpec, SchTextSpec

SCH_CORE_FILE = "schematic.core.py"
SCH_GEN_FILE = "schematic.gen.py"

# The schematic constructors. Each records into the design; the file executing decides whether it may hold them.
SCH_NAMES = ("SchPlace", "SchRegion", "SchSheet", "SchNet", "SchText", "SchLabel", "SchPower")
SCH_CALLERS = frozenset(SCH_NAMES)
SIDES = ("left", "right", "top", "bottom")
GRID = 1.27
AUTO = "auto-"

# The CSS of the forms before docs/schematic-layout.md PR 5: refused by name, proposing nothing (§3.6).
_CSS = frozenset(
    {
        "left", "top", "right", "bottom", "width", "height", "inset", "margin", "margin_top", "margin_right",
        "margin_bottom", "margin_left", "padding", "padding_top", "padding_right", "padding_bottom", "padding_left",
        "position", "style",
    }
)


def _lang():
    from . import language

    return language


def _here() -> str:
    """`schematic.core.py:12` (or `board.py:12`): the deepest frame in the file being loaded, or ""."""
    L = _lang()
    target = L._sch_path or L._layout_path or (str(L._board_path) if L._board_path is not None else None)
    if target is None:
        return ""
    f = sys._getframe(1)
    while f is not None:
        if f.f_code.co_filename == target:
            return f"{pathlib.Path(target).name}:{f.f_lineno}"
        f = f.f_back
    return ""


def _line_no() -> int:
    where = _here()
    return int(where.rsplit(":", 1)[1]) if where else 0


def _gate(construct: str, label: str) -> bool:
    """True when `board.py` is running: the call is recorded as a refusal and the caller returns without touching
    the design. Raises when a layout file (`layout.core.py`, `layout.gen.py`) is running: schematic layout is not
    that file's."""
    L = _lang()
    if L._refuse_in_board(construct, label):
        return True
    if L._layout_path is not None:
        name = pathlib.Path(L._layout_path).name
        raise ValueError(f"{_here() or name}: {label} is schematic layout; it lives in {SCH_CORE_FILE} beside board.py")
    return False


def _who(label: str) -> str:
    where = _here()
    return f"{where}: {label}" if where else label


def _refuse_keywords(label: str, given: dict, takes: tuple[str, ...], *, css: str = "") -> None:
    """Every keyword an object does not take is refused, naming it and the keywords it takes (§3); CSS by name (§3.6)."""
    if css and any(k in _CSS for k in given):
        raise ValueError(f"{_who(label)} takes no CSS; {css}")
    bad = sorted(k for k in given if k not in takes)
    if bad:
        raise ValueError(
            f"{_who(label)} takes no keyword {', '.join(k + '=' for k in bad)}; it takes {', '.join(k + '=' for k in takes)}"
        )


def _num(label: str, key: str, value, *, allow_negative: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{_who(label)}: {key}={value!r} is not a number of millimetres")
    v = float(value)
    if not math.isfinite(v) or (v < 0 and not allow_negative):
        raise ValueError(f"{_who(label)}: {key}={value!r} is not a length (at least 0 mm)")
    return v


def _on_grid(v: float) -> bool:
    return abs(v / GRID - round(v / GRID)) < 1e-6


def _grid(label: str, key: str, v: float) -> float:
    if not _on_grid(v):
        raise ValueError(f"{_who(label)}: {key} {v:g} is off the {GRID} mm grid")
    return v


def _point(label: str, key: str, value, *, grid: bool) -> tuple[float, float]:
    if not isinstance(value, (tuple, list)) or len(value) != 2:
        raise ValueError(f"{_who(label)}: {key}={value!r} is not a point (x, y) in mm")
    x, y = (_num(label, key, v, allow_negative=True) for v in value)
    if grid:
        for v in (x, y):
            if not _on_grid(v):
                raise ValueError(f"{_who(label)}: {key}=({x:g}, {y:g}): {v:g} is off the {GRID} mm grid")
    return (x, y)


def _refpin(label: str, key: str, value) -> str:
    if not isinstance(value, str) or "." not in value or not value.split(".", 1)[0] or not value.split(".", 1)[1]:
        raise ValueError(f"{_who(label)}: {key}={value!r} is not 'REF.PIN'")
    return value


# --------------------------------------------------------------------------- the vocabulary (§3.1)

_PLACE_TAKES = ("at", "to", "pin", "side", "gap", "along", "align", "below", "rotate", "mirror", "reason")


def SchPlace(ref: str, **kw) -> SchPlaceSpec | None:  # noqa: N802
    """A schematic pose (docs/schematic-layout.md §3). At most one anchor: ``at=(x, y)`` locks the symbol origin in its
    region's frame (the part must be a member of a declared SchRegion); ``to="U1.EN"`` puts the pin of this part on
    U1.EN's net in line with it (``pin=`` names a different pin, ``side=`` the side of the target it hangs on, ``gap=``
    the pin-to-pin distance); ``along="C1.1"`` stacks beside that part (``align="U2.EN"`` lands the hanging pin on that
    pin's row or column); ``below="J2"`` stacks under a part; no anchor makes the part its region's anchor.
    ``rotate=`` (0/90/180/270) and ``mirror="x"|"y"`` fix the pose."""
    label = f"SchPlace({ref!r})"
    if _gate("SchPlace", label):
        return None
    L = _lang()
    if "parent" in kw:
        parent = kw["parent"]
        raise ValueError(f"{_who(f'SchPlace({ref!r}, parent={parent!r})')}: membership is SchRegion(members=)")
    _refuse_keywords(label, kw, _PLACE_TAKES, css="a pose is at=(x, y) in its region")
    who = _who(label)
    anchors = [k for k in ("at", "to", "along", "below") if kw.get(k) is not None]
    if len(anchors) > 1:
        raise ValueError(f"{who}: {' and '.join(a + '=' for a in anchors)} are {len(anchors)} anchors; a SchPlace takes one")
    for key, needs in (("pin", ("to",)), ("side", ("to", "along")), ("gap", ("to", "along")), ("align", ("along",))):
        if kw.get(key) is not None and not any(kw.get(n) is not None for n in needs):
            raise ValueError(f"{who}: {key}= belongs to {' or '.join(n + '=' for n in needs)}, and this SchPlace has none")
    side, mirror, rotate = kw.get("side"), kw.get("mirror"), kw.get("rotate")
    if side is not None and side not in SIDES:
        raise ValueError(f"{who}: side must be left/right/top/bottom, got {side!r}")
    if mirror is not None and mirror not in ("x", "y"):
        raise ValueError(f"{who}: mirror must be 'x' (top/bottom) or 'y' (left/right), got {mirror!r}")
    spec = SchPlaceSpec(
        ref=str(ref),
        pin=kw.get("pin"),
        to=_refpin(label, "to", kw["to"]) if kw.get("to") is not None else None,
        along=_refpin(label, "along", kw["along"]) if kw.get("along") is not None else None,
        below=str(kw["below"]) if kw.get("below") is not None else None,
        gap=_num(label, "gap", kw["gap"]) if kw.get("gap") is not None else None,
        align=_refpin(label, "align", kw["align"]) if kw.get("align") is not None else None,
        side=side,
        mirror=mirror,
        mirror_set="mirror" in kw,
        at=_point(label, "at", kw["at"], grid=True) if kw.get("at") is not None else None,
        reason=str(kw.get("reason") or ""),
        line=_line_no(),
        source="core",
    )
    if rotate is not None:
        if rotate not in (0, 90, 180, 270):
            raise ValueError(f"{who}: rotate={rotate!r}; a schematic symbol turns by 0, 90, 180 or 270")
        spec.rot = float(rotate)
        spec.rotate_set = True
    L._doc().sch_places.append(spec)
    return spec


_REGION_TAKES = ("members", "title", "at", "row", "col", "outline", "reason")


def SchRegion(name: str, **kw) -> SchRegionSpec | None:  # noqa: N802
    """A titled group of parts on the sheet, outlined by a dashed box fitted around them. ``members`` (required) is
    closed: every part of the region and no other. Its origin (the frame's top-left corner) is ``at=(x, y)`` in sheet mm,
    or a grid cell ``row=``, ``col=``, or left to the auto-layout's grid. ``outline=False`` draws no box."""
    label = f"SchRegion({name!r})"
    if _gate("SchRegion", label):
        return None
    L = _lang()
    if "parent" in kw:
        raise ValueError(f"{_who(label)}: parent= nests a containing block; a region's members are SchRegion(members=), and regions do not nest")
    _refuse_keywords(label, kw, _REGION_TAKES, css="its origin is at=(x, y) or a grid cell row=, col=")
    who = _who(label)
    if kw.get("members") is None:
        raise ValueError(f"{who}: members= is required; membership is closed and SchRegion(members=) is its one home")
    members = kw["members"]
    if isinstance(members, str) or not isinstance(members, (tuple, list)) or not all(isinstance(m, str) for m in members):
        raise ValueError(f"{who}: members={members!r} is not a tuple of part refs")
    if len(set(members)) != len(members):
        twice = sorted({m for m in members if list(members).count(m) > 1})
        raise ValueError(f"{who}: members names {', '.join(twice)} twice")
    if kw.get("at") is not None and (kw.get("row") is not None or kw.get("col") is not None):
        raise ValueError(f"{who}: at= and row=/col= are two anchors for its origin; a SchRegion takes one")
    if str(name).startswith(AUTO) and str(name)[len(AUTO):] not in members:
        raise ValueError(f"{who}: a region named {AUTO}... is named by its anchor part, and {str(name)[len(AUTO):]} is not among its members")
    for key in ("row", "col"):
        v = kw.get(key)
        if v is not None and (isinstance(v, bool) or not isinstance(v, int) or v < 0):
            raise ValueError(f"{who}: {key}={v!r} is not a grid index (0, 1, 2, ...)")
    outline = kw.get("outline", True)
    if not isinstance(outline, bool):
        raise ValueError(f"{who}: outline={outline!r} is True or False")
    spec = SchRegionSpec(
        name=str(name),
        members=tuple(members),
        title=str(kw["title"]) if kw.get("title") is not None else None,
        at=_point(label, "at", kw["at"], grid=True) if kw.get("at") is not None else None,
        row=kw.get("row"),
        col=kw.get("col"),
        outline=outline,
        reason=str(kw.get("reason") or ""),
        line=_line_no(),
    )
    doc = L._doc()
    for other in doc.sch_regions:
        if other.name == spec.name:
            raise ValueError(f"{who}: region {spec.name!r} is already declared at {SCH_CORE_FILE}:{other.line}; a region is declared once")
    doc.sch_regions.append(spec)
    return spec


_PAPERS = ("A4", "A3", "A2")


def SchSheet(**kw) -> SchSheetSpec | None:  # noqa: N802
    """The sheet: ``paper`` ("A4", "A3", "A2"; None picks by the extent, width and height) and ``wire_mm``, the
    longest wire drawn between two symbols before a net is joined by labels instead (25.4)."""
    label = "SchSheet()"
    if _gate("SchSheet", label):
        return None
    L = _lang()
    _refuse_keywords(label, kw, ("paper", "wire_mm"))
    paper = kw.get("paper")
    if paper is not None and paper not in _PAPERS:
        raise ValueError(f"{_who(label)}: paper={paper!r}; the sheet is A4, A3 or A2")
    doc = L._doc()
    if doc.sch_sheet is not None:
        raise ValueError(f"{_who(label)}: the sheet is already declared at {SCH_CORE_FILE}:{doc.sch_sheet.line}; a sheet has one SchSheet()")
    wire = _num(label, "wire_mm", kw["wire_mm"]) if "wire_mm" in kw else 25.4
    doc.sch_sheet = SchSheetSpec(paper=paper, wire_mm=wire, line=_line_no())
    return doc.sch_sheet


def SchNet(net: str, **kw) -> SchNetSpec | None:  # noqa: N802
    """One net's wire limit on the sheet: ``wire_mm`` is the longest wire drawn for it (0: labels only)."""
    label = f"SchNet({net!r})"
    if _gate("SchNet", label):
        return None
    L = _lang()
    _refuse_keywords(label, kw, ("wire_mm",))
    if "wire_mm" not in kw:
        raise ValueError(f"{_who(label)}: wire_mm= is the one thing a SchNet() says, and it is not written")
    doc = L._doc()
    for other in doc.sch_nets:
        if other.net == str(net):
            raise ValueError(f"{_who(label)}: {net} already has SchNet() at {SCH_CORE_FILE}:{other.line}; a net takes one")
    spec = SchNetSpec(net=str(net), wire_mm=_num(label, "wire_mm", kw["wire_mm"]), line=_line_no())
    doc.sch_nets.append(spec)
    return spec


def SchText(ref: str, field: str, **kw) -> SchTextSpec | None:  # noqa: N802
    """A Reference or Value as it is seen: ``at=(dx, dy)`` from the symbol origin in sheet axes, ``rotate`` 0 or 90,
    ``justify`` None, "left" or "right" (as drawn), ``hide``."""
    label = f"SchText({ref!r}, {field!r})"
    if _gate("SchText", label):
        return None
    L = _lang()
    _refuse_keywords(label, kw, ("at", "rotate", "justify", "hide"))
    who = _who(label)
    if field not in ("Reference", "Value"):
        raise ValueError(f"{who}: field {field!r}; a SchText places the Reference or the Value")
    if kw.get("at") is None:
        raise ValueError(f"{who}: at=(dx, dy) is where the text is, and it is not written")
    rotate, justify, hide = kw.get("rotate", 0), kw.get("justify"), kw.get("hide", False)
    if rotate not in (0, 90):
        raise ValueError(f"{who}: rotate={rotate!r}; a field reads at 0 or 90")
    if justify not in (None, "left", "right"):
        raise ValueError(f"{who}: justify={justify!r}; it is None, 'left' or 'right'")
    if not isinstance(hide, bool):
        raise ValueError(f"{who}: hide={hide!r} is True or False")
    doc = L._doc()
    for other in doc.sch_texts:
        if (other.ref, other.field) == (str(ref), field):
            raise ValueError(f"{who}: {ref}'s {field} is already placed at {SCH_CORE_FILE}:{other.line}; a field takes one SchText()")
    spec = SchTextSpec(str(ref), field, _point(label, "at", kw["at"], grid=False), int(rotate), justify, hide, _line_no())
    doc.sch_texts.append(spec)
    return spec


def SchLabel(net: str, **kw) -> SchLabelSpec | None:  # noqa: N802
    """The label that names `net` at a pin. ``pin="R6.2", side=, stub=``: a label on a stub of ``stub`` mm leaving the
    pin to ``side`` (its own side), and the pin takes no wire, even where one would fit. ``pin=, at=(dx, dy), rotate=,
    justify=``: the label of the pin's wired group, on its wire, ``at`` from the pin end."""
    label = f"SchLabel({net!r})"
    if _gate("SchLabel", label):
        return None
    L = _lang()
    _refuse_keywords(label, kw, ("pin", "side", "stub", "at", "rotate", "justify"))
    who = _who(label)
    pin = _refpin(label, "pin", kw.get("pin"))
    stub_form = kw.get("side") is not None or kw.get("stub") is not None
    at_form = kw.get("at") is not None
    if stub_form == at_form:
        raise ValueError(f"{who}: a label is side= and stub= (on a stub off its pin) or at= (on its group's wire), one of the two")
    spec = SchLabelSpec(str(net), pin, line=_line_no())
    if stub_form:
        if kw.get("side") not in SIDES or kw.get("stub") is None:
            raise ValueError(f"{who}: a stub label takes side= (left/right/top/bottom) and stub= (mm)")
        if kw.get("rotate") is not None or kw.get("justify") is not None:
            raise ValueError(f"{who}: a stub label's text stands as its pin points; rotate= and justify= belong to the at= form")
        spec.side, spec.stub = kw["side"], _grid(label, "stub", _num(label, "stub", kw["stub"]))
    else:
        spec.at = _point(label, "at", kw["at"], grid=False)
        rotate, justify = kw.get("rotate", 0), kw.get("justify")
        if rotate not in (0, 90):
            raise ValueError(f"{who}: rotate={rotate!r}; a label reads at 0 or 90")
        if justify not in ("left", "right"):
            raise ValueError(f"{who}: justify={justify!r}; it is 'left' or 'right'")
        spec.rotate, spec.justify = int(rotate), justify
    doc = L._doc()
    for other in doc.sch_labels:
        if (other.net, other.pin) == (spec.net, spec.pin):
            raise ValueError(f"{who}: {pin} already has SchLabel() at {SCH_CORE_FILE}:{other.line}; a pin takes one")
    doc.sch_labels.append(spec)
    return spec


def SchPower(net: str, **kw) -> SchPowerSpec | None:  # noqa: N802
    """The power symbol of `net` at a pin: on a stub of ``stub`` mm leaving the pin to ``side``, ``jog`` mm on (down
    the sheet when positive), turned ``rotate`` (180 points it the other way)."""
    label = f"SchPower({net!r})"
    if _gate("SchPower", label):
        return None
    L = _lang()
    _refuse_keywords(label, kw, ("pin", "side", "stub", "jog", "rotate"))
    who = _who(label)
    pin = _refpin(label, "pin", kw.get("pin"))
    if kw.get("side") not in SIDES:
        raise ValueError(f"{who}: side={kw.get('side')!r}; the stub leaves the pin to left, right, top or bottom")
    rotate = kw.get("rotate", 0)
    if rotate not in (0, 180):
        raise ValueError(f"{who}: rotate={rotate!r}; a power symbol stands at 0 or 180")
    stub = _grid(label, "stub", _num(label, "stub", kw.get("stub", 0.0)))
    jog = _grid(label, "jog", _num(label, "jog", kw.get("jog", 0.0), allow_negative=True))
    spec = SchPowerSpec(str(net), pin, kw["side"], stub, jog, int(rotate), _line_no())
    doc = L._doc()
    for other in doc.sch_powers:
        if (other.net, other.pin) == (spec.net, spec.pin):
            raise ValueError(f"{who}: {pin} already has SchPower() at {SCH_CORE_FILE}:{other.line}; a pin takes one")
    doc.sch_powers.append(spec)
    return spec


def SchStyle(*_a, **_k):  # noqa: N802
    """Refused in every file: the sheet's defaults are `SchSheet()` in `schematic.core.py` (the `Chain()` precedent)."""
    where = _here()
    raise ValueError(f"{where + ': ' if where else ''}SchStyle() is now SchSheet(), in {SCH_CORE_FILE}")


CONSTRUCTORS = {
    "SchPlace": SchPlace,
    "SchRegion": SchRegion,
    "SchSheet": SchSheet,
    "SchNet": SchNet,
    "SchText": SchText,
    "SchLabel": SchLabel,
    "SchPower": SchPower,
}


# --------------------------------------------------------------------------- the loader

# Design fields that are neither board.py's nor layout.core.py's: the schematic's own, and the load's bookkeeping.
_SCH_FIELDS = ("sch_places", "sch_regions", "sch_sheet", "sch_nets", "sch_texts", "sch_labels", "sch_powers", "sch_core")
_BOOKKEEPING = ("source", "modules", "reads", "core_reads", "core_loaded")
_CORE_OWNED = ("places", "keepouts", "regions", "outline", "copper", "poses", "graphics")


def _watched(design) -> tuple[str, ...]:
    import dataclasses

    return tuple(f.name for f in dataclasses.fields(design) if f.name not in _SCH_FIELDS and f.name not in _BOOKKEEPING)


def _line_of(exc: BaseException, path: pathlib.Path) -> str:
    import traceback

    if isinstance(exc, SyntaxError) and exc.lineno:
        return f"{path.name}:{exc.lineno}"
    line = 0
    for fr in traceback.extract_tb(exc.__traceback__):
        if fr.filename == str(path):
            line = fr.lineno or line
    return f"{path.name}:{line}" if line else path.name


def _stub(name: str, owner: str):
    def refuse(*_a, **_k):
        raise ValueError(f"{_here() or SCH_CORE_FILE}: {name}() is {owner}'s, not {SCH_CORE_FILE}'s")

    return refuse


def load_sch_core(design, board_path: pathlib.Path) -> None:
    """Run the `schematic.core.py` beside `board_path` into `design`, under its own trace (`design.sch_core` records
    its sha and its reads), then the facts that need both files."""
    from .build import _sha  # noqa: PLC0415 - one hash function for every stamp
    from .trace import tracing

    board_path = pathlib.Path(board_path).resolve()
    cp = board_path.parent / SCH_CORE_FILE
    if not cp.exists():
        design.sch_core = None
        check_facts(design, board_path.name)
        return
    with tracing("sch-core", board_path.parent) as seen:
        _exec(cp, design)
    design.sch_core = {"sha": _sha(cp), "reads": seen.record()["reads"]}
    check_facts(design, board_path.name)


def _primitive(name: str):
    def refuse(*_a, **_k):
        raise ValueError(
            f"{name}(...) is a primitive of the drawn sheet: schematic.gen.py writes it for the emitter from the lock lines and "
            f"board.py's netlist, and it is not a lock; the locks are {', '.join(SCH_NAMES)}"
        )

    return refuse


def _exec(path: pathlib.Path, design) -> None:
    L = _lang()
    ns: dict = {name: _stub(name, "board.py") for name in L._BOARD_NAMES}
    ns.update({name: _stub(name, "layout.core.py") for name in (*L._CORE_NAMES, *L._LAYOUT_NAMES)})
    ns.update(CONSTRUCTORS)
    ns["SchStyle"] = SchStyle
    from .sch_prims import NAMES as _PRIM_NAMES

    ns.update({name: _primitive(name) for name in _PRIM_NAMES})
    ns.update({"__file__": str(path), "__name__": "__pcbc_schematic__"})
    watched = _watched(design)
    before = {f: repr(getattr(design, f)) for f in watched}
    prev_doc, prev_path = L._current, L._sch_path
    L._current = design
    L._sch_path = str(path)
    before_mods = set(sys.modules)
    before_path = list(sys.path)
    try:
        exec(compile(path.read_text(), str(path), "exec"), ns, ns)  # noqa: S102
    except ValueError as exc:
        msg = str(exc)
        raise ValueError(msg if msg.startswith(path.name) else f"{_line_of(exc, path)}: {msg}") from None
    except Exception as exc:  # noqa: BLE001 - a typo in the core file is the author's to fix, named by its line
        raise ValueError(f"{_line_of(exc, path)}: {type(exc).__name__}: {exc}") from None
    finally:
        L._current = prev_doc
        L._sch_path = prev_path
        L._local_modules(path.parent, set(sys.modules) - before_mods)
        sys.path[:] = before_path
    after = {f: repr(getattr(design, f)) for f in watched}
    changed = [f for f in watched if after[f] != before[f]]
    if changed:
        owners = sorted({"layout.core.py" if f in _CORE_OWNED else "board.py" for f in changed})
        raise ValueError(
            f"{path.name}: this file changed {' and '.join(o + chr(39) + 's' for o in owners)} part of the design "
            f"({', '.join(changed)}), however it was imported; {path.name} holds schematic layout only"
        )


def _pin_net(inst, token: str) -> str | None:
    """The net of `token` on `inst`: a symbol pin name, or one of its pads."""
    if token in inst.pins:
        return inst.pins[token]
    for name, pin in inst.part.pins.items():
        if token in pin.pads and name in inst.pins:
            return inst.pins[name]
    return None


def _pins_of(inst) -> str:
    return ", ".join(sorted(inst.pins))


def check_facts(design, board_name: str = "board.py") -> None:
    """What needs both files (§5): parts, nets and pins exist; one home per fact; membership is closed and has one home;
    a pose is in a declared region's frame; an attach stays in its region; labels and power symbols sit on their net."""
    f = SCH_CORE_FILE
    insts = {inst.ref: inst for inst in design.instances}
    lines: dict[str, list[int]] = {}
    for p in design.sch_places:
        lines.setdefault(p.ref, []).append(int(p.line or 0))
    twice = [(r, ls) for r, ls in lines.items() if len(ls) > 1]
    if twice:
        raise ValueError("; ".join(
            f"{f}:{ls[1]}: SchPlace({r!r}) is already written {'on this line' if ls[1] == ls[0] else f'at line {ls[0]}'}; a part takes one SchPlace(), and pcbc does not pick between them"
            for r, ls in twice
        ))
    for p in design.sch_places:
        if p.ref not in insts:
            raise ValueError(f"{f}:{p.line}: SchPlace({p.ref!r}): {board_name} has no part {p.ref}")
        for key in ("to", "along", "align"):
            v = getattr(p, key)
            if v and v.split(".", 1)[0] not in insts:
                raise ValueError(f"{f}:{p.line}: SchPlace({p.ref!r}): {key}={v!r} names {v.split('.', 1)[0]}, and {board_name} has no such part")
        if p.below and p.below not in insts:
            raise ValueError(f"{f}:{p.line}: SchPlace({p.ref!r}): below={p.below!r}, and {board_name} has no such part")
    region_of: dict[str, object] = {}
    for r in design.sch_regions:
        for m in r.members:
            if m not in insts:
                raise ValueError(f"{f}:{r.line}: SchRegion({r.name!r}): members names {m}, and {board_name} has no such part")
            if m in region_of:
                other = region_of[m]
                raise ValueError(f"{f}:{r.line}: SchRegion({r.name!r}): {m} is already a member of SchRegion({other.name!r}) at line {other.line}; a part is in one region")
            region_of[m] = r
    anchors: dict[str, SchPlaceSpec] = {}
    by_ref = {p.ref: p for p in design.sch_places}
    for p in design.sch_places:
        reg = region_of.get(p.ref)
        if p.at is not None and reg is None:
            raise ValueError(
                f"{f}:{p.line}: the at= of {p.ref}'s SchPlace is a pose in its region's frame, and {p.ref} is in no SchRegion(members=) of {f}; "
                f"its frame is the region the auto-layout put it in (schematic.gen.py writes that SchRegion line)"
            )
        if p.anchor_kind() == "anchor":
            key = reg.name if reg is not None else None
            if key is not None and key in anchors:
                o = anchors[key]
                raise ValueError(f"{f}:{p.line}: SchPlace({p.ref!r}) is the anchor of region {key!r}, and SchPlace({o.ref!r}) at line {o.line} already is; a region has one")
            if key is not None:
                anchors[key] = p
        target = (p.to or p.along or "").split(".", 1)[0] or p.below
        if target:
            mine, theirs = region_of.get(p.ref), region_of.get(target)
            if mine is not theirs:
                name = lambda r: f"SchRegion({r.name!r})" if r is not None else "no declared region"  # noqa: E731
                raise ValueError(f"{f}:{p.line}: SchPlace({p.ref!r}) hangs off {target}; {p.ref} is in {name(mine)} and {target} in {name(theirs)}; an attach stays in its region")
    # Attach cycles, with the chain of refs.
    for p in design.sch_places:
        seen = [p.ref]
        cur = p
        while cur is not None:
            target = (cur.to or cur.along or "").split(".", 1)[0] or cur.below
            if not target:
                break
            if target in seen:
                chain = " -> ".join(seen[seen.index(target):] + [target])
                raise ValueError(f"{f}:{p.line}: SchPlace({p.ref!r}): the attachments go round in a circle ({chain})")
            seen.append(target)
            cur = by_ref.get(target)
    nets = design.nets
    for n in design.sch_nets:
        if n.net not in nets:
            raise ValueError(f"{f}:{n.line}: SchNet({n.net!r}): {board_name} has no net {n.net}")
    for t in design.sch_texts:
        if t.ref not in insts:
            raise ValueError(f"{f}:{t.line}: SchText({t.ref!r}, {t.field!r}): {board_name} has no part {t.ref}")
    for what, specs in (("SchLabel", design.sch_labels), ("SchPower", design.sch_powers)):
        for s in specs:
            label = f"{f}:{s.line}: {what}({s.net!r}, pin={s.pin!r})"
            if s.net not in nets:
                raise ValueError(f"{label}: {board_name} has no net {s.net}")
            ref, tok = s.pin.split(".", 1)
            inst = insts.get(ref)
            if inst is None:
                raise ValueError(f"{label}: {board_name} has no part {ref}")
            got = _pin_net(inst, tok)
            if got is None:
                raise ValueError(f"{label}: {ref} has no pin {tok} (its pins: {_pins_of(inst)})")
            if got != s.net:
                raise ValueError(f"{label}: {s.pin} is on {got}, not {s.net}")
            kind = nets[s.net].kind
            if what == "SchLabel" and kind in ("power", "ground"):
                raise ValueError(f"{label}: {s.net} is {'Ground' if kind == 'ground' else 'Power'}(), drawn with power symbols, not labels")
            if what == "SchPower" and kind not in ("power", "ground"):
                raise ValueError(f"{label}: {s.net} is a signal net, joined by wires and labels, not power symbols")
