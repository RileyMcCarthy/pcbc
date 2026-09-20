"""KiCad custom rules (.kicad_dru) and project net classes from the ConstraintSet.

docs/r1-design.md section E. `rules(cs)` is pure and computed once at compile (every rule area
R1 writes is known before placement); `render` is the file; `validate` runs before any write,
because one malformed rule silently disables every rule in KiCad 10 and kicad-cli says nothing
(the canary rule, written last, is the gate's check that the file parsed).

KiCad applies the **last** matching rule of a constraint type, so the file is ordered general to
specific: geometry, class widths, keep-aways, creepage classes, isolation, per-net rules, skew,
pairs, areas, the footprint exemption, the canary.

Conditions: `A.hasNetclass('X')` for classes, `A.NetName == 'X'` for nets, `||`-lists of names
for groups, `A.intersectsArea('X')` for areas (each verified in docs/router-plan.md section 2 or
by tests/test_dru.py's probes). No membership-only netclasses.
"""

from __future__ import annotations

import re

from .constraints import ConstraintSet, DruRule, slug

# KiCad 10 constraint types pcbc writes; anything else is refused before the write.
KNOWN_CONSTRAINTS = frozenset(
    {
        "clearance",
        "creepage",
        "length",
        "skew",
        "via_count",
        "track_width",
        "track_segment_length",
        "track_angle",
        "diff_pair_gap",
        "diff_pair_uncoupled",
        "disallow",
    }
)
# Constraint types whose (min|max|opt) values are lengths and must carry `mm`.
_LENGTH_CONSTRAINTS = KNOWN_CONSTRAINTS - {"track_angle", "via_count", "disallow"}
_SEVERITIES = ("error", "warning", "ignore")

# The class order of today's .kicad_pro files; classes not listed follow in first-seen order.
PROJECT_CLASS_ORDER = ("Default", "Analog", "Clock", "Power", "USB", "SwitchNode")

# A footprint's own pads are the land, not a routing choice: every clearance rule between nets
# carries this exemption and the `pads_of_one_footprint` rule (E.16) repeats it after them.
NOT_OWN_PADS = "!(A.Type == 'Pad' && B.Type == 'Pad' && A.Reference == B.Reference)"
OWN_PADS = "A.Type == 'Pad' && B.Type == 'Pad' && A.Reference == B.Reference"

# Rule names by kind, for the gate's soft counts (E: track_width, skew, via_budget, diff_pair_uncoupled).
SOFT_PREFIXES = {"width_": "track_width", "skew_": "skew", "vias_": "via_budget", "uncoupled_": "diff_pair_uncoupled"}


def _mm(v: float) -> str:
    """`0.6mm`, `1.25mm`, `0.001mm` (E: `f"{round(v, 4):g}mm"`)."""
    return f"{round(v, 4):g}mm"


def _names(prefix: str, nets: tuple[str, ...] | list[str]) -> str:
    return " || ".join(f"{prefix}.NetName == '{n}'" for n in nets)


