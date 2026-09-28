"""Constraints: every NetReq / Pair / Bus / Chain / Isolation / Guard line turned into numbers with
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
from dataclasses import asdict, dataclass, field, replace

from .layout import resolve_regions
from .model import BridgeReq, BusReq, ChainReq, Design, GuardReq, IsolationReq, NetReqSpec, PairReq, ThermalReq
from .stackup import (
    IPC2221_TABLE_6_1,
    IEC60664_TABLE_F5_PD2,
    PAIR_FIT_MM,
    Derived,
    Source,
    Stackup,
    capacitance_pf_per_mm,
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
COMMON_KWARGS = ("max_mm", "layers", "vias", "autoroute", "class_name", "keep_clear_of", "keep_clear_mm", "volts", "z_se_ohm", "rise_ps")

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


@dataclass(frozen=True)
class GroupSpec:
    kind: str  # "bus" | "chain"
    members: tuple[str, ...]  # bus: net names (clock included); chain: "REF.PIN" in feed order
    match_mm: float | None  # bus skew budget
    clock: str | None
    line: int = 0  # the Bus / Chain / NetReq line
    source: str = ""  # "preset clock" | "Bus line 12" | "NetReq line 40"


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
    reference: str | None  # reference plane layer for controlled impedance
    z_se: tuple[float, Derived] | None  # (target, computed at width_mm)
    pair: PairSpec | None
    group: GroupSpec | None
    airwire_max_mm: float | None  # today's max_mm: a placement budget, never a KiCad rule
    length_max_mm: Derived | None  # routed length -> KiCad length (max)
    keep_away: tuple[KeepAway, ...]
    spacing_w: int  # 3 or 5; printed in R1, a router cost in R3
    current: CurrentSpec | None
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
    autoroute: bool | str  # `diff_pair` marks a pair (routed as one object); False a sensitive preset, whose layer/via constraint the router honours
    soft: tuple[str, ...]  # rule kinds written as warnings in R1: subset of SOFT_RULES
    notes: tuple[str, ...]  # honest caveats, e.g. the 2-layer USB line
    req_index: int = -1  # index into Design.netreqs; -1 when synthesised from Pair/Bus alone


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

    out: list[ThermalSpec] = []
    seen: dict[str, int] = {}
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
        seen[th.pad] = th.line
        # R-M4, and it is the one refusal in this statement that no amount of fab money buys off: a
        # via inside a two-terminal passive's pad wicks the joint whatever the barrel is filled with,
        # and `fab.via_in_pad_blockers` refuses the board for it. Refused here so the author reads it
        # from `pcbc check` instead of from a fab gate three stages later, and `via_in_pad_blockers`
        # keeps a zero diff — it must stay the judge, not become a list this statement edits.
        if ref in passive_refs(design):
            refusals.append(
                f"{who}: {ref} is a two-terminal passive, and a via inside a passive's pad wicks the joint "
                f"whatever the fab fills it with (fab.via_in_pad_blockers refuses the board for it). "
                f"Drop the line; a passive that has to shed heat sheds it through its own copper, so widen "
                f"the track with NetReq(\"{net}\", amps=...) instead"
            )
            continue
        # The array's whole job is to reach a plane. Without one the vias land in nothing: on a
        # two-layer board whose GND is not a power net the route stage writes no back pour at all
        # (`route_scene.plane_targets`), and sixteen stitch vias would go into a pour never written.
        plane = next((lay for pnet, lay in pours if pnet == net), "")
        if not plane:
            refusals.append(
                f"{who}: {net} has no plane or pour to reach, so an array under {th.pad} would be "
                f"{th.watts:g} W of vias into bare laminate. "
                + (
                    f'Board(planes=[("{net}", "In1.Cu")]) gives it one'
                    if stack.layers > 2
                    else f'NetReq("{net}", kind="power") is what gives it the back pour (route_scene.plane_targets)'
                )
            )
            continue
        # A barrel that is not plugged is a hole under a reflowed pad. `Stackup.via_tenting` closes
        # the mask over it and leaves the tube open underneath, which is the distinction `via_fill`
        # exists to carry.
        if stack.via_fill == "none":
            refusals.append(
                f'{who}: stackup "{stack.name}" does not fill its vias, and tenting is a mask dam rather '
                f"than a plug — solder wicks down an open barrel and starves the joint. "
                f"Board(stackup=...) on a stackup whose via_fill is set is the edit"
            )
            continue
        if not (stack.via_fill_min_drill - 1e-9 <= stack.via_drill <= stack.via_fill_max_drill + 1e-9):
            refusals.append(
                f'{who}: stackup "{stack.name}" fills {stack.via_fill_min_drill:g} to {stack.via_fill_max_drill:g} mm '
                f"and its standard via is {stack.via_drill:g} mm, so these barrels cannot be plugged. "
                f"Board(stackup=...) on a stackup whose standard via is inside that range is the edit"
            )
            continue
        foreign = tuple((pnet, lay) for pnet, lay in pours if pnet != net)
        if foreign and not th.across_planes:
            anti = ", ".join(f"{pnet} on {lay}" for pnet, lay in foreign)
            refusals.append(
                f"{who}: every via here is a through via, so {vias_for_theta(stack.board_mm, stack.via_drill, th.watts, th.rise_c, stack.via_plating_mm)} "
                f"of them punch {vias_for_theta(stack.board_mm, stack.via_drill, th.watts, th.rise_c, stack.via_plating_mm)} antipads in {anti}, "
                f"which this net does not join. That is finding 10 at nine times the scale — one row of taps at a "
                f"footprint's own pitch already slotted a plane and turned a 1.80 mm path across it into 8.09 mm. "
                f"Thermal(\"{th.pad}\", watts={th.watts:g}, across_planes=True) consents to it, and the pitch the array "
                f"is then placed at is derived so the webs survive rather than chosen"
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
    move: str  # ends in a board.py edit, or "" for `kept` / `pinned`, which ask for none

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
        return move_line(self.net, f"the return current crossing {a}->{b}", goal, f"{self.why} ({self.verdict})", self.move)


def _plane_move(stack: Stackup, want_net: str, ref_layers: tuple[str, ...], planes: tuple[tuple[str, str], ...]) -> str:
    """`Board(planes=[...])` with `want_net` under every layer in `ref_layers` — the declaration that
    would make a layer change keep one reference net. Ordered by the stackup's own layer order so the
    edit is byte-identical run to run, and every plane the board already declares that is not one of
    the two is carried through rather than silently dropped."""
    order = stack.copper_layers()
    keep = [(net, lay) for net, lay in planes if lay not in ref_layers]
    out = keep + [(want_net, lay) for lay in ref_layers]
    body = ", ".join(f'("{net}", "{lay}")' for net, lay in sorted(out, key=lambda p: order.index(p[1])))
    return f"Board(planes=[{body}])"


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
        ref_layers = tuple(dict.fromkeys(r[0] for _lay, r in refs if r))
        fixes = [
            f"{_plane_move(stack, fr[1], ref_layers, planes)} puts one net under both layers, at the cost of {to[1]}'s plane.",
            f"Left as they are, the return wants a capacitor between {fr[1]} and {to[1]} at the crossing, which is Bridge()'s "
            f"shape and not a via's — and Bridge() ties two Ground() nets, so {to[1]} declared with Power() is not one today "
            f"(measured 2026-09-21: Bridge(\"{fr[1]}\", \"{to[1]}\") is refused with 'a bridge ties two grounds').",
        ]
        return ReturnRule(c.net, tuple(c.layers), refs, (a, b), verdict, why, " ".join(fixes))

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
    with_ref = tuple(lay for lay, r in refs if r)
    # `"GND"` where `kept` is None is not invented here: it is the spelling the compiler's own
    # existing refusal for this exact situation already uses ("... wants a plane on In1.Cu;
    # Board(planes=[("GND", "In1.Cu")]) declares it"), so the two moves ask for the same edit.
    want_net = kept[1] if kept else "GND"
    lost_fixes = []
    if with_ref:
        layers_txt = ", ".join(f'"{lay}"' for lay in with_ref)
        lost_fixes.append(f"NetReq(layers=[{layers_txt}]) on line {c.line} keeps it on the layer{'' if len(with_ref) == 1 else 's'} that has one.")
    elif stack.layers <= 2:
        lost_fixes.append('NetReq("GND", kind="power") is what gives this board the back pour this net would reference (route_scene.plane_targets).')
    if stack.layers > 2:
        # One neighbour, not both: `gone` needs *a* plane, and declaring two would be a larger edit
        # than the fault asks for.
        want = stack.plane_below(gone) or stack.plane_above(gone)
        if want:
            lost_fixes.append(f"{_plane_move(stack, want_net, (want,), planes)} gives {gone} one.")
    else:
        lost_fixes.append(
            f'Board(layers=4, stackup="jlcpcb_4l_1oz", planes=[("{want_net}", "In1.Cu")]) gives the net a second '
            f"plane, which is a different board rather than a one-line edit."
        )
    return ReturnRule(c.net, tuple(c.layers), refs, (a, b), verdict, why, " ".join(lost_fixes))


def return_rules(cs: ConstraintSet) -> tuple[ReturnRule, ...]:
    """One rule per net the compiler gave a `Constraint.reference`, in net order.

    Pure in the set and therefore in `board.py`: `cs.planes` is compiled, `cs.stackup` is compiled,
    and nothing here opens a file. `tests/test_return_rules.py` asserts every verdict on all five
    boards with no `layout/` directory present at all.
    """
    return tuple(r for c in cs.constraints if (r := return_rule(cs.stackup, cs.planes, c)) is not None)


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
    refusals: tuple[str, ...]  # what `pcbc check` fails on (a Chain pin off its net, ...)
    lines: tuple[str, ...]  # the report, one number per line, sorted by net
    isolation_specs: tuple[IsolationSpec, ...] = ()  # the numbers behind `isolations`
    bridges: tuple[BridgeSpec, ...] = ()  # one per `Bridge()`; no geometry, see `BridgeSpec`
    thermals: tuple[ThermalSpec, ...] = ()  # one per `Thermal()`; the budget, not the placement
    planes: tuple[tuple[str, str], ...] = ()
    """(net, layer) for every pour this board will **have** — not only what `Board(planes=)` says.

    The same question `route_scene.plane_targets` answers off a `CompiledJob`, asked here because a
    return path is a question about the reference **net** and the ConstraintSet had no net-to-layer
    map at all: `Constraint.reference` is a bare layer name, and on two layers it is not even a layer
    (`"B.Cu pour"`). Without this, classifying a layer change needed a PCB file, and the whole value
    of `docs/stitch-plan.md` S5 is that it does not.

    On four layers it is `Board(planes=)` verbatim. On two it is the back `GND` pour the route stage
    writes when `GND` is a power net — and only that: a two-layer `Board(planes=)` is not read here,
    while `route_scene.plane_targets` (what the route stage pours) takes it on every stackup. The two
    agree on the five boards, none of which declares `planes=` on two layers
    (`tests/test_return_rules.py::test_the_compiled_planes_are_the_route_plans_own`); they differ on a
    two-layer board that does (`tests/fixtures/planes`).

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
    autoroute: bool | str


