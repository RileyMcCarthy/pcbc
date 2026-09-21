# R2 measurements

What the copper looks like before each R2 slice and after it. Every number comes from a fresh
build of all five boards (`pcbc build <board> --force` into a temp copy, never the checked-in
`layout/` artifacts, which are a stale generation), read by `copper_bar.copper_bar`.

Boards: the four examples plus the MaD DS2 Addon (`Hardware/DS2Addon/pcbc/ds2_addon.py`), which
is the only board here drawn for a real order.

## What each pattern would own

Every routed segment classified by the pattern that would own it, as a share of routed
millimetres (2026-09-20, before any R2 slice). This is the measurement R2 rests on: two thirds of
the copper is structured, so the maze router's real job is the remaining third.

| pattern | segments | mm | share |
|---|---|---|---|
| power spine | 486 | 554.4 | 37.0 % |
| general signal | 247 | 464.3 | 31.0 % |
| differential pair | 220 | 214.2 | 14.3 % |
| plane tap | 211 | 178.9 | 12.0 % |
| short hop | 97 | 84.8 | 5.7 % |

Per board, by share of that board's copper:

| board | power | signal | pair | plane tap | hop |
|---|---|---|---|---|---|
| blinky | — | 100 % | — | — | — |
| buck | 86 % | 9 % | — | — | 4 % |
| c3_usb | 66 % | 2 % | 27 % | — | 5 % |
| node | 5 % | 26 % | 26 % | 36 % | 8 % |
| ds2 | 38 % | 57 % | — | — | 5 % |

## S1 — the fresh baseline, and the fanout stubs straightened

The escape via's across-coordinate was being snapped to KRT's routing grid while the pad's own
coordinate was not, so every escape stub left its pad up to half a grid step sideways: 14 tilted
stubs, and with them most of pcbc's own contribution to the bar's off-45 count. Pattern copper
does not need to sit on the router's grid — it is locked, so KRT reads it as an obstacle and
never has to land on it — so the via now keeps the pad's across-coordinate exactly.

| board | vias | off 0/45/90 | under 0.2 mm | worst detour | routed mm |
|---|---|---|---|---|---|
| blinky | 0 | 0 | 2 | LED 1.03x | 22.5 |
| buck | 4 | 3 | 26 | VIN 2.09x | 142.5 |
| c3_usb | 16 | 7 → **5** | 183 | USB_DN 1.81x | 315.7 |
| node | 30 → **29** | 63 → **62** | 165 | T_OUT 1.72x | 511.1 |
| ds2 | 32 | 23 → **13** | 113 | REFP_F 3.23x | 514.6 |

Escape stubs off 0/45/90, before and after: c3_usb 2 → **0**, node 2 → **0**, ds2 10 → **0**.
Length and micro-segment counts move by a few because KRT routes around obstacles that shifted by
a few hundredths of a millimetre; the ceilings in `tests/test_examples_fab.py` are re-recorded
from this build, and ds2's are recorded for the first time in `test_copper_bar.py`.

### S1b — a custom pad is the copper it draws, not the anchor it declares

`parse_foot` read a pad's `(size w h)`. For a `custom` pad that is the anchor KiCad snaps to, not
its copper: the USB-C's four shield pads on c3_usb and node declare `(size 0.005 0.005)` and draw
a `gr_poly` spanning 0.6 x 1.3 mm with a 0.1 mm stroke, so **0.7 x 1.4 mm**. Every check that
reads pads (placement, corridors, keep-away, the fanout lanes) had been blind to them.

Reading them truthfully brought them into the fanout's closed rows, and escaping them cost c3_usb
8 vias and 16.6 mm and pushed the USB pair's detour from 1.81 to 2.04. A part standing on a board
edge is now left out of the fanout: it is the board's entry, the parts it feeds are placed around
it, and its pads already face inward. An IC's supply pins keep their escapes; without those,
c3_usb and ds2 do not route at all.

| board | vias | off 0/45/90 | under 0.2 mm | worst detour | routed mm |
|---|---|---|---|---|---|
| blinky | 0 | 0 | 2 | LED 1.03x | 22.5 |
| buck | 4 | 3 | 26 | VIN 2.09x | 142.5 |
| c3_usb | 16 → **13** | 5 → 7 | 183 → **139** | 1.81 → 1.92 | 315.7 → **312.8** |
| node | 29 → **28** | 62 → 63 | 165 → 181 | T_OUT 1.72x | 511.1 → 512.8 |
| ds2 | 32 | 13 | 113 | REFP_F 3.23x | 514.6 |

Placement did not move: the pads were always inside their footprints' courtyards, so what changed
is only what the checks and the fanout can see. The two regressions (c3_usb's off-45 and its
pair's detour) are the honest cost of that, and the pair is KRT's until R2 gives it a pattern.

## S2 — the geometry core, and nothing on any board

`src/pcbc/route_geom.py` is one shape (`hull(pts) + disc(r)`), one distance (`hull_dist2`, squared
so the accept path has no square root) and one path form (0/45/90 by construction). Nothing
imports it yet, so **every board is byte-identical to S1b** and no copper-bar number moves; the
ceilings in `tests/test_examples_fab.py` and `test_copper_bar.py` are unchanged.

What was measured rather than assumed:

- **The USB-C shield pad's real outline.** `J1.A4B9` on c3_usb draws an eight-point concave
  `gr_poly` spanning **0.599974 x 1.299997 mm** with a `(width 0.1)` stroke — 0.7 x 1.4 mm of
  copper against the `(size 0.005 0.005)` the pad declares. Its hull is **six** points, not four:
  the export's own 25 nm wiggles are real hull vertices. Pinned in
  `test_a_custom_primitive_is_the_hull_of_its_points_plus_half_its_stroke`.
- **The random corpus the property tests run on.** 2000 seeded shape pairs, placed so the answer
  is a decision and not a foregone conclusion: **614 overlap, 293 sit within 0.2 mm of each other
  and 445 more between 0.2 and 0.5 mm**; 1132 of the 2000 clear their requirement. A corpus
  scattered at random over a board answers "obviously yes" nine times in ten, so the generator
  targets the gap and the test asserts the shape of the distribution it got.
- **The 16-point ceiling, and what reducing to it costs.** An outline with more hull vertices
  than `Shape` holds is reduced by deleting the edge that adds the least area and extending its
  neighbours to meet — never by dropping a vertex, which would make the shape a subset. Rounding
  that meeting point to the nearest nanometre puts the two vertices it replaces a hair *outside*
  the new edges, and the containment backstop then sent every reduction to the bounding box: a
  32-point ring came back as its 4.0 mm2 box instead of a 3.16 mm2 hexadecagon. The new vertex is
  now searched outward from the rounded point with exact integer predicates, at most 8 nm. Over
  ~800 000 calls on seeded rings and ellipses of 17 to 999 points, **46 % need no push, 53 % need
  one nanometre and 1.9 % need 2 to 8**; the 0.03 % that find nothing simply lose that edge to
  another one. A 200-gon of area 3.1411 reduces to **3.1897**, against 4.0 for the box.
- **The quantisation cost.** Rounding a coordinate to the nearest nanometre can leave a shape up
  to 0.5 nm inside its true copper when the input was not already on the grid. `EPS_MM` is 100 nm
  of one-sided margin on top — two hundred times the worst case — and everything read out of a
  board file is already on the grid, where `q` is the identity.

`tests/fixtures/geom_vectors.json` holds 2000 shape pairs with their exact squared distances,
gaps and verdicts (468 KB, one case per line). R3's Rust port replays it unchanged; regenerate it
with `PCBC_WRITE_GEOM_VECTORS=1 pytest tests/test_route_geom.py -k vectors`.

`free_intervals` (A.8) is named in A.1's module surface but takes a `Scene`, which S3 owns, so it
lands with `route_scene.py` rather than here.

## S3 — pads, the scene, the table, the emitter, and the agreement test

`src/pcbc/pads.py` (a pad's true copper), `src/pcbc/route_scene.py` (the placed board as
obstacles, the five rules, pad exits, free intervals, connectivity) and `src/pcbc/route_emit.py`
(pieces to KiCad text, and the geometry-keyed sidecar), plus `constraints.ClearanceTable` and
`stackup.mask_bridge_min`. `fanout.py` now builds `Piece`s, reads its footprints through the
scene and writes through the emitter, and its copper is **byte-identical on all five boards** —
that is this slice's acceptance and the proof the new core agrees with the code that already
works. No copper-bar number moves and no board changes.

### The agreement test (D.2), and why it is not vacuous

The checker runs over the four examples' checked-in routed boards and every clearance, hole, edge
or short error KiCad reports must be one it also rejects. Two of those boards carry real errors,
so the test has teeth:

| board | KiCad errors | the checker found | stricter than KiCad |
|---|---|---|---|
| blinky | 1 `shorting_items` + 1 `solder_mask_bridge` | both | 0 |
| buck | 0 | — | 0 |
| c3_usb | 4 `clearance` (0.1990, 0.1965, 0.1965, 0.1889 mm) | all four, **at the same millimetre** | 1 |
| node | 0 | — | 1 |

The two "stricter" rows are the one-sided epsilon doing its job: c3_usb has a 3V3 track exactly
0.2 mm from `SW_BOOT.1 [BOOT]` against a 0.2 mm requirement, and node has two vias exactly 0.5 mm
apart hole to hole against `hole_to_hole` 0.5. A gap that lands exactly on the requirement is
refused rather than handed to the arbiter to argue about.

blinky's short is worth naming: the checked-in routed artifact is a generation older than the
pinned bar (F.2), and its `LED` track crosses `D1.1 [GND]`. The gate never ran on it, and the
checker finds it without KiCad.

### A.1 against the arbiter: the probe ring

Four real board pads — c3_usb's `J1.A4B9` (concave custom `gr_poly`), `J1.1` (oval pad with an
oval drill), `U1.49` (chamfered roundrect) and the DS2 Addon's header pin 1 (`thru_hole rect` with
a round drill) — each with a ring of eight probe tracks and one clearance rule far wider than the
gap, so KiCad prints its own `actual` for every probe. **In all 32 probes KiCad's air is never
less than pcbc's** (a superset is legal, a subset is not), and **25 of 32 agree to the last digit
KiCad prints**. The seven that do not are exactly the two supersets A.1 declares — the hulled
concave shield pad (pcbc 0.1299 mm of overlap where the true concave copper is 0.5943 mm away) and
the ignored chamfer (0.2997 against 0.7201) — and the probes that land inside copper, where KiCad
reports a short and has no `actual` to compare.

### What A.4 said and what KiCad does

- **Rule 2 skips a same-net pair.** A.4's table says rule 2 (hole to copper) never skips on a
  shared net. KiCad's `hole_clearance` does skip it — a hole inside its own net's copper is the
  connection, and the barrel is plated to it — and reading A.4 literally refuses **21 pieces on
  buck, 65 on c3_usb and 161 on node**, every one of them a track landing on its own via and every
  one passed by KiCad, and it would make a plane tap (a via in the pad it welds) impossible to
  emit. That is not a superset, it is a modelling error, so rule 2 skips a same-net pair. Rule 3
  (hole to hole) still skips nothing: two holes are a drilling constraint whatever they connect to.
- **`between` starts at the fab floor, not at Default.** KiCad resolves a clearance from the two
  items' own net classes and gives an unassigned net the Default class. Starting from Default reads
  two USB nets (0.155) as 0.16 and refuses c3_usb's own routed pair at 0.159 mm, which KiCad passes.
- **KiCad skips `hole_to_hole` for slots.** Measured: a round NPTH 2 mm from a via under an 8 mm
  rule is reported (`actual 1.4000 mm`, edge to edge as A.4 rule 3 says, and pcbc measures the same
  1.4000); the identical pair with `(drill oval 0.6 1.6)` is reported by nothing at all. pcbc models
  the slot as a capsule, so rule 3 beside the four `thru_hole oval` J1 legs on c3_usb and node is
  **pcbc's own choice and not a DRC error it prevents** — a move that claims otherwise is lying.
- **KiCad's own tolerance is 0.0005 mm.** A 0.2 mm clearance rule last faults at a 0.1994989 mm gap
  and first passes at 0.1994995. So pcbc is 0.0006 mm stricter than KiCad, not the 0.0001 that
  `EPS_MM` alone suggests, and 1e-4 mm is exactly the 4-decimal resolution KiCad's reports print —
  not below it, as the constant's comment used to claim.

### The rest, measured

- **The pad census is complete**: all 397 pads of the five placed boards round-trip through
  `pad_geoms` — 363 smd, 30 thru_hole, 4 np_thru_hole; 165 rect, 156 roundrect, 36 circle, 32 oval,
  8 custom. `J1.A4B9` comes back 0.699974 x 1.399997 mm against the 0.005 it declares. An oval
  drill's capsule is **one nanometre longer** than the drill (the end-cap centres are half a
  nanometre off the grid and `q` rounds them outward), which is the safe side.
- **`free_intervals` never offers an offset `clears` refuses.** Swept against `clears` at 0.01 mm
  over node on three (net, layer) pairs. The only two disagreements the sweep found were the
  interval *endpoints*: `clears` carries `EPS_MM` on the strict side, so the blocked interval
  carries it too and the free interval is rounded **inward** rather than to nearest.
- **`_rot_cs` and libm.** Perturbing `cos` by +1 ULP and `sin` by -1 ULP moves **0 of 300 000**
  rotated rect pads on the 1 nm grid; the closest any raw coordinate came to a half-nanometre tie
  was 3.39e-07 nm against the ~1.1e-08 nm a 1 ULP change moves a 50 mm coordinate (about one corner
  in 50 million), and every `(at x y rot)` on all four example boards is 0, 90, 180 or 270, which
  `_rot_cs` returns exactly. The risk is recorded and accepted rather than chased.

### The review of S2, fixed in the same commit

Five bug-grade findings against `route_geom.py`, each with the test that would have caught it:

1. `Shape` accepted a **self-intersecting star-wound** point list, because it only checked that
   every consecutive triple turns the same way. A pentagram passes that and `_in_hull` is then
   wrong: a via 0.25 mm *inside* the copper was reported 0.427893 mm clear of it, a 0.678 mm error,
   accepted at every clearance the five boards compile. The invariant is now "these points are
   their own hull, in hull order".
2. A **doubled point** was a legal `Path` that `is_octilinear` waved through and `turn_ok` crashed
   on — and `octile_path(a, mid) + octile_path(mid, b)` produces one at the join. All three judges
   now agree it is not copper, and `octile_path` refuses a route from a point to itself.
3. `clears` summed the two radii **in call order**, so `clears(a, b)` and `clears(b, a)` disagreed
   on **286 of 3240** realistic (clearance, radius, radius) triples — two 0.5 mm vias at an exact
   0.239 mm separation, say. Nothing unsafe was accepted; determinism was, which is the contract.
4. `gap` had the same asymmetry and printed two different 4-dp numbers for the same pair on **422
   of 400 000** pairs (0.1592 one way, 0.1593 the other, and 0.1593 is the truth).
5. `gap` returned **`-0.0`** for **88 010 of 400 000** exactly touching pairs — a different byte
   string for the same geometry, in the module's one reporting function.

