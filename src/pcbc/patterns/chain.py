"""`chain` — pads in a declared feed order (`docs/r2-design.md` B.2).

A chain is the one pattern whose shape is not derived from the geometry: it is an **order**, and the
order is the author's. `Chain("VDDA", "J2.1", "C4.1", "U1.12")` says the rail leaves the connector,
goes *through* the bypass cap and only then reaches the pin — the cap is in the path, not hanging off
it — and that is a sentence no router infers from a netlist, because every one of the six orders over
three pads connects the same three pads. So the chain is linked consecutively in the order it was
given, and it is never re-ordered to close the net.

**Every refusal this pattern makes is soft**, declared or not (C.6), and S6's review is why. The
slice shipped a declared chain as a *hard* refusal — an abort of the whole build before KRT runs —
on the argument that "falling through to KRT would produce copper that violates the intent rather
than merely a worse route, because KRT would branch". Two measurements retired it. On the only board
in the repo with a declared chain that is not a pair, KRT honours the order: it runs a single
straight 45-degree trace **across** `C4.1`'s pad, 1.1185 mm of that trace's centre line inside the
pad's own copper. And the invariant the abort was protecting was checked nowhere at all —
`docs/router-plan.md` line 202 tags R-X4 "**P** (chain), **V**" and the **V** did not exist. It does
now (`route_verify.chain_order`, run on the routed board by `build._chain_gate`), so a declared order
that is genuinely violated fails the build on a measurement of the finished copper rather than on a
guess about what KRT was going to do. `--strict-patterns` still turns every refusal here into a stop
for the author who wants one.

Two populations, and B.2's own split:

- **declared** — every `GroupSpec(kind="chain")`, in board order, at the order the `Chain()` wrote.
  Soft refusal, and `route_verify.chain_order` reads the routed board afterwards.
- **implicit** — any net with three or more pads that no other pattern owns, ordered by
  `route_checks._principal_order`: the *same* function `check_chains` measures an undeclared chain
  against, so the placement check and the router cannot disagree about what the order is. Soft
  refusal, by `hop._refuse`'s argument — nothing was declared, so nothing is being disobeyed, and
  no gate reads an order pcbc invented.

**What this pattern deliberately does not do.** It writes no via and it changes no layer: a chain is
a feed order on one layer, so R-I2's `per_change` budget never arises, and a net whose stations do
not all share a copper layer gets the links it can — `spine`'s rule, for `spine`'s reason. It has no
detour cap, unlike `hop` (`DETOUR_MAX`) and `spine` (`LINK_DETOUR_MAX`): those two choose their own
shape and a doubled-back one is a routing problem wearing a pattern's clothes, while a chain's shape
is the author's and a long link between two stations they put far apart is the thing they asked for.
What bounds a chain is the board — `blocked`, `lane_ok` and R-X4 below — and not a ratio.

**Five decisions this file does not get to make**, taken in `docs/r2-design.md` S6 from the survey
and recorded here so they are not re-decided silently:

1. **R-X4's distance is `stackup.clearance_min + w/2`, not B.2's `between(net, net) + w/2 +
   pad_half`.** `ClearanceTable.between` returns `(0.0, "same net")` for a net against itself, so
   B.2's formula collapses to "must not *touch* a later pad" — a rule quantised geometry steps
   around. `stackup.clearance_min` is the number `route.py` already hands KRT as
   `--same-net-pad-clearance` and the one `ClearanceTable.via_to_same_net_smd_pad` returns for
   exactly this same-net-pad question.
2. **`blocked()` cannot express R-X4 at all.** `route_scene._pair_clashes` skips the copper rule for
   same-net pairs by design (and must: a chain link *lands* on its own stations), so the standard
   clash path can never report a later chain member. `stub_clash` below is an explicit test that
   synthesises its own `Clash`, not a tightened `need` handed to `blocked()`.
3. **Four of the five declared chains in the repo are pair nets** — c3_usb's and node's `USB_DP` and
   `USB_DN` — so B.2's "pairs are R4's" skip rule leaves this pattern **one** declared chain to
   route: the DS2 Addon's `VDDA`. The four skips are printed notes, never silences.
4. The implicit population is **claimed and tried, not pre-filtered by length**. node's `SCL` and
   `SDA` open with ~39 mm links and the S6 acceptance wants them *refused with a named blocker*; a
   39 mm run across a placed board names real copper, and that is a measurement rather than a guess.
5. **The width of a net with no `NetReq` is `synthesised(net)`'s Default class width, not
   `stack.track_min`.** `hop._width` falls back to the fab floor (0.0889 mm on node); B.0 says a
   pattern writes at the class width or not at all, and the fab floor is not a class. `_width` reads
   it off `ConstraintSet.classes`, which is where `synthesised` gets it too — see that function.

**Three deviations from B.2, each with what is behind it.**

- **`ELBOWS`** — the two right-angled shapes B.0 names and `link_candidates` drops one step before
  `mitre` repairs them, offered again at the end of this pattern's candidate list. Two nets finished
  and three refusals given a blocker to name; the measured table is in that constant's docstring.
- **A `Chain()` member is a pin name, not a pad.** `U1.VIN` may be one pad or four, and `_members`
  resolves a member to **every** pad that pin names, in `patterns._pad_key` order, making each of
  them a station — the chain feeds all of them, in the one order that is a function of the
  footprint. No member of the five declared chains in this repo maps to more than one pad, so the
  rule is stated rather than measured, and the first board that exercises it should measure it.
- **R-X4 fires on no board here.** `stub_clash` is implemented and tested against a fixture, and it
  has never refused a candidate on blinky, buck, c3_usb, node or ds2. Its docstring says so.

**And one thing this pattern does that the S6 acceptance did not expect: it refuses ds2's `VDDA`.**
The acceptance asks for that chain to be *carried* in feed order; the placement cannot give it.
`C5`, a 3V3 bypass cap, sits with its pad ending at `x` 22.63 directly across `U1.12`'s escape column
at 22.83, and every other approach is cut off by `U1`'s own row of five escape vias. B.2's worked
example of a chain refusal is **this link, on this board, naming this cap** — the design doc
illustrated it refusing on the same page that asked for it to be carried, and the measurement says
the illustration was right.

What the refusal is *not* is a verdict on the net. This pattern is a fixed enumeration of B.0's seven
shapes over `exits(a) x exits(b)` plus the two `ELBOWS`, bounded by `MAX_PER_LINK`, and the route
this link needs is a six-corner detour of 14.744 mm around three sides of the `U1`/`C5` cluster for a
3.8204 mm airwire — a shape no candidate in that set can express, because `link_candidates` builds
every point out of `ax, ay, bx, by` and all 96 candidate points for this link lie inside the two
exits' own bounding box. That is R3's maze router's job and not a defect in this enumeration, and it
is why the refusal is **soft**: the pattern is saying "not with these shapes", which is not the same
sentence as "not at all". KRT then routes it — in the declared order, measured — and
`route_verify.chain_order` is what checks that it did.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from ..blocking import move_line
from ..copper_bar import airwire_mm
from ..route_checks import CHAIN_HOP_MM, _principal_order
from ..route_emit import Piece
from ..route_geom import Pt, Shape, clears, gap, qp, track_shape
from ..route_scene import Clash, Exit, Item, blocked, pad_exits
from . import (
    MAX_LINKS,
    PatternCtx,
    PatternResult,
    Refusal,
    Terminal,
    lane_ok,
    legs_of,
    legs_ok,
    mitre,
    pieces_of,
    shape_ok,
    terminals,
    toward_first,
)
from . import spine as _spine
from .spine import WIDE_MM, anchor

REASON = "chain"

CONNECTS = True
"""A chain's copper joins the stations of the net it is on (`docs/stitch-plan.md` §2k). See
`hop.CONNECTS`. `chain` is not in any stage tuple (`patterns.MID`), so this is never read today; it
is declared because `_modules()` is what a stage looks a module up in, not `_STAGES`."""

ELBOWS = ("L-h*", "L-v*")
"""B.0's `L-h` and `L-v`, offered again **after** `mitre` has had them rather than before.