PRESETS: dict[str, Preset] = {
    "generic": Preset("Default", ("z_se_ohm",), None, None, None, True, None, ("F.Cu", "B.Cu"), None, 3, True),
    "power": Preset("Power", ("amps", "temp_rise_c", "volts", "loop_mm2"), None, 0.20, (0.8, 0.4), True, None, POWER_LAYERS, None, 3, True),
    "analog": Preset("Analog", ("keep_clear_of", "keep_clear_mm"), 0.20, 0.20, (0.6, 0.3), False, None, ("F.Cu",), 25.0, 5, False),
    "switch_node": Preset("SwitchNode", ("loop_mm2", "amps"), 0.30, 0.20, (0.6, 0.3), False, None, ("F.Cu",), 8.0, 3, False),
    "clock": Preset("Clock", ("match_mm", "vias_max"), 0.15, 0.20, (0.6, 0.3), True, 2, ("F.Cu", "B.Cu"), None, 5, True),
    "usb_hs": Preset(
        "USB", ("z_diff_ohm", "pair", "match_mm", "uncoupled_mm", "vias_max", "reference", "length_mm"), None, None, None, True, 2, ("F.Cu", "B.Cu"), None, 3, "diff_pair"
    ),
    "spi": Preset("SPI", ("clock", "match_mm", "length_mm", "z_se_ohm"), None, 0.20, None, True, None, ("F.Cu", "B.Cu"), None, 3, True),
    "i2c": Preset("I2C", ("pf_max",), None, 0.20, None, True, None, ("F.Cu", "B.Cu"), None, 3, True),
    "sense": Preset("Sense", ("match_mm", "keep_clear_of", "keep_clear_mm"), 0.20, 0.20, (0.6, 0.3), False, None, ("F.Cu",), 25.0, 5, False),
    "feedback": Preset("Feedback", ("keep_clear_of", "keep_clear_mm"), 0.20, 0.20, (0.6, 0.3), False, None, ("F.Cu",), 15.0, 5, False),
}

