"""The schematic sheet as KiCad primitives, one Python object per primitive, field for field (docs/schematic-layout.md
PR 6; docs/direction.md §4 applied to the sheet).

`schematic.kicad_sch` has one writer, `sheet_text`: it writes the header, the library symbols (derived from the parts'
`.kicad_sym` files, as a footprint is from its `.kicad_mod`) and the primitives, in KiCad's own order and layout
(rectangles, texts, junctions, no-connect marks, wires, labels, symbols, each kind sorted by uuid, every field KiCad
writes), so `kicad-cli sch upgrade` saves the body back byte for byte. The sch stage writes the primitives into
`schematic.gen.py` (`gen_lines`), loads them back from that file (`load_gen`) and emits the sheet from what it loaded:
the sheet is emitted from gen objects. `decompile` reads a sheet back into the same objects and refuses any token it
has no row for (`Unmapped`): emitted sheet -> objects -> sheet is the same file, and the gen lines written from the
decompiled objects are the gen file's, byte for byte.

The table `FIELDS` lists, per primitive, every Python keyword, the attribute and the KiCad token. Primitive lines are
build output: none is a lock (a lock is the `Sch*` vocabulary of `schematic.core.py`), and one copied into
`schematic.core.py` is refused naming the fact.
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields as dc_fields

from .sexp import Q, parse_tree, quote

# KiCad's order of the sheet's top-level primitives (measured with `kicad-cli sch upgrade --force`, KiCad 10.0.6):
# by kind in this order, each kind by uuid.
ORDER = ("rectangle", "text", "junction", "no_connect", "wire", "label", "symbol")


def num(v: float) -> str:
    """A schematic coordinate as KiCad 10 writes it: at most 4 decimals (the sheet's 100 nm unit), no trailing zeros."""
    s = f"{float(v):.4f}".rstrip("0").rstrip(".")
    return "0" if s in ("", "-0") else s


def _ang(v: float) -> str:
    return num(v)


class Unmapped(ValueError):
    """A token of a sheet that no primitive row maps: a field Python cannot carry is a defect, never a silent loss."""


@dataclass(frozen=True)
class SheetField:
    """`(property "name" "value" (at x y angle) [(hide yes)] (show_name ..) (do_not_autoplace ..) (effects (font (size
    s s)) [(justify ..)]))`: one property of a placed symbol."""

    name: str
    value: str
    at: tuple[float, float] = (0.0, 0.0)
    angle: float = 0.0
    hide: bool = False
    show_name: bool = False
    do_not_autoplace: bool = False
    size: float = 1.27
    justify: tuple[str, ...] = ()


@dataclass(frozen=True)
class SheetSymbol:
    """`(symbol ...)`: a placed symbol (a part, a power symbol, a PWR_FLAG)."""

    lib_id: str
    at: tuple[float, float]
    angle: float = 0.0
    mirror: str | None = None
    unit: int = 1
    body_style: int = 1
    exclude_from_sim: bool = False
    in_bom: bool = True
    on_board: bool = True
    in_pos_files: bool = True
    dnp: bool = False
    uuid: str = ""
    fields: tuple[SheetField, ...] = ()
    pins: tuple[tuple[str, str], ...] = ()  # (number, uuid)

    kind = "symbol"


@dataclass(frozen=True)
class SheetWire:
    """`(wire (pts (xy) (xy)) (stroke (width) (type)) (uuid))`."""

    a: tuple[float, float]
    b: tuple[float, float]
    width: float = 0.0
    style: str = "default"
    uuid: str = ""

    kind = "wire"


@dataclass(frozen=True)
class SheetJunction:
    """`(junction (at) (diameter) (color r g b a) (uuid))`."""

    at: tuple[float, float]
    diameter: float = 0.0
    color: tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)
    uuid: str = ""

    kind = "junction"


@dataclass(frozen=True)
class SheetNoConnect:
    """`(no_connect (at) (uuid))`."""

    at: tuple[float, float]
    uuid: str = ""

    kind = "no_connect"


