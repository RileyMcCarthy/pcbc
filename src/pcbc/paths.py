"""Paths: what belongs to the copper between named pads (`docs/net-path-physics.md`, version 1).

A **net** holds what is the same at every pad (`NetReq`); a **path** (`Path(net, *pads, ...)`) holds
what belongs to the copper between two stations: current (and so width, per layer, and the via group
a layer change places), layers, vias allowed, an airwire budget, keep-aways, and `kind="feedback"` or
`"sense"` for a branch that carries a measurement. This module compiles every `Path()` and `Loop()`
line of a design into `constraints.PathSpec` / `Link` / `LoopSpec`, with every refusal of §7 and the
report lines of §3.1 and §5. Pure: a `Design` and the net-level `ConstraintSet` in, numbers out.

**One combine rule** (§2): a link takes the path's written value, else the path kind's default, else
the net's. A path may loosen a default, never a written value; a path kind's default yields to a value
the `NetReq` wrote.
"""

from __future__ import annotations

import math
import pathlib
from dataclasses import replace

from .constraints import (
    DEFAULT_LOOP_MM2,
    KIND_ALIASES,
    LOOP_NOTE,
    BUS_KINDS_MATCH,
    PAIR_PRESETS,
    PATH_PRESETS,
    PRESETS,
    Constraint,
    ConstraintSet,
    CurrentSpec,
    Derived,
    Joint,
    Link,
    LoopSpec,
    PathSpec,
    Source,
    ViaGroup,
    ViaSpec,
    _current_width,
    _g,
    _kwargs_of,
    _preset,
    _reference_for,
    canon_reference,
    is_poured_reference,
    clearance_table,
    z_se_width,
)
from .model import Design, PathReq
from .stackup import Stackup, track_amps, via_amps, via_group, vias_per_change

PATH_KINDS = ("feedback", "sense")
CURRENT_VIA_MM = {"current": (0.8, 0.4), "switch_node": (0.6, 0.3)}
"""`docs/physics-subjects.md` KD 13: the via (diameter, drill) of a path that carries a current; today's two
preset values, so no copper moves. A pcbc default with no published source (O7)."""


# --- resolution -----------------------------------------------------------------------------------


def _padnum(num: str) -> tuple[int, str]:
    return (int(num), "") if num.isdigit() else (1 << 30, num)


def station_pads(design: Design, net: str, member: str) -> tuple[str, ...]:
    """One station, `"REF.X"`, as the pad ids it names on `net` (§1.2): `X` a pin name names every pad
    of that pin, a pad number names that pad; `_refpin_net`'s resolution, pin name first. Sorted by pad
    number. The one helper every consumer of a station calls."""
    ref, _, pin = member.partition(".")
    inst = next((i for i in design.instances if i.ref == ref), None)
    if inst is None:
        return ()
    if pin in inst.part.pins:
        if inst.pins.get(pin) != net:
            return ()
        nums = tuple(inst.part.pins[pin].pads)
    else:
        nums = ()
        for pname, p in inst.part.pins.items():
            if pin in p.pads and inst.pins.get(pname) == net:
                nums = (pin,)
                break
    return tuple(f"{ref}.{n}" for n in sorted(dict.fromkeys(nums), key=_padnum))


def _pads_of_net(design: Design, net: str) -> tuple[str, ...]:
    out: list[str] = []
    for inst in design.instances:
        for pname, n in inst.pins.items():
            if n != net:
                continue
            p = inst.part.pins.get(pname)
            for num in p.pads if p else ():
                pid = f"{inst.ref}.{num}"
                if pid not in out:
                    out.append(pid)
    return tuple(out)


def _part_pads(design: Design, ref: str) -> dict[str, str]:
    """pad number -> net, for one instance."""
    inst = next((i for i in design.instances if i.ref == ref), None)
    out: dict[str, str] = {}
    if inst is None:
        return out
    for pname, p in inst.part.pins.items():
        for num in p.pads:
            out[num] = inst.pins.get(pname, "")
    return out


# --- how a refusal names a line and a value ----------------------------------------------------------


def _who(p: PathReq) -> str:
    return f'Path("{p.net}") line {p.line}'


def _netreq_line(design: Design, c: Constraint | None) -> str:
    return f"NetReq line {c.line}" if c is not None and c.line else "its NetReq"


# --- the pitch a group is laid at ----------------------------------------------------------------


def group_pitch(cs: ConstraintSet, net: str, dia: float, drill: float) -> float:
    """`max(via_pitch + grid, antipad_pitch)` (§4.3): barrels of one net keep the hole and ring
    arithmetic the stitch ring uses, one pattern grid step more (its first radius is refused by
    `clears`), and do not merge their antipads in a pour they pass but do not join."""
    table = clearance_table(cs)
    grid = 0.05 if cs.stackup.layers <= 2 else 0.1
    pitch = round(table.via_pitch(net, net, drill, dia, drill, dia) + grid, 4)
    default = next((k.clearance_mm for k in cs.classes if k.name == "Default"), cs.stackup.clearance_min)
    for pnet, _layer in cs.planes:
        if pnet == net:
            continue
        one = max(default, table.between(pnet, net)[0])
        pitch = max(pitch, round(2 * one + cs.stackup.pour_min_mm + dia, 4))  # the plane pours' min_thickness (`route_native.plane_pours`)
    return pitch


def make_group(cs: ConstraintSet, net: str, amps: float, dt: float, via: ViaSpec, widths: tuple[tuple[str, Derived], ...]) -> ViaGroup | None:
    """The group a layer change carrying `amps` places, or None when one barrel holds it."""
    if amps <= 0 or not via.allowed or len(widths) < 2:
        return None
    stack = cs.stackup
    landing = max(d.value for _l, d in widths)
    pitch = group_pitch(cs, net, via.diameter_mm, via.drill_mm)
    # The landings run on the link's layers; the thinnest copper among them carries the group's spread (postfix B2).
    cu = stack.landing_oz(lay for lay, _d in widths)
    n, worst = via_group(amps, via.drill_mm, stack.via_plating_mm, dt, pitch, landing, board_mm=stack.board_mm, copper_oz=cu)
    if n <= 1:
        return None
    return ViaGroup(amps, n, pitch, landing, worst, via_amps(via.drill_mm, stack.via_plating_mm, dt), via.drill_mm, dt)


# --- the compile ---------------------------------------------------------------------------------


def _width_text(stack: Stackup, planes, widths: tuple[tuple[str, Derived], ...], current: CurrentSpec | None) -> str:
    """`0.781 mm on F.Cu (ipc2221_ext 2 A 10 C 1 oz; plane 0.593 at 1.53 mm B.Cu pour), 1.09 mm on B.Cu
    (ipc2152_fit, no plane)`: each layer's width, the curve that set it, and the plane under it."""
    from .stackup import cu_text, ipc2152_plane_modifier

    out = []
    for lay, d in widths:
        ref = _reference_for(stack, lay, planes)
        plane = f"plane {ipc2152_plane_modifier(ref[0]):.3f} at {_g(ref[0])} mm {ref[1]}" if ref is not None else "no plane"
        if d.source.formula in ("ipc2221_ext", "ipc2221_int") and current is not None:
            out.append(f"{_g(d.value)} mm on {lay} ({d.source.formula} {_g(current.amps)} A {_g(current.temp_rise_c)} C {cu_text(stack, lay)}; {plane})")
        elif d.source.formula == "ipc2152_fit":
            out.append(f"{_g(d.value)} mm on {lay} (ipc2152_fit {cu_text(stack, lay)}{', ' if ref is None else '; '}{plane})")
        else:
            out.append(f"{_g(d.value)} mm on {lay} ({d.source.formula} {d.source.ref}; {plane})")
    return ", ".join(out)


def _carries(stack: Stackup, planes, w: float, dt: float, layers) -> str:
    parts = []
    for lay in layers:
        ref = _reference_for(stack, lay, planes)
        a = track_amps(w, dt, stack, ref[0] if ref is not None else None, lay)
        over = f" (over {ref[2]})" if ref is not None else ""
        parts.append(f"{a:.3f} A on {lay}{over}")
    return " and ".join(parts)


