# The topological router: the plan

pcbc's own router, as a **gate graph**: a per-layer triangulation over obstacle hull vertices whose
edges are *channels* with exact capacity, searched with negotiated congestion, and embedded by
`route_relax`. Designed 2026-09-21 by a judged panel — a survey, four independent designs (SEARCH,
REPRESENTATION, COEXIST, and the relaxation half already shipped as `route_relax.py`), and one judge
who re-ran every load-bearing claim against fresh whole-board builds rather than taking them.

**Read `docs/quality-plan.md` section 5 first**, which struck `docs/router-plan.md` §6.4 (rasterised
octilinear maze A*) on a throughput census. This document is what replaces it.

## Preamble: what the measurements corrected

Five things in the four designs did not survive the judge's re-runs, and they are the reason this
plan is shaped the way it is rather than the way any one design proposed:

1. **`docs/quality-plan.md` slice 4's two levers do not close ds2's detour class.** `REFP_F` ships at
   66.19 mm against a 20.50 mm airwire — detour 3.23. Reordering the analog step fails the build in
   5 of 6 rotations; not locking the analog copper fails the same way; `--rip-existing-nets '*'
   --force-reroute` fails the same way; both together fail the same way. The hazard is not the lock
   and not the net order — it is that the copper is *there*, and `--keep-input-copper` means KRT
   routes around it. Detour is homotopy, and only phase A chooses homotopy.
2. **`(obstacle, side)` is not an invariant** and REPRESENTATION's instrument said it was. A
   per-obstacle instrument, immune to chain identity, finds the closest-leg cross product flipping
   on buck across `relax` 1/3/10 times at a 0.3/0.5/1.0 mm corridor, while the *winding* class
   changes zero times on the same board. A gate word is a crossing sequence and is invariant by
   construction; a side word is not. REPRESENTATION's conclusion was right and its definition was
   not the thing that proved it.
3. **Capacity does not come from `route_scene.free_intervals`**, as `docs/quality-plan.md` slice 5
   says it does. Verified in code: `_AXES = ("x","y","u","v")` **raises** on any other axis
   (`route_scene.py:936`) and `_project` projects through `it.box()`, an AABB. Capacity comes from
   the gate's two endpoints and `ClearanceTable.between`. Strike that phrase from slice 5.
4. **Delaunay edges are the right alphabet and vertex-index names are the wrong names.** Moving one
   part 0.5 mm keeps only 84.7 % of buck's Delaunay edge *names* (U1: 62.8 %). Under
   `(owner, corner)` naming the same alphabet survives at 97.1 % mean / 89.8 % worst on buck and
   99.2 % / 96.6 % on ds2 at the same move. It was a naming defect, not an architecture defect.
5. **Crossings, not capacity, are what slice 3 leaves behind.** buck/ds2/c3_usb/node produce
   26/96/328/648 crossing pairs and **0 irreducible** — every crossing pair shares a gate, so slot
   assignment *can* remove them, and that pass is slice 4's and is unwritten. Slice 3 must never be
   sold as a router.

### What slice 1 corrected when it was built

`src/pcbc/route_channel.py` shipped 2026-09-21. Five things in slice 1's own text did not survive
contact with the code, and all five are measured:

1. **The gate-clearance predicate (R6) ignores the offset radius as written.** Slice 1 spells it
   `hull_dist2(gate_hull, it.copper.pts) == 0.0`, and `hull_dist2` compares hull *points* — so that
   test misses every track and every via, a via being one point with a radius. The shipped predicate
   is `hull_dist2(gate, sh.pts) <= sh.r ** 2`, which is the exact "does this segment enter that
   copper" test and degenerates to the plan's for a zero-radius item.
