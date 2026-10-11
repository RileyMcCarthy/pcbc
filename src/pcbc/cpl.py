"""The placement file (CPL) JLCPCB places by: each part's rotation and centre *in JLC's own footprint's frame*,
registered against that footprint, never KiCad's `(at x y rot)` copied (docs/direction.md §4c).

JLC's machine turns a part from the zero of **its** footprint for the part's LCSC number (the EasyEDA footprint,
which `easyeda2kicad` writes with `(property "LCSC Part" "C…")`). A KiCad-library footprint has its own zero:
KiCad's SOT-23-5 has pad 1 top-left, JLC's SOT-23-5 for the LMV321 (C7972) bottom-right — 180 degrees apart —
and KiCad's SOT-23-6 sits 90 degrees off JLC's for the TPS54202 (C191884). So per part:

- **JLC's footprint.** The `.kicad_mod` whose `LCSC Part` property is the part's LCSC number: the board footprint
  itself when it is one (identity), else one on disk beside the board's parts (`components/**`, the part's own
  folder) or in the checkout's `components/` (`pcbc fetch C… --into components`). A copied board that brings
  only its module still finds a part fetched into the checkout.
- **Registration.** For each quarter turn `θ` of JLC's footprint, every numbered pad's centre turned by `θ` and
  moved by `t` (the least-squares shift over the pads) must land inside the board footprint's pad of the same
  pin, and every board pad must be landed on: a terminal on its own pad. Pads pair by number (a package's pin
  numbers are its datasheet's in both libraries), except a two-terminal part marked at its cathode or `+`
  (§4c), where numbering is a library's choice: its cathode pad pairs with the pad JLC's own symbol (the
  `.kicad_sym` beside JLC's footprint, same `LCSC Part`) names the cathode. Exactly one `θ` must land; then
  JLC's rotation is the footprint's KiCad rotation plus `θ`, and JLC's centre is KiCad's plus `t` turned with
  the part.
- **Facts.** No JLC footprint on disk, pads that land at no turn or at two, a JLC symbol that names no cathode:
  the part is not registered, and the fact says which part, which LCSC number and what is missing. For a part
  whose orientation matters (§4c G1: it needs or gets a mark) that fails fab; a part that is its own turned
  self (a resistor, a ceramic capacitor, a plain header) keeps KiCad's rotation with the fact in the notes —
  turned 180 degrees it is the same part, and turned 90 no terminal lands on a pad (G1 measures both), so a
  zero 90 degrees off is a misfit JLC's placement preview shows, not a wrong part.
"""

from __future__ import annotations

import math
import re
from pathlib import Path

LAND_TOL = 0.01  # a turned pad centre lands on a pad when inside its copper by this much slack (mm)
_LCSC_PART = re.compile(r'\(property\s+"LCSC Part"\s+"([^"]*)"')
CATHODE = ("K", "KA", "CATHODE", "C", "-")  # a JLC symbol's cathode pin names: KiCad's K, EasyEDA's "-", Everlight's C (C72043)


def lcsc_of(text: str) -> str | None:
    """The `LCSC Part` a footprint or symbol text records (easyeda2kicad writes it), or None."""
    m = _LCSC_PART.search(text)
    return m.group(1).strip().upper() if m and m.group(1).strip() else None


def repo_components() -> Path | None:
    """The checkout's `components/` (`pcbc fetch --into components`), or None when this tree has none."""
    for cand in Path(__file__).resolve().parents:
        if (cand / "pyproject.toml").is_file() and (cand / "src" / "pcbc").is_dir():
            lib = cand / "components"
            return lib if lib.is_dir() else None
    return None


def library_roots(*extra: Path | None) -> list[Path]:
    """Directories a board's JLC footprints are read from: the ones it was given (its own `components/`), then the
    checkout's `components/` when that is a different directory."""
    roots: list[Path] = []
    seen: set[Path] = set()
    for raw in (*extra, repo_components()):
        if raw is None:
            continue
        path = Path(raw)
        key = path.resolve()
        if key in seen:
            continue
        seen.add(key)
        roots.append(path)
    return roots