def compile_paths(design: Design, cs: ConstraintSet, refusals: list[str]) -> tuple[ConstraintSet, dict[str, list[str]], tuple[LoopSpec, ...]]:
    """Every `Path()` and `Loop()` of `design` compiled onto `cs` (whose net numbers are final). Returns
    (the set with `Constraint.paths`, `off_path` and the net's own via groups filled in, the report
    lines per net, the loops). Refusals are appended to `refusals`."""
    stack = cs.stackup
    # The pours the board will have, not `Board(planes=)`: a width is sized for the plane really under it.
    planes = tuple(cs.planes)
    poured = {n for n, _l in cs.planes}
    have = stack.copper_layers()
    by_net = {c.net: c for c in cs.constraints}
    lines: dict[str, list[str]] = {}

    paths_by_net: dict[str, list[PathReq]] = {}
    for p in design.paths:
        paths_by_net.setdefault(p.net, []).append(p)

    new_constraints: dict[str, Constraint] = {}
    for net in sorted(paths_by_net):
        c = by_net.get(net)
        if c is None:
            continue  # `no net` refused by compile_constraints
        req = design.netreqs[c.req_index] if c.req_index >= 0 else None
        reqs = paths_by_net[net]
        all_pads = _pads_of_net(design, net)
        compiled: list[PathSpec] = []
        stations_of: list[tuple[tuple[str, ...], ...]] = []
        ok_paths: list[PathReq] = []
        for p in reqs:
            st = tuple(station_pads(design, net, m) for m in p.pads)
            if any(not s for s in st):
                continue  # the station refusal is compile_constraints' (no part / no pin / not on the net)
            bad = _path_refusals(design, cs, c, req, p, st, all_pads, poured, have)
            refusals.extend(bad)
            if bad:
                continue
            stations_of.append(st)
            ok_paths.append(p)
        # A link named twice carries two widths.
        seen: dict[frozenset, PathReq] = {}
        for p, st in zip(ok_paths, stations_of):
            for i in range(len(st) - 1):
                k = frozenset((st[i], st[i + 1]))
                if k in seen and seen[k] is not p:
                    first = seen[k]
                    refusals.append(f"{net}: Path line {first.line} and Path line {p.line} both name {p.pads[i]} -> {p.pads[i + 1]}; a link carries one width")
                else:
                    seen[k] = p
        # Postfix A8 (refutation physics r2): the declared links of a net's Paths form a tree. A link whose two
        # stations other declared links already join closes a loop; the router lays one route between joined copper
        # (route_native: `if find(i) == find(j): continue`), so that link got no copper and its current ran in the
        # others' copper, sized for less (loop.py: 1.9 A in 0.4 mm rated 1.152 A, build "copper verified"). On a pour
        # the links are the pour's, not tracks, so a poured net is not checked.
        if not any(n == net for n, _l in cs.planes):
            refusals.extend(_link_cycles(net, ok_paths, stations_of))
        # Sense paths written backwards (§3.2, refusals (a) and (b)).
        for p, st in zip(ok_paths, stations_of):
            if p.kind not in PATH_KINDS:
                continue
            why = _backwards(design, by_net, net, p, st, list(zip(ok_paths, stations_of)))
            if why:
                refusals.append(f"{_who(p)}, kind {p.kind}: {why}")
        # Currents must balance at a shared station (§3.3); on a pour the stations meet in the pour, not in a track.
        if not any(n == net for n, _l in cs.planes):
            refusals.extend(_balance(net, list(zip(ok_paths, stations_of))))
        # A Path with a current on a poured rail (PR 8): the pour is the copper between its stations, so its links
        # carry nothing of their own (a field-less chain) and each station's joint to the pour carries its draw.
        pour = next((lay for n, lay in cs.planes if n == net), None)
        poured_current = {id(p) for p in ok_paths if pour is not None and p.amps is not None}
        if poured_current:
            stripped = [replace(p, amps=None, draws=(), temp_rise_c=None) if id(p) in poured_current else p for p in ok_paths]
        else:
            stripped = ok_paths
        # The links.
        for p, q, st in zip(ok_paths, stripped, stations_of):
            spec = _compile_one(design, cs, c, req, q, st, planes, have, list(zip(stripped, stations_of)))
            if id(p) in poured_current:
                spec = replace(spec, pour=pour, joints=_pour_joints(design, stack, p, st))
            compiled.append(spec)
        if poured_current:
            compiled = _sum_shared_joints(compiled, stack)
            for spec in compiled:
                for j in spec.joints:
                    if j.barrel_mm is not None and j.one_amps + 1e-9 < joint_pad_amps(j) * len(j.pads) and (not j.lines or j.lines[0] == spec.line):
                        # E23 (PR 8): a barrel cannot be given a second hole; the joint is what it is.
                        refusals.append(joint_refusal(net, spec.line, pour, j, 1, j.one_amps))
        named = {x for st in stations_of for s in st for x in s}
        off = tuple(x for x in all_pads if x not in named)
        nc = replace(c, paths=tuple(compiled), off_path=off)
        new_constraints[net] = nc
        lines[net] = _path_lines(design, cs, nc, req, planes)
        refusals.extend(_impedance(nc, req, stack, planes))
        lines[net] += _series_notes(design, by_net, nc, stack, planes, poured)

    # A net carries no current of its own (`docs/physics-subjects.md` PR 3), so it has no via group: a
    # path's links carry theirs.
    out = [new_constraints.get(c.net, c) for c in cs.constraints]
    loops = compile_loops(design, cs, refusals)
    return replace(cs, constraints=tuple(out), loops=loops), lines, loops


def _path_refusals(design: Design, cs: ConstraintSet, c: Constraint, req, p: PathReq, st, all_pads, poured, have) -> list[str]:
    """§7: what a `Path()` line may not say, each stated as a fact (direction.md §7b: no rewritten line)."""
    out: list[str] = []
    who = _who(p)
    net = p.net
    nkind = "usb_hs" if c.kind == "pair" else c.kind
    nline = _netreq_line(design, c)
    fields = p.fields()
    # A multi-pad station: on a pair net one pad per station; in the middle of a path, split.
    multi = [i for i, s in enumerate(st) if len(s) > 1]
    if multi and c.pair is not None:
        i = multi[0]
        out.append(f"{who}: {p.pads[i]} is pads {_and(x.split('.', 1)[1] for x in st[i])}; a pair's Path names one pad per station")
        return out
    for i in multi:
        if 0 < i < len(st) - 1:
            ref = p.pads[i].split(".")[0]
            out.append(
                f"{who}: {p.pads[i]} is pads {_and(x.split('.', 1)[1] for x in st[i])} of {ref}, and it is not an end of this Path; version 1 "
                f"joins the pads of a multi-pad station only where a Path ends there (its join, §1.2), so in the middle of a Path nothing would "
                f"carry the Path's current between {_and(st[i])}"
            )
            return out
    # A station named twice: a path is a feed order, and a pad it returns to closes it on itself.
    seen_st: dict[tuple[str, ...], int] = {}
    for i, s_ in enumerate(st):
        if s_ in seen_st:
            out.append(f"{who}: {p.pads[i]} is station {seen_st[s_] + 1} ({p.pads[seen_st[s_]]}) again; a Path names each station once")
            return out
        seen_st[s_] = i
    # E11 (`docs/physics-subjects.md` §5, §7): a draw is at a station other than the first.
    for key, _a in p.draws:
        if key == p.pads[0]:
            out.append(f"{who}: amps= names {key}, its first station, where the current enters; a station in amps= is one the current leaves by")
            return out
        if key not in p.pads:
            out.append(f"{who}: amps= names {key}, which is not a station of this Path ({', '.join(p.pads)})")
            return out
    if not fields:
        return out
    pair_fields = [f for f in fields if f != "max_mm"]  # max_mm= is the node's span: E9 below, a NetReq's
    if c.pair is not None and pair_fields:
        f0 = pair_fields[0]
        half = f"{who}: {f0}= on half of the pair {'/'.join(sorted((net, c.pair.partner)))}"
        if f0 in ("amps", "temp_rise_c"):
            # Refutation one-home r1 (defect 2): this pointed at the Pair line, which refuses the keyword in turn.
            out.append(
                f"{half}: each member is {_g(c.width_mm.value)} mm wide, the width its pair's {_g(c.pair.z_diff_ohm)} ohm on Pair line {c.line} sets, "
                "and version 1 does not size or check a current on a coupled pair"
            )
        elif f0 == "z_se_ohm":
            out.append(
                f"{half}: each member's width and gap are sized for the pair's {_g(c.pair.z_diff_ohm)} ohm on Pair line {c.line}, and one width is not "
                "sized for two impedances"
            )
        elif f0 == "kind":
            out.append(f"{half}: kind= on a Path names a sense branch, and this Path is half of the coupled pair on Pair line {c.line}")
        else:
            # E17 (`docs/physics-subjects.md` §7): vias=, vias_max=, layers=, reference= and length_mm= are the Pair line's.
            out.append(f"{half}; the pair's numbers are on Pair line {c.line}, and a Path on a pair net names stations only")
        return out
    if c.group is not None and c.group.kind == "bus" and p.length_mm is not None:
        # E17's Bus twin (KD 8, G3): a Bus owns each member's length budget, as a Pair owns its two members'
        # (refutation one-home r1: Bus(length_mm=50) and its member's Path(length_mm=30) were both accepted).
        out.append(
            f"{who}: length_mm= on {net}, a member of Bus line {c.group.line} ({', '.join(c.group.members)}); a Bus holds the length "
            f"budget of each of its members (Bus length_mm=)"
        )
        return out
    # Why the net is poured: Board(planes=) when the board declares pours (on every stackup, plane_targets), else the
    # implicit two-layer back pour of a power GND (postfix A7: the text named the NetReq on a declared pour too).
    declared = bool(design.board is not None and design.board.planes)
    if net in poured and p.kind is not None:
        layer = next(lay for n, lay in cs.planes if n == net)
        why = "Board(planes=)" if declared else f"route_scene.plane_targets: {nline} is kind {c.kind} and names {net}"
        far = p.pads[-1]
        out.append(
            f"{who}: {net} is poured ({layer}; {why}), and version 1 does not build "
            f"a {p.kind} path on a poured net: the pour joins {far} to {net} wherever it reaches, so nothing can keep it off the rest of {net} "
            f"(a Kelvin tap); without kind= the line is not a Kelvin tap either, since {far} still reaches the pour "
            f"(net-path-physics.md §10 A: a sense path on a poured net is after version 1)"
        )
        return out
    if net in poured and p.amps is not None:
        layer = next(lay for n, lay in cs.planes if n == net)
        why = "Board(planes=)" if declared else f"route_scene.plane_targets: {nline} is kind {c.kind} and names {net}"
        gnd = design.nets.get(net)
        if gnd is not None and gnd.kind == "ground":
            # PR 8 (`docs/physics-subjects.md`): a rail's draws are declared; a ground's return current is not.
            out.append(
                f"{who}: {net} is poured ({layer}; {why}), and a Path with "
                f"amps= on a poured ground is a return path, which version 1 does not build"
            )
            return out
        if any(x is not None for x in (p.vias, p.layers, p.vias_max, p.reference, p.z_se_ohm, p.length_mm, p.max_mm)):
            # The pour is the copper between the stations: there is no track for a copper field to describe.
            f0 = next(k for k in ("vias", "layers", "vias_max", "reference", "z_se_ohm", "length_mm", "max_mm") if getattr(p, k) is not None)
            out.append(f"{who}: {f0}= describes the copper between two stations, and on the {net} pour ({layer}) that copper is the pour")
            return out
    # Every pad named (`docs/physics-subjects.md` KD 2): a current on every pad is a Path like any other, and so is a
    # kind. A Path on every pad is the whole node, so its kind's copper (width, layer, no vias, length) is the node's
    # copper and nothing else of the net meets it. Refutation one-home r1 (defect 6): the every-pad kind= refusal
    # and E14 sent a two-pad measurement line (thermistor to ADC) to each other, so kind="sense" had no home there.
    # kind=
    if p.kind is not None and p.kind not in PATH_KINDS:
        k = KIND_ALIASES.get(p.kind, p.kind)
        if k in PAIR_PRESETS or k in BUS_KINDS_MATCH:
            # E18: a pair's or a bus's kind holds for two paths at once.
            what = "Pair" if k in PAIR_PRESETS else "Bus"
            out.append(f"{who}: kind=\"{p.kind}\" is a {what} kind; on a Path, kind= names a sense branch and takes \"feedback\" or \"sense\"")
            return out
        if k in PRESETS:
            # Refutation round 2: this said `kind="analog" is the net's` of a power net. The kind a
            # net has is its NetReq's; say what it is.
            has = f"{net} is kind {c.kind} ({nline})" if c.line else f"{net} has no NetReq (kind {c.kind})"
            out.append(f"{who}: kind=\"{p.kind}\" is a net kind, and {has}; on a Path, kind= names a sense branch and takes \"feedback\" or \"sense\"")
        else:
            out.append(f"{who}: unknown kind {p.kind!r}; on a Path, kind= names a sense branch and takes \"feedback\" or \"sense\"")
        return out
    if p.kind is not None and p.amps is not None:
        out.append(f"{who}: kind=\"{p.kind}\" takes no amps=; a sense branch carries a measurement, not a load")
        return out
    if p.temp_rise_c is not None and p.amps is None:
        out.append(f"{who}: temp_rise_c= sizes a current, and this Path gives no amps=")
        return out
    if p.layers is not None:
        unknown = [lay for lay in p.layers if lay not in have]
        if unknown:
            out.append(f"{who}: {', '.join(unknown)}: {stack_name(cs)} has {', '.join(have)}")
            return out
    # E9 (`docs/physics-subjects.md` §7): a span is the node's, a length the path's.
    if p.max_mm is not None:
        out.append(f"{who}: max_mm= is the span of all of {net}'s pads, a NetReq field; a Path's own length between its stations is its length_mm=")
        return out
    # E10: a reference plane is the plane under one layer's copper.
    if p.reference is not None:
        layers = _path_layers(cs, c, p, have)[0]
        planes_of = {lay: r[1] for lay in layers if (r := _reference_for(cs.stackup, lay, tuple(cs.planes))) is not None}
        ref = canon_reference(cs.stackup, p.reference)
        under = [lay for lay, r_ in planes_of.items() if r_ == ref]
        poured = {lay for _n, lay in cs.planes}
        if not is_poured_reference(ref, poured):
            out.append(f'{who}: reference="{p.reference}" is not a plane this board pours ({_plane_list(cs)})')
            return out
        if len(layers) > 1:
            on = f"the plane under {_and(under)} only" if under else "under none of its layers"
            out.append(f'{who}: reference="{p.reference}" is {on}, and this Path may run on {_and(layers)}')
            return out
        if not under:
            out.append(f'{who}: reference="{p.reference}" is not the plane under {layers[0]}, the one layer this Path may run on')
            return out
    return out


