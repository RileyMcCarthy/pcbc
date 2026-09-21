"""Named stackups and electrical -> geometry solvers (R1 section B and C of docs/r1-design.md).

Everything here is pure and deterministic: bisection solvers run a fixed 60 iterations from
`stack.track_min` to 6.0 mm and round to 4 dp, tables are tuples, and no dict order is relied on.

Formulas and their references:

- microstrip: Hammerstad and Jensen 1980, static, with the strip-thickness correction and the
  `(Z01(u1)/Z01(ur))^2` factor KiCad's `microstrip.cpp` keeps.
- coupled_microstrip: Hammerstad and Jensen 1980 even/odd (Q1..Q10), static.
- stripline / stripline_asym: Wadell 1991 section 4.2.1 (Cohn with thickness) and the parallel
  combination for an asymmetric line.
- cpwg: Ghione and Naldi 1987 conductor-backed coplanar waveguide, t = 0.
- ipc2221_width_mm / ipc2152_width_mm: IPC-2221B eq. 6-2 and the IPC-2152 chart fits
  (Figures 5-1, 5-8, 5-11 as digitised by mbedded.ninja).
- via_amps: IPC-2221 internal curve on the plated barrel area.
- ipc2221_clearance_mm: IPC-2221B Table 6-1; iec_creepage_mm / iec_clearance_mm: IEC 60664-1
  Tables F.5, F.1 and F.2 at pollution degree 2.

The per-stackup `z_bias_*` factors are the fab calibration of section C.2: JLC's own solver
models the solder mask and the etch trapezoid, the closed forms do not, so every impedance a
stackup reports is `z_bias * Z_bare` and every width is solved for `Z_target / z_bias`.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

ETA0 = 376.730313668  # ohm, impedance of free space
C_MM_PER_NS = 299.792458  # mm / ns
PAIR_FIT_MM = 0.25  # a pair member wider than this cannot leave a USB-C's 0.3 mm pads at 0.5 mm pitch
SOLVE_ITERATIONS = 60
SOLVE_HI_MM = 6.0
OZ_MIL = 1.378  # 1 oz copper in mil
MIL2_PER_MM2 = 1.0 / 0.00064516


# ---------------------------------------------------------------------------------------------
# Derived numbers: a value plus where it came from. constraints.py (S2) is the home the design
# names for these; they are defined here so `current_width_mm` can return one without importing
# a module above this one. S2 re-exports them from constraints.py.
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Source:
    formula: str  # a FORMULAS name, or "preset" | "NetReq" | "Pair" | "Bus" | "stackup" | "pcbc_floor" | "class_floor"
    ref: str  # what the formula was evaluated against
    note: str = ""  # free text the report appends


@dataclass(frozen=True)
class Derived:
    value: float  # rounded: mm 4 dp, ohm 2 dp, A 3 dp, mm2 1 dp, pF/mm 4 dp
    unit: str  # "mm" | "ohm" | "A" | "V" | "mm2" | "pF/mm" | "count"
    source: Source

    def line(self) -> str:
        """`0.781 mm (ipc2221_ext IPC-2221B eq. 6-2 external 2 A 10 C 1 oz; ...)`."""
        inner = f"{self.source.formula} {self.source.ref}".strip()
        if self.source.note:
            inner = f"{inner}; {self.source.note}"
        return f"{self.value:g} {self.unit} ({inner})"


# ---------------------------------------------------------------------------------------------
# Stackup data
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Dielectric:
    material: str  # "7628" | "3313" | "2116" | "1080" | "core"
    thickness_mm: float
    dk: float
    kind: str  # "prepreg" | "core"


@dataclass(frozen=True)
class Copper:
    layer: str  # "F.Cu", "In1.Cu", "In2.Cu", "B.Cu"
    thickness_mm: float


@dataclass(frozen=True)
class ImpedanceRow:
    structure: str  # "se" | "diff"
    z_ohm: float
    width_mm: float
    gap_mm: float | None
    source: str


@dataclass(frozen=True)
class Stackup:
    """A named fab stackup: the layer stack for impedance and current, and the fab's limits.

    The limits are the one source for the board's constraints (`.kicad_pro`), the net
    classes' vias, the router's floor, and the copper gate. KiCad's own defaults were
    judging the copper before these were written.
    """

    name: str
    layers: int
    jlc_code: str | None  # "JLC04161H-7628"; None for the 2-layer default
    stack: tuple[Copper | Dielectric, ...]  # top to bottom
    copper_oz: float = 1.0
    track_min: float = 0.127  # mm; JLC 1-2 layer 5 mil
    clearance_min: float = 0.127
    via_drill: float = 0.3  # the standard via, no extra cost
    via_diameter: float = 0.5
    annular_min: float = 0.1
    # Copper to a hole's edge. JLC quotes 10 mil (0.254); KRT keeps tracks off an NPTH by this on
    # its 0.05 mm grid and lands at 0.25, so the gate holds 0.25 (4 um under any fab's tolerance).
    hole_clearance: float = 0.25
    hole_to_hole: float = 0.5
    edge_clearance: float = 0.3  # copper to the board edge
    # The solder-mask dam a fab will still print between two openings (JLC's published minimum on
    # its 1 oz rungs). Read by nothing but A.4 rule 4, which is **advisory in R2**: a candidate that
    # fails only the mask rule is accepted and emits a `style:` note, because KiCad's own
    # `solder_mask_bridge` check is what gates the build and pcbc has not yet measured what 0.10
    # costs in tap sites (docs/r2-design.md A.4). No compiled number reads it, so no
    # `tests/fixtures/compiled/*.json` moves.
    mask_bridge_min: float = 0.0
    # Every via tented, both sides, and it is pcbc's fact rather than KiCad's default. Measured:
    # every tap sits at exactly `clearance_min` from the pad it welds (0.0889 mm on node, 0.1270 mm
    # on the two-layer rungs) and a pad's mask opening is its copper here (`pad_to_mask_clearance 0`,
    # `solder_mask_margin 0`), so with the vias open the dam between a 0402's opening and a tap ring
    # is 0.0889 mm — under `mask_bridge_min`, the sliver a fab drops — and pad and barrel end up in
    # one opening. `(tenting (front yes) (back yes))` is what keeps solder out of the barrel, and
    # until S5's review nothing in pcbc wrote it or checked it: the stanza on every routed board was
    # KiCad 10's own default (`docs/r2-measurements.md` S5r, finding 3).
    via_tenting: bool = True
    mask_over_substrate_mm: float = 0.0305  # JLC impedance page: 1.2 mil
    mask_over_trace_mm: float = 0.0152  # 0.6 mil
    mask_dk: float = 3.8
    via_plating_mm: float = 0.018  # JLC capabilities: "average hole plating 18 um"
    z_bias_se: float = 1.0  # multiplies the bare formula's Z0 (section C.2)
    z_bias_diff: float = 1.0
    z_bias_source: str = ""
    rows: tuple[ImpedanceRow, ...] = ()

    # -- derived views kept for every existing caller -------------------------------------
    @property
    def h_mm(self) -> float:
        """The dielectric under F.Cu (microstrip height)."""
        return self._dielectrics()[0].thickness_mm

    @property
    def t_mm(self) -> float:
        """The outer copper thickness."""
        return self._coppers()[0].thickness_mm

    @property
    def er(self) -> float:
        """Dk of the dielectric under F.Cu."""
        return self._dielectrics()[0].dk

    @property
    def board_mm(self) -> float:
        """The finished thickness: the sum of the stack, 4 dp."""
        return round(sum(m.thickness_mm for m in self.stack), 4)

    def _coppers(self) -> tuple[Copper, ...]:
        return tuple(m for m in self.stack if isinstance(m, Copper))

    def _dielectrics(self) -> tuple[Dielectric, ...]:
        return tuple(m for m in self.stack if isinstance(m, Dielectric))

    def copper_layers(self) -> tuple[str, ...]:
        return tuple(c.layer for c in self._coppers())

    def _index(self, layer: str) -> int:
        for i, m in enumerate(self.stack):
            if isinstance(m, Copper) and m.layer == layer:
                return i
        raise KeyError(f"stackup {self.name!r} has no copper layer {layer!r}; it has {', '.join(self.copper_layers())}")

    def copper_t(self, layer: str) -> float:
        m = self.stack[self._index(layer)]
        assert isinstance(m, Copper)
        return m.thickness_mm

    def dielectric_between(self, a: str, b: str) -> tuple[float, float]:
        """(height, series Dk) between two copper layers: every dielectric and every copper the
        span crosses adds to the height; the Dk is the series value `h_total / sum(h_i / dk_i)`."""
        i, j = sorted((self._index(a), self._index(b)))
        if i == j:
            raise ValueError(f"dielectric_between needs two different layers, got {a!r} twice")
        between = self.stack[i + 1 : j]
        height = sum(m.thickness_mm for m in between)
        inv = sum(m.thickness_mm / m.dk for m in between if isinstance(m, Dielectric))
        return round(height, 4), round(height / inv, 4)

    def plane_below(self, layer: str) -> str | None:
        """The next copper layer under `layer`: "F.Cu" -> "In1.Cu"; "B.Cu" -> None."""
        names = self.copper_layers()
        i = names.index(layer)
        return names[i + 1] if i + 1 < len(names) else None

    def plane_above(self, layer: str) -> str | None:
        names = self.copper_layers()
        i = names.index(layer)
        return names[i - 1] if i > 0 else None

    def height_to_reference(
        self, layer: str, planes: tuple[tuple[str, str], ...], two_layer_pour: bool
    ) -> tuple[float, str] | None:
        """(dielectric height, reference layer) for a track on `layer`: the adjacent declared plane, or on a
        2-layer board the B.Cu GND pour the route plan always writes (reference "B.Cu pour"); None otherwise."""
        declared = {lay for _net, lay in planes}
        found: list[tuple[float, int, str]] = []
        for order, ref in ((0, self.plane_below(layer)), (1, self.plane_above(layer))):
            if ref is not None and ref in declared:
                h, _dk = self.dielectric_between(layer, ref)
                found.append((h, order, ref))
        if found:
            h, _order, ref = min(found)
            return h, ref
        if two_layer_pour and self.layers == 2 and layer == "F.Cu":
            h, _dk = self.dielectric_between("F.Cu", "B.Cu")
            return h, "B.Cu pour"
        return None


# JLC's own 1 oz / 2 oz track / clearance minima (mm), for the report only: pcbc's limits below
# are the ones every board is held to. Key: (two_layer, copper_oz) -> (track, clearance).
JLC_LIMITS: dict[tuple[bool, float], tuple[float, float]] = {
    (True, 1.0): (0.10, 0.10),
    (False, 1.0): (0.09, 0.09),
    (True, 2.0): (0.16, 0.16),
    (False, 2.0): (0.15, 0.15),
}

_JITX = "JLCPCB impedance calculator as recorded by JITX, jitxlib.jlcpcb.{code}, read 2026-09-19"
_ROWS_7628 = (
    ImpedanceRow("se", 50.0, 0.3244, None, _JITX.format(code="JLC04161H_7628")),
    ImpedanceRow("diff", 90.0, 0.2332, 0.15, _JITX.format(code="JLC04161H_7628")),
    ImpedanceRow("diff", 100.0, 0.1722, 0.15, _JITX.format(code="JLC04161H_7628")),
)
_ROWS_1080 = (
    ImpedanceRow("se", 50.0, 0.1176, None, _JITX.format(code="JLC04161H_1080")),
    ImpedanceRow("diff", 90.0, 0.09, 0.09, _JITX.format(code="JLC04161H_1080")),
    ImpedanceRow("diff", 100.0, 0.09, 0.137, _JITX.format(code="JLC04161H_1080")),
)

# jlcpcb.com/impedance (read 2026-09-19): prepreg 7628 0.2104 mm Dk 4.4, 3313 0.0994 / 4.1,
# 2116 0.1164 / 4.16, 1080 0.0764 / 3.91; core Dk 4.6 (the 1.6 mm build's core, `verify`:
# it only affects stripline and In1->In2 heights); outer copper 0.035; inner 0.0152.
_OUTER = 0.035
_INNER = 0.0152
_CORE_DK = 4.6


def _four_layer(prepreg: str, h: float, dk: float, core: float) -> tuple[Copper | Dielectric, ...]:
    return (
        Copper("F.Cu", _OUTER),
        Dielectric(prepreg, h, dk, "prepreg"),
        Copper("In1.Cu", _INNER),
        Dielectric("core", core, _CORE_DK, "core"),
        Copper("In2.Cu", _INNER),
        Dielectric(prepreg, h, dk, "prepreg"),
        Copper("B.Cu", _OUTER),
    )


_FOUR_LAYER_LIMITS = dict(track_min=0.0889, clearance_min=0.0889, via_drill=0.2, via_diameter=0.35, annular_min=0.075, mask_bridge_min=0.10)
_FITTED = "fitted to JLC04161H-{code} rows (JITX), 2026-09-19"
_INTERPOLATED = "interpolated between 7628 and 1080 fits; capture JLC rows to replace"

STACKUPS: dict[str, Stackup] = {
    "jlcpcb_4l_1oz": Stackup(
        name="jlcpcb_4l_1oz",
        layers=4,
        jlc_code="JLC04161H-7628",
        stack=_four_layer("7628", 0.2104, 4.4, 1.065),
        z_bias_se=0.9147,
        z_bias_diff=0.8532,
        z_bias_source=_FITTED.format(code="7628"),
        rows=_ROWS_7628,
        **_FOUR_LAYER_LIMITS,
    ),
    "jlcpcb_4l_1oz_3313": Stackup(
        name="jlcpcb_4l_1oz_3313",
        layers=4,
        jlc_code="JLC04161H-3313",
        stack=_four_layer("3313", 0.0994, 4.1, 1.265),
        z_bias_se=0.9058,
        z_bias_diff=0.842,
        z_bias_source=_INTERPOLATED,
        **_FOUR_LAYER_LIMITS,
    ),
    "jlcpcb_4l_1oz_2116": Stackup(
        name="jlcpcb_4l_1oz_2116",
        layers=4,
        jlc_code="JLC04161H-2116",
        stack=_four_layer("2116", 0.1164, 4.16, 1.265),
        z_bias_se=0.9058,
        z_bias_diff=0.842,
        z_bias_source=_INTERPOLATED,
        **_FOUR_LAYER_LIMITS,
    ),
    "jlcpcb_4l_1oz_1080": Stackup(
        name="jlcpcb_4l_1oz_1080",
        layers=4,
        jlc_code="JLC04161H-1080",
        stack=_four_layer("1080", 0.0764, 3.91, 1.265),
        z_bias_se=0.8968,
        z_bias_diff=0.8308,
        z_bias_source=_FITTED.format(code="1080"),
        rows=_ROWS_1080,
        **_FOUR_LAYER_LIMITS,
    ),
    "jlcpcb_2l_1oz": Stackup(
        name="jlcpcb_2l_1oz",
        layers=2,
        jlc_code=None,
        stack=(Copper("F.Cu", _OUTER), Dielectric("core", 1.53, _CORE_DK, "core"), Copper("B.Cu", _OUTER)),
        z_bias_se=1.0,
        z_bias_diff=1.0,
        z_bias_source="uncalibrated: formula only",
        mask_bridge_min=0.10,
    ),
}

STACKUP_ALIASES: dict[str, str] = {s.jlc_code: name for name, s in sorted(STACKUPS.items()) if s.jlc_code}


def get_stackup(name: str) -> Stackup:
    """By pcbc name or by JLC code (`Board(stackup="JLC04161H-3313")`)."""
    if name in STACKUPS:
        return STACKUPS[name]
    if name in STACKUP_ALIASES:
        return STACKUPS[STACKUP_ALIASES[name]]
    known = ", ".join(sorted(STACKUPS))
    codes = ", ".join(sorted(STACKUP_ALIASES))
    raise KeyError(f"unknown stackup {name!r}. known: {known} (or a JLC code: {codes})")


def is_outer(layer: str) -> bool:
    return layer in ("F.Cu", "B.Cu")


# ---------------------------------------------------------------------------------------------
# Fab-limit derived numbers (unchanged from R0)
# ---------------------------------------------------------------------------------------------


def hole_floor(stack: Stackup) -> float:
    """The copper clearance at which a track next to a via's ring also clears its hole."""
    return round(stack.hole_clearance - stack.annular_min + 0.005, 4)


