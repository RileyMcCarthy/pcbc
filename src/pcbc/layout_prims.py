"""KiCad 10 board drawings as schemas: one Python object per KiCad primitive, one keyword per KiCad field.

A board drawing (`gr_line`, `gr_rect`, `gr_circle`, `gr_arc`, `gr_poly`, `gr_curve`, `gr_text`,
`gr_text_box`, `dimension`, `group`, `image`, `table`, `barcode`, `target`, `point`, `generated`) is
described once here, as the ordered list of fields KiCad 10.0.6 writes for it. The same description
is the Python constructor's keyword list (`language.py`), the decompiler (`read`), the writer
(`write`), the line `layout.gen.py` writes (`gen.line_of`), the census map (`census_map`) and the docs
table (`docs/layout-properties.md`). There is no second place a field can be forgotten in.

Where the field list came from, and how to re-check it (`docs/layout-properties.md`, "Drawings"):

- `kicad-cli pcb upgrade --force` on a hand-written board: KiCad loads it and writes it back in its own
  format, so what it writes is its formatter's output for every token its parser accepted;
- the parser's own refusal (`Expecting a, b or c. Got 'zzbogus'`) at every level of every drawing,
  which lists the tokens that level accepts;
- boards KiCad 10.0.6's `pcbnew` Python module saved with every option it exposes set
  (`tests/fixtures/kicad10/make_kicad10_primitives3.py`).

A token KiCad accepts on read but never writes (`tstamp`, the old bare `width`, `status`) is not a
field: the bar is "every field KiCad writes". A token KiCad writes that is derived rather than set
(`render_cache`, the outline-font polygons of a text) is read and dropped, never written, and KiCad
regenerates it on its save; `docs/layout-properties.md` records that and the proof.

Numbers: a length is written to KiCad's 1 nm (`sexp.fmt_num`); a ratio, an angle, a percentage or an
area is a double in KiCad and is written exactly (`sexp.fmt_exact`), so nothing is cut to six decimals.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .sexp import Q, fmt_exact, fmt_num, quote

REQ = object()  # `omit`: KiCad always writes it. `default`: a hand-written line must give it.


class Unmapped(ValueError):
    """A token KiCad wrote that no Python field carries. Fix the schema, never skip the token."""


# ----------------------------------------------------------------------------------- the schema


@dataclass(frozen=True)
class Atom:
    """One value. `kind` says how it is spelled; `omit` is the value at which KiCad writes nothing
    (REQ: always written); `default` is a hand-written line's value when it leaves the field out."""

    kw: str
    kind: str
    omit: object = REQ
    default: object = REQ
    flag: str = ""  # kind "flag": the bare atom whose presence is True
    doc: str = ""
    # kind "sym" / "atoms": the words KiCad's parser accepts **and its save keeps** for this field
    # (`WORDS` below). A hand-written value outside the list is refused at load, naming the line;
    # empty means the field is free text (a layer name, a generator type).
    words: tuple = ()


@dataclass(frozen=True)
class Leaf:
    """`(tok atom ...)`: a child with atoms only."""

    tok: str
    atoms: tuple[Atom, ...]


@dataclass(frozen=True)
class Node:
    """`(tok child ...)`: a container. Its children's keywords are fields of the object itself
    (`stroke` -> `width`, `stroke_type`). Written when any child is written."""

    tok: str
    slots: tuple


@dataclass(frozen=True)
class DictNode:
    """`(tok child ...)` whose children are one dict-valued field, keyed by KiCad's own token names
    (`dimension (format (prefix) (units) ...)` -> `format={"prefix": ..., "units": ...}`)."""

    tok: str
    kw: str
    slots: tuple
    omit: object = None
    default: object = None


@dataclass(frozen=True)
class Head:
    """The atom right after the primitive's own head: `(gr_text "A" ...)`, `(target plus ...)`."""

    atom: Atom


@dataclass(frozen=True)
class Sub:
    """`(tok ...)` that is itself a primitive: the dimension's own `gr_text`. A dict field."""

    tok: str
    kw: str
    schema: "Schema"
    omit: object = None
    default: object = None


@dataclass(frozen=True)
class Many:
    """Every `(tok ...)` child of a container, each a primitive: a table's `table_cell`s."""

    tok: str
    kw: str
    schema: "Schema"


@dataclass(frozen=True)
class Props:
    """A generator's own properties: every child no other slot claims, `(key value)`, as a dict in
    KiCad's order. KiCad's `generated` object is a key/value bag by design; the keys are KiCad's."""

    kw: str


@dataclass(frozen=True)
class Cache:
    """KiCad's derived cache (`render_cache`): read and dropped, never written; KiCad regenerates it."""

    tok: str


@dataclass(frozen=True)
class Schema:
    head: str  # the KiCad token
    name: str  # the Python constructor
    slots: tuple
    positional: tuple[str, ...] = ()
    doc: str = ""
    kws: tuple[str, ...] = field(default=())

    def fields(self) -> list[Atom | DictNode | Sub | Many | Props]:
        """Every Python field, in KiCad's order."""
        return list(_fields(self.slots))


def _fields(slots):
    for s in slots:
        if isinstance(s, Head):
            yield s.atom
        elif isinstance(s, Leaf):
            yield from s.atoms
        elif isinstance(s, Node):
            yield from _fields(s.slots)
        elif isinstance(s, (DictNode, Sub, Many, Props)):
            yield s


def kw_of(f) -> str:
    return f.kw


def default_of(f):
    return f.default if hasattr(f, "default") else ([] if isinstance(f, Many) else {} if isinstance(f, Props) else REQ)


def omit_of(f):
    if isinstance(f, Many):
        return ()
    if isinstance(f, Props):
        return REQ
    return f.omit


# ----------------------------------------------------------------------------------- KiCad's words

