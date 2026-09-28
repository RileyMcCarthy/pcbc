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


def check_design(design: Design, pcb: bool = True, notes: list[str] | None = None) -> list[str]:
    """What is wrong before a build. ``pcb=False`` is the schematic loop:
    Place() is not needed to draw the sheet.

    ``notes`` is an out-parameter for findings that are **moves and not failures**: the caller passes
    a list and gets back the sentences to print with an exit code of 0. Only one thing writes to it
    today — two `Ground()` nets with nothing tying them (`_untied_grounds`) — and the reason it is a
    note rather than a `fails` entry is H.3's promotion procedure: the DS2 Addon ships with this
    defect and builds green, so failing on it would stop a board on a fault its author has not been
    told about yet, which is how an AI learns to reach for `--force`. It is promoted to a failure in
    the PR that shows that board tied, never before.
    """
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
        if inst.part.origin:
            for msg in _library_fails(inst.part):
                fails.append(f"{inst.ref}: library {msg}")
        elif pcb and inst.part.kind in ("generic", "led"):
            from .footprints import generic_mod

            try:
                generic_mod(inst.part.prefix, inst.part.package)
            except FileNotFoundError as exc:
                fails.append(f"{inst.ref}: {exc}")

    placed = {p.ref for p in design.places}
    sch_placed = {p.ref for p in design.sch_places}
    for what, specs in (("Place", design.places), ("SchPlace", design.sch_places)):
        seen: dict[str, int] = {}
        for p in specs:
            seen[p.ref] = seen.get(p.ref, 0) + 1
        for ref, n in sorted(seen.items()):
            if n > 1 and (pcb or what == "SchPlace"):
                fails.append(f"{ref}: {what}() {n} times; keep one (the last one does not win, the check does)")
    for inst in design.instances:
        if pcb and inst.ref not in placed:
            fails.append(f"{inst.ref}: no Place() — every part is CSS-placed")
        if inst.ref not in sch_placed:
            fails.append(f"{inst.ref}: no SchPlace() — schematic pose is CSS, not auto-layout")
    if design.board is not None:
        # A bad NetReq / Pair / Bus / Chain / Isolation / Guard / Bridge line fails here, before
        # anything is drawn.
        from .constraints import compile_constraints

        try:
            cs = compile_constraints(design)
        except (KeyError, ValueError) as exc:
            fails.append(str(exc.args[0]) if exc.args else str(exc))
        else:
            fails.extend(cs.refusals)
            m, n = check_bridges(design, cs)
            fails.extend(m)
            if notes is not None:
                notes.extend(n)
    if notes is not None:
        notes.extend(_untied_grounds(design))
    return fails


# ---------------------------------------------------------------------------------------------
# Bridge: the one point two grounds are tied at, asked of the netlist alone
# ---------------------------------------------------------------------------------------------


def _two_pin_ties(design: Design, a: str, b: str) -> list[str]:
    """Every two-pin instance with one pad on `a` and one on `b`, sorted by ref.

    Two pins and not two *pads*: a part with two pins is a part current goes **through**, which is
    what a tie is. `U1` on the DS2 Addon binds both grounds on 16 pins and is not one — an IC
    referenced to two grounds is what makes the split matter, not what closes it, and that
    distinction is the load-bearing rule of the whole statement (`docs/stitch-plan.md` §3).
    """
    out = []
    for inst in design.instances:
        if len(inst.part.pins) != 2:
            continue
        nets = set(inst.pins.values())
        if a in nets and b in nets:
            out.append(inst.ref)
    return sorted(out)


def _grounds(design: Design) -> list[str]:
    return sorted(n.name for n in design.nets.values() if n.kind == "ground")


def _pads_on(design: Design, net: str) -> list[str]:
    """Every `"REF.PAD"` of `net`, sorted, connectors first — the vocabulary `Bridge(at=)` takes."""
    out: list[tuple[int, str]] = []
    for inst in design.instances:
        rank = 0 if inst.part.prefix == "J" else 1
        for pin, bound in inst.pins.items():
            if bound != net:
                continue
            nums = inst.part.pins[pin].pads if pin in inst.part.pins else (pin,)
            for num in nums:
                out.append((rank, f"{inst.ref}.{num}"))
    return [pid for _r, pid in sorted(out)]