`link_candidates` builds all seven of B.0's shapes and then drops any that fails `turn_ok`, and a
right angle fails it — so `MAX_LINKS`' own docstring records that `L-h` and `L-v` "survive only where
they degenerate to a straight line". But every caller of `link_candidates` mitres afterwards, and
`mitre` is the function whose whole job is to make a right angle legal: it cuts `MITRE_MM` back along
each leg and joins them with a 45. The filter therefore runs one step before the repair, and the two
shapes B.0 names are unreachable for any pattern that mitres — which is all of them.

**What that costs is measured on all five boards**, driving this pattern over each board's placed
copper with the hops and the fanout already down (2026-09-20):

| board | net | without the elbows | with them |
|---|---|---|---|
| buck | `FB` | refuses `no candidate`, no blocker to name | refuses `copper`, naming `R_FB_TOP.1 [5V]` and its number |
| c3_usb | `EN` | 2 of 3 links, 5.997 mm, refused | **3 of 3**, 9.803 mm, and c3_usb's chain refusals go 1 -> **0** |
| node | `GATE` | 0 of 2 links, nothing written, refused at link 1 | 1 of 2, 2.149 mm, refused at link 2 |
| ds2 | `REFP_F` | 2 of 3 links, 13.055 mm, refused `hole_to_copper` | **3 of 3**, 25.429 mm, no refusal |
| ds2 | `AIN1` | refused `hole_to_copper` on `J1.1`'s barrel | refused `copper` on `J2.3 [A0]`, a pad an author can move |
| blinky | — | no chains at all | no chains at all |

Two of those are a net finished and three are a refusal that stops saying "no shape existed" and
starts naming a blocker with a millimetre in it, which is the difference between a move an AI can act
on and a dead end. The elbow's corner comes out of `mitre` as a 0.2828 mm chord, so it is the same 135-degree
copper every other corner pcbc writes and `pcbc_geometry_angles` sees nothing new.

**It does not rescue the one declared chain, and this paragraph is the corrected version of the
claim that it made the refusal actionable.** ds2's `Chain("VDDA", "J2.1", "C4.1", "U1.12")` still
refuses on link 2. The elbow does add a candidate that misses by a number an author could move `C5`
by — an `L-h*` over the top at `y` 5.641 descending at `x` 22.83, which leaves **0.075 mm of air
where the Power class needs 0.200** — but **that number is never printed**, and S6's review caught it
(`docs/r2-measurements.md`, S6). `_link` keeps the *worst* clash by `need - have`, and the worst is a
different `L-h*`, the one that runs along `y` 6.4411 straight through `C5.1`'s pad at **-0.350**. All
five candidates that reach the clearance judge, measured on the placed board (2026-09-20):

| candidate | blocker | have | need - have |
|---|---|---|---|
| `Z-far` | `C5.1 [3V3]` | 0.0554 | 0.1446 |
| `L-h*` | `C5.1 [3V3]` | **-0.3500** | **0.5500** (printed) |
| `L-v*` | `U1.13 [3V3]` | 0.2001 | -0.0001 |
| `L-v*` | `U1.15 [ADC_TX]` | -0.0614 | 0.2614 |
| `L-h*` | `C5.1 [3V3]` | 0.0750 | 0.1250 |

Two consequences, both recorded rather than repaired here. The refusal's `Place("C5", toward="up")`
is computed from the printed clash's position, so it is the move for the candidate that runs *through*
the cap and not for the near miss, which needs `C5` 0.125 mm to the **left**.

