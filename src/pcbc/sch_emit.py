"""Emit a KiCad 10 .kicad_sch from a pcbc Design. No default.net."""

from __future__ import annotations

import math
import re
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from .footprints import symbol_path
from .model import Design
from .sch_place import (
    FLIP,
    body_aabb,
    core_aabb,
    hat_zone,
    lib_to_sheet,
    pin_outward,
    pin_world,
    text_zone,
    world_aabb,
)
from .sch_prims import Header, SheetField, SheetJunction, SheetLabel, SheetNoConnect, SheetRect, SheetSymbol, SheetText, SheetWire, sheet_text
from .sexp import new_uuid, stable_uuid
from .symbol import extract_main_symbol, parse_symbol_layout, parse_symbol_pins_geom


def is_power_net(name: str, kinds: dict[str, str] | None = None) -> bool:
    """True only if the board declared Power() or Ground() for this net."""
    return (kinds or {}).get(name, "") in ("power", "ground")


def is_ground_net(name: str, kinds: dict[str, str] | None = None) -> bool:
    return (kinds or {}).get(name, "") == "ground"

_FONT = 1.27
_CHAR_W = 0.95
_PIN_LEN = 3.81
_PITCH = 2.54
_HAT = 5.08
_PROP = re.compile(r'\(property \(name "([^"]+)"\) \(value "([^"]*)"\)')


def _fmt(n: float) -> str:
    s = f"{n:.4f}".rstrip("0").rstrip(".")
    return s if s else "0"


def _text_w(name: str) -> float:
    """A generous width for `name` at 1.27 mm: the room labels, power-symbol names and pin numbers are given."""
    return max(len(name), 1) * _FONT * _CHAR_W


def _field_w(text: str) -> float:
    """How wide KiCad draws a Reference or Value at 1.27 mm (`sch_font`, measured on KiCad's own SVG)."""
    from .sch_font import text_width

    return text_width(text, _FONT)


@dataclass
class PinDef:
    number: str
    name: str
    lx: float
    ly: float
    rot: float  # toward body
    net: str
    length: float = 2.54


@dataclass
class Part:
    ref: str
    lib_id: str
    display: str
    kind: str  # r, c, l, box
    pins: list[PinDef]
    x: float = 0.0
    y: float = 0.0
    rot: float = 0.0
    mirror: str | None = None
    hw: float = 8.0
    hh: float = 6.0
    bbox: tuple[float, float, float, float] = (-8.0, -6.0, 8.0, 6.0)
    body_bbox: tuple[float, float, float, float] = (-8.0, -6.0, 8.0, 6.0)
    core_bbox: tuple[float, float, float, float] = (-6.0, -4.0, 6.0, 4.0)
    lib_sexp: str | None = None
    placed: bool = False
    prop_ref: tuple[float, float] = (0.0, 0.0)
    prop_val: tuple[float, float] = (0.0, 0.0)
    text_ref: tuple[float, float, str | None, int] | None = None
    text_val: tuple[float, float, str | None, int] | None = None


def _kind(ref: str) -> str:
    if ref[:1] == "R":
        return "r"
    if ref[:1] == "C":
        return "c"
    if ref[:1] == "L":
        return "l"
    return "box"


def _passive_pins(kind: str, pin_nets: dict[str, str]) -> list[PinDef]:
    # KiCad Device convention: pin 1 at library +Y (top of the sheet), pin 2
    # below. Coordinates are the electrical ends, not the body edge.
    n1 = pin_nets.get("1", "")
    n2 = pin_nets.get("2", pin_nets.get("1", ""))
    end = 3.81 if kind in ("r", "l") else 2.54
    return [
        PinDef("1", "1", 0.0, end, 270, n1),
        PinDef("2", "2", 0.0, -end, 90, n2),
    ]


@dataclass
class PkgLib:
    key: str
    pad_to_name: dict[str, str]
    lib_id: str | None
    pins: list[PinDef]
    sexp: str | None


def load_packages(root: Path | None) -> list[PkgLib]:
    return []


def load_component_pin_maps(root: Path | None) -> list[tuple[str, dict[str, str]]]:
    return [(p.key, p.pad_to_name) for p in load_packages(root)]


def _pin_map_for(comp: dict, maps: list[tuple[str, dict[str, str]]]) -> dict[str, str]:
    blob = " ".join(str(comp.get(k) or "") for k in ("footprint", "value", "libpart", "display"))
    blob_l = blob.lower()
    for key, pad_map in maps:
        if key.lower() in blob_l:
            return pad_map
    return {}


def _pkg_for(comp: dict, pkgs: list[PkgLib]) -> PkgLib | None:
    blob = " ".join(str(comp.get(k) or "") for k in ("footprint", "value", "libpart", "display"))
    blob_l = blob.lower()
    for pkg in pkgs:
        if pkg.key.lower() in blob_l:
            return pkg
    return None


def _fallback_pin_name(pad: str, net: str) -> str:
    if pad and not pad.isdigit():
        return pad
    if is_power_net(net):
        u = net.upper()
        if "GND" in u or u == "VSS":
            return "GND"
        if "PGND" in u:
            return "PGND"
        if net.startswith("+") or "VCC" in u or "VDD" in u or "VM" in u:
            return net if len(net) <= 6 else "VCC"
        return net[:8]
    return "~"


def _logical_pins(
    pin_nets: dict[str, str], pad_to_name: dict[str, str]
) -> list[tuple[str, str, str]]:
    """(name, number, net) — merge datasheet-same pads (OUT1 on 17/18/19)."""
    used: set[str] = set()
    out: list[tuple[str, str, str]] = []
    name_order: list[str] = []
    pads_of: dict[str, list[str]] = defaultdict(list)
    for pad, name in pad_to_name.items():
        if name not in pads_of:
            name_order.append(name)
        pads_of[name].append(pad)
    for name in name_order:
        pads = [p for p in pads_of[name] if p in pin_nets]
        if not pads:
            continue
        out.append((name, pads[0], pin_nets[pads[0]]))
        used.update(pads)
    extra: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for pad, net in sorted(pin_nets.items(), key=lambda t: (len(t[0]), t[0])):
        if pad in used:
            continue
        name = _fallback_pin_name(pad, net)
        if name == "~":
            out.append(("~", pad, net))
        else:
            extra[name].append((pad, net))
    for name, items in extra.items():
        items.sort(key=lambda t: (len(t[0]), t[0]))
        out.append((name, items[0][0], items[0][1]))
    return out


def _box_pins(
    pin_nets: dict[str, str], pad_to_name: dict[str, str] | None = None
) -> list[PinDef]:
    logical = _logical_pins(pin_nets, pad_to_name or {})
    n = max(len(logical), 1)
    left_n = (n + 1) // 2
    left, right = logical[:left_n], logical[left_n:]
    rows = max(len(left), len(right), 1)
    half_h = rows * _PITCH / 2
    longest = max((len(name) for name, _num, _net in logical), default=1)
    half_w = max(10.16, longest * _FONT * 0.55 + 3.0)
    pins: list[PinDef] = []
    for i, (name, num, net) in enumerate(left):
        y = half_h - _PITCH / 2 - i * _PITCH
        pins.append(PinDef(num, name, -(half_w + _PIN_LEN), y, 0, net))
    for i, (name, num, net) in enumerate(right):
        y = half_h - _PITCH / 2 - i * _PITCH
        pins.append(PinDef(num, name, half_w + _PIN_LEN, y, 180, net))
    return pins


def _lib_r() -> str:
    return """		(symbol "R"
			(pin_numbers (hide yes))
			(pin_names (hide yes))
			(exclude_from_sim no)
			(in_bom yes)
			(on_board yes)
			(property "Reference" "R"
				(at 2.032 0 90)
				(effects (font (size 1.27 1.27)))
			)
			(property "Value" "R"
				(at -2.032 0 90)
				(effects (font (size 1.27 1.27)))
			)
			(symbol "R_0_1"
				(rectangle
					(start -1.016 -2.54)
					(end 1.016 2.54)
					(stroke (width 0.2032) (type default))
					(fill (type none))
				)
			)
			(symbol "R_1_1"
				(pin passive line
					(at 0 3.81 270)
					(length 1.27)
					(name "1" (effects (font (size 1.27 1.27))))
					(number "1" (effects (font (size 1.27 1.27))))
				)
				(pin passive line
					(at 0 -3.81 90)
					(length 1.27)
					(name "2" (effects (font (size 1.27 1.27))))
					(number "2" (effects (font (size 1.27 1.27))))
				)
			)
			(embedded_fonts no)
		)
"""


def _lib_c() -> str:
    return """		(symbol "C"
			(pin_numbers (hide yes))
			(pin_names (hide yes))
			(exclude_from_sim no)
			(in_bom yes)
			(on_board yes)
			(property "Reference" "C"
				(at 2.54 0 90)
				(effects (font (size 1.27 1.27)))
			)
			(property "Value" "C"
				(at -2.54 0 90)
				(effects (font (size 1.27 1.27)))
			)
			(symbol "C_0_1"
				(polyline
					(pts (xy -1.524 -0.508) (xy 1.524 -0.508))
					(stroke (width 0.254) (type default))
					(fill (type none))
				)
				(polyline
					(pts (xy -1.524 0.508) (xy 1.524 0.508))
					(stroke (width 0.254) (type default))
					(fill (type none))
				)
			)
			(symbol "C_1_1"
				(pin passive line
					(at 0 2.54 270)
					(length 2.032)
					(name "1" (effects (font (size 1.27 1.27))))
					(number "1" (effects (font (size 1.27 1.27))))
				)
				(pin passive line
					(at 0 -2.54 90)
					(length 2.032)
					(name "2" (effects (font (size 1.27 1.27))))
					(number "2" (effects (font (size 1.27 1.27))))
				)
			)
			(embedded_fonts no)
		)
"""


def _lib_l() -> str:
    return """		(symbol "L"
			(pin_numbers (hide yes))
			(pin_names (hide yes))
			(exclude_from_sim no)
			(in_bom yes)
			(on_board yes)
			(property "Reference" "L"
				(at 2.54 0 90)
				(effects (font (size 1.27 1.27)))
			)
			(property "Value" "L"
				(at -2.54 0 90)
				(effects (font (size 1.27 1.27)))
			)
			(symbol "L_0_1"
				(arc (start 0 -2.032) (mid 0.889 -1.016) (end 0 0)
					(stroke (width 0) (type default)) (fill (type none)))
				(arc (start 0 0) (mid 0.889 1.016) (end 0 2.032)
					(stroke (width 0) (type default)) (fill (type none)))
			)
			(symbol "L_1_1"
				(pin passive line
					(at 0 3.81 270)
					(length 1.778)
					(name "1" (effects (font (size 1.27 1.27))))
					(number "1" (effects (font (size 1.27 1.27))))
				)
				(pin passive line
					(at 0 -3.81 90)
					(length 1.778)
					(name "2" (effects (font (size 1.27 1.27))))
					(number "2" (effects (font (size 1.27 1.27))))
				)
			)
			(embedded_fonts no)
		)
"""


def _lib_led() -> str:
    """Vertical LED: pin 2 A at library +Y (sheet top), pin 1 K below. Current flows down."""
    return """		(symbol "LED"
			(pin_numbers (hide yes))
			(pin_names (hide yes))
			(exclude_from_sim no)
			(in_bom yes)
			(on_board yes)
			(property "Reference" "D"
				(at 3.81 0 0)
				(effects (font (size 1.27 1.27)) (justify left))
			)
			(property "Value" "LED"
				(at 3.81 2.54 0)
				(effects (font (size 1.27 1.27)) (justify left))
			)
			(symbol "LED_0_1"
				(polyline (pts (xy -1.016 -0.762) (xy 1.016 -0.762))
					(stroke (width 0.254) (type default)) (fill (type none)))
				(polyline (pts (xy -1.016 0.762) (xy 1.016 0.762) (xy 0 -0.762) (xy -1.016 0.762))
					(stroke (width 0.254) (type default)) (fill (type none)))
				(polyline (pts (xy 0 -2.54) (xy 0 2.54))
					(stroke (width 0) (type default)) (fill (type none)))
				(polyline (pts (xy 1.27 -0.254) (xy 2.286 -1.016) (xy 1.778 -1.016) (xy 2.286 -1.016) (xy 2.286 -0.508))
					(stroke (width 0) (type default)) (fill (type none)))
				(polyline (pts (xy 1.778 0.508) (xy 2.794 -0.254) (xy 2.286 -0.254) (xy 2.794 -0.254) (xy 2.794 0.254))
					(stroke (width 0) (type default)) (fill (type none)))
			)
			(symbol "LED_1_1"
				(pin passive line
					(at 0 -2.54 90)
					(length 1.778)
					(name "K" (effects (font (size 1.27 1.27))))
					(number "1" (effects (font (size 1.27 1.27))))
				)
				(pin passive line
					(at 0 2.54 270)
					(length 1.778)
					(name "A" (effects (font (size 1.27 1.27))))
					(number "2" (effects (font (size 1.27 1.27))))
				)
			)
			(embedded_fonts no)
		)
"""


def _lib_gnd() -> str:
    # Cached the way eeschema writes power:GND — body _0_1 + pin _1_1 stay one symbol.
    name = "power:GND"
    etype, vis = "power_in", " hide"
    return f"""		(symbol "{name}" (power) (pin_names (offset 0)) (in_bom yes) (on_board yes)
			(property "Reference" "#PWR" (at 0 -6.35 0)
				(effects (font (size 1.27 1.27)) hide)
			)
			(property "Value" "GND" (at 0 -3.81 0)
				(effects (font (size 1.27 1.27)))
			)
			(symbol "{name.split(':')[1]}_0_1"
				(polyline
					(pts (xy 0 0) (xy 0 -1.27) (xy 1.27 -1.27) (xy 0 -2.54) (xy -1.27 -1.27) (xy 0 -1.27))
					(stroke (width 0) (type default))
					(fill (type none))
				)
			)
			(symbol "{name.split(':')[1]}_1_1"
				(pin {etype} line (at 0 0 270) (length 0){vis}
					(name "GND" (effects (font (size 1.27 1.27))))
					(number "1" (effects (font (size 1.27 1.27))))
				)
			)
		)
"""


def _lib_vcc() -> str:
    name = "power:VCC"
    etype, vis = "power_in", " hide"
    return f"""		(symbol "{name}" (power) (pin_names (offset 0)) (in_bom yes) (on_board yes)
			(property "Reference" "#PWR" (at 0 -3.81 0)
				(effects (font (size 1.27 1.27)) hide)
			)
			(property "Value" "VCC" (at 0 3.556 0)
				(effects (font (size 1.27 1.27)))
			)
			(symbol "{name.split(':')[1]}_0_1"
				(polyline
					(pts (xy -0.762 1.27) (xy 0 2.54))
					(stroke (width 0) (type default))
					(fill (type none))
				)
				(polyline
					(pts (xy 0 2.54) (xy 0.762 1.27))
					(stroke (width 0) (type default))
					(fill (type none))
				)
				(polyline
					(pts (xy 0 0) (xy 0 2.54))
					(stroke (width 0) (type default))
					(fill (type none))
				)
			)
			(symbol "{name.split(':')[1]}_1_1"
				(pin {etype} line (at 0 0 90) (length 0){vis}
					(name "VCC" (effects (font (size 1.27 1.27))))
					(number "1" (effects (font (size 1.27 1.27))))
				)
			)
		)
"""


def _lib_pwr_flag() -> str:
    # KiCad's power:PWR_FLAG: a power-output pin, visible and zero length, that
    # names no net and drives whatever it sits on.
    return """		(symbol "power:PWR_FLAG" (power) (pin_numbers (hide yes)) (pin_names (offset 0) (hide yes)) (in_bom yes) (on_board yes)
			(property "Reference" "#FLG" (at 0 1.905 0)
				(effects (font (size 1.27 1.27)) hide)
			)
			(property "Value" "PWR_FLAG" (at 0 3.81 0)
				(effects (font (size 1.27 1.27)))
			)
			(symbol "PWR_FLAG_0_1"
				(polyline
					(pts (xy 0 0) (xy 0 1.27) (xy -1.016 1.905) (xy 0 2.54) (xy 1.016 1.905) (xy 0 1.27))
					(stroke (width 0) (type default))
					(fill (type none))
				)
			)
			(symbol "PWR_FLAG_1_1"
				(pin power_out line (at 0 0 90) (length 0)
					(name "pwr" (effects (font (size 1.27 1.27))))
					(number "1" (effects (font (size 1.27 1.27))))
				)
			)
		)
"""


def _lib_box(lib_id: str, pins: list[PinDef], ref_prefix: str, kinds: dict[str, str] | None = None) -> str:
    xs = [p.lx for p in pins] or [0.0]
    ys = [p.ly for p in pins] or [0.0]
    # Body inside pin tips.
    half_w = 12.7
    half_h = max(abs(min(ys)), abs(max(ys)), _PITCH) + 0.0
    if pins:
        inward = [abs(p.lx) - _PIN_LEN for p in pins]
        if inward:
            half_w = max(inward)
    pin_sexps = []
    for p in pins:
        etype = "power_in" if is_power_net(p.net, kinds) else "unspecified"
        pin_sexps.append(
            f"""				(pin {etype} line
					(at {_fmt(p.lx)} {_fmt(p.ly)} {p.rot:g})
					(length {_PIN_LEN})
					(name "{p.name}" (effects (font (size 1.27 1.27))))
					(number "{p.number}" (effects (font (size 1.27 1.27))))
				)
"""
        )
    hide_nums = "yes"
    hide_names = "no"
    return f"""		(symbol "{lib_id}"
			(pin_names (offset 1.016) (hide {hide_names}))
			(pin_numbers (hide {hide_nums}))
			(exclude_from_sim no)
			(in_bom yes)
			(on_board yes)
			(property "Reference" "{ref_prefix}"
				(at 0 {_fmt(half_h + 2.54)} 0)
				(effects (font (size 1.27 1.27)))
			)
			(property "Value" "{lib_id}"
				(at 0 {_fmt(-half_h - 2.54)} 0)
				(effects (font (size 1.27 1.27)))
			)
			(symbol "{lib_id}_0_1"
				(rectangle
					(start {_fmt(-half_w)} {_fmt(half_h)})
					(end {_fmt(half_w)} {_fmt(-half_h)})
					(stroke (width 0.254) (type default))
					(fill (type background))
				)
			)
			(symbol "{lib_id}_1_1"
{''.join(pin_sexps)}			)
			(embedded_fonts no)
		)
"""


def _rotate(x: float, y: float, deg: float) -> tuple[float, float]:
    rad = math.radians(deg)
    c, s = math.cos(rad), math.sin(rad)
    return x * c - y * s, x * s + y * c


_GRID = 2.54


def _snap(v: float) -> float:
    return round(v / _GRID) * _GRID


def _pin_world(part: Part, pin: PinDef) -> tuple[float, float]:
    return pin_world(part, pin)


def _bbox(part: Part) -> tuple[float, float, float, float]:
    return world_aabb(part)


def _seg_hits_part(x0: float, y0: float, x1: float, y1: float, part: Part) -> bool:
    x_min, y_min, x_max, y_max = _bbox(part)
    # Ignore the endpoints (they sit on this part's pins).
    mx, my = (x0 + x1) / 2, (y0 + y1) / 2
    return x_min <= mx <= x_max and y_min <= my <= y_max


class _Ids:
    """Ids keyed by what they name, so the same board gives the same file byte
    for byte: no churn in git, and a report that does not move between runs."""

    def __init__(self, title: str):
        self.title = title
        self.seen: dict[str, int] = {}

    def __call__(self, key: str) -> str:
        # Keyed by what it names only: a placement drawn again in a later pass of the loop gets the id it had, so
        # the sheet does not depend on how many times the loop drew it (a lock copied from gen skips those draws).
        # Two objects with one key in the finished sheet are told apart by `_dedupe_ids`, in file order.
        return stable_uuid(self.title, key, 0)


_ids: _Ids | None = None


def _dedupe_ids(prims: list, title: str) -> list:
    """Every uuid the sheet carries once: the second object with an id (same key) gets the id of (that id, 1), and so on,
    in drawing order (a symbol's own id, then its pins')."""
    import dataclasses

    seen: dict[str, int] = {}

    def one(u: str) -> str:
        n = seen.get(u, 0)
        seen[u] = n + 1
        return u if n == 0 else stable_uuid(title, u, n)

    out = []
    for p in prims:
        if p.kind == "symbol":
            out.append(dataclasses.replace(p, uuid=one(p.uuid), pins=tuple((num, one(u)) for num, u in p.pins)))
        else:
            out.append(dataclasses.replace(p, uuid=one(p.uuid)))
    return out


def _uid(key: str) -> str:
    return _ids(key) if _ids is not None else new_uuid()


def _q4(v: float) -> float:
    """A coordinate as the file holds it (4 decimals): the primitive carries what KiCad reads back."""
    return float(_fmt(v))


def _wire(x0: float, y0: float, x1: float, y1: float) -> SheetWire:
    return SheetWire((_q4(x0), _q4(y0)), (_q4(x1), _q4(y1)), uuid=_uid(f"wire:{_fmt(x0)},{_fmt(y0)},{_fmt(x1)},{_fmt(y1)}"))


