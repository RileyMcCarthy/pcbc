import pcbnew, sys
from pcbnew import VECTOR2I, FromMM as MM
b = pcbnew.BOARD()
b.SetCopperLayerCount(4)
net = pcbnew.NETINFO_ITEM(b, "GND"); b.Add(net)
def P(x,y): return VECTOR2I(MM(x),MM(y))
# plain track
t = pcbnew.PCB_TRACK(b); t.SetStart(P(1,1)); t.SetEnd(P(5,1)); t.SetWidth(MM(0.25)); t.SetLayer(pcbnew.F_Cu); t.SetNet(net); b.Add(t)
# every-option track
t = pcbnew.PCB_TRACK(b); t.SetStart(P(1,2)); t.SetEnd(P(5,2)); t.SetWidth(MM(0.25)); t.SetLayer(pcbnew.F_Cu); t.SetNet(net); t.SetLocked(True)
try: t.SetHasSolderMask(True); t.SetLocalSolderMaskMargin(MM(0.05))
except Exception as e: print("tr mask", e)
b.Add(t)
a = pcbnew.PCB_ARC(b); a.SetStart(P(1,3)); a.SetMid(P(3,4)); a.SetEnd(P(5,3)); a.SetWidth(MM(0.2)); a.SetLayer(pcbnew.B_Cu); a.SetNet(net); a.SetLocked(True)
try: a.SetHasSolderMask(True); a.SetLocalSolderMaskMargin(MM(0.05))
except Exception as e: print("arc mask", e)
b.Add(a)
v = pcbnew.PCB_VIA(b); v.SetPosition(P(10,10)); v.SetWidth(MM(0.6)); v.SetDrill(MM(0.3)); v.SetNet(net); b.Add(v)
tries = {}
v = pcbnew.PCB_VIA(b); v.SetViaType(pcbnew.VIATYPE_BLIND); v.SetPosition(P(12,10)); v.SetLayerPair(pcbnew.F_Cu, pcbnew.In1_Cu); v.SetWidth(MM(0.45)); v.SetDrill(MM(0.2)); v.SetNet(net)
for name, fn in [("locked", lambda: v.SetLocked(True)), ("free", lambda: v.SetIsFree(True)), ("rm", lambda: v.SetRemoveUnconnected(True)), ("keep", lambda: v.SetKeepStartEnd(True)),
                 ("ft", lambda: v.SetFrontTentingMode(pcbnew.TENTING_MODE_TENTED)), ("bt", lambda: v.SetBackTentingMode(pcbnew.TENTING_MODE_NOT_TENTED)),
                 ("fc", lambda: v.SetFrontCoveringMode(pcbnew.COVERING_MODE_COVERED)), ("bc", lambda: v.SetBackCoveringMode(pcbnew.COVERING_MODE_NOT_COVERED)),
                 ("fp", lambda: v.SetFrontPluggingMode(pcbnew.PLUGGING_MODE_PLUGGED)), ("bp", lambda: v.SetBackPluggingMode(pcbnew.PLUGGING_MODE_NOT_PLUGGED)),
                 ("cap", lambda: v.SetCappingMode(pcbnew.CAPPING_MODE_CAPPED)), ("fill", lambda: v.SetFillingMode(pcbnew.FILLING_MODE_FILLED)),
                 ("zlo", lambda: v.SetZoneLayerOverride(pcbnew.F_Cu, pcbnew.ZLO_FORCE_FLASHED)),
                 ("td", lambda: v.SetTeardropsEnabled(True)), ("tdl", lambda: v.SetTeardropMaxLength(MM(1.5))), ("tdc", lambda: v.SetTeardropCurved(True)),
                 # A backdrill needs its layer and size: the mode alone writes `(layers "B.Cu" "UNDEFINED")`,
                 # which KiCad itself then refuses to load ("items on undefined layers").
                 ("bd", lambda: (v.SetBackdrillMode(pcbnew.BACKDRILL_MODE_BACKDRILL_BOTTOM), v.SetBottomBackdrillSize(MM(0.22)), v.SetBottomBackdrillLayer(pcbnew.In2_Cu))),
                 ]:
    try: fn(); tries[name]="ok"
    except Exception as e: tries[name]=repr(e)[:80]
b.Add(v)
print(tries)
for vt in ("VIATYPE_BURIED","VIATYPE_MICROVIA"):
    v = pcbnew.PCB_VIA(b); v.SetViaType(getattr(pcbnew,vt)); v.SetPosition(P(14,10)); v.SetLayerPair(pcbnew.In1_Cu, pcbnew.In2_Cu); v.SetWidth(MM(0.45)); v.SetDrill(MM(0.2)); v.SetNet(net); b.Add(v)