And the third row, which is the one that has to be read with its configuration attached. On **this**
path — the mid stage driven straight at the placed board, no `pre`, no fanout — that candidate's worst
clash is `U1.13` at a true gap of 0.20009999999999903 mm against `need + EPS_MM` = 0.2001. It is not a
tie lost in a squared comparison: the computed distance is **one ulp short** of the requirement, about
1e-15 mm, on geometry that is exactly at the limit. `EPS_MM` is `route_geom`'s deliberate "always
stricter than KiCad" margin and is not a bug to shave.

**It is also not what happens on a build, and the difference matters more than the ulp.** Re-measured
on the real sequence (`pre` -> fanout -> `mid`), the fanout's escape copper is on the board, link 1
lands differently, and link 2's candidates are judged against a different head. Every clash the chain
stage then sees on `VDDA`:

| blocker | true gap | needs |
|---|---|---|
| `U1.16 [ADC_RX]` | 0.11912621869315965 | 0.200 |
| `C5.1 [3V3]` | 0.07499999999999715 | 0.200 |
| `U1.15 [ADC_TX]` | -0.1399000000000008 | 0.200 |
| `U1.14 [ADC_DRDY]` | -0.2856000000000005 | 0.200 |
| `U1.13 [3V3]` | -0.3321493290600952 | 0.200 |
| `C5.1 [3V3]` | -0.35 | 0.200 |

The closest is short by **0.0809 mm**, eight hundred times `EPS_MM`. So "had it cleared, this chain
would be complete" is true of the placed-only probe and **false of the build**: on the board pcbc
actually produces there is no near miss to clear, and the refusal is the geometry, not the epsilon.

The elbows are this pattern's own and not the shared builder's: `link_candidates` is `hop`'s and
`spine`'s too, and giving them two new shapes would move copper on boards whose numbers are recorded
in `tests/test_examples_fab.py`. The honest fix is upstream in a slice that owns those numbers; this
is the one S6 owns."""

MAX_PER_LINK = 4 * 4 * (MAX_LINKS + len(ELBOWS)) + 3
"""The bound on one link's candidate list, declared here and nowhere else.

`spine.link_shapes` is B.0's `exits(a) x exits(b) x link_candidates` with the three `crowded` centre
shapes in front of it, and this pattern uses it unchanged and then appends `ELBOWS`: the shapes are
reason-free, and a chain link and a backbone link are the same question asked about two pads of one
net. What is *not* shared is `spine._link`, which hard-codes `reason="spine"` off a `SpineSpec`; its
body is reproduced below at this pattern's own reason, width and layer."""


# --- which nets this pattern has something to say about -------------------------------------------


@dataclass(frozen=True)
class ChainSpec:
    """One chain: the stations in feed order, and where the order came from.

    `declared` separates the author's order from pcbc's reading of a placement, and after S6's
    review that distinction decides **where the order is checked**, not how hard the refusal is: a
    declared chain is read back off the routed board by `route_verify.chain_order`, an implicit one
    is not, because there is no declaration to disobey. It is carried on the spec rather than
    re-derived in the refusal, where it would be a second opinion about the same fact.

    `labels` is parallel to `stations` and is what a message calls each stop: the `Chain()`'s own
    `REF.PIN` spelling for a declared chain, so the refusal quotes the line the author wrote, and the
    `REF.PADNUM` owner for an implicit one, because there is no line to quote.
    """

    net: str
    declared: bool
    where: str  # "Chain line 97" | "3 pads on their own principal axis"
    stations: tuple[Terminal, ...]
    labels: tuple[str, ...]
    width: float
    layer: str  # the one copper layer the whole chain runs on; "" when the stations share none
    airwire: float
    spans: tuple[float, ...] = ()
    """Centre to centre for each consecutive pair, parallel to `stations[:-1]`.

    Carried so the locality bound and the refusal read the same number rather than re-deriving it."""
    skip: str = ""  # non-empty: this chain is a printed note and no copper at all


def _width(ctx: PatternCtx, net: str) -> float:
    """The class width, and for a net with no `NetReq` that means the **Default class**, not the fab
    floor (S6 decision 5).

    `constraints.compile_constraints.synthesised` is the function that would give this net a
    `Constraint` if anything had asked for one — a `Bus()`, a `Chain()`, a `Guard()` — and it
    compiles a `generic` request, whose preset carries `width_mm=None`, which `_compile_req` reads as
    `floor_w`: the exact number `classes["Default"].track_width_mm` is built from, one line above it.
    So reading the Default class here is not an approximation of `synthesised`; it is the same
    number, without recompiling the design to get at a closure.

    `hop._width`'s fallback is `stack.track_min` — 0.0889 mm on node — and it is right there and
    wrong here: a hop necks into a pad by R-I3 and is allowed to be narrow, while B.0's "a pattern
    writes at the class width or not at all" is the whole of what a chain promises.
    """
    c = ctx.cs.by_net(net)
    if c is not None:
        return float(c.width_mm.value)
    default = next((k for k in ctx.cs.classes if k.name == "Default"), None)
    return float(default.track_width_mm) if default is not None else ctx.scene.stack.track_min


def _layer(ctx: PatternCtx, net: str, stations: tuple[Terminal, ...]) -> str:
    """The one copper layer every station shares, intersected with the net's declared `layers` in the
    constraint's order — `spine._layer` verbatim, so a `NetReq(layers=[...])` is honoured and not
    merely avoided, and a chain never picks a layer the author excluded."""
    shared = frozenset(ctx.scene.layers)
    for t in stations:
        shared = shared & t.layers
    c = ctx.cs.by_net(net)
    order = list(c.layers) if c is not None and c.layers else list(ctx.scene.layers)
    for layer in order:
        if layer in shared:
            return layer
    return ""


