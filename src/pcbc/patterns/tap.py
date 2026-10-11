"""`tap` — a pad to its plane or pour (`docs/r2-design.md` B.3).

Every **SMD** pad whose net has a plane or a pour gets its own via, straight out of the pad on the
axis, into the fanout geometry `fanout.py` already uses so the two agree. Through-hole pads are
skipped and the report says why: the barrel already reaches every copper layer, so the zone connects
them and a tap would be a second hole for nothing. So is a pad that already reaches the plane
through copper that is down — a closed row's escape via is a tap that has already happened.

**One via per pad.** Not per component, not clustered: it is the electrical answer a four-layer
board exists for, and the via *count* objection is answered by splitting the copper bar (D.4) rather
than by holding forty-seven ground pads off the ground plane. Clustering is a named non-goal (H.2).

Three things decide this pattern, and all three are the fab's, not the router's
(`docs/copper-plan.md`):

- a via whose copper sits inside a pad wicks solder and `fab.via_in_pad_blockers` refuses the board,
  so a tap clears every pad of its own net by `via_to_same_net_smd_pad` and every foreign pad by
  A.4 rule 1;
- two holes closer than the fab's hole-to-hole is an **error** in pcbc's project, so A.4 rule 3 is
  asked of every candidate against every other hole on the board, this pattern's own earlier taps
  included (the stage adds each one to the scene before the next pad runs);
- a pour judges nothing until it is filled, and no pour is filled while the route stage runs (KiCad
  fills the emitted board), so on an outer-layer pour a candidate the fill would not reach is refused
  here rather than discovered by the gate as an unconnected item (`route_verify.pour_raster`).
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from ..moves import move_line
from ..route_emit import Piece, seg_piece, via_piece
from ..route_geom import EPS_MM, MICRO_MM, Pt, clears, gap, q
from ..route_scene import Clash, Scene, antipad_clash, blocked, pad_exits
from ..sexp import stable_uuid
from ..stackup import via_amps
from . import PatternCtx, PatternResult, Refusal, Terminal, _pad_key, lane_ok, terminals

REASON = "tap"

CONNECTS = True
"""A tap joins its pad to its own net's plane, so the net it claims is the net it connects
(`docs/stitch-plan.md` §2k). See `hop.CONNECTS`."""

TAP_REACH_MM = 1.5
"""How far past its own first site a tap may step outward, in mm.