def jlc_library(roots) -> dict[str, dict[str, list[Path]]]:
    """`{LCSC number: {"mod": [.kicad_mod ...], "sym": [.kicad_sym ...]}}` of every file under `roots` (directories,
    searched recursively, or files) that records its LCSC number."""
    out: dict[str, dict[str, list[Path]]] = {}
    seen: set[Path] = set()
    for root in roots:
        root = Path(root)
        if not root.exists():
            continue
        files = [root] if root.is_file() else sorted(p for p in root.rglob("*") if p.suffix in (".kicad_mod", ".kicad_sym"))
        for f in files:
            rf = f.resolve()
            if rf in seen:
                continue
            seen.add(rf)
            try:
                lc = lcsc_of(f.read_text(errors="replace"))
            except OSError:
                continue
            if lc:
                out.setdefault(lc, {"mod": [], "sym": []})["mod" if f.suffix == ".kicad_mod" else "sym"].append(f)
    return out


def _pads(specs) -> list[dict]:
    """Numbered copper pads of a footprint (library frame): num, centre, half extents along x and y."""
    out = []
    for sp in specs:
        if not sp.num or sp.kind == "np_thru_hole":
            continue
        w, h = sp.w, sp.h
        if sp.custom is not None:
            w, h = max(w, sp.custom[0]), max(h, sp.custom[1])
        a = math.radians(sp.angle)
        hx = abs(w / 2 * math.cos(a)) + abs(h / 2 * math.sin(a))
        hy = abs(w / 2 * math.sin(a)) + abs(h / 2 * math.cos(a))
        out.append({"num": str(sp.num), "x": sp.x, "y": sp.y, "hx": hx, "hy": hy})
    return out


def _turn(x: float, y: float, deg: float) -> tuple[float, float]:
    """A point of a footprint turned `deg` the way KiCad turns a footprint by its `(at x y deg)` (y down)."""
    from .silk import _rot_pt

    return _rot_pt(x, y, deg)


