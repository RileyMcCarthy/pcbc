"""Blocking analysis: what stands between an unreached pad and the rest of its net.

A routing failure that only names the net is a dead end for the AI. This names the pad, the
copper and pads in the corridor between it and its nearest same-net copper, which step put
each item there (the routed work dir keeps every step's board), and whose part each belongs
to, so the move is an edit to `board.py`: move that part, or give that net another layer.
Done by hand three times on the DS2 Addon (dump a box around the pad, read the step files);
it is mechanical.
"""

from __future__ import annotations

import math
import re
from pathlib import Path

from .copper_bar import segments, vias
from .layout import footprints_by_ref
from .model import Design
from .sexp import footprint_at

REACH_MM = 0.6  # how far beyond the corridor's box an item still counts as in the way
_STEP_FILE = re.compile(r"^(\d\d)_(.+)\.kicad_pcb$")


def step_boards(work: Path) -> list[tuple[str, str]]:
    """(step name, board text) for every step file in the routed work dir, in order."""
    out: list[tuple[str, str]] = []
    for p in sorted(Path(work).glob("[0-9][0-9]_*.kicad_pcb")):
        m = _STEP_FILE.match(p.name)
        if m:
            out.append((m.group(2), p.read_text()))
    return out


def _pads(design: Design, text: str) -> list[dict]:
    """Every pad with a net: ref, num, net, world position, half sizes."""
    from .pcb_place import _pins_of, parse_foot

    nets_of = _pins_of(design)
    out: list[dict] = []
    for ref, block in footprints_by_ref(text).items():
        at = footprint_at(block)
        if at is None:
            continue
        foot = parse_foot(ref, block)
        foot.at, foot.rot = (at[0], at[1]), at[2]
        for p in foot.pads:
            net = nets_of.get(ref, {}).get(p.num, p.net)
            if not p.num or not net:
                continue
            x, y = foot.pad_world(p)
            hw, hh = (p.h, p.w) if foot.rot % 180 == 90 else (p.w, p.h)
            out.append({"ref": ref, "num": p.num, "net": net, "at": (x, y), "half": (hw / 2.0, hh / 2.0)})
    return out


def _step_of(item_key: tuple, steps: list[tuple[str, dict]]) -> str:
    for name, keys in steps:
        if item_key in keys:
            return name
    return "?"


def _keys(text: str) -> dict:
    keys: dict = {}
    for s in segments(text):
        keys[("seg", s["layer"], s["start"], s["end"])] = True
    for v in vias(text):
        keys[("via", v["at"])] = True
    return keys


def _seg_dist(p: tuple[float, float], a: tuple[float, float], b: tuple[float, float]) -> float:
    ax, ay = a
    bx, by = b
    px, py = p
    dx, dy = bx - ax, by - ay
    if abs(dx) < 1e-9 and abs(dy) < 1e-9:
        return math.hypot(px - ax, py - ay)
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def _in_box(pt: tuple[float, float], box: tuple[float, float, float, float]) -> bool:
    return box[0] <= pt[0] <= box[2] and box[1] <= pt[1] <= box[3]


def _seg_in_box(a, b, box) -> bool:
    if _in_box(a, box) or _in_box(b, box):
        return True
    # A long segment crossing the box with both ends outside: sample it.
    for i in range(1, 8):
        t = i / 8.0
        if _in_box((a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t), box):
            return True
    return False


def move_line(net: str, what: str, goal: str, blockers: str, fixes: str = "", *, sep: str = " ") -> str:
    """The one sentence shape every pcbc routing failure speaks, so a pattern refusal and a KRT
    failure read the same and an AI that can act on one can act on the other (E.2).

    "<net>: <what> cannot reach <goal>. In the way: <blockers>. <fixes>" — `sep` is what stands
    between the sentences, a space for a one-line report and a newline plus two spaces for a
    pattern's multi-line refusal. `fixes` always ends in a `board.py` edit; a failure line that does
    not is a dead end, which is the whole reason this function exists rather than an f-string per
    call site.
    """
    tail = f"{sep}{fixes}" if fixes else ""
    return f"{net}: {what} cannot reach {goal}.{sep}In the way: {blockers}.{tail}"


