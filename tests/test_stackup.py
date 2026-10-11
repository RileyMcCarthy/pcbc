"""The stackup data and every formula pcbc derives a number from, pinned to their references.

One assertion per row of docs/r1-design.md section C.10; every pinned number carries where it
came from in the assertion message, so a drift names the standard it left.
"""

from __future__ import annotations

import math
from dataclasses import replace

import pytest

from pcbc.stackup import (
    JLC_LIMITS,
    PAIR_FIT_MM,
    STACKUPS,
    Copper,
    Dielectric,
    Stackup,
    board_rules,
    capacitance_pf_per_mm,
    coupled_microstrip,
    cpwg,
    current_width_mm,
    diff_pair_geometry,
    diff_pair_width_mm,
    diff_pair_z,
    elliptic_k,
    exact_stripline,
    fanout_lane,
    fanout_stagger,
    get_stackup,
    hole_floor,
    i2c_max_mm,
    iec_clearance_mm,
    iec_creepage_mm,
    ipc2141_stripline,
    ipc2152_area_mil2,
    ipc2152_width_mm,
    ipc2221_clearance_mm,
    ipc2221_width_mm,
    microstrip,
    microstrip_reported_eeff,
    microstrip_z0,
    solve_width,
    stripline,
    stripline_asym,
    via_amps,
    via_barrel_mil2,
    vias_per_change,
    width_for_z0,
)
from conftest import write_split

L2 = get_stackup("jlcpcb_2l_1oz")
L4 = get_stackup("jlcpcb_4l_1oz")
L1080 = get_stackup("jlcpcb_4l_1oz_1080")


# --- B. the stackups ---------------------------------------------------------------------------


def test_the_five_stackups_and_their_jlc_codes():
    """B.2: the 1.6 mm build of each JLC impedance code, jlcpcb.com/impedance read 2026-09-19."""
    assert sorted(STACKUPS) == ["jlcpcb_2l_1oz", "jlcpcb_2l_2oz", "jlcpcb_4l_1oz", "jlcpcb_4l_1oz_1080", "jlcpcb_4l_1oz_2116", "jlcpcb_4l_1oz_3313", "jlcpcb_4l_2oz", "jlcpcb_4l_2oz_3313"]
    assert L4.jlc_code == "JLC04161H-7628", "B.3: the default 4L is what JLC builds for a 1.6 mm order without an impedance pick"
    assert get_stackup("JLC04161H-3313") is STACKUPS["jlcpcb_4l_1oz_3313"], "B.1: the JLC code is an alias of the pcbc name"
    assert get_stackup("JLC04161H-7628") is L4 and get_stackup("JLC04161H-2116").name == "jlcpcb_4l_1oz_2116"
    assert get_stackup("JLC04161H-1080") is L1080
    assert L2.jlc_code is None, "B.2: JLC publishes no 2-layer impedance stackup"
    with pytest.raises(KeyError, match="unknown stackup 'jlcpcb_6l'"):
        get_stackup("jlcpcb_6l")
    # The layer stacks, top to bottom, and their sums (B.2 table).
    assert L4.stack == (
        Copper("F.Cu", 0.035),
        Dielectric("7628", 0.2104, 4.4, "prepreg"),
        Copper("In1.Cu", 0.0152),
        Dielectric("core", 1.065, 4.6, "core"),
        Copper("In2.Cu", 0.0152),
        Dielectric("7628", 0.2104, 4.4, "prepreg"),
        Copper("B.Cu", 0.035),
    ), "B.2: JLC04161H-7628 as jlcpcb.com/impedance lists it (core 1.065 verify)"
    assert L2.stack == (Copper("F.Cu", 0.035), Dielectric("core", 1.53, 4.6, "core"), Copper("B.Cu", 0.035)), "B.3: 1.6 mm less two 0.035 coppers, JLC's core Dk"
    sums = {name: s.board_mm for name, s in sorted(STACKUPS.items()) if s.copper_oz == 1.0}
    assert sums == {"jlcpcb_2l_1oz": 1.6, "jlcpcb_4l_1oz": 1.5862, "jlcpcb_4l_1oz_1080": 1.5182, "jlcpcb_4l_1oz_2116": 1.5982, "jlcpcb_4l_1oz_3313": 1.5642}, "B.2 sum column (4 dp)"
    for name, s in sorted(STACKUPS.items()):
        assert s.copper_layers() == (("F.Cu", "B.Cu") if s.layers == 2 else ("F.Cu", "In1.Cu", "In2.Cu", "B.Cu")), name
        assert (s.mask_over_substrate_mm, s.mask_over_trace_mm, s.mask_dk, s.via_plating_mm) == (0.0305, 0.0152, 3.8, 0.018), "JLC impedance page mask 1.2 / 0.6 mil Dk 3.8; plating 18 um"
    thin = {"jlcpcb_4l_1oz_3313": ("3313", 0.0994, 4.1), "jlcpcb_4l_1oz_2116": ("2116", 0.1164, 4.16), "jlcpcb_4l_1oz_1080": ("1080", 0.0764, 3.91)}
    for name, (mat, h, dk) in thin.items():
        s = STACKUPS[name]
        assert (s.stack[1], s.stack[3]) == (Dielectric(mat, h, dk, "prepreg"), Dielectric("core", 1.265, 4.6, "core")), f"B.2: {name} prepreg and its 1.265 core"


