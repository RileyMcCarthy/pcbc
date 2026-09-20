"""check → seed → sch → place → route → fab. No Zener."""

from __future__ import annotations

from pathlib import Path

from .circuit import check_design
from .compile import CompiledJob, compile_design
from .fab import fab_job
from .language import load_board
from .netcheck import KicadMissing, check_copper, check_erc, check_schematic
from .place import place_job
from .project import layout_dir, packed_reason, seed_pcb
from .route import route_job
from .sch_emit import emit_schematic_file
from .seed import seed_job

STAGES = ("check", "seed", "sch", "place", "route", "fab")


def rules_summary(job: CompiledJob) -> dict:
    """What `job.dru` holds, for the one report line `constraints.py` cannot print itself (it sits
    below `dru.py`, so it never sees the rules): every rule pcbc wrote, how many are errors, how many
    are warnings (the soft kinds of R1, the two geometry rules and the canary: none fails the gate),
    and the net the canary rule watches."""
    cs = job.constraints
    return {
        "written": len(job.dru),
        "error": sum(1 for r in job.dru if r.severity == "error"),
        "soft": sum(1 for r in job.dru if r.severity == "warning"),
        "canary_net": cs.canary_net if cs is not None else None,
    }


def rules_line(summary: dict) -> str:
    canary = f"canary on net {summary['canary_net']}" if summary.get("canary_net") else "no canary (no net has two pads)"
    return f"rules: {summary['written']} written ({summary['error']} error, {summary['soft']} soft), {canary}"


def constraint_lines(job: CompiledJob) -> list[str]:
    """`pcbc check --constraints`: one number per line with its source (`cs.lines`, sorted by net,
    ending in the class summary), then the rules line."""
    cs = job.constraints
    if cs is None:
        return []
    return list(cs.lines) + [rules_line(rules_summary(job))]


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


def pcb_job(board: Path) -> dict:
    """The copper inner loop: check, seed, place; the layout report as moves. No schematic, no routing."""
    from .pcb_place import validate

    board = Path(board).resolve()
    design = load_board(board)
    layout = layout_dir(board)
    name = board.stem
    result: dict = {"board": str(board), "layout": str(layout), "error": None}
    fails = check_design(design, pcb=True) + validate(design)
    result["check"] = fails
    if fails:
        result["error"] = "; ".join(fails)
        return result
    job = compile_design(design)
    result["constraints"] = job.constraints.to_dict() if job.constraints is not None else None
    result["rules"] = rules_summary(job)
    seed = seed_pcb(board)
    result["seed"] = seed_job(design, seed, name=name)
    placed = layout / "placed" / "layout.kicad_pcb"
    step = place_job(design, seed, out=placed)
    result["placed"] = str(placed)
    result["layout_report"] = step.get("layout", [])
    result["layout_notes"] = step.get("notes", [])
    result["poses"] = step.get("poses", {})
    result["applied"] = step.get("applied")
    if step.get("error"):
        result["error"] = step["error"]
    return result


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
        step = {"stage": "check", "fails": fails}
        if not fails:
            step["constraints"] = constraint_lines(compile_design(design))
        result["steps"].append(step)
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
        try:
            emit_schematic_file(design, sch, title=name, report=report)
        except ValueError as exc:
            result["steps"].append({"stage": "sch", "error": str(exc)})
            result["error"] = f"schematic: {exc}"
            return result
        step = {"stage": "sch", "sch": str(sch), "readability": report.get("issues", [])}
        try:
            fails = check_schematic(design, sch)
            step["netlist"] = "verified" if not fails else fails
            erc = check_erc(sch)
            step["erc"] = "clean" if not erc["errors"] else erc["errors"]
            step["erc_warnings"] = erc["warnings"]
        except KicadMissing as exc:
            fails = []
            erc = {"errors": []}
            step["netlist"] = step["erc"] = f"unchecked: {exc}"
        result["steps"].append(step)
        if fails:
            result["error"] = "schematic netlist differs from board.py: " + "; ".join(fails)
            return result
        if erc["errors"]:
            result["error"] = "schematic fails KiCad ERC: " + "; ".join(erc["errors"])
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
        entry = {"stage": "route", **step}
        if step.get("error"):
            result["steps"].append(entry)
            result["error"] = step["error"]
            return result
        try:
            gate = check_copper(design, routed)
            entry["copper"] = "verified" if gate["ok"] else gate["fails"]
            entry["drc_warnings"] = gate["drc_warnings"]
            entry["geometry"] = gate.get("geometry")
            # R1 (docs/r1-design.md section E, S3): `check_copper` gains "soft" {rule_name: hits} for the
            # warnings of pcbc's soft rules and "rules" {rule_name: hits} for every pcbc rule. Carried as
            # given; None until netcheck provides them.
            entry["soft"] = gate.get("soft")
            entry["rules"] = gate.get("rules")
            from .route_emit import read_sidecar
            from .sexp import pin_all_uuids

            # KiCad's save invented ids. pcbc's own copper keeps the ids the sidecar names, so a
            # piece stays traceable from `copper.json` into the board and into KiCad's UI (D.4).
            side = routed.parent / "copper.json"
            mine = frozenset(i["uuid"] for i in read_sidecar(side).items) if side.exists() else frozenset()
            routed.write_text(pin_all_uuids(routed.read_text(), name, "routed", keep=mine))
        except KicadMissing as exc:
            gate = {"ok": True, "fails": []}
            entry["copper"] = f"unchecked: {exc}"
        result["steps"].append(entry)
        if not gate["ok"]:
            result["error"] = "copper is not board.py's netlist, or fails KiCad DRC: " + "; ".join(gate["fails"])
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