def _plane_list(cs: ConstraintSet) -> str:
    return ", ".join(f"{lay} ({n})" for n, lay in cs.planes) or "none"


def _path_layers(cs: ConstraintSet, c: Constraint, p: PathReq, have) -> tuple[tuple[str, ...], str]:
    """The layers a path's copper may run on and where that came from: its own `layers=`, else its path kind's
    default, else every layer no plane takes (the net's, `docs/physics-subjects.md` P4)."""
    kp = PATH_PRESETS.get(p.kind) if p.kind in PATH_KINDS else None
    if p.layers is not None:
        return tuple(lay for lay in p.layers if lay in have), f"Path line {p.line}"
    if kp is not None:
        return tuple(lay for lay in kp.layers if lay in have), f"preset {p.kind}"
    return tuple(c.layers), ("Pair line " + str(c.line) if c.kind == "pair" else f"preset {c.kind}")


def stack_name(cs: ConstraintSet) -> str:
    return cs.stackup.name


def _and(xs) -> str:
    xs = list(xs)
    return xs[0] if len(xs) == 1 else ", ".join(xs[:-1]) + " and " + xs[-1]


def link_amps(p) -> tuple[float, ...]:
    """The current each station link of a `PathReq` (or `PathSpec`) carries, in feed order
    (`docs/physics-subjects.md` §5): a number on every link; with draws, by Kirchhoff's current law, the
    sum of the draws at the stations beyond the link. `()` for a path with no current."""
    n = len(p.pads if hasattr(p, "pads") else p.written) - 1
    if p.amps is None:
        return ()
    if not p.draws:
        return tuple(float(p.amps) for _ in range(n))
    names = p.pads if hasattr(p, "pads") else p.written
    at = {k: a for k, a in p.draws}
    return tuple(round(sum(at.get(names[j], 0.0) for j in range(i + 1, n + 1)), 9) for i in range(n))


def _carries_current(design: Design, pid: str) -> str:
    """Why current enters or leaves the net at pad `pid`, or "": a pad of a connector (reference
    designator `J` or `P`, IEEE 315: the net's current comes onto or off the board there), or a pin its
    symbol types `power_in` / `power_out`. Read off the design, not guessed from a pin name."""
    ref, num = pid.split(".", 1)
    inst = next((i for i in design.instances if i.ref == ref), None)
    if inst is None:
        return ""
    if inst.part.prefix in ("J", "P"):
        return f"a pad of connector {ref}, where its current comes onto or off the board"
    for pname, pin in inst.part.pins.items():
        if num in pin.pads and pin.etype in ("power_in", "power_out"):
            return f"{ref}'s {pname} pin, which its symbol types {pin.etype}"
    return ""


def _backwards(design: Design, by_net, net: str, p: PathReq, st, paths) -> str:
    """Why a sense path reads backwards, or "" (§3.2): (a) its far station is a pad of a two-pad part
    whose other pad is a station of a Path with `amps=` on its net (`docs/physics-subjects.md` PR 1: a
    net takes current only where a Path says so, never by its kind); (b) its far station is a pad the net's current
    enters or leaves by (`_carries_current`: a connector's, a power pin's), which the sense trace alone
    would then feed (refutation round 2: `Path("3V3", "U1.REFP", "J2.1", kind="sense")` was accepted and
    the excitation current ran in the 0.2 mm sense copper); (c) another path of the net names its last
    station and not its first."""
    far = st[-1]
    if len(far) == 1:
        ref, num = far[0].split(".", 1)
        pads = _part_pads(design, ref)
        if len(pads) == 2:
            other = next((n for k, n in pads.items() if k != num), "")
            other_pad = next((f"{ref}.{k}" for k in pads if k != num), "")
            q = next((q for q in design.paths if q.net == other and q.amps is not None and any(other_pad in station_pads(design, other, m) for m in q.pads)), None)
            if q is not None:
                return (
                    f"its far station {p.pads[-1]} is a pad of {ref}, whose other pad {other_pad} is a station of Path line {q.line}, which carries current on "
                    f"{other}, so it sits on the load; a sense Path starts where it leaves the rail"
                )
    why = next((w for w in (_carries_current(design, pid) for pid in far) if w), "")
    if why:
        return (
            f"its far station {p.pads[-1]} is {why}; a sense Path's copper beyond its first station {p.pads[0]} reaches the rest of {net} only "
            f"inside {p.pads[0]}, so {p.pads[-1]}'s current would run in the sense trace"
        )
    first, last = set(st[0]), set(st[-1])
    for q, qs in paths:
        if q is p:
            continue
        names = {x for s in qs for x in s}
        if last & names and not (first & names):
            return f"Path line {q.line} names {p.pads[-1]}, this Path's last station, and not {p.pads[0]}, its first; a sense Path starts at the station it shares"
    return ""


