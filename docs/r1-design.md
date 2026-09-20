# pcbc R1: constraints and rules

Final specification for phase R1 of `docs/router-plan.md` (sections 5, 6.2, 6.3, 6.6, 8).
Synthesised from three judged designs; every number below was recomputed from the formula
as written here (scratch script, not committed) and every reference is named. Pure Python,
no new dependencies, byte-for-byte reproducible.

Conventions: mm, ohms, amps, volts, degrees C. A `Derived` is a number plus where it came
from; a `Constraint` is the bundle of them for one net; `pcbc check --constraints` prints one
line per number with its source. "Today" means the code on `main` at 2026-09-19 (R0 landed).

Two bugs found while reading, both fixed by this design: `language.NetReq(*nets, kind,
**kwargs)` silently drops any kwarg it does not know (`amp=2` is a 0.2 A net); and
`stackup.microstrip_z0` is the IPC-2141 closed form, whose validity (0.1 < w/h < 2) the JLC
4-layer prepregs leave at every practical width (w/h = 2.6 at 0.2 mm on 1080), while
`jlcpcb_4l_1oz` carries `h_mm 0.12, er 4.5`, a dielectric JLC does not build.

---

## A. Data model

### A.1 `src/pcbc/constraints.py` (new; S2)

```python
from __future__ import annotations
from dataclasses import dataclass, field

@dataclass(frozen=True)
class Source:
    formula: str            # a name from FORMULAS below, or "preset" | "NetReq" | "Pair" | "Bus" | "stackup" | "pcbc_floor" | "class_floor"
    ref: str                # what the formula was evaluated against: "JLC04161H-7628", "IPC-2221B 6-1 row 31-50 V B2",
                            # "IEC 60664-1 F.5 row 250 V IIIa", "usb_hs", "NetReq line 143", "Pair line 262"
    note: str = ""          # free text the report appends: "IPC-2221 floor holds over IPC-2152 0.646"

FORMULAS = (
    "hj_microstrip", "hj_coupled_microstrip", "wadell_stripline", "wadell_stripline_asym", "ghione_naldi_cpwg",
    "ipc2221_ext", "ipc2221_int", "ipc2152_fit", "ipc2221_6_1", "iec60664_f5", "iec60664_f2", "i2c_capacitance",
    "via_barrel",
)

@dataclass(frozen=True)
class Derived:
    value: float            # rounded: mm 4 dp, ohm 2 dp, A 3 dp, mm2 1 dp, pF/mm 4 dp
    unit: str               # "mm" | "ohm" | "A" | "V" | "mm2" | "pF/mm" | "count"
    source: Source

    def line(self) -> str:  # "0.2291 mm (hj_coupled_microstrip x 0.85 JLC04161H-7628; JLC row 0.2332)"
        ...

@dataclass(frozen=True)
class ViaSpec:
    diameter_mm: float
    drill_mm: float
    allowed: bool
    count_max: int | None           # KiCad via_count (max n); None = unlimited
    amps: Derived | None            # one via at the net's temp rise; None when the net has no amps
    per_change: int                 # ceil(amps / amps_per_via); 1 without amps

@dataclass(frozen=True)
class PairSpec:                     # compiled; the language-level object is model.PairReq
    partner: str
    width_mm: Derived
    gap_mm: Derived
    z_diff_ohm: float               # target
    z_computed: Derived             # formula x bias at the chosen geometry (unit "ohm")
    tolerance_pct: float            # 15 for usb_hs, 10 otherwise
    skew_mm: Derived
    uncoupled_mm: Derived
    controlled: bool                # False when the stackup cannot reach the target (2-layer USB)

@dataclass(frozen=True)
class GroupSpec:
    kind: str                       # "bus" | "chain"
    members: tuple[str, ...]        # bus: net names (clock included); chain: "REF.PIN" in feed order
    match_mm: float | None          # bus skew budget
    clock: str | None

@dataclass(frozen=True)
class CurrentSpec:
    amps: float
    temp_rise_c: float
    width_ipc2221: Derived          # external curve, always computed
    width_ipc2152: Derived          # with board-thickness and plane modifiers, always computed
    plane_h_mm: float | None        # dielectric to the nearest reference copper; None = no plane

@dataclass(frozen=True)
class VoltageSpec:
    volts: float
    row: str                        # "B1" | "B2"
    clearance_mm: Derived           # IPC-2221B 6-1 value for the row (before the class max)
    creepage_mm: Derived | None     # IEC 60664-1 F.5 value; only when volts >= CREEPAGE_FROM_V

@dataclass(frozen=True)
class KeepAway:
    other: str                      # a net name (globs expanded before this)
    mm: float
    source: Source

@dataclass(frozen=True)
class Constraint:
    net: str                        # one per real net; globs expanded, sorted
    kind: str                       # preset name (aliases resolved: digital/default -> generic)
    class_name: str
    line: int                       # board.py line of the NetReq (0 when synthesised from Pair/Bus alone)
    width_mm: Derived
    clearance_mm: Derived           # the class clearance (kind number or voltage row, whichever is larger)
    lane_clearance_mm: float        # the kind number alone: what fanout lanes are sized from
    via: ViaSpec
    layers: tuple[str, ...]
    reference: str | None           # reference plane layer for controlled impedance
    z_se: tuple[float, Derived] | None      # (target, computed at width_mm)
    pair: PairSpec | None
    group: GroupSpec | None
    airwire_max_mm: float | None    # today's max_mm: a placement budget, never a KiCad rule
    length_max_mm: Derived | None   # routed length -> KiCad length (max)
    keep_away: tuple[KeepAway, ...]
    spacing_w: int                  # 3 or 5; printed in R1, a router cost in R3
    current: CurrentSpec | None
    voltage: VoltageSpec | None
    loop_mm2: Derived | None        # switch_node hot-loop budget, or the power decap-loop budget
    guard_stitch_mm: float | None
    isolation_side: str | None      # Region name
    autoroute: bool | str           # unchanged meaning for route.py
    soft: tuple[str, ...]           # rule kinds written as warnings in R1: subset of SOFT_RULES
    notes: tuple[str, ...]          # honest caveats, e.g. the 2-layer USB line

@dataclass(frozen=True)
class RuleArea:                     # a named KiCad rule-area zone; compile computes, apply renders
    name: str                       # "ISO_primary_secondary"
    box: tuple[float, float, float, float]
    layers: tuple[str, ...]         # ("F&B.Cu", "In1.Cu", "In2.Cu")
    disallow: tuple[str, ...]       # ("track", "via", "zone")
    reason: str

@dataclass
class DruRule:                      # moved here from compile.py; compile re-exports it
    name: str
    constraint: str
    condition: str
    severity: str = "error"         # KiCad: error | warning | ignore

@dataclass(frozen=True)
class ConstraintSet:
    stackup: "Stackup"
    constraints: tuple[Constraint, ...]      # sorted by net
    classes: tuple["CompiledClass", ...]     # derived, section A.3
    groups: tuple[GroupSpec, ...]
    isolations: tuple["IsolationReq", ...]
    rule_areas: tuple[RuleArea, ...]         # R1: one per Isolation (the corridor between the two Regions)
    canary_net: str | None                   # compile._canary_net(design), carried so dru.py needs no Design
    refusals: tuple[str, ...]                # what `pcbc check` fails on (a Chain pin off its net, ...)
    lines: tuple[str, ...]                   # the report, one number per line, sorted by net

    def by_net(self, name: str) -> Constraint | None: ...
    def to_dict(self) -> dict: ...           # Derived -> {"value", "unit", "formula", "ref", "note"}

CREEPAGE_FROM_V = 60.0      # IEC 62368-1 ES1 boundary: below it no creepage rule is written
PAIR_FIT_MM = 0.25          # a pair member wider than this cannot leave a USB-C's 0.3 mm pads at 0.5 mm pitch
SOFT_RULES = ("track_width", "length_preset", "skew", "via_budget", "diff_pair_uncoupled")

def compile_constraints(design: Design) -> ConstraintSet: ...   # pure: Design in, numbers out; never reads a board file
```

`compile_constraints` never reads a board file. Everything in it is a function of `design`
and the stackup, so `pcbc check --constraints` runs before any geometry exists.

### A.2 `src/pcbc/model.py` (S2)

`NetReqSpec` keeps every existing field and gains:

```python
    length_mm: float | None = None      # routed length -> KiCad length (max)
    uncoupled_mm: float | None = None
    vias_max: int | None = None
    reference: str | None = None        # "In1.Cu"
    clock: str | None = None            # spi: the net the others match to
    pf_max: float | None = None         # i2c bus capacitance budget
    loop_mm2: float | None = None
    keep_clear_of: tuple[str, ...] = () # was str | None; Constraint expands globs
    line: int = 0
```

New specs (frozen):

```python
@dataclass(frozen=True)
class PairReq:
    p: str; n: str; z_diff_ohm: float = 90.0; match_mm: float = 0.5; uncoupled_mm: float = 2.0
    gap_mm: float | None = None; layers: tuple[str, ...] | None = None; reference: str | None = None; line: int = 0

@dataclass(frozen=True)
class BusReq:
    nets: tuple[str, ...]; match_mm: float; clock: str | None = None; length_mm: float | None = None; line: int = 0

@dataclass(frozen=True)
class ChainReq:
    net: str; pads: tuple[str, ...]; line: int = 0          # ("J2.1", "C4.1", "U1.12"), pin names as Place(to=) takes them

@dataclass(frozen=True)
class IsolationReq:
    a: str; b: str; volts: float; slot: bool = False; across: tuple[str, ...] = (); reinforced: bool = False; line: int = 0

@dataclass(frozen=True)
class GuardReq:
    net: str; stitch_mm: float = 2.5; ground: str = "GND"; line: int = 0
```

`Design` gains `pairs: list[PairReq]`, `buses: list[BusReq]`, `chains: list[ChainReq]`,
`isolations: list[IsolationReq]`, `guards: list[GuardReq]` (all `field(default_factory=list)`).

### A.3 Relationship to `CompiledClass` / `CompiledNet` / `CompiledJob` (`compile.py`, S2)

`CompiledClass`, `CompiledNet`, `CompiledJob` keep every field; `route.py`, `fanout.py`,
`copper.py`, `apply.py`, `seed.py` read them unchanged. They become projections of the
`ConstraintSet`:

1. `cs = compile_constraints(design)`.
2. `job.classes = list(cs.classes)`. A class is a view on the constraints of one `NetReq`
   (every net of one `NetReq` shares every number by construction). Name: `class_name=`,
   else `_KIND_CLASS[kind]` extended with `spi: "SPI"`, `i2c: "I2C"`, `sense: "Sense"`,
   `feedback: "Feedback"`, `generic: "Default"`; a bare `z_se_ohm` on `generic` gets
   `f"Z{int(z)}"`. If a later `NetReq` compiles a class whose name is taken and whose numbers
   `(width, clearance, via_d, via_h, pair_w, pair_gap)` differ, it gets `f"{base}_{n}"`
   (`Power_2`, n counting from 2 in file order); the report says `class Power_2 (Power is
   NetReq line 55 at 0.781 mm)`. Today the later `NetReq` silently overwrites the earlier
   class's numbers; that stops. Identical numbers share the class (node's two power lines).
   `CompiledClass` gains `lane_clearance_mm: float | None = None` (None reads as
   `clearance_mm`): the kind's clearance without the voltage row, so a 250 V class never
   widens every closed row's fanout lane (`pcb_place.lane_rules`, S4).
3. `job.nets` (`CompiledNet` per `NetReq`, patterns kept) is filled from the first constraint
   of the group: `patterns=req.nets, class_name, autoroute, vias=via.allowed, layers,
   max_length_mm=airwire_max_mm, match_group=(group.members if group and group.kind == "bus"
   else pair nets if pair else None), keep_clear_of=keep_away[0].other if keep_away else
   None, keep_clear_mm=keep_away[0].mm if keep_away else None, kind, amps=(amps if kind ==
   "power" else None)`. Every value is byte-identical to today's on the five boards (S2's
   snapshot test) except node's USB pair.
4. `job.constraints = cs` (new field); `CompiledJob.to_dict()` gains `"constraints":
   cs.to_dict()` and `"rule_areas": [asdict(a) for a in cs.rule_areas]`.
