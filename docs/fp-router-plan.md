# The first-principles router: the plan of record

> **2026-09-23:** §3 (the corridor phase A) and the stages built on it are superseded by [`direction.md`](direction.md) §5, after the proof run showed the elbow and the good path cross the same gate word. §2 and §6 remain useful; §6.1's 29 % and §6.2's ds2 figure do not reproduce (see `direction.md`, Superseded).

pcbc's own router, driven by the net's compiled requirements, with a **corridor** — an ordered
sequence of named channel gates plus the layer arcs between them — as the one object that replaces
both the external autorouter and the post-route relaxation step. Placement is declarative where the
board author declared it and **solved** where they did not, scored by the router's own phase A rather
than by a proxy for it.

Designed 2026-09-22 by a judged panel: one survey re-run against the tree, three independent
architectures (Corridor, Toll, The Gate Market), three lenses (buildable, quality, principles).

**Correction, and it is not cosmetic.** This document was written as "Corridor with grafts" because
the tally said Corridor won. It did not. The totals were summed by design *name*, and two judges
wrote `The Gate Market` where one wrote `Gate Market`, so one design's score was split across two
rows. Merged correctly, the panel **tied at 22–22**, with Toll at 18 — Corridor takes principles
8–6, The Gate Market takes buildability 8–7 and board quality 8–7 (table in §0). So §3–§5 below are
built on a design that did not win and did not lose. The two places where the tied designs genuinely
disagree are settled in **§0**, on evidence from the tree; the verdicts are propagated into §3.3, §4
and §5, and what they raised is in §6. Everything else here is common ground between the two.

**Read `docs/topo-plan.md` first** — it is the architecture this document narrows, and its §2
contradictions (C-D through C-I) are not restated here. **Read `docs/quality-plan.md` §1** for why
every number below is from a fresh build and not from a checked-in artifact.

No code was modified while writing this.

---

## 1. The direction

The owner's words, verbatim:

> imo the external router and relaxer are bad steps. Ideally we have components get constraints on
> placement or they don't. If they don't the tool should do optimized placement to support routing.
> The router should do first principles routing based on requirements of the net

Concretely, for this codebase, that is four statements:

1. **KRT goes.** `KiCadRoutingTools`, pinned at `KRT_SHA = 3244726b…` (`route.py:37-38`) and running
   as 0.21.4 on this machine, draws 80–92 % of the copper on every non-trivial board. It is an
   external, grid-based autorouter told eleven numbers per net and none of the rest.
2. **`route_relax.py` stops being a step.** Today it runs *last*, as a repair pass that pulls copper
   somebody else drew taut by *replacing* it. Copper should be drawn taut once, by the thing that
   decided where it goes.
