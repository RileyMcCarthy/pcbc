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
from pcbc.constraints import clearance_table
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
    pytest.mark.skipif(
        not (EXAMPLES / "c3_usb" / "layout" / "c3_usb" / "routed" / "layout.kicad_pcb").exists(),
        reason="needs routed boards: run `pcbc build` on the examples first",
    ),
]

def _routed(name: str) -> Path:
    if name == "ds2":
        return DS2 / "layout" / "ds2_addon" / "routed" / "layout.kicad_pcb"
    return EXAMPLES / name / "layout" / name / "routed" / "layout.kicad_pcb"


BOARDS = [n for n in ("blinky", "buck", "c3_usb", "node", "ds2") if _board(n).exists() and _routed(n).exists()]
"""The boards this file can actually read, which is not the same as the boards it records.

`examples/**/layout/` is in `.gitignore` — every routed board is a **build output**, rebuilt by CI
with `--force` — and the DS2 Addon lives outside this repo entirely. So a checkout that has not been
built has nothing here to measure, and the honest answer is a skip with a sentence rather than a
`FileNotFoundError` from a test that reads a file it never checked for. The dicts in
`test_examples_fab.py` are the record either way; this is what can be asserted against right now.
"""


def _text(name: str) -> str:
    """The routed board, or a skip saying how to get one."""
    p = _routed(name)
    if not p.exists():
        pytest.skip(f"{name}'s routed board is a build output (examples/**/layout/ is gitignored); run `pcbc build`")
    return p.read_text()


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


def test_the_two_boards_that_have_a_population_have_one_verdict_each_and_it_is_the_stackups():
    """node changes the reference **net**; c3_usb **loses** the reference. Neither is a near miss.

    node is four layers with `GND` on In1.Cu and `3V3` on In2.Cu, so a through via takes the track
    from copper referenced to GND to copper referenced to 3V3 — and a via joins one net to itself, so
    there is no via anywhere that carries that return current. What that pair wants is a capacitor
    between the two planes, which is technique 6 and not a stitch.

    c3_usb is two layers with one pour on B.Cu, so a via that puts the track on B.Cu lands it inside
    the GND pour's own layer: there is no second plane to reach and the reference is simply gone.

    Both facts are the stackup's, so neither moves when the router does. That is the whole argument
    for `docs/stitch-plan.md` §8 refusing to ship a placer: the alternative is widening a tolerance
    until something lands, or putting a via on the GND side of node's crossing and calling it half
    done — copper that connects nothing while the tool's own report blesses it.
    """
    node = return_vias(_text("node"), _cs("node"))
    assert len(node) == 7 and {r.verdict for r in node} == {"net_change"}, node
    assert {(r.from_ref, r.to_ref) for r in node} == {(("In1.Cu", "GND"), ("In2.Cu", "3V3"))}, node
    c3 = return_vias(_text("c3_usb"), _cs("c3_usb"))
    assert len(c3) == 4 and {r.verdict for r in c3} == {"lost"}, c3
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


def test_the_nearest_reference_net_via_on_either_board_is_1_6279_mm_away():
    """The number `RETURN_MM`'s docstring rests on, measured rather than remembered.

    Nearest across all eleven: c3_usb's `USB_DP` via at (15.05,2.75) to the GND via at (16.25,3.85),
    **1.6279 mm** — 1.63x `RETURN_MM` and 2.03x the tightest a return via could legally be placed
    (`ClearanceTable.via_pitch("USB_DP","GND",...)` is 0.8 mm on the two-layer stackup, 0.7 mm on
    node's). So even on the board that comes closest, nothing is within reach of the threshold, and
    nothing here is decided by one.
    """
    near = []
    for name in ("c3_usb", "node"):
        near += [r.near[2] for r in return_vias(_text(name), _cs(name)) if r.near]
    assert len(near) == 11 and round(min(near), 4) == 1.6279, sorted(near)
    assert min(near) > RETURN_MM, "not one of eleven has a reference-net via inside the threshold"
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

    if not {"c3_usb", "node"} & set(BOARDS):
        pytest.skip("neither board with a referenced net is built here; run `pcbc build`")
    base = {name: tuple(r.verdict for r in return_vias(_text(name), _cs(name))) for name in BOARDS}
    for tol in (0.0, 0.5, 0.7, 0.8, 1.0, 1.6279, 2.0, 5.0, 10.0):
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


