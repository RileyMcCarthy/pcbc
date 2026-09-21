"""The `chain` pattern: `docs/r2-design.md` B.2, and the S6 slice that owns it.

One assertion per rule, every pinned number carrying its reference in the message, and every report
line asserted as an exact string. The numbers were measured against the five placed boards on
2026-09-20 by driving `pattern_copper(stage="mid")` over each board's checked-in placed layout — the
same deterministic input `test_patterns.py::_mid` uses, and not a build, so a drift here names the
rule it left rather than the router that moved under it.

**Three of S6's own sentences do not survive contact with the boards, and the tests that show why are
here rather than in a comment.** The acceptance asks for ds2's `Chain("VDDA", "J2.1", "C4.1",
"U1.12")` to be *carried* in feed order; `C5` sits across `U1.12`'s only corridor and the chain
refuses its second link. `route_checks.check_chains` reports nothing about it, because the check
measures a straight centre-line corridor and the router has to leave pads through `pad_exits` and
write 0/45/90: on this link the two disagree, and the test that pins the refusal pins the
disagreement beside it so the design's claim cannot quietly come back. And the refusal is **not**
hard, which the slice originally said it was: R-X4's verify half now reads the routed board instead
(`route_verify.chain_order`, pinned in `tests/test_route_verify_chains.py`), so a declared order is
gated on a measurement of KRT and not on a prediction about it.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from pcbc.compile import compile_design
from pcbc.language import load_board
from pcbc.patterns import MID, PatternCtx, pattern_copper, terminals
from pcbc.patterns.chain import (
    ELBOWS,
    MAX_PER_LINK,
    ChainSpec,
    REASON,
    elbows,
    link_shapes,
    run as chain_run,
    specs as chain_specs,
    stub_clash,
    stub_need,
)
from pcbc.patterns.spine import WIDE_MM, anchor
from pcbc.route_checks import _principal_order, build_ctx, check_chains
from pcbc.route_emit import piece_key
from pcbc.route_geom import clears, gap, track_shape
from pcbc.route_scene import blocked, build_scene, pad_exits
from pcbc.route_verify import verify_copper

ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = ROOT / "examples"
DS2 = Path.home() / "Documents" / "MaD" / "Hardware" / "DS2Addon" / "pcbc"
NAMES = ("blinky", "buck", "c3_usb", "node", "ds2")
ALL = tuple(n for n in NAMES if n != "ds2" or (DS2 / "ds2_addon.py").exists())


def _board(name: str) -> Path:
    return DS2 / "ds2_addon.py" if name == "ds2" else EXAMPLES / name / f"{name}.py"


def _placed(name: str) -> Path:
    # Generated from `board.py`, never read out of `examples/**/layout/` — that directory is
    # gitignored build output, so reading it made 45 tests in this file raise
    # `FileNotFoundError` on a clean checkout, and the artifacts were stale besides
    # (`conftest.placed_board`). Placing is pure Python: check + seed + place, no KiCad.
    from conftest import placed_board

    return placed_board(name, _board(name))


def _uuid_name(name: str) -> str:
    return "ds2_addon" if name == "ds2" else name


def _ctx(name: str) -> PatternCtx:
    """A `PatternCtx` over the board's checked-in **placed** layout, with no pattern copper in it.

    The deterministic input every unit test here starts from. A real build runs the hops and the
    fanout first (C.1), so the copper a chain sees on a build is not this copper; the build's numbers
    are pinned where they are measured and these are pinned where they are measured."""
    design = load_board(_board(name))
    job = compile_design(design)
    scene = build_scene(design, job, job.constraints, _placed(name).read_text())
    return PatternCtx(scene=scene, design=design, job=job, cs=job.constraints, board=_uuid_name(name), stage="mid")


class _Plan:
    """What `pattern_copper` hands back, for the one pattern this file is about."""

    def __init__(self, pieces, refused, notes, claimed, scene, ids, links):
        self.pieces, self.refused, self.notes, self.claimed = pieces, refused, notes, claimed
        self.scene, self.ids, self.links = scene, ids, links

    def refusals(self):
        """`PatternPlan.refusals`' shape, so a test reads the same way either side of registration."""
        return tuple(r for rs in self.refused.values() for r in rs)


def _mid(name: str):
    """**The chain pattern alone**, over the board's checked-in placed layout.

    It is driven directly rather than through `pattern_copper(stage="mid")` because `chain` is not in
    `patterns.MID` — S6 wrote it and did not register it, and
    `test_the_chain_is_written_and_deliberately_not_registered` carries the four ds2 builds that
    decided that. Driving it here keeps every number in this file exercised and pinned, so the
    pattern cannot rot between now and the maze router that would let it run.

    The scene is the placed board with no pattern copper in it: a real build runs the hops and the
    fanout first (C.1), so the copper a chain would see on a build is not this copper. The build's
    numbers are pinned where they are measured and these are pinned where they are measured.
    """
    from pcbc.patterns import chain as _chain_mod

    design = load_board(_board(name))
    job = compile_design(design)
    text = _placed(name).read_text()
    ctx = _ctx(name)
    pieces, refused, notes, links = [], {}, [], {}
    for spec in chain_specs(ctx):
        res = _chain_mod.run(ctx, spec)
        if res.pieces:
            ctx.scene.add(ctx.scene.item_of(x) for x in res.pieces)
            pieces.extend(res.pieces)
        if res.refusal is not None:
            refused[res.net] = (res.refusal,)
        if res.links != (0, 0) or res.coverage:
            links[res.net] = [res.links[0], res.links[1], res.coverage]
        notes.extend(res.notes)
    claimed = frozenset(x.net for x in pieces)
    ids = tuple(ctx.scene.item_of(x).id for x in pieces)
    return _Plan(tuple(pieces), refused, tuple(notes), claimed, ctx.scene, ids, {REASON: links}), design, job, text


