# Constraints: every number, its formula, its reference

What `pcbc check board.py --constraints` prints and where each number comes from. The design
is `docs/r1-design.md` (router-plan R1, landed 2026-09-20); this page is the reference card:
the formulas as implemented in `src/pcbc/stackup.py`, the calibration against JLC's published
rows, the preset table, and the seen/now table of what R1 changed. Conventions: mm, ohms,
amps, volts, degrees C; every solver is bisection, 60 iterations, from the fab's `track_min`
to 6.0 mm, rounded to 4 dp, so the same board gives the same numbers on every machine.

## The pipeline

```
board.py ── language.py ──► Design (NetReq, Pair, Bus, Chain, Isolation, Guard)
                                │
                        constraints.compile_constraints(design) ──► ConstraintSet
                                │        one Constraint per net; every number a Derived(value, unit, Source)
              ┌─────────────────┼──────────────────────┐
        route_checks.py    dru.rules(cs)          compile.py projections
        moves before       .kicad_dru rules,      CompiledClass / CompiledNet / krt plan
        any copper (F)     project classes (E)    (unchanged interface; same numbers)
                                │
                          cs.lines: the report, one number per line, sorted by net
```

A `Derived` is a number plus where it came from: `Source(formula, ref, note)`. `formula` is
one of `hj_microstrip`, `hj_coupled_microstrip`, `wadell_stripline`, `wadell_stripline_asym`,
`ghione_naldi_cpwg`, `ipc2221_ext`, `ipc2221_int`, `ipc2152_fit`, `ipc2221_6_1`,
`iec60664_f5`, `iec60664_f2`, `i2c_capacitance`, `via_barrel`, or `preset` / `NetReq` /
`Pair` / `Bus` / `stackup` / `pcbc_floor` / `class_floor` for a number nothing derived. The
line prints the value, the unit, the formula, what it was evaluated against, and the note:

```
USB_DP: pair with USB_DN, 0.2288 mm wide, gap 0.15 mm on F.Cu over In1.Cu (GND): 90 ohm (hj_coupled_microstrip x 0.85 JLC04161H-7628; JLC row 0.2332/0.15; target 90 +-15 %; NetReq line 143)
VBUS: width 0.4 mm (pcbc_floor amps >= 0.2; ipc2221_ext 1 A 10 C 1 oz 0.300; ipc2152_fit x board 1.099 x plane 0.430 at 0.2104 mm In1.Cu 0.134)
VBUS: via 0.8/0.4 mm (preset power), 2 per layer change (via_barrel 0.4/0.018 mm 0.871 A at 10 C) [report only in R1]
```

`pcbc default` marks a number with no standard behind it. `[soft: warning in R1]` marks a rule
KiCad checks as a warning (R1 H.3). `[report only in R1]` marks a number nothing gates yet.

## Stackups (`stackup.py`, B.2)

Source: jlcpcb.com/impedance, read 2026-09-19. Outer copper 0.035 mm, inner 0.0152; solder mask
1.2 mil over substrate, 0.6 mil over trace, Dk 3.8 (carried as data, not modelled: see the
bias); via plating 0.018 mm (JLC capabilities: "average hole plating 18 um").

| pcbc name | JLC code | stack, top to bottom | sum |
|---|---|---|---|
| `jlcpcb_4l_1oz` | JLC04161H-7628 | F.Cu 0.035, 7628 0.2104 / 4.4, In1.Cu 0.0152, core 1.065 / 4.6, In2.Cu 0.0152, 7628 0.2104 / 4.4, B.Cu 0.035 | 1.586 |
| `jlcpcb_4l_1oz_3313` | JLC04161H-3313 | F.Cu 0.035, 3313 0.0994 / 4.1, In1 0.0152, core 1.265 / 4.6, In2 0.0152, 3313 0.0994 / 4.1, B.Cu 0.035 | 1.564 |
| `jlcpcb_4l_1oz_2116` | JLC04161H-2116 | F.Cu 0.035, 2116 0.1164 / 4.16, In1 0.0152, core 1.265 / 4.6, In2 0.0152, 2116 0.1164 / 4.16, B.Cu 0.035 | 1.598 |
| `jlcpcb_4l_1oz_1080` | JLC04161H-1080 | F.Cu 0.035, 1080 0.0764 / 3.91, In1 0.0152, core 1.265 / 4.6, In2 0.0152, 1080 0.0764 / 3.91, B.Cu 0.035 | 1.518 |
| `jlcpcb_2l_1oz` | none (JLC publishes no 2-layer impedance stackup) | F.Cu 0.035, core 1.53 / 4.6, B.Cu 0.035 | 1.6 |