@dataclass(frozen=True)
class SheetLabel:
    """`(label "text" (at x y angle) (effects (font (size)) (justify h v)) (uuid))`: a local net label."""

    text: str
    at: tuple[float, float]
    angle: float = 0.0
    size: float = 1.27
    justify: tuple[str, ...] = ("left", "bottom")
    uuid: str = ""

    kind = "label"


@dataclass(frozen=True)
class SheetRect:
    """`(rectangle (start) (end) (stroke (width) (type)) (fill (type)) (uuid))`: a region's outline."""

    start: tuple[float, float]
    end: tuple[float, float]
    width: float = 0.254
    style: str = "dash"
    fill: str = "none"
    uuid: str = ""

    kind = "rectangle"


@dataclass(frozen=True)
class SheetText:
    """`(text "text" (exclude_from_sim) (at x y angle) (effects (font (size)) (justify ..)) (uuid))`: a region's title."""

    text: str
    at: tuple[float, float]
    angle: float = 0.0
    exclude_from_sim: bool = False
    size: float = 2.54
    justify: tuple[str, ...] = ("left", "top")
    uuid: str = ""

    kind = "text"


PRIMS = (SheetSymbol, SheetWire, SheetJunction, SheetNoConnect, SheetLabel, SheetRect, SheetText)
NAMES = tuple(c.__name__ for c in PRIMS) + ("SheetField",)
_BY_KIND = {c.kind: c for c in PRIMS}

# (python keyword, attribute, KiCad token) per primitive: the 1:1 table (docs/direction.md §4). None is positional.
FIELDS: dict[str, list[tuple[str | None, str, str]]] = {
    "SheetSymbol": [
        (None, "lib_id", "symbol (lib_id)"), ("at", "at", "symbol (at x y)"), ("angle", "angle", "symbol (at x y angle)"),
        ("mirror", "mirror", "symbol (mirror x|y)"), ("unit", "unit", "symbol (unit)"), ("body_style", "body_style", "symbol (body_style)"),
        ("exclude_from_sim", "exclude_from_sim", "symbol (exclude_from_sim)"), ("in_bom", "in_bom", "symbol (in_bom)"),
        ("on_board", "on_board", "symbol (on_board)"), ("in_pos_files", "in_pos_files", "symbol (in_pos_files)"), ("dnp", "dnp", "symbol (dnp)"),
        ("uuid", "uuid", "symbol (uuid)"), ("fields", "fields", "symbol (property ...)*"), ("pins", "pins", 'symbol (pin "n" (uuid))*'),
    ],
    "SheetField": [
        (None, "name", "property name"), (None, "value", "property value"), ("at", "at", "property (at x y)"),
        ("angle", "angle", "property (at x y angle)"), ("hide", "hide", "property (hide yes)"), ("show_name", "show_name", "property (show_name)"),
        ("do_not_autoplace", "do_not_autoplace", "property (do_not_autoplace)"), ("size", "size", "property (effects (font (size s s)))"),
        ("justify", "justify", "property (effects (justify ...))"),
    ],
    "SheetWire": [(None, "a", "wire (pts (xy))"), (None, "b", "wire (pts (xy) (xy))"), ("width", "width", "wire (stroke (width))"), ("style", "style", "wire (stroke (type))"), ("uuid", "uuid", "wire (uuid)")],
    "SheetJunction": [(None, "at", "junction (at)"), ("diameter", "diameter", "junction (diameter)"), ("color", "color", "junction (color r g b a)"), ("uuid", "uuid", "junction (uuid)")],
    "SheetNoConnect": [(None, "at", "no_connect (at)"), ("uuid", "uuid", "no_connect (uuid)")],
    "SheetLabel": [
        (None, "text", "label text"), ("at", "at", "label (at x y)"), ("angle", "angle", "label (at x y angle)"),
        ("size", "size", "label (effects (font (size s s)))"), ("justify", "justify", "label (effects (justify h v))"), ("uuid", "uuid", "label (uuid)"),
    ],
    "SheetRect": [
        (None, "start", "rectangle (start)"), (None, "end", "rectangle (end)"), ("width", "width", "rectangle (stroke (width))"),
        ("style", "style", "rectangle (stroke (type))"), ("fill", "fill", "rectangle (fill (type))"), ("uuid", "uuid", "rectangle (uuid)"),
    ],
    "SheetText": [
        (None, "text", "text text"), ("at", "at", "text (at x y)"), ("angle", "angle", "text (at x y angle)"),
        ("exclude_from_sim", "exclude_from_sim", "text (exclude_from_sim)"), ("size", "size", "text (effects (font (size s s)))"),
        ("justify", "justify", "text (effects (justify ...))"), ("uuid", "uuid", "text (uuid)"),
    ],
}