The first site is `fanout.py`'s distance exactly — half the pad plus the fab's clearance plus half
the via — and the extra steps buy the pads that have to clear something: on the USB-C boards `J1`'s
shell pad needs 1.076 mm to clear its own mounting hole's `hole_to_hole`. A named module constant
with its measurement rather than a derived number, and H.8 records that: when the spine needs a
reach of its own, "reach" probably becomes `k * fanout_lane(stack, clearance, pitch)` and derives
properly. `docs/r2-measurements.md` S5."""


@dataclass(frozen=True)
class TapSpec:
    """One pad the pattern has something to say about: a tap, or a skip with its reason.

    A skipped pad is a spec rather than an omission because B.3 says the report says *why* a plane
    pad has no via of its own. An omission says nothing, and "there is no via on J1.1" is exactly
    the kind of silence the gate later turns into an unconnected item nobody expected.
    """

    pad: Terminal
    layer: str  # the outer copper layer this pad sits on
    plane: str  # the plane or pour layer it is being welded to
    via: tuple[float, float]  # (diameter, drill)
    why: str  # why that via size, with its arithmetic, for the report and the refusal
    skip: str = ""  # "" for a tap; otherwise the reason there is none
    count: int = 1  # vias this pad's joint needs: a station of a Path on the pour draws through it (PR 8)


# --- the via a tap gets ---------------------------------------------------------------------------


def tap_via(scene: Scene, cs, net: str, pads: int, pad: str | None = None) -> tuple[tuple[float, float], str, float]:
    """The smallest via whose current rating covers one pad's share of the net's current, capped at
    the net's class via. Returns ((diameter, drill), why, share).

    B.0's rule was "a branch takes the fab's standard via, full stop", and H.1 records it as the
    decision its author was least sure of. This is the answer the owner's stand-in gave, and it is
    the same arithmetic R1 already compiled: `Constraint.current.amps` is what the net carries,
    `Constraint.via.amps` is what one class via carries at the net's own temperature rise, and
    `via_amps` is the curve both come from (IPC-2221B eq. 6-2 on the plated barrel).

    A tap carries one pad's share, not the rail: a decoupling cap's ripple, not the 1 A the plane
    carries. Measured on node, the busiest board here: `GND` asks 1 A across 47 SMD pads, so a pad's
    share is 0.021 A against the 0.527 A one 0.2 mm barrel carries at 10 C — a factor of 25. The cap
    matters because it is what keeps node routable: at the Power class's 0.8 mm ring two taps need
    1.0 mm between centres against 0.7 mm for the stackup via, and node's pads are not 1.0 mm apart.
    """
    c = cs.by_net(net)
    stack = scene.stack
    # `docs/physics-subjects.md` PR 3: a net declares no current, so a tap's share is the current of the
    # path link that ends at the tapped pad (`pad`, a pad id), and 0 where none does.
    lk = None
    if pad is not None and c is not None:
        lk = next((x for p in c.paths for x in p.links if x.current is not None and x.amps_written and not x.join and pad in x.b), None)
    amps = float(lk.current.amps) if lk is not None else 0.0
    share = round(amps, 4)
    cap = (c.via.diameter_mm, c.via.drill_mm) if c is not None else (stack.via_diameter, stack.via_drill)
    dt = lk.current.temp_rise_c if lk is not None else 10.0
    sizes: list[tuple[float, float]] = []
    for size in ((stack.via_diameter, stack.via_drill), cap):
        if size[1] <= cap[1] + 1e-9 and size not in sizes:
            sizes.append(size)
    sizes.sort(key=lambda s: (s[1], s[0]))
    for dia, drill in sizes:
        one = via_amps(drill, stack.via_plating_mm, dt)
        if one + 1e-9 >= share:
            if (dia, drill) == (stack.via_diameter, stack.via_drill):
                why = (
                    f"the fab's standard via: {one:g} A carries the {share:g} A {lk.source}'s link {lk.key} brings to this pad"
                    if lk is not None
                    else f"the fab's standard via: no Path declares a current into this pad of {net}, and {one:g} A is what it carries"
                )
            else:
                why = f"the {c.class_name} class via: this pad's {share:g} A needs more than the fab's standard {stack.via_drill:g} mm barrel"
            return ((dia, drill), why, share)
    # Past the cap: one class via does not carry one pad's share. Nothing on these five boards is
    # anywhere near it (the margin is 25x on node), and the honest answer is the cap plus a note
    # rather than a via the net's own class does not allow.
    one = via_amps(cap[1], stack.via_plating_mm, dt)
    return (cap, f"the {c.class_name if c is not None else 'net'} class via, which is the cap: it carries {one:g} A against this pad's {share:g} A", share)


# --- which pads this pattern has something to say about --------------------------------------------


def _outer(scene: Scene, t: Terminal) -> str:
    """The outer copper layer this pad sits on, in the board's own layer order; "" when it is on
    none (an inner-layer pad, which R2 never sees, or a pad with no copper at all)."""
    for layer in scene.layers:
        if layer in ("F.Cu", "B.Cu") and layer in t.layers:
            return layer
    return ""


def _drilled(scene: Scene, t: Terminal) -> bool:
    """Is this pad through-hole? A pad's drill is held by one of its items (`pad_items` gives the
    hole to the first primitive), so the question is asked of the owner and not of one shape."""
    return any(it.hole is not None for it in scene.items if it.kind == "pad" and it.owner == t.owner)


def _welded(scene: Scene, net: str, plane: str) -> frozenset[str]:
    """The pads of `net` already connected to `plane` by copper that is down.

    `route_scene.components` asks which pads reach **each other**; this asks which reach the
    **plane**, which is the other half of the same union-find and the half a tap needs. The geometry
    core is S2/S3's and frozen this slice, so the ten lines live here with the reason rather than
    growing a second public question onto a module this slice may not touch.

    It matters on the two-layer USB boards, where a closed row's escape via already spans F.Cu to
    B.Cu: that pad is welded to the back pour the moment the pour is filled, and a tap beside it
    would be a second hole for a connection that exists.
    """
    mine = [it for it in scene.items if it.net == net and it.kind in ("pad", "track", "via")]
    parent = list(range(len(mine)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i: int, j: int) -> None:
        a, b = find(i), find(j)
        if a != b:
            parent[max(a, b)] = min(a, b)

    for i in range(len(mine)):
        a = mine[i]
        for j in range(i + 1, len(mine)):
            b = mine[j]
            if (a.layers & b.layers) and a.copper is not None and b.copper is not None and gap(a.copper, b.copper) <= 0.0:
                union(i, j)
    reach = {find(i) for i, it in enumerate(mine) if it.kind == "via" and plane in it.layers}
    return frozenset(it.owner for i, it in enumerate(mine) if it.kind == "pad" and find(i) in reach)


def joints_of(cs, net: str) -> dict:
    """`{pad id: Joint}` for every station of a Path on `net`'s pour (`constraints.Joint`, PR 8)."""
    c = cs.by_net(net) if cs is not None else None
    if c is None:
        return {}
    return {pad: j for p in c.paths for j in p.joints for pad in j.pads}


