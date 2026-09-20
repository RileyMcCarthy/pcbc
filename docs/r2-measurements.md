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
