"""Every part is placed with CSS Place() — that pose is the layout."""

from __future__ import annotations

from pathlib import Path

from .apply import apply_job
from .check import check_job
from .compile import compile_design
from .model import Design
from .pcb_place import layout_report, resolve_places
from .project import copy_with_siblings


def place_job(design: Design, seed: Path, *, out: Path) -> dict:
    job = compile_design(design)
    seed = Path(seed)
    out = Path(out)
    copy_with_siblings(seed, out)
    job.places, moves, fids = resolve_places(design, job, out.read_text())
    if fids:
        from .fab import insert_fiducials
        from .model import KeepoutSpec
        from .pcb_place import FID_HALF

        text, _ = insert_fiducials(out.read_text(), job.board_size_mm, spots=fids)
        out.write_text(text)
        # The router keeps off a fiducial's copper dot, not its 2 mm mask opening: a track
        # through the opening is a solder-mask bridge. A keepout the size of its courtyard.
        for ref, x, y in fids:
            job.keepouts.append(KeepoutSpec(name=f"{ref}_mask", box=(x - FID_HALF, y - FID_HALF, x + FID_HALF, y + FID_HALF), no=("copper", "via")))
    applied = apply_job(job, out, backup=False)
    fails = check_job(job, out)
    report = moves + layout_report(design, job, out.read_text())
    result = {
        "pcb": str(out),
        "applied": applied,
        "check": fails,
        "layout": report,
        "poses": {p.ref: {"at": list(p.at) if p.at else None, "rot": p.rot} for p in job.places},
        "error": None,
    }
    if applied.get("missing"):
        result["error"] = "missing refs: " + ", ".join(applied["missing"])
    if fails:
        result["error"] = result.get("error") or ("check: " + "; ".join(fails))
    return result
