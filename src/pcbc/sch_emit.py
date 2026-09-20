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
    apply_sch_places,
    body_aabb,
    core_aabb,
    hat_zone,
    lib_to_sheet,
    pin_outward,
    pin_world,
    text_zone,
    world_aabb,
)
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
    return max(len(name), 1) * _FONT * _CHAR_W


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


def _passive_chains(parts: list[Part], kinds: dict[str, str] | None = None) -> list[list[Part]]:
    """Paths of 2-pin passives that share a signal net — place them in a column."""
    passives = [p for p in parts if p.kind in ("r", "c", "l", "d")]
    by_ref = {p.ref: p for p in passives}
    adj: dict[str, list[tuple[str, str]]] = defaultdict(list)
    sites: dict[str, list[Part]] = defaultdict(list)
    for p in passives:
        for pin in p.pins:
            if pin.net and not is_power_net(pin.net, kinds):
                sites[pin.net].append(p)
    for net, group in sites.items():
        uniq: list[Part] = []
        for p in group:
            if p not in uniq:
                uniq.append(p)
        for i, a in enumerate(uniq):
            for b in uniq[i + 1 :]:
                adj[a.ref].append((b.ref, net))
                adj[b.ref].append((a.ref, net))

    remaining = {p.ref for p in passives}
    chains: list[list[Part]] = []

    def has_power(ref: str) -> bool:
        return any(
            is_power_net(pin.net or "", kinds) and not is_ground_net(pin.net or "", kinds)
            for pin in by_ref[ref].pins
        )

    def has_ground(ref: str) -> bool:
        return any(is_ground_net(pin.net or "", kinds) for pin in by_ref[ref].pins)

    while remaining:
        leaves = [r for r in remaining if len([n for n, _ in adj[r] if n in remaining]) <= 1]
        start = next((r for r in leaves if has_power(r)), None)
        if start is None:
            start = next((r for r in leaves if not has_ground(r)), None)
        if start is None:
            start = next(iter(leaves or remaining))
        path = [start]
        remaining.remove(start)
        while True:
            nxt = [n for n, _ in adj[path[-1]] if n in remaining]
            if not nxt:
                break
            path.append(nxt[0])
            remaining.remove(nxt[0])
        ordered = [by_ref[r] for r in path]
        if ordered and has_power(ordered[-1].ref) and not has_power(ordered[0].ref):
            ordered.reverse()
        chains.append(ordered)
    return chains


def _layout(parts: list[Part], kinds: dict[str, str] | None = None) -> None:
    """Compact placement. Each part once. Connected passives share a column."""
    for p in parts:
        p.placed = False
        p.rot = 0.0
    x = 25.4
    y_top = 25.4
    for chain in _passive_chains(parts, kinds):
        y = y_top
        for p in chain:
            p.x, p.y = _snap(x), _snap(y)
            p.rot = 0.0
            p.placed = True
            y += 4 * _GRID
        x += 6 * _GRID
    rest = [p for p in parts if not p.placed]
    rest.sort(key=lambda p: (0 if p.ref[:1] in "UA" else 1 if p.ref[:1] == "J" else 2, p.ref))
    col0 = x if any(p.placed for p in parts) else 25.4
    x, y = col0, y_top
    row_bottom = y
    for p in rest:
        gap = 12.7 if p.kind == "box" else 7.62
        p.x = _snap(x + p.hw + gap)
        p.y = _snap(y)
        p.placed = True
        x = p.x + p.hw + gap
        row_bottom = min(row_bottom, y - p.hh * 2 - 15.0)
        if x > 190:
            x = col0
            y = row_bottom
    if not parts:
        return
    # Keep everything in the positive quadrant with a small margin.
    min_x = min(p.x - p.hw for p in parts)
    min_y = min(p.y - p.hh for p in parts)
    dx = 0.0 if min_x >= 12.7 else _snap(12.7 - min_x)
    dy = 0.0 if min_y >= 12.7 else _snap(12.7 - min_y)
    for p in parts:
        p.x = _snap(p.x + dx)
        p.y = _snap(p.y + dy)


