"""What a routed power net actually carries, end to end.

`copper.power_ampacity_failures` asks whether a net's copper is wide enough for its current, and
until the S7 review it asked that of the net's **widest** track (`copper_by_net`'s `width` is a
`max`). One wide segment satisfied the whole net however narrow the rest of it was, so the check
could not fail — and did not, on three boards that violate its own criterion: buck shipped 19.39 mm
of 0.3905 mm copper on a 2 A `VIN`, c3_usb 24.07 mm under class on `VBUS` including five 0.127 mm
cut edges in series, node 6.48 mm at 0.200 mm on a 1 A rail (findings 1, 3, 7, 12, 20).

A net is as good as its **narrowest series copper**, so that is what this module measures: the
widest-bottleneck path between every pair of the net's pads over the copper that actually touches,
and the worst of those paths. The unit is amps rather than millimetres, because a via and a track
are both on that path and they are not comparable as widths — a 0.2 mm drill carries 0.527 A and a
0.2 mm track carries 0.745 A. `stackup.track_amps` and `stackup.via_amps` put both on the curve the
widths were derived from, so one number answers the question the width was asked for: **how much
current can reach this pad**.

That also closes the second half of finding 2 — node's `VBUS` carries its declared 1 A through two
single 0.2 mm vias, each rated 0.527 A, and nothing in the build looked at a via's current at all
(`via.per_change` appears nowhere outside `constraints.py`). Vias close enough together to be a
stitch are treated as parallel and carry `n x via_amps`; a lone via on a path carries one via's
worth, which is what a cut edge is.

**This is a measurement, not the gate.** Three of the five boards fail it today and R2 cannot fix
them: the copper that necks is KRT's leftover, which R3's maze router owns. `docs/r2-measurements.md`
S7 records the numbers and `test_examples_fab.py` pins them as a ledger that must improve. The gate
(`copper.power_ampacity_failures`) judges the copper **pcbc itself wrote**, where a neck is a bug
pcbc committed rather than one it inherited.
"""

from __future__ import annotations

import fnmatch
import itertools
import math
import re
from collections.abc import Sequence
from dataclasses import asdict, dataclass

from .compile import CompiledJob
from .copper import copper_by_net, net_table
from .pads import pad_geoms
from .route_geom import Shape, aabb, gap, track_shape, via_shape
from .sexp import board_footprint_spans, footprint_at, footprint_reference
from .stackup import WIDTH_FLOOR_MM, track_amps, via_amps

TOUCH_MM = 1e-4
"""`route_geom.EPS_MM`: two pieces of copper are connected when their shapes are this close. KiCad
joins tracks that touch, and the quantised geometry means "touch" is exactly zero gap; the epsilon is
the same 0.1 um slack every other geometry decision here carries."""

VIA_PARALLEL_MM = 1.0
"""How close two vias of one net have to be before they count as carrying the current together.

A stitching pair is parallel and a trunk's two layer changes are in series, and the only thing that
separates them on a finished board is how far apart they are: a fab's hole-to-hole plus two annular
rings is about 0.7 mm on these stackups, so vias within 1.0 mm of each other were placed as a group
and vias further apart were placed for different reasons. node's two `VBUS` vias are 2.9 mm apart and
are therefore two cut edges at 0.527 A each, which is what finding 2 measured by hand."""

CURVE_EPS = 0.01
"""How much less than the declared current a path may carry and still read as carrying it.

`ipc2221_width_mm` rounds its answer to 3 dp, so the width a class was given is up to 0.0005 mm
narrower than the curve asked for, and asking the inverse back gives fractionally less current than
went in: buck's `5V` at exactly its 0.781 mm class width comes back as **1.999 A** of 2 A. One
percent is two orders of magnitude above that rounding and two below any neck this measures — the
smallest real one on these boards is buck's `VIN` at 60 % of its rail."""

ALL_CU = frozenset({"F.Cu", "B.Cu", "In1.Cu", "In2.Cu"})

_SEG = re.compile(
    r"\(segment\s*\(start ([-0-9.]+) ([-0-9.]+)\)\s*\(end ([-0-9.]+) ([-0-9.]+)\)\s*\(width ([-0-9.]+)\)"
    r'(?:\s*\(locked yes\))?\s*\(layer "([^"]+)"\)\s*\(net (?:(\d+)|(?:\d+\s+)?"([^"]*)")\)'
)
_VIA = re.compile(
    r'\(via\s*\(at ([-0-9.]+) ([-0-9.]+)\)\s*\(size ([-0-9.]+)\)\s*\(drill ([-0-9.]+)\)\s*\(layers "([^"]+)" "([^"]+)"\)'
    r'(?:\s*\(locked yes\))?\s*\(net (?:(\d+)|(?:\d+\s+)?"([^"]*)")\)'
)