def _yn(v: bool) -> str:
    return "yes" if v else "no"


def _effects(ind: str, size: float, justify: tuple[str, ...]) -> list[str]:
    out = [f"{ind}(effects", f"{ind}\t(font", f"{ind}\t\t(size {num(size)} {num(size)})", f"{ind}\t)"]
    if justify:
        out.append(f"{ind}\t(justify {' '.join(justify)})")
    out.append(f"{ind})")
    return out


def write(p) -> str:
    """One primitive, in the layout KiCad 10 saves it with."""
    k = p.kind
    if k == "rectangle":
        lines = [
            "\t(rectangle", f"\t\t(start {num(p.start[0])} {num(p.start[1])})", f"\t\t(end {num(p.end[0])} {num(p.end[1])})",
            "\t\t(stroke", f"\t\t\t(width {num(p.width)})", f"\t\t\t(type {p.style})", "\t\t)", "\t\t(fill", f"\t\t\t(type {p.fill})", "\t\t)",
            f"\t\t(uuid {quote(p.uuid)})", "\t)",
        ]
    elif k == "text":
        lines = [f"\t(text {quote(p.text)}", f"\t\t(exclude_from_sim {_yn(p.exclude_from_sim)})", f"\t\t(at {num(p.at[0])} {num(p.at[1])} {_ang(p.angle)})"]
        lines += _effects("\t\t", p.size, p.justify) + [f"\t\t(uuid {quote(p.uuid)})", "\t)"]
    elif k == "junction":
        c = " ".join(num(v) for v in p.color)
        lines = ["\t(junction", f"\t\t(at {num(p.at[0])} {num(p.at[1])})", f"\t\t(diameter {num(p.diameter)})", f"\t\t(color {c})", f"\t\t(uuid {quote(p.uuid)})", "\t)"]
    elif k == "no_connect":
        lines = ["\t(no_connect", f"\t\t(at {num(p.at[0])} {num(p.at[1])})", f"\t\t(uuid {quote(p.uuid)})", "\t)"]
    elif k == "wire":
        lines = [
            "\t(wire", "\t\t(pts", f"\t\t\t(xy {num(p.a[0])} {num(p.a[1])}) (xy {num(p.b[0])} {num(p.b[1])})", "\t\t)",
            "\t\t(stroke", f"\t\t\t(width {num(p.width)})", f"\t\t\t(type {p.style})", "\t\t)", f"\t\t(uuid {quote(p.uuid)})", "\t)",
        ]
    elif k == "label":
        lines = [f"\t(label {quote(p.text)}", f"\t\t(at {num(p.at[0])} {num(p.at[1])} {_ang(p.angle)})"]
        lines += _effects("\t\t", p.size, p.justify) + [f"\t\t(uuid {quote(p.uuid)})", "\t)"]
    elif k == "symbol":
        lines = ["\t(symbol", f"\t\t(lib_id {quote(p.lib_id)})", f"\t\t(at {num(p.at[0])} {num(p.at[1])} {_ang(p.angle)})"]
        if p.mirror:
            lines.append(f"\t\t(mirror {p.mirror})")
        lines += [
            f"\t\t(unit {p.unit})", f"\t\t(body_style {p.body_style})", f"\t\t(exclude_from_sim {_yn(p.exclude_from_sim)})",
            f"\t\t(in_bom {_yn(p.in_bom)})", f"\t\t(on_board {_yn(p.on_board)})", f"\t\t(in_pos_files {_yn(p.in_pos_files)})",
            f"\t\t(dnp {_yn(p.dnp)})", f"\t\t(uuid {quote(p.uuid)})",
        ]
        for f in p.fields:
            lines += [f"\t\t(property {quote(f.name)} {quote(f.value)}", f"\t\t\t(at {num(f.at[0])} {num(f.at[1])} {_ang(f.angle)})"]
            if f.hide:
                lines.append("\t\t\t(hide yes)")
            lines += [f"\t\t\t(show_name {_yn(f.show_name)})", f"\t\t\t(do_not_autoplace {_yn(f.do_not_autoplace)})"]
            lines += _effects("\t\t\t", f.size, f.justify) + ["\t\t)"]
        for n, u in p.pins:
            lines += [f"\t\t(pin {quote(n)}", f"\t\t\t(uuid {quote(u)})", "\t\t)"]
        lines.append("\t)")
    else:  # pragma: no cover - every kind has a branch
        raise TypeError(k)
    return "\n".join(lines) + "\n"