def _label(name: str, x: float, y: float, rot: int, justify: str, vjust: str = "bottom") -> SheetLabel:
    # KiCad keeps a label's direction in its angle: a right-justified label at 0 (90) degrees is the one it reads, and
    # saves, at 180 (270) degrees (`kicad-cli sch upgrade`), so the primitive is written that way.
    ang = (int(rot) + 180) % 360 if justify == "right" and int(rot) % 360 in (0, 90) else int(rot) % 360
    return SheetLabel(name, (_q4(x), _q4(y)), float(ang), _FONT, (justify, vjust), uuid=_uid(f"label:{name}:{_fmt(x)},{_fmt(y)}"))


def _rot_xy(x: float, y: float, rot: float) -> tuple[float, float]:
    """Rotate a library-local offset with the parent symbol (CCW)."""
    r = int(rot) % 360
    if r == 90:
        return -y, x
    if r == 180:
        return -x, -y
    if r == 270:
        return y, -x
    return x, y


# Library-local property offsets from KiCad power.kicad_sym — keep them
# attached to the symbol so graphics + pin + name stay one group.
_GND_REF, _GND_VAL = (0.0, -6.35), (0.0, -3.81)
_VCC_REF, _VCC_VAL = (0.0, -3.81), (0.0, 3.556)


_FLAG_REF, _FLAG_VAL = (0.0, 1.905), (0.0, 3.81)


def _hat(net: str, x: float, y: float, gnd: bool, rot: int = 0, lib: str | None = None, value: str | None = None) -> SheetSymbol:
    """Place the whole power symbol on the pin. Do not explode it into a wire + graphic."""
    flag = lib == "power:PWR_FLAG"
    lib = lib or ("power:GND" if gnd else "power:VCC")
    uid = _uid(f"hat:{lib}:{net}:{_fmt(x)},{_fmt(y)}")
    pwr = "#FLG?" if flag else "#PWR?"  # numbered in one pass over the finished sheet
    ref_l = _FLAG_REF if flag else (_GND_REF if gnd else _VCC_REF)
    val_l = _FLAG_VAL if flag else (_GND_VAL if gnd else _VCC_VAL)
    net = value or net
    rx, ry = _rot_xy(*ref_l, rot)
    vx, vy = _rot_xy(*val_l, rot)
    # Eeschema stores property Y as at_y − library_y (Y-up library, Y-down sheet).
    rwx, rwy = x + rx, y - ry
    vwx, vwy = x + vx, y - vy
    at = (_q4(x), _q4(y))
    fields = (
        SheetField("Reference", pwr, (_q4(rwx), _q4(rwy)), 0.0, hide=True),
        SheetField("Value", net, (_q4(vwx), _q4(vwy)), 0.0),
        SheetField("Footprint", "", at, 0.0, hide=True),
        SheetField("Datasheet", "", at, 0.0, hide=True),
        SheetField("Description", "", at, 0.0),
    )
    return SheetSymbol(lib, at, float(rot), uuid=uid, fields=fields, pins=(("1", _uid(f"hatpin:{net}:{_fmt(x)},{_fmt(y)}")),))


def _prop_world(part: Part, lib_xy: tuple[float, float]) -> tuple[float, float]:
    """Instance property in sheet coords."""
    dx, dy = lib_to_sheet(lib_xy[0], lib_xy[1], part.rot, part.mirror)
    return part.x + dx, part.y + dy


def _passive_label_xy(part: Part) -> tuple[tuple[float, float], tuple[float, float]]:
    """Reference/Value off the body, perpendicular to the pins, away from hats."""
    x0, y0, x1, y1 = body_aabb(part)
    cx, cy = (x0 + x1) / 2.0, (y0 + y1) / 2.0
    if len(part.pins) >= 2:
        ax, ay = _pin_world(part, part.pins[0])
        bx, by = _pin_world(part, part.pins[1])
        horiz = abs(bx - ax) >= abs(by - ay)
    else:
        horiz = False
    if horiz:
        # Pins left-right: text above (smaller Y) so GND hats below stay clear.
        return (cx, y0 - 2.6), (cx, y0 - 5.4)
    return (x1 + 2.8, cy - 1.2), (x1 + 2.8, cy + 1.4)


def _instance(part: Part) -> SheetSymbol:
    pins = tuple((p.number, _uid(f"pin:{part.ref}:{p.number}")) for p in part.pins)
    rot = int(part.rot) % 360
    # KiCad turns field text with a 90/270 symbol; a 90 field angle undoes that.
    ang = 90 if rot in (90, 270) else 0
    rjust = vjust = None
    rvis = vvis = 0
    if part.text_ref and part.text_val:
        rwx, rwy, rjust, rvis = part.text_ref
        vwx, vwy, vjust, vvis = part.text_val
    elif part.kind in ("r", "c", "l", "d"):
        (rwx, rwy), (vwx, vwy) = _passive_label_xy(part)
    else:
        rwx, rwy = _prop_world(part, part.prop_ref)
        vwx, vwy = _prop_world(part, part.prop_val)
    # A field's angle adds to the symbol's; KiCad never draws text upside
    # down, so the visual angle is (field + symbol) mod 180.
    rang = (rvis - rot) % 180
    vang = (vvis - rot) % 180
    at = (_q4(part.x), _q4(part.y))
    fields = (
        SheetField("Reference", part.ref, (_q4(rwx), _q4(rwy)), float(rang if part.text_ref else ang), hide=bool(getattr(part, "hide_ref", False)), justify=(rjust,) if rjust else ()),
        SheetField("Value", part.display, (_q4(vwx), _q4(vwy)), float(vang if part.text_val else ang), hide=bool(getattr(part, "hide_val", False)), justify=(vjust,) if vjust else ()),
        # KiCad writes these three on every symbol it saves (`kicad-cli sch upgrade`): empty, at the symbol's origin.
        SheetField("Footprint", "", at, 0.0),
        SheetField("Datasheet", "", at, 0.0),
        SheetField("Description", "", at, 0.0),
    )
    return SheetSymbol(part.lib_id, at, float(rot), part.mirror if part.mirror in ("x", "y") else None, uuid=_uid(f"sym:{part.ref}"), fields=fields, pins=pins)


def _degree(nets: list[dict]) -> dict[str, int]:
    return {n["name"]: len(n["nodes"]) for n in nets}


def _pad_nets(inst) -> dict[str, str]:
    out: dict[str, str] = {}
    for pname, net in inst.pins.items():
        pin = inst.part.pins.get(pname)
        if not pin:
            continue
        for pad in pin.pads:
            out[str(pad)] = net
    return out


def _pin_bbox(pins: list[PinDef], hw: float, hh: float) -> tuple[float, float, float, float]:
    if not pins:
        return (-hw, -hh, hw, hh)
    xs = [p.lx for p in pins]
    ys = [p.ly for p in pins]
    return (min(xs) - 1.0, min(ys) - 1.0, max(xs) + 1.0, max(ys) + 1.0)


_PASSIVE_PROP = {
    "r": ((2.032, 0.0), (-2.032, 0.0), (-3.2, -4.6, 3.2, 4.6)),
    "c": ((2.54, 0.0), (-2.54, 0.0), (-3.2, -3.4, 3.2, 3.4)),
    "l": ((2.54, 0.0), (-2.54, 0.0), (-3.2, -4.6, 3.2, 4.6)),
    "d": ((3.81, 0.0), (3.81, 2.54), (-3.5, -3.5, 6.5, 3.5)),
}

# Graphics + pins, no text: what other symbols must not overlap.
_PASSIVE_BODY = {
    "r": (-1.3, -3.81, 1.3, 3.81),
    "c": (-1.8, -2.54, 1.8, 2.54),
    "l": (-1.3, -3.81, 1.3, 3.81),
    "d": (-1.3, -2.54, 3.1, 2.54),
}

# Graphics only (between the pin ends): what a wire may not cross.
_PASSIVE_CORE = {
    "r": (-1.3, -2.8, 1.3, 2.8),
    "c": (-1.8, -0.8, 1.8, 0.8),
    "l": (-1.3, -2.3, 1.3, 2.3),
    "d": (-1.3, -1.1, 3.1, 1.1),
}


def _parts_from_design(design: Design, kinds: dict[str, str] | None = None) -> list[Part]:
    parts: list[Part] = []
    for inst in design.instances:
        pnets = _pad_nets(inst)
        prefix = (inst.part.prefix or inst.ref[:1] or "U").upper()[:1]
        prop_ref, prop_val = (0.0, 0.0), (0.0, 0.0)
        if inst.part.kind == "led":
            kind = "d"
            lib_id = "LED"
            pins = [
                PinDef("1", "K", 0.0, -2.54, 90, pnets.get("1", pnets.get("K", ""))),
                PinDef("2", "A", 0.0, 2.54, 270, pnets.get("2", pnets.get("A", ""))),
            ]
            lib_sexp = None
            prop_ref, prop_val, bbox = _PASSIVE_PROP["d"]
            body_bbox = _PASSIVE_BODY["d"]
            core_bbox = _PASSIVE_CORE["d"]
            hw = max(abs(bbox[0]), abs(bbox[2]))
            hh = max(abs(bbox[1]), abs(bbox[3]))
        elif inst.part.kind == "generic" or prefix in "RCL":
            kind = {"R": "r", "C": "c", "L": "l"}.get(prefix, "r")
            pins = _passive_pins(kind, pnets)
            lib_id = kind.upper()
            lib_sexp = None
            prop_ref, prop_val, bbox = _PASSIVE_PROP[kind]
            body_bbox = _PASSIVE_BODY[kind]
            core_bbox = _PASSIVE_CORE[kind]
            hw = max(abs(bbox[0]), abs(bbox[2]))
            hh = max(abs(bbox[1]), abs(bbox[3]))
        else:
            kind = "box"
            lib_sexp = None
            lib_id = re.sub(r"[^A-Za-z0-9_.-]", "_", inst.part.name)[:40]
            pins = []
            sp = symbol_path(inst.part)
            layout = None
            if sp and sp.exists():
                raw_text = sp.read_text()
                lib_id, raw = extract_main_symbol(raw_text)
                lib_sexp = "\t\t" + raw.replace("\n", "\n\t\t") + "\n"
                layout = parse_symbol_layout(raw_text)
                for gp in layout["pins"] or parse_symbol_pins_geom(sp):
                    pins.append(
                        PinDef(
                            gp["number"],
                            gp["name"],
                            gp["x"],
                            gp["y"],
                            gp["rot"],
                            pnets.get(gp["number"], ""),
                            float(gp.get("length", 2.54)),
                        )
                    )
            if not pins:
                pins = _box_pins(pnets, {})
            if layout:
                bbox = layout["bbox"]
                body_bbox = layout.get("body_bbox", bbox)
                core_bbox = layout.get("core_bbox", body_bbox)
                prop_ref = layout["prop_ref"]
                prop_val = layout["prop_val"]
            else:
                xs = [abs(p.lx) for p in pins] or [12.0]
                ys = [abs(p.ly) for p in pins] or [8.0]
                bbox = _pin_bbox(pins, max(xs) + 2.0, max(ys) + 2.0)
                body_bbox = bbox
                # _lib_box draws the rectangle one pin length inside the pin ends.
                core_bbox = (
                    bbox[0] + _PIN_LEN + 1.0,
                    bbox[1] + 1.0,
                    bbox[2] - _PIN_LEN - 1.0,
                    bbox[3] - 1.0,
                )
                prop_ref = (0.0, bbox[3] + 2.54)
                prop_val = (0.0, bbox[1] - 2.54)
            hw = max(abs(bbox[0]), abs(bbox[2]))
            hh = max(abs(bbox[1]), abs(bbox[3]))
        parts.append(
            Part(
                ref=inst.ref,
                lib_id=lib_id,
                display=inst.value or inst.part.value or inst.part.mpn,
                kind=kind,
                pins=pins,
                hw=hw,
                hh=hh,
                bbox=bbox,
                body_bbox=body_bbox,
                core_bbox=core_bbox,
                lib_sexp=lib_sexp,
                prop_ref=prop_ref,
                prop_val=prop_val,
            )
        )
    return parts


def _degree_parts(parts: list[Part]) -> dict[str, int]:
    deg: dict[str, int] = defaultdict(int)
    for p in parts:
        seen: set[str] = set()
        for pin in p.pins:
            if pin.net and pin.net not in seen:
                deg[pin.net] += 1
                seen.add(pin.net)
    return deg


@dataclass
class _Site:
    """One bound pin end on the sheet."""

    part: Part
    pin: PinDef
    x: float
    y: float


_EPS = 0.05
_CLEAN = 0.6  # mm² of overlap below which a placement counts as touching nothing


def _bkey(v: float) -> int:
    """Coordinate bucket one tolerance wide; a lookup checks the neighbours too."""
    return int(round(v / _EPS))
Box = tuple[float, float, float, float]


def _near(ax: float, ay: float, bx: float, by: float) -> bool:
    return abs(ax - bx) < _EPS and abs(ay - by) < _EPS


def _inside_segment(px: float, py: float, x0: float, y0: float, x1: float, y1: float) -> bool:
    """Point on an axis-aligned segment, endpoints excluded."""
    if abs(x0 - x1) < _EPS:
        return abs(px - x0) < _EPS and min(y0, y1) + _EPS < py < max(y0, y1) - _EPS
    if abs(y0 - y1) < _EPS:
        return abs(py - y0) < _EPS and min(x0, x1) + _EPS < px < max(x0, x1) - _EPS
    return False


def pin_lines(parts: list["Part"]) -> list[tuple[float, float, float, float, str]]:
    """Every pin's drawn line, `(end x, end y, body x, body y, "REF.PIN")`: from the pin's electrical end (where a wire
    connects) to where it meets the body, for every part (box symbols and two-pin parts alike)."""
    out = []
    for p in parts:
        for pin in p.pins:
            ln = float(getattr(pin, "length", 0.0) or 0.0)
            if ln <= _EPS:
                continue
            ex, ey = pin_world(p, pin)
            ox, oy = pin_outward(p, pin)
            out.append((ex, ey, ex - ox * ln, ey - oy * ln, f"{p.ref}.{pin.number}"))
    return out


def over_pin_line(x0: float, y0: float, x1: float, y1: float, line: tuple) -> bool:
    """A wire segment meets a pin's drawn line anywhere but the pin's electrical end: it crosses the line, runs along
    it, or ends on it. KiCad connects a wire only at a pin end, so the drawing then reads as a connection that is not
    one (refute:sheet 2026-10-08: node's LED wire across SW_RST pin 2 read as LED tied to the switch and GND)."""
    ex, ey, bx, by = line[0], line[1], line[2], line[3]
    if max(x0, x1) < min(ex, bx) - _EPS or min(x0, x1) > max(ex, bx) + _EPS:
        return False
    if max(y0, y1) < min(ey, by) - _EPS or min(y0, y1) > max(ey, by) + _EPS:
        return False
    sv, lv = abs(x0 - x1) < _EPS, abs(ex - bx) < _EPS
    sh, lh = abs(y0 - y1) < _EPS, abs(ey - by) < _EPS
    if (sv and lv) or (sh and lh):
        # Collinear (the box test above already put them on one line): the shared stretch, if longer than a point.
        if sv:
            lo, hi = max(min(y0, y1), min(ey, by)), min(max(y0, y1), max(ey, by))
            if hi - lo > _EPS:
                return True
            return hi - lo > -_EPS and not _near(x0, lo, ex, ey)
        lo, hi = max(min(x0, x1), min(ex, bx)), min(max(x0, x1), max(ex, bx))
        if hi - lo > _EPS:
            return True
        return hi - lo > -_EPS and not _near(lo, y0, ex, ey)
    if (sv and lh) or (sh and lv):
        ix, iy = (x0, ey) if sv else (ex, y0)
        return not _near(ix, iy, ex, ey)
    return False


def _segment_crosses_box(x0: float, y0: float, x1: float, y1: float, box: Box, pad: float = 0.3) -> bool:
    bx0, by0, bx1, by1 = box
    return not (
        max(x0, x1) < bx0 - pad
        or min(x0, x1) > bx1 + pad
        or max(y0, y1) < by0 - pad
        or min(y0, y1) > by1 + pad
    )


def _point_in_aabb(x: float, y: float, box: Box, pad: float = 0.0) -> bool:
    x0, y0, x1, y1 = box
    return (x0 - pad) <= x <= (x1 + pad) and (y0 - pad) <= y <= (y1 + pad)


def _seg_through_box(x0: float, y0: float, x1: float, y1: float, box: Box) -> bool:
    """The segment's middle passes through the box. A wire that only leaves a
    symbol at its pin, along the box edge, does not."""
    bx0, by0, bx1, by1 = box
    if max(x0, x1) < bx0 or min(x0, x1) > bx1 or max(y0, y1) < by0 or min(y0, y1) > by1:
        return False  # nowhere near the box
    span = math.hypot(x1 - x0, y1 - y0)
    if span < 1.0:
        return False
    for t in (0.35, 0.5, 0.65):
        if _point_in_aabb(x0 + (x1 - x0) * t, y0 + (y1 - y0) * t, box, pad=-0.2):
            return True
    return False


def _name_blocked(sheet: "_Sheet", box: Box, owner: str) -> bool:
    """A name the readability report would reject. The part's own body does
    not count: the name is allowed to sit against it."""
    for x0, y0, x1, y1, _net in sheet.segs:
        if _overlap_area(_seg_box(x0, y0, x1, y1), box) > 0.6:
            return True
    for o in sheet.occupants:
        if (o.owner == owner or o.owner.startswith(owner + ".")) and o.kind in ("symbol", "pin", "pintext", "reserve"):
            continue
        if o.kind in ("hat", "label", "ref", "value", "symbol", "pin") and _overlap_area(o.box, box) > 0.6:
            return True
    return False


def _graphic_box(o: "_Occupant") -> Box:
    """The drawn arrow or diamond, not the name beside it. The name's box is
    much wider, and a wire beside the word is not a wire through the symbol."""
    cx = (o.box[0] + o.box[2]) / 2.0
    rot = int(o.rot or 0)
    down = o.gnd if rot == 0 else not o.gnd
    pin_y = o.box[1] if down else o.box[3]
    if down:
        return (cx - 1.4, pin_y, cx + 1.4, pin_y + 2.8)
    return (cx - 1.4, pin_y - 2.8, cx + 1.4, pin_y)


def _collinear_touch(
    x0: float, y0: float, x1: float, y1: float, sx0: float, sy0: float, sx1: float, sy1: float
) -> bool:
    """Two axis-aligned segments on one line that share any point."""
    if abs(x0 - x1) < _EPS and abs(sx0 - sx1) < _EPS and abs(x0 - sx0) < _EPS:
        return max(min(y0, y1), min(sy0, sy1)) <= min(max(y0, y1), max(sy0, sy1)) + _EPS
    if abs(y0 - y1) < _EPS and abs(sy0 - sy1) < _EPS and abs(y0 - sy0) < _EPS:
        return max(min(x0, x1), min(sx0, sx1)) <= min(max(x0, x1), max(sx0, sx1)) + _EPS
    return False


def _crosses(
    x0: float, y0: float, x1: float, y1: float, sx0: float, sy0: float, sx1: float, sy1: float
) -> bool:
    """Two axis-aligned segments crossing each other's interior. KiCad does not
    connect them, but a reader cannot tell - so it is never drawn."""
    a_vert = abs(x0 - x1) < _EPS
    s_vert = abs(sx0 - sx1) < _EPS
    if a_vert == s_vert:
        return False
    if a_vert:
        vx, vy0, vy1, hy, hx0, hx1 = x0, min(y0, y1), max(y0, y1), sy0, min(sx0, sx1), max(sx0, sx1)
    else:
        vx, vy0, vy1, hy, hx0, hx1 = sx0, min(sy0, sy1), max(sy0, sy1), y0, min(x0, x1), max(x0, x1)
    return hx0 + _EPS < vx < hx1 - _EPS and vy0 + _EPS < hy < vy1 - _EPS


def _overlap_area(a: Box, b: Box) -> float:
    w = min(a[2], b[2]) - max(a[0], b[0])
    h = min(a[3], b[3]) - max(a[1], b[1])
    return w * h if w > 0 and h > 0 else 0.0


def _seg_box(x0: float, y0: float, x1: float, y1: float, pad: float = 0.25) -> Box:
    return (min(x0, x1) - pad, min(y0, y1) - pad, max(x0, x1) + pad, max(y0, y1) + pad)


@dataclass
class _Occupant:
    box: Box
    kind: str  # symbol | pintext | ref | value | label | hat
    owner: str
    net: str = ""
    tag: str = ""
    rot: int = 0
    gnd: bool = False  # placement this belongs to, so it can be taken back
    rot: int | None = None  # a power symbol's rotation, to re-score it in place


_CELL = 5.08


def _cell_range(lo: float, hi: float) -> range:
    return range(math.floor((lo - 0.1) / _CELL), math.floor((hi + 0.1) / _CELL) + 1)


