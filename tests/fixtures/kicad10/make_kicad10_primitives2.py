import pcbnew, sys
from pcbnew import VECTOR2I, FromMM as MM
b = pcbnew.BOARD(); b.SetCopperLayerCount(4)
net = pcbnew.NETINFO_ITEM(b, "GND"); b.Add(net)
def P(x,y): return VECTOR2I(MM(x),MM(y))
res={}
def T(name, fn):
    try: fn(); res[name]="ok"
    except Exception as e: res[name]=repr(e)[:120]
# via: padstack front/inner/back
v = pcbnew.PCB_VIA(b); v.SetPosition(P(1,1)); v.SetWidth(MM(0.6)); v.SetDrill(MM(0.3)); v.SetNet(net)
def ps():
    p = v.Padstack(); p.SetMode(pcbnew.PADSTACK.MODE_FRONT_INNER_BACK); p.SetSize(VECTOR2I(MM(0.8),MM(0.8)), pcbnew.F_Cu); p.SetSize(VECTOR2I(MM(0.5),MM(0.5)), pcbnew.PADSTACK.INNER_LAYERS); v.SetPadstack(p)
T("padstack", ps)
b.Add(v)
v2 = pcbnew.PCB_VIA(b); v2.SetPosition(P(3,1)); v2.SetWidth(MM(0.6)); v2.SetDrill(MM(0.3)); v2.SetNet(net)
T("front_tent_only", lambda: v2.SetFrontTentingMode(pcbnew.TENTING_MODE_TENTED))
T("pm", lambda: (v2.SetFrontPostMachining(pcbnew.PAD_DRILL_POST_MACHINING_MODE_COUNTERBORE), v2.SetFrontPostMachiningSize(MM(0.9)), v2.SetFrontPostMachiningDepth(MM(0.2))))
T("pmb", lambda: (v2.SetBackPostMachining(pcbnew.PAD_DRILL_POST_MACHINING_MODE_COUNTERSINK), v2.SetBackPostMachiningSize(MM(0.9)), v2.SetBackPostMachiningAngle(900)))
T("sec", lambda: (v2.SetSecondaryDrillSize(MM(0.35)), v2.SetSecondaryDrillStartLayer(pcbnew.F_Cu), v2.SetSecondaryDrillEndLayer(pcbnew.In1_Cu)))
T("bdmode", lambda: (v2.SetBackdrillMode(pcbnew.BACKDRILL_MODE_BACKDRILL_BOTTOM), v2.SetBottomBackdrillSize(MM(0.33)), v2.SetBottomBackdrillLayer(pcbnew.In2_Cu)))
b.Add(v2)
v3 = pcbnew.PCB_VIA(b); v3.SetPosition(P(5,1)); v3.SetWidth(MM(0.6)); v3.SetDrill(MM(0.3)); v3.SetNet(net)
T("fillflag", lambda: v3.SetPrimaryDrillFilledFlag(True)); T("capflag", lambda: v3.SetPrimaryDrillCappedFlag(True))
T("zlo2", lambda: (v3.SetZoneLayerOverride(pcbnew.B_Cu, pcbnew.ZLO_FORCE_NO_ZONE_CONNECTION)))
b.Add(v3)
t = pcbnew.PCB_TRACK(b); t.SetStart(P(1,2)); t.SetEnd(P(5,2)); t.SetWidth(MM(0.25)); t.SetLayer(pcbnew.B_Cu); t.SetNet(net); T("mask", lambda: t.SetHasSolderMask(True)); b.Add(t)
# net-less copper zone, multi outline
z = pcbnew.ZONE(b); z.SetLayer(pcbnew.F_Cu)
o = z.Outline(); o.NewOutline(); [o.Append(MM(x),MM(y)) for x,y in ((0,0),(5,0),(5,5))]
o.NewOutline(); [o.Append(MM(x),MM(y)) for x,y in ((10,10),(15,10),(15,15))]
b.Add(z)
z2 = pcbnew.ZONE(b); z2.SetLayer(pcbnew.F_Cu); z2.SetNet(net); o = z2.Outline(); o.NewOutline(); [o.Append(MM(x),MM(y)) for x,y in ((20,0),(25,0),(25,5))]
T("tdz", lambda: z2.SetTeardropAreaType(1)); b.Add(z2)
# rule area with placement enabled
k = pcbnew.ZONE(b); k.SetIsRuleArea(True); k.SetLayer(pcbnew.F_Cu); o=k.Outline(); o.NewOutline(); [o.Append(MM(x),MM(y)) for x,y in ((30,0),(35,0),(35,5))]
T("pl", lambda: (k.SetPlacementAreaEnabled(True), k.SetPlacementAreaSourceType(1), k.SetPlacementAreaSource("grp")))
b.Add(k)
print(res)
pcbnew.SaveBoard(sys.argv[1], b)