def test_the_2oz_stackups_are_jlcs_numbers_and_every_current_width_names_its_stackup():
    """Decision 3 (owner, 2026-10-01): JLC's 2 oz outer-copper options beside the 1 oz ones. The 4-layer stacks are
    JLC's JLC04162H templates (outer 0.07 mm, the JLC04161H prepreg and core); the 2-layer core is the 1 oz rung's rule
    (1.6 mm less the two coppers); the fab minima are JLC's 2 oz rows. A width derived from a 2 oz board is the 2 oz
    width and its source names the stackup."""
    from pcbc.stackup import cu_text, current_width_mm

    two = get_stackup("jlcpcb_2l_2oz")
    assert two.stack == (Copper("F.Cu", 0.07), Dielectric("core", 1.46, 4.6, "core"), Copper("B.Cu", 0.07)), "1.6 - 2 x 0.07"
    assert (two.layers, two.copper_oz, two.board_mm, two.t_mm, two.jlc_code) == (2, 2.0, 1.6, 0.07, None)
    assert (two.track_min, two.clearance_min, two.mask_bridge_min) == (0.16, 0.16, 0.20), "JLC 2 oz, 2-layer: 0.16 / 0.16 mm; mask 0.20 mm"
    four = get_stackup("JLC04162H-7628")
    assert four is STACKUPS["jlcpcb_4l_2oz"]
    assert four.stack == (
        Copper("F.Cu", 0.07),
        Dielectric("7628", 0.2104, 4.4, "prepreg"),
        Copper("In1.Cu", 0.0152),
        Dielectric("core", 1.065, 4.6, "core"),
        Copper("In2.Cu", 0.0152),
        Dielectric("7628", 0.2104, 4.4, "prepreg"),
        Copper("B.Cu", 0.07),
    ), "JLC04162H-7628: outer 0.07 mm over the JLC04161H-7628 dielectrics"
    assert (four.copper_oz, four.board_mm, four.track_min, four.clearance_min, four.mask_bridge_min) == (2.0, 1.6562, 0.15, 0.15, 0.20)
    assert get_stackup("JLC04162H-3313").stack[1:6] == STACKUPS["jlcpcb_4l_1oz_3313"].stack[1:6]
    assert all(STACKUPS[n].z_bias_source.startswith("uncalibrated: formula only") for n in ("jlcpcb_2l_2oz", "jlcpcb_4l_2oz", "jlcpcb_4l_2oz_3313"))
    # The width the 2 A of buck's SW gets over the 2-layer pour: 0.781 mm at 1 oz, 0.391 mm at 2 oz.
    w1 = current_width_mm(2.0, 10.0, L2, 1.53)
    w2 = current_width_mm(2.0, 10.0, two, 1.46)
    assert (w1.value, w2.value) == (0.781, 0.391)
    assert "2 oz jlcpcb_2l_2oz" in w2.source.ref and "1 oz jlcpcb_2l_1oz" in w1.source.ref, (w1.line(), w2.line())
    assert cu_text(four) == "2 oz jlcpcb_4l_2oz"


def test_the_derived_views_keep_every_existing_caller_working():
    """B.1: h_mm, t_mm, er are the dielectric under F.Cu, the outer copper and its Dk."""
    assert (L4.h_mm, L4.t_mm, L4.er) == (0.2104, 0.035, 4.4)
    assert (L2.h_mm, L2.t_mm, L2.er) == (1.53, 0.035, 4.6)
    assert L4.copper_t("In1.Cu") == 0.0152 and L4.copper_t("B.Cu") == 0.035
    assert L4.dielectric_between("F.Cu", "In1.Cu") == (0.2104, 4.4)
    assert L4.dielectric_between("F.Cu", "In2.Cu") == (1.2906, 4.6202), "B.1: 0.2104 + 0.0152 + 1.065, series Dk h_total / sum(h_i / dk_i)"
    assert L4.dielectric_between("In2.Cu", "F.Cu") == L4.dielectric_between("F.Cu", "In2.Cu")
    assert L4.plane_below("F.Cu") == "In1.Cu" and L4.plane_below("In2.Cu") == "B.Cu" and L4.plane_below("B.Cu") is None
    assert L2.plane_below("F.Cu") == "B.Cu"
    with pytest.raises(KeyError, match="no copper layer 'In1.Cu'"):
        L2.copper_t("In1.Cu")


def test_height_to_reference_finds_the_adjacent_plane_or_the_two_layer_pour():
    """B.1: the adjacent declared plane; on 2L the B.Cu GND pour the route plan always writes."""
    node_planes = (("GND", "In1.Cu"), ("3V3", "In2.Cu"))
    assert L4.height_to_reference("F.Cu", node_planes, False) == (0.2104, "In1.Cu")
    assert L4.height_to_reference("B.Cu", node_planes, False) == (0.2104, "In2.Cu")
    assert L4.height_to_reference("In1.Cu", (("3V3", "In2.Cu"),), False) == (1.065, "In2.Cu")
    assert L4.height_to_reference("F.Cu", (("3V3", "In2.Cu"),), False) is None, "In2.Cu is not adjacent to F.Cu"
    assert L2.height_to_reference("F.Cu", (), True) == (1.53, "B.Cu pour")
    assert L2.height_to_reference("F.Cu", (), False) is None
    assert L2.height_to_reference("B.Cu", (), True) is None, "the pour is on B.Cu itself, not under it"


def test_the_fab_limits_are_todays_and_jlcs_own_are_for_the_report():
    """B.2: limits unchanged from R0; JLC_LIMITS carries JLC's 1 oz / 2 oz numbers for the report only."""
    assert (L2.track_min, L2.clearance_min, L2.via_drill, L2.via_diameter, L2.annular_min) == (0.127, 0.127, 0.3, 0.5, 0.1)
    for name in ("jlcpcb_4l_1oz", "jlcpcb_4l_1oz_3313", "jlcpcb_4l_1oz_2116", "jlcpcb_4l_1oz_1080"):
        s = STACKUPS[name]
        assert (s.track_min, s.clearance_min, s.via_drill, s.via_diameter, s.annular_min) == (0.0889, 0.0889, 0.2, 0.35, 0.075), name
    for s in STACKUPS.values():
        assert (s.hole_clearance, s.hole_to_hole, s.edge_clearance) == (0.25, 0.5, 0.3)
        assert s.copper_oz == (2.0 if "_2oz" in s.name else 1.0), s.name
    assert JLC_LIMITS == {(True, 1.0): (0.10, 0.10), (False, 1.0): (0.09, 0.09), (True, 2.0): (0.16, 0.16), (False, 2.0): (0.15, 0.15)}, "JLC capabilities page, 1 oz / 2 oz"
    assert hole_floor(L2) == 0.155 and hole_floor(L4) == 0.18, "hole_clearance - annular_min + 0.005"


