"""Emit a KiCad 10 seed board from a Design. No Zener, no default.net."""

from __future__ import annotations

import json
import re
from pathlib import Path

from .compile import compile_design
from .footprints import footprint_path
from .model import Design, Instance
from .sexp import matching_paren, stable_uuid

_PROP_VAL = re.compile(r'(\(property "([^"]+)" )"[^"]*"')


def pad_nets(inst: Instance) -> dict[str, str]:
    """Pad number → net name, from the symbol pin map + instance bindings."""
    out: dict[str, str] = {}
    for pname, net in inst.pins.items():
        pin = inst.part.pins.get(pname)
        if not pin:
            continue
        for pad in pin.pads:
            out[str(pad)] = net
    return out


def _set_prop(block: str, name: str, value: str) -> str:
    pat = rf'(\(property "{re.escape(name)}" )"[^"]*"'
    new, n = re.subn(pat, rf'\1"{value}"', block, count=1)
    if n:
        return new
    prop = (
        f'\t(property "{name}" "{value}"\n'
        f'\t\t(at 0 0 0)\n'
        f'\t\t(layer "F.Fab")\n'
        f"\t\t(hide yes)\n"
        f'\t\t(uuid "{stable_uuid("prop", name, value)}")\n'
        f"\t\t(effects (font (size 1.27 1.27)))\n"
        f"\t)\n"
    )
    attr = block.find("\t(attr ")
    if attr < 0:
        return block + prop
    return block[:attr] + prop + block[attr:]


def instantiate(
    inst: Instance,
    *,
    board: str,
    at: tuple[float, float],
    rot: float = 0.0,
) -> str:
    mod = footprint_path(inst.part).read_text()
    j = mod.find("(footprint ")
    if j < 0:
        raise ValueError(f"{inst.ref}: not a kicad_mod")
    end = matching_paren(mod, j)
    body = mod[j : end + 1]
    stem = Path(footprint_path(inst.part)).stem
    lib_id = f"pcbc:{stem}"
    body = re.sub(r'^\(footprint "[^"]*"', f'(footprint "{lib_id}"', body, count=1)
    uid = stable_uuid(board, inst.ref)
    head = (
        f'(footprint "{lib_id}"\n'
        f'\t(layer "F.Cu")\n'
        f'\t(uuid "{uid}")\n'
        f"\t(at {at[0]:.4f} {at[1]:.4f} {rot:g})\n"
    )
    # Drop the original (footprint … (layer …) and keep the rest.
    rest_start = body.find("\n", body.find("(layer "))
    rest = body[rest_start + 1 :] if rest_start >= 0 else body
    # rest still contains inner content starting at descr/tags/property
    block = head + rest
    if not block.endswith(")"):
        block = block.rstrip() + "\n)"
    value = inst.value or inst.part.value or inst.part.mpn
    block = _set_prop(block, "Reference", inst.ref)
    block = _set_prop(block, "Value", value)
    if inst.part.mpn:
        block = _set_prop(block, "Mpn", inst.part.mpn)
    if inst.part.lcsc:
        block = _set_prop(block, "LCSC", inst.part.lcsc)
    if inst.part.manufacturer:
        block = _set_prop(block, "Manufacturer", inst.part.manufacturer)
    nets = pad_nets(inst)
    block = _bind_pads(block, nets, board=board, ref=inst.ref)
    return "\t" + block.replace("\n", "\n\t") + "\n"


def _bind_pads(block: str, nets: dict[str, str], *, board: str, ref: str) -> str:
    out: list[str] = []
    last = 0
    pos = 0
    while True:
        j = block.find("(pad ", pos)
        if j < 0:
            out.append(block[last:])
            break
        end = matching_paren(block, j)
        pad = block[j : end + 1]
        m = re.match(r'\(pad "([^"]*)"', pad)
        num = m.group(1) if m else ""
        net = nets.get(num, "")
        if net and '(net "' not in pad:
            insert = (
                f'\n\t\t(net "{net}")'
                f'\n\t\t(uuid "{stable_uuid(board, ref, "pad", num)}")'
            )
            pad = pad[:-1] + insert + "\n\t)"
        out.append(block[last:j])
        out.append(pad)
        last = end + 1
        pos = end + 1
    return "".join(out)