def register(board_specs, jlc_specs, pairs: dict[str, str] | None = None) -> dict:
    """Registers JLC's footprint (`jlc_specs`, its `PadSpec`s) on the board footprint (`board_specs`, library frame).

    `pairs`: `{board pad number: JLC pad number}` when the numbering differs (a cathode), else pads pair by number.
    Returns `{"theta": the quarter turn, or None, "t": (dx, dy) in the board footprint's frame, "landed": [θ that
    land], "worst": {θ: (the worst pad, how far its turned centre is outside the board pad's copper, mm)}, "why":
    a fact when it does not register}`."""
    B = _pads(board_specs)
    J = _pads(jlc_specs)
    rename = pairs or {}
    back = {v: k for k, v in rename.items()}
    J = [{**p, "num": back.get(p["num"], p["num"]) if rename else p["num"]} for p in J]
    bn = sorted({p["num"] for p in B})
    jn = sorted({p["num"] for p in J})
    out: dict = {"theta": None, "t": (0.0, 0.0), "landed": [], "worst": {}, "why": None}
    if bn != jn:
        only_b = [n for n in bn if n not in jn]
        only_j = [n for n in jn if n not in bn]
        out["why"] = "its pads are not JLC's: " + "; ".join(
            s for s in (f"pad {', '.join(only_b[:6])} only on the board footprint" if only_b else "", f"pad {', '.join(only_j[:6])} only on JLC's" if only_j else "") if s
        )
        return out
    single = [n for n in bn if sum(p["num"] == n for p in B) == 1 and sum(p["num"] == n for p in J) == 1]
    for th in (0, 90, 180, 270):
        tj = [{**p, "tx": _turn(p["x"], p["y"], th)[0], "ty": _turn(p["x"], p["y"], th)[1]} for p in J]
        if single:  # the shift that best lays the pads one number each on theirs
            bx = [next(q for q in B if q["num"] == n) for n in single]
            jx = [next(q for q in tj if q["num"] == n) for n in single]
            dx = sum(b["x"] - j["tx"] for b, j in zip(bx, jx)) / len(single)
            dy = sum(b["y"] - j["ty"] for b, j in zip(bx, jx)) / len(single)
        else:  # every number on two pads or more: centre on centre
            dx = sum(q["x"] for q in B) / len(B) - sum(q["tx"] for q in tj) / len(tj)
            dy = sum(q["y"] for q in B) / len(B) - sum(q["ty"] for q in tj) / len(tj)
        worst = (None, -math.inf)

        def outside(px, py, q) -> float:
            return max(abs(px - q["x"]) - q["hx"], abs(py - q["y"]) - q["hy"])

        for j in tj:  # every JLC terminal lands inside a board pad of its pin
            d = min(outside(j["tx"] + dx, j["ty"] + dy, q) for q in B if q["num"] == j["num"])
            if d > worst[1]:
                worst = (j["num"], d)
        for q in B:  # and every board pad gets a terminal
            d = min(outside(j["tx"] + dx, j["ty"] + dy, q) for j in tj if j["num"] == q["num"])
            if d > worst[1]:
                worst = (q["num"], d)
        out["worst"][th] = (worst[0], round(max(0.0, worst[1]), 3))
        if worst[1] <= LAND_TOL:
            out["landed"].append(th)
            if out["theta"] is None:
                out["t"] = (round(dx, 4), round(dy, 4))
    if len(out["landed"]) == 1:
        out["theta"] = out["landed"][0]
    else:
        out["t"] = (0.0, 0.0)
        if not out["landed"]:
            th, (num, d) = min(out["worst"].items(), key=lambda kv: kv[1][1])
            out["why"] = f"JLC's pads land on the board footprint's at no quarter turn (the nearest, {th} degrees, puts pad {num} {d:.2f} mm off its pad)"
        else:
            out["why"] = f"JLC's pads land on the board footprint's at {' and '.join(str(t) for t in out['landed'])} degrees alike, so its zero is not told apart"
    return out


def _cathode(pins: dict[str, tuple[str, ...]]) -> tuple[str, str] | None:
    """(the pin name, its one pad) of a two-terminal part's cathode or `+`, by name, else None."""
    from .orient import CATHODE as K, PLUS

    for name, pads in pins.items():
        if (name.upper() in K or name in PLUS) and len(pads) == 1:
            return name, str(pads[0])
    return None


def _jlc_cathode(sym: Path, plus: bool) -> str | None:
    from .symbol import parse_symbol_pins

    pins = parse_symbol_pins(sym)
    for name, pin in pins.items():
        if len(pin.pads) != 1:
            continue
        if (plus and name == "+") or (not plus and name.upper() in CATHODE):
            return str(pin.pads[0])
    return None