def sort_key(p) -> tuple:
    return (ORDER.index(p.kind), p.uuid)


@dataclass
class Header:
    """The sheet's header and the derived library: version, generator, the sheet uuid, the paper, the title, and the
    `lib_symbols` text (each library symbol as `sch_emit` reads it from the part's `.kicad_sym`)."""

    uuid: str
    paper: str
    title: str
    libs: str = ""
    version: str = "20260306"
    generator: str = "pcbc"
    generator_version: str = "0.4"


def sheet_text(header: Header, prims) -> str:
    """The one writer of `schematic.kicad_sch`."""
    body = "".join(write(p) for p in sorted(prims, key=sort_key))
    return (
        "(kicad_sch\n"
        f"\t(version {header.version})\n"
        f"\t(generator {quote(header.generator)})\n"
        f"\t(generator_version {quote(header.generator_version)})\n"
        f"\t(uuid {quote(header.uuid)})\n"
        f"\t(paper {quote(header.paper)})\n"
        "\t(title_block\n"
        f"\t\t(title {quote(header.title)})\n"
        "\t)\n"
        "\t(lib_symbols\n" + header.libs + "\t)\n"
        + body
        + "\t(sheet_instances\n"
        '\t\t(path "/"\n'
        '\t\t\t(page "1")\n'
        "\t\t)\n"
        "\t)\n"
        "\t(embedded_fonts no)\n"
        ")\n"
    )


# ------------------------------------------------------------------ decompile


def _f(a) -> float:
    return float(a)


def _kids(node: list, where: str, allowed: set[str]) -> dict[str, list]:
    out: dict[str, list] = {}
    for c in node[1:]:
        if not isinstance(c, list):
            continue
        h = c[0]
        if h not in allowed:
            raise Unmapped(f"{where}: ({h} ...) has no field in sch_prims")
        out.setdefault(h, []).append(c)
    return out


def _flag(node: list | None) -> bool:
    return bool(node) and str(node[1]) == "yes"


def _effects_of(node: list, where: str) -> tuple[float, tuple[str, ...], bool]:
    k = _kids(node, where + " effects", {"font", "justify", "hide"})
    font = _kids(k["font"][0], where + " font", {"size"})
    size = _f(font["size"][0][1])
    just = tuple(str(a) for a in k["justify"][0][1:]) if "justify" in k else ()
    hide = "hide" in k
    if any(not isinstance(a, list) and str(a) == "hide" for a in node[1:]):
        hide = True
    return size, just, hide


