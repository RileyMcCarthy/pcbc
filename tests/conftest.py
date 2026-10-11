import os
import shutil as _shutil
import tempfile
from pathlib import Path

import pytest


def pytest_collection_modifyitems(config, items):
    skip_kicad = pytest.mark.skip(reason="kicad-cli (set PCBC_REQUIRE_KICAD=1)")
    for item in items:
        if item.get_closest_marker("kicad") and not os.environ.get("PCBC_REQUIRE_KICAD"):
            item.add_marker(skip_kicad)


# ---------------------------------------------------------------------------------------------
# Build artifacts a test may read, and where they come from.
#
# `examples/**/layout/` is in `.gitignore`: it is build output and nothing in it is tracked. Six
# test files read it anyway, so on a clean checkout 45 tests in `test_patterns.py` alone raised
# `FileNotFoundError` — measured with `git archive HEAD | tar -x` into an empty directory — and CI
# runs `pytest` **before** its first `pcbc build` in both jobs. The suite was green only on a
# machine that happened to have built the boards before, which makes every count it ever printed a
# statement about that machine rather than about the code.
#
# A **placed** board is a pure function of `board.py`: `pcb_job` is check + place (in memory) + the
# place-only emit, no subprocess, no KiCad, 0.02 s on blinky. So it is generated here rather than read, once per
# session, into pytest's own tmp directory. That removes the dependency on build state entirely —
# and removes a second bug with it, because the checked-in artifacts were *stale*: node's on-disk
# placed board differs from a fresh one at the placement level (`(at 17.1 17.3 -90)` against
# `(at 14.1 19.05 -90)`), so tests reading it were pinning numbers for a board pcbc no longer makes.
#
# A **routed** board needs KiCad (the emitted board is filled and judged by it) and up to a minute of
# routing. Tests that read one are marked `kicad`.
# ---------------------------------------------------------------------------------------------

_PLACED: dict[tuple[str, str], Path] = {}


SCH_CORE = "schematic.core.py"
_NOT_BOARDS = frozenset({"layout.core.py", SCH_CORE, "part.py"})


def _copy_core(board: Path, work: Path) -> None:
    """The `layout.core.py` and `schematic.core.py` beside `board`, copied beside its work copy: the board's placement
    and outline live in the one (docs/direction.md §2), its schematic layout in the other (docs/schematic-layout.md),
    so a copy of `board.py` alone is a board with neither."""
    for name in ("layout.core.py", SCH_CORE):
        core = Path(board).parent / name
        if core.exists():
            (work / name).write_text(core.read_text())


def write_board(dir: Path, name: str = "board.py", *, board: str, core: str | None = None, sch: str | None = None) -> Path:
    """`board` as `dir/name` and, when given, `core` as `dir/layout.core.py` beside it (docs/board-outline.md
    A.6): a test's circuit goes in the one, its placement and outline in the other. Schematic lines written in
    `board` (the old way) move to `dir/schematic.core.py` at the same line numbers (`split_sch`), as does `sch`."""
    dir = Path(dir)
    dir.mkdir(parents=True, exist_ok=True)
    path = dir / name
    board, moved = split_sch(board)
    path.write_text(board)
    if core is not None:
        (dir / "layout.core.py").write_text(core)
    if sch is not None or moved:
        (dir / SCH_CORE).write_text((sch or "") + (moved or ""))
    return path


_GEOM_NAMES = ("Place", "Region", "Keepout", "Outline")
_SIZE_KW = ("width", "height", "size_mm", "padding")


def _calls_in(node, names) -> bool:
    import ast

    return any(isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in names for n in ast.walk(node))