def pass_mm(stack: Stackup) -> float:
    """The narrowest gap between two pads that the thinnest track can still pass through."""
    return round(stack.track_min + 2 * stack.clearance_min, 4)


def fanout_stagger(stack: Stackup, clearance: float, pitch: float) -> float:
    """How far past its neighbour's a via must step on a row of this pitch: two holes keep the
    fab's hole-to-hole, two rings keep the class clearance. 0.47 mm on a 0.65 mm row at JLC 2L."""
    need = max(stack.via_drill + stack.hole_to_hole, stack.via_diameter + max(clearance, stack.clearance_min))
    return round(math.sqrt(max(0.0, need * need - pitch * pitch)), 4)


def fanout_lane(stack: Stackup, clearance: float, pitch: float) -> float:
    """The free lane a closed pad row needs outside it: the router keeps a via `clearance_min` off
    its own pad, then the via, then the next row's stagger, then clearance to whatever sits
    beyond. `clearance` is the widest class's. 1.29 mm on a 0.65 mm row at JLC's 2-layer rung."""
    return round(stack.clearance_min + stack.via_diameter + max(clearance, stack.clearance_min) + fanout_stagger(stack, clearance, pitch), 4)


def board_rules(stack: Stackup) -> dict[str, float]:
    """The `.kicad_pro` design_settings.rules block."""
    return {
        "min_clearance": stack.clearance_min,
        "min_track_width": stack.track_min,
        "min_via_diameter": stack.via_diameter,
        "min_via_annular_width": stack.annular_min,
        "min_through_hole_diameter": stack.via_drill,
        "min_hole_clearance": stack.hole_clearance,
        "min_hole_to_hole": stack.hole_to_hole,
        "min_copper_edge_clearance": stack.edge_clearance,
    }