2. **"A fifth of every board is wall" is an artefact of a worst-case clearance.** The prototype
   subtracts, at each gate end, the *largest* clearance anything on the board could owe that
   endpoint, and then counts how many tracks are left. That is a **lower** bound on capacity, so
   `0` means "not every net fits", not "nothing fits". Under the complementary bound — the
   *smallest* clearance `ClearanceTable.between` can return, which makes the count an upper bound and
   `0` a proof — **buck has 0 walls of 196 F.Cu channels and ds2 has 0 of 454**, where the prototype
   reports 31 and 28. The claim survives only on the two boards carrying a dense connector (c3_usb
   52 of 897, node 39 of 1107). `route_channel.census` ships **both**, labelled, and reproduces the
   plan's table exactly as the `holds` histogram.
3. **`slots` must divide by the smallest *class* clearance, not `stackup.clearance_min`.**
   `between` floors at the stackup and then takes a max over the two nets' classes, and a net with no
   `NetReq` gets Default — so nothing on buck is ever routed closer than 0.16 mm although the fab
   floor is 0.127. Using the floor over-counts on 8 of buck's 196 F.Cu channels.
4. **A bare drill is an obstacle and the prototype's vertex pass skips it.** An NPTH has
   `it.copper is None`, so a copper-only pass triangulates straight through a mounting hole. node and
   c3_usb each carry two on `J1`, and they are the whole of the +6 raw gates this module counts over
   the plan's table (node 1111 against 1105, c3_usb 897 against 891). buck and ds2 have none and
   reproduce the plan's raw counts exactly, 196 and 470.
5. **The straight line between two pads is not a placement oracle.** It was implemented first and
   fired a `Place()` move on **19 of buck's 21 airwires** — on a board that routes with zero
   unconnected items — because the straight line crosses whatever is in the way and a route goes
   around. Which channels a route crosses is its homotopy class, and that is slice 3. What ships
   instead is the **pad escape**: the channels incident on a pad's own corners are the only ways out
   of it, which is local, needs no homotopy, and is the question slice 1's own example line asks
   ("the narrowest channel `VBUS` must cross is 0.200 mm between `J1.B8` and `J1.A5`" is a channel
   between two pads of one connector). Measured: **0 sealed pads on all five boards**, which is the
   right answer for five boards that route.

One defect was found by the acceptance tests rather than by reasoning, and it is worth recording
because it is the class of bug this codebase's house rules exist to prevent: an AABB prefilter added
for speed compared `it.box()`'s `y0 = 0.30000000000000004` against a gate at `y = 0.3` and dropped the
one obstacle blocking ds2's top-edge channel. `it.box()` is raw float arithmetic. The prefilter is
gone; `Scene.query` plus an exact predicate now agrees with a brute-force scan of every item on the
board, on every gate of every layer of four boards.

---

## THE ANSWER

Build the topological router as a **gate graph**: a per-layer triangulation over obstacle hull
vertices whose edges are *channels* with exact capacity, searched with negotiated congestion, and
embedded by `route_relax`. SEARCH is the architecture. REPRESENTATION is right about one thing that
SEARCH got wrong (naming) and wrong about the thing it was most confident in (`side`). COEXIST found
a real flag worth one line and a composition rule whose mechanism it misdiagnosed — I retired its own
R1 and then found the rule is *stronger* than it claimed.

I re-ran the load-bearing claims. Nothing in `~/pcbc` or `~/Documents/MaD/Hardware/DS2Addon` was
modified during the design phase.

**The three measurements that decide it, all today, all through the real `route_job`:**

1. **ds2's one real detour is unreachable by every KRT lever.** `REFP_F` ships at 66.19 mm, detour
   **3.23**, against a 20.50 mm airwire. Reordering the analog step fails the build in **5 of 6**
   rotations (`3V3` at `R10.1`, `VSS`/`GND` unrouted) — reproduced through the *real* `route_job`
   with `patterns_post`/`patterns_final`, which is exactly what COEXIST could not verify. Then: **not
   locking** the analog copper — same failure. `--rip-existing-nets '*' --force-reroute` — same
   failure. Both together — same failure. On the shipped order, rip+unlock moves the whole board by
   0.4 mm (503.0 → 503.4). **`docs/quality-plan.md` slice 4's two levers do not close ds2's detour
   class.** The hazard is not the lock, and not the author: it is that the copper is *there*, and
   `--keep-input-copper` means KRT routes around it.

