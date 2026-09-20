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