def _members(ctx: PatternCtx, net: str, spec: str) -> tuple[tuple[Terminal, str], ...]:
    """One `Chain()` member, `"REF.PIN"`, as the terminals it names — `route_checks._refpin_pad`'s
    resolution, over a whole net's terminals rather than over one `Foot`.

    A member is a **pin name** as `Place(to=)` takes it (`U1.VIN`), falling back to a pad number when
    the part declares no such pin, and one pin may own several pads (`GND on 1 and 44`). Every pad it
    names becomes a station, in `terminals`' own `sorted(ref, pad_num)` order.
    """
    ref, _, pin = spec.partition(".")
    inst = next((i for i in ctx.design.instances if i.ref == ref), None)
    nums: tuple[str, ...] = (pin,)
    if inst is not None and pin in inst.part.pins:
        nums = inst.part.pins[pin].pads
    want = {f"{ref}.{num}" for num in nums}
    return tuple((t, spec) for t in terminals(ctx.scene, net) if t.owner in want)


def _skip(net: str, where: str, why: str) -> ChainSpec:
    return ChainSpec(net=net, declared=True, where=where, stations=(), labels=(), width=0.0, layer="", airwire=0.0, skip=why)


def specs(ctx: PatternCtx) -> tuple[ChainSpec, ...]:
    """B.2's population: every declared `Chain()` in **board order**, then the implicit chains in
    `sorted(net)` — B.0's order for a net-driven pattern.

    Declared first because a declared order is the author's and an implicit one is pcbc's reading of
    where they put the parts; when the two populations ever overlap on a net (they cannot today — a
    declared net is excluded from the implicit sweep) the author's wins by running first.

    The implicit population is "three or more pads and nobody else's". `tap` owns every pad of a
    plane or pour net, `hop` owns a net with exactly two terminals, and `spine` owns a net with three
    or more whose class is at least `WIDE_MM` — so the three predicates here are read off those three
    patterns rather than restated, and a net that changes hands changes hands in one place. Pairs are
    R4's and a declared bus member is the ribbon's (B.5), exactly as in `hop.specs` and
    `spine.specs`.
    """
    scene = ctx.scene
    out: list[ChainSpec] = []
    declared: set[str] = set()
    for ch in ctx.design.chains:
        where = f"Chain line {ch.line}"
        declared.add(ch.net)
        c = ctx.cs.by_net(ch.net)
        if c is not None and c.pair is not None:
            # B.2: "Chains on nets carrying a `PairSpec` are skipped with a printed note, not
            # silently: pairs are R4's." Four of the five declared chains in this repo are here.
            out.append(_skip(ch.net, where, f"carries a Pair, and R2 routes no pairs at all — {', '.join(ch.pads)} is R4's"))
            continue
        pairs: list[tuple[Terminal, str]] = []
        for member in ch.pads:
            got = _members(ctx, ch.net, member)
            if not got:
                # `compile_constraints` already refused this line, so the author has a message; a
                # second one here says which half of the build stopped reading it.
                out.append(_skip(ch.net, where, f"{member} names no pad on {ch.net}, which `pcbc check` refused before this"))
                pairs = []
                break
            pairs.extend(got)
        if not pairs:
            continue
        if len(pairs) < 2:
            out.append(_skip(ch.net, where, f"names {len(pairs)} pad, and a chain is an order over at least two"))
            continue
        stations = tuple(t for t, _l in pairs)
        out.append(
            ChainSpec(
                net=ch.net,
                declared=True,
                where=where,
                stations=stations,
                labels=tuple(label for _t, label in pairs),
                width=_width(ctx, ch.net),
                layer=_layer(ctx, ch.net, stations),
                airwire=round(airwire_mm([anchor(t) for t in stations]), 4),
                spans=_spans(stations),
            )
        )
    for net in sorted({it.net for it in scene.items if it.kind == "pad" and it.net}):
        if net in declared or net in scene.plane_of:
            continue
        c = ctx.cs.by_net(net)
        if c is not None and c.pair is not None:
            continue
        if c is not None and c.group is not None and c.group.kind == "bus":
            continue
        pads = terminals(scene, net)
        if len(pads) < 3:
            continue  # two terminals is a hop's (B.1), and B.2 says three or more
        width = _width(ctx, net)
        if width >= WIDE_MM:
            continue  # `spine.specs`' own predicate: this net is the spine's (B.4)
        stations = tuple(pads[i] for i in _principal_order([anchor(t) for t in pads]))
        out.append(
            ChainSpec(
                net=net,
                declared=False,
                where=f"{len(stations)} pads on their own principal axis",
                stations=stations,
                labels=tuple(t.owner for t in stations),
                width=width,
                layer=_layer(ctx, net, stations),
                airwire=round(airwire_mm([anchor(t) for t in stations]), 4),
                spans=_spans(stations),
            )
        )
    return tuple(out)


# --- R-X4: the no-stub rule -----------------------------------------------------------------------


def stub_need(scene, w: float) -> float:
    """R-X4's distance: `stackup.clearance_min + w/2` (S6 decision 1).

    B.2 writes `between(net, net) + w/2 + pad_half`, and `between` of a net against itself is
    `(0.0, "same net")` — so B.2's own formula says "must not touch", which quantised geometry steps
    around by a nanometre. The process floor is the number that means something here: copper closer
    together than a fab's minimum clearance with bare laminate across it is copper a fab may bridge,
    and a link bridging into a later member **is** the stub the rule forbids.

    The `w/2` stays, and it is measured from the piece's own copper rather than from its centre line:
    `clears` is asked with the track's `Shape`, which already carries `w/2` as its offset radius, so
    the test is "half a track plus the process floor of air between this copper and that pad" — one
    track's worth of room, which is the width of the stub the rule is there to refuse.
    """
    return scene.stack.clearance_min + w / 2.0


def _pad_items(ctx: PatternCtx, t: Terminal) -> tuple[Item, ...]:
    """Every primitive that station draws, not only the widest one `Terminal.item` carries: a custom
    pad is several `Item`s in the scene, and a link may run past the half of it that is not the
    widest. `Scene.pads_of` narrows to the net first, so this is a walk over one net's pads and not
    over the board — it is asked once per later stop per candidate that has already cleared."""
    return tuple(it for it in ctx.scene.pads_of(t.net) if it.owner == t.owner)