2. **Phase A puts `REFP_F` in the short class, in 0.09 s.** The SEARCH prototype on ds2 converges at
   **iteration 4, zero overflow, 0.09 s**, and its gate-midpoint realisation of `REFP_F` is
   **19.12 mm** against KRT's 66.19. Independently, COEXIST measured `REFP_F` routed *alone* at
   23.01 mm. Two methods agree the corridor is real and ~3x shorter than what ships. Phase A is
   shorter than KRT on **all 15** ds2 nets (`AIN2` 33.23 → 14.07, `GPIO0` 23.90 → 9.75, `ADC_RX`
   13.46 → 5.84). Honest caveat: 8 of 15 land *below* the airwire, which proves the midpoint
   polyline is a lower bound and not a route.

3. **The capacity rule is exact and the search is deterministic.** Re-ran `calib.py`: **0 false-full
   of 875 gates** the arbiter's own accepted routing crosses, across four boards and ten layers.
   Re-ran `pathfinder.py`: byte-identical signatures on repeat runs, all four boards
   (`86b67daed939a1a8`, `4a4022182808abaa`, `4cfc3ca9b66e0883`, `4837ffaa0b6cb8c0`). Gate counts,
   capacity histograms and CDT timings reproduce SEARCH's table exactly.

---

## 1. THE ARCHITECTURE

**A topology is a gate word.** Per (net, layer-run): the ordered sequence of gates it crosses, plus
two named pad terminals with their `Exit.side`, plus a `hop` naming the layer pair between runs.
**No coordinates.**

A gate is a channel between two obstacles, named **`((ownerA, cornerA), (ownerB, cornerB))`** —
`route_scene.Item.owner` plus the hull-corner index — never by vertex index.

**The layer is in the topology, the via is not** (REPRESENTATION §5, adopted verbatim).
`hop = (from, to)` ends a run; the via's coordinate is phase B's, free to slide, bounded by
`route_scene.antipad_clash`. v1 uses only via sites already on the board (fanout escapes + taps); v2
introduces one where planarisation reports a forced crossing — which is the honest answer to "where
do vias come from", and it is derived, not guessed.

**Phase B is `route_relax`, and I verified the property that makes it one.** Using a *true* homotopy
invariant — winding number of (chain + straight chord back) about each obstacle centre — the relaxer
changes **zero classes** on 75 chains whose endpoints it did not move (buck 16/16, ds2 59/59). That
is `docs/quality-plan.md` C1 as a property rather than a sentence.

**Composition with KRT: whole-board or not at all.** The topo step emits copper only for nets it
*completes*, KRT's `signals` step gets `!NET` for each (the `plan.done` mechanism at `route.py:364`
already exists), and a net phase A cannot complete gets **no pcbc copper at all**. Finding 1 above is
the measurement behind that rule: a partial homotopy choice frozen in front of KRT breaks the real
board, and no amount of unfreezing recovers it.

---

## 2. EVERY CONTRADICTION, RESOLVED

**C-A — The alphabet: obstacle names (REPRESENTATION) or triangulation edges (SEARCH)?**
REPRESENTATION's objection is true and I reproduced it: moving one part 0.5 mm destroys a large
fraction of Delaunay edge *names* — buck mean **84.7 %** kept, U1 **62.8 %**. It is a **naming**
defect, not an architecture defect. Under `(owner, corner)` naming the same alphabet survives at
**97.1 % mean / 89.8 % worst on buck and 99.2 % / 96.6 % on ds2** at the same move. Gates win,
because the gate sequence *is* what phase A emits — one object, no translation.