def _draw_at(p, i: int) -> float:
    """What Path `p` leaves at its station `i` (`i` > 0): its draw there (`amps={...}`), or, for a number `amps=`,
    all of it at the last station and nothing in between (`docs/physics-subjects.md` §5)."""
    if p.draws:
        return float(dict(p.draws).get(p.pads[i], 0.0))
    return float(p.amps or 0.0) if i == len(p.pads) - 1 else 0.0


def _balance(net: str, paths) -> list[str]:
    """At a station some path enters, the paths that leave it may not carry more than enter it (§3.3, §5: "the
    balance rule holds at every shared station"). A Path enters each of its stations after the first, its last
    and every one in between, and it brings there only what it draws there: its links before that station are
    sized for its own draws beyond them (`link_amps`), so a current that leaves a middle station has no copper
    sized for it (refutation physics r1, `midbranch`: a 0.25 mm link solved at 1.65 A).

    Round 3 (refute-current f4c, f2c): a Path with no `amps=` that enters a station brings no current to it (its
    links are sized for none), so it is counted at 0 A and not skipped; and a station is its pads, not its spelling
    (`U3.I/O1` is pads 1 and 6, so `U3.1` is the same station: `_station_groups`)."""
    out: list[str] = []
    group = _station_groups(s_ for _p, st in paths for s_ in st)
    stations: dict[tuple[str, ...], tuple[list[str], list, list]] = {}
    for p, st in paths:
        for i in range(1, len(st)):
            row = stations.setdefault(group[st[i]], ([], [], []))
            row[0].append(p.pads[i])
            row[1].append((p, i))
        if p.amps is not None:
            row = stations.setdefault(group[st[0]], ([], [], []))
            row[0].append(p.pads[0])
            row[2].append(p)
    for _st, (names, enter, leave) in sorted(stations.items(), key=lambda kv: kv[0]):
        if not enter or not leave:
            continue
        a_in = round(sum(_draw_at(p, i) for p, i in enter), 9)
        a_out = round(sum(p.amps for p in leave), 9)
        if a_out <= a_in + 1e-9:
            continue
        spell = list(dict.fromkeys(names))
        name = spell[0] if len(spell) == 1 else f"{spell[0]} (also written {_and(spell[1:])}, which shares its pads)"
        lv = " and ".join(f"Path line {p.line}" for p in leave)
        verb = "leave" if len(leave) > 1 else "leaves"
        between = " between them" if len(leave) > 1 else ""
        bare = [(p, i) for p, i in enter if p.amps is None]
        if bare:
            # A field-less Path's links are sized for no current: it brings none, whatever it is drawn as.
            facts = [
                f"Path line {p.line} enters {p.pads[i]} with no amps=, so its link {p.pads[i - 1]} -> {p.pads[i]} is sized for no current and brings none"
                for p, i in bare
            ]
            rest = [(p, i) for p, i in enter if p.amps is not None]
            if rest:
                facts.append(" and ".join(f"Path line {p.line} brings {_g(_draw_at(p, i))} A to it" for p, i in rest))
            out.append(f"{net}: {lv} {verb} {name} with {_g(a_out)} A{between}; {'; '.join(facts)}; currents at a station must balance")
            continue
        middle = [(p, i) for p, i in enter if i < len(p.pads) - 1]
        if middle:
            # A middle station: the entering Path goes on past it, so what it brings is its draw there, and the
            # link into the station is sized for that Path's own draws beyond it, never for the leaving current.
            facts = []
            for p, i in middle:
                into = f"{p.pads[i - 1]} -> {p.pads[i]}"
                facts.append(
                    f"{name} is a middle station of Path line {p.line}, which draws {_g(_draw_at(p, i))} A there and sizes its link {into} "
                    f"for {_g(link_amps(p)[i - 1])} A, the draws beyond that link"
                )
            ends = [p for p, i in enter if i == len(p.pads) - 1]
            if ends:
                facts.append(" and ".join(f"Path line {p.line} brings {_g(_draw_at(p, len(p.pads) - 1))} A to it" for p in ends))
            out.append(f"{net}: {lv} {verb} {name} with {_g(a_out)} A{between}; {'; '.join(facts)}; currents at a station must balance")
            continue
        if len(enter) == 1:
            e = enter[0][0]
            out.append(
                f"{net}: {lv} {verb} {name} with {_g(a_out)} A{between}, and Path line {e.line} brings {_g(a_in)} A to it, the only Path that enters {spell[0]}; "
                f"currents at a station must balance, and an off-path branch carries none of a Path's"
            )
        else:
            ev = " and ".join(f"Path line {p.line}" for p, _i in enter)
            out.append(f"{net}: {lv} {verb} {name} with {_g(a_out)} A{between}, and {ev} bring {_g(a_in)} A to it; currents at a station must balance")
    return out


def _link_cycles(net: str, paths: list, stations_of: list) -> list[str]:
    """Each declared link that closes a loop among the links declared before it (file order), with the links that
    already join its two stations. A station's pads are one node (a pin's pads are one station)."""
    adj: dict[str, list[tuple[str, str | None]]] = {}  # pad -> [(pad, "Path line N (A -> B)" or None inside a station)]

    def edge(a: str, b: str, what: str | None) -> None:
        adj.setdefault(a, []).append((b, what))
        adj.setdefault(b, []).append((a, what))

    def chain(a: str, b: str) -> list[str] | None:
        prev: dict[str, tuple[str, str | None] | None] = {a: None}
        queue = [a]
        while queue:
            u = queue.pop(0)
            if u == b:
                out_: list[str] = []
                while prev[u] is not None:
                    u, what = prev[u]  # type: ignore[misc]
                    if what is not None:
                        out_.append(what)
                return list(reversed(out_))
            for v, what in adj.get(u, ()):
                if v not in prev:
                    prev[v] = (u, what)
                    queue.append(v)
        return None

    out: list[str] = []
    done: set[frozenset] = set()
    for p, st in zip(paths, stations_of):
        for s_ in st:
            for x in s_[1:]:
                edge(s_[0], x, None)
        for i in range(len(st) - 1):
            a, b = st[i][0], st[i + 1][0]
            k = frozenset((st[i], st[i + 1]))
            if k in done:
                continue  # a link named twice: "a link carries one width" states it
            done.add(k)
            what = f"Path line {p.line} ({p.pads[i]} -> {p.pads[i + 1]})"
            got = chain(a, b)
            if got is not None:
                joined = " and ".join(got) if got else "one station"
                out.append(
                    f"{net}: {what} joins two stations that {joined} already join, so the declared links of {net} close a loop; "
                    f"the router lays one route between copper already joined, so this link would get no copper of its own and its "
                    f"current would run in the other links' copper (version 1 sizes each link for its own Path's current, and does "
                    f"not split a current around a loop)"
                )
                continue
            edge(a, b, what)
    return out


def joint_refusal(net: str, line: int, layer: str, j: Joint, got: int, one: float) -> str:
    """E23 (`docs/physics-subjects.md` §7): `Path("3V3") line 160: J_LOAD.1 draws 1 A, and its joint to the 3V3 pour on
    In2.Cu is 1 via (0.35/0.2 mm; via_barrel 0.527 A at 10 C)`. `got` is the vias the joint has (on the emitted board)."""
    verb = _joint_verb(j)
    if j.barrel_mm is not None:
        what = f"its own barrel ({_g(j.barrel_mm)} mm drill; via_barrel {one:g} A at {_g(j.temp_rise_c)} C)"
    else:
        what = f"{got} via{'s' if got != 1 else ''} ({_g(j.via[0])}/{_g(j.via[1])} mm; via_barrel {one:g} A at {_g(j.temp_rise_c)} C)"
    return f'Path("{net}") line {line}: {j.station} {verb} {_g(j.amps)} A{_joint_whose(j)}{_joint_share(j)}, and its joint to the {net} pour on {layer} is {what}'


def _pour_joints(design: Design, stack: Stackup, p: PathReq, st) -> tuple[Joint, ...]:
    """Each station's joint to the pour (`docs/physics-subjects.md` PR 8): the first station supplies the sum, a
    station named in `amps={...}` draws its value (a number `amps=` is drawn at the last station), and a station
    that draws nothing has no joint to size. An SMD pad's joint is `ceil(A / one)` stackup vias. The assumption
    (§10): a station's joint carries only its own draw; in a pour current also reaches a pin from its
    neighbours' joints, and only the SPICE stage could show by how much."""
    dt = p.temp_rise_c if p.temp_rise_c is not None else 10.0
    via = (stack.via_diameter, stack.via_drill)
    one = via_amps(stack.via_drill, stack.via_plating_mm, dt)
    draws = dict(p.draws) if p.draws else {p.pads[-1]: float(p.amps or 0.0)}
    out: list[Joint] = []
    for i, (written, pads) in enumerate(zip(p.pads, st)):
        amps = float(p.amps or 0.0) if i == 0 else float(draws.get(written, 0.0))
        if amps <= 0.0:
            continue
        drills = [_barrel_mm(design, x) for x in pads]
        if drills and all(d is not None for d in drills):
            # A through-hole pad's barrel already reaches the pour (`patterns.tap` skips it): that barrel is the joint.
            d = min(drills)  # type: ignore[type-var]
            out.append(Joint(written, tuple(pads), round(amps, 6), i == 0, 0, via, round(via_amps(d, stack.via_plating_mm, dt) * len(pads), 3), dt, barrel_mm=d))
            continue
        per_pad = amps / max(1, len(pads))
        n = max(1, math.ceil(per_pad / one - 1e-9))
        out.append(Joint(written, tuple(pads), round(amps, 6), i == 0, n, via, round(one, 3), dt))
    return tuple(out)


