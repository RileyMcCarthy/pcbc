# Routing quality: the plan

How pcbc reaches hand-quality routing. Designed 2026-09-21 by a judged panel: one survey of router
architectures, four independent designs (post-route relaxation, own the search tier, placement is the
ceiling, the limits are in the language), one judge who **rebuilt all four boards and re-measured
every headline claim** rather than taking them.

**Read section 1 first.** Two of the four designs measured their numbers on the checked-in layout
artifacts, which for buck, c3_usb and node are **stale** — they carry zero locked segments and
predate the R2 pattern stage. Only ds2's is current. Every number below is from a fresh build.

I have what I need. All four designs were judged against fresh builds I ran today.

## The answer, in five sentences

Build **pcbc's own whole-board rubber-band relaxation pass as a new step after `patterns_final`** — I ran the RELAX prototype against fresh builds of all four boards today and it takes buck from 50.9 to **18.1 deg/mm** and 96 to **6** micro-segments in 0.49 s, against the hand-routed DS2 Addon's 17.2, with KiCad reporting **zero new DRC errors, zero new unconnected items and identical warning totals**. That single pass, roughly 300 lines against primitives that already exist, closes the entire *shape* gap to hand work on three of four boards and costs nothing at KRT, which never sees it. What it does **not** close is detour, which is set by homotopy, net order, and pcbc freezing its own copper in front of KRT — and I measured that cost precisely: buck with `PCBC_PATTERNS=off` routes at 25.5 deg/mm and 26 micro-segments where the shipped pipeline gets 50.9 and 96, so **pcbc's own pre-KRT copper is currently the single largest quality regression on the board**. So: relax first (days), then fix the diff-pair leak and mitre board-wide (days), then unfreeze and probe placement for detour and feasibility (weeks), and only consider a topological router after those four have been measured — KRT stays, demoted from "the router" to "the completion engine". Do not build a grid maze router, do not tune `--turn-cost`, and do not build the meander engine; the first duplicates KRT, the second collides with the relaxer, and the third has no board that needs it.

---

## 1. What I measured today, and why it reorders everything

**Provenance first, because it decides whose numbers to trust.** OWN's E7 is correct and it is the most important finding in the four reports: the on-disk artifacts for buck, c3_usb and node carry **zero locked segments** and predate the R2 pattern stage. I confirmed it — `buck/layout/buck/routed/layout.kicad_pcb` is 97 segments / 145.7 mm / 0 locked, where a fresh build is 175 / 149.3 / 33 locked. **RELAX and OWN both measured their headline numbers on boards that are not the shipping pipeline's output**, for three of four boards. Only ds2's artifact is current. PLACEMENT is the only design that measured fresh builds, and it is the only one whose table reproduces the task's.

So I rebuilt everything. My instrument reproduces the task's table exactly:

| board (fresh) | segs | mm | micro | off45 | corners/mm | **deg/mm** | mean seg | pcbc owns |
|---|---|---|---|---|---|---|---|---|
| buck | 175 | 149.3 | 96 (55%) | 1 | 1.031 | **50.9** | 0.85 | 15% |
| c3_usb | 444 | 361.2 | 188 (42%) | 11 | 0.869 | **52.3** | 0.81 | 20% |
| node | 314 | 411.7 | 54 (17%) | 25 | 0.452 | **37.7** | 1.31 | 18% |
| ds2 | 315 | 508.3 | 96 (31%) | 12 | 0.478 | **24.1** | 1.61 | 8% |
| *hand-routed DS2 Addon* | *131* | *255.8* | *10 (7.6%)* | *1* | *0.336* | ***17.2*** | *1.95* | — |

Note the boards are **worse than the survey's own table implies**: the survey quoted 33.9 deg/mm for c3_usb from a stale artifact; the shipping build is 52.3.

### 1a. The relaxer on the boards that actually ship

I ran RELAX's `relax2.py` unmodified against each fresh build:

| board | segs | micro | off45 | corners/mm | **deg/mm** | mean seg | time |
|---|---|---|---|---|---|---|---|
| buck | 175 → **69** | 96 → **6** | 1 → **0** | 1.031 → **0.334** | 50.9 → **18.1** | 0.85 → **2.13** | 0.49 s |
| c3_usb | 444 → **260** | 188 → **40** | 11 → **4** | 0.869 → **0.383** | 52.3 → **29.2** | 0.81 → **1.35** | 1.91 s |
| node | 314 → **263** | 54 → **27** | 25 → **9** | 0.452 → **0.340** | 37.7 → **29.0** | 1.31 → **1.55** | 1.83 s |
| ds2 | 315 → **204** | 96 → **10** | 12 → **3** | 0.478 → **0.260** | 24.1 → **12.1** | 1.61 → **2.47** | 2.44 s |

**Corners per mm lands at or below the hand board's 0.336 on all four.** buck and ds2 beat the hand board outright on turning. The effect on current boards is roughly **twice** what RELAX measured on the stale ones, because there is twice as much damage to remove.