def specs(ctx: PatternCtx) -> tuple[TapSpec, ...]:
    """Every plane pad, in `sorted(ref, pad_num)` — B.0's order for a pad-driven pattern.

    One order for the whole pattern and not one per net: the taps of two nets sit beside each other
    on the same board and each is the next one's obstacle, so "GND's pads and then 3V3's" and "the
    board's pads in ref order" are different boards. The second is the one a reader can predict.
    """
    scene = ctx.scene
    out: list[TapSpec] = []
    for net in sorted(scene.plane_of):
        plane = scene.plane_of[net]
        welded = _welded(scene, net, plane)
        rows: list[tuple[Terminal, str, str]] = []
        for t in terminals(scene, net):
            layer = _outer(scene, t)
            if _drilled(scene, t):
                rows.append((t, layer, f"a through-hole pad: its barrel already reaches {plane}, so the zone connects it and a tap would be a second hole for nothing"))
            elif not layer:
                rows.append((t, layer, "no outer copper layer to leave from"))
            elif t.owner in welded:
                rows.append((t, layer, f"already welded to {plane} by copper that is down (an escape via is a tap that has happened)"))
            else:
                rows.append((t, layer, ""))
        taps = sum(1 for _t, _l, skip in rows if not skip)
        via, why, _share = tap_via(scene, ctx.cs, net, taps)
        joints = joints_of(ctx.cs, net)
        for t, layer, skip in rows:
            j = joints.get(t.owner)
            if j is not None and not skip and j.barrel_mm is None:
                # docs/physics-subjects.md PR 8: a station of a Path on this pour draws through this pad's joint.
                from ..paths import joint_text

                out.append(TapSpec(pad=t, layer=layer, plane=plane, via=j.via, why=f"the joint of {joint_text(j)}", skip=skip, count=j.vias))
                continue
            out.append(TapSpec(pad=t, layer=layer, plane=plane, via=via, why=why, skip=skip))
    return tuple(sorted(out, key=lambda s: _pad_key(s.pad.owner)))


# --- the candidates -------------------------------------------------------------------------------


def _width(spec: TapSpec, ctx: PatternCtx, horizontal: bool) -> float:
    """`fanout.py`'s own neck: the class width, narrowed **to** the pad's across dimension and never
    below the fab's `track_min`, so the stub out of a 0402 pad is the same copper the escape beside
    it would have been."""
    c = ctx.cs.by_net(spec.pad.net)
    width = float(c.width_mm.value) if c is not None else ctx.scene.stack.track_min
    box = spec.pad.item.box()
    across = (box[3] - box[1]) if horizontal else (box[2] - box[0])
    return round(max(min(width, across), ctx.scene.stack.track_min), 4)