# Every word-valued field of a layout object and the words KiCad 10.0.6's parser accepts for it and
# its save keeps. **One source**: the drawing schemas below take their `words` from here, the copper
# constructors (`language.Via`, `language.Pour`) check against here, and the docs table is printed
# from here. A word outside the list is refused at load naming the line — until the fourth review
# (C3) eight of fifteen such lines routed a whole board and then died in a `RuntimeError` traceback
# out of `kicad-cli` ("Expecting none, edge, or full. Got 'bogus'"). The lists are KiCad's own
# parser messages (`pcb_parser.cpp`, quoted in that traceback) narrowed to what its **save** keeps,
# measured with `kicad-cli pcb upgrade` (`tests/fixtures/kicad10/kicad10_words.kicad_pcb`): a via's
# `(tenting (front none) (back none))` is dropped on save (none = inherit the board's; one side
# `none` beside a set side is kept, probe 2), a zone's `(smoothing none)` and `(fill (mode polygon))`
# are dropped too, so those spellings are "leave the field out", not words.
WORDS: dict[str, tuple] = {
    "stroke_type": ("solid", "dash", "dash_dot", "dash_dot_dot", "dot", "default"),
    "justify": ("left", "right", "top", "bottom", "mirror"),
    "fill": ("hatch", "reverse_hatch", "cross_hatch"),  # plus yes/no as True/False; `solid` and `none` are read as those
    # The spellings of a plain fill a hand-written line may use, read as True/False (what KiCad keeps).
    "fill.plain": ("yes", "no", "solid", "none"),
    "dimension.kind": ("aligned", "orthogonal", "radial", "leader", "center"),
    "dimension.style.arrow_direction": ("inward", "outward"),
    # A dimension's integer enums: KiCad clamps anything past the last to it on load (measured,
    # `fix6/facts.py`: units 4 -> 0 [sic, an unknown unit is inches], units_format 4 -> 3,
    # text_position_mode 4 -> 3, text_frame 4 -> 3), so a value outside is refused at load.
    "dimension.format.units": (0, 1, 2, 3),  # inches, mils, millimetres, automatic
    "dimension.format.units_format": (0, 1, 2, 3),  # no suffix, bare suffix, parenthesised suffix, ...
    "dimension.style.text_position_mode": (0, 1, 2, 3),  # outside, inline, manual, ...
    "dimension.style.text_frame": (0, 1, 2, 3),  # none, rectangle, circle, rounded rectangle
    "barcode.kind": ("qr", "microqr", "code39", "code128", "datamatrix"),
    "barcode.ecc_level": ("L", "M", "Q", "H"),
    # A micro QR code has no H level: KiCad's save turns H into Q (measured, `fix6/facts.py`); the
    # linear codes and data matrix carry no level at all and KiCad drops one written on them.
    "barcode.ecc_level.microqr": ("L", "M", "Q"),
    "target.shape": ("plus", "x"),
    "generated.type": ("tuning_pattern",),
    # A tuning pattern's own word-valued properties (KiCad rewrites anything else to its default on
    # load: `initial_side "bogus"` comes back `default`, `tuning_mode "bogus"` comes back `single`).
    "generated.tuning_pattern.initial_side": ("default", "left", "right"),
    "generated.tuning_pattern.tuning_mode": ("single", "diff_pair", "diff_pair_skew"),
    "via.kind": ("through", "blind", "buried", "micro"),
    "via.tenting": ("yes", "no", "none"),  # both sides `none` is dropped by KiCad's save (`language._sided` refuses it): leave tenting out
    "via.covering": ("yes", "no", "none"),
    "via.plugging": ("yes", "no", "none"),
    "via.padstack.mode": ("front_inner_back", "custom"),
    "via.post_machining.mode": ("counterbore", "countersink"),
    "pour.hatch": ("none", "edge", "full"),
    "pour.connect": ("thermal", "solid", "none", "thru_hole_only"),
    "pour.fill_mode": ("hatch",),  # None is solid: KiCad writes no `(mode)` and drops `(mode polygon)`
    "pour.smoothing": ("chamfer", "fillet"),  # None: KiCad drops `(smoothing none)`
    "pour.hatch_border_algorithm": ("hatch_thickness", "min_thickness"),
    "pour.island_removal_mode": (0, 1, 2),
    "pour.hatch_smoothing_level": (0, 1, 2, 3),
    "pour.keepout": ("allowed", "not_allowed"),
    "pour.teardrop_type": ("padvia", "track_end"),
}

# KiCad's layer ids (the `(layers ...)` table every board carries, `seed._layers`): the order KiCad
# writes a zone's `(layers ...)` in (its LSET, by id), and its parser's own `Expecting ...` order.
_LAYER_ID = {
    "F.Cu": 0, "B.Cu": 2, "F.Mask": 1, "B.Mask": 3, "F.SilkS": 5, "B.SilkS": 7, "F.Adhes": 9, "B.Adhes": 11,
    "F.Paste": 13, "B.Paste": 15, "Dwgs.User": 17, "Cmts.User": 19, "Eco1.User": 21, "Eco2.User": 23,
    "Edge.Cuts": 25, "Margin": 27, "B.CrtYd": 29, "F.CrtYd": 31, "B.Fab": 33, "F.Fab": 35,
}  # fmt: skip


def layer_id(name: str) -> int:
    """KiCad's id for a layer name: `In<k>.Cu` is 2k+2, `User.<n>` 37+2n; an unknown name sorts last."""
    if name in _LAYER_ID:
        return _LAYER_ID[name]
    import re

    m = re.fullmatch(r"In(\d+)\.Cu", name)
    if m:
        return 2 * int(m.group(1)) + 2
    m = re.fullmatch(r"User\.(\d+)", name)
    if m:
        return 37 + 2 * int(m.group(1))
    return 10**6


def lset_order(layers) -> tuple[str, ...]:
    """`layers` in the order KiCad's save writes a zone's `(layers ...)`: by layer id (F.Cu, B.Cu,
    In1.Cu, In2.Cu, ...; measured, `kicad-cli pcb upgrade`). A hand-written `Pour(layers=...)` is
    put in this order at load so KiCad's save keeps it as written (fourth review, C3: a rule area
    written in stackup order was refused after routing as "the router dropped it")."""
    return tuple(sorted((str(x) for x in layers), key=lambda n: (layer_id(n), n)))