def rules(cs: ConstraintSet) -> list[DruRule]:
    """Pure, deterministic; computed once in compile_design."""
    stack = cs.stackup
    by_class: dict[str, list] = {}
    for c in cs.constraints:
        by_class.setdefault(c.class_name, []).append(c)

    out: list[DruRule] = [
        # E.1, E.2: geometry KiCad can count: a grid router's staircases and 90 degree corners.
        # Warnings, so they reach the copper bar without failing a legal board; the bar decides.
        DruRule("pcbc_geometry_segments", "(constraint track_segment_length (min 0.2mm))", "A.Type == 'Track'", "warning"),
        # No unit on the angle: "(min 135deg)" makes KiCad 10 drop the whole rule file, silently.
        DruRule("pcbc_geometry_angles", "(constraint track_angle (min 135))", "A.Type == 'Track'", "warning"),
    ]

    # E.3: class widths, soft (H.3): fanout stubs on closed rows are track_min wide by design and
    # legal necks have no rule area until R2. Written for the classes whose constraints carry the
    # soft `track_width` kind (power, pairs, z_se_ohm classes), never for the floor classes.
    for cls in cs.classes:
        cons = by_class.get(cls.name, [])
        if cls.track_width_mm > stack.track_min and any("track_width" in c.soft for c in cons):
            out.append(DruRule(f"width_{slug(cls.name)}", f"(constraint track_width (min {_mm(cls.track_width_mm)}))", f"A.hasNetclass('{cls.name}')", "warning"))

    # E.4: keep-aways, one per (class, other net); the exemption keeps an IC's own FB/SW pins out.
    for cls in cs.classes:
        aways: dict[str, float] = {}
        for c in by_class.get(cls.name, []):
            for k in c.keep_away:
                aways.setdefault(k.other, k.mm)
        for other in sorted(aways):
            out.append(
                DruRule(
                    f"{slug(cls.name)}_away_from_{slug(other)}",
                    f"(constraint clearance (min {_mm(aways[other])}))",
                    f"A.hasNetclass('{cls.name}') && B.NetName == '{other}' && {NOT_OWN_PADS}",
                )
            )

    # E.6: creepage by volts (classes at >= CREEPAGE_FROM_V); the class row carries the clearance (E.5).
    # `B.NetName != ''` keeps the rule off items with no net: pcbc's own fiducial-mask rule areas
    # were reported at 0.05 mm ("Rule area 'FID3_mask'"), and `B.Type != 'Rule Area'` does not
    # exclude them. KiCad resolves creepage per NET PAIR, so a part whose two pads span this class
    # and another net (a divider resistor off a 250 V rail) is reported at its own pad pitch and no
    # A.Reference exemption can silence it: that is a real finding about the footprint's rating.
    for cls in cs.classes:
        creep = [c.voltage.creepage_mm.value for c in by_class.get(cls.name, []) if c.voltage is not None and c.voltage.creepage_mm is not None]
        if creep:
            out.append(
                DruRule(
                    f"creepage_{slug(cls.name)}",
                    f"(constraint creepage (min {_mm(max(creep))}))",
                    f"A.hasNetclass('{cls.name}') && !B.hasNetclass('{cls.name}') && B.NetName != ''",
                )
            )

    # E.13, E.14: isolation clearance and creepage between the two sides' nets (C.8 numbers).
    for spec in cs.isolation_specs:
        if not spec.nets_a or not spec.nets_b:
            continue
        a, b = spec.req.a, spec.req.b
        cond = f"({_names('A', spec.nets_a)}) && ({_names('B', spec.nets_b)}) && {NOT_OWN_PADS}"
        out.append(DruRule(f"iso_{slug(a)}_{slug(b)}_clearance", f"(constraint clearance (min {_mm(spec.clearance_mm.value)}))", cond))
        if not spec.req.slot:  # with slot=True the contour through the slot satisfies the creepage (C.8)
            # KiCad resolves creepage per NET PAIR, not per item: `!(A.Type == 'Pad' && ...
            # A.Reference == B.Reference)` never suppresses anything (measured: the optocoupler's
            # own two pads at 0.85 mm were still reported, so every non-slot Isolation failed the
            # gate on the isolator itself). The nets an `across=` part carries are left out of the
            # condition instead: the barrier inside that part is its own rating, as E.13 says.
            across = set(spec.across_nets)
            creep_a = tuple(n for n in spec.nets_a if n not in across)
            creep_b = tuple(n for n in spec.nets_b if n not in across)
            if creep_a and creep_b:
                out.append(
                    DruRule(
                        f"iso_{slug(a)}_{slug(b)}_creepage",
                        f"(constraint creepage (min {_mm(spec.creepage_mm.value)}))",
                        f"({_names('A', creep_a)}) && ({_names('B', creep_b)})",
                    )
                )

    # E.7: routed length, one rule per net (from length_mm=, i2c, or Bus(length_mm=)); never from max_mm.
    for c in cs.constraints:
        if c.length_max_mm is not None:
            out.append(DruRule(f"length_{slug(c.net)}", f"(constraint length (max {_mm(c.length_max_mm.value)}))", f"A.NetName == '{c.net}'"))
    # E.9: no vias (per net: the verified form); E.10: the via budget, soft.
    for c in cs.constraints:
        if not c.via.allowed:
            out.append(DruRule(f"novia_{slug(c.net)}", "(constraint via_count (max 0))", f"A.NetName == '{c.net}'"))
    for c in cs.constraints:
        if c.via.allowed and c.via.count_max is not None:
            out.append(DruRule(f"vias_{slug(c.net)}", f"(constraint via_count (max {c.via.count_max}))", f"A.NetName == '{c.net}'", "warning"))

    # E.8: skew, one per pair and per bus (members sorted; a pair and a bus over the same nets share
    # the tighter budget), soft.
    skews: dict[tuple[str, ...], float] = {}
    for c in cs.constraints:
        if c.pair is not None and c.pair.partner:
            key = tuple(sorted((c.net, c.pair.partner)))
            skews[key] = min(skews.get(key, c.pair.skew_mm.value), c.pair.skew_mm.value)
    for g in cs.groups:
        if g.kind == "bus" and g.match_mm is not None and len(g.members) > 1:
            key = tuple(sorted(g.members))
            skews[key] = min(skews.get(key, g.match_mm), g.match_mm)
    for members in sorted(skews):
        out.append(DruRule(f"skew_{'_'.join(slug(m) for m in members)}", f"(constraint skew (max {_mm(skews[members])}))", _names("A", members), "warning"))

    # E.11, E.12: pair gap (error; today's .2f) and uncoupled length (soft), per pair class.
    for cls in cs.classes:
        if cls.diff_pair_gap_mm is None:
            continue
        g = cls.diff_pair_gap_mm
        out.append(DruRule(f"{slug(cls.name)}_pair_gap", f"(constraint diff_pair_gap (min {_mm(max(0.1, round(g - 0.03, 4)))}) (opt {_mm(g)}))", f"A.hasNetclass('{cls.name}')"))
        uncoupled = [c.pair.uncoupled_mm.value for c in by_class.get(cls.name, []) if c.pair is not None]
        if uncoupled:
            out.append(DruRule(f"uncoupled_{slug(cls.name)}", f"(constraint diff_pair_uncoupled (max {_mm(max(uncoupled))}))", f"A.hasNetclass('{cls.name}')", "warning"))

    # E.15: the isolation corridor: a rule area (apply._apply_rule_areas writes the zone) nothing routes through.
    for area in cs.rule_areas:
        out.append(DruRule(f"{slug(area.name)}_area", f"(constraint disallow {' '.join(area.disallow)})", f"A.intersectsArea('{area.name}')"))

    # E.16: a connector's own pads sit closer than a power class asks (USB-C: 0.1 mm); that is the
    # land, not a routing choice. After every clearance rule so it wins.
    out.append(DruRule("pads_of_one_footprint", "(constraint clearance (min 0.1mm))", OWN_PADS))
    # E.17: the canary. One malformed rule silently disables every rule and kicad-cli says nothing.
    # This fires once on every board that has copper; the gate fails when it does not.
    if cs.canary_net:
        out.append(DruRule("pcbc_canary", "(constraint length (max 0.001mm))", f"A.NetName == '{cs.canary_net}'", "warning"))
    return out