`jlcpcb_4l_1oz` is 7628 because that is what JLC builds for a 1.6 mm 4-layer order that does
not pick an impedance stackup (r1-design H.2). `Board(stackup="JLC04161H-3313")` takes the
JLC code as an alias. The fab limits (track, clearance, via, ring, hole, edge) are unchanged
from before R1; JLC's own 1 oz / 2 oz numbers are in `JLC_LIMITS` as data for a later rung (nothing reads them yet).

Published rows (JLC's calculator as JITX records them, `jitxlib.jlcpcb`):

| code | 50 ohm SE outer | 90 ohm diff outer (w / gap) | 100 ohm diff outer |
|---|---|---|---|
| JLC04161H-7628 | 0.3244 | 0.2332 / 0.15 | 0.1722 / 0.15 |
| JLC04161H-1080 | 0.1176 | 0.09 / 0.09 | 0.09 / 0.137 |
| 3313, 2116, 2L | not published where pcbc can read it |

## Formulas

Each with its reference and the test vector that pins it (`tests/test_stackup.py::test_vector_n_*`,
tolerance in the assertion message).

### Microstrip, single (C.1): Hammerstad and Jensen 1980, static, with strip thickness

`microstrip(w, h, t, er) -> (z0, eeff)`, `eta0 = 376.730313668`:

```
u = w/h
Z01(u) = (eta0 / 2 pi) ln( F(u)/u + sqrt(1 + (2/u)^2) ),   F(u) = 6 + (2 pi - 6) exp(-(30.666/u)^0.7528)
a(u)   = 1 + ln((u^4 + (u/52)^2)/(u^4 + 0.432))/49 + ln(1 + (u/18.1)^3)/18.7
b(er)  = 0.564 ((er - 0.9)/(er + 3))^0.053
eeff(u, er) = (er + 1)/2 + (er - 1)/2 (1 + 10/u)^(-a(u) b(er))
thickness (t > 0):  th = t/h
  du1 = (th / pi) ln(1 + 4 e / (th coth^2(sqrt(6.517 u))))
  dur = (1/2) (1 + 1/cosh(sqrt(er - 1))) du1
  u1 = u + du1;  ur = u + dur
eeff_t = eeff(ur, er) (Z01(u1) / Z01(ur))^2
Z0     = Z01(ur) / sqrt(eeff_t)
```

Reference: E. Hammerstad, O. Jensen, "Accurate models for microstrip computer-aided design",
IEEE MTT-S Digest 1980, eqs. 1-8 and the thickness correction; KiCad 10
`common/transline_calculations/microstrip.cpp` (same equations). Valid 0.01 <= u <= 100,
1 <= er <= 128, 0.2 %. The `(Z01(u1)/Z01(ur))^2` factor is the one that is easy to drop:
the assembly is KiCad 10.0.6's `microstrip_Z0()` exactly (its `q_t` thickness term included,
and the `(Z01(u1)/Z01(ur))^2` factor in the *reported* permittivity, not in Z0), measured
against a harness compiled from KiCad's own source: 54.660 ohm at JLC's 7628 50 ohm width,
where bare H&J gives 53.858. R1 shipped with that factor in Z0 and no `q_t`, reading 55.165,
which was neither; corrected 2026-09-20. Vectors 1-5: alumina 49.289 ohm / eeff 6.579; 7628 54.660; 1080 55.754; the 2-layer
core 83.033 at 1 mm and 2.7966 mm for 50 ohm; 0.3244 mm for 50 ohm on 7628 with the bias.

### Coupled microstrip, edge-coupled (C.3): Hammerstad and Jensen 1980 even / odd, static

`coupled_microstrip(w, s, h, t, er) -> (ze, zo, eps_e, eps_o)`; `zdiff = 2 zo`. `z0, eeff` are
the thickness-corrected single line at width `w`; `u = w/h` is the bare ratio (not `ur`) and
`g = s/h`; thickness enters only through `z0` and `eeff`.

