"""check → sch → place → route → fab, native: no board file is an intermediate. No Zener."""

from __future__ import annotations

import shutil
from pathlib import Path

from .circuit import check_design
from .compile import CompiledJob, compile_design
from .fab import fab_job
from .language import load_board
from .netcheck import KicadMissing, check_copper, check_erc, check_schematic, kicad_drc
from .project import layout_dir
from .sch_emit import emit_schematic_file
from .trace import drift as trace_drift
from .trace import sha as _trace_sha
from .trace import tracing

STAGES = ("check", "sch", "place", "route", "fab")


def _plane_gate(routed_text: str, doc, design=None) -> dict:
    """D.5, run in the product: every tap via inside its own plane, joined by its stub to the pad it
    was placed for, and every plane still one island.

    The islands are reported as `{(net, layer): count}` with their filled **area**, because the count
    alone cannot see a fragment the fill deleted (`route_verify.plane_area`, finding 9). The area is
    a number to read and to pin, never a pass/fail here: what a plane is *worth* is the board's, and
    only a recorded baseline (`test_examples_fab.py::PLANES`) can say whether a given mm2 is right.

    The stub: a tap is a via and the track from its pad to it, and `pcbc:tap:<owner>` names the
    pad. A via 12 mm from that pad, inside the plane, passed the ring check while welding a pad it
    was never placed for (the second review's m6); with `design` given, the via must be joined to
    its owner pad by tracks and vias alone (`layout_check.copper_graph` without the pours).
    """
    from .route_emit import via_piece
    from .route_verify import plane_area, plane_checks, plane_islands

    pieces = []
    for i in (doc.items if doc is not None else []):
        if i.get("key", [""])[0] != "via":
            continue
        pieces.append(via_piece(i["net"], i["reason"], tuple(i["key"][1]), float(i.get("w") or 0.0), float(i.get("drill") or 0.0), owner=i.get("owner", "")))
    islands = plane_islands(routed_text)
    fails = plane_checks(routed_text, pieces)
    fails += [f"the {net} plane on {layer} is {n} islands, so a fragment has been cut off it (D.5)" for (net, layer), n in sorted(islands.items()) if n > 1]
    if design is not None:
        fails += tap_stubs(routed_text, doc, design)
    return {
        "islands": {f"{net} {layer}": n for (net, layer), n in sorted(islands.items())},
        "area_mm2": {f"{net} {layer}": a for (net, layer), a in sorted(plane_area(routed_text).items())},
        "fails": fails,
    }


def tap_stubs(routed_text: str, doc, design) -> list[str]:
    """Every `pcbc:tap:<REF.PAD>` via joined to that pad by tracks and vias (no pour): the stub the
    tap pattern wrote. One sentence per via that is not."""
    from .gen import decompile
    from .layout_check import copper_graph
    from .layout_job import board_pads

    taps = [i for i in (doc.items if doc is not None else []) if i.get("reason") == "tap" and i.get("key", [""])[0] == "via" and i.get("owner")]
    if not taps:
        return []
    board = decompile(routed_text)
    pads, layers = board_pads(design, routed_text)
    nodes, find = copper_graph(board.copper, pads, layers, pours=False)
    by_uuid = {n[3].uuid: k for k, n in enumerate(nodes) if hasattr(n[3], "uuid") and getattr(n[3], "uuid", None)}
    # A pad number names every primitive of the pad: KiCad lets a footprint repeat a number (node's
    # U1.49 is nine copper rectangles), and the tap is joined to the pad when it reaches any of them.
    by_pad: dict[str, list[int]] = {}
    for k, n in enumerate(nodes):
        if hasattr(n[3], "ref") and hasattr(n[3], "num"):
            by_pad.setdefault(f"{n[3].ref}.{n[3].num}", []).append(k)
    out = []
    for i in taps:
        v, pad = by_uuid.get(i.get("uuid")), by_pad.get(str(i["owner"]))
        if v is None or not pad:
            out.append(f"tap {i['net']}: the via for {i['owner']} at ({i['key'][1][0]:g},{i['key'][1][1]:g}) or that pad is not on the board")
            continue
        if find(v) not in {find(k) for k in pad}:
            out.append(f"tap {i['net']}: the via at ({i['key'][1][0]:g},{i['key'][1][1]:g}) is not joined to {i['owner']} by any track: it is not that pad's tap (D.5)")
    return out


def _chain_gate(routed_text: str, design) -> dict:
    """R-X4's verify half, run in the product: every declared `Chain()`'s order, read off the copper.

    The gate the chain pattern's hard refusal was standing in for. Until this function the pattern
    refused a link it could not write and **nothing** read the finished board to ask whether the
    order the author declared had survived in it: `docs/router-plan.md` line 202 tags R-X4 "**P**
    (chain), **V**" and only the P existed (`docs/r2-measurements.md`, S6). Wired the way `_plane_gate`
    is — one parse of a file already on disk, in the route step, with a move for its error.

    **Fatal for every declared chain.** Until the native router a chain on a pair net was the old
    router's copper and only reported (c3_usb's and node's `USB_DN` shipped with `U3.3`/`U3.4` hanging
    off the feed, measured 2026-09-20). The native router links a declared chain station to station
    before any other link of the net (`route_native.net_links`), so every chain is pcbc's copper and
    a violated order is pcbc's fault: it fails the build.
    """
    from .compile import compile_design
    from .route_verify import chain_order

    job = compile_design(design)
    rows = chain_order(routed_text, design, job.constraints) if job.constraints is not None else ()
    bad = [r for r in rows if r.verdict in ("spur", "open")]
    return {
        "checked": {r.net: r.verdict for r in rows},
        # Fatal for every declared chain: the native router links a chain station to station
        # (`route_native.net_links`), pair nets included, so every copper a chain names is pcbc's.
        "fails": [f"{r.net} chain {' -> '.join(r.stations)} ({r.where}): {r.detail} (R-X4). {r.move}" for r in bad],
        "notes": [f"{r.net} chain ({r.where}): {r.detail}" for r in rows if r.verdict in ("poured", "unresolved")],
    }


def _pair_gate(routed_text: str, design) -> dict:
    """Every differential pair's coupling, measured on the emitted board (`route_verify.pair_coupling`),
    **fatal when its uncoupled length exceeds `PairSpec.uncoupled_mm`** (docs/native-plan.md, critique
    #2): a pair routed as two nets passes every KiCad pair rule by ceasing to be a pair, so the build
    measures it itself. Skew is reported beside it and not gated: a pair net's extra pads (a USB-C
    connector's flip side) add length to one half that no coupling could remove."""
    from .compile import compile_design
    from .route_verify import pair_coupling

    cs = compile_design(design).constraints
    rows = pair_coupling(routed_text, cs) if cs is not None else ()
    return {
        "pairs": {f"{r.a}/{r.b}": {"len": [r.len_a, r.len_b], "coupled": [r.coupled_a, r.coupled_b], "uncoupled": r.uncoupled, "skew": r.skew} for r in rows},
        "lines": [r.line() for r in rows],
        "fails": [
            f"{r.line()}. Give the pair a lane both halves fit in (move what sits between its chain stations in board.py), or lock its copper in layout.core.py"
            for r in rows
            if not r.ok
        ],
    }


def _return_gate(routed_text: str, design) -> dict:
    """R-Z4's **V**, run in the product and **never fatal** (`docs/stitch-plan.md` §6 row 5, S1).

    Every via on a net carrying `Constraint.reference`, classified by what its layer change does to
    the return current. Wired the way `_plane_gate` and `_chain_gate` are — one parse of a file
    already on disk, in the route step — and deliberately without a `fails` key, because there is
    nothing here a build should stop on: the verdict on all eleven vias of the two boards that have
    any is settled by the **stackup**, and the edits that change it (`Board(planes=...)` on node,
    `NetReq(layers=...)` on c3_usb) are board decisions rather than routing faults. S1 owns counting
    them, so that the placer `docs/stitch-plan.md` §8 refuses to build is refused against a measured
    population and not an assumed one.

    **S5 added the other half and it is the valuable one.** `constraints.return_rules` is the same
    classification made from `board.py` **before any copper exists** — R-Z4's missing **C** — and this
    gate now carries it (`rules`) beside the vias, prints its moves, and records whether the finished
    board agreed (`mispredicted`, empty on every board measured 2026-09-21). A via that has already
    been routed is expensive news; a sentence in `pcbc check --constraints` saying this pair must not
    change layers costs nothing and arrives before the router runs.
    """
    from .compile import compile_design
    from .constraints import return_rules
    from .route_verify import referenced_nets, return_lines, return_vias

    job = compile_design(design)
    cs = job.constraints
    watched = referenced_nets(cs) if cs is not None else ()
    rows = return_vias(routed_text, cs) if cs is not None else ()
    rules = return_rules(cs) if cs is not None else ()
    verdicts: dict[str, int] = {}
    for r in rows:
        verdicts[r.verdict] = verdicts.get(r.verdict, 0) + 1
    # S5's own claim, checked here rather than argued: the **C** predicts the **V**. A rule computed
    # from `board.py` with no PCB file says `lost` or `net_change` for a net, and every via the router
    # then put on that net must carry that same verdict; a `kept` net's vias can only be a distance
    # question (`served`/`far`/`none`), and a `pinned` net should carry no via at all. Measured
    # 2026-09-21 on the checked-in routed boards, it holds on all eleven: node's seven `net_change`
    # against a `net_change` rule, c3_usb's four `lost` against a `lost` rule. Reported, never fatal —
    # the two readings can honestly differ, because the rule reads the pours the board *will* have and
    # `return_vias` reads the zones KiCad actually filled (blinky's routed board has no zone at all).
    admits = {"lost": {"lost"}, "net_change": {"net_change"}, "kept": {"served", "far", "none"}, "pinned": set()}
    by_rule = {r.net: r for r in rules}
    missed = [
        f"{r.net} via at ({r.at[0]:g},{r.at[1]:g}) is {r.verdict}, and the compiler said {by_rule[r.net].verdict} from board.py alone"
        for r in rows
        if r.net in by_rule and r.verdict not in admits[by_rule[r.net].verdict]
    ]
    return {
        "watched": list(watched),
        "vias": [r.to_dict() for r in rows],
        "verdicts": dict(sorted(verdicts.items())),
        "rules": [r.to_dict() for r in rules],
        "mispredicted": missed,
        "lines": return_lines(rows, watched) + [r.line() for r in rules],
    }


