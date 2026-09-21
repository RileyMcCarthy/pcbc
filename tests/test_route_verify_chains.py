"""R-X4's **V** half: does a declared `Chain()`'s order survive in the copper KRT finished?

`docs/router-plan.md` line 202 tags R-X4 "**P** (chain), **V**" — a pattern *and* a verification —
and until S6's review only the P existed. The chain pattern refused a link it could not write, and
nothing anywhere read a routed board to ask whether the order the author declared was in it. That
absence is what made the pattern's refusal **hard**: the abort was standing in for a gate nobody had
built, on the premise that KRT would branch. This file is the gate, and the first test is the
measurement that retired the premise.

Every board here is read **read-only**, including the DS2 Addon's, which lives outside this repo.
Nothing in `~/Documents/MaD` is written by this suite.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from pcbc.ampacity import board_pad_geoms
from pcbc.build import _chain_gate
from pcbc.compile import compile_design
from pcbc.language import load_board
from pcbc.route_geom import hull_dist2
from pcbc.route_verify import chain_order

ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = ROOT / "examples"
DS2 = Path.home() / "Documents" / "MaD" / "Hardware" / "DS2Addon" / "pcbc"
HAS_DS2 = (DS2 / "ds2_addon.py").exists()


def _board(name: str) -> Path:
    return DS2 / "ds2_addon.py" if name == "ds2" else EXAMPLES / name / f"{name}.py"


def _routed(name: str) -> Path:
    if name == "ds2":
        return DS2 / "layout" / "ds2_addon" / "routed" / "layout.kicad_pcb"
    return EXAMPLES / name / "layout" / name / "routed" / "layout.kicad_pcb"


def _ask(name: str, text: str | None = None, **kw):
    design = load_board(_board(name))
    job = compile_design(design)
    return chain_order(text if text is not None else _routed(name).read_text(), design, job.constraints, **kw)


# --- the measurement that retired the hard refusal -------------------------------------------------


@pytest.mark.skipif(not HAS_DS2, reason="the DS2 Addon is not checked out here")
def test_ds2s_declared_chain_is_fed_in_its_order_by_the_router_that_actually_routed_it():
    """`Chain("VDDA", "J2.1", "C4.1", "U1.12")`, read off the board KRT finished: **held**.

    This is the whole of why `chain`'s refusal is soft now. The pattern cannot write link 2 — the
    route needs a six-corner 14.744 mm detour for a 3.8204 mm airwire and the shape set cannot
    express it — and the slice escalated that inability to a build-aborting refusal on the claim that
    KRT would connect the same three pads by branching, which is exactly what the order forbids. The
    board says otherwise: the feed leaves `J2.1`, runs **through** `C4.1`'s pad copper and carries on
    to `U1.12`, which is the sentence the `Chain()` wrote.
    """
    got = {r.net: r for r in _ask("ds2")}
    assert set(got) == {"VDDA"}, got
    r = got["VDDA"]
    assert (r.verdict, r.owner, r.where) == ("held", "chain", "Chain line 97"), r
    assert r.stations == ("J2.1", "C4.1", "U1.12"), "the Chain()'s own order, resolved to pads"
    assert (r.detail, r.move) == ("", ""), "a chain that held has nothing to say"


@pytest.mark.skipif(not HAS_DS2, reason="the DS2 Addon is not checked out here")
def test_the_junction_that_makes_ds2_pass_is_inside_the_caps_copper_and_not_a_junction_at_all():
    """Why the check removes a pad's **region** and not its node in a graph.

    Walk ds2's `VDDA` as a graph of track endpoints and it has one degree-3 vertex, at
    (20.35, 6.45). Read that as a T and the conclusion inverts: the two "through" edges leave it at
    -45.00 and 135.00 degrees — exactly collinear — and the third is a 0.0219 mm stub to the pad
    centre, so as a graph `C4.1` is a **leaf**, and deleting the vertex leaves `J2.1` connected to
    `U1.12`. A cut-vertex reading of "the stop is in the path" fails the one board where the feed
    demonstrably runs across the cap's pad.

    What is actually there is one straight trace crossing the pad plus 21.9 um of redundant copper.
    The numbers below are the tolerance's justification: the junction sits **0.4300 mm inside**
    `C4.1`'s copper, and **1.1185 mm** of the trunk's centre line is inside it — against a 0.1270 mm
    process floor. Nothing about this verdict is close.
    """
    import math
    import re

    text = _routed("ds2").read_text()
    pads = {p.id: p for p in board_pad_geoms(text) if p.net == "VDDA"}
    c4 = pads["C4.1"].copper[0]
    junction = (20.35, 6.45)
    assert round(math.dist(junction, pads["C4.1"].at), 6) == 0.021891, "the stub is 21.9 um, not 12.6"
    # Inside the roundrect outline by (r - distance to the nearest hull edge), which is the number
    # the tolerance has to be smaller than for this board to pass.
    pts = c4.pts
    edge = min(
        abs((junction[0] - a[0]) * (b[1] - a[1]) - (junction[1] - a[1]) * (b[0] - a[0])) / math.dist(a, b)
        for a, b in zip(pts, pts[1:] + pts[:1])
    )
    assert hull_dist2((junction,), pts) == 0.0, "inside the hull, so inside the copper"
    assert round(edge + c4.r, 4) == 0.4300, (edge + c4.r, "the junction is 0.43 mm inside C4.1's copper")
    trunk = [((19.45, 7.35), (20.35, 6.45)), ((20.35, 6.45), (22.75, 4.05))]
    for a, b in trunk:
        assert re.search(rf"\(start {a[0]} {a[1]}\)\s*\(end {b[0]} {b[1]}\)", text), (a, b, "the trunk moved")
    inside = 0.0
    for a, b in trunk:
        n = 200000
        hit = sum(1 for k in range(n) if hull_dist2(((a[0] + (b[0] - a[0]) * (k + 0.5) / n, a[1] + (b[1] - a[1]) * (k + 0.5) / n),), pts) <= c4.r**2)
        inside += math.dist(a, b) * hit / n
    assert round(inside, 3) == 1.118, (inside, "1.1185 mm of trunk centre line inside the pad")


# --- the gate can fail -----------------------------------------------------------------------------


TEE = (
    ("(start 19.45 7.35)\n\t\t(end 20.35 6.45)", "(start 19.45 7.35)\n\t\t(end 19.45 4.05)"),
    ("(start 20.35 6.45)\n\t\t(end 22.75 4.05)", "(start 19.45 4.05)\n\t\t(end 22.75 4.05)"),
    ("(start 20.35 6.45)\n\t\t(end 20.33 6.4411)", "(start 19.45 6.4411)\n\t\t(end 20.33 6.4411)"),
)
"""ds2's routed `VDDA` with the trunk walked round `C4.1` instead of across it.