```
v      = u (20 + g^2)/(10 + g^2) + g exp(-g)
eps_e  = (er + 1)/2 + (er - 1)/2 (1 + 10/v)^(-a(v) b(er))
a_o = 0.7287 (eeff - (er + 1)/2)(1 - exp(-0.179 u));  b_o = 0.747 er/(0.15 + er)
c_o = b_o - (b_o - 0.207) exp(-0.414 u);              d_o = 0.593 + 0.694 exp(-0.562 u)
eps_o  = ((er + 1)/2 + a_o - eeff) exp(-c_o g^d_o) + eeff
Q1 = 0.8695 u^0.194;   Q2 = 1 + 0.7519 g + 0.189 g^2.31
Q3 = 0.1975 + (16.6 + (8.4/g)^6)^-0.387 + ln(g^10/(1 + (g/3.4)^10))/241
Q4 = 2 Q1/Q2 / (u^Q3 exp(-g) + (2 - exp(-g)) u^-Q3)
Ze = z0 sqrt(eeff/eps_e) / (1 - z0 sqrt(eeff) Q4/eta0)
Q5 = 1.794 + 1.14 ln(1 + 0.638/(g + 0.517 g^2.43))
Q6 = 0.2305 + ln(g^10/(1 + (g/5.8)^10))/281.3 + ln(1 + 0.598 g^1.154)/5.1
Q7 = (10 + 190 g^2)/(1 + 82.3 g^3);   Q8 = exp(-6.5 - 0.95 ln g - (g/0.15)^5)
Q9 = ln(Q7)(Q8 + 1/16.5);   Q10 = (Q2 Q4 - Q5 exp(ln(u) Q6 u^-Q9))/Q2
Zo = z0 sqrt(eeff/eps_o) / (1 - z0 sqrt(eeff) Q10/eta0)
```

Reference: Hammerstad and Jensen 1980, "Coupled microstrip" (Q1..Q10 are theirs; Kirschning
and Jansen 1984 is the dispersion model and is not used); KiCad 10 `coupled_microstrip.cpp`
(separate even / odd thickness widths, under 1 ohm from this single-`du` treatment in the
fitted range). Valid 0.1 <= u <= 10, 0.1 <= g <= 10. Feeding `ur` into the Q terms gives
103.67 ohm at vector 6 instead of 104.635 and 133.08 at the 2L clamp instead of 140.1, which is
what the vectors are there to catch. Vectors 6-10.