def split_board(text: str) -> tuple[str, str]:
    """A board text written the pre-2026-10-03 way, split into (board.py, layout.core.py), every line keeping
    its number in the file it lands in (docs/board-outline.md A.6): a `Place`/`Region`/`Keepout` statement
    (one line or many, or a loop that only places) is blanked in `board.py` and written at the same lines of
    the core text; `Board(width=, height=)` / `Board((w, h))` / `size_mm=` keeps its physics and its size
    becomes `Outline(width=, height=)` on the same line of the core text; `Place`, `Region` and `Keepout`
    leave a `from pcbc import` line. A text with none of these comes back with an empty core."""
    import ast

    try:
        tree = ast.parse(text)
    except SyntaxError:
        return text, ""
    lines = text.split("\n")
    board = list(lines)
    core = [""] * len(lines)
    moved = False

    def put(a: int, b: int, new: list[str], into: list[str]) -> None:
        for i in range(a - 1, b):
            into[i] = new[i - (a - 1)] if i - (a - 1) < len(new) else ""

    shared: dict[int, list] = {}
    for node in tree.body:
        if node.lineno == node.end_lineno:
            shared.setdefault(node.lineno, []).append(node)
    done: set[int] = set()
    for ln, nodes in shared.items():
        if len(nodes) < 2 or not any(_calls_in(n, _GEOM_NAMES) and not isinstance(n, ast.ImportFrom) for n in nodes):
            continue
        # `Place("R1", at=(5, 5)); SchPlace("R1", left=20, top=20)`: one line, two files.
        geo = [ast.get_source_segment(text, n) for n in nodes if _calls_in(n, _GEOM_NAMES)]
        rest = [ast.get_source_segment(text, n) for n in nodes if not _calls_in(n, _GEOM_NAMES)]
        core[ln - 1] = "; ".join(geo)
        board[ln - 1] = "; ".join(rest)
        done.update(id(n) for n in nodes)
        moved = True
    for node in tree.body:
        a, b = node.lineno, node.end_lineno
        if id(node) in done:
            continue
        if isinstance(node, ast.ImportFrom) and node.module == "pcbc" and any(al.name in _GEOM_NAMES for al in node.names):
            keep = [al for al in node.names if al.name not in _GEOM_NAMES]
            put(a, b, [f"from pcbc import {', '.join(al.name + (f' as {al.asname}' if al.asname else '') for al in keep)}" if keep else ""], board)
            continue
        call = node.value if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call) else None
        if call is not None and isinstance(call.func, ast.Name) and call.func.id == "Board":
            size = None
            if call.args:
                size = ast.unparse(call.args[0])
            kws = {k.arg: k for k in call.keywords}
            if "size_mm" in kws:
                size = ast.unparse(kws["size_mm"].value)
            w = ast.unparse(kws["width"].value) if "width" in kws else None
            h = ast.unparse(kws["height"].value) if "height" in kws else None
            if size is None and w is None and h is None:
                continue
            if size is not None:
                pair = ast.literal_eval(size)
                w, h = repr(pair[0]), repr(pair[1])
            rest = [k for k in call.keywords if k.arg not in _SIZE_KW]
            new = ast.Call(func=call.func, args=[], keywords=rest)
            indent = lines[a - 1][: len(lines[a - 1]) - len(lines[a - 1].lstrip())]
            put(a, b, [indent + ast.unparse(new)], board)
            core[a - 1] = f"Outline(width={w}, height={h})"
            moved = True
            continue
        if isinstance(node, ast.For) and _calls_in(node, _GEOM_NAMES) and all(isinstance(st, ast.Expr) for st in node.body) and not node.orelse:
            # A loop that places and draws (`Place(r, ...); SchPlace(r, ...)`): the loop in both files, each with its own calls.
            head = lines[a - 1]
            ind = head[: len(head) - len(head.lstrip())]
            for into, want in ((core, True), (board, False)):
                body: dict[int, list[str]] = {}
                for st in node.body:
                    if _calls_in(st, _GEOM_NAMES) == want:
                        body.setdefault(st.lineno, []).append(ast.get_source_segment(text, st))
                new = [head] + [""] * (b - a)
                for ln, stmts in body.items():
                    new[ln - a] = ind + "    " + "; ".join(stmts)
                if not body:
                    new[1 if b > a else 0] = (ind + "    pass") if b > a else head + " pass"
                put(a, b, new, into)
            moved = True
            continue
        if _calls_in(node, _GEOM_NAMES) and not _calls_in(node, ("Board", "NetReq", "Net", "Path", "Pair", "Bus", "Loop", "Isolation", "Guard", "Bridge", "Thermal", "SchPlace", "SchRegion")):
            put(a, b, lines[a - 1 : b], core)
            put(a, b, [], board)
            moved = True
    if not moved:
        return "\n".join(board), ""
    return "\n".join(board), "\n".join(core).rstrip("\n") + "\n"


_SCH_NAMES = ("SchPlace", "SchRegion", "SchStyle", "SchSheet", "SchNet", "SchText", "SchLabel", "SchPower")


