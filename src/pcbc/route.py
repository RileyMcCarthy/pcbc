"""Copper by KiCadRoutingTools (KRT). The AI never draws a track.

The tool routes every net of the placed board; `netcheck.check_copper` then asks
KiCad itself (DRC, unconnected items) and diffs the copper netlist against
`board.py`. KRT's geometry is bit-identical run to run; only the ids it invents
differ, so they are re-keyed here and `pcbc build` stays byte-for-byte.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

from .compile import compile_design
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


def _default_class(design: Design) -> tuple[float, float]:
    job = compile_design(design)
    for cls in job.classes:
        if cls.name == "Default":
            return float(cls.track_width_mm), float(cls.clearance_mm)
    return 0.2, 0.2


def route_job(design: Design, placed: Path, *, out: Path, name: str = "board") -> dict:
    placed = Path(placed)
    out = Path(out)
    copy_with_siblings(placed, out)
    home = krt_home()
    reason = krt_missing(home)
    if reason:
        return {"pcb": str(out), "router": None, "error": reason}
    job = compile_design(design)
    width, clearance = _default_class(design)
    cmd = [
        str(krt_python(home)),
        "-X",
        "utf8",
        str(home / "py_router" / "route.py"),
        str(placed),
        str(out),
        "--layers",
        *copper_layers(job.layers),
        "--track-width",
        f"{width:g}",
        "--clearance",
        f"{clearance:g}",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, cwd=str(home))
    log = ((proc.stdout or "") + (proc.stderr or ""))[-3000:]
    result: dict = {
        "pcb": str(out),
        "router": "krt",
        "krt": {"home": str(home), "version": krt_version(home), "cmd": cmd},
        "log": log,
        "error": None,
    }
    if proc.returncode != 0 or not out.exists():
        result["error"] = f"KRT route.py failed ({proc.returncode}): {log.strip()[-600:]}"
        return result
    text = pin_copper_ids(out.read_text(), name)
    out.write_text(text)
    opens = unrouted_nets(text)
    result["segments"] = len(re.findall(r"\n\t\(segment\b", text))
    result["vias"] = len(re.findall(r"\n\t\(via\b", text))
    result["unrouted"] = opens
    if opens:
        result["error"] = "unrouted " + ", ".join(opens)
    return result
