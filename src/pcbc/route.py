"""Copper by KiCadRoutingTools (KRT). The AI never draws a track.

The route stage compiles `NetReq` intent into an ordered KRT plan and runs it:

1. constrained nets first (`vias=False`, or a single layer): each group on its layers
   only, while the board is empty, so a switch node or an analog trace gets the straight shot;
2. differential pairs (`pair=True`) as coupled pairs;
3. on four layers the declared `planes=` pours, with via taps;
4. everything else, power nets at the width their amps ask for;
5. on two layers a GND pour on the back, then one more pass to tie what the pour missed.

`netcheck.check_copper` then asks KiCad itself (DRC, unconnected items) and diffs the copper
netlist against `board.py`; the fab stage checks vias on no-via nets and ampacity. KRT's
geometry is bit-identical run to run; only the ids it invents differ, so they are re-keyed
here and `pcbc build` stays byte-for-byte. KRT never rewrites the board's design rules.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from .compile import CompiledJob, compile_design
from .copper import unrouted_nets
from .model import Design
from .project import copy_with_siblings
from .sexp import matching_paren, stable_uuid

if TYPE_CHECKING:  # `patterns` sits on top of `route_scene`/`route_emit`, never on this module
    from .patterns import PatternPlan

KRT_REPO = "https://github.com/drandyhaas/KiCadRoutingTools.git"
KRT_SHA = "3244726b2c15668fb109a0bb24384750a054af40"

PCBC_STEP = "pcbc"
"""What stands in a step command's first slot when the step is pcbc's own and not a KRT process.

