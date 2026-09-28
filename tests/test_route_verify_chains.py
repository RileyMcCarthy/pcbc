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



# A **routed** board is not a pure function of `board.py`: it needs KiCad and KRT and minutes of
# work, and `examples/**/layout/` is gitignored build output that a clean checkout does not have.
# So every test here is marked `kicad` + `krt` — which is what those markers already promise — and
# `_routed` **builds** the board rather than reading that directory at all. Measured: at 77d596b a
# clean `git archive` checkout failed 45 tests in `test_patterns.py` and 4 here, and CI runs
# `pytest` **before** its first `pcbc build` in both jobs, so the suite was green only on a machine
# that had built the boards before — and was asserting that machine's numbers when it was.
pytestmark = [
    pytest.mark.kicad,
]  # no skipif: `_routed` BUILDS the board (conftest.routed_board), so nothing on disk is needed.

def _routed(name: str) -> Path:
    """Built, never read out of `examples/**/layout/`.

    That directory is gitignored build output, a clean checkout does not have it, and whatever is in
    it is whatever somebody last built — on this machine its buck carries 71 segments where a fresh
    build carries 73. Three of the dicts this file asserts were pinned against it and were wrong
    before anything in this session touched the router; `conftest.routed_board` builds the board
    once per session instead (`kicad` + `krt`, which is what this file is already marked).
    """
    from conftest import routed_board

    return routed_board(name, _board(name))


def _ask(name: str, text: str | None = None, **kw):
    design = load_board(_board(name))
    job = compile_design(design)
    return chain_order(text if text is not None else _routed(name).read_text(), design, job.constraints, **kw)


def _ds2_with_krt_vdda() -> str:
    """The natively emitted DS2 Addon with its `VDDA` copper replaced by the old router's
    (`tests/fixtures/native/ds2_vdda_krt.txt`): the one routed feed in this repo known to run *across*
    a chain station's pad, kept as a fixture for the checker now that the router that drew it is gone."""
    from boardtext import _blocks

    text = _routed("ds2").read_text()
    out, pos = [], 0
    for s, e, h in _blocks(text):
        if h in ("segment", "arc", "via") and '(net "VDDA")' in text[s:e]:
            out.append(text[pos : s - 2])
            pos = e
    out.append(text[pos:])
    kept = "".join(out).rstrip()
    frag = "".join(ln + "\n" for ln in (Path(__file__).parent / "fixtures" / "native" / "ds2_vdda_krt.txt").read_text().splitlines() if not ln.startswith(";"))
    return kept[:-1] + frag + ")\n"


# --- the measurement that retired the hard refusal -------------------------------------------------


@pytest.mark.skipif(not HAS_DS2, reason="the DS2 Addon is not checked out here")
def test_ds2s_declared_chain_is_fed_in_its_order_on_the_native_board():
    """N0: the native router links `Chain("VDDA", "J2.1", "C4.1", "U1.12")` station to station
    (`route_native.net_links`), so the declared order is the order it routes. Measured 2026-09-25 on a
    board this test builds: **held**, at every tolerance swept (see below)."""
    got = {r.net: r for r in _ask("ds2")}
    assert set(got) == {"VDDA"}, got
    r = got["VDDA"]
    assert (r.verdict, r.owner, r.where) == ("held", "chain", "Chain line 97"), r
    assert r.stations == ("J2.1", "C4.1", "U1.12") and (r.detail, r.move) == ("", ""), r


@pytest.mark.skipif(not HAS_DS2, reason="the DS2 Addon is not checked out here")
def test_ds2s_declared_chain_is_fed_in_its_order_by_the_router_that_actually_routed_it():
    """`Chain("VDDA", "J2.1", "C4.1", "U1.12")`, read off the copper the old router finished (the
    fixture spliced into the native board): **held**.

    This is the whole of why `chain`'s refusal is soft now. The pattern cannot write link 2 — the
    route needs a six-corner 14.744 mm detour for a 3.8204 mm airwire and the shape set cannot
    express it — and the slice escalated that inability to a build-aborting refusal on the claim that
    KRT would connect the same three pads by branching, which is exactly what the order forbids. The
    board says otherwise: the feed leaves `J2.1`, runs **through** `C4.1`'s pad copper and carries on
    to `U1.12`, which is the sentence the `Chain()` wrote.
    """
    got = {r.net: r for r in _ask("ds2", _ds2_with_krt_vdda())}
    assert set(got) == {"VDDA"}, got
    r = got["VDDA"]
    assert (r.verdict, r.owner, r.where) == ("held", "chain", "Chain line 97"), r
    assert r.stations == ("J2.1", "C4.1", "U1.12"), "the Chain()'s own order, resolved to pads"
    assert (r.detail, r.move) == ("", ""), "a chain that held has nothing to say"


