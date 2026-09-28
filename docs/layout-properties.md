# Layout properties: the layout language, one Python object per KiCad primitive

`layout.core.py` (a source file, next to `board.py`) and `layout.gen.py` (build
output, next to the routed board) are written in the layout language of
`pcbc.language`: `Seg`, `Arc`, `Via`, `Pour`, the sixteen drawing kinds, and
`Place`. This page is the contract between that language and KiCad 10: which
object is which primitive, which keyword is which token, what the build does
with the two files, and what was measured to hold. Regenerated from the code's
own tables (`gen.FIELDS`, `layout_prims.SCHEMAS`, `gen.CENSUS`) and a fresh
five-board build (`scratchpad/step1b/fix2/make_doc.py`); the tests in
`tests/test_layout_roundtrip.py` hold the tables to the code.

## Language goal

Anything the KiCad PCB editor can put on a board, `layout.core.py` and
`layout.gen.py` can say. **One Python object is exactly one KiCad board
primitive, field for field**: a `Seg` is a `(segment ...)`, a `Via` a
`(via ...)`, a `Pour` a `(zone ...)`, a `Place` in `layout.gen.py` a
footprint's `(at x y rot)`, `(layer)` and `(locked)`, a `Rect` a `(gr_rect ...)`,
a `Group` a `(group ...)`. No object expands into several primitives, no
primitive is split across objects, and there is no intermediate format between
them: emit is a transcription, and nothing after emit reads the router's own
model (`copper.json`).

The footprint editor is not the PCB editor. Pads, footprint graphics, and the
3D model stay in the `.kicad_mod`. `Place` only sets where that footprint sits.
Nets, `NetReq` and `Keepout` stay in `board.py`. Schematic objects stay in the
schematic.

| KiCad PCB object | Python | Step 1 |
|---|---|---|
| `footprint` `(at)` `(layer)` `(locked)` | `Place` (in `layout.gen.py`; refused in `layout.core.py`) | decompiled, emitted |
| `segment` | `Seg` | decompiled, emitted |
| `arc` (track) | `Arc` | decompiled, emitted |
| `via` (through, blind, buried, micro) | `Via` | decompiled, emitted |
| `zone` (pour or rule area) | `Pour` (`Pour(None, keepout=...)` is a rule area) | decompiled, emitted |
| `gr_line` `gr_rect` `gr_circle` `gr_arc` `gr_poly` `gr_curve` | `Line` `Rect` `Circle` `DrawArc` `Poly` `Curve` | decompiled, emitted |
| `gr_text` `gr_text_box` | `Text` `TextBox` | decompiled, emitted |
| `dimension` `group` `image` `table` `barcode` `target` `point` `generated` | `Dimension` `Group` `Image` `Table` `Barcode` `Target` `Point` `Generated` | decompiled, emitted |

Every top-level node of a board is one of these, a footprint, or a board
setting the layout language leaves to KiCad (`gen.BOARD_OWNED`: the header,
layers, setup, nets, board properties, embedded files). Anything else fails
the build with `gen.Unmapped`, as does any token inside a primitive that no
field carries.

## Pipeline (native generator, N0, 2026-09-25)

The owner's decision (`docs/direction.md`): auto layout and the auto router write Python, and KiCad
is the final step. Two sources of truth, `board.py` and `layout.core.py`; `layout.gen.py` is build
output; the board is `emit(merge(core, gen))`. **Since N0 (`docs/native-plan.md`) nothing in
generation reads or writes a KiCad board file before emit**: placement and routing read the Python
model and write `Place`/`Seg`/`Via`/`Pour` objects, and KiCad is the arbiter (DRC, fill, fab) and
never the source. The external router (KiCadRoutingTools) and the decompile-from-router path are
deleted; nothing invokes either (`tests/test_native.py`, P1 and P2).

| Stage | Reads | Writes |
|---|---|---|
| check | `board.py`, its modules and part files | nothing (refusals name `board.py` lines) |
| sch | `board.py`, its modules and part files | `layout/<name>/schematic.kicad_sch` and `schematic.inputs.json`, unchanged from step 1 (below) |
| place | `board.py`, every `.kicad_mod` it places | **in memory** (`place_native.place`): the poses, `Placement.feet` (each part's `.kicad_mod` parsed once by `foot_native.lib_foot` and posed by its `Pose`: what the router reads), the footprint board text that is emit's base (`seed.emit_pcb` from the `.kicad_mod` files, posed by `apply._apply_places`, fiducials, silk references by `legalize_silk`; the placer's own search still reads it) and the place-level layout objects — the `Edge.Cuts` outline (`gr_rect`, uuid `stable_uuid(name, "edge")`), keepouts and rule areas as `Pour(None, keepout=...)`, slots. The place-only board is rendered in memory and checked (`check.check_text`). Only `--upto place` (`pcbc pcb`) writes it: `placed/layout.gen.py`, `placed/layout.kicad_pcb` (by the same `emit_board`), its sidecars and `placed/inputs.json`. A full build writes no `placed/` and deletes one left by an earlier `--upto place`; no later stage reads it |
| clean | - | unlinks every earlier output of route and fab (`layout.gen.py`, the emitted board and its sidecars, `routed/unrouted/`, `fab/`, `inputs.json`) before anything can refuse |
| core | `layout.core.py` | nothing: loaded, filled, refused at load naming the line exactly as in step 1 (`validate_core`, `core_probe.round_trip` — the M1 probe board is the one KiCad board before emit, written and read by `core_probe` alone and exempted by module name in P1 — `layout_check`, `route_scene.clashes`) |
| route | `Placement.feet` and core copper | nothing on disk and no board text: `route_native.route_stage` builds a `route_scene.Scene` from the Python model (`build_scene(design, job, cs, feet)`: pads and lanes from the posed `.kicad_mod`s, keepouts, rule areas, the outline from `Board()`) and runs, in order, the patterns (hop, fanout, spine), the plane `Pour` objects (`route_native.plane_pours`), the taps, **the cost-field router** (`route_cost.Router`: A* on a lattice of `track_min + clearance_min`, cost = length + Σ pitch·tan(θ/2) + vias·via_mm, legality only through `route_scene`), differential pairs as one object (`route_pair.route_pair_link`), stitch, `route_relax.relax_pieces` and `prune_dangling`. Nets are routed constrained-first, then widest-first (`route_native.ORDER`). In **adoption passes** (`LOCK_PASSES` = 2): a core line the generated copper reproduces field for field is adopted, the rest are locked into the scene and the route runs again. A net the router cannot finish is left open and reported (below); nothing else is refused here |
| objects | the route's pieces | `Seg`/`Via` per piece with a `pcbc:<reason>:<owner>` group (`route_native.to_objects`; the router's own reason is `route`); an object with no uuid of its own gets `stable_uuid(board, "cu", repr(signature), n)` (`place_native.geometry_uuids`) |
| gen | the objects | `routed/layout.gen.py`, loaded back and required equal field for field to what was written |
| emit | `layout.gen.py` as loaded, `layout.core.py`, the in-memory footprint board | `routed/layout.kicad_pcb` (`layout_emit.emit_board`; `trace.mark("emit")` is the first moment a board text exists), decompiled once and required equal to `merge(core, gen)`; its `.kicad_pro`/`.kicad_dru` from the compiled job |
| gates | `routed/layout.kicad_pcb` | one fill and save, `netcheck.check_copper` (KiCad DRC and netlist), and the plane, chain (fatal for every chain), pair coupling (fatal past `PairSpec.uncoupled_mm`), return, barrel, thermal, guard, stitch and bridge gates on that board; the copper census counted on it. **KiCad's unconnected nets must be a subset of the router's unrouted nets**, or the build fails as a router bug |
| unrouted | the route's failures | `routed/unrouted/<net>-<k>.json` (a replayable problem: `python -m pcbc.route_native --replay <file> --board board.py`, which loads `layout.core.py` and routes in the build's adoption passes), and one move per missing link naming the two ends, what is in the way and whose `board.py`/`layout.core.py` line put it there. An unfinished net has **no** generated object in `layout.gen.py` (`route_native.drop_unfinished`); a closing move line says how much was dropped. The build **emits and gates the board, then fails** with `unrouted: ...`: no stamp, no fab package |
| stamp | `board.py`, `layout.core.py`, the part files, the local modules, `layout.gen.py`, the routed board and its sidecars | `layout/<name>/inputs.json` as in step 1, with `traced` recording what the stage read and ran and `tools["pcbc"]` = the hash of pcbc's own code (`trace.code_sha`), so a code change is stale from route. The route record does not ask for a placed record: the route stage reads no earlier stage's board |
| fab | `routed/layout.kicad_pcb` | unchanged from step 1 (below); the KRT-specific DRC exemptions (`dangling`, `diff_pair_gap`) are gone |
| review | the freshest stage whose stamp matches | unchanged from step 1 |

Defaults the native generator changed (reversible):

- **`NetReq(autoroute=False)` means constrained, not "skip"**: every net is routed, and a net with
  `autoroute=False`, `vias=False`, `layers=` or a special `kind` goes first.
- **`Board(net_order=...)` is refused** at the board line: the order is the router's
  (`route_native.ORDER`, constrained first then widest).
- **A router uuid is derived from the object's geometry** (`geometry_uuids`), so locking one object
  renumbers nothing else; there are no router ids to re-key.
- **A core `Pour` on a plane target's (net, layer) is that plane**: the generated plane `Pour` is
  replaced by it after the adoption check.
- **A core rule area covering an unrouted pad is one sentence**:
  `layout.core.py:N (rule area 'x') covers R1.2: clear it off that pad or delete it`.
- **A via the router cannot join on its second layer is not written** (`prune_dangling`); KiCad's
  `via_dangling`/`track_dangling` is a failure, never swept after the fact.

The table below is step 1's (2026-09-24). Its sch, fab and review rows still hold; its seed, route,
normalise and gen rows describe the deleted router path and are kept as the record of what N0
replaced.

## Pipeline (step 1; seed/route/normalise superseded by the native generator 2026-09-25)

The owner's decision: auto layout and the auto router write Python, and KiCad
is the final step. Two sources of truth, `board.py` and `layout.core.py`;
everything else is build output and is never edited or read back as input.
KiCad is the backend and the independent arbiter.

