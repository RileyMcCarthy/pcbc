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


def _label(name: str, x: float, y: float, rot: int, justify: str) -> str:
    return (
        f'\t(label "{name}"\n'
        f'\t\t(at {_fmt(x)} {_fmt(y)} {rot})\n'
        f'\t\t(effects (font (size {_FONT} {_FONT})) (justify {justify} bottom))\n'
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
    if part.kind in ("r", "c", "l", "d"):
        (rwx, rwy), (vwx, vwy) = _passive_label_xy(part)
    else:
        rwx, rwy = _prop_world(part, part.prop_ref)
        vwx, vwy = _prop_world(part, part.prop_val)
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
        f'\t\t\t(at {_fmt(rwx)} {_fmt(rwy)} 0)\n'
        f'\t\t\t(effects (font (size 1.27 1.27)))\n'
        f'\t\t)\n'
        f'\t\t(property "Value" "{part.display}"\n'
        f'\t\t\t(at {_fmt(vwx)} {_fmt(vwy)} 0)\n'
        f'\t\t\t(effects (font (size 1.27 1.27)))\n'
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
            body_bbox = bbox
            core_bbox = _PASSIVE_CORE["d"]
            hw = max(abs(bbox[0]), abs(bbox[2]))
            hh = max(abs(bbox[1]), abs(bbox[3]))
        elif inst.part.kind == "generic" or prefix in "RCL":
            kind = {"R": "r", "C": "c", "L": "l"}.get(prefix, "r")
            pins = _passive_pins(kind, pnets)
            lib_id = kind.upper()
            lib_sexp = None
            prop_ref, prop_val, bbox = _PASSIVE_PROP[kind]
            body_bbox = bbox
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


def _near(ax: float, ay: float, bx: float, by: float) -> bool:
    return abs(ax - bx) < _EPS and abs(ay - by) < _EPS


def _inside_segment(px: float, py: float, x0: float, y0: float, x1: float, y1: float) -> bool:
    """Point on an axis-aligned segment, endpoints excluded."""
    if abs(x0 - x1) < _EPS:
        return abs(px - x0) < _EPS and min(y0, y1) + _EPS < py < max(y0, y1) - _EPS
    if abs(y0 - y1) < _EPS:
        return abs(py - y0) < _EPS and min(x0, x1) + _EPS < px < max(x0, x1) - _EPS
    return False


def _segment_crosses_box(
    x0: float, y0: float, x1: float, y1: float, box: tuple[float, float, float, float], pad: float = 0.3
) -> bool:
    bx0, by0, bx1, by1 = box
    return not (
        max(x0, x1) < bx0 - pad
        or min(x0, x1) > bx1 + pad
        or max(y0, y1) < by0 - pad
        or min(y0, y1) > by1 + pad
    )


def _point_in_aabb(
    x: float, y: float, box: tuple[float, float, float, float], pad: float = 0.0
) -> bool:
    x0, y0, x1, y1 = box
    return (x0 - pad) <= x <= (x1 + pad) and (y0 - pad) <= y <= (y1 + pad)


class _Sheet:
    """Geometry already on the sheet. KiCad connects wire endpoints to pin ends and
    to other wire endpoints; it does not connect anything that merely crosses.
    So every new segment must end on this net and its interior must stay off
    every pin end and every symbol's graphics."""

    def __init__(self, parts: list[Part]):
        self.parts = parts
        self.pin_ends = [pin_world(p, pin) for p in parts for pin in p.pins]
        self.cores = [core_aabb(p) for p in parts]
        self.segs: list[tuple[float, float, float, float, str]] = []

    def on_pin_end(self, x: float, y: float) -> bool:
        return any(_near(x, y, px, py) for px, py in self.pin_ends)

    def segment_ok(self, x0: float, y0: float, x1: float, y1: float) -> bool:
        if any(_inside_segment(px, py, x0, y0, x1, y1) for px, py in self.pin_ends):
            return False
        return not any(_segment_crosses_box(x0, y0, x1, y1, box) for box in self.cores)

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
            if not self.segment_ok(ax, ay, bx, by):
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
    """Candidate Manhattan paths from a to b, most natural first."""
    ax, ay, bx, by = a.x, a.y, b.x, b.y
    oa = pin_outward(a.part, a.pin)
    out: list[list[tuple[float, float]]] = []
    if abs(ax - bx) < _EPS or abs(ay - by) < _EPS:
        out.append([(ax, ay), (bx, by)])
    if abs(oa[0]) >= abs(oa[1]):
        mx = (ax + bx) / 2.0
        out.append([(ax, ay), (mx, ay), (mx, by), (bx, by)])
        out.append([(ax, ay), (bx, ay), (bx, by)])
        out.append([(ax, ay), (ax, by), (bx, by)])
    else:
        my = (ay + by) / 2.0
        out.append([(ax, ay), (ax, my), (bx, my), (bx, by)])
        out.append([(ax, ay), (ax, by), (bx, by)])
        out.append([(ax, ay), (bx, ay), (bx, by)])
    return out


def _label_pose(ox: float, oy: float) -> tuple[int, str]:
    """Text hangs away from the symbol along the outward direction."""
    if abs(ox) >= abs(oy):
        return 0, "right" if ox < 0 else "left"
    return 90, "right" if oy > 0 else "left"


def _hat_site(sites: list[_Site], gnd: bool, parts: list[Part]) -> _Site:
    """The pin in a connected group whose power symbol collides least."""
    best, best_score = sites[0], float("inf")
    for s in sites:
        box = (s.x - 2.2, s.y, s.x + 2.2, s.y + 5.2) if gnd else (s.x - 2.2, s.y - 5.2, s.x + 2.2, s.y)
        hits = 0
        for o in parts:
            if o.ref == s.part.ref:
                continue
            ox0, oy0, ox1, oy1 = world_aabb(o)
            if not (box[2] < ox0 or ox1 < box[0] or box[3] < oy0 or oy1 < box[1]):
                hits += 1
        # GND symbols want to hang down the sheet, supply symbols to point up.
        score = hits * 1000.0 + (-s.y if gnd else s.y)
        if score < best_score:
            best, best_score = s, score
    return best


def _annotate(parts: list[Part], kinds: dict[str, str] | None = None) -> list[str]:
    """Wires, labels and power symbols so KiCad reads exactly the board's netlist.

    Per net: join same-symbol pins that sit in a line, then join nearby symbols
    with Manhattan wires when a clean path exists. Whatever is still apart gets
    named: every connected group of a signal net carries a label, every group of
    a power net carries a power symbol. Nothing is skipped because a pin name
    looks like the net — KiCad does not connect on names.
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
            for pts in _routes(sts[i], sts[j]):
                if sheet.path_ok(pts, net):
                    out.extend(sheet.add(pts, net))
                    net_segs[net].append((i, pts))
                    uf.join(i, j)
                    break

    # Name every connected group.
    for net, sts in sites.items():
        uf = unions[net]
        power = is_power_net(net, kinds)
        gnd = is_ground_net(net, kinds)
        for group in uf.groups():
            members = [sts[i] for i in group]
            if power:
                s = _hat_site(members, gnd, parts)
                out.append(_hat(net, s.x, s.y, gnd=gnd, rot=0))
                continue
            segs = [
                (ax, ay, bx, by)
                for i, pts in net_segs[net]
                if uf.find(i) == uf.find(group[0])
                for (ax, ay), (bx, by) in zip(pts, pts[1:])
                if not _near(ax, ay, bx, by)
            ]
            if segs:
                # Label on the longest run, horizontal preferred: readable, and
                # a mid-wire anchor always connects.
                segs.sort(key=lambda s: (abs(s[3] - s[1]) < _EPS, math.hypot(s[2] - s[0], s[3] - s[1])), reverse=True)
                ax, ay, bx, by = segs[0]
                mx, my = (ax + bx) / 2.0, (ay + by) / 2.0
                rot = 0 if abs(by - ay) < _EPS else 90
                out.append(_label(net, mx, my, rot, "left"))
                continue
            s = members[0]
            ox, oy = pin_outward(s.part, s.pin)
            rot, just = _label_pose(ox, oy)
            placed = False
            for k in range(12):
                length = 5.08 + 1.27 * k
                ex, ey = s.x + ox * length, s.y + oy * length
                if not sheet.segment_ok(s.x, s.y, ex, ey) or not sheet.point_free(ex, ey, net):
                    continue
                if any(
                    o is not s.part and _point_in_aabb(ex, ey, world_aabb(o), pad=1.0)
                    for o in parts
                ):
                    continue
                out.extend(sheet.add([(s.x, s.y), (ex, ey)], net))
                out.append(_label(net, ex, ey, rot, just))
                placed = True
                break
            if not placed:
                # No clean stub: the label sits on the pin end, which also connects.
                out.append(_label(net, s.x, s.y, rot, just))
    return out


def emit_from_design(design: Design, *, title: str = "") -> str:
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
    body: list[str] = [_instance(p) for p in parts]
    body.extend(_annotate(parts, kinds))
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


def emit_schematic_file(design: Design, out_path: Path, *, title: str = "") -> Path:
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(emit_from_design(design, title=title or "pcbc"))
    return out_path
