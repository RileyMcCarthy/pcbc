# pcbc R2: patterns on an exact-geometry core

Design for phase R2 of `docs/router-plan.md` (section 6.4 stage 2, section 8's R2 line),
written 2026-09-20 on top of R1 as landed (`docs/r1-design.md` A and D). Synthesised from a
judged three-way panel; the numbers below were re-measured against the checked-in boards and
the compiled `ConstraintSet` before they were written down, and two open questions were settled
by running KiCad and running the geometry rather than by argument (A.4 rule 5, B.5's skew
constant).

**The thesis.** R2's product is not the patterns. It is the exact-geometry core they stand on:
one shape primitive, one distance function, one clearance table, one obstacle index, one
self-check. A pattern is a thin deterministic enumeration over that core, and R3's maze router
is another one. So the core is built and proved against KiCad *before* a millimetre of pattern
copper is written, and R3 replays the core's golden vectors instead of re-deriving its
judgement.

**What R2 delivers, in one line:** pcbc writes the hops, chains, plane taps and power spines
itself, exactly, with every piece carrying a reason and every refusal carrying a move; KRT
keeps the long signals and the differential pairs.

**What R2 does not do**, stated once and honestly:

- **Differential pairs stay KRT's.** The plan puts them in R4 and they stay there. R2 builds the
  ribbon geometry (B.5) and the exact skew arithmetic the pair pattern will need, and measures
  nothing about pairs. 14.3 % of the copper is untouched.
- **No layer change on a spine.** A spine link that cannot fit in-plane refuses. R-I2's
  `per_change` vias therefore never get placed in R2, which is why no blind/buried-via question
  and no via-budget arithmetic arises (H.4).
- **No serpentines, no length tuning, no 3W cost, no neck-down rule areas, no teardrops,
  no return vias placed.** R2 *asserts* return vias are absent (D.1 item 6) rather than
  pretending to defer them.
- **No Rust.** Python only; the maze core is R3.
- **`bus` and `guard`/`stitch` ship against fixture boards only**, because no example declares
  `Bus()` or `Guard()`. Their census reads zero on all five real boards, and the spec says so
  rather than hiding it in a total.
- **A refusal is not fatal in R2.** KRT is still behind every pattern, so a refusal is a printed
  move plus a leftover, with the count pinned exactly. It becomes fatal in R3 (C.6).

---

## A. Geometry core

Four new modules, in dependency order: `route_geom.py` (pure geometry, stdlib only — this is
what R3 ports), `pads.py` (a pad's true copper), `route_scene.py` (the placed board as
obstacles), `route_emit.py` (pieces to KiCad text). `patterns/` (section B) sits on all four.

### A.1 One shape primitive

Every obstacle and every candidate piece of copper is

```python
# src/pcbc/route_geom.py
Pt = tuple[float, float]
Box = tuple[float, float, float, float]   # x0, y0, x1, y1

EPS_MM = 1e-4          # 0.1 um: pcbc is always this much stricter than KiCad
MICRO_MM = 0.2         # copper_bar's number, re-exported so there is one of it
NM = 1e-6              # KiCad's unit, and what f"{v:.6f}" writes

def q(v: float) -> float:              # round(v * 1e6) / 1e6
def qp(p: Pt) -> Pt:                   # (q(x), q(y))

@dataclass(frozen=True)
class Shape:
    pts: tuple[Pt, ...]    # convex hull, CCW, 1..16 points, each already nm-quantised
    r: float = 0.0         # offset radius, >= 0
```

read as the Minkowski sum `hull(pts) ⊕ disc(r)`. That one form is exact for everything KiCad
draws in copper, which is why there is no shape zoo:

| KiCad item | `pts` | `r` | exact? |
|---|---|---|---|
| track segment | the two centreline ends | `width/2` | exact (KiCad's `SHAPE_SEGMENT` is a capsule) |
| via ring | the centre (1 point) | `size/2` | exact |
| via hole, round PTH drill | the centre | `drill/2` | exact |
| `(drill oval w h)` | the two end-cap centres in the pad's frame, rotated | `min(w,h)/2` | exact (a capsule) |
| circle pad | the centre | `diameter/2` | exact |
| oval pad | the two end-cap centres, rotated | `min(w,h)/2` | exact |
| rect pad | the four corners, rotated | `0` | exact |
| roundrect pad | the four corners inset by `rad`, rotated | `rad = rratio * min(w,h)` | exact |
| trapezoid pad | the four true corners | `0` | exact |
| chamfered roundrect | as roundrect, chamfer ignored | `rad` | **superset** (a chamfer only removes copper) |
| custom pad primitive | the hull of that primitive's points | `stroke/2` | **superset** (hull of a concave outline) |
| keepout / rule area | the box corners | `0` | exact |
| fanout lane strip | the strip corners | `0` | exact (a policy obstacle, A.7) |
| board edge | a containment box, not an item | — | exact for the rectangular outlines pcbc emits |

The rule that makes the core sound, and the only rule: **a shape is the true copper or a strict
superset of it, never a subset.** Clearing a superset clears the truth. Refusing because of a
superset costs a pattern a fit and is reported as a move, which is a design conversation, not a
DRC error.

**Measured, and the reason `pads.py` exists.** `pcb_place.parse_foot` reads a pad's
`(size w h)`. For `J1.A4B9` on c3_usb and node that is `(size 0.005 0.005)` — a 5 um obstacle.
The pad's real copper is the `gr_poly` primitive, whose points span **0.599974 x 1.299997 mm**,
plus its `(width 0.1)` stroke, i.e. **0.700 x 1.400 mm** (read from
`examples/c3_usb/layout/c3_usb/placed/layout.kicad_pcb` line 327 and the identical blocks on
node). That is four pads per USB-C board, on the two boards whose VBUS, GND and pair copper runs
straight past them, and every `route_checks` check has been blind to them since R1.

### A.2 Distance, with no sqrt in the accept path

```python
def hull_dist2(p: tuple[Pt, ...], q: tuple[Pt, ...]) -> float:
    """Squared distance between two convex hulls; exactly 0.0 when they touch or overlap."""

def clears(a: Shape, b: Shape, need: float) -> bool:
    """hull_dist2(a.pts, b.pts) >= (need + a.r + b.r + EPS_MM) ** 2"""

def gap(a: Shape, b: Shape) -> float:
    """sqrt(hull_dist2(...)) - a.r - b.r, rounded to 4 dp. For report lines ONLY, never a decision."""
```

`hull_dist2` is the textbook convex-polygon distance: if any edge pair crosses, or a vertex of
one lies inside the other, return 0.0; otherwise the minimum over vertex-to-edge squared
distances, both directions. With hulls of at most 16 points that is at most 512 point-segment
tests in the worst case and 8 in the typical one (a capsule against a rect).

Three properties hold it together, and each is a property test in D.1:

1. **Quantisation.** Every coordinate entering a `Shape` passes through `q`, KiCad's own 1 nm
   unit and exactly what `f"{v:.6f}"` writes. The geometry pcbc checks is bit-for-bit the
   geometry KiCad parses; there is no "we rounded on the way out" class of bug.
2. **No transcendentals in the accept path.** Comparisons are squared; division appears only in
   the vertex-to-edge projection. Python floats are IEEE-754 doubles with no fused multiply-add,
   so accept/reject is identical on every platform. `sqrt` and `hypot` appear only in `gap`, in
   messages and in the bar.
3. **A one-sided epsilon.** `EPS_MM = 1e-4` is added to every requirement, never subtracted, so
   pcbc is strictly stricter than KiCad by 100 nm — three orders above the double-precision error
   on 100 mm coordinates (~1e-11 mm) and below KiCad's display resolution.

### A.3 The clearance table

Every distance requirement comes from one function so the router cannot drift from the rules
KiCad is handed.

```python
# src/pcbc/constraints.py  (additive; nothing existing changes)
@dataclass(frozen=True)
class ClearanceTable:
    def between(self, net_a: str, net_b: str) -> tuple[float, str]:   # (mm, why)
    def edge(self) -> float
    def hole_to_copper(self) -> float
    def hole_to_hole(self) -> float
    def via_to_same_net_smd_pad(self) -> float
    def mask_bridge(self) -> float
    def via_pitch(self, net_a: str, net_b: str, drill_a: float, dia_a: float,
                  drill_b: float, dia_b: float) -> float

def clearance_table(cs: ConstraintSet) -> ClearanceTable
```

`between(a, b)` is `max` over, in this order — and the winner's name is the `why` string:

- the two nets' class clearances (`Constraint.clearance_mm`, floored at `stackup.clearance_min`);
- `Constraint.keep_away` in both directions (R-D3, R-A1);
- class creepage where both classes carry one (`VoltageSpec.creepage_mm`); enforcing creepage as
  a straight-line distance is conservative, since the surface path is never shorter;
- `IsolationSpec.clearance_mm` when the two nets sit on opposite sides of a barrier.

`between(a, a)` for a non-empty net is `0.0, "same net"`.

**The compiled numbers, measured, so the table is sized against the truth and not against a
remembered range** (`compile_constraints` on the five boards, 2026-09-20):

| board | classes: clearance | class via (dia / drill) |
|---|---|---|
| blinky (2L) | Default 0.16, Power 0.2 | 0.5/0.3, 0.8/0.4 |
| buck (2L) | Default 0.16, Analog 0.2, Power 0.2, SwitchNode 0.2 | 0.5/0.3, 0.6/0.3, 0.8/0.4, 0.6/0.3 |
| c3_usb (2L) | Default 0.16, **USB 0.155**, Power 0.2 | 0.5/0.3, 0.5/0.3, 0.8/0.4 |
| node (4L) | Default 0.18, USB 0.18, Analog 0.2, Power 0.2 | 0.35/0.2, 0.35/0.2, 0.6/0.3, 0.8/0.4 |
| ds2 (2L) | Default 0.16, Analog 0.2, Power 0.2 | 0.5/0.3, 0.6/0.3, 0.8/0.4 |

Stackups: 2L `track_min` / `clearance_min` 0.127, via 0.5/0.3, annular 0.1; 4L 0.0889 / 0.0889,
via 0.35/0.2, annular 0.075. Both: `hole_clearance` 0.25, `hole_to_hole` 0.5, `edge_clearance`
0.3.

**One-source discipline is kept by a test, not by a refactor of `dru.py`.**
`test_clearance_table_covers_every_written_rule` walks `dru.rules(cs)` for every example plus the
DS2 Addon and, for each `clearance` / `creepage` rule, asserts `between` returns at least that
rule's number for every net pair the rule's condition matches.

**The exception the test must exempt, named.** `pads_of_one_footprint (constraint clearance
(min 0.1mm))` is written *last* in every `.kicad_dru` precisely to **lower** the pad-to-pad
number below the 0.155-0.2 class clearances, and KiCad's later-rule-wins precedence makes it
stick. `ClearanceTable` takes the maximum, so it does not model that rule — it does not need to,
because it is pad-to-pad and pcbc never draws a pad. The exemption is a named constant
`LOWERING_RULES = ("pads_of_one_footprint",)` in the test with this paragraph as its comment, so
the invariant "the table is at least every written rule" is true of the rules it claims to cover
instead of quietly false.

### A.4 The five rules a candidate is judged by

`blocked()` (A.6) applies these in this order, all through `clears`, all from the table. Two
items on the same non-empty net skip rules 1 and 4 but **never** rule 2, 3 or 5.

| # | rule | test | number (2L / 4L) | the measured KiCad error it prevents |
|---|---|---|---|---|
| 1 | copper | `clears(cand.copper, it.copper, between(a,b))` when copper layers intersect | 0.155-0.2 / 0.18-0.2 | `clearance (actual 0.0970 mm)` |
| 2 | hole ↔ copper | `clears(cand.hole, it.copper, hole_to_copper())` and the mirror | 0.25 / 0.25 | `hole_clearance (actual 0.1845 mm)`, a via against `J1`'s NPTH |
| 3 | hole ↔ hole, **same net included** | `clears(cand.hole, it.hole, hole_to_hole())` | 0.5 / 0.5 | `hole_to_hole (actual 0.2595 mm)`; `rule_severities.hole_to_hole` is `"error"` in every example `.kicad_pro` |
| 4 | mask bridge (**advisory in R2**) | `clears(cand.mask, it.mask, mask_bridge())` when mask layers intersect | 0.10 / 0.10 | `solder_mask_bridge` |
| 5 | board edge | candidate copper **and** its hole inside `scene.outline` | 0.3 / 0.3 | `copper_edge_clearance` |

Three of these needed a decision the panel could not agree on. Each is settled here with a number
and a reason.

**Rule 3 is edge-to-edge, not centre-to-centre.** KiCad measures `hole_to_hole` between hole
edges, which is what `stackup.fanout_stagger` already encodes (`need = via_drill + hole_to_hole`).
So two 0.2 mm-drill vias need **0.7 mm** centre to centre on 4L, two 0.3 mm-drill vias need
**0.8 mm** on 2L, and a 0.4 mm-drill Power via against a 0.2 mm one needs **0.8 mm**. Writing it
as `clears(hole, hole, hole_to_hole())` gets all three right for free; writing it as "centres
>= hole_to_hole" is short by one drill diameter and fires on the very first pair of taps.

**Rule 5 is measured from the NOMINAL Edge.Cuts line. KiCad ignores the stroke.** The panel
split on whether the outline's `(stroke (width 0.05))` moves the inner face 0.025 mm inward.
It does not. Experiment, run for this design on 2026-09-20 with KiCad 10.0.6: node's placed board
(`(gr_rect (start 0 0) (end 60 45) (stroke (width 0.05)))`, `min_copper_edge_clearance 0.3`) with
locked 0.2 mm-wide GND segments added at y = 0.35, 0.39, 0.399, 0.401, 0.41, 0.45 and 0.5:

```
error copper_edge_clearance  ... edge clearance 0.3000 mm; actual 0.2500 mm   [y = 0.35]
error copper_edge_clearance  ... edge clearance 0.3000 mm; actual 0.2900 mm   [y = 0.39]
error copper_edge_clearance  ... edge clearance 0.3000 mm; actual 0.2990 mm   [y = 0.399]
(nothing at y = 0.401, 0.41, 0.45, 0.50)
```

`actual = y - width/2`, i.e. the copper's own edge measured to the nominal line at y = 0, with no
stroke term anywhere in it. So `scene.outline` is the
content rect inset by `stack.edge_clearance` exactly, with no stroke term, and
`test_edge_clearance_ignores_outline_stroke` re-runs that experiment (marked `kicad`) so the day
KiCad changes its mind, pcbc finds out.

**Rule 4 is advisory in R2, and here is the procedure that promotes it.** `Stackup` gains one
additive field, `mask_bridge_min: float = 0.0`, set to **0.10** on the JLC 1 oz rungs (JLC's
published minimum solder-mask dam). No compiled number changes and no
`tests/fixtures/compiled/*.json` moves, because nothing reads it but rule 4. A candidate that
fails **only** rule 4 is accepted and emits a `style:` note counted in the census; it is never
refused. The reason: KiCad's own `solder_mask_bridge` check is what gates the build, pcbc has not
measured what 0.10 costs in tap sites, and R1's procedure for exactly this situation
(`r1-design.md` H.3) is to count first and promote in the PR that shows the zeros. If the census
shows `mask: 0` on all five boards, the next PR makes rule 4 a refusal.

**Zones are not obstacles.** KiCad's fill retreats from foreign copper, so a track over a pour is
legal. What a pour needs instead is the island and reachability check (D.5), which is cheap.

### A.5 Paths: 0/45/90 by construction

```python
@dataclass(frozen=True)
class Path:
    pts: tuple[Pt, ...]     # >= 2, nm-quantised
    w: float
    layer: str

def is_octilinear(pts: tuple[Pt, ...]) -> bool:
    """Every leg, on integers of nm: dx == 0 or dy == 0 or |dx| == |dy|. Exact, not 'within half a degree'."""

def octant(a: Pt, b: Pt) -> int:              # 0..7, KiCad's y-down frame

def turn_ok(pts: tuple[Pt, ...]) -> bool:
    """At every interior vertex, d = abs(octant(prev,this) - octant(this,next));
       min(d, 8 - d) <= 1.   <=>  interior angle >= 135 degrees."""

def seg_lengths(pts: tuple[Pt, ...]) -> tuple[float, ...]
def path_mm(pts: tuple[Pt, ...]) -> float
def clip_len_in_box(a: Pt, b: Pt, box: Box) -> float
```

`turn_ok` **must** wrap: a leg going up-right (octant 7) followed by one going east (octant 0) is
a legal 45-degree turn, and `abs(7 - 0) = 7` would reject it. `min(d, 8 - d)` is the whole fix and
`test_turn_ok_wraps` pins all 64 octant pairs.

**Two construction rules, and the first one is a bug fix.**

1. **Never snap a coordinate that must stay equal to another.** A stub out of a pad keeps the pad
   centre's across-coordinate *exactly*; only the along-coordinate moves. `fanout.py` today runs
   the stub from the unsnapped `foot.pad_world(p)` to a via whose across-coordinate went through
   `_snap_near`, so **every** fanout stub is tilted. Measured by re-running `fanout_copper` on the
   five placed boards for this design:

   | board | stubs | tilted | worst |
   |---|---|---|---|
   | blinky | 0 | 0 | — |
   | buck | 0 | 0 | — |
   | c3_usb | 7 | **2** | `GND (20.75, 18.6385) -> (21.8, 18.65)`, 0.6275 deg |
   | node | 2 | **2** | `CC1 (28.75, 37.44) -> (28.8, 36.0)`, 1.9886 deg |
   | ds2 | 10 | **10** | `GPIO1 (20.22, 15.07) -> (20.2, 16.35)`, 0.8952 deg |

   `copper_bar.off_45` has a 0.5-degree tolerance, so all fourteen are counted against pcbc's own
   bar today. Slice S1 fixes it at source: the via's across-coordinate is not snapped, only its
   along-coordinate is. This must land before the self-check of D.1 exists, or fanout has to be
   exempted from the check the whole design rests on.
2. **Quantise once, at construction.** A candidate's points are built in integer nanometres and
   passed through `qp` exactly once. A 45 leg built from a midpoint computes the midpoint in nm,
   rounds it once, then *re-derives* the far endpoint from the rounded corner, so the diagonal
   stays exactly diagonal.

A multi-layer route is `(tuple[Path, ...], tuple[Via, ...])`; a via joins the last point of a path
on layer A to the first point of a path on layer B, at the identical quantised point.

**Pattern copper does not have to sit on KRT's grid**, and must not be snapped onto it, because
snapping is what tilts fanout today. KRT never needs to *land* on pcbc's copper — the pattern's
connection is already complete and locked; KRT only routes around it. `krt_grid(job)` (0.05 on
2L, 0.1 on 4L) is used for exactly two things: the spacing between successive tap candidates, and
rounding a free interval's width down.

### A.6 The scene, the index, and accepting a candidate

```python
# src/pcbc/route_scene.py
@dataclass(frozen=True)
class Item:
    id: int                       # index into Scene.items; the canonical tie-break
    kind: str                     # "pad"|"hole"|"track"|"via"|"keepout"|"lane"|"edge"
    net: str                      # "" = no net (an unbound pad); never invented
    layers: frozenset[str]
    copper: Shape | None
    hole: Shape | None
    mask: Shape | None            # copper outset by mask_margin; None = same as copper
    owner: str                    # "U1.4" | "GND via at (21.75,16.40)" | "J1 mounting hole" | "U1 lane top"
    reason: str                   # pcbc copper only: "fanout"|"hop"|"chain"|"tap"|"spine"|"bus"|"guard"|"stitch"
    locked: bool

@dataclass
class Scene:
    stack: Stackup
    cs: ConstraintSet
    table: ClearanceTable
    items: tuple[Item, ...]
    outline: Box                          # content rect inset by stack.edge_clearance (A.4 rule 5)
    plane_of: dict[str, str]              # net -> plane or pour layer (C.2)
    layers: tuple[str, ...]
    grid: float                           # krt_grid(job)
    feet: dict[str, Foot]                 # for pad exits and lane sides
    def query(self, layer: str, box: Box, pad: float) -> tuple[int, ...]
    def add(self, items: Iterable[Item]) -> None
    def item_of(self, piece: "Piece") -> Item

def build_scene(design: Design, job: CompiledJob, cs: ConstraintSet, pcb_text: str) -> Scene

@dataclass(frozen=True)
class Clash:
    item: Item; have: float; need: float; at: Pt; rule: str   # rule name from A.4

def clashes(scene: Scene, pieces: Sequence["Piece"], net: str,
            *, ignore: frozenset[int] = frozenset()) -> tuple[Clash, ...]

def blocked(scene: Scene, pieces: Sequence["Piece"], net: str,
            *, ignore: frozenset[int] = frozenset()) -> Clash | None:
    """The worst clash by (need - have) descending, then item.id ascending. None when it fits."""
```

**Canonical order of `Scene.items`** — this is what makes byte-identical output arguable rather
than hoped for: sort by `(KIND_ORDER[kind], net, owner, x_nm, y_nm)` with
`KIND_ORDER = {"pad":0, "hole":1, "track":2, "via":3, "keepout":4, "lane":5, "edge":6}`; ids are
assigned after the sort. Copper added mid-run by `Scene.add` is appended with ids above every
existing id, in the order the patterns emitted it. No `set` iteration and no dict-over-floats
iteration anywhere in a decision path.

**The index** is a uniform bucket grid, `CELL_MM = 1.0`, keyed `(layer, floor(x), floor(y))`,
each cell an ascending tuple of ids. An item is inserted into every cell its AABB touches; a
multi-layer item (through pad, via, hole) is inserted on every copper layer it spans.
`query(layer, box, pad)` returns the ascending, deduplicated union of the cells covering `box`
grown by `pad`, where the caller passes `pad = max_need + max_item_r` so nothing near enough to
matter is ever missed. Measured shape of the problem: node is 60 x 45 mm with ~1200 items; a
typical query returns 4 to 20 ids against 152 for a linear scan over pads alone.

### A.7 Pad exits and fanout lanes

> **Amended after S5's review** (`docs/r2-measurements.md` S5r, finding 6): the lane rule below is
> written for a **segment** — "may cross, may not run along", with `lane_budget` as the threshold —
> and `lane_ok` implemented it by skipping every piece that was not one. A via has no run length to
> budget: it **occupies**, so any part of a lane its copper touches is a lane that footprint's pads
> can no longer escape through, and the rule for a via is occupancy. ds2 had one, `C5.2`'s tap via,
> 0.1221 mm inside `U1`'s top lane.

A pattern never starts a track at a pad centre and aims at another pad centre. Measured on the
placed boards for this design, the straight centre-to-centre line for buck's `EN`
(`U1.5 (16.46, 7.66) -> R_EN.2 (17.71, 6.454)`) passes **0.0892 mm** from `U1.4 [FB]` against
the 0.2 mm the Default/Analog pair needs; node's `LED`
(`U1.16 (17.15, 9.45) -> R_LED.1 (19.37, 11.70)`) passes **0.1367 mm** from `U1.15`, which is an
**unbound pad with no net** — a blocker message that invents a net for it is the one kind of move
the AI cannot act on, so `Item.net` for that pad is `""` and the message prints `[no net]`.

```python
@dataclass(frozen=True)
class Exit:
    at: Pt                 # where the stub ends and the link begins
    dir: tuple[int, int]   # (+-1, 0) or (0, +-1)
    stub: Path             # pad centre -> at, on the pad's layer, at the net's width

def pad_exits(scene: Scene, pad: Item, width: float, layer: str) -> tuple[Exit, ...]
```

Distance out: `max(half_extent_along_dir + between(net, net-neighbour-worst) + width/2, MICRO_MM)`,
quantised, where the clearance term is the widest `between(net, other)` over the nets on that
footprint. The `MICRO_MM` floor is what guarantees no pattern stub can ever fire
`track_segment_length (min 0.2mm)`.

Order (deterministic, no scoring):

1. the pad's **fanout escape side** when `Foot.escape` names one — a closed row can only go
   straight out, and that is the side `fanout.py` already spent the lane on;
2. **outward**: the axis direction maximising `(pad_centre - footprint_centre) . dir`
   (`route_checks._side_of_pin`'s question), then the remaining three by the same dot product,
   ties broken `right, left, down, up`.

Every exit is clearance-checked as a piece; a blocked exit is dropped, and when nothing fits the
pattern reports the exit that got furthest (most legs clear).

**Lane strips: one owner, two questions, and the difference is deliberate.**
`route_checks._lane_strips(foot)` moves to `route_scene.lane_strips(foot)` and both callers
import it. The measured widths, because the panel quoted wrong ones: `stackup.fanout_lane(stack,
0.2, 0.5)` is **1.4515 mm** at the 2-layer rung and **1.1288 mm** at the 4-layer rung, and on the
placed boards c3_usb's `J1` top lane is 1.4515, its `U2`/`U3` side lanes 0.827, and node's `J1`
top lane is 1.1288.

- **`route_checks.check_corridors` keeps filling the whole strip** as an obstacle unless the net
  owns an escape in it. It asks "does a channel exist end to end for this net's *trunk*", it only
  asks it for classes 0.4 mm and wider, and being conservative there is a warning before any
  copper exists — the right direction.
- **The router asks about one piece**: may it *cross* a lane (yes) or *run along* it (no)?
  `clip_len_in_box(a, b, strip) <= lane_width + w + 2 * scene.grid`, owner exempt. That is the
  DS2 Addon's lesson exactly (`docs/copper-plan.md` line 191: analog copper ran *along* a lane and
  walled AVDD, DVDD and the UART pins in). Forbidding crossing outright would cut c3_usb's board
  in half along `J1`'s 1.45 mm lane.

`test_lane_rule_is_stricter_in_the_raster` pins the relation: any piece the router would accept as
a *run-along* is also refused by the raster, so the two never disagree in the dangerous direction.

### A.8 Free intervals: the one computed quantity

```python
def free_intervals(axis: str, span: tuple[float, float], window: tuple[float, float],
                   half: float, net: str, scene: Scene, layer: str) -> tuple[tuple[float, float], ...]
```

"For a track of half-width `half` on `layer` running along `axis` across `span`, which values of
the perpendicular coordinate clear everything?" One pass: each obstacle whose extent along `axis`
overlaps `span` grown by `half + need` contributes the blocked interval
`[c0 - half - need, c1 + half + need]` in the perpendicular coordinate; merge with a 1e-9
tolerance, complement inside `window`, drop intervals narrower than `scene.grid`, round each
endpoint to 4 dp. Same-net obstacles contribute nothing. `axis` is `"x"`, `"y"`, `"u"` or `"v"`;
for a diagonal the world is transformed by `u = x + y`, `v = x - y` (no sqrt(2) on the
coordinates, so 4 dp stays exact) and `half` and `need` are multiplied by sqrt(2).

This is a closed-form slab sweep, O(obstacles in the band). It is not a search: it returns the
whole answer at once, and the candidate offsets *are* the intervals, ordered by distance from the
preferred offset. It is what makes a spine trunk deterministic (B.5), and it is what lets a
refusal say a number the AI can act on:

```
the widest free lane across x = 2.45..19.56 is 0.43 mm at y = 13.10
```

### A.9 Connectivity: who still needs KRT

```python
def components(scene: Scene, net: str) -> tuple[frozenset[str], ...]
def net_open(scene: Scene, net: str) -> bool
```

Union-find over the net's pads, tracks, vias and — when `scene.plane_of` names a layer for it —
the plane: two items join when `gap(a, b) <= 0` on a shared layer; a via joins whatever touches
it on either of its layers; a pad joins the plane when it has a via on the plane's layer. A net
is handed to KRT when `net_open` is true. This is *computed*, never declared, so a pattern that
connects three of a net's four pads is honest by construction, and it is the structure R3's maze
core takes as its input directly.

---

## B. The pattern catalogue

### B.0 What every pattern is

```python
# src/pcbc/patterns/__init__.py
@dataclass(frozen=True)
class Piece:
    kind: str                      # "seg" | "via"
    net: str
    reason: str                    # "fanout"|"hop"|"chain"|"tap"|"spine"|"bus"|"guard"|"stitch"
    layer: str | tuple[str, str]   # seg: one layer; via: (from, to) — always ("F.Cu", "B.Cu") in R2
    a: Pt
    b: Pt | None                   # seg: the far end; via: None
    w: float                       # seg width, or via diameter
    drill: float | None            # via only
    key: tuple                     # ("seg", layer, a, b, w) | ("via", at, layers)

@dataclass(frozen=True)
class Refusal:
    pattern: str; net: str; what: str      # "U1.52" | "CC2 J1.B5->R_CC2.1"
    clash: Clash | None
    rule: str                              # a rule name from A.4, or "no candidate"
    hard: bool                             # fatal even in R2 (C.6)
    move: str                              # one line, ends in a board.py edit

@dataclass(frozen=True)
class PatternResult:
    reason: str
    net: str
    pieces: tuple[Piece, ...]              # () when it did not fit
    joins: tuple[tuple[str, str], ...]     # pad ids this connected: ("U1.5", "R_EN.2")
    candidate: str                         # "Zh exit(top,right)" — what fitted, for the report
    refusal: Refusal | None
    notes: tuple[str, ...]                 # "style:" lines (the advisory mask rule, loop areas)

@dataclass(frozen=True)
class PatternCtx:
    scene: Scene                  # mutated only by Scene.add, between patterns, never mid-candidate
    design: Design
    job: CompiledJob
    cs: ConstraintSet
    board: str                    # for stable_uuid
    stage: str                    # "pre" | "post"

def run(ctx: PatternCtx, spec) -> PatternResult
```

Every pattern is pure, and every one of them:

- enumerates a **fixed, finite, ordered** candidate list derived only from the pads' own geometry
  and the class numbers, and takes the first that clears. No cost function, no backtracking, no
  rip-up, no state carried between candidates. The list's bound is declared in the pattern's spec.
- emits **nothing at all** when nothing clears, and returns a `Refusal` naming the worst blocker.
- **never degrades**: it does not narrow a track below the pad neck R-I3 already allows, does not
  drop to another layer unasked, does not add a via a `Constraint` forbids, does not shorten or
  bend a candidate to make it fit.

**The failure strings below are literal in shape and in vocabulary, and literal in their numbers
only where this design measured them.** The `hop` string of B.1 is measured
(`U1.4 [FB]`, 0.089 of 0.200 on buck) and `test_pattern_hop.py` pins it verbatim. The `tap`,
`chain`, `spine` and `bus` strings use real refs and real class numbers but illustrative
coordinates, because the blockers depend on copper that does not exist until the slice before
them lands; each slice's test pins **its own** measured string verbatim, in the same shape, and
the PR records it. A test that pins an invented string pins a coincidence.

**Ordering inside a pattern** is `sorted(ref, pad_num)` for pad-driven patterns and
`sorted(net)` for net-driven ones, so the board is a pure function of the placed board plus the
`ConstraintSet`.

**Every via R2 writes is a through via**, `(layers "F.Cu" "B.Cu")`, exactly as `fanout._via`
hardcodes it. No example `.kicad_pro` sets `allow_blind_buried_vias`, so KiCad's default (false)
applies and a via declared `("F.Cu", "In1.Cu")` — the shape a tap "dropping to the plane layer"
would take — is a `blind/buried via not allowed` error. `Piece.layer` for a via is therefore
typed as a pair but is always that pair in R2, and `verify_copper` asserts it.

**The shared link builder**, used by hop, chain, spine and bus:

```python
def link_candidates(ea: Exit, eb: Exit) -> tuple[tuple[str, tuple[Pt, ...]], ...]
```

in this order, dropping any that is not octilinear, fails `turn_ok`, or repeats an earlier one:

```
  direct   ea ------------- eb        only when dx == 0, dy == 0 or |dx| == |dy|

  Z-far    ea -------\                the 45 on the short axis, taken at the far end
                      \---- eb
  Z-near   ea \                       the same 45 taken at the near end
                \----------- eb
  L-h      ea ----------+             axis, then axis
                        +-- eb
  L-v      ea +
              +------------ eb
  S-h      ea ----+                   axis, cross at the midpoint, axis
                  +--------- eb
  S-v      ea +
              +----+
                   +------- eb
```

The full candidate list for a link is `exits(a) x exits(b) x link_candidates`, at most
4 x 4 x 7 = **112**, walked in that nested order; the first that clears wins.

**Every corner is computed, never snapped.** For `Z`, `m = min(|dx|, |dy|)` is used **unsnapped**
to place the corner: `corner = a + (sign(dx) * m, sign(dy) * m)`, and the far leg is then exactly
axis-aligned because the equality `|dx| == |dy|` on the diagonal leg was never broken. Snapping
`m` to the grid and keeping both endpoints is the one construction that produces neither an axis
nor a 45: on node's `J1 -> U1` (`a = (28.75, 37.44)`, `b = (31.80, 36.00)`, grid 0.1) it gives a
far leg of `dx = 1.65, dy = -0.04`, and on buck's `(2.45, 11.975) -> (19.56, 6.16)` (grid 0.05) a
far leg of `dx = 11.31, dy = -0.01`. `test_link_candidates_are_octilinear` asserts
`is_octilinear` on every candidate of every exit pair of all five boards.

**Via sizing: the rule, because two numbers exist and they mean different things.**

- `Constraint.via.diameter_mm / drill_mm` is the **class** via: sized for the net's current, and
  what `ViaSpec.per_change` (R-I2) was computed from. It disagrees with the stackup on every
  board (node Power 0.8/0.4 and Analog 0.6/0.3 against a 0.35/0.2 stackup; buck/c3_usb/ds2 Power
  0.8/0.4 against 0.5/0.3).
- `stack.via_diameter / via_drill` is the **fab's standard** via: 0.5/0.3 on 2L, 0.35/0.2 on 4L,
  sitting exactly on `min_via_diameter` and `min_via_annular_width`.

**A pattern via that carries the net's current across layers uses `Constraint.via`. A pattern via
that is a branch uses the stackup via.** In R2 the branches are `tap` and `fanout`; everything
else (the `hop`/`chain` layer change, `bus`) uses `Constraint.via`. The argument: a tap connects
one pad to a plane, so it carries that pad's share — a decoupling cap's ripple, not the rail's
1 A — and `via_amps(0.2)` at 10 C rise is 0.53 A, which no single SMD pad on these boards
approaches. The plane, not the via, is the rail's conductor. R-I2's `per_change` applies where the
*trunk* changes layer, and R2 never makes one (the spine refuses instead). This is also what
keeps node routable: 55 SMD GND pads at the Power class's 0.8 mm ring would need 1.0 mm centre to
centre, against 0.7 mm for the stackup via.

`test_via_sizes_follow_the_branch_rule` pins every emitted via's `(diameter, drill)` against the
rule, per board. **This is the decision I am least sure of and it is in H.1.**

**Fanout keeps the stackup via in R2, for a stated reason.** `stackup.fanout_lane` sizes the lane
from `stack.via_diameter`, and `Foot.lane` widens the keep box from that lane, so changing
fanout's via size *moves parts*. That is a placement change wearing a router's clothes and it is
not R2's. Recorded as a follow-up in H.6.

### B.1 `hop` — two pads, one layer, no via

**Routes.** A net with exactly two terminals that share a copper layer, excluding plane nets,
differential pairs and declared bus members, when **either**

- the two centres are within `HOP_MM = 6.0` (`route.LOCAL_MM` is 5.0; 6.0 catches ds2's `nRESET`
  at 3.11 mm and node's `CC2` at 4.13 mm with headroom and still refuses ds2's `ADC_RX` at
  10.14 mm, which is the maze router's job), **or**
- the **`direct` candidate alone** — one straight leg, no corner — clears.

The second clause costs nothing and is not a loophole: a straight pad-to-pad line is never a
detour and never takes a corridor it did not already need, so there is no reason to hand it to a
maze router. It is also the whole of blinky: `LED` runs `R1.2 (9.44, 12.5)` to
`D1.2 (31.3075, 12.5)`, exactly collinear, **21.8675 mm** apart, and it is blinky's only multi-pad
net (GND and VCC have one pad each). Without the clause blinky is 100 % leftover in R2 and the hop
pattern never touches the simplest board in the repo; with it, blinky is one locked 0.16 mm
segment and 0 % leftover. A long two-pad net whose straight line is blocked still goes to KRT.

**Geometry.** Layer: the two pads' shared layers intersected with `Constraint.layers`, in the
constraint's order. Width: `Constraint.width_mm`, necked at the entry stub only to
`max(min(width, pad across), stack.track_min)` — `fanout.py`'s own neck, which necks *to* the pad
and not to twice it.

```
   +--------+ stub                                stub +--------+
   | pad a  |--*--------\                        /--*--| pad b  |
   +--------+  exit      \----------------------/      +--------+
                          45                  45
   |<- half + clearance + w/2, at least 0.2 mm ->|
```

**Candidates**: the 112 of B.0, in that order.

**Order against fanout: hops run FIRST, before fanout.** A hop between two neighbouring pads has
one path, and anything routed before it can cut that path — `route.py` already says so in its own
comment ("the 2 mm hop became a 65 mm detour with a via in the resistor's pad") and
`docs/copper-plan.md` line 181 records the failure. `fanout._excluded` gains the nets the hop
pattern claimed, so a closed row's lane is spent on the hop that needed it rather than on a via
the hop then has to start from.

**Refusal** (soft: KRT routes it, and the sentence prices that):

```
hop EN: R_EN.2 at (17.71,6.454) cannot reach U1.5 at (16.46,7.66) on F.Cu.
  In the way: U1.4 pad [FB] at (16.46,7.26) leaves 0.089 mm of the 0.200 mm the Default/Analog
  pair needs (rule: copper). Tried 4 exits x 7 links = 28 candidates.
  KRT routed it instead, at 1.77x its airwire.
  Moves: Place("R_EN", toward="up") clears U1's own row; or NetReq("EN", layers=["B.Cu"]).
```

`test_pattern_hop.py` pins that string verbatim, including which candidate family the blocker came
from, so the test cannot pin a coincidence.

### B.2 `chain` — pads in a declared feed order

**Routes.** Every `GroupSpec(kind="chain")` (`Chain("VDDA", "J2.1", "C4.1", "U1.12")`, which the
DS2 Addon carries), and an **implicit chain** for any net with three or more pads that no other
pattern owns, ordered by `route_checks._principal_order` — the same order `check_chains` already
measures against, so the placement check and the router cannot disagree about what the order is.

**Geometry.** Consecutive links by B.0's builder, at the chain net's width, in the declared order.

```
  J2.1 --\              C4.1               U1.12
          \----*----+----| |----+----*--------+
                    the cap is IN the path, not a spur
```

**Two rules beyond the link.**

- **No stub (R-X4).** A link's pieces must not pass within `between(net, net) + w/2 + pad_half` of
  a *later* member's pad on the same layer. The chain goes through pads in order, never past one
  and back.
- **The order is the order.** A chain that cannot fit link *i* emits links `0..i-1` and refuses
  with the failing link named. It never re-orders to close the net: the order is the AI's intent.

Chains on nets carrying a `PairSpec` are **skipped with a printed note**, not silently: pairs are
R4's.

**Refusal** (hard when the chain was declared with `Chain()`, soft for an implicit one — a
declared chain is intent KRT structurally cannot honour, because KRT would branch):

```
chain VDDA: link 2 of 3, C4.1 at (12.40,8.05) -> U1.12 at (15.10,9.65), does not fit on F.Cu at 0.25 mm.
  In the way: GND track on F.Cu (11.90,9.20)-(16.30,9.20) [02_analog_nets, locked, U3.4's net]
  leaves 0.112 mm of the 0.200 mm this class needs (rule: copper).
  Moves: Place("C4", toward="right") puts the cap in the path; or Chain("VDDA","J2.1","C4.1","U1.12")
  is asking for an order this placement cannot give - drop C4 from the chain and decouple U1.12 locally.
```

### B.3 `tap` — a pad to its plane or pour

> **Amended after S5's review** (`docs/r2-measurements.md` S5r, finding 10): "one via per pad, never
> clustered" is right and it is not the whole rule. A through via on a foreign net carves an antipad
> out of every **other** plane it crosses, and a row of them at a footprint's own pitch merges those
> antipads into a slot the fill then deletes — node's `U1` column cut 8.74 mm out of the 3V3 plane
> while every check here reported one island and every tap inside its own. A candidate is now refused
> when its antipad would come within a foreign zone's `min_thickness` of another hole's, and the row
> steps out of line instead (`route_scene.antipad_clash`, `zone_rules`).

**Routes.** Every **SMD** pad whose net has a plane or pour (`scene.plane_of`), on an outer layer,
that does not already carry fanout copper. Through-hole pads on a plane net are **skipped and the
report says why**: the barrel already reaches every copper layer, so the zone connects them and a
tap would be a second hole for nothing.

**The census, corrected and measured** (`_pins_of` against the placed board, 2026-09-20). node's
GND and 3V3 carry **77** pads: GND 55 smd + 4 thru_hole, 3V3 17 smd + 1 thru_hole. So the tap
pattern's population on node is **72 SMD pads**, not 64 — every acceptance below is against 72.

**Geometry** — `fanout.py`'s numbers exactly, so the two agree:

```
        +---------+
        | GND pad |-----*   via (F.Cu..B.Cu), stackup size (B.0's branch rule)
        +---------+  stub   the plane on In1.Cu (4L) or the B.Cu pour (2L) picks it up
             d = half_out + stack.clearance_min + stack.via_diameter/2

  the stub keeps the pad centre's OTHER coordinate exactly, so it is exactly axis-aligned
  stub width = max(min(class width, pad across), stack.track_min)
```

On node's 0402 GND pads (`half_out` 0.275): `d = 0.275 + 0.0889 + 0.175 = 0.5389`.

**Candidate order**, `4 x (1 + floor(TAP_REACH_MM / grid))` candidates — **64** on four layers
(grid 0.1) and **124** on two (grid 0.05) — bounded:

1. **direction first**, by descending `(pad_centre - footprint_centre) . dir` so the *outward*
   side is tried first, ties `right, left, down, up`;
2. then `k = 0 .. floor(TAP_REACH_MM / scene.grid)` extra grid steps outward, `TAP_REACH_MM = 1.5`.

Outward-first is not cosmetic: with distance-first ordering the first candidate for a passive's
pad sits between the passive's own two pads (`C_VBUS.2` blocked by `C_VBUS.1 [VBUS]`,
`R_CC1.2` blocked by `R_CC1.1 [CC1]`). The extra steps buy the pads that need to clear a mounting
hole — `J1`'s shell pad needs 1.076 mm.

**Never** inside any pad of a passive (R-M4), never in a lane it does not own, never closer than
`hole_to_hole` edge-to-edge to any hole including the four NPTH mounting holes on the USB-C
boards (which have `num == ""` and no pad copper, and which every caller drops today on
`if not p.num`).

**One via per pad.** Not per component, not clustered. Reasons, in order: it is the electrical
answer a 4-layer board exists for (every SMD ground pad over a ground plane gets its own via);
the alternative's `TAP_JOIN_MM` is a magic constant with no measurement behind it; and the via
*count* objection is answered by splitting the bar (D.4) rather than by holding 55 ground pads off
the ground plane. Clustering is a named non-goal of R2 (H.2).

**Refusal** (soft; the refused pads go to KRT's existing `plane_taps` step, which runs only when
there are refusals):

```
tap GND: U1.52 at (24.10,17.85) has no via site on any of its four sides (64 candidates tried).
  In the way, worst first: USB_DN track on F.Cu (23.80,17.20)-(25.40,18.60) [04_pair_usb_dn, locked,
  J1.A7's net] leaves 0.131 mm of the 0.200 mm this class needs (rule: copper);
  3V3 via at (24.55,17.85) [tap of C_3V3.1] leaves 0.410 mm of the 0.500 mm hole to hole (rule: hole_to_hole).
  Moves: Place("U1", toward="left"); or NetReq("USB_DN","USB_DP", layers=["B.Cu"]) takes the pair
  off this module's south row.
```

### B.4 `spine` — a power net with three or more pads and no plane

**Routes.** Every net with `Constraint.kind == "power"` (or `"switch_node"` with at least three
pads) that the tap pattern did not own, and any net with at least three pads whose class width is
at least `WIDE_MM = 0.4`. 37 % of the copper.

Two forms, tried in that order.

**(a) comb — one straight trunk, perpendicular ribs.**

```
   J_IN.1                     C_IN1.1   C_IN2.1
     *==========================#=========#=======*   trunk, class width (0.781 mm on buck's VIN)
                                #         #
                                * U1.1    * R_EN.1     ribs, perpendicular, necked at the pad
```

1. The trunk **axis** is the one with the larger spread of the pads' centres: `x`, `y`, then the
   two diagonals `u`, `v`, in that fixed order when the spread ties within `scene.grid`.
2. The trunk **span** is `[min, max]` of the stations' projections onto the axis, extended at each
   end by that pad's half size so it reaches.
3. The trunk **offset** comes from `free_intervals(axis, span, window, width/2, net, scene,
   layer)` (A.8), and the candidates are, in order: every interval that contains a pad's own
   perpendicular coordinate (a rib of length zero — the trunk runs through the pad row), nearest
   to the pads' median first; then every remaining interval's centre, nearest to the median first.
   **At most 6.**
4. Each **rib** is a `straight` from the pad centre to its foot of perpendicular on the trunk; a
   rib whose foot is blocked retries once as an `ell` to the nearest free point of the trunk, then
   the comb refuses as a whole.
5. A pad whose class width exceeds its pad's across dimension necks **the rib only** (R-I3). The
   trunk never necks; a trunk that cannot fit at the class width refuses rather than narrowing.

**Why the offset is swept and not guessed.** The obvious trunk — "the straight or L path between
the net's two farthest pads" — is the one form that has been measured *not* to fit: on buck,
`VIN`'s farthest pair is `J_IN.1 (2.45, 11.975)` and `R_EN.1 (19.56, 6.16)`, and the line between
them runs through the `C_IN1` / `U1` / `C_IN2` cluster at x 17-20. The panel's measurement of the
single-trunk form was 3 of 7 candidate power nets. The sweep is the same idea done with the whole
answer in hand.

**(b) backbone — stations linked pairwise.** When the comb refuses: order the stations with
`route_checks._principal_order` and link consecutive pairs with B.0's builder at the trunk width.
A middle station is a T-junction: the link ends on the pad, the next link starts there, and
KiCad's connectivity joins tracks that touch. A pad whose link fails splits the spine into two
components and **both are kept** — partial copper is legal, useful and honest, and `net_open`
hands what remains to KRT.

**No layer change.** R2 spines are single-layer, by decision (see "What R2 does not do"). A power
net whose pads are not all on one layer gets the links it can and refuses the rest.

**Refusal** (soft), and this is where `free_intervals` earns its place:

```
spine VIN: the trunk along x across 2.45..19.56 does not fit on F.Cu at 0.781 mm.
  The widest free lane there is 0.430 mm at y=13.100; the trunk needs 1.181 mm (0.781 + 2 x 0.200).
  In the way at the median: J_IN.2 pad [GND] at (4.95,11.97); C_IN1.2 pad [GND] at (15.51,11.68).
  Moves: Place("J_IN", rotate=180) puts VIN on the outside of the row; Place("C_IN1", toward="up")
  opens the lane; or NetReq("VIN", amps=1) narrows the trunk to 0.400 mm.
```

### B.5 `bus` — a ribbon

**Routes.** Every `GroupSpec(kind="bus")` with at least two members whose pads form two ordered
groups (a row on an IC to a row on a connector). **No example board declares `Bus()`**, so this
pattern's acceptance is a fixture board and its census reads `bus: 0 pieces` on all five real
boards.

**Geometry.** One shared path shape — the *centre member's* — with each other member offset
perpendicular by `k * pitch`.

```
  U1                                              U2
  |-*=============\                        /=========*-|  D0
  |-*==============\======================/==========*-|  D1
  |-*===============\====================/===========*-|  D2
                     \                  /
                 pitch p between neighbours, everywhere
```

`p = ceil_grid(w + max(between(m, m'), spacing_w * w))` for a ribbon with no corner, and
`p = ceil_grid(sqrt(2) * (w + need))` for one that turns, because the perpendicular gap between
two diagonal legs offset by `p` along an axis is `p / sqrt(2)`. `Constraint.spacing_w` is 3 or 5
and already compiled.

**Corners stay exactly 45 degrees.** A member's corner is displaced **along the direction of
travel**, never sideways: the inner member turns `d * (sqrt(2) - 1)` early and the outer that much
late, where `d` is that member's offset. Verified numerically for this design at
`d = s/2 = 0.1894`: the displacement is **0.0784520487 mm** and the two mitred members hold a
perpendicular separation of exactly 0.3788 mm. Rounding of the displacement is **outward**, so a
gap can only grow.

**Skew is exact, and the constant is 2 tan(22.5), not tan(22.5).** A 45-degree corner makes the
inner member shorter than the outer by

```
  skew_per_corner = 2 * s * tan(22.5 deg) = 0.828427 * s
```

Verified numerically by offsetting the polyline `(0,0) -> (10,0) -> (15,5)` by `+-s/2` with
mitred corners at `s = 0.3788`: outer 16.914164 mm, inner 17.227972 mm, difference
**0.31380819 mm**, and `2 * 0.3788 * tan(22.5) = 0.31380819`. The half-value `0.414 * s` that one
panel design used is the *displacement* constant, not the skew, and accepting a two-corner ribbon
on it would pass 0.6276 mm of real skew against a 0.5 mm budget while reporting 0.3136.

**Candidate order**: the link families of B.0 applied to the centre member, then every other
member as the same polyline offset by `k * pitch`. The ribbon is accepted **only if every member
clears** — it fits as a whole or it fails as a whole; no member is ever routed alone. Member order
is `GroupSpec.members`, offsets assigned by the pads' order along the source row so the ribbon
does not cross itself. A destination order that is neither the source order nor its exact reverse
is a crossing: refuse, naming the two members and the pin swap.

**Skew gate**: `abs(sum(turns)) * 0.828427 * pitch_span + abs(approach_a - approach_b) <=
GroupSpec.match_mm`, with the approach stubs measured exactly, not estimated.

**Refusal** (soft):

```
bus D0..D3: the 4-member ribbon at pitch 0.386 mm does not fit on F.Cu; member D2 is blocked.
  In the way: CLK track on F.Cu (20.10,14.00)-(26.40,14.00) [06_signals, U2.7's net] leaves
  0.121 mm of the 0.160 mm this class needs (rule: copper).
  Moves: Place("U2", toward="down") by 0.4 mm gives the ribbon its pitch; or Bus("D0","D1","D2","D3",
  match_mm=1.0) without CLK in the way - route CLK on B.Cu with NetReq("CLK", layers=["B.Cu"]).
```

### B.6 `guard` and `stitch`

**Routes.** `Constraint.guard_stitch_mm` (from `Guard("AIN0", stitch_mm=2.5)`) on a net **pcbc
itself routed**, so its path is known. No example declares `Guard()`; fixture-only in R2.

**Geometry.** Two ground tracks parallel to the guarded path, mitred at the corners, at offset
`d = w/2 + max(between(guard_net, net), spacing_w * w)`, at `stack.track_min`, plus stitch vias
every `stitch_mm` along each side starting from the guarded path's first vertex and one via at
each end.

```
    ==============================   guard (GND), track_min
     *        *        *        *    stitch vias every 2.5 mm
    ------------------------------   AIN0
     *        *        *        *
    ==============================
```

**A guard on a net KRT routed is not attempted.** pcbc would be guarding copper it does not own
and cannot re-check when KRT moves it. It reports, and this is the whole message:

```
guard AIN0: deferred - the net is routed by KRT, not by a pattern, so pcbc cannot offset a path
  it does not own. R3 owns this.
```

**A guard may apply partially, and it is the only pattern that may.** A guard is a shield, not a
connection, so a blocked stretch is dropped rather than failing the pattern, and the note says how
much of the run is guarded:

```
style: AIN0 guarded 18.2 of 24.0 mm; C4 blocks 5.8 mm on the north side (8 stitch vias placed, 2 dropped)
```

**Return vias are not placed in R2.** `verify_copper` asserts pcbc placed no via at all on a net
carrying `Constraint.reference` (D.1 item 6), which is the honest way to defer R-Z4 to R4.

---

## C. The plan

### C.1 Where patterns run

One pattern stage, between fanout and KRT, plus one tap stage inside the KRT sequence. The order
inside the pre stage is by degrees of freedom, least free first — the same argument `krt_plan`
already makes for giving KRT's constrained step the empty board.

```
placed.kicad_pcb
   0  build_scene                      pads, holes, keepouts, lanes, outline, clearance table
   1  hops         pcbc                two-pad nets: within 6 mm, or any straight one       (locked)
   2  fanout       pcbc  (exists)      closed rows, minus the nets the hops claimed        (locked)
   3  constrained  pcbc  hop/chain     vias=False or one layer: SW, FB, T_OUT, T_DIV, ds2's analog
   4  chain        pcbc                declared Chain groups, then implicit 3+ pad nets    (locked)
   5  spine        pcbc                power nets, >= 3 pads, no plane                     (locked)
   6  bus          pcbc                declared Bus groups           (fixture boards only) (locked)
   ->  00_patterns_pre.kicad_pcb
KRT, from krt_plan on that board:
   local_hops      only the hops the pattern refused (usually the step disappears entirely)
   *_nets          constrained nets the pattern refused
   pair_*          differential pairs                          (unchanged; R4 owns pairs)
   planes          the 4-layer pours                           (unchanged)
   -> back to pcbc:
   7  tap          pcbc                every SMD plane pad; the signals are not down yet     (locked)
   8  guard/stitch pcbc                on nets 1..7 routed        (fixture boards only)      (locked)
   ->  NN_patterns_post.kicad_pcb
KRT again:
   plane_taps      only the pads the tap pattern refused (skipped when there are none)
   signals         the leftover: the long signals
   gnd_pour        2-layer pour                                (unchanged)
   finalize        what the pour missed                        (unchanged)
   -> pin_copper_ids(keep=...) -> copper bar -> the gate
```

Each pattern's copper enters the scene before the next pattern runs, so a later pattern sees it as
an obstacle and the whole stage is a single left-to-right pass with no revisiting.

**The post stage always sits immediately before KRT's `signals` step**, in both stackups. On four
layers that is after `planes`, so the plane the tap lands in already exists. On two layers there is
no `planes` step — the pour is `gnd_pour`, near the end — so the taps are placed *before* the pour
exists, and D.5's reachability raster is what makes that safe: it predicts the pour's flooded
region from the copper already down and refuses a tap the fill will not reach, rather than letting
the gate discover it as an unconnected item. c3_usb and ds2 are the two boards this applies to
(both two-layer, both with `GND` among their power nets, so `plane_targets` gives them
`('GND', 'B.Cu')`).

**Why taps sit between `planes` and `signals`, with the measurement.** Four orderings were tried
by the panel on node, and this one is the only one that is both connected and better:

| order | segments | vias | off45 | micro | mm | worst detour | note |
|---|---|---|---|---|---|---|---|
| baseline, no patterns | 420 | 30 | 63 | 161 | 501.5 | 1.72 | |
| everything before KRT | 307 | 74 | 33 | 65 | 414.0 | **1.83** | the taps box the pair in |
| taps after `signals` | 247 | 66 | 28 | 48 | 374.1 | 1.77 | **21 unconnected**: the signals took every tap site |
| taps after `signals` + KRT tie-up | 371 | 73 | 47 | 95 | 429.0 | 1.77 | the tie-up costs 100 segments for 7 pads |
| **taps between `planes` and `signals`** | **354** | 75 | **32** | **90** | **417.5** | **1.72** | chosen |

Running the spines *before* the hops was also considered and rejected: `docs/copper-plan.md` line
181 and `route.py`'s own comment both record that long nets routed before short hops wall them in,
and a spine is the widest locked copper on the board.

### C.2 The plane target is one fact, computed once

`krt_plan` decides on the fly that a 2-layer board with `GND` among its power nets gets a B.Cu
pour. The tap pattern needs the same fact. Factor it:

```python
# src/pcbc/route.py
def plane_targets(job: CompiledJob) -> tuple[tuple[str, str], ...]:
    """(net, layer) for every net that will have a plane or a pour: job.planes on 4 layers,
    ('GND', 'B.Cu') on 2 layers when GND is a power net - the rule krt_plan already applies."""
```

used by `krt_plan` (behaviour unchanged) and by `Scene.plane_of`.

### C.3 What stays locked, and the two guards on it

`route_emit.segment` / `route_emit.via` are lifted **verbatim** from `fanout.py` (which then
imports them) and write `(locked yes)` in the two positions KRT's parser reads: after `(width)` on
a segment, after `(layers)` on a via. That `(locked yes)` holds was confirmed end to end by the
panel rather than read off KRT's README: across node's seven step files the locked count is
constant (2 segments + 2 vias from `00_fanout` through `06_signals` and the final board; 14
segments from `02_analog_nets` onward), and the same on buck (17 segments from
`03_switchnode_nets`). KRT never moves or deletes one.

Two guards on top of that promise, both cheap, because R2 writes ten times more locked copper than
fanout does and puts it in KRT's way:

- after every KRT step, `route_job` asserts that the set of pattern geometry keys present in the
  step's output equals the set it wrote, and turns a violation into an error naming the step
  (`test_patterns_survive_krt`);
- `pin_copper_ids(text, board, keep: frozenset[str] = frozenset())` gains `keep`: uuids in `keep`
  are not re-keyed by sheet order, so pcbc's own deterministic uuids survive and the sidecar
  cannot silently point at ids that no longer exist. KRT's invented ids are still re-keyed exactly
  as today.

### C.4 The leftover, handed over

```python
@dataclass(frozen=True)
class PatternPlan:
    text: str                                # the board with the pattern copper in it
    pieces: tuple[Piece, ...]
    census: dict[str, dict]                  # reason -> {"pieces", "segments", "vias", "mm", "nets"}
    moves: tuple[str, ...]
    notes: tuple[str, ...]
    done: frozenset[str]                     # nets fully connected by patterns (net_open is False)
    partial: frozenset[str]                  # nets with some pattern copper and still open
    refused: dict[str, tuple[Refusal, ...]]  # net -> its refusals
    wall_ms: int
```

`krt_plan(job, design, placed, work, home, plan: PatternPlan | None = None)` uses it:

- `local_hops` is built from `plan.refused` restricted to local nets, and the step is **skipped
  when empty**;
- each `{class}_nets` step drops `plan.done`;
- `signals` gains `!NET` for every member of `plan.done` (belt and braces: KRT would skip them
  anyway);
- `plane_taps` runs only for plane nets that the tap pattern refused a pad of;
- everything else is untouched, so a board where patterns fit nothing routes exactly as today.

### C.5 The one change KRT needs, scoped so it cannot raise a floor

`write_fab_overrides` writes `clearance = stack.clearance_min` today (0.127 on 2L). With pattern
copper on the board KRT chooses routes it would not have chosen, and the panel measured it
producing a violation of its own on ds2: `clearance (netclass 'Power' 0.2000 mm; actual
0.1284 mm)` between a GPIO1 track and a 3V3 via, both KRT's.

The fix must not do what one panel design proposed — writing `max(class clearance)`, which on node
is 0.2 and would raise the floor **above** the USB class's own 0.18 and above the
`usb_pair_gap (opt 0.15mm)` the pair is dimensioned for. `write_fab_overrides` is documented as
"the fab's floor ... KRT must not escalate past it"; raising a floor past a class's own number is
a different edit.

So: `clearance = max(min(c.clearance_mm for c in job.classes), stack.clearance_min)`. By
construction that is at most every class's own clearance — 0.155 on c3_usb, 0.16 on buck and ds2,
0.18 on node — so no class is ever escalated, and it is above the 0.1284 that broke ds2. It lands
in the slice that needs it (S4), with a **patterns-off** before/after on all five boards recorded
in the same PR; if it moves any board with patterns off, it lands as its own slice with the
change explained.

This is a patch on a router that is not legal by construction, and it is the last one. Any further
KRT self-violation is reported through the gate with blocking analysis as evidence for R3, and the
answer is to move that pattern's share into pcbc — not a third workaround.

### C.6 The refusal contract, and the escape hatch

- **Soft refusal** (the default, and almost all of them): the move is printed, the net falls
  through to KRT, the build passes. The **count** is pinned exactly per board in
  `test_examples_fab.py`, the way `SOFT` already is, so a new refusal is a test failure and cannot
  drift into being ignored.
- **Hard refusal** (fatal in both modes): only where honouring the refusal would break a
  constraint KRT structurally cannot hold — a net with `vias=False`, a single-layer net, or a
  declared `Chain()`. Those are the cases where falling through to KRT produces copper that
  violates the intent rather than merely a worse route.
- `--strict-patterns` / `PCBC_STRICT_PATTERNS=1` makes every refusal fatal. R3 flips the default.
- `PCBC_PATTERNS=off` restores today's plan **exactly** — one env check around one call. It is the
  difference between a bad pattern being a rollback and being a revert.

---

## D. Verification

### D.1 The exact-geometry self-check (pcbc's own, not KiCad's)

```python
# src/pcbc/route_verify.py
def verify_copper(scene: Scene, pieces: Sequence[Piece], cs: ConstraintSet) -> list[str]
```

Runs unconditionally at the end of each pattern stage; a non-empty result **raises** rather than
writes. It is not a nicety — it is the reason the gate can be expected to find nothing.

1. every piece against every other item in the scene with the same `clears` and the same table,
   as one pass, so an ordering bug in the incremental checks cannot hide;
2. `is_octilinear` and `turn_ok` on every path;
3. every segment at least `MICRO_MM`;
4. every coordinate an exact multiple of 1 nm, and the emitted text re-parsed to the same numbers
   (emit -> parse -> emit is a fixed point);
5. every via's `(diameter, drill)` equal to what B.0's branch rule says for its reason and net —
   **not** "equal to the stackup's", which would pin the wrong number for a class via;
6. **no via at all** on a net whose `ViaSpec.allowed` is False, and **no via at all** on a net
   carrying `Constraint.reference` (R2 never changes layers on a controlled-impedance net; this is
   the cheap half of R-Z4 and asserting zero is the honest way to defer the rest to R4);
7. no piece inside a rule area that disallows its kind;
8. every via's `layers` exactly `("F.Cu", "B.Cu")` (B.0: KiCad's default forbids blind and buried
   vias and no example turns it on);
9. the lane rule of A.7 satisfied by every piece, owner exempt;
10. every piece's tightest margin reported, for the bar.

### D.2 The agreement test — the real proof, and it runs before any pattern exists

Run the checker over the four examples' *existing* KRT-routed boards, which `kicad-cli pcb drc`
already passes, at the class clearances `dru.py` wrote. Two directions, and they are not
symmetric:

- pcbc flags something KiCad passes -> pcbc's model is too strict (a superset that costs fits).
  Allowed; every instance is recorded in `docs/r2-measurements.md` with its shape.
- KiCad flags a clearance, hole or edge error pcbc passed -> **pcbc's model is unsound**, and the
  slice does not land.

This calibrates the geometry core against the arbiter on real copper before one millimetre of
pattern copper is written, and it makes the plan's section 9 rule ("any error is a router bug")
testable. It is S3's gate.

### D.3 Property tests

`tests/test_route_geom.py`, seeded `random.Random(20260920)` so CI is deterministic:

- **no false accept**: 2000 random shape pairs; `clears(a, b, need)` implies a dense boundary
  sampling of both shapes finds no pair of points closer than `need`;
- **superset**: for each pad kind present on the five boards plus a generated corpus (rect,
  roundrect at rratio 0 / 0.243902 / 0.25 / 0.5, circle, oval, trapezoid, chamfered, custom), 500
  points sampled inside KiCad's true shape (computed by an independent naive rasteriser straight
  from the s-expression) all lie inside the `Shape`;