def split_sch(text: str) -> tuple[str, str]:
    """A board text written the pre-schematic-core way, split into (board.py, schematic.core.py), every line keeping
    its number in the file it lands in (docs/schematic-layout.md §6): a `Sch*` statement (one line or many, a loop of
    them, or the `Sch*` half of a `Place(...); SchPlace(...)` line) is blanked in `board.py` and written at the same
    lines of the schematic core text. The import line is left as it is (`from pcbc import SchPlace` is not refused).
    A text with none comes back with an empty schematic core."""
    import ast

    try:
        tree = ast.parse(text)
    except SyntaxError:
        return text, ""
    lines = text.split("\n")
    board = list(lines)
    sch = [""] * len(lines)
    moved = False

    def put(a: int, b: int, new: list[str], into: list[str]) -> None:
        for i in range(a - 1, b):
            into[i] = new[i - (a - 1)] if i - (a - 1) < len(new) else ""

    shared: dict[int, list] = {}
    for node in tree.body:
        if node.lineno == node.end_lineno:
            shared.setdefault(node.lineno, []).append(node)
    done: set[int] = set()
    for ln, nodes in shared.items():
        if len(nodes) < 2 or not any(_calls_in(n, _SCH_NAMES) for n in nodes):
            continue
        mine = [ast.get_source_segment(text, n) for n in nodes if _calls_in(n, _SCH_NAMES)]
        rest = [ast.get_source_segment(text, n) for n in nodes if not _calls_in(n, _SCH_NAMES)]
        sch[ln - 1] = "; ".join(mine)
        board[ln - 1] = "; ".join(rest)
        done.update(id(n) for n in nodes)
        moved = True
    for node in tree.body:
        a, b = node.lineno, node.end_lineno
        if id(node) in done or isinstance(node, (ast.Import, ast.ImportFrom)):
            continue
        if isinstance(node, ast.For) and _calls_in(node, _SCH_NAMES) and all(isinstance(st, ast.Expr) for st in node.body) and not node.orelse:
            head = lines[a - 1]
            ind = head[: len(head) - len(head.lstrip())]
            mixed = any(not _calls_in(st, _SCH_NAMES) for st in node.body)
            if not mixed:
                put(a, b, lines[a - 1 : b], sch)
                put(a, b, [], board)
                moved = True
                continue
            for into, want in ((sch, True), (board, False)):
                body: dict[int, list[str]] = {}
                for st in node.body:
                    if _calls_in(st, _SCH_NAMES) == want:
                        body.setdefault(st.lineno, []).append(ast.get_source_segment(text, st))
                new = [head] + [""] * (b - a)
                for ln, stmts in body.items():
                    new[ln - a] = ind + "    " + "; ".join(stmts)
                put(a, b, new, into)
            moved = True
            continue
        if _calls_in(node, _SCH_NAMES):
            put(a, b, lines[a - 1 : b], sch)
            put(a, b, [], board)
            moved = True
    if not moved:
        return text, ""
    # docs/schematic-layout.md PR 5: the CSS forms are gone. A test text written before them carries `SchPlace("R1",
    # left=20, top=20)` only so that the old check passed; the auto-layout now draws those parts, so such a line is
    # dropped. Every other schematic line (an attach, a region with members) is kept as written.
    out = "\n".join(_drop_css("\n".join(sch)).split("\n")).rstrip("\n")
    return "\n".join(board), (out + "\n") if out.strip() else ""


_CSS_KW = {"left", "top", "right", "bottom", "width", "height", "parent", "position", "style", "title", "padding", "inset", "margin"}


def _drop_css(text: str) -> str:
    """Blank every `SchPlace`/`SchRegion` call that says nothing but CSS (and a region's title): the old forms."""
    import ast

    lines = text.split("\n")
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return text
    for node in ast.walk(tree):
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call) and isinstance(node.value.func, ast.Name) and node.value.func.id in ("SchPlace", "SchRegion"):
            kws = {k.arg for k in node.value.keywords}
            if kws and kws <= _CSS_KW and "members" not in kws:
                seg = ast.get_source_segment(text, node)
                a = node.lineno - 1
                lines[a] = lines[a].replace(seg, "pass" if lines[a].strip() == seg.strip() and lines[a].startswith((" ", "\t")) else "").rstrip().rstrip(";")
    return "\n".join(lines)