def plan(parts: list[dict], library: dict) -> list[dict]:
    """One row per part (`parts`: `{"ref", "lcsc", "at": (x, y, rot), "side", "specs": board footprint PadSpecs
    in its library frame, "own": the LCSC Part its own footprint records or None, "pins": {name: pads}, "need":
    orient's G1 need or None, "shape": orient's mark shape or None}`), in ref order:

    `{"ref", "lcsc", "kicad": (x, y, rot), "jlc": (x, y, rot) or None, "theta", "t", "evidence": words, "fact":
    None or the fact, "fails": whether the fact fails fab}`."""
    from .pads import pad_specs

    rows = []
    for p in sorted(parts, key=lambda q: q["ref"]):
        x, y, rot = p["at"]
        row = {"ref": p["ref"], "lcsc": p.get("lcsc") or "", "kicad": (x, y, rot), "jlc": None, "theta": None, "t": (0.0, 0.0), "evidence": "", "fact": None, "fails": False}
        matters = p.get("need") not in (None, "none")
        lc = (p.get("lcsc") or "").strip().upper()
        if not lc:
            row["evidence"] = "no LCSC number: not placed by JLC"
            row["jlc"] = (x, y, rot)
            rows.append(row)
            continue
        if p.get("side", "Top") != "Top":
            row["fact"] = f"{p['ref']} ({lc}): CPL rotation not registered: the part is on the back, and JLC's back-side zero is not measured here"
            row["fails"] = matters
            row["jlc"] = (x, y, rot)
            rows.append(row)
            continue
        if p.get("own") == lc:
            row.update(jlc=(x, y, rot % 360.0), theta=0, evidence=f"the board footprint is JLC's own for {lc} (its LCSC Part)")
            rows.append(row)
            continue
        got = library.get(lc) or {}
        mods = got.get("mod") or []
        if not mods:
            whose = f"the board footprint records LCSC Part {p['own']}, another part" if p.get("own") else "the board footprint is not JLC's (it records no LCSC Part)"
            row["fact"] = f"{p['ref']} ({lc}): CPL rotation not registered: no JLC footprint for {lc} on disk (a .kicad_mod whose LCSC Part is {lc}); {whose}"
            if not matters:
                row["fact"] += "; KiCad's rotation is written: turned 180 degrees the part is itself, and turned 90 no terminal lands on a pad (§4c G1)"
            row["fails"] = matters
            row["jlc"] = None if matters else (x, y, rot)
            rows.append(row)
            continue
        jmod = mods[0]
        pairs = None
        cath = _cathode(p.get("pins") or {}) if p.get("shape") in ("bar", "plus") else None
        if cath is not None:
            syms = got.get("sym") or []
            jk = _jlc_cathode(syms[0], cath[0] == "+") if syms else None
            if jk is None:
                what = "no JLC symbol for it on disk" if not syms else f"JLC's symbol {syms[0].name} names no {'+' if cath[0] == '+' else 'cathode'} pin"
                row["fact"] = f"{p['ref']} ({lc}): CPL rotation not registered: {p['ref']}'s {cath[0]} is pad {cath[1]}, and which of the pads of JLC's footprint {jmod.name} is the {'+' if cath[0] == '+' else 'cathode'} is not known: {what}"
                row["fails"] = matters
                rows.append(row)
                continue
            other_b = next(n for n in {str(s.num) for s in p["specs"] if s.num} if n != cath[1])
            other_j = next(n for n in {str(s.num) for s in pad_specs(jmod.read_text()) if s.num} if n != jk)
            pairs = {cath[1]: jk, other_b: other_j}
        reg = register(p["specs"], pad_specs(jmod.read_text()), pairs)
        if reg["theta"] is None:
            row["fact"] = f"{p['ref']} ({lc}): CPL rotation not registered against JLC's footprint {jmod.name}: {reg['why']}"
            row["fails"] = matters
            rows.append(row)
            continue
        th, (tx, ty) = reg["theta"], reg["t"]
        wx, wy = _turn(tx, ty, rot)
        row.update(theta=th, t=(tx, ty), jlc=(round(x + wx, 4), round(y + wy, 4), (rot + th) % 360.0))
        how = f" (its {cath[0]}, pad {cath[1]}, on JLC's pad {pairs[cath[1]]})" if pairs else ""
        row["evidence"] = f"JLC's footprint {jmod.name} for {lc} lands pad for pad{how} turned {th} degrees and moved ({tx:g}, {ty:g}) mm; at every other quarter turn " + ", ".join(
            f"{a}: pad {n} {d:.2f} mm off" for a, (n, d) in sorted(reg["worst"].items()) if a != th
        )
        rows.append(row)
    return rows


def cpl_csv(rows: list[dict], side: dict[str, str]) -> str:
    """JLC's CPL from `plan`'s rows: Designator, Mid X, Mid Y (negated: the file is y down, JLC's y up), Rotation,
    Layer. A part with no JLC placement is written at KiCad's (fab has failed on it, or it is not JLC's to place)."""
    import csv
    import io

    out = io.StringIO()
    w = csv.writer(out, lineterminator="\n")
    w.writerow(["Designator", "Mid X", "Mid Y", "Rotation", "Layer"])
    for r in rows:
        x, y, rot = r["jlc"] or r["kicad"]
        w.writerow([r["ref"], f"{x:.6f}", f"{-y:.6f}", f"{rot % 360.0:.6f}", side.get(r["ref"], "Top")])
    return out.getvalue()


