"""Constraints: every NetReq / Pair / Bus / Path / Isolation / Guard line turned into numbers with
their sources (docs/r1-design.md sections A and D).

`compile_constraints(design)` is pure: a `Design` in, a `ConstraintSet` out. It never reads a board
file, so `pcbc check --constraints` runs before any geometry exists. Everything here is a function of
the design and the stackup; iteration is sorted; every number carries the formula, the reference and
a note as a `Derived`, and the report (`ConstraintSet.lines`) prints one number per line.

`compile.py` projects the set onto `CompiledClass` / `CompiledNet` for every existing consumer;
`dru.py` writes KiCad rules from it. `DruRule` and `CompiledClass` live here so that
`constraints <- dru <- compile` has no cycle; `compile.py` re-exports them.
"""

from __future__ import annotations

import fnmatch
import pathlib
from dataclasses import asdict, dataclass, field, replace

from .layout import resolve_regions
from .model import BridgeReq, BusReq, Design, GuardReq, IsolationReq, NetReqSpec, PairReq, ThermalReq
from .stackup import (
    IPC2221_TABLE_6_1,
    IEC60664_TABLE_F5_PD2,
    PAIR_FIT_MM,
    Derived,
    Source,
    Stackup,
    capacitance_pf_per_mm,
    cu_text,
    ipc2221_formula,
    current_width_mm,
    diff_pair_geometry,
    diff_pair_width_mm,
    diff_pair_z,
    get_stackup,
    hole_floor,
    i2c_max_mm,
    iec_clearance_mm,
    iec_creepage_mm,
    ipc2152_board_modifier,
    ipc2152_plane_modifier,
    ipc2152_width_mm,
    ipc2221_clearance_mm,
    ipc2221_width_mm,
    is_outer,
    microstrip,
    microstrip_z0,
    pair_gap_mm,
    via_amps,
    via_theta_c_per_w,
    vias_for_theta,
    vias_per_change,
    width_for_z0,
)

__all__ = [
    "PAD_NECK_MAX_MM",
    "CREEPAGE_FROM_V",
    "FORMULAS",
    "PAIR_FIT_MM",
    "PRESETS",
    "SOFT_RULES",
    "BridgeSpec",
    "ClearanceTable",
    "CompiledClass",
    "Constraint",
    "ConstraintSet",
    "CurrentSpec",
    "Derived",
    "DruRule",
    "GroupSpec",
    "IsolationSpec",
    "KeepAway",
    "PairSpec",
    "RETURN_VERDICTS",
    "ReturnRule",
    "RuleArea",
    "Source",
    "ViaSpec",
    "VoltageSpec",
    "clearance_table",
    "compile_constraints",
    "return_rule",
    "return_rules",
    "slug",
]

FORMULAS = (
    "hj_microstrip",
    "hj_coupled_microstrip",
    "wadell_stripline",
    "wadell_stripline_asym",
    "ghione_naldi_cpwg",
    "ipc2221_ext",
    "ipc2221_int",
    "ipc2152_fit",
    "ipc2221_6_1",
    "iec60664_f5",
    "iec60664_f2",
    "i2c_capacitance",
    "via_barrel",
)

CREEPAGE_FROM_V = 60.0  # IEC 62368-1 ES1 boundary: below it no creepage rule is written
SOFT_RULES = ("track_width", "length_preset", "skew", "via_budget", "diff_pair_uncoupled")
SOFT = " [soft: warning in R1]"

# The kind aliases kept for blinky and c3_usb.
KIND_ALIASES = {"digital": "generic", "default": "generic"}

# Kwargs every kind accepts (A.4); `z_se_ohm` and `volts` are "on any kind" (D, notes on the table).
COMMON_KWARGS = ("max_mm", "class_name", "keep_clear_of", "keep_clear_mm", "volts", "rise_ps")

# The four-layer tuple today's power class carries on every board (byte-identical projection).
POWER_LAYERS = ("F.Cu", "B.Cu", "In1.Cu", "In2.Cu")


# ---------------------------------------------------------------------------------------------
# A.1 data model
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class ViaSpec:
    diameter_mm: float
    drill_mm: float
    allowed: bool
    count_max: int | None  # KiCad via_count (max n); None = unlimited; 0 when `allowed` is False
    amps: Derived | None  # one via at the net's temp rise; None when the net has no amps
    per_change: int  # ceil(amps / amps_per_via); 1 without amps


@dataclass(frozen=True)
class PairSpec:  # compiled; the language-level object is model.PairReq
    partner: str
    width_mm: Derived
    gap_mm: Derived
    z_diff_ohm: float  # target
    z_computed: Derived  # formula x bias at the chosen geometry (unit "ohm")
    tolerance_pct: float  # 15 for usb_hs, 10 otherwise
    skew_mm: Derived
    uncoupled_mm: Derived
    controlled: bool  # False when the stackup cannot reach the target (2-layer USB)
    length_mm: float | None = None  # Pair(length_mm=): each member Path's length budget between its stations
    line: int = 0  # the Pair line


@dataclass(frozen=True)
class GroupSpec:
    kind: str  # "bus" | "chain"
    members: tuple[str, ...]  # bus: net names (clock included); chain: "REF.PIN" in feed order
    match_mm: float | None  # bus skew budget
    clock: str | None
    line: int = 0  # the Bus / Path / NetReq line
    source: str = ""  # "preset clock" | "Bus line 12" | "NetReq line 40"
    length_mm: float | None = None  # Bus(length_mm=): each member's budget between the stations its Path names


@dataclass(frozen=True)
class CurrentSpec:
    amps: float
    temp_rise_c: float
    width_ipc2221: Derived  # external curve, always computed
    width_ipc2152: Derived  # with board-thickness and plane modifiers, always computed
    plane_h_mm: float | None  # dielectric to the nearest reference copper; None = no plane


@dataclass(frozen=True)
class VoltageSpec:
    volts: float
    row: str  # "B1" | "B2"
    clearance_mm: Derived  # IPC-2221B 6-1 value for the row (before the class max)
    creepage_mm: Derived | None  # IEC 60664-1 F.5 value; only when volts >= CREEPAGE_FROM_V


@dataclass(frozen=True)
class KeepAway:
    other: str  # a net name (globs expanded before this)
    mm: float
    source: Source


@dataclass(frozen=True)
class ViaGroup:
    """The via group a layer change carrying a current places (`docs/net-path-physics.md` §4.3): a
    **flow-through line** of `count` barrels at `pitch_mm`, both layers' copper (the landing,
    `landing_mm` wide, the wider of the two layer widths) running over every barrel, the current
    entering at the first and leaving at the last. `count` is the smallest whose worst barrel, by
    `stackup.via_group`'s lumped DC model, carries at most `rating_a`."""

    amps: float
    count: int
    pitch_mm: float
    landing_mm: float
    worst_a: float
    rating_a: float
    drill_mm: float
    temp_rise_c: float

    @property
    def length_mm(self) -> float:
        return round((self.count - 1) * self.pitch_mm, 4)

    def text(self, plating_mm: float) -> str:
        """`3 vias per layer change, flow-through at 1.3 mm pitch, worst barrel 0.750 A of 0.871 (...)`."""
        over = "" if self.worst_a <= self.rating_a + 1e-9 else f"; no line up to {self.count} barrels holds it"
        return (
            f"{self.count} vias per layer change, flow-through at {_g(self.pitch_mm)} mm pitch, worst barrel {self.worst_a:.3f} A of "
            f"{self.rating_a:.3f} (via_barrel {_g(self.drill_mm)}/{_g(plating_mm)} mm at {_g(self.temp_rise_c)} C; lumped DC model{over})"
        )


@dataclass(frozen=True)
class Link:
    """Two consecutive stations of a `Path()` (or the join of a multi-pad end station), with the
    numbers the router and the gates read for the copper between them (§4.1). `width` is per allowed
    layer; clearance, via size and length budget stay the net's."""

    net: str
    a: tuple[str, ...]  # resolved pad ids of the station
    b: tuple[str, ...]
    written: tuple[str, str]  # as the author wrote them
    source: str  # "Path line 62", or "Path line N (station J1.VBUS)" for a join
    key: str  # "C_OUT1.1~R_FB_TOP.1", from the first resolved pad of each station
    width: tuple[tuple[str, Derived], ...]  # per allowed layer
    via: ViaSpec
    group: ViaGroup | None
    layers: tuple[str, ...]
    current: CurrentSpec | None
    keep_away: tuple[KeepAway, ...]
    sense: bool
    join: bool
    line: int = 0
    kind: str | None = None  # the path kind, "feedback" | "sense"
    amps_written: bool = False  # the Path wrote amps=: rule 2 of §3.2 applies to it
    layers_src: str = ""  # where `layers` came from, for a move
    vias_src: str = ""  # where `via.allowed` came from, for a move
    z_se: tuple[tuple[str, tuple[float, Derived]], ...] = ()  # per layer (target, computed z) when the Path writes z_se_ohm=
    reference: tuple[tuple[str, str], ...] = ()  # per layer, the plane under this copper (z_se_ohm= / reference=)

    def width_on(self, layer: str) -> float:
        for lay, d in self.width:
            if lay == layer:
                return d.value
        return max((d.value for _lay, d in self.width), default=0.0)

    def widest(self) -> float:
        return max((d.value for _lay, d in self.width), default=0.0)

    def narrowest(self) -> float:
        return min((d.value for _lay, d in self.width), default=0.0)


@dataclass(frozen=True)
class PathSpec:
    """One `Path()` line, compiled (§4.1)."""

    net: str
    stations: tuple[tuple[str, ...], ...]  # resolved pad ids per station
    written: tuple[str, ...]  # the members as written
    line: int
    kind: str | None
    links: tuple[Link, ...]  # station links, then joins
    length_mm: float | None  # the length budget (Path(length_mm=) or the path kind's): airwire at placement, routed length after
    fielded: bool  # writes a field or a kind
    amps: float | None  # the current it wrote: the number, or the sum of its draws (what the first station supplies)
    draws: tuple[tuple[str, float], ...] = ()  # `amps={station: A}` as written; () for a number
    z_se_ohm: float | None = None  # the impedance target it wrote (N4), or None
    pour: str | None = None  # the pour layer, when the Path's current is on a poured net (PR 8): the pour is its copper
    joints: tuple["Joint", ...] = ()  # PR 8: each station's joint to that pour, sized for its own draw
    length_src: str = ""  # whose budget `length_mm` is: "Path line 9", "Pair line 12", "Bus line 14", "" for a path kind's


@dataclass(frozen=True)
class Joint:
    """A station's joint to its net's pour (`docs/physics-subjects.md` PR 8): on a poured rail the pour is the copper
    between the stations, so what a draw needs is a joint from the station's pad to the pour that carries it. An SMD
    pad's joint is its tap via group: `vias` stackup vias, each `one_amps` at `temp_rise_c` (IPC-2221B eq. 6-2 on
    the barrel, `stackup.via_amps`); a through-hole pad's own barrel reaches the pour, and the gate rates that."""

    station: str  # as written: "J_LOAD.1"
    pads: tuple[str, ...]  # its pad ids
    amps: float  # what it draws, or for the first station what it supplies (the sum)
    supplies: bool  # the first station
    vias: int  # stackup vias an SMD pad of this station needs: ceil(amps / one_amps), at least 1; 0 for a barrel
    via: tuple[float, float]  # (diameter, drill)
    one_amps: float  # one stackup via's rating, or the barrel's when `barrel_mm` is set
    temp_rise_c: float
    barrel_mm: float | None = None  # every pad of the station is through-hole: its own barrel (this drill) joins the pour
    lines: tuple[int, ...] = ()  # the Path lines whose currents meet at this station, when more than one (their sum)
    verb: str = ""  # "supplies"/"draws" from `supplies`; "carries" when the Paths meeting here supply and draw
    # Round 3 (refute-current f1c): what each pad carries when Paths name overlapping pad sets of one station (a pin
    # and one of its pads); empty means `amps` split evenly over `pads`. `paths.joint_pad_amps` reads the worst pad.
    pad_amps: tuple[tuple[str, float], ...] = ()


@dataclass(frozen=True)
class LoopSpec:
    """One `Loop()` line, compiled (`docs/net-path-physics.md` §6): a check on the area its pads
    enclose, closed from the last back to the first; it lays no copper."""

    pads: tuple[str, ...]  # one pad id per station, in the order written
    written: tuple[str, ...]
    line: int
    budget: Derived  # mm2: Loop(mm2=) or the pcbc default
    steps: tuple[str, ...]  # per step: "net <name>" or "part <ref>"
    parts: tuple[str, ...]  # the refs a loop names (caps and ICs): the guessed loops skip them


@dataclass(frozen=True)
class Constraint:
    net: str  # one per real net; globs expanded, sorted
    kind: str  # preset name (aliases resolved: digital/default -> generic); "pair" for a bare Pair()
    class_name: str
    line: int  # board.py line of the NetReq (0 when synthesised from Pair/Bus alone)
    width_mm: Derived
    clearance_mm: Derived  # the class clearance (kind number or voltage row, whichever is larger)
    lane_clearance_mm: float  # the kind number alone: what fanout lanes are sized from
    via: ViaSpec
    layers: tuple[str, ...]
    reference: str | None  # reference plane layer of a pair (a single-ended impedance is a Path's link's, PR 5)
    pair: PairSpec | None
    group: GroupSpec | None
    airwire_max_mm: float | None  # today's max_mm: a placement budget, never a KiCad rule
    length_max_mm: Derived | None  # routed length -> KiCad length (max)
    keep_away: tuple[KeepAway, ...]
    spacing_w: int  # 3 or 5; printed in R1, a router cost in R3
    voltage: VoltageSpec | None
    loop_mm2: Derived | None  # switch_node hot-loop budget, or the power decap-loop budget
    rise_ps: float | None
    """The fastest edge this net carries, in picoseconds, exactly as `NetReq(rise_ps=)` wrote it.

    **Carried, never derived, and never defaulted.** It is the one number in this dataclass with no
    `Derived` around it and no fallback behind it, and both are deliberate: an edge rate is a fact
    about the parts a board is built from — what an ESP32's USB PHY or a buck's gate driver actually
    does — the way `Thermal(watts=)` is a fact about what a pad must move. `docs/stitch-plan.md`
    section 8 item 3 refuses `DEFAULT_RISE_PS` and the four per-kind guesses that came with it, and
    the refusal holds here: `PRESETS` sets no `rise_ps`, `_numbers` reads only what the line wrote,
    and a board that says nothing has `None` on every net.

    Its one consumer is the plane-stitch pitch (`patterns.stitch.stitch_pitch`), which needs a
    wavelength and therefore a frequency. `ConstraintSet.fastest_edge` folds this over the whole
    board, because a cavity between two pours is shared by every net that crosses it and the pitch
    that suppresses its resonance is set by the **fastest** edge in it, not by any one net's.
    """
    guard_stitch_mm: float | None
    guard_ground: str | None
    """The net a `Guard()` shields this one **with**, or None when no `Guard()` names it.

    `Guard(net, *, stitch_mm=2.5, ground="GND")` has carried `ground=` since R1 and, measured
    2026-09-21, it never reached the router: `_compile` stored `stitch_mm` alone, so a board that
    said `Guard("AIN0", ground="VSS")` compiled to a guard pcbc would have written in `GND`
    (`docs/stitch-plan.md` section 2(r)). Two halves of one statement, one of them silently dropped,
    is the failure mode the ConstraintSet exists to prevent — every number the router reads is
    compiled once, here — so the second half is stored beside the first.

    It is a separate field rather than a default read at the pattern, because `"GND"` is
    `language.Guard`'s default and not the compiler's fact: a board with one `Ground("VSS")` and no
    `GND` at all would have a guard carrier reaching for a net that does not exist, and the refusal
    for that is made here where the netlist is (`_compile`), not in a pattern that has only a scene.
    """
    isolation_side: str | None  # Region name
    autoroute: bool | str  # `diff_pair` marks a pair (routed as one object); True otherwise (PR 7: no NetReq sets it)
    soft: tuple[str, ...]  # rule kinds written as warnings in R1: subset of SOFT_RULES
    notes: tuple[str, ...]  # honest caveats, e.g. the 2-layer USB line
    req_index: int = -1  # index into Design.netreqs; -1 when synthesised from Pair/Bus alone
    paths: tuple["PathSpec", ...] = ()
    """Every `Path()` on this net, in board order (§4.1)."""
    off_path: tuple[str, ...] = ()
    """The net's pads no `Path()` names, as pad ids, when the net has a path (§3.1)."""

    def width_on(self, layer: str) -> float:
        """The width of the net's own (off-path) copper on `layer`: one number on every layer, because a net
        carries no current of its own (`docs/physics-subjects.md` PR 3); a path's link has its own."""
        return self.width_mm.value

    def fielded(self) -> bool:
        """Does a path of this net carry a field or a kind? Only then do role owners, the junction
        rules and the off-path report apply (§4.2)."""
        return any(p.fielded for p in self.paths)

    def structural(self) -> bool:
        """Is this net's copper structural — a pair member, a sensitive net kind (`SENSITIVE_KINDS`), or a net whose one
        Path is the whole node and may not change layer (`vias=False`) or has one layer? Those route first and the
        relaxer never pulls them (`route_native.constrained_nets`, `fanout._excluded`). Since docs/physics-subjects.md
        PR 5 a net writes no `vias=`/`layers=`; any other path's constraint rides on its own links."""
        if self.pair is not None or self.autoroute == "diff_pair" or self.kind in SENSITIVE_KINDS:
            return True
        whole = self.whole_node_path()
        return whole is not None and any((not lk.via.allowed) or len(lk.layers) == 1 for lk in whole.links)

    def whole_node_path(self) -> "PathSpec | None":
        """The one Path that names every pad of this net, when it is the net's only Path: that Path's copper
        is the whole node, so a KiCad rule selected by net checks exactly it (`docs/physics-subjects.md`
        KD 12). None otherwise."""
        if len(self.paths) == 1 and not self.off_path and self.paths[0].fielded:
            return self.paths[0]
        return None


