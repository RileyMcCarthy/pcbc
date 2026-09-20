"""`hop` — two pads, one layer, no via (`docs/r2-design.md` B.1).

A net with exactly two terminals that share a copper layer, when either the two centres are within
`HOP_MM` or the **straight line** between them is the route. The second clause costs nothing and is
not a loophole: a straight pad-to-pad line is never a detour and never takes a corridor it did not
already need, so there is no reason to hand it to a maze router. It is also the whole of blinky,
whose only multi-pad net is one 21.87 mm airwire — and whose straight line turns out to be blocked by
`D1`'s own other pad, which is what `_allowed_shapes` is about.

The candidates are B.0's: every exit of pad a, against every exit of pad b, against every link
shape, in that nested order, and the first that clears wins. No cost, no backtracking, no rip-up.
A refusal names the worst blocker with its own number, the rule it broke, and a move.
"""

from __future__ import annotations

import math

from ..blocking import move_line
from ..route_geom import MICRO_MM, path_mm
from ..route_scene import blocked, pad_exits
from . import ALL_SHAPES, MAX_LINKS, mitre, PatternCtx, PatternResult, Refusal, Terminal, lane_ok, legs_of, legs_ok, link_candidates, pieces_of, shape_ok, terminals

HOP_MM = 6.0
"""How far apart two pads may be and still be a hop on distance alone. `route.LOCAL_MM` is 5.0; 6.0
catches ds2's `nRESET` at 3.11 mm and node's `CC2` at 4.13 mm with headroom and still refuses ds2's
`ADC_RX` at 10.14 mm, which is the maze router's job."""

DETOUR_MAX = 1.5
"""How far past the straight line between the two pads a hop's copper may run.

B.0 says a pattern never degrades — it does not bend a candidate to make it fit — and the whole claim
of R2 is that pcbc's own structured copper is *better* than what a maze router leaves. A hop that
walks twice the airwire is neither: it is a routing problem wearing a pattern's clothes, and the
honest thing is to say so and let KRT have it. The same argument B.1 makes for its straight-line
clause, applied to the answer instead of to the question.

Measured on the five boards: every hop that fits is at **1.00 to 1.24x** except c3_usb's `LED_A`,
where `D1.2` and `R_LED.2` sit 1.336 mm apart with their two exits 0.08 mm from crossing, so the only
shape that clears goes round the outside at **1.93x** — and taking it moved c3_usb's worst detour on
the copper bar from 1.92 to 2.02, a pinned number, for 1.36 mm of copper. 1.5 sits above every hop
the boards actually want and below that one. `docs/r2-measurements.md` S4."""

REASON = "hop"


def specs(ctx: PatternCtx) -> tuple[str, ...]:
    """Every net this pattern will try, `sorted(net)` — B.0's order for a net-driven pattern."""
    scene = ctx.scene
    out: list[str] = []
    for net in sorted({it.net for it in scene.items if it.kind == "pad" and it.net}):
        if net in scene.plane_of:
            continue  # a plane net is the tap pattern's (B.3), never a hop's
        c = ctx.cs.by_net(net)
        if c is not None and c.pair is not None:
            continue  # differential pairs are R4's, and R2 measures nothing about them
        if c is not None and c.group is not None and c.group.kind == "bus":
            continue  # a declared bus member belongs to the ribbon (B.5)
        if len(terminals(scene, net)) != 2:
            continue
        out.append(net)
    return tuple(out)


def _layer(ctx: PatternCtx, net: str, a: Terminal, b: Terminal) -> str:
    """The layer the hop runs on: the two pads' shared copper layers intersected with the net's
    declared `layers`, in the constraint's order, so a `NetReq(layers=[...])` is honoured and not
    merely avoided."""
    shared = a.layers & b.layers
    c = ctx.cs.by_net(net)
    order = list(c.layers) if c is not None and c.layers else list(ctx.scene.layers)
    for layer in order:
        if layer in shared:
            return layer
    return ""


