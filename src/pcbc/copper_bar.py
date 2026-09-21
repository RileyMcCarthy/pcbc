"""The copper bar: what the routed board looks like, in numbers the tests can hold.

The schematic pass has a readability list; copper gets the same. Per net: routed length over
airwire (the detour ratio), vias, segments off 0/45/90 degrees, segments shorter than 0.2 mm
(a grid router's staircases). Per board: the totals and the worst net. `pcbc build` prints
the lines; `tests/test_examples_fab.py` holds the examples to their recorded numbers, so a
change to the plan that makes copper uglier fails a test, as with schematic readability.

KiCad counts two of these itself once pcbc writes the rules (`track_segment_length`,
`track_angle`, both as warnings); the bar counts them from the file so the number exists
without KiCad and so the two can be compared.
"""

from __future__ import annotations

import math
import re

from .copper import net_table, pads_by_net

MICRO_MM = 0.2  # a segment shorter than this is a grid artefact, not a route
_SEG = re.compile(
    r"\(segment\s*\(start ([-0-9.]+) ([-0-9.]+)\)\s*\(end ([-0-9.]+) ([-0-9.]+)\)\s*\(width ([-0-9.]+)\)"
    r'(?:\s*\(locked yes\))?\s*\(layer "([^"]+)"\)\s*\(net (?:(\d+)|(?:\d+\s+)?"([^"]*)")\)'
)
_VIA = re.compile(
    r'\(via\s*\(at ([-0-9.]+) ([-0-9.]+)\)\s*\(size ([-0-9.]+)\)\s*\(drill ([-0-9.]+)\)\s*\(layers "([^"]+)" "([^"]+)"\)'
    r'(?:\s*\(locked yes\))?\s*\(net (?:(\d+)|(?:\d+\s+)?"([^"]*)")\)'
)


def segments(text: str) -> list[dict]:
    """Every track segment: net, layer, start, end, width, length, locked."""
    names = net_table(text)
    out: list[dict] = []
    for m in _SEG.finditer(text):
        x1, y1, x2, y2, w = (float(m.group(i)) for i in range(1, 6))
        net = names.get(int(m.group(7))) if m.group(7) else m.group(8)
        out.append(
            {
                "net": net or "",
                "layer": m.group(6),
                "start": (x1, y1),
                "end": (x2, y2),
                "width": w,
                "length": math.hypot(x2 - x1, y2 - y1),
                "locked": "(locked yes)" in m.group(0),
            }
        )
    return out


def vias(text: str) -> list[dict]:
    names = net_table(text)
    out: list[dict] = []
    for m in _VIA.finditer(text):
        net = names.get(int(m.group(7))) if m.group(7) else m.group(8)
        out.append({"net": net or "", "at": (float(m.group(1)), float(m.group(2))), "size": float(m.group(3)), "locked": "(locked yes)" in m.group(0)})
    return out


def off_45(seg: dict) -> bool:
    """Not a multiple of 45 degrees, to half a degree. Zero-length segments are not angles."""
    if seg["length"] < 1e-6:
        return False
    (x1, y1), (x2, y2) = seg["start"], seg["end"]
    a = math.degrees(math.atan2(y2 - y1, x2 - x1)) % 45.0
    return min(a, 45.0 - a) > 0.5


def airwire_mm(points: list[tuple[float, float]]) -> float:
    """Minimum spanning tree length of the pad centres: the shortest copper that could connect them."""
    if len(points) < 2:
        return 0.0
    left = list(points[1:])
    best = {p: math.hypot(p[0] - points[0][0], p[1] - points[0][1]) for p in left}
    total = 0.0
    while left:
        nxt = min(left, key=lambda p: (best[p], p))
        total += best[nxt]
        left.remove(nxt)
        for p in left:
            d = math.hypot(p[0] - nxt[0], p[1] - nxt[1])
            if d < best[p]:
                best[p] = d
    return total


def bar_key(kind: str, *parts) -> tuple:
    """A copper item's geometry key, with a segment's two ends in a fixed order. `route.bar_key`'s
    twin: the board is rewritten twice between the piece being written and the bar reading it, and
    nothing guarantees which end KiCad writes first."""
    if kind == "seg":
        layer, a, b, w = parts
        ra, rb = (round(a[0], 4), round(a[1], 4)), (round(b[0], 4), round(b[1], 4))
        return ("seg", layer, min(ra, rb), max(ra, rb), round(w, 4))
    (at,) = parts
    return ("via", (round(at[0], 4), round(at[1], 4)))