def _cell_index(boxes: list[Box]) -> dict[tuple[int, int], list[int]]:
    """Each box's index under every grid cell its box (already grown by the test's tolerance) touches."""
    idx: dict[tuple[int, int], list[int]] = defaultdict(list)
    for i, (x0, y0, x1, y1) in enumerate(boxes):
        for cx in _cell_range(x0, x1):
            for cy in _cell_range(y0, y1):
                idx[(cx, cy)].append(i)
    return idx


def _cell_hits(idx: dict[tuple[int, int], list[int]], x0: float, y0: float, x1: float, y1: float) -> list[int]:
    """The boxes sharing a cell with the segment's box, in index order."""
    out: set[int] = set()
    for cx in _cell_range(min(x0, x1), max(x0, x1)):
        for cy in _cell_range(min(y0, y1), max(y0, y1)):
            out.update(idx.get((cx, cy), ()))
    return sorted(out)


class _Sheet:
    """Geometry already on the sheet.

    Two jobs. Connectivity: KiCad joins wire endpoints to pin ends and to other
    wire endpoints, never anything that merely crosses, so every new segment
    must end on its own net and keep its interior off every pin end and every
    symbol's graphics. Readability: everything drawn is also a box, and each
    new label, power symbol or Reference/Value picks the spot that overlaps the
    least of what is already there.
    """

    def __init__(self, parts: list[Part]):
        self.parts = parts
        self.pin_ends = [pin_world(p, pin) for p in parts for pin in p.pins]
        self.cores = [core_aabb(p) for p in parts]
        self.pin_lines = pin_lines(parts)
        # Symbol cores and pin lines bucketed by a coarse grid cell: `segment_ok` tests a segment only against those whose
        # box shares a cell with its own (the parts do not move while a pass draws; the answer is the same as testing all).
        self._core_cells = _cell_index([(b[0] - 0.3, b[1] - 0.3, b[2] + 0.3, b[3] + 0.3) for b in self.cores])
        self._line_cells = _cell_index([(min(l[0], l[2]) - _EPS, min(l[1], l[3]) - _EPS, max(l[0], l[2]) + _EPS, max(l[1], l[3]) + _EPS) for l in self.pin_lines])
        self.segs: list[tuple[float, float, float, float, str]] = []
        self.seg_tags: list[str] = []
        self.seg_boxes: list[Box] = []
        self.occupants: list[_Occupant] = []
        # Label anchors and power-symbol pins: a wire through or onto one connects.
        self.anchors: list[tuple[float, float, str, str]] = []
        self._anc_of: list | None = None
        self._anc_n = -1
        # Pin ends bucketed by coordinate, wires split by axis with their
        # extents: a candidate segment only does geometry against what lies
        # on its own line.
        self.pins_x: dict[int, list[tuple[float, float]]] = defaultdict(list)
        self.pins_y: dict[int, list[tuple[float, float]]] = defaultdict(list)
        for px, py in self.pin_ends:
            self.pins_x[_bkey(px)].append((px, py))
            self.pins_y[_bkey(py)].append((px, py))
        self.vsegs: list[tuple[float, float, float, str]] = []  # x, lo, hi, net
        self.hsegs: list[tuple[float, float, float, str]] = []  # y, lo, hi, net
        self._stale = True
        # What each label and power symbol placement is, in schematic.core.py's terms (`sch_gen` writes them), by tag.
        self.records: dict[str, dict] = {}
        # The pin of every power symbol and PWR_FLAG drawn, by tag: a symbol pin like a part's, so it is one of the ways
        # that meet where it touches a wire (`junction_points`).
        self.hat_pins: list[tuple[float, float, str]] = []

    def _index(self) -> None:
        if not self._stale:
            return
        self.vsegs = []
        self.hsegs = []
        for x0, y0, x1, y1, net in self.segs:
            if abs(x0 - x1) < _EPS:
                self.vsegs.append((x0, min(y0, y1), max(y0, y1), net))
            else:
                self.hsegs.append((y0, min(x0, x1), max(x0, x1), net))
        self._stale = False

    def _pins_at_x(self, x: float):
        k = _bkey(x)
        for kk in (k - 1, k, k + 1):
            yield from self.pins_x.get(kk, ())

    def _pins_at_y(self, y: float):
        k = _bkey(y)
        for kk in (k - 1, k, k + 1):
            yield from self.pins_y.get(kk, ())

    def _anchor_index(self) -> None:
        """Anchors bucketed by x and by y; appended ones are added, a `remove` (a new list) rebuilds."""
        if self._anc_of is not self.anchors or self._anc_n > len(self.anchors):
            self._anc_x: dict[int, list] = defaultdict(list)
            self._anc_y: dict[int, list] = defaultdict(list)
            self._anc_of, self._anc_n = self.anchors, 0
        for a in self.anchors[self._anc_n :]:
            self._anc_x[_bkey(a[0])].append(a)
            self._anc_y[_bkey(a[1])].append(a)
        self._anc_n = len(self.anchors)

    def _anchors_at_x(self, x: float) -> list:
        """The anchors within one tolerance of x (the same neighbourhood `_pins_at_x` looks in)."""
        self._anchor_index()
        k = _bkey(x)
        return [a for kk in (k - 1, k, k + 1) for a in self._anc_x.get(kk, ())]

    def _anchors_at_y(self, y: float) -> list:
        self._anchor_index()
        k = _bkey(y)
        return [a for kk in (k - 1, k, k + 1) for a in self._anc_y.get(kk, ())]

    # -- connectivity --------------------------------------------------------
    def on_pin_end(self, x: float, y: float) -> bool:
        return any(_near(x, y, px, py) for px, py in self._pins_at_x(x))

    def pins_at(self, x: float, y: float) -> int:
        """Symbol pins ending at the point: parts' and power symbols' (and PWR_FLAGs')."""
        return sum(1 for px, py in self._pins_at_x(x) if _near(x, y, px, py)) + sum(1 for hx, hy, _t in self.hat_pins if _near(x, y, hx, hy))

    def on_wire_interior(self, x: float, y: float) -> bool:
        """Strictly inside some wire, any net (a symbol pin there does not connect)."""
        self._index()
        for sx, lo, hi, _n in self.vsegs:
            if abs(sx - x) < _EPS and lo + _EPS < y < hi - _EPS:
                return True
        for sy, lo, hi, _n in self.hsegs:
            if abs(sy - y) < _EPS and lo + _EPS < x < hi - _EPS:
                return True
        return False

    def anchor(self, x: float, y: float, net: str, tag: str = "") -> None:
        self.anchors.append((x, y, net, tag))

    def remove(self, tag: str) -> None:
        """Take one placement (its wires, box and anchor) back off the sheet."""
        keep = [i for i, t in enumerate(self.seg_tags) if t != tag]
        self.segs = [self.segs[i] for i in keep]
        self.seg_tags = [self.seg_tags[i] for i in keep]
        self.seg_boxes = [self.seg_boxes[i] for i in keep]
        self.occupants = [o for o in self.occupants if o.tag != tag]
        self.anchors = [a for a in self.anchors if a[3] != tag]
        self.hat_pins = [h for h in self.hat_pins if h[2] != tag]
        self.records.pop(tag, None)
        self._stale = True

    def segment_ok(self, x0: float, y0: float, x1: float, y1: float, net: str = "") -> bool:
        """Interior off every pin end, every label anchor and every symbol's
        graphics, and no contact with another net's wire other than a plain
        crossing (KiCad joins collinear wires that touch, and a T without a
        junction misleads). Never along a wire of its own net either: two wires
        on one stretch put a junction dot where nothing branches (refute:sheet
        2026-10-08: a power symbol's stub laid over its pin's bus bar, c3_usb and DS2)."""
        vertical = abs(x0 - x1) < _EPS
        if vertical:
            lo, hi = min(y0, y1), max(y0, y1)
            for px, py in self._pins_at_x(x0):
                if abs(px - x0) < _EPS and lo + _EPS < py < hi - _EPS:
                    return False
        else:
            lo, hi = min(x0, x1), max(x0, x1)
            for px, py in self._pins_at_y(y0):
                if abs(py - y0) < _EPS and lo + _EPS < px < hi - _EPS:
                    return False
        # Only an anchor on the segment's own line can be inside it or at one of its ends (a slanted one, never drawn,
        # is tested against every anchor).
        if vertical:
            near = self._anchors_at_x(x0) + self._anchors_at_x(x1)
        elif abs(y0 - y1) < _EPS:
            near = self._anchors_at_y(y0) + self._anchors_at_y(y1)
        else:
            near = self.anchors
        for ax, ay, anet, _tag in near:
            if _inside_segment(ax, ay, x0, y0, x1, y1):
                return False  # a symbol pin or label on a wire's interior, any net: never drawn
            if anet != net and (_near(ax, ay, x0, y0) or _near(ax, ay, x1, y1)):
                return False
        cores, lines = self.cores, self.pin_lines
        if any(_segment_crosses_box(x0, y0, x1, y1, cores[i]) for i in _cell_hits(self._core_cells, x0, y0, x1, y1)):
            return False
        if any(over_pin_line(x0, y0, x1, y1, lines[i]) for i in _cell_hits(self._line_cells, x0, y0, x1, y1)):
            return False
        self._index()
        # Against other nets' wires: no collinear contact, no endpoint of
        # either on the other's interior, no crossing.
        if vertical:
            x = x0
            for sx, slo, shi, snet in self.vsegs:
                if snet != net and abs(sx - x) < _EPS and max(lo, slo) <= min(hi, shi) + _EPS:
                    return False
                if snet == net and abs(sx - x) < _EPS and min(hi, shi) - max(lo, slo) > _EPS:
                    return False  # along a wire of its own net: two wires drawn on top of each other
            for sy, slo, shi, snet in self.hsegs:
                if snet == net:
                    continue
                if lo + _EPS < sy < hi - _EPS:
                    if abs(slo - x) < _EPS or abs(shi - x) < _EPS:
                        return False  # their end on my interior
                    if slo + _EPS < x < shi - _EPS:
                        return False  # crossing
                if (abs(lo - sy) < _EPS or abs(hi - sy) < _EPS) and slo + _EPS < x < shi - _EPS:
                    return False  # my end on their interior
        else:
            y = y0
            for sy, slo, shi, snet in self.hsegs:
                if snet != net and abs(sy - y) < _EPS and max(lo, slo) <= min(hi, shi) + _EPS:
                    return False
                if snet == net and abs(sy - y) < _EPS and min(hi, shi) - max(lo, slo) > _EPS:
                    return False
            for sx, slo, shi, snet in self.vsegs:
                if snet == net:
                    continue
                if lo + _EPS < sx < hi - _EPS:
                    if abs(slo - y) < _EPS or abs(shi - y) < _EPS:
                        return False
                    if slo + _EPS < y < shi - _EPS:
                        return False
                if (abs(lo - sx) < _EPS or abs(hi - sx) < _EPS) and slo + _EPS < y < shi - _EPS:
                    return False
        return True

    def blocker(self, pts: list, net: str) -> str:
        """What a path of `net` would meet, as a fact for a refused lock: a pin end, another net's name or symbol, a
        symbol's graphics, or another net's wire."""
        for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
            if _near(x0, y0, x1, y1):
                continue
            for px, py in self.pin_ends:
                if _inside_segment(px, py, x0, y0, x1, y1):
                    return f"a pin end at ({px:g}, {py:g})"
            for ax, ay, anet, tag in self.anchors:
                if _inside_segment(ax, ay, x0, y0, x1, y1) or (anet != net and (_near(ax, ay, x0, y0) or _near(ax, ay, x1, y1))):
                    return f"the {anet} {'label' if tag.startswith('label') else 'symbol'} at ({ax:g}, {ay:g})"
            for p in self.parts:
                if _segment_crosses_box(x0, y0, x1, y1, core_aabb(p)):
                    return f"symbol {p.ref}"
            for ln in self.pin_lines:
                if over_pin_line(x0, y0, x1, y1, ln):
                    return f"the pin line of {ln[4]}"
            from .sch_score import seg_through_box

            for o in self.occupants:
                if o.kind == "hat" and seg_through_box(x0, y0, x1, y1, _graphic_box(o)):
                    return f"the {o.net} power symbol at {o.owner}"
            for sx0, sy0, sx1, sy1, snet in self.segs:
                if snet != net and _seg_through_box(x0, y0, x1, y1, _seg_box(sx0, sy0, sx1, sy1, 0.05)):
                    return f"the {snet} wire ({sx0:g}, {sy0:g})-({sx1:g}, {sy1:g})"
        return "another net's wire end"

    def point_free(self, x: float, y: float, net: str) -> bool:
        """A bend, stub end or label anchor may not touch another net."""
        if self.on_pin_end(x, y):
            return False
        if any(anet != net and _near(x, y, ax, ay) for ax, ay, anet, _t in self._anchors_at_x(x)):
            return False
        self._index()
        for sx, lo, hi, snet in self.vsegs:
            if snet != net and abs(sx - x) < _EPS and lo - _EPS <= y <= hi + _EPS:
                return False
        for sy, lo, hi, snet in self.hsegs:
            if snet != net and abs(sy - y) < _EPS and lo - _EPS <= x <= hi + _EPS:
                return False
        return True

    def path_ok(self, pts: list[tuple[float, float]], net: str) -> bool:
        for (ax, ay), (bx, by) in zip(pts, pts[1:]):
            if _near(ax, ay, bx, by):
                continue
            if abs(ax - bx) > _EPS and abs(ay - by) > _EPS:
                return False
            if not self.segment_ok(ax, ay, bx, by, net):
                return False
        return all(self.point_free(x, y, net) for x, y in pts[1:-1])

    def add(self, pts: list[tuple[float, float]], net: str, tag: str = "") -> list[str]:
        out: list[str] = []
        for (ax, ay), (bx, by) in zip(pts, pts[1:]):
            if _near(ax, ay, bx, by):
                continue
            if any(
                (_near(ax, ay, sx0, sy0) and _near(bx, by, sx1, sy1)) or (_near(ax, ay, sx1, sy1) and _near(bx, by, sx0, sy0))
                for sx0, sy0, sx1, sy1, _n in self.segs
            ):
                continue  # already drawn (a flag's stub over a symbol's)
            self.segs.append((ax, ay, bx, by, net))
            self.seg_tags.append(tag)
            self.seg_boxes.append(_seg_box(ax, ay, bx, by))
            out.append(_wire(ax, ay, bx, by))
        self._stale = True
        return out

    # -- readability ----------------------------------------------------------
    def occupy(self, box: Box, kind: str, owner: str, net: str = "", tag: str = "") -> None:
        self.occupants.append(_Occupant(box, kind, owner, net, tag))

    def hard_cost(self, box: Box, net: str = "", movable_labels: bool = False) -> float:
        """Overlap with what is really drawn: symbols, pins, text, other nets'
        wires. Reservations and the net's own wires do not count. Zero means
        the box is clean. With `movable_labels`, labels and their stubs do not count
        either: a power symbol is placed first and the labels, which have more spots,
        settle around it in the passes that follow."""
        bx0, by0, bx1, by1 = box
        total = 0.0
        for o in self.occupants:
            if o.kind == "reserve" or (movable_labels and o.kind == "label"):
                continue
            ox0, oy0, ox1, oy1 = o.box
            if ox1 <= bx0 or ox0 >= bx1 or oy1 <= by0 or oy0 >= by1:
                continue
            total += (min(bx1, ox1) - max(bx0, ox0)) * (min(by1, oy1) - max(by0, oy0))
        for (sx0, sy0, sx1, sy1), seg, tag in zip(self.seg_boxes, self.segs, self.seg_tags):
            if seg[4] == net or sx1 <= bx0 or sx0 >= bx1 or sy1 <= by0 or sy0 >= by1:
                continue
            if movable_labels and tag.startswith("label:"):
                continue
            total += (min(bx1, sx1) - max(bx0, sx0)) * (min(by1, sy1) - max(by0, sy0))
        return total

    def cost(self, box: Box, net: str = "") -> float:
        """Overlap area with what is drawn. Own-net wires count less: a label
        sits on its wire by design. Reservations (where a power symbol or a
        part's text will probably go) count half, and not for their own net."""
        bx0, by0, bx1, by1 = box
        total = 0.0
        for o in self.occupants:
            ox0, oy0, ox1, oy1 = o.box
            if ox1 <= bx0 or ox0 >= bx1 or oy1 <= by0 or oy0 >= by1:
                continue
            area = (min(bx1, ox1) - max(bx0, ox0)) * (min(by1, oy1) - max(by0, oy0))
            if o.kind == "reserve":
                if o.net and o.net == net:
                    continue
                total += area * 0.5
            else:
                total += area
        for (sx0, sy0, sx1, sy1), seg in zip(self.seg_boxes, self.segs):
            if sx1 <= bx0 or sx0 >= bx1 or sy1 <= by0 or sy0 >= by1:
                continue
            area = (min(bx1, sx1) - max(bx0, sx0)) * (min(by1, sy1) - max(by0, sy0))
            total += area * (0.3 if seg[4] == net else 1.0)
        return total


class _Union:
    def __init__(self, n: int):
        self.parent = list(range(n))

    def find(self, i: int) -> int:
        while self.parent[i] != i:
            self.parent[i] = self.parent[self.parent[i]]
            i = self.parent[i]
        return i

    def join(self, i: int, j: int) -> None:
        self.parent[self.find(i)] = self.find(j)

    def groups(self) -> list[list[int]]:
        by: dict[int, list[int]] = defaultdict(list)
        for i in range(len(self.parent)):
            by[self.find(i)].append(i)
        return list(by.values())


def _routes(a: _Site, b: _Site) -> list[list[tuple[float, float]]]:
    """Candidate Manhattan paths from a to b: straight, out-across-in with the
    crossing leg at the midpoint or a few grid steps out from either pin, and
    the two L shapes. Most natural first; ties in cost keep that order."""
    ax, ay, bx, by = a.x, a.y, b.x, b.y
    oa = pin_outward(a.part, a.pin)
    ob = pin_outward(b.part, b.pin)
    out: list[list[tuple[float, float]]] = []
    if abs(ax - bx) < _EPS or abs(ay - by) < _EPS:
        out.append([(ax, ay), (bx, by)])
    if abs(oa[0]) >= abs(oa[1]):
        xs = [(ax + bx) / 2.0]
        xs += [ax + oa[0] * k for k in (2.54, 5.08, 7.62, 10.16)]
        xs += [bx + ob[0] * k for k in (2.54, 5.08, 7.62)] if abs(ob[0]) >= abs(ob[1]) else []
        for mx in xs:
            out.append([(ax, ay), (mx, ay), (mx, by), (bx, by)])
        out.append([(ax, ay), (bx, ay), (bx, by)])
        out.append([(ax, ay), (ax, by), (bx, by)])
    else:
        ys = [(ay + by) / 2.0]
        ys += [ay + oa[1] * k for k in (2.54, 5.08, 7.62, 10.16)]
        ys += [by + ob[1] * k for k in (2.54, 5.08, 7.62)] if abs(ob[1]) > abs(ob[0]) else []
        for my in ys:
            out.append([(ax, ay), (ax, my), (bx, my), (bx, by)])
        out.append([(ax, ay), (ax, by), (bx, by)])
        out.append([(ax, ay), (bx, ay), (bx, by)])
    return out


def _route_cost(sheet: _Sheet, pts: list[tuple[float, float]], net: str) -> float:
    """Overlap of the path with everything drawn, plus a little per mm and per bend."""
    c = 0.0
    for (ax, ay), (bx, by) in zip(pts, pts[1:]):
        if _near(ax, ay, bx, by):
            continue
        c += sheet.cost(_seg_box(ax, ay, bx, by), net) + 0.01 * math.hypot(bx - ax, by - ay)
    return c + 0.2 * max(0, len(pts) - 2)


# -- text boxes (KiCad 1.27 mm font) ------------------------------------------
_TEXT_H = 1.6


def _label_pose(ox: float, oy: float) -> tuple[int, str]:
    """Text hangs away from the symbol along the outward direction."""
    if abs(ox) >= abs(oy):
        return 0, "right" if ox < 0 else "left"
    return 90, "right" if oy > 0 else "left"


def _label_box(name: str, x: float, y: float, rot: int, just: str, vjust: str) -> Box:
    w = _text_w(name) + 0.4
    if rot == 0:
        x0, x1 = (x, x + w) if just == "left" else (x - w, x)
        y0, y1 = (y - _TEXT_H, y) if vjust == "bottom" else (y, y + _TEXT_H)
    else:
        y0, y1 = (y - w, y) if just == "left" else (y, y + w)
        x0, x1 = (x - _TEXT_H, x) if vjust == "bottom" else (x, x + _TEXT_H)
    return (x0, y0, x1, y1)


def _prop_box(text: str, x: float, y: float, just: str | None, width=None) -> Box:
    w = (width or _text_w)(text) + 0.4
    if just == "left":
        x0, x1 = x, x + w
    elif just == "right":
        x0, x1 = x - w, x
    else:
        x0, x1 = x - w / 2.0, x + w / 2.0
    return (x0, y - _TEXT_H / 2.0, x1, y + _TEXT_H / 2.0)


