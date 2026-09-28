"""JLCPCB fab package: Gerbers, drill, BOM/CPL, fiducials, via-in-pad notes."""

from __future__ import annotations

import csv
import io
import json
import math
import os
import re
import shutil
import subprocess
from pathlib import Path

from .ampacity import bottleneck_lines, power_bottlenecks, power_moves
from .apply import write_dru
from .check import check_job
from .compile import CompiledJob
from .copper import power_ampacity_failures, unrouted_nets, vias_on_no_via_nets
from .dru import soft_kind
from .project import copy_board, copy_with_siblings, write_sidecars
from .stackup import copper_layers
from .geom import footprint_box_local
from .sexp import (
    board_footprint_spans,
    footprint_at,
    footprint_reference,
    stable_uuid,
)


def kicad_cli() -> Path:
    found = shutil.which("kicad-cli")
    if found:
        return Path(found)
    for candidate in (
        Path("/usr/bin/kicad-cli"),
        Path("/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli"),
    ):
        if candidate.exists():
            return candidate
    return Path("kicad-cli")


def _default_fab_dir(pcb: Path) -> Path:
    if pcb.parent.name in ("routed", "placed"):
        return pcb.parent.parent / "fab"
    return pcb.parent / "fab"


_ON = ("1", "true", "yes", "on")
"""How every pcbc environment switch is read (`patterns.hard_refusals`). One spelling across the
tool: `PCBC_STRICT_POWER=true` meaning "off" is a silent no-op, and a silent no-op on a strictness
flag is the worst kind."""


def copper_gerber_layers(n: int) -> str:
    if n <= 2:
        coppers = ["F.Cu", "B.Cu"]
    else:
        coppers = ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"]
    extras = [
        "F.Paste",
        "B.Paste",
        "F.SilkS",
        "B.SilkS",
        "F.Mask",
        "B.Mask",
        "Edge.Cuts",
    ]
    return ",".join(coppers + extras)


def lcsc_index(root: Path) -> dict[str, dict]:
    """Unused. LCSC is a footprint property, not a sidecar."""
    return {}


_PROP = re.compile(r'\(property "([^"]+)" "([^"]*)"')


def footprint_props(block: str) -> dict[str, str]:
    return dict(_PROP.findall(block))


def jlc_bom(pcb_text: str, sources: dict[str, dict]) -> tuple[list[dict], list[str]]:
    """Group footprints into JLC BOM rows. Missing LCSC listed in the second value."""
    rows_by: dict[tuple[str, str, str], list[str]] = {}
    missing: list[str] = []
    for start, end in board_footprint_spans(pcb_text):
        block = pcb_text[start:end]
        ref = footprint_reference(block)
        if not ref or ref.startswith("FID"):
            continue
        if "exclude_from_bom" in block:
            continue
        props = footprint_props(block)
        mpn = props.get("Mpn") or ""
        lcsc = props.get("LCSC") or (sources.get(mpn) or {}).get("lcsc") or ""
        comment = props.get("Value") or mpn
        footprint = (block.split("\n", 1)[0].split('"')[1] if '"' in block[:80] else "")
        if "\n" in block:
            m = re.match(r'\(footprint "([^"]+)"', block)
            footprint = m.group(1) if m else footprint
        if not lcsc and "(attr through_hole" in block:
            # User-soldered TH (Teensy, pin headers). Not a JLC SMT row.
            continue
        key = (comment, footprint.split(":")[-1], str(lcsc))
        rows_by.setdefault(key, []).append(ref)
        if not lcsc:
            missing.append(ref)
    rows = []
    for (comment, footprint, lcsc), refs in sorted(rows_by.items(), key=lambda kv: kv[1][0]):
        rows.append(
            {
                "Comment": comment,
                "Designator": ",".join(sorted(refs)),
                "Footprint": footprint,
                "LCSC Part #": lcsc,
            }
        )
    return rows, missing


def write_bom_csv(rows: list[dict], path: Path) -> None:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(["Comment", "Designator", "Footprint", "LCSC Part #"])
    for r in rows:
        w.writerow([r["Comment"], r["Designator"], r["Footprint"], r["LCSC Part #"]])
    path.write_text(buf.getvalue())


def kicad_pos_to_jlc_cpl(pos_csv: str) -> str:
    reader = csv.DictReader(io.StringIO(pos_csv))
    out = io.StringIO()
    w = csv.writer(out, lineterminator="\n")
    w.writerow(["Designator", "Mid X", "Mid Y", "Rotation", "Layer"])
    for row in reader:
        ref = (row.get("Ref") or row.get("Designator") or "").strip().strip('"')
        if not ref or ref.startswith("FID"):
            continue
        x = row.get("PosX") or row.get("Mid X") or "0"
        y = row.get("PosY") or row.get("Mid Y") or "0"
        rot = row.get("Rot") or row.get("Rotation") or "0"
        side = (row.get("Side") or row.get("Layer") or "top").strip().strip('"').lower()
        layer = "Top" if side.startswith("top") or side == "front" else "Bottom"
        w.writerow([ref, x.strip().strip('"'), y.strip().strip('"'), rot.strip().strip('"'), layer])
    return out.getvalue()


def footprint_side(block: str) -> str:
    m = re.search(r'\n\t\t\(layer "([^"]+)"\)', block)
    if not m:
        m = re.search(r'\(layer "([^"]+)"\)', block)
    layer = m.group(1) if m else "F.Cu"
    if layer.startswith("B.") or layer in ("B.Cu", "Bottom"):
        return "Bottom"
    return "Top"


def jlc_cpl_from_board(pcb_text: str) -> str:
    """JLC CPL from footprint (at ...) — does not need kicad-cli pos.

    Y is negated to match KiCad POS / JLCPCB (file Y-down vs pick-place).
    """
    rows: list[tuple[str, str, str, str, str]] = []
    for start, end in board_footprint_spans(pcb_text):
        block = pcb_text[start:end]
        ref = footprint_reference(block)
        if not ref or ref.startswith("FID"):
            continue
        if "exclude_from_pos_files" in block:
            continue
        at = footprint_at(block)
        if not at:
            continue
        x, y, rot = at
        rows.append(
            (ref, f"{x:.6f}", f"{-y:.6f}", f"{rot:.6f}", footprint_side(block))
        )
    rows.sort(key=lambda r: r[0])
    out = io.StringIO()
    w = csv.writer(out, lineterminator="\n")
    w.writerow(["Designator", "Mid X", "Mid Y", "Rotation", "Layer"])
    w.writerows(rows)
    return out.getvalue()


_VIA_AT = re.compile(
    r"\(via\s*\n\s*\(at ([0-9.+-]+) ([0-9.+-]+)\)"
    r"(?:\s*\n\s*\(size ([0-9.+-]+)\))?"
)
PASSIVE_PREFIXES = ("C", "R", "L", "D", "FB")
"""The reference *prefixes* a two-terminal passive declares. A `Part` says its own prefix (`Component(
prefix="L")`, and pcbc's `Resistor`/`Capacitor`/`Led` set "R"/"C"/"D"), so this reads a declaration and
not a spelling. `via_in_pad_blockers` takes the design where it has one; the regex below is what it
falls back to when it is handed only a board file."""

_PASSIVE_REF = re.compile(r"^(?:FB|[CRLD])(?![A-Za-z])")
r"""The fallback, when no `Design` is at hand. The `\d` this used to end in was the bug: it matched
`C1` and `R1` and not `C_VBUS`, `C_3V3_HF`, `R_FB_BOT` or `R_CC1`, which is every passive on buck,
c3_usb and node. Measured by injecting a via dead-centre in every passive pad of every built board:
blinky 2 of 2 refused, ds2 44 of 44, buck **2 of 18**, c3_usb **0 of 24**, node **0 of 40** (that
probe walked R/C/L footprints; `passive_refs` reads every declared passive prefix, so the suite's own
counts are 4, 18, 26 and 42)
(`docs/r2-measurements.md` S5r, finding 1). The negative lookahead keeps `U1`, `J1`, `SW_RST`,
`FID1`, `Y1` out; `D1` is in, and so is a ferrite `FB1`."""

_RING_SAMPLES = 32
"""How many points of a via's ring `inside` is decided on. A pad is convex (A.1), so a ring whose 32
points all lie in the pad is inside it to within `r*(1-cos(pi/32))` = 0.5 % of the ring radius, i.e.
0.9 um on a 0.35 mm via. `inside` only picks the exemption, never whether the via is a hit."""


_FLASH = re.compile(r"X(-?\d+)Y(-?\d+)D03\*")
_FS = re.compile(r"%FSLAX(\d)(\d)Y(\d)(\d)\*%")


