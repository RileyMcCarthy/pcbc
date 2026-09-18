"""The schematic must encode exactly the netlist in board.py.

`emit_from_design` draws wires, labels and power symbols; KiCad decides what
they connect. Ask kicad-cli for its netlist and diff it against the Design.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

from .fab import kicad_cli
from .model import Design

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


def compare(expected: dict[str, set[Pad]], actual: dict[str, set[Pad]]) -> list[str]:
    """Failures when the KiCad netlist is not exactly the board's netlist."""
    fails: list[str] = []
    by_pad: dict[Pad, str] = {}
    for name, pads in actual.items():
        for pad in pads:
            by_pad[pad] = name
    matched: set[str] = set()
    for name in sorted(expected):
        want = expected[name]
        hit = sorted({by_pad[p] for p in want if p in by_pad})
        if not hit:
            fails.append(f"{name}: none of {_fmt_pads(want)} is connected in the schematic")
            continue
        if len(hit) > 1:
            parts = "; ".join(f"{h} = {_fmt_pads(actual[h] & want)}" for h in hit)
            fails.append(f"{name}: split across {len(hit)} schematic nets ({parts})")
            matched.update(hit)
            continue
        got_name = hit[0]
        got = actual[got_name]
        matched.add(got_name)
        extra = got - want
        missing = want - got
        if extra or missing:
            msg = f"{name}: schematic net {got_name!r}"
            if missing:
                msg += f" is missing {_fmt_pads(missing)}"
            if extra:
                msg += f"{' and' if missing else ''} also has {_fmt_pads(extra)}"
            fails.append(msg)
        if got_name != name:
            fails.append(f"{name}: schematic names it {got_name!r}")
    bound = {p for pads in expected.values() for p in pads}
    for name, pads in sorted(actual.items()):
        if name in matched or name.startswith("unconnected-"):
            continue
        if len(pads) < 2 and not (pads & bound):
            continue
        fails.append(f"schematic-only net {name!r} = {_fmt_pads(pads)}")
    return fails


def check_schematic(design: Design, sch: Path, cli: Path | None = None) -> list[str]:
    """Run kicad-cli on the emitted sheet and diff. Empty list means identical."""
    return compare(expected_nets(design), kicad_nets(Path(sch), cli))