- **determinism**: shuffling the input order of footprints, pads and nets gives byte-identical
  emitted text; running twice gives byte-identical text;
- **golden vectors**: `tests/fixtures/geom_vectors.json`, 2000 shape pairs with their exact
  distances, which R3's Rust port replays unchanged. This is the cheapest thing in the design and
  it is what makes R3 a port instead of a rewrite.
- **pattern soundness**: 200 random placements (2 to 12 footprints drawn from the five boards'
  footprints, random positions and rotations from `{0, 90, 180, 270}`), run every pattern, assert
  `verify_copper` is empty; 10 of those also written to a board and run through
  `kicad-cli pcb drc` under the `kicad` marker, asserting zero clearance / edge / hole errors
  attributable to pcbc copper.

### D.4 The reason census and what the copper bar gains

KiCad rewrites the board during the gate's refill-and-save, and `pin_copper_ids` re-keys uuids, so
a reason cannot live in the file. Truth lives in `layout/<board>/routed/copper.json`, written by
`route_emit`, **keyed by geometry**, not by uuid:

```json
{"items": [{"key": ["via", [24.55, 17.85], ["F.Cu", "B.Cu"]], "uuid": "...", "reason": "tap",
            "net": "GND", "owner": "C_MCU.2", "step": "patterns_post", "mm": 0.0}],
 "refusals": [{"pattern": "tap", "net": "GND", "what": "U1.52", "rule": "copper",
               "blocker": "USB_DN track on F.Cu", "hard": false, "move": "..."}],
 "notes": ["style: ..."],
 "census": {"fanout": {"segments": 9, "vias": 9, "mm": 13.5},
            "hop":    {"segments": 12, "vias": 0, "mm": 23.7},
            "tap":    {"segments": 72, "vias": 72, "mm": 48.0},
            "leftover": {"segments": 277, "vias": 15, "mm": 359.6}}}
```