def _at(node: list) -> tuple[tuple[float, float], float]:
    return (_f(node[1]), _f(node[2])), (_f(node[3]) if len(node) > 3 else 0.0)


def _prim(node: list):
    h = node[0]
    where = f"({h})"
    if h == "rectangle":
        k = _kids(node, where, {"start", "end", "stroke", "fill", "uuid"})
        st = _kids(k["stroke"][0], where + " stroke", {"width", "type"})
        fl = _kids(k["fill"][0], where + " fill", {"type"})
        return SheetRect((_f(k["start"][0][1]), _f(k["start"][0][2])), (_f(k["end"][0][1]), _f(k["end"][0][2])), _f(st["width"][0][1]), str(st["type"][0][1]), str(fl["type"][0][1]), str(k["uuid"][0][1]))
    if h == "text":
        k = _kids(node, where, {"exclude_from_sim", "at", "effects", "uuid"})
        at, ang = _at(k["at"][0])
        size, just, _hide = _effects_of(k["effects"][0], where)
        return SheetText(str(node[1]), at, ang, _flag(k.get("exclude_from_sim", [None])[0]), size, just, str(k["uuid"][0][1]))
    if h == "junction":
        k = _kids(node, where, {"at", "diameter", "color", "uuid"})
        return SheetJunction((_f(k["at"][0][1]), _f(k["at"][0][2])), _f(k["diameter"][0][1]), tuple(_f(v) for v in k["color"][0][1:5]), str(k["uuid"][0][1]))
    if h == "no_connect":
        k = _kids(node, where, {"at", "uuid"})
        return SheetNoConnect((_f(k["at"][0][1]), _f(k["at"][0][2])), str(k["uuid"][0][1]))
    if h == "wire":
        k = _kids(node, where, {"pts", "stroke", "uuid"})
        xy = [c for c in k["pts"][0][1:] if isinstance(c, list)]
        if len(xy) != 2 or any(c[0] != "xy" for c in xy):
            raise Unmapped(f"{where}: a wire of {len(xy)} points; sch_prims carries a wire of two")
        st = _kids(k["stroke"][0], where + " stroke", {"width", "type"})
        return SheetWire((_f(xy[0][1]), _f(xy[0][2])), (_f(xy[1][1]), _f(xy[1][2])), _f(st["width"][0][1]), str(st["type"][0][1]), str(k["uuid"][0][1]))
    if h == "label":
        k = _kids(node, where, {"at", "effects", "uuid"})
        at, ang = _at(k["at"][0])
        size, just, _hide = _effects_of(k["effects"][0], where)
        return SheetLabel(str(node[1]), at, ang, size, just, str(k["uuid"][0][1]))
    if h == "symbol":
        k = _kids(node, where, {"lib_id", "at", "mirror", "unit", "body_style", "exclude_from_sim", "in_bom", "on_board", "in_pos_files", "dnp", "uuid", "property", "pin"})
        at, ang = _at(k["at"][0])
        flds = []
        for pr in k.get("property", []):
            pk = _kids(pr, where + " property", {"at", "hide", "show_name", "do_not_autoplace", "effects"})
            pat, pang = _at(pk["at"][0])
            size, just, hide = _effects_of(pk["effects"][0], where + " property")
            hide = hide or _flag(pk.get("hide", [None])[0])
            flds.append(SheetField(str(pr[1]), str(pr[2]), pat, pang, hide, _flag(pk.get("show_name", [None])[0]), _flag(pk.get("do_not_autoplace", [None])[0]), size, just))
        pins = []
        for pn in k.get("pin", []):
            pk = _kids(pn, where + " pin", {"uuid"})
            pins.append((str(pn[1]), str(pk["uuid"][0][1])))
        one = lambda t, d: k[t][0] if t in k else d  # noqa: E731
        return SheetSymbol(
            str(k["lib_id"][0][1]), at, ang, str(k["mirror"][0][1]) if "mirror" in k else None, int(one("unit", [0, 1])[1]),
            int(one("body_style", [0, 1])[1]), _flag(one("exclude_from_sim", None)), _flag(one("in_bom", [0, "yes"])), _flag(one("on_board", [0, "yes"])),
            _flag(one("in_pos_files", [0, "yes"])), _flag(one("dnp", None)), str(k["uuid"][0][1]), tuple(flds), tuple(pins),
        )
    raise Unmapped(f"({h} ...) is no primitive sch_prims carries")