Gap policy: `gap = max(0.15, clearance_min)` (JLC's own 4-layer pair gap), `Pair(gap_mm=)`
overrides. Width is solved for `z_diff / z_bias_diff`. **Pair fit clamp**: a solved member
wider than `PAIR_FIT_MM = 0.25` cannot leave a USB-C's 0.3 mm pads at 0.5 mm pitch, so the
pair is written at `(track_min, clearance_min)`, `controlled=False`, the impedance at that
geometry is printed and the note says what to do (`use Board(stackup="jlcpcb_4l_1oz") for
high speed`).

### Stripline, inner layers (C.4): Wadell 4.2.1 (Cohn) with thickness

`stripline(w, b, t, er)`, `b` = plane to plane:

```
x = t/b;   m = 6 (1 - x)/(3 - x)
dw = (b - t) (x/(pi (1 - x))) (1 - 0.5 ln( (x/(2 - x))^2 + (0.0796 x/(w/b + 1.1 x))^m ))
w' = w + dw;   A = (8/pi)(b - t)/w'
Z0 = (30/sqrt(er)) ln( 1 + (4/pi)((b - t)/w') (A + sqrt(A^2 + 6.27)) )
```

Asymmetric (a signal on In1.Cu between F.Cu at `h1` and In2.Cu at `h2`):
`stripline_asym = 2 Z(2 h1 + t) Z(2 h2 + t) / (Z(2 h1 + t) + Z(2 h2 + t))`, `er` the
thickness-weighted mean, printed `+-5 %` (JLC publishes no inner-layer rows). Reference:
B. C. Wadell, *Transmission Line Design Handbook*, Artech 1991, 4.2.1 (Cohn 1954 with the
thickness correction) and 4.2.2. As `t -> 0` it reproduces the exact conformal map
`Z0 = (30 pi/sqrt(er)) K(k')/K(k), k = tanh(pi w/2b)` to 0.05 %. A mis-transcribed
`m = 6x/(3+2x)` gives 70.26 ohm at the first vector; the correct form 67.46 (KiCad 67.19).
IPC-2141's `60/sqrt(er) ln(1.9 b/(0.8 w + t))` is kept as `ipc2141_stripline` for the
cross-check column only. Vectors 11-13.

### Coplanar waveguide with ground (C.5): Ghione and Naldi 1987, t = 0

`cpwg(w, s, h, er) -> (z0, eeff)`:

```
k1 = w/(w + 2 s);   k3 = tanh(pi w/(4 h)) / tanh(pi (w + 2 s)/(4 h))
q1 = K(k1)/K(k1'),  q3 = K(k3)/K(k3')      (K by the arithmetic-geometric mean, k' = sqrt(1 - k^2))
eeff = 1 + q3 (er - 1)/(q1 + q3)
Z0   = (eta0/2) / ((q1 + q3) sqrt(eeff))
```

Reference: G. Ghione, C. Naldi, "Coplanar waveguides for MMIC applications", IEEE MTT-35(3)
1987, eqs. 6-7 (conductor-backed CPW); KiCad 10 `coplanar.cpp` (`backMetal`). Thickness is
not applied in R1 (KiCad's Gupta-Garg term is second order at 0.035 mm; the report says
`t = 0`). Used only where a pour flanks the track: a 2-layer `z_se_ohm` request (gap
`s = max(0.3, 2 x clearance)`) and `Guard`. Validity `s/h <= 1`; beyond it pcbc takes
`min(cpwg, microstrip)` and says so. Vector 14.

### Current (C.6): IPC-2152 with the IPC-2221 floor

`ipc2221_width_mm` is unchanged (k = 0.048 external, 0.024 internal;
`area_mil2 = (I/(k dT^0.44))^(1/0.725)`; 1 oz = 1.378 mil). `ipc2152_width_mm(amps,
temp_rise_c, copper_oz, board_mm, plane_h_mm | None)`:

```
area_mil2 = (I / (0.0897 dT^0.394))^(1 / (0.5038 dT^0.0385))        universal chart fit, Fig 5-1
board_mod = 24.93 (board_mm/0.0254)^-0.755                            Fig 5-8; 1.6 mm -> 1.092
plane_mod = 0.00313 (plane_h_mm/0.0254) + 0.4045, plane_h >= 0.144    Fig 5-11; 0.2104 -> 0.430, 1.53 -> 0.593; 1.0 with no plane
width_mm  = area_mil2 board_mod plane_mod / (1.378 copper_oz) x 0.0254
```

Reference: IPC-2152 Figures 5-1, 5-8, 5-11 as digitised by G. Hunter, mbedded.ninja "PCB
Track Current Capability (IPC-2152) Calculator" (coefficients quoted verbatim with the figure
numbers); valid 0.274-26 A, 1-100 C, 0.72-2.36 mm boards; below 0.274 A the fit extrapolates
and the line says so. Cross-check: the smps.us fit of the same chart,
`(117.555 dT^-0.913 + 1.15) I^(0.84 dT^-0.108 + 1.159)`, gives 513.1 mil2 at 10 A / 20 C
against 517.9 here and 500 read off the chart. `current_width_mm` = `max(ipc2221 external,
ipc2152)`, the winner as the source, the loser in the note; the plane term applies when a
declared `planes=` layer is adjacent to the routing layer, or on a 2-layer board because the
route plan always pours GND on B.Cu (`plane_h = 1.53`). Decision H.1: the floor stays; IPC-2152
only ever raises a width (a no-plane track above 1 A: 2 A -> 1.090 mm), so every example keeps
its width (buck 2 A over the pour: 2221 0.781 over 2152 0.646). `board_mm` is the literal
stack sum (1.5862 on 7628 -> board mod 1.099, not 1.092). Vectors 15-19.

### Vias per amp (C.7)

```
barrel_mil2 = pi drill_mm via_plating_mm / 0.00064516
via_amps    = 0.024 dT^0.44 barrel_mil2^0.725               IPC-2221 internal curve on the barrel
per_change  = max(1, ceil(amps / via_amps))
```