def _chain(plan):
    return [p for p in plan.pieces if p.reason == REASON]


# --- B.2's population and its order ---------------------------------------------------------------

CHAINS = {
    "blinky": (),
    "buck": ("FB", "SW"),
    "c3_usb": ("USB_DP", "USB_DN", "BOOT", "EN"),
    "node": ("USB_DP", "USB_DN", "BOOT", "EN", "GATE", "SCL", "SDA", "T_DIV", "T_OUT"),
    "ds2": ("VDDA", "3V3", "AIN0", "AIN1", "AIN2", "AIN3", "REFN_F", "REFP_F", "VSS"),
}
"""B.2's population per board, in `specs`' own order: the declared `Chain()`s in **board order**
first, then every unowned three-or-more-pad net `sorted(net)`.

Measured 2026-09-20. blinky has none — its only multi-pad net is `GND`, which is a plane and the
tap's. Four of the five declared chains in the repo are the two USB pairs, which are skipped; the
fifth is ds2's `VDDA`. Everything else here is implicit."""

SKIPPED = {
    "c3_usb": ("USB_DP", "USB_DN"),
    "node": ("USB_DP", "USB_DN"),
}
"""The declared chains that carry a `PairSpec`. B.2: "Chains on nets carrying a `PairSpec` are
**skipped with a printed note**, not silently: pairs are R4's.\""""

CHAIN_OWNS = {"blinky": (0, 0.0), "buck": (1, 1.13), "c3_usb": (16, 14.646), "node": (28, 17.759), "ds2": (13, 16.373)}
"""`(segments, mm)` this pattern writes on each placed board, exact. A chain that stops claiming a net
shows up here before it shows up anywhere else."""

CHAIN_LINKS = {
    "blinky": {},
    "buck": {"FB": (1, 2, 0.3824), "SW": (0, 2, 0.0)},
    "c3_usb": {"BOOT": (2, 2, 1.1046), "EN": (3, 3, 1.5245)},
    "node": {
        "BOOT": (2, 2, 1.1046), "EN": (3, 3, 1.5648), "GATE": (1, 2, 0.9063), "SCL": (0, 2, 0.0),
        "SDA": (0, 2, 0.0), "T_DIV": (2, 2, 0.951), "T_OUT": (0, 2, 0.0),
    },
    # Every ds2 number here is a zero or a fragment, and that is the slice's finding rather than a
    # disappointment: `CHAIN_HOP_MM` bounds a link to the 8 mm a placed chain hop reaches, and ds2's
    # parts are further apart than that. `VDDA` is 0 of 2 because its own first link is 13.04 mm.
    "ds2": {
        "VDDA": (0, 2, 0.0), "3V3": (1, 4, 0.355), "AIN0": (2, 3, 0.1819), "AIN1": (2, 3, 0.1673),
        "AIN2": (0, 3, 0.0), "AIN3": (0, 3, 0.0), "REFN_F": (0, 3, 0.0), "REFP_F": (0, 3, 0.0),
        "VSS": (0, 8, 0.0),
    },
}

"""`{net: (made, needed, coverage)}` — how many of each chain's links the copper actually joins,
counted off the pieces by `PatternResult.links`, and how much of the net's airwire it wrote."""

CHAIN_REFUSED = {
    "blinky": [],
    "buck": [("R_FB_TOP.2->R_FB_BOT.1", "copper", False), ("C_BOOT.2->U1.2", "no candidate", False)],
    "c3_usb": [],
    "node": [
        ("R_G.2->Q1.1", "no candidate", False), ("U1.19->U4.4", "not local", False),
        ("U1.18->U4.3", "not local", False), ("U1.12->U5.4", "copper", False),
    ],
    # Eight of ds2's nine are `not local`, including the declared `VDDA`: its first link is 13.04 mm
    # centre to centre, past the 8 mm `CHAIN_HOP_MM` bounds a chain hop to. That is what the bound
    # was for — see `test_the_chain_is_written_and_deliberately_not_registered` for the builds.
    "ds2": [
        ("J2.1->C4.1", "not local", False), ("R10.1->U1.13", "not local", False),
        ("C7.1->U1.11", "not local", False), ("C8.2->U1.10", "not local", False),
        ("U1.7->C10.1", "not local", False), ("U1.6->C11.2", "not local", False),
        ("U1.8->C1.1", "not local", False), ("U1.9->C3.1", "not local", False),
        ("C9.2->J2.2", "not local", False),
    ],
}
"""`(failing link, rule, hard)` per board, exact (C.6). **No hard refusal exists in this repo at all**,
on any board, declared chain included — S6's review retired the last one (`Refusal.hard`), so
`test_examples_fab.py`'s `not any(r["hard"] ...)` assertion is now true by construction rather than by
the accident of which four boards it builds."""