def courtyard_boxes(text: str) -> list[tuple[str, tuple[float, float, float, float]]]:
    """Every footprint's courtyard as a world AABB, `(ref, box)`, sorted by ref.

    KiCad's courtyard rules are footprint-to-footprint only, so DRC says nothing about a via that
    lands in one and pcbc said nothing either (finding 4). It is not a refusal — a tented via under
    a part's keep-out is legal copper — but H.1's third decision is that a pattern which makes a
    neighbour worse gets a number, so the number is here.
    """
    from .geom import footprint_box_local
    from .sexp import board_footprint_spans, footprint_at, footprint_reference

    out: list[tuple[str, tuple[float, float, float, float]]] = []
    for start, end in board_footprint_spans(text):
        block = text[start:end]
        at = footprint_at(block)
        if at is None:
            continue
        x0, y0, x1, y1 = footprint_box_local(block, "courtyard")
        if x1 <= x0 or y1 <= y0:
            continue
        fx, fy, rot = at
        rad = math.radians(-rot)
        c, s = math.cos(rad), math.sin(rad)
        xs, ys = [], []
        for px, py in ((x0, y0), (x1, y0), (x1, y1), (x0, y1)):
            xs.append(fx + px * c - py * s)
            ys.append(fy + px * s + py * c)
        out.append((footprint_reference(block) or "?", (min(xs), min(ys), max(xs), max(ys))))
    return sorted(out)


def vias_in_courtyard(text: str, reasons: dict, owners: dict) -> list[dict]:
    """Every via pcbc placed that sits inside a courtyard belonging to some **other** footprint.

    Its own footprint's courtyard is where a tap is supposed to be: the via leaves that pad. The
    number that was silent is the other one: c3_usb's tap for `J1.A1B12` at (14.6729, 22.44) sits in
    `SW_RST`'s courtyard, and node's for `C_MCU.1` sat in `U1`'s until the antipad rule moved it.
    Both boards read 0 before the taps (`docs/r2-measurements.md` S5r, finding 4).
    """
    boxes = courtyard_boxes(text)
    out: list[dict] = []
    for v in vias(text):
        key = bar_key("via", v["at"])
        reason = reasons.get(key)
        if reason is None:
            continue  # KRT's leftover via: R3 owns where those land, and it is not measured here
        owner = (owners.get(key) or "").split(".")[0]
        x, y = v["at"]
        for ref, (x0, y0, x1, y1) in boxes:
            if ref != owner and x0 <= x <= x1 and y0 <= y <= y1:
                out.append({"via": [round(x, 4), round(y, 4)], "reason": reason, "owner": owners.get(key, ""), "courtyard": ref})
                break
    return sorted(out, key=lambda h: (h["courtyard"], h["via"]))


REDUNDANT = ("stitch",)
"""Reasons whose copper is **not part of the net's route** (`docs/stitch-plan.md` R-S1).

Every other reason here writes copper whose absence leaves a pad unreached, so counting its
millimetres against the net's airwire is the question `detour` exists to ask: how far past the
straight line did the route go. A `stitch` is the opposite by definition — a parallel barrel beside
one the router already placed, a shield beside a track — and its absence leaves nothing unconnected.
Measured on node, 2026-09-21: one rung adds 1.800 mm of `VBUS` copper and moves not one nanometre of
the route, and counting it took `DETOURS["node"]["VBUS"]` from **1.26 to 1.36** and
`LEFTOVER["node"]["VBUS"]`'s pattern half from 8.819 to 10.619 — two pinned numbers rising to report
that the route got worse, on a build where `off45`, `micro`, `vias_leftover`, `worst_detour`, every
other net's detour and the whole `SOFT` table are byte-identical. That is a ceiling measuring the
wrong thing, and it is the same correction `docs/stitch-plan.md` §6 makes one module over for
`ampacity.power_bottlenecks`: a 0.127 mm ground guard is not a narrow ground rail.

What it does **not** change: `routed_mm`, `segments` and `vias` still count every piece of copper on
the net, because a census that hides copper is worse than a ratio that misreads it, and `by_reason` /
`vias_pattern` still carry every stitch piece exactly. `pattern_mm + leftover_mm + stitch_mm ==
routed_mm` on every net, which `test_stitch.py` pins. S7's `"guard"` belongs in this tuple for the
same reason and is left out until there is copper carrying it."""