3. **Placement is declared or solved, never neither.** A part with `Place(at=)`, CSS insets, `to=`,
   or `edge=` is authoritative and the tool never moves it — the earlier decision ("keep it greedy
   and have the router fail with an error and we move it") holds there, unchanged. A part with *no*
   placement constraint should be positioned by the tool against what makes the board routable. That
   is a reversal: today there is no such thing as an undeclared part, only an accidental spelling
   that silently drops the part on a grid (§2.6).
4. **Routing decisions follow from the net's requirements** — current → width and barrel count,
   impedance → width and reference plane, clearance and creepage → capacity, allowed layers → which
   graphs exist, via budget → a hard bound, length budget → a prune — rather than from a grid search
   that cannot see any of them.

Point (4) is the one with the most leverage, because the requirements already exist as numbers with
provenance. `constraints.Constraint` (`constraints.py:178`) carries `width_mm`, `via: ViaSpec` with
`count_max`, `layers`, `reference`, `z_se`, `pair`, `group`, `length_max_mm`, `airwire_max_mm`,
`keep_away`, `spacing_w`, `current`, `voltage`, `loop_mm2`, `rise_ps`, `guard_ground`. Of that,
exactly two fields reach any geometry today: clearance, through `ClearanceTable.between`, and width,
through `route_channel.class_widths`.

---

## Where the tie was broken

Merged by design rather than by spelling, the panel tied:

| design | buildable | quality | principles | total |
|---|---|---|---|---|
| Corridor | 7 | 7 | **8** | **22** |
| The Gate Market | **8** | **8** | 6 | **22** |
| Toll | 6 | 7 | 5 | 18 |

A tie means the document's architecture is not settled by the panel, only by the two forks where the
tied designs actually disagree. Each fork was given to three independent verifiers who read the tree
and measured against it. Both came back unanimous — and both came back with the *reasoning* in this
document corrected, which is the part that mattered more than the winner.

### Fork A — what scores a candidate placement?

**Corridor:** tier 2 of the placement score is *incremental phase A itself*, so the objective is the
router rather than a stand-in for it.
**The Gate Market:** phase A is too expensive per candidate, so the score is named proxies
(pad-escape slack, airwire over budget, return-loop area, gate slack destroyed).

**Verdict: Corridor. 3–0, all three verifiers at high confidence.** All three also found the *ladder*
wrong, unanimously, so the verdict lands with a restructure attached — propagated into §3.3 and S5.

**The decisive evidence, and how it was obtained.** `route_channel.escapes(scene, maps, widths, …)`
takes the built per-layer `Channels` maps **as an argument** (`route_channel.py:815`), and `slack_nm`
is over `Gate` objects that only exist after `channels()`. The Gate Market's headline proxy — and
this document's own "cheap" tier 1 — therefore pays exactly the same channel-map rebuild per
candidate that the "expensive" tier 2 pays. Three verifiers read the signature; two then measured the
split, each on their own harness: copy an `examples/<name>/<name>.py` into a fresh `mkdtemp`, place it
with `pcbc.build.pcb_job`, then time `build_scene` + `channels()` and a full-graph `heapq` Dijkstra
over the adjacency `triangulate` already computes, `perf_counter`, min of N. Nothing in `examples/`
was written.

| per candidate position | buck | c3_usb | node |
|---|---|---|---|
| `build_scene` + `channels()`, all layers | 12.1 ms | 52.2 ms | 69.9 ms |
| one full-graph Dijkstra — phase A's inner search | 0.08–0.09 ms | 0.40 ms | 0.55–0.57 ms |
| `escapes()` over every pad — the Gate Market's proxy | 1.0 ms | 3.3 ms | 4.8–4.9 ms |

Both verifiers' `channels()` figures independently reproduce §2.4's table (1.4 / 5.4 / 34 / 44 ms), so
the two measurement sets are comparable with each other and with this document. **The map is ~99 % of
an evaluation and phase A's search is under 2 % of it**, which makes the cost premise the Gate Market
rests on false by roughly 50×. The real boundary is *map-free versus map-based*, not *proxy versus
router*.

Three consequences, agreed by all three verifiers:

- **Tier 1 as written is deleted.** A tier that must build the map to answer is not a cheap filter;
  it is tier 2 at 95 % of the price and worse discrimination. The ladder becomes **tier 0, map-free**
  (legality and budget, shortlisting to a declared **K**) and **tier 2, map-based** (incremental
  phase A). Nothing sits between them.
- **The cost that has to be cut is tier 0's, and this document is two orders of magnitude optimistic
  about it.** node's grid is 60 × 45 mm ÷ 0.25 mm × 4 rotations = **172,800 candidates per free part
  per sweep** (`Board(width=60, height=45)`; `pcb_place._STEP = 0.25` at `:35`, `_ROTS` at `:39`), and
  the existing `_clashes` (`pcb_place.py:560`) — the same containment + courtyard-gap + fanout-lane +
  keepout test §3.3 calls "microseconds" — profiled at **119.6 µs per candidate**, 220,963 calls and
  26.42 s of a 27.08 s node placement under cProfile (~50 µs unprofiled; `pcb_job` is 11.0 s wall).
- **`escape_deficit_nm` must never outrank a router term**, because as written it can. The three
  verifiers split on the remedy; that is an owner decision, recorded as **§6.11**.

Two things this verdict does **not** claim. Phase A is the router's *lower bound*, not the router:
its gate-midpoint realisation lands below the airwire on 8 of 15 ds2 nets and S4 forbids pinning a
length from it. And with K = 8 on node, phase A sees 8 of 172,800 candidates — the shortlist is what
places the part in practice. Both verifiers who pressed this said the same thing: the agreement rate
between the two rankings has never been measured once, in this repo or anywhere in the panel's
material. **S5's proof is rewritten to measure it**, and if phase A reorders the shortlist on under
10 % of part-placements then its term is decoration, Gate Market was right in fact if not in cost,
and the term should be deleted rather than kept.

### Fork B — what is the unit of handover from KRT?

**Corridor:** the whole board. `Board(router="pcbc"|"krt")`, the two arms never mix, one board flips
at a time.
**The Gate Market:** the net class. pcbc takes the classes it routes well; KRT keeps the generic ones,
excluded from its target list with `!NET`.

**Verdict: Corridor. 3–0, all three verifiers at high confidence.** And all three independently found
that **§4's stated reason is wrong** and must be replaced — see the rewritten §4.

**The decisive evidence, and how it was obtained.** `!NET` cannot protect anything, and that is
readable in KRT's own source rather than inferred. `expand_net_patterns`
(`~/Downloads/KiCadRoutingTools/py_router/net_queries.py:638-665`) resolves `!FOO` into an exclusion
from the returned net **name list**; the routing map is built by `build_working_obstacle_map`
(`py_router/obstacle_cache.py:889-907`) as `base_obstacles.clone_fresh()` plus
`add_net_obstacles_from_cache` for **every** entry in `net_obstacles_cache`. Nothing in the exclusion
path reaches the map — verified by direct read of both files. pcbc then pins that copper deliberately:
`--keep-input-copper` on every non-plane step (`route.py:251`). So `!NET` means "do not route this
net", and its copper remains a hard obstacle to every net KRT *does* route. The tree already says so
about its own use of the flag (`route.py:184`: "belt and braces, since KRT would skip them anyway"),
and `docs/copper-plan.md:179` records `--nets * !AIN0` failing even at its nominal job — which is why
`route.lock_copper` had to be written.

The experiment has also already been run, four times, on the board that matters.
`patterns/__init__.py:565-600` records four bounded `chain` configurations on ds2; the deciding row
wrote 13 segments / 16.4 mm on three short local runs and `GND` and `VSS` came back unrouted — "none
of it within 3.4 mm of the tap that fails and none of it on the layer of the plane that splits.
Locked copper moves KRT, and KRT's own copper closes the escape two stages later." An independent
second producer reproduced it: `spine.WIDE_MM` records that at every locality cap tried (6, 8, 12 mm
and none) ds2's 0.25 mm spines broke the gate outright. **Four bounds and four caps, and the failure
moved rather than shrank.**

**The replacement reason.** All three verifiers rejected "the hazard is the *presence* of copper" and
converged on one rule in three wordings: pcbc's copper is safe when it embodies no **choice** a later
step needed to make (V1), when it consumes no **routing freedom** a later step needed (V2), and — in
the tree's own already-written **R-S1** (`docs/stitch-plan.md:22`) — when it is "written after the
last router step that could have used the space it takes" (V3). That single rule explains both halves
of the record, which "presence" cannot: the five classes pcbc already holds are each forced
(`hop`: one path), pre-reserved (`fanout`: a lane `lane_rules` kept free at placement time), repaired
by a later KRT step that shares its intent (`tap` → `plane_taps`), or followed by nothing at all
(`stitch`) — while `chain` and a wide `spine` are discretionary, and both broke ds2.

**Why it still lands on whole-board.** A pcbc-routed *generic* net class satisfies none of those
conditions by construction. Phase A's `fits(scene, gate, members)` negotiates congestion among its own
members only, so a class-scoped phase A is blind to the demand of the classes left with KRT and will
fill to capacity the gates a KRT net needed — the ds2 mechanism exactly, with board-spanning nets
instead of 16.4 mm of local runs. No repairing successor exists either, because KRT cannot repair a
pcbc-routed net without ripping it, which `docs/topo-plan.md` C-G already refuted by measurement. And
because the failure surfaces in nets of a class that was never flipped, on a layer the flipped copper
does not occupy, **no per-class gate is cheaper than the whole-board gate of §4** — it is the same
eight conditions paid N times with both arms alive throughout, and with condition 1
(`leftover_mm == 0`) unavailable by construction in every intermediate state. The Gate Market's
time-to-value argument inverts under its own gate.

**What would overturn it**, and it is worth funding anyway: the one mechanism that could make a
partial router non-blind is a **ghost-demand pass** — run phase A over ds2's six `vias=False` analog
nets plus `REFP_F` with every other net entered as a *non-routed* member contributing `demand_nm` to
each gate its airwire crosses, emit copper for that class only, then run the unchanged remainder of
`krt_plan`. Green on all five boards against every `test_examples_fab` ceiling would mean partial
negotiation is defensible and the class is a legitimate unit after all. `GND`/`VSS` unrouted again is
the third independent reproduction and settles it permanently. Recorded as **§6.12**, not as a stage.

---

## 2. What pcbc does today

Every number carries how it was obtained. Nothing here is from a checked-in layout artifact:
`examples/c3_usb` and `examples/node` carry **zero** locked segments of 296 and 536 and predate the
R2 pattern stage — verified by scanning each `routed/layout.kicad_pcb` for `(locked yes)`. Only
blinky's and buck's are current.

### 2.1 The external router draws the board

| board | pcbc owns | KRT draws |
|---|---|---|
| buck | 15 % | 85 % |
| c3_usb | 20 % | 80 % |
| node | 18 % | 82 % |
| ds2 | 8 % | 92 % |
| blinky | 100 % | — |

*Source: `docs/roadmap.md:25` and `docs/quality-plan.md:28-31`, both from fresh builds.*
Independently recomputed for buck from `examples/buck/layout/buck/routed/copper.json`: the per-reason
census is `spine` 18.0015 mm + `tap` 4.7986 mm = **22.80 mm** against `leftover` **122.90 mm** — 15.6 %
pcbc, 84.4 % KRT — on a routed board that parses to 71 segments / 145.7 mm. blinky's census is
`{hop: 3 seg / 22.493 mm, tap: 1 seg + 1 via / 0.8146 mm, leftover: {}}`: **4 segments, 23.3 mm**, the
only board pcbc routes entirely.

**`(locked yes)` is not provenance.** A regex scan of buck's routed board finds 33 locked segments /
39.0 mm — 26.7 % of its copper — of which only 22.8 mm is pcbc's. The other 16.2 mm is KRT's own
constrained-net copper, locked by `route.lock_copper` (`route.py:155`). `PatternCtx.owned` is the
only thing that knows which is which.

### 2.2 What actually crosses the boundary into KRT

Two channels. The `.kicad_pro` netclass rows — five numbers per class (`clearance`, `track_width`,
`via_diameter`, `via_drill`, plus `diff_pair_gap`/`width` on pair classes) — and the CLI. `route.py`
mentions 23 flag names of which 21 are KRT's, out of the 95 `route.py` exposes (`grep -o '"--[a-z-]*"'`
over `src/pcbc/route.py`; `python3 krt_capabilities.py` for the inventory). KRT is **204,950 lines of
Python** plus 4,333 of Rust (`find`/`wc` over `~/Downloads/KiCadRoutingTools`, excluding `.venv` and
tests).

So the complete set of per-net information that decides a route today is: net name, layer list, one
track width, a diff-pair gap and a 90 Ω flag, via diameter and drill, a power-net width, a via cost,
a rip-up budget, a grid pitch, and a clearance ceiling on the pour step.

Three consequences, each verified in the tree:

- **The `.kicad_dru` is never handed to KRT.** `grep -n "kicad_dru" src/pcbc/route.py` returns
  nothing. Every keep-away, creepage, length, via-budget, skew and uncoupled rule is post-hoc KiCad
  DRC, and six of node's thirteen generated rules are *warnings*.
- **The wildcard `signals` step routes at `--track-width 0.0889`** — `stackup.track_min`, the fab
  floor — not at the class width (node's Default class is 0.16 mm).
- **The pair step is passed every board layer.** `layers = copper_layers(job.layers)`
  (`route.py:210`) is what the `pair_*` step receives (`route.py:~341`), not `cn.layers`. node's 90 Ω
  pair is therefore legally routable on `In1.Cu` and `In2.Cu` — which are the GND and 3V3 pours. A
  controlled-impedance pair on an inner layer has a different Z₀ from the microstrip preset that
  sized it, and it slices the plane every other net references. `docs/roadmap.md:129` records this as
  "the defect is real, the four-line fix is refuted"; the fix that is refuted is a *different* fix
  (see §5, S1).

### 2.3 The relaxer, and what it is worth

`route_relax.py` is 988 lines of which ~411 are executable. Shipped 2026-09-21; measured on fresh
builds of all five boards against the same build with the relaxer disabled by one line in
`route_job`:

| | segs | mm | micro | off45 | corners/mm | deg/mm |
|---|---|---|---|---|---|---|
| buck | 175 → **73** | 149.33 → 147.37 | 96 → **7** | 1 → **0** | 1.031 → **0.346** | 50.91 → **18.32** |
| ds2 | 315 → **228** | 508.26 → 503.55 | 96 → **32** | 12 → **7** | 0.470 → **0.302** | 23.53 → **14.74** |
| c3_usb | 444 → **302** | 361.16 → 358.88 | 188 → **85** | 14 → **12** | 0.839 → **0.479** | 49.59 → **33.09** |
| node | 314 → **275** | 411.72 → 409.55 | 54 → **28** | 25 → **18** | 0.442 → **0.352** | 35.92 → **28.61** |
| blinky | 6 → **4** | 23.63 → 23.31 | 0 | 0 | 0.169 → **0.086** | 7.62 → **3.86** |

*Source: `docs/roadmap.md:39-52`; the hand-routed DS2 Addon reference is 17.2 deg/mm and 0.336
corners/mm (`docs/quality-plan.md:32`). Arbiter: zero DRC errors, zero unconnected, no per-type
violation count above base on any board, one `kicad_drc` run per arm.*

Half of that improvement is pcbc buying back damage pcbc caused: with `PCBC_PATTERNS=off`, buck routes
at **25.5 deg/mm and 26 micro-segments** where the shipped pipeline gets 50.9 and 96, and the whole
difference is inside KRT's `signals` step (1.25 seg/mm and 0.84 micro/mm with patterns on, 0.60 and
0.19 with them off — `docs/quality-plan.md` §1b, attributed by reading every step file).