def test_the_planes_a_check_reads_are_the_ones_the_file_has_and_blinky_is_why_that_matters():
    """`poured_planes` reads the zones KiCad filled; `plane_targets` reads the compiled job.

    They agree on four of the five boards and **disagree on blinky**, which is the whole reason a
    check reading a finished board asks the file. `krt_plan` schedules `gnd_pour` for every two-layer
    board with `GND` among its power nets, so `plane_targets` promises blinky `(("GND","B.Cu"),)` —
    and blinky's checked-in routed board contains **no `(zone ...)` block at all**, on a board whose
    whole copper is one segment between two footprints. `docs/stitch-plan.md` §7.1 names this as the
    trap a guard would fall into: sixteen stitch vias into a pour that was never written, and nothing
    anywhere would say a word, because KiCad's unconnected-items check is pad-to-pad.

    The consequence for S1 is small and exact: `via_parallelism`'s poured-rail exemption and
    `return_vias`' reference map both follow the copper, so neither can be talked out of measuring a
    net by a declaration that poured nothing — the same trap `power_bottlenecks`' `zoned` flag was
    moved off `job.planes` to avoid (its finding 1).
    """
    for name in [n for n in BOARDS if n != "blinky"]:
        job = compile_design(load_board(_board(name)))
        text = _text(name)
        assert poured_planes(text) == tuple(sorted(plane_targets(job))), (name, poured_planes(text), plane_targets(job))
    blinky = _text("blinky")
    assert plane_targets(compile_design(load_board(_board("blinky")))) == (("GND", "B.Cu"),), "the compiled job promises a pour"
    assert "(zone" not in blinky and blinky.count("(segment") == 1, "and the routed board has one segment and no zone at all"
    assert poured_planes(blinky) == (), "so the file says there is no plane, and the file is what a finished-board check reads"


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
    """node's `VBUS`: four singleton groups, `via_amps(0.2)` = 0.527 A each against a 1 A rail,
    `vias_per_change(1.0, 0.2)` = 2 — **four rungs**, which is the count S4 has to place.

    And the negative half, which is the more useful one: ds2 can never trigger it. 0.1 A against one
    0.3 mm barrel's 0.707 A is `per_change` 1, so every group it has is rated by arithmetic and not
    by luck — `docs/stitch-plan.md` §4's claim that the `final` stage writes zero pieces on ds2 for
    *structural* reasons rather than because a bound was tuned until it passed.
    """
    if "node" not in BOARDS:
        pytest.skip("node's routed board is a build output; run `pcbc build`")
    short = {name: [r for r in via_parallelism(_text(name), _cs(name)) if r.add] for name in BOARDS}
    assert [name for name, rows in short.items() if rows] == ["node"], {k: len(v) for k, v in short.items()}
    node = short["node"]
    assert {r.net for r in node} == {"VBUS"} and len(node) == 4, node
    assert {(r.n, r.drill, r.carries, r.need, r.add) for r in node} == {(1, 0.2, 0.527, 2, 1)}, node
    assert via_amps(0.2, 0.018, 10.0) == 0.527 and vias_per_change(1.0, 0.2, 0.018, 10.0) == 2
    assert sum(r.add for r in node) == 4, "four rungs, one per cluster"
    if HAS_DS2:
        ds2 = via_parallelism(_text("ds2"), _cs("ds2"))
        assert len(ds2) == 13 and {r.need for r in ds2} == {1} and {r.drill for r in ds2} == {0.3}, ds2
        assert vias_per_change(0.1, 0.3, 0.018, 10.0) == 1, "0.1 A under one 0.707 A barrel: no rung can ever be owed"


def test_a_poured_rail_is_exempt_and_the_exemption_is_what_keeps_the_count_honest():
    """Measured: without it, buck's `GND` reads five under-rated groups and node's `GND` and `3V3`
    read sixteen and seven — and every one of those vias is a tap into the pour that carries the
    current. A plane is the rail's conductor, which is the same sentence `power_bottlenecks` uses to
    skip a zoned net's path walk, asked of a via instead of a track.
    """
    for name, net, amps, groups in (("buck", "GND", 2.0, 5), ("node", "GND", 1.0, 16), ("node", "3V3", 1.0, 7)):
        text = _text(name)
        cs = _cs(name)
        assert net in {n for n, _lay in poured_planes(text)}, (name, net)
        assert not [r for r in via_parallelism(text, cs) if r.net == net], (name, net, "a poured rail is not counted")
        mine = sorted((v for v in board_vias(text) if v["net"] == net), key=lambda v: v["at"])
        clusters = _clusters(mine)
        drill = min(v["drill"] for v in mine)
        assert len(clusters) == groups and all(len(g) == 1 for g in clusters), (name, net, clusters)
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
    # silently double a reported ampacity. Measured 2026-09-20 on the checked-in boards.
    want = {n: c for n, c in (("buck", 5), ("c3_usb", 16), ("node", 40), ("ds2", 32)) if n in BOARDS}
    assert seen == want, (seen, want, "blinky has no via at all, so it contributes no key")