def stack_order(a: str, b: str) -> tuple[str, str]:
    """A via's two layers as `(top, bottom)` in the copper stack's order (F.Cu, In1.Cu, ..., B.Cu):
    KiCad's save writes `(layers "F.Cu" "B.Cu")` for a via handed `("B.Cu", "F.Cu")` (measured)."""

    def key(n: str) -> tuple:
        if n == "F.Cu":
            return (0, 0)
        if n == "B.Cu":
            return (2, 0)
        import re

        m = re.fullmatch(r"In(\d+)\.Cu", n)
        return (1, int(m.group(1))) if m else (3, n)

    return tuple(sorted((str(a), str(b)), key=key))  # type: ignore[return-value]


_UUID = None


def uuid_check(value, who: str) -> str:
    """A hand-written `uuid=` in the one form KiCad keeps: lowercase `8-4-4-4-12` hex. Anything else
    is refused at load naming the line, the uuid, and what KiCad would keep of it (an uppercase one
    comes back lowercase, measured; a legacy 8-hex timestamp comes back as a uuid KiCad invents),
    so the object is never looked for on the router's board under a uuid KiCad rewrote (fourth
    review, C3 minor)."""
    import re

    s = str(value)
    if re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", s):
        return s
    if re.fullmatch(r"[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}", s):
        raise ValueError(f"{who}: uuid {s!r} is not what KiCad keeps: its save writes it lowercase, {s.lower()!r}; write that")
    raise ValueError(f"{who}: uuid {s!r} is not a uuid KiCad keeps (lowercase 8-4-4-4-12 hex); KiCad rewrites anything else on load, so the object could not be found again. Write one in that form, or leave uuid= out and one is derived from id=")


# ----------------------------------------------------------------------------------- shared parts

A = Atom
YES = dict(omit=False, default=False)


def _stroke(width_kw: str = "width", type_kw: str = "stroke_type", color_kw: str = "stroke_color", *, width_default: object = REQ, type_default: object = "solid") -> Node:
    """`(stroke (width) (type) (color r g b a))`."""
    return Node(
        "stroke",
        (
            Leaf("width", (A(width_kw, "len", default=width_default),)),
            Leaf("type", (A(type_kw, "sym", default=type_default, doc="solid|dash|dot|dash_dot|dash_dot_dot|default", words=WORDS["stroke_type"]),)),
            Leaf("color", (A(color_kw, "reals", omit=None, default=None, doc="r g b a"),)),
        ),
    )


def _layer(*, default: object = REQ, knockout: bool = False) -> tuple:
    atoms = (A("layer", "str", omit=None if not knockout else REQ, default=default),)
    if knockout:
        atoms += (A("knockout", "flag", flag="knockout", **YES),)
        return (Leaf("layer", atoms),)
    # A drawing on copper with its mask opened is `(layers "F.Cu" "F.Mask")` in place of `(layer)`.
    return (Leaf("layer", atoms), Leaf("layers", (A("layers", "strs", omit=None, default=None),)))


def _effects(size_default=(1.0, 1.0), thickness_default=0.15) -> Node:
    return Node(
        "effects",
        (
            Node(
                "font",
                (
                    Leaf("face", (A("face", "str", omit=None, default=None),)),
                    Leaf("size", (A("size", "lens", default=size_default, doc="(height, width) as KiCad writes it"),)),
                    Leaf("line_spacing", (A("line_spacing", "real", omit=None, default=None),)),
                    Leaf("thickness", (A("thickness", "len", omit=None, default=thickness_default),)),
                    Leaf("bold", (A("bold", "yn", **YES),)),
                    Leaf("italic", (A("italic", "yn", **YES),)),
                ),
            ),
            Leaf("justify", (A("justify", "atoms", omit=(), default=(), doc="left|right, top|bottom, mirror", words=WORDS["justify"]),)),
            Leaf("hide", (A("hide", "yn", **YES),)),
        ),
    )


LOCKED = Leaf("locked", (A("locked", "yn", **YES),))
UUID = Leaf("uuid", (A("uuid", "str", default=None),))
NET = Leaf("net", (A("net", "str", omit=None, default=None),))
MASK_MARGIN = Leaf("solder_mask_margin", (A("solder_mask_margin", "len", omit=None, default=None),))
FILL = Leaf("fill", (A("fill", "fill", default=False, doc="yes|no|hatch|reverse_hatch|cross_hatch"),))


def _shape(head: str, name: str, geometry: tuple, positional: tuple, *, fill: bool, doc: str, width: object = 0.15, layer: object = "F.SilkS") -> Schema:
    slots = geometry + (_stroke(width_default=width),) + ((FILL,) if fill else ()) + (LOCKED,) + _layer(default=layer) + (MASK_MARGIN, NET, UUID)
    return Schema(head, name, slots, positional, doc)


def _text_schema(head: str, name: str) -> Schema:
    """`gr_text`, and the text a dimension carries (the same object, nested)."""
    return Schema(
        head,
        name,
        (
            Head(A("text", "str")),
            LOCKED,
            Leaf("at", (A("at", "xy"), A("angle", "real", default=0.0))),
            Leaf("layer", (A("layer", "str", default="F.SilkS"), A("knockout", "flag", flag="knockout", **YES))),
            UUID,
            _effects(),
            Cache("render_cache"),
        ),
        ("text", "at"),
        "A KiCad `gr_text`.",
    )


TEXT = _text_schema("gr_text", "Text")