# ---------------------------------------------------------------------------------------------
# C.1 Microstrip, single: Hammerstad and Jensen 1980, static, with strip thickness
# ---------------------------------------------------------------------------------------------


def _hj_z01(u: float) -> float:
    f = 6.0 + (2.0 * math.pi - 6.0) * math.exp(-((30.666 / u) ** 0.7528))
    return (ETA0 / (2.0 * math.pi)) * math.log(f / u + math.sqrt(1.0 + (2.0 / u) ** 2))


def _hj_a(u: float) -> float:
    return 1.0 + math.log((u**4 + (u / 52.0) ** 2) / (u**4 + 0.432)) / 49.0 + math.log(1.0 + (u / 18.1) ** 3) / 18.7


def _hj_b(er: float) -> float:
    return 0.564 * ((er - 0.9) / (er + 3.0)) ** 0.053


def _hj_eeff(u: float, er: float) -> float:
    return (er + 1.0) / 2.0 + (er - 1.0) / 2.0 * (1.0 + 10.0 / u) ** (-_hj_a(u) * _hj_b(er))


def _hj_du(u: float, th: float, er: float) -> float:
    """Hammerstad and Jensen's strip-thickness widening of u, for a medium of Dk `er`."""
    if th <= 0:
        return 0.0
    d = (th / math.pi) * math.log(1.0 + (4.0 * math.e) * math.tanh(math.sqrt(6.517 * u)) ** 2 / th)
    return 0.5 * d * (1.0 + 1.0 / math.cosh(math.sqrt(er - 1.0)))