def write_split(path: Path, text: str) -> Path:
    """`text` (a board written with its placement and outline in it, the old way) written as `path` and the
    `layout.core.py` beside it (`split_board`). An existing core file is replaced only when the text has
    geometry of its own."""
    path = Path(path)
    board, core = split_board(text)
    board, sch = split_sch(board)
    path.write_text(board)
    if sch:
        (path.parent / SCH_CORE).write_text(sch)
    elif not (path.parent / SCH_CORE).exists():
        src = _known_boards().get(text)
        if src is not None and (src.parent / SCH_CORE).exists():
            (path.parent / SCH_CORE).write_text((src.parent / SCH_CORE).read_text())
    if core:
        (path.parent / "layout.core.py").write_text(core)
    elif not (path.parent / "layout.core.py").exists():
        # A copy of an example's or a fixture's board.py, written whole: its placement is the core file beside
        # the original, which goes with it.
        src = _known_boards().get(text)
        if src is None:
            # An edited copy of an example (`node.py` with planes=[]) or a fixture (`thermal.py` with no Thermal()): its
            # core file, by name, an example's first.
            named = [b for b in _known_boards().values() if b.name == path.name]
            src = next((b for b in named if b.parent.parent.name == "examples"), named[0] if named else None)
        if src is not None:
            _copy_core(src, path.parent)
    return path


_CORE_MARK = "# -- the board's own placement and outline (tests/conftest.py put_core) --\n"


def put_core(board: Path, body: str) -> Path:
    """`body` written as the first lines of the `layout.core.py` beside `board` (the board.py, or that core path),
    ahead of the placement and outline the board's core file holds, so a test's core line keeps its number
    (`layout.core.py:1`) and the board keeps its placement. That placement is what the core file beside it holds
    (below an earlier `put_core`'s body), or, with none there, the known example's or fixture's core file for a board
    copied verbatim. The design is loaded afterwards: the core file runs with `board.py` (board-outline.md A.2)."""
    board = Path(board)
    core = board.parent / "layout.core.py"
    boards = [board] if board.name != "layout.core.py" else sorted(b for b in board.parent.glob("*.py") if b.name not in _NOT_BOARDS)
    if core.exists():
        # the board's own core file, copied with it (or an earlier put_core's, below its body)
        have = core.read_text()
        base = have.split(_CORE_MARK, 1)[1] if _CORE_MARK in have else have
    else:
        src = next((_known_boards()[b.read_text()] for b in boards if b.read_text() in _known_boards()), None)
        base = (src.parent / "layout.core.py").read_text() if src is not None else ""
    core.write_text(body + ("\n" if body and not body.endswith("\n") else "") + (_CORE_MARK + base if base else ""))
    return core


def core_load(core: Path):
    """The design of the board beside `core` (a `layout.core.py`), loaded whole: `board.py`, then the core file under
    its own trace (`language.load_board`). The pre-2026-10-03 tests ran `load_layout(core, design, source="core")`
    on a design already loaded; the core file now runs once, with its board."""
    from pcbc.language import load_board

    core = Path(core)
    board = next(b for b in sorted(core.parent.glob("*.py")) if b.name not in _NOT_BOARDS and "Board(" in b.read_text())
    return load_board(board)


_KNOWN: dict[str, Path] | None = None


def _known_boards() -> dict[str, Path]:
    """{board text: its file} for every example and fixture board that has a `layout.core.py` beside it."""
    global _KNOWN
    if _KNOWN is None:
        root = Path(__file__).resolve().parent
        _KNOWN = {}
        for core in [*(root.parent / "examples").glob("*/layout.core.py"), *(root / "fixtures").rglob("layout.core.py")]:
            for b in core.parent.glob("*.py"):
                if b.name not in _NOT_BOARDS:
                    _KNOWN.setdefault(b.read_text(), b)
    return _KNOWN


_GEOM_LINE = __import__("re").compile(r"^(Place|Region|Keepout|Outline)\(.*\)\s*(#.*)?$")


def _whole(text: str, old: str) -> bool:
    """`old` in `text` where it is not the tail of a longer name (`Place("J1"` inside `SchPlace("J1"`)."""
    import re

    return re.search(r"(?<![A-Za-z0-9_])" + re.escape(old), text) is not None