@dataclass(frozen=True)
class RuleArea:  # a named KiCad rule-area zone; compile computes, apply renders
    name: str  # "ISO_primary_secondary"
    box: tuple[float, float, float, float]
    layers: tuple[str, ...]  # ("F&B.Cu", "In1.Cu", "In2.Cu")
    disallow: tuple[str, ...]  # ("track", "via", "zone")
    reason: str


@dataclass(frozen=True)
class IsolationSpec:
    """The numbers of one `Isolation` (C.8), for dru.py and the placement check: the sides' nets,
    the clearance and creepage the barrier needs, and the gap the two Regions must keep."""

    req: IsolationReq
    nets_a: tuple[str, ...]
    nets_b: tuple[str, ...]
    clearance_mm: Derived  # max(IPC-2221B B2, IEC 60664-1 F.2 impulse clearance)
    creepage_mm: Derived  # IEC 60664-1 F.5 (reinforced doubles); satisfied by the slot when slot=True
    gap_min_mm: float  # what the Regions must keep apart along their separating axis
    axis: str  # "x" | "y"
    across_nets: tuple[str, ...] = ()  # nets an `across=` part carries: its own pad pitch is its rating


@dataclass(frozen=True)
class BridgeSpec:
    """One `Bridge()` compiled: the two grounds, the part that ties them, and the two pads it ties.

    There is no number in it, and that is the point — every other spec in this file exists because a
    declaration had to become millimetres, and a bridge becomes a **part the board already places**
    (`docs/stitch-plan.md` §8 item 4). What compiling it buys is the resolution: `at="R11"` turned
    into `("R11.1", "R11.2")` with the a-side pad first, so `route_verify.bridge_ties` asks the
    copper about pads rather than about a reference, and so a tie whose pads are not one per net is
    refused by `circuit.check_design` before any copper exists.

    `pads` is empty when `at` names no instance on this board, or names one whose pads do not land
    one per net. The refusal for that is `check_design`'s, not this file's: it is a fact about the
    netlist, and it reads better beside the instance it names.
    """

    req: BridgeReq
    a: str
    b: str
    kind: str
    at: tuple[str, ...]
    tie_ref: str  # "R11", or "" for kind="off_board" and for an `at` that resolved to nothing
    pads: tuple[str, ...]  # ("R11.1", "R11.2"), a-side first; () when it did not resolve
    why: str


@dataclass(frozen=True)
class ThermalSpec:
    """One `Thermal()` compiled: the land it names, the heat it has to move, and the count that moves it.

    Everything here is derivable from `board.py` and the stackup with no PCB file — the barrel's K/W
    is `stackup.via_theta_c_per_w` over `board_mm`, the drill and the plating; the count is
    `vias_for_theta`; the net is the netlist's. What is **not** here is the pitch and the sites,
    because both are questions about the board's own geometry: the pitch has to clear the antipads a
    foreign pour cuts (`patterns.stitch.thermal_pitch`) and a site has to be inside a pad primitive
    this file has never read. So this spec is the **budget** and the pattern is the **placement**,
    which is the same split `ViaSpec.per_change` and `patterns.stitch.specs` already have for the
    parallel carrier.

    `planes` is the pours this array's barrels will cross that are **not** its own net's — the
    foreign planes `across_planes=` is consent for. It is computed here rather than in the pattern
    because it is a fact about `Board(planes=)` and the stackup, and because the refusal for
    withholding that consent has to arrive in `pcbc check`, before a millimetre of copper.
    """

    req: ThermalReq
    pad: str  # "U1.49" as the author wrote it
    ref: str  # "U1"
    nums: tuple[str, ...]  # the pad numbers it resolves to: ("49",)
    net: str
    watts: float
    rise_c: float
    across_planes: bool
    fill: bool
    via: tuple[float, float]  # (diameter, drill) — the stackup's standard via, `BRANCH_REASONS`' own
    theta_via: Derived  # one barrel, K/W
    need: int  # how many barrels the budget asks for
    plane: str  # the layer this pad's net is poured on
    planes: tuple[tuple[str, str], ...]  # the foreign pours a through barrel crosses

    @property
    def ids(self) -> tuple[str, ...]:
        """The pad ids a scene knows this land by: `("U1.49",)`. `pads.PadGeom.id`'s spelling."""
        return tuple(f"{self.ref}.{num}" for num in self.nums)

    def theta_array(self) -> float:
        """`n` barrels in parallel, K/W, 2 dp — what the array the budget asked for achieves."""
        return round(self.theta_via.value / max(self.need, 1), 2)

    def rise_of(self, got: int) -> float:
        """The copper rise `got` barrels leave at `watts`, C, 2 dp. `got = 0` is the pad alone, which
        this model has nothing to say about, so it reports the budget being missed by all of it."""
        if got <= 0:
            return round(self.theta_via.value * self.watts, 2)
        return round((self.theta_via.value / got) * self.watts, 2)

    def to_dict(self) -> dict:
        return {
            "pad": self.pad,
            "net": self.net,
            "watts": self.watts,
            "rise_c": self.rise_c,
            "across_planes": self.across_planes,
            "fill": self.fill,
            "via": list(self.via),
            "theta_via": self.theta_via.value,
            "need": self.need,
            "plane": self.plane,
            "planes": [list(p) for p in self.planes],
        }

    def line(self) -> str:
        """The report line: the budget, its arithmetic, and what the array it asks for achieves."""
        return (
            f"Thermal {self.pad}: {self.need} x {self.via[0]:g}/{self.via[1]:g} via on {self.net} "
            f"({self.theta_via.line()}; {self.watts:g} W within {self.rise_c:g} C wants "
            f"{self.theta_array():g} K/W, so {self.rise_of(self.need):g} C) [Thermal line {self.req.line}]"
        )


def _thermal_refusals(design: Design, stack: Stackup, pours: tuple[tuple[str, str], ...], refusals: list[str]) -> tuple[ThermalSpec, ...]:
    """Every `Thermal()` compiled, or a refusal naming the edit — before any copper exists.

    Four of the five refusals `docs/stitch-plan.md` section 3 lists are here, and one is not, because
    one of them is a question about **geometry** and this file has never opened a footprint: "the pad
    carries no `pad_prop_heatsink` and is narrower than `pitch + dia`" is
    `patterns.stitch._land_refusal`'s, beside the primitives it measures. Splitting them that way is
    the same split `Bridge()` already has — the netlist facts here, the instance facts in
    `circuit.check_design` — and the rule behind all three is that a refusal lives where its
    measurement lives.
    """
    from .fab import passive_refs

    from .paths import station_pads

    planes_declared = bool(design.board.planes) if design.board is not None else False
    out: list[ThermalSpec] = []
    seen: dict[str, int] = {}
    # One land, one array, keyed on the pads the line resolves to: "U1.GND" (a pin of 22 pads) and
    # "U1.49" (one of them) name the same copper, and two arrays on it are two answers to one question.
    seen_pads: dict[str, tuple[int, str]] = {}
    for th in design.thermals:
        who = f'Thermal("{th.pad}") line {th.line}'
        ref, _, pin = th.pad.partition(".")
        if not any(i.ref == ref for i in design.instances):
            refusals.append(f"{who}: no part {ref}")
            continue
        net = _refpin_net(design, ref, pin)
        if net is None:
            refusals.append(f"{who}: {ref} has no pad or pin {pin!r}")
            continue
        if th.pad in seen:
            refusals.append(f"{who}: {th.pad} already has a Thermal() on line {seen[th.pad]}; one land, one array")
            continue
        pads = station_pads(design, net, th.pad)
        if len(pads) > 1:
            # Round 3 (refute-onehome, bt_gnd035): "U1.GND" is 22 pads, 21 of them separate lands around the module's
            # edge. The array was budgeted for "one land", the stitch pattern laid barrels in edge pads and in no pad,
            # and the build's own gate called it a pcbc bug. An array sits under one land: one pad number (which may
            # be several primitives, as U1.49's nine blocks are).
            shown = sorted((x.split(".", 1)[1] for x in pads), key=lambda n: (len(n), n))
            listing = ", ".join(shown) if len(shown) <= 6 else ", ".join(shown[:6]) + ", ..."
            refusals.append(f"{who}: {th.pad} is {len(pads)} pads of {ref} ({listing}); a thermal array is under one land, and a land is one pad")
            continue
        shared = [p for p in pads if p in seen_pads]
        if shared:
            line, written = seen_pads[shared[0]]
            refusals.append(
                f"{who}: {th.pad} is pad{'s' if len(pads) != 1 else ''} {', '.join(pads)}, and "
                f"{', '.join(shared)} {'is' if len(shared) == 1 else 'are'} already the land of "
                f'Thermal("{written}") on line {line}; one land, one array'
            )
            continue
        seen[th.pad] = th.line
        for p in pads:
            seen_pads[p] = (th.line, th.pad)
        # R-M4, and it is the one refusal in this statement that no amount of fab money buys off: a
        # via inside a two-terminal passive's pad wicks the joint whatever the barrel is filled with,
        # and `fab.via_in_pad_blockers` refuses the board for it. Refused here so the author reads it
        # from `pcbc check` instead of from a fab gate three stages later, and `via_in_pad_blockers`
        # keeps a zero diff — it must stay the judge, not become a list this statement edits.
        if ref in passive_refs(design):
            refusals.append(
                f"{who}: {ref} is a two-terminal passive, and a via inside a passive's pad wicks the joint "
                f"whatever the fab fills it with (fab.via_in_pad_blockers refuses the board for it)"
            )
            continue
        # The array's whole job is to reach a plane. Without one the vias land in nothing: on a
        # two-layer board whose GND is not a power net the route stage writes no back pour at all
        # (`route_scene.plane_targets`), and sixteen stitch vias would go into a pour never written.
        plane = next((lay for pnet, lay in pours if pnet == net), "")
        if not plane:
            refusals.append(
                f"{who}: {net} has no plane or pour to reach, so an array under {th.pad} would be "
                f"{th.watts:g} W of vias into bare laminate"
                + (
                    f" (Board(planes=) pours no {net} plane)"
                    if (stack.layers > 2 or planes_declared)
                    else f" (a two-layer board pours B.Cu only when GND is a power net, route_scene.plane_targets)"
                )
            )
            continue
        # A barrel that is not plugged is a hole under a reflowed pad. `Stackup.via_tenting` closes
        # the mask over it and leaves the tube open underneath, which is the distinction `via_fill`
        # exists to carry.
        if stack.via_fill == "none":
            refusals.append(
                f'{who}: stackup "{stack.name}" does not fill its vias, and tenting is a mask dam rather '
                f"than a plug — solder wicks down an open barrel and starves the joint"
            )
            continue
        if not (stack.via_fill_min_drill - 1e-9 <= stack.via_drill <= stack.via_fill_max_drill + 1e-9):
            refusals.append(
                f'{who}: stackup "{stack.name}" fills {stack.via_fill_min_drill:g} to {stack.via_fill_max_drill:g} mm '
                f"and its standard via is {stack.via_drill:g} mm, so these barrels cannot be plugged"
            )
            continue
        foreign = tuple((pnet, lay) for pnet, lay in pours if pnet != net)
        if foreign and not th.across_planes:
            anti = ", ".join(f"{pnet} on {lay}" for pnet, lay in foreign)
            refusals.append(
                f"{who}: every via here is a through via, so {vias_for_theta(stack.board_mm, stack.via_drill, th.watts, th.rise_c, stack.via_plating_mm)} "
                f"of them punch {vias_for_theta(stack.board_mm, stack.via_drill, th.watts, th.rise_c, stack.via_plating_mm)} antipads in {anti}, "
                f"which this net does not join. That is finding 10 at nine times the scale — one row of taps at a "
                f"footprint's own pitch already slotted a plane and turned a 1.80 mm path across it into 8.09 mm; "
                f"Thermal() gives no across_planes=True"
            )
            continue
        theta = via_theta_c_per_w(stack.board_mm, stack.via_drill, stack.via_plating_mm)
        need = vias_for_theta(stack.board_mm, stack.via_drill, th.watts, th.rise_c, stack.via_plating_mm)
        nums = _pad_nums(design, ref, pin)
        out.append(
            ThermalSpec(
                req=th,
                pad=th.pad,
                ref=ref,
                nums=nums,
                net=net,
                watts=th.watts,
                rise_c=th.rise_c,
                across_planes=th.across_planes,
                fill=th.fill,
                via=(stack.via_diameter, stack.via_drill),
                theta_via=Derived(
                    theta,
                    "K/W",
                    Source(
                        "via_theta",
                        f"{stack.name} {stack.board_mm:g} mm board, {stack.via_drill:g} mm drill, {stack.via_plating_mm:g} mm plating",
                        f"L/(k*A) on the plating annulus, k_cu {385:g} W/m/K",
                    ),
                ),
                need=need,
                plane=plane,
                planes=foreign,
            )
        )
    return tuple(out)


def _pad_nums(design: Design, ref: str, pin: str) -> tuple[str, ...]:
    """`"49"` or `"GND"` as the pad numbers it names on this part — `route_verify._station_pads`'
    resolution, asked of the design instead of of a routed file so it is available at compile."""
    for inst in design.instances:
        if inst.ref != ref:
            continue
        if pin in inst.part.pins:
            return tuple(inst.part.pins[pin].pads)
        for _pname, p in inst.part.pins.items():
            if pin in p.pads:
                return (pin,)
        return ()
    return ()


# --- R-Z4's C, which has never existed --------------------------------------------------------
#
# `docs/router-plan.md` tagged R-Z4 **R, V** — route it, verify it — until this slice.
# `docs/stitch-plan.md` S5 says the tag was short by one: before any copper exists there is a **C**, a
# statement the *compiler* can make about what a layer change would do to a return current, and on
# these boards it is the valuable half. `route_verify.return_vias` is the V and it reads a finished
# board; everything below is the C and it reads `board.py`. The doc now reads **C, R, V**.
#
# **This slice places zero copper and that is the finding, not a shortfall** (`docs/stitch-plan.md`
# §8 item 2). Measured 2026-09-21, the population of return vias on all five boards is **empty for a
# structural reason no routing improvement fixes**: on node a layer change changes the reference
# *net* (`GND` on In1.Cu -> `3V3` on In2.Cu) and a via joins one net to itself, so no via anywhere
# carries that current; on c3_usb it **loses** the reference entirely, because B.Cu is the pour's own
# layer and a two-layer board has no second plane to reach. Shipping a placer against that would mean
# widening a tolerance until something landed, or dropping a via that connects nothing while the
# tool's own report blessed it. So what ships is the sentence, and the sentence is worth more than
# the via: it tells an AI, **before a millimetre of copper is routed**, that these two pairs must not
# change layers. Nothing said that today, and both boards ship with the pair allowed on both layers.


RETURN_VERDICTS = ("lost", "net_change", "kept", "pinned")
"""What a layer change does to a net's return current, worst first — and "worst" is an order, not a
taste.

- `lost` — one of the two layers has **no** reference at all. The return has nothing to follow onto
  and the impedance is uncontrolled on that side too, so it is the larger statement of the two
  failures and it wins a tie.
- `net_change` — both layers have a reference and the two are **different nets**. The return exists
  and no via can carry it there.
- `kept` — every layer this net may use is referenced to one net. A via beside the signal's would
  carry the return across; this is the only verdict under which a return-via *placer* would have
  anything to do, and **no net on any of the five boards reaches it**.
- `pinned` — the net cannot change layer at all: one layer, or `via.allowed` False. Nothing to
  classify, which is a different answer from "nothing went wrong" and is printed as one.

Two of the four are spelled exactly as `route_verify.return_vias` spells them, deliberately: the C
predicts the V or it is not worth printing, and `tests/test_route_verify_stitch.py` holds the two
vocabularies together on every classified via of every board.
"""


@dataclass(frozen=True)
class ReturnRule:
    """What a via on one referenced net would do to its return current — computed from `board.py`.

    Pure in the `ConstraintSet`: `stackup.reference_of` on each layer of `Constraint.layers`, against
    the planes the board will have. No PCB file, no copper, no distance — which is the point, because
    measured 2026-09-21 **not one of the eleven vias `return_vias` classifies is a distance question
    either**. Both boards are settled by the stackup, and the stackup is compiled.
    """

    net: str
    layers: tuple[str, ...]  # the layers this net may use, `Constraint.layers`
    refs: tuple[tuple[str, tuple[str, str] | None], ...]  # (layer, (reference layer, reference net) | None)
    change: tuple[str, str] | None  # the layer pair the verdict is about; None when `pinned`
    verdict: str  # one of RETURN_VERDICTS
    why: str
    move: str  # "" always since docs/direction.md §7b: the verdict and why are the facts; no edit is proposed

    def to_dict(self) -> dict:
        return {
            "net": self.net,
            "layers": list(self.layers),
            "refs": [[lay, list(r) if r else None] for lay, r in self.refs],
            "change": list(self.change) if self.change else None,
            "verdict": self.verdict,
            "why": self.why,
            "move": self.move,
        }

    def line(self) -> str:
        """The report line, in `moves.move_line`'s one sentence shape so a compile finding and a
        routing failure read the same (E.2) — `<net>: <what> cannot reach <goal>. In the way: <...>.
        <the board.py edit>`. `kept` and `pinned` have no failure to report and say so plainly."""
        from .moves import move_line

        if self.verdict in ("kept", "pinned"):
            return f"{self.net}: returns {self.verdict}, {self.why}"
        assert self.change is not None
        a, b = self.change
        goal = next((f"{r[1]} on {r[0]}" for _lay, r in self.refs if r), "a reference plane")
        return move_line(self.net, f"the return current crossing {a}->{b}", goal, f"{self.why} ({self.verdict})")