def _bridge_gate(routed_text: str, design) -> dict:
    """Technique 6's verify half, run in the product: what joins each pair of `Ground()` nets.

    Wired the way `_plane_gate` and `_chain_gate` are — one parse of a file already on disk, in the
    route step, with a move for its error — and **fatal for `multi` alone**
    (`docs/stitch-plan.md` §6 row 6). The reasoning behind that one-verdict list is worth keeping:

    - `multi` is fatal because it is the only thing here KiCad cannot see. To KiCad two declared nets
      joined by two separate 0R parts are two nets and two parts; there is no violation to report,
      and the board ships with a ground loop nobody wrote down. pcbc owns the single-point check
      precisely because it is the half `fab.copper_drc_errors` structurally cannot make.
    - `open` is **not** fatal, and not out of leniency: a tie whose pad no copper reaches is a KiCad
      unconnected item, so `netcheck.check_copper` already stops that build two lines above this one.
      A second fatal gate for it would only change which sentence the author reads.
    - `none` is not fatal because the DS2 Addon ships that way and builds green today; failing it
      here would stop a board on a defect its author has not been told about, which is H.3's
      promotion procedure and `--strict-power`'s precedent. The move is printed, exit 0.

    Nothing here writes copper, and `netcheck.py` has a zero diff in the slice that added it.
    """
    from .compile import compile_design
    from .route_verify import bridge_lines, bridge_ties

    job = compile_design(design)
    rows = bridge_ties(routed_text, design, job.constraints) if job.constraints is not None else ()
    return {
        "ties": [r.to_dict() for r in rows],
        "verdicts": {f"{r.a}|{r.b}": r.verdict for r in rows},
        "fails": [f"{r.a} and {r.b} are tied at more than one point: {r.why}. {r.move}" for r in rows if r.verdict == "multi"],
        "notes": [r.line() for r in rows if r.verdict != "tied"],
        "lines": bridge_lines(rows),
    }


def _barrel_gate(routed_text: str, doc, design) -> dict:
    """Technique 1's two halves on the finished board: what the rails have, and what pcbc wrote.

    S1 shipped this as `_parallel_gate`, a census with nothing to check; S4 gives it a rung and the
    one question that can fail. Wired the way `_plane_gate` is — one parse of a file already on
    disk, in the route step, off the sidecar KiCad has already refilled and saved around.

    **Fatal for exactly one thing: a rung that is not joined to its anchor.**
    `ampacity._via_clusters` counts proximity with no connectivity test, so an unjoined twin makes
    pcbc's own measurement report twice the ampacity of a board carrying no more current — and
    nothing else on the board can see it. KiCad's unconnected-items check is pad to pad, so a via on
    a named net welded to nothing passes DRC; `netcheck.check_copper` reads pad bindings inside
    footprint blocks and never a segment or a via; `verify_copper` asks about clearance, angle and
    size. That is copper pcbc itself wrote doing the opposite of what pcbc reports, which is the
    definition of a bug this tool should stop a build on.

    **Not fatal: a rail that is still short.** `via_parallelism`'s census is a measurement of the
    board, and a rail short a barrel after this stage is short because the pattern **refused** —
    every site on the ring blocked by copper the router put there — and the refusal is already
    printed as a move with its blockers. Stopping the build there would stop a board that builds
    today on a fault whose repair the tool has just been unable to make, which is C.6's argument and
    `--strict-power`'s precedent; `ampacity.power_moves` says the same thing once more in the fab
    step. The one exception is the case that would be a tool bug rather than a board fact: **every**
    spec on a net succeeded, pcbc wrote every rung its own arithmetic asked for, and the finished
    board still reads that rail's bottleneck as a single barrel under its declared current. Then the
    copper went in and the measurement did not move, and one of the two is wrong.

    Measured 2026-09-21: node writes one rung, is joined to its anchor on both layers it spans, and
    two of its three `VBUS` groups refused — so the rail's own verdict is a note here and a move in
    the fab step, and `fails` is empty. The other four boards write no rung at all.
    """
    from .compile import compile_design
    from .route_emit import seg_piece, via_piece
    from .route_verify import parallel_joined, parallel_joined_lines, parallel_lines, via_parallelism

    job = compile_design(design)
    pieces = []
    for i in (doc.items if doc is not None else []):
        if i.get("reason") != "stitch":
            continue
        key = i.get("key") or [""]
        if key[0] == "via":
            pieces.append(via_piece(i["net"], i["reason"], tuple(key[1]), float(i.get("w") or 0.0), float(i.get("drill") or 0.0), owner=i.get("owner", "")))
        elif key[0] == "seg":
            pieces.append(seg_piece(i["net"], i["reason"], key[1], tuple(key[2]), tuple(key[3]), float(i.get("w") or 0.0), owner=i.get("owner", "")))
    rungs = parallel_joined(routed_text, pieces)
    rows = via_parallelism(routed_text, job.constraints) if job.constraints is not None else ()
    # Which nets pcbc set out to stitch and finished: a stitch refusal names the net it was on, so a
    # net with copper here and no refusal is one where every spec was served.
    refused = {r.get("net") for r in (doc.refusals if doc is not None else []) if r.get("pattern") == "stitch"}
    served = {r.net for r in rungs} - refused
    fails = [f"pcbc wrote a parallel via that is not parallel: {r.line()}. Drop the rung or join it: this is a pcbc bug, not a board move" for r in rungs if not r.ok]
    stalled = []
    if job.constraints is not None and served:
        from .ampacity import CURVE_EPS, power_bottlenecks

        for net, row in sorted(power_bottlenecks(job, routed_text).items()):
            if net not in served or row["zoned"]:
                continue
            carries = row["carries"] or 0.0
            if row["kind"] == "via" and carries + 1e-9 < row["amps"] * (1.0 - CURVE_EPS):
                stalled.append(
                    f"{net}: pcbc placed every rung vias_per_change asked for and the worst path still carries "
                    f"{carries:g} A of {row['amps']:g} A through one barrel at {row['at_mm']} — the copper went in "
                    f"and the measurement did not move, so one of the two is wrong"
                )
    return {
        "rungs": [r.to_dict() for r in rungs],
        "groups": [r.to_dict() for r in rows],
        "short": [r.to_dict() for r in rows if r.add],
        "fails": fails + stalled,
        "lines": parallel_joined_lines(rungs) + parallel_lines(rows),
    }


def _guard_gate(routed_text: str, doc) -> dict:
    """Technique 3's verify half on the finished board: is the shield actually ground?

    Wired the way `_plane_gate`, `_barrel_gate` and `_thermal_gate` are — one parse of a file already
    on disk, in the route step, off the sidecar KiCad has already refilled and saved around.

    **Fatal for one thing, and it is the one nothing else on this board can see.** A guard is `GND`
    copper that is only `GND` because a stitch via ties it to the pour. Take the via away and what is
    left is a track on a named net, welded to nothing — and KiCad's unconnected-items check is pad to
    pad, `netcheck.check_copper` reads pad bindings inside footprint blocks and never a segment,
    `verify_copper` asks about clearance, angle and size, and `copper_bar` counts its millimetres like
    any other copper. It would pass every gate pcbc has and shield nothing. `docs/stitch-plan.md`
    section 6 row 3 names the exact trap: blinky's routed board has **zero** `(zone ...)` blocks, so a
    guard there would put sixteen vias into a pour that was never written and not one line of output
    would say so.

    **Not fatal: a shield that is short.** `Guard()` is a target under R-S3 — B.6 makes it the pattern
    that may apply partially — so a run with 21.4 mm of shield where 32.4 mm was possible is one
    `style:` note carrying its `Coverage`, and the stretches it lost are `Place()` decisions rather
    than routing faults. Measured on `tests/fixtures/guard/guard.py`: the pattern drops a stretch that
    got no via *before* writing it, precisely so this gate never has one to fail on, and the two
    numbers agree by construction rather than by luck.

    The four example boards declare no `Guard()`, so `rows` is empty on every one of them and this
    returns the sentence that says so.
    """
    from .route_emit import seg_piece, via_piece
    from .route_verify import guard_cover, guard_lines

    pieces = []
    for i in (doc.items if doc is not None else []):
        if i.get("reason") != "guard":
            continue
        key = i.get("key") or [""]
        if key[0] == "via":
            pieces.append(via_piece(i["net"], i["reason"], tuple(key[1]), float(i.get("w") or 0.0), float(i.get("drill") or 0.0), owner=i.get("owner", "")))
        elif key[0] == "seg":
            pieces.append(seg_piece(i["net"], i["reason"], key[1], tuple(key[2]), tuple(key[3]), float(i.get("w") or 0.0), owner=i.get("owner", "")))
    rows = guard_cover(routed_text, pieces)
    fails = [
        f"pcbc wrote a guard that is not ground: {r.line()}. Drop the stretch or stitch it: this is a "
        f"pcbc bug, not a board move"
        for r in rows
        if not r.ok
    ]
    return {
        "guards": [r.to_dict() for r in rows],
        "cover": {r.net: r.mm for r in rows},
        "fails": fails,
        "lines": guard_lines(rows),
    }


