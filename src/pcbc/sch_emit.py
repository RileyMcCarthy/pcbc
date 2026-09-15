"""Emit a KiCad 10 .kicad_sch from a pcbc Design. No default.net."""

from __future__ import annotations

import math
import re
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from .footprints import symbol_path
from .model import Design
from .sexp import matching_paren, new_uuid
from .symbol import extract_main_symbol, parse_symbol_pins_geom


def is_power_net(name: str) -> bool:
    n = (name or "").strip()
    if not n or n.startswith("unconnected") or n.endswith(".NC"):
        return False
    u = n.upper()
    if n.startswith("+") or n.startswith("-"):
        return True
    return u in {"GND", "VSS", "VBUS", "VIN", "VCC", "VDD", "3V3", "5V"} or u.startswith(
        ("VCC", "VDD", "GND")
    )

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
    hw: float = 8.0
    hh: float = 6.0
    lib_sexp: str | None = None


def _kind(ref: str) -> str:
    if ref[:1] == "R":
        return "r"
    if ref[:1] == "C":
        return "c"
    if ref[:1] == "L":
        return "l"
    return "box"


def _passive_pins(kind: str, pin_nets: dict[str, str]) -> list[PinDef]:
    # Pin 1 top (screen up = −Y), pin 2 bottom.
    n1 = pin_nets.get("1", "")
    n2 = pin_nets.get("2", pin_nets.get("1", ""))
    return [
        PinDef("1", "1", 0.0, -2.54, 90, n1),
        PinDef("2", "2", 0.0, 2.54, 270, n2),
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


def _lib_gnd() -> str:
    return """		(symbol "GND"
			(power global)
			(pin_numbers (hide yes))
			(pin_names (offset 0) (hide yes))
			(exclude_from_sim no)
			(in_bom yes)
			(on_board yes)
			(property "Reference" "#PWR"
				(at 0 -6.35 0)
				(hide yes)
				(effects (font (size 1.27 1.27)))
			)
			(property "Value" "GND"
				(at 0 -3.81 0)
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
				(pin power_in line
					(at 0 0 270)
					(length 0)
					(name "" (effects (font (size 1.27 1.27))))
					(number "1" (effects (font (size 1.27 1.27))))
				)
			)
			(embedded_fonts no)
		)
"""


def _lib_vcc() -> str:
    return """		(symbol "VCC"
			(power global)
			(pin_numbers (hide yes))
			(pin_names (offset 0) (hide yes))
			(exclude_from_sim no)
			(in_bom yes)
			(on_board yes)
			(property "Reference" "#PWR"
				(at 0 6.35 0)
				(hide yes)
				(effects (font (size 1.27 1.27)))
			)
			(property "Value" "VCC"
				(at 0 3.556 0)
				(effects (font (size 1.27 1.27)))
			)
			(symbol "VCC_0_1"
				(polyline
					(pts (xy 0 0) (xy 0 2.54))
					(stroke (width 0) (type default))
					(fill (type none))
				)
				(polyline
					(pts (xy -0.762 1.27) (xy 0 2.54) (xy 0.762 1.27))
					(stroke (width 0) (type default))
					(fill (type none))
				)
			)
			(symbol "VCC_1_1"
				(pin power_in line
					(at 0 0 90)
					(length 0)
					(name "" (effects (font (size 1.27 1.27))))
					(number "1" (effects (font (size 1.27 1.27))))
				)
			)
			(embedded_fonts no)
		)
"""


def _lib_box(lib_id: str, pins: list[PinDef], ref_prefix: str) -> str:
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
        etype = "power_in" if is_power_net(p.net) else "unspecified"
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


def _layout(parts: list[Part]) -> None:
    groups = [
        [p for p in parts if p.ref[:1] in "UA"],
        [p for p in parts if p.ref[:1] == "J"],
        [p for p in parts if p.kind == "l"],
        [p for p in parts if p.kind == "c"],
        [p for p in parts if p.kind == "r"],
        [p for p in parts if p.ref[:1] not in "UAJLRC"],
    ]
    y = 50.0
    for group in groups:
        if not group:
            continue
        group.sort(key=lambda p: p.ref)
        row_hh = max(p.hh + (28.0 if p.kind == "box" else 12.0) for p in group)
        y += row_hh
        x = 50.0
        for p in group:
            margin = 28.0 if p.kind == "box" else 12.0
            x += p.hw + margin
            p.x, p.y = x, y
            x += p.hw + margin + 8.0
        y += row_hh + 14.0


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


def _hat(net: str, x: float, y: float, gnd: bool) -> str:
    lib = "GND" if gnd else "VCC"
    uid = new_uuid()
    val_y = y + 3.81 if gnd else y - 3.81
    return (
        f'\t(symbol\n'
        f'\t\t(lib_id "{lib}")\n'
        f'\t\t(at {_fmt(x)} {_fmt(y)} 0)\n'
        f'\t\t(unit 1)\n'
        f'\t\t(in_bom no)\n'
        f'\t\t(on_board no)\n'
        f'\t\t(dnp no)\n'
        f'\t\t(uuid "{uid}")\n'
        f'\t\t(property "Reference" "#PWR{uid[:8]}"\n'
        f'\t\t\t(at {_fmt(x)} {_fmt(y)} 0)\n'
        f'\t\t\t(hide yes)\n'
        f'\t\t\t(effects (font (size 1.27 1.27)))\n'
        f'\t\t)\n'
        f'\t\t(property "Value" "{net}"\n'
        f'\t\t\t(at {_fmt(x)} {_fmt(val_y)} 0)\n'
        f'\t\t\t(effects (font (size 1.27 1.27)))\n'
        f'\t\t)\n'
        f'\t\t(pin "1" (uuid "{new_uuid()}"))\n'
        f'\t)\n'
    )


def _instance(part: Part) -> str:
    pins = "".join(
        f'\t\t(pin "{p.number}" (uuid "{new_uuid()}"))\n' for p in part.pins
    )
    return (
        f'\t(symbol\n'
        f'\t\t(lib_id "{part.lib_id}")\n'
        f'\t\t(at {_fmt(part.x)} {_fmt(part.y)} 0)\n'
        f'\t\t(unit 1)\n'
        f'\t\t(exclude_from_sim no)\n'
        f'\t\t(in_bom yes)\n'
        f'\t\t(on_board yes)\n'
        f'\t\t(dnp no)\n'
        f'\t\t(uuid "{new_uuid()}")\n'
        f'\t\t(property "Reference" "{part.ref}"\n'
        f'\t\t\t(at {_fmt(part.x)} {_fmt(part.y - part.hh - 2.0)} 0)\n'
        f'\t\t\t(effects (font (size 1.27 1.27)))\n'
        f'\t\t)\n'
        f'\t\t(property "Value" "{part.display}"\n'
        f'\t\t\t(at {_fmt(part.x)} {_fmt(part.y + part.hh + 2.0)} 0)\n'
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


def _parts_from_design(design: Design) -> list[Part]:
    parts: list[Part] = []
    for inst in design.instances:
        pnets = _pad_nets(inst)
        prefix = (inst.part.prefix or inst.ref[:1] or "U").upper()[:1]
        if inst.part.kind == "generic" or prefix in "RCLD":
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


def emit_from_design(design: Design, *, title: str = "") -> str:
    parts = _parts_from_design(design)
    _layout(parts)
    deg = _degree_parts(parts)
    libs = [_lib_gnd(), _lib_vcc(), _lib_r(), _lib_c(), _lib_l()]
    seen_lib: set[str] = {"GND", "VCC", "R", "C", "L"}
    for p in parts:
        if p.lib_id in seen_lib:
            continue
        if p.lib_sexp:
            libs.append(p.lib_sexp)
            seen_lib.add(p.lib_id)
        elif p.kind == "box":
            libs.append(_lib_box(p.lib_id, p.pins, p.ref[:1] or "U"))
            seen_lib.add(p.lib_id)
    body: list[str] = [_instance(p) for p in parts]
    hats_done: set[tuple[str, str]] = set()
    labels_done: set[tuple[str, str]] = set()
    for part in parts:
        for pin in part.pins:
            wx = part.x + pin.lx
            wy = part.y + pin.ly
            net = pin.net
            if not net or net.startswith("unconnected") or net.endswith(".NC"):
                continue
            if is_power_net(net):
                key = (part.ref, net)
                if key in hats_done:
                    continue
                hats_done.add(key)
                gnd = net.upper() in ("GND", "VSS")
                hx, hy = wx, wy + (_HAT if gnd else -_HAT)
                # Prefer vertical hat off the pin; if the pin is left/right, still drop/raise.
                body.append(_wire(wx, wy, hx, hy))
                body.append(_hat(net, hx, hy, gnd=gnd))
                continue
            if deg.get(net, 0) < 2:
                continue
            key = (part.ref, net)
            if key in labels_done:
                continue
            labels_done.add(key)
            length = max(5.08, _text_w(net))
            dx, dy = _stub_delta(pin.rot, length)
            sx, sy = wx + dx, wy + dy
            rot, just = _underline_pose(dx, dy)
            body.append(_wire(wx, wy, sx, sy))
            body.append(_label(net, sx, sy, rot, just))
    max_x = max((p.x + p.hw + 40 for p in parts), default=200)
    max_y = max((p.y + p.hh + 40 for p in parts), default=150)
    paper = "A2" if max_x < 420 and max_y < 300 else "A1" if max_x < 850 else "A0"
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