def test_the_bias_and_the_published_rows():
    """C.2: the fab bias per stackup, fitted to JLC's rows as JITX records them."""
    assert (L4.z_bias_se, L4.z_bias_diff) == (0.9147, 0.8532), "7628: 50 / 54.660; geometric mean of 90/104.635 and 100/118.170"
    assert (L1080.z_bias_se, L1080.z_bias_diff) == (0.8968, 0.8308), "1080: 50 / 55.754; geometric mean of 90/111.331 and 100/117.114"
    for name in ("jlcpcb_4l_1oz_3313", "jlcpcb_4l_1oz_2116"):
        s = STACKUPS[name]
        assert (s.z_bias_se, s.z_bias_diff, s.rows) == (0.9058, 0.842, ()), f"{name}: interpolated between the 7628 and 1080 fits"
        assert s.z_bias_source == "interpolated between 7628 and 1080 fits; capture JLC rows to replace"
    assert (L2.z_bias_se, L2.z_bias_diff, L2.rows, L2.z_bias_source) == (1.0, 1.0, (), "uncalibrated: formula only")
    assert L4.z_bias_source == "fitted to JLC04161H-7628 rows (JITX), 2026-09-19"
    assert L1080.z_bias_source == "fitted to JLC04161H-1080 rows (JITX), 2026-09-19"
    rows = [(r.structure, r.z_ohm, r.width_mm, r.gap_mm) for r in L4.rows]
    assert rows == [("se", 50.0, 0.3244, None), ("diff", 90.0, 0.2332, 0.15), ("diff", 100.0, 0.1722, 0.15)], "JLC04161H-7628 rows (JITX)"
    rows = [(r.structure, r.z_ohm, r.width_mm, r.gap_mm) for r in L1080.rows]
    assert rows == [("se", 50.0, 0.1176, None), ("diff", 90.0, 0.09, 0.09), ("diff", 100.0, 0.09, 0.137)], "JLC04161H-1080 rows (JITX)"
    assert all(r.source == "JLCPCB impedance calculator as recorded by JITX, jitxlib.jlcpcb.JLC04161H_7628, read 2026-09-19" for r in L4.rows)


# --- C.10 vectors, one per row ------------------------------------------------------------------


def test_vector_1_hj_textbook_alumina_line():
    z0, eeff = microstrip(1.0, 1.0, 0, 9.8)
    assert abs(z0 - 49.289) <= 0.02, f"C.10 #1: H&J 1980 alumina line w/h = 1, er 9.8 is 49.289 ohm (KiCad calculator); got {z0}"
    assert abs(eeff - 6.579) <= 0.005, f"C.10 #1: eeff 6.579 (H&J 1980); got {eeff}"


def test_vector_2_hj_with_thickness_is_assembled_as_kicad_assembles_it():
    """Measured against a harness compiled from KiCad 10.0.6's own transline code: 54.66016 ohm.
    An earlier pcbc put the (Z01(u1)/Z01(ur))^2 factor into Z0 and dropped KiCad's q_t term,
    reading 55.165, which is neither KiCad nor bare H&J (53.858)."""
    z0, eeff = microstrip(0.3244, 0.2104, 0.035, 4.4)
    assert abs(z0 - 54.6602) <= 0.02, f"C.10 #2: KiCad 10.0.6 microstrip_Z0() at JLC's 7628 50 ohm width is 54.660 ohm; got {z0}"
    assert abs(eeff - 3.1999) <= 0.002, f"C.10 #2: eeff_t 3.1999, the permittivity Z0 is built from; got {eeff}"
    assert abs(microstrip_reported_eeff(0.3244, 0.2104, 0.035, 4.4) - 3.0501) <= 0.002, "C.10 #2: KiCad displays eeff_t (Z01(u1)/Z01(ur))^2 = 3.0501"


def test_vector_3_hj_on_1080():
    z0, _ = microstrip(0.1176, 0.0764, 0.035, 3.91)
    assert abs(z0 - 55.7539) <= 0.02, f"C.10 #3: KiCad's form at JLC's 1080 50 ohm width is 55.754 ohm (measured from the compiled KiCad harness); got {z0}"


def test_vector_4_hj_on_the_two_layer_core_and_its_50_ohm_width():
    z0, _ = microstrip(1.0, 1.53, 0.035, 4.6)
    assert abs(z0 - 83.033) <= 0.02, f"C.10 #4: 1.0 mm on 1.53 mm FR-4 is 83.033 ohm (compiled KiCad harness: 83.033); got {z0}"
    w = width_for_z0(50, L2)
    assert abs(w - 2.7966) <= 0.0005, f"C.10 #4: 50 ohm on jlcpcb_2l_1oz (bias 1.0) is 2.7966 mm; got {w}"


def test_vector_5_the_bias_reproduces_jlcs_50_ohm_row():
    w = width_for_z0(50, L4)
    assert abs(w - 0.3244) <= 0.0005, f"C.10 #5: 50 ohm on 7628 with bias 0.9147 is JLC's row 0.3244 mm (by construction); got {w}"
    assert microstrip_z0(0.3244, L4) == 50.0, "C.2: z_bias_se x Z_bare at JLC's row reads JLC's 50 ohm"


def test_vector_6_coupled_hj_at_jlcs_90_ohm_row():
    ze, zo, _, _ = coupled_microstrip(0.2332, 0.15, 0.2104, 0.035, 4.4)
    assert abs(2 * zo - 104.635) <= 0.05, f"C.10 #6: H&J coupled at JLC's 7628 90 ohm row is Zdiff 104.635 (x 0.8532 = 89.3 vs JLC 90); got {2 * zo}"
    assert abs(ze - 73.93) <= 0.05, f"C.10 #6: Ze 73.93 (H&J even mode); got {ze}"
    assert abs(zo - 52.318) <= 0.05, f"C.10 #6: Zo 52.318 (H&J odd mode); got {zo}"


def test_vector_7_coupled_hj_at_jlcs_100_ohm_row():
    _, zo, _, _ = coupled_microstrip(0.1722, 0.15, 0.2104, 0.035, 4.4)
    assert abs(2 * zo - 118.17) <= 0.05, f"C.10 #7: H&J coupled at JLC's 7628 100 ohm row is 118.17 (x 0.8532 = 100.8 vs JLC 100); got {2 * zo}"