And the smaller ones: `turn_ok` accepted a 90-degree corner whenever a leg was a hair off-axis
(the two S1 tilted stubs, 90.6275 and 90.8952 degrees, were both signed off), `octile_corner` did
not quantise its own endpoints (50 000 of 50 000 off-grid pairs came back not-quite-45),
`clip_len_in_box` and `path_mm` returned `nan`/`inf` where `q` raises, `clears` inverted its
comparison for a requirement more negative than the shapes' own offsets, `MICRO_MM` was documented
as KiCad's rule and used by nothing (`legs_ok` is now the third judge; 2.15 % of random endpoint
pairs and 31.20 % of hops under 2 mm produce a leg under it, and KiCad faults exactly the same 11
of 72 measured segments), and `q` was documented as being `float(f"{v:.6f}")` when it is a fixed
point of that format and not the format itself — they differ by a nanometre on 38 % of midpoints
(21.732048 / 21.734519 go to 21.733284 here and 21.733283 there), so the house rule is written
down: a coordinate is written as `f"{q(v):.6f}"`, never as `f"{v:.6f}"` of a raw computed value.
`geom_vectors.json` gained the four exact ties where Rust's `f64::round` diverges from Python's
half-to-even, so an R3 port that reaches for `.round()` fails on replay.

### Deviations from the design, with reasons

- **`free_intervals`, `plane_targets` and `lane_strips` live in `route_scene.py`**, not where E.1
  lists them: the first two take a `Scene` / a `CompiledJob` (A.1's module is stdlib-only) and
  `route.py` is S4's file. `route_checks.py` imports `lane_strips` back, so there is one rectangle.
- **`Piece` lives in `route_emit.py`.** B.0 gives it to `patterns/__init__.py`, which does not exist
  until S4 and which is built *on top of* the emitter; the type belongs at the bottom of the stack
  and S4 re-exports it.
- **`route_checks._pad_box` is unchanged.** E.2 expected the custom-pad fix to land here and change
  messages on c3_usb, node and ds2 — S1b already landed it in `parse_foot`, so `_pad_box` is already
  reading 0.7 x 1.4 mm and re-routing it through `pad_geoms` would move messages for nothing.
- **`_seg_seg_dist`, `_pt_seg_dist`, `_segs_cross` and `_seg_rect_dist` stay in `route_checks.py`.**
  Moving them onto `hull_dist2` is a behaviour change to the placement report with no caller in this
  slice to justify it; it belongs in the slice whose patterns actually consume them.

## S4 — the hop, and the pattern stage

pcbc now writes copper. `src/pcbc/patterns/` is B.0's contract (`Piece`, `Refusal`, `PatternResult`,
`PatternCtx`, `PatternPlan`, the shared link builder) plus B.1's `hop`; `src/pcbc/route_verify.py` is
D.1's self-check; `route.py` runs the pre stage before KRT, hands KRT what is left
(`krt_plan(..., plan)`), asserts after every KRT step that nothing of pcbc's moved, and writes
`layout/<board>/routed/copper.json`. `copper_bar` gains `by_reason`, `vias_pattern` and
`vias_leftover`; `blocking.move_line` is the one sentence shape a pattern refusal and a KRT failure
both speak.

**Before and after, every board, fresh builds** (`pcbc build --force` into a temp copy; the "before"
column is the same source with `PCBC_PATTERNS=off`, which reproduces the S1b row **exactly** on all
five — so every move below is the hops and nothing else, and C.5's change to `write_fab_overrides`
moves nothing on its own):

| board | leftover share | segments | vias | off 0/45/90 | under 0.2 mm | routed mm | worst detour | refusals | route wall |
|---|---|---|---|---|---|---|---|---|---|
| blinky | 100 % → **0 %** | 6 → **5** | 0 → 0 | 0 → 0 | 2 → **0** | 22.5 → 22.8 | 1.03 → 1.04 | 0 | 5.7 → **5.4 s** |
| buck | 100 % → 100 % | 96 | 4 | 3 | 26 | 142.5 | VIN 2.09 | **2 hop** | 17.3 → **16.2 s** |
| c3_usb | 98.3 % → **95.2 %** | 342 → 377 | 13 → 14 | 7 → 7 | 139 → 168 | 312.8 → 312.9 | 1.92 → **1.81** | **1 hop** | 24.6 → **18.1 s** |
| node | 100 % → **97.0 %** | 437 → **411** | 28 → 28 | 63 → **57** | 181 → **154** | 512.8 → 513.1 | T_OUT 1.72 | **1 hop** | 34.1 → **32.8 s** |
| ds2 | 97.1 % → **92.7 %** | 337 → 340 | 32 → **28** | 13 → **11** | 113 → 118 | 514.6 → **509.9** | REFP_F 3.23 | **5 hop** | 17.9 → 18.8 s |

Zero hard refusals on every board; the gate is verified on all five (KiCad DRC clean, zero
unconnected, canary fired); `verify_copper` is empty on all five; two builds are byte-identical in
both the routed `.kicad_pcb` and `copper.json`; the pattern stage costs 9 to 80 ms.

What pcbc owns now, exact per board (`copper.json`'s census, and `test_examples_fab.py::OWNS`):

| board | fanout | hop | leftover |
|---|---|---|---|
| blinky | — | 5 seg / 22.8 mm | 0 |
| buck | — | — | 96 seg / 142.5 mm / 4 vias |
| c3_usb | 5 seg / 5 vias / 5.3 mm | 11 seg / 9.8 mm | 361 seg / 297.8 mm / 9 vias |
| node | — | 11 seg / 15.4 mm | 400 seg / 497.7 mm / 28 vias |
| ds2 | 9 seg / 9 vias / 13.5 mm | 10 seg / 23.7 mm | 321 seg / 472.7 mm / 19 vias |

The nets the hop claimed: blinky `LED`; c3_usb `CC1`, `CC2`, `LED`; node `CC1`, `CC2`, `LED`,
`LED_A`, `LOAD`; ds2 `A0`-`A3`, `DRDY`, `REFN`, `REFP`, `UART_RX`, `UART_TX`, `nRESET` — which is
S4's acceptance list verbatim. buck claims none.

What the KRT plan does with that (C.4): `local_hops` **disappears** from ds2's plan (all ten of its
local nets are hops) and never existed on blinky. It stays on buck (`EN` and `BOOT` refused), on
c3_usb (`LED_A`) and on node (`DRV`) — S4's acceptance expected it to go on buck and c3_usb, and it
does not, because those are exactly the three boards with a refusal. Every other step is untouched.

### Three of B.1's sentences do not survive contact with the boards

Each is refuted by a test rather than by an argument, because each rests on a number measured before
pads had exits.

- **blinky's `LED` is not a straight line, and the line B.1 quotes is a short.** B.1 says it is
  21.8675 mm of copper from `R1.2 (9.44, 12.5)` to `D1.2 (31.3075, 12.5)`. `D1.1 [GND]` sits **on**
  that line: the straight candidate overlaps it by **0.2988 mm** against the 0.2 mm the
  Default/Power pair needs. The number came from the checked-in routed artifact, whose `LED` track
  crosses `D1.1` — the short S3's own agreement test reported. The hop steps around it in five legs,
  **22.8182 mm**, and blinky reaches 0 % leftover the long way round
  (`test_the_straight_line_b1_wanted_across_blinky_shorts_d1_1`).
- **buck's `EN` is refused by its pads' exits, not by `U1.4`'s copper.** B.1 pins the refusal at
  `U1.4 pad [FB]` leaving 0.089 of 0.200 mm. That is A.7's measurement of the **centre-to-centre**
  line, which a pattern never draws. With exits, `U1.5`'s only one points down, away from `R_EN.2`,
  so no candidate has a shape at all and the refusal says which exits exist — the fact that can be
  acted on. Pinned verbatim in `test_bucks_en_is_refused_and_the_refusal_is_a_move`.
- **`L-h` and `L-v` never survive.** Both are a right angle, and A.5's turn rule is an octant
  difference of at most one, so B.0's seven shapes are five in practice. They stay in the
  enumeration because B.0 names them and because the *filter* is what removes them.

### Three decisions the measurement forced, each with its number

- **A pad exit carries `EPS_MM`** (`route_scene.pad_exits`). A.7 sizes the distance out as
  `half + between + width/2`, which for a neighbouring pad of the same size at the same offset puts
  the link at **exactly** the clearance — and `clears` is one-sided, so it refuses its own exit. It
  is not a corner case: it is every two-pad passive and every IC row. Measured: all sixteen of
  blinky's candidates came back `0.2 mm of the 0.2 mm` before this term. `free_intervals` already
  carries the same term for the same reason (S3).
- **A right angle where copper leaves a pad is mitred, not written** (`patterns.mitre`, `MITRE_MM`
  0.2). A.7's stubs are axis-aligned, so the join with a link is usually a quarter turn — which
  `dru.py`'s own `(constraint track_angle (min 135))` counts. Writing those corners raised
  `pcbc_geometry_angles` on blinky from **0 to 2**, i.e. the pattern raising the count of the warning
  pcbc wrote to catch it. The mitre cuts 0.2 mm back along each leg (a 0.2828 mm new leg, over
  `MICRO_MM`) and every board's angle count is what it was.
- **`DETOUR_MAX = 1.5` and the exits are ordered toward the partner.** A.7 orders a pad's exits
  *outward*, which is what a fanout escape needs and the opposite of what a link wants: taking that
  order literally sent c3_usb's `LED_A` round the outside at 1.93x its 1.336 mm airwire and moved
  c3_usb's worst detour from 1.92 to 2.02 for 1.36 mm of copper. Re-ordering the enumeration toward
  the other pad fixed node's `CC1` (1.27 → 1.00x), node's `LED_A` (1.00x) and six of ds2's ten hops,
  but not `LED_A` on c3_usb, whose two exits sit 0.08 mm from crossing. Every hop that fits on the
  five boards is **1.00 to 1.24x**; 1.5 sits above all of them and below that one, which now refuses.

### B.1's second clause, and the failure that narrowed it

"The `direct` candidate alone clears" is B.1's rule for a net past `HOP_MM`; its *reason* is that a
straight pad-to-pad line takes no corridor the airwire did not already need. Read as "any candidate,
when the straight line is blocked only by the two pads' own footprints", it let three ds2 signals
through — `GPIO0` at 17.34 mm, `ADC_TX` at 12.76 mm, `ADC_DRDY` at 16.80 mm — which locked **33 mm**
of copper across the board before anything else was routed, and `GND` then could not reach five of
its pads (`C5.2, C6.2, J1.2, J3.3, U1.4`). That is `route.py`'s own recorded failure mode, from the
other side.

The hole was that "no straight line exists at all" fell into the permissive branch. Past `HOP_MM` the
full list is now allowed only when a straight candidate **existed** and every blocker of it was one
of the two terminals' own pads — blinky and nothing else on the five boards. ds2 then hops exactly
its ten local two-pad nets, which is S4's acceptance list.

### The two numbers that got worse, and why they are recorded rather than fixed

**c3_usb: `micro` 139 → 168, `vias` 13 → 14, and the soft rules `width_power` 34 → 36 and
`vias_usb_dp` 0 → 1.** Every one of them is the **USB pair**, and the pair got *shorter*: locking
`CC1`, `CC2` and `LED` moved `USB_DN` from 46.25 to 43.65 mm and its detour from 1.92 to **1.81**,
and KRT's differential-pair router staircases more on the route it then takes (`USB_DN` 22 → 44
micro segments, `USB_DP` 15 → 23 and one more via). Per-net, nothing else on the board moved except
`CC1`, `LED` and `VBUS` by a segment or two.

F.3 item 7 says soft-rule hits must not rise, and two of them do. The pair is the 14.3 % of copper R2
explicitly does not touch and R4 owns, so the honest options were to record it or to stop hopping the
three nets nearest the connector — and the second would cost the detour improvement that is the point
of the slice. Recorded, and in S4's open issues.

**ds2: `micro` 113 → 118 and `segments` 337 → 340**, against `vias` 32 → 28, `off45` 13 → 11,
514.6 → **509.9 mm** and the soft `width_power` 5 → **3**. Per net: `VDDA` loses 19 micro segments
and `VSS` two, `ADC_DRDY` gains 25 and `GND` eight, all of it KRT's. The board is shorter, has four
fewer holes in it and is straighter, and it is the only board here drawn for a real order. ds2's
ceilings are pinned for the first time, in `test_patterns.py::DS2_BAR` — S1 said they would be and
they never were — and its `width_power` count moves in `test_dru.py`.

### What is not measured here

`bus`, `guard` and `stitch` read zero on every board because no example declares them (S8). The post
stage is **not wired in**: C.1 puts it between KRT's `planes` and `signals` steps and G lists it under
S5's owned files, so S4 leaves `POST` empty and `pattern_copper(stage=...)` taking the argument, and
S5 adds `tap` to the list and one call. A.4 rule 4 (the mask dam) is still advisory and still counts
zero, because the hop places no vias and a track's mask opening is its own copper.

## S5 — the tap, the post stage, and the plane checks

pcbc now welds the copper to the planes as well as drawing it. `src/pcbc/patterns/tap.py` is B.3;
`route.py` runs the post stage as a step of its own plan, between KRT's `planes` and `signals` steps
on four layers and immediately before `signals` on two, and KRT's `plane_taps` step then runs for the
plane nets pcbc refused a pad of and is skipped outright when there are none (C.4);
`route_verify.pour_raster`, `plane_islands` and `plane_checks` are D.5.

**Three decisions the project owner's stand-in took, which override `docs/r2-design.md` H.1 where
they differ.** They are implemented as taken, and each carries its arithmetic:

1. **A tap via is the smallest via whose current rating covers one pad's share of the net's current,
   capped at the net's class via** — not the class via by default, and not B.0's flat "a branch takes
   the fab's standard via". The arithmetic is R1's, already compiled: `Constraint.current.amps` is
   what the net carries and `via_amps` is the curve `Constraint.via.amps` and `per_change` come from.
   On every board here the answer is the fab's standard via, with room to spare — node's `GND` asks
   1 A across 47 SMD pads, so 0.0213 A a pad against the 0.527 A one 0.2 mm barrel carries at 10 C.
   The cap is what keeps node routable: at the Power class's 0.8 mm ring two taps need 1.0 mm between
   centres against 0.7 mm, and node's pads are not 1.0 mm apart. The middle case — the class via
   chosen because it *covers* where the standard one does not — is unreachable on all five boards,
   and that is arithmetic and not luck. Each board's window is its **own** stackup's, and the
   sentence used to mix two boards' references into one (finding 18): on node the window is
   (0.527, 0.871] A — the 0.2 mm barrel against the Power class's 0.4 mm one — and node's `GND`
   carries 1 A, which is 1.0 A over one pad (past the cap) and 0.5 A over two (below the floor), so
   no whole number of pads lands in it; on buck the stackup via is 0.3 mm and carries
   `via_amps(0.3, 0.018, 10)` = 0.707 A, so its window is (0.707, 0.871] and needs a pad count in
   [2.296, 2.828], which is not an integer either — 2 A over 2 pads is 1.0 and over 3 is 0.667. The
   rule is swept over every pad count from 1 to 119 on every plane net of all five boards, so a board
   that does reach it is not a surprise.
2. **The copper bar's via ceiling splits.** `vias_leftover` stays a ceiling; `vias_pattern` is exact
   per reason, like `SOFT` and the refusal counts. node goes from 28 vias to 76 and that is allowed:
   62 are taps, and holding node to 28 means holding forty-seven ground pads off the ground plane.
3. **Where a pattern makes a neighbour worse, the number is recorded.** Two do; both are below.