def stub_clash(ctx: PatternCtx, spec: ChainSpec, pieces: tuple[Piece, ...], later: tuple[Terminal, ...]) -> Clash | None:
    """R-X4: does this link's copper run past a **later** member's pad? The worst offender, or None.

    "The chain goes through pads in order, never past one and back" (B.2). `blocked()` cannot answer
    it — `_pair_clashes` skips the copper rule for a same-net pair, and it must, because a chain link
    lands *on* its own two stations — so this is an explicit test with a `Clash` synthesised for the
    refusal line (S6 decision 2).

    **It fires on none of the five boards** (measured 2026-09-20, every link of every spec on blinky,
    buck, c3_usb, node and ds2: zero hits), which is worth writing down rather than leaving as an
    absence. A chain whose stations are in principal-axis order rarely runs past a later one, and the
    one declared chain in the repo has three stops. So the rule is carried by a fixture in
    `tests/test_pattern_chain.py` that puts a pad in a link's path, and the first board to fire it in
    anger should be recorded here.

    Only the members *after* the link's far station are tested. An earlier station is not a stub: the
    run already passes through it by construction, and the copper the link is being judged beside is
    its own. `clears` makes the decision and `gap` only describes it, which is `route_geom`'s standing
    rule — `clears` carries `EPS_MM` internally and the number is not added a second time here.
    """
    worst: tuple[float, Clash] | None = None
    for t in later:
        for it in _pad_items(ctx, t):
            if it.copper is None:
                continue
            for p in pieces:
                if p.kind != "seg" or p.b is None:
                    continue
                layer = p.layer if isinstance(p.layer, str) else p.layer[0]
                if layer not in it.layers:
                    continue
                need = stub_need(ctx.scene, p.w)
                mine: Shape = track_shape(p.a, p.b, p.w)
                if clears(mine, it.copper, need):
                    continue
                have = gap(mine, it.copper)
                clash = Clash(it, have, round(need, 4), it.at(), "no_stub", f"a later stop on {spec.net}'s chain")
                if worst is None or (need - have) > worst[0]:
                    worst = (need - have, clash)
    return worst[1] if worst else None


# --- the candidate list ---------------------------------------------------------------------------


def elbows(ea: Exit, eb: Exit) -> tuple[tuple[str, tuple[Pt, ...]], ...]:
    """`ELBOWS`: axis then axis, both ways round, with the corner left for `mitre` to cut.

    `link_candidates`' own construction for `L-h` and `L-v`, in integer-exact form: the corner takes
    one coordinate from each already-quantised exit, so it is on KiCad's grid by construction and
    `qp` sees it exactly once. A corner equal to either end is a straight line `direct` already
    offers, so it is not returned — a duplicate candidate is not a second answer.
    """
    ax, ay = ea.at
    bx, by = eb.at
    out: list[tuple[str, tuple[Pt, ...]]] = []
    for name, corner in (("L-h*", qp((bx, ay))), ("L-v*", qp((ax, by)))):
        if corner == ea.at or corner == eb.at:
            continue
        out.append((name, (ea.at, corner, eb.at)))
    return tuple(out)


def link_shapes(a: Terminal, b: Terminal, exits_a, exits_b) -> tuple[tuple[str, tuple[Pt, ...]], ...]:
    """Every shape this pattern offers one link, in order: `spine.link_shapes` unchanged, then the
    `ELBOWS` for every exit pair.

    Appended rather than interleaved, so the prefix of this list is byte-identical to what a spine
    backbone link would try and the elbows can only answer a link that had no answer at all. Order is
    the answer here as everywhere: there is no score, and the first candidate that clears wins.
    """
    out = list(_spine.link_shapes(a, b, exits_a, exits_b))
    for ea in exits_a:
        for eb in exits_b:
            for name, pts in elbows(ea, eb):
                out.append((name, (anchor(a), *pts, anchor(b))))
    return tuple(out)


# --- one link, and then the run -------------------------------------------------------------------


def _served(scene, pieces: tuple[Piece, ...], net: str) -> frozenset[str]:
    """Which footprints this candidate serves, asked the way **D.1 will ask it** and not the way a
    pattern usually does.

    `lane_ok` is normally handed the link's two terminals, because a link runs to a pad and a lane
    exists so that footprint's own pads can escape. `route_verify.served_refs` asks the same question
    of the finished copper by looking for a piece **endpoint** inside a pad, and for `hop`, `spine`
    and `tap` the two answers agree. For a chain they do not, and the reason is what a chain *is*: the
    feed goes **through** each stop, so a middle station is an interior point of the run, and
    `pieces_of` merges the two collinear legs either side of it into one segment whose endpoints are
    somewhere else entirely. `served_refs` then cannot see that the copper serves that footprint at
    all — measured on ds2's implicit `3V3` chain, whose run through `U1.13` was accepted by `lane_ok`
    with `U1` exempt and then failed `verify_copper` with `runs 3.3951 mm along U1 lane top`.

    `served_refs`' own docstring predicted this: "Tightening it is a matter of threading the run
    through, and it waits for a pattern that writes disjoint runs on one net (`chain`, S6)." Until
    that threading exists, the pattern asks the **judge's** question, which can only make it refuse
    copper D.1 would have allowed and never the reverse — the direction B.0 requires, since a pattern
    that emits nothing is correct and a pattern that emits what its own self-check rejects is a crash.
    """
    from ..route_verify import served_refs

    return served_refs(scene, pieces, net, REASON)


def _dedupe(pts: tuple[Pt, ...]) -> tuple[Pt, ...]:
    out: list[Pt] = []
    for p in pts:
        if not out or out[-1] != p:
            out.append(p)
    return tuple(out)