The key `("seg", layer, a, b, w)` / `("via", at, layers)` is the one `blocking._keys` already uses,
and it is stable through both rewrites because the geometry is locked. The uuid is **derived from
the same key** (`stable_uuid(board, "pcbc", reason, net, key)`) and preserved by
`pin_copper_ids(keep=)`, so a piece is traceable in KiCad's UI; `test_sidecar_keys_match_the_board`
asserts the two agree, so a stale sidecar is a test failure rather than a census that quietly lies.

`copper_bar(text, reasons=None)` gains `totals["by_reason"]` and three keys; nothing is renamed and
`test_copper_bar.py`'s existing assertions stand:

```python
totals["vias_pattern"]    # {reason: n} — vias pcbc placed
totals["vias_leftover"]   # vias the leftover router placed
totals["refusals"]        # {pattern: n}
```

and the report lines become

```
copper: 355 segments, 89 vias, 417.5 mm; 32 off 0/45/90, 90 under 0.2 mm
copper: pcbc owns 78 segments / 57.9 mm and 74 vias (fanout 2, hop 4 seg, tap 72); leftover 277 segments / 359.6 mm / 15 vias
copper: 4 pads could not be tapped, 1 hop refused (see moves)
```

**`test_examples_fab.py::BAR` splits, and this is a deliberate change to a pinned number.** `vias`
becomes `vias_leftover` and stays a ceiling; `taps` and `refusals` become **exact** counts, like
`SOFT`. The honest reason: the existing `vias` ceiling was recorded to catch KRT's staircase vias,
and a tap via is the point of a 4-layer board — holding node to 30 total vias means holding 55
ground pads off the ground plane. Every other ceiling stays a ceiling and every one of them must
improve or hold. **ds2's ceilings are recorded for the first time in the same PR**: `BAR` has
exactly three entries today (buck, c3_usb, node) and `grep -rn ds2 tests/` finds ds2 only in
`test_dru.py`, so "no bar number worse anywhere" has nothing to compare ds2 against until S1
records it.