def return_rule(stack: Stackup, planes: tuple[tuple[str, str], ...], c: Constraint) -> ReturnRule | None:
    """Classify one constraint's layer change, or None when the compiler gave it no `reference`.

    The population this watches is `route_verify.referenced_nets`' — `wants_reference` is set only
    for `kind == "usb_hs"` or a declared `z_se_ohm` (below), so measured 2026-09-21 it is
    `USB_DN`/`USB_DP` on c3_usb and node and **empty on blinky, buck and ds2**. `cs.by_net("GND")
    .reference` is `None` on all five, which is also why `route_verify`'s D.1 item 6 needs no change
    for any of this: a return via's net *is* the reference net, so item 6 never had one to block.

    Every referenced net on all five boards is allowed on exactly **two** layers (`F.Cu`, `B.Cu`), so
    there is exactly one pair per net and `RETURN_VERDICTS`' ordering is never exercised here. It is
    written down so a board with an inner signal layer gets a defined, deterministic answer rather
    than the first one the loop happened to reach.
    """
    if not c.reference:
        return None
    refs = tuple((lay, stack.reference_of(lay, planes)) for lay in c.layers)
    by_layer = dict(refs)
    if len(c.layers) < 2 or not c.via.allowed:
        why = (
            f"{c.net} may only use {c.layers[0] if c.layers else 'no layer'}, so no via changes its reference"
            if len(c.layers) < 2
            else f"vias are refused on {c.net} ({', '.join(c.layers)} are allowed but nothing may cross between them), so no via changes its reference"
        )
        return ReturnRule(c.net, tuple(c.layers), refs, None, "pinned", why, "")

    order = {"lost": 0, "net_change": 1, "kept": 2}
    best: tuple[int, tuple[str, str], str] | None = None
    for i, a in enumerate(c.layers):
        for b in c.layers[i + 1 :]:
            fr, to = by_layer[a], by_layer[b]
            if fr is None or to is None:
                verdict = "lost"
            elif fr[1] != to[1]:
                verdict = "net_change"
            else:
                verdict = "kept"
            if best is None or order[verdict] < order[best[2]]:
                best = (order[verdict], (a, b), verdict)
    assert best is not None
    _rank, (a, b), verdict = best
    fr, to = by_layer[a], by_layer[b]

    if verdict == "kept":
        assert fr is not None
        why = f"{a} and {b} are both referenced to {fr[1]}, so a {fr[1]} via beside the signal's carries the return across"
        return ReturnRule(c.net, tuple(c.layers), refs, (a, b), verdict, why, "")

    if verdict == "net_change":
        assert fr is not None and to is not None
        why = (
            f"{a} is referenced to {fr[1]} on {fr[0]} and {b} to {to[1]} on {to[0]}, the reference changes net across the via, "
            f"and a via joins one net to itself, so no return via anywhere carries this current"
        )
        return ReturnRule(c.net, tuple(c.layers), refs, (a, b), verdict, why, "")

    # lost
    gone, other, kept = (a, b, to) if fr is None else (b, a, fr)
    if kept is None:
        why = f"neither {a} nor {b} has a plane above or below it, so there is no reference on this board to return to"
    elif kept[0] == gone:
        why = (
            f"{gone} is the {kept[1]} pour's own layer, which cannot be its own reference; {other} was referenced to "
            f"{kept[1]} on {gone} and there is no second plane to reach"
        )
    else:
        why = f"{gone} has no plane above or below it, and the via leaves {other}'s {kept[1]} on {kept[0]} behind"
    return ReturnRule(c.net, tuple(c.layers), refs, (a, b), verdict, why, "")


def return_rules(cs: ConstraintSet) -> tuple[ReturnRule, ...]:
    """One rule per net the compiler gave a `Constraint.reference`, in net order.

    Pure in the set and therefore in `board.py`: `cs.planes` is compiled, `cs.stackup` is compiled,
    and nothing here opens a file. `tests/test_return_rules.py` asserts every verdict on all five
    boards with no `layout/` directory present at all.
    """
    out: list[ReturnRule] = []
    for c in cs.constraints:
        r = return_rule(cs.stackup, cs.planes, c)
        if r is not None:
            out.append(r)
            continue
        # A single-ended impedance is a Path's (docs/physics-subjects.md PR 5, N4/N14): its first link with a
        # reference is classified the same way, over the layers and vias that link may use.
        for p in c.paths:
            lk = next((x for x in p.links if not x.join and x.z_se and x.reference), None)
            if lk is None:
                continue
            proxy = _LinkReturn(net=c.net, reference=lk.reference[0][1], layers=tuple(lk.layers), via=lk.via)
            r = return_rule(cs.stackup, cs.planes, proxy)  # type: ignore[arg-type]
            if r is not None:
                out.append(r)
            break
    return tuple(out)


@dataclass(frozen=True)
class _LinkReturn:
    """What `return_rule` reads of a constraint, for one path link that carries an impedance."""

    net: str
    reference: str
    layers: tuple[str, ...]
    via: ViaSpec


def _return_caveat(rule: ReturnRule, spelling: str, reference_name: str) -> str:
    """The `Constraint.notes` caveat: `Constraint.reference` is **one layer's** plane.

    A separate sentence from `ReturnRule.line()` because it corrects a different thing. The line is
    about the return current; this is about the compiled field. `Constraint.reference` is a single
    layer name taken from `layers[0]` (`_numbers`, `_reference_for`), it is what the `z_se` line
    prints "over" and what R3/R4 will read to pick a rule — and on both boards that carry one the net
    is allowed on a second layer where the answer is different. Nothing said so before this slice.

    `spelling` is how the net is named: the sorted pair for a pair, so both members produce the same
    text and `_report`'s note de-duplication prints it once rather than per member.
    """
    tail = ", ".join(
        f"on {lay} it is {r[1]} on {r[0]}" if r else f"on {lay} there is no reference at all"
        for lay, r in rule.refs[1:]
    )
    return (
        f"{spelling}: Constraint.reference is {reference_name}, and that is {rule.layers[0]}'s alone; {tail} — the field names "
        f"one layer's plane and a via changes which plane this net is over"
    )


@dataclass
class DruRule:  # moved here from compile.py; compile re-exports it
    name: str
    constraint: str
    condition: str
    severity: str = "error"  # KiCad: error | warning | ignore


@dataclass
class CompiledClass:  # moved here from compile.py; compile re-exports it
    name: str
    track_width_mm: float
    clearance_mm: float
    via_diameter_mm: float = 0.45
    via_drill_mm: float = 0.20
    diff_pair_width_mm: float | None = None
    diff_pair_gap_mm: float | None = None
    patterns: list[str] = field(default_factory=list)
    lane_clearance_mm: float | None = None  # the kind's clearance without the voltage row; None reads as clearance_mm


@dataclass(frozen=True)
class ConstraintSet:
    stackup: Stackup
    constraints: tuple[Constraint, ...]  # sorted by net
    classes: tuple[CompiledClass, ...]  # derived, section A.3
    groups: tuple[GroupSpec, ...]
    isolations: tuple[IsolationReq, ...]
    rule_areas: tuple[RuleArea, ...]  # R1: one per Isolation (the corridor between the two Regions)
    canary_net: str | None  # compile._canary_net(design), carried so dru.py needs no Design
    refusals: tuple[str, ...]  # what `pcbc check` fails on (a Path pin off its net, ...)
    lines: tuple[str, ...]  # the report, one number per line, sorted by net
    isolation_specs: tuple[IsolationSpec, ...] = ()  # the numbers behind `isolations`
    bridges: tuple[BridgeSpec, ...] = ()  # one per `Bridge()`; no geometry, see `BridgeSpec`
    thermals: tuple[ThermalSpec, ...] = ()  # one per `Thermal()`; the budget, not the placement
    loops: tuple["LoopSpec", ...] = ()  # one per `Loop()` (§6): a check on named pads
    _index: dict = field(default_factory=dict, init=False, repr=False, compare=False)
    planes: tuple[tuple[str, str], ...] = ()
    """(net, layer) for every pour this board will **have** — not only what `Board(planes=)` says.

    The same question `route_scene.plane_targets` answers off a `CompiledJob`, asked here because a
    return path is a question about the reference **net** and the ConstraintSet had no net-to-layer
    map at all: `Constraint.reference` is a bare layer name, and on two layers it is not even a layer
    (`"B.Cu pour"`). Without this, classifying a layer change needed a PCB file, and the whole value
    of `docs/stitch-plan.md` S5 is that it does not.

    It is `route_scene.plane_targets`' answer: `Board(planes=)` verbatim when the board declares any, on
    every stackup; otherwise, on two layers, the back `GND` pour the route stage writes when `GND` is a
    power net (`tests/test_return_rules.py::test_the_compiled_planes_are_the_route_plans_own`). Until
    postfix A7 a two-layer `Board(planes=)` was not read here while the route stage poured it, so a
    current Path on that pour got no joints and a sense Path on it was not refused.

    **Not `CompiledJob.planes`**, which is `Board(planes=)` verbatim on every stackup and is therefore
    `()` on all four two-layer boards here. The two differ exactly where it matters — the 2L pour — so
    reading the wrong one would silently classify c3_usb's pair as having no reference on either layer
    instead of on one.
    """

    def by_net(self, name: str) -> Constraint | None:
        for c in self.constraints:
            if c.net == name:
                return c
        return None

    # -- paths and links (docs/net-path-physics.md §4.1) ---------------------------------------------

    def _links(self) -> dict:
        """(net, pad, pad) -> Link, both orders, and key -> Link; built once per set."""
        idx = self._index.get("links")
        if idx is None:
            by_pads: dict = {}
            by_key: dict = {}
            for c in self.constraints:
                for p in c.paths:
                    for lk in p.links:
                        by_key.setdefault(lk.key, lk)
                        for x in lk.a:
                            for y in lk.b:
                                if x != y:
                                    by_pads.setdefault((c.net, x, y), lk)
                                    by_pads.setdefault((c.net, y, x), lk)
            idx = (by_pads, by_key)
            self._index["links"] = idx
        return idx

    def link(self, net: str, pad_a: str, pad_b: str) -> Link | None:
        """The link between two pads (pad ids, `U1.4`), when they are consecutive stations of a path or
        two pads of one multi-pad end station (its join); None otherwise."""
        return self._links()[0].get((net, pad_a.split("@")[0], pad_b.split("@")[0]))

    def link_by_key(self, key: str) -> Link | None:
        return self._links()[1].get(key)

    def links_of(self, net: str) -> tuple[Link, ...]:
        c = self.by_net(net)
        return tuple(lk for p in (c.paths if c is not None else ()) for lk in p.links)

    def fielded(self, net: str) -> bool:
        c = self.by_net(net)
        return c is not None and c.fielded()

    def pad_width(self, net: str, pad: str, layer: str | None = None) -> float:
        """The width copper leaving `pad` on `layer` must have: the widest link at the pad, or the
        net's width where an off-path branch may end there."""
        c = self.by_net(net)
        if c is None:
            d = next((k for k in self.classes if k.name == "Default"), None)
            return d.track_width_mm if d is not None else self.stackup.track_min
        own = c.width_on(layer) if layer else c.width_mm.value
        pad = pad.split("@")[0]
        # An off-path branch may end on any pad but a sense path's far stations (§3.2), so the net's own
        # width reaches every other pad.
        far = {x for p in c.paths if p.kind is not None for st in p.stations[1:] for x in st}
        best = own if pad not in far else 0.0
        for lk in self.links_of(net):
            if pad in lk.a or pad in lk.b:
                best = max(best, lk.width_on(layer) if layer else lk.widest())
        return best or own

    def widest(self, net: str) -> float:
        """The widest copper of the net on any layer: its own width and every link's."""
        c = self.by_net(net)
        if c is None:
            return 0.0
        vals = [c.width_mm.value]
        vals += [lk.widest() for lk in self.links_of(net)]
        return max(vals)

    def narrowest(self, net: str) -> float:
        """The narrowest copper of the net on any layer: its own width and every link's."""
        c = self.by_net(net)
        if c is None:
            return 0.0
        vals = [c.width_mm.value]
        vals += [lk.narrowest() for lk in self.links_of(net)]
        return min(vals)

    def fastest_edge(self) -> tuple[str, float] | None:
        """`(net, rise_ps)` for the fastest edge any `NetReq(rise_ps=)` on this board declares, or
        `None` when not one net declares one.

        **A board-level fold, because the thing it is asked about is board-level.** The consumer is
        the plane-stitch pitch (`patterns.stitch.stitch_pitch`), and the cavity between two pours of
        one net is shared by every signal that crosses it: the pitch that keeps a cell's first
        resonance above the knee has to be set by the *fastest* edge in the cavity, not by the edge
        of whichever net happens to be nearest. Taking the minimum `rise_ps` is that sentence as
        arithmetic.

        The tie-break is the net name so the answer is a property of the board and not of the order
        the `NetReq` lines were written in, which is the same rule every other ordered thing in this
        compiler follows.

        `None` is the honest answer and it is a **refusal**, not a default: measured 2026-09-21,
        every one of the five boards returns `None`, because nothing in this repo has ever declared an
        edge rate and `docs/stitch-plan.md` section 8 item 3 refuses to invent one per kind.
        """
        found = [(c.rise_ps, c.net) for c in self.constraints if c.rise_ps is not None]
        if not found:
            return None
        ps, net = min(found, key=lambda r: (r[0], r[1]))
        return (net, float(ps))

    def to_dict(self) -> dict:
        return {
            "stackup": self.stackup.name,
            "constraints": [_to_json(asdict(c)) for c in self.constraints],
            "classes": [asdict(c) for c in self.classes],
            "groups": [asdict(g) for g in self.groups],
            "isolations": [asdict(i) for i in self.isolations],
            "isolation_specs": [_to_json(asdict(i)) for i in self.isolation_specs],
            "bridges": [_to_json(asdict(b)) for b in self.bridges],
            "thermals": [t.to_dict() for t in self.thermals],
            "rule_areas": [asdict(a) for a in self.rule_areas],
            "canary_net": self.canary_net,
            "planes": [list(p) for p in self.planes],
            "loops": [_to_json(asdict(lp)) for lp in self.loops],
            "refusals": list(self.refusals),
            "lines": list(self.lines),
        }