def microstrip(w: float, h: float, t: float, er: float) -> tuple[float, float]:
    """(Z0, eeff) of a microstrip of width `w` over dielectric `h`, strip thickness `t`, Dk `er`.

    Hammerstad and Jensen 1980 eqs. 1-8 with the thickness corrections, assembled exactly as
    KiCad 10.0.6's `pcb_calculator/transline/microstrip.cpp::microstrip_Z0()` with no cover:

        du1 = du(u, t/h, er=1);  ur = u + du(u, t/h, er)
        q   = q_inf(ur, er) - q_t,  q_t = (2 ln2 / pi) (t/h) / sqrt(ur)     [delta_q_thickness]
        eeff_t = (er + 1)/2 + q (er - 1)/2
        Z0     = Z01(ur) / sqrt(eeff_t)

    The returned `eeff` is `eeff_t`, the permittivity Z0 is built from, so the pair is
    self-consistent for the coupled model. KiCad additionally *reports* `eeff_t *
    (Z01(u1)/Z01(ur))^2`, which is a different number (3.0501 against 3.1999 on the 7628 row).
    An earlier pcbc put that ratio factor into Z0 instead and matched neither this nor pure
    H&J (55.165 ohm where KiCad gives 54.660 and bare H&J 53.858); the calibration in C.2 is
    fitted to whatever the bare formula is, so the synthesised widths were and remain JLC's.
    Valid 0.01 <= w/h <= 100, 1 <= er <= 128, 0.2 % (H&J).
    """
    if w <= 0 or h <= 0 or er < 1:
        raise ValueError(f"microstrip needs w > 0, h > 0, er >= 1; got w={w}, h={h}, er={er}")
    u = w / h
    th = t / h if t > 0 else 0.0
    ur = u + _hj_du(u, th, er)
    q_t = (2.0 * math.log(2.0) / math.pi) * (th / math.sqrt(ur)) if th > 0 else 0.0
    q = (1.0 + 10.0 / ur) ** (-_hj_a(ur) * _hj_b(er)) - q_t
    eeff_t = (er + 1.0) / 2.0 + q * (er - 1.0) / 2.0
    z0 = _hj_z01(ur) / math.sqrt(eeff_t)
    return z0, eeff_t


def microstrip_reported_eeff(w: float, h: float, t: float, er: float) -> float:
    """The effective permittivity KiCad's calculator *displays*: `eeff_t (Z01(u1)/Z01(ur))^2`.
    Not what Z0 is built from; kept so a number can be checked against the calculator's screen."""
    u = w / h
    th = t / h if t > 0 else 0.0
    u1 = u + _hj_du(u, th, 1.0)
    ur = u + _hj_du(u, th, er)
    return microstrip(w, h, t, er)[1] * (_hj_z01(u1) / _hj_z01(ur)) ** 2


# ---------------------------------------------------------------------------------------------
# C.3 Coupled microstrip, edge-coupled: Hammerstad and Jensen 1980 even/odd, static
# ---------------------------------------------------------------------------------------------


def coupled_microstrip(w: float, s: float, h: float, t: float, er: float) -> tuple[float, float, float, float]:
    """(Ze, Zo, eps_e, eps_o) of an edge-coupled microstrip pair; Zdiff = 2 Zo.

    Hammerstad and Jensen 1980, "Coupled microstrip" (Q1..Q10 theirs; no dispersion). `z0` and
    `eeff` are C.1's thickness-corrected single line at width `w`; `u = w/h` is the bare ratio
    (not `ur`) and `g = s/h`, so thickness enters only through `z0` and `eeff`. Valid
    0.1 <= u <= 10, 0.1 <= g <= 10. KiCad's `coupled_microstrip.cpp` uses separate even/odd
    thickness widths, which is close on the 7628 rows (under 0.6 ohm) and not on the thin ones:
    3.1 ohm at the 1080 90 ohm row and 4.5 ohm at its 100 ohm row (2.7 to 3.8 %). The per-stackup
    bias of C.2 is fitted against JLC's published rows, so that difference is absorbed where a
    row exists and is stated as `interpolated` or `formula only` where none does.
    """
    if s <= 0:
        raise ValueError(f"coupled_microstrip needs s > 0; got s={s}")
    z0, eeff = microstrip(w, h, t, er)
    u = w / h
    g = s / h
    v = u * (20.0 + g * g) / (10.0 + g * g) + g * math.exp(-g)
    eps_e = (er + 1.0) / 2.0 + (er - 1.0) / 2.0 * (1.0 + 10.0 / v) ** (-_hj_a(v) * _hj_b(er))
    a_o = 0.7287 * (eeff - (er + 1.0) / 2.0) * (1.0 - math.exp(-0.179 * u))
    b_o = 0.747 * er / (0.15 + er)
    c_o = b_o - (b_o - 0.207) * math.exp(-0.414 * u)
    d_o = 0.593 + 0.694 * math.exp(-0.562 * u)
    eps_o = ((er + 1.0) / 2.0 + a_o - eeff) * math.exp(-c_o * g**d_o) + eeff
    q1 = 0.8695 * u**0.194
    q2 = 1.0 + 0.7519 * g + 0.189 * g**2.31
    q3 = 0.1975 + (16.6 + (8.4 / g) ** 6) ** -0.387 + math.log(g**10 / (1.0 + (g / 3.4) ** 10)) / 241.0
    q4 = 2.0 * q1 / q2 / (u**q3 * math.exp(-g) + (2.0 - math.exp(-g)) * u**-q3)
    ze = z0 * math.sqrt(eeff / eps_e) / (1.0 - z0 * math.sqrt(eeff) * q4 / ETA0)
    q5 = 1.794 + 1.14 * math.log(1.0 + 0.638 / (g + 0.517 * g**2.43))
    q6 = 0.2305 + math.log(g**10 / (1.0 + (g / 5.8) ** 10)) / 281.3 + math.log(1.0 + 0.598 * g**1.154) / 5.1
    q7 = (10.0 + 190.0 * g * g) / (1.0 + 82.3 * g**3)
    q8 = math.exp(-6.5 - 0.95 * math.log(g) - (g / 0.15) ** 5)
    q9 = math.log(q7) * (q8 + 1.0 / 16.5)
    q10 = (q2 * q4 - q5 * math.exp(math.log(u) * q6 * u**-q9)) / q2
    zo = z0 * math.sqrt(eeff / eps_o) / (1.0 - z0 * math.sqrt(eeff) * q10 / ETA0)
    return ze, zo, eps_e, eps_o


