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
   and that is arithmetic and not luck: it needs a share in (0.527, 0.871] A on four layers, and no
   board's amps over a whole number of pads lands there (buck's 2 A over 2 pads is 1.0, over 3 is
   0.667). The rule is swept over every pad count from 1 to 119 on every plane net of all five
   boards, so a board that does reach it is not a surprise.
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

**c3_usb: `micro` 168 → 204, `segments` 377 → 453, `routed mm` 312.9 → 355.8, and the soft
`width_power` 36 → 49.** Thirty-four new locked vias sit between the connector and the MCU, and KRT
answers by staircasing the leftover `GND` and `VBUS` around them. Against that: `vias_leftover` 9 →
8, the leftover share 95.2 % → 86.3 %, and the board's ground is now welded pad by pad to the back
pour instead of relying on whatever KRT could reach. F.3 item 7 says soft-rule hits must not rise, and
`width_power` does; it is in the open issues.

**buck: `micro` 26 → 37 and `segments` 96 → 114**, against `vias_leftover` 4 → **1**, off45 3 → 2,
142.5 → **139.4 mm** and `width_power` 40 → 39. Six taps on a board with eight ground pads, and the
same staircase answer at a much smaller scale.

**node: the soft `width_usb` 57 → 58.** One more necked segment on the USB pair, which R2 does not
touch and R4 owns. Its `width_power` falls 44 → **20** in the same build, because the taps carry GND
and 3V3 to their planes at the class width and KRT has far less power copper left to neck.

### What is not measured here

`bus`, `guard` and `stitch` still read zero on every board (S8). The decap loop-area note D.5 asks the
tap stage for is **not** written: `route_checks._loop_area` needs the IC-and-cap pairing its own
checker builds, and reproducing that inside a pattern for a `style:` line is work S5 did not do — the
loop is still reported by `check_decoupling` where it always was. A.4 rule 4 (the mask dam) is still
advisory and still counts zero: a tap's mask opening is its own ring, and no tap came within 0.10 mm
of a foreign one. The leftover share does **not** reach F.3 item 5's 70 % on any board, and it was
never going to at S5: the census says a hop is 5.7 % of the copper and a plane tap 12 %, so 82 % is
the arithmetic. The 45 % target needs the spine (37 %), which is S7.

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