### D.5 Plane, island and return checks (cheap, so in)

> **Amended after S5's review** (`docs/r2-measurements.md` S5r, findings 9, 11, 12 and 17). Three
> sentences below do not survive their own arithmetic. **The island count cannot see what it is for**:
> under `island_removal_mode 0` a cut-off fragment is *deleted*, so it is never written as a second
> `filled_polygon` and the count stays at 1 — the number that moves is the filled **area**
> (`route_verify.plane_area`), and ds2 drops five orphans, 8.52 mm2, at a count of one. **The centre
> is not the via**: containment is asked of the ring, 16 points of it, and `copper.json` carries the
> size so a caller no longer has to invent one. **Absence is an answer**: a net whose zone came back
> unfilled reports a line rather than being skipped. And all of it runs in `pcbc build`, not only in
> the suite — until the review neither check was called from `src/` at all.

- **Island count and via containment, on every plane layer a via SPANS.** Every pattern via is a
  through via (`F.Cu`..`B.Cu`), so on node a GND tap punches a clearance hole in the **3V3** plane
  on In2.Cu as well as landing in the GND plane on In1.Cu. With `island_removal_mode 0` a fragment
  that separates is deleted, and a 3V3 pad whose only connection was that fragment becomes an
  unconnected item the gate fails on. So the check runs **once per plane layer the via crosses**,
  not once for the tapped net: after the gate refills, count `filled_polygon` blocks per zone, and
  assert every tap via's centre lies inside its own net's zone polygon and that no foreign plane's
  island count rose. Measured today: node's In1 (GND) and In2 (3V3) are each a single island
  before and after, and the panel measured them still single after 60 taps (point counts
  1173 -> 1448 and 1358 -> 2900).
