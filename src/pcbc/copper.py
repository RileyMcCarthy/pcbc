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
            box = _pad_aabb(fx + lx, fy + ly, float(sm.group(1)), float(sm.group(2)), frot + prot)
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
    copper = copper_by_net(text)
    fails: list[str] = []
    for net in job.nets:
        if net.vias:
            continue
        for name, rec in copper.items():
            if rec.get("via") and _match(name, net.patterns):
                fails.append(f"{name} has {int(rec['via'])} via(s); NetReq vias=False")
    return fails


def _class_need(job: CompiledJob, name: str, amps: float) -> float:
    """What the current asks of a track: `max(IPC-2221 external, IPC-2152 with modifiers)`.

    The same `max` `stackup.current_width_mm` takes and the same one `Constraint.width_mm` was
    derived from, so the gate asks the question the width was an answer to. It used to read
    `width_ipc2221` alone (finding 1), which is the smaller of the two on every net where the 2152
    curve wins and made the gate quietly weaker than the number on the board."""
    cs = job.constraints
    c = cs.by_net(name) if cs is not None else None
    if c is not None and c.current is not None:
        return max(c.current.width_ipc2221.value, c.current.width_ipc2152.value)
    return ipc2221_width_mm(amps)


def _class_width(job: CompiledJob, name: str) -> float:
    cs = job.constraints
    c = cs.by_net(name) if cs is not None else None
    return float(c.width_mm.value) if c is not None else 0.0


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
    job: CompiledJob, text: str, owned: Sequence[tuple[str, str, float]] = ()
) -> list[str]:
    """The hard gate: **pcbc must not write a necked power track**, and a rail must have copper.

    `owned` is `(net, reason, width_mm)` per piece pcbc wrote, read off the board's own `pcbc:` role
    groups with the width the board has (`fab.board_roles` -> `layout_job.roles_doc`); the router's
    sidecar `copper.json` is consumed by normalise and nothing here reads it. With it empty only the
    missing-rail rule can fire, which is what a board with no pattern copper has to be judged by.

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
    copper, at the class width the pattern declared, with the one exemption R-I3 already grants
    (`PATTERN_NECK_OK`).
    """
    copper = copper_by_net(text)
    plane_nets = {n for n, _ in job.planes} | {n for n, r in copper.items() if r.get("zone")}
    fails: list[str] = []
    for net in job.nets:
        if net.kind != "power" or not net.amps or net.amps < 0.2:
            continue
        need = _class_need(job, net.patterns[0], float(net.amps))
        for name, reason, width in owned:
            if reason in PATTERN_NECK_OK or not _match(name, net.patterns):
                continue
            cls = _class_width(job, name)
            if cls and width + 1e-6 < cls:
                fails.append(
                    f"{name} {reason} copper {width:g} mm < the {cls:g} mm {name} class "
                    f"({net.amps:g} A, IPC asks {_class_need(job, name, float(net.amps)):g} mm)"
                )
        # Pattern never seen as copper or plane
        if not any(_match(n, net.patterns) for n in list(copper) + list(plane_nets)):
            if net.amps >= 1.0:
                fails.append(
                    f"{net.patterns[0]} has no copper/plane for {net.amps:g} A "
                    f"(need ≥ {need:.2f} mm)"
                )
    return sorted(set(fails))