def zdiff(w: float, s: float, h: float, t: float, er: float) -> float:
    """Differential impedance of an edge-coupled microstrip pair: 2 Zo."""
    return 2.0 * coupled_microstrip(w, s, h, t, er)[1]


# ---------------------------------------------------------------------------------------------
# C.4 Stripline: Wadell 4.2.1 (Cohn) with thickness; asymmetric by parallel combination
# ---------------------------------------------------------------------------------------------


def stripline(w: float, b: float, t: float, er: float) -> float:
    """Z0 of a centred stripline of width `w` between planes `b` apart, strip thickness `t`.

    Wadell, *Transmission Line Design Handbook*, 1991, section 4.2.1 (Cohn 1954 with the
    thickness correction): `m = 6 (1 - x)/(3 - x)`, `x = t/b`. As `t -> 0` it reproduces the
    exact conformal map `(30 pi/sqrt(er)) K(k')/K(k)`, `k = tanh(pi w/2b)`, to 0.05 %.
    """
    if w <= 0 or b <= 0 or t < 0 or t >= b:
        raise ValueError(f"stripline needs w > 0, 0 <= t < b; got w={w}, b={b}, t={t}")
    x = t / b
    m = 6.0 * (1.0 - x) / (3.0 - x)
    dw = (b - t) * (x / (math.pi * (1.0 - x))) * (1.0 - 0.5 * math.log((x / (2.0 - x)) ** 2 + (0.0796 * x / (w / b + 1.1 * x)) ** m))
    wp = w + dw
    a = (8.0 / math.pi) * (b - t) / wp
    return (30.0 / math.sqrt(er)) * math.log(1.0 + (4.0 / math.pi) * ((b - t) / wp) * (a + math.sqrt(a * a + 6.27)))


def stripline_asym(w: float, h1: float, h2: float, t: float, er1: float, er2: float) -> float:
    """Z0 of a stripline `h1` under one plane and `h2` over the other: the parallel combination
    `2 Z(2 h1 + t) Z(2 h2 + t) / (Z(2 h1 + t) + Z(2 h2 + t))` with the thickness-weighted Dk
    (Wadell 4.2.2; an approximation, printed +-5 %; JLC publishes no inner-layer rows)."""
    er = (er1 * h1 + er2 * h2) / (h1 + h2)
    za = stripline(w, 2.0 * h1 + t, t, er)
    zb = stripline(w, 2.0 * h2 + t, t, er)
    return 2.0 * za * zb / (za + zb)


def ipc2141_stripline(w: float, b: float, t: float, er: float) -> float:
    """IPC-2141's closed form, kept for the cross-check column only."""
    return (60.0 / math.sqrt(er)) * math.log(1.9 * b / (0.8 * w + t))


def exact_stripline(w: float, b: float, er: float) -> float:
    """The zero-thickness conformal map `(30 pi/sqrt(er)) K(k')/K(k)`, `k = tanh(pi w/2b)`."""
    k = math.tanh(math.pi * w / (2.0 * b))
    kp = math.sqrt(1.0 - k * k)
    return (30.0 * math.pi / math.sqrt(er)) * elliptic_k(kp) / elliptic_k(k)


# ---------------------------------------------------------------------------------------------
# C.5 Coplanar waveguide with ground: Ghione and Naldi 1987, t = 0
# ---------------------------------------------------------------------------------------------


def elliptic_k(k: float) -> float:
    """The complete elliptic integral of the first kind K(k) by the arithmetic-geometric mean."""
    if not 0.0 <= k < 1.0:
        raise ValueError(f"elliptic_k needs 0 <= k < 1; got {k}")
    a, b = 1.0, math.sqrt(1.0 - k * k)
    for _ in range(40):
        a, b = 0.5 * (a + b), math.sqrt(a * b)
        if abs(a - b) < 1e-16:
            break
    return math.pi / (2.0 * a)


def _k_ratio(k: float) -> float:
    return elliptic_k(k) / elliptic_k(math.sqrt(1.0 - k * k))


def cpwg(w: float, s: float, h: float, er: float) -> tuple[float, float]:
    """(Z0, eeff) of a conductor-backed coplanar waveguide: track `w`, gap `s` to the flanking
    pour, dielectric `h` to the ground plane. Ghione and Naldi 1987 eqs. 6-7; KiCad 10
    `coplanar.cpp` (`backMetal`). Thickness is not applied (t = 0). Validity `s/h <= 1`."""
    if w <= 0 or s <= 0 or h <= 0:
        raise ValueError(f"cpwg needs w, s, h > 0; got w={w}, s={s}, h={h}")
    k1 = w / (w + 2.0 * s)
    k3 = math.tanh(math.pi * w / (4.0 * h)) / math.tanh(math.pi * (w + 2.0 * s) / (4.0 * h))
    if k3 >= 1.0 - 1e-12:
        # Both tanh arguments saturate (w + 2s >= about 24 h): the flanking pour is electrically
        # far in units of h and the plane below dominates, so the structure IS a microstrip.
        # Left to K(k3) this raised ValueError, and the bisection in `solve_width` probes
        # 3.04 mm on its first step, so every CPWG width solve on a thin prepreg crashed.
        return microstrip(w, h, 0.0, er)
    q1 = _k_ratio(k1)
    q3 = _k_ratio(k3)
    eeff = 1.0 + q3 * (er - 1.0) / (q1 + q3)
    z0 = (ETA0 / 2.0) / ((q1 + q3) * math.sqrt(eeff))
    return z0, eeff


# ---------------------------------------------------------------------------------------------
# Stackup-aware impedance: bias, layers, solvers
# ---------------------------------------------------------------------------------------------


def solve_width(z_of_w, target: float, stack: Stackup, lo: float | None = None, hi: float = SOLVE_HI_MM) -> float:
    """The width at which the falling function `z_of_w` meets `target`: bisection, 60 iterations,
    from `stack.track_min` to 6.0 mm, rounded to 4 dp. Deterministic on every machine."""
    lo = stack.track_min if lo is None else lo
    for _ in range(SOLVE_ITERATIONS):
        mid = 0.5 * (lo + hi)
        if z_of_w(mid) > target:
            lo = mid
        else:
            hi = mid
    return round(0.5 * (lo + hi), 4)


def _inner_heights(stack: Stackup, layer: str) -> tuple[float, float, float, float]:
    """(h_above, h_below, er_above, er_below) for a track on an inner layer."""
    above = stack.plane_above(layer)
    below = stack.plane_below(layer)
    if above is None or below is None:
        raise ValueError(f"{layer!r} is not an inner layer of {stack.name!r}")
    h1, er1 = stack.dielectric_between(above, layer)
    h2, er2 = stack.dielectric_between(layer, below)
    return h1, h2, er1, er2