**C-B — Is `(obstacle, side)` well defined? REPRESENTATION says yes, 0 flips on 374 chains. It is
not.** Their instrument bags words into multisets and they hand-corrected a chain-reversal artefact.
A per-obstacle instrument, immune to chain identity, finds the closest-leg cross product flipping on
buck across `relax`: **0 flips at a 0.0-0.1 mm corridor (the word is empty), 1 at 0.3, 3 at 0.5, 10
at 1.0** — while the winding class changes **zero** times on the same board. Example: `VIN`/`R_EN.2`
flips `R -> L` with the distance unchanged at 0.618 mm; the *closest leg* moved, not the route.
**REPRESENTATION's conclusion is right and its definition is not the thing that proves it.** A gate
word is a crossing sequence and is invariant by construction.

**C-C — Does word length diagnose detour?** No, and REPRESENTATION says so honestly. Adopted: the
cost function is length *in the chosen class* plus congestion, never word length.

**C-D — Where does capacity come from?** Not `route_scene.free_intervals`. Verified in code:
`_AXES = ("x","y","u","v")` **raises** on any other axis (`route_scene.py:936`) and `_project`
projects through `it.box()`, an AABB. **Strike "from `route_scene.free_intervals`" from
`docs/quality-plan.md` slice 5.** Capacity comes from the gate's two endpoints and
`ClearanceTable.between`.

**C-E — Capacity or crossing?** Crossing. Reproduced exactly: **26 / 96 / 328 / 648** crossings
(buck/ds2/c3_usb/node) and **0 irreducible** — every crossing pair shares a gate, so slot assignment
*can* remove them, but that pass is unwritten and is the real work. Slice 3 must never be sold as a
router.

**C-F — Does reordering KRT open the detour class?** Partly, and it is bounded. Whole-board through
the real `route_job`, with the relaxer:

| buck | segs | mm | VIN detour | uncon | DRC err | `track_angle` | `seg_len` | vias |
|---|---|---|---|---|---|---|---|---|
| shipped (`mps`) | 73 | 147.4 | **2.51** | 0 | 0 | 20 | 7 | 7 |
| `--ordering original` | **71** | **132.9** | **1.83** | 0 | 0 | 22 | 8 | 8 |

-9.8 % copper, -27 % worst detour. That is `docs/quality-plan.md` **slice 4's target (<= 1.80)
reached by one flag**. On ds2 the same lever on the whole `signals` step wins 503.0 → 498.9 mm
(0.8 %) for +3 segments and +2 vias, and leaves `REFP_F` at 3.23. Ship it as a flag; do **not** build
the search harness (see §5).

**Re-measured on shipping 2026-09-21, and this table is a hair stale in a way worth naming.** Both
arms move by the `route_relax` joint floor that landed with slice 1 (a pad now carries
`clearance_min` of required depth, which costs ds2 one segment and 0.56 mm and c3_usb one accept):
buck now reads **72 / 133.06 / 1.84 / 22 / 8 / 8 vias** on the `original` arm against 73 / 147.37 /
2.51 / 20 / 7 / 7 on `mps`, and ds2 **503.55 → 499.58 mm** (-0.8 %) for +2 segments and +2 vias with
`REFP_F` unmoved. The conclusion is unchanged; the digits are.

**C-G — Is the hazard the freeze or the author?** Neither. It is the copper's *presence*. COEXIST's
rule is right in conclusion, wrong in mechanism, and the mechanism is what mattered: it is why
unfreezing cannot be the fix.

**C-H — Is `route_relax` the embedder?** Yes; SEARCH and REPRESENTATION agree in substance. Adopt
with three additions: `Holds` gains a `pinned` field (the Sketch *declares* `Exit.at`, so a trim may
not retract a terminal); `_hold2` degenerates to `sh.r**2` for a seed, and **REPRESENTATION's R4 is
correct that this must be written down** — the safety property the `max` provides on existing copper
is delivered by a different term in the seed path, and a reviewer checking the two call sites for
sameness will see an intentional mismatch; and the homotopy guard, whose cost is **measured zero on
existing copper and unmeasured from a seed**. Do not let "measured zero" migrate into the embedding
docstring.

