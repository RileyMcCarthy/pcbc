from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__
from .build import STAGES, build_job
from .language import check_board, load_board
from .netcheck import KicadMissing, check_erc, check_schematic
from .project import layout_dir
from .review import review_job
from .sch_emit import emit_schematic_file


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="pcbc", description="Python in, fab-ready KiCad out")
    parser.add_argument("--version", action="version", version=f"pcbc {__version__}")
    sub = parser.add_subparsers(dest="cmd", required=True)

    ck = sub.add_parser("check", help="Load board.py; unbound pins, Place(), LCSC")
    ck.add_argument("board")
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

    rv = sub.add_parser("review", help="HTML: schematic, copper, 3D")
    rv.add_argument("board")
    rv.add_argument("--no-open", action="store_true")
    rv.set_defaults(func=cmd_review)

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
        return 1
    design = load_board(path)
    print(json.dumps({"ok": True, "instances": len(design.instances), "nets": len(design.nets)}))
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
    fails = check_board(path)
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


if __name__ == "__main__":
    raise SystemExit(main())