def _hat_box(net: str, x: float, y: float, gnd: bool, rot: int, text: str | None = None) -> Box:
    hw = max(1.5, _text_w(text or net) / 2.0 + 0.2)
    down = gnd if rot == 0 else not gnd  # GND hangs down; a supply points up
    return (x - hw, y, x + hw, y + 4.8) if down else (x - hw, y - 4.8, x + hw, y)


# -- placements ---------------------------------------------------------------
def _two_pin(part: Part) -> bool:
    """Passives and 2-pin library symbols (a switch, a crystal): text is placed
    around them, and they may turn or mirror to face what they hang off."""
    return bool(part.pins) and (part.kind in ("r", "c", "l", "d") or len(part.pins) <= 2)


def _reserve(sheet: _Sheet, parts: list[Part], sites: dict[str, list[_Site]], kinds: dict[str, str] | None) -> None:
    """Before routing: pencil in where power symbols and passive text will want
    to be, so wires steer around those spots instead of through them."""
    for net, sts in sites.items():
        if not is_power_net(net, kinds):
            continue
        gnd = is_ground_net(net, kinds)
        for s in sts:
            sheet.occupy(hat_zone(s.part, s.pin, net, gnd), "reserve", s.part.ref, net)
    for part in parts:
        if _two_pin(part):
            sheet.occupy(text_zone(part), "reserve", part.ref)


_HatCand = tuple[tuple[int, float], _Site, list[tuple[float, float]], int]


def _hat_candidates(sheet: _Sheet, members: list[_Site], net: str, gnd: bool, text: str | None = None, branches: bool = False) -> list[_HatCand]:
    """Every legal spot for one group's power symbol, best first: on the pin end
    when the pin already points the symbol's way, else on a stub — straight, or
    with a jog either way. A spot that touches nothing drawn beats every spot
    that does; among those, the least overlap."""
    found: list[_HatCand] = []
    if len(members) > 4:
        # A big group (an IC's GND rail) only needs its few least crowded pins
        # tried - plus the end the symbol naturally hangs from: the bottom pin
        # for ground, the top one for a supply, where pointing the right way
        # is free.
        def _room(s: _Site) -> float:
            ox, oy = pin_outward(s.part, s.pin)
            cx, cy = s.x + ox * 3.0, s.y + oy * 3.0
            return sheet.hard_cost((cx - 4.0, cy - 4.0, cx + 4.0, cy + 4.0), net)

        natural_end = max(members, key=lambda s: s.y) if gnd else min(members, key=lambda s: s.y)
        members = sorted(members, key=_room)[:3] + [natural_end]
    for s in members:
        ox, oy = pin_outward(s.part, s.pin)
        vertical = abs(oy) > abs(ox)
        natural = vertical and ((gnd and oy > 0) or (not gnd and oy < 0))
        jog_y = 1.0 if gnd else -1.0  # GND hangs down the sheet, a supply points up
        cands: list[tuple[list[tuple[float, float]], int]] = []
        # Beside the pin, not further along it. The second symbol on a pin (a
        # PWR_FLAG behind the VCC arrow) cannot slide out on that pin: the wire
        # to reach it runs through the symbol already there. 7.62 mm clears the
        # flag's own name.
        px, py = -oy, ox
        side_pts = [
            [(s.x, s.y), (s.x + px * side, s.y + py * side)]
            for side in (7.62, -7.62, 10.16, -10.16)
        ]
        if natural:
            for length in (0.0, 2.54, 5.08):
                cands.append(([(s.x, s.y), (s.x + ox * length, s.y + oy * length)], 0))
            for pts in side_pts:
                cands.append((pts, 0))
        elif vertical:
            for length in (2.54, 5.08, 7.62):
                cands.append(([(s.x, s.y), (s.x + ox * length, s.y + oy * length)], 180))
            for pts in side_pts:
                cands.append((pts, 180))
        else:
            for length in (2.54, 5.08, 7.62, 10.16, 12.7, 15.24):
                ex, ey = s.x + ox * length, s.y + oy * length
                cands.append(([(s.x, s.y), (ex, ey)], 0))
                # A jog the symbol's way, or the other way (a supply that
                # first drops below its pin, then points up - the clean answer
                # when the pin above is busy).
                for jog in (2.54, 5.08):
                    cands.append(([(s.x, s.y), (ex, ey), (ex, ey + jog_y * jog)], 0))
                    cands.append(([(s.x, s.y), (ex, ey), (ex, ey - jog_y * jog)], 0))
                # Upside down (a supply pointing down, GND pointing up): only
                # when every upright spot collides - it still beats a crossing.
                cands.append(([(s.x, s.y), (ex, ey)], 180))
                for jog in (2.54, 5.08):
                    cands.append(([(s.x, s.y), (ex, ey), (ex, ey - jog_y * jog)], 180))
        if branches:
            cands += _flag_branches(sheet, s, net)
        for pts, rot in cands:
            hx, hy = pts[-1]
            moving = [p for p in pts[1:]]
            if moving and not _near(pts[0][0], pts[0][1], hx, hy):
                # What is drawn: the stub less any stretch its own net's wire already covers (`_trim_own`).
                if not sheet.path_ok(_trim_own(sheet, pts, net), net) or not sheet.point_free(hx, hy, net):
                    continue
                # kicad-cli: a symbol pin on a wire's interior does not connect.
                if sheet.on_wire_interior(hx, hy):
                    continue
            box = _hat_box(net, hx, hy, gnd, rot, text)
            # The symbol may not sit on its own wire: a GND jogged up and
            # pointing back down through the jog is legal and looks broken.
            if any(
                _point_in_aabb((ax + bx) / 2.0, (ay + by) / 2.0, box, pad=-0.05)
                for (ax, ay), (bx, by) in zip(pts, pts[1:])
                if not _near(ax, ay, bx, by)
            ):
                continue
            hard = sheet.hard_cost(box, net)
            c = sheet.cost(box, net)
            # A same-net wire does not count in hard_cost, so a flag would
            # happily sit on the arrow's stem or on another stub of its net.
            for sx0, sy0, sx1, sy1, _snet in sheet.segs:
                if _seg_through_box(sx0, sy0, sx1, sy1, box):
                    hard += 5.0
            for other in sheet.occupants:
                if other.kind != "hat":
                    continue
                graphic = _graphic_box(other)
                for (ax, ay), (bx, by) in zip(pts, pts[1:]):
                    if _seg_through_box(ax, ay, bx, by, graphic):
                        hard += 5.0
            if rot == 180 and not vertical:
                # Upside down: a supply pointing down is seen often enough;
                # a ground pointing up hardly ever.
                c += 4.0 if gnd else 2.5
            for (ax, ay), (bx, by) in zip(pts, pts[1:]):
                if not _near(ax, ay, bx, by):
                    sb = _seg_box(ax, ay, bx, by)
                    hard += sheet.hard_cost(sb, net)
                    c += sheet.cost(sb, net) + 0.02 * math.hypot(bx - ax, by - ay)
            if len(pts) == 3 and not natural and not vertical:
                # The jog away from the symbol's direction reads a little worse.
                away = (pts[2][1] - pts[1][1]) * jog_y < 0
                c += 0.5 if away else 0.0
            found.append(((0 if hard < _CLEAN else 1, c), s, pts, rot))
    found.sort(key=lambda t: t[0])
    if text == "PWR_FLAG" and not branches and (not found or found[0][0][0] > 0):
        # No clean spot from the pins: try off the ends of the power symbols already drawn on them.
        return _hat_candidates(sheet, members, net, gnd, text, branches=True)
    return found


def _flag_branches(sheet: _Sheet, s: _Site, net: str) -> list:
    """A PWR_FLAG may also hang off the end of the stub of a power symbol already drawn on this pin: straight on past
    it, or to either side. A stub from the pin itself has to pass that symbol (DS2 with no core: the VDDA flag's only
    stub from J2.1 ran up through J2's VDDA arrow)."""
    out = []
    for tag, rec in sheet.records.items():
        if rec.get("kind") != "power" or rec.get("net") != net or rec["site"] is not s:
            continue
        a = next(((ax, ay) for ax, ay, anet, t in sheet.anchors if t == tag), None)
        if a is None:
            continue
        dx, dy = _SIDE_VEC[rec["side"]]
        for vx, vy in ((dx, dy), (-dy, dx), (dy, -dx)):
            for length in (2.54, 5.08, 7.62):
                out.append(([a, (a[0] + vx * length, a[1] + vy * length)], 0))
    return out


def _place_hat(
    sheet: _Sheet, members: list[_Site], net: str, gnd: bool, tag: str = "", lib: str | None = None, value: str | None = None
) -> list[str]:
    cands = _hat_candidates(sheet, members, net, gnd, value)
    if cands:
        # A spot whose stub runs through another power symbol's or flag's drawing is taken only when there is no other:
        # on DS2 with no core the VDDA PWR_FLAG's stub ran up through J2's VDDA arrow (a hard wire through text).
        return _commit_hat(sheet, cands[0], net, gnd, tag, lib, value)
    s = members[0]
    return _commit_hat(sheet, ((1, 0.0), s, [(s.x, s.y)], 0), net, gnd, tag, lib, value)


def _trim_own(sheet: _Sheet, pts: list, net: str) -> list:
    """A symbol's stub without the stretch from its pin that runs along a wire of its own net already drawn (a bus bar
    leaving the pin the same way): the stub then starts where that wire turns away, and no wire is drawn twice."""
    sheet._index()
    out = list(pts)
    while len(out) >= 2:
        (ax, ay), (bx, by) = out[0], out[1]
        span = math.hypot(bx - ax, by - ay)
        if span <= _EPS:
            out.pop(0)
            continue
        ux, uy = (bx - ax) / span, (by - ay) / span
        reach = 0.0
        if abs(ax - bx) < _EPS:
            for sx, lo, hi, snet in sheet.vsegs:
                if snet == net and abs(sx - ax) < _EPS and lo - _EPS <= ay <= hi + _EPS:
                    reach = max(reach, (hi - ay) if uy > 0 else (ay - lo))
        elif abs(ay - by) < _EPS:
            for sy, lo, hi, snet in sheet.hsegs:
                if snet == net and abs(sy - ay) < _EPS and lo - _EPS <= ax <= hi + _EPS:
                    reach = max(reach, (hi - ax) if ux > 0 else (ax - lo))
        if reach <= _EPS:
            break
        if reach >= span - _EPS:
            out.pop(0)
            continue
        out[0] = (ax + ux * reach, ay + uy * reach)
    return out


def _commit_hat(
    sheet: _Sheet, cand: _HatCand, net: str, gnd: bool, tag: str, lib: str | None = None, value: str | None = None
) -> list[str]:
    _k, s, pts, rot = cand
    hx, hy = pts[-1]
    if tag and not tag.startswith("flag:"):
        sheet.records[tag] = _power_record(s, pts, rot, net)
    drawn = _trim_own(sheet, pts, net)
    out = sheet.add(drawn, net, tag) if len(drawn) > 1 else []
    out.append(_hat(net, hx, hy, gnd=gnd, rot=rot, lib=lib, value=value))
    sheet.occupy(_hat_box(net, hx, hy, gnd, rot, value), "hat", s.part.ref, net, tag)
    sheet.occupants[-1].rot = rot
    sheet.occupants[-1].gnd = gnd and not (tag or "").startswith("flag:")
    sheet.anchor(hx, hy, net, tag)
    sheet.hat_pins.append((hx, hy, tag))
    return out


def _place_stub_label(sheet: _Sheet, s: _Site, net: str, tag: str = "") -> list[str]:
    """Label on a stub off a lone pin, text on either side of the wire; a spot
    that touches nothing drawn wins, else the least overlap. With no clean stub
    at all the label sits on the pin end, which also connects."""
    ox, oy = pin_outward(s.part, s.pin)
    rot, just = _label_pose(ox, oy)
    owner = f"{s.part.ref}.{s.pin.number}"
    best: tuple[tuple[int, float], float, str] | None = None
    for length in (0.0, 2.54, 5.08, 6.35, 7.62, 8.89, 10.16, 12.7, 15.24, 17.78, 20.32):
        ex, ey = s.x + ox * length, s.y + oy * length
        if length and (not sheet.segment_ok(s.x, s.y, ex, ey, net) or not sheet.point_free(ex, ey, net)):
            continue
        stub = _seg_box(s.x, s.y, ex, ey) if length else None
        for vjust in ("bottom",):  # on the wire, never hung beneath it
            box = _label_box(net, ex, ey, rot, just, vjust)
            hard = sheet.hard_cost(box, net) + (sheet.hard_cost(stub, net) if stub else 0.0)
            c = sheet.cost(box, net) + (sheet.cost(stub, net) if stub else 0.0) + 0.02 * length
            c += 0.3 if not length else 0.0  # a little wire between symbol and name reads better
            key = (0 if hard < _CLEAN else 1, c)
            if best is None or key < best[0]:
                best = (key, length, vjust)
    if best is None:
        box = _label_box(net, s.x, s.y, rot, just, "bottom")
        sheet.occupy(box, "label", owner, net, tag)
        sheet.anchor(s.x, s.y, net, tag)
        if tag:
            sheet.records[tag] = {"kind": "label", "net": net, "site": s, "side": _side_of(ox, oy), "stub": 0.0}
        return [_label(net, s.x, s.y, rot, just)]
    _k, length, vjust = best
    ex, ey = s.x + ox * length, s.y + oy * length
    if tag:
        sheet.records[tag] = {"kind": "label", "net": net, "site": s, "side": _side_of(ox, oy), "stub": length}
    out = sheet.add([(s.x, s.y), (ex, ey)], net, tag) if length else []
    out.append(_label(net, ex, ey, rot, just, vjust))
    sheet.occupy(_label_box(net, ex, ey, rot, just, vjust), "label", owner, net, tag)
    sheet.anchor(ex, ey, net, tag)
    return out


def _place_wire_label(sheet: _Sheet, segs: list[Box], net: str, owner: str, tag: str = "", site: "_Site | None" = None) -> list[str]:
    """Label on one of the group's own wires: any point on it connects. The
    text lies along the run - centred when it fits, else flush with an end or
    overhanging - on the side where it touches nothing. Near the middle and
    away from pin ends reads best; text across the wire is a last resort."""
    w = _text_w(net) + 0.4
    best: tuple[tuple[int, float], float, float, int, str, str] | None = None
    for ax, ay, bx, by in segs:
        horiz = abs(by - ay) < _EPS
        length = math.hypot(bx - ax, by - ay)
        lo, hi = (min(ax, bx), max(ax, bx)) if horiz else (min(ay, by), max(ay, by))
        mid = (lo + hi) / 2.0
        rot_along = 0 if horiz else 90
        # (anchor along the run, justify): where the text starts. A rot-0 "left"
        # label runs right from its anchor; a rot-90 "left" label runs up.
        opts: list[tuple[float, str]] = []
        if horiz:
            if length >= w + 1.0:
                opts += [(mid - w / 2.0, "left"), (lo + 0.5, "left"), (hi - w - 0.5, "left")]
            else:
                opts += [(lo + 0.5, "left"), (hi - 0.5, "right")]
        elif length >= w + 1.0:
            opts += [(mid + w / 2.0, "left"), (hi - 0.5, "left"), (lo + w + 0.5, "left")]
        else:
            opts += [(hi - 0.5, "left"), (lo + 0.5, "right")]
        cands = [(a, rot_along, j) for a, j in opts]
        cands.append((mid, 90 - rot_along, "left"))
        for a, rot, just in cands:
            mx, my = (a, ay) if horiz else (ax, a)
            if not (lo + _EPS < a < hi - _EPS) or not sheet.point_free(mx, my, net):
                continue
            # The text sits on the wire (KiCad's "bottom": its base on the line),
            # never hung beneath it - that reads as the row below.
            for vjust in ("bottom",):
                box = _label_box(net, mx, my, rot, just, vjust)
                hard = sheet.hard_cost(box, net)
                c = sheet.cost(box, net)
                c += 0.0 if rot == 0 else 0.3  # upright text reads better
                c += 0.5 if rot != rot_along else 0.0
                centre = (box[0] + box[2]) / 2.0 if horiz else (box[1] + box[3]) / 2.0
                c += 0.05 * abs(centre - mid)
                if any(_point_in_aabb(px, py, box, pad=1.0) for px, py in sheet.pin_ends):
                    c += 0.4  # crowding a pin end is what makes a name hard to read
                key = (0 if hard < _CLEAN else 1, c)
                if best is None or key < best[0]:
                    best = (key, mx, my, rot, just, vjust)
    if best is None:
        return []
    _k, mx, my, rot, just, vjust = best
    mx, my = round(mx, 4), round(my, 4)  # the file's precision: a lock copied from gen lands on the same float
    sheet.occupy(_label_box(net, mx, my, rot, just, vjust), "label", owner, net, tag)
    sheet.anchor(mx, my, net, tag)
    if tag and site is not None:
        sheet.records[tag] = {"kind": "label", "net": net, "site": site, "at": (mx - site.x, my - site.y), "rotate": rot, "justify": just}
    return [_label(net, mx, my, rot, just, vjust)]


_SIDE_VEC = {"left": (-1.0, 0.0), "right": (1.0, 0.0), "top": (0.0, -1.0), "bottom": (0.0, 1.0)}


def _side_of(dx: float, dy: float) -> str:
    if abs(dx) >= abs(dy):
        return "left" if dx < 0 else "right"
    return "top" if dy < 0 else "bottom"


def _power_record(s: "_Site", pts: list, rot: int, net: str) -> dict:
    """A power symbol's placement in SchPower's terms: the stub's side and length, the jog after it, the turn."""
    if len(pts) > 1 and not _near(pts[0][0], pts[0][1], pts[1][0], pts[1][1]):
        dx, dy = pts[1][0] - pts[0][0], pts[1][1] - pts[0][1]
        side, stub = _side_of(dx, dy), math.hypot(dx, dy)
    else:
        side, stub = _side_of(*pin_outward(s.part, s.pin)), 0.0
    jog = (pts[2][1] - pts[1][1]) if len(pts) > 2 else 0.0
    return {"kind": "power", "net": net, "site": s, "side": side, "stub": round(stub, 4), "jog": round(jog, 4), "rotate": int(rot)}


def _lock_pin(part: Part, tok: str) -> PinDef | None:
    named = [p for p in part.pins if p.name == tok]
    if len(named) == 1:
        return named[0]
    return next((p for p in part.pins if p.number == tok), named[0] if named else None)


def _lock_sites(sites: dict[str, list["_Site"]], locks: dict) -> tuple[list, list]:
    """The sites the SchLabel and SchPower locks name, with their specs, in sheet order."""
    by_key = {(s.part.ref, s.pin.number): s for sts in sites.values() for s in sts}
    parts = {s.part.ref: s.part for sts in sites.values() for s in sts}
    out: dict[str, list] = {"label": [], "power": []}
    for kind in ("label", "power"):
        for spec in locks.get(kind) or ():
            ref, tok = spec.pin.split(".", 1)
            part = parts.get(ref)
            pin = _lock_pin(part, tok) if part is not None else None
            site = by_key.get((ref, pin.number)) if pin is not None else None
            if site is None:
                raise ValueError(f"schematic.core.py:{spec.line}: Sch{kind.capitalize()}({spec.net!r}, pin={spec.pin!r}): {spec.pin} is not a bound pin end on the sheet")
            out[kind].append((site, spec))
    return out["label"], out["power"]


def _commit_label_lock(sheet: _Sheet, s: "_Site", spec, tag: str, segs: list, members: list, check: bool = False) -> list[str]:
    """A SchLabel lock, drawn exactly: a stub of `stub` mm leaving the pin to `side` with the label at its end, or the
    label at `at` from the pin end, which must be on the group's wire or one of its pin ends."""
    net = spec.net
    owner = f"{s.part.ref}.{s.pin.number}"
    if spec.stub is not None:
        ox, oy = _SIDE_VEC[spec.side]
        ex, ey = s.x + ox * spec.stub, s.y + oy * spec.stub
        out: list[str] = []
        if spec.stub > 0:
            if check and (not sheet.segment_ok(s.x, s.y, ex, ey, net) or not sheet.point_free(ex, ey, net)):
                raise ValueError(
                    f"schematic.core.py:{spec.line}: SchLabel({net!r}, pin={spec.pin!r}): its {spec.stub:g} mm stub to the {spec.side} "
                    f"from ({s.x:g}, {s.y:g}) meets {sheet.blocker([(s.x, s.y), (ex, ey)], net)}"
                )
            out = sheet.add([(s.x, s.y), (ex, ey)], net, tag)
        rot, just = _label_pose(ox, oy)
        sheet.occupy(_label_box(net, ex, ey, rot, just, "bottom"), "label", owner, net, tag)
        sheet.anchor(ex, ey, net, tag)
        sheet.records[tag] = {"kind": "label", "net": net, "site": s, "side": spec.side, "stub": spec.stub, "line": spec.line}
        out.append(_label(net, ex, ey, rot, just))
        return out
    mx, my = round(s.x + spec.at[0], 4), round(s.y + spec.at[1], 4)
    on_wire = any(_inside_segment(mx, my, ax, ay, bx, by) or _near(mx, my, ax, ay) or _near(mx, my, bx, by) for ax, ay, bx, by in segs)
    on_pin = any(_near(mx, my, m.x, m.y) for m in members)
    if check and not (on_wire or on_pin):
        # What the group is on this sheet, as drawn: its wires and pin ends, and how far the nearest is from the point.
        def dist(ax, ay, bx, by):
            qx, qy = min(max(mx, min(ax, bx)), max(ax, bx)), min(max(my, min(ay, by)), max(ay, by))
            return math.hypot(mx - qx, my - qy)

        near = min([dist(*sg) for sg in segs] + [math.hypot(mx - m.x, my - m.y) for m in members])
        wires = ", ".join(f"({_fmt(ax)}, {_fmt(ay)})-({_fmt(bx)}, {_fmt(by)})" for ax, ay, bx, by in segs[:6]) + (f" and {len(segs) - 6} more" if len(segs) > 6 else "")
        ends = ", ".join(f"{m.part.ref}.{m.pin.number} ({_fmt(m.x)}, {_fmt(m.y)})" for m in members)
        raise ValueError(
            f"schematic.core.py:{spec.line}: SchLabel({net!r}, pin={spec.pin!r}, at=({spec.at[0]:g}, {spec.at[1]:g})): ({mx:g}, {my:g}) is on no "
            f"wire of {spec.pin}'s group and on none of its pin ends, so the label would connect nothing; on this sheet the group's "
            f"wires are {wires or 'none'} and its pin ends {ends}, the nearest {near:.2f} mm from ({mx:g}, {my:g})"
        )
    sheet.occupy(_label_box(net, mx, my, spec.rotate, spec.justify, "bottom"), "label", owner, net, tag)
    sheet.anchor(mx, my, net, tag)
    sheet.records[tag] = {"kind": "label", "net": net, "site": s, "at": spec.at, "rotate": spec.rotate, "justify": spec.justify, "line": spec.line}
    return [_label(net, mx, my, spec.rotate, spec.justify, "bottom")]