class _Ids:
    """Ids keyed by what they name, so the same board gives the same file byte
    for byte: no churn in git, and a report that does not move between runs."""

    def __init__(self, title: str):
        self.title = title
        self.seen: dict[str, int] = {}

    def __call__(self, key: str) -> str:
        n = self.seen.get(key, 0)
        self.seen[key] = n + 1
        return stable_uuid(self.title, key, n)


_ids: _Ids | None = None


def _uid(key: str) -> str:
    return _ids(key) if _ids is not None else new_uuid()


def _wire(x0: float, y0: float, x1: float, y1: float) -> str:
    return (
        f'\t(wire\n'
        f'\t\t(pts (xy {_fmt(x0)} {_fmt(y0)}) (xy {_fmt(x1)} {_fmt(y1)}))\n'
        f'\t\t(stroke (width 0) (type default))\n'
        f'\t\t(uuid "{_uid(f"wire:{_fmt(x0)},{_fmt(y0)},{_fmt(x1)},{_fmt(y1)}")}")\n'
        f'\t)\n'
    )


def _label(name: str, x: float, y: float, rot: int, justify: str, vjust: str = "bottom") -> str:
    return (
        f'\t(label "{name}"\n'
        f'\t\t(at {_fmt(x)} {_fmt(y)} {rot})\n'
        f'\t\t(effects (font (size {_FONT} {_FONT})) (justify {justify} {vjust}))\n'
        f'\t\t(uuid "{_uid(f"label:{name}:{_fmt(x)},{_fmt(y)}")}")\n'
        f'\t)\n'
    )


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


def _hat(net: str, x: float, y: float, gnd: bool, rot: int = 0, lib: str | None = None, value: str | None = None) -> str:
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
    return (
        f'\t(symbol (lib_id "{lib}") (at {_fmt(x)} {_fmt(y)} {rot}) (unit 1)\n'
        f'\t\t(in_bom yes) (on_board yes) (dnp no)\n'
        f'\t\t(uuid "{uid}")\n'
        f'\t\t(property "Reference" "{pwr}" (at {_fmt(rwx)} {_fmt(rwy)} 0)\n'
        f'\t\t\t(effects (font (size 1.27 1.27)) hide)\n'
        f'\t\t)\n'
        f'\t\t(property "Value" "{net}" (at {_fmt(vwx)} {_fmt(vwy)} 0)\n'
        f'\t\t\t(effects (font (size 1.27 1.27)))\n'
        f'\t\t)\n'
        f'\t\t(property "Footprint" "" (at {_fmt(x)} {_fmt(y)} 0)\n'
        f'\t\t\t(effects (font (size 1.27 1.27)) hide)\n'
        f'\t\t)\n'
        f'\t\t(property "Datasheet" "" (at {_fmt(x)} {_fmt(y)} 0)\n'
        f'\t\t\t(effects (font (size 1.27 1.27)) hide)\n'
        f'\t\t)\n'
        f'\t\t(pin "1" (uuid "{_uid(f"hatpin:{net}:{_fmt(x)},{_fmt(y)}")}"))\n'
        f'\t)\n'
    )


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


