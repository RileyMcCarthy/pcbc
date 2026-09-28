"""The third census probe: every board drawing KiCad 10 has, plus the via and zone fields probes 1 and 2
could not set, all saved by KiCad itself.

Run with KiCad 10.0.6's own Python (the one that imports `pcbnew`):

    /Applications/KiCad/KiCad.app/Contents/Frameworks/Python.framework/Versions/3.9/bin/python3 \
        make_kicad10_primitives3.py kicad10_primitives3.kicad_pcb

Two halves. `pcbnew` builds what its bindings can set (shapes with every option, text, text boxes, the
five dimension kinds, a target, a barcode, a group, vias with a top and a bottom backdrill and both
post-machining modes, a zone with doubles `fmt_num` used to cut to six decimals). What the bindings
cannot build (a table with cells, a `point`, a `generated` tuning pattern, a reference image, an
outline-font text with its `render_cache`) is appended in KiCad's own grammar. Then
`kicad-cli pcb upgrade --force` loads the whole board and **KiCad writes every byte of the file**, so
nothing in the fixture is pcbc's spelling.
"""

import subprocess
import sys

import pcbnew
from pcbnew import VECTOR2I
from pcbnew import FromMM as MM

out = sys.argv[1]
b = pcbnew.BOARD()
b.SetCopperLayerCount(4)
net = pcbnew.NETINFO_ITEM(b, "GND")
b.Add(net)


def P(x, y):
    return VECTOR2I(MM(x), MM(y))


def T(label, fn):
    try:
        fn()
    except Exception as e:  # noqa: BLE001 - a probe reports what the bindings refuse and goes on
        print("FAIL", label, e)


def shape(kind, layer=pcbnew.F_SilkS):
    s = pcbnew.PCB_SHAPE(b)
    s.SetShape(kind)
    s.SetLayer(layer)
    return s


# ---- shapes
s = shape(pcbnew.SHAPE_T_SEGMENT, pcbnew.F_Cu)
s.SetStart(P(1, 1)); s.SetEnd(P(5, 1)); s.SetWidth(MM(0.3)); s.SetNet(net); s.SetLocked(True)
T("mask", lambda: s.SetHasSolderMask(True)); T("mm", lambda: s.SetLocalSolderMaskMargin(MM(0.07)))
b.Add(s)
s = shape(pcbnew.SHAPE_T_RECTANGLE)
s.SetStart(P(10, 1)); s.SetEnd(P(14, 4)); s.SetWidth(MM(0.12)); s.SetCornerRadius(MM(0.4)); s.SetFillMode(pcbnew.FILL_T_HATCH); s.SetLocked(True)
b.Add(s)
s = shape(pcbnew.SHAPE_T_CIRCLE)
s.SetCenter(P(10, 10)); s.SetEnd(P(10, 12)); s.SetWidth(MM(0.1)); s.SetFilled(True)
b.Add(s)
s = shape(pcbnew.SHAPE_T_CIRCLE, pcbnew.B_Cu)
s.SetCenter(P(20, 10)); s.SetEnd(P(21, 10)); s.SetWidth(MM(0.1)); s.SetFillMode(pcbnew.FILL_T_CROSS_HATCH); s.SetNet(net)
b.Add(s)
s = shape(pcbnew.SHAPE_T_ARC)
s.SetArcGeometry(P(1, 5), P(3, 6), P(5, 5)); s.SetWidth(MM(0.1))
b.Add(s)
s = shape(pcbnew.SHAPE_T_POLY)
s.SetPolyPoints([P(30, 1), P(32, 1), P(31, 3)]); s.SetWidth(MM(0.1)); s.SetFilled(True)
b.Add(s)
s = shape(pcbnew.SHAPE_T_POLY, pcbnew.F_Cu)
s.SetPolyPoints([P(30, 5), P(32, 5), P(31, 7)]); s.SetWidth(MM(0.2)); s.SetFillMode(pcbnew.FILL_T_REVERSE_HATCH); s.SetNet(net); s.SetHasSolderMask(True)
b.Add(s)
s = shape(pcbnew.SHAPE_T_BEZIER)
s.SetStart(P(1, 20)); s.SetBezierC1(P(2, 22)); s.SetBezierC2(P(4, 22)); s.SetEnd(P(5, 20)); s.SetWidth(MM(0.1))
b.Add(s)

