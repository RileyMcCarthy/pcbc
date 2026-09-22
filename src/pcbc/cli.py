from __future__ import annotations

import argparse
import contextlib
import os
import json
import sys
from pathlib import Path

from . import __version__
from .build import STAGES, build_job, constraint_lines, pcb_job, rules_line
from .compile import compile_design
from .language import check_board, load_board
from .netcheck import KicadMissing, check_erc, check_schematic
from .project import layout_dir
from .review import review_job
from .sch_emit import emit_schematic_file


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="pcbc", description="Python in, fab-ready KiCad out")
    parser.add_argument("--version", action="version", version=f"pcbc {__version__}")
    sub = parser.add_subparsers(dest="cmd", required=True)

    ck = sub.add_parser("check", help="Load board.py; unbound pins, Place(), LCSC, a NetReq line that does not compile")
    ck.add_argument("board")
    ck.add_argument("--constraints", action="store_true", help="then every number a NetReq/Pair/Bus line became, one per line with its source")
    ck.add_argument("--json", action="store_true", help="with --constraints: the ConstraintSet as JSON instead of the lines")
    ck.set_defaults(func=cmd_check)

    bd = sub.add_parser("build", help="check → seed → sch → place → route → fab")
    bd.add_argument("board")
    bd.add_argument("--upto", default="fab", choices=STAGES)
    bd.add_argument("--force", action="store_true")
    bd.add_argument(
        "--strict-patterns",
        action="store_true",
        help="fail the build on any pattern refusal, not only a hard one (docs/r2-design.md C.6)",
    )
    bd.add_argument(
        "--strict-power",
        action="store_true",
        help="fail the build when a rail cannot carry its declared current, instead of printing it as a move",
    )
    bd.set_defaults(func=cmd_build)

    sc = sub.add_parser("sch", help="Draw the schematic, prove it is board.py's netlist, list what to move")
    sc.add_argument("board")
    sc.add_argument("--json", action="store_true", help="the full report: parts' poses, issues, netlist")
    sc.set_defaults(func=cmd_sch)

    pc = sub.add_parser("pcb", help="Place the copper from board.py's Place() lines and list what to move")
    pc.add_argument("board")
    pc.add_argument("--json", action="store_true", help="the full report: every part's pose, the moves")
    pc.add_argument("--constraints", action="store_true", help="also print the constraint report the placement was checked against")
    pc.set_defaults(func=cmd_pcb)

    ro = sub.add_parser("route", help="Routing questions about a placed board (today: --channels)")
    ro.add_argument("board")
    ro.add_argument("--channels", action="store_true", help="the congestion map: every channel between two obstacles, what fits through it, and the pads with no way out")
    ro.add_argument("--json", action="store_true", help="with --channels: the block that goes into copper.json")
    ro.set_defaults(func=cmd_route)

    rv = sub.add_parser("review", help="HTML: schematic, copper, 3D")
    rv.add_argument("board")
    rv.add_argument("--no-open", action="store_true")
    rv.set_defaults(func=cmd_review)

    se = sub.add_parser("search", help="LCSC / JLC parts for a query (stock, price, basic/extended)")
    se.add_argument("query")
    se.add_argument("--limit", type=int, default=10)
    se.add_argument("--json", action="store_true")
    se.set_defaults(func=cmd_search)

    fe = sub.add_parser("fetch", help="LCSC id → components/<Mfr>/<MPN>/{part.py, .kicad_sym, .kicad_mod, .step}, scored")
    fe.add_argument("lcsc", nargs="+", help="C-numbers, e.g. C191884")
    fe.add_argument("--into", default="components", help="components dir (default: ./components)")
    fe.add_argument("--force", action="store_true", help="rewrite an existing part.py")
    fe.add_argument("--json", action="store_true")
    fe.set_defaults(func=cmd_fetch)

    so = sub.add_parser("score", help="Library quality of a part dir, a .kicad_sym, or every part a board.py loads")
    so.add_argument("path", nargs="+")
    so.add_argument("--json", action="store_true")
    so.set_defaults(func=cmd_score)

    args = parser.parse_args(argv)
    return int(args.func(args))