def _station_groups(pad_sets) -> dict[tuple[str, ...], tuple[str, ...]]:
    """`{pad set: its group's key}`: pad sets that share a pad are one station (round 3, refute-current f1c/f2c: a
    pin written once by its name and once by one of its pads, `J1.VBUS` = pads A4B9 and B4A9, and `J1.A4B9`). The
    key is the sorted union of the group's pads."""
    parent: dict[str, str] = {}

    def find(x: str) -> str:
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    sets = [tuple(ps) for ps in pad_sets]
    for ps in sets:
        for x in ps[1:]:
            ra, rb = find(ps[0]), find(x)
            if ra != rb:
                parent[max(ra, rb)] = min(ra, rb)
        if ps:
            find(ps[0])
    members: dict[str, set[str]] = {}
    for x in list(parent):
        members.setdefault(find(x), set()).add(x)
    return {ps: tuple(sorted(members[find(ps[0])])) for ps in sets if ps}


def joint_pad_amps(j: Joint) -> float:
    """What the worst pad of joint `j` carries: its `pad_amps` when Paths named overlapping pads of the station,
    else `amps` split evenly over its pads (a pin's pads share its current, the assumption of §10)."""
    if j.pad_amps:
        return max(a for _p, a in j.pad_amps)
    return j.amps / max(1, len(j.pads))


def _sum_shared_joints(specs: list, stack: Stackup) -> list:
    """Postfix A6 (refutation physics r2): two current Paths on one pour that share a station meet in that station's
    pad, so its one joint carries both currents. Each Path's joint was sized for its own, and the tap kept the last
    (a dict by pad): pourtree's R0.2 supplied 2 A through 2 vias rated 1.054 A. Every joint at a shared station is
    replaced by one sized for the sum of the currents the Paths bring there (supplies and draws added: an upper bound
    when they oppose), naming each Path line.

    Round 3 (refute-current f1c): a station is its pads, not its spelling. `J1.VBUS` (pads A4B9, B4A9) and `J1.A4B9`
    share pad A4B9, so their joints are one: each Path's current is split over the pads it names (pcbc's split), the
    pads' shares are added, and every pad gets the vias its worst pad needs (the tap lays one count per pad, the
    gate reads the worst pad: `joint_pad_amps`)."""
    group = _station_groups(j.pads for spec in specs for j in spec.joints)
    by_key: dict[tuple[str, ...], list[tuple[int, Joint]]] = {}
    for spec in specs:
        for j in spec.joints:
            by_key.setdefault(group[j.pads], []).append((spec.line, j))
    merged: dict[tuple[str, ...], Joint] = {}
    for key, rows in by_key.items():
        if len(rows) < 2:
            continue
        first = rows[0][1]
        amps = round(sum(j.amps for _l, j in rows), 6)
        dt = min(j.temp_rise_c for _l, j in rows)
        kinds = {j.supplies for _l, j in rows}
        verb = ("supplies" if True in kinds else "draws") if len(kinds) == 1 else "carries"
        share: dict[str, float] = {x: 0.0 for x in key}
        for _l, j in rows:
            for x, a in (j.pad_amps or tuple((x, j.amps / max(1, len(j.pads))) for x in j.pads)):
                share[x] += a
        pad_amps = tuple((x, round(share[x], 6)) for x in key)
        worst = max(a for _x, a in pad_amps)
        uneven = any(abs(a - amps / len(key)) > 1e-9 for _x, a in pad_amps)
        spellings = list(dict.fromkeys(j.station for _l, j in rows))
        station = spellings[0] if len(spellings) == 1 else f"{spellings[0]} (also written {_and(spellings[1:])})"
        drills = [j.barrel_mm for _l, j in rows if j.barrel_mm is not None]
        if len(drills) == len(rows) and set().union(*(set(j.pads) for _l, j in rows)) == set(key):
            d = min(drills)
            one = round(via_amps(d, stack.via_plating_mm, dt) * len(key), 3)
            n, barrel = 0, d
        else:
            one = round(via_amps(stack.via_drill, stack.via_plating_mm, dt), 3)
            n = max(1, math.ceil(worst / via_amps(stack.via_drill, stack.via_plating_mm, dt) - 1e-9))
            barrel = None
        merged[key] = replace(first, station=station, pads=key, amps=amps, supplies=verb == "supplies", vias=n, one_amps=one,
                              temp_rise_c=dt, barrel_mm=barrel, lines=tuple(sorted(dict.fromkeys(line for line, _j in rows))),
                              verb=verb, pad_amps=pad_amps if uneven else ())
    if not merged:
        return specs
    return [replace(spec, joints=tuple(merged.get(group[j.pads], j) for j in spec.joints)) for spec in specs]


def _joint_verb(j: Joint) -> str:
    if j.verb:
        return j.verb
    return "supplies" if j.supplies else "draws"


def _joint_whose(j: Joint) -> str:
    """` in all, for Path lines 9 and 10` on a station several Paths share (A6); empty otherwise."""
    if len(j.lines) < 2:
        return ""
    return " in all, for Path lines " + ", ".join(str(x) for x in j.lines[:-1]) + f" and {j.lines[-1]}"


def _joint_share(j: Joint) -> str:
    """`, 1 A of it through pad J1.A4B9` when Paths named overlapping pads of the station (round 3); empty otherwise."""
    if not j.pad_amps:
        return ""
    x, a = max(j.pad_amps, key=lambda kv: kv[1])
    return f", {_g(a)} A of it through pad {x}"


def _barrel_mm(design: Design, pad: str) -> float | None:
    """The drill of a through-hole pad (`REF.NUM`), read off its part's footprint file; None for an SMD pad."""
    from .pads import pad_specs

    ref, num = pad.split(".", 1)
    inst = next((i for i in design.instances if i.ref == ref), None)
    if inst is None or inst.part.origin is None or not inst.part.footprint:
        return None
    f = pathlib.Path(inst.part.origin) / inst.part.footprint
    try:
        specs = pad_specs(f.read_text())
    except OSError:
        return None
    s = next((x for x in specs if x.num == num and x.kind == "thru_hole" and x.drill is not None), None)
    if s is None:
        return None
    _oval, w, h = s.drill  # type: ignore[misc]
    return round(min(w, h) if h else w, 4)


def joint_text(j: Joint) -> str:
    """`U2.VOUT supplies 1 A: 2 vias 0.35/0.2 mm (via_barrel 0.527 A each at 10 C)` (the report, and the tap's why)."""
    verb = _joint_verb(j)
    if j.barrel_mm is not None:
        return f"{j.station} {verb} {_g(j.amps)} A{_joint_whose(j)}: its own barrel ({_g(j.barrel_mm)} mm drill; via_barrel {j.one_amps:g} A at {_g(j.temp_rise_c)} C)"
    each = f"{j.vias} via{'s' if j.vias != 1 else ''} {_g(j.via[0])}/{_g(j.via[1])} mm"
    per = " per pad" if len(j.pads) > 1 else ""
    return f"{j.station} {verb} {_g(j.amps)} A{_joint_whose(j)}{_joint_share(j)}: {each}{per} (via_barrel {j.one_amps:g} A each at {_g(j.temp_rise_c)} C)"