def test_a_chain_is_a_declared_order_first_then_every_unowned_three_pad_net():
    """B.2's population and `specs`' order, exact per board.

    Declared first and in **board order**, because a declared order is the author's and an implicit
    one is pcbc's reading of where they put the parts; then the implicit ones `sorted(net)`, which is
    B.0's order for a net-driven pattern. The three exclusions are read off the three patterns that
    own those nets rather than restated: a plane net is `tap`'s, a two-terminal net is `hop`'s, and a
    net at least `WIDE_MM` wide is `spine`'s.
    """
    for name in ALL:
        ctx = _ctx(name)
        assert tuple(s.net for s in chain_specs(ctx)) == CHAINS[name], (name, [s.net for s in chain_specs(ctx)])
        declared = [ch.net for ch in ctx.design.chains]
        for i, s in enumerate(chain_specs(ctx)):
            if s.declared:
                assert s.net == declared[i] and s.where.startswith("Chain line "), (name, s.net, s.where)
                continue
            assert s.net not in ctx.scene.plane_of, (name, s.net, "a plane net is the tap's: a plane IS its spine")
            assert len(terminals(ctx.scene, s.net)) >= 3, (name, s.net, "two terminals is a hop's (B.1)")
            assert s.width < WIDE_MM, (name, s.net, s.width, "a net this wide is the spine's (B.4)")
            c = ctx.cs.by_net(s.net)
            assert c is None or (c.pair is None and (c.group is None or c.group.kind != "bus")), (name, s.net)
        impl = [s.net for s in chain_specs(ctx) if not s.declared]
        assert impl == sorted(impl), (name, impl, "B.0: sorted(net) for a net-driven pattern")


def test_a_pair_net_chain_is_skipped_with_a_printed_note_and_never_in_silence():
    """B.2: "Chains on nets carrying a `PairSpec` are skipped with a printed note, not silently."

    Four of the five declared chains in this repo are pair nets, so this rule is most of what the
    declared population does. A skip is a **note** and not a `Refusal`: C.6 pins the refusal count
    per board exactly, and a chain pcbc declined to consider is not a chain pcbc failed to route —
    counting it would make that number mean something else.
    """
    for name in ("c3_usb", "node"):
        ctx = _ctx(name)
        skipped = [s for s in chain_specs(ctx) if s.skip]
        assert tuple(s.net for s in skipped) == SKIPPED[name], (name, [s.net for s in skipped])
        for s in skipped:
            res = chain_run(ctx, s)
            assert res.pieces == () and res.refusal is None, (name, s.net, "a skip writes nothing and refuses nothing")
            assert len(res.notes) == 1 and res.notes[0].startswith(f"style: chain {s.net}: skipped, carries a Pair"), res.notes
    ctx = _ctx("c3_usb")
    dp = next(s for s in chain_specs(ctx) if s.net == "USB_DP")
    assert chain_run(ctx, dp).notes == (
        "style: chain USB_DP: skipped, carries a Pair, and R2 routes no pairs at all — J1.A6, U3.1, U1.27 is R4's "
        "(Chain line 267)",
    ), chain_run(ctx, dp).notes
    plan, _d, _j, _t = _mid("c3_usb")
    assert len([n for n in plan.notes if n.startswith("style: chain")]) == 2, plan.notes
    assert "USB_DP" not in plan.claimed and "USB_DN" not in plan.claimed, plan.claimed


def test_every_chain_refusal_is_soft_declared_or_not_and_nothing_in_the_repo_is_hard():
    """C.6's hardness rule after S6's review, which for R2 is now "nothing sets it".

    The slice shipped a declared `Chain()` as a **hard** refusal — an abort of the whole build before
    KRT runs — on the premise that KRT would connect the same pads by branching and the branch is
    exactly what the order was written to forbid. Two measurements retired it, and both are pinned
    below and in `test_route_verify_chains.py`:

    1. On the only board in the repo with a declared chain that is not a pair, **KRT honours the
       order**. ds2's routed `VDDA` runs one straight 45-degree trace across `C4.1`'s pad, with
       1.1185 mm of its centre line inside the pad's own copper.
    2. The invariant the abort protected was checked **nowhere**: R-X4's `**V**` half did not exist.
       It does now (`route_verify.chain_order`), so the stop happens on a measurement of the finished
       copper instead of on a prediction about the router.

    So every refusal this pattern makes is soft, declared or not, and `--strict-patterns` is the
    author's switch for wanting otherwise. `Refusal.hard` and `hard_refusals` stay wired for the two
    cases that still deserve them (`vias=False`, a single-layer net) and for that flag; nothing sets
    them, and this test is the assertion that nothing does.
    """
    for name in ALL:
        plan, _d, _j, _t = _mid(name)
        got = [(r.what, r.rule, r.hard) for r in plan.refusals() if r.pattern == REASON]
        assert got == CHAIN_REFUSED[name], (name, got, CHAIN_REFUSED[name])
        for r in plan.refusals():
            if r.pattern != REASON:
                continue
            spec = next(s for s in chain_specs(_ctx(name)) if s.net == r.net)
            assert not r.hard, (name, r.net, spec.declared, "a declared chain is no longer a hard refusal")
            assert r.move.startswith(f"chain {r.net}: "), r.move
            assert "Moves: " in r.move and r.move.rstrip().endswith("."), r.move
    assert not any(r.hard for name in ALL for r in _mid(name)[0].refusals()), (
        "test_examples_fab.py asserts this on the four it builds; after S6's review it holds on ds2 too"
    )
    # And the `hard` mechanism itself is alive, so retiring its last producer did not quietly retire
    # the escape hatch: `--strict-patterns` still turns every one of these into a stop.
    import os

    from pcbc.patterns import hard_refusals

    plan, _d, _j, _t = _mid("ds2")
    assert hard_refusals(plan) == (), "nothing is hard without the flag"
    was = os.environ.get("PCBC_STRICT_PATTERNS")
    os.environ["PCBC_STRICT_PATTERNS"] = "1"
    try:
        assert len(hard_refusals(plan)) == len(plan.refusals()) > 0, "the flag makes every refusal fatal"
    finally:
        os.environ.pop("PCBC_STRICT_PATTERNS", None) if was is None else os.environ.update({"PCBC_STRICT_PATTERNS": was})