def mask_flashes(gerber_text: str) -> list[tuple[float, float]]:
    """Every aperture flash in a solder-mask Gerber, in board mm (Y negated, as Gerber writes it).

    The one end-to-end way to ask "is this via tented?": a tented via has no mask aperture, so its
    coordinate is simply not in this list. `Stackup.via_tenting` is what pcbc writes into the board
    (`seed._tenting`) and this is what reads back what the arbiter plotted from it — because until
    S5's review the stanza on every routed board was KiCad's default and nothing checked it
    (`docs/r2-measurements.md` S5r, finding 3).

    Only `%MOMM%` files with a leading-zero-omitted absolute format are read, which is every Gerber
    KiCad 10 writes here; anything else comes back empty rather than wrong.
    """
    if "%MOMM*%" not in gerber_text:
        return []
    fs = _FS.search(gerber_text)
    if not fs:
        return []
    scale = 10.0 ** int(fs.group(2))
    return [(int(m.group(1)) / scale, -int(m.group(2)) / scale) for m in _FLASH.finditer(gerber_text)]


def board_pads(pcb_text: str) -> list:
    """Every pad of every footprint on the board, as the copper it actually draws.

    `pad_geoms` and not `(size w h)`: a custom pad's size is KiCad's anchor and not its copper (S1b),
    so a USB-C shell pad reads as 5 um square to anything that believes the size field, which is what
    `parse_foot` did before S1b and what this function did until S5r (finding 2).
    """
    from .pads import pad_geoms

    out = []
    for start, end in board_footprint_spans(pcb_text):
        block = pcb_text[start:end]
        ref = footprint_reference(block) or "?"
        at = footprint_at(block)
        if at is None:
            continue
        side = "B" if re.search(r'\(layer "B\.Cu"\)', block[: block.find("(pad") if "(pad" in block else len(block)]) else "F"
        out.extend(g for g in pad_geoms(block, at, side=side, ref=ref) if g.kind == "smd" and g.copper)
    return out


def via_in_pad(pcb_text: str, *, pads: list | None = None) -> list[dict]:
    """Vias whose copper touches a copper pad, with `inside` saying whether it is wholly in it.

    Two questions, split, because the old single test answered neither honestly (finding 2). It asked
    for **full containment** inside the pad's `(size w h)` box, so a via breaking a pad's edge — which
    wicks the joint exactly as a centred one does — came back as no hit at all, and a custom pad was
    measured as its 5 um anchor. Now the hit is an **overlap** of the true copper (`route_geom.clears`
    against `pads.pad_geoms`' own shapes, so rotation, roundrect corners and custom primitives are
    exact), and `inside` is what the exemption in `via_in_pad_blockers` reads.

    A via overlapping an IC pin of its own net is reported and is not a blocker; one overlapping a
    passive's pad is (`via_in_pad_blockers`).
    """
    from .route_geom import clears, hull_dist2, via_shape

    vias = [
        (float(m.group(1)), float(m.group(2)), float(m.group(3) or 0.25))
        for m in _VIA_AT.finditer(pcb_text)
    ]
    geoms = board_pads(pcb_text) if pads is None else pads
    hits = []
    for vx, vy, vsize in vias:
        ring = via_shape((vx, vy), vsize)
        vr = vsize / 2.0
        for g in geoms:
            for shape in g.copper:
                if clears(ring, shape, 0.0):
                    continue
                inside = all(
                    hull_dist2(((vx + vr * math.cos(2 * math.pi * k / _RING_SAMPLES), vy + vr * math.sin(2 * math.pi * k / _RING_SAMPLES)),), shape.pts) <= shape.r * shape.r + 1e-12
                    for k in range(_RING_SAMPLES)
                )
                hits.append({"via": (vx, vy), "pad": f"{g.ref}.{g.num}", "inside": inside, "net": g.net})
                break
            else:
                continue
            break
    return hits


def passive_refs(design) -> frozenset[str]:
    """The refs of every part whose declared prefix makes it a two-terminal passive."""
    return frozenset(i.ref for i in getattr(design, "instances", ()) if i.part.prefix in PASSIVE_PREFIXES or i.part.kind in ("generic", "led"))


def via_in_pad_blockers(hits: list[dict], design=None) -> list[dict]:
    """VIP that JLC Standard cannot assemble: passives and connector mounting pegs.

    USB-C underpad (qfn_fanout --allow-via-in-pad) and IC pins stay named in
    FAB_NOTES as filled+capped; they are not a Standard-fab hard fail.
    A via inside an 0603/resistor/inductor pad wicks the joint even when filled — and so does one
    that only breaks its edge, which is why a passive blocks on **any** overlap while the IC and
    USB-C exemption still covers a hit of either kind (`docs/r2-measurements.md` S5r, finding 2's
    deviation: the three partial overlaps on these boards are all same-net vias grazing an IC pin,
    all tented, and refusing them would refuse two boards that ship).

    `design` is what decides what a part *is*; the reference string cannot (finding 1). Without one
    the `_PASSIVE_REF` fallback reads the spelling, which is what an audit of a bare board file has.
    """
    passives = passive_refs(design) if design is not None else None
    out: list[dict] = []
    for h in hits:
        pad = str(h.get("pad") or "")
        ref = pad.split(".", 1)[0]
        name = pad.split(".", 1)[-1] if "." in pad else ""
        passive = (ref in passives) if passives is not None else bool(_PASSIVE_REF.match(ref))
        if name.upper() == "MP" or passive:
            out.append(h)
    return out


def _fiducial_sexp(ref: str, x: float, y: float) -> str:
    """One fiducial footprint: a 1 mm copper dot in a 2 mm mask opening, its reference hidden on the
    silkscreen exactly as the place stage's silk pass (`silk.legalize_silk`) leaves every `FID` reference, and one uuid per
    item (the reference and the pad used to share one, which KiCad's load re-keyed at random)."""
    uid = stable_uuid("fiducial", ref)
    puid = stable_uuid("fiducial", ref, "ref")
    cuid = stable_uuid("fiducial", ref, "crtyd")
    duid = stable_uuid("fiducial", ref, "pad")
    return f'''	(footprint "Fiducial_1mm_Mask2mm"
		(layer "F.Cu")
		(uuid "{uid}")
		(at {x:.4f} {y:.4f})
		(property "Reference" "{ref}"
			(at 0 0 0)
			(layer "F.SilkS")
			(hide yes)
			(uuid "{puid}")
			(effects
				(font
					(size 0.5 0.5)
					(thickness 0.08)
				)
			)
		)
		(attr smd exclude_from_bom exclude_from_pos_files)
		(fp_circle
			(center 0 0)
			(end 1.25 0)
			(stroke (width 0.05) (type solid))
			(fill no)
			(layer "F.CrtYd")
			(uuid "{cuid}")
		)
		(pad "" smd circle
			(at 0 0)
			(size 1 1)
			(layers "F.Cu" "F.Mask")
			(solder_mask_margin 0.5)
			(uuid "{duid}")
		)
	)
'''


def _world_courtyard(block: str) -> tuple[float, float, float, float] | None:
    at = footprint_at(block)
    if not at:
        return None
    x0, y0, x1, y1 = footprint_box_local(block, "courtyard")
    fx, fy, rot = at
    rad = math.radians(-rot)
    c, s = math.cos(rad), math.sin(rad)
    xs, ys = [], []
    for px, py in ((x0, y0), (x1, y0), (x1, y1), (x0, y1)):
        xs.append(fx + px * c - py * s)
        ys.append(fy + px * s + py * c)
    return (min(xs), min(ys), max(xs), max(ys))


def _fid_hits_courtyard(text: str, x: float, y: float, radius: float = 1.35) -> bool:
    for start, end in board_footprint_spans(text):
        box = _world_courtyard(text[start:end])
        if not box:
            continue
        cx = min(max(x, box[0]), box[2])
        cy = min(max(y, box[1]), box[3])
        if math.hypot(x - cx, y - cy) < radius:
            return True
    return False


def existing_fiducials(text: str) -> list[tuple[str, float, float]]:
    """The `FIDn` footprints a board already carries, `(ref, x, y)` each."""
    placed = []
    for s, e in board_footprint_spans(text):
        b = text[s:e]
        r = footprint_reference(b)
        at = footprint_at(b)
        if r and r.startswith("FID") and at:
            placed.append((r, at[0], at[1]))
    return placed


