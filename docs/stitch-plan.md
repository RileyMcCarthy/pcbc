# Stitching: the plan

Full stitching support for pcbc, designed 2026-09-20 by a judged panel — three independent
architectures (minimum mechanism, risk first, electrical correctness) and six per-technique designs,
resolved by one judge. Everything below is measured against the five boards unless it says otherwise.

**The panel corrected two premises the brief asserted**, and both corrections are load-bearing:

- `route_verify`'s D.1 item 6 does **not** block return vias: a return via's net *is* the reference
  net, and `cs.by_net("GND").reference` is `None` on every board. The deferral it encodes stays.
- `netcheck.check_copper` does **not** compare track connectivity: `copper_nets` reads pad bindings
  inside footprint blocks only and never sees a segment or a via. A component bridge needs no gate
  change; the judge of a copper tie is KiCad's own `shorting_items`, and that is why a copper tie is
  refused rather than exempted.

## 1. The architecture to build

**One new stage, one new pattern module, two new declarations, and three of the six techniques ship as verification only.** The spine of the design is MINIMUM MECHANISM's unification; the governing *rule* is RISK FIRST's; the honesty about populations is ELECTRICAL CORRECTNESS's and the per-technique designs'.

### 1.1 The governing rule (taken from RISK FIRST, §0)

> **R-S1. Copper whose absence leaves nothing unconnected is written after the last router step that could have used the space it takes.**

This is the best single sentence in the three proposals because it explains the ds2 failure instead of merely avoiding it. The ds2 mechanism is *reaction*: it requires a router step to run after the copper. A tap cannot take that position (measured: taps after `signals` left 21 unconnected pads, `route.py:302-305`); a stitch can, because a stitch that loses its site costs a shield, not a pad. Two corollaries, non-negotiable:

- **R-S2.** A stitch takes what is left; it is never an input to a route.
- **R-S3.** A stitch whose count is an *electrical requirement* is all-or-nothing; a stitch whose count is a *target* applies partially and reports the shortfall. (This generalises B.6's guard exception into a rule instead of a special case.)

### 1.2 The stage — `final`, and POST is structurally impossible for the headline technique

All three architectures and five of six technique designs converged on a new stage after KRT's last step. I verified the reason that makes it not a preference:

```
node's VBUS vias, measured on examples/node/layout/node/routed/04_signals.kicad_pcb:
  4 vias, all 0.35/0.2 (the STACKUP via, i.e. KRT's — VIAS_PATTERN["node"] == {"tap": 62})
```

`VBUS` is a 6-pad power net with no plane, so KRT routes it in `signals`, which runs **after** `patterns_post`. At POST time the via to be paralleled does not exist. POST is not a worse choice for technique 1; it is an impossible one. Same for a return via (pairs are KRT's) and for a guard on a KRT-routed net (measured: the analog nets one guards are *all* KRT's on all five boards).

```python
# src/pcbc/route.py — krt_plan, the only ordering change
    def pcbc_step(name: str, stage: str) -> None:          # route.py:213, one parameter added
        nonlocal prev
        out = work / f"{len(steps) + 1:02d}_{name}.kicad_pcb"
        steps.append((name, [PCBC_STEP, "-X", "utf8", "patterns", str(prev), str(out), "--stage", stage]))
        prev = out

    if post: pcbc_step("patterns_post", "post")   # route.py:307 — UNCHANGED position
    ...
    step("signals", "route.py", signals)          # route.py:321
    if job.layers <= 2 and "GND" in power:
        step("gnd_pour", ...); step("finalize", ...)
    if post: pcbc_step("patterns_final", "final") # NEW — the last step of the plan, both stackups
```

```python
# src/pcbc/patterns/__init__.py
POST  = ("tap",)                                  # UNCHANGED
FINAL = ("stitch",)                               # one module, five carriers (§1.3)
_STAGES = {"pre": PRE, "mid": MID, "post": POST, "final": FINAL}
```

**`route_job` must keep plans per stage, not in one variable.** This is the one piece of plumbing all three got loose, and RISK FIRST alone spotted why (its R4):

```python
# src/pcbc/route.py — route_job, the PCBC_STEP branch (was route.py:481)
stage = cmd[cmd.index("--stage") + 1]                  # today hardcoded "post"
plans[stage] = pattern_copper(design, job, job.constraints, current.read_text(), name, stage=stage)
owned += list(plans[stage].pieces)
```
and every downstream reader keyed on the stage it means:
- `plane_taps`' skip reads `plans["post"].refusals()` **explicitly** (`route.py:496-506`). It is safe today only because `plane_taps` runs before `patterns_final` — an ordering invariant, not a type-level one. Make it explicit so a future reorder cannot break it silently.
- the sidecar gains `sidecar(plans["final"].pieces, step="patterns_final")` beside `patterns_post` (`route.py:580`).
- `result["pattern_links"]` merges three plans, not two.
- `_lost` needs no guard — it runs per KRT step and there are none after.

**Why `final` is provably the narrowest blast radius**, and it is testable rather than argued: nothing routes after it, and `unrouted_nets` (`copper.py:121`) is a *presence* test, not a connectivity walk, so a final-stage via cannot make a net read unrouted. The complete list of things that still read the board after `final` is: `pin_copper_ids` (geometry-keyed, uuids in `keep=`), `copper_bar` / `same_net_slots` / the sidecar (read-only), and **KiCad's `--refill-zones --save-board` during the gate**. That refill is the *entire* residual risk surface, and it is the one failure pcbc already instruments end to end (`antipad_clash` predicts it per candidate; `plane_area` + `plane_islands` measure it after; `PLANES` pins it to 0.05 mm²). The chain had no such instrument for the mechanism that killed it.

**Consequence, and it is the acceptance criterion of S2:** `BAR`, `DETOURS`, `SOFT`, `LEFTOVER`, `SPINE_*`, `REFUSED` and `TAP_REFUSED` are *provably* unmoved on all five boards. Only `VIAS_PATTERN`, `OWNS`, `PLANES` (area), `SAME_NET`, `COURTYARD` and `BOTTLENECK` can move.

### 1.3 The mechanism — one module, `StitchSpec`, three site generators

MINIMUM MECHANISM's unification is correct and is the smallest thing that covers the techniques worth building. `tap.run` (`tap.py:325-382`) is already the general algorithm: walk an ordered site list, ask five questions per site (`_in_a_pad`, `blocked`, `antipad_clash`, `lane_ok`, `raster.reaches`), take the first that passes. Only three things vary.

```python
# src/pcbc/patterns/stitch.py   (new — the only new pattern module)
REASON = "stitch"        # plus "guard" for the two tracks a guard writes
CONNECTS = False         # see §2(k)

@dataclass(frozen=True)
class StitchSpec:
    carrier: str             # "parallel" | "thermal" | "guard"
    net: str                 # the net the vias carry
    sites: tuple[Pt, ...]    # the fixed, finite, ordered candidate list
    need: int                # how many this spec must place
    required: bool           # True: all-or-nothing; False: partial, with a Coverage record
    via: tuple[float, float] # (diameter, drill) — see §2(h)
    joins: tuple[str, str]   # the two layers every via must land in real copper on
    owner: str               # "U1.49" | "VBUS via at (32.30,34.90)"
    why: str                 # the arithmetic, for the report and the refusal
    bound: int               # the declared candidate bound

@dataclass(frozen=True)
class Coverage:              # from ELECTRICAL CORRECTNESS §6 — the best idea in that document
    want: int; got: int
    measure: float; target: float; unit: str   # "A" | "K/W" | "mm"
    floor_ok: bool
    why: str

def sites_ring(ctx, at, dia, reach) -> tuple[Pt, ...]      # carrier: parallel
def sites_lattice(ctx, boxes, pitch) -> tuple[Pt, ...]     # carrier: thermal
def sites_along(ctx, path, pitch) -> tuple[Pt, ...]        # carrier: guard
def specs(ctx: PatternCtx) -> tuple[StitchSpec, ...]
def run(ctx: PatternCtx, spec: StitchSpec) -> PatternResult
```

`run` is `tap.run` with two edits: accept until `len(placed) == spec.need`; then
- `required and got < need` → emit **nothing**, one refusal via `blocking.move_line`;
- `not required` → emit what fitted, plus **exactly one** `style:` note carrying the `Coverage` record.

`PatternResult` gains `cover: Coverage | None`. **One refusal per spec, never one per site** — this is what stops `REFUSED` (an exact pinned number) from flooding, and an implementer who skips it will pass their own tests and destroy the ledger.

### 1.4 What I took from where

| taken | from | why |
|---|---|---|
| R-S1/S2/S3, the load-bearing-vs-redundant split | RISK FIRST | the only framing that *explains* ds2 rather than dodging it |
| one module, `StitchSpec`, three site generators, `required` | MINIMUM MECHANISM | smallest mechanism that covers what ships |
| `Coverage` + one-refusal-per-spec | ELECTRICAL CORRECTNESS | the only one that saw `REFUSED` flooding |
| mandatory link segments on a parallel via; match the anchor's size | power-vias | keeps `_via_clusters` honest (§2g, §2h) |
| pitch derived from `antipad_clash`'s own arithmetic | thermal | corrects R-T1's own number (§2m) |
| `next_nm` offset, drop `spacing_w`, `CONNECTS=False` | guard | all three measured (§2i, §2k) |
| the `Bridge()` shape, two-pin rule, `off_board` kind | bridge + ELECTRICAL CORRECTNESS | measured on the one board that has the problem |
| the reference classifier, report line, `RETURNS` | return-vias | the only design that asked the stackup what a layer change *does* |
| **refusing** plane stitch and return-via placement outright | mine | §8 |

---

## 2. Every contradiction, and its resolution

**(a) Stage name.** `final` / `final` / `LAST`. → **`final`**. Trivial; two of three.

**(b) The brief's premise on D.1 item 6 is wrong, and three designs said so.** Brief: "`route_verify` ASSERTS pcbc places no via on a net carrying `Constraint.reference` … would have to change." Measured: `_via_rules` reads `cs.by_net(p.net).reference` (`route_verify.py:234`). A return via's net is the *reference* net. Measured reference nets on all five boards:

```
node:   USB_DN -> In1.Cu,  USB_DP -> In1.Cu      c3_usb: USB_DN/USB_DP -> "B.Cu pour"
blinky, buck, ds2: none
```
`cs.by_net("GND").reference` is `None` everywhere (`wants_reference` is only set for `usb_hs` or a declared `z_se_ohm`, `constraints.py:1017`; GND is `kind="power"`). **Item 6 needs no change, keeps its value, and the deferral it encodes stays.** Resolution: MINIMUM MECHANISM / ELECTRICAL CORRECTNESS / return-vias are right; the brief is wrong.

**(c) The brief's premise on the copper gate is wrong, and all three said so.** Brief: "`netcheck.check_copper` compares copper connectivity against board.py's netlist exactly, so copper joining two declared nets FAILS the gate." Measured: `copper_nets` (`netcheck.py:125-146`) matches `(pad "N" … (net "NAME"))` **inside footprint blocks only** — it never reads a `(segment)` or a `(via)`. A track joining GND to VSS changes no pad binding and `compare()` passes it. The judge is KiCad's `shorting_items`, surfaced by `fab.copper_drc_errors` (`fab.py:489-505`), which exempts only same-footprint pad-pad (`_same_footprint_pad_pad`, `fab.py:473`). **Consequence: a component bridge needs no gate change at all, and a copper bridge would need an exemption on a genuine KiCad error — which would also excuse the accidental short the check exists to catch.** Refuse the copper bridge (§8).

**(d) Return vias: MINIMUM MECHANISM would ship copper that is not a return path.** MINIMUM MECHANISM's S4 places `return` vias on node and c3_usb; return-vias says the population is zero. Measured `Stackup.height_to_reference`:

```
node   F.Cu -> (0.2104, 'In1.Cu')   B.Cu -> (0.2104, 'In2.Cu')    In1 is GND, In2 is 3V3
c3_usb F.Cu -> (1.53, 'B.Cu pour')  B.Cu -> None
```
A via joins one net to itself. On node a layer change changes the reference **net** (GND→3V3): no return via carries that current. On c3_usb it **loses** the reference: there is no second plane to reach. **return-vias is right.** Resolution: technique 5 ships as a classifier + compile note + finished-board verifier, and places **zero copper**. This is the most consequential contradiction in the set — the alternative is decoration that the tool's own report would then bless.

**(e) Parallel via: bare via, or via + link segments?** MINIMUM MECHANISM emits a via only. power-vias and ELECTRICAL CORRECTNESS require the two link segments / a `parallel_joined` gate. Measured: `ampacity._via_clusters` (`ampacity.py:148-167`) is single-linkage on `math.dist(...) <= VIA_PARALLEL_MM` with **no connectivity test**, and `net_nodes` (`ampacity.py:199`) then assigns each member `cluster_size * via_amps(drill)`. A bare twin 0.9 mm away and joined to nothing would **double the reported ampacity of a board carrying no more current**. → **power-vias/ELECTRICAL CORRECTNESS win.** A rung is a via **plus one link segment on each of its two layers, at the class width**, unconditionally. Anything less is pcbc gaming its own measurement.

**(f) Parallel via size: class via, or match the anchor?** MINIMUM MECHANISM passes `Constraint.via`; power-vias matches the anchor. → **power-vias**, and the arithmetic is decisive. A mixed cluster reports `n × via_amps(larger drill)`. Worse, on node the class via is 0.8/0.4 and its antipad neck in the 3V3 plane puts the nearest legal partner at **1.080 mm** against `VIA_PARALLEL_MM = 1.0` — a class-size partner **cannot be placed at all**. One via must mean one thing.

**(g) Thermal in POST-before-tap, or `final`?** ELECTRICAL CORRECTNESS and the thermal design put it in POST before `tap`, so `tap._welded` skips the pad. MINIMUM MECHANISM puts it in `final` with a new `tap` skip string. **Both create the same hazard and neither named it:** if `tap` skips the pad and `thermal` then refuses every site, the pad is connected to nothing — an unconnected item, a *hard* build failure, on the one pattern allowed to apply partially.

→ **My resolution, which neither proposed: thermal runs in `final` and makes NO change to `tap` at all.** The pad keeps its tap via. The array is redundant thermal conductance on top of an electrical connection that already exists, which is exactly what R-S1 says it is. Cost: one extra via and (measured) one array site lost to `hole_to_hole` against the tap via — node's tap sits ≈0.99 mm from a block centre and the nearest array site at ≈0.43 mm, 0.56 mm apart against a 0.7 mm requirement, so `blocked()` drops that one site and the array places the rest. The hazard disappears entirely and `tap.py` has a zero diff.

**(h) Thermal pitch: R-T1's hole-to-hole, or derived?** R-T1 says "a grid of vias at the stackup's hole-to-hole" = 0.700 mm on node. The thermal design's arithmetic, verified against the real `zone_rules` (`[('GND','In1.Cu',0.184,0.1), ('3V3','In2.Cu',0.184,0.1)]`) and `between("GND","3V3") = 0.2`:

```
antipad radius in 3V3 = 0.175 + 0.2 = 0.375  ->  diameter 0.750
web at a 0.700 pitch  = 0.700 - 0.750 = -0.050 mm   ANTIPADS OVERLAP
derived pitch = 2*0.2 + 0.1 (min_thickness) + 0.35 (ring) = 0.850, +EPS -> 0.8501
web at 0.8501         = +0.1001 mm, a tenth of a micron over min_thickness
site count per 1.45 mm block: 1 + floor(1.100/0.700) = 2 ; 1 + floor(1.100/0.8501) = 2  — IDENTICAL
```
**R-T1's number writes the same copper and deletes the plane.** → thermal design wins; the pitch is `antipad_clash`'s own arithmetic turned from a test into a constructor. This is the single most useful correction in the six technique designs.

**(i) Guard offset.** B.6: `d = w/2 + max(between, spacing_w * w)`. Guard design: `d = next_nm(w/2 + between + max(track_min, via_diameter)/2 + EPS_MM)`, `spacing_w` dropped. → **guard design**, on three grounds, two of them measured: B.6's formula omits the guard track's own half-width; it omits the stitch via's ring (0.5 mm against a 0.127 mm track on ds2 — B.6's via cannot sit on B.6's track); and `spacing_w` is a crosstalk rule between two *signal* nets, measured as not even monotone (ds2 `AIN3` 59.9 % → 28.2 % when widened to 3W). The `next_nm` is not a flourish: at exactly `need + EPS_MM`, `clears()` loses on the float boundary and **every stitch via on every board was dropped**; one nanometre fixes all of them. The guard is the first pattern whose whole geometry sits exactly on the clearance on every leg, so it is the first to meet this.

**(j) Guard's "pcbc routed it" precondition.** B.6 refuses to guard KRT's copper. All three architectures and the guard design say `final` removes the stated reason ("cannot re-check when KRT moves it" — nothing moves it). → **Drop the precondition.** Recorded as the one decision that ties a guard's shape to `route.KRT_SHA` (pinned, so deterministic; brittle across a KRT bump).

**(k) `CONNECTS`.** Only the guard design raised it. Without it `pattern_copper` does `claimed.add(res.net)` for a guard that wrote only `GND` copper, and `PatternPlan.done` says something false about the guarded net. → **Take it.** `CONNECTS: bool = True` on hop/spine/tap/chain, `False` on the stitch module; `pattern_copper` skips `claimed.add` and the `done` recomputation for it.

**(l) Plane-stitch pitch: 5 mm (R-E1) / 6.6 mm (λ/20, plane-stitch) / 9.98 mm (λ/20, ELECTRICAL CORRECTNESS).** Three numbers, and the two derived ones disagree by 50 % because they fold the nudge budget differently. → **None ships (§8).** Measured, `plane_targets` on all five: `blinky/buck/c3_usb/ds2 = (('GND','B.Cu'),)`, `node = (('GND','In1.Cu'), ('3V3','In2.Cu'))`. **No board pours one net on two facing layers.** node's two planes are different nets — a via between them is a short, and what that pair wants is a stitching capacitor, which is technique 6.

**(m) The bridge's name.** `Tie` / `Split` / `Bridge`. → **`Bridge`**. `Split` implies pcbc enforces a separation it does not enforce (no moat by default — measured, see (n)); `Tie` is fab vocabulary for the copper form, which we refuse. `Bridge(a, b, at=)` asserts what the verification checks: exactly one path.

**(n) The moat / `RuleArea` for the split.** RISK FIRST wants `Split(gap_mm=)` raising `between(a,b)` and optionally a RuleArea. Measured on ds2 — `check_isolation`'s own four-axis test finds **no axis separates the two domains**:

```
x [13.270,37.750] vs [1.520,41.928]  overlap 24.480 mm
y [4.916,24.130]  vs [3.200,20.360]  overlap 15.444 mm      GND's interval is strictly INSIDE
u [21.058,43.756] vs [13.470,41.668] overlap 20.610 mm      VSS's on all four axes
v [-2.291,14.506] vs [-13.322,20.195] overlap 16.797 mm
```
ds2's split is two names, not two regions. → **No moat, no RuleArea, no clearance raise.** Don't build it (§8).

**(o) parallel count on node: 2 rungs (power-vias) or 4 (ELECTRICAL CORRECTNESS)?** Measured on the checked-in routed board: **4 `VBUS` vias, all 0.35/0.2, at (28.0,38.6), (26.9,34.7), (32.3,36.4), (32.3,34.9)** — closest pair 1.5 mm, so four singleton clusters, `need = 2` each → **4 rungs.** ELECTRICAL CORRECTNESS is right on this board state. **Caveat that binds the slice:** these artifacts are stale (the routed dir has no `patterns_post` step, so they predate S5) and `power_bottlenecks` on them reads `VBUS kind=track carries=0.414 width=0.0889`, not the recorded `(0.527, …)`. The fresh S7 build's 0.527 is *exactly* `via_amps(0.2)`, which is conclusive that the fresh board's bottleneck is a via. **S4 must re-measure the count, not copy 4.**

**(p) Does buck's `VIN` also fire? (MINIMUM MECHANISM risk 2, power-vias risk)** Measured:

```
buck   VIN  kind=track carries=0.536 width=0.127   under current   -> a TRACK. Does not fire.
buck   5V   kind=track carries=1.999                ok
c3_usb 3V3  kind=via   carries=0.707                ok             -> a via, but passing
c3_usb VBUS kind=track carries=0.536 width=0.127    under floor    -> a TRACK. Does not fire.
node   VBUS                                          under current -> the only one
```
**Technique 1 fires on exactly one net on exactly one board.** ds2 cannot ever trigger it: `per_change = 1` (0.1 A against 0.871 A).

**(q) The biggest single unverified claim in all nine documents — and it checks out.** power-vias' risk #1: "whether node's second `VBUS` via can be placed at all … Check this first." I ran the full candidate ladder (8 octants × radii 0.7…1.0 at the 0.1 grid) against the real board with `blocked` + `antipad_clash`:

```
via (28.0,38.6): 32 tried, 5 clear  {hole_to_hole 13, copper 11, plane_neck 3}  first clear r=0.9 right
via (26.9,34.7): 32 tried, 1 clear  {copper 29, hole_to_hole 1, plane_neck 1}   first clear r=0.9 right
via (32.3,36.4): 32 tried, 2 clear  {copper 26, hole_to_hole 3, plane_neck 1}   first clear r=0.9 down
via (32.3,34.9): 32 tried, 4 clear  {copper 24, hole_to_hole 2, plane_neck 2}   first clear r=0.9 left
```
**All four have a site, all four first clear at exactly 0.9 mm**, exactly as power-vias predicted from arithmetic. The window `[0.85, 1.00]` between "two antipads merge" and "the measurement stops calling it parallel" is 0.15 mm wide and non-empty **only because node is four layers with a 0.35 mm via**. On 2L it closes to a point that `EPS_MM` then shuts — accept and refuse; no 2-layer board triggers.

**(r) `Guard(ground=)` never reaches the router.** Measured: `constraints.py:811` stores only `stitch_mm`. `Constraint` has `guard_stitch_mm` and no `guard_ground`. → Add `guard_ground: str | None`. **This breaks `tests/test_cli.py:123`**, which pins `sorted(doc["constraints"][0])` verbatim — same commit, or CI goes red for a reason unrelated to geometry.

**(s) `REASONS` and `BRANCH_REASONS`.** `route_emit.REASONS` (`route_emit.py:41`) **already** contains `"guard"` and `"stitch"` — no change, and `copper_bar.totals["vias_pattern"]` is already `{reason: n}`. `route_verify.BRANCH_REASONS` is `("fanout",)` and must become `("fanout", "stitch")` — with the **exception** that `carrier == "parallel"` takes `spec.via` (the anchor's size, §2f), which is a passthrough rather than new arithmetic.

**(t) B.6's "the only pattern that may apply partially."** plane-stitch, ELECTRICAL CORRECTNESS and thermal all want to extend it. → Restate as **R-S3**: required ⇒ all-or-nothing; target ⇒ partial with a `Coverage` record. Guard and thermal are targets; parallel is required. B.6's sentence loses the word "only" in the same PR. **"Never degrades" keeps its full force on geometry** — a target pattern may write *fewer* vias, never at the wrong pitch, size, width or net. Coverage is a count; degradation is a geometry change; they are different axes and the contract must say so or an implementer will trade one for the other.

---

## 3. The language — complete, and it is two new statements

```python
# EXISTS, unchanged — src/pcbc/language.py:587
def Guard(net: str, *, stitch_mm: float = 2.5, ground: str = "GND") -> GuardReq

# NEW
def Thermal(pad: str, *, watts: float, rise_c: float = 10.0,
            across_planes: bool = False, fill: bool = False) -> ThermalReq:
    """A via array under one exposed pad (R-T1).

    `pad` is "REF.PADNUM" or "REF.PINNAME", resolved by `route_verify._station_pads` — the same
    resolver `Chain()` uses, so the two cannot disagree about what "U1.49" means.
    `watts` is what this pad must move; it is a BOARD fact, not a library fact, which is why this is
    a board.py statement and not `thermal=` on a Part (an LDO dissipates differently per board, and
    `Part.__call__(**pin_nets)` would silently bind a `thermal=` kwarg as a pin named "thermal").
    `rise_c` is the copper rise budget, defaulting to `CurrentSpec.temp_rise_c`'s own 10.0 so the
    thermal and current models share one number. Not a junction rise; theta_JA is not pcbc's.
    `across_planes` consents to carving antipads in a plane this net does not join (the default
    refusal is finding 10's lesson at nine times the scale).
    `fill` places every site that fits instead of the N the budget needs.
    """

# NEW
def Bridge(a: str, b: str, *, at: str | Sequence[str],
           kind: str = "short", why: str = "") -> BridgeReq:
    """The one point at which two declared grounds are tied, and what ties them.

    pcbc writes NO copper for this. `at` names a part the board already places (a 0R, a ferrite, a
    cap); the netlist keeps both nets, `expected_nets` and `copper_nets` agree, and KiCad sees no
    short — so the gate is untouched. `kind="off_board"` says the tie is made elsewhere and `at`
    then names the two pads that leave the board, with a mandatory `why`.
    """
```

`ThermalReq` / `BridgeReq` in `model.py`; `Design.thermals` / `Design.bridges`; both into `load_board`'s namespace beside `Guard`. `ConstraintSet` gains `thermals: tuple[ThermalSpec, ...]` and `bridges: tuple[BridgeSpec, ...]`; `Constraint` gains `guard_ground: str | None`.

**What each refuses.**

`Thermal` — at load: `watts <= 0`; a `pad` that does not resolve. At compile (`cs.refusals`, before any copper):
- the pad's owner is a **passive** (`fab.passive_refs`) — *"a via inside a passive's pad wicks the joint whatever the fab fills it with (`fab.via_in_pad_blockers` refuses the board). Drop the line."*
- the pad carries no `pad_prop_heatsink` **and** is narrower than `pitch + dia` — *"U1.3 is 0.850 x 0.300 mm and carries no pad_prop_heatsink; did you mean Thermal(\"U1.49\")?"*
- the pad's net has no plane or pour (`route_scene.plane_targets`) — the move names `Board(planes=…)`.
- the stackup does not fill its vias — *"tenting is a mask dam, not a plug"*; the move names a via-fill stackup. (`Stackup` gains `via_fill: str = "none"`, `via_fill_min_drill = 0.15`, `via_fill_max_drill = 0.55` — a fab option with a price, in the same place §3 of router-plan already records the via-in-pad range.)
- `across_planes=False` and the pad sits over a foreign plane — the refusal of §6.2 of the thermal design, with the measured −0.050 mm / +0.1001 mm arithmetic in it.

`Guard` — add the mirror of the refusal it already has for `net`: an undeclared `ground=` net. Keep the no-pour-for-the-ground-net case as a **pattern** refusal, not a compile one, because its move is a `Board(...)` edit that reads better beside the geometry.

`Bridge` — at load: `a == b`; either not a declared net; either not `kind == "ground"` (*"VSS is declared with Net(); a bridge ties two grounds — write Ground(\"VSS\")"*); `kind` not in `("short","cap","bead","off_board")`; `kind == "off_board"` without two `"REF.PAD"` strings and a `why`. In `circuit.check_design`: `at` is not an instance; **`at` does not have exactly two pins** (this is the load-bearing rule — ds2's `U1` binds both grounds and has 16 pins, so an IC is not a tie); its two pads are not one on `a` and one on `b`; a second two-pin part already joins the pair. A **note**, not a refusal, when `at` is a capacitor: *"a capacitor is an AC bridge; {a} and {b} still have no DC reference between them."*

**And the check that needs no declaration at all, which is the one ds2 needs.** In `circuit.check_design`: *more than one net with `kind == "ground"`, and no two-pin instance joining any two of them.* Measured — it fires on ds2 today, on a board that builds:

```
grounds: ['GND', 'VSS']
the only instance binding both: U1 (16 pins) — DGND: GND, AVSS: VSS
two-pin parts touching either: C1,C3,C4,C7,C9,C10,C12 (all VSS-side), C5,C6 (all GND-side)
nothing joins them.
```
Printed as a **move, exit 0** (R1's H.3 promotion procedure and `--strict-power`'s precedent). Promote to a failure only in the PR that shows ds2 green — stopping a shipping board on a defect the author has not been told about teaches `--force`.

**What is deliberately NOT in the language.** No `Stitch()` (still true after S8 — the edge rate went on `NetReq`, where every other net number already lives). No `DEFAULT_RISE_PS` (still true; `NetReq(rise_ps=)` itself ships with S8, with no default anywhere — see section 8 item 3). No declaration for parallel vias (`NetReq(amps=)` already compiles `ViaSpec.per_change`; a second source for one number is exactly what the house rules forbid, and it would let an author silence a current fault without fixing it). No declaration for return vias (`reference` is already compiled). **Two new statements total** — smaller than all three proposals.

---

## 4. Where each technique runs, defended

| # | technique | stage | carrier | net | required | writes copper on |
|---|---|---|---|---|---|---|
| 1 | power via redundancy | `final` | `sites_ring` | the power net | **yes** | **node only** |
| 4 | thermal array | `final` | `sites_lattice` | the pad's net | no | c3_usb, node (declared) |
| 3 | guard + stitch | `final` | `sites_along` | `GuardReq.ground` | no | fixture only |
| 5 | return via | **no stage** | — | — | — | **nothing, ever** |
| 2 | plane / edge stitch | **not built** | — | — | — | — |
| 6 | ground bridge | **no stage** — a declaration and three checks | — | — | — | **nothing** |

Order inside `final`, by C.1's own argument (least free first): **`parallel`, `thermal`, `guard`**. `parallel` has a ring of 8 within 1 mm of one point — near-zero freedom, and it is the only *required* one. `thermal` owns a region nothing else wants. `guard` owns a corridor and writes tracks — the largest footprint in the stage, and the only carrier that reads KRT's copper as *input geometry*.

**Defence against the ds2 evidence, technique by technique.**

- **All of them:** the ds2 mechanism is KRT reacting to locked copper. `krt_plan` has no step after `patterns_final`. The mechanism is not bounded, it is **absent**. The residual surface is the gate's refill alone, and that is the one failure pcbc already predicts per candidate (`antipad_clash`) and measures after (`plane_area`, `plane_islands`, `PLANES` pinned to 0.05 mm²). The chain had no equivalent instrument for the mechanism that killed it.
- **ds2 specifically, measured, and it is the evidence the design turns on:** ds2's Power `per_change` is **1** (0.1 A against 0.871 A) so `parallel` writes nothing; ds2 is TSSOP-16 with no exposed pad so `thermal` writes nothing; ds2 declares no `Guard()`. **The entire `final` stage writes zero pieces on ds2**, and it writes zero for *structural* reasons — no trigger exists — not because a bound was tuned until it passed. That distinction is exactly what `spine.WIDE_MM` got right and the chain got wrong.
- **The two-layer bonus.** At `patterns_post` the 2L pour does not exist and `pour_raster` has to *predict* it. At `patterns_final` the pour is already written by `gnd_pour` (unfilled, but written), so the raster's input is complete — every piece of copper on the board is down. The prediction is exact rather than optimistic. The hostile boards (c3_usb, ds2) get the safest stage. **Every 2-layer stitch site must still go through `raster.reaches` exactly as a tap does (`tap.py:362`)** — a via the fill does not reach is an unconnected item at the gate, which is a build *failure*, not a move. Do not ship without that assertion pinned.
- **The one honest cost:** nothing repairs damage after `final`. If the refill fractures a plane, the build fails with no move except "drop the stitch". Accepted, because the alternative — running earlier so a router can repair it — reintroduces the exact mechanism the design exists to remove. The escape is `PCBC_PATTERNS=off` with a recorded reason (C.6), never a fudge factor in the pattern.

---

## 5. The via budget

**There is no global cap, and proposing one would be the decoration failure in its purest form** — you would drop the via that mattered to stay under a number somebody wrote down. What bounds the total is that every count is a closed form over the compiled constraints and the board's own geometry, so it is computable before a single via exists and printable by `pcbc check --constraints`.

| carrier | n | source | measured on these boards |
|---|---|---|---|
| parallel | `vias_per_change(amps, anchor_drill, plating, dt) − cluster_size`, per under-rated cluster | `stackup.vias_per_change` (already compiles `ViaSpec.per_change`) | node `VBUS`: `vias_per_change(1.0, 0.2) = 2`, 4 singleton clusters → **4 rungs** (re-measure on a fresh build) |
| thermal | `ceil(theta_via / (rise_c / watts))`, capped by `sum over primitives of nx*ny` | `board_mm / (k_cu · π · ((drill/2+plating)² − (drill/2)²))`, k_cu = 385 W/m·K | node **334.2 K/W** per barrel → 12 at 0.35 W; c3_usb **231.1 K/W** → **9** = one per block |
| guard | `ceil(path_mm / guard_stitch_mm) + 2` per surviving stretch | `Constraint.guard_stitch_mm` ← `Guard(stitch_mm=)`, default 2.5 (R-X3) | 0 — no example declares one |

**The pitch, and the floors.** Nothing invented:

| number | value | source |
|---|---|---|
| parallel near limit | `ClearanceTable.via_pitch(net,net,drill,dia,drill,dia)` — **0.700** (4L), 0.800 (2L) | measured; composes `hole_to_hole` + drills vs `between` + rings |
| parallel far limit | `ampacity.VIA_PARALLEL_MM = 1.0` | **the measurement's own constant.** A via further out is copper `_via_clusters` will not count, so the pattern *cannot* place one. No new constant — unlike `tap`, which had to invent `TAP_REACH_MM` |
| the binding one on node | `antipad_clash`: `2 × 0.2 + 0.1 + 0.35` = **0.850 centre-to-centre** | measured `zone_rules` `(0.184, 0.1)` maxed with `between("GND","3V3") = 0.2` |
| thermal pitch | `max(hole_to_hole + drill, between(net,net) + dia, antipad term)` + EPS | node **0.8501**, c3_usb **0.8001** (no foreign pour → no antipad term) |
| guard offset | `next_nm(w/2 + between(g,net) + max(track_min, via_dia)/2 + EPS_MM)` | ds2 **0.550101**, node **0.475101** |
| link width | `Constraint.width_mm` — the class, never narrowed | node `VBUS` 0.4 mm carries 1.231 A, so a rung is never the new neck |

**The thermal pitch derivation does double duty and this is the strongest argument for deriving it.** `PatternCtx` promises the scene is mutated only between patterns, never mid-candidate — so a pattern placing 36 vias in one call cannot judge via 5 against vias 1–4. It does not have to: `p1` and `p2` are *exactly* the two rules one array via could break against another, so within one call the sites clear each other **by construction**. A swept pitch would have to be re-judged against itself.

**What absorbs it, and it needs no new census.** `route_emit.REASONS` already has `"stitch"` and `"guard"`; `copper_bar.totals["vias_pattern"]` is already `{reason: n}` and already **exact** in `VIAS_PATTERN` (`test_examples_fab.py:131`). A stitch appearing on a board where it was not recorded is a test failure on the first run — strictly stronger than any ratio budget, and already wired. I argue **against** a ratio cap on the house rule that a cost function is not a pattern.

**The real ceiling is `PLANES`.** Every stitch via removes about `π(dia/2 + clearance)²` of a foreign pour — 0.4418 mm² per via per crossed plane at node's numbers. `PLANES` is pinned per plane to ±0.05 mm² and computed by `route_verify.plane_area` in the product (`build._plane_gate`). A flood shows up there before it shows up anywhere else. **Deliberate re-pins, with their arithmetic in the PR, never a silent raise:** node's two inner planes lose ≈0.88 mm² each to 4 parallel rungs (S4), and `("3V3","In2.Cu")` ≈5.30 mm² to a 12-via array (S6). `SAME_NET` (a ceiling) will also move when `Thermal` lands, because a lattice at the process floor is what it counts.

**One sentence `ampacity.power_moves` gains** (`ampacity.py:432`): today it offers three edits, none of which is "add a via", because nothing placed one. When `kind == "via"` and the stitch refused, print the stitch's own refusal instead — "the parallel via had nowhere to go, here is what is in its way" is actionable; "move the parts closer together" for a via bottleneck names two parts that are not the problem.

---

## 6. Verification on the finished board

The idiom is `build._plane_gate` / `_chain_gate` (`build.py:21`, `build.py:47`, wired at `build.py:280`/`291`): one parse of a file already on disk, in the route step, with a move for its error.

| # | technique | check | where | how it catches "written but useless" |
|---|---|---|---|---|
| 1 | parallel | **`route_verify.parallel_joined(text, pieces)`** — every `parallel` twin is in the same connected copper component as its anchor **on both spanned layers**, not merely within 1 mm. Reuses `ampacity._adjacency`'s exact-geometry touch test. Plus: re-run `power_bottlenecks` on the finished board and require `kind != "via"` **or** `carries >= amps × (1 − CURVE_EPS)` for every stitched net | new `build._barrel_gate` | this is **the single most important check in the feature**: `_via_clusters` counts proximity with no connectivity test, so an unjoined twin makes pcbc's own measurement lie. A fixture with a deliberately unjoined twin must fail the gate |
| 4 | thermal | every `thermal` via's **ring** (`RING_SAMPLES = 16`) inside one primitive of its own pad **and** inside its net's zone on the far layer; the count exactly what the spec computed; `fab.via_in_pad(text)` gains exactly N hits all with `inside == True` **and `fab.via_in_pad_blockers` is unchanged** | `plane_checks` extended + `build._barrel_gate` | pcbc's own detector must find every via it deliberately put in a pad, and the blocker list must not grow. If the first fails the array is not where it thinks it is; if the second fails, `Thermal()` let a passive through |
| 3 | guard | **`route_verify.guard_connected(text, pieces)`** — every guard segment reaches its net's copper (the `tap._welded` union-find, asked of the guard's pieces plus the zones); every stitch via's ring inside the ground pour; `guarded_mm / run_mm` recorded | new `build._guard_gate` | **an orphan guard is invisible to every existing check**: KiCad's unconnected-items is pad-to-pad, so a floating track on a named net passes DRC, `check_copper` and `verify_copper`. blinky's routed board has **zero** `(zone …)` blocks — a guard there would place 16 vias into a pour that was never written and nothing would say a word |
| 5 | return | **`route_verify.return_vias(text, design, cs)`** — every via on a net carrying `Constraint.reference`, classified `served` / `far` / `none` / `net_change` / `lost`, with the nearest reference-net via and its distance | new `build._return_gate`, **reported, never fatal** | this is R-Z4's **V**, which has never existed. Measured on the checked-in boards: **not one of eleven** signal vias has a reference-net via within 1 mm; the nearest on either board is 1.628 mm; and every verdict is `net_change` (node) or `lost` (c3_usb) — which is the finding |
| 6 | bridge | **`route_verify.bridge_ties(text, design, cs)`** — (i) the two nets' copper reaches, (ii) removing `at`'s pad regions makes it **not** reach (the `chain_order` "through, not past" construction, `_reaches` + `_outside`, **not** a cut-vertex test), (iii) `at` sits at the two domains' closest approach, (iv) no second two-pin part joins them | new `build._bridge_gate`, **fatal for `multi`** | KiCad owns the short check; pcbc owns the *single-point* check, which KiCad cannot make because to KiCad the nets are simply separate. Plus the facing-area / capacitance number: measured on ds2, **21.600 mm² of VSS copper (64.5 %) faces the 1012.099 mm² GND pour across 1.53 mm — 0.575 pF and no DC return path at all** |

**Changes to existing checks, all small and all named:**
- `route_verify.BRANCH_REASONS`: `("fanout",)` → `("fanout", "stitch")`, with `carrier == "parallel"` passing `spec.via` (the anchor's size).
- `route_verify.plane_checks` (`route_verify.py:393`): today `p.reason != "tap"` and "any filled zone of its own net, whichever layer". Generalise to a reason set **and** make it `joins`-aware — a via must be inside a filled zone on **each** of the two layers its spec claims to join. **Keep the tap branch byte-identical** and add a separate branch; do not refactor the containment loop. The generalisation is a strict superset, so the tap's 62 assertions on node are unchanged.
- `build._plane_gate`: **no change at all.** It already reads every via out of `copper.json`, already runs `plane_islands` and `plane_area`, and already fails the build on a plane that split. That is the anti-fracture gate for all of this and it already exists.
- `netcheck.check_copper`: **zero diff.** No new `.kicad_dru` rule, no compiled number changed. The arbiter stays the arbiter.
- `ampacity.power_bottlenecks` must be handed the reasons map (it already exists in `copper.json`) so it skips `guard`/`stitch` items — a 0.127 mm ground guard is not narrow ground rail. No example moves; the fixture is where it bites, which is when it is cheapest to fix.
- `_via_clusters` should take `n × min(via_amps(drill_j))` over the cluster — a one-line change that is correct regardless of this feature and that stops a tap-plus-fanout pair over-reporting **today**.

---

## 7. The build order

### S1 — measure, place nothing (1 day)
**Owns:** `route_verify.return_vias`, `route_verify.via_parallelism`, both report-only, called from `build.py` beside `_plane_gate`. New exact dicts `RETURNS` and `PARALLEL` in `test_examples_fab.py`. The `power_moves` sentence for a via bottleneck.
**Must not touch:** any pattern, `route.py`, `netcheck.py`, `dru.py`.
**Acceptance:** every existing pinned number byte-identical; `RETURNS["node"]` all `net_change`, `RETURNS["c3_usb"]` all `lost`, `RETURNS["blinky"/"buck"/"ds2"] == {}`; `PARALLEL["node"]["VBUS"]` names the under-rated clusters.
**Board:** all five. **Proves:** the populations of techniques 1 and 5 exist and are *counted* before anything is designed around them. Two days of pure risk removal before a millimetre of copper.

### S2 — the empty `final` stage (1 day)
**Owns:** `pcbc_step(name, stage)`, `krt_plan(..., final=True)`, `route_job` reading `--stage` from the cmd into `plans[stage]`, the explicit `plans["post"]` read in the `plane_taps` skip, the per-stage sidecar label, `_STAGES["final"] = ()`, `PatternCtx.stage` docstring, `CONNECTS` on the four existing modules, `PCBC_PATTERNS=off` skipping both pcbc stages.
**Must not touch:** any pattern's geometry, any constant, `test_examples_fab` numbers.
**Acceptance — this is the whole slice:** with `FINAL = ()`, `git diff` on every `layout/*/routed/layout.kicad_pcb` is **empty** on all five boards, with one extra (empty) step file. `PCBC_PATTERNS=off` still reproduces the pre-R2 plan exactly.
**Board:** **ds2 first**, then the other four. **Proves:** the central architectural hypothesis at zero risk. If a no-op step after `finalize` moves any board, the architecture is wrong and nothing else should be built. All three architectures independently made this their first copper-bearing slice; it is unanimous and it is right.

### S3 — `Bridge()` and the two-grounds check (2 days). No copper, no router change.
**Owns:** `language.Bridge`, `model.BridgeReq`, `constraints.BridgeSpec` + report line, the `check_design` finding for two untied `Ground()` nets (printed move, exit 0), `route_checks.check_bridge` (the tie at the closest approach), `route_verify.bridge_ties`, `build._bridge_gate`, `copper_bar.totals["bridge"]`.
**Must not touch:** `netcheck.py` (**zero diff, and that is the point of the slice**), `fab.copper_drc_errors`, any pattern, any clearance number.
**Acceptance:** on ds2 **as it stands, read-only**, pcbc prints the move naming `GND`/`VSS`, `U1` as a 16-pin part that is not a tie, the four-axis overlap, the 0.575 pF and the 0.225 mm closest approach. The four example boards print nothing and are byte-identical. A fixture mirroring ds2's two grounds with a declared `Resistor("R11","0R",p1=GND,p2=VSS)` + `Bridge("GND","VSS",at="R11")` builds to fab with the gate verified.
**Board:** **ds2** (advisory) + fixture (full path). **Proves:** the hardest technique delivering real value on a real board with zero copper and zero risk. Independent of every other slice — could ship first.

### S4 — `patterns/stitch.py`, carrier `parallel` only (3 days). **The thesis slice.**
**Owns:** `StitchSpec`, `Coverage`, `sites_ring`, `run`, the `required` refusal, the rung (via + one link segment per layer at the class width), the cluster-merge refusal, `BRANCH_REASONS`, `route_verify.parallel_joined`, `build._barrel_gate`.
**Must not touch:** `tap.py`, `POST`, any 2-layer path (the window is empty on 2L; refuse and say so with the `Board(layers=4)` move).
**Acceptance, all recorded:** `BOTTLENECK["node"]["VBUS"]` `carries` rises off 0.527 (a floor that must rise — already asserted that way); `VIAS_PATTERN["node"]` gains `{"stitch": n}` exact; `OWNS["node"]` gains `{"stitch": (2n, n)}`; `PLANES["node"]` re-pinned with its arithmetic (≈0.88 mm² per inner plane per 2 vias); **and `BAR`, `DETOURS`, `SOFT`, `LEFTOVER`, `SPINE_*`, `REFUSED`, `TAP_REFUSED` unchanged on all five — which is the `final` stage's claim tested rather than argued.** A **twice-build byte diff** (every `final` pattern reads KRT's output, so this puts more copper behind KRT's determinism than anything before it). A fixture with a deliberately unjoined twin that `parallel_joined` fails.
**Board:** **node**, the only board with a via-limited net. Writes nothing on the other four (measured).
**Honest unknown the slice must settle:** whether node's verdict reaches `ok` or merely flips `via → track` when the next-worst piece binds. Both close the via half of finding 2; only one empties `UNDER["node"]` and breaks the "only blinky has nothing to say" assertion. Measure, don't assume.

### S5 — the return-via classifier and report (1 day). No copper.
**Owns:** `stackup.reference_of`, `constraints.return_rule` / `return_rules` (pure in the `ConstraintSet`, no board file), the compile report line and `Constraint.notes` caveat, `return_vias` promoted from S1's report to an asserted `RETURNS` dict.
**Must not touch:** D.1 item 6 (measured: it needs no change); any pattern; `krt_plan`.
**Acceptance:** all five boards byte-identical; `test_return_rules_are_pure` computes the verdicts from `board.py` alone with no PCB file; node and c3_usb each print two lines naming `Board(planes=[("GND","In1.Cu"),("GND","In2.Cu")])` and `NetReq(layers=["F.Cu"])` respectively.
**Board:** node, c3_usb. **Proves:** R-Z4 is **C, R, V**, not **R, V** — the plan's tag is short by one, and the C half is the valuable one: it tells an AI, before any copper exists, that these two pairs must not change layers. Nothing says that today and both boards ship with the pair on both layers.

### S6 — `Thermal()`, carrier `thermal` (3 days)
**Owns:** `language.Thermal`, `ThermalSpec`, `stackup.via_theta_c_per_w` + `via_fill` fields, `thermal_pitch`, `sites_lattice` over **all nine** primitives of a module land, `route_verify.thermal_budget`, the `FAB_NOTES` paragraph, `pads.PadGeom.prop`.
**Must not touch:** `tap.py` (**zero diff** — §2g), `fab.via_in_pad_blockers`, `POST`.
**Acceptance:** **c3_usb first** — 2 layers, one GND pour, no foreign plane, so `PLANES` cannot move and the slice tests the array alone: pitch 0.8001, 36 sites, **9 placed** (one per block, which is what a hand layout does — the arithmetic and the practice agreeing is the best evidence the model is not nonsense), θ 25.7 K/W, rise 8.99 °C at 0.35 W. Then **node** with `across_planes=True`: pitch 0.8501, 12 placed, `PLANES[("3V3","In2.Cu")]` re-pinned ≈5.30 mm² lower with the webs at +0.1001 mm, `plane_islands` still 1 per plane. `SAME_NET` re-recorded with its reason. The via-in-pad assertion of §6 row 4.
**Board:** **c3_usb** proves it; **node** proves `antipad_clash` earns its keep a second time.

### S7 — `Guard()` runs, carrier `guard` (4 days)
**Owns:** `Constraint.guard_ground` (+ the `test_cli.py:123` pin), `route_scene.net_runs`, `route_scene.clear_runs` (the dual of `free_intervals`, with its epsilon discipline verbatim and its own `geom_vectors.json` entry so R3 ports it), `sites_along`, the two offset tracks at `track_min`, `GUARD_MIN_MM`, the partial-application note, `route_verify.guard_connected`, `build._guard_gate`, the `power_bottlenecks` reasons-map fix.
**Must not touch:** `spacing_w`'s meaning elsewhere, `tap._in_a_pad`, `free_intervals`.
**Acceptance:** `test_clear_runs_agrees_with_clears` sweeping ds2 at 0.01 mm (the way A.8 calibrated `free_intervals`); the fixture board of §7.1 below builds to fab with the gate verified; the partial-application `style:` line pinned **verbatim**; `guard_connected == []`; `GUARD = {}` on all five real boards.
**Board:** **a sixth example, `examples/guard/guard.py`** — it cannot be proven otherwise (§7.1). Then ds2's `AIN*` as a *measurement*, not an acceptance.

### 7.1 What cannot be proven, and the fixture each needs

- **Guard has no board and cannot get one from the five.** Measured: pcbc routes `fanout`, `hop`, `spine`, `tap`; the nets an author guards are `kind="analog"`, which compiles to `layers=('F.Cu',)`, `via.allowed=False` — i.e. *constrained*, routed by KRT's `{class}_nets` step. The intersection of "nets pcbc routes" and "nets worth guarding" is **empty on all five boards**. And the measured coverage on the real routed boards is: blinky 94 % (the board is empty — and it has *no zone*, so all 16 stitch vias would be orphans), ds2 4.5–60 %, buck 10–17 %, **c3_usb 0 %, node 0 %** — because KRT staircases and the guarded net's own copper blocks its own guard (`T_OUT` is nine legs including one of 0.071 mm). **The fixture must be a sixth example, not a unit test**, because a unit test cannot pin a report string, cannot exercise the `final` stage inside `route_job`, and cannot prove a stitch via lands in a pour only `gnd_pour` writes. It must contain: 2 layers / `jlcpcb_2l_1oz` / ~40×25 mm; `Ground("GND")` **and** `NetReq("GND", kind="power")` (or `krt_plan` writes no `gnd_pour` at all and the vias land in nothing — the blinky trap); one guarded net that is `kind="analog"`, ≥10 mm, over `HOP_MM` so it is KRT's; one that *is* a hop, so both provenances are covered; ~2 mm of clear board either side with both ends `locked=True`; one 0402 parked to interrupt ~3 mm of one side, which is what pins the partial-application note verbatim; a third net parallel 1.5 mm away so `clear_runs` has a real obstacle; and one 45° leg so the `ceil(nm(d)·√2/2)` branch is on a built board.
- **Thermal has boards but no declaration.** c3_usb and node both carry `U1.49` — measured: nine 1.45×1.45 mm pads, all numbered 49, all `(property pad_prop_heatsink)`, all on GND. So S6 declares one on a real board rather than inventing a fixture. blinky, buck and ds2 have none (buck's largest SMD pad is `L1.1`, an inductor — a **passive**, and the case that proves the refusal).
- **Plane stitching has neither, and never will from these five.** See §8.

---

## 8. What I would NOT build

**1. Plane / edge stitching (R-E1) — the whole carrier, the `Stitch()` statement, the pitch derivation.**

> **CORRECTED BY S8, 2026-09-21 — built.** The measurement below is right and the conclusion was
> overturned by the user, who ruled that the four example boards are tests and the routing risk is
> acceptable. What S8 then measured says the risk was smaller than this paragraph assumed:
> **every example board's route plan is byte-identical**, because the change (`route_scene
> .plane_targets`: a declared `planes=` is the board's answer on every stackup, the implicit back
> pour is the default) only moves copper where a board declares a pour the old router ignored, and
> none of them does. The `Board()` refusal quoted below is **removed**, and removed rather than
> relaxed: it had stopped being true of this router, and it would now forbid the only way to write
> down the facing pair R-E1 needs.
>
> Three things this paragraph got right and one it got wrong. Right: no board had the population;
> the derivation belongs in `router-plan.md` (it is there); the 5 mm was folklore. Wrong: *"in order
> to serve zero boards"* — a board can now declare the pair, and node's experiment showed the
> technique works on four layers (74 barrels, both planes still one island). What stopped **node**
> was not the stitch but the pour: `3V3` loses its plane, 51.311 mm of a 1 A rail drops to 0.0889 mm,
> and KRT then puts a GND via inside a passive's pad so `fab.via_in_pad_blockers` refuses the board.
> node ships unchanged, and `tests/test_planes.py` records why.
>
> `Stitch()` was **not** built either: the edge rate is `NetReq(rise_ps=)`, one number in the place
> every other net number already lives, with no default anywhere — which keeps item 3 below intact.
Measured: `plane_targets` gives four boards `(('GND','B.Cu'),)` and node `(('GND','In1.Cu'),('3V3','In2.Cu'))`. **No board pours one net on two facing layers.** Worse, the refusal's own move is currently an *error*: `language.Board` raises on `planes=` with `layers <= 2`, deliberately, with the comment *"a declaration the router never reads is a lie the board tells its author"* — and that comment is right. So shipping R-E1 means first landing a routing change (`Board` validation + `plane_targets` + `krt_plan`'s `gnd_pour`) on four boards that ship, which is **larger and riskier than the stitching feature it would enable**, in order to serve zero boards. R-E1's own words are "deferred until a board needs it". Take them. Document the derivation (λ/20 at the knee, `dielectric_between`, the floor from `antipad_clash`) in `router-plan.md` so the sixth board starts from arithmetic rather than from folklore — but ship none of it.

**2. Return-via *placement* (the R half of R-Z4).**
Zero population, and zero for a structural reason that no amount of routing improvement fixes: node changes the reference **net**, c3_usb **loses** the reference. The honest output is the refusal and its move, and the move is real and checkable. Shipping a placer here would mean either widening `RETURN_MM` until something lands, or placing a via on the GND side of node's crossing and calling it half-done — both are copper that connects nothing while the tool's own report blesses it. The `net_change` case belongs to technique 6 and should be handed over **by name** in the refusal text.

**3. `NetReq(rise_ps=)` and `DEFAULT_RISE_PS`.**

> **HALF CORRECTED BY S8, 2026-09-21.** The refusal is of `DEFAULT_RISE_PS` and it stands, word for
> word: there is no per-kind table, no preset value and no fallback anywhere, and the one number with
> a spec behind it is still quoted from memory and still not read off a spec in any session, so it is
> not in the code either. What S8 **did** add is the keyword with no default at all — the author
> states the fastest edge their parts really produce, the way `Thermal(watts=)` states what a pad must
> move, and `ConstraintSet.fastest_edge` folds it over the board. A board that says nothing gets a
> soft refusal naming the keyword (measured: that is all five boards here). "The only consumer is the
> plane stitch, which is not built" is the sentence the user overturned; the rest is unchanged.

ELECTRICAL CORRECTNESS proposes edge-rate defaults for five kinds, of which four (`clock`, `spi`, `i2c`, `switch_node`) are labelled "pcbc default" — i.e. guesses — and the pitch scales linearly with them. That is four invented numbers, entering the constraint compiler, to serve zero boards (the only consumer is the plane stitch, which is not built). Against the house rule that numbers come from the compiler and never from a guess. The one number with a spec behind it (USB 2.0 HS 500 ps) is quoted from memory in two separate designs and has not been read off the spec in any session.

**4. Any copper ground tie, and any gate exemption for one.**
`kind="net_tie"`, a `net_tie_pad_groups` footprint, or a pattern that writes a track between two declared nets. All three die on the same measurement: it would need an exemption in **two** judges — `fab.copper_drc_errors` (discarding a genuine KiCad error, keyed on a net pair, board-wide, which would also excuse the accidental short the check exists to catch) **and** `route_scene._pair_clashes`, whose `same = bool(net) and it.net == net` makes GND copper touching a VSS pad a `Clash("copper", 0.0, 0.200)` that `verify_copper` raises on before the gate ever runs. And on the one board that has the problem, **there is nowhere to put it** (no axis separates ds2's grounds). Say so in the docstring so nobody rediscovers it and reaches for the gate. A 0R **is** a DC tie made of a part; what a component bridge cannot be is zero-area and BOM-free, and no board here needs that.

**5. The moat / `Split(gap_mm=)` / a `RuleArea` between two grounds.**
Measured: no axis separates ds2's two domains — GND's projection is strictly *inside* VSS's on all four axes. A moat is the only part of technique 6 that can break a board, and it has no board to sit on. Recording the four overlaps as a pinned test is worth more than the feature.

**6. A global via cap or a "stitch vias ≤ 20 % of the board" ratio.**
A cost function is not a pattern. `VIAS_PATTERN` is already exact per board per reason and already zero-margin; a ratio is strictly weaker and would make you drop the via that mattered to stay under a number somebody wrote down.

**7. A `tap` skip for a thermal array, and thermal in POST.**
Both create an unconnected-pad hazard neither proposal named (§2g). `tap.py` keeps a zero diff.

**8. Splitting `POST`.** `POST` is *defined* by sitting before `signals`, and that position was chosen by a four-way measurement on node (C.1). Putting stitches in front of `signals` is the ordering that measured 21 unconnected pads. `POST = ("tap",)` is untouched.

---

**What ships, in one line:** three techniques that write copper on real boards (parallel on node, thermal on c3_usb/node, guard on a fixture), two that write none and are worth more for it (return-via classification on node/c3_usb, the ground bridge on ds2), and one that is correctly deferred to the sixth board. Two new statements. One new stage. One new pattern module. `netcheck.py`, `dru.py` and `tap.py` all at zero diff.