def test_vector_8_coupled_hj_at_the_1080_rows():
    z90 = 2 * coupled_microstrip(0.09, 0.09, 0.0764, 0.035, 3.91)[1]
    z100 = 2 * coupled_microstrip(0.09, 0.137, 0.0764, 0.035, 3.91)[1]
    assert abs(z90 - 111.331) <= 0.05, f"C.10 #8: 1080 90 ohm row (0.09/0.09) is 111.331 (x 0.8308 = 92.5, the worst residual +2.8 %); got {z90}"
    assert abs(z100 - 117.114) <= 0.05, f"C.10 #8: 1080 100 ohm row (0.09/0.137) is 117.114 (x 0.8308 = 97.3, -2.7 %); got {z100}"


def test_vector_9_pair_synthesis_with_the_bias_on_7628():
    w90, g90 = diff_pair_geometry(90, L4)
    w100, g100 = diff_pair_geometry(100, L4)
    assert abs(w90 - 0.2288) <= 0.0005 and g90 == 0.15, f"C.10 #9: 90 ohm on 7628 at gap 0.15 is 0.2288 mm (JLC row 0.2332, -1.9 %); got {(w90, g90)}"
    assert abs(w100 - 0.1759) <= 0.0005 and g100 == 0.15, f"C.10 #9: 100 ohm on 7628 is 0.1759 mm (JLC row 0.1722, +2.1 %); got {(w100, g100)}"
    assert diff_pair_z(0.2288, 0.15, L4) == 90.0, "C.2: z_bias_diff x 2 Zo at the synthesised geometry reads the target"
    assert diff_pair_z(0.2332, 0.15, L4) == 89.27, "C.2 residual: JLC's own 90 ohm row reads 89.3 (-0.8 %)"
    assert diff_pair_z(0.1722, 0.15, L4) == 100.82, "C.2 residual: JLC's own 100 ohm row reads 100.8 (+0.8 %)"
    # The other calibrated stackup and the interpolated one.
    assert abs(width_for_z0(50, L1080) - 0.1176) <= 0.0005, "C.2: 1080 50 ohm lands on JLC's row 0.1176"
    assert abs(diff_pair_width_mm(90, L1080, gap=0.09) - 0.0958) <= 0.0005, "C.2: 1080 90 ohm at gap 0.09 is 0.0958 (row 0.09, +6 %)"
    # C.2 says 0.0846 for 1080's 100 ohm pair at gap 0.137 (row 0.09, -6 %); that is under the
    # 4L track_min, and the solver's floor is track_min (section C), so it saturates at 0.0889.
    assert diff_pair_width_mm(100, L1080, gap=0.137) == 0.0889, "C.2 / C: the 0.0846 solve is under the fab floor 0.0889, where the bisection starts"
    assert diff_pair_geometry(100, L1080, gap=0.137) == (0.0889, 0.137), "a member under the fab's track_min is written at it"
    assert diff_pair_geometry(90, get_stackup("jlcpcb_4l_1oz_3313")) == (0.1351, 0.15), "B.3: 3313 would keep the pair near today's width"


def test_vector_10_the_pair_fit_clamp_on_two_layers():
    assert diff_pair_geometry(90, L2) == (0.127, 0.127), "C.10 #10: a 90 ohm member on 1.53 mm FR-4 is over PAIR_FIT_MM, so the pair is written at the fab floor (track_min, clearance_min)"
    assert PAIR_FIT_MM == 0.25, "A.1: a member wider than 0.25 cannot leave a USB-C's 0.3 mm pads at 0.5 mm pitch"
    w = diff_pair_width_mm(90, L2)
    assert abs(w - 0.7746) <= 0.0005, f"C.10 #10: the solved member before the clamp is 0.7746 mm (H&J coupled, gap 0.127); got {w}"
    z = 2 * coupled_microstrip(0.127, 0.127, 1.53, 0.035, 4.6)[1]
    assert abs(z - 140.053) <= 0.05, f"C.10 #10: the clamped pair at 0.127/0.127 on 1.53 mm reads 140.05 ohm (feeding ur into the Q-terms gives 133.08); got {z}"
    assert diff_pair_z(0.127, 0.127, L2) == 140.05, "C.3: the 2L note's 140.1 ohm"
    sanity = 2 * coupled_microstrip(0.9, 0.127, 1.53, 0.035, 4.6)[1]
    assert abs(sanity - 83.376) <= 0.05, f"C.10 #10 sanity row: 0.9 mm at 5 mil gap on 1.52 mm FR-4 is 83.38 (a Polar-solver forum figure gives 0.93 mm for 90 ohm); got {sanity}"


def test_vector_11_wadell_stripline_with_thickness():
    cases = ((0.2, 1.0, 0.0152, 67.46, 66.68, 67.19), (0.15, 0.5, 0.0175, 54.86, 54.07, 54.28), (0.2, 0.4, 0.0175, 42.55, 40.69, 42.47))
    for w, b, t, expected, ipc, kicad in cases:
        z = stripline(w, b, t, 4.6)
        assert abs(z - expected) <= 0.02, f"C.10 #11: Wadell 4.2.1 (Cohn with thickness) stripline({w}, {b}, {t}, 4.6) is {expected} (KiCad {kicad} within 0.5 %; the mis-transcribed m = 6x/(3+2x) gives 70.26 at the first); got {z}"
        assert abs(z - kicad) / kicad <= 0.011, f"C.10 #11: KiCad 10 gives {kicad} (its separate even/odd thickness widths); within 1.1 % (the second vector measures 1.07 %, not the 0.5 % C.4 states)"
        z_ipc = ipc2141_stripline(w, b, t, 4.6)
        assert abs(z_ipc - ipc) <= 0.02, f"C.10 #11 cross-check column: IPC-2141 60/sqrt(er) ln(1.9 b/(0.8 w + t)) is {ipc}; got {z_ipc}"


def test_vector_12_wadell_reproduces_the_exact_conformal_map_at_zero_thickness():
    for w, expected, exact in ((0.2, 71.39, 71.40), (0.3, 60.31, 60.33), (0.5, 46.82, 46.86)):
        z = stripline(w, 1.0, 1e-9, 4.6)
        assert abs(z - expected) <= 0.05, f"C.10 #12: stripline({w}, 1.0, t -> 0, 4.6) is {expected}; got {z}"
        k = exact_stripline(w, 1.0, 4.6)
        assert abs(k - exact) <= 0.01, f"C.10 #12: the exact map (30 pi/sqrt(er)) K(k')/K(k), k = tanh(pi w/2b), is {exact}; got {k}"
        assert abs(z - k) / k <= 0.001, f"C.4: Wadell reproduces the conformal map as t -> 0 (0.01 % at w = 0.2, 0.09 % at w = 0.5; C.4 states 0.05 %); got {z} vs {k}"