Reference: IPC-2221B eq. 6-2 internal, barrel-as-internal-trace convention (D. Brooks, J. Adam,
*PCB Design Guide to Via and Trace Currents and Temperatures*, Artech 2021, ch. 9; Saturn PCB
Toolkit uses the same). Plating 0.018 mm is JLC's figure; 0.025 would be 20 % optimistic. A
0.3 mm via carries 0.707 A at 10 C, a 0.2 mm via 0.527 A; node's 1 A LOAD on 0.2 mm drills
wants 2 per layer change. Printed in the constraint report only: nothing places the second via until the router does (R2), and the copper bar does not count them, not gated, until the router
can place two (R2). Vectors 20-21.

### Voltage (C.8): IPC-2221B Table 6-1 clearance, IEC 60664-1 creepage and impulse clearance

`ipc2221_clearance_mm(volts, row)`, rows as IPC-2221B Table 6-1 (2012) and KiCad's
`panel_electrical_spacing_ipc2221.cpp` encode them; above 500 V the 301-500 value plus the
per-volt increment:

| V (peak) | B1 internal | B2 external, uncoated, <= 3050 m | B4 external, permanent polymer coating |
|---|---|---|---|
| 0-15 | 0.05 | 0.1 | 0.05 |
| 16-30 | 0.05 | 0.1 | 0.05 |
| 31-50 | 0.1 | 0.6 | 0.13 |
| 51-100 | 0.1 | 0.6 | 0.13 |
| 101-150 | 0.2 | 0.6 | 0.4 |
| 151-170 | 0.2 | 1.25 | 0.4 |
| 171-250 | 0.2 | 1.25 | 0.4 |
| 251-300 | 0.2 | 1.25 | 0.4 |
| 301-500 | 0.25 | 2.5 | 0.8 |
| > 500, per V | +0.0025 | +0.005 | +0.00305 |

pcbc uses **B2** for any net that may run on an outer layer (JLC's LPI mask is not a qualified
permanent polymer coating under IPC-2221) and **B1** when `layers=` is inner-only; B4 is
carried and not selectable in R1. Under 30 V every row is below every class floor.

`iec_creepage_mm(volts_rms, material_group="IIIa", pollution_degree=2, reinforced=False)`:
IEC 60664-1:2020 Table F.5 (F.4 in 2007), PD2, general columns (not "printed wiring", decision
H.5: conservative by 2.5x at 250 V); IEC 62368-1:2018 Table 17 carries the same PD2 rows.
Looked up at the next voltage >= `volts`; reinforced doubles. FR-4 is group IIIa (175 <= CTI
< 400). Cross-check: TI SLUP419 (2024) Table 3 reproduces 63 V 0.63 / 0.9 / 1.25, 400 V 2.0 /
2.8 / 4.0, 800 V 4.0 / 5.6 / 8.0, 1000 V 5.0 / 7.1 / 10.0.

| V rms | I | II | IIIa / IIIb |
|---|---|---|---|
| 50 | 0.60 | 0.85 | 1.20 |
| 63 | 0.63 | 0.90 | 1.25 |
| 80 | 0.67 | 0.95 | 1.30 |
| 100 | 0.71 | 1.00 | 1.40 |
| 125 | 0.75 | 1.05 | 1.50 |
| 160 | 0.80 | 1.10 | 1.60 |
| 200 | 1.00 | 1.40 | 2.00 |
| 250 | 1.25 | 1.80 | 2.50 |
| 320 | 1.60 | 2.20 | 3.20 |
| 400 | 2.00 | 2.80 | 4.00 |
| 500 | 2.50 | 3.60 | 5.00 |
| 630 | 3.20 | 4.50 | 6.30 |
| 800 | 4.00 | 5.60 | 8.00 |
| 1000 | 5.00 | 7.10 | 10.0 |

`iec_clearance_mm(volts_rms, overvoltage_category=2, reinforced=False)`: rated impulse from
Table F.1, OV II (<= 50 V: 500 V; <= 100: 800; <= 150: 1500; <= 300: 2500; <= 600: 4000;
<= 1000: 6000), reinforced multiplies the impulse by 1.6 (62368-1 practice, as KiCad's
`iec60664.cpp`), then Table F.2 inhomogeneous field, PD2 (minimum 0.2 mm): 1.0 kV 0.2; 1.2
0.25; 1.5 0.5; 2.0 1.0; 2.5 1.5; 3.0 2.0; 4.0 3.0; 5.0 4.0; 6.0 5.5; 8.0 8.0; 10 11.