**The arbiter agrees** (`netcheck.check_copper`, KiCad 10.0.6, pcbc's own rules restored on both arms):

| | gate ok | DRC errors | unconnected | total warnings | `track_segment_length` | `track_angle` | `width_power` |
|---|---|---|---|---|---|---|---|
| buck base | true | 0 | 0 | 34 | 95 | 28 | 23 |
| buck relaxed | true | **0** | **0** | **34** | **6** | **20** | **8** |
| ds2 base | true | 0 | 0 | 70 | 94 | 14 | 3 |
| ds2 relaxed | true | **0** | **0** | **70** | **10** | **8** | **3** |

Byte-deterministic: two runs of buck produced identical output (`sha ccdea292a6e9bd70` twice).

### 1b. The finding none of the four designs made

I ran the documented rollback switch on identical code, same `board.py`, same KRT, same KiCad:

| buck | segs | mm | micro | off45 | vias | corners/mm | **deg/mm** |
|---|---|---|---|---|---|---|---|
| `PCBC_PATTERNS=on` (shipped) | 175 | 149.3 | 96 (55%) | 1 | 7 | 1.031 | **50.9** |
| `PCBC_PATTERNS=off` | 96 | 142.5 | 26 (27%) | 3 | 4 | 0.526 | **25.5** |

**pcbc's own 22.8 mm of pre-KRT copper costs buck 2.0× its corner density, 3.7× its micro-segments, +4.8 mm of copper, +3 vias and +79 segments.** I attributed it to a single step by measuring every step file:

| buck, patterns ON | segs | mm | micro | deg/mm | | patterns OFF | segs | mm | micro | deg/mm |
|---|---|---|---|---|---|---|---|---|---|---|
| `04_patterns_post` (entering signals) | 38 | 42.0 | 3 | 28.9 | | `03_switchnode_nets` | 22 | 19.3 | 2 | 44.2 |
| `05_signals` (leaving) | 172 | 149.3 | 93 | **49.7** | | `04_signals` | 96 | 142.5 | 26 | **25.5** |
| **signals step added** | **+134** | +107.3 | **+90** | | | **added** | **+74** | +123.2 | **+24** | |

The signals step routed **more** copper (123 vs 107 mm) with **half** the segments and **a quarter** the micro-segments when the board it was handed had 19 mm of locked copper instead of 42 mm. Per mm routed: **1.25 seg/mm and 0.84 micro/mm with patterns on, 0.60 and 0.19 with them off.** That is task finding #2 and the `BAR` comment, isolated to one step, measured at whole-board scale on current code.

**But do not act on it by turning patterns off.** With patterns off, buck's ampacity report got *worse* (VIN's narrowest track 0.3905 → 0.127 mm, and a new 5V via-ampacity complaint), which is exactly what the spine and taps exist to fix. The correct reading is that pcbc is paying for electrical structure in corner density — **and the relaxer buys the corner density back (18.1 beats the patterns-off 25.5) without giving up the structure.** That is why relaxation is first and unfreezing is not.

---

## 2. The ordering, with reasons

**1st — RELAX.** It is the only design whose central claim I could verify end-to-end today, on the shipping pipeline, with the arbiter, and it is the largest measured effect in the whole survey: 50.9 → 18.1 deg/mm on buck, 96 → 6 micro, in 0.49 s, gate-clean. It touches no router, changes no pinned command, re-orders no nets, and runs in a slot (`route.py:429`, after `patterns_final`) whose safety argument is structural — `krt_plan` schedules nothing after it, so no router can react to it.

**2nd — EXPRESSIVE, but only Move 2, and demoted from architecture to bug fix.** I confirmed the root cause by reading: `route.py:263` excludes `autoroute == "diff_pair"` from `constrained`, `route.py:295` computes `pairs` and uses it only to filter `local`, and `route.py:364` builds `signals = ["--nets", "*", !constrained, !plane_nets, !done]`. The pair is in none of the three, so the wildcard step claims a net that is already routed as a pair. Four lines. **Magnitude correction: on a fresh c3_usb build the skew is 2.190 mm against the 0.5 mm budget, not the 11.599 mm EXPRESSIVE measured on the stale artifact.** Still 4.4× over, still a real defect, one-fifth the drama.

**3rd — PLACEMENT, reframed from "optimizer" to "probe".** Its band is real and it measured fresh builds, but its best buck placement (77 segs / 10 micro / 131.8 mm) is **beaten on shape by the relaxer on the shipped placement** (69 / 6), at 1/500th the wall clock (0.5 s vs ~4 min for its 21-point probe). What placement owns that relaxation cannot touch is **detour and feasibility** — and PLACEMENT proved that orthogonality itself (its route estimate correlates +0.945 with copper length and −0.09 with segment count). Its single most valuable output is not a better board, it is the **neighbourhood-feasibility fraction**: ds2 2/13, buck 13/21, c3_usb 10/11. That number distinguishes "try a different placement" from "the board is full", and nothing in pcbc says it today.

