# Native plan (N0): delete KRT, generate Python natively

Written 2026-09-25 from a read of the tree and the spikes. `docs/direction.md` wins over this file
wherever they disagree. The owner's two decisions this executes:

- *"I want the auto layout and placement to generate directly python natively. That is why we need
  to build our own tool."*
- *"Do delete today's path. Use native only."*

**What this document is not.** Nothing below §0 was built or measured when it was written (see *As built*). Only these numbers were produced
for it, by a regex over `tests/test_*.py` (a Python one-liner, `re.finditer(r"^def (test_\w+)\(")`
plus decorator / `pytestmark` scan): **761 test functions, 83 of them marked `krt`** (the 1011 in
`direction.md` §9 is the collected, parametrised count, not mine). Every other number here is quoted
with its source and is not re-measured. The spike results (buck 21/21, 15.36 deg/mm; node 105/110)
are **unverified** (`direction.md` §5): nothing in N0 is believed until its own acceptance run and an
independent refutation pass.

## As built (2026-09-25): where N0 differs from the plan below

The plan below is kept as written; this section is what was built, and it wins where they differ.
Every number here was produced by a command named beside it (scratchpad `native/`, and the tests).

**Deleted.** `src/pcbc/route.py`, `src/pcbc/blocking.py`, `src/pcbc/place.py`, `tests/test_route_plan.py`;
the functions of §1.2 (plus `build.seed_stage`/`_stamp_seed`/`non_layout_diff`, `apply.apply_job` and
its file-level helpers, `seed.seed_job`, `project.seed_pcb`, `fanout.fanout_copper`,
`route_relax.relax_board`, the `route_emit` text writers, `patterns_off`/`empty_plan`/`PatternPlan.text`,
`layout_job`'s normalise/`_drop_dangling`/`_role_groups`/`ROUTER_NAME`/`SIDECAR*`,
`CompiledJob.krt`/`net_order`, `BoardSpec.net_order`, `NET_ORDERS`, `trace`'s KRT record, `fab`'s
KRT exemptions for `dangling` and `diff_pair_gap`, `validate_core`'s KRT parenthesis refusal), the
`krt` pytest marker, `KRT_HOME`/`PCBC_REQUIRE_KRT`/`PCBC_KRT_TIMEOUT`/`PCBC_PATTERNS`, the CI install
step. The text writers the tests still need to build fixtures live in `tests/boardtext.py`, test-only.

**Built.** `place_native.py` (placement as layout objects; `geometry_uuids`), `route_cost.py` (the
cost-field router), `route_native.py` (the route stage, net order, links, pours, moves, problem files,
`--replay`, `prune_dangling`), `route_pair.py` (a pair as one object), `moves.py` (`move_line`, moved
out of `blocking.py`); `layout_job.py` rewritten to §2.1; `trace.py` gains the event log
(`events`, `mark`, `note`, `code_sha`); `tests/test_native.py` (P1, P2, P3 on an unfinished board),
`tests/test_route_cost.py`.

**Where the build departs from the plan, and why**

1. **`NetReq(autoroute=False)` is not "leave the net alone"** (§1.2 was wrong). Every preset that sets
   it (`switch_node`, `analog`) is a *sensitive* net the old plan routed first on its own layers; its
   constraint (`vias=False`, one layer) is what the router honours. Every net with two pads is
   routed; the constrained ones go first. Found when buck's `SW` and `FB` were left unrouted.
2. **Net order is `ORDER = "constrained"`**: constrained nets (one layer, no vias, pairs) first, each
   group by `(-width_mm, net)`, then everything else by `(-width_mm, net)`. No pad-count tie-break.
   Measured against plain width order on the five boards (`native/exp.py`) and kept because it routed
   more links. Pairs are routed before any single net (`route_pairs`).
3. **Links are Kruskal over terminals, not Prim over pads.** Terminals are the net's pads **and every
   via already on the net** (`ViaTerminal`, a fanout escape or a tap), grouped by connectivity
   (plane included), declared `Chain` stations first. A fanout via is continued by the router
   instead of being left dangling.
4. **A via the router cannot join on a second layer is not written** (`prune_dangling`, after relax):
   c3_usb had 3 and ds2 8 `via_dangling` warnings before it, 0 after (`native/exp.py`). The fab
   stage's old exemption for dangling copper is deleted, so KiCad's `via_dangling`/`track_dangling`
   would now fail a board.
5. **Pairs** (`route_pair.py`): the centreline is routed at the pair's envelope width with virtual ends
   (free lattice cells within `PAIR_REACH_MM` = 1.5 mm of each station's midpoint), legality judged on
   the two offset halves, halves trimmed by arc length (`TRIM_STEP_MM` = 0.1), joins to the pads routed
   by the single-net router on a `Scene.fork()`; pitch = pair width + max(gap, table clearance) +
   4·EPS. `build._pair_gate` fails a board whose uncoupled length exceeds `PairSpec.uncoupled_mm`
   (skew reported, not gated). N0 does not meet that budget on c3_usb or node (see the table).
6. **`_chain_gate` is fatal for every chain** (it was report-only for KRT's copper).
7. **The emit mark** (`trace.mark("emit")`) is placed the moment `emit_board` has produced the board
   text; the in-memory decompile that checks the transcription runs after it. `decompile` is kept as
   the post-emit reader (§2.3's recommendation; the owner question is still open) and as the M1
   probe's reader.
8. **`Board(net_order=...)` is refused** at load (§10 of the risks: refused, not reinterpreted).
9. **The stamp carries `tools["pcbc"]` = a hash of pcbc's own source** (`trace.code_sha`), so a code
   change is stale from route; the route record asks for no placed record.
10. **An unrouted build** emits, fills and runs every gate on the emitted board, then fails with
    `unrouted: <nets>`, one move per failed link, `routed/unrouted/<net>-<k>.json` and no stamp, no
    `fab/`. A gate that would have failed is appended as `also:`. KiCad's unconnected nets must be a
    subset of the router's unrouted nets (else "pcbc router bug"). The KiCad canary is not required
    to fire when its own net is the unrouted one.
11. **Power vias.** The router's via is the net's compiled via (`Constraint.via`), 0.8/0.4 mm on the
    power classes, so a 2 A rail's single-via change is under-rated (buck `VIN`: 0.871 A per barrel,
    3 needed). The router writes one barrel per change; the rung shortfall is the fab power move it
    was before, not a routing failure (`test_route_verify_stitch.py`, re-recorded).

**First refutation round (2026-09-25).** Seven majors were reproduced (`scratchpad/native/fix1/repro.sh
before`) and fixed; each is pinned in `tests/test_native_refuted.py` or the test named:

12. **An unfinished net ships no generated copper** (§5 is per net). `route_native.drop_unfinished`
    removes every piece of every net with a failed link — routed links, fanout escapes, taps, hops —
    before the pieces become objects; its `layout.core.py` lines stay. A closing move line says how
    much was dropped. c3_usb and node had 13 and 14 `VBUS` object lines in `layout.gen.py`; now 0.
13. **A free end of a locked core track is a routing terminal** (`EndTerminal`). A component holding one
    is reached only at its free ends; an end the spanning tree did not use is linked afterwards to the
    nearest terminal that was not its own copper. The §7 loop (copy a gen line into core, move a part in
    `board.py`, rebuild) failed with "joins nothing"; now it builds to fab (the refuters' E10, E2, E3).
14. **Moves.** One move per missing link (a spanning forest over the failed attempts; node printed three
    for two). The no-via move names the `NetReq` line the net is on, says to split it out of a shared
    line, and offers `layers=` with `vias=` when the net has one layer (ds2's `REFN_F`: the printed move
    applied as written passes `pcbc check` and **the ds2 copy then builds to fab** — `fix1/repro2.sh`;
    the MaD board itself was not edited). A pattern that would put a via on a `vias=False` net is a soft
    refusal naming that line (`patterns._no_banned_vias`), not "internal router error". A via end is
    printed once; a no-number pad no longer reads as a part with no `Place()`.
15. **`--replay` routes with the core file** (`layout_job.route_in_passes`, shared with the build).
16. **Emit may change nothing but layout objects** (`build.non_layout_diff`, restored natively): the emit
    against the in-memory placed board, and again after KiCad's refill-and-save against KiCad's save of
    the placed board (0 differences on all five boards, 0.43-0.49 s each, measured before wiring it in).
17. **`power_bottlenecks`** keeps a declared power rail with two or more pads and no copper (it reads
    `open`, where it used to drop out of the table); a through via's layers are the board's copper layers
    (buck's move said `In1.Cu/In2.Cu` on two layers).

**Second refutation round (2026-09-25).** Seven majors and one minor, each reproduced first
(`scratchpad/native/fix2/before/`), fixed, re-run (`fix2/after/`), and pinned by a test shown to fail under a
mutation that blinds it (`fix2/mut/`):

18. **The router reads the Python model** (`docs/direction.md` §1). `route_scene.build_scene(design, job, cs,
    feet)` is built from `Placement.feet` — each part's `.kicad_mod` parsed once (`foot_native.lib_foot`,
    `pads.PadSpec`) and posed by its `Pose` — plus keepouts, rule areas and the `Board()` outline. Fanout, the
    relaxer, the dangling sweep and `pattern_copper` take no text. Before: the route stage parsed the in-memory
    board text 133 times on buck (`fix2/r1_probe.py`); after: 0. The switch was proved before the text path was
    deleted: the two scenes equal item for item on all five boards (`fix2/scene_eq.py`), and a native build of
    all five byte-identical to the text-path build in every file but the stamps' code hash. The one difference,
    in `Foot.box` only, is the text path's: it read a rotated part's pad box with the pad's board angle
    (`SW_BOOT`/`SW_RST`, node's `U5`). The placer still read the emit base text for its own search, its reports
    and the router's `Pose`s (finished in item 29). Tests read fixture board files back
    through `tests/boardtext.scene_from_text` (test-only). Pinned: `tests/test_native_scene.py`.
19. **A lock's free end stays joined.** The router joined both free ends of a locked core track to one pad (a
    loop) and the relaxer's trim dropped one link: the old rule held a run to *the track*, anywhere along it.
    `route_relax.free_ends_held` pins an end of immovable copper that only the run keeps joined, and
    `_core_ends_lost` raises a pcbc bug if a pass ever opens one. The `track_dangling` refusal of a core line
    now names the router's note when it could not join the end, and says "pcbc bug" otherwise (it used to
    blame the author's line). Pinned: `tests/test_native_gates.py`.
20. **The pair gate is proven fatal** on `tests/fixtures/pair/pair.py` (everything routed, pair uncoupled
    4.1065 mm against 1: stops; against 40: fab).
21. **The plane gate and the thermal verdicts are asserted on the emitted boards that stop before fab**
    (`test_examples_fab.UNFINISHED_PLANES`, `UNFINISHED_THERMAL`), and a 3V3 track cut across node's In1.Cu must
    make `_plane_gate` report the GND plane split.
22. **The thermal array's fab note reaches fab** on `tests/fixtures/thermal/thermal.py` (24 barrels, IPC-4761 Type
    VII). At 0.35 W the build stopped with "KiCad reports GND unconnected and the router reported them routed".
    Corrected by the third refutation round: the array got its 9 barrels, in 6 of the 9 blocks — a 3V3 B.Cu
    track under the top row refused every first-rank site there and the rank-major walk placed its 9 before it
    tried their second-rank sites — so 3 blocks had none and 2 were left unconnected, and the router, taking
    the nine blocks of pad `49` for one terminal, called GND routed. Fixed in item 27.
23. **The checker is graded on native copper**: `test_route_scene`'s agreement test builds each board natively and
    asks it again with three errors planted (`tests/boardtext.plant_errors`).
24. **Every removal by the dangling sweep is a note and pinned** (`route_native.prune_dangling` records why; ds2's
    five fanout vias and three stubs in `test_patterns.DS2_PRUNED`, none on the other four). `DS2_BAR` is
    re-recorded natively and asserted again.
25. **Docstrings.** No line in `src/pcbc` describes KRT (117 lines rewritten or cut), nor `route.py`,
    `krt_plan`, `gnd_pour` or `PCBC_PATTERNS` as current.

**Third refutation round (2026-09-26).** Five majors and ten minors; each major reproduced first
(`scratchpad/native/fix3/before/`, on a snapshot of the source), fixed, re-run (`fix3/after/`), and pinned by
`tests/test_native_refuted3.py`, every pin shown to fail under a mutation that blinds it (`fix3/mut/muts.log`,
`muts2.log`: 19 mutations, all caught once the two first survivors — the overlap merge, and a lock island
counted as a missing link — got pins on the unfinished boards and on a lone via by `FID3`):

26. **A lock's free ends are accounted for.** A core via joined on fewer than two layers is a routing terminal on
    each layer nothing of its net joins (`route_native.EndTerminal`, kind `via`; `via_joined`): buck's `5V` pad
    escape + via, via-track-via and a lone via were refused as `via_dangling` and now build to fab. The route
    stage records what became of every free end the locks had (`core_end_states`: joined, dropped with its
    unfinished net, one of the unfinished net's missing links, a spur with nothing to join, a search that found
    no path, or lost after joining), and the build words `track_dangling` / `via_dangling` on a core line from
    that record (`build._dangling_core_move`) — "pcbc bug" only when the record says joined and KiCad disagrees.
    A via's free layer does not fix the way its component is reached (it is often welded to a pour that
    joins half the net); a locked track's free end still does. Locked copper that no pad reaches and no
    link can reach is the lock's finding, not a missing link of its net: the net keeps its copper and the
    build names the line (a lone `GND` via by buck's `FID3` used to drop all of GND's generated copper).
    Generated copper ending on a lock's free end is never cut by the overlap merge (item 31).
    Before: a spur (`BOOT` pad to pad plus a stub off it) and a stub on c3_usb's unfinished `VBUS` were both
    refused as "a pcbc bug"; after: "a spur ... has nothing left to join ... end the line on the copper it joins,
    or delete it" and "... was joined by a link that was dropped with the unfinished net VBUS ... the line is
    not at fault".
27. **The router counts pads as KiCad does.** Root cause of the thermal fixture's "router bug": `_net_groups`
    unioned every pad with the same number and `patterns.terminals` merged them, so the ESP32's nine `49` blocks
    were one terminal. `route_scene.Item.block` names the `(pad ...)` a primitive belongs to, `_net_groups`
    unions a pad's primitives only, and `route_native.pad_terminals` makes each block group a terminal
    (`U1.49`, `U1.49@(x,y)`). The fixture with no `Thermal()` went from GND links [0, 0] and 8 unconnected items
    to [8, 8] and fab; c3_usb and node now join their nine blocks with 8 short F.Cu links each (29/31 and 33/35
    router links; KiCad's own count unchanged, 73/79 and 104/110).
28. **The router's connectivity claim is checked against KiCad's on every build.** `route_native.open_links`
    counts, per net, the links still open on the copper the stage hands on (per pad block; a pour joins its
    net's copper on its layer). A net the router reports routed with any open is a pcbc bug raised in the
    stage itself; the build holds the count to KiCad's unconnected items per net (`netcheck.unconnected_by_net`)
    and stops on any difference ("the router's count of open links is not KiCad's"). Five boards: equal on
    every net (VBUS 6, VBUS 6, REFN_F 3, nothing else).
29. **Placement reads the Python model.** `place_native.place` searches over each part's `.kicad_mod` read once
    (`foot_native.LibFoot`, now carrying the CSS boxes, the silkscreen extents and pads, its uuids) posed from
    its seed spot (`seed_feet`): `pcb_place.resolve_places(seeds)`, `silk.silk_plan`, `layout_report(posed)`,
    `route_checks.build_ctx(posed)` (both via `pcb_place.posed_feet_of`, the one reading the router's scene also
    makes). The `Pose`s are the resolved `Place` objects (`poses_of_places`), never a board text read back.
    `Placement.text` is the emit base, built lazily for emit (`emit_base`, writes only); `layout_job.load_core`
    takes the uuids from `place_native.foreign_uuids` and the layer table from `seed._layers`. Proof: the old
    and new `place()` agree on poses, reports, notes, applied places, keepouts and the emit base's sha256 on
    seven boards (`fix3/native/cmp_place.sh`); `foreign_uuids` equals the text reading on seven
    (`uuid_eq.py`); the refuter's model tracer counts 0 board-text calls in `place`, `load_core` and the router
    on all five (`fix3/native/mtsum2.log`; before: 496 / 1823 / 3529 / 7595 / 4270 in `place`).
30. **The thermal land takes a barrel in every pad.** The walk gives each pad of a multi-pad land its sites in
    order until one takes a barrel, then the rest rank-major (`patterns.stitch.run`); a pad left bare is a
    `bare` verdict (`route_verify.ThermalArray.bare`) that fails the gate with the pattern's note naming the
    blocker and the move. The fixture at 0.35 W: 9 barrels in 9 pads, fab. The fab note is written from the
    board's own rows (`route_verify.thermal_rows`): at 2 W it said 47 barrels / 9.83 C (the budget) and now says
    30 / 7.7 K/W / 15.41 C and "Short ... +5.41 C over its budget".
31. **Same-net copper laid over itself is resolved** (`route_native.merge_overlaps`: a contained segment goes, a
    fold takes the container's tail with it, a partial overlap is cut back): c3_usb 4.622 mm, node 0.974 mm and
    ds2 3.487 mm before, 0 after, pinned per board.
32. **Minors.** The fix-2 table's KiCad links re-recorded (above); the pair gate's same-layer rule, the fab note's
    absence on a board with no `Thermal()`, and the prune sweep's pour exemption each have a test; `DS2_OWNS`'
    docstring says 9 + 9 and pins it; `route_scene.build_scene`'s one-reading claim is now true.
    Found, not fixed: the router treats `Keepout(no=("via",))` as a full copper keepout (`fix3/after/thko`).

**Acceptance run (2026-09-26, after the third refutation round).** `acc_build.py` under `fix3/acc/a` and
`fix3/acc/other path/deeper/b`, `fix3/table.py` (KiCad links: KiCad's unconnected items on the emitted board
with tracks, vias and zones stripped = total; routed = total less the full board's).

| Board | Result | KiCad links | Router links | Unrouted | DRC errors | Unconnected items | Router open links | deg/mm | Vias | mm | Overlap mm | Two paths |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| blinky | fab | 1/1 | 0/0 | - | 0 | 0 | - | 3.86 | 1 | 23.308 | 0 | 33 files identical |
| buck | fab | 21/21 | 11/11 | - | 0 | 0 | - | 12.53 | 11 | 99.583 | 0 | 33 files identical |
| c3_usb | unrouted | 73/79 | 29/31 | VBUS | 0 | 6 | VBUS 6 | 12.07 | 53 | 273.837 | 0 | 9 files identical |
| node | unrouted | 104/110 | 33/35 | VBUS | 0 | 6 | VBUS 6 | 10.34 | 82 | 377.287 | 0 | 9 files identical |
| ds2 | unrouted | 48/51 | 38/39 | REFN_F | 0 | 3 | REFN_F 3 | 10.38 | 24 | 411.804 | 0 | 8 files identical |

**Acceptance run (2026-09-25, after the second refutation round).** `acc_build.py` under `fix2/acc/a` and
`fix2/acc/other path/deeper/b`, `acc_compare.py`. KiCad links re-recorded by the third refutation round: total =
KiCad's own unconnected-item count on the emitted board with every track, via and zone stripped (per pad
*block*: KiCad counts each of the ESP32's nine `49` blocks), routed = total less KiCad's unconnected items on the
full board. `fix2/table.py` had deduplicated pads on (ref, number), collapsing the nine blocks into one, and
subtracted a count made per block: c3_usb and node read 65/71 and 96/102, KiCad's own numbers are 73/79 and 104/110.

| Board | Result | KiCad links | Router links | Unrouted | DRC errors | Unconnected items | deg/mm | Vias | mm | Two paths |
|---|---|---|---|---|---|---|---|---|---|---|
| blinky | fab | 1/1 | 0/0 | - | 0 | 0 | 3.86 | 1 | 23.308 | 33 files identical |
| buck | fab | 21/21 | 11/11 | - | 0 | 0 | 12.53 | 11 | 99.583 | 33 files identical |
| c3_usb | unrouted | 73/79 | 21/23 | VBUS | 0 | 6 | 12.07 | 53 | 262.659 | 9 files identical |
| node | unrouted | 104/110 | 25/27 | VBUS | 0 | 6 | 10.75 | 82 | 362.972 | 9 files identical |
| ds2 | unrouted | 48/51 | 38/39 | REFN_F | 0 | 3 | 11.49 | 24 | 415.291 | 8 files identical |

**Acceptance run (2026-09-25, after the first refutation round).** Same commands, roots
`native/fix1/acc/a` and `native/fix1/acc/other path/deeper/b`; `fix1/acc/acceptance.json`.

| Board | Result | Router links | Unrouted | KiCad DRC errors / unconnected items | Segments / vias / mm | deg/mm | Two paths |
|---|---|---|---|---|---|---|---|
| blinky | fab | 0/0 | - | 0 / 0 | 4 / 1 / 23.308 | 3.86 | 33 files identical |
| buck | fab | 11/11 | - | 0 / 0 | 49 / 11 / 99.583 | 12.53 | 33 files identical |
| c3_usb | unrouted | 21/23 | VBUS | 0 / 6 | 127 / 53 / 262.659 | 12.07 | 9 files identical |
| node | unrouted | 25/27 | VBUS | 0 / 6 | 165 / 82 / 362.972 | 10.75 | 9 files identical |
| ds2 | unrouted | 38/39 | REFN_F | 0 / 3 | 144 / 24 / 415.291 | 11.49 | 8 files identical |

(The unconnected column now counts KiCad's unconnected *items*; with no `VBUS`/`REFN_F` copper left, more
pads are open. The unconnected *nets* are still exactly the router's unrouted nets on all three.)

**Acceptance run (2026-09-25, first cut).** Each board built to fab with `build_job(force=True)` from copies under
two absolute paths (`native/acc_build.py <root>`, roots `acc/a` and `acc/other path/deeper/b`), then
`native/acc_compare.py` (every file under `layout/<board>/` compared by sha256; `native/measure.py` on
the emitted board). KRT numbers are step 0's, `tests/fixtures/native/krt_baseline.json`.

| Board | Result | Router links | Unrouted | KiCad DRC errors / unconnected | Segments / vias / mm | deg/mm | KRT: segments / vias / mm / deg/mm | Two paths |
|---|---|---|---|---|---|---|---|---|
| blinky | fab | 0/0 (all hops) | - | 0 / 0 | 4 / 1 / 23.308 | 3.86 | 4 / 1 / 23.308 / 3.86 | 33 files identical |
| buck | fab | 11/11 | - | 0 / 0 | 49 / 11 / 99.583 | 12.53 | 73 / 7 / 147.374 / 18.32 | 33 files identical |
| c3_usb | unrouted | 21/23 | VBUS (J1 to U3 and J1 A/B side) | 0 / 2 | 140 / 53 / 289.671 | 12.97 | 302 / 55 / 358.883 / 35.43 | 10 files identical |
| node | unrouted | 25/27 | VBUS (J1, U2, U3) | 0 / 2 | 177 / 84 / 377.161 | 11.65 | 274 / 86 / 409.453 / 30.26 | 10 files identical |
| ds2 | unrouted | 38/39 | REFN_F (C1.1 to U1.8, one layer, no vias) | 0 / 1 | 147 / 24 / 419.472 | 11.69 | 227 / 27 / 501.772 / 15.36 | 8 files identical |

Gates on the emitted board: every gate passes on blinky, buck and ds2; on c3_usb the pair gate
(uncoupled 42.8 mm against 2) and the chain gate (`USB_DP` does not cross U3.1) fail; on node the
pair gate (uncoupled 29.6 mm against 2). KiCad's unconnected nets equal the router's unrouted nets
on all three unfinished boards. The unfinished boards write no `fab/` and no stamp.

**What N0 does not do yet.** No rip-up and reroute (a link that fails stays failed; the order is
pinned). VBUS at the USB-C connector on c3_usb and node, and ds2's `REFN_F`, are unrouted. The pair
coupling does not meet `uncoupled_mm` on c3_usb or node. No solved placement (poses are still
`board.py`'s `Place()` through the existing placer). `length_max_mm` is reported by KiCad, not
priced or enforced by the router. No re-route of a two-layer pour island. Core drawings on copper are
not router obstacles (core copper and core rule areas are). Silk reference positions are not gen
objects. `relax_pieces` is not gated by `NetCost`. P8 (the independent refutation pass) has not run.

---

## 0. The shape of the change in one paragraph

Today: `seed/layout.kicad_pcb` → `placed/layout.kicad_pcb` → KRT step files → `routed/router.kicad_pcb`
→ KiCad normalise → `gen.decompile` → `layout.gen.py` → `emit` onto the placed board. After N0:
`board.py` + `.kicad_mod` + `layout.core.py` → Python objects (Place, Seg, Arc, Via, Pour, Graphic)
→ `routed/layout.gen.py` → `emit` → `routed/layout.kicad_pcb` → KiCad fill + DRC + gates → fab. The
first `.kicad_pcb` any build stage writes is the emitted one. The one other KiCad board file that
exists before emit is `core_probe`'s M1 probe in a `pcbc-core-probe-*` temp dir: a validator whose
only output is pass/fail naming a core line. It never feeds generation, and the proof in §5 allows
that one path by name and nothing else.

---

## 1. The deletion list

### 1.1 Files deleted outright

| File | Why it is KRT/decompile-only |
|---|---|
| `src/pcbc/route.py` (1011 lines) | `krt_home`, `krt_python`, `krt_missing`, `krt_version`, `KRT_REPO`, `KRT_SHA`, `PCBC_STEP`, `pcbc_step`, `pin_copper_ids`, `lock_copper`, `krt_plan`, `krt_timeout`, `KRT_TIMEOUT_S`, `LOCAL_MM`, `_pour_flags`, `take_locked_pours`, `locked_pours`, `_local_nets`, `write_fab_overrides`, `route_job`, `_with_nets`, `_lost`, `_unreached_pads`, `_UNREACHED`, `_SUMMARY`, `_krt_summary`. Three things survive, moved into `route_native.py`: `copper_layers`, `bar_key` (as `piece_key` already exists in `route_emit`, keep one), and `_unrouted_move` (rewritten, §3.6). |
| `src/pcbc/blocking.py` | `step_boards` reads KRT step files; `blocking_lines` reads a board text plus a work dir of step boards. Replaced by `route_native.blockers(scene, net, ...)` over the live `Scene` (§3.6). `move_line` moves to `route_native.py` unchanged. |
| `tests/test_route_plan.py` (11 tests) | Every test asserts `krt_plan` argv. |
| `tests/fixtures/compiled/*.json` `"krt"` keys | Regenerated without the key (not the files). |

### 1.2 Functions, constants and fields deleted from kept files

| File | Delete | Keep / change |
|---|---|---|
| `layout_job.py` | `ROUTER_NAME`, `SIDECAR`, `SIDECAR_CONSUMED`, `_blocks`, `_piece_sig`, `_role_groups` (copper.json → groups), `_normalise`, `_drop_dangling`, `_missing_line`, `_first_uuid`, the `route_job` call and the `core.kicad_pcb` lock board in `layout_job`, `validate_core`'s "KRT's pour reader is not string-aware" parenthesis refusal (`layout_job.py:301-306`), the module docstring's steps 2-4 | Keep and make in-process: `load_core`, `validate_core`, `core_path`, `gen_path`, `LOCK_PASSES` + `adopt_key` + `signature` adoption passes (locks go into the `Scene` as locked items, not onto a board file), `_clearance_clashes`, `_unrouted_blockers`, `_unfillable`, `diff_objects`, `comparable`, `layout_uuids`, `roles_doc` (it reads the **emitted** board's groups: a checker), `board_layer_names` (from `seed._layers` only), `board_uuids` (from the in-memory base). `stage_outputs` shrinks to gen + emitted board + sidecars. |
| `build.py` | `non_layout_diff`, the `router_pcb` comparison, the `router_drc` comparison, `_stamp_seed`, `seed_stage`, the `seed` stage, every `seed/` and `placed/` sha in `_stamp`/`stale_reason`, `_sidecars`' "KRT reads the sidecars" rationale | `STAGES = ("check", "sch", "place", "route", "fab")`. Every gate after emit stays byte-for-byte. `decompile` stays **only** as a reader of the emitted board (the field-for-field fidelity check, `roles_doc`, `tap_stubs`) — see §2.3. |
| `gen.py` | `decompile(..., unique=False)` (only the router intermediate needed it), `split_core` (normalise only) | `decompile`, `write_gen`, `gen_source`, `line_of`, `assign_ids`, `census`, `unmapped` stay (importer, test tool, post-emit reader). |
| `route_emit.py` | `segment`, `via`, `piece_text`, `append_items`, `strip_segments`, `replace_segments`, `write_pieces`, `write_sidecar`, `read_sidecar` (text writers and the `copper.json` file) | `Piece`, `seg_piece`, `via_piece`, `piece_key`, `seg_key`, `census`, `census_by_net`, `Sidecar` (as the in-memory roles doc `roles_doc` returns). New: `to_copper(pieces, board) -> (list[Copper], list[Graphic])` (§3.3). |
| `route_scene.py` | `krt_grid` (and `fanout`'s re-export) — `Scene.grid` becomes `stack.track_min + stack.clearance_min`, the lattice pitch the spike used (`cf/router.py:91`), not KRT's grid | `plane_targets` stays the one pour rule; its docstring loses `krt_plan`. |
| `apply.py` | `apply_job` (file-in, file-out), `_apply_outline`, `_edge_rect`, `_apply_keepouts`, `_drop_named_zone`, `_apply_rule_areas`, `_apply_slot`, `_drop_uuid_rect`, `restore_design_rules`, the `.bak-pcbspace` backup | `_apply_places`, `_rewrite_footprint`, `_turn_pads` become in-memory transforms of a footprint block; `write_dru`, `_render_dru`, `_apply_pro` stay for the sidecars; `_rule_area_zone`/`_slot_rect` geometry becomes Python objects (§4). |
| `place.py` | `place_job(design, seed, out=)` (file-based) | Replaced by `place_native.place(design, job) -> Placement` (§4). |
| `seed.py` | `seed_job` (writes `seed/layout.kicad_pcb`) | `instantiate`, `_bind_pads`, `_absolute_models`, `drop_degenerate_lines`, `_layers`, `_nets`, `_tenting`, `emit_pro` stay; `emit_pcb`'s header half becomes `parts.base_header`. |
| `project.py` | `seed_pcb`, `"seed"` in path helpers, the KRT paragraph in `write_sidecars` | `layout_dir`, `copy_board`, `write_sidecars`, `PACKED` minus nothing. |
| `patterns/__init__.py` | `PatternPlan.text`, `empty_plan(text)`, `patterns_off` + `PCBC_PATTERNS` (it was the rollback *to KRT*; with no KRT there is nothing to roll back to), `hard_refusals`' `PCBC_STRICT_PATTERNS` stays (it is `--strict-patterns`) | `pattern_copper(design, job, cs, scene, board, stage=...)` — no text in, pieces out. `_STAGES` stays; the docstrings' KRT scheduling prose is rewritten to the native order (§3.4). |
| `fanout.py` | `fanout_copper` (text writer), the `krt_grid` re-export | `fanout_pieces(design, job, parts, board, scene, claimed)`. |
| `route_relax.py` | reading `bar_segments(text)`/`bar_vias(text)`, `replace_segments`, `_reason_of`'s "locked segment pcbc does not own is `lock_copper`'s" branch | `relax_pieces(design, job, cs, scene, pieces) -> RelaxResult` over `Piece`s. `RELAXABLE` keeps `leftover` (the router's copper, see §3.3). |
| `language.py` / `model.py` / `compile.py` | `NET_ORDERS`, `DEFAULT_NET_ORDER`, `Board(net_order=)` (a KRT `--ordering` flag; no example or the DS2 Addon uses it — `grep net_order examples ds2` is empty), `BoardSpec.net_order`, `CompiledJob.net_order`, `CompiledJob.krt` and its `to_dict` key | `Board(net_order=...)` becomes a load refusal naming the line: "net_order was the old router's flag; the native router orders nets by class (docs/native-plan.md §3.2)". `NetReq(autoroute=)` stays: `"diff_pair"` = route as one pair object, `False` = the router leaves the net alone (an unrouted move if it has no core copper). |
| `trace.py` | `krt_home_raw`, `krt_record`, `tools["krt"]`, `KRT_` in `ENV_PREFIXES`/`CHILD_ENV_PREFIXES`, the KRT drift line | Add an ordered event log (§5, proof P1). |
| `constraints.py`, `stackup.py`, `ampacity.py`, `route_channel.py`, `route_verify.py`, `patterns/*.py`, `copper.py`, `copper_bar.py`, `netcheck.py`, `fab.py` | Every message string that names `krt_plan`/KRT as the reason (e.g. `constraints.py:415,675` "is what makes krt_plan write the back pour") | Reworded to name `plane_targets` / the native route stage. Docstring history may keep the word KRT; code identifiers and user-facing strings may not (enforced by P2, §5). |

### 1.3 CLI flags, env vars, markers, fixtures, CI

| Item | Action |
|---|---|
| `KRT_HOME`, `PCBC_REQUIRE_KRT`, `PCBC_KRT_TIMEOUT`, `PCBC_PATTERNS` | Deleted; nothing reads them (P2 greps the AST). |
| `pcbc build --upto seed` | Gone with the stage. `--strict-patterns`, `--strict-power`, `--force`, `--upto {check,sch,place,route,fab}` stay. |
| `pcbc route` | `--channels` stays (reads the native `Scene`); its "use pcbc build" text stays. |
| `pyproject.toml` marker `krt` | Deleted. `--strict-markers` is already on, so any `@pytest.mark.krt` left anywhere becomes a collection **error** — the marker's removal is its own proof. |
| `tests/conftest.py` | `skip_krt` deleted. `placed_board(name, board)` returns the place-only **emitted** board (§4) — a checker-side file, fine for tests. `routed_board` builds natively and returns the emitted board **also when the only error is unrouted nets** (it raises on any other error); tests that need a complete board ask `complete_board(name)`, which raises "N0: <board> has unrouted nets" instead of returning. |
| `.github/workflows/test.yml` | Delete the "Install KiCadRoutingTools" step and `PCBC_REQUIRE_KRT`; `-m "not kicad and not krt"` → `-m "not kicad"`. `tests/test_ci.py` updated to match. |
| `README.md` | The KRT install block (lines 119-123) and every table row naming KRT (lines 160-225) rewritten to the native pipeline; rows pointing at deleted tests removed. |

### 1.4 Every test file

D = deleted, R = rewritten (named tests change), K = kept unchanged. "−krt" = drop the marker, the body
stands. "native-xfail" = `pytest.mark.xfail(strict=True, reason="N0 step k")` until the step that makes
it pass; strict means an unexpected pass fails the suite, so the mark cannot outlive its reason.

| File | | What happens |
|---|---|---|
| test_ampacity | R | `the_pour_move_is_only_offered_where_pcbc_actually_pours`: `krt_plan` → `plane_targets`. |
| test_bridge | R | 2 `krt` tests −krt; `the_fixture_builds_to_fab_with_the_gate_verified` native-xfail until step 5. |
| test_buck, test_c3_usb, test_circuit, test_copper_bar, test_fab_lcsc, test_fab_via_in_pad, test_netcheck, test_node, test_review, test_rules, test_sch, test_source, test_stackup, test_silk | K | No KRT dependency; `pcb_job`/`placed_board` callers see the same API (§4). |
| test_build_blinky | R | `upto_route`, `fab` −krt; **D** `the_step_after_the_last_router_step_writes_a_byte_copy_of_the_board` (KRT step files). |
| test_ci | R | Marker expression. |
| test_cli | R | `check_constraints_json...`: drop the `krt` key. |
| test_constraints | R | `..._keep_todays_classes_nets_and_krt` loses `krt`; `a_plane_the_router_will_never_pour...` asserts `plane_targets`. Fixtures regenerated. |
| test_copper_rules | R | `the_stackup_is_the_one_source_of_fab_limits`: drop the `krt_plan` argv half. |
| test_dru | R | 3 `krt` tests −krt, on native boards (ds2 ones read the emitted board even when unrouted). |
| test_examples_fab | R | Tables `BAR`, `DETOURS`, `DANGLING_REMOVED`, `LEFTOVER`, `REFUSED`, `SPINE_*`, `TAP_*`, `VIAS_PATTERN`, `OWNS`, `SOFT`, `RETURNS`, `PARALLEL`, `THERMAL`, `PLANES`, `SAME_NET`, `UNDER`, `BOTTLENECK` are KRT recordings: **deleted**, and re-recorded from native builds only for boards that complete (step 9). The test asserts: blinky, buck → fab; c3_usb, node → fab **or** an `unrouted:` error whose moves equal KiCad's unconnected set, no traceback, no `fab/`. |
| test_fanout | R | `fanout_copper` → `fanout_pieces`. |
| test_fifth_review | R | **D** `every_krt_step_reads_the_compiled_rules_beside_its_input`, `a_krt_step_that_does_not_finish_is_stopped_and_named`; `a_pour_is_adopted_only_when_every_field_is_the_build_s` −krt (adoption is kept); the 2 other `krt` tests −krt. |
| test_guard | R | `pattern_copper`/`fanout_pieces` signatures; `the_fixture_builds_to_fab...` −krt, native-xfail until step 5. |
| test_layout_gen | R | `gen_file_is_the_routed_board...` → "gen is the Python the stage wrote and core copper still emits". |
| test_layout_roundtrip (78) | R | 32 `krt` tests −krt (they test locks, gates, stamps, determinism, fab on the build: all kept mechanisms). **D** `the_emitted_board_is_the_router_s_copper_field_for_field`, `the_emitted_board_may_change_nothing_but_the_layout_objects` (`non_layout_diff`), `the_pour_step_pins_the_plane_inset_to_the_stackup` (→ R: pin the native Pour's inset to `edge_clearance`), `the_routed_boards_have_no_unmapped_field` (→ R: the emitted boards). Tests that need a complete board run on blinky/buck. |
| test_pattern_chain | R | `pattern_copper` signature only. |
| test_patterns (68) | R | **D** `krt_plan_drops_what_the_patterns_finished`, `pin_copper_ids_keeps_pcbcs_own_ids...`, `patterns_off_claims_nothing_and_is_a_rollback`, `a_pieces_key_survives_the_two_rewrites...`, `the_post_stage_runs_between_the_planes_and_the_signals`, `the_final_stage_is_the_last_step_of_the_plan...`, `krts_tap_step_runs_only_for_the_nets_the_pattern_refused`; **R** `the_ds2_addon_builds_to_fab_with_its_hops_in_it` → "ds2's hops are in gen" (no fab), `every_pattern_module_says_whether...` (drop the `krt_plan` half), `a_tap_the_pour_cannot_reach...`. New: `the_native_stage_order_is_pre_route_post_final` (§3.4). |
| test_pcb_place, test_route_checks, test_seed | R | Seed/placed reads come from `parts`/`place_native`; `test_seed` asserts `parts.base_text`, not `seed_job`. |
| test_planes | R | `the_route_plan_of_every_example_board_is_unmoved...` and `a_two_layer_board_that_declares_two_pours_gets_a_gnd_pour_step...` → assert the Pour objects in gen; fixture build −krt. |
| test_return_rules | R | One `krt_plan` assertion → `plane_targets`. |
| test_route | R | **D** `copper_ids_are_keyed_by_order_not_chance`, `route_without_krt_says_how_to_get_it`, `route_starts_from_a_clean_work_dir`, `a_net_krt_could_not_finish_is_reported_as_a_move`, `a_pad_krt_left_open...`; **R** `an_unrouted_differential_pair_is_a_move...`, `pair_false_is_the_no_op...` to the native move (§3.6); `blinky_routes_clean_and_the_same_twice` −krt. |
| test_route_channel | R | Reads `channels_doc(scene)` directly, not `copper.json`. |
| test_route_geom | K | |
| test_route_relax | R | `relax_board(text)` → `relax_pieces`; `a_relaxed_tap_keeps_its_reason_and_reports_the_step...` loses the step-file half. |
| test_route_scene | R | `build_scene` native signature; `the_sidecar_is_keyed_by_geometry_and_round_trips` **D**. |
| test_route_verify_chains, test_route_verify_stitch | R | Module `pytestmark` loses `krt`; they read `routed_board` (native, maybe unrouted). |
| test_seventh_review | R | 4 `krt` tests −krt; `the_route_stage_refuses_a_line_kicad_rewrites_before_it_routes` keeps its assertion (M1 runs before routing natively too). |
| test_stitch, test_thermal | R | `pattern_copper` signature; one `krt_plan` reference each → `plane_targets`. |

---

## 2. The native pipeline

### 2.1 Stages

| Stage | Reads | Writes | KiCad file? |
|---|---|---|---|
| check | `board.py`, part `part.py`/`.kicad_sym`/`.kicad_mod` paths | nothing | none |
| sch | Design | `schematic/*.kicad_sch` (separate output, unchanged) | the schematic only |
| place | Design, compiled job, each part's `.kicad_mod` (via `seed.instantiate`, in memory) | `Placement` in memory; with `--upto place` only: `placed/layout.gen.py` + `placed/layout.kicad_pcb` **by emit** (for `pcbc pcb` and review; never opened by a later stage — P1 proves it) | none before emit |
| route | Design, job, `.kicad_mod` (via `parts`), `layout.core.py`, `Placement` recomputed in memory (pure, ms) | Python objects → `routed/layout.gen.py` | none before emit |
| emit | `layout.gen.py` loaded back + core objects + `parts.base_text` (in memory) | `routed/layout.kicad_pcb` + compiled `.kicad_pro`/`.kicad_dru` | **the first `.kicad_pcb`** |
| gates | `routed/layout.kicad_pcb` after `kicad_drc(refill=True)` | stamps, report | checker reads only |
| fab | `routed/layout.kicad_pcb` | `fab/` (unchanged; refuses any unrouted net, `fab.py:832`) | checker + exporter |

Route, step by step (each writes Python objects into one in-memory `Layout(poses, copper, graphics)`):

1. **core** — `load_core` + M1 `core_probe.round_trip` (probe base = `parts.base_header`, not a placed board)
   + `check_layout` + `_clearance_clashes`. Core copper enters the `Scene` as locked items.
2. **place** — `place_native.place` → `Place` poses, outline `gr_rect`, slot `gr_rect`s, keepout and
   rule-area `Pour(keepout=...)`s, fiducial footprints, silk-legalised reference positions (§4).
3. **pours** — `Pour(net, layers=(L,))` for every `plane_targets(job)` entry (§3.5).
4. **route** — patterns and the router, in the order of §3.4, writing `Piece`s into the `Scene`.
5. **objects** — `route_emit.to_copper` turns pieces into `Seg`/`Via` with geometry-derived uuids and
   `pcbc:<reason>:<owner>` groups; `layout_emit.stamp` completes every field.
6. **gen** — `assign_ids`, `write_gen(routed/layout.gen.py)`, load it back, `diff_objects` (kept).
7. **emit** — `emit_board(parts.base_text(...), poses, core+gen copper, core+gen graphics)`; decompile the
   emitted **text** once in memory and compare field for field (kept); write the file.

### 2.2 How every board-text reader gets its data natively

The seam is two new modules. **`parts.py`**: `posed_blocks(design, poses) -> {ref: block}` — each
footprint block made from its `.kicad_mod` by `seed.instantiate` (pads bound to board.py's nets) and
posed by `apply._rewrite_footprint`, all in memory; `base_header(design, name)`; `base_text(design,
poses, name)` = header + posed blocks, no layout primitives (what `emit_board` needs as its first
argument and `core_probe` as its base). **`Scene` inputs**: `build_scene(design, job, cs, *, parts,
copper=(), pours=())` — pads from `parts`, tracks/vias from `Copper`/`Piece`, zone rules from `Pour`
objects. A test-only adapter `build_scene_from_board(text)` reads an emitted board (the old signature,
for tests on emitted boards); P1 proves the build never calls it before emit.

| Reader today (`file:function`) | Reads now | Natively from |
|---|---|---|
| `layout.footprints_by_ref(pcb_text)` (used by route_scene, route_checks, pcb_place, fanout, layout_job, apply, check) | footprint blocks off a board | `parts.posed_blocks(design, poses)` |
| `pcb_place.parse_foot(ref, block)` | a posed block | same function, block from `parts` (`at` from the `Pose`, not `footprint_at`) |
| `pads.pad_geoms(block, at, ...)` | a posed block | unchanged; block and `at` from `parts` |
| `route_scene.copper_items(text)`, `_vias(text)`, `net_table(text)` | segments/vias in the text | `copper_items(copper: Sequence[Copper])` + `Scene.add(pieces)` |
| `route_scene.zone_rules(text)` | zone heads in the text | `zone_rules(pours: Sequence[Copper])` |
| `route_checks.build_ctx(design, job, pcb_text)` (F.1-F.8) | blocks + `_pad_layers(block)` | `build_ctx(design, job, parts)`; `_pad_layers` on the `parts` block |
| `pcb_place.resolve_places(design, job, pcb_text)` | seed board's blocks at seed-grid poses | `resolve_places(design, job, parts0)` where `parts0` is `posed_blocks` at the default pose (§4) |
| `pcb_place.layout_report(design, job, pcb_text)` | placed blocks | `layout_report(design, job, parts)` |
| `fanout.fanout_pieces(design, job, text, ...)` | blocks | `fanout_pieces(design, job, parts, ...)` |
| `layout_job.board_pads(design, text)` | blocks | `board_pads(design, parts)` |
| `layout_job.board_layer_names(design, placed_text)` / `board_uuids(placed_text)` | the placed board's layer table / uuids | `seed._layers(n)` / `parts.base_text` uuids |
| `core_probe.probe_base(placed_text)` | the placed board stripped | `parts.base_header(design, name)` |
| `silk.legalize_silk(text, ...)` via `silk_job(pcb)` | a board file | `legalize_silk(parts.base_text(...))` in memory; the Reference positions it returns go into the blocks `parts` hands emit |
| `fab.insert_fiducials(text, ...)` at place time | the placed board | `parts` appends `fab._fiducial_sexp` blocks for `Placement.fiducials` |
| `apply._apply_places(text, job)` | seed board | per-block `_rewrite_footprint` inside `parts.posed_blocks` |
| `check.check_job(job, pcb_path)` | the placed board | runs on the **emitted** board as a gate (checker) |
| `blocking.blocking_lines(design, text, work, net)` | step boards | deleted; `route_native.blockers(scene, net, stage_of)` (§3.6) |
| `route_relax.relax_board(text)` | segments/vias in text | `relax_pieces(scene, pieces)` |
| `patterns.pattern_copper(..., text)` | text → scene | `pattern_copper(..., scene)` (patterns are already scene-only: `grep text patterns/*.py` finds only a comment) |
| `route_scene`'s `pads_by_net(text)` (spike) | text | `Scene.pads_of(net)` |
| `copper.py`, `copper_bar.py`, `route_verify.py`, `ampacity.py`, `netcheck.py`, `fab.py`, `review.py`, `refs.py` | the **emitted** or fab board | unchanged: they are checkers or fab (`direction.md` §2 allows exactly this) |

### 2.3 The decompiler

`gen.decompile` leaves generation: nothing turns a board into `layout.gen.py` during a build. It stays
as an importer (`pcbc import`, later), a test tool, and the **reader of the emitted board** in the
post-emit checks that already exist (`build.py` fidelity check, `roles_doc`, `tap_stubs`,
`fab_board_diff`, `fab_copper_diff`). That last use is a checker reading the `.kicad_pcb`, which
`direction.md` §2 allows. **Owner question:** if "leaves the build path" was meant to include those
checks, the alternative is a direct text-to-object comparison that is itself a second parser; I
recommend keeping `decompile` as the one reader.

---

## 3. The native router (N0)

### 3.1 Modules

- **`src/pcbc/route_cost.py`** — the spike made a module: `NetCost` and `net_cost(cs, net)` from
  `cf/reqs.py` (every term `Derived` from `constraints.py`; `REFUSED` kept as the documented list of
  what is not priced), `Slack` from `cf/field.py`, `Lattice`/`Router` (A* + `pull` + `tidy` + `link`)
  from `cf/router.py`. Changes on the way in: `VIA_SCALE` env var deleted (a tuning knob); A* ties
  broken by `(f, g, seq)` with a monotonic `seq`; the budget is node expansions, never seconds; the
  lattice origin is `scene.outline[0:2]`; `banned` is a sorted structure.
- **`src/pcbc/route_pair.py`** — `cf/pair.py`'s `offset` plus the pair object (§3.5).
- **`src/pcbc/route_native.py`** — the route stage: order (§3.4), pours, pieces → objects, unrouted
  moves, problem files. Replaces `route.route_job`; called by the rewritten `layout_job`.

`route_scene.blocked` stays the only legality judge on every candidate and on the finished run
(`cf/router.py` already does this); `route_verify.verify_copper` runs over all router pieces at the
end, as `pattern_copper` does for pattern pieces, and a failure raises (a bug, not a move).

### 3.2 Net order

Deterministic, by class then width then name — the old stage order, minus KRT:

1. nets with `vias=False` or one layer (the spike's `SW`, `FB`, `T_DIV`, `T_OUT`), grouped by
   (layers, class), each group by `(-width_mm, net)`;
2. pairs (`autoroute == "diff_pair"`), by name of the first member;
3. plane-net pads the tap pattern refused (§3.5);
4. everything else by `(-width_mm, -pad_count, net)` (the spike used `(-width, net)`; pad count is the
   tie-break the spike did not need on its three boards — measure both in step 5 and keep the one
   that routes more links; record which).

Within a net: Prim MST over distinct pad positions (`board_route.py:mst`), links in MST order;
terminals entered only on their pad's layers (`board_route.py:79-84`). Existing same-net copper
(core, patterns) is a terminal set, so a link may end on it. **No rip-up in N0.** The visibility
spike's order sweep moved buck from 13/21 to 20/21 by order alone (`spike-visibility/order_sweep.log`),
so order is pinned by rule and never searched; rip-up is a later step with its own measurement.

### 3.3 Pieces become Python objects

`route_emit.to_copper(pieces, board)`: a seg `Piece` → `Copper("seg", ...)` with `width=w`,
`layer`, `net`, `uuid=stable_uuid(board, "seg", net, layer, a, b, w)` (geometry-derived, never the
absolute path); a via → `Copper("via", ..., size=w, drill, layers=(F.Cu, B.Cu))`. Pattern pieces
keep their roles as `Graphic("group", name=f"pcbc:{reason}:{owner}", members=...)`, exactly the
groups `_role_groups` writes today, so `roles_doc`, `fab.board_roles` and every gate read the same
thing. Router copper gets **no group**: its reason is `leftover`, as KRT's copper was, so
`copper_bar`, `RELAXABLE` and the gates keep their meaning. Every object then goes through
`layout_emit.stamp` for the fields KiCad would add; the post-emit field-for-field check catches any
field KiCad still rewrites, and a test runs `core_probe.round_trip` over every gen object of
blinky and buck (`test_every_gen_line_passes_kicad_s_reading_in_one_call` already exists).

### 3.4 Where the patterns run

R-S1 existed because KRT could not see pcbc's locked copper as anything but a wall and could not be
asked again. The native router sees every piece in the same `Scene`, and a pattern is just a net
routed early by a fixed recipe. The order keeps the measured one and replaces KRT's steps in place:

| # | Stage | Why here |
|---|---|---|
| 1 | `hop` (PRE) | a two-pad hop between neighbours has one path; least freedom first (`patterns/__init__.py:549`) |
| 2 | `fanout` | a closed row's escape has one direction |
| 3 | `spine` (MID) | as today; **measured both ways in step 5** — the spike routed buck's power nets itself at class width and got 15.36 deg/mm with no spine; if spine-off routes ≥ as many links with lower deg/mm on buck and node, spine leaves the default order (the module and its tests stay) |
| 4 | router: constrained nets, then pairs | §3.2 items 1-2, on a board that is still mostly empty |
| 5 | `Pour` objects | fields only; they are not obstacles |
| 6 | `tap` (POST) | every SMD plane pad welded to its plane; `route_verify.pour_raster` predicts reach |
| 7 | router: refused plane pads, then signals | §3.2 items 3-4 |
| 8 | 2-layer re-check | recompute `pour_raster` over all copper; a tap via no longer in its plane's largest region re-enters step 7 once as a pad to route (KRT's `finalize`); still out → unrouted move |
| 9 | `stitch` (FINAL) | copper whose absence disconnects nothing; nothing routes after it |
| 10 | `relax_pieces` | the embedder: string-pull on `RELAXABLE` pieces; the router's own `pull` already ran per link, so this is measured, not assumed (step 5 records relax on/off deg/mm) |

### 3.5 Pours, plane pads and pairs

**Pours.** One `Pour(net, layers=(layer,))` per `plane_targets(job)` entry (declared `planes=`, or the
2-layer implicit `("GND","B.Cu")` — the rule every gate already reads). Outline: the board rectangle
with the edge inset from `stackup.edge_clearance`, not `layout_emit.stamp`'s hard-coded
`inset = 0.5` (`layout_emit.py:126`, which the pinned-inset test would fail). Clearance from the
net's class, connect mode and thermal fields from `ZONE_DEFAULTS`, `filled=yes`. KiCad fills at emit
(`kicad_drc(refill=True)` already runs before every gate). A core `Pour` on the same (net, layer)
replaces the generated one (the locked-pour tests keep their meaning).

**Plane pads.** The tap pattern first. For a pad it refuses, the router links the pad to a target set
of lattice cells inside the predicted pour region on the plane layer (`pour_raster(scene, net, layer)`),
ending in a via: same cost, same judge. 4-layer inner planes are not cut by signals, so this is
usually one short stub; on 2 layers step 8 of §3.4 re-checks.

**Pairs as one object.** For a `Constraint.pair`: route one centreline between the two pad-pair
midpoints at the envelope width `width_mm + pitch_mm` with `pitch_mm = width_mm + gap` (the spike's
`t1b_pair.py` rule); derive both halves by `route_pair.offset(±pitch/2)`; breakout legs from each
pad to its half's end are octile stubs judged by `blocked()`; both halves and the two against each
other are judged before either is kept. A pad a pair net has beyond its two ends (USB-C `B6`/`B7`)
is linked afterwards as a single-net link. **Unproven at board scale:** the spike only re-embedded
a slice of KRT's shipped pair (`t1b_pair.py:45`), never a pad-to-pad pair. Coupled length (the
same method `route_relax._skip_nets` uses) is reported per pair from the emitted board, beside
KiCad's `diff_pair_uncoupled_length_too_long`/`skew_out_of_range` warnings.

### 3.6 An unroutable net becomes a move

A link that fails leaves **no copper** for that link (earlier links of the net stay only if they
form a connected piece containing pads; otherwise the whole net is dropped, so no dangling copper
reaches gen). The net is absent from, or partial in, `layout.gen.py`; the build still emits, fills and
runs DRC and every gate on what exists, then returns `error = "unrouted: ..."` with no stamp and no
fab. Shape (the existing `move_line`, one per failed link):

```
USB_DN: J1.A7 (F.Cu, 30.25,37.44) cannot reach J1.B7 / the rest of USB_DN (nearest 29.25,37.44).
  In the way: J1.A6 pad (USB_DP) — J1 is placed by board.py:212 Place("J1", edge="bottom");
  U3 courtyard — board.py:216 Place("U3", to="J1.DP1"); layout.core.py:14 Seg on GND.
  Move board.py:216: Place("U3", ...) further from J1 (the pair needs 0.64 mm; 0.31 mm is free),
  or lock J1's escape in layout.core.py (copy the pair's lines from routed/layout.gen.py).
  Problem file: layout/c3_usb/routed/unrouted/USB_DN-1.json
```

(Illustrative: the `board.py` line numbers are c3_usb's real `Place()` lines; the coordinates and
clearances are made up for the shape.)

- Blockers come from `route_native.blockers(scene, net, a, b)`: the items the failed A* frontier hit
  most, nearest first, at most 4 — each named by its **source line**: a part by its `Place()` line,
  core copper by its `layout.core.py:N`, earlier router/pattern copper by `"<net> copper (routed
  before this net; its NetReq board.py:N)"`. A part with no `Place()` is named "has no Place() in
  board.py". This needs `where` on `PlaceSpec` and `NetReqSpec` (captured at load like core lines'
  `where`; they have none today — `model.py:19,283`).
- The fix sentence always ends in a `board.py` or `layout.core.py` edit (`move_line`'s rule); the pair
  and no-via texts of today's `_unrouted_move` carry over, with KRT's step names removed.
- Every refusal writes a problem file (`direction.md` §8): the net, its pads, `NetCost` terms, the
  scene items within the link's box, the order position — JSON, never a board. `python -m
  pcbc.route_native --replay <file>` re-runs that one link; `tests/fixtures/unrouted/` takes it.
- Never a traceback: `route_native` catches only its own `Unroutable`; anything else is a bug and
  propagates (a caught bug would be a move that lies).
- Cross-check after emit: the set of nets KiCad reports in `unconnected_items` must equal the router's
  unrouted set. A net KiCad calls open that the router called done is a **failure** (router bug), not
  a move.

### 3.7 Determinism

No wall clock, no randomness, no set or float-keyed dict iterated in a decision path; every point
through `route_geom.q` once; A* ties by `seq`; order by §3.2; uuids from geometry and board name;
`gen_source` sorts; `(model ...)` paths relative (`seed._absolute_models` base). Proof: the existing
`test_two_builds_from_two_absolute_paths_are_byte_identical_with_no_normalisation`, −krt, on every board
(including the unrouted ones: gen and the emitted board must still be byte-identical).

---

## 4. Native placement (N0)

`src/pcbc/place_native.py`: `place(design, job) -> Placement(poses, graphics, copper, fiducials,
report, notes)`, pure, no files.

1. `parts0 = parts.posed_blocks(design, default_poses(design))`, where `default_poses` is the seed's
   grid (`seed.emit_pcb`: `(5 + (i % cols)·8, 5 + (i // cols)·8)`), so an undeclared part lands where
   it lands today.
2. `resolve_places(design, job, parts0)` exactly as now (file order, CSS anchors, fiducial spots,
   relations; a clash is a move). A declared `Place()` is never moved.
3. The silk loop from `place.place_job` (three rounds, gap +0.6 mm for cramped relations, decaps may
   hide) on `parts.base_text` in memory.
4. Objects: a `Place` per footprint (`Pose(ref, at, rot, layer, locked)`); the outline as
   `Graphic("gr_rect", Edge.Cuts, uuid=stable_uuid("edge", w, h))`; each `Isolation(slot=True)` as a
   `gr_rect` with `stable_uuid("slot", name)`; each keepout (fiducial masks included) and each compiled
   rule area as `Copper("pour", keepout=..., name=...)` with the uuids `apply` uses today. Same uuids
   means today's gen lines for these objects are the equality target in step 2 of §6.
5. `layout_report` + `route_aware_report` (F.1-F.8) on `parts` → `report`; `check_job` becomes a
   gate on the emitted board.

A part with no `Place()` stays at its default pose and gets a note: "U5 has no Place() in board.py;
it sits at the default grid spot (x, y)". **Solved placement is not N0.** It plugs in between steps 2
and 4 above as `place_solve.py`, taking the undeclared refs and choosing poses by the router's own
cost over the incremental channel map (`direction.md` §6: 2 ms median update on node), writing only
`Place` objects; declared ones are inputs it may not move.

---

## 5. N0 acceptance

Every number with its command; every build on a copy (`rsync -a --exclude layout examples/<n>/ <tmp>/<n>/`,
ds2 from `~/Documents/MaD/Hardware/DS2Addon/pcbc/` to `<tmp>/ds2/`), never in `examples/` or `~/Documents/MaD`.

**Per board**

| Board | Must hold |
|---|---|
| blinky | `pcbc build` to fab; KiCad DRC 0 errors, 0 unconnected; every gate passes; fab equality exact. |
| buck | 0 unconnected items and 0 DRC errors (the spike's "21/21 links" is reported with the spike's `mst` definition, noting the spike routed GND as tracks and N0 pours it on B.Cu, so it is not the same experiment); to fab. |
| c3_usb, node, ds2 | Either fab as above, or: `error` starts `unrouted:`, one move per failed link in §3.6's shape, a problem file per move, KiCad's unconnected set == the router's unrouted set, **0 DRC errors** on the emitted board, no traceback, no `fab/` directory, no stamp on the route stage. |

**Every board, every build**

- **P0 gates**: every gate in `build.py` that applies runs on the emitted board and passes, except the
  netlist/unconnected verdict on a board with unrouted nets (reported, not passed).
- **P1 no board before emit**: `trace.Trace` gains an ordered event log `(seq, kind, path|argv)` and a
  `mark("emit")` that `layout_emit`'s writer calls. A test builds each board and asserts: no open (read
  or write) of any `*.kicad_pcb` with `seq` before the emit mark, except paths under a
  `pcbc-core-probe-*` temp dir; no open of anything under `placed/` by the route stage.
- **P2 KRT not invoked**: in the same traced build every `subprocess` argv starts with `kicad-cli`;
  a static test walks `src/pcbc` with `ast` and fails on any identifier or string literal containing
  `krt`/`KiCadRoutingTools` outside docstrings and comments; `pcbc.route` does not import; the `krt`
  marker is gone from `pyproject.toml` (strict markers).
- **P3 determinism**: two builds from two absolute paths give byte-identical `layout.gen.py`,
  `layout.kicad_pcb` and `fab/` (existing test, −krt, all five boards).
- **P4 core locks**: `test_a_gen_line_copied_into_core_locks_that_segment`,
  `test_every_line_of_layout_gen_can_be_locked_verbatim`, `test_a_gen_line_locked_alone_changes_nothing_else`,
  the locked-pour and core-rule-area tests pass natively (on blinky and buck; on the other boards
  wherever they do not need a complete board).
- **P5 mutation**: corrupt one object in `layout.gen.py` between write and emit → the build fails naming
  the line and field (existing `test_a_line_of_layout_gen_changed_before_emit_never_reaches_the_board`).
- **P6 copper quality**, per board, one script run on the emitted board: completed links / total, total
  mm, segments, vias, deg/mm (the spike's `board_route.degmm`: degree-2 vertices only), micro-segments,
  KiCad DRC by type, coupled mm per pair — printed beside the **KRT baseline captured in step 0**.
  Recorded KRT numbers to compare against, not re-measured here: buck 18.32 deg/mm and ds2 14.74
  (`docs/quality-plan.md:130`); hand-routed DS2 17.2 (`docs/fp-router-plan.md:273`); the BAR ceilings
  (`tests/test_examples_fab.py:117-120`, recorded with KRT+relax). N0 does not require beating them; it requires reporting them.
- **P7 suite**: `cd /Users/rileymccarthy/pcbc && PCBC_REQUIRE_KICAD=1 .venv/bin/python -m pytest -q -p no:cacheprovider`
  green, run last with nothing else building; count reported against 1011 with the deleted tests listed.
- **P8 refutation**: an independent verifier, with its own comparators, tries to refute P1-P6 on buck
  and node before anything is called done (`direction.md` §8).

---

## 6. Order of work

Each step ends with the full suite runnable (deleted tests gone, not-yet-passing ones strict-xfail).

0. **Baseline, before deleting anything** (KRT still installed). One script over fresh builds of the
   five boards on copies records P6's numbers and the place-level gen lines (every `Place`, outline,
   slot, keepout, rule area) to `tests/fixtures/native/krt_baseline.json` with the command in it. After
   step 1 these cannot be reproduced except from the checkpoint patch.
1. **Delete KRT and decompile-from-router.** Remove §1.1-1.3. `layout_job` now: core → patterns (pre,
   mid, post, final) + `relax_pieces` + Pour objects → objects → gen → emit, with **no router**; every
   non-pattern link is an unrouted move (§3.6 shape, blockers minimal). The placed `.kicad_pcb` is
   still the emit base in this step only (named as such in the docstring). blinky builds to fab (its
   one link is a hop). Tests per §1.4; board-completing tests native-xfail.
2. **`parts.py` + `place_native.py`.** Emit base, scene inputs and M1 base from `parts`. Seed stage and
   `placed/` as an input are gone; `pcb_job`/`--upto place` emit a place-only board. Equality tests:
   native place objects == step 0's recorded lines on all five boards; `parts.base_text` == today's
   placed board minus layout primitives (then that transitional test is deleted).
3. **Native readers.** `build_scene`, `build_ctx`, `fanout_pieces`, `layout_report`, `board_pads`,
   `relax_pieces` off `parts`/objects (§2.2). Equality: native `Scene.items` == `build_scene_from_board`
   on the place-only emitted board, five boards.
4. **P1 + P2 tests** land (they pass now: nothing before emit reads a board).
5. **`route_cost.py` singles.** Order §3.2 items 1 and 4. Targets: blinky unchanged, buck 0 unconnected
   / 0 errors. Measure spine on/off and relax on/off on buck and node; keep the defaults the numbers pick.
6. **Plane pads** (§3.5) and the 2-layer re-check.
7. **`route_pair.py`.** c3_usb and node pairs; coupled mm reported.
8. **Moves at scale.** `blockers`, `where` on `PlaceSpec`/`NetReqSpec`, problem files + `--replay`,
   the KiCad-vs-router unrouted cross-check. c3_usb, node, ds2 meet their row.
9. **Acceptance.** P0-P8 on all five, P6 table written with commands, `test_examples_fab` tables
   re-recorded for the boards that complete; README and CI updated; refutation pass.

## 7. Risks

1. **The spike is unverified** and was run on KRT-era placed boards; the numbers may not survive
   verification. Step 5's acceptance is buck's KiCad verdict, not the spike's report.
2. **Buck with a pour is a different board from the spike's buck** (the spike routed GND as seven
   track links; N0 pours GND on B.Cu and taps). 21/21 may not transfer.
3. **Order sensitivity** (13/21-20/21 on buck by order alone in the visibility spike): a pinned order
   can be wrong for a board; without rip-up the cost is moves, not traceback. Rip-up is post-N0.
4. **Pad-to-pad pairs are unproven**; node's four USB-C failures (`node_run.log`) are the expected
   moves. Core locks at J1 are the intended answer, not tuning.
5. **Runtime**: node took 586 s in the spike, per build; P3 needs two builds per board and adoption
   passes (`LOCK_PASSES`) can double routing. Budget by expansions; cache `Slack`; measure suite time.
6. **Field completeness without KiCad's normalise**: objects the router makes must be exactly what
   KiCad saves (via layers, zone defaults). The post-emit field check fails the build if not; the
   probe round trip over gen objects in tests finds it first.
7. **Pre-router pattern copper hurt KRT** (`docs/quality-plan.md`: buck 25.5 deg/mm patterns-off vs
   50.9 on, before relax). It may hurt ours too; step 5 measures spine on/off rather than assuming.
8. **Test churn**: 83 `krt`-marked tests plus the KRT recordings in `test_examples_fab`; strict xfail
   keeps the suite honest while boards do not complete.
9. **Losing the comparison**: once KRT is deleted its numbers exist only in step 0's file and the
   checkpoint patch; skip step 0 and P6 has nothing to compare against.
10. **Interpretation risks for the owner**: `decompile` kept as the post-emit reader (§2.3); the place
    stage keeping a place-only emit for `pcbc pcb` (§2.1); `Board(net_order=)` refused rather than
    reinterpreted (§1.2).
