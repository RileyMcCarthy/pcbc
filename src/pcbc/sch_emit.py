"""Emit a KiCad 10 .kicad_sch from a pcbc Design. No default.net."""

from __future__ import annotations

import math
import re
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from .footprints import symbol_path
from .model import Design
from .sch_place import apply_sch_places
from .sexp import matching_paren, new_uuid
from .symbol import extract_main_symbol, parse_symbol_pins_geom


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
    lib_sexp: str | None = None
    placed: bool = False


def _kind(ref: str) -> str:
    if ref[:1] == "R":
        return "r"
    if ref[:1] == "C":
        return "c"
    if ref[:1] == "L":
        return "l"
    return "box"


def _passive_pins(kind: str, pin_nets: dict[str, str]) -> list[PinDef]:
    # Match Device-style libraries: pin 1 at −Y (KiCad sheet +Y is up, so pin 1
    # is the lower end), pin 2 at +Y. Electrical ends, not the body edge.
    n1 = pin_nets.get("1", "")
    n2 = pin_nets.get("2", pin_nets.get("1", ""))
    end = 3.81 if kind in ("r", "l") else 2.54
    return [
        PinDef("1", "1", 0.0, -end, 90, n1),
        PinDef("2", "2", 0.0, end, 270, n2),
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
					(at 0 -3.81 90)
					(length 1.27)
					(name "1" (effects (font (size 1.27 1.27))))
					(number "1" (effects (font (size 1.27 1.27))))
				)
				(pin passive line
					(at 0 3.81 270)
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
					(at 0 -2.54 90)
					(length 2.032)
					(name "1" (effects (font (size 1.27 1.27))))
					(number "1" (effects (font (size 1.27 1.27))))
				)
				(pin passive line
					(at 0 2.54 270)
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
					(at 0 -3.81 90)
					(length 1.778)
					(name "1" (effects (font (size 1.27 1.27))))
					(number "1" (effects (font (size 1.27 1.27))))
				)
				(pin passive line
					(at 0 3.81 270)
					(length 1.778)
					(name "2" (effects (font (size 1.27 1.27))))
					(number "2" (effects (font (size 1.27 1.27))))
				)
			)
			(embedded_fonts no)
		)