| Stage | Reads | Writes |
|---|---|---|
| seed | `board.py` | `layout/<name>/seed/layout.kicad_pcb`: every part on a grid, no copper, in its own directory one level under `layout/<name>/` like every other board the build writes (never beside the shipped one); a fetched part's `(model ...)` path is written relative to the board file (`../../../components/...`), which is right on every later copy; a footprint's zero-length and repeated `fp_line`s are dropped from the copy on the board (`seed.drop_degenerate_lines`, fifth review); `seed/layout.kicad_pro` from the compiled job (`seed.emit_pro`) **and nothing else beside the seed board** — a `.kicad_dru` or `.kicad_prl` lying in `seed/` is unlinked (`build.seed_stage`, fifth review), so the stamp records only what the stage wrote; and `seed/inputs.json`, the hashes of `board.py`, its modules and part files **and of the seed board and its sidecar** (fourth review, C1) |
| sch | `board.py`, its modules and part files | `layout/<name>/schematic.kicad_sch`, read back by KiCad (netlist, ERC), and **its own record** `layout/<name>/schematic.inputs.json` (`build.sch_stage`, `_stamp_sch`): the `board.py`/module/part hashes it was drawn from, the schematic's own hash and **what the stage was seen to read** (M2, `traced`), written only when KiCad's netlist and ERC passed. A `schematic.kicad_pro`/`.kicad_prl` beside the sheet is unlinked before it is drawn (seventh round: `kicad-cli sch erc` reads the `.kicad_pro`'s severities); one that appears later is beside a file `kicad-cli` was handed and is stale from seed (M2). A sheet KiCad cannot read is a refusal naming KiCad's message, never a traceback. `pcbc build` and `pcbc sch` share the stage; `pcbc pcb` (check, seed, place) never writes the record, so it never vouches for a schematic it did not draw (fifth review) |
| place | `board.py`, every module it imports from its own directory (`from helper import LEFT`: `language._local_modules`, recorded on `Design.modules`), every part file it loads (`part.py`, `.kicad_sym`, `.kicad_mod`), and the seed board | `layout/<name>/placed/layout.kicad_pcb` with the fiducials and **the silkscreen references, placed here and nowhere else** (fab writes none, review plots them as they are), its `.kicad_pro`/`.kicad_dru` **regenerated from the compiled job** (`project.write_sidecars`, never `apply`'s merge into the seed's copy), and `placed/inputs.json`: the hashes of those files, of the seed board it placed from, **and of the placed board and its sidecars**, written only when the place succeeded (its old record is unlinked first) |
| clean | - | unlinks every earlier output of route and fab (`core.kicad_pcb` and its `.kicad_pro`/`.kicad_dru`/`.kicad_prl`, `router.kicad_pcb`, `copper.json`, `copper.consumed.json`, `layout.gen.py`, the routed board, `fab/`, `inputs.json`) before anything can refuse |
| core | `layout.core.py` (next to `board.py`), the placed board's layer table and uuids | `layout/<name>/routed/core.kicad_pcb`: the placed board plus the core drawings **on copper** (a drawing anywhere else is not routing input and never reaches the router's board; normalise puts every core drawing back) and **the core copper the first route pass did not reproduce**, `(locked yes)` (see route); only when there is something to put on it. **A core line is refused at load naming the line, or KiCad keeps it as written** (fifth review, C3; the lists under "Fifth review"): every yes/no field is `True`/`False`; every word-valued field — a dict field's included — is one of KiCad's words (`layout_prims.WORDS`, the one source); every layer any field names is on the board's layer table (`layout_job.validate_core`); a value KiCad's save spells another way is read the way KiCad keeps it (a length at 1 nm, a double to ten digits, `justify` in KiCad's order, a drawing's `layers` as (copper, its mask), a via's missing side as `none`, a pour's `*.Cu` spelled out, ...) and a value KiCad's save rewrites or drops is refused; every uuid names one object and none is the placed board's. **Then KiCad reads every core line (M1, `core_probe.round_trip`, seventh round): the stamped core objects go on one probe board (the placed board's header, layer stack, setup and net table; no footprint, no other layout primitive), `kicad-cli pcb upgrade --force` loads and saves it, and every object is compared field for field; any difference is a refusal naming `layout.core.py:N`, the field, the value written and the value KiCad kept ("write that"), and a probe KiCad cannot load is bisected, one probe board per half, to the lines it refuses, named with KiCad's own message. The explicit checks above stay as fast early refusals; the round trip is the authority.** Every core copper object names a net of `board.py` (a rule area is the one no-net zone); a core drawing on `Edge.Cuts` is refused; the fields the build filled in on a hand-written line are recorded in the route step's `core_filled` |
| route | the placed board (pass 1), then that board (later passes) | `route.route_job`, in **passes** (`layout_job.LOCK_PASSES`, fourth review C4): pass 1 routes with no core copper on the board; every core line **equal in every KiCad field but its id and uuid** to a piece the KiCad-saved router board carries (`layout_job.adopt_key`; the signature finds the candidates) is **adopted** — a piece the build draws anyway, kept with its role group, uuid and id (fifth review: adoption by signature alone let a pour with the build's outline and its own clearance, connect mode and thermal gap ship them); the lines not reproduced are locked onto `core.kicad_pcb` for pass 2, and after `LOCK_PASSES` passes every core line is locked (the pre-review behaviour). The step's `lock_passes` records each pass, `adopted` and `locked` the outcome. A locked core pour (a `Pour` on the plane net) is taken **off** the board the patterns and every KRT step before the pour step see (`route.take_locked_pours`) and put back on the board the pour step reads (`pours_in_<step>.kicad_pcb`), which then runs with KRT's `--skip-existing-zones` and keeps it as written. **Every KRT step reads the compiled job's `.kicad_pro`/`.kicad_dru` beside its input**, written there just before the step runs (`project.write_sidecars`; the input is first copied into `routed/` when it is another stage's board), so no KRT step routes to KRT's stock rules or to the floors an earlier step lowered (fifth review). Every pour step says `--board-edge-clearance <stackup.edge_clearance>`. A KRT step past `PCBC_KRT_TIMEOUT` (default 1800 s) is stopped and the route refused naming the step. `routed/router.kicad_pcb`, the step files, `copper.json` |
| normalise | `routed/router.kicad_pcb`, `copper.json` (the one reader of it; renamed `copper.consumed.json` the moment it has been read, so nothing later can) | the same file: saved by KiCad, **every track or via KiCad reports dangling that is the router's own removed and KiCad asked again until it reports none** (`layout_job._drop_dangling`, recorded in `dangling_removed`; a core object is never removed, `build.py` refuses it with a move; a `pcbc:guard` piece — a shield, which joins nothing by design — is never swept either, `NEVER_SWEPT`: the guard pattern ends every shield on its vias, and one KiCad still calls dangling is refused as a pcbc bug), a core line whose uuid is the uuid of a piece pcbc's patterns wrote (read off `copper.json`, before KiCad's save can re-key a duplicate) refused naming the line and the piece, core copies replaced by the core objects, router uuids re-keyed from geometry, each pcbc piece's role written as a KiCad `group` `pcbc:<reason>:<owner>` (a core group naming a piece a pattern owns is refused naming both groups), saved by KiCad again (`kicad-cli pcb drc --refill-zones --save-board`); its DRC by type is `router_drc` |
| gen | `routed/router.kicad_pcb` | `routed/layout.gen.py`: a `Place` per footprint and one line per copper primitive and drawing that is not core (the outline and the role groups included), every field explicit; the build JSON names it as `gen` |
| emit | `routed/layout.gen.py` (loaded back, and required equal to what was written), `layout.core.py`, the placed board | `routed/layout.kicad_pcb`: the placed board with every layout primitive stripped (`strip_layout`), every footprint posed from its `Place`, and `merge(core, gen)` rendered; decompiled once more and required equal to that Python; its `.kicad_pro`/`.kicad_dru` from the compiled job |
| gates | `routed/layout.kicad_pcb` | one fill and save first (`netcheck.kicad_drc`, refill + save: KiCad's first fill of empty zones is not always its fill of a filled board — measured on `tests/fixtures/planes`, 591 vs 590 vertices in one zone — so the gates judge the fixed point, M3), then `netcheck.check_copper` (KiCad DRC + netlist, refill + save; every net token on a segment, arc, via or zone must be a net of `board.py`), uuid pinning, **the saved board decompiled and required equal to the Python, its DRC by type required equal to `router_drc`, and its footprints and every other non-layout node required equal to the router's board's (uuids masked, `build.non_layout_diff`)**; every DRC item KiCad reports on a core object names its `layout.core.py` line (an error is a failure, a warning a note in `core_drc_notes`, a `via_dangling`/`track_dangling` a failure with a move); `_plane_gate` (the ring, the islands, and each `pcbc:tap:<pad>` via joined to that pad by tracks, `build.tap_stubs`), `_chain_gate`, `_return_gate`, `_barrel_gate`, `_thermal_gate`, `_guard_gate`, `_stitch_gate`, `_bridge_gate`, their pieces and roles read off the board's `pcbc:` groups (`layout_job.roles_doc`); the copper census `copper_bar` is counted on this board too (`build.emitted_copper_bar`; the router's count is kept as `copper_bar_router`), so it never names copper the dangling sweep took off |
| stamp | `board.py`, `layout.core.py`, the part files, the local modules, the placed board, `layout.gen.py`, the routed board and its sidecars | `layout/<name>/inputs.json`: the hashes of what the route consumed, of the two files it wrote, and of the `.kicad_pro`/`.kicad_dru` KiCad's DRC read beside the routed board and the `.kicad_prl` KiCad wrote. **Every file a stage reads that the build wrote is stamped by content beside it, and any mismatch is stale from the stage that wrote the file, worded "build output, never edited"** (`build.stale_reason`, C1): stale from **check** (re-seed) when `seed/inputs.json` is missing, its `board.py`/module/part hashes differ, or the seed board or its sidecar is not the bytes the seed stage wrote; from **seed** (the sch stage runs) when `schematic.inputs.json` is missing, was drawn from another `board.py`, or `schematic.kicad_sch` is not its bytes; from **sch** (place runs) when `placed/inputs.json` is missing or differs, or the placed board or a placed sidecar is not the bytes the place stage wrote (fifth review: it was stale from check, which re-seeded and redrew the schematic for nothing); from **place** when this record is missing, a consumed file changed, or `layout.gen.py` / `routed/layout.kicad_pcb` / a routed sidecar are not the bytes the route wrote; from **route** (fab runs) when `fab/inputs.json` is missing or any file under `fab/` but that record itself (by its path) is not the bytes the fab stage wrote; the build JSON says why in `stale`. **Every record also carries what its stage was seen to read (M2, `trace`, seventh round)**: `reads` (every file loading `board.py` opened, by content: `Design.reads`) and `traced` — every file the stage's Python opened for reading that it did not write (a helper module `layout.core.py` imports, a data file it opens, KRT's `VERSION`, the boards earlier stages wrote), every directory and argv handed to `kicad-cli` or KRT with the files that were already there (inputs, by content) and the ones the stage wrote (outputs, by name), every `PCBC_*`/`KRT_*`/`KICAD*`/`SOURCE_DATE_EPOCH` variable the stage read (and the `KRT_*`/`KICAD*` ones a tool inherited), the `kicad-cli` version string, and KRT's pinned sha with the sha its install is at. A difference in any of them is stale from the stage that read it, named |
| fab | `routed/layout.kicad_pcb` | `layout/<name>/fab/`, **cleared first** on every fab run (fifth review: a hand-added Gerber survived a fab-only rerun and was stamped as fab's own). **Fab writes nothing onto the board but what `fab.FAB_WRITES` names**: fiducials only when the routed board has none (the place stage inserts them on every board measured, so fab inserts none; `insert_fiducials` reports only what it appended), **no silk** (the references are the place stage's), and every uuid the routed board carries kept as it is (`pin_all_uuids` with `keep`; only an item the routed board lacks is pinned by position). **Fab is not a second judge of the board: it is held to the first, node for node, in order, uuids included.** After its own DRC and save, the fab board must equal the expected board (`fab.expected_fab_board`: the routed board, plus any fiducial fab appended as KiCad saves it) as a multiset of every top-level node, each compared **in order** (`fab._canon_node`: atoms where they stand, children in file order; fifth review BLOCKER — the sorted canonicaliser equalled a pad's `(size 1 1.45)` with `(size 1.45 1)`) — segments, arcs, vias, zones and `pcbc:` groups also by uuid and field (`fab.fab_copper_diff`), footprints with their pads, the outline, `general`, `paper`, `title_block`, `layers`, `setup` (tenting included), `property`, `net`, `embedded_fonts`; a zone without its `filled_polygon`, and its fill **exactly, vertex for vertex** (M3, seventh round; it was (island count, area) to a 0.01 mm2 tolerance, and an area-neutral `.kicad_dru` edit inside fab moved a GND pour to 0.0105 mm from a 5V pad) — or fab refuses naming the node or the zone; a uuid any two items carry is refused on either board. Its `.kicad_pro`/`.kicad_dru` come from the compiled job and **must still be those bytes after fab's DRC and refill and again after its export** (`fab.sidecars_not_compiled`, M3), or fab refuses naming the file; KiCad's `unconnected_items` on the fab board is an error; each export must exit 0 and every requested layer's Gerber, the job file and both drill files must exist (`fab.export_missing`); the ampacity gate reads the widths pcbc's pieces have **on that board** (`fab.board_roles`). `bom.csv` is written only after the board passed; a refused fab leaves no BOM, CPL, position file, Gerber or drill file, and its board as `refused.kicad_pcb` (`fab._clear_refused`). Every wall-clock date KiCad writes into the Gerbers, the job file, the drill files, the drill maps and `drc.json` is rewritten to `SOURCE_DATE_EPOCH`, or 1980-01-01T00:00:00Z when unset (`fab.pin_dates`), so the package is the same bytes on every build. `fab/inputs.json` stamps the routed board it read and every file it wrote; `report.json` paths are relative to `layout/<name>/` |
| review | the freshest stage whose stamp matches (`build.stale_reason`) | `layout/<name>/review/`: the plots of that stage's board as it is, `review/schematic.kicad_sch` (review's own drawing, never over the build's), and the notes; when a later stage is stale its verdicts (the copper note, the BOM) are replaced by the stale reason (fifth review: review read a hand-edited fab board and BOM and called them "KiCad DRC clean") |

Defaults chosen for step 1 (reversible):

- **`Place()` lives in `board.py` only.** A `Place` in `layout.core.py` is
  refused with a message naming the line and `board.py`. So is every other
  `board.py` object (`NetReq`, `Keepout`, `Region`, a net, a part), however it
  was imported (`from pcbc import Place` included): a layout file that changes
  `board.py`'s part of the design is refused. A rule area in a layout file is
  `Pour(None, keepout=...)`. In `layout.gen.py` a `Place` is the generator's
  output, a pose.
- **Copper and drawings live in `layout.core.py` only.** A `Seg`, `Via`,
  `Line`, ... in `board.py` is refused naming its line.
- **`layout.gen.py` is written in the build directory**, beside the routed
  board, not beside `board.py`. `layout.core.py` is a source file and sits
  beside `board.py`.
- **A core object is locked for the router whatever its `locked` field
  says**: copper is rendered `(locked yes)` onto the board the router starts
  from. The emitted board carries the field as the core line wrote it.
- **Core and gen are held to one standard, the board's hard minimums**: the
  numbers pcbc compiles into the `.kicad_pro` design rules and KiCad's DRC
  fails a board on (`stackup.board_rules`: minimum track, via diameter,
  through-hole diameter, clearance). A core object below one is refused naming
  its line; a gen object below one is a note (KiCad fails the build on it
  anyway). A net's class numbers (its compiled width, which pcbc writes as a
  soft KiCad rule, and its netclass via) are targets: an object under them is
  a note for core and gen alike. So every line of `layout.gen.py` can be
  copied into `layout.core.py` verbatim.
- **Core copper is refused before routing** when it overlaps, or comes closer
  than the compiled clearance to, a pad, hole, copper or edge of another net,
  by the router's own scene (`route_scene.clashes`). A net the router then
  leaves unrouted names any core object lying across its pads, and a KiCad
  DRC failure on a core object names its line (KiCad reports the uuid).
- **Omitted fields of a hand-written core line** are filled before emit:
  track width and via size/drill from the net's compiled constraint, a pour's
  clearance from the net's class and its minimum thickness from the board's
  minimum track, its other numbers from KiCad's defaults for a new zone, and
  `filled` as KiCad's refill leaves it. A value that is present is never
  widened.
- **A router uuid is derived from the object's geometry.** The router keys its
  own ids by file position (`route.pin_copper_ids`, unchanged); normalise
  replaces them, so locking one object renumbers nothing else. pcbc's own
  pieces keep the geometry-derived uuids `copper.json` names.
- **A `Place` may move a footprint, never turn or flip it**: a footprint's pads
  carry absolute angles and a flip mirrors all of its geometry, so a pose whose
  rotation or side differs from the placed board is refused, not half-written.
  A move moves the footprint's own zones with it, as KiCad's editor does.
- **`layout_check` is a property check** (core legality, per-layer
  connectivity notes) and never says "verified"; only `check_copper` does.
- **KiCad is required to read the router's board back.** Without `kicad-cli`
  the route stage stops with an error; HEAD passed that case as
  `copper: unchecked`.
- **A pcbc piece's role is a KiCad group.** One group per (reason, owner),
  named `pcbc:<reason>:<owner>`; the gates read it off the emitted board. In
  KiCad's editor, clicking a grouped track selects its group (double-click
  enters it). A core group may hold only core objects, and may not use the
  `pcbc:` prefix. The gen header says so: a `Place`, a `pcbc:` group and the
  `Edge.Cuts` outline (`Board(width, height)` in `board.py`) are the three kinds
  of gen line that cannot be locked (the outline was missing from the header
  until the seventh round).
- **Core copper names a net of `board.py`, or no net.** `Seg("PHANTOM", ...)`
  is refused at load naming the line; `Seg(None, ...)`, which used to become
  `(net "None")`, is KiCad's own no-net copper — `(net "")` on a track or via,
  no `(net)` token on a zone — for a floating spreader, a coupon, a rule area
  (`docs/direction.md` §2). The copper gate fails on any primitive whose named
  net `board.py` does not have (KiCad's own check compares pad bindings and
  cannot see a track on a net no pad has).
- **The outline has one home, `Board(width, height)`.** A core drawing on
  `Edge.Cuts` is refused naming `board.py` (default for this round, reversible):
  the place stage reads the board rectangle and never a core cutout.
- **A core `Pour` on the plane net is the plane.** KRT's pour step
  (`route_planes.py`) does not keep an input zone — it deletes every zone of
  the net and writes its own — so the plan leaves that `(net, layer)` out of
  the step (`route.locked_pours`) and the taps, exclusions and gates see the
  net as a plane net as before. Measured: every copper line of buck's and
  c3_usb's `layout.gen.py` copied into `layout.core.py` builds to fab verified.
- **A core id may not take a gen object's id.** A core line copied from gen
  keeps its id with the uuid it was copied with (the gen copy is dropped); a
  hand-written id that equals the id gen would give to different copper is
  refused naming the line, never renamed around.
- **A core rule area over an unrouted pad is one sentence**:
  `layout.core.py:N covers R1.1: clear it off that pad or delete it`; the
  router's own move list is kept in `router_error`, not in the error.
- **A tap via belongs to its pad.** `pcbc:tap:<REF.PAD>` names the pad; the via
  must be joined to it by tracks and vias alone (`build.tap_stubs`), not only
  sit inside the plane.
- **A core load may not add a pose by any path** (`_gen_place`, a `Pose`
  appended to the design): refused naming the line, like `Place()` itself.
- **A pour's connect mode is honoured by the connectivity note**:
  `connect="none"` joins no pad, `thru_hole_only` no SMD pad.

## One object, one primitive: the field tables

Every keyword `layout.gen.py` writes and the KiCad token it is. A field at the
value in the "written when" column is not written by KiCad and not written by
the emitter; `layout.gen.py` still writes it explicitly.

### Footprint poses and copper

`gen.FIELDS`, which the writer walks: the attribute of `model.Copper` /
`model.Pose` each keyword lands in, and its KiCad token.

| Python | keyword | model attribute | KiCad token |
|---|---|---|---|
| `Place` | (positional) | `ref` | footprint (property "Reference" ...) |
| `Place` | `at` | `at` | footprint (at x y) |
| `Place` | `rot` | `rot` | footprint (at x y rot) (omitted when 0) |
| `Place` | `layer` | `layer` | footprint (layer) |
| `Place` | `locked` | `locked` | footprint (locked yes) |
| `Seg` | (positional) | `net` | segment (net) |
| `Seg` | (positional) | `a` | segment (start x y) |
| `Seg` | (positional) | `b` | segment (end x y) |
| `Seg` | `width` | `width` | segment (width) |
| `Seg` | `layer` | `layer` | segment (layer) / first of (layers cu mask) |
| `Seg` | `solder_mask` | `solder_mask` | segment (layers cu "F.Mask"|"B.Mask") |
| `Seg` | `solder_mask_margin` | `solder_mask_margin` | segment (solder_mask_margin) |
| `Seg` | `locked` | `locked` | segment (locked yes) |
| `Seg` | `uuid` | `uuid` | segment (uuid) |
| `Seg` | `id` | `id` | - (pcbc handle) |
| `Arc` | (positional) | `net` | arc (net) |
| `Arc` | (positional) | `a` | arc (start x y) |
| `Arc` | (positional) | `mid` | arc (mid x y) |
| `Arc` | (positional) | `b` | arc (end x y) |
| `Arc` | `width` | `width` | arc (width) |
| `Arc` | `layer` | `layer` | arc (layer) / first of (layers cu mask) |
| `Arc` | `solder_mask` | `solder_mask` | arc (layers cu "F.Mask"|"B.Mask") |
| `Arc` | `solder_mask_margin` | `solder_mask_margin` | arc (solder_mask_margin) |
| `Arc` | `locked` | `locked` | arc (locked yes) |
| `Arc` | `uuid` | `uuid` | arc (uuid) |
| `Arc` | `id` | `id` | - (pcbc handle) |
| `Via` | (positional) | `net` | via (net) |
| `Via` | (positional) | `at` | via (at x y) |
| `Via` | `size` | `size` | via (size) |
| `Via` | `drill` | `drill` | via (drill) |
| `Via` | `layers` | `layers` | via (layers a b) |
| `Via` | `kind` | `via_type` | via atom: (via blind|buried|micro ...), none = through |
| `Via` | `locked` | `locked` | via (locked yes) |
| `Via` | `free` | `free` | via (free yes) |
| `Via` | `remove_unused_layers` | `remove_unused_layers` | via (remove_unused_layers yes) |
| `Via` | `keep_end_layers` | `keep_end_layers` | via (keep_end_layers yes) |
| `Via` | `zone_layer_connections` | `zone_layer_connections` | via (zone_layer_connections ...) |
| `Via` | `tenting` | `tenting` | via (tenting (front) (back)) |
| `Via` | `covering` | `covering` | via (covering (front) (back)) |
| `Via` | `plugging` | `plugging` | via (plugging (front) (back)) |
| `Via` | `capping` | `capping` | via (capping yes|no) |
| `Via` | `filling` | `filling` | via (filling yes|no) |
| `Via` | `padstack` | `padstack` | via (padstack (mode) (layer name (size)) ...) |
| `Via` | `backdrill` | `backdrill` | via (backdrill (size) (layers a b)) |
| `Via` | `tertiary_drill` | `tertiary_drill` | via (tertiary_drill (size) (layers a b)) |
| `Via` | `front_post_machining` | `front_post_machining` | via (front_post_machining counterbore|countersink (size) (depth) (angle)) |
| `Via` | `back_post_machining` | `back_post_machining` | via (back_post_machining counterbore|countersink (size) (depth) (angle)) |
| `Via` | `teardrops` | `teardrops` | via (teardrops (best_length_ratio) ... (prefer_zone_connections)) |
| `Via` | `uuid` | `uuid` | via (uuid) |
| `Via` | `id` | `id` | - (pcbc handle) |
| `Pour` | (positional) | `net` | zone (net); None = no (net) token |
| `Pour` | `layer` | `layer` | zone (layer) |
| `Pour` | `layers` | `layers` | zone (layers ...) |
| `Pour` | `name` | `name` | zone (name) |
| `Pour` | `priority` | `priority` | zone (priority) |
| `Pour` | `locked` | `locked` | zone (locked yes) |
| `Pour` | `hatch` | `hatch` | zone (hatch style pitch) |
| `Pour` | `connect` | `connect` | zone (connect_pads [yes|no|thru_hole_only] ...): thermal|solid|none|thru_hole_only |
| `Pour` | `clearance` | `clearance` | zone (connect_pads (clearance)) |
| `Pour` | `min_thickness` | `min_thickness` | zone (min_thickness) |
| `Pour` | `filled_areas_thickness` | `filled_areas_thickness` | zone (filled_areas_thickness yes|no) |
| `Pour` | `teardrop_type` | `teardrop_type` | zone (attr (teardrop (type))) |
| `Pour` | `keepout` | `keepout` | zone (keepout (tracks) (vias) (pads) (copperpour) (footprints)) |
| `Pour` | `placement` | `placement` | zone (placement (enabled) (sheetname|component_class|group)) |
| `Pour` | `filled` | `filled` | zone (fill yes ...) |
| `Pour` | `fill_mode` | `fill_mode` | zone (fill (mode hatch)) |
| `Pour` | `thermal_gap` | `thermal_gap` | zone (fill (thermal_gap)) |
| `Pour` | `thermal_bridge_width` | `thermal_bridge_width` | zone (fill (thermal_bridge_width)) |
| `Pour` | `smoothing` | `smoothing` | zone (fill (smoothing)) |
| `Pour` | `radius` | `radius` | zone (fill (radius)) |
| `Pour` | `island_removal_mode` | `island_removal_mode` | zone (fill (island_removal_mode)) |
| `Pour` | `island_area_min` | `island_area_min` | zone (fill (island_area_min)) |
| `Pour` | `hatch_thickness` | `hatch_thickness` | zone (fill (hatch_thickness)) |
| `Pour` | `hatch_gap` | `hatch_gap` | zone (fill (hatch_gap)) |
| `Pour` | `hatch_orientation` | `hatch_orientation` | zone (fill (hatch_orientation)) |
| `Pour` | `hatch_smoothing_level` | `hatch_smoothing_level` | zone (fill (hatch_smoothing_level)) |
| `Pour` | `hatch_smoothing_value` | `hatch_smoothing_value` | zone (fill (hatch_smoothing_value)) |
| `Pour` | `hatch_border_algorithm` | `hatch_border_algorithm` | zone (fill (hatch_border_algorithm)) |
| `Pour` | `hatch_min_hole_area` | `hatch_min_hole_area` | zone (fill (hatch_min_hole_area)) |
| `Pour` | `hatch_position` | `hatch_position` | zone (property (layer "X") (hatch_position (xy x y))), one per layer: {"X": (x, y)} |
| `Pour` | `points` | `points` | zone first (polygon (pts (xy) | (arc (start) (mid) (end)) ...)) |
| `Pour` | `holes` | `holes` | zone every later (polygon (pts ...)) |
| `Pour` | `uuid` | `uuid` | zone (uuid) |
| `Pour` | `id` | `id` | - (pcbc handle) |

### Drawings

`layout_prims.SCHEMAS`, which the constructors, the decompiler, the emitter
and the writer all walk; a drawing's fields live in `model.Graphic.f` under
the keyword. A `DictNode`/`Sub` keyword is a dict keyed by KiCad's own token
names (`Dimension(format={"prefix": "", "units": 2, ...})`); a `Many` is a
list of them (a table's cells). Numbers: lengths to 1 nm, angles, ratios and
areas exactly.

| Python | KiCad primitive | keyword | KiCad token path | kind | written when |
|---|---|---|---|---|---|
| `Line` | `gr_line` | `start` (positional) | `start` | xy | always |
| `Line` | `gr_line` | `end` (positional) | `end` | xy | always |
| `Line` | `gr_line` | `width` | `stroke/width` | len | always |
| `Line` | `gr_line` | `stroke_type` | `stroke/type` | sym | always |
| `Line` | `gr_line` | `stroke_color` | `stroke/color` | reals | not `None` |
| `Line` | `gr_line` | `locked` | `locked` | yn | not `False` |
| `Line` | `gr_line` | `layer` | `layer` | str | not `None` |
| `Line` | `gr_line` | `layers` | `layers` | strs | not `None` |
| `Line` | `gr_line` | `solder_mask_margin` | `solder_mask_margin` | len | not `None` |
| `Line` | `gr_line` | `net` | `net` | str | not `None` |
| `Line` | `gr_line` | `uuid` | `uuid` | str | always |
| `Rect` | `gr_rect` | `start` (positional) | `start` | xy | always |
| `Rect` | `gr_rect` | `end` (positional) | `end` | xy | always |
| `Rect` | `gr_rect` | `radius` | `radius` | len | not `None` |
| `Rect` | `gr_rect` | `width` | `stroke/width` | len | always |
| `Rect` | `gr_rect` | `stroke_type` | `stroke/type` | sym | always |
| `Rect` | `gr_rect` | `stroke_color` | `stroke/color` | reals | not `None` |
| `Rect` | `gr_rect` | `fill` | `fill` | fill | always |
| `Rect` | `gr_rect` | `locked` | `locked` | yn | not `False` |
| `Rect` | `gr_rect` | `layer` | `layer` | str | not `None` |
| `Rect` | `gr_rect` | `layers` | `layers` | strs | not `None` |
| `Rect` | `gr_rect` | `solder_mask_margin` | `solder_mask_margin` | len | not `None` |
| `Rect` | `gr_rect` | `net` | `net` | str | not `None` |
| `Rect` | `gr_rect` | `uuid` | `uuid` | str | always |
| `Circle` | `gr_circle` | `center` (positional) | `center` | xy | always |
| `Circle` | `gr_circle` | `end` | `end` | xy | always |
| `Circle` | `gr_circle` | `width` | `stroke/width` | len | always |
| `Circle` | `gr_circle` | `stroke_type` | `stroke/type` | sym | always |
| `Circle` | `gr_circle` | `stroke_color` | `stroke/color` | reals | not `None` |
| `Circle` | `gr_circle` | `fill` | `fill` | fill | always |
| `Circle` | `gr_circle` | `locked` | `locked` | yn | not `False` |
| `Circle` | `gr_circle` | `layer` | `layer` | str | not `None` |
| `Circle` | `gr_circle` | `layers` | `layers` | strs | not `None` |
| `Circle` | `gr_circle` | `solder_mask_margin` | `solder_mask_margin` | len | not `None` |
| `Circle` | `gr_circle` | `net` | `net` | str | not `None` |
| `Circle` | `gr_circle` | `uuid` | `uuid` | str | always |
| `DrawArc` | `gr_arc` | `start` (positional) | `start` | xy | always |
| `DrawArc` | `gr_arc` | `mid` (positional) | `mid` | xy | always |
| `DrawArc` | `gr_arc` | `end` (positional) | `end` | xy | always |
| `DrawArc` | `gr_arc` | `width` | `stroke/width` | len | always |
| `DrawArc` | `gr_arc` | `stroke_type` | `stroke/type` | sym | always |
| `DrawArc` | `gr_arc` | `stroke_color` | `stroke/color` | reals | not `None` |
| `DrawArc` | `gr_arc` | `locked` | `locked` | yn | not `False` |
| `DrawArc` | `gr_arc` | `layer` | `layer` | str | not `None` |
| `DrawArc` | `gr_arc` | `layers` | `layers` | strs | not `None` |
| `DrawArc` | `gr_arc` | `solder_mask_margin` | `solder_mask_margin` | len | not `None` |
| `DrawArc` | `gr_arc` | `net` | `net` | str | not `None` |
| `DrawArc` | `gr_arc` | `uuid` | `uuid` | str | always |
| `Poly` | `gr_poly` | `pts` (positional) | `pts` | pts | always |
| `Poly` | `gr_poly` | `width` | `stroke/width` | len | always |
| `Poly` | `gr_poly` | `stroke_type` | `stroke/type` | sym | always |
| `Poly` | `gr_poly` | `stroke_color` | `stroke/color` | reals | not `None` |
| `Poly` | `gr_poly` | `fill` | `fill` | fill | always |
| `Poly` | `gr_poly` | `locked` | `locked` | yn | not `False` |
| `Poly` | `gr_poly` | `layer` | `layer` | str | not `None` |
| `Poly` | `gr_poly` | `layers` | `layers` | strs | not `None` |
| `Poly` | `gr_poly` | `solder_mask_margin` | `solder_mask_margin` | len | not `None` |
| `Poly` | `gr_poly` | `net` | `net` | str | not `None` |
| `Poly` | `gr_poly` | `uuid` | `uuid` | str | always |
| `Curve` | `gr_curve` | `pts` (positional) | `pts` | pts | always |
| `Curve` | `gr_curve` | `width` | `stroke/width` | len | always |
| `Curve` | `gr_curve` | `stroke_type` | `stroke/type` | sym | always |
| `Curve` | `gr_curve` | `stroke_color` | `stroke/color` | reals | not `None` |
| `Curve` | `gr_curve` | `locked` | `locked` | yn | not `False` |
| `Curve` | `gr_curve` | `layer` | `layer` | str | not `None` |
| `Curve` | `gr_curve` | `layers` | `layers` | strs | not `None` |
| `Curve` | `gr_curve` | `solder_mask_margin` | `solder_mask_margin` | len | not `None` |
| `Curve` | `gr_curve` | `net` | `net` | str | not `None` |
| `Curve` | `gr_curve` | `uuid` | `uuid` | str | always |
| `Text` | `gr_text` | `text` (positional) | `(atom)` | str | always |
| `Text` | `gr_text` | `locked` | `locked` | yn | not `False` |
| `Text` | `gr_text` | `at` (positional) | `at (atom 1)` | xy | always |
| `Text` | `gr_text` | `angle` | `at (atom 2)` | real | always |
| `Text` | `gr_text` | `layer` | `layer (atom 1)` | str | always |
| `Text` | `gr_text` | `knockout` | `layer (atom 2)` | flag | not `False` |
| `Text` | `gr_text` | `uuid` | `uuid` | str | always |
| `Text` | `gr_text` | `face` | `effects/font/face` | str | not `None` |
| `Text` | `gr_text` | `size` | `effects/font/size` | lens | always |
| `Text` | `gr_text` | `line_spacing` | `effects/font/line_spacing` | real | not `None` |
| `Text` | `gr_text` | `thickness` | `effects/font/thickness` | len | not `None` |
| `Text` | `gr_text` | `bold` | `effects/font/bold` | yn | not `False` |
| `Text` | `gr_text` | `italic` | `effects/font/italic` | yn | not `False` |
| `Text` | `gr_text` | `justify` | `effects/justify` | atoms | not `()` |
| `Text` | `gr_text` | `hide` | `effects/hide` | yn | not `False` |
| `TextBox` | `gr_text_box` | `text` (positional) | `(atom)` | str | always |
| `TextBox` | `gr_text_box` | `locked` | `locked` | yn | not `False` |
| `TextBox` | `gr_text_box` | `start` (positional) | `start` | xy | not `None` |
| `TextBox` | `gr_text_box` | `end` (positional) | `end` | xy | not `None` |
| `TextBox` | `gr_text_box` | `pts` | `pts` | pts | not `None` |
| `TextBox` | `gr_text_box` | `margins` | `margins` | lens | not `None` |
| `TextBox` | `gr_text_box` | `angle` | `angle` | real | not `None` |
| `TextBox` | `gr_text_box` | `layer` | `layer` | str | always |
| `TextBox` | `gr_text_box` | `uuid` | `uuid` | str | always |
| `TextBox` | `gr_text_box` | `face` | `effects/font/face` | str | not `None` |
| `TextBox` | `gr_text_box` | `size` | `effects/font/size` | lens | always |
| `TextBox` | `gr_text_box` | `line_spacing` | `effects/font/line_spacing` | real | not `None` |
| `TextBox` | `gr_text_box` | `thickness` | `effects/font/thickness` | len | not `None` |
| `TextBox` | `gr_text_box` | `bold` | `effects/font/bold` | yn | not `False` |
| `TextBox` | `gr_text_box` | `italic` | `effects/font/italic` | yn | not `False` |
| `TextBox` | `gr_text_box` | `justify` | `effects/justify` | atoms | not `()` |
| `TextBox` | `gr_text_box` | `hide` | `effects/hide` | yn | not `False` |
| `TextBox` | `gr_text_box` | `border` | `border` | yn | always |
| `TextBox` | `gr_text_box` | `width` | `stroke/width` | len | always |
| `TextBox` | `gr_text_box` | `stroke_type` | `stroke/type` | sym | always |
| `TextBox` | `gr_text_box` | `stroke_color` | `stroke/color` | reals | not `None` |
| `TextBox` | `gr_text_box` | `knockout` | `knockout` | yn | always |
| `Dimension` | `dimension` | `kind` | `type` | sym | always |
| `Dimension` | `dimension` | `locked` | `locked` | yn | not `False` |
| `Dimension` | `dimension` | `layer` | `layer` | str | always |
| `Dimension` | `dimension` | `uuid` | `uuid` | str | always |
| `Dimension` | `dimension` | `pts` (positional) | `pts` | pts | always |
| `Dimension` | `dimension` | `height` | `height` | len | not `None` |
| `Dimension` | `dimension` | `orientation` | `orientation` | int | not `None` |
| `Dimension` | `dimension` | `leader_length` | `leader_length` | len | not `None` |
| `Dimension` | `dimension` | `format` | `format` | DictNode | not `None` |
| `Dimension` | `dimension` | `style` | `style` | DictNode | not `None` |
| `Dimension` | `dimension` | `gr_text` | `gr_text` | Sub | not `None` |
| `Group` | `group` | `name` (positional) | `(atom)` | str | always |
| `Group` | `group` | `uuid` | `uuid` | str | always |
| `Group` | `group` | `locked` | `locked` | yn | not `False` |
| `Group` | `group` | `lib_id` | `lib_id` | str | not `None` |
| `Group` | `group` | `members` (positional) | `members` | refs | always |
| `Image` | `image` | `at` (positional) | `at` | xy | always |
| `Image` | `image` | `layer` | `layer` | str | always |
| `Image` | `image` | `scale` | `scale` | real | not `None` |
| `Image` | `image` | `locked` | `locked` | yn | not `False` |
| `Image` | `image` | `data` (positional) | `data` | blob | always |
| `Image` | `image` | `uuid` | `uuid` | str | always |
| `Table` | `table` | `column_count` (positional) | `column_count` | int | always |
| `Table` | `table` | `uuid` | `uuid` | str | always |
| `Table` | `table` | `locked` | `locked` | yn | not `False` |
| `Table` | `table` | `layer` | `layer` | str | always |
| `Table` | `table` | `border` | `border` | DictNode | not `None`; its `width`/`type` are `None` and no `(stroke)` is written when `external` and `header` are both off, as KiCad saves it |
| `Table` | `table` | `separators` | `separators` | DictNode | not `None`; its `width`/`type` are `None` and no `(stroke)` is written when `rows` and `cols` are both off, as KiCad saves it |
| `Table` | `table` | `column_widths` | `column_widths` | lens | always |
| `Table` | `table` | `row_heights` | `row_heights` | lens | always |
| `Table` | `table` | `cells` | `cells/table_cell (each)` | Many | not `()` |
| `Barcode` | `barcode` | `locked` | `locked` | yn | not `False` |
| `Barcode` | `barcode` | `at` (positional) | `at (atom 1)` | xy | always |
| `Barcode` | `barcode` | `angle` | `at (atom 2)` | real | always |
| `Barcode` | `barcode` | `layer` | `layer` | str | always |
| `Barcode` | `barcode` | `size` | `size` | lens | always |
| `Barcode` | `barcode` | `text` (positional) | `text` | str | always |
| `Barcode` | `barcode` | `text_height` | `text_height` | len | always |
| `Barcode` | `barcode` | `kind` | `type` | sym | always |
| `Barcode` | `barcode` | `ecc_level` | `ecc_level` | sym | not `None` |
| `Barcode` | `barcode` | `hide` | `hide` | yn | always |
| `Barcode` | `barcode` | `knockout` | `knockout` | yn | always |
| `Barcode` | `barcode` | `margins` | `margins` | lens | not `None` |
| `Barcode` | `barcode` | `uuid` | `uuid` | str | always |
| `Target` | `target` | `shape` | `(atom)` | sym | always |
| `Target` | `target` | `at` (positional) | `at` | xy | always |
| `Target` | `target` | `size` | `size` | len | always |
| `Target` | `target` | `width` | `width` | len | always |
| `Target` | `target` | `layer` | `layer` | str | always |
| `Target` | `target` | `uuid` | `uuid` | str | always |
| `Point` | `point` | `at` (positional) | `at` | xy | always |
| `Point` | `point` | `size` | `size` | len | always |
| `Point` | `point` | `layer` | `layer` | str | always |
| `Point` | `point` | `uuid` | `uuid` | str | always |
| `Generated` | `generated` | `uuid` | `uuid` | str | always |
| `Generated` | `generated` | `type` (positional) | `type` | sym | always |
| `Generated` | `generated` | `name` (positional) | `name` | str | always |
| `Generated` | `generated` | `layer` | `layer` | str | always |
| `Generated` | `generated` | `props` | `(every other child)` | Props | always |
| `Generated` | `generated` | `members` (positional) | `members` | refs | always |

## Field census

Every token KiCad 10.0.6 wrote under a primitive, in the five routed boards
(blinky, buck, c3_usb, node, ds2_addon; the router boards of a fresh build:
882 `segment`, 0 `arc`, 177 `via`, 23 `zone`, 107 `footprint`, 5 `gr_rect`, 149 `group`) and in four probe boards saved by KiCad 10.0.6 itself
(`tests/fixtures/kicad10/`, made by the `make_kicad10_primitives*.py` scripts
beside them: pcbnew with every via, track, zone and drawing option its Python
bindings set; for probe 3 what the bindings cannot build written in KiCad's
grammar and saved by `kicad-cli pcb upgrade`; and probe 4 the two fields the
second review found no Python field for — a zone's per-layer
`(property (layer) (hatch_position (xy)))` and an `(arc (start) (mid) (end))`
entry inside a `(pts ...)`, in a zone outline, a zone hole and a `gr_poly` —
written the same way and saved by KiCad: 1 `arc`, 1 `barcode`, 6 `dimension`, 1 `generated`, 1 `gr_arc`, 2 `gr_circle`, 1 `gr_curve`, 1 `gr_line`, 3 `gr_poly`, 1 `gr_rect`, 2 `gr_text`, 2 `gr_text_box`, 1 `group`, 1 `image`, 1 `point`, 4 `segment`, 1 `table`, 1 `target`, 8 `via`, 12 `zone`), with
the Python field that carries it. Zero unmapped fields:
`tests/test_layout_roundtrip.py` parses the boards and fails on a path this
table and `gen.CENSUS` do not have, and `gen.decompile` raises `Unmapped` on
one. `(atom)` is the bare word after the head (`via blind`, `target plus`,
`gr_text "..."`); `*` under `generated` is every property of the generator.

| KiCad primitive | KiCad token | Python field | in the 5 routed boards | in the 4 probes |
|---|---|---|---:|---:|
| `segment` | `start` | a (positional) | 882 | 4 |
| `segment` | `end` | b (positional) | 882 | 4 |
| `segment` | `width` | width | 882 | 4 |
| `segment` | `locked` | locked | 326 | 1 |
| `segment` | `layer` | layer | 882 | 2 |
| `segment` | `layers` | layer + solder_mask=True | 0 | 2 |
| `segment` | `solder_mask_margin` | solder_mask_margin | 0 | 1 |
| `segment` | `net` | net (positional) | 882 | 4 |
| `segment` | `uuid` | uuid | 882 | 4 |
| `arc` | `start` | a (positional) | 0 | 1 |
| `arc` | `end` | b (positional) | 0 | 1 |
| `arc` | `width` | width | 0 | 1 |
| `arc` | `locked` | locked | 0 | 1 |
| `arc` | `layer` | layer | 0 | 0 |
| `arc` | `layers` | layer + solder_mask=True | 0 | 1 |
| `arc` | `solder_mask_margin` | solder_mask_margin | 0 | 1 |
| `arc` | `net` | net (positional) | 0 | 1 |
| `arc` | `uuid` | uuid | 0 | 1 |
| `arc` | `mid` | mid (positional) | 0 | 1 |
| `via` | `(atom)` | kind (blind | buried | micro; none = through) | 0 | 3 |
| `via` | `at` | at (positional) | 177 | 8 |
| `via` | `size` | size | 177 | 8 |
| `via` | `drill` | drill | 177 | 8 |
| `via` | `layers` | layers | 177 | 8 |
| `via` | `locked` | locked | 141 | 1 |
| `via` | `free` | free | 0 | 1 |
| `via` | `remove_unused_layers` | remove_unused_layers | 0 | 1 |
| `via` | `keep_end_layers` | keep_end_layers | 0 | 1 |
| `via` | `zone_layer_connections` | zone_layer_connections | 0 | 1 |
| `via` | `tenting` | tenting | 0 | 2 |
| `via` | `tenting/front` | tenting['front'] | 0 | 2 |
| `via` | `tenting/back` | tenting['back'] | 0 | 2 |
| `via` | `covering` | covering | 0 | 1 |
| `via` | `covering/front` | covering['front'] | 0 | 1 |
| `via` | `covering/back` | covering['back'] | 0 | 1 |
| `via` | `plugging` | plugging | 0 | 1 |
| `via` | `plugging/front` | plugging['front'] | 0 | 1 |
| `via` | `plugging/back` | plugging['back'] | 0 | 1 |
| `via` | `capping` | capping | 0 | 2 |
| `via` | `filling` | filling | 0 | 2 |
| `via` | `padstack` | padstack | 0 | 1 |
| `via` | `padstack/mode` | padstack[0] | 0 | 1 |
| `via` | `padstack/layer` | padstack[1][i][0] | 0 | 2 |
| `via` | `padstack/layer/size` | padstack[1][i][1] | 0 | 2 |
| `via` | `backdrill` | backdrill | 0 | 1 |
| `via` | `backdrill/size` | backdrill[0] | 0 | 1 |
| `via` | `backdrill/layers` | backdrill[1] | 0 | 1 |
| `via` | `tertiary_drill` | tertiary_drill | 0 | 3 |
| `via` | `tertiary_drill/size` | tertiary_drill[0] | 0 | 3 |
| `via` | `tertiary_drill/layers` | tertiary_drill[1] | 0 | 3 |
| `via` | `front_post_machining` | front_post_machining['mode'] | 0 | 1 |
| `via` | `front_post_machining/size` | front_post_machining['size'] | 0 | 1 |
| `via` | `front_post_machining/depth` | front_post_machining['depth'] | 0 | 1 |
| `via` | `front_post_machining/angle` | front_post_machining['angle'] | 0 | 0 |
| `via` | `back_post_machining` | back_post_machining['mode'] | 0 | 1 |
| `via` | `back_post_machining/size` | back_post_machining['size'] | 0 | 1 |
| `via` | `back_post_machining/depth` | back_post_machining['depth'] | 0 | 0 |
| `via` | `back_post_machining/angle` | back_post_machining['angle'] | 0 | 1 |
| `via` | `teardrops` | teardrops | 0 | 2 |
| `via` | `teardrops/best_length_ratio` | teardrops['best_length_ratio'] | 0 | 2 |
| `via` | `teardrops/max_length` | teardrops['max_length'] | 0 | 2 |
| `via` | `teardrops/best_width_ratio` | teardrops['best_width_ratio'] | 0 | 2 |
| `via` | `teardrops/max_width` | teardrops['max_width'] | 0 | 2 |
| `via` | `teardrops/curved_edges` | teardrops['curved_edges'] | 0 | 2 |
| `via` | `teardrops/filter_ratio` | teardrops['filter_ratio'] | 0 | 2 |
| `via` | `teardrops/enabled` | teardrops['enabled'] | 0 | 2 |
| `via` | `teardrops/allow_two_segments` | teardrops['allow_two_segments'] | 0 | 2 |
| `via` | `teardrops/prefer_zone_connections` | teardrops['prefer_zone_connections'] | 0 | 2 |
| `via` | `net` | net (positional) | 177 | 8 |
| `via` | `uuid` | uuid | 177 | 8 |
| `zone` | `net` | net (positional; None = no token) | 6 | 9 |
| `zone` | `locked` | locked | 0 | 1 |
| `zone` | `layer` | layer | 6 | 10 |
| `zone` | `layers` | layers | 17 | 2 |
| `zone` | `uuid` | uuid | 23 | 12 |
| `zone` | `name` | name | 17 | 2 |
| `zone` | `hatch` | hatch | 23 | 12 |
| `zone` | `priority` | priority | 0 | 1 |
| `zone` | `attr` | teardrop_type | 0 | 0 |
| `zone` | `attr/teardrop` | teardrop_type | 0 | 0 |
| `zone` | `attr/teardrop/type` | teardrop_type | 0 | 0 |
| `zone` | `connect_pads` | connect (the atom: none = thermal, yes = solid, no = none, thru_hole_only) | 23 | 12 |
| `zone` | `connect_pads/clearance` | clearance | 23 | 12 |
| `zone` | `min_thickness` | min_thickness | 23 | 12 |
| `zone` | `filled_areas_thickness` | filled_areas_thickness | 0 | 0 |
| `zone` | `keepout` | keepout | 17 | 2 |
| `zone` | `keepout/tracks` | keepout['tracks'] | 17 | 2 |
| `zone` | `keepout/vias` | keepout['vias'] | 17 | 2 |
| `zone` | `keepout/pads` | keepout['pads'] | 17 | 2 |
| `zone` | `keepout/copperpour` | keepout['copperpour'] | 17 | 2 |
| `zone` | `keepout/footprints` | keepout['footprints'] | 17 | 2 |
| `zone` | `placement` | placement | 17 | 2 |
| `zone` | `placement/enabled` | placement['enabled'] | 17 | 2 |
| `zone` | `placement/sheetname` | placement['sheetname'] | 17 | 1 |
| `zone` | `placement/component_class` | placement['component_class'] | 0 | 1 |
| `zone` | `placement/group` | placement['group'] | 0 | 0 |
| `zone` | `fill` | filled (the `yes` atom) | 23 | 12 |
| `zone` | `fill/mode` | fill_mode | 0 | 3 |
| `zone` | `fill/thermal_gap` | thermal_gap | 23 | 12 |
| `zone` | `fill/thermal_bridge_width` | thermal_bridge_width | 23 | 12 |
| `zone` | `fill/smoothing` | smoothing | 0 | 1 |
| `zone` | `fill/radius` | radius | 0 | 1 |
| `zone` | `fill/island_removal_mode` | island_removal_mode | 23 | 12 |
| `zone` | `fill/island_area_min` | island_area_min | 0 | 1 |
| `zone` | `fill/hatch_thickness` | hatch_thickness | 0 | 3 |
| `zone` | `fill/hatch_gap` | hatch_gap | 0 | 3 |
| `zone` | `fill/hatch_orientation` | hatch_orientation | 0 | 3 |
| `zone` | `fill/hatch_smoothing_level` | hatch_smoothing_level | 0 | 1 |
| `zone` | `fill/hatch_smoothing_value` | hatch_smoothing_value | 0 | 1 |
| `zone` | `fill/hatch_border_algorithm` | hatch_border_algorithm | 0 | 3 |
| `zone` | `fill/hatch_min_hole_area` | hatch_min_hole_area | 0 | 3 |
| `zone` | `property` | hatch_position (one (property) per layer) | 0 | 2 |
| `zone` | `property/layer` | hatch_position key (the layer) | 0 | 2 |
| `zone` | `property/hatch_position` | hatch_position[layer] | 0 | 2 |
| `zone` | `property/hatch_position/xy` | hatch_position[layer] = (x, y) | 0 | 2 |
| `zone` | `polygon` | points (first) / holes (every later one) | 23 | 14 |
| `zone` | `polygon/pts` | points / holes[i] | 23 | 14 |
| `zone` | `polygon/pts/xy` | points[i] / holes[i][j] (a point) | 92 | 44 |
| `zone` | `polygon/pts/arc` | points[i] / holes[i][j] (an arc entry: ((sx, sy), (mx, my), (ex, ey))) | 0 | 2 |
| `zone` | `polygon/pts/arc/start` | points[i][0] / holes[i][j][0] | 0 | 2 |
| `zone` | `polygon/pts/arc/mid` | points[i][1] / holes[i][j][1] | 0 | 2 |
| `zone` | `polygon/pts/arc/end` | points[i][2] / holes[i][j][2] | 0 | 2 |
| `zone` | `filled_polygon` | not a field: KiCad's fill, regenerated by `kicad-cli pcb drc --refill-zones` from the fields above | 6 | 0 |
| `footprint` | `at` | Place at + rot | 107 | 0 |
| `footprint` | `layer` | Place layer | 107 | 0 |
| `footprint` | `locked` | Place locked | 92 | 0 |
| `gr_line` | `end` | end | 0 | 1 |
| `gr_line` | `layer` | layer | 0 | 0 |
| `gr_line` | `layers` | layers | 0 | 1 |
| `gr_line` | `locked` | locked | 0 | 1 |
| `gr_line` | `net` | net | 0 | 1 |
| `gr_line` | `solder_mask_margin` | solder_mask_margin | 0 | 1 |
| `gr_line` | `start` | start | 0 | 1 |
| `gr_line` | `stroke` | holds width, stroke_type, stroke_color | 0 | 1 |
| `gr_line` | `stroke/color` | stroke_color | 0 | 0 |
| `gr_line` | `stroke/type` | stroke_type | 0 | 1 |
| `gr_line` | `stroke/width` | width | 0 | 1 |
| `gr_line` | `uuid` | uuid | 0 | 1 |
| `gr_rect` | `end` | end | 5 | 1 |
| `gr_rect` | `fill` | fill | 5 | 1 |
| `gr_rect` | `layer` | layer | 5 | 1 |
| `gr_rect` | `layers` | layers | 0 | 0 |
| `gr_rect` | `locked` | locked | 0 | 1 |
| `gr_rect` | `net` | net | 0 | 0 |
| `gr_rect` | `radius` | radius | 0 | 1 |
| `gr_rect` | `solder_mask_margin` | solder_mask_margin | 0 | 0 |
| `gr_rect` | `start` | start | 5 | 1 |
| `gr_rect` | `stroke` | holds width, stroke_type, stroke_color | 5 | 1 |
| `gr_rect` | `stroke/color` | stroke_color | 0 | 0 |
| `gr_rect` | `stroke/type` | stroke_type | 5 | 1 |
| `gr_rect` | `stroke/width` | width | 5 | 1 |
| `gr_rect` | `uuid` | uuid | 5 | 1 |
| `gr_circle` | `center` | center | 0 | 2 |
| `gr_circle` | `end` | end | 0 | 2 |
| `gr_circle` | `fill` | fill | 0 | 2 |
| `gr_circle` | `layer` | layer | 0 | 2 |
| `gr_circle` | `layers` | layers | 0 | 0 |
| `gr_circle` | `locked` | locked | 0 | 0 |
| `gr_circle` | `net` | net | 0 | 1 |
| `gr_circle` | `solder_mask_margin` | solder_mask_margin | 0 | 0 |
| `gr_circle` | `stroke` | holds width, stroke_type, stroke_color | 0 | 2 |
| `gr_circle` | `stroke/color` | stroke_color | 0 | 0 |
| `gr_circle` | `stroke/type` | stroke_type | 0 | 2 |
| `gr_circle` | `stroke/width` | width | 0 | 2 |
| `gr_circle` | `uuid` | uuid | 0 | 2 |
| `gr_arc` | `end` | end | 0 | 1 |
| `gr_arc` | `layer` | layer | 0 | 1 |
| `gr_arc` | `layers` | layers | 0 | 0 |
| `gr_arc` | `locked` | locked | 0 | 0 |
| `gr_arc` | `mid` | mid | 0 | 1 |
| `gr_arc` | `net` | net | 0 | 0 |
| `gr_arc` | `solder_mask_margin` | solder_mask_margin | 0 | 0 |
| `gr_arc` | `start` | start | 0 | 1 |
| `gr_arc` | `stroke` | holds width, stroke_type, stroke_color | 0 | 1 |
| `gr_arc` | `stroke/color` | stroke_color | 0 | 0 |
| `gr_arc` | `stroke/type` | stroke_type | 0 | 1 |
| `gr_arc` | `stroke/width` | width | 0 | 1 |
| `gr_arc` | `uuid` | uuid | 0 | 1 |
| `gr_poly` | `fill` | fill | 0 | 3 |
| `gr_poly` | `layer` | layer | 0 | 2 |
| `gr_poly` | `layers` | layers | 0 | 1 |
| `gr_poly` | `locked` | locked | 0 | 0 |
| `gr_poly` | `net` | net | 0 | 1 |
| `gr_poly` | `pts` | pts | 0 | 3 |
| `gr_poly` | `pts/arc` | pts[i] (an arc entry: ((sx, sy), (mx, my), (ex, ey))) | 0 | 1 |
| `gr_poly` | `pts/arc/end` | pts[i][2] | 0 | 1 |
| `gr_poly` | `pts/arc/mid` | pts[i][1] | 0 | 1 |
| `gr_poly` | `pts/arc/start` | pts[i][0] | 0 | 1 |
| `gr_poly` | `pts/xy` | pts[i] (a point) | 0 | 8 |
| `gr_poly` | `solder_mask_margin` | solder_mask_margin | 0 | 0 |
| `gr_poly` | `stroke` | holds width, stroke_type, stroke_color | 0 | 3 |
| `gr_poly` | `stroke/color` | stroke_color | 0 | 0 |
| `gr_poly` | `stroke/type` | stroke_type | 0 | 3 |
| `gr_poly` | `stroke/width` | width | 0 | 3 |
| `gr_poly` | `uuid` | uuid | 0 | 3 |
| `gr_curve` | `layer` | layer | 0 | 1 |
| `gr_curve` | `layers` | layers | 0 | 0 |
| `gr_curve` | `locked` | locked | 0 | 0 |
| `gr_curve` | `net` | net | 0 | 0 |
| `gr_curve` | `pts` | pts | 0 | 1 |
| `gr_curve` | `pts/arc` | pts[i] (an arc entry: ((sx, sy), (mx, my), (ex, ey))) | 0 | 0 |
| `gr_curve` | `pts/arc/end` | pts[i][2] | 0 | 0 |
| `gr_curve` | `pts/arc/mid` | pts[i][1] | 0 | 0 |
| `gr_curve` | `pts/arc/start` | pts[i][0] | 0 | 0 |
| `gr_curve` | `pts/xy` | pts[i] (a point) | 0 | 4 |
| `gr_curve` | `solder_mask_margin` | solder_mask_margin | 0 | 0 |
| `gr_curve` | `stroke` | holds width, stroke_type, stroke_color | 0 | 1 |
| `gr_curve` | `stroke/color` | stroke_color | 0 | 0 |
| `gr_curve` | `stroke/type` | stroke_type | 0 | 1 |
| `gr_curve` | `stroke/width` | width | 0 | 1 |
| `gr_curve` | `uuid` | uuid | 0 | 1 |
| `gr_text` | `(atom)` | text (positional) | 0 | 2 |
| `gr_text` | `at` | at + angle | 0 | 2 |
| `gr_text` | `effects` | holds face, size, line_spacing, thickness, bold, italic, justify, hide | 0 | 2 |
| `gr_text` | `effects/font` | holds face, size, line_spacing, thickness, bold, italic | 0 | 2 |
| `gr_text` | `effects/font/bold` | bold | 0 | 1 |
| `gr_text` | `effects/font/face` | face | 0 | 1 |
| `gr_text` | `effects/font/italic` | italic | 0 | 1 |
| `gr_text` | `effects/font/line_spacing` | line_spacing | 0 | 1 |
| `gr_text` | `effects/font/size` | size | 0 | 2 |
| `gr_text` | `effects/font/thickness` | thickness | 0 | 2 |
| `gr_text` | `effects/hide` | hide | 0 | 0 |
| `gr_text` | `effects/justify` | justify | 0 | 2 |
| `gr_text` | `layer` | layer + knockout | 0 | 2 |
| `gr_text` | `locked` | locked | 0 | 1 |
| `gr_text` | `render_cache` | not a field: KiCad's derived cache, regenerated on save | 0 | 1 |
| `gr_text` | `uuid` | uuid | 0 | 2 |
| `gr_text_box` | `(atom)` | text (positional) | 0 | 2 |
| `gr_text_box` | `angle` | angle | 0 | 1 |
| `gr_text_box` | `border` | border | 0 | 2 |
| `gr_text_box` | `effects` | holds face, size, line_spacing, thickness, bold, italic, justify, hide | 0 | 2 |
| `gr_text_box` | `effects/font` | holds face, size, line_spacing, thickness, bold, italic | 0 | 2 |
| `gr_text_box` | `effects/font/bold` | bold | 0 | 1 |
| `gr_text_box` | `effects/font/face` | face | 0 | 0 |
| `gr_text_box` | `effects/font/italic` | italic | 0 | 0 |
| `gr_text_box` | `effects/font/line_spacing` | line_spacing | 0 | 0 |
| `gr_text_box` | `effects/font/size` | size | 0 | 2 |
| `gr_text_box` | `effects/font/thickness` | thickness | 0 | 1 |
| `gr_text_box` | `effects/hide` | hide | 0 | 0 |
| `gr_text_box` | `effects/justify` | justify | 0 | 2 |
| `gr_text_box` | `end` | end | 0 | 1 |
| `gr_text_box` | `knockout` | knockout | 0 | 2 |
| `gr_text_box` | `layer` | layer | 0 | 2 |
| `gr_text_box` | `locked` | locked | 0 | 1 |
| `gr_text_box` | `margins` | margins | 0 | 2 |
| `gr_text_box` | `pts` | pts | 0 | 1 |
| `gr_text_box` | `pts/arc` | pts[i] (an arc entry: ((sx, sy), (mx, my), (ex, ey))) | 0 | 0 |
| `gr_text_box` | `pts/arc/end` | pts[i][2] | 0 | 0 |
| `gr_text_box` | `pts/arc/mid` | pts[i][1] | 0 | 0 |
| `gr_text_box` | `pts/arc/start` | pts[i][0] | 0 | 0 |
| `gr_text_box` | `pts/xy` | pts[i] (a point) | 0 | 4 |
| `gr_text_box` | `render_cache` | not a field: KiCad's derived cache, regenerated on save | 0 | 0 |
| `gr_text_box` | `start` | start | 0 | 1 |
| `gr_text_box` | `stroke` | holds width, stroke_type, stroke_color | 0 | 2 |
| `gr_text_box` | `stroke/color` | stroke_color | 0 | 0 |
| `gr_text_box` | `stroke/type` | stroke_type | 0 | 2 |
| `gr_text_box` | `stroke/width` | width | 0 | 2 |
| `gr_text_box` | `uuid` | uuid | 0 | 2 |
| `dimension` | `format` | format | 0 | 5 |
| `dimension` | `format/override_value` | format.override_value | 0 | 2 |
| `dimension` | `format/precision` | format.precision | 0 | 5 |
| `dimension` | `format/prefix` | format.prefix | 0 | 5 |
| `dimension` | `format/suffix` | format.suffix | 0 | 5 |
| `dimension` | `format/suppress_zeroes` | format.suppress_zeroes | 0 | 1 |
| `dimension` | `format/units` | format.units | 0 | 5 |
| `dimension` | `format/units_format` | format.units_format | 0 | 5 |
| `dimension` | `gr_text` | gr_text | 0 | 5 |
| `dimension` | `gr_text/(atom)` | gr_text.text (positional) | 0 | 0 |
| `dimension` | `gr_text/at` | gr_text.at + gr_text.angle | 0 | 5 |
| `dimension` | `gr_text/effects` | holds gr_text.face, gr_text.size, gr_text.line_spacing, gr_text.thickness, gr_text.bold, gr_text.italic, gr_text.justify, gr_text.hide | 0 | 5 |
| `dimension` | `gr_text/effects/font` | holds gr_text.face, gr_text.size, gr_text.line_spacing, gr_text.thickness, gr_text.bold, gr_text.italic | 0 | 5 |
| `dimension` | `gr_text/effects/font/bold` | gr_text.bold | 0 | 0 |
| `dimension` | `gr_text/effects/font/face` | gr_text.face | 0 | 0 |
| `dimension` | `gr_text/effects/font/italic` | gr_text.italic | 0 | 0 |
| `dimension` | `gr_text/effects/font/line_spacing` | gr_text.line_spacing | 0 | 0 |
| `dimension` | `gr_text/effects/font/size` | gr_text.size | 0 | 5 |
| `dimension` | `gr_text/effects/font/thickness` | gr_text.thickness | 0 | 1 |
| `dimension` | `gr_text/effects/hide` | gr_text.hide | 0 | 0 |
| `dimension` | `gr_text/effects/justify` | gr_text.justify | 0 | 0 |
| `dimension` | `gr_text/layer` | gr_text.layer + gr_text.knockout | 0 | 5 |
| `dimension` | `gr_text/locked` | gr_text.locked | 0 | 1 |
| `dimension` | `gr_text/render_cache` | not a field: KiCad's derived cache, regenerated on save | 0 | 0 |
| `dimension` | `gr_text/uuid` | gr_text.uuid | 0 | 5 |
| `dimension` | `height` | height | 0 | 3 |
| `dimension` | `layer` | layer | 0 | 6 |
| `dimension` | `leader_length` | leader_length | 0 | 1 |
| `dimension` | `locked` | locked | 0 | 1 |
| `dimension` | `orientation` | orientation | 0 | 1 |
| `dimension` | `pts` | pts | 0 | 6 |
| `dimension` | `pts/arc` | pts[i] (an arc entry: ((sx, sy), (mx, my), (ex, ey))) | 0 | 0 |
| `dimension` | `pts/arc/end` | pts[i][2] | 0 | 0 |
| `dimension` | `pts/arc/mid` | pts[i][1] | 0 | 0 |
| `dimension` | `pts/arc/start` | pts[i][0] | 0 | 0 |
| `dimension` | `pts/xy` | pts[i] (a point) | 0 | 12 |
| `dimension` | `style` | style | 0 | 6 |
| `dimension` | `style/arrow_direction` | style.arrow_direction | 0 | 3 |
| `dimension` | `style/arrow_length` | style.arrow_length | 0 | 6 |
| `dimension` | `style/extension_height` | style.extension_height | 0 | 3 |
| `dimension` | `style/extension_offset` | style.extension_offset | 0 | 6 |
| `dimension` | `style/keep_text_aligned` | style.keep_text_aligned | 0 | 4 |
| `dimension` | `style/text_frame` | style.text_frame | 0 | 1 |
| `dimension` | `style/text_position_mode` | style.text_position_mode | 0 | 6 |
| `dimension` | `style/thickness` | style.thickness | 0 | 6 |
| `dimension` | `type` | kind | 0 | 6 |
| `dimension` | `uuid` | uuid | 0 | 6 |
| `group` | `(atom)` | name (positional) | 149 | 1 |
| `group` | `lib_id` | lib_id | 0 | 0 |
| `group` | `locked` | locked | 0 | 1 |
| `group` | `members` | members | 149 | 1 |
| `group` | `uuid` | uuid | 149 | 1 |
| `image` | `at` | at | 0 | 1 |
| `image` | `data` | data | 0 | 1 |
| `image` | `layer` | layer | 0 | 1 |
| `image` | `locked` | locked | 0 | 1 |
| `image` | `scale` | scale | 0 | 1 |
| `image` | `uuid` | uuid | 0 | 1 |
| `table` | `border` | border | 0 | 1 |
| `table` | `border/external` | border.external | 0 | 1 |
| `table` | `border/header` | border.header | 0 | 1 |
| `table` | `border/stroke` | holds border.width, border.type, border.color | 0 | 1 |
| `table` | `border/stroke/color` | border.color | 0 | 0 |
| `table` | `border/stroke/type` | border.type | 0 | 1 |
| `table` | `border/stroke/width` | border.width | 0 | 1 |
| `table` | `cells` | holds cells | 0 | 1 |
| `table` | `cells/table_cell` | cells[i] | 0 | 2 |
| `table` | `cells/table_cell/(atom)` | cells[i].text (positional) | 0 | 0 |
| `table` | `cells/table_cell/angle` | cells[i].angle | 0 | 0 |
| `table` | `cells/table_cell/effects` | holds cells[i].face, cells[i].size, cells[i].line_spacing, cells[i].thickness, cells[i].bold, cells[i].italic, cells[i].justify, cells[i].hide | 0 | 2 |
| `table` | `cells/table_cell/effects/font` | holds cells[i].face, cells[i].size, cells[i].line_spacing, cells[i].thickness, cells[i].bold, cells[i].italic | 0 | 2 |
| `table` | `cells/table_cell/effects/font/bold` | cells[i].bold | 0 | 1 |
| `table` | `cells/table_cell/effects/font/face` | cells[i].face | 0 | 0 |
| `table` | `cells/table_cell/effects/font/italic` | cells[i].italic | 0 | 0 |
| `table` | `cells/table_cell/effects/font/line_spacing` | cells[i].line_spacing | 0 | 0 |
| `table` | `cells/table_cell/effects/font/size` | cells[i].size | 0 | 2 |
| `table` | `cells/table_cell/effects/font/thickness` | cells[i].thickness | 0 | 2 |
| `table` | `cells/table_cell/effects/hide` | cells[i].hide | 0 | 0 |
| `table` | `cells/table_cell/effects/justify` | cells[i].justify | 0 | 1 |
| `table` | `cells/table_cell/end` | cells[i].end | 0 | 2 |
| `table` | `cells/table_cell/layer` | cells[i].layer | 0 | 2 |
| `table` | `cells/table_cell/locked` | cells[i].locked | 0 | 0 |
| `table` | `cells/table_cell/margins` | cells[i].margins | 0 | 2 |
| `table` | `cells/table_cell/pts` | cells[i].pts | 0 | 0 |
| `table` | `cells/table_cell/pts/arc` | cells[i].pts[i] (an arc entry: ((sx, sy), (mx, my), (ex, ey))) | 0 | 0 |
| `table` | `cells/table_cell/pts/arc/end` | cells[i].pts[i][2] | 0 | 0 |
| `table` | `cells/table_cell/pts/arc/mid` | cells[i].pts[i][1] | 0 | 0 |
| `table` | `cells/table_cell/pts/arc/start` | cells[i].pts[i][0] | 0 | 0 |
| `table` | `cells/table_cell/pts/xy` | cells[i].pts[i] (a point) | 0 | 0 |
| `table` | `cells/table_cell/render_cache` | not a field: KiCad's derived cache, regenerated on save | 0 | 0 |
| `table` | `cells/table_cell/span` | cells[i].span | 0 | 2 |
| `table` | `cells/table_cell/start` | cells[i].start | 0 | 2 |
| `table` | `cells/table_cell/uuid` | cells[i].uuid | 0 | 2 |
| `table` | `column_count` | column_count | 0 | 1 |
| `table` | `column_widths` | column_widths | 0 | 1 |
| `table` | `layer` | layer | 0 | 1 |
| `table` | `locked` | locked | 0 | 1 |
| `table` | `row_heights` | row_heights | 0 | 1 |
| `table` | `separators` | separators | 0 | 1 |
| `table` | `separators/cols` | separators.cols | 0 | 1 |
| `table` | `separators/rows` | separators.rows | 0 | 1 |
| `table` | `separators/stroke` | holds separators.width, separators.type, separators.color | 0 | 1 |
| `table` | `separators/stroke/color` | separators.color | 0 | 0 |
| `table` | `separators/stroke/type` | separators.type | 0 | 1 |
| `table` | `separators/stroke/width` | separators.width | 0 | 1 |
| `table` | `uuid` | uuid | 0 | 1 |
| `barcode` | `at` | at + angle | 0 | 1 |
| `barcode` | `ecc_level` | ecc_level | 0 | 1 |
| `barcode` | `hide` | hide | 0 | 1 |
| `barcode` | `knockout` | knockout | 0 | 1 |
| `barcode` | `layer` | layer | 0 | 1 |
| `barcode` | `locked` | locked | 0 | 1 |
| `barcode` | `margins` | margins | 0 | 1 |
| `barcode` | `size` | size | 0 | 1 |
| `barcode` | `text` | text | 0 | 1 |
| `barcode` | `text_height` | text_height | 0 | 1 |
| `barcode` | `type` | kind | 0 | 1 |
| `barcode` | `uuid` | uuid | 0 | 1 |
| `target` | `(atom)` | shape (positional) | 0 | 1 |
| `target` | `at` | at | 0 | 1 |
| `target` | `layer` | layer | 0 | 1 |
| `target` | `size` | size | 0 | 1 |
| `target` | `uuid` | uuid | 0 | 1 |
| `target` | `width` | width | 0 | 1 |
| `point` | `at` | at | 0 | 1 |
| `point` | `layer` | layer | 0 | 1 |
| `point` | `size` | size | 0 | 1 |
| `point` | `uuid` | uuid | 0 | 1 |
| `generated` | `*` | props[key] (the generator's own properties) | 0 | 0 |
| `generated` | `layer` | layer | 0 | 1 |
| `generated` | `members` | members | 0 | 1 |
| `generated` | `name` | name | 0 | 1 |
| `generated` | `type` | type | 0 | 1 |
| `generated` | `uuid` | uuid | 0 | 1 |

A footprint's other children are the `.kicad_mod`'s and `board.py`'s, not
layout fields; the ones the five boards carry: `attr`, `descr`, `duplicate_pad_numbers_are_jumpers`, `embedded_fonts`, `fp_circle`, `fp_line`, `fp_poly`, `fp_rect`, `fp_text`, `model`, `pad`, `property`, `tags`, `uuid`, `zone`.

## What KiCad rewrites on save, and why it is not a loss

- **`filled_polygon`** is the zone fill, not a field. The board is refilled by
  `kicad-cli pcb drc --refill-zones --save-board` from the fields above; on
  all five boards every zone's refilled polygons are identical to the router
  board's, block for block.
- **The `(fill yes ...)` atom** is KiCad's "this zone is filled" flag. It is a
  field (`filled`). The router's board is refilled before it is decompiled, so
  a gen line carries `filled=True`; a hand-written core `Pour` that leaves it
  out gets what the refill leaves (yes for a copper pour, no for a rule area).
- **`render_cache`** is the outline-font polygons of a text whose `face` is a
  real font. Derived from the text, its font and its position; the decompiler
  drops it and KiCad regenerates it on the save (probe 3's `FACE` text:
  emitted without it, saved by `kicad-cli`, back identical).
- **A dimension's `gr_text`**: KiCad recomputes its string, and its position
  unless `style.text_position_mode` is 2 (manual), from the dimension's own
  fields on every save, and gives it the dimension's uuid. Decompiled as KiCad
  wrote it, so the round trip holds.
- **A group's `members`**: KiCad writes the member uuids sorted; the emitter
  sorts them the same way, and members compare as a set.
- **A generator's `last_*` properties** (a tuning pattern's record of its last
  tuning run) and its `name` ("Tuning Pattern"): KiCad redoes the tuning on
  load and names the pattern itself. A decompiled line carries KiCad's values;
  a hand-written `Generated("tuning_pattern", ...)` gets KiCad 10.0.6's
  defaults for every property it leaves out, and `last_*` compare as KiCad's.
- **Canonical forms KiCad writes, which the constructors write too** (measured
  with `kicad-cli pcb upgrade`), so a hand-written object is already what
  KiCad's save reads back: a `gr_rect`'s corners as (min x, min y), (max x,
  max y); a `gr_arc` turning clockwise on screen (a track `arc` is never
  swapped); a text box's default `margins` (0.75 x text height + half the
  stroke: 0.825 for 1 mm text and a 0.15 stroke); a table cell's default
  `margins` (0.75 x text height less 1 nm: 0.749999) and `span` (1 1); a
  dimension's `format`/`style` defaults per kind; a rule area's zero
  `clearance` and `(placement (enabled no) (sheetname ""))`. The build refuses
  a core line KiCad did not keep as written, naming the line and the field.
- **uuids**: every layout object's uuid is a field, written explicitly, so it
  survives emit and save — on the fab board too (`fab.layout_uuids_of` is
  `pin_all_uuids`' `keep` there; until the second review the fab board kept
  none of them, every `pcbc:` group dangled and KiCad dropped them all on
  load). Everything else (footprints, pads, fields) is re-pinned by position
  with `sexp.pin_all_uuids` under the same key on both boards, and comes out
  identical.
- **A zone's `(property (layer) (hatch_position (xy)))`** is the hatch fill's
  per-layer offset, `hatch_position={"F.Cu": (x, y)}`, written after `(fill)`
  and before the polygons as KiCad does. **An `(arc (start) (mid) (end))`
  inside a `(pts ...)`** is the entry `((sx, sy), (mx, my), (ex, ey))` of
  `points`, `holes[i]` or a drawing's `pts`; a property check reads an arc as
  its three points (`layout_prims.flat_points`), the arc itself is KiCad's.
- **A teardrop zone** — a `(zone ...)` carrying `(attr (teardrop (type ...)))`,
  with no `(uuid)` and no `(name)` — is KiCad's teardrop generator's, derived
  from a via's or pad's `teardrops` fields. Like `filled_polygon` it is not a
  layout object: `gen.decompile` reads and drops it (`Board.teardrops` counts
  them), `route_verify.zones` and `route_scene.zone_rules` skip it, the
  emitter never writes one, and KiCad's refill regenerates it. A hand-written
  `Via(teardrops={"enabled": True})` takes KiCad 10.0.6's values for every
  teardrop parameter it leaves out (`layout_emit.TEARDROP_DEFAULTS`, recorded
  in `core_filled`); probe 5 carries one as KiCad saved it.
- **Strings**: inside a quoted string KiCad writes a newline as `\n`, a
  carriage return as `\r`, a double quote as `\"`, a backslash as `\\`, and
  a tab as itself; `sexp.parse_tree` decodes exactly those and `sexp.quote`
  writes them, so a `Text("a\nb")` round-trips through KiCad's save byte for
  byte (probe 5, `kicad-cli pcb upgrade`).
- **A zone outline** is held by KiCad's load with every coordinate at 1 nm, a
  vertex equal to the one before it dropped, a closing vertex equal to the
  first dropped, and a vertex equal to the start of the arc after it or the
  end of the arc before it dropped; a collinear midpoint, a clockwise
  outline and a self-touching outline are kept. `layout_emit.normalise_outline`
  puts a core `Pour`'s `points` and `holes` in that form before anything
  compares them (the change is recorded in `core_filled`), so a line KiCad
  would rewrite is never refused as "the router dropped locked copper".
- **A hatch fill's defaults**: a zone handed `(fill yes (mode hatch))` and
  nothing else comes back with `hatch_thickness 1`, `hatch_gap 1.5`,
  `hatch_orientation 0`, `hatch_border_algorithm hatch_thickness`,
  `hatch_min_hole_area 0.15`; `Pour(fill_mode="hatch")` takes them
  (`layout_emit.HATCH_DEFAULTS`, recorded in `core_filled`).
- **KiCad's precision** (measured, `kicad-cli pcb upgrade`): a length is kept
  to 1 nm (six decimals); a teardrop ratio, a hatch orientation or smoothing
  value, and a text or text-box angle to **10 significant digits**
  (`0.3333333333333333` comes back `0.3333333333`); a zone's `island_area_min`
  to six decimals; a post-machining angle to **one** decimal (`12.3456789012345`
  comes back `12.3`). A core line written past a limit is refused with what
  KiCad kept: every refusal of a field prints `written X, KiCad's save has Y:
  write that`, and a dict-valued field names the key that differs
  (`teardrops.filter_ratio`), not the dict around it.
- **Order and spelling**: KiCad orders children and top-level items itself;
  the decompiler reads children in any order. Lengths are written to 1 nm
  (KiCad stores nanometres); angles, ratios and areas are doubles in KiCad and
  are written exactly (`sexp.fmt_exact`), never cut to six decimals.

## Measured round trip (2026-09-24, KiCad 10.0.6, third review)

Fresh builds of temp copies of the five boards (`build_job(..., upto="fab",
force=True)`, `scratchpad/step1b/fix3/make_table.py`), twice each from two
different absolute paths. The router's board (`routed/router.kicad_pcb`, after
normalise), the emitted board (`routed/layout.kicad_pcb`, after the copper
gate's refill and save) and the fab board (`fab/layout.kicad_pcb`, after fab's
DRC and save) compared primitive by primitive and by uuid:

| Board | segments | vias | zones | drawings (outline + role groups) | fields differing, router vs emitted | copper objects differing, emitted vs fab (`fab_copper_diff`) | KiCad DRC, router = emitted by type | fab DRC unconnected | every gate passed, fab ok | Gerbers | two builds from two absolute paths byte-identical (seed, placed, gen, routed, fab board, bom, cpl, report.json, both stamps) | fab board -> Python = `layout.gen.py` bytes |
|---|---:|---:|---:|---:|---:|---:|---|---:|---|---:|---|---|
| blinky | 4 | 1 | 4 | 3 | 0 | 0 | 0 errors, 5 warnings, same | 0 | yes | 10 | yes | yes |
| buck | 73 | 7 | 4 | 9 | 0 | 0 | 0 errors, 70 warnings, same | 0 | yes | 10 | yes | yes |
| c3_usb | 302 | 55 | 5 | 48 | 0 | 0 | 0 errors, 283 warnings, same | 0 | yes | 10 | yes | yes |
| node | 275 | 86 | 6 | 72 | 0 | 0 | 0 errors, 276 warnings, same | 0 | yes | 12 | yes | yes |
| ds2_addon | 228 | 28 | 4 | 22 | 0 | 0 | 0 errors, 114 warnings, same | 0 | yes | 10 | yes | yes |

"Every gate passed" is `planes` (ring, islands, tap stubs), `chains`,
`parallel`, `thermal`, `guards`, `planes_stitched` and `bridges` with an empty
`fails`, the copper verified by KiCad, and `fab_job` with no error, on the
board fab exported the Gerbers from. What still differs between two build
paths is KiCad's own: the creation timestamps inside the Gerbers, the drill
files and `drc.json`, KRT's random uuids inside its step files, and the fab
stamp that hashes the timestamped files.

Locks, measured on buck and c3_usb (`fix3/lock.sh`, each a force build of a
temp copy against a fresh baseline): buck's GND pour hand-copied **verbatim**
into core builds verified with the baseline's copper object for object (0
gone, 0 new) and KiCad's DRC the same by type, where the second round's fix
had rerouted GND and VIN (25 gone, 9 new); the pour, the 5V segment and the
GND via at (12.75, 11.7) locked together builds verified the same way, where
the second round refused it blaming the via line; a hand five-point pour
(`gndpour5`) builds verified with only the pour itself differing. **Every
copper line** of buck's (84) and c3_usb's (362) `layout.gen.py` copied
verbatim into `layout.core.py` builds to fab verified with the baseline's
vias (7, 55) and drill files (11, 59 holes; the third round measured 68 for
59 on c3_usb, a second thermal array placed over the locked one), every
core object once on the board and no DRC error. What is not the baseline's
is pinned rather than hidden
(`tests/test_layout_roundtrip.py::EXTRA_WELD_STUBS`, buck 1, c3_usb 5): with
every path locked, the pour step's own pad-centre weld stubs (KRT
`route_planes.py`'s "#107" corridors, under 1 mm, a KiCad warning at most)
survive, because the relaxer, which absorbs them into the redrawn paths on a
build that owns its copper, cannot redraw across locked copper. A ledger
that must fall when the in-house router replaces KRT.

Hand-written core drawings of all sixteen kinds (`Rect`, `Circle`, `DrawArc`,
`Poly`, `Curve`, `TextBox`, `Image`, `Table`, `Barcode`, `Target`, `Point`,
`Generated`, `Group`, the five `Dimension` kinds, `Text`, `Line`) go through a
blinky build verified, KiCad keeping every authored field
(`tests/test_layout_roundtrip.py::test_every_drawing_kind_written_by_hand_in_core_survives_kicad_s_save`),
and every line `layout.gen.py` writes for every probe loads back alone to the
object it was written from (`test_every_gen_line_of_every_probe_loads_back_alone_equal`:
a `Dimension` line's positional `pts` and a `Table` line's positional
`column_count` are accepted by the constructors, and a `Generated`'s point
lists are tuples on both sides).

The third review's harnesses (`scratchpad/step1b/refute-*-r3`) fail before
these fixes and pass after them, each pinned by a test in the "third review"
section of `tests/test_layout_roundtrip.py`; the reproductions and the
after-runs are under `scratchpad/step1b/fix3/{before,after}`, and the rerun of
every harness, the five-board table and the suite on the final tree under
`scratchpad/step1b/fix3/retry` (2026-09-24, same numbers).

## Fourth review (2026-09-24): four structural fixes

The fourth adversarial round found defects in four classes; each was closed by one structural
change, not by patching symptoms. The harnesses are under `scratchpad/step1b/refute-*-r4/`, the
reruns against this tree under `scratchpad/step1b/fix5/after2/`, and each class is pinned by a test
in the "fourth review" section of `tests/test_layout_roundtrip.py`.

### C1 — every file a stage reads is stamped; the sidecars are regenerated

The seed board and the `.kicad_pro`/`.kicad_dru`/`.kicad_prl` beside every board were build outputs
that the place stage, KRT, `kicad-cli pcb drc` and fab read, and none was stamped. Measured before:
`build --upto seed`, edit `board.py` (R1 1k -> 4k7), plain build: the old seed was placed, routed and
fabbed, `bom.csv` said 1k, no stale reason (`h/seedw.py`); `placed/layout.kicad_pro` edited to
clearances 0.02 and every severity `ignore`: the route said `verified` and fab shipped the edited
project file (`siblings2.py` S1p, S2, S3, S3b, S5; `h/sib2.py` W, X).

Two things, and both are in: **regenerate**, then **stamp as the floor**.

- `project.write_sidecars(design, job, pcb)` writes the `.kicad_pro` (`seed.emit_pro`) and the
  `.kicad_dru` (`apply.write_dru`) from the compiled job beside every board a stage writes for a
  later reader — the seed, the placed board, `routed/core.kicad_pcb`, `router.kicad_pcb`, the
  `00_*` start board, every pcbc `patterns_*`/`relax` step output, the emitted board, the fab
  board — and unlinks any `.kicad_prl` lying there. Nothing is copied forward, so there is nothing
  between stages to tamper with. KRT's own step outputs keep the sidecar KRT wrote (its
  `INRUN_FLOOR_SYNC`), contained inside the route step as before; pcbc's own steps hand the next
  KRT step the compiled project, as before. An honest build's bytes are unchanged: seed, placed,
  routed and fab `.kicad_pro` were already byte-equal (`emit_pro` and `apply._apply_pro` agree).
- Stamps: `seed/inputs.json` (new: `board`, `parts`, `modules`, the seed board, its sidecars);
  `placed/inputs.json` gains the seed board it placed from, `schematic.kicad_sch` (the sch stage
  has no directory of its own, so its output rides here) and the placed sidecars;
  `layout/<name>/inputs.json` gains the routed board's sidecars (`.kicad_pro`, `.kicad_dru`, and
  the `.kicad_prl` KiCad wrote). `stale_reason` checks the seed stamp at every stage, so a
  `board.py` edit after `--upto seed` is `board.py changed since the board was seeded` and a plain
  build re-seeds; every edited file is named, "build output, never edited".

After: `seedw` ships 4k7 (`fix5/after2/seedw.log`); `siblings3` S7 ships 10uF and S8 refuses
`C_NEW` at check (`fix5/after2/siblings3.log`); `sib2` and `siblings2`: every sidecar edit is stale
from the stage that wrote it and the rebuild's sidecars are the job's (`fix5/after2/sib2.log`,
`siblings2.log`).

### C2 — fab equality on the whole board

`fab_copper_diff` compared segments, arcs, vias, zones and `pcbc:` groups by uuid in a dict.
Everything else shipped if changed inside fab (`in_fab2.py`, `in_fab3.py`, `dupuuid_unit.log`): a
`gr_poly`/`gr_line`/`gr_rect`/`gr_text` on F.Cu or B.Cu (a new G36 region in the copper Gerber),
a footprint added (`C_X`), deleted, moved, re-valued or given another LCSC number, a pad's paste
or size, the outline grown, `pad_to_mask_clearance`, the tenting; and a second via or segment
carrying an existing uuid was invisible to the dict and shipped, KiCad re-netting it to GND.

Now `fab.fab_board_diff` decompiles **both** boards completely (`gen.decompile` raises
`gen.DuplicateUuid` on a uuid two layout objects carry; fab refuses it before the uuid re-pin
could hide it) and compares **every top-level node as a multiset**: layout objects with their
uuids (a zone without its fill, which is compared separately, below), footprints with their pads and every other node (`general`, `paper`,
`title_block`, `layers`, `setup`, `property`, `net`, `embedded_fonts`) with KiCad's invented uuids
masked. The only allowed differences are `fab.FAB_WRITES`:

| fab writes | how it is allowed |
|---|---|
| the fiducial footprints `insert_fiducials` adds when the board has none | a footprint on the fab board the routed board lacks passes only when its reference is one fab reported adding this run, it is `Fiducial_1mm_Mask2mm`, on F.Cu, at the reported spot, with exactly one pad on no net (`fab._is_fab_fiducial`). The place stage already inserts the fiducials, so on every board measured fab adds none |
| a footprint's silk Reference (`silk_job`) | the `(property "Reference" ...)` node's `at`, `hide` and `effects` are masked; everything else in the footprint must be equal |
| its own `.kicad_pro`/`.kicad_dru` | not board nodes; written from the compiled job |

**Superseded by the fifth review** (below, C2): the canonicaliser now keeps order, fab writes no silk
and keeps every uuid, and the only allowed addition is a fiducial fab appended to a board that had
none, compared exactly against KiCad's save of it. The two rows above for fiducials and silk no
longer describe the code.

A zone's fill is the one thing on the board that is not compared node for node: fab refills every
zone (`--refill-zones`), and two KiCad refills of one board differ by a nanometre on a vertex
(`tests/fixtures/planes`: 492 vs 491 vertices on the F.Cu pour, seven points 1e-6 mm apart, 2.5e-7 mm2;
0 on each of the five boards), which failed the planes fixture's fab once the fill was part of the
node. The fill is KiCad's function of everything else, which *is* compared node for node, so it is
compared by layer as (island count, total area) to `FILL_AREA_TOL` = 0.01 mm2 — the area of a 0.1 mm
track 0.1 mm long; a filled vertex moved 1 mm on buck is refused (`test_fab_refuses_every_change_outside_its_own_allowlist`).
**Superseded in the seventh round (M3):** the tolerance admitted an area-neutral rule edit that moved a
pour to 0.0105 mm from another net's pad. The fill is now compared exactly; the planes fixture's
nanometre came from comparing KiCad's *first* fill of the emitted board with a *refill*, and the route
stage now hands fab the refill's fixed point (see "Seventh round").

Anything else naming the node is a refusal (`the fab board is not the routed board: footprint C_X is
on the fab board and nothing routed it ...`, `(setup) is on the routed board and not on the fab
board as it was`, `the fab board is not the routed board: the fab board carries a uuid twice: uuid
... is carried by 2 layout objects (via, via)`). After: `fix5/after2/in_fab3.log`, `in_fab2.log` —
every mutation refused, no Gerbers, except `identity` (nothing changed) and `teardrop_zone`, a
KiCad teardrop zone inserted inside fab: KiCad's own refill deletes it (the fab board carries no
`(teardrop` and no F.Cu region), so the board fab ships is the routed board and `ok=True` is right.

### C3 — validate at load, never a traceback after routing

Every word-valued field of a layout object is checked at load against KiCad's own word list,
`layout_prims.WORDS` — one source: the drawing schemas' `Atom.words` come from it, `language.Via`
and `language.Pour` check against it, and this table is printed from it. The lists are KiCad's
parser messages narrowed to what its **save keeps**, measured with `kicad-cli pcb upgrade` on
`tests/fixtures/kicad10/kicad10_words.kicad_pcb` (`make_kicad10_words.py`):

| field | KiCad's words the save keeps | left out means |
|---|---|---|
| `stroke_type` | `solid`, `dash`, `dash_dot`, `dash_dot_dot`, `dot`, `default` | - |
| `justify` | `left`, `right`, `top`, `bottom`, `mirror` | - |
| `fill` | `hatch`, `reverse_hatch`, `cross_hatch` | `True`/`False`; `fill.plain` = `yes`, `no`, `solid`, `none` are read as those |
| `dimension.kind` | `aligned`, `orthogonal`, `radial`, `leader`, `center` | - |
| `dimension.style.arrow_direction` | `inward`, `outward` | - |
| `dimension.format.units`, `dimension.format.units_format`, `dimension.style.text_position_mode`, `dimension.style.text_frame` | `0`, `1`, `2`, `3` | - (fifth review: KiCad clamps a larger value on load) |
| `barcode.kind` | `qr`, `microqr`, `code39`, `code128`, `datamatrix` | - |
| `barcode.ecc_level` | `L`, `M`, `Q`, `H` | - |
| `barcode.ecc_level.microqr` | `L`, `M`, `Q` | - (KiCad's save turns a micro QR's `H` into `Q`; a code39, code128 or datamatrix carries no level) |
| `target.shape` | `plus`, `x` | - |
| `generated.type` | `tuning_pattern` | - |
| `generated.tuning_pattern.initial_side` | `default`, `left`, `right` | - |
| `generated.tuning_pattern.tuning_mode` | `single`, `diff_pair`, `diff_pair_skew` | - |
| `via.kind` | `through`, `blind`, `buried`, `micro` | - |
| `via.tenting` | `yes`, `no`, `none` | inherit the board's; a block whose every side is `none` is dropped by KiCad's save and refused at load (one side `none` beside a set side is kept, probe 2) |
| `via.covering` | `yes`, `no`, `none` | - |
| `via.plugging` | `yes`, `no`, `none` | - |
| `via.padstack.mode` | `front_inner_back`, `custom` | - |
| `via.post_machining.mode` | `counterbore`, `countersink` | - |
| `pour.hatch` | `none`, `edge`, `full` | - |
| `pour.connect` | `thermal`, `solid`, `none`, `thru_hole_only` | - |
| `pour.fill_mode` | `hatch` | solid (KiCad writes no `(mode)` and drops `(mode polygon)`) |
| `pour.smoothing` | `chamfer`, `fillet` | none (KiCad drops `(smoothing none)`) |
| `pour.hatch_border_algorithm` | `hatch_thickness`, `min_thickness` | - |
| `pour.island_removal_mode` | `0`, `1`, `2` | - |
| `pour.hatch_smoothing_level` | `0`, `1`, `2`, `3` | - |
| `pour.keepout` | `allowed`, `not_allowed` | - |
| `pour.teardrop_type` | `padvia`, `track_end` | - |

A value outside its list is refused at load naming the line (`layout.core.py:1: Pour('LED'):
fill_mode must be one of 'hatch' (or leave it out), got 'solid'`). Before, 8 of `coreprobe5.py`'s
15 cases routed a whole board and died in a `RuntimeError` out of `kicad-cli`, and 3 more were refused
only after routing; after, 14 of 15 are refused at load naming the line, and the fifteenth,
`via_layers_reversed` (`Via("GND", (20, 5), layers=("B.Cu", "F.Cu"))`), is no longer "the router
dropped it": its layers load as (F.Cu, B.Cu), and the build refuses it for what it is, a via at
(20, 5) that joins nothing (`via_dangling`, naming the line, with the move) (`fix5/after2/coreprobe5.log`).

Also at load: a via's `layers` are put in the copper stack's order, (top, bottom)
(`layout_prims.stack_order`), and a zone's `layers` in KiCad's layer-id order (`lset_order`:
F.Cu, B.Cu, In1.Cu, In2.Cu, ...), which is what KiCad's save writes — so a rule area written in
stackup order on node is no longer refused after routing as "the router dropped it"
(`coreprobe7.py`; `fix5/after2/coreprobe7.log`). A `fill` of `solid`/`none` is read as
`True`/`False`. A `uuid=` is checked (`uuid_check`): an uppercase one is refused naming the
lowercase KiCad keeps, anything not lowercase 8-4-4-4-12 hex is refused naming the uuid. A string
with a character beyond the BMP (a net `LED😀`) is written into `layout.gen.py` as `\U0001f600`
(`gen._py_str`), never as the UTF-16 surrogate pair `json.dumps` wrote, which Python read as two
lone surrogates so emit refused its own output (`cb_net.py`; `fix5/after2/cb_net.log`). A core
`Group` over a piece a pattern owns is refused naming both groups (KiCad keeps an item in one
group; the guard compared ids against uuids and never fired: `coreprobe6.py`).

Two values KiCad's save rewrites are refused at load too (they were refused only after routing,
naming the field): a track's `solder_mask_margin` must be a number and is kept by KiCad only with
`solder_mask=True` (`coreprobe5.py` `seg_width_string`: `solder_mask_margin="0.05"` routed the board,
then "written 0.05, KiCad's save has None"); and a copper member of a `Group(locked=True)` must say
`locked=True` itself, because KiCad's save locks it (`coreprobe6.py` `free_seg_locked_group`).