- **Pour reachability** (2-layer boards, where the pour comes after the signals): rasterise at
  `scene.grid` with every foreign copper item and pad dilated by the pour's clearance, flood from
  the pour's largest free region, and refuse a tap whose via is not in the flooded label. This is
  what turns "the fill retreated and isolated a via" — seen once already (plan section 9) — into a
  move before the board is written. node at 0.1 mm is 600 x 450 cells; `check_corridors` already
  runs that size inside its 3 s bar.
- **Loop area** for a tapped decap: `route_checks._loop_area` exists; the tap stage reports the
  cap's loop with its tap via in place as a `style:` note (a tap at the cap can only shrink it).
- **Plane split (R-Z3)** and **3W (R-X1)**: still named, not measured. R4.

### D.6 What the gate keeps

`netcheck.check_copper` is untouched: KiCad DRC clean, zero unconnected items, the copper netlist
equal to what `board.py` says, the canary fired, the soft counts recorded. R2 adds no rule to
`dru.py` and changes no compiled number. **The gate is still the arbiter**; pcbc's self-check is a
stricter pre-filter, never a replacement.

---

## E. Data model and file plan

### E.1 New modules

| module | public surface | depends on |
|---|---|---|
| `src/pcbc/route_geom.py` | `Pt`, `Box`, `EPS_MM`, `MICRO_MM`, `q`, `qp`, `Shape`, `track_shape`, `via_shape`, `hole_shape`, `rect_shape`, `roundrect_shape`, `oval_shape`, `circle_shape`, `poly_shape`, `hull`, `hull_dist2`, `clears`, `gap`, `Path`, `is_octilinear`, `octant`, `turn_ok`, `seg_lengths`, `path_mm`, `clip_len_in_box`, `aabb`, `box_grow`, `free_intervals` | stdlib only — **this is what R3 ports** |
| `src/pcbc/pads.py` | `PadGeom`, `pad_geoms(block, at, side)` | `route_geom`, `sexp` |
| `src/pcbc/route_scene.py` | `Item`, `Scene`, `Clash`, `build_scene`, `lane_strips`, `copper_items`, `clashes`, `blocked`, `components`, `net_open` | `route_geom`, `pads`, `constraints`, `stackup`, `pcb_place`, `layout`, `sexp` |
| `src/pcbc/route_emit.py` | `segment`, `via`, `write_pieces`, `piece_key`, `census`, `write_sidecar`, `read_sidecar` | `sexp` only |
| `src/pcbc/route_verify.py` | `verify_copper`, `plane_islands`, `pour_reachable`, `reason_census` | `route_geom`, `route_scene` |
| `src/pcbc/patterns/__init__.py` | `Piece`, `Refusal`, `PatternResult`, `PatternCtx`, `PatternPlan`, `Exit`, `pad_exits`, `link_candidates`, `REGISTRY`, `pattern_copper(design, job, cs, text, board, *, stage)` | the four above |
| `src/pcbc/patterns/hop.py` | `run(ctx, spec)`, `HOP_MM = 6.0` | |
| `src/pcbc/patterns/chain.py` | `run(ctx, spec)` | |
| `src/pcbc/patterns/tap.py` | `run(ctx, spec)`, `TAP_REACH_MM = 1.5` | |
| `src/pcbc/patterns/spine.py` | `run(ctx, spec)`, `WIDE_MM = 0.4` | |
| `src/pcbc/patterns/ribbon.py` | `run(ctx, spec)` (bus) | |
| `src/pcbc/patterns/guard.py` | `run(ctx, spec)` | |