@dataclass(frozen=True)
class Node:
    """One piece of copper on one net: its shape, the layers it is on, and what it carries."""

    key: tuple
    shape: Shape
    layers: frozenset[str]
    amps: float  # math.inf for a pad or a plane: neither is the conductor a width question is about
    mm: float


@dataclass(frozen=True)
class Bottleneck:
    """The worst pad-to-pad path on one power net."""

    net: str
    amps: float  # what the net declares (`NetReq(amps=)`)
    carries: float  # what the worst path between two of its pads can carry
    width_mm: float  # the narrowest track on that path (0.0 when a via is the bottleneck)
    kind: str  # "track" | "via" | "open" — which piece of copper is the bottleneck
    where: str  # "J_IN.1->U1.3": the pad pair the path runs between
    need_mm: float  # max(IPC-2221 external, IPC-2152) at the declared current
    class_mm: float  # the class width the copper was supposed to be written at
    under_mm: float  # millimetres of this net's copper narrower than the class
    zoned: bool  # the net carries a zone in the file, so a plane is its conductor
    at_mm: str = ""
    """Where the narrowest piece actually **is** — "17.4,10.8 on F.Cu" — and not where the path that
    found it ends. `where` is a pad pair, and every pair whose path crosses one neck ties at that
    neck's amps, so the pair that gets reported is the first in sorted order among the tied ones:
    provenance, never a location. buck is the proof — its 2 A path is `J_IN.1 -> U1.3` and the pair
    reported is `C_IN1.1 -> R_EN.1`, a 100 k enable pull-up drawing 0.12 mA. Telling an author to
    move those two parts closer names two parts that are not on the rail."""
    verdict: str = "ok"
    """Which of three different things is wrong, because they are not the same severity.

    - `under current` — the path cannot carry what the net declares. An electrical fact.
    - `under floor` — it carries the current on the curve but is narrower than `need_mm`. `need_mm`
      is `max(IPC-2221, IPC-2152)` at the declared current and is usually a **curve** value; it is
      the `WIDTH_FLOOR_MM` constant only at the bottom, where `ipc2221_width_mm` clamps. Which of
      the two it is decides whether `NetReq(amps=)` is an edit at all, so `power_moves` tests it
      rather than assuming (`at_floor`). c3_usb's `VBUS` is the clamped case and only that case:
      0.127 mm carries 0.536 A of its 0.5 A with 7 % margin and no allowance for the LDO's inrush
      (finding 3), and no current an author can declare moves a constant.
    - `open` — two pads of the net have no copper path between them at all.
    """


def board_pad_geoms(text: str, layers: tuple[str, ...] = ("F.Cu", "B.Cu", "In1.Cu", "In2.Cu")) -> list:
    """Every pad that draws copper, SMD and through-hole alike.

    `fab.board_pads` filters to `smd`, which is right for a via-in-pad question and wrong here: buck's
    `VIN` enters the board through `J_IN`, a through-hole JST, and dropping it would leave the rail's
    source out of its own ampacity path."""
    out = []
    for start, end in board_footprint_spans(text):
        block = text[start:end]
        ref = footprint_reference(block) or "?"
        at = footprint_at(block)
        if at is None:
            continue
        head = block[: block.find("(pad")] if "(pad" in block else block
        side = "B" if re.search(r'\(layer "B\.Cu"\)', head) else "F"
        out.extend(g for g in pad_geoms(block, at, side=side, ref=ref, layers=layers) if g.copper)
    return out


