"""`kicad10_words.kicad_pcb`: what KiCad 10.0.6's save keeps of the word-valued fields the layout
language accepts, measured with `kicad-cli pcb upgrade --force` on the hand-written board below
(fourth review, C3). The findings the fixture pins (`test_layout_roundtrip.py`, fourth review):

- a `gr_rect` `(fill solid)` comes back `(fill yes)`, `(fill none)` comes back `(fill no)`, and
  `(fill hatch)` is kept;
- a via's `(tenting (front none) (back none))` is dropped (none = inherit the board's), while
  `(covering (front none))` and `(plugging (back none))` are kept;
- a via's `(layers "B.Cu" "F.Cu")` comes back `("F.Cu" "B.Cu")`;
- an uppercase uuid comes back lowercase;
- a zone's `(layers "In2.Cu" "F.Cu" "In1.Cu" "B.Cu")` comes back in layer-id order,
  `("F.Cu" "B.Cu" "In1.Cu" "In2.Cu")`;
- a zone's `(smoothing none)` and `(fill (mode polygon))` are dropped.

Run from this directory: `python make_kicad10_words.py` (needs `kicad-cli` on the path)."""

import shutil
import subprocess
from pathlib import Path

RAW = """(kicad_pcb
	(version 20260206)
	(generator "pcbnew")
	(generator_version "10.0")
	(general (thickness 1.6) (legacy_teardrops no))
	(paper "A4")
	(layers
		(0 "F.Cu" signal)
		(4 "In1.Cu" signal)
		(6 "In2.Cu" signal)
		(2 "B.Cu" signal)
		(9 "F.Adhes" user "F.Adhesive")
		(11 "B.Adhes" user "B.Adhesive")
		(13 "F.Paste" user)
		(15 "B.Paste" user)
		(5 "F.SilkS" user "F.Silkscreen")
		(7 "B.SilkS" user "B.Silkscreen")
		(1 "F.Mask" user)
		(3 "B.Mask" user)
		(17 "Dwgs.User" user "User.Drawings")
		(19 "Cmts.User" user "User.Comments")
		(21 "Eco1.User" user "User.Eco1")
		(23 "Eco2.User" user "User.Eco2")
		(25 "Edge.Cuts" user)
		(27 "Margin" user)
		(31 "F.CrtYd" user "F.Courtyard")
		(29 "B.CrtYd" user "B.Courtyard")
		(35 "F.Fab" user)
		(33 "B.Fab" user)
	)
	(setup (pad_to_mask_clearance 0))
	(net 0 "")
	(net 1 "GND")
	(gr_rect (start 0 0) (end 30 30) (stroke (width 0.05) (type default)) (fill none) (layer "Edge.Cuts") (uuid "11111111-1111-4111-8111-111111111111"))
	(gr_rect (start 2 2) (end 5 5) (stroke (width 0.1) (type solid)) (fill solid) (layer "F.SilkS") (uuid "11111111-1111-4111-8111-111111111112"))
	(gr_rect (start 6 2) (end 9 5) (stroke (width 0.1) (type solid)) (fill hatch) (layer "F.SilkS") (uuid "11111111-1111-4111-8111-111111111113"))
	(via (at 10 10) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (tenting (front none) (back none)) (covering (front none) (back yes)) (plugging (front no) (back none)) (net 1) (uuid "11111111-1111-4111-8111-111111111114"))
	(via (at 12 10) (size 0.6) (drill 0.3) (layers "B.Cu" "F.Cu") (net 1) (uuid "11111111-1111-4111-8111-111111111115"))
	(via (at 14 10) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 1) (uuid "ABCDEF01-1111-4111-8111-111111111116"))
	(zone (net 1) (net_name "GND") (layers "In2.Cu" "F.Cu" "In1.Cu" "B.Cu") (uuid "11111111-1111-4111-8111-111111111117") (hatch edge 0.5) (connect_pads (clearance 0.2)) (min_thickness 0.25) (keepout (tracks not_allowed) (vias allowed) (pads allowed) (copperpour not_allowed) (footprints allowed)) (fill (thermal_gap 0.5) (thermal_bridge_width 0.5) (smoothing none)) (polygon (pts (xy 20 20) (xy 25 20) (xy 25 25) (xy 20 25))))
	(zone (net 1) (net_name "GND") (layer "B.Cu") (uuid "11111111-1111-4111-8111-111111111118") (hatch edge 0.5) (connect_pads (clearance 0.2)) (min_thickness 0.25) (fill yes (mode polygon) (thermal_gap 0.5) (thermal_bridge_width 0.5) (smoothing chamfer) (radius 1)) (polygon (pts (xy 20 2) (xy 25 2) (xy 25 7) (xy 20 7))))
	(gr_text "t" (at 15 15 0) (layer "F.SilkS") (uuid "11111111-1111-4111-8111-111111111119") (effects (font (size 1 1) (thickness 0.15)) (justify left top mirror)))
)
"""

if __name__ == "__main__":
    here = Path(__file__).resolve().parent
    out = here / "kicad10_words.kicad_pcb"
    out.write_text(RAW)
    cli = shutil.which("kicad-cli") or "/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli"
    subprocess.run([cli, "pcb", "upgrade", "--force", str(out)], check=True)
    print("wrote", out)