**Detour it does not touch**, and provably cannot: relaxation cannot change which side of an obstacle
a route passes. buck 2.55 → 2.51, c3_usb 1.70 → 1.69, node 1.82 held; ds2's `REFP_F` stays at **3.23 ×**
its 20.50 mm airwire.

### 2.4 The geometry layer is sound, and has no search in it

`route_geom.py` (766 lines, stdlib only), `route_scene.py` (1,215) and `route_channel.py` (950) are
exact, grid-free and integer-predicate. Measured on this machine (scripts under the session
scratchpad; `Channels.wall_ms` for the map, 2,000 random 5 mm segments for `blocked`):

| | blinky | buck | c3_usb | node |
|---|---|---|---|---|
| `channels()`, placed, all layers | 1.4 ms | 5.4 ms | 34 ms | 44 ms |
| `channels()`, routed | — | 18 ms | — | **165 ms** |
| `blocked()` per call | — | 119 µs | — | 218 µs |
| `free_intervals()` per call | — | 88 µs | — | 423 µs |
| `crossed()` per call | — | 57 µs | — | 215 µs |
| `components()` over every net | — | — | — | 43.5 ms placed / 241 ms routed |

Two structural facts fall out of that table and they are load-bearing for §3:

- **A routed board costs ~4 × a placed one to map.** That is the cost a design which freezes the
  obstacle set after fanout never pays.
- **`blocked()` at 110–271 µs is why a rasterised maze router is arithmetically impossible** here
  (`docs/quality-plan.md` C8, against KRT's 3,530–77,632 expansions on c3_usb's `signals` alone), and
  it is why the search in §3 never calls it.

And there is no path query of any kind: `grep -rniE 'dijkstra|a_star|astar|visibility|funnel|homotop'`
over `src/pcbc/*.py` finds one Dijkstra, in `ampacity.py:228`, over a current graph. `route_channel.fits`
— the exact integer-nanometre capacity rule — has **zero production callers**, as do `crossed`,
`lane_run_mm` and `audit`.

### 2.5 Two silent correctness holes in the judge layer

- **`route_scene._MAX_NEED = 3.0`** (`route_scene.py:162`) bounds how far the index looks. A required
  clearance above `scene._max_r + 3.0` is **not checked at all**. Demonstrated with a two-item scene
  and a stub `ClearanceTable` returning 6.4 mm (which is
  `stackup.iec_creepage_mm(300, 'IIIa', reinforced=True)`): an obstacle at 4.65 mm returns
  `clashes() == []` and `blocked() is None`; the same obstacle at 2.0 mm is correctly refused with
  `has 1.65 mm, needs 6.4 mm`. Latent today — no example board declares `Isolation()` — and KiCad's
  own DRC still catches it, so the cost is a wasted build. It stops being latent the moment a router
  derives clearances from voltage. `route_channel`'s copy of the same constant (`route_channel.py:113`)
  is dead code: defined, docstringed, never referenced.
- **Every via in the scene spans every copper layer.** `_vias` parses `(layers "A" "B")` and
  `copper_items` (`route_scene.py:~328`) discards it, assigning `frozenset(stack.copper_layers())`;
  `Scene.item_of` does the same. `route_scene.py:193` says so out loud. Any multi-layer router built
  on this scene believes a buried In1–In2 via blocks F.Cu at the same xy.

A third, smaller: a via item is named `f"{net} via at ({x:g},{y:g})"` (`route_scene.py:339`, `:565`)
— a coordinate inside a name. A cited via that moves renames itself, and with it every stored gate
word that names it.

### 2.6 Placement reads one electrical number, and "you decide" is an accident

`pcb_place.resolve_places` reads `job.regions`, `job.keepouts`, `job.places`, the board geometry, and
`lane_rules(job)` — the max class `lane_clearance_mm`, used only to widen a closed pad row's fanout
lane. It never touches `job.nets` or `job.constraints`.

Across the four in-repo boards, of 64 `Place()` statements: **51 relational (`to=`), 8 CSS, 5 `edge=`,
0 `at=`, 0 undeclared** (classified by loading each board and reading `design.places`). 12 of 39 nets
carry no `Constraint` at all. `keep_away` is empty on every example board and
`ConstraintSet.fastest_edge()` returns `None` on all of them.

And there is an accidental "you decide": `Place("D1", locked=False)` with no anchor is **accepted** —
the refusal at `language.py:306` is gated on `spec.locked`. Ran it end to end on a copy of blinky:
`pcbc check` returns `{"ok": true}`, `pcbc pcb` prints `layout: nothing to move`, `resolve_places`
returns `at=None position="static"` and `continue`s before `settle`, so the part is **never added to
the placer's obstacle list** while its copper is real (instrumented `fiducial_spots` printed
`OBSTACLES seen: [('R1', (8.93, 12.5))]` — D1 absent). D1 lands at exactly (13.0, 5.0), which is
`seed.emit_pcb`'s 8 mm insertion-order grid (`seed.py:213-217`). Forcing R1 onto the same slot gives
`error: check: R1 (VCC) vs D1 (LED) pad gap 0.000 mm < 0.127 mm` — caught afterwards, not avoided.

---

## 3. The target architecture

One object: the **corridor**. An ordered sequence of gates — a gate being a channel between two
obstacle corners, named `((ownerA, cornerA), (ownerB, cornerB))` — plus the via arcs between runs on
different layers. **Phase A** chooses corridors; **phase B** fills them with copper. The same phase A
is the placement solver's objective function, so "optimised placement to support routing" is not a
proxy: the objective literally is the router. That claim survived Fork A (§0) on cost — phase A's
search is under 2 % of a candidate evaluation — but it must be read with two limits the verifiers
established: phase A is the router's *lower bound*, not its route (§6.7), and the solver evaluates it
one stage upstream of fanout (§6.14).

### 3.1 A net, from requirement to copper

**Step 1 — compile the need.** Each net's `Constraint` folds to a `Need`: `width_nm` (IPC-2152 from
`amps`, or `width_for_z0` from `z_se`/`z_diff`), `layers` with a per-layer integer cost, `via_budget`
(`via.count_max`, 0 when `NetReq(vias=False)`), `vias_per_change` from barrel ampacity,
`length_max_nm`, the pair partner, and the clearance closure `table.between(net, ·)`. `keep_away`,
`volts` and `Isolation` need no new wiring — they are already inside `between`, and after S1 they are
inside `blocked` too.

**Step 2 — fanout, then freeze.** `fanout.py` writes closed-row escape stubs and staggered vias
exactly as today. **After that the obstacle set is frozen for the whole route.** This is the
architectural claim that makes the cost arithmetic work, and it is stated here rather than assumed:
phase A commits no copper, so the geometry never changes and the congestion map is built **once**
(44 ms on placed node) instead of per net on a board that is 4 × more expensive to map once routed.

**Step 3 — build the search graph.** Per layer, `route_channel.channels(scene)`. Nodes are
`(layer, triangle)`; intra-layer edges are gates weighted midpoint-to-midpoint in integer nanometres;
inter-layer edges are **via arcs**, admissible when `antipad_clash` is clean and `via_pitch` holds,
priced `vias_per_change(net) × via_equiv_nm + return_penalty(return_rule(net, L1, L2))` — `kept` and
`pinned` free, `net_change` a stitch-pair's worth, `lost` +∞ unless the net declares it may. The dual
graph is the triangle adjacency `tn` that `triangulate` already computes and discards
(`route_channel.py:283` returns `(sorted(tris), sorted(edges))`); returning it is three lines.

**Step 4 — phase A: a Steiner corridor per net, negotiated.** Nets in a declared order. Repeatedly
Dijkstra from the current tree to the nearest unconnected terminal, queue key
`(cost_nm, gate_id, prev_gate_id, layer_id)` — four integers, no float ever pushed. An edge is
admissible only if the layer is allowed, the branch's via count is within budget, and
`slack_nm(scene, gate, net, width) >= 0`: one net alone not fitting is a **wall**, and a wall is a
refusal, not a cost. `length_max_nm` and `via.count_max` are *prunes*, so the refusal carries the
search's own reason rather than a post-hoc comparison. Cost is
`length_nm + present_cost[gate] + history_cost[gate]` plus the via terms.

