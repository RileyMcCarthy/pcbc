"""M1: every core line is held to KiCad's own reading of it, at load, before anything is routed.

The load checks in `language` and `layout_job.load_core` are pcbc's reading of what KiCad keeps: a
table of words, layers, ranges and field dependencies written down by hand, one review at a time.
Each review found more rows missing from it — a through via on an inner layer that KiCad rewrites to
`F.Cu`-`B.Cu`, a stroke colour KiCad stores as 0-255 integers, a `keepout` KiCad completes, a
`margins` of the wrong length KiCad cannot parse — and every one of them was caught only after the
whole board had been routed (sixth review). The hand table cannot be finished; KiCad can be asked.

`round_trip` asks it. Every core object, stamped exactly as it will go onto the router's board, is
written onto **one probe board**: the placed board's header, layer stack, setup and net table, with
every layout primitive and every footprint taken off (no core object references a footprint: a core
group may only hold core objects). KiCad loads and saves that board (`kicad-cli pcb upgrade
--force`), the save is decompiled, and every core object is compared field for field with what was
written (`layout_job.diff_objects`, the comparison the build already makes after routing). Any
difference is a refusal naming `layout.core.py:N`, the field, the value written and the value KiCad
kept, with "write that". If KiCad cannot load the probe at all, the objects are bisected, one probe
board per half, down to the line KiCad refuses, which is named with KiCad's own message. A line the
emitter itself cannot write (a required field left `None`) is named the same way.

What this does not see: anything KiCad decides from the rest of the board — the router's copper,
the footprints, the fill. Those are the route stage's, and `layout_job` still compares every core
object after routing as the backstop (`KiCad did not keep a core line as written`).
"""

from __future__ import annotations

import re
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

PROBE = "probe.kicad_pcb"
WHAT = "KiCad's load of the line"


def probe_base(placed_text: str) -> str:
    """The placed board with every layout primitive and every footprint taken off: the header, the
    layer stack, the setup, the net table and the board properties, which is what a core line is
    read against."""
    from .layout_emit import strip_layout
    from .sexp import board_footprint_spans

    text = strip_layout(placed_text)
    out, pos = [], 0
    for s, e in board_footprint_spans(text):
        # the span starts at the '(' after "\n\t"; drop the newline and tab that introduced it too
        out.append(text[pos : s - 2])
        pos = e
    out.append(text[pos:])
    return "".join(out)


def _render(design, copper, graphics, *, name: str, keep_members: set[str] | None = None) -> str:
    """The core objects as the emitter writes them. A group's members outside `keep_members` are
    dropped (bisection: a half may not carry every member)."""
    from .layout_emit import render, render_graphics, uuid_of

    uuids = {o.id: uuid_of(o, name) for o in [*copper, *graphics]}
    gr = []
    for g in graphics:
        if keep_members is not None and "members" in g.f:
            from .model import Graphic

            g = Graphic(g.kind, g.id, {**g.f, "members": tuple(m for m in g.f["members"] if m in keep_members)}, g.source, g.comment, g.where)
        gr.append(g)
    return render(design, list(copper), board=name) + render_graphics(gr, board=name, uuids=uuids)


def _emit_errors(design, copper, graphics, *, name: str) -> list[str]:
    """Each core line the emitter cannot write, with the emitter's own message."""
    out = []
    ids = {o.id for o in [*copper, *graphics]}
    for o, alone in [*((c, ([c], [])) for c in copper), *((g, ([], [g])) for g in graphics)]:
        try:
            _render(design, *alone, name=name, keep_members=ids)
        except ValueError as exc:
            out.append(f"{o.where or o.id}: {exc}; KiCad cannot hold the line as written: give the field a value")
    return out


def kicad_save(text: str, *, design, name: str, cli: Path | None = None) -> tuple[str | None, str]:
    """(KiCad's save of `text`, "") or (None, KiCad's message). One `kicad-cli pcb upgrade --force`
    on a scratch copy beside the board's own sidecars (the compiled job's `.kicad_pro`/`.kicad_dru`)."""
    from .compile import compile_design
    from .fab import kicad_cli
    from .project import write_sidecars

    import shutil

    from .netcheck import KicadMissing

    cli = cli or kicad_cli()
    if not Path(cli).exists() and shutil.which(str(cli)) is None:
        raise KicadMissing(f"kicad-cli not found ({cli})")
    with tempfile.TemporaryDirectory(prefix="pcbc-core-probe-") as tmp:
        pcb = Path(tmp) / PROBE
        pcb.write_text(text)
        write_sidecars(design, compile_design(design), pcb, name=name)
        try:
            run = subprocess.run([str(cli), "pcb", "upgrade", "--force", str(pcb)], capture_output=True, text=True)
        except FileNotFoundError as exc:
            raise KicadMissing(f"kicad-cli not found ({cli})") from exc
        if run.returncode != 0:
            msg = (run.stderr or run.stdout or "no output").strip()
            msg = msg.replace(str(pcb), PROBE).replace(tmp, "")
            # KiCad stamps its warnings with the wall clock ("10:11:01 PM: Warning: ..."): not part of
            # the reason, and a refusal is worded the same on every run.
            msg = re.sub(r"(?m)^\d{1,2}:\d\d:\d\d(?: [AP]M)?: ", "", msg)
            return None, " ".join(msg.split())[-400:]
        return pcb.read_text(), ""