5. `job.dru = dru.rules(cs)` computed **once, at compile**. Every rule area R1 writes is
   known at compile (an `Isolation` corridor is the strip between two Regions, and
   `resolve_regions` already runs in `compile_design` for keepouts), so nothing about the DRU
   depends on placement. `apply.write_dru` and `fab.write_dru` only render `job.dru`.
   Neck-down areas (R-I3) are not in R1 (section E, H.3).
6. `krt` dict unchanged.
7. `copper.power_ampacity_failures` reads `cs.by_net(name).current.width_ipc2221.value`
   instead of calling `ipc2221_width_mm` (the same function, the same default rise, so the
   number and the gate are unchanged; one source).

### A.4 `src/pcbc/language.py` (S2): exact signatures

```python
def NetReq(*nets: str, kind: str = "generic",
           z_diff_ohm: float | None = None, z_se_ohm: float | None = None,
           volts: float | None = None, amps: float | None = None, temp_rise_c: float = 10.0,
           max_mm: float | None = None, length_mm: float | None = None,
           match_mm: float | None = None, uncoupled_mm: float | None = None,
           pair: bool = False, vias: bool | None = None, vias_max: int | None = None,
           layers: Sequence[str] | None = None, reference: str | None = None,
           keep_clear_of: str | Sequence[str] | None = None, keep_clear_mm: float | None = None,
           clock: str | None = None, pf_max: float | None = None, loop_mm2: float | None = None,
           autoroute: bool | str | None = None, class_name: str | None = None) -> NetReqSpec

def Pair(p: str, n: str, *, z_diff_ohm: float = 90.0, match_mm: float = 0.5, uncoupled_mm: float = 2.0,
         gap_mm: float | None = None, layers: Sequence[str] | None = None, reference: str | None = None) -> PairReq
def Bus(*nets: str, match_mm: float, clock: str | None = None, length_mm: float | None = None) -> BusReq
def Chain(net: str, *pads: str) -> ChainReq
def Isolation(a: str, b: str, *, volts: float, slot: bool = False,
              across: Sequence[str] = (), reinforced: bool = False) -> IsolationReq
def Guard(net: str, *, stitch_mm: float = 2.5, ground: str = "GND") -> GuardReq
```

- No `**kwargs`. An unknown keyword is a `TypeError` at load; `check` reports it as
  `NetReq("VBUS") line 144: unexpected keyword 'amp'; did you mean amps=?` (`difflib.get_close_matches`,
  stdlib). A kwarg the kind does not use is a refusal in `compile_constraints`:
  `NetReq("SW") line 56: kind="switch_node" does not take amps=; it takes loop_mm2, max_mm, layers, vias, autoroute, class_name`
  (the per-kind sets are the "own kwargs" column of section D plus the common set
  `max_mm, layers, vias, autoroute, class_name, keep_clear_of, keep_clear_mm, volts`).
- `kind` must be in `PRESETS` (`generic`, `power`, `analog`, `switch_node`, `clock`,
  `usb_hs`, `spi`, `i2c`, `sense`, `feedback`; aliases `digital`, `default` -> `generic`,
  kept for blinky and c3_usb); otherwise `NetReq("X") line n: unknown kind 'spy'; known: ...`.
- `line` is `sys._getframe(1).f_lineno` when that frame's `co_filename` is the board being
  loaded (`_board_path`), else 0. Deterministic: it is a property of the file.
- `Chain` needs at least two `"REF.PIN"` pads (`parse_refpin`); `Bus` at least two nets and
  `clock` in them; `Isolation` names two `Region`s; `Guard.net` must exist. All five append
  to the `Design`, are exported by `load_board`'s namespace and `pcbc/__init__.py`.

`check_design` (circuit.py, S2) appends `compile_constraints(design).refusals` when
`design.board` is set, so a bad line fails `pcbc check` before anything is drawn. Refusals
(each cites the line):

| refusal | message |
|---|---|
| a net named by two `NetReq`s or two `Pair`s | `USB_DP: NetReq line 143 and Pair line 262 both name it; say it once (Pair overrides only z_diff_ohm, match_mm, uncoupled_mm, gap_mm, layers, reference)` |
| `Chain` pad not on the net | `Chain("VDDA") line 97: C4.2 is on VSS, not VDDA` |
| `Bus.clock` not a member | `Bus line 12: clock "CLK" is not one of D0, D1, D2, D3` |
| `Isolation` side that is not a Region | `Isolation line 30: no Region "primary"` |
| a part on no side of an Isolation | `Isolation primary/secondary line 30: Q3 is placed on neither side; Place("Q3", parent="primary", ...) or list it in across=` |
| a net with pads on both sides not through an `across` part | `Isolation primary/secondary line 30: NET1 has pads on both sides (R3.1 primary, U7.4 secondary) and no across= part carries it: that is a short across the barrier` |
| `Pair` nets KiCad cannot pair | `Pair line 262: KiCad pairs names ending P/N, _P/_N or +/-; rename D_A/D_B` |

A part's side is decided at compile from `Place()` lines, no geometry: `Place(parent=<region>)`
gives the region; `Place(to="REF.PIN")` inherits REF's side (followed transitively); a part
with neither is on no side (refusal above); parts in `across=` are on both.

Precedence when a net is named twice legally (a `NetReq(kind="usb_hs")` and a `Pair` for
the same two nets): `Pair` overrides only its own fields, and the report prints
`USB_DP: skew 0.5 mm (Pair line 262 overrides preset usb_hs 0.5)`. Never silent.

### A.5 One-source rule, made concrete

| consumer | reads | today | after R1 |
|---|---|---|---|
| `.kicad_pro` classes | `CompiledClass` | compile's if-chain | `Constraint.width_mm/clearance_mm/via` via `dru.project_classes` (one writer for seed and apply) |
| `.kicad_dru` rules | `job.dru` | inline in compile | `dru.rules(cs)` from the same `Derived` values, computed once at compile |
| placement checks (`pcb_place`, `route_checks`) | `job.nets`, `lane_rules` | class clearance | `Constraint` per net; `lane_clearance_mm` |
| router plan (`route.krt_plan`) | `job.classes`, `job.nets` | same | unchanged interface, same numbers |
| fab ampacity (`copper.py`) | `ipc2221_width_mm` recomputed | | `Constraint.current.width_ipc2221` |
| report (`pcbc check --constraints`) | none | | `cs.lines` |

---

## B. Stackup data (`src/pcbc/stackup.py`, S1)

### B.1 `Stackup`

Every existing limit field keeps its name and value (`track_min`, `clearance_min`,
`via_drill`, `via_diameter`, `annular_min`, `hole_clearance`, `hole_to_hole`,
`edge_clearance`, `copper_oz`); `h_mm`, `t_mm`, `er` become derived properties (the
dielectric under F.Cu, the outer copper, its Dk) so `microstrip_z0(w, stack)` and every
existing caller keep working. `board_rules()` is unchanged
(`test_the_stackup_is_the_one_source_of_fab_limits` pins it).

```python
@dataclass(frozen=True)
class Dielectric:
    material: str           # "7628" | "3313" | "2116" | "1080" | "core"
    thickness_mm: float
    dk: float
    kind: str               # "prepreg" | "core"

@dataclass(frozen=True)
class Copper:
    layer: str              # "F.Cu", "In1.Cu", "In2.Cu", "B.Cu"
    thickness_mm: float

@dataclass(frozen=True)
class ImpedanceRow:
    structure: str          # "se" | "diff"
    z_ohm: float
    width_mm: float
    gap_mm: float | None
    source: str             # "JLCPCB impedance calculator as recorded by JITX, jitxlib.jlcpcb.JLC04161H_7628, read 2026-09-19"

@dataclass(frozen=True)
class Stackup:
    name: str
    layers: int
    jlc_code: str | None                      # "JLC04161H-7628"; None for the 2-layer default
    stack: tuple[Copper | Dielectric, ...]    # top to bottom
    copper_oz: float = 1.0
    track_min: float = 0.127
    clearance_min: float = 0.127
    via_drill: float = 0.3
    via_diameter: float = 0.5
    annular_min: float = 0.1
    hole_clearance: float = 0.25
    hole_to_hole: float = 0.5
    edge_clearance: float = 0.3
    mask_over_substrate_mm: float = 0.0305    # JLC impedance page: 1.2 mil
    mask_over_trace_mm: float = 0.0152        # 0.6 mil
    mask_dk: float = 3.8
    via_plating_mm: float = 0.018             # JLC capabilities: "average hole plating 18 um"
    z_bias_se: float = 1.0                    # multiplies the bare formula's Z0 (section C.2)
    z_bias_diff: float = 1.0
    z_bias_source: str = ""                   # "fitted to JLC rows 50 ohm SE, 90 and 100 ohm diff" | "uncalibrated"
    rows: tuple[ImpedanceRow, ...] = ()

    @property
    def h_mm(self) -> float: ...              # first Dielectric.thickness_mm
    @property
    def t_mm(self) -> float: ...              # first Copper.thickness_mm
    @property
    def er(self) -> float: ...                # first Dielectric.dk
    @property
    def board_mm(self) -> float: ...          # sum of the stack
    def copper_layers(self) -> tuple[str, ...]: ...
    def copper_t(self, layer: str) -> float: ...
    def dielectric_between(self, a: str, b: str) -> tuple[float, float]:   # (height, series Dk) between two copper layers
    def plane_below(self, layer: str) -> str | None: ...                    # "F.Cu" -> "In1.Cu"; "B.Cu" -> None
    def height_to_reference(self, layer: str, planes: tuple[tuple[str, str], ...], two_layer_pour: bool) -> tuple[float, str] | None:
        """(dielectric height, reference layer) for a track on `layer`: the adjacent declared plane, or on a
        2-layer board the B.Cu GND pour the route plan always writes (reference "B.Cu pour"); None otherwise."""
```

`dielectric_between("F.Cu", "In2.Cu")` on 7628 crosses In1.Cu's copper (0.0152) and two
dielectrics: height 0.2104 + 0.0152 + 1.065, series Dk `h_total / sum(h_i / dk_i)`.

`get_stackup` accepts the JLC code as an alias (`Board(stackup="JLC04161H-3313")`).

### B.2 The stackups to encode

Source: jlcpcb.com/impedance (read 2026-09-19): prepreg 7628 0.2104 mm Dk 4.4, 3313 0.0994 /
4.1, 2116 0.1164 / 4.16, 1080 0.0764 / 3.91; core Dk 4.6; outer copper 0.035; inner 0.0152;
mask 1.2 mil / 0.6 mil, Dk 3.8. Cores: the 1.6 mm build of each code (JLC lists 1.065 for
7628 and 1.265 for the three thin prepregs; the core only affects stripline and `In1->In2`
heights and is marked `verify` in the source string).

| pcbc name | jlc_code | stack, top to bottom | sum |
|---|---|---|---|
| `jlcpcb_4l_1oz` | JLC04161H-7628 | F.Cu 0.035, 7628 0.2104/4.4, In1.Cu 0.0152, core 1.065/4.6, In2.Cu 0.0152, 7628 0.2104/4.4, B.Cu 0.035 | 1.586 |
| `jlcpcb_4l_1oz_3313` | JLC04161H-3313 | F.Cu 0.035, 3313 0.0994/4.1, In1 0.0152, core 1.265/4.6, In2 0.0152, 3313 0.0994/4.1, B.Cu 0.035 | 1.564 |
| `jlcpcb_4l_1oz_2116` | JLC04161H-2116 | F.Cu 0.035, 2116 0.1164/4.16, In1 0.0152, core 1.265/4.6, In2 0.0152, 2116 0.1164/4.16, B.Cu 0.035 | 1.598 |
| `jlcpcb_4l_1oz_1080` | JLC04161H-1080 | F.Cu 0.035, 1080 0.0764/3.91, In1 0.0152, core 1.265/4.6, In2 0.0152, 1080 0.0764/3.91, B.Cu 0.035 | 1.518 |
| `jlcpcb_2l_1oz` | None | F.Cu 0.035, core 1.53/4.6, B.Cu 0.035 | 1.6 |

Limits stay exactly today's (2L: 0.127/0.127, via 0.3/0.5, ring 0.1; 4L: 0.0889/0.0889, via
0.2/0.35, ring 0.075; hole 0.25, hole-to-hole 0.5, edge 0.3): the annular-ring
reconciliation in router-plan section 3 is a fab decision, not R1. A `JLC_LIMITS[(two_layer,
oz)]` table carries JLC's own 1 oz / 2 oz numbers (1 oz 0.10/0.10 2L, 0.09/0.09 4L+; 2 oz
0.16/0.16 2L, 0.15/0.15 4L+) for the report only; no 2 oz stackup is encoded in R1.

