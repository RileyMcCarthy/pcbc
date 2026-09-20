"""The schematic must encode exactly the netlist in board.py.

`emit_from_design` draws wires, labels and power symbols; KiCad decides what
they connect. Ask kicad-cli for its netlist and diff it against the Design.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

from .dru import soft_kind
from .fab import kicad_cli
from .model import Design
from .sexp import matching_paren

Pad = tuple[str, str]  # (ref, pad number)


class KicadMissing(RuntimeError):
    pass


def expected_nets(design: Design) -> dict[str, set[Pad]]:
    """net name → {(ref, pad)} from the bound pins of every instance."""
    nets: dict[str, set[Pad]] = {}
    for inst in design.instances:
        for pname, net in inst.pins.items():
            pin = inst.part.pins.get(pname)
            if pin is None:
                continue
            for pad in pin.pads:
                nets.setdefault(net, set()).add((inst.ref, str(pad)))
    return nets


def kicad_nets(sch: Path, cli: Path | None = None) -> dict[str, set[Pad]]:
    """net name → {(ref, pad)} as kicad-cli reads the sheet. Power symbols dropped."""
    cli = cli or kicad_cli()
    if not cli.exists() and shutil.which(str(cli)) is None:
        raise KicadMissing(f"kicad-cli not found ({cli})")
    with tempfile.TemporaryDirectory() as td:
        xml = Path(td) / "netlist.xml"
        run = subprocess.run(
            [str(cli), "sch", "export", "netlist", "--format", "kicadxml", "-o", str(xml), str(sch)],
            capture_output=True,
            text=True,
        )
        if run.returncode != 0 or not xml.exists():
            raise RuntimeError(f"kicad-cli sch export netlist failed: {run.stderr.strip() or run.stdout.strip()}")
        root = ET.parse(xml).getroot()
    nets: dict[str, set[Pad]] = {}
    for net in root.iter("net"):
        name = (net.get("name") or "").lstrip("/")
        pads = {
            (nd.get("ref") or "", nd.get("pin") or "")
            for nd in net.findall("node")
            if not (nd.get("ref") or "").startswith("#")
        }
        if pads:
            nets[name] = pads
    return nets


def _fmt_pads(pads: set[Pad]) -> str:
    return ", ".join(f"{r}.{p}" for r, p in sorted(pads))


def compare(expected: dict[str, set[Pad]], actual: dict[str, set[Pad]], what: str = "schematic") -> list[str]:
    """Failures when the KiCad netlist is not exactly the board's netlist."""
    fails: list[str] = []
    schematic = what
    by_pad: dict[Pad, str] = {}
    for name, pads in actual.items():
        for pad in pads:
            by_pad[pad] = name
    matched: set[str] = set()
    for name in sorted(expected):
        want = expected[name]
        hit = sorted({by_pad[p] for p in want if p in by_pad})
        if not hit:
            fails.append(f"{name}: none of {_fmt_pads(want)} is connected in the {schematic}")
            continue
        if len(hit) > 1:
            parts = "; ".join(f"{h} = {_fmt_pads(actual[h] & want)}" for h in hit)
            fails.append(f"{name}: split across {len(hit)} {schematic} nets ({parts})")
            matched.update(hit)
            continue
        got_name = hit[0]
        got = actual[got_name]
        matched.add(got_name)
        extra = got - want
        missing = want - got
        if extra or missing:
            msg = f"{name}: {schematic} net {got_name!r}"
            if missing:
                msg += f" is missing {_fmt_pads(missing)}"
            if extra:
                msg += f"{' and' if missing else ''} also has {_fmt_pads(extra)}"
            fails.append(msg)
        if got_name != name:
            fails.append(f"{name}: {schematic} names it {got_name!r}")
    bound = {p for pads in expected.values() for p in pads}
    for name, pads in sorted(actual.items()):
        if name in matched or name.startswith("unconnected-"):
            continue
        if len(pads) < 2 and not (pads & bound):
            continue
        fails.append(f"{schematic}-only net {name!r} = {_fmt_pads(pads)}")
    return fails


# ---------------------------------------------------------------- copper

_PCB_NET_DEF = re.compile(r'\(net\s+(\d+)\s+"([^"]*)"\)')
_PAD_HEAD = re.compile(r'\(pad\s+"([^"]*)"')
_PAD_NET = re.compile(r'\(net\s+(?:\d+\s+)?"([^"]*)"\)')  # pcbc seeds pads as (net "NAME"); KiCad writes (net N "NAME")


def copper_nets(text: str) -> dict[str, set[Pad]]:
    """net name → {(ref, pad)} from a .kicad_pcb's pad bindings. Unnumbered pads dropped."""
    from .sexp import board_footprint_spans, footprint_reference

    nets: dict[str, set[Pad]] = {}
    for start, end in board_footprint_spans(text):
        block = text[start:end]
        ref = footprint_reference(block) or "?"
        pos = 0
        while True:
            j = block.find("(pad ", pos)
            if j < 0:
                break
            k = matching_paren(block, j)
            pad = block[j : k + 1]
            pos = k + 1
            num = _PAD_HEAD.match(pad)
            net = _PAD_NET.search(pad)
            if not num or not num.group(1) or not net or not net.group(1):
                continue
            nets.setdefault(net.group(1), set()).add((ref, num.group(1)))
    return nets


def names_rule(description: str, rule: str) -> bool:
    """Whether a KiCad DRC violation came from the custom rule `rule`. Most checks say
    `(rule 'name' ...)`; the diff-pair checks say `(name minimum gap ...)` / `(name maximum
    uncoupled length ...)` with no `rule` word (KiCad 10.0.6, probed in tests/test_dru.py)."""
    return f"rule '{rule}'" in description or f"({rule} " in description


