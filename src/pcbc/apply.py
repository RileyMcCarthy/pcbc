"""Compiled geometry onto footprint blocks in memory (poses), and the compiled rules onto the sidecars.

The outline, keepouts, rule areas and slots are layout objects now (`place_native.place_objects`);
nothing here writes a board file."""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

from dataclasses import replace

from . import dru as _dru
from .compile import CompiledJob
from .layout import footprints_by_ref, resolve_place, resolve_regions
from .model import BoardSpec, PlaceSpec
from .refs import build_alias_index, resolve_ref
from .sexp import (
    board_footprint_spans,
    footprint_at,
    footprint_reference,
    has_edge_cuts_shape,
    matching_paren,
    stable_uuid,
)


def _apply_places(text: str, job: CompiledJob) -> tuple[str, list[str], list]:
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
    resolved_places = []
    by_kref: dict[str, PlaceSpec] = {}
    found_aliases: set[str] = set()
    for p in job.places:
        kref = resolve_ref(p.ref, aliases) or p.ref
        block = fps.get(kref)
        bound = replace(p, ref=kref)
        rp = resolve_place(bound, board, block, regions)
        resolved_places.append(rp)
        by_kref[kref] = rp
        if kref in fps:
            found_aliases.add(p.ref)
    spans = board_footprint_spans(text)
    pieces = []
    last = 0
    for start, end in spans:
        block = text[start:end]
        ref = footprint_reference(block)
        if ref and ref in by_kref:
            place = by_kref[ref]
            if place.at is not None:
                block = _rewrite_footprint(block, place)
        pieces.append(text[last:start])
        pieces.append(block)
        last = end
    pieces.append(text[last:])
    missing = [p.ref for p in job.places if p.ref not in found_aliases]
    return "".join(pieces), missing, resolved_places


_PAD_AT = re.compile(r"(\(pad\s+\"[^\"]*\"\s+\w+\s+\w+\s*\(at\s+[0-9.+-]+\s+[0-9.+-]+)(?:\s+([0-9.+-]+))?\)")


def _turn_pads(block: str, delta: float) -> str:
    """KiCad stores a pad's angle as footprint angle + pad angle (a board-file quirk, unlike the
    library file); turning the footprint must turn every pad's angle with it, or the pads
    render unrotated - the ESP32 module's 0.8 mm pitch pads then touch."""
    if abs(delta) < 1e-9:
        return block

    def fix(m: re.Match) -> str:
        a = (float(m.group(2) or 0) + delta) % 360
        return f"{m.group(1)}{'' if abs(a) < 1e-9 else f' {a:g}'})"

    return _PAD_AT.sub(fix, block)


def _rewrite_footprint(block: str, place) -> str:
    if place.at is None:
        return block
    layer = "F.Cu" if place.side == "F" else "B.Cu"
    was = footprint_at(block)
    block = _turn_pads(block, float(place.rot) - (was[2] if was else 0.0))
    at = f"(at {place.at[0]:.4f} {place.at[1]:.4f} {place.rot:g})"
    block, n = re.subn(
        r"\n\t\t\(at [0-9.+-]+ [0-9.+-]+(?: [0-9.+-]+)?\)",
        f"\n\t\t{at}",
        block,
        count=1,
    )
    if n == 0:
        block = re.sub(r"\(at [0-9.+-]+ [0-9.+-]+(?: [0-9.+-]+)?\)", at, block, count=1)
    block = re.sub(
        r'\(layer "(F|B)\.Cu"\)',
        f'(layer "{layer}")',
        block,
        count=1,
    )
    if place.locked:
        if re.search(r"\(locked\s+", block) is None:
            block = re.sub(
                r"(\n\t\t\(at [^\n]+\n)",
                r"\1\t\t(locked yes)\n",
                block,
                count=1,
            )
        else:
            block = re.sub(r"\(locked\s+(yes|no)\)", "(locked yes)", block, count=1)
        # KiCad 10 attr tokens are smd/through_hole/virtual/… only.
        # Lock is a sibling (locked yes), not an attr flag.
    return block


def write_dru(job: CompiledJob, pcb_path: Path) -> Path:
    """Write compiled custom rules (.kicad_dru). Does not touch .kicad_pro.

    `job.dru` was computed once at compile (`dru.rules`); this only validates and renders it. A
    rule KiCad would choke on raises here, before the file exists, because one malformed rule
    silently disables every rule and kicad-cli says nothing.

    """
    errs = _dru.validate(job.dru)
    if errs:
        raise ValueError("refusing to write .kicad_dru (one bad rule disables every rule in KiCad): " + "; ".join(errs))
    path = Path(pcb_path).with_suffix(".kicad_dru")
    path.write_text(_dru.render(job.dru))
    return path


def _apply_pro(pro_path: Path, job: CompiledJob) -> None:
    """Merge pcbc's class rows (`dru.project_classes`, the one writer seed and apply share) into the
    project file, keeping what KiCad added to an existing row (colours, priority)."""
    pro = json.loads(pro_path.read_text())
    ns = pro.setdefault("net_settings", {})
    existing = {c.get("name"): c for c in ns.get("classes", []) if c.get("name")}
    cs = job.constraints if job.constraints is not None else job
    for want in _dru.project_classes(cs):
        row = existing.get(
            want["name"],
            {
                "name": want["name"],
                "pcb_color": "rgba(0, 0, 0, 0.000)",
                "schematic_color": "rgba(0, 0, 0, 0.000)",
                "priority": 8,
                "bus_width": 12,
                "wire_width": 6,
                "line_style": 0,
                "tuning_profile": "",
            },
        )
        for key, value in want.items():
            if key != "name":
                row[key] = value
        existing[want["name"]] = row
    classes = [existing[n] for n in _dru.PROJECT_CLASS_ORDER if n in existing]
    for name, row in existing.items():
        if name not in _dru.PROJECT_CLASS_ORDER:
            classes.append(row)
    ns["classes"] = classes
    ns["netclass_patterns"] = _dru.netclass_patterns(cs)
    pro_path.write_text(json.dumps(pro, indent=2) + "\n")


def _render_dru(job: CompiledJob) -> str:
    """Today's name for `dru.render(job.dru)`, kept for its importers."""
    return _dru.render(job.dru)


SLOT_WIDTH_MM = 1.0  # IEC 60664-1 groove rule at PD2: a slot narrower than 1 mm does not count as a creepage path
SLOT_WEB_MM = 3.0  # board left at each end of the slot so the two sides stay one board