def _link(
    ctx: PatternCtx,
    spec: ChainSpec,
    a: Terminal,
    b: Terminal,
    mine: frozenset[int],
    head: tuple[Pt, ...],
    later: tuple[Terminal, ...],
) -> tuple[tuple[tuple[Pt, ...], tuple[Piece, ...]] | None, int, Clash | None, str]:
    """One more station on the run: B.0's `exits(a) x exits(b) x link_candidates`, first that clears.

    `spine._link`'s body at this pattern's own reason, width and layer. It is copied rather than
    imported because `spine._link` reads a `SpineSpec` and hard-codes `reason="spine"`, and a piece
    whose reason lies is a piece the copper bar, `_owned_copper` and the ampacity gate all attribute
    to the wrong pattern. What *is* shared is everything reason-free: `spine.link_shapes` and the
    `crowded` reading of the exits underneath it.

    `head` is the run this link extends and the candidate is judged as the whole run it will be —
    `shape_ok`'s rule one station further out — because a mitre at a middle station moves copper that
    was already accepted, and judging each link alone writes a right angle there and then fails
    pcbc's own self-check.

    The width never moves. There is no neck into a pad here as there is in `hop` and no bypass
    allowance as there is in `spine`: a chain is a feed, every station carries what the whole chain
    carries, and B.0's "a pattern never degrades" has nothing to trade against.
    """
    scene = ctx.scene
    exits_a = toward_first(pad_exits(scene, a.item, spec.width, spec.layer), a, b)
    exits_b = toward_first(pad_exits(scene, b.item, spec.width, spec.layer), b, a)
    worst: tuple[float, Clash] | None = None
    tried = 0
    why = ""
    for _name, pts in link_shapes(a, b, exits_a, exits_b):
        raw = _dedupe(head + pts)
        full = mitre(raw)
        if full is None or not shape_ok(full):
            continue
        pieces = pieces_of(full, [spec.width] * (len(full) - 1), net=spec.net, reason=REASON, layer=spec.layer, owner=a.owner, board=ctx.board)
        merged = legs_of(pieces)
        if not legs_ok(merged):
            continue
        tried += 1
        fits, _lane = lane_ok(scene, pieces, _served(scene, pieces, spec.net))
        if not fits:
            why = why or "lane"
            continue
        clash = blocked(scene, pieces, spec.net, ignore=mine)
        if clash is None:
            stub = stub_clash(ctx, spec, pieces, later)
            if stub is None:
                return ((merged, pieces), tried, None, "")
            if worst is None or (stub.need - stub.have) > worst[0]:
                worst = (stub.need - stub.have, stub)
            continue
        if worst is None or (clash.need - clash.have) > worst[0]:
            worst = (clash.need - clash.have, clash)
    return (None, tried, worst[1] if worst else None, why or ("no candidate" if not tried else "copper"))


def _span(a: Terminal, b: Terminal) -> float:
    """Centre to centre between two stations, the distance `CHAIN_HOP_MM` is a bound on."""
    ax, ay = anchor(a)
    bx, by = anchor(b)
    return math.hypot(bx - ax, by - ay)


def _spans(stations: tuple[Terminal, ...]) -> tuple[float, ...]:
    """`_span` for each consecutive pair, rounded, so the bound and the refusal read one number."""
    return tuple(round(_span(a, b), 4) for a, b in zip(stations, stations[1:]))


def _run(ctx: PatternCtx, spec: ChainSpec) -> tuple[tuple[Piece, ...], list[tuple[str, str]], tuple | None, int]:
    """The stations linked consecutively, in the spec's order, **stopping at the first failure**.

    This is where a chain and a spine part company, and B.2 says why. A spine whose link fails splits
    into two components and keeps both, because a spine is a shape and half a shape is still copper
    that carries current. A chain is an *order*: emitting links `i+1..n` after refusing link `i`
    would be copper laid in an order the author did not ask for, joined up later by a router that
    does not know the order exists. So links `0..i-1` are emitted, link `i` is named in the refusal,
    and nothing after it is tried. It never re-orders to close the net.
    """
    mine = frozenset(it.id for it in ctx.scene.items if it.owner in {t.owner for t in spec.stations})
    joins: list[tuple[str, str]] = []
    head: tuple[Pt, ...] = ()
    grown: tuple[Piece, ...] = ()
    tried = 0
    for i, (a, b) in enumerate(zip(spec.stations, spec.stations[1:])):
        if _span(a, b) > CHAIN_HOP_MM + 1e-9:
            # **The bound this pattern earns, and it applies to a declared chain too.**
            # `route_checks.CHAIN_HOP_MM` is already the codebase's word for the distance, with this
            # sentence attached: "a chain hop the placement made (`pcb_place._REACH`): its corridor
            # is checked; a longer one is the router's." `check_chains` stops *measuring* there; this
            # stops *writing* there, and for the same reason `hop.HOP_MM` exists — a fixed ~7-shape
            # enumeration between two pad exits draws a short local link well and a long one badly,
            # whoever asked for it. A declaration says which order the author wants; it does not give
            # this pattern a search it does not have.
            #
            # Measured on ds2, three configurations, each a real `pcbc build` (S6):
            #
            # | what the chain wrote | outcome |
            # |---|---|---|
            # | everything (51 seg, 98.6 mm) | `GND` and `VSS` unrouted |
            # | implicit bounded, declared free (18 seg, 30.8 mm) | `GND` and `VSS` unrouted |
            # | declared only (5 seg, 14.4 mm) | copper verified, then the `GND` plane in 2 islands |
            # | bounded, declared included (0 seg) | builds, exactly as with the pattern off |
            #
            # None of the copper was near what broke. The 14.4 mm declared link sits 3.4 mm from the
            # tap that failed and on the opposite layer from the plane that split: locked copper
            # moves KRT, and KRT's own copper closes the escape two stages later. That is the cost a
            # long link carries and the reason the bound is not a tidy round number but this one.
            return (grown, joins, (i, a, b, None, "not local"), tried)
        got, n, clash, why = _link(ctx, spec, a, b, mine, head, spec.stations[i + 2 :])
        tried += n
        if got is None:
            return (grown, joins, (i, a, b, clash, why), tried)
        head, grown = got
        joins.append((a.owner, b.owner))
    return (grown, joins, None, tried)