@pytest.mark.skipif(not HAS_DS2, reason="the DS2 Addon is not checked out here")
def test_the_junction_that_makes_ds2_pass_is_inside_the_caps_copper_and_not_a_junction_at_all():
    """Why the check removes a pad's **region** and not its node in a graph.

    Walk ds2's `VDDA` as a graph of track endpoints and it has one degree-3 vertex, at
    (20.35, 6.45). Read that as a T and the conclusion inverts: two of its three edges are the feed
    passing through — they leave at 135.00 and 0.00 degrees, a plain 45-degree corner — and the
    third is a 0.0219 mm stub to the pad centre, so as a graph `C4.1` is a **leaf**, and deleting the
    vertex leaves `J2.1` connected to `U1.12`. A cut-vertex reading of "the stop is in the path"
    fails the one board where the feed demonstrably runs across the cap's pad.

    (Those two angles were -45.00 and 135.00 — exactly collinear — on the checked-in artifact this
    test used to read, and they are collinear on no board pcbc builds: with the relaxer disabled the
    second edge leaves at **45.00** degrees, toward (20.55,6.65), and slice 1 re-lays it at **0.00**
    along `y` 6.45. So the "exactly collinear" reading was a property of a file nobody had rebuilt.
    None of it matters to the verdict: what makes `C4.1` a leaf is the 21.9 um stub, and what makes
    the verdict `held` is that the feed's own copper covers 0.9987 mm of centre line inside the pad.)

    What is actually there is one straight trace crossing the pad plus 21.9 um of redundant copper.
    The numbers below are the tolerance's justification: the junction sits **0.4300 mm inside**
    `C4.1`'s copper, and **0.9987 mm** of the trunk's centre line is inside it — against a 0.1270 mm
    process floor. Nothing about this verdict is close.

    **Re-measured 2026-09-21 against a board this test builds** (`conftest.routed_board`) rather than
    against `examples/**/layout/`. The junction, the 21.9 um stub and the 0.4300 mm depth are
    byte-identical — they are where `C4.1` is and where KRT lands on it — but the **trunk changed
    shape twice over**. Its second leg used to read `(20.35,6.45)-(22.75,4.05)`, a segment on no
    board pcbc builds and the stale artifact's; with the relaxer disabled the same leg is a 0.2828 mm
    step to `(20.55,6.65)`; and `docs/quality-plan.md` slice 1 pulls that step and the two legs after it
    (0.2828 + 0.1 + 0.5657 mm) into a straight run along `y` 6.45 and one diagonal out (0.5 +
    0.2828). So the copper across the pad is `(19.45,7.35)-(20.35,6.45)` and
    `(20.35,6.45)-(20.85,6.45)`, carrying 0.5687 + 0.4300 mm of centre line inside the pad where
    the stale pair carried 1.1185. The verdict is `held` on all three, and a *test* reading a file
    nobody had rebuilt is the same defect this suite exists to catch.
    """
    import math
    import re

    text = _ds2_with_krt_vdda()
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
    trunk = [((19.45, 7.35), (20.35, 6.45)), ((20.35, 6.45), (20.85, 6.45))]
    for a, b in trunk:
        assert re.search(rf"\(start {a[0]} {a[1]}\)\s*\(end {b[0]} {b[1]}\)", text), (a, b, "the trunk moved")
    inside = 0.0
    for a, b in trunk:
        n = 200000
        hit = sum(1 for k in range(n) if hull_dist2(((a[0] + (b[0] - a[0]) * (k + 0.5) / n, a[1] + (b[1] - a[1]) * (k + 0.5) / n),), pts) <= c4.r**2)
        inside += math.dist(a, b) * hit / n
    assert round(inside, 3) == 0.999, (inside, "0.9987 mm of trunk centre line inside the pad")


# --- the gate can fail -----------------------------------------------------------------------------


TEE = (
    ("(start 19.45 7.35)\n\t\t(end 20.35 6.45)", "(start 19.45 7.35)\n\t\t(end 21.05 7.35)"),
    ("(start 20.35 6.45)\n\t\t(end 20.85 6.45)", "(start 21.05 7.35)\n\t\t(end 21.05 6.45)"),
    ("(start 20.85 6.45)\n\t\t(end 21.05 6.25)", "(start 21.05 6.45)\n\t\t(end 21.05 6.25)"),
    ("(start 20.35 6.45)\n\t\t(end 20.33 6.4411)", "(start 20.33 7.35)\n\t\t(end 20.33 6.4411)"),
)
"""ds2's routed `VDDA` with the trunk walked round `C4.1` instead of across it.

The same four tracks, re-routed as an L along `y` 7.35 and down `x` 21.05 — **0.1450 mm** clear of
the pad's copper at its nearest, against ds2's 0.1270 mm process floor — with the cap reached by a
0.9089 mm tee off the middle of the horizontal leg. Nothing else on the board moves: same nets, same
widths, same layer, same connectivity, and the netlist is still exactly the netlist `board.py`
declares, which is the point. This is the failure KiCad's own gate is blind to by construction.

Rebuilt 2026-09-21 against the copper pcbc now produces: the old patch named
`(20.35,6.45)-(22.75,4.05)`, a segment that exists on no board this repo can build, so it silently
replaced nothing and the "failing" arm was the passing board. A patch that does not apply is a test
that asserts nothing, which is why all four replacements are counted before any is made."""


