"""Vendored chip lands. package= on a generic is the only selector."""

from __future__ import annotations

from pathlib import Path

from .model import Part

SHARE = Path(__file__).resolve().parent / "share" / "footprints"

# prefix, EIA package → filename in SHARE
_GENERIC = {
    ("R", "0402"): "R_0402_1005Metric.kicad_mod",
    ("R", "0603"): "R_0603_1608Metric.kicad_mod",
    ("C", "0402"): "C_0402_1005Metric.kicad_mod",
    ("C", "0603"): "C_0603_1608Metric.kicad_mod",
    ("D", "0603"): "LED_0603_1608Metric.kicad_mod",
    ("L", "0402"): "R_0402_1005Metric.kicad_mod",
    ("L", "0603"): "R_0603_1608Metric.kicad_mod",
}


def generic_mod(prefix: str, package: str) -> Path:
    key = (prefix.upper()[:1], str(package).strip())
    name = _GENERIC.get(key)
    if not name:
        raise FileNotFoundError(
            f"no vendored land for {prefix} package {package!r}. "
            f"known: {sorted(_GENERIC)}"
        )
    path = SHARE / name
    if not path.exists():
        raise FileNotFoundError(path)
    return path


def footprint_path(part: Part) -> Path:
    if part.kind in ("generic", "led"):
        return generic_mod(part.prefix, part.package)
    if part.origin and part.footprint:
        p = part.origin / part.footprint
        if p.exists():
            return p
    if part.origin:
        mods = list(part.origin.glob("*.kicad_mod"))
        if len(mods) == 1:
            return mods[0]
    raise FileNotFoundError(
        f"{part.name}: no .kicad_mod (set footprint= or leave exactly one in the package)"
    )


def symbol_path(part: Part) -> Path | None:
    if not part.origin:
        return None
    if part.symbol:
        p = part.origin / part.symbol
        return p if p.exists() else None
    syms = list(part.origin.glob("*.kicad_sym"))
    return syms[0] if len(syms) == 1 else None