def _commit_power_lock(sheet: _Sheet, s: "_Site", spec, net: str, gnd: bool, tag: str, check: bool = False) -> list[str]:
    ox, oy = _SIDE_VEC[spec.side]
    pts = [(s.x, s.y)]
    if spec.stub > 0:
        pts.append((s.x + ox * spec.stub, s.y + oy * spec.stub))
    if spec.jog:
        last = pts[-1]
        pts.append((last[0], last[1] + spec.jog))
    if check and len(pts) > 1 and not sheet.path_ok(_trim_own(sheet, pts, net), net):
        raise ValueError(
            f"schematic.core.py:{spec.line}: SchPower({net!r}, pin={spec.pin!r}): its stub ({spec.side} {spec.stub:g} mm, jog {spec.jog:g} mm) "
            f"from ({s.x:g}, {s.y:g}) meets {sheet.blocker(pts, net)}"
        )
    out = _commit_hat(sheet, ((0, 0.0), s, pts, spec.rotate), net, gnd, tag)
    sheet.records[tag]["line"] = spec.line
    return out


def junction_points(sheet: _Sheet) -> list[tuple[float, float]]:
    """Where `_junctions` puts a dot: a wire ending on another wire of its own net (a symbol's stub leaving a route), or
    three or more ways meeting at a point, each wire end and each symbol pin ending there one way: three wire ends, a
    part's pin with two wires, and a power symbol's pin as a part's (refute:sheet 2026-10-09: a T whose third arm was a
    power symbol was drawn without a dot on seven sheets, where every T with a part pin had one). Same-net only by
    construction - other nets' wires never touch."""
    sheet._index()
    ends: dict[tuple[int, int], tuple[float, float, int]] = {}
    for x0, y0, x1, y1, _net in sheet.segs:
        for x, y in ((x0, y0), (x1, y1)):
            k = (_bkey(x), _bkey(y))
            px, py, n = ends.get(k, (x, y, 0))
            ends[k] = (px, py, n + 1)
    out: list[tuple[float, float]] = []
    for (px, py, n) in ends.values():
        if n + sheet.pins_at(px, py) >= 3 or sheet.on_wire_interior(px, py):
            out.append((px, py))
    return out


def _junctions(sheet: _Sheet) -> list[SheetJunction]:
    """Junction dots where eeschema would put them (`junction_points`)."""
    return [SheetJunction((_q4(px), _q4(py)), uuid=_uid(f"junction:{_fmt(px)},{_fmt(py)}")) for px, py in junction_points(sheet)]


def wire_overlaps(sheet: _Sheet, parts: list[Part]) -> list[str]:
    """Two wires on one stretch (collinear, sharing more than a point), and a junction dot where fewer than three ways
    meet (wires leaving the point, a pin ending there): drawn, each reads as a branch that is not one (refute:sheet
    2026-10-08: c3_usb's GND stub over U1's bus bar, a dot on U1.11's bare end). As facts."""
    out: list[str] = []
    segs = sheet.segs
    for i, (ax0, ay0, ax1, ay1, an) in enumerate(segs):
        for bx0, by0, bx1, by1, bn in segs[i + 1 :]:
            if abs(ax0 - ax1) < _EPS and abs(bx0 - bx1) < _EPS and abs(ax0 - bx0) < _EPS:
                shared = min(max(ay0, ay1), max(by0, by1)) - max(min(ay0, ay1), min(by0, by1))
            elif abs(ay0 - ay1) < _EPS and abs(by0 - by1) < _EPS and abs(ay0 - by0) < _EPS:
                shared = min(max(ax0, ax1), max(bx0, bx1)) - max(min(ax0, ax1), min(bx0, bx1))
            else:
                continue
            if shared > _EPS:
                out.append(f"wire {an} ({_fmt(ax0)}, {_fmt(ay0)})-({_fmt(ax1)}, {_fmt(ay1)}) runs along wire {bn} ({_fmt(bx0)}, {_fmt(by0)})-({_fmt(bx1)}, {_fmt(by1)}) for {shared:.2f} mm")
    pins = [(pin_world(p, pin), pin_outward(p, pin), f"{p.ref}.{pin.number}") for p in parts for pin in p.pins]
    for px, py in junction_points(sheet):
        ways: set[tuple[int, int]] = set()
        for x0, y0, x1, y1, _n in segs:
            for (ex, ey), (fx, fy) in (((x0, y0), (x1, y1)), ((x1, y1), (x0, y0))):
                if _near(ex, ey, px, py) and not _near(fx, fy, px, py):
                    ways.add((int(round(math.copysign(1, fx - ex))) if abs(fx - ex) > _EPS else 0, int(round(math.copysign(1, fy - ey))) if abs(fy - ey) > _EPS else 0))
            if _inside_segment(px, py, x0, y0, x1, y1):
                for fx, fy in ((x0, y0), (x1, y1)):
                    ways.add((int(round(math.copysign(1, fx - px))) if abs(fx - px) > _EPS else 0, int(round(math.copysign(1, fy - py))) if abs(fy - py) > _EPS else 0))
        named = []
        for (ex, ey), (ox, oy), name in pins:
            if _near(ex, ey, px, py):
                ways.add((-int(round(ox)), -int(round(oy))))
                named.append(name)
        for hx, hy, tag in sheet.hat_pins:
            if _near(hx, hy, px, py):
                ways.add(("symbol", tag))  # the power symbol's body, which no wire runs into
        if len(ways) < 3:
            out.append(f"junction dot at ({_fmt(px)}, {_fmt(py)}) where {len(ways)} ways meet" + (f" (the end of {', '.join(named)})" if named else ""))
    return out


def _joint_hats(sheet: _Sheet, a: tuple, b: tuple, drawn: dict[str, list[str]]) -> None:
    """Place two power symbols together: the pair whose spots are cleanest, and
    among clean pairs the least overlap. Only better than what each had alone
    is kept. Labels within reach are lifted first - they are re-placed around
    the symbols in the pass that follows, so they should not decide this."""
    tag_a, mem_a, net_a, gnd_a = a
    tag_b, mem_b, net_b, gnd_b = b
    sites = list(mem_a) + list(mem_b)
    parts = {pt.ref: pt for pt in sheet.parts}
    for o in list(sheet.occupants):
        if o.kind == "label" and o.tag:
            cx, cy = (o.box[0] + o.box[2]) / 2.0, (o.box[1] + o.box[3]) / 2.0
            if any(math.hypot(cx - st.x, cy - st.y) <= 12.7 for st in sites):
                sheet.remove(o.tag)
                # What the label will need at the least once it is put back (a short stub off its pin and its name),
                # pencilled in: the pair is chosen around it (DS2's AVSS symbol over the AIN3 label below it).
                ref, _, num = o.owner.partition(".")
                pt = parts.get(ref)
                pin = next((pn for pn in pt.pins if pn.number == num), None) if pt is not None else None
                if pin is not None:
                    px, py = pin_world(pt, pin)
                    ox, oy = pin_outward(pt, pin)
                    rot, just = _label_pose(ox, oy)
                    ex, ey = px + ox * 2.54, py + oy * 2.54
                    sheet.occupy(_label_box(o.net, ex, ey, rot, just, "bottom"), "reserve", o.owner, "", "liftres")
                    sheet.occupy(_seg_box(px, py, ex, ey), "reserve", o.owner, "", "liftres")
    before = _pair_key(sheet, a, b)
    if before is not None and before[0] == 0:
        # Both already stand clean: the joint search (48 partner enumerations) could only trim overlap. Each is
        # re-chosen alone with the labels lifted, as when the search finds nothing better.
        sheet.remove(tag_a)
        sheet.remove(tag_b)
        sheet.remove("liftres")
        drawn[tag_a] = _place_hat(sheet, mem_a, net_a, gnd_a, tag_a)
        drawn[tag_b] = _place_hat(sheet, mem_b, net_b, gnd_b, tag_b)
        return
    sheet.remove(tag_a)
    sheet.remove(tag_b)
    best = None
    # The partner is enumerated afresh for each of our candidates: its best
    # spot next to us (a supply hung below its pin) is rarely among its best
    # spots alone, so a fixed shortlist would miss the pair that works.
    for ca in _hat_candidates(sheet, mem_a, net_a, gnd_a)[:48]:
        _commit_hat(sheet, ca, net_a, gnd_a, tag_a)
        cbs = _hat_candidates(sheet, mem_b, net_b, gnd_b)
        sheet.remove(tag_a)
        if not cbs:
            continue
        cb = cbs[0]
        joint = (ca[0][0] + cb[0][0], ca[0][1] + cb[0][1])
        if best is None or joint < best[0]:
            best = (joint, ca)
    sheet.remove("liftres")
    if best is not None and (before is None or best[0] < before):
        drawn[tag_a] = _commit_hat(sheet, best[1], net_a, gnd_a, tag_a)
        drawn[tag_b] = _place_hat(sheet, mem_b, net_b, gnd_b, tag_b)
    else:
        drawn[tag_a] = _place_hat(sheet, mem_a, net_a, gnd_a, tag_a)
        drawn[tag_b] = _place_hat(sheet, mem_b, net_b, gnd_b, tag_b)


def _pair_key(sheet: _Sheet, a: tuple, b: tuple) -> tuple[int, float] | None:
    """How the two symbols score where they are now, each judged the way a
    candidate is (with the other in place, flip and stub penalties included)."""
    total = [0, 0.0]
    for tag, members, net, gnd in (a, b):
        occ = [o for o in sheet.occupants if o.tag == tag]
        if not occ:
            return None
        mine = [sheet.segs[i][:4] for i, t in enumerate(sheet.seg_tags) if t == tag]
        rot = getattr(occ[0], "rot", None)
        sheet.remove(tag)
        try:
            match = None
            for cand in _hat_candidates(sheet, members, net, gnd):
                segs = [(ax, ay, bx, by) for (ax, ay), (bx, by) in zip(cand[2], cand[2][1:]) if not _near(ax, ay, bx, by)]
                if cand[3] == rot and len(segs) == len(mine) and all(
                    _near(p[0], p[1], q[0], q[1]) and _near(p[2], p[3], q[2], q[3]) for p, q in zip(segs, mine)
                ):
                    match = cand
                    break
        finally:
            if match is not None:
                _commit_hat(sheet, match, net, gnd, tag)
        if match is None:
            return None  # not a legal spot any more: anything the pair finds is better
        total[0] += match[0][0]
        total[1] += match[0][1]
    return (total[0], total[1])


_SWAP = {"left": "right", "right": "left", None: None}


def _field_just(just: str | None, vis: int, rot: float = 0.0, mirror: str | None = None) -> str | None:
    """The justification KiCad draws a field with, for a field written by `_instance` (angle (vis - rot) % 180) on a
    symbol at `rot` and `mirror`. KiCad keeps the text readable and swaps the justification where the symbol's
    transform turns the text's run around: horizontal text on a symbol at 90 or 180 (unless mirrored "y", which
    turns it back) or at 0/270 mirrored "y"; vertical text on a symbol at 180 or 270 (unless mirrored "x") or at 0/90
    mirrored "x". Measured on `kicad-cli sch export svg` for every rotation, mirror, justification and field angle
    (docs/schematic-layout.md §2.6, the field-model fix; tests/test_sch_score.py pins it)."""
    r = int(round(rot)) % 360
    if vis == 90:
        flip = (r in (180, 270)) != (mirror == "x")
    else:
        flip = (r in (90, 180)) != (mirror == "y")
    return _SWAP[just] if flip else just


def _file_spot(part: Part, spot: tuple) -> tuple:
    """A text spot as it is drawn (x, y, justification as seen, angle as seen) → as `_instance` writes it: the
    justification KiCad will swap back (`_field_just` is its own inverse)."""
    x, y, just, vis = spot
    return (x, y, _field_just(just, vis, part.rot, part.mirror), vis)


def _text_box(text: str, x: float, y: float, just: str | None, vis: int) -> Box:
    """Where a field's text lands as it is seen: horizontal from its anchor, or vertical (running up from it when
    justified left, down when right, centred when neither). `just` is the justification as drawn (`_file_spot`)."""
    if vis == 90:
        w = _field_w(text) + 0.4
        if just == "left":
            return (x - _TEXT_H / 2.0, y - w, x + _TEXT_H / 2.0, y)
        if just == "right":
            return (x - _TEXT_H / 2.0, y, x + _TEXT_H / 2.0, y + w)
        return (x - _TEXT_H / 2.0, y - w / 2.0, x + _TEXT_H / 2.0, y + w / 2.0)
    return _prop_box(text, x, y, just, _field_w)


def _text_lock_spots(part: Part, locks: dict | None) -> tuple:
    """The SchText locks of this part as text spots (x, y, justification as seen, angle as seen), or None per field."""
    out = []
    for fld in ("Reference", "Value"):
        t = (locks or {}).get((part.ref, fld))
        out.append(None if t is None else (round(part.x + t.at[0], 4), round(part.y + t.at[1], 4), t.justify, t.rotate))
    part.hide_ref = bool((locks or {}).get((part.ref, "Reference")) and locks[(part.ref, "Reference")].hide)
    part.hide_val = bool((locks or {}).get((part.ref, "Value")) and locks[(part.ref, "Value")].hide)
    return tuple(out)


def _with_locks(cands: list, lr, lv) -> list:
    """Candidate (name, ref spot, value spot) list with the locked spots in place; one candidate when both are locked."""
    if lr is not None and lv is not None:
        return [("locked", lr, lv)]
    if lr is None and lv is None:
        return cands
    out, seen = [], set()
    for name, r, v in cands:
        c = (name, lr or r, lv or v)
        if (c[1], c[2]) not in seen:
            seen.add((c[1], c[2]))
            out.append(c)
    return out


def _occupy_text(sheet: _Sheet, part: Part, r, v) -> None:
    r = (round(r[0], 4), round(r[1], 4), r[2], r[3])
    v = (round(v[0], 4), round(v[1], 4), v[2], v[3])
    part.text_seen = (r, v)
    part.text_ref = _file_spot(part, r)
    part.text_val = _file_spot(part, v)
    if not getattr(part, "hide_ref", False):
        sheet.occupy(_text_box(part.ref, *r), "ref", part.ref)
    if not getattr(part, "hide_val", False):
        sheet.occupy(_text_box(part.display, *v), "value", part.ref)


def _place_box_text(sheet: _Sheet, part: Part, locks: dict | None = None) -> None:
    """An IC's Reference and Value: where the library puts them when that is
    clear, else the least crowded of a few spots around the body. A SchText lock is where it says."""
    sheet.occupants = [o for o in sheet.occupants if not (o.owner == part.ref and o.kind in ("ref", "value"))]
    lr, lv = _text_lock_spots(part, locks)
    rx, ry = _prop_world(part, part.prop_ref)
    vx, vy = _prop_world(part, part.prop_val)
    x0, y0, x1, y1 = core_aabb(part)
    cx = (x0 + x1) / 2.0
    ref, val = part.ref, part.display
    Spot = tuple[float, float, str | None, int]
    cands: list[tuple[str, Spot, Spot]] = [
        ("library", (rx, ry, None, 0), (vx, vy, None, 0)),
        ("above-left", (x0, y0 - 2.7, "right", 0), (x0, y0 - 1.0, "right", 0)),
        ("above-right", (x1, y0 - 2.7, "left", 0), (x1, y0 - 1.0, "left", 0)),
        ("below-left", (x0, y1 + 1.0, "right", 0), (x0, y1 + 2.7, "right", 0)),
        ("below-right", (x1, y1 + 1.0, "left", 0), (x1, y1 + 2.7, "left", 0)),
        ("above", (cx, y0 - 2.7, None, 0), (cx, y0 - 1.0, None, 0)),
        ("below", (cx, y1 + 1.0, None, 0), (cx, y1 + 2.7, None, 0)),
    ]
    cands = _with_locks(cands, lr, lv)
    best = None
    for i, (_name, r, v) in enumerate(cands):
        rb, vb = _text_box(ref, *r), _text_box(val, *v)
        crossed = _name_blocked(sheet, rb, ref) or _name_blocked(sheet, vb, ref)
        c = sheet.cost(rb) + sheet.cost(vb) + 0.05 * i
        key = (1 if crossed else 0, c)
        if best is None or key < best[0]:
            best = (key, r, v)
    assert best is not None
    _c, r, v = best
    _occupy_text(sheet, part, r, v)


def cut_off(segs, box: Box, body: Box):
    """The first wire (x0, y0, x1, y1, net) that runs between a text box and a body: across the shortest line from
    the text to the body, which then reads as a different part's name (refute:sheet 2026-10-09: DS2's R4 under the
    ADC_DRDY wire read "R4 47R | R5 47R"; buck's R_EN on the far side of the 5V wire). None when the text touches the
    body or no wire runs between."""
    bx0, by0, bx1, by1 = body
    tx, ty = (box[0] + box[2]) / 2.0, (box[1] + box[3]) / 2.0
    px, py = min(max(tx, bx0), bx1), min(max(ty, by0), by1)  # the body's point nearest the text
    qx, qy = min(max(px, box[0]), box[2]), min(max(py, box[1]), box[3])  # the text's point nearest that
    if abs(px - qx) < _EPS and abs(py - qy) < _EPS:
        return None
    for seg in segs:
        x0, y0, x1, y1 = seg[:4]
        if abs(y0 - y1) < _EPS:  # horizontal wire
            lo, hi = min(py, qy), max(py, qy)
            if not (lo + _EPS < y0 < hi - _EPS):
                continue
            t = (y0 - qy) / (py - qy)
            x = qx + t * (px - qx)
            if min(x0, x1) - _EPS <= x <= max(x0, x1) + _EPS:
                return seg
        elif abs(x0 - x1) < _EPS:  # vertical wire
            lo, hi = min(px, qx), max(px, qx)
            if not (lo + _EPS < x0 < hi - _EPS):
                continue
            t = (x0 - qx) / (px - qx)
            y = qy + t * (py - qy)
            if min(y0, y1) - _EPS <= y <= max(y0, y1) + _EPS:
                return seg
    return None