def _instance(part: Part) -> str:
    pins = "".join(
        f'\t\t(pin "{p.number}" (uuid "{_uid(f"pin:{part.ref}:{p.number}")}"))\n' for p in part.pins
    )
    rot = int(part.rot) % 360
    mirror = f"\n\t\t(mirror {part.mirror})" if part.mirror in ("x", "y") else ""
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
    rj = f" (justify {rjust})" if rjust else ""
    vj = f" (justify {vjust})" if vjust else ""
    # A field's angle adds to the symbol's; KiCad never draws text upside
    # down, so the visual angle is (field + symbol) mod 180.
    rang = (rvis - rot) % 180
    vang = (vvis - rot) % 180
    return (
        f'\t(symbol\n'
        f'\t\t(lib_id "{part.lib_id}")\n'
        f'\t\t(at {_fmt(part.x)} {_fmt(part.y)} {rot}){mirror}\n'
        f'\t\t(unit 1)\n'
        f'\t\t(exclude_from_sim no)\n'
        f'\t\t(in_bom yes)\n'
        f'\t\t(on_board yes)\n'
        f'\t\t(dnp no)\n'
        f'\t\t(uuid "{_uid(f"sym:{part.ref}")}")\n'
        f'\t\t(property "Reference" "{part.ref}"\n'
        f'\t\t\t(at {_fmt(rwx)} {_fmt(rwy)} {rang if part.text_ref else ang})\n'
        f'\t\t\t(effects (font (size 1.27 1.27)){rj})\n'
        f'\t\t)\n'
        f'\t\t(property "Value" "{part.display}"\n'
        f'\t\t\t(at {_fmt(vwx)} {_fmt(vwy)} {vang if part.text_val else ang})\n'
        f'\t\t\t(effects (font (size 1.27 1.27)){vj})\n'
        f'\t\t)\n'
        f"{pins}"
        f'\t)\n'
    )


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
    tag: str = ""  # placement this belongs to, so it can be taken back
    rot: int | None = None  # a power symbol's rotation, to re-score it in place


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
        self.segs: list[tuple[float, float, float, float, str]] = []
        self.seg_tags: list[str] = []
        self.seg_boxes: list[Box] = []
        self.occupants: list[_Occupant] = []
        # Label anchors and power-symbol pins: a wire through or onto one connects.
        self.anchors: list[tuple[float, float, str, str]] = []
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

    # -- connectivity --------------------------------------------------------
    def on_pin_end(self, x: float, y: float) -> bool:
        return any(_near(x, y, px, py) for px, py in self._pins_at_x(x))

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
        self._stale = True

    def segment_ok(self, x0: float, y0: float, x1: float, y1: float, net: str = "") -> bool:
        """Interior off every pin end, every label anchor and every symbol's
        graphics, and no contact with another net's wire other than a plain
        crossing (KiCad joins collinear wires that touch, and a T without a
        junction misleads)."""
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
        for ax, ay, anet, _tag in self.anchors:
            if _inside_segment(ax, ay, x0, y0, x1, y1):
                return False  # a symbol pin or label on a wire's interior, any net: never drawn
            if anet != net and (_near(ax, ay, x0, y0) or _near(ax, ay, x1, y1)):
                return False
        if any(_segment_crosses_box(x0, y0, x1, y1, box) for box in self.cores):
            return False
        self._index()
        # Against other nets' wires: no collinear contact, no endpoint of
        # either on the other's interior, no crossing.
        if vertical:
            x = x0
            for sx, slo, shi, snet in self.vsegs:
                if snet != net and abs(sx - x) < _EPS and max(lo, slo) <= min(hi, shi) + _EPS:
                    return False
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

    def point_free(self, x: float, y: float, net: str) -> bool:
        """A bend, stub end or label anchor may not touch another net."""
        if self.on_pin_end(x, y):
            return False
        if any(anet != net and _near(x, y, ax, ay) for ax, ay, anet, _t in self.anchors):
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

    def hard_cost(self, box: Box, net: str = "") -> float:
        """Overlap with what is really drawn: symbols, pins, text, other nets'
        wires. Reservations and the net's own wires do not count. Zero means
        the box is clean."""
        bx0, by0, bx1, by1 = box
        total = 0.0
        for o in self.occupants:
            if o.kind == "reserve":
                continue
            ox0, oy0, ox1, oy1 = o.box
            if ox1 <= bx0 or ox0 >= bx1 or oy1 <= by0 or oy0 >= by1:
                continue
            total += (min(bx1, ox1) - max(bx0, ox0)) * (min(by1, oy1) - max(by0, oy0))
        for (sx0, sy0, sx1, sy1), seg in zip(self.seg_boxes, self.segs):
            if seg[4] == net or sx1 <= bx0 or sx0 >= bx1 or sy1 <= by0 or sy0 >= by1:
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