def test_vector_13_asymmetric_stripline_on_in1_of_7628():
    z = stripline_asym(0.2, 0.2104, 1.065, 0.0152, 4.4, 4.6)
    assert abs(z - 60.09) <= 0.02, f"C.10 #13: parallel-combination form 2 Z(2h1+t) Z(2h2+t)/(Z(2h1+t) + Z(2h2+t)), In1.Cu on 7628, is 60.09 ohm; got {z}"
    w = width_for_z0(50, L4, layer="In1.Cu")
    assert abs(w - 0.2968) <= 0.0005, f"C.10 #13: 50 ohm on In1.Cu of 7628 is 0.2968 mm (formula only, no bias on inner layers); got {w}"
    assert microstrip_z0(0.2, L4, layer="In1.Cu") == 60.09, "an inner-layer request reads the asymmetric stripline"


def test_vector_14_ghione_naldi_cpwg():
    z, eeff = cpwg(0.5, 0.3, 1.53, 4.6)
    assert abs(z - 73.786) <= 0.02, f"C.10 #14: Ghione-Naldi 1987 conductor-backed CPW as KiCad (t = 0) cpwg(0.5, 0.3, 1.53, 4.6) is 73.79 ohm; got {z}"
    assert abs(eeff - 2.8382) <= 0.002, f"C.10 #14: eeff 2.8382; got {eeff}"
    z_wide, _ = cpwg(1.5, 0.3, 1.53, 4.6)
    assert abs(z_wide - 50.47) <= 0.02, f"C.10 #14: cpwg(1.5, 0.3, 1.53, 4.6) is 50.47; got {z_wide}"
    w = width_for_z0(50, L2, pour_gap=0.2)
    assert abs(w - 1.1992) <= 0.0005, f"C.10 #14: 50 ohm on 2L with the pour at s = 0.2 is 1.1992 mm; got {w}"
    assert abs(elliptic_k(0.0) - math.pi / 2) < 1e-12, "K(0) = pi/2 (AGM)"
    assert abs(elliptic_k(0.5) - 1.6857503548125960) < 1e-9, "K(0.5) = 1.68575 (Abramowitz and Stegun 17.3)"
    # C.5 validity: past s/h = 1 the wide-gap limit overestimates a plain microstrip, so pcbc takes the smaller.
    assert microstrip_z0(1.0, L2, pour_gap=5.0) == microstrip_z0(1.0, L2) == 83.03, "C.5: min(cpwg, microstrip) beyond s/h = 1: cpwg reads 88.10 at s = 5, the microstrip 83.033"
    assert microstrip_z0(1.0, L2, pour_gap=2.0) == 82.64, "C.5: at s/h = 1.31 the CPWG form (82.64) is still under the microstrip and is what min() keeps"


def test_vector_15_ipc2152_universal_chart_fit_against_the_smps_fit():
    area = ipc2152_area_mil2(10, 20)
    assert abs(area - 517.9) <= 0.5, f"C.10 #15: IPC-2152 Fig 5-1 fit (mbedded.ninja coefficients) at 10 A / 20 C is 517.9 mil2; got {area}"
    smps = (117.555 * 20**-0.913 + 1.15) * 10 ** (0.84 * 20**-0.108 + 1.159)
    assert abs(smps - 513.1) <= 0.5, f"C.10 #15: the smps.us fit of the same chart gives 513.1 (both within 4 % of the chart's 500); got {smps}"


def test_vector_16_ipc2152_width_without_a_plane():
    w1 = ipc2152_width_mm(1, 10, 1, 1.6, None)
    w2 = ipc2152_width_mm(2, 10, 1, 1.6, None)
    assert abs(w1 - 0.309) <= 0.002, f"C.10 #16: IPC-2152 1 A 10 C 1 oz on 1.6 mm, no plane, is 0.309 mm (board mod 1.092, Fig 5-8); got {w1}"
    assert abs(w2 - 1.090) <= 0.002, f"C.10 #16: 2 A, no plane, is 1.090 mm (where IPC-2152 raises the width over IPC-2221's 0.781); got {w2}"


def test_vector_17_ipc2152_width_with_the_plane_modifier():
    w_pour = ipc2152_width_mm(2, 10, 1, 1.6, 1.53)
    w_in1 = ipc2152_width_mm(1, 10, 1, 1.6, 0.2104)
    assert abs(w_pour - 0.646) <= 0.002, f"C.10 #17: 2 A over the 2L pour at 1.53 mm is 0.646 (plane mod 0.593, Fig 5-11); got {w_pour}"
    assert abs(w_in1 - 0.133) <= 0.002, f"C.10 #17: 1 A over In1 at 0.2104 mm is 0.133 (plane mod 0.430); got {w_in1}"


def test_vector_18_the_ipc2221_floor_holds_over_ipc2152():
    d = current_width_mm(2, 10, L2, 1.53)
    assert d.value == 0.781, f"C.10 #18: buck's 2 A power width is unchanged from test_route_plan (IPC-2221 0.781 over IPC-2152 0.646); got {d.value}"
    assert d.unit == "mm" and d.source.formula == "ipc2221_ext", "C.6: the source names the winner"
    assert "0.646" in d.source.note, f"C.6: the note names the loser (IPC-2152 with the pour 0.646); got {d.source.note!r}"
    assert d.source.note == "IPC-2221 floor holds over ipc2152_fit x board 1.092 x plane 0.593 at 1.53 mm 0.646"
    assert d.source.ref == "IPC-2221B eq. 6-2 external 2 A 10 C 1 oz jlcpcb_2l_1oz"
    raised = current_width_mm(2, 10, L2, None)
    assert (raised.value, raised.source.formula) == (1.09, "ipc2152_fit"), "C.6: a no-plane track above 1 A is where IPC-2152 raises the width (2 A: 1.090)"
    assert raised.source.note == "over the IPC-2221 external floor 0.781"
    low = current_width_mm(0.2, 10, L2, 1.53)
    assert low.source.note.endswith("; below 0.274 A the IPC-2152 fit extrapolates"), "C.6: the fit is valid 0.274-26 A and the report says so below it"