def _sites(ctx: PatternCtx, spec: TapSpec) -> tuple[tuple[str, tuple[int, int], int, Pt, float], ...]:
    """B.3's candidate list: `4 x (1 + floor(TAP_REACH_MM / grid))` sites, direction first.

    **Outward first, distance second**, and that order is not cosmetic. With distance first the first
    candidate for a passive's pad sits between the passive's own two pads — `C_VBUS.2` blocked by
    `C_VBUS.1 [VBUS]`, `R_CC1.2` by `R_CC1.1 [CC1]` — so the cheap site is the one that cannot work.
    The side order is `route_scene.pad_exits`' own (A.7): the fanout escape side when the footprint
    declares one, then outward by `(pad centre - footprint centre) . dir`, ties right, left, down, up.

    The distance out is `fanout.py`'s, exactly: half the pad, the fab's clearance, half the via. The
    stub keeps the pad centre's other coordinate **exactly** — the across coordinate is never snapped
    to the router's grid, which is what tilted every escape stub before S1 (A.5) — so every tap is
    axis-aligned by construction rather than by rounding.
    """
    scene = ctx.scene
    pad = spec.pad.item
    cx, cy = pad.at()
    box = pad.box()
    dia = spec.via[0]
    steps = int(math.floor(TAP_REACH_MM / scene.grid + 1e-9))
    out: list[tuple[str, tuple[int, int], int, Pt, float]] = []
    for ex in pad_exits(scene, pad, _width(spec, ctx, True), spec.layer, check=False):
        dx, dy = ex.dir
        half = (box[2] - box[0]) / 2.0 if dx else (box[3] - box[1]) / 2.0
        w = _width(spec, ctx, dx != 0)
        # `+ EPS_MM` for `route_scene.pad_exits`' own reason, in its own words: "a stub placed at
        # exactly `need` is copper this module's own judge refuses". `clears` demands `need + EPS_MM`
        # and this pattern is the one that placed copper at exactly `need`, exempting the pad it
        # welds from the test — 88 of 105 taps sat on the fab floor to the last bit (0.0889 mm on
        # node, 0.1270 elsewhere). 0.1 um out, and the docstring's claim survives the exemption
        # being removed (`docs/r2-measurements.md` S5r, finding 8).
        d0 = half + scene.stack.clearance_min + EPS_MM + dia / 2.0
        for k in range(steps + 1):
            d = d0 + k * scene.grid
            out.append((ex.side, (dx, dy), k, (q(cx + dx * d), q(cy + dy * d)), w))
    return tuple(out)


def anchor(spec: TapSpec) -> Pt:
    """Where the stub starts: the centre of the **primitive** it leaves from, not the centre of
    everything the pad draws.

    They are the same point for the 363 pads of these five boards that draw one shape, and they are
    not the same for the two that do not: a USB-C shield pad draws several `gr_poly` primitives, and
    node's `U1` writes its QFN thermal pad as nine separate pads all numbered 49. `Terminal.at` is
    the centre of the whole owner, which for those two is a point that may sit between the shapes —
    so a stub from there to a site measured off one shape is neither axis-aligned nor necessarily on
    copper. Measured: it emitted `(10.275,10.8139) -> (12.25,7.85)` on node, which is exactly the
    diagonal the self-check refused (D.1 item 2).
    """
    return spec.pad.item.at()


def _pieces(ctx: PatternCtx, spec: TapSpec, at: Pt, w: float, n: int = 0) -> tuple[Piece, ...]:
    t = spec.pad
    uid = (ctx.board, "pcbc", REASON, t.net, t.owner) + ((str(n),) if n else ())
    return (
        seg_piece(t.net, REASON, spec.layer, anchor(spec), at, w, owner=t.owner, uuid=stable_uuid(*uid, "stub")),
        via_piece(t.net, REASON, at, spec.via[0], spec.via[1], owner=t.owner, uuid=stable_uuid(*uid, "via")),
    )