def _prop_box(text: str, x: float, y: float, just: str | None) -> Box:
    w = _text_w(text) + 0.4
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


def _hat_candidates(sheet: _Sheet, members: list[_Site], net: str, gnd: bool, text: str | None = None) -> list[_HatCand]:
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
        if natural:
            for length in (0.0, 2.54, 5.08):
                cands.append(([(s.x, s.y), (s.x + ox * length, s.y + oy * length)], 0))
        elif vertical:
            for length in (2.54, 5.08, 7.62):
                cands.append(([(s.x, s.y), (s.x + ox * length, s.y + oy * length)], 180))
        else:
            for length in (2.54, 5.08, 7.62, 10.16):
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
        for pts, rot in cands:
            hx, hy = pts[-1]
            moving = [p for p in pts[1:]]
            if moving and not _near(pts[0][0], pts[0][1], hx, hy):
                if not sheet.path_ok(pts, net) or not sheet.point_free(hx, hy, net):
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
    return found


def _place_hat(
    sheet: _Sheet, members: list[_Site], net: str, gnd: bool, tag: str = "", lib: str | None = None, value: str | None = None
) -> list[str]:
    cands = _hat_candidates(sheet, members, net, gnd, value)
    if cands:
        return _commit_hat(sheet, cands[0], net, gnd, tag, lib, value)
    s = members[0]
    return _commit_hat(sheet, ((1, 0.0), s, [(s.x, s.y)], 0), net, gnd, tag, lib, value)


def _commit_hat(
    sheet: _Sheet, cand: _HatCand, net: str, gnd: bool, tag: str, lib: str | None = None, value: str | None = None
) -> list[str]:
    _k, s, pts, rot = cand
    hx, hy = pts[-1]
    out = sheet.add(pts, net, tag) if len(pts) > 1 else []
    out.append(_hat(net, hx, hy, gnd=gnd, rot=rot, lib=lib, value=value))
    sheet.occupy(_hat_box(net, hx, hy, gnd, rot, value), "hat", s.part.ref, net, tag)
    sheet.occupants[-1].rot = rot
    sheet.anchor(hx, hy, net, tag)
    return out


def _place_stub_label(sheet: _Sheet, s: _Site, net: str, tag: str = "") -> list[str]:
    """Label on a stub off a lone pin, text on either side of the wire; a spot
    that touches nothing drawn wins, else the least overlap. With no clean stub
    at all the label sits on the pin end, which also connects."""
    ox, oy = pin_outward(s.part, s.pin)
    rot, just = _label_pose(ox, oy)
    owner = f"{s.part.ref}.{s.pin.number}"
    best: tuple[tuple[int, float], float, str] | None = None
    for length in (0.0, 2.54, 5.08, 6.35, 7.62, 8.89, 10.16, 12.7, 15.24):
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
        return [_label(net, s.x, s.y, rot, just)]
    _k, length, vjust = best
    ex, ey = s.x + ox * length, s.y + oy * length
    out = sheet.add([(s.x, s.y), (ex, ey)], net, tag) if length else []
    out.append(_label(net, ex, ey, rot, just, vjust))
    sheet.occupy(_label_box(net, ex, ey, rot, just, vjust), "label", owner, net, tag)
    sheet.anchor(ex, ey, net, tag)
    return out


def _place_wire_label(sheet: _Sheet, segs: list[Box], net: str, owner: str, tag: str = "") -> list[str]:
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
    sheet.occupy(_label_box(net, mx, my, rot, just, vjust), "label", owner, net, tag)
    sheet.anchor(mx, my, net, tag)
    return [_label(net, mx, my, rot, just, vjust)]