The same three tracks, re-routed as an L down `x` 19.45 and along `y` 4.05 — 0.3050 mm clear of the
pad's copper at its nearest — with the cap reached by a 0.8800 mm tee off the middle of the vertical
leg. Nothing else on the board moves: same nets, same widths, same layer, same connectivity, and the
netlist is still exactly the netlist `board.py` declares, which is the point. This is the failure
KiCad's own gate is blind to by construction."""


@pytest.mark.skipif(not HAS_DS2, reason="the DS2 Addon is not checked out here")
def test_a_cap_teed_off_the_run_by_millimetres_fails_and_names_the_edit():
    """The other side of the tolerance: a stop the feed goes *round*."""
    text = _routed("ds2").read_text()
    for old, new in TEE:
        assert text.count(old) == 1, old
        text = text.replace(old, new)
    r = {x.net: x for x in _ask("ds2", text)}["VDDA"]
    assert (r.verdict, r.owner) == ("spur", "chain"), r
    assert r.detail == "the copper joins J2.1 to U1.12 without crossing C4.1's pad, so C4.1 hangs off the feed instead of sitting in it", r.detail
    assert r.move == (
        'Place("C4", toward="down") moves it into the corridor between J2.1 and U1.12; '
        'or Chain("VDDA", "J2.1", "C4.1", "U1.12") is asking for an order this copper does not take '
        "— drop C4 from the chain and feed it locally instead."
    ), r.move