def _to_json(obj):
    """asdict() output with every Derived flattened to {"value", "unit", "formula", "ref", "note"}."""
    if isinstance(obj, dict):
        if set(obj) == {"value", "unit", "source"} and isinstance(obj["source"], dict):
            src = obj["source"]
            return {"value": obj["value"], "unit": obj["unit"], "formula": src["formula"], "ref": src["ref"], "note": src["note"]}
        return {k: _to_json(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_to_json(v) for v in obj]
    return obj


# ---------------------------------------------------------------------------------------------
# D. Presets
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Preset:
    class_name: str
    own: tuple[str, ...]  # the kind's own kwargs, in the order the refusal lists them
    width_mm: float | None  # None: the class floor
    clearance_mm: float | None  # None: the class floor
    via: tuple[float, float] | None  # (diameter, drill); None: the stackup via
    vias_allowed: bool
    vias_max: int | None
    layers: tuple[str, ...]
    airwire_mm: float | None
    spacing_w: int


PRESETS: dict[str, Preset] = {
    # docs/physics-subjects.md PR 5 (§4.1): a net kind keeps only what holds at every pad — the clearance number,
    # the spacing, the span and the loops it generates. No net kind sets a width, a via, a layer or a current:
    # its copper is the class floor at the stackup via on every layer `Board(planes=)` does not give to a plane.
    "generic": Preset("Default", (), None, None, None, True, None, POWER_LAYERS, None, 3),
    "power": Preset("Power", ("volts",), None, 0.20, None, True, None, POWER_LAYERS, None, 3),
    "analog": Preset("Analog", ("keep_clear_of", "keep_clear_mm"), None, 0.20, None, True, None, POWER_LAYERS, 25.0, 5),
    # Decision 2 (owner, 2026-10-01, "ok do 1-4"): switch-node copper may change layer through a via group sized for
    # its current (`paths.make_group`); its copper area stays priced by the route cost and measured
    # (`route_verify.switch_copper`).
    "switch_node": Preset("SwitchNode", (), None, 0.20, None, True, None, POWER_LAYERS, 8.0, 3),
    "clock": Preset("Clock", (), None, 0.20, None, True, None, POWER_LAYERS, None, 5),
    "i2c": Preset("I2C", ("pf_max",), None, 0.20, None, True, None, POWER_LAYERS, None, 3),
}


PAIR_PRESETS: dict[str, Preset] = {
    # docs/physics-subjects.md PR 6 (§4.2): `usb_hs` is a Pair kind. It is also the geometry every Pair is
    # compiled with (edge-coupled microstrip, C.3); a Pair with no kind carries only its own line's numbers.
    "usb_hs": Preset("USB", (), None, None, None, True, 2, POWER_LAYERS, None, 3),
}
BARE_PAIR = Preset("Pair", (), None, None, None, True, None, POWER_LAYERS, None, 3)
"""A Pair with no kind (refutation one-home r1, defect 2): the pair geometry from its own line, vias allowed at the
stackup via with no budget, the 3W spacing every net kind but analog and clock has. Not a kind a line can write."""
BUS_KINDS_MATCH = {"spi": 2.5}
"""Bus kinds and their default skew budget (§4.2): `spi`, matched to its clock within 2.5 mm by default."""
SENSITIVE_KINDS = ("switch_node", "analog")
"""Net kinds whose copper routes first and the relaxer never pulls (decision 2, owner 2026-10-01). Until
docs/physics-subjects.md PR 7 this was read off `Preset.autoroute is False`; the field is gone (it was the old
router's switch), and the routing fact it carried is the net kind's (`Constraint.structural`)."""
NET_KINDS = tuple(sorted(PRESETS))
"""The kinds a NetReq takes (§4.2): what a node is. A Pair kind, a Bus kind and a Path kind are refused on it."""


@dataclass(frozen=True)
class PathPreset:
    """A path kind's defaults (`docs/physics-subjects.md` KD 15): what a branch that carries a measurement
    gets when its Path does not say. pcbc defaults with no published source, carried over unchanged."""

    width_mm: float
    vias_allowed: bool
    layers: tuple[str, ...]
    length_mm: float


PATH_PRESETS: dict[str, PathPreset] = {
    "feedback": PathPreset(0.20, False, ("F.Cu",), 15.0),
    "sense": PathPreset(0.20, False, ("F.Cu",), 25.0),
}

# Preset defaults with no standard behind them (printed "pcbc default") and the ones with a source.
DEFAULT_MATCH_MM = {"clock": 2.0, "usb_hs": 0.5, "spi": 2.5}
MATCH_SOURCE = {"clock": "pcbc default", "usb_hs": "TI usb_layout_basics", "spi": "about 15 ps on FR-4, pcbc default"}
DEFAULT_LOOP_MM2 = {"power": 6.0, "switch_node": 20.0}
LOOP_NOTE = {"power": "decap loop, pcbc default", "switch_node": "hot loop, pcbc default"}
USB_UNCOUPLED_MM = 2.0
USB_Z_DIFF = 90.0
I2C_PF_MAX = 400.0
KEEP_CLEAR_DEFAULT_MM = 3.0
PAD_NECK_MAX_MM = 1.0
"""How far from a pad's edge a path's copper may run narrower than the path, at no more than that pad's own
width across the exit direction (decision 1, owner 2026-10-01, *"ok do 1-4"*; `docs/direction.md` §5b). Source:
IPC-2152/IPC-2221 widths are derived for long conductors, and a short neck between a wide trace and the part's
pad dumps its heat into both; TI's TPS54202 EVM (SLVUAP3, Figure 3-1) leaves SW through a pad-width neck into
vias. The neck must end in full-width copper of that path, a via group sized for the path, or a pour of the
net (`route_verify.pad_necks`). A board that does not route under this limit is reported; the limit is not
raised to make one route."""
CURRENT_FLOOR_STEP_AMPS = 0.2
"""The step of the current floor (`docs/physics-subjects.md` KD 10): a copper that carries `amps=` is at least
0.4 mm at this current or more, 0.25 mm below it. A pcbc default with no source (O7)."""
KEEP_AWAY_KINDS = ("analog",)  # docs/physics-subjects.md PR 7: sense and feedback are Path kinds

_KIND_CLASS = {kind: p.class_name for kind, p in PRESETS.items()}
_KIND_CLASS.update({"digital": "Default", "default": "Default"})


def slug(s: str) -> str:
    return "".join(ch.lower() if ch.isalnum() else "_" for ch in s).strip("_")


def _canary_net(design: Design) -> str | None:
    """The first net (sorted) with two or more pads: it always carries copper on a routed board."""
    count: dict[str, int] = {}
    for inst in design.instances:
        for pname, net in inst.pins.items():
            pin = inst.part.pins.get(pname)
            if pin and net:
                count[net] = count.get(net, 0) + len(pin.pads)
    for net in sorted(count):
        if count[net] >= 2 and "'" not in net:
            return net
    return None


# ---------------------------------------------------------------------------------------------
# Helpers: sources, tables, the design's pads
# ---------------------------------------------------------------------------------------------


def _preset(value: float, unit: str, kind: str, note: str = "") -> Derived:
    return Derived(value, unit, Source("preset", kind, note))


def _override(value: float, unit: str, who: str, line: int, kind: str, default: float | str) -> Derived:
    """A number the board wrote over the preset: `(NetReq line 96; overrides preset analog 25)`."""
    return Derived(value, unit, Source(who, f"line {line}", f"overrides preset {kind} {default}"))


def _g(x: float) -> str:
    return f"{x:g}"


def _ipc2221_row_label(volts: float) -> str:
    prev = -1.0
    for upper, *_ in IPC2221_TABLE_6_1:
        if volts <= upper:
            return f"{_g(prev + 1)}-{_g(upper)}"
        prev = upper
    return "> 500"


def _creepage_row_v(volts: float) -> float:
    for row in IEC60664_TABLE_F5_PD2:
        if volts <= row[0]:
            return row[0]
    return IEC60664_TABLE_F5_PD2[-1][0]


def _has_glob(pattern: str) -> bool:
    return any(ch in pattern for ch in "*?[")


def _closest(name: str, net_names: list[str]) -> str:
    """'; the nearest net name on this board is X' when one of the board's nets is close to what was
    written: a measurement of the names, never a guess at the one that was meant."""
    import difflib

    near = difflib.get_close_matches(name, net_names, n=1, cutoff=0.6)
    return f"; the nearest net name on this board is {near[0]!r}" if near else ""


def _expand(patterns: tuple[str, ...], net_names: list[str]) -> list[str]:
    """Globs expanded against the design's nets (sorted); a literal name is kept as written, and a
    glob that matches nothing is kept too so today's class patterns still carry it."""
    out: list[str] = []
    for p in patterns:
        matches = [n for n in net_names if fnmatch.fnmatchcase(n, p)] if _has_glob(p) else [p]
        for n in matches or [p]:
            if n not in out:
                out.append(n)
    return out


def _pads_by_net(design: Design) -> dict[str, list[tuple[str, str]]]:
    """net -> [(ref, pad number)] in instance order, pads in symbol order."""
    out: dict[str, list[tuple[str, str]]] = {}
    for inst in design.instances:
        for pname, net in inst.pins.items():
            pin = inst.part.pins.get(pname)
            if pin and net:
                for pad in pin.pads:
                    out.setdefault(net, []).append((inst.ref, pad))
    return out


def _refpin_net(design: Design, ref: str, pin: str) -> str | None:
    """The net on REF.PIN: by pin name first, then by pad number (`U1.12` is AVDD)."""
    for inst in design.instances:
        if inst.ref != ref:
            continue
        if pin in inst.pins:
            return inst.pins[pin]
        for pname, p in inst.part.pins.items():
            if pin in p.pads and pname in inst.pins:
                return inst.pins[pname]
        return None
    return None


def canon_reference(stack: Stackup, reference: str) -> str:
    """A written `reference=` as the name the compiler keys planes by. On a two-layer board the plane under
    F.Cu is the B.Cu pour, which the plane list prints as "B.Cu (GND)" and `height_to_reference` names
    "B.Cu pour"; a line that writes the layer it sees, "B.Cu", names that pour (postfix A3: E10 refused
    "B.Cu" while the compiler said F.Cu runs over the B.Cu pour). Round 3 (refute-onehome): the same holds for a
    declared F.Cu pour, the plane over B.Cu: "F.Cu" names "F.Cu pour"."""
    return f"{reference} pour" if stack.layers <= 2 and reference in ("F.Cu", "B.Cu") else reference


def is_poured_reference(ref: str, poured) -> bool:
    """`ref` (canonical, `canon_reference`) names a pour of `poured` (layers): a layer, or "B.Cu pour"/"F.Cu pour"."""
    return ref in poured or (ref.endswith(" pour") and ref[: -len(" pour")] in poured)


def _reference_for(stack: Stackup, layer: str, planes: tuple[tuple[str, str], ...]) -> tuple[float, str, str] | None:
    """(height, reference layer, printed name) for a track on `layer`, read off `planes`, the pours the
    board **will have** (`ConstraintSet.planes`): the adjacent plane ("In1.Cu (GND)"), or on a 2-layer
    board the B.Cu GND pour when the route stage writes one (GND compiled to kind power). A 2-layer
    board with no such pour has no plane under F.Cu, and a width sized as if it had one carries less
    than its label says (refutation round 2: 1.999 A on the gate, 1.665 A by the repo's own model)."""
    found = stack.height_to_reference(layer, planes, two_layer_pour=False)
    if found is None:
        return None
    h, ref = found
    if stack.layers <= 2:
        # Round 3 (refute-onehome): on two layers the plane facing a layer is a pour on the other outer layer,
        # either one (a declared `planes=[("GND", "F.Cu")]` is the plane over B.Cu); it is named "<layer> pour".
        # This read only a B.Cu pour, so a B.Cu track under a declared F.Cu pour had no reference plane.
        return h, f"{ref} pour", f"the {ref} pour"
    net = next(n for n, lay in planes if lay == ref)
    return h, ref, f"{ref} ({net})"


def _given(req: NetReqSpec) -> list[str]:
    """The kwargs the NetReq line wrote, in signature order."""
    out: list[str] = []
    for name in (
        "volts", "max_mm", "keep_clear_of", "keep_clear_mm", "pf_max", "rise_ps", "class_name",
    ):
        v = getattr(req, name)
        if name == "keep_clear_of":
            if v:
                out.append(name)
        elif v is not None:
            out.append(name)
    return out


def _and_list(xs) -> str:
    """"F.Cu", "F.Cu and B.Cu", "F.Cu, In1.Cu and B.Cu": a list as a sentence names it."""
    xs = list(xs)
    return xs[0] if len(xs) == 1 else ", ".join(xs[:-1]) + " and " + xs[-1]


def _net_kinds_text() -> str:
    """The kinds a NetReq takes, in §4.2's order, as a sentence's list."""
    order = [k for k in ("generic", "power", "switch_node", "analog", "clock", "i2c") if k in PRESETS]
    return ", ".join(order[:-1]) + " or " + order[-1]


def _kwargs_of(kind: str) -> tuple[str, ...]:
    own = (PRESETS.get(kind) or PAIR_PRESETS[kind]).own
    return own + tuple(k for k in COMMON_KWARGS if k not in own)


# ---------------------------------------------------------------------------------------------
# compile_constraints
# ---------------------------------------------------------------------------------------------


@dataclass
class _Numbers:
    """What one NetReq (or bare Pair) compiles to, shared by every net it names."""

    kind: str
    class_base: str
    line: int
    width: Derived
    clearance: Derived
    lane_clearance: float
    via: ViaSpec
    layers: tuple[str, ...]
    reference: str | None
    reference_name: str | None
    pair_geometry: tuple[Derived, Derived, float, Derived, float, Derived, Derived, bool] | None  # (w, g, z_target, z, tol, skew, uncoupled, controlled)
    airwire: float | None
    airwire_src: Source | None
    length: Derived | None
    length_none: bool  # print "length none (give length_mm=)"
    keep_away: tuple[KeepAway, ...]
    spacing: int
    voltage: VoltageSpec | None
    loop: Derived | None
    rise_ps: float | None
    autoroute: bool | str
    soft: tuple[str, ...]
    notes: tuple[str, ...]
    width_line: str | None  # the pair line of the report (replaces "width ..."), without the net prefix
    via_src: str  # "stackup jlcpcb_2l_1oz" | "preset power"
    layers_src: str  # "preset analog" | "NetReq line 96" | "Pair line 262"
    pair_of: dict[str, str]  # net -> partner
    pair_length: float | None = None  # Pair(length_mm=)


def compile_constraints(design: Design) -> ConstraintSet:
    """Pure: Design in, numbers out; never reads a board file."""
    if design.board is None:
        raise ValueError("design has no Board()")
    board = design.board
    stack = get_stackup(board.stackup)
    planes = tuple(board.planes)
    net_names = sorted(design.nets)
    pads_by_net = _pads_by_net(design)
    refusals: list[str] = []
    notes_lines: list[str] = []  # class collision lines and other whole-board notes
    has_switch_node = any(KIND_ALIASES.get(r.kind, r.kind) == "switch_node" for r in design.netreqs)

    floor_w = max(0.16, stack.track_min)
    hf = hole_floor(stack)
    floor_clear = max(stack.clearance_min, hf)
    class_clear = max(0.16, floor_clear)
    if hf >= 0.16:
        floor_clear_src = Source("class_floor", f"hole_clearance {_g(stack.hole_clearance)} - ring {_g(stack.annular_min)} + 0.005")
    else:
        floor_clear_src = Source("class_floor", f"0.16 over hole_clearance {_g(stack.hole_clearance)} - ring {_g(stack.annular_min)} + 0.005 = {_g(hf)}")
    floor_w_src = Source("class_floor", f"max(0.16, track_min {_g(stack.track_min)})")

    # -- who names which net -----------------------------------------------------------------
    owner: dict[str, tuple[str, int]] = {}  # net -> ("NetReq"|"Pair", line)
    bare_pairs: list[PairReq] = []
    req_nets: list[list[str]] = [_expand(r.nets, net_names) for r in design.netreqs]
    known = set(net_names)
    for req, got in zip(design.netreqs, req_nets, strict=True):
        for pat in req.nets:
            hit = [n for n in got if n in known and (fnmatch.fnmatchcase(n, pat) if _has_glob(pat) else n == pat)]
            if not hit:
                near = _closest(pat, net_names)
                refusals.append(
                    f'NetReq("{req.nets[0]}") line {req.line}: {"no net matches " if _has_glob(pat) else "no net "}{pat!r}{near}'
                )
    for pr in design.pairs:
        for name in (pr.p, pr.n):
            if name not in known:
                refusals.append(f'Pair line {pr.line}: no net {name!r}{_closest(name, net_names)}')
    for bus_req in design.buses:
        for name in bus_req.nets:
            if name not in known and not _has_glob(name):
                refusals.append(f'Bus line {bus_req.line}: no net {name!r}{_closest(name, net_names)}')
    owner_idx: dict[str, int] = {}  # net -> the index of the NetReq that names it
    for i, req in enumerate(design.netreqs):
        for n in req_nets[i]:
            if n in owner:
                who, line = owner[n]
                # No Pair is involved when two NetReqs collide: pointing at a Pair's overrides
                # sent the AI somewhere it could not go.
                how = "(every net of one NetReq shares every number)" if who == "NetReq" else "(a net is named by one NetReq or one Pair)"
                refusals.append(f"{n}: {who} line {line} and NetReq line {req.line} both name it, and a net is named once {how}")
            else:
                owner[n] = ("NetReq", req.line)
                owner_idx[n] = i
    # Refutation one-home r1 (defect 1): what holds at every pad of a node (volts=, max_mm=, rise_ps=, keep_clear_of=,
    # class_name=, docs/physics-subjects.md §2.1) is a NetReq's, a pair net's too. A NetReq that names both nets of a
    # Pair, and nothing else, gives them those node facts; the Pair keeps its own numbers (impedance, gap, skew,
    # layers, reference, vias). Its kind stays generic: what the two nodes are is the Pair's (a coupled pair).
    pair_node: dict[int, int] = {}  # index into design.pairs -> index into design.netreqs
    for k, pr in enumerate(design.pairs):
        named = [n for n in (pr.p, pr.n) if n in owner]
        if not named:
            owner[pr.p] = owner[pr.n] = ("Pair", pr.line)
            bare_pairs.append(pr)
            continue
        whos = {owner[n][0] for n in named}
        if whos != {"NetReq"}:
            n = named[0]
            who, line = owner[n]
            how = "(a net is named by one NetReq or one Pair)" if who == "NetReq" else "(one of them, not both)"
            refusals.append(f"{n}: {who} line {line} and Pair line {pr.line} both name it, and a net is named once {how}")
            continue
        idx = {owner_idx[n] for n in named}
        i = min(idx)
        req = design.netreqs[i]
        head = ", ".join(f'"{x}"' for x in req.nets)
        kind = KIND_ALIASES.get(req.kind, req.kind)
        others = [x for x in req_nets[i] if x not in (pr.p, pr.n)]
        if len(named) < 2 or len(idx) > 1:
            n = named[0]
            partner = pr.n if n == pr.p else pr.p
            refusals.append(
                f"{n}: NetReq line {req.line} names it and not {partner}, its partner on Pair line {pr.line}; the two nets of a Pair "
                "are one class (their width and gap are the pair's), so a NetReq names both of them or neither"
            )
            continue
        if others:
            refusals.append(
                f"NetReq({head}) line {req.line}: names {pr.p} and {pr.n}, the nets of Pair line {pr.line}, and {', '.join(others)}; a Pair's "
                "nets are a class of their own (width and gap from its impedance), and every net of one NetReq shares one class"
            )
            continue
        if kind not in ("generic",):
            refusals.append(
                f'NetReq({head}) line {req.line}: kind="{req.kind}" says what a node is, and {pr.p} and {pr.n} are the coupled pair of '
                f"Pair line {pr.line}; a NetReq that names a Pair's nets states their node facts (volts=, max_mm=, rise_ps=, "
                "keep_clear_of=, class_name=) and takes kind generic"
            )
            continue
        owner[pr.p] = owner[pr.n] = ("Pair", pr.line)
        pair_node[k] = i
        bare_pairs.append(pr)
    node_reqs = set(pair_node.values())
    # A skew budget is one Pair's or one Bus's (docs/physics-subjects.md KD 8, G1/G3): a net timed by two of them
    # carries two budgets for one length difference (refutation one-home r1: `Bus("USB_DP", "USB_DN")` over a Pair,
    # and two Buses on SDA/SCL, were both accepted and both printed).
    timed: dict[str, tuple[str, int, str]] = {}
    for pr in design.pairs:
        for name in (pr.p, pr.n):
            timed.setdefault(name, ("Pair", pr.line, f"{pr.p} and {pr.n} within {_g(pr.match_mm)} mm of each other"))
    for bus_req in design.buses:
        members = [m for m in _expand(bus_req.nets, net_names) if m in known]
        clash: dict[tuple[str, int, str], list[str]] = {}
        for m in members:
            if m in timed:
                clash.setdefault(timed[m], []).append(m)
        for (who, line, what), nets in clash.items():
            verb = "is" if len(nets) == 1 else "are"
            refusals.append(
                f"Bus line {bus_req.line}: {', '.join(nets[:-1]) + ' and ' + nets[-1] if len(nets) > 1 else nets[0]} {verb} timed by {who} line {line} "
                f"already ({what}); a net's skew budget is one Pair's or one Bus's"
            )
        for m in members:
            timed.setdefault(m, ("Bus", bus_req.line, f"within {_g(bus_req.match_mm)} mm"))

    # -- the pours this board will have ------------------------------------------------------
    #
    # Read before any width is sized, because a width depends on the plane under it: on four layers
    # `Board(planes=)` verbatim, on two the back GND pour the route stage writes when GND is a power
    # net (`route_scene.plane_targets`), asked of the NetReq that names GND; a declared `planes=` on any
    # stackup (see `ConstraintSet.planes`).
    # Postfix A7 (refutation physics r2): a two-layer `planes=` IS read, as `route_scene.plane_targets` reads it (a
    # declared planes= is the board's answer on every stackup). It was ignored here while the route stage poured it,
    # so a current Path on a two-layer 3V3 pour got no joints and no E23, and a sense Path on it was not refused.
    gnd_power = any("GND" in req_nets[i] and KIND_ALIASES.get(r.kind, r.kind) == "power" for i, r in enumerate(design.netreqs))
    pours = planes if (planes or stack.layers > 2) else ((("GND", "B.Cu"),) if gnd_power else ())

    # -- per NetReq numbers ------------------------------------------------------------------
    numbers: list[tuple[int, list[str], _Numbers]] = []  # (req index, nets, numbers)
    for i, req in enumerate(design.netreqs):
        kind = KIND_ALIASES.get(req.kind, req.kind)
        first = req.nets[0]
        if kind not in PRESETS:
            head = ", ".join(f'"{n}"' for n in req.nets)
            if kind in PAIR_PRESETS:
                # E5 (docs/physics-subjects.md §7): a pair's kind holds for two paths at once.
                refusals.append(f'NetReq({head}) line {req.line}: kind="{kind}" is a Pair kind: it holds for two paths at once; a NetReq takes kind {_net_kinds_text()}')
            elif kind in BUS_KINDS_MATCH:
                # E6: a bus kind times two or more paths together.
                refusals.append(f'NetReq({head}) line {req.line}: kind="{kind}" is a Bus kind: it times two or more paths together; a NetReq takes kind {_net_kinds_text()}')
            elif kind in PATH_PRESETS:
                # E14 (PR 7): a sense or feedback branch is a Path's kind.
                refusals.append(
                    f'NetReq({head}) line {req.line}: kind="{kind}" is a Path kind: it names a branch of a net that carries a measurement; '
                    f"a NetReq takes kind {_net_kinds_text()}"
                )
            else:
                # E19: the kinds a NetReq takes, and the aliases.
                refusals.append(f'NetReq("{first}") line {req.line}: unknown kind {req.kind!r}; known: {", ".join(NET_KINDS)}, and the aliases digital and default')
            kind = "generic"
        allowed = _kwargs_of(kind)
        for kw in _given(req):
            if kw not in allowed:
                refusals.append(f'NetReq("{first}") line {req.line}: kind="{kind}" does not take {kw}=; it takes {", ".join(allowed)}')
        nets = req_nets[i]
        for name in nets:
            if "'" in name or '"' in name:
                refusals.append(
                    f'NetReq("{first}") line {req.line}: the net name {name!r} carries a quote; KiCad\'s rule file '
                    "reads the condition as ending there and silently drops every rule"
                )
        if req.class_name and ("'" in req.class_name or '"' in req.class_name):
            refusals.append(
                f'NetReq("{first}") line {req.line}: class_name={req.class_name!r} carries a quote; it goes into '
                "every rule condition as `A.hasNetclass('...')` and silently drops the whole rule file"
            )
        # E3 (docs/physics-subjects.md §7): a Ground() net is the 0 V its domain's voltages are measured from,
        # so a volts= that holds for every net of the line is false of it.
        if req.volts is not None:
            src = pathlib.Path(design.source).name if design.source else "board.py"
            head = ", ".join(f'"{n}"' for n in req.nets)
            for name in nets:
                net = design.nets.get(name)
                if net is not None and net.kind == "ground":
                    at = f" ({src} line {net.line})" if net.line else ""
                    refusals.append(
                        f"NetReq({head}) line {req.line}: volts={_g(req.volts)} holds for every net the line names, and {name} is "
                        f'Ground("{name}"){at}, the 0 V its domain\'s voltages are measured from'
                    )
        if i in node_reqs:
            continue  # the node facts of a Pair's two nets: compiled with the Pair, below
        num = _compile_req(
            design, stack, planes, req, kind, nets, None, pads_by_net, refusals, has_switch_node,
            floor_w, floor_w_src, class_clear, floor_clear, floor_clear_src, pours=pours,
        )
        numbers.append((i, nets, num))

    # -- bare Pairs: usb_hs's pair logic with the Pair's numbers, class Pair_<p> ---------------
    pair_index = {id(pr): k for k, pr in enumerate(design.pairs)}
    for pr in bare_pairs:
        if not _kicad_pairs(pr.p, pr.n):
            refusals.append(f"Pair line {pr.line}: KiCad pairs names ending P/N, _P/_N or +/-, and {pr.p}/{pr.n} are not such a pair")
        # A Pair with no kind is compiled from its own line only (refutation one-home r1, defect 2): never the
        # usb_hs preset's via budget, and no "preset usb_hs" source on a number the line did not write.
        pkind = pr.kind or "pair"
        nreq = design.netreqs[pair_node[pair_index[id(pr)]]] if pair_index[id(pr)] in pair_node else None
        cname = "USB" if pr.kind == "usb_hs" else f"Pair_{pr.p}"
        if nreq is not None:
            req = NetReqSpec(
                nets=(pr.p, pr.n), kind=pkind, line=pr.line, class_name=nreq.class_name or cname, volts=nreq.volts, max_mm=nreq.max_mm,
                keep_clear_of=nreq.keep_clear_of, keep_clear_mm=nreq.keep_clear_mm, rise_ps=nreq.rise_ps,
            )
        else:
            req = NetReqSpec(nets=(pr.p, pr.n), kind=pkind, line=pr.line, class_name=cname)
        num = _compile_req(
            design, stack, planes, req, pkind, [pr.p, pr.n], pr, pads_by_net, refusals, has_switch_node,
            floor_w, floor_w_src, class_clear, floor_clear, floor_clear_src, bare_pair=True, pours=pours,
            node_line=nreq.line if nreq is not None else None,
        )
        numbers.append((-1, [pr.p, pr.n], num))
    # Board order: a Pair line takes its place among the NetReq lines, so the classes read as the file does.
    numbers.sort(key=lambda t: t[2].line)

    # -- classes: a view on the numbers of one NetReq; collisions get a suffix ------------------
    classes: dict[str, CompiledClass] = {
        "Default": CompiledClass("Default", floor_w, class_clear, stack.via_diameter, stack.via_drill, lane_clearance_mm=class_clear)
    }
    class_owner: dict[str, tuple[int, float]] = {}
    constraints: dict[str, Constraint] = {}
    for idx, nets, num in numbers:
        pair_w = num.pair_geometry[0].value if num.pair_geometry else None
        pair_g = num.pair_geometry[1].value if num.pair_geometry else None
        key = (num.width.value, num.clearance.value, num.via.diameter_mm, num.via.drill_mm, pair_w, pair_g)
        name = num.class_base
        if name in classes:
            c = classes[name]
            if (c.track_width_mm, c.clearance_mm, c.via_diameter_mm, c.via_drill_mm, c.diff_pair_width_mm, c.diff_pair_gap_mm) != key:
                n = 2
                while f"{num.class_base}_{n}" in classes:
                    n += 1
                name = f"{num.class_base}_{n}"
                if num.class_base in class_owner:
                    owner_line, owner_w = class_owner[num.class_base]
                    notes_lines.append(f"class {name} ({num.class_base} is NetReq line {owner_line} at {_g(owner_w)} mm)")
                else:
                    notes_lines.append(f"class {name} ({num.class_base} is the class floor at {_g(c.track_width_mm)}/{_g(c.clearance_mm)} mm)")
        if name not in classes:
            classes[name] = CompiledClass(
                name=name,
                track_width_mm=num.width.value,
                clearance_mm=num.clearance.value,
                via_diameter_mm=num.via.diameter_mm,
                via_drill_mm=num.via.drill_mm,
                diff_pair_width_mm=pair_w,
                diff_pair_gap_mm=pair_g,
                lane_clearance_mm=num.lane_clearance,
            )
            class_owner[name] = (num.line, num.width.value)
        if idx >= 0:
            classes[name].patterns.extend(design.netreqs[idx].nets)
        else:
            classes[name].patterns.extend(nets)
        for net in nets:
            if net in constraints:
                continue  # the double-naming refusal already says so; the first line wins
            constraints[net] = Constraint(
                net=net,
                kind=num.kind,
                class_name=name,
                line=num.line,
                width_mm=num.width,
                clearance_mm=num.clearance,
                lane_clearance_mm=num.lane_clearance,
                via=num.via,
                layers=num.layers,
                reference=num.reference,
                pair=_pair_spec(num, net),
                group=None,
                airwire_max_mm=num.airwire,
                length_max_mm=num.length,
                keep_away=num.keep_away,
                spacing_w=num.spacing,
                voltage=num.voltage,
                loop_mm2=num.loop,
                rise_ps=num.rise_ps,
                guard_stitch_mm=None,
                guard_ground=None,
                isolation_side=None,
                autoroute=num.autoroute,
                soft=num.soft,
                notes=num.notes,
                req_index=idx,
            )

    # -- groups: the NetReq's own bus, then Bus() and Path() ------------------------------------
    groups: list[GroupSpec] = []
    for idx, nets, num in numbers:
        if idx < 0:
            continue
        # docs/physics-subjects.md PR 7: no net kind makes a bus (sense's pair of nets left with the kind).

    def synthesised(net: str) -> None:
        if net in constraints:
            return
        num = _compile_req(
            design, stack, planes, NetReqSpec(nets=(net,), kind="generic"), "generic", [net], None, pads_by_net, refusals, has_switch_node,
            floor_w, floor_w_src, class_clear, floor_clear, floor_clear_src,
        )
        constraints[net] = Constraint(
            net=net, kind="generic", class_name="Default", line=0, width_mm=num.width, clearance_mm=num.clearance,
            lane_clearance_mm=num.lane_clearance, via=num.via, layers=num.layers, reference=None, pair=None, group=None,
            airwire_max_mm=None, length_max_mm=None, keep_away=(), spacing_w=3, voltage=None, loop_mm2=None,
            rise_ps=None, guard_stitch_mm=None, guard_ground=None, isolation_side=None, autoroute=True, soft=(), notes=(), req_index=-1,
        )

    for bus_req in design.buses:
        members = _expand(bus_req.nets, net_names)
        if bus_req.clock is not None and bus_req.clock not in members:
            refusals.append(f'Bus line {bus_req.line}: clock "{bus_req.clock}" is not one of {", ".join(members)}')
        src = f"Bus line {bus_req.line}" if bus_req.match_written else f"preset {bus_req.kind}, Bus line {bus_req.line}"
        spec = GroupSpec("bus", tuple(members), bus_req.match_mm, bus_req.clock, bus_req.line, src,
                         length_mm=round(bus_req.length_mm, 4) if bus_req.length_mm is not None else None)
        groups.append(spec)
        for net in members:
            synthesised(net)
            c = constraints[net]
            length = c.length_max_mm
            # Round 3 (refute-onehome): Bus(length_mm=) is each member's budget between the stations its Path names,
            # as Pair(length_mm=) is (`paths._compile_one`); a whole-net length counts stubs no path times (node's
            # SDA: the R_SDA pull-up). A member no Path names is two pads (E13), so its net is its path.
            if bus_req.length_mm is not None and not any(q.net == net for q in design.paths):
                length = Derived(round(bus_req.length_mm, 4), "mm", Source("Bus", f"line {bus_req.line}"))
            constraints[net] = replace(c, group=spec, length_max_mm=length)

    for ch in design.paths:
        if ch.net not in design.nets:
            refusals.append(f'Path("{ch.net}") line {ch.line}: no net "{ch.net}"')
            continue
        ok = True
        for pad in ch.pads:
            if "." not in pad:
                refusals.append(f'Path("{ch.net}") line {ch.line}: {pad!r} is not REF.PIN (a part reference, a dot, a pin name or pad number)')
                ok = False
                continue
            ref, pin = pad.split(".", 1)
            if not any(inst.ref == ref for inst in design.instances):
                refusals.append(f'Path("{ch.net}") line {ch.line}: no part {ref}')
                ok = False
                continue
            net = _refpin_net(design, ref, pin)
            if net is None:
                refusals.append(f'Path("{ch.net}") line {ch.line}: {ref} has no pin {pin}')
                ok = False
            elif net != ch.net:
                # The part's pad on the path's net, when it has exactly one: the line to write.
                inst = next(i for i in design.instances if i.ref == ref)
                mine = [n for pname, pin_ in inst.part.pins.items() if inst.pins.get(pname) == ch.net for n in pin_.pads]
                # A fact about the part (its pad on the path's net), never the rewritten line.
                hint = f"; {ref}'s {ch.net} pad is {ref}.{mine[0]}" if len(mine) == 1 else (f"; {ref} has no pad on {ch.net}" if not mine else "")
                refusals.append(f'Path("{ch.net}") line {ch.line}: {pad} is on {net}, not {ch.net}{hint}')
                ok = False
        if not ok:
            continue
        spec = GroupSpec("chain", tuple(ch.pads), None, None, ch.line, f"Path line {ch.line}")
        groups.append(spec)
        synthesised(ch.net)
        c = constraints[ch.net]
        if c.group is None or c.group.kind != "bus":
            constraints[ch.net] = replace(c, group=spec)

    # -- E13 (docs/physics-subjects.md §7): a Pair or Bus compares the lengths of its members' paths, so a member
    # with three or more pads needs exactly one Path naming the stations being compared.
    for who, members in [(f"Pair line {pr.line}", (pr.p, pr.n)) for pr in design.pairs] + [(f"Bus line {b.line}", tuple(_expand(b.nets, net_names))) for b in design.buses]:
        for m in members:
            pads = [f"{r}.{n}" for r, n in pads_by_net.get(m, [])]
            if len(pads) < 3:
                continue
            k = sum(1 for q in design.paths if q.net == m)
            if k != 1:
                refusals.append(f"{who}: {m} has {len(pads)} pads ({', '.join(pads)}) and {k} Paths name its stations, so the length the {who.split()[0].lower()} compares is not defined")

    # -- guards: the shielded net, the pitch, and the net the shield is made of -------------------
    #
    # Both halves of the statement, because until `docs/stitch-plan.md` S7 only one of them arrived:
    # `ground=` was parsed by `language.Guard`, stored on `GuardReq`, and then dropped here, so the
    # router had no way to write a guard in anything but whatever net a pattern defaulted to. The
    # refusal below is the mirror of the one above it and lives here for the same reason — this is
    # the one place that holds the netlist, and a pattern is handed a scene.
    guarded: dict[str, int] = {}
    for gd in design.guards:
        if gd.net not in design.nets:
            refusals.append(f'Guard("{gd.net}") line {gd.line}: no net "{gd.net}"')
            continue
        if gd.net in guarded:
            # Round 3 (refute-onehome): one net, one guard. The second Guard overwrote the first's pitch and
            # ground while the report credited the first line.
            refusals.append(f'Guard("{gd.net}") line {gd.line}: {gd.net} already has a Guard() on line {guarded[gd.net]}; one net, one guard')
            continue
        guarded[gd.net] = gd.line
        if gd.ground not in design.nets:
            refusals.append(
                f'Guard("{gd.net}") line {gd.line}: no net "{gd.ground}" to make the guard out of'
                f'{_closest(gd.ground, net_names)}'
            )
            continue
        synthesised(gd.net)
        constraints[gd.net] = replace(constraints[gd.net], guard_stitch_mm=gd.stitch_mm, guard_ground=gd.ground)

    # -- bridges: the one point two grounds are tied at, and nothing else ------------------------
    #
    # No geometry is compiled here and none ever will be: `docs/stitch-plan.md` §8 item 4 refuses
    # every copper form of a ground tie, so what a `Bridge()` becomes is a resolved pair of pads on
    # a part the board already places. The net-level refusals are here because they are facts about
    # the netlist the compiler already holds; the instance-level ones are `circuit.check_design`'s,
    # beside the part they name.
    bridge_specs: list[BridgeSpec] = []
    seen_pairs: dict[frozenset[str], int] = {}
    for br in design.bridges:
        who = f'Bridge("{br.a}", "{br.b}") line {br.line}'
        ok = True
        for name in (br.a, br.b):
            net = design.nets.get(name)
            if net is None:
                refusals.append(f"{who}: no net {name!r}{_closest(name, net_names)}")
                ok = False
            elif net.kind != "ground":
                said = "Power" if net.kind == "power" else "Net"
                refusals.append(
                    f'{who}: {name} is declared with {said}(); a bridge ties two grounds, and {name} is not declared with Ground()'
                )
                ok = False
        pair = frozenset((br.a, br.b))
        if pair in seen_pairs:
            refusals.append(f"{who}: {br.a} and {br.b} are already bridged on line {seen_pairs[pair]}; two grounds are tied at one point or they are not tied at one point")
            ok = False
        else:
            seen_pairs[pair] = br.line
        if not ok:
            continue
        tie_ref, pads = "", ()
        if br.kind == "off_board":
            pads = br.at
        else:
            tie_ref = br.at[0]
            inst = next((i for i in design.instances if i.ref == tie_ref), None)
            if inst is not None:
                on = {}
                for pin, net_name in inst.pins.items():
                    for num in (inst.part.pins[pin].pads if pin in inst.part.pins else (pin,)):
                        on.setdefault(net_name, []).append(f"{tie_ref}.{num}")
                if len(on.get(br.a, ())) == 1 and len(on.get(br.b, ())) == 1:
                    pads = (on[br.a][0], on[br.b][0])
        bridge_specs.append(BridgeSpec(req=br, a=br.a, b=br.b, kind=br.kind, at=br.at, tie_ref=tie_ref, pads=tuple(pads), why=br.why))

    # -- thermals: the budget under one land, and the four refusals that precede it ---------------
    #
    # After the pours are known, because three of the four refusals are questions about them. See
    # `_thermal_refusals` for why the fifth — the one about the land's own geometry — is the
    # pattern's and not this file's.
    #
    # -- isolations: sides from Place() lines, the corridor, the numbers -------------------------
    rule_areas: list[RuleArea] = []
    iso_specs: list[IsolationSpec] = []
    # An Isolation's sides are Regions of layout.core.py (docs/board-outline.md A.1): with core not loaded
    # (`load_board(core=False)`, the schematic stage) there is nothing to resolve them against.
    if design.isolations and design.core_loaded is not False:
        from .layout import declared_outline, frame_board

        # The Regions resolve in the frame: against the declared outline, or the derived frame (B.1).
        region_rects = resolve_regions(frame_board(board, declared_outline(design)[0]), design.regions)
        region_parent = {r.name: r.parent for r in design.regions}
        sides = _part_sides(design)
        bname = pathlib.Path(design.source).name if design.source else "board.py"
        for iso in design.isolations:
            missing = [s for s in (iso.a, iso.b) if s not in region_rects]
            if missing:
                has_core = design.source is not None and (pathlib.Path(design.source).parent / "layout.core.py").exists()
                for s in missing:
                    tail = f'layout.core.py declares no Region "{s}"' if has_core or design.source is None else f"there is no layout.core.py next to {bname}"
                    refusals.append(f'{bname}:{iso.line}: Isolation("{iso.a}", "{iso.b}") names Region "{s}", and {tail}')
                continue
            spec, area = _compile_isolation(design, stack, board, iso, region_rects, region_parent, sides, pads_by_net, refusals)
            iso_specs.append(spec)
            if area is not None:
                rule_areas.append(area)
            for net in spec.nets_a:
                synthesised(net)
                constraints[net] = replace(constraints[net], isolation_side=iso.a)
            for net in spec.nets_b:
                synthesised(net)
                constraints[net] = replace(constraints[net], isolation_side=iso.b)

    # -- the pours this board will have, and the caveat they put on `Constraint.reference` ---------
    #
    # On two layers the route stage pours the back GND when GND is one of the power nets; asked of the
    # compiled constraints that is "GND compiled to kind=power", the same question with the globs
    # already expanded. A declared `planes=` is read on every stackup (see `ConstraintSet.planes`).
    assert pours == (planes if (planes or stack.layers > 2) else ((("GND", "B.Cu"),) if any(c.net == "GND" and c.kind == "power" for c in constraints.values()) else ())), "the pours read before the widths are the compiled ones"
    # The caveat goes on the net's own `notes` so it prints beside its numbers rather than at the
    # bottom of the report, and it is spelled with the **sorted** pair name for a pair so both members
    # write byte-identical text and `_report` prints it once (the width note next to it does the same
    # thing with the NetReq's own order; sorted is the spelling that does not depend on which member
    # the compiler reached first).
    by_net_ref: dict[str, str] = {}
    for _idx, nets, num in numbers:
        if num.reference_name:
            for n in nets:
                by_net_ref.setdefault(n, num.reference_name)
    for name, c in list(constraints.items()):
        rule = return_rule(stack, pours, c)
        if rule is None or rule.verdict in ("kept", "pinned"):
            continue
        spelling = "/".join(sorted((c.net, c.pair.partner))) if c.pair is not None else c.net
        note = _return_caveat(rule, spelling, by_net_ref.get(name, c.reference or "it"))
        if note not in c.notes:
            constraints[name] = replace(c, notes=c.notes + (note,))

    thermal_specs = _thermal_refusals(design, stack, pours, refusals)
    ordered = tuple(constraints[n] for n in sorted(constraints))
    cs = ConstraintSet(
        stackup=stack,
        constraints=ordered,
        classes=tuple(classes.values()),
        groups=tuple(groups),
        isolations=tuple(design.isolations),
        rule_areas=tuple(rule_areas),
        canary_net=_canary_net(design),
        refusals=tuple(refusals),
        lines=(),
        isolation_specs=tuple(iso_specs),
        bridges=tuple(bridge_specs),
        thermals=thermal_specs,
        planes=pours,
    )
    # Paths and loops (docs/net-path-physics.md): compiled onto the net numbers, which are final here.
    from .paths import compile_paths

    cs, path_lines, _loops = compile_paths(design, cs, refusals)
    cs = replace(cs, refusals=tuple(refusals))
    return replace(cs, lines=tuple(_report(cs, design, numbers, notes_lines, has_switch_node, path_lines)))


def _kicad_pairs(p: str, n: str) -> bool:
    """KiCad pairs names that differ only by a P/N, _P/_N or +/- suffix, in either order: a board
    is free to write the negative net first, and a glob hands them over sorted (USB_DN, USB_DP)."""
    if len(p) < 2 or len(n) < 2 or p[:-1] != n[:-1]:
        return False
    return (p[-1], n[-1]) in (("P", "N"), ("+", "-"), ("N", "P"), ("-", "+"))


def _pair_spec(num: _Numbers, net: str) -> PairSpec | None:
    if num.pair_geometry is None:
        return None
    w, g, z_target, z, tol, skew, uncoupled, controlled = num.pair_geometry
    return PairSpec(num.pair_of.get(net, ""), w, g, z_target, z, tol, skew, uncoupled, controlled, num.pair_length, num.line)


def z_se_width(stack: Stackup, z_target: float, layer: str, lane: float) -> tuple[Derived, tuple[float, Derived], str]:
    """(width, (target, computed z), note) for a single-ended impedance on `layer` (C.1/C.4/C.5): the one
    block a Path's link calls (`docs/physics-subjects.md` N4: an impedance is a piece of copper's). `lane`
    is the net's clearance (the 2-layer CPWG pour gap is `max(0.3, 2 x lane)`). `note` is "" or why the
    target is not reached."""
    pour = None
    if stack.layers == 2 and is_outer(layer):
        pour = max(0.3, 2 * lane)
    w = width_for_z0(z_target, stack, layer=layer, pour_gap=pour)
    z = microstrip_z0(w, stack, layer=layer, pour_gap=pour)
    if not is_outer(layer):
        formula, ref = "wadell_stripline_asym", f"{stack.name} {layer} (formula only, +-5 %)"
    elif pour is not None:
        formula, ref = "ghione_naldi_cpwg", f"x {_g(stack.z_bias_se)} {stack.jlc_code or stack.name} pour gap {_g(pour)} t = 0"
    else:
        formula, ref = "hj_microstrip", f"x {_g(stack.z_bias_se)} {stack.jlc_code or stack.name}"
    cal = stack.z_bias_source if is_outer(layer) else "JLC publishes no inner-layer rows"
    off = abs(z - z_target) / z_target if z_target else 0.0
    why = ""
    if off > 0.02:
        why = (
            f"the fab floor {_g(stack.track_min)} mm stops at {_g(z)} ohm"
            if abs(w - stack.track_min) < 1e-9
            else f"6 mm of copper only reaches {_g(z)} ohm"
            if w >= 5.999
            else f"the solver lands {off * 100:.0f} % off"
        )
    reached = f"; not reached: {why}" if why else ""
    return Derived(w, "mm", Source(formula, ref, cal)), (z_target, Derived(z, "ohm", Source(formula, ref, f"{cal}; target {_g(z_target)}{reached}"))), why


def _current_width(amps: float, dt: float, stack: Stackup, plane: tuple[float, str, str] | None, layer: str | None = None) -> tuple[Derived, CurrentSpec]:
    """The width a current needs on one layer, and its `CurrentSpec`: the one block a path's link
    (`paths.compile_paths`) and a multi-pad station's join call (`docs/net-path-physics.md` §2); a net
    carries no current of its own (`docs/physics-subjects.md` PR 3). `plane` is `_reference_for` of that layer.

    IPC-2221B against IPC-2152 (`current_width_mm`), floored at 0.4 mm at `CURRENT_FLOOR_STEP_AMPS` or
    more and 0.25 mm below it, whatever the net's kind (`docs/physics-subjects.md` KD 10: one floor for
    every copper that carries a current; the switch node's 0.3 mm folds into it). A pcbc default with no
    published source, kept so that no declared copper moves (O7)."""
    plane_h = plane[0] if plane is not None else None
    cu = stack.layer_oz(layer)
    f2221, side = ipc2221_formula(stack, layer)
    w2221 = Derived(ipc2221_width_mm(amps, dt, cu, outer=not stack.is_inner(layer)), "mm", Source(f2221, f"IPC-2221B eq. 6-2 {side} {_g(amps)} A {_g(dt)} C {cu_text(stack, layer)}"))
    bm = ipc2152_board_modifier(stack.board_mm)
    pm = ipc2152_plane_modifier(plane_h)
    plane_txt = f"plane {pm:.3f} at {_g(plane_h)} mm {plane[1]}" if plane is not None else "no plane"
    w2152 = Derived(
        ipc2152_width_mm(amps, dt, cu, stack.board_mm, plane_h),
        "mm",
        Source("ipc2152_fit", f"IPC-2152 Fig 5-1, 5-8, 5-11 {_g(amps)} A {_g(dt)} C {cu_text(stack, layer)} x board {bm:.3f} x {plane_txt}", "below 0.274 A the fit extrapolates" if 0 < amps < 0.274 else ""),
    )
    current = CurrentSpec(amps, dt, w2221, w2152, plane_h)
    cur = current_width_mm(amps, dt, stack, plane_h, layer)
    both = f"{f2221} {_g(amps)} A {_g(dt)} C {cu_text(stack, layer)} {w2221.value:.3f}; ipc2152_fit x board {bm:.3f} x {plane_txt} {w2152.value:.3f}"
    floor = 0.4 if amps >= CURRENT_FLOOR_STEP_AMPS else 0.25
    if floor >= cur.value:
        width = Derived(floor, "mm", Source("pcbc_floor", f"amps {'>=' if amps >= CURRENT_FLOOR_STEP_AMPS else '<'} {_g(CURRENT_FLOOR_STEP_AMPS)}", both))
    else:
        width = cur
    # C.6: below 0.274 A the IPC-2152 fit extrapolates. The Derived carried that clause and the
    # report line dropped it, so a 0.1 A rail read as if the chart covered it.
    clause = next((part for part in (cur.source.note or "").split("; ") if "extrapolat" in part), "")
    if clause and clause not in (width.source.note or ""):
        width = replace(width, source=replace(width.source, note=f"{width.source.note}; {clause}"))
    return width, current


def _compile_req(
    design: Design,
    stack: Stackup,
    planes: tuple[tuple[str, str], ...],
    req: NetReqSpec,
    kind: str,
    nets: list[str],
    pair_req: PairReq | None,
    pads_by_net: dict[str, list[tuple[str, str]]],
    refusals: list[str],
    has_switch_node: bool,
    floor_w: float,
    floor_w_src: Source,
    class_clear: float,
    floor_clear: float,
    floor_clear_src: Source,
    bare_pair: bool = False,
    pours: tuple[tuple[str, str], ...] = (),
    node_line: int | None = None,
) -> _Numbers:
    preset = PRESETS.get(kind) or PAIR_PRESETS.get(kind) or BARE_PAIR
    line = req.line
    # The line the node facts (volts=, max_mm=, keep_clear_of=, ...) came from: the NetReq's, also when it names a
    # Pair's two nets and they are compiled with the Pair (refutation one-home r1, defect 1).
    nline = node_line if node_line is not None else line
    is_pair = pair_req is not None
    first = req.nets[0]
    notes: list[str] = []
    soft: list[str] = []

    # layers: every copper layer `Board(planes=)` does not give to a plane (docs/physics-subjects.md P4); a
    # Pair's own layers= for its two paths (G2). A net kind sets none.
    have = stack.copper_layers()
    plane_layers = {lay for _n, lay in planes} if stack.layers > 2 else set()
    layers: tuple[str, ...] = tuple(lay for lay in have if lay not in plane_layers) or tuple(have)
    layers_src = "every layer no plane takes" if plane_layers else f"preset {kind}"
    if is_pair:
        # A pair is sized as edge-coupled microstrip (C.3), which exists on an outer layer only; its default is
        # the outer layers no plane takes (its Pair(layers=) can say otherwise).
        layers = tuple(lay for lay in layers if is_outer(lay)) or layers
        layers_src = f"preset {kind}" if kind in PAIR_PRESETS else "a pair's default: the outer layers no plane takes"
    if pair_req is not None and pair_req.layers is not None:
        layers, layers_src = tuple(pair_req.layers), f"Pair line {pair_req.line}"
    unknown = [lay for lay in layers if lay not in have] if (pair_req is not None and pair_req.layers is not None) else []
    if unknown:
        # This used to reach stackup.plane_below and die with `tuple.index(x): x not in tuple`.
        refusals.append(f'Pair line {pair_req.line}: {", ".join(unknown)}: {stack.name} has {", ".join(have)}')  # type: ignore[union-attr]
        layers = tuple(lay for lay in layers if lay in have) or (have[0],)
    layer0 = layers[0] if layers else "F.Cu"
    inner_only = bool(layers) and not any(is_outer(lay) for lay in layers)

    # width
    width: Derived = Derived(floor_w, "mm", floor_w_src)
    if preset.width_mm is not None:
        width = _preset(preset.width_mm, "mm", kind)
    # No current is sized here: a net carries none of its own (`docs/physics-subjects.md` PR 3). A
    # power net's copper no Path names is the class floor; a Path's links carry their own widths.
    plane = _reference_for(stack, layer0, pours)

    # clearance: the kind number, then the voltage row, then the floor
    lane = preset.clearance_mm if preset.clearance_mm is not None else class_clear
    clearance = _preset(preset.clearance_mm, "mm", kind) if preset.clearance_mm is not None else Derived(class_clear, "mm", floor_clear_src)
    voltage: VoltageSpec | None = None
    if req.volts is not None:
        row = "B1" if inner_only else "B2"
        row_mm = ipc2221_clearance_mm(req.volts, row)
        label = _ipc2221_row_label(req.volts)
        row_d = Derived(row_mm, "mm", Source("ipc2221_6_1", f"IPC-2221B 6-1 row {label} V {row}"))
        creep = None
        if req.volts >= CREEPAGE_FROM_V:
            creep = Derived(iec_creepage_mm(req.volts, "IIIa"), "mm", Source("iec60664_f5", f"IEC 60664-1 F.5 row {_g(_creepage_row_v(req.volts))} V IIIa PD2"))
        voltage = VoltageSpec(req.volts, row, row_d, creep)
        if row_mm > clearance.value:
            was = f"preset {kind}" if clearance.source.formula == "preset" else clearance.source.formula
            clearance = Derived(row_mm, "mm", Source("ipc2221_6_1", f"IPC-2221B 6-1 row {label} V {row}", f"over {was} {_g(clearance.value)}"))
        else:
            clearance = replace(clearance, source=replace(clearance.source, note=f"ipc2221_6_1 row {label} V {row} {_g(row_mm)}"))

    # via
    if preset.via is not None:
        via_d, via_h = max(preset.via[0], stack.via_diameter), max(preset.via[1], stack.via_drill)
        via_src = f"preset {kind}"
    else:
        via_d, via_h = stack.via_diameter, stack.via_drill
        via_src = f"stackup {stack.name}"
    allowed = preset.vias_allowed
    count_max = preset.vias_max
    if pair_req is not None and (pair_req.vias is not None or pair_req.vias_max is not None):
        # Pair(vias=, vias_max=): the pair's copper, both members (refutation one-home r1, defect 2).
        if pair_req.vias is not None:
            allowed = bool(pair_req.vias)
        if pair_req.vias_max is not None:
            count_max = pair_req.vias_max
        via_src = f"Pair line {pair_req.line}" if pair_req.vias is False else via_src
    via = ViaSpec(via_d, via_h, allowed, 0 if not allowed else count_max, None, 1)

    # controlled impedance: the pair, or a single-ended target
    reference: str | None = None
    reference_name: str | None = None
    reference_src = None
    pair_ref = canon_reference(stack, pair_req.reference) if pair_req is not None and pair_req.reference is not None else None
    ref_refused = False
    if pair_ref is not None:
        reference_src = pair_ref
    pair_geometry = None
    width_line: str | None = None
    pair_of: dict[str, str] = {}
    wants_reference = is_pair
    if wants_reference and pair_req is not None and pair_ref is not None:
        # E10 on a Pair (refutation one-home r1, defect 3): a reference plane is the plane under one layer's
        # copper (N14, G2), as on a Path.
        under = [lay for lay in layers if (r_ := _reference_for(stack, lay, pours)) is not None and r_[1] == pair_ref]
        poured = {lay for _n, lay in pours}
        if not is_poured_reference(pair_ref, poured):
            # Postfix A2: on any layer count, as on a Path (paths._path_refusals). It was refused on four
            # layers only, so a two-layer Pair printed "over Banana".
            plane_list = ", ".join(f"{lay} ({n})" for n, lay in pours) or "none"
            refusals.append(f'Pair line {pair_req.line}: reference="{pair_req.reference}" is not a plane this board pours ({plane_list})')
            ref_refused = True
        elif len(layers) > 1:
            on = f"the plane under {_and_list(under)} only" if under else "under none of its layers"
            refusals.append(f'Pair line {pair_req.line}: reference="{pair_req.reference}" is {on}, and this Pair may run on {_and_list(layers)}')
        elif not under:
            refusals.append(f'Pair line {pair_req.line}: reference="{pair_req.reference}" is not the plane under {layers[0]}, the one layer this Pair may run on')
    if wants_reference:
        if reference_src is not None:
            reference = reference_src
            net = next((n for n, lay in planes if lay == reference), None)
            reference_name = f"the {reference}" if reference.endswith(" pour") else (f"{reference} ({net})" if net else reference)
            if stack.layers > 2 and net is None and not ref_refused:
                refusals.append(f'{first}: {_g(pair_req.z_diff_ohm if pair_req else USB_Z_DIFF)} ohm on {layer0} (Pair line {line}) names reference {reference}, which is not in Board(planes=)')
        elif plane is not None:
            reference, reference_name = plane[1], plane[2]
        elif stack.layers > 2 and is_outer(layer0):
            want = stack.plane_below(layer0) or stack.plane_above(layer0)
            z_txt = _g(pair_req.z_diff_ohm if pair_req else USB_Z_DIFF)
            refusals.append(f"{first}: {z_txt} ohm on {layer0} (Pair line {line}) has no reference plane: {want}, the layer next to {layer0}, is not in Board(planes=)")
        elif stack.layers <= 2 and pours:
            # Round 3 (refute-onehome): the board pours, just not the layer facing this one.
            z_txt = _g(pair_req.z_diff_ohm if pair_req else USB_Z_DIFF)
            other = "B.Cu" if layer0 == "F.Cu" else "F.Cu"
            refusals.append(
                f"{first}: {z_txt} ohm on {layer0} (Pair line {line}) has no reference plane: {other}, the layer facing {layer0}, is not poured "
                f"(this board pours {', '.join(f'{lay} ({n})' for n, lay in pours)})"
            )
        elif stack.layers <= 2:
            # Two layers and no back pour (GND is not a power net, so `route_scene.plane_targets` pours
            # nothing): an impedance with nothing to return through, which nothing can size.
            z_txt = _g(pair_req.z_diff_ohm if pair_req else USB_Z_DIFF)
            refusals.append(
                f"{first}: {z_txt} ohm on {layer0} (Pair line {line}) has no reference plane: this two-layer board pours B.Cu only "
                "when GND is a power net (route_scene.plane_targets), and no NetReq names GND with kind power"
            )

    if is_pair:
        if len(nets) == 2:
            pair_of = {nets[0]: nets[1], nets[1]: nets[0]}
        # docs/physics-subjects.md PR 6: every pair is a Pair line; `kind="usb_hs"` fills what the line leaves out
        # from the preset, and a Pair with no kind carries only its own line's numbers.
        preset_pair = pair_req is not None and pair_req.kind == "usb_hs"
        z_target = float(pair_req.z_diff_ohm) if pair_req is not None else USB_Z_DIFF
        z_src_who = f"preset {kind}" if (preset_pair and "z_diff_ohm" not in pair_req.written) else f"Pair line {line}"  # type: ignore[union-attr]
        gap_given = None
        gap_src = Source("stackup", stack.name, f"gap max(0.15, clearance_min {_g(stack.clearance_min)})")
        if pair_req is not None and pair_req.gap_mm is not None:
            gap_given = pair_req.gap_mm
            raised = pair_gap_mm(stack, gap_given) > gap_given
            gap_src = Source(
                "Pair",
                f"line {pair_req.line}",
                f"gap_mm={_g(pair_req.gap_mm)} raised to the fab floor clearance_min {_g(stack.clearance_min)}"
                if raised
                else f"overrides gap max(0.15, clearance_min {_g(stack.clearance_min)})",
            )
        pair_layer = layer0 if is_outer(layer0) else "F.Cu"
        w, g = diff_pair_geometry(z_target, stack, pair_layer, gap_given)
        solved = diff_pair_width_mm(z_target, stack, pair_layer, gap_given)
        controlled = solved <= PAIR_FIT_MM
        z = diff_pair_z(w, g, stack, pair_layer)
        code = stack.jlc_code or stack.name
        row = next((r for r in stack.rows if r.structure == "diff" and r.z_ohm == z_target), None)
        if row is not None:
            row_note = f"JLC row {_g(row.width_mm)}/{_g(row.gap_mm or 0)}"
        elif stack.rows:
            row_note = f"no JLC row at {_g(z_target)} ohm; {stack.z_bias_source}"
        elif stack.jlc_code:
            row_note = "uncalibrated"
        else:
            row_note = "formula only"
        tol = 15.0 if preset_pair else 10.0
        src = Source("hj_coupled_microstrip", f"x {_g(stack.z_bias_diff)} {code}", row_note)
        w_d = Derived(w, "mm", src)
        g_d = Derived(g, "mm", gap_src)
        z_d = Derived(z, "ohm", Source("hj_coupled_microstrip", f"x {_g(stack.z_bias_diff)} {code}", f"{row_note}; target {_g(z_target)} +-{_g(tol)} %"))
        skew_default = DEFAULT_MATCH_MM["usb_hs"]
        if preset_pair:
            written = pair_req.written  # type: ignore[union-attr]
            skew = _override(pair_req.match_mm, "mm", "Pair", line, kind, _g(skew_default)) if "match_mm" in written else _preset(skew_default, "mm", kind, MATCH_SOURCE["usb_hs"])  # type: ignore[union-attr]
            uncoupled = (
                _override(pair_req.uncoupled_mm, "mm", "Pair", line, kind, _g(USB_UNCOUPLED_MM)) if "uncoupled_mm" in written  # type: ignore[union-attr]
                else _preset(USB_UNCOUPLED_MM, "mm", kind, MATCH_SOURCE["usb_hs"])
            )
        else:
            skew = Derived(pair_req.match_mm if pair_req else skew_default, "mm", Source("Pair", f"line {line}"))
            uncoupled = Derived(pair_req.uncoupled_mm if pair_req else USB_UNCOUPLED_MM, "mm", Source("Pair", f"line {line}"))
        pair_geometry = (w_d, g_d, z_target, z_d, tol, skew, uncoupled, controlled)
        width = Derived(w, "mm", src)
        pair_clear = max(min(0.16, w), floor_clear)
        clearance = Derived(pair_clear, "mm", floor_clear_src if floor_clear >= min(0.16, w) else Source("preset", kind, "min(0.16, pair width)"))
        lane = pair_clear  # the kind number: a voltage row never widens a fanout lane
        if voltage is not None and voltage.clearance_mm.value > pair_clear:
            # D, "volts on any kind": the class clearance is max(kind number, IPC-2221B 6-1 row).
            # This block used to overwrite the row the voltage block had already applied.
            clearance = replace(voltage.clearance_mm, source=replace(voltage.clearance_mm.source, note=f"over preset {kind} {_g(pair_clear)}"))
        elif voltage is not None:
            # The row the pair's own clearance already covers, said as on any other net (a NetReq naming the
            # pair's nets, refutation one-home r1 defect 1).
            row_txt = f"ipc2221_6_1 row {_ipc2221_row_label(voltage.volts)} V {voltage.row} {_g(voltage.clearance_mm.value)}"
            clearance = replace(clearance, source=replace(clearance.source, note=f"{clearance.source.note}; {row_txt}" if clearance.source.note else row_txt))
        over = f" over {reference_name}" if reference_name else ""
        extra = "" if controlled else "; uncontrolled"
        width_line = f"{_g(w)} mm wide, gap {_g(g)} mm on {pair_layer}{over}: {_g(z)} ohm ({z_d.source.formula} {z_d.source.ref}; {z_d.source.note}{extra}; {z_src_who})"
        if not controlled:
            a, b = (nets[0], nets[1]) if len(nets) == 2 else (first, "")
            # The calibration state, not a slice of its sentence: rows -> fitted, a code without
            # rows -> interpolated, neither -> formula only (the 2-layer case).
            cal_word = "fitted" if stack.rows else ("interpolated" if stack.jlc_code else "formula only")
            advice = (
                "within the USB full speed tolerance, outside the high speed one"
                if stack.layers <= 2
                else f"{_g(z_target)} ohm is not reached within {_g(PAIR_FIT_MM)} mm members at this gap on this prepreg"
            )
            notes.append(
                f"{a}/{b}: {_g(z_target)} ohm needs {_g(solved)} mm members at gap {_g(pair_gap_mm(stack, gap_given))} on {stack.name} "
                f"({cal_word}); pair written at the fab floor {_g(stack.track_min)}/{_g(stack.clearance_min)} = {z:.1f} ohm; {advice}"
            )
        soft += ["track_width", "skew", "via_budget", "diff_pair_uncoupled"]

    # airwire budget (placement), routed length (KiCad)
    airwire = preset.airwire_mm
    airwire_src = Source("preset", kind) if airwire is not None else None
    if req.max_mm is not None:
        airwire = req.max_mm
        airwire_src = Source("NetReq", f"line {nline}", f"overrides preset {kind} {_g(preset.airwire_mm)}" if preset.airwire_mm is not None else "")
    length: Derived | None = None
    length_none = False
    if is_pair and pair_req is not None and pair_req.length_mm is not None:
        pass  # a Pair's length_mm= is each member Path's budget (`paths`), never a net rule (KD 12: a net rule sums stubs)
    elif is_pair and pair_req is not None and pair_req.kind == "usb_hs":
        length_none = True  # the usb_hs Pair kind reports none until a length is written (§4.1)
    if kind == "i2c":
        pf_max = req.pf_max if req.pf_max is not None else I2C_PF_MAX
        pf_src = f"NetReq line {line}" if req.pf_max is not None else "UM10204 7.1"
        # UM10204 7.1: Cb is per bus line. Summing SDA's and SCL's pads charged every line for
        # the other's devices (10 pF each). Every net of one NetReq shares one number, so take
        # the busiest line: the shorter, safer length of the two.
        pins = max((len(pads_by_net.get(n, ())) for n in nets), default=0)
        z0, eeff = microstrip(width.value, stack.h_mm, stack.copper_t("F.Cu"), stack.er)
        c_exact = capacitance_pf_per_mm(z0 * stack.z_bias_se, eeff / stack.z_bias_se**2, exact=True)
        c = round(c_exact, 4)
        mm = i2c_max_mm(pf_max, pins, c_exact)
        if mm <= 0:
            refusals.append(
                f'NetReq("{first}") line {line}: pf_max={_g(pf_max)} pF is under the {pins} device pin{"s" if pins != 1 else ""} on the bus '
                f"({10 * pins} pF at 10 pF each, UM10204 7.1): no length is left for copper"
            )
            mm = 0.0
        length = Derived(mm, "mm", Source("i2c_capacitance", f"{_g(pf_max)} pF ({pf_src}) - 10 pF x {pins} pins on the busiest line at {_g(c)} pF/mm ({_g(width.value)} mm F.Cu)"))

    # keep-away
    keep: list[KeepAway] = []
    if req.keep_clear_mm is not None and not req.keep_clear_of:
        refusals.append(f'NetReq("{first}") line {nline}: keep_clear_mm={_g(req.keep_clear_mm)} sizes a keep-away, and this NetReq gives no keep_clear_of=')
    for pat in req.keep_clear_of or ():
        if not any(n in design.nets for n in _expand((pat,), sorted(design.nets))):
            refusals.append(f'NetReq("{first}") line {nline}: keep_clear_of names {pat!r}, and no net on this board matches it')
    if req.keep_clear_of:
        mm = req.keep_clear_mm if req.keep_clear_mm is not None else KEEP_CLEAR_DEFAULT_MM
        ksrc = (
            Source("NetReq", f"line {nline}")
            if req.keep_clear_mm is not None
            else (Source("NetReq", f"line {nline}", "3 mm when keep_clear_of is given, pcbc default") if is_pair else Source("preset", kind, "3 mm when keep_clear_of is given"))
        )
        for other in _expand(tuple(req.keep_clear_of), sorted(design.nets)):
            if other not in nets:
                keep.append(KeepAway(other, mm, ksrc))
        keep.sort(key=lambda k: k.other)

    # loops
    loop: Derived | None = None
    if kind in DEFAULT_LOOP_MM2:
        loop = _preset(DEFAULT_LOOP_MM2[kind], "mm2", kind, LOOP_NOTE[kind])

    if kind == "power":
        soft.append("track_width")
    if kind == "clock":
        soft += ["skew", "via_budget"]

    # `diff_pair` marks a pair, routed as one object (every pair is compiled from its Pair line, PR 6).
    autoroute: bool | str = "diff_pair" if is_pair else True

    class_base = req.class_name or preset.class_name

    return _Numbers(
        kind="pair" if bare_pair else kind,
        class_base=class_base,
        line=line,
        width=width,
        clearance=clearance,
        lane_clearance=lane,
        via=via,
        layers=layers,
        reference=reference,
        reference_name=reference_name,
        pair_geometry=pair_geometry,
        airwire=airwire,
        airwire_src=airwire_src,
        length=length,
        length_none=length_none,
        keep_away=tuple(keep),
        spacing=preset.spacing_w,
        voltage=voltage,
        loop=loop,
        rise_ps=req.rise_ps,
        autoroute=autoroute,
        soft=tuple(dict.fromkeys(soft)),
        notes=tuple(notes),
        width_line=width_line,
        via_src=via_src,
        layers_src=layers_src,
        pair_of=pair_of,
        pair_length=pair_req.length_mm if (is_pair and pair_req is not None) else None,
    )


# ---------------------------------------------------------------------------------------------
# Isolation: sides from Place() lines, no geometry
# ---------------------------------------------------------------------------------------------


def _part_sides(design: Design) -> dict[str, str | None]:
    """ref -> Region name from Place(parent=) or inherited through Place(to="REF.PIN"); None otherwise."""
    place_of = {p.ref: p for p in design.places}
    sides: dict[str, str | None] = {}
    for inst in design.instances:
        ref = inst.ref
        seen: set[str] = set()
        side: str | None = None
        cur = ref
        while cur in place_of and cur not in seen:
            seen.add(cur)
            p = place_of[cur]
            if p.parent:
                side = p.parent
                break
            if p.to and "." in p.to:
                cur = p.to.split(".", 1)[0]
                continue
            break
        sides[ref] = side
    return sides


def _side_root(name: str | None, region_parent: dict[str, str | None], roots: tuple[str, str]) -> str | None:
    seen: set[str] = set()
    while name is not None and name not in seen:
        if name in roots:
            return name
        seen.add(name)
        name = region_parent.get(name)
    return None


def _compile_isolation(
    design: Design,
    stack: Stackup,
    board,
    iso: IsolationReq,
    rects: dict,
    region_parent: dict[str, str | None],
    sides: dict[str, str | None],
    pads_by_net: dict[str, list[tuple[str, str]]],
    refusals: list[str],
) -> tuple[IsolationSpec, RuleArea | None]:
    tag = f"Isolation {iso.a}/{iso.b}"
    across = set(iso.across)
    side_of: dict[str, str | None] = {}
    for inst in design.instances:
        if inst.ref in across:
            side_of[inst.ref] = "both"
            continue
        side_of[inst.ref] = _side_root(sides.get(inst.ref), region_parent, (iso.a, iso.b))
        if side_of[inst.ref] is None:
            bname = pathlib.Path(design.source).name if design.source else "board.py"
            refusals.append(f"{tag} ({bname}:{iso.line}): {inst.ref} is on neither side: no Place(parent=) in layout.core.py puts it in {iso.a} or {iso.b}, and it is not in across=")
    nets_a: list[str] = []
    nets_b: list[str] = []
    for net in sorted(pads_by_net):
        on_a = sorted((ref, pad) for ref, pad in pads_by_net[net] if side_of.get(ref) == iso.a)
        on_b = sorted((ref, pad) for ref, pad in pads_by_net[net] if side_of.get(ref) == iso.b)
        carried = any(side_of.get(ref) == "both" for ref, _pad in pads_by_net[net])
        if on_a and on_b:
            where = f"({on_a[0][0]}.{on_a[0][1]} {iso.a}, {on_b[0][0]}.{on_b[0][1]} {iso.b})"
            tail = "and no across= part carries it: " if not carried else ""
            refusals.append(f"{tag} line {iso.line}: {net} has pads on both sides {where} {tail}that is a short across the barrier")
            continue
        if on_a:
            nets_a.append(net)
        elif on_b:
            nets_b.append(net)

    # numbers (C.8)
    ipc = ipc2221_clearance_mm(iso.volts, "B2")
    iec = iec_clearance_mm(iso.volts, reinforced=iso.reinforced)
    label = _ipc2221_row_label(iso.volts)
    rein = ", reinforced" if iso.reinforced else ""
    if iec >= ipc:
        clearance = Derived(iec, "mm", Source("iec60664_f2", f"IEC 60664-1 F.1 OV II + F.2 PD2 {_g(iso.volts)} V{rein}", f"over ipc2221_6_1 row {label} V B2 {_g(ipc)}"))
    else:
        clearance = Derived(ipc, "mm", Source("ipc2221_6_1", f"IPC-2221B 6-1 row {label} V B2", f"over iec60664_f2 {_g(iec)}"))
    creep = Derived(iec_creepage_mm(iso.volts, "IIIa", reinforced=iso.reinforced), "mm", Source("iec60664_f5", f"IEC 60664-1 F.5 row {_g(_creepage_row_v(iso.volts))} V IIIa PD2{rein}"))
    if iso.slot:
        gap_min = round(max(clearance.value, 1.0, creep.value - 2 * stack.board_mm), 4)
    else:
        gap_min = max(clearance.value, creep.value)

    # the corridor between the two Regions
    ra, rb = rects[iso.a], rects[iso.b]
    # The corridor's along-span is the outline (docs/board-outline.md B.3 step 5): the declared one here; in derived
    # mode the derived frame, which the place stage replaces with R and the fit with O (`respan_rule_areas`).
    from .layout import declared_outline

    rect = declared_outline(design)[0]
    ox0, oy0, ox1, oy1 = rect if rect is not None else (0.0, 0.0, 0.0, 0.0)
    area: RuleArea | None = None
    axis = ""
    if ra.x1 <= rb.x0 or rb.x1 <= ra.x0:
        axis = "x"
        left, right = (ra, rb) if ra.x1 <= rb.x0 else (rb, ra)
        box = (round(left.x1, 4), float(oy0), round(right.x0, 4), float(oy1))
    elif ra.y1 <= rb.y0 or rb.y1 <= ra.y0:
        axis = "y"
        top, bottom = (ra, rb) if ra.y1 <= rb.y0 else (rb, ra)
        box = (float(ox0), round(top.y1, 4), float(ox1), round(bottom.y0, 4))
    else:
        refusals.append(f"{tag} line {iso.line}: Regions overlap in both axes; no straight line separates them")
        box = None
    if box is not None:
        layers = ("F&B.Cu",) if stack.layers == 2 else ("F&B.Cu",) + tuple(lay for lay in stack.copper_layers() if not is_outer(lay))
        area = RuleArea(f"ISO_{iso.a}_{iso.b}", box, layers, ("track", "via", "zone"), f"{tag} {_g(iso.volts)} V line {iso.line}")
    across_nets = sorted({net for net, sites in pads_by_net.items() for ref, _pad in sites if side_of.get(ref) == "both"})
    spec = IsolationSpec(iso, tuple(nets_a), tuple(nets_b), clearance, creep, gap_min, axis, tuple(across_nets))
    return spec, area


# ---------------------------------------------------------------------------------------------
# The report: one number per line, sorted by net
# ---------------------------------------------------------------------------------------------


def _report(cs: ConstraintSet, design: Design, numbers, notes_lines: list[str], has_switch_node: bool, path_lines: dict | None = None) -> list[str]:
    by_net_num: dict[str, _Numbers] = {}
    for _idx, nets, num in numbers:
        for n in nets:
            by_net_num.setdefault(n, num)
    lines: list[str] = []
    seen_notes: set[str] = set()
    stack = cs.stackup
    for c in cs.constraints:
        num = by_net_num.get(c.net)
        n = c.net
        pr_of = next((x for x in design.pairs if x.line == c.line and c.net in (x.p, x.n)), None) if c.kind == "pair" else None
        preset_of = (pr_of.kind or None) if pr_of is not None else ("usb_hs" if c.kind == "pair" else c.kind)
        if c.pair is not None and num is not None and num.width_line:
            lines.append(f"{n}: pair with {c.pair.partner}, {num.width_line}")
        else:
            lines.append(f"{n}: width {c.width_mm.line()}")
        lines.append(f"{n}: clearance {c.clearance_mm.line()}")
        v = c.via
        if num is not None:
            via_src = num.via_src
        else:  # synthesised from a Bus / Path / Guard alone: the stackup via
            via_src = f"stackup {stack.name}" if (v.diameter_mm, v.drill_mm) == (stack.via_diameter, stack.via_drill) else f"preset {preset_of}"
        if not v.allowed:
            lines.append(f"{n}: no vias ({via_src})")
        else:
            line = f"{n}: via {_g(v.diameter_mm)}/{_g(v.drill_mm)} mm ({via_src})"
            if v.count_max is not None:
                budget_src = f"Pair line {pr_of.line}" if (pr_of is not None and pr_of.vias_max is not None) else f"preset {preset_of}"
                line += f", at most {v.count_max} ({budget_src}){SOFT}"
            lines.append(line)
        if c.pair is not None:
            lines.append(f"{n}: skew {c.pair.skew_mm.line()}{SOFT}")
            lines.append(f"{n}: uncoupled {c.pair.uncoupled_mm.line()}{SOFT}")
        if c.length_max_mm is not None:
            lines.append(f"{n}: length {c.length_max_mm.line()}")
        elif num is not None and num.length_none and not any(p.length_mm is not None for p in c.paths):
            lines.append(f"{n}: length none (no length_mm= on Pair line {c.line} or on a Path of {n})")
        if c.airwire_max_mm is not None:
            src = num.airwire_src if num is not None and num.airwire_src is not None else Source("preset", c.kind)
            inner = f"{src.formula} {src.ref}" + (f"; {src.note}" if src.note else "")
            lines.append(f"{n}: airwire {_g(c.airwire_max_mm)} mm ({inner})")
        if c.keep_away:
            for k in c.keep_away:
                lines.append(f"{n}: keep_clear_of {k.other} {_g(k.mm)} mm ({k.source.formula} {k.source.ref}{'; ' + k.source.note if k.source.note else ''})")
        elif c.kind in KEEP_AWAY_KINDS:
            hint = ""
            if has_switch_node:
                sw = sorted(x.net for x in cs.constraints if x.kind == "switch_node")
                hint = f"; this board has a switch node ({', '.join(sw)})" if sw else ""
            lines.append(f"{n}: keep_clear_of none{hint}")
        if c.group is not None:
            g = c.group
            if g.kind == "bus":
                to = f" matched to {g.clock}" if g.clock else ""
                lines.append(f"{n}: bus {', '.join(g.members)}{to} within {_g(g.match_mm or 0)} mm ({g.source}){SOFT}")
        if path_lines and path_lines.get(n):
            lines.extend(path_lines[n])
        # KD 12 / O3: a KiCad rule selects copper by net or class, never the copper between two pads, so
        # a path's numbers reach KiCad only when one Path is the whole node. Every other fielded path is
        # checked by pcbc's copper gate alone, and the report says so.
        whole = c.whole_node_path()
        for p in c.paths:
            if not p.fielded or p is whole:
                continue
            lines.append(
                f"{n}: KiCad does not check Path line {p.line}'s copper (width, vias, layers): a KiCad rule selects copper by net or class, "
                f"never the copper between two pads; pcbc's copper gate checks it on the emitted board"
            )
        if c.voltage is not None and c.voltage.creepage_mm is not None:
            lines.append(f"{n}: creepage {c.voltage.creepage_mm.line()}")
        if c.loop_mm2 is not None:
            lines.append(f"{n}: loop {c.loop_mm2.line()}")
        if c.guard_stitch_mm is not None:
            gd = next((g for g in design.guards if g.net == n), None)
            lines.append(
                f"{n}: guard stitch {_g(c.guard_stitch_mm)} mm in {c.guard_ground or '?'} "
                f"(Guard line {gd.line if gd else 0}; R2 pattern)"
            )
        if c.isolation_side is not None:
            iso = next((i for i in design.isolations if c.isolation_side in (i.a, i.b)), None)
            lines.append(f"{n}: isolation side {c.isolation_side} (Isolation {iso.a}/{iso.b} line {iso.line})" if iso else f"{n}: isolation side {c.isolation_side}")
        lines.append(f"{n}: layers {', '.join(c.layers)} ({num.layers_src if num is not None else 'preset generic'})")
        lines.append(f"{n}: spacing {c.spacing_w}W ({f'preset {preset_of}' if preset_of else 'a bare Pair: 3W, pcbc default'})")
        for note in c.notes:
            if note not in seen_notes:
                seen_notes.add(note)
                lines.append(note)
    # R-Z4's **C** (`docs/stitch-plan.md` S5): one line per net the compiler gave a reference, saying
    # what a layer change does to its return current and what edit would change the answer. It sits
    # here, after the per-net numbers and before the board-level specs, because it *is* a statement
    # about a net — and it is printed for `kept` and `pinned` too, because "nothing to report" and
    # "nothing went wrong" read the same in an empty report and are not the same fact.
    lines.extend(r.line() for r in return_rules(cs))
    for spec in cs.thermals:
        lines.append(spec.line())
    for spec in cs.bridges:
        tag = f"Bridge {spec.a}/{spec.b}"
        where = f"Bridge line {spec.req.line}"
        if spec.kind == "off_board":
            lines.append(f"{tag}: tied off this board at {' and '.join(spec.at)} ({where}; {spec.why}); pcbc writes no copper for it")
        elif spec.pads:
            lines.append(f"{tag}: tied at {spec.tie_ref} kind={spec.kind}, {spec.pads[0]} on {spec.a} and {spec.pads[1]} on {spec.b} ({where}); pcbc writes no copper for it")
        else:
            lines.append(f"{tag}: tied at {spec.tie_ref} kind={spec.kind} ({where}); pcbc writes no copper for it")
    for spec in cs.isolation_specs:
        tag = f"Isolation {spec.req.a}/{spec.req.b}"
        lines.append(f"{tag}: clearance {spec.clearance_mm.line()}")
        if spec.req.slot:
            lines.append(f"{tag}: creepage {spec.creepage_mm.line()}; slot=True: the contour gap + 2 x board {_g(stack.board_mm)} mm satisfies it, gap >= {_g(spec.gap_min_mm)} mm")
        else:
            lines.append(f"{tag}: creepage {spec.creepage_mm.line()}")
    for lp in cs.loops:
        src = f"{lp.budget.source.formula}{'; ' + lp.budget.source.note if lp.budget.source.note else ''}" if lp.budget.source.formula != "Loop" else f"Loop line {lp.line}"
        lines.append(f"loop {' -> '.join(lp.written)} (Loop line {lp.line}): {_g(lp.budget.value)} mm2 ({src})")
    lines.extend(notes_lines)
    lines.append("classes: " + ", ".join(_class_summary(c, stack) for c in cs.classes))
    return lines


def _class_summary(c: CompiledClass, stack: Stackup) -> str:
    s = f"{c.name} {_g(c.track_width_mm)}/{_g(c.clearance_mm)}"
    if c.diff_pair_width_mm is not None:
        s += f" pair {_g(c.diff_pair_width_mm)}/{_g(c.diff_pair_gap_mm or 0)}"
    if (c.via_diameter_mm, c.via_drill_mm) != (stack.via_diameter, stack.via_drill):
        s += f" via {_g(c.via_diameter_mm)}/{_g(c.via_drill_mm)}"
    return s



# ---------------------------------------------------------------------------------------------
# A.3 the clearance table: every distance requirement the router asks for, from one place
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class ClearanceTable:
    """Every number a routing decision needs, so the router cannot drift from the rules KiCad is
    handed (`docs/r2-design.md` A.3).

    `between(a, b)` is a **max** over the requirements that apply to the pair, and the winner names
    itself in the `why` string, so a refusal can say which rule it came from rather than a bare
    number. It is deliberately at least every `clearance` and `creepage` rule `dru.py` writes for
    that pair — `test_clearance_table_covers_every_written_rule` walks `dru.rules(cs)` on every
    board and asserts exactly that — with one exemption, named here because it is the only rule in
    the file that exists to **lower** a number: `pads_of_one_footprint` is written last, after every
    clearance rule, precisely so a connector's own 0.1 mm land beats the 0.155-0.2 class clearances,
    and KiCad's later-rule-wins precedence makes it stick. Taking the max cannot model a lowering
    rule and does not need to: it is pad to pad, and pcbc never draws a pad.

    Being the max also means the table ignores the `NOT_OWN_PADS` exemption the written rules carry.
    That is the safe direction — a candidate refused near a footprint's own two pads costs a fit and
    prints a move, and never the other way round.
    """

    cs: ConstraintSet
    _by_net: dict = field(default_factory=dict, repr=False, compare=False)
    _class_away: dict = field(default_factory=dict, repr=False, compare=False)
    _class_creepage: dict = field(default_factory=dict, repr=False, compare=False)
    _default_clearance: float = 0.0

    # -- the pair question ---------------------------------------------------------------------

    def between(self, net_a: str, net_b: str) -> tuple[float, str]:
        """(mm, why) for copper on `net_a` against copper on `net_b`."""
        if net_a and net_a == net_b:
            return (0.0, "same net")
        # KiCad resolves a clearance from the two items' own net classes and gives an unassigned
        # net the Default class, so the baseline is the fab floor and each net contributes its own
        # class — not Default. Starting from Default instead reads two USB nets (0.155) as 0.16 and
        # refuses c3_usb's own routed pair at 0.159, which KiCad passes.
        best = (self.cs.stackup.clearance_min, "stackup floor")
        for net in (net_a, net_b):
            c = self._by_net.get(net)
            if c is not None:
                v = max(c.clearance_mm.value, self.cs.stackup.clearance_min)
                if v > best[0]:
                    best = (v, f"class {c.class_name}")
            elif self._default_clearance > best[0]:
                best = (self._default_clearance, "Default class")
        # Keep-aways are written per class in `dru.py` (E.4), so they apply to every net of the
        # class, not only to the net whose NetReq carried the line.
        for a, b in ((net_a, net_b), (net_b, net_a)):
            ca = self._by_net.get(a)
            if ca is None:
                continue
            v = self._class_away.get((ca.class_name, b))
            if v is not None and v > best[0]:
                best = (v, f"{ca.class_name} keep-away from {b}")
        # Class creepage (E.6): the written rules are "this class against any other net" and, since F1
        # (physics-subjects PR 4), "this class against another net of the same class"; the table covers both.
        # Enforcing a creepage as a straight-line distance is conservative: the surface path between
        # two pieces of copper is never shorter than the air between them.
        for a, b in ((net_a, net_b), (net_b, net_a)):
            ca = self._by_net.get(a)
            cb = self._by_net.get(b)
            if ca is None or not b:
                continue
            # docs/physics-subjects.md §3.9 (F1, PR 4): two nets of one class at 60 V or more get the class's
            # creepage between them too (NetReq("HV_P", "HV_N", volts=400) may be 800 V apart).
            v = self._class_creepage.get(ca.class_name)
            if v is not None and v > best[0]:
                best = (v, f"creepage {ca.class_name}")
        for spec in self.cs.isolation_specs:
            sides = ((spec.nets_a, spec.nets_b), (spec.nets_b, spec.nets_a))
            if any(net_a in x and net_b in y for x, y in sides):
                tag = f"isolation {spec.req.a}/{spec.req.b}"
                if spec.clearance_mm.value > best[0]:
                    best = (spec.clearance_mm.value, tag)
                across = set(spec.across_nets)
                if not spec.req.slot and net_a not in across and net_b not in across and spec.creepage_mm.value > best[0]:
                    best = (spec.creepage_mm.value, f"{tag} creepage")
        return (round(best[0], 4), best[1])

    # -- the board-wide numbers ----------------------------------------------------------------

    def edge(self) -> float:
        """Copper to the nominal Edge.Cuts line. KiCad ignores the outline's stroke (A.4 rule 5)."""
        return self.cs.stackup.edge_clearance

    def hole_to_copper(self) -> float:
        return self.cs.stackup.hole_clearance

    def hole_to_hole(self) -> float:
        """Edge to edge, the way KiCad measures it, which is why two 0.3 mm drills need 0.8 mm
        centre to centre and `clears(hole, hole, hole_to_hole())` gets that for free (A.4 rule 3)."""
        return self.cs.stackup.hole_to_hole

    def via_to_same_net_smd_pad(self) -> float:
        """A via in a passive's own pad wicks solder and the fab stage refuses it; the number a via keeps
        from a same-net SMD pad."""
        return self.cs.stackup.clearance_min

    def mask_bridge(self) -> float:
        """A.4 rule 4, advisory in R2: a candidate that fails only this is accepted with a note."""
        return self.cs.stackup.mask_bridge_min

    def via_pitch(self, net_a: str, net_b: str, drill_a: float, dia_a: float, drill_b: float, dia_b: float) -> float:
        """Centre to centre for two vias: the holes keep `hole_to_hole` edge to edge and the rings
        keep `between`, whichever is larger. `stackup.fanout_stagger` is this same arithmetic on a
        row of a given pitch, and it is the number the fanout's staggered rows already obey."""
        holes = self.hole_to_hole() + (drill_a + drill_b) / 2.0
        rings = self.between(net_a, net_b)[0] + (dia_a + dia_b) / 2.0
        return round(max(holes, rings), 4)


def clearance_table(cs: ConstraintSet) -> ClearanceTable:
    """Build the table once per board. Pure; every lookup below is a dict built here, in sorted
    order, so nothing in a decision path iterates a dict whose order could move."""
    by_net = {c.net: c for c in cs.constraints}
    away: dict[tuple[str, str], float] = {}
    creepage: dict[str, float] = {}
    for c in sorted(cs.constraints, key=lambda c: c.net):
        for k in c.keep_away:
            key = (c.class_name, k.other)
            away[key] = max(away.get(key, 0.0), k.mm)
        if c.voltage is not None and c.voltage.creepage_mm is not None:
            creepage[c.class_name] = max(creepage.get(c.class_name, 0.0), c.voltage.creepage_mm.value)
    default = next((c.clearance_mm for c in cs.classes if c.name == "Default"), cs.stackup.clearance_min)
    return ClearanceTable(
        cs=cs,
        _by_net=by_net,
        _class_away=away,
        _class_creepage=creepage,
        _default_clearance=max(default, cs.stackup.clearance_min),
    )


def respan_rule_areas(cs: ConstraintSet, outline) -> ConstraintSet:
    """`cs` with each isolation corridor's along-span drawn to `outline` (x0, y0, x1, y1): R while placing and routing,
    O after the fit (docs/board-outline.md B.3 step 5). The across-span stays the two Regions'. A declared outline
    gives the boxes compile drew, unchanged."""
    if cs is None or not cs.rule_areas:
        return cs
    x0, y0, x1, y1 = outline
    axis = {f"ISO_{s.req.a}_{s.req.b}": s.axis for s in cs.isolation_specs}
    areas = []
    for a in cs.rule_areas:
        ax = axis.get(a.name)
        if ax == "x":
            a = replace(a, box=(a.box[0], float(y0), a.box[2], float(y1)))
        elif ax == "y":
            a = replace(a, box=(float(x0), a.box[1], float(x1), a.box[3]))
        areas.append(a)
    return replace(cs, rule_areas=tuple(areas))