# Preset defaults with no standard behind them (printed "pcbc default") and the ones with a source.
DEFAULT_MATCH_MM = {"clock": 2.0, "usb_hs": 0.5, "spi": 2.5, "sense": 1.0}
MATCH_SOURCE = {"clock": "pcbc default", "usb_hs": "TI usb_layout_basics", "spi": "about 15 ps on FR-4, pcbc default", "sense": "R-A2, pcbc default"}
DEFAULT_LOOP_MM2 = {"power": 6.0, "switch_node": 20.0}
LOOP_NOTE = {"power": "decap loop, pcbc default", "switch_node": "hot loop, pcbc default"}
USB_UNCOUPLED_MM = 2.0
USB_Z_DIFF = 90.0
I2C_PF_MAX = 400.0
KEEP_CLEAR_DEFAULT_MM = 3.0
POWER_DEFAULT_AMPS = 0.2
KEEP_AWAY_KINDS = ("analog", "sense", "feedback")

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
    """'; did you mean X?' when one of the board's nets is close to what was written."""
    import difflib

    near = difflib.get_close_matches(name, net_names, n=1, cutoff=0.6)
    return f"; did you mean {near[0]!r}?" if near else ""


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


def _reference_for(stack: Stackup, layer: str, planes: tuple[tuple[str, str], ...]) -> tuple[float, str, str] | None:
    """(height, reference layer, printed name) for a track on `layer`: the adjacent declared plane
    ("In1.Cu (GND)"), or on a 2-layer board the B.Cu pour the route plan always writes."""
    found = stack.height_to_reference(layer, planes, two_layer_pour=True)
    if found is None:
        return None
    h, ref = found
    if ref == "B.Cu pour":
        return h, ref, "the B.Cu pour"
    net = next(n for n, lay in planes if lay == ref)
    return h, ref, f"{ref} ({net})"


def _given(req: NetReqSpec) -> list[str]:
    """The kwargs the NetReq line wrote, in signature order."""
    out: list[str] = []
    for name in (
        "z_diff_ohm", "z_se_ohm", "volts", "amps", "temp_rise_c", "max_mm", "length_mm", "match_mm", "uncoupled_mm", "pair", "vias",
        "vias_max", "layers", "reference", "keep_clear_of", "keep_clear_mm", "clock", "pf_max", "loop_mm2", "rise_ps", "autoroute", "class_name",
    ):
        v = getattr(req, name)
        if name == "temp_rise_c":
            if v != 10.0:
                out.append(name)
        elif name == "pair":
            if v:
                out.append(name)
        elif name == "keep_clear_of":
            if v:
                out.append(name)
        elif v is not None:
            out.append(name)
    return out


