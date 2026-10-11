# The SPICE stage: proof that the finished board carries its declared currents (design, version 2)

Draft 2026-10-01, from the owner's decision recorded in `docs/direction.md` §5c: *"spice is the last
interaction stage for ensuring we actually meet physical requirements. We should be able to run spice
without more info besides the physical requirements we already defined."* and, the same day, *"We should
optimistically use spice models for components if we can find them."*

Read `docs/direction.md` first (§5b net and path, pad necks, `not rated: pour`; §5c this stage; §7b errors;
§8 how anything gets believed); it wins over this document. `docs/net-path-physics.md` defines `Path()`,
stations, the junction rules and via groups that the testbenches below judge.

Nothing here is built. Four surveys and three reviews (build, owner's rules, physics) ran on 2026-10-01
against a frozen copy of the source (`rsync -a pcbc/ scratchpad/spice/frozen/ --exclude .venv --exclude
layout`, run with `PYTHONPATH=frozen/src .venv/bin/python`); every number below names the file that
produced it. Paths `s1/ s2/ t3/ models/` are the surveys, `rb/ rphys/` the reviewers' probes, `rev/` this
revision's probes, all under `scratchpad/spice/`. **Measured** numbers came from those runs; **illustrative**
error texts show the shape of a message with invented numbers and say so; **projected** numbers are
extrapolations and say from what.

---

## Summary

- The stage solves **DC current through the as-built copper** with ngspice and judges every trace piece and
  every via barrel against the rating of the same function that sized it (`current_width_mm` without its
  manufacturability floor, `via_amps`), with the plane credit read **off the board**, not from the design.
  That is the one family of requirements SPICE can give a real pass or fail on from what is already
  declared: `Path(amps=)` (a number, or `{station: A}` draws), via groups, off-path branches, and the Kelvin
  junction of feedback/sense taps. (`docs/physics-subjects.md`: a net declares no current, so the "every
  branch" bench B2 and the owner question Q1 are dropped; see the notes at each.)
- **What ngspice itself contributes is the network solve**: the split of a current among parallel copper.
  The extraction (geometry to ohms) and the ratings (IPC curves) are pcbc's own physics, and the cut-edge
  findings below need no simulator at all, only the conductor graph. So §5c's "a check we did not write
  the physics of" holds for the current split and for nothing else in this stage.
- Two testbenches: **B1** a path's current (one deck per Path that writes `amps=`; a draws-Path drives one
  source per drawing station), **B3** a sense tap meets its net only inside its first station's pad (measured
  as a voltage, not a current). **B2** (a net's current on every branch) is dropped: no net carries a current
  since `docs/physics-subjects.md` PR 3; its measurements below are kept as history. All are DC operating
  points; none needs a part model.
- On today's builds the stage finds, all measured (`rev/uncov.py`, `rev/runb2.sh`, `rphys/b2pair.py`):
  - **Fatal (unpoured copper):** four buck pieces at 0.781 mm carry 2 A with no copper under part of their
    length, where `current_width_mm` gives 1.09 mm with no plane: 5V into J_OUT.1 (about 0.2 mm), VIN out of
    J_IN.1 (about 0.2 mm), and two VIN transitions (0.746 and 0.745 mm). The design assumed the plane.
  - **Report, pending the owner (poured nets, §5b `not rated: pour`):** pin-to-pour vias over their rating
    under "every branch carries A": buck GND 6 of 6 (4 are the only copper to the pour, at 2.000 A), node
    3V3 14 of 17, node GND 44 of 47 (43 are the only copper, at 1.000 A). A 13.3 kΩ divider's ground via is
    among them.
- Everything else either is geometry pcbc and KiCad already check, or needs a number nobody declared (a drop
  budget, an operating bias, an edge rate, an IC model, a field solver). The stage names each such
  requirement as `not checked` with the reason; it never invents the number.
- Vendor models are fetched from the manufacturer by MPN, screened as text, pinned by hash and load-tested.
  On the five boards **only Samsung's MLCC static models are usable**, and with no declared operating bias
  they add a "model runs" row and no number. ST's USBLC6 model runs but LCSC's part is UMW's, so it is not
  the part. TI's TPS54202 model is encrypted. No verdict depends on a part model.
- The stage is a build stage, `spice`, between `route` and `fab`, built on `trace.tracing`; its stamp is
  written only when every row is `pass`, `report` or `not checked`, and fab refuses a routed board without
  it. Five decisions are the owner's (next section).

## Decisions for the owner

The stage is designed with a default for each; the default is what version 2 builds.

| # | Question | Measured consequence | Default |
|---|---|---|---|
| Q1 (dropped with B2) | §5b says a poured net's under-rated vias are `not rated: pour`, never `ok`, and sizing them is out of version 1. Should the stage make them `fail` (B2 "every branch")? | buck GND 6 vias, node 3V3 14, node GND 44 would block fab (§5 B2). Most are idle branches: R_FB_BOT.2's via carries about 44 µA in the circuit; the declaration says 2 A. | `report` (§5b stands) |
| Q2 | The plane credit read off the board fails a 0.781 mm, 2 A piece wherever any part of it lacks copper underneath, including the pour's clearance ring at a through-hole connector (about 0.2 mm). Should the §5b pad-neck allowance (about 1 mm at a pad) extend to an unplaned stretch at a pad? | buck: 2 of the 4 fatal pieces are such 0.2 mm stretches (J_OUT.1, J_IN.1); the other 2 (0.745 mm, 0.746 mm) are not at a pad and fail either way | `fail` (the curve as written) |
| Q3 | "Optimistically use models": with no operating bias declared and no part in a DC bench, a model yields only "runs" in the report. Is that what was meant? | Samsung MLCCs: 12 parts "runs"; no capacitance number printed | as stated |
| Q4 | LCSC identity mismatches (declared MPN or maker differs from the LCSC part): report, or a gate? | 4 MPN and 2 maker mismatches across the five boards (§6.2) | `report` |
| Q5 | Commit vendor model files? ST's says "All rights reserved"; Samsung's carry no terms | without them a fresh clone prints "model pinned, file not present" | gitignored, lock committed |

---

### Defaults (recommended 2026-10-01; Q1 follows from the owner's path-physics decision, Q2–Q5 are NOT yet confirmed by the owner)

- **Q1 — superseded.** There is no net-wide current any more (`docs/physics-subjects.md`, direction.md
  §5b): a via is judged by the current the solve puts through it, never by a number declared on its net.
  An idle branch (R_FB_BOT.2's via, about 44 µA) passes.
- **(proposed) Q2 — yes.** An unplaned stretch of at most the pad-neck length at a pad (the pour's clearance ring at a
  through-hole connector, about 0.2 mm) takes the pad-neck allowance; anywhere else the curve applies.
- **(proposed) Q3 — yes.** A capacitor model is evaluated at the voltage already declared on its net (`volts=`), so
  the report prints a real effective capacitance (buck's C_IN1, 22 µF nominal: 3.34 µF at 12 V). No new
  input: the bias is the net's own declared voltage.
- **(proposed) Q4 — the model is not used.** When the declared MPN or maker differs from the LCSC part, the vendor
  model describes a different part; the stage does not use it and the report says why, as a fact.
- **(proposed) Q5 — not committed.** Vendor model files are gitignored; a lock file of their hashes is committed and
  the tool fetches them on demand from the manufacturer.

---

## 1. What the stage proves, and what it does not

**It proves**, for each declared current and on the emitted board KiCad refilled:

1. Every piece of copper the current crosses (a segment, an arc, one via barrel between two layers, a
   thermal spoke) carries no more than its own width or drill is rated for, by the same curve and at the
   same temperature rise that `constraints.py` sized it with, with the plane credit the **board** gives
   that piece. The split of the current among parallel copper is solved by Kirchhoff over the real
   geometry, not assumed.
2. A branch no path names carries none of a path's current (§5b).
3. A feedback or sense path meets its net only inside its first station's pad (§5b's junction rule), shown
   by the voltage at its far station.

**What is independent of pcbc.** ngspice contributes the network solve. The ratings (`current_width_mm`,
`via_amps`) are pcbc's IPC formulas and the extraction (`R = ρL/(wt)`, the barrel annulus, the pour mesh) is
pcbc code. So the stage independently checks the places where pcbc's design **assumed** a split: the lumped
flow-through line (`stackup.via_group_shares`), "vias within 1.0 mm count as parallel"
(`ampacity.VIA_PARALLEL_MM`), and a poured net's tap vias (`not rated: pour`, `ampacity.py:394-407`). It does
**not** check equal sharing among vias that sit in one pad: that split depends on where the part's lead
meets the pad, which nobody declares (§3, pads).

Where a conductor is the only copper between two places (a cut edge of the conductor graph), its current is
the whole current by Kirchhoff's current law alone, at DC and at any frequency; no resistance value matters.
These findings need the conductor graph, not ngspice, and they are exact whatever the copper thickness,
resistivity or pour model, provided the graph's connectivity is right (§3: decided from polygon geometry,
not from the mesh).

**It does not prove**: that the IPC curves are right for this board (they are pcbc's design inputs, cited
in `stackup.py`); any temperature (there is no thermal solve; `temp_rise_c` enters only through the rating);
the split of a current that is mostly AC (§5.2); impedance, coupling, ringing or EMC; regulator stability,
ripple or output voltage; part voltage or current ratings; that the circuit works. Its rows say `pass`,
`fail`, `report` or `not checked`, never `verified` (§8: that word is KiCad's).

## 2. Inputs

Only what is declared, the copper KiCad saved, and part values. Exactly:

| Input | From | Used for |
|---|---|---|
| `Path(net, *stations, amps=, temp_rise_c=, kind=)` | `board.py`, compiled by `constraints.py` | B1 injection points and current (a number: in at the first station, out at the last; draws: out at each drawing station, the sum in at the first); B3 tap identity |
| `Loop(*pads)` | `board.py` | which shares are mostly AC (§5.2) |
| The IPC curves: `ipc2221_width_mm` and `ipc2152_width_mm` as `current_width_mm` (`stackup.py:832`) combines them, without `WIDTH_FLOOR_MM` (§5) | `stackup.py` | the pass rule for a trace piece |
| `via_amps(drill, plating, dt)` | `stackup.py:915` | the pass rule for a barrel |
| Stackup: each `Copper` layer's thickness, each `Dielectric`'s thickness, `via_plating_mm` 0.018 (`stackup.py:164`, JLC "average hole plating 18 um") | `stackup.py` | ohms per square per layer; barrel length between layers; barrel area; plane distance for the plane credit |
| `RHO_CU_OHM_M` 1.72e-8 Ω·m at 20 °C (`stackup.py:933`) | `stackup.py` | resistivity |
| Segments, arcs, vias, pads (shapes and layers), zones' thermal settings, and **filled** zone polygons of every net | the emitted, KiCad-refilled `layout/<name>/routed/layout.kicad_pcb` | extraction; the plane under each piece; thermal spokes |
| Which path owns each piece | `route_verify.gate_roles`, the reading `path_junctions` already uses | B1 off-path rule, B3 |
| Accepted pad necks | `route_verify.pad_necks` (live tree, being built now; the reading `ampacity._rate_necks` uses) | which pieces the neck rule judges instead of width (§5) |
| Part values, MPN, manufacturer | `board.py`, `components/<Mfr>/<MPN>/part.py` | report rows; model lookup |
| Vendor model files | `components/<Mfr>/<MPN>/spice/`, pinned by `model.lock` (§6) | model report rows only |

`NetReq(volts=)` is **not** an input: it sizes clearance and creepage (`docs/constraints.md:287, 435`), it is
not a DC potential, and one `NetReq` covers several nets (`node.py:144` gives VBUS, 3V3 and GND `volts=5`).

The fill exists only in the `.kicad_pcb` (`direction.md` §3), so extraction reads that file, like every
other gate; `layout.gen.py` is used only to name the line an error is about.

## 3. Extraction: copper to a resistor network

Validated 2026-10-02 (owner, 2026-10-01: validate the SPICE copper extractor). All paths below are under
`scratchpad/spice/validate/`: `cases/` holds the 37 cases and `FORMAT.md`; `ref/` is the reference solver, a P1
finite-element solve on Triangle meshes of each case's copper, converged to at most 0.07 % on resistances and
3e-4 A per ampere on via currents and exact against every closed form; `fv3/` is an independent re-derivation
(its own solver, ngspice on the same network, KiCad 10's own copper); `stage/` holds this method's runs. The
extractors of the surveys and the spike (`s2/extract_dc.py`, `dcx.py`, the spike's 1-D `extract.py` and its
raster `raster.py`) and the lumped `stackup.via_group_shares` are **not used for any verdict**. §3.3 says how
far each one was off and why.

**Copper is the union of a net's shapes on each layer.** The union takes segment stadiums, arcs, pads, via rings
and KiCad's filled polygons, then removes every drill hole (vias and plated through-hole pads). Overlapping
tracks, a track's run inside a pad or a via ring, a via landing under a wider track and a track lying in a
same-net fill all become one polygon. Nothing is modelled as a resistor in parallel with copper it overlaps. That
is what the 1-D extractor got wrong (§3.3).

**The network is a cut-cell finite-volume grid of that polygon.**
- Square cells of side **h = 0.04 mm** on every net. The grid is anchored to the board origin at multiples of h
  plus a fixed offset of 0.1234567 h.
- One node per cell that touches copper. A link joins neighbouring cells with conductance (t/ρ)·a/h, where a is
  the exact length of their shared face that lies inside the copper (scan-line crossings of the polygon edges).
  So a width is counted exactly wherever its edges fall on the grid.
- Equipotential regions (a pad under the (E) corner, a via's hole wall) are one node. A link from an outside
  cell into one is cut at the region's outline, at its exact distance (Shortley–Weller).
- The network is written as an ngspice netlist (R elements, one current source per excitation, values
  `%.12e`) and solved by ngspice-47 with `.options klu` and `op`. The cells, the scan line and the outline
  distances are pcbc code; ngspice solves the network.

| Item | Model | Measured | Determinism |
|---|---|---|---|
| Segment, arc, overlap | The union polygon, nothing else (above). | Resistance within 0.12 % of the reference at h = 0.04 on every trace case: straight, step, bend, pad exits, parallel strips, and P8a/b/c overlapping and contained tracks (`stage/errtab.txt`). On six extra 0.15 and 0.2 mm strips at 0°, 30° and 45° (0.15 mm is `WIDTH_FLOOR_MM`) it is within 0.07 % of ρ·8/(w·t) (`stage/mk_narrow.py`). | Grid fixed by the board origin and h |
| Pour | KiCad's `filled_polygon`, the same cells as the tracks. No separate mesh, no ties. | K1–K5 textbook pours within 0.12 %. Buck's whole GND net within 0.08 % (hole wall) and 0.59 % (sheet) on resistance, and 0.0014 A per A on via currents. | as above |
| Via barrel | Solved two ways, and the verdict takes both (§3.4). **(hole_wall)**: the drill-hole circle on each layer is one node, and the two are joined by ρh/(π((r+p)² − r²)), p = `via_plating_mm`. **(sheet)**: the barrel is a plated cylinder, thickness p at radius r + p/2, resistive along its length and around its circumference. Each cell link into the hole lands on the barrel's end row at the azimuth where it crosses. In both, the via's ring is plain copper. **Not used: (ring_disc)**, the whole ring as one node (the spike's convention). It shorts about 57 % of the 1.397 mm pitch along buck's landings and reads 361e2165 6.5 % low against hole_wall. | Against the reference with the same attachment: hole_wall within 0.0012 A per A, sheet within 0.0017 A per A. Neither attachment models the 3-D joint of barrel and ring; on 361e2165 the two are 4.0 % apart. | Closed form (hole_wall); a grid of max(32, 2πr/h) around and ≥ 8 along (sheet) |
| Pad | Solved two ways, and the verdict takes both (§3.4). **(E)**: every pad is one node (a lead covering the pad). **(U)**: an SMD pad is plain copper with the station's current spread evenly over its area; a through-hole pad's current enters at its drill-hole wall, and a barrel of `via_plating_mm` joins its layers. (U) replaces version 1's (M) "current at the pad centre": a point contact has no limit in 2-D, because its resistance grows without bound as the mesh is refined (`cases/FORMAT.md`). A via whose hole lies inside a pad stays a via: the pad region stops at its hole wall (`stage/fvcore.py`, P9a). | Over every pad pair at 2 A, moving from (E) to (U) changes buck's VIN via currents by at most 0.0019 A. On GND it changes 78b6ed06 by up to 0.20 A (R_FB_BOT.2→J_OUT.2), and its worst pair goes from 1.172 to 1.294 A (`stage/pairs.py`). It moves the 5V L1.2→J_OUT.1 resistance from 6.2487 to 6.540 mΩ (L1.2 spread) and to 7.277 mΩ with all three changes (`fv3/out/B2_terms.txt`). | as above |
| Thermal spoke | Read off the fill like any copper: the spoke is part of the union polygon. | Not exercised: every pour on the five boards is `connect="solid"` (`rphys` P7). | as above |

**Connectivity is decided from polygon geometry.** Two pieces are joined when their copper polygons are one
polygon on a layer, or when a barrel joins them. The island check runs on that graph before ngspice. The stage
then asserts that the cell network has the same connected components (`scipy.sparse.csgraph` in the
validation). At h = 0.04 every case and every buck net passed. At **h = 0.08 the network of
`B1_VIN_group_crop2` lost a joint**: the current was injected into an island (`stage/out_coarse/`). So h is
not raised to save time. A net whose network differs from its polygon graph is extracted again at h/2, down to
0.01 mm, and is an error naming the piece if it still differs. This check is design: it is not built.

**A piece's current** is the net current across a cut through the copper, perpendicular to the piece at the
middle of each sub-run: the sum of the link currents the cut crosses, which is a graph cut of the network. Where
a piece is the only copper between two places, Kirchhoff makes this exact. A split between parallel copper was
checked on P4 (0.5 and 1.0 mm strips, exactly 1:2): within 6e-6 A per A at every h and offset
(`stage/fvstage.py` `probe_current`). No other reference for the split of a piece's current exists.

**The plane under each piece is read off the board.** For each trace piece the stage samples its footprint (5
points across, every 0.01 of its length; `rev/uncov.py`) against the filled zone polygons of every net on the
copper layers above and below it. A sub-run whose whole cross-section lies over fill on a layer gets
`plane_h` = the stack distance to the nearest such layer; any other sub-run gets `plane_h = None`. A track
or pad is not a plane. A sub-run inside pad copper is pad, not judged as track. The rule is the conservative
reading of IPC-2152's plane modifier (a plane under the conductor); no coverage fraction is chosen. Q2 asks
the owner whether a short unplaned stretch at a pad is allowed.

**Determinism.** Building every buck net twice gave byte-identical netlists: sha256 equal on all 7 nets
(`stage/board_time.txt`, `stage/board_time_rerun.txt`). ngspice's solution of a netlist agrees with scipy's
direct solve of the same matrix within 8.1e-7 A per A on via currents and 5e-9 V on terminals
(`stage/board_chain.txt`, `stage/spice_vs_scipy.py`). The grid offset is fixed. Across three offsets the answers
moved by at most the spreads in `stage/errtab.txt`, and those spreads are inside the bound in §3.3.

**Runtime on buck's whole board.** These are the most a declaration can ask: all 7 nets, every pad as a
superposition basis, h = 0.04, in one ngspice deck per net with `alter` and `op` per basis, on an M2 with 16 GB
(`stage/board_chain.py`).

| Corner | Python (build + write) | ngspice | Largest deck |
|---|---|---|---|
| hole_wall, (E) | 2.1 s | 131.9 s | GND: 1,155,439 resistors, 7 `op`, 130.6 s |
| sheet, (E) | 2.2 s | 101.5 s | GND: 1,175,643 resistors, 7 `op`, 100.2 s |

- The (U) corners were not run in ngspice. Their networks are within 0.3 % of the (E) size, and scipy solved
  them in the same time (`stage/out/board_*.json` info). **Projected**: about 8 minutes for all four corners,
  99 % of it the GND pour.
- One GND `op` at h = 0.04 takes 14.5 s and 2.9 GB, after 4.6 s of parsing.
- At h = 0.02 the same deck takes 62 s and 6.5 GB for one `op`. Pour decks therefore run one at a time.
- Under buck's declarations today (paths on SW and 5V, no vias, no pours on them) the decks are 5,073 and
  14,537 resistors, under 0.2 s each.
- A mixed grid (coarser cells in a pour's interior) was **not measured**.

**What it needs.** numpy and shapely (union, containment, outline distance). The pcbc `.venv` has neither
(checked 2026-10-02), so the build adds them or ports the polygon union and the scan line to pure Python. The
cost of a pure-Python port was not measured.

### 3.1 What the resistances depend on

- **Copper thickness.** R ∝ 1/t. The stackup's 0.035 mm is nominal foil; finished outer copper is plated
  up, and the fab's finished value was not established. It is likely the largest error in every drop. It does
  not move a cut edge's current. It moves a share only through the ratio of foil to barrel plating where tracks
  and vias are in parallel. Measured on buck's VIN group with this extraction (`fv3/out/sens.txt`, 2 A
  J_IN.1→R_EN.1, current through 361e2165 as hole_wall / sheet):

  | Copper t | 361e2165 at 2 A |
  |---|---|
  | 0.030 mm | 1.073 / 1.114 A |
  | 0.035 mm | 1.041 / 1.083 A |
  | 0.043 mm | 0.999 / 1.042 A |
  | 0.048 mm | 0.977 / 1.020 A |

  For plating p, the current at 2 A is 1.002 / 1.045 A at p = 0.015 mm and 1.113 / 1.152 A at p = 0.025 mm,
  against `via_amps` ratings of 0.763 and 1.106 A. The verdict is for the declared stackup; these inputs are not
  part of the extraction's bound (§3.3). The earlier figure here, "reaches its 0.871 A rating only if the
  rail/rung ratio reaches 2.38", came from the lumped flow-through closed form, which assumes the wrong path for
  this board (§3.3).
- **Temperature.** At one uniform temperature ρ cancels out of every share. Drops are reported at 20 °C, the
  temperature of `RHO_CU_OHM_M`'s source. No temperature coefficient is used: none is sourced in pcbc, and no
  operating temperature is declared.
- **The barrel annulus.** `via_group_shares` and this extraction put the plating outside the drill,
  πp(d+p); `via_amps` rates πdp; inside the drill it would be πp(d−p). For 0.3 mm: 0.01798, 0.01696 and
  0.01595 mm² (`rphys` P9). Resistance and rating therefore use different areas; the rating stays the sizing
  function (K2). Whether JLC's "drill" is the drilled or the finished hole is not established. With the
  inward annulus, 361e2165 carries 1.023 A instead of 1.0415 A (hole_wall, h = 0.04, 2 A J_IN.1→R_EN.1;
  `stage/`, command in §3.3), so no verdict moves. The 0.747 / 0.843 A that stood here were the lumped model's.

### 3.2 Inductance and capacitance: not in version 2

Survey 2's closed forms, kept for the record and **not used for any verdict**:

| Item | Closed form | Why not used |
|---|---|---|
| Trace over an unbroken plane | `L′ = Z0·√εeff/c` from pcbc's Hammerstad–Jensen: 0.541 nH/mm (0.781 mm on 1.53 mm core), 0.787 nH/mm (0.2 mm) | Only valid over an unbroken plane; buck's B.Cu GND pour is cut by the B.Cu VIN tracks. And it is pcbc's own impedance formula, so not independent (§5c). |
| Via | Johnson–Graham 1.30 nH, Goldfarb–Pucel 0.54 nH, solid-rod partial self about 0.66 nH (0.3 mm drill, 1.6 mm) | 2.4× spread; the answer depends on where the return current goes. |
| Trace partial self L | Rosa/Grover bar formula: 11.142 × 0.781 mm bar = 8.52 nH | Meaningful only summed round a loop with mutuals. |
| C to the plane | 0.065 pF/mm (0.781 mm), 0.0425 pF/mm (0.2 mm); parallel plate 0.0266 pF/mm² on 1.53 mm | No declared requirement uses it. |

**Where the DC split stops describing the board.** It is not the skin depth (35 µm on 1 oz at 3.56 MHz) but
the frequency at which a branch's inductance matches its resistance. From the values above: a 0.781 mm trace
over plane, R′/(2πL′) = 0.6292 mΩ/mm / (2π × 0.541 nH/mm) = 185 kHz; a 0.3 mm barrel, 1.530 mΩ with 0.54 /
0.66 / 1.30 nH, 451 / 369 / 187 kHz (`rphys` P8). Above that, current divides by inductance. So a DC share
describes a DC-dominated current; §5.2 says which shares the stage therefore does not judge.

### 3.3 Validation: the measured error

Each case was solved at h = 0.04, 0.02 and 0.01 mm, at three grid offsets (0.1234567, 0.5 and 0.8765 h), under
both via attachments. The numbers are compared with the reference at the same attachment, at its finest mesh.
Commands, from `validate/stage/`:
- `../venv/bin/python fvstage.py ../cases/<case>.json --h 0.04,0.02,0.01 --attach hole_wall,sheet --off 0.1234567,0.5,0.8765 --out out/<case>.json`
  (B3_GND_full at 0.04 and 0.02, one offset)
- `python3 errtab.py > errtab.txt`

Resistance is V/I between an excitation's terminals. Via current is in A per ampere of the excitation, for every
via carrying at least 0.01 A per A. Each cell is the worst over every quantity, excitation and offset:

| Cases | Attachment | R at 0.04 / 0.02 / 0.01 | Via current at 0.04 / 0.02 / 0.01 (A per A) |
|---|---|---|---|
| K1–K5 pours with known answers, aligned and offset | hole_wall | −0.12 / −0.03 / −0.014 % | – |
| K4 via in a pour | sheet | +0.31 / +0.15 / +0.07 % | – |
| P1–P4, P7a–c: strip, step, bend, parallel strips, pad exits | hole_wall | +0.06 / +0.02 / +0.02 % | – |
| P8a–c: overlapping and contained tracks | hole_wall | −0.12 / −0.06 / −0.02 % | – |
| P5, P9a, P9b via strips and ladders | hole_wall | +0.04 / +0.01 / +0.003 % | 0.00003 / 0.00001 / 0.00000 |
| P5, P9a, P9b | sheet | +0.42 / +0.23 / +0.08 % | 0.0013 / 0.0007 / 0.0003 |
| B1: buck VIN, 7 crops and the whole net | hole_wall | +0.31 / +0.26 / +0.10 % | 0.0012 / 0.0008 / 0.0003 |
| B1 | sheet | +0.32 / +0.26 / +0.10 % | 0.0017 / 0.0011 / 0.0004 |
| B2: buck 5V, the whole net | – | −0.02 / −0.004 / −0.001 % | – |
| B3: buck GND, 4 pour crops and the whole net | hole_wall | +0.08 / +0.05 / +0.05 % | 0.0001 / 0.00004 / 0.00003 |
| B3 | sheet | +0.59 / +0.19 / +0.09 % | 0.0014 / 0.0005 / 0.0002 |

- P4's 1:2 split is within 6e-6 A per A.
- The B3 sense tap R_FB_TOP.1 sits within 2.7e-10 V per A of C_OUT1.1 (exactly 0).
- The six narrow strips are within 0.07 % (§3).
- Coarser grids were tried on 9 cases (`stage/out_coarse/`). At 0.06 mm the error grows to 1.5 % and 0.0034 A
  per A. At 0.08 mm it grows to 2.7 % and 0.0069 A per A, and one crop lost a joint (§3, connectivity).

**The extraction's bound at h = 0.04** is the measured worst plus the reference's own uncertainty:

| Quantity | Bound |
|---|---|
| A resistance between terminals | 0.7 % |
| A via barrel's current | 0.002 A per ampere of the excitation |
| A piece's current where copper splits | 0.002 A per ampere (P4 is the only reference; Kirchhoff makes an only-path piece exact) |
| A sense tap's potential (B3) | 1e-9 V per ampere |

This bound covers the discretisation only. It does not cover two other things:
- **The modelling corners** (barrel attachment, pad contact). They are not errors of known size; they are
  solved, and the verdict takes the interval over them (§3.4).
- **The declared inputs** t, p and ρ (§3.1).

**What the earlier methods got wrong**, on the disputed via. The case is buck variant B, via 361e2165 at
(21.89, 10.968), 0.4 mm drill, rated `via_amps(0.4, 0.018, 10)` = 0.871 A, with 2 A from J_IN.1 to R_EN.1.
The excitation came from build B's `NetReq("VIN", "GND", amps=2)` under the every-branch reading.

| Method | 361e2165 | Against hole_wall / sheet | Why |
|---|---|---|---|
| Reference, hole_wall / sheet | 1.0407 / 1.0829 A (±0.0002) | – | `ref/`; reproduced by `fv3/` to four digits |
| This extraction, h = 0.04, four corners | 1.0415 – 1.0820 A | +0.08 % / −0.08 % | `stage/pairs.py VIN 2` |
| Spike raster, 0.025 mm | 0.977 A | −6.1 / −9.8 % | It solves the copper correctly, but merges the whole 0.8 mm ring into one node (ring_disc); at 0.00625 mm it converges onto ring_disc within 0.1 %. |
| Spike 1-D extractor | 0.858 A | −17.6 / −20.8 % | ring_disc as well, plus overlap handling. It adds a track lying inside another in parallel (P8a/b/c −23, −42, −8.7 %), and joins the 0.781 mm VIN feed to the 1.09 mm landing only at tie points, so too little current reaches the landing near 361e2165. Its corner and cap rules add +5.1 % on P3 and +4.6 % on P2. |
| `stackup.via_group_shares` (lumped) | 0.755 A | −27.5 / −30.3 % | It assumes the current enters at one end via and leaves at the other. On this board the current both arrives and leaves at 361e2165's end. On its own topology (P9a) it is still −13 % on the middle barrel, because it charges the pitch as plain landing. |

The 1-D extractor's "pass" and the lumped model's "pass" were wrong verdicts. All four methods of the
re-derivation put 361e2165 over 0.871 A under that excitation.

### 3.4 The verdict rule, with the bound

A piece is a sub-run of track at its cut (§3) or one via barrel between two layers. For each excitation of
current A (a `Path`'s `amps`, §5), the stage solves the four corners {hole_wall, sheet} × {(E), (U)} and takes
the piece's current in each:

- I_lo = the smallest |I| over the corners − 0.002·A
- I_hi = the largest |I| over the corners + 0.002·A
- The rating is the one that sized the piece (§5): `via_amps(drill, via_plating_mm, temp_rise_c)` for a barrel,
  and the current `current_width_mm` gives the sub-run's width for, with its `plane_h`, for a track.

| Condition | Row | Wording |
|---|---|---|
| I_lo > rating | `fail` | names the piece, I_lo–I_hi and the rating: "carries 1.038–1.086 A; rated 0.871 A" |
| I_hi ≤ rating | `pass` | |
| otherwise | `report` | "carries I_lo–I_hi A; rated R A; within the extraction's uncertainty", with the corner that gives I_hi |

The `report` row states a fact and suggests nothing (direction.md §7b). A `report` row does not withhold the
stamp (§8).

**Applied to buck variant B's copper:**
- Under the every-branch reading of `NetReq("VIN", "GND", amps=2)` that build B was routed from, the worst pair
  is J_IN.1→R_EN.1. Via 361e2165 carries 1.0415–1.0820 A in the four corners, so I_lo = 1.0375 A, which is
  above 0.871 A: **`fail`**, by at least 0.166 A (19 %).
- On the converter's input path, J_IN.1→U1.3 at the same 2 A, it carries 0.258–0.275 A, and the most any VIN
  via carries is 0.350 A (7525a5dc): **`pass`**.
- R_EN is `Resistor("R_EN", "100k", p1=VIN, p2=EN)`, about 0.1 mA in the circuit. So the fail exists only under
  the every-branch reading. That reading is superseded by "current is path" (direction.md §5b, Q1).
- Today's buck (working tree, 2026-10-02) declares no `Path` on VIN, so under the current rule no declared current crosses 361e2165.

## 4. Running ngspice

Survey 3 (`t3/`), build review (`rb/`), this revision (`rev/`); ngspice-47 Homebrew, macOS arm64.

- **One process per deck, the deck a file, a closed environment.** `subprocess.run(["ngspice", "-n", "-b",
  "deck.cir"], cwd=<decks dir>, env={"PATH": <ngspice's dir and /usr/bin:/bin>, "HOME": <decks dir>})`.
  `-n` stops ngspice reading `.spiceinit` from the working directory and from `HOME`; both ran in the review
  without it (`rb/`: a cwd `.spiceinit` with `set numdgt=3` changed printed values; one in `HOME` ran its
  `echo`). These files are ngspice control language, which can run `shell`. `SPICE_SCRIPTS` replaces the
  system `spinit` even under `-n` (`SPINIT_FROM_SPICE_SCRIPTS`), so the environment carries no `SPICE_*` or
  `NGSPICE*` variable. The runner records the env it passed, the cwd, ngspice's path and `ngspice -v`.
- A model that needs PSpice compatibility gets `-D ngbehavior=psa` on the command line instead of a
  `.spiceinit`. Measured: under `-n` the variable is set (`rev/dtest.cir`: `echo $ngbehavior` prints `psa`).
  **Not established**: that it changes netlist parsing as the `.spiceinit` line does; the model load test
  (§6.1) establishes it per model.
- `ngspice -b /dev/stdin` returned 1.752047e+13 V in survey 3 for a netlist that gives 2.764286e-03 from a file;
  the build review could not reproduce that on a trivial deck. The rule (always a file) costs nothing.
- `.options klu` on every deck, and the runner asserts the line `Using KLU as Direct Linear Solver` (printed
  once per `op`): an unknown option such as `.options klu_bogus` is ignored silently and the default prints
  `Using SPARSE 1.3`.
- **Results.** `set numdgt=15`, `set wr_vecnames`, `set wr_singlescale`, then `wrdata <file> v(n1) v(n2) …`
  with an explicit list of the node vectors the stage needs. The header row names every column; the first
  column is the scale and is dropped; values are read **by name**, lowercased (ngspice lowercases names).
  Without `wr_vecnames` the rows are unlabelled, and `wrdata … all` writes vectors in an order that is neither
  sorted nor declaration order and includes branch currents (`rb/b.cir`). Measured: a 5,000-vector list on
  one 43,905-character line gives one header and one value row (`rev/long.cir`). Branch currents are computed
  by the stage as ΔV/R from the element values it wrote. `.meas`, `print` and `echo $&var` round to 6 digits
  and are not used.
- Failures that exit with rc = 0 and are caught by the runner: a failed measure (`out of interval`); a
  floating node (`Warning: singular matrix: check node b`, then `v(b) = 0`). The stage checks its own network
  is one island before ngspice runs, and also scans for `singular matrix`. rc = 1 (bad model, `device already
  exists`) is an error with ngspice's first error line.
- **Names.** Nodes are `n<k>`, elements `R<k>`, sources `I<k>`, numbered in the sorted order of their geometric
  keys; no pad, net or part name appears in a deck. A map file beside each deck pairs every name with its
  piece and `layout.gen.py` line. Case collisions and SPICE-special characters cannot occur.
- **Paths.** Decks name only files in their own directory (`wrdata out.I3.txt …`) and run with that
  directory as cwd; a screened model's text is inlined into the deck, never `.include`d. So a deck is
  byte-identical from any build path.
- Determinism: one SHA over 5 runs for a 10k-node OP (KLU and Sparse each), 3 runs each of 33/100/1000
  section AC ladders, 3 runs of switched transients; 20 decks serial vs 8 threads identical (`t3/`). A 4-pad
  alter-loop deck gave byte-identical outputs over three runs, with and without `-n` (`rb/`). The buck GND B2
  deck at 0.1 mm gave 7 of 7 outputs byte-identical over two runs (`rev/run01`, `rev/run01b`). KLU and
  Sparse differ at about 1e-12 relative (`1.188166057329083e-02` vs `1.188166057328946e-02`), so the solver
  is pinned and recorded. Homebrew ngspice-47 and KiCad 10's bundled libngspice ("ngspice-45.2") gave the
  same SHA-256 over all 701 complex AC points of the 1000-section ladder (`t3/ct2.py`).
  **Not established**: Linux x86-64 against macOS arm64, and ngspice 42 (Ubuntu noble) against 47, nor
  whether noble's ngspice has KLU. PR 1 establishes it in CI before anything depends on it.

## 5. The testbenches

All three are DC operating points over the extracted network of one net. Current sources are the only
excitation. **One reference per deck**: one pad of the island is node 0; every other excitation is a
floating current source between two pads, so no ideal 0 Ω path through node 0 joins two places the board
does not join (`rphys` P10, `rb` R9). No part is in any bench: a station's pad is where the current enters or
leaves the board's copper.

**Tolerances (instrument, not requirement).** Currents are compared unrounded and printed to 3 dp in A.
"Carries none" means |I| ≤ 1e-9 × the injected current; B3's "no voltage" means |ΔV| ≤ 1e-9 × the path's
end-to-end drop. Both are 1,000× the solver's own scatter (KLU against Sparse, 1.2e-12 relative, §4); an
error that crosses one prints the tolerance and its derivation, so a row failing at 2e-9 is read as an
instrument limit, not a design fact.

**Which rule judges a piece.**

1. A piece that carries none (above) is not judged by width. Width floors are manufacturability, checked by
   `constraints.py` and DRC.
2. A piece `route_verify.pad_necks` accepts as a §5b pad neck is judged by that rule (it is listed in the
   report with its current), not by width, as `ampacity._rate_necks` already treats it. §5b wins over this
   document.
3. Any other trace piece: its width ≥ the IPC curve at its solved current I, the `dt` `constraints.py` used,
   and the `plane_h` the board gives that sub-run (§3). The curve is `current_width_mm`'s `max(IPC-2221
   external, IPC-2152 with modifiers)` **without** `WIDTH_FLOOR_MM` (0.15 mm), which `stackup.py:787` says is
   "not a point on any curve"; PR 4 adds that keyword to `current_width_mm` rather than a second formula.
   Width is compared, not amps, because the compiler sized by width: buck's 5V is 0.781 mm, sized by
   `ipc2221_ext` at exactly 2 A, yet `track_amps(0.781, 10, stack, 1.53)` returns 1.999 A.
4. A barrel: I ≤ `via_amps(drill, via_plating_mm, dt)`, with the same `- 1e-9` that `vias_per_change` uses.
5. A thermal spoke: as a trace piece of width `thermal_bridge_width`, with no plane credit.
6. Pour cells: reported (worst A per mm of cell width), not judged. No rating for plane copper is declared or
   sourced, and a pour neck is not separable from plane by any declared number: `not checked`, reason stated.

**Bounds.** Every share is solved at both pad models (E, M) and, on a poured net, both pitches (0.2, 0.1 mm),
and judged at the largest current; the error prints each value and which produced the verdict.

### B1. A path's current

Requirement: `Path(net, s1, …, sn, amps=A, temp_rise_c=dt)`, or with draws `amps={s_k: A_k}`: one current
source `I<k> n<s_k> n<s1> <A_k>` per drawing station instead of the single `I0`, so each link carries the sum of
the draws beyond it. On a poured rail (PR 8) the pour is the copper between the stations: each station's joint
(its tap vias, or a through-hole pad's barrel) is judged at its own draw, and the pour cells are reported.

Deck:

```
* B1 path, map in <deck>.map
.options klu
R<k> n<i> n<j> <ohms>       ; every segment piece, barrel span, pad and pour cell of the island
I0 n<sn> n<s1> <A>          ; floating: A enters at s1, leaves at sn
                            ; node 0 is the island's reference pad (sn's pad)
.control
set numdgt=15
set wr_vecnames
set wr_singlescale
op
wrdata out.txt v(n<...>) ... ; the nodes of every judged piece and every station
quit
.endc
.end
```

Analysis: `op`, once per path that writes `amps=`, at each pad bound and pitch.

Pass: every segment and barrel piece passes its rule at its solved current; and every segment and barrel
piece `gate_roles` assigns to no path carries none of A. Pour cells are not "a branch no path names"
(§5b's branches are routed copper); their current is reported. On a poured net, a B1 share that depends on
the pour mesh's resistance (not a cut edge) is `report: rests on pcbc's pour mesh, not refuted` until the
refuter of §10 agrees (§8).

Errors. The first is measured on the survey's buck build (`rev/uncov.py s1/buck/.../layout.kicad_pcb
layout.gen.py 5V`; the no-plane width is `current_width_mm(2, 10, jlcpcb_2l_1oz, None)`); the others are
illustrative:

```
spice: Path("5V") line 62 (2 A, L1.2 -> C_OUT1.1 -> J_OUT.1): Seg("5V", (36.001, 11.476), (37.55, 13.025)) layout.gen.py line 53, 0.781 mm on F.Cu, carries 2.000 A; for 0.197 mm before J_OUT.1's pad no copper lies under it on B.Cu, where current_width_mm gives 1.09 mm for 2.000 A at 10 C (ipc2152_fit, IPC-2152 Fig 5-1, 5-8, 5-11, no plane)
spice: Path("SW") line 60 (2 A, U1.SW -> L1.1): Via("SW", (18.40, 9.96)) layout.gen.py line 37, drill 0.3 mm, plating 0.018 mm, carries 1.214 A (pads E) / 1.198 A (pads M) of the 2.000 A that crosses F.Cu -> B.Cu at this group (3 barrels: 1.214, 0.452, 0.334 A at E); via_amps rates one barrel 0.707 A at 10 C (IPC-2221B eq. 6-2 internal)
spice: Path("5V") line 62 (2 A, L1.2 -> J_OUT.1): Seg("5V", (30.10, 13.20), (30.10, 15.40)) layout.gen.py line 88 belongs to no path and carries 0.412 A of this path's 2.000 A (tolerance 2.0e-9 A: 1e-9 of the path's current)
```

Report row, measured on the frozen buck (`5V_L1.2_J_OUT.1.cir`, `s2/extract_dc.py`, `rev/uncov.py`):

```
5V: path L1.2 -> C_OUT1.1 -> J_OUT.1 (Path line 62, 2 A): fail (above); every other piece 0.781 mm over plane carries 2.000 A (needs 0.781 mm); C_OUT2 stub and the feedback tap carry none; drop 12.61 mV at 20 C, pads E (pads M not measured)
```

The VIN line of three 0.4 mm vias on buck, measured (`VIN_C_IN2.1_R_EN.1.cir`, hand closed form in
`rphys`): shares 0.755 / 0.490 / 0.755 A against 0.871 A, pass. pcbc's design model for the same line,
`via_group_shares(3, 2, 0.4, 0.018, 1.6, 1.0, 0.781)`, gives (0.754957, 0.490087, 0.754957). The report
prints both, so the design model is checked on every board that has a group. (These vias are not in a pad.)

### B2. A net's current on every branch (dropped)

**Dropped** (`docs/physics-subjects.md` PR 3, owner 2026-10-01: *"no current number for nets"*): `NetReq(amps=)`
is refused, so there is no net-wide current to apply to every branch. A branch no Path names is judged by the
current the solve puts through it from the declared Paths (B1), and a model-less part's undeclared load is
"unmodelled part, not checked" (O1, `direction.md` §5c). What follows is the design as it stood, kept for its
measurements.

Requirement: `NetReq(..., amps=A, temp_rise_c=dt)`: "every branch can carry this" (§5b), on the copper no
amps-path owns.

"Every branch" read literally is every pair of the net's pads. By superposition it needs one solve per pad,
not per pair: with unit current from pad p to a fixed reference pad r, let `T_k(p)` be piece k's current;
then for current A from pad a to pad b, piece k carries `A·(T_k(a) − T_k(b))`, and the worst over all pairs
is `A·(max_p T_k(p) − min_p T_k(p))`. On one connected island this is at most A, and exactly A on a cut edge.
A value above A means the network is not one island, and is an instrument error.

Deck: as B1, with one current source per pad, all at 0 A, the reference pad node 0, and a control loop that
sets one at a time and writes only the judged pieces' nodes:

```
I<k> 0 n<pad k> 0           ; one per pad of the net except the reference, and except a sense path's stations beyond its first
.control
set numdgt=15
set wr_vecnames
set wr_singlescale
alter I0 dc=1
op
wrdata out.I0.txt v(n<...>) ...
destroy all
alter I0 dc=0
...                          ; once per source, in k order
quit
.endc
```

Analysis: `op` × (pads − 1) in one deck. Each `op` repeats the solve (`rb` R18: about 1.3 s per op at 90k
nodes); the claim of one factorisation in version 1 is withdrawn. Poured-net decks run one at a time (§9).

**Verdict class.** On an unpoured net B2 judges every piece no amps-path owns: `fail` is fatal. On a poured
net, a pin-to-pour via or spoke that does not carry A is §5b's `not rated: pour`; the stage turns that row into
`report` with the solved worst current, never `ok` and never `fail`, until the owner decides Q1. Every
other piece of a poured net is judged, and a verdict that depends on the pour mesh's resistance (not a cut
edge) is `report: rests on pcbc's pour mesh` until §10's refuter agrees.

**Wording.** B2's current is the declared requirement applied to every pad pair, not a current the board will
carry in operation; rows say "crosses", not "carries" (§7b: an error is true). B1 says "carries", because B1
injects the path's declared current.

Rows, measured on the survey's builds (`rphys/b2pair.py ... --ideal-pour`, `rev/runb2.sh`,
`rev/viafail.py`; `GND_J_OUT.2_U1.1.cir` re-run with `ngspice -b`: `(3.869948e-03 − 8.091896e-04) V /
1.530379e-03 Ω = 2.000 A`):

```
spice: report (not rated: pour, direction §5b): NetReq("VIN", "GND") line 55 (amps=2, every branch of GND): Via("GND", (14.8669, 9.96)) layout.gen.py line 35, drill 0.3 mm, plating 0.018 mm, is the only copper between U1.1 and the B.Cu pour, so 2 A between U1.1 and any other GND pad crosses it; via_amps rates it 0.707 A at 10 C (IPC-2221B eq. 6-2 internal)
spice: report (not rated: pour, direction §5b): NetReq("VIN", "GND") line 55 (amps=2, every branch of GND): Via("GND", (14.6329, 11.676)) layout.gen.py line 34, drill 0.3 mm, plating 0.018 mm, 1.588 A crosses it when 2 A flows between C_IN1.2 and J_IN.2 (in parallel with a 9.38 mm F.Cu trace; pour ideal, pads E); via_amps rates it 0.707 A at 10 C
spice: NetReq("VIN", "GND") line 55 (amps=2, every branch of VIN): Seg("VIN", (19.858, 5.38), (18.8934, 5.38)) layout.gen.py line 82, 0.781 mm on F.Cu, 2.000 A crosses it between R_EN.1 and J_IN.1 (it is the only copper between them); for 0.746 mm of it no copper lies under it on B.Cu, where current_width_mm gives 1.09 mm for 2.000 A at 10 C (ipc2152_fit, no plane)
```

"is the only copper between" is printed only for a cut edge of the conductor graph, which the stage computes.

**Today's builds, every row over its rating** (all at the declared A; "cut" = the only copper, exact):

| Board, net (A) | Class | Over rating | Of which cut edges | Source |
|---|---|---|---|---|
| buck GND (2 A) | report (pour) | 6 vias of 6: U1.1, R_FB_BOT.2, C_IN2.2, C_OUT2.2 at 2.000 A; C_IN1.2 1.588 A; C_OUT1.2/J_OUT.2 1.303 A; all 0.3 mm, 0.707 A | 4 | `rphys` P1 |
| buck GND (2 A) | **fail** | R_FB_BOT.2's 0.64 mm escape Seg (line 64), cut edge at 2.000 A, needs at least 0.781 mm (0.781 over plane, 1.09 without; its plane cover not measured) | 1 | `rb` R2 |
| buck VIN (2 A) | **fail** | 3 F.Cu 0.781 mm pieces with unplaned runs (lines 73, 82, 83), each at 2.000 A, need 1.09 mm | 3 | `rev/runb2.sh`, `rev/uncov.py` |
| node 3V3 (1 A) | report (pour) | 14 vias of 17, 0.2 mm, 0.527 A: 12 at 1.000 A, plus 0.901 and 0.531 A | 12 | `rphys` P1 |
| node GND (1 A) | report (pour) | 44 vias of 47, 0.2 mm, 0.527 A: 43 at 1.000 A, plus 0.769 A | 43 | `rev/out.node.GND.txt` |
| node LOAD (1 A) | – | no via; 1 Seg at 1.000 A (width not judged here) | – | `rev/out.node.LOAD.txt` |
| c3_usb GND, 3V3 (0.5 A) | not used | the probe printed rows above A on 3V3 (0.503, 0.524, 0.734 A at A = 0.5), which superposition forbids on one island; c3_usb stops in route (VBUS unrouted), so its copper is likely split | – | `rev/out.c3_usb.*.txt` |
| VBUS (node, c3_usb) | not run | unrouted | – | `s1/*.log` |
| blinky VCC, GND (0.05 A) | not run | no survey build | – | – |

The segment rows above were not judged against width by the probe except where named; the stage judges all
of them. The R_FB_BOT.2 escape row's verdict is §5b's: an unpoured piece on a poured net is judged
("every branch"), and only the pin-to-pour joint is `not rated: pour`. The pour model was ideal (one node) in
every probe; share rows (not cut edges) move with the mesh.

Under the declared semantics R_FB_BOT.2's via must carry 2 A though the divider draws about 44 µA in the
circuit (5 V / 113.3 kΩ); I²C pull-ups and decoupling capacitors draw no DC at all. That is the declaration
working as §5b wrote it, and Q1 is where the owner decides what it costs.

### B3. A sense tap meets its net only inside its first station's pad

Requirement: `Path(net, s1, …, kind="feedback" | "sense")` and §5b's junction rule.

The far station is left open (no part in the bench), so the tap carries no current wherever it joins: a
current test passes a tap that joins the load copper outside s1's pad (`rphys` P3, measured below). The
bench therefore measures the quantity the rule protects, the voltage the far station sees.

Deck: B1's deck for the tap's net, every amps-path on that net driven at once at its declared current, one
reference (one path's sink is node 0; every path a floating source). Pads at bound (E): an equipotential s1
pad makes ΔV exactly zero when, and only when, the tap meets the net inside it.

Pass: |V(far station) − V(s1's pad)| ≤ 1e-9 × the largest driven path's end-to-end drop.

Error, measured (`rphys/b3_mut.cir`: buck's 5V network with the tap moved from C_OUT1.1's pad to the middle
of the L1.2 → C_OUT1.1 piece, no loop formed; `ngspice -b`; the current in the tap is −2.0e-16 A, so a
current test passes it):

```
spice: Path("5V") line 64 (kind="feedback", C_OUT1.1 -> R_FB_TOP.1): R_FB_TOP.1 sits 1.360 mV above C_OUT1.1's pad while Path("5V") line 62 carries 2 A; the tap's copper meets the 5V load copper at (<x>, <y>) on F.Cu, outside C_OUT1.1's pad (tolerance 1.7e-11 V: 1e-9 of the path's 16.74 mV)
```

The point named is the tap node that touches copper the tap does not own nearest s1. (The coordinates and the
drop basis are illustrative in this line; the 1.360 mV is measured.)

Report row, measured on the frozen buck with the bench as specified (`rphys/b3_orig.cir`, far end open): both
R_FB_TOP.1 and C_OUT1.1 sit 14.0211 mV above J_OUT.1 at 2 A, so B3 passes:

```
5V: feedback Path line 64 (C_OUT1.1 -> R_FB_TOP.1): pass; R_FB_TOP.1 is 14.02 mV above J_OUT.1 at 2 A on the 5V copper (track length to pad centres); ground side not checked: needs the IC's reference pin and its GND current, neither declared
```

This is not "the regulation point": the regulator holds V(FB) − V(U1.GND), and a drop between R_FB_BOT.2 and
U1.GND moves the output by that drop × (100 + 13.3)/13.3 = 8.52 (`rphys` P4). The survey's deck had a
44.13 µA divider draw at R_FB_TOP.1, which made the tap carry 44 µA and gave 14.020 vs 14.021 mV; the bench
does not have it.

`path_junctions` (`route_verify.py:1552`) checks the junction as geometry on routed copper. B3 checks it
electrically, through any copper on any layer: a tap that reaches the load copper through a via and a pour
on another layer shows a ΔV. A sense path on a poured net is refused by §5b and never reaches B3.

### 5.1 Report-only rows

Printed beside the verdicts, with the declaration they belong to, never a verdict:

- IR drop along each amps-path, end to end, at 20 °C, at both pad bounds (no drop budget is declared).
- Each feedback/sense tap's voltage above the path's last station (B3), with the ground side `not checked`.
- The worst pour cell current per mm of width, under each B1 path's declared current (B2's pairwise solves
  write only judged pieces, §9).
- Each via group not in a pad: the solved shares beside `via_group_shares`'s prediction.
- Each accepted pad neck and its current.

### 5.2 Shares of a current that is mostly AC

A cut edge carries the whole current at any frequency. A **share** among parallel copper is a DC share, and
§3.2 puts the crossover to inductive division at 185–451 kHz on today's geometry. The only declaration that
marks copper as carrying a chopped current is `Loop(*pads)` (buck: `Loop("C_IN1.1", "U1.VIN", "U1.GND",
"C_IN1.2")`, line 66). So a share whose worst pad pair are both pads of one declared `Loop` is `not checked:
DC share; the copper between Loop(...) line <n>'s pads carries a switched current, and the split of an AC
current needs an inductance extraction`. On buck today this judges nothing differently: C_IN1.1 → U1.3 and
C_IN2.1 → U1.3 are traces with no via (1.080 and 1.049 mΩ, `rphys/dcx_r.py`) and U1.1's via is a cut edge.
Inductor current on SW is DC-dominated and stays judged.

## 6. Component models

The owner's rule (`direction.md` §5c): the tool looks for each part's model from its manufacturer by MPN,
keeps it beside the part, uses it when it loads and runs in ngspice, and the report names per part the
model used (source, licence) or why there is none. The AI never supplies one. Survey 4 (`models/`,
`screen.py`, `zsweep.py`, `sem_fetch.py`) ran this by hand.

### 6.1 Finding, screening, pinning, using

Models are fetched by an explicit command (`pcbc models`), never by a build: a build reads only what the
lock pins. `pcbc models` writes `components/<Mfr>/<MPN>/spice/` and creates that directory for a part that
has none (buck's Samsung and UniOhm passives have no part directory today); it writes no `part.py`.

1. **Identity first.** The part's LCSC page title must name the declared MPN and manufacturer. If not, the
   part gets no model: `no model: LCSC <C-number> is <maker> <MPN>`. A mismatch is a `models:` report row, not
   a stage failure (Q4). Survey 4 found 4 of 37 distinct (MPN, LCSC) pairs disagreeing and 2 manufacturer
   mismatches (§6.2).
2. **Manufacturer sources only**, plus KiCad's official libraries. One fetcher per manufacturer, on that
   manufacturer's own URLs; a manufacturer with none gives `no model: no fetcher for <mfr>`. A fetcher that
   cannot fetch records `no model: <url> answered <status>`. Working in survey 4:
   - TI: `ti.com/lit/zip/<id>`, recording the revisioned redirect target (`lit/mo/<id><rev>/…`).
   - ST: `st.com/resource/en/spice_model/<mpn>_pspice.zip`. ST refused a plain HTTP fetch; survey 4 got the
     file through a browser. The fetcher records what it gets; survey 4's browser download is not what
     `pcbc models` would record.
   - Samsung: two HTTPS POSTs (`/mlcc/checkDownloadYn.do`, `/mlcc/download.do`) for the **Static** model at
     0 V. The bias is a fetch parameter, recorded as such, not an operating point: no DC bias is declared
     (`volts=` is a clearance voltage, §2).
   - Yageo: the S1P zip. It is measured impedance, not SPICE: recorded `not SPICE`, not converted (a
     conversion would be new physics pcbc writes).
   - Diodes: `spice/download/<n>/<MPN>.spice.txt`.
   - KiCad 10's official libraries hold no model for any part on the five boards: `symbols/Simulation_SPICE.sp`
     (57 lines) has only `kicad_builtin_*` generics (an opamp with `GAIN=20k VOFF=10m`), referenced by
     `Sim.Library` from 2 symbols in `Amplifier_Operational` and 5 in `Device`; no symbol carrying a `Sim.`
     field names TPS54202, USBLC6, AP2112K, LMV321 or AO3400 (grep of the install, this revision). Generic
     parameters are not a manufacturer's, so they are not used.
   `pcbc fetch` (LCSC/EasyEDA) is not a model source: the EasyEDA API returned `c_spiceCmd: null` and no
   `.subckt`/`.model` text for C20917, C191884, C1525, C2687116, C7972 and C51118.
3. **Screen by allowlist.** A model file is read as bytes. A byte 0x00–0x08, 0x0B, 0x0C or 0x0E–0x1F makes it
   `binary`, refused. It is decoded as Latin-1 (every byte maps; the allowlist is ASCII) and CRLF is
   normalised to LF. A non-ASCII byte is allowed only on a `*` comment line: all 72 non-ASCII lines in the 18
   Samsung files are comments (byte 0xA1, EUC-KR "±"; `rev/` scan). Accepted lines: comments; `+`
   continuations; element lines R C L D Q M J E F G H B V I X K S W T O U; `.subckt`, `.ends`, `.model`,
   `.param`, `.func`. A `.model` line with a quoted string or `file=` is refused (XSPICE `filesource` reads a
   file). Refused, each named in the row: `A` (XSPICE code-model instances), `.lib`/`.endl` (with or without a
   path), `.include`, `.control`/`.endc`, and every other dot-command, `shell`, `exec`, `system`, `source`,
   `load`, `write`, `wrdata`, `cd`, `codemodel`. The row says `refused: <what> on line <n> is not on the
   allowlist`, not `no model`. `$CDNENC…` blocks and `.PROT` mark the file `encrypted`. Nothing downloaded is
   executed. Survey 4's denylist screen was mutation-tested (`screen_mut/`); the allowlist is stricter.
4. **Load test.** Through the runner (§4, `-n`, closed environment, `-D ngbehavior=psa` when the file needs
   it), a minimal bench per part type: a two-terminal impedance sweep (`.ac dec 200 100 1G`) for capacitors and
   inductors, a DC sweep for diodes and ESD arrays, every port terminated separately (1 GΩ). A run that prints
   `singular matrix` or exits 1 fails, with ngspice's line. Only a file that passes is used.
5. **Pin.** `components/<Mfr>/<MPN>/spice/model.lock`: source URL, final URL, retrieval date, sha256 of the
   archive and of the extracted file (raw bytes), a stable sha256 (CRLF normalised, `*` comment lines
   removed: Samsung files carry a server-stamped `* Date : Oct-02-2026` line; same-day re-fetches were
   byte-identical, sha256 f6d16e1d…, a later day was not tested), format, encrypted flag, licence text, screen
   and load results. The model file sits beside it and is gitignored (Q5).
6. **Absent file.** On a fresh clone or CI the file is not there. The row is `report: model pinned
   (<sha>), file not present`, never a failure. A present file whose stable hash is not the lock's is a
   `models:` error naming the file and both shas; it fails `pcbc models --check`, not the build (no verdict
   uses a model).
7. **Use.** Report rows only (§6.3). Model-derived rows are written to `layout/<name>/spice/models.txt`,
   which is not part of the stamp and not compared between build paths: it depends on which machine has the
   files. The stamp records the locks only.
8. **IC ports.** A subcircuit's ports are matched to the symbol's pin names exactly, or, when every port is a
   number, to the footprint's pad numbers exactly. Nothing else is inferred. TI's TPS54202 model declares
   `TPS54202_TRANS GND PH VIN VSENSE EN BOOT`; the symbol's pins are `VIN GND SW FB EN BOOT`:
   `TPS54202DDCR: ports PH, VSENSE match no pin of U1 (VIN, GND, SW, FB, EN, BOOT)`.

Errors and report lines (the first two use survey 4's findings; the third is illustrative):

```
models: buck.py line 32: R_FB_BOT declares mpn 0402WGF1332TCE (13.3 kΩ); LCSC C25879 is 0402WGF2201TCE (2.2 kΩ)
models: components/TI/TPS54202DDCR/spice/TPS54202_TRANS.LIB: model exists, not runnable: about 50 $CDNENCSTART blocks (encrypted PSpice); ngspice-47: "Error: Missing token in line 76"
models: components/ST/USBLC6-2SC6/spice/usblc6-2sc6.lib: refused: ".control" on line 12 is not on the allowlist (comments, "+" continuations, element lines, .subckt, .ends, .model, .param, .func)
```

### 6.2 The five boards, per part

Survey 4, 2026-10-01, with this revision's identity rule applied. "Runs" means the file passed the screen
and a clean load test in ngspice-47.

| Board | Part (MPN, manufacturer) | Model | Status | Licence |
|---|---|---|---|---|
| buck | TPS54202DDCR, TI | ti.com/lit/zip/SLVMBJ5 → `lit/mo/slvmbj5c/slvmbj5c.zip` | **Exists, not runnable**: encrypted (about 50 `$CDNENCSTART` blocks, although TI's page calls it "Unencrypted"); ngspice-47 `Error: Missing token in line 76`; ports PH/VSENSE match no pin | TI copyright 2017, "as is" |
| buck | FXL0630-150-M, Changjiang | none found | no manufacturer model site found | – |
| buck | CL21A226MAQNNNE, Samsung | Samsung Static | **Runs** (0, 5, 12 V files) | no terms in file; site terms not read |
| buck | CL05B104KO5NNNC, Samsung | Samsung Static | **Runs** | as above |
| buck | 0402WGF1003TCE, 0402WGF1332TCE, UniOhm | not searched | no model; **R_FB_BOT's LCSC C25879 is 0402WGF2201TCE (2.2 kΩ)** | – |
| buck | B2B-XH-A, JST | – | not applicable | – |
| c3_usb | ESP32-C3-MINI-1-N4, Espressif | none: the download overview's 28 entries have no SPICE or IBIS | no model | – |
| c3_usb, node | USBLC6-2SC6, ST | st.com/resource/en/spice_model/usblc6-2sc6_pspice.zip (by browser) | **no model: LCSC C2687116 is UMW's**. ST's file itself runs with every port terminated separately (`rb/st.cir`: 1.678 pF per line at 2.5 V); survey 4's bench tied ports 3 and 4 and printed `singular matrix`, so its "runs" did not stand | "(c) 2014 ST… All rights reserved" |
| c3_usb, node | AP2112K-3.3TRG1, Diodes | not in Diodes' LDO collection (AP2127, ZMR500, ZXTR2105F only) | no model | – |
| c3_usb, node | TYPE-C-31-M-12 (HRO), B3U-1000P (Omron) | – | not applicable | – |
| c3_usb, node | CL10A106KO8NQNC, CL05B104KO5NNNC, CL05A105KA5NQNC, Samsung | Samsung Static | **Runs** | no terms in file |
| c3_usb | CL10A106KP8NNNC, Samsung | Samsung Static | **Runs** | no terms in file |
| c3_usb, node | 0402WGF*TCE, UniOhm | not searched | no model | – |
| c3_usb | KT-0603R, LCSC C2286 | not found (web search only) | no model | – |
| node | BME280, Bosch | none on the product page | no model | – |
| node | AO3400A, AOS | none on the product page | no model | – |
| node | LMV321IDBVR, TI | none; LMV321A (SLOM448) and LMV321-N (SNOM071) are different parts | no model | – |
| node | NCP15XH103F03RC, Murata | Murata has no NTC SPICE pages | no model; **LCSC C13564 is NCP18XH103F03RB (0603)** | – |
| node, blinky | KT-0603R, LCSC C72043 | – | no model; **LCSC C72043 is an Everlight 19-217/GHC-YR1S2/3T** | – |
| blinky | 0402WGF1001TCE, LCSC C21190 | – | no model; **LCSC C21190 is 0603WAF1001T5E** | – |
| ds2_addon | ADS122U04IPW, TI | ti.com/lit/zip/SBAM320 | **IBIS 4.1 only** (I/O buffers, no ADC behaviour); ngspice has no IBIS reader | – |
| ds2_addon | CC0805KRX7R9BB104, Yageo | yageogroup.com S1P (Keysight E4990A, 20 Hz–120 MHz, 0 V) | **not SPICE**; not converted (survey 4's hand conversion: 100.6 nF at 1 kHz, SRF 11.5 MHz, ESR 21.5 mΩ, not a tool number) | – |
| ds2_addon | CGA0805X7R105K250KT, declared TDK | none found (web search only) | **no model: LCSC lists the maker as HRE** | – |
| ds2_addon | CL10B104KB8NNNC, CL10A105KB8NNNC, Samsung | Samsung Static | **Runs** | no terms in file |
| ds2_addon | 0805W8F1001T5E, 0805W8F1002T5E, FRC0805F47R0TS | not searched | no model | – |
| ds2_addon | PZ254V headers, XFCN | – | not applicable | – |

For the record, Samsung's static models' capacitance at 1 kHz at several biases (`zsweep.py`, survey 4). The
stage prints none of these: no operating bias is declared.

| Part | Nominal | 0 V | 3.3 V | 5 V | 12 V |
|---|---|---|---|---|---|
| CL21A226MAQNNNE | 22 µF | 16.5 µF | – | 8.95 µF | 3.34 µF (SRF 3.51 MHz, ESR 6.3 mΩ) |
| CL10A106KO8NQNC | 10 µF | 6.63 µF | 3.67 µF | 2.47 µF | – |
| CL10A106KP8NNNC | 10 µF | 7.35 µF | 3.69 µF | 2.46 µF | – |
| CL05A105KA5NQNC | 1 µF | 838 nF | 641 nF | – | – |
| CL10A105KB8NNNC | 1 µF | 834 nF | 748 nF | – | – |
| CL05B104KO5NNNC | 100 nF | 87.2 nF | 84.3 nF | 81.5 nF | – |
| CL10B104KB8NNNC | 100 nF | 96.8 nF | 95.8 nF | – | – |

### 6.3 What the models add

- **B1–B3 gain nothing.** No part is in a DC current bench; a station's pad is where current enters or leaves
  the copper.
- **Report rows:** per part, the model used (source, licence, sha) and that it runs, or why there is none. A
  capacitor's operating capacitance is `not checked: operating DC bias not declared (NetReq volts= is a
  clearance voltage)`. Q3 puts this consequence to the owner.
- **The buck switching transient** (TPS54202 + extracted copper) does not run, for three separate reasons,
  each measured: the model is encrypted; two of its ports match no pin; and a switched transient with trace
  L/C sections is unusable (lumped: 0.62–0.77 s; 10 sections: 77–85 s, 438 MB of `wrdata`, about 1.3 GB of
  memory; 100 sections still running at 279 s, killed; `t3/gent.py`). Even running, it would check no declared
  number: `Path("SW", amps=2)` is the design current, not a prediction to compare against.
- **The USB pairs**: no model (identity), and a pair bench also needs a field solver and an edge rate (§7).
- **Hot-loop impedance** could use Samsung's ESR/ESL, but loop inductance is not extractable here (§3.2).

## 7. Requirements the stage cannot check yet

Each is printed as `spice: not checked: <declaration> line <n>: <reason>`, one line per declaration. The
reason is a fact ("no drop budget is declared"), never a request. The third column below is a design note
for this document; it is never printed (§7b).

| Requirement | Reason printed | What would make it checkable (not printed) |
|---|---|---|
| `NetReq(volts=)` as a voltage drop | no drop budget is declared; the language has no rail tolerance | A declared budget (a language change, the owner's call) |
| `volts` as clearance/creepage, `Isolation()` | geometry, checked by DRC and pcbc | – |
| A feedback divider's output voltage | needs the IC's reference voltage | An IC fact (model or datasheet) |
| The ground side of a feedback tap | needs the IC's reference pin and its GND current | IC facts |
| `loop_mm2`, `Loop(*pads, mm2=)`; shares inside a declared Loop (§5.2) | loop inductance needs a partial-inductance extractor; via L closed forms differ 2.4×; no target impedance | FastHenry (§11), a target |
| `kind="switch_node"` beyond its DC current | ripple needs the switching frequency; ringing needs the IC switch model and an edge rate | IC model that runs; `rise_ps` |
| `z_se_ohm`, `z_diff_ohm`, `Pair()` tolerance | ngspice computes no Z0 from a cross-section; pcbc's own formula would be circular (§5c). c3_usb's pair is 140.05 Ω against 90 ± 15 % by pcbc's formula already | A 2D field solver; `rise_ps`; IC pin capacitances |
| `rise_ps` where a board declares it | no coupling or reflection extractor exists; the value is read and not used | as above |
| `match_mm`, `uncoupled_mm`, `length_mm`, `max_mm`, `Bus()` | geometry, measured already | – |
| `vias`, `layers`, `reference`, return rules | topology; return-path continuity needs a 3D return-current extraction | – |
| `keep_clear_of`, `keep_clear_mm` | no aggressor edge rate, mutual L/C extraction or coupling limit is declared | `rise_ps`, an extractor, a limit |
| `pf_max` (i2c) | capacitance is a field-solver extraction; the bus mode is not declared. No example declares i2c | An extractor, a mode |
| Analog, sense, clock, spi presets | no noise figure is declared | – |
| `Thermal(pad, watts, rise_c)` | not electrical; package and ambient data are not declared | Thermal FEM (§11) |
| `Bridge(a, b)` | the netlist check already proves the single tie | – |
| Current in pour copper and pour necks | no rating for plane copper is declared or sourced; a neck is not separable from plane by a declared number | A sourced rating |
| A capacitor's operating capacitance | operating DC bias not declared | A declared bias |

## 8. Where it runs, the stamp and the report

**Stage.** `STAGES = ("check", "sch", "place", "route", "spice", "fab")` (`build.py:19` today ends at `fab`).
The stage needs the route stamp (`_stamp`, `build.py:773`) and reads the routed board it records. It does not
run in a build whose route stopped unrouted (`build.py:1427-1436`); `pcbc spice <routed board>` runs the
benches on any emitted board for diagnosis and tests, judging the nets whose links are complete, and its
output says `board not stamped`. On today's examples a build therefore never reaches the stage: buck (SW
unrouted), c3_usb (VBUS unrouted, pair skew 29.07 mm against 0.5 mm) and node (VBUS unrouted, skew 16.03 mm)
all stop in route (`s1/*.log`).

**Stamp: built on the tracer.** The stage runs inside `trace.tracing("spice", root)`, as every stamped stage
does, so its inputs are recorded by the tracer (every read, the subprocess argv, the env, and `code_sha`, the
hash of pcbc's own source), not by a hand list: a change to the extractor or a pass rule makes the stamp
stale. The tracer gains ngspice beside `kicad-cli` (`trace.py:169, 246`): its resolved path and `ngspice -v`.
The stage empties `layout/<name>/spice/decks/` before writing, so no earlier file is counted as an input.
`build.stale_reason` (`build.py:854`) gains a `spice` rung between routed and fab: a stale spice stamp
re-runs the spice stage only, and fab after it.

The stamp, `layout/<name>/spice/inputs.json`, is written **only when every row is `pass`, `report` or `not
checked`**, like `_stamp`. It holds the tracer's record, the routed board's sha (equal to the route stamp's),
the solver line seen, the sha256 of every deck and every `wrdata` output, each model's lock entry, and the
rows. A failing run writes `spice/report.txt` and its decks and no stamp.

Decks are kept under `layout/<name>/spice/decks/` so a failure is reproducible with one `ngspice -n -b` (§8's
"a failure becomes a fixture in one command"). `wrdata` outputs are hashed, and kept only for a failing bench:
they are regenerated from the deck byte for byte (§4). `write x.raw` is not used: its `Date:` line differs
between runs (`t3/`).

**Fab.** Refuses unless the spice stamp exists and its routed sha is the routed board's; a stamp exists only
for a run with no `fail`:

```
fab needs the spice stage's stamp for this routed board: layout/buck/spice/inputs.json records routed 3f9a…, layout/buck/routed/layout.kicad_pcb is 81c2…
```

**ngspice missing.** The stage needs ngspice for any board with a bench (any `amps=`; blinky has one,
`NetReq("VCC","GND", amps=0.05)`, line 54). A board with no bench writes its stamp with every row `not
checked` and does not run ngspice.

```
spice: ngspice is not on PATH; the spice stage did not run, and fab reads its stamp
```

**Instrument errors** (illustrative):

```
spice: 5V copper extracted from layout/buck/routed/layout.kicad_pcb is 2 islands; island 2 holds C_OUT2.1 and Seg("5V", (31.2, 14.0), (31.2, 15.5)) layout.gen.py line 90 and touches no other 5V copper
spice: ngspice-47 solved layout/buck/spice/decks/5V.path62.cir with SPARSE 1.3; the stage requires KLU ("Using KLU as Direct Linear Solver" is not in its output)
spice: ngspice-47 reported "singular matrix: check node n123" in layout/buck/spice/decks/GND.net55.cir; n123 is GND copper at (14.8669, 9.96) on B.Cu
spice: ngspice-47 exited 1 on layout/buck/spice/decks/GND.net55.cir: "Error: ..."
spice: B2 on GND solved 2.073 A across Via("GND", (14.8669, 9.96)) for 2 A between two pads; superposition on one island bounds it by 2 A, so the network is not what the stage built
```

**Report.** One row per declaration, in `board.py` order: `pass`, `fail` (the error text), `report` (the value
and why it has no verdict) or `not checked` (§7). The per-part model rows are in `spice/models.txt` (§6.1).

## 9. Cost

- Per deck from Python, traces and vias only: about 25 ms including write, run and parse (`t3/harness.py`: 20
  decks in 0.50 s serial, 0.15 s on 8 threads). ngspice startup 20–30 ms warm, 1.67 s on the very first cold
  run.
- KLU against the default solver on a resistor grid (`t3/gen.py`): 10,000 nodes 0.15 s vs 3.27 s; 40,000 nodes
  0.68 s vs 50.26 s. KLU is required for any pour.
- Pour mesh build, buck GND (`s2/gnd_sweep.txt`): 0.2 mm 22,457 nodes 6.81 s; 0.1 mm 90,884 nodes 27.84 s; 0.05
  mm 363,517 nodes 158.27 s. Almost all is pure-Python mesh building (no numpy, scipy or shapely).
- **B2 on a pour, measured**: buck GND at 0.1 mm, 90,884 nodes, 8 pads, 7 `op`s in one deck writing only the
  20 judged nodes (`rev/b2mesh.py`, `cd rev/run01 && env -i PATH=… HOME=$PWD /usr/bin/time -l ngspice -n -b
  ../b2g01.cir`): **7.96 s wall, 1.03 GB max RSS**, 7 outputs of 1,842 bytes; a second run 7.95 s, outputs
  byte-identical. Writing every node instead costs 24–95 MB per deck at 22k–90k nodes (`rb` R7).
- Synthetic grids (`rb/gen_b2.py`): 90k nodes × 12 pads 17.90 s, 1.97 GB (15.06 s, 1.65 GB with `destroy all`);
  270k nodes × 1 pad 8.17 s, 1.36 GB; × 3 pads 21.48 s, 1.94 GB.
- So pour decks run **one at a time** (1–2 GB each on a 16 GB machine); trace-only decks run in parallel.
- **Projected, not measured**, from the rows above: node's GND plane (50 pads in the B2 probe) at 0.1 mm,
  about 270k cells: 8 + 49 × 6.7 s ≈ 5.6 min of solve plus about 80 s of mesh build; with 3V3 and the 0.2 mm
  pass, on the order of 10 minutes per node build.
- Buck's emitted board is 40 segments, 14 vias, 4 pours, 1 filled polygon, 0 arcs (`rb`).

## 10. Testing

A `spice` pytest marker beside `kicad`, skipped unless `PCBC_REQUIRE_NGSPICE=1`. Every test that builds
through fab is marked `spice` as well as `kicad` once fab needs the stamp (PR 5): today 13 files (test_bridge,
test_build_blinky, test_guard, test_examples_fab, test_native*, test_fifth/seventh_review,
test_layout_roundtrip, test_planes, test_route; `rb` R1), and CI's jobs that run them install ngspice (PR 1).

**Fixture board.** No example produces a route stamp today, and blinky has no `Path`. PR 5 adds a small
fixture board under `tests/fixtures/` that the native router completes: a 2-layer board with a 2 A `Path`
through a via group, a feedback tap, an off-path stub and a poured GND, so the stage and fab's refusal are
tested on a real stamp, never a forged one.

**Mutations: each bench must fail, naming the line** (§8: corrupt one object in `layout.gen.py` and the build
must fail, naming it). Each runs on a stamped copy of the fixture, or on a survey build through `pcbc spice`:

| Bench | Mutation | Must say |
|---|---|---|
| B1 trace | One `Seg` of a 2 A path narrowed to 0.3 mm | that `Seg`'s line, 2.000 A, the width needed |
| B1 plane | The fill under one 0.781 mm, 2 A piece removed (a keepout) | that `Seg`'s line, the unplaned length, 1.09 mm |
| B1 group | A via group on a 2 A path reduced to one barrel | that `Via`'s line, the whole current, `via_amps` |
| B1 off-path | A `Seg` from a stub to the middle of the path trace | the stub piece, belongs to no path, its current |
| B1 zero current | A 0.12 mm off-path stub carrying none | no error (rule 1) |
| B1 neck | A path meeting a narrower pad through an accepted neck | the neck listed with its current, no error |
| Via in pad | Three vias in an intermediate station's SMD pad, the trace entering one side | the barrels' shares differ at pads M; both bounds printed |
| B2 cut edge | buck U1.1 and node U2.5 on the survey builds: the rows pin `report (not rated: pour)`. Reverse: a parallel conductor of the same rating added, and "the only copper" disappears | the measured rows in §5 B2 |
| B2 share | buck's VIN line of three 0.4 mm vias (0.755/0.490/0.755 A, pass) with two barrels removed | the remaining barrel, 2 A, 0.871 A |
| B2 spoke | A 2 A pad switched to thermal relief with two 0.2 mm spokes | each spoke's current, 0.2 mm, the width needed |
| B2 AC share | A second via added in parallel inside buck's declared Loop | `not checked: DC share`, naming Loop line 66 |
| B3 Kelvin | The tap's junction moved outside s1's pad, no loop (`rphys/b3_mut.cir`) | 1.360 mV, the junction point |
| B3 loop | A `Seg` joining the tap's middle to the path trace | a non-zero ΔV, the junction point |
| Two paths | Two amps-paths on one net in one B3 deck | one reference; the split equals the two B1 solves superposed |
| Extraction | A 5V `Seg` deleted so a cap is isolated | the island error, before ngspice runs |
| Connectivity | A via ring touching a 0.1 mm fill sliver | joined at both pitches |
| Runner | A fake `ngspice` that prints `Using SPARSE 1.3`; one that prints `singular matrix`; one that exits 1 | the three instrument errors |
| Runner env | A `.spiceinit` with `set numdgt=3` and one with `shell` in cwd and in `HOME`; `SPICE_SCRIPTS` set | outputs unchanged; no shell ran |
| Parser | The same network with node names permuted | identical currents |
| Screen | `.control`, `shell`, `.include /etc/passwd`, `.lib x.lib`, an `A` element, `file=` in a `.model`, a NUL byte, `$CDNENCSTART`; a Samsung file with 0xA1 in comments | refused ×7 with the line, encrypted ×1, accepted ×1 |
| Lock | One byte of a stored model changed; the file removed | the hash mismatch with both shas; `model pinned, file not present` |

**Numbers pinned**: 5V L1.2→J_OUT.1 6.3061 mΩ at pads E and the hand sum; the B2 rows of §5 (2.000 A, 1.000 A,
1.588 A); the VIN line 0.755/0.490/0.755 A; the 5V plane-gap error (0.197 mm, 1.09 mm); B3's 1.360 mV. Pinned
to 6 significant digits, with the ngspice version and solver recorded, until a CI run proves Linux/42 bit-equal
to macOS/47.

**Determinism**: two runs, decks and `wrdata` byte-identical; two build paths (decks name no absolute path, §4).

**An independent refuter** (§8): the mesh's GND resistances (§3) compared with one of the KiCad resistance
plugins found by search and **not run**: KiPIDA (2.5D resistive mesh, KiCad 9+), the "Fill Resistance" plugin
(kipy, KiCad 10.0.1+), Kicad-nikfemm (GPL-3). Until one agrees, every verdict that depends on the pour mesh's
resistance is `report: rests on pcbc's pour mesh`; cut-edge verdicts and unpoured nets do not depend on it.
Agreement is the gate for making mesh-dependent rows fatal.

## 11. Alternatives considered

- **SPICE for design** (synthesising widths). Rejected by the owner: the formulas give a width directly, with a
  source (§5c).
- **A graph-only stage** (cut edges, no ngspice). It would find every cut-edge row above and none of the
  shares; the shares are where pcbc's design assumed a split. Kept as the stage's first pass (the island and
  cut-edge computation), with ngspice for the rest.
- **kicad-cli `sch export netlist --format spice`.** Writes R/L/C values correctly (`L1 /SW 5V 15u`, `R_FB_BOT
  /FB GND 13.3k`), but ICs and connectors become stubs ngspice refuses ("Unable to find definition of
  model"), and it contains no copper. At most a cross-check, which the existing netlist gate covers.
- **libngspice through ctypes.** 3 ms init, the 10k-grid OP in 0.063 s in-process, same value as the
  subprocess. Rejected: global state, one circuit per process, a crash takes down the build, a different
  library path per OS. PySpice wraps the same and is not installed.
- **The kicad-happy spice skill** (prior art). Its parasitic extractor sums series resistance per net with no
  topology, so it cannot split current; its via inductance ignores the return; it fills gaps with numbers §5c
  forbids (ρ 1.68e-8, 25 µm plating against JLC's 18 µm, εr 4.5, ESR 5 or 10 mΩ, inductor Q 100, 5/10/20 %
  tolerances, `spice_templates.py:1219-1230`); it reads results through 6-digit `echo`. Its benches (RC, LC,
  divider, opamp gain) are schematic-level, not finished copper.
- **FastHenry2** (MIT-origin, text input, deterministic): the reference for loop and via inductance where closed
  forms disagree, and for AC shares (§5.2). Not installed; would be built from source.
- **FasterCap** (LGPL-2.1): pad and pour capacitance cross-check. Not installed.
- **openEMS** (full-wave): only for pair impedance. Not installed, not evaluated.
- **Thermal FEM** (for `Thermal()` and pour temperature): not evaluated; it needs package and ambient data
  nobody declares, which §5c rules out inventing.
- **A triangulated (P1 FEM) pour mesh**: same matrix with exact edges; would remove the neck wobble (±1.4 %).
  Deferred until the square mesh's neck error matters to a verdict.
- **Long switching transients with distributed trace L/C**: measured unusable (§6.3).

## Key decisions

| # | Decision | Where |
|---|---|---|
| K1 | DC current only: B1 path current, B2 every branch of a net's current, B3 a sense tap's junction (by voltage) | §5 |
| K2 | Pass rules use the curves that sized the copper: width ≥ `current_width_mm(I)` without `WIDTH_FLOOR_MM`, at the board's plane credit; I ≤ `via_amps` per barrel; spokes as traces | §5 |
| K3 | "Every branch can carry A" is every pad pair, computed by superposition, one `op` per pad; a result above A is an instrument error | §5 B2 |
| K4 | On an unpoured net B2 failures are fatal. A poured net's under-rated pin-to-pour joints stay §5b's `not rated: pour`, printed as `report` with the solved current (Q1). Measured: buck GND 6, node 3V3 14, node GND 44 such rows | §5 B2, Q1 |
| K5 | Extraction reads the emitted, KiCad-refilled `.kicad_pcb`; `layout.gen.py` only names lines | §2 |
| K6 | ngspice by subprocess with `-n`, a closed environment and the decks dir as cwd; one file per deck; KLU asserted; named `wrdata` of listed vectors at 15 digits; currents computed as ΔV/R | §4 |
| K7 | Pads at two bounds, equipotential (E) and meshed with centre injection (M); vias in pad copper tied to it; judged at the worse; neither is a proven bound for vias inside one pad | §3 |
| K8 | Pours meshed at 0.2 and 0.1 mm for resistance; connectivity from polygon geometry; judged at the larger current; pour cells reported, not judged | §3, §5 |
| K9 | A separate `spice` stage between route and fab, on `trace.tracing`, with a `stale_reason` rung; stamp written only on no `fail`; fab refuses without it | §8 |
| K10 | Model files in `components/<Mfr>/<MPN>/spice/`, gitignored, `model.lock` committed (Q5); an absent file is a report row | §6.1 |
| K11 | Models fetched by an explicit command, never a build; LCSC identity checked first, a mismatch gives no model and a report row (Q4) | §6.1 |
| K12 | No verdict depends on a part model; model rows live outside the stamp | §6.1, §6.3 |
| K13 | Words: `pass`, `fail`, `report`, `not checked`. Never `verified` | §1 |
| K14 | No transient analysis | §6.3 |
| K15 | The plane credit is read off the board per sub-run (fill on an adjacent layer under the whole cross-section), not taken from the design (Q2) | §3 |
| K16 | Accepted pad necks (`route_verify.pad_necks`) are judged by §5b's rule, not width; pieces carrying none are not judged by width | §5 |
| K17 | Tolerances are instrument limits: 1e-9 of the injected current, 1e-9 of the path drop | §5 |
| K18 | One reference node per deck; every other excitation a floating source | §5 |
| K19 | A DC share whose worst pair are pads of one declared `Loop` is `not checked`; cut edges stay judged | §5.2 |
| K20 | A verdict that depends on the pour mesh's resistance is `report` until an independent tool agrees with the mesh | §10 |
| K21 | Operating capacitor bias is not declared; `volts=` is clearance and is not read as a potential | §2, §6.3 |

## PR plan

Each PR is mergeable alone and leaves the suite green. Only PR 5 changes what a build does.

1. **The runner and CI** (`spice_run.py`). Writes a deck file, runs `ngspice -n -b` in its decks dir with the
   closed environment, asserts the KLU line, scans for `singular matrix` and error lines, parses named
   `wrdata`, records path, `ngspice -v`, env and cwd. The `spice` marker and `PCBC_REQUIRE_NGSPICE`. CI: the
   test job, `blinky-fab` and the kicad suite install ngspice, and a CI step runs a KLU deck and asserts the
   line on Ubuntu; if the distribution's ngspice lacks KLU, the job builds ngspice from ngspice's own source
   release. The PR does not merge until that step is green. Tests: the fake-ngspice, hostile-init,
   `SPICE_SCRIPTS` and permuted-name mutations; determinism. No build change.
2. **Trace and via extraction** from the emitted `.kicad_pcb`: segments, arcs, barrels by layer span, the
   per-layer union, pads at bounds E and M, via-in-pad ties, geometric connectivity, the island and cut-edge
   computation, the per-sub-run plane credit. `pcbc spice <board>` prints resistances and plane gaps (report
   only). Tests: 5V 6.3061 mΩ and the hand sum; the 5V and VIN plane gaps; one 4-layer net with per-span
   barrels, measured; byte-identical decks.
3. **The pour mesh.** Square mesh of `filled_polygon` at a given pitch, pad and via cells merged, spokes
   identified. Tests: buck GND pair resistances at 0.2 and 0.1 mm (§3 table), the sliver mutation,
   determinism. The refuter comparison (§10) against one KiCad plugin lands here or as its own PR. Report only.
4. **B1, B2 and B3 as report-only output of `pcbc spice`.** The pass rules (with the unfloored keyword on
   `current_width_mm`), necks read from `route_verify.pad_necks`, the AC-share rule, tolerances. Tests: every
   bench mutation of §10 on survey builds and the fixture board; the §5 rows pinned. No build change.
5. **The stage.** `"spice"` in `STAGES`, `tracing("spice")`, the ngspice tracer entry, the `stale_reason` rung,
   the stamp written on no `fail`, fab's refusal, the fixture board, the 13 fab-building test files marked
   `spice`. The PR states each example's outcome in CI and changes `test.yml` deliberately where an example's
   expected result changes: blinky must reach fab (not yet measured; 0.05 A against 0.527 A vias); buck stops
   in route today (`s1/buck.log`), and once it routes, the VIN and 5V plane-gap rows fail it unless Q2 is
   decided otherwise, so its `test -f .../fab/bom.csv` step (`test.yml:63-68`) changes with this PR.
6. **Models** (`pcbc models`), after PR 1 (its load test runs through the runner): identity check, the
   fetchers, the allowlist screen, load tests, `model.lock`, `--check`, the cache directory and its
   `.gitignore` entry. Tests: the screen and lock mutations; the identity mismatches of §6.2 as fixtures.
7. **Model report rows** in `spice/models.txt`. Needs 5 and 6.

## Not established

- The fab's finished outer copper thickness (every drop scales as 1/t), and whether JLC's "drill" is the
  drilled or the finished hole (the barrel annulus, §3.1).
- Whether ngspice 42 on Linux reproduces macOS ngspice-47's numbers bit for bit, and whether Ubuntu noble's
  ngspice 42 has KLU (PR 1 establishes it). Whether the KiCad 10 PPA's `ngspice-kicad` provides an `ngspice`
  binary.
- That `-D ngbehavior=psa` changes netlist parsing as a `.spiceinit` does (the variable is set; its effect
  on a PSpice model is not tested).
- The meshed-pad bound (M) on any board; the per-span barrel split on any 4-layer net; whether a
  thermal-relief pad connects in the mesh.
- The mesh against any independent tool: only the tracks-only 5V net was checked by hand.
- KLU time on node-sized pours (about 270k nodes per plane, projected in §9).
- Every share row in §5 B2 used an ideal pour (one node); the meshed values are not measured. The cut-edge rows
  do not depend on it.
- c3_usb's B2 rows (the probe's output exceeded the superposition bound on an incomplete board) and blinky's
  (no survey build).
- SW, both VBUS nets and both USB pairs on today's boards: not routed, so nothing was measured on them.
- TI's and Samsung's download terms; whether Espressif or AOS provide models on request; KEMET K-SIM export;
  KiCad's IBIS-to-SPICE path from the command line; whether Samsung's model body is identical across days.
- Whether a declared drop budget, rail tolerance, operating bias or edge rate will be added to the language;
  until one is, those rows stay `report` or `not checked`.

## Revision Summary

Version 2, 2026-10-01, after three reviews (`review-build.md` R1–R20, `review-owner.md` O1–O16,
`review-physics.md` P1–P12). New probes are in `scratchpad/spice/rev/`.

- **Counts corrected** (R2, O4, P1): B2's own rule over every pad finds buck GND 6 vias, node 3V3 14, node GND
  44 (new run, `rev/runb2.sh`), plus the R_FB_BOT.2 escape Seg; c3_usb not usable, blinky not run. The Summary
  no longer says "two".
- **K4 put to the owner** (O3, O5): poured nets' pin-to-pour rows stay §5b's `not rated: pour`, printed as
  `report` with the solved current (Q1); mesh-dependent verdicts are `report` until the refuter agrees (K20).
- **B2 wording** (P2): "crosses", the requirement, not "carries".
- **Plane credit from the board** (P5, K15): new measurement (`rev/uncov.py`) gives four fatal buck pieces
  (5V at J_OUT.1, VIN at J_IN.1 and two VIN transitions); Q2 asks whether a short stretch at a pad is allowed.
- **Pass rule** (R3, O1, K16): pieces carrying none are not width-judged; the curve is judged without
  `WIDTH_FLOOR_MM`; accepted pad necks (`route_verify.pad_necks`, live tree) are judged by §5b.
- **B1 on pours** (O7): pour cells are not "branches"; mesh-dependent shares are `report`.
- **B3 redesigned** (P3, P11, P4): the far-station voltage, not the tap current; measured mutation 1.360 mV;
  the report row regenerated from the specified bench (14.0211 mV) and no longer called the regulation
  point, the ground side `not checked`.
- **One reference per deck** (R9, P10, K18).
- **Pads** (P6, K7): equipotential and meshed bounds; via-in-pad ties; the claim that the stage checks equal
  sharing inside a pad withdrawn.
- **Thermal spokes judged as traces; pour necks `not checked`** (P7). **Connectivity from geometry, copper as a
  union per layer** (P12a, b); the 4-layer barrel split marked not measured (P12c).
- **AC shares** (P8, K19): the skin-depth sentence replaced by the R/L crossover (185–451 kHz); shares inside a
  declared `Loop` are `not checked`.
- **Annulus** (P9): recorded, resistance and rating areas differ by design (K2); no verdict moves.
- **Runner** (R4, R5, R16, R17, K6): `-n`, closed environment, fixed cwd, `-D ngbehavior=psa` (measured to set
  the variable), named `wrdata` of listed vectors (a 5,000-vector line measured), generated names, relative
  paths, inlined models.
- **Stamp** (R6, R20, K9): built on `tracing`, `code_sha`, ngspice in the tracer, a `stale_reason` rung, decks
  dir emptied, stamp only on no `fail`.
- **Cost** (R7, R18): B2 on buck GND at 0.1 mm measured, 7.96 s, 1.03 GB, outputs byte-identical over two runs;
  only judged nodes written; pour decks serial; the one-factorisation claim withdrawn.
- **CI and tests** (R1, R10): ngspice in CI in PR 1 with the KLU proof; fab-building tests marked `spice`; a
  routing fixture board; the stage moved to PR 5 after the report-only benches, with CI's buck step changed
  deliberately there.
- **Tolerances** (R19, O12, K17): one definition, 1e-9 relative, printed as an instrument limit.
- **Errors print both bounds** (O13). **§7** reasons are facts, the third column is never printed; declared
  `rise_ps` is read and `not checked` (O14). **TCR removed** (O11). **Cite fixed** to `stackup.py:832` (R11).
- **Models**: bias not read from `volts=` (O2, K21); USBLC6 and CGA0805 get no model by the identity rule and
  USBLC6's evidence corrected (O8, R12); identity mismatches are report rows (O10, Q4); absent files are report
  rows outside the stamp (R8, O9); decoding and CRLF specified (R13); the allowlist extended to S W T O U and
  refusals named (R14); KiCad's libraries checked, none apply; ST's browser fetch and Yageo's S1P recorded as
  what the tool would record (O16); "optimistically use" put to the owner (O15, Q3); PR 6 after PR 1 (R15).
- **Independence** (O6): the Summary and §1 say plainly that ngspice contributes the network solve only.