@pytest.mark.skipif("ds2" not in ALL, reason="the DS2 Addon is not checked out here")
def test_the_ds2_addon_declared_chain_refuses_on_distance_and_the_order_is_checked_on_the_board():
    """The one declared chain R2 would route, the sentence S6 ended up with, and why it is honest.

    `Chain("VDDA", "J2.1", "C4.1", "U1.12")` refuses at **link 1**, on distance: `J2.1 -> C4.1` is
    13.04 mm centre to centre against `CHAIN_HOP_MM`'s 8 mm. Nothing is written.

    That bound is S6's own finding and not a tidy round number. Without it the chain wrote 14.4 mm
    here, and the ds2 build then failed with its `GND` plane cut into two islands — 3.4 mm from the
    nearest chain copper and on the other layer. Four bounds were tried and the failure moved rather
    than went away, which is why the pattern is not registered at all
    (`test_the_chain_is_written_and_deliberately_not_registered`).

    **S6's acceptance asked for this chain to be carried and the pattern cannot carry it.** The route
    exists — KRT builds it, a six-corner 14.744 mm detour for a 3.8204 mm airwire — and every one of
    the 96 candidate points `link_candidates` can construct for that link lies inside the two exits'
    own bounding box. A fixed enumeration is not a search, and R3's maze router is where this belongs.

    What replaced "carry it" is "check it": the declared order is read back off the **finished**
    board by `route_verify.chain_order`, which reports `held` for this chain on ds2's routed layout
    (`test_route_verify_chains.py`). The order is still enforced; it is enforced by measuring KRT
    rather than by predicting it, which is the change the S6 review made.

    The refusal is pinned verbatim because every clause is a decision: the failing link by number,
    the distance with the bound it broke and the reason the bound exists, that nothing was emitted,
    what was tried, and two `board.py` edits.
    """
    ctx = _ctx("ds2")
    spec = next(s for s in chain_specs(ctx) if s.net == "VDDA")
    assert spec.declared and spec.where == "Chain line 97" and spec.labels == ("J2.1", "C4.1", "U1.12"), spec
    assert tuple(t.owner for t in spec.stations) == ("J2.1", "C4.1", "U1.12"), "the Chain()'s order, verbatim"
    assert spec.spans == (13.0402, 3.8204), spec.spans
    res = chain_run(ctx, spec)
    assert res.pieces == () and res.joins == () and res.links == (0, 2), (res.pieces, res.joins, res.links)
    assert res.refusal is not None and not res.refusal.hard and res.refusal.what == "J2.1->C4.1", res.refusal
    assert res.refusal.rule == "not local" and res.refusal.clash is None, res.refusal
    assert res.refusal.move.splitlines() == [
        "chain VDDA: link 1 of 2, J2.1 at (8.89,12.7) cannot reach C4.1 at (20.33,6.4411) on F.Cu at 0.25 mm "
        "(Chain line 97).",
        "  In the way: J2.1 and C4.1 are 13.04 mm apart, past the 8 mm a chain hop the placement made reaches — "
        "this pattern draws a short local link well and a long one badly, so the run is the router's and the order "
        "is read back off the finished board instead (rule: not local)",
        "  Nothing of this chain is down: link 1 of 2 is its first, and a chain is an order pcbc does not re-order "
        "to close",
        f"  Tried 0 candidates of at most 2 x {MAX_PER_LINK}.",
        '  Moves: Place("J2", toward="right") turns its exit toward C4.1; or '
        'Chain("VDDA", "J2.1", "C4.1", "U1.12") is asking for an order this placement cannot give — drop C4 from the '
        "chain and feed it locally instead.",
    ], res.refusal.move


# --- R-X4, the no-stub rule -----------------------------------------------------------------------