@pytest.mark.skipif(not HAS_DS2, reason="the DS2 Addon is not checked out here")
def test_the_build_gate_can_fail_and_does_not_on_the_board_as_routed():
    """A gate that cannot fail is not a gate (`test_examples_fab.py`'s own rule, applied here).

    The same function, the same board, one difference: whether the feed crosses the cap's pad. It
    must be silent on the board as routed and it must stop the build on the board that goes round —
    otherwise "ds2 passes" says nothing about the check and everything about the board.
    """
    design = load_board(_board("ds2"))
    good = _routed("ds2").read_text()
    bad = good
    for old, new in TEE:
        bad = bad.replace(old, new)
    assert _chain_gate(good, design) == {"checked": {"VDDA": "held"}, "fails": [], "notes": []}
    stopped = _chain_gate(bad, design)
    assert stopped["checked"] == {"VDDA": "spur"} and stopped["notes"] == [], stopped
    assert stopped["fails"] == [
        "VDDA chain J2.1 -> C4.1 -> U1.12 (Chain line 97): the copper joins J2.1 to U1.12 without crossing "
        "C4.1's pad, so C4.1 hangs off the feed instead of sitting in it (R-X4). "
        'Place("C4", toward="down") moves it into the corridor between J2.1 and U1.12; or '
        'Chain("VDDA", "J2.1", "C4.1", "U1.12") is asking for an order this copper does not take — drop C4 '
        "from the chain and feed it locally instead."
    ], stopped["fails"]
    # And the line the build would print, which is what an AI reads.
    assert ("a declared chain is not fed in its order: " + "; ".join(stopped["fails"])).endswith("feed it locally instead."), stopped


# --- the four chains R2 hands to R4, and what they measure -----------------------------------------


def test_the_pair_chains_r2_routes_none_of_are_reported_and_never_fatal():
    """B.2 skips a `Chain()` on a `PairSpec` net — "pairs are R4's" — so R2 writes none of that
    copper and KRT writes all of it. The gate still reads them, and two of the four are violated on
    their own checked-in boards: c3_usb's `U3.3` and node's `U3.4` hang off the feed rather than
    sitting in it, while both `USB_DP` chains hold.

    They are **notes and not fails**, on `--strict-power`'s argument in C.6: stopping a board on a
    fault the tool cannot yet repair teaches an author to reach for `--force`. Fixing them is a board
    change plus R4's pair router, and nothing in R2 can take either. What the gate can do is stop
    them being invisible, which is what this test pins.
    """
    for name, held, spur in (("c3_usb", "USB_DP", "USB_DN"), ("node", "USB_DP", "USB_DN")):
        got = {r.net: r for r in _ask(name)}
        assert set(got) == {held, spur}, (name, got)
        assert (got[held].verdict, got[held].owner) == ("held", "R4"), (name, got[held])
        assert (got[spur].verdict, got[spur].owner) == ("spur", "R4"), (name, got[spur])
        gate = _chain_gate(_routed(name).read_text(), load_board(_board(name)))
        assert gate["fails"] == [], (name, "a pair chain never stops the build")
        assert len(gate["notes"]) == 1 and gate["notes"][0].startswith(f"{spur} chain "), gate["notes"]
        assert "R2 routes no pair at all, so this copper is KRT's and R4's" in gate["notes"][0], gate["notes"]
        assert gate["checked"] == {held: "held", spur: "spur"}, (name, gate["checked"])


def test_a_board_with_no_declared_chain_says_nothing():
    for name in ("blinky", "buck"):
        assert _ask(name) == (), name
        assert _chain_gate(_routed(name).read_text(), load_board(_board(name))) == {"checked": {}, "fails": [], "notes": []}, name