def _kwargs_of(kind: str) -> tuple[str, ...]:
    own = PRESETS[kind].own
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
    z_se: tuple[float, Derived] | None
    pair_geometry: tuple[Derived, Derived, float, Derived, float, Derived, Derived, bool] | None  # (w, g, z_target, z, tol, skew, uncoupled, controlled)
    airwire: float | None
    airwire_src: Source | None
    length: Derived | None
    length_none: bool  # print "length none (give length_mm=)"
    keep_away: tuple[KeepAway, ...]
    spacing: int
    current: CurrentSpec | None
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
    pair_for_req: dict[int, PairReq] = {}  # req index -> the Pair that overrides it
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
    for i, req in enumerate(design.netreqs):
        for n in req_nets[i]:
            if n in owner:
                who, line = owner[n]
                # No Pair is involved when two NetReqs collide: pointing at a Pair's overrides
                # sent the AI somewhere it could not go.
                how = (
                    "(merge them: every net of one NetReq shares every number)"
                    if who == "NetReq"
                    else "(a Pair carries its own z_diff_ohm, match_mm, uncoupled_mm, gap_mm, layers, reference)"
                )
                refusals.append(f"{n}: {who} line {line} and NetReq line {req.line} both name it; say it once {how}")
            else:
                owner[n] = ("NetReq", req.line)
    for pr in design.pairs:
        owners = [owner.get(n) for n in (pr.p, pr.n)]
        req_idx = [i for i, nets in enumerate(req_nets) if pr.p in nets and pr.n in nets]
        if req_idx and KIND_ALIASES.get(design.netreqs[req_idx[0]].kind, design.netreqs[req_idx[0]].kind) == "usb_hs" and req_idx[0] not in pair_for_req:
            pair_for_req[req_idx[0]] = pr
            continue
        named = [(n, o) for n, o in zip((pr.p, pr.n), owners) if o is not None]
        if named:
            n, (who, line) = named[0]
            # The overrides listed here are only available on a usb_hs NetReq; on any other kind a
            # Pair is refused outright, so pointing the AI at them would be a dead end.
            how = (
                "(a Pair overrides only z_diff_ohm, match_mm, uncoupled_mm, gap_mm, layers, reference "
                "on a kind=\"usb_hs\" NetReq)"
                if who == "NetReq"
                else "(one of them, not both)"
            )
            refusals.append(f"{n}: {who} line {line} and Pair line {pr.line} both name it; say it once {how}")
            continue
        owner[pr.p] = owner[pr.n] = ("Pair", pr.line)
        bare_pairs.append(pr)

    # -- per NetReq numbers ------------------------------------------------------------------
    numbers: list[tuple[int, list[str], _Numbers]] = []  # (req index, nets, numbers)
    for i, req in enumerate(design.netreqs):
        kind = KIND_ALIASES.get(req.kind, req.kind)
        first = req.nets[0]
        if kind not in PRESETS:
            refusals.append(f'NetReq("{first}") line {req.line}: unknown kind {req.kind!r}; known: {", ".join(sorted(PRESETS))}')
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
                    "reads the condition as ending there and silently drops every rule. Rename the net."
                )
        if req.class_name and ("'" in req.class_name or '"' in req.class_name):
            refusals.append(
                f'NetReq("{first}") line {req.line}: class_name={req.class_name!r} carries a quote; it goes into '
                "every rule condition as `A.hasNetclass('...')` and silently drops the whole rule file"
            )
        if kind == "usb_hs" and req.z_se_ohm is not None:
            refusals.append(
                f'NetReq("{first}") line {req.line}: kind="usb_hs" does not take z_se_ohm=; '
                "a pair is sized by z_diff_ohm= (the single-ended number follows from it)"
            )
        if kind == "usb_hs" and req.layers and not any(is_outer(lay) for lay in req.layers):
            refusals.append(
                f'NetReq("{first}") line {req.line}: kind="usb_hs" on {", ".join(req.layers)}: R1 sizes coupled pairs on outer '
                'layers only (C.3 is edge-coupled microstrip); layers=["F.Cu"] or ["B.Cu"]'
            )
        if kind == "usb_hs" and len(nets) != 2:
            refusals.append(f'NetReq("{first}") line {req.line}: kind="usb_hs" pairs exactly two nets; got {len(nets)}')
        if kind == "usb_hs" and len(nets) == 2 and not _kicad_pairs(nets[0], nets[1]):
            refusals.append(f'NetReq("{first}", "{nets[1]}") line {req.line}: KiCad pairs names ending P/N, _P/_N or +/-; rename {nets[0]}/{nets[1]}')
        if kind == "spi" and len(nets) > 1:
            if req.clock is None:
                refusals.append(f'NetReq("{first}") line {req.line}: kind="spi" with {len(nets)} nets needs clock=; which one is the clock?')
            elif req.clock not in nets:
                refusals.append(f'NetReq("{first}") line {req.line}: clock "{req.clock}" is not one of {", ".join(nets)}')
        num = _compile_req(
            design, stack, planes, req, kind, nets, pair_for_req.get(i), pads_by_net, refusals, has_switch_node,
            floor_w, floor_w_src, class_clear, floor_clear, floor_clear_src,
        )
        numbers.append((i, nets, num))

    # -- bare Pairs: usb_hs's pair logic with the Pair's numbers, class Pair_<p> ---------------
    for pr in bare_pairs:
        if not _kicad_pairs(pr.p, pr.n):
            refusals.append(f"Pair line {pr.line}: KiCad pairs names ending P/N, _P/_N or +/-; rename {pr.p}/{pr.n}")
        req = NetReqSpec(nets=(pr.p, pr.n), kind="usb_hs", z_diff_ohm=pr.z_diff_ohm, pair=True, line=pr.line, class_name=f"Pair_{pr.p}")
        num = _compile_req(
            design, stack, planes, req, "usb_hs", [pr.p, pr.n], pr, pads_by_net, refusals, has_switch_node,
            floor_w, floor_w_src, class_clear, floor_clear, floor_clear_src, bare_pair=True,
        )
        numbers.append((-1, [pr.p, pr.n], num))

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
                z_se=num.z_se,
                pair=_pair_spec(num, net),
                group=None,
                airwire_max_mm=num.airwire,
                length_max_mm=num.length,
                keep_away=num.keep_away,
                spacing_w=num.spacing,
                current=num.current,
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

    # -- groups: the NetReq's own bus, then Bus() and Chain() ------------------------------------
    groups: list[GroupSpec] = []
    for idx, nets, num in numbers:
        if idx < 0:
            continue
        req = design.netreqs[idx]
        bus = _preset_bus(num.kind, nets, req)
        if bus is not None:
            groups.append(bus)
            for net in nets:
                constraints[net] = replace(constraints[net], group=bus)

    def synthesised(net: str) -> None:
        if net in constraints:
            return
        num = _compile_req(
            design, stack, planes, NetReqSpec(nets=(net,), kind="generic"), "generic", [net], None, pads_by_net, refusals, has_switch_node,
            floor_w, floor_w_src, class_clear, floor_clear, floor_clear_src,
        )
        constraints[net] = Constraint(
            net=net, kind="generic", class_name="Default", line=0, width_mm=num.width, clearance_mm=num.clearance,
            lane_clearance_mm=num.lane_clearance, via=num.via, layers=num.layers, reference=None, z_se=None, pair=None, group=None,
            airwire_max_mm=None, length_max_mm=None, keep_away=(), spacing_w=3, current=None, voltage=None, loop_mm2=None,
            rise_ps=None, guard_stitch_mm=None, guard_ground=None, isolation_side=None, autoroute=True, soft=(), notes=(), req_index=-1,
        )

    for bus_req in design.buses:
        members = _expand(bus_req.nets, net_names)
        if bus_req.clock is not None and bus_req.clock not in members:
            refusals.append(f'Bus line {bus_req.line}: clock "{bus_req.clock}" is not one of {", ".join(members)}')
        spec = GroupSpec("bus", tuple(members), bus_req.match_mm, bus_req.clock, bus_req.line, f"Bus line {bus_req.line}")
        groups.append(spec)
        for net in members:
            synthesised(net)
            c = constraints[net]
            length = c.length_max_mm
            if bus_req.length_mm is not None:
                length = Derived(round(bus_req.length_mm, 4), "mm", Source("Bus", f"line {bus_req.line}"))
            constraints[net] = replace(c, group=spec, length_max_mm=length)

    for ch in design.chains:
        if ch.net not in design.nets:
            refusals.append(f'Chain("{ch.net}") line {ch.line}: no net "{ch.net}"')
            continue
        ok = True
        for pad in ch.pads:
            if "." not in pad:
                refusals.append(f'Chain("{ch.net}") line {ch.line}: {pad!r}: write REF.PIN, e.g. U1.VIN')
                ok = False
                continue
            ref, pin = pad.split(".", 1)
            if not any(inst.ref == ref for inst in design.instances):
                refusals.append(f'Chain("{ch.net}") line {ch.line}: no part {ref}')
                ok = False
                continue
            net = _refpin_net(design, ref, pin)
            if net is None:
                refusals.append(f'Chain("{ch.net}") line {ch.line}: {ref} has no pin {pin}')
                ok = False
            elif net != ch.net:
                refusals.append(f'Chain("{ch.net}") line {ch.line}: {pad} is on {net}, not {ch.net}')
                ok = False
        if not ok:
            continue
        spec = GroupSpec("chain", tuple(ch.pads), None, None, ch.line, f"Chain line {ch.line}")
        groups.append(spec)
        synthesised(ch.net)
        c = constraints[ch.net]
        if c.group is None or c.group.kind != "bus":
            constraints[ch.net] = replace(c, group=spec)

    # -- guards: the shielded net, the pitch, and the net the shield is made of -------------------
    #
    # Both halves of the statement, because until `docs/stitch-plan.md` S7 only one of them arrived:
    # `ground=` was parsed by `language.Guard`, stored on `GuardReq`, and then dropped here, so the
    # router had no way to write a guard in anything but whatever net a pattern defaulted to. The
    # refusal below is the mirror of the one above it and lives here for the same reason — this is
    # the one place that holds the netlist, and a pattern is handed a scene.
    for gd in design.guards:
        if gd.net not in design.nets:
            refusals.append(f'Guard("{gd.net}") line {gd.line}: no net "{gd.net}"')
            continue
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
                    f'{who}: {name} is declared with {said}(); a bridge ties two grounds — write Ground("{name}")'
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
    if design.isolations:
        region_rects = resolve_regions(board, design.regions)
        region_parent = {r.name: r.parent for r in design.regions}
        sides = _part_sides(design)
        for iso in design.isolations:
            missing = [s for s in (iso.a, iso.b) if s not in region_rects]
            if missing:
                for s in missing:
                    refusals.append(f'Isolation line {iso.line}: no Region "{s}"')
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
    # already expanded. A two-layer `planes=` is not read here (see `ConstraintSet.planes`).
    pours = (
        planes
        if stack.layers > 2
        else ((("GND", "B.Cu"),) if any(c.net == "GND" and c.kind == "power" for c in constraints.values()) else ())
    )
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
    return replace(cs, lines=tuple(_report(cs, design, numbers, notes_lines, has_switch_node)))


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
    return PairSpec(num.pair_of.get(net, ""), w, g, z_target, z, tol, skew, uncoupled, controlled)