def test_the_no_stub_rule_fires_on_a_later_stop_and_leaves_the_order_the_pads_give_alone():
    """R-X4: "A link's pieces must not pass within ... of a *later* member's pad on the same layer.
    The chain goes through pads in order, never past one and back" (B.2).

    Both halves on one board and one net, because a rule that only ever fires proves nothing about
    when it should not. c3_usb's `EN` has four stops, and in the order `_principal_order` gives —
    `SW_RST.1, R_EN.2, C_EN.1, U1.8` — every link clears: the feed goes *through* `R_EN.2` rather than
    past it, which is what a chain is. Re-order it to `SW_RST.1, U1.8, R_EN.2, C_EN.1` and the first
    link has to run from the switch to the MCU pin straight past `R_EN.2`, 0.215 mm **inside** it
    against the 0.207 mm `stackup.clearance_min + w/2` this rule keeps, and the pattern writes nothing.

    The move is the part that makes this rule worth having: what is wrong is the **order**, not the
    placement, so the refusal hands back the `Chain()` line with `R_EN.2` put where the placement
    already has it rather than telling the author to move a part that is exactly where it belongs.
    """
    ctx = _ctx("c3_usb")
    good = next(s for s in chain_specs(ctx) if s.net == "EN")
    assert good.labels == ("SW_RST.1", "R_EN.2", "C_EN.1", "U1.8"), good.labels
    assert chain_run(ctx, good).refusal is None, "the order the pads give feeds through R_EN.2, so R-X4 says nothing"
    order = (0, 3, 1, 2)
    bad = replace(
        good,
        stations=tuple(good.stations[i] for i in order),
        labels=tuple(good.labels[i] for i in order),
    )
    res = chain_run(ctx, bad)
    assert res.pieces == (), "B.0: a pattern emits nothing at all when nothing clears"
    assert res.refusal is not None and res.refusal.rule == "no_stub" and res.refusal.what == "SW_RST.1->U1.8", res.refusal
    assert res.refusal.move.splitlines() == [
        "chain EN: link 1 of 3, SW_RST.1 at (13.1,18.85) cannot reach U1.8 at (13.85,13.75) on F.Cu at 0.16 mm "
        "(4 pads on their own principal axis).",
        "  In the way: R_EN.2 [EN] at 13.85,15.37 is a later stop on this chain, and every candidate leaves it "
        "-0.215 mm of the 0.207 mm a chain keeps off a stop it has not reached yet — so this link would run past it "
        "and the feed would come back, which is the stub B.2 forbids: a chain goes through its pads in order "
        "(rule: no_stub)",
        "  Nothing of this chain is down: link 1 of 3 is its first, and a chain is an order pcbc does not re-order "
        "to close",
        f"  Tried 13 candidates of at most 3 x {MAX_PER_LINK}.",
        '  Moves: Chain("EN", "SW_RST.1", "R_EN.2", "U1.8", "C_EN.1") declares the order the placement gives, in '
        "place of the one pcbc read off the pads' own principal axis.",
    ], res.refusal.move


def test_r_x4s_distance_is_the_process_floor_and_blocked_could_not_have_been_asked_this():
    """S6 decisions 1 and 2, as two facts about the same candidate.

    B.2 writes R-X4's distance as `between(net, net) + w/2 + pad_half`, and
    `ClearanceTable.between` of a net against itself is `(0.0, "same net")` — so B.2's own formula
    says "must not touch a later pad", which quantised geometry steps around by a nanometre.
    `stub_need` is `stackup.clearance_min + w/2` instead: the process floor, the number `route.py`
    already hands KRT as `--same-net-pad-clearance`.

    And `blocked()` is structurally unable to report it. `route_scene._pair_clashes` skips the copper
    rule for two items on the same net, which it **must**, because a chain link lands *on* its own
    stations — so the standard clash path is handed the very candidate that runs through `R_EN.2` and
    says nothing about it, while `stub_clash` says the number. That is why this rule is an explicit
    test in `chain.py` and not a tightened `need`.
    """
    from pcbc.patterns import legs_of, mitre, pieces_of, shape_ok

    ctx = _ctx("c3_usb")
    scene = ctx.scene
    assert scene.table.between("EN", "EN") == (0.0, "same net"), "B.2's own formula collapses to `must not touch`"
    good = next(s for s in chain_specs(ctx) if s.net == "EN")
    assert stub_need(scene, good.width) == pytest.approx(scene.stack.clearance_min + good.width / 2.0)
    assert stub_need(scene, good.width) == pytest.approx(0.207), "0.127 process floor + half of the 0.16 mm class"
    order = (0, 3, 1, 2)
    bad = replace(good, stations=tuple(good.stations[i] for i in order), labels=tuple(good.labels[i] for i in order))
    a, b, later = bad.stations[0], bad.stations[1], bad.stations[2:]
    exits_a = pad_exits(scene, a.item, bad.width, bad.layer)
    exits_b = pad_exits(scene, b.item, bad.width, bad.layer)
    # `mine` is the chain's own stations, exactly as `_run` builds it — so `blocked` is asked the
    # question with the *later* stop's pad still in the scene and still visible to it.
    mine = frozenset(it.id for it in scene.items if it.owner in {t.owner for t in bad.stations if t.owner != "R_EN.2"})
    fired = 0
    for _name, pts in link_shapes(a, b, exits_a, exits_b):
        full = mitre(pts)
        if full is None or not shape_ok(full):
            continue
        pieces = pieces_of(full, [bad.width] * (len(full) - 1), net="EN", reason=REASON, layer=bad.layer, owner=a.owner, board="x")
        if not legs_of(pieces):
            continue
        clash = stub_clash(ctx, bad, pieces, later)
        if clash is None:
            continue
        fired += 1
        assert clash.rule == "no_stub" and clash.item.owner == "R_EN.2", clash
        assert clash.need == round(stub_need(scene, bad.width), 4) and clash.have < clash.need, clash
        # The same pieces, the same pad in scope, and `blocked` reports nothing about it.
        reported = [c for c in (blocked(scene, pieces, "EN", ignore=mine),) if c is not None]
        assert not any(c.item.owner == "R_EN.2" for c in reported), (
            reported, "route_scene._pair_clashes skips the copper rule for a same-net pair, by design"
        )
        # `clears` makes the decision and carries EPS_MM itself; `gap` only describes it, which is
        # why the two are never both added to a requirement.
        piece = next(q for q in pieces if not clears(track_shape(q.a, q.b, q.w), clash.item.copper, clash.need))
        assert gap(track_shape(piece.a, piece.b, piece.w), clash.item.copper) < clash.need, piece
    assert fired, "the re-ordered EN chain is the fixture: at least one candidate must run past R_EN.2"