def edit_board(board: Path, edits, *, strict: bool = True, every: bool = False, once: bool = False) -> None:
    """Apply `(old, new)` text edits to `board` and the `layout.core.py` beside it, each to the file that holds
    `old` (an example's Place lines are in its core file, its NetReq lines in board.py). A one-line `Place`,
    `Region`, `Keepout` or `Outline` an edit writes into `board.py` (a test adding a part with its placement)
    is moved to the end of `layout.core.py`, where it belongs (docs/direction.md §2)."""
    board = Path(board)
    core = board.parent / "layout.core.py"
    schf = board.parent / SCH_CORE
    texts = {board: board.read_text(), core: core.read_text() if core.exists() else "", schf: schf.read_text() if schf.exists() else ""}
    for old, new in edits:
        order = (schf, board, core) if old.lstrip().startswith(_SCH_NAMES) else (core, board, schf) if old.lstrip().startswith(_GEOM_NAMES) else (board, core, schf)
        hit = next((f for f in order if _whole(texts[f], old)), None) or next((f for f in order if old in texts[f]), None)
        if hit is None:
            if strict:
                raise AssertionError(f"{old!r} is in none of {board.name}, layout.core.py and {SCH_CORE}")
            continue
        if once:
            assert texts[hit].count(old) == 1, old
        if _whole(texts[hit], old):
            import re

            texts[hit] = re.sub(r"(?<![A-Za-z0-9_])" + re.escape(old), lambda _m: new, texts[hit], count=0 if every else 1)
        else:
            texts[hit] = texts[hit].replace(old, new) if every else texts[hit].replace(old, new, 1)
    btext, ctext = split_board(texts[board])
    if ctext:
        # Geometry an edit wrote into board.py moves to the end of the core file; a size becomes the Outline().
        texts[board] = "\n".join(ln for ln, was in zip(btext.split("\n"), texts[board].split("\n")) if ln or not was.strip() or was.lstrip().startswith("Board("))
        add = [ln for ln in ctext.split("\n") if ln.strip()]
        have = texts[core].split("\n")
        for ln in [x for x in add if x.startswith("Outline(")]:
            at = next((i for i, h in enumerate(have) if h.startswith("Outline(")), None)
            if at is None:
                have.insert(0, ln)
            else:
                have[at] = ln
        rest = [x for x in add if not x.startswith("Outline(")]
        texts[core] = "\n".join(have).rstrip("\n") + ("\n" if "\n".join(have).strip() else "") + ("\n".join(rest) + "\n" if rest else "")
    scht, geo = split_board(texts[schf]) if texts[schf] else ("", "")
    if geo:
        # A Place an edit wrote beside a schematic line (`('SchRegion(', 'Place("R4", ...)\nSchRegion(')`) is layout.core.py's.
        texts[schf] = "\n".join(ln for ln, was in zip(scht.split("\n"), texts[schf].split("\n")) if ln or not was.strip())
        texts[core] = texts[core].rstrip("\n") + ("\n" if texts[core].strip() else "") + "\n".join(x for x in geo.split("\n") if x.strip()) + "\n"
    btext, stext = split_sch(texts[board])
    if stext:
        # A schematic line an edit wrote into board.py moves to the end of schematic.core.py.
        texts[board] = "\n".join(ln for ln, was in zip(btext.split("\n"), texts[board].split("\n")) if ln or not was.strip())
        add = [ln for ln in stext.split("\n") if ln.strip()]
        have = texts[schf]
        texts[schf] = have.rstrip("\n") + ("\n" if have.strip() else "") + "\n".join(add) + "\n"
    board.write_text(texts[board])
    if texts[core] or core.exists():
        core.write_text(texts[core])
    if texts[schf] or schf.exists():
        schf.write_text(texts[schf])


def copy_board(src: Path, dst_dir: Path, edits=(), *, name: str | None = None) -> Path:
    """`src` (a board.py) copied into `dst_dir` with the `layout.core.py` beside it, then `edit_board`."""
    dst_dir = Path(dst_dir)
    dst_dir.mkdir(parents=True, exist_ok=True)
    out = dst_dir / (name or Path(src).name)
    out.write_text(Path(src).read_text())
    _copy_core(Path(src), dst_dir)
    edit_board(out, edits)
    return out
_ROOT: Path | None = None


def _key(name: str, board: Path) -> tuple[tuple[str, str], str]:
    """The cache key and the work directory's name for one board: by name **and** by source file, so
    the migrated example and the pre-migration vehicle (both `buck`) never share a cached build."""
    import hashlib

    src = str(Path(board).resolve())
    return (name, src), f"{name}-{hashlib.sha256(src.encode()).hexdigest()[:8]}"


