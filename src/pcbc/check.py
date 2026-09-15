"""Verify a KiCad board still matches compiled intent."""

from __future__ import annotations

from pathlib import Path

from dataclasses import replace

from .compile import CompiledJob
from .layout import footprints_by_ref, resolve_place, resolve_regions
from .model import BoardSpec
from .refs import build_alias_index, resolve_ref
from .copper import pad_clearance_failures, sensitive_airwire_failures
from .sexp import board_footprint_spans, footprint_at, footprint_reference


class CheckError(Exception):
    def __init__(self, failures: list[str]):
        self.failures = failures
        super().__init__("\n".join(failures))


def check_job(job: CompiledJob, pcb_path: Path, tol_mm: float = 0.05) -> list[str]:
    """Return a list of failure strings. Empty means pass."""
    pcb_path = Path(pcb_path)
    text = pcb_path.read_text()
    failures: list[str] = []

    by_ref = {}
    for start, end in board_footprint_spans(text):
        block = text[start:end]
        ref = footprint_reference(block)
        if ref:
            by_ref[ref] = block

    board = BoardSpec(
        size_mm=job.board_size_mm,
        padding=job.padding,
        layers=job.layers,
        stackup=job.stackup,
        pcb=job.pcb,
        planes=job.planes,
    )
    regions = resolve_regions(board, job.regions)
    fps = footprints_by_ref(text)
    aliases = build_alias_index(text)

    for place in job.places:
        kref = resolve_ref(place.ref, aliases) or place.ref
        block = by_ref.get(kref)
        if block is None:
            failures.append(f"missing footprint {place.ref}")
            continue
        bound = replace(place, ref=kref)
        want = resolve_place(bound, board, fps.get(kref), regions)
        if want.at is None:
            continue
        at = footprint_at(block)
        if at is None:
            failures.append(f"{place.ref} has no (at …)")
            continue
        dx = abs(at[0] - want.at[0])
        dy = abs(at[1] - want.at[1])
        if dx > tol_mm or dy > tol_mm:
            failures.append(
                f"{place.ref} moved: have ({at[0]:.3f},{at[1]:.3f}) "
                f"want ({want.at[0]:.3f},{want.at[1]:.3f})"
            )
        if abs((at[2] or 0) - want.rot) > 0.5:
            failures.append(
                f"{place.ref} rotated: have {at[2]:g} want {want.rot:g}"
            )
        if place.locked and "(locked yes)" not in block and "locked)" not in block:
            failures.append(f"{place.ref} is not locked")

    for ko in job.keepouts:
        if f'(name "{ko.name}")' not in text:
            failures.append(f"missing keepout zone {ko.name}")

    dru = pcb_path.with_suffix(".kicad_dru")
    if job.dru and not dru.exists():
        failures.append(f"missing {dru.name}")
    failures.extend(sensitive_airwire_failures(job, text))
    floor = 0.10 if job.layers <= 2 else 0.16
    failures.extend(pad_clearance_failures(text, clearance=floor))
    return failures


def check_or_raise(job: CompiledJob, pcb_path: Path) -> None:
    failures = check_job(job, pcb_path)
    if failures:
        raise CheckError(failures)