**The census, corrected.** B.3 says node carries 77 plane pads (GND 55 smd + 4 thru, 3V3 17 + 1).
Counted as **pads** rather than as `(pad ...)` blocks it is **69** — GND 47 smd + 4 thru, 3V3 17 smd
+ 1 thru — because KiCad writes `U1`'s QFN thermal pad as nine blocks all numbered 49 and each USB-C
shield pad as several `gr_poly` primitives. So every acceptance below is against **64 SMD pads**, not
72, and S5's ">= 66 of 72" is read as ">= 60 of 64"; 62 of them are tapped on the built board.

**Before and after, every board, fresh builds** (`pcbc build --force` into a temp copy). The "before"
column is S4 as landed. `PCBC_PATTERNS=off` on this same source reproduces the **S1b** row exactly on
all five boards — blinky 6/0/0/2/22.5/1.03, buck 96/4/3/26/142.5/2.09, c3_usb 342/13/7/139/312.8/1.92,
node 437/28/63/181/512.8/1.72, ds2 337/32/13/113/514.6/3.23 — so the rollback is still a rollback and
every move below is the taps and nothing else:

| board | leftover share | segments | vias\_leftover | vias\_pattern | off 0/45/90 | under 0.2 mm | routed mm | worst detour | refusals | route wall |
|---|---|---|---|---|---|---|---|---|---|---|
| blinky | 0 % → 0 % | 5 → 6 | 0 → 0 | — → `{tap: 1}` | 0 → 0 | 0 → 0 | 22.8 → 23.6 | 1.04 → 1.04 | 0 | 5.4 → **5.3 s** |
| buck | 100 % → **96.6 %** | 96 → 114 | 4 → **1** | — → `{tap: 6}` | 3 → **2** | 26 → 37 | 142.5 → **139.4** | VIN 2.09 | 2 hop | 16.2 → **9.5 s** |
| c3_usb | 95.2 % → **86.3 %** | 377 → 453 | 9 → **8** | `{fanout: 5}` → `{fanout: 5, tap: 34}` | 7 → 11 | 168 → 204 | 312.9 → 355.8 | USB\_DN 1.81 | 1 hop, **2 tap** | 18.1 → 20.2 s |
| node | 97.0 % → **85.8 %** | 411 → **335** | 28 → **14** | — → `{tap: 62}` | 57 → **31** | 154 → **67** | 513.1 → **400.8** | T\_OUT 1.72 | 1 hop, **2 tap** | 32.8 → 36.6 s |
| ds2 | 92.7 % → **92.3 %** | 340 → **310** | 19 → 19 | `{fanout: 9}` → `{fanout: 9, tap: 2}` | 11 → 11 | 118 → **91** | 509.9 → **507.4** | REFP\_F 3.23 | 5 hop | 18.8 → 20.5 s |

The gate is verified on all five (KiCad DRC clean, zero unconnected items, canary fired);
`verify_copper` is empty on all five; two builds of node and c3_usb into two directories give an
identical `copper.json` and a routed `.kicad_pcb` differing in exactly the five and one absolute
`(model ...)` paths, which is the caveat `docs/constraints.md` already records and the same result S4
measured; the two pattern stages together cost 26 ms on blinky, 46 on buck,
147 on ds2, 765 on node and 867 on c3_usb, against F.3's 2.0 s (the two-layer boards pay for the pour
raster).