**C-I — Via cost.** Unswept, and SEARCH says so. Carry `Constraint.via.count_max`
(`constraints.py:125`, 0 when `NetReq(vias=False)`) as a hard bound with a declared source; price the
via at the gate length it consumes; say the price is a guess. `Constraint.spacing_w`
(`constraints.py:195`, documented "a router cost in R3") is the parallel-run term and is **not** in
v1.

---

## 3. THE SLICES

### Slice 0 — `--ordering original` *(1 day)* — **SHIPPED 2026-09-21, as a declaration**
**Owns:** one line in `krt_plan` — an explicit `--nets` list in place of the `*` wildcard on
`signals`/`finalize`, plus `--ordering original`. Verified: KRT's `expand_net_patterns` preserves the
order of non-pattern names, and pcbc passes `--ordering` **nowhere** today, so it runs on
`DEFAULT_ORDERING_STRATEGY = "mps"` (`routing_defaults.py:212`).
**Must not touch:** net *selection*, any other step, the pinned `KRT_SHA`.
**Acceptance:** all four boards, no per-type DRC count above base, 0 new unconnected. Measured on buck
and ds2 only; c3_usb and node are the gate.
**Board: buck. Number: VIN detour 2.51 → 1.83, copper 147.4 → 132.9 mm.**
**Blocker:** `assert gate["soft"] == SOFT[name]` (`tests/test_examples_fab.py:793`) must become `<=`
first. This slice perturbs every board's counts.
**Cost if it fails c3_usb or node:** revert the line. Nothing downstream depends on it.

**It failed c3_usb, and then it failed buck.** The acceptance above — "all four boards, no per-type
DRC count above base" — is not met by a default: c3_usb's worst detour goes **1.69 → 1.73** with `EN`
alone at 1.04 → **1.73**, six more segments, `track_segment_length` 83 → 87 and `track_width`
25 → 28; blinky and node are **byte-identical**. So it became a per-board declaration,
`Board(net_order=)`.

**And then buck — the board it wins on — turned it down.** The -9.7 % of copper is bought by routing
the **2.0 A** `VIN` rail through **two layer changes it does not make under `mps`**, each on a
**single 0.3 mm via**: `route_verify.via_parallelism` reads **0.707 A carried against 3 needed** at
`(18.2, 8.45)` and `(18.25, 5.75)`, and `_barrel_gate` lists `VIN` as short on a board whose `short`
was empty. That is ampacity traded for detour. **No board in this repo declares a net order**, all
five are byte-identical to base, and the `SOFT` blocker below never had to be touched — nothing moved.

Two details the slice did not anticipate. The `--nets` half was **not needed**: the wildcard `*`
stays and `--ordering` alone carries the effect. And the **scope is measured**: the flag reaches the
`signals` and `finalize` steps only, because giving it to every `route.py` step makes the DS2 Addon
fail `kicad_drc` outright with two `items_not_allowed` errors and a `solder_mask_bridge` — where the
`signals`-only arm is identical to base on every violation type.

### Slice 1 — `route_channel.py`: the congestion map *(4-6 days)* — **the standalone slice**
**Owns:** triangulation, gates, exact capacity, and a report (`pcbc route --channels`, plus a
`channels` block in `copper.json`). ~300 lines on top of `route_geom` and `route_scene`. **No search,
no copper, no router.**
**Must not touch:** `route_geom.py`, `route_scene.py` (beyond reading), `krt_plan`'s steps, any KRT
command.
**Acceptance:**
- Euler's formula per layer.
- **The calibration test**, marked `kicad`+`krt`, board from `conftest.placed_board`: every segment
  of a routed board crosses only gates the model calls open — **0 of 875 today**. It fails the moment
  the capacity rule drifts.
