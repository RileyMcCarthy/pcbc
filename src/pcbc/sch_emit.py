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
    lib_to_sheet,
    pin_outward,
    pin_world,
    world_aabb,
)
from .sexp import matching_paren, new_uuid
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


def parse_components_rich(text: str) -> list[dict]:
    comps, _nets = parse_netlist(text)
    by_ref = {c["ref"]: dict(c) for c in comps}
    pos = 0
    while True:
        j = text.find("(comp ", pos)
        if j < 0:
            break
        k = matching_paren(text, j)
        block = text[j : k + 1]
        pos = k + 1
        rm = re.search(r'\(ref "([^"]+)"\)', block)
        if not rm or rm.group(1) not in by_ref:
            continue
        rec = by_ref[rm.group(1)]
        props = dict(_PROP.findall(block))
        rec["display"] = props.get("value") or props.get("description") or rec["value"]
        rec["libpart"] = rec["value"]
    return list(by_ref.values())


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
    hw: float = 8.0
    hh: float = 6.0
    bbox: tuple[float, float, float, float] = (-8.0, -6.0, 8.0, 6.0)
    body_bbox: tuple[float, float, float, float] = (-8.0, -6.0, 8.0, 6.0)
    core_bbox: tuple[float, float, float, float] = (-6.0, -4.0, 6.0, 4.0)
    lib_sexp: str | None = None
    placed: bool = False
    prop_ref: tuple[float, float] = (0.0, 0.0)
    prop_val: tuple[float, float] = (0.0, 0.0)
    text_ref: tuple[float, float, str | None] | None = None
    text_val: tuple[float, float, str | None] | None = None


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
    return """		(symbol "power:GND" (power) (pin_names (offset 0)) (in_bom yes) (on_board yes)
			(property "Reference" "#PWR" (at 0 -6.35 0)
				(effects (font (size 1.27 1.27)) hide)
			)
			(property "Value" "GND" (at 0 -3.81 0)
				(effects (font (size 1.27 1.27)))
			)
			(symbol "GND_0_1"
				(polyline
					(pts (xy 0 0) (xy 0 -1.27) (xy 1.27 -1.27) (xy 0 -2.54) (xy -1.27 -1.27) (xy 0 -1.27))
					(stroke (width 0) (type default))
					(fill (type none))
				)
			)
			(symbol "GND_1_1"
				(pin power_in line (at 0 0 270) (length 0) hide
					(name "GND" (effects (font (size 1.27 1.27))))
					(number "1" (effects (font (size 1.27 1.27))))
				)
			)
		)
"""