def _width(ctx: PatternCtx, net: str) -> float:
    c = ctx.cs.by_net(net)
    return float(c.width_mm.value) if c is not None else ctx.scene.stack.track_min


def _neck(width: float, t: Terminal, layer: str, ctx: PatternCtx, horizontal: bool) -> float:
    """`fanout.py`'s own neck: narrow the stub **to** the pad's across dimension, never to twice it,
    and never below the fab's `track_min`. A hop does not degrade a track to make it fit (B.0); this
    is the one width change it makes, and it is the same one the escapes already make."""
    box = t.item.box()
    across = (box[3] - box[1]) if horizontal else (box[2] - box[0])
    return round(max(min(width, across), ctx.scene.stack.track_min), 4)


def run(ctx: PatternCtx, spec: str) -> PatternResult:
    """One net. The first candidate that clears is the answer; nothing else is consulted."""
    net = spec
    scene = ctx.scene
    a, b = terminals(scene, net)
    layer = _layer(ctx, net, a, b)
    if not layer:
        return PatternResult(
            reason=REASON,
            net=net,
            refusal=_refuse(ctx, net, a, b, "", None, "no layer", 0, 0, 0, 0),
        )
    span = math.dist(a.at, b.at)
    width = _width(ctx, net)
    # The exits are asked for at the net's full width, not the necked one: the distance out grows
    # with the width, so the wider question gives a stub at least as far from the pad's neighbours as
    # the copper that will actually be written, and never one that is closer.
    exits_a = _toward_first(pad_exits(scene, a.item, width, layer), a, b)
    exits_b = _toward_first(pad_exits(scene, b.item, width, layer), b, a)
    mine = frozenset(it.id for it in scene.items if it.owner in (a.owner, b.owner))
    built = 0  # every (exit pair, link shape) the enumeration produced
    tried = 0  # of those, the ones whose shape is copper pcbc writes, so the board judged them
    worst: tuple[float, object] | None = None
    links_seen = 0
    lane_why = ""
    best_ratio = 0.0
    allowed, strangers = _allowed_shapes(ctx, net, a, b, span, exits_a, exits_b, layer, mine)
    for ea in exits_a:
        wa = _neck(width, a, layer, ctx, ea.dir[0] != 0)
        for eb in exits_b:
            wb = _neck(width, b, layer, ctx, eb.dir[0] != 0)
            links = link_candidates(ea, eb)
            links_seen = max(links_seen, len(links))
            for name, pts in links:
                if name not in allowed:
                    continue
                built += 1
                full = mitre(_dedupe((a.at, ea.at) + pts[1:-1] + (eb.at, b.at)))
                if full is None or not shape_ok(full):
                    continue
                widths = [wa] + [width] * (len(full) - 3) + [wb]
                pieces = pieces_of(full, widths, net=net, reason=REASON, layer=layer, owner=a.owner, board=ctx.board)
                merged = legs_of(pieces)
                if not legs_ok(merged):
                    # The length judge runs on the merged path, never on the raw one: two collinear
                    # legs that each fall under MICRO_MM are one legal segment once merged.
                    continue
                tried += 1
                ratio = path_mm(merged) / span if span > 0 else 1.0
                if ratio > DETOUR_MAX:
                    best_ratio = ratio if best_ratio == 0.0 else min(best_ratio, ratio)
                    continue
                fits, why = lane_ok(scene, pieces, (a.ref, b.ref))
                if not fits:
                    # A.7: a lane may be crossed and not run along. Refused here rather than in the
                    # self-check so the next candidate gets its turn, which is what the enumeration
                    # is for; `verify_copper` then finds nothing, which is the point of D.1.
                    lane_why = lane_why or why
                    continue
                clash = blocked(scene, pieces, net, ignore=mine)
                if clash is None:
                    return PatternResult(
                        reason=REASON,
                        net=net,
                        pieces=pieces,
                        joins=((a.owner, b.owner),),
                        candidate=f"{name} exit({ea.side},{eb.side})",
                        tried=tried,
                    )
                score = clash.need - clash.have
                if worst is None or score > worst[0]:
                    worst = (score, clash)
    return PatternResult(
        reason=REASON,
        net=net,
        refusal=_refuse(
            ctx,
            net,
            a,
            b,
            layer,
            worst[1] if worst else None,
            worst[1].rule if worst else ("lane" if lane_why else ("not local" if (strangers and not built) else "no candidate")),
            len(exits_a),
            len(exits_b),
            built,
            tried,
            links=links_seen or MAX_LINKS,
            lane=lane_why,
            strangers=strangers,
            ratio=best_ratio,
            sides=(tuple(e.side for e in exits_a), tuple(e.side for e in exits_b)),
        ),
        tried=tried,
    )