def placed_board(name: str, board: Path) -> Path:
    """The placed layout for `name`, generated from its `board.py` and cached for the session."""
    from pcbc.build import pcb_job

    global _ROOT

    key, dirname = _key(name, board)
    if key in _PLACED and _PLACED[key].exists():
        return _PLACED[key]
    if _ROOT is None:
        _ROOT = Path(tempfile.mkdtemp(prefix="pcbc-placed-"))
    work = _ROOT / dirname
    work.mkdir(parents=True, exist_ok=True)
    src = work / board.name
    src.write_text(board.read_text())
    _copy_core(board, work)
    for extra in ("components",):
        if (board.parent / extra).exists() and not (work / extra).exists():
            _shutil.copytree(board.parent / extra, work / extra)
    result = pcb_job(src)
    if result.get("error"):
        raise RuntimeError(f"placing {name}: {result['error']}")
    _PLACED[key] = Path(result["placed"])
    return _PLACED[key]


_ROUTED: dict[tuple[str, str], tuple[Path, dict]] = {}


def routed_result(name: str, board: Path) -> tuple[Path, dict]:
    """(the emitted board, the build result) for `name`, built natively from its `board.py` to the route
    stage and cached for the session. The board is returned **also when the only error is unrouted
    nets** (N0: the native router may leave a net open; the build still emits, fills and runs every
    gate on what it wrote). Any other error raises. Five boards cost five builds however many tests ask.

    Why generated and not read from `examples/**/layout/`: that directory is gitignored build output;
    whatever is in it is whatever somebody last built."""
    from pcbc.build import build_job

    key, dirname = _key(name, board)
    if key in _ROUTED and _ROUTED[key][0].exists():
        return _ROUTED[key]
    global _ROOT
    if _ROOT is None:
        _ROOT = Path(tempfile.mkdtemp(prefix="pcbc-placed-"))
    work = _ROOT / "routed" / dirname
    work.mkdir(parents=True, exist_ok=True)
    src = work / board.name
    src.write_text(board.read_text())
    _copy_core(board, work)
    for extra in ("components",):
        if (board.parent / extra).exists() and not (work / extra).exists():
            _shutil.copytree(board.parent / extra, work / extra)
    result = build_job(src, upto="route", force=True)
    if result.get("error") and not str(result["error"]).startswith("unrouted:"):
        raise RuntimeError(f"routing {name}: {result['error']}")
    out = work / "layout" / src.stem / "routed" / "layout.kicad_pcb"
    if not out.exists():
        raise RuntimeError(f"routing {name}: no routed board at {out}")
    _ROUTED[key] = (out, result)
    return _ROUTED[key]


def routed_board(name: str, board: Path) -> Path:
    """The emitted board of `routed_result` (complete or not)."""
    return routed_result(name, board)[0]


def complete_board(name: str, board: Path) -> Path:
    """The emitted board, only when the native router finished every net; raises otherwise, so a test
    that needs a complete board says so (N0: c3_usb, node and the DS2 Addon may not complete yet)."""
    out, result = routed_result(name, board)
    if result.get("unrouted"):
        raise RuntimeError(f"N0: {name} has unrouted nets ({', '.join(result['unrouted'])})")
    return out


# ---------------------------------------------------------------------------------------------
# The DS2 Addon: the owner's real board, outside this repo, read only.
#
# `Path()` replaced `Chain()` (`docs/net-path-physics.md` §1.3) and a `Chain()` line is refused at load
# until the owner edits his own file. So every test reads the DS2 Addon through this one helper: the
# directory copied whole into a temp directory (nothing is ever written under ~/Documents/MaD), and,
# while the copy still says `Chain(`, the **exact** rewrite the refusal prints applied to the copy
# (`language.chain_rewrite`). One test pins that refusal against the real file.
# ---------------------------------------------------------------------------------------------

DS2_REAL = Path.home() / "Documents" / "MaD" / "Hardware" / "DS2Addon" / "pcbc"
_DS2_COPY: Path | None = None


def ds2_dir() -> Path:
    """The DS2 Addon's directory as the suite reads it: a session temp copy, `Chain()` rewritten to
    `Path()` the way the refusal says. The real path when the board is not checked out here (every
    reader skips on `exists()`)."""
    global _DS2_COPY
    if not (DS2_REAL / "ds2_addon.py").exists():
        return DS2_REAL
    if _DS2_COPY is not None and (_DS2_COPY / "ds2_addon.py").exists():
        return _DS2_COPY
    from pcbc.language import chain_rewrite

    root = Path(tempfile.mkdtemp(prefix="pcbc-ds2-")) / "pcbc"
    _shutil.copytree(DS2_REAL, root)
    board = root / "ds2_addon.py"
    text = board.read_text()
    if "Chain(" in text:
        text = chain_rewrite(text, board.name)[0]
    text = ds2_migrate(text)
    if not (root / "layout.core.py").exists():
        text = ds2_part_a(text, root)
    if not (root / SCH_CORE).exists():
        text = ds2_sch(text, root)
    board.write_text(text)
    _DS2_COPY = root
    return root