def _junctions(sheet: _Sheet) -> list[str]:
    """Junction dots where eeschema would put them: a wire ending on another
    wire of its own net (a symbol's stub leaving a route), three or more
    wire ends at one point, or a pin end with two wires. Same-net only by
    construction - other nets' wires never touch."""
    sheet._index()
    ends: dict[tuple[int, int], tuple[float, float, int]] = {}
    for x0, y0, x1, y1, _net in sheet.segs:
        for x, y in ((x0, y0), (x1, y1)):
            k = (_bkey(x), _bkey(y))
            px, py, n = ends.get(k, (x, y, 0))
            ends[k] = (px, py, n + 1)
    out: list[str] = []
    for (px, py, n) in ends.values():
        on_pin = sheet.on_pin_end(px, py)
        if n >= 3 or (on_pin and n >= 2) or sheet.on_wire_interior(px, py):
            out.append(
                f'\t(junction\n'
                f'\t\t(at {_fmt(px)} {_fmt(py)})\n'
                f'\t\t(diameter 0)\n'
                f'\t\t(color 0 0 0 0)\n'
                f'\t\t(uuid "{_uid(f"junction:{_fmt(px)},{_fmt(py)}")}")\n'
                f'\t)\n'
            )
    return out


def _joint_hats(sheet: _Sheet, a: tuple, b: tuple, drawn: dict[str, list[str]]) -> None:
    """Place two power symbols together: the pair whose spots are cleanest, and
    among clean pairs the least overlap. Only better than what each had alone
    is kept. Labels within reach are lifted first - they are re-placed around
    the symbols in the pass that follows, so they should not decide this."""
    tag_a, mem_a, net_a, gnd_a = a
    tag_b, mem_b, net_b, gnd_b = b
    sites = list(mem_a) + list(mem_b)
    for o in list(sheet.occupants):
        if o.kind == "label" and o.tag:
            cx, cy = (o.box[0] + o.box[2]) / 2.0, (o.box[1] + o.box[3]) / 2.0
            if any(math.hypot(cx - st.x, cy - st.y) <= 12.7 for st in sites):
                sheet.remove(o.tag)
    before = _pair_key(sheet, a, b)
    sheet.remove(tag_a)
    sheet.remove(tag_b)
    best = None
    # The partner is enumerated afresh for each of our candidates: its best
    # spot next to us (a supply hung below its pin) is rarely among its best
    # spots alone, so a fixed shortlist would miss the pair that works.
    for ca in _hat_candidates(sheet, mem_a, net_a, gnd_a)[:10]:
        _commit_hat(sheet, ca, net_a, gnd_a, tag_a)
        cbs = _hat_candidates(sheet, mem_b, net_b, gnd_b)
        sheet.remove(tag_a)
        if not cbs:
            continue
        cb = cbs[0]
        joint = (ca[0][0] + cb[0][0], ca[0][1] + cb[0][1])
        if best is None or joint < best[0]:
            best = (joint, ca)
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


def _text_box(text: str, x: float, y: float, just: str | None, vis: int) -> Box:
    """Where a field's text lands: horizontal from its anchor, or running up
    from it when drawn at 90 degrees (KiCad's rotated field, justify left)."""
    if vis == 90:
        w = _text_w(text) + 0.4
        return (x - _TEXT_H / 2.0, y - w, x + _TEXT_H / 2.0, y)
    return _prop_box(text, x, y, just)