- **The gate-clearance test** (SEARCH's R6, the one real correctness gap): the prototype is a plain
  Delaunay with same-owner edges filtered, so a gate from pad A's corner to pad C's corner may cross
  pad B. Fix it with pcbc's own exact predicate — reject a gate when
  `hull_dist2(gate_hull, it.copper.pts) == 0.0` for a third item found by `Scene.query`. A filter,
  not a constrained triangulation, and exact.
- The board outline enters as **four distinct items with four edges** (one shared owner disconnects
  the dual on near-empty layers).
- Byte-identical over two runs.

**Board: buck proves it; ds2 is the one that matters.**
**Number it must move: none on the board.** It must *print*, and reproduce: buck 196 F.Cu gates of
which **31 hold zero tracks and 37 hold exactly one**; ds2 470 of which **28 and 63**; c3_usb 891 of
which **181 and 100**; node 1105 of which **217 and 149**. Roughly a fifth of every board is wall and
another fifth is single-file, and **nothing in pcbc sees that today.** Runtime: 2.5 ms (buck F.Cu) to
19.4 ms (node F.Cu).
**If you stop here:** the user has their placement oracle. "The narrowest channel `VBUS` must cross
is 0.200 mm between `J1.B8` and `J1.A5`, and it needs 0.450" is a `Place()` move with a magnitude in
millimetres, computable from a *placed* board in 20 ms, with no router and no KRT run. That is what
`docs/quality-plan.md` section 6 asks for and it is worth shipping alone.

### Slice 2 — `route_topo.py::word_of` + the sketch in `copper.json` *(3-4 days)*
**Owns:** the reader only — the gate word of copper that already exists; a `"sketches"` key in
`route_emit.Sidecar` (`read_sidecar` already tolerates missing keys via `raw.get`, so old files load
with `[]`); a `RelaxResult` field.
**Must not touch:** relax's accept loop, any geometry, any step.
**Prerequisite it forces (REPRESENTATION's R3, confirmed):** `route_scene` names via items
`f"{net} via at ({x:g},{y:g})"` (`route_scene.py:339` and `:565`) — **a coordinate in a name**. A
cited via that moves renames itself and every word containing it silently changes. Add a stable id
beside the human label in `_vias`/`copper_items`; keep the label for `Item.label()` and the refusal
lines.
**Acceptance:** the word is byte-identical across `relax` on chains whose endpoints did not move —
measured **0 class changes, buck 16/16 and ds2 59/59**, by the winding test. Plus the blast-radius
table for free.
**Board: buck then ds2. Number: 0 word changes.**
**If you stop here:** `docs/quality-plan.md` C1 stops being a sentence, and a part move has a
published blast radius.

### Slice 3 — `route_search.py`: phase A as an oracle *(2-3 weeks)*
**Owns:** the dual graph, PathFinder negotiated congestion, integer costs, the declared net order,
and the refusal. Two real extensions over the prototype: **two layers with via arcs** (every headline
number today is F.Cu only) and a **Steiner tree** per net instead of a pad-id spanning path.
**Must not touch:** the routing path. No copper.
**Acceptance:** buck's 20-point anchor grid, false-accept **29 % → <= 10 %**; and on ds2, phase A
must place `REFP_F` in a class whose *embedded* length is <= 1.3x its airwire where KRT's is 3.23x.
**Board: buck (the grid), ds2 (the defect). Number: false-accept 29 % → <= 10 %.**
**If you stop here:** placement failure is per-channel arithmetic and the user's greedy loop has a
real objective. Still no copper.

### Slice 4 — planarisation, slot assignment, embed *(3-4 weeks)*
**Owns:** per-gate member ordering, per-face consistency, the packed realisation,
`pcbc_step("topo","topo")` **before** the KRT signal steps, `"topo"` added to `RELAXABLE`, `!NET` for
every net phase A completed, and the vias introduced at forced crossings.
**Must not touch:** any net phase A does not complete — **no partial copper, ever** (finding 1).
**Acceptance:** ds2 `REFP_F` detour **3.23 → <= 1.35**, 0 unconnected, no per-type DRC count above
base, byte-identical twice.
**Board: ds2. Number: REFP_F 3.23 → <= 1.35.** That is 43 mm, 9 % of the board.

---

## 4. DETERMINISM, CONCRETELY

Every place it could leak, and the fix:

| # | Leak | Fix |
|---|---|---|
| 1 | Vertex dedup dict | `Scene.items` is already sorted and ids assigned after the sort; read it as `sorted(by_pt)`, never iterate the dict |
| 2 | Triangulation insertion order | Coarse-grid boustrophedon keyed `(cell_y, +-cell_x, i)` — a pure function of the points; keep `i` as the final tie-break |
| 3 | Geometric predicates | Integer determinants, exact, no epsilon; every coordinate to nanometres exactly once |
| 4 | Gate ids | `sorted(gkeys)` then index — and under this plan sort by `((owner,corner),(owner,corner))`, a total order on strings and ints, **never on floats** |
| 5 | Dual adjacency | `for a in adj: a.sort()` |
| 6 | Dijkstra queue | `(cost_nm, gate_id, prev_gate_id)` — integers only; never push a float |
| 7 | Occupancy set | Memo key `(gate, tuple(sorted(members)), net)`; `demand` sorts before reading; a set is only ever an O(1) membership test |
| 8 | **The accept test** | `demand > usable + 1e-9` is the one float comparison that *decides*. **Move it to integer nanometres** — `toll`/`room` are already precomputed in nm; finish the job. This is a real defect in the prototype |
| 9 | Net / link order | Declared, not emergent: `(0 if via.count_max == 0 else 1, 0 if len(layers) == 1 else 1, -len(keep_away), -width_mm, net, pad_a.id, pad_b.id)` — every term from `board.py` |
| 10 | Termination | `_ITER_CAP = 30`, a `for` not a `while`; `hist` monotone non-decreasing. Weaker than `route_relax.quality`'s monotone potential — **write that down rather than inheriting it** |
| 11 | Embedding | `route_relax`'s existing four: fixed chain order, fixed scan order, first-accept, one `q` |
| 12 | Serialisation | Sketch order is `chains_of`'s (`sorted(groups)` then `sorted(adj)`); no floats except `width`, already rounded to 4 dp in the chain key |

Verified: two runs of the search produce identical signatures on all four boards, and two *different
implementations* of the same capacity rule produce the same signatures — stronger than repeat-runs,
because it says the answer is a function of the rule and not of the evaluation order.

---

## 5. WHAT I WOULD NOT BUILD

- **COEXIST's `route_order.py` rotation-search harness.** Measured: buck's five rotations produce
  **two** distinct boards — `VIN`-first reproduces `mps` exactly and the other four are byte-identical
  to each other. It is not a search, it is a flag (slice 0). On ds2 the whole-step reorder buys 0.8 %
  for +3 segments and +2 vias; on the subset it fails the build. A module, a scratch-build loop,
  `Q_order`, and 25-95 s per build for that.
- **`--guide-corridor` as the topology→KRT channel.** COEXIST measured it global rather than per-net,
  needing a 0.6 mm clear square, and orphaning a net in violation of its own docstring. Record it in
  `docs/router-plan.md` beside the struck §6.4 or it will be tried again.
- **`docs/quality-plan.md` slice 4 as a *detour* lever.** Three variants measured today — unlock,
  whole-board rip-up, both — and none touches ds2's `REFP_F`. Keep the slice if you want it for other
  reasons; delete its detour claim.
- **A stored `(obstacle, side)` word under the closest-leg definition.** Not invariant (C-B).
- **A rubber-band sketch with anchor points.** An anchor is a coordinate; it cannot survive a part
  move by construction.
- **The board-spanning cut oracle.** COEXIST measured 10.3 % peak utilisation on placed ds2 — the
  densest board. It does not discriminate.
- **A grid maze router.** Already struck; the throughput census makes it arithmetic.
- **A dependency for the triangulation.** `~/pcbc/.venv` has no numpy and no scipy, and
  `pyproject.toml` declares none. 114 lines of pure-Python Bowyer-Watson with exact integer
  predicates runs the largest board in **19.4 ms**. Keep the project's dependency posture.

---

## 6. HONEST COST, AND WHAT YOU GET IF YOU STOP

| slice | effort | if you stop here |
|---|---|---|
| **0. `--ordering original`** *(shipped 2026-09-21 as `Board(net_order=)`)* | **1 day** | buck -9.7 % copper and worst detour **2.51 → 1.84**, 0 unconnected, 0 DRC errors, and it runs *faster*. Reaches slice 4's stated target with one line. Costs: `track_angle` 20 → 22, `seg_len` 7 → 8, one extra via, `deg/mm` 18.32 → **20.45** — a better homotopy is shape-worse even after relaxation, so judge it on detour and connectivity, never on shape. **It is not general and it is not safe**: c3_usb is worse (1.69 → 1.73, `EN` 1.04 → 1.73), blinky and node are byte-identical, and buck pays for its win with **two under-rated vias on the 2 A `VIN` rail** (0.707 A carried against 3 needed, `via_parallelism`). Shipped as `Board(net_order=)` with **no declarer**; nothing moved. |
| **1. congestion map** | **4-6 days** | **The placement oracle.** A per-channel supply-vs-demand number on a *placed* board in 20 ms, a `Place()` move in millimetres, and the narrowest channel on every board named pad-to-pad. A fifth of each board is wall and a fifth is single-file, and pcbc cannot see either today. No copper changes. **This is the best stopping point and I would ship it alone.** |
| **2. sketch reader** | **3-4 days** | Plus: C1 executable, a published blast radius per part, and the via-naming defect fixed before anything depends on it. |
| **3. phase-A oracle** | **2-3 weeks** | Plus: false-accept 29 % → <= 10 %, and "the board is full" distinguished from "try harder" with a magnitude. Still no copper — do not let buck's and ds2's convergence be read as "phase A routes these boards". |
| **4. planarise + embed** | **3-4 weeks** | Plus: the only thing that closes ds2's `REFP_F` — 3.23 → <= 1.35, 43 mm, 9 % of the user's real board. |

**Total to the oracle: about five weeks. Total to copper: about nine.** The first six days are worth
taking whatever happens next.

---

## 7. WHERE I AM STILL GUESSING

- **Slice 0 is measured on buck and ds2 only.** c3_usb and node are unmeasured and they carry
  differential pairs, which `_skip_nets` already excludes for the same reason slice 0 should.
- **The phase-A/KRT length comparison is a proxy.** The gate-midpoint polyline lands below the
  airwire on 8 of 15 ds2 nets, so it is a lower bound on the class, not a route. What it establishes
  is *relative*: KRT's class for `REFP_F` is 3.23x the airwire and phase A's is near 1. The absolute
  number is slice 4's to produce.
- **Everything in the search prototype is single-layer, via-free, and uses a pad-id spanning path
  rather than a Steiner tree.** Adding B.Cu roughly doubles the gate graph and adds via arcs.
  Unmeasured.
- **The 0/875 calibration proves the model never refuses what the arbiter accepted.** It does not
  prove tightness — a model that accepted everything would score the same. The complement is the
  capacity histogram and slice 3's false-accept number.
- **The homotopy guard's fire rate from a seed is unmeasured**, and the regime it was measured in
  (KRT's loose copper, where the word at zero slack is empty) is the wrong one.
- **The via price is a guess.** Every other term is a length the board already carries.
- **Zones and multi-terminal nets are out of scope and stay out.** `Scene` has no zone kind;
  `route_relax.Holds` already carries that hole and a gate word inherits it. A star net is several
  runs and *which* pad feeds which lives only in `route_verify.chain_order`.