def _preset_bus(kind: str, nets: list[str], req: NetReqSpec) -> GroupSpec | None:
    """clock: a bus of the NetReq's nets when > 1; spi: matched to the clock; sense: exactly two."""
    if kind == "clock" and len(nets) > 1:
        return _bus(nets, kind, req, None)
    if kind == "spi" and len(nets) > 1 and req.clock in nets:
        return _bus(nets, kind, req, req.clock)
    if kind == "sense" and len(nets) == 2:
        return _bus(nets, kind, req, None)
    return None


def _bus(nets: list[str], kind: str, req: NetReqSpec, clock: str | None) -> GroupSpec:
    if req.match_mm is not None:
        return GroupSpec("bus", tuple(nets), req.match_mm, clock, req.line, f"NetReq line {req.line}")
    return GroupSpec("bus", tuple(nets), DEFAULT_MATCH_MM[kind], clock, req.line, f"preset {kind}")


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
) -> _Numbers:
    preset = PRESETS[kind]
    line = req.line
    first = req.nets[0]
    notes: list[str] = []
    soft: list[str] = []
    dt = req.temp_rise_c

    # layers (Pair overrides the NetReq's; a bare Pair carries only its own)
    layers: tuple[str, ...] = preset.layers
    layers_src = f"preset {kind}"
    if req.layers is not None:
        layers, layers_src = tuple(req.layers), f"NetReq line {line}"
    if pair_req is not None and pair_req.layers is not None:
        layers, layers_src = tuple(pair_req.layers), f"Pair line {pair_req.line}"
    have = stack.copper_layers()
    if layers == POWER_LAYERS:  # the preset names four; a 2-layer board has two
        layers = tuple(lay for lay in POWER_LAYERS if lay in have)
    written = req.layers is not None or (pair_req is not None and pair_req.layers is not None)
    unknown = [lay for lay in layers if lay not in have] if written else []
    if unknown:
        # This used to reach stackup.plane_below and die with `tuple.index(x): x not in tuple`.
        refusals.append(
            f'NetReq("{first}") line {line}: {", ".join(unknown)}: {stack.name} has {", ".join(have)}'
            if pair_req is None or pair_req.layers is None
            else f'Pair line {pair_req.line}: {", ".join(unknown)}: {stack.name} has {", ".join(have)}'
        )
        layers = tuple(lay for lay in layers if lay in have) or (have[0],)
    layer0 = layers[0] if layers else "F.Cu"
    inner_only = bool(layers) and not any(is_outer(lay) for lay in layers)

    # width
    width: Derived = Derived(floor_w, "mm", floor_w_src)
    if preset.width_mm is not None:
        width = _preset(preset.width_mm, "mm", kind)
    current: CurrentSpec | None = None
    plane = _reference_for(stack, layer0, planes)
    if kind in ("power", "switch_node") and (kind == "power" or req.amps is not None):
        amps = req.amps if req.amps is not None else POWER_DEFAULT_AMPS
        plane_h = plane[0] if plane is not None else None
        w2221 = Derived(ipc2221_width_mm(amps, dt, stack.copper_oz), "mm", Source("ipc2221_ext", f"IPC-2221B eq. 6-2 external {_g(amps)} A {_g(dt)} C {_g(stack.copper_oz)} oz"))
        bm = ipc2152_board_modifier(stack.board_mm)
        pm = ipc2152_plane_modifier(plane_h)
        plane_txt = f"plane {pm:.3f} at {_g(plane_h)} mm {plane[1]}" if plane is not None else "no plane"
        w2152 = Derived(
            ipc2152_width_mm(amps, dt, stack.copper_oz, stack.board_mm, plane_h),
            "mm",
            Source("ipc2152_fit", f"IPC-2152 Fig 5-1, 5-8, 5-11 {_g(amps)} A {_g(dt)} C {_g(stack.copper_oz)} oz x board {bm:.3f} x {plane_txt}", "below 0.274 A the fit extrapolates" if 0 < amps < 0.274 else ""),
        )
        current = CurrentSpec(amps, dt, w2221, w2152, plane_h)
        cur = current_width_mm(amps, dt, stack, plane_h)
        both = f"ipc2221_ext {_g(amps)} A {_g(dt)} C {_g(stack.copper_oz)} oz {w2221.value:.3f}; ipc2152_fit x board {bm:.3f} x {plane_txt} {w2152.value:.3f}"
        if kind == "power":
            floor = 0.4 if amps >= POWER_DEFAULT_AMPS else 0.25
            if floor >= cur.value:
                width = Derived(floor, "mm", Source("pcbc_floor", f"amps {'>=' if amps >= POWER_DEFAULT_AMPS else '<'} 0.2", both))
            else:
                width = cur
        else:
            width = cur if cur.value > preset.width_mm else _preset(preset.width_mm, "mm", kind, both)  # type: ignore[arg-type]
        # C.6: below 0.274 A the IPC-2152 fit extrapolates. The Derived carried that clause and the
        # report line dropped it, so a 0.1 A rail read as if the chart covered it.
        clause = next((part for part in (cur.source.note or "").split("; ") if "extrapolat" in part), "")
        if clause and clause not in (width.source.note or ""):
            width = replace(width, source=replace(width.source, note=f"{width.source.note}; {clause}"))

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
    allowed = preset.vias_allowed if req.vias is None else bool(req.vias)
    count_max = preset.vias_max
    if req.vias_max is not None:
        count_max = int(req.vias_max)
    if not allowed:
        count_max = 0
    via_a: Derived | None = None
    per_change = 1
    if current is not None:
        via_a = Derived(via_amps(via_h, stack.via_plating_mm, dt), "A", Source("via_barrel", f"{_g(via_h)}/{_g(stack.via_plating_mm)} mm", f"IPC-2221B eq. 6-2 internal on the barrel at {_g(dt)} C"))
        per_change = vias_per_change(current.amps, via_h, stack.via_plating_mm, dt)
    via = ViaSpec(via_d, via_h, allowed, count_max, via_a, per_change)

    # controlled impedance: the pair, or a single-ended target
    reference: str | None = None
    reference_name: str | None = None
    reference_src = req.reference
    if pair_req is not None and pair_req.reference is not None:
        reference_src = pair_req.reference
    pair_geometry = None
    width_line: str | None = None
    z_se: tuple[float, Derived] | None = None
    pair_of: dict[str, str] = {}
    wants_reference = kind == "usb_hs" or req.z_se_ohm is not None
    if wants_reference:
        if reference_src is not None:
            reference = reference_src
            net = next((n for n, lay in planes if lay == reference), None)
            reference_name = f"{reference} ({net})" if net else reference
            if stack.layers > 2 and net is None:
                refusals.append(f'{first}: {_g(req.z_diff_ohm or (pair_req.z_diff_ohm if pair_req else USB_Z_DIFF)) if kind == "usb_hs" else _g(req.z_se_ohm)} ohm on {layer0} wants a plane on {reference}; Board(planes=[("GND", "{reference}")]) declares it')
        elif plane is not None:
            reference, reference_name = plane[1], plane[2]
        elif stack.layers > 2 and is_outer(layer0):
            want = stack.plane_below(layer0) or stack.plane_above(layer0)
            z_txt = _g(req.z_diff_ohm or (pair_req.z_diff_ohm if pair_req else USB_Z_DIFF)) if kind == "usb_hs" else _g(req.z_se_ohm)
            refusals.append(f'{first}: {z_txt} ohm on {layer0} wants a plane on {want}; Board(planes=[("GND", "{want}")]) declares it')

    if kind == "usb_hs":
        if len(nets) == 2:
            pair_of = {nets[0]: nets[1], nets[1]: nets[0]}
        z_target = float(req.z_diff_ohm) if req.z_diff_ohm is not None else USB_Z_DIFF
        z_src_who = f"NetReq line {line}" if req.z_diff_ohm is not None else f"preset {kind}"
        gap_given = None
        gap_src = Source("stackup", stack.name, f"gap max(0.15, clearance_min {_g(stack.clearance_min)})")
        if pair_req is not None:
            z_target = float(pair_req.z_diff_ohm)
            z_src_who = f"Pair line {pair_req.line}"
            if pair_req.gap_mm is not None:
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
        tol = 15.0 if not bare_pair else 10.0
        src = Source("hj_coupled_microstrip", f"x {_g(stack.z_bias_diff)} {code}", row_note)
        w_d = Derived(w, "mm", src)
        g_d = Derived(g, "mm", gap_src)
        z_d = Derived(z, "ohm", Source("hj_coupled_microstrip", f"x {_g(stack.z_bias_diff)} {code}", f"{row_note}; target {_g(z_target)} +-{_g(tol)} %"))
        skew_default = DEFAULT_MATCH_MM["usb_hs"]
        skew = _preset(skew_default, "mm", kind, MATCH_SOURCE["usb_hs"])
        uncoupled = _preset(USB_UNCOUPLED_MM, "mm", kind, MATCH_SOURCE["usb_hs"])
        if req.match_mm is not None:
            skew = _override(req.match_mm, "mm", "NetReq", line, kind, _g(skew_default))
        if req.uncoupled_mm is not None:
            uncoupled = _override(req.uncoupled_mm, "mm", "NetReq", line, kind, _g(USB_UNCOUPLED_MM))
        if pair_req is not None:
            skew = _override(pair_req.match_mm, "mm", "Pair", pair_req.line, kind, _g(skew_default))
            uncoupled = _override(pair_req.uncoupled_mm, "mm", "Pair", pair_req.line, kind, _g(USB_UNCOUPLED_MM))
        if bare_pair:
            skew = Derived(pair_req.match_mm, "mm", Source("Pair", f"line {pair_req.line}"))  # type: ignore[union-attr]
            uncoupled = Derived(pair_req.uncoupled_mm, "mm", Source("Pair", f"line {pair_req.line}"))  # type: ignore[union-attr]
        pair_geometry = (w_d, g_d, z_target, z_d, tol, skew, uncoupled, controlled)
        width = Derived(w, "mm", src)
        pair_clear = max(min(0.16, w), floor_clear)
        clearance = Derived(pair_clear, "mm", floor_clear_src if floor_clear >= min(0.16, w) else Source("preset", kind, "min(0.16, pair width)"))
        lane = pair_clear  # the kind number: a voltage row never widens a fanout lane
        if voltage is not None and voltage.clearance_mm.value > pair_clear:
            # D, "volts on any kind": the class clearance is max(kind number, IPC-2221B 6-1 row).
            # This block used to overwrite the row the voltage block had already applied.
            clearance = replace(voltage.clearance_mm, source=replace(voltage.clearance_mm.source, note=f"over preset {kind} {_g(pair_clear)}"))
        over = f" over {reference_name}" if reference_name else ""
        extra = "" if controlled else "; uncontrolled"
        width_line = f"{_g(w)} mm wide, gap {_g(g)} mm on {pair_layer}{over}: {_g(z)} ohm ({z_d.source.formula} {z_d.source.ref}; {z_d.source.note}{extra}; {z_src_who})"
        if not controlled:
            a, b = (nets[0], nets[1]) if len(nets) == 2 else (first, "")
            # The calibration state, not a slice of its sentence: rows -> fitted, a code without
            # rows -> interpolated, neither -> formula only (the 2-layer case).
            cal_word = "fitted" if stack.rows else ("interpolated" if stack.jlc_code else "formula only")
            advice = (
                'fine for USB full speed, use Board(stackup="jlcpcb_4l_1oz") for high speed'
                if stack.layers <= 2
                else f"widen the gap (Pair(gap_mm=)) or take a thinner prepreg "
                f'(Board(stackup="jlcpcb_4l_1oz_1080")) to reach {_g(z_target)} ohm within {_g(PAIR_FIT_MM)} mm members'
            )
            notes.append(
                f"{a}/{b}: {_g(z_target)} ohm needs {_g(solved)} mm members at gap {_g(pair_gap_mm(stack, gap_given))} on {stack.name} "
                f"({cal_word}); pair written at the fab floor {_g(stack.track_min)}/{_g(stack.clearance_min)} = {z:.1f} ohm; {advice}"
            )
        soft += ["track_width", "skew", "via_budget", "diff_pair_uncoupled"]
    elif req.z_se_ohm is not None:
        z_target = float(req.z_se_ohm)
        pour = None
        if stack.layers == 2 and is_outer(layer0):
            pour = max(0.3, 2 * lane)
        w = width_for_z0(z_target, stack, layer=layer0, pour_gap=pour)
        z = microstrip_z0(w, stack, layer=layer0, pour_gap=pour)
        if not is_outer(layer0):
            formula, ref = "wadell_stripline_asym", f"{stack.name} {layer0} (formula only, +-5 %)"
        elif pour is not None:
            formula, ref = "ghione_naldi_cpwg", f"x {_g(stack.z_bias_se)} {stack.jlc_code or stack.name} pour gap {_g(pour)} t = 0"
        else:
            formula, ref = "hj_microstrip", f"x {_g(stack.z_bias_se)} {stack.jlc_code or stack.name}"
        cal = stack.z_bias_source if is_outer(layer0) else "JLC publishes no inner-layer rows"
        width = Derived(w, "mm", Source(formula, ref, cal))
        # A target the fab floor (or 6 mm of copper) cannot reach was printed as if it were met.
        off = abs(z - z_target) / z_target if z_target else 0.0
        if off > 0.02:
            why = (
                f"the fab floor {_g(stack.track_min)} mm stops at {_g(z)} ohm"
                if abs(w - stack.track_min) < 1e-9
                else f"6 mm of copper only reaches {_g(z)} ohm"
                if w >= 5.999
                else f"the solver lands {off * 100:.0f} % off"
            )
            reached = f"; not reached: {why}"
            notes.append(f"{first}: z_se_ohm={_g(z_target)} is not reachable on {stack.name} {layer0}: {why}")
        else:
            reached = ""
        z_se = (z_target, Derived(z, "ohm", Source(formula, ref, f"{cal}; target {_g(z_target)}{reached}")))
        if w > stack.track_min:
            soft.append("track_width")

    # airwire budget (placement), routed length (KiCad)
    airwire = preset.airwire_mm
    airwire_src = Source("preset", kind) if airwire is not None else None
    if req.max_mm is not None:
        airwire = req.max_mm
        airwire_src = Source("NetReq", f"line {line}", f"overrides preset {kind} {_g(preset.airwire_mm)}" if preset.airwire_mm is not None else "")
    length: Derived | None = None
    length_none = False
    if kind in ("usb_hs", "spi") and not bare_pair:
        if req.length_mm is not None:
            length = Derived(round(req.length_mm, 4), "mm", Source("NetReq", f"line {line}"))
        else:
            length_none = True
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
    if req.keep_clear_of:
        mm = req.keep_clear_mm if req.keep_clear_mm is not None else KEEP_CLEAR_DEFAULT_MM
        ksrc = Source("NetReq", f"line {line}") if req.keep_clear_mm is not None else Source("preset", kind, "3 mm when keep_clear_of is given")
        for other in _expand(tuple(req.keep_clear_of), sorted(design.nets)):
            if other not in nets:
                keep.append(KeepAway(other, mm, ksrc))
        keep.sort(key=lambda k: k.other)

    # loops
    loop: Derived | None = None
    if kind in DEFAULT_LOOP_MM2:
        loop = _preset(DEFAULT_LOOP_MM2[kind], "mm2", kind, LOOP_NOTE[kind])
        if req.loop_mm2 is not None:
            loop = _override(round(req.loop_mm2, 1), "mm2", "NetReq", line, kind, _g(DEFAULT_LOOP_MM2[kind]))

    if kind == "power":
        soft.append("track_width")
    if kind == "clock":
        soft += ["skew", "via_budget"]
    if kind == "spi" and len(nets) > 1:
        soft.append("skew")
    if kind == "sense" and len(nets) == 2:
        soft.append("skew")

    autoroute: bool | str = preset.autoroute
    if req.autoroute is not None:
        autoroute = req.autoroute
    if req.pair:
        autoroute = "diff_pair"

    class_base = req.class_name or preset.class_name
    if req.class_name is None and kind == "generic" and req.z_se_ohm is not None:
        class_base = f"Z{int(req.z_se_ohm)}"

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
        z_se=z_se,
        pair_geometry=pair_geometry,
        airwire=airwire,
        airwire_src=airwire_src,
        length=length,
        length_none=length_none,
        keep_away=tuple(keep),
        spacing=preset.spacing_w,
        current=current,
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
            refusals.append(f'{tag} line {iso.line}: {inst.ref} is placed on neither side; Place("{inst.ref}", parent="{iso.a}", ...) or list it in across=')
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
    bw, bh = board.size_mm
    area: RuleArea | None = None
    axis = ""
    if ra.x1 <= rb.x0 or rb.x1 <= ra.x0:
        axis = "x"
        left, right = (ra, rb) if ra.x1 <= rb.x0 else (rb, ra)
        box = (round(left.x1, 4), 0.0, round(right.x0, 4), float(bh))
    elif ra.y1 <= rb.y0 or rb.y1 <= ra.y0:
        axis = "y"
        top, bottom = (ra, rb) if ra.y1 <= rb.y0 else (rb, ra)
        box = (0.0, round(top.y1, 4), float(bw), round(bottom.y0, 4))
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