def _via_clusters(vs: list[dict]) -> dict[int, int]:
    """Index -> how many vias are in its parallel group (`VIA_PARALLEL_MM`), single-linkage."""
    parent = list(range(len(vs)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i, j in itertools.combinations(range(len(vs)), 2):
        if math.dist(vs[i]["at"], vs[j]["at"]) <= VIA_PARALLEL_MM:
            a, b = find(i), find(j)
            if a != b:
                parent[a] = b
    size: dict[int, int] = {}
    for i in range(len(vs)):
        size[find(i)] = size.get(find(i), 0) + 1
    return {i: size[find(i)] for i in range(len(vs))}


def net_nodes(text: str, net: str, pads: list, *, dt: float, stack, plane_h: float | None, plating: float = 0.018) -> list[Node]:
    """Every piece of copper on `net`, each carrying what it can carry."""
    names = net_table(text)
    out: list[Node] = []
    for i, m in enumerate(_SEG.finditer(text)):
        name = names.get(int(m.group(7))) if m.group(7) else m.group(8)
        if name != net:
            continue
        x1, y1, x2, y2, w = (float(m.group(k)) for k in range(1, 6))
        out.append(
            Node(
                key=("seg", i),
                shape=track_shape((x1, y1), (x2, y2), w),
                layers=frozenset({m.group(6)}),
                amps=track_amps(w, dt, stack, plane_h),
                mm=math.hypot(x2 - x1, y2 - y1),
            )
        )
    vs = []
    for m in _VIA.finditer(text):
        name = names.get(int(m.group(7))) if m.group(7) else m.group(8)
        if name != net:
            continue
        vs.append({"at": (float(m.group(1)), float(m.group(2))), "size": float(m.group(3)), "drill": float(m.group(4))})
    parallel = _via_clusters(vs)
    for i, v in enumerate(vs):
        out.append(
            Node(
                key=("via", i),
                shape=via_shape(v["at"], v["size"]),
                layers=ALL_CU,
                amps=round(parallel[i] * via_amps(v["drill"], plating, dt), 3),
                mm=0.0,
            )
        )
    for p in pads:
        if p.net != net:
            continue
        for j, sh in enumerate(p.copper):
            out.append(Node(key=("pad", p.id, j), shape=sh, layers=p.cu_layers, amps=math.inf, mm=0.0))
    return out


def _adjacency(nodes: list[Node]) -> dict[tuple, set[tuple]]:
    boxes = [aabb(n.shape) for n in nodes]
    adj: dict[tuple, set[tuple]] = {}
    for i, j in itertools.combinations(range(len(nodes)), 2):
        if not (nodes[i].layers & nodes[j].layers):
            continue
        a, b = boxes[i], boxes[j]
        if a[0] > b[2] + TOUCH_MM or b[0] > a[2] + TOUCH_MM or a[1] > b[3] + TOUCH_MM or b[1] > a[3] + TOUCH_MM:
            continue
        if gap(nodes[i].shape, nodes[j].shape) <= TOUCH_MM:
            adj.setdefault(nodes[i].key, set()).add(nodes[j].key)
            adj.setdefault(nodes[j].key, set()).add(nodes[i].key)
    return adj


def widest_path(adj: dict[tuple, set[tuple]], amps: dict[tuple, float], sources: list[tuple]) -> dict[tuple, float]:
    """Dijkstra with `min` for `+` and `max` for `min`: the best bottleneck reachable from `sources`.

    Conservative in the one direction that matters — two parallel paths are not summed, so the answer
    is the best **single** path and never more than the copper really carries."""
    import heapq

    best: dict[tuple, float] = {}
    heap: list[tuple[float, tuple]] = []
    for s in sources:
        w = amps.get(s, math.inf)
        if w > best.get(s, -1.0):
            best[s] = w
            heapq.heappush(heap, (-w, s))
    while heap:
        nw, n = heapq.heappop(heap)
        nw = -nw
        if nw < best.get(n, -1.0) - 1e-12:
            continue
        for m in adj.get(n, ()):
            w = min(nw, amps.get(m, math.inf))
            if w > best.get(m, -1.0) + 1e-12:
                best[m] = w
                heapq.heappush(heap, (-w, m))
    return best


def power_bottlenecks(job: CompiledJob, text: str) -> dict[str, dict]:
    """Per power net with a declared current: the worst pad-to-pad path, as a JSON-able dict.

    A net that carries a zone is reported with `zoned` true and no path walk: a plane is the rail's
    conductor and "the narrowest track" is not the question being asked of it. That is also the
    exemption `job.planes` was meant to give and could not — it is `()` on buck, c3_usb and ds2 while
    every one of those boards has a poured `GND` in the routed file (finding 1)."""
    cs = job.constraints
    if cs is None:
        return {}
    layers = ("F.Cu", "B.Cu", "In1.Cu", "In2.Cu") if job.layers >= 4 else ("F.Cu", "B.Cu")
    pads = board_pad_geoms(text, layers=layers)
    census = copper_by_net(text)
    out: dict[str, dict] = {}
    for net in job.nets:
        if net.kind != "power" or not net.amps:
            continue
        for name in sorted(n for n in census if any(fnmatch.fnmatch(n, p) for p in net.patterns)):
            if name in out:
                continue
            c = cs.by_net(name)
            if c is None or c.current is None:
                continue
            cur = c.current
            need = round(max(cur.width_ipc2221.value, cur.width_ipc2152.value), 4)
            cls = round(float(c.width_mm.value), 4)
            # The zone **in the file**, and nothing else. `job.planes` used to be an alternative
            # here and it is a predictive exemption: `krt_plan` reads `planes=` only when
            # `job.layers > 2`, so `Board(planes=[("VIN", "B.Cu")])` on a two-layer board pours no
            # copper at all and would have stopped `VIN` being measured — which is the shape of the
            # move `power_moves` prints, so the tool could have talked an author into silencing it.
            # The census reads the routed board, where every real pour is a zone, so nothing is lost.
            zoned = bool(census[name].get("zone"))
            nodes = net_nodes(text, name, pads, dt=cur.temp_rise_c, stack=cs.stackup, plane_h=cur.plane_h_mm)
            under = round(sum(n.mm for n in nodes if n.key[0] == "seg" and n.amps < math.inf and n.mm and _narrow(n, cls)), 3)
            row = Bottleneck(
                net=name,
                amps=float(cur.amps),
                carries=math.inf,
                width_mm=0.0,
                kind="track",
                where="",
                need_mm=need,
                class_mm=cls,
                under_mm=under,
                zoned=zoned,
            )
            if not zoned:
                row = _verdict(_walk(row, nodes, pads, name))
            out[name] = {**asdict(row), "carries": None if row.carries == math.inf else row.carries}
    return out


def _narrow(node: Node, cls: float) -> bool:
    return node.shape.r * 2.0 + 1e-9 < cls


def _walk(row: Bottleneck, nodes: list[Node], pads: list, net: str) -> Bottleneck:
    by_pad: dict[str, list[tuple]] = {}
    for n in nodes:
        if n.key[0] == "pad":
            by_pad.setdefault(n.key[1], []).append(n.key)
    if len(by_pad) < 2:
        return row
    adj = _adjacency(nodes)
    amps = {n.key: n.amps for n in nodes}
    width = {n.key: round(n.shape.r * 2.0, 4) for n in nodes if n.key[0] == "seg"}
    ids = sorted(by_pad)
    worst: tuple[float, str, tuple] | None = None
    for a in ids:
        best = widest_path(adj, amps, by_pad[a])
        for b in ids:
            if b <= a:
                continue
            hit = [best[k] for k in by_pad[b] if k in best]
            got = max(hit) if hit else -1.0
            if worst is None or got < worst[0]:
                worst = (got, f"{a}->{b}", ())
    if worst is None:
        return row
    carries, where, _ = worst
    if carries < 0:
        return Bottleneck(**{**asdict(row), "carries": 0.0, "kind": "open", "where": where})
    # Which piece is the bottleneck, and how wide it is — a via has no width a class can be compared
    # against, so `width_mm` is 0.0 there and `kind` says which number to read.
    at = [n for n in nodes if abs(n.amps - carries) < 1e-9]
    kind = "via" if at and all(n.key[0] == "via" for n in at) else "track"
    w = min((width[n.key] for n in at if n.key in width), default=0.0)
    return Bottleneck(
        **{
            **asdict(row),
            "carries": round(carries, 3),
            "kind": kind,
            "width_mm": w,
            "where": where,
            "at_mm": _at(at),
        }
    )


def _at(nodes: list[Node]) -> str:
    """Where the narrowest copper is: the centre of the first such piece, and the layer it is on.

    One coordinate and not a list, because the move wants somewhere to look and the ledger already
    carries the millimetres. Sorted so it is the same string on every build of the same board."""
    if not nodes:
        return ""
    def mid(n: Node) -> tuple[float, float]:
        xs = [x for x, _ in n.shape.pts]
        ys = [y for _, y in n.shape.pts]
        return round(sum(xs) / len(xs), 4), round(sum(ys) / len(ys), 4)

    best = min(nodes, key=lambda n: (*mid(n), sorted(n.layers)))
    x, y = mid(best)
    return f"{x:g},{y:g} on {'/'.join(sorted(best.layers))}"


def _verdict(row: Bottleneck) -> Bottleneck:
    if row.kind == "open":
        return Bottleneck(**{**asdict(row), "verdict": "open"})
    if math.isinf(row.carries):
        # `_walk` returned the row untouched: the net has fewer than two pads, so nothing was
        # measured. `width_mm` is then 0.0 and the floor test below — `0.0 < need_mm` — was always
        # true, stamping an unmeasured net `under floor` and, once `power_moves` existed, printing
        # "carries 0 A ... 0 mm track at " with an empty location. Under `--strict-power` that
        # failed the build on a net with no measured shortfall at all.
        return row
    if row.carries + 1e-9 < row.amps * (1.0 - CURVE_EPS):
        return Bottleneck(**{**asdict(row), "verdict": "under current"})
    if row.kind == "track" and row.width_mm + 1e-9 < row.need_mm:
        return Bottleneck(**{**asdict(row), "verdict": "under floor"})
    return row


def bottleneck_lines(rows: dict[str, dict]) -> list[str]:
    """One line per power net, for `FAB_NOTES.md` and the build's own output."""
    out = []
    for name, r in sorted(rows.items()):
        if r["zoned"]:
            out.append(f"{name}: a zone carries {r['amps']:g} A; no series track to measure")
            continue
        carries = r["carries"]
        piece = f"{r['width_mm']:g} mm track" if r["kind"] == "track" else f"one {r['kind']}"
        # The neck's coordinate here too, where there is one: a person checking the electrics wants
        # somewhere to look, and the pad pair is provenance rather than a location (`Bottleneck.at_mm`).
        spot = f" at {r['at_mm']}" if r.get("at_mm") else ""
        out.append(
            f"{name}: {r['where']} carries {carries if carries is not None else 0:g} A of {r['amps']:g} A "
            f"through its narrowest {piece}{spot} ({r['under_mm']:g} mm under the {r['class_mm']:g} mm "
            f"class, IPC asks {r['need_mm']:g} mm) [{r['verdict']}]"
        )
    return out


def _plane_layer(layers: Sequence[str], taken: Sequence[tuple[str, str]] = ()) -> str | None:
    """Which layer a `Board(planes=...)` suggestion should name, or `None` for no suggestion at all.

    A free **inner** copper layer, because a plane layer is what an inner layer is for, and nothing
    otherwise: not on two layers, where `Board(planes=...)` is not an edit that does anything at all,
    and not on the outer layers of a four-layer board, where every part is already standing.

    Three things a move must never do, and the first version of this did all three. Name a layer the
    board does not have (`In1.Cu` to a two-layer board). Name a layer another net already pours: node
    pours `GND` on `In1.Cu` and `3V3` on `In2.Cu`, and its `VBUS` move said `In1.Cu`, an edit that
    lays a second plane over the ground plane on a board whose own D.5 check then asks why `GND` is
    in two pieces. And — the one that matters most — **name an edit that silences the measurement
    without pouring any copper**: `route.krt_plan` reads `planes=` only when `job.layers > 2`
    (`plane_nets = ... if job.planes and job.layers > 2 else []`), so on buck a
    `Board(planes=[("VIN", "B.Cu")])` pours nothing whatever, while `power_bottlenecks` would mark
    `VIN` zoned and stop measuring it. A move that makes the warning disappear and the board no
    better is worse than no move.
    """
    if len(tuple(layers)) <= 2:
        return None
    free = [n for n in layers if n not in {L for _, L in taken} and n.startswith("In")]
    return free[0] if free else None


def power_moves(
    rows: dict[str, dict], layers: Sequence[str] = (), planes: Sequence[tuple[str, str]] = ()
) -> list[str]:
    """The rails that cannot carry what the board declares, phrased as the edits that fix them.

    `bottleneck_lines` is the *ledger* — every power net, passing or not, in `FAB_NOTES.md` for a
    person reading the electrics. This is the *move*: only the nets whose verdict is not `ok`, each
    naming the four edits available, so `pcbc build` itself says what to change instead of leaving
    the shortfall in a file nobody opened. Three of the five example boards print a move here today.

    The four edits, in the order an author should consider them:

    1. **Move the parts.** The neck is almost always KRT's leftover copper filling a gap pcbc's own
       patterns did not span, and the gap is a placement fact: bring the parts either side of that
       copper together with `Place()` and the path the current takes is shorter and wider. The move
       names the neck's **coordinate** (`at_mm`), not the pad pair the walk happened to end on.
    2. **Give the rail a plane.** `Board(planes=[(net, layer)])` makes the conductor a pour rather
       than a track, and a poured net is exempt here for exactly that reason (`zoned`). The layer is
       one the board has and no other net already pours (`_plane_layer`); where there is no such
       layer the suggestion is left out rather than made wrong.
    3. **Declare what it really carries.** Change `amps=` on the `NetReq` the net already has — a
       2 A number written for a connector's rating rather than the load is the commonest cause of a
       move that cannot be satisfied. Every edit here is an edit to an existing line: a *new*
       `NetReq` for a net another one names is refused twice, and a second `Board()` raises at load.
    4. **Widen it by hand** in KiCad, which is the escape hatch and not a fix pcbc can keep.

    Deliberately *not* a build failure by default (`--strict-power` makes it one): the copper that
    necks is the router's leftover, R3's maze router owns it, and a gate that stops three of five
    boards on a fault the tool cannot yet repair teaches an author to pass `--force`, which is worse
    than a move they read.
    """
    plane = _plane_layer(layers, planes)
    out = []
    for name, r in sorted(rows.items()):
        if r["zoned"] or r.get("verdict", "ok") == "ok":
            continue
        head = f"{name}: "
        if r["verdict"] == "open":
            out.append(
                head + f"declared {r['amps']:g} A and no copper joins {r['where']}; the route left "
                f"the rail in two pieces — this is a tool bug, not a board move, so report it"
            )
            continue
        carries = r["carries"] or 0.0
        piece = f"{r['width_mm']:g} mm track" if r["kind"] == "track" else f"single {r['kind']}"
        # `need_mm` at the constant is the one case where re-declaring the current is not an edit:
        # `ipc2221_width_mm` never returns less than `WIDTH_FLOOR_MM` however small the amps, so no
        # number an author can write clears the row. c3_usb's `VBUS` is exactly this, and offering it
        # "declare what it carries" after saying it carries **more** than it declares (0.536 A of
        # 0.5 A) is a sentence that argues with itself — and taking the advice below 0.2 A would
        # narrow the class pcbc writes from 0.4 mm to 0.25 mm while the same line kept printing.
        at_floor = r["verdict"] == "under floor" and r["need_mm"] <= WIDTH_FLOOR_MM + 1e-9
        # The **neck's** own coordinate, and the pad pair only as provenance. Every pair whose path
        # crosses one neck ties at that neck's amps, so `where` is an arbitrary member of the tied
        # set: buck's is `C_IN1.1->R_EN.1`, a 100 k enable pull-up drawing 0.12 mA, while the 2 A
        # path is `J_IN.1->U1.3`. "Move those two parts closer" named two parts not on the rail.
        spot = r.get("at_mm") or ""
        at = f"at {spot}" if spot else f"on the {r['where']} path"
        seen = f"; path {r['where']}" if spot else ""
        if r["verdict"] == "under floor":
            why = (
                f"carries {carries:g} A of {r['amps']:g} A on the curve but its narrowest {piece} "
                f"{at} is under pcbc's {r['need_mm']:g} mm floor"
            )
            if at_floor:
                why += ", which is a constant and not a curve point"
        else:
            why = (
                f"declared {r['amps']:g} A, carries {carries:g} A through its narrowest {piece} {at}"
            )
        # Every edit below is an edit to a line the board **already has**. A move that reads as a
        # new statement is a build refusal, not a fix: a second `NetReq("VIN", ...)` is refused
        # twice over (`kind="generic"` does not take `amps=`, and the net is already named by
        # another NetReq), and a second bare `Board()` raises before `check` even runs.
        edits = []
        if not at_floor:
            # A shorter path is a narrower one less often: the neck is the leftover router filling a
            # gap the patterns did not span, and a gap is a placement fact. Left out at the floor,
            # where the copper is on the curve already and the number it misses is a constant.
            edits.append("Place() the parts either side of that copper closer together")
        if plane:
            edits.append(f'pour it: add ("{name}", "{plane}") to your Board(planes=...)')
        if not at_floor:
            edits.append(f'change amps= on the NetReq that already declares "{name}"')
        tail = (
            ", or ".join(edits)
            if edits
            else "No board.py edit widens it: the copper is the leftover router's and R3 owns its width"
        )
        out.append(
            head + why + f" ({r['under_mm']:g} mm of this net is narrower than its {r['class_mm']:g} mm "
            f"class{seen}). " + tail[0].upper() + tail[1:]
        )
    return out