Policies: a `NetReq(volts=)` gets class clearance `max(kind, ipc2221 row)` and a creepage
rule only from `CREEPAGE_FROM_V = 60` (IEC 62368-1 ES1): 3.3, 5, 12 and 48 V nets get none;
48 V creepage is `Isolation(..., volts=48)`. An `Isolation(volts=V)` gets clearance
`max(ipc2221 B2, iec_clearance)` (250 V: max(1.25, 1.5) = 1.5 mm) and creepage
`iec_creepage` (250 V: 2.5 mm; reinforced 3.0 / 5.0). With `slot=True` the creepage path is
the contour through the slot: `gap + 2 x board_mm >= creepage` and `gap >= 1.0` (IEC 60664-1
groove rule at PD2, KiCad `GetMinGrooveWidth`); clearance still applies to the gap. Vectors
22-24.

### I2C length from capacitance (C.9)

`capacitance_pf_per_mm(z0, eeff) = sqrt(eeff) / (299.792458 z0) x 1000`;
`i2c_max_mm(pf_max, pins, c_pf_per_mm) = (pf_max - 10 pins) / c_pf_per_mm` at the class
width on the outer layer (bias applied to `z0`, `eeff / z_bias^2` as the consistent
velocity). Reference: I2C-bus specification UM10204 rev 7, section 7.1 (Cb <= 400 pF, 10 pF
per device pin). On 2L at 0.16 mm (142.9 ohm, 0.0404 pF/mm) a 2-device bus is bounded at 9.4 m:
it binds only on long buses on thin prepregs, which is the point of printing it. Vector 25.

## Calibration to JLC (C.2): the per-stackup fab bias

At JLC's own 50 ohm width the bare H&J formula gives 55.16 ohm (7628) and 56.11 ohm (1080);
at their 90 ohm pair geometries the bare coupled formula gives 105.1 and 111.7 ohm. The
residual is systematic: JLC's Polar-type solver models the solder mask fill between and over
the traces and the trapezoid etch; the closed forms do not. The KiCad solder-mask term
(`ApplySoldermaskCorrection`, Wan-Hoorfar filling factor) could not be verified from a source
this design could read and closes only a third of the gap on paper, so pcbc does not model
the mask: the stackup carries two **fab bias** factors, fitted to JLC's rows, printed on every
number they touch, replaced by one edit when a row is captured:

```
Z_pcbc = z_bias x Z_bare          (z_bias_se for single, z_bias_diff for pairs)
width synthesis: solve Z_bare(w) = Z_target / z_bias
```

| code | z_bias_se (fit) | z_bias_diff (fit) | residuals at JLC's rows | source string |
|---|---|---|---|---|
| 7628 | 0.9147 (= 50 / 54.660) | 0.8532 (geometric mean of 90/104.635 and 100/118.170) | 90 row -> 89.3 ohm (-0.8 %), 100 row -> 100.9 (+0.9 %) | `fitted to JLC04161H-7628 rows (JITX), 2026-09-19` |
| 1080 | 0.8968 (= 50 / 55.754) | 0.8308 (geometric mean of 90/111.331 and 100/117.114) | 90 row -> 92.4 (+2.7 %), 100 row -> 97.3 (-2.7 %) | same, 1080 |
| 3313, 2116 | 0.9058 | 0.842 | none: interpolated (mean of the two fits) | `interpolated between 7628 and 1080 fits; capture JLC rows to replace` |
| 2L | 1.0 | 1.0 | none; c/h = 0.02, the mask barely matters | `uncalibrated: formula only` |

Synthesised widths with the bias against JLC's rows:

| code | 50 ohm SE | 90 ohm pair (gap) | 100 ohm pair (gap) |
|---|---|---|---|
| 7628 | 0.3244 (row 0.3244, by construction) | 0.2288 at 0.15 (row 0.2332, -1.8 %) | 0.1759 at 0.15 (row 0.1722, +2.3 %) |
| 1080 | 0.1176 (row 0.1176) | 0.0958 at 0.09 (row 0.09, +6 %) | 0.0889 at 0.137 (the solve is 0.0846, under the fab floor the bisection starts from) |

