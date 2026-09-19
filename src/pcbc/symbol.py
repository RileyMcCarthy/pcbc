"""Read pin names and pad numbers from a .kicad_sym.

The symbol is the name → pad map. Footprints only have pad numbers.
"""

from __future__ import annotations

import math
import re
from pathlib import Path

from .model import Pin
from .sexp import matching_paren

_PIN_NAME = re.compile(r'\(name\s+"([^"]*)"')
_PIN_NUM = re.compile(r'\(number\s+"([^"]*)"')
_PIN_TYPE = re.compile(r"\(pin\s+(\S+)")
_PIN_AT = re.compile(r"\(at\s+([0-9.+-]+)\s+([0-9.+-]+)(?:\s+([0-9.+-]+))?\)")
_PIN_LEN = re.compile(r"\(length\s+([0-9.+-]+)\)")
_SYM_NAME = re.compile(r'\(symbol\s+"([^"]+)"')

_OPTIONAL_NAMES = frozenset({"NC", "DNC"})
_OPTIONAL_TYPES = frozenset({"no_connect"})

_RECT = re.compile(
    r"\(rectangle\s*\(\s*start\s+([0-9.+-]+)\s+([0-9.+-]+)\)\s*\(\s*end\s+([0-9.+-]+)\s+([0-9.+-]+)\)",
    re.S,
)
_XY = re.compile(r"\(xy\s+([0-9.+-]+)\s+([0-9.+-]+)\)")
_CENTER = re.compile(r"\(center\s+([0-9.+-]+)\s+([0-9.+-]+)\)")
_RADIUS = re.compile(r"\(radius\s+([0-9.+-]+)\)")
_ARC_PT = re.compile(r"\((?:start|mid|end)\s+([0-9.+-]+)\s+([0-9.+-]+)\)")
_HIDE_NAMES = re.compile(r"\(pin_names\b[^)]*\bhide\s+yes", re.S)
_HIDE_NUMS = re.compile(r"\(pin_numbers\b[^)]*\bhide\s+yes", re.S)
_PROP_NAME = re.compile(r'\(property\s+"([^"]+)"')
_CHAR_W = 1.27 * 0.8
_FONT_H = 1.27


def parse_symbol_pins(path: Path) -> dict[str, Pin]:
    text = Path(path).read_text()
    grouped: dict[str, list[str]] = {}
    etypes: dict[str, str] = {}
    start = 0
    while True:
        j = text.find("(pin ", start)
        if j < 0:
            break
        end = matching_paren(text, j)
        block = text[j : end + 1]
        start = end + 1
        nm = _PIN_NAME.search(block)
        num = _PIN_NUM.search(block)
        if not num:
            continue
        name = (nm.group(1) if nm else "").strip() or num.group(1)
        if name == "~":
            name = num.group(1)
        grouped.setdefault(name, []).append(num.group(1))
        tp = _PIN_TYPE.search(block)
        if name not in etypes and tp:
            etypes[name] = tp.group(1)
    pins: dict[str, Pin] = {}
    for name, pads in grouped.items():
        etype = etypes.get(name, "unspecified")
        optional = name.upper() in _OPTIONAL_NAMES or etype in _OPTIONAL_TYPES
        pins[name] = Pin(name=name, pads=tuple(pads), optional=optional, etype=etype)
    return pins


_UNIT_NAME = re.compile(r'\(symbol\s+"[^"]+_(\d+)_\d+"')


def symbol_units(text: str) -> int:
    """How many units the main symbol has (sub-symbols NAME_<unit>_<style>;
    unit 0 is shared graphics). easyeda2kicad makes single-unit symbols."""
    _name, block = extract_main_symbol(text)
    units = {int(m.group(1)) for m in _UNIT_NAME.finditer(block)} - {0}
    return max(len(units), 1)


def extract_main_symbol(text: str) -> tuple[str, str]:
    j = text.find("(symbol ")
    if j < 0:
        raise ValueError("no symbol in library")
    end = matching_paren(text, j)
    block = text[j : end + 1]
    nm = _SYM_NAME.search(block)
    return (nm.group(1) if nm else "SYM"), block


def parse_symbol_pins_geom(path: Path) -> list[dict]:
    return _pins_geom(Path(path).read_text())