def _toward_first(exits, here: Terminal, there: Terminal):
    """A.7's exits, re-ordered so the ones pointing at the other pad come first.

    A.7 orders a pad's exits **outward** — away from its own footprint's centre — because that is what
    a fanout escape needs: a closed row can only go straight out. A link between two pads wants the
    opposite first, and taking A.7's order literally makes a hop leave its pad in the wrong direction
    and walk around. Measured: node's `CC1` came out at 1.27x the distance between its pads and its
    `LED_A` the same, and six of the DS2 Addon's ten hops were two or three segments where one would
    do; with the re-ordering every one of them is **1.00x**, a single straight segment.

    This is an ordering of the enumeration, not a score on the candidates — B.0's nesting is an order
    too — and it is derived from the pads' own geometry and nothing else, so the board stays a pure
    function of the placement. It does not rescue every case: c3_usb's `LED_A` has its two exits
    0.08 mm from crossing and still walks, which is what `DETOUR_MAX` is for.
    """
    dx, dy = there.at[0] - here.at[0], there.at[1] - here.at[1]
    order = {e.side: i for i, e in enumerate(exits)}
    return tuple(sorted(exits, key=lambda e: (-(e.dir[0] * dx + e.dir[1] * dy), order[e.side])))


def _allowed_shapes(ctx, net, a: Terminal, b: Terminal, span: float, exits_a, exits_b, layer: str, mine) -> tuple[frozenset[str], str]:
    """Which link shapes this net may use, and who made it so.

    Within `HOP_MM`, all of them. Past it, the straight line is the rule — and stepping around the
    pads' **own** footprints is part of it.

    B.1 writes the second clause as "the `direct` candidate alone — one straight leg, no corner —
    clears", and gives its reason: a straight line is never a detour and never takes a corridor it
    did not already need. The reason is the rule. B.1's own worked example is blinky, whose `LED` it
    says is 21.8675 mm of straight copper from `R1.2` to `D1.2` — and it is not: `D1.1 [GND]` sits
    **on** that line, 0.2988 mm inside it. That claim came from the checked-in routed artifact, which
    carries the short as a real one; S3's agreement test already reported it. So read literally, the
    clause leaves blinky 100 % leftover, which is the exact outcome B.1 says it exists to avoid.

    What separates blinky from a net that really needs the maze router is **who** blocks the straight
    line. When every blocker is a pad of one of the two terminals' own footprints, stepping around it
    is a local decision the pads' own geometry answers — the same decision `pad_exits` already makes,
    one pad further out — and the candidate shapes bound how far the step can go. When anything else
    is in the way, or when no straight line exists between the exits at all, going round is a
    corridor decision, and corridor decisions are the maze router's.

    Measured on the five boards: blinky's `LED` is the only long two-pad net that takes the
    permissive branch. Letting the "no straight line at all" case take it too locked 33 mm of copper
    across the DS2 Addon before anything else was routed — `GPIO0` at 17.34 mm, `ADC_TX` at 12.76 mm,
    `ADC_DRDY` at 16.8 mm — after which `GND` could not reach five of its pads. That is `route.py`'s
    own recorded failure mode, seen from the other side. `docs/r2-measurements.md` S4.
    """
    if span <= HOP_MM:
        return (ALL_SHAPES, "")
    strangers: list[str] = []
    saw_direct = False
    for ea in exits_a:
        for eb in exits_b:
            for name, pts in link_candidates(ea, eb):
                if name != "direct":
                    continue
                full = mitre(_dedupe((a.at, ea.at) + pts[1:-1] + (eb.at, b.at)))
                if full is None or not shape_ok(full):
                    continue
                w = _width(ctx, net)
                pieces = pieces_of(full, [w] * (len(full) - 1), net=net, reason=REASON, layer=layer, owner=a.owner, board=ctx.board)
                saw_direct = True
                clash = blocked(ctx.scene, pieces, net, ignore=mine)
                if clash is None:
                    return (frozenset({"direct"}), "")  # the straight line fits; the main loop finds it
                if clash.item.owner.split(".")[0] not in (a.ref, b.ref):
                    strangers.append(clash.item.label())
    if strangers:
        return (frozenset({"direct"}), sorted(set(strangers))[0])
    if not saw_direct:
        # No straight line exists at all between these pads' exits, so there is nothing to step
        # around: the net is a route, not a hop. This is the case the permissive branch must not
        # swallow — on ds2 it let three signals (GPIO0 at 17.3 mm, ADC_TX at 12.8 mm, ADC_DRDY) lock
        # 33 mm of copper across the board before anything else was routed, and GND could then not
        # reach five of its pads. Exactly the failure `route.py`'s own comment records.
        return (frozenset({"direct"}), "no straight line between their exits")
    return (ALL_SHAPES, "")