def blocking_lines(design: Design, text: str, work: Path | None, net: str, named: list[str] | None = None, limit: int = 4) -> list[str]:
    """One line per unreached pad of `net` (at most three): what is in its way and whose it is."""
    pads = _pads(design, text)
    mine = [p for p in pads if p["net"] == net]
    if len(mine) < 2:
        return []
    segs = segments(text)
    vs = vias(text)
    own_pts: list[tuple[float, float]] = []
    for s in segs:
        if s["net"] == net:
            own_pts += [s["start"], s["end"]]
    own_pts += [v["at"] for v in vs if v["net"] == net]

    def reached(p: dict) -> bool:
        r = max(p["half"]) + 0.05
        return any(math.hypot(x - p["at"][0], y - p["at"][1]) <= r for x, y in own_pts)

    unreached = [p for p in mine if not reached(p)]
    if named:  # KRT named them: keep those first
        want = [n.split(" at ")[0] for n in named]
        unreached.sort(key=lambda p: (p["ref"] not in want, p["ref"], p["num"]))
    if not unreached or not own_pts:
        unreached = (unreached or mine)[:1]  # no copper at all: one corridor, from the first pad
    steps = [(name, _keys(t)) for name, t in (step_boards(work) if work else [])]
    by_net_pads: dict[str, list[dict]] = {}
    for p in pads:
        by_net_pads.setdefault(p["net"], []).append(p)

    def owner(item_net: str, at: tuple[float, float]) -> str:
        near = min(by_net_pads.get(item_net, []), key=lambda p: math.hypot(p["at"][0] - at[0], p["at"][1] - at[1]), default=None)
        return f"{near['ref']}.{near['num']}" if near else "?"

    lines: list[str] = []
    for p in unreached[:3]:
        px, py = p["at"]
        # The corridor: to the nearest copper of the net, else to the nearest other pad of the net.
        others = [q["at"] for q in mine if q is not p]
        target = min(own_pts or others, key=lambda t: math.hypot(t[0] - px, t[1] - py))
        box = (min(px, target[0]) - REACH_MM, min(py, target[1]) - REACH_MM, max(px, target[0]) + REACH_MM, max(py, target[1]) + REACH_MM)
        found: list[tuple[float, str]] = []
        for q in pads:
            if q["net"] == net or not _in_box(q["at"], box):
                continue
            found.append((math.hypot(q["at"][0] - px, q["at"][1] - py), f"{q['ref']}.{q['num']} pad [{q['net']}]"))
        for s in segs:
            if s["net"] == net or not _seg_in_box(s["start"], s["end"], box):
                continue
            step = _step_of(("seg", s["layer"], s["start"], s["end"]), steps)
            tag = ", locked" if s["locked"] else ""
            found.append((_seg_dist(p["at"], s["start"], s["end"]), f"{s['net']} track on {s['layer']} ({s['start'][0]:g},{s['start'][1]:g})-({s['end'][0]:g},{s['end'][1]:g}) [{step}{tag}, {owner(s['net'], s['start'])}'s net]"))
        for v in vs:
            if v["net"] == net or not _in_box(v["at"], box):
                continue
            step = _step_of(("via", v["at"]), steps)
            found.append((math.hypot(v["at"][0] - px, v["at"][1] - py), f"{v['net']} via at ({v['at'][0]:g},{v['at'][1]:g}) [{step}, {owner(v['net'], v['at'])}'s net]"))
        found.sort(key=lambda f: (f[0], f[1]))
        what = "; ".join(f for _d, f in found[:limit]) or "nothing pcbc can see: the pad itself may be unreachable (a closed row with no lane, or a keepout)"
        goal = "its own copper" if own_pts else f"{net}'s other pads"
        parts = sorted({f.split(" ")[0].split(".")[0] for _d, f in found[:limit] if " pad [" in f} | {f.split("'s net")[0].rsplit(", ", 1)[-1].split(".")[0] for _d, f in found[:limit] if "'s net" in f})
        move = f"Move {', '.join(parts)}, or give a net in the way another layer (NetReq(..., layers=))." if parts else ""
        lines.append(move_line(net, f"{p['ref']}.{p['num']} at ({px:g},{py:g})", goal, what, move))
    return lines