def cmd_check(args: argparse.Namespace) -> int:
    path = Path(args.board)
    if not path.exists():
        print(f"no such file: {path}", file=sys.stderr)
        return 2
    notes: list[str] = []
    fails = check_board(path, notes=notes)
    if fails:
        for f in fails:
            print(f, file=sys.stderr)
        if args.json:
            print(json.dumps({"ok": False, "fails": fails, "notes": notes}, indent=2))
        return 1
    design = load_board(path)
    if args.constraints and args.json:
        # ConstraintSet.to_dict(): every Derived as {"value", "unit", "formula", "ref", "note"}.
        print(json.dumps(compile_design(design).constraints.to_dict(), indent=2))
        return 0
    print(json.dumps({"ok": True, "instances": len(design.instances), "nets": len(design.nets), "notes": notes}))
    # A move, and an exit code of 0: `check_design`'s `notes` are findings the board should act on
    # and that no build is stopped for (H.3's promotion procedure, `--strict-power`'s precedent).
    for line in notes:
        print(f"note: {line}")
    if args.constraints:
        # One number per line with its source, sorted by net (docs/r1-design.md A.1); the numbers the
        # AI did not have to know. A caveat (the 2-layer USB note) is a line, never a failure.
        for line in constraint_lines(compile_design(design)):
            print(line)
    return 0


@contextlib.contextmanager
def _env(values: dict[str, str]):
    """Set these environment variables for the block and put the old ones back, whatever happens."""
    before = {k: os.environ.get(k) for k in values}
    os.environ.update(values)
    try:
        yield
    finally:
        for k, old in before.items():
            if old is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = old


def cmd_build(args: argparse.Namespace) -> int:
    path = Path(args.board)
    if not path.exists():
        print(f"no such file: {path}", file=sys.stderr)
        return 2
    # C.6's escape hatches: a refusal (and, since `power_moves`, a rail under its declared current)
    # is a printed move and a fall-through in R2, and these say "I want the move to stop the build".
    # R3 flips both defaults. Scoped to this one call rather than set on the process: a flag that
    # outlives its build turned three example boards' own builds into errors inside one pytest
    # session, which is the same leak a user gets from a second `main()` call in one process.
    strict = {
        "PCBC_STRICT_PATTERNS": getattr(args, "strict_patterns", False),
        "PCBC_STRICT_POWER": getattr(args, "strict_power", False),
    }
    with _env({k: "1" for k, on in strict.items() if on}):
        result = build_job(path, upto=args.upto, force=args.force)
    print(json.dumps(result, indent=2, default=str))
    # On stderr as well as in the JSON: a rail that cannot carry its declared current is the one
    # thing a passing build says that a reader must not scroll past (`ampacity.power_moves`).
    for line in result.get("power") or []:
        print(f"power: {line}", file=sys.stderr)
    # The two findings that are moves and not failures, on stderr as well as in the JSON: two
    # `Ground()` nets with nothing tying them (`circuit._untied_grounds`, the check stage) and what
    # the finished copper says about the same pair (`build._bridge_gate`, the route stage). Only the
    # check stage's `notes` is read here, not every step's — a route step's `notes` is the pattern
    # stage's style list and it already has a home in the JSON.
    for step in result.get("steps") or []:
        lines = (step.get("notes") or []) if step.get("stage") == "check" else (step.get("bridges") or {}).get("notes") or []
        for line in lines:
            print(f"note: {line}", file=sys.stderr)
    if result.get("error"):
        print(result["error"], file=sys.stderr)
        return 1
    return 0