_QUERY_PAD_MM = 3.0
"""How far past the via the bucket index is asked to look, in mm. `route_scene._MAX_NEED`'s number
and its reason: every clearance these boards compile is 0.0889 to 0.5 and the widest `keep_clear_mm`
any example writes is 3, so this covers the requirement with room. The index answer is a superset
either way and `clears` does the exact test on what comes back."""


def _in_a_pad(scene: Scene, spec: TapSpec, at: Pt) -> Clash | None:
    """A.4 rule 1 does not ask this one, and the fab does: a via inside a pad of its **own** net.

    Rule 1 skips a same-net pair, correctly — a via in its own net's copper is a connection, and
    without that skip a tap could not be emitted at all. But `fab.via_in_pad_blockers` refuses a
    board where a via's copper sits inside a passive's pad whatever net it is on, because it wicks
    the joint. So the tap asks it here, of every pad of its own net except the
    one primitive it is welding — which it clears by `clearance_min` by construction, since that
    term is in the distance out.

    The exemption is the **primitive**, not the owner, and that matters on exactly the pads that draw
    more than one: node's `U1` writes its QFN thermal pad as nine blocks all numbered 49, and
    exempting the owner would let a tap sit in the gap between two of them and call it its own pad.
    Measured on node: the tap for `U1.49` sits 0.0889 mm below the block it leaves and 1.10 mm from
    the nearest other block, so nothing moves — the hole was only ever theoretical, and it is closed.
    """
    need = scene.table.via_to_same_net_smd_pad()
    ring = scene.item_of(_pieces_via(spec, at)).copper
    if ring is None:
        return None
    box = (at[0], at[1], at[0], at[1])
    # Every copper layer, not the pad's: a tap is a through via, so a same-net pad on the far side of
    # the board is as much under it as one beside it.
    seen: set[int] = set()
    for layer in scene.layers:
        for i in scene.query(layer, box, need + spec.via[0] + _QUERY_PAD_MM):
            if i in seen:
                continue
            seen.add(i)
            it = scene.items[i]
            if it.kind != "pad" or it.net != spec.pad.net or it.id == spec.pad.item.id or it.copper is None:
                continue
            if not clears(ring, it.copper, need):
                return Clash(it, gap(ring, it.copper), need, it.at(), "via_in_pad", "stackup.clearance_min (same-net pad)")
    return None


def _pieces_via(spec: TapSpec, at: Pt) -> Piece:
    return via_piece(spec.pad.net, REASON, at, spec.via[0], spec.via[1], owner=spec.pad.owner)