@pytest.mark.skipif(not HAS_DS2, reason="the DS2 Addon is not checked out here")
def test_a_cap_teed_off_the_run_by_millimetres_fails_and_names_the_edit():
    """The other side of the tolerance: a stop the feed goes *round*."""
    text = _ds2_with_krt_vdda()
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
    good = _ds2_with_krt_vdda()
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


def test_the_pair_chains_are_the_routers_now_and_a_violated_one_is_fatal():
    """The native router routes a pair's declared chain station pair by station pair (`route_pair`),
    so every chain is pcbc's copper and the gate is fatal for all of them (`build._chain_gate`).
    Measured 2026-09-25 on boards this test builds: node's two chains **hold** (both pair links are
    one object each); c3_usb's `USB_DN` holds and its `USB_DP` is a **spur** — the J1 -> U3 link did
    not fit as a pair (U3's DP and DN pins sit either side of its GND pin), and the single-net join
    reaches U3.1 off the feed. So c3_usb's gate fails, naming the edit."""
    for name, want in (("c3_usb", {"USB_DP": "spur", "USB_DN": "held"}), ("node", {"USB_DP": "held", "USB_DN": "held"})):
        got = {r.net: r for r in _ask(name)}
        assert {n: r.verdict for n, r in got.items()} == want, (name, got)
        gate = _chain_gate(_routed(name).read_text(), load_board(_board(name)))
        spurs = sorted(n for n, v in want.items() if v == "spur")
        assert sorted(f.split(" chain ")[0] for f in gate["fails"]) == spurs, gate["fails"]
        assert gate["notes"] == [] and gate["checked"] == want, (name, gate)


def test_a_board_with_no_declared_chain_says_nothing():
    for name in ("blinky", "buck"):
        assert _ask(name) == (), name
        assert _chain_gate(_routed(name).read_text(), load_board(_board(name))) == {"checked": {}, "fails": [], "notes": []}, name


# --- the tolerance, and what it is worth -----------------------------------------------------------


SWEEP = [0.0, 0.05, 0.0889, 0.127, 0.2, 0.3, 0.4]
"""Every tolerance the verdicts were swept at (mm), from "the pad's exact copper" to three times the
coarsest process floor in the repo. Re-recorded 2026-09-25 for the native router (the one spur left,
c3_usb's `USB_DP`, flips at 0.4775 mm; see the test).

Re-recorded 2026-09-21: this used to end at 2.0 mm, and on boards pcbc builds c3_usb's `USB_DP`
flips at **1.64392 mm** — so 2.0 was past the first flip rather than short of it, and the sweep was
asserting the opposite of what it claimed. Bisected over sixty halvings on fresh builds, the flip
points are c3_usb `USB_DP` **1.64392**, node `USB_DN` **1.94995**, c3_usb `USB_DN` **3.29168**, and
node's `USB_DP` and ds2's `VDDA` never (swept to 6 mm)."""


def test_the_tolerance_decides_nothing_on_any_board_in_this_repo():
    """The number this check dilates a station's pad by is `stackup.clearance_min`, and the argument
    for it is `chain.stub_need`'s (S6 decision 1): copper closer together than a fab's minimum
    clearance is not two separable things. The measurement is that it does not matter — no verdict
    here moves between 0.0 mm and 0.4 mm.

    Re-measured 2026-09-25 on natively routed boards (bisected): the one spur left, c3_usb's `USB_DP`,
    flips at **0.4775 mm**, where the dilated pad swallows the spur: 3.76x that board's own 0.127 mm
    process floor. node's two chains and ds2's `VDDA` hold at any tolerance (swept to 6 mm).
    """
    names = ["c3_usb", "node"] + (["ds2"] if HAS_DS2 else [])
    for name in names:
        base = {r.net: r.verdict for r in _ask(name)}
        for tol in SWEEP:
            assert {r.net: r.verdict for r in _ask(name, floor=tol)} == base, (name, tol, base)
    if HAS_DS2:
        assert base == {"VDDA": "held"}, base
    for name, net, below, above in (("c3_usb", "USB_DP", 0.4774, 0.4776),):
        assert {r.net: r.verdict for r in _ask(name, floor=below)}[net] == "spur", (name, net, below, "still a spur a tenth of a micron below")
        assert {r.net: r.verdict for r in _ask(name, floor=above)}[net] == "held", (name, net, above, "the dilated pad has swallowed the spur")
    assert {r.net: r.verdict for r in _ask("node", floor=6.0)} == {"USB_DP": "held", "USB_DN": "held"}, "node's held chains have no flip to find"


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