### C4 — lock idempotence

Measured before (`refute-determinism-r4/L/each/*/results.jsonl`, one build per copper line of the
baseline `layout.gen.py` copied alone into `layout.core.py`): buck 84 lines, 22 pattern pieces
(members of a `pcbc:` group: 10 SAME, 12 DIFF) and 62 KRT pieces (21 SAME, 39 DIFF, 2 REFUSED);
c3_usb 362 lines, 104 pattern pieces (28 SAME, 72 DIFF, 4 REFUSED) and 258 KRT pieces (89 SAME,
116 DIFF, 51 REFUSED, 2 CRASH); blinky 9 lines, 1 DIFF (the tap via). The mechanism, read off
blinky: the locked tap via sat in D1's exit lane before the pre-stage hop pattern ran, the hop
refused, and KRT routed LED at 0.127 mm instead — the lock was re-placed around.

**Adoption by reproduction** (`layout_job`, `LOCK_PASSES`; the fifth review made it equality in every KiCad field, `adopt_key`, below): pass 1 routes with no core copper on
the board; every core line the KiCad-saved router board carries by `gen.signature` (kind, net,
layers, geometry) is adopted — it is a piece the build draws anyway, and it keeps the role group,
uuid and id it has on that board. The lines not reproduced are the real locks: they go on
`core.kicad_pcb` `(locked yes)` and the route runs again; the locked set only grows; after
`LOCK_PASSES` passes every core line is locked (the pre-review behaviour, which always terminates).
The route step records `lock_passes`, `adopted` and `locked`. So a verbatim gen line locked alone
is the baseline by construction, pattern piece or KRT piece, and the every-line lock is one pass.
Measured after (`fix5/after2/L_*.log`, `fix5/L/<mode>/<board>/results.jsonl`):