TEXTBOX = Schema(
    "gr_text_box",
    "TextBox",
    (
        Head(A("text", "str")),
        LOCKED,
        Leaf("start", (A("start", "xy", omit=None, default=None),)),
        Leaf("end", (A("end", "xy", omit=None, default=None),)),
        Leaf("pts", (A("pts", "pts", omit=None, default=None, doc="the four corners KiCad writes for a turned box"),)),
        Leaf("margins", (A("margins", "lens", omit=None, default=None, doc="left top right bottom"),)),
        Leaf("angle", (A("angle", "real", omit=None, default=None),)),
        Leaf("layer", (A("layer", "str", default="F.SilkS"),)),
        UUID,
        _effects(),
        Leaf("border", (A("border", "yn", default=True),)),
        _stroke(width_default=0.15),
        Leaf("knockout", (A("knockout", "yn", default=False),)),
        Cache("render_cache"),
    ),
    ("text", "start", "end"),
    "A KiCad `gr_text_box`.",
)

DIM_FORMAT = (
    Leaf("prefix", (A("prefix", "str"),)),
    Leaf("suffix", (A("suffix", "str"),)),
    Leaf("units", (A("units", "int", words=WORDS["dimension.format.units"]),)),
    Leaf("units_format", (A("units_format", "int", words=WORDS["dimension.format.units_format"]),)),
    Leaf("precision", (A("precision", "int"),)),
    Leaf("override_value", (A("override_value", "str", omit=None),)),
    Leaf("suppress_zeroes", (A("suppress_zeroes", "yn", omit=False),)),
)
DIM_STYLE = (
    Leaf("thickness", (A("thickness", "len"),)),
    Leaf("arrow_length", (A("arrow_length", "len"),)),
    Leaf("text_position_mode", (A("text_position_mode", "int", words=WORDS["dimension.style.text_position_mode"]),)),
    Leaf("arrow_direction", (A("arrow_direction", "sym", omit=None, words=WORDS["dimension.style.arrow_direction"]),)),
    Leaf("extension_height", (A("extension_height", "len", omit=None),)),
    Leaf("text_frame", (A("text_frame", "int", omit=None, words=WORDS["dimension.style.text_frame"]),)),
    Leaf("extension_offset", (A("extension_offset", "len"),)),
    Leaf("keep_text_aligned", (A("keep_text_aligned", "yn", omit=None),)),
)
DIM_TEXT = _text_schema("gr_text", "Text")

DIMENSION = Schema(
    "dimension",
    "Dimension",
    (
        Leaf("type", (A("kind", "sym", default="aligned", doc="aligned|orthogonal|radial|leader|center", words=WORDS["dimension.kind"]),)),
        LOCKED,
        Leaf("layer", (A("layer", "str", default="Dwgs.User"),)),
        UUID,
        Leaf("pts", (A("pts", "pts"),)),
        Leaf("height", (A("height", "len", omit=None, default=None),)),
        Leaf("orientation", (A("orientation", "int", omit=None, default=None),)),
        Leaf("leader_length", (A("leader_length", "len", omit=None, default=None),)),
        DictNode("format", "format", DIM_FORMAT),
        DictNode("style", "style", DIM_STYLE),
        Sub("gr_text", "gr_text", DIM_TEXT),
    ),
    ("pts",),
    "A KiCad `dimension`. KiCad recomputes its `gr_text`'s string and, unless `text_position_mode` is 2, its `at`, on every save.",
)

CELL = Schema(
    "table_cell",
    "cell",
    (
        Head(A("text", "str")),
        LOCKED,
        Leaf("start", (A("start", "xy", omit=None, default=None),)),
        Leaf("end", (A("end", "xy", omit=None, default=None),)),
        Leaf("pts", (A("pts", "pts", omit=None, default=None),)),
        Leaf("angle", (A("angle", "real", omit=None, default=None),)),
        Leaf("margins", (A("margins", "lens", omit=None, default=None),)),
        Leaf("span", (A("span", "ints", omit=None, default=None),)),
        Leaf("layer", (A("layer", "str"),)),
        UUID,
        _effects(),
        Cache("render_cache"),
    ),
    ("text",),
    "One `table_cell`, a dict in `Table(cells=[...])`.",
)

def _table_stroke() -> Node:
    """A table's border or separator `(stroke (width) (type) (color))`: KiCad writes none at all when
    the border (`external` and `header`) or the separators (`rows` and `cols`) are both off (measured
    with `kicad-cli pcb upgrade`, third review P4), so `width` and `type` are None then and nothing is
    written; a table with a visible border carries both."""
    return Node(
        "stroke",
        (
            Leaf("width", (A("width", "len", omit=None, default=None),)),
            Leaf("type", (A("type", "sym", omit=None, default=None, doc="solid|dash|dot|dash_dot|dash_dot_dot|default", words=WORDS["stroke_type"]),)),
            Leaf("color", (A("color", "reals", omit=None, default=None, doc="r g b a"),)),
        ),
    )


TABLE = Schema(
    "table",
    "Table",
    (
        Leaf("column_count", (A("column_count", "int"),)),
        UUID,
        LOCKED,
        Leaf("layer", (A("layer", "str", default="F.SilkS"),)),
        DictNode("border", "border", (Leaf("external", (A("external", "yn"),)), Leaf("header", (A("header", "yn"),)), _table_stroke())),
        DictNode("separators", "separators", (Leaf("rows", (A("rows", "yn"),)), Leaf("cols", (A("cols", "yn"),)), _table_stroke())),
        Leaf("column_widths", (A("column_widths", "lens"),)),
        Leaf("row_heights", (A("row_heights", "lens"),)),
        Node("cells", (Many("table_cell", "cells", CELL),)),
    ),
    ("column_count",),
    "A KiCad `table`; `cells` is one dict per `table_cell`, row by row.",
)

