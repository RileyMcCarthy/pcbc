"""Project paths. Output is always layout/<stem>/ — Board() does not choose a path."""

from __future__ import annotations

import shutil
from pathlib import Path

PACKED = frozenset({"placed", "routed", "fab"})
SIBLINGS = (".kicad_pro", ".kicad_prl", ".kicad_dru")


def layout_dir(board: Path) -> Path:
    board = Path(board).resolve()
    return board.parent / "layout" / board.stem


def packed_reason(pcb: Path) -> str | None:
    pcb = Path(pcb)
    for parent in pcb.parents:
        if parent.name in PACKED:
            return f"{pcb.name} is under {parent.name}/; rebuild with --force"
    return None


def copy_with_siblings(src: Path, dst: Path) -> None:
    """Copy a board **and** whatever `.kicad_pro`/`.kicad_prl`/`.kicad_dru` sit beside it: for a caller
    that wants a faithful copy of a board it did not make (fab's working copy). Every board the build
    writes gets its sidecars from `write_sidecars` instead: regenerated from the compiled job, never
    copied forward (fourth review, C1)."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    sbase = str(src)[: -len(".kicad_pcb")]
    dbase = str(dst)[: -len(".kicad_pcb")]
    for ext in SIBLINGS:
        s = Path(sbase + ext)
        if s.exists():
            shutil.copy2(s, dbase + ext)


def copy_board(src: Path, dst: Path) -> None:
    """Copy the `.kicad_pcb` alone. The sidecars beside `dst` are `write_sidecars`' to write."""
    dst = Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)


def write_sidecars(design, job, pcb: Path, *, name: str) -> dict[str, str]:
    """The `.kicad_pro` and `.kicad_dru` beside `pcb`, **regenerated from the compiled job**, and any
    `.kicad_prl` there removed.

    Every reader of a board the build wrote — `kicad-cli pcb drc` (`.kicad_pro` rules and severities,
    `.kicad_dru` custom rules), fab — used to read a sidecar copied forward from the stage before,
    which nothing stamped: a hand edit of `placed/layout.kicad_pro` (clearances 0.02, every severity
    `ignore`) silenced the arbiter and shipped in the fab package while the build said `verified`
    (fourth review, C1). Now the sidecar a stage hands its reader is written here, from `board.py`'s
    compiled job, at the moment the board is written; the copy on disk is also stamped
    (`build.stale_reason`), so an edit is stale from the stage that wrote it. `emit_pro` and
    `write_dru` are the same two writers the seed and place stages always used, so an honest build's
    bytes are unchanged (measured: seed, placed, routed and fab `.kicad_pro` were already byte-equal).

    A `.kicad_prl` is KiCad's own, written beside a board when `kicad-cli` saves it; one lying beside
    a board pcbc is writing fresh is from an earlier save of another board and is unlinked.
    """
    from .apply import write_dru
    from .seed import emit_pro

    pcb = Path(pcb)
    pro = pcb.with_suffix(".kicad_pro")
    pro.write_text(emit_pro(design, name=name))
    dru = write_dru(job, pcb)
    prl = pcb.with_suffix(".kicad_prl")
    if prl.exists():
        prl.unlink()
    return {"pro": str(pro), "dru": str(dru)}


def sidecar_files(pcb: Path) -> dict[str, Path]:
    """Every sidecar present beside `pcb`, keyed by its file name: what a stamp records."""
    pcb = Path(pcb)
    out = {}
    for ext in SIBLINGS:
        p = pcb.with_suffix(ext)
        if p.exists():
            out[p.name] = p
    return out
