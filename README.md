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

The AI never draws a track. The route stage compiles `NetReq` into an ordered KRT plan and runs it: closed pad rows (a 0.65 mm TSSOP, a QFN) are fanned out first by pcbc itself (`fanout.py`): a stub and a via just past every unconstrained pad, neighbours staggered for the fab's hole-to-hole, on the router's grid, locked, so the lane placement kept free is spent on escapes and nothing routes along it; short hops (every pad of the net within 5 mm: a header pin to its resistor, a cap to its pin) next on the still-empty board, since a hop between neighbouring pads has one path and anything routed before it can cut that path; nets that may not carry vias, or live on one layer (`kind="switch_node"`, `kind="analog"`, `vias=False`, `layers=`), next on their layers, and their copper is then locked so no later step reroutes them; differential pairs (`pair=True`) as coupled pairs; on four layers the declared `planes=` pours, then every pad on a plane net welded to its plane in its own step; then everything else, with power nets at the width their `amps` ask for on every step that may route one; on two layers a GND pour on the back and one more pass. The plan is a pure function of `board.py` (`tests/test_route_plan.py`). The gate is KiCad's own verdict plus the netlist:

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
| Two holes closer than the fab's hole-to-hole are a DRC error in pcbc's project (KiCad's default is a warning, and the gate counts errors) | `test_fanout.py::test_kicad_finds_no_copper_error_in_the_fanned_board` |
| No step passes `--clearance` but the pours (it is a ceiling on every class; the pair step's 0.16 once capped Power); on four layers the plane nets are welded to their planes in their own `plane_taps` step with the same-net keepout off, since KRT's pour places no tap vias and the keepout stops the welds | `test_route_plan.py::test_node_pours_its_planes_before_the_signals_and_asks_90_ohm` |
| Every way KRT reports an unreached pad is read (`failed_single`, `pad_pairs_open`, a pour's `unconnected pad`) and becomes a move naming the net, its parts and the pad; only constrained copper is locked, short hops stay movable | `test_route.py::test_a_net_krt_could_not_finish_is_reported_as_a_move`, `test_a_pad_krt_left_open_is_reported_whichever_field_names_it` |

What KRT taught the tool is in `docs/copper-plan.md`. The plan for pcbc's own constraint-first router, with the requirements every routing practice imposes and what KiCad 10 can enforce as rules, is `docs/router-plan.md`.

Blinky is a 40×25 mm 2-layer LED + resistor. `pcbc build` writes:

- `layout/blinky/layout.kicad_pcb` — seed
- `layout/blinky/schematic.kicad_sch`
- `layout/blinky/placed/` / `routed/`
- `layout/blinky/fab/` — Gerbers, `bom.csv`, `cpl.csv`, `FAB_NOTES.md`

This is a sibling of [pcb-space](https://github.com/RileyMcCarthy/pcb-space), which stays the Zener-era tool.