def _stitch_gate(routed_text: str, doc) -> dict:
    """Technique 2's verify half on the finished board: does the lattice land in **both** pours?

    Wired the way `_plane_gate`, `_barrel_gate`, `_thermal_gate` and `_guard_gate` are — one parse of
    a file already on disk, in the route step, off the sidecar KiCad has already refilled and saved
    around.

    **Fatal for one thing, and it is the one nothing else on this board can see.** A lattice barrel
    exists to tie two pours of one net together; a barrel whose ring is inside the front pour and
    inside a clearance hole in the back one ties one plane to nothing, and it still reads as connected
    everywhere else — KiCad's unconnected-items check is pad to pad, `netcheck.check_copper` reads pad
    bindings inside footprint blocks and never a via, `verify_copper` asks about clearance, angle and
    size, and `copper_bar` counts it like any other via. That is pcbc's own copper doing the opposite
    of what pcbc reports, which is the definition of a bug a build should stop on, and it is the same
    sentence `_guard_gate` makes about a shield welded to nothing.

    **Not fatal: a lattice that is short.** R-E1 asks for a pitch and `patterns.stitch._plane_specs`
    makes the whole candidate list the `need`, so a site lost to the copper already on the board is a
    coarser stitch and not a failure (R-S3's target half). What it costs is stated in the pattern's
    one `style:` note, in the unit the pitch was derived in — the highest edge the surviving lattice
    is still a lambda/20 stitch for.

    **And not fatal: the pour area.** A lattice takes plane copper by construction, about
    `pi*(dia/2 + clearance)^2` per via per crossed plane, and what a plane is worth is the board's.
    `_plane_gate` already fails a build on a plane that split into islands; the mm2 is reported here
    and pinned in `test_examples_fab.py::PLANES`, so a flood shows up as a moved number with its
    arithmetic rather than as a gate nobody can tune.

    Every board in this repo writes no lattice at all — none of the five pours one net on two facing
    layers — so this returns the sentence that says so on all of them.
    """
    from .route_emit import via_piece
    from .route_verify import plane_stitch, plane_stitch_lines

    pieces = []
    for i in (doc.items if doc is not None else []):
        key = i.get("key") or [""]
        if i.get("reason") != "plane" or key[0] != "via":
            continue
        pieces.append(via_piece(i["net"], i["reason"], tuple(key[1]), float(i.get("w") or 0.0), float(i.get("drill") or 0.0), owner=i.get("owner", "")))
    rows = plane_stitch(routed_text, pieces)
    fails = [
        f"pcbc wrote a plane stitch that stitches one plane: {r.line()}. Drop the lattice or pour both "
        f"layers: this is a pcbc bug, not a board move"
        for r in rows
        if not r.ok
    ]
    return {
        "lattices": [r.to_dict() for r in rows],
        "welded": {r.net: r.welded for r in rows},
        "fails": fails,
        "lines": plane_stitch_lines(rows),
    }


def _thermal_gate(routed_text: str, doc, design, notes=()) -> dict:
    """Technique 4's verify half on the finished board: is the array where pcbc believes it is?

    Wired the way `_plane_gate` and `_barrel_gate` are — one parse of a file already on disk, in the
    route step, off the sidecar KiCad has already refilled and saved around.

    **Fatal for two things, and both of them are pcbc contradicting itself rather than a board fact.**

    - A barrel pcbc placed under a land whose ring is **not** inside one primitive of that land, or
      not inside its net's zone on the plane layer. A via beside the pad still reads as connected —
      KiCad's unconnected-items check is pad to pad, `netcheck.check_copper` never sees a via, and
      `verify_copper` asks about clearance, angle and size — and it moves none of the heat the array
      was placed for. That is the silent failure `docs/stitch-plan.md` section 6 row 4 exists for.
    - `fab.via_in_pad_blockers` **growing**. A thermal array is via-in-pad by definition, so the
      board's blocker list is the one number that says whether the statement let a pad through that
      the fab cannot assemble. It is compared against the blockers of the same board with pcbc's own
      array vias taken out: the array may add via-in-pad **hits**, and it may not add a single
      blocker. `via_in_pad_blockers` has a zero diff in this slice and this is what keeps it honest.

    **Fatal, and a board move: a pad of the land with no barrel** (`ThermalArray.bare`). A land drawn
    as several pads — the ESP32-C3-MINI's nine `49`s — moves no heat through the board under a pad
    with none, however many its neighbours carry; the pattern gives every pad one before any gets a
    second (`patterns.stitch.run`), so a bare pad is one whose every site was blocked, and the
    failure quotes the pattern's note naming the blocker and the edit that moves it (the third
    refutation round: at 0.35 W three of nine pads were bare and the gate said "served").

    **Not fatal: an array that is short.** `Thermal()` is a target under R-S3, so a land with nine
    sites and room for eight is eight barrels and one `style:` note carrying the rise it actually
    leaves. Stopping a build there would stop it on a fact about how much room the land had, which is
    a `Place()` decision and not a routing fault.
    """
    from .compile import compile_design
    from .fab import via_in_pad, via_in_pad_blockers
    from .route_verify import thermal_lines, thermal_pieces, thermal_rows

    job = compile_design(design)
    cs = job.constraints
    pieces = thermal_pieces(doc)
    rows = thermal_rows(routed_text, cs, doc)
    fails = [
        f"pcbc put a thermal via where it is not a thermal via: {r.line()}. "
        f"{r.got - r.in_pad} of {r.got} are outside the land and {r.got - r.in_plane} outside the plane — "
        f"this is a pcbc bug, not a board move"
        for r in rows
        if r.verdict == "adrift"
    ]
    from .patterns.stitch import BARE

    for r in rows:
        if r.verdict != "bare":
            continue
        said = [n for n in notes if n.startswith(f"thermal {r.pad}: {BARE}")]
        where = ", ".join(f"({x:g},{y:g})" for x, y in r.bare)
        fails.append(f"{r.line()}: {len(r.bare)} of its {r.blocks} pads carry no barrel ({where})" + (" — " + " ".join(said) if said else ""))
    # The blocker list with and without the array, so "it did not grow" is a measurement rather than
    # a hope. `via_in_pad` is keyed on the via's coordinate, which is what the sidecar has.
    mine = {(round(p.a[0], 4), round(p.a[1], 4)) for p in pieces}
    hits = via_in_pad(routed_text)
    blockers = via_in_pad_blockers(hits, design)
    ours = [h for h in hits if (round(h["via"][0], 4), round(h["via"][1], 4)) in mine]
    theirs = via_in_pad_blockers([h for h in hits if h not in ours], design)
    grown = [h for h in blockers if h not in theirs]
    fails += [
        f"a thermal via is inside {h['pad']}, which the fab cannot assemble at any via fill "
        f"(fab.via_in_pad_blockers). Thermal() must never name a passive or a mounting peg"
        for h in grown
    ]
    return {
        "arrays": [r.to_dict() for r in rows],
        "verdicts": {r.pad: r.verdict for r in rows},
        "via_in_pad": len(hits),
        "via_in_pad_ours": len(ours),
        "via_in_pad_inside": sum(1 for h in ours if h.get("inside")),
        "blockers": len(blockers),
        "fails": fails,
        "lines": thermal_lines(rows),
    }


def emitted_copper_bar(entry: dict, final_text: str, doc) -> dict:
    """The copper census (`copper_bar`) of the **emitted** board, with each piece's reason and owner
    read off its `pcbc:` role group (`roles_doc`). Copper in no group is `leftover` (a core line's)."""
    from .copper_bar import bar_key, copper_bar
    from .route_verify import bridge_ties

    reasons: dict = {}
    owners: dict = {}
    for i in doc.items:
        k = i["key"]
        key = bar_key("seg", k[1], tuple(k[2]), tuple(k[3]), k[4]) if k[0] == "seg" else bar_key("via", tuple(k[1]))
        reasons[key] = i["reason"]
        owners[key] = i["owner"]
    design = entry.pop("_design", None)
    ties = {}
    if design is not None:
        job = compile_design(design)
        if job.constraints is not None:
            ties = {f"{t.a}|{t.b}": t.tie or t.verdict for t in bridge_ties(final_text, design, job.constraints, coupling=False)}
    now = copper_bar(final_text, reasons, owners, ties)
    out = {**entry, "copper_bar": now}
    out["leftover"] = now["totals"]["by_reason"].get("leftover", {})
    return out