# docs/board-outline.md A.6: the owner's `layout.core.py` for the DS2 Addon, exactly as the doc gives it to him.
DS2_CORE = """\
# Rev 1's outline: 47 x 25.4 mm, the headers on the edges they were on (the EdgeBoard mates to it).
Outline(width=47, height=25.4)

# Copper: headers on their rev 1 edges; the ADC between the gauge side and the EdgeBoard side.
Place("J2", edge="left", reason="gauge supply and bridge")
Place("J5", edge="right", reason="bridge, second pair")
Place("J4", edge="top", right=8, reason="reference, off to the side so the ADC's top pin row has room")
Place("J1", edge="bottom", left=12, reason="EdgeBoard")
Place("J3", edge="bottom", right=8, reason="GPIO")
Place("U1", position="absolute", left=20, top=10, reason="ADC in the middle")
# Only the decoupling and the pull-up crowd the ADC: a TSSOP's 0.65 mm pad rows need their
# escapes. The series resistors go at the MCU header, each filter at the connector it serves.
Place("C5", to="U1.DVDD")
Place("C4", to="U1.AVDD")
Place("C6", to="U1.DVDD")
Place("R10", to="U1.~{RESET}")
Place("R3", to="J1.3")
Place("R4", to="J1.4")
Place("R5", to="J1.5")
Place("R6", to="J2.3")
Place("R7", to="J2.4")
Place("C7", to="R6.2")
Place("C8", to="R6.2")
Place("C9", to="R7.2")
Place("R8", to="J5.1")
Place("R9", to="J5.2")
Place("C10", to="R8.2")
Place("C11", to="R8.2")
Place("C12", to="R9.2")
Place("R1", to="J4.1")
Place("R2", to="J4.2")
Place("C1", to="R1.2")
Place("C2", to="R1.2")
Place("C3", to="R2.2")
"""


def ds2_part_a(text: str, root: Path) -> str:
    """docs/board-outline.md A.6's Part-A step, applied to the temp copy only while it still has the old lines:
    `DS2_CORE` written beside it, line 15's import without `Place`, line 94's `Board()` without its size, and
    lines 93 and 135-165 blanked so every other line keeps its number (`test_ds2_addon_lines_are_pinned` pins
    lines up to 134)."""
    lines = text.split("\n")
    if not any(ln.startswith("Place(") for ln in lines):
        return text
    assert lines[93].startswith("Board(width=47, height=25.4"), lines[93]
    lines[14] = lines[14].replace(" Place,", "")
    lines[93] = 'Board(layers=2, stackup="jlcpcb_2l_1oz")'
    lines[92] = ""
    for i in range(134, 165):
        lines[i] = ""
    (root / "layout.core.py").write_text(DS2_CORE)
    return "\n".join(lines).rstrip("\n") + "\n"


# DS2's schematic.core.py after docs/schematic-layout.md §6 (PR 5 and its implementation status): the three groups
# in reading order. (The three SchPower locks of 2026-10-08's first migration are gone: the auto-layout now places
# those power symbols clear of their neighbours itself, and the sheet scores the same with one fewer upside-down symbol.)
DS2_SCH_CORE = """\
# Schematic: the tool lays the sheet out from the netlist; these lines name the three groups and their reading order.
SchRegion("inputs", title="Inputs", col=0, members=("J2", "J5", "J4", "R6", "R7", "C7", "C8", "C9", "R8", "R9", "C10", "C11", "C12", "R1", "R2", "C1", "C2", "C3"))
SchRegion("adc", title="ADC", col=1, members=("U1", "C4", "C5", "C6", "R10"))
SchRegion("edge", title="EdgeBoard", col=2, members=("J1", "J3", "R3", "R4", "R5"))
"""


def ds2_sch(text: str, root: Path) -> str:
    """docs/schematic-layout.md §6's DS2 step, applied to the temp copy only while it still has the schematic lines:
    `DS2_SCH_CORE` written as the `schematic.core.py` beside it, lines 99-133 (the comment, three `SchRegion` and 28
    `SchPlace`) blanked in the board so every other line keeps its number, and `SchPlace, SchRegion` taken out of line
    15's import."""
    lines = text.split("\n")
    if not any(ln.startswith(("SchPlace(", "SchRegion(")) for ln in lines):
        return text
    assert lines[98].startswith("# Schematic:") and lines[132].startswith('SchPlace("J3"'), (lines[98], lines[132])
    (root / SCH_CORE).write_text(DS2_SCH_CORE)
    for i in range(98, 133):
        lines[i] = ""
    lines[14] = lines[14].replace(" SchPlace, SchRegion,", "")
    return "\n".join(lines)