# --- the tolerance, and what it is worth -----------------------------------------------------------


SWEEP = [0.0, 0.05, 0.0889, 0.127, 0.2, 0.3, 0.5, 1.0, 2.0]
"""Every tolerance the verdicts were swept at (mm), from "the pad's exact copper" to sixteen times
the coarsest process floor in the repo."""


def test_the_tolerance_decides_nothing_on_any_board_in_this_repo():
    """The number this check dilates a station's pad by is `stackup.clearance_min`, and the argument
    for it is `chain.stub_need`'s (S6 decision 1): copper closer together than a fab's minimum
    clearance is not two separable things. The argument is worth stating because the measurement is
    that it does not matter — no verdict here moves between 0.0 mm and 2.0 mm.

    The nearest flip is node's `USB_DN` at 2.5 mm and c3_usb's at 3.0 mm, where the dilated pad
    swallows the spur and its via whole: 28x and 24x their own process floors (0.0889 and 0.127 mm). So the gap between the closest pass
    (ds2's junction, 0.43 mm *inside* its pad) and the closest fail is about 3 mm wide, and a reader
    deciding whether to trust a verdict is deciding on millimetres, not on a tolerance.
    """
    names = ["c3_usb", "node"] + (["ds2"] if HAS_DS2 else [])
    for name in names:
        base = {r.net: r.verdict for r in _ask(name)}
        for tol in SWEEP:
            assert {r.net: r.verdict for r in _ask(name, floor=tol)} == base, (name, tol, base)
    if HAS_DS2:
        assert base == {"VDDA": "held"}, base
    for name, net, flips in (("node", "USB_DN", 2.5), ("c3_usb", "USB_DN", 3.0)):
        got = {r.net: r.verdict for r in _ask(name, floor=flips)}
        assert got[net] == "held", (name, net, flips, "the dilated pad has swallowed the spur")


# --- the two things the copper cannot answer -------------------------------------------------------


@dataclass(frozen=True)
class _Chain:
    net: str
    pads: tuple[str, ...]
    line: int


@dataclass(frozen=True)
class _Design:
    chains: tuple
    instances: tuple = ()


@pytest.mark.skipif(not HAS_DS2, reason="the DS2 Addon is not checked out here")
def test_a_poured_net_and_a_member_that_names_no_pad_both_say_so_rather_than_passing():
    """Two verdicts that are neither held nor violated, because the copper cannot answer.

    A net with a filled zone joins every pad of itself through the plane, so "the order" is not a
    question the graph can be asked — it is reported and skipped, never failed. And a `Chain()`
    member naming no pad of that net is `unresolved`: `pcbc check` refuses it before the board is
    drawn, and a gate that silently skipped it would be a gate that passes a typo.
    """
    job = compile_design(load_board(_board("ds2")))
    text = _routed("ds2").read_text()
    poured = chain_order(text, _Design(chains=(_Chain("GND", ("U1.4", "C5.2", "J1.2"), 1),)), job.constraints)
    assert [(r.net, r.verdict) for r in poured] == [("GND", "poured")], poured
    assert poured[0].detail.startswith("GND carries a filled zone"), poured[0].detail
    typo = chain_order(text, _Design(chains=(_Chain("VDDA", ("J2.1", "C4.9", "U1.12"), 97),)), job.constraints)
    assert [(r.net, r.verdict, r.detail) for r in typo] == [("VDDA", "unresolved", "C4.9 names no pad of VDDA on this board")], typo


def test_the_answer_is_a_function_of_the_copper_and_not_of_the_order_it_was_read_in():
    """Determinism, the way every other check in this package is asserted: same input, same bytes.

    The graph walk is a set-driven flood, so a verdict that depended on iteration order would be a
    verdict that moved between runs of the same build.
    """
    text = _routed("c3_usb").read_text()
    once = [r.to_dict() for r in _ask("c3_usb", text)]
    for _ in range(3):
        assert [r.to_dict() for r in _ask("c3_usb", text)] == once