def render(rules: list[DruRule]) -> str:
    """The .kicad_dru text (today's apply._render_dru, moved)."""
    lines = [
        "(version 1)\n",
        "# Generated by pcbc. Do not hand-edit.\n",
    ]
    for rule in rules:
        sev = f"\t(severity {rule.severity})\n" if rule.severity != "error" else ""
        lines.append(
            f'\n(rule "{rule.name}"\n'
            f"{sev}"
            f"\t{rule.constraint}\n"
            f'\t(condition "{rule.condition}"))\n'
        )
    return "".join(lines)


_CONSTRAINT_HEAD = re.compile(r"^\(constraint ([a-z_]+)")
_VALUES = re.compile(r"\((min|max|opt) ([^()]*)\)")
_ANGLE_UNIT = re.compile(r"track_angle \(min [0-9.]+[A-Za-z]")


def _numbers(constraint: str) -> list[tuple[str, float]]:
    out: list[tuple[str, float]] = []
    for which, value in _VALUES.findall(constraint):
        try:
            out.append((which, float(value.strip().rstrip("mm").strip() or "0")))
        except ValueError:
            continue
    return out


def validate(rules: list[DruRule]) -> list[str]:
    """What would make KiCad drop the whole file, before any write; a non-empty list raises ValueError
    in `apply.write_dru`. Refuses a constraint name KiCad 10 does not know, a unit on `track_angle`
    (`(min 135deg)` silently disables every rule), a length value without `mm`, an empty condition,
    an unknown severity, and two rules with one name."""
    errs: list[str] = []
    seen: dict[str, int] = {}
    for r in rules:
        seen[r.name] = seen.get(r.name, 0) + 1
        m = _CONSTRAINT_HEAD.match(r.constraint)
        if m is None:
            errs.append(f"rule {r.name!r}: {r.constraint!r} is not a (constraint ...) form")
            continue
        kind = m.group(1)
        if kind not in KNOWN_CONSTRAINTS:
            errs.append(f"rule {r.name!r}: unknown constraint {kind!r}; KiCad 10 checks {', '.join(sorted(KNOWN_CONSTRAINTS))}")
        if kind == "track_angle" and _ANGLE_UNIT.search(r.constraint):
            errs.append(f"rule {r.name!r}: track_angle takes a bare number; a unit ((min 135deg)) silently disables every rule in KiCad 10.0.6")
        if kind in _LENGTH_CONSTRAINTS:
            for which, value in _VALUES.findall(r.constraint):
                if not re.fullmatch(r"[0-9.]+mm", value.strip()):
                    errs.append(f"rule {r.name!r}: {kind} ({which} {value}) needs a length in mm")
        if not r.condition.strip():
            errs.append(f"rule {r.name!r}: empty condition")
        # A name with a quote closes KiCad's string early and the whole file is dropped in
        # silence: `A.NetName == 'it's'`. `check` refuses such names, and this is the backstop.
        if r.condition.count("'") % 2:
            errs.append(
                f"rule {r.name!r}: the condition {r.condition!r} has an unbalanced quote (a net or class name "
                "carries one); KiCad reads the string as ending there and drops every rule in the file"
            )
        if any(v < 0 for _which, v in _numbers(r.constraint)):
            errs.append(f"rule {r.name!r}: {r.constraint!r} carries a negative value; KiCad takes it and the rule can never be met")
        if r.severity not in _SEVERITIES:
            errs.append(f"rule {r.name!r}: severity {r.severity!r} is not one of error, warning, ignore")
    for name, n in seen.items():
        if n > 1:
            errs.append(f"rule {name!r} is written {n} times; KiCad keeps one and pcbc cannot say which")
    return errs