SCHEMAS: dict[str, Schema] = {
    s.head: s
    for s in (
        _shape("gr_line", "Line", (Leaf("start", (A("start", "xy"),)), Leaf("end", (A("end", "xy"),))), ("start", "end"), fill=False, doc="A KiCad `gr_line`.", width=0.05, layer="Edge.Cuts"),
        _shape(
            "gr_rect",
            "Rect",
            (Leaf("start", (A("start", "xy"),)), Leaf("end", (A("end", "xy"),)), Leaf("radius", (A("radius", "len", omit=None, default=None, doc="corner radius"),))),
            ("start", "end"),
            fill=True,
            doc="A KiCad `gr_rect`.",
        ),
        _shape("gr_circle", "Circle", (Leaf("center", (A("center", "xy"),)), Leaf("end", (A("end", "xy", doc="a point on the circle"),))), ("center",), fill=True, doc="A KiCad `gr_circle`."),
        _shape("gr_arc", "DrawArc", (Leaf("start", (A("start", "xy"),)), Leaf("mid", (A("mid", "xy"),)), Leaf("end", (A("end", "xy"),))), ("start", "mid", "end"), fill=False, doc="A KiCad `gr_arc`.", width=0.05, layer="Edge.Cuts"),
        _shape("gr_poly", "Poly", (Leaf("pts", (A("pts", "pts"),)),), ("pts",), fill=True, doc="A KiCad `gr_poly`."),
        _shape("gr_curve", "Curve", (Leaf("pts", (A("pts", "pts", doc="start, control 1, control 2, end"),)),), ("pts",), fill=False, doc="A KiCad `gr_curve` (a cubic Bezier)."),
        TEXT,
        TEXTBOX,
        DIMENSION,
        Schema(
            "group",
            "Group",
            (Head(A("name", "str")), UUID, LOCKED, Leaf("lib_id", (A("lib_id", "str", omit=None, default=None),)), Leaf("members", (A("members", "refs", default=REQ, doc="ids of other layout objects"),))),
            ("name", "members"),
            "A KiCad `group`. `members` are the ids of layout objects; KiCad writes their uuids sorted.",
        ),
        Schema(
            "image",
            "Image",
            (Leaf("at", (A("at", "xy"),)), Leaf("layer", (A("layer", "str", default="Dwgs.User"),)), Leaf("scale", (A("scale", "real", omit=None, default=None),)), LOCKED, Leaf("data", (A("data", "blob", doc="the file, base64"),)), UUID),
            ("at", "data"),
            "A KiCad `image` (reference image).",
        ),
        TABLE,
        Schema(
            "barcode",
            "Barcode",
            (
                LOCKED,
                Leaf("at", (A("at", "xy"), A("angle", "real", default=0.0))),
                Leaf("layer", (A("layer", "str", default="F.SilkS"),)),
                Leaf("size", (A("size", "lens", default=(5.0, 5.0), doc="width height"),)),
                Leaf("text", (A("text", "str"),)),
                Leaf("text_height", (A("text_height", "len", default=1.0),)),
                Leaf("type", (A("kind", "sym", default="qr", doc="qr|microqr|code39|code128|datamatrix", words=WORDS["barcode.kind"]),)),
                Leaf("ecc_level", (A("ecc_level", "sym", omit=None, default=None, words=WORDS["barcode.ecc_level"]),)),
                Leaf("hide", (A("hide", "yn", default=False),)),
                Leaf("knockout", (A("knockout", "yn", default=False),)),
                Leaf("margins", (A("margins", "lens", omit=None, default=None),)),
                UUID,
            ),
            ("text", "at"),
            "A KiCad `barcode`.",
        ),
        Schema(
            "target",
            "Target",
            (Head(A("shape", "sym", default="plus", doc="plus|x", words=WORDS["target.shape"])), Leaf("at", (A("at", "xy"),)), Leaf("size", (A("size", "len", default=5.0),)), Leaf("width", (A("width", "len", default=0.15),)), Leaf("layer", (A("layer", "str", default="Dwgs.User"),)), UUID),
            ("at",),
            "A KiCad `target` (an alignment mark).",
        ),
        Schema(
            "point",
            "Point",
            (Leaf("at", (A("at", "xy"),)), Leaf("size", (A("size", "len", default=1.27),)), Leaf("layer", (A("layer", "str", default="Dwgs.User"),)), UUID),
            ("at",),
            "A KiCad `point`.",
        ),
        Schema(
            "generated",
            "Generated",
            (UUID, Leaf("type", (A("type", "sym", words=WORDS["generated.type"]),)), Leaf("name", (A("name", "str"),)), Leaf("layer", (A("layer", "str", default="F.Cu"),)), Props("props"), Leaf("members", (A("members", "refs"),))),
            ("type", "name", "members"),
            "A KiCad `generated` object (a tuning pattern); `props` are the generator's own `(key value)`s.",
        ),
    )
}
BY_NAME: dict[str, Schema] = {s.name: s for s in SCHEMAS.values()}
HEADS = tuple(SCHEMAS)


# ----------------------------------------------------------------------------------- reading


def _num(a, where: str) -> float:
    if isinstance(a, Q):
        raise Unmapped(f"{where}: a number is quoted: {a!r}")
    try:
        return float(a)
    except ValueError as exc:
        raise Unmapped(f"{where}: {a!r} is not a number") from exc


def _atoms_of(node: list) -> list:
    return [x for x in node[1:] if not isinstance(x, list)]


def _decode(atom: Atom, vals: list, where: str):
    """Take `atom`'s value off the front of `vals` (mutated). Missing -> `omit`."""
    k = atom.kind
    if k == "flag":
        if atom.flag in vals and not isinstance(vals[vals.index(atom.flag)], Q):
            vals.remove(atom.flag)
            return True
        return False
    if k in ("atoms", "strs", "lens", "ints", "reals", "refs", "blob"):
        out = []
        while vals:
            v = vals.pop(0)
            if k == "atoms":
                if isinstance(v, Q):
                    raise Unmapped(f"{where}: quoted {v!r}")
                out.append(str(v))
            elif k in ("strs", "refs", "blob"):
                if not isinstance(v, Q):
                    raise Unmapped(f"{where}: bare {v!r} where KiCad writes a string")
                out.append(str(v))
            elif k == "ints":
                out.append(int(_num(v, where)))
            else:
                out.append(_num(v, where))
        if k == "blob":
            return "".join(out)
        return tuple(out)
    if not vals:
        return _missing(atom, where)
    if k == "xy":
        if len(vals) < 2:
            raise Unmapped(f"{where}: ({atom.kw}) needs x y")
        return (_num(vals.pop(0), where), _num(vals.pop(0), where))
    v = vals.pop(0)
    if k == "len" or k == "real":
        return _num(v, where)
    if k == "int":
        return int(_num(v, where))
    if k == "str":
        if not isinstance(v, Q):
            raise Unmapped(f"{where}: bare {v!r} where KiCad writes a string")
        return str(v)
    if k == "sym":
        if isinstance(v, Q):
            raise Unmapped(f"{where}: quoted {v!r} where KiCad writes a word")
        return str(v)
    if k == "yn":
        if v not in ("yes", "no") or isinstance(v, Q):
            raise Unmapped(f"{where}: {v!r} is not yes/no")
        return v == "yes"
    if k == "fill":
        return {"yes": True, "no": False}.get(str(v), str(v))
    raise AssertionError(k)


