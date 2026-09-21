# pcbc

**Python in, fab-ready KiCad out.** One AI-writable board file compiles to a KiCad 10 project and a JLCPCB zip.

Zener is not used. There is no `SOURCE.json`, no `pcb.toml`, no pin lockfile.

```
pcbc build board.py           check → seed pcb → sch → place → route → fab
```

## One fact, one file

| Fact | Lives in |
|---|---|
| Nets, MPN, LCSC, Place, board size | `board.py` — every part needs PCB `Place()` and schematic `SchPlace()` |
| Pin **names** → pad numbers + artwork | `.kicad_sym` |
| Land | `.kicad_mod` |
| IC MPN / LCSC / which CAD files | `components/…/part.py` (no `pins=`) |

Generics (`Resistor("R1", "1k", package="0402", …)`) use vendored KiCad chip lands. Footprint properties: `Value` = electrical, `Mpn` = reel, `LCSC` = JLC code.

Schematic symbols come from `.kicad_sym` **as-is** (no compact rewrite). `SchPlace` is how an AI lays them out — it only says *where*, never what connects:

```python
SchPlace("U1", parent="mcu", left=150, top=20)   # CSS: ICs / connectors
SchPlace("C1", to="U1.3V3")                      # the pin of C1 on U1.3V3's net, in line with it
SchPlace("C2", along="C1.1", side="bottom")      # hang under that node, turned to face it
SchPlace("SW1", along="C2.1", side="left")       # beside it, facing it
SchPlace("U3", to="J1.DP1", side="bottom")       # an IC parks on a face, upright
SchPlace("C3", along="C1.1", side="bottom", align="U2.EN")  # hang it so its pin sits on U2.EN's row
```

Parts keep out of each other: a part's keepout is its body plus the power symbols its pins will need, and an attached part that lands in another's keepout is shoved along its own attach axis until it clears everything (its wire just gets longer). Wires between symbols are short runs only — `SchStyle(wire_mm=25.4)` sets the sheet default, `Net("X", wire_mm=0)` a per-net cap (0 = labels only) — so a net that should be labelled never gets a wandering wire.