def copper_bar(text: str, reasons: dict | None = None, owners: dict | None = None, bridges: dict | None = None) -> dict:
    """Per-net and total numbers, plus the lines a report prints.

    `reasons` maps `bar_key` to the pattern that wrote that piece (D.4). With it the totals gain
    `by_reason`, `vias_pattern` and `vias_leftover`, the per-net rows gain their `leftover_*` half,
    and the report gains the line that says how much of the board is pcbc's own. Without it every
    piece reads as leftover, which is what a `PCBC_PATTERNS=off` board is: the numbers
    `test_patterns.py::test_patterns_off_claims_nothing_and_is_a_rollback` compares are unchanged.

    `bridges` maps `"A|B"` to the part that ties two `Ground()` nets, or to the verdict when nothing
    does (`route_verify.bridge_ties`). It is the one entry in this census that counts **no copper at
    all**, and deliberately: pcbc writes none for a ground tie (`docs/stitch-plan.md` section 8 item
    4), so the number a board is held to is *who* ties them — which is exactly what `vias_pattern` is
    for reasons, a value that moves when somebody changes the board and nothing else would say so.
    `{}` on all four examples, which each declare one `Ground()`.
    """
    segs = segments(text)
    vs = vias(text)
    pads = pads_by_net(text)
    nets: dict[str, dict] = {}
    reasons = reasons or {}
    for net, sites in pads.items():
        if len(sites) < 2 or not net or net.startswith("unconnected-"):
            continue
        nets[net] = {
            "airwire_mm": round(airwire_mm([(x, y) for _r, x, y in sites]), 3),
            "routed_mm": 0.0,
            "vias": 0,
            "off45": 0,
            "micro": 0,
            "segments": 0,
            # The leftover's own half of every number above (finding 10). Without it there is no
            # figure anywhere for what KRT paid for a pattern: `routed_mm` and `detour` are over
            # **all** the copper on the net, pattern and leftover together, so c3_usb's `3V3`
            # reads as a 19.81 mm win (leftover 50.90 -> 31.09) while the net's own total copper
            # rose 10 % and its leftover doubled in segments. A pattern that starts costing more
            # leftover than it writes is then a number that moved rather than a silence.
            "leftover_mm": 0.0,
            "leftover_segments": 0,
            "leftover_vias": 0,
            "leftover_micro": 0,
            "pattern_mm": 0.0,
            # Copper pcbc wrote on this net that is not part of its route (`REDUNDANT`). Held apart
            # from `pattern_mm` rather than folded into it, so the spine's trade with the leftover
            # stays a trade between two things that route.
            "stitch_mm": 0.0,
        }
    for s in segs:
        rec = nets.get(s["net"])
        if rec is None:
            continue
        rec["segments"] += 1
        rec["routed_mm"] += s["length"]
        if off_45(s):
            rec["off45"] += 1
        if 1e-6 < s["length"] < MICRO_MM:
            rec["micro"] += 1
        why = reasons.get(bar_key("seg", s["layer"], s["start"], s["end"], s["width"]), "leftover")
        if why == "leftover":
            rec["leftover_segments"] += 1
            rec["leftover_mm"] += s["length"]
            if 1e-6 < s["length"] < MICRO_MM:
                rec["leftover_micro"] += 1
        elif why in REDUNDANT:
            rec["stitch_mm"] += s["length"]
        else:
            rec["pattern_mm"] += s["length"]
    for v in vs:
        rec = nets.get(v["net"])
        if rec is not None:
            rec["vias"] += 1
            if reasons.get(bar_key("via", v["at"]), "leftover") == "leftover":
                rec["leftover_vias"] += 1
    for rec in nets.values():
        rec["routed_mm"] = round(rec["routed_mm"], 3)
        rec["leftover_mm"] = round(rec["leftover_mm"], 3)
        rec["pattern_mm"] = round(rec["pattern_mm"], 3)
        rec["stitch_mm"] = round(rec["stitch_mm"], 3)
        # The ratio is over the copper that **routes** the net. `REDUNDANT` says why, and on every
        # net with no stitch copper `route` is `routed_mm` to the last bit, so nothing else moves.
        route = round(rec["routed_mm"] - rec["stitch_mm"], 3)
        rec["detour"] = round(route / rec["airwire_mm"], 2) if rec["airwire_mm"] >= 0.05 and route > 0 else None
    by_reason: dict[str, dict] = {}
    for item, key in [(s, bar_key("seg", s["layer"], s["start"], s["end"], s["width"])) for s in segs] + [(v, bar_key("via", v["at"])) for v in vs]:
        row = by_reason.setdefault(reasons.get(key, "leftover"), {"segments": 0, "vias": 0, "mm": 0.0})
        if "length" in item:
            row["segments"] += 1
            row["mm"] += item["length"]
        else:
            row["vias"] += 1
    totals = {
        "nets": len(nets),
        "segments": len(segs),
        "vias": len(vs),
        "off45": sum(1 for s in segs if off_45(s)),
        "micro": sum(1 for s in segs if 1e-6 < s["length"] < MICRO_MM),
        "routed_mm": round(sum(s["length"] for s in segs), 1),
    }
    # The worst detour among nets long enough for the ratio to mean something.
    worst = max(((r["detour"], n) for n, r in nets.items() if r["detour"] is not None and r["airwire_mm"] >= 1.0), default=None)
    totals["worst_detour"] = [worst[1], worst[0]] if worst else None
    totals["by_reason"] = {r: {"segments": v["segments"], "vias": v["vias"], "mm": round(v["mm"], 1)} for r, v in sorted(by_reason.items())}
    totals["vias_pattern"] = {r: v["vias"] for r, v in sorted(by_reason.items()) if r != "leftover" and v["vias"]}
    totals["vias_leftover"] = by_reason.get("leftover", {}).get("vias", 0)
    # Per net, not just the worst: `worst_detour` is a max, and a max hides every net under it. It
    # held at USB_DN 1.81 on c3_usb through S5 while VBUS went 1.33 -> 1.70 underneath it, which is
    # the slice's largest per-net regression and was in no number anywhere (finding 16).
    totals["detours"] = {n: r["detour"] for n, r in sorted(nets.items()) if r["detour"] is not None and r["airwire_mm"] >= 1.0}
    totals["vias_in_courtyard"] = vias_in_courtyard(text, reasons, owners or {}) if reasons else []
    totals["bridge"] = dict(sorted((bridges or {}).items()))
    return {"nets": nets, "totals": totals, "lines": bar_lines(nets, totals)}


