"""Board copper census: unrouted nets, analog airwires, power ampacity."""

from __future__ import annotations

import fnmatch
import math
import re
from collections.abc import Sequence

from .compile import CompiledJob
from .sexp import board_footprint_spans, footprint_at, footprint_reference, matching_paren
from .stackup import ipc2221_width_mm

_NET_DEF = re.compile(r'\(net\s+(\d+)\s+"([^"]*)"\)')
_PAD_AT = re.compile(r"\(at\s+([0-9.+-]+)\s+([0-9.+-]+)(?:\s+([0-9.+-]+))?\)")
_PAD_SIZE = re.compile(r"\(size\s+([0-9.+-]+)\s+([0-9.+-]+)\)")
_PAD_LAYERS = re.compile(r"\(layers\s+([^)]+)\)")
_WIDTH = re.compile(r"\(width\s+([0-9.+-]+)\)")


def net_table(text: str) -> dict[int, str]:
    return {int(i): name for i, name in _NET_DEF.findall(text)}


def _iter_board_tag(text: str, tag: str):
    token = f"({tag}"
    start = 0
    while True:
        j = text.find(token, start)
        if j < 0:
            return
        nxt = j + len(token)
        if nxt < len(text) and (text[nxt].isalnum() or text[nxt] == "_"):
            start = nxt
            continue
        end = matching_paren(text, j)
        yield text[j : end + 1]
        start = end + 1


def _rotate(x: float, y: float, deg: float) -> tuple[float, float]:
    a = math.radians(-deg)
    c, s = math.cos(a), math.sin(a)
    return x * c - y * s, x * s + y * c


def _net_name_of(block: str, names: dict[int, str] | None = None) -> str:
    names = names or {}
    m = re.search(r'\(net "([^"]+)"\)', block)
    if m:
        raw = m.group(1)
        return names.get(int(raw), raw) if raw.isdigit() else raw
    m = re.search(r'\(net (\d+)(?: "([^"]*)")?\)', block)
    if not m:
        return ""
    if m.lastindex >= 2 and m.group(2):
        return m.group(2)
    return names.get(int(m.group(1)), "")


def pads_by_net(text: str) -> dict[str, list[tuple[str, float, float]]]:
    names = net_table(text)
    out: dict[str, list[tuple[str, float, float]]] = {}
    for start, end in board_footprint_spans(text):
        block = text[start:end]
        ref = footprint_reference(block) or "?"
        at = footprint_at(block) or (0.0, 0.0, 0.0)
        fx, fy, frot = at
        pos = 0
        while True:
            j = block.find("(pad ", pos)
            if j < 0:
                break
            k = matching_paren(block, j)
            pad = block[j : k + 1]
            pos = k + 1
            name = _net_name_of(pad, names)
            am = _PAD_AT.search(pad)
            if not name or not am:
                continue
            px, py = float(am.group(1)), float(am.group(2))
            lx, ly = _rotate(px, py, frot)
            out.setdefault(name, []).append((ref, fx + lx, fy + ly))
    return out


def copper_by_net(text: str) -> dict[str, dict[str, float]]:
    """Per net: segment count, via count, zone count, widest and narrowest track width mm.

    **Both widths, because they answer different questions.** `width` is the maximum and is what a
    presence test wants — "is there copper of this class on this net at all", and a zone reads as
    10 mm because a plane is wider than any rule asks. `width_min` is the minimum **over segments
    only**, and it is the one an ampacity question wants: a net is as good as its narrowest series
    copper, and the maximum is satisfied by one wide segment however narrow the rest is. Reading the
    maximum is what made `power_ampacity_failures` unable to fail (S7 review, findings 1, 7, 12 and
    20) while buck shipped 19.39 mm of 0.3905 mm copper on a 2 A net. `width_min` is 0.0 on a net
    with no segments at all.
    """
    names = net_table(text)
    hits: dict[str, dict[str, float]] = {}
    for tag in ("segment", "via", "zone"):
        for block in _iter_board_tag(text, tag):
            name = _net_name_of(block, names)
            if not name:
                continue
            rec = hits.setdefault(
                name, {"segment": 0, "via": 0, "zone": 0, "width": 0.0, "width_min": 0.0}
            )
            rec[tag] += 1
            if tag == "segment":
                wm = _WIDTH.search(block)
                if wm:
                    w = float(wm.group(1))
                    rec["width"] = max(rec["width"], w)
                    rec["width_min"] = w if not rec["width_min"] else min(rec["width_min"], w)
            if tag == "zone":
                rec["width"] = max(rec["width"], 10.0)
    return hits


