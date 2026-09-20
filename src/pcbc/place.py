"""Every part is placed with CSS Place() — that pose is the layout."""

from __future__ import annotations

from pathlib import Path

from .apply import apply_job
from .check import check_job
from .compile import compile_design
from .model import Design
from .pcb_place import layout_report, resolve_places
from .project import copy_with_siblings


def _decaps(design: Design) -> list[str]:
    """Two-pin caps between a supply and a ground: their distance to the pin beats their label."""
    kinds = {n.name: getattr(n, "kind", "") for n in design.nets.values()}
    out = []
    for inst in design.instances:
        if not inst.ref.startswith("C") or len(inst.pins) != 2:
            continue
        ks = {kinds.get(net, "") for net in inst.pins.values()}
        if ks == {"power", "ground"}:
            out.append(inst.ref)
    return out


def place_job(design: Design, seed: Path, *, out: Path) -> dict:
    job = compile_design(design)
    seed = Path(seed)
    out = Path(out)
    copy_with_siblings(seed, out)
    from .fab import insert_fiducials
    from .model import KeepoutSpec
    from .pcb_place import FID_HALF, GAP
    from .silk import silk_job

    seed_text = out.read_text()
    specs = list(job.places)
    decaps = frozenset(_decaps(design))
    for round_ in range(3):
        # A part whose silkscreen reference finds no room is placed again with more gap (relations only);
        # the anchors and the rest stay put. Two rounds, then the report says so.
        job.places = specs
        job.keepouts = [k for k in job.keepouts if not k.name.endswith("_mask")]
        out.write_text(seed_text)
        job.places, moves, fids = resolve_places(design, job, seed_text)
        if fids:
            text, _ = insert_fiducials(out.read_text(), job.board_size_mm, spots=fids)
            out.write_text(text)
            # The router keeps off a fiducial's copper dot, not its 2 mm mask opening: a track
            # through the opening is a solder-mask bridge. A keepout the size of its courtyard.
            for ref, x, y in fids:
                job.keepouts.append(KeepoutSpec(name=f"{ref}_mask", box=(x - FID_HALF, y - FID_HALF, x + FID_HALF, y + FID_HALF), no=("copper", "via")))
        applied = apply_job(job, out, backup=False)
        silk = silk_job(job, out, out=out, backup=False, hide_if_no_room=decaps)["silk"]  # fab and review re-run it, same answer
        cramped = {m.split(":")[0] for m in silk.get("issues", [])}
        retry = [s for s in specs if s.ref in cramped and s.to and s.ref not in decaps]
        if not retry or round_ == 2:
            break
        from dataclasses import replace

        specs = [replace(s, gap=(s.gap if s.gap is not None else GAP) + 0.6) if s in retry else s for s in specs]
    fails = check_job(job, out)
    report = moves + layout_report(design, job, out.read_text()) + silk.get("issues", [])
    notes = list(silk.get("notes", []))
    result = {
        "pcb": str(out),
        "applied": applied,
        "check": fails,
        "layout": report,
        "notes": notes,
        "poses": {p.ref: {"at": list(p.at) if p.at else None, "rot": p.rot} for p in job.places},
        "error": None,
    }
    if applied.get("missing"):
        result["error"] = "missing refs: " + ", ".join(applied["missing"])
    if fails:
        result["error"] = result.get("error") or ("check: " + "; ".join(fails))
    return result