def insert_fiducials(
    text: str, size_mm: tuple[float, float], inset: float = 4.0, spots: list[tuple[str, float, float]] | None = None
) -> tuple[str, list[tuple[str, float, float]]]:
    """(the board with three fiducials appended, **the ones appended**). A board that already carries
    any `FIDn` is returned unchanged with an empty list: fab's allowlist admits only a fiducial this
    call wrote (fifth review: the existing ones were returned as if inserted, and a second FID1
    stacked on the first was excused by them)."""
    if existing_fiducials(text):
        return text, []
    w, h = size_mm
    if spots is not None:
        return _append_fiducials(text, list(spots)), list(spots)
    # Prefer NE/SE/SW; fall back to NW if a courtyard eats a corner.
    candidates = [
        (w - inset, inset),
        (w - inset, h - inset),
        (inset, h - inset),
        (inset, inset),
    ]
    spots: list[tuple[str, float, float]] = []
    for x, y in candidates:
        if not _fid_hits_courtyard(text, x, y):
            spots.append((f"FID{len(spots) + 1}", x, y))
        if len(spots) == 3:
            break
    if len(spots) < 3:
        spots = [
            ("FID1", w - inset, inset),
            ("FID2", w - inset, h - inset),
            ("FID3", inset, h - inset),
        ]
    return _append_fiducials(text, spots), spots


def _append_fiducials(text: str, spots: list[tuple[str, float, float]]) -> str:
    if not text.rstrip().endswith(")"):
        raise ValueError("board file does not end with )")
    stripped = text.rstrip()
    chunk = "".join(_fiducial_sexp(n, x, y) for n, x, y in spots)
    return stripped[:-1] + chunk + ")\n"


def _run(cmd: list[str]) -> dict:
    proc = subprocess.run(cmd, capture_output=True, text=True)
    # kicad-cli prints a line per file; whole, not cut at a count that lands at a different place
    # for every absolute path (third review D3: node's report differed between two build paths in
    # where a 2000-character cut fell). `_relative` makes the paths in it the layout dir's.
    return {
        "cmd": cmd,
        "returncode": proc.returncode,
        "stdout": (proc.stdout or "")[-20000:],
        "stderr": (proc.stderr or "")[-20000:],
    }


# The silkscreen checks: the footprint library's own drawings, not copper. Same-footprint pad-pad is a
# KiCad AABB false short on rotated modules — DRC owns those. Nothing else is exempt: the router's
# exemptions (dangling tracks and vias, the pair gap it rewrote, clearance errors above the fab floor)
# went with it (docs/native-plan.md, critique #1). A KiCad error is an error.
_IGNORE_DRC = {
    "silk_overlap",
    "silk_over_copper",
    "silk_edge_clearance",
    "silk_mask_clearance",
    # length_out_of_range is not ignored: NetReq max_mm is an airwire budget and never becomes a
    # length rule, the canary is a warning and never reaches the error gate, and an explicit
    # length_mm= (or an i2c bus) must gate.
}


def _same_footprint_pad_pad(v: dict) -> bool:
    items = v.get("items") or []
    if len(items) < 2:
        return False
    refs: list[str] = []
    for item in items:
        desc = item.get("description") or ""
        if not desc.startswith("Pad ") or " of " not in desc:
            return False
        refs.append(desc.split(" of ", 1)[1].split()[0])
    return len(set(refs)) == 1


_ACTUAL_MM = re.compile(r"actual ([0-9.]+)\s*mm")


def copper_drc_errors(drc: dict, floor_mm: float = 0.10) -> list[dict]:
    viol = drc.get("violations") or drc.get("Violations") or []
    out = []
    for v in viol:
        typ = v.get("type") or v.get("Type") or ""
        sev = (v.get("severity") or v.get("Severity") or "").lower()
        if typ in _IGNORE_DRC:
            continue
        if typ == "shorting_items" and _same_footprint_pad_pad(v):
            continue
        if sev == "error":
            out.append(v)
    return out


def bom_refs(rows: list[dict]) -> set[str]:
    refs: set[str] = set()
    for row in rows:
        for d in row["Designator"].split(","):
            d = d.strip()
            if d:
                refs.add(d)
    return refs


def cpl_refs(cpl_csv: str) -> set[str]:
    refs: set[str] = set()
    for i, line in enumerate(cpl_csv.splitlines()):
        if i == 0 or not line.strip():
            continue
        ref = line.split(",")[0].strip().strip('"')
        if ref:
            refs.add(ref)
    return refs


def board_roles(text: str):
    """The pieces pcbc's patterns wrote and their roles, **read off the board being fabricated**
    (`layout_job.roles_doc`: the `pcbc:<reason>:<owner>` groups and the geometry the board has).

    Until the second review this came from `routed/copper.json`, the router's sidecar: a spine
    narrowed on the shipped board to 0.2 mm still read 0.781 mm in the sidecar, and the gate below
    passed it. The sidecar is renamed after normalise (`layout_job.SIDECAR_CONSUMED`) so nothing can
    read it here. A board with no `pcbc:` group (patterns off, a placed board) has nothing of pcbc's
    to judge, exactly as an absent sidecar did."""
    from .layout_job import ROLE_PREFIX, roles_doc

    if f'(group "{ROLE_PREFIX}' not in text:
        from .route_emit import Sidecar

        return Sidecar()
    return roles_doc(text)


def _owned_copper(doc) -> tuple[tuple[str, str, float], ...]:
    """`(net, reason, width_mm)` for every segment pcbc wrote, with the width **the board has**."""
    return tuple((str(i["net"]), str(i["reason"]), float(i["w"])) for i in doc.items if i.get("key", ("",))[0] == "seg" and i.get("net"))


def _shield_copper(doc) -> frozenset:
    """`copper_bar.bar_key`s of the copper that is not on any rail (`ampacity.NOT_A_RAIL`: today the
    `guard` reason alone — see its docstring for why this is **not** `copper_bar.REDUNDANT`). It keeps
    a shield out of `ampacity.power_bottlenecks`, where 27 mm of 0.127 mm ground guard was once
    reported as 27 mm of under-width ground rail. The four example boards declare no `Guard()`, so it
    is empty on every one of them."""
    from .ampacity import NOT_A_RAIL
    from .copper_bar import bar_key

    out = set()
    for i in doc.items:
        if i.get("reason") not in NOT_A_RAIL:
            continue
        key = i.get("key") or [""]
        if key[0] == "seg":
            out.add(bar_key("seg", key[1], tuple(key[2]), tuple(key[3]), float(i.get("w") or 0.0)))
        elif key[0] == "via":
            out.add(bar_key("via", tuple(key[1])))
    return frozenset(out)


def layout_uuids_of(text: str) -> frozenset[str]:
    """Every uuid a layout object of `text` carries (`layout_job.layout_uuids`), for `pin_all_uuids`'
    `keep`. A board the layout pipeline did not emit (a placed board handed straight to fab) may
    carry a primitive the decompiler has no field for; it then has no Python uuids to keep."""
    from .gen import Unmapped, decompile
    from .layout_job import layout_uuids

    try:
        return layout_uuids(decompile(text))
    except Unmapped:
        return frozenset()