def test_vector_19_ipc2221_widths_are_the_existing_pins():
    assert ipc2221_width_mm(2) == 0.781, "C.10 #19: IPC-2221 external 2 A 10 C 1 oz, the existing pin"
    assert ipc2221_width_mm(1) == 0.300, "C.10 #19: 1 A"
    assert ipc2221_width_mm(3) == 1.367, "C.10 #19: 3 A"


def test_vector_20_one_via_at_its_temperature_rise():
    assert abs(via_barrel_mil2(0.3, 0.018) - 26.30) <= 0.01, "C.7: pi x 0.3 x 0.018 mm2 = 26.30 mil2"
    assert abs(via_barrel_mil2(0.2, 0.018) - 17.53) <= 0.01, "C.7: 0.2 mm drill barrel 17.53 mil2"
    assert abs(via_amps(0.3, 0.018, 10) - 0.707) <= 0.002, "C.10 #20: IPC-2221 internal on a 26.30 mil2 barrel at 10 C is 0.707 A (Brooks and Adam ch. 9; JLC plating 18 um)"
    assert abs(via_amps(0.2, 0.018, 10) - 0.527) <= 0.002, "C.10 #20: 0.2 mm drill 0.527 A (node's LOAD on 0.2 mm drills wants 2 per change)"
    assert abs(via_amps(0.3, 0.018, 20) - 0.960) <= 0.002, "C.10 #20: 0.3 mm at 20 C 0.960 A"


def test_vector_21_vias_per_layer_change():
    assert vias_per_change(1.0, 0.3) == 2, "C.10 #21: ceil(1.0 / 0.707) = 2 (C.7)"
    assert vias_per_change(0.1, 0.3) == 1, "C.10 #21: never fewer than 1"
    assert vias_per_change(2.0, 0.3) == 3, "C.10 #21: ceil(2.0 / 0.707) = 3"
    assert vias_per_change(1.0, 0.2) == 2, "C.10 #21: ceil(1.0 / 0.527) = 2"


def test_vector_22_ipc2221_table_6_1_clearance():
    cases = ((5, "B2", 0.1), (48, "B2", 0.6), (250, "B2", 1.25), (400, "B2", 2.5), (600, "B2", 3.0), (250, "B1", 0.2), (250, "B4", 0.4), (1000, "B1", 1.5))
    for volts, row, expected in cases:
        got = ipc2221_clearance_mm(volts, row)
        assert got == expected, f"C.10 #22: IPC-2221B Table 6-1 row {row} at {volts} V is {expected} (as KiCad's panel_electrical_spacing_ipc2221.cpp; above 500 V the 301-500 value plus the per-volt increment); got {got}"
    with pytest.raises(ValueError, match="B1, B2 or B4"):
        ipc2221_clearance_mm(48, "B3")


def test_vector_23_iec_60664_creepage():
    assert iec_creepage_mm(250, "IIIa") == 2.5, "C.10 #23: IEC 60664-1 F.5 / 62368-1 T17 PD2 250 V group IIIa 2.5 mm"
    assert iec_creepage_mm(250, "I") == 1.25, "C.10 #23: 250 V group I 1.25"
    assert iec_creepage_mm(250, "II") == 1.8, "C.10 #23: 250 V group II 1.8"
    assert iec_creepage_mm(400, "IIIa") == 4.0, "C.10 #23: 400 V group IIIa 4.0 (SLUP419 Table 3)"
    assert iec_creepage_mm(48, "IIIa") == 1.2, "C.10 #23: 48 V looks up the next row, 50 V, 1.2"
    assert iec_creepage_mm(250, "IIIa", reinforced=True) == 5.0, "C.10 #23: reinforced doubles"
    assert iec_creepage_mm(250, "IIIb") == 2.5, "C.8: IIIa and IIIb share the PD2 column"
    assert (iec_creepage_mm(63, "I"), iec_creepage_mm(63, "II"), iec_creepage_mm(63, "IIIa")) == (0.63, 0.9, 1.25), "SLUP419 Table 3 cross-check at 63 V"
    assert (iec_creepage_mm(800, "I"), iec_creepage_mm(800, "II"), iec_creepage_mm(800, "IIIa")) == (4.0, 5.6, 8.0), "SLUP419 Table 3 cross-check at 800 V"
    assert (iec_creepage_mm(1000, "I"), iec_creepage_mm(1000, "II"), iec_creepage_mm(1000, "IIIa")) == (5.0, 7.1, 10.0), "SLUP419 Table 3 cross-check at 1000 V"
    with pytest.raises(ValueError, match="pollution degree 2 only"):
        iec_creepage_mm(250, "IIIa", pollution_degree=3)
    with pytest.raises(ValueError, match="stops at 1000 V"):
        iec_creepage_mm(1500, "IIIa")


def test_vector_24_iec_60664_impulse_clearance():
    assert iec_clearance_mm(250) == 1.5, "C.10 #24: F.1 OV II 250 V -> 2.5 kV impulse -> F.2 PD2 1.5 mm (Isolation 250 V: max(1.25, 1.5))"
    assert iec_clearance_mm(250, reinforced=True) == 3.0, "C.10 #24: reinforced x1.6 -> 4 kV -> 3.0 mm"
    assert iec_clearance_mm(48) == 0.2, "C.10 #24: 48 V -> 500 V impulse, under the PD2 minimum 0.2 mm"
    assert iec_clearance_mm(100) == 0.2 and iec_clearance_mm(150) == 0.5 and iec_clearance_mm(600) == 3.0 and iec_clearance_mm(1000) == 5.5, "F.1 OV II rows 800 / 1500 / 4000 / 6000 V into F.2"
    with pytest.raises(ValueError, match="overvoltage category II only"):
        iec_clearance_mm(250, overvoltage_category=3)


