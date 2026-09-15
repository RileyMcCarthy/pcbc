"""Project paths. Output is always layout/<stem>/ — Board() does not choose a path."""

from __future__ import annotations

import shutil
from pathlib import Path

PACKED = frozenset({"placed", "routed", "fab"})
SIBLINGS = (".kicad_pro", ".kicad_prl", ".kicad_dru")


def layout_dir(board: Path) -> Path:
    board = Path(board).resolve()
    return board.parent / "layout" / board.stem


def seed_pcb(board: Path) -> Path:
    return layout_dir(board) / "layout.kicad_pcb"


def packed_reason(pcb: Path) -> str | None:
    pcb = Path(pcb)
    for parent in pcb.parents:
        if parent.name in PACKED:
            return f"{pcb.name} is under {parent.name}/; rebuild with --force"
    return None


def copy_with_siblings(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    sbase = str(src)[: -len(".kicad_pcb")]
    dbase = str(dst)[: -len(".kicad_pcb")]
    for ext in SIBLINGS:
        s = Path(sbase + ext)
        if s.exists():
            shutil.copy2(s, dbase + ext)