def fab_job(
    job: CompiledJob,
    pcb: Path,
    *,
    out_dir: Path | None = None,
    components: Path | None = None,
    insert_fids: bool = True,
    design=None,
) -> dict:
    pcb = Path(pcb)
    out_dir = Path(out_dir) if out_dir else _default_fab_dir(pcb)
    out_dir.mkdir(parents=True, exist_ok=True)
    gerber_dir = out_dir / "gerbers"
    gerber_dir.mkdir(exist_ok=True)
    work = out_dir / "layout.kicad_pcb"
    # The fab board's `.kicad_pro` and `.kicad_dru` are written from the compiled job, never copied
    # from the routed board's directory: fab's DRC and its exports read them, and a copied sidecar was
    # a file nothing stamped (fourth review, C1). `design` is the loaded board when the build calls
    # this; an audit of a bare board file has no design and keeps the sidecars beside `pcb`.
    copy_board(pcb, work)
    side_name = Path(pcb).parent.parent.name if Path(pcb).parent.name in ("routed", "placed", "seed") else Path(pcb).stem
    if design is not None:
        write_sidecars(design, job, work, name=side_name)
    else:
        copy_with_siblings(pcb, work)
        write_dru(job, work)
    text = work.read_text()
    fids: list[tuple[str, float, float]] = []
    if insert_fids:
        text, fids = insert_fiducials(text, job.board_size_mm)
        work.write_text(text)
    # Fab writes nothing else onto the board. The silkscreen references were placed by the place
    # stage (`place_native.place`'s `silk.legalize_silk`, with the decoupling caps it may hide) and are
    # on the routed board the gates judged; until the fifth review fab ran the silk pass again, without that
    # list and in another footprint order, moved three of buck's references, and needed an allowlist
    # that admitted any reference position and any text size.
    silk_rep = {"silk": {"moved": [], "hidden": [], "sized": [], "issues": [], "notes": ["fab writes no silk: the references are the place stage's, on the routed board"]}}

    comp_root = Path(components) if components else None
    if comp_root is None:
        for cand in pcb.parents:
            if (cand / "components").is_dir():
                comp_root = cand / "components"
                break
        if comp_root is None:
            comp_root = Path("components")
    sources = lcsc_index(comp_root)
    bom_rows, missing_lcsc = jlc_bom(text, sources)
    vip = via_in_pad(text)
    # The design, not the reference string, is what says a part is a passive whose pad a via wicks
    # (finding 1). `build_job` hands it over; an audit of a bare board file falls back to the ref.
    blockers = via_in_pad_blockers(vip, design)
    # Every `Thermal()` land as this board has it — the barrels placed, their K/W and rise, the pads
    # left bare — measured the way the route gate measures it (`route_verify.thermal_rows`): the fab
    # note states what is on the board, never the budget.
    from .route_verify import thermal_rows

    thermal = [r.to_dict() for r in thermal_rows(text, job.constraints)]

    cli = kicad_cli()
    steps: list[dict] = []
    result = {
        "fab": str(out_dir),
        "pcb": str(work),
        "kicad_cli": str(cli),
        "fiducials": [{"ref": n, "at": [x, y], "by": "fab"} for n, x, y in fids] + [{"ref": n, "at": [x, y], "by": "place"} for n, x, y in (existing_fiducials(text) if not fids else [])],
        "bom_rows": len(bom_rows),
        "missing_lcsc": missing_lcsc,
        "via_in_pad": vip,
        "via_in_pad_blockers": blockers,
        "thermal": thermal,
        "silk": silk_rep.get("silk"),
        "steps": steps,
        "error": None,
    }
    lock_fail = check_job(job, work)
    result["check"] = lock_fail
    if lock_fail:
        result["error"] = "pcbc check: " + "; ".join(lock_fail)
        _write_notes(out_dir, job, result)
        return result
    if not cli.exists() and shutil.which(str(cli)) is None:
        result["error"] = f"kicad-cli not found ({cli})"
        _write_notes(out_dir, job, result)
        return result

    drc_json = out_dir / "drc.json"
    drc_step = _run(
        [
            str(cli),
            "pcb",
            "drc",
            "--refill-zones",
            "--save-board",
            "--format",
            "json",
            "--severity-error",
            "-o",
            str(drc_json),
            str(work),
        ]
    )
    steps.append(drc_step)
    from .sexp import pin_all_uuids

    if work.exists():
        # Every layout object keeps the uuid its Python line carries (`layout.gen.py`,
        # `layout.core.py`): the `pcbc:` role groups name their members by it, and a fab board whose
        # groups dangle is one KiCad drops the groups from on load and one that cannot be decompiled
        # back to the Python it came from. Only what KiCad invented (pads, fields) is re-pinned. A
        # uuid two layout objects carry is refused here, before the re-pin could hide it (C2).
        from .gen import DuplicateUuid, decompile

        try:
            decompile(work.read_text())
        except DuplicateUuid as exc:
            result["copper_equal"] = False
            result["error"] = f"the fab board is not the routed board: the fab board carries a uuid twice: {exc}"
            _write_notes(out_dir, job, result)
            return result
        except Exception:  # noqa: BLE001 - a board the decompiler cannot read is judged below
            pass
        # Every uuid the routed board carries is kept as it is: those are the routed board's own,
        # pinned there, and KiCad's load keeps them. Only an item the routed board lacks (a fiducial
        # fab inserted) is pinned by position. Until the fifth review every footprint's uuid and every
        # child's was re-pinned here under another key (58 on blinky, 727 on node), which the
        # comparison below then had to mask.
        work.write_text(pin_all_uuids(work.read_text(), work.parent.name, "fab", keep=layout_uuids_of(work.read_text()) | all_uuids_of(pcb.read_text())))
    drc_doc: dict = {}
    if drc_json.exists():
        try:
            drc_doc = json.loads(drc_json.read_text())
        except json.JSONDecodeError:
            drc_doc = {}
    elif drc_step["returncode"] != 0:
        err = (drc_step["stderr"] or drc_step["stdout"] or "no output").strip()
        result["error"] = (
            "kicad-cli DRC did not write json (need KiCad 10 for this board): "
            + err[-500:]
        )
        _write_notes(out_dir, job, result)
        return result
    # (S1) The board fab exports is the board the route step's gates judged, **node for node**: every
    # segment, arc, via, zone and `pcbc:` role group by uuid and field, and every other top-level
    # node — footprints with their pads, the outline, the setup, the nets — as a multiset, with an
    # explicit allowlist of what fab itself writes (`FAB_WRITES`: the fiducials it inserted this
    # run, the silk reference positions, its sidecars). Until the third review (A1, A2, A5) a via
    # deleted, a spine narrowed with its group dropped, or a pour shrunk inside fab shipped ten
    # Gerbers `ok=True`; until the fourth (C2) so did a `gr_poly` on F.Cu, a footprint added,
    # deleted or moved, a pad's paste, a duplicated uuid. Fab is not a second judge of the board,
    # it is held to the first.
    # M3: fab's own sidecars are the compiled job's, byte for byte, after fab's DRC and refill have read
    # them: fab's `.kicad_dru`/`.kicad_pro` steer its refill, its DRC and the Gerber export's zone
    # check, and until the sixth review an area-neutral `.kicad_dru` edit made inside fab shipped a GND
    # pour 0.0105 mm from a 5V pad with `copper_equal=True`, stamped as fab's own output.
    if design is not None:
        side = sidecars_not_compiled(design, job, work, name=side_name)
        if side:
            result["copper_equal"] = False
            result["error"] = "fab's sidecars are not the compiled job's: " + "; ".join(side)
            _write_notes(out_dir, job, result)
            return result
    expected = expected_fab_board(pcb.read_text(), fids, work.parent.name)
    moved = fab_board_diff(expected, work.read_text())
    result["copper_equal"] = not moved
    if moved:
        result["error"] = "the fab board is not the routed board: " + "; ".join(moved[:6])
        _write_notes(out_dir, job, result)
        return result
    # The BOM from the board that passed, never before (fifth review: a refused fab left a `bom.csv`
    # built from the refused board beside it).
    write_bom_csv(bom_rows, out_dir / "bom.csv")
    # An open net is an unconnected item in KiCad's report, whatever `--severity-error` filters from
    # the violations; until the third review (A1) fab never read it.
    unconnected = drc_doc.get("unconnected_items") or []
    result["unconnected"] = len(unconnected)
    if unconnected:
        shown = "; ".join(str(u.get("description", "")) for u in unconnected[:4])
        result["error"] = f"kicad-cli DRC: {len(unconnected)} unconnected item(s) on the fab board: {shown}"
        _write_notes(out_dir, job, result)
        return result
    # 2-layer USB floor is 0.10. 4-layer JLCPCB standard is 0.127 mm (5 mil);
    # 0.16 was a house comfort value that flagged 0.15 mm via-seg scrapes the
    # fab will build.
    from .stackup import get_stackup

    floor = get_stackup(job.stackup).clearance_min
    copper_err = copper_drc_errors(drc_doc, floor_mm=floor)
    result["drc_floor_mm"] = floor
    result["drc_copper_errors"] = len(copper_err)

    # CPL from the board file. kicad-cli pos is provenance only — Ubuntu's
    # KiCad 7 cannot load a KiCad 10 board and writes an empty POS.
    text = work.read_text()
    (out_dir / "cpl.csv").write_text(jlc_cpl_from_board(text))
    pos_raw = out_dir / "pos_kicad.csv"
    steps.append(
        _run(
            [
                str(cli),
                "pcb",
                "export",
                "pos",
                "--format",
                "csv",
                "--units",
                "mm",
                "--side",
                "both",
                "-o",
                str(pos_raw),
                str(work),
            ]
        )
    )

    cpl_path = out_dir / "cpl.csv"
    missing_cpl = sorted(bom_refs(bom_rows) - cpl_refs(cpl_path.read_text()))
    result["bom_not_in_cpl"] = missing_cpl

    if copper_err:
        result["error"] = f"kicad-cli DRC {len(copper_err)} copper error(s)"
        _write_notes(out_dir, job, result)
        return result
    if blockers:
        result["error"] = (
            f"{len(blockers)} via-in-pad on passives/mounting pegs; "
            "re-route with --same-net-pad-clearance (dog-bone, not VIP)"
        )
        _write_notes(out_dir, job, result)
        return result
    if missing_cpl:
        result["error"] = f"BOM refs missing from CPL: {', '.join(missing_cpl)}"
        _write_notes(out_dir, job, result)
        return result

    opens = unrouted_nets(text)
    result["unrouted"] = opens
    if opens:
        result["error"] = "unrouted net " + ", ".join(opens)
        _write_notes(out_dir, job, result)
        return result
    via_fail = vias_on_no_via_nets(job, text)
    result["no_via_violations"] = via_fail
    if via_fail:
        result["error"] = "; ".join(via_fail)
        _write_notes(out_dir, job, result)
        return result
    # Two different questions, and the S7 review is the reason they are two (findings 1, 2, 3, 7,
    # 12, 20). `power_ampacity_failures` is the **gate**: pcbc's own pattern copper, at the class
    # width it declared, which is the thing pcbc is answerable for and which stops the build.
    # `power_bottlenecks` is the **measurement**: what the whole net — leftover and all — can carry
    # between its pads, printed as a power move when a rail is short (`ampacity.power_moves`). It is
    # recorded in `report.json` and in `FAB_NOTES.md`, and pinned per board in
    # `test_examples_fab.py`, so it is a ledger that must not get worse rather than a boolean already true.
    roles = board_roles(text)
    amp = power_ampacity_failures(job, text, owned=_owned_copper(roles))
    result["ampacity"] = amp
    result["ampacity_bottleneck"] = power_bottlenecks(job, text, redundant=_shield_copper(roles))
    # The measurement only counted while it sat in `FAB_NOTES.md`: the build printed
    # `copper: verified`, `error: null`, exit 0, and an AI reading that shipped buck with 1.21 A of
    # copper on a 2 A rail. `power_moves` carries the non-`ok` rows out as moves, `build.py` puts
    # them in the build's own output and the CLI prints them on stderr, and `--strict-power`
    # (`PCBC_STRICT_POWER=1`) turns them into this gate's error for an author who wants the stop.
    result["power_moves"] = power_moves(
        result["ampacity_bottleneck"], copper_layers(job.layers), job.planes
    )
    if amp:
        result["error"] = "; ".join(amp)
        _write_notes(out_dir, job, result)
        return result
    if result["power_moves"] and os.environ.get("PCBC_STRICT_POWER", "").lower() in _ON:
        result["error"] = "power: " + "; ".join(result["power_moves"])
        _write_notes(out_dir, job, result)
        return result

    layers = copper_gerber_layers(job.layers)
    gerber_step = _run(
        [
            str(cli),
            "pcb",
            "export",
            "gerbers",
            "--check-zones",
            "--layers",
            layers,
            "-o",
            str(gerber_dir),
            str(work),
        ]
    )
    steps.append(gerber_step)
    drill_step = _run(
        [
            str(cli),
            "pcb",
            "export",
            "drill",
            "--format",
            "excellon",
            "--excellon-units",
            "mm",
            "--excellon-separate-th",
            "--generate-map",
            "-o",
            str(out_dir),
            str(work),
        ]
    )
    steps.append(drill_step)
    gfiles = list(gerber_dir.glob("*"))
    result["gerbers"] = len(gfiles)
    if design is not None:
        # The export's `--check-zones` refilled from the same sidecars: still the compiled job's.
        side = sidecars_not_compiled(design, job, work, name=side_name)
        if side:
            result["error"] = "fab's sidecars are not the compiled job's after the export: " + "; ".join(side)
            _write_notes(out_dir, job, result)
            return result
    # A failed or partial export is an error, not a package (third review A3): every requested layer's
    # Gerber and both drill files must be on disk and each export must have exited 0.
    missing_out = export_missing(work.read_text(), layers.split(","), gerber_dir, out_dir)
    if gerber_step["returncode"] != 0:
        result["error"] = f"kicad-cli gerber export failed ({gerber_step['returncode']}): {(gerber_step['stderr'] or gerber_step['stdout']).strip()[-300:]}"
    elif drill_step["returncode"] != 0:
        result["error"] = f"kicad-cli drill export failed ({drill_step['returncode']}): {(drill_step['stderr'] or drill_step['stdout']).strip()[-300:]}"
    elif missing_out:
        result["error"] = "kicad-cli export wrote no " + ", ".join(missing_out[:6])
    if missing_lcsc:
        result["error"] = result.get("error") or f"BOM missing LCSC for {', '.join(missing_lcsc)}"
    result["dates_pinned"] = len(pin_dates(out_dir))
    _write_notes(out_dir, job, result)
    return result