def _compile_one(design: Design, cs: ConstraintSet, c: Constraint, req, p: PathReq, st, planes, have, paths) -> PathSpec:
    stack = cs.stackup
    nkind = "usb_hs" if c.kind == "pair" else c.kind
    kp = PATH_PRESETS.get(p.kind) if p.kind in PATH_KINDS else None
    # layers and vias: the Path's, else its path kind's default, else every layer no plane takes and the
    # stackup via (`docs/physics-subjects.md` P4, KD 15).
    layers, layers_src = _path_layers(cs, c, p, have)
    if p.vias is not None:
        allowed, vias_src = bool(p.vias), f"Path line {p.line}"
    elif kp is not None:
        allowed, vias_src = kp.vias_allowed, f"preset {p.kind}"
    elif c.pair is not None:
        allowed, vias_src = c.via.allowed, f"Pair line {c.line}"
    else:
        allowed, vias_src = c.via.allowed, f"preset {nkind}"
    # current and width per layer, per link: a draws-Path's links each carry the sum of the draws beyond
    # them (`docs/physics-subjects.md` §5), so each is sized, and grouped, at its own current.
    count_max = p.vias_max if p.vias_max is not None else c.via.count_max
    # 10 C unless the Path writes temp_rise_c= (a net carries no current, so no rise of its own).
    dt = p.temp_rise_c if (p.amps is not None and p.temp_rise_c is not None) else 10.0

    def sized(a: float | None):
        """(width per layer, CurrentSpec, ViaSpec, ViaGroup) for copper carrying `a` A (None: no current of its own)."""
        cur_: CurrentSpec | None = None
        if a is not None:
            ws = []
            for lay in layers:
                w, cur = _current_width(a, dt, stack, _reference_for(stack, lay, planes), lay)
                ws.append((lay, w))
                if cur_ is None:
                    cur_ = cur
            wd = tuple(ws)
            if z_widths:
                # N4 with N2 on one piece of copper: the impedance sets the width; `_impedance` refuses a current
                # that width cannot carry rather than widen the line off its impedance.
                wd = tuple((lay, z_widths[lay][0] if lay in z_widths else w) for lay, w in wd)
        elif kp is not None:
            wd = tuple((lay, _preset(kp.width_mm, "mm", p.kind)) for lay in layers)
        elif z_widths:
            wd = tuple((lay, z_widths[lay][0] if lay in z_widths else c.width_mm) for lay in layers)
        else:
            # A field-less (or layers=/vias= only) link: the net's own copper, the class width, no current.
            wd = tuple((lay, c.width_mm) for lay in layers)
        amps_ = a
        # KD 13: a path with a current gets its own via size, 0.8/0.4 mm (0.6/0.3 on a switch node, whose
        # via pads are part of the node's copper area), never below the stackup's; a pcbc default with no
        # published source, kept so no declared copper moves (O7). A link with no current takes the net's.
        if amps_:
            vd, vh = CURRENT_VIA_MM["switch_node" if nkind == "switch_node" else "current"]
            dia, drill = max(vd, stack.via_diameter), max(vh, stack.via_drill)
        else:
            dia, drill = c.via.diameter_mm, c.via.drill_mm
        via_a = Derived(via_amps(drill, stack.via_plating_mm, dt), "A", Source("via_barrel", f"{_g(drill)}/{_g(stack.via_plating_mm)} mm")) if amps_ else None
        per_change = vias_per_change(amps_, drill, stack.via_plating_mm, dt) if amps_ else 1
        via_ = ViaSpec(dia, drill, allowed, 0 if not allowed else count_max, via_a, per_change)
        return wd, cur_, via_, (make_group(cs, p.net, amps_, dt, via_, wd) if amps_ else None)

    # z_se_ohm= (N4): the width each layer's copper needs over the plane under it; with a current as well,
    # the wider of the two is the link's (and `_impedance` refuses a current the impedance width cannot carry).
    z_widths: dict[str, tuple[Derived, tuple[float, Derived]]] = {}
    refs: dict[str, str] = {}
    for lay in layers:
        r = _reference_for(stack, lay, planes)
        if r is not None:
            refs[lay] = r[1]
    if p.reference is not None:
        refs = {lay: canon_reference(cs.stackup, p.reference) for lay in layers}
    if p.z_se_ohm is not None:
        for lay in layers:
            if lay in refs:
                w, z, _why = z_se_width(stack, p.z_se_ohm, lay, c.lane_clearance_mm)
                z_widths[lay] = (w, z)
    la = link_amps(p) or tuple(None for _ in range(len(st) - 1))
    per_link = {a: sized(a) for a in dict.fromkeys(la)}
    width, current, via, group = per_link[la[0]] if la else sized(None)
    # A keep-away is net to net (docs/physics-subjects.md PR 7, E8): a link keeps its net's.
    keep_away = tuple(sorted(c.keep_away, key=lambda k: k.other))
    links: list[Link] = []
    for i in range(len(st) - 1):
        a, b = st[i], st[i + 1]
        w_i, cur_i, via_i, group_i = per_link[la[i]]
        links.append(
            Link(
                net=p.net, a=a, b=b, written=(p.pads[i], p.pads[i + 1]), source=f"Path line {p.line}", key=f"{a[0]}~{b[0]}",
                width=w_i, via=via_i, group=group_i, layers=layers, current=cur_i if p.amps is not None or kp is None else None,
                keep_away=keep_away, sense=kp is not None, join=False, line=p.line, kind=p.kind if kp is not None else None,
                amps_written=p.amps is not None, layers_src=layers_src, vias_src=vias_src,
                z_se=tuple((lay, z) for lay, (_w, z) in z_widths.items()), reference=tuple(sorted(refs.items())) if (p.z_se_ohm is not None or p.reference is not None) else (),
            )
        )
    # The join of a multi-pad end station (§1.2): built once, by the first path (in file order) that
    # ends there, at the station's throughput: the larger of the summed declared currents of the paths
    # that enter it (it is their last station) and of those that leave it (their first). Refutation
    # round 2: each path used to build its own join at the largest single current while labelling it
    # with its own, so J1.VBUS shared by a 0.3 A and a 2 A path carried two joins, one sized for 2 A and
    # labelled 0.3 A, and neither for the 2.3 A the station's pads pass between them.
    for i in (0, len(st) - 1):
        s = st[i]
        if len(s) < 2:
            continue
        first_owner = next((q for q, qs in paths if qs[0] == s or qs[-1] == s), p)
        if first_owner is not p:
            continue
        a_in = sum(link_amps(q)[-1] for q, qs in paths if q.amps is not None and qs[-1] == s)
        a_out = sum(q.amps for q, qs in paths if q.amps is not None and qs[0] == s)
        jamps = max(a_in, a_out) if (a_in or a_out) else None
        here = la[0] if i == 0 else la[-1]  # the link of this path that meets the station
        width, current, via, group = per_link[here]
        jw, jcur = width, current
        if jamps is not None and (here is None or abs(jamps - here) > 1e-12):
            got = [_current_width(jamps, dt, stack, _reference_for(stack, lay, planes), lay) for lay in layers]
            jw = tuple((lay, g[0]) for lay, g in zip(layers, got))
            jcur = got[0][1] if got else current
        jgroup = make_group(cs, p.net, jamps, dt, via, jw) if jamps else group
        links.append(
            Link(
                net=p.net, a=s, b=s, written=(p.pads[i], p.pads[i]), source=f"Path line {p.line} (station {p.pads[i]})", key="~".join(s),
                width=jw, via=via, group=jgroup, layers=layers, current=jcur, keep_away=keep_away, sense=kp is not None, join=True,
                line=p.line, kind=p.kind if kp is not None else None, amps_written=jamps is not None, layers_src=layers_src, vias_src=vias_src,
            )
        )
    length = p.length_mm if p.length_mm is not None else (kp.length_mm if kp is not None else None)
    length_src = f"Path line {p.line}" if p.length_mm is not None else ""
    if length is None and c.pair is not None and c.pair.length_mm is not None:
        length = c.pair.length_mm  # Pair(length_mm=): each member's budget between the stations its Path names (G1)
        length_src = f"Pair line {c.pair.line}"
    if length is None and c.group is not None and c.group.kind == "bus" and c.group.length_mm is not None:
        length = c.group.length_mm  # Bus(length_mm=), round 3: the same home as a Pair's
        length_src = f"Bus line {c.group.line}"
    return PathSpec(
        net=p.net, stations=st, written=p.pads, line=p.line, kind=p.kind if kp is not None else None, links=tuple(links), length_mm=length,
        fielded=bool(p.fields()), amps=p.amps, draws=p.draws, z_se_ohm=p.z_se_ohm, length_src=length_src,
    )


def _path_lines(design: Design, cs: ConstraintSet, c: Constraint, req, planes) -> list[str]:
    """The report: one line per path (§5), then every off-path pad of a net whose paths carry a field
    or a kind, with what its branch carries on each layer (§3.1)."""
    stack = cs.stackup
    out: list[str] = []
    net = c.net
    # The largest current any link of the net's paths carries (a draws-Path's first link carries their sum).
    biggest = max((p for p in c.paths if p.amps is not None), key=lambda p: (max(link_amps(p)), -p.line), default=None)
    for p in c.paths:
        head = f"{net}: path {' -> '.join(p.written)} (Path line {p.line}{', kind ' + p.kind if p.kind else ''})"
        if p.pour is not None:
            # PR 8: the pour is the copper between the stations (not rated: pour, direction.md §5b); the joints carry the draws.
            joints = "; ".join(joint_text(j) for j in p.joints)
            out.append(f"{head}: the {net} pour on {p.pour} is the copper between its stations (not rated: pour); joints to the pour: {joints}")
            continue
        if not p.fielded:
            # A group's length budget is this path's though the Path line writes no field (Pair or Bus length_mm=).
            out.append(f"{head}: length {_g(p.length_mm)} mm ({p.length_src})" if p.length_mm is not None and p.length_src else head)
            continue
        if p.draws:
            # §5: each link at its own current, the sum of the draws beyond it.
            draws = ", ".join(f"{k} {_g(a)} A" for k, a in p.draws)
            parts = []
            for lk in (x for x in p.links if not x.join):
                parts.append(f"link {lk.written[0]} -> {lk.written[1]} {_g(lk.current.amps) if lk.current else '?'} A: {_link_text(stack, planes, p, lk)}")
            text = f"draws {draws}, {_g(p.amps)} A from {p.written[0]}; " + "; ".join(parts)
            for j in (x for x in p.links if x.join):
                text += f"; {j.written[0]}'s pads joined at {_g(j.current.amps) if j.current else '?'} A (join {j.key}, conservative: each pad carries part of the current)"
            out.append(f"{head}: {text}")
            continue
        lk = p.links[0]
        text = _link_text(stack, planes, p, lk, design=design, c=c)
        if p.length_mm is not None:
            if _wrote_length(design, p):
                where = f" (Path line {p.line})"
            elif p.length_src:
                where = f" ({p.length_src})"
            else:
                where = ""
            text += f", length {_g(p.length_mm)} mm{where}"
        for lay, (target, z) in (lk.z_se or ()):
            text += f"; {lay} over {dict(lk.reference).get(lay, '?')}: {z.line()}"
        if p.kind is not None:
            text += f"; meets {net} only inside {p.written[0]}, its first station"
        for j in (x for x in p.links if x.join):
            text += f"; {j.written[0]}'s pads joined at {_g(j.current.amps) if j.current else '?'} A (join {j.key}, conservative: each pad carries part of the current)"
        out.append(f"{head}: {text}")
    if c.fielded() and c.off_path:
        # §6: a branch no Path names carries no declared current; the report says what its copper carries.
        w = c.width_mm.value
        f = c.width_mm.source.formula
        src = "class floor" if f == "class_floor" else (f"preset {c.width_mm.source.ref}" if f == "preset" else f"{f} {c.width_mm.source.ref}".strip())
        for pad in c.off_path:
            carries = _carries(stack, planes, w, 10.0, c.layers)
            line = f"{net}: {pad} is on no Path; its branch is {_g(w)} mm ({src}) and carries no declared current; a {_g(w)} mm branch carries {carries} at 10 C"
            if biggest is not None:
                line += f", not Path line {biggest.line}'s {_g(max(link_amps(biggest)))} A"
            out.append(line)
    return out