_TOP = {"version", "generator", "generator_version", "uuid", "paper", "title_block", "lib_symbols", "sheet_instances", "embedded_fonts"}


def decompile(text: str) -> tuple[Header, list]:
    """A `.kicad_sch` (pcbc's, or the same sheet KiCad saved) as its header and its primitives, field for field."""
    tree = parse_tree(text)
    if tree[0] != "kicad_sch":
        raise Unmapped("not a kicad_sch")
    head: dict[str, list] = {}
    prims = []
    for node in tree[1:]:
        if not isinstance(node, list):
            continue
        if node[0] in _TOP:
            head[node[0]] = node
        elif node[0] in _BY_KIND:
            prims.append(_prim(node))
        else:
            raise Unmapped(f"({node[0]} ...) at the sheet's top level has no primitive in sch_prims")
    title = ""
    for c in head.get("title_block", [])[1:]:
        if isinstance(c, list) and c[0] == "title":
            title = str(c[1])
    libs = ""
    if "lib_symbols" in head and "\t(lib_symbols\n" in text:
        # The derived library is kept as text: it is the parts' .kicad_sym, not layout.
        libs = _lib_text(text, text.index("\t(lib_symbols\n") + len("\t(lib_symbols\n"))
    hdr = Header(str(head["uuid"][1]), str(head["paper"][1]), title, libs, str(head["version"][1]), str(head["generator"][1]), str(head.get("generator_version", [0, ""])[1]))
    return hdr, prims


def _lib_text(text: str, start: int) -> str:
    """The text between `(lib_symbols` and its closing parenthesis (the derived library), as written."""
    depth, i, n = 1, start, len(text)
    in_str = False
    while i < n:
        c = text[i]
        if in_str:
            if c == "\\":
                i += 2
                continue
            if c == '"':
                in_str = False
        elif c == '"':
            in_str = True
        elif c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                # back to the start of the closing line ("\t)")
                j = text.rfind("\n", 0, i) + 1
                return text[start:j]
        i += 1
    raise Unmapped("lib_symbols is not closed")


# ------------------------------------------------------------------ gen lines


def _py(v) -> str:
    if isinstance(v, bool) or v is None:
        return repr(v)
    if isinstance(v, (int, float)):
        return num(v) if isinstance(v, float) else str(v)
    if isinstance(v, str):
        return '"' + v.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\r", "\\r") + '"'
    if isinstance(v, SheetField):
        return line_of(v)
    if isinstance(v, tuple):
        inner = ", ".join(_py(x) for x in v)
        return f"({inner}{',' if len(v) == 1 else ''})"
    raise TypeError(type(v))


def line_of(p) -> str:
    """A primitive as one gen line: every field explicit, in `FIELDS` order."""
    name = type(p).__name__
    parts = []
    for kw, attr, _tok in FIELDS[name]:
        v = getattr(p, attr)
        if attr in ("at", "a", "b", "start", "end", "color"):
            v = tuple(float(x) for x in v)
        if attr in ("angle", "width", "diameter", "size"):
            v = float(v)
        parts.append(_py(v) if kw is None else f"{kw}={_py(v)}")
    return f"{name}({', '.join(parts)})"


def gen_lines(prims) -> list[str]:
    return [line_of(p) for p in sorted(prims, key=sort_key)]


