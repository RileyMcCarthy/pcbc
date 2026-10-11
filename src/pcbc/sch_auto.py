"""The schematic auto-layout: a readable sheet from the netlist alone, around the locks in `schematic.core.py`
(docs/schematic-layout.md §2, §3).

1. **Regions** (§2.2). Declared regions hold their `members` (closed). Every other part goes to an inferred region:
   anchors (symbols with more than two pins, and connectors that carry a signal net), passive units (two-pin parts
   joined by signal nets, which join one anchor whole: a connector, else the anchor with the fewest pins), decoupling
   banks (two-pin parts on power and ground only, one bank per set of rails, which join the rail's regulator, else the
   IC with most pins on the rail, else an anchor on it, else the region of the first part the rail reaches), power-only
   connectors (which join their rail's region), and the anchor graph split at its hubs (a component with more than two
   anchors loses its most connected anchor, which is a region alone, until none has). A part attach-locked to an
   undeclared part goes where that part goes.
2. **Inside a region** (§2.3): the anchor at the region's first member slot, every other member placed by a `SchPlace`
   spec — the one `schematic.core.py` wrote, or one the auto-layout writes in the same form (`to=`, `along=`, `below=`).
   Pose locks (`at=`) are never moved; `sch_place._separate` pushes, the pull slides hangers back toward their targets.
3. **Order** (§2.4): regulator regions that are not hubs in row 0; hubs in the middle of row 1, most connected in the
   middle; a region beside a hub on the side most of the hub pins it reaches are drawn on (a tie goes left), ordered
   down a column by the mean height of those pins. `SchRegion(row=, col=)` locks a cell, `at=` an origin.

The result is the same `SchPlaceSpec`s a person writes, resolved by the solvers `sch_place` already has, so a gen line
copied into core reproduces the pose it came from (`sch_gen`).
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field

from .model import SchPlaceSpec, SchRegionSpec
from .sch_core import AUTO, SCH_CORE_FILE

_GRID = 1.27
PACK_ORIGIN = 12.7  # the first frame's corner (inside the drawing sheet's 12 mm border)
PAD = 5.08  # region origin to its first member slot
HEADER = 6.35  # the region title's band above the members
CLUSTERS_PER_COLUMN = 2  # inside a region: clusters stacked down one column before the next column opens
GUTTER = 12.7  # frame to frame (a frame is what is drawn for the region, measured once a pass has drawn it)


def _snap(v: float) -> float:
    return round(v / _GRID) * _GRID


@dataclass
class Region:
    name: str
    title: str
    members: list[str]
    anchors: list[str]
    declared: SchRegionSpec | None = None
    hub: bool = False
    regulator: bool = False
    outline: bool = True
    row: int | None = None
    col: int | None = None
    stack: int = 0
    side: str | None = None  # left/right of the hub it is beside
    origin: tuple[float, float] = (0.0, 0.0)
    frame: tuple[float, float] = (0.0, 0.0)
    rect: tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)  # the frame, relative to the origin
    norm: tuple[float, float] = (0.0, 0.0)  # the shift that puts the members' box at the first member slot (`_normalize`)
    order: list[str] = field(default_factory=list)  # placement order of its members
    note: str = ""  # how the auto-layout placed it, for gen's comment


@dataclass
class Layout:
    regions: list[Region]
    region_of: dict[str, Region]
    specs: dict[str, SchPlaceSpec]  # every part's spec: locked (source "core") or the auto-layout's (source "auto")
    met: dict[str, SchPlaceSpec] = field(default_factory=dict)  # core attach lines equal to the auto spec (`met_attaches`)


# --------------------------------------------------------------------------- nets


def _sig(net: str, kinds: dict[str, str]) -> bool:
    return bool(net) and kinds.get(net, "net") not in ("power", "ground") and not net.startswith("unconnected") and not net.endswith(".NC")


def _is_conn(ref: str) -> bool:
    return ref[:1] in ("J", "P")


# --------------------------------------------------------------------------- §2.2 regions


def infer_regions(design, parts: list, kinds: dict[str, str]) -> list[Region]:
    """The regions of the sheet: the declared ones and the inferred ones for every part outside them.

    A declared region named `auto-...` whose members are the members the auto-layout infers for the region of that name
    is that inferred region, with its title, outline and origin from the line: gen's region line copied into core
    changes nothing (refute:lock 2026-10-09: four of DS2's inferred region lines copied together left two undeclared
    regions, which the hub cut no longer split, and five parts went off the paper). The inference always runs over every
    part outside the other declared regions, so declaring an inferred region never changes how the rest are inferred."""
    by = {p.ref: p for p in parts}
    order = [p.ref for p in parts]
    pos = {r: i for i, r in enumerate(order)}
    autos = [r for r in design.sch_regions if r.name.startswith(AUTO)]
    neutral = {r.name for r in autos}
    while True:
        closed = [r for r in design.sch_regions if r.name not in neutral]
        taken = {m for r in closed for m in r.members}
        inferred = _infer(design, [by[r] for r in order if r not in taken], by, kinds, pos)
        named = {r.name: r for r in inferred}
        still = {r.name for r in autos if r.name in neutral and r.name in named and set(named[r.name].members) == set(r.members)}
        if still == neutral:
            break
        neutral = still
    declared: list[Region] = []
    # File order never places a region (docs/schematic-layout.md §3.3): declared regions are taken in the order of their
    # first member in board.py, then by name. A gen region line copied over its core line, in any place in the file,
    # then lays out the sheet it came from (refute:split 2026-10-08: DS2's adc line copied in the middle of the file moved
    # J2 and refused its SchPower locks; the same line at the end did not).
    for spec in sorted(closed, key=lambda r: (min((pos[m] for m in r.members if m in pos), default=len(pos)), r.name)):
        members = [m for m in order if m in set(spec.members)]
        anchor = _declared_anchor(design, spec, by, kinds)
        declared.append(Region(spec.name, spec.title or spec.name, members, [anchor] if anchor else [], declared=spec, outline=spec.outline))
    if len(inferred) == 1 and not declared:
        inferred[0].outline = False  # a sheet with one region draws no outline
    for reg in inferred:
        spec = next((r for r in autos if r.name == reg.name and r.name in neutral), None)
        if spec is not None:
            reg.declared = spec
            reg.title = spec.title if spec.title is not None else reg.title
            reg.outline = spec.outline
    regions = declared + inferred
    # A declared region whose anchor is an IC on two supply rails (a regulator) sits in row 0 like an inferred one;
    # whether it is a hub is decided by the grid.
    for reg in declared:
        a = reg.anchors[0] if reg.anchors else None
        rails = {pn.net for pn in by[a].pins if pn.net and kinds.get(pn.net) == "power"} if a else set()
        sig = any(_sig(pn.net, kinds) for pn in by[a].pins) if a else False
        reg.regulator = a is not None and not _is_conn(a) and len(rails) >= 2 and not sig
    return regions


def _declared_anchor(design, spec: SchRegionSpec, by: dict, kinds: dict[str, str]) -> str | None:
    """A declared region's anchor: the member whose SchPlace has no anchor keyword, else its first pose-locked member,
    else the member with most signal neighbours among the anchors (connectors first), else its first member."""
    members = [m for m in spec.members if m in by]
    if not members:
        return None
    specs = {p.ref: p for p in design.sch_places}
    for m in members:
        s = specs.get(m)
        if s is not None and s.anchor_kind() == "anchor":
            return m
    # A pose lock does not make its part the anchor: the anchor is the auto-layout's, so a pose copied from gen sits in
    # the frame it was copied from (the region's other members are placed around the same anchor).
    locked = {m for m in members if m in specs and specs[m].anchor_kind() in ("to", "along", "below")}
    cands = [m for m in members if m not in locked and (len(by[m].pins) > 2 or (_is_conn(m) and any(_sig(pn.net, kinds) for pn in by[m].pins)))]
    if cands:
        conns = [m for m in cands if _is_conn(m)]
        return max(cands, key=lambda m: (len(by[m].pins), -members.index(m))) if not conns else conns[0]
    free = [m for m in members if m not in locked]
    return (free or members)[0]


def _infer(design, parts: list, by_all: dict, kinds: dict[str, str], pos: dict[str, int]) -> list[Region]:
    if not parts:
        return []
    refs = [p.ref for p in parts]
    by = {p.ref: p for p in parts}
    mine = set(refs)
    specs = {p.ref: p for p in design.sch_places}

    def sig(n):
        return _sig(n, kinds)

    anchors = [p.ref for p in parts if len(p.pins) > 2 or _is_conn(p.ref)]
    power_only = [a for a in anchors if _is_conn(a) and not any(sig(pn.net) for pn in by[a].pins)]
    anchors = [a for a in anchors if a not in power_only]
    # A part attach-locked to another part follows it (the lock is the AI's grouping): it is not an anchor of its own.
    follows = {}
    for r in refs:
        s = specs.get(r)
        if s is not None and s.anchor_kind() in ("to", "along", "below"):
            t = (s.to or s.along or "").split(".", 1)[0] or s.below
            if t in mine:
                follows[r] = t
    anchors = [a for a in anchors if a not in follows]
    power_only = [a for a in power_only if a not in follows]
    on_net: dict[str, list[str]] = defaultdict(list)
    for p in parts:
        for pin in p.pins:
            if pin.net and p.ref not in on_net[pin.net]:
                on_net[pin.net].append(p.ref)
    home: dict[str, str] = {a: a for a in anchors}
    path_next: dict[str, str] = {}
    for pr in getattr(design, "paths", []) or []:
        pads = [s.split(".", 1)[0] for s in pr.pads]
        for a, b in zip(pads, pads[1:]):
            if a not in mine or b not in mine:
                continue
            if a not in anchors and b in anchors:
                path_next.setdefault(a, b)
            if b not in anchors and a in anchors:
                path_next.setdefault(b, a)
            # A cap a Path() names between a connector and an IC goes with the IC (it decouples the pin).
            for cap, other in ((a, b), (b, a)):
                if cap in path_next and _is_conn(path_next[cap]) and other in anchors and not _is_conn(other):
                    path_next[cap] = other
    rails_of = {p.ref: tuple(sorted({pn.net for pn in p.pins if pn.net and not sig(pn.net)})) for p in parts}
    passives = [r for r in refs if r not in anchors and r not in power_only and r not in follows]
    unit_of: dict[str, str] = {}
    units: dict[str, list[str]] = {}
    for r in passives:
        if r in unit_of:
            continue
        unit, stack = [r], [r]
        unit_of[r] = r
        while stack:
            cur = stack.pop()
            for pin in by[cur].pins:
                if not sig(pin.net):
                    continue
                for o in sorted(on_net[pin.net], key=pos.get):
                    if o in passives and o not in unit_of:
                        unit_of[o] = r
                        unit.append(o)
                        stack.append(o)
        units[r] = sorted(unit, key=pos.get)
    for key, unit in units.items():
        nets = {pin.net for u in unit for pin in by[u].pins if sig(pin.net)}
        if not nets:
            continue  # a decoupling bank, below
        cands = sorted({o for n in nets for o in on_net[n] if o in anchors}, key=pos.get)
        conns = [c for c in cands if _is_conn(c)]
        pick = conns[0] if conns else (min(cands, key=lambda a: (len(by[a].pins), pos[a])) if cands else None)
        for u in unit:
            home[u] = pick or key
    banks: dict[tuple, list[str]] = defaultdict(list)
    for r in passives:
        if r not in home:
            banks[rails_of[r]].append(r)

    def power_rails(a):
        return {pn.net for pn in by[a].pins if pn.net and kinds.get(pn.net) == "power"}

    for rails, bank in banks.items():
        for c in [c for c in bank if c in path_next]:
            home[c] = path_next[c]
        bank = [c for c in bank if c not in path_next]
        if not bank:
            continue
        rail = [n for n in rails if kinds.get(n) == "power"]
        on_rail = [a for a in anchors if power_rails(a) & set(rail)]
        regs = [a for a in on_rail if not _is_conn(a) and len(power_rails(a)) >= 2]
        ics = sorted((a for a in on_rail if not _is_conn(a)), key=lambda a: (-len(by[a].pins), pos[a]))
        pick = (regs or ics or on_rail or [None])[0]
        if pick is None:
            pick = next((home[o] for n in rail for o in sorted(on_net[n], key=pos.get) if o in home), None)
        for c in bank:
            home[c] = pick or bank[0]
    for c in power_only:
        rail = [pn.net for pn in by[c].pins if kinds.get(pn.net) == "power"]
        home[c] = next((home[o] for n in rail for o in sorted(on_net[n], key=pos.get) if o in home and o != c), c)
    for r in refs:  # anything left (a part with no nets at all) is its own home
        home.setdefault(r, r) if r not in follows else None
    # Followers take their target's home, through chains.
    for r in refs:
        cur, seen = r, set()
        while cur in follows and cur not in seen:
            seen.add(cur)
            cur = follows[cur]
        if r in follows:
            home[r] = home.get(cur, cur)
    # Homes that are not anchors (a unit or a bank with no anchor to join): anchors of their own.
    roots = list(dict.fromkeys(home[r] for r in refs))
    anchors = [a for a in sorted(set(anchors) | {h for h in roots}, key=pos.get)]
    adj: dict[str, set[str]] = defaultdict(set)
    for net, rs in on_net.items():
        if not sig(net):
            continue
        hs = {home[r] for r in rs if r in home}
        for a in hs:
            adj[a] |= hs - {a}

    def comps(nodes):
        out, done = [], set()
        for a in nodes:
            if a in done:
                continue
            comp, q = [], [a]
            done.add(a)
            while q:
                c = q.pop()
                comp.append(c)
                for o in sorted(adj[c], key=pos.get):
                    if o in nodes and o not in done:
                        done.add(o)
                        q.append(o)
            out.append(sorted(comp, key=pos.get))
        return out

    groups: list[tuple[list[str], bool]] = []
    work = comps(list(anchors))
    while work:
        c = work.pop(0)
        if len(c) <= 2:
            groups.append((c, False))
            continue
        cut = max(c, key=lambda a: (len(adj[a] & set(c)), len(by[a].pins), -pos[a]))
        groups.append(([cut], True))
        work = comps([a for a in c if a != cut]) + work
    out: list[Region] = []
    for anchors_r, hub in groups:
        members = sorted((r for r in refs if home.get(r) in anchors_r), key=pos.get)
        if not members:
            continue
        lead = _lead(anchors_r, by)
        title = ", ".join(f"{a} {by[a].display}".strip() for a in anchors_r)
        reg = Region(f"{AUTO}{lead}", title, members, [lead] + [a for a in anchors_r if a != lead], hub=hub)
        reg.regulator = not hub and any(not _is_conn(a) and len(power_rails(a)) >= 2 for a in anchors_r)
        out.append(reg)
    return out


def _lead(anchors: list[str], by: dict) -> str:
    """The anchor a region is built from: a connector (the region's entry), else the one with most pins."""
    conns = [a for a in anchors if _is_conn(a)]
    if conns:
        return conns[0]
    return max(anchors, key=lambda a: (len(by[a].pins), -anchors.index(a)))


# --------------------------------------------------------------------------- §2.3 the specs inside a region


def region_specs(design, region: Region, by: dict, kinds: dict[str, str], auto_of: dict | None = None) -> dict[str, SchPlaceSpec]:
    """Every member's spec: the core line when there is one, else one the auto-layout writes in the same form. A posed
    member (`at=`) takes the place its auto spec (`auto_of`) would have in the order the others are written in: hung
    off the same part, first at the same pin, so the others are written as on the sheet its pose was copied from."""
    auto_of = auto_of or {}
    core = {p.ref: p for p in design.sch_places}
    out: dict[str, SchPlaceSpec] = {}
    members = list(region.members)
    mset = set(members)
    lead = region.anchors[0] if region.anchors else members[0]
    for m in members:
        if m in core:
            out[m] = core[m]
    if lead not in out:
        out[lead] = SchPlaceSpec(ref=lead, source="auto")
    # A core line that hangs a part off another member counts as placed only once that member is: until then nothing
    # the auto-layout writes may hang off it, or the two would wait on each other (buck's J_OUT locked to C_OUT2.1).
    def _target(m):
        s = out[m]
        if s.anchor_kind() == "at" and m in auto_of:
            s = auto_of[m]
        return (s.to or s.along or s.below or "").split(".", 1)[0] if s.anchor_kind() in ("to", "along", "below") else None

    pending = [m for m in members if m in out and _target(m) in mset]
    placed = [m for m in members if m in out and m not in pending]

    def promote() -> None:
        grew = True
        while grew:
            grew = False
            for m in list(pending):
                if _target(m) in placed:
                    pending.remove(m)
                    placed.append(m)
                    grew = True

    def sig(n):
        return _sig(n, kinds)

    def auto(ref, **kw) -> None:
        out[ref] = SchPlaceSpec(ref=ref, source="auto", **kw)
        placed.append(ref)
        promote()

    def pin_on(ref: str, net: str):
        return next((pn for pn in by[ref].pins if pn.net == net), None)

    def label(pn) -> str:
        """A pin as `to=` names it: its name when no other pin of the part has it, else its number."""
        return pn.name

    def pname(ref: str, pn) -> str:
        names = [p.name for p in by[ref].pins]
        return pn.name if names.count(pn.name) == 1 else pn.number

    # Hangers at a node: the first part attached at each (part, pin) target, so a second one stacks along it.
    first_at: dict[tuple[str, str], str] = {}
    for m, s in list(out.items()):
        s = auto_of.get(m, s) if s.anchor_kind() == "at" else s
        if s.to:
            first_at.setdefault(tuple(s.to.split(".", 1)), m)

    # Second anchors (and, in a declared region, every other member an anchor would be): a connector below the
    # connector before it, an IC hung off its shared signal pin.
    others = [a for a in region.anchors[1:] if a in mset]
    others += [m for m in members if m not in others and m != lead and (len(by[m].pins) > 2 or (_is_conn(m) and any(sig(pn.net) for pn in by[m].pins)))]
    last_conn = lead if _is_conn(lead) else None
    for a in others:
        if a in out:
            if _is_conn(a):
                last_conn = a
            continue
        if _is_conn(a) and last_conn is not None:
            auto(a, below=last_conn)
            last_conn = a
            continue
        shared = next((pn for pn in by[lead].pins if sig(pn.net) and pin_on(a, pn.net)), None)
        if shared is not None:
            auto(a, to=f"{lead}.{pname(lead, shared)}", side="bottom")
        else:
            auto(a, below=placed[-1])
        if _is_conn(a):
            last_conn = a
    # Units: breadth first from what is placed, over signal nets. Off the anchor, the pin highest on its side goes
    # first, so hangers nest outward down a side (the staircase a person draws: BOOT, then EN, then FB).
    while True:
        cands = []
        for mi, m in enumerate(members):
            if m in out:
                continue
            for net in [pn.net for pn in by[m].pins if sig(pn.net)]:
                for qi, q in enumerate(placed):
                    qp = pin_on(q, net)
                    if qp is not None:
                        # At one node: a pull-up first (it stands up from the row), then a part to ground (it hangs
                        # down), then the rest — the spots a person gives them, taken before a third part needs one.
                        far = [kinds.get(pn.net, "net") for pn in by[m].pins if pn.net and pn.net != net]
                        rank = 0 if "power" in far else 1 if "ground" in far else 2
                        # Of two parts that would take the same spot, the shorter: the in-line spot under a node reaches
                        # down the rows below it, and a long part there cuts the next pin's wire (node's SW_RST under the EN
                        # node crossed IO10's run to R_LED; the hand sheet hangs C_EN there and SW_RST beside it).
                        cands.append(((q != lead, qi, -qp.ly if q == lead else 0.0, qp.lx if q == lead else 0.0, rank, _span(by[m]), mi), m, q, qp))
        if not cands:
            break
        _k, m, q, qp = min(cands, key=lambda c: c[0])
        key = (q, pname(q, qp))
        if key in first_at:
            prev = first_at[key]
            pp = pin_on(prev, qp.net)
            auto(m, along=f"{prev}.{pname(prev, pp)}", side=BESIDE)
        else:
            auto(m, to=f"{q}.{pname(q, qp)}")
        first_at[key] = m
    # Banks: the first part on a rail to the anchor's pin on it (else the first placed part on it), the next along the
    # one before, so the bank is one row. Then the power-only connectors, each at the end of its rail's row, on the side
    # the row runs out to.
    last_on: dict[str, str] = {}
    root_side: dict[str, str] = {}
    cap_row: list = [None]
    rest = [m for m in members if m not in out]
    promote()
    # A region of two-pin parts only (d14's three resistors across one supply) has no IC to hang a bank off: the bank
    # stands as a row beside its anchor.
    passive_only = all(len(by[m].pins) == 2 for m in members)
    for m in [m for m in rest if not _is_conn(m)] + [m for m in rest if _is_conn(m)]:
        rails = [pn.net for pn in by[m].pins if pn.net and kinds.get(pn.net) == "power"] or [pn.net for pn in by[m].pins if pn.net]
        target = None
        for net in rails:
            if net in last_on:
                prev = last_on[net]
                pp = pin_on(prev, net)
                if _is_conn(m):
                    target = dict(to=f"{prev}.{pname(prev, pp)}", side=root_side.get(net) or _root_side(out, by, lead, prev))
                else:
                    target = dict(along=f"{prev}.{pname(prev, pp)}", side=BESIDE)
                break
            lp = pin_on(lead, net)
            if lp is not None and not _is_conn(m) and _dense(by[lead], lp):
                # A busy side (six bound pins or more): its decoupling is drawn as a row under the IC with its own
                # power symbols, not hung among the pin rows it would crowd (DS2's ADC: DVDD and AVDD between RX/TX/DRDY
                # and the analog inputs).
                target = dict(below=lead) if cap_row[0] is None else dict(along=f"{cap_row[0]}.{pname(cap_row[0], by[cap_row[0]].pins[0])}", side="right")
                cap_row[0] = m
                root_side[net] = "left" if lp.lx < 0 else "right"
                break
            if lp is not None and passive_only:
                target = dict(along=f"{lead}.{pname(lead, lp)}", side=BESIDE)  # a row beside a two-pin anchor
                root_side[net] = "right"
                break
            if lp is not None:
                target = dict(to=f"{lead}.{pname(lead, lp)}")
                root_side[net] = "left" if lp.lx < 0 else "right"
                # The lowest (highest) bound pin of its side: the bank parks under (over) the body, out of the rows of
                # the pins beside it (buck's input stage under U1, as drawn by hand).
                same = [pn.ly for pn in by[lead].pins if pn.net and (pn.lx < 0) == (lp.lx < 0) and abs(pn.lx) > 1e-6]
                if same and abs(lp.lx) > 1e-6 and len(same) > 1 and lp.ly <= min(same) + 1e-6:
                    target["side"] = "bottom"
                break
            q = next((q for q in placed if pin_on(q, net) is not None), None)
            if q is not None:
                target = dict(to=f"{q}.{pname(q, pin_on(q, net))}")
                root_side[net] = _root_side(out, by, lead, q)
                break
        if target is None:
            auto(m, below=placed[-1])
        else:
            auto(m, **target)
        for net in rails:
            last_on[net] = m
    placed.extend(pending)  # hung off a part outside this region, or never rooted: the placer names that
    _ = mset, label
    return out


def _span(part) -> float:
    """How far a two-pin part reaches from pin to pin (a box symbol's span is not compared: 0)."""
    if len(part.pins) != 2:
        return 0.0
    a, b = part.pins
    return round(abs(a.lx - b.lx) + abs(a.ly - b.ly), 4)


def _dense(part, pin) -> bool:
    """Six or more bound pins on the side of `part` that `pin` is on."""
    side = [pn for pn in part.pins if pn.net and (pn.lx < 0) == (pin.lx < 0) and abs(pn.lx) > 1e-6]
    return abs(pin.lx) > 1e-6 and len(side) >= 6


def _root_side(specs: dict, by: dict, lead: str, ref: str) -> str:
    """The side of the region's anchor a part's attach chain leaves it from: the side its row runs out to."""
    seen = set()
    cur = ref
    while cur in specs and cur not in seen and cur != lead:
        seen.add(cur)
        s = specs[cur]
        target = s.to or s.along
        if not target:
            break
        tref, tpin = target.split(".", 1)
        if tref == lead:
            pn = next((p for p in by[lead].pins if p.name == tpin or p.number == tpin), None)
            return "left" if pn is not None and pn.lx < 0 else "right"
        cur = tref
    return "left"


# --------------------------------------------------------------------------- placement

# A side the auto-layout decides when the part before it is placed: under it when that part hangs from a row (the
# `along` solver then turns it to stand beside), beside it — away from the region's anchor — when that part is
# stacked under its own target (a second hanger there would otherwise stand in line, cut off by the first's body).
BESIDE = "beside"


def _beside(s: SchPlaceSpec, by: dict, lead: str) -> SchPlaceSpec:
    import dataclasses

    if s.side != BESIDE:
        return s
    prev = by[(s.along or s.to).split(".", 1)[0]]
    d = getattr(prev, "attach_dir", None)
    if d and abs(d[1]) > 0.5:
        side = "left" if prev.x <= by[lead].x else "right"
    else:
        side = "bottom"
    return dataclasses.replace(s, side=side)


def _structure(design, parts: list, kinds: dict, by: dict):
    """The layout's structure before anything is placed: the regions on their grid and every part's spec."""
    regions = infer_regions(design, parts, kinds)
    start = _grid(regions, by, kinds)  # rows, columns and sides come from the netlist alone
    specs: dict[str, SchPlaceSpec] = {}
    region_of: dict[str, Region] = {}
    # For each posed part, the spec the auto-layout would write for it with no pose line: what the other members' specs
    # are written around (`region_specs`) and the first attach `_explain` tries for it.
    posed = {p.ref for p in design.sch_places if p.anchor_kind() == "at"}
    auto_of: dict[str, SchPlaceSpec] = {}
    if posed:
        import types

        proxy = types.SimpleNamespace(sch_places=[p for p in design.sch_places if p.anchor_kind() != "at"], paths=getattr(design, "paths", []))
        for reg in regions:
            if posed & set(reg.members):
                rs = region_specs(proxy, reg, by, kinds)
                auto_of.update({m: rs[m] for m in reg.members if m in posed and m in rs})
    for reg in regions:
        rs = region_specs(design, reg, by, kinds, auto_of)
        specs.update(rs)
        for m in reg.members:
            region_of[m] = reg
    _orient_connectors(regions, by, specs, kinds)
    return regions, start, specs, region_of, auto_of


def _pin_no(by: dict, tok: str | None):
    """`REF.PIN` as (ref, pin number): a pin named by its name or its number is the same pin."""
    if not tok:
        return None
    ref, _, name = tok.partition(".")
    part = by.get(ref)
    if part is None:
        return (ref, name)
    named = [p for p in part.pins if p.name == name]
    pin = named[0] if len(named) == 1 else next((p for p in part.pins if p.number == name), None)
    return (ref, pin.number if pin is not None else name)


def same_attach(line: SchPlaceSpec, auto: SchPlaceSpec | None, by: dict) -> bool:
    """A core attach line that states what the auto-layout writes for its part: the same anchor (`to=`, `along=`,
    `below=` the same pin), `pin=`, `align=`, `gap=`, rotation and mirror, and the same side, or a side where the
    auto-layout leaves it to the placer (`beside`, which the placer turns into left, right or bottom)."""
    if auto is None or auto.anchor_kind() != line.anchor_kind() or line.anchor_kind() not in ("to", "along", "below"):
        return False
    own = by.get(line.ref)

    def own_pin(tok):
        return _pin_no(by, f"{line.ref}.{tok}")[1] if tok and own is not None else tok

    return (
        _pin_no(by, line.to) == _pin_no(by, auto.to)
        and _pin_no(by, line.along) == _pin_no(by, auto.along)
        and line.below == auto.below
        and own_pin(line.pin) == own_pin(auto.pin)
        and _pin_no(by, line.align) == _pin_no(by, auto.align)
        and line.gap == auto.gap
        and (line.side == auto.side or (auto.side == BESIDE and line.side in ("left", "right", "bottom")))
        and (line.rotate_set, line.rot if line.rotate_set else 0.0) == (auto.rotate_set, auto.rot if auto.rotate_set else 0.0)
        and (line.mirror or None) == (auto.mirror or None)
    )


def met_attaches(design, parts: list, kinds: dict, by: dict):
    """The core attach lines (`to=`, `along=`, `below=`) that state what the auto-layout writes for their part with them
    left out, and the design without them. Such a line changes nothing (docs/schematic-layout.md §3.3, §4): the part is
    placed by the auto-layout's own spec, in its turn (refute:lock 2026-10-09: gen's `# auto:` comment copied into core
    was placed before every auto spec and moved other parts in 76 of 147 cases). Found by elimination: the lines left
    out together, a line that differs put back, until every line left out matches. A line whose side is one the
    placer gives a `beside` spec is met only if the placer gives it that side on the sheet that is kept: `sch_emit._settle`
    checks, and names it in `design._sch_forced` to be laid out as the lock it then is."""
    import copy

    forced = set(getattr(design, "_sch_forced", ()) or ())
    lines = {s.ref: s for s in design.sch_places if s.anchor_kind() in ("to", "along", "below") and s.ref not in forced}
    met = set(lines)
    while met:
        view = copy.copy(design)
        view.sch_places = [s for s in design.sch_places if s.ref not in met]
        specs = _structure(view, parts, kinds, by)[2]
        keep = {m for m in met if same_attach(lines[m], specs.get(m), by)}
        if keep == met:
            return view, {m: lines[m] for m in sorted(met)}
        met = keep
    return design, {}


def resolve(design, parts: list, gap_extra: dict | None = None, frame_hints: dict | None = None, honor_at: bool = True) -> Layout:
    """Set every part's pose (x, y, rot, mirror), its region (`sch_group`, `sch_title`), and return the layout."""
    import dataclasses

    from .sch_place import _separate

    kinds = {n.name: n.kind for n in design.nets.values()}
    by = {p.ref: p for p in parts}
    design, met = met_attaches(design, parts, kinds, by)
    degree: dict[str, int] = {}
    for p in parts:
        for pn in p.pins:
            if pn.net:
                degree[pn.net] = degree.get(pn.net, 0) + 1
    regions, start, specs, region_of, auto_of = _structure(design, parts, kinds, by)
    for reg in regions:
        hint = (frame_hints or {}).get(reg.name)
        # A region with pose locks is laid out as any other: its anchor at the first member slot (§3.2), the frame every
        # pose is written in, and each locked part put at its pose when its turn in the placement order comes, so the
        # parts placed after it go round it as they went round it on the sheet the pose was copied from.
        _place_region(reg, [by[m] for m in reg.members], specs, by, kinds, degree, gap_extra or {}, auto_of=auto_of)
        fixed = {m for m in reg.members if specs[m].anchor_kind() in ("at", "anchor") or m == (reg.anchors[0] if reg.anchors else None)}
        _separate([by[m] for m in reg.members], fixed, kinds)
        _pull([by[m] for m in reg.members], specs, kinds)
        _normalize(reg, [by[m] for m in reg.members], specs, kinds, hint)
    _pack(regions, start, honor_at)
    regions.sort(key=lambda r: (r.origin[1], r.origin[0]))  # emission order: the grid's, top to bottom, left to right
    for reg in regions:
        ox, oy = reg.origin
        for m in reg.members:
            p = by[m]
            # Quantized to the file's 4 decimals: a pose copied from schematic.gen.py is then the same float, and the
            # wiring, whose ties are broken by distance, is the same wiring (the fixed point, §4).
            p.x = round(p.x + ox, 4)
            p.y = round(p.y + oy, 4)
            p.sch_group = reg.name
            p.sch_title = reg.title
            if getattr(p, "attach_wire", None):
                (ax, ay), (bx, by_) = p.attach_wire
                p.attach_wire = ((ax + ox, ay + oy), (bx + ox, by_ + oy))
    _refuse_pose_overlaps(regions, by, specs)
    return Layout(regions, region_of, specs, met)


def _orient_connectors(regions: list[Region], by: dict, specs: dict, kinds: dict) -> None:
    """Connectors face inward (§2.4): a connector in a region left of its hub whose signal pins are drawn on its left
    is mirrored "y" so they point right, and the same the other way. Only where no line says the pose (`mirror=`,
    `rotate=`), and only for the forms that do not pose a part by its pin (an anchor, `below=`)."""
    import dataclasses

    for reg in regions:
        if reg.side not in ("left", "right"):
            continue
        for m in reg.members:
            s = specs[m]
            if not _is_conn(m) or s.mirror_set or s.rotate_set or s.anchor_kind() not in ("anchor", "below"):
                continue
            xs = [pn.lx for pn in by[m].pins if _sig(pn.net, kinds)]
            if not xs:
                continue
            left = sum(1 for x in xs if x < 0)
            right = len(xs) - left
            away = left > right if reg.side == "left" else right > left
            if away:
                specs[m] = dataclasses.replace(s, mirror="y")


def _explain(m: str, specs: dict, by: dict, placed: list[str], kinds: dict, degree: dict, auto_of: dict) -> None:
    """A posed part as the attach that would put it on its pose: the auto-layout's own spec for it, else a `to=` from any
    of its pins to a pin of a placed part on the same net, tried on copies. The first whose pose is the lock's, or the
    lock's moved only along that attach's axis (the gap a person wrote, `gap=15.24`), gives the part that attach's
    record (`attach_ref`, `attach_dir`, `attach_wire`, the wired pins): the parts placed after it, `_separate` and the
    `along=` solver then see it as they saw it on the sheet the pose was copied from (refute:split 2026-10-08: node's
    C_MCU pose copied over `to="U1.3V3"` hung C_MCU_HF on the other side and moved six parts)."""
    import copy

    from .sch_place import _apply_attach, parse_refpin, pin_world

    part = by[m]
    cands = []
    h = auto_of.get(m)
    if h is not None and h.anchor_kind() in ("to", "along"):
        cands.append(h)
    for q in placed:
        if q == m:
            continue
        for op in by[q].pins:
            if op.net and any(pn.net == op.net for pn in part.pins):
                cands.append(SchPlaceSpec(ref=m, to=f"{q}.{op.number}", source="auto"))
    for spec in cands:
        tref, tpin = parse_refpin(spec.to or spec.along)
        if tref not in placed or tref not in by:
            continue
        trial = copy.copy(part)
        target = copy.copy(by[tref])
        for x in (trial, target):
            if getattr(x, "wired_pins", None) is not None:
                x.wired_pins = set(x.wired_pins)
        try:
            _apply_attach(spec, trial, target, tpin, [by[r] for r in placed if r != tref] + [target], kinds, degree, {})
        except ValueError:
            continue
        if getattr(trial, "attach_dir", None) is None or int(round(trial.rot)) % 360 != int(round(part.rot)) % 360 or (trial.mirror or None) != (part.mirror or None):
            continue
        dx, dy = part.x - trial.x, part.y - trial.y
        sx, sy = trial.attach_dir
        if abs(dx * sy - dy * sx) > 1e-3 or dx * sx + dy * sy < -1e-3:
            continue
        (ax, ay), (bx, by_) = trial.attach_wire if getattr(trial, "attach_wire", None) else (pin_world(target, target.pins[0]), (trial.x, trial.y))
        part.attach_ref, part.attach_dir = trial.attach_ref, trial.attach_dir
        if getattr(trial, "attach_wire", None):
            part.attach_wire = ((ax, ay), (bx + dx, by_ + dy))
        part.wired_pins = set(getattr(trial, "wired_pins", set()) or set())
        new = set(getattr(target, "wired_pins", set()) or set()) - set(getattr(by[tref], "wired_pins", set()) or set())
        if new:
            by[tref].wired_pins = set(getattr(by[tref], "wired_pins", set()) or set()) | new
        return


def _place_region(reg: Region, parts: list, specs: dict, by: dict, kinds: dict, degree: dict, gap_extra: dict, lead_at=None, auto_of: dict | None = None) -> None:
    from .sch_place import _apply_attach, _apply_below, _apply_origin, parse_refpin, world_aabb

    for p in parts:
        p.placed = False
        for attr in ("attach_ref", "attach_dir", "attach_wire", "stuck_on", "stuck_wire", "shoved_mm", "shoved_by", "wired_pins"):
            if attr in p.__dict__:
                delattr(p, attr)
    lead = reg.anchors[0] if reg.anchors else reg.members[0]
    placed: list[str] = []
    order: list[str] = []

    def pose(m: str) -> None:
        s, p = specs[m], by[m]
        p.rot = float(s.rot) if s.rotate_set else 0.0
        p.mirror = s.mirror
        p.x, p.y = s.at
        p.placed = True

    if specs[lead].anchor_kind() == "at":
        pose(lead)
        placed.append(lead)
        order.append(lead)
    if lead not in placed and lead_at is not None:
        p = by[lead]
        p.x, p.y, p.rot, p.mirror = lead_at
        p.placed = True
        placed.append(lead)
        order.append(lead)
    if lead not in placed:
        s, p = specs[lead], by[lead]
        if s.anchor_kind() in ("anchor",) or s.source == "auto":
            spec = s if s.anchor_kind() == "anchor" else SchPlaceSpec(ref=lead, source="auto")
            _apply_origin(spec, p)
            x0, y0, _x1, _y1 = world_aabb(p)
            p.x = _snap(p.x + PAD - x0)
            p.y = _snap(p.y + HEADER + PAD - y0)
            placed.append(lead)
            order.append(lead)
    def rigid(m):
        return 0 if by[m].kind not in ("r", "c", "d") else 1

    gen_order = {m: i for i, m in enumerate(specs_order(specs, reg.members))}

    def attach_all(pending: list[str], placed: list[str]) -> None:
        guard = 0
        while pending:
            guard += 1
            if guard > 256:
                raise ValueError("schematic: placement does not converge for " + ", ".join(pending))
            progressed = False
            # A core line is placed before anything the auto-layout chose: the lock gets its spot, the rest go round it.
            for m in sorted(pending, key=lambda r: (specs[r].source != "core", rigid(r), gen_order[r])):
                s = specs[m]
                kind = s.anchor_kind()
                if kind == "at":
                    pose(m)
                    _explain(m, specs, by, placed, kinds, degree, auto_of or {})
                    placed.append(m)
                    order.append(m)
                    pending.remove(m)
                    progressed = True
                    break
                if kind == "anchor":
                    s = SchPlaceSpec(ref=m, below=placed[-1], rot=s.rot, rotate_set=s.rotate_set, mirror=s.mirror, source=s.source, line=s.line)
                    kind = "below"
                if kind == "below":
                    if s.below not in placed:
                        continue
                    if any(r != m and r not in placed and _root(specs, r) == s.below for r in reg.members):
                        continue
                    _apply_below(s, by[m], by[s.below], [by[r] for r in placed], kinds)
                else:
                    tref, tpin = parse_refpin(s.to or s.along)
                    aref = parse_refpin(s.align)[0] if s.align else None
                    if tref not in placed or (aref and aref not in placed and aref in by):
                        continue
                    if s.side == BESIDE:
                        s = specs[m] = _beside(s, by, lead)
                    try:
                        _apply_attach(s, by[m], by[tref], tpin, [by[r] for r in placed], kinds, degree, gap_extra)
                    except ValueError as exc:
                        where = f"{SCH_CORE_FILE}:{s.line}: " if s.line else ""
                        raise ValueError(f"{where}{exc}") from None
                placed.append(m)
                order.append(m)
                pending.remove(m)
                progressed = True
                break
            if not progressed:
                raise ValueError("schematic: SchPlace waits on a part that is never placed: " + ", ".join(
                    f"{m} on {specs[m].to or specs[m].along or specs[m].below}" for m in pending))

    # A posed part is no cluster's root: it is placed with the anchor's cluster, at its pose.
    roots = [m for m in reg.members if specs[m].anchor_kind() not in ("to", "along", "at")]
    if lead not in placed or len(roots) <= 1:
        attach_all([m for m in reg.members if m not in placed], placed)
        reg.order = order
        return
    # Clusters (§2.3): each part not hung off another (the anchor, a connector stacked below=) with everything hung
    # off it, solved on its own, then stacked under what is placed, its root's left edge on its target's. A second
    # connector's filters then clear the first's whole cluster, not the first connector's bare body.
    from .sch_place import keepout_boxes

    cluster = {m: _root(specs, m) for m in reg.members}
    cluster.update({m: lead for m in reg.members if specs[m].anchor_kind() == "at"})
    others = sorted((r for r in roots if r != lead), key=lambda r: gen_order[r])
    attach_all([m for m in reg.members if cluster[m] == lead and m not in placed], placed)

    def box(refs):
        bs = [b for r in refs for b in [world_aabb(by[r]), *keepout_boxes(by[r], kinds)]]
        return min(b[0] for b in bs), min(b[1] for b in bs), max(b[2] for b in bs), max(b[3] for b in bs)

    # Clusters stack down a column, at most `CLUSTERS_PER_COLUMN` to a column; the next opens a column to the right, its
    # top level with the region's first cluster (DS2's three inputs: one column was 1.6 times the hand sheet's area).
    columns: list[list[str]] = [list(placed)]
    roots_in: list[list[str]] = [[lead]]
    for root in others:
        s = specs[root]
        spec = SchPlaceSpec(ref=root, rot=s.rot, rotate_set=s.rotate_set, mirror=s.mirror, source=s.source)
        _apply_origin(spec, by[root])
        mine = [root]
        order.append(root)
        attach_all([m for m in reg.members if cluster[m] == root and m != root], mine)
        cx0, cy0, _cx1, _cy1 = box(mine)
        if len(roots_in[-1]) >= CLUSTERS_PER_COLUMN:
            target = roots_in[-1][0]
            dx = _snap(box(placed)[2] + 2 * 5.08 - cx0)
            dy = _snap(box(columns[0])[1] - cy0)
            columns.append([])
            roots_in.append([])
        else:
            target = s.below if s.below in columns[-1] else roots_in[-1][0]
            bx0, by0, bx1, by1 = box(columns[-1])
            dx = _snap(world_aabb(by[target])[0] - world_aabb(by[root])[0])
            dy = _snap(by1 + 5.08 - cy0)
        columns[-1].extend(mine)
        roots_in[-1].append(root)
        for r in mine:
            p = by[r]
            p.x += dx
            p.y += dy
            if getattr(p, "attach_wire", None):
                (ax, ay), (bx, by_) = p.attach_wire
                p.attach_wire = ((ax + dx, ay + dy), (bx + dx, by_ + dy))
        by[root].attach_ref = target
        by[root].attach_dir = (0.0, 1.0)
        placed.extend(mine)
    reg.order = order


def _root(specs: dict, ref: str) -> str:
    """The part an attach chain (`to=`/`along=`) ends at."""
    seen = set()
    cur = ref
    while cur not in seen:
        seen.add(cur)
        s = specs.get(cur)
        t = (s.to or s.along) if s is not None else None
        if not t:
            return cur
        cur = t.split(".", 1)[0]
    return cur


def specs_order(specs: dict, members: list[str]) -> list[str]:
    """The members in the order their specs were written: core lines first (file order), then the auto-layout's."""
    core = sorted((m for m in members if specs[m].source == "core"), key=lambda m: specs[m].line)
    auto = [m for m in specs if m in set(members) and specs[m].source != "core"]
    return core + [m for m in auto if m not in core]


def _pull(parts: list, specs: dict, kinds: dict) -> None:
    """§2.5's pull: each attached hanger slides back toward its target, one grid step at a time, while it keeps its asked
    gap, its keepout clear of every other part's, and 2.54 mm between bodies."""
    from .sch_place import _clash, body_aabb

    def gap(a, b):
        ax0, ay0, ax1, ay1 = a
        bx0, by0, bx1, by1 = b
        dx = max(bx0 - ax1, ax0 - bx1, 0.0)
        dy = max(by0 - ay1, ay0 - by1, 0.0)
        return math.hypot(dx, dy)

    for p in parts:
        s = specs.get(p.ref)
        if s is None or s.anchor_kind() in ("at", "anchor") or not getattr(p, "shoved_mm", 0.0):
            continue
        d = getattr(p, "attach_dir", None)
        if not d:
            continue
        dx, dy = d
        for _ in range(int(round(p.shoved_mm / _GRID))):
            x0, y0 = p.x, p.y
            p.x -= dx * _GRID
            p.y -= dy * _GRID
            ok = all(not _clash(p, o, kinds) for o in parts if o is not p) and all(
                gap(body_aabb(p), body_aabb(o)) >= 2.54 - 1e-3 for o in parts if o is not p and o.ref != getattr(p, "attach_ref", None)
            )
            if not ok:
                p.x, p.y = x0, y0
                break
            p.shoved_mm = max(0.0, p.shoved_mm - _GRID)
            if getattr(p, "attach_wire", None):
                (ax, ay), (bx, by_) = p.attach_wire
                p.attach_wire = ((ax, ay), (bx - dx * _GRID, by_ - dy * _GRID))


def _normalize(reg: Region, parts: list, specs: dict, kinds: dict | None = None, hint=None) -> None:
    """The region's frame, relative to its origin (the anchor sits at the first member slot). The frame is an estimate of what will be drawn for the region (bodies, the power symbols and names around them)
    grown to what an earlier pass drew (`hint`, relative to the origin): what the grid packs."""
    from .sch_place import keepout_boxes, world_aabb

    if not parts:
        reg.frame = (0.0, 0.0)
        reg.rect = (0.0, 0.0, 0.0, 0.0)
        return
    # The members are not moved: the anchor's body stays at the first member slot (§3.2), so a pose is written, and read
    # back, in a frame the anchor fixes (moving the members' box to the slot made the frame depend on every member: a part
    # locked at its gen pose that set the box's corner moved the anchor under it, refute:split 2026-10-08: node's C_BME
    # 0 -> 18 hard findings). The frame is measured as before, with the members' box at the slot (`norm`, the shift that
    # would put it there; `hint` is in that frame too), and stated relative to the origin: the packed sheet is the same.
    boxes = [world_aabb(p) for p in parts]
    dx = _snap(PAD - min(b[0] for b in boxes))
    dy = _snap(HEADER + PAD - min(b[1] for b in boxes))
    reg.norm = (dx, dy)
    boxes = boxes + [b for p in parts for b in keepout_boxes(p, kinds)]
    rect = [min(b[0] for b in boxes) + dx - PAD, min(b[1] for b in boxes) + dy - PAD - HEADER, max(b[2] for b in boxes) + dx + PAD, max(b[3] for b in boxes) + dy + PAD]
    rect[0], rect[1] = min(rect[0], 0.0), min(rect[1], 0.0)
    if hint:
        rect = [min(rect[0], hint[0]), min(rect[1], hint[1]), max(rect[2], hint[2]), max(rect[3], hint[3])]
    reg.rect = (rect[0] - dx, rect[1] - dy, rect[2] - dx, rect[3] - dy)
    reg.frame = (rect[2] - rect[0], rect[3] - rect[1])


# --------------------------------------------------------------------------- §2.4 order on the sheet


def _grid(regions: list[Region], by: dict, kinds: dict) -> int:
    """Rows, columns and sides (§2.4): regulators that are not hubs in row 0, hubs in the middle of row 1 (most connected
    in the middle), every other region beside the hub it shares most signal nets with, on the side of the hub pins it
    reaches (a tie goes left), ordered down its column by the mean height of those pins. A declared `row=`/`col=` is
    taken as written; a declared region left of the centre column faces right, and the other way."""
    if not regions:
        return 0
    sig_of = {r.name: {pn.net for m in r.members for pn in by[m].pins if _sig(pn.net, kinds)} for r in regions}
    hubs = [r for r in regions if r.hub]
    if not hubs:
        # No inferred hub (a sheet of declared regions, or one region): the region that shares signal nets with most
        # others is the centre, by which a declared column is left or right.
        def reach(r):
            return (sum(1 for o in regions if o is not r and sig_of[r.name] & sig_of[o.name]), max((len(by[m].pins) for m in r.members), default=0))

        centre = max(regions, key=lambda r: (reach(r), -regions.index(r)))
        hubs_auto = [centre] if reach(centre)[0] else []
    else:
        hubs_auto = hubs

    def links(h):
        return sum(len(sig_of[h.name] & sig_of[o.name]) for o in hubs_auto if o is not h)

    ranked = sorted(hubs_auto, key=lambda h: (-links(h), regions.index(h)))
    line: list[Region] = []
    for i, h in enumerate(ranked):
        line = line + [h] if i % 2 else [h] + line
    for reg in regions:
        if reg in line:
            reg.row = 1
            continue
        if reg.regulator:
            reg.row = 0
            continue
        best = None
        for h in line:
            shared = sig_of[reg.name] & sig_of[h.name]
            if not shared:
                continue
            hub_part = by[h.anchors[0]] if h.anchors else by[h.members[0]]
            pins = [(-pn.ly, pn.lx) for pn in hub_part.pins if pn.net in shared]
            if pins and (best is None or len(shared) > best[0]):
                best = (len(shared), h, pins)
        reg.row = 1
        if best is None:
            continue
        _n, h, pins = best
        left = sum(1 for _y, x in pins if x < 0)
        right = len(pins) - left
        reg.side = "right" if right > left else "left"
        reg.stack = sum(y for y, _x in pins) / len(pins)
        reg.note = f"beside {h.name}, {reg.side} ({left} of its pins left, {right} right)"
    has_left = any(r.side == "left" for r in regions if r not in line)
    start = 1 if has_left else 0
    for i, h in enumerate(line):
        h.col = start + i
        h.note = h.note or ("hub" if h.hub else "centre")
    for reg in regions:
        if reg in line:
            continue
        if reg.side == "left":
            reg.col = start - 1
        elif reg.side == "right":
            reg.col = start + len(line)
    for reg in regions:
        d = reg.declared
        if d is None:
            continue
        if d.row is not None:
            reg.row = d.row
        if d.col is not None:
            reg.col = d.col
            reg.side = None
            cols = [h.col for h in line if h is not reg and h.declared is not None and h.declared.col is not None] or [h.col for h in line if h is not reg]
            if cols and reg not in line:
                reg.side = "left" if d.col < min(cols) else "right" if d.col > max(cols) else None
        if d.row is not None or d.col is not None:
            reg.note = f"grid row {reg.row}, col {reg.col} (locked)"
    for reg in regions:
        if reg.row is None:
            reg.row = 1
        if not reg.note.startswith("grid"):
            reg.note = f"grid row {reg.row}, col {reg.col if reg.col is not None else 'next'}" + (f"; {reg.note}" if reg.note else "")
    return start


def _pack(regions: list[Region], start: int = 0, honor_at: bool = True) -> None:
    """Origins: a region with `at=` keeps it; every region is packed on the grid, `GUTTER` apart, as if none were locked
    (a gen region line copied into core then moves nothing, docs/schematic-layout.md §4), and a packed frame a locked one
    lands on moves down past it."""
    # The layout loop's passes pack every region on the grid (`honor_at=False`); the sheet it keeps is drawn once more
    # with the locked origins when one differs from where the grid put it (`sch_emit._emit`), so an origin copied from
    # gen, which is where the grid put it, changes nothing.
    fixed = [r for r in regions if r.declared is not None and r.declared.at is not None] if honor_at else []
    # Every region keeps its grid cell, locked or not: the others pack as if no origin were locked, so an origin copied
    # from gen moves nothing, and one written elsewhere only pushes down what it lands on.
    _pack_grid(regions, regions, start)
    for r in fixed:
        r.origin = r.declared.at
        r.note = r.note or "at= (locked)"
    # A packed frame never sits on a locked one: it moves down past it, and past any packed frame that moving put it on
    # (a U1 pose 20 mm left widened node's mcu frame; the analog region moved down onto the load region).
    for _ in range(64):
        moved = False
        settled = list(fixed)
        for r in sorted((r for r in regions if r not in fixed), key=lambda r: (r.origin[1], r.origin[0])):
            for f in settled:
                if _frames_overlap(r, f):
                    r.origin = (r.origin[0], _snap(f.origin[1] + f.rect[3] + GUTTER - r.rect[1]))
                    moved = True
            settled.append(r)
        if not moved:
            break


def _pack_grid(regions: list[Region], free: list[Region], start: int = 0) -> None:
    """The grid origins of `free`. A side of a hub stacks its regions down a column no taller than the hub row's tallest
    frame; past that, the next region opens a column further out (node's four regions left of the ESP32 would otherwise
    be one column twice the hub's height)."""
    row1 = [r for r in free if (r.row if r.row is not None else 1) == 1]
    side_h = max([r.frame[1] for r in row1 if r.side is not None] + [0.0])
    limit = max([r.frame[1] for r in row1 if r.side is None] + [2 * side_h + GUTTER])
    # Auto columns for the regions beside a hub: nearest column first, opening further ones when a stack is full.
    for side in ("left", "right"):
        group = sorted((r for r in row1 if r.side == side and (r.declared is None or r.declared.col is None)), key=lambda r: (r.stack, regions.index(r)))
        if not group:
            continue
        base = group[0].col
        col, height = base, 0.0
        for r in group:
            if height and height + GUTTER + r.frame[1] > limit + 1e-6:
                col += -1 if side == "left" else 1
                height = 0.0
            r.col = col
            height += (GUTTER if height else 0.0) + r.frame[1]
    rows: dict[int, list[Region]] = defaultdict(list)
    for r in free:
        rows[r.row if r.row is not None else 1].append(r)
    taken_cols = [r.col for r in free if r.col is not None]
    ncol = 1 + max(taken_cols, default=-1)
    hub_col = min((r.col for r in row1 if r.side is None and r.col is not None), default=start)
    for row in sorted(rows):
        loose = [r for r in rows[row] if r.col is None]
        taken = {r.col for r in rows[row] if r.col is not None}
        c = hub_col if row == 0 else ncol
        for r in loose:
            while c in taken and row != 0:
                c += 1
            r.col = c
            taken.add(c)
            c += 1
    lo = min((r.col for r in free), default=0)
    for r in free:
        if r.declared is None or r.declared.col is None:
            r.col -= lo
    cols: dict[int, dict[int, list[Region]]] = defaultdict(lambda: defaultdict(list))
    for row, regs in rows.items():
        for r in regs:
            cols[row][r.col].append(r)
    all_cols = sorted({c for row in cols.values() for c in row})
    width = {c: max((r.frame[0] for row in cols.values() for r in row.get(c, [])), default=0.0) for c in all_cols}
    xs: dict[int, float] = {}
    x = PACK_ORIGIN
    for c in all_cols:
        xs[c] = x
        x += width[c] + GUTTER
    y = PACK_ORIGIN
    for row in sorted(cols):
        tallest = 0.0
        for c, regs in sorted(cols[row].items()):
            regs.sort(key=lambda r: (r.stack, regions.index(r)))
            cy = y
            for r in regs:
                # Left of a hub: pulled right against it; right of a hub or in the hub column: left-aligned.
                rx = xs[c] + (width[c] - r.frame[0] if r.side == "left" else 0.0)
                r.origin = (_snap(rx - r.rect[0]), _snap(cy - r.rect[1]))
                cy += r.frame[1] + GUTTER
            tallest = max(tallest, cy - GUTTER - y)
        y += tallest + GUTTER


def _frames_overlap(a: Region, b: Region) -> bool:
    ax0, ay0, ax1, ay1 = a.origin[0] + a.rect[0], a.origin[1] + a.rect[1], a.origin[0] + a.rect[2], a.origin[1] + a.rect[3]
    bx0, by0, bx1, by1 = b.origin[0] + b.rect[0], b.origin[1] + b.rect[1], b.origin[0] + b.rect[2], b.origin[1] + b.rect[3]
    return ax0 < bx1 and bx0 < ax1 and ay0 < by1 and by0 < ay1


def _refuse_pose_overlaps(regions: list[Region], by: dict, specs: dict) -> None:
    """Two pose-locked symbols of one region whose bodies overlap: refused naming both lines and the overlap (§5)."""
    from .sch_place import body_aabb

    for reg in regions:
        locked = [m for m in reg.members if specs[m].anchor_kind() == "at"]
        for i, a in enumerate(locked):
            for b in locked[i + 1 :]:
                ba, bb = body_aabb(by[a]), body_aabb(by[b])
                dx = min(ba[2], bb[2]) - max(ba[0], bb[0])
                dy = min(ba[3], bb[3]) - max(ba[1], bb[1])
                if dx > 1e-6 and dy > 1e-6:
                    raise ValueError(
                        f"{SCH_CORE_FILE}:{specs[a].line} and {SCH_CORE_FILE}:{specs[b].line}: SchPlace({a!r}) and SchPlace({b!r}) "
                        f"are both posed in region {reg.name!r} and their bodies overlap by {dx:.2f} x {dy:.2f} mm"
                    )