def non_layout_diff(placed_text: str, emitted_text: str) -> list[str]:
    """Every top-level node that is not a layout object — a footprint (its pads included), the
    header, layers, setup, nets — compared between the placed board (`place_native`, footprints posed
    from `board.py`, no layout objects) and the emitted one, with uuids masked (the two files order their
    layout objects differently). Emit is a transcription of the layout objects and may change nothing
    else; a pad or a setting that differs is refused (the second review's m5). Both texts must be in
    the same writer's form: pcbc's emit against the in-memory placed board (`layout_job`), or KiCad's
    save of the emitted board against KiCad's save of the placed board (`_placed_as_kicad_saves`).
    Measured 2026-09-25: 0 differences on all five boards either way."""
    from collections import Counter

    from .layout_prims import HEADS
    from .sexp import Q, parse_tree

    skip = set(HEADS) | {"segment", "arc", "via", "zone"}

    def canon(node):
        if isinstance(node, list):
            kids = [canon(x) for x in node[1:] if not (isinstance(x, list) and x and x[0] in ("uuid", "filled_polygon", "render_cache"))]
            return (str(node[0]), tuple(sorted(map(repr, kids))))
        if isinstance(node, Q):
            return ("Q", str(node))
        try:
            return ("n", float(node))
        except ValueError:
            return ("a", str(node))

    def nodes(text: str) -> Counter:
        out: Counter = Counter()
        for n in parse_tree(text)[1:]:
            if isinstance(n, list) and n and n[0] not in skip:
                out[repr(canon(n))] += 1
        return out

    a, b = nodes(placed_text), nodes(emitted_text)
    if a == b:
        return []
    out = []
    for key, n in (a - b).items():
        head = key[2:].split("'", 1)[0]
        out.append(f"the placed board has {n} ({head}) node(s) the emitted board lacks")
    for key, n in (b - a).items():
        head = key[2:].split("'", 1)[0]
        out.append(f"the emitted board has {n} ({head}) node(s) the placed board lacks")
    return out


def _placed_as_kicad_saves(placed_text: str) -> str:
    """The placed board as KiCad's refill-and-save writes it, for comparing with the emitted board after
    the same save (KiCad rewrites footprints in its own form, so pcbc's text and KiCad's never compare
    directly). Written to a temporary directory after the emit, read once, never a build output."""
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        f = Path(td) / "placed.kicad_pcb"
        f.write_text(placed_text)
        kicad_drc(f, refill=True)
        return f.read_text()


def rules_summary(job: CompiledJob) -> dict:
    """What `job.dru` holds, for the one report line `constraints.py` cannot print itself (it sits
    below `dru.py`, so it never sees the rules): every rule pcbc wrote, how many are errors, how many
    are warnings (the soft kinds of R1, the two geometry rules and the canary: none fails the gate),
    and the net the canary rule watches."""
    cs = job.constraints
    return {
        "written": len(job.dru),
        "error": sum(1 for r in job.dru if r.severity == "error"),
        "soft": sum(1 for r in job.dru if r.severity == "warning"),
        "canary_net": cs.canary_net if cs is not None else None,
    }


def rules_line(summary: dict) -> str:
    canary = f"canary on net {summary['canary_net']}" if summary.get("canary_net") else "no canary (no net has two pads)"
    return f"rules: {summary['written']} written ({summary['error']} error, {summary['soft']} soft), {canary}"


def constraint_lines(job: CompiledJob) -> list[str]:
    """`pcbc check --constraints`: one number per line with its source (`cs.lines`, sorted by net,
    ending in the class summary), then the rules line."""
    cs = job.constraints
    if cs is None:
        return []
    return list(cs.lines) + [rules_line(rules_summary(job))]


INPUTS = "inputs.json"


def _sha(path: Path) -> str | None:
    import hashlib

    path = Path(path)
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None


def part_files(design) -> list[Path]:
    """Every file a `load_board` read besides `board.py` itself: each part's `part.py`, the `.kicad_sym`
    its pin map comes from, and the `.kicad_mod` the seed instantiates (`footprints.footprint_path`)."""
    from .footprints import footprint_path

    out: list[Path] = []
    seen: set[str] = set()
    for inst in design.instances:
        part = inst.part
        origin = getattr(part, "origin", None)
        cands = []
        if origin:
            cands.append(Path(origin) / "part.py")
            if part.symbol:
                cands.append(Path(origin) / part.symbol)
        try:
            cands.append(Path(footprint_path(part)))
        except (FileNotFoundError, ValueError, TypeError):
            pass
        for c in cands:
            key = str(c)
            if key not in seen and c.is_file():
                seen.add(key)
                out.append(c)
    return out


def _inputs(board: Path, design=None) -> dict:
    """The source files a build reads, by content: `board.py`, `layout.core.py` beside it, and every
    part file the board loads (`part_files`). `design` is the loaded board; loaded here when absent."""
    board = Path(board)
    if design is None:
        design = load_board(board)
    import os

    root = board.parent.resolve()
    parts = {}
    for f in part_files(design):
        f = Path(f).resolve()
        key = os.path.relpath(f, root) if str(f).startswith(str(root) + os.sep) else str(f)
        parts[key] = _sha(f)
    # The modules `board.py` imported from its own directory (`language._local_modules`): a second
    # source file the build reads, hashed like the part files (third review T2).
    modules = {m: _sha(root / m) for m in (getattr(design, "modules", None) or ())}
    # M2: every file loading `board.py` was seen to open (`language.load_board` traces it), by content.
    reads = dict(sorted((getattr(design, "reads", None) or {}).items()))
    return {"board": _sha(board), "core": _sha(board.parent / "layout.core.py"), "parts": dict(sorted(parts.items())), "modules": dict(sorted(modules.items())), "reads": reads}


def _write_stamp(path: Path, doc: dict) -> None:
    import json

    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(doc, sort_keys=True) + "\n")


def _read_stamp(path: Path) -> dict | None:
    import json

    try:
        got = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None
    return got if isinstance(got, dict) else None


def _sidecars(pcb: Path) -> dict[str, str | None]:
    """The sidecars beside a board the build wrote, by content: `{file name: sha}`. `kicad-cli pcb drc`
    reads the `.kicad_pro` and `.kicad_dru`, and KiCad writes a `.kicad_prl` beside a board it saves;
    every one is build output and is stamped (C1)."""
    from .project import sidecar_files

    return {name: _sha(p) for name, p in sorted(sidecar_files(pcb).items())}


SCH_INPUTS = "schematic.inputs.json"


def _stamp_sch(layout: Path, board: Path, design, traced: dict | None = None) -> None:
    """`layout/<name>/schematic.inputs.json`: the schematic's own record, written **only by the sch
    stage** when it has drawn `schematic.kicad_sch` and KiCad has read it: the `board.py`, module and
    part-file hashes it was drawn from and the schematic's own hash. Until the fifth review the
    schematic's hash rode in the placed board's record, written by `_stamp_place` for whatever file
    was on disk — so `pcbc pcb` (check, seed, place, no sch stage) then `pcbc build` vouched for a
    schematic that did not exist, or for yesterday's that contradicted the BOM, and the build said ok."""
    now = _inputs(board, design)
    _write_stamp(layout / SCH_INPUTS, {"board": now["board"], "parts": now["parts"], "modules": now["modules"], "reads": now["reads"], "sch": _sha(layout / "schematic.kicad_sch"), "traced": traced})


def _unstamp_sch(layout: Path) -> None:
    if (layout / SCH_INPUTS).exists():
        (layout / SCH_INPUTS).unlink()


def sch_stage(design, board: Path, layout: Path, *, name: str) -> tuple[dict, str | None]:
    """The sch stage, one writer for `pcbc build` and `pcbc sch`: draw `schematic.kicad_sch`, have
    KiCad read its netlist and run ERC, and stamp it (`_stamp_sch`) only when both pass. Returns
    (the step record, the error or None)."""
    sch = layout / "schematic.kicad_sch"
    _unstamp_sch(layout)
    # The schematic's sidecars are not the sch stage's to keep: `kicad-cli sch erc` reads a
    # `schematic.kicad_pro` beside the sheet (its ERC severities), and one lying there steered the ERC
    # gate — `ignore` silenced it, `error` failed it — while nothing wrote, removed or stamped it (sixth
    # review). Unlinked before the sheet is drawn, as the seed stage does its strays; the tracer then
    # stamps the directory `kicad-cli` is handed, so one that appears later is stale (M2).
    for ext in (".kicad_pro", ".kicad_prl"):
        stray = sch.with_suffix(ext)
        if stray.exists():
            stray.unlink()
    report: dict = {}
    with tracing("sch", Path(board).parent) as seen:
        try:
            emit_schematic_file(design, sch, title=name, report=report)
        except ValueError as exc:
            return {"stage": "sch", "error": str(exc)}, f"schematic: {exc}"
        step: dict = {"stage": "sch", "sch": str(sch), "readability": report.get("issues", []), "notes": report.get("notes", [])}
        try:
            fails = check_schematic(design, sch)
            step["netlist"] = "verified" if not fails else fails
            erc = check_erc(sch)
            step["erc"] = "clean" if not erc["errors"] else erc["errors"]
            step["erc_warnings"] = erc["warnings"]
        except KicadMissing as exc:
            fails = []
            erc = {"errors": []}
            step["netlist"] = step["erc"] = f"unchecked: {exc}"
        except (RuntimeError, ValueError) as exc:
            # KiCad could not read the sheet pcbc drew (a net name it cannot hold got past the load
            # check): a refusal naming what KiCad said, never a traceback out of the build.
            return step, f"KiCad could not read the schematic pcbc drew: {str(exc)[-400:]}"
        if fails:
            return step, "schematic netlist differs from board.py: " + "; ".join(fails)
        if erc["errors"]:
            return step, "schematic fails KiCad ERC: " + "; ".join(erc["errors"])
    _stamp_sch(layout, board, design, traced=seen.record())
    return step, None