# ---- text and text boxes
t = pcbnew.PCB_TEXT(b)
t.SetText("HELLO"); t.SetPosition(P(20, 20)); t.SetLayer(pcbnew.B_SilkS); t.SetMirrored(True); t.SetBold(True); t.SetItalic(True)
t.SetHorizJustify(pcbnew.GR_TEXT_H_ALIGN_LEFT); t.SetVertJustify(pcbnew.GR_TEXT_V_ALIGN_TOP)
t.SetTextSize(VECTOR2I(MM(1.2), MM(0.8))); t.SetTextThickness(MM(0.2)); t.SetTextAngleDegrees(30.123456789)
t.SetIsKnockout(True); t.SetLineSpacing(1.3); t.SetLocked(True)
b.Add(t)
tb = pcbnew.PCB_TEXTBOX(b)
tb.SetText("BOX"); tb.SetStart(P(40, 1)); tb.SetEnd(P(48, 5)); tb.SetLayer(pcbnew.F_SilkS)
tb.SetMarginLeft(MM(0.3)); tb.SetMarginTop(MM(0.4)); tb.SetMarginRight(MM(0.5)); tb.SetMarginBottom(MM(0.6))
tb.SetBorderEnabled(False); tb.SetBorderWidth(MM(0.2)); tb.SetIsKnockout(True); tb.SetBold(True); tb.SetLocked(True)
b.Add(tb)
tb = pcbnew.PCB_TEXTBOX(b)
tb.SetText("ROT"); tb.SetStart(P(40, 10)); tb.SetEnd(P(48, 14)); tb.SetLayer(pcbnew.F_SilkS); tb.SetTextAngleDegrees(45)
b.Add(tb)

# ---- dimensions, all five kinds
d = pcbnew.PCB_DIM_ALIGNED(b)
d.SetStart(P(1, 30)); d.SetEnd(P(11, 30)); d.SetHeight(MM(2)); d.SetLayer(pcbnew.Dwgs_User)
d.SetPrefix("W="); d.SetSuffix("!"); d.SetUnitsMode(pcbnew.DIM_UNITS_MODE_MILS); d.SetUnitsFormat(pcbnew.DIM_UNITS_FORMAT_PAREN_SUFFIX)
d.SetPrecision(pcbnew.DIM_PRECISION_X_XX); d.SetSuppressZeroes(True); d.SetOverrideTextEnabled(True); d.SetOverrideText("OVR")
d.SetLineThickness(MM(0.11)); d.SetArrowLength(MM(1.1)); d.SetTextPositionMode(pcbnew.DIM_TEXT_POSITION_MANUAL)
d.SetArrowDirection(pcbnew.DIM_ARROW_DIRECTION_INWARD); d.SetExtensionHeight(MM(0.7)); d.SetExtensionOffset(MM(0.3)); d.SetKeepTextAligned(False); d.SetLocked(True)
b.Add(d)
d = pcbnew.PCB_DIM_ORTHOGONAL(b)
d.SetStart(P(1, 40)); d.SetEnd(P(11, 44)); d.SetHeight(MM(2)); d.SetLayer(pcbnew.Dwgs_User); d.SetOrientation(pcbnew.PCB_DIM_ORTHOGONAL.DIR_VERTICAL)
b.Add(d)
d = pcbnew.PCB_DIM_RADIAL(b)
d.SetStart(P(20, 40)); d.SetEnd(P(22, 42)); d.SetLayer(pcbnew.Dwgs_User); d.SetLeaderLength(MM(3))
b.Add(d)
d = pcbnew.PCB_DIM_LEADER(b)
d.SetStart(P(30, 40)); d.SetEnd(P(32, 42)); d.SetLayer(pcbnew.Dwgs_User); d.SetTextBorder(pcbnew.DIM_TEXT_BORDER_ROUNDRECT); d.SetOverrideText("LEAD")
b.Add(d)
d = pcbnew.PCB_DIM_CENTER(b)
d.SetStart(P(40, 40)); d.SetEnd(P(41, 40)); d.SetLayer(pcbnew.Dwgs_User)
b.Add(d)

# ---- target, barcode, group
tg = pcbnew.PCB_TARGET(b)
tg.SetShape(1); tg.SetPosition(P(50, 50)); tg.SetSize(MM(4)); tg.SetWidth(MM(0.2)); tg.SetLayer(pcbnew.Edge_Cuts)
b.Add(tg)
bc = pcbnew.PCB_BARCODE(b)
bc.SetText("PCBC"); bc.SetPosition(P(60, 60)); bc.SetLayer(pcbnew.F_SilkS); bc.SetKind(pcbnew.BARCODE_T_QR_CODE); bc.SetErrorCorrection(pcbnew.BARCODE_ECC_T_Q)
bc.SetWidth(MM(5)); bc.SetHeight(MM(5)); bc.SetShowText(True); bc.SetTextSize(MM(1.3)); bc.SetIsKnockout(True); bc.SetMargin(VECTOR2I(MM(0.2), MM(0.3))); bc.SetOrientation(90); bc.SetLocked(True)
b.Add(bc)
g = pcbnew.PCB_GROUP(b)
g.SetName("marks")
b.Add(g)
for it in list(b.Drawings())[:2]:
    g.AddItem(it)
g.SetLocked(True)