The rest of the command keeps every position a KRT step keeps: the board it reads in slot 4, the
board it writes in slot 5. So the plan stays one list of steps, `test_route_plan.py`'s chain
assertion reads it unchanged, the step files number and sort in run order, and `blocking.step_boards`
names `patterns_post` as the step that placed a tap without knowing anything about patterns."""

_UUID = re.compile(r'\(uuid\s+"([^"]*)"\)')
_COPPER_ITEM = re.compile(r"\n\t\((segment|via|zone|arc)\b")


def krt_home() -> Path:
    return Path(os.environ.get("KRT_HOME") or (Path.home() / "Downloads" / "KiCadRoutingTools"))


def krt_python(home: Path) -> Path:
    venv = home / ".venv" / "bin" / "python"
    return venv if venv.exists() else Path(sys.executable)


def krt_missing(home: Path | None = None) -> str | None:
    """Why KRT cannot run, with the fix; None when it can."""
    home = home or krt_home()
    if (home / "py_router" / "route.py").exists():
        return None
    return (
        f"KiCadRoutingTools not found at {home} (set KRT_HOME, or): "
        f"git clone {KRT_REPO} {home} && cd {home} && git checkout {KRT_SHA} && "
        f"python3 -m venv .venv && .venv/bin/pip install -r requirements.txt && .venv/bin/python build_router.py"
    )


def krt_version(home: Path) -> str:
    v = home / "VERSION"
    return v.read_text().strip() if v.exists() else "?"


def copper_layers(n: int) -> list[str]:
    inner = [f"In{i}.Cu" for i in range(1, max(n, 2) - 1)]
    return ["F.Cu", *inner, "B.Cu"]


def pin_copper_ids(text: str, board: str, keep: frozenset[str] = frozenset()) -> str:
    """Re-key every segment / via / zone uuid by its order on the sheet — except `keep`'s.

    pcbc's own copper derives its uuid from the same geometry key the sidecar is keyed by
    (`route_emit.piece_key`), so a piece stays traceable in KiCad's UI and `copper.json` cannot
    silently point at ids that no longer exist. KRT's invented ids are still re-keyed exactly as
    before, and a kept id still consumes its position, so nothing else moves (C.3).
    """
    out: list[str] = []
    pos = 0
    counts: dict[str, int] = {}
    while True:
        m = _COPPER_ITEM.search(text, pos)
        if not m:
            out.append(text[pos:])
            break
        tag = m.group(1)
        open_at = m.start() + 2
        end = matching_paren(text, open_at)
        block = text[open_at : end + 1]
        i = counts.get(tag, 0)
        counts[tag] = i + 1
        have = _UUID.search(block)
        mine = have is not None and have.group(1) in keep
        if not mine:
            block = _UUID.sub(f'(uuid "{stable_uuid(board, tag, i)}")', block, count=1)
        out.append(text[pos:open_at])
        out.append(block)
        pos = end + 1
    return "".join(out)


# ---------------------------------------------------------------- the plan


def _class(job: CompiledJob, name: str):
    return next((c for c in job.classes if c.name == name), None)


def _fmt(v: float) -> str:
    return f"{v:g}"


def _net_names(design: Design, patterns) -> list[str]:
    from fnmatch import fnmatch

    return sorted(n for n in design.nets if any(fnmatch(n, p) for p in patterns))


_ITEM_NET = re.compile(r'\(net\s+(?:(\d+)|(?:\d+\s+)?"([^"]*)")\)')


def lock_copper(text: str, nets: set[str]) -> str:
    """`(locked yes)` on every segment and via of these nets: KiCad-locked copper is never
    ripped by a later KRT step, so a net routed under its own rules stays as routed."""
    names = {n: name for n, name in re.findall(r'\(net (\d+) "([^"]*)"\)', text)}
    out: list[str] = []
    pos = 0
    for m in re.finditer(r"\n\t\((segment|via)\b", text):
        if m.start() < pos:
            continue
        end = matching_paren(text, m.start() + 2)
        block = text[m.start() : end + 1]
        nm = _ITEM_NET.search(block)
        net = (names.get(nm.group(1)) if nm and nm.group(1) else (nm.group(2) if nm else None))
        if net in nets and "(locked" not in block:
            # Where KiCad writes it and KRT's parser reads it: after (width) on a segment, after
            # (layers) on a via. Anywhere else and KRT never sees the segment at all.
            block = re.sub(r"(\n\t\t\(width [^\n]*\)\n|\n\t\t\(layers [^\n]*\)\n)", r"\1\t\t(locked yes)\n", block, count=1)
        out.append(text[pos : m.start()])
        out.append(block)
        pos = end + 1
    out.append(text[pos:])
    return "".join(out)


def krt_plan(job: CompiledJob, design: Design, placed: Path, work: Path, home: Path, plan: "PatternPlan | None" = None, post: bool = False) -> list[tuple[str, list[str]]]:
    """(step name, command) pairs. Pure: the same board.py gives the same plan.

    `plan` is the `PatternPlan` the pattern stage handed back (C.4). What it changes: `local_hops` is
    built from the hops the pattern **refused** and the step disappears when there are none; each
    `{class}_nets` step drops the nets patterns finished; and `signals` gains a `!NET` for every one
    of them, belt and braces, since KRT would skip them anyway. Everything else is untouched, so a
    board where patterns fit nothing routes exactly as it did before R2.

    `post` reserves pcbc's own second stage as a step of the plan (C.1): it sits after `planes` and
    before `plane_taps` and `signals`, on both stackups, and it is a step file like any other so
    `blocking.step_boards` can name it as the step that placed a piece. It is not a KRT command —
    `route_job` runs it in process — and it is marked by `PCBC_STEP` in the command's first slot,
    with the board it reads and the board it writes in the two slots every step keeps them in.
    """
    done = set(plan.done) if plan is not None else set()
    py = str(krt_python(home))
    router = home / "py_router"
    from .stackup import get_stackup

    stack = get_stackup(job.stackup)
    layers = copper_layers(job.layers)
    floor = stack.clearance_min
    grid = "0.05" if job.layers <= 2 else "0.1"
    via = ["--via-size", _fmt(stack.via_diameter), "--via-drill", _fmt(stack.via_drill)]
    steps: list[tuple[str, list[str]]] = []
    prev = placed

    # The fab's floor, pinned: KRT otherwise escalates to smaller "advanced" vias at fine-pitch
    # pads and the board's own constraints then reject them. Classes already clear a via's hole.
    overrides = work / "fab_overrides.txt"
    # `--clearance` is a CEILING on every class (min(class, value)); the classes carry the numbers, so
    # steps only name it where one class is meant.
    default = _class(job, "Default")
    default_clear = default.clearance_mm if default else floor

    def step(name: str, tool: str, args: list[str]) -> None:
        nonlocal prev
        out = work / f"{len(steps) + 1:02d}_{name}.kicad_pcb"
        common = ["--no-fix-drc-settings", "--grid-step", grid, *via, "--fab-overrides", str(overrides)]
        if tool == "route_planes.py" or name in ("signals", "local_hops"):
            # Keeps every via out of same-net SMD pads (a via in an 0603 pad wicks solder; the
            # fab stage refuses it on a passive): the long nets' vias, and a hop's via if it ever
            # needs one. Not on the constrained steps (no vias there anyway).
            common += ["--same-net-pad-clearance", _fmt(floor)]
        elif name == "plane_taps":
            # KRT's pour places no tap vias (its "bare pour"); the route step welds each pad to the
            # plane, and with the keepout on it welded nothing (node: 4 of 59 GND pads). KRT records
            # the keepout in the sibling project, so this step must say no explicitly.
            common += ["--same-net-pad-clearance", "-1"]
        if tool != "route_planes.py":
            common.append("--keep-input-copper")  # planes keep it regardless
        steps.append((name, [py, "-X", "utf8", str(router / tool), str(prev), str(out), *args, *common]))
        prev = out

    def pcbc_step(name: str) -> None:
        """One of pcbc's own stages, in the same shape as a KRT step: the board it reads in slot 4,
        the board it writes in slot 5, so the chain, the step files and the stale-file sweep all work
        on it unchanged. `route_job` runs it in process; nothing is spawned."""
        nonlocal prev
        out = work / f"{len(steps) + 1:02d}_{name}.kicad_pcb"
        steps.append((name, [PCBC_STEP, "-X", "utf8", "patterns", str(prev), str(out), "--stage", "post"]))
        prev = out

    # The constrained nets: no vias, or a single layer. Grouped by (layers, class), named nets only.
    constrained: list[str] = []
    groups: dict[tuple[tuple[str, ...], str], list[str]] = {}
    for cn in job.nets:
        if cn.autoroute == "diff_pair":
            continue
        if cn.vias is False or len(cn.layers) == 1:
            names = _net_names(design, cn.patterns)
            if names:
                lays = tuple(cn.layers[:1]) if cn.vias is False else tuple(cn.layers)
                groups.setdefault((lays, cn.class_name), []).extend(names)
    for (_lays, _cls_name), names in sorted(groups.items()):
        constrained += sorted(set(names))

    # 0. The escapes of every closed pad row are already on the board, locked (`fanout.py`): the
    # router starts from a board whose fanout lanes are spent on stubs and vias, so nothing runs
    # a track along them (the DS2 Addon's analog nets once did, and walled AVDD, DVDD and the
    # UART pins in).

    # 1. Short hops first, on the empty board: a header pin to the resistor beside it, a cap to
    # its pin. A hop between two neighbouring pads has one path; anything routed before it can
    # cut that path (the DS2 Addon's locked analog copper ran between a header pin and its
    # resistor, and the 2 mm hop became a 65 mm detour with a via in the resistor's pad), and
    # anything routed after it goes around a hop for free.
    # Power nets at the width their amps ask for, on every step that may route one.
    power: list[str] = []
    widths: list[str] = []
    for cn in job.nets:
        if cn.kind == "power":
            cls = _class(job, cn.class_name)
            for n in _net_names(design, cn.patterns):
                if n not in power:
                    power.append(n)
                    widths.append(_fmt(cls.track_width_mm if cls else 0.4))
    at_width = ["--power-nets", *power, "--power-nets-widths", *widths] if power else []
    local = [n for n in _local_nets(design, placed) if n not in constrained and n not in done]
    pairs = {n for cn in job.nets if cn.autoroute == "diff_pair" for n in _net_names(design, cn.patterns)}
    local = [n for n in local if n not in pairs and n not in {p for p, _ in job.planes}]
    if local:
        # Not "local_nets": only a constrained step's copper (`*_nets`) is locked afterwards. A hop
        # on a power net (node's 1 A LOAD, a JST pin to the MOSFET beside it) keeps its width.
        step("local_hops", "route.py", ["--nets", *local, "--layers", *layers, "--track-width", _fmt(stack.track_min), "--max-ripup", "2", *at_width])

    # 2. Constrained nets on their layers while the board is otherwise empty. Later steps never
    # touch them: a net that could not finish on its layer stays open and is reported, rather
    # than finished with the vias its NetReq forbids.
    for (lays, cls_name), names in sorted(groups.items()):
        cls = _class(job, cls_name)
        left = sorted(set(names) - done)
        if not left:
            continue  # the patterns finished every net of this group; KRT has nothing to route here
        step(
            f"{cls_name.lower()}_nets",
            "route.py",
            ["--nets", *left, "--layers", *lays, "--track-width", _fmt(cls.track_width_mm if cls else floor), "--via-cost", "100000", "--max-ripup", "5"],
        )

    # 3. Differential pairs.
    for cn in job.nets:
        if cn.autoroute != "diff_pair":
            continue
        cls = _class(job, cn.class_name)
        names = _net_names(design, cn.patterns)
        if len(names) != 2:
            continue
        width = (cls.diff_pair_width_mm or cls.track_width_mm) if cls else floor
        gap = max((cls.diff_pair_gap_mm or floor) if cls else floor, (cls.clearance_mm if cls else floor))
        # No `--clearance`: it is a ceiling on every class, and the pair's 0.16 capped the Power
        # class at 0.16 while USB_DN passed a VBUS pad (c3_usb: KiCad measured 0.195 against 0.2).
        args = ["--nets", *names, "--track-width", _fmt(width), "--diff-pair-gap", _fmt(gap), "--layers", *layers, "--diff-pair-intra-match"]
        if job.layers >= 4:
            args += ["--impedance", "90"]
        step(f"pair_{names[0].lower()}", "route_diff.py", args)

    # 3. Planes first on four layers: the pours, then every pad on a plane net welded to its plane.
    plane_nets = [n for n, _ in job.planes] if job.planes and job.layers > 2 else []
    if plane_nets:
        step("planes", "route_planes.py", ["--nets", *plane_nets, "--plane-layers", *[layer for _, layer in job.planes], "--clearance", _fmt(default_clear)])

    # 4. pcbc's post stage: every SMD pad on a plane net welded to its plane by a tap of its own
    # (B.3), between `planes` and `signals` on both stackups. The order is measured, not tidy: of
    # the four orderings the design tried on node, taps before KRT boxed the USB pair in and taps
    # after the signals left 21 pads unconnected, because the signals had taken every tap site
    # (C.1). On two layers there is no `planes` step and the pour comes last, so the taps go down
    # before the pour exists and `route_verify.pour_raster` is what makes that safe.
    if post:
        pcbc_step("patterns_post")

    if plane_nets:
        # What the tap pattern refused, and nothing else. KRT's pour places no tap vias; this welds
        # the pads pcbc could not, alone, so the same-net keepout the signals step needs never sees
        # these nets. `route_job` skips the step outright when there are no refusals (C.4).
        step("plane_taps", "route.py", ["--nets", *plane_nets, "--layers", *layers, "--track-width", _fmt(stack.track_min), "--max-ripup", "5", "--no-bga-zones", *at_width])

    # 5. Everything else. Power nets at their width.
    signals = ["--nets", "*", *[f"!{n}" for n in constrained], *[f"!{n}" for n in plane_nets], *[f"!{n}" for n in sorted(done) if n not in constrained and n not in plane_nets], "--layers", *layers, "--track-width", _fmt(stack.track_min), "--max-ripup", "5", "--no-bga-zones", *at_width]
    step("signals", "route.py", signals)

    # 6. Two layers: pour GND on the back and tie what the pour could not reach.
    if job.layers <= 2 and "GND" in power:
        step("gnd_pour", "route_planes.py", ["--nets", "GND", "--plane-layers", "B.Cu", "--clearance", _fmt(default_clear)])
        step("finalize", "route.py", signals)
    return steps


LOCAL_MM = 5.0


def _local_nets(design: Design, placed: Path) -> list[str]:
    """Nets whose pads all lie within LOCAL_MM of each other on the placed board, sorted."""
    import math

    from .layout import footprints_by_ref
    from .pcb_place import _pins_of, parse_foot
    from .sexp import footprint_at

    if not Path(placed).exists():
        return []
    text = Path(placed).read_text()
    nets_of = _pins_of(design)
    sites: dict[str, list[tuple[float, float]]] = {}
    for ref, block in footprints_by_ref(text).items():
        at = footprint_at(block)
        if at is None:
            continue
        foot = parse_foot(ref, block)
        foot.at, foot.rot = (at[0], at[1]), at[2]
        for pad in foot.pads:
            net = nets_of.get(ref, {}).get(pad.num)
            if net:
                sites.setdefault(net, []).append(foot.pad_world(pad))
    out = []
    for net, pts in sites.items():
        if len(pts) < 2:
            continue
        span = max(math.hypot(a[0] - b[0], a[1] - b[1]) for i, a in enumerate(pts) for b in pts[i + 1 :])
        if span <= LOCAL_MM:
            out.append(net)
    return sorted(out)


def write_fab_overrides(job: CompiledJob, work: Path) -> Path:
    """The pinned fab rung every KRT step reads (see krt_plan)."""
    from .stackup import get_stackup

    s = get_stackup(job.stackup)
    path = work / "fab_overrides.txt"
    # C.5: the floor is the SMALLEST class clearance, not the stackup's absolute minimum. With pattern
    # copper on the board KRT chooses routes it would not have chosen, and it was measured producing a
    # violation of its own on ds2 (`clearance (netclass 'Power' 0.2000 mm; actual 0.1284 mm)`, a GPIO1
    # track against a 3V3 via, both KRT's). By construction this is at most every class's own number —
    # 0.155 on c3_usb, 0.16 on buck and ds2, 0.18 on node — so no class is ever escalated, which is
    # what `max(class clearance)` would have done to node's USB pair and its 0.15 mm gap.
    floor = max(min([c.clearance_mm for c in job.classes], default=s.clearance_min), s.clearance_min)
    path.write_text(
        "# pcbc: the fab's floor for this stackup; KRT must not escalate past it\n"
        f"via_diameter = {s.via_diameter:g}\nvia_drill = {s.via_drill:g}\nhole_to_hole = {s.hole_to_hole:g}\n"
        f"clearance = {floor:g}\ntrack_width = {s.track_min:g}\nboard_edge = {s.edge_clearance:g}\n"
    )
    return path


def route_job(design: Design, placed: Path, *, out: Path, name: str = "board") -> dict:
    placed = Path(placed)
    out = Path(out)
    copy_with_siblings(placed, out)
    # A killed build leaves step files behind; KRT reads a step's sibling .kicad_pro, so a stale
    # one from another run steers the next step. Every route starts from a clean work dir.
    for stale in sorted(out.parent.glob("[0-9][0-9]_*.kicad_*")):
        stale.unlink()
    home = krt_home()
    reason = krt_missing(home)
    if reason:
        return {"pcb": str(out), "router": None, "error": reason}
    job = compile_design(design)
    work = out.parent
    write_fab_overrides(job, work)
    # C.1's pre stage: pcbc writes the structured copper itself, locked, before KRT sees the board.
    # Hops run FIRST, before the fanout, and the fanout then skips the nets they claimed: a hop
    # between two neighbouring pads has one path and anything routed before it can cut that path, so
    # a closed row's lane is better spent on the hop that needed it than on a via the hop then has to
    # start from (`docs/copper-plan.md` line 181, and this module's own comment below).
    from .fanout import fanout_pieces
    from .patterns import empty_plan, hard_refusals, pattern_copper, patterns_off
    from .route_emit import census as _census, sidecar, write_pieces, write_sidecar

    text = placed.read_text()
    plan = empty_plan(text) if patterns_off() else pattern_copper(design, job, job.constraints, text, name, stage="pre")
    fan_pieces, fan = fanout_pieces(design, job, plan.text, name, plan.scene, claimed=plan.claimed)
    if fan_pieces and plan.scene is not None:
        # The escapes join the scene the patterns were judged against, so the post stage (S5) and the
        # self-check see them. With `PCBC_PATTERNS=off` there is no scene, and `fanout_pieces` builds
        # its own exactly as it did before R2 — which is what makes the switch a true rollback.
        plan.scene.add(plan.scene.item_of(pc) for pc in fan_pieces)
    pre = tuple(plan.pieces) + tuple(fan_pieces)
    start = placed
    if pre:
        start = work / "00_patterns_pre.kicad_pcb"
        copy_with_siblings(placed, start)
        start.write_text(write_pieces(plan.text, fan_pieces))
    steps = krt_plan(job, design, start, work, home, plan, post=not patterns_off())
    result: dict = {
        "pcb": str(out),
        "router": "krt",
        "krt": {"home": str(home), "version": krt_version(home)},
        "plan": [{"step": n, "cmd": cmd} for n, cmd in steps],
        "fanout": fan,
        "patterns": _census(pre),
        "pattern_moves": list(plan.moves),
        "notes": list(plan.notes),
        "refusals": [r.to_dict() for r in plan.refusals()],
        "refused": plan.counts(),
        "pattern_ms": plan.wall_ms,
        "steps": [],
        "error": None,
    }
    fatal = hard_refusals(plan)
    if fatal:
        # C.6: a soft refusal is a printed move and a fall-through to KRT, and the build passes; a
        # hard one is intent KRT structurally cannot honour. `--strict-patterns` makes every refusal
        # hard, and R3 flips that default.
        result["error"] = "pattern refused:\n" + "\n".join(r.move for r in fatal)
        return result
    last: Path | None = None
    failed: dict[str, str] = {}
    unreached: dict[str, list[str]] = {}
    nets_run: list[str] | None = None  # a step whose net list the run narrowed, for the report
    # Everything pcbc owns on this board, in emission order: the pre stage plus the post stage once
    # it has run. It is what `_lost` checks for survival after every KRT step, what keeps its own
    # uuids through `pin_copper_ids`, and what the copper bar reads a reason off.
    owned: list = list(pre)
    post_plan = None
    current = start
    for step_name, cmd in steps:
        # The chain is the boards that were actually written, not the ones the plan predicted: a
        # skipped `plane_taps` leaves a gap, and the next step has to read the board before it.
        cmd = [*cmd[:4], str(current), *cmd[5:]]
        produced = Path(cmd[5])
        if cmd[0] == PCBC_STEP:
            # The siblings come from the **placed** board, not from the step before it: KRT lowers
            # the output project's `min_hole_clearance` to the floor it actually routed to (its
            # `INRUN_FLOOR_SYNC`, 0.25 -> 0.0889 on the first step) and every later step then reads
            # the lowered one, so by the signals step the router is routing to rules `board.py`
            # never declared. The gate judges the board at the declared rules, so pcbc's own step
            # hands the next step the project pcbc compiled. Measured: without it KRT routed c3_usb's
            # VBUS 0.2365 mm from `J1`'s NPTH against the 0.25 the board declares, four times.
            copy_with_siblings(placed, produced)
            post_plan = pattern_copper(design, job, job.constraints, current.read_text(), name, stage="post")
            produced.write_text(post_plan.text)
            owned += list(post_plan.pieces)
            result["patterns"] = _census(owned)
            result["pattern_moves"] += list(post_plan.moves)
            result["notes"] += list(post_plan.notes)
            result["refusals"] += [r.to_dict() for r in post_plan.refusals()]
            merged = dict(result["refused"])
            for pattern, n in post_plan.counts().items():
                merged[pattern] = merged.get(pattern, 0) + n
            result["refused"] = {k: merged[k] for k in sorted(merged)}
            result["pattern_ms"] += post_plan.wall_ms
            result["steps"].append(
                {"step": step_name, "returncode": 0, "log": "", "summary": {"pieces": len(post_plan.pieces), "refused": post_plan.counts(), "ms": post_plan.wall_ms}}
            )
            fatal = hard_refusals(post_plan)
            if fatal:
                result["error"] = "pattern refused:\n" + "\n".join(r.move for r in fatal)
                return result
            current = produced
            last = produced
            continue
        if step_name == "plane_taps" and post_plan is not None:
            # C.4: KRT's tap step runs for the plane nets whose pads pcbc could not tap, and not at
            # all when there are none. A pad the tap pattern skipped is not a refusal: a through-hole
            # pad's barrel already reaches the plane and the zone connects it (B.3).
            #
            # `post_plan is not None` is the whole of `PCBC_PATTERNS=off`'s safety here: with the
            # patterns off there are no taps, so every plane pad still needs KRT's step, and skipping
            # it left node with 72 unconnected items — the rollback has to be a rollback (C.6).
            left = sorted({r.net for r in post_plan.refusals() if r.pattern == "tap"})
            if not left:
                result["steps"].append({"step": step_name, "returncode": 0, "log": "skipped: pcbc tapped every plane pad it owns", "summary": {"skipped": True}})
                continue
            cmd = _with_nets(cmd, left)
            nets_run = left
        proc = subprocess.run(cmd, capture_output=True, text=True, cwd=str(home))
        full = (proc.stdout or "") + (proc.stderr or "")
        log = full[-3000:]
        summary = _krt_summary(full)
        entry = {"step": step_name, "returncode": proc.returncode, "log": log[-1200:], "summary": summary}
        if nets_run is not None:
            entry["nets"] = nets_run
            nets_run = None
        result["steps"].append(entry)
        if proc.returncode != 0 or not produced.exists():
            result["error"] = f"KRT {step_name} failed ({proc.returncode}): {log.strip()[-600:]}"
            return result
        for net in summary.get("failed_single", []):
            failed.setdefault(net, step_name)
        for net in (summary.get("pad_pairs_open") or {}).get("nets", []):
            failed.setdefault(net, step_name)  # a multipoint net KRT left with an open pad pair
        for ref, net, x, y in _unreached_pads(full):
            # The pour's own list. KRT's bare pour defers every tap to the route step after it, so
            # this names pads and does not fail the net: the route steps' fields above do that.
            unreached.setdefault(net, []).append(f"{ref} at ({x}, {y})")
        if step_name.endswith("_nets"):
            # A constrained step's copper is locked so the signal steps cannot rip it up
            # and finish the net with vias its NetReq forbids.
            i = cmd.index("--nets")
            named = set()
            for a in cmd[i + 1 :]:
                if a.startswith("--"):
                    break
                named.add(a)
            produced.write_text(lock_copper(produced.read_text(), named))
        missing = _lost(produced.read_text(), owned)
        if missing:
            # C.3's guard on the promise that `(locked yes)` holds. R2 writes ten times more locked
            # copper than the fanout did and puts it in KRT's way, so the promise is checked rather
            # than trusted: a step that moved or dropped a piece names itself here.
            result["error"] = f"KRT {step_name} moved or dropped {len(missing)} piece(s) of pcbc's own locked copper: {'; '.join(missing[:3])}"
            return result
        current = produced
        last = produced
    if last is None:
        result["error"] = "nothing to route"
        return result
    text = pin_copper_ids(last.read_text(), name, frozenset(p.uuid for p in owned if p.uuid))
    out.write_text(text)
    from .copper_bar import copper_bar

    reasons = {bar_key(p): p.reason for p in owned}
    owners = {bar_key(p): p.owner for p in owned}
    result["copper_bar"] = copper_bar(text, reasons, owners)
    result["leftover"] = result["copper_bar"]["totals"]["by_reason"].get("leftover", {})
    doc = sidecar(pre, step="patterns_pre", refusals=result["refusals"], notes=result["notes"], leftover=result["leftover"])
    if post_plan is not None and post_plan.pieces:
        # One sidecar, two stages: a piece carries the step that wrote it, so `copper.json` says
        # which of pcbc's stages a via came from and the census is the whole board's (D.4).
        doc.items += sidecar(post_plan.pieces, step="patterns_post").items
        doc.census = _census(owned, leftover=result["leftover"])
    write_sidecar(out.parent / "copper.json", doc)
    opens = unrouted_nets(text)
    result["segments"] = len(re.findall(r"\n\t\(segment\b", text))
    result["vias"] = len(re.findall(r"\n\t\(via\b", text))
    result["zones"] = len(re.findall(r"\n\t\(zone\b", text))
    # KRT says a net "failed" when it could not reach every pad even though the net has copper;
    # pcbc's own open-net check sees copper and would say nothing. Both are the same failure.
    for net in failed:
        if net not in opens:
            opens.append(net)
    result["unrouted"] = sorted(opens)
    if opens:
        from .blocking import blocking_lines

        blocking: dict[str, list[str]] = {n: blocking_lines(design, text, work, n, unreached.get(n)) for n in sorted(opens)}
        result["blocking"] = blocking
        moves = [_unrouted_move(job, design, n, unreached.get(n)) for n in sorted(opens)]
        result["error"] = "unrouted: " + "; ".join(moves) + "".join(f"\n  {line}" for n in sorted(opens) for line in blocking[n])
    return result


def _with_nets(cmd: list[str], nets: list[str]) -> list[str]:
    """The same command with its `--nets` list replaced. Used once: KRT's `plane_taps` step runs for
    the plane nets pcbc's own tap pattern refused a pad of, and for no others (C.4)."""
    i = cmd.index("--nets")
    j = i + 1
    while j < len(cmd) and not cmd[j].startswith("--"):
        j += 1
    return [*cmd[: i + 1], *nets, *cmd[j:]]