The tool picks the distance (room for the net's label on the wire) and the pose, the way an engineer would: a cap to ground **hangs down** from its pin's row, a pull-up **stands up** to its supply, a signal part takes the free side (up off the top pin of a side, down off the bottom one), and a series element or a 2-pin library part (an inductor, a connector) **lies along** the pin; a bigger symbol only mirrors; text stays readable either way. Parts on adjacent pins stagger by up to eight grid steps before the tool tries another pose, and a part that had to slide further than that is reported as a move. `side=` on `along=` is read relative to the parent as drawn — "bottom" off the top pin of a hanging cap means beside it (a second cap on the same node), "bottom" off the bottom pin (a divider's tap) means below. `gap=`, `rotate=`, `mirror="x"|"y"` override; `pin=` is only for hanging a pin that is *not* on the target's net.

Library `.kicad_sym` artwork is used as-is. Collision uses the real symbol (graphics, pin text, Reference/Value).

The AI never draws a wire. pcbc wires every net from `board.py`: pins of one symbol in a line share a rail (a bus bar just outside the pins when others sit between); a signal net gets a short Manhattan wire only where it crosses nothing, else labels; a supply net gets a wire only as one short straight segment, else a power symbol per pin. Wires never cross — a crossing reads as a connection — so what cannot be drawn cleanly is named instead. Every connected group is then named: a label on signal nets, a power symbol on `Power()`/`Ground()` nets. Labels, power symbols and each passive's Reference/Value then take the spot that overlaps least of what is already drawn. `pcbc build` asks `kicad-cli sch export netlist` for what KiCad actually reads and fails the `sch` stage if it is not exactly the board's netlist (`tests/test_netcheck.py` does the same). Placement changes how it looks, never what it connects.

Power symbols and labels are placed after the wires, each where it touches nothing already drawn, in three passes so drawing order does not decide who gets the clean spot; two symbols on one part (an LDO's GND between VIN and EN) are placed as a pair. What KiCad actually connects was checked with `kicad-cli`, not assumed: a wire ending part-way along another wire does not connect (no junction), a symbol pin sitting on a wire's interior does not connect, a label anchored on a wire or a pin end does. pcbc only ever draws the connecting kinds.

`pcbc sch board.py` is the inner loop: it draws the sheet, proves with `kicad-cli` that it connects exactly what `board.py` says and passes KiCad's ERC, and prints what still collides as a numbered list of moves — edit the `SchPlace()` lines, run it again. `--json` adds every part's pose. `pcbc check` catches a `SchPlace` that names a missing pin or hangs a part off a pin it shares no net with, before anything is drawn. The output is deterministic: the same `board.py` gives the same `.kicad_sch` byte for byte (ids are keyed by what they name), so the report never moves between runs and the file does not churn in git.

### Drawing rules

What the sheet is held to, and the test that pins each one (`tests/`):

| Rule | Test |
|---|---|
| The sheet connects exactly `board.py`'s netlist, as `kicad-cli` reads it | `test_netcheck.py::test_kicad_reads_the_board_netlist` |
| Only connecting geometry is drawn: no wire ends on another wire's interior, no symbol pin on a wire's interior, no T without a junction | `test_rules.py::test_only_connecting_geometry` |
| Wires never cross | `test_rules.py::test_no_two_wires_cross` |
| Wires are short runs; past `wire_mm` a net is joined by labels | `test_sch.py::test_wire_limit_per_net_and_sheet_default` |
| A supply net is wired only as one short straight segment, else a symbol per pin; ground is always symbols | `test_rules.py::test_power_is_symbols_or_one_short_wire` |
| Net names lie along their wire, on top of it, never hung beneath | `test_rules.py::test_labels_sit_on_their_wire`, `test_sch.py::test_wire_label_sits_mid_wire_not_at_the_pin` |
| A power symbol never sits on its own jog; a ground points down and a supply up unless every upright spot collides | `test_rules.py::test_symbols_upright_where_they_can_be` |
| Symbol bodies never overlap; a part keeps out of the room another's symbols need | `test_c3_usb.py::test_c3_usb_sch_no_symbol_overlap`, `test_sch.py::test_keepouts_shove_along_the_attach_axis_without_marching` |
| Attached pins line up with their target, and the tool picks the gap | `test_sch.py::test_attach_picks_the_pin_on_the_target_net`, `test_gap_defaults_to_room_for_the_label`, `test_align_lands_the_hanging_pin_on_another_pins_row` |
| 2-pin parts turn or mirror to face their node; ICs stay upright; field text stays horizontal | `test_sch.py::test_switch_mirrors_to_face_its_node`, `test_ic_stays_upright_when_attached`, `test_rotated_symbol_fields_stay_horizontal` |
| A hanger takes the engineer's pose: down to ground, up to a supply, along for a series part; a divider's tap continues below | `test_buck.py::test_hangers_take_the_engineers_pose` |
| A series resistor (its far end goes on to another part: a filter resistor off a header, a resistor before an LED) lies in line with its pin; caps between two signals still hang | `test_sch.py::test_a_series_resistor_lies_in_line_with_its_pin` |
| A part that had to slide far, or could not be placed at all, is reported as a move, and stays where the collision is | `test_buck.py::test_a_taken_lane_is_reported` |
| Off a pin that points up or down, a cap lies sideways from a short stub; a pull-up stands with its supply up, a part to ground with its ground down, or the report says so | `test_node.py::test_cap_lies_sideways_off_a_vertical_supply_pin`, `test_divider_and_pullups_stand_the_right_way_up` |
| A supply may be wired as one short straight run, or a short U tying two of one part's pins; anything longer is a symbol per pin | `test_rules.py::test_power_is_symbols_or_one_short_wire` |
| The sheet passes KiCad ERC: a pin the board leaves open carries a no-connect mark, each power net carries one `PWR_FLAG`, everything sits on the 1.27 mm grid | `test_netcheck.py::test_erc_is_clean_and_on_grid`, `test_rules.py::test_everything_on_the_grid_and_open_pins_marked` |
| The same `board.py` gives the same file, byte for byte | `test_sch.py::test_the_same_board_gives_the_same_file` |
| Everything left is reported as a move; the examples stay under the bar | `test_rules.py::test_readability_bar` |

What still collides comes back as a list — in the `sch` step of `pcbc build` (`"readability"`), from `pcbc sch`, and on the review page — phrased as moves: `wire 3V3 runs through C_MCU_HF value`, `EN: R_EN.2 and C_EN.1 are 19 mm apart but joined by labels`. The AI iterates on that list, not on a picture. Legal drawings a person might still tidy (a supply symbol that had to point down because nothing was clear above it) come back separately as `style:` notes and are not counted.

## Parts

Library files are the one thing the AI cannot write. `pcbc fetch` gets them and says how good they are:

```bash
pcbc search "TPS54202"                 # LCSC / JLC hits: id, package, stock, price, basic / extended
pcbc fetch C191884 C158012             # → components/<Mfr>/<MPN>/{part.py, .kicad_sym, .kicad_mod, .step}
pcbc score examples/node/node.py       # every part a board loads, or a part dir, or a .kicad_sym
```

`fetch` runs `easyeda2kicad` (`pip install easyeda2kicad`), upgrades the files to the current KiCad format with `kicad-cli`, drops the random ids so a second fetch is byte-identical, keeps the STEP model beside the footprint (`pcbc build` points the board at it), and writes a `part.py` whose comment lists the pin **names** — what `board.py` binds — so the AI never opens the `.kicad_sym`. A `part.py` you edited is kept unless `--force`.

The score is what pcbc needs from a library, not taste. `fail` items break the netlist or the drawing; `warn` items cost readability or ERC coverage:

| Check | Why it matters |
|---|---|
| every symbol pin number is a footprint pad (and the other way round) | a pin with no pad is silently dropped from the copper netlist |
| one unit, no repeated pin numbers, no two pins drawn on one spot | pcbc draws one unit per symbol; a pad carries one pin |
| pin ends on the 1.27 mm grid | wires can only meet pins on the grid |
| pin numbers narrower than their pins; pins ≥ 2.54 mm apart | numbers print into the body; names collide and labels have no room |
| pins named and typed | `board.py` binds by name; ERC skips `unspecified` pins |
| courtyard, body outline, 3D model, current file format | placement keeps parts apart; `pcbc build` cannot place a KiCad 5 `(module` |
| `part.py` `body_mm` matches the footprint's outline | the wrong land, or the wrong datasheet |

`good` ≥ 90, `usable` ≥ 70, else `bad` (and `pcbc score` exits 1). Anything scores — SnapEDA, the KiCad library, a hand-drawn symbol — so two candidates for one chip can be compared before `load()`-ing one. The examples' USB-C receptacle scores `bad`: its symbol numbers the joined pins `A1B12` while the footprint has `A1` and `B12`; the schematic is right, the copper would not be.

## For the AI that designs the next board

`.claude/skills/pcbc/SKILL.md` is the loop in order, the placement vocabulary, how to read each report, and what not to do. Read it before writing a `board.py`; `docs/copper-plan.md` keeps the table of what broke and what the tool does about it now.

## Install

```bash
pip install -e ".[dev]"
pcbc check examples/blinky/blinky.py
pcbc build examples/blinky/blinky.py --force
pcbc review examples/blinky/blinky.py   # schematic, copper, 3D in one HTML page
pcbc check examples/c3_usb/c3_usb.py
pcbc build examples/c3_usb/c3_usb.py --upto place --force
pcbc review examples/c3_usb/c3_usb.py
```

KiCad 10 `kicad-cli` is required for DRC and Gerbers. `pcb` (Zener) is not.

Copper is routed by [KiCadRoutingTools](https://github.com/drandyhaas/KiCadRoutingTools) (KRT), pinned to `pcbc.route.KRT_SHA`; `pcbc build` names the clone command when it is missing. Install it once (`KRT_HOME` if not in `~/Downloads`):

```bash
git clone https://github.com/drandyhaas/KiCadRoutingTools.git ~/Downloads/KiCadRoutingTools
cd ~/Downloads/KiCadRoutingTools && git checkout 3244726b2c15668fb109a0bb24384750a054af40
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt && .venv/bin/python build_router.py
```

### Copper placement

`Place()` is placed the way `SchPlace()` is: anchors by CSS, everything else by what it belongs to, and the tool picks the spot:

```python
Place("J1", edge="bottom")                    # a connector on that edge, face out, centred (left=/right= say where along it)
Place("U1", position="absolute", left=1, top=1, rotate=90)   # an anchor: CSS, as before
Place("C_MCU_HF", to="U1.3V3")                # this part's pad on that net, right outside U1, on the side the pin faces
Place("C_MCU", to="U1.3V3")                   # the next cap on the same pin goes beside the first: the first Place() written sits nearest
Place("D1", to="R_LED.2", toward="right")     # toward= overrides the side; gap= the courtyard clearance (0.2 mm)
```

`pcbc pcb board.py` is the inner loop: check, seed, place, and a numbered list of moves (`--json` adds every part's pose). Parts keep off each other's courtyards **and pads** (EasyEDA draws the body on the courtyard; pads stick out), off keepouts and the board edge; a part with no clear spot stays where the collision is and is reported.

| Rule | Test |
|---|---|
| A relation names a real part, a real pin, and a pin of the placed part on that net, or `check` says which is wrong | `test_pcb_place.py::test_validate_names_the_mistake` |
| `to=` puts the part's pad next to the pin, outside the target's keepout | `test_pcb_place.py::test_to_puts_the_pad_next_to_the_pin_outside_the_target` |
| Parts on one pin share it in file order, the first written nearest; `toward=` picks the side | `test_two_parts_on_one_pin_share_it_in_file_order`, `test_toward_overrides_the_side` |
| `edge=` is the old CSS for a connector, with the rotation picked from where its pads are | `test_edge_is_the_old_css_for_a_connector` |
| A boxed-in part is reported, never hidden | `test_a_boxed_in_part_is_reported_not_hidden` |
| Courtyards never overlap; nothing sits outside the board; the first decoupling cap on a pin is within 2.5 mm and the next within 5; a connector is on an edge, or the report says so | `test_report_reads_a_hand_placed_board` |
| The same `board.py` places the same, byte for byte | `test_the_same_board_places_the_same` |
| Relations are placed in file order and nothing else: the first `Place()` written gets the closest spot (list the small decoupling cap first) | `test_copper_rules.py::test_the_first_place_on_a_pin_gets_the_closest_spot` |
| Copper stays 0.3 mm off the board edge; a net with `NetReq(max_mm=)` has no pad farther than that from its nearest neighbour | `test_copper_rules.py::test_report_holds_copper_off_the_edge_and_nets_to_their_max_mm` |
| Turning a footprint turns its pads (KiCad stores pad angles as footprint + pad angle in a board file); the placement reads a turned footprint's pads back in its own frame | `test_copper_rules.py::test_turning_a_footprint_turns_its_pads`, `test_pcb_place.py::test_a_part_placed_on_a_closed_row_keeps_out_of_the_lane` |
| A pad row nothing can pass between (a 0.65 mm TSSOP, a 0.5 mm QFN: gap < track + 2 x clearance) is closed; each closed row keeps a fanout lane outside it sized from the stackup for two staggered via rows (`stackup.fanout_lane`: 1.29 mm on a 0.65 mm row at JLC 2L), that relations stay out of and the report guards; a decoupling cap on such a row may sit the lane further | `test_pcb_place.py::test_a_closed_pad_row_gets_a_fanout_lane`, `test_a_part_placed_on_a_closed_row_keeps_out_of_the_lane` |
| A footprint's own pads are held to the fab floor, not the net class; ringless mounting holes are repaired at fetch; touching pads fail the score; every generic has a vendored land | `test_copper_rules.py` |
| Silkscreen references are placed by the tool: beside the part, turned to fit a narrow slot, on the body of a big part away from its pads; a reference with no room re-places its part with more gap, then is reported | `test_silk.py` |
| The examples stay under the bar: c3_usb 0, buck 0, node 0, silk included | `test_c3_usb_layout_bar`, `test_node.py::test_node_layout_bar`, `test_silk.py::test_the_examples_references_all_fit` |

### Copper rules

The AI never draws a track. The route stage compiles `NetReq` into an ordered KRT plan and runs it — but pcbc writes the structured copper itself first, locked, so the router only gets what is left. Short hops go down first (`patterns/hop.py`): every two-pad net within 6 mm, or whose straight line is the route, drawn out of each pad's own exit at the class width, 0/45/90 by construction, with any right angle at a pad mitred into two 45s; a hop between neighbouring pads has one path and anything routed before it can cut that path. Then the closed pad rows (a 0.65 mm TSSOP, a QFN) are fanned out (`fanout.py`): a stub and a via just past every unconstrained pad the hops did not claim, neighbours staggered for the fab's hole-to-hole, so the lane placement kept free is spent on escapes and nothing routes along it. KRT then gets the rest: the hops it still owns (the step disappears when there are none), nets that may not carry vias, or live on one layer (`kind="switch_node"`, `kind="analog"`, `vias=False`, `layers=`), next on their layers, and their copper is then locked so no later step reroutes them; differential pairs (`pair=True`) as coupled pairs; on four layers the declared `planes=` pours; then pcbc again, welding every SMD pad on a plane net to its plane with a via of its own (`patterns/tap.py`) — one per pad, the smallest via whose current rating covers that pad's share of the net's current, never in a pad and never closer than the fab's hole-to-hole to another drill, and on a two-layer board never where the back pour will not reach it; KRT's own tap step then runs only for the nets pcbc refused a pad of, and not at all when there are none; then everything else, with power nets at the width their `amps` ask for on every step that may route one; on two layers a GND pour on the back and one more pass. The plan is a pure function of `board.py` (`tests/test_route_plan.py`). The gate is KiCad's own verdict plus the netlist:

| Rule | Test |
|---|---|
| A `fail`-grade library part (a symbol pin with no pad, several units, a KiCad 5 footprint) is refused by `pcbc check` before anything is drawn | `test_source.py::test_check_refuses_a_fail_grade_part` |
| The routed board passes `kicad-cli pcb drc` with nothing unconnected, and its pads are bound exactly as `board.py` says | `test_route.py::test_blinky_routes_clean_and_the_same_twice`, `test_placed_board_fails_the_copper_gate_as_unconnected` |
| The same `board.py` gives the same copper, byte for byte (KRT's ids are re-keyed by order) | `test_route.py::test_blinky_routes_clean_and_the_same_twice` |
| Without KRT the route stage stops and says how to get it | `test_route.py::test_route_without_krt_says_how_to_get_it` |
| The `Stackup` is the one source of the fab's limits: the project's constraints, the net classes' vias, the router's clearances and the gate all read it | `test_copper_rules.py::test_the_stackup_is_the_one_source_of_fab_limits` |
| The gate judges filled pours and saves them, so what it judged is what the fab gets; fiducials carry a keepout so no track crosses their mask opening | `test_route.py::test_blinky_routes_clean_and_the_same_twice`, `test_copper_rules.py::test_fiducials_take_free_corners_and_are_kept_clear` |
| Every route starts from a clean work dir; a killed build's step files are never inherited | `test_route.py::test_route_starts_from_a_clean_work_dir` |
| Vias keep out of same-net SMD pads on the long-net step and the pour (a via in a passive's pad wicks solder and the fab stage refuses it); the short-hop and constrained steps route without that keepout | `test_route_plan.py::test_vias_keep_out_of_same_net_pads_on_the_long_nets_and_the_pour_only` |
| A footprint with a closed pad row is fanned out before KRT runs: pcbc's own stub and via on every unconstrained pad (`fanout.py`), staggered for the fab's hole-to-hole, on the grid, locked, deterministic, clean under KiCad DRC; pairs and constrained nets keep their pads bare; a part attached to a closed-row pad is placed straight out through its lane, never beside the row | `test_fanout.py`, `test_route_plan.py::test_the_plan_has_no_fanout_step_and_starts_from_the_board_it_is_given`, `test_pcb_place.py::test_a_part_placed_on_a_closed_row_keeps_out_of_the_lane` |
| KiCad resolves `creepage` per net pair, so no item-level exemption reaches it: an `Isolation`'s rule names only the two sides' own nets and leaves out the nets the `across=` part carries (its internal barrier is its rating) | `test_dru.py::test_kicad_resolves_creepage_per_net_pair_so_the_own_pads_exemption_is_dead` |
| The impedance formulas are KiCad's own assembly, measured against a harness compiled from its source, and every derived number names the formula, the stackup and the JLC row it was fitted to | `test_stackup.py` (26 vectors), `docs/r1-review.md` |
| Two holes closer than the fab's hole-to-hole are a DRC error in pcbc's project (KiCad's default is a warning, and the gate counts errors) | `test_fanout.py::test_kicad_finds_no_copper_error_in_the_fanned_board` |
| No step passes `--clearance` but the pours (it is a ceiling on every class; the pair step's 0.16 once capped Power); on four layers the plane nets are welded to their planes in their own `plane_taps` step with the same-net keepout off, since KRT's pour places no tap vias and the keepout stops the welds | `test_route_plan.py::test_node_pours_its_planes_before_the_signals_and_asks_90_ohm` |
| The copper bar: per net the detour ratio (routed over airwire), vias, off-45 segments and segments under 0.2 mm; `pcbc build` prints the lines and the examples are held to their recorded numbers | `test_copper_bar.py`, `test_examples_fab.py` |
| pcbc writes KiCad's geometry rules as warnings (`track_segment_length`, `track_angle`) and a canary rule that must fire on every board: one malformed rule silently disables every custom rule and kicad-cli says nothing, so the gate fails when the canary is missing | `test_copper_rules.py::test_pcbc_writes_the_geometry_rules_and_a_canary_that_must_fire` |
| A routing failure names the unreached pad, the copper and pads in its corridor, the step that put each there and whose part it belongs to (blocking analysis), phrased as a move | `test_copper_bar.py::test_a_blocked_pad_is_reported_with_what_is_in_the_way_and_whose_it_is` |
| Every way KRT reports an unreached pad is read (`failed_single`, `pad_pairs_open`, a pour's `unconnected pad`) and becomes a move naming the net, its parts and the pad; only constrained copper is locked, short hops stay movable | `test_route.py::test_a_net_krt_could_not_finish_is_reported_as_a_move`, `test_a_pad_krt_left_open_is_reported_whichever_field_names_it` |
| Every obstacle and every candidate piece of copper is one shape, `hull(pts) + disc(r)`: a track is a capsule, a via is a disc, a roundrect pad is its inset corners offset by the corner radius. It is the true copper of the KiCad item or a strict superset of it — a chamfer ignored, a concave custom pad hulled — and never a subset, so clearing the model clears the truth | `test_route_geom.py` (one test per row of `docs/r2-design.md` A.1), `test_route_geom.py::test_property_superset_for_every_shape_kind` |
| A clearance decision has no square root, no trigonometry and no tolerance: `clears` compares squares, overlap is decided on integer nanometres, and every requirement carries a one-sided 0.1 um, so pcbc is always stricter than KiCad and never hands the arbiter a coin flip. `gap` takes the square root, and it is for report lines only | `test_route_geom.py::test_no_transcendental_reaches_the_accept_path`, `test_clears_is_one_sided_so_a_shape_that_only_just_fits_is_refused`, `test_property_no_false_accept` |
| A hole is read edge to edge as KiCad reads it, so two 0.3 mm drills need 0.8 mm centre to centre and an oval drill is a capsule, not the scalar that under-models the USB-C legs by 0.35 mm at each end | `test_route_geom.py::test_clears_reads_hole_to_hole_edge_to_edge_as_kicad_does`, `test_an_oval_drill_is_a_capsule_not_a_circle` |
| pcbc's own copper is 0/45/90 **by construction**, never by snapping afterwards: a corner is derived from the already-rounded endpoints in nanometres, so a diagonal through a rounded midpoint is still exactly diagonal, and a turn is legal when the octant difference wraps to one or less | `test_route_geom.py::test_turn_ok_wraps`, `test_property_a_diagonal_through_a_rounded_midpoint_stays_exactly_diagonal`, `test_octile_path_is_octilinear_by_construction_either_way_round` |
| 2000 golden shape pairs and their exact distances are replayed on every run, so the geometry core cannot drift and R3's router is a port rather than a rewrite | `test_route_geom.py::test_the_golden_vectors_replay_bit_for_bit`, `tests/fixtures/geom_vectors.json` |
| A `Shape` is its points' own hull, in hull order — not merely "every triple turns left", which a self-intersecting star also satisfies while the containment every clearance rests on is wrong by 0.68 mm; a doubled point is not a legal `Path`, and `is_octilinear`, `turn_ok` and `legs_ok` are three judges that cannot disagree about the same path | `test_route_geom.py::test_a_star_wound_point_list_is_not_a_hull_even_though_every_triple_turns_left`, `test_the_three_judges_agree_about_a_doubled_point`, `test_legs_ok_is_the_third_judge_and_refuses_exactly_what_kicad_counts` |
| A clearance answer does not depend on which argument is which, and the one number a report prints is a function of the geometry alone: the radii are summed smallest-first and subtracted as one exactly-rounded sum, and a touching pair prints `0.0` and never `-0.0` | `test_route_geom.py::test_clears_and_gap_do_not_care_which_argument_is_which`, `test_gap_never_prints_a_negative_zero_for_shapes_that_touch`, `test_property_the_module_is_deterministic` |
| A pad is the copper KiCad draws, not the `(size ...)` it declares: a USB-C shield pad is its 0.7 x 1.4 mm `gr_poly`, an oval drill is a capsule, an NPTH is a hole with no copper, a chamfer is ignored as a superset — and all 397 pads of the five boards round-trip | `test_route_scene.py` (one test per row of `docs/r2-design.md` A.1), `test_every_pad_of_every_board_is_read_and_the_census_is_the_one_e1_counted` |
| Every distance requirement comes from one table, and the table is at least every `clearance` and `creepage` rule pcbc hands KiCad — with the one rule written to *lower* a number (a footprint's own pads) named and exempted | `test_route_scene.py::test_clearance_table_covers_every_written_rule`, `test_a_synthetic_board_with_every_kind_exercises_creepage_and_isolation` |
| A candidate is judged by five rules in one place: copper, hole to copper, hole to hole (same net included), the advisory mask dam, and the board edge measured to the nominal Edge.Cuts line; the scene is sorted so `item.id` is the tie-break and the bucket index never misses what a linear scan would find | `test_route_scene.py::test_a_candidate_over_a_foreign_pad_is_refused_and_the_worst_clash_is_the_one_reported`, `test_two_items_on_one_net_skip_the_copper_rule_and_never_the_hole_rules`, `test_the_index_never_misses_anything_a_linear_scan_would_find` |
| A pattern leaves a pad by an exit, never from its centre: axis-aligned, far enough out to be copper KiCad accepts, in a fixed order, and an exit that cannot be drawn is dropped. A fanout lane may be crossed and not run along | `test_route_scene.py::test_a_pad_exit_leaves_far_enough_to_be_copper_and_in_a_fixed_order`, `test_an_exit_that_cannot_be_drawn_is_dropped`, `test_the_lanes_of_a_closed_row_are_obstacles_the_router_may_cross_and_not_run_along` |
| Which offsets a track may take is a closed form, not a search, and it never offers one `clears` refuses (swept against it at 0.01 mm); who still needs the router is computed from the copper, never declared | `test_route_scene.py::test_free_intervals_never_offers_an_offset_clears_refuses`, `test_a_placed_board_is_open_and_a_routed_one_is_not` |
| The geometry core agrees with the arbiter on real copper: every clearance, hole, edge and short error KiCad finds on the routed examples is one the checker also rejects — the four c3_usb clearance errors at exactly the millimetre KiCad prints — and around four real board pads KiCad's air is never less than pcbc's, 25 of 32 probes to the last digit | `test_route_scene.py::test_the_checker_rejects_every_copper_error_kicad_finds`, `test_route_geom.py::test_kicad_measures_the_same_air_around_a_real_pad_as_the_shape_model_does` |
| The fanout's copper goes out through the shared emitter and its footprints come from the scene, and it is byte-identical on all five boards: the new core agrees with the code that already works | `test_route_scene.py::test_the_fanout_writes_the_same_copper_through_the_new_core`, `test_fanout.py` |
| pcbc writes the short two-pad nets itself, before KRT runs and locked: a stub out of each pad's own exit, one of seven link shapes between them, the first that clears and no scoring anywhere — so the same placed board gives byte-identical copper | `test_patterns.py::test_a_link_is_one_of_b0s_seven_shapes_in_b0s_order`, `test_blinky_is_one_hop_and_nothing_is_left_over`, `test_the_board_is_a_pure_function_of_the_placed_board` |
| A hop is a hop when the pads are within 6 mm, or when the straight line between them is the route; past that, a net whose exits have no straight line between them is a route and KRT's — letting one through locked 33 mm across the DS2 Addon and left `GND` unable to reach five pads | `test_patterns.py::test_past_hop_mm_only_the_straight_line_counts`, `test_the_ds2_addon_hops_exactly_its_ten_local_two_pad_nets` |
| A hop that would walk more than 1.5x the distance between its pads refuses instead: a pattern never degrades, and copper that walks is a routing problem wearing a pattern's clothes | `test_patterns.py::test_a_hop_that_walks_is_not_a_hop` |
| Where a link meets an axis-aligned pad stub at a right angle the corner is **mitred** into two 45s, so pcbc never writes the corner its own `track_angle (min 135)` rule was written to catch | `test_patterns.py::test_a_right_angle_where_copper_leaves_a_pad_is_mitred_not_written`, `test_no_board_writes_a_corner_kicads_own_angle_rule_would_count` |
| Every refusal names the rule that decided it, the number it measured against the number it needed, what it tried, and two `board.py` edits — and a refused net gets no copper at all | `test_patterns.py::test_bucks_en_is_refused_and_the_refusal_is_a_move`, `test_a_hop_refuses_rather_than_reordering_or_narrowing` |
| pcbc's own copper is checked against the whole board by pcbc's own geometry before KiCad ever sees it, and a failure raises rather than writes; KRT is then asserted to have moved none of it, step by step | `test_patterns.py::test_the_stage_checks_its_own_copper_and_raises_rather_than_writes`, `test_examples_fab.py::test_example_builds_to_fab` |
| Why each piece of copper exists survives both rewrites the board gets: `layout/<board>/routed/copper.json` is keyed by geometry, its uuids are kept through `pin_copper_ids` and the gate, and the copper bar reports what pcbc owns against what is left over | `test_patterns.py::test_a_pieces_key_survives_the_two_rewrites_the_board_gets`, `test_examples_fab.py::test_example_builds_to_fab` |
| pcbc welds every SMD pad on a plane net to its plane itself, one via per pad, straight out of the pad on the axis at the escape's own distance — and a through-hole pad is skipped with a printed reason, because its barrel already reaches every layer | `test_patterns.py::test_every_smd_pad_on_a_plane_net_gets_a_via_of_its_own`, `test_a_through_hole_plane_pad_is_skipped_and_the_report_says_why`, `test_a_tap_leaves_its_pad_by_fanouts_own_distance_and_is_exactly_axis_aligned` |
| A tap gets the smallest via whose current rating covers **one pad's share** of the net's current, capped at the net's class via — node's 47 ground pads share 1 A, so 0.0213 A each against the 0.527 A a 0.2 mm barrel carries | `test_patterns.py::test_a_tap_via_is_the_smallest_via_that_carries_one_pads_share`, `test_a_tap_of_the_wrong_size_fails_the_self_check` |
| No tap ever lands in a pad (the fab refuses a via in a passive's pad outright, and the copper rule does not ask the question for a pad of its own net), and no two holes on the board come closer than the fab's hole-to-hole, the connector's unnumbered mounting holes included | `test_patterns.py::test_no_tap_via_sits_inside_a_pad`, `test_two_taps_keep_the_fabs_hole_to_hole_and_so_does_a_mounting_hole` |
| The taps go down between KRT's planes and its signals — the only one of four measured orderings that is both connected and better — and KRT's own tap step then runs for the nets pcbc refused a pad of, and not at all when there are none | `test_patterns.py::test_the_post_stage_runs_between_the_planes_and_the_signals`, `test_krts_tap_step_runs_only_for_the_nets_the_pattern_refused` |
| A plane judges nothing until it is filled: on two layers a tap the pour will not reach is refused before it is written, and on every board the gate's own filled copper is checked back — each plane still one island **of the area it was**, each tap via's whole **ring** inside its own net's copper, each plane present at all — and `pcbc build` asks both, not only the suite | `test_patterns.py::test_a_tap_the_pour_cannot_reach_is_refused_before_it_is_written`, `test_the_plane_a_tap_lands_in_is_still_one_island`, `test_examples_fab.py::test_example_builds_to_fab` |
| A tap never cuts the plane it does **not** join: a row of vias at a footprint's own pitch merges its antipads into a slot, so a site whose antipad comes within the zone's `min_thickness` of another hole's is refused and the row steps out of line — node's `U1` column staggers 0.30 mm and its 3V3 plane keeps a path that an 8.74 mm slot had severed | `test_patterns.py::test_a_tap_never_merges_its_antipad_into_a_neighbours_in_a_plane_it_does_not_join`, `test_a_zone_header_is_read_for_the_numbers_a_fill_will_use` |
| A via may **cross** a fanout lane and never sit in one — it has no run length to budget, so occupancy is the test — and the self-check asks the same of a via as of a segment | `test_patterns.py::test_a_via_may_not_sit_in_a_fanout_lane_at_all` |
| A via that only breaks a pad's edge is still a via in a pad: the fab stage measures the **copper the pad draws** and refuses any overlap on a passive, whatever its reference is spelled like | `test_fab_via_in_pad.py::test_a_via_that_only_breaks_a_pads_edge_is_a_hit_and_says_it_is_partial`, `test_the_blocker_list_reads_the_part_and_not_the_spelling_of_its_reference`, `test_examples_fab.py::test_example_builds_to_fab` |
| Every via is tented, front and back, because pcbc says so and not because KiCad defaults to it — a tap's ring sits at the fab's clearance from the pad it welds, so an open barrel would share that pad's mask opening | `test_seed.py::test_the_seeded_board_declares_its_own_via_tenting`, `test_examples_fab.py::test_example_builds_to_fab` |
| Where a pattern makes a neighbour worse the number is pinned, not summarised: per-net detour (a max hides every net under it), pcbc's own 135-degree corner count, how many tap stubs are in the width warnings, and how many pattern vias sit in another part's courtyard | `test_examples_fab.py::test_example_builds_to_fab`, `docs/r2-measurements.md` S5r |
| `PCBC_PATTERNS=off` restores the pre-R2 plan exactly — one env check around one call, and every copper-bar number on all five boards comes back | `test_patterns.py::test_patterns_off_claims_nothing_and_is_a_rollback`, `docs/r2-measurements.md` S5 |

What KRT taught the tool is in `docs/copper-plan.md`. The plan for pcbc's own constraint-first router, with the requirements every routing practice imposes and what KiCad 10 can enforce as rules, is `docs/router-plan.md`.

### Constraints

The AI never types a width, a clearance or a pair gap. It states intent, one line per net or group, and `constraints.py` turns the line into numbers from the stackup (`stackup.py`: JLC's own dielectrics, Hammerstad-Jensen / Wadell / Ghione-Naldi impedance, IPC-2152 with the IPC-2221 floor, IPC-2221B Table 6-1 and IEC 60664-1 for voltage). The same numbers are enforced three times with no second copy: at placement before any copper exists (`route_checks.py`), in the router's plan, and in KiCad's own rule file (`dru.py`), so the arbiter judges exactly what the router obeyed. Every formula, its reference and its calibration against JLC's published rows is in `docs/constraints.md`; the design is `docs/r1-design.md`.

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

Kinds: `generic` (`digital`, `default`), `power`, `analog`, `switch_node`, `clock`, `usb_hs`, `spi`, `i2c`, `sense`, `feedback`. A kwarg a kind does not use, an unknown kwarg (`amp=` for `amps=`), an unknown kind, a `Chain` pad off its net, a `Bus` clock outside the bus, an `Isolation` side that is not a `Region` or a part on neither side: each is a `pcbc check` failure naming the `board.py` line. Nothing is dropped silently, and a later `NetReq` never overwrites an earlier class's numbers (it gets `Power_2`, and the report says so).

`pcbc check board.py --constraints` prints, after the check line, every number a line became, one per line with its source, sorted by net; `--json` prints the same `ConstraintSet` as a document; `pcbc pcb --constraints` prints them after the moves; `pcbc build` carries them in the `check` step. Node:

```
USB_DP: pair with USB_DN, 0.2288 mm wide, gap 0.15 mm on F.Cu over In1.Cu (GND): 90 ohm (hj_coupled_microstrip x 0.85 JLC04161H-7628; JLC row 0.2332/0.15; target 90 +-15 %; NetReq line 143)
USB_DP: clearance 0.18 mm (class_floor hole_clearance 0.25 - ring 0.075 + 0.005)
USB_DP: via 0.35/0.2 mm (stackup jlcpcb_4l_1oz), at most 2 (preset usb_hs) [soft: warning in R1]
USB_DP: skew 0.5 mm (preset usb_hs; TI usb_layout_basics) [soft: warning in R1]
USB_DP: uncoupled 2 mm (preset usb_hs; TI usb_layout_basics) [soft: warning in R1]
USB_DP: length none (give length_mm=)
VBUS: width 0.4 mm (pcbc_floor amps >= 0.2; ipc2221_ext 1 A 10 C 1 oz 0.300; ipc2152_fit x board 1.099 x plane 0.430 at 0.2104 mm In1.Cu 0.134)
VBUS: clearance 0.2 mm (preset power; ipc2221_6_1 row 0-15 V B2 0.1)
VBUS: via 0.8/0.4 mm (preset power), 2 per layer change (via_barrel 0.4/0.018 mm 0.871 A at 10 C) [report only in R1]
T_DIV: width 0.2 mm (preset analog)
T_DIV: no vias (preset analog)
T_DIV: airwire 25 mm (preset analog)
T_DIV: keep_clear_of none
T_DIV: spacing 5W (preset analog)
classes: Default 0.16/0.18, USB 0.2288/0.18 pair 0.2288/0.15, Power 0.4/0.2 via 0.8/0.4, Analog 0.2/0.2 via 0.6/0.3
rules: 13 written (4 error, 9 soft), canary on net 3V3
```

A number with no standard behind it says `pcbc default`; a formula off its calibrated rows says `uncalibrated` or `formula only`; a 2-layer USB pair that cannot reach 90 ohm says so (`USB_DP/USB_DN: 90 ohm needs 0.7746 mm members at gap 0.15 on jlcpcb_2l_1oz (formula only); pair written at the fab floor 0.127/0.127 = 140.1 ohm; fine for USB full speed, use Board(stackup="jlcpcb_4l_1oz") for high speed`) and is not a failure. `[soft: warning in R1]` marks a rule KiCad checks as a warning: `track_width`, `skew`, `via_count` budgets and `diff_pair_uncoupled` (there is no preset length rule: H.6) are counted by the copper bar and pinned per example, not gated, until the router can hold them (`docs/r1-design.md` H.3). Promotion: a soft rule that hits zero on the four examples and the DS2 Addon is switched to an error in the same PR that shows the zeros.

| Rule | Test |
|---|---|
| Every formula reproduces its reference: H&J microstrip with the thickness factor (54.660 ohm at JLC's 7628 row, as KiCad), coupled H&J even/odd, Wadell stripline (and the exact conformal map at t = 0), Ghione-Naldi CPWG, IPC-2152 (chart fit, board and plane modifiers), the via barrel, IPC-2221B 6-1, IEC 60664-1 F.1/F.2/F.5, I2C length from capacitance; 26 vectors with their tolerances | `test_stackup.py::test_vector_1_hj_textbook_alumina_line` … `test_vector_26_existing_pins_unchanged` |
| The stackups are JLC's own (7628, 3313, 2116, 1080, the 2-layer core), by name and JLC code, and the fab limits are unchanged; the bias is fitted to JLC's published rows and printed on every number it touches | `test_stackup.py::test_the_five_stackups_and_their_jlc_codes`, `test_the_bias_and_the_published_rows`, `test_the_fab_limits_are_todays_and_jlcs_own_are_for_the_report` |
| A pair the stackup cannot reach is clamped to the fab floor, `controlled=False`, and the report says so instead of printing 90 ohm | `test_stackup.py::test_vector_10_the_pair_fit_clamp_on_two_layers`, `test_constraints.py::test_c3_usb_keeps_its_two_layer_pair_and_its_report_stops_lying` |
| The five boards keep today's classes, nets and router plan byte for byte; node's USB pair is the one change (0.2288 / 0.15 on JLC04161H-7628) | `test_constraints.py::test_the_five_boards_keep_todays_classes_nets_and_krt`, `test_to_dict_round_trips_identically_on_two_compiles` |
| Every kind compiles on two and four layers and each number's line is pinned with its source; node's, buck's and the DS2 Addon's lines are pinned | `test_constraints.py::test_every_kind_compiles_on_two_layers_and_its_lines_are_pinned`, `test_every_kind_compiles_on_four_layers_and_its_lines_are_pinned`, `test_node_lines_are_pinned`, `test_buck_lines_are_pinned`, `test_ds2_addon_lines_are_pinned` |
| An unknown kwarg is refused with a did-you-mean, a kind-foreign kwarg with the list it takes, an unknown kind with the known ones, each citing the line; every refusal of a `Pair`, `Bus`, `Chain`, `Isolation` line is pinned | `test_constraints.py::test_an_unknown_kwarg_is_refused_with_the_did_you_mean_line`, `test_a_kind_foreign_kwarg_and_an_unknown_kind_are_refused_citing_the_line`, `test_every_refusal_of_a4_is_pinned`, `test_chain_on_the_ds2_addon_validates_its_pads_against_the_net`, `test_isolation_refusals_name_the_part_and_the_short` |
| `volts=48` gets the 0.6 mm IPC-2221B row and no creepage rule; `volts=250` gets 1.25 mm and 2.5 mm creepage; a `Pair` overrides only its own fields and says so | `test_constraints.py::test_a_netreq_at_48_v_gets_0_6_and_no_creepage_and_250_v_gets_1_25_and_2_5`, `test_pair_bus_chain_and_guard_load_validate_and_override_never_silently` |
| A controlled pair on four layers wants its reference plane declared (`Board(planes=[("GND", "In1.Cu")])`) or `check` refuses | `test_constraints.py::test_a_controlled_pair_on_four_layers_wants_its_plane_declared` |
| `pcbc check --constraints` prints every number with its source after the check line; `--json` is the `ConstraintSet`; a refusal exits 1 and a caveat does not; `pcbc pcb --constraints` and the build's check step carry the same lines | `test_cli.py::test_check_constraints_prints_every_number_with_its_source`, `test_check_constraints_json_prints_the_constraintset_with_its_keys`, `test_check_constraints_exit_code_is_checks_and_a_caveat_is_not_a_failure`, `test_pcb_constraints_prints_the_lines_and_json_carries_the_constraintset`, `test_build_check_step_carries_the_constraint_lines` |
| E.1–E.2 The geometry rules (`track_segment_length (min 0.2mm)`, `track_angle (min 135)`, no unit) are warnings on every board | `test_copper_rules.py::test_pcbc_writes_the_geometry_rules_and_a_canary_that_must_fire`, `test_dru.py` |
| E.3 Every class wider than the fab floor gets `track_width (min)` by `hasNetclass`, a warning (soft) | `test_dru.py::test_every_row_of_e_is_pinned_on_two_layers`, `test_every_row_of_e_is_pinned_on_four_layers` |
| E.4 Every keep-away is a `clearance (min)` between the class and the other net, a footprint's own pads exempt | `test_dru.py::test_every_row_of_e_is_pinned_on_two_layers`, `test_every_row_of_e_is_pinned_on_four_layers` |
| E.5 Voltage clearance is the class row in `.kicad_pro` (`max(kind, IPC-2221B 6-1)`), never a duplicate rule | `test_constraints.py::test_a_netreq_at_48_v_gets_0_6_and_no_creepage_and_250_v_gets_1_25_and_2_5`, `test_stackup.py::test_vector_22_ipc2221_table_6_1_clearance`, `test_dru.py` |
| E.6 A class at 60 V or more gets `creepage (min)` against every other class | `test_dru.py::test_every_row_of_e_is_pinned_on_two_layers`, `test_every_row_of_e_is_pinned_on_four_layers` |
| E.7 `length_mm=` and the I2C budget become `length (max)` per net, an error; `max_mm` never does | `test_dru.py::test_every_row_of_e_is_pinned_on_two_layers`, `test_every_row_of_e_is_pinned_on_four_layers` |
| E.8 Every pair and bus gets `skew (max)` over its members, a warning (soft) | `test_dru.py::test_every_row_of_e_is_pinned_on_two_layers`, `test_every_row_of_e_is_pinned_on_four_layers` |
| E.9 Every no-via net gets `via_count (max 0)`, an error | `test_dru.py::test_every_row_of_e_is_pinned_on_two_layers`, `test_every_row_of_e_is_pinned_on_four_layers` |
| E.10 A via budget (`usb_hs`, `clock`) is `via_count (max n)`, a warning (soft) | `test_dru.py::test_every_row_of_e_is_pinned_on_two_layers`, `test_every_row_of_e_is_pinned_on_four_layers` |
| E.11 Every pair gets `diff_pair_gap (min/opt)` by `hasNetclass` | `test_dru.py::test_todays_rules_are_still_written_unchanged_and_the_examples_lists_are_pinned`, `test_dru.py` |
| E.12 Every pair gets `diff_pair_uncoupled (max)`, a warning (soft) | `test_dru.py::test_every_row_of_e_is_pinned_on_two_layers`, `test_every_row_of_e_is_pinned_on_four_layers` |
| E.13–E.14 An `Isolation` writes `clearance` and `creepage` between the two sides' nets (no creepage rule with `slot=True`: the slot is the path) | `test_dru.py::test_isolation_rows_e13_to_e15_are_pinned`, `test_kicad_resolves_creepage_per_net_pair_so_the_own_pads_exemption_is_dead` |
| E.15 An `Isolation` writes a rule area `ISO_{a}_{b}` over the corridor with `disallow track via zone`, and the zone is in the placed board | `test_constraints.py::test_isolation_sides_come_from_place_lines_and_the_corridor_is_a_rule_area`, `test_dru.py` |
| E.16 A footprint's own pads are held to the fab floor, after every clearance rule so it wins | `test_copper_rules.py::test_design_rules_exempt_a_footprints_own_pads_down_to_the_floor` |
| E.17 The canary (`length (max 0.001mm)` on one net) is last and must fire, or the gate fails; `validate` refuses `(min 135deg)`, an unknown constraint, a duplicate name before any write | `test_copper_rules.py::test_pcbc_writes_the_geometry_rules_and_a_canary_that_must_fire`, `test_dru.py` |
| E.18 `hole_to_hole` is an error in pcbc's project | `test_fanout.py::test_kicad_finds_no_copper_error_in_the_fanned_board` |
| E.19 Fiducial masks and keepouts are KiCad keepout zones | `test_copper_rules.py::test_fiducials_take_free_corners_and_are_kept_clear` |
| E: each rule kind written alone into the built DS2 board on a violating fixture, then all together, the canary firing every time; `seed` and `apply` write identical class rows and the examples' `.kicad_pro` files do not change; the gate returns `soft` and `rules` counts and the examples' counts are pinned beside the bar | `test_dru.py` (kicad-marked), `test_examples_fab.py` |
| F.1 Airwire length and skew: a net over `max_mm` or a pair/bus over `match_mm` is a move before a track exists (c3_usb spread 0.096 mm) | `test_route_checks.py::test_the_whole_nets_airwire_is_held_to_max_mm`, `test_a_moved_esd_part_skews_the_pair_and_the_move_says_how_much_serpentine`, `test_a_move_never_asks_the_ai_to_undo_its_own_floorplan` |
| F.2 Chain order, no stubs: `Chain` pads lie in order along the feed with nothing between; a third pad off the line on a `usb_hs` or `sense` net asks for the `Chain` line | `test_route_checks.py::test_ds2_chain_passes_as_placed_and_a_cap_after_the_pin_is_named`, `test_a_pad_in_the_chain_corridor_is_named`, `test_an_implicit_chain_puts_the_connector_at_an_end` |
| F.3 A wide net (>= 0.4 mm) has a channel of width plus two clearances between its pads on some outer layer, or the pinch is named (node under 3 s) | `test_route_checks.py::test_a_boxed_pad_names_the_pinch`, `test_buck_wide_nets_have_their_channels_as_placed` |
| F.4 Decap loops are a `style:` note over 6 mm2; a hot loop over its budget is a move naming the input cap or the low side (buck 3.3 mm2) | `test_route_checks.py::test_buck_hot_loop_is_measured_and_noted`, `test_a_far_input_cap_opens_the_hot_loop` |
| F.5 A keep-away is measured pad to pad and to courtyards, a footprint's own pads exempt (buck FB to the boot cap: 1.62 mm) | `test_route_checks.py::test_keep_away_from_the_switch_node_names_the_boot_cap` |
| F.6 A controlled net's pads sit over its reference plane, outside any keepout that forbids copper; on 2L the pour is the reference | `test_constraints.py::test_a_controlled_pair_on_four_layers_wants_its_plane_declared`, `test_route_checks.py` |
| F.7 An `Isolation`'s Regions are at least clearance and creepage apart along one axis, every part on its side, an `across` part spanning the gap; the rule area matches the placed gap | `test_route_checks.py::test_isolation_regions_keep_the_creepage_gap_and_parts_stay_on_their_side` |

Blinky is a 40×25 mm 2-layer LED + resistor. `pcbc build` writes:

- `layout/blinky/layout.kicad_pcb` — seed
- `layout/blinky/schematic.kicad_sch`
- `layout/blinky/placed/` / `routed/`
- `layout/blinky/fab/` — Gerbers, `bom.csv`, `cpl.csv`, `FAB_NOTES.md`

This is a sibling of [pcb-space](https://github.com/RileyMcCarthy/pcb-space), which stays the Zener-era tool.