def _wrote_length(design: Design | None, p: PathSpec) -> bool:
    return design is not None and any(q.net == p.net and q.line == p.line and q.length_mm is not None for q in design.paths)


def _link_text(stack: Stackup, planes, p: PathSpec, lk: Link, *, design: Design | None = None, c: Constraint | None = None) -> str:
    """One link's width, vias and layers as the report prints them."""
    if p.kind is not None:
        w = lk.width[0][1] if lk.width else c.width_mm  # type: ignore[union-attr]
        text = f"width {_g(w.value)} mm (preset {p.kind})"
    elif p.amps is not None:
        text = f"width {_width_text(stack, planes, lk.width, lk.current)}"
    else:
        text = f"width {', '.join(f'{_g(d.value)} mm on {lay}' for lay, d in lk.width)} ({_netreq_line(design, c)})"  # type: ignore[arg-type]
    if not lk.via.allowed:
        text += ", no vias" + (f" ({lk.vias_src})" if lk.vias_src.startswith("Path line") or lk.vias_src.startswith("NetReq") else "")
    elif lk.group is not None:
        text += "; " + lk.group.text(stack.via_plating_mm)
    elif p.amps is not None and lk.via.amps is not None:
        text += f"; 1 via per layer change (via_barrel {_g(lk.via.drill_mm)}/{_g(stack.via_plating_mm)} mm {lk.via.amps.value:g} A at {_g(lk.current.temp_rise_c if lk.current else 10.0)} C)"
    elif lk.vias_src.startswith("Path line"):
        text += f", vias allowed ({lk.vias_src})"
    show_layers = len(lk.layers) == 1 or p.kind is not None or lk.layers_src.startswith("Path line") or "yields" in lk.layers_src
    if show_layers:
        src = "" if lk.layers_src == f"preset {p.kind}" else f" ({lk.layers_src})"
        text += f", layers {', '.join(lk.layers)}{src}"
    return text


def _impedance(c: Constraint, req, stack: Stackup, planes) -> list[str]:
    """A current the impedance width cannot carry (§7), per link of a Path that writes both (N4 and N2 on one
    piece of copper), and an impedance with no plane under one of its layers (E20)."""
    out: list[str] = []
    for p in c.paths:
        for lk in (x for x in p.links if not x.join):
            if p.z_se_ohm is None:
                continue
            zl = dict(lk.z_se)
            missing = [lay for lay in lk.layers if lay not in dict(lk.reference)]
            if zl or missing:
                target = p.z_se_ohm
                for lay in missing:
                    # E20 (`docs/physics-subjects.md` §7): the net-level text with the Path's line.
                    pour_on = next((n for n, pl in planes if pl == lay), None)
                    if stack.layers <= 2 and pour_on is not None:
                        # Postfix A4: the layer is the pour's own, not a layer with a pour missing under it.
                        out.append(
                            f"{c.net}: {_g(target or 0)} ohm on {lay} (Path line {p.line}) has no reference plane: {lay} is "
                            f"the {pour_on} pour's own layer, and a two-layer board has no plane under it"
                        )
                    elif stack.layers <= 2:
                        out.append(
                            f"{c.net}: {_g(target or 0)} ohm on {lay} (Path line {p.line}) has no reference plane: this two-layer board pours B.Cu only "
                            "when GND is a power net (route_scene.plane_targets), and no NetReq names GND with kind power"
                        )
                    else:
                        want = stack.plane_below(lay) or stack.plane_above(lay)
                        out.append(f"{c.net}: {_g(target or 0)} ohm on {lay} (Path line {p.line}) has no reference plane: {want}, the layer next to {lay}, is not in Board(planes=)")
                    break
            if p.amps is None or not zl:
                continue
            for lay, d in lk.width:
                zw = dict(lk.z_se).get(lay)
                if zw is None or lk.current is None:
                    continue
                need = _current_width(lk.current.amps, lk.current.temp_rise_c, stack, _reference_for(stack, lay, planes), lay)[0]
                if need.value > d.value + 1e-9:
                    ref = _reference_for(stack, lay, planes)
                    carries = track_amps(d.value, lk.current.temp_rise_c, stack, ref[0] if ref else None, lay)
                    out.append(
                        f'Path("{c.net}") line {p.line}: amps={_g(lk.current.amps)} needs {_g(need.value)} mm on {lay} (current_width_mm: IPC-2221B over IPC-2152), and '
                        f"z_se_ohm={_g(zw[0])} on the same line holds {_g(d.value)} mm, which carries {carries:.3f} A"
                    )
                    break
            break
    return out


def _multi_pad_pour_notes(design: Design, c: Constraint, p, ref: str, num: str, member: str, a: float, pads: dict[str, str], poured) -> list[str]:
    """Postfix A5 (refutation migration r2): a current Path through a pad of a part with more than two pads (a FET,
    a regulator) whose other pads include one on a poured net. What leaves through which pad is the part's, not the
    netlist's, so the note bounds it (up to the Path's current) and says that no joint of those pads to the pour is
    checked; node's LOAD (1 A into Q1.D) leaves at Q1.S into the GND pour on a 0.16 mm stub and was named nowhere."""
    out: list[str] = []
    by_other: dict[str, list[str]] = {}
    for k, n in pads.items():
        if k != num and n in poured and n != c.net:
            by_other.setdefault(n, []).append(f"{ref}.{k}")
    for other, ids in by_other.items():
        if any(
            q.amps is not None and q.amps >= a - 1e-9 and any(pid in station_pads(design, other, m) for m in q.pads for pid in ids)
            for q in design.paths
            if q.net == other
        ):
            continue
        ids = sorted(ids, key=lambda x: _padnum(x.split(".", 1)[1]))
        named = ", ".join(ids) if len(ids) <= 4 else f"{len(ids)} pads of {ref}"
        way = "into" if member != p.written[0] else "out of"
        out.append(
            f"{other}: {ref} has {len(pads)} pads; Path line {p.line} carries {_g(a)} A {way} {ref}.{num}, and up to {_g(a)} A of it "
            f"can pass through {named} on {other}, into the {other} pour; version 1 does not size a return in a pour "
            f"(net-path-physics.md §10 A), so nothing checks that {'their' if len(ids) > 1 else 'its'} joint to the pour carries {_g(a)} A"
        )
    return out


def _series_notes(design: Design, by_net, c: Constraint, stack: Stackup, planes, poured=frozenset()) -> list[str]:
    """§3.3's second note: a path with amps= names a pad of a two-pad part (not a capacitor) whose other
    pad is on a net that carries less."""
    out: list[str] = []
    for p in c.paths:
        if p.amps is None:
            continue
        la = link_amps(p)
        for k, (s, member) in enumerate(zip(p.stations, p.written)):
            # The current through this station's pad (`docs/physics-subjects.md` PR 2): the first link's at
            # the first station, a draws-Path's draw at a drawing station (0 at one that draws nothing),
            # else the link that arrives (every link of a number path carries the one number).
            a = la[0] if k == 0 else (dict(p.draws).get(member, 0.0) if p.draws else la[k - 1])
            if a <= 0:
                continue
            if len(s) != 1:
                continue
            ref, num = s[0].split(".", 1)
            inst = next((i for i in design.instances if i.ref == ref), None)
            pads = _part_pads(design, ref)
            if inst is None or inst.part.prefix == "C":
                continue
            if len(pads) > 2:
                out += _multi_pad_pour_notes(design, c, p, ref, num, member, a, pads, poured)
                continue
            if len(pads) != 2:
                continue
            other_num, other = next(((k, n) for k, n in pads.items() if k != num), ("", ""))
            oc = by_net.get(other)
            if oc is None or other == c.net:
                continue
            through = any(q.amps is not None and q.amps >= a - 1e-9 for q in design.paths if q.net == other and any(f"{ref}.{other_num}" in station_pads(design, other, m) for m in q.pads))
            if through:
                continue
            if other in poured:
                # A poured net's return current flows in the pour; a Path with amps= there is refused
                # (§7), so the note does not ask for one.
                out.append(
                    f"{other}: {ref} is a two-pad part between {c.net} and {other}; Path line {p.line} carries {_g(a)} A {'out of' if member == p.written[0] else 'into'} {ref}.{num}, "
                    f"so if {ref} passes DC, {ref}.{other_num} carries {_g(a)} A too, into the {other} pour; version 1 does not size a return in a pour "
                    f"(net-path-physics.md §10 A), so nothing checks that {ref}.{other_num}'s joint to the pour carries {_g(a)} A"
                )
                continue
            o_w = oc.width_mm.value
            lay0 = oc.layers[0] if oc.layers else "F.Cu"
            ref0 = _reference_for(stack, lay0, planes)
            carry = f" (its {_g(o_w)} mm carries {track_amps(o_w, 10.0, stack, ref0[0] if ref0 else None, lay0):.3f} A on {lay0})"
            # PR 3: the note fires when no Path on the other net through that pad carries the current.
            out.append(
                f"{other}: {ref} is a two-pad part between {c.net} and {other}; Path line {p.line} carries {_g(a)} A {'out of' if member == p.written[0] else 'into'} {ref}.{num}, "
                f"so if {ref} passes DC, {ref}.{other_num} carries {_g(a)} A too, and {other} declares no current through {ref}.{other_num}{carry}"
            )
    return out