def _ctor(cls):
    names = {attr: kw for kw, attr, _t in FIELDS[cls.__name__]}
    pos = [attr for kw, attr, _t in FIELDS[cls.__name__] if kw is None]

    def make(*args, **kw):
        vals = dict(zip(pos, args))
        for k, v in kw.items():
            attr = next((a for a, n in names.items() if n == k), None)
            if attr is None:
                raise TypeError(f"{cls.__name__}() takes no {k}=")
            vals[attr] = v
        for f in dc_fields(cls):
            if f.name in vals and isinstance(vals[f.name], list):
                vals[f.name] = tuple(vals[f.name])
        return cls(**vals)

    return make


def namespace() -> dict:
    """The constructors a gen file's primitive lines call."""
    ns = {c.__name__: _ctor(c) for c in PRIMS}
    ns["SheetField"] = _ctor(SheetField)
    return ns


def load_gen(text: str) -> list:
    """The primitives of a gen file: every `Sheet*` line, executed with the constructors above (the lock lines are the
    core vocabulary's and are not read here)."""
    ns = namespace()
    out = []
    for i, ln in enumerate(text.splitlines(), 1):
        if ln.startswith(tuple(c.__name__ + "(" for c in PRIMS)):
            try:
                out.append(eval(compile(ln, f"schematic.gen.py:{i}", "eval"), {"__builtins__": {}}, ns))  # noqa: S307
            except Exception as exc:  # noqa: BLE001
                raise ValueError(f"schematic.gen.py:{i}: {type(exc).__name__}: {exc}") from None
    return out


# ------------------------------------------------------------------ census


def census(text: str) -> dict[str, dict[str, int]]:
    """Every token under every top-level primitive of a sheet, counted (the zero-unmapped bar)."""
    from collections import Counter

    tree = parse_tree(text)
    out: dict[str, Counter] = {}

    def walk(node, kind, path):
        for c in node[1:]:
            if isinstance(c, list) and c:
                p = f"{path}/{c[0]}"
                out.setdefault(kind, Counter())[p] += 1
                walk(c, kind, p)

    for node in tree[1:]:
        if isinstance(node, list) and node and node[0] in _BY_KIND:
            walk(node, node[0], node[0])
    return {k: dict(v) for k, v in out.items()}


# The token paths each primitive maps (`write` writes them all; `decompile` reads them all).
MAPPED: dict[str, frozenset[str]] = {
    "rectangle": frozenset({"rectangle/start", "rectangle/end", "rectangle/stroke", "rectangle/stroke/width", "rectangle/stroke/type", "rectangle/fill", "rectangle/fill/type", "rectangle/uuid"}),
    "text": frozenset({"text/exclude_from_sim", "text/at", "text/effects", "text/effects/font", "text/effects/font/size", "text/effects/justify", "text/uuid"}),
    "junction": frozenset({"junction/at", "junction/diameter", "junction/color", "junction/uuid"}),
    "no_connect": frozenset({"no_connect/at", "no_connect/uuid"}),
    "wire": frozenset({"wire/pts", "wire/pts/xy", "wire/stroke", "wire/stroke/width", "wire/stroke/type", "wire/uuid"}),
    "label": frozenset({"label/at", "label/effects", "label/effects/font", "label/effects/font/size", "label/effects/justify", "label/uuid"}),
    "symbol": frozenset({
        "symbol/lib_id", "symbol/at", "symbol/mirror", "symbol/unit", "symbol/body_style", "symbol/exclude_from_sim", "symbol/in_bom",
        "symbol/on_board", "symbol/in_pos_files", "symbol/dnp", "symbol/uuid", "symbol/property", "symbol/property/at", "symbol/property/hide",
        "symbol/property/show_name", "symbol/property/do_not_autoplace", "symbol/property/effects", "symbol/property/effects/font",
        "symbol/property/effects/font/size", "symbol/property/effects/justify", "symbol/pin", "symbol/pin/uuid",
    }),
}


def unmapped(counts: dict[str, dict[str, int]]) -> list[str]:
    return sorted(p for kind, c in counts.items() for p in c if p not in MAPPED.get(kind, frozenset()))
