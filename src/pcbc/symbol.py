"""Read pin names and pad numbers from a .kicad_sym.

The symbol is the name → pad map. Footprints only have pad numbers.
"""

from __future__ import annotations

import re
from pathlib import Path

from .model import Pin
from .sexp import matching_paren

_PIN_NAME = re.compile(r'\(name\s+"([^"]*)"')
_PIN_NUM = re.compile(r'\(number\s+"([^"]*)"')
_PIN_TYPE = re.compile(r"\(pin\s+(\S+)")

_OPTIONAL_NAMES = frozenset({"NC", "DNC", "EP", "PAD", "MP", "RSVD"})
_OPTIONAL_TYPES = frozenset({"no_connect"})


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