# The lines `docs/physics-subjects.md` §8 moves on the DS2 Addon, each the owner's line exactly as his file
# has it and what the tool accepts in its place. Applied to the temp copy only while his file still has the
# old line; the real file's refusal for each is pinned by `test_path.py` against the file itself.
DS2_MIGRATIONS: list[tuple[str, str]] = [
    # PR 3: a NetReq declares no current (E1); PR 4: a Ground() net takes no volts= (E3).
    (
        'NetReq("3V3", "GND", "VDDA", "VSS", kind="power", volts=3.3, amps=0.1)\n',
        # Two statements on the owner's one line, so every later line of his file keeps its number.
        'NetReq("3V3", "VDDA", kind="power", volts=3.3); NetReq("GND", "VSS", kind="power")\n',
    ),
]


def ds2_migrate(text: str) -> str:
    """`text` with every `DS2_MIGRATIONS` line its owner has not yet changed replaced."""
    for old, new in DS2_MIGRATIONS:
        if old in text:
            text = text.replace(old, new)
    return text


# ---------------------------------------------------------------------------------------------
# The pipeline's test vehicle.
#
# `examples/buck` migrated to Paths (docs/net-path-physics.md §5, PR 10) and its switch path is left
# open as placed: C_IN1 pinches the 0.781 mm channel out of U1.SW to 0.46 mm, and the build says so with
# a move. Dozens of tests use buck as a **complete board to drive the pipeline** (locks, round trips,
# fab, gates), not to test buck's own physics. They read the board as it shipped before the migration
# (`tests/fixtures/buck_netreq/`, its components a link to the example's), which still completes; the
# example itself is held by the tests about it (`test_examples_fab.py::test_migrated_buck_*`,
# `test_constraints.py`, `test_route_checks.py`, `test_paths_compile.py`, `test_junctions.py`).
# ---------------------------------------------------------------------------------------------

BUCK_NETREQ = Path(__file__).resolve().parent / "fixtures" / "buck_netreq"


class _Vehicles(type(Path())):
    """`EXAMPLES` for a pipeline test: `/ "buck"` is the pre-migration buck; blinky, c3_usb and node are the examples as
    they shipped with a declared outline (`declared_examples`, docs/board-outline.md PR 4); any other name the example."""

    def __truediv__(self, key):
        if str(key) == "buck":
            return BUCK_NETREQ
        if str(key) in ("blinky", "c3_usb", "node"):
            return Path(__file__).resolve().parent / "fixtures" / "declared" / str(key)
        return Path(str(self)) / key


def vehicle_examples() -> Path:
    return _Vehicles(str(Path(__file__).resolve().parent.parent / "examples"))


# ---------------------------------------------------------------------------------------------
# The examples as they shipped with a declared outline (docs/board-outline.md PR 4).
#
# PR 4 dropped the examples' `Outline()`: their outline is derived now, and the derivation moves parts (B.2:
# relations limited to the anchors' box, edge groups docked). Tests that use an example as a **vehicle** for a
# router, pattern or placement-check feature — measured on the placement it had — read the board as it shipped
# before PR 4: `tests/fixtures/declared/<name>/`, its board.py the example's, its layout.core.py the one with
# `Outline()` and the anchors B.7 changed, its components a link to the example's. The examples themselves are
# held by the tests about them (`test_board_outline.py`, `test_constraints.py`'s compiled fixtures, the layout bars).
# ---------------------------------------------------------------------------------------------

DECLARED = Path(__file__).resolve().parent / "fixtures" / "declared"


def declared_examples() -> Path:
    """`EXAMPLES` for a vehicle test: `/ name` is the example with its pre-PR 4 declared outline."""
    return DECLARED


def copy_board_file(dst: Path, src: Path) -> Path:
    """`src` (a board.py) written as `dst`, with the `layout.core.py` beside `src` copied beside `dst`: a copy of a board
    is a copy of its placement too (docs/direction.md §2). A board with no core file beside it is copied alone."""
    dst, src = Path(dst), Path(src)
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(src.read_text())
    _copy_core(src, dst.parent)
    return dst