def test_this_is_not_the_same_question_as_the_bottlenecks_kind():
    """The difference that binds S4, and it is measurable on these very files.

    `power_bottlenecks` reports the worst piece on the worst pad-to-pad **path**, so a net whose
    narrowest track is worse than its barrels reports `kind == "track"` and the under-rated via
    underneath is invisible. node's `VBUS` on the checked-in board is exactly that — `kind=track`,
    `carries=0.414`, `width=0.0889` — while four of its layer changes are single 0.527 A barrels on a
    1 A rail. `docs/stitch-plan.md` §2(o) flagged these artifacts as stale (the routed directory has
    no `patterns_post` step, so they predate S5) and warned S4 to re-measure rather than copy; the
    count of under-rated groups survives that staleness because it does not depend on which piece
    happens to bind today.
    """
    job = compile_design(load_board(_board("node")))
    text = _text("node")
    row = power_bottlenecks(job, text)["VBUS"]
    assert (row["kind"], row["carries"], row["width_mm"]) == ("track", 0.414, 0.0889), row
    assert len([r for r in via_parallelism(text, job.constraints) if r.add]) == 4, "the barrels are short whatever binds"
    # And the two boards whose bottleneck **is** a via are the ones that pass: a via bottleneck is
    # not the same thing as an under-rated one (`docs/stitch-plan.md` §2(p)).
    for name in ("c3_usb",) + (("ds2",) if HAS_DS2 else ()):
        rows = power_bottlenecks(compile_design(load_board(_board(name))), _text(name))
        assert any(r["kind"] == "via" for r in rows.values()), name
        assert [r for r in via_parallelism(_text(name), _cs(name)) if r.add] == [], name


# --- the reports, and where they run ---------------------------------------------------------------


def test_the_lines_say_what_is_there_including_when_nothing_is():
    """A report nobody can act on is still a report somebody has to read, so the empty case is a
    sentence and not a blank."""
    assert parallel_lines(via_parallelism(_text("blinky"), _cs("blinky"))) == [
        "parallel: no via on any unpoured power net, so there is no group to rate"
    ]
    assert parallel_lines(via_parallelism(_text("c3_usb"), _cs("c3_usb"))) == [
        "parallel: 5 via group(s) on unpoured power nets, every one rated for its declared current"
    ]
    node = parallel_lines(via_parallelism(_text("node"), _cs("node")))
    assert node[0] == "VBUS via group at (26.9,34.7): 1 x 0.2 mm drill carries 0.527 A of 1 A [needs 2, so 1 more]", node[0]
    assert node[-1] == (
        "parallel: 4 of 4 via group(s) under their rail's declared current (VBUS); 4 more via(s) would carry it"
    ), node[-1]
    ret = return_lines(return_vias(_text("node"), _cs("node")))
    assert ret[0] == (
        "USB_DN via at (16.3,12.5) F.Cu->B.Cu [net_change]: the reference changes net across the via, "
        "GND on In1.Cu -> 3V3 on In2.Cu, and a via joins one net to itself; nearest GND via 2.1932 mm away at (17.9,11)"
    ), ret[0]
    assert ret[-1] == "returns: 7 via(s) on a referenced net — 7 net_change", ret[-1]


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
    """
    design = load_board(_board(name))
    text = _text(name)
    ret = _return_gate(text, design)
    par = _barrel_gate(text, None, design)
    assert set(ret) == {"watched", "vias", "verdicts", "lines"} and set(par) == {"rungs", "groups", "short", "fails", "lines"}, (ret.keys(), par.keys())
    assert "fails" not in ret, name
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
    assert sum(len(v) for v in RETURNS.values()) == 11, "eleven signal vias on a referenced net, in total"
    assert {v for rows in RETURNS.values() for _net, _at, v, _mm in rows} == {"lost", "net_change"}
    for name in BOARDS:
        stack = _cs(name).stackup
        for net, rows in PARALLEL[name].items():
            amps = _cs(name).by_net(net).current.amps
            drill = stack.via_drill
            assert {need for _at, _n, need in rows} == {vias_per_change(amps, drill, stack.via_plating_mm, 10.0)}, (name, net, rows)
            assert all(math.dist(a, b) > RETURN_MM for i, (a, _, _) in enumerate(rows) for b, _, _ in rows[i + 1 :]), (name, net, "every recorded group is a singleton, so no two anchors are within a group's reach")