```python
# the PadGeom the whole core reads a pad through
@dataclass(frozen=True)
class PadGeom:
    ref: str; num: str; net: str
    kind: str                     # "smd" | "thru_hole" | "np_thru_hole"
    copper: tuple[Shape, ...]     # one per primitive; () for NPTH
    hole: Shape | None            # circle or capsule; (drill oval w h) is a capsule
    cu_layers: frozenset[str]     # "*.Cu" and "F&B.Cu" expanded
    mask_layers: frozenset[str]
    mask_margin: float            # (solder_mask_margin ...), 0.0 when absent

def pad_geoms(block: str, at: tuple[float, float, float], side: str) -> tuple[PadGeom, ...]
```

**The census `pads.py` must cover, so completeness is provable and not claimed** (counted across
the five placed boards, 2026-09-20): **363 smd** (160 rect, 156 roundrect, 24 oval, 15 circle,
8 custom), **30 thru_hole** (17 circle, 8 oval, 5 rect), **4 np_thru_hole** (circle); 397 pads in
all. `roundrect_rratio` takes the values 0, 0.243902 and 0.25; 8 `gr_poly` and 5 `gr_rect`
primitives; 15 pads carry `solder_mask_margin`; drills `(drill d)` and
`(drill oval 0.799998 1.499997)` / `(drill oval 0.799998 1.199998)` (the USB-C legs on both USB
boards — a scalar read of those under-models the hole by 0.35 mm at each end). Layer tokens to
expand: `F.Cu`, `B.Cu`, `In*.Cu`, `*.Cu`, `F&B.Cu`. A pad's `(at x y rot)` in a board file already
carries the footprint rotation, so `rot` applies to the primitive points and the footprint
rotation only to the pad anchor — the convention `parse_foot` documents.

### E.2 What changes in existing files

- **`constraints.py`**: add `ClearanceTable` and `clearance_table(cs)`. Nothing existing changes;
  no compiled number moves; every `tests/fixtures/compiled/*.json` stays byte-identical.
- **`stackup.py`**: one additive field, `mask_bridge_min: float = 0.0`, set to 0.10 on the JLC
  1 oz rungs. Read by nothing but A.4 rule 4.
- **`fanout.py`**: the stub is made exactly axis-aligned (A.5 rule 1); `_segment` / `_via` /
  `_snap_out` / `_snap_near` move to `route_emit` / `route_geom` and are imported back;
  `_excluded` also excludes the nets the hop pattern claimed; `fanout_copper` returns `Piece`s
  alongside its notes.
- **`route.py`**: `plane_targets(job)`; `krt_plan(..., plan: PatternPlan | None = None)`;
  `route_job` runs `pattern_copper` after the hops and again after `planes`, writes
  `00_patterns_pre.kicad_pcb` and `NN_patterns_post.kicad_pcb` (step files, so
  `blocking.step_boards` names `patterns_pre` as the step that placed an item and the blocking
  analysis keeps working for free), asserts pattern survival after each KRT step, merges both
  censuses and both move lists into the result, and runs `verify_copper` before the gate;
  `pin_copper_ids(..., keep=...)`; `write_fab_overrides` per C.5.
  `lock_copper`, `_local_nets`, `_krt_summary`, `_unrouted_move` unchanged.
- **`copper_bar.py`**: optional `reasons` argument, `by_reason` / `vias_pattern` /
  `vias_leftover` / `refusals` in totals. No renames; the `reasons=None` path is byte-identical to
  today's output.
- **`route_checks.py`**: `_seg_seg_dist`, `_pt_seg_dist`, `_segs_cross`, `_seg_rect_dist` and
  `_lane_strips` move to `route_geom` / `route_scene` and are imported back; `_pad_box` switches to
  `pads.pad_geoms`' AABB, which fixes the 5 um custom-pad hole. **Expect message changes on
  c3_usb, node and ds2**; they are re-recorded with the reason, as R0 and R1 did.