**Step 5 — PathFinder, `for _ in range(30)`.** After every net has a corridor, evaluate each gate
with `fits(scene, gate, members)` — integer nanometres on both sides, no epsilon, and already
calibrated at 0 false-full over the 875 gates a routed board's own copper crosses. Where it fails,
raise `present_cost`, accumulate `history_cost`, rip up the *corridor* (not copper) and re-route.
Measured on the existing prototype: ds2 converges at iteration 4, zero overflow, 0.09 s.

**Step 6 — phase B pass 1, slot assignment.** Within each gate the crossing members get integer
nanometre offsets along the gate line, in the crossing order the corridor induces, tie-broken by the
net key, with offsets computed from `demand_nm`'s own arithmetic term for term so the assignment and
the capacity test cannot disagree about a nanometre. A pair occupies one slot, because it entered
phase A as one member of width `2w + gap` (§3.3).

**Step 7 — phase B pass 2, realise and push.** The slotted points are a polyline with `|word| + 2`
vertices. Gate capacity is checked per gate and never per triangular *face*, so a vertex may still be
blocked: each vertex slides along its own gate, outward from its assigned offset in `_STEP`
increments, until `route_scene.blocked` returns `None`. A vertex with no clear offset is a phase-B
refusal naming the face. **This pass is not optional** — `route_relax` never checks its input's
legality (`cur = _straighten(chain.pts); best = quality(cur, ends)`, with no `blocked` call anywhere),
so without the push a blocked seed is accepted, reported as improved, and shipped.

**Step 8 — phase B pass 3, pull, octilinearise, emit.** `route_relax`'s accept loop unchanged in
substance: `_moves`' O(n²) candidate list, `quality()`'s lexicographic objective, `Holds`' contact
preservation, `blocked` as the veto on drawn legs, plus a homotopy guard so the pull cannot leave the
class phase A paid for. Then `octile_path`/`q`, then `route_emit.write_pieces` — **appended once,
never replaced**. Then `route_verify.verify_copper`, `net_open` per net, then KiCad DRC as the
arbiter.

**Cost, arithmetically.** Map once, ≤ 50 ms. Phase A 0.09 s measured on ds2. Phase B per net: ~2
`blocked()` calls per gate to push (40 × 220 µs ≈ 9 ms) plus a pull loop of at most 6 first-accept
passes (worst observed on buck: 6). Call it 0.4 s per hard net; 43 nets on ds2 ≈ 17 s, against KRT's
4.6–34.8 s route wall. The grid plan failed this arithmetic and this one passes, for a structural
reason: the corridor *is* the search, and inside it the geometry is one-dimensional.

### 3.2 Requirement-preserving solvers for the classes that are not generic

Grafted from The Gate Market, and scoped: the corridor search owns the generic two-terminal class,
and a small set of per-kind solvers own the classes where the requirement should *generate* the
answer rather than be priced by it. Each exposes exactly three names, the discipline
`patterns/__init__.py` already states — `demands(c, chan, scene)` returning a fixed, finite, ordered
tuple of itineraries; `price(it, prices)`; `embed(it, slots, scene)`:

- **`current`** — a `CurrentSpec` net is a cross-section budget, not a track. Its itinerary is *k*
  parallel conductors of width *w* with `ceil(amps / via_amps)` barrels per layer change, all from
  `stackup`/`ampacity`. At 40 A the answer is "4 conductors of 2.10 mm and 14 vias per transition",
  and if the gates cannot supply it that is a refusal, not a neck.
- **`pair`** — one corridor member of width `2w + gap`, so the two halves share a gate word by
  construction and skew is structural rather than repaired.
- **`constrained`** — `vias=False`, single-layer, `keep_away`: the requirement removes freedom, the
  word is pinned to one layer, and the solver refuses rather than inventing a via.
- **`plane`** — pours stay a separate producer (§4).

The generic class stays a corridor search: The Gate Market never specified how `demands()` would
enumerate a finite ordered itinerary set over a 1,200-triangle graph, and c3_usb's 296 and node's 536
segments live in exactly that class.

### 3.3 An unconstrained part gets a position

**Declared is authoritative.** `at=`, CSS anchors, `to=`/`toward=`/`gap=`, `edge=`, `side=`, `rot=`
resolve first, in file order, exactly as `pcb_place.resolve_places` does today, and a declared part
with no clear spot stays where the collision is and is reported as a move.

**Undeclared gets a name.** One keyword, scoped by machinery that already exists:

```python
Place("C7", free=True)                       # anywhere in the content box
Place("C7", free=True, parent="analog")      # anywhere in that Region
Place("C7", free=True, near="U1.AVDD")       # soft attractor: the soft twin of the hard to=
Place("C7", free=True, side="B")
```

`circuit.check_design`'s rule that every instance must have a `Place()` is satisfied, not bypassed —
`free=True` *is* a `Place()`, and what it declares is "you decide". **Closing the accidental spelling
is part of the same change and is not optional**: the refusal at `language.py:306` must fire for
`locked=False` with no anchor too, and `resolve_places` must make a free part a first-class member of
`placed` so it is an obstacle to everything else. Otherwise the unsound spelling of §2.6 ships beside
the sound one.

**The candidate list is finite and ordered**, the same discipline as the patterns:
(a) relational sites generated by reusing `pcb_place._attach` verbatim — for each net this part
carries, the site `_attach` would produce against every already-placed part on that net, at each of
`_ROTS`, ordered by `(target_ref, target_pin, rot)`, with `near=`'s target promoted to the front; then
(b) grid sites over `parent`'s rect at `_STEP = 0.25 mm` × `_ROTS` in coarse-grid boustrophedon
order — a pure function of the rect.

**Scored lexicographically, every term an integer:**
`(sealed_pads, escape_deficit_nm, corridor_overflow, corridor_length_nm, via_count, candidate_index)`,
with `escape_deficit_nm` a **shortfall clamped at zero**, never `Escape.slack`'s signed value — see
§6.11, where the term's place in the key is an open owner decision.

**Two tiers, and the boundary is map-free versus map-based** (Fork A, §0; the three-tier ladder this
document originally specified is withdrawn, because its "cheap" tier 1 was
`route_channel.escapes(scene, maps, …)`, which consumes the same 12–70 ms channel map as tier 2 and
discriminates worse):

- **Tier 0 — map-free, every candidate.** Courtyard gap + `Foot.lane` keep box + keepout + region
  containment: the test `pcb_place._clashes` already implements. It produces the shortlist, of
  **declared size K** (K = 8 to start; K is a number in the source with the agreement measurement of
  S5 beside it, not a constant nobody can argue with). Because tier 0 is the only thing that runs
  172,800 times per free part per sweep on node, **and it profiles at 119.6 µs per candidate today**,
  making it affordable is part of S5's work and not an aside — a rejecting predicate ordered
  cheapest-first, an interval or grid index over `others` instead of the linear scan, and a coarse
  sweep before the fine one.