def _pins_geom(text: str) -> list[dict]:
    pins: list[dict] = []
    start = 0
    while True:
        j = text.find("(pin ", start)
        if j < 0:
            break
        end = matching_paren(text, j)
        block = text[j : end + 1]
        start = end + 1
        num = _PIN_NUM.search(block)
        at = _PIN_AT.search(block)
        if not num or not at:
            continue
        nm = _PIN_NAME.search(block)
        ln = _PIN_LEN.search(block)
        tp = _PIN_TYPE.search(block)
        pins.append(
            {
                "name": nm.group(1) if nm else num.group(1),
                "number": num.group(1),
                "x": float(at.group(1)),
                "y": float(at.group(2)),
                "rot": float(at.group(3) or 0),
                "length": float(ln.group(1) if ln else 2.54),
                "type": tp.group(1) if tp else "unspecified",
            }
        )
    return pins


def _text_half_w(s: str) -> float:
    return max(len(s), 1) * _CHAR_W / 2.0


def parse_symbol_layout(text: str) -> dict:
    """Visual AABB + visible Reference/Value offsets from a .kicad_sym.

    Does not rewrite the symbol. Used so SchPlace collision matches what KiCad draws.
    """
    _name, block = extract_main_symbol(text)
    xs: list[float] = []
    ys: list[float] = []

    def add(x: float, y: float, hx: float = 0.0, hy: float = 0.0) -> None:
        xs.extend((x - hx, x + hx))
        ys.extend((y - hy, y + hy))

    for m in _RECT.finditer(block):
        x0, y0, x1, y1 = (float(m.group(i)) for i in range(1, 5))
        xs.extend((x0, x1))
        ys.extend((y0, y1))
    for m in _XY.finditer(block):
        add(float(m.group(1)), float(m.group(2)))
    for m in _CENTER.finditer(block):
        cx, cy = float(m.group(1)), float(m.group(2))
        rm = _RADIUS.search(block[m.start() : m.start() + 160])
        r = float(rm.group(1)) if rm else 0.4
        add(cx, cy, r, r)
    for m in _ARC_PT.finditer(block):
        add(float(m.group(1)), float(m.group(2)))

    pad = 0.8
    core_bbox = (
        (min(xs) - pad, min(ys) - pad, max(xs) + pad, max(ys) + pad)
        if xs
        else None
    )

    hide_names = _HIDE_NAMES.search(block) is not None
    hide_nums = _HIDE_NUMS.search(block) is not None
    pins = _pins_geom(block)
    for p in pins:
        x, y, rot, length = p["x"], p["y"], p["rot"], p["length"]
        add(x, y)
        rad = math.radians(rot)
        # Pin `at` is the electrical end; `rot` points toward the body.
        bx = x + length * math.cos(rad)
        by = y + length * math.sin(rad)
        add(bx, by)
        if not hide_names:
            tw = _text_half_w(str(p["name"]))
            add((x + bx) / 2.0, (y + by) / 2.0, tw, _FONT_H / 2.0)
        if not hide_nums:
            # KiCad draws the number along the pin, between its end and the body.
            tw = _text_half_w(str(p["number"]))
            add((x + bx) / 2.0, (y + by) / 2.0, tw, _FONT_H / 2.0)

    if not xs:
        xs, ys = [-2.54, 2.54], [-2.54, 2.54]
    body_bbox = (min(xs) - pad, min(ys) - pad, max(xs) + pad, max(ys) + pad)

    props: dict[str, tuple[float, float]] = {}
    start = 0
    while True:
        j = block.find("(property", start)
        if j < 0:
            break
        k = matching_paren(block, j)
        chunk = block[j : k + 1]
        start = k + 1
        nm = _PROP_NAME.search(chunk)
        if not nm or nm.group(1) not in ("Reference", "Value"):
            continue
        if re.search(r"\bhide\b", chunk):
            continue
        at = _PIN_AT.search(chunk)
        if not at:
            continue
        px, py = float(at.group(1)), float(at.group(2))
        props[nm.group(1)] = (px, py)
        vm = re.search(r'\(property\s+"[^"]+"\s+"([^"]*)"', chunk)
        label = vm.group(1) if vm else nm.group(1)
        tw = _text_half_w(label)
        add(px, py, tw, _FONT_H / 2.0 + 0.4)

    bbox = (min(xs) - pad, min(ys) - pad, max(xs) + pad, max(ys) + pad)
    return {
        "bbox": bbox,
        "body_bbox": body_bbox,
        "core_bbox": core_bbox or body_bbox,
        "prop_ref": props.get("Reference", (bbox[2] + 1.5, 1.27)),
        "prop_val": props.get("Value", (bbox[2] + 1.5, -1.27)),
        "pins": pins,
    }