def run(ctx: PatternCtx, spec: TapSpec) -> PatternResult:
    """One pad. The first site that clears is the answer; nothing else is consulted. A pad whose joint needs
    more than one via (`TapSpec.count`, a station's draw on the pour, PR 8) takes the next sites that clear,
    each judged against the vias already chosen (on a fork of the scene, so a refused pad writes nothing)."""
    t = spec.pad
    scene = ctx.scene
    if spec.skip:
        return PatternResult(reason=REASON, net=t.net, notes=(f"style: tap {t.net}: {t.owner} has no via of its own - {spec.skip}",))
    mine = frozenset(it.id for it in scene.items if it.owner == t.owner)
    sites = _sites(ctx, spec)
    worst: tuple[float, Clash] | None = None
    seen: list[Clash] = []
    lane_why = ""
    tried = 0
    raster = _raster(ctx, spec)
    unreached = 0
    work = scene if spec.count <= 1 else scene.fork()
    got: list[Piece] = []
    chosen: list[str] = []
    dirs: list[tuple[int, int]] = []  # the exit direction of each chosen via, in `chosen`'s order
    notes: tuple[str, ...] = ()
    for side, d, k, at, w in sites:
        if d in dirs:
            # One via per exit side (refutation physics r1, `pour_two`): every stub starts at the pad's centre, so
            # a second site further out on the same side lays its stub over the first (the self-check's "turn
            # sharper than 135 degrees" back along itself) and the shared copper would carry both vias' current.
            continue
        if not _legal_star(dirs + [d]) and len(dirs) + 1 >= spec.count:
            continue  # two stubs at a right angle out of one pad centre are a 90 degree turn in the copper
        pieces = _pieces(ctx, spec, at, w, len(chosen))
        if pieces[0].mm < MICRO_MM:
            continue  # a stub KiCad's own `track_segment_length` would count; the next site is longer
        tried += 1
        clash = (
            _in_a_pad(work, spec, at)
            or blocked(work, pieces, t.net, ignore=mine)
            # The plane this via does NOT join is the one it can quietly cut: a row of taps at a
            # footprint's own pitch merges its antipads into a slot (finding 10). `_sites` offers 16
            # distances on each of four sides, so a row that cannot be tapped one-per-pad in line can
            # still be tapped by stepping alternate pads out until the neck holds.
            or antipad_clash(work, at, spec.via[0], t.net, ignore=mine)
        )
        if clash is not None:
            seen.append(clash)
            if worst is None or (clash.need - clash.have) > worst[0]:
                worst = (clash.need - clash.have, clash)
            continue
        fits, why = lane_ok(work, pieces, (t.ref,))
        if not fits:
            lane_why = lane_why or why
            continue
        if raster is not None and not raster.reaches(at, spec.via[0] / 2.0):
            # The pour is written after the signals on a two-layer board, so this tap is placed
            # before the copper it has to live in exists. A tap the fill will not reach connects
            # nothing, and an island the pour cannot reach is worse than no tap at all (D.5).
            unreached += 1
            continue
        got.extend(pieces)
        chosen.append(f"{side}+{k} {spec.via[0]:g}/{spec.via[1]:g}")
        dirs.append(d)
        notes = notes or _neck_note(ctx, spec, w)
        if len(chosen) >= spec.count:
            break
        work.add(work.item_of(p) for p in pieces)
    while len(dirs) > 1 and not _legal_star(dirs):
        # Short of the count with a right-angle pair left over: keep the vias whose stubs make legal copper; the
        # gate states the joint's shortfall on the emitted board (E23).
        dirs.pop()
        chosen.pop()
        del got[-2:]
    if got:
        if len(chosen) < spec.count:
            # The joint is short of what the draw needs; the gate states it on the emitted board (E23).
            notes = notes + (f"style: tap {t.net}: {t.owner} has {len(chosen)} of the {spec.count} vias {spec.why}",)
        return PatternResult(
            reason=REASON,
            net=t.net,
            pieces=tuple(got),
            joins=((t.owner, f"{t.net} plane on {spec.plane}"),),
            candidate=", ".join(chosen),
            tried=tried,
            notes=notes,
        )
    return PatternResult(
        reason=REASON,
        net=t.net,
        refusal=_refuse(ctx, spec, worst[1] if worst else None, seen, len(sites), tried, lane_why, unreached),
        tried=tried,
    )


def _legal_star(dirs: list[tuple[int, int]]) -> bool:
    """Do stubs leaving one pad centre in `dirs` make copper `route_verify.verify_copper` accepts? One stub, or
    three or more (the centre is then a junction, not a corner), or two in opposite directions (one straight
    line); two at a right angle are a 90 degree corner (`route_geom.turn_ok`)."""
    if len(dirs) != 2:
        return True
    (ax, ay), (bx, by) = dirs
    return (ax, ay) == (-bx, -by)


def _neck_note(ctx: PatternCtx, spec: TapSpec, w: float) -> tuple[str, ...]:
    """A `style:` line when the stub is narrower than the class the net declares.

    `_width` narrows to the pad's across dimension on purpose — it is `fanout.py`'s own neck, and a
    0402 pad is 0.5 mm wide — but the class's `width_*` rule has no such exemption, so KiCad counts
    the stub as a soft hit like any other necked track. `test_examples_fab.py` used to say the
    opposite ("no tap stub is in these counts") and four stubs were in them: buck's `R_FB_BOT.2` at
    0.64 mm against `width_power`'s 0.781, and node's `U4.2`, `U4.6` and `U4.7` at 0.364 against 0.4
    (finding 14). Counted here so a board where the neck matters electrically is a line pcbc wrote
    rather than a DRC warning nobody attributed.
    """
    c = ctx.cs.by_net(spec.pad.net)
    if c is None or w >= float(c.width_mm.value) - 1e-9:
        return ()
    return (f"style: tap {spec.pad.net}: {spec.pad.owner}'s stub necks to {w:g} mm, the pad's across dimension, against the {c.class_name} class's {float(c.width_mm.value):g} mm",)


