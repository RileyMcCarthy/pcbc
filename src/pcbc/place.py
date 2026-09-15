"""Apply locked CSS poses. Unlocked parts need KRT (not in the blinky path)."""

from __future__ import annotations

from pathlib import Path

from .apply import apply_job
from .check import check_job
from .compile import compile_design
from .model import Design
from .project import copy_with_siblings


def place_job(design: Design, seed: Path, *, out: Path) -> dict:
    job = compile_design(design)
    seed = Path(seed)
    out = Path(out)
    unlocked = [p.ref for p in design.places if not p.locked]
    copy_with_siblings(seed, out)
    applied = apply_job(job, out, backup=False)
    fails = check_job(job, out)
    result = {
        "pcb": str(out),
        "applied": applied,
        "check": fails,
        "error": None,
    }
    if unlocked:
        result["error"] = (
            "unlocked Place() needs KRT place_seed (not used for blinky). "
            f"lock: {', '.join(unlocked)}"
        )
        return result
    if applied.get("missing"):
        result["error"] = "missing refs: " + ", ".join(applied["missing"])
    if fails:
        result["error"] = result.get("error") or ("check: " + "; ".join(fails))
    return result
