"""S1 of `docs/stitch-plan.md`: measure, place nothing.

Two of the six stitching techniques were argued about for a whole judged panel on the strength of
populations nobody had counted. This file counts them, on the boards as they are checked in, before
a millimetre of copper is designed around either:

- **technique 5, the return via** — `route_verify.return_vias`. Population **zero**, and zero for a
  structural reason no routing improvement fixes. Eleven vias sit on a net carrying
  `Constraint.reference` — seven on node, four on c3_usb — and every one of them is settled by the
  *stackup* before any distance is taken, so not one of them is a distance question at all. Measured
  again on fresh builds, 2026-09-20: node's seven are gone (its pair never leaves F.Cu on today's
  router, so the checked-in file is stale exactly as `docs/stitch-plan.md` §2(o) says), and c3_usb
  has **five**, still every one of them `lost`. The verdict that survives a re-route is the one the
  stackup decides, which is the whole point.
- **technique 1, the parallel power via** — `route_verify.via_parallelism`. Population **one net on
  one board**: node's `VBUS`, a 1 A rail whose every layer change is a single 0.2 mm barrel rated
  0.527 A. Four groups on the checked-in board, **three** on a fresh build — which is why
  `docs/stitch-plan.md` §2(o) tells S4 to re-measure the count rather than copy it.

Every board here is read **read-only**, including the DS2 Addon's, which lives outside this repo.
Nothing in `~/Documents/MaD` is written by this suite.
"""

from __future__ import annotations

import math
from pathlib import Path

import pytest

from pcbc.ampacity import _via_clusters, power_bottlenecks
from pcbc.build import _barrel_gate, _return_gate
from pcbc.compile import compile_design
from pcbc.constraints import clearance_table, return_rules
from pcbc.language import load_board
from pcbc.route_scene import _vias as board_vias
from pcbc.route_scene import plane_targets
from pcbc.route_verify import (
    RETURN_MM,
    _clusters,
    parallel_lines,
    poured_planes,
    referenced_nets,
    return_lines,
    return_vias,
    via_parallelism,
)
from pcbc.stackup import via_amps, vias_per_change

from test_examples_fab import PARALLEL, RETURNS
from test_return_rules import RULES

ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = ROOT / "examples"
DS2 = Path.home() / "Documents" / "MaD" / "Hardware" / "DS2Addon" / "pcbc"
HAS_DS2 = (DS2 / "ds2_addon.py").exists()


def _board(name: str) -> Path:
    return DS2 / "ds2_addon.py" if name == "ds2" else EXAMPLES / name / f"{name}.py"