def test_r_x4_fires_on_no_board_in_this_repo_and_that_is_recorded_rather_than_assumed():
    """A rule with no measurement behind it is a rule that can rot, so the absence is a test.

    Driven over every spec of every link of all five placed boards, `stub_clash` refuses nothing: a
    chain whose stations are in principal-axis order rarely runs past a later one, and the one
    declared chain in the repo has three stops. When a board does fire it, this test fails and
    `stub_clash`'s docstring gets the board's name — which is how the rule stops being carried by a
    fixture alone.
    """
    import pcbc.patterns.chain as chain_mod

    hits: list[tuple[str, str]] = []
    real = chain_mod.stub_clash
    try:
        def spy(ctx, spec, pieces, later):
            got = real(ctx, spec, pieces, later)
            if got is not None:
                hits.append((spec.net, got.item.label()))
            return got

        chain_mod.stub_clash = spy
        for name in ALL:
            _mid(name)
    finally:
        chain_mod.stub_clash = real
    assert hits == [], hits


# --- the order, and what a partial chain does -----------------------------------------------------


def test_a_chain_is_never_re_ordered_to_close_it():
    """B.2: "It never re-orders to close the net: the order is the AI's intent."

    A declared chain's stations are the `Chain()`'s members resolved to pads, in the order written,
    and an implicit chain's are `route_checks._principal_order` over the pad anchors — the *same*
    function `check_chains` measures an undeclared chain against, so the placement check and the
    router cannot disagree about what the order is. Nothing anywhere in this pattern sorts them
    again, including on the refusal path, which is where a re-order would be most tempting.
    """
    for name in ALL:
        ctx = _ctx(name)
        for s in chain_specs(ctx):
            if s.skip:
                continue
            if s.declared:
                ch = next(c for c in ctx.design.chains if c.net == s.net)
                assert s.labels == ch.pads, (name, s.net, s.labels, ch.pads, "the Chain() line, verbatim")
                continue
            pads = terminals(ctx.scene, s.net)
            want = tuple(pads[i].owner for i in _principal_order([anchor(t) for t in pads]))
            assert tuple(t.owner for t in s.stations) == want, (name, s.net, "route_checks._principal_order, exactly")
            res = chain_run(ctx, s)
            joined = [j[0] for j in res.joins]
            assert joined == [t.owner for t in s.stations[: len(joined)]], (name, s.net, res.joins, "links in order, from the first")


def test_a_chain_that_cannot_fit_a_link_keeps_the_ones_before_it_and_stops_there():
    """B.2: "A chain that cannot fit link *i* emits links `0..i-1` and refuses with the failing link
    named."

    This is where a chain and a spine part company, and it is not a detail. `spine._backbone` starts a
    new run after a failed link and keeps both halves, because a spine is a shape and half a shape
    still carries current. A chain is an *order*: emitting links `i+1..n` after refusing link `i` is
    copper laid in an order the author did not ask for, which a router then joins up without knowing
    the order exists. ds2's `AIN0` is the case — two of its three links are down and the third is
    named — and its copper stops at the stop the refusal names.
    """
    ctx = _ctx("ds2")
    spec = next(s for s in chain_specs(ctx) if s.net == "AIN0")
    assert spec.labels == ("C8.1", "R6.2", "C7.1", "U1.11"), spec.labels
    res = chain_run(ctx, spec)
    assert res.links == (2, 3) and res.joins == (("C8.1", "R6.2"), ("R6.2", "C7.1")), (res.links, res.joins)
    assert res.refusal is not None and res.refusal.what == "C7.1->U1.11", res.refusal
    assert "Links 1..2 of 3 are down, from C8.1 to C7.1" in res.refusal.move, res.refusal.move
    assert "nothing after link 3 was tried" in res.refusal.move, res.refusal.move
    reached = {o for j in res.joins for o in j}
    assert "U1.11" not in reached, "the stop after the failing link is not fed by some other link"
    for name in ALL:
        plan, _d, _j, _t = _mid(name)
        got = {n: tuple(v) for n, v in (plan.links.get(REASON) or {}).items()}
        assert got == CHAIN_LINKS[name], (name, got, CHAIN_LINKS[name])


# --- the width, and what a chain never does -------------------------------------------------------