**4th — OWN's search tier. Do not build it yet.** Its relaxation half is RELAX's, measured worse. Its search half completed 29 of 43 links on ds2 and 13 of 20 on buck, single-layer, no vias, no fanout, no rip-up, and its `TURN_MM = 1.0` produced buck at 44.2 deg/mm — *worse than KRT*. Its genuine contribution is two numbers I am adopting: the exact-predicate throughput census (3,685–26,765 calls/s) that kills the grid-router plan, and the observation that every refusal on ds2 was an escape into `U1` or `J3`.

### The single highest quality-per-effort move

**The relaxer.** ~300 lines, one new module, one `krt_plan` line, one `route_job` branch, two `route_emit` helpers. It moves `deg/mm` from 50.9 to 18.1 on buck, `track_segment_length` warnings from 95 to 6, `track_angle` from 28 to 20, and it is the only change here that requires no other change to be worth shipping.

---

## 3. Every contradiction, and its resolution

**C1 — Does relaxation fix detour?** RELAX: "false as measured", c3_usb 1.81 → 1.70, no net worse. OWN: "at all. No", ds2 REFP_F 3.23 → 3.22. **Both measured the same thing and got the same numbers; only the framing differs.** Resolution: relaxation recovers the *non-monotone slack* inside a homotopy class — 5–15% of the excess on a wiggly net, ~0.3% on a genuinely misrouted one. OWN's framing is the honest one; RELAX overclaims by calling a 0.11 recovery "false as measured". **Detour remains an open problem after every slice in this plan except 4 and 5.**