def _lib_vcc() -> str:
    return """		(symbol "power:VCC" (power) (pin_names (offset 0)) (in_bom yes) (on_board yes)
			(property "Reference" "#PWR" (at 0 -3.81 0)
				(effects (font (size 1.27 1.27)) hide)
			)
			(property "Value" "VCC" (at 0 3.556 0)
				(effects (font (size 1.27 1.27)))
			)
			(symbol "VCC_0_1"
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
			(symbol "VCC_1_1"
				(pin power_in line (at 0 0 90) (length 0) hide
					(name "VCC" (effects (font (size 1.27 1.27))))
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


def _build_parts(
    net_text: str,
    pin_maps: list[tuple[str, dict[str, str]]] | None = None,
    pkgs: list[PkgLib] | None = None,
) -> list[Part]:
    comps = parse_components_rich(net_text)
    _c, nets = parse_netlist(net_text)
    lookup = pin_to_net(nets)
    pins_by_ref: dict[str, dict[str, str]] = defaultdict(dict)
    for (ref, pin), net in lookup.items():
        pins_by_ref[ref][pin] = net
    maps = pin_maps or []
    pkgs = pkgs or []
    parts: list[Part] = []
    for c in comps:
        ref = c["ref"]
        kind = _kind(ref)
        pnets = pins_by_ref.get(ref, {})
        lib_sexp = None
        if kind in ("r", "c", "l"):
            pins = _passive_pins(kind, pnets)
            # Keep netlist pin numbers if present.
            for p in pins:
                if p.number in pnets:
                    p.net = pnets[p.number]
            lib_id = kind.upper()
            hw, hh = 2.0, 4.0
        else:
            if not pnets:
                pnets = {"1": ""}
            pkg = _pkg_for(c, pkgs)
            if pkg and pkg.pins:
                pins = [
                    PinDef(p.number, p.name, p.lx, p.ly, p.rot, pnets.get(p.number, ""))
                    for p in pkg.pins
                ]
                lib_id = pkg.lib_id or re.sub(r"[^A-Za-z0-9_.-]", "_", c["libpart"] or ref)[:40]
                lib_sexp = pkg.sexp
            else:
                pins = _box_pins(pnets, _pin_map_for(c, maps))
                lib_id = re.sub(r"[^A-Za-z0-9_.-]", "_", c["libpart"] or ref)[:40]
            xs = [abs(p.lx) for p in pins]
            ys = [abs(p.ly) for p in pins]
            hw = (max(xs) if xs else 16.0) + 2.0
            hh = (max(ys) if ys else 8.0) + 2.0
        parts.append(
            Part(
                ref=ref,
                lib_id=lib_id,
                display=c.get("display") or c["value"],
                kind=kind,
                pins=pins,
                hw=hw,
                hh=hh,
                lib_sexp=lib_sexp,
            )
        )
    return parts


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


def _wire(x0: float, y0: float, x1: float, y1: float) -> str:
    return (
        f'\t(wire\n'
        f'\t\t(pts (xy {_fmt(x0)} {_fmt(y0)}) (xy {_fmt(x1)} {_fmt(y1)}))\n'
        f'\t\t(stroke (width 0) (type default))\n'
        f'\t\t(uuid "{new_uuid()}")\n'
        f'\t)\n'
    )


def _label(name: str, x: float, y: float, rot: int, justify: str, vjust: str = "bottom") -> str:
    return (
        f'\t(label "{name}"\n'
        f'\t\t(at {_fmt(x)} {_fmt(y)} {rot})\n'
        f'\t\t(effects (font (size {_FONT} {_FONT})) (justify {justify} {vjust}))\n'
        f'\t\t(uuid "{new_uuid()}")\n'
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


def _hat(net: str, x: float, y: float, gnd: bool, rot: int = 0) -> str:
    """Place the whole power symbol on the pin. Do not explode it into a wire + graphic."""
    lib = "power:GND" if gnd else "power:VCC"
    uid = new_uuid()
    ref_l = _GND_REF if gnd else _VCC_REF
    val_l = _GND_VAL if gnd else _VCC_VAL
    rx, ry = _rot_xy(*ref_l, rot)
    vx, vy = _rot_xy(*val_l, rot)
    # Eeschema stores property Y as at_y − library_y (Y-up library, Y-down sheet).
    rwx, rwy = x + rx, y - ry
    vwx, vwy = x + vx, y - vy
    return (
        f'\t(symbol (lib_id "{lib}") (at {_fmt(x)} {_fmt(y)} {rot}) (unit 1)\n'
        f'\t\t(in_bom yes) (on_board yes) (dnp no)\n'
        f'\t\t(uuid "{uid}")\n'
        f'\t\t(property "Reference" "#PWR{uid[:8]}" (at {_fmt(rwx)} {_fmt(rwy)} 0)\n'
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
        f'\t\t(pin "1" (uuid "{new_uuid()}"))\n'
        f'\t)\n'
    )


def _prop_world(part: Part, lib_xy: tuple[float, float]) -> tuple[float, float]:
    """Instance property in sheet coords."""
    dx, dy = lib_to_sheet(lib_xy[0], lib_xy[1], part.rot)
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
        f'\t\t(pin "{p.number}" (uuid "{new_uuid()}"))\n' for p in part.pins
    )
    rot = int(part.rot) % 360
    # KiCad turns field text with a 90/270 symbol; a 90 field angle undoes that.
    ang = 90 if rot in (90, 270) else 0
    rjust = vjust = None
    if part.text_ref and part.text_val:
        rwx, rwy, rjust = part.text_ref
        vwx, vwy, vjust = part.text_val
    elif part.kind in ("r", "c", "l", "d"):
        (rwx, rwy), (vwx, vwy) = _passive_label_xy(part)
    else:
        rwx, rwy = _prop_world(part, part.prop_ref)
        vwx, vwy = _prop_world(part, part.prop_val)
    rj = f" (justify {rjust})" if rjust else ""
    vj = f" (justify {vjust})" if vjust else ""
    return (
        f'\t(symbol\n'
        f'\t\t(lib_id "{part.lib_id}")\n'
        f'\t\t(at {_fmt(part.x)} {_fmt(part.y)} {rot})\n'
        f'\t\t(unit 1)\n'
        f'\t\t(exclude_from_sim no)\n'
        f'\t\t(in_bom yes)\n'
        f'\t\t(on_board yes)\n'
        f'\t\t(dnp no)\n'
        f'\t\t(uuid "{new_uuid()}")\n'
        f'\t\t(property "Reference" "{part.ref}"\n'
        f'\t\t\t(at {_fmt(rwx)} {_fmt(rwy)} {ang})\n'
        f'\t\t\t(effects (font (size 1.27 1.27)){rj})\n'
        f'\t\t)\n'
        f'\t\t(property "Value" "{part.display}"\n'
        f'\t\t\t(at {_fmt(vwx)} {_fmt(vwy)} {ang})\n'
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
        self.occupants: list[_Occupant] = []

    # -- connectivity --------------------------------------------------------
    def on_pin_end(self, x: float, y: float) -> bool:
        return any(_near(x, y, px, py) for px, py in self.pin_ends)

    def segment_ok(self, x0: float, y0: float, x1: float, y1: float, net: str = "") -> bool:
        """Interior off every pin end and every symbol's graphics, and no contact
        with another net's wire other than a plain crossing (KiCad joins
        collinear wires that touch, and a T without a junction misleads)."""
        if any(_inside_segment(px, py, x0, y0, x1, y1) for px, py in self.pin_ends):
            return False
        if any(_segment_crosses_box(x0, y0, x1, y1, box) for box in self.cores):
            return False
        for sx0, sy0, sx1, sy1, snet in self.segs:
            if snet == net:
                continue
            if _collinear_touch(x0, y0, x1, y1, sx0, sy0, sx1, sy1):
                return False
            if _inside_segment(sx0, sy0, x0, y0, x1, y1) or _inside_segment(sx1, sy1, x0, y0, x1, y1):
                return False
            if _inside_segment(x0, y0, sx0, sy0, sx1, sy1) or _inside_segment(x1, y1, sx0, sy0, sx1, sy1):
                return False
        return True

    def point_free(self, x: float, y: float, net: str) -> bool:
        """A bend, stub end or label anchor may not touch another net."""
        if self.on_pin_end(x, y):
            return False
        for sx0, sy0, sx1, sy1, snet in self.segs:
            if snet == net:
                continue
            if (
                _near(x, y, sx0, sy0)
                or _near(x, y, sx1, sy1)
                or _inside_segment(x, y, sx0, sy0, sx1, sy1)
            ):
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

    def add(self, pts: list[tuple[float, float]], net: str) -> list[str]:
        out: list[str] = []
        for (ax, ay), (bx, by) in zip(pts, pts[1:]):
            if _near(ax, ay, bx, by):
                continue
            self.segs.append((ax, ay, bx, by, net))
            out.append(_wire(ax, ay, bx, by))
        return out

    # -- readability ----------------------------------------------------------
    def occupy(self, box: Box, kind: str, owner: str, net: str = "") -> None:
        self.occupants.append(_Occupant(box, kind, owner, net))

    def cost(self, box: Box, net: str = "") -> float:
        """Overlap area with what is drawn. Own-net wires count less: a label
        sits on its wire by design. Reservations (where a power symbol or a
        part's text will probably go) count half, and not for their own net."""
        total = 0.0
        for o in self.occupants:
            if o.kind == "reserve":
                if o.net and o.net == net:
                    continue
                total += _overlap_area(box, o.box) * 0.5
                continue
            total += _overlap_area(box, o.box)
        for x0, y0, x1, y1, snet in self.segs:
            total += _overlap_area(box, _seg_box(x0, y0, x1, y1)) * (0.3 if snet == net else 1.0)
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


def _hat_box(net: str, x: float, y: float, gnd: bool, rot: int) -> Box:
    hw = max(1.5, _text_w(net) / 2.0 + 0.2)
    down = gnd if rot == 0 else not gnd  # GND hangs down; a supply points up
    return (x - hw, y, x + hw, y + 4.8) if down else (x - hw, y - 4.8, x + hw, y)


# -- placements ---------------------------------------------------------------
def _reserve(sheet: _Sheet, parts: list[Part], sites: dict[str, list[_Site]], kinds: dict[str, str] | None) -> None:
    """Before routing: pencil in where power symbols and passive text will want
    to be, so wires steer around those spots instead of through them."""
    for net, sts in sites.items():
        if not is_power_net(net, kinds):
            continue
        gnd = is_ground_net(net, kinds)
        for s in sts:
            ox, oy = pin_outward(s.part, s.pin)
            natural = abs(oy) > abs(ox) and ((gnd and oy > 0) or (not gnd and oy < 0))
            hx, hy = (s.x, s.y) if natural else (s.x + ox * 2.54, s.y + oy * 2.54)
            sheet.occupy(_hat_box(net, hx, hy, gnd, 0), "reserve", s.part.ref, net)
    for part in parts:
        if part.kind not in ("r", "c", "l", "d") or not part.pins:
            continue
        x0, y0, x1, y1 = body_aabb(part)
        cx, cy = (x0 + x1) / 2.0, (y0 + y1) / 2.0
        vertical = abs(pin_outward(part, part.pins[0])[1]) > 0.5
        if vertical:
            boxes = [_prop_box(part.ref, x1 + 0.8, cy - 1.0, "left"), _prop_box(part.display, x1 + 0.8, cy + 1.0, "left")]
        else:
            boxes = [_prop_box(part.ref, cx, y0 - 2.7, None), _prop_box(part.display, cx, y0 - 1.0, None)]
        for b in boxes:
            sheet.occupy(b, "reserve", part.ref)


def _place_hat(sheet: _Sheet, members: list[_Site], net: str, gnd: bool) -> list[str]:
    """Power symbol for one connected group: on the pin end when the pin already
    points the symbol's way, else on a stub — straight, or with a jog the
    symbol's way — wherever symbol and wires overlap least."""
    best: tuple[float, _Site, list[tuple[float, float]], int] | None = None
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
            for length in (2.54, 3.81, 5.08, 7.62, 10.16, 12.7, 15.24):
                ex, ey = s.x + ox * length, s.y + oy * length
                cands.append(([(s.x, s.y), (ex, ey)], 0))
                for jog in (2.54, 5.08):
                    cands.append(([(s.x, s.y), (ex, ey), (ex, ey + jog_y * jog)], 0))
        for pts, rot in cands:
            hx, hy = pts[-1]
            moving = [p for p in pts[1:]]
            if moving and not _near(pts[0][0], pts[0][1], hx, hy):
                if not sheet.path_ok(pts, net) or not sheet.point_free(hx, hy, net):
                    continue
            c = sheet.cost(_hat_box(net, hx, hy, gnd, rot), net)
            for (ax, ay), (bx, by) in zip(pts, pts[1:]):
                if not _near(ax, ay, bx, by):
                    c += sheet.cost(_seg_box(ax, ay, bx, by), net) + 0.02 * math.hypot(bx - ax, by - ay)
            if best is None or c < best[0]:
                best = (c, s, pts, rot)
    if best is None:
        s = members[0]
        best = (0.0, s, [(s.x, s.y)], 0)
    _c, s, pts, rot = best
    hx, hy = pts[-1]
    out = sheet.add(pts, net) if len(pts) > 1 else []
    out.append(_hat(net, hx, hy, gnd=gnd, rot=rot))
    sheet.occupy(_hat_box(net, hx, hy, gnd, rot), "hat", s.part.ref, net)
    return out


def _place_stub_label(sheet: _Sheet, s: _Site, net: str) -> list[str]:
    """Label on a stub off a lone pin; the stub length that overlaps least wins.
    With no clean stub at all the label sits on the pin end, which also connects."""
    ox, oy = pin_outward(s.part, s.pin)
    rot, just = _label_pose(ox, oy)
    best: tuple[float, float] | None = None
    for k in range(9):
        length = 5.08 + 1.27 * k
        ex, ey = s.x + ox * length, s.y + oy * length
        if not sheet.segment_ok(s.x, s.y, ex, ey, net) or not sheet.point_free(ex, ey, net):
            continue
        c = sheet.cost(_label_box(net, ex, ey, rot, just, "bottom"), net)
        c += sheet.cost(_seg_box(s.x, s.y, ex, ey), net) + 0.02 * length
        if best is None or c < best[0]:
            best = (c, length)
    if best is None:
        box = _label_box(net, s.x, s.y, rot, just, "bottom")
        sheet.occupy(box, "label", f"{s.part.ref}.{s.pin.number}", net)
        return [_label(net, s.x, s.y, rot, just)]
    length = best[1]
    ex, ey = s.x + ox * length, s.y + oy * length
    out = sheet.add([(s.x, s.y), (ex, ey)], net)
    out.append(_label(net, ex, ey, rot, just))
    sheet.occupy(_label_box(net, ex, ey, rot, just, "bottom"), "label", f"{s.part.ref}.{s.pin.number}", net)
    return out


def _place_wire_label(sheet: _Sheet, segs: list[Box], net: str, owner: str) -> list[str]:
    """Label on one of the group's own wires: any midpoint connects, so pick the
    side and segment whose text overlaps least (horizontal runs preferred)."""
    best: tuple[float, float, float, int, str] | None = None
    for ax, ay, bx, by in segs:
        horiz = abs(by - ay) < _EPS
        length = math.hypot(bx - ax, by - ay)
        for t in (0.5, 0.35, 0.65):
            mx, my = ax + (bx - ax) * t, ay + (by - ay) * t
            if not sheet.point_free(mx, my, net):
                continue
            for rot in (0, 90):
                along = (rot == 0) == horiz
                for vjust in ("bottom", "top"):
                    c = sheet.cost(_label_box(net, mx, my, rot, "left", vjust), net)
                    c += 0.0 if rot == 0 else 0.3  # upright text reads better
                    if along:
                        c += max(0.0, _text_w(net) - length) * 0.2  # text longer than its wire
                    else:
                        c += 0.5
                    if best is None or c < best[0]:
                        best = (c, mx, my, rot, vjust)
    if best is None:
        return []
    _c, mx, my, rot, vjust = best
    sheet.occupy(_label_box(net, mx, my, rot, "left", vjust), "label", owner, net)
    return [_label(net, mx, my, rot, "left", vjust)]


def _place_passive_text(sheet: _Sheet, part: Part) -> None:
    """Reference and Value beside a 2-pin part on the side that overlaps least."""
    x0, y0, x1, y1 = body_aabb(part)
    cx, cy = (x0 + x1) / 2.0, (y0 + y1) / 2.0
    ref, val = part.ref, part.display
    cands: list[tuple[str, tuple[float, float, str | None], tuple[float, float, str | None]]] = [
        ("right", (x1 + 0.8, cy - 1.0, "left"), (x1 + 0.8, cy + 1.0, "left")),
        ("left", (x0 - 0.8, cy - 1.0, "right"), (x0 - 0.8, cy + 1.0, "right")),
        ("above", (cx, y0 - 2.7, None), (cx, y0 - 1.0, None)),
        ("below", (cx, y1 + 1.0, None), (cx, y1 + 2.7, None)),
        ("above-left", (x1, y0 - 2.7, "right"), (x1, y0 - 1.0, "right")),
        ("above-right", (x0, y0 - 2.7, "left"), (x0, y0 - 1.0, "left")),
        ("below-left", (x1, y1 + 1.0, "right"), (x1, y1 + 2.7, "right")),
        ("below-right", (x0, y1 + 1.0, "left"), (x0, y1 + 2.7, "left")),
    ]
    vertical = abs(pin_outward(part, part.pins[0])[1]) > 0.5 if part.pins else True
    order = cands if vertical else cands[2:4] + cands[:2] + cands[4:]
    best = None
    for i, (_side, r, v) in enumerate(order):
        c = sheet.cost(_prop_box(ref, *r)) + sheet.cost(_prop_box(val, *v)) + 0.01 * i
        if best is None or c < best[0]:
            best = (c, r, v)
    assert best is not None
    _c, r, v = best
    part.text_ref = r
    part.text_val = v
    sheet.occupy(_prop_box(ref, *r), "ref", part.ref)
    sheet.occupy(_prop_box(val, *v), "value", part.ref)


def _lint(
    sheet: _Sheet,
    parts: list[Part],
    sites: dict[str, list[_Site]],
    unions: dict[str, _Union],
    power_nets: set[str],
) -> dict:
    """What still hurts readability, as things an AI can act on by moving parts."""
    issues: list[str] = []
    for i, a in enumerate(parts):
        for b in parts[i + 1 :]:
            if _overlap_area(body_aabb(a), body_aabb(b)) > 0.0:
                issues.append(f"{a.ref} overlaps {b.ref}: move one of them")
    text_kinds = {"label", "hat", "ref", "value"}
    occ = sheet.occupants
    for i, a in enumerate(occ):
        for b in occ[i + 1 :]:
            if a.kind not in text_kinds and b.kind not in text_kinds:
                continue
            if a.owner == b.owner and {a.kind, b.kind} == {"ref", "value"}:
                continue
            area = _overlap_area(a.box, b.box)
            if area < 0.6:
                continue
            issues.append(f"{_describe(a)} overlaps {_describe(b)}")
    for x0, y0, x1, y1, net in sheet.segs:
        for o in occ:
            if o.kind in text_kinds | {"pin"} and o.net != net and _overlap_area(_seg_box(x0, y0, x1, y1), o.box) > 0.6:
                issues.append(f"wire {net} runs through {_describe(o)}")
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
                if d < 30.0:
                    a, b = sts[i], sts[j]
                    issues.append(
                        f"{net}: {a.part.ref}.{a.pin.name} and {b.part.ref}.{b.pin.name} are "
                        f"{d:.0f} mm apart but joined by labels, no clean wire path; "
                        f"line them up or move them apart"
                    )
    return {"issues": issues, "count": len(issues)}


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


def _annotate(parts: list[Part], kinds: dict[str, str] | None = None) -> tuple[list[str], dict]:
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
                sheet.occupy(_seg_box(ex, ey, bx, by, pad=0.3), "pin", f"{p.ref}.{pin.number}")
                # The number sits along the pin; a long number is clipped to it.
                half = min(_text_w(pin.number) / 2.0, pin.length / 2.0)
                mx, my = (ex + bx) / 2.0, (ey + by) / 2.0
                if abs(ox) > abs(oy):
                    box = (mx - half, my - _TEXT_H, mx + half, my)
                else:
                    box = (mx - _TEXT_H, my - half, mx, my + half)
                sheet.occupy(box, "pintext", f"{p.ref}.{pin.number}")
            rx, ry = _prop_world(p, p.prop_ref)
            vx, vy = _prop_world(p, p.prop_val)
            sheet.occupy(_prop_box(p.ref, rx, ry, None), "ref", p.ref)
            sheet.occupy(_prop_box(p.display, vx, vy, None), "value", p.ref)
        else:
            sheet.occupy(body_aabb(p), "symbol", p.ref)

    _reserve(sheet, parts, sites, kinds)
    unions: dict[str, _Union] = {}
    net_segs: dict[str, list[tuple[int, list[tuple[float, float]]]]] = defaultdict(list)

    for net, sts in sites.items():
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
        # Same side but other pins in between (a USB-C's DP1/DP2): a bus bar
        # just outside the pin ends, one step further out per crowded net.
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
                if span <= 10.16:  # a short bar; far-apart pins get their own symbol or label
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

        # Other symbols: shortest clean Manhattan paths first (ground is symbols only).
        if gnd:
            continue
        limit = 80.0 if len(sts) == 2 else 45.0
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

    sheet.occupants = [o for o in sheet.occupants if o.kind != "reserve"]

    # Name every connected group. Labels first: a label can only slide along
    # its stub, a power symbol can also jog, so it is the one that gives way.
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
            if segs:
                out.extend(_place_wire_label(sheet, segs, net, f"{s.part.ref}.{s.pin.number}"))
            else:
                out.extend(_place_stub_label(sheet, s, net))
    for net, sts in sites.items():
        if not is_power_net(net, kinds):
            continue
        gnd = is_ground_net(net, kinds)
        for group in unions[net].groups():
            out.extend(_place_hat(sheet, [sts[i] for i in group], net, gnd))
    for p in parts:
        if p.kind in ("r", "c", "l", "d"):
            _place_passive_text(sheet, p)
    power_nets = {n for n in sites if is_power_net(n, kinds)}
    return out, _lint(sheet, parts, sites, unions, power_nets)


def emit_from_design(design: Design, *, title: str = "", report: dict | None = None) -> str:
    """The sheet text. ``report`` (if given) receives the readability issues."""
    kinds = {n.name: n.kind for n in design.nets.values()}
    parts = _parts_from_design(design, kinds)
    apply_sch_places(design, parts)
    libs = [_lib_gnd(), _lib_vcc(), _lib_r(), _lib_c(), _lib_l(), _lib_led()]
    seen_lib: set[str] = {"power:GND", "power:VCC", "GND", "VCC", "R", "C", "L", "LED"}
    for p in parts:
        if p.lib_id in seen_lib:
            continue
        if p.lib_sexp:
            libs.append(p.lib_sexp)
            seen_lib.add(p.lib_id)
        elif p.kind == "box":
            libs.append(_lib_box(p.lib_id, p.pins, p.ref[:1] or "U", kinds))
            seen_lib.add(p.lib_id)
    annotations, lint = _annotate(parts, kinds)
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
    uid = new_uuid()
    title = title or "pcbc"
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
        + "".join(body)
        + f'\t(sheet_instances\n'
        f'\t\t(path "/" (page "1"))\n'
        f'\t)\n'
        f'\t(embedded_fonts no)\n'
        f")\n"
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