def _dedupe(pts):
    out = []
    for p in pts:
        if not out or out[-1] != p:
            out.append(p)
    return tuple(out)


def _other_layer(ctx: PatternCtx, layer: str) -> str:
    """The layer a `NetReq(layers=[...])` move would name instead: the far side of the board, which
    on every board R2 sees is `B.Cu` for something on the front and `F.Cu` for something on the back."""
    layers = [L for L in ctx.scene.layers if L != layer]
    return layers[-1] if layers else layer


_DIRS = {"right": (1.0, 0.0), "left": (-1.0, 0.0), "down": (0.0, 1.0), "up": (0.0, -1.0)}


def _toward(dx: float, dy: float) -> str:
    """The nearest of right/left/down/up in KiCad's y-down frame. `route_checks._quantise`'s answer,
    and the vocabulary `Place(toward=)` accepts (`pcb_place._DIRS`)."""
    if abs(dx) >= abs(dy):
        return "right" if dx >= 0 else "left"
    return "down" if dy >= 0 else "up"


def _moves(ctx, net: str, a: Terminal, b: Terminal, layer: str, clash, sides, other: str) -> str:
    """Two edits, both real: one that moves a part, one that moves the net.

    The direction is computed, not waved at. Against a blocker it is the way out of the blocker; with
    no candidate at all it is the side the other pad's own exits face, because that is what the
    enumeration could not reach — a hop into a pad whose only exit points away from its partner has
    no shape, and no amount of retrying gives it one.
    """
    if clash is not None:
        # The part to move is the *smaller* one, not merely the nearer one: both pads are equally in
        # the way of each other, and "move the 24-pin MCU" is not a move an AI can act on while "move
        # the resistor hanging off it" is. Distance to the blocker breaks the tie.
        near = min((a, b), key=lambda t: (len(ctx.scene.feet[t.ref].pads) if t.ref in ctx.scene.feet else 0, math.dist(t.at, clash.at), t.ref))
        toward = _toward(near.at[0] - clash.at[0], near.at[1] - clash.at[1])
        why = f"moves it off {clash.item.owner}"
    else:
        near = a
        want = sides[1][0] if sides[1] else ""
        if want:
            toward = want
            why = f"puts it on the side {b.owner}'s {sides[1][0]} exit faces"
        else:
            toward = _toward(a.at[0] - b.at[0], a.at[1] - b.at[1])
            why = f"gives {b.owner} room for an exit"
    return (
        f'Place("{near.ref}", toward="{toward}") {why}; '
        f'or NetReq("{net}", layers=["{other}"]) puts the net on {other}, where this row is not.'
    )


