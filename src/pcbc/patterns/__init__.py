"""What every pattern is, and the stage that runs them (`docs/r2-design.md` B.0, C.1, C.4).

A pattern is a thin deterministic enumeration over the geometry core: it derives a **fixed, finite,
ordered** candidate list from the pads' own geometry and the class numbers, takes the first that
clears, and emits nothing at all when nothing does. There is no cost function, no backtracking, no
rip-up and no state carried between candidates, which is why the same placed board gives
byte-identical copper and why a refusal is a sentence the AI can act on rather than a retry.

`pattern_copper` is the stage. It walks the patterns of C.1 in C.1's order, adds each pattern's
copper to the scene before the next one runs — so a later pattern sees it as an obstacle and the
whole stage is one left-to-right pass with no revisiting — and hands back a `PatternPlan`: the board
text with the copper in it, the census, the moves, and which nets still need KRT.

Nothing here decides geometry. `route_geom` owns the shapes and the paths, `route_scene` owns the
five rules and the exits, `route_emit` owns the text; this module owns the *order*.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Sequence

from ..compile import CompiledJob
from ..constraints import ConstraintSet
from ..model import Design
from ..route_emit import Piece, census as _census, seg_piece, write_pieces
from ..route_geom import Pt, is_octilinear, legs_ok, octant, qp, turn_ok
from ..route_scene import Clash, Exit, Item, Scene, build_scene, net_open, pad_exits
from ..sexp import stable_uuid

__all__ = [
    "ALL_SHAPES",
    "MAX_LINKS",
    "PRE",
    "MID",
    "POST",
    "FINAL",
    "merge_plans",
    "PatternCtx",
    "PatternPlan",
    "PatternResult",
    "Piece",
    "Refusal",
    "Terminal",
    "Exit",
    "legs_ok",
    "link_candidates",
    "MITRE_MM",
    "mitre",
    "lane_ok",
    "legs_of",
    "pad_exits",
    "pattern_copper",
    "pieces_of",
    "shape_ok",
    "terminals",
    "toward_first",
]


# --- B.0: the four records ------------------------------------------------------------------------


@dataclass(frozen=True)
class Refusal:
    """Why a pattern emitted nothing, and what the AI can do about it.

    `hard` is fatal even in R2 (C.6): it aborts the route stage before KRT is run at all
    (`route.py`). It exists for the case where honouring the refusal would break a constraint KRT
    structurally **cannot** hold whatever it does with the net — a net whose `NetReq` sets
    `vias=False`, or a net that lives on one layer — because there the fall-through does not produce
    a worse route, it produces copper the author forbade.

    **Nothing in this codebase sets it today, and that is deliberate rather than an oversight.**
    Every producer passes `hard=False`: `hop`, `spine`, `tap`, and — since S6's review — `chain`.
    The field and `hard_refusals` are kept because the two cases above are real and unimplemented,
    and because `--strict-patterns` (`PCBC_STRICT_PATTERNS=1`) reads the same path to make every
    refusal fatal on demand. A dead field with a live reason is better than a rediscovered one.

    **A declared `Chain()` was the one thing that set it, and the justification did not survive being
    measured.** The argument was that falling through to KRT "would produce copper that violates the
    intent rather than merely a worse route, because KRT would branch". On the only board in the repo
    with a declared chain that is not a pair — the DS2 Addon's `Chain("VDDA", "J2.1", "C4.1",
    "U1.12")` — KRT honours the order: it runs one straight 45-degree trace **across** `C4.1`'s pad,
    with 1.1185 mm of that trace's centre line inside the pad's own copper and its junction 0.4300 mm
    inside it (`docs/r2-measurements.md`, S6). The refusal was aborting the build on a prediction
    about KRT that the board contradicts. Worse, the invariant it was protecting was checked
    **nowhere**: R-X4's `**V**` half did not exist. It exists now — `route_verify.chain_order`, run
    on the routed board by `build._chain_gate` — so a declared order that really is violated fails
    the build on a measurement instead of on an assumption, and the refusal that could not know is
    soft.

    Everything else is soft: the move is printed, the net falls through to KRT, the build passes,
    and the **count** is pinned per board so a new refusal is a test failure rather than a drift.
    """

    pattern: str
    net: str
    what: str  # "U1.52" | "CC2 J1.B5->R_CC2.1"
    clash: Clash | None
    rule: str  # a rule name from A.4, or "no candidate"
    hard: bool
    move: str  # one line (or a few), ending in a board.py edit

    def to_dict(self) -> dict:
        return {
            "pattern": self.pattern,
            "net": self.net,
            "what": self.what,
            "rule": self.rule,
            "blocker": self.clash.item.label() if self.clash else "",
            "hard": self.hard,
            "move": self.move,
        }


@dataclass(frozen=True)
class PatternResult:
    """One pattern's answer for one spec: the copper, what it joined, and why not."""

    reason: str
    net: str
    pieces: tuple[Piece, ...] = ()  # () when it did not fit
    joins: tuple[tuple[str, str], ...] = ()  # the pad ids this connected: ("U1.5", "R_EN.2")
    candidate: str = ""  # "Z-near exit(right,left)" — what fitted, for the report
    refusal: Refusal | None = None
    notes: tuple[str, ...] = ()  # "style:" lines (the advisory mask rule, loop areas)
    tried: int = 0  # how many candidates were judged, for the report
    links: tuple[int, int] = (0, 0)
    """`(made, needed)`: how many of the pad pairs this spec had to join the emitted copper actually
    joins, counted **off the copper** rather than off the calls that succeeded.

    The two differ in both directions and the S7 report got both wrong on one board (findings 5, 8,
    15, 17): c3_usb's `VBUS` was published as "1 of 6" and writes no copper at all, and its `3V3` as
    "4 of 7" when the copper joins five pairs, because a link that passes over a third station of its
    own net connects it for free. `made` is `len(stations) - components` over the stations and the
    pieces, which is what the board does; `needed` is `len(stations) - 1`."""
    coverage: float = 0.0
    """`sum(piece.mm) / airwire`: how much of the net's shortest possible copper this pattern wrote.

    The number that separates a spine that replaced a route from one that only got in its way, and
    neither the piece count nor `links` says it: buck's `VIN` makes 2 of its 4 links (0.50, the same
    ratio as node's `VBUS`) and covers **0.159** of its airwire, against 0.639 on buck's `5V`, 0.683
    on c3_usb's `3V3` and 0.477 on node's `VBUS` (finding 9). Reported per net, not gated — see
    `docs/r2-measurements.md` S7 for why a coverage floor was measured and rejected."""


@dataclass(frozen=True)
class PatternCtx:
    """Everything a pattern may read. The scene is mutated only by `Scene.add`, between patterns,
    never mid-candidate: a candidate is judged against a board that does not move under it."""

    scene: Scene
    design: Design
    job: CompiledJob
    cs: ConstraintSet
    board: str  # for stable_uuid
    stage: str
    """Which of `_STAGES`' keys is running: `"pre"` | `"mid"` | `"post"` | `"final"`.

    The four are not interchangeable and a pattern may read this to say so. `pre` and `mid` see a
    board with no KRT copper on it at all; `post` sees the planes but not the signals; `final` sees
    every piece of copper the board will ever have, because nothing routes after it (`FINAL`). On two
    layers that difference is the whole of D.5: at `post` the back pour does not exist and
    `route_verify.pour_raster` has to predict it, while at `final` `gnd_pour` has already written it.
    """

    owned: tuple[Piece, ...] = ()
    """Every piece pcbc has written on this board **so far in this route**, in emission order.

    Empty for the `pre` stage, which is the first thing that runs; the pre and mid stages' copper at
    `post`; all of it at `final`. It is not the scene — the scene holds KRT's copper too and cannot
    tell the two apart — and that distinction is the whole of `docs/r2-design.md` B.6's precondition:
    **a guard may only be offset from a path pcbc itself routed**, because pcbc cannot offset a path
    it does not own. Read off the run rather than inferred from the file, because the only provenance
    signal a board file carries is `(locked yes)` and KRT locks its own constrained-net copper too
    (`route.lock_copper`), so a board's locked tracks are not pcbc's tracks.

    `route_job` has held exactly this list since S2 — it is what `_lost` checks for survival after
    every KRT step and what the copper bar reads a reason off — so this is that list reaching the one
    pattern whose candidate list is derived from it. Everything else here is a question about the
    board; this is the one question about the *route*.
    """


@dataclass(frozen=True)
class PatternPlan:
    """What the stage hands to KRT (C.4)."""

    text: str  # the board with the pattern copper in it
    pieces: tuple[Piece, ...] = ()
    ids: tuple[int, ...] = ()  # scene item id of each piece, in the same order
    census: dict = field(default_factory=dict)
    moves: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()
    done: frozenset[str] = frozenset()  # nets fully connected by patterns (`net_open` is False)
    partial: frozenset[str] = frozenset()  # nets with pattern copper and still open
    refused: dict = field(default_factory=dict)  # net -> its refusals
    claimed: frozenset[str] = frozenset()  # every net a pattern wrote copper for
    links: dict = field(default_factory=dict)
    """`{reason: {net: [made, needed, coverage]}}` — what each pattern joined, off its own copper.

    Generated here so the ledger in `docs/r2-measurements.md` and the assertion in
    `test_examples_fab.py` read the same place the build does. The S7 table was hand-copied and said
    c3_usb spined two nets with `VBUS` at "1 of 6"; the build writes no `VBUS` copper at all
    (findings 5, 8, 15, 17)."""
    wall_ms: int = 0
    scene: Scene | None = None

    def refusals(self) -> tuple[Refusal, ...]:
        return tuple(r for net in sorted(self.refused) for r in self.refused[net])

    def counts(self) -> dict:
        """{pattern: refusals}, exact per board (C.6) — a new refusal is a test failure."""
        out: dict[str, int] = {}
        for r in self.refusals():
            out[r.pattern] = out.get(r.pattern, 0) + 1
        return out


# --- B.0: the pads a pattern starts from ----------------------------------------------------------


@dataclass(frozen=True)
class Terminal:
    """One pad of a net, as a pattern sees it.

    A custom pad draws several primitives and the scene holds each as its own `Item` (tighter than
    hulling them together), so a terminal is the **owner**, not the item: its centre is the centre of
    everything that pad draws, and `item` is the primitive a stub leaves from — the widest one, which
    is the one whose half extent sets how far out the exit has to be.
    """

    owner: str
    net: str
    at: Pt
    layers: frozenset[str]
    item: Item

    @property
    def ref(self) -> str:
        return self.owner.split(".")[0]

    @property
    def num(self) -> str:
        return self.owner.split(".", 1)[1] if "." in self.owner else ""


def _pad_key(owner: str) -> tuple:
    """`sorted(ref, pad_num)` of B.0, with a numeric pad number sorting as a number: KiCad's pad 10
    comes after pad 2, and a string sort puts it after pad 1."""
    ref, _, num = owner.partition(".")
    return (ref, (0, int(num), "") if num.isdigit() else (1, 0, num))


def terminals(scene: Scene, net: str) -> tuple[Terminal, ...]:
    """The net's pads, one per pad, in `sorted(ref, pad_num)` order."""
    by_owner: dict[str, list[Item]] = {}
    for it in scene.items:
        if it.kind == "pad" and it.net == net:
            by_owner.setdefault(it.owner, []).append(it)
    out: list[Terminal] = []
    for owner in sorted(by_owner, key=_pad_key):
        parts = by_owner[owner]
        x0 = min(p.box()[0] for p in parts)
        y0 = min(p.box()[1] for p in parts)
        x1 = max(p.box()[2] for p in parts)
        y1 = max(p.box()[3] for p in parts)
        widest = max(parts, key=lambda p: ((p.box()[2] - p.box()[0]) * (p.box()[3] - p.box()[1]), -p.id))
        layers: frozenset[str] = frozenset()
        for p in parts:
            layers = layers | p.layers
        out.append(Terminal(owner, net, qp(((x0 + x1) / 2.0, (y0 + y1) / 2.0)), layers, widest))
    return tuple(out)


# --- B.0: the shared link builder -----------------------------------------------------------------

ALL_SHAPES = frozenset({"direct", "Z-far", "Z-near", "L-h", "L-v", "S-h", "S-v"})
"""Every link shape B.0 names. A pattern that wants all of them says so with this, and a pattern that
wants only the straight line says so with a smaller set; the bound on a candidate list is declared in
the pattern's spec and nowhere else."""

MAX_LINKS = 7
"""The seven link shapes of B.0. Two of them — `L-h` and `L-v` — are a 90 degree corner, which
`turn_ok` refuses (a right angle is an octant difference of 2, and the rule is 1), so they survive
only where they degenerate to a straight line and are then deduped against `direct`. They stay in the
enumeration because B.0 names them and because the *filter* is what removes them: a later slice that
loosens `turn_ok` gets them back without touching this list. `test_patterns.py` pins that."""


def _nm(v: float) -> int:
    return int(round(v * 1e6))


def _mm(n: int) -> float:
    return n / 1e6


def link_candidates(ea: Exit, eb: Exit) -> tuple[tuple[str, tuple[Pt, ...]], ...]:
    """The link from one exit to another, in B.0's fixed order, as (name, points).

    Every corner is **computed, never snapped**: the points are built in integer nanometres from the
    already-quantised exits, so a diagonal leg is exactly diagonal and an axis leg is exactly axial.
    Snapping a corner to the router's grid while keeping both endpoints is the one construction that
    produces neither an axis nor a 45 — on node's `J1 -> U1` it leaves a far leg of dx 1.65, dy -0.04
    — and it is the bug that tilted every fanout stub before S1 (A.5).

    Dropped: anything that is not octilinear, anything that fails `turn_ok`, and anything that
    repeats an earlier candidate's points. Order is the answer; there is no scoring.
    """
    ax, ay = _nm(ea.at[0]), _nm(ea.at[1])
    bx, by = _nm(eb.at[0]), _nm(eb.at[1])
    dx, dy = bx - ax, by - ay
    sx = 1 if dx > 0 else -1
    sy = 1 if dy > 0 else -1
    adx, ady = abs(dx), abs(dy)
    run = min(adx, ady)
    raw: list[tuple[str, tuple[tuple[int, int], ...]]] = []
    # direct: one straight leg, only when it is already 0/45/90.
    raw.append(("direct", ((ax, ay), (bx, by))))
    # Z-far: the 45 on the short axis, taken at the far end; Z-near: the same 45 at the near end.
    raw.append(("Z-far", ((ax, ay), (bx - sx * run, by - sy * run), (bx, by))))
    raw.append(("Z-near", ((ax, ay), (ax + sx * run, ay + sy * run), (bx, by))))
    # L: axis, then axis. A right angle, so `turn_ok` refuses it unless it degenerates.
    raw.append(("L-h", ((ax, ay), (bx, ay), (bx, by))))
    raw.append(("L-v", ((ax, ay), (ax, by), (bx, by))))
    # S: axis, the 45 taken at the midpoint, axis. The diagonal spans the whole cross-axis
    # difference, so its two corners are derived from one another and the 45 is exact.
    cx1 = (ax + bx - sx * ady) // 2
    raw.append(("S-h", ((ax, ay), (cx1, ay), (cx1 + sx * ady, by), (bx, by))))
    cy1 = (ay + by - sy * adx) // 2
    raw.append(("S-v", ((ax, ay), (ax, cy1), (bx, cy1 + sy * adx), (bx, by))))
    out: list[tuple[str, tuple[Pt, ...]]] = []
    seen: set[tuple[Pt, ...]] = set()
    for name, ipts in raw:
        pts = _straighten(_dedupe(tuple((_mm(x), _mm(y)) for x, y in ipts)))
        if len(pts) < 2 or pts in seen:
            continue
        if not is_octilinear(pts) or not turn_ok(pts):
            continue
        seen.add(pts)
        out.append((name, pts))
    return tuple(out)


def _dedupe(pts: tuple[Pt, ...]) -> tuple[Pt, ...]:
    """Consecutive duplicates removed. A repeated point is not a legal `Path` and a degenerate leg
    is not an angle — `route_geom` decided that once, for all three judges (S2 review, finding 2)."""
    out: list[Pt] = []
    for p in pts:
        if not out or out[-1] != p:
            out.append(p)
    return tuple(out)


def _straighten(pts: tuple[Pt, ...]) -> tuple[Pt, ...]:
    """A vertex that is not a turn is not a vertex. Without this, `S-h` between two exits at the same
    y comes back as the straight line with a redundant midpoint in it — the same copper as `direct`,
    which the dedupe then cannot see is the same."""
    if len(pts) < 3:
        return pts
    out = [pts[0]]
    for i in range(1, len(pts) - 1):
        if not _collinear(pts[i - 1], pts[i], pts[i + 1]):
            out.append(pts[i])
    out.append(pts[-1])
    return tuple(out)


def _collinear(a: Pt, b: Pt, c: Pt) -> bool:
    return octant(a, b) == octant(b, c)


# --- B.0: the order a link's exits are walked in ---------------------------------------------------


def toward_first(exits: Sequence[Exit], here: Terminal, there: Terminal) -> tuple[Exit, ...]:
    """A.7's exits, re-ordered so the ones pointing at the other pad come first.

    A.7 orders a pad's exits **outward** — away from its own footprint's centre — because that is what
    a fanout escape needs: a closed row can only go straight out. A link between two pads wants the
    opposite first, and taking A.7's order literally makes a hop leave its pad in the wrong direction
    and walk around. Measured: node's `CC1` came out at 1.27x the distance between its pads and its
    `LED_A` the same, and six of the DS2 Addon's ten hops were two or three segments where one would
    do; with the re-ordering every one of them is **1.00x**, a single straight segment.

    This is an ordering of the enumeration, not a score on the candidates — B.0's nesting is an order
    too — and it is derived from the pads' own geometry and nothing else, so the board stays a pure
    function of the placement. It does not rescue every case: c3_usb's `LED_A` has its two exits
    0.08 mm from crossing and still walks, which is what `DETOUR_MAX` is for.
    """
    dx, dy = there.at[0] - here.at[0], there.at[1] - here.at[1]
    order = {e.side: i for i, e in enumerate(exits)}
    return tuple(sorted(exits, key=lambda e: (-(e.dir[0] * dx + e.dir[1] * dy), order[e.side])))


def pieces_of(
    pts: Sequence[Pt],
    widths: Sequence[float],
    *,
    net: str,
    reason: str,
    layer: str,
    owner: str,
    board: str,
) -> tuple[Piece, ...]:
    """The path as segments, with consecutive legs of the same width and heading merged.

    Merging is what makes each of the DS2 Addon's ten hops **one** segment rather than three: where
    the pads face each other the stub out, the link and the stub in are one straight run of copper,
    and writing it as three touching segments would be three items in the file for one piece of
    geometry — and a different answer, from the copper bar and from `blocking`, to the same question.
    A width change is a new segment even when the heading does not turn, because the copper is
    genuinely a different track there (`fanout.py`'s neck at a pad, B.1).
    """
    legs: list[tuple[Pt, Pt, float]] = []
    for i in range(len(pts) - 1):
        a, b, w = pts[i], pts[i + 1], widths[i]
        if legs and legs[-1][2] == w and _collinear(legs[-1][0], legs[-1][1], b):
            legs[-1] = (legs[-1][0], b, w)
        else:
            legs.append((a, b, w))
    return tuple(
        seg_piece(
            net,
            reason,
            layer,
            a,
            b,
            w,
            owner=owner,
            uuid=stable_uuid(board, "pcbc", reason, net, layer, f"{a[0]:.6f}", f"{a[1]:.6f}", f"{b[0]:.6f}", f"{b[1]:.6f}", f"{w:g}"),
        )
        for a, b, w in legs
    )


MITRE_MM = 0.2
"""How far back from a right-angled corner the mitre cuts, along each leg.

A link leaves one pad's stub and enters the other's, and the stub is axis-aligned by A.7, so the join
is often a quarter turn: `turn_ok` refuses it, and so does pcbc's own written rule — `dru.py` writes
`(constraint track_angle (min 135))` as `pcbc_geometry_angles` on every board. A pattern that emits
90 degree corners at its pads is a pattern that raises the count of the warning pcbc wrote to catch
them, which F.3 item 7 forbids outright.

So the corner is **mitred**: cut this far back along each leg and joined by a 45, which is the
construction a hand router makes and which leaves every turn at 135 degrees or better. At 0.2 mm the
new leg is 0.2828 mm, comfortably over `MICRO_MM`, and a leg too short to be cut twice simply loses
that candidate to the next one. Measured: without it blinky's one net raises `pcbc_geometry_angles`
from 0 to 2; with it every board's angle count is what it was."""


def mitre(pts: Sequence[Pt], m: float = MITRE_MM) -> tuple[Pt, ...] | None:
    """Every right angle replaced by two 45s. None when a turn is sharper than a right angle.

    Exact by construction, like everything else here: the cut steps `m` **per component** along each
    leg's own direction in integer nanometres, so the chord between the two new points is an axis when
    the corner joined two diagonals and a 45 when it joined two axes — never an angle that has to be
    rounded into one. A corner already at 135 degrees or better is left exactly as it was.
    """
    ip = [(_nm(x), _nm(y)) for x, y in pts]
    step = _nm(m)
    out: list[tuple[int, int]] = [ip[0]]
    for i in range(1, len(ip) - 1):
        a, c, b = ip[i - 1], ip[i], ip[i + 1]
        d = abs(octant(_pt(a), _pt(c)) - octant(_pt(c), _pt(b)))
        d = min(d, 8 - d)
        if d <= 1:
            out.append(c)
            continue
        if d > 2:
            return None  # a hairpin: the copper doubles back, and no mitre makes that a route
        ux, uy = _sgn(c[0] - a[0]), _sgn(c[1] - a[1])
        vx, vy = _sgn(b[0] - c[0]), _sgn(b[1] - c[1])
        out.append((c[0] - ux * step, c[1] - uy * step))
        out.append((c[0] + vx * step, c[1] + vy * step))
    out.append(ip[-1])
    got = _dedupe(tuple(_pt(q) for q in out))
    return got if len(got) >= 2 else None


def _sgn(v: int) -> int:
    return (v > 0) - (v < 0)


def _pt(ip: tuple[int, int]) -> Pt:
    return (_mm(ip[0]), _mm(ip[1]))


def shape_ok(pts: Sequence[Pt]) -> bool:
    """Is this the shape of copper pcbc writes — on the whole run, the stubs included?

    A candidate is judged as the copper it will be, not as the link alone: the corner where a link
    meets the stub out of its pad is a corner like any other, and a link that leaves a pad sideways
    is a 90 degree turn `turn_ok` refuses. Judging the link on its own is how a pattern emits a
    right angle at a pad and then fails its own self-check (D.1 item 2).

    `legs_ok` is deliberately **not** here: it is asked of the merged path (`pieces_of`), because two
    collinear legs that each fall under `MICRO_MM` are one legal segment once they are merged, and
    refusing them before the merge refuses copper KiCad would have accepted.
    """
    p = tuple(pts)
    return len(p) >= 2 and is_octilinear(p) and turn_ok(p)


def lane_ok(scene: Scene, pieces: Sequence[Piece], refs: Sequence[str]) -> tuple[bool, str]:
    """A.7's lane rule, asked of a candidate: a piece may **cross** a fanout lane and may not **run
    along** one.

    This is the DS2 Addon's lesson exactly (`docs/copper-plan.md` line 191): analog copper ran along
    a lane and walled AVDD, DVDD and the UART pins in. Forbidding crossing outright would cut
    c3_usb's board in half along `J1`'s 1.4515 mm lane, so the threshold is `lane_width + w + 2 *
    grid` — long enough for any crossing, short enough that a run is one. A lane's owner is exempt in
    its own lane, and for a link both ends' footprints are owners: a stub into a closed row's pad is
    inside that row's lane by construction.
    """
    from ..route_verify import lane_overrun, via_in_lane

    exempt = frozenset({*refs} | {p.owner.split(".")[0] for p in pieces})
    # The same set `route_verify.served_refs` computes from the finished copper: the footprints this
    # run lands on. The two must agree, or a candidate the pattern accepts fails the self-check.
    for p in pieces:
        if p.kind == "via":
            # A via cannot cross a lane at all — it occupies one. Asked here because until S5's
            # review this loop skipped every piece that was not a segment, so 105 of the tap's 210
            # pieces were never put to the rule (finding 6).
            who = via_in_lane(scene, p.a, p.w, exempt)
            if who:
                return (False, f"puts a via inside {who}")
            continue
        run, who = lane_overrun(scene, p.a, p.b, p.w, exempt)
        if who:
            return (False, f"runs {run:g} mm along {who}")
    return (True, "")


def legs_of(pieces: Sequence[Piece]) -> tuple[Pt, ...]:
    """The merged path a `pieces_of` run came out as, for the length judge."""
    if not pieces:
        return ()
    return (pieces[0].a,) + tuple(p.b for p in pieces)


# --- C.1: the stage -------------------------------------------------------------------------------

PRE = ("hop",)
"""The pre stage, in C.1's order, least free first. `fanout` is not here: it is `route.py`'s own
step, run **after** the hops so `fanout._excluded` can drop the nets the hops claimed — a closed
row's lane is better spent on the hop that needed it than on a via the hop then has to start from."""

MID = ("spine",)
"""The rest of C.1's pre stage, the half that runs **after** the fanout.

C.1 puts the fanout at step 2 and the spine at step 5, and the order is the whole argument of the
stage: least free first. A closed row's escape has exactly one direction it can leave in, and a
spine is the **widest** copper pcbc writes — buck's `VIN` trunk is 0.781 mm, wider than anything else
on that board — so a spine placed first can stand across a lane that a fanout via then has no second
answer for. Running it after means the spine sees every escape as an obstacle and steps round it,
which is the direction the dependency actually runs.

It costs one extra stage name and one merge (`merge_plans`), and it is measured rather than assumed:
`docs/r2-measurements.md` S7 records both orders on the two boards that have a fanout at all.

**`chain` is written and is deliberately NOT in this tuple.** `patterns/chain.py` exists, its unit
tests run against it directly, and `route_verify.chain_order` — the finished-board half S6 was really
for — is wired into the build. The pattern itself does not run, because a real build says it must not
yet. On ds2, a board that builds today, every configuration tried breaks it, and never where the
copper is:

| what the chain wrote on ds2 | outcome |
|---|---|
| the whole population, 51 seg / 98.6 mm | `GND` and `VSS` unrouted |
| implicit links bounded to `CHAIN_HOP_MM`, 18 seg / 30.8 mm | `GND` and `VSS` unrouted |
| the one declared chain only, 5 seg / 14.4 mm | copper verified, then the `GND` plane in **2 islands** |
| every link bounded, 13 seg / 16.4 mm on three short local runs | `GND` and `VSS` unrouted |

The last row is the one that decided it: 16.4 mm of short, local, legal copper on the far side of a
47 x 25.4 mm board, none of it within 3.4 mm of the tap that fails and none of it on the layer of the
plane that splits. Locked copper moves KRT, and KRT's own copper closes the escape two stages later.
The other four boards agree in the smaller way: with the chain registered, `test_examples_fab`
fails on buck, c3_usb and node (node's `angles` 102 against a ceiling of 100), and only blinky is
untouched.

There is precedent for the shape of this, and it is `spine.WIDE_MM`: S7 measured that ds2's 0.25 mm
power nets could not take a spine either, because locked copper takes the corridors its constrained
nets structurally need, and excluded them on the measurement rather than tuning until it passed.
This is the same board saying the same thing one pattern later.

What would change it is a router that can search, not a bound that can be tuned: four bounds were
tried and the failure moved rather than went away. R3's maze router is where this belongs, and
`chain.py` is kept whole — not deleted and not behind a runtime flag — so that slice starts from the
pattern, its 17 tests and this table rather than from scratch.
"""


POST = ("tap",)
"""The post stage, in C.1's order. `tap` (B.3) is S5's; `guard` and `stitch` (B.6) are S8's and are
fixture-only, because no example declares `Guard()`.

It runs between KRT's `planes` and `signals` steps in both stackups, which is a measured choice and
not a tidy one: of the four orderings the design tried on node, taps before KRT boxed the USB pair in
(detour 1.72 -> 1.83) and taps after the signals left **21 unconnected** pads, because the signals had
taken every tap site. C.1's table has all four."""


FINAL = ("stitch",)
"""The stage after KRT's last step: copper whose absence leaves nothing unconnected.

The governing rule is `docs/stitch-plan.md` **R-S1** — *copper whose absence leaves nothing
unconnected is written after the last router step that could have used the space it takes* — with
the two corollaries that are the whole reason this stage can exist. **R-S2:** a stitch takes what is
left; it is never an input to a route. **R-S3:** a stitch whose count is an *electrical requirement*
is all-or-nothing, and one whose count is a *target* applies partially and reports the shortfall.

`POST` cannot hold that copper, and the reason is structural rather than a preference. A tap has to
be followed by a router step — a pad the tap could not weld is an unconnected pad, and KRT's
`plane_taps` is what repairs it — which is why C.1's four-way measurement on node put the taps
between `planes` and `signals` and why taps after the signals left **21 unconnected** pads. A stitch
is the opposite shape: losing its site costs a shield, not a pad, so nothing has to run after it, and
`krt_plan` schedules nothing after `patterns_final` on either stackup. The ds2 mechanism — KRT
reacting to pcbc's locked copper, the table under `MID` and `spine.WIDE_MM` — is therefore not
*bounded* here, it is **absent**.

**It was empty when the stage landed, and the emptiness was the measurement.** With `FINAL = ()` the
stage ran for real on every board — `build_scene` over the finished copper, `pattern_copper`'s
self-check, a step file of its own — and wrote nothing, so a fresh build's `routed/layout.kicad_pcb`
was byte-identical (sha256) to the same build without the stage. Measured on all five, each in its
own temp dir, the extra step file being a byte copy of the board before it:

| board | `routed/layout.kicad_pcb` | the step it adds, and its input |
|---|---|---|
| ds2    | identical | `06_patterns_final` == `05_finalize` |
| blinky | identical | `05_patterns_final` == `04_finalize` |
| buck   | identical | `08_patterns_final` == `07_finalize` |
| c3_usb | identical | `07_patterns_final` == `06_finalize` |
| node   | identical | `08_patterns_final` == `07_signals` |

**`stitch` is the first copper it writes, and the claim held.** Measured 2026-09-21 with
`FINAL = ("stitch",)`: node gains **one rung** — a 0.35/0.2 twin at (28.9,33.4) beside its `VBUS`
barrel at (28,33.4), with a 0.4 mm link on F.Cu and one on B.Cu — and `BAR`, `DETOURS`, `SOFT`,
`LEFTOVER`, `SPINE_LINKS`, `SPINE_NETS`, `SPINE_REFUSED` and `TAP_REFUSED` are byte-identical on it,
as are the `hop`, `spine` and `tap` entries of `REFUSED`. Nothing routes after the stage, so there is
nothing left to react to its copper; that is the whole architecture and it is now tested rather than
argued. What did move is exactly the list §1.2 says can: `VIAS_PATTERN`, `OWNS`, and the two inner
plane areas by the antipads the rung carves.

**ds2 is the board that matters, and it is still empty with the first carrier down** — for want of a trigger,
not for want of a tuned bound. Its four power nets (`3V3`, `GND`, `VDDA`, `VSS`) each declare 0.1 A
against the **0.871 A** one 0.4/0.018 mm barrel carries, so `ViaSpec.per_change` is **1** and every
via group on the board is already rated; it is TSSOP-16 with **zero** `pad_prop_heatsink` pads, so a
thermal array has no land to sit on; and `Design.guards` is `[]`. There is no number in that
paragraph that could be moved to make ds2 place a stitch. `docs/stitch-plan.md` §4, §7 (S2).
"""


_STAGES = {"pre": PRE, "mid": MID, "post": POST, "final": FINAL}
"""Which patterns each stage runs. `pre` and `mid` are the two halves of C.1's pre stage, with
`route.py`'s own fanout step between them; `post` sits inside the KRT sequence and `final` after all
of it. The keys are the `--stage` values `route.pcbc_step` writes into the plan and `route_job` reads
back out of the command, so a stage exists here or it does not run."""


def _modules() -> dict:
    from . import chain, hop, spine, stitch, tap

    return {"chain": chain, "hop": hop, "spine": spine, "stitch": stitch, "tap": tap}


def pattern_copper(
    design: Design,
    job: CompiledJob,
    cs: ConstraintSet,
    text: str,
    board: str,
    *,
    stage: str = "pre",
    scene: Scene | None = None,
    verify: bool = True,
    owned: Sequence[Piece] = (),
) -> PatternPlan:
    """Run the stage's patterns over one board, in order, and hand back what KRT still owes.

    Each pattern's copper enters the scene before the next pattern runs, so the stage is a single
    left-to-right pass: no pattern ever revisits a decision, and the board is a pure function of the
    placed board plus the `ConstraintSet`.
    """
    t0 = time.perf_counter()
    scene = scene if scene is not None else build_scene(design, job, cs, text)
    ctx = PatternCtx(scene=scene, design=design, job=job, cs=cs, board=board, stage=stage, owned=tuple(owned))
    pieces: list[Piece] = []
    ids: list[int] = []
    moves: list[str] = []
    notes: list[str] = []
    refused: dict[str, list[Refusal]] = {}
    claimed: set[str] = set()
    links: dict[str, dict[str, list]] = {}
    mods = _modules()
    for reason in _STAGES[stage]:
        mod = mods[reason]
        # `CONNECTS` is the module's answer to "does your copper join the pads of the net it is on?"
        # (`docs/stitch-plan.md` §2k). Every module in `_modules()` says True and the read is a no-op
        # today; it is here so the one that will say False cannot be added without deciding. Without
        # it, a guard that wrote only `GND` copper would `claimed.add` the *guarded* net, and
        # `PatternPlan.done` — which `krt_plan` drops nets from and `signals` writes `!NET` for —
        # would then say something false about a net no pattern routed. Read as an attribute, not a
        # `getattr` default: a new module that forgets to declare it should fail here, loudly.
        connects = mod.CONNECTS
        for spec in mod.specs(ctx):
            res = mod.run(ctx, spec)
            if res.links != (0, 0) or res.coverage:
                links.setdefault(reason, {})[res.net] = [res.links[0], res.links[1], res.coverage]
            if res.pieces:
                added = scene.add(scene.item_of(p) for p in res.pieces)
                pieces.extend(res.pieces)
                ids.extend(it.id for it in added)
                if connects:
                    claimed.add(res.net)
            if res.refusal is not None:
                refused.setdefault(res.net, []).append(res.refusal)
                moves.append(res.refusal.move)
            notes.extend(res.notes)
    done = frozenset(n for n in sorted(claimed) if not net_open(scene, n))
    plan = PatternPlan(
        text=write_pieces(text, pieces) if pieces else text,
        pieces=tuple(pieces),
        ids=tuple(ids),
        census=_census(pieces),
        moves=tuple(moves),
        notes=tuple(notes),
        done=done,
        partial=frozenset(sorted(claimed - done)),
        refused={n: tuple(rs) for n, rs in sorted(refused.items())},
        claimed=frozenset(claimed),
        links={r: dict(sorted(v.items())) for r, v in sorted(links.items())},
        wall_ms=int(round((time.perf_counter() - t0) * 1000)),
        scene=scene,
    )
    if verify:
        from ..route_verify import verify_copper

        bad = verify_copper(scene, plan.pieces, cs, ids=plan.ids)
        if bad:
            raise ValueError("pattern copper failed its own self-check:\n  " + "\n  ".join(bad))
    return plan


def merge_plans(first: PatternPlan, second: PatternPlan) -> PatternPlan:
    """The two halves of C.1's pre stage, with the fanout between them, as the one plan KRT is handed.

    `second` ran on `first`'s own scene, so the scene is already shared and `done` is recomputed over
    both stages' claims against it: a net the hops half-claimed and the spine finished is `done`, and
    asking each half on its own would call it `partial` twice and hand it to KRT anyway.
    """
    scene = second.scene if second.scene is not None else first.scene
    pieces = first.pieces + second.pieces
    claimed = first.claimed | second.claimed
    done = frozenset(n for n in sorted(claimed) if scene is not None and not net_open(scene, n))
    refused: dict = {n: tuple(rs) for n, rs in first.refused.items()}
    for n, rs in second.refused.items():
        refused[n] = tuple(refused.get(n, ())) + tuple(rs)
    return PatternPlan(
        text=second.text,
        pieces=pieces,
        ids=first.ids + second.ids,
        census=_census(pieces),
        moves=first.moves + second.moves,
        notes=first.notes + second.notes,
        done=done,
        partial=frozenset(sorted(claimed - done)),
        refused={n: refused[n] for n in sorted(refused)},
        claimed=claimed,
        links={r: dict(sorted({**first.links.get(r, {}), **second.links.get(r, {})}.items())) for r in sorted({*first.links, *second.links})},
        wall_ms=first.wall_ms + second.wall_ms,
        scene=scene,
    )


def hard_refusals(plan: PatternPlan) -> tuple[Refusal, ...]:
    """The refusals that are fatal even in R2 (C.6), and under `--strict-patterns` all of them.

    No pattern sets `hard` today (see `Refusal`), so without the env var this returns `()` on every
    board in the repo. It is the `--strict-patterns` path that keeps it wired, and the two
    unimplemented cases `Refusal.hard` names that keep it worth having."""
    import os

    strict = os.environ.get("PCBC_STRICT_PATTERNS", "").lower() in ("1", "true", "yes", "on")
    return tuple(r for r in plan.refusals() if r.hard or strict)


def patterns_off() -> bool:
    """`PCBC_PATTERNS=off` restores the pre-R2 plan exactly — one env check around one call. It is
    the difference between a bad pattern being a rollback and being a revert (C.6)."""
    import os

    return os.environ.get("PCBC_PATTERNS", "").lower() in ("off", "0", "false", "no")


def empty_plan(text: str, scene: Scene | None = None) -> PatternPlan:
    """What the stage gives when it is switched off: the board unchanged and nothing claimed."""
    return PatternPlan(text=text, scene=scene)