def _place_passive_text(sheet: _Sheet, part: Part, locks: dict | None = None) -> None:
    """Reference and Value beside a 2-pin part on the side that overlaps least.
    Beside a part that stands vertically they may also stand vertically -
    how an engineer labels a hanging resistor when the row beside it is busy. A SchText lock is where it says."""
    sheet.occupants = [o for o in sheet.occupants if not (o.owner == part.ref and o.kind in ("ref", "value"))]
    lr, lv = _text_lock_spots(part, locks)
    x0, y0, x1, y1 = body_aabb(part)
    cx, cy = (x0 + x1) / 2.0, (y0 + y1) / 2.0
    ref, val = part.ref, part.display
    Spot = tuple[float, float, str | None, int]
    cands: list[tuple[str, Spot, Spot]] = [
        ("right", (x1 + 0.8, cy - 1.0, "left", 0), (x1 + 0.8, cy + 1.0, "left", 0)),
        ("left", (x0 - 0.8, cy - 1.0, "right", 0), (x0 - 0.8, cy + 1.0, "right", 0)),
        ("above", (cx, y0 - 2.7, None, 0), (cx, y0 - 1.0, None, 0)),
        ("below", (cx, y1 + 1.0, None, 0), (cx, y1 + 2.7, None, 0)),
        # Both lines tucked against the body. The normal stack's second line
        # falls on the next pin's wire (R_EN's 100k on FB).
        ("below-tight", (cx, y1 - 0.2, None, 0), (cx, y1 + 1.4, None, 0)),
        ("above-tight", (cx, y0 - 1.4, None, 0), (cx, y0 + 0.2, None, 0)),
        ("above-left", (x1, y0 - 2.7, "right", 0), (x1, y0 - 1.0, "right", 0)),
        ("above-right", (x0, y0 - 2.7, "left", 0), (x0, y0 - 1.0, "left", 0)),
        ("below-left", (x1, y1 + 1.0, "right", 0), (x1, y1 + 2.7, "right", 0)),
        ("below-right", (x0, y1 + 1.0, "left", 0), (x0, y1 + 2.7, "left", 0)),
        # Slid along a side: the two lines sit toward one end of the part.
        ("right-high", (x1 + 0.8, cy - 2.7, "left", 0), (x1 + 0.8, cy - 1.0, "left", 0)),
        ("right-low", (x1 + 0.8, cy + 1.0, "left", 0), (x1 + 0.8, cy + 2.7, "left", 0)),
        ("left-high", (x0 - 0.8, cy - 2.7, "right", 0), (x0 - 0.8, cy - 1.0, "right", 0)),
        ("left-low", (x0 - 0.8, cy + 1.0, "right", 0), (x0 - 0.8, cy + 2.7, "right", 0)),
    ]
    vertical = abs(pin_outward(part, part.pins[0])[1]) > 0.5 if part.pins else True
    if vertical:
        # Rotated text runs up from its anchor; anchored at body-top + width it
        # spans the body and spills below it, never above the top pin, where
        # the part's node and rail are.
        wr, wv = _text_w(ref) + 0.4, _text_w(val) + 0.4
        cands += [
            ("right-rot", (x1 + 1.2, y0 + wr, "left", 90), (x1 + 3.0, y0 + wv, "left", 90)),
            ("left-rot", (x0 - 3.0, y0 + wr, "left", 90), (x0 - 1.2, y0 + wv, "left", 90)),
        ]
        # One row, name then value. Two stacked lines are taller than the
        # gap between adjacent pin wires, so they cannot both fit there.
        gap = _text_w(ref) + 0.6
        for k in range(-3, 4):
            y = cy + k * 1.27
            cands.append(("row-right", (x1 + 0.6, y, "left", 0), (x1 + 0.6 + gap, y, "left", 0)))
            cands.append(("row-left", (x0 - 0.6, y, "right", 0), (x0 - 0.6 - gap, y, "right", 0)))
    order = cands if vertical else cands[2:4] + cands[:2] + cands[4:]
    order = _with_locks(order, lr, lv)
    body = (x0, y0, x1, y1)
    best = None
    clear = None
    for i, (side, r, v) in enumerate(order):
        rb, vb = _text_box(ref, *r), _text_box(val, *v)
        # A wire through the name is a miss, not a small cost. The clear side
        # wins even when it sits nearer the part's own stub.
        crossed = _name_blocked(sheet, rb, ref) or _name_blocked(sheet, vb, ref)
        c = sheet.cost(rb) + sheet.cost(vb) + 0.01 * i
        c += 0.4 if side.endswith("-rot") else 0.0  # upright reads better when it fits
        key = (1 if crossed else 0, c)
        if _overlap_area(rb, vb) > 0.2 and len(order) > 1:
            continue
        # A wire between the name and its body makes it read as the next part's: such a spot gives way to the
        # cheapest one that is clear of wires and of everything drawn (refute:sheet 2026-10-09: DS2's R4 below the
        # ADC_DRDY wire, buck's R_EN beyond the 5V wire).
        cut = cut_off(sheet.segs, rb, body) is not None or cut_off(sheet.segs, vb, body) is not None
        clean = not crossed and not cut and sheet.hard_cost(rb) + sheet.hard_cost(vb) < 1e-9
        if best is None or key < best[0]:
            best = (key, r, v, cut)
        if clean and (clear is None or key < clear[0]):
            clear = (key, r, v, cut)
    if best is not None and best[3] and clear is not None and (lr is None and lv is None):
        best = clear
    if best is None:  # a locked spot pair that overlaps itself: kept, as written
        best = ((1, 0.0), order[0][1], order[0][2], False)
    if best[0][0] and (lr is None and lv is None):
        # The ranked spots are all crossed. Nudge the least-crossed pair by a
        # grid step or two; the name moves, the part stays.
        _k, r0, v0, _cut = best
        found = None
        for dist in (1.27, 2.54, 3.81):
            for dx, dy in ((dist, 0), (-dist, 0), (0, dist), (0, -dist)):
                rr = (r0[0] + dx, r0[1] + dy, r0[2], r0[3])
                vv = (v0[0] + dx, v0[1] + dy, v0[2], v0[3])
                rb, vb = _text_box(ref, *rr), _text_box(val, *vv)
                if _overlap_area(rb, vb) > 0.2:
                    continue
                if not _name_blocked(sheet, rb, ref) and not _name_blocked(sheet, vb, ref):
                    found = (rr, vv)
                    break
            if found:
                break
        if found:
            best = (best[0], found[0], found[1], False)
    _c, r, v, _cut = best
    _occupy_text(sheet, part, r, v)


def _lint(
    sheet: _Sheet,
    parts: list[Part],
    sites: dict[str, list[_Site]],
    unions: dict[str, _Union],
    power_nets: set[str],
    ground_nets: set[str] = frozenset(),
    kinds: dict[str, str] | None = None,
    wire_limits: dict[str, float] | None = None,
    wire_default: float = 25.4,
) -> dict:
    """What still hurts readability, as things an AI can act on by moving parts."""
    issues: list[str] = []
    _HANGERS.clear()
    for p in parts:
        ref = getattr(p, "attach_ref", None)
        if ref:
            _HANGERS.setdefault(ref, []).append(p.ref)
    # A pull-up ends up, a part to ground ends down: a standing 2-pin part
    # with its supply end below or its ground end above was pushed the wrong
    # way by whatever took the right spot.
    for p in parts:
        if not _two_pin(p) or len(p.pins) != 2:
            continue
        (ax, ay), (bx, by) = pin_world(p, p.pins[0]), pin_world(p, p.pins[1])
        if abs(ax - bx) > _EPS:
            continue  # lying: no up or down to get wrong
        for pin, y_self, y_other in ((p.pins[0], ay, by), (p.pins[1], by, ay)):
            kind = (kinds or {}).get(pin.net or "", "net")
            if kind == "power" and y_self > y_other + _EPS:
                issues.append(
                    f"{p.ref} stands the wrong way up: its {pin.net} end is at the bottom - "
                    f"nothing was clear above it"
                )
            if kind == "ground" and y_self < y_other - _EPS:
                issues.append(
                    f"{p.ref} stands the wrong way up: its {pin.net} end is at the top - "
                    f"nothing was clear below it"
                )
    for p in parts:
        shoved = getattr(p, "shoved_mm", 0.0)
        if shoved > 10 * 1.27:  # a stagger of a few grid steps is normal; more is a lane taken
            issues.append(
                f"{p.ref} was pushed {shoved:.0f} mm along its attach to clear {getattr(p, 'shoved_by', '?')}: "
                f"they want the same side of the same part"
            )
        for other in sorted(getattr(p, "stuck_on", set())):
            issues.append(
                f"{p.ref} overlaps {other} at every distance along its attach"
            )
        wire = getattr(p, "stuck_wire", None)
        if wire and wire[1]:
            issues.append(
                f"{p.ref}: its wire from {wire[0]} would run through {', '.join(wire[1])} at every distance along its attach"
            )
    for p in parts:
        if p.kind != "box":
            continue
        wide = [pin.number for pin in p.pins if _text_w(pin.number) > pin.length + 0.5]
        if wide:
            shown = ", ".join(wide[:4]) + (", ..." if len(wide) > 4 else "")
            issues.append(
                f"{p.ref}: pin numbers {shown} are wider than their {p.pins[0].length:g} mm pins "
                f"and print into the body; that is the .kicad_sym, which no placement changes"
            )
    for i, a in enumerate(parts):
        for b in parts[i + 1 :]:
            if _overlap_area(body_aabb(a), body_aabb(b)) > 0.0:
                issues.append(f"{a.ref} overlaps {b.ref}")
    text_kinds = {"label", "hat", "ref", "value"}
    occ = sheet.occupants
    # One line per placement that lost, listing everything it hit: the AI
    # acts on the cause, not on five symptoms of it.
    lost: dict[tuple[str, str, str], tuple[_Occupant, _Occupant, list[str]]] = {}
    for i, a in enumerate(occ):
        for b in occ[i + 1 :]:
            if a.kind not in text_kinds and b.kind not in text_kinds:
                continue
            if a.owner == b.owner and {a.kind, b.kind} == {"ref", "value"}:
                continue
            area = _overlap_area(a.box, b.box)
            if area < 0.6:
                continue
            loser, partner = _loser(a, b)
            key = (loser.kind, loser.owner, loser.net)
            entry = lost.setdefault(key, (loser, partner, []))
            what = _describe(partner)
            if what not in entry[2]:
                entry[2].append(what)
    for loser, partner, hits in lost.values():
        issues.append(f"{_describe(loser)} overlaps {', '.join(hits)}{_move_hint(loser, partner)}")
    # Style notes: legal drawings a person might still tidy. Not counted.
    notes: list[str] = []
    for o in occ:
        if o.kind == "hat" and getattr(o, "rot", 0) == 180 and not (o.tag or "").startswith("flag:"):
            way = "up" if o.net in ground_nets else "down"
            notes.append(f"the {o.net} symbol at {o.owner} points {way}: nothing was clear the right way up")
    # A 2-pin part with its own power symbol next to a same-net pin: hang it off
    # that pin instead and the wire carries the net - one symbol fewer, no clash.
    by_ref = {p.ref: p for p in parts}
    for o in occ:
        if o.kind != "hat" or not o.tag or o.net in ground_nets:
            continue  # ground is always symbols; only a supply can share a node by wire
        ref = o.owner
        part = by_ref.get(ref)
        if part is None or not _two_pin(part):
            continue
        sts = sites.get(o.net, [])
        uf = unions[o.net]
        idx = {id(s): i for i, s in enumerate(sts)}
        mine = [s for s in sts if s.part is part]
        for m in mine:
            near = [
                (math.hypot(m.x - s.x, m.y - s.y), s)
                for s in sts
                if s.part is not part
                and uf.find(idx[id(s)]) != uf.find(idx[id(m)])  # not already wired together
                and math.hypot(m.x - s.x, m.y - s.y) <= 15.24
                and (abs(m.x - s.x) < _EPS or abs(m.y - s.y) < _EPS)
            ]
            if near:
                d, s = min(near, key=lambda t: t[0])
                issues.append(
                    f"{o.net}: {ref} carries its own {o.net} symbol though {s.part.ref}.{s.pin.name} is in line "
                    f"{d:.0f} mm away and not wired to it"
                )
                break
    through: dict[tuple[str, str, str], tuple[_Occupant, list[str]]] = {}
    for x0, y0, x1, y1, net in sheet.segs:
        for o in occ:
            text_hit = o.kind in text_kinds | {"pin"} and o.net != net and _overlap_area(_seg_box(x0, y0, x1, y1), o.box) > 0.6
            # A power symbol's own wire through its graphic is the same net, so the
            # text test above never sees the flag stacked behind the VCC arrow.
            symbol_hit = o.kind == "hat" and _seg_through_box(x0, y0, x1, y1, _graphic_box(o))
            if text_hit or symbol_hit:
                entry = through.setdefault((o.kind, o.owner, o.net), (o, []))
                if net not in entry[1]:
                    entry[1].append(net)
    for o, nets in through.values():
        issues.append(f"wire{'s' if len(nets) > 1 else ''} {', '.join(nets)} run{'' if len(nets) > 1 else 's'} through {_describe(o)}")
    blocked: dict[str, str] = {}
    for net, sts in sites.items():
        if net in power_nets:
            continue  # power nets are joined by symbols on purpose
        uf = unions[net]
        groups = uf.groups()
        if len(groups) < 2:
            continue
        for gi, ga in enumerate(groups):
            for gb in groups[gi + 1 :]:
                pairs = [
                    (math.hypot(sts[i].x - sts[j].x, sts[i].y - sts[j].y), i, j)
                    for i in ga
                    for j in gb
                    if sts[i].part is not sts[j].part
                ]
                if not pairs:
                    continue
                d, i, j = min(pairs)
                a, b = sts[i], sts[j]
                in_line = abs(a.x - b.x) < _EPS or abs(a.y - b.y) < _EPS
                if in_line and d <= (wire_limits or {}).get(net, wire_default) + _EPS:
                    # What stands on the straight run (a body or pin of a third part, touching counts): the loop's repair.
                    lx0, lx1 = sorted((a.x, b.x))
                    ly0, ly1 = sorted((a.y, b.y))
                    for o in sheet.occupants:
                        if o.kind not in ("symbol", "pin") or not o.owner:
                            continue
                        ref = o.owner.partition(".")[0]
                        if ref in (a.part.ref, b.part.ref):
                            continue
                        bx0, by0, bx1, by1 = o.box
                        if bx0 <= lx1 + 0.01 and bx1 >= lx0 - 0.01 and by0 <= ly1 + 0.01 and by1 >= ly0 - 0.01:
                            blocked.setdefault(ref, net)
                if in_line and d < 20.0:
                    issues.append(
                        f"{net}: {a.part.ref}.{a.pin.name} and {b.part.ref}.{b.pin.name} are in line "
                        f"{d:.0f} mm apart but joined by labels: something sits between them"
                    )
    # A net name that does not fit on its wire: push the downstream part one
    # grid step out along the pin it hangs from. One step per pass.
    pushes: dict[str, float] = {}
    cut_pushes: dict[str, float] = {}
    # A two-pin part's name with a wire between it and its body reads as the next part's (refute:sheet 2026-10-09:
    # DS2's R4 under ADC_DRDY, buck's R_EN beyond 5V): every spot beside it was crossed. The part slides a grid step
    # out along its attach, for room.
    for p in parts:
        if not _two_pin(p):
            continue
        body = body_aabb(p)
        for o in occ:
            if o.owner != p.ref or o.kind not in ("ref", "value"):
                continue
            seg = cut_off(sheet.segs, o.box, body)
            if seg is not None:
                issues.append(f"{p.ref}'s {'Reference' if o.kind == 'ref' else 'Value'} is cut off from {p.ref} by wire {seg[4]}: no spot beside it was clear")
                # Room is made by moving the part out along its attach, and the part the wire is drawn for (its symbol's
                # or label's stub, or the pins it ends on).
                tag = sheet.seg_tags[sheet.segs.index(seg)]
                owners = {p.ref}
                if tag.count(":") >= 2:
                    owners.add(tag.split(":", 2)[2].split(".", 1)[0])
                for q in parts:
                    if any(_near(*pin_world(q, pn), seg[0], seg[1]) or _near(*pin_world(q, pn), seg[2], seg[3]) for pn in q.pins):
                        owners.add(q.ref)
                for r in sorted(owners):
                    cut_pushes[r] = 2.54
                break
    longest: dict[str, float] = {}
    for x0, y0, x1, y1, net in sheet.segs:
        if net in power_nets:
            continue
        longest[net] = max(longest.get(net, 0.0), math.hypot(x1 - x0, y1 - y0))
    for net, length in longest.items():
        if length + 0.05 >= _text_w(net) + 1.0:
            continue
        on = {p.ref for p in parts if any(getattr(pin, "net", "") == net for pin in p.pins)}
        for p in parts:
            if getattr(p, "attach_ref", None) in on and p.ref in on and getattr(p, "kind", "") in ("r", "c", "l", "d"):
                pushes[p.ref] = 2.54
    return {"issues": issues, "count": len(issues), "notes": notes, "pushes": pushes, "cut_pushes": cut_pushes, "blocked": sorted(blocked)}


_HANGERS: dict[str, list[str]] = {}
_LOSES = {"ref": 0, "value": 0, "label": 1, "hat": 2}


def _loser(a: _Occupant, b: _Occupant) -> tuple[_Occupant, _Occupant]:
    """Of two things that overlap, the one that was placed and lost: text
    before a label before a power symbol, before anything fixed."""
    ra, rb = _LOSES.get(a.kind, 9), _LOSES.get(b.kind, 9)
    return (a, b) if ra <= rb else (b, a)


def _move_hint(a: _Occupant, b: _Occupant) -> str:
    """What to change in board.py for this pair."""
    for t, other in ((a, b), (b, a)):
        if t.kind in ("ref", "value"):
            return f" - no clear spot for {t.owner}'s name"
    for t, other in ((a, b), (b, a)):
        if t.kind == "hat":
            hangers = _HANGERS.get(t.owner, [])
            if hangers:
                # What hangs off the part, as a fact; what blocks each spot is the overlap this line opens with (a
                # part posed by a lock may be the one in the way: refute:split 2026-10-08, U1 posed over R_EN).
                return f" - no clear spot for the {t.net} symbol at {t.owner}; {', '.join(hangers)} hang off {t.owner} at the default gap"
            return f" - no clear spot for the {t.net} symbol at {t.owner}"
    for t, other in ((a, b), (b, a)):
        if t.kind == "label":
            return f" - the parts on {t.net} leave its label no clear spot"
    return ""


def _describe(o: _Occupant) -> str:
    if o.kind == "symbol":
        return f"symbol {o.owner}"
    if o.kind == "pintext":
        return f"pin number of {o.owner}"
    if o.kind == "pin":
        return f"pin {o.owner}"
    if o.kind == "label":
        return f"label {o.net} at {o.owner}"
    if o.kind == "hat":
        return f"{o.net} symbol at {o.owner}"
    return f"{o.owner} {o.kind}"