| board | mode | copper lines | pattern pieces | KRT pieces | pattern verdicts | KRT verdicts | cases run |
|---|---|---:|---:|---:|---|---|---:|
| blinky | each | 9 | 5 | 4 | {'SAME': 5} | {'SAME': 4} | 9 |
| buck | each | 84 | 22 | 62 | {'SAME': 22} | {'SAME': 62} | 84 |
| c3_usb | each | 362 | 104 | 258 | {'SAME': 104} | {'DIFF': 4, 'SAME': 254} | 362 |
| blinky | all | 9 | 5 | 4 | {'SAME': 1} | {} | 1 |
| buck | all | 84 | 22 | 62 | {'SAME': 1} | {} | 1 |
| c3_usb | all | 362 | 104 | 258 | {'SAME': 1} | {} | 1 |
| node | all | 366 | 158 | 208 | {'SAME': 1} | {} | 1 |
| ds2_addon | all | 258 | 30 | 228 | {'SAME': 1} | {} | 1 |

Also `buck net` (every copper line of one net locked together, 8 nets): 8 of 8 SAME, `VIN` included,
which the fourth review saw refused by a `too_many_vias` item naming no core line
(`fix5/after2/L_buck_net.log`). The every-line locks of node and ds2, refused before (a KRT stub
the lock turned into a core `track_dangling`), are SAME. The driver is `fix5/run_all2.sh`; the baseline
each lock is compared with is a fresh build of the same tree (`fix5/A`), and the comparison is
`lockrun.py`'s, byte-identical to the fourth review's own copy (`refute-determinism-r4/lockrun.py`):
copper by signature both ways, via and drill counts, uuids of every common object, DRC by type,
`copper: verified`, every gate.