# --- Loop() --------------------------------------------------------------------------------------


def compile_loops(design: Design, cs: ConstraintSet, refusals: list[str]) -> tuple[LoopSpec, ...]:
    """Every `Loop()` (§6): one pad per station, each step on one net or across one part's body, a
    budget from `mm2=` or the pcbc default (20 mm2 when a part of the loop has a `switch_node` pad,
    else 6)."""
    out: list[LoopSpec] = []
    by_net = {c.net: c for c in cs.constraints}
    for lp in design.loops:
        who = f"Loop line {lp.line}"
        pads: list[str] = []
        ok = True
        for m in lp.pads:
            ref, _, pin = m.partition(".")
            inst = next((i for i in design.instances if i.ref == ref), None)
            if inst is None:
                refusals.append(f"{who}: no part {ref}")
                ok = False
                break
            nums: tuple[str, ...] = ()
            if pin in inst.part.pins:
                nums = tuple(inst.part.pins[pin].pads)
            elif any(pin in p.pads for p in inst.part.pins.values()):
                nums = (pin,)
            if not nums:
                refusals.append(f"{who}: {ref} has no pin {pin}")
                ok = False
                break
            if len(nums) > 1:
                shown = sorted(nums, key=_padnum)
                listing = ", ".join(shown) if len(shown) <= 6 else ", ".join(shown[:6]) + ", ..."
                refusals.append(f"{who}: {m} is {len(nums)} pads of {ref} ({listing}); a loop's station is one pad")
                ok = False
                break
            pads.append(f"{ref}.{nums[0]}")
        if not ok:
            continue
        twice = next((x for i, x in enumerate(pads) if x in pads[:i]), None)
        if twice is not None:
            i, j = pads.index(twice), len(pads) - 1 - pads[::-1].index(twice)
            refusals.append(
                f"{who}: {lp.pads[j]} is pad {twice}, which is already station {i + 1} ({lp.pads[i]}); a loop passes each pad once, "
                f"and a pad named twice closes the loop early around nothing"
            )
            continue
        steps: list[str] = []
        parts: list[str] = []
        net_of = {}
        for pid in pads:
            ref, num = pid.split(".", 1)
            net_of[pid] = _part_pads(design, ref).get(num, "")
        n = len(pads)
        for i in range(n):
            a, b = pads[i], pads[(i + 1) % n]
            ra, rb = a.split(".")[0], b.split(".")[0]
            if net_of[a] and net_of[a] == net_of[b]:
                steps.append(f"net {net_of[a]}")
            elif ra == rb:
                steps.append(f"part {ra}")
            else:
                hint = ""
                same = [x for x, nn in _part_pads(design, ra).items() if nn == net_of[b]]
                if same:
                    want = f"{ra}.{same[0]}"
                    at = pads.index(want) if want in pads else -1
                    # A fact about the part, not an edit: where its pad on the next station's net is.
                    hint = f"; {ra}'s {net_of[b]} pad is {ra}.{same[0]}" + (f", station {at + 1} of this loop" if at >= 0 else "")
                refusals.append(
                    f"{who}: {lp.pads[i]} -> {lp.pads[(i + 1) % n]} is neither one net ({lp.pads[i]} is on {net_of[a] or 'no net'}, "
                    f"{lp.pads[(i + 1) % n]} on {net_of[b] or 'no net'}) nor one part; each step of a loop stays on one net or crosses one part{hint}"
                )
                ok = False
                break
        if not ok:
            continue
        crossings = sum(1 for x in steps if x.startswith("part "))
        if crossings < 2:
            on = sorted({net_of[x] for x in pads if net_of[x]})
            others = []
            for pid in pads:
                ref, num = pid.split(".", 1)
                pp = _part_pads(design, ref)
                if len(pp) == 2:
                    o = next(k for k in pp if k != num)
                    if f"{ref}.{o}" not in pads:
                        others.append(f"{ref}.{o} ({pp[o]})")
            hint = f"; {', '.join(dict.fromkeys(others))} {'is' if len(set(others)) == 1 else 'are'} the other side" if others else ""
            refusals.append(
                f"{who}: its steps cross {crossings} part{'s' if crossings != 1 else ''} and stay on {', '.join(on) or 'no net'}; a loop goes out through one part "
                f"and back through another, so its pads are on two nets at least and it crosses two parts at least{hint}"
            )
            continue
        for pid in pads:
            r = pid.split(".")[0]
            if r not in parts:
                parts.append(r)
        sw = next(((r, nn) for r in parts for nn in sorted(set(_part_pads(design, r).values())) if by_net.get(nn) is not None and by_net[nn].kind == "switch_node"), None)
        kind = "switch_node" if sw else "power"
        if lp.mm2 is not None:
            budget = Derived(round(lp.mm2, 1), "mm2", Source("Loop", f"line {lp.line}"))
        else:
            budget = Derived(DEFAULT_LOOP_MM2[kind], "mm2", Source("pcbc default", "", f"{sw[0]} carries {sw[1]}" if sw else "a decap budget"))
        # Round 3 (refute-onehome): one loop, one budget. The same pads, in any rotation, direction or spelling
        # (U1.3 for U1.VIN), are the same current loop: a second Loop() gave it a second budget, and both compiled.
        first = next((o for o in out if set(o.pads) == set(pads)), None)
        if first is not None:
            refusals.append(
                f"{who}: {' -> '.join(lp.pads)} is the loop of Loop line {first.line} ({' -> '.join(first.written)}): the same pads "
                f"({', '.join(sorted(pads))}); one loop, one budget"
            )
            continue
        out.append(LoopSpec(pads=tuple(pads), written=lp.pads, line=lp.line, budget=budget, steps=tuple(steps), parts=tuple(parts)))
    # One attachment, one statement: a cap's step to an IC pin on one net, when its Place(to=) names
    # another pin of that IC.
    for spec in out:
        for i, pid in enumerate(spec.pads):
            nxt = spec.pads[(i + 1) % len(spec.pads)]
            ra, rb = pid.split(".")[0], nxt.split(".")[0]
            for cap, ic, ic_pad in ((ra, rb, nxt), (rb, ra, pid)):
                pl = next((x for x in design.places if x.ref == cap and getattr(x, "to", None)), None)
                if pl is None or "." not in str(pl.to) or ra == rb:
                    continue
                to_ref, to_pin = str(pl.to).split(".", 1)
                if to_ref != ic:
                    continue
                to_pads = station_pads_any(design, ic, to_pin)
                if ic_pad in to_pads or not to_pads:
                    continue
                if len(_part_pads(design, cap)) != 2:
                    continue
                ic_nets = _part_pads(design, ic)
                if ic_nets.get(ic_pad.split(".", 1)[1]) != ic_nets.get(to_pads[0].split(".", 1)[1]):
                    continue  # the step is on another net than the attachment: two statements about two nets
                pin_name = _pin_of(design, ic, ic_pad.split(".", 1)[1])
                refusals.append(
                    f"Loop line {spec.line}: its step {pid} -> {nxt} lands on another pin of {ic} ({pin_name}) than layout.core.py:{getattr(pl, 'line', 0)}, "
                    f"which attaches {cap} to {pl.to}; one attachment, one statement"
                )
    # NetReq(loop_mm2=) is refused at load (E7, docs/physics-subjects.md PR 7): a loop's budget is its Loop's.
    return tuple(out)


def station_pads_any(design: Design, ref: str, pin: str) -> tuple[str, ...]:
    inst = next((i for i in design.instances if i.ref == ref), None)
    if inst is None:
        return ()
    if pin in inst.part.pins:
        return tuple(f"{ref}.{n}" for n in inst.part.pins[pin].pads)
    return (f"{ref}.{pin}",) if any(pin in p.pads for p in inst.part.pins.values()) else ()


def _pin_of(design: Design, ref: str, num: str) -> str:
    inst = next((i for i in design.instances if i.ref == ref), None)
    if inst is None:
        return num
    for pname, p in inst.part.pins.items():
        if num in p.pads:
            return pname
    return num




def loop_area(points) -> float:
    """Shoelace area of the points in the order given; a self-crossing loop by its hull."""
    n = len(points)
    if n < 3:
        return 0.0
    s = 0.0
    for i in range(n):
        x0, y0 = points[i]
        x1, y1 = points[(i + 1) % n]
        s += x0 * y1 - x1 * y0
    return abs(s) / 2.0


__all__ = ["compile_paths", "compile_loops", "station_pads", "group_pitch", "make_group", "loop_area"]