def test_vector_25_i2c_length_from_capacitance():
    z0, eeff = microstrip(0.16, 1.53, 0.035, 4.6)
    c = capacitance_pf_per_mm(z0, eeff)
    assert abs(c - 0.0404) <= 0.0002, f"C.10 #25: sqrt(eeff)/(c z0) at 0.16 mm on 2L (142.9 ohm) is 0.0404 pF/mm; got {c}"
    length = i2c_max_mm(400, 2, c)
    assert abs(length - 9406) / 9406 <= 0.01, f"C.10 #25: (400 pF - 10 pF x 2 pins) / 0.0404 = 9406 mm (UM10204 rev 7 section 7.1); got {length}"


def test_vector_26_existing_pins_unchanged():
    assert fanout_stagger(L2, 0.2, 0.65) == 0.4664, "C.10 #26: existing pin (test_pcb_place)"
    assert fanout_lane(L2, 0.2, 0.65) == 1.2934, "C.10 #26: existing pin (test_pcb_place)"
    assert board_rules(L2) == {
        "min_clearance": 0.127,
        "min_track_width": 0.127,
        "min_via_diameter": 0.5,
        "min_via_annular_width": 0.1,
        "min_through_hole_diameter": 0.3,
        "min_hole_clearance": 0.25,
        "min_hole_to_hole": 0.5,
        "min_copper_edge_clearance": 0.3,
    }, "C.10 #26: today's dict (test_copper_rules pins it against the .kicad_pro)"


# --- the solver and the trap the vectors exist to catch --------------------------------------


def test_the_solver_is_bisection_from_the_fab_floor_and_deterministic():
    """C: 60 iterations from track_min to 6.0 mm, 4 dp; a target the stackup cannot reach saturates."""
    assert solve_width(lambda w: 100.0 / w, 50.0, L2) == 2.0
    assert solve_width(lambda w: 100.0 / w, 1.0, L2) == 6.0, "hi = 6.0: an impossible target saturates at the ceiling"
    assert solve_width(lambda w: 100.0 / w, 1e9, L2) == L2.track_min, "lo = track_min: never under the fab floor"
    assert width_for_z0(50, L4) == width_for_z0(50, L4) == 0.3244
    assert isinstance(L4, Stackup) and L4.layers == 4


def test_thickness_enters_the_coupled_line_only_through_z0_and_eeff():
    """C.3: u = w/h is the bare ratio. Feeding ur into the Q-terms reads 104.15 at vector 6 and 133.1 at the 2L clamp."""
    z6 = 2 * coupled_microstrip(0.2332, 0.15, 0.2104, 0.035, 4.4)[1]
    assert abs(z6 - 103.67) > 0.5 and abs(z6 - 104.635) <= 0.05, "C.3: the ur trap (103.67) is not what the bare-u form gives (104.635)"
    zc = 2 * coupled_microstrip(0.127, 0.127, 1.53, 0.035, 4.6)[1]
    assert abs(zc - 133.08) > 5 and abs(zc - 140.053) <= 0.05, "C.3: the ur trap (133.08) is not what the bare-u form gives (140.05)"


def test_the_ipc2152_fit_says_when_it_is_off_its_chart():
    """C.6 gives the fit's range (0.274 to 26 A, boards 0.72 to 2.36 mm). Only the low end said so,
    so a 30 A rail or a 0.5 mm board read as if the chart covered it."""
    L2 = get_stackup("jlcpcb_2l_1oz")
    assert "below 0.274 A the IPC-2152 fit extrapolates" in current_width_mm(0.1, 10, L2, 1.53).source.note
    assert "above 26 A the IPC-2152 fit extrapolates" in current_width_mm(28, 10, L2, 1.53).source.note
    assert "extrapolates" not in current_width_mm(2, 10, L2, 1.53).source.note, "2 A on a 1.6 mm board is on the chart"
    thin = replace(L2, stack=(Copper("F.Cu", 0.035), Dielectric("core", 0.43, 4.6, "core"), Copper("B.Cu", 0.035)))
    note = current_width_mm(2, 10, thin, 0.43).source.note
    assert "on a 0.5 mm board (the chart covers 0.72 to 2.36)" in note, note


def test_every_layer_is_sized_and_rated_on_its_own_copper():
    """Postfix B2 (refutation stackup r2): on `jlcpcb_4l_2oz` a 2 A link on In1.Cu got 0.4 mm (the floor), sized as 2 oz
    external copper; the inner copper is 0.0152 mm (0.434 oz) and IPC-2221's internal curve (k 0.024) needs 4.68 mm.
    Width and rating read the layer's own thickness and curve; the outer layers keep the stackup weight."""
    from pcbc.stackup import STACKUPS, current_width_mm, track_amps

    s = STACKUPS["jlcpcb_4l_2oz"]
    assert (s.layer_oz("F.Cu"), s.layer_oz("In1.Cu"), s.is_inner("In1.Cu"), s.is_inner("B.Cu")) == (2.0, 0.4343, True, False)
    inner = current_width_mm(2.0, 10.0, s, 1.065, "In1.Cu")
    assert (inner.value, inner.source.formula) == (4.681, "ipc2221_int"), inner
    assert "0.4343 oz In1.Cu 0.0152 mm jlcpcb_4l_2oz" in inner.source.ref, inner.source.ref
    assert track_amps(0.4, 10.0, s, 1.065, "In1.Cu") < 0.5 < 2.0 <= track_amps(0.4, 10.0, s, 1.065, "F.Cu") + 0.6
    assert current_width_mm(2.0, 10.0, s, None, "F.Cu").value == current_width_mm(2.0, 10.0, s, None).value, "an outer layer is the stackup weight"


@pytest.mark.parametrize("name", sorted(__import__("pcbc.stackup", fromlist=["STACKUPS"]).STACKUPS))
def test_the_width_a_current_gets_carries_that_current_on_every_stackup_and_layer(name: str):
    """Postfix B14 (refutation tests r2): the ampacity the gate uses (`track_amps`) was never round-tripped on the 2 oz
    stackups; rating 2 oz copper as 1 oz survived the whole suite (0.391 mm for 2 A rated 1.529 A instead of 2.001).
    Widening to `current_width_mm(I)` and asking back gives `I`, short only by the width's 3 dp rounding (half a micron
    of width more carries it; on 2 oz that rounding is worth up to 0.003 A), on every stackup, every copper layer, with
    and without a plane."""
    from pcbc.stackup import STACKUPS, current_width_mm, track_amps

    s = STACKUPS[name]
    for layer in s.copper_layers():
        for h in (None, 0.2, 1.53):
            for k in range(1, 81):
                amps = k * 0.1
                w = current_width_mm(amps, 10.0, s, h, layer).value
                got = track_amps(w + 0.0005, 10.0, s, h, layer)
                assert got >= amps - 0.0005, (name, layer, h, amps, w, got)
    if s.copper_oz == 2.0:
        w = current_width_mm(2.0, 10.0, s, 1.53 if s.layers == 2 else 0.2104, "F.Cu").value
        assert track_amps(w, 10.0, s, 1.53 if s.layers == 2 else 0.2104, "F.Cu") >= 1.999, (name, w)