def _stamp_place(layout: Path, board: Path, design, traced: dict | None = None) -> None:
    """`layout/<name>/placed/inputs.json`, written only when the place stage **emitted** its board
    (`--upto place`, `pcbc pcb`): the hashes of `board.py`, its local modules and part files, of the
    place-only board and its `layout.gen.py`, and of the sidecars beside it. No later stage reads
    these files (the route stage places again, in memory); the stamp is so that a hand edit of the
    place-only board is stale, never reviewed as the build's."""
    now = _inputs(board, design)
    placed = layout / "placed" / "layout.kicad_pcb"
    _write_stamp(
        layout / "placed" / INPUTS,
        {
            "board": now["board"],
            "parts": now["parts"],
            "modules": now["modules"],
            "reads": now["reads"],
            "placed": _sha(placed),
            "gen": _sha(layout / "placed" / "layout.gen.py"),
            "sidecars": _sidecars(placed),
            "traced": traced,
        },
    )


def _stamp(layout: Path, board: Path, design=None, traced: dict | None = None) -> None:
    """`layout/<name>/inputs.json`: what the route stage consumed (`board.py`, `layout.core.py`, the
    part files, the local modules) and what it wrote (`layout.gen.py`, the emitted board after the
    copper gate's save and uuid pinning, and the sidecars beside it), by content, and what it was
    seen to read and run (`traced`, which carries the hash of pcbc's own code). A plain build compares
    every one of these."""
    now = _inputs(board, design)
    routed = layout / "routed"
    now["gen"] = _sha(routed / "layout.gen.py")
    now["routed"] = _sha(routed / "layout.kicad_pcb")
    now["sidecars"] = _sidecars(routed / "layout.kicad_pcb")
    now["traced"] = traced
    _write_stamp(layout / INPUTS, now)


def _sidecar_drift(pcb: Path, was: dict | None) -> str | None:
    """The first sidecar beside `pcb` that is not the bytes the stamp recorded, or None."""
    if not isinstance(was, dict):
        return "?"
    now = _sidecars(pcb)
    for name in sorted(set(was) | set(now)):
        if was.get(name) != now.get(name):
            return name
    return None


def _unstamp(layout: Path) -> None:
    if (layout / INPUTS).exists():
        (layout / INPUTS).unlink()


def fab_outputs(layout: Path) -> dict[str, str | None]:
    """Every file under `layout/<name>/fab/` but the stamp itself, by content, keyed by its path
    relative to `fab/`: the fab board, the Gerbers, the drill files and their maps, the BOM, the CPL,
    the DRC report, the notes and the report."""
    fab = layout / "fab"
    if not fab.is_dir():
        return {}
    # Only the stamp itself, by its path: a file named `inputs.json` anywhere else under `fab/` is a
    # file in the package and is stamped like any other (fifth review: `fab/gerbers/inputs.json` shipped
    # unstamped).
    return {p.relative_to(fab).as_posix(): _sha(p) for p in sorted(fab.rglob("*")) if p.is_file() and p.relative_to(fab).as_posix() != INPUTS}


def _stamp_fab(layout: Path, traced: dict | None = None) -> None:
    """`layout/<name>/fab/inputs.json`: the routed board the fab package was made from and every file
    the fab stage wrote (`fab_outputs`), by content, written when it succeeds. A fab board narrowed by
    hand, a deleted Gerber or drill file, is stale from fab, never "skipped: fab, ok" (third review
    A4/T4)."""
    _write_stamp(layout / "fab" / INPUTS, {"routed": _sha(layout / "routed" / "layout.kicad_pcb"), "outputs": fab_outputs(layout), "traced": traced})


def _unstamp_fab(layout: Path) -> None:
    if (layout / "fab" / INPUTS).exists():
        (layout / "fab" / INPUTS).unlink()


NEVER_EDITED = "it is build output, never edited"


def _load_drift(stamp: dict, board: Path, since: str) -> str | None:
    """The first file loading `board.py` was seen to open (M2: `design.reads`) whose bytes are not the
    ones a stage's stamp recorded, as a stale reason; a stamp with no such record is an older pcbc's.
    Recorded files are re-hashed; the set is not compared, because a module Python has cached is not
    opened twice in one process, and a new input can only come from an edit to a recorded one."""
    reads = stamp.get("reads")
    if not isinstance(reads, dict):
        return f"the {since} has no record of the files loading board.py read: it was made by an older pcbc"
    root = Path(board).resolve().parent
    for key, was in sorted(reads.items()):
        path = Path(key) if Path(key).is_absolute() else root / key
        if _trace_sha(str(path)) != was:
            return f"{key} changed since the {since} (a file loading board.py opened; {'deleted' if not path.exists() else 'edited'})"
    return None


def _changed(was: dict | None, now: dict) -> list[str]:
    was = was or {}
    return sorted(k for k in set(was) | set(now) if was.get(k) != now.get(k))


def stale_reason(layout: Path, board: Path, design=None) -> tuple[str | None, str | None]:
    """(the last stage whose outputs are on disk **and were made from the sources on disk now, and
    are the bytes that stage wrote**, why the later ones are not).

    Every output a build writes is stamped by content in a record beside it, and any mismatch is
    stale **from the stage that wrote the file**, so exactly that stage and the later ones run again:

    - `schematic.inputs.json` (the sch stage): the sources it was drawn from and the schematic's
      bytes. Missing, drawn from another `board.py`, edited or deleted: stale from check (None).
    - `placed/inputs.json` (the place stage, only when it emitted — `--upto place`, `pcbc pcb`): the
      sources, the place-only board, its gen file and sidecars. Different: stale from sch.
    - `inputs.json` (the route stage): the sources, `layout.core.py`, and what the route wrote
      (`layout.gen.py`, the emitted board, its sidecars), with pcbc's own code hash in `traced`.
      Different: stale from place. The route stage reads no earlier stage's board (it places again,
      in memory), so the placed record is not asked of it.
    - `fab/inputs.json` (the fab stage): the emitted board and every file under `fab/`. Different:
      stale from route.
    """
    current = None
    if (layout / "fab" / INPUTS).exists():
        current = "fab"
    if current is None and (layout / "routed" / "layout.kicad_pcb").exists():
        current = "route"
    if current is None and (layout / "placed" / "layout.kicad_pcb").exists():
        current = "place"
    if current is None and (layout / SCH_INPUTS).exists():
        current = "sch"
    if board is None or current is None:
        return current, None
    now = _inputs(board, design)
    root = Path(board).resolve().parent
    sch = _read_stamp(layout / SCH_INPUTS)
    if sch is None:
        return None, "the schematic has no record of the board.py it was drawn from (the sch stage has not run on this board, or its last run was refused; `pcbc pcb` places without drawing it)"
    if sch.get("board") != now["board"] or sch.get("parts") != now["parts"] or (sch.get("modules") or {}) != now["modules"]:
        return None, "board.py, a module it imports or a part file changed since the schematic was drawn"
    if sch.get("sch") != _sha(layout / "schematic.kicad_sch"):
        return None, f"schematic.kicad_sch is not the file the sch stage wrote (edited or deleted): {NEVER_EDITED}"
    why = _load_drift(sch, board, "schematic was drawn") or trace_drift(sch.get("traced"), root, "sch")
    if why:
        return None, why
    if current == "sch":
        return current, None
    if current == "place":
        placed = _read_stamp(layout / "placed" / INPUTS)
        placed_pcb = layout / "placed" / "layout.kicad_pcb"
        if placed is None:
            return "sch", "the placed board has no record of the board.py it was placed from"
        if placed.get("board") != now["board"]:
            return "sch", "board.py changed since the board was placed"
        if placed.get("parts") != now["parts"]:
            return "sch", f"a part file changed since the board was placed ({', '.join(_changed(placed.get('parts'), now['parts'])[:3])})"
        if (placed.get("modules") or {}) != now["modules"]:
            return "sch", f"a module board.py imports changed since the board was placed ({', '.join(_changed(placed.get('modules'), now['modules'])[:3])})"
        if placed.get("placed") != _sha(placed_pcb):
            return "sch", f"placed/layout.kicad_pcb is not the board the place stage wrote (edited or deleted): {NEVER_EDITED}"
        if placed.get("gen") != _sha(layout / "placed" / "layout.gen.py"):
            return "sch", f"placed/layout.gen.py is not the file the place stage wrote (edited or deleted): {NEVER_EDITED}"
        drift = _sidecar_drift(placed_pcb, placed.get("sidecars"))
        if drift:
            return "sch", f"placed/{drift} is not the file the place stage wrote (edited or deleted): {NEVER_EDITED}"
        why = _load_drift(placed, board, "board was placed") or trace_drift(placed.get("traced"), root, "place")
        if why:
            return "sch", why
        return current, None
    stamp = _read_stamp(layout / INPUTS)
    routed = layout / "routed"
    why = None
    if stamp is None:
        why = "the routed board has no record of what it was routed from"
    elif stamp.get("core") != now["core"]:
        why = "layout.core.py changed since the route"
    elif stamp.get("board") != now["board"] or stamp.get("parts") != now["parts"] or (stamp.get("modules") or {}) != now["modules"]:
        why = "board.py, a module it imports or a part file changed since the route"
    elif stamp.get("gen") != _sha(routed / "layout.gen.py"):
        why = f"layout.gen.py is not the file the route wrote (edited or deleted): {NEVER_EDITED}"
    elif stamp.get("routed") != _sha(routed / "layout.kicad_pcb"):
        why = f"routed/layout.kicad_pcb is not the board the gates judged (edited or deleted since the route): {NEVER_EDITED}"
    elif "sidecars" not in stamp:
        why = "the routed board has no record of its sidecars: it was routed by an older pcbc"
    else:
        drift = _sidecar_drift(routed / "layout.kicad_pcb", stamp.get("sidecars"))
        if drift:
            why = f"routed/{drift} is not the file the route stage wrote (edited or deleted): {NEVER_EDITED}"
        else:
            why = _load_drift(stamp, board, "route") or trace_drift(stamp.get("traced"), root, "route")
    if why:
        return "place", why
    if current == "fab":
        stamp = _read_stamp(layout / "fab" / INPUTS)
        if stamp is None or "outputs" not in stamp:
            return "route", "the fab package has no record of what it was made from"
        if stamp.get("routed") != _sha(layout / "routed" / "layout.kicad_pcb"):
            return "route", "the routed board changed since the fab package was made"
        now_out = fab_outputs(layout)
        was = stamp["outputs"]
        moved = sorted(k for k in set(was) | set(now_out) if was.get(k) != now_out.get(k))
        if moved:
            return "route", f"fab/{moved[0]} is not the file the fab stage wrote (edited or deleted{', and ' + str(len(moved) - 1) + ' more' if len(moved) > 1 else ''}): {NEVER_EDITED}"
        why = trace_drift(stamp.get("traced"), root, "fab")
        if why:
            return "route", why
    return current, None