def bar_key(p) -> tuple:
    """A piece as the routed board will name it: the geometry, with the two ends in a fixed order.

    `copper_bar` reads a board file and cannot know which end KiCad wrote first, and the board is
    rewritten twice (KRT's steps, then the gate's refill-and-save), so the key is order-free. It is
    the one place a reason survives those rewrites: the reason cannot live in the file, because
    `pin_copper_ids` re-keys uuids and KiCad invents its own (D.4).
    """
    if p.kind == "seg":
        a, b = (round(p.a[0], 4), round(p.a[1], 4)), (round(p.b[0], 4), round(p.b[1], 4))
        return ("seg", p.layer, min(a, b), max(a, b), round(p.w, 4))
    return ("via", (round(p.a[0], 4), round(p.a[1], 4)))


def _lost(text: str, pieces) -> list[str]:
    """Which of pcbc's own pieces are no longer on this board, by geometry."""
    from .copper_bar import segments as _segs, vias as _vias

    have = {("seg", s["layer"], min(_r(s["start"]), _r(s["end"])), max(_r(s["start"]), _r(s["end"])), round(s["width"], 4)) for s in _segs(text)}
    have |= {("via", _r(v["at"])) for v in _vias(text)}
    return [f"{p.reason} {p.net} {bar_key(p)[1:]}" for p in pieces if bar_key(p) not in have]