**C2 — The accept rule.** RELAX: lexicographic `Q = (off-45, sharp turns, micro legs, corners, length)`. OWN: "strictly shorter and no more corners". **RELAX wins, decisively and by measurement.** A monotone staircase collapse is exactly length-neutral, so "strictly shorter" accepted 0 of 21 chains on buck. The prototype I ran is RELAX's Q and it produced 175 → 69 on fresh buck; OWN's rule produced 97 → 79 on the easier stale board. Adopt Q, with RELAX's two corrections: test **relative to the input** (the absolute rule test rejected 865 of 1214 candidates), and include **the turns at the chain's two ends** against copper that does not move (without it ds2's `track_angle` went 10 → 12).

**C3 — The preserve set.** RELAX: capsule-overlap witness points. OWN: geometric `touches()` contact set. **These are the same rule** and both designs arrived at it *after* an endpoint-only version broke real nets — RELAX got `track_dangling` 1 → 2 on c3_usb, OWN broke `5V`, `VBUS`, `AIN0` and `VSS` across three boards. Two independent implementations failing the same way and converging on the same fix is the strongest evidence in the four reports. **Adopt it, and treat "freeze the endpoints and the degree≠2 vertices" as a known-wrong design that will look correct in review.**

**C4 — May the pass touch pcbc's own locked pattern copper?** RELAX: yes by default, skip only constrained `NetReq` copper. OWN: never, only unlocked. **OWN wins on the argument; RELAX's prototype is what I measured.** The code says `route_job` `continue`s out of the `PCBC_STEP` branch before `missing = _lost(...)` (`route.py:~585`), so a pcbc step *may* legally rewrite pcbc's own locked copper — RELAX is right about the mechanics. But `piece_key` (`route_emit.py:115`) is pure geometry, and `_guard_gate`, `_thermal_gate`, `_barrel_gate` and `_chain_gate` (`build.py:244, 352, 169, 47`) rebuild `Piece`s from the sidecar's geometry keys and re-check semantics — `guard_cover`, `thermal_budget`, `parallel_joined`, `chain_order` — that were satisfied *by construction* at emission time. A relaxed guard is no longer a fixed offset from the path it shields. **Verdict: v1 relaxes `leftover`, `hop`, `fanout` and `tap`; it skips `spine`, `guard`, `stitch`, `thermal`, `chain` and anything `lock_copper` (`route.py:155`) locked on a constrained `NetReq`.** Honest gap: my gate-clean runs relaxed *everything*, so the restricted policy's whole-board effect is **unmeasured** — that is slice 1's day-one acceptance test.

**C5 — OWN's "beat the hand board this week".** Methodologically wrong: its headline ds2 numbers (2.31 mm/seg, 12.2 deg/mm) are computed over the **unlocked subset only** (303 of 514 mm), not the whole board, so they are not comparable to the hand board's whole-board 1.95 and 17.2. **The conclusion is nonetheless right** — my whole-board fresh run gives ds2 2.47 mm/seg and 12.1 deg/mm. Right answer, invalid arithmetic; do not reuse the method.

**C6 — Is pre-KRT copper worth its cost?** Survey Move 3(a) says reduce it; OWN says pre-KRT copper has *negative* value below a coverage threshold and post-KRT copper has positive value at any coverage; RELAX is silent. **OWN is right and I have now measured the price at whole-board scale: 25.4 deg/mm and 70 micro-segments on buck.** But OWN's conclusion ("do not put more pcbc copper in front of KRT") should not become "remove what is there": patterns-off made buck's ampacity worse. Resolution: **the relaxer makes this non-urgent** (18.1 relaxed beats 25.5 patterns-off), so unfreezing becomes a detour move, per-board and measured, not an architecture decision.

**C7 — `--turn-cost` vs the relaxer.** Survey recommends the knob first; RELAX and OWN both warn they attack the same number and will not add. **Both are right, and the knob loses.** The relaxer gets buck to 18.1 deg/mm board-wide, to a fixed point, in pcbc's own code, with no change to a pinned command and no re-pinning of KRT's behaviour. The knob gets one step from 32.5 to 20.0 and changes KRT's routes, which re-records every ceiling. **Skip the knob.** `--add-teardrops` and `--bus` are orthogonal and stay available later.

**C8 — Grid router or gridless?** Survey §5 and OWN §2 agree against `router-plan.md` §6.4's rasterised octilinear maze A*. OWN's throughput census settles it: `blocked()` costs 110–271 µs, and KRT's own c3_usb signals step takes 3,530–77,632 expansions. **`router-plan.md` §6.4 should be struck.** Nobody disputes this; it is the one place all four designs and the survey agree, and the plan document still says otherwise.

**C9 — `assert gate["soft"] == SOFT[name]`.** Confirmed at `tests/test_examples_fab.py:793`. EXPRESSIVE is right that exact equality on defect hit counts means **fixing a defect turns the suite red**. That is a real structural problem and it blocks slices 2 and 4.

---

## 4. The plan, as slices

### Slice 1 — `route_relax.py`: whole-board octilinear string-pull *(3–5 days)*
**Owns:** one new module; a new `pcbc_step("relax", "relax")` last in `krt_plan`; a branch in `route_job`; `route_emit.strip_segments` + `replace_segments`; rewriting `owned` so `copper_bar` still labels pieces (`route.py:~680` builds `reasons` from `bar_key(p)`); regenerating `copper.json`.
**Must not touch:** `route_geom.py`, `route_scene.py`, KRT's commands, net order, placement, vias, layers, widths, or any pattern copper carrying a verify rule (C4). Not a pattern — `pattern_copper` ends in `write_pieces`, which only appends.
**Acceptance:** zero new DRC errors, zero new unconnected items, no per-type violation count above base (**compare by type, not `ok` vs `ok`** — that is how both prototypes caught their own bugs), plane islands unchanged, `same_net_slots` recorded as a per-board ceiling, byte-identical output over two runs.
**Board that proves it: buck** (largest effect, 0.5 s, 20 chains) then **ds2** (the real board, 46% locked, proves the restricted policy).
**Number it must move:** buck `deg/mm` **50.9 → ≤ 20** and micro **96 → ≤ 15**; ds2 `deg/mm` **24.1 → ≤ 15**. Measured today at 18.1/6 and 12.1/10 with the unrestricted policy.

**SHIPPED 2026-09-21, and the C4 gap is closed.** The honest gap above — "my gate-clean runs relaxed *everything*, so the restricted policy's whole-board effect is unmeasured" — is now measured, with the restricted policy (`RELAXABLE = fanout, hop, leftover, tap`) on fresh builds of all five boards against the same build with `relax=False`. buck **50.91 → 18.32** deg/mm and micro **96 → 7**; ds2 **23.53 → 14.74** and micro **96 → 32** (with `deg/mm` counted at degree-2 vertices only — a branch at a via is not a corner — which is why ds2 does not reproduce the prototype's 12.1). The acceptance holds on every clause: **zero DRC errors, zero unconnected items, and no per-type violation count above base on any board**, compared by type off one `kicad_drc` run per arm; only `track_segment_length`, `track_angle` and `track_width` move, and all three fall on all four boards that have any. Plane islands unchanged; plane area moves by +0.04 mm² on c3_usb and −0.01 on node and nowhere else. **Three** independent builds of every board are byte-identical once the build directory's own path in the `(model ...)` lines is normalised. `same_net_slots` is recorded per board and buck's fell 1 → 0 — its one slot was `EN`'s leading leg passing 0.0605 mm from `U1.5`'s own pad, and that leg is exactly the one the trim drops. `orphan_copper` is 0 on all 64 nets of all five boards on both arms, in millimetres of centreline rather than items.

**One defect the acceptance list did not name, found while landing it and fixed.** C3's preserve set says the run's centreline stays inside the copper it joins, and `hold2 = max(shape.r**2, reach_in**2)` says exactly that and nothing more — it does **not** say a joint may not get shallower, which the module's own docstring claimed. On fresh builds the `trim` took 27 of 302 pad joints shallower, three of them below the board's own `clearance_min`: c3_usb's `C_EN.2` **0.2700 → 0.0760 mm** against a 0.127 mm floor, its `U1.23` 0.2 → 0.1, ds2's `R5.2` **0.5075 → 0.1175**. 76 µm is three times the 23.5 µm this plan already calls unshippable. A pad now carries `floor = stackup.clearance_min` in `hold2` (a via and an immovable track do not — neither is a solder joint), which costs blinky, buck and node **nothing byte-for-byte**, c3_usb one accept, one segment, 0.28 mm and 0.10 deg/mm, and ds2 one segment, 0.56 mm and 0.07 deg/mm. It buys `C_EN.2` and `R5.2` back outright — they stop retracting at all, ending at 0.2800 and 0.5125 mm — and it cannot reach `U1.23`, a **square** pad whose `Shape` carries `r = 0`: `hull_dist2` is 0 anywhere inside a rectangle, so the strongest bound expressible is "the centreline is in the outline". A signed depth would fix that and no board here needs one.

### Slice 2 — mitre board-wide, and stop the pair leak *(2–3 days)*
**Owns:** `patterns.mitre` (`patterns/__init__.py:456`) applied to every surviving 90° in the `relax` step; and EXPRESSIVE's four edits — `!pair` in `signals` (`route.py:364`), `*cn.layers` instead of `*layers` in the pair step (`route.py:328`), `--max-ripup 5` on it, and an unfinished pair as a refusal.
**Must not touch:** the meander/tune engine. EXPRESSIVE recommends against it and I agree — after the leak fix the measured residual is inside budget on c3_usb.
**Acceptance:** c3_usb pair skew **2.190 mm → ≤ 0.5**; `track_angle` down on c3_usb and node; no net newly unrouted.
**Board: c3_usb** (the pair) and **node** (`deg/mm` 29.0 after slice 1 at 85 deg/corner — nearly all 90° corners, which is what mitre is for).
**Number:** c3_usb and node `deg/mm` **29 → ≤ 22**, skew **2.19 → ≤ 0.5**.
**Blocker:** `SOFT` must move from `==` to `<=` first, or this slice reds the suite by fixing the defect it pins.

**REFUTED 2026-09-21, by building it.** The pair half of this slice was designed off a number that is
not skew and against a board that is not the one with the defect. Every claim below is a fresh build
of c3_usb and node with `kicad_drc` as the arbiter.

**1. The magnitude was wrong in the other direction too.** "c3_usb skew 2.190 mm against 0.5, 4.4x
over" is a `copper_bar` `routed_mm` difference between the two nets, and those nets do not carry the
same number of pads — so it measures stubs, not skew. **KiCad reads c3_usb's `skew_out_of_range` at
0.6154 mm**, 1.23x over. The board with the real defect is **node**, which this plan never built:
**11.9196 mm, 23.8x over**, with `diff_pair_uncoupled_length_too_long` at **22.81 mm against 2.0**.

**2. Three of the four edits do nothing or harm.**
- `!pair` in `signals` (`route.py:364`) leaves **3 pads unconnected on c3_usb and 6 on node**, so
  neither board builds. On the boards it produces, c3_usb's skew is *worse* (0.6154 -> 4.8613) and
  node's is much better (11.9196 -> 1.3212, uncoupled 22.81 -> 4.60).
- `*cn.layers` instead of `*layers` on the pair step (`route.py:328`) is an **exact no-op**: `cn.layers`
  is already `("F.Cu", "B.Cu")` on both pair boards, and node's `In1/In2` are plane layers the pair
  router never used. Byte-identical output.
- `--max-ripup 5` on the pair step is an **exact no-op**. Byte-identical output. (KRT's own
  `routing_defaults.py:115` says it reverted `MAX_RIPUP = 5` upstream for making its holdout sets
  worse; this repo has no counter-measurement and now has one: nothing.)
- **an unfinished pair as a refusal** is the one that survived, and it shipped — `route._unrouted_move`
  gains a `diff_pair` branch. Its move had to be checked against the compiler before it could be
  written: **`pair=False` is not the edit**, because the `usb_hs` preset carries
  `autoroute="diff_pair"` and `constraints.py:1860`'s `if req.pair:` can only turn it *on*.
  `autoroute=True` is the override the preset loses to.

**3. Two further arrangements, built and rejected.** **Locking the pair's copper** after its own step
(the `_nets` steps' own trick): byte-identical on c3_usb, and node 11.9196 -> 12.0853. So `signals`
is not ripping a pair up — it is **adding** to it. A dedicated **`pair_stubs` step** immediately
after the pair step, on a near-empty board, with the pair copper locked: c3_usb 0.6154 -> **8.0421**,
node -> **23.4034**. Both worse than the thing they replaced. Letting pairs into `local_hops` is a
third no-op — `_local_nets` wants *every* pad of a net within `LOCAL_MM`, and a pair spans the board.

**4. What it actually is.** A USB-C pair net on these boards is **not a two-terminal net**: c3_usb's
`USB_DP` has five pads (`J1.A6, J1.B6, U1.27, U3.1, U3.6`) because the connector carries both
orientations and the ESD array has two sides. `route_diff.py` routes one coupled path per net, so
those pads were never its job, and `signals` is the only step that reaches them. On node that copper
is **19.5 mm** and it *is* the skew. So the fix is not a step-list edit: it is pcbc owning the short
hop to a pair net's extra pads before KRT runs, and it is filed as roadmap item 6.

The **mitre** half of this slice is untouched and still stands.

### Slice 3 — `pcbc place --probe` *(1 week)*
**Owns:** sweep one declared DOF at a time (`toward=`, `to=`, `rot=`, `gap=`, anchor lattice, `Place()` order), evaluate each with a real build, print a table of `copper_bar` fields per candidate plus the **neighbourhood-feasibility fraction**. Prints lines; writes nothing.
**Must not touch:** the default build path, `pcb_place._attach`'s score (`pcb_place.py:534`), the `order=` tie-break (`pcb_place.py:373`). No autoplacer.
**Acceptance:** the probe runs on **node**, which nobody has swept — if node's band is narrow, the thesis is bounded and we learn it in an afternoon.
**Board: node first** (unmeasured), then ds2 (feasibility).
**Number:** produce the feasibility fraction for all five boards, and a per-board detour band. Target: buck worst detour **2.55 → ≤ 1.70** via a one-token edit the probe names.

### Slice 4 — unfreeze, per board, measured *(1–2 weeks)*
**Owns:** a per-board switch to route `signals` before locking pcbc's spine, plus an optional final whole-board rip-up (`--rip-existing-nets '*' --force-reroute`) protected by KRT's own `improvement_gate`. Slice 1 must already be in, because the relaxer is what makes the shape safe to trade.
**Must not touch:** the tap and plane stages (removing them left node with 72 unconnected items) or ampacity structure (patterns-off made buck's VIN narrowest track 0.3905 → 0.127 mm).
**Acceptance:** detour improves and neither ampacity nor plane coverage regresses.
**Board: c3_usb** (USB_DN 1.13 at the pair step → 1.81 after signals is the cleanest traced case) and **buck** (VIN 2.55).
**Number:** c3_usb worst detour **1.70 → ≤ 1.35**; buck VIN **2.55 → ≤ 1.80**.

**MEASURED AND DECLINED 2026-09-21, and not by unfreezing anything.** The cheapest half of this
slice was a KRT flag nobody had tried through the real pipeline: `--ordering original` against its
default `mps`. The mechanism ships as **`Board(net_order=)`**, a per-board declaration rather than a
default, because it measured differently on every board — and then **no board took it**, because the
one win it offers is paid for in ampacity. Fresh builds, all five, 0 errors and 0 unconnected on
every arm:

| | copper | worst detour | segs | vias | deg/mm | DRC by type vs base |
|---|---|---|---|---|---|---|
| **buck** | 147.37 -> **133.06 mm (-9.7 %)** | `VIN` **2.51 -> 1.84** | 73 -> 72 | 7 -> 8 | 18.32 -> **20.45** | `track_angle` 20 -> 22, `track_segment_length` 7 -> 8, `track_width` 8 -> 7 |
| ds2 | 503.55 -> **499.58 mm** (-0.8 %) | `REFP_F` 3.23, unmoved | 228 -> 230 | 28 -> 30 | — | **identical** (114 warnings, both arms) |
| c3_usb | 358.80 -> 358.93 (+0.0 %) | 1.69 -> **1.73**; `EN` 1.04 -> **1.73** | 302 -> 308 | 55 | — | `track_segment_length` 83 -> 87, `track_width` 25 -> 28, `track_angle` 116 -> 112 |
| node | **byte-identical** | | | | | |
| blinky | **byte-identical** | | | | | |

**No board declares it, and buck is why.** buck's `VIN` lands at **1.84 against this slice's ≤ 1.80**
and the board sheds 9.7 % of its copper — and then the verification layer said what that bought:

> `VIN` makes **two layer changes it does not make under `mps`**, each on a **single 0.3 mm via**.
> `route_verify.via_parallelism` reads **0.707 A carried against 3 vias needed** at `(18.2, 8.45)`
> and `(18.25, 5.75)` on a **2.0 A** rail; `build._barrel_gate` lists `VIN` in `short` on a board
> whose `short` was empty; `PARALLEL["buck"]` goes `{}` -> two singleton groups; and
> `test_technique_one_fires_on_exactly_one_net_on_exactly_one_board` stops being true.

The shorter route is shorter because it hops layers through vias rated at a third of the rail. That
is **ampacity** traded for detour, not shape traded for detour, and a 2 A rail does not make that
trade. buck's turning also goes 18.32 -> **20.45 deg/mm**, missing slice 1's own ≤ 20 bar, but the
vias are what decided it.

So this slice ships the **mechanism** — the declaration, its refusal, its measured scope — and no
declarer. All five boards stay byte-identical to base and **not one recorded number moves**. Four
`test_route_verify_stitch.py` failures were the entire evidence, and they only exist because this
repo records per-board via parallelism; shipped as the *default* the plan asked for, buck would have
carried two under-rated vias and those four numbers would have been re-recorded around them.

**The scope is measured, not tidy:** `net_order` reaches **the `signals` and `finalize` steps only**.
Given to every `route.py` step — `local_hops`, the constrained `*_nets` steps, `plane_taps` — the DS2
Addon stops being a board: two `items_not_allowed` errors and a `solder_mask_bridge` from
`kicad_drc`, where the `signals`-only arm is identical to base on every type. The constrained steps
route on an otherwise empty board and have no congestion for an order to negotiate.

What is left of this slice is the unfreezing itself, and it is unchanged.

### Slice 5 — the topological phase, as a routability oracle only *(1 month, optional)*
**Owns:** constrained Delaunay over pad/via/obstacle vertices per layer, exact channel capacity `Σ(width + clearance) ≤ edge_length` — **not** from `route_scene.free_intervals`, which projects an AABB along one of four fixed axes (`_AXES` raises on any other) and cannot answer "how much room is there between these two pads"; capacity comes from the gate's two endpoints and `ClearanceTable.between` (`docs/topo-plan.md` C-D, and shipped in `route_channel.py`) — PathFinder negotiated congestion over triangulation edges — **and no embedding**. Its output is a per-corridor overflow number fed back to `pcb_place.py` and printed as a `Place()` move.
**Must not touch:** the routing path. Do not embed, do not replace KRT.
**Acceptance:** on buck's 20-point anchor grid it must separate the 8 unroutable placements from the 13 routable ones better than today's gate (17 accepted, 5 of them unroutable — a 29% false-accept rate).
**Board: buck** (the grid is already recorded).
**Number:** false-accept rate **29% → ≤ 10%**.

---

## 5. What this replaces

**KRT is kept and demoted, not removed.** Today it is "the router" — it draws 80–92% of every board and pcbc accepts the output. After slice 1 it is **the completion engine**: it decides connectivity and homotopy, and pcbc owns the final geometry. After slice 4 it is a **negotiator** whose net order and rip-up matter more than its cost function.

**Nothing removes it on this evidence.** OWN's own search prototype — the only one anybody built — completed 29 of 43 links on ds2, on one layer, with no vias, and emitted `TURN_MM`-driven copper at 44.2 deg/mm, worse than KRT. The bake-off in `router-plan.md` §7 is the right gate, and the preconditions for even holding it are: pcbc's own proposer completes ≥ 95% of links on all five boards **with** vias and layer changes, and beats KRT on detour, not just on corners. That is not within a quarter.

**What is removed now: `router-plan.md` §6.4.** The rasterise-into-a-cell-grid octilinear maze A* plan should be struck from the document. All four designs and the survey agree, the throughput census makes it arithmetic rather than taste, and leaving it in the plan is the single likeliest way for someone to spend a quarter arriving at KRT's quality with none of KRT's 147-board tuning.

---

## 6. House rules that must change

**"No scoring, no backtracking" → "no scoring *inside a pattern*; a whole-board pass may search."** `patterns.link_candidates` says "Order is the answer; there is no scoring", and that rule is correct *for a pattern* — a pattern is a 112-candidate enumeration whose determinism comes from first-accept over a fixed list. It cannot survive contact with a relaxer, which needs a potential function to terminate at all.

*Defended against determinism:* the relaxer's determinism does not come from the absence of scoring, it comes from (a) a fixed chain order, (b) a fixed scan order — `i` ascending, `j` descending, `diagonal_first` then not, (c) first-accept rather than best-accept, and (d) every coordinate through `route_geom.q` exactly once. I verified it: two runs of buck produced byte-identical output. A *bounded, first-accept, fixed-order* search is as much a pure function of the input as an enumeration is.

*Defended against "a refusal is a move":* the relaxer's refusals are strictly better than the patterns'. `blocked()` returns the one worst `Clash` with `Clash.line()` already in pcbc's move shape, and the prototype recorded 120–917 rejected pulls per board. Every one names an obstacle, a gap and a requirement. That is more moves, not fewer.

**"A pattern never degrades: class width or nothing."** Keep it, and satisfy it *structurally* by putting width in the chain key — a width change becomes a chain boundary, so the pass cannot change a width even by accident.

**"Copper written in `final` disturbs nothing" extends from adding to replacing.** `stitch-plan.md` R-S1 justifies `final` by redundancy ("copper whose absence leaves nothing unconnected"), which does not reach a rewrite. The argument that does is **positional** — `krt_plan` schedules nothing after `patterns_final` (`route.py:429`), so no router can react — plus contact preservation, which makes connectivity provable rather than hoped for. Write that argument down explicitly; do not let it be inherited from R-S1.

**`assert gate["soft"] == SOFT[name]` must become `<=`** (`tests/test_examples_fab.py:793`). Exact equality on defect hit counts means every fix turns the suite red, and a dimensionless hit count cannot tell 0.6 mm from 11.6 mm. Replace the counts with EXPRESSIVE's `declared()` verdicts in millimetres. This is a prerequisite for slice 2, and it is the cheapest item in the whole plan.

---

## 7. What I would not build

- **A grid maze router, in Python or Rust.** `router-plan.md` §6.4. It reproduces KRT with pcbc's bugs and wastes the exact grid-free substrate.
- **`--turn-cost` tuning.** It attacks the same number as the relaxer, wins less, changes KRT's routes, and re-records every ceiling. Revisit only if slice 1 underperforms.
- **`patterns/tune.py`, the meander/accordion engine.** EXPRESSIVE specified it and then argued against building it, correctly: after the pair leak is fixed the residual skew is inside budget on c3_usb and is a `Place()` move on node. No board in the repo justifies the mechanism.
- **OWN's `route_search.py` as a shipping feature.** 29 of 43 links, one layer, no vias, and worse corner density than KRT. Its value was the measurement, and the measurement is now banked.
- **A placement *search*** (as opposed to slice 3's probe). 8 of buck's 13 routable placements are Pareto-optimal and **none dominates the shipped board**; a scalarised search with badly-chosen weights will confidently ship worse boards than an AI wrote by hand. Print the table, let a person write the line.
- **Anything depending on headless push-and-shove.** Verified absent from KiCad's Python bindings.
- **The topological router's embedding half.** Slice 5 builds the oracle, not the router. The embedding is the largest single piece in the survey and it pays off only after unfreezing and placement have taken their share of the detour.

---

## 8. The honest cost, and what you get if you stop

| slice | effort | if you stop here |
|---|---|---|
| **1. relaxer** | **3–5 days** | **Hand-class shape on the shipping boards.** buck 50.9 → 18.1 deg/mm, 96 → 6 micro, 175 → 69 segments; ds2 24.1 → 12.1; `track_segment_length` warnings 95 → 6. Detour, vias, placement and completion all unchanged. This is the best stopping point in the plan and I would ship it alone. |
| **2. mitre + pair leak** | **2–3 days** | Plus: c3_usb's declared 0.5 mm skew budget actually met (2.19 → ≤ 0.5); node and c3_usb's residual 90° corners cut; off-45 near zero. The tests finally measure magnitudes instead of hit counts. Still no detour movement. |
| **3. placement probe** | **1 week** | Plus: a ranked list of one-token `Place()` edits with measured prices, and the feasibility fraction — the number that says "the board is full" rather than "try harder". Buck's worst detour reachable at 2.55 → ~1.70 by an edit the probe names. Nothing in the default build changes. |
| **4. unfreeze** | **1–2 weeks** | Plus: the detour class opens. c3_usb's USB_DN stops paying 16.4 mm to route around copper the signals step was forbidden to touch. Risk: per-board regressions in ampacity and plane coverage; this is the first slice that can make a board worse, and it needs the `improvement_gate`. |
| **5. topological oracle** | **~1 month** | Plus: placement failures become per-corridor arithmetic instead of "nothing pcbc can see", and the 29% false-accept rate closes. This is the root-cause fix for task finding #4 and it is the only slice I would call optional. |

**Total to hand-quality shape and a correct pair: about two weeks.** Total to close the detour class as well: about six. The ceiling beyond that — a full topological router with embedding and negotiated congestion — is a quarter, and on today's numbers it buys the last 10%, not the first 60%.

**Files read for the claims above:** `/Users/rileymccarthy/pcbc/src/pcbc/route.py` (`krt_plan:179`, the `diff_pair` skip at `:263`, `pairs` at `:295`, the pair step's `--layers` at `:328`, `signals` at `:364`, `pcbc_step`, the `PCBC_STEP` branch and its `continue` before `_lost`, `lock_copper:155`, `bar_key:722`), `route_emit.py` (`piece_key:115`, `segment:126`, `write_pieces:161`), `build.py` (`_plane_gate:21`, `_chain_gate:47`, `_barrel_gate:169`, `_guard_gate:244`, `_stitch_gate:297`, `_thermal_gate:352`), `pcb_place.py` (`order:373`, `score:534`), `place.py:42`, `tests/test_examples_fab.py:552,793`, `docs/router-plan.md` §0–2. Measurement scripts and all fresh builds are in `/private/tmp/claude-501/-Users-rileymccarthy-Documents-MaD/53e19ae4-b53e-47b5-bcd4-d03eaa58410c/scratchpad/` (`bar.py`, `fresh/`, `gate_buck/`, `gate_ds2/`). **No file in `~/pcbc` or in MaD was modified**; every build was run on a copy in the scratchpad.