def _missing(atom: Atom, where: str):
    if atom.omit is REQ:
        raise Unmapped(f"{where}: KiCad always writes ({atom.kw}) and this one has none")
    return atom.omit


def _missing_slot(slot, out: dict, where: str) -> None:
    if isinstance(slot, Leaf):
        for a in slot.atoms:
            out[a.kw] = _missing(a, f"{where} ({slot.tok})")
    elif isinstance(slot, Node):
        for s in slot.slots:
            _missing_slot(s, out, f"{where} ({slot.tok})")
    elif isinstance(slot, (DictNode, Sub)):
        if slot.omit is REQ:
            raise Unmapped(f"{where}: no ({slot.tok})")
        out[slot.kw] = slot.omit
    elif isinstance(slot, Many):
        out[slot.kw] = ()
    elif isinstance(slot, Head):
        out[slot.atom.kw] = _missing(slot.atom, where)


def _kids(node: list) -> dict[str, list[list]]:
    kids: dict[str, list[list]] = {}
    for x in node[1:]:
        if isinstance(x, list):
            if not x or isinstance(x[0], list):
                raise Unmapped(f"({node[0]}): an unnamed child {x!r}")
            kids.setdefault(str(x[0]), []).append(x)
    return kids


def _read_slots(slots: tuple, node: list, out: dict, where: str, *, head_atoms: list | None = None) -> None:
    kids = _kids(node)
    atoms = head_atoms if head_atoms is not None else _atoms_of(node)
    props: Props | None = None
    for s in slots:
        if isinstance(s, Head):
            out[s.atom.kw] = _decode(s.atom, atoms, where)
        elif isinstance(s, Cache):
            kids.pop(s.tok, None)
        elif isinstance(s, Props):
            props = s
        elif isinstance(s, Many):
            out[s.kw] = tuple(read_node(s.schema, x, where=f"{where} ({s.tok})") for x in kids.pop(s.tok, []))
        else:
            got = kids.pop(s.tok, None)
            if got is None:
                _missing_slot(s, out, where)
                continue
            if len(got) > 1:
                raise Unmapped(f"{where}: ({s.tok}) appears {len(got)} times")
            n = got[0]
            w = f"{where} ({s.tok})"
            if isinstance(s, Leaf):
                if any(isinstance(x, list) for x in n[1:]) and not any(a.kind == "pts" for a in s.atoms):
                    raise Unmapped(f"{w}: holds a child {n!r}")
                if any(a.kind == "pts" for a in s.atoms):
                    out[s.atoms[0].kw] = _pts(n, w)
                    continue
                vals = list(n[1:])
                for a in s.atoms:
                    out[a.kw] = _decode(a, vals, w)
                if vals:
                    raise Unmapped(f"{w}: KiCad wrote {vals!r} and no field carries it")
            elif isinstance(s, Node):
                if _atoms_of(n):
                    raise Unmapped(f"{w}: bare atoms {_atoms_of(n)!r}")
                _read_slots(s.slots, n, out, w)
            elif isinstance(s, DictNode):
                if _atoms_of(n):
                    raise Unmapped(f"{w}: bare atoms {_atoms_of(n)!r}")
                d: dict = {}
                _read_slots(s.slots, n, d, w)
                out[s.kw] = d
            elif isinstance(s, Sub):
                out[s.kw] = read_node(s.schema, n, where=w)
    if props is not None:
        bag: dict = {}
        for key in [k for k in list(kids) if k != "members"]:
            for n in kids.pop(key):
                bag[key] = _prop_value(n, f"{where} ({key})")
        out[props.kw] = bag
    if atoms:
        raise Unmapped(f"{where}: bare atoms {atoms!r} and no field carries them")
    if kids:
        raise Unmapped(f"{where}: KiCad wrote {sorted(kids)} and no Python field carries it")


def is_arc(entry) -> bool:
    """A `pts` entry that is an `(arc (start) (mid) (end))`: a triple of points, not a point."""
    return isinstance(entry, (tuple, list)) and len(entry) == 3 and all(isinstance(p, (tuple, list)) and len(p) == 2 for p in entry)


def flat_points(entries) -> tuple[tuple[float, float], ...]:
    """Every point of a `pts` list as a plain vertex: an arc entry contributes its start, mid and end
    (for a box, a point-in-polygon or a bounding box; the arc itself is KiCad's)."""
    out: list[tuple[float, float]] = []
    for e in entries:
        if is_arc(e):
            out += [(float(e[0][0]), float(e[0][1])), (float(e[1][0]), float(e[1][1])), (float(e[2][0]), float(e[2][1]))]
        else:
            out.append((float(e[0]), float(e[1])))
    return tuple(out)