# ---- vias: a top and a bottom backdrill, both post-machining modes; teardrops with ratios no six-decimal cut survives
v = pcbnew.PCB_VIA(b)
v.SetPosition(P(70, 1)); v.SetWidth(MM(0.8)); v.SetDrill(MM(0.4)); v.SetNet(net)
v.SetBackdrillMode(pcbnew.BACKDRILL_MODE_BACKDRILL_BOTH)
v.SetTopBackdrillSize(MM(0.65)); v.SetTopBackdrillLayer(pcbnew.In1_Cu); v.SetBottomBackdrillSize(MM(0.6)); v.SetBottomBackdrillLayer(pcbnew.In2_Cu)
v.SetFrontPostMachiningMode(pcbnew.PAD_DRILL_POST_MACHINING_MODE_COUNTERBORE); v.SetFrontPostMachiningSize(MM(1.0)); v.SetFrontPostMachiningDepth(MM(0.3))
v.SetBackPostMachiningMode(pcbnew.PAD_DRILL_POST_MACHINING_MODE_COUNTERSINK); v.SetBackPostMachiningSize(MM(1.1)); v.SetBackPostMachiningAngle(823)
v.SetTeardropsEnabled(True); v.SetTeardropBestLengthRatio(0.1234567891); v.SetTeardropBestWidthRatio(0.9876543219)
b.Add(v)

# ---- a zone with doubles: hatch orientation and hole area
z = pcbnew.ZONE(b)
z.SetLayer(pcbnew.In1_Cu); z.SetNet(net)
o = z.Outline(); o.NewOutline()
for x, y in ((60, 0), (80, 0), (80, 20), (60, 20)):
    o.Append(MM(x), MM(y))
z.SetFillMode(pcbnew.ZONE_FILL_MODE_HATCH_PATTERN); z.SetHatchThickness(MM(0.3)); z.SetHatchGap(MM(0.4))
z.SetHatchOrientation(pcbnew.EDA_ANGLE(12.3456789, pcbnew.DEGREES_T)); z.SetHatchHoleMinArea(0.3333333333); z.SetHatchSmoothingValue(0.123456789)
b.Add(z)

pcbnew.SaveBoard(out, b)

# ---- what the bindings cannot build, in KiCad's own grammar; KiCad then saves the whole file
extra = """
	(table (column_count 2) (locked yes) (layer "F.SilkS") (uuid "00000000-0000-0000-0000-00000000000a")
		(border (external yes) (header yes) (stroke (width 0.15) (type solid)))
		(separators (rows yes) (cols no) (stroke (width 0.1) (type dash)))
		(column_widths 3 4) (row_heights 2)
		(cells
			(table_cell "A" (start 20 70) (end 23 72) (margins 0.1 0.2 0.3 0.4) (span 1 1) (layer "F.SilkS") (uuid "00000000-0000-0000-0000-00000000000b") (effects (font (size 1 1) (thickness 0.15) (bold yes)) (justify left)))
			(table_cell "B" (start 23 70) (end 27 72) (layer "F.SilkS") (uuid "00000000-0000-0000-0000-00000000000c") (effects (font (size 1 1) (thickness 0.15))))))
	(point (at 3 70) (size 1.5) (layer "F.SilkS") (uuid "00000000-0000-0000-0000-000000000007"))
	(segment (start 0 80) (end 5 80) (width 0.2) (layer "F.Cu") (net "GND") (uuid "00000000-0000-0000-0000-000000000011"))
	(generated (uuid "00000000-0000-0000-0000-00000000000d") (type tuning_pattern) (name "t") (layer "F.Cu")
		(base_line (pts (xy 0 80) (xy 5 80))) (corner_radius_percent 80) (end (xy 5 80)) (initial_side "default")
		(last_diff_pair_gap 0) (last_netname "GND") (last_status "unset") (last_track_width 0.2) (last_tuning "unset")
		(max_amplitude 1) (min_amplitude 0.2) (min_spacing 0.6) (origin (xy 0 80)) (override_custom_rules no) (rounded yes)
		(single_sided no) (tuning_mode "single") (members "00000000-0000-0000-0000-000000000011"))
	(image (at 90 90) (layer "F.SilkS") (scale 2) (locked yes) (uuid "00000000-0000-0000-0000-000000000012")
		(data "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGP4z8AAAAMBAQDJ/pLvAAAAAElFTkSuQmCC"))
	(gr_text "FACE" (at 90 80 12.5) (layer "F.SilkS") (uuid "00000000-0000-0000-0000-000000000013")
		(effects (font (face "Arial") (size 1 1.5) (thickness 0.2)) (justify right bottom)))
	(dimension (type aligned) (layer "Dwgs.User") (uuid "00000000-0000-0000-0000-000000000014") (pts (xy 90 60) (xy 95 60)) (height 1)
		(format (prefix "") (suffix "") (units 3) (units_format 1) (precision 4))
		(style (thickness 0.1) (arrow_length 1.27) (text_position_mode 0) (arrow_direction outward) (extension_height 0.58) (extension_offset 0.5) (keep_text_aligned yes))
		(gr_text "5 mm" (at 92.5 59 0) (layer "Dwgs.User") (uuid "00000000-0000-0000-0000-000000000014") (effects (font (size 1 1) (thickness 0.15)))))
"""
text = open(out).read().rstrip()
assert text.endswith(")")
open(out, "w").write(text[:-1] + extra + ")\n")
run = subprocess.run(["kicad-cli", "pcb", "upgrade", "--force", out], capture_output=True, text=True)
print(run.stdout.strip(), run.stderr.strip())
if run.returncode != 0:
    sys.exit(run.returncode)
print("saved")