# --- one chain ------------------------------------------------------------------------------------


def run(ctx: PatternCtx, spec: ChainSpec) -> PatternResult:
    """One chain, in its own order. The links it can make, and the first one it cannot, named."""
    if spec.skip:
        # A skip is a printed note and never a refusal: `Refusal` counts are exact per board (C.6),
        # and a chain pcbc declined to consider is not a chain pcbc failed to route.
        return PatternResult(reason=REASON, net=spec.net, notes=(f"style: chain {spec.net}: skipped, {spec.skip} ({spec.where})",))
    if not spec.layer:
        return PatternResult(reason=REASON, net=spec.net, refusal=_refuse_layer(ctx, spec))
    pieces, joins, failed, tried = _run(ctx, spec)
    mm = sum(p.mm for p in pieces)
    made = len(joins)
    common = {
        "joins": tuple(joins),
        "links": (made, len(spec.stations) - 1),
        "coverage": round(mm / spec.airwire, 4) if spec.airwire > 0 else 0.0,
        "tried": tried,
        "candidate": f"chain of {len(spec.stations)} stations on {spec.layer}" if pieces else "",
    }
    if failed is None:
        return PatternResult(reason=REASON, net=spec.net, pieces=pieces, **common)
    return PatternResult(reason=REASON, net=spec.net, pieces=pieces, refusal=_refuse(ctx, spec, failed, tried), **common)


# --- the refusal ----------------------------------------------------------------------------------


def _other(ctx: PatternCtx, layer: str) -> str:
    layers = [L for L in ctx.scene.layers if L != layer]
    return layers[-1] if layers else layer


def _toward(dx: float, dy: float) -> str:
    """`route_checks._quantise`'s answer and the vocabulary `Place(toward=)` accepts, in KiCad's
    y-down frame."""
    if abs(dx) >= abs(dy):
        return "right" if dx >= 0 else "left"
    return "down" if dy >= 0 else "up"


def _chain_call(spec: ChainSpec) -> str:
    """The `Chain(...)` line this chain is, or would be — the edit the move ends in."""
    return f'Chain("{spec.net}", ' + ", ".join(f'"{label}"' for label in spec.labels) + ")"


def _refuse_layer(ctx: PatternCtx, spec: ChainSpec) -> Refusal:
    """The stations share no copper layer, and R2 puts no via on a chain (B.2: a chain is a feed
    order on one layer).

    Soft, like every other chain refusal (C.6). A net whose own `NetReq(layers=)` leaves its stations
    without a shared layer is one of the two cases `Refusal.hard` is *for* — KRT cannot hold a
    single-layer constraint it is not given — but this refusal is not that case: it fires when the
    stations' **pads** share no layer, which is a placement fact KRT is free to route around with a
    via. Nothing here sets `hard`."""
    where = ", ".join(f"{label} on {'/'.join(sorted(t.layers)) or 'no copper layer'}" for t, label in zip(spec.stations[:3], spec.labels[:3]))
    return Refusal(
        pattern=REASON,
        net=spec.net,
        what=f"{len(spec.stations)} stops",
        clash=None,
        rule="no layer",
        hard=False,
        move="chain "
        + move_line(
            spec.net,
            f"{len(spec.stations)} stops in feed order",
            "one run on one copper layer",
            f"its stops share no copper layer ({where}), and R2 puts no via on a chain — a chain is a feed order on one layer (rule: no layer)",
            f'Moves: NetReq("{spec.net}", layers=["{ctx.scene.layers[0]}"]) names the layer the feed runs on.',
            sep="\n  ",
        ),
    )


def _blocker(spec: ChainSpec, clash: Clash | None, why: str, i: int) -> str:
    """The one line that says what stopped link `i`, with its own number and the rule that decided it."""
    if clash is not None and clash.rule == "no_stub":
        return (
            f"{clash.item.label()} at {clash.at[0]:g},{clash.at[1]:g} is a later stop on this chain, and every "
            f"candidate leaves it {clash.have:.3f} mm of the {clash.need:.3f} mm a chain keeps off a stop it has not "
            f"reached yet — so this link would run past it and the feed would come back, which is the stub B.2 forbids: "
            f"a chain goes through its pads in order (rule: {clash.rule})"
        )
    if clash is not None:
        return (
            f"{clash.item.label()} at {clash.at[0]:g},{clash.at[1]:g} leaves {clash.have:.3f} mm "
            f"of the {clash.need:.3f} mm {clash.why} needs (rule: {clash.rule})"
        )
    if why == "lane":
        return "every candidate runs along a fanout lane, and a lane may be crossed, not run along and not sat in (rule: lane)"
    if why == "not local":
        a, b = spec.labels[i], spec.labels[i + 1]
        return (
            f"{a} and {b} are {spec.spans[i]:.2f} mm apart, past the {CHAIN_HOP_MM:g} mm a chain hop the placement "
            f"made reaches — this pattern draws a short local link well and a long one badly, so the run is the "
            f"router's and the order is read back off the finished board instead (rule: not local)"
        )
    return (
        f"no link between these two stops is the shape of copper pcbc writes — every candidate doubled back on itself "
        f"or left a leg under the segment minimum (rule: {why})"
    )


def _reordered(spec: ChainSpec, i: int, who: str) -> str:
    """The `Chain()` line with `who`'s stop moved to just after stop `i` — the edit an R-X4 refusal
    ends in, because what is wrong there is the order and not the placement."""
    labels = list(spec.labels)
    k = next((n for n, label in enumerate(labels) if label.split(".")[0] == who), -1)
    if k < 0:
        return _chain_call(spec)
    labels.insert(i + 1, labels.pop(k))
    return f'Chain("{spec.net}", ' + ", ".join(f'"{label}"' for label in labels) + ")"