def _done(layout: Path, board: Path | None = None, design=None) -> str | None:
    """`stale_reason`'s stage alone."""
    return stale_reason(layout, board, design)[0]


def planned(current: str | None, *, upto: str, force: bool) -> list[str]:
    end = STAGES.index(upto)
    if force or current is None:
        start = 0
    else:
        start = STAGES.index(current) + 1 if current in STAGES else 0
    if start > end:
        return []
    return list(STAGES[start : end + 1])


def _core_pour_blame(objs, fails: list[str]) -> str:
    """The core `Pour` lines a failed plane gate is judging, named, with the move: a gate that fails on
    a net and layer a core line locked is about that line (fifth review: a minimal core GND pour on
    c3_usb was refused as "the via ... has 7/17 of its ring inside the GND plane" naming no line)."""
    if not objs:
        return ""
    text = " ".join(fails)
    hits = []
    for c in objs.get("copper", ()):
        if c.source != "core" or c.kind != "pour" or c.keepout is not None or not c.net:
            continue
        lays = c.layers or ((c.layer,) if c.layer else ())
        if c.net in text and any(la in text for la in lays):
            hits.append(f"{c.where or c.id} (Pour {c.net!r} on {', '.join(lays)})")
    if not hits:
        return ""
    return f" — this is the plane {', '.join(hits)} locks: the gate judges the locked pour as written; give it the outline and fields the build draws (copy its line from layout.gen.py), or unlock it (delete the line)"


def place_stage(design, board: Path, layout: Path, *, name: str, emit: bool) -> tuple[dict, object, str | None]:
    """The place stage: `place_native.place` in memory, the place-only board checked in memory. With
    `emit` (`--upto place`, `pcbc pcb`) it is also **emitted** — `placed/layout.gen.py` and
    `placed/layout.kicad_pcb`, written by the same `emit_board` the route stage uses — and stamped; no
    later stage reads them. Returns (the step record, the placement, the error or None)."""
    import shutil

    from .check import check_text
    from .place_native import emitted_text, place

    placed_dir = layout / "placed"
    if (placed_dir / INPUTS).exists():
        (placed_dir / INPUTS).unlink()
    if not emit and placed_dir.exists():
        # A full build writes no place-only board (the route stage places again, in memory); one left
        # by an earlier `--upto place` is not this build's and goes, so the tree is the sources'.
        shutil.rmtree(placed_dir)
    placement = place(design, name=name, base=layout / "routed")
    step: dict = {
        "stage": "place",
        "layout": placement.report,
        "notes": placement.notes,
        "poses": {p.ref: {"at": list(p.at), "rot": p.rot} for p in placement.poses},
        "applied": placement.applied,
        "error": placement.error,
    }
    if placement.error:
        return step, placement, placement.error
    text = emitted_text(design, placement, name=name)
    fails = check_text(placement.job, text)
    step["check"] = fails
    if fails:
        step["error"] = "check: " + "; ".join(fails)
        return step, placement, step["error"]
    if emit:
        from . import trace
        from .gen import assign_ids, write_gen
        from .layout_emit import stamp
        from .project import write_sidecars

        if placed_dir.exists():
            shutil.rmtree(placed_dir)
        placed_dir.mkdir(parents=True)
        copper = stamp(design, placement.copper)
        graphics = list(placement.graphics)
        assign_ids(copper, set(), graphics)
        write_gen(placed_dir / "layout.gen.py", placement.poses, copper, board=name, graphics=graphics)
        pcb = placed_dir / "layout.kicad_pcb"
        from .layout_emit import emit_board

        trace.mark("emit")
        write_sidecars(design, placement.job, pcb, name=name)
        pcb.write_text(emit_board(placement.text, placement.poses, copper, graphics, design=design, board=name))
        step["pcb"] = str(pcb)
        step["gen"] = str(placed_dir / "layout.gen.py")
    return step, placement, None


def pcb_job(board: Path) -> dict:
    """The copper inner loop: check, place, emit the place-only board; the layout report as moves. No
    schematic, no routing."""
    from .pcb_place import validate

    board = Path(board).resolve()
    design = load_board(board)
    layout = layout_dir(board)
    name = board.stem
    result: dict = {"board": str(board), "layout": str(layout), "error": None}
    check_notes: list[str] = []
    fails = check_design(design, pcb=True, notes=check_notes) + validate(design)
    result["check"] = fails
    result["check_notes"] = check_notes
    if fails:
        result["error"] = "; ".join(fails)
        return result
    job = compile_design(design)
    result["constraints"] = job.constraints.to_dict() if job.constraints is not None else None
    result["rules"] = rules_summary(job)
    with tracing("place", board.parent) as place_seen:
        step, _placement, err = place_stage(design, board, layout, name=name, emit=True)
    if not err:
        _stamp_place(layout, board, design, traced=place_seen.record())
    result["placed"] = str(layout / "placed" / "layout.kicad_pcb")
    result["layout_report"] = step.get("layout", [])
    result["layout_notes"] = step.get("notes", [])
    result["poses"] = step.get("poses", {})
    result["applied"] = step.get("applied")
    if err:
        result["error"] = err
    return result