def bare_z0(w_mm: float, stack: Stackup, layer: str = "F.Cu", pour_gap: float | None = None) -> float:
    """The formula's own Z0 at `w_mm` on `layer`, before the fab bias: microstrip on an outer
    layer (CPWG when a pour flanks it at `pour_gap`; `min(cpwg, microstrip)` past `s/h = 1`),
    asymmetric stripline on an inner one."""
    if is_outer(layer):
        h, t, er = stack.h_mm, stack.copper_t(layer), stack.er
        if layer == "B.Cu":
            h, er = stack.dielectric_between("B.Cu", stack.plane_above("B.Cu") or "F.Cu")
        z_ms = microstrip(w_mm, h, t, er)[0]
        if pour_gap is None:
            return z_ms
        z_cp = cpwg(w_mm, pour_gap, h, er)[0]
        return z_cp if pour_gap / h <= 1.0 else min(z_cp, z_ms)
    h1, h2, er1, er2 = _inner_heights(stack, layer)
    return stripline_asym(w_mm, h1, h2, stack.copper_t(layer), er1, er2)


def microstrip_z0(w_mm: float, stack: Stackup, layer: str = "F.Cu", pour_gap: float | None = None) -> float:
    """The single-ended impedance pcbc reports for a track of `w_mm` on `layer`: the formula
    (section C.1, C.4 or C.5 by layer and pour) times the stackup's `z_bias_se` on outer
    layers; inner layers are formula only (no JLC row to fit to). 2 dp."""
    z = bare_z0(w_mm, stack, layer, pour_gap)
    if is_outer(layer):
        z *= stack.z_bias_se
    return round(z, 2)


def width_for_z0(
    z0: float, stack: Stackup, wmin: float | None = None, wmax: float = SOLVE_HI_MM, layer: str = "F.Cu", pour_gap: float | None = None
) -> float:
    """The width that lands `z0` on `layer`: solves the bare formula for `z0 / z_bias_se` on an
    outer layer (`z0` itself on an inner one), 4 dp."""
    target = z0 / stack.z_bias_se if is_outer(layer) else z0
    return solve_width(lambda w: bare_z0(w, stack, layer, pour_gap), target, stack, lo=wmin, hi=wmax)


def pair_gap_mm(stack: Stackup, gap: float | None = None) -> float:
    """JLC's own pair gap on 4L, never under the fab's clearance: `max(0.15, clearance_min)`."""
    return max(0.15, stack.clearance_min) if gap is None else max(gap, stack.clearance_min)


def _pair_h_t_er(stack: Stackup, layer: str) -> tuple[float, float, float]:
    if not is_outer(layer):
        raise ValueError(f"R1 sizes coupled pairs on outer layers only; {layer!r} is inner on {stack.name!r}")
    if layer == "B.Cu":
        h, er = stack.dielectric_between("B.Cu", stack.plane_above("B.Cu") or "F.Cu")
    else:
        h, er = stack.h_mm, stack.er
    return h, stack.copper_t(layer), er


def diff_pair_width_mm(z_diff: float, stack: Stackup, layer: str = "F.Cu", gap: float | None = None) -> float:
    """The member width the coupled formula (times `z_bias_diff`) needs for `z_diff` at the
    pair gap, before the pair-fit clamp. 4 dp."""
    h, t, er = _pair_h_t_er(stack, layer)
    s = pair_gap_mm(stack, gap)
    return solve_width(lambda w: zdiff(w, s, h, t, er), z_diff / stack.z_bias_diff, stack)


def diff_pair_z(w: float, s: float, stack: Stackup, layer: str = "F.Cu") -> float:
    """The differential impedance pcbc reports at `(w, s)` on `layer`: `z_bias_diff * 2 Zo`, 2 dp."""
    h, t, er = _pair_h_t_er(stack, layer)
    return round(stack.z_bias_diff * zdiff(w, s, h, t, er), 2)


def diff_pair_geometry(z_diff: float, stack: Stackup, layer: str = "F.Cu", gap: float | None = None) -> tuple[float, float]:
    """(width, gap) of an edge-coupled pair for `z_diff` on `layer`.

    Gap `max(0.15, clearance_min)` unless given; width solved for `z_diff / z_bias_diff` by
    C.3. The pair-fit clamp: a member wider than `PAIR_FIT_MM` (0.25) cannot leave a USB-C's
    pads, so the pair is written at the fab floor `(track_min, clearance_min)` and the caller
    reports it as uncontrolled (`diff_pair_width_mm` still says what it would have needed).
    """
    w = diff_pair_width_mm(z_diff, stack, layer, gap)
    if w > PAIR_FIT_MM:
        return stack.track_min, stack.clearance_min
    return max(w, stack.track_min), pair_gap_mm(stack, gap)


# ---------------------------------------------------------------------------------------------
# C.6 Current: IPC-2221 external (the floor) and IPC-2152 with the board and plane modifiers
# ---------------------------------------------------------------------------------------------


def ipc2221_width_mm(
    amps: float,
    temp_rise_c: float = 10.0,
    copper_oz: float = 1.0,
    outer: bool = True,
) -> float:
    """IPC-2221 chart fit. Area in mil², 1 oz ≈ 1.378 mil."""
    if amps <= 0:
        return 0.2
    k = 0.048 if outer else 0.024
    dt = max(temp_rise_c, 1.0)
    area = (amps / (k * (dt**0.44))) ** (1 / 0.725)
    thick_mil = OZ_MIL * copper_oz
    width_mil = area / thick_mil
    return round(max(0.15, width_mil * 0.0254), 3)


IPC2152_MIN_AMPS = 0.274  # the universal-chart fit's range (C.6): 0.274 to 26 A,
IPC2152_MAX_AMPS = 26.0  # on boards 0.72 to 2.36 mm thick. Outside it the fit extrapolates and
IPC2152_MIN_BOARD_MM = 0.72  # says so on the Derived, so a number off the chart is never silent.
IPC2152_MAX_BOARD_MM = 2.36


def _num_g(v: float) -> str:
    return f"{v:g}"
IPC2152_PLANE_H_MIN_MM = 0.144  # Fig 5-11's digitised range starts here