IN1_4L_2OZ = '''from pcbc import *
V3 = Power("3V3"); GND = Ground("GND")
Resistor("R0", "0R", package="1206", mpn="X", lcsc="C1", p1="VIN", p2=V3)
Resistor("R1", "10", package="1206", mpn="X", lcsc="C1", p1=V3, p2=GND)
Board(width=40, height=20, layers=4, stackup="jlcpcb_4l_2oz", planes=[("GND", "In2.Cu")])
NetReq("3V3", kind="power", volts=3.3)
Path("3V3", "R0.2", "R1.1", amps=2, layers=["F.Cu", "In1.Cu"])
Place("R0", at=(5, 10))
Place("R1", at=(20, 10))
'''


def test_a_via_groups_landing_copper_has_one_home(tmp_path, monkeypatch):
    """Round 3 (refute-twooz): the compiler (`paths.make_group`) and the path gate spread a via group's current in the
    thinnest landing layer (B2), while the router (`route_cost._group_plan`), the ampacity gate (`ampacity.net_nodes`)
    and the census (`route_verify.via_parallelism`) passed the stackup's outer weight. On jlcpcb_4l_2oz an F.Cu+In1.Cu
    group is 0.434 oz to the first two and 2 oz to the rest (a 4-barrel group: 1.849 A against 2.038 A). Every home
    now asks `Stackup.landing_oz`; each call of the one arithmetic is recorded and every one is 0.4343 oz."""
    from types import SimpleNamespace

    import pcbc.stackup as S
    from pcbc.ampacity import net_nodes
    from pcbc.compile import compile_design
    from pcbc.language import load_board
    from pcbc.route_cost import Router
    from pcbc.route_emit import Sidecar
    from pcbc.route_verify import path_copper_failures

    board = tmp_path / "in1.py"
    write_split(board, IN1_4L_2OZ)
    job = compile_design(load_board(board))
    stack = job.constraints.stackup
    assert stack.landing_oz(("F.Cu", "In1.Cu")) == 0.4343 and stack.landing_oz(()) == 2.0
    (lk,) = [x for x in job.constraints.by_net("3V3").paths[0].links if not x.join]
    g = lk.group
    assert g is not None and g.count == S.via_group(2.0, g.drill_mm, stack.via_plating_mm, 10.0, g.pitch_mm, g.landing_mm, board_mm=stack.board_mm, copper_oz=0.4343)[0], g

    seen: list[float] = []
    real = S.via_group_shares

    def spy(*a, **k):
        seen.append(a[7] if len(a) > 7 else k["copper_oz"])
        return real(*a, **k)

    monkeypatch.setattr(S, "via_group_shares", spy)
    homes = {}
    # the compiler
    from pcbc.paths import make_group

    seen.clear(); make_group(job.constraints, "3V3", 2.0, 10.0, lk.via, lk.width); homes["compile"] = set(seen)
    # the router: a group plan off the lattice returns None after sizing the line once
    fake = SimpleNamespace(group=g, nc=SimpleNamespace(width_on=lambda _l: 0.0), step=0.1, scene=SimpleNamespace(stack=stack), lat=SimpleNamespace(nx=0, ny=0))
    seen.clear(); assert Router._group_plan(fake, 0, 0, "F.Cu", "In1.Cu", 0) is None; homes["router"] = set(seen)
    # the path gate: the compiled group laid on F.Cu and In1.Cu landings
    at = [(10.0 + g.pitch_mm * k, 10.0) for k in range(g.count)]
    items = [{"key": ["via", list(p), ["F.Cu", "B.Cu"]], "uuid": f"v{k}", "reason": "route_group", "net": "3V3", "owner": lk.key, "w": 0.45, "drill": g.drill_mm} for k, p in enumerate(at)]
    items += [{"key": ["seg", lay, list(at[0]), list(at[-1]), g.landing_mm], "uuid": f"s{lay}", "reason": "route_group", "net": "3V3", "owner": lk.key, "w": g.landing_mm, "drill": 0.0} for lay in ("F.Cu", "In1.Cu")]
    seen.clear(); assert path_copper_failures(job, Sidecar(items=items)) == []; homes["gate"] = set(seen)
    # the ampacity walk: a barrel of a router group, its landings on F.Cu and In1.Cu (fab._via_groups' third field)
    text = f'(via (at {at[0][0]} {at[0][1]}) (size 0.45) (drill {g.drill_mm}) (layers "F.Cu" "B.Cu") (net "3V3"))'
    from pcbc.fab import _via_groups

    groups = _via_groups(Sidecar(items=items))
    assert groups[at[0]] == (tuple(at), g.landing_mm, ("F.Cu", "In1.Cu")), groups[at[0]]
    seen.clear(); net_nodes(text, "3V3", [], dt=10.0, stack=stack, plane_h=None, groups=groups); homes["ampacity"] = set(seen)
    # the census: the same group on a board, its role items as the board's roles
    import pcbc.layout_job as LJ
    from pcbc.route_verify import via_parallelism

    vias = "".join(f'(via (at {p[0]} {p[1]}) (size 0.45) (drill {g.drill_mm}) (layers "F.Cu" "B.Cu") (net "3V3"))\n' for p in at)
    monkeypatch.setattr(LJ, "roles_doc", lambda _t: Sidecar(items=items))
    seen.clear(); (row,) = via_parallelism(vias, job.constraints); homes["census"] = set(seen)
    assert homes == {k: {0.4343} for k in ("compile", "router", "gate", "ampacity", "census")}, homes