def unrouted_nets(text: str) -> list[str]:
    pads = pads_by_net(text)
    copper = copper_by_net(text)
    open_nets: list[str] = []
    for name, sites in pads.items():
        if len(sites) < 2 or not name or name.startswith("unconnected-"):
            continue
        hit = copper.get(name) or {}
        if hit.get("segment") or hit.get("via") or hit.get("zone"):
            continue
        open_nets.append(name)
    return sorted(set(open_nets))


def airwire_span_mm(text: str) -> dict[str, float]:
    pads = pads_by_net(text)
    out: dict[str, float] = {}
    for name, sites in pads.items():
        if len(sites) < 2 or not name:
            continue
        span = 0.0
        for i, (_r1, x1, y1) in enumerate(sites):
            for _r2, x2, y2 in sites[i + 1 :]:
                span = max(span, math.hypot(x2 - x1, y2 - y1))
        out[name] = span
    return out


def _match(name: str, patterns: tuple[str, ...] | list[str]) -> bool:
    return any(fnmatch.fnmatch(name, pat) for pat in patterns)


def _cu_layers(raw: str) -> set[str]:
    toks = re.findall(r'"([^"]+)"|[A-Za-z0-9_.*]+', raw)
    out: set[str] = set()
    for t in toks:
        if t == "*.Cu":
            out.update({"F.Cu", "B.Cu", "In1.Cu", "In2.Cu"})
        elif t.endswith(".Cu"):
            out.add(t)
    return out


def _pad_aabb(
    x: float, y: float, sx: float, sy: float, rot: float
) -> tuple[float, float, float, float]:
    hx, hy = sx / 2, sy / 2
    xs, ys = [], []
    for px, py in ((-hx, -hy), (hx, -hy), (hx, hy), (-hx, hy)):
        wx, wy = _rotate(px, py, rot)
        xs.append(x + wx)
        ys.append(y + wy)
    return (min(xs), min(ys), max(xs), max(ys))