def _dangling_core_move(text_: str, kind: str, lines, ends) -> str:
    """KiCad's `track_dangling` / `via_dangling` on a core line, worded from the route stage's own
    record of that line's free ends (`route_native.core_end_states`), never from the absence of a note:

    - `dropped` / `unfinished`: the net is unfinished; its generated copper, a link to this end with it,
      is not on the board (`docs/direction.md` §5), or this end is one of its missing links — a
      consequence of the net's moves, not a fault of the line;
    - `nothing`: a spur — nothing is left for the end to join, and KiCad calls an open end dangling;
    - `failed`: the router searched and found no path (its note is quoted);
    - `joined` / `lost` / no record: the stage says it joined the end (or found it joined), and KiCad
      finds it open: a pcbc bug, not a board move."""
    mine = [e for e in ends if e.get("line") in lines]
    what = "core via" if kind == "via_dangling" else "core track"
    states = {e["state"] for e in mine}
    if "dropped" in states:
        nets = sorted({e["net"] for e in mine if e["state"] == "dropped"})
        at = "; ".join(e["owner"] for e in mine if e["state"] == "dropped")
        return f"{text_} — this {what}'s free end ({at}) was joined by a link that was dropped with the unfinished net {', '.join(nets)}: the moves for {', '.join(nets)} above finish it, and the line is not at fault"
    if "unfinished" in states:
        nets = sorted({e["net"] for e in mine if e["state"] == "unfinished"})
        at = "; ".join(e["owner"] for e in mine if e["state"] == "unfinished")
        return f"{text_} — this {what}'s free end ({at}) is one of the links the unfinished net {', '.join(nets)} still misses: the moves for {', '.join(nets)} above name it, and the line is not at fault"
    if "failed" in states:
        said = "; ".join(e["why"] for e in mine if e["state"] == "failed")
        return f"{text_} — the router could not join this {what}'s free end ({said}); move that end onto the copper it should join, or unlock the line (delete it)"
    if "nothing" in states:
        spur = [e for e in mine if e["state"] == "nothing"]
        at = "; ".join(f"({e['at'][0]:g},{e['at'][1]:g})" for e in spur)
        net = spur[0]["net"]
        return (
            f"{text_} — a spur: this {what}'s free end at {at} has nothing left to join (every {net} pad and via it could reach "
            f"is already joined by the lock's own copper), and KiCad calls an open end dangling: end the line on the copper it joins, or delete it"
        )
    if "lost" in states:
        return f"{text_} — the route stage joined this {what}'s free end and a later step (relax or prune) left it open: this is a pcbc bug, not a board move"
    if mine:
        return f"{text_} — the route stage records this {what}'s free end joined and KiCad finds it open: this is a pcbc bug, not a board move"
    return f"{text_} — the route stage found no free end on this {what} (every end touched copper of its net) and KiCad finds one open: this is a pcbc bug, not a board move"


def _unconnected_nets(gate: dict) -> list[str]:
    return list(gate.get("unconnected_nets") or [])