- **`build.py`**: carries `patterns`, `pattern_moves`, `notes` and `leftover` into the route step
  entry and prints the census and refusal lines.
- **`blocking.py`**: gains `move_line(net, what, goal, blockers, fixes) -> str`;
  `blocking_lines()` is refactored onto it with its output unchanged, so every pattern refusal and
  every KRT failure speak the same sentence shape.

### E.3 What must not change

`stackup.py`'s existing numbers; `dru.py`'s rules and severities; `constraints.py`'s compiled
values and `CompiledJob`'s fields; `netcheck.check_copper`'s contract and failure text; the gate;
`blocking.py`'s message *shape*; the KRT step names, order and flags for what remains;
`copper_bar`'s existing keys and definitions (`MICRO_MM`, `off_45`, `airwire_mm`); `stable_uuid`
keying; the `(locked yes)` byte positions; `pcb_place.Pad` / `Foot` / `parse_foot` / `lane()` and
everything `route_checks` reads from them (`PadGeom` is a second, richer view of the same block,
additive); `model.py` and `language.py` — **R2 adds no new syntax**. The two escape hatches
(`--strict-patterns`, `PCBC_PATTERNS=off`) are CLI and environment, not `board.py`.

---

## F. The measurement

### F.1 Recorded per board, before and after each slice

Into `docs/r2-measurements.md` and `report.json` under `route.patterns`, printed by `pcbc build`,
for blinky, buck, c3_usb, node and the DS2 Addon:

| column | source |
|---|---|
| routed mm, segments, vias | `copper_bar` totals |
| **leftover mm and %** | routed mm not carried by a pattern reason |
| leftover nets | `plan.refused` union the nets KRT still routes |
| by-reason census | segments, vias, mm per reason |
| off45, micro, worst detour + its net | `copper_bar` totals |
| refusals | count per pattern, and the list |
| soft rule hits | `check_copper(...)["soft"]` |
| wall time | pattern pre stage, pattern post stage, KRT stage, gate — separately |
| determinism | build twice into two dirs, byte-diff the routed board **and** `copper.json` |
| DRC | errors, unconnected, canary |

### F.2 The baseline: a fresh build, never the checked-in artifacts

The checked-in `examples/*/layout/*/routed/layout.kicad_pcb` (mtime 2026-09-19 18:40) **predate**
`00_fanout` and `plane_taps` and disagree with the pinned bar. Measured for this design with
`copper_bar` on those files:

| board | segments | vias | off45 | micro | mm | worst detour |
|---|---|---|---|---|---|---|
| blinky | 1 | 0 | 0 | 0 | 21.9 | LED 1.00 |
| buck | 97 | 5 | 5 | 22 | 145.7 | EN 1.77 |
| c3_usb | 296 | 16 | 10 | 89 | 304.4 | USB_DN 1.81 |
| node | 536 | 40 | 60 | 256 | 486.7 | CC2 4.80 |
| ds2 | 337 | 32 | 23 | 113 | 514.4 | REFP_F 3.23 |

A fresh `build_job(upto="route", force=True)` gives buck `96 / 4 / 3 / 26 / 142.5, VIN 2.09` and
node `420 / 30 / 63 / 161 / 501.5, T_OUT 1.72`, which is what `BAR` pins. **So every "before"
number in this design is the fresh build, and S1's first job is to write that table down for all
five boards.** Quoting the checked-in artifacts is how a design ends up arguing about a `VIN` that
has 18 segments in one generation and 29 in the next.

### F.3 Acceptance thresholds for R2 as landed

Non-negotiable, every slice:

1. **Gate**: all five boards `error is None`, zero DRC errors, zero unconnected items, canary
   fired. Zero DRC violations attributable to pcbc-written copper, always.
2. **Determinism**: two builds byte-identical — the routed `.kicad_pcb` and `copper.json` — and a
   shuffled-input build byte-identical.
3. **Self-check**: `verify_copper` empty on all five.
4. **Agreement**: no clearance, hole or edge error KiCad finds that the checker passed, on the
   four routed examples.

Numeric, against the S1 fresh baseline (the right-hand numbers are the panel's measured
hop+tap result and are what S4 and S5 are expected to reach; each is re-pinned by S1 if the
fresh build disagrees, **with its reason in the same PR**, never silently):

| board | `vias_leftover` | off45 | micro | worst detour | routed mm |
|---|---|---|---|---|---|
| blinky | 0 -> 0 | 0 -> **0** | 0 -> **0** | — | 22.5 -> **21.8675** (exact: 1 segment) |
| buck | 4 -> **<= 4** | 3 -> **<= 3** | 26 -> **<= 26** | 2.09 -> **<= 2.09** | 142.5 -> <= 142.5 |
| c3_usb | 16 -> **<= 17** | 7 -> **<= 7** | 182 -> **<= 120** | 1.81 -> **<= 1.49** | 315.7 -> <= 300.0 |
| node | 30 -> **<= 15** | 63 -> **<= 32** | 161 -> **<= 90** | 1.72 -> **<= 1.72** | 501.5 -> <= 420.0 |
| ds2 | 32 -> **<= 33** | 23 -> **<= 18** | 113 -> **<= 113** | 3.23 -> **<= 1.84** | 514.4 -> <= 485.0 |

`vias_pattern` is exact, not a ceiling, and is recorded per reason: blinky `{}`; buck `{}`;
c3_usb `{fanout: 7, tap: <= 40}`; node `{fanout: 2, tap: <= 72}`; ds2 `{fanout: 10, tap: <= 30}`.
(c3_usb's and ds2's one extra `vias_leftover` is the hop pattern spending a lane, and is inside the
ceiling. c3_usb and ds2 are two-layer boards with a back GND pour, so they get taps too — see C.1's
two-layer note — and their exact counts are set by S5's measurement, not predicted here.)

5. **Leftover** handed to KRT: **<= 70 %** of routed mm on every board after S5 (hops and taps
   landed), and **<= 45 %** after S7 (spines landed), excluding differential pairs, which R2 does
   not touch. blinky goes to **0 %** at S4, when the hop pattern's `direct` clause claims its one
   net.
6. **Refusals**, exact, per board: **<= 1 hop**, **<= 5 tap**, **<= 2 spine**, **0 hard**. A change
   either way is a deliberate edit to the table.
7. **Soft rule hits** must not rise on any board. `width_power` should fall on buck (40 today) and
   c3_usb (36) once spines carry the class width where KRT necked to `track_min`; a fall to zero
   on all five is a promotion candidate in the same PR, per R1's procedure.
8. **Wall time**: the two pattern stages together **<= 2.0 s** on node and ds2, **<= 0.3 s** on
   blinky; total route stage no more than 10 % slower than the S1 baseline. (The panel measured
   node getting 22 % *faster*, 32.9 -> 25.6 s, because KRT has less to do; that is an expectation,
   not a threshold.)
9. **`route_checks` messages** that change because of the custom-pad fix are re-recorded in the
   slice that changes them, with the before and after in the PR body.

---

## G. Slices

Eight slices, strictly sequential, each landing green on `pytest`, on the four examples and the
DS2 Addon to fab (`PCBC_REQUIRE_KICAD=1 PCBC_REQUIRE_KRT=1 pytest tests/test_examples_fab.py` plus
`pcbc build ~/Documents/MaD/Hardware/DS2Addon/pcbc/ds2_addon.py`), with its rows in
`docs/r2-measurements.md` and the README in the same PR. Sequential because each pattern's
acceptance is a bar number the previous slice fixed.

### S1 — record the before, and straighten the fanout stub (1 day)

**Files owned**: `docs/r2-measurements.md` (new), `src/pcbc/fanout.py` (the snap fix only),
`tests/test_examples_fab.py` (`BAR` gains a ds2 entry; numbers re-recorded from the fresh build),
`pcbc route --census` skeleton (the classifier that buckets existing copper by the pattern that
*would* own it — the same classifier the plan's own 37/31/14/12/6 % table came from).

**Files not to touch**: everything else. No new module in this slice.

**Test file**: `tests/test_fanout.py` (extended).

**Acceptance**: fresh numbers for all five boards in the F.1 table; every fanout stub exactly
axis-aligned, so pcbc's own contribution to `off45` is **0** (c3_usb 2 -> 0, node 2 -> 0,
ds2 10 -> 0, and ds2's board total 23 -> **<= 18**); every other bar number unchanged or better;
byte-identical on rebuild; ds2's ceilings pinned for the first time.

### S2 — the geometry core (3 to 4 days)

**Files owned**: `src/pcbc/route_geom.py`, `tests/fixtures/geom_vectors.json`.

**Files not to touch**: every other `src/pcbc/*.py`. This slice writes no copper and changes no
board.

**Test file**: `tests/test_route_geom.py`.

**Acceptance**: the property tests of D.3 (no false accept over 2000 seeded pairs; superset per
pad kind against the independent rasteriser; determinism; quantisation fixed point);
`test_turn_ok_wraps` green on all 64 octant pairs; `free_intervals` equals a 0.01 mm brute-force
sweep on 20 seeded spans of node; `geom_vectors.json` written and replayed by its own test;
`pytest -m "not kicad and not krt"` green; every board byte-identical (nothing calls this yet).

### S3 — pads, the scene, the table, the emitter, and the agreement test (3 to 4 days)

**Files owned**: `src/pcbc/pads.py`, `src/pcbc/route_scene.py`, `src/pcbc/route_emit.py`,
`constraints.clearance_table` + `ClearanceTable`, `stackup.mask_bridge_min`, the emitter and
`_lane_strips` moves out of `fanout.py` and `route_checks.py`, `route_checks._pad_box`.

**Files not to touch**: `route.py`, `krt_plan`, anything under `patterns/`.

**Test files**: `tests/test_pads.py`, `tests/test_route_scene.py`, `tests/test_clearance_table.py`,
`tests/test_edge_clearance.py`.

**Acceptance**: **fanout copper byte-identical** to S1's on all five boards (`test_fanout.py`
unchanged); `pad_geoms` on `J1.B4A9` gives a 0.700 x 1.400 mm outline, not 0.005; NPTH pads come
back with a hole and no copper; oval drills come back as capsules; all 397 pads of the five boards
round-trip; `test_clearance_table_covers_every_written_rule` green with `LOWERING_RULES` exempted;
`test_edge_clearance_ignores_outline_stroke` reproduces A.4's experiment under the `kicad` marker;
**the agreement test of D.2** — the checker over the four routed examples finds no clearance, hole
or edge error KiCad missed, and every disagreement in the other direction is recorded;
`route_checks` message changes on c3_usb, node and ds2 re-recorded with before/after.

### S4 — hop, and the pattern stage (3 days)

**Files owned**: `src/pcbc/patterns/__init__.py`, `src/pcbc/patterns/hop.py`,
`src/pcbc/route_verify.py` (`verify_copper` only), `route.py`'s pre stage, `PatternPlan`,
`plane_targets`, `pin_copper_ids(keep=)`, `write_fab_overrides` (C.5), `fanout._excluded`,
`blocking.move_line`, `copper_bar`'s `reasons`, `build.py`'s lines,
`tests/test_examples_fab.py` (BAR split, refusal counts).

**Files not to touch**: `route_geom.py`, `pads.py`, `route_scene.py` (frozen by S2/S3),
`patterns/tap.py` and the rest (do not exist yet).

**Test files**: `tests/test_pattern_hop.py`, `tests/test_route_plan.py` (updated),
`tests/test_copper_reasons.py`.

**Acceptance**: ds2's ten local two-pad nets (A0-A3, REFP, REFN, UART_RX, UART_TX, DRDY, nRESET)
are single-path hops of the recorded length; buck's `EN` is **refused**, the refusal names
`U1.4 pad [FB]` at 0.089 of 0.200 and the rule `copper`, and the test pins which candidate family
produced it; blinky's `LED` is one straight 0.16 mm segment from (9.44, 12.5) to (31.3075, 12.5),
**21.8675 mm**, now pcbc's and locked, so blinky is 1 segment, 0 vias, 0 off45, 0 micro and 0 %
leftover; `local_hops` disappears from buck's and c3_usb's plans;
`test_patterns_survive_krt` green on all five; `write_fab_overrides`'s change measured
patterns-off on all five and recorded; `PCBC_PATTERNS=off` reproduces S3's boards byte for byte;
bar: c3_usb 402 -> <= 318 segments, micro 182 -> <= 120, detour 1.81 -> <= 1.49; ds2 337 -> <= 316
segments, 514.4 -> <= 485 mm, detour 3.23 -> <= 1.84; buck and node unchanged.

### S5 — tap, the post stage, and the plane checks (3 to 4 days)

**Files owned**: `src/pcbc/patterns/tap.py`, `route_verify.plane_islands` and
`route_verify.pour_reachable`, `route.py`'s post stage and the `plane_taps`-only-on-refusal rule.

**Files not to touch**: `patterns/hop.py`, the geometry core.

**Test file**: `tests/test_pattern_tap.py`.

**Acceptance**: on node, **at least 66 of the 72 SMD plane pads** are tapped, at most 5 refused,
and the refused refs and their blockers are pinned exactly; every one of the 5 through-hole plane
pads is skipped and the test says why; every tap stub is exactly axis-aligned and every via clears
all five rules including hole-to-hole against its neighbours and against `J1`'s four NPTH
mounting holes; every tap via is a stackup via (4L 0.35/0.2), asserted against B.0's branch rule;
node's In1 **and In2** are each still one island after the taps; `verify_copper` empty; bar:
node 420 -> <= 354 segments, off45 63 -> <= 32, micro 161 -> <= 90, 501.5 -> <= 420 mm, detour
1.72 held, `vias_leftover` 30 -> <= 15, `vias_pattern` tap <= 72; zero unconnected on all five.

### S6 — chain (2 days)

**Files owned**: `src/pcbc/patterns/chain.py` (declared and implicit, the no-stub rule).

**Files not to touch**: everything from S2 to S5.

**Test file**: `tests/test_pattern_chain.py`.

**Acceptance**: the DS2 Addon's `Chain("VDDA", "J2.1", "C4.1", "U1.12")` is carried by the pattern
in feed order and `route_checks.check_chains` agrees; node's SCL, SDA and T_OUT are carried or
refused with a named blocker; a declared chain that cannot fit is a **hard** refusal and
`--strict-patterns` is not needed to see it; ds2 worst detour <= 1.84 held; no bar number worse
anywhere.

### S7 — spine (4 days)

**Files owned**: `src/pcbc/patterns/spine.py` (comb, backbone).

**Files not to touch**: everything from S2 to S6.

**Test file**: `tests/test_pattern_spine.py`.

**Acceptance**: buck's `VIN` and `5V` are a trunk plus ribs at the class width (0.781 mm and
0.4 mm) with **<= 10 segments each** and buck's worst detour **<= 1.60**; a fixture where the comb
is blocked falls to the backbone and the test shows both; the comb's refusal sentence names the
widest free lane it found, with the number; `width_power` soft hits fall on buck (40 -> <= 10) and
c3_usb (36 -> <= 10); leftover **<= 45 %** of routed mm on buck, c3_usb, node and ds2 excluding
pairs; no bar number worse anywhere; no spine places a via.