# zones
def zone(layer, pts):
    z = pcbnew.ZONE(b); z.SetLayer(layer); z.SetNet(net)
    o = z.Outline(); o.NewOutline()
    for x,y in pts: o.Append(MM(x),MM(y))
    return z
z = zone(pcbnew.F_Cu, [(0,0),(20,0),(20,20),(0,20)]); b.Add(z)
z = zone(pcbnew.B_Cu, [(0,0),(20,0),(20,20),(0,20)])
zt={}
for name, fn in [("prio", lambda: z.SetAssignedPriority(3)), ("name", lambda: z.SetZoneName("PLANE")), ("locked", lambda: z.SetLocked(True)),
                 ("fillmode", lambda: z.SetFillMode(pcbnew.ZONE_FILL_MODE_HATCH_PATTERN)), ("ht", lambda: z.SetHatchThickness(MM(0.3))), ("hg", lambda: z.SetHatchGap(MM(0.4))),
                 ("ho", lambda: z.SetHatchOrientation(pcbnew.EDA_ANGLE(45, pcbnew.DEGREES_T))), ("hsl", lambda: z.SetHatchSmoothingLevel(2)), ("hsv", lambda: z.SetHatchSmoothingValue(0.3)),
                 ("hba", lambda: z.SetHatchBorderAlgorithm(1)), ("hhma", lambda: z.SetHatchHoleMinArea(0.4)),
                 ("smooth", lambda: z.SetCornerSmoothingType(pcbnew.ZONE_SETTINGS.SMOOTHING_FILLET)), ("rad", lambda: z.SetCornerRadius(MM(0.5))),
                 ("isl", lambda: z.SetIslandRemovalMode(pcbnew.ISLAND_REMOVAL_MODE_AREA)), ("isla", lambda: z.SetMinIslandArea(MM(1)*MM(1))),
                 ("pad", lambda: z.SetPadConnection(pcbnew.ZONE_CONNECTION_THT_THERMAL)), ("clr", lambda: z.SetLocalClearance(MM(0.3))), ("mt", lambda: z.SetMinThickness(MM(0.2))),
                 ("tg", lambda: z.SetThermalReliefGap(MM(0.4))), ("ts", lambda: z.SetThermalReliefSpokeWidth(MM(0.35))),
                 ("border", lambda: z.SetBorderDisplayStyle(pcbnew.ZONE_BORDER_DISPLAY_STYLE_DIAGONAL_FULL, MM(0.7), True)),
                 ("placement", lambda: (z.SetPlacementAreaEnabled(True), z.SetPlacementAreaSource("sheet1"))),
                 ("hole", lambda: (z.Outline().NewHole(), [z.Outline().Append(MM(x),MM(y),0,0) for x,y in ((5,5),(8,5),(8,8))])),
                 ]:
    try: fn(); zt[name]="ok"
    except Exception as e: zt[name]=repr(e)[:100]
print(zt)
b.Add(z)
# second outline zone (multi-polygon)
z2 = zone(pcbnew.In1_Cu, [(0,0),(5,0),(5,5)]); o=z2.Outline(); o.NewOutline(); [o.Append(MM(x),MM(y)) for x,y in ((10,10),(15,10),(15,15))]
z2.SetPadConnection(pcbnew.ZONE_CONNECTION_NONE); b.Add(z2)
z3 = zone(pcbnew.In2_Cu, [(0,0),(5,0),(5,5)]); z3.SetPadConnection(pcbnew.ZONE_CONNECTION_FULL); b.Add(z3)
# keepout with every flag, multi-layer
k = zone(pcbnew.F_Cu, [(30,0),(35,0),(35,5)]); k.SetIsRuleArea(True); k.SetNetCode(0)
ls = pcbnew.LSET(); ls.AddLayer(pcbnew.F_Cu); ls.AddLayer(pcbnew.B_Cu); k.SetLayerSet(ls)
k.SetDoNotAllowTracks(False); k.SetDoNotAllowVias(True); k.SetDoNotAllowPads(True); k.SetDoNotAllowZoneFills(False); k.SetDoNotAllowFootprints(True); k.SetZoneName("KO")
b.Add(k)
# teardrop zone
tdz = zone(pcbnew.F_Cu, [(40,0),(41,0),(41,1)]); 
try: tdz.SetTeardropAreaType(pcbnew.TEARDROP_TYPE_TD_VIAPAD)
except Exception as e: print("td", e)
b.Add(tdz)
pcbnew.SaveBoard(sys.argv[1], b)
print("saved")