def _refuse(ctx, net, a: Terminal, b: Terminal, layer: str, clash, rule: str, na: int, nb: int, built: int, tried: int, links: int = MAX_LINKS, lane: str = "", strangers: str = "", ratio: float = 0.0, sides=((), ())) -> Refusal:
    """The refusal sentence, in the one shape every pcbc move line uses (`blocking.move_line`).

    Always **soft** (C.6). A hard refusal is for intent KRT structurally cannot honour, and a hop is
    not that: a net with `vias=False` or one layer is routed by KRT's own constrained `*_nets` step,
    on those layers, with the via cost that keeps it there. So a refused hop costs a route pcbc would
    have drawn better and never costs the constraint itself.

    It says the number it measured against the number it needed, names the rule of A.4 that decided
    it, counts what it tried so "it did not try hard enough" is answerable, and ends in a `board.py`
    edit. A refusal that does not end in an edit is a dead end for the AI, which is the whole reason
    this file exists rather than a `continue`.
    """
    where = layer or "any shared layer"
    if clash is None and strangers and not built:
        blockers = (
            f"these pads are {math.dist(a.at, b.at):.4g} mm apart, past the {HOP_MM:g} mm a hop takes on distance alone, "
            f"so only the straight line between them counts — and {strangers} (rule: not local)"
        )
    elif clash is None and ratio:
        blockers = (
            f"the shortest candidate that fits runs {ratio:.4g}x the {math.dist(a.at, b.at):.4g} mm between the pads, "
            f"past the {DETOUR_MAX:g}x a hop may take (rule: detour)"
        )
    elif clash is None and lane:
        blockers = f"every candidate runs {lane}, and a fanout lane may be crossed and not run along (rule: lane)"
    elif clash is None:
        blockers = (
            f"no link fits between the exits these pads have on {where} "
            f"({a.owner}: {', '.join(sides[0]) or 'none'}; {b.owner}: {', '.join(sides[1]) or 'none'}) — "
            f"every candidate doubled back on itself or left a leg under {MICRO_MM:g} mm (rule: {rule})"
        )
    else:
        blockers = (
            f"{clash.item.label()} at {clash.at[0]:g},{clash.at[1]:g} leaves {clash.have:.3f} mm "
            f"of the {clash.need:.3f} mm {clash.why} needs (rule: {clash.rule})"
        )
    if strangers and built:
        blockers += (
            f"\n  These pads are {math.dist(a.at, b.at):.4g} mm apart, past the {HOP_MM:g} mm a hop takes on distance alone, "
            f"so only the straight line between them was tried: {strangers}."
        )
    blockers += f"\n  Tried {na} x {nb} exits x {links} link shapes = {built} candidates, {tried} of them the shape of copper pcbc writes"
    fixes = "Moves: " + _moves(ctx, net, a, b, layer, clash, sides, _other_layer(ctx, layer))
    return Refusal(
        pattern=REASON,
        net=net,
        what=f"{a.owner} at ({a.at[0]:g},{a.at[1]:g})",
        clash=clash,
        rule=rule,
        hard=False,
        move="hop "
        + move_line(
            net,
            f"{a.owner} at ({a.at[0]:g},{a.at[1]:g})",
            f"{b.owner} at ({b.at[0]:g},{b.at[1]:g}) on {where}",
            blockers,
            fixes,
            sep="\n  ",
        ),
    )