Stated accuracy: **+-3 % impedance / +-3 % width on 7628, +-3 % / +-6 % on 1080**, inside
USB 2.0's +-15 % and 100 ohm Ethernet's +-10 %; off-row stackups and 2L print `uncalibrated`
or `formula only`. A real order asks JLC for impedance control anyway; the bias makes the
first spin land where their calculator would.

## Presets (D)

"floor" is `max(0.16, track_min)` width and `max(0.16, hole_floor)` clearance
(`hole_floor = hole_clearance - annular_min + 0.005`: 0.155 on 2L, 0.18 on 4L). Any accepted
kwarg replaces the preset's number and the source becomes `NetReq line n`; `Pair()` overrides
`z_diff_ohm, match_mm, uncoupled_mm, gap_mm, layers, reference` and prints `Pair line n
overrides preset usb_hs 0.5`; a kwarg the kind does not use is refused.

| kind | class | own kwargs (default) | width | clearance | via, allowed, budget | layers | airwire / length | group | spacing |
|---|---|---|---|---|---|---|---|---|---|
| `generic` (`digital`, `default`) | Default | `z_se_ohm` | floor; with `z_se_ohm`: H&J x bias on `layers[0]` (inner: Wadell; 2L: CPWG with the pour) | floor | stackup via, allowed | F, B | none | none | 3W |
| `power` | Power | `amps` (0.2), `temp_rise_c` (10), `volts`, `loop_mm2` (6.0 decap loop) | C.6, then `max(w, 0.4 if amps >= 0.2 else 0.25)` (a neck a pad can carry) | `max(0.20, B2 row)` | 0.8/0.4 (`max` with the stackup), allowed, `per_change` | all copper | none | none | 3W |
| `analog` | Analog | `keep_clear_of`, `keep_clear_mm` (3.0) | 0.20 | 0.20 | 0.6/0.3, **no vias** | F.Cu | airwire 25 | none | 5W |
| `switch_node` | SwitchNode | `loop_mm2` (20.0 hot loop), `amps` | 0.30, or C.6 with `amps` | 0.20 | 0.6/0.3, no vias | F.Cu | airwire 8 | none | 3W |
| `clock` | Clock | `match_mm` (2.0), `vias_max` (2) | 0.15 | 0.20 | 0.6/0.3, budget 2 | F, B | none | bus of the nets when > 1 | 5W |
| `usb_hs` | USB | `z_diff_ohm` (90), `match_mm` (0.5), `uncoupled_mm` (2.0), `vias_max` (2), `reference`, `length_mm` | pair by C.3 x bias at gap `max(0.15, clearance_min)`; 2L clamp | `min(0.16, w)` then the floor | stackup via, budget 2 | F, B; 4L `reference` = `plane_below(layers[0])` | `length_mm` only when given (H.6) | pair: skew 0.5, uncoupled 2.0 (TI usb_layout_basics) | 3W |
| `spi` | SPI | `clock` (required when > 1 net), `match_mm` (2.5), `length_mm`, `z_se_ohm` | floor | 0.20 | stackup via | F, B | `length_mm` | bus matched to `clock` | 3W |
| `i2c` | I2C | `pf_max` (400, UM10204) | floor | 0.20 | stackup via | F, B | `length (max i2c_max_mm)` | none | 3W |
| `sense` | Sense | `match_mm` (1.0), `keep_clear_of`, `keep_clear_mm` (3.0) | 0.20 | 0.20 | 0.6/0.3, no vias | F.Cu | airwire 25 | two nets: bus, skew 1 mm | 5W |
| `feedback` | Feedback | `keep_clear_of`, `keep_clear_mm` (3.0) | 0.20 | 0.20 | 0.6/0.3, no vias | F.Cu | airwire 15 | none | 5W |