def _annotate(
    parts: list[Part],
    kinds: dict[str, str] | None = None,
    wire_limits: dict[str, float] | None = None,
    wire_default: float = 25.4,
    locks: dict | None = None,
) -> tuple[list[str], dict]:
    """Wires, labels and power symbols so KiCad reads exactly the board's netlist.

    Per net: join same-symbol pins that sit in a line, then join nearby symbols
    with Manhattan wires when a clean path exists. Whatever is still apart gets
    named: every connected group of a signal net carries a label, every group of
    a power net carries a power symbol. Nothing is skipped because a pin name
    looks like the net — KiCad does not connect on names. Then text goes where
    it overlaps least, and what still collides is reported.
    """
    out: list[str] = []
    sites: dict[str, list[_Site]] = defaultdict(list)
    for p in parts:
        for pin in p.pins:
            if not pin.net or pin.net.startswith("unconnected") or pin.net.endswith(".NC"):
                continue
            wx, wy = pin_world(p, pin)
            sites[pin.net].append(_Site(p, pin, wx, wy))

    sheet = _Sheet(parts)
    locks = locks or {}
    label_locks, power_locks = _lock_sites(sites, locks)
    # A stub-label lock makes its pin's link a label even where a wire would fit (docs/schematic-layout.md §3.4).
    nowire = {(s.part.ref, s.pin.number) for s, spec in label_locks if spec.stub is not None}
    for p in parts:
        sheet.occupy(core_aabb(p), "symbol", p.ref)
        if p.kind == "box":
            for pin in p.pins:
                ex, ey = pin_world(p, pin)
                ox, oy = pin_outward(p, pin)
                bx, by = ex - ox * pin.length, ey - oy * pin.length
                # Sideways slack only: a stub leaving the pin end is not a hit.
                if abs(ox) > abs(oy):
                    pin_box = (min(ex, bx), ey - 0.3, max(ex, bx), ey + 0.3)
                else:
                    pin_box = (ex - 0.3, min(ey, by), ex + 0.3, max(ey, by))
                sheet.occupy(pin_box, "pin", f"{p.ref}.{pin.number}")
                # The number sits along the pin, centred on it; KiCad draws a long one past the pin's ends (USB-C's
                # A1B12), and a power symbol at the pin end must clear it.
                from .sch_font import text_width

                half = max(min(_text_w(pin.number) / 2.0, pin.length / 2.0), text_width(pin.number) / 2.0)
                mx, my = (ex + bx) / 2.0, (ey + by) / 2.0
                if abs(ox) > abs(oy):
                    # KiCad draws the number clear of the pin's line (0.508 mm above it): a wire along the pin
                    # does not cross it.
                    box = (mx - half, my - _TEXT_H - 0.4, mx + half, my - 0.4)
                else:
                    box = (mx - _TEXT_H - 0.4, my - half, mx - 0.4, my + half)
                sheet.occupy(box, "pintext", f"{p.ref}.{pin.number}")
            if not _two_pin(p):
                # Pencilled in where the library puts them; placed for real
                # after the symbols, and moved if that spot is taken.
                rx, ry = _prop_world(p, p.prop_ref)
                vx, vy = _prop_world(p, p.prop_val)
                sheet.occupy(_prop_box(p.ref, rx, ry, None), "reserve", p.ref)
                sheet.occupy(_prop_box(p.display, vx, vy, None), "reserve", p.ref)
        else:
            sheet.occupy(body_aabb(p), "symbol", p.ref)

    _reserve(sheet, parts, sites, kinds)
    unions: dict[str, _Union] = {}
    net_segs: dict[str, list[tuple[int, list[tuple[float, float]]]]] = defaultdict(list)

    def _span(sts: list[_Site]) -> float:
        return min(
            (math.hypot(a.x - b.x, a.y - b.y) for i, a in enumerate(sts) for b in sts[i + 1 :] if a.part is not b.part),
            default=0.0,
        )

    # Two-pin nets first, closest first: a short straight wire claims its lane
    # before a big net has to bend around it.
    order = sorted(sites, key=lambda n: (is_power_net(n, kinds), len(sites[n]) > 2, _span(sites[n])))
    for net in order:
        sts = sites[net]
        uf = _Union(len(sts))
        unions[net] = uf
        gnd = is_ground_net(net, kinds)

        # Same symbol, pins in a line: a rail along the pin ends.
        rails: list[tuple[float, int, int]] = []
        for i, a in enumerate(sts):
            for j in range(i + 1, len(sts)):
                b = sts[j]
                if a.part is not b.part:
                    continue
                if abs(a.x - b.x) < _EPS or abs(a.y - b.y) < _EPS:
                    rails.append((math.hypot(a.x - b.x, a.y - b.y), i, j))
        def _free(i: int, j: int, pts) -> bool:
            if not nowire:
                return True
            locked = (sts[i].part.ref, sts[i].pin.number) in nowire or (sts[j].part.ref, sts[j].pin.number) in nowire
            return not locked or all(_near(ax, ay, bx, by) for (ax, ay), (bx, by) in zip(pts, pts[1:]))

        for _d, i, j in sorted(rails):
            if uf.find(i) == uf.find(j):
                continue
            pts = [(sts[i].x, sts[i].y), (sts[j].x, sts[j].y)]
            if not _free(i, j, pts):
                continue
            if sheet.path_ok(pts, net):
                out.extend(sheet.add(pts, net))
                net_segs[net].append((i, pts))
                uf.join(i, j)
        # Same side with unconnected pins in between (a USB-C's DP1/DP2): a bus
        # bar just outside the pin ends. Never around a pin of another net -
        # that would wall its wire in.
        jogs: list[tuple[float, int, int]] = []
        for i, a in enumerate(sts):
            for j in range(i + 1, len(sts)):
                b = sts[j]
                if a.part is not b.part or uf.find(i) == uf.find(j):
                    continue
                oa, ob = pin_outward(a.part, a.pin), pin_outward(b.part, b.pin)
                if oa[0] * ob[0] + oa[1] * ob[1] < 0.9:
                    continue
                span = math.hypot(a.x - b.x, a.y - b.y)
                if span > 10.16:  # a short bar; far-apart pins get their own symbol or label
                    continue
                walled = any(
                    pin.net and pin.net != net and _inside_segment(*pin_world(a.part, pin), a.x, a.y, b.x, b.y)
                    for pin in a.part.pins
                )
                if not walled:
                    jogs.append((span, i, j))
        for _d, i, j in sorted(jogs):
            if uf.find(i) == uf.find(j):
                continue
            a, b = sts[i], sts[j]
            ox, oy = pin_outward(a.part, a.pin)
            best_jog = None
            for k in (2.54, 5.08, 7.62, 10.16):
                pts = [(a.x, a.y), (a.x + ox * k, a.y + oy * k), (b.x + ox * k, b.y + oy * k), (b.x, b.y)]
                if not _free(i, j, pts) or not sheet.path_ok(pts, net):
                    continue
                c = _route_cost(sheet, pts, net)
                if best_jog is None or c < best_jog[0]:
                    best_jog = (c, pts)
            if best_jog is not None:
                out.extend(sheet.add(best_jog[1], net))
                net_segs[net].append((i, best_jog[1]))
                uf.join(i, j)

        # Other symbols: shortest clean Manhattan paths first. Ground is symbols
        # only; a supply net gets a wire only when it is one short straight
        # segment, else each site gets its own symbol - never a rail that
        # crosses the sheet.
        if gnd:
            continue
        power = is_power_net(net, kinds)
        limit = (wire_limits or {}).get(net, wire_default)
        if power:
            limit = min(limit, 15.24)
        if limit <= 0:
            continue
        edges: list[tuple[float, int, int]] = []
        for i, a in enumerate(sts):
            for j in range(i + 1, len(sts)):
                b = sts[j]
                if a.part is b.part:
                    continue
                d = math.hypot(a.x - b.x, a.y - b.y)
                if d <= limit:
                    edges.append((d, i, j))
        for _d, i, j in sorted(edges):
            if uf.find(i) == uf.find(j):
                continue
            best_route = None
            for k, pts in enumerate(_routes(sts[i], sts[j])):
                if power and len(pts) > 2:
                    # A supply is symbols, not rails - except a short L that
                    # ties two of one part's pins (VDDIO to VDD two pins up).
                    run = sum(math.hypot(bx - ax, by - ay) for (ax, ay), (bx, by) in zip(pts, pts[1:]))
                    if len(pts) > 4 or run > 10.16 or sts[i].part is not sts[j].part:
                        continue
                if sum(math.hypot(bx - ax, by - ay) for (ax, ay), (bx, by) in zip(pts, pts[1:])) > limit + _EPS:
                    continue
                if not _free(i, j, pts) or not sheet.path_ok(pts, net):
                    continue
                c = _route_cost(sheet, pts, net) + 0.001 * k
                if best_route is None or c < best_route[0]:
                    best_route = (c, pts)
            if best_route is not None:
                pts = best_route[1]
                out.extend(sheet.add(pts, net))
                net_segs[net].append((i, pts))
                uf.join(i, j)

    # Name every connected group. Labels first: a label can only slide along
    # its stub, a power symbol can also jog, so it is the one that gives way.
    # The symbol reservations stay up until the symbols themselves are placed,
    # so a label does not sit where a symbol has to go. Three passes: each
    # re-places every name with all the others in sight, so the order they
    # were drawn in does not decide who got the clean spot.
    jobs: list[tuple[str, object]] = []
    hats: list[tuple] = []
    locked_tags: set[str] = set()
    for net, sts in sites.items():
        if is_power_net(net, kinds):
            continue
        uf = unions[net]
        for group in uf.groups():
            mine = [(st, spec) for st, spec in label_locks if st.pin.net == net and any(sts[i] is st for i in group)]
            if mine:
                for st, spec in mine:
                    tag = f"label:{net}:{st.part.ref}.{st.pin.number}"
                    gsegs = [
                        (ax, ay, bx, by)
                        for i, pts in net_segs[net]
                        if uf.find(i) == uf.find(group[0])
                        for (ax, ay), (bx, by) in zip(pts, pts[1:])
                        if not _near(ax, ay, bx, by)
                    ]
                    jobs.append((tag, lambda t=tag, st=st, sp=spec, g=gsegs, members=[sts[i] for i in group], ck=False: _commit_label_lock(sheet, st, sp, t, g, members, ck)))
                    locked_tags.add(tag)
                continue
            segs = [
                (ax, ay, bx, by)
                for i, pts in net_segs[net]
                if uf.find(i) == uf.find(group[0])
                for (ax, ay), (bx, by) in zip(pts, pts[1:])
                if not _near(ax, ay, bx, by)
            ]
            s = sts[group[0]]
            tag = f"label:{net}:{s.part.ref}.{s.pin.number}"
            if segs:
                jobs.append((tag, lambda t=tag, sg=segs, n=net, o=f"{s.part.ref}.{s.pin.number}", st=s: _place_wire_label(sheet, sg, n, o, t, st)))
            else:
                jobs.append((tag, lambda t=tag, st=s, n=net: _place_stub_label(sheet, st, n, t)))
    for net, sts in sites.items():
        if not is_power_net(net, kinds):
            continue
        gnd = is_ground_net(net, kinds)
        for group in unions[net].groups():
            members = [sts[i] for i in group]
            mine = [(st, spec) for st, spec in power_locks if st.pin.net == net and any(st is m for m in members)]
            if mine:
                for st, spec in mine:
                    tag = f"hat:{net}:{st.part.ref}.{st.pin.number}"
                    jobs.append((tag, lambda t=tag, st=st, sp=spec, n=net, g=gnd, ck=False: _commit_power_lock(sheet, st, sp, n, g, t, ck)))
                    locked_tags.add(tag)
                continue
            tag = f"hat:{net}:{members[0].part.ref}.{members[0].pin.number}"
            jobs.append((tag, lambda t=tag, m=members, n=net, g=gnd: _place_hat(sheet, m, n, g, t)))
            hats.append((tag, members, net, gnd))
    drawn: dict[str, list[str]] = {}

    def one_pass() -> None:
        for tag, job in jobs:
            sheet.remove(tag)
            drawn[tag] = job()
            if tag.startswith("hat:"):
                # Its symbol is real now; the pencilled-in zones for that net's pins go.
                net_of = tag.split(":")[1]
                sheet.occupants = [o for o in sheet.occupants if not (o.kind == "reserve" and o.net == net_of)]

    one_pass()
    one_pass()
    # Two symbols on one part (an LDO's GND between VIN and EN) can each block
    # the other's only clean spot; one at a time never gets out of that. Try
    # them as a pair: for each good spot of one, the best of the other.
    by_part: dict[str, list[tuple]] = defaultdict(list)
    for h in hats:
        for m in h[1]:
            if h not in by_part[m.part.ref]:
                by_part[m.part.ref].append(h)
    seen_pairs: set[tuple[str, str]] = set()

    def near(a: tuple, b: tuple) -> bool:
        return any(
            math.hypot(x.x - y.x, x.y - y.y) <= 10.16 for x in a[1] for y in b[1]
        )

    for group in by_part.values():
        for i, a in enumerate(group):
            for b in group[i + 1 :]:
                if (a[0], b[0]) in seen_pairs or not near(a, b):
                    continue
                seen_pairs.add((a[0], b[0]))
                _joint_hats(sheet, a, b, drawn)
    # Then every name and symbol again with all the others in sight, until none moves (each pass places them in one
    # order, so one may have chosen against a neighbour that then moved): at most four passes.
    for _settle in range(4):
        before = {t: tuple(v) for t, v in drawn.items()}
        one_pass()
        if all(tuple(drawn[t]) == before.get(t) for t in drawn):
            break
    # A lock is judged on the finished sheet, where every other name and symbol has settled (during the passes a solved
    # placement may stand on its spot for a while): taken off, checked, and drawn again. A stub that meets another net
    # is a refusal naming its line.
    # A refusal is the kept pass's only: this pass records it, and `_emit` ranks a pass with one below every pass
    # without, so a lock copied from gen is judged where the layout it came from has settled (a pass before the repair
    # loop's flips may not have its wire yet).
    lock_errors: list[str] = []
    lock_failed: list[str] = []
    lock_involved: list[tuple[str, ...]] = []
    for tag, job in jobs:
        if tag in locked_tags:
            sheet.remove(tag)
            try:
                job(ck=True)
            except ValueError as exc:
                lock_errors.append(str(exc))
                lock_failed.append(tag)
                # The parts on the lock's net, for the repair loop: the lock was written on a sheet where they stood
                # otherwise (a flip the loop made before the lock was copied from gen).
                net = tag.split(":")[1]
                lock_involved.append(tuple(sorted({st.part.ref for st in sites.get(net, ())})))
            sheet.remove(tag)
            drawn[tag] = job()
    sheet.occupants = [o for o in sheet.occupants if o.kind != "reserve"]
    for tag, _job in jobs:
        out.extend(drawn[tag])
    # One PWR_FLAG per power net, placed like any symbol where it touches
    # nothing: ERC then sees every power net driven (the connector's VBUS and
    # the regulator's output have no power-output pin in their library symbols).
    for net, sts in sites.items():
        if is_power_net(net, kinds):
            out.extend(_place_hat(sheet, sts, net, False, f"flag:{net}", lib="power:PWR_FLAG", value="PWR_FLAG"))
    out.extend(_junctions(sheet))
    # A pin the board leaves unbound is marked so: ERC's "pin not connected"
    # is for pins someone forgot, not for the ones the board file left open.
    for p in parts:
        for pin in p.pins:
            if not pin.net or pin.net.startswith("unconnected") or pin.net.endswith(".NC"):
                x, y = pin_world(p, pin)
                out.append(SheetNoConnect((_q4(x), _q4(y)), uuid=_uid(f"nc:{p.ref}:{pin.number}")))
    two_pin = [p for p in parts if _two_pin(p)]
    boxes = [p for p in parts if not _two_pin(p)]
    text_locks = locks.get("text") or {}
    for _round in range(2):
        for p in boxes:
            _place_box_text(sheet, p, text_locks)
        for p in two_pin:
            _place_passive_text(sheet, p, text_locks)
    power_nets = {n for n in sites if is_power_net(n, kinds)}
    ground_nets = {n for n in sites if is_ground_net(n, kinds)}
    report = _lint(sheet, parts, sites, unions, power_nets, ground_nets, kinds, wire_limits, wire_default)
    # Everything drawn for a part: its body, its name, and the symbols and
    # labels hung on its pins. The region outline is the box around these.
    report["marks"] = [
        (o.owner, o.box)
        for o in sheet.occupants
        if o.kind != "reserve"
    ]
    report["_sheet"] = sheet
    report["_locked_tags"] = locked_tags
    report["lock_errors"] = lock_errors
    report["_lock_failed"] = [(tag, sheet.records.get(tag)) for tag in lock_failed]
    report["lock_involved"] = lock_involved
    return out, report


def emit_from_design(design: Design, *, title: str = "", report: dict | None = None) -> str:
    """The sheet text. ``report`` (if given) receives the readability issues
    and every part's pose. The same design gives the same text, byte for byte."""
    global _ids
    _ids = _Ids(title or "pcbc")
    try:
        return _emit(design, title=title, report=report)
    finally:
        _ids = None


def _owner_ref(owner: str) -> str:
    return owner.split(".", 1)[0]


def _region_boxes(layout, parts: list[Part], marks: list[tuple[str, Box]]) -> list[dict]:
    """The box around each region the layout drew an outline for (`sch_auto.Region.outline`). Membership is the
    parts; the outline is everything drawn for those parts, including names, pin numbers, and the power symbols on
    their pins."""
    regions = [r for r in layout.regions if r.outline]
    if not regions:
        return []
    titles = {r.name: (getattr(r, "title", None) or r.name) for r in regions}
    groups: dict[str, list[Part]] = {r.name: [] for r in regions}
    for part in parts:
        name = getattr(part, "sch_group", None)
        if name in groups:
            groups[name].append(part)
    pad = 2.54
    header = 5.08
    # A flag or a label sits a short way off its pin. Farther than this, it
    # is on a wire leaving the group, and the box should not chase it.
    reach = 18.0
    boxes: list[dict] = []
    for region in regions:
        group = groups[region.name]
        if not group:
            continue
        refs = {part.ref for part in group}
        xs: list[float] = []
        ys: list[float] = []
        for part in group:
            x0, y0, x1, y1 = world_aabb(part)
            xs += [x0, x1]
            ys += [y0, y1]
            # KiCad draws a pin number past the end of a pin when the number
            # is wider than the pin. The body's box does not include that.
            for pin in part.pins:
                extra = _text_w(pin.number) - float(pin.length)
                if extra <= 0.5:
                    continue
                px, py = pin_world(part, pin)
                ox, oy = pin_outward(part, pin)
                xs.append(px + ox * extra)
                ys.append(py + oy * extra)
        body = (min(xs), min(ys), max(xs), max(ys))
        bx0, by0, bx1, by1 = body
        for owner, box in marks:
            if _owner_ref(owner) not in refs:
                continue
            mx0, my0, mx1, my1 = box
            if mx1 < bx0 - reach or mx0 > bx1 + reach or my1 < by0 - reach or my0 > by1 + reach:
                continue
            xs += [mx0, mx1]
            ys += [my0, my1]
        x0, y0 = min(xs) - pad, min(ys) - pad - header
        x1, y1 = max(xs) + pad, max(ys) + pad
        from .sch_font import text_width

        x1 = max(x1, x0 + 1.27 + text_width(titles[region.name], 2.54) + pad)  # the title stays inside its box
        boxes.append(
            {
                "name": region.name,
                "title": titles[region.name].replace('"', "'"),
                "x0": x0,
                "y0": y0,
                "x1": x1,
                "y1": y1,
                "refs": sorted(refs),
                # Relative to the frame `sch_auto._normalize` measures in (the members' box at the first slot).
                "raw": (x0 - region.origin[0] + region.norm[0], y0 - region.origin[1] + region.norm[1], x1 - region.origin[0] + region.norm[0], y1 - region.origin[1] + region.norm[1]),
                "row": region.row,
                "col": region.col,
            }
        )
    _align_region_boxes(boxes)
    return boxes


def _render_region_boxes(boxes: list[dict]) -> list:
    out: list = []
    for box in boxes:
        x0, y0, x1, y1 = box["x0"], box["y0"], box["x1"], box["y1"]
        out.append(SheetRect((_q4(x0), _q4(y0)), (_q4(x1), _q4(y1)), uuid=_uid(f"group:{box['name']}")))
        out.append(SheetText(box["title"], (_q4(x0 + 1.27), _q4(y0 + 1.5)), uuid=_uid(f"group-title:{box['name']}")))
    return out


def _align_region_boxes(boxes: list[dict]) -> None:
    """One row shares a top and a bottom, with the same gap between neighbors.

    A row is the groups whose tops already sit together. Shorter boxes grow
    down to the tallest, and the space between them is opened to one gutter
    when the parts left more than that."""
    gutter = 12.7
    rows: list[list[dict]] = []
    # A region stacked over another (their boxes share some width) keeps its own height and width: stretching it would
    # cover the one below. Decided from the boxes alone, so a sheet whose regions are locked at= aligns the same way.
    def xs_meet(a, b):
        return a["x0"] < b["x1"] and b["x0"] < a["x1"]

    stacked = [b for b in boxes if any(o is not b and xs_meet(o, b) for o in boxes)]
    boxes = [b for b in boxes if not any(b is x for x in stacked)]
    for box in boxes:
        for row in rows:
            if abs(box["y0"] - row[0]["y0"]) <= 15.0:
                row.append(box)
                break
        else:
            rows.append([box])
    for row in rows:
        top = min(box["y0"] for box in row)
        bottom = max(box["y1"] for box in row)
        for box in row:
            box["y0"] = top
            box["y1"] = bottom
        row.sort(key=lambda box: box["x0"])
        for left, right in zip(row, row[1:]):
            extra = (right["x0"] - left["x1"]) - gutter
            if extra <= 0:
                continue
            if any(st["x0"] < right["x0"] and left["x1"] < st["x1"] for st in stacked):
                continue  # a stacked column stands between them: widening would cover it
            left["x1"] += extra / 2.0
            right["x0"] -= extra / 2.0


_PASSES = 12
_DEBUG = bool(__import__('os').environ.get('PCBC_SCH_DEBUG'))
# Paper sizes (landscape) and the extent each holds: every symbol's far edge plus a 20 mm margin under these.
_PAPER = (("A4", 280.0, 190.0), ("A3", 400.0, 277.0), ("A2", 574.0, 400.0))
_PAPER_MARGIN = 20.0


def _paper(parts: list[Part], sheet_spec=None) -> str:
    """`SchSheet(paper=)` when given, else the smallest paper whose width **and** height hold every symbol with a
    20 mm margin (docs/schematic-layout.md §2.6: picking by width alone put node's sheet on A3 with 24 of 31 bodies
    below the paper's edge)."""
    if sheet_spec is not None and getattr(sheet_spec, "paper", None):
        return sheet_spec.paper
    max_x, max_y = 100.0, 80.0
    for p in parts:
        x0, y0, x1, y1 = world_aabb(p)
        max_x = max(max_x, x1 + _PAPER_MARGIN)
        max_y = max(max_y, y1 + _PAPER_MARGIN)
    for name, w, h in _PAPER:
        if max_x < w and max_y < h:
            return name
    return _PAPER[-1][0]


def _movable(cur: dict, ref: str) -> bool:
    """A part the loop may push: attached (`to=`/`along=`) and not posed by a lock."""
    spec = cur["layout"].specs.get(ref)
    return spec is not None and spec.anchor_kind() in ("to", "along")


def _locks(design: Design, layout) -> dict:
    """schematic.core.py's text, label and power-symbol locks, keyed as `_annotate` reads them."""
    return {
        "text": {(t.ref, t.field): t for t in getattr(design, "sch_texts", ())},
        "label": list(getattr(design, "sch_labels", ())),
        "power": list(getattr(design, "sch_powers", ())),
    }


def _draw_pass(design: Design, kinds: dict, extra: dict, limits: dict, wire_default: float, hints: dict | None = None, honor_at: bool = False) -> dict:
    """One pass: place, then draw the wires, names and symbols. Ids are drawn afresh for every pass, so the pass that
    is kept has the ids it would have had alone (a lock that skips the passes before it changes no id)."""
    if _ids is not None:
        _ids.seen.clear()
    from .sch_auto import resolve

    parts = _parts_from_design(design, kinds)
    layout = resolve(design, parts, extra, hints, honor_at=honor_at)
    annotations, lint = _annotate(parts, kinds, limits, wire_default, locks=_locks(design, layout))
    sheet = lint.pop("_sheet")
    return {
        "parts": parts,
        "layout": layout,
        "annotations": annotations,
        "lint": lint,
        "extra": dict(extra),
        "sheet": sheet,
        "boxes": _region_boxes(layout, parts, lint.get("marks") or []),
        "paper": _paper(parts, getattr(design, "sch_sheet", None)),
        "repacked": bool(hints),
        "hints": dict(hints or {}),
    }


