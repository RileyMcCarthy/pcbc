"""Map Zener instance names (J1) to KiCad references (U3)."""

from __future__ import annotations

import re

from .sexp import board_footprint_spans, footprint_reference

_PATH = re.compile(r'\(property "Path" "([^"]+)"')


def footprint_path(block: str) -> str | None:
    m = _PATH.search(block)
    return m.group(1) if m else None


def ref_aliases(block: str) -> list[str]:
    """Names that may appear in a .place.py for this footprint."""
    names: list[str] = []
    ref = footprint_reference(block)
    if ref:
        names.append(ref)
    path = footprint_path(block)
    if path:
        names.append(path)
        head = path.split(".", 1)[0]
        if head and head not in names:
            names.append(head)
    return names


def build_alias_index(text: str) -> dict[str, str]:
    """alias → KiCad Reference. First writer wins on collisions."""
    idx: dict[str, str] = {}
    for start, end in board_footprint_spans(text):
        block = text[start:end]
        ref = footprint_reference(block)
        if not ref:
            continue
        for alias in ref_aliases(block):
            idx.setdefault(alias, ref)
    return idx


def resolve_ref(name: str, index: dict[str, str]) -> str | None:
    if name in index:
        return index[name]
    return None


def refs_report(places: list, pcb_text: str) -> dict:
    """Place() names vs KiCad Reference / Zener Path."""
    idx = build_alias_index(pcb_text)
    rows = []
    missing: list[str] = []
    for p in places:
        ref = p.ref if hasattr(p, "ref") else str(p)
        locked = bool(getattr(p, "locked", False))
        k = resolve_ref(ref, idx)
        rows.append({"place": ref, "kicad": k, "locked": locked})
        if k is None:
            missing.append(ref)
    zener_to_kicad = {alias: dest for alias, dest in idx.items() if alias != dest}
    return {
        "places": rows,
        "missing": missing,
        "zener_to_kicad": zener_to_kicad,
        "error": (
            f"Place() names missing on board: {', '.join(missing)}" if missing else None
        ),
    }
