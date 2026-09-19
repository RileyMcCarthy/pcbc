"""Parts from LCSC / EasyEDA, or any .kicad_sym + .kicad_mod: search, fetch, score.

`pcbc search` asks the public jlcsearch API. `pcbc fetch C…` runs easyeda2kicad,
upgrades the files to the current KiCad format with kicad-cli, splits them into
`components/<Mfr>/<MPN>/` next to a generated `part.py`, and scores them.
`pcbc score` measures any part the same way, wherever its files came from, so
two candidate libraries for one chip can be compared before `load()`-ing one.

The score is what pcbc needs from a library, not taste: pin numbers that are the
footprint's pads, pins on the 1.27 mm grid with room for their numbers, named and
typed pins (ERC), one unit, a courtyard and a body outline, a 3D model.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from .geom import _graphics_bbox, _iter_tagged, _pads_bbox, box_size_mm
from .symbol import _CHAR_W, _pins_geom, extract_main_symbol, parse_symbol_layout, symbol_units

JLCSEARCH = "https://jlcsearch.tscircuit.com/api/search"
USER_AGENT = "pcbc/0.1 (+https://github.com/RileyMcCarthy/pcbc)"
GRID = 1.27

_PROP = re.compile(r'\(property\s+"([^"]+)"\s+"([^"]*)"', re.S)
_PAD_NUM = re.compile(r'\(pad\s+"?([^"\s)]*)"?')
_PAD_SIZE = re.compile(r"\(size\s+([0-9.+-]+)\s+([0-9.+-]+)\)")
_MODEL = re.compile(r'\(model\s+"([^"]+)"')
_UUID_LINE = re.compile(r'^[ \t]*\(uuid\s+"[^"]*"\)[ \t]*\n', re.M)
_BODY = re.compile(r"L(\d+(?:\.\d+)?)-W(\d+(?:\.\d+)?)")

# ki_keywords (EasyEDA's category) → reference prefix. First match wins.
_PREFIX = (
    ("R", ("resistor",)),
    ("C", ("capacitor",)),
    ("L", ("inductor", "ferrite", "choke")),
    ("J", ("connector", "usb", "header", "socket", "terminal", "jack", "receptacle")),
    ("Q", ("mosfet", "transistor", "bjt", "jfet", "igbt")),
    ("D", ("diode", "tvs", "esd", "led", "rectifier", "schottky", "zener")),
    ("Y", ("crystal", "oscillator", "resonator")),
    ("SW", ("switch", "tactile", "button")),
    ("F", ("fuse",)),
    ("BT", ("battery",)),
    ("AE", ("antenna",)),
)

GOOD = 90
USABLE = 70


# ---------------------------------------------------------------- search


def search_lcsc(query: str, limit: int = 10, opener=None) -> list[dict]:
    """LCSC / JLC parts for a query. `opener` stands in for urlopen in tests."""
    url = JLCSEARCH + "?" + urllib.parse.urlencode({"q": query, "limit": int(limit)})
    open_fn = opener or urllib.request.urlopen
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with open_fn(req, timeout=20) as resp:
            raw = resp.read()
    except urllib.error.URLError as e:
        raise RuntimeError(f"LCSC search failed: {e}") from e
    data = json.loads(raw.decode() if isinstance(raw, (bytes, bytearray)) else raw)
    hits: list[dict] = []
    for row in data.get("components") or []:
        n = str(row.get("lcsc") or "")
        hits.append(
            {
                "lcsc": n if n.upper().startswith("C") else f"C{n}",
                "mpn": str(row.get("mfr") or ""),
                "package": str(row.get("package") or ""),
                "description": str(row.get("description") or ""),
                "stock": int(row.get("stock") or 0),
                "price_usd": float(row["price"]) if row.get("price") is not None else None,
                "basic": bool(row.get("is_basic")),
                "preferred": bool(row.get("is_preferred")),
            }
        )
    return hits


def format_hits(hits: list[dict]) -> str:
    if not hits:
        return "no parts"
    lines = [f"{'LCSC':<9} {'MPN':<24} {'package':<14} {'stock':>7} {'USD':>7}  note"]
    for h in hits:
        price = f"{h['price_usd']:.4f}" if h["price_usd"] is not None else "-"
        note = "basic" if h["basic"] else ("preferred" if h["preferred"] else "extended")
        lines.append(f"{h['lcsc']:<9} {h['mpn'][:24]:<24} {h['package'][:14]:<14} {h['stock']:>7} {price:>7}  {note}")
    return "\n".join(lines)


# ---------------------------------------------------------------- fetch


def find_easyeda2kicad() -> str | None:
    found = shutil.which("easyeda2kicad")
    if found:
        return found
    sibling = Path(sys.executable).parent / "easyeda2kicad"
    return str(sibling) if sibling.exists() else None


def _kicad_cli() -> Path | None:
    from .fab import kicad_cli

    cli = kicad_cli()
    if cli.exists() or shutil.which(str(cli)):
        return cli
    return None


def slug(text: str) -> str:
    """ASCII directory name: 'TI(德州仪器)' → 'TI', 'Bosch(博世)' → 'Bosch'."""
    text = re.sub(r"\([^)]*\)", "", text)
    text = re.sub(r"[^A-Za-z0-9._+-]+", "_", text.strip()).strip("_")
    return text or "unknown"


def guess_prefix(category: str) -> str:
    c = category.lower()
    for prefix, words in _PREFIX:
        if any(w in c for w in words):
            return prefix
    return "U"


def symbol_props(text: str) -> dict[str, str]:
    _name, block = extract_main_symbol(text)
    return dict(_PROP.findall(block))


def upgrade_symbol(src: Path, dst: Path, cli: Path | None) -> bool:
    """kicad-cli sym upgrade → dst. False (and a plain copy) when kicad-cli is missing."""
    if cli is not None:
        run = subprocess.run([str(cli), "sym", "upgrade", "--force", "-o", str(dst), str(src)], capture_output=True, text=True)
        if run.returncode == 0 and dst.exists():
            return True
    shutil.copyfile(src, dst)
    return False


def upgrade_footprint(src: Path, dst: Path, cli: Path | None) -> bool:
    """kicad-cli fp upgrade → dst, minus the random uuids it sprinkles (KiCad regrows them)."""
    if cli is not None:
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "up.pretty"
            run = subprocess.run([str(cli), "fp", "upgrade", "--force", "-o", str(out), str(src.parent)], capture_output=True, text=True)
            made = out / src.name
            if run.returncode == 0 and made.exists():
                dst.write_text(_UUID_LINE.sub("", made.read_text()))
                return True
    shutil.copyfile(src, dst)
    return False


def _relink_model(mod_text: str, model_name: str | None) -> str:
    """Point `(model …)` at a file beside the footprint; seed resolves it from the part dir."""
    if model_name is None:
        return mod_text
    return _MODEL.sub(f'(model "{model_name}"', mod_text, count=1)


def fetch_lcsc(
    lcsc_id: str,
    into: Path,
    *,
    force: bool = False,
    easyeda2kicad: str | None = None,
    kicad_cli: Path | None = None,
) -> dict:
    """easyeda2kicad → upgrade → components/<Mfr>/<MPN>/{part.py, sym, mod, step} → score."""
    lcsc_id = lcsc_id.strip().upper()
    if not lcsc_id.startswith("C"):
        lcsc_id = "C" + lcsc_id
    exe = easyeda2kicad or find_easyeda2kicad()
    if not exe:
        raise RuntimeError("easyeda2kicad is not installed: pip install easyeda2kicad")
    cli = kicad_cli if kicad_cli is not None else _kicad_cli()
    with tempfile.TemporaryDirectory() as td:
        prefix = Path(td) / "e"
        cmd = [exe, "--full", "--lcsc_id", lcsc_id, "--output", str(prefix.with_suffix(".kicad_sym")), "--overwrite"]
        run = subprocess.run(cmd, capture_output=True, text=True)
        sym_src = prefix.with_suffix(".kicad_sym")
        mods = sorted(Path(str(prefix) + ".pretty").glob("*.kicad_mod")) if Path(str(prefix) + ".pretty").is_dir() else []
        if run.returncode != 0 or not sym_src.exists() or not mods:
            raise RuntimeError(f"easyeda2kicad gave no symbol+footprint for {lcsc_id}: {(run.stderr or run.stdout).strip()[-400:]}")
        mod_src = mods[0]
        shapes = Path(str(prefix) + ".3dshapes")
        step = next(iter(sorted(shapes.glob("*.step"))), None) if shapes.is_dir() else None
        wrl = next(iter(sorted(shapes.glob("*.wrl"))), None) if shapes.is_dir() else None

        props = symbol_props(sym_src.read_text())
        mpn = props.get("MPN") or props.get("Value") or lcsc_id
        mfr = props.get("Manufacturer") or "unknown"
        category = props.get("ki_keywords") or props.get("ki_description") or ""
        dest = Path(into) / slug(mfr) / slug(mpn)
        dest.mkdir(parents=True, exist_ok=True)
        sym_name = f"{slug(mpn)}.kicad_sym"
        mod_name = mod_src.name
        upgraded_sym = upgrade_symbol(sym_src, dest / sym_name, cli)
        upgraded_mod = upgrade_footprint(mod_src, dest / mod_name, cli)
        # One model, STEP first (KiCad renders it; the .wrl is a legacy duplicate and 3D files are big).
        model_name = None
        src3d = step or wrl
        if src3d is not None:
            model_name = mod_src.stem + src3d.suffix
            shutil.copyfile(src3d, dest / model_name)
        mod_path = dest / mod_name
        mod_path.write_text(_relink_model(mod_path.read_text(), model_name))

    report = score_part(dest, symbol=sym_name, footprint=mod_name)
    part_py = dest / "part.py"
    wrote = False
    if force or not part_py.exists():
        part_py.write_text(
            part_py_text(
                mpn=mpn,
                manufacturer=slug(mfr).replace("_", " "),
                lcsc=lcsc_id,
                category=category,
                symbol=sym_name,
                footprint=mod_name,
                mod_text=mod_path.read_text(),
                pins=_pins_geom(extract_main_symbol((dest / sym_name).read_text())[1]),
                report=report,
            )
        )
        wrote = True
    if not upgraded_sym or not upgraded_mod:
        report["findings"].append(("warn", "kicad-cli not found: files kept in easyeda2kicad's KiCad 5 format; install KiCad and re-run fetch --force"))
    return {
        "lcsc": lcsc_id,
        "dir": str(dest),
        "part_py": str(part_py),
        "wrote_part_py": wrote,
        "symbol": sym_name,
        "footprint": mod_name,
        "model": model_name,
        "score": report,
    }


def part_py_text(
    *,
    mpn: str,
    manufacturer: str,
    lcsc: str,
    category: str,
    symbol: str,
    footprint: str,
    mod_text: str,
    pins: list[dict],
    report: dict,
) -> str:
    prefix = guess_prefix(category)
    kind = "th" if "(attr through_hole" in mod_text or ("thru_hole" in mod_text and " smd " not in mod_text) else "ic"
    outline = body_box(mod_text)
    body = f"    body_mm=({outline[0]:.2f}, {outline[1]:.2f}),\n" if outline else ""
    # Pin names as the .kicad_sym spells them: what board.py binds. Numbers in brackets.
    by_name: dict[str, list[str]] = {}
    for p in sorted(pins, key=lambda p: _pin_sort_key(p["number"])):
        name = p["name"] if p["name"] not in ("", "~") else p["number"]
        by_name.setdefault(name, []).append(p["number"])
    items = [f"{n}({','.join(nums)})" for n, nums in by_name.items()]
    pin_lines: list[str] = []
    line = "# Pins:"
    for it in items:
        if len(line) + len(it) + 1 > 96:
            pin_lines.append(line)
            line = "#      "
        line += " " + it
    pin_lines.append(line)
    cat = f"# {category}. " if category else "# "
    head = f"{cat}Fetched from LCSC {lcsc} by pcbc fetch; library quality {report['grade']} ({report['score']}/100).\n"
    kind_line = '    kind="th",\n' if kind == "th" else ""
    return (
        "from pcbc import Component\n\n"
        + head
        + "\n".join(pin_lines)
        + "\n"
        + "part = Component(\n"
        + f'    name="{mpn}",\n'
        + f'    prefix="{prefix}",\n'
        + f'    mpn="{mpn}",\n'
        + f'    manufacturer="{manufacturer}",\n'
        + f'    lcsc="{lcsc}",\n'
        + f'    footprint="{footprint}",\n'
        + f'    symbol="{symbol}",\n'
        + kind_line
        + body
        + ")\n"
    )


def _pin_sort_key(num: str) -> tuple:
    m = re.match(r"([A-Za-z]*)(\d+)(.*)", num)
    if m:
        return (m.group(1), int(m.group(2)), m.group(3))
    return (num, 0, "")


# ---------------------------------------------------------------- score


def score_symbol(text: str) -> dict:
    """Findings (severity, message) and pin facts for one .kicad_sym."""
    findings: list[tuple[str, str]] = []
    try:
        _name, block = extract_main_symbol(text)
    except ValueError:
        return {"findings": [("fail", "no symbol in the library")], "pins": [], "penalty": 100}
    pins = _pins_geom(block)
    penalty = 0

    def add(sev: str, pts: int, msg: str) -> None:
        nonlocal penalty
        findings.append((sev, msg))
        penalty += pts

    units = symbol_units(text)
    if units > 1:
        add("fail", 40, f"{units} units (A, B, ...): pcbc draws one unit per symbol; use a single-unit symbol")
    if not pins:
        add("fail", 100, "no pins")
        return {"findings": findings, "pins": [], "penalty": penalty}

    nums = [p["number"] for p in pins]
    dups = sorted({n for n in nums if nums.count(n) > 1}, key=_pin_sort_key)
    if dups and "(duplicate_pin_numbers_are_jumpers yes)" not in block:
        add("fail", 30, f"pin numbers repeat: {', '.join(dups[:6])}; a pad can carry one pin")

    off = [p["number"] for p in pins if _off_grid(p["x"]) or _off_grid(p["y"])]
    if off:
        add("fail", 30, f"pin ends off the {GRID} mm grid: {_some(off)}; wires cannot meet them")

    ends: dict[tuple[float, float], list[str]] = {}
    for p in pins:
        ends.setdefault((round(p["x"], 2), round(p["y"], 2)), []).append(p["number"])
    stacked = [v for v in ends.values() if len(v) > 1]
    if stacked:
        add("fail", 30, f"pins drawn on top of each other: {_some(['/'.join(v) for v in stacked])}")

    wide = [p["number"] for p in pins if len(p["number"]) * _CHAR_W > p["length"] + 0.5]
    if wide:
        add("warn", 15, f"pin numbers {_some(wide)} are wider than their {pins[0]['length']:g} mm pins and print into the body; lengthen the pins or hide numbers")

    close = _too_close(pins)
    if close:
        add("warn", 10, f"pins closer than 2.54 mm on one edge: {_some(close)}; names collide and labels have no room")

    unnamed = [p["number"] for p in pins if p["name"] in ("", "~") or p["name"] == p["number"]]
    if unnamed and len(pins) > 2:
        add("warn", 10, f"pins without names: {_some(unnamed)}; board.py binds pins by name")

    types = [p["type"] for p in pins]
    unspec = types.count("unspecified")
    if unspec == len(pins):
        add("warn", 10, "every pin is 'unspecified': ERC cannot check directions or power")
    elif unspec:
        add("info", 3, f"{unspec} of {len(pins)} pins are 'unspecified' (ERC skips them)")

    layout = parse_symbol_layout(text)
    if layout.get("core_bbox") is None:
        add("warn", 10, "no body outline: only pins are drawn")

    bw, bh = box_size_mm(layout["bbox"])
    if max(bw, bh) > 120:
        add("info", 0, f"large symbol ({bw:.0f} × {bh:.0f} mm)")

    lengths = sorted({p["length"] for p in pins})
    if len(lengths) > 1:
        add("info", 2, f"mixed pin lengths {', '.join(f'{v:g}' for v in lengths)}")

    return {"findings": findings, "pins": pins, "penalty": penalty, "units": units, "size_mm": (round(bw, 2), round(bh, 2))}


def score_footprint(text: str) -> dict:
    findings: list[tuple[str, str]] = []
    penalty = 0

    def add(sev: str, pts: int, msg: str) -> None:
        nonlocal penalty
        findings.append((sev, msg))
        penalty += pts

    legacy = text.lstrip().startswith("(module")
    if legacy:
        add("fail", 30, "KiCad 5 '(module' format: pcbc build cannot place it; kicad-cli fp upgrade (pcbc fetch does this)")
    pads: list[str] = []
    zero: list[str] = []
    smd = th = 0
    for pad in _iter_tagged(text, "pad"):
        m = _PAD_NUM.match(pad)
        num = m.group(1) if m else ""
        pads.append(num)
        sz = _PAD_SIZE.search(pad)
        if sz and (float(sz.group(1)) <= 0 or float(sz.group(2)) <= 0):
            zero.append(num)
        if " smd " in pad[:40]:
            smd += 1
        elif "thru_hole" in pad[:40]:
            th += 1
    if not pads:
        add("fail", 100, "no pads")
        return {"findings": findings, "pads": [], "penalty": penalty, "legacy": legacy}
    if zero:
        add("fail", 20, f"pads with no size: {_some(zero)}")
    if _graphics_bbox(text, "CrtYd") is None:
        add("warn", 10, "no courtyard: placement cannot keep parts apart")
    if _graphics_bbox(text, ".Fab") is None:
        add("info", 3, "no fabrication outline (EasyEDA draws the body on the courtyard)")
    crt = _graphics_bbox(text, "CrtYd")
    pb = _pads_bbox(text)
    if crt is not None and pb is not None:
        out = max(crt[0] - pb[0], crt[1] - pb[1], pb[2] - crt[2], pb[3] - crt[3])
        if out > 0.05:
            add("info", 2, f"pads reach {out:.2f} mm outside the courtyard (EasyEDA draws the body, not the land); pcbc keeps parts off the pads anyway")
    if "SilkS" not in text:
        add("info", 5, "no silkscreen")
    if not _MODEL.search(text):
        add("info", 5, "no 3D model")
    if "(attr " not in text:
        add("info", 5, "no smd/through_hole attribute")
    return {
        "findings": findings,
        "pads": pads,
        "penalty": penalty,
        "legacy": legacy,
        "smd": smd,
        "th": th,
        "body_mm": body_box(text),
    }


def body_box(text: str) -> tuple[float, float] | None:
    """Body size from the fab outline, else the courtyard (what EasyEDA draws the body on)."""
    for layer in (".Fab", "CrtYd"):
        box = _graphics_bbox(text, layer)
        if box is not None:
            w, h = box_size_mm(box)
            if w and h:
                return (round(w, 2), round(h, 2))
    return None


def score_part(path: Path, *, symbol: str | None = None, footprint: str | None = None) -> dict:
    """Score a part dir (or part.py / .kicad_sym); cross-check symbol pins against footprint pads."""
    path = Path(path)
    if path.is_file() and path.suffix == ".py":
        path = path.parent
    if path.is_file():
        pkg = path.parent
        symbol = symbol or (path.name if path.suffix == ".kicad_sym" else None)
        footprint = footprint or (path.name if path.suffix == ".kicad_mod" else None)
    else:
        pkg = path
    sym_path = _pick(pkg, symbol, "*.kicad_sym")
    mod_path = _pick(pkg, footprint, "*.kicad_mod")
    findings: list[tuple[str, str]] = []
    penalty = 0
    sym_pins: list[dict] = []
    pads: list[str] = []
    if sym_path is None:
        findings.append(("fail", f"no .kicad_sym in {pkg}"))
        penalty += 100
    else:
        s = score_symbol(sym_path.read_text())
        findings += s["findings"]
        penalty += s["penalty"]
        sym_pins = s["pins"]
    if mod_path is None:
        findings.append(("fail", f"no .kicad_mod in {pkg}"))
        penalty += 100
    else:
        f = score_footprint(mod_path.read_text())
        findings += f["findings"]
        penalty += f["penalty"]
        pads = f["pads"]
    if sym_pins and pads:
        pad_set = {p for p in pads if p}
        pin_set = {p["number"] for p in sym_pins}
        missing = sorted(pin_set - pad_set, key=_pin_sort_key)
        if missing:
            findings.append(("fail", f"symbol pins with no pad in the footprint: {_some(missing)}; the netlist would drop them"))
            penalty += 40
        extra = sorted(pad_set - pin_set, key=_pin_sort_key)
        if extra:
            findings.append(("warn", f"footprint pads with no symbol pin: {_some(extra)}; a thermal pad? pcbc leaves them unconnected"))
            penalty += 10
    part_py = pkg / "part.py"
    if part_py.exists():
        m = re.search(r"body_mm\s*=\s*\(\s*([0-9.]+)\s*,\s*([0-9.]+)\s*\)", part_py.read_text())
        outline = body_box(mod_path.read_text()) if mod_path is not None else None
        if m and outline:
            fw, fh = outline
            bw, bh = float(m.group(1)), float(m.group(2))
            tol = 0.6
            same = (abs(fw - bw) <= tol and abs(fh - bh) <= tol) or (abs(fw - bh) <= tol and abs(fh - bw) <= tol)
            if not same:
                findings.append(("warn", f"part.py body_mm ({bw:g} x {bh:g}) is not the footprint's outline ({fw:g} x {fh:g}); wrong land or wrong datasheet"))
                penalty += 10
    score = max(0, 100 - penalty)
    grade = "good" if score >= GOOD else ("usable" if score >= USABLE else "bad")
    order = {"fail": 0, "warn": 1, "info": 2}
    findings.sort(key=lambda f: (order.get(f[0], 3), f[1]))
    return {
        "dir": str(pkg),
        "symbol": sym_path.name if sym_path else None,
        "footprint": mod_path.name if mod_path else None,
        "pins": len(sym_pins),
        "pads": len(pads),
        "score": score,
        "grade": grade,
        "findings": findings,
    }


def format_score(report: dict) -> str:
    head = f"{report['dir']}: {report['grade']} ({report['score']}/100), {report['pins']} pins / {report['pads']} pads"
    lines = [head]
    for sev, msg in report["findings"]:
        lines.append(f"  {sev:<4} {msg}")
    return "\n".join(lines)


def _pick(pkg: Path, name: str | None, pattern: str) -> Path | None:
    if name:
        p = pkg / name
        return p if p.exists() else None
    found = sorted(pkg.glob(pattern))
    return found[0] if len(found) == 1 else None


def _off_grid(v: float) -> bool:
    return abs(v / GRID - round(v / GRID)) > 1e-3


def _some(items: list[str], n: int = 6) -> str:
    return ", ".join(items[:n]) + (", ..." if len(items) > n else "")


def _too_close(pins: list[dict]) -> list[str]:
    """Pairs on one edge (same rot, same line) nearer than 2.54 mm."""
    out: list[str] = []
    by_edge: dict[tuple[float, float], list[dict]] = {}
    for p in pins:
        # Horizontal pins (rot 0/180) sit on a vertical edge: same x, spread along y.
        key = (p["rot"] % 360, round(p["y"] if p["rot"] % 180 == 90 else p["x"], 2))
        by_edge.setdefault(key, []).append(p)
    for (rot, _), group in by_edge.items():
        axis = "x" if rot % 180 == 90 else "y"
        group = sorted(group, key=lambda p: p[axis])
        for a, b in zip(group, group[1:]):
            if 0 < abs(b[axis] - a[axis]) < 2.54 - 1e-6:
                out.append(f"{a['number']}/{b['number']}")
    return out
