"""check → seed → sch → place → route → fab. No Zener."""

from __future__ import annotations

from pathlib import Path

from .circuit import check_design
from .compile import CompiledJob, compile_design
from .fab import fab_job
from .language import load_board
from .netcheck import KicadMissing, check_copper, check_erc, check_schematic
from .place import place_job
from .project import layout_dir, packed_reason, seed_pcb
from .route import route_job
from .sch_emit import emit_schematic_file
from .seed import seed_job

STAGES = ("check", "seed", "sch", "place", "route", "fab")


def _plane_gate(routed_text: str, doc) -> dict:
    """D.5, run in the product: every tap via inside its own plane, and every plane still one island.

    The islands are reported as `{(net, layer): count}` with their filled **area**, because the count
    alone cannot see a fragment the fill deleted (`route_verify.plane_area`, finding 9). The area is
    a number to read and to pin, never a pass/fail here: what a plane is *worth* is the board's, and
    only a recorded baseline (`test_examples_fab.py::PLANES`) can say whether a given mm2 is right.
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
    return {
        "islands": {f"{net} {layer}": n for (net, layer), n in sorted(islands.items())},
        "area_mm2": {f"{net} {layer}": a for (net, layer), a in sorted(plane_area(routed_text).items())},
        "fails": fails,
    }


def _chain_gate(routed_text: str, design) -> dict:
    """R-X4's verify half, run in the product: every declared `Chain()`'s order, read off the copper.

    The gate the chain pattern's hard refusal was standing in for. Until this function the pattern
    refused a link it could not write and **nothing** read the finished board to ask whether the
    order the author declared had survived in it: `docs/router-plan.md` line 202 tags R-X4 "**P**
    (chain), **V**" and only the P existed (`docs/r2-measurements.md`, S6). Wired the way `_plane_gate`
    is — one parse of a file already on disk, in the route step, with a move for its error.

    **Fatal for the chains pcbc routes, reported for the ones B.2 hands away.** A `Chain()` on a net
    carrying a `PairSpec` is skipped by the pattern with a printed note — "pairs are R4's" — so R2
    writes none of that copper and KRT writes all of it; failing the build on it would stop a board
    on a fault the tool cannot yet repair, which is `--strict-power`'s argument in C.6 and the same
    answer. It is reported rather than swallowed, and the report is not hypothetical: measured
    2026-09-20, c3_usb's `USB_DN` and node's `USB_DN` ship with their declared order violated on
    their own checked-in boards — `U3.3` and `U3.4` hang off the feed instead of sitting in it. R4
    owns routing a pair, and it owns those two lines.
    """
    from .compile import compile_design
    from .route_verify import chain_order

    job = compile_design(design)
    rows = chain_order(routed_text, design, job.constraints) if job.constraints is not None else ()
    bad = [r for r in rows if r.verdict in ("spur", "open")]
    return {
        "checked": {r.net: r.verdict for r in rows},
        "fails": [f"{r.net} chain {' -> '.join(r.stations)} ({r.where}): {r.detail} (R-X4). {r.move}" for r in bad if r.owner == "chain"],
        "notes": [f"{r.net} chain {' -> '.join(r.stations)} ({r.where}): {r.detail} (R-X4), and R2 routes no pair at all, so this copper is KRT's and R4's. {r.move}" for r in bad if r.owner != "chain"]
        + [f"{r.net} chain ({r.where}): {r.detail}" for r in rows if r.verdict in ("poured", "unresolved")],
    }


def _return_gate(routed_text: str, design) -> dict:
    """R-Z4's **V**, run in the product and **never fatal** (`docs/stitch-plan.md` §6 row 5, S1).

    Every via on a net carrying `Constraint.reference`, classified by what its layer change does to
    the return current. Wired the way `_plane_gate` and `_chain_gate` are — one parse of a file
    already on disk, in the route step — and deliberately without a `fails` key, because there is
    nothing here a build should stop on: the verdict on all eleven vias of the two boards that have
    any is settled by the **stackup**, and the edits that change it (`Board(planes=...)` on node,
    `NetReq(layers=...)` on c3_usb) are board decisions rather than routing faults. S5 owns printing
    them as moves; S1 owns counting them, so that the placer `docs/stitch-plan.md` §8 refuses to
    build is refused against a measured population and not an assumed one.
    """
    from .compile import compile_design
    from .route_verify import referenced_nets, return_lines, return_vias

    job = compile_design(design)
    cs = job.constraints
    watched = referenced_nets(cs) if cs is not None else ()
    rows = return_vias(routed_text, cs) if cs is not None else ()
    verdicts: dict[str, int] = {}
    for r in rows:
        verdicts[r.verdict] = verdicts.get(r.verdict, 0) + 1
    return {
        "watched": list(watched),
        "vias": [r.to_dict() for r in rows],
        "verdicts": dict(sorted(verdicts.items())),
        "lines": return_lines(rows, watched),
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


def _done(layout: Path) -> str | None:
    if (layout / "fab" / "bom.csv").exists() and (layout / "fab" / "gerbers").exists():
        if any((layout / "fab" / "gerbers").glob("*")):
            return "fab"
    if (layout / "routed" / "layout.kicad_pcb").exists():
        return "route"
    if (layout / "placed" / "layout.kicad_pcb").exists():
        return "place"
    if (layout / "layout.kicad_pcb").exists():
        return "seed"
    return None


def planned(current: str | None, *, upto: str, force: bool) -> list[str]:
    end = STAGES.index(upto)
    if force or current is None:
        start = 0
    else:
        start = STAGES.index(current) + 1 if current in STAGES else 0
        if current == "seed":
            start = STAGES.index("sch")
        # sch is not tracked as a stage file besides schematic.kicad_sch
        if (current == "seed") and upto != "check":
            pass
    if start > end:
        return []
    return list(STAGES[start : end + 1])


def pcb_job(board: Path) -> dict:
    """The copper inner loop: check, seed, place; the layout report as moves. No schematic, no routing."""
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
    seed = seed_pcb(board)
    result["seed"] = seed_job(design, seed, name=name)
    placed = layout / "placed" / "layout.kicad_pcb"
    step = place_job(design, seed, out=placed)
    result["placed"] = str(placed)
    result["layout_report"] = step.get("layout", [])
    result["layout_notes"] = step.get("notes", [])
    result["poses"] = step.get("poses", {})
    result["applied"] = step.get("applied")
    if step.get("error"):
        result["error"] = step["error"]
    return result


def build_job(
    board: Path,
    *,
    upto: str = "fab",
    force: bool = False,
) -> dict:
    board = Path(board).resolve()
    if upto not in STAGES:
        raise ValueError(f"upto must be one of {STAGES}")
    design = load_board(board)
    layout = layout_dir(board)
    name = board.stem
    current = None if force else _done(layout)
    plan = planned(current, upto=upto, force=force)
    result: dict = {
        "board": str(board),
        "layout": str(layout),
        "plan": plan,
        "steps": [],
        "error": None,
    }
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

    seed = seed_pcb(board)
    if "seed" in plan:
        if packed_reason(seed) and not force:
            result["error"] = packed_reason(seed)
            return result
        step = seed_job(design, seed, name=name)
        result["steps"].append({"stage": "seed", **step})

    if "sch" in plan:
        sch = layout / "schematic.kicad_sch"
        report: dict = {}
        try:
            emit_schematic_file(design, sch, title=name, report=report)
        except ValueError as exc:
            result["steps"].append({"stage": "sch", "error": str(exc)})
            result["error"] = f"schematic: {exc}"
            return result
        step = {"stage": "sch", "sch": str(sch), "readability": report.get("issues", [])}
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
        result["steps"].append(step)
        if fails:
            result["error"] = "schematic netlist differs from board.py: " + "; ".join(fails)
            return result
        if erc["errors"]:
            result["error"] = "schematic fails KiCad ERC: " + "; ".join(erc["errors"])
            return result

    placed = layout / "placed" / "layout.kicad_pcb"
    if "place" in plan:
        step = place_job(design, seed, out=placed)
        result["steps"].append({"stage": "place", **step})
        if step.get("error"):
            result["error"] = step["error"]
            return result

    routed = layout / "routed" / "layout.kicad_pcb"
    if "route" in plan:
        src = placed if placed.exists() else seed
        step = route_job(design, src, out=routed, name=name)
        entry = {"stage": "route", **step}
        if step.get("error"):
            result["steps"].append(entry)
            result["error"] = step["error"]
            return result
        # Read before the KiCad block rather than inside it: `_barrel_gate` runs on every build,
        # `kicad-cli` or not, and what it needs is the copper pcbc wrote, which is pcbc's own file.
        from .route_emit import read_sidecar as _read_sidecar

        side = routed.parent / "copper.json"
        doc = _read_sidecar(side) if side.exists() else None
        try:
            gate = check_copper(design, routed)
            entry["copper"] = "verified" if gate["ok"] else gate["fails"]
            entry["drc_warnings"] = gate["drc_warnings"]
            entry["geometry"] = gate.get("geometry")
            # R1 (docs/r1-design.md section E, S3): `check_copper` gains "soft" {rule_name: hits} for the
            # warnings of pcbc's soft rules and "rules" {rule_name: hits} for every pcbc rule. Carried as
            # given; None until netcheck provides them.
            entry["soft"] = gate.get("soft")
            entry["rules"] = gate.get("rules")
            from .sexp import pin_all_uuids

            # KiCad's save invented ids. pcbc's own copper keeps the ids the sidecar names, so a
            # piece stays traceable from `copper.json` into the board and into KiCad's UI (D.4).
            mine = frozenset(i["uuid"] for i in doc.items) if doc is not None else frozenset()
            routed.write_text(pin_all_uuids(routed.read_text(), name, "routed", keep=mine))
            # D.5 on the board the arbiter itself refilled and saved. Until S5's review both halves
            # of this check lived only in the suite — `grep plane_checks src/` found nothing outside
            # `route_verify`'s own definitions — so "every tap via lands in its own net's plane" and
            # "every plane is still one island" were asked of the five examples and of no user's
            # board at all (finding 11). It costs one parse of a file already on disk.
            entry["planes"] = _plane_gate(routed.read_text(), doc)
            if entry["planes"]["fails"]:
                result["steps"].append(entry)
                result["error"] = "the planes the taps weld to: " + "; ".join(entry["planes"]["fails"])
                return result
        except KicadMissing as exc:
            gate = {"ok": True, "fails": []}
            entry["copper"] = f"unchecked: {exc}"
        # R-X4's **V**, outside the `try` because it needs no KiCad: one parse of the routed file,
        # asking whether each declared `Chain()`'s order is in the copper KRT finished. The chain
        # pattern's refusal is soft (C.6) precisely because this runs.
        final_text = routed.read_text()
        entry["chains"] = _chain_gate(final_text, design)
        # `docs/stitch-plan.md` S1's return-via count, which can never fail a build, and S4's barrel
        # gate, which can fail it for exactly one thing: a rung pcbc wrote that is not joined to the
        # anchor it is supposed to be parallel to (`_barrel_gate`).
        entry["returns"] = _return_gate(final_text, design)
        entry["parallel"] = _barrel_gate(final_text, doc, design)
        # Technique 6's verify half (`docs/stitch-plan.md` S3): what joins each pair of `Ground()`
        # nets, and whether it joins them at one point. Fatal for `multi` and nothing else — see
        # `_bridge_gate` for why each of the other four verdicts is a printed move instead.
        entry["bridges"] = _bridge_gate(final_text, design)
        if entry["chains"]["fails"]:
            result["steps"].append(entry)
            result["error"] = "a declared chain is not fed in its order: " + "; ".join(entry["chains"]["fails"])
            return result
        if entry["bridges"]["fails"]:
            result["steps"].append(entry)
            result["error"] = "two grounds are tied at more than one point: " + "; ".join(entry["bridges"]["fails"])
            return result
        if entry["parallel"]["fails"]:
            result["steps"].append(entry)
            result["error"] = "a parallel via is not parallel: " + "; ".join(entry["parallel"]["fails"])
            return result
        result["steps"].append(entry)
        if not gate["ok"]:
            result["error"] = "copper is not board.py's netlist, or fails KiCad DRC: " + "; ".join(gate["fails"])
            return result

    if "fab" in plan:
        src = routed if routed.exists() else placed if placed.exists() else seed
        job = compile_design(design)
        step = fab_job(job, src, out_dir=layout / "fab", design=design)
        # `power` is a move, not a gate (`ampacity.power_moves`): the rails whose narrowest series
        # copper cannot carry what `NetReq(amps=)` declares, each naming the edits that fix it. It
        # rides in the build's own output because the shortfall used to live only in `FAB_NOTES.md`
        # while the build printed `copper: verified` and exited 0 — an AI reading that shipped buck
        # with 1.21 A of copper on a 2 A rail. `--strict-power` makes it stop the build instead.
        moves = step.get("power_moves") or []
        result["steps"].append(
            {"stage": "fab", "fab": step.get("fab"), "power": moves, "error": step.get("error")}
        )
        if moves:
            result["power"] = moves
        if step.get("error"):
            result["error"] = step["error"]
            return result

    result["ok"] = True
    return result