Published rows (JLC's calculator, as JITX records them; the numbers pcbc asks the fab for):

| code | 50 ohm SE outer | 90 ohm diff outer (w / gap) | 100 ohm diff outer |
|---|---|---|---|
| JLC04161H-7628 | 0.3244 | 0.2332 / 0.15 | 0.1722 / 0.15 |
| JLC04161H-1080 | 0.1176 | 0.09 / 0.09 | 0.09 / 0.137 |
| JLC04161H-3313, -2116 | not published where pcbc can read it; `rows=()`, bias interpolated (C.2), report says `uncalibrated` |
| `jlcpcb_2l_1oz` | JLC publishes no 2-layer impedance stackup; `rows=()`, bias 1.0, report says `formula only` |

### B.3 Which code the existing names map to, and why

- `jlcpcb_4l_1oz` -> **JLC04161H-7628**. It is what JLC builds for a 1.6 mm 4-layer order
  that does not pick an impedance stackup (node and every previous 4L pcbc board were ordered
  that way), it is first on their impedance page, and today's `h 0.12, er 4.5` matches none
  of the four, so nothing recorded depends on the old numbers. Consequence, stated plainly:
  node's USB pair changes from 0.1554 / 0.12 to **0.2291 / 0.15** (C.2, D). 3313 would keep
  the pair near today's width (0.1353 / 0.15) but is not what JLC ships by default; it is
  available by name.
- `jlcpcb_2l_1oz` -> no code. `1.5 / 4.5` becomes `1.53 / 4.6` (1.6 mm less two 0.035
  coppers; JLC's core Dk). No example reads a 2-layer impedance number (USB on 2L is clamped,
  section D), so nothing changes.

---

## C. Formulas (`src/pcbc/stackup.py`, S1)

Every solver is bisection, 60 iterations, `lo = stack.track_min`, `hi = 6.0`, result rounded
to 4 dp: deterministic on every machine. Existing names keep their signatures and get a new
body or a new keyword: `microstrip_z0(w_mm, stack, layer="F.Cu")`, `width_for_z0(z0, stack,
wmin=..., wmax=..., layer="F.Cu")`, `diff_pair_geometry(z_diff, stack, layer="F.Cu",
gap=None) -> (w, s)`, `ipc2221_width_mm` unchanged.

### C.1 Microstrip, single: Hammerstad and Jensen 1980, static, with strip thickness

`microstrip(w, h, t, er) -> (z0, eeff)`; `eta0 = 376.730313668`:

```
u = w/h
Z01(u) = (eta0 / 2 pi) * ln( F(u)/u + sqrt(1 + (2/u)^2) ),   F(u) = 6 + (2 pi - 6) exp(-(30.666/u)^0.7528)
a(u)   = 1 + ln((u^4 + (u/52)^2)/(u^4 + 0.432))/49 + ln(1 + (u/18.1)^3)/18.7
b(er)  = 0.564 ((er - 0.9)/(er + 3))^0.053
eeff(u, er) = (er + 1)/2 + (er - 1)/2 * (1 + 10/u)^(-a(u) b(er))
thickness (t > 0):  th = t/h
  du1 = (th / pi) * ln(1 + 4 e / (th * coth^2(sqrt(6.517 u))))
  dur = (1/2) (1 + 1/cosh(sqrt(er - 1))) * du1
  u1 = u + du1;  ur = u + dur
eeff_t = eeff(ur, er) * (Z01(u1) / Z01(ur))^2
Z0     = Z01(ur) / sqrt(eeff_t)
```

The `(Z01(u1)/Z01(ur))^2` factor is the part two of the three drafts dropped; without it the
7628 row reads 53.86 ohm, with it 55.165, which is what KiCad's PCB Calculator gives.
Reference: E. Hammerstad, O. Jensen, "Accurate models for microstrip computer-aided design",
IEEE MTT-S Digest 1980, eqs. 1-8 and the thickness correction; KiCad 10
`common/transline_calculations/microstrip.cpp` (same equations). Valid 0.01 <= u <= 100,
1 <= er <= 128, 0.2 % (H&J).

### C.2 Calibration to JLC: the per-stackup bias

At JLC's own 50 ohm width the bare formula gives 55.16 ohm (7628) and 56.11 ohm (1080); at
their 90 ohm pair geometries the bare coupled formula (C.3) gives 105.1 and 111.7 ohm. The
residual is systematic: JLC's Polar-type solver models the solder mask fill between and over
the traces and the trapezoid etch; the closed forms do not. The KiCad solder-mask term
(`ApplySoldermaskCorrection`, Wan-Hoorfar filling factor) could not be verified from a source
this design could read and closes only a third of the gap on paper, so pcbc does not model
the mask separately: the stackup carries two **fab bias** factors, fitted to JLC's rows,
printed as such on every number they touch, and replaced by one edit when a row is captured:

```
Z_pcbc = z_bias * Z_bare          (z_bias_se for single, z_bias_diff for pairs)
width synthesis: solve Z_bare(w) = Z_target / z_bias
```

| code | z_bias_se (fit) | z_bias_diff (fit) | residuals at JLC's rows | source string |
|---|---|---|---|---|
| 7628 | 0.9064 (= 50 / 55.165) | 0.85 (geometric mean of 90/105.09 = 0.8564 and 100/118.69 = 0.8426) | 90 row -> 89.3 ohm (-0.8 %), 100 row -> 100.9 (+0.9 %) | `fitted to JLC04161H-7628 rows (JITX), 2026-09-19` |
| 1080 | 0.8911 (= 50 / 56.111) | 0.827 (mean of 0.8054, 0.85) | 90 row -> 92.4 (+2.7 %), 100 row -> 97.3 (-2.7 %) | same, 1080 |
| 3313, 2116 | 0.899 | 0.838 | none: interpolated (mean of the two fits) | `interpolated between 7628 and 1080 fits; capture JLC rows to replace` |
| 2L | 1.0 | 1.0 | none; c/h = 0.02, the mask barely matters | `uncalibrated: formula only` |

With the bias the synthesised widths land at: 7628 50 ohm 0.3244 (row 0.3244), 90 ohm pair
at gap 0.15 **0.2291** (row 0.2332, -1.8 %), 100 ohm 0.1762 (row 0.1722, +2.3 %); 1080 50
ohm 0.1176, 90 ohm at gap 0.09 0.0956 (row 0.09, +6 %), 100 ohm at gap 0.137 0.0846 (row
0.09, -6 %). Stated accuracy: **+-3 % impedance / +-3 % width on 7628, +-3 % / +-6 % on
1080**, inside USB 2.0's +-15 % and 100 ohm Ethernet's +-10 %; off-row stackups and 2L print
`uncalibrated`. `mask_*` fields stay on the stackup as data for the report and for a real
covered-microstrip model later.

### C.3 Coupled microstrip, edge-coupled: Hammerstad and Jensen 1980 even/odd, static

`coupled_microstrip(w, s, h, t, er) -> (ze, zo, eps_e, eps_o)`; `zdiff = 2 zo`. Inputs
`z0, eeff` are C.1's thickness-corrected single line at width `w` (`eeff_t`, with the
`(Z01(u1)/Z01(ur))^2` factor); **`u = w/h` is the bare ratio, not `ur`**, and `g = s/h`.
Thickness enters only through `z0` and `eeff`. Feeding `ur` into the Q-terms instead gives
104.15 ohm at vector 6 (not 105.09) and 133.1 at the 2L clamp (not 140.1): a 1-5 % trap the
test vectors are there to catch.

```
v      = u (20 + g^2)/(10 + g^2) + g exp(-g)
eps_e  = (er + 1)/2 + (er - 1)/2 (1 + 10/v)^(-a(v) b(er))
a_o = 0.7287 (eeff - (er + 1)/2)(1 - exp(-0.179 u));  b_o = 0.747 er/(0.15 + er)
c_o = b_o - (b_o - 0.207) exp(-0.414 u);              d_o = 0.593 + 0.694 exp(-0.562 u)
eps_o  = ((er + 1)/2 + a_o - eeff) exp(-c_o g^d_o) + eeff
Q1 = 0.8695 u^0.194;   Q2 = 1 + 0.7519 g + 0.189 g^2.31
Q3 = 0.1975 + (16.6 + (8.4/g)^6)^-0.387 + ln(g^10/(1 + (g/3.4)^10))/241
Q4 = 2 Q1/Q2 / (u^Q3 exp(-g) + (2 - exp(-g)) u^-Q3)
Ze = z0 sqrt(eeff/eps_e) / (1 - z0 sqrt(eeff) Q4/eta0)
Q5 = 1.794 + 1.14 ln(1 + 0.638/(g + 0.517 g^2.43))
Q6 = 0.2305 + ln(g^10/(1 + (g/5.8)^10))/281.3 + ln(1 + 0.598 g^1.154)/5.1
Q7 = (10 + 190 g^2)/(1 + 82.3 g^3);   Q8 = exp(-6.5 - 0.95 ln g - (g/0.15)^5)
Q9 = ln(Q7)(Q8 + 1/16.5);   Q10 = (Q2 Q4 - Q5 exp(ln(u) Q6 u^-Q9))/Q2
Zo = z0 sqrt(eeff/eps_o) / (1 - z0 sqrt(eeff) Q10/eta0)
```

Reference: Hammerstad and Jensen 1980, section "Coupled microstrip" (Q1..Q10 are theirs;
Kirschning and Jansen 1984 is the dispersion model and is not used); KiCad 10
`coupled_microstrip.cpp` implements the same static forms with separate even/odd thickness
widths, which differ from this single-`du` treatment by under 1 ohm in the fitted range
(stated in the test). Valid 0.1 <= u <= 10, 0.1 <= g <= 10.