_LAYER_ROW = re.compile(r'\(\d+\s+"([^"]+)"\s+\w+(?:\s+"([^"]+)")?\)')


def gerber_stems(pcb_text: str, layers: list[str]) -> dict[str, str]:
    """`{layer: file stem}` for the Gerbers kicad-cli writes: `<board stem>-<layer user name with "."
    as "_">`, the user name being the layer table's (`(5 "F.SilkS" user "F.Silkscreen")` ->
    `F_Silkscreen`, `F.Cu` -> `F_Cu`)."""
    names = {}
    m = re.search(r"\(layers\n(.*?)\n\t\)", pcb_text, flags=re.S)
    for row in _LAYER_ROW.finditer(m.group(1) if m else ""):
        names[row.group(1)] = row.group(2) or row.group(1)
    return {la: names.get(la, la).replace(".", "_") for la in layers}


def export_missing(pcb_text: str, layers: list[str], gerber_dir: Path, out_dir: Path) -> list[str]:
    """Every output the two exports were asked for and did not write: `gerber <layer>` per layer whose
    file is absent, `gerber job file`, `drill PTH`, `drill NPTH`."""
    stem = "layout"
    out = []
    for la, name in gerber_stems(pcb_text, layers).items():
        if not any(gerber_dir.glob(f"{stem}-{name}.*")):
            out.append(f"gerber {la}")
    if not (gerber_dir / f"{stem}-job.gbrjob").exists():
        out.append("gerber job file")
    for kind in ("PTH", "NPTH"):
        if not (out_dir / f"{stem}-{kind}.drl").exists():
            out.append(f"drill {kind}")
    return out


def sidecars_not_compiled(design, job: CompiledJob, work: Path, *, name: str) -> list[str]:
    """Each of fab's `.kicad_pro`/`.kicad_dru` that is not, byte for byte, what `write_sidecars` writes
    from the compiled job (M3). Empty is the only acceptable answer."""
    import tempfile

    from .project import write_sidecars

    out = []
    with tempfile.TemporaryDirectory(prefix="pcbc-fab-sidecars-") as tmp:
        fresh = Path(tmp) / work.name
        write_sidecars(design, job, fresh, name=name)
        for ext in (".kicad_pro", ".kicad_dru"):
            want, have = fresh.with_suffix(ext), work.with_suffix(ext)
            if not have.exists():
                out.append(f"fab/{have.name} is missing")
            elif have.read_bytes() != want.read_bytes():
                out.append(f"fab/{have.name} is not the file the compiled job writes (fab never edits its rules)")
    return out


def all_uuids_of(text: str) -> frozenset[str]:
    """Every `(uuid ...)` a board carries."""
    return frozenset(m.group(1) for m in re.finditer(r'\(uuid\s+"([^"]*)"\)', text))


def expected_fab_board(routed_text: str, fiducials, key: str) -> str:
    """The board fab may export: the routed board, plus — only when the routed board had none — the
    fiducials fab inserted this run, **as KiCad saves them** (the routed board with them appended,
    saved by `kicad-cli pcb upgrade` and pinned the way fab pins its own board). Nothing else fab
    could write is on it, so `fab_board_diff` can hold the fab board to it node for node, in order,
    uuids included (fifth review: the allowlist admitted any footprint shaped like a fiducial, any
    silk reference position and effects, and every uuid masked)."""
    from .sexp import pin_all_uuids

    if not fiducials:
        return routed_text
    import tempfile

    with tempfile.TemporaryDirectory(prefix="pcbc-fab-expected-") as tmp:
        scratch = Path(tmp) / "layout.kicad_pcb"
        scratch.write_text(_append_fiducials(routed_text, list(fiducials)))
        run = _run([str(kicad_cli()), "pcb", "upgrade", "--force", str(scratch)])
        if run["returncode"] != 0:
            raise RuntimeError(f"kicad-cli could not save the expected fab board: {(run['stderr'] or run['stdout']).strip()[-300:]}")
        text = scratch.read_text()
    return pin_all_uuids(text, key, "fab", keep=layout_uuids_of(text) | all_uuids_of(routed_text))