def ipc2152_area_mil2(amps: float, temp_rise_c: float) -> float:
    """IPC-2152 universal chart (Fig 5-1) fit: `(I / (0.0897 dT^0.394))^(1 / (0.5038 dT^0.0385))`
    mil2, coefficients as digitised by mbedded.ninja."""
    dt = max(temp_rise_c, 1.0)
    return (amps / (0.0897 * dt**0.394)) ** (1.0 / (0.5038 * dt**0.0385))


def ipc2152_board_modifier(board_mm: float) -> float:
    """Fig 5-8 board-thickness modifier: `24.93 * (board_mil)^-0.755`; 1.6 mm -> 1.092."""
    return 24.93 * (board_mm / 0.0254) ** -0.755


def ipc2152_plane_modifier(plane_h_mm: float | None) -> float:
    """Fig 5-11 plane-proximity modifier: `0.00313 * plane_mil + 0.4045` at `plane_h >= 0.144`
    (clamped to the fit's floor below it); 1.0 with no plane."""
    if plane_h_mm is None:
        return 1.0
    return 0.00313 * (max(plane_h_mm, IPC2152_PLANE_H_MIN_MM) / 0.0254) + 0.4045


def ipc2152_width_mm(amps: float, temp_rise_c: float, copper_oz: float, board_mm: float, plane_h_mm: float | None) -> float:
    """IPC-2152 width (mm, 3 dp): the chart fit times the board and plane modifiers."""
    if amps <= 0:
        return 0.0
    area = ipc2152_area_mil2(amps, temp_rise_c) * ipc2152_board_modifier(board_mm) * ipc2152_plane_modifier(plane_h_mm)
    return round(area / (OZ_MIL * copper_oz) * 0.0254, 3)


def current_width_mm(amps: float, temp_rise_c: float, stack: Stackup, plane_h_mm: float | None) -> Derived:
    """`max(IPC-2221 external, IPC-2152 with modifiers)`; the source names the winner, the note
    the loser. Decision H.1: the 2221 curve stays the floor in R1."""
    w2221 = ipc2221_width_mm(amps, temp_rise_c, stack.copper_oz)
    w2152 = ipc2152_width_mm(amps, temp_rise_c, stack.copper_oz, stack.board_mm, plane_h_mm)
    oz = f"{stack.copper_oz:g} oz"
    plane = f"plane {ipc2152_plane_modifier(plane_h_mm):.3f} at {plane_h_mm:g} mm" if plane_h_mm is not None else "no plane"
    fit = f"ipc2152_fit x board {ipc2152_board_modifier(stack.board_mm):.3f} x {plane}"
    outside = []
    if 0 < amps < IPC2152_MIN_AMPS:
        outside.append(f"below {_num_g(IPC2152_MIN_AMPS)} A")
    elif amps > IPC2152_MAX_AMPS:
        outside.append(f"above {_num_g(IPC2152_MAX_AMPS)} A")
    board_mm = stack.board_mm
    if board_mm and not IPC2152_MIN_BOARD_MM <= board_mm <= IPC2152_MAX_BOARD_MM:
        outside.append(f"on a {_num_g(board_mm)} mm board (the chart covers {_num_g(IPC2152_MIN_BOARD_MM)} to {_num_g(IPC2152_MAX_BOARD_MM)})")
    extrapolated = f"; {' and '.join(outside)} the IPC-2152 fit extrapolates" if outside else ""
    if w2221 >= w2152:
        return Derived(
            w2221,
            "mm",
            Source("ipc2221_ext", f"IPC-2221B eq. 6-2 external {amps:g} A {temp_rise_c:g} C {oz}", f"IPC-2221 floor holds over {fit} {w2152:.3f}{extrapolated}"),
        )
    return Derived(
        w2152,
        "mm",
        Source("ipc2152_fit", f"IPC-2152 Fig 5-1, 5-8, 5-11 {amps:g} A {temp_rise_c:g} C {oz} {plane}", f"over the IPC-2221 external floor {w2221:.3f}{extrapolated}"),
    )


# ---------------------------------------------------------------------------------------------
# C.7 Vias per amp: IPC-2221 internal curve on the plated barrel
# ---------------------------------------------------------------------------------------------


def via_barrel_mil2(drill_mm: float, plating_mm: float) -> float:
    return math.pi * drill_mm * plating_mm * MIL2_PER_MM2


def via_amps(drill_mm: float, plating_mm: float = 0.018, temp_rise_c: float = 10.0) -> float:
    """One via's current at `temp_rise_c`: IPC-2221B eq. 6-2 internal on the barrel area
    (Brooks and Adam 2021 ch. 9; Saturn PCB Toolkit). 0.3 mm at 18 um: 0.707 A at 10 C. 3 dp."""
    dt = max(temp_rise_c, 1.0)
    return round(0.024 * dt**0.44 * via_barrel_mil2(drill_mm, plating_mm) ** 0.725, 3)


def vias_per_change(amps: float, drill_mm: float, plating_mm: float = 0.018, temp_rise_c: float = 10.0) -> int:
    """`max(1, ceil(amps / via_amps))` vias per layer change (a report line in R1, not a gate)."""
    if amps <= 0:
        return 1
    return max(1, math.ceil(amps / via_amps(drill_mm, plating_mm, temp_rise_c) - 1e-9))


# ---------------------------------------------------------------------------------------------
# C.8 Voltage: IPC-2221B Table 6-1 clearance, IEC 60664-1 creepage and impulse clearance
# ---------------------------------------------------------------------------------------------

# IPC-2221B Table 6-1 (2012), as KiCad's panel_electrical_spacing_ipc2221.cpp encodes it:
# (upper V peak, B1 internal, B2 external uncoated <= 3050 m, B4 external permanent polymer coating).
IPC2221_TABLE_6_1: tuple[tuple[float, float, float, float], ...] = (
    (15.0, 0.05, 0.1, 0.05),
    (30.0, 0.05, 0.1, 0.05),
    (50.0, 0.1, 0.6, 0.13),
    (100.0, 0.1, 0.6, 0.13),
    (150.0, 0.2, 0.6, 0.4),
    (170.0, 0.2, 1.25, 0.4),
    (250.0, 0.2, 1.25, 0.4),
    (300.0, 0.2, 1.25, 0.4),
    (500.0, 0.25, 2.5, 0.8),
)
IPC2221_ABOVE_500_PER_V: dict[str, float] = {"B1": 0.0025, "B2": 0.005, "B4": 0.00305}
_IPC2221_COLUMN: dict[str, int] = {"B1": 1, "B2": 2, "B4": 3}