Keep-away is explicit: `analog`, `sense` and `feedback` hold one only when `keep_clear_of=` is
written; with a `switch_node` on the board and none written the report prints `FB:
keep_clear_of none; NetReq("FB", kind="analog", keep_clear_of="SW") holds 3 mm`. Buck as
placed has R_FB_TOP.2 (FB) 1.62 mm from C_BOOT.2 (SW) and the IC's own FB and SW pins 1 mm
apart; a silent default would fail the stock example with a move that cannot be made (the boot
cap belongs at BOOT). The AI states the distance; the tool holds it.

## What the numbers feed (E, F)

KiCad rules (`dru.py`): class `track_width` (warning), keep-away `clearance` with the
footprint's own pads exempt, `creepage` from 60 V, `length (max)` from `length_mm=` and I2C,
`skew` per pair and bus (warning), `via_count (max 0)` on no-via nets, via budgets (warning),
`diff_pair_gap` (error) and `diff_pair_uncoupled` (warning), isolation `clearance` / `creepage` / rule
area, the footprint exemption, the canary last. KiCad applies the last matching rule of a
type, so the file is ordered general to specific, and `validate` refuses a malformed rule
before any write (one malformed rule silently disables the whole file). Placement checks
(`route_checks.py`): airwire length and skew, chain order and stubs, a channel for wide
tracks, decap and hot loops, keep-aways, the reference plane under a controlled net, the
isolation line; each a move naming refs, pins and numbers, ending in an edit to `board.py`.
The README's Constraints table maps each to its test.

## Seen / now

| Seen | Now |
|---|---|
| `NetReq(*nets, kind, **kwargs)` dropped an unknown kwarg silently (`amp=2` was a 0.2 A net) | exact signature; `NetReq("VBUS") line 144: unexpected keyword 'amp'; did you mean amps=?`; a kind-foreign kwarg lists what the kind takes |
| `microstrip_z0` was IPC-2141, valid 0.1 < w/h < 2, out of range at every practical width on the JLC prepregs (w/h 2.6 at 0.2 mm on 1080) | Hammerstad-Jensen with thickness, H&J coupled, Wadell stripline, Ghione-Naldi CPWG; IPC-2141 as the cross-check column only |
| `jlcpcb_4l_1oz` was `h_mm 0.12, er 4.5`, no JLC build | five JLC stackups by code; `jlcpcb_4l_1oz` = JLC04161H-7628; node's pair 0.1554 / 0.12 -> 0.2288 / 0.15 (JLC row 0.2332 / 0.15) |
| the closed forms miss JLC's solver by 5 to 12 % (mask fill, etch) | per-stackup fab bias fitted to JLC's rows, printed on every number, +-3 % on 7628 |
| c3_usb's report said 90 ohm while the 2L pair was floored to 0.127 / 0.127 | the clamp is a printed note with the real 140.1 ohm and the fix; `controlled=False` |
| a later `NetReq` overwrote an earlier class's numbers silently | identical numbers share the class; different numbers get `Power_2` and the report says so |
| power widths from IPC-2221 external alone | `max(IPC-2221 external, IPC-2152 with board and plane modifiers)`, the loser printed; every example's width unchanged |
| one via per layer change on any current | vias per change from the barrel's ampacity (JLC 18 um plating), printed and counted in the bar; not gated until R2 |
| `volts=` set nothing | class clearance from IPC-2221B 6-1 B2 / B1; creepage from IEC 60664-1 F.5 at 60 V and up; `Isolation` with clearance, creepage, rule area and slot |
| a 3 mm keep-away default would have failed the stock buck (FB 1.62 mm from the boot cap on SW) | keep-away is explicit and the report prints the hint |
| a `NetReq`'s numbers were visible only in the `.kicad_pro` after a build | `pcbc check board.py --constraints` prints every number with its source; `--json` the `ConstraintSet`; `pcbc pcb --constraints`; the build's `check` step |
| plan section 6.6 said everything pcbc writes is an error; KRT is not held to length, skew, uncoupled length, via budgets or a per-segment width floor, and fanout stubs are `track_min` wide by design | those five kinds are warnings counted by the bar and pinned per example, marked `[soft: warning in R1]`; a kind at zero on the four examples and the DS2 Addon is promoted in the same PR that shows the zeros |

Not in R1 (named so nobody waits): return vias, plane-split verification, 3W parallel-run
measurement, serpentines, the ESD-at-connector check, B4 coated rows, thermal via arrays,
neck-down areas, membership netclasses, 2 oz stackups.