def _canon_node(node, *, mask_uuid: bool = False):
    """A parsed node as a comparable value, **in order**: its atoms where they stand, its child nodes
    in file order, numbers exactly, quoted and bare atoms apart, `render_cache` (KiCad's derived
    outline-font cache) dropped. With `mask_uuid` every `(uuid)` is dropped.

    Until the fifth review the children were sorted, atoms included, so `(size 1 1.45)` equalled
    `(size 1.45 1)`, `(at 29 14 90)` equalled `(at 14 29 90)` and a square `(pts)` equalled the same
    points as a bowtie: a pad turned or a primitive's vertices reordered inside fab shipped. Both
    boards are KiCad saves of one board, so nothing about their order is noise (measured, all five
    boards: routed and fab differ in no child's order)."""
    from .sexp import Q

    if isinstance(node, list):
        if not node:
            return ("list",)
        drop = {"render_cache"} | ({"uuid"} if mask_uuid else set())
        kids = [x for x in node[1:] if not (isinstance(x, list) and x and x[0] in drop)]
        return (str(node[0]), tuple(_canon_node(x, mask_uuid=mask_uuid) for x in kids))
    if isinstance(node, Q):
        return ("Q", str(node))
    try:
        return ("n", float(node))
    except ValueError:
        return ("a", str(node))


FAB_WRITES = (
    "the fiducial footprints `insert_fiducials` appends when the routed board has none, each exactly as KiCad saves `_fiducial_sexp(ref, x, y)` (the expected board, `expected_fab_board`); the place stage inserts them on every board measured, so fab appends none",
    "the zones' fill (`--refill-zones`), which must come back **vertex for vertex** the routed board's (M3): KiCad refills the same board from the same compiled rules to the same bytes, measured on every board the build has shipped",
    "its own `.kicad_dru` and `.kicad_pro` (`project.write_sidecars`; not board nodes), which must be byte for byte what the compiled job writes after fab's DRC and after its export (`sidecars_not_compiled`)",
    "the order of the top-level nodes: KiCad's save writes footprints, segments and vias in its own order (measured, sixth review: 3 to 167 nodes out of place on the five boards); nodes are compared as a multiset, each node in order inside",
)
"""Everything fab itself writes onto the board it exports, and nothing else: `fab_board_diff` holds the
fab board to the expected board (`expected_fab_board`) node for node, in order, **uuids included** —
fab keeps every uuid the routed board carries (`pin_all_uuids` with `keep`) and writes no silk (the
references are the place stage's). Anything else that differs is a refusal naming the node."""


def _uuid_counts(text: str) -> dict[str, int]:
    """How many items carry each uuid. A dimension's own `gr_text` carries the dimension's uuid by
    KiCad's rule (one object, `layout_prims.own_uuids`), so a uuid is counted once per dimension."""
    from collections import Counter

    from .sexp import parse_tree

    counts: Counter = Counter()

    def walk(n, bag: list) -> None:
        if not isinstance(n, list) or not n:
            return
        if n[0] == "uuid" and len(n) > 1:
            bag.append(str(n[1]))
            return
        for x in n[1:]:
            walk(x, bag)

    for n in parse_tree(text)[1:]:
        bag: list[str] = []
        walk(n, bag)
        if isinstance(n, list) and n and n[0] == "dimension":
            bag = list(dict.fromkeys(bag))
        counts.update(bag)
    return counts


def _uuid_owners(text: str, uid: str) -> list[str]:
    """The top-level nodes that carry `uid` anywhere inside them, named as `fab_board_diff` names a node."""
    from .sexp import parse_tree

    def has(n) -> bool:
        if not isinstance(n, list):
            return False
        if n[:1] == ["uuid"] and len(n) > 1 and str(n[1]) == uid:
            return True
        return any(has(x) for x in n[1:])

    out = []
    for n in parse_tree(text)[1:]:
        if isinstance(n, list) and n and has(n):
            head = str(n[0])
            if head == "footprint":
                ref = next((str(x[2]) for x in n[1:] if isinstance(x, list) and len(x) > 2 and x[0] == "property" and x[1] == "Reference"), "?")
                out.append(f"footprint {ref}")
            else:
                own = next((str(x[1]) for x in n[1:] if isinstance(x, list) and len(x) > 1 and x[0] == "uuid"), None)
                out.append(f"{head} {own}" if own else f"({head})")
    return out


_ONLY_FAB_WRITES = "fab writes nothing onto the board but the fiducials it appends when the routed board has none (fab.FAB_WRITES)"


def fab_board_diff(routed_text: str, fab_text: str) -> list[str]:
    """Every difference between the board fab may export (`expected_fab_board`: the routed board, plus
    any fiducial fab appended as KiCad saves it) and the board it exports, **on the whole board**, one
    sentence each. Empty is the only acceptable answer.

    Both boards are decompiled completely (a uuid two layout objects carry is `gen.DuplicateUuid`, a
    refusal; a uuid any two items carry is refused too) and every top-level node is compared as a
    multiset of **ordered** canonical nodes, uuids included (`_canon_node`): layout objects,
    footprints with their pads, the outline, `general`, `paper`, `title_block`, `layers`, `setup`
    (tenting included), `property`, `net`, `embedded_fonts`. A zone is compared without its
    `filled_polygon`, and its fill exactly, vertex for vertex (M3). A node that
    differs is named by its kind and, for a footprint, its reference; a copper object by its uuid and
    the fields that moved (`fab_copper_diff`, kept for the field-level sentence)."""
    from collections import Counter

    from .gen import DuplicateUuid, Unmapped, decompile
    from .sexp import parse_tree

    out: list[str] = []
    for what, text in (("routed", routed_text), ("fab", fab_text)):
        try:
            decompile(text)
        except DuplicateUuid as exc:
            return [f"the {what} board carries a uuid twice: {exc}"]
        except Unmapped as exc:
            return [f"the {what} board does not decompile, so it cannot be compared: {exc}"]
    for what, text in (("routed", routed_text), ("fab", fab_text)):
        seen = _uuid_counts(text)
        twice = sorted(u for u, n in seen.items() if n > 1)
        if twice:
            owners = _uuid_owners(text, twice[0])
            return [f"the {what} board carries the uuid {twice[0]} {seen[twice[0]]} times ({', '.join(owners)}); a uuid names one item"]
    out += fab_copper_diff(routed_text, fab_text)

    fills: dict[str, dict] = {"routed": {}, "fab": {}}

    def nodes(text: str, which: str) -> tuple[Counter, dict]:
        bag: Counter = Counter()
        raw: dict = {}
        for n in parse_tree(text)[1:]:
            if not isinstance(n, list) or not n:
                continue
            if str(n[0]) == "zone":
                # M3: a zone's fill is compared **exactly**, vertex for vertex in file order: fab ships
                # the fill the route stage's gates judged or it ships nothing. Until the sixth review the
                # fill was compared by (island count, total area) to a tolerance, and an area-neutral
                # rule edit inside fab moved a GND pour to 0.0105 mm from a 5V pad inside it. KiCad's
                # refill is deterministic given the same board and the same (compiled, byte-checked)
                # sidecars: measured on the five boards, twice each (docs/layout-properties.md, M3).
                uid = next((str(x[1]) for x in n[1:] if isinstance(x, list) and len(x) > 1 and x[0] == "uuid"), "?")
                kept = [x for x in n if isinstance(x, list) and x and x[0] == "filled_polygon"]
                fills[which][uid] = (_fill_summary(n), repr(tuple(_canon_node(x) for x in kept)), sum(len(x) for x in kept))
                n = [x for x in n if not (isinstance(x, list) and x and x[0] == "filled_polygon")]
            key = repr(_canon_node(n))
            bag[key] += 1
            raw.setdefault(key, n)
        return bag, raw

    a, ra = nodes(routed_text, "routed")
    b, rb = nodes(fab_text, "fab")
    for uid in sorted(set(fills["routed"]) & set(fills["fab"])):
        (x, fx, _), (y, fy, _) = fills["routed"][uid], fills["fab"][uid]
        if fx == fy:
            continue
        said_one = False
        for layer in sorted(set(x) | set(y)):
            (nx, ax), (ny, ay) = x.get(layer, (0, 0.0)), y.get(layer, (0, 0.0))
            if (nx, ax) != (ny, ay) or not said_one:
                out.append(f"zone {uid}'s fill on {layer} is not the routed board's vertex for vertex ({nx} island(s), {ax:.6f} mm2 routed; {ny} island(s), {ay:.6f} mm2 on the fab board): fab ships the fill the gates judged, exactly")
                said_one = True

    def name(n: list) -> str:
        head = str(n[0])
        if head == "footprint":
            ref = next((str(x[2]) for x in n[1:] if isinstance(x, list) and len(x) > 2 and x[0] == "property" and x[1] == "Reference"), "?")
            return f"footprint {ref}"
        uid = next((str(x[1]) for x in n[1:] if isinstance(x, list) and len(x) > 1 and x[0] == "uuid"), None)
        return f"{head} {uid}" if uid else f"({head})"

    layout_heads = set(_LAYOUT_HEADS())
    said = {m.split(" ", 2)[1] for m in out if len(m.split(" ", 2)) > 1}  # uuids fab_copper_diff already named
    for key, n in (a - b).items():
        node = ra[key]
        uid = next((str(x[1]) for x in node[1:] if isinstance(x, list) and len(x) > 1 and x[0] == "uuid"), None)
        if str(node[0]) in layout_heads and uid in said:
            continue
        out.append(f"{name(node)} is on the routed board and not on the fab board as it was ({n} node(s)); {_ONLY_FAB_WRITES}")
    for key, n in (b - a).items():
        node = rb[key]
        uid = next((str(x[1]) for x in node[1:] if isinstance(x, list) and len(x) > 1 and x[0] == "uuid"), None)
        if str(node[0]) in layout_heads and uid in said:
            continue
        out.append(f"{name(node)} is on the fab board and nothing routed it ({n} node(s)); {_ONLY_FAB_WRITES}")
    return out




