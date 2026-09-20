"""Copper by KiCadRoutingTools (KRT). The AI never draws a track.

The route stage compiles `NetReq` intent into an ordered KRT plan and runs it:

1. constrained nets first (`vias=False`, or a single layer): each group on its layers
   only, while the board is empty, so a switch node or an analog trace gets the straight shot;
2. differential pairs (`pair=True`) as coupled pairs;
3. on four layers the declared `planes=` pours, with via taps;
4. everything else, power nets at the width their amps ask for;
5. on two layers a GND pour on the back, then one more pass to tie what the pour missed.

`netcheck.check_copper` then asks KiCad itself (DRC, unconnected items) and diffs the copper
netlist against `board.py`; the fab stage checks vias on no-via nets and ampacity. KRT's
geometry is bit-identical run to run; only the ids it invents differ, so they are re-keyed
here and `pcbc build` stays byte-for-byte. KRT never rewrites the board's design rules.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

from .compile import CompiledJob, compile_design
from .copper import unrouted_nets
from .model import Design
from .project import copy_with_siblings
from .sexp import matching_paren, stable_uuid

KRT_REPO = "https://github.com/drandyhaas/KiCadRoutingTools.git"
KRT_SHA = "3244726b2c15668fb109a0bb24384750a054af40"

_UUID = re.compile(r'\(uuid\s+"[^"]*"\)')
_COPPER_ITEM = re.compile(r"\n\t\((segment|via|zone|arc)\b")


def krt_home() -> Path:
    return Path(os.environ.get("KRT_HOME") or (Path.home() / "Downloads" / "KiCadRoutingTools"))


def krt_python(home: Path) -> Path:
    venv = home / ".venv" / "bin" / "python"
    return venv if venv.exists() else Path(sys.executable)


def krt_missing(home: Path | None = None) -> str | None:
    """Why KRT cannot run, with the fix; None when it can."""
    home = home or krt_home()
    if (home / "py_router" / "route.py").exists():
        return None
    return (
        f"KiCadRoutingTools not found at {home} (set KRT_HOME, or): "
        f"git clone {KRT_REPO} {home} && cd {home} && git checkout {KRT_SHA} && "
        f"python3 -m venv .venv && .venv/bin/pip install -r requirements.txt && .venv/bin/python build_router.py"
    )


def krt_version(home: Path) -> str:
    v = home / "VERSION"
    return v.read_text().strip() if v.exists() else "?"


def copper_layers(n: int) -> list[str]:
    inner = [f"In{i}.Cu" for i in range(1, max(n, 2) - 1)]
    return ["F.Cu", *inner, "B.Cu"]


def pin_copper_ids(text: str, board: str) -> str:
    """Re-key every segment / via / zone uuid by its order on the sheet."""
    out: list[str] = []
    pos = 0
    counts: dict[str, int] = {}
    while True:
        m = _COPPER_ITEM.search(text, pos)
        if not m:
            out.append(text[pos:])
            break
        tag = m.group(1)
        open_at = m.start() + 2
        end = matching_paren(text, open_at)
        block = text[open_at : end + 1]
        i = counts.get(tag, 0)
        counts[tag] = i + 1
        block = _UUID.sub(f'(uuid "{stable_uuid(board, tag, i)}")', block, count=1)
        out.append(text[pos:open_at])
        out.append(block)
        pos = end + 1
    return "".join(out)


# ---------------------------------------------------------------- the plan


def _class(job: CompiledJob, name: str):
    return next((c for c in job.classes if c.name == name), None)


def _fmt(v: float) -> str:
    return f"{v:g}"


def _net_names(design: Design, patterns) -> list[str]:
    from fnmatch import fnmatch

    return sorted(n for n in design.nets if any(fnmatch(n, p) for p in patterns))


def krt_plan(job: CompiledJob, design: Design, placed: Path, work: Path, home: Path) -> list[tuple[str, list[str]]]:
    """(step name, command) pairs. Pure: the same board.py gives the same plan."""
    py = str(krt_python(home))
    router = home / "py_router"
    from .stackup import get_stackup

    stack = get_stackup(job.stackup)
    layers = copper_layers(job.layers)
    floor = stack.clearance_min
    grid = "0.05" if job.layers <= 2 else "0.1"
    via = ["--via-size", _fmt(stack.via_diameter), "--via-drill", _fmt(stack.via_drill)]
    steps: list[tuple[str, list[str]]] = []
    prev = placed

    # The fab's floor, pinned: KRT otherwise escalates to smaller "advanced" vias at fine-pitch
    # pads and the board's own constraints then reject them. Classes already clear a via's hole.
    overrides = work / "fab_overrides.txt"
    # `--clearance` is a CEILING on every class (min(class, value)); the classes carry the numbers, so
    # steps only name it where one class is meant.
    default = _class(job, "Default")
    default_clear = default.clearance_mm if default else floor

    def step(name: str, tool: str, args: list[str]) -> None:
        nonlocal prev
        out = work / f"{len(steps) + 1:02d}_{name}.kicad_pcb"
        common = ["--no-fix-drc-settings", "--same-net-pad-clearance", _fmt(floor), "--grid-step", grid, *via, "--fab-overrides", str(overrides)]
        if tool != "route_planes.py":
            common.append("--keep-input-copper")  # planes keep it regardless
        steps.append((name, [py, "-X", "utf8", str(router / tool), str(prev), str(out), *args, *common]))
        prev = out

    # 1. Constrained nets: no vias, or a single layer. Grouped by (layers, class), named nets only.
    groups: dict[tuple[tuple[str, ...], str], list[str]] = {}
    for cn in job.nets:
        if cn.autoroute == "diff_pair":
            continue
        if cn.vias is False or len(cn.layers) == 1:
            names = _net_names(design, cn.patterns)
            if names:
                lays = tuple(cn.layers[:1]) if cn.vias is False else tuple(cn.layers)
                groups.setdefault((lays, cn.class_name), []).extend(names)
    for (lays, cls_name), names in sorted(groups.items()):
        cls = _class(job, cls_name)
        step(
            f"{cls_name.lower()}_nets",
            "route.py",
            ["--nets", *sorted(set(names)), "--layers", *lays, "--track-width", _fmt(cls.track_width_mm if cls else floor),
             "--clearance", _fmt(cls.clearance_mm if cls else default_clear), "--via-cost", "100000", "--max-ripup", "5"],
        )

    # 2. Differential pairs.
    for cn in job.nets:
        if cn.autoroute != "diff_pair":
            continue
        cls = _class(job, cn.class_name)
        names = _net_names(design, cn.patterns)
        if len(names) != 2:
            continue
        width = (cls.diff_pair_width_mm or cls.track_width_mm) if cls else floor
        gap = max((cls.diff_pair_gap_mm or floor) if cls else floor, (cls.clearance_mm if cls else floor))
        args = ["--nets", *names, "--track-width", _fmt(width), "--diff-pair-gap", _fmt(gap), "--clearance", _fmt(cls.clearance_mm if cls else default_clear),
                "--layers", *layers, "--diff-pair-intra-match"]
        if job.layers >= 4:
            args += ["--impedance", "90"]
        step(f"pair_{names[0].lower()}", "route_diff.py", args)

    # 3. Planes first on four layers: the pours, with via taps.
    if job.planes and job.layers > 2:
        step("planes", "route_planes.py", ["--nets", *[n for n, _ in job.planes], "--plane-layers", *[layer for _, layer in job.planes], "--clearance", _fmt(default_clear)])

    # 4. Everything else. Power nets at their width.
    power: list[str] = []
    widths: list[str] = []
    for cn in job.nets:
        if cn.kind == "power":
            cls = _class(job, cn.class_name)
            for n in _net_names(design, cn.patterns):
                if n not in power:
                    power.append(n)
                    widths.append(_fmt(cls.track_width_mm if cls else 0.4))
    signals = ["--nets", "*", "--layers", *layers, "--track-width", _fmt(stack.track_min), "--max-ripup", "5", "--no-bga-zones"]
    if power:
        signals += ["--power-nets", *power, "--power-nets-widths", *widths]
    step("signals", "route.py", signals)

    # 5. Two layers: pour GND on the back and tie what the pour could not reach.
    if job.layers <= 2 and "GND" in power:
        step("gnd_pour", "route_planes.py", ["--nets", "GND", "--plane-layers", "B.Cu", "--clearance", _fmt(default_clear)])
        step("finalize", "route.py", signals)
    return steps


def write_fab_overrides(job: CompiledJob, work: Path) -> Path:
    """The pinned fab rung every KRT step reads (see krt_plan)."""
    from .stackup import get_stackup

    s = get_stackup(job.stackup)
    path = work / "fab_overrides.txt"
    path.write_text(
        "# pcbc: the fab's floor for this stackup; KRT must not escalate past it\n"
        f"via_diameter = {s.via_diameter:g}\nvia_drill = {s.via_drill:g}\nhole_to_hole = {s.hole_to_hole:g}\n"
        f"clearance = {s.clearance_min:g}\ntrack_width = {s.track_min:g}\nboard_edge = {s.edge_clearance:g}\n"
    )
    return path


def route_job(design: Design, placed: Path, *, out: Path, name: str = "board") -> dict:
    placed = Path(placed)
    out = Path(out)
    copy_with_siblings(placed, out)
    # A killed build leaves step files behind; KRT reads a step's sibling .kicad_pro, so a stale
    # one from another run steers the next step. Every route starts from a clean work dir.
    for stale in sorted(out.parent.glob("[0-9][0-9]_*.kicad_*")):
        stale.unlink()
    home = krt_home()
    reason = krt_missing(home)
    if reason:
        return {"pcb": str(out), "router": None, "error": reason}
    job = compile_design(design)
    work = out.parent
    write_fab_overrides(job, work)
    steps = krt_plan(job, design, placed, work, home)
    result: dict = {
        "pcb": str(out),
        "router": "krt",
        "krt": {"home": str(home), "version": krt_version(home)},
        "plan": [{"step": n, "cmd": cmd} for n, cmd in steps],
        "steps": [],
        "error": None,
    }
    last: Path | None = None
    for step_name, cmd in steps:
        proc = subprocess.run(cmd, capture_output=True, text=True, cwd=str(home))
        log = ((proc.stdout or "") + (proc.stderr or ""))[-3000:]
        produced = Path(cmd[5])
        result["steps"].append({"step": step_name, "returncode": proc.returncode, "log": log[-1200:]})
        if proc.returncode != 0 or not produced.exists():
            result["error"] = f"KRT {step_name} failed ({proc.returncode}): {log.strip()[-600:]}"
            return result
        last = produced
    if last is None:
        result["error"] = "nothing to route"
        return result
    text = pin_copper_ids(last.read_text(), name)
    out.write_text(text)
    opens = unrouted_nets(text)
    result["segments"] = len(re.findall(r"\n\t\(segment\b", text))
    result["vias"] = len(re.findall(r"\n\t\(via\b", text))
    result["zones"] = len(re.findall(r"\n\t\(zone\b", text))
    result["unrouted"] = opens
    if opens:
        result["error"] = "unrouted " + ", ".join(opens) + " - the router found no path; move the parts on those nets closer or give them a free side"
    return result