def read_pts(n: list, where: str) -> tuple:
    """A `(pts ...)` node: each child is `(xy x y)`, read as `(x, y)`, or `(arc (start x y) (mid x y)
    (end x y))`, read as `((sx, sy), (mx, my), (ex, ey))` — the two entries KiCad 10 writes into a
    polygon's points (a zone outline, a `gr_poly`). Anything else is `Unmapped`."""
    out = []
    for x in n[1:]:
        if isinstance(x, list) and x[0] == "xy" and len(x) == 3:
            out.append((_num(x[1], where), _num(x[2], where)))
        elif isinstance(x, list) and x[0] == "arc":
            w = f"{where} (arc)"
            got: dict = {}
            for y in x[1:]:
                if not isinstance(y, list) or y[0] not in ("start", "mid", "end") or len(y) != 3 or y[0] in got:
                    raise Unmapped(f"{w}: holds {y!r}")
                got[y[0]] = (_num(y[1], w), _num(y[2], w))
            if set(got) != {"start", "mid", "end"}:
                raise Unmapped(f"{w}: KiCad writes (start) (mid) (end) and this one has {sorted(got)}")
            out.append((got["start"], got["mid"], got["end"]))
        else:
            raise Unmapped(f"{where}: (pts ...) holds {x!r}")
    return tuple(out)


def pts_sexp(entries) -> str:
    """`(pts ...)` as KiCad writes it: `(xy x y)` per point, `(arc (start x y) (mid x y) (end x y))` per arc."""
    parts = []
    for e in entries:
        if is_arc(e):
            (sx, sy), (mx, my), (ex, ey) = e
            parts.append(f"(arc (start {fmt_num(sx)} {fmt_num(sy)}) (mid {fmt_num(mx)} {fmt_num(my)}) (end {fmt_num(ex)} {fmt_num(ey)}))")
        else:
            parts.append(f"(xy {fmt_num(e[0])} {fmt_num(e[1])})")
    return "(pts " + " ".join(parts) + ")"


def _pts(n: list, where: str) -> tuple:
    return read_pts(n, where)


def _prop_value(n: list, where: str):
    if len(n) == 2 and not isinstance(n[1], list):
        v = n[1]
        if isinstance(v, Q):
            return str(v)
        if v in ("yes", "no"):
            return v == "yes"
        return _num(v, where)
    if len(n) == 2 and isinstance(n[1], list) and n[1][0] == "xy":
        return (_num(n[1][1], where), _num(n[1][2], where))
    if len(n) == 2 and isinstance(n[1], list) and n[1][0] == "pts":
        # A tuple, as `language._norm` makes a hand-written or loaded point list: the same value on
        # both sides of the round trip (third review P3: `Generated` loaded back "differs in props").
        return tuple(_pts(n[1], where))
    raise Unmapped(f"{where}: {n!r} is not a number, string, yes/no, (xy) or (pts)")


def read_node(schema: Schema, node: list, *, where: str = "") -> dict:
    """One KiCad node as `{keyword: value}`, every field present. Raises `Unmapped` on any token the
    schema has no field for, and on any field KiCad always writes that is missing."""
    out: dict = {}
    w = where or f"({schema.head})"
    _read_slots(schema.slots, node, out, w)
    return out


# ----------------------------------------------------------------------------------- writing


def _enc(atom: Atom, v) -> list[str]:
    k = atom.kind
    if k == "flag":
        return [atom.flag] if v else []
    if k == "xy":
        return [fmt_num(v[0]), fmt_num(v[1])]
    if k == "len":
        return [fmt_num(v)]
    if k == "real":
        return [fmt_exact(v)]
    if k == "int":
        return [str(int(v))]
    if k == "str":
        return [quote(v)]
    if k == "sym":
        return [str(v)]
    if k == "yn":
        return ["yes" if v else "no"]
    if k == "fill":
        return ["yes" if v is True else "no" if v is False else str(v)]
    if k == "atoms":
        return [str(x) for x in v]
    if k in ("strs", "refs"):
        return [quote(x) for x in v]
    if k == "lens":
        return [fmt_num(x) for x in v]
    if k == "reals":
        return [fmt_exact(x) for x in v]
    if k == "ints":
        return [str(int(x)) for x in v]
    if k == "blob":
        raw = "".join(str(v).split())
        return [quote(raw[i : i + 76]) for i in range(0, len(raw), 76)] or ['""']
    if k == "pts":
        return [pts_sexp(v)]
    raise AssertionError(k)


def _written(atom: Atom, v) -> bool:
    return atom.omit is REQ or v != atom.omit


def _write_slots(slots: tuple, vals: dict, where: str) -> list[str]:
    out: list[str] = []
    for s in slots:
        if isinstance(s, (Head, Cache)):
            continue
        if isinstance(s, Leaf):
            parts: list[str] = []
            any_written = False
            for i, a in enumerate(s.atoms):
                v = vals[a.kw]
                if not _written(a, v):
                    if i == 0:
                        break
                    continue
                if v is None:
                    raise ValueError(f"{where}: {a.kw} is required")
                parts += _enc(a, v)
                any_written = True
            if any_written:
                if s.atoms[0].kind == "pts":
                    out.append(f"({s.tok} " + pts_sexp(vals[s.atoms[0].kw])[len("(pts "):])
                else:
                    out.append(f"({s.tok}" + "".join(" " + p for p in parts) + ")")
        elif isinstance(s, Node):
            inner = _write_slots(s.slots, vals, f"{where} ({s.tok})")
            if inner:
                out.append(f"({s.tok} " + " ".join(inner) + ")")
        elif isinstance(s, DictNode):
            d = vals[s.kw]
            if d is None or d == s.omit:
                continue
            unknown = set(d) - {kw_of(f) for f in _fields(s.slots)}
            if unknown:
                raise ValueError(f"{where}: {s.kw} has no key {sorted(unknown)}; KiCad's are {[kw_of(f) for f in _fields(s.slots)]}")
            full = {kw_of(f): d.get(kw_of(f), omit_of(f) if omit_of(f) is not REQ else None) for f in _fields(s.slots)}
            inner = _write_slots(s.slots, full, f"{where} ({s.tok})")
            out.append(f"({s.tok} " + " ".join(inner) + ")")
        elif isinstance(s, Sub):
            d = vals[s.kw]
            if d is None:
                continue
            out.append(write_node(s.schema, complete(s.schema, d), inline=True))
        elif isinstance(s, Many):
            out += [write_node(s.schema, complete(s.schema, d), inline=True) for d in vals[s.kw]]
        elif isinstance(s, Props):
            for k, v in vals[s.kw].items():
                out.append(f"({k} {_prop_text(v)})")
    return out