What pcbc owns now, exact per board (`copper.json`'s census, and `test_examples_fab.py::OWNS`):

| board | fanout | hop | tap | leftover |
|---|---|---|---|---|
| blinky | — | 5 seg / 22.8 mm | 1 seg / 1 via / 0.8 mm | 0 |
| buck | — | — | 6 seg / 6 vias / 4.8 mm | 108 seg / 134.6 mm / 1 via |
| c3_usb | 5 seg / 5 vias / 5.3 mm | 11 seg / 9.8 mm | 34 seg / 34 vias / 33.7 mm | 403 seg / 307.0 mm / 8 vias |
| node | — | 11 seg / 15.4 mm | 62 seg / 62 vias / 41.5 mm | 262 seg / 343.9 mm / 14 vias |
| ds2 | 9 seg / 9 vias / 13.5 mm | 10 seg / 23.7 mm | 2 seg / 2 vias / 1.7 mm | 289 seg / 468.5 mm / 19 vias |

`vias_pattern` per reason, exact: blinky `{tap: 1}`; buck `{tap: 6}`; c3_usb `{fanout: 5, tap: 34}`;
node `{tap: 62}`; ds2 `{fanout: 9, tap: 2}`.

### What each board taps, and what it does not

| board | plane pads | tapped | through-hole, skipped | already welded, skipped | refused |
|---|---|---|---|---|---|
| blinky | 1 | 1 | 0 | 0 | 0 |
| buck | 8 | 6 | 2 | 0 | 0 |
| c3_usb | 42 | 34 | 4 | 2 | **2** |
| node | 69 | 62 | 5 | 0 | **2** |
| ds2 | 5 | 2 | 2 | 1 | 0 |

The refusal count by rule, across the five boards: **3 `copper`** (`C_EN.2` and `R_CC1.2` on c3_usb,
`C_VBUS.2` on node — a passive's pad boxed in by its own partner and a neighbouring track) and
**1 `edge`** (`U1.51` on node, 1.06 mm from the board edge with the USB pair on its other side).
Nothing was refused for `hole_to_hole`, `via_in_pad`, `lane` or `pour_reach` on any board. Every
refused pad is welded by KRT's `plane_taps` step instead: node's `06_plane_taps.kicad_pcb` adds
exactly two vias to the post stage's 62, and runs for `GND` alone because both refusals are GND's.
No other board has a `plane_taps` step at all — it is a four-layer step and the other four boards are
two-layer — so the "skipped when there are no refusals" path is exercised by a board that refuses
nothing only when a four-layer board does, and `test_krts_tap_step_runs_only_for_the_nets_the_pattern_
refused` is what pins the rule itself.

`docs/r2-design.md` F.3's node targets are met: `vias_leftover` 28 → 14 (≤ 15), off45 57 → 31
(≤ 32), micro 154 → 67 (≤ 90), 513.1 → 400.8 mm (≤ 420), detour 1.72 held, zero unconnected. The
count target reads ">= 66 of 72" against the corrected census as ">= 60 of 64", and 62 are tapped.

### The pour a two-layer board does not have yet

C.1 puts the post stage immediately before `signals` on both stackups, so on c3_usb and ds2 the taps
go down **before** `gnd_pour` has run — the pour is written near the end of the plan. D.5's raster is
what makes that safe: every foreign item on the pour's layer is dilated by what the pour owes it, the
free cells are labelled 4-connected, and the pour is the largest region. A cell counts as blocked when
an obstacle touches any part of it, so a coarser cell blocks more and the error refuses a tap that
would have worked rather than accepting one that would not.

The cell is **0.2 mm and not `scene.grid`**, which is the one place this slice departs from the
design's letter. At 0.05 mm c3_usb is 576 000 cells against a whole-stage budget of 2.0 s; at 0.2 mm
it is 36 000, the raster costs about a tenth of a second, and it is still four times finer than the
0.5 mm hole-to-hole that decides whether two taps can sit side by side at all. It refused nothing on
either board, which is the honest reading: at post time the signals are not down yet, so the raster
sees an emptier board than the pour will, and it can only catch the gross case. The check that
actually bites is the one taken after the fact — on all five boards every plane is still **one
island** after the taps and every tap via lands inside its own net's plane, measured on the gate's own
refilled copper (`plane_islands`, `plane_checks`), and there is not one via inside a pad anywhere
(`fab.via_in_pad`).

**Three of those four sentences were weaker than they read, and S5r below is what each of them says
now.** The island count cannot see a fragment the fill *deleted* (ds2 drops five, 8.52 mm2, and still
reports one island); `plane_checks` asked the via's **centre**, never its ring, and said nothing at
all about a net whose plane came back unfilled; and `via_in_pad` asked for full containment inside a
pad's declared `(size w h)`, so it missed three vias that break an IC pad's edge on the built boards
and measured a custom pad as its 5 um anchor. None of the five boards is wrong — the taps themselves
clear every same-net pad by `clearance_min` and every plane is genuinely one island — but the backstop
behind them was not the statement it looked like.

### One KRT patch, and why it is not the third workaround C.5 forbids

With the taps down, KRT routed c3_usb's `VBUS` **0.2365 mm from `J1`'s NPTH mounting hole** against
the 0.25 the board declares, four times, and the gate failed the board. The cause is not the taps:
KRT lowers its output project's `min_hole_clearance` to the floor it actually routed to (its own
`INRUN_FLOOR_SYNC`, 0.25 → 0.0889 on the first step) and every later step reads the lowered one, so
by the signals step the router is routing to rules `board.py` never declared. A.4 rule 2's own table
already records this class of error (`hole_clearance (actual 0.1845 mm)`, a via against `J1`'s NPTH);
the taps only moved `VBUS` into it.

The fix is one line in the post stage: **its step board's siblings are copied from the placed board**,
not from the step before it, so the next KRT step reads the project pcbc compiled. That is not a
patch on KRT's geometry and it raises no floor — it restores the rules the gate judges the board
against. With it, c3_usb is verified; without it, four errors. `fab_overrides.txt` has no
hole-clearance key, so there is nothing to write there instead.

### What got worse, board by board, and why it is recorded rather than fixed

**c3_usb: `micro` 168 → 204, `segments` 377 → 453, `routed mm` 312.9 → 355.8, `off45` 7 → 11, the
soft `width_power` 36 → 49, and the per-net detour on `VBUS` 1.33 → 1.70.** Thirty-four new locked
vias sit between the connector and the MCU, and KRT answers by staircasing the leftover `GND` and
`VBUS` around them. Per net, because the split is not what the sentence above it used to say: `VBUS`
takes 35 of the 36 new micro segments (`GND` +7, `3V3` -6) and goes 36.849 → 47.158 mm, `GND` takes
all four of the new off-45 segments (0 → 4), and `GND` 1.23 → 1.44 and `3V3` 1.10 → 1.38 move with
them. Against that: `vias_leftover` 9 → 8, the leftover share 95.2 % → 86.3 %, and the board's ground
is now welded pad by pad to the back pour instead of relying on whatever KRT could reach. F.3 item 7
says soft-rule hits must not rise, and `width_power` does; it is in the open issues.

`off45` 7 → 11 and `VBUS` 1.33 → 1.70 were **raised ceilings that this section did not name** until
the S5 review (findings 15 and 16). `VBUS` is the larger of the two and it is invisible in the table:
`worst_detour` is a `max`, and it held at `USB_DN` 1.81 only because a different net was already
worse. The bar now carries `detours` per net and `test_examples_fab.py` pins every one of them, so a
net creeping toward the worst net's number is a test failure rather than a silence — S7 owns `VBUS`
and is expected to take this back.

**buck: `micro` 26 → 37 and `segments` 96 → 114**, against `vias_leftover` 4 → **1**, off45 3 → 2,
142.5 → **139.4 mm** and `width_power` 40 → 39. Six taps on a board with eight ground pads, and the
same staircase answer at a much smaller scale.

**node: the soft `width_usb` 57 → 58.** One more necked segment on the USB pair, which R2 does not
touch and R4 owns. Its `width_power` falls 44 → **20** in the same build, because the taps carry GND
and 3V3 to their planes at the class width and KRT has far less power copper left to neck.

**Every board: `angles`, pcbc's own 135-degree corner rule, on three of them.** `pcbc_geometry_angles`
is the rule pcbc writes into every `.kicad_dru` (`(constraint track_angle (min 135))`) and it goes
buck 6 → 32, c3_usb 40 → 124, node 32 → 104 — the largest proportional soft-rule rise of the slice,
and it was in neither this section nor the open issues until the review found it (finding 5). It is
the taps, directly: matching each `track_angle` warning's position and KiCad's own reported track
length against `copper.json`, a tap stub is one of the two tracks in 16 of buck's 32, 74 of c3_usb's
124 and 92 of node's 104. A tap stub is axis-aligned by construction and KRT arrives at whatever
angle it likes, so every tap is a fresh corner for the leftover router to make sharp. No copper is
illegal — `kicad-cli pcb drc` reports the rule as a warning — and it is **accepted** here the way
`width_power` is, with the number now a pinned ceiling in `BAR` rather than a silence. Making the tap
prefer an exit whose junction with the leftover can be 135 degrees or wider is a copper change and
belongs behind its own measurement (S7's neighbourhood).

**Four tap stubs are inside the `width_power` counts above**, against the sentence in
`test_examples_fab.py` that said no tap stub was in them (finding 14): buck's `R_FB_BOT.2` at 0.64 mm
against `width_power`'s 0.781 mm, and node's `U4.2`, `U4.6` and `U4.7` at 0.364 mm against 0.4 mm.
`tap._width` narrows the class width to the pad's across dimension — `fanout.py`'s own neck, and the
right answer for a 0402 — and the width rule has no such exemption, so the stub is a soft hit like
any other necked track. The pattern now emits a `style:` line naming each one, the count is pinned as
`TAP_NECKED`, and the sentence says how many instead of denying them.

### What is not measured here

`bus`, `guard` and `stitch` still read zero on every board (S8). The decap loop-area note D.5 asks the
tap stage for is **not** written: `route_checks._loop_area` needs the IC-and-cap pairing its own
checker builds, and reproducing that inside a pattern for a `style:` line is work S5 did not do — the
loop is still reported by `check_decoupling` where it always was. A.4 rule 4 (the mask dam) is still
advisory and still counts zero: a tap's mask opening is its own ring, and no tap came within 0.10 mm
of a foreign one — and what keeps the barrel out of the pad's own opening is the tenting, which S5r
makes pcbc's fact rather than KiCad's default. The leftover share does **not** reach F.3 item 5's
70 % on any board, and it was never going to at S5: the census says a hop is 5.7 % of the copper and
a plane tap 12 %, so 82 % is the arithmetic. The 45 % target needs the spine (37 %), which is S7.

### The open issues S5 leaves, as they stand after the review

1. **`width_power` rises on c3_usb**, 36 → 49 at S5 and 49 → **51** after the review's EPS fix; F.3
   item 7 forbids a soft-rule rise. It is KRT's leftover around locked copper, and S7's spine owns
   that neighbourhood.
2. **`angles` — pcbc's own 135-degree rule — triples on buck, c3_usb and node** (finding 5). The
   number is recorded and pinned as a ceiling; the fix is a tap that can choose an exit the leftover
   can meet at 135 degrees or wider, and it is a copper change for a later slice.
3. **node's worst detour 1.72 → 1.82**, on `USB_DN`. The pair is KRT's until R4 and the `signals`
   step re-lays it; this is the price of the taps stepping out of line for finding 10, taken against
   `vias_leftover` 14 → 11, micro 67 → 62, off45 31 → 30 and a 3V3 plane that is no longer cut.
4. **c3_usb's `VBUS` detour is 1.70 against a 1.81 ceiling** (finding 16). S7 owns `VBUS` and should
   take it back; until then the per-net ceiling is what stops it drifting the last 0.11.
5. **ds2's pour drops five orphan islands, 8.52 mm2** (finding 9). Pre-existing — the patterns-off
   build drops three, 10.34 mm2 — and now a pinned area rather than an unchecked one.

## S5r — the review of S5, and what the eighteen findings moved

Four independent attacks ran against S5 as committed (716166b) and raised eighteen findings, five of
them bug-grade. Every one is answered below with what it cost. Two things are true of all of them:
**no shipped board was wrong** — the taps themselves clear every same-net pad, every plane is one
island, KiCad DRC is clean on all five — and **most of the backstops behind them were not the
statements they looked like**. A check that cannot fail is not a check, and five of these were.

**The bar, re-recorded from fresh builds, 2026-09-20.** "S5" is the row this document recorded when
the slice landed; "now" is the same board built from the fixed tree.

| board | segments | vias\_leftover | off 0/45/90 | under 0.2 mm | routed mm | worst detour | angles | plane mm2 |
|---|---|---|---|---|---|---|---|---|
| blinky | 6 → 6 | 0 → 0 | 0 → 0 | 0 → 0 | 23.6 → 23.6 | LED 1.04 | 0 → 0 | GND B.Cu 917.25 |
| buck | 114 → 114 | 1 → 1 | 2 → 2 | 37 → 37 | 139.4 → 139.4 | VIN 2.09 | 32 → 32 | GND B.Cu 935.59 |
| c3_usb | 453 → 453 | 8 → 8 | 11 → **13** | 204 → **203** | 355.8 → **356.7** | USB\_DN 1.81 | 124 → **122** | GND B.Cu 1050.50 |
| node | 335 → **318** | 14 → **11** | 31 → **30** | 67 → **62** | 400.8 → **409.4** | T\_OUT 1.72 → **USB\_DN 1.82** | 104 → **98** | GND In1 2533.39, 3V3 In2 2520.84 |
| ds2 | 310 → **315** | 19 → **17** | 11 → **12** | 91 → **96** | 507.4 → **508.3** | REFP\_F 3.23 | 14 → 14 | GND B.Cu 1008.42 |

The census does not move: `{tap: 1}`, `{tap: 6}`, `{fanout: 5, tap: 34}`, `{tap: 62}`,
`{fanout: 9, tap: 2}`, and every refusal is the same pad for the same reason. Two boards' copper
changed on purpose and their neighbours answered:

- **node** — the taps of a row now step out of line where staying in it would slot the plane below
  (finding 10). Everything in the bar improves except one number, and that one is the pair: `USB_DN`
  1.72 → **1.82**, which the `signals` step re-lays and R4 owns. It is the only regression the fixes
  bought, it is accepted, and it is in the open issues. `width_usb` 58 → **46** and `width_power`
  20 → **15** fall in the same build.
- **ds2** — one tap moved out of a fanout lane (finding 6) and KRT re-staircased around it:
  `vias_leftover` 19 → **17** and 2.13 mm2 more filled pour, against segments 310 → 315, micro
  91 → 96, off45 11 → 12 and 507.4 → 508.3 mm.
- **c3_usb** — nothing moved but 34 vias, by **0.1 um** each (finding 8), and its leftover came back
  with two more off-45 segments, two more `width_power` hits and one fewer micro segment. That is
  worth writing down for what it says about the leftover router rather than about the taps: KRT's
  output is not continuous in its input, and a 100 nm change is enough to move it.

### The five bug-grade findings

**1. `fab.via_in_pad_blockers` could not see a passive on three of the five boards.** `_PASSIVE_REF`
was `^[CRL]\d` — a digit straight after the letter — so it matched `C1` and `R1` and not `C_VBUS`,
`C_3V3_HF`, `R_FB_BOT` or `R_CC1`, which is every passive on buck, c3_usb and node. Reproduced by
injecting a via dead-centre in every passive pad of every built board: blinky 2 hits / 2 blockers,
ds2 44 / 44, buck 18 / **2**, c3_usb 24 / **0**, node 40 / **0**. `blockers` is the fab stage's hard
stop, so on c3_usb and node a via sitting in a 0402's pad would have shipped. Fixed by reading the
**part** rather than the reference: `Part.prefix` (`C`, `R`, `L`, `D`, `FB`) and pcbc's own
`kind="generic"`/`"led"`, with the corrected regex as the fallback an audit of a bare board file has.
The `== []` assertions are now positive: a via at every passive pad centre, and the blocker count must
equal the pad count — **4, 18, 26, 42** on blinky, buck, c3_usb and node. Those four are larger than
the probe's own census above (2, 18, 24, 40) for a reason worth naming: the probe walked `R`/`C`/`L`
footprints, and `passive_refs` reads every part whose declared prefix is a two-terminal passive's, so
the LEDs and diodes are in it too — which is the point of reading the part instead of the spelling.

**2. `via_in_pad` asked for containment, so a via breaking a pad's edge was invisible.** It tested
`|dx| + r <= sx/2` against the pad's declared `(size w h)` — full containment, of the anchor rather
than the copper. Three vias on the shipped boards break an IC pad's edge and no number mentioned
them: c3_usb (14.75,1.50) over `U1.27` by 0.2 mm, c3_usb (15.55,1.45) over `U1.26` by 0.15 mm, node
(17.0,2.4) over `U1.51` by 0.025 mm. The hit is now an **overlap** of the true copper
(`route_geom.clears` against `pads.pad_geoms`, so rotation, roundrect corners and a USB-C shell pad's
`gr_poly` are exact) and each hit carries `inside`. **Deviation from the finding's fix**, recorded
because it is a real disagreement: it asked for the IC exemption to be narrowed to `inside=True`
hits, which would refuse c3_usb and node — all three overlaps are same-net vias grazing an IC pin,
all tented, none of them a solder path — so a passive blocks on any overlap, partial or whole, and
the IC/USB-C exemption stays as FAB_NOTES describes it.

**5. pcbc's own 135-degree corner rule tripled and the number was nowhere.** buck 6 → 32, c3_usb
40 → 124, node 32 → 104 across S4 → S5, with a tap stub one of the two tracks in 16, 74 and 92 of
them. Recorded in S5's "what got worse" above and pinned as a ceiling in `BAR`. Accepted, not fixed:
making the tap prefer an exit whose junction with the leftover can be 135 degrees or wider is a
copper change and belongs behind its own measurement.

**9. The island check cannot see a fragment the fill deleted.** `plane_islands` counts
`filled_polygon` blocks, and under `island_removal_mode 0` a cut-off fragment is **removed** — so it
is never written as a second polygon and the count stays at 1. Measured: ds2's shipped board drops
five orphan GND islands totalling 8.52 mm2 and reports one island; a synthetic wall of 38 vias
fencing off a corner of node's GND plane deletes 69.44 mm2 and still reports one. The number that
moves is the **area**, so `route_verify.plane_area` is new and `test_examples_fab.py` pins mm2 per
(net, layer) beside the island count. ds2's orphans are a **pre-existing condition and not an S5
regression** — the `PCBC_PATTERNS=off` build drops three totalling 10.34 mm2 — and its pinned
1008.42 mm2 is 2.13 mm2 more than the S5 build, because the tap that left `U1`'s lane freed pour.

**10. A tap row at a footprint's own pitch cut the plane it did not join.** Eleven taps down node's
`U1`, one per pad at 0.8 mm, each carving `0.35 + 2 * 0.2` out of the **3V3** plane on In2.Cu: 0.05 mm
of neck against the zone's 0.1 mm `min_thickness`, so KiCad deleted the necks and eleven antipads
became one slot. Measured on the shipped board: a continuous **8.74 mm** void down x = 6.6861, and
inside a 3.5 mm window around the column **no path at all** through the 3V3 copper from (6.0,7.0) to
(8.0,7.0). B.3's "one via per pad, never clustered" had never been introduced to the zone's own
arithmetic — `plane_checks` only ever asks about the tapped net's plane, and `plane_islands` stays at
1 because a necked plane is not two islands.

`route_scene.zone_rules` now reads every pour's `(net, layer, connect_pads clearance, min_thickness)`
off the board — the header exists at post time and the fill does not — and `antipad_clash` refuses a
site whose antipad would come within `min_thickness` of another hole's **in a plane the via does not
join**. The clearance is the larger of the zone's own number and the clearance table's, which is
measured and not assumed: node writes `(clearance 0.18)`, the GND-to-3V3 class clearance is 0.2, and
the antipad on the built board has radius 0.375-0.380 mm around a 0.175 mm ring. Taking the zone's
0.18 alone put the requirement 0.02 mm light, and the first build with it still slotted the plane.

The result, on the same board: the taps of `U1`'s column alternate x = 6.686 and x = 6.386 — a 0.30 mm
stagger, which is the first candidate at which 0.35 + 2 × 0.2 + 0.1 = 0.85 mm fits between centres
0.8 mm apart — **all 62 taps are still placed**, the longest void down any line of the column is
0.75 mm (one antipad), the plane is continuous across the column between every pair, and the path
that did not exist is now 2.33 mm against a 2.00 mm straight line. node's plane gains 2.82 mm2 of GND
and 1.48 mm2 of 3V3.

**14. "No tap stub is in these counts" was false.** Four of the 54 combined `width_power` hits are
pcbc's own tap stubs: buck's `R_FB_BOT.2` at 0.64 mm against 0.781, node's `U4.2`, `U4.6` and `U4.7`
at 0.364 mm against 0.4. The narrowing is deliberate — `tap._width` is `fanout.py`'s neck, and a 0402
pad is 0.5 mm wide — so the pattern now says so itself in a `style:` note per stub, and `TAP_NECKED`
pins the count per board, matched to the stubs by the position and length KiCad reports.

### The gaps and the nits

**3 — tenting is pcbc's fact now.** Every tap's ring sits at exactly `clearance_min` from the pad it
welds (0.0889 mm on node, 0.1270 mm elsewhere) and a pad's mask opening here **is** its copper
(`pad_to_mask_clearance 0`, `solder_mask_margin 0`). What keeps solder out of the barrel is
`(tenting (front yes) (back yes))` — and that stanza was KiCad 10's default, written the first time
KiCad saved the file; `grep -rn tent src/pcbc/` returned nothing. `Stackup.via_tenting` now carries
the fact with its measurement, `seed` writes it into the board, and `test_examples_fab.py` reads it
back out of the F/B mask Gerbers: no pattern via has a flash (node's `layout-F_Mask.gts` has 157
flashes and none of them is a tap).

**4 — two taps in another part's courtyard, now counted.** c3_usb's tap for `J1.A1B12` at
(14.6729, 22.44) is inside `SW_RST`'s courtyard; node's for `C_MCU.1` was inside `U1`'s and is not any
more, because finding 10's antipad rule moved that tap. `copper_bar` reports `vias_in_courtyard` and
the count is pinned per board: **c3_usb 1, blinky, buck, node and ds2 0**. It is a placement note, not a refusal: KiCad's
courtyard rules are footprint-to-footprint, the via is tented, and nothing is illegal.

**6 — a via may not sit in a fanout lane.** `lane_ok` and D.1 both looped
`if p.kind != "seg": continue`, so no via was ever put to the lane rule and 105 of the tap's 210
pieces were exempt from it. One instance on the boards: ds2's `C5.2` tap via, 0.1221 mm inside `U1`'s
top lane, occupying the column `U1.11` (AIN0) escapes through. A via has no run length, so occupancy
is the test; the stub now leaves that pad upward. ds2's bar paid for it, above.

**7 — the neck is a `style:` line now**, counted in the census like A.4 rule 4: "tap GND: U4.7's stub
necks to 0.364 mm, the pad's across dimension, against the Power class's 0.4 mm".

**8 — the tap adds `EPS_MM` like everything else.** 88 of 105 taps sat at exactly `clearance_min`
from the pad they weld, which is copper `route_geom.clears` refuses; the pattern only got away with it
because `_in_a_pad` exempts the primitive it leaves from. `route_scene.pad_exits` already adds the
same epsilon with the same comment. The cost is above: c3_usb's leftover moved.

**11 — D.5 runs in the product.** `plane_checks` and `plane_islands` were called from `tests/` and
from nowhere in `src/`, so on any board that is not one of the five examples neither question was ever
asked. `build_job` now asks both on the board the gate itself refilled and saved, and fails the build
with the sentence naming the pad; the result carries `planes: {islands, area_mm2, fails}`.

**12 — `plane_checks` asks the ring.** It asked the centre, and the via's diameter never entered it,
because `copper.json`'s via key carries neither size nor drill and the one caller had to invent 0.0.
The sidecar now records `w` and `drill` per piece and the check requires the centre plus 16 points of
the ring to be in the net's own filled copper. Measured before changing it: all 62 node taps, all 34
c3_usb taps and both ds2 taps are fully covered, so this tightens a true statement rather than
fixing a false one.

**13 — the fall-through's real price, in B.3's own docstring.** A refused tap "costs a via pcbc would
have placed better and never costs the connection" was half the story: KRT's `plane_taps` step runs
with `--same-net-pad-clearance -1`, for the reason recorded in `route.py` (with the keepout on it
welded nothing — 4 of 59 GND pads), so its replacement via is under no obligation to clear the pad it
welds. node's refused `U1.51` came back with a via overlapping that pad's copper by 0.025 mm, which
is exactly what `_in_a_pad` refuses. **Deviation:** the finding's second option — pass the flag to
that step — is refused by the measurement already in the code, so the docstring is where this lands.

**15, 16, 18 — the record.** c3_usb's `off45` 7 → 11 and `VBUS`'s detour 1.33 → 1.70 are now in S5's
"what got worse" with their per-net attribution, and the bar carries `detours` per net so a max can no
longer hide a net under it. S5's decision-1 arithmetic quoted node's window and buck's example in one
sentence; each board's window is now given with its own numbers.

**17 — an unfilled plane is a sentence.** `plane_checks` skipped a net with no filled zone and
`set(plane_islands(...).values()) <= {1}` is True of an empty dict, so deleting node's 3V3 fill left
both checks silent while 17 taps welded nothing. The first reports the line; the second is pinned
against the expected key set.

## Verification of S2 to S4, independent of the agents that wrote them

Re-run on a clean checkout of the committed tree, not taken on trust:

- **The five bug-grade findings the attacks raised are refused now**, each checked directly: a
  star-wound point list is not accepted as a hull; a repeated point is rejected by `Path`; a
  zero-length leg makes `is_octilinear` and `turn_ok` return False rather than raise;
  `octile_path(a, a)` raises; `clears` returns True when the requirement is at or below zero.
  `turn_ok` was checked against the stated rule (interior angle at least 135 degrees) computed
  independently with `atan2` over all 64 octant pairs: no disagreement.
- **Determinism holds.** Two builds of c3_usb in the *same* directory give a byte-identical routed
  board; two builds in *different* directories differ in exactly two lines, both the absolute path
  in a `(model ...)` line, which is the caveat `docs/constraints.md` already records. `copper.json`
  and the Gerbers are identical either way, including across `PYTHONHASHSEED`.
- **The escape hatch is exact.** `PCBC_PATTERNS=off` on node reproduces the S1b row to the digit
  (28 vias, 63 off-45, 181 micro, T_OUT 1.72x, 512.8 mm), so the pattern stage is the only thing
  that moves a number.
- **All five boards build to fab with the gate verified**, and the full suite is 437 green with
  KiCad and KRT.

### What the hop is worth, and what it costs

| board | leftover | vias | off 0/45/90 | under 0.2 mm | worst detour | verdict |
|---|---|---|---|---|---|---|
| blinky | 100 % → **0 %** | 0 | 0 | 2 → **0** | 1.03 → 1.04 | its one net is a hop |
| buck | 100 % | 4 | 3 | 26 | 2.09 | nothing: both its local nets refuse |
| c3_usb | 98.3 % → **95.2 %** | 13 → 14 | 7 | 139 → **168** | 1.92 → **1.81** | mixed |
| node | 100 % → **97.0 %** | 28 | 63 → **57** | 181 → **154** | 1.72 | better |
| ds2 | 97.1 % → **92.7 %** | 32 → **28** | 13 → **11** | 113 → 118 | 3.23 | better |

Three boards better, one untouched, one mixed. c3_usb's is the one to watch: hopping CC1, CC2 and
LED locks copper beside the USB-C, and KRT answers with 29 more staircase segments and one more
via while the pair itself comes out **shorter** (1.92 → 1.81). That trade is accepted on purpose —
a differential pair's length is electrical and a staircase is not, and R3's own router removes
staircases wholesale — but it is the first place where pattern copper has made a neighbour worse,
and the tap pattern will change the same neighbourhood again.

The leftover share barely moves on the big boards, which is exactly what the census predicted: a
hop is 5.7 % of the copper. The design's 70 % target needs the tap (12 %), and its 45 % target
needs the spine (37 %).

## Verification of S5 and S5r, independent of the agents that wrote them

Re-run on the committed tree from fresh builds of all five boards:

- **Every number in the S5r table reproduces exactly.** Segments, leftover vias, the exact
  `vias_pattern` per reason, off-45, micro and routed length match on all five boards, and the
  gate reports `verified` on each.
- **The fab check that was blind now fires.** A via planted at the centre of every passive pad is
  reported as a blocker on all five boards (4 on blinky, 18 buck, 26 c3_usb, 42 node, 44 ds2). The
  attack's finding was that `via_in_pad_blockers` matched the reference spelling and so could not
  see the passives on three boards; it reads the part now. The three real via-in-pad hits that
  remain are on IC pins, which the fab notes allow as filled and capped, and no blocker survives.
- **The plane slot is gone.** node's tap column under U1 now alternates between x 6.386 and 6.686,
  so the antipads no longer merge. Every plane on every board comes back as exactly one island
  after the gate refills, and the check that each tap via lands inside its own net's plane is
  clean on all five.
- **460 tests green** with KiCad and KRT.

### What the tap is worth

| board | leftover | vias (leftover + pattern) | off 0/45/90 | under 0.2 mm | routed mm |
|---|---|---|---|---|---|
| blinky | 0 % | 0 + 1 tap | 0 | 0 | 23.6 |
| buck | 100 % → **96.6 %** | 4 → **1** + 6 taps | 3 → **2** | 26 → 37 | 142.5 → **139.4** |
| c3_usb | 95.2 % → **86.4 %** | 13 → **8** + 39 | 7 → 13 | 168 → 203 | 312.9 → 356.7 |
| node | 97.0 % → **84.9 %** | 28 → **11** + 62 | 57 → **30** | 154 → **62** | 513.1 → **409.4** |
| ds2 | 92.7 % → **92.3 %** | 19 → **17** + 11 | 11 → 12 | 118 → 96 | 509.9 → 508.3 |

node is what the pattern was for: a hundred millimetres of copper gone, staircases down by more
than half, off-45 halved, and seventeen of its vias now placed deliberately rather than found by a
maze search. c3_usb is the opposite case and is recorded rather than argued away: 34 taps buy it
nine points of leftover and cost it staircases and length, because its pour is on the back of a
two-layer board where every tap is also an obstacle to the next track. The spine (S7) owns that
neighbourhood.

## S7 — the spine, and the width a maze router will not keep

pcbc now writes the power nets itself. `src/pcbc/patterns/spine.py` is B.4; `patterns.MID` is the
second half of C.1's pre stage — the half that runs **after** the fanout, because C.1 puts the
escapes at step 2 and the spine at step 5 and the spine is the widest copper pcbc writes — and
`patterns.merge_plans` makes the two halves one plan for KRT (C.4).

**The number this slice exists for.** `width_power` counts every place KRT necked a power track below
its class. A spine writes at the class width or refuses, and never in between:

| board | `width_power` before | after | what is left |
|---|---|---|---|
| blinky | 0 | 0 | nothing: its one power net is a pour with one pad |
| buck | 39 | **23** | 2 GND tap stubs; the rest is KRT finishing the links the backbone could not make |
| c3_usb | 51 | **40** | 5 GND, the rest `VBUS` and `3V3` leftover |
| node | 15 | **14** | mostly `GND` and `3V3` plane leftover; `VBUS` is the only spine there |
| ds2 | 3 | 3 | ds2 has no spine at all, and that is the first open issue |

**Read this table with S7r's millimetres beside it.** `width_power` counts necked **segments**, so
the same copper cut into fewer pieces reads as an improvement: buck's fall is real (52.65 → 20.42 mm
of sub-class power copper), c3_usb's and node's are mostly KRT re-segmenting identical copper, and the
one net c3_usb spined had no `width_power` hits to lose. The per-net millimetres are in S7r.

**Before and after, every board, fresh builds** (`pcbc build --force` into a temp copy). "Before" is
S5r as landed. `PCBC_PATTERNS=off` on this same source reproduces the **S1b** row exactly on all five
— blinky 6/0/2/22.5/1.03, buck 96/4/3/26/142.5/2.09, c3_usb 342/8+5/7/139/312.8/1.92,
node 437/28/63/181/512.8/1.72, ds2 337/22/13/113/514.6/3.23 — so the rollback is still a rollback and
every move below is the spine and nothing else:

| board | leftover share | segments | vias\_leftover | vias\_pattern | off 0/45/90 | under 0.2 mm | routed mm | worst detour | angles | refusals | route wall |
|---|---|---|---|---|---|---|---|---|---|---|---|
| blinky | 0 % → 0 % | 6 → 6 | 0 → 0 | `{tap: 1}` | 0 → 0 | 0 → 0 | 23.6 → 23.6 | LED 1.04 | 0 → 0 | 0 | 4.6 → 6.9 s |
| buck | 96.6 % → **84.7 %** | 114 → 175 | 1 → 1 | `{tap: 6}` | 2 → **1** | 37 → **96** | 139.4 → 149.3 | VIN 2.09 → **2.55** | 32 → **28** | 2 hop, **2 spine** | 9.3 → 16.4 s |
| c3_usb | 86.3 % → **79.6 %** | 453 → **444** | 8 → **7** | `{fanout: 5, tap: 34}` | 13 → **11** | 203 → **188** | 356.7 → 361.2 | USB\_DN 1.81 → **VBUS 1.70** | 122 → **130** | 1 hop, **2 spine**, 2 tap | 16.3 → 23.1 s |
| node | 84.9 % → **82.4 %** | 318 → **312** | 11 → 11 | `{tap: 62}` | 30 → **25** | 62 → **54** | 409.4 → 409.9 | USB\_DN 1.82 | 98 → **100** | 1 hop, **1 spine**, 2 tap | 25.9 → 34.8 s |
| ds2 | 92.3 % → 92.3 % | 315 → 315 | 17 → 17 | `{fanout: 9, tap: 2}` | 12 → 12 | 96 → 96 | 508.3 → 508.3 | REFP\_F 3.23 | 14 → 14 | 5 hop | 15.0 → 26.9 s |

The gate is verified on all five (KiCad DRC clean, zero unconnected items, the canary fired, the pads
bound exactly as `board.py` says); `verify_copper` is empty on all five; every plane comes back one
island after the gate's refill and there is not a via-in-pad blocker anywhere;
`copper.power_ampacity_failures` is empty on every board and is now asserted per build rather than
only through the fab stage — **and that assertion said nothing about the board as S7 shipped it**,
because the function read the net's widest track. See S7r: the gate now judges the copper pcbc wrote
and can fail, and `ampacity.power_bottlenecks` measures what each rail really carries.

Two builds of c3_usb and node into two directories, one of them under a different `PYTHONHASHSEED`,
give an **identical** `copper.json` and routed boards differing in exactly the one and five absolute
`(model ...)` paths — the caveat `docs/constraints.md` already records and the same result S4 and S5
measured. The three pattern stages together cost 45 ms on blinky, 209 on ds2, 542 on buck, 990 on
node and 1456 on c3_usb, against F.3's 2.0 s.

What pcbc owns now, exact per board (`copper.json`'s census, and `test_examples_fab.py::OWNS`):

| board | fanout | hop | spine | tap | leftover |
|---|---|---|---|---|---|
| blinky | — | 5 seg / 22.8 mm | — | 1 seg / 1 via / 0.8 mm | 0 |
| buck | — | — | **10 seg / 18.0 mm** | 6 seg / 6 vias / 4.8 mm | 159 seg / 126.5 mm / 1 via |
| c3_usb | 5 seg / 5 vias / 5.3 mm | 11 seg / 9.8 mm | **9 seg / 25.1 mm** | 34 seg / 34 vias / 33.5 mm | 385 seg / 287.4 mm / 7 vias |
| node | — | 11 seg / 15.4 mm | **9 seg / 8.8 mm** | 62 seg / 62 vias / 47.8 mm | 230 seg / 337.9 mm / 11 vias |
| ds2 | 9 seg / 9 vias / 13.5 mm | 10 seg / 23.7 mm | — | 2 seg / 2 vias / 1.7 mm | 294 seg / 469.3 mm / 17 vias |

A spine writes **no vias**: R2 forbids a layer change on one (H.4), so `vias_pattern` does not move.

### What each board spines, and what it does not

| board | candidate nets | spined | pad pairs joined | coverage | refused |
|---|---|---|---|---|---|
| blinky | 0 | — | — | — | 0 |
| buck | 2 (`5V`, `VIN`) | 2 | `5V` 3 of 4, `VIN` 2 of 4 | 0.639, **0.159** | **2** |
| c3_usb | 2 (`3V3`, `VBUS`) | **1** | `3V3` **5** of 7, `VBUS` **0** of 6 (no copper) | 0.683, 0.000 | **2** |
| node | 1 (`VBUS`) | 1 | 3 of 6 | 0.477 | **1** |
| ds2 | 0 | — | — | — | 0 |

**Two cells in this row were wrong as first published and the review caught both** (S7 review,
findings 5, 8, 15, 17). c3_usb spines **one** net, not two: the `VBUS` spine refuses all six of its
links and writes no copper at all, so the board's nine `spine` pieces (25.13 mm) are `3V3`'s alone.
And `3V3` joins **five** of its seven pad pairs, not four, because a link that runs over a third
station of its own net connects it for free and no count of successful `_link` calls can see that.

Both numbers now come off the copper rather than off a hand-copied row: `PatternResult.links` is
`stations - components` over a union-find of the emitted pieces and the station pads,
`route_emit.census_by_net` splits D.4's census by net as well as by reason, and
`test_examples_fab.py::SPINE_LINKS` and `SPINE_NETS` pin both per board, exact. A net listed as
spined that writes no copper is now a test failure.

`coverage` is spine millimetres over the net's own airwire, and it is the column that separates a
spine that replaced a route from one that got in its way. By links made, buck's `VIN` (2 of 4) and
node's `VBUS` (3 of 6) are indistinguishable; by coverage they are 0.159 and 0.477.

**Not one spine on these boards connects its whole net**, and that is B.4's own answer rather than a
failure: a pad whose link fails splits the spine and both halves are kept, `net_open` hands what is
left to KRT, and the refusal names the first link that did not fit. What the pattern buys is not the
connection — KRT would have made it — but the **width** it is made at.

### Three decisions the measurement forced, each with its number

1. **The comb is tried first and fits no net on any of the five boards.** B.4's (a) is a straight
   trunk on a lane `free_intervals` proves free, with a perpendicular rib to every pad; its (b) is the
   stations linked pairwise. Every one of the five spines refuses the comb and takes the backbone, and
   the `style:` line says so rather than leaving it silent. The comb is not dead code and is tested end
   to end on the one net in this repo whose comb does fit — ds2's `VDDA`, whose trunk is 14.9615 mm at
   y = 12.700 with a zero-length rib at `J2.1`, a straight rib from `U1.12` and an **ell** where
   `C4.1`'s foot is blocked (`test_the_comb_is_the_first_form_and_it_fits_no_net_on_these_boards`).
   That same comb is why ds2 has no spine at all: see the open issues.

2. **Two pads of one net are routinely closer together than their own exits, and B.0's 112
   candidates then have nothing to say.** `pad_exits` puts an exit `half + the widest clearance the
   net owes on that footprint + width/2` out, and that distance grows with the width — so at
   0.781 mm on buck, `C_IN1.1 -> U1.3` are 1.716 mm apart with their exits 1.3156 and 1.1266 mm out,
   `U1.3 -> C_IN2.1` 1.543 mm, `C_IN2.1 -> R_EN.1` 3.009 mm, and every exit-to-exit candidate for
   those three doubles back on itself for `mitre` to refuse as a hairpin. Three of `VIN`'s four links
   and three of `5V`'s four were going to KRT for a reason about the exits and not about the board.
   `spine.link_shapes` puts **three** more candidates in front of the enumeration where and only
   where the pads are `crowded`: the straight centre line and `route_geom`'s two octile paths between
   the two pad centres. The bound is 4 x 4 x 7 + 3 = **115** per link. The centre line is exempt from
   nothing — `blocked` judges it exactly as it judges the rest — and it is the one shape the exit
   enumeration cannot express. Measured: buck went from 1 link of 8 to 5 of 8, and c3_usb's `3V3`
   from 3 of 7 to 4 of 7.

3. **A backbone is one growing run, not a bag of links.** A middle station is a T-junction whose two
   links share that pad's centre exactly, so `route_verify.paths_of` re-assembles them into one run
   and asks `turn_ok` at the station — and where the chain bends there, the two links leave by
   adjacent sides and meet at a right angle, which is the warning `dru.py` writes
   `pcbc_geometry_angles` to catch. Judging each link alone writes that corner and then fails pcbc's
   own self-check. `_backbone` grows one run a link at a time, splices the new link onto the run
   before `mitre`, and re-judges the whole of it each time, because a mitre moves copper that was
   already accepted. buck's `5V` bends at `C_OUT1.1` and is the case.

### Two numbers got worse on buck, and they are the finding

buck is 86 % power and the only board here whose class width is 0.781 mm — the widest copper in the
repo. 18 mm of it is now locked across the middle of the board before KRT starts, and KRT answers:

- **micro 37 → 96.** Segments under 0.2 mm: a grid router's staircases, tripled, as it works round a
  0.781 mm wall it did not choose.
- **`VIN`'s detour 2.09 → 2.55**, already the worst on any board. `VIN` gets 2 of its 4 links; the
  two it does not get are `J_IN.1 -> C_IN1.1` (14.963 mm across a row of `GND` pads — B.4's own
  worked example, and it refuses for B.4's own reason) and `C_IN2.1 -> R_EN.1`. KRT then routes both
  around the copper the other two links laid.

Against that: `width_power` 39 → **23**, off-45 2 → **1**, `angles` 32 → **28**, leftover share
96.6 % → **84.7 %**. The trade is taken because a necked power track is an electrical fact and a
staircase is not — `docs/r2-design.md` F.3 item 7 forbids a soft rule *rising*, and `width_power`
falls — but both ceilings are raised in `test_examples_fab.py::BAR` with this paragraph as their
reason, and they are open issues, not a footnote.

c3_usb is the mirror image and worth reading beside it: its `angles` rise 122 → **130** and its
routed length 356.7 → 361.2 mm, while `USB_DN` — the differential pair, KRT's until R4 — comes out
**shorter**, 1.81 → **1.65**, and `USB_DP` 1.62 → 1.56. Its `3V3` detour rises 1.38 → 1.53, its
`BOOT` 1.05 → 1.18 and its `GND` 1.44 → 1.48. node moves barely at all: off-45 30 → 25, micro
62 → 54, `angles` 98 → 100, one `width_power` hit.

**The reason first given for the pair's improvement was wrong and is withdrawn** (S7 review, finding
18). It read "because `VBUS` is no longer wandering through the pair's corridor"; pcbc writes **no**
`VBUS` copper on c3_usb — the spine refuses all six of its links — and `VBUS`'s own detour is
1.70 before and after, the one c3_usb net in `totals["detours"]` that does not move. The only net the
spine wrote copper for there is `3V3`, whose 25.1 mm backbone is locked before KRT's `signals` step
and whose own detour rose 1.38 → 1.53 to pay for it. Whether that is what straightened the pair is
not measured here, so the pair's 1.81 → 1.65 is recorded as unattributed leftover motion rather than
given a cause: showing it would take a build with the `3V3` spine suppressed, the way the `crowded`
claim was shown by disabling `crowded`.

### ds2, and the sweep that decided `WIDE_MM`

**S7 deviates from B.4 in one place**: B.4 uses `WIDE_MM` only to admit a net that is not `power`,
and S7 applies it to every spine, so a net whose class is narrower than 0.4 mm gets none. ds2 is the
board that decided it, and the measurement is unambiguous.

ds2 is two layers, dense, with a 0.65 mm TSSOP whose pins escape through 1.29 mm lanes and **six**
`vias=False` single-layer analog nets whose only corridors are the ones a spine would take. Its Power
class is **0.25 mm** against a fab floor of 0.127, so a spine there is spending a corridor to save
0.123 mm of width. Every configuration tried broke the gate:

| ds2 with its 0.25 mm spines | spine copper | what failed |
|---|---|---|
| link cap 6 mm | 35.9 mm | `AIN0` found no path on F.Cu to `U1.11` |
| link cap 8 mm | 18.9 mm | `3V3`, `GND` and `VSS` found no path on any layer |
| link cap 8 mm, comb capped too | 18.9 mm | the same three |
| link cap 12 mm | 55.7 mm | `AIN0` again |
| no cap | 71.6 mm | `AIN1` no path, `GND` no path, the pour could not reach `C5` |

The 6 mm and 12 mm failures are `VDDA`'s **comb**: a 14.9615 mm trunk at y = 12.700 that cuts the
board in half to pick up `J2.1`. C.1 predicts the whole class of failure — the constrained nets are
step 3 and the spine is step 5, but until `chain` (S6) lands, step 3 is KRT's and runs *after* the
whole pre stage, so the spine takes the corridors the constrained nets structurally need.

A locality cap was written and then removed, because with the width floor in place the sweep says it
only costs:

| SPINE\_MM (link span cap) | buck leftover | c3_usb | node | ds2 |
|---|---|---|---|---|
| 8 mm | 91.7 % | 84.8 % | 82.4 % | 92.3 % (no spine) |
| 12 mm | 84.7 % | 84.8 % | 82.4 % | 92.3 % |
| none | **84.7 %** | **79.6 %** | **82.4 %** | 92.3 % |

All five build and the gate is verified at every row, so the cap buys nothing and costs c3_usb five
points of leftover and buck seven. It is gone, and the fact that nothing now bounds how far a spine
reaches is S7's second open issue rather than a number with no measurement behind it.

### What is not measured here

- **`chain` (S6) has not landed**, so C.1's step 4 does not exist and the spine is the only pattern
  in the mid stage. The two are independent; the order in `patterns.MID` is C.1's and has room for it.
- **The comb's rib neck** is implemented to B.4 item 5, and no rib on any of the five boards necks
  because no comb fits. `test_no_spine_copper_is_ever_narrower_than_its_class` puts the rule to
  `_rib_width` directly instead. The rule it puts is not the one S7 shipped: the review found the
  neck floored on `amps / pads`, which under-sized the one branch that carries everything (below).
- **Nothing about differential pairs**, except where the leftover moved: `USB_DN` and `USB_DP` are
  KRT's until R4, and their improvement on c3_usb is a by-product, not a claim.

### The open issues S7 leaves

1. **ds2 has no spine, and the reason is a width floor rather than a fix.** 38 % of its copper is
   power and it keeps every bit of it. What it actually needs is C.1's step 3 — the constrained nets
   routed before the spine rather than after it — which is `chain` (S6) plus a constrained-net
   pattern, or R3's blocking analysis deciding corridors properly. `WIDE_MM` is a threshold that
   happens to separate the boards where the trade pays from the one where it does not, and it is
   honest about being that.
2. **Nothing bounds how far a spine reaches.** The cap was measured to cost and removed (above), so a
   wide net spread across a large board can lay a trunk corner to corner exactly as ds2's `VDDA`
   comb did. It has not happened on a board that passes the floor; it will.
3. **buck's micro 37 → 96 and `VIN` 2.09 → 2.55** (above). Both ceilings raised in `BAR`, and `VIN`
   is still the worst detour in the repo — the net B.4 names in its own refusal example.
4. **c3_usb's `angles` 122 → 130 and node's 98 → 100.** pcbc's own 135-degree rule, rising for the
   third slice running (S5's open issue 2 is the same number). Every corner a pattern writes is
   mitred; these are the leftover's, meeting locked copper at whatever angle KRT chose.
5. **Four of the eight candidate nets write copper, and every one of the four is partial.** The
   population is exactly eight: buck's `5V` and `VIN`, c3_usb's `3V3` and `VBUS`, node's `VBUS`, and
   ds2's `3V3`, `VDDA` and `VSS`. The three ds2 nets are excluded by `WIDE_MM` (0.25 < 0.4) and
   c3_usb's `VBUS` refuses all six of its links and writes nothing, so four write copper: buck's `5V`
   joins 3 of its 4 pad pairs, buck's `VIN` 2 of 4, c3_usb's `3V3` 5 of 7 and node's `VBUS` 3 of 6.
   (The sentence here first said "five of eight nets are not spined and three of five that are get
   half their links", which contradicted the table two sections above it — S7 review, finding 21.
   `specs` filters on **terminals**, so "three or more pads" is three or more footprints, which is
   worth saying since it is the only place the population is named.) The gap between "pcbc owns the
   power nets" and what this slice ships is the maze router, which is R3.
6. **Three of the five boards do not carry their declared current end to end, and R2 cannot fix
   them.** buck's `VIN` carries 1.21 A of 2 A, node's `VBUS` 0.527 A of 1 A through a single 0.2 mm
   via, and c3_usb's `VBUS` runs 24.07 mm below its class including 0.127 mm cut edges in series. The
   copper that necks is KRT's leftover in every case. Recorded as a ledger below and pinned in
   `test_examples_fab.py::BOTTLENECK`; R3's maze router owns it.
7. **Same-net copper closer than the process floor.** buck 1, c3_usb 22, node 16 places where two
   pieces of one net sit under `clearance_min` apart with bare laminate across the gap. The class is
   pre-existing rather than the spine's — `PCBC_PATTERNS=off` measures buck 3, c3_usb 17, node 22 —
   but nothing measured it at all until the review, and a wide locked spine is the copper most likely
   to be hugged.

---

## S7r — the review of S7, and what the twenty-three findings moved

Four independent attacks on S7, 23 findings, 14 of them bug-grade. **No board changed**: every BAR,
SOFT, OWNS, DETOURS and refusal number in the tables above is byte-for-byte what S7 recorded, because
every bug was in a measurement, a printed sentence or a rule no board reaches yet. What changed is
what the build can see and what it says.

### The gate could not fail, and three boards were failing its criterion

`copper.power_ampacity_failures` compared `copper_by_net`'s `width` — a **`max`** over the net's
segments — against IPC. One wide segment satisfied a whole net however narrow the rest of it was, so
the check returned `[]` on every board and could not have returned anything else. S7 added a
per-build assertion that it is empty and offered it as proof that "a spine that necks is the bug it
exists to fix" is enforced; the same assertion passes on the `PCBC_PATTERNS=off` board whose `VIN` is
two thirds fab-floor copper. Four of the twenty-three findings are this one (1, 7, 12, 20), and a
fifth (2) is the same blindness one layer down: nothing in the build read a **via's** current at all,
and `ViaSpec.per_change` appeared nowhere outside `constraints.py`.

It is now two questions, because they have two different answers.

**The gate** (`power_ampacity_failures`) judges the copper **pcbc wrote**, off `routed/copper.json`:
a hop, a spine trunk or a backbone link narrower than its class fails the build, with the one
exemption R-I3 already grants (`tap` and `fanout` pad necks, which `TAP_NECKED` counts). It is empty
on all five boards and it is now failable — `test_examples_fab.py` re-runs it with the same copper
declared narrow and requires it to fire.

**The measurement** (`ampacity.power_bottlenecks`) is the honest whole-net question: the
widest-bottleneck path between every pair of a net's pads over the copper that actually touches, with
each piece carrying what it can carry — `stackup.track_amps` (the exact inverse of
`current_width_mm`) for a track, `stackup.via_amps` for a via, and vias within 1.0 mm of each other
counted as parallel. The unit is amps because a 0.2 mm drill and a 0.2 mm track are not comparable as
widths: 0.527 A against 0.745 A. A net with a zone in the file is exempt — a plane is the rail's
conductor — and the exemption reads the **file**, not `job.planes`, which is `()` on buck, c3_usb and
ds2 while all three have a poured `GND`.

What it says, `PCBC_PATTERNS=off` → as built, with the millimetres of that net's copper under its
class beside it:

| net | carries, off | carries, built | class | under class, off → built | verdict |
|---|---|---|---|---|---|
| buck `5V` | 0.707 A (a via) | **1.999 A** (0.781 mm) | 2 A | 9.68 → **0.00** mm | ok |
| buck `VIN` | 0.536 A (0.127 mm) | **1.21 A** (0.3905 mm) | 2 A | 41.64 → **19.39** mm | **under current** |
| c3_usb `3V3` | 0.707 A (a via) | **1.231 A** (0.4 mm) | 0.5 A | 0.00 → 0.00 mm | ok |
| c3_usb `VBUS` | 0.536 A (0.127 mm) | 0.536 A (0.127 mm) | 0.5 A | 20.56 → **24.07** mm | **under floor** |
| node `VBUS` | 0.527 A (a via) | 0.527 A (a via) | 1 A | 7.60 → **6.48** mm | **under current** |
| node `LOAD` | 1.231 A | 1.231 A | 1 A | 0.00 → 0.00 mm | ok |

Three things to read in it.

- **The spine's real win is buck, and it is large.** `5V` goes from a 0.707 A via bottleneck to the
  full 2 A, and `VIN`'s own 2 A path — `J_IN.1 → U1.3`, connector to regulator pin — goes from
  **0.127 mm to 0.781 mm**, a factor of six. `VIN`'s remaining 0.3905 mm bottleneck is on the branch
  to `R_EN.1`, a 100 k pull-up drawing 0.12 mA, which is the one leaf where a neck is defensible.
- **c3_usb's `VBUS` got worse.** 20.56 → 24.07 mm under class, on the net the spine refused, because
  the 25.1 mm `3V3` backbone is in its way. That is the obstruction cost, and no number in S7 could
  show it. Its verdict is `under floor` rather than `under current`: 0.127 mm carries 0.536 A of its
  0.5 A on the IPC-2221B curve with 7 % margin, and the 0.150 mm it is short of is pcbc's own
  manufacturability floor. Seven per cent and no allowance for the LDO's inrush is not margin.
- **node's `VBUS` is a via problem, not a track problem** (finding 2). Its 1 A crosses two single
  0.2 mm-drill vias in series, each rated 0.527 A — 1.9x over, twice — and the class via for that net
  is 0.8/0.4 (0.871 A), which KRT was never handed. Both vias are KRT's; the spine writes none.

**This is a ledger and not a build-stopper**, and the reason is stated rather than assumed: the copper
that necks is KRT's leftover on every one of the three, and R2 has no slice that can move it. Making
it fatal would stop all three boards instead of measuring them. `BOTTLENECK` in
`test_examples_fab.py` pins `carries` as a floor that must rise and `under_mm` as a ceiling that must
fall, per net, per board.

### A branch's current is a property of the pin, not of the pad count

`_rib_width` floored a rib's neck at `current_width_mm(amps / pads)` and then let the pad's own
across dimension decide. On buck's `VIN` that is 2 A over five pads = 0.4 A a pad, and **no pad on
that net draws it**: `J_IN.1` (the input connector) and `U1.3` (the TPS54202's VIN pin) carry the
whole 2 A in series, the two 22 uF caps carry ripple, and `R_EN.1` carries 12 V / 100 k = 0.12 mA. So
the rule asked `U1.3` for a rib of **0.532 mm** — a SOT-23-6 pad's width, arrived at by luck of
package geometry — which carries 1.513 A of that pin's 2 A, while handing 0.54 mm to a pull-up that
needs four orders of magnitude less (findings 4, 6, 11).

The fix is to stop dividing. A rib may neck only into a **bypass pad**, and that is the one
population pcbc can name today: a declared two-terminal passive whose other pad is on a plane or
return net, which is `tap`'s own test. Everything else — an IC's power pin, a connector, a series
passive — gets the trunk width, and when the trunk width does not fit beside its neighbours `blocked`
refuses the comb, which is B.0's "a pattern never degrades" enforced by the clearance table rather
than by a second rule. `share` survives as the floor under a bypass rib, and the string the refusal
prints now says it is a placeholder and names the pads it applies to, instead of stating a per-pad
current as fact on all five boards.

Latency, stated plainly: the comb fits no net here, so this under-width copper was never on a board.
It was the rule that would decide the first one.

### Two bugs in the comb's own geometry, both visible in the refusal it prints

- **The free-lane window on a diagonal was the bounding box** (finding 13). `_project` returns the
  min and max of `u` over the outline's four corners, which is right as a conservative obstacle
  extent and wrong as a containment window: the across range attainable on a diagonal shrinks as you
  move along it. c3_usb's `VBUS` got a window of `(0.6, 69.4)` and spent both its candidate offsets
  on lanes whose trunk endpoints are off a board that ends at x=39.7, y=29.7 — one of them at x=45.0
  — while the refusal said "2 free lanes there could take it" one line above naming the board edge as
  the blocker. `spine.trunk_window` now returns the attainable range, intersected at the span's two
  ends (exact, because the bound is linear in `along`), less the trunk's own half width. The real
  window for that net is `(23.9078, 53.2272)`. It also shrinks the `x` and `y` windows by the same
  half width, which moves ds2's `VDDA` comb offset 2.9705 → 3.033 and buck's widest reported lane
  5.243 → 4.853 mm — lanes that could not hold the trunk centred in them were never lanes.
- **The refusal printed the lane's coordinate with the wrong axis letter** (finding 14). The offset
  is an **across** coordinate and the string labelled it with the trunk's **along** axis: buck's
  `VIN` printed `x=2.922` for a `y`, c3_usb's `VBUS` printed `v=56.605` for a value `v` cannot take
  over that span at all, and `docs/r2-design.md` B.4's own worked example has it right. It is the one
  number a person or an agent acts on — the line ends in `Place("J_IN", toward="left")`. `ACROSS` is
  the fix, and a test asserts the printed letter differs from the trunk's on every refusal on every
  board, so the two cannot drift back together.

### What the leftover paid, which nothing measured

`copper_bar`'s per-net `routed_mm` and `detour` are over **all** the copper on a net, pattern and
leftover together, so a spine that adds to KRT's route rather than replacing it looks like a win in
every number that existed (finding 10). `nets[net]` now carries `pattern_mm`, `leftover_mm`,
`leftover_segments`, `leftover_vias` and `leftover_micro` beside them, and the trade, against the
`PCBC_PATTERNS=off` leftover, is:

| net | spine mm | leftover off → built | leftover mm saved per mm of spine | coverage |
|---|---|---|---|---|
| buck `5V` | 14.62 | 24.23 → 11.59 | **0.87** | 0.639 |
| buck `VIN` | 3.38 | 44.30 → **50.68** | **-1.88** | **0.159** |
| c3_usb `3V3` | 25.13 | 40.38 → 31.09 | 0.37 | 0.683 |
| node `VBUS` | 8.82 | 24.79 → 14.52 | 1.16 | 0.477 |

**A coverage floor was proposed and is rejected, and the measurement is why.** Finding 9 asked for
`SPINE_COVER_MIN = 0.30`, which drops buck's `VIN` alone: its 3.38 mm of spine buys +6.27 mm of
leftover, +38 segments, +40 staircases and `VIN`'s detour 2.09 → 2.55, and a `5V`-only buck is better
than the shipped one on every BAR number except `width_power`. But the same build is worse on the
number this slice exists for: `VIN`'s 2 A path `J_IN.1 → U1.3` is **0.127 mm without the `VIN` spine
and 0.781 mm with it** (measured on the `PCBC_PATTERNS=off` board, whose `VIN` leftover is identical
to a `5V`-only board's — the two spines do not interact). Refusing that spine would trade a factor of
six on the rail's only series path for staircases the fab does not care about, and a necked power
track is an electrical fact while a staircase is not — `docs/r2-design.md` F.3 item 7's own argument,
applied in the direction it points. The coverage ratio is **reported** per net instead, in
`PatternResult.coverage`, in the table above and pinned in `SPINE_LINKS`, so a future spine that
covers less than buck's `VIN` is a number that moved rather than a silence.

### `width_power` counts segments, and two of its three falls are re-segmentation

`width_power` is a KiCad `track_width` warning count — one hit per necked **segment** — so the same
copper cut into fewer pieces reads as an improvement (finding 19). Per net, against
`PCBC_PATTERNS=off`, in millimetres of copper under the class:

- **buck: genuine.** `5V` 9.68 → **0.00** mm, `VIN` 41.64 → **19.39** mm, `GND` 7.41 → 1.02 mm. The
  whole of `VIN`'s 0.127 mm copper is gone.
- **c3_usb: not the spine's width.** The one net it spined, `3V3`, had no necked copper before the
  slice and none after (0.00 → 0.00 mm). `VBUS`, which it refused, went 20.56 → **24.07** mm — worse
  — and `GND` 16.02 → 5.31 mm. The fall in the hit count is `VBUS`'s copper re-segmented and `GND`'s
  taps.
- **node: one segment.** `VBUS` 7.60 → 6.48 mm.

The millimetres are what `BOTTLENECK`'s `under_mm` pins, per net, so a fall that is only
re-segmentation cannot be reported as a width win again.

### The clearance question neither pcbc nor KiCad asks

`route_scene._pair_clashes` skips the copper and mask rules for a same-net pair by design, KiCad
exempts same-net pairs from clearance entirely, and KRT treats its own net as free — so nothing
anywhere looks at how close a net's own copper comes to itself (finding 16). `route_verify.same_net_slots`
counts it: same-net copper on one layer closer than the process floor, with the closest-approach line
sampled and any gap another piece of same-net copper fills discarded, which removes every mitre
corner and continuous run exactly. Built: blinky 0, buck 1, c3_usb 22, node 16, ds2 0, against
`PCBC_PATTERNS=off` buck 3, c3_usb 17, node 22. The class is pre-existing and both directions move,
so it is a census and not a rule; node's tightest is 0.050 mm between KRT's leftover and the locked
`VBUS` spine on a board whose floor is 0.0889 mm.

### The nits, and one number that did not reproduce

- `crowded`'s docstring cited exit distances no build produces (finding 22). Re-taken:
  `C_IN1.1 → U1.3` are 1.7160 mm apart with widest exits **1.3156 and 1.1266** mm, not 1.09 and 0.73.
  `C_IN2.1 → R_EN.1` is dropped from the list of pairs the centre line rescued — it measures roomy,
  gets six candidates rather than 115, and still fails with `no candidate` — and a test now asserts
  that, so the docstring cannot drift back to claiming it.
- Every tightened BAR ceiling sits at its measured value (finding 23). That is deliberate and now
  says so: KRT and KiCad are pinned to exact versions, the assertion is `<=`, so an improvement
  passes silently and only a regression fails. The two `DETOURS` risers the comment did not name —
  buck's `5V` 1.12 → 1.14 and c3_usb's `GND` 1.44 → 1.48 — are named.
- **One pinned number did not reproduce**: finding 11 gives `ipc2221_amps(0.532)` as 1.515 A and
  finding 4 gives 1.513 A for the same width. pcbc's own curve says **1.513**, and that is what the
  docs and the assertion carry.

## Power, in the build's own words

The S7 review's first finding was that `power_ampacity_failures` read each net's **widest** track, so
no neck could ever fail it; `ampacity.py` replaced that with the honest number — the widest-bottleneck
path between every pair of a net's pads, vias included — and found three of the five boards under
their declared current. That was the measurement. What it did **not** do was reach the build.

Measured on the finished boards before this change, with `PCBC_REQUIRE_KICAD=1 PCBC_REQUIRE_KRT=1`:

| board | net | declared | narrowest series copper | carries | under class |
|---|---|---|---|---|---|
| buck | `VIN` | 2.0 A | 0.3905 mm track, `C_IN1.1->R_EN.1` | 1.21 A | 19.39 mm |
| c3_usb | `VBUS` | 0.5 A | 0.1270 mm track | 0.54 A | 24.07 mm |
| node | `VBUS` | 1.0 A | one 0.2 mm via, 0.527 A | 0.74 A | 6.48 mm |

and the build that produced them said, in full: `ok: true`, `error: null`, route step
`{"copper": "verified"}`, fab step `{"error": null}`, exit **0**. The three rows lived in
`fab/report.json` under `ampacity_bottleneck` and as three lines of `FAB_NOTES.md`. An AI reading the
build's own output — which is the reader this tool is written for — would ship buck with 1.21 A of
copper on a 2 A rail and never see a word about it.

**`ampacity.power_moves` is the missing half.** `bottleneck_lines` stays the *ledger*: every power
net, passing or not, in `FAB_NOTES.md` for a person reading the electrics. `power_moves` is the
*move*: only the nets whose verdict is not `ok`, each naming the edits that fix it, carried out of
`fab_job` as `power_moves`, into the build's fab step as `power`, to the top of the build result as
`power`, and onto stderr by `pcbc build`. blinky prints nothing; buck, c3_usb and node print one line
each, verbatim on buck:

```
power: VIN: declared 2 A, carries 1.21 A through its narrowest 0.3905 mm track at 13.75,9.075 on
F.Cu (19.394 mm of this net is narrower than its 0.781 mm class; path C_IN1.1->R_EN.1). Place() the
parts either side of that copper closer together, or change amps= on the NetReq that already
declares "VIN"
```

Every clause of that sentence was wrong in the first version of it, and a 25-finding adversarial
review of the change is what found each one (`docs/power-moves-review.md` is the ledger: 25
findings raised, 15 confirmed by adversarial verifiers, all 15 fixed). What the line says now, and
why:

1. **It names the neck's coordinate, not the pad pair the walk ended on.** `where` is a pad pair,
   and every pair whose path crosses one neck ties at that neck's amps, so the reported pair is the
   first in sorted order among the tied ones: provenance, never a location. buck's is
   `C_IN1.1->R_EN.1` — `R_EN` is a 100 k enable pull-up drawing 0.12 mA — while its 2 A path is
   `J_IN.1->U1.3`. "Move those two parts closer" named two parts that are not on the rail, and both
   are already `Place(to=)`d to different pins of the regulator. `Bottleneck.at_mm` carries the
   narrowest piece's own midpoint and layer, and the pair stays on the line as `path ...`.
2. **Every edit is an edit to a line the board already has.** A second `NetReq("VIN", amps=...)` is
   refused twice before the board is drawn — `kind="generic"` takes no `amps=`, and the net is
   already named by another `NetReq` — and a second bare `Board()` raises at load. The move now says
   *change* `amps=` on the NetReq that declares the net, and *add* a tuple to the `Board(planes=...)`
   that exists.
3. **The pour clause appears only where pcbc actually pours.** `route.krt_plan` reads `planes=` only
   when `job.layers > 2` and writes the `GND` back pour itself below that. So on buck the first
   version's `Board(planes=[("VIN", "B.Cu")])` would have poured **no copper at all** — while
   `power_bottlenecks` marked `VIN` zoned on the strength of the declaration and stopped measuring
   it. The tool would have talked an author into silencing its own warning. The clause is now
   offered only for a free **inner** layer of a four-layer board: not on two layers at all, not on a
   layer another net already pours (node pours `GND` on `In1.Cu` and `3V3` on `In2.Cu`, and its
   `VBUS` move said `In1.Cu`), and not on an outer layer, where the parts are standing.
4. **`NetReq(amps=)` is left out where it cannot work.** `need_mm` is `max(IPC-2221, IPC-2152)` and
   is usually a curve value, but at the bottom `ipc2221_width_mm` clamps to `WIDTH_FLOOR_MM`
   (0.15 mm), and no current an author can declare moves a constant. c3_usb's `VBUS` is that case:
   it carries 0.536 A of its declared 0.5 A, so "declare what it carries" was a sentence arguing
   with itself, and taking it below 0.2 A would have narrowed the class pcbc writes from 0.4 mm to
   0.25 mm while the same line kept printing. That row now ends "No board.py edit widens it: the
   copper is the leftover router's and R3 owns its width", which is the true answer.
5. **An unmeasured net is not a shortfall.** `_walk` returns the row untouched for a net with fewer
   than two pads, leaving `carries` at `math.inf` and `width_mm` at 0.0 — and the floor test,
   `0.0 < need_mm`, was then always true. The ledger papered over it ("carries 0 A" among rows where
   every net appears); the move turned it into a claim about copper nobody measured, with an empty
   location, and `--strict-power` failed the build on it. `_verdict` now returns such a row `ok`.

**Built, all five, `pcbc build --force`, exit 0 on every one** (blinky and ds2 say nothing; the copy
of the DS2 Addon was built in a temp dir, never in the MaD checkout):

```
buck    power: VIN: declared 2 A, carries 1.21 A through its narrowest 0.3905 mm track at
        13.75,9.075 on F.Cu (19.394 mm ... 0.781 mm class; path C_IN1.1->R_EN.1). Place() the parts
        either side of that copper closer together, or change amps= on the NetReq that already
        declares "VIN"
c3_usb  power: VBUS: carries 0.536 A of 0.5 A on the curve but its narrowest 0.127 mm track at
        21.475,16.925 on F.Cu is under pcbc's 0.15 mm floor, which is a constant and not a curve
        point (24.068 mm ... 0.4 mm class; path C_VBUS.1->J1.A4B9). No board.py edit widens it: the
        copper is the leftover router's and R3 owns its width
node    power: VBUS: declared 1 A, carries 0.527 A through its narrowest single via at 27.8,36.3 on
        B.Cu/F.Cu/In1.Cu/In2.Cu (6.476 mm ... 0.4 mm class; path C_VBUS.1->J1.A4B9). Place() the
        parts either side of that copper closer together, or change amps= on the NetReq that
        already declares "VBUS"
```

Three boards, three shapes, and **no pour clause on any of them** — buck and c3_usb because two
layers pour nothing from `planes=`, node because both its inner layers already carry one. The clause
only appears on a four-layer board with an inner layer free, which is the only place it is true.

Three further things the same review changed, none of them in the sentence:

- **The exemption is the zone in the file, and nothing else.** `power_bottlenecks` used to read
  `census[net]["zone"] or net in job.planes`. The second half is a *predictive* exemption and it is
  the other half of finding 3 above: a declared plane that pours nothing stops the net being
  measured. Dropped. Every net that really pours is already a zone in the routed board, so no
  example's verdict moves.
- **A plane declaration the router will never honour is refused at the `Board()` line.** `planes=`
  on a two-layer board, and a layer the stackup does not have. Both were accepted silently, and the
  first was the trap the pour move would have walked an author into.
- **One spelling for every environment switch.** `PCBC_STRICT_PATTERNS` has always taken
  1/true/yes/on; `PCBC_STRICT_POWER` matched `"1"` exactly, so `PCBC_STRICT_POWER=true` was a silent
  no-op — the worst thing a strictness flag can be.

**It is a move and not a gate, by default.** `--strict-power` (`PCBC_STRICT_POWER=1`) makes
`fab_job` return it as the stage's error, exactly as `--strict-patterns` does for a pattern refusal.
The default is the move because the copper that necks is the router's leftover and R3's maze router
owns it: a gate that stops three of five example boards on a fault the tool cannot yet repair teaches
an author to reach for `--force`, and a build that is routinely forced is a build with no gates at
all. **This is the one call in the change that is the board owner's rather than the tool's**, and it
is one flag either way.

**What the first full run of the suite found, and it was mine.** The `--strict-power` test drove
`main(["build", ..., "--strict-power"])`, which set `PCBC_STRICT_POWER=1` on the **process** — the
way `--strict-patterns` already did — and pytest runs one process, so buck, c3_usb and node each
failed their own build hundreds of tests later with the move as their error. `cmd_build` now scopes
both flags to the single `build_job` call and puts the previous value back, and two tests pin that:
one that the flag is gone when the build returns, one that a value the caller set on purpose
survives. The leak was latent in `--strict-patterns` from S4 and nothing had called it twice in one
process; a user's second `main()` call would have hit it.

Pinned: `test_ampacity.py` (the three verdicts; where the pour clause is offered and where it is
not; a declared plane that never exempts an unpoured net; an unmeasured net; the coordinate rather
than the pair; a poured net; a row with no verdict key), `test_constraints.py` (the two `Board()`
refusals, with node's own declaration as the control), `test_cli.py` (stderr, the JSON, the flag,
the env spellings, that the flag does not outlive its build, that a value the caller set survives,
and silence when there is nothing to say), `test_examples_fab.py` (the move list equals `UNDER` per
board, buck's line verbatim, and `--strict-power` turning that same board's build into an error
while still writing the ledger it stopped on).

---

## S6 — chain, and the hard refusal that was standing in for a gate

The `chain` pattern shipped with one **hard** refusal — the only one in the repo — and a hard refusal
aborts the build before KRT runs (`route.py` lines 447 and 493). It fired on the DS2 Addon's
`Chain("VDDA", "J2.1", "C4.1", "U1.12")`, which is the *only* declared chain in the repo that is not
a pair, so the one board with the feature no longer built. This section is what happened when that
was measured instead of argued, and what replaced it. Everything below was re-derived on 2026-09-20
against the checked-in placed and routed boards; the DS2 Addon's tree was read and never written.

### The link the pattern cannot write, and why that part is correct

ds2's `VDDA` chain makes link 1 and refuses link 2. On the build's own path (`pre` hops, then the
fanout, then `mid`) link 1 is **14.3841 mm** over 5 pieces from `J2.1` into `C4.1`; on the
placed-board-only path the unit tests drive it is 14.5012 mm over 3. The geometry around link 2, off
the placed board after pre and fanout:

| item | box (mm) | note |
|---|---|---|
| `C4.1` | 19.880..20.780 x 5.966..6.916 | centre (20.330, 6.4411), F.Cu |
| `U1.12` | 22.6585..23.0015 x 8.4645..10.1955 | centre (22.830, 9.330), F.Cu |
| `C5.1 [3V3]` | 21.730..22.630 x 5.966..6.916 | between them, at `C4.1`'s own `y` |
| `C5.2 [GND]` | 23.280..24.180 x 5.966..6.916 | |
| `U1 lane top` | 20.0..25.0 x 7.1711..8.4645 | `fanout`'s reserved lane, F.Cu **and** B.Cu |
| `U1.12` escape | (22.83,9.33)->(22.83,8.05), via dia 0.5 | already down when the chain runs |

Exits: 3 from `C4.1`, 2 from `U1.12`. Airwire `C4.1 -> U1.12` = **3.8204 mm**. Five candidates reach
the clearance judge and every one is blocked by real copper with a real number — the table is in
`chain.ELBOWS`' docstring. **It is not an enumeration bug.** `link_candidates` builds every candidate
point out of `ax, ay, bx, by` and `elbows` puts its corner at `(bx, ay)` or `(ax, by)`, so all 96
candidate points across all 3 x 2 exit pairs lie inside the two exits' own bounding box
(x 20.3300..22.8300, y 5.6410..10.5206). The route this link needs leaves that box on **both** axes.
A fixed ~7-shape enumeration between pad exits structurally cannot express it, that is R3's maze
router's job, and refusing is the correct behaviour.

**Three things the refusal's own story got wrong**, all found by re-running it rather than reading it:

1. The `0.075 mm of air where the Power class needs 0.200` in `ELBOWS`' docstring is real but is
   **never printed**. `_link` keeps the worst clash by `need - have`, and that is a different `L-h*`
   candidate running along `y` 6.4411 straight *through* `C5.1`'s pad at **-0.350**. The move line is
   therefore derived from the wrong candidate too: it says `Place("C5", toward="up")`, while the near
   miss wants `C5` 0.125 mm to the **left**.
2. One candidate sits at exactly the limit — but only on a path pcbc does not run, and the review
   of the review caught that. Driving the mid stage **straight at the placed board** (no `pre`, no
   fanout), that candidate's worst clash is `U1.13` at a true gap of 0.20009999999999903 mm against
   `need + EPS_MM` = 0.2001: one ulp short, ~1e-15 mm, not a tie lost in a squared comparison.
   Re-measured on the **real** sequence (`pre` -> fanout -> `mid`), the fanout copper is down, link 1
   lands elsewhere, and the same net's clashes become 0.1192, 0.0750, -0.1399, -0.2856, -0.3321 and
   -0.3500 against 0.200. The closest is short by **0.0809 mm, eight hundred times `EPS_MM`**. So the
   claim "had it cleared, this chain would be complete" holds for the probe and **not for the build**.
   `EPS_MM` refuses nothing on the board pcbc actually produces, and is not shaved.
3. The "0.650 mm corridor between `C5.1` and `C5.2` that misses by `EPS_MM`" story, which the design
   notes carried, refuses nothing: `route_scene.pad_exits` only ever emits exits on a pad's own
   centre axes, so `x` 22.955 is not a point any candidate can contain. Anyone reading it as "it
   misses by a tenth of a micron" chases the wrong fix. The 0.0001 mm that *is* real is item 2.

### The outcome: the pattern is written and not registered

S6 set out to route chains. It ships the **check** and not the pattern, and a real build is what
decided that. `chain` is absent from `patterns.MID`; `chain.py` and its 17 tests stay whole and are
driven directly, so nothing rots.

Four configurations were built on ds2 — a board that builds today — and every one broke it:

| what the chain wrote on ds2 | outcome |
|---|---|
| the whole population, 51 seg / 98.6 mm | `GND` and `VSS` unrouted |
| implicit links bounded to `CHAIN_HOP_MM`, 18 seg / 30.8 mm | `GND` and `VSS` unrouted |
| the one declared chain only, 5 seg / 14.4 mm | copper **verified**, then the `GND` plane in 2 islands |
| every link bounded, 13 seg / 16.4 mm on three short local runs | `GND` and `VSS` unrouted |

**The last row is the one that decided it.** 16.4 mm of short, local, entirely legal copper on the
far side of a 47 x 25.4 mm board. None of it within 3.4 mm of the tap that fails; none of it on the
layer of the plane that splits. Locked copper moves KRT, and KRT's own copper closes the escape two
stages later. That is not a bound problem, and four bounds proved it: the failure moved rather than
went away. The other four boards agree in the smaller way — registered, `test_examples_fab` fails on
buck, c3_usb and node (node's `angles` 102 against a ceiling of 100), and only blinky is untouched.

`spine.WIDE_MM` is the precedent and it is the same board: S7 measured that ds2's 0.25 mm power nets
could not take a spine either, because locked copper takes the corridors its constrained nets need,
and excluded them rather than tuning until they passed. What this needs is a router that can search,
which is R3's.

**`CHAIN_HOP_MM` stayed anyway**, applied to every link, declared or not. `route_checks` already owns
the number with the sentence attached — "a chain hop the placement made: its corridor is checked; a
longer one is the router's" — and `hop.HOP_MM` exists for the same reason. A declaration says which
order the author wants; it does not give a fixed ~7-shape enumeration a search it does not have. On
ds2 that turns the declared `VDDA` into a distance refusal at link 1 (13.04 mm against 8), and the
refusal's own last clause is the architecture: "the run is the router's and the order is read back
off the finished board instead".

Built, all five, `pcbc build --force`, **exit 0 on every one**, with the new gate running:

| board | copper | chains checked |
|---|---|---|
| blinky | verified | none declared |
| buck | verified | none declared |
| c3_usb | verified | `USB_DP` spur, `USB_DN` spur |
| node | verified | `USB_DP` held, `USB_DN` spur |
| ds2 | verified | `VDDA` **held** |

Suite: **517 passed** with `PCBC_REQUIRE_KICAD=1 PCBC_REQUIRE_KRT=1`, from 492 before the slice.

**Three declared chains in this repo are violated on their own boards and nothing had ever looked.**
c3_usb's `USB_DP` and `USB_DN` and node's `USB_DN` route connector to module while the ESD part sits
on a spur off the run — c3_usb's `USB_DN` never comes within 2.58 mm of `U3.3`'s pad. They are pair
nets, so R4 owns that copper and the gate reports rather than stops (C.6's argument, the same one
`--strict-power` makes). They are real, they predate S6, and connectivity cannot see them: a netlist
gate is order-blind by construction, which is the whole reason R-X4 was specified with a **V** half.

### What KRT does instead, and the measurement that retired the hard refusal

The justification for `hard` was, verbatim from `Refusal.hard`: falling through to KRT "would produce
copper that violates the intent rather than merely a worse route, because KRT would branch". On the
checked-in routed board, KRT routes `C4.1 -> U1.12` the long way round — out to `x` 26.3, up the
right-hand edge and back left into the fanout via:

```
(20.35,6.45) -> (22.75,4.05) -> (25.8,4.05) -> [four 0.05 mm staircase jogs] -> (26.3,4.45)
             -> (26.3,6.9) -> (25.15,8.05) -> (22.85,8.05) -> (22.83,9.33)
```

**14.7663 mm over 11 segments and 10 direction changes** (six real corners; four are sub-0.08 mm grid
jogs) for a 3.8204 mm airwire — a **3.87x** detour. The whole net is 29.4483 mm of copper, 43
segments and one via.

And the order **holds**. The `VDDA` graph has three degree-1 vertices — `J2.1` at (8.9,12.7),
`C4.1`'s pad at (20.33,6.4411), `U1.12` at (22.83,9.33) — and exactly one degree-3 vertex, at
(20.35,6.45), which is **inside `C4.1`'s own pad copper**:

- 0.021891 mm from the pad centre (**not** the 12.6 um an earlier note claimed);
- **0.4300 mm inside** the roundrect outline;
- **1.1185 mm** of the trunk's centre line lies inside the pad's copper (0.5687 in, 0.5498 out).

So KRT did not branch away from the cap: it ran **one straight 45-degree trace across the cap's pad**
— the two through-edges leave that vertex at -45.00 and 135.00 degrees, exactly collinear — and
tacked on 21.9 um of bookkeeping copper to the pad centre. The declared order is in the board.

**The premise was not merely unproven; it was contradicted on the one board that could test it.** And
worse: the invariant the abort protected was checked **nowhere**. `docs/router-plan.md` line 202 tags
R-X4 "**P** (chain), **V**" and nothing in `route_verify.py`, `dru.py`, `copper.py`, `review.py`,
`check.py` or `build.py` read a declared order off a finished board. `verify_copper` is called once,
from `patterns/__init__.py`, on `plan.pieces` — pcbc's own copper, inside the pattern stage.
`netcheck.check_copper` is KiCad DRC plus netlist connectivity, which is order-blind by construction.
`route_checks.check_chains` is a placement check over pad centres and says **nothing** about this
chain at all, because it measures a straight centre-line corridor and this link runs at 49.1 degrees.
The build-aborting refusal was the only thing in the repo with an opinion about chain order, and it
had that opinion about **one net in five**.

### Three things the measurement does *not* say, recorded because they were nearly claimed

1. **"KRT does not branch" is false as a general claim.** Degree>=3 track junctions that are *not*
   inside a pad of their own net, counted on the checked-in routed boards: blinky 0, buck 4
   (all `GND`), c3_usb 12, node 17, **ds2 8** — and the c3_usb and node counts include `USB_DN` and
   `USB_DP` themselves, the declared chains. KRT tees mid-trace on multi-pad nets routinely, on the
   very board this argument is being made about. What ds2 shows is that on **this net** the branch
   landed inside the station's pad, which is an outcome and not a property.
2. **The routed board was produced without any chain copper.** Its steps are the pre-R2 chain
   (`00_fanout` ... `05_finalize`) and the only locked `VDDA` segment in it is the fanout escape, so
   KRT had a free hand over the net. Under a soft refusal pcbc hands KRT a **14.3841 mm locked link-1
   track** from `J2.1` into `C4.1` that KRT has never seen, and KRT could legally tee off the middle
   of it. Nothing measures that configuration, and this slice does not pretend to.
3. **Which is exactly why the answer is a verifier and not a different assumption.** The gate reads
   the board that is actually produced, whatever KRT does with whatever input it is given. That turns
   the question from "what will KRT do" into "what did KRT do", which is this project's own rule.

### The verifier: `route_verify.chain_order`

One verdict per declared `Chain()`, read off the routed board:

1. **Joined** — for each consecutive pair of the declared order there is a path through touching
   copper: every track, via and **pad** of the net, because a pad is copper and a run that lands on a
   third pad of the same net and leaves it is a path the board really has.
2. **Through, not past** — for each *intermediate* stop, removing that pad's copper **region** must
   disconnect its two neighbours. A stop still bypassed once its pad is gone was never in the path.

**One honest note on the citation.** R-X4's own text scopes itself to a *high-speed* net, while this
gate reads every declared `Chain()` — ds2's `VDDA` is Power class. The widening is deliberate: B.2's
rule is "the chain goes through its pads in order", and a bypass cap fed as a spur is the same defect
as a signal stub whatever the class. It is worth writing down because it cuts the other way too — the
invariant the hard refusal was enforcing was already broader than the rule it cited.

**Region, not node, and that is the whole design.** A graph cut-vertex reading of "the stop is in the
path" **fails ds2**: the two through-edges at (20.35,6.45) are collinear, so deleting the vertex
leaves `J2.1` connected to `U1.12` and `C4.1` reads as a leaf on a 21.9 um stub. Removing the pad's
copper instead cuts the trunk into two arms 1.12 mm apart with nothing between them, which is what
the copper physically is.

A track is cut where its centre line crosses the station's outline, found by ternary search plus two
bisections (`CHAIN_BISECT` = 64): the distance from a point to a convex hull is convex along a line,
so the interval within `d` of one pad primitive is single and closed. Both ends are taken from the
inside, so the check never removes more copper than the pad covers.

**The tolerance is `stackup.clearance_min`** — the pad is dilated by it before removal, so a junction
just *outside* a stop still counts as feeding through. It is `chain.stub_need`'s own number (S6
decision 1) and the one `route.py` hands KRT as `--same-net-pad-clearance`, on the argument that
copper closer together than a fab's minimum clearance is not two separable things. **It decides
nothing here**: swept 0.0 -> 2.0 mm, every one of the five declared chains keeps its verdict. The
nearest flip is node's `USB_DN` at 2.5 mm and c3_usb's at 3.0 mm — 28x and 24x their own process
floors (0.0889 and 0.127 mm).
The gap between the closest pass (0.43 mm *inside* a pad) and the closest fail is about 3 mm wide.

### What the gate found the moment it existed

| board | chain | verdict |
|---|---|---|
| ds2 | `VDDA` (`Chain line 97`) | **held** |
| c3_usb | `USB_DP` | held |
| c3_usb | `USB_DN` | **spur** — `U3.3` is off the feed |
| node | `USB_DP` | held |
| node | `USB_DN` | **spur** — `U3.4` is off the feed |

Identical on each board's `routed/` and `fab/` files. c3_usb's `J1.A7 -> U1.26` copper reaches
`U1.26` through the via at (17.45,19.45) and `U3.3`/`U3.4` hang off that via on a branch; removing
`U3.3`'s pad leaves the two ends joined. **Two of the four pair chains in this repo ship with their
declared order violated**, and nothing had ever said so.

**Fatal for the chains pcbc routes, reported for the ones B.2 hands away.** A `Chain()` on a
`PairSpec` net is skipped by the pattern with a printed note — "pairs are R4's" — so R2 writes none of
that copper and KRT writes all of it. Failing the build on it would stop two boards on a fault no part
of R2 can repair, which is `--strict-power`'s argument in C.6 and gets the same answer: it is a note,
loudly. Repairing them is a board change *plus* R4's pair router. What changed is that they are no
longer invisible.

### The escape hatch, and a field with no producer

`Refusal.hard` now has **no producer anywhere in the codebase**: `hop`, `spine`, `tap` and `chain` all
pass `hard=False`. The field and `hard_refusals()` stay wired, for two reasons that are written into
`Refusal`'s docstring rather than left to be rediscovered — the two cases the flag genuinely exists
for are real and unimplemented (`vias=False`, a single-layer net), and `--strict-patterns`
(`PCBC_STRICT_PATTERNS=1`) reads the same path to make every refusal fatal on demand.
`test_pattern_chain.py` asserts both halves: nothing is hard without the flag, and with it every
refusal on ds2 becomes one.

Pinned: `tests/test_route_verify_chains.py` (ds2 held on the board KRT actually routed; the junction's
0.4300 mm and 1.1185 mm; the synthetic tee that walks the trunk round the pad at 0.3050 mm clearance
and fails with its move verbatim; `_chain_gate` silent on the real board and stopping the build on the
mutated one, in `test_examples_fab.py`'s "a gate that cannot fail is not a gate" style; the four pair
chains as notes; the tolerance sweep and the two boards where it finally flips; a poured net and a
`Chain()` member that names no pad; determinism) and `tests/test_pattern_chain.py` (every chain
refusal soft, declared or not; `hard_refusals` empty without the flag and complete with it; ds2's
refusal still pinned verbatim, now soft).