def bar_lines(nets: dict[str, dict], totals: dict) -> list[str]:
    lines = [
        f"copper: {totals['segments']} segments, {totals['vias']} vias, {totals['routed_mm']:g} mm; "
        f"{totals['off45']} off 0/45/90, {totals['micro']} under {MICRO_MM:g} mm"
    ]
    owned = {r: v for r, v in totals.get("by_reason", {}).items() if r != "leftover"}
    if owned:
        left = totals["by_reason"].get("leftover", {"segments": 0, "mm": 0.0, "vias": 0})
        what = ", ".join(f"{r} {v['segments']} seg" + (f" {v['vias']} vias" if v["vias"] else "") for r, v in owned.items())
        lines.append(
            f"copper: pcbc owns {sum(v['segments'] for v in owned.values())} segments / "
            f"{round(sum(v['mm'] for v in owned.values()), 1):g} mm and {sum(v['vias'] for v in owned.values())} vias ({what}); "
            f"leftover {left['segments']} segments / {left['mm']:g} mm / {left['vias']} vias"
        )
    cy = totals.get("vias_in_courtyard") or []
    if cy:
        who = ", ".join(f"{h['owner'] or h['reason']} in {h['courtyard']}" for h in cy[:3])
        lines.append(f"copper: {len(cy)} of pcbc's vias sit in another footprint's courtyard ({who})")
    for pair, who in (totals.get("bridge") or {}).items():
        a, _, b = pair.partition("|")
        loose = who in ("none", "open", "multi", "off_board")
        lines.append(f"copper: {a} and {b} [{who}] — pcbc writes no copper for a ground tie" if loose else f"copper: {a} and {b} are tied at {who}, and at nothing else")
    ranked = sorted(((r["detour"], n, r) for n, r in nets.items() if r["detour"] is not None and r["airwire_mm"] >= 1.0), reverse=True)
    for detour, net, r in ranked[:3]:
        if detour < 1.5:
            break
        route = round(r["routed_mm"] - r.get("stitch_mm", 0.0), 3)
        lines.append(f"copper: {net} runs {detour:g}x its airwire ({route:g} of {r['airwire_mm']:g} mm), {r['vias']} via(s)")
    return lines