"""


def _lib_led() -> str:
    """Vertical LED: pin 2 A at top (−Y), pin 1 K at bottom (+Y). Current flows down."""
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
				(polyline (pts (xy -1.016 0.762) (xy 1.016 0.762))
					(stroke (width 0.254) (type default)) (fill (type none)))
				(polyline (pts (xy -1.016 -0.762) (xy 1.016 -0.762) (xy 0 0.762) (xy -1.016 -0.762))
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
					(at 0 2.54 270)
					(length 1.778)
					(name "K" (effects (font (size 1.27 1.27))))
					(number "1" (effects (font (size 1.27 1.27))))
				)
				(pin passive line
					(at 0 -2.54 90)
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
    show_numbers = any(p.name != p.number for p in pins)
    hide_nums = "no" if show_numbers else "yes"
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


def _stub_delta(rot: float, length: float) -> tuple[float, float]:
    rad = math.radians(rot)
    return (-length * math.cos(rad), -length * math.sin(rad))


def _underline_pose(dx: float, dy: float) -> tuple[int, str]:
    if abs(dx) >= abs(dy):
        return 0, "left" if dx < 0 else "right"
    return 90, "left" if dy > 0 else "right"


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
    lx, ly = pin.lx, pin.ly
    rot = part.rot % 360.0
    if abs(rot - 180) < 1:
        lx, ly = -lx, -ly
    elif abs(rot - 90) < 1:
        lx, ly = -ly, lx
    elif abs(rot - 270) < 1:
        lx, ly = ly, -lx
    return part.x + lx, part.y + ly


def _bbox(part: Part) -> tuple[float, float, float, float]:
    return (
        part.x - part.hw - 1.0,
        part.y - part.hh - 1.0,
        part.x + part.hw + 1.0,
        part.y + part.hh + 1.0,
    )


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


def _instance(part: Part) -> str:
    pins = "".join(
        f'\t\t(pin "{p.number}" (uuid "{new_uuid()}"))\n' for p in part.pins
    )
    rot = int(part.rot) % 360
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
        f'\t\t\t(at {_fmt(part.x + part.hw + 1.5)} {_fmt(part.y + 1.27)} 0)\n'
        f'\t\t\t(effects (font (size 1.27 1.27)) (justify left))\n'
        f'\t\t)\n'
        f'\t\t(property "Value" "{part.display}"\n'
        f'\t\t\t(at {_fmt(part.x + part.hw + 1.5)} {_fmt(part.y - 1.27)} 0)\n'
        f'\t\t\t(effects (font (size 1.27 1.27)) (justify left))\n'
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


def _parts_from_design(design: Design) -> list[Part]:
    parts: list[Part] = []
    for inst in design.instances:
        pnets = _pad_nets(inst)
        prefix = (inst.part.prefix or inst.ref[:1] or "U").upper()[:1]
        if inst.part.kind == "led":
            kind = "d"
            lib_id = "LED"
            pins = [
                PinDef("1", "K", 0.0, 2.54, 270, pnets.get("1", pnets.get("K", ""))),
                PinDef("2", "A", 0.0, -2.54, 90, pnets.get("2", pnets.get("A", ""))),
            ]
            hw, hh = 3.0, 3.0
            lib_sexp = None
        elif inst.part.kind == "generic" or prefix in "RCL":
            kind = {"R": "r", "C": "c", "L": "l"}.get(prefix, "r")
            pins = _passive_pins(kind, pnets)
            lib_id = kind.upper()
            hw, hh = 2.0, 4.0
            lib_sexp = None
        else:
            kind = "box"
            lib_sexp = None
            lib_id = re.sub(r"[^A-Za-z0-9_.-]", "_", inst.part.name)[:40]
            pins: list[PinDef] = []
            sp = symbol_path(inst.part)
            if sp and sp.exists():
                lib_id, raw = extract_main_symbol(sp.read_text())
                lib_sexp = "\t\t" + raw.replace("\n", "\n\t\t") + "\n"
                for gp in parse_symbol_pins_geom(sp):
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
            xs = [abs(p.lx) for p in pins]
            ys = [abs(p.ly) for p in pins]
            hw = (max(xs) if xs else 16.0) + 2.0
            hh = (max(ys) if ys else 8.0) + 2.0
        parts.append(
            Part(
                ref=inst.ref,
                lib_id=lib_id,
                display=inst.value or inst.part.value or inst.part.mpn,
                kind=kind,
                pins=pins,
                hw=hw,
                hh=hh,
                lib_sexp=lib_sexp,
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


def _annotate(parts: list[Part], deg: dict[str, int], kinds: dict[str, str] | None = None) -> list[str]:
    """Wires, one label per 2-pin net, power hats. KiCad sheet +Y is up."""
    out: list[str] = []
    sites: dict[str, list[tuple[Part, PinDef, float, float]]] = defaultdict(list)
    for p in parts:
        for pin in p.pins:
            if not pin.net or pin.net.startswith("unconnected") or pin.net.endswith(".NC"):
                continue
            wx, wy = _pin_world(p, pin)
            sites[pin.net].append((p, pin, wx, wy))

    wired: set[str] = set()
    for net, pts in sites.items():
        if is_power_net(net, kinds) or len(pts) != 2:
            continue
        (pa, _, x0, y0), (pb, _, x1, y1) = pts
        dist = math.hypot(x1 - x0, y1 - y0)
        skip = {pa.ref, pb.ref}
        if dist > 20.0:
            continue
        segs = _manhattan(x0, y0, x1, y1, parts, skip)
        if segs is None:
            continue
        out.extend(segs)
        mx, my = (x0 + x1) / 2, (y0 + y1) / 2
        if abs(x1 - x0) >= abs(y1 - y0):
            out.append(_label(net, mx, my - 1.27, 0, "left"))
        else:
            out.append(_label(net, mx - 2.54, my, 0, "right"))
        wired.add(net)

    hats_done: set[tuple[str, str]] = set()
    labels_done: set[tuple[str, str]] = set()
    for p in parts:
        for pin in p.pins:
            net = pin.net
            if not net or net.startswith("unconnected") or net.endswith(".NC"):
                continue
            wx, wy = _pin_world(p, pin)
            if is_power_net(net, kinds):
                key = (p.ref, net)
                if key in hats_done:
                    continue
                hats_done.add(key)
                gnd = is_ground_net(net, kinds)
                # Whole library symbol on the pin (graphics + pin + name).
                # Bodies are drawn VCC-up / GND-down; do not explode into wires.
                out.append(_hat(net, wx, wy, gnd=gnd, rot=0))
                continue
            if net in wired or deg.get(net, 0) < 2:
                continue
            key = (p.ref, net)
            if key in labels_done:
                continue
            length = min(max(2.54, _text_w(net) * 0.6), 5.08)
            dx, dy = _stub_delta(pin.rot + p.rot, length)
            sx, sy = wx + dx, wy + dy
            if any(_seg_hits_part(wx, wy, sx, sy, o) and o.ref != p.ref for o in parts):
                continue
            labels_done.add(key)
            rot, just = _underline_pose(dx, dy)
            out.append(_wire(wx, wy, sx, sy))
            out.append(_label(net, sx, sy, rot, just))
    return out


def _manhattan(
    x0: float,
    y0: float,
    x1: float,
    y1: float,
    parts: list[Part],
    skip: set[str],
) -> list[str] | None:
    def hits(ax, ay, bx, by) -> bool:
        return any(_seg_hits_part(ax, ay, bx, by, p) and p.ref not in skip for p in parts)

    if abs(x0 - x1) < 0.4 or abs(y0 - y1) < 0.4:
        if hits(x0, y0, x1, y1):
            return None
        return [_wire(x0, y0, x1, y1)]
    for mx, my in ((x0, y1), (x1, y0)):
        if not hits(x0, y0, mx, my) and not hits(mx, my, x1, y1):
            return [_wire(x0, y0, mx, my), _wire(mx, my, x1, y1)]
    return None


def emit_from_design(design: Design, *, title: str = "") -> str:
    kinds = {n.name: n.kind for n in design.nets.values()}
    parts = _parts_from_design(design)
    apply_sch_places(design, parts)
    deg = _degree_parts(parts)
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
    body.extend(_annotate(parts, deg, kinds))
    max_x = max((p.x + p.hw + 20 for p in parts), default=100)
    max_y = max((p.y + p.hh + 20 for p in parts), default=80)
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