def _fill_summary(zone: list) -> dict[str, tuple[int, float]]:
    """`{layer: (filled polygon count, total area mm2)}` of a parsed `(zone ...)` node's fill."""
    out: dict[str, list] = {}
    for x in zone[1:]:
        if not (isinstance(x, list) and x and x[0] == "filled_polygon"):
            continue
        layer = next((str(y[1]) for y in x[1:] if isinstance(y, list) and y and y[0] == "layer"), "?")
        pts = next((y for y in x[1:] if isinstance(y, list) and y and y[0] == "pts"), [])
        xy = [(float(p[1]), float(p[2])) for p in pts[1:] if isinstance(p, list) and p and p[0] == "xy"]
        area = abs(sum(xy[i][0] * xy[(i + 1) % len(xy)][1] - xy[(i + 1) % len(xy)][0] * xy[i][1] for i in range(len(xy)))) / 2.0 if xy else 0.0
        cur = out.setdefault(layer, [0, 0.0])
        cur[0] += 1
        cur[1] += area
    return {k: (v[0], v[1]) for k, v in out.items()}


def _LAYOUT_HEADS() -> tuple[str, ...]:
    from .layout_prims import HEADS

    return ("segment", "arc", "via", "zone", *HEADS)


def fab_copper_diff(routed_text: str, fab_text: str) -> list[str]:
    """Every copper object or `pcbc:` role group of the board fab was handed that the fab board does
    not carry field for field, and every one the fab board carries that the input lacks, by uuid
    (`layout_job.comparable`, so a group's members compare as a set). The fab board keeps every
    layout object's uuid (`layout_uuids_of` under `pin_all_uuids`), so a uuid names the same object
    on both. Empty when fab changed no copper, which is the only acceptable answer."""
    from .gen import Unmapped, decompile
    from .layout_job import ROLE_PREFIX, comparable

    try:
        a, b = decompile(routed_text), decompile(fab_text)
    except Unmapped as exc:
        return [f"a board does not decompile, so its copper cannot be compared: {exc}"]

    def objs(board) -> dict:
        got = {c.uuid: c for c in board.copper}
        got.update({g.uuid: g for g in board.graphics if g.kind == "group" and str(g.f.get("name", "")).startswith(ROLE_PREFIX)})
        return got

    x, y = objs(a), objs(b)
    out = []
    for uid in sorted(set(x) | set(y), key=str):
        if uid not in y:
            o = x[uid]
            out.append(f"{o.kind} {uid}{' on ' + str(o.net) if hasattr(o, 'net') else ''} is on the routed board and not on the fab board")
        elif uid not in x:
            o = y[uid]
            out.append(f"{o.kind} {uid}{' on ' + str(o.net) if hasattr(o, 'net') else ''} is on the fab board and nothing routed it")
        elif comparable(x[uid]) != comparable(y[uid]):
            p, q = comparable(x[uid]), comparable(y[uid])
            fields = [str(s[0]) if isinstance(s, tuple) else "kind" for s, t in zip(p, q) if s != t]
            out.append(f"{x[uid].kind} {uid} differs on the fab board in {', '.join(fields)}")
    return out


def _thermal_notes(job: CompiledJob, rows=()) -> list[str]:
    """The paragraph a `Thermal()` board has to carry to the fab, or nothing at all — written from
    `rows`, the lands **as the board has them** (`route_verify.thermal_rows`: the barrels placed, the
    K/W and rise they give, the pads left bare), never from the budget. A short array is stated as
    short, with its rise over the budget and the move (the third refutation round: a 2 W land with 30
    barrels was written up as its budget's 47 and 9.83 C, where the board gives 15.41 C).

    **This is the one thing in the whole technique that pcbc cannot do for the author**, and it is the
    reconciliation `docs/stitch-plan.md` S6 asks for in writing. A thermal array is via-in-pad by
    definition: every barrel it places sits inside the copper of the land it is cooling, on purpose,
    and `fab.via_in_pad` will find every one of them with `inside == True`. Two things follow, and
    they pull in opposite directions.

    - **The passive rule does not move.** `via_in_pad_blockers` refuses any via overlapping a
      two-terminal passive's pad or a connector's mounting peg, on **any** overlap, filled or not,
      because the barrel wicks the joint and starves it. `Thermal()` refuses a passive at compile
      (`constraints._thermal_refusals`) so the two agree, and `build._thermal_gate` compares the
      blocker list with and without pcbc's own array so a slip shows up as a failed build rather than
      as a longer list nobody diffed. `via_in_pad_blockers` has a **zero diff** in this slice.
    - **The fab package has to declare the fill, and only the person ordering can make that true.**
      `Stackup.via_tenting` closes the solder mask over a barrel's mouth; it does not plug the barrel.
      What makes a via safe under a reflowed land is IPC-4761 **Type VII — filled and capped**: the
      hole plugged with non-conductive epoxy, planarised and plated over, so the pad's surface is
      continuous copper. `Stackup.via_fill` says the fab *offers* it. This paragraph is what says the
      order must *ask* for it, because JLC's default via covering is tented and an array built tented
      is a land with n holes in it.
    """
    cs = job.constraints
    if cs is None or not cs.thermals:
        return []
    stack = cs.stackup
    by_pad = {r["pad"]: r for r in rows}
    lines = ["", "## Thermal vias (via-in-pad, deliberate)", ""]
    total = sum(int(by_pad[t.pad]["got"]) for t in cs.thermals if t.pad in by_pad)
    lines.append(
        f"{total} via(s) are placed **inside** a pad on purpose, under {len(cs.thermals)} exposed land(s). "
        f"They are not a DRC accident and they are not optional: they are the pad's heat path."
    )
    lines.append("")
    for t in cs.thermals:
        r = by_pad.get(t.pad)
        if r is None:
            lines.append(f"- **{t.pad}** on `{t.net}`: not measured on this board (no land {t.pad} was found); its budget asked for {t.need} barrel(s).")
            continue
        got = int(r["got"])
        head = (
            f"- **{t.pad}** on `{t.net}`: {got} x {t.via[0]:g}/{t.via[1]:g} mm through vias to the "
            f"{t.net} plane on `{t.plane}`. One barrel is {t.theta_via.value:g} K/W "
            f"(L/(k*A) on the plating annulus, k_cu 385 W/m/K, {stack.board_mm:g} mm board, "
            f"{stack.via_plating_mm:g} mm plating), so {got} in parallel are {r['theta_c_per_w']:g} K/W "
            f"and {t.watts:g} W raises the copper {r['rise_c']:g} C against a {t.rise_c:g} C budget."
        )
        if r["verdict"] == "short" or got < t.need:
            head += (
                f" **Short:** the budget asks for {t.need} barrel(s) ({t.rise_of(t.need):g} C) and the land had room for {got}, "
                f"so this land runs {r['rise_c'] - t.rise_c:+.2f} C over its budget: give it room (clear the tracks under it, "
                f"or move the part) or accept the rise with `Thermal(rise_c=)`."
            )
        if r.get("bare"):
            head += f" {len(r['bare'])} of its {r.get('blocks')} pads carry no barrel."
        lines.append(head)
    lines += [
        "",
        "**The order must specify via covering = Epoxy Filled & Capped (IPC-4761 Type VII)** for these "
        f"barrels — {stack.via_drill:g} mm drill, inside the fab's {stack.via_fill_min_drill:g} to "
        f"{stack.via_fill_max_drill:g} mm filled range. Tenting is a mask dam over the mouth of the hole, "
        "not a plug: a tented barrel under a reflowed land wicks the paste down the hole and starves the "
        "joint. Filled and capped is a **paid option and not the default**; a board ordered tented has "
        "holes in its thermal land.",
        "",
        "These are IC / heatsink lands. The passive rule is unchanged and unrelaxed: a via inside a "
        "resistor's, capacitor's, inductor's or connector-peg pad still fails this gate, filled or not.",
    ]
    return lines