def cmd_sch(args: argparse.Namespace) -> int:
    """The inner loop: edit SchPlace() lines, run this, act on the list. No picture needed."""
    path = Path(args.board)
    if not path.exists():
        print(f"no such file: {path}", file=sys.stderr)
        return 2
    fails = check_board(path, pcb=False)
    if fails:
        for f in fails:
            print(f, file=sys.stderr)
        return 1
    design = load_board(path)
    sch = layout_dir(path) / "schematic.kicad_sch"
    report: dict = {}
    emit_schematic_file(design, sch, title=path.stem, report=report)
    report["sch"] = str(sch)
    try:
        mismatch = check_schematic(design, sch)
        report["netlist"] = "verified" if not mismatch else mismatch
        erc = check_erc(sch)
        report["erc"] = "clean" if not erc["errors"] else erc["errors"]
        report["erc_warnings"] = erc["warnings"]
    except KicadMissing as exc:
        mismatch = []
        erc = {"errors": [], "warnings": {}}
        report["netlist"] = report["erc"] = f"unchecked: {exc}"
    if args.json:
        print(json.dumps(report, indent=2, default=str))
        return 1 if (mismatch or erc["errors"]) else 0
    print(f"schematic: {sch}")
    if report["netlist"] == "verified":
        print("netlist: kicad-cli reads exactly board.py's netlist")
    elif mismatch:
        print(f"netlist: MISMATCH ({len(mismatch)}) - the drawing does not connect what board.py says")
        for m in mismatch:
            print(f"  ! {m}")
    else:
        print(f"netlist: {report['netlist']}")
    if report["erc"] == "clean":
        w = erc["warnings"]
        print(f"erc: clean ({sum(w.values())} warnings: " + ", ".join(f"{k} {v}" for k, v in sorted(w.items())) + ")" if w else "erc: clean")
    elif erc["errors"]:
        print(f"erc: {len(erc['errors'])} error(s)")
        for e in erc["errors"]:
            print(f"  ! {e}")
    else:
        print(f"erc: {report['erc']}")
    issues = report.get("issues", [])
    if issues:
        print(f"readability: {len(issues)} to fix by editing SchPlace() lines")
        for i, m in enumerate(issues, 1):
            print(f"  {i}. {m}")
    else:
        print("readability: nothing overlaps")
    notes = report.get("notes", [])
    if notes:
        print(f"style: {len(notes)} note{'s' if len(notes) > 1 else ''} (legal, not counted)")
        for m in notes:
            print(f"  - {m}")
    return 1 if (mismatch or erc["errors"]) else 0


def cmd_review(args: argparse.Namespace) -> int:
    path = Path(args.board)
    if not path.exists():
        print(f"no such file: {path}", file=sys.stderr)
        return 2
    result = review_job(path, open_html=not args.no_open)
    print(json.dumps({k: result[k] for k in ("html", "pcb", "error") if k in result}, indent=2))
    if result.get("error") and not (result.get("html") and Path(result["html"]).exists()):
        print(result["error"], file=sys.stderr)
        return 1
    return 0


