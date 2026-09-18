"""check → seed → sch → place → route → fab. No Zener."""

from __future__ import annotations

from pathlib import Path

from .circuit import check_design
from .compile import compile_design
from .fab import fab_job
from .language import load_board
from .netcheck import KicadMissing, check_schematic
from .place import place_job
from .project import layout_dir, packed_reason, seed_pcb
from .route import route_job
from .sch_emit import emit_schematic_file
from .seed import seed_job

STAGES = ("check", "seed", "sch", "place", "route", "fab")


def _done(layout: Path) -> str | None:
    if (layout / "fab" / "bom.csv").exists() and (layout / "fab" / "gerbers").exists():
        if any((layout / "fab" / "gerbers").glob("*")):
            return "fab"
    if (layout / "routed" / "layout.kicad_pcb").exists():
        return "route"
    if (layout / "placed" / "layout.kicad_pcb").exists():
        return "place"
    if (layout / "layout.kicad_pcb").exists():
        return "seed"
    return None


def planned(current: str | None, *, upto: str, force: bool) -> list[str]:
    end = STAGES.index(upto)
    if force or current is None:
        start = 0
    else:
        start = STAGES.index(current) + 1 if current in STAGES else 0
        if current == "seed":
            start = STAGES.index("sch")
        # sch is not tracked as a stage file besides schematic.kicad_sch
        if (current == "seed") and upto != "check":
            pass
    if start > end:
        return []
    return list(STAGES[start : end + 1])


def build_job(
    board: Path,
    *,
    upto: str = "fab",
    force: bool = False,
) -> dict:
    board = Path(board).resolve()
    if upto not in STAGES:
        raise ValueError(f"upto must be one of {STAGES}")
    design = load_board(board)
    layout = layout_dir(board)
    name = board.stem
    current = None if force else _done(layout)
    plan = planned(current, upto=upto, force=force)
    result: dict = {
        "board": str(board),
        "layout": str(layout),
        "plan": plan,
        "steps": [],
        "error": None,
    }
    if not plan:
        result["ok"] = True
        result["skipped"] = current
        return result

    if "check" in plan:
        fails = check_design(design)
        result["steps"].append({"stage": "check", "fails": fails})
        if fails:
            result["error"] = "; ".join(fails)
            return result

    seed = seed_pcb(board)
    if "seed" in plan:
        if packed_reason(seed) and not force:
            result["error"] = packed_reason(seed)
            return result
        step = seed_job(design, seed, name=name)
        result["steps"].append({"stage": "seed", **step})

    if "sch" in plan:
        sch = layout / "schematic.kicad_sch"
        report: dict = {}
        emit_schematic_file(design, sch, title=name, report=report)
        step = {"stage": "sch", "sch": str(sch), "readability": report.get("issues", [])}
        try:
            fails = check_schematic(design, sch)
            step["netlist"] = "verified" if not fails else fails
        except KicadMissing as exc:
            fails = []
            step["netlist"] = f"unchecked: {exc}"
        result["steps"].append(step)
        if fails:
            result["error"] = "schematic netlist differs from board.py: " + "; ".join(fails)
            return result

    placed = layout / "placed" / "layout.kicad_pcb"
    if "place" in plan:
        step = place_job(design, seed, out=placed)
        result["steps"].append({"stage": "place", **step})
        if step.get("error"):
            result["error"] = step["error"]
            return result

    routed = layout / "routed" / "layout.kicad_pcb"
    if "route" in plan:
        src = placed if placed.exists() else seed
        step = route_job(design, src, out=routed, name=name)
        result["steps"].append({"stage": "route", **step})
        if step.get("error"):
            result["error"] = step["error"]
            return result

    if "fab" in plan:
        src = routed if routed.exists() else placed if placed.exists() else seed
        job = compile_design(design)
        step = fab_job(job, src, out_dir=layout / "fab")
        result["steps"].append({"stage": "fab", "fab": step.get("fab"), "error": step.get("error")})
        if step.get("error"):
            result["error"] = step["error"]
            return result

    result["ok"] = True
    return result