# The one date the fab package carries (the reproducible-builds convention: `SOURCE_DATE_EPOCH` when
# set, else this constant). KiCad stamps the wall clock into every Gerber (`%TF.CreationDate` and the
# `G04 ... date` comment), the job file, both drill files, the drill maps and `drc.json`, and ignores
# `SOURCE_DATE_EPOCH` (measured with kicad-cli 10.0.6), so two builds of one board differed in 20 of
# blinky's 60 files and the stamp's hashes with them (fifth review; direction.md section 8: byte for byte).
FAB_EPOCH = 315532800  # 1980-01-01T00:00:00Z, the ZIP epoch


def _fab_date():
    import datetime

    try:
        epoch = int(os.environ.get("SOURCE_DATE_EPOCH", FAB_EPOCH))
    except ValueError:
        epoch = FAB_EPOCH
    return datetime.datetime.fromtimestamp(epoch, tz=datetime.timezone.utc)


_ISO_DT = re.compile(rb"\d{4}-\d\d-\d\d([T ])\d\d:\d\d:\d\d([+-]\d\d:?\d\d)?")
_PDF_DT = re.compile(rb"\(D:\d{4}:\d\d:\d\d:\d\d:\d\d:\d\d\)")


def pin_dates(out_dir: Path) -> list[str]:
    """Every wall-clock date KiCad wrote into the fab exports rewritten to `_fab_date()`, in the same
    spelling (a `T` or a space, the zone offset's form, the PDF's `D:` form at its own length, so no
    PDF offset moves). Returns the files it changed. Run after the exports and before the package is
    hashed (`build._stamp_fab`)."""
    when = _fab_date()

    def iso(m: re.Match) -> bytes:
        sep = m.group(1).decode()
        out = when.strftime(f"%Y-%m-%d{sep}%H:%M:%S")
        tz = m.group(2)
        if tz:
            out += "+00:00" if b":" in tz else "+0000"
        return out.encode()

    pdf = when.strftime("(D:%Y:%m:%d:%H:%M:%S)").encode()
    files = [*(out_dir / "gerbers").glob("*"), *out_dir.glob("*.drl"), *out_dir.glob("*.pdf"), out_dir / "drc.json"]
    changed = []
    for f in files:
        if not f.is_file():
            continue
        raw = f.read_bytes()
        new = _PDF_DT.sub(pdf, raw) if f.suffix == ".pdf" else _ISO_DT.sub(iso, raw)
        if new != raw:
            f.write_bytes(new)
            changed.append(f.name)
    return changed


# What a fab package is made of: on a refusal every one of these goes, so a refused fab never leaves
# a BOM, a CPL, a Gerber or the refused board standing where a package would be (fifth review). The
# refused board is kept for diagnosis as `refused.kicad_pcb`, with its sidecars, and `report.json`,
# `FAB_NOTES.md` and `drc.json` say why.
PACKAGE_FILES = ("bom.csv", "cpl.csv", "pos_kicad.csv")


def _clear_refused(out_dir: Path) -> None:
    for name in PACKAGE_FILES:
        f = out_dir / name
        if f.exists():
            f.unlink()
    for pat in ("*.drl", "*-drl_map.*"):
        for f in out_dir.glob(pat):
            f.unlink()
    if (out_dir / "gerbers").exists():
        shutil.rmtree(out_dir / "gerbers")
    for ext in (".kicad_pcb", ".kicad_pro", ".kicad_dru", ".kicad_prl"):
        f = out_dir / f"layout{ext}"
        if f.exists():
            f.replace(out_dir / f"refused{ext}")


def _write_notes(out_dir: Path, job: CompiledJob, result: dict) -> None:
    if result.get("error"):
        _clear_refused(out_dir)
        result["refused_board"] = str(out_dir / "refused.kicad_pcb")
    vip = result.get("via_in_pad") or []
    fids = result.get("fiducials") or []
    miss = result.get("missing_lcsc") or []
    lines = [
        "# Fab notes",
        "",
        f"Board {job.board_size_mm[0]:g}×{job.board_size_mm[1]:g} mm, {job.layers} layer, stackup `{job.stackup}`.",
        "",
        "## Fiducials",
        "",
    ]
    if fids:
        for f in fids:
            lines.append(f"- {f['ref']} at ({f['at'][0]:g}, {f['at'][1]:g}) — 1 mm Cu, 2 mm mask, F.Cu")
        lines.append("- JLCPCB Standard SMT wants 3–4 fiducials ≥ 3.35 mm from the edge. Rails (5 mm) can be added at order time.")
    else:
        lines.append("- None in the file. Ask JLCPCB to add rails + fiducials, or re-run with insert.")
    lines += ["", "## Via-in-pad", ""]
    blockers = result.get("via_in_pad_blockers") or []
    if vip:
        inside = sum(1 for h in vip if h.get("inside"))
        # Two different lists and, until S6 put nine deliberate barrels in one of them, one sentence
        # over both: the header said "they fail this gate" and then printed `blockers or vip`, so a
        # board with **no** blocker at all listed its allowed underpad vias under a sentence saying
        # they failed. c3_usb's two IC-pin grazes read that way before the array arrived and made it
        # loud. The blockers are named as blockers, and the rest are named as what they are.
        lines.append(
            f"{len(vip)} via(s) touch an SMT pad's copper, {inside} of them wholly inside it. "
            "USB-C and IC underpad may stay (filled + capped, IPC-4761 Type VII). "
            "Passives and connector mounting pegs must be dog-boned."
        )
        if blockers:
            lines.append("")
            lines.append(f"**{len(blockers)} of them fail this gate** and the board cannot be assembled as it stands:")
            for h in blockers[:40]:
                lines.append(f"- {h['pad']} @ ({h['via'][0]:.3f}, {h['via'][1]:.3f})")
        allowed = [h for h in vip if h not in blockers]
        if allowed:
            lines.append("")
            lines.append(f"{len(allowed)} underpad via(s) are allowed (USB-C / IC); order filled + capped:")
            for h in allowed[:40]:
                lines.append(f"- {h['pad']} @ ({h['via'][0]:.3f}, {h['via'][1]:.3f})")
    else:
        lines.append("- None detected.")
    lines += _thermal_notes(job, result.get("thermal") or ())
    lines += ["", "## BOM LCSC", ""]
    if miss:
        lines.append("Missing LCSC on footprint property: " + ", ".join(miss))
    else:
        lines.append("Every BOM row has an LCSC code from the footprint LCSC property.")
    lines.append(
        "Through-hole footprints without LCSC (modules, pin headers) are omitted "
        "from the JLC BOM — solder them after SMT."
    )
    lines += [
        "",
        "## Do not",
        "",
    ]
    if blockers or miss:
        lines.append("- Order until via-in-pad blockers and LCSC rows are accepted.")
    lines += [
        "- Upload from this toolchain; zip `fab/` locally and order yourself.",
        "- Re-run seed on `fab/`.",
        "",
    ]
    if job.constraints is not None:
        # The numbers the board was routed to, each with its source (`pcbc check --constraints`).
        lines += ["## Constraints", ""]
        lines += [f"- {line}" for line in job.constraints.lines]
        errors = sum(1 for r in job.dru if r.severity == "error")
        soft = sum(1 for r in job.dru if r.severity == "warning" and soft_kind(r.name) is not None)
        canary = f", canary on net {job.constraints.canary_net}" if job.constraints.canary_net else ""
        lines += [f"- rules: {len(job.dru)} written ({errors} error, {soft} soft){canary}", ""]
    rows = result.get("ampacity_bottleneck") or {}
    if rows:
        # The number a person checking the electrics wants, and the one the board was quietly failing
        # while the gate read the net's widest track (S7 review, finding 1): what the **narrowest**
        # copper on each rail's worst pad-to-pad path can carry, vias included.
        lines += ["## Power, end to end", ""]
        lines += [f"- {line}" for line in bottleneck_lines(rows)]
        lines += [""]
    if result.get("error"):
        lines += ["## Error", "", result["error"], ""]
    (out_dir / "FAB_NOTES.md").write_text("\n".join(lines))
    (out_dir / "report.json").write_text(json.dumps(_relative(result, out_dir.parent), indent=2, default=str) + "\n")


def _relative(value, root: Path):
    """`value` with every path string under `root` (the layout directory) written relative to it, so
    two builds of one board from two absolute paths write the same `report.json` (third review D3).
    The one absolute path left is `kicad_cli`, which is the machine's and not the build's."""
    prefix = str(Path(root).resolve()) + os.sep
    if isinstance(value, str):
        return value.replace(prefix, "") if prefix in value else value
    if isinstance(value, dict):
        return {k: _relative(v, root) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_relative(v, root) for v in value]
    return value
