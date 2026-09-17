"""Board file → Design, and the check that replaces pcb build."""

from __future__ import annotations

from pathlib import Path

from .model import Design, Instance, Net, Part

def net_name(value: object) -> str:
    if isinstance(value, Net):
        return value.name
    if value is None:
        raise ValueError("pin bound to None")
    return str(value)


def instantiate(part: Part, ref: str, pin_nets: dict[str, object]) -> Instance:
    from .language import _doc, _register_net

    bound: dict[str, str] = {}
    aliases = {p.name.upper(): p.name for p in part.pins.values()}
    for k, v in pin_nets.items():
        key = str(k)
        if key in ("p1", "P1"):
            key = "1" if "1" in part.pins else "P1"
        elif key in ("p2", "P2"):
            key = "2" if "2" in part.pins else "P2"
        if key not in part.pins:
            up = aliases.get(key.upper())
            if up:
                key = up
        name = net_name(v)
        bound[key] = name
        if isinstance(v, Net):
            _register_net(v)
        else:
            _register_net(Net(name))
    inst = Instance(ref=str(ref), part=part, pins=bound, value=part.value)
    _doc().instances.append(inst)
    return inst


def check_design(design: Design) -> list[str]:
    fails: list[str] = []
    if design.board is None:
        fails.append("no Board() — need width/height and stackup")

    seen: set[str] = set()
    for inst in design.instances:
        if inst.ref in seen:
            fails.append(f"duplicate ref {inst.ref}")
        seen.add(inst.ref)
        if not inst.part.pins:
            fails.append(f"{inst.ref}: part {inst.part.name!r} has no symbol pins")
        for pname, pin in inst.part.pins.items():
            if pin.optional:
                continue
            if pname not in inst.pins:
                fails.append(f"{inst.ref}.{pname} unbound")
        for pname in inst.pins:
            if pname not in inst.part.pins:
                fails.append(
                    f"{inst.ref}.{pname} is not a pin on {inst.part.name} "
                    f"(symbol pins: {', '.join(inst.part.pins) or 'none'})"
                )
        if inst.part.kind != "th" and not inst.part.lcsc:
            fails.append(f"{inst.ref} missing lcsc (JLC BOM needs it on SMT)")

    placed = {p.ref for p in design.places}
    for inst in design.instances:
        if inst.ref not in placed:
            fails.append(f"{inst.ref}: no Place() — every part is CSS-placed")
    return fails


def load_part(path: Path) -> Part:
    """Exec part.py; pin map comes from the sibling .kicad_sym."""
    path = Path(path)
    if path.is_file():
        src = path
        pkg = path.parent
    else:
        pkg = path
        src = pkg / "part.py"
    if not src.exists():
        raise FileNotFoundError(src)
    ns: dict = {
        "Component": _library_component,
        "Part": _library_component,
        "__file__": str(src),
        "__name__": "__pcbc_part__",
    }
    exec(compile(src.read_text(), str(src), "exec"), ns, ns)  # noqa: S102
    parts = [v for v in ns.values() if isinstance(v, Part)]
    if "part" in ns and isinstance(ns["part"], Part):
        part = ns["part"]
    elif len(parts) == 1:
        part = parts[0]
    else:
        raise ValueError(f"{src} must assign part = Component(...)")
    part.origin = pkg
    if not part.symbol:
        syms = [p for p in pkg.glob("*.kicad_sym")]
        if len(syms) == 1:
            part.symbol = syms[0].name
    if not part.footprint:
        mods = list(pkg.glob("*.kicad_mod"))
        if len(mods) == 1:
            part.footprint = mods[0].name
    if part.symbol:
        sym = pkg / part.symbol
        if not sym.exists():
            raise FileNotFoundError(f"{part.name}: symbol {sym} missing")
        from .symbol import parse_symbol_pins

        part.pins = parse_symbol_pins(sym)
        for name in part.optional_pins:
            if name in part.pins:
                part.pins[name].optional = True
    elif not part.pins:
        raise ValueError(
            f"{part.name}: package needs one .kicad_sym (that is the pin map)"
        )
    return part


def _library_component(**kwargs) -> Part:
    return Part(
        name=str(kwargs.get("name") or "part"),
        prefix=str(kwargs.get("prefix") or "U"),
        mpn=str(kwargs.get("mpn") or kwargs.get("name") or ""),
        manufacturer=str(kwargs.get("manufacturer") or ""),
        lcsc=kwargs.get("lcsc"),
        footprint=str(kwargs.get("footprint") or ""),
        symbol=kwargs.get("symbol"),
        body_mm=kwargs.get("body_mm"),
        package=str(kwargs.get("package") or ""),
        value=str(kwargs.get("value") or ""),
        kind=str(kwargs.get("kind") or "ic"),
        optional_pins=tuple(str(n) for n in (kwargs.get("optional_pins") or ())),
    )