def _raster(ctx: PatternCtx, spec: TapSpec):
    """The pour's predicted reach on an outer layer, or None for an inner plane.

    No pour is filled while the route stage runs (KiCad fills the emitted board). An inner plane has no
    signal copper on its layer (`route_native.route_layers`) and `route_verify.plane_islands` judges it
    after the fill — the arbiter's own answer. An outer pour shares its layer with pads and tracks, so
    the raster is the only thing between a tap and an island.
    """
    from ..route_verify import pour_raster

    if spec.plane not in ("F.Cu", "B.Cu"):
        return None
    return pour_raster(ctx.scene, spec.pad.net, spec.plane)


# --- the refusal ----------------------------------------------------------------------------------


def _other(ctx: PatternCtx, layer: str) -> str:
    layers = [L for L in ctx.scene.layers if L != layer]
    return layers[-1] if layers else layer


def _refuse(ctx: PatternCtx, spec: TapSpec, clash: Clash | None, seen: list[Clash], sites: int, tried: int, lane: str, unreached: int) -> Refusal:
    """B.3's refusal: **soft**, and the sentence prices it.

    Soft because the fall-through is exact: a refused pad stays open on its net, and the router links
    it (`route_native.route_nets`: its terminals are the net's pads and every via already on it,
    plane taps included). So a refused tap never costs the connection — it costs the via the tap
    would have placed beside the pad, and the honest price is written here (finding 13).

    It lists the blockers **worst first** — B.3 asks for that, because the second one is usually the
    reason the first cannot simply be moved — names the rule of A.4 that decided each, counts the
    sites it tried, and ends in a `board.py` edit.
    """
    t = spec.pad
    by_pair: dict[tuple[str, int], Clash] = {}
    for c in seen:
        key = (c.rule, c.item.id)
        if key not in by_pair or (c.need - c.have) > (by_pair[key].need - by_pair[key].have):
            by_pair[key] = c  # one line per blocker, and it is that blocker's worst site
    worst = sorted(by_pair.values(), key=lambda c: (-(c.need - c.have), c.item.id, c.rule))[:2]
    if worst:
        blockers = "worst first: " + "; ".join(
            f"{c.item.label()} at {c.at[0]:g},{c.at[1]:g} leaves {c.have:.3f} mm of the {c.need:.3f} mm {c.why} needs (rule: {c.rule})" for c in worst
        )
    elif unreached:
        blockers = f"every site clears, and the pour does not reach {unreached} of them (rule: pour_reach)"
    elif lane:
        blockers = f"every site {lane}, and a fanout lane may be crossed, not run along and not sat in (rule: lane)"
    else:
        blockers = f"no site is even {MICRO_MM:g} mm of copper from the pad (rule: no candidate)"
    blockers += f"\n  Tried {sites} sites (4 sides, {sites // 4} distances {ctx.scene.grid:g} mm apart), {tried} of them copper pcbc writes"
    return Refusal(
        pattern=REASON,
        net=t.net,
        what=t.owner,
        clash=clash,
        rule=clash.rule if clash is not None else ("pour_reach" if unreached else ("lane" if lane else "no candidate")),
        hard=False,
        move="tap "
        + move_line(
            t.net,
            f"{t.owner} at ({t.at[0]:g},{t.at[1]:g})",
            f"the {t.net} plane on {spec.plane} with a {spec.via[0]:g}/{spec.via[1]:g} via ({spec.why})",
            blockers,
            sep="\n  ",
        ),
    )
