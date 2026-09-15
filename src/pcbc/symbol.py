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
_PIN_AT = re.compile(r"\(at\s+([0-9.+-]+)\s+([0-9.+-]+)(?:\s+([0-9.+-]+))?\)")
_PIN_LEN = re.compile(r"\(length\s+([0-9.+-]+)\)")
_SYM_NAME = re.compile(r'\(symbol\s+"([^"]+)"')

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


def extract_main_symbol(text: str) -> tuple[str, str]:
    j = text.find("(symbol ")
    if j < 0:
        raise ValueError("no symbol in library")
    end = matching_paren(text, j)
    block = text[j : end + 1]
    nm = _SYM_NAME.search(block)
    return (nm.group(1) if nm else "SYM"), block


def parse_symbol_pins_geom(path: Path) -> list[dict]:
    text = Path(path).read_text()
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
