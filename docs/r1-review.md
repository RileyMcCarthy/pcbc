# R1 review: what eight lenses found

A review of the merged R1 (2026-09-20) ran eight independent lenses over the code, each
required to run what it claimed: an independent re-derivation of every formula against its
named reference, spec compliance per slice, live KiCad rule probes, adversarial placement
cases, end-to-end determinism against the pre-R1 build, the AI-facing report, a completeness
critic and a robustness sweep of boards a careless AI might write. Bug-grade findings went to
three skeptics each, who had to reproduce them or refute them by running the code.

Nine were confirmed by the skeptics and fixed first; a second pass then worked through the rest, and every row marked `fixed` below is fixed on main (`docs/copper-plan.md` carries the seen/now rows).
The rest are recorded here as they were reported, so the next pass starts from evidence and
not from scratch. **A row below is one reviewer's claim, not a verdict**: only the `fixed` and
`refuted` rows were adjudicated.

| status | severity | finding | file |
|---|---|---|---|
| fixed | bug | C.1 microstrip thickness form is neither Hammerstad-Jensen nor KiCad; 55.165 ohm is not 'what KiCad gives' (KiCad 10.0.6: 54.660; H&J: 53.858) | `src/pcbc/stackup.py` line 396 |
| fixed | bug | E.14 isolation creepage: KiCad resolves creepage per net pair, so the own-pads exemption is dead and every non-slot Isolation fails the gate on the isolator's own pads | `src/pcbc/dru.py` line 116 |
| fixed | bug | E.6 class creepage fires against no-net items (the fiducial mask rule areas) and a part's own pads | `src/pcbc/dru.py` line 106 |
| fixed | bug | Pair-fit clamp note on a 4-layer board says 'uncalibrated' on the fitted 7628 stack and recommends the stackup already in use | `src/pcbc/constraints.py` line 1010 |
| fixed | bug | cpwg() raises 'elliptic_k needs 0 <= k < 1; got 1.0' once tanh saturates; every CPWG width solve on the thin-prepreg 4L stackups crashes | `src/pcbc/stackup.py` line 535 |
| fixed | bug | i2c length counts the pads of every net in the NetReq, not the pads on the line (UM10204 Cb is per bus line) | `src/pcbc/constraints.py` line 1049 |
| fixed | bug | usb_hs with inner-only layers= is silently sized as an F.Cu pair and the report names F.Cu | `src/pcbc/constraints.py` line 966 |
| fixed | bug | volts= on a usb_hs NetReq is dropped from the class clearance (D: "volts on any kind") | `src/pcbc/constraints.py` line 1001 |
| fixed | bug | z_se_ohm= is accepted on kind="usb_hs" and silently ignored | `src/pcbc/constraints.py` line 1014 |
| fixed | bug | A ValueError raised while loading board.py is a raw traceback from `pcbc check`, not a line-numbered refusal | `src/pcbc/language.py` line 757 |
| fixed | bug | A single quote in a net name writes a .kicad_dru that KiCad silently drops; check passes, dru.validate says clean | `src/pcbc/dru.py` line 193 |
| fixed | bug | A single quote in class_name does the same: `A.hasNetclass('it's')` drops the rule file | `src/pcbc/constraints.py` line 1090 |
| fixed | bug | A string for volts= or amps= crashes compile with a TypeError traceback | `src/pcbc/language.py` line 424 |
| fixed | bug | A valid P/N pair written N-first, or reached through a glob, is refused with 'rename USB_DN/USB_DP' | `src/pcbc/constraints.py` line 797 |
| fixed | bug | An unreachable z_se_ohm target is reported as met at the solver's clamp: z_se_ohm=500 gives 0.0889 mm at 86.61 ohm, no note | `src/pcbc/constraints.py` line 1019 |
| fixed | bug | Board(layers=4, stackup="jlcpcb_2l_1oz") and planes= on a layer the stackup lacks are accepted; the PCB is seeded with 4 copper layers while every number was computed for 2 | `src/pcbc/language.py` line 117 |
| fixed | bug | Isolation with the same Region on both sides produces six contradictory messages instead of one refusal | `src/pcbc/constraints.py` line 1172 |
| fixed | bug | Negative numbers pass check and become KiCad rules: length (max -639mm), clearance (min -1mm), via_count (max -1) | `src/pcbc/constraints.py` line 1052 |
| fixed | bug | amps=50 on a 40 mm board is accepted as a 223.729 mm wide Power class; amps=0 and amps=-1 are accepted too | `src/pcbc/constraints.py` line 866 |
| fixed | bug | layers= naming a layer the stackup does not have crashes into `tuple.index(x): x not in tuple` | `src/pcbc/constraints.py` line 853 |
| fixed | bug | usb_hs on inner layers is silently sized as an F.Cu microstrip and reported 'on F.Cu' | `src/pcbc/constraints.py` line 966 |
| fixed | bug | volts above 1000 (NetReq or Isolation) fails with a table-range message that cites no line and says nothing to change | `src/pcbc/circuit.py` line 101 |
| fixed | deviation | A NetReq/Pair/Bus naming a net that does not exist, or a glob matching nothing, is accepted and printed as if the net were real | `src/pcbc/constraints.py` line 395 |
| reported, unverified | deviation | S4 acceptance gap: the 2L F.6 style note has no test (S4 admits it) | `src/pcbc/route_checks.py` line 1163 |
| fixed | doc | 'KiCad coupled_microstrip.cpp lands within 1 ohm of this in the fitted range' is false on the 1080 rows and on Ze | `src/pcbc/stackup.py` line 429 |
| fixed | doc | Concepts the AI needs that no doc states | `.claude/skills/pcbc/SKILL.md` line 120 |
| fixed | doc | Docs promise refusals the tool does not make | `.claude/skills/pcbc/SKILL.md` line 78 |
| reported, unverified | doc | README 'Rule \| Test' table cites a deleted test and names only files for E.3-E.15 and F.1-F.7 | `README.md` line 245 |
| reported, unverified | doc | README Rule \| Test table cites a deleted test and, for E.3-E.15 and F.1-F.7, file names instead of the tests that assert the rule | `README.md` line 245 |
| fixed | doc | README and docs/constraints.md still say buck FB-to-boot-cap is 1.84 mm; the tool prints 1.62 | `README.md` line 258 |
| fixed | doc | README cites a test that no longer exists | `README.md` line 245 |
| fixed | doc | README lists 'preset lengths' as a soft rule kind; no such rule is ever written | `README.md` line 223 |
| fixed | doc | README's node `rules:` example line does not match what the tool prints | `README.md` line 220 |
| fixed | doc | README's node report ends with a rules line that is not what the tool prints | `README.md` line 220 |
| fixed | doc | README's node report example ends in a rules line the tool never prints for node | `README.md` line 220 |
| fixed | doc | README's node report example ends with a rules line the tool never prints | `README.md` line 220 |
| fixed | doc | README/SKILL say a Chain has 'nothing between' its pads; the corridor is only checked on hops of at most 8 mm and an off-line chain pad is never a stub | `README.md` line 255 |
| fixed | doc | The report never says the IPC-2152 fit extrapolates below 0.274 A, although docs and spec say the line does | `src/pcbc/constraints.py` line 879 |
| fixed | doc | docs/constraints.md claims the CPWG-vs-microstrip fallback 'says so'; the code takes the min silently and the branch is unreachable from a NetReq | `docs/constraints.md` line 173 |
| fixed | doc | docs/constraints.md lists diff_pair_gap as a warning; it is an error | `docs/constraints.md` line 355 |
| fixed | doc | docs/constraints.md says JLC_LIMITS and vias-per-change are 'for the report' / 'counted in the copper bar'; nothing reads either | `docs/constraints.md` line 213 |
| fixed | doc | docs/constraints.md says the 1080 100-ohm pair synthesises to 0.0846 mm at gap 0.137; the tool returns 0.0889 (the fab floor) | `docs/constraints.md` line 315 |
| fixed | doc | docs/constraints.md states the t->0 stripline agreement as 0.05 % where the pinned vector measures 0.09 % | `docs/constraints.md` line 152 |
| fixed | nit | A Chain/Bus/NetReq with too few arguments is a Python traceback from `pcbc check`, not a line-cited refusal | `src/pcbc/language.py` line 756 |
| fixed | nit | A bare Pair() prints 'length none (give length_mm=)' although Pair has no length_mm kwarg | `src/pcbc/constraints.py` line 1045 |
| reported, unverified | nit | Builds are reproducible only in place: absolute paths are baked into the seed/placed/routed/fab .kicad_pcb (3D model) and report.json | `src/pcbc/seed.py` line 1 |
| fixed | nit | Bus() on a net that does not exist is compiled as a phantom constraint instead of refused (Chain/Guard refuse 'no net') | `src/pcbc/constraints.py` line 704 |
| reported, unverified | nit | Every DS2-dependent acceptance test skips in CI, including the S5-named test and the S3 KiCad rule-kind probe | `tests/test_cli.py` line 36 |
| fixed | nit | F.1 serpentine clause rounds to 'adds 0.0 mm' | `src/pcbc/route_checks.py` line 427 |
| reported, unverified | nit | F.1 whole-net outlier names locked anchors (the module, the connector) as the part to move | `src/pcbc/route_checks.py` line 372 |
| reported, unverified | nit | F.3 obstacles and F.5 'mine' pads name IC pads by number (U1.2, U1.4) and F.5 ends in 'move U1 up' for an anchor | `src/pcbc/route_checks.py` line 762 |
| reported, unverified | nit | IPC-2152 extrapolation is flagged only below 0.274 A; above 26 A and off the 0.72-2.36 mm board range it is silent, and two error paths are rough | `src/pcbc/stackup.py` line 713 |
| reported, unverified | nit | Implicit-chain suggestion puts the connector mid-chain when it is not at an end of the principal order | `src/pcbc/route_checks.py` line 565 |
| fixed | nit | Isolation(across="U7") iterates the string into parts "U" and "7" | `src/pcbc/language.py` line 519 |
| fixed | nit | Other physically meaningless values accepted without a word: layers=[], volts=-12, temp_rise_c=0 (silently clamped to 1 C but printed as 0 C), Isolation volts=0, match_mm=0, length_mm=0, loop_mm2=-3 | `src/pcbc/constraints.py` line 856 |
| fixed | nit | Pair(gap_mm=) below clearance_min is raised silently; the report shows the raised gap as the Pair's own number | `src/pcbc/constraints.py` line 964 |
| fixed | nit | Pair-gap rule is written at .2f while the report and .kicad_pro carry the 3-dp gap | `src/pcbc/dru.py` line 149 |
| fixed | nit | Pair-on-non-usb_hs refusal text points the AI at an override that is never allowed | `src/pcbc/constraints.py` line 545 |
| fixed | nit | Power layers line names In1.Cu/In2.Cu on two-layer boards | `src/pcbc/constraints.py` line 103 |
| fixed | nit | S3 acceptance gap: blinky's soft count is not pinned (spec says the four examples and DS2) | `tests/test_examples_fab.py` line 43 |
| fixed | nit | The 'records it as intent' / 'or NetReq(..., layers=[...])' alternates are refused as new lines | `src/pcbc/route_checks.py` line 1041 |
| fixed | nit | The Regions-overlap refusal is the one Isolation refusal that cites no line | `src/pcbc/constraints.py` line 1227 |
| fixed | nit | The uncontrolled-pair note gives wrong advice on a 4-layer board and mislabels a fitted stackup | `src/pcbc/constraints.py` line 1010 |
| fixed | nit | capacitance_pf_per_mm rounds to 4 dp before i2c_max_mm divides by it | `src/pcbc/stackup.py` line 873 |
| fixed | nit | iec_creepage_mm returns the 50 V row for every voltage below 50 V; IEC 60664-1 F.5 (as KiCad encodes it) has lower rows | `src/pcbc/stackup.py` line 805 |
| fixed | nit | width_usb soft hits on 4L pairs are a 0.0004 mm rounding shortfall, so the rule can never reach zero | `src/pcbc/route.py` line 244 |
| refuted | bug | 'rules: N written (E error, S soft)' has two writers that disagree on every example | `src/pcbc/build.py` line 30 |
| refuted | bug | Decap-loop note ends in the Place line that is already in board.py, claiming it 'closes' a 13.9 mm2 loop | `src/pcbc/route_checks.py` line 957 |
| refuted | bug | Decap-loop style note tells the AI to write a Place() line that is already in board.py | `src/pcbc/route_checks.py` line 960 |
| refuted | bug | E.15 corridor disallows `zone`, and pcbc's own GND pour/plane spans the whole board: every board with an Isolation fails the copper gate on the tool's own copper | `src/pcbc/dru.py` line 156 |
| refuted | bug | F.1 skew move ends in Place(to="J1.A7"), a pad number that `pcbc check` refuses | `src/pcbc/route_checks.py` line 457 |
| refuted | bug | F.2 order move names the chain's HEAD (the connector/IC anchor) when the second pad projects behind it | `src/pcbc/route_checks.py` line 517 |
| refuted | bug | F.3 fix for a two-pin part sends a decap to its OTHER net's IC pin (C_IN1 -> U1.GND) | `src/pcbc/route_checks.py` line 329 |
| refuted | bug | F.3 pinch attribution names the endpoint's own pad and a gap wider than the channel; the real blocker (a full-height keepout) is never named and the fix moves the wrong part | `src/pcbc/route_checks.py` line 806 |
| refuted | bug | F.3 treats a Keepout that allows copper (no=["via"]) as a track obstacle | `src/pcbc/route_checks.py` line 768 |
| refuted | bug | F.5 reports the pad distance even when a courtyard is closer, so the number and the keep_clear_mm relax hint are wrong | `src/pcbc/route_checks.py` line 1077 |
| refuted | bug | F.5 stays silent on a pad pair that E.4's KiCad rule errors on; the tool's suggested keep_clear_mm=1.5 can never pass the gate | `src/pcbc/route_checks.py` line 1059 |
| refuted | bug | F.7 'does not span' edit lands the part 9.4 mm off the board (parent= double offset) | `src/pcbc/route_checks.py` line 1278 |
| refuted | bug | F.7 pad-in-gap test needs the pad wholly inside the gap: an across= part's pads 1.3 mm into the corridor are silent | `src/pcbc/route_checks.py` line 1284 |
| refuted | bug | Hot-loop polygon under-reads an obviously large loop: Cin 13.6 mm from the IC passes as 13.2 mm2 | `src/pcbc/route_checks.py` line 1026 |
| refuted | bug | I2C pin count is summed over both bus lines, so the length budget halves and goes negative at 20 devices | `src/pcbc/constraints.py` line 1049 |
| refuted | bug | IPC-2221 width is floored at 0.15 mm inside ipc2221_width_mm and the report attributes the floor to the standard | `src/pcbc/stackup.py` line 670 |
| refuted | bug | Net names differing only by +/- collapse to one rule name; validate refuses the file and `pcbc build` crashes with an uncaught ValueError at place (README's own SENSE+/SENSE- example) | `src/pcbc/constraints.py` line 339 |
| refuted | bug | Place(parent=) is offset by its Region twice in the real place pipeline (S4's claim confirmed; the tool's own F.7 suggestion triggers it) | `src/pcbc/apply.py` line 84 |
| refuted | bug | Place(parent=<Region>) lands the part with the Region's offset applied twice (settle keeps parent= while writing world left/top; apply re-resolves in the Region frame) | `src/pcbc/pcb_place.py` line 354 |
| refuted | bug | The 2-layer pair-fit clamp's printed '140.1 ohm' is the coupled formula evaluated at u = g = 0.083, outside its own 0.1..10 validity, and no formula guards its u/g domain | `src/pcbc/stackup.py` line 422 |
| refuted | bug | The same board gets two different `rules:` lines: the CLI counts every warning as soft, FAB_NOTES counts only the soft kinds | `src/pcbc/build.py` line 30 |
| refuted | bug | Two writers of the `rules: N written (E error, S soft)` line disagree on S; the FAB_NOTES line does not add up and neither test can catch the split | `src/pcbc/fab.py` line 749 |
| refuted | bug | USB clearance on 2L prints a source that says 0.16 won when 0.155 did | `src/pcbc/constraints.py` line 1001 |
| refuted | bug | `pcbc check --constraints` and FAB_NOTES print two different `soft` counts for the same board | `src/pcbc/build.py` line 30 |
| refuted | deviation | E.3 'soft width' hits are not fanout stubs on buck: 29 mm of the 2 A VIN rail at 0.127 mm and 9.7 mm of 5V at 0.39 mm, invisible to the ampacity gate | `tests/test_examples_fab.py` line 29 |
| refuted | deviation | F.6 on two layers never fires for a pair: `pair.controlled` is False on 2L so the pour-hole style note is unreachable | `src/pcbc/route_checks.py` line 1122 |
| refuted | deviation | Node's 52 width_usb hits: 27 are the whole routed pair body at 0.2287 mm (0.4 um under the 0.2291 class) because route_diff gets both --track-width 0.2291 and --impedance 90; S3's 'KRT routes narrower' is a rounding-level mismatch, not a routing choice | `src/pcbc/route.py` line 246 |
| refuted | deviation | Power nets on a 2-layer board report layers In1.Cu and In2.Cu that the board does not have | `src/pcbc/constraints.py` line 103 |
| refuted | deviation | The copper bar never prints `rules: track_width 0, skew 0, uncoupled 0, vias 0` from `soft`; each slice's report assigns the line to the other | `src/pcbc/copper_bar.py` line 130 |
| refuted | deviation | The copper bar never prints the `rules: track_width 0, skew 0, uncoupled 0, vias 0` line the spec and the docs promise | `src/pcbc/netcheck.py` line 235 |
| refuted | deviation | z_se_ohm on a power net silently replaces the ampacity width (D power: C.6 then the pcbc floor) | `src/pcbc/constraints.py` line 1028 |

## The two that mattered most

**The microstrip provenance.** The physics lens compiled a harness from KiCad 10.0.6's own
`transline` source and measured it: 54.660 ohm at JLC's 7628 50 ohm width, where R1 computed
55.165 and bare Hammerstad-Jensen gives 53.858. R1's assembly put H&J's `(Z01(u1)/Z01(ur))^2`
factor into Z0 and omitted KiCad's `q_t` thickness term, so it matched neither reference it
cited. The formula is now KiCad's line for line; the per-stackup bias was refitted, so JLC's
published rows still reproduce by construction and node's pair moved 0.2291 to 0.2288 mm.

**Creepage is resolved per net pair.** The KiCad-rules lens built the isolation fixture and
found the gate failing on the optocoupler's own two pads at 0.85 mm, although the rule carried
the own-pads exemption that works for `clearance`. No item-level condition reaches `creepage`.
The nets an `across=` part carries now leave the condition instead, and
`test_dru.py::test_kicad_resolves_creepage_per_net_pair_so_the_own_pads_exemption_is_dead`
pins both directions against KiCad itself.