def ipc2221_clearance_mm(volts: float, row: str) -> float:
    """IPC-2221B Table 6-1 clearance for `volts` (peak) in row B1 (internal), B2 (external,
    uncoated) or B4 (external, permanent polymer coating); above 500 V the 301-500 value plus
    the per-volt increment times `(V - 500)`. 4 dp."""
    if row not in _IPC2221_COLUMN:
        raise ValueError(f"IPC-2221 row must be B1, B2 or B4; got {row!r}")
    col = _IPC2221_COLUMN[row]
    for upper, *cols in IPC2221_TABLE_6_1:
        if volts <= upper:
            return round(cols[col - 1], 4)
    return round(IPC2221_TABLE_6_1[-1][col] + IPC2221_ABOVE_500_PER_V[row] * (volts - 500.0), 4)


# IEC 60664-1:2020 Table F.5 (F.4 in 2007), pollution degree 2, general columns:
# (V rms, group I, group II, group IIIa / IIIb). IEC 62368-1:2018 Table 17 carries the same rows.
IEC60664_TABLE_F5_PD2: tuple[tuple[float, float, float, float], ...] = (
    (50.0, 0.60, 0.85, 1.20),
    (63.0, 0.63, 0.90, 1.25),
    (80.0, 0.67, 0.95, 1.30),
    (100.0, 0.71, 1.00, 1.40),
    (125.0, 0.75, 1.05, 1.50),
    (160.0, 0.80, 1.10, 1.60),
    (200.0, 1.00, 1.40, 2.00),
    (250.0, 1.25, 1.80, 2.50),
    (320.0, 1.60, 2.20, 3.20),
    (400.0, 2.00, 2.80, 4.00),
    (500.0, 2.50, 3.60, 5.00),
    (630.0, 3.20, 4.50, 6.30),
    (800.0, 4.00, 5.60, 8.00),
    (1000.0, 5.00, 7.10, 10.0),
)
_MATERIAL_COLUMN: dict[str, int] = {"I": 1, "II": 2, "IIIa": 3, "IIIb": 3}


def iec_creepage_mm(volts_rms: float, material_group: str = "IIIa", pollution_degree: int = 2, reinforced: bool = False) -> float:
    """IEC 60664-1 Table F.5 creepage at PD2, looked up at the next row voltage >= `volts_rms`;
    reinforced doubles. FR-4 is group IIIa. 4 dp.

    The table as encoded starts at 50 V, so anything below it reads the 50 V row (1.2 mm on
    IIIa). That is conservative, never permissive; the standard's rows below 50 V are smaller
    (KiCad's `iec60664.cpp` carries them) and are not encoded here because no pcbc path reaches
    them: an automatic creepage rule is written only at `CREEPAGE_FROM_V` (60 V) and above."""
    if pollution_degree != 2:
        raise ValueError(f"R1 encodes IEC 60664-1 Table F.5 at pollution degree 2 only; got {pollution_degree}")
    if material_group not in _MATERIAL_COLUMN:
        raise ValueError(f"material group must be I, II, IIIa or IIIb; got {material_group!r}")
    col = _MATERIAL_COLUMN[material_group]
    for row in IEC60664_TABLE_F5_PD2:
        if volts_rms <= row[0]:
            return round(row[col] * (2.0 if reinforced else 1.0), 4)
    raise ValueError(f"IEC 60664-1 Table F.5 as encoded stops at 1000 V rms; got {volts_rms}")


# IEC 60664-1 Table F.1, overvoltage category II: (V rms up to, rated impulse V).
IEC60664_TABLE_F1_OV2: tuple[tuple[float, float], ...] = (
    (50.0, 500.0),
    (100.0, 800.0),
    (150.0, 1500.0),
    (300.0, 2500.0),
    (600.0, 4000.0),
    (1000.0, 6000.0),
)
# IEC 60664-1 Table F.2, inhomogeneous field, pollution degree 2: (impulse kV, clearance mm).
IEC60664_TABLE_F2_PD2: tuple[tuple[float, float], ...] = (
    (1.0, 0.2),
    (1.2, 0.25),
    (1.5, 0.5),
    (2.0, 1.0),
    (2.5, 1.5),
    (3.0, 2.0),
    (4.0, 3.0),
    (5.0, 4.0),
    (6.0, 5.5),
    (8.0, 8.0),
    (10.0, 11.0),
)
IEC60664_PD2_MIN_MM = 0.2


def iec_impulse_v(volts_rms: float, overvoltage_category: int = 2, reinforced: bool = False) -> float:
    """Rated impulse voltage from Table F.1 (OV II), times 1.6 when reinforced (IEC 62368-1
    practice, as KiCad's iec60664.cpp)."""
    if overvoltage_category != 2:
        raise ValueError(f"R1 encodes IEC 60664-1 Table F.1 at overvoltage category II only; got {overvoltage_category}")
    for upper, impulse in IEC60664_TABLE_F1_OV2:
        if volts_rms <= upper:
            return impulse * (1.6 if reinforced else 1.0)
    raise ValueError(f"IEC 60664-1 Table F.1 as encoded stops at 1000 V rms; got {volts_rms}")


def iec_clearance_mm(volts_rms: float, overvoltage_category: int = 2, reinforced: bool = False) -> float:
    """IEC 60664-1 impulse clearance: Table F.1 rated impulse (OV II, x1.6 reinforced) into
    Table F.2 (inhomogeneous field, PD2, minimum 0.2 mm) at the next impulse >= the rating. 4 dp."""
    kv = iec_impulse_v(volts_rms, overvoltage_category, reinforced) / 1000.0
    for upper, mm in IEC60664_TABLE_F2_PD2:
        if kv <= upper:
            return round(max(mm, IEC60664_PD2_MIN_MM), 4)
    raise ValueError(f"IEC 60664-1 Table F.2 as encoded stops at 10 kV impulse; got {kv} kV")


# ---------------------------------------------------------------------------------------------
# C.9 I2C length from capacitance
# ---------------------------------------------------------------------------------------------


def capacitance_pf_per_mm(z0: float, eeff: float, *, exact: bool = False) -> float:
    """Line capacitance `sqrt(eeff) / (c z0)`, 4 dp pF/mm (`exact=True` keeps full precision).

    `i2c_max_mm` divides a budget by this, so rounding first moved the answer by 0.1 %; the
    length is computed from the exact value and only the printed pF/mm is rounded."""
    c = math.sqrt(eeff) / (C_MM_PER_NS * z0) * 1000.0
    return c if exact else round(c, 4)


def i2c_max_mm(pf_max: float, pins: int, c_pf_per_mm: float) -> float:
    """UM10204 rev 7 section 7.1: `Cb <= pf_max` with 10 pF per device pin; the rest is trace. 4 dp."""
    return round((pf_max - 10.0 * pins) / c_pf_per_mm, 4)