def _layers(n: int) -> str:
    coppers = (
        '\t\t(0 "F.Cu" mixed)\n'
        '\t\t(2 "B.Cu" mixed)\n'
        if n <= 2
        else (
            '\t\t(0 "F.Cu" mixed)\n'
            '\t\t(4 "In1.Cu" power)\n'
            '\t\t(6 "In2.Cu" power)\n'
            '\t\t(2 "B.Cu" mixed)\n'
        )
    )
    return (
        "\t(layers\n"
        + coppers
        + '\t\t(9 "F.Adhes" user "F.Adhesive")\n'
        '\t\t(11 "B.Adhes" user "B.Adhesive")\n'
        '\t\t(13 "F.Paste" user)\n'
        '\t\t(15 "B.Paste" user)\n'
        '\t\t(5 "F.SilkS" user "F.Silkscreen")\n'
        '\t\t(7 "B.SilkS" user "B.Silkscreen")\n'
        '\t\t(1 "F.Mask" user)\n'
        '\t\t(3 "B.Mask" user)\n'
        '\t\t(17 "Dwgs.User" user "User.Drawings")\n'
        '\t\t(19 "Cmts.User" user "User.Comments")\n'
        '\t\t(21 "Eco1.User" user "User.Eco1")\n'
        '\t\t(23 "Eco2.User" user "User.Eco2")\n'
        '\t\t(25 "Edge.Cuts" user)\n'
        '\t\t(27 "Margin" user)\n'
        '\t\t(31 "F.CrtYd" user "F.Courtyard")\n'
        '\t\t(29 "B.CrtYd" user "B.Courtyard")\n'
        '\t\t(35 "F.Fab" user)\n'
        '\t\t(33 "B.Fab" user)\n'
        "\t)\n"
    )


def _nets(design: Design) -> str:
    names = [""] + sorted(design.nets)
    lines = [f'\t(net {i} "{n}")\n' for i, n in enumerate(names)]
    return "".join(lines)


def _outline(w: float, h: float, board: str) -> str:
    return (
        f"\t(gr_rect\n"
        f"\t\t(start 0 0)\n"
        f"\t\t(end {w:g} {h:g})\n"
        f"\t\t(stroke (width 0.05) (type default))\n"
        f"\t\t(fill none)\n"
        f'\t\t(layer "Edge.Cuts")\n'
        f'\t\t(uuid "{stable_uuid(board, "edge")}")\n'
        f"\t)\n"
    )


def emit_pcb(design: Design, *, name: str) -> str:
    if design.board is None:
        raise ValueError("no Board()")
    w, h = design.board.size_mm
    fps: list[str] = []
    cols = max(int(w // 8), 1)
    for i, inst in enumerate(design.instances):
        gx = 5.0 + (i % cols) * 8.0
        gy = 5.0 + (i // cols) * 8.0
        fps.append(instantiate(inst, board=name, at=(gx, gy)))
    layers_n = design.board.layers
    return (
        "(kicad_pcb\n"
        "\t(version 20260206)\n"
        '\t(generator "pcbc")\n'
        '\t(generator_version "0.1")\n'
        "\t(general\n"
        "\t\t(thickness 1.6)\n"
        "\t)\n"
        '\t(paper "A4")\n'
        "\t(title_block\n"
        f'\t\t(title "{name}")\n'
        "\t)\n"
        + _layers(layers_n)
        + "\t(setup\n"
        "\t\t(pad_to_mask_clearance 0)\n"
        "\t\t(allow_soldermask_bridges_in_footprints no)\n"
        "\t)\n"
        + _nets(design)
        + "".join(fps)
        + _outline(w, h, name)
        + ")\n"
    )


def emit_pro(design: Design, *, name: str) -> str:
    job = compile_design(design)
    classes = []
    for cls in job.classes:
        row = {
            "name": cls.name,
            "clearance": cls.clearance_mm,
            "track_width": cls.track_width_mm,
            "via_diameter": cls.via_diameter_mm,
            "via_drill": cls.via_drill_mm,
        }
        if cls.diff_pair_gap_mm is not None:
            row["diff_pair_gap"] = cls.diff_pair_gap_mm
            row["diff_pair_width"] = cls.diff_pair_width_mm
        classes.append(row)
    patterns = [
        {"netclass": c.name, "pattern": p}
        for c in job.classes
        for p in c.patterns
    ]
    doc = {
        "board": {
            "design_settings": {
                "defaults": {},
                "rules": {"min_clearance": 0.09, "min_track_width": 0.09},
            }
        },
        "meta": {"filename": f"{name}.kicad_pcb", "version": 1},
        "net_settings": {"classes": classes, "netclass_patterns": patterns},
        "text_variables": {},
    }
    return json.dumps(doc, indent=2) + "\n"


def seed_job(design: Design, out_pcb: Path, *, name: str) -> dict:
    out_pcb = Path(out_pcb)
    out_pcb.parent.mkdir(parents=True, exist_ok=True)
    pcb = emit_pcb(design, name=name)
    pro = emit_pro(design, name=name)
    out_pcb.write_text(pcb)
    out_pcb.with_suffix(".kicad_pro").write_text(pro)
    return {"pcb": str(out_pcb), "instances": len(design.instances)}
