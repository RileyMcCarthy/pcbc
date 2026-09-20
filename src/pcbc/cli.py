from __future__ import annotations

import argparse
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
    fails = check_board(path)
    if fails:
        for f in fails:
            print(f, file=sys.stderr)
        if args.json:
            print(json.dumps({"ok": False, "fails": fails}, indent=2))
        return 1
    design = load_board(path)
    if args.constraints and args.json:
        # ConstraintSet.to_dict(): every Derived as {"value", "unit", "formula", "ref", "note"}.
        print(json.dumps(compile_design(design).constraints.to_dict(), indent=2))
        return 0
    print(json.dumps({"ok": True, "instances": len(design.instances), "nets": len(design.nets)}))
    if args.constraints:
        # One number per line with its source, sorted by net (docs/r1-design.md A.1); the numbers the
        # AI did not have to know. A caveat (the 2-layer USB note) is a line, never a failure.
        for line in constraint_lines(compile_design(design)):
            print(line)
    return 0


def cmd_build(args: argparse.Namespace) -> int:
    path = Path(args.board)
    if not path.exists():
        print(f"no such file: {path}", file=sys.stderr)
        return 2
    result = build_job(path, upto=args.upto, force=args.force)
    print(json.dumps(result, indent=2, default=str))
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
    if args.constraints and result.get("constraints"):
        lines = list(result["constraints"]["lines"]) + [rules_line(result["rules"])]
        print(f"constraints: {len(lines)} lines (what the placement was checked against; pcbc check --constraints prints them alone)")
        for line in lines:
            print(f"  {line}")
    return 1 if result.get("error") else 0


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