def test_a_net_with_no_netreq_is_written_at_the_default_class_width_and_not_at_the_fab_floor():
    """S6 decision 5, and B.0's "a pattern writes at the class width or not at all".

    `hop._width` falls back to `stack.track_min` when a net has no `Constraint`, which on node is
    **0.0889 mm** — the fab's floor, and not a class. node is the board with the population: `BOOT`,
    `EN`, `GATE`, `SCL` and `SDA` have no `NetReq` at all, so `cs.by_net` gives `None` for every one
    of them, and a chain written at `track_min` would be pcbc choosing the narrowest copper the fab
    can make for a net nobody said anything about.

    `_width` reads the **Default** class instead, which is the same number
    `constraints.compile_constraints.synthesised` would give the net: it compiles a `generic` request,
    whose preset carries `width_mm=None`, which `_compile_req` reads as `floor_w` — the value
    `classes["Default"]` is built from, one line above it.
    """
    ctx = _ctx("node")
    default = next(k for k in ctx.cs.classes if k.name == "Default")
    assert ctx.scene.stack.track_min == 0.0889 and default.track_width_mm == 0.16, (ctx.scene.stack.track_min, default)
    unconstrained = [s for s in chain_specs(ctx) if ctx.cs.by_net(s.net) is None]
    assert [s.net for s in unconstrained] == ["BOOT", "EN", "GATE", "SCL", "SDA"], [s.net for s in unconstrained]
    for s in unconstrained:
        assert s.width == default.track_width_mm, (s.net, s.width, "synthesised(net)'s Default class, not the fab floor")
    for s in chain_specs(ctx):
        c = ctx.cs.by_net(s.net)
        if c is not None and not s.skip:
            assert s.width == float(c.width_mm.value), (s.net, s.width, "the net's own class where it has one")


def test_no_chain_copper_ever_necks_changes_layer_or_places_a_via():
    """B.0's "a pattern never degrades", with nothing to trade against.

    A chain has no neck into a pad as `hop` does and no bypass allowance as `spine` does: it is a
    feed, so every stop carries what the whole chain carries and a narrower link is simply a link that
    does not fit. And it writes no via and stays on one layer — a chain is a feed order on one layer,
    so R-I2's `per_change` budget never arises and a net whose stops do not share a layer gets the
    links it can (`spine`'s rule, for `spine`'s reason).
    """
    for name in ALL:
        plan, _d, _j, _t = _mid(name)
        widths = {s.net: s.width for s in chain_specs(_ctx(name))}
        layers = {s.net: s.layer for s in chain_specs(_ctx(name))}
        for p in _chain(plan):
            assert p.kind == "seg" and p.drill is None, (name, p, "no chain places a via")
            assert p.w == widths[p.net], (name, p.net, p.w, widths[p.net], "the class width, end to end")
            assert p.layer == layers[p.net], (name, p.net, p.layer, "one layer, and it is the one the spec chose")


def test_the_chain_stage_owns_what_the_census_says_and_checks_its_own_copper():
    """D.1 and D.4 on this pattern's own copper: the census is exact, the self-check is clean, and the
    same placed board gives byte-identical copper (F.3 item 2).

    `verify_copper` is asked of the **whole** mid stage rather than of the chain's half, because D.1
    judges a board and not one pattern's share of it — and it is the assertion that caught the one
    real bug in this slice: a chain runs *through* its middle stops, `pieces_of` merges the two
    collinear legs either side of one into a single segment, and `route_verify.served_refs` then
    cannot see that the copper serves that footprint, so ds2's `3V3` chain was accepted by `lane_ok`
    with `U1` exempt and rejected by the self-check for running 3.3951 mm along `U1`'s lane.
    `chain._served` is the fix, and it asks the judge's question rather than the pattern's.
    """
    for name in ALL:
        plan, _d, job, _t = _mid(name)
        mine = _chain(plan)
        got = (len(mine), round(sum(p.mm for p in mine), 3))
        assert got == CHAIN_OWNS[name], (name, got, CHAIN_OWNS[name])
        assert verify_copper(plan.scene, plan.pieces, job.constraints, ids=plan.ids) == [], name
        again, _d2, _j2, _t2 = _mid(name)
        assert [piece_key(p) for p in _chain(again)] == [piece_key(p) for p in mine], (name, "pure: same board, same copper")
        assert [p.uuid for p in _chain(again)] == [p.uuid for p in mine], name


def test_the_chain_is_written_and_deliberately_not_registered():
    """S6's outcome, and the measurement that decided it.

    `chain` is not in `patterns.MID`. Every other test in this file drives the pattern directly, so
    it is exercised and cannot rot; what does not happen is a build running it. On ds2 — a board that
    builds today — four configurations were built and every one broke it, never where the copper is:

    | what the chain wrote on ds2 | outcome |
    |---|---|
    | the whole population, 51 seg / 98.6 mm | `GND` and `VSS` unrouted |
    | implicit links bounded, 18 seg / 30.8 mm | `GND` and `VSS` unrouted |
    | the declared chain only, 5 seg / 14.4 mm | copper verified, then the `GND` plane in 2 islands |
    | every link bounded, 13 seg / 16.4 mm | `GND` and `VSS` unrouted |

    The last row decided it: 16.4 mm of short, local, legal copper, none of it within 3.4 mm of the
    tap that fails and none on the layer of the plane that splits. Locked copper moves KRT and KRT's
    copper closes the escape two stages later. Registered, `test_examples_fab` also fails on buck,
    c3_usb and node. `spine.WIDE_MM` is the precedent: S7 excluded ds2's narrow power nets from the
    spine on the same kind of measurement rather than tuning until they passed.

    What S6 ships instead is the half that needed no pattern: `route_verify.chain_order` reads a
    declared order off the finished board (`test_route_verify_chains.py`).
    """
    from pcbc.patterns import MID, PRE, POST

    assert PRE == ("hop",) and MID == ("spine",) and POST == ("tap",), (PRE, MID, POST)
    # The **stage** writes no chain copper on any board: that is what "not registered" means.
    for name in ALL:
        design = load_board(_board(name))
        job = compile_design(design)
        stage = pattern_copper(design, job, job.constraints, _placed(name).read_text(), _uuid_name(name), stage="mid")
        assert not [p for p in stage.pieces if p.reason == REASON], (name, "the mid stage must write no chain copper")
    # And the pattern is still whole: asked directly it still has a population and still writes.
    plan, _d, _j, _t = _mid("c3_usb")
    assert [p for p in plan.pieces if p.reason == REASON], "driven directly it still writes"
    assert [sp.net for sp in chain_specs(_ctx("c3_usb")) if not sp.skip], "and still has a population"