Gap policy: `gap = max(0.15, stack.clearance_min)` (JLC's own pair gap on 4L), overridable by
`Pair(gap_mm=)`. Width is solved for `z_diff / z_bias_diff`. **Pair fit clamp** (today's
behaviour, now said): when the solved width exceeds `PAIR_FIT_MM = 0.25` the pair is written
at `(track_min, clearance_min)`, `controlled=False`, `z_computed` is evaluated at that
geometry and the note reads `USB_DP/USB_DN: 90 ohm needs 0.7764 mm members at gap 0.15 on
jlcpcb_2l_1oz (formula only); pair written at the fab floor 0.127/0.127 = 140.1 ohm; fine for
USB full speed, use Board(stackup="jlcpcb_4l_1oz") for high speed`. c3_usb's geometry is
unchanged; its report stops lying.

### C.4 Stripline (inner layers): Wadell 4.2.1 (Cohn) with thickness, asymmetric by parallel combination

`stripline(w, b, t, er)`, `b` = plane-to-plane:

```
x = t/b;   m = 6 (1 - x)/(3 - x)                      (Wadell: m = 2/(1 + (2/3) x/(1 - x)))
dw = (b - t) * (x/(pi (1 - x))) * (1 - 0.5 ln( (x/(2 - x))^2 + (0.0796 x/(w/b + 1.1 x))^m ))
w' = w + dw;   A = (8/pi)(b - t)/w'
Z0 = (30/sqrt(er)) ln( 1 + (4/pi)((b - t)/w') (A + sqrt(A^2 + 6.27)) )
```

Asymmetric (a signal on In1.Cu between F.Cu at `h1` and In2.Cu at `h2`):
`stripline_asym(w, h1, h2, t, er1, er2) = 2 Z(2 h1 + t) Z(2 h2 + t) / (Z(2 h1 + t) + Z(2 h2 + t))`
with `er` the thickness-weighted mean, printed `+-5 %` (an approximation; JLC publishes no
inner-layer rows). The mis-transcribed `m = 6x/(3+2x)` of one draft gave 70.26 ohm at the
first vector below; the correct form gives 67.46, KiCad 67.19. Reference: B. C. Wadell,
*Transmission Line Design Handbook*, Artech 1991, section 4.2.1 (Cohn 1954 with the
thickness correction) and 4.2.2; as `t -> 0` it reproduces the exact conformal-map stripline
`Z0 = (30 pi/sqrt(er)) K(k')/K(k), k = tanh(pi w/2b)` to 0.05 % (vector 12). IPC-2141
`60/sqrt(er) ln(1.9 b/(0.8 w + t))` is kept as `ipc2141_stripline` for the cross-check column.

### C.5 Coplanar waveguide with ground: Ghione and Naldi 1987, t = 0

`cpwg(w, s, h, er) -> (z0, eeff)`:

```
k1 = w/(w + 2 s);   k3 = tanh(pi w/(4 h)) / tanh(pi (w + 2 s)/(4 h))
q1 = K(k1)/K(k1'),  q3 = K(k3)/K(k3')      (K by the arithmetic-geometric mean, k' = sqrt(1 - k^2))
eeff = 1 + q3 (er - 1)/(q1 + q3)
Z0   = (eta0/2) / ((q1 + q3) sqrt(eeff))
```

Reference: G. Ghione, C. Naldi, "Coplanar waveguides for MMIC applications", IEEE MTT-35(3)
1987, eqs. 6-7 (conductor-backed CPW); KiCad 10 `coplanar.cpp` (`backMetal` branch).
Thickness is not applied in R1 (KiCad's Gupta-Garg correction is a second-order term at
0.035 mm; the report says `t = 0`). Used only where a pour flanks the track: 2-layer
`z_se_ohm` requests and `Guard`. Validity: `s/h <= 1`; beyond it pcbc takes
`min(cpwg, microstrip)` and says so (the CPWG form's wide-gap limit overestimates a plain
microstrip by 14 % at w/h = 0.65, so it is not used as a microstrip substitute).

### C.6 Current: IPC-2152 with the IPC-2221 floor

`ipc2221_width_mm` unchanged (k = 0.048 external, 0.024 internal;
`area_mil2 = (I/(k dT^0.44))^(1/0.725)`; 1 oz = 1.378 mil).

`ipc2152_width_mm(amps, temp_rise_c, copper_oz, board_mm, plane_h_mm | None) -> float`:

```
area_mil2   = (I / (0.0897 dT^0.394))^(1 / (0.5038 dT^0.0385))        universal chart fit (Fig 5-1)
board_mod   = 24.93 * (board_mm/0.0254)^-0.755                          Fig 5-8; 1.6 mm -> 1.092
plane_mod   = 0.00313 * (plane_h_mm/0.0254) + 0.4045, plane_h >= 0.144  Fig 5-11; 0.2104 -> 0.430, 1.53 -> 0.593; 1.0 when no plane
width_mm    = area_mil2 * board_mod * plane_mod / (1.378 copper_oz) * 0.0254
```

Reference: IPC-2152 Figures 5-1, 5-8, 5-11 as digitised by G. Hunter, mbedded.ninja "PCB
Track Current Capability (IPC-2152) Calculator" (coefficients quoted verbatim there with the
figure numbers); valid 0.274-26 A, 1-100 C, 0.72-2.36 mm boards; below 0.274 A the fit
extrapolates and the report says so. Cross-check: the smps.us fit of the same chart,
`(117.555 dT^-0.913 + 1.15) I^(0.84 dT^-0.108 + 1.159)`, gives 513.1 mil2 at 10 A / 20 C
against 517.9 here and 500 read off the chart (vector 15).

`current_width_mm(amps, temp_rise_c, stack, plane_h_mm) -> Derived`:
`max(ipc2221 external, ipc2152)`; `source` names the winner, `note` the loser. The plane
term applies when a declared `planes=` layer is adjacent to the routing layer, or on a
2-layer board because the route plan always pours GND on B.Cu (`plane_h = 1.53`). Every
example's width is unchanged: buck 2 A -> 2152 with the pour 0.646 < 2221 0.781; node 1 A
over In1 at 0.2104 -> 0.133 < 0.300 < the 0.4 floor. A no-plane track above 1 A is where
2152 raises the width (2 A, no plane: 1.090 mm). Decision H.1.

### C.7 Vias per amp

```
barrel_mil2 = pi * drill_mm * via_plating_mm / 0.00064516
via_amps    = 0.024 * dT^0.44 * barrel_mil2^0.725             IPC-2221 internal curve on the barrel
per_change  = max(1, ceil(amps / via_amps))
```