The one residual is KRT's: c3_usb's router writes two 0.127 mm pair stubs twice each under two
uuids — `USB_DN` (19.25, 22.44)-(19.25, 22.45) and `USB_DP` (19.7, 22.4)-(19.75, 22.45), identical on
every c3_usb build measured — and normalise replaces every copy of a core object with the one core
line, so locking either copy of either stub ships it once: **4 of c3_usb's 362 lines** (all KRT
pieces, 0 pattern pieces) are not the baseline, by exactly that one object each (`base_only=1`, one
`track_segment_length` warning fewer). Pinned by its cause: `tests/test_layout_roundtrip.py`
`LOCK_RESIDUAL = {"buck": 0, "c3_usb": 2}` (duplicated objects on the baseline board). The build does
not deduplicate KRT's copper; doing so would make these four lines idempotent too, at the price of
two fewer segments on every c3_usb build.

Two more C4 facts: the build never ships a dangling track or via — KiCad's `track_dangling` /
`via_dangling` on the router's own copper is removed before emit and KiCad asked again
(`layout_job._drop_dangling`, recorded in `dangling_removed`; node's 0.1 mm `USB_DN` end, the DS2
Addon's `GPIO0` fanout via KRT joined on one layer only and its stub), so a stub never reaches
`layout.gen.py` and the every-line lock is no longer refused on node and ds2. Measured against a
build without the sweep (`fix5/measure_moved.py`, `refute-determinism-r4/A` against `fix5/T1`):
node loses exactly the 0.089 mm-wide, 0.1 mm `USB_DN` segment (30.2, 38.3)-(30.3, 38.3) and with it
one `track_dangling`, two `track_angle`, one `track_segment_length` and one `track_width` warning
(soft rule `width_usb` 46 -> 45); ds2 loses `U1.2`'s `GPIO0` fanout via at (20.87, 16.85), joined on
F.Cu by its 1.78 mm escape and on B.Cu by nothing, then the escape it orphaned — one `via_dangling`
warning fewer, the `GND` B.Cu pour 1008.42 -> 1009.0 mm2 (the via's antipad), `fanout` 9 -> 8 vias
and segments; KiCad's unconnected count is 0 before and after on both, every gate passes. The
copper census is counted on the emitted board (`build.emitted_copper_bar`), so it no longer counts
the swept pieces. A **guard** is never swept (`NEVER_SWEPT`): the sweep first took every shield
track and then every stitch via off `tests/fixtures/guard` and the guard gate read "pcbc wrote no
guard copper". A shield now ends on its outermost stitch vias (`stitch._between_vias`: a track end
past its last via touches nothing, KiCad's `track_dangling`), and a stretch that holds one via is
dropped as coverage, since no track of it can end on a via at both ends. Moved on the fixture:
`SIG` 10 -> 9 vias, 24.898 -> 17.5 mm guarded on the placed board (a 3.518 mm one-via stretch
dropped, the two others cut to their vias); `SIG2` 2 -> 0 vias (both its stretches held one); the
routed fixture 11 -> 9 guard vias, "2 of 2" -> "1 of 1 shield(s) welded", no dangling item left. And every pour step
passes `--board-edge-clearance <stackup.edge_clearance>` explicitly, so the plane inset never depends
on which sibling `.kicad_pro` a KRT step happened to leave (blinky's pour was inset 0.5 mm against a
declared 0.3; it is 0.3 now and its area is 942.54 mm2, `test_examples_fab.PLANES`).

### Minors

- `schematic.kicad_sch` is stamped (in `placed/inputs.json`; stale from seed).
- A core uuid KiCad would rewrite is refused at load naming the uuid and what KiCad keeps.
- A core `Group` over a pattern's piece names both groups.
- A core `Seg(locked=False)` inside a core `Group(locked=True)` is refused **at load** naming the
  member's line (`'s1' is a member of the locked group 'g' ..., and KiCad's save locks it`); so is a
  `solder_mask_margin` that is not a number or sits on a track with `solder_mask=False`. Measured on
  copper members only; a drawing member of a locked group is not checked (no probe measured one).
- The seed's and `apply`'s `(fill none)` outline spelling is what KiCad reads as `(fill no)`; left
  as is (KiCad re-saves every board before the decompiler reads one).
- Not done, with the reason: a per-net lock refused by a DRC item naming no core line
  (`too_many_vias` on buck's `novia_sw`) is KiCad naming a connection, not an item, and with
  adoption the case no longer arises for a verbatim line (measured: buck `each` all SAME); a KiCad
  group with a footprint member does not decompile (a footprint is not a layout object, by the
  language's goal); `kicad-cli` 10.0.6's segfault on a rotated table cell with an outline font is
  KiCad's; `zone_layer_connections` stays a field (KiCad writes it; the census bar is "every field
  KiCad writes"); a small core `Pour` on the plane net on a layer without a router plane is by
  design the plane there (`docs/direction.md` §2).

### Measured round trip (2026-09-24, fourth review)

Fresh builds of temp copies of the five boards, twice each from two different absolute paths
(`scratchpad/step1b/fix5/make_table.py`, run by `run_all2.sh` into `fix5/T1` and `fix5/T2/deeper`; table `fix5/after2/table.md`), compared with the whole-board comparison
above (`router_vs_emitted`: copper fields by uuid plus `build.non_layout_diff`;
`emitted_vs_fab_whole_board`: `fab.fab_board_diff`):

| board | seg | via | zone | drawings | router_vs_emitted | emitted_vs_fab_whole_board | drc_router_eq_emitted | drc | dangling_removed | lock_passes | copper | gate_fails | fab_ok | gerbers | fab_unconnected | two_paths_identical | two_paths_differ | fab_board_to_gen_bytes | stale_after | secs |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| blinky | 4 | 1 | 4 | 3 | 0 | 0 | True | 0 errors, 5 warnings | 0 | 1 | verified | {} | True | 10 | 0 | True | [] | True | ('fab', None) | 5.8 |
| buck | 73 | 7 | 4 | 9 | 0 | 0 | True | 0 errors, 70 warnings | 0 | 1 | verified | {} | True | 10 | 0 | True | [] | True | ('fab', None) | 12.9 |
| c3_usb | 302 | 55 | 5 | 48 | 0 | 0 | True | 0 errors, 283 warnings | 0 | 1 | verified | {} | True | 10 | 0 | True | [] | True | ('fab', None) | 20.7 |
| node | 274 | 86 | 6 | 72 | 0 | 0 | True | 0 errors, 271 warnings | 1 | 1 | verified | {} | True | 12 | 0 | True | [] | True | ('fab', None) | 30.0 |
| ds2_addon | 227 | 27 | 4 | 21 | 0 | 0 | True | 0 errors, 113 warnings | 2 | 1 | verified | {} | True | 10 | 0 | True | [] | True | ('fab', None) | 19.7 |

Every board: router vs emitted 0 differences (copper fields by uuid, non-layout nodes), emitted
vs fab 0 differences on the whole board, KiCad's DRC by type equal router = emitted, 0 errors,
0 unconnected on the fab board, every gate passed, fab ok, one route pass, two builds from two
absolute paths byte-identical in the seed board, its `.kicad_pro` and stamp, the placed board, its
sidecars and stamp, `layout.gen.py`, the routed board, its sidecars and stamp, the fab board, its
sidecars, `bom.csv`, `cpl.csv` and `report.json`, and the fab board decompiles to
`layout.gen.py`'s bytes; `stale_reason` after each build is `("fab", None)`. `dangling_removed`:
node 1 (the `USB_DN` stub), ds2 2 (the `GPIO0` fanout via and its stub), which is why node has 274
segments (275 before) and ds2 27 vias (28 before).

Suite, `cd ~/pcbc && PCBC_REQUIRE_KICAD=1 PCBC_REQUIRE_KRT=1 .venv/bin/python -m pytest -q -p no:cacheprovider`, run last by `run_all2.sh` with nothing else building: **898 passed in 940.83s** (`fix5/after2/suite.log`).

## Fifth review (2026-09-24): the four classes, second round

The fifth adversarial round found defects in the same four classes. Each class was closed by one
structural change; the reviewers' harnesses are under `scratchpad/step1b/refute-*-r5/` (their logs are
the "before"), rerun unchanged or minimally adapted against this tree under
`scratchpad/step1b/fix6/rerun/<lens>/` (the "after"); `fix6/probe.py` with `fix6/facts*.py` is the
KiCad-behaviour battery that measured every rule below (`kicad-cli pcb upgrade --force`, 10.0.6). Each
class is pinned in `tests/test_fifth_review.py`.

### C1 — every file a stage reads or writes is stamped, and stale from the stage that wrote it

- **The schematic has its own record**, `layout/<name>/schematic.inputs.json`, written by the sch
  stage alone (`build.sch_stage`, shared by `pcbc build` and `pcbc sch`) when KiCad read its netlist
  and ERC passed: the `board.py`/module/part hashes it was drawn from and its own hash. It used to ride
  in `placed/inputs.json`, written by `_stamp_place` for whatever file was on disk, so `pcbc pcb` then
  `pcbc build` never ran the sch stage (no schematic, or yesterday's saying R1 1k beside a BOM saying
  4k7) and said ok. Missing, drawn from another `board.py`, or edited: stale from seed, so the sch stage
  runs. Before (`refute-truth-r5/logs/pcbjob_sch.log`): P1 plan `['route', 'fab']`, no schematic after;
  P2 schematic R1 1k, BOM 4k7. After (`fix6/rerun/truth/logs/pcbjob_sch.log`): P1 plan `['sch', 'place',
  'route', 'fab']` and a schematic; P2 schematic R1 4k7, BOM 4k7. The CLI (`logs/cli_build.log`): one
  `"stage": "sch"` entry, the schematic exists.
- **The fab stage clears `fab/` first**, as the route stage always did: a hand-added
  `fab/gerbers/layout-In1_Cu.g2` used to survive a fab-only rerun, be counted as an eleventh Gerber and
  be stamped as fab's own (`sweep.log` `fab_inject_gerber`: after, the rebuilt package equals the
  honest reference). `fab_outputs` leaves out only the stamp itself, by its path, so
  `fab/gerbers/inputs.json` is stamped (`logs/inputs_name.log`: stale from route, gone after the rebuild).
- **The seed stage keeps no sidecar it did not write** (`build.seed_stage` unlinks a `.kicad_dru` or
  `.kicad_prl` beside the seed board), so its stamp records only its `.kicad_pro`.
- **Stale from the stage that wrote the file**: a placed board or placed sidecar edited is stale from
  sch now (place and later run), not from check (which re-seeded and redrew the schematic for nothing):
  `sweep.log` `placed_move` and `placed_inject_prl` plan `['place', 'route', 'fab']`.
- **`pcbc review` reads a stage's files only when that stage's stamp matches** (`build.stale_reason`),
  prints the stale reason in place of the copper and BOM verdicts, and draws its own schematic under
  `layout/<name>/review/`, never over the build's. Before (`review_stale.log`): the hand-edited fab board
  "KiCad DRC clean ... as board.py says", `bom.csv`'s hand-edited R1 99k shown; after: the routed board
  plotted, the copper note "not judged — the build's outputs are stale (fab/bom.csv is not the file the
  fab stage wrote ...)", no BOM row.

The sidecars stay regenerated (fourth review) and stamped; the route stage now also regenerates them
beside every KRT step's input (C4).

Not done, and why: `docs/direction.md` §2 says no stage may take a fact out of a `.kicad_pcb`; the
place stage reads the seed board (`place.place_job`, footprint geometry and poses) and the route stage's
pattern scene reads the placed board (`route.py`, `layout_job.py`). Both reads are of stamped build
outputs, so the stamping rule holds; the sentence does not. Amending the owner's decision record, or
rebuilding those facts from `board.py` and the part files in Python, is the owner's call and not this
round's.

### C2 — fab is held to the routed board node for node, in order, uuids included

- **The canonicaliser keeps order** (`fab._canon_node`): atoms where they stand, child nodes in file
  order. The fifth review's BLOCKER: children and atoms were sorted, so `(size 1 1.45)` equalled
  `(size 1.45 1)`, `(at 29 14)` equalled `(at 14 29)` and a square equalled a bowtie; a pad turned inside
  fab shipped ten Gerbers with a new aperture. Both boards are KiCad saves of one board: the reviewers'
  own ordered diff (`fabdiff.py`) of routed against fab now finds **no difference at all** on any of
  the five boards (`fix6/rerun/arbiter/fabdiff_*.full`; before, buck showed 247 changed uuids and three
  moved references).
- **Fab writes no silk.** The references are placed by the place stage and are what ships; fab's
  second `silk_job` (without the decoupling-cap list, in another footprint order) moved three of buck's
  references and needed an allowlist admitting any reference position and any text size.
- **Fab keeps every uuid the routed board carries** (`pin_all_uuids` with `keep` = the routed board's
  uuids); it used to re-pin every footprint's and child's uuid (58 on blinky, 727 on node), which the
  comparison had to mask.
- **The only thing fab may add is a fiducial, and only to a board that has none**: `insert_fiducials`
  reports only what it appended (it used to report the existing FID1-3 as inserted, which excused a
  second FID1 stacked on the first), and the expected board (`fab.expected_fab_board`) is the routed
  board plus those fiducials **as KiCad saves them**, so an inserted fiducial is compared exactly, not
  by shape. The place stage inserts the fiducials on every board measured; fab inserts none.
- **A uuid any two items carry is a refusal** on either board, naming the items (a dimension and its
  own text are one item: KiCad gives the text the dimension's uuid).
- **A refused fab leaves no package**: `bom.csv` is written only after the board passed; on any
  refusal the BOM, CPL, position file, Gerbers, drill files and maps go and the board is kept as
  `refused.kicad_pcb` (`fab._clear_refused`).

`fab.FAB_WRITES` is now three lines: the fiducials appended to a board with none (compared exactly),
the zones' refill (compared by layer as island count and area, `FILL_AREA_TOL`; exactly since the
seventh round, M3), and fab's own `.kicad_pro`/`.kicad_dru` (byte-checked against the compiled job since
the seventh round). Every other difference is refused naming the node.

Before/after, the reviewers' in-fab mutations (`in_fab5.py`, applied right after fab's own edits; the
mutation point moved from `silk_job`, which fab no longer calls, to `check_job`, the next call on the
same board: `fix6/rerun/arbiter/in_fab6x.py`), on buck: of 33 cases, 30 applied mutations are refused (29 by the whole-board comparison naming the node, or a uuid carried twice naming both footprints; 1, a fourth copper layer, by KiCad's load), `identity` ships, `net_table_add` ships the routed board unchanged (KiCad's save drops the added `(net 99 "EVIL")` row, as it dropped the fourth review's `teardrop_zone`), and `pad_size_swap_C_IN2` was not applied (the harness's own assertion: the first `(size` it finds has equal numbers; `_p1` and `_both` are the same mutation, both refused) — before (`refute-arbiter-r5/infab5..8_buck.log`), `pad_size_swap_C_IN2_p1`, `silk_line_swap_C_IN2`, `fid_dup_paste_nocrtyd`, `fid_dup_mask5_nocrtyd` and `ref_effects_huge_C_IN2` shipped ten Gerbers `ok=True` and the outline swap was refused only by its fill, naming no `gr_rect` (it names the `gr_rect` now) (`fix6/rerun/arbiter/infab_all_buck.log`); on c3_usb:
`c3_prim_vertex_swap` (shipped `ok=True` before), `c3_shell_rotate` and `c3_prim_bowtie` (stopped before only by KiCad's DRC, `copper_equal` True) are each refused naming footprint J1, `identity` ships (`infab_all_c3.log`). The reviewers' pair test (`unit_diff.py`): the square-to-bowtie `gr_poly`, the
coordinate-swapped `gr_line` and the swapped end are each two differences now (0 before), the control
two as before, the unchanged pair 0.

### C3 — a core line is refused at load naming the line, or KiCad keeps it as written

Every rule below was measured with `fix6/probe.py` (a core line emitted onto a probe board and saved by
`kicad-cli pcb upgrade --force`); the fifth review's own batteries are `refute-primitives-r5/cases_val.py`
and `refute-roundtrip-r5/cases_*.py`.

**Refused at load, naming the line** (`language.py`, and `layout_job.validate_core` for what needs the
board):

| what | why (KiCad 10.0.6, measured) |
|---|---|
| a yes/no field or flag that is not `True`/`False` (`solder_mask="no"`, `locked="no"`, `hide="no"`, ...) | `bool("no")` is True: the BLOCKER shipped a mask opening along a track |
| a number given as a string, NaN or infinity; a string field given as another type | read as KiCad would not |
| a word outside `layout_prims.WORDS`, **inside dict fields too** (a dimension's `style['arrow_direction']`, a table border's `type`) | `kicad-cli` refuses the board ("Expecting inward or outward") |
| a dimension's `format['units']`/`units_format` outside 0-3, `style['text_position_mode']`/`text_frame` outside 0-3 | clamped on load |
| a layer the board's layer table does not have (drawing `layer`/`layers`, table cells, dimension text, via `zone_layer_connections`, `backdrill`, `tertiary_drill`, custom `padstack`, pour `hatch_position`); `User.1`..`User.45` are always there | "items were found on undefined layers" |
| a drawing's `layers=` other than `('F.Cu', 'F.Mask')` or `('B.Cu', 'B.Mask')` (a mismatched side, an inner layer, three layers); `layer=` and `layers=` both | rewritten to another pair, or to `(layer)` |
| `solder_mask=True` on an inner-layer track | "Expecting no mask layer when track is on internal layer" |
| two words on one `justify` axis, or one twice | KiCad keeps the last |
| `hide=True` on a text, text box, table cell or dimension text | KiCad 10 writes no `(hide)` on board text |
| a text height or width under 0.001 mm; a stroke thickness of 0 or less | raised to 0.001; written as the default |
| a `stroke_color`/`color` without its alpha | "Expecting alpha" |
| a `Rect` radius over half the short side; a `Poly` point written twice in a row; a negative `Image` scale; image data that is not base64 or not a PNG/JPEG | clamped, dropped, dropped, "Failed to read image data" |
| a barcode under 0.01 mm; `ecc_level` on code39/code128/datamatrix; `H` on a micro QR | raised; dropped; turned into `Q` |
| `height` on a radial/leader/center dimension, `orientation` on any but orthogonal, `leader_length` on any but radial; `arrow_direction`/`extension_height` on a radial/leader/center one, `text_frame` on any but a leader; a missing `arrow_direction`/`extension_height` on aligned/orthogonal | dropped, or the board refused ("Expecting '('"), or filled in |
| a dimension whose text's `locked`, layer or uuid differs from the dimension's | one flag, one layer, one uuid (the text's) |
| `Pour(filled_areas_thickness=...)`; `Pour(teardrop_type=...)`; a `placement` value of the wrong type | never kept; the zone dropped whole; the zone dropped |
| `zone_layer_connections` without `remove_unused_layers=True` | the list dropped |
| a custom `padstack` with an `F.Cu` entry (that is `size=`) or without every copper layer below F.Cu; a `front_inner_back` one not exactly `Inner` and `B.Cu` | the entry dropped; the rest filled in; `Inner` written |
| `teardrops` with a key KiCad has not, or a value of the wrong type | a traceback out of the emitter before |
| a group member named twice; a `lib_id` that is not one `library:item` or holds `"`, `\`, or a control character; a tuning pattern's property KiCad has not, or of the wrong type, `initial_side`/`tuning_mode` outside their words, or a layer other than its members' | dropped; the board refused; dropped, rewritten or moved |
| a member of a locked group, **transitively** (a nested group, a drawing, a table and its cells, a dimension, a barcode) that does not say `locked=True` | KiCad's save locks it (only direct copper members were checked before) |
| a NUL or a lone surrogate in any string | cut at the NUL; no file can hold a surrogate (a `UnicodeEncodeError` traceback before) |
| an unbalanced parenthesis in any string of a drawing **on copper** | KRT's reader is not string-aware: it dropped pcbc's copper and once hung 20 minutes; a drawing off copper never reaches the router now |
| a uuid two core objects carry (a table cell's and a dimension text's included, derived ones included), or one the placed board carries (a footprint, the outline) | a `DuplicateUuid` traceback after routing, or the outline replaced |

**Read as KiCad keeps it** (one fact, KiCad's spelling): a length at 1 nm and a double to ten
significant digits (`width=0.2000004` is 0.2, an angle `12.345678901234567` is `12.3456789`);
`justify` in KiCad's order (horizontal, vertical, mirror); a drawing's `layers` as (copper, mask), and
one layer as `layer=`; a via's `tenting`/`covering`/`plugging` side left out as `none`; `teardrops`
with KiCad's defaults for the keys left out; `zone_layer_connections` and a custom `padstack` in the
copper stack's order; a pour's `layers=("*.Cu",)`/`("F&B.Cu",)` spelled out in KiCad's order, and one
layer as `layer=`; a pour's `priority=0`, `name=""` and `net=""` as unset; `keep_text_aligned=False`
as unset; a dimension's text on the dimension's layer and lock when the text dict leaves them out; a
`Rect` radius of 0 as none.

Returned, never raised: every `ValueError` out of the route stage is a refusal (`build.build_job`), a
`DuplicateUuid` or a `kicad-cli` load failure inside the route stage is a refusal naming what happened
(`layout_job`), and a KRT step past `PCBC_KRT_TIMEOUT` (default 1800 s) is stopped and the route refused
naming the step. A core uuid that equals the uuid of a piece pcbc's patterns draw (known only once they
have drawn it) is refused in normalise naming the line and the piece.

Before/after on the reviewers' batteries: the words battery (`fuzz.py cases_words.py W`, 132 full builds) went from 21 tracebacks, 28 refused after routing, 36 refused before it and 47 built to **0 tracebacks, 3 refused after routing, 87 refused before it, 42 built** (`fix6/rerun/roundtrip/W/results.json`; the 3 are two cases of a core `Group` over a line the build adopts as a pattern's piece and one core pour whose plane fails the plane gate, named); the ten yes/no-as-string cases that built with the value inverted are all refused at load. The uuid battery (U, 13): 2 tracebacks and 3 after-routing refusals to 0 and 2 (`uuid_of_gen_via_on_line`, now refused in normalise naming the line and the tap piece, and `id_gen_id_other_obj`, both after routing by nature); the outline's and R1's uuid are refused at load naming their owner. The parenthesis battery (P2): the two texts KRT could not parse build now (a silkscreen text never reaches the router). The generated-object battery (G): 4 refused after routing to 4 refused at load. The strings battery (S): 1 traceback and 4 after-routing refusals to 0 and 2 (`ALL__cjk`, `ALL__var`: KiCad's DRC finds a barcode's clearance violation, a real board error); `ALL__parens`, which hung KRT for 1198 s, builds in 13 s; `S_withlibid` (a group `lib_id` holding each string; 12 run before, 24 after) went from 8 tracebacks and 4 after-routing refusals to 24 refused at load (none is a `library:item` link), and `E` (3) from 1 traceback and 2 after-routing refusals to 3 refused at load. The primitives build battery (`buildprobe.py`): before, all 26 of its builds were tracebacks (7) or refused after routing (19) (`refute-primitives-r5/buildprobe*.log`); after, of 28, 24 are refused at load and 4 build, `text_angle_long`, `seg_subnm_width`, `rule_area_prio0` and `rule_area_name_empty` being read as KiCad keeps them (`fix6/rerun/primitives/buildprobe.log`). The loader battery through `load_core` and the build's stamp (`fix6/probe.py cases_val.py`, 124 lines): 80 refused at load, 44 kept by KiCad's save field for field, **0** otherwise (the fifth review's own loader, which bypasses `load_core`, `pyside_val.log`: 75 refused, 39 kept, 5 layer names only `load_core` checks, 5 differences that are the build's stamp defaults or a cell uuid KiCad invents where the build derives one). The KiCad-saved probes (`rt5.py`, 52 boards): 49 round-trip field for field, 3 are the documented refusals below.

Residuals, each pinned: a KiCad-saved group whose member is a footprint does not decompile (a footprint
is not a layout object; `tests/test_fifth_review.py::test_a_group_holding_a_footprint_is_a_documented_refusal`);
a KiCad-saved board with two segments sharing a uuid is `DuplicateUuid` (by the language's rule); a
10 kB string builds but takes 40-60 times as long (KiCad's and KRT's time, not a correctness issue);
two classes are refused only after routing by their nature — a core `Group` over a line the build
adopts as a pattern's piece (KiCad keeps an item in one group), and a core id equal to the id
`layout.gen.py` gives another object — each naming the line.

### C4 — adoption is equality, every KRT step routes to the compiled rules, the package is the same bytes

- **Adoption is equality with a piece the build drew**, every KiCad field but the handles (`id`,
  `uuid`; `layout_job.adopt_key`), the signature only finding candidates. A pour with the build's own
  outline and its own clearance, connect mode or thermal gap was "adopted" by signature and shipped
  fields the build never drew (`refute-determinism-r5/adopt` E1-E4 on buck, C1 on c3_usb); it is a
  real lock now. A plane gate that fails on a net and layer a core `Pour` covers names that line
  (`build._core_pour_blame`).
- **Every KRT step reads the compiled job's `.kicad_pro`/`.kicad_dru` beside its input**, written just
  before it runs (`route.route_job`). Without a sibling KRT fell back to its stock 0.3 mm track and
  0.2 mm hole-to-hole, and a sibling existed only when an earlier step had lowered a floor, so the pour
  step's rules depended on how the route came out (`sidecar_probe.log`: c3_usb three GND segments at
  0.3 mm, ds2 four). Pinned by `test_every_krt_step_reads_the_compiled_rules_beside_its_input`. On the
  five boards the routed copper and `layout.gen.py` are byte-identical to the fourth review's baseline
  builds (`fix5/T1`): no route changed.
- **The fab package is the same bytes on every build**: every date KiCad writes into the Gerbers
  (`%TF.CreationDate`, the `G04 ... date` comment), the job file, the drill files, the drill maps and
  `drc.json` is rewritten to `SOURCE_DATE_EPOCH`, or 1980-01-01T00:00:00Z (`fab.pin_dates`; KiCad
  10.0.6 ignores `SOURCE_DATE_EPOCH`, measured). The determinism test now compares the **whole**
  `layout/<name>/` tree; the one class it leaves out is named: the KRT step boards (`routed/NN_*`,
  `routed/pours_in_*`), the router's own chained intermediates inside the route step
  (`docs/direction.md` §2), which carry KRT's random uuids and which nothing reads after the route.

Lock idempotence, measured again with one build per copper line of the baseline `layout.gen.py`
locked alone (`fix6/rerun/determinism/lockrun.py`, `out/L_*.log`): 

| board | mode | copper lines | pattern pieces | KRT pieces | pattern verdicts | KRT verdicts | cases run |
|---|---|---:|---:|---:|---|---|---:|
| blinky | each | 9 | 5 | 4 | {'SAME': 5} | {'SAME': 4} | 9 |
| buck | each | 84 | 22 | 62 | {'SAME': 22} | {'SAME': 62} | 84 |
| c3_usb | each | 362 | 104 | 258 | {'SAME': 104} | {'DIFF': 4, 'SAME': 254} | 362 |
| buck | all | 84 | 22 | 62 | {'SAME': 1} | {} | 1 |
| c3_usb | all | 362 | 104 | 258 | {'SAME': 1} | {} | 1 |
| node | all | 366 | 158 | 208 | {'SAME': 1} | {} | 1 |
| ds2_addon | all | 258 | 30 | 228 | {'SAME': 1} | {} | 1 |

The baseline each lock is compared with is a fresh build of this tree (`fix6/rerun/determinism/A`),
the comparison `lockrun.py`'s (copper by signature both ways, via and drill counts, uuids of every
common object, DRC by type, `copper: verified`, every gate). **The KRT residual is 4 of c3_usb's 362
lines, 0 of its 104 pattern pieces**, unchanged from the fourth review: KRT writes two 0.127 mm pair
stubs twice each under two uuids (`USB_DN` (19.25, 22.44)-(19.25, 22.45) and `USB_DP` (19.7, 22.4)-(19.75,
22.45)), and locking either copy of either ships it once (`base_only=1`, cases L0278, L0279, L0342,
L0343). Pinned by its cause, `tests/test_layout_roundtrip.py` `LOCK_RESIDUAL = {"buck": 0, "c3_usb": 2}`.
Every other line of buck (84), c3_usb and blinky (9) locked alone is the baseline, and every-line
locks of buck, c3_usb, node and ds2 are the baseline. Adoption by equality changed no verdict here:
a verbatim gen line equals the router's piece in every field.

### Minors (fifth review)

Done: every word check goes through `layout_prims.WORDS` and `language._word`, one message form
(`connect must be one of 'thermal', ..., got 'bogus'`), the literal lists in `language.py` and
`gen.py` gone (a barcode's `qrcode`/`data_matrix` spellings are aliases onto its words); the DS2
Addon's header footprint's two identical zero-length courtyard lines, which swapped on every KiCad
save, are dropped from the copy on the board (`seed.drop_degenerate_lines`; the part file in the
owner's tree is untouched); a lone surrogate and a `teardrops` key are refusals at load, never a
traceback; the place stage's comment that fab and review "re-run silk, same answer" is corrected
(neither does now).

Not done, with the reason: `docs/direction.md` §2's "no stage may take a fact out of a `.kicad_pcb`"
(C1 above: two stamped reads remain, the owner's call); a KiCad-saved group with a footprint member
stays a documented refusal rather than a `fp:REF` member (the emitted board's footprint uuids are
re-pinned after KiCad's save, so a member reference would not survive; pinned by a test); a 10 kB
string still takes 5-9 minutes to build (`ALL__big` 293 s, `ALL__bigcjk` 556 s: KiCad's and KRT's
time, the values read back exactly); the seed and placed boards are pcbc's own text, not KiCad
saves, so the decompiler reads their `(fill none)` outline as `fill="none"` beside KiCad's `(fill no)`
(`rt5_five.log`, unchanged from the fourth review's note).

### Measured round trip (2026-09-24, fifth review)

Fresh builds of temp copies of the five boards (ds2 copied from the owner's tree, never built there),
twice each from two different absolute paths (`fix6/run_table6.sh` into `fix6/T1` and
`fix6/T2/deeper dir/x`; `fix6/make_table6.py`, table `fix6/table6.md`):

| board | seg | via | zone | drawings | router_vs_emitted | emitted_vs_fab_whole_board | drc_router_eq_emitted | drc | dangling_removed | copper | gate_fails | fab_ok | gerbers | fab_unconnected | fab_fiducials_inserted | tree_files | two_paths_differ_outside_krt_steps | krt_step_files_differ | fab_board_to_gen_bytes | fab_resave_fixed_point | routed_resave_fixed_point | stale_after | secs |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| blinky | 4 | 1 | 4 | 3 | 0 | 0 | True | 0 errors, 5 warnings | 0 | verified | {} | True | 10 | 0 | 0 | 65 | [] | 4/19 | True | nodes equal, bytes differ | nodes equal, bytes differ | ('fab', None) | 6.1 |
| buck | 73 | 7 | 4 | 9 | 0 | 0 | True | 0 errors, 70 warnings | 0 | verified | {} | True | 10 | 0 | 0 | 73 | [] | 9/27 | True | nodes equal, bytes differ | nodes equal, bytes differ | ('fab', None) | 13.0 |
| c3_usb | 302 | 55 | 5 | 48 | 0 | 0 | True | 0 errors, 283 warnings | 0 | verified | {} | True | 10 | 0 | 0 | 70 | [] | 8/24 | True | nodes equal, bytes differ | nodes equal, bytes differ | ('fab', None) | 20.5 |
| node | 274 | 86 | 6 | 72 | 0 | 0 | True | 0 errors, 271 warnings | 1 | verified | {} | True | 12 | 0 | 0 | 76 | [] | 9/28 | True | nodes equal, bytes differ | nodes equal, bytes differ | ('fab', None) | 30.1 |
| ds2_addon | 227 | 27 | 4 | 21 | 0 | 0 | True | 0 errors, 113 warnings | 2 | verified | {} | True | 10 | 0 | 0 | 67 | [] | 7/21 | True | nodes equal, bytes differ | nodes equal, bytes differ | ('fab', None) | 19.7 |

Every board: the router's board and the emitted board have 0 differences (copper fields by uuid,
every non-layout node); the fab board and the expected fab board have 0 differences on the whole
board, in order, uuids included; KiCad's DRC by type router = emitted, 0 errors; 0 unconnected on
the fab board; every gate passed; fab inserted no fiducial (the place stage's are on every board);
**the whole `layout/<name>/` tree (65-76 files) is byte-identical across the two absolute paths**
except the KRT step boards, named apart (`routed/NN_*`, `routed/pours_in_*`: 4-9 of 19-28 differ, in
KRT's random uuids); the fab board decompiles to `layout.gen.py`'s bytes; KiCad's re-save of the fab
and routed boards is a fixed point node for node (the ds2 header's swapping courtyard lines are gone;
the bytes differ only in the order KiCad writes top-level items, which the file's content does not
depend on); `stale_reason` after each build is `("fab", None)`. The routed copper and `layout.gen.py`
of every board are byte-identical to the fourth review's baseline builds (`fix5/T1`).

Suite, `cd ~/pcbc && PCBC_REQUIRE_KICAD=1 PCBC_REQUIRE_KRT=1 .venv/bin/python -m pytest -q -p no:cacheprovider`,
run last with nothing else building: **990 passed in 1023.12s** (`scratchpad/step1b/fix6/suite.log`).

## Seventh round (2026-09-24): three mechanisms instead of more cases

The sixth review's findings fell into three classes, and each class had been answered case by case
for three rounds. This round closes each with one mechanism; the cases are the evidence, not the fix.
Scratch and every log named here: `scratchpad/step1b/fix7/`.

### M1 — KiCad reads every core line at load (`core_probe.round_trip`)

The explicit load checks (`language`, `layout_job.load_core` / `validate_core`) are pcbc's own table of
what KiCad keeps, and every review found a row missing: a through via on an inner layer (KiCad
rewrites it to `F.Cu`-`B.Cu`), a stroke colour (KiCad stores r, g, b as 0-255 integers), a width of 0
(KiCad's floor is 0.1), `island_area_min` past six decimals, a post-machining angle past one, a
partial `keepout` (KiCad completes it), a `hatch_gap` on a solid pour or a `radius` without smoothing
(dropped), a `margins` or `span` of the wrong length (KiCad cannot parse the board), a group that holds
itself (dropped), a required yes/no left `None` (the emitter cannot write it). Each was refused only
after the whole board had been routed, or not at all.

Now, after the explicit checks and the stamp (so the objects are exactly what the router's board will
carry), `layout_job` hands every core object to `core_probe.round_trip`:

1. the **probe board** is the placed board with every layout primitive and every footprint taken off
   (`core_probe.probe_base`): its header, layer stack, setup, net table and properties. No core object
   references a footprint (a core group may only hold core objects), so none is kept;
2. the core objects are rendered onto it as the emitter renders them; a line the emitter cannot write
   is named with the emitter's message (`layout.core.py:1: (gr_line) (stroke): stroke_type is
   required; KiCad cannot hold the line as written: give the field a value`);
3. `kicad-cli pcb upgrade --force` loads and saves it, beside the compiled job's sidecars;
4. the save is decompiled and every core object compared field for field with what was written
   (`layout_job.diff_objects`, the comparison the route stage already made after routing). A
   difference is `a core line is refused: layout.core.py:N: KiCad's load of the line differs in
   <field> (written X, KiCad's load of the line has Y: write that)`; an object KiCad dropped is
   `... has no such object: KiCad drops it on load (...)`;
5. if KiCad cannot load the probe at all, the objects are **bisected**: one probe board per half, both
   halves at once, down to the lines KiCad refuses, each named with KiCad's own message (the wall
   clock KiCad prefixes to a warning stripped): `layout.core.py:14: KiCad refuses to load the line
   (Failed to load board: need a number for 'bottom margin' in 'probe.kicad_pcb', line 49, offset 23.)`.

**Cost** (`fix7/m1/cost.py`, every gen line of node but its `Place`s, its `pcbc:` groups and its
outline as `layout.core.py`: 366 objects): load 0.08 s, stamp 0.00 s, **round trip 0.38-0.39 s, one
`kicad-cli` call**, three runs on an idle machine. The other four boards' every gen line: blinky 9
objects, buck 84, c3_usb 362, ds2 258 — 0 refusals each, one call each (0.67-0.87 s while other
builds ran). A board with no core file pays nothing.

**It is exact, not strict.** Every copper and drawing line of all five boards' `layout.gen.py` passes
(0 refusals, above; pinned by `test_every_gen_line_passes_kicad_s_reading_in_one_call` on blinky and
buck). Run over the sixth review's 1100 probe cases (`fix7/m1/m1probe.py`, log
`fix7/logs/after_m1probe.log`), every case the probe found `KICAD_REFUSED` (5) or `EMIT_RAISED` (21) is
refused at load; 23 `DIFF` cases are refused; the other 147 `DIFF` cases pass, and every one of them is
a difference of the probe harness's, not of the build's: 91 are fields left `None` that the build's
stamp fills before emit (the harness compared the unstamped object), 56 are the dimension text KiCad
recomputes on every save (`layout_job.comparable` treats it as derived).

**What the explicit rules are now.** The round trip is the authority; the hand rules stay as fast
early refusals with better wording. Which ones it makes redundant was measured, not argued
(`fix7/m1/m1rules.py`, log `fix7/logs/after_m1rules.log`): each object was built legally and then
mutated past its rule, so the rule never ran, and M1 alone judged it. **26 of 26 are refused by M1**:
text `hide=True`, a text size under 0.001 mm, a thickness of 0, a drawing's `layers=` other than
(copper, its own mask), `justify` naming one axis twice, a `Rect` radius past half its short side, a
point written twice in a row, a negative image scale, image data that is not base64, a barcode under
0.01 mm, an `ecc_level` on a code without one, a colour without its alpha, a NUL in a string, a
word KiCad has no token for, `solder_mask=True` on an inner layer, a `solder_mask_margin` without
`solder_mask`, a tenting block with every side `none`, a tenting block with a side left out, a
`zone_layer_connections` without `remove_unused_layers`, `keep_end_layers` without
`remove_unused_layers`, a custom padstack missing a layer, a custom padstack with an `F.Cu` entry, a
post-machining angle to two decimals, a width finer than 1 nm, `filled_areas_thickness`,
`teardrop_type`. The rules M1 does **not** replace are the ones that are not about KiCad's reading:
a net `board.py` does not have, a `Place` or any `board.py` object in core, an `Edge.Cuts` drawing, a
core group naming a gen object or using the `pcbc:` prefix, a uuid the placed board carries, an
unbalanced parenthesis in a drawing on copper (KRT's reader, not KiCad's), a lone surrogate (no file
can hold it), and the clearance and minimum checks (`layout_check`, `route_scene.clashes`). The read-as-
KiCad-keeps normalisations (`_nm`, `_dbl`, a via's missing side filled as `none`, the teardrop and
hatch defaults) still make such lines *pass*; M1 is what now proves them right, and the doc's older
claim that every double is kept to ten significant digits no longer needs to be true for anything to
be safe (it is false for `island_area_min`, an image scale, a post-machining angle and the tuning
pattern's properties; M1 names what KiCad kept).

What M1 cannot see is what KiCad decides from the rest of the board — the router's copper, the
footprints, the fill — so the route stage's own comparison after routing stays as the backstop
(`KiCad did not keep a core line as written`).

### M2 — a stamp is what the stage was seen to read (`trace`)

Every stamp was a hand-kept list, and each review found an input missing from it: a helper module or
a data file `layout.core.py` reads, a `schematic.kicad_pro` beside the schematic steering KiCad's ERC,
`PCBC_PATTERNS=off` shipping different copper and the next plain build saying "skipped", the date and
`--strict-power` changing the package unstamped. Now `trace.tracing(stage, board_dir)` wraps each stage
(check+seed, sch, place, route with its gates, fab) and the load of `board.py`. It installs one
`sys.addaudithook` hook per process and watches `os.environ`; while a stage runs it records:

- every file the build's own process **opens for reading** (the `open` audit event: `open()`,
  `os.open`, `Path.read_text` and the import system all raise it) that the stage did not write — by
  content, a `.pyc` as its source. Python's library, installed packages, the operating system's files
  and pcbc's own code are the tool and are left out; pcbc's bundled footprints are read like any
  other input. The load of `board.py` is recorded as `reads` in every stamp, each stage's own as
  `traced.reads`. A module the core imports and the `sys.path` entry it added are taken back out after
  the load, so the next load reads — and the tracer sees — the file again;
- every **subprocess** (`subprocess.Popen`): the exact argv (the board's directory written `.`, a
  scratch directory `<tmp>`, so two builds from two paths stamp the same bytes), and every file in each
  directory it is handed, split at the stage's end into the files the stage wrote (outputs, by name)
  and the ones already there (inputs, by content). A file that appears afterwards beside a tool's input
  is stale;
- the **environment that changes output**: every `PCBC_*`, `KRT_*`, `KICAD*` and `SOURCE_DATE_EPOCH`
  variable the stage reads (so `PCBC_PATTERNS`, `PCBC_STRICT_PATTERNS`, `PCBC_KRT_TIMEOUT` and `KRT_HOME`
  land in the route stamp, `SOURCE_DATE_EPOCH` and `PCBC_STRICT_POWER` in the fab stamp, each only where
  it is read), the `KRT_*`/`KICAD*` variables a tool inherits, the `kicad-cli` version string of each
  `kicad-cli` the stage ran, and KRT's pinned sha with the sha the install is at (read from its `.git`).
  `PCBC_*` variables nothing reads (the test switches `PCBC_REQUIRE_*`) are not stamped: tracing
  records what was read, not what was set.

`build.stale_reason` re-hashes every recorded file, re-lists every handed directory, re-reads every
recorded variable and tool, and a difference is stale **from the stage that read it**, worded as
before: `corehelp.py changed since the route stage read it (a file the route stage opened; edited)`,
`the environment changed since the route stage read it: PCBC_PATTERNS was 'off', now unset`,
`layout/blinky/schematic.kicad_pro is beside a file a sch-stage tool was handed and was not there when
it ran (a tool reads its directory: KiCad reads a .kicad_pro beside the file it opens); delete it, or
rebuild`. A stamp without the record is an older pcbc's and is stale. Belt and braces for the
schematic: the sch stage unlinks a `schematic.kicad_pro`/`.kicad_prl` before it draws the sheet. The
recorded set is not compared as a set (Python opens a cached module once per process); a new input can
only arrive through an edit to one that is recorded. Cost: the checks re-hash a handful of files and
run `kicad-cli version` once per process (0.22-0.37 s).

### M3 — fab ships exactly the fill the gates judged

Fab compared each zone's fill as (island count, total area) to 0.01 mm2 and never checked its own
sidecars; an area-neutral `.kicad_dru` edit made inside fab shipped buck's GND pour 0.0105 mm from a 5V
pad with `ok=True`, stamped. Now:

- fab's `.kicad_pro`/`.kicad_dru` are written from the compiled job (they were) and **must still be
  those bytes after fab's DRC and refill, and again after the Gerber export's zone check**
  (`fab.sidecars_not_compiled`), or fab refuses naming the file;
- every zone's `filled_polygon` must equal the routed board's **exactly, vertex for vertex in file
  order** (`fab.fab_board_diff`), or fab refuses naming the zone and layer;
- the route stage fills the emitted board once before the gates' refill (`build.py`), because KiCad's
  first fill of empty zones is not always its fill of a filled board: on `tests/fixtures/planes` the
  emitted board's first fill has 591 vertices and every refill of it 590, one zone differing (this was
  the "nanometre" the tolerance existed for); refill is a fixed point after that one step (`fix7/fill`,
  `fp/*/r1`, `r2`). On the five boards the two fills were already equal. Cost: one `kicad-cli pcb drc
  --refill-zones --save-board`, 0.71-0.88 s on the five boards.

**Deterministic, measured**: two fresh builds of each of the five boards from two paths
(`fix7/five/T1`, `fix7/five/T2/deeper dir/x`): routed fill = fab fill exactly on every board (32 to
4804 filled vertices), build 1 = build 2 for the routed fill and for the fab fill, and the router's
board's fill = the emitted board's. The planes fixture: routed = fab, 590 vertices, on two builds; and
equal to the fill the fixture shipped before this round. `FAB_WRITES` now names what fab really
changes: the fill (exactly), its sidecars (byte-checked), and the order of top-level nodes (KiCad's
save; nodes are compared as a multiset, each in order).

### Before and after, the sixth review's majors and blocker

Each harness is the reviewer's own, run unchanged on the tree before this round
(`PYTHONPATH=fix7/before_src`, logs `fix7/logs/before_*.log`) and after (`fix7/logs/after_*.log`).

| Finding | Harness | Before | After | Closed by |
|---|---|---|---|---|
| through via on an inner layer | `fuzz.py cases_build.py`, `n_via_through_inner` | refused after routing, "the router dropped it" | refused before routing: `layers (written ('F.Cu', 'In1.Cu'), KiCad's load of the line has ('F.Cu', 'B.Cu'): write that)` | M1 |
| `None` on a required yes/no or word | `b_stroke_none`, `b_locked_none`, `b_bold_none` | "the route stage stopped: (gr_line) (stroke): stroke_type is required" (no line) | `layout.core.py:1: (gr_line) (stroke): stroke_type is required; ...` before routing | M1 |
| values KiCad rewrites (roundtrip lens, 11 builds) | `b_table_border_type_none` ... `b_line_width_neg` | 11 refused after routing | 11 refused before routing, 1.1-1.3 s, each naming the line and "write that" | M1 |
| list of the wrong length | `b_textbox_margins2`, `b_cell_span1`; `buildprobe6b.py` `tbox_margins3`, `barcode_margins1` | "KiCad could not load the router's board after normalise ... report it", no line | `layout.core.py:1: KiCad refuses to load the line (... need a number for 'right margin' ...)` before routing | M1 (bisect) |
| lower precision / integers / clamps (primitives lens) | `buildprobe6.py`, `6b`, `6c` | 13 refused after routing | refused before routing, naming line, field and KiCad's value | M1 |
| fields dropped by another field (17 build probes) | `buildprobe6.py`, `6b`, `6c` | 17 refused after routing | 17 refused before routing (group self-membership, a group cycle and an empty `Generated` as `... has no such object: KiCad drops it on load`) | M1 |
| net name with a backslash or newline | `cb_net_r6.py bs nl` | `build_job` raised `ParseError` / `RuntimeError` | returned: `board.py line 5: the net name 'LED\\b' carries a backslash, which KiCad reads as an escape; rename the net` | load check |
| fab fill by area; sidecars unchecked (BLOCKER) | `in_fab_r6.py buck side_dru_near_short side_dru_area_neutral side_pro_severity_ignore` | `ok=True`, `copper_equal True`, stamped | refused: `fab's sidecars are not the compiled job's: fab/layout.kicad_dru is not the file the compiled job writes` (`.kicad_pro` for the third) | M3 |
| `schematic.kicad_pro` steers ERC | `h/ercpro.py` | stale `('fab', None)`, plan `[]`, ERC silenced | stale from seed naming the file; the rerun unlinks it; ERC as without it | M2 |
| `PCBC_PATTERNS=off` ships other copper | `h/envp.py` E1 | plan `[]`, skipped, board 7a83e6e4d515 shipped | stale from place (`PCBC_PATTERNS was 'off', now unset`), rerouted to 3367305eab35 = the reference build | M2 |
| a core helper module / data file | `h/coreimp.py` V2, V3 | stale `('fab', None)`, shipped x=5 | stale from place naming `corehelp.py` / `corehelp.json`, shipped x=7 = the force build | M2 |
| `SOURCE_DATE_EPOCH` (minor) | `h/envp.py` E2 | plan `[]`, gbrjob 0b6331f86498 | plan `['fab']`, gbrjob df2fdbb636cc = the reference | M2 |
| `--strict-power` after a plain build (minor) | `h/envp.py` E3 | rc 0, skipped | rc 1, plan `['fab']`, the power error | M2 |

### Minors closed this round

`routed/core.kicad_pro`/`.kicad_dru`/`.kicad_prl` are stage outputs, unlinked by the clean step; the gen
header names the outline as the third kind of line that cannot be locked, and a test locks every
non-copper gen line of blinky and checks each refused kind is named; the every-line lock test's drill
assertion compares the locked build's PTH hits with the baseline's vias plus plated pad holes (it was
`assert True`); adoption compares a segment's or arc's two ends as a set (`layout_job.adopt_key`);
`Text`/`TextBox` text and a `Group`'s name and members must be strings, a `size` must be one number or
two (a bool, a string or a 1- or 3-tuple is refused with "size is (height, width)"; a barcode's is
"(width, height)"); a `Pour` outline needs three points enclosing an area; a non-number's hint echoes
the number the string held (`write solder_mask_margin=0.05`, not `=5`); a core zone on a non-copper
layer is refused as pcbc's copper-only rule, not as "an undefined layer"; after a refused sch stage the
stale reason says the last run was refused. Not done: the two `docs/direction.md` literal findings
(§2's permitted reads, §8's routing-problem file) are the owner's to decide.

### Measured round trip (2026-09-24, seventh round)

Fresh builds of temp copies of the five boards (ds2 copied from the owner's tree, never built there),
each from two absolute paths built at the same time (`fix7/five/run.sh`; table `fix7/five/table7.md`,
`fix7/five/make_table7.py`):

| board | seg | via | zone | drawings | router_vs_emitted | emitted_vs_fab_whole_board | drc_router_eq_emitted | drc | dangling_removed | copper | gate_fails | fab_ok | gerbers | fab_unconnected | fab_fiducials_inserted | tree_files | two_paths_differ_outside_krt_steps | krt_step_files_differ | fill_vertices | fill_routed_vs_fab_exact | fill_build1_vs_build2_routed | fill_build1_vs_build2_fab | fill_router_vs_routed | fab_drc_violations | route_stamp_traced | fab_board_to_gen_bytes | fab_resave_fixed_point | routed_resave_fixed_point | stale_after | secs |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| blinky | 4 | 1 | 4 | 3 | 0 | 0 | True | 0 errors, 5 warnings | 0 | verified | {} | True | 10 | 0 | 0 | 65 | [] | 4/19 | 32 | equal | equal | equal | equal | 0 | reads 2, dirs 1, env ['KRT_HOME', 'PCBC_KRT_TIMEOUT', 'PCBC_PATTERNS', 'PCBC_STRICT_PATTERNS'], tools ['kicad-cli', 'krt'], argv 7 | True | nodes equal, bytes differ | nodes equal, bytes differ | ('fab', None) | 7.5 |
| buck | 73 | 7 | 4 | 9 | 0 | 0 | True | 0 errors, 70 warnings | 0 | verified | {} | True | 10 | 0 | 0 | 73 | [] | 9/27 | 120 | equal | equal | equal | equal | 0 | reads 2, dirs 1, env ['KRT_HOME', 'PCBC_KRT_TIMEOUT', 'PCBC_PATTERNS', 'PCBC_STRICT_PATTERNS'], tools ['kicad-cli', 'krt'], argv 10 | True | nodes equal, bytes differ | nodes equal, bytes differ | ('fab', None) | 15.1 |
| c3_usb | 302 | 55 | 5 | 48 | 0 | 0 | True | 0 errors, 283 warnings | 0 | verified | {} | True | 10 | 0 | 0 | 70 | [] | 8/24 | 482 | equal | equal | equal | equal | 0 | reads 2, dirs 1, env ['KRT_HOME', 'PCBC_KRT_TIMEOUT', 'PCBC_PATTERNS', 'PCBC_STRICT_PATTERNS'], tools ['kicad-cli', 'krt'], argv 9 | True | nodes equal, bytes differ | nodes equal, bytes differ | ('fab', None) | 23.7 |
| node | 274 | 86 | 6 | 72 | 0 | 0 | True | 0 errors, 271 warnings | 1 | verified | {} | True | 12 | 0 | 0 | 76 | [] | 9/28 | 4804 | equal | equal | equal | equal | 0 | reads 2, dirs 1, env ['KRT_HOME', 'PCBC_KRT_TIMEOUT', 'PCBC_PATTERNS', 'PCBC_STRICT_PATTERNS'], tools ['kicad-cli', 'krt'], argv 11 | True | nodes equal, bytes differ | nodes equal, bytes differ | ('fab', None) | 41.4 |
| ds2_addon | 227 | 27 | 4 | 21 | 0 | 0 | True | 0 errors, 113 warnings | 2 | verified | {} | True | 10 | 0 | 0 | 67 | [] | 7/21 | 1253 | equal | equal | equal | equal | 0 | reads 2, dirs 1, env ['KRT_HOME', 'PCBC_KRT_TIMEOUT', 'PCBC_PATTERNS', 'PCBC_STRICT_PATTERNS'], tools ['kicad-cli', 'krt'], argv 10 | True | nodes equal, bytes differ | nodes equal, bytes differ | ('fab', None) | 25.8 |

Every board: router's board and emitted board 0 differences; emitted and fab board 0 differences on
the whole board, in order, uuids included; KiCad's DRC by type router = emitted, 0 errors; 0
unconnected, 0 fab DRC violations; every gate passed; **the fill exact** routed = fab, and build 1 =
build 2 for both; the whole `layout/<name>/` tree byte-identical across the two paths outside the KRT
step boards (the stamps with their traced records included); the fab board decompiles to
`layout.gen.py`'s bytes; `stale_reason` after is `("fab", None)`. Against last round's builds
(`fix6/T1`): the fab board, every Gerber and the gen body are byte-identical on all five; the routed
board has the same nodes (the extra fill's save reorders top-level items). The seconds are wall time
with the two builds of each board running at once.

Suite, `cd ~/pcbc && PCBC_REQUIRE_KICAD=1 PCBC_REQUIRE_KRT=1 .venv/bin/python -m pytest -q -p no:cacheprovider`,
run last with nothing else building: **1011 passed in 1229.65s** (`scratchpad/step1b/fix7/suite2.log`;
the first full run, `fix7/suite.log`, was 1 failed, 1010 passed: a test pinned the seed stamp's exact key
set, which now carries `reads` and `traced`, and was updated).