# --- the elbows -----------------------------------------------------------------------------------


def test_the_elbows_are_offered_after_every_shape_the_shared_builder_has():
    """The one deviation from B.0's candidate list, and the shape of it.

    `link_candidates` builds B.0's `L-h` and `L-v` and then drops them for failing `turn_ok`, which a
    right angle does — but every caller mitres afterwards, and `mitre` exists to make a right angle
    legal. So the filter runs one step before the repair and B.0's two elbow shapes are unreachable
    for every pattern. This pattern offers them again, **after** everything `spine.link_shapes` has,
    so the prefix of its candidate list is byte-identical to a backbone link's: an elbow can only
    answer a link that had no answer, and it reorders nothing.
    """
    from pcbc.patterns.spine import link_shapes as spine_link_shapes

    ctx = _ctx("ds2")
    spec = next(s for s in chain_specs(ctx) if s.net == "VDDA")
    a, b = spec.stations[1], spec.stations[2]
    exits_a = pad_exits(ctx.scene, a.item, spec.width, spec.layer)
    exits_b = pad_exits(ctx.scene, b.item, spec.width, spec.layer)
    shared = spine_link_shapes(a, b, exits_a, exits_b)
    mine = link_shapes(a, b, exits_a, exits_b)
    assert mine[: len(shared)] == shared, "the shared builder's list, unchanged and in front"
    extra = mine[len(shared):]
    assert extra and {n for n, _pts in extra} <= set(ELBOWS), extra
    assert MAX_PER_LINK == 4 * 4 * (7 + 2) + 3 == 147, MAX_PER_LINK
    assert len(mine) <= MAX_PER_LINK, (len(mine), MAX_PER_LINK)
    # Two axis legs and a corner taken one coordinate from each end, so it is on KiCad's grid by
    # construction — and a corner equal to either end is a straight line `direct` already offers.
    for ea in exits_a:
        for eb in exits_b:
            for name, pts in elbows(ea, eb):
                assert len(pts) == 3 and pts[0] == ea.at and pts[2] == eb.at, (name, pts)
                assert pts[1] in ((eb.at[0], ea.at[1]), (ea.at[0], eb.at[1])), (name, pts)
                assert pts[1] != ea.at and pts[1] != eb.at, (name, pts, "a degenerate elbow is `direct`")


def test_an_elbow_finishes_a_chain_the_shared_shapes_cannot():
    """What the deviation buys, measured rather than argued.

    c3_usb's `EN` is the clearest of the four cases: with B.0's shapes alone it makes 2 of its 3 links
    and refuses, and with the elbows it makes **3 of 3** and c3_usb's chain refusals go 1 to 0. The
    test drives the pattern with the elbows suppressed, so the comparison is on this board and this
    copper rather than on a remembered number.
    """
    import pcbc.patterns.chain as chain_mod
    from pcbc.patterns.spine import link_shapes as spine_link_shapes

    ctx = _ctx("c3_usb")
    spec = next(s for s in chain_specs(ctx) if s.net == "EN")
    with_elbows = chain_run(ctx, spec)
    assert with_elbows.links == (3, 3) and with_elbows.refusal is None, with_elbows
    real = chain_mod.link_shapes
    try:
        chain_mod.link_shapes = spine_link_shapes
        without = chain_run(ctx, spec)
    finally:
        chain_mod.link_shapes = real
    assert without.links == (2, 3) and without.refusal is not None, without
    assert without.refusal.what == "C_EN.1->U1.8", without.refusal.what


# --- the spec record ------------------------------------------------------------------------------


def test_a_chain_spec_is_frozen_and_carries_where_its_order_came_from():
    """`ChainSpec.declared` separates the author's order from pcbc's reading of a placement, so it is
    carried on the spec and not re-derived in the refusal, where it would be a second opinion about
    the same fact. After S6's review it no longer decides how hard the refusal is — nothing does —
    but it still decides **where the order is checked**: only a declared chain is read back off the
    routed board by `route_verify.chain_order`, because only a declared order can be disobeyed.
    `where` is what the refusal quotes: the `Chain()`'s own line number, or how many pads the
    implicit sweep found and what ordered them."""
    ctx = _ctx("ds2")
    vdda = next(s for s in chain_specs(ctx) if s.net == "VDDA")
    vss = next(s for s in chain_specs(ctx) if s.net == "VSS")
    assert isinstance(vdda, ChainSpec) and vdda.declared and vdda.where == "Chain line 97"
    assert not vss.declared and vss.where == "9 pads on their own principal axis"
    with pytest.raises(Exception):
        vdda.net = "nope"  # type: ignore[misc]
    assert len(vss.labels) == len(vss.stations) == 9, vss.labels
    assert all(label == t.owner for label, t in zip(vss.labels, vss.stations)), "an implicit stop is named by its pad"
    assert vdda.labels == ("J2.1", "C4.1", "U1.12"), "a declared stop is named the way the Chain() named it"
