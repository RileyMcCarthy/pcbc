"""The fourth census probe: the two zone/polygon fields the first three probes could not set, saved by
KiCad itself.

    python make_kicad10_primitives4.py kicad10_primitives4.kicad_pcb

- a zone's per-layer hatch offset, `(property (layer "F.Cu") (hatch_position (xy x y)))`, which
  pcbnew's bindings set through `ZONE_LAYER_PROPERTIES.hatching_offset` (one block per copper layer);
- an `(arc (start) (mid) (end))` entry inside a `(pts ...)`: in a zone's outline, in a zone's hole
  (its second `(polygon)`), and in a `gr_poly`.

Both were found by the second adversarial round (`refute-primitives-r2`): KiCad 10.0.6 writes them,
`kicad-cli pcb upgrade --force` writes them back unchanged, and until this probe no Python field
carried them. Written in KiCad's own grammar on the stripped header of probe 3, then loaded and
**rewritten by KiCad** (`kicad-cli pcb upgrade --force`), so every byte of the fixture is KiCad's.
"""

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))
from pcbc.layout_emit import splice, strip_layout  # noqa: E402

out = sys.argv[1]
base = strip_layout((Path(__file__).parent / "kicad10_primitives3.kicad_pcb").read_text())


def Z(body: str) -> str:
    return "\t(zone\n" + body + "\t)\n"


blocks = [
    # per-layer hatch offset on a two-layer hatched pour: one (property) per layer
    Z(
        '\t\t(net "GND")\n'
        '\t\t(layers "F.Cu" "B.Cu")\n'
        '\t\t(uuid "44444444-0000-4000-8000-000000000001")\n'
        "\t\t(hatch edge 0.5)\n"
        "\t\t(connect_pads (clearance 0.5))\n"
        "\t\t(min_thickness 0.25)\n"
        "\t\t(fill (mode hatch) (thermal_gap 0.5) (thermal_bridge_width 0.5) (island_removal_mode 0) (hatch_thickness 0.3) (hatch_gap 0.4) (hatch_orientation 30) (hatch_border_algorithm hatch_thickness) (hatch_min_hole_area 0.3))\n"
        '\t\t(property (layer "F.Cu") (hatch_position (xy 0.3 0.4)))\n'
        '\t\t(property (layer "B.Cu") (hatch_position (xy 0.1234567 -0.25)))\n'
        "\t\t(polygon (pts (xy 0 0) (xy 20 0) (xy 20 20) (xy 0 20)))\n"
    ),
    # an arc in a zone outline and an arc in its hole
    Z(
        '\t\t(net "GND")\n'
        '\t\t(layer "B.Cu")\n'
        '\t\t(uuid "44444444-0000-4000-8000-000000000002")\n'
        "\t\t(hatch edge 0.5)\n"
        "\t\t(connect_pads (clearance 0.5))\n"
        "\t\t(min_thickness 0.25)\n"
        "\t\t(fill (thermal_gap 0.5) (thermal_bridge_width 0.5) (island_removal_mode 0))\n"
        "\t\t(polygon (pts (xy 30 0) (xy 50 0) (arc (start 50 0) (mid 55 10) (end 50 20)) (xy 30 20)))\n"
        "\t\t(polygon (pts (xy 35 5) (arc (start 40 5) (mid 42 8) (end 40 11)) (xy 35 11)))\n"
    ),
    # an arc in a gr_poly's points
    "\t(gr_poly\n"
    "\t\t(pts (xy 60 0) (arc (start 70 0) (mid 72 5) (end 70 10)) (xy 60 10))\n"
    "\t\t(stroke (width 0.1) (type solid))\n"
    "\t\t(fill no)\n"
    '\t\t(layer "F.SilkS")\n'
    '\t\t(uuid "44444444-0000-4000-8000-000000000003")\n'
    "\t)\n",
]
Path(out).write_text(splice(base, "".join(blocks)))
run = subprocess.run(["kicad-cli", "pcb", "upgrade", "--force", out], capture_output=True, text=True)
print(run.stdout, run.stderr)
assert run.returncode == 0, run.returncode
text = Path(out).read_text()
assert text.count("(hatch_position") == 2 and text.count("(arc") == 3, "KiCad did not keep the probe's fields"
print("ok", out)