def _rank(cur: dict) -> tuple:
    """A pass's place in the loop's order: one whose locks do not hold ranks below every one whose locks do, then the
    readability order (`sch_score.key`)."""
    from .sch_score import key

    return (bool(cur["lint"].get("lock_errors")), key(cur["vec"]))


def _run(design: Design, kinds: dict, limits: dict, wire_default: float) -> dict:
    """The layout loop (docs/schematic-layout.md §2.5): passes drawn and scored, the best by `_rank` kept."""
    from .sch_score import key as _score_key, score as _score

    extra: dict[str, float] = {}
    hints: dict[str, tuple] = {}
    best: dict | None = None
    for _pass in range(_PASSES):
        cur = _draw_pass(design, kinds, extra, limits, wire_default, hints)
        cur["vec"] = _score(cur["sheet"], cur["parts"], cur["lint"], paper=cur["paper"], region_boxes=cur["boxes"])
        if _DEBUG:
            print("pass", _pass, _score_key(cur["vec"]), cur["vec"].get("_involved"), {k: v for k, v in extra.items()})
        if best is None or _rank(cur) <= _rank(best):
            best = cur
        elif not cur.get("repacked"):
            break
        # What this pass drew for each region, as the frame the next pass packs (docs/schematic-layout.md §2.5).
        grown = dict(hints)
        for b in cur["boxes"]:
            old = grown.get(b["name"])
            raw = b["raw"]
            grown[b["name"]] = raw if old is None else (min(old[0], raw[0]), min(old[1], raw[1]), max(old[2], raw[2]), max(old[3], raw[3]))
        repack = grown != hints and cur["vec"]["region_frames"] > 0
        hints = grown
        pushes = dict(cur["lint"].get("pushes") or {})
        if not cur["vec"]["hard"]:
            # A name cut off from its part is repaired once the pass has no hard finding: room for a name never comes
            # before a clean sheet (pushed with the hard repairs, DS2's R4 and R5 made the no-core sheet 1.55 times the
            # hand sheet's area).
            for r, mm in (cur["lint"].get("cut_pushes") or {}).items():
                pushes[r] = max(pushes.get(r, 0.0), mm)
        # A hard finding (§2.6) pushes the hangers it involves one grid step further out: the repair the loop tries.
        flips: list[str] = []
        # So does a part standing between two pins of one net that are in line (they are joined by labels instead).
        for refs in list(cur["vec"].get("_involved", ())) + [[r] for r in cur["lint"].get("blocked", ())] + list(cur["lint"].get("lock_involved", ())):
            movable = [r for r in refs if _movable(cur, r)]
            for r in movable:
                if not extra.get(r + FLIP):
                    flips.append(r)  # first its other pose, then further out
                else:
                    pushes[r] = max(pushes.get(r, 0.0), 2.54)
        grew = False
        trial = dict(extra)
        for ref, mm in pushes.items():
            nxt = trial.get(ref, 0.0) + mm
            if nxt <= 10.16 and nxt > trial.get(ref, 0.0) + 0.01:
                trial[ref] = nxt
                grew = True
        for r in flips:
            trial[r + FLIP] = 1.0
            grew = True
        if not grew and not repack:
            break
        extra = trial
    assert best is not None
    # A region origin locked by `SchRegion(at=)` holds on the sheet that is kept: the passes packed it on the grid, and
    # when the kept pass has it elsewhere the sheet is drawn once more, with that pass's pushes and frames, at the lock.
    locked_at = {r.name: tuple(r.declared.at) for r in best["layout"].regions if r.declared is not None and r.declared.at is not None}
    if any(abs(r.origin[0] - locked_at[r.name][0]) > 1e-4 or abs(r.origin[1] - locked_at[r.name][1]) > 1e-4 for r in best["layout"].regions if r.name in locked_at):
        best = _draw_pass(design, kinds, best["extra"], limits, wire_default, best["hints"], honor_at=True)
        best["vec"] = _score(best["sheet"], best["parts"], best["lint"], paper=best["paper"], region_boxes=best["boxes"])
    return best


def relaxed(design: Design, locks: list, applied: set) -> Design:
    """The design with every lock of a drawn value (`sch_gen.lock_bodies`) left out except the `applied` ones: a region
    origin, a pose, a label, a power symbol, a text and the paper."""
    import copy
    import dataclasses

    if len(applied) == len(locks):
        return design
    keep = {id(spec) for _stage, key, _body, spec in locks if key in applied}
    listed = {id(spec) for *_x, spec in locks}

    def held(spec) -> bool:
        return id(spec) not in listed or id(spec) in keep

    run = copy.copy(design)
    run.sch_regions = [r if held(r) else dataclasses.replace(r, at=None) for r in design.sch_regions]
    run.sch_places = [s for s in design.sch_places if held(s)]
    run.sch_texts = [s for s in design.sch_texts if held(s)]
    run.sch_labels = [s for s in design.sch_labels if held(s)]
    run.sch_powers = [s for s in design.sch_powers if held(s)]
    if design.sch_sheet is not None and not held(design.sch_sheet):
        run.sch_sheet = dataclasses.replace(design.sch_sheet, paper=None)
    return run


def _causal_poses(design: Design, kinds: dict, locks: list, applied: set, now: list, cur: dict, steps: dict, tried: dict) -> list:
    """Of the pose locks that differ from the pass `cur`, the ones to apply first, as `(region, picked, differing)`.
    In each region one pose is tried alone: the one whose pose put in place makes the most of the region's other
    differing poses come out as locked (placement alone, with the pass's pushes and frames; one `resolve` each), then
    one its part's own spec cannot reach, then placement order. `_settle` keeps it if, laid out, it makes another of them
    come out as locked, and else takes it back and tries the next (`tried`). A pose that differs only because a part
    placed before it moved is then met once that part's pose holds (refute:lock 2026-10-09: node's C_MCU pose copied
    over its attach line with R_LED's, c3_usb's C_MCU with C_EN's, node's C_BME with R_SCL's: applied together, the
    second part, pinned where it already belonged, took the repair loop another road and moved others). When every pose
    of a region has been tried, or after `_CAUSAL_STEPS` steps, the rest are applied at once."""
    from .sch_auto import resolve
    from .sch_gen import pose_body

    bodies = {key: body for _stage, key, body, _spec in locks}
    region = {key: getattr(cur["layout"].region_of.get(spec.ref), "name", None) for key, spec in now}
    order = {p.ref: i for i, p in enumerate(cur["parts"])}
    out = []
    for reg in dict.fromkeys(r for r in region.values()):
        mine = [(key, spec) for key, spec in now if region[key] == reg]
        cands = [(key, spec) for key, spec in mine if key not in tried.get(reg, set())]
        if reg is None or len(mine) < 2 or not cands or steps.get(reg, 0) >= _CAUSAL_STEPS:
            out.append((reg, mine, mine))
            continue
        ranked = []
        for key, spec in cands:
            parts = _parts_from_design(design, kinds)
            lay = resolve(relaxed(design, locks, applied | {key}), parts, dict(cur["extra"]), dict(cur["hints"]) or None, honor_at=False)
            by = {p.ref: p for p in parts}
            origin = {r.name: r.origin for r in lay.regions}
            left = sum(1 for k, s in mine if k != key and pose_body(by[s.ref], origin[lay.region_of[s.ref].name]) != bodies[k])
            ranked.append(((left, not _unreached(spec, cur), order.get(spec.ref, 0)), key, spec))
        ranked.sort(key=lambda r: r[0])
        steps[reg] = steps.get(reg, 0) + 1
        out.append((reg, [(ranked[0][1], ranked[0][2])], mine))
    return out


_CAUSAL_STEPS = 6  # poses applied one at a time per region before the rest go at once


def _unreached(spec, cur: dict) -> bool:
    """A pose lock its part's own spec cannot put it on in pass `cur`: another rotation or mirror, or a point off the
    line the part was attached along (a pose its spec reaches differs only by how far the part was pushed)."""
    lay = cur["layout"]
    part = next((p for p in cur["parts"] if p.ref == spec.ref), None)
    reg = lay.region_of.get(spec.ref)
    if part is None or reg is None:
        return True
    rot = int(round(spec.rot)) % 360 if spec.rotate_set else 0
    if rot != int(round(part.rot)) % 360 or (spec.mirror or None) != (part.mirror or None):
        return True
    d = getattr(part, "attach_dir", None)
    if not d:
        return True
    dx, dy = spec.at[0] - (part.x - reg.origin[0]), spec.at[1] - (part.y - reg.origin[1])
    return abs(dx * d[1] - dy * d[0]) > 1e-3


def _settle(design: Design, kinds: dict, limits: dict, wire_default: float, parts: list) -> dict:
    """The kept pass, with schematic.core.py's locks of drawn values (a region origin, a pose, a label, a power symbol, a
    text, the paper) applied only where they change the sheet.

    A lock equal to what the layout draws without it is a fixed point (docs/schematic-layout.md §4: a gen line copied
    into core changes nothing). Applying such a lock inside the layout is not: a part locked where the repair loop's
    third pass put it is already there on the first, so the loop takes another road and every part it moves on that road
    lands elsewhere (refute:split 2026-10-08: 117 of 1091 single gen lines changed the sheet, node's U1 raised it to 19
    hard findings). So the sheet is first laid out with every such lock left out; a lock that equals the result is met
    and the result stands. Otherwise the locks that differ, of the earliest stage the layout decides (origins and paper,
    then poses, then names and symbols, then texts), are applied, and the rest are compared again with that sheet.

    Of the poses that differ, the ones that explain the others are applied first (`_causal_poses`): a pose that differs
    only because a part placed before it moved is met once that part's pose holds."""
    import copy

    from .sch_gen import lock_bodies, object_lines, _pt

    locks = lock_bodies(design, parts)
    applied: set[tuple] = set()
    history: list[dict] = []
    forced: set[str] = set()
    steps: dict = {}  # per region, the poses tried one at a time (`_causal_poses`)
    tried: dict = {}  # per region, the poses that, applied alone, made none of the others come out as locked
    trials: dict = {}  # per region, the pose being tried and the others that differed with it
    done: dict = {}  # the passes laid out so far, by the locks applied
    while True:
        sig = (frozenset(applied), frozenset(forced))
        if sig in done:
            best = done[sig]
        else:
            run = relaxed(design, locks, applied)
            if forced:
                run = copy.copy(run)
                run.__dict__["_sch_forced"] = frozenset(forced)
            best = done[sig] = _run(run, kinds, limits, wire_default)
        # A met attach line whose side the placer chose for a `beside` spec holds only if it chose the line's side on the
        # kept pass; otherwise the line is the lock it says, and the sheet is laid out again with it (sch_auto.met_attaches).
        wrong = {m for m, line in best["layout"].met.items() if line.side and best["layout"].specs[m].side != line.side}
        if wrong:
            forced |= wrong
            continue
        best["applied"] = {key: spec for _stage, key, _body, spec in locks if key in applied}
        best["relaxed_hard"] = history[0]["vec"]["hard"] if history else best["vec"]["hard"]
        history.append(best)
        raw = best["lint"].setdefault("_lock_errors_raw", list(best["lint"].get("lock_errors") or []))
        if raw:
            best["lint"]["lock_errors"] = [_lock_error_fact(e, tag, history, best["applied"]) for e, (tag, _rec) in zip(raw, best["lint"].get("_lock_failed") or [])] or raw
        if not locks or len(applied) == len(locks):
            return best
        drawn = {key: body for key, body, _c in object_lines(design, best["layout"], best["sheet"], best["parts"])}
        drawn.update({("SchRegion", r.name): _pt(r.origin) for r in best["layout"].regions})
        drawn[("SchSheet",)] = best["paper"]
        differ = [(stage, key, spec) for stage, key, body, spec in locks if key not in applied and drawn.get(key) != body]
        # A pose tried alone stays if another of its region's differing poses now comes out as locked; else it is taken
        # back and the next one is tried (from the pass laid out without it).
        still = {key for _stage, key, _spec in differ}
        back = {key for key, others in trials.values() if others and others <= still}
        for reg, (key, others) in list(trials.items()):
            if key in back:
                applied.discard(key)
                tried.setdefault(reg, set()).add(key)
        trials.clear()
        if back:
            continue
        if differ:
            first = min(stage for stage, _k, _s in differ)
            now = [(key, spec) for stage, key, spec in differ if stage == first]
            if first == 1 and len(now) > 1:
                picked = []
                for reg, keys, mine in _causal_poses(design, kinds, locks, applied, now, best, steps, tried):
                    picked.extend(keys)
                    if len(keys) == 1 and len(mine) > 1:
                        trials[reg] = (keys[0][0], {k for k, _s in mine} - {keys[0][0]})
                now = picked
            applied |= {key for key, _spec in now}
            continue
        # Every lock left out is met: it is recorded as the lock it is (gen names its line).
        for _stage, key, _body, spec in locks:
            if key in applied:
                continue
            if key[0] == "SchPlace":
                best["layout"].specs[spec.ref] = spec
            elif key[0] == "SchRegion":
                for reg in best["layout"].regions:
                    if reg.name == spec.name:
                        reg.declared = spec
            elif key[0] in ("SchLabel", "SchPower"):
                kind = "label" if key[0] == "SchLabel" else "power"
                for rec in best["sheet"].records.values():
                    s = rec["site"]
                    if rec["kind"] == kind and rec["net"] == key[1] and s.part.ref == key[2] and s.pin.number == key[3]:
                        rec["line"] = spec.line
        return best


def _lock_error_fact(err: str, tag: str, history: list[dict], applied: dict) -> str:
    """A lock refusal with the facts of how its sheet came to be: which locks of schematic.core.py this sheet applies
    (the ones that differ from the sheet laid out without them), and, for a label placed on its group's wire, whether
    that point was on the net's wire in a sheet laid out with fewer of them (refute:split 2026-10-08: the refusal said
    the point was on no wire when the tool's own redraw had moved that wire)."""
    others = sorted({spec.line for spec in applied.values() if getattr(spec, "line", 0)} - {_tag_line(history[-1], tag)})
    if not others:
        return err + "; this sheet applies no other lock of schematic.core.py (the layout without its locks draws the group so)"
    out = err + f"; this sheet applies the locks at schematic.core.py:{', '.join(str(n) for n in others)}, which differ from the sheet laid out without them"
    point = _label_point(err)
    net = tag.split(":")[1] if tag.count(":") >= 2 else None
    if point is not None and net is not None:
        for earlier in history[:-1]:
            hit = next(((x0, y0, x1, y1) for x0, y0, x1, y1, n in earlier["sheet"].segs if n == net and (_inside_segment(*point, x0, y0, x1, y1) or _near(*point, x0, y0) or _near(*point, x1, y1))), None)
            if hit is not None:
                lines = sorted(spec.line for spec in earlier["applied"].values() if getattr(spec, "line", 0))
                what = f"with only the locks at schematic.core.py:{', '.join(map(str, lines))}" if lines else "with none of them"
                out += f"; laid out {what}, ({_fmt(point[0])}, {_fmt(point[1])}) is on the {net} wire ({_fmt(hit[0])}, {_fmt(hit[1])})-({_fmt(hit[2])}, {_fmt(hit[3])})"
                break
    return out


def _lock_label(key: tuple) -> str:
    """A lock's object as its line names it: `SchPlace('L1')`, `SchRegion('mcu')`, `SchLabel('EN', pin='R_EN.2')`."""
    kind = key[0]
    if kind in ("SchPlace", "SchRegion"):
        return f"{kind}({key[1]!r})"
    if kind == "SchText":
        return f"SchText({key[1]!r}, {key[2]!r})"
    if kind in ("SchLabel", "SchPower"):
        return f"{kind}({key[1]!r}, pin='{key[2]}.{key[3]}')"
    return f"{kind}()"


def _tag_line(cur: dict, tag: str) -> int:
    rec = cur["sheet"].records.get(tag) or {}
    return int(rec.get("line") or 0)


def _label_point(err: str):
    m = re.search(r"\)\): \((-?[0-9.]+), (-?[0-9.]+)\) is on no wire", err)
    return (float(m.group(1)), float(m.group(2))) if m else None


def _emit(design: Design, *, title: str, report: dict | None) -> str:
    kinds = {n.name: n.kind for n in design.nets.values()}
    parts = _parts_from_design(design, kinds)
    libs = [_lib_gnd(), _lib_vcc(), _lib_pwr_flag(), _lib_r(), _lib_c(), _lib_l(), _lib_led()]
    seen_lib: set[str] = {"power:GND", "power:VCC", "power:PWR_FLAG", "GND", "VCC", "R", "C", "L", "LED"}
    for p in parts:
        if p.lib_id in seen_lib:
            continue
        if p.lib_sexp:
            libs.append(p.lib_sexp)
            seen_lib.add(p.lib_id)
        elif p.kind == "box":
            libs.append(_lib_box(p.lib_id, p.pins, p.ref[:1] or "U", kinds))
            seen_lib.add(p.lib_id)
    # The wire limits are schematic.core.py's: SchNet(net, wire_mm=) per net, SchSheet(wire_mm=) for the rest.
    limits = {n.net: float(n.wire_mm) for n in design.sch_nets}
    wire_default = design.sch_sheet.wire_mm if design.sch_sheet is not None else 25.4
    # Names take a spot no wire crosses. When a net's name does not fit on its
    # wire, the downstream part slides one grid step out and the sheet is
    # drawn again. Each pass is scored (`sch_score`, docs/schematic-layout.md
    # §2.6) and the best by that order is kept; a tie keeps the later pass, and
    # the loop stops at the first pass that is worse.
    best = _settle(design, kinds, limits, wire_default, parts)
    if best["lint"].get("lock_errors"):
        raise ValueError(best["lint"]["lock_errors"][0])
    if report is not None:
        # The pose locks this sheet applies (the ones that differ from the layout without them): a hard finding that
        # names one of their parts names its line too (`build._name_locks`).
        report["_applied_poses"] = {spec.ref: spec.line for key, spec in best["applied"].items() if key[0] == "SchPlace"}
        # Every lock the sheet applies, and the hard count of the sheet laid out without them: a hard finding raised by a
        # real move names the locks that differ, whichever part the finding names (`build`, refute:lock 2026-10-09).
        report["_applied_locks"] = sorted((spec.line, _lock_label(key)) for key, spec in best["applied"].items() if getattr(spec, "line", 0))
        report["_relaxed_hard"] = best.get("relaxed_hard", 0)
    parts, annotations, lint, extra, paper = best["parts"], best["annotations"], best["lint"], best["extra"], best["paper"]
    if report is not None:
        report["parts"] = [
            {"ref": p.ref, "x": round(p.x, 3), "y": round(p.y, 3), "rot": p.rot, "mirror": p.mirror}
            for p in parts
        ]
        report["gap_extra"] = dict(extra)
        report.update({k: v for k, v in lint.items() if k != "_sheet"})
        report["readability"] = best["vec"]
        # The kept pass itself, for the sch stage's re-score on KiCad's own SVG (`sch_score.with_svg`). Not JSON.
        report["_sheet"], report["_parts"], report["_boxes"], report["_layout"] = best["sheet"], parts, best["boxes"], best["layout"]
    body: list = _render_region_boxes(best["boxes"])
    body.extend(_instance(p) for p in parts)
    body.extend(annotations)
    uid = _uid("sheet")
    title = title or "pcbc"
    body = _dedupe_ids(body, title)
    body = _number_power(body)
    header = Header(uid, paper, title, "".join(libs))
    if report is not None:
        # The sheet as primitives (docs/schematic-layout.md PR 6): what gen writes and the sch stage emits from.
        report["_prims"], report["_header"] = body, header
    return sheet_text(header, body)


def _number_power(prims: list) -> list:
    """`#PWR001`, `#PWR002`, ... and `#FLG001`, ... in drawing order: the power symbols' and flags' References."""
    import dataclasses

    count = {"#PWR?": 0, "#FLG?": 0}
    out = []
    for p in prims:
        if p.kind == "symbol" and p.fields and p.fields[0].name == "Reference" and p.fields[0].value in count:
            k = p.fields[0].value
            count[k] += 1
            ref = f"{k[:-1]}{count[k]:03d}"
            p = dataclasses.replace(p, fields=(dataclasses.replace(p.fields[0], value=ref), *p.fields[1:]))
        out.append(p)
    return out


def _find_components(start: Path) -> Path | None:
    for parent in (start.parent, *start.parents):
        c = parent / "components"
        if c.is_dir():
            return c
    return None


def emit_schematic_file(
    design: Design, out_path: Path, *, title: str = "", report: dict | None = None
) -> Path:
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(emit_from_design(design, title=title or "pcbc", report=report))
    return out_path