def _place_box_text(sheet: _Sheet, part: Part) -> None:
    """An IC's Reference and Value: where the library puts them when that is
    clear, else the least crowded of a few spots around the body."""
    sheet.occupants = [o for o in sheet.occupants if not (o.owner == part.ref and o.kind in ("ref", "value"))]
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
    best = None
    for i, (_name, r, v) in enumerate(cands):
        c = sheet.cost(_text_box(ref, *r)) + sheet.cost(_text_box(val, *v)) + 0.05 * i
        if best is None or c < best[0]:
            best = (c, r, v)
    assert best is not None
    _c, r, v = best
    part.text_ref = r
    part.text_val = v
    sheet.occupy(_text_box(ref, *r), "ref", part.ref)
    sheet.occupy(_text_box(val, *v), "value", part.ref)


def _place_passive_text(sheet: _Sheet, part: Part) -> None:
    """Reference and Value beside a 2-pin part on the side that overlaps least.
    Beside a part that stands vertically they may also stand vertically -
    how an engineer labels a hanging resistor when the row beside it is busy."""
    sheet.occupants = [o for o in sheet.occupants if not (o.owner == part.ref and o.kind in ("ref", "value"))]
    x0, y0, x1, y1 = body_aabb(part)
    cx, cy = (x0 + x1) / 2.0, (y0 + y1) / 2.0
    ref, val = part.ref, part.display
    Spot = tuple[float, float, str | None, int]
    cands: list[tuple[str, Spot, Spot]] = [
        ("right", (x1 + 0.8, cy - 1.0, "left", 0), (x1 + 0.8, cy + 1.0, "left", 0)),
        ("left", (x0 - 0.8, cy - 1.0, "right", 0), (x0 - 0.8, cy + 1.0, "right", 0)),
        ("above", (cx, y0 - 2.7, None, 0), (cx, y0 - 1.0, None, 0)),
        ("below", (cx, y1 + 1.0, None, 0), (cx, y1 + 2.7, None, 0)),
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
    order = cands if vertical else cands[2:4] + cands[:2] + cands[4:]
    best = None
    for i, (side, r, v) in enumerate(order):
        c = sheet.cost(_text_box(ref, *r)) + sheet.cost(_text_box(val, *v)) + 0.01 * i
        c += 0.4 if side.endswith("-rot") else 0.0  # upright reads better when it fits
        if best is None or c < best[0]:
            best = (c, r, v)
    assert best is not None
    _c, r, v = best
    part.text_ref = r
    part.text_val = v
    sheet.occupy(_text_box(ref, *r), "ref", part.ref)
    sheet.occupy(_text_box(val, *v), "value", part.ref)


def _lint(
    sheet: _Sheet,
    parts: list[Part],
    sites: dict[str, list[_Site]],
    unions: dict[str, _Union],
    power_nets: set[str],
    ground_nets: set[str] = frozenset(),
    kinds: dict[str, str] | None = None,
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
                    f"nothing was clear above it; give the parts beside it more gap"
                )
            if kind == "ground" and y_self < y_other - _EPS:
                issues.append(
                    f"{p.ref} stands the wrong way up: its {pin.net} end is at the top - "
                    f"nothing was clear below it; give the parts beside it more gap"
                )
    for p in parts:
        shoved = getattr(p, "shoved_mm", 0.0)
        if shoved > 10 * 1.27:  # a stagger of a few grid steps is normal; more is a lane taken
            issues.append(
                f"{p.ref} was pushed {shoved:.0f} mm along its attach to clear {getattr(p, 'shoved_by', '?')}: "
                f"they want the same side of the same part - hang one of them off a different pin, "
                f"or down from its row (SchPlace(..., rotate=0)), or give it its own SchPlace() spot"
            )
        for other in sorted(getattr(p, "stuck_on", set())):
            issues.append(
                f"{p.ref} overlaps {other} at every distance along its attach: hang {p.ref} off a different pin or side"
            )
        wire = getattr(p, "stuck_wire", None)
        if wire and wire[1]:
            issues.append(
                f"{p.ref}: its wire from {wire[0]} would run through {', '.join(wire[1])} at every distance - "
                f"that pin's free side is the other way; attach {p.ref} to a pin on this side, or to the far end of the run"
            )
    for p in parts:
        if p.kind != "box":
            continue
        wide = [pin.number for pin in p.pins if _text_w(pin.number) > pin.length + 0.5]
        if wide:
            shown = ", ".join(wide[:4]) + (", ..." if len(wide) > 4 else "")
            issues.append(
                f"{p.ref}: pin numbers {shown} are wider than their {p.pins[0].length:g} mm pins "
                f"and print into the body - that is the .kicad_sym; lengthen the pins or hide numbers"
            )
    for i, a in enumerate(parts):
        for b in parts[i + 1 :]:
            if _overlap_area(body_aabb(a), body_aabb(b)) > 0.0:
                issues.append(f"{a.ref} overlaps {b.ref}: move one of them")
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
                how = (
                    f"SchPlace({ref!r}, to={s.part.ref + '.' + s.pin.name!r})"
                    if not _two_pin(s.part)
                    else f"SchPlace({ref!r}, along={s.part.ref + '.' + s.pin.number!r}, side=...)"
                )
                issues.append(
                    f"{o.net}: {ref} carries its own {o.net} symbol though {s.part.ref}.{s.pin.name} is in line "
                    f"{d:.0f} mm away and not wired to it; {how} shares that node instead"
                )
                break
    through: dict[tuple[str, str, str], tuple[_Occupant, list[str]]] = {}
    for x0, y0, x1, y1, net in sheet.segs:
        for o in occ:
            if o.kind in text_kinds | {"pin"} and o.net != net and _overlap_area(_seg_box(x0, y0, x1, y1), o.box) > 0.6:
                entry = through.setdefault((o.kind, o.owner, o.net), (o, []))
                if net not in entry[1]:
                    entry[1].append(net)
    for o, nets in through.values():
        issues.append(f"wire{'s' if len(nets) > 1 else ''} {', '.join(nets)} run{'' if len(nets) > 1 else 's'} through {_describe(o)}")
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
                if d < 20.0 and (abs(a.x - b.x) < _EPS or abs(a.y - b.y) < _EPS):
                    issues.append(
                        f"{net}: {a.part.ref}.{a.pin.name} and {b.part.ref}.{b.pin.name} are in line "
                        f"{d:.0f} mm apart but joined by labels: something sits between them"
                    )
    return {"issues": issues, "count": len(issues), "notes": notes}


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
            return f" - no clear spot for {t.owner}'s name; give SchPlace({t.owner!r}) more gap or move it"
    for t, other in ((a, b), (b, a)):
        if t.kind == "hat":
            hangers = _HANGERS.get(t.owner, [])
            if hangers:
                return (
                    f" - no clear spot for the {t.net} symbol at {t.owner}: {', '.join(hangers)} hang off {t.owner} "
                    f"at the default gap and box it in; give them gap=15.24 or hang them off another node"
                )
            return f" - no clear spot for the {t.net} symbol at {t.owner}; give {t.owner} more room"
    for t, other in ((a, b), (b, a)):
        if t.kind == "label":
            return f" - move the parts on {t.net} apart"
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
                # The number sits along the pin; a long number is clipped to it.
                half = min(_text_w(pin.number) / 2.0, pin.length / 2.0)
                mx, my = (ex + bx) / 2.0, (ey + by) / 2.0
                if abs(ox) > abs(oy):
                    box = (mx - half, my - _TEXT_H, mx + half, my)
                else:
                    box = (mx - _TEXT_H, my - half, mx, my + half)
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
        for _d, i, j in sorted(rails):
            if uf.find(i) == uf.find(j):
                continue
            pts = [(sts[i].x, sts[i].y), (sts[j].x, sts[j].y)]
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
                if not sheet.path_ok(pts, net):
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
                if not sheet.path_ok(pts, net):
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
    for net, sts in sites.items():
        if is_power_net(net, kinds):
            continue
        uf = unions[net]
        for group in uf.groups():
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
                jobs.append((tag, lambda t=tag, sg=segs, n=net, o=f"{s.part.ref}.{s.pin.number}": _place_wire_label(sheet, sg, n, o, t)))
            else:
                jobs.append((tag, lambda t=tag, st=s, n=net: _place_stub_label(sheet, st, n, t)))
    for net, sts in sites.items():
        if not is_power_net(net, kinds):
            continue
        gnd = is_ground_net(net, kinds)
        for group in unions[net].groups():
            members = [sts[i] for i in group]
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
    one_pass()
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
                out.append(
                    f'\t(no_connect\n'
                    f'\t\t(at {_fmt(x)} {_fmt(y)})\n'
                    f'\t\t(uuid "{_uid(f"nc:{p.ref}:{pin.number}")}")\n'
                    f'\t)\n'
                )
    two_pin = [p for p in parts if _two_pin(p)]
    boxes = [p for p in parts if not _two_pin(p)]
    for _round in range(2):
        for p in boxes:
            _place_box_text(sheet, p)
        for p in two_pin:
            _place_passive_text(sheet, p)
    power_nets = {n for n in sites if is_power_net(n, kinds)}
    ground_nets = {n for n in sites if is_ground_net(n, kinds)}
    return out, _lint(sheet, parts, sites, unions, power_nets, ground_nets, kinds)


def emit_from_design(design: Design, *, title: str = "", report: dict | None = None) -> str:
    """The sheet text. ``report`` (if given) receives the readability issues
    and every part's pose. The same design gives the same text, byte for byte."""
    global _ids
    _ids = _Ids(title or "pcbc")
    try:
        return _emit(design, title=title, report=report)
    finally:
        _ids = None


def _emit(design: Design, *, title: str, report: dict | None) -> str:
    kinds = {n.name: n.kind for n in design.nets.values()}
    parts = _parts_from_design(design, kinds)
    apply_sch_places(design, parts)
    if report is not None:
        report["parts"] = [
            {"ref": p.ref, "x": round(p.x, 3), "y": round(p.y, 3), "rot": p.rot, "mirror": p.mirror}
            for p in parts
        ]
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
    limits = {n.name: float(n.wire_mm) for n in design.nets.values() if n.wire_mm is not None}
    annotations, lint = _annotate(parts, kinds, limits, design.sch_wire_mm)
    if report is not None:
        report.update(lint)
    body: list[str] = [_instance(p) for p in parts]
    body.extend(annotations)
    max_x, max_y = 100.0, 80.0
    for p in parts:
        x0, y0, x1, y1 = world_aabb(p)
        max_x = max(max_x, x1 + 20)
        max_y = max(max_y, y1 + 20)
    paper = "A4" if max_x < 280 and max_y < 190 else "A3" if max_x < 400 else "A2"
    uid = _uid("sheet")
    title = title or "pcbc"
    body_text = "".join(body)
    n = 0

    def _number(_m: re.Match) -> str:
        nonlocal n
        n += 1
        return f'"#PWR{n:03d}"'

    body_text = re.sub(r'"#PWR\?"', _number, body_text)
    n = 0
    body_text = re.sub(r'"#FLG\?"', lambda m: _number(m).replace("#PWR", "#FLG"), body_text)
    return (
        f'(kicad_sch\n'
        f'\t(version 20260306)\n'
        f'\t(generator "pcbc")\n'
        f'\t(generator_version "0.4")\n'
        f'\t(uuid "{uid}")\n'
        f'\t(paper "{paper}")\n'
        f'\t(title_block\n'
        f'\t\t(title "{title}")\n'
        f'\t)\n'
        f'\t(lib_symbols\n'
        + "".join(libs)
        + "\t)\n"
        + body_text
        + '\t(sheet_instances\n'
        '\t\t(path "/" (page "1"))\n'
        '\t)\n'
        '\t(embedded_fonts no)\n'
        ")\n"
    )


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