def row_line(r: dict) -> str:
    """A part's CPL row in words, for FAB_NOTES."""
    kx, ky, kr = r["kicad"]
    if r["fact"]:
        return r["fact"]
    jx, jy, jr = r["jlc"]
    if r["theta"] in (None, 0) and (jx, jy) == (kx, ky):
        return f"{r['ref']} ({r['lcsc'] or 'no LCSC'}): rotation {jr % 360:g}, KiCad's: {r['evidence']}"
    return f"{r['ref']} ({r['lcsc']}): rotation {kr % 360:g} -> {jr % 360:g}, centre ({kx:g}, {-ky:g}) -> ({jx:g}, {-jy:g}): {r['evidence']}"


def note_lines(rows: list[dict]) -> list[str]:
    """`row_line` for every row, the parts with one fact but for their reference (the same LCSC number, the same
    footprint) written once, their references joined: the notes' form."""
    out: list[str] = []
    groups: dict[str, list[str]] = {}
    order: list[str] = []
    for r in rows:
        line = row_line(r)
        if r["fact"] and not r["fails"] and line.startswith(f"{r['ref']} ("):
            key = line[len(r["ref"]) :]
            if key not in groups:
                groups[key] = []
                order.append(key)
            groups[key].append(r["ref"])
            continue
        out.append(line)
    out += [", ".join(groups[k]) + k for k in order]
    return out


def board_rows(text: str, design=None, roots=()) -> list[dict]:
    """`plan` over a board text (the fab board): every footprint but the fiducials and those excluded from position
    files, with its LCSC from its `LCSC` property, its pads, and — with `design` — its pins and §4c need."""
    from .orient import need, part_of, pins_of
    from .pads import pad_specs
    from .sexp import board_footprint_spans, footprint_at, footprint_reference

    insts = {i.ref: i for i in design.instances} if design is not None else {}
    parts = []
    lib_roots = list(roots)
    if design is not None:
        from .footprints import footprint_path

        for inst in design.instances:
            try:
                fp = footprint_path(inst.part)
            except FileNotFoundError:
                continue
            lib_roots.append(fp.parent if inst.part.origin else fp)
    library = jlc_library(lib_roots)
    for start, end in board_footprint_spans(text):
        block = text[start:end]
        ref = footprint_reference(block)
        if not ref or ref.startswith("FID") or "exclude_from_pos_files" in block:
            continue
        at = footprint_at(block)
        if not at:
            continue
        from .fab import footprint_props, footprint_side

        props = footprint_props(block)
        specs = [_lib_frame(sp, at[2]) for sp in pad_specs(block)]
        inst = insts.get(ref)
        n = None
        if inst is not None:
            op = part_of(ref, (0.0, 0.0, 0.0), specs, body=None, pins=pins_of(inst), kind=inst.part.kind, prefix=inst.part.prefix)
            n = need(op)
        own = lcsc_of(block)
        if inst is not None and own is None:
            try:
                from .footprints import footprint_path

                own = lcsc_of(footprint_path(inst.part).read_text())
            except (FileNotFoundError, OSError):
                own = None
        parts.append(
            {
                "ref": ref,
                "lcsc": props.get("LCSC", ""),
                "at": at,
                "side": footprint_side(block),
                "specs": specs,
                "own": own,
                "pins": pins_of(inst) if inst is not None else {},
                "need": n["need"] if n else None,
                "shape": n["shape"] if n else None,
            }
        )
    return plan(parts, library)


def _lib_frame(sp, rot: float):
    """A board file's pad (its angle library plus footprint) in its library frame."""
    from dataclasses import replace

    return replace(sp, angle=(sp.angle - rot) % 360.0)
