"""Straight F.Cu segments for 2-pin nets. Enough for blinky without KRT."""

from __future__ import annotations

from pathlib import Path

from .compile import compile_design
from .copper import pads_by_net, unrouted_nets
from .model import Design
from .project import copy_with_siblings
from .sexp import stable_uuid


def _width_for(design: Design, net: str) -> float:
    job = compile_design(design)
    for n in job.nets:
        from fnmatch import fnmatch

        if any(fnmatch(net, pat) for pat in n.patterns):
            for cls in job.classes:
                if cls.name == n.class_name:
                    return float(cls.track_width_mm)
    return 0.2


def _segment(x1: float, y1: float, x2: float, y2: float, width: float, net: str, board: str, i: int) -> str:
    return (
        f"\t(segment\n"
        f"\t\t(start {x1:.6f} {y1:.6f})\n"
        f"\t\t(end {x2:.6f} {y2:.6f})\n"
        f"\t\t(width {width:g})\n"
        f'\t\t(layer "F.Cu")\n'
        f'\t\t(net "{net}")\n'
        f'\t\t(uuid "{stable_uuid(board, "seg", net, i)}")\n'
        f"\t)\n"
    )


def route_job(design: Design, placed: Path, *, out: Path, name: str = "board") -> dict:
    placed = Path(placed)
    out = Path(out)
    copy_with_siblings(placed, out)
    text = out.read_text()
    pads = pads_by_net(text)
    segs: list[str] = []
    i = 0
    for net, sites in sorted(pads.items()):
        if len(sites) < 2:
            continue
        w = _width_for(design, net)
        # MST: nearest-neighbour chain from the first pad.
        remaining = list(sites)
        cur = remaining.pop(0)
        while remaining:
            nxt = min(remaining, key=lambda s: (s[1] - cur[1]) ** 2 + (s[2] - cur[2]) ** 2)
            remaining.remove(nxt)
            segs.append(_segment(cur[1], cur[2], nxt[1], nxt[2], w, net, name, i))
            i += 1
            cur = nxt
    if segs:
        stripped = text.rstrip()
        if not stripped.endswith(")"):
            raise ValueError("board file does not end with )")
        text = stripped[:-1] + "".join(segs) + ")\n"
        out.write_text(text)
    opens = unrouted_nets(out.read_text())
    return {
        "pcb": str(out),
        "segments": len(segs),
        "unrouted": opens,
        "error": ("unrouted " + ", ".join(opens)) if opens else None,
    }