def build_job(
    board: Path,
    *,
    upto: str = "fab",
    force: bool = False,
) -> dict:
    board = Path(board).resolve()
    if upto not in STAGES:
        raise ValueError(f"upto must be one of {STAGES}")
    try:
        design = load_board(board)
    except ValueError as exc:
        # A board.py line refused at load (a net name KiCad cannot hold, ...): a refusal, returned with
        # its line like every other, never a traceback out of the build.
        return {"board": str(board), "layout": str(layout_dir(board)), "plan": [], "steps": [{"stage": "check", "fails": [str(exc)]}], "error": str(exc)}
    layout = layout_dir(board)
    name = board.stem
    root = board.parent
    current, why = (None, None) if force else stale_reason(layout, board, design)
    plan = planned(current, upto=upto, force=force)
    result: dict = {
        "board": str(board),
        "layout": str(layout),
        "plan": plan,
        "steps": [],
        "error": None,
    }
    if why:
        result["stale"] = why
    if not plan:
        result["ok"] = True
        result["skipped"] = current
        return result

    if "check" in plan:
        check_notes: list[str] = []
        fails = check_design(design, notes=check_notes)
        # Findings that are moves and not failures (`check_design`'s `notes`): today, two `Ground()`
        # nets with nothing tying them. Printed with an exit code of 0 — see `check_design`.
        step = {"stage": "check", "fails": fails, "notes": check_notes}
        if not fails:
            step["constraints"] = constraint_lines(compile_design(design))
        result["steps"].append(step)
        if fails:
            result["error"] = "; ".join(fails)
            return result

    if "sch" in plan:
        step, err = sch_stage(design, board, layout, name=name)
        result["steps"].append(step)
        if err:
            result["error"] = err
            return result

    placement = None
    if "place" in plan:
        # The route stage's own record goes first: a board routed from an earlier placement must not
        # be mistaken for this one.
        _unstamp(layout)
        with tracing("place", root) as place_seen:
            step, placement, err = place_stage(design, board, layout, name=name, emit=upto == "place")
        result["steps"].append(step)
        if err:
            result["error"] = err
            return result
        if upto == "place":
            _stamp_place(layout, board, design, traced=place_seen.record())

    routed = layout / "routed" / "layout.kicad_pcb"
    if "route" in plan:
        # M2: everything the route stage opens, runs and reads from the environment — the core file's
        # helpers and data files, the part files, pcbc's own code — is stamped as it was seen (`trace`).
        with tracing("route", root) as route_seen:
            # A route that fails must not leave the last build's board, gen or Gerbers standing where a
            # plain `pcbc build` would call them done (`_done`): this stage's outputs go first, and the
            # fab outputs made from them with them.
            _unstamp(layout)
            if (layout / "fab").exists():
                import shutil

                shutil.rmtree(layout / "fab")
            # The route stage is `layout_job`: place (again, in memory, when this run did not), route,
            # write `layout.gen.py`, emit. Every gate from here down reads that emitted board, which is
            # also what fab reads.
            from .layout_job import layout_job, roles_doc

            try:
                step = layout_job(design, out=routed, board=board, name=name, placement=placement)
            except ValueError as exc:
                # Every refusal is returned, never raised (fifth review, C3).
                step = {"pcb": str(routed), "error": f"the route stage stopped: {exc}"}
            objs = step.pop("layout_objects", None)
            placed_text = step.pop("placed_text", None)
            entry = {"stage": "route", **step}
            entry.pop("copper_uuids", None)
            if step.get("gen"):
                result["gen"] = step["gen"]  # `layout.gen.py`, in the build directory beside the routed board
            if step.get("error"):
                result["steps"].append(entry)
                result["error"] = step["error"]
                return result
            unrouted = sorted(step.get("unrouted") or [])
            core_where = {}
            if objs is not None:
                from .layout_emit import uuid_of

                core_where = {uuid_of(o, name): o.where for o in [*objs["copper"], *objs["graphics"]] if o.source == "core" and o.where}
            try:
                # M3: the gates judge the fixed point of KiCad's fill — one fill here, then the refill
                # `check_copper` makes — so fab's refill of the board it is handed comes back vertex for
                # vertex, and fab compares it exactly (`fab.fab_board_diff`).
                kicad_drc(routed, refill=True)
                gate = check_copper(design, routed)
                # "verified" is KiCad's word and only this line may write it: DRC and the netlist on the
                # emitted board. `layout_check` (run inside `layout_job`) is a property check and never is.
                entry["drc_warnings"] = gate["drc_warnings"]
                entry["drc_by_type"] = gate.get("by_type")
                entry["geometry"] = gate.get("geometry")
                entry["soft"] = gate.get("soft")
                entry["rules"] = gate.get("rules")
                from .sexp import pin_all_uuids

                # KiCad's save invented ids. Every layout object keeps the uuid its Python object says;
                # the rest — pads, fields — are re-pinned by position.
                mine = frozenset(step.get("copper_uuids") or ())
                routed.write_text(pin_all_uuids(routed.read_text(), name, "routed", keep=mine))
                fails = list(gate["fails"])
                # The emitted board, as KiCad saved it, must still be the Python it was written from,
                # field for field: without this a line of `layout.gen.py` could change between emit and
                # the Gerbers and the build would call the result verified.
                if objs is not None:
                    from .gen import decompile
                    from .layout_job import diff_objects

                    back = decompile(routed.read_text())
                    ids = {uuid_of(o, name): o.id for o in [*objs["copper"], *objs["graphics"]]}
                    for o in [*back.copper, *back.graphics]:
                        o.id = ids.get(o.uuid, o.uuid)
                    for g in back.graphics:
                        if "members" in g.f:
                            g.f["members"] = tuple(ids.get(m, m) for m in g.f["members"])
                    from dataclasses import replace as _replace

                    from .layout_emit import own_uuids
                    from .model import Graphic

                    want = [_replace(c, uuid=uuid_of(c, name)) for c in objs["copper"]] + [Graphic(g.kind, g.id, own_uuids(g, name), g.source, g.comment, g.where) for g in objs["graphics"]]
                    drift = diff_objects(want, [*back.copper, *back.graphics], what="the board KiCad saved")
                    drift += diff_objects(objs["poses"], back.poses, what="the board KiCad saved (pose)")
                    if drift:
                        fails.append("the emitted board is not the layout Python it was written from: " + "; ".join(drift[:6]))
                if placed_text is not None:
                    # ... and nothing but the layout objects: every footprint and pad, the setup, the
                    # layers and the nets as KiCad saved the placed board (the second review's m5).
                    moved_nodes = non_layout_diff(_placed_as_kicad_saves(placed_text), routed.read_text())
                    if moved_nodes:
                        fails.append("the emitted board differs from the placed board outside the layout objects (a footprint, a pad, a board setting): " + "; ".join(moved_nodes[:4]))
                if core_where:
                    # A failure on an object a core line wrote names that line (KiCad reports the uuid).
                    # KiCad reports an item inside a rule area by the item alone, so a core rule area is
                    # named by where the item is.
                    from .layout_check import _inside
                    from .layout_prims import flat_points

                    areas = [c for c in objs["copper"] if c.source == "core" and c.kind == "pour" and c.keepout is not None and c.where]
                    by_uuid = {uuid_of(c, name): c for c in objs["copper"]}

                    def samples(c) -> list:
                        if c.kind == "via" and c.at:
                            return [c.at]
                        pts = [p for p in (c.a, c.mid, c.b) if p is not None]
                        out = []
                        for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
                            n = max(1, int(((x1 - x0) ** 2 + (y1 - y0) ** 2) ** 0.5 / 0.05))
                            out += [(x0 + (x1 - x0) * k / n, y0 + (y1 - y0) * k / n) for k in range(n + 1)]
                        return out or list(item.get("pos") or ())

                    for item in gate.get("fail_items") or ():
                        lines = {core_where[u] for u in item["uuids"] if u in core_where}
                        if item.get("type") == "items_not_allowed":
                            hit = [by_uuid[u] for u in item["uuids"] if u in by_uuid]
                            lines |= {f"{a.where} (rule area{' ' + repr(a.name) if a.name else ''})" for a in areas for c in hit if any(_inside(pt, flat_points(a.points)) for pt in samples(c))}
                        if lines:
                            fails.append(f"{', '.join(sorted(lines))}: {item['text']}")
                    # Every other DRC item KiCad reported on a core object — a warning included — names
                    # the line as a note; a core via or track that KiCad finds dangling joins nothing,
                    # and that is a finding with a move.
                    core_notes: list[str] = []
                    for item in gate.get("items") or ():
                        if item.get("severity") == "error":
                            continue  # already a failure above, with its line
                        lines = {core_where[u] for u in item["uuids"] if u in core_where}
                        if not lines:
                            continue
                        text_ = f"{', '.join(sorted(lines))}: {item['text']}"
                        if item.get("type") in ("via_dangling", "track_dangling"):
                            fails.append(_dangling_core_move(text_, item.get("type"), lines, (step.get("route_stats") or {}).get("core_ends") or ()))
                        else:
                            core_notes.append(text_)
                    entry["core_drc_notes"] = core_notes
                # The router's open nets against KiCad's: a net KiCad calls open that the router called
                # done is a router bug, never a move (docs/native-plan.md §3.6). The unconnected items
                # of the nets the router reported are the moves already in hand, not a second failure.
                kicad_open = _unconnected_nets(gate)
                entry["unconnected_nets"] = kicad_open
                surprise = sorted(set(kicad_open) - set(unrouted))
                if surprise:
                    # A core line on such a net is the author's copper the router had to route around
                    # or to: name it first (the refuters: a stranded lock was blamed on the router alone).
                    held = sorted({o.where for o in (objs or {}).get("copper", ()) if o.source == "core" and o.where and o.net in surprise})
                    if held:
                        fails.append(f"KiCad reports {', '.join(surprise)} unconnected and the router reported them routed; the core lines on them ({', '.join(held)}) are the first suspects: check each one's ends sit on the copper they should join, or delete it. If none of them is at fault, this is a pcbc router bug")
                    else:
                        fails.append(f"KiCad reports {', '.join(surprise)} unconnected and the router reported them routed: this is a pcbc router bug, not a board move")
                # ... and count for count: the router's own open links per net on the copper it handed
                # on (`route_native.open_links`, per pad block, as KiCad's ratsnest counts), against
                # KiCad's unconnected items per net. Equal on every net, routed or not, or the router's
                # model of connectivity is not KiCad's (the third refutation round: nine pad-`49` blocks
                # counted as one terminal).
                claim = {n: k for n, k in ((step.get("route_stats") or {}).get("open_links") or {}).items() if k}
                counted = dict(gate.get("unconnected_by_net") or {})
                entry["open_links"] = {"router": dict(sorted(claim.items())), "kicad": counted}
                differ = sorted(n for n in set(claim) | set(counted) if claim.get(n, 0) != counted.get(n, 0) and n not in surprise)
                if differ:
                    fails.append(
                        "the router's count of open links is not KiCad's: "
                        + "; ".join(f"{n}: the router's copper leaves {claim.get(n, 0)}, KiCad counts {counted.get(n, 0)}" for n in differ)
                        + " — the router's model of what joins what is not KiCad's: this is a pcbc bug, not a board move"
                    )
                if unrouted:
                    fails = [f for f in fails if not (gate.get("unconnected") and f.startswith(f"{gate['unconnected']} unconnected item"))]
                    # The canary is a length rule on one net: when the router left that net open there is
                    # no track for it to measure, and its silence says nothing about the rule file.
                    canary = (compile_design(design).constraints or None) and compile_design(design).constraints.canary_net
                    if canary in unrouted:
                        fails = [f for f in fails if "the canary rule did not fire" not in f]
                        entry["canary"] = f"not measured: the canary net {canary} is unrouted"
                    entry["router_open_kicad_closed"] = sorted(set(unrouted) - set(kicad_open))
                gate = {**gate, "ok": not fails, "fails": fails}
                entry["copper"] = ("verified" if gate["ok"] else gate["fails"]) if not unrouted else (gate["fails"] or f"not verified: {len(unrouted)} net(s) unrouted")
                # The pieces pcbc wrote, and their roles, read off the emitted board's groups with the
                # geometry the board has.
                doc = roles_doc(routed.read_text(), step.get("refusals") or ())
                # D.5 on the board the arbiter itself refilled and saved.
                entry["planes"] = _plane_gate(routed.read_text(), doc, design)
                if entry["planes"]["fails"]:
                    result["steps"].append(entry)
                    result["error"] = "the planes the taps weld to: " + "; ".join(entry["planes"]["fails"]) + _core_pour_blame(objs, entry["planes"]["fails"])
                    return result
            except KicadMissing as exc:
                # KiCad is the arbiter: the router needs no KiCad and the board is emitted, but nothing
                # judged it, so the route stage is not done (no stamp) and nothing is called verified.
                entry["copper"] = f"unchecked: {exc}"
                result["steps"].append(entry)
                result["error"] = f"kicad-cli is missing ({exc}): the emitted board was written and nothing judged it; install KiCad 10 and rebuild"
                return result
            final_text = routed.read_text()
            entry["_design"] = design
            entry = emitted_copper_bar(entry, final_text, doc)
            # Finding 16's census, asked of the emitted board (it was asked of the router's text).
            from .route_verify import same_net_slots

            cs_ = compile_design(design).constraints
            entry["same_net_slots"] = same_net_slots(final_text, cs_.stackup.clearance_min) if cs_ is not None else []
            entry["chains"] = _chain_gate(final_text, design)
            entry["pairs"] = _pair_gate(final_text, design)
            entry["returns"] = _return_gate(final_text, design)
            entry["parallel"] = _barrel_gate(final_text, doc, design)
            entry["thermal"] = _thermal_gate(final_text, doc, design, step.get("notes") or ())
            entry["guards"] = _guard_gate(final_text, doc)
            entry["planes_stitched"] = _stitch_gate(final_text, doc)
            entry["bridges"] = _bridge_gate(final_text, design)
            gate_fails = [
                ("a declared chain is not fed in its order: ", entry["chains"]["fails"]),
                ("a differential pair is not coupled: ", entry["pairs"]["fails"]),
                ("two grounds are tied at more than one point: ", entry["bridges"]["fails"]),
                ("a parallel via is not parallel: ", entry["parallel"]["fails"]),
                ("a thermal land is not served: ", entry["thermal"]["fails"]),
                ("a guard is not welded to its pour: ", entry["guards"]["fails"]),
                ("a plane stitch does not tie both planes: ", entry["planes_stitched"]["fails"]),
            ]
            if not unrouted:
                for head, fl in gate_fails:
                    if fl:
                        result["steps"].append(entry)
                        result["error"] = head + "; ".join(fl)
                        return result
            result["steps"].append(entry)
            if unrouted:
                # Every gate ran on what was emitted (P0); the board is not finished, so there is no
                # stamp and no fab. The gates that would have failed are reported beside the moves.
                also = [head + "; ".join(fl) for head, fl in gate_fails if fl] + [f for f in gate.get("fails") or []]
                result["unrouted"] = unrouted
                result["moves"] = step.get("moves") or []
                result["error"] = "unrouted: " + "; ".join(unrouted) + "\n" + "\n".join(step.get("moves") or []) + ("".join(f"\n  also: {a}" for a in also) if also else "")
                return result
            if not gate["ok"]:
                result["error"] = "copper is not board.py's netlist, or fails KiCad DRC: " + "; ".join(gate["fails"])
                return result
            _stamp(layout, board, design, traced=route_seen.record())

    if "fab" in plan:
        if not routed.exists():
            result["error"] = "fab needs the routed board (layout/<name>/routed/layout.kicad_pcb); run the route stage"
            return result
        job = compile_design(design)
        # The fab package's own record goes first and is written again only when this fab succeeds,
        # and the whole directory goes: the fab stamp records every file under `fab/`.
        _unstamp_fab(layout)
        if (layout / "fab").exists():
            import shutil

            shutil.rmtree(layout / "fab")
        with tracing("fab", root) as fab_seen:
            step = fab_job(job, routed, out_dir=layout / "fab", design=design)
        # `power` is a move, not a gate (`ampacity.power_moves`); `--strict-power` makes it stop the build.
        moves = step.get("power_moves") or []
        result["steps"].append(
            {"stage": "fab", "fab": step.get("fab"), "power": moves, "error": step.get("error")}
        )
        if moves:
            result["power"] = moves
        if step.get("error"):
            result["error"] = step["error"]
            return result
        _stamp_fab(layout, traced=fab_seen.record())

    result["ok"] = True
    return result