def _class_row(cls) -> dict:
    row = {
        "name": cls.name,
        "clearance": cls.clearance_mm,
        "track_width": cls.track_width_mm,
        "via_diameter": cls.via_diameter_mm,
        "via_drill": cls.via_drill_mm,
    }
    if cls.diff_pair_gap_mm is not None:
        row["diff_pair_gap"] = cls.diff_pair_gap_mm
        row["diff_pair_width"] = cls.diff_pair_width_mm
    return row


def project_classes(cs: ConstraintSet) -> list[dict]:
    """Rows for .kicad_pro net_settings.classes: one per CompiledClass, ordered Default, Analog,
    Clock, Power, USB, SwitchNode, then the rest in first-seen order (today's list). `seed.emit_pro`
    and `apply._apply_pro` both read this; nothing else writes a class number."""
    rows = {c.name: _class_row(c) for c in cs.classes}
    ordered = [rows[n] for n in PROJECT_CLASS_ORDER if n in rows]
    ordered += [rows[c.name] for c in cs.classes if c.name not in PROJECT_CLASS_ORDER]
    return ordered


def netclass_patterns(cs: ConstraintSet) -> list[dict]:
    """`[{"netclass": name, "pattern": p}, ...]` in class order (today's form)."""
    return [{"netclass": c.name, "pattern": p} for c in cs.classes for p in c.patterns]


def soft_kind(name: str) -> str | None:
    """The soft rule kind (SOFT_RULES) a pcbc rule name belongs to, or None for an error rule."""
    for prefix, kind in SOFT_PREFIXES.items():
        if name.startswith(prefix):
            return kind
    return None


__all__ = [
    "KNOWN_CONSTRAINTS",
    "NOT_OWN_PADS",
    "PROJECT_CLASS_ORDER",
    "netclass_patterns",
    "project_classes",
    "render",
    "rules",
    "soft_kind",
    "validate",
]
