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


def copper_bar(text: str, reasons: dict | None = None) -> dict:
    """Per-net and total numbers, plus the lines a report prints.

    `reasons` maps `bar_key` to the pattern that wrote that piece (D.4). With it the totals gain
    `by_reason`, `vias_pattern` and `vias_leftover`, and the report gains the line that says how much
    of the board is pcbc's own. Without it the output is byte-identical to what it was before R2,
    which is what `PCBC_PATTERNS=off` relies on.
    """
    segs = segments(text)
    vs = vias(text)
    pads = pads_by_net(text)
    nets: dict[str, dict] = {}
    for net, sites in pads.items():
        if len(sites) < 2 or not net or net.startswith("unconnected-"):
            continue
        nets[net] = {"airwire_mm": round(airwire_mm([(x, y) for _r, x, y in sites]), 3), "routed_mm": 0.0, "vias": 0, "off45": 0, "micro": 0, "segments": 0}
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
    for v in vs:
        rec = nets.get(v["net"])
        if rec is not None:
            rec["vias"] += 1
    for rec in nets.values():
        rec["routed_mm"] = round(rec["routed_mm"], 3)
        rec["detour"] = round(rec["routed_mm"] / rec["airwire_mm"], 2) if rec["airwire_mm"] >= 0.05 and rec["routed_mm"] > 0 else None
    reasons = reasons or {}
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
    ranked = sorted(((r["detour"], n, r) for n, r in nets.items() if r["detour"] is not None and r["airwire_mm"] >= 1.0), reverse=True)
    for detour, net, r in ranked[:3]:
        if detour < 1.5:
            break
        lines.append(f"copper: {net} runs {detour:g}x its airwire ({r['routed_mm']:g} of {r['airwire_mm']:g} mm), {r['vias']} via(s)")
    return lines