def kicad_drc(pcb: Path, cli: Path | None = None, *, refill: bool = True) -> dict:
    """kicad-cli pcb drc as parsed JSON (violations, unconnected_items).

    The router writes pours without fills; KiCad judges an unfilled zone as no copper, so
    every plane-fed pad reads unconnected and the Gerbers would carry no plane. DRC refills
    the zones and saves the board, so what the gate judged is what the fab gets."""
    cli = cli or kicad_cli()
    if not cli.exists() and shutil.which(str(cli)) is None:
        raise KicadMissing(f"kicad-cli not found ({cli})")
    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "drc.json"
        fill = ["--refill-zones", "--save-board"] if refill else []
        run = subprocess.run(
            [str(cli), "pcb", "drc", "--format", "json", "--all-track-errors", *fill, "-o", str(out), str(pcb)],
            capture_output=True,
            text=True,
        )
        if not out.exists():
            raise RuntimeError(f"kicad-cli pcb drc failed: {run.stderr.strip() or run.stdout.strip()}")
        return json.loads(out.read_text())


def check_copper(design: Design, pcb: Path, cli: Path | None = None, floor_mm: float | None = None, *, refill: bool = True) -> dict:
    """The copper gate: KiCad DRC clean, nothing unconnected, pads bound exactly as board.py says."""
    from .compile import compile_design
    from .fab import copper_drc_errors

    pcb = Path(pcb)
    text = pcb.read_text()
    fails: list[str] = []
    nets = compare(expected_nets(design), copper_nets(text), what="copper")
    fails += nets
    doc = kicad_drc(pcb, cli, refill=refill)
    job = compile_design(design)
    if floor_mm is None:
        from .stackup import get_stackup

        floor_mm = get_stackup(job.stackup).clearance_min
    violations = doc.get("violations") or []
    canary_written = any(r.name == "pcbc_canary" for r in job.dru)
    canary_fired = any("rule 'pcbc_canary'" in str(v.get("description", "")) for v in violations)
    if canary_written and not canary_fired:
        fails.append(
            "KiCad applied none of pcbc's custom rules (the canary rule did not fire): a rule in the board's "
            ".kicad_dru does not parse, and kicad-cli does not say which; every rule is off until it does"
        )
    geometry = {
        "segments": sum(1 for v in violations if v.get("type") == "track_segment_length"),
        "angles": sum(1 for v in violations if v.get("type") == "track_angle"),
    }
    # Every pcbc rule's hits (the description names the rule, as the canary check reads it) and, of
    # those, the soft rules (E, H.3): warnings the bar counts and test_examples_fab pins per example.
    rule_hits = {r.name: sum(1 for v in violations if names_rule(str(v.get("description", "")), r.name)) for r in job.dru}
    soft = {r.name: rule_hits[r.name] for r in job.dru if r.severity == "warning" and soft_kind(r.name) is not None}
    pcbc_warning_rules = [r.name for r in job.dru if r.severity == "warning"]  # soft rules, the geometry rules, the canary
    errors = [
        f"{v.get('type')}: {v.get('description')}"
        for v in copper_drc_errors(doc, floor_mm=floor_mm)
    ]
    unconnected = doc.get("unconnected_items") or []
    fails += errors
    if unconnected:
        shown = "; ".join(str(u.get("description", "")) for u in unconnected[:4])
        fails.append(f"{len(unconnected)} unconnected item(s): {shown}")
    return {
        "ok": not fails,
        "fails": fails,
        "nets": nets,
        "drc_errors": errors,
        "unconnected": len(unconnected),
        "drc_warnings": sum(
            1
            for v in violations
            if (v.get("severity") or "").lower() == "warning"
            and v.get("type") not in ("track_segment_length", "track_angle")
            and not any(names_rule(str(v.get("description", "")), name) for name in pcbc_warning_rules)
        ),
        "geometry": geometry,  # KiCad's own count of staircases and 90 degree corners
        "canary": canary_fired,
        "soft": soft,  # {rule name: hits} for pcbc's soft rules (track_width, skew, via budget, uncoupled)
        "rules": rule_hits,  # {rule name: hits} for every pcbc rule
    }


def check_schematic(design: Design, sch: Path, cli: Path | None = None) -> list[str]:
    """Run kicad-cli on the emitted sheet and diff. Empty list means identical."""
    return compare(expected_nets(design), kicad_nets(Path(sch), cli))


def check_erc(sch: Path, cli: Path | None = None) -> dict:
    """KiCad's own ERC on the sheet. ``errors`` is a list of strings (empty is
    clean); ``warnings`` counts each warning type. Errors are what a person
    opening the project would see first: an unbound pin with no mark, a
    power net nothing drives."""
    import json
    from collections import Counter

    cli = cli or kicad_cli()
    if not cli.exists() and shutil.which(str(cli)) is None:
        raise KicadMissing(f"kicad-cli not found ({cli})")
    with tempfile.TemporaryDirectory() as td:
        rep = Path(td) / "erc.json"
        run = subprocess.run(
            [str(cli), "sch", "erc", "--format", "json", "--severity-all", "-o", str(rep), str(sch)],
            capture_output=True,
            text=True,
        )
        if not rep.exists():
            raise RuntimeError(f"kicad-cli sch erc failed: {run.stderr.strip() or run.stdout.strip()}")
        data = json.loads(rep.read_text())
    errors: list[str] = []
    warnings: Counter = Counter()
    for sheet in data.get("sheets", []):
        for v in sheet.get("violations", []):
            if v.get("severity") == "error":
                what = "; ".join(i.get("description", "") for i in v.get("items", []))
                errors.append(f"{v.get('type')}: {what}")
            else:
                warnings[v.get("type", "?")] += 1
    return {"errors": errors, "warnings": dict(warnings)}
