# Roadmap

What pcbc is, measured, and what stands between it and a board like
[OpenESC-20x20](https://github.com/OpenDrone-hw/OpenESC-20x20) — 6 layers, 1.6 mm, 2 oz copper,
31.2 x 33.0 mm, **40 A continuous per channel** across four independent channels, four MCUs,
twenty-four MOSFETs, current sensing to 165 A. That board is the benchmark because it is real, it is
public, it is KiCad 10, and its author already documents the design methodology the way pcbc assumes.

Everything below is measured on the five boards (blinky, buck, c3_usb, node, and the DS2 Addon) on
2026-09-21 unless it says otherwise. Sizes are honest and include the verification, not just the code.

---

## Where it is

| | measured |
|---|---|
| suite, clean tree, no build output | **677 passed** |
| suite, KiCad + KRT | **764 passed** |
| turning, buck | 50.91 -> **18.32** deg/mm (hand-routed DS2 Addon: **17.2**) |
| turning, ds2 | 23.53 -> **14.74** deg/mm |
| segments under 0.2 mm, buck | 96 -> **7** |
| worst detour, ds2 `REFP_F` | **3.23**, and neither relaxation nor net order touches it |
| pair skew, node, against a 0.5 mm budget | **11.92 mm** — 23.8x over, and pcbc does not own it |
| share of copper the external router draws | **80-92 %** |
| congestion map | **60 ms** on c3_usb and **15 ms** on buck, no router, no KiCad; 0 sealed pads of 82 and of 28 |

Every routing number here is re-measured 2026-09-21 on fresh builds of all five boards, the relaxer
on and off. The base/relaxed table in item 0 is the source; `deg/mm` counts turning at every degree-2
vertex of one net on one layer at one width, so a branch at a via is not a corner — the reading
`route_verify.paths_of` takes and **not** the one `docs/quality-plan.md` section 1 used, which is
why ds2 reads 23.53 -> 14.74 here where that table reads 24.1 -> 12.1.

**The shape gap is closed and the detour gap is not.** Pulling copper taut reaches hand quality on
turning; it provably cannot change which side of an obstacle a route passes, which is what detour is.

---

## 0. Land what is built  *(days)* — **done 2026-09-21**

`route_relax.py` (the embedder) and `route_channel.py` (the congestion map) are in, with
`conftest.routed_board`, `pcbc route --channels`, and the `relax` step last in `krt_plan`.

**What the relaxer does, base vs shipped, on fresh builds of all five boards** (the relaxer disabled
by one line in `route_job` is the base; deg/mm counts turning at every degree-2 vertex of one net,
one layer, one width):

| | segs | mm | micro | off45 | corners/mm | deg/mm |
|---|---|---|---|---|---|---|
| buck | 175 -> **73** | 149.33 -> 147.37 | 96 -> **7** | 1 -> **0** | 1.031 -> **0.346** | 50.91 -> **18.32** |
| ds2 | 315 -> **228** | 508.26 -> 503.55 | 96 -> **32** | 12 -> **7** | 0.470 -> **0.302** | 23.53 -> **14.74** |
| c3_usb | 444 -> **302** | 361.16 -> 358.88 | 188 -> **85** | 14 -> **12** | 0.839 -> **0.479** | 49.59 -> **33.09** |
| node | 314 -> **275** | 411.72 -> 409.55 | 54 -> **28** | 25 -> **18** | 0.442 -> **0.352** | 35.92 -> **28.61** |
| blinky | 6 -> **4** | 23.63 -> 23.31 | 0 | 0 | 0.169 -> **0.086** | 7.62 -> **3.86** |

**The arbiter, by violation type, base vs shipped, one `kicad_drc` run per arm per board: zero errors,
zero unconnected, and no type above base on any board.** Only three types move and all three fall —
`track_segment_length` (buck 95 -> 7, c3_usb 185 -> 83, node 54 -> 28, ds2 94 -> 32), `track_angle`
(28 -> 20, 130 -> 116, 102 -> 96, 14 -> 8) and `track_width` (23 -> 8, 40 -> 25, 60 -> 58, 3 -> 3).
Plane islands unchanged; plane area moves only on c3_usb (+0.04 mm²) and node (−0.01). **Three**
independent builds of every board are byte-identical once the build directory's own path in the
`(model ...)` lines is normalised. `orphan_copper` is **0 on all 64 nets** of all five boards on both
arms — millimetres of centreline, not items, so tidying cannot buy the guard slack.

One defect found while landing and fixed: `Holds` guaranteed the centreline stays in a pad's copper
but **not** that a joint stops getting shallower, and a `trim` cut three joints below their board's
own `clearance_min` (c3_usb's `C_EN.2` 0.2700 -> 0.0760 mm, ds2's `R5.2` 0.5075 -> 0.1175). A pad now
carries `clearance_min` of required depth; it costs blinky/buck/node nothing, c3_usb 0.10 deg/mm and
ds2 0.07. **The deepest pad overlap still falls on 25 of 302 pads and that is by design** — a `trim`
drops the leg that runs to a pad's centre and what is left joins the pad by clipping it — and the
floor bounds how far only where a pad has a rounded outline to spend it out of. The two joints left
under a board's `clearance_min` are c3_usb's `U1.23` (0.2 -> 0.1 mm) and `J1.A7` (0.1, untouched),
both **square** pads, whose `Shape` carries `r = 0`: `hull_dist2` is 0 anywhere inside a rectangle,
so the strongest bound expressible is "the centreline is in the outline" and a run may still slide
along the edge. A signed depth would fix it and nothing needs one yet.

Two "measured wins" rode along. **Neither survived being built, and finding that out is the whole of
item 0.5.**

---

## 0.5. The two ride-alongs  *(a day)* — **done 2026-09-21, and both declined**

Both were written down as "measured, small, blocked on nothing". Built, one turns out to trade
ampacity for detour and the other to be a misreading of what its number counts. **Nothing on the
five boards moved by a millimetre and that is the deliverable**: two plausible improvements are now
measured, refuted, and cannot be proposed again without new evidence.

### The net order: a declaration that no board declares, and the reason is ampacity

`Board(net_order=)` ships, taking KRT's four `--ordering` names, refused at declaration time against
a list rather than at step 4 of a build. It reaches **the `signals` and `finalize` steps only**, and
that scope is a measurement: given to every `route.py` step instead, the DS2 Addon stops being a
board — `kicad_drc` returns two `items_not_allowed` errors and a `solder_mask_bridge` — where the
same board with the flag on `signals` alone is identical to base on every violation type. The
constrained steps route on an otherwise empty board and have no congestion for an order to negotiate.

**`"original"` against KRT's default `"mps"`, fresh builds, all five boards, 0 errors and 0
unconnected on every arm:**

| | copper | worst detour | segs | vias | deg/mm | DRC by type |
|---|---|---|---|---|---|---|
| **buck** | 147.37 -> **133.06 mm (-9.7 %)** | `VIN` 2.51 -> **1.84** | 73 -> 72 | 7 -> **8** | 18.32 -> **20.45** | `track_angle` 20 -> 22, `track_segment_length` 7 -> 8, `track_width` 8 -> 7 |
| ds2 | 503.55 -> **499.58 mm** (-0.8 %) | `REFP_F` 3.23, **unmoved** | 228 -> 230 | 28 -> 30 | — | **identical**, 114 warnings both arms |
| c3_usb | 358.80 -> 358.93 (+0.0 %) | 1.69 -> **1.73** (`EN` 1.04 -> **1.73**) | 302 -> 308 | 55 | — | `track_segment_length` 83 -> 87, `track_width` 25 -> 28, `track_angle` 116 -> 112 |
| node | **byte-identical** | | | | | |
| blinky | **byte-identical** | | | | | |

One board of five wins, one loses, two do not move and one is marginal. So it is a declaration. And
then **the one board it wins on turned it down**, which is the result worth keeping:

> **buck's -9.7 % of copper is bought with two under-rated vias on a 2 A rail.** `VIN` makes **two**
> layer changes it does not make under `mps`, each on a **single 0.3 mm via**;
> `route_verify.via_parallelism` reads **0.707 A carried against 3 vias needed** at `(18.2, 8.45)`
> and `(18.25, 5.75)`, and `_barrel_gate` lists `VIN` as short on a board where `short` was empty.
> Base buck changes layer on that rail **not at all**.

That is not shape traded for detour; it is **ampacity** traded for detour, and a 2 A rail does not
make that trade. So `net_order` ships as a mechanism with a refusal and a measured scope, **no board
in this repo declares one**, all five stay byte-identical to base, and not one recorded number moves.

The tool caught this, not a reviewer: `via_parallelism` and `_barrel_gate` are the "unusually good
verification layer" this roadmap already credits, and four `test_route_verify_stitch.py` failures
were the whole of the evidence. Had `net_order` shipped as the **default** the plan asked for, buck
would have carried two under-rated vias and those four numbers would have been re-recorded around
them — a recorded ceiling raised to accommodate a regression is the one failure mode this repo's
whole discipline exists to prevent, and it came within one judgement call of happening.

### The differential-pair leak: the defect is real, the four-line fix is refuted

**The headline number was wrong.** "c3_usb skew 2.190 mm against 0.5, 4.4x over" is a `copper_bar`
`routed_mm` *difference*, and the two nets do not carry the same number of pads. **KiCad — the
arbiter — reads c3_usb at 0.6154 mm against 0.5**, 1.23x over. The board with the real defect is
**node, which nobody measured: 11.9196 mm, 23.8x over**, with `diff_pair_uncoupled_length_too_long`
at **22.81 mm against a 2.0 mm budget**.

All four of the planned edits were built and measured, and **three of the four do nothing or harm**:

| edit | measured, fresh builds of c3_usb and node |
|---|---|
| `!pair` in `signals` | leaves **3 pads open on c3_usb and 6 on node** — neither board builds. c3_usb's skew gets *worse* (0.62 -> 4.86); node's gets much better (11.92 -> **1.32**, uncoupled 22.81 -> **4.60**) on a board that no longer exists |
| `*cn.layers` on the pair step | **exact no-op** on both. `cn.layers` is already `F.Cu, B.Cu`, and node's `In1/In2` are planes the pair router never used |
| `--max-ripup 5` on the pair step | **exact no-op** on both, byte for byte |
| an unfinished pair as a refusal | **shipped** — the only one that survived. A `diff_pair` branch of `route._unrouted_move`: no pair on these five boards fires it today, and its move had to be checked against the compiler before it could be written, because **`pair=False` is not the edit** — the `usb_hs` preset carries `autoroute="diff_pair"` and `constraints.py:1860` can only turn it *on*. `autoroute=True` is the override the preset loses to. |

Two further arrangements were tried and rejected: **locking the pair's copper** after its own step
(byte-identical on c3_usb, and node 11.92 -> 12.09 — so `signals` is *adding* to a pair, not ripping
it), and a **dedicated `pair_stubs` step** right after the pair step on a near-empty board (c3_usb
0.62 -> **8.04**, node 11.92 -> **23.40**; both worse than `signals`).

**Why, and it is the finding:** a USB-C pair net here is **not a two-terminal net**. c3_usb's
`USB_DP` has five pads — `J1.A6, J1.B6, U1.27, U3.1, U3.6` — because a USB-C connector carries both
orientations of the signal and an ESD array has two sides. `route_diff.py` routes **one coupled path
per net** and nothing else, so those extra pads were never its job, and the wildcard `signals` step
is the only thing that reaches them. Excluding pairs from it does not stop a leak; it stops the pair
being connected. And on node the copper that reaches them is 19.5 mm long, which *is* the skew.

So this is not a bug in a step list. It is **item 6**: pairs are the external router's entirely, and
ownership is the feature. The bounded next move is named there.

---

## 1. Hierarchy  *(2-3 weeks)* — the one that multiplies everything else

**Nothing in `language.py` has it.** Four identical motor channels means writing every part, every
`NetReq`, every `Place()` and every `Chain()` four times, with four sets of unique references. The
source file grows linearly with the copy count and every constraint has to be restated per copy.

This is the gap that gets **worse** as boards get more complex rather than better, and it is the
reason the benchmark board is impractical to describe today even ignoring every other limit.

Owns: a block declaration, instantiation with a reference prefix and a net-name mapping, and how a
block's internal constraints compose with the board's. Proves it: a fixture with four instances whose
copper is identical under translation.

---

## 2. The capability limits  *(1-2 weeks)* — data and validation, not algorithms

Three lines and one table stand between pcbc and an entire class of real board:

- **Layers.** Only 2 and 4 exist. `copper_layers` generates inner names correctly, but every
  stackup, impedance solve and plane rule assumes one of the two shipped stackups. The benchmark is 6.
- **Copper weight.** Every stackup is 1 oz (0.035 mm). At 2 oz every current-carrying width halves
  and every clearance and impedance number moves. This is **fab data**, and it must be real data
  rather than scaled guesses.
- **Current.** `NetReq(amps=)` refuses anything over **30 A** (`language.py`, `hi=30.0`). The
  benchmark is 40 A per channel. Raising the limit is one argument; proving the IPC-2221/2152
  arithmetic and the via-ampacity curve still hold there is the actual work.
- **Outline.** Rectangles only (`seed.py` writes four `Edge.Cuts` lines). Real boards have cutouts,
  castellations and mounting geometry.

Cheap relative to their effect, and each is measurable against a stackup or a standard.

---

## 3. The topological search  *(4-6 weeks)* — the detour gap

Designed (`docs/topo-plan.md`), not built. A per-layer triangulation over obstacle hull vertices
whose edges are channels with exact capacity, searched by negotiated congestion, **embedded by
`route_relax`** — which is why the embedder was built first and why it is verified to preserve
homotopy class (winding number, **zero** changes across 75 chains).

The evidence that it is necessary rather than merely nicer, all measured through the real pipeline:
- ds2's `REFP_F` ships at **66.19 mm, detour 3.23**, against a 20.50 mm airwire.
- **Every lever the external router has was tried**: reordering the analog step fails the build in
  **5 of 6** rotations; not locking the copper, full rip-up and reroute, and both together all fail
  the same way. **None** of them move the 3.23.
- Phase A finds that net a corridor at **19.12 mm**, converging in **0.09 s** with zero overflow, and
  a second independent method routing it alone measured **23.01 mm**.

Slice 1 (the congestion map) is **built**. It is also the placement oracle and the referee for
board-size compaction — all three requests resolve to one artifact.

---

## 4. The placement loop  *(1-2 weeks)*

Placement stays **greedy and manual** by decision: one pass, file order, no search, and the score
knows nothing about routing. The router feeds back and an agent moves parts.

What is missing is attribution. Today nothing turns a routing defect into a placement cause: ds2
routes a net at 3.2x its straight line and the build says `ok`. The work is tracing every detour,
staircase, leftover via and unroutable pad to a geometric cause, naming the part and the direction,
and stating plainly whether the placement is nominal.

Two things it needs: a **cheap loop** (re-route from the placed board without re-seeding, redrawing
the schematic or re-fabbing) and **one number that moves monotonically** so the next edit can be
judged against the last.

---

## 5. Shaped copper as a conductor  *(3-4 weeks, least certain)*

A 40 A phase output is a **polygon**, not a track. `patterns/spine.py` writes a trunk and ribs at a
class width; nothing shapes conductive copper as a pour with derived thermal relief, stitching and
clearance.

**This is the item I have least confidence in.** Everything else on this list is a known technique
with a clear shape. This one needs a design panel before an estimate is worth anything.

---

## 6. Differential pairs, length matching, layer assignment  *(3-4 weeks)*

- **Pairs are the external router's entirely, and item 0.5 measured what that costs.** KiCad reads
  node's `USB_DP`/`USB_DN` skew at **11.9196 mm against a 0.5 mm budget** and its uncoupled length at
  **22.81 mm against 2.0**; c3_usb is 0.6154 against 0.5. `route_diff.py` routes one coupled path per
  net, so a pair net's *other* pads — a USB-C connector's flip-side `B6`/`B7`, an ESD array's second
  side; five pads on c3_usb's `USB_DP` — are reached by the wildcard `signals` step as ordinary
  copper, and on node that copper is **19.5 mm** and is the whole of the skew. Four rearrangements of
  the existing steps were measured and all are worse or leave pads open (item 0.5). **The bounded
  next move is to give those pads pcbc's own `hop` copper before KRT runs**, so the run that reaches
  `J1.B7` is the 2 mm one a hand router would draw and `signals` has nothing left to add; then, and
  only then, excluding pairs from `signals` is safe. Ownership is the feature.
- **Length matching** with tuning (serpentines) does not exist. `match_mm` compiles and nothing
  routes to it.
- **Layer assignment is a declaration, not a decision.** On six layers, which layer a net lives on is
  a real choice and `NetReq(layers=)` fixes it by hand.

---

## 7. Manufacturing  *(2-3 weeks)*

Thin: no teardrops (R-M3, deferred), no panelisation, no test-point strategy, no assembly checks
beyond the via-in-pad rule. All well-understood, none blocking, all needed before a board is ordered
in volume.

---

## What is already strong

Worth knowing where **not** to spend:

- **The constraint compiler.** Widths from IPC-2221 and IPC-2152, clearances from IPC-2221B Table 6-1
  and IEC 60664-1, impedance from Hammerstad-Jensen as KiCad itself implements it, all from real fab
  data with the extrapolation ranges stated.
- **The verification layer**, which is unusually good: rule checking and netlist by KiCad itself,
  plane islands and area, declared feed order read off finished copper, current-carrying by
  **narrowest series copper** rather than widest track, same-net slots, via-in-pad by true pad
  copper, ground bridges, and per-board recorded ceilings that fail on a regression.
- **Stitching**, six techniques, every number derived from the stackup.
- **The geometry core**: exact, grid-free, integer-nanometre predicates — the right substrate for
  topology, and the reason a grid maze router was struck from the plan.

---

## Sequence

~~0~~ **done** -> **1 hierarchy** (multiplies everything) -> **2 limits** (cheap, unblocks a class of
board) -> **3 topological search** (the remaining quality gap) -> 4 loop -> 5 pours -> 6 pairs/tuning
-> 7 DFM.

Hierarchy first is the one non-obvious call. It buys no quality at all, and without it every later
item is paid for once per copy on a board that has four of everything.

**Item 0.5 is done and it shipped no quality at all** — which was the correct outcome both times.
The net order is a mechanism with a refusal and no declarer, because the one board it wins on buys
that win with under-rated vias; the pair leak turned out not to be a leak, and what it really is now
sits in item 6 with the arbiter's numbers and a bounded first move. **All five boards are
byte-identical to base and not one recorded number moved.** There is no cheap quality left on the
list: **hierarchy is next.**