def _r(pt) -> tuple:
    return (round(pt[0], 4), round(pt[1], 4))


_UNREACHED = re.compile(r"unconnected pad (\S+) on '([^']+)' at \(([-0-9.]+), ([-0-9.]+)\)")


def _unreached_pads(log: str) -> list[tuple[str, str, str, str]]:
    """route_planes.py names each pad its pour could not tap: (ref, net, x, y)."""
    return [(m.group(1), m.group(2), m.group(3), m.group(4)) for m in _UNREACHED.finditer(log)]


_SUMMARY = re.compile(r"JSON_SUMMARY_MIN:\s*(\{.*\})")


def _krt_summary(log: str) -> dict:
    """The last JSON_SUMMARY_MIN line a KRT step printed: failed nets, deficits, vias."""
    import json

    hits = _SUMMARY.findall(log)
    if not hits:
        return {}
    try:
        d = json.loads(hits[-1])
    except ValueError:
        return {}
    return {k: d.get(k) for k in ("failed", "failed_single", "multipoint_deficit", "open_single", "pad_pairs_open", "routed", "vias") if k in d}


def _unrouted_move(job: CompiledJob, design: Design, net: str, unreached: list[str] | None = None) -> str:
    """An unrouted net as a move: who is on it, what constrained it, and which pad KRT named."""
    from fnmatch import fnmatch

    from .netcheck import expected_nets

    pads = sorted(expected_nets(design).get(net, set()))
    refs = ", ".join(f"{r}.{p}" for r, p in pads[:6]) + (", ..." if len(pads) > 6 else "")
    rule = next((cn for cn in job.nets if any(fnmatch(net, p) for p in cn.patterns)), None)
    if rule and (rule.vias is False or len(rule.layers) == 1):
        how = f"on {', '.join(rule.layers[:1] if rule.vias is False else rule.layers)} without vias (NetReq kind={rule.kind!r})"
        fix = f"line its parts up on that side of the board, or allow vias with NetReq({net!r}, kind={rule.kind!r}, vias=True)"
    else:
        how = "on any layer"
        fix = "move its parts closer together or out from between others"
    where = f"; the pour could not reach {', '.join(unreached[:6])}{', ...' if len(unreached) > 6 else ''}" if unreached else ""
    return f"{net} ({refs}) found no path {how}{where}: {fix}"