### S8 — bus, guard, stitch, and the numbers (3 days)

**Files owned**: `src/pcbc/patterns/ribbon.py`, `src/pcbc/patterns/guard.py`,
`tests/fixtures/bus_board.py`, `tests/fixtures/guard_board.py`,
`docs/r2-measurements.md` after-columns, README rows, `docs/router-plan.md`'s R2 line.

**Files not to touch**: every pattern from S4 to S7.

**Test file**: `tests/test_pattern_ribbon.py`.

**Acceptance**: the two fixture boards build to fab with the gate verified; four ribbon members at
the computed pitch with the gap at or above the class clearance on every leg including the
diagonals, and the measured skew equal to `0.828427 * pitch_span` per corner to 1e-6;
a crossed permutation refuses naming the two members; the guard is stitched every 2.5 mm within
one grid step and trims with a note where it cannot fit; `bus: 0` and `guard: 0` in every real
board's census; every threshold in F.3 met or re-recorded with its reason.

---

## H. Risks, and the decisions I am least sure of

**H.1 Which via a tap gets. This is the one I would most like overruled if I am wrong.**
`Constraint.via` and `stack.via_diameter` disagree on every board, and R2's rule (B.0) is: the
class via where the net's current crosses layers, the stackup via for a branch. Under that rule
node's 72 taps are 0.35/0.2 and fit; under the class rule they are 0.8/0.4, need 1.0 mm centre to
centre against 0.7, and node's density decides the tap pattern's fate. The physics is on my side —
a decap's tap carries that cap's ripple, and `via_amps(0.2)` is 0.53 A — but "the class says 0.8"
is a real counter-argument that a reviewer may prefer, and if the answer is the class via then the
tap pattern needs a per-net opt-out and node's acceptance drops. **The knob if we need one**, and it is *not* in R2 (E.3: R2 adds no new
syntax): `NetReq(..., tap_via="class"|"fab")` defaulting to `"fab"`, added in S5 only if S5's
measurement says a board needs both answers — not guessed at now. Note also that a stackup via on a Power net today is what KRT already places (`krt_plan`
passes `--via-size stack.via_diameter` globally), so the rule preserves today's board rather than
changing it.

**H.2 One via per ground pad, and the bar change that pays for it.** node goes from 30 vias to
about 89 (15 leftover + 72 tap + 2 fanout). I am confident it is correct practice and the rest of the bar
agrees — 66 fewer segments, 71 fewer micro segments, 31 fewer off-45, 84 mm less copper, detour
held. The alternative (cluster adjacent same-net pads onto one via within `TAP_JOIN_MM`) saves
maybe 10 vias and lengthens every return path, and its constant has no measurement behind it, so
it is out of R2. **What I am least sure of is whether the owner wants `BAR`'s `vias` ceiling
relaxed at all.** The fallback if not: record `vias_leftover` only and leave the total unpinned —
strictly less information, but it breaks nothing and the census still says which via is which.

**H.3 A pattern that fits but is worse than KRT's route.** A spine's trunk takes the lane a signal
needed and the leftover signal then detours; nothing in a local exact check can see it. The
mitigations are measurement, not cleverness: the per-board bar (a worse detour fails), the
leftover share, and the order of C.1 putting the widest copper after the least free. If a board
regresses, the escape is `PCBC_PATTERNS=off` for the release and a recorded reason, not a fudge
factor in the pattern.

**H.4 KRT with ten times more locked copper in its way.** `(locked yes)` holds — confirmed across
every step file of node and buck — so the risk is not that KRT rips pcbc's copper; it is that KRT
routes *through* a gap a spine leaves and then fails its own DRC (C.5's ds2 case, already seen), or
refuses a net whose partial copper it cannot extend. The guards are the per-step survival
assertion, `plan.done` exclusions, and the gate. The one thing I will not do is add a third KRT
workaround; the next one moves that pattern's share into pcbc instead.

**H.5 "No search inside a pattern" versus 112 candidates.** A hop enumerates up to 112 and a tap
up to 64. I claim that is not search: the list is fixed, finite, ordered, computed from the pads
alone, with no cost function, no backtracking over earlier decisions, and no state. The stricter
reading — one candidate, fail otherwise — fits 8 of 11 short hops instead of 10 and hands the
difference back to KRT. The mitigation that keeps it honest is that the *winning candidate's name*
is printed in the report (`Zh exit(top,right)`), so a human can see it was an enumeration.

**H.6 Fanout's via is still the stackup's while its lane is sized from the same number.** That is
consistent today and it is why fanout is left alone in R2 — changing it moves parts through
`Foot.lane`. But it means a Power-class closed-row pad escapes on a 0.3 mm drill while a
Power-class hop via beside it is 0.4 mm. Under H.1's rule that is correct (fanout is a branch); if
H.1 is overruled, fanout and the lane arithmetic have to move together, and that is a placement
PR, not a router one.

**H.7 Superset shapes cost fits.** A concave custom pad becomes its hull, so the USB-C shield pads
are fatter than their copper; the panel measured the cost at one refused link on c3_usb
(`J1.B4A9 -> C_VBUS.1`). Keep the hull: exact concave polygon-versus-capsule distance is where a
geometry core stops being provable in a week, and the refusal is a printed move the board author
can act on.

**H.8 The two magic numbers, `HOP_MM = 6.0` and `TAP_REACH_MM = 1.5`.** Both were chosen by
measurement, neither derives from the stackup, which is against R1's one-source spirit. Keep them
as named module constants with the measurement in the docstring, and revisit when the spine needs
a reach of its own — at which point "reach" probably becomes
`k * fanout_lane(stack, clearance, pitch)` and derives properly.

**H.9 Determinism.** The usual hazards: a `set` iterated, a dict ordered by insertion from an
unordered source, a float compared. The answers are all structural — canonical `Scene.items`
order, insertion-ordered `query`, literal candidate sequences, one quantisation at construction,
`free_intervals` merged with a 1e-9 tolerance and rounded to 4 dp before use — and CI's byte-diff
of two builds plus the shuffled-input build is the proof.

**H.10 Bus and guard with no board.** Building two patterns against fixtures risks building the
wrong thing, and the plan's own discipline says wait for the real board. I keep them, because the
sixth board of plan section 7 arrives in R4 and the `Bus` / `Guard` language already points at
them, but they are the last slice and their census prints zero. If S8 runs long, dropping it costs
nothing the five boards need.

---

## Slices, in order

| # | slice | files owned | files NOT to touch | test file | acceptance |
|---|---|---|---|---|---|
| S1 | record the before, straighten the fanout stub | `docs/r2-measurements.md`, `fanout.py` (snap fix), `test_examples_fab.py` (BAR + ds2), `pcbc route --census` | every other `src/pcbc/*.py` | `tests/test_fanout.py` | five-board fresh baseline written; pcbc's own off-45 contribution 0 (ds2 23 -> <= 18); no other bar number worse; byte-identical rebuild |
| S2 | the geometry core | `route_geom.py`, `tests/fixtures/geom_vectors.json` | every other `src/pcbc/*.py` | `tests/test_route_geom.py` | no false accept over 2000 seeded pairs; superset per pad kind; `turn_ok` wraps on all 64 octant pairs; `free_intervals` == a 0.01 mm sweep on 20 seeded spans; golden vectors replayable; all five boards unchanged |
| S3 | pads, scene, table, emitter, agreement | `pads.py`, `route_scene.py`, `route_emit.py`, `constraints.clearance_table`, `stackup.mask_bridge_min`, `route_checks._pad_box` | `route.py`, `krt_plan`, `patterns/` | `test_pads.py`, `test_route_scene.py`, `test_clearance_table.py`, `test_edge_clearance.py` | fanout bytes identical to S1; `J1.B4A9` reads 0.700 x 1.400; all 397 pads round-trip; table covers every written rule (`LOWERING_RULES` exempt); the edge experiment reproduced; **the agreement test clean** |
| S4 | hop + the pattern stage | `patterns/__init__.py`, `patterns/hop.py`, `route_verify.verify_copper`, `route.py` pre stage, `PatternPlan`, `plane_targets`, `pin_copper_ids(keep=)`, `write_fab_overrides`, `blocking.move_line`, `copper_bar` reasons, `build.py` | `route_geom.py`, `pads.py`, `route_scene.py`, `patterns/tap.py` and later | `test_pattern_hop.py`, `test_route_plan.py`, `test_copper_reasons.py` | ds2's ten hops single-path; buck `EN` refused naming `U1.4 [FB]` 0.089/0.200; blinky leftover 0 %; `local_hops` gone on buck and c3_usb; `PCBC_PATTERNS=off` byte-identical to S3; c3_usb micro <= 120, detour <= 1.49; ds2 detour <= 1.84 |
| S5 | tap + the post stage + plane checks | `patterns/tap.py`, `route_verify.plane_islands`, `route_verify.pour_reachable`, `route.py` post stage | S2-S4's files | `test_pattern_tap.py` | >= 66 of node's 72 SMD plane pads tapped, <= 5 refused, refs pinned; 5 thru-hole pads skipped with a reason; every tap via 0.35/0.2 per the branch rule; In1 **and In2** still one island each; node off45 <= 32, micro <= 90, mm <= 420, `vias_leftover` <= 15; zero unconnected |
| S6 | chain | `patterns/chain.py` | S2-S5's files | `test_pattern_chain.py` | ds2's `Chain("VDDA", ...)` carried in feed order and `check_chains` agrees; a declared chain that cannot fit is a hard refusal; no bar number worse |
| S7 | spine | `patterns/spine.py` | S2-S6's files | `test_pattern_spine.py` | buck `VIN`/`5V` = trunk + ribs at class width, <= 10 segments each, buck detour <= 1.60; the refusal names the widest free lane with its number; `width_power` 40 -> <= 10 on buck, 36 -> <= 10 on c3_usb; leftover <= 45 % excluding pairs; no spine via |
| S8 | bus, guard, stitch, the numbers | `patterns/ribbon.py`, `patterns/guard.py`, the two fixture boards, `docs/r2-measurements.md`, README, `router-plan.md` R2 line | every pattern from S4-S7 | `test_pattern_ribbon.py` | both fixtures build to fab, gate verified; ribbon gap >= class clearance on every leg including diagonals; skew == `0.828427 * pitch_span` per corner to 1e-6; a crossed permutation refuses naming both members; `bus: 0` and `guard: 0` on the five real boards; every F.3 threshold met or re-recorded with its reason |