Reference: IPC-2221B eq. 6-2 internal, barrel-as-internal-trace convention (D. Brooks, J.
Adam, *PCB Design Guide to Via and Trace Currents and Temperatures*, Artech 2021, ch. 9;
Saturn PCB Toolkit uses the same). Plating 0.018 mm is JLC's own figure ("average 18 um");
0.025 mm, used by one draft, is 20 % optimistic. A 0.3 mm via carries 0.707 A at 10 C; node's
1 A LOAD on 0.2 mm drills (0.527 A) wants 2 per layer change. **R1 prints `per_change` and
counts it in the copper bar; it is not a gate** (the router places one via per change until
R2's spines and taps; a gate would fail node on day one).

### C.8 Voltage: IPC-2221B Table 6-1 clearance, IEC 60664-1 creepage and impulse clearance

`ipc2221_clearance_mm(volts, row) -> float`, rows exactly as IPC-2221B Table 6-1 (2012) and
KiCad's `panel_electrical_spacing_ipc2221.cpp` encode them; above 500 V the 301-500 value
plus the per-volt increment times `(V - 500)`:

| V (peak) | B1 internal | B2 external, uncoated, <= 3050 m | B4 external, permanent polymer coating |
|---|---|---|---|
| 0-15 | 0.05 | 0.1 | 0.05 |
| 16-30 | 0.05 | 0.1 | 0.05 |
| 31-50 | 0.1 | 0.6 | 0.13 |
| 51-100 | 0.1 | 0.6 | 0.13 |
| 101-150 | 0.2 | 0.6 | 0.4 |
| 151-170 | 0.2 | 1.25 | 0.4 |
| 171-250 | 0.2 | 1.25 | 0.4 |
| 251-300 | 0.2 | 1.25 | 0.4 |
| 301-500 | 0.25 | 2.5 | 0.8 |
| > 500, per V | +0.0025 | +0.005 | +0.00305 |

pcbc uses **B2** for any net that may run on an outer layer (JLC's LPI mask is not a
qualified permanent polymer coating under IPC-2221) and **B1** when `layers=` is inner-only.
B4 is carried in the table and not selectable in R1. Under 30 V every row is below every class
floor, so no example changes.

`iec_creepage_mm(volts_rms, material_group="IIIa", pollution_degree=2, reinforced=False)`:
IEC 60664-1:2020 Table F.5 (Table F.4 in the 2007 edition), PD2, general columns (not the
"printed wiring" column, decision H.5); IEC 62368-1:2018 Table 17 carries the same PD2 rows.
Rows are looked up at the next voltage >= `volts`; reinforced doubles:

| V rms | I | II | IIIa / IIIb |
|---|---|---|---|
| 50 | 0.60 | 0.85 | 1.20 |
| 63 | 0.63 | 0.90 | 1.25 |
| 80 | 0.67 | 0.95 | 1.30 |
| 100 | 0.71 | 1.00 | 1.40 |
| 125 | 0.75 | 1.05 | 1.50 |
| 160 | 0.80 | 1.10 | 1.60 |
| 200 | 1.00 | 1.40 | 2.00 |
| 250 | 1.25 | 1.80 | 2.50 |
| 320 | 1.60 | 2.20 | 3.20 |
| 400 | 2.00 | 2.80 | 4.00 |
| 500 | 2.50 | 3.60 | 5.00 |
| 630 | 3.20 | 4.50 | 6.30 |
| 800 | 4.00 | 5.60 | 8.00 |
| 1000 | 5.00 | 7.10 | 10.0 |

FR-4 is material group **IIIa** (175 <= CTI < 400; IIIb is 100 <= CTI < 175); same numbers
at PD2 either way. Cross-check: TI SLUP419 (2024) Table 3 reproduces 63 V 0.63/0.9/1.25, 400
V 2.0/2.8/4.0, 800 V 4.0/5.6/8.0, 1000 V 5.0/7.1/10.0.

`iec_clearance_mm(volts_rms, overvoltage_category=2, reinforced=False)`: rated impulse from
Table F.1, OV II (<= 50 V: 500 V; <= 100: 800; <= 150: 1500; <= 300: 2500; <= 600: 4000;
<= 1000: 6000), reinforced multiplies the impulse by 1.6 (62368-1 practice, as KiCad's
`iec60664.cpp`), then Table F.2 inhomogeneous field, PD2 (minimum 0.2 mm): 1.0 kV 0.2; 1.2
0.25; 1.5 0.5; 2.0 1.0; 2.5 1.5; 3.0 2.0; 4.0 3.0; 5.0 4.0; 6.0 5.5; 8.0 8.0; 10 11.

Policies: a plain `NetReq(volts=)` gets the class clearance `max(kind clearance,
ipc2221_clearance_mm(volts, row))` and a **creepage rule only when `volts >=
CREEPAGE_FROM_V` (60 V, IEC 62368-1 ES1)**: 3.3, 5, 12 and 48 V nets get none (pinned in
`test_constraints.py`); a board that wants creepage at 48 V writes `Isolation(...,
volts=48)`. An `Isolation(volts=V)` gets clearance `max(ipc2221 B2, iec_clearance_mm)`
(250 V: max(1.25, 1.5) = **1.5 mm**) and creepage `iec_creepage_mm` (250 V: **2.5 mm**;
reinforced 3.0 / 5.0). With `slot=True` the creepage path is the contour through the slot:
`gap + 2 * stack.board_mm >= creepage` and `gap >= 1.0` (IEC 60664-1 groove rule at PD2,
KiCad `GetMinGrooveWidth`); clearance still applies to the gap.

### C.9 I2C length from capacitance

`capacitance_pf_per_mm(z0, eeff) = sqrt(eeff) / (299.792458 * z0) * 1000`;
`i2c_max_mm(pf_max, pins, c_pf_per_mm) = (pf_max - 10 * pins) / c_pf_per_mm` at the class
width on the outer layer (bias applied to `z0`, `eeff / z_bias^2` as the consistent
velocity). Reference: I2C-bus specification UM10204 rev 7, section 7.1 (Cb <= 400 pF, 10 pF
per device pin). On 2L at 0.16 mm (145 ohm, 0.0391 pF/mm) a 2-device bus is bounded at 9.7 m:
it binds only on long buses on thin prepregs, which is the point of printing it.

### C.10 Test vectors (`tests/test_stackup.py`, one `assert` per line, tolerance as stated)

| # | call | expected | tol | reference |
|---|---|---|---|---|
| 1 | `microstrip(1.0, 1.0, 0, 9.8)` | 49.289 ohm, eeff 6.579 | 0.02 / 0.005 | H&J 1980 textbook alumina line; KiCad calculator |
| 2 | `microstrip(0.3244, 0.2104, 0.035, 4.4)` | 55.165 ohm, eeff 3.1417 | 0.02 / 0.002 | H&J as KiCad (the dropped `(Z01(u1)/Z01(ur))^2` factor gives 53.858 instead) |
| 3 | `microstrip(0.1176, 0.0764, 0.035, 3.91)` | 56.111 ohm | 0.02 | same, 1080 |
| 4 | `microstrip(1.0, 1.53, 0.035, 4.6)`; `width_for_z0(50, jlcpcb_2l_1oz)` | 83.495 ohm; 2.8097 mm | 0.02; 0.0005 | H&J |
| 5 | `width_for_z0(50, jlcpcb_4l_1oz)` (bias 0.9064) | 0.3244 mm | 0.0005 | JLC04161H-7628 row (by construction of the bias) |
| 6 | `coupled_microstrip(0.2332, 0.15, 0.2104, 0.035, 4.4)` | Zdiff 105.09, Ze 73.93, Zo 52.54 | 0.05 | H&J coupled; x 0.85 = 89.3 vs JLC 90 |
| 7 | `coupled_microstrip(0.1722, 0.15, 0.2104, 0.035, 4.4)` | Zdiff 118.69 | 0.05 | x 0.85 = 100.9 vs JLC 100 |
| 8 | `coupled_microstrip(0.09, 0.09, 0.0764, 0.035, 3.91)`; `(0.09, 0.137, ...)` | 111.74; 117.65 | 0.05 | 1080 rows; x 0.827 = 92.4 / 97.3 (the worst residual, +-2.7 %) |
| 9 | `diff_pair_geometry(90, jlcpcb_4l_1oz)`; `(100, ...)` | (0.2291, 0.15); (0.1762, 0.15) | 0.0005 | synthesis with the bias; JLC rows 0.2332 / 0.1722 within 2.3 % |
| 10 | `diff_pair_geometry(90, jlcpcb_2l_1oz)` | (0.127, 0.127) clamped; solved member 0.7764; `zdiff(0.127, 0.127, 1.53, 0.035, 4.6)` = 140.11 | 0.0005; 0.05 | the pair-fit clamp; `coupled_microstrip(0.9, 0.127, 1.53, 0.035, 4.6)` = 83.43 as a sanity row (a Polar-solver forum figure: 0.93 mm at 5 mil gap on 1.52 mm FR-4) |
| 11 | `stripline(0.2, 1.0, 0.0152, 4.6)`; `(0.15, 0.5, 0.0175, 4.6)`; `(0.2, 0.4, 0.0175, 4.6)` | 67.46; 54.86; 42.55 (IPC-2141: 66.68, 54.07, 40.69) | 0.02 | Wadell 4.2.1; KiCad 67.19 / 54.28 / 42.47 within 0.5 % |
| 12 | `stripline(w, 1.0, 1e-9, 4.6)` for w = 0.2, 0.3, 0.5 | 71.39, 60.31, 46.82 | 0.05 | exact conformal map `(30 pi/sqrt(er)) K(k')/K(k)` = 71.40, 60.33, 46.86 |
| 13 | `stripline_asym(0.2, 0.2104, 1.065, 0.0152, 4.4, 4.6)`; w for 50 | 60.09 ohm; 0.2968 mm | 0.02; 0.0005 | parallel-combination form, In1.Cu on 7628 |
| 14 | `cpwg(0.5, 0.3, 1.53, 4.6)`; `cpwg(1.5, 0.3, 1.53, 4.6)`; w for 50 at s = 0.2 | 73.79 ohm, eeff 2.838; 50.47; 1.1992 | 0.02 / 0.002 | Ghione-Naldi as KiCad t = 0 |
| 15 | `ipc2152_area_mil2(10, 20)`; smps.us fit at (10, 20) | 517.9; 513.1 | 0.5 | both fits within 4 % of the chart's 500 |
| 16 | `ipc2152_width_mm(1, 10, 1, 1.6, None)`; `(2, 10, 1, 1.6, None)` | 0.309; 1.090 | 0.002 | mbedded.ninja coefficients, board mod 1.092 |
| 17 | `ipc2152_width_mm(2, 10, 1, 1.6, 1.53)`; `(1, 10, 1, 1.6, 0.2104)` | 0.646; 0.133 | 0.002 | plane mod 0.593 / 0.430 |
| 18 | `current_width_mm(2, 10, jlcpcb_2l_1oz, 1.53)` | 0.781, source `ipc2221_ext`, note names 0.646 | exact | unchanged from `test_route_plan` |
| 19 | `ipc2221_width_mm(2)`, `(1)`, `(3)` | 0.781, 0.300, 1.367 | exact | existing pins |
| 20 | `via_amps(0.3, 0.018, 10)`; `(0.2, 0.018, 10)`; `(0.3, 0.018, 20)` | 0.707; 0.527; 0.960 A | 0.002 | IPC-2221 internal on 26.30 / 17.53 mil2 |
| 21 | `vias_per_change(1.0, 0.3)`, `(0.1, 0.3)`, `(2.0, 0.3)`, `(1.0, 0.2)` | 2, 1, 3, 2 | exact | C.7 |
| 22 | `ipc2221_clearance_mm(5, "B2")`, `(48, "B2")`, `(250, "B2")`, `(400, "B2")`, `(600, "B2")`, `(250, "B1")`, `(250, "B4")`, `(1000, "B1")` | 0.1, 0.6, 1.25, 2.5, 3.0, 0.2, 0.4, 1.5 | exact | IPC-2221B 6-1 as KiCad |
| 23 | `iec_creepage_mm(250, "IIIa")`, `("I")`, `("II")`, `(400, "IIIa")`, `(48, "IIIa")`, `(250, "IIIa", reinforced=True)` | 2.5, 1.25, 1.8, 4.0, 1.2, 5.0 | exact | IEC 60664-1 F.5 / 62368-1 T17; SLUP419 T3 |
| 24 | `iec_clearance_mm(250)`, `(250, reinforced=True)`, `(48)` | 1.5, 3.0, 0.2 | exact | IEC 60664-1 F.1 + F.2 |
| 25 | `capacitance_pf_per_mm(*microstrip(0.16, 1.53, 0.035, 4.6))`; `i2c_max_mm(400, 2, that)` | 0.0391 pF/mm; 9719 mm | 0.0002; 1 % | C.9 |
| 26 | `fanout_stagger(2L, 0.2, 0.65)`, `fanout_lane(2L, 0.2, 0.65)`, `board_rules(2L)` | 0.4664, 1.2934, today's dict | exact | existing pins, unchanged |

---

## D. Preset table (`constraints.PRESETS`)

"floor" = today's class floors `max(0.16, track_min)` width and `max(0.16, hole_floor)`
clearance (`hole_floor = hole_clearance - annular_min + 0.005`: 0.155 on 2L, 0.18 on 4L).
"2L" is `jlcpcb_2l_1oz`, "4L" is `jlcpcb_4l_1oz` (7628). Overrides: any accepted kwarg
replaces the preset's number and the source becomes `NetReq line n`; `Pair()` overrides
`z_diff_ohm, match_mm, uncoupled_mm, gap_mm, layers, reference`; `Bus()` sets the group.
Numbers with no standard behind them carry `source.formula = "preset"` and print `(pcbc
default)`.

| kind | class | own kwargs (default) | width | clearance / lane | via (dia/drill), allowed, budget | layers | airwire / routed length | group | spacing | rules (E) |
|---|---|---|---|---|---|---|---|---|---|---|
| `generic` (`digital`, `default`) | Default | `z_se_ohm` | floor; with `z_se_ohm`: C.1 x bias on `layers[0]` (inner: C.4), 2L: C.5 with the pour at `s = max(0.3, 2 x clearance)` | floor / same | stackup via, allowed | F, B | none | none | 3W | none (`z_se_ohm`: reference check F.6) |
| `power` | Power | `amps` (0.2), `temp_rise_c` (10), `volts`, `loop_mm2` (6.0, decap loop, pcbc default) | C.6, then `max(w, 0.4 if amps >= 0.2 else 0.25)` ("pcbc_floor: a neck a pad can carry"): 1 A -> 0.4, 2 A -> 0.781 | `max(0.20, B2 row)`: 0.20 to 30 V, 0.6 at 48 V, 1.25 at 250 V / lane 0.20 | 0.8/0.4 (today; `max` with the stackup), allowed, `per_change` from C.7 | all copper | none | none | 3W | `track_width` (soft), creepage when `volts >= 60` |
| `analog` | Analog | `keep_clear_of`, `keep_clear_mm` (3.0 when `keep_clear_of` given) | 0.20 | 0.20 / 0.20 | 0.6/0.3, **no vias** (`via_count (max 0)`) | F.Cu | airwire 25 (today) | none | 5W | `via_count`, keep-away clearance when given |
| `switch_node` | SwitchNode | `loop_mm2` (20.0, hot loop, pcbc default; buck measures 3.3), `amps` | 0.30, or C.6 if `amps` | 0.20 / 0.20 | 0.6/0.3, no vias | F.Cu | airwire 8 | none | 3W | `via_count` |
| `clock` | Clock | `match_mm` (2.0), `vias_max` (2) | 0.15 | 0.20 / 0.20 | 0.6/0.3, allowed, budget 2 | F, B | none | bus of the `NetReq`'s nets when > 1, `match_mm` | 5W | `skew` (soft), `via_budget` (soft) |
| `usb_hs` | USB | `z_diff_ohm` (90), `pair` (True), `match_mm` (0.5), `uncoupled_mm` (2.0), `vias_max` (2), `reference`, `length_mm` (none) | pair by C.3 x bias at gap `max(0.15, clearance_min)`: 4L 0.2291/0.15; 2L clamp 0.127/0.127, `controlled=False`, note | `min(0.16, w)` then the floor (today: 0.155 on 2L, 0.18 on 4L) / same | stackup via, allowed, budget 2 | F, B (today); 4L `reference` = `plane_below(layers[0])` when it is in `planes=` | none; `length_mm` only when given (USB 2.0 does not bound the board trace: decision H.6) | pair: skew 0.5, uncoupled 2.0 (R-Z5; TI usb_layout_basics, Altium: the tight number) | 3W | `diff_pair_gap` (error), `diff_pair_uncoupled` (soft), `skew` (soft), `via_budget` (soft), `track_width` (soft), chain check on 3+ pads (F.2) |
| `spi` | SPI | `clock` (required when > 1 net), `match_mm` (2.5, about 15 ps on FR-4, pcbc default), `length_mm`, `z_se_ohm` | floor; with `z_se_ohm` as generic | 0.20 / 0.20 | stackup via, allowed | F, B | `length_mm` when given | bus matched to `clock` within `match_mm` | 3W | `skew` (soft), `length` (error when given) |
| `i2c` | I2C | `pf_max` (400, UM10204) | floor | 0.20 / 0.20 | stackup via, allowed | F, B | routed `length (max i2c_max_mm)` (C.9), printed with pF/mm and pins | none | 3W | `length` (error) |
| `sense` | Sense | `match_mm` (1.0, R-A2), `keep_clear_of`, `keep_clear_mm` (3.0) | 0.20 | 0.20 / 0.20 | 0.6/0.3, no vias | F.Cu | airwire 25 | when exactly two nets: bus of the two, `skew (max 1mm)`; a `Chain` from the load pad is validated (F.2) | 5W | `via_count`, `skew` (soft) |
| `feedback` | Feedback | `keep_clear_of`, `keep_clear_mm` (3.0) | 0.20 | 0.20 / 0.20 | 0.6/0.3, no vias | F.Cu | airwire 15 | none | 5W | `via_count`, keep-away clearance when given |

Notes on the table:

- **Keep-away is explicit.** `analog`, `sense` and `feedback` hold a keep-away only when
  `keep_clear_of=` is written; when the board has a `switch_node` net and the line has none,
  the report prints `FB: keep_clear_of none; NetReq("FB", kind="analog", keep_clear_of="SW")
  holds 3 mm`. Reason: buck as placed has R_FB_TOP.2 (FB) 1.84 mm edge-to-edge from C_BOOT.2
  (SW), and U1's own FB and SW pins 1 mm apart; a silent 3 mm default would fail the stock
  example in KiCad and in F.5 with a move the AI cannot make (the boot cap belongs at BOOT).
  The AI states the distance; the tool holds it.
- `volts` on any kind: the class clearance row as for power; creepage rule at >= 60 V.
- `z_se_ohm` on any kind: as `generic`'s column; `reference` defaults to
  `plane_below(layers[0])` when that layer is declared in `planes=` (report: `over In1.Cu
  (GND)`), on 2L `over the B.Cu pour`.
- `Pair(p, n)` on nets without a `NetReq` gives them kind `usb_hs`'s pair logic with the
  `Pair`'s numbers and class `Pair_<p>`; `Bus(...)` gives a `GroupSpec("bus")` on the members'
  existing constraints (generic when none); `Guard(net)` records `guard_stitch_mm` (printed;
  R2 pattern); `Isolation` sets `isolation_side` on every net whose parts are all on one side
  and adds the rules of E.8-E.10.

Unchanged outputs on the five boards, checked against the compile dump: blinky, buck, DS2
identical classes and nets; c3_usb identical (the 2L pair still floors to 0.127/0.127, now
with the C.3 note); node: USB `0.2291 / 0.15` instead of `0.1554 / 0.12` (H.2), everything
else identical.

---

## E. DRU emission (`src/pcbc/dru.py`, S3) and project classes

```python
def rules(cs: ConstraintSet) -> list[DruRule]          # pure, deterministic; computed once in compile_design
def render(rules: list[DruRule]) -> str                # today's apply._render_dru, moved
def validate(rules: list[DruRule]) -> list[str]        # run before any write; a non-empty list raises ValueError
def project_classes(cs: ConstraintSet) -> list[dict]   # rows for .kicad_pro net_settings.classes
def netclass_patterns(cs: ConstraintSet) -> list[dict] # [{"netclass": name, "pattern": p}, ...]
```

`validate` refuses a constraint name outside `{clearance, creepage, length, skew, via_count,
track_width, track_segment_length, track_angle, diff_pair_gap, diff_pair_uncoupled, disallow}`,
a `track_angle` value followed by any letter (`re.search(r"track_angle \(min [0-9.]+[A-Za-z]", text)`),
a length value without `mm`, an empty condition, or two rules with one name. Values are
formatted `f"{round(v, 4):g}mm"` (`0.6mm`, `1.25mm`, `0.001mm`); the pair-gap rule keeps
today's `.2f`. KiCad applies the **last** matching rule of a constraint type, so the file is
ordered general to specific: geometry, class widths, keep-aways, isolation, per-net rules,
pairs, areas, the footprint exemption, the canary last.

Conditions: `A.hasNetclass('X')` for classes (verified, plan section 2), `A.NetName == 'X'`
for nets, `||`-lists of names for groups (`skew` verified on two names), `A.intersectsArea('X')`
for areas (verified). No membership-only netclasses: a patterns-only class is unverified
in KiCad 10 and net-name lists are.

| # | constraint | rule name | rule text template | condition | severity | when |
|---|---|---|---|---|---|---|
| 1 | geometry (R-M2) | `pcbc_geometry_segments` | `(constraint track_segment_length (min 0.2mm))` | `A.Type == 'Track'` | warning | always (exists) |
| 2 | geometry | `pcbc_geometry_angles` | `(constraint track_angle (min 135))` | `A.Type == 'Track'` | warning | always (exists; no unit) |
| 3 | class width (R-I1) | `width_{cls_slug}` | `(constraint track_width (min {width}mm))` | `A.hasNetclass('{cls}')` | **warning** (soft `track_width`) | every class whose width > `track_min` (power, pair, `z_se_ohm` classes); fanout stubs on closed rows are `track_min` wide and legal necks have no rule area until R2 |
| 4 | keep-away (R-D3, R-A1) | `{cls_slug}_away_from_{net_slug}` | `(constraint clearance (min {mm}mm))` | `A.hasNetclass('{cls}') && B.NetName == '{other}' && !(A.Type == 'Pad' && B.Type == 'Pad' && A.Reference == B.Reference)` | error | every `KeepAway`, one per (class, other net); the exemption keeps an IC's own FB/SW pins out of it |
| 5 | voltage clearance (R-V1) | none | the class row in `.kicad_pro` carries `max(kind, IPC row)`; KiCad applies the larger of the two nets' class clearances | | | a rule would duplicate the class (one draft's `A.hasNetclass('X') && B.hasNetclass('Y')` pairs are redundant) |
| 6 | creepage by `volts` (R-V1) | `creepage_{cls_slug}` | `(constraint creepage (min {mm}mm))` | `A.hasNetclass('{cls}') && !B.hasNetclass('{cls}')` | error | classes with `volts >= 60` |
| 7 | routed length (R-L1) | `length_{net_slug}` | `(constraint length (max {mm}mm))` | `A.NetName == '{net}'` | error when from `length_mm=` or i2c; there is no preset length rule in R1 | one rule per net, sorted; `max_mm` never becomes a length rule (the maze path is longer than the airwire) |
| 8 | skew (R-L2) | `skew_{group_slug}` | `(constraint skew (max {mm}mm))` | `A.NetName == '{a}' \|\| A.NetName == '{b}' ...` (members sorted) | **warning** (soft `skew`) | one per pair and per bus (members + clock) |
| 9 | no vias (R-D3, R-A1) | `novia_{net_slug}` | `(constraint via_count (max 0))` | `A.NetName == '{net}'` | error | every net with `via.allowed False` (per net: the verified form); `copper.vias_on_no_via_nets` stays as the offline mirror |
| 10 | via budget (R-Z4) | `vias_{net_slug}` | `(constraint via_count (max {n}))` | `A.NetName == '{net}'` | **warning** (soft `via_budget`) | `via.count_max` on usb_hs and clock |
| 11 | pair gap (R-Z5) | `{cls_slug}_pair_gap` | `(constraint diff_pair_gap (min {max(0.1, g-0.03):.2f}mm) (opt {g:.2f}mm))` | `A.hasNetclass('{cls}')` | error | every pair (exists; condition rewritten from `A.NetClass ==`); `fab._IGNORE_DRC` keeps `diff_pair_gap_out_of_range` while KRT rewrites the 2L gap |
| 12 | uncoupled (R-Z5) | `uncoupled_{cls_slug}` | `(constraint diff_pair_uncoupled (max {mm}mm))` | `A.hasNetclass('{cls}')` | **warning** (soft `diff_pair_uncoupled`) | every pair |
| 13 | isolation clearance (R-V2) | `iso_{a}_{b}_clearance` | `(constraint clearance (min {mm}mm))` | `({A-side names}) && ({B-side names}) && !(A.Type == 'Pad' && B.Type == 'Pad' && A.Reference == B.Reference)` where `{A-side names}` = `A.NetName == 'X' \|\| A.NetName == 'Y'` and B likewise, both sorted | error | every `Isolation`; a bridging part's own pads are its rating, not layout |
| 14 | isolation creepage (R-V2) | `iso_{a}_{b}_creepage` | `(constraint creepage (min {mm}mm))` | same as 13 | error | every `Isolation` without `slot`; with `slot=True` the slot satisfies it (C.8) and no creepage rule is written |
| 15 | isolation corridor (R-V2) | `iso_{a}_{b}_area` | `(constraint disallow track via zone)` | `A.intersectsArea('ISO_{a}_{b}')` | error | every `Isolation`; the area is a zone named `ISO_{a}_{b}` with `(keepout (tracks allowed) (vias allowed) (pads allowed) (copperpour allowed) (footprints allowed))` written by `apply._apply_rule_areas` with `stable_uuid("area", name)`, box = the strip between the two Regions' rects along their separating axis, full length of the board across it, all copper layers; pads and footprints stay allowed so the isolator straddles |
| 16 | footprint's own pads | `pads_of_one_footprint` | `(constraint clearance (min 0.1mm))` | `A.Type == 'Pad' && B.Type == 'Pad' && A.Reference == B.Reference` | error | always (exists); placed after every clearance rule so it wins |
| 17 | canary | `pcbc_canary` | `(constraint length (max 0.001mm))` | `A.NetName == '{cs.canary_net}'` | warning | always, last; the gate fails when it does not fire (exists) |
| 18 | `hole_to_hole` | project `rule_severities` | | | error | exists (seed) |
| 19 | fiducial masks, keepouts | KiCad keepout zones | | | | exist; no rule |

