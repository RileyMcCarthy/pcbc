from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__
from .build import STAGES, build_job
from .language import check_board, load_board


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="pcbc", description="Python in, fab-ready KiCad out")
    parser.add_argument("--version", action="version", version=f"pcbc {__version__}")
    sub = parser.add_subparsers(dest="cmd", required=True)

    ck = sub.add_parser("check", help="Load board.py; unbound pins / USB / LCSC")
    ck.add_argument("board")
    ck.set_defaults(func=cmd_check)

    bd = sub.add_parser("build", help="check → seed → sch → place → route → fab")
    bd.add_argument("board")
    bd.add_argument("--upto", default="fab", choices=STAGES)
    bd.add_argument("--force", action="store_true")
    bd.set_defaults(func=cmd_build)

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


if __name__ == "__main__":
    raise SystemExit(main())