def cmd_pcb(args: argparse.Namespace) -> int:
    path = Path(args.board)
    if not path.exists():
        print(f"no such file: {path}", file=sys.stderr)
        return 2
    try:
        result = pcb_job(path)
    except ValueError as exc:
        print(f"placement: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(result, indent=2, default=str))
        return 0 if not result.get("error") else 1
    if result.get("check"):
        print("check failed:")
        for f in result["check"]:
            print(f"  - {f}")
        return 2
    print(f"placed: {result['placed']}")
    if result.get("error"):
        print(f"error: {result['error']}")
    report = result.get("layout_report", [])
    if report:
        print(f"layout: {len(report)} to fix by editing Place() lines")
        for i, m in enumerate(report, 1):
            print(f"  {i}. {m}")
    else:
        print("layout: nothing to move")
    notes = result.get("layout_notes", [])
    if notes:
        print(f"style: {len(notes)} note{'s' if len(notes) > 1 else ''} (legal, not counted)")
        for m in notes:
            print(f"  - {m}")
    for line in result.get("check_notes") or []:
        print(f"note: {line}")
    if args.constraints and result.get("constraints"):
        lines = list(result["constraints"]["lines"]) + [rules_line(result["rules"])]
        print(f"constraints: {len(lines)} lines (what the placement was checked against; pcbc check --constraints prints them alone)")
        for line in lines:
            print(f"  {line}")
    return 1 if result.get("error") else 0


def cmd_route(args: argparse.Namespace) -> int:
    """`pcbc route --channels`: the congestion map of a placed board.

    It places the board first — `pcb_job` is check + seed + place, a pure function of `board.py` with
    no KiCad and no router in it — and then measures the air between the obstacles. That is the whole
    point of the command: the answer is available *before* anything is routed, which is when a
    `Place()` line can still be written.
    """
    import time

    from .route_channel import channels, channels_doc, class_widths, moves, escapes, report
    from .route_scene import build_scene

    path = Path(args.board)
    if not path.exists():
        print(f"no such file: {path}", file=sys.stderr)
        return 2
    if not args.channels:
        print("pcbc route today ships only --channels (the congestion map).", file=sys.stderr)
        print("To route a board, use: pcbc build --upto route", file=sys.stderr)
        return 2
    try:
        result = pcb_job(path)
    except ValueError as exc:
        print(f"placement: {exc}", file=sys.stderr)
        return 2
    if result.get("check"):
        print("check failed:")
        for f in result["check"]:
            print(f"  - {f}")
        return 2
    design = load_board(path)
    job = compile_design(design)
    text = Path(result["placed"]).read_text()
    t0 = time.perf_counter()
    scene = build_scene(design, job, job.constraints, text)
    widths = class_widths(job)
    layers_of = {c.net: c.layers for c in (job.constraints.constraints if job.constraints is not None else ())}
    maps = channels(scene)
    if args.json:
        print(json.dumps(channels_doc(scene, widths=widths, layers_of=layers_of, maps=maps), indent=2))
        return 0
    print(f"placed: {result['placed']}")
    for line in report(scene, widths=widths, layers_of=layers_of, maps=maps):
        print(line)
    print(f"({time.perf_counter() - t0:.3f} s, no router and no KiCad)")
    # A sealed pad is a placement refusal, so the command exits non-zero the way `pcbc pcb` does when
    # it has moves: this is meant to be run in a loop that edits `board.py` until it is quiet.
    return 1 if moves(escapes(scene, maps, widths, layers_of=layers_of)) else 0


def cmd_search(args: argparse.Namespace) -> int:
    from .source import format_hits, search_lcsc

    try:
        hits = search_lcsc(args.query, limit=args.limit)
    except RuntimeError as e:
        print(str(e), file=sys.stderr)
        return 1
    print(json.dumps(hits, indent=2) if args.json else format_hits(hits))
    return 0


def cmd_fetch(args: argparse.Namespace) -> int:
    from .source import fetch_lcsc, format_score

    results = []
    worst = 0
    for lcsc in args.lcsc:
        try:
            r = fetch_lcsc(lcsc, Path(args.into), force=args.force)
        except RuntimeError as e:
            print(f"{lcsc}: {e}", file=sys.stderr)
            worst = 1
            continue
        results.append(r)
        if not args.json:
            print(f"{r['lcsc']} -> {r['dir']}  ({'wrote' if r['wrote_part_py'] else 'kept'} part.py)")
            print(format_score(r["score"]))
    if args.json:
        print(json.dumps(results, indent=2))
    return worst


def cmd_score(args: argparse.Namespace) -> int:
    from .source import format_score, score_part

    dirs: list[Path] = []
    for raw in args.path:
        p = Path(raw)
        if not p.exists():
            print(f"no such path: {p}", file=sys.stderr)
            return 2
        if p.is_file() and p.suffix == ".py" and p.name != "part.py":
            design = load_board(p)
            seen: set[Path] = set()
            for inst in design.instances:
                if inst.part.origin and inst.part.origin not in seen:
                    seen.add(inst.part.origin)
                    dirs.append(inst.part.origin)
        else:
            dirs.append(p)
    reports = [score_part(d) for d in dirs]
    if args.json:
        print(json.dumps(reports, indent=2))
    else:
        for r in reports:
            print(format_score(r))
    return 0 if all(r["grade"] != "bad" for r in reports) else 1


if __name__ == "__main__":
    raise SystemExit(main())