Not writable as rules (KiCad has no check): plane-split crossing, return vias, vias per amp,
loop areas, chain order, parallel-run length, corridor: these are P (section F) or V (R2+).

`.kicad_pro` classes: `dru.project_classes(cs)` returns `{"name", "clearance",
"track_width", "via_diameter", "via_drill"}` plus `diff_pair_gap` / `diff_pair_width` for
pairs, one row per `CompiledClass`, ordered **`Default, Analog, Clock, Power, USB,
SwitchNode`, then the rest in first-seen order** (today's list, so the four examples'
project files do not change bytes); `netclass_patterns` = `[{"netclass": c.name, "pattern":
p} for c in classes for p in c.patterns]` (today's form). Both `seed.emit_pro` and
`apply._apply_pro` call these two functions; `_apply_pro` keeps merging into existing rows
(colours, priority) as today.

Gate (`netcheck.check_copper`, S3): the result gains `"soft": {rule_name: hits}` counting
warnings whose description names one of pcbc's soft rules (`"rule '{name}'" in
description`, the form the canary check already uses), and `"rules": {rule_name: hits}` for
every pcbc rule; `drc_warnings` excludes soft-rule and geometry warnings and the canary.
`copper_drc_errors` is unchanged (errors only). `fab._IGNORE_DRC` drops
`length_out_of_range` (the canary is a warning and never reaches it; an explicit
`length_mm=` must gate) and keeps `diff_pair_gap_out_of_range` (KRT rewrites the 2L gap;
removing it is an R4 item). The copper bar prints `rules: track_width 0, skew 0, uncoupled 0,
vias 0` from `soft`, and `test_examples_fab.py` pins the counts per example beside the bar.
**Promotion procedure** (in the README row): a soft rule that hits zero on the four examples
and the DS2 Addon is switched to error in the same PR that shows the zeros; R4's tuning pass
promotes the rest. The report marks each soft number `[soft: warning in R1]`.

`check.py` (S3): `check_job` also fails on a missing `ISO_*` zone (`missing rule area
ISO_primary_secondary`), like keepouts today.

---

## F. Route-aware placement checks (`src/pcbc/route_checks.py`, S4)

```python
@dataclass
class Ctx:
    design: Design; job: CompiledJob; cs: ConstraintSet
    feet: dict[str, Foot]            # parsed, world-posed, pads' nets bound (as layout_report builds them)
    content: Rect; regions: dict[str, Rect]; keepouts: list[KeepoutSpec]
    pads_on: dict[str, list[tuple[str, str, float, float, float, float]]]   # net -> (ref, num, x, y, w, h) world

def route_aware_report(design: Design, job: CompiledJob, pcb_text: str) -> tuple[list[str], list[str]]  # (moves, style notes)
def check_airwires(ctx) -> list[str]; check_chains(ctx); check_corridors(ctx); check_loops(ctx) -> tuple[list[str], list[str]]
def check_keep_away(ctx); check_reference(ctx) -> tuple[list[str], list[str]]; check_isolation(ctx)
```

Called from `place.place_job` after `layout_report`: `moves, notes = route_aware_report(...)`;
`report += moves`, `notes += notes`. Pure, deterministic (refs sorted, pads in footprint
order), no copper read. Every message names refs, pins and numbers, and ends in an edit to
`board.py`. Distances are pad edge to pad edge (AABB gap, `Pad.w/h` turned by the footprint's
rotation) unless said. Existing `layout_report` checks (decap 2.5/5 mm, `max_mm` as
nearest-pad, edge, lane, overlap) are untouched.

**F.1 Airwire length and skew (R-L4).** For every net with `airwire_max_mm`: MST of its
pad centres (`copper_bar.airwire_mm`); `> airwire_max_mm` -> move (the existing nearest-pad
line stays; this one says the whole net). For every pair and bus: MST per member,
`spread = max - min`; `spread > match_mm` -> move; the router can only add length, so the
message says how much serpentine the short member needs and what to turn.
Messages: `AIN0: 31.4 mm of airwire (MST of 4 pads) over max_mm=30 (NetReq line 96); the route is longer still: Place("C7", to="U1.AIN0") or raise max_mm`;
`USB_DP/USB_DN: airwires 12.3 and 9.1 mm differ by 3.2 mm; skew budget 0.5 mm (preset usb_hs): Place("U3", to="J1.A6") evens them, or the router adds 2.7 mm of serpentine`.
Test: stock c3_usb prints nothing (measured: DP 24.175, DN 24.079, spread 0.096); c3_usb with
`Place("U3", position="absolute", left=30, top=12)` prints the line with its pinned numbers.

**F.2 Chain order, no stubs (R-X4, R-A2, R-D1).** For `Chain(net, p0..pn)`: world centres
`P_i`; `d = (P_n - P_0)/|P_n - P_0|`; `t_i = (P_i - P_0) . d` must be strictly increasing;
for each consecutive pair the corridor (segment `P_i P_{i+1}` swept by `width + 2 clearance`
of the net's class) must contain no other-net pad AABB. Implicit chain: every `usb_hs`,
`sense` net with 3+ pads and no `Chain` gets the same check on its pads sorted along the
first principal axis of their centres, and the message ends in the exact line to type.
Messages: `VDDA chain J2.1 -> C4.1 -> U1.12: C4.1 projects after U1.12 along the feed (t = 6.1 vs 4.8 mm); the cap must come first: Place("C4", to="U1.AVDD", toward="left")`;
`VDDA chain: R10.2 (nRESET) lies in the corridor between C4.1 and U1.12; move R10`;
`USB_DP has 3 pads (J1.A6, U3.1, U1.27): a third pad off the line is a stub; say the order: Chain("USB_DP", "J1.A6", "U3.1", "U1.27")`.
Test: DS2 with `Chain("VDDA", "J2.1", "C4.1", "U1.12")` passes as placed; with C4's `Place`
given `toward="right"` fails with the order message; c3_usb prints the implicit-stub line
until its board.py carries the `Chain` lines (S4 adds them: an intent line, never a
coordinate).

**F.3 Corridor for wide tracks (R-I4).** For every net whose class width >= 0.4 mm: grid
0.1 mm over the content rect (0.2 mm when width > 1 mm), one bitmap per allowed outer layer
(inner layers are planes, skipped). Obstacles: every other-net pad AABB on that layer (TH on
both), every keepout box, every closed row's fanout lane rect (`Foot.world_keep` minus
`world_box`, spent on vias), the edge band `edge_clearance`; each dilated by `ceil((width/2 +
clearance)/grid)` cells. For each MST edge (pad A, pad B) of the net: BFS 4-connected from A's
cells to B's cells; passable if any layer reaches. Failure names the pinch: the obstacle
owners nearest the straight segment A-B, sorted by distance.
Message: `VIN: no 0.78 mm channel from C_IN1.1 to U1.VIN on F.Cu or B.Cu (0.781 width + 2 x 0.2 clearance = 1.18 mm): R_EN.1 and C_EN.2 pinch it to 0.55 mm; Place("R_EN", to="U1.EN", toward="up") or NetReq("VIN", layers=["B.Cu"])`.
Performance bar: node under 3 s for all its wide nets (an acceptance criterion; the 0.2 mm
coarsening is the escape hatch). Test: buck as placed passes; buck with `R_EN` placed by CSS
between U1 and C_IN1 fails naming R_EN.

**F.4 Decap loop and hot loop (R-D1, R-D2).** Decap: for every cap `layout_report`'s
`by_pin` pairs with a pin: polygon [IC supply pad, cap supply pad, cap GND pad, the IC's
nearest GND pad]; shoelace area; `> loop_mm2` of the power constraint (6 mm2, pcbc default) ->
`style:` note, never a move in R1 (R-D1's area is V in the plan; c3_usb's four decaps measure
0.13-2.59 mm2). Hot loop, per `switch_node` net: the IC = the part with the SW pad and a pad
on a `power` net (the input rail); Cin = the nearest two-pin cap between that rail and a
ground net; low side = a two- or three-pin part with pads on SW and ground (diode or FET) if
one exists, else the IC's own GND pad (synchronous, buck's TPS54202). Polygon: [Cin.rail,
IC.rail, IC.SW, low.SW, low.GND, Cin.GND] (synchronous: [Cin.rail, IC.rail, IC.GND,
Cin.GND]); area `> loop_mm2` (20 mm2 default) -> move naming whichever of Cin / low side is
farther from the IC.
Message: `SW: hot loop C_IN1.1 -> U1.VIN -> U1.GND -> C_IN1.2 encloses 41.0 mm2 over the 20 mm2 budget (preset switch_node, pcbc default): Place("C_IN1", to="U1.VIN") closes it, or NetReq("SW", kind="switch_node", loop_mm2=45) records it as intent`.
A measured loop over the preset lands as a visible `loop_mm2=` line in the example's
board.py, never a recorded number. Test: buck stock -> note `SW: hot loop 3.3 mm2 (budget
20)`; buck with C_IN1 placed by CSS 8 mm away -> the move with its pinned area.

**F.5 Keep-away (R-D3, R-A1).** For each `KeepAway(other, mm)` on a constraint: minimum
edge-to-edge distance between the net's pads and (a) the other net's pads, (b) the courtyards
of parts with a pad on the other net, excluding pads of one footprint; `< mm` -> move,
`toward` = the quantised direction away from the offending pad.
Message: `FB: R_FB_TOP.2 is 1.84 mm from C_BOOT.2 (SW); keep_clear_mm=3 (NetReq line 51): Place("R_FB_TOP", to="U1.FB", toward="up"), or keep_clear_mm=1.5 if the boot cap must sit there`.
Test: buck with `NetReq("FB", kind="analog", keep_clear_of="SW")` prints that line (numbers
pinned: 1.84); stock buck (no `keep_clear_of`) prints nothing and the constraint report
carries the `keep_clear_of none` note of section D.

**F.6 Reference plane (R-Z3).** For each constraint with `reference` and `controlled`
impedance: 4L: `reference` must be a `planes=` layer adjacent to `layers[0]`
(`plane_below`), else a check refusal at compile (`USB_DP: 90 ohm on F.Cu wants a plane on
In1.Cu; Board(planes=[("GND", "In1.Cu")]) declares it`); at placement every pad of the net
must lie inside the content rect minus `edge_clearance` and outside any `Keepout` that
forbids copper (the plane is full-board today). 2L: the B.Cu pour is the reference: a
`style:` note when a `Keepout` lies under the straight line between the pair's pads.
Message: `USB_DP: J1.A6 sits in Keepout ANTENNA: no In1.Cu GND under it, the pair has no reference there; move the keepout or Place("J1", edge="bottom", left=6)`.
Test: node with `planes=[]` -> the compile refusal; node with the antenna keepout stretched
over J1 -> the move.

**F.7 Isolation line (R-V2).** For each `Isolation(a, b, volts, slot, across)`: rects `RA`,
`RB` of the two Regions; separating axis: x if they do not overlap in x, else y, else refusal
(`Isolation primary/secondary: Regions overlap in both axes; no straight line separates them`);
`gap` = distance between the rects along that axis; requirement = `max(clearance, creepage)`
from C.8 (with `slot=True`: `clearance` and `gap >= 1.0`); `gap < req` -> move naming the
Region to shift. Every part's courtyard must lie inside its side's rect unless the part is in
`across`; an `across` part's courtyard must span the gap; no pad of an `across` part may lie
in the gap.
Messages: `Isolation primary/secondary (250 V): Regions are 1.8 mm apart; needs 2.5 mm creepage (IEC 60664-1 F.5 row 250 V, PD2, IIIa): Region("secondary", left=+0.7)`;
`Isolation primary/secondary: C9 (secondary) crosses the corridor: move C9`;
`Isolation primary/secondary: U7 is in across= but does not span the gap; Place("U7", parent="primary", right=-1.0)`.
Output: the `ISO_{a}_{b}` rule area already computed at compile (E.15) is verified to match
the placed gap; with `slot=True` an `Edge.Cuts` slot rectangle 1.0 mm wide centred in the gap
(`apply._apply_slot`, S3; listed as R1's one fab-visible addition).
Test: a synthetic two-Region board in `tmp_path` (the `test_copper_rules.py` style) with an
optocoupler in `across=`: 2 mm apart at 250 V fails, 3 mm passes, the zone is in the placed
board; a part on the wrong side is named.

Each check is one function over `Ctx`, so S4 lands them one at a time; every message is
asserted by exact string in `tests/test_route_checks.py`.

---

## G. File partition (see also the Slices section at the end)

Order: **S1 -> S2 -> S3 | S4 | S5 in parallel.** S3, S4, S5 own disjoint files and import
only S2's public names (`compile_design`, `CompiledJob.constraints`, `ConstraintSet`,
`Constraint`, `Derived`, `DruRule`, `RuleArea`). `route.py`, `fanout.py`, `copper_bar.py`,
`blocking.py`, `css.py`, `layout.py` are not touched in R1.

`dru.py` is created by S2 as a stub containing exactly today's rule list rebuilt from the
`ConstraintSet` (`rules(cs)`: pads_of_one_footprint, the two geometry rules, keep-away, pair
gap, canary), imported by `compile.py`; S3 owns it from then on. `DruRule` lives in
`constraints.py` so `constraints <- dru <- compile` has no cycle; `compile.py` re-exports it.

---

## H. Risks and the decisions I am least sure of

**H.1 IPC-2221 external stays the floor over IPC-2152-with-plane.** Pick: `max(2221 ext,
2152 with modifiers)`. IPC-2152 only ever raises a width (a no-plane track above 1 A);
every example keeps its width byte for byte, which decides it for R1. The physics case for
dropping the floor when a plane is within 0.5 mm is real (Fig 5-11: 0.43 at 0.21 mm); R2 can
drop it per net once a board is built and measured. The vias-per-amp count is where IPC-2152
thinking already bites (node's 1 A LOAD on 0.2 mm drills wants 2 vias per change) and it is
a report line, not a gate, until the router can place two.

**H.2 `jlcpcb_4l_1oz` -> JLC04161H-7628 and node's pair.** Pick: the default is what JLC
builds. Node's USB pair widens from 0.1554/0.12 to 0.2291/0.15 (fits the USB-C's 0.3 mm
pads at 0.5 mm pitch: 0.2291 < `PAIR_FIT_MM`); `route_diff.py --impedance 90` re-routes it
and the bar (`node: vias 30, off45 64, micro 161, detour 1.72`) may move. S2's acceptance
runs node to fab under the real markers; a bar change is re-recorded with its reason in
`test_examples_fab.py`, as R0 did. Fallback if the wider pair fails the bar: map
`jlcpcb_4l_1oz` to 3313 (pair 0.1353/0.15) with 7628 under its own name.

**H.3 Soft severities in R1, and no neck-down areas.** Plan section 6.6 says everything
pcbc writes is an error; KRT is not held to length, skew, uncoupled length, via budgets or
a per-segment width floor, and fanout stubs on closed rows are `track_min` wide by design,
so those five rule kinds are warnings counted by the bar, pinned per example, with the
written promotion procedure. R-I3's neck areas (rule areas per closed row relaxing
`track_width`) are R2's: they need placement geometry, which would have made the DRU
depend on the place stage and given S3 and S4 a shared surface. The risk is that "warning"
reads as optional to the next AI; the pinned counts make a regression a test failure.

**H.4 The JLC bias instead of a mask model.** Two per-stackup factors fitted on two
stackups (two single-ended and four differential rows) recorded by a third party (JITX),
worst residual 2.7 % in impedance; 3313/2116 interpolated and labelled; 2L and inner
layers formula-only and labelled. The KiCad solder-mask term was not verifiable from a
source this design could open and closes a third of the gap on paper, so it is not
modelled; the mask thickness and Dk stay on the stackup for the day it is. The alternative
(rows only, refuse synthesis off-row) would make a 75 ohm request on 7628 a `check`
failure; synthesis with a printed +-3 % serves USB (+-15 %) and 100 ohm Ethernet (+-10 %),
and a real order asks JLC for impedance control anyway.

**H.5 Creepage: the general PD2 column, not "printed wiring" (2.5 mm at 250 V, not 1.0),
and the 60 V cut-off.** Conservative by 2.5x; IEC 62368-1 Table 17 is what R-V1 names and
was cross-checked only through KiCad's encoding of 60664-1 and TI's SLUP419 subset; the
implementer should open Table 17 once. The 60 V cut-off (ES1) is a policy so 3.3/5/12/48 V
rails never get a 1.2 mm creepage rule; a 48 V isolation is `Isolation(volts=48)`.

**H.6 No preset length rule for USB.** The plan's R-Z5 lists `length` among the USB rules;
no source bounds the board trace of a USB 2.0 pair (the spec bounds the cable and the
+-15 % impedance), so inventing 100 mm would be a pcbc default with nothing behind it.
`length_mm=` on the NetReq writes the rule; the report says `length: none (give length_mm=)`.

**H.7 KiCad rule semantics.** `!( ... )` grouping in a condition, `creepage` on `||`-lists
of names, `via_count` per net, `hasNetclass` in every class condition, the rule-order
exemption for a footprint's own pads: each was probed alone or is documented, none all in
one file. S3's probe test (each rule kind alone on a violating fixture, then all together,
canary firing every time) is the safety net; if `!( ... )` fails to parse, the fallback is
the ordered exemption rule (E.16 already last), and a rule kind that misbehaves is dropped to
a warning rather than left to disable the file.

**H.8 Corridor raster cost and false alarms (F.3).** Obstacles are pads, keepouts, lanes and
the edge, not courtyards (a track may legally cross a resistor's courtyard); a false alarm
needs a fully boxed pad, which KRT would fail on too. Node under 3 s is the bar.

**H.9 `Isolation` by `Place()` sides, not geometry.** A part placed by CSS at board level
is on no side and is a refusal. That is the honest failure and it forces the right order
(Regions first); the geometry check F.7 then verifies what compile assumed.

Not in R1 (named so nobody waits): return vias, plane-split verification, 3W parallel-run
measurement, serpentines, the ESD-at-connector check, B4 coated rows, thermal via arrays,
neck-down areas, membership netclasses, 2 oz stackups.

---

## Slices

Every slice ends green on `pytest` and, for S2 onward, on the four examples and the DS2
Addon to fab (`PCBC_REQUIRE_KICAD=1 PCBC_REQUIRE_KRT=1 pytest tests/test_examples_fab.py`
plus `pcbc build ~/Documents/MaD/Hardware/DS2Addon/pcbc/ds2_addon.py`), with its README
"Rule | Test" rows in the same PR. Order: **S1 -> S2 -> S3 | S4 | S5** (the last three in
parallel by different people).

### S1: stackup data + formulas

- Owns: `src/pcbc/stackup.py`; test file `tests/test_stackup.py` (new).
- Must not touch: anything else (`compile.py` keeps calling `microstrip_z0`, `width_for_z0`,
  `diff_pair_geometry`, `ipc2221_width_mm`, `hole_floor`, `get_stackup` with today's
  signatures).
- Adds: `Dielectric`, `Copper`, `ImpedanceRow`, the new `Stackup` fields and methods,
  `JLC_LIMITS`, the five stackups and JLC-code aliases, `microstrip`, `coupled_microstrip`,
  `stripline`, `stripline_asym`, `ipc2141_stripline`, `cpwg`, `elliptic_k`,
  `ipc2152_area_mil2`, `ipc2152_width_mm`, `current_width_mm`, `via_amps`,
  `vias_per_change`, `ipc2221_clearance_mm`, `iec_creepage_mm`, `iec_clearance_mm`,
  `capacitance_pf_per_mm`, `i2c_max_mm`, `solve_width`.
- Acceptance: the 26 vectors of C.10 pinned, each with its reference in the assertion
  message; every existing pin unchanged (`test_copper_rules.py::test_the_stackup_is_the_one_source_of_fab_limits`,
  `fanout_stagger`, `fanout_lane`, `board_rules`, `ipc2221_width_mm(2) == 0.781`);
  `get_stackup("jlcpcb_4l_1oz").jlc_code == "JLC04161H-7628"` and `get_stackup("JLC04161H-3313")`
  resolves; `pytest -m "not kicad and not krt"` green with the one expected change: node's
  compiled USB pair `(0.2291, 0.15)`, updated in `test_route_plan.py` if it pins the number
  (it pins `--impedance 90` only today), and `diff_pair_geometry(90, jlcpcb_2l_1oz) == (0.127, 0.127)`
  after the existing clamp in `compile.py`. `PCBC_REQUIRE_KICAD=1 PCBC_REQUIRE_KRT=1 pytest
  tests/test_examples_fab.py` green (node's bar re-recorded only if it moves, with the reason).

### S2: constraints + language

- Owns: `src/pcbc/constraints.py` (new), `src/pcbc/dru.py` (new stub: today's rules from
  the `ConstraintSet`; S3 owns it afterwards), `src/pcbc/model.py`, `src/pcbc/language.py`,
  `src/pcbc/circuit.py` (only the `refusals` call in `check_design`), `src/pcbc/compile.py`,
  `src/pcbc/__init__.py`; test file `tests/test_constraints.py` (new) and the snapshot
  fixtures `tests/fixtures/compiled/{blinky,buck,c3_usb,node,ds2_addon}.json` (recorded
  from today's `CompiledJob.to_dict()` before any change).
- Must not touch: `stackup.py`, `apply.py`, `seed.py`, `netcheck.py`, `fab.py`, `copper.py`,
  `check.py`, `pcb_place.py`, `place.py`, `cli.py`, `build.py`, docs.
- Acceptance: for the five boards, every key recorded in the fixture's `classes`, `nets`,
  `krt`, `keepouts`, `places`, `regions` equals today's value (new keys such as
  `lane_clearance_mm` are allowed); node's USB class is the one asserted difference
  (`0.2291 / 0.15`); an unknown kwarg is refused with the did-you-mean line and a kind-foreign
  kwarg with the accepted list, both citing the line; every kind of section D compiles on a
  synthetic 2L and 4L board and its report lines are pinned as exact strings (sources
  included); the DS2 lines are pinned (`AIN0: airwire 30 mm (NetReq line 96 overrides preset analog 25)` among them);
  `Pair`, `Bus`, `Chain`, `Isolation`, `Guard` load, validate, and every refusal of A.4 is
  pinned; a `NetReq(volts=48)` gets clearance 0.6 and no creepage rule, `volts=250` gets 1.25
  and creepage 2.5; `ConstraintSet.to_dict()` round-trips through `json.dumps(sort_keys=True)`
  identically on two compiles; `test_route_plan.py`, `test_fanout.py`, `test_copper_rules.py`
  green unchanged.

### S3: DRU + project + gate

- Owns: `src/pcbc/dru.py` (full body: `rules`, `render`, `validate`, `project_classes`,
  `netclass_patterns`), `src/pcbc/apply.py` (`write_dru` renders `job.dru` after `validate`;
  `_apply_pro` uses `project_classes`/`netclass_patterns`; `_apply_rule_areas`; `_apply_slot`),
  `src/pcbc/seed.py` (`emit_pro` uses the same two functions), `src/pcbc/netcheck.py`
  (`soft`, `rules` counts), `src/pcbc/fab.py` (`_IGNORE_DRC`, `_write_notes` prints
  `cs.lines`), `src/pcbc/copper.py` (`power_ampacity_failures` reads the constraint),
  `src/pcbc/check.py` (missing rule-area zone); test file `tests/test_dru.py` (new; the
  `kicad`-marked probes).
- Must not touch: `compile.py`, `constraints.py`, `pcb_place.py`, `place.py`,
  `route_checks.py`, `cli.py`, `build.py`, docs, `test_examples_fab.py` except to add the
  `soft` counts row.
- Acceptance: every row of E has a test pinning the rendered rule text for a synthetic board
  (exact string); `validate` rejects `(min 135deg)`, an unknown constraint name and a
  duplicate name; `render` of the four examples parses (the `kicad` probe: each rule kind
  written alone into the built DS2 board on a deliberately violating fixture, then all
  together, the canary firing every time and the intended violation type present:
  `length_out_of_range`, `skew_out_of_range`, `too_many_vias`, `creepage`, `clearance`,
  `items_not_allowed`, `diff_pair_uncoupled_length_too_long`, `track_width`);
  `seed.emit_pro` and `apply._apply_pro` produce identical class rows and the four examples'
  `.kicad_pro` files are byte-identical to today's; `check_copper` returns `soft` and
  `rules`; `test_pcbc_writes_the_geometry_rules_and_a_canary_that_must_fire` green; the four
  examples and DS2 pass the gate with their recorded bar, and their `soft` counts are pinned.

### S4: route-aware placement checks

- Owns: `src/pcbc/route_checks.py` (new), `src/pcbc/pcb_place.py` (only `lane_rules`
  reading `lane_clearance_mm`), `src/pcbc/place.py` (the one call), and the examples' and
  DS2's `board.py` only to add an intent line a check asks for (`Chain(...)`, `loop_mm2=`;
  never a coordinate); test file `tests/test_route_checks.py` (new).
- Must not touch: `dru.py`, `apply.py`, `netcheck.py`, `fab.py`, `copper.py`, `check.py`,
  `cli.py`, `build.py`, docs.
- Acceptance: each of F.1-F.7 has a passing case on a real example and a failing case with
  the exact message (numbers pinned: c3_usb skew 0.096, buck hot loop 3.3, buck FB-to-boot-cap
  1.84); `pcb_job` on the five boards adds no new move (an intent line added is shown in the
  PR); node `pcb_job` wall time under 3 s more than today; `test_the_same_board_places_the_same`,
  `test_pcb_place.py`, `test_node.py::test_node_layout_bar`, `test_c3_usb_layout_bar` green.

### S5: CLI + docs

- Owns: `src/pcbc/cli.py` (`pcbc check board.py --constraints [--json]`; `pcbc pcb` prints
  `constraints:` lines with `--constraints`), `src/pcbc/build.py` (the check step and
  `pcb_job` carry `"constraints": cs.lines` / `cs.to_dict()` under `--json`; the route step
  carries `soft`), `README.md` (a "Constraints" section: the language block, the report
  example, a "Rule | Test" table with one row per E and F entry naming its test),
  `docs/router-plan.md` (section 8 R1 status; section 3's "pcbc today" column),
  `docs/copper-plan.md` (seen/now rows: the silent-kwarg bug, the IPC-2141 validity bug, the
  `h 0.12` stackup, the boot-cap keep-away finding), `docs/constraints.md` (new: every
  formula with its reference, the calibration table of C.2 with residuals, the seen/now table),
  `.claude/skills/pcbc/SKILL.md` (the language lines below, "read `pcbc check --constraints`
  before placing", "never type a number the report can derive"); test file `tests/test_cli.py`
  (additions).
- Must not touch: any `src/pcbc/*.py` other than `cli.py` and `build.py`; no test file other
  than `test_cli.py`.
- Acceptance: `pcbc check ds2_addon.py --constraints` prints, after the check lines, one line
  per number in file-then-net order, pinned in
  `test_cli.py::test_check_constraints_prints_every_number_with_its_source`; `--json` prints
  `ConstraintSet.to_dict()` with the keys pinned; exit code as `check` today (a refusal is 1;
  the 2L USB note is not a failure); CI's example job runs `pcbc check --constraints` on every
  example; the three docs and the skill updated in the same PR.

The SKILL.md language block (S5), what the AI writes:

```python
NetReq("VBUS", "3V3", "GND", kind="power", volts=5, amps=1)        # width, vias per change, clearance row
NetReq("HV+", "HV-", kind="power", volts=300)                       # clearance and creepage rows
NetReq("SW", kind="switch_node")                                    # one layer, no vias, hot loop budget
NetReq("FB", kind="feedback", keep_clear_of="SW")                   # far from SW, no vias, one layer
NetReq("AIN0", "AIN1", kind="analog", keep_clear_of="SW", keep_clear_mm=3)
NetReq("USB_DP", "USB_DN", kind="usb_hs")                           # 90 ohm pair, skew 0.5, uncoupled 2
NetReq("SCK", "MOSI", "MISO", kind="spi", clock="SCK")              # matched to the clock
NetReq("SDA", "SCL", kind="i2c", pf_max=400)                        # length from capacitance
NetReq("SENSE+", "SENSE-", kind="sense")                            # Kelvin pair: same layer, matched
Pair("USB_DP", "USB_DN", z_diff_ohm=90, match_mm=0.5)               # explicit form of the preset
Bus("D0", "D1", "D2", "D3", match_mm=1.0, clock="CLK")
Chain("VDDA", "J2.1", "C4.1", "U1.12")                              # feed order: cap before pin
Isolation("primary", "secondary", volts=250, across=("U7",), slot=True)  # two Regions, the isolators that span them
Guard("AIN0", stitch_mm=2.5)
```

The report line format (S2 defines `Derived.line()` and `cs.lines`; S5 prints them), one
number per line, sorted by net, the source in parentheses; pinned for node:

```
USB_DP: pair with USB_DN, 0.2291 mm wide, gap 0.15 mm on F.Cu over In1.Cu (GND): 89.9 ohm (hj_coupled_microstrip x 0.85 JLC04161H-7628; JLC row 0.2332/0.15; target 90 +-15 %)
USB_DP: clearance 0.18 mm (class_floor hole_clearance 0.25 - ring 0.075 + 0.005); via 0.35/0.2 mm (stackup), at most 2 (preset usb_hs) [soft: warning in R1]
USB_DP: skew 0.5 mm, uncoupled 2 mm (preset usb_hs; TI usb_layout_basics) [soft: warning in R1]; length none (give length_mm=)
VBUS: width 0.4 mm (pcbc_floor amps >= 0.2; ipc2221_ext 1 A 10 C 1 oz 0.300; ipc2152_fit x board 1.092 x plane 0.430 at 0.2104 mm In1.Cu 0.133)
VBUS: clearance 0.2 mm (preset power; ipc2221_6_1 row 0-15 V B2 0.1); via 0.8/0.4 mm (preset power), 1 per layer change (via_barrel 0.4/0.018 mm 0.871 A at 10 C)
LOAD: width 0.4 mm (pcbc_floor; ipc2221_ext 1 A 0.300); via 0.35/0.2 mm (stackup), 2 per layer change (via_barrel 0.2/0.018 mm 0.527 A at 10 C) [report only in R1]
T_DIV: width 0.2 mm, clearance 0.2 mm (preset analog); no vias; F.Cu; airwire 25 mm (preset analog); keep_clear_of none; spacing 5W
classes: Default 0.16/0.18, USB 0.2291/0.18 pair 0.2291/0.15, Power 0.4/0.2 via 0.8/0.4, Analog 0.2/0.2 via 0.6/0.3
rules: 12 written (7 error, 5 soft), canary on net 3V3
```