def _moves(ctx: PatternCtx, spec: ChainSpec, i: int, a: Terminal, b: Terminal, clash: Clash | None) -> str:
    """Two edits, both real, and which two depends on what stopped the link.

    - **R-X4** — the thing in the way is one of the chain's **own later stops**, so the placement is
      not the fault and moving a part is the wrong advice: the order is. The move is the `Chain()`
      line with that stop put where the placement already has it, which is an edit the author can
      paste. For an implicit chain it is the same line, offered as the `Chain()` to write — pcbc read
      the order off `_principal_order` and the geometry disagrees, and saying so is more use than
      saying the pads are badly placed.
    - **anything else** — the thing in the way is a third part sitting in the corridor between two
      pads the author named, so **that** is what moves. `hop._moves` moves a terminal instead,
      because a hop chooses its own corridor and either end can step aside; a chain's corridor is
      given. `route_checks.check_chains` already says exactly this for the placement-time version of
      the same finding — "`{who}` lies in the corridor between ... ; move `{who}`" — so the router's
      move and the checker's name the same part. The direction is computed: away from the line
      between the two stops, which is the way out of the corridor rather than away from one end of it.
    - **no blocker at all** — there is no third part to name, so the move is the near stop pointed at
      the far one: an exit facing the wrong way is what "no candidate" means.

    The second edit is always the chain itself, because a chain is the one refusal where "this cannot
    be done" may be the right answer: the author asked for a feed through a pad the placement cannot
    feed through, and dropping that stop and decoupling it locally is a real board change. That is
    B.2's own second move, and it names a stop of the chain — never the blocker, which is not in it.
    """
    drop = a.ref if a.ref != spec.labels[0].split(".")[0] else b.ref
    if clash is not None and clash.rule == "no_stub":
        who = clash.item.owner.split(".")[0]
        line = _reordered(spec, i, who)
        if spec.declared:
            return f"Moves: {line} puts {clash.item.owner} where the placement already has it, which is the order this chain can be fed in."
        return (
            f"Moves: {line} declares the order the placement gives, in place of the one pcbc read "
            f"off the pads' own principal axis."
        )
    if clash is not None and clash.item.owner:
        who = clash.item.owner.split(".")[0]
        mid = ((anchor(a)[0] + anchor(b)[0]) / 2.0, (anchor(a)[1] + anchor(b)[1]) / 2.0)
        toward = _toward(clash.at[0] - mid[0], clash.at[1] - mid[1])
        first = f'Place("{who}", toward="{toward}") takes it out of the corridor between {spec.labels[i]} and {spec.labels[i + 1]}'
    else:
        toward = _toward(anchor(b)[0] - anchor(a)[0], anchor(b)[1] - anchor(a)[1])
        first = f'Place("{a.ref}", toward="{toward}") turns its exit toward {spec.labels[i + 1]}'
    if spec.declared:
        return (
            f"Moves: {first}; or {_chain_call(spec)} is asking for an order this placement cannot give "
            f"— drop {drop} from the chain and feed it locally instead."
        )
    return (
        f"Moves: {first}; or {_chain_call(spec)} declares the order you want, in place of the one the pads' own line "
        f'gives; or NetReq("{spec.net}", layers=["{_other(ctx, spec.layer)}"]) takes the feed to the other side.'
    )


def _refuse(ctx: PatternCtx, spec: ChainSpec, failed: tuple, tried: int) -> Refusal:
    """B.2's refusal: the failing link named by its number and its two stops, a real blocker with its
    own number, what was emitted before it, and a `board.py` edit.

    **Soft, declared or not** (C.6). Until S6's review a declared chain refused *hard* — aborting the
    build before KRT ran — on the premise that "KRT would connect the same pads by branching and the
    branch is exactly what the order was written to forbid". The premise was never checked and the
    one board that could check it says otherwise: on ds2's routed `VDDA`, KRT runs a straight
    45-degree trace across `C4.1`'s pad with 1.1185 mm of centre line inside the pad's own copper,
    and the declared order is honoured in the finished board. An implicit chain was already soft for
    `hop._refuse`'s reason — nothing was declared, so a fall-through to KRT disobeys nothing and
    costs a route pcbc would have drawn straighter.

    What replaced the hard refusal is the check it was standing in for. `route_verify.chain_order`
    reads the routed board and fails the build when a **declared** order really is violated there
    (`build._chain_gate`), so the stop happens on a measurement of KRT rather than on a prediction
    about it — "KiCad is the arbiter" applied to the one question the arbiter's own netlist gate
    cannot ask, because connectivity is order-blind.
    """
    i, a, b, clash, why = failed
    la, lb = spec.labels[i], spec.labels[i + 1]
    n = len(spec.stations) - 1
    head = _blocker(spec, clash, why, i)
    if i:
        head += f"\n  Links 1..{i} of {n} are down, from {spec.labels[0]} to {la}; nothing after link {i + 1} was tried, because a chain is an order and pcbc does not re-order one to close it"
    else:
        head += f"\n  Nothing of this chain is down: link 1 of {n} is its first, and a chain is an order pcbc does not re-order to close"
    head += f"\n  Tried {tried} candidates of at most {n} x {MAX_PER_LINK}"
    return Refusal(
        pattern=REASON,
        net=spec.net,
        what=f"{la}->{lb}",
        clash=clash,
        rule=(clash.rule if clash is not None else why),
        hard=False,
        move="chain "
        + move_line(
            spec.net,
            f"link {i + 1} of {n}, {la} at ({anchor(a)[0]:g},{anchor(a)[1]:g})",
            f"{lb} at ({anchor(b)[0]:g},{anchor(b)[1]:g}) on {spec.layer} at {spec.width:g} mm ({spec.where})",
            head,
            _moves(ctx, spec, i, a, b, clash),
            sep="\n  ",
        ),
    )


__all__ = [
    "ELBOWS",
    "MAX_PER_LINK",
    "REASON",
    "ChainSpec",
    "elbows",
    "link_shapes",
    "run",
    "specs",
    "stub_clash",
    "stub_need",
]
