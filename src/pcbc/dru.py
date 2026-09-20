"""KiCad custom rules (.kicad_dru) from the ConstraintSet.

R1 slice S2 stub: exactly today's rule list rebuilt from the `ConstraintSet` (the footprint's own
pads, the two geometry rules, the canary, then per class the keep-aways and the pair gap). S3 owns
this file from here on (docs/r1-design.md section E: `rules`, `render`, `validate`,
`project_classes`, `netclass_patterns`).
"""

from __future__ import annotations

from .constraints import ConstraintSet, DruRule, slug


def rules(cs: ConstraintSet) -> list[DruRule]:
    """Pure, deterministic; computed once in compile_design."""
    out: list[DruRule] = [
        # A connector's own pads sit closer than a power class asks (USB-C: 0.1 mm); that is the
        # land, not a routing choice. Inside one footprint only the fab floor applies.
        DruRule(
            name="pads_of_one_footprint",
            constraint="(constraint clearance (min 0.1mm))",
            condition="A.Type == 'Pad' && B.Type == 'Pad' && A.Reference == B.Reference",
        ),
        # Geometry KiCad can count: a grid router's staircases and 90 degree corners. Warnings, so
        # they reach the copper bar without failing a legal board; the bar decides.
        DruRule("pcbc_geometry_segments", "(constraint track_segment_length (min 0.2mm))", "A.Type == 'Track'", "warning"),
        # No unit on the angle: "(min 135deg)" makes KiCad 10 drop the whole rule file, silently.
        DruRule("pcbc_geometry_angles", "(constraint track_angle (min 135))", "A.Type == 'Track'", "warning"),
    ]
    # The canary: one malformed rule silently disables every rule and kicad-cli says nothing.
    # This fires once on every board that has copper; the gate fails when it does not.
    if cs.canary_net:
        out.append(DruRule("pcbc_canary", "(constraint length (max 0.001mm))", f"A.NetName == '{cs.canary_net}'", "warning"))
    for cls in cs.classes:
        aways: dict[str, float] = {}
        for c in cs.constraints:
            if c.class_name == cls.name:
                for k in c.keep_away:
                    aways.setdefault(k.other, k.mm)
        for other in sorted(aways):
            out.append(
                DruRule(
                    name=f"{cls.name.lower()}_away_from_{slug(other)}",
                    constraint=f"(constraint clearance (min {aways[other]:g}mm))",
                    condition=f"A.NetClass == '{cls.name}' && B.NetName == '{other}'",
                )
            )
        # max_mm is an airwire / cluster budget (pcbc check). Do not emit a KiCad length rule:
        # the maze path is longer than the airwire, and Analog nets with different max_mm share
        # one class.
        if cls.diff_pair_gap_mm is not None:
            g = cls.diff_pair_gap_mm
            out.append(
                DruRule(
                    name=f"{cls.name.lower()}_pair_gap",
                    constraint=f"(constraint diff_pair_gap (min {max(0.1, g - 0.03):.2f}mm) (opt {g:.2f}mm))",
                    condition=f"A.NetClass == '{cls.name}'",
                )
            )
    return out