def _bisect(design, base: str, copper, graphics, *, name: str, msg: str, rec: dict) -> list[str]:
    """The lines KiCad refuses to load, found by halving: one probe board per half, both halves at
    once, down to single lines. A failure only the whole set shows (two lines together) is named as
    that set's lines with KiCad's message."""
    from .layout_emit import splice

    ids = {o.id for o in [*copper, *graphics]}
    objs = [*copper, *graphics]
    cu_ids = {o.id for o in copper}

    def loads(part: list) -> str | None:
        keep = {o.id for o in part}
        text = splice(base, _render(design, [o for o in part if o.id in cu_ids], [o for o in part if o.id not in cu_ids], name=name, keep_members=keep & ids))
        got, why = kicad_save(text, design=design, name=name)
        rec["kicad_calls"] += 1
        return None if got is not None else why

    def walk(part: list, why: str) -> list[str]:
        if len(part) == 1:
            o = part[0]
            return [f"{o.where or o.id}: KiCad refuses to load the line ({why}); write what KiCad reads, or delete the line"]
        half = len(part) // 2
        a, b = part[:half], part[half:]
        with ThreadPoolExecutor(2) as ex:
            ra, rb = ex.map(loads, (a, b))
        out = []
        if ra is not None:
            out += walk(a, ra)
        if rb is not None:
            out += walk(b, rb)
        if not out:
            where = ", ".join(o.where or o.id for o in part[:6])
            out.append(f"{where}: KiCad refuses to load these lines together ({why}); each loads alone")
        return out

    return walk(objs, msg)


def round_trip(design, copper, graphics, placed_text: str, *, name: str) -> tuple[list[str], dict]:
    """(refusals, record). `copper` is the stamped core copper, `graphics` the core drawings, exactly
    as the route stage will render them. Every refusal names `layout.core.py:N`. The record says how
    many objects were probed, how many KiCad calls it took and how long."""
    import time

    from .gen import Unmapped, decompile
    from .layout_emit import own_uuids, splice, uuid_of
    from .layout_job import diff_objects
    from .model import Graphic

    t0 = time.monotonic()
    rec = {"objects": len(copper) + len(graphics), "kicad_calls": 0}
    if not copper and not graphics:
        return [], rec
    base = probe_base(placed_text)
    try:
        block = _render(design, copper, graphics, name=name)
    except ValueError as exc:
        errs = _emit_errors(design, copper, graphics, name=name)
        return errs or [f"layout.core.py: {exc}"], rec
    saved, why = kicad_save(splice(base, block), design=design, name=name)
    rec["kicad_calls"] = 1
    if saved is None:
        refusals = _bisect(design, base, copper, graphics, name=name, msg=why, rec=rec)
        rec["bisected"] = True
        rec["seconds"] = round(time.monotonic() - t0, 2)
        return refusals, rec
    try:
        back = decompile(saved)
    except Unmapped as exc:
        rec["seconds"] = round(time.monotonic() - t0, 2)
        return [f"layout.core.py: KiCad's save of the core lines does not decompile ({exc}): report it"], rec
    by_uuid = {uuid_of(o, name): o for o in [*copper, *graphics]}
    inv = {u: o.id for u, o in by_uuid.items()}
    got = []
    for o in [*back.copper, *back.graphics]:
        mine = by_uuid.get(o.uuid)
        if mine is None:
            continue  # the probe base carries nothing else, but a dimension text or cell is not an object
        o.id, o.where = mine.id, mine.where
        if isinstance(o, Graphic) and "members" in o.f:
            o.f["members"] = tuple(inv.get(m, m) for m in o.f["members"])
        got.append(o)
    want = [replace(c, uuid=uuid_of(c, name)) for c in copper] + [Graphic(g.kind, g.id, own_uuids(g, name), g.source, g.comment, g.where) for g in graphics]
    refusals = diff_objects(want, got, what=WHAT)
    refusals = [r.replace(f"{WHAT} has no such object", f"{WHAT} has no such object: KiCad drops it on load (a group that holds itself or a group cycle, a generator with no members, or a line KiCad reads as something else)") for r in refusals]
    rec["seconds"] = round(time.monotonic() - t0, 2)
    return refusals, rec