def _untied_grounds(design: Design) -> list[str]:
    """The finding that needs no declaration: two `Ground()` nets and nothing joining them.

    A move, printed, exit 0. Measured on the DS2 Addon 2026-09-21, where it fires on a board that
    builds to fab today: `GND` and `VSS` are both `Ground()`, the only instance binding both is `U1`
    with **16** pins, and of the two-pin parts that touch either, C5 and C6 are wholly on GND and C1,
    C3, C4, C7, C9, C10 and C12 wholly on VSS. Nothing joins them, so the ADC's two references have
    no defined potential between them and the split is two names rather than two domains — measured
    on the placed board, their pads overlap on all four separating axes, so there is not even a line
    to draw between them (`route_checks.check_bridge`).

    Silent on all four example boards, and for a structural reason rather than a tuned one: each of
    them declares exactly one `Ground()`, so the pair loop has nothing to iterate.
    """
    from .moves import move_line

    grounds = _grounds(design)
    if len(grounds) < 2:
        return []
    out: list[str] = []
    for i, a in enumerate(grounds):
        for b in grounds[i + 1 :]:
            if _two_pin_ties(design, a, b):
                continue
            if any(br.a in (a, b) and br.b in (a, b) for br in design.bridges):
                continue
            both = [inst for inst in design.instances if {a, b} <= set(inst.pins.values())]
            who = "; ".join(
                f"{inst.ref} binds both on {len(inst.part.pins)} pins ("
                + ", ".join(f"{pin} on {net}" for pin, net in sorted(inst.pins.items()) if net in (a, b))
                + ")"
                for inst in sorted(both, key=lambda i: i.ref)
            )
            side_a = sorted({inst.ref for inst in design.instances if len(inst.part.pins) == 2 and a in inst.pins.values()})
            side_b = sorted({inst.ref for inst in design.instances if len(inst.part.pins) == 2 and b in inst.pins.values()})
            blockers = (
                f"nothing — no two-pin part has one pad on {a} and one on {b}"
                + (f"; {who}, which is not a tie" if who else "")
                + f"; the two-pin parts are all on one side or the other ({a}: {', '.join(side_a) or 'none'}; {b}: {', '.join(side_b) or 'none'})"
            )
            pa = _pads_on(design, a)
            pb = _pads_on(design, b)
            off = (
                f'Bridge("{a}", "{b}", at=("{pa[0]}", "{pb[0]}"), kind="off_board", why="tied in the harness")'
                if pa and pb
                else f'Bridge("{a}", "{b}", at=(...), kind="off_board", why="...")'
            )
            out.append(
                move_line(
                    f"{a}/{b}",
                    f"{b}",
                    a,
                    blockers,
                    f"Two grounds with no DC path between them have no defined potential difference, and pcbc writes no copper to make one "
                    f'(docs/stitch-plan.md section 8 item 4). Say where they meet: Resistor("R11", "0R", p1={a}, p2={b}) then '
                    f'Bridge("{a}", "{b}", at="R11"); or {off} if they already meet off this board.',
                )
            )
    return out


def check_bridges(design: Design, cs) -> tuple[list[str], list[str]]:
    """(refusals, notes) for every declared `Bridge()`, asked of the netlist and of nothing else.

    The net-level refusals (an undeclared net, a net that is not a `Ground()`, the same pair twice)
    are `compile_constraints`' — it already holds those facts. What is here is everything that names
    an **instance**, because a message that names a part reads better beside the part.
    """
    fails: list[str] = []
    notes: list[str] = []
    for spec in cs.bridges:
        who = f'Bridge("{spec.a}", "{spec.b}") line {spec.req.line}'
        if spec.kind == "off_board":
            by_net = {a: set(_pads_on(design, a)) for a in (spec.a, spec.b)}
            for pid, net in zip(spec.at, (spec.a, spec.b)):
                if pid not in by_net[net]:
                    other = next((n for n in (spec.a, spec.b) if pid in by_net[n]), None)
                    fails.append(
                        f"{who}: {pid} is not a pad of {net}"
                        + (f"; it is on {other}" if other else " — kind=\"off_board\" names one pad of each, in the order the nets are written")
                    )
            continue
        inst = next((i for i in design.instances if i.ref == spec.tie_ref), None)
        if inst is None:
            fails.append(f"{who}: no part {spec.tie_ref}; at= names a part this board places")
            continue
        if len(inst.part.pins) != 2:
            fails.append(
                f"{who}: {spec.tie_ref} has {len(inst.part.pins)} pins, and a tie is a part the current goes **through** — "
                f"a part that merely binds both grounds is what makes the split matter, not what closes it. "
                f'Give the two grounds their own part: Resistor("R11", "0R", p1={spec.a}, p2={spec.b}) then Bridge("{spec.a}", "{spec.b}", at="R11").'
            )
            continue
        if not spec.pads:
            on = sorted(set(inst.pins.values()))
            fails.append(
                f"{who}: {spec.tie_ref} is on {', '.join(on)}, not one pad on {spec.a} and one on {spec.b}; "
                f"rebind it: {spec.tie_ref}(p1={spec.a}, p2={spec.b})."
            )
            continue
        others = [r for r in _two_pin_ties(design, spec.a, spec.b) if r != spec.tie_ref]
        if others:
            fails.append(
                f"{who}: {', '.join(others)} also join{'s' if len(others) == 1 else ''} {spec.a} to {spec.b}, so the tie is not one point. "
                f"Drop all but one, or drop the Bridge() line and say what the several ties are for."
            )
            continue
        if inst.part.prefix == "C" or inst.ref.startswith("C"):
            notes.append(
                f"{who}: {spec.tie_ref} is a capacitor, so this is an AC bridge — {spec.a} and {spec.b} still have no DC "
                f"reference between them. That is a real choice on an isolated domain; it is a mistake on a shared one."
            )
    return fails, notes


_LIBRARY_FAILS: dict[tuple[str, str | None, str], list[str]] = {}


def _library_fails(part: Part) -> list[str]:
    """`fail`-grade findings from pcbc score: what would break the netlist or the drawing."""
    key = (str(part.origin), part.symbol, part.footprint)
    if key not in _LIBRARY_FAILS:
        from .source import score_part

        report = score_part(Path(part.origin), symbol=part.symbol or None, footprint=part.footprint or None)
        # A missing file is reported by seed (footprint) or the pin check (symbol); here only what both files say.
        _LIBRARY_FAILS[key] = [
            msg for sev, msg in report["findings"] if sev == "fail" and not msg.startswith(("no .kicad_mod", "no .kicad_sym"))
        ]
    return _LIBRARY_FAILS[key]


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