- **Tier 2 — map-based, K candidates.** **Incremental phase A over only the nets this part touches**,
  every other corridor held fixed. "Held fixed" is not free and must be specified rather than
  assumed: a 0.5 mm part move preserves 96.5–97.0 % of buck's 202 gate names and 99.4 % of node's
  1086 (measured by shifting one footprint's `(at x y)` in the placed layout text and set-differencing
  the `{g.key}` sets — which independently reproduces §3.5's 97.1 %), so **4–7 gates per move lose
  their name** and the corridors through them must be locally repaired, not silently dropped.

Tier 2 is what makes the objective the router rather than a proxy, so the solver does not ship until
phase A exists (§5, S5). What tier 2 is *not*: it runs on a pre-fanout scene (`fanout_pieces` consumes
the placed board text), so it is a higher-fidelity proxy for phase B than the proxies it replaced, not
literally the shipped route — §6.14.

**Construct, then improve, both bounded.** Free parts in order `(-degree, -courtyard_area, ref)`; then
`for _ in range(4)` sweeps in the same order, each accepting only a **strictly better** score. It
terminates on its own because the score decreases lexicographically and is bounded below; the cap
bounds a bug, not the algorithm — the same shape as `route_relax._ITER_CAP = 400` against a worst
observed 6.

**`pcbc place --freeze`** prints the solved positions as pasteable `Place(ref, at=(x, y), rot=r)`
lines, converting a solved part into a declared one. Because the solver is deterministic, *not*
freezing is equally safe; freezing is for when the author wants the tool to stop having an opinion.

**Why this cannot regress a shipped board:** zero of the five boards' 64 `Place()` statements use any
undeclared form, so the solver is dead code on all five, and "byte-identical" is the gate.

### 3.4 The failure path: what is printed when routing cannot succeed

Every refusal is a `blocking.move_line` sentence ending in a `board.py` edit. Three shapes.

**Gate contention.** The search already holds each member's second-best path cost, so the refusal
ranks the choice instead of asking the author to guess:

```
VBAT: the channel between U3.7 and C12.1 on F.Cu carries VBAT, SDA and 3V3.
  They need 2.840 mm across it and it supplies 1.900 mm — 0.940 mm short, after 30 rounds.
  Contested since round 4. 2 of 3 members have no alternative under their own NetReq.
  Place("C12", to="U3.VIN", gap=1.15)          # was gap=0.2; +0.94 mm, the direct fix
  NetReq("SDA", layers=("F.Cu","B.Cu"))        # or let SDA change layer: its next-best corridor
                                               #   is 4.21 mm longer and clears this gate
```

**A cross-section that no corridor can carry** — the refusal reaches outside the placement, because a
solver that owns its own cross-section budget knows what else would satisfy it:

```
PHASE_A: 40 A needs 4 conductors of 2.100 mm (IPC-2152, 2 oz, 10 C rise) and 14 vias per
  layer change; the corridor Q1.2 -> J2.1 supplies 3 on F.Cu + In2.Cu. Over by one conductor.
  NetReq("PHASE_A", amps=30)                   # derate
  or Board(layers=8)
  or Place("Q1", toward="down", gap=0.6)       # widens the U3/Q1 channel by 0.55 mm
```

**A free part with no site.** It refuses; it does not silently land on a grid:

```
C7: no site within 2.5 mm of U1.AVDD clears (loop budget 4.0 mm2); nearest is 3.1 mm.
  In the way: U2 courtyard, R4 pad 2.
  Place("C7", to="U1.AVDD", gap=0.9)
  or Place("U2", left=11.4)                    # was left=12.0
```

Every millimetre in those lines comes off the same `supply_nm`/`demand_nm` pair the rule used, so the
report and the decision cannot disagree.

### 3.5 Determinism

The candidate list, the net order, the gate names and the queue keys are pure functions of
`board.py` + the stackup. Gates are named `((owner, corner), (owner, corner))` — never a vertex index
— which survives a 0.5 mm part move at 97.1 % on buck against 84.7 % for indices. `build_scene` sorts
items and assigns ids after the sort. Every cost is integer nanometres. `topo.json` and
`placement.json` are byte-compared across two runs in the suite. `route_geom.q` must stay
**ties-to-even** in any future Rust port (`.round_ties_even()`, not `.round()`); `geom_vectors.json`
already carries the four diverging ties.

---

## 4. What happens to `route_relax.py`, and to KRT

### `route_relax.py`: the file survives, the step dies

It is renamed `route_embed.py` and becomes phase B's third pass. `relax_board` shrinks to
`pull(scene, seed, need, holds)`. What survives verbatim: `quality()`, `Holds`/`holds()`,
`_straighten`, `_seg_meet`/`_seg_closest`/`_div_round`, `orphan_copper`, and the three whole-board
self-checks (connectivity groups, orphan millimetres, file-truth segment count — the third caught the
module's worst historical bug).

Four changes, three of them already specified by `docs/topo-plan.md` C-H:

1. `_hold2` degenerates to `sh.r**2` for a **seed**. On existing copper `max(requirement, reach_of_the_input)`
   is a safety property; on a path the router just proposed it freezes every accidental graze. **The
   two call sites will therefore differ on purpose, and that must be written down in the module** — a
   reviewer checking them for sameness will otherwise "fix" it.
2. `Holds` gains a `pinned` field: the corridor *declares* its terminals and a trim may not retract
   one.
3. The homotopy guard becomes a check rather than a property, because the seed's class was paid for
   in tolls. Its fire rate from a seed is **unmeasured**, and the regime the measured zero came from
   (KRT's loose copper) is the wrong one. The measured zero must not migrate into the docstring.
4. New: the push pass (§3.1 step 7) runs **before** the pull.

**It cannot start from an airwire, and this is written down so nobody re-derives it.** Verified:
`_moves(((0.0,0.0),(10.0,4.0)))` yields `[]` — both trims are guarded by `len(cur) >= 3`
(`route_relax.py:650`) and the pull loop is `range(0, n-2)` (`:654`), empty at n=2. So the relaxer
would return an airwire unchanged, straight through every obstacle, report `moved=0`, and pass all
three self-checks, because connectivity did not get *worse* and it was never asked whether it got
better. The architectural reason is stronger than the mechanical one: an airwire carries no homotopy
class, phase A owns homotopy, and phase B owns shape. The legitimate seed is the slotted corridor
polyline, which has `|word| + 2 >= 3` vertices by construction.

`RELAXABLE`, `chains_of`, `_reason_of` and `_skip_nets` go with the step — in the pcbc arm there is no
`leftover` to classify. `route_emit.strip_segments`/`replace_segments` stay in the tree, for the KRT
arm while it exists and afterwards for `pcbc route --net NAME`.

What must be retired consciously: the relaxer's *entire* safety argument today is positional —
`krt_plan` schedules nothing after it. As a router phase it has a different argument, and it has one:
it operates on a seed it produced, inside a class it chose, and there is exactly one emission, at the
end, after every decision.

### KRT: deleted per board, against a measured gate

**Whole board or not at all** — the verdict of Fork B (§0), 3–0, and the reason below is the
*corrected* one. The obvious ratchet — pcbc routes what it can, KRT takes the rest — is the failure
mode this repo has documented three times. Registering the `chain` pattern puts 16.4 mm of short,
legal, *complete*, local copper on the far side of a 47 × 25.4 mm board and ds2's `GND` and `VSS` come
back unrouted — never where the copper is. `PCBC_PATTERNS=on` doubles buck's corner density entirely
inside KRT's `signals` step. And `docs/topo-plan.md` C-G: reordering, unlocking, full rip-up and
force-reroute all fail the same way.

**The reason is not "the copper is there".** That was this document's original claim and it is
refuted by its own record: five classes of pcbc copper are present on every board and mix safely. The
rule the three verifiers converged on — and which the tree already states as **R-S1**
(`docs/stitch-plan.md:22`) — is that pcbc's copper is safe exactly when **no later router step is left
contesting the space it took**: when it embodies no choice a later step needed to make, and consumes
no freedom a later step needed. Every class pcbc holds today meets that test. `hop` has one path
(`LOCAL_MM = 5.0`, "a hop between two neighbouring pads has one path"). `fanout` writes into a
`Foot.lane` that `pcb_place.lane_rules` reserved at *placement* time, so no router was ever allowed
there. `tap` is a via under its own pad with zero lateral extent, and `plane_taps` runs afterwards to
weld the pads it missed. `stitch` is FINAL — `krt_plan` schedules nothing after it. `chain` and a wide
`spine` are discretionary, and both broke ds2 at every bound and every cap tried.

**`!NET` does not satisfy that rule and cannot.** It is a target-list filter: KRT's
`expand_net_patterns` drops the name from the list to route, while `build_working_obstacle_map` adds
obstacles for **every** net in the cache regardless (`py_router/net_queries.py:638-665`,
`py_router/obstacle_cache.py:889-907`), and `--keep-input-copper` (`route.py:251`) pins pcbc's copper
there on purpose. `route.py:184` already calls pcbc's own `!NET` "belt and braces"; `copper-plan.md:179`
records it failing even at its nominal job. A pcbc-routed *generic* class fails the rule at the
source: `fits(scene, gate, members)` negotiates only among its own members, so a class-scoped phase A
fills gates a KRT net needed and no successor can repair them without ripping copper. Only whole-board
removes contention.

`Board(router="pcbc"|"krt")` selects the arm, defaulting to `"krt"`. The two arms never mix. Rollback
is one line in one `board.py`. Fanout and plane taps are pcbc's in both arms, as they already are.

**The gate, per board, both arms, fresh builds:**

1. `leftover_mm == 0` in `copper.json` — the single number that says pcbc drew all of it.
2. **0 KiCad DRC errors and 0 unconnected items.** KiCad is the arbiter; nothing else is an argument.
3. No per-type DRC violation count above the KRT arm, warnings included.
4. Worst detour per net ≤ the KRT arm's.
5. deg/mm ≤ the KRT arm's, and measured against the hand board's 17.2.
6. **Copper millimetres ≤ 1.05 × the KRT arm's.** A shorter homotopy class is allowed to be
   shape-worse before the pull; it is not allowed to be longer after it. Detour ceilings do not catch
   this.
7. Ampacity holds: `route_verify.via_parallelism` and `_barrel_gate` report no net short. This is the
   check that caught `Board(net_order=)` buying buck −9.7 % copper with two under-rated vias on a
   2 A rail, and it is the check a from-scratch router is most likely to fail first.
8. Byte-identical across two runs, board and `topo.json`.

Order of flips: **blinky** (pcbc already routes it entirely), then **buck + ds2** together, then
**c3_usb + node**. Only after the last does the deletion stage run. The gate is unchanged by Fork B —
whole-board won — but the corrected reason gives the *KRT arm* a rule it did not have: while both
arms exist, no new pre-router producer is registered unless it meets **R-S1** (forced, pre-reserved,
or followed by a step that repairs the same intent). That is the admission test `chain` failed four
times and `spine` failed four times, stated once instead of rediscovered per pattern.

**What must not be quietly excused.** *Plane pours*: `route_planes.py` is 43 flags and a corridor
router does not subsume it. The plan is that pcbc writes the pour outline and KiCad's own filler
fills it — which `route_verify.pour_raster` already predicts — and that is a different producer, not
a port. **If pours are still KRT's when everything else has moved, that is a legitimate stopping
point and it will be said out loud.** *147 boards of tuning*: gate conditions 3–7 are the only
defence against shipping a worse router with a better architecture, and they are written to fail
rather than to pass.

---

## 5. The stages

Every stage ends with the same non-negotiable clause, stated once here and repeated in each: **all
five boards build, route, and pass `kicad_drc` with 0 errors and 0 unconnected items, with no
per-type violation count above the stage's own base.** A stage that cannot meet that does not ship.

### S1 — Requirements reach the geometry, and the clearance ceiling goes *(4–6 days)*

**Changes.** `route_channel.class_widths(job)` becomes `needs(job, cs)`, returning a `Need` per net
(width, layers, via budget, `vias_per_change`, length budget, pair partner). `demand_nm` learns
pair-as-one-member (`2w + gap` as a single member) — cheaper than a solver, and it is what makes
skew structural later. `escapes`/`moves` read `Need.layers` instead of the ad-hoc `layers_of`.
`route_channel.triangulate` returns the adjacency array `tn` it already builds. `route_scene._MAX_NEED`
becomes board-derived — the largest value `ClearanceTable.between` can return — and
`route_channel._MAX_NEED` is deleted as dead. `ignore=` is added to `free_intervals` and to
`channels()` to match `clear_runs`. `_blocking` is promoted from a private bool to a public ordered
entry/exit ray-cast. One more line, out of scope but four characters and shipping here because it
improves the **KRT** arm too: the pair step is given `cn.layers`, not `copper_layers(job.layers)`
(§2.2). New fixtures: one board declaring `NetReq(keep_clear_of=)` and a `Pair()` (no example does),
and **one in-repo board that reproduces ds2's detour class**, because `tests/test_bridge.py:32` points
at `~/Documents/MaD/Hardware/DS2Addon/pcbc` and every ds2 test is `skipif`-guarded — without it the
headline proof of S4 and S7 cannot run on a clean checkout.

**Proof.** The synthetic case that silently accepts today must refuse: 6.4 mm required against an
obstacle at 4.65 mm returns `has 4.65 mm, needs 6.4 mm` instead of `[]`. `blocked()` throughput is
**pinned in the same PR** — 119 µs/call on placed buck, 218 on node, must not more than double —
because widening the index reach costs every query and `blocked()` is on phase B's hot path. The
fixture board shows a different census and different `escapes` slack with and without its keep-away
and its pair. `tests/test_route_channel.py:297` (buck 196 F.Cu gates) still passes; Euler still holds
per layer with `adj` returned. **All five boards byte-identical.**

**Risk.** Low. The real hazard is re-recording a pinned slack number without measuring why it moved;
each moved number gets a one-line cause beside it in the test file.

**Value if you stop here.** The placement oracle starts answering per *net* instead of per class, a
silent false accept on isolation and creepage rules is closed, and the 90 Ω pair stops being legally
routable through the plane layers in the arm that ships today.

### S2 — Vias get spans; the scene stops being planar *(2–3 days)*

**Changes.** `copper_items` and `Scene.item_of` keep the parsed `(layers A B)` instead of assigning
`frozenset(stack.copper_layers())`; the `route_scene.py:193` comment goes. `Item` gains a stable id
beside the coordinate-bearing label; the label stays for `Item.label()` and the refusal lines.
`pour_raster` and `antipad_clash` are audited against the narrower spans.

**Proof.** Every via on today's boards is a through via, so **all five boards byte-identical** — and
this is the only stage that can newly *permit* something, so if a board moves, the stage stops and
the diff is read before anything else. Two new tests: a buried In1–In2 via does not clash with F.Cu
copper at the same xy; a through via at the same xy still does. A via that moves keeps its id.

**Risk.** Medium, and bounded by the gate above. Scheduled before any via arc is built, because a
via arc enumerated on a planar scene is a false refuse on four layers and structurally wrong on six.

### S3 — `route_topo.word_of` and the stored sketch *(3–4 days)*

**Changes.** The reader only: the gate word of copper that already exists, consuming
`route_relax.chains_of`; a `"sketches"` key in `route_emit.Sidecar` (`read_sidecar` already tolerates
missing keys). No search, no copper.

**Proof.** The word is byte-identical across a `relax` run on every chain whose endpoints did not
move — the measurement exists today as 0 winding-class changes on 75 chains (buck 16/16, ds2 59/59),
now expressed as the stored word. Per-part blast-radius table published. **All five boards
byte-identical.**

**Risk.** Low. S2 removed its prerequisite defect.

### S4 — Phase A as an oracle: corridors, multi-layer, requirement-driven, no copper *(2–3 weeks)*

**Changes.** `route_search.py`: the dual graph from S1's adjacency, via arcs priced by
`vias_per_change` and `constraints.return_rule`'s four verdicts, a Steiner heuristic per net,
PathFinder with `fits` as the occupancy test and `for _ in range(30)`, the declared net order, and
the two refusal shapes of §3.4. `topo.json` carries every net's word, its convergence iteration, and
per-gate final occupancy. `pcbc route --corridors` prints the census and the contention moves. **No
copper.**

**Proof.** Converges with zero overflow on all five boards. ds2's `REFP_F` lands in a corridor whose
midpoint realisation is ≤ 1.3 × its 20.50 mm airwire where KRT ships it at 3.23 × (two existing
methods agree the corridor is real: 19.12 mm by gate midpoint, 23.01 mm routed alone). buck's
20-point anchor grid: false-accept 29 % → ≤ 10 %. Every net gets a corridor or a refusal with a move.
Byte-identical `topo.json` twice, on all five boards. **All five boards byte-identical**, because
nothing is emitted.

**Risk.** High; this is the big one. Named unknowns: the via price is a guess (§6), Steiner trees on
multi-terminal nets are unprototyped, and the midpoint polyline lands *below* the airwire on 8 of 15
ds2 nets — which proves it is a lower bound on the class and not a route. **No test may pin a length
from this stage as a quality number**, and the report must say so on every line.

**Value if you stop here.** Placement failure becomes per-channel arithmetic with a magnitude and a
ranked alternative, which is a better failure than pcbc has today.

### S5 — Solved placement for parts with no declared position *(1–2 weeks)*

**Changes.** `Place(free=, near=)` in `language.py`, and the refusal at `language.py:306` extended so
`locked=False` with no anchor is finally illegal. `resolve_places` makes a free part a first-class
member of `placed`. New `place_solve.py`: the candidate enumeration, the lexicographic score, and
**two tiers, not three** (Fork A, §0 and §3.3) — tier 0 map-free to a shortlist of declared size K,
tier 2 incremental phase A on the K, `escapes` deleted from the ladder because it is not cheap.
**Tier 2 is real from day one**, which is why this stage is after S4 and not before it.
`escape_deficit_nm` is a shortfall clamped at zero wherever it appears. Two items Fork A moved into
this stage's scope that the original change list did not name:

- **Tier 0 has to become affordable.** `_clashes` profiles at 119.6 µs per candidate against a
  172,800-candidate grid per free part per sweep on node. A rejecting predicate ordered
  cheapest-first, an index over `others` instead of the linear scan, and a coarse sweep before the
  fine one — with the per-candidate cost pinned in the PR the way `blocked()`'s is in S1.
- **Incremental phase A is a named deliverable, not an adjective.** One part's nets re-solved against
  held corridors, plus the local repair for the 4–7 gate names a 0.5 mm move destroys.

**Proof.** The five boards declare every part, so **all five must be byte-identical** — that is the
gate and it is what stops this stage regressing a working board. A new fixture with six free decaps
places identically on two runs (byte-compared `placement.json`), has 0 sealed pads, routes with 0
unconnected and 0 DRC errors, and lands within one candidate step of the hand-written `to=` version
with identical `copper_bar` output. `Place("D1", locked=False)` now refuses, with a line-cited
message. Placement is independent of the order parts appear in `board.py` (shuffle the file, assert
the same placement).

**And the measurement Fork A turned on, which is a ship condition for tier 2 and not a nice-to-have.**
Nobody in this repo or in the panel has ever measured whether the router's opinion changes a
placement. Two tables, published:

1. **Agreement, on buck's existing 20-point anchor grid** — the fixture behind §6.1's 29 %
   false-accept number, whose 13 routable / 8 unroutable ground truth comes from real builds. Score
   all 20 anchors three ways: (i) the Gate Market proxy terms alone, off one map; (ii) tier 0 plus
   incremental phase A; (iii) the full KRT/KiCad build as arbiter. Report per method the false-accept
   rate against (iii), the Spearman rank correlation with (iii)'s ordering, and wall clock per
   candidate. Repeat on node for one high-degree part (`U1`, net-degree 11) and one low
   (`C_3V3`, degree 2), reporting how often phase A's argmin lands inside the proxy's top K.
2. **Does tier 2 change anything** — run the six-free-decap fixture with tier 2 enabled and disabled
   and count the part-placements where the winner differs.

**The ship condition:** if tier 2 reorders the shortlist on **under 10 %** of part-placements, and
its false-accept rate is within ~3 points of the proxy's with no better rank correlation, then the
objective is the proxy in fact, phase A's term is decoration, and **it is deleted from the score
rather than kept** — Fork A is re-decided for The Gate Market on the measurement, and §0 is amended
to say so. If it reorders on **over 40 %**, the defect is the shortlist cap and K rises to whatever
the map budget allows. Between the two, K stays 8 and the table is published with the number.

**Risk.** Medium. `resolve_places` is the function every board's geometry comes out of; the
byte-identical assertion on all five is the mitigation, and a free part is the only new path through
it. The subtler risk is scope creep into a general optimiser: the moment the candidate list stops
being finite, ordered and derived from the board, determinism becomes a property to be defended
rather than a consequence. Fork A added two deliverables — an affordable tier 0 and a genuinely
incremental phase A — and both are schedule risk; this stage may run past two weeks because of them.

### S6 — Phase B, and blinky flips *(3–4 weeks)*

**Changes.** `route_relax.py` → `route_embed.py` with the four changes of §4. New: slot assignment
(integer offsets from `demand_nm`'s own terms), the realise-and-push pass, and the pull. The
`constrained` solver (§3.2), because its class is the one KRT already routes first on a near-empty
board. `Board(router="pcbc"|"krt")` defaulting to `"krt"`; a `pcbc_route` plan with no KRT step at
all; `examples/blinky/blinky.py` declares `router="pcbc"`.

**Proof.** blinky's full eight-condition gate: `leftover_mm == 0`, 0 DRC errors, 0 unconnected, no
per-type count above the KRT arm, worst detour ≤ 1.03, deg/mm ≤ 3.86, copper ≤ 1.05 × 23.31 mm,
byte-identical twice. Both arms of blinky stay in the suite until S9. The other four boards are on
the same plan and the same bytes. **All five boards build and pass DRC.**

**Risk.** Medium. blinky is four segments, so this proves the pipeline runs end to end and almost
nothing about quality. Its value is making the two-arm mechanism real and reviewable before a hard
board depends on it.

### S7 — buck and ds2 flip *(3–4 weeks)*

**Changes.** Via sites introduced where planarisation reports a forced crossing, bounded by
`antipad_clash` and `via_pitch`. **The per-face inscribed-width test** — the corridor width through a
triangle at a given (entry slot, exit slot) pair, which is the hole `fits` structurally has. The
two-layer pour producer (outline only; KiCad fills; `pour_raster` predicts). The `current` solver.
`Board(router="pcbc")` on buck and ds2.

**Proof.** **ds2 `REFP_F` detour 3.23 → ≤ 1.35** — 43 mm, 9 % of a real board, and the number nothing
else in this repo can move. buck at or under its KRT arm on every DRC type, on deg/mm (18.32), on
copper (147.37 mm × 1.05), and on `via_parallelism`. `leftover_mm == 0` on both. Byte-identical
twice. The other three boards untouched and still green.

**Risk.** High. This is where the design meets a real two-layer board and the recorded precedent is
against it (§6.2). The face test is the specific mitigation and it is unwritten and unmeasured.

### S8 — c3_usb and node flip *(3–4 weeks)*

**Changes.** The `pair` solver end to end, with multi-terminal pairs handled honestly: c3_usb's
`USB_DP` has five pads because a USB-C connector carries both orientations and an ESD array has two
sides, so the pair is a Steiner tree with a coupled trunk and uncoupled stubs, and the stub length is
what `uncoupled_mm` bounds. Four-layer plane pours and the tap pattern consuming corridors rather
than running as a separate stage. `Board(router="pcbc")` on both.

**Proof.** **node's skew 11.9196 mm → ≤ 0.5 mm and its uncoupled length 22.81 mm → ≤ 2.0 mm, read by
KiCad** — the arbiter's own number, 23.8 × over today. c3_usb 0.6154 → ≤ 0.5. `leftover_mm == 0` on
both; plane islands and plane area not worse than the KRT arm; 0 unconnected. Byte-identical twice.

**Risk.** High, and the risk is concentrated in pours, not pairs. If the outline-plus-KiCad-filler
producer does not hold, these two boards stay on the KRT arm — and then the skew defect survives,
which is the quality judge's standing objection to this architecture. Partial answer already banked:
the pair-layers fix lands in **S1**, so the 90 Ω pair stops being routable through the planes in the
KRT arm whatever happens here. Full answer: if node cannot flip by S8, the pair solver is not
finished and S9 does not run.

### S9 — KRT deleted *(1 week)*

**Changes.** Remove `krt_plan`, `KRT_REPO`/`KRT_SHA`, `krt_missing`, `krt_home`, `krt_version`,
`write_fab_overrides`, `_lost`, `copy_with_siblings`, `lock_copper`, the 21 CLI flags, the `krt`
pytest mark, `Board(router=)` itself, and the nine documented workarounds for KRT's behaviour — the
`(locked yes)` byte position, the `local_hops`-not-`local_nets` naming, the `--ordering` scope, the
`--clearance` ceiling, the `--same-net-pad-clearance -1`. `route_embed` loses `RELAXABLE`,
`chains_of` and `_reason_of`; `strip_segments`/`replace_segments` survive for `pcbc route --net NAME`.
`PCBC_PATTERNS=off` retires with it — there is no pre-router stage left to roll back.

**Proof.** The full suite green with `KRT_HOME` unset and `~/Downloads/KiCadRoutingTools` renamed.
All five boards: 0 DRC errors, 0 unconnected, `leftover_mm == 0`, byte-identical twice, and every
recorded ceiling in `tests/test_examples_fab.py` at or below its KRT-arm value.

**Risk.** Low by then — S6–S8 did the work and this stage only removes code. The real risk is that it
is never reached: if any board is still on the KRT arm, this stage does not ship, and that is the
honest outcome rather than a crisis.

### S10 — OpenESC-20×20 *(unestimated; blocked on roadmap items 1 and 2)*

**Changes.** Not router work first: hierarchy (four identical motor channels is four copies of every
part, `NetReq`, `Place()` and `Chain()` today), six-layer stackups with real fab data, 2 oz copper,
and `NetReq(amps=)` above its 30 A ceiling with the IPC-2221/2152 and via-ampacity arithmetic proved
at 40 A. Router-side: six per-layer graphs, layer cost driven by copper weight, and the `current`
solver's pour realisation.

**Proof.** The board describes, places, routes and passes KiCad DRC with 0 unconnected, with every
phase conductor's `ampacity.power_bottlenecks` clear at 40 A and `via_parallelism` clear on every
layer change. Wall clock published, not hidden. The five example boards still build byte-identically.

**Risk.** Highest, and partly outside this design: a 40 A phase output is a *polygon*, not a track.

---

## 6. Open problems

### 6.1 The face — the design's own weakest point, unsolved

Gate capacity is checked per gate and never per triangular face. `fits(scene, gate, members)` asks
whether a set of nets fits across one channel; it never asks whether a path entering face *t* through
gate *g1* at slot *i* can *leave* through gate *g2* at slot *j*. A triangle whose three gates are each
individually open can still pinch at width. This is not theoretical — it is what buck's 29 %
false-accept rate on the 20-point anchor grid measures, and S4's acceptance only promises ≤ 10 %. At
10 % on ds2's 43 links, four nets have a corridor phase A proved and phase B cannot embed. Under the
whole-board rule those are four refusals a human must move for, on a board KRT routes silently — the
tool would be strictly worse from the user's chair even though the architecture is strictly better.
The per-face inscribed-width test in S7 is the mitigation and it is **unwritten, unmeasured, and the
hardest geometry in the design**: a funnel problem over convex hulls with per-member clearances, not a
scalar.

**Fork A makes this worse, not better, and that is the cost of the verdict.** A placement solver whose
objective is phase A maximises phase A's opinion, so it drifts systematically toward placements phase A
likes and phase B cannot embed — a proxy failure mode wearing the router's clothes. The face test is
the only thing that closes it, and it is unwritten.

### 6.2 The only prior evidence points the wrong way

The only from-scratch search anyone in this repo has built completed **29 of 43 links on ds2** and 13
of 20 on buck — one layer, no vias, no fanout, no rip-up — and emitted copper at **44.2 deg/mm**,
worse than KRT. This design's answer is that the prototype had no corridor (it searched geometry
directly) and no negotiated congestion, so its completion rate is not this design's prior. **That is
an argument, not a measurement, and it stays an argument until S7.**

### 6.3 The via price is a guess — and the owner should decide how it is set

Every other term in the phase-A cost is a length the board already carries in nanometres, with a
source. The via price is a number someone picks, and it silently decides how often the router changes
layer — which on a six-layer 2 oz board with planes to punch is one of the two or three decisions
that determine whether the board is any good. The proposal is to **sweep it across all six boards and
publish the table before S7**, rather than tuning until buck looks nice. Owner decision: whether a
published sweep is enough, or whether the via price should be a declared board-level number.

### 6.4 Phase A's convergence is weaker than the relaxer's

`route_relax` terminates because `quality` strictly decreases and is bounded below; its cap bounds a
bug. PathFinder has no such potential — `_ITER_CAP = 30` is a real stopping condition, and "not
converged" is a refusal the user sees. On a dense six-layer board with four identical channels
competing for the same corridors, non-convergence is the likely failure, and the message it produces
may not be actionable: the real answer might be "this board needs another layer". §3.2's `current`
solver can say `Board(layers=8)` for a cross-section shortfall; nothing in the design derives it for
a *signal* congestion failure.

**Fork A adds a second consequence that was not visible before.** A non-converged phase A returns a
score that is **not comparable between two candidate positions**, so a placement solver that silently
ranks a converged result against a non-converged one is worse than one that never pretends. S5 must
therefore report the fraction of candidates on which phase A hits `_ITER_CAP`, and a non-converged
candidate must be ordered *after* every converged one rather than by its number. Above a few percent,
the router-as-objective is unusable regardless of its cost.

### 6.5 Plane pours are not subsumed, and may not move

`route_planes.py` is 43 flags. The plan is that pcbc writes the pour outline and KiCad's own filler
fills it, and that is a different producer rather than a port. If pours are still KRT's when
everything else has moved, S9 does not run. **Owner decision:** whether "pcbc routes, KiCad pours,
KRT gone except for nothing" is acceptable as the end state, or whether a pcbc pour producer is
required before KRT can be deleted.

### 6.6 `spacing_w` stays out of v1, deliberately, and that is a decision to revisit

`Constraint.spacing_w` is documented "a router cost in R3" and has zero consumers. Wiring it into
`demand_nm` as `w × spacing_w` is one line and is **refused here**, because `patterns/stitch.py:688`
records it as measured **not even monotone** — ds2's `AIN3` goes 59.9 % clear to 28.2 % when widened
to 3W. Promoting it to a capacity floor would make the tool refuse, with a pasteable `Place()` move,
channels KiCad passes. As a soft parallel-run cost it is unmeasured. Owner decision: whether to fund
the measurement, or to leave the field carried-and-unused.

### 6.7 The homotopy guard's fire rate from a seed is unmeasured

Measured zero on existing copper, across 75 chains. That regime — KRT's loose copper, where the word
at zero slack is empty — is the wrong one. On a taut seed it may fire constantly, and every fire is a
pull rejected, which turns the embedder back into a shape-worse realiser. Unmeasured until S6 runs.

### 6.8 ds2 is not in this repo

`tests/test_bridge.py:32` points at `~/Documents/MaD/Hardware/DS2Addon/pcbc` and every ds2 test is
`skipif`-guarded. "All five boards byte-identical" is really four boards in CI plus one that exists on
one machine — and ds2 carries this plan's single most persuasive number (`REFP_F` 3.23 → ≤ 1.35). S1
adds an in-repo fixture reproducing the detour class. Whether that fixture is *enough* — whether a
synthetic board can stand in for a real one in the acceptance of S7 — is not settled.

### 6.9 Throughput in Python, and the Rust port nobody has costed

*Narrowed by Fork A.* The vague half of this is settled and removed: the per-candidate cost of a map
is no longer an unknown but a measurement — 12.1 ms on buck, 52.2 on c3_usb, 69.9 on node for
`build_scene` + `channels()` across all layers, against 0.08–0.57 ms for one full-graph Dijkstra
(§0). What remains open is unchanged and real: `components()` is O(k²) in a net's items with a `sqrt`
per pair (241 ms over routed node's 18 nets); six layers × a few hundred nets × up to 30 negotiation
iterations is minutes at best, and S10's OpenESC is both the board where placement solving matters
most and the board where the inner loop is least affordable. `route_geom.py` was written stdlib-only,
importing nothing from pcbc, precisely so it could be ported to Rust — and that port is **uncosted in
this plan**. The incremental-map half has been promoted out of here into §6.13, because Fork A made it
a blocking item for a stage rather than a background worry.

### 6.10 204,950 lines and 147 boards of tuning do not reproduce in nine stages

Stated plainly because it is the thing most likely to be forgotten. The gate in §4 — no per-type DRC
count above the KRT arm, ampacity clear, detour and deg/mm and copper at or below — is the only
defence against quietly shipping a worse router with a better architecture, and it is designed to
fail rather than to pass. KiCad, not KRT, has always been the arbiter, and condition 2 is the
arbiter's own verdict.

### 6.11 Where `escape_deficit_nm` belongs in the score — owner decision

Fork A's three verifiers agreed the term is dangerous as written and split three ways on the remedy,
so this is the owner's call and the positions are laid out rather than averaged.

The problem: in `(sealed_pads, escape_deficit_nm, corridor_overflow, corridor_length_nm, via_count,
candidate_index)` a proxy term in nanometres outranks **every** router term. `route_channel.Escape.slack`
is a *signed* integer and `moves()` only reports it when negative, so both a signed and a clamped
reading are live in the code today. Under the signed reading the term decides almost every comparison
at nanometre resolution and phase A never gets a vote — precisely on the crowded boards where
placement matters.

- **Delete it from the key** (V1): a pad that cannot escape on any allowed layer is a *wall* phase A
  refuses on (§3.1 step 4), and `sealed_pads` above it already carries the refusal, so the term is
  redundant. Keep it for the refusal sentence only.
- **Clamp and keep** (V2): make it a shortfall floored at zero, which is sound, and keep it as a cheap
  guard above the router terms.
- **Inert today, a veto tomorrow** (V3): measured over freshly placed scenes, the term is constant
  across candidates on every board in the repo — blinky 4 escapes / 0 sealed / min slack 9.093 mm,
  buck 28 / 0 / 0.907, c3_usb 82 / 0 / 1.070, node 120 / 0 / 0.909, with `moves()` emitting 0 on all
  four. No pad on any board is within 0.2 mm of sealing, so today it decides nothing either way — but
  S10's six-layer OpenESC is exactly the board where pads *would* be tight and the proxy *would* take
  the veto back.

All three agree on the floor, so the clamp ships in S5 regardless (§3.3). The open question is only
whether the term stays in the key above `corridor_overflow`.

### 6.12 Ghost demand — the experiment that would reopen Fork B

Phase A is blind to the nets it does not route only because nobody has written the reservation pass.
Entering every non-routed net as a ghost member of each gate its airwire crosses — contributing
`demand_nm`, committing no copper — would let a *partial* router negotiate against the whole net set,
which is the exact property Fork B's verdict says only a whole-board router has. If it works, staged
per-class handover is survivable and `Board(router=)` need never exist; the whole-board verdict is a
self-imposed delay. The experiment is specified in §0 (Fork B, "what would overturn it"); it is
weakened in advance by §6.7's problem — the midpoint polyline is a lower bound, so the reservation
would be against a bound rather than a route, and KRT would not honour the corridor anyway. It is
unfunded and unscheduled, and the verdict does not depend on it.

### 6.13 There is no incremental map, and Fork A made that blocking

`channels()` rebuilds `by_pt` from scratch and calls `triangulate(pts)` per layer on every call; it
has no cache, no incremental update and no `ignore=` before S1, and its only production caller is
`cli.py:329`. `build_scene` parses footprints out of `pcb_text`, so there is no scene-mutation path
for "move one part and re-evaluate" at all. Moving a part changes the point set, so **every candidate
position invalidates the map** — which means §3.1's cost argument ("map once, ≤ 50 ms", justified by
"after fanout the obstacle set is frozen") does not transfer to placement, the one phase where the
obstacle set changes by construction.

The lever exists and is unbuilt: `triangulate` is Bowyer-Watson, incremental by construction, walking
from the previous triangle (`route_channel.py:185`), and a part move was measured to perturb only 4–7
of ~200–1,100 gate names (§3.3). A local insert/delete around one part is therefore geometrically
available. **The number that decides how much of this design is affordable:** time a candidate re-map
against the measured 69.9 ms full rebuild on node. Under ~2 ms, phase A becomes affordable on hundreds
of candidates, tier 0 shrinks to the courtyard test, K stops mattering, and Fork A is moot. Above
~300 ms on a dense six-layer board, neither tier is affordable and the incremental map is not an
optimisation but the prerequisite.

### 6.14 Tier 2 scores a pre-fanout scene

`fanout_pieces(design, job, text, …)` consumes the *placed* board text, and fanout runs after
placement and before the router steps (`route.py:493-524`). So tier-2 phase A necessarily runs on a
scene without the escape stubs and staggered vias the router will actually route around. §3.1's claim
that "the objective literally is the router" is therefore overstated: the objective is a
higher-fidelity proxy for the route than the alternatives, evaluated one stage upstream of it. Whether
that gap matters is unmeasured; the honest options are to say so (done, here), to run a cheap fanout
per candidate (unaffordable), or to make tier 0 reserve the lane the way `lane_rules` already does at
placement time (untried).

