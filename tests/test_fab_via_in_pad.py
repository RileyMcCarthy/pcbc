"""`fab.via_in_pad` and its blockers: the fab's hard stop on a via that wicks a joint.

Both halves of it were measured in S5's review and both were weaker than they read
(`docs/r2-measurements.md` S5):

- the hit was **full containment inside the pad's `(size w h)` box**, so a via breaking a pad's edge
  — which wicks exactly as a centred one does — came back as no hit at all, and a custom pad was
  measured as the 5 um anchor it declares instead of the copper it draws (finding 2);
- the blocker list matched `^[CRL]\\d`, which needs a digit straight after the letter, so it saw `C1`
  and `R1` and not `C_VBUS`, `C_3V3_HF`, `R_FB_BOT` or `R_CC1` — every passive on buck, c3_usb and
  node (finding 1).
"""

from __future__ import annotations

from pcbc.fab import board_pads, passive_refs, via_in_pad, via_in_pad_blockers

# ds2's `C1.1` exactly, read off the built board: a roundrect 0402 land whose copper spans
# 37.25,9.125 .. 38.25,10.575 in world mm.
PAD_W, PAD_H = 1.0, 1.45
PAD_AT = (37.75, 9.85)


def _board(vias: list[tuple[float, float, float]], ref: str = "C1") -> str:
    """One roundrect SMD pad and the vias asked about, as a board file."""
    body = (
        '(kicad_pcb\n\t(version 20260206)\n\t(generator "pcbc")\n\t(generator_version "0.1")\n'
        '\t(general\n\t\t(thickness 1.6)\n\t)\n\t(paper "A4")\n'
        '\t(layers\n\t\t(0 "F.Cu" mixed)\n\t\t(2 "B.Cu" mixed)\n\t\t(25 "Edge.Cuts" user)\n\t)\n'
        "\t(setup\n\t\t(pad_to_mask_clearance 0)\n\t)\n"
        '\t(net 0 "")\n\t(net 1 "REFN_F")\n'
        f'\t(footprint "t:c"\n\t\t(layer "F.Cu")\n\t\t(uuid "00000000-0000-4000-8000-000000000001")\n\t\t(at {PAD_AT[0]} {PAD_AT[1]} 0)\n'
        f'\t\t(property "Reference" "{ref}"\n\t\t\t(at 0 0 0)\n\t\t\t(layer "F.SilkS")\n\t\t\t(uuid "00000000-0000-4000-8000-000000000009")\n\t\t)\n'
        f'\t\t(pad "1" smd roundrect\n\t\t\t(at 0 0)\n\t\t\t(size {PAD_W} {PAD_H})\n\t\t\t(roundrect_rratio 0.25)\n'
        '\t\t\t(layers "F.Cu" "F.Mask" "F.Paste")\n\t\t\t(net 1 "REFN_F")\n\t\t)\n\t)\n'
    )
    for i, (x, y, size) in enumerate(vias):
        body += (
            f'\t(via\n\t\t(at {x} {y})\n\t\t(size {size})\n\t\t(drill 0.3)\n\t\t(layers "F.Cu" "B.Cu")\n'
            f'\t\t(net 1 "REFN_F")\n\t\t(uuid "00000000-0000-4000-8000-0000000000{i:02d}")\n\t)\n'
        )
    return body + ")\n"


def test_a_via_that_only_breaks_a_pads_edge_is_a_hit_and_says_it_is_partial():
    """Finding 2, with its own measurement as the fixture.

    `(37.75, 9.85)` is dead centre and `(38.075, 9.85)` is still wholly on the land; `(38.2375, 9.85)`
    puts 0.1625 mm of a 0.3 mm ring on the pad's copper — more than half the ring — and the old
    containment test called it nothing at all. All three wick the joint; only the first two used to
    be seen.
    """
    text = _board([(37.75, 9.85, 0.3), (38.075, 9.85, 0.3), (38.2375, 9.85, 0.3), (39.0, 9.85, 0.3)])
    hits = via_in_pad(text)
    assert [(h["via"], h["inside"]) for h in hits] == [
        ((37.75, 9.85), True),
        ((38.075, 9.85), True),
        ((38.2375, 9.85), False),
    ], hits
    assert all(h["pad"] == "C1.1" for h in hits), hits
    assert len(via_in_pad_blockers(hits)) == 3, "a passive blocks on any overlap, partial or whole"


def test_the_blocker_list_reads_the_part_and_not_the_spelling_of_its_reference():
    """Finding 1. `C_VBUS`, `C_3V3_HF`, `R_FB_BOT`, `R_CC1` are the ref styles buck, c3_usb and node
    use, and the regex behind this check could not see one of them; `blockers` is the fab stage's
    hard stop, so a via dead-centre in a 0402's pad shipped on two boards.

    The fallback below is the spelling — it is all an audit of a bare board file has — and the real
    answer is the **part**, which `passive_refs` reads off the design's own declared prefixes.
    """
    for ref in ("C1", "C_VBUS", "C_3V3_HF", "R_FB_BOT", "R_CC1", "L1", "D1", "FB1"):
        hits = via_in_pad(_board([(37.75, 9.85, 0.3)], ref=ref))
        assert len(via_in_pad_blockers(hits)) == 1, (ref, "a two-terminal passive's pad")
    for ref in ("U1", "J1", "SW_RST", "FID1", "Y1"):
        hits = via_in_pad(_board([(37.75, 9.85, 0.3)], ref=ref))
        assert via_in_pad_blockers(hits) == [], (ref, "an IC pin or a connector stays a FAB_NOTES line")


def test_the_design_is_what_says_a_part_is_a_passive():
    """`passive_refs` reads `Part.prefix` and `Part.kind`, which the part declares: pcbc's own
    `Resistor`/`Capacitor`/`Led` set "R"/"C"/"D" and `kind="generic"`, and a loaded part says its own
    (buck's inductor is `Component(prefix="L")`). A reference string is a spelling; this is not."""
    from pathlib import Path

    from pcbc.language import load_board

    design = load_board(Path(__file__).resolve().parent.parent / "examples" / "buck" / "buck.py")
    refs = passive_refs(design)
    assert {"C_IN1", "C_BOOT", "R_FB_BOT", "R_EN", "L1"} <= refs, refs
    assert not ({"U1", "J_IN", "J_OUT"} & refs), refs


def test_a_pads_copper_is_what_it_draws_and_not_the_size_it_declares():
    """S1b's lesson, applied to the fab stage (finding 2's second half). A USB-C shell pad declares
    `(size 0.005 0.005)` and draws a 0.7 x 1.4 mm `gr_poly`; `via_in_pad` read the anchor, so those
    four pads on c3_usb and node were invisible to it in both directions."""
    text = _board([(37.75, 9.85, 0.3)])
    (pad,) = board_pads(text)
    x0, y0, x1, y1 = pad.box()
    assert (round(x1 - x0, 4), round(y1 - y0, 4)) == (PAD_W, PAD_H), (pad.box(), "the copper the pad draws")
