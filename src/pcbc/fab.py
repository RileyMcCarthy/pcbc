"""JLCPCB fab package: Gerbers, drill, BOM/CPL, fiducials, via-in-pad notes."""

from __future__ import annotations

import csv
import io
import json
import math
import re
import shutil
import subprocess
from pathlib import Path

from .apply import write_dru
from .check import check_job
from .compile import CompiledJob
from .copper import power_ampacity_failures, unrouted_nets, vias_on_no_via_nets
from .dru import soft_kind
from .project import copy_with_siblings
from .silk import silk_job
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

    Measured on the S5 boards: three overlaps no number mentioned before — c3_usb's via at
    (14.75,1.5) over `U1.27` by 0.2 mm and (15.55,1.45) over `U1.26` by 0.15 mm, node's (17,2.4) over
    `U1.51` by 0.025 mm, all of them KRT's leftover vias on IC pins and all same-net.
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
    uid = stable_uuid("fiducial", ref)
    puid = stable_uuid("fiducial", ref, "ref")
    return f'''	(footprint "Fiducial_1mm_Mask2mm"
		(layer "F.Cu")
		(uuid "{uid}")
		(at {x:.4f} {y:.4f})
		(property "Reference" "{ref}"
			(at 0 -1.95 0)
			(layer "F.SilkS")
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
		)
		(pad "" smd circle
			(at 0 0)
			(size 1 1)
			(layers "F.Cu" "F.Mask")
			(solder_mask_margin 0.5)
			(uuid "{puid}")
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


def insert_fiducials(
    text: str, size_mm: tuple[float, float], inset: float = 4.0, spots: list[tuple[str, float, float]] | None = None
) -> tuple[str, list[tuple[str, float, float]]]:
    existing = [footprint_reference(text[s:e]) for s, e in board_footprint_spans(text)]
    if any(r and r.startswith("FID") for r in existing):
        placed = []
        for s, e in board_footprint_spans(text):
            b = text[s:e]
            r = footprint_reference(b)
            at = footprint_at(b)
            if r and r.startswith("FID") and at:
                placed.append((r, at[0], at[1]))
        return text, placed
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
    return {
        "cmd": cmd,
        "returncode": proc.returncode,
        "stdout": (proc.stdout or "")[-2000:],
        "stderr": (proc.stderr or "")[-1500:],
    }


# Silk / dangling vias from unfilled zones. Same-footprint pad-pad is a KiCad
# AABB false short on rotated modules — DRC owns those. diff_pair_gap is
# ignored if a router rewrote .kicad_dru; fab restores compiled classes.
_IGNORE_DRC = {
    "silk_overlap",
    "silk_over_copper",
    "silk_edge_clearance",
    "silk_mask_clearance",
    "via_dangling",
    "track_dangling",
    # KRT rewrites the 2-layer pair gap; removing this is an R4 item (docs/r1-design.md E).
    "diff_pair_gap_out_of_range",
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
        if typ == "clearance":
            m = _ACTUAL_MM.search(v.get("description") or "")
            if m and float(m.group(1)) + 1e-9 >= floor_mm:
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
    copy_with_siblings(pcb, work)
    # Restore compiled USB pair-gap. Do not rewrite .kicad_pro: KRT already
    # lowered Default/Power clearance to the routed floor (0.10 on 2-layer).
    write_dru(job, work)
    text = work.read_text()
    fids: list[tuple[str, float, float]] = []
    if insert_fids:
        text, fids = insert_fiducials(text, job.board_size_mm)
        work.write_text(text)
    silk_rep = silk_job(job, work, backup=False)
    text = work.read_text()

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
    write_bom_csv(bom_rows, out_dir / "bom.csv")
    vip = via_in_pad(text)
    # The design, not the reference string, is what says a part is a passive whose pad a via wicks
    # (finding 1). `build_job` hands it over; an audit of a bare board file falls back to the ref.
    blockers = via_in_pad_blockers(vip, design)

    cli = kicad_cli()
    steps: list[dict] = []
    result = {
        "fab": str(out_dir),
        "pcb": str(work),
        "kicad_cli": str(cli),
        "fiducials": [{"ref": n, "at": [x, y]} for n, x, y in fids],
        "bom_rows": len(bom_rows),
        "missing_lcsc": missing_lcsc,
        "via_in_pad": vip,
        "via_in_pad_blockers": blockers,
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
        work.write_text(pin_all_uuids(work.read_text(), work.parent.name, "fab"))
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
    amp = power_ampacity_failures(job, text)
    result["ampacity"] = amp
    if amp:
        result["error"] = "; ".join(amp)
        _write_notes(out_dir, job, result)
        return result

    layers = copper_gerber_layers(job.layers)
    steps.append(
        _run(
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
    )
    steps.append(
        _run(
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
    )
    gfiles = list(gerber_dir.glob("*"))
    result["gerbers"] = len(gfiles)
    if not gfiles:
        result["error"] = "kicad-cli wrote no gerbers"
    if missing_lcsc:
        result["error"] = result.get("error") or f"BOM missing LCSC for {', '.join(missing_lcsc)}"
    _write_notes(out_dir, job, result)
    return result


def _write_notes(out_dir: Path, job: CompiledJob, result: dict) -> None:
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
        lines.append(
            f"{len(vip)} via(s) touch an SMT pad's copper, {inside} of them wholly inside it. "
            "USB-C underpad may stay (filled + capped, IPC-4761 Type VII). "
            "Passives and connector mounting pegs must be dog-boned — they fail this gate:"
        )
        for h in (blockers or vip)[:40]:
            lines.append(f"- {h['pad']} @ ({h['via'][0]:.3f}, {h['via'][1]:.3f})")
        allowed = [h for h in vip if h not in blockers]
        if allowed and blockers:
            lines.append(
                f"{len(allowed)} underpad via(s) remain (USB-C / IC); order filled + capped."
            )
    else:
        lines.append("- None detected.")
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
    if result.get("error"):
        lines += ["## Error", "", result["error"], ""]
    (out_dir / "FAB_NOTES.md").write_text("\n".join(lines))
    (out_dir / "report.json").write_text(json.dumps(result, indent=2, default=str) + "\n")