def _aabb_gap(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> float:
    dx = max(0.0, a[0] - b[2], b[0] - a[2])
    dy = max(0.0, a[1] - b[3], b[1] - a[3])
    if dx and dy:
        return math.hypot(dx, dy)
    return dx + dy


def pad_clearance_failures(text: str, clearance: float = 0.16) -> list[str]:
    """Different-net pads on a shared copper layer closer than *clearance*.

    Same-footprint pairs are skipped (KiCad AABB false shorts on a rotated
    module). This is the placement pad-pad gate: NTC on a Teensy pin row
    must fail even when CSS locks still match.
    """
    names = net_table(text)
    recs: list[tuple[str, str, str, tuple[float, float, float, float], set[str]]] = []
    for start, end in board_footprint_spans(text):
        block = text[start:end]
        ref = footprint_reference(block) or "?"
        at = footprint_at(block) or (0.0, 0.0, 0.0)
        fx, fy, frot = at
        pos = 0
        while True:
            j = block.find("(pad ", pos)
            if j < 0:
                break
            k = matching_paren(block, j)
            pad = block[j : k + 1]
            pos = k + 1
            net = _net_name_of(pad, names)
            am = _PAD_AT.search(pad)
            sm = _PAD_SIZE.search(pad)
            lm = _PAD_LAYERS.search(pad)
            if not net or net.startswith("unconnected-") or not am or not sm:
                continue
            px, py = float(am.group(1)), float(am.group(2))
            prot = float(am.group(3) or 0)
            lx, ly = _rotate(px, py, frot)
            # A board file writes the pad's angle with the footprint's already in it (KiCad's board-file
            # convention, `pads.PadSpec.angle`); adding `frot` again turned a 90-degree part's pads by 180.
            box = _pad_aabb(fx + lx, fy + ly, float(sm.group(1)), float(sm.group(2)), prot)
            layers = _cu_layers(lm.group(1) if lm else "")
            recs.append((ref, net, pad[5:20], box, layers or {"F.Cu"}))
    fails: list[str] = []
    for i, (r1, n1, _p1, a, la) in enumerate(recs):
        for r2, n2, _p2, b, lb in recs[i + 1 :]:
            if r1 == r2 or n1 == n2:
                continue
            if not (la & lb):
                continue
            gap = _aabb_gap(a, b)
            if gap + 1e-9 < clearance:
                fails.append(
                    f"{r1} ({n1}) vs {r2} ({n2}) pad gap {gap:.3f} mm < {clearance:g} mm"
                )
    return fails


def sensitive_airwire_failures(job: CompiledJob, text: str) -> list[str]:
    spans = airwire_span_mm(text)
    pads = pads_by_net(text)
    locked = {p.ref for p in job.places if p.locked}
    fails: list[str] = []
    for net in job.nets:
        if net.max_length_mm is None:
            continue
        for name, span in spans.items():
            if not _match(name, net.patterns):
                continue
            sites = pads.get(name) or []
            anchors = [(x, y) for r, x, y in sites if r in locked]
            if net.kind == "switch_node" and anchors:
                span = 0.0
                for _r, x, y in sites:
                    span = max(
                        span,
                        min(math.hypot(x - ax, y - ay) for ax, ay in anchors),
                    )
            if span > float(net.max_length_mm) + 0.05:
                fails.append(
                    f"{name} airwire {span:.1f} mm > max_mm {net.max_length_mm:g} "
                    f"({net.kind or net.class_name})"
                )
    return fails


def vias_on_no_via_nets(job: CompiledJob, text: str) -> list[str]:
    """E22 (`docs/physics-subjects.md` §7): a via on a link whose Path allows none, per link owner (a net writes no
    `vias=` since PR 5). The owners are the board's own `pcbc:` role groups."""
    cs = job.constraints
    if cs is None:
        return []
    from .layout_job import roles_doc

    try:
        roles = roles_doc(text)
    except ValueError:
        return []
    count: dict[str, int] = {}
    for i in roles.items:
        if i["key"][0] != "via":
            continue
        lk = cs.link_by_key(str(i.get("owner", "")))
        if lk is not None and not lk.via.allowed:
            count[lk.key] = count.get(lk.key, 0) + 1
    fails: list[str] = []
    for key in sorted(count):
        lk = cs.link_by_key(key)
        said = f"Path line {lk.line} says vias=False" if lk.vias_src.startswith("Path line") else f"Path line {lk.line} is kind {lk.kind}, whose default is no vias"
        fails.append(f"{lk.net}: link {lk.key} has {count[key]} via(s), and {said}")
    return fails


PATTERN_NECK_OK = ("fanout", "guard", "tap")
"""The reasons whose copper is allowed to be narrower than its class.

R-I3's allowance is for **the last millimetre into a pad**, and two of these are the patterns that
write only that: `tap._width` and `fanout` neck an escape stub to the pad's own across dimension,
which `TAP_NECKED` counts and `test_examples_fab.py` pins. Everything else pcbc writes — a hop, a
spine trunk, a backbone link, a chain, a bus member — is written at the class width or not at all
(B.0: a pattern never degrades), so a narrow one is a bug in the pattern rather than a fact about the
board.

`guard` is here for a different reason and a stronger one: **a guard is not on the rail at all.**
`docs/r2-design.md` B.6 specifies its width as `stack.track_min` because a shield carries no current
— it is a ground track beside another net, tied to the pour by stitch vias — so comparing it to the
ground net's *class* width asks how much current a thing designed to carry none can carry. Measured
2026-09-21 on `tests/fixtures/guard/guard.py` with its `GND` declared at 0.5 A instead of 0.1: the
gate returned **"GND guard copper 0.127 mm < the 0.4 mm GND class (0.5 A, IPC asks 0.15 mm)"** and
stopped the build, on copper whose absence changes the rail by nothing. It is the same correction
`copper_bar.REDUNDANT` makes one census over and `ampacity.power_bottlenecks` makes one gate over: a
0.127 mm ground guard is not a narrow ground rail."""


def power_ampacity_failures(
    job: CompiledJob, text: str, owned: Sequence[tuple] = (), necks: tuple = ()
) -> list[str]:
    """The hard gate: **pcbc must not write a necked power track**, and a rail must have copper.

    `owned` is `(net, reason, width_mm[, owner, layer])` per piece pcbc wrote, read off the board's own
    `pcbc:` role groups with the width the board has (`fab.board_roles` -> `layout_job.roles_doc`); the
    router's sidecar `copper.json` is consumed by normalise and nothing here reads it. With it empty
    only the missing-rail rule can fire, which is what a board with no pattern copper has to be judged
    by.

    **What this deliberately does not judge, and why.** Until the S7 review this function compared
    the net's *widest* track (`copper_by_net`'s `width` is a `max`) against IPC, so one wide segment
    satisfied a whole net and the check could not fail — it returned `[]` on buck while buck shipped
    19.39 mm of 0.3905 mm copper on a 2 A rail, and returned `[]` just as readily on the
    patterns-off board whose `VIN` is two thirds fab-floor copper (findings 1, 7, 12, 20). The honest
    whole-net question is the **bottleneck** between the net's pads, and `ampacity.power_bottlenecks`
    asks it and reports the answer as a power move (`ampacity.power_moves`; `--strict-power` makes it
    stop the build). The numbers are pinned per board in `test_examples_fab.py` (`BOTTLENECK`) as a
    ledger that must not get worse.

    So the criterion here is the one pcbc *is* answerable for, and it is failable: a pattern's own
    copper, at the width the compiler gave it **on the piece's own layer** (`Constraint.width_on`, PR 9
    of `docs/net-path-physics.md`), with the one exemption R-I3 already grants (`PATTERN_NECK_OK`). A
    piece whose role owner is a path's link is judged against that link's width on its layer, at that
    link's current (§4.4) — the second loop, which also reaches nets whose only current is a path's (a
    migrated `5V` writes no `NetReq(amps=)`; `SW` is not `power`).

    Decision 1 (owner, 2026-10-01): on a path's link the only copper allowed narrower than the link is a
    **pad neck** (`route_verify.pad_necks`). `necks` is `(accepted, refused)` from it, each a list of rows
    keyed by `copper_bar.bar_key`, and `owned` rows then carry that key as their sixth element: an accepted
    neck passes, a refused one fails with the fact that refused it, and `PATTERN_NECK_OK` no longer exempts
    link copper (only a guard, which is not on the rail).
    """
    copper = copper_by_net(text)
    plane_nets = {n for n, _ in job.planes} | {n for n, r in copper.items() if r.get("zone")}
    cs = job.constraints
    fails: list[str] = []

    def row(r: tuple) -> tuple[str, str, float, str, str]:
        return (str(r[0]), str(r[1]), float(r[2]), str(r[3]) if len(r) > 3 else "", str(r[4]) if len(r) > 4 else "")

    links = {}
    if cs is not None and hasattr(cs, "link_by_key"):
        for c in cs.constraints:
            for p in c.paths:
                for lk in p.links:
                    links[lk.key] = lk
    # The path loop (`docs/physics-subjects.md` PR 3: a net carries no current, so every declared current is
    # a path link's): copper a path's link owns, against that link's width on the piece's layer. A pad
    # neck (decision 1) is the one narrower piece a link may carry.
    ok_necks = {n["key"] for n in (necks[0] if necks else ())}
    bad_necks = {n["key"]: n for n in (necks[1] if necks else ())}
    for r in owned:
        name, reason, width, owner, layer = row(r)
        lk = links.get(owner)
        if lk is None or reason == "guard" or (reason in PATTERN_NECK_OK and not necks):
            continue
        need_w = max(d.value for _l, d in lk.width) if reason == "route_group" else lk.width_on(layer)
        key = tuple(r[5]) if len(r) > 5 else None
        if key is not None and key in ok_necks:
            continue
        if key is not None and key in bad_necks:
            from .route_verify import neck_refusal_lines

            fails += neck_refusal_lines([bad_necks[key]])
            continue
        if width + 1e-6 < need_w:
            amps = f"{lk.current.amps:g} A" if lk.current is not None and lk.amps_written else (f"kind {lk.kind}" if lk.kind else "the net's numbers")
            what = "landing" if reason == "route_group" else "copper"
            fails.append(f"{name} {reason} {what} {width:g} mm on {layer} < the {need_w:g} mm {lk.source}'s link {lk.key} needs on {layer} ({amps})")
    # A path with a current whose net has no copper and no plane on the emitted board (it moved here
    # from the net loop PR 3 deleted): at 1 A or more, the board carries nothing at all for it.
    if cs is not None:
        for c in cs.constraints:
            for p in c.paths:
                if p.amps is None or p.amps < 1.0:
                    continue
                if not any(n == c.net for n in list(copper) + list(plane_nets)):
                    lk = next((x for x in p.links if x.current is not None), None)
                    need = max(lk.current.width_ipc2221.value, lk.current.width_ipc2152.value) if lk is not None else 0.0
                    fails.append(f"{c.net} has no copper/plane for Path line {p.line}'s {p.amps:g} A (need ≥ {need:.2f} mm)")
    # The off-path loop: on a power net, and on a net whose paths carry a field, the copper no link owns
    # (the off-path branches, §3.1) against the net's own width on the piece's layer (a pattern never
    # writes a power net under its class width; the class is the floor since PR 3). The router lays it at those
    # numbers; until this loop nothing on the emitted board checked it (refutation round 1, defect 10:
    # buck's C_OUT2.1 branch narrowed 0.4 -> 0.25 mm passed every gate, fab and KiCad included,
    # because KiCad's 5V rule is the 0.2 mm tap).
    if cs is not None:
        for r in owned:
            name, reason, width, owner, layer = row(r)
            if owner in links or reason in PATTERN_NECK_OK:
                continue
            c = cs.by_net(name)
            if c is None or not (c.kind == "power" or (hasattr(c, "fielded") and c.fielded())):
                continue
            need_w = c.width_on(layer) if layer and hasattr(c, "width_on") else float(c.width_mm.value)
            if width + 1e-6 < need_w:
                fails.append(
                    f"{name} {reason} copper {width:g} mm on {layer} < the {need_w:g} mm {name} needs on {layer} "
                    f"off its paths (on no Path: the net's own numbers, {_netreq_src(c)})"
                )
    return sorted(set(fails))


def _netreq_src(c) -> str:
    return f"NetReq line {c.line}" if getattr(c, "line", 0) else f"class {c.class_name}"