# A **routed** board is not a pure function of `board.py`: it needs KiCad and KRT and minutes of
# work, and `examples/**/layout/` is gitignored build output that a clean checkout does not have.
# So every test here that reads one is marked `kicad` + `krt` — which is what those markers already
# promise — and skips when the artifact is absent rather than raising `FileNotFoundError` in the
# fast job. Measured: at 77d596b a clean `git archive` checkout failed 45 tests in
# `test_patterns.py` and 4 here, and CI runs `pytest` **before** its first `pcbc build` in both
# jobs, so the suite was green only on a machine that had built the boards before.
pytestmark = [
    pytest.mark.kicad,
    pytest.mark.krt,
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


# Collection must not build: `_routed` is a real KiCad+KRT build, so the list is the boards whose
# **source** is here, and the build happens inside the test that asks for it.
BOARDS = [n for n in ("blinky", "buck", "c3_usb", "node", "ds2") if _board(n).exists()]
"""The boards whose **source** is here, which is now the same as the boards this file can read.

It used to be `_board(n).exists() and _routed(n).exists()` — the boards somebody had happened to
build — so on a machine with no `examples/**/layout/` the file silently recorded nothing, and on a
machine with a *stale* one it recorded that. `_routed` builds, so the only thing that can be missing
now is a `board.py`, and the DS2 Addon's is the only one that ever is (it lives outside this repo).
"""


def _text(name: str) -> str:
    """The routed board, built."""
    return _routed(name).read_text()


def _cs(name: str):
    if not _board(name).exists():
        pytest.skip(f"{name}'s board.py is not checked out here")
    return compile_design(load_board(_board(name))).constraints


# --- technique 5: the population is zero, and it is zero for a reason ------------------------------


@pytest.mark.parametrize("name", BOARDS)
def test_every_via_on_a_referenced_net_is_classified_and_none_of_them_has_a_return(name: str):
    """`RETURNS`, exact, off the checked-in boards.

    The whole of R-Z4's **V**, which has never existed in this repo: `verify_copper`'s D.1 item 6
    asserts that *pcbc* writes no via on a controlled-impedance net, which is a statement about
    pcbc's own copper. All eleven of these are KRT's, and nothing has ever read them.
    """
    rows = return_vias(_text(name), _cs(name))
    got = tuple((r.net, r.at, r.verdict, r.near[2] if r.near else None) for r in rows)
    assert got == RETURNS[name], (name, got, "RETURNS: every via on a net carrying Constraint.reference")


def test_the_one_board_that_has_a_population_has_one_verdict_and_it_is_the_stackups():
    """c3_usb **loses** the reference on every one of its five vias. Not one is a near miss.

    c3_usb is two layers with one pour on B.Cu, so a via that puts the track on B.Cu lands it inside
    the GND pour's own layer: there is no second plane to reach and the reference is simply gone.
    That fact is the stackup's, so it does not move when the router does. It is the whole argument
    for `docs/stitch-plan.md` §8 refusing to ship a placer: the alternative is widening a tolerance
    until something lands, or putting a via on the GND side of a crossing and calling it half done —
    copper that connects nothing while the tool's own report blesses it.

    **This test used to read "the two boards", and node was the second one.** Measured 2026-09-21 on
    a build this test now makes itself (`conftest.routed_board`), node's `USB_DN` and `USB_DP` carry
    **no via at all** — the pair never leaves F.Cu — so its seven `net_change` rows were a property
    of the stale artifact in `examples/**/layout/` and not of anything pcbc produces. node's *rule*
    is untouched and still says `net_change` on both nets, which is the half that is the stackup's
    (`test_the_other_empty_case_is_a_different_sentence_and_a_fresh_node_is_in_it`); the argument
    survives its example, and the count did not.
    """
    node = return_vias(_text("node"), _cs("node"))
    assert node == (), (node, "the pair never leaves F.Cu on a board pcbc built")
    assert {r.net: r.verdict for r in return_rules(_cs("node"))} == {"USB_DN": "net_change", "USB_DP": "net_change"}, (
        "the stackup still says what a layer change would cost; there is no layer change to cost"
    )
    c3 = return_vias(_text("c3_usb"), _cs("c3_usb"))
    assert len(c3) == 5 and {r.verdict for r in c3} == {"lost"}, c3
    assert {(r.from_ref, r.to_ref) for r in c3} == {(("B.Cu", "GND"), None)}, c3
    assert all("cannot be its own reference" in r.why for r in c3), [r.why for r in c3]


@pytest.mark.parametrize("name", [n for n in ("blinky", "buck", "ds2")])
def test_a_board_with_no_controlled_impedance_net_has_an_empty_population_and_says_so(name: str):
    """Empty, and the sentence says empty rather than printing nothing.

    "Nothing printed" and "nothing to print" read the same in a log and are not the same thing, which
    is `plane_checks`' finding 17 asked of a different absence. blinky has no zone on its board at
    all; buck and ds2 have a GND pour and no net that asks for a reference over it.
    """
    if name == "ds2" and not HAS_DS2:
        pytest.skip("the DS2 Addon is not checked out here")
    cs = _cs(name)
    rows = return_vias(_text(name), cs)
    assert rows == () and RETURNS[name] == (), (name, rows)
    assert referenced_nets(cs) == (), (name, "nothing to watch, which is the first of the two empty cases")
    assert return_lines(rows, referenced_nets(cs)) == ["returns: no net on this board carries a reference plane, so no via changes one"]


def test_the_other_empty_case_is_a_different_sentence_and_a_fresh_node_is_in_it():
    """Empty because there is nothing to watch, and empty because what is watched never changed layer.

    The second is not hypothetical and it is the better news. Measured 2026-09-20 on a fresh
    `pcbc build` of node in a temp directory: `USB_DN` and `USB_DP` carry **no via at all** — the pair
    stays on F.Cu for its whole run — so the seven `net_change` rows in `RETURNS["node"]` are a
    property of the **stale** checked-in artifact that `docs/stitch-plan.md` §2(o) already flagged
    (that routed directory has no `patterns_post` step, so it predates S5). A line reading "no net on
    this board carries a reference plane" about that build would be false twice over: node has two
    such nets, and the reason the population is empty is the thing you would want to be told.
    """
    cs = _cs("node")
    assert referenced_nets(cs) == ("USB_DN", "USB_DP"), "node watches two nets whatever the router does"
    assert return_lines((), referenced_nets(cs)) == [
        "returns: USB_DN, USB_DP carry a reference plane and no via at all, so nothing here changes one"
    ]
    # And the classifier itself, given node's own constraints and a board with no via on either net:
    # the same four layers, the same two planes, nothing to classify.
    assert return_vias(_one_net_two_planes().replace('(net 1 "USB_DN")', '(net 1 "SPARE")'), cs) == ()


def test_the_nearest_reference_net_via_on_either_board_is_2_2472_mm_away():
    """The number `RETURN_MM`'s docstring rests on, measured rather than remembered.

    Nearest across all five: c3_usb's `USB_DN` via at (15.45,1.9) to the GND via at (16.25,4),
    **2.2472 mm** — 2.25x `RETURN_MM` and 2.81x the tightest a return via could legally be placed
    (`ClearanceTable.via_pitch("USB_DP","GND",...)` is 0.8 mm on the two-layer stackup, 0.7 mm on
    node's). So even on the board that comes closest, nothing is within reach of the threshold, and
    nothing here is decided by one.

    Re-recorded 2026-09-21: eleven distances and a 1.6279 mm minimum were the stale artifacts', and
    a fresh build has five and 2.2472. The margin **grew**, which is the direction that costs the
    argument nothing — and the argument was never the number, it is that `net_change` and `lost` are
    decided before a distance is taken at all (`test_the_threshold_decides_nothing_on_any_board...`).
    """
    near = []
    for name in ("c3_usb", "node"):
        near += [r.near[2] for r in return_vias(_text(name), _cs(name)) if r.near]
    assert len(near) == 5 and round(min(near), 4) == 2.2472, sorted(near)
    assert min(near) > RETURN_MM, "not one of the five has a reference-net via inside the threshold"
    for name, pitch in (("c3_usb", 0.8), ("node", 0.7)):
        cs = _cs(name)
        stack = cs.stackup
        table = clearance_table(cs)
        assert table.via_pitch("USB_DP", "GND", stack.via_drill, stack.via_diameter, stack.via_drill, stack.via_diameter) == pitch, name


def test_the_threshold_decides_nothing_on_any_board_in_this_repo(monkeypatch):
    """Sweep `RETURN_MM` from zero to ten millimetres: not one verdict moves.

    `chain_order`'s tolerance test makes the same argument and has to sweep to 2.5 mm to find a flip.
    This one cannot find a flip at any distance, because `net_change` and `lost` are decided before a
    distance is taken at all — the sweep is proof that the constant is carrying no weight rather than
    a calibration of it.
    """
    import pcbc.route_verify as rv

    assert {"c3_usb", "node"} <= set(BOARDS), "both boards with a referenced net are always here"
    base = {name: tuple(r.verdict for r in return_vias(_text(name), _cs(name))) for name in BOARDS}
    # 2.2472 is the nearest reference-net via on any board, re-measured 2026-09-21 (it was 1.6279 on
    # the stale artifacts). The sweep steps over it and past it by 4x and nothing moves.
    for tol in (0.0, 0.5, 0.7, 0.8, 1.0, 2.2472, 3.0, 5.0, 10.0):
        monkeypatch.setattr(rv, "RETURN_MM", tol)
        for name in BOARDS:
            got = tuple(r.verdict for r in return_vias(_text(name), _cs(name)))
            assert got == base[name], (name, tol, got, base[name])


def _zone(net_no: int, net: str, layer: str) -> str:
    return (
        f"\t(zone\n\t\t(net {net_no})\n\t\t(net_name \"{net}\")\n\t\t(layer \"{layer}\")\n"
        f"\t\t(filled_polygon\n\t\t\t(layer \"{layer}\")\n\t\t\t(pts (xy 0 0) (xy 60 0) (xy 60 50) (xy 0 50))\n\t\t)\n\t)"
    )


def _one_net_two_planes(*gnd_vias: tuple[float, float]) -> str:
    """node's stackup with `GND` poured on **both** inner layers: the case none of the five boards has.

    `route_scene.plane_targets` measured on all five — blinky/buck/c3_usb/ds2 `(("GND","B.Cu"),)`,
    node `(("GND","In1.Cu"),("3V3","In2.Cu"))` — **no board pours one net on two facing layers**, which
    is also why §8 does not build plane stitching. So the three verdicts that *are* distance questions
    have no board to stand on and need this fixture, because a classifier that cannot reach three of
    its five verdicts on any input is not a classifier.
    """
    vias = "\n".join(f'\t(via (at {x:g} {y:g}) (size 0.35) (drill 0.2) (layers "F.Cu" "B.Cu") (net 2))' for x, y in gnd_vias)
    return (
        '(kicad_pcb\n\t(net 0 "")\n\t(net 1 "USB_DN")\n\t(net 2 "GND")\n'
        + _zone(2, "GND", "In1.Cu")
        + "\n"
        + _zone(2, "GND", "In2.Cu")
        + "\n"
        + '\t(via (at 10 10) (size 0.35) (drill 0.2) (layers "F.Cu" "B.Cu") (net 1))\n'
        + (vias + "\n" if vias else "")
        + ")\n"
    )


def test_the_three_distance_verdicts_are_reachable_and_nothing_in_this_repo_reaches_them():
    """`none`, `far` and `served`, on the fixture that has to exist for them.

    A check whose verdict set is larger than the verdicts it can produce is a check that has not been
    read. These three are what a return via would be judged by once a board pours one net on two
    facing layers — and until one does, the honest statement is that the five boards here produce
    `net_change` and `lost` and nothing else.
    """
    cs = _cs("node")
    assert poured_planes(_one_net_two_planes()) == (("GND", "In1.Cu"), ("GND", "In2.Cu"))
    alone = return_vias(_one_net_two_planes(), cs)
    assert [r.verdict for r in alone] == ["none"] and alone[0].near is None, alone
    assert alone[0].from_ref == ("In1.Cu", "GND") and alone[0].to_ref == ("In2.Cu", "GND"), alone[0]
    served = return_vias(_one_net_two_planes((10.8, 10.0)), cs)[0]
    assert (served.verdict, served.near) == ("served", ("GND", (10.8, 10.0), 0.8)), served
    far = return_vias(_one_net_two_planes((12.0, 10.0)), cs)[0]
    assert (far.verdict, far.near) == ("far", ("GND", (12.0, 10.0), 2.0)), far
    # And the five boards do not: no row anywhere in this repo is one of these three.
    for name in BOARDS:
        assert not {r.verdict for r in return_vias(_text(name), _cs(name))} & {"none", "far", "served"}, name


def test_the_compiled_verdict_predicts_every_via_the_router_wrote():
    """S5's claim, on the finished boards: the **C** predicts the **V**.

    `constraints.return_rules` classifies a layer change from `board.py` with no PCB file; this reads
    the copper KRT actually wrote and asks whether the two agree. Measured 2026-09-21 on builds this
    test makes itself they agree on **all five** vias — c3_usb's five `lost` against a `lost` rule —
    and the prediction is total rather than partial, because a `kept` net's vias can only be a
    distance question and a `pinned` net should carry no via at all.

    The population is five and not eleven because node's seven were the stale artifact's; its rule is
    still `net_change` and there is now no copper to check it against, which is the strongest form
    the claim can take and the weakest evidence for it. That asymmetry is the point of shipping the
    **C** at all: the verdict was knowable before the router ran, and on node it is all there is.

    That is what makes the compile-time sentence worth shipping instead of a placer. The verdict was
    knowable before the router ran; the vias only confirmed it, and each one of them is a via an
    author would rather have been told about while the net was still a line in `board.py`.
    """
    admits = {"lost": {"lost"}, "net_change": {"net_change"}, "kept": {"served", "far", "none"}, "pinned": set()}
    seen = 0
    for name in BOARDS:
        cs = _cs(name)
        rules = {r.net: r.verdict for r in return_rules(cs)}
        assert rules == RULES[name], (name, rules)
        for row in return_vias(_text(name), cs):
            assert row.net in rules, (name, row.net, "a classified via on a net the compiler never watched")
            assert row.verdict in admits[rules[row.net]], (name, row.net, row.verdict, rules[row.net])
            seen += 1
    assert seen == sum(len(RETURNS[n]) for n in BOARDS), (seen, "every via `RETURNS` records is a via the compiler predicted")


def test_the_planes_a_check_reads_are_the_ones_the_file_has_and_not_the_ones_it_was_promised():
    """`poured_planes` reads the zones KiCad filled; `plane_targets` reads the compiled job.

    **This test used to say they disagree on blinky, and on a board pcbc builds they do not.** The
    counterexample was the checked-in `examples/blinky/layout/` — gitignored build output, one
    segment, no `(zone ...)` block at all — and a fresh `pcbc build` of the same `board.py` writes
    four segments, one via and a filled `GND` pour on B.Cu, so `poured_planes` and `plane_targets`
    agree on all five boards. Measured 2026-09-21. The disagreement was a fact about a file nobody
    had rebuilt, which is the same defect this whole file was re-pointed at `conftest.routed_board`
    to stop, and it is worth recording that a *test* was the thing relying on the stale artifact.

    The rule it was written to defend is unchanged and is asserted below on copper instead of on an
    accident: strip the zone out of a finished board and `poured_planes` says so, because it reads
    the file. `docs/stitch-plan.md` §7.1 names the trap — stitch vias into a pour that was never
    written, and nothing anywhere says a word, because KiCad's unconnected-items check is pad to pad.
    `via_parallelism`'s poured-rail exemption and `return_vias`' reference map both follow the
    copper, so neither can be talked out of measuring a net by a declaration that poured nothing —
    the same trap `power_bottlenecks`' `zoned` flag was moved off `job.planes` to avoid (finding 1).
    """
    import re

    for name in BOARDS:
        job = compile_design(load_board(_board(name)))
        text = _text(name)
        assert poured_planes(text) == tuple(sorted(plane_targets(job))), (name, poured_planes(text), plane_targets(job))
    blinky = _text("blinky")
    assert plane_targets(compile_design(load_board(_board("blinky")))) == (("GND", "B.Cu"),), "the compiled job promises a pour"
    assert "(zone" in blinky and blinky.count("(segment") == 4, "and a board pcbc built keeps that promise"
    assert poured_planes(blinky) == (("GND", "B.Cu"),), "so the file and the job agree"
    # The promise broken, which is what the check exists for and what no board in the repo supplies
    # any more: the same file with its zones cut out still compiles to a promised pour.
    stripped = re.sub(r"\n\t\(zone\b.*?\n\t\)", "", blinky, flags=re.S)
    assert "(zone" not in stripped, "the fixture has to actually lose its pour"
    assert poured_planes(stripped) == (), "and then the file says there is no plane, and the file is what a finished-board check reads"


# --- technique 1: one net, one board ---------------------------------------------------------------


@pytest.mark.parametrize("name", BOARDS)
def test_every_parallel_via_group_on_an_unpoured_rail_is_rated(name: str):
    """`PARALLEL`, exact, off the checked-in boards: `{net: ((anchor, n, need), ...)}`."""
    rows = via_parallelism(_text(name), _cs(name))
    got: dict[str, list] = {}
    for r in rows:
        got.setdefault(r.net, []).append((r.at, r.n, r.need))
    assert {k: tuple(v) for k, v in sorted(got.items())} == PARALLEL[name], (name, got, "PARALLEL: the population of technique 1")


def test_technique_one_fires_on_exactly_one_net_on_exactly_one_board():
    """node's `VBUS`: three groups on a 1 A rail, `via_amps(0.2)` = 0.527 A per barrel and
    `vias_per_change(1.0, 0.2)` = 2, so a singleton is short by one and a pair is rated.

    **Re-recorded 2026-09-21 off a board this test builds, and the population went 4 singletons to
    3 groups — because one of them is now pcbc's own rung.** `patterns/stitch.py` placed a twin
    0.9 mm from the anchor at (28,33.4); `_via_clusters` reads the two barrels as one cluster of
    two, `need` is met, and the group drops out of `short`. What is left owed is 2 rungs at
    (27.8,36.3) and (31.3,38.5) — the two groups `STITCH_REFUSED` names as walled in by KRT's copper,
    named there and counted here. That is the technique visible in both directions at once: what it
    placed and what it could not.

    And the negative half, which is the more useful one: ds2 can never trigger it. 0.1 A against one
    0.3 mm barrel's 0.707 A is `per_change` 1, so every group it has is rated by arithmetic and not
    by luck — `docs/stitch-plan.md` §4's claim that the `final` stage writes zero pieces on ds2 for
    *structural* reasons rather than because a bound was tuned until it passed. Its group count moves
    13 -> 14 on a fresh build and **not one `need` moves**, which is what "structural" has to mean.
    """
    assert "node" in BOARDS, "node's board.py is in this repo, and `_routed` builds the rest"
    short = {name: [r for r in via_parallelism(_text(name), _cs(name)) if r.add] for name in BOARDS}
    assert [name for name, rows in short.items() if rows] == ["node"], {k: len(v) for k, v in short.items()}
    node = short["node"]
    assert {r.net for r in node} == {"VBUS"} and len(node) == 2, node
    assert {(r.n, r.drill, r.carries, r.need, r.add) for r in node} == {(1, 0.2, 0.527, 2, 1)}, node
    assert via_amps(0.2, 0.018, 10.0) == 0.527 and vias_per_change(1.0, 0.2, 0.018, 10.0) == 2
    assert sum(r.add for r in node) == 2, "two rungs owed, one per short cluster"
    # And the third group, which is the one pcbc already answered: two barrels, `need` met, no `add`.
    rated = [r for r in via_parallelism(_text("node"), _cs("node")) if not r.add]
    assert [(r.net, r.at, r.n, r.need) for r in rated] == [("VBUS", (28.0, 33.4), 2, 2)], rated
    if HAS_DS2:
        ds2 = via_parallelism(_text("ds2"), _cs("ds2"))
        assert len(ds2) == 14 and {r.need for r in ds2} == {1} and {r.drill for r in ds2} == {0.3}, ds2
        assert vias_per_change(0.1, 0.3, 0.018, 10.0) == 1, "0.1 A under one 0.707 A barrel: no rung can ever be owed"


def test_a_poured_rail_is_exempt_and_the_exemption_is_what_keeps_the_count_honest():
    """Measured on boards this test builds, 2026-09-21: without it, buck's `GND` reads **7**
    under-rated groups and node's `GND` and `3V3` read **42** and **15** — and every one of those
    vias is a tap into the pour that carries the current. A plane is the rail's conductor, which is
    the same sentence `power_bottlenecks` uses to skip a zoned net's path walk, asked of a via
    instead of a track.

    Re-recorded from 5 / 16 / 7, which were the stale `examples/**/layout/` artifacts': those boards
    predate the tap pattern, so they carried a fraction of the plane welds pcbc now writes. **The
    exemption grew by more than a factor of two on node and it is the same exemption**, which is the
    useful form of this number — it says how much a wrong answer here would now cost.

    `all(len(g) == 1)` went with them: node's tap fields are dense enough that 6 of `GND`'s 42
    clusters and 2 of `3V3`'s 15 hold more than one barrel, so the assertion is on the group count
    and on the largest cluster instead. A multi-via cluster on a poured rail is the exemption doing
    *more* work, not less.
    """
    for name, net, amps, groups, biggest in (("buck", "GND", 2.0, 7, 1), ("node", "GND", 1.0, 42, 10), ("node", "3V3", 1.0, 15, 2)):
        text = _text(name)
        cs = _cs(name)
        assert net in {n for n, _lay in poured_planes(text)}, (name, net)
        assert not [r for r in via_parallelism(text, cs) if r.net == net], (name, net, "a poured rail is not counted")
        mine = sorted((v for v in board_vias(text) if v["net"] == net), key=lambda v: v["at"])
        clusters = _clusters(mine)
        drill = min(v["drill"] for v in mine)
        assert len(clusters) == groups and max(len(g) for g in clusters) == biggest, (name, net, sorted(len(g) for g in clusters))
        assert vias_per_change(amps, drill, cs.stackup.via_plating_mm, 10.0) > 1, (name, net, "which is what the exemption is hiding, and rightly")


def test_the_two_cluster_walks_agree_on_every_via_of_every_board():
    """`route_verify._clusters` gives the membership; `ampacity._via_clusters` gives the sizes.

    They are the same single-linkage walk at the same `VIA_PARALLEL_MM` written twice, because the
    ampacity walk needs only "how many carry together" and the parallelism count needs "which ones".
    If they ever disagreed, the current pcbc *reports* a cluster carrying and the vias it counts to
    get there would be about different sets of copper — which is precisely the lie
    `docs/stitch-plan.md` §2(e) refuses to let a bare unjoined twin tell.
    """
    seen: dict[str, int] = {}
    for name in BOARDS:
        text = _text(name)
        by_net: dict[str, list] = {}
        for v in board_vias(text):
            by_net.setdefault(v["net"], []).append(v)
        for net, vs in sorted(by_net.items()):
            vs = sorted(vs, key=lambda v: v["at"])
            mine = {i: len(g) for g in _clusters(vs) for i in g}
            assert mine == _via_clusters(vs), (name, net, mine, _via_clusters(vs))
            seen[name] = seen.get(name, 0) + len(vs)
    # Every via on every board, not only the ones either function goes on to rate: the agreement has
    # to hold where a cluster is a tap field and not a rung, because that is where a merge would
    # silently double a reported ampacity. Measured 2026-09-21 on builds this test makes itself, and
    # re-recorded from 5 / 16 / 40 / 32 with no blinky key at all — those were the stale
    # `examples/**/layout/` artifacts', which predate the tap pattern. A fresh build has 3.4x as many
    # vias on c3_usb and 2.2x on node, and blinky now carries one (its `GND` tap) where it used to
    # contribute no key at all, so the walk is held to agree over **177** vias rather than 93.
    want = {n: c for n, c in (("blinky", 1), ("buck", 7), ("c3_usb", 55), ("node", 86), ("ds2", 28)) if n in BOARDS}
    assert seen == want, (seen, want, "every via on every board, and blinky has one now")


def test_this_is_not_the_same_question_as_the_bottlenecks_kind():
    """The difference that binds S4, measured on boards this test builds rather than on artifacts.

    `power_bottlenecks` walks the worst pad-to-pad **path** and names the one worst piece on it;
    `via_parallelism` counts **every** under-rated group on the net. They are a max and a census, and
    the census is the one S4 has to place against.

    **Re-recorded 2026-09-21 and the example inverted, which is the useful direction.** The stale
    checked-in node reported `kind=track, carries=0.414, width=0.0889` — its narrowest track was
    worse than its barrels, so the under-rated vias were invisible to the path walk, and that was
    this test's illustration. On a board pcbc builds today the path walk lands on a **via**:
    `kind=via`, `carries=0.527`, at `27.8,36.3`, verdict `under current`. So the two questions now
    agree on which piece is worst — and still disagree on how many there are, because
    `via_parallelism` finds **two** short groups on that net and the walk can only ever name one.
    That is the difference stated on the same board instead of on a coincidence.

    The negative half moved with it: c3_usb's `VBUS` bottleneck is a 0.127 mm **track** under pcbc's
    0.15 mm floor and not a via at all, so "the boards whose bottleneck is a via" is ds2 alone, whose
    three via bottlenecks are all `ok` and owe no rung — a via bottleneck is not the same thing as an
    under-rated one (`docs/stitch-plan.md` §2(p)), and ds2 is the board that shows it.
    """
    job = compile_design(load_board(_board("node")))
    text = _text("node")
    row = power_bottlenecks(job, text)["VBUS"]
    assert (row["kind"], row["carries"], row["width_mm"], row["verdict"]) == ("via", 0.527, 0.0, "under current"), row
    assert row["at_mm"].startswith("27.8,36.3 on "), row["at_mm"]
    short = [r for r in via_parallelism(text, job.constraints) if r.add]
    assert len(short) == 2 and (27.8, 36.3) in {r.at for r in short}, "the walk names one of the two the census finds"
    # A via bottleneck is not an under-rated via: ds2's three are every one of them rated.
    if HAS_DS2:
        rows = power_bottlenecks(compile_design(load_board(_board("ds2"))), _text("ds2"))
        vias = {n: r["carries"] for n, r in rows.items() if r["kind"] == "via"}
        assert sorted(vias) == ["3V3", "VDDA", "VSS"] and set(vias.values()) == {0.707}, vias
        assert all(r["verdict"] == "ok" for n, r in rows.items() if r["kind"] == "via"), rows
        assert [r for r in via_parallelism(_text("ds2"), _cs("ds2")) if r.add] == [], "and none of them owes a rung"
    c3 = power_bottlenecks(compile_design(load_board(_board("c3_usb"))), _text("c3_usb"))["VBUS"]
    assert (c3["kind"], c3["width_mm"], c3["verdict"]) == ("track", 0.127, "under floor"), c3
    assert [r for r in via_parallelism(_text("c3_usb"), _cs("c3_usb")) if r.add] == [], "c3_usb owes no rung either"


# --- the reports, and where they run ---------------------------------------------------------------


def test_the_lines_say_what_is_there_including_when_nothing_is():
    """A report nobody can act on is still a report somebody has to read, so the empty case is a
    sentence and not a blank."""
    assert parallel_lines(via_parallelism(_text("blinky"), _cs("blinky"))) == [
        "parallel: no via on any unpoured power net, so there is no group to rate"
    ]
    assert parallel_lines(via_parallelism(_text("c3_usb"), _cs("c3_usb"))) == [
        "parallel: 4 via group(s) on unpoured power nets, every one rated for its declared current"
    ]
    node = parallel_lines(via_parallelism(_text("node"), _cs("node")))
    assert node[0] == "VBUS via group at (27.8,36.3): 1 x 0.2 mm drill carries 0.527 A of 1 A [needs 2, so 1 more]", node[0]
    assert node[-1] == (
        "parallel: 2 of 3 via group(s) under their rail's declared current (VBUS); 2 more via(s) would carry it"
    ), node[-1]
    # node's return line is the **other** empty case and the sentence has to say which: two nets are
    # watched and neither carries a via, which is not the same as nothing being watched.
    assert return_lines(return_vias(_text("node"), _cs("node")), referenced_nets(_cs("node"))) == [
        "returns: USB_DN, USB_DP carry a reference plane and no via at all, so nothing here changes one"
    ]
    ret = return_lines(return_vias(_text("c3_usb"), _cs("c3_usb")))
    assert ret[0] == (
        "USB_DN via at (15.45,1.9) F.Cu->B.Cu [lost]: the via lands the copper in the GND pour's own layer "
        "(B.Cu), which cannot be its own reference; F.Cu was referenced to GND on B.Cu and there is no "
        "second plane to reach; nearest GND via 2.2472 mm away at (16.25,4)"
    ), ret[0]
    assert ret[-1] == "returns: 5 via(s) on a referenced net — 5 lost", ret[-1]


@pytest.mark.parametrize("name", BOARDS)
def test_both_counts_run_in_the_build_and_the_census_half_can_stop_nothing(name: str):
    """`build._return_gate` and `build._barrel_gate`, wired beside `_plane_gate`.

    The return gate is report-only by construction, not by discipline: it returns no `fails` key at
    all, so there is no path by which it stops a build. That was S1's contract for both — the copper
    that is short a via is copper pcbc did not place, and a gate that stops a board on a fault the
    tool cannot repair teaches an author to reach for `--force` (C.6).

    **S4 gives the barrel gate a `fails` key and it stays empty on every board here**, which is the
    same contract said one level more precisely. What it can fail on is not "this rail is short" but
    "pcbc wrote a rung that is not joined to its anchor" — copper pcbc itself wrote doing the
    opposite of what `ampacity._via_clusters` will report about it. These five boards are read
    read-only and carry no pcbc stitch copper at all, so the list is empty for want of a rung and the
    census half is unchanged from S1.

    **S5 gives the return gate `rules` and `mispredicted` and still no `fails`.** The rules are the
    same classification made from `board.py` with no PCB file (`constraints.return_rules`) — R-Z4's
    **C** — and `mispredicted` is the claim that the two halves agree, empty on every board here.
    Report-only stays report-only: the edits these moves ask for (`Board(planes=...)` on node,
    `NetReq(layers=...)` on c3_usb) are board decisions, not routing faults, and a gate cannot repair
    a decision.
    """
    design = load_board(_board(name))
    text = _text(name)
    ret = _return_gate(text, design)
    par = _barrel_gate(text, None, design)
    assert set(ret) == {"watched", "vias", "verdicts", "rules", "mispredicted", "lines"} and set(par) == {"rungs", "groups", "short", "fails", "lines"}, (ret.keys(), par.keys())
    assert "fails" not in ret, name
    assert ret["mispredicted"] == [], (name, ret["mispredicted"])
    assert {r["net"]: r["verdict"] for r in ret["rules"]} == RULES[name], (name, ret["rules"])
    assert par["fails"] == [] and par["rungs"] == [], (name, par["fails"], par["rungs"])
    assert [(v["net"], tuple(v["at"]), v["verdict"], v["near"][2] if v["near"] else None) for v in ret["vias"]] == [tuple(r) for r in RETURNS[name]], name
    assert ret["verdicts"] == ({} if not RETURNS[name] else {RETURNS[name][0][2]: len(RETURNS[name])}), (name, ret["verdicts"])
    short_nets = sorted({g["net"] for g in par["short"]})
    assert short_nets == (["VBUS"] if name == "node" else []), (name, short_nets)


@pytest.mark.parametrize("name", BOARDS)
def test_the_answer_is_a_function_of_the_copper_and_not_of_the_order_it_was_read_in(name: str):
    """Determinism, the way every other check in this package is asserted: same input, same bytes."""
    text = _text(name)
    cs = _cs(name)
    once = ([r.to_dict() for r in return_vias(text, cs)], [r.to_dict() for r in via_parallelism(text, cs)])
    for _ in range(3):
        assert ([r.to_dict() for r in return_vias(text, cs)], [r.to_dict() for r in via_parallelism(text, cs)]) == once, name


def test_the_recorded_dicts_cover_every_board_and_are_internally_consistent():
    """The ledger's own shape: five boards in each, and `need` in `PARALLEL` is the arithmetic and
    not a copied number."""
    assert sorted(RETURNS) == sorted(PARALLEL) == ["blinky", "buck", "c3_usb", "ds2", "node"]
    # Five, not eleven, and one verdict rather than two: node's seven `net_change` rows were the
    # stale `examples/**/layout/` artifact's and a fresh build gives it none (see `RETURNS`). The
    # `net_change` verdict survives in `RULES`, which is compiled from `board.py` and needs no copper.
    assert sum(len(v) for v in RETURNS.values()) == 5, "five signal vias on a referenced net, in total"
    assert {v for rows in RETURNS.values() for _net, _at, v, _mm in rows} == {"lost"}
    assert "net_change" in {r for rules in RULES.values() for r in rules.values()}, (
        "the verdict with no copper behind it is still the compiler's, and node is the board that carries it"
    )
    for name in BOARDS:
        stack = _cs(name).stackup
        for net, rows in PARALLEL[name].items():
            amps = _cs(name).by_net(net).current.amps
            drill = stack.via_drill
            assert {need for _at, _n, need in rows} == {vias_per_change(amps, drill, stack.via_plating_mm, 10.0)}, (name, net, rows)
            assert all(math.dist(a, b) > RETURN_MM for i, (a, _, _) in enumerate(rows) for b, _, _ in rows[i + 1 :]), (name, net, "every recorded group is a singleton, so no two anchors are within a group's reach")