def _prop_text(v) -> str:
    if isinstance(v, bool):
        return "yes" if v else "no"
    if isinstance(v, (int, float)):
        return fmt_exact(v)
    if isinstance(v, str):
        return quote(v)
    if isinstance(v, tuple) and len(v) == 2 and all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in v):
        return f"(xy {fmt_num(v[0])} {fmt_num(v[1])})"
    if isinstance(v, (list, tuple)) and v and all(isinstance(p, (list, tuple)) for p in v):
        return pts_sexp(v)
    raise ValueError(f"generated property {v!r} is not a number, string, yes/no, (x, y) or a list of points")


def _head_atoms(schema: Schema, vals: dict) -> str:
    heads = [s.atom for s in schema.slots if isinstance(s, Head)]
    return "".join(" " + " ".join(_enc(a, vals[a.kw])) for a in heads if _written(a, vals[a.kw]))


def write_node(schema: Schema, vals: dict, *, inline: bool = False) -> str:
    """One primitive, every field in `vals` (see `complete`), in the order KiCad 10 writes them."""
    lines = _write_slots(schema.slots, vals, f"({schema.head})")
    if inline:
        return f"({schema.head}{_head_atoms(schema, vals)} " + " ".join(lines) + ")"
    return f"\t({schema.head}{_head_atoms(schema, vals)}\n" + "".join(f"\t\t{x}\n" for x in lines) + "\t)\n"


def complete(schema: Schema, given: dict) -> dict:
    """`given` with every field the schema has: a field left out takes its hand-writing default."""
    out: dict = {}
    for f in schema.fields():
        kw = kw_of(f)
        if kw in given:
            out[kw] = given[kw]
            continue
        d = default_of(f)
        if d is REQ:
            raise ValueError(f"{schema.name}: {kw} is required")
        out[kw] = d
    unknown = set(given) - set(out)
    if unknown:
        raise ValueError(f"{schema.name}: no field {sorted(unknown)}")
    return out


# ----------------------------------------------------------------------------------- the census map


def census_map() -> dict[str, dict[str, str]]:
    """Every token path under every drawing, with the Python field it lands in: `gen.CENSUS`'s rows for
    the drawings, and the docs table. A nested field is written `format.prefix`, `cells[i].text`."""
    out: dict[str, dict[str, str]] = {}

    def walk(slots, pre: str, bag: dict, owner: str) -> None:
        for s in slots:
            if isinstance(s, Head):
                bag[f"{pre}(atom)"] = f"{owner}{s.atom.kw} (positional)"
            elif isinstance(s, Leaf):
                path = f"{pre}{s.tok}"
                if s.atoms[0].kind == "pts":
                    bag[path] = f"{owner}{s.atoms[0].kw}"
                    bag[f"{path}/xy"] = f"{owner}{s.atoms[0].kw}[i] (a point)"
                    bag[f"{path}/arc"] = f"{owner}{s.atoms[0].kw}[i] (an arc entry: ((sx, sy), (mx, my), (ex, ey)))"
                    for k in ("start", "mid", "end"):
                        bag[f"{path}/arc/{k}"] = f"{owner}{s.atoms[0].kw}[i][{('start', 'mid', 'end').index(k)}]"
                else:
                    bag[path] = " + ".join(f"{owner}{a.kw}" for a in s.atoms)
            elif isinstance(s, Node):
                bag[f"{pre}{s.tok}"] = "holds " + ", ".join(f"{owner}{kw_of(f)}" for f in _fields(s.slots))
                walk(s.slots, f"{pre}{s.tok}/", bag, owner)
            elif isinstance(s, DictNode):
                bag[f"{pre}{s.tok}"] = f"{owner}{s.kw}"
                walk(s.slots, f"{pre}{s.tok}/", bag, f"{owner}{s.kw}.")
            elif isinstance(s, Sub):
                bag[f"{pre}{s.tok}"] = f"{owner}{s.kw}"
                walk(s.schema.slots, f"{pre}{s.tok}/", bag, f"{owner}{s.kw}.")
            elif isinstance(s, Many):
                bag[f"{pre}{s.tok}"] = f"{owner}{s.kw}[i]"
                walk(s.schema.slots, f"{pre}{s.tok}/", bag, f"{owner}{s.kw}[i].")
            elif isinstance(s, Cache):
                bag[f"{pre}{s.tok}"] = "not a field: KiCad's derived cache, regenerated on save"
            elif isinstance(s, Props):
                bag[f"{pre}*"] = f"{owner}{s.kw}[key] (the generator's own properties)"

    for head, schema in SCHEMAS.items():
        bag: dict[str, str] = {}
        walk(schema.slots, "", bag, "")
        out[head] = dict(sorted(bag.items()))
    return out


def own_uuids(schema: Schema, vals: dict) -> list[str]:
    """The uuids that name distinct objects of `vals`: its own and its `Many` children's (a table's
    cells), not a `Sub`'s (a dimension's `gr_text` carries the dimension's own uuid, by KiCad's rule).
    What `gen.decompile`'s uniqueness check counts."""
    got = [vals["uuid"]] if vals.get("uuid") else []
    for s in _fields(schema.slots):
        if isinstance(s, Many):
            for d in vals.get(s.kw) or ():
                got += [u for u in own_uuids(s.schema, d) if u]
    return got


def all_uuids(schema: Schema, vals: dict) -> list[str]:
    """Every uuid the object carries, its nested cells' and text's included."""
    got = [vals["uuid"]] if vals.get("uuid") else []
    for s in _fields(schema.slots):
        if isinstance(s, Sub) and vals.get(s.kw):
            got += [u for u in all_uuids(s.schema, vals[s.kw]) if u]
        elif isinstance(s, Many):
            for d in vals.get(s.kw) or ():
                got += [u for u in all_uuids(s.schema, d) if u]
    return got