def _report(cs: ConstraintSet, design: Design, numbers, notes_lines: list[str], has_switch_node: bool) -> list[str]:
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
        preset_of = "usb_hs" if c.kind == "pair" else c.kind
        if c.pair is not None and num is not None and num.width_line:
            lines.append(f"{n}: pair with {c.pair.partner}, {num.width_line}")
        else:
            lines.append(f"{n}: width {c.width_mm.line()}")
        if c.z_se is not None:
            target, z = c.z_se
            over = f" over {num.reference_name}" if num is not None and num.reference_name else ""
            lines.append(f"{n}: {_g(c.width_mm.value)} mm on {c.layers[0] if c.layers else 'F.Cu'}{over}: {z.line()}")
        lines.append(f"{n}: clearance {c.clearance_mm.line()}")
        v = c.via
        if num is not None:
            via_src = num.via_src
        else:  # synthesised from a Bus / Chain / Guard alone: the stackup via
            via_src = f"stackup {stack.name}" if (v.diameter_mm, v.drill_mm) == (stack.via_diameter, stack.via_drill) else f"preset {preset_of}"
        if not v.allowed:
            lines.append(f"{n}: no vias ({via_src})")
        else:
            line = f"{n}: via {_g(v.diameter_mm)}/{_g(v.drill_mm)} mm ({via_src})"
            if v.count_max is not None:
                line += f", at most {v.count_max} ({'NetReq line ' + str(c.line) if _vias_max_given(design, c) else 'preset ' + preset_of}){SOFT}"
            if v.amps is not None:
                line += f", {v.per_change} per layer change (via_barrel {v.amps.source.ref} {_g(v.amps.value)} A at {_g(c.current.temp_rise_c if c.current else 10.0)} C) [report only in R1]"
            lines.append(line)
        if c.pair is not None:
            lines.append(f"{n}: skew {c.pair.skew_mm.line()}{SOFT}")
            lines.append(f"{n}: uncoupled {c.pair.uncoupled_mm.line()}{SOFT}")
        if c.length_max_mm is not None:
            lines.append(f"{n}: length {c.length_max_mm.line()}")
        elif num is not None and num.length_none:
            lines.append(f"{n}: length none (give length_mm=)")
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
                hint = f'; NetReq("{n}", kind="{c.kind}", keep_clear_of="{sw[0]}") holds {_g(KEEP_CLEAR_DEFAULT_MM)} mm' if sw else ""
            lines.append(f"{n}: keep_clear_of none{hint}")
        if c.group is not None:
            g = c.group
            if g.kind == "bus":
                to = f" matched to {g.clock}" if g.clock else ""
                lines.append(f"{n}: bus {', '.join(g.members)}{to} within {_g(g.match_mm or 0)} mm ({g.source}){SOFT}")
            else:
                lines.append(f"{n}: chain {' -> '.join(g.members)} ({g.source})")
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
        lines.append(f"{n}: spacing {c.spacing_w}W (preset {preset_of})")
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
    lines.extend(notes_lines)
    lines.append("classes: " + ", ".join(_class_summary(c, stack) for c in cs.classes))
    return lines


def _vias_max_given(design: Design, c: Constraint) -> bool:
    return c.req_index >= 0 and design.netreqs[c.req_index].vias_max is not None


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
        # Class creepage (E.6): the written rule is "this class against any other net", not "against
        # another class that also carries creepage", and the table has to cover what is written.
        # Enforcing a creepage as a straight-line distance is conservative: the surface path between
        # two pieces of copper is never shorter than the air between them.
        for a, b in ((net_a, net_b), (net_b, net_a)):
            ca = self._by_net.get(a)
            cb = self._by_net.get(b)
            if ca is None or not b:
                continue
            if cb is not None and cb.class_name == ca.class_name:
                continue
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
