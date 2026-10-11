# Net and path: where each physical requirement lives (version 1, revised)

Draft 2026-09-30, from the brief Grok wrote for the owner's question: *"lets redesign the physics
approach to use property of net or path so its clear what the placement and router needs."* Three
review rounds (code, physics, language) shaped a long draft; version 1 cut it to what gets built and
applied the owner's three answers of 2026-09-30; a fourth review round (v1: code, physics, language) is
answered in this revision (the Revision Summary at the end lists what changed). **Version 1 was built
on 2026-09-30 in the working tree (uncommitted); "Build record" at the end says what landed, where it
departs from this text, and what was measured.** Statements about today's behaviour cite the code they were read from; *measured* numbers came from
scratch copies by the commands in §0 and §4.3; *predicted* numbers are what the compiler would print once
this is built.

Read `docs/direction.md` first, §5b in particular; it wins over this document. Nothing here changes the
three files, the two sources of truth, KiCad as arbiter, or the native router.

**The owner's answers, binding.**

1. *"Call it path."* `Path()` replaces `Chain()` outright: one function, no alias. Loading a file with a
   `Chain()` line fails, and the error gives each rewritten line exactly (§1.3).
2. *"The router should pick the most efficient route that meets all criteria, if we need to change
   layers and 3 vias meets criteria then we do it."* At a layer change, copper carrying a current places
   the via group that current needs (the count from the repo's own `via_barrel` rating, judged at the
   group's worst barrel), priced in `route_cost` like length. Needing more than one via is never by
   itself a reason to refuse (§4.3). This holds for a path's links and, in version 1, for a net's own
   links too (PR 9).
3. *"Yes migrate."* `examples/buck/buck.py` moves to the new form; it is the only example with a
   sense/load split (§5).

---

## Key decisions

Each decision names where it is specified and the PR that builds it. The PR plan (§9) is ordered so that
every decision is built before the PR that relies on it.

| # | decision | where | PR |
|---|---|---|---|
| K1 | `Path()` replaces `Chain()`; a `Chain()` line is refused with every rewritten line. The DS2 Addon (the owner's file) is read by the tests through one helper that applies that exact rewrite to a temp copy, so the suite stays green until the owner edits his file | §1.3, §8 | 1 |
| K2 | Two subjects. A net holds what is the same at every pad; a path holds what belongs to the copper between named pads | §1, §2 | 1-3 |
| K3 | One combine rule for every path field: **a path may loosen a default, never a written value**; a path kind's defaults yield to values the `NetReq` wrote | §2 | 3, 5 |
| K4 | A branch no path names gets the net's numbers, never a path's current, and the report prints what that branch carries on each layer | §3.1 | 3 |
| K5 | One cost per link; a width per layer; every router site that reads a width reads the searched layer's | §4.2 | 2, 3 |
| K6 | A layer change carrying a current places a **flow-through via group**: a line of barrels at the board's via pitch with both layers' copper running over every barrel, its count the smallest whose worst barrel (lumped DC model) is within `via_amps`. The rule is by current: a fanout escape, a tap, or an existing via a link starts on must meet it too | §4.3 | 4 |
| K7 | Two legality rules where branches join: a sense path meets its net only inside its first station's pad; a path that writes `amps=` is met only inside its station pads. The router and the emitted-board check dilate the pad by one number, the net's clearance | §3.2 | 5 |
| K8 | A sense path written backwards is refused on two kinds of evidence, one of them general (its far station is a two-pad part bridging to a current-carrying net) | §3.2, §7 | 5 |
| K9 | A multi-pad end station compiles to an explicit join link at the path's numbers; a multi-pad intermediate station is refused with the split into two paths as its move | §1.2 | 2, 3 |
| K10 | Declared currents must balance at a shared station; when they do not, the compiler refuses with the edit | §3.3 | 3 |
| K11 | `Loop()` is a check on named pads; its pass line prints the measured area as a lower bound | §6 | 7 |
| K12 | Placement checks print warnings that name the line to edit, never a pose; the existing `check_chains` order message, which printed a pose, is rewritten | §4.5 | 6 |
| K13 | The net's own links meet the same two criteria (width per layer, via groups) in version 1, **before** buck migrates; a poured net's vias stay taps (§10 A) | §4.3, §8 | 9 |
| K14 | Buck migrates: the 5V trunk and feedback tap, a switch-node path, one `Loop()`; `VIN` stays on its `NetReq` | §5 | 10 |

---

## Summary

A **net** is one electrical node: every pad on it is at one potential. What follows from the potential
is written on `NetReq`: `kind`, `volts` (the clearance and creepage to another net follow from the two
potentials), the reference plane, the pairing of two nets, the edge rate, and the impedance, length and
via budgets the kinds already take.

A **path** is an ordered list of stations on one net, written from where its current enters (a sense
path: from where it leaves the rail). What follows from the copper between two stations is written on
`Path(net, *pads, ...)`, which takes eight fields: `amps`, `temp_rise_c`, `vias`, `layers`, `max_mm`,
`keep_clear_of`, `keep_clear_mm`, `kind="feedback"|"sense"`. How a path's field joins its net's is the
"combine" column of §2, under one rule: **a path may loosen a default, never a written value**. Current,
and so width, replaces the net's on the path's own links; `vias` and `layers` replace a default but may
not loosen what the `NetReq` wrote; airwire budgets both hold; keep-aways add. Everywhere a path does
not name, the `NetReq` holds. A field-less path carries its net's numbers, so every existing board
compiles and routes as today once its `Chain()` lines are renamed. A **branch no path names** gets the
net's numbers, never a path's current, and is listed in the report with what it carries (§3.1).

The router reads one cost per **link** (two consecutive stations of a path) instead of one per net: a
width **per layer** (the plane under the copper changes what a width carries), layers, vias allowed,
keep-aways, and at a layer change **the via group its current needs**, laid so the barrels share the
current. Two legality rules keep the physics where branches meet (§3.2): a **sense** path touches the
rest of its net only inside its **first** station's pad, and a path that writes **`amps=`** touches the
rest of its net only inside its own station pads.

`Loop(*pads, mm2=)` is a **check** on named pads that replaces the guessed nearest-cap loop for the
parts it names; it is not routed. The placer reads nothing new; the placement checks read links and
print warnings that name the line to edit, never a pose. The emitted board is checked for each path's
width, vias, layers and via groups.

Out of version 1, with their reasoning and the round-3 findings kept in §10: return paths on a pour, the
full junction rule set, routed `Loop()` hops, impedance and length on a path, pair skew along paths,
flow-through stations, and superposition of path currents.

---

## 0. What exists today, measured

**The pad list.** `Chain(net, *pads)` (`src/pcbc/language.py:1984`) records an ordered tuple of
`"REF.PIN"` and nothing else. Its consumers: the router links a chain's consecutive stations before any
other link of its net (`route_native.net_links`, `src/pcbc/route_native.py:923-927`); `route_pairs`
(`route_native.py:1112`) routes a pair's two chains station pair by station pair; `build._chain_gate`
(`src/pcbc/build.py:87`) fails the build when the emitted copper does not feed the stations in order
(`route_verify.chain_order`, `src/pcbc/route_verify.py:1060`); `route_checks.check_chains`
(`route_checks.py:613`) checks order and corridor at placement for every chain, poured nets included
(`route_checks.py:620`). Five lines use it: `examples/c3_usb/c3_usb.py:268-269`,
`examples/node/node.py:147-148`, and the DS2 Addon's `ds2_addon.py:97`. Two test fixtures carry three
more (`tests/fixtures/ds2_addon.baseline.py:97`, `tests/fixtures/pair/pair.py:29-30`).

**The physics.** `NetReq` (`language.py:1843`) compiles to one `Constraint` per net
(`src/pcbc/constraints.py:178`). The router reads it once per net (`net_cost` and `route_layers`,
`route_native.py:971-972`) and lays every link of the net at `nc.width_mm` (`route_cost.net_cost`,
`src/pcbc/route_cost.py:113`). A second chain on a net reaches `cs.groups` but not the net's constraint:
`constraints[ch.net] = replace(c, group=spec)` (`constraints.py:1351-1356`) keeps the last, and so does
the report (`_report`, `constraints.py:2071-2077`).

**The buck feedback tap, as built.** `NetReq("VIN", "5V", "GND", kind="power", volts=12, amps=2)`
(`examples/buck/buck.py:55`) gives every `5V` link 0.781 mm. The spine pattern takes `5V`
(`patterns/spine.py` `specs`, lines 268-300, run before `route_nets`) and never reads chains. The last
local build (`examples/buck/layout/buck/routed/layout.gen.py`, 2026-09-27) has a 0.781 mm trunk
`R_FB_TOP.1 -> L1.2 -> C_OUT1.1 -> C_OUT2.1` (lines 43-50; 43-45 run to the inductor's pad), and the
router joins `J_OUT.1` from `C_OUT1.1` (lines 49, 51, 52). So the divider, which draws 5 V / 113.3 kΩ =
44 µA, is fed by copper sized for 2 A that leaves the rail at the inductor's switching end.

**The decap and loop checks guess.** `pcb_place.layout_report` (`src/pcbc/pcb_place.py:596`) takes the
nearest IC pad on a cap's power net (`pcb_place.py:625-647`) and ignores the cap's `Place(to=)`, which
buck writes for every cap (`buck.py:63`). `route_checks.check_loops` (`route_checks.py:1036`) guesses
the decap and hot loops the same way; buck's hot-loop guess is `C_IN1.1 -> U1.VIN -> U1.GND ->
C_IN1.2`, 3.3 mm² of 20 (measured, `tests/test_route_checks.py:343`). Nothing the author wrote names
those pads.

**The placer reads no physics.** `pcb_place.resolve_places` (`pcb_place.py:356`) and `_attach`
(`pcb_place.py:459`) never read amps, volts or impedance. Grok's uncommitted change
(`src/pcbc/place_solve.py`, `tests/test_place_solve.py`, 5 lines in `place_native.place`) prints a
warning, never a pose; every message this design adds follows that style.

**A per-pad current has been asked for twice**: `patterns/spine.py` shares a rail's amps evenly over its
pads and calls that a placeholder (`spine.py:237-240`), and `ampacity._walk` (`ampacity.py:371`)
demands the declared amps between **every** pair of the net's pads, so the buck tap is held to 2 A.

**Power copper on the shipped buck, against the declaration and against the physics** (measured on the
2026-09-27 `layout.gen.py`; not caused by this design; physics v1-R1, v1-R4). The declaration is
`amps=2` on `VIN`, `5V` and `GND`; `SW` declares no current.

| copper | as built | against the declaration | against the physics |
|---|---|---|---|
| `VIN` on `B.Cu` (lines 80-83) | 0.781 mm | **under**: 2 A needs 1.09 mm on `B.Cu` (`current_width_mm(2, 10, jlcpcb_2l_1oz, None)`; `height_to_reference("B.Cu")` is `None` on 2 layers), and 0.781 mm there carries 1.665 A. The width block takes the plane modifier from `layers[0]` only (`constraints.py:1580, 1588`), so the net is compiled for `F.Cu` over the pour | fine: the `J_IN.1 -> C_IN1.1` branch carries the input DC, 0.833 A lossless or 0.926 A at an assumed 90 % efficiency; the `R_EN.1` branch carries the 0.12 mA pull-up |
| `VIN` vias (lines 40-42) | three lone 0.8/0.4 vias, 0.871 A each | **under**: `vias_per_change(2, 0.4)` is 3 | the `J_IN.1 -> C_IN1.1` via is marginal (96 to 106 % of 0.871 A; 2 at 90 % efficiency); the two on `R_EN.1`'s branch carry 0.12 mA |
| `SW`, `U1.SW -> L1.1` (lines 75-77, 79, 6.35 mm, `F.Cu`) | 0.3 mm (preset `switch_node`) | met: `SW` declares no current | **over**: it carries the inductor current, 2 A DC plus 0.112 A rms ripple, and 0.3 mm on `F.Cu` carries 0.999 A (`track_amps`), so it runs at 2.0× its rating (about 48 °C rise by the inverted IPC-2221 law, an estimate) |

Why the stitch `parallel` carrier added no twins to `VIN` (code v1-R14, measured this round on a scratch
copy, `layout.gen.py` byte-identical to the shipped one): it tried all 24 ring sites at each of the
three vias and refused each, because *"two 0.8 mm rings need 1.3 mm not to merge their antipads in the
GND plane on B.Cu, whose min_thickness is 0.1 mm"* while the cluster walk counts a twin only within
`VIA_PARALLEL_MM` = 1.0 mm; *"on this stackup there is no distance that is both"*. So on buck an
anchor-plus-twins ring can never be placed, which §4.3's group shape is built around.

Version 1 fixes the `VIN` rows for paths (PR 4, PR 8) and for the net's own links (PR 9), and the `SW`
row by declaring the switch current on a path (PR 10, §5).

Commands (scratch copies under `scratchpad/netpath/`; nothing written under the repo or
`~/Documents/MaD`):

```
cd scratchpad/netpath/v1 && PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=~/pcbc/src ~/pcbc/.venv/bin/python perlayer.py \
  blinky/blinky.py buck/buck.py c3_usb/c3_usb.py node/node.py ds2/ds2_addon.py      # width per layer, vias per change
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src .venv/bin/python -c "from pcbc import stackup as s; st=s.get_stackup('jlcpcb_2l_1oz'); \
  print(s.current_width_mm(2,10,st,None).value, s.track_amps(0.781,10,st,None), s.via_amps(0.4), s.vias_per_change(2,0.4))"
grep -n 'Seg("VIN"\|Via("VIN"\|Seg("SW"' examples/buck/layout/buck/routed/layout.gen.py         # read only
rsync -a --exclude layout ~/pcbc/examples/buck/ scratchpad/netpath/r14/buck/ && cd scratchpad/netpath/r14/buck && \
  PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=~/pcbc/src ~/pcbc/.venv/bin/python -c \
  "from pcbc.cli import main; import sys; sys.argv=['pcbc','build','buck.py']; main()" > build.log   # exit 0; grep 'stitch VIN' build.log
cmp layout/buck/routed/layout.gen.py ~/pcbc/examples/buck/layout/buck/routed/layout.gen.py          # identical
```

The `SW` and `VIN`-efficiency numbers are physics v1-R1 and v1-R4's (their `nums.py`), reproduced from
the repo's own `track_amps`; the 90 % efficiency is an assumption, not the TPS54202 datasheet.

---

## 1. The two subjects

### 1.1 Path

A path is two or more stations on one net, fed in the order written through each intermediate station
(the "through, not past" rule `chain_order` already enforces). Each consecutive pair is a **link**. A net
may carry several paths; paths may share a station; a link is named by at most one `Path()`. **One
`Path()` carries one current on every link**: a rail whose current changes at a station is one `Path()`
per link.

`Path` keeps every consumer `Chain` has. The language changes one name and adds keyword fields; the
model becomes `PathReq` and `Design.paths`. Internal verifier names (`chain_order`, `_chain_gate`,
`patterns/chain.py`) are kept so the rename stays mechanical; every user-facing message says `Path
line N` (the full list is in PR 1, §9).

### 1.2 Stations

A station is `"REF.X"`, resolved as `_refpin_net` (`constraints.py:979`) resolves it, pin name first. A
pin names every pad of that pin; a pad number names that pad.

- **A multi-pad end station** compiles to an explicit **join** (language v1-2). The router leaves (or
  enters) the station at its **anchor**, the pad nearest its one neighbour station (nearest on the
  placed board, ties by pad number), and every other pad of the station is joined to the anchor by a
  compiled `Link` of the path: key `"J1.A4B9~J1.B4A9"`, source `"Path line N (station J1.VBUS)"`, the
  path's numbers. It has a role owner like any link, the fatal width gate and the via-group check judge
  it, rule 2 (§3.2) applies to it, and `net_links` routes it before Kruskal, so the other pad never
  pairs straight to the neighbour station. **Its current** is the larger of the declared currents
  entering and leaving the station (the paths whose last station it is, and those whose first station it
  is). Sizing the join for the whole path current is conservative, since each pad of a connector pin
  carries part of the cable current; the cost is copper beside fine-pitch pads and, where the join
  must change layer, a via group there (§5.1). If no join fits, it fails with a route error like any link.
  When two paths end at one multi-pad station, the join is built once, by the first of them in file order,
  at the station's throughput: the larger of the summed declared currents of the paths that end there and
  of those that start there (refutation round 2: J1.VBUS shared by a 0.3 A and a 2 A path is joined at 2.3 A).
- **A multi-pad intermediate station is refused** (§7): version 1 joins a multi-pad station's pads only
  where a path ends there. With different entry
  and exit pads and no join, nothing would carry the path's current between them, since `_net_groups`
  (`route_native.py:689-726`) unions only pads that share a number (physics R3-6). Threading is §10 F.
- **On a pair net and in a `Loop()`, a station is one pad**: the pair router pairs pad with pad, and a
  loop's area is taken between points.

Today four consumers resolve a multi-pad pin four ways (`route_checks._refpin_pad`,
`route_checks.py:289`; `route_native._pad_owner`, `route_native.py:654`; `patterns/chain._members`,
`chain.py:287-293`; `route_verify._station_pads`, `route_verify.py:1029-1044`). PR 2 makes all four call
one pure helper, `constraints.station_pads`; for an end station `chain_order` asks "joined" of each pad.
No declared station on the five boards is multi-pad (measured, round 2), so no board changes.

### 1.3 Migrating a `Chain()` line

`pcbc.Chain` stays importable only to refuse. When a board calls it, the refusal scans the file's AST
(as `_explain_type_error` already parses it, `language.py:2176-2184`) and prints every `Chain(` call
and the `pcbc` import that names `Chain`, each named by its line with the fact that `Chain()` is now
`Path()` (owner 2026-09-30, and since 2026-10-01 with no rewritten line: `docs/direction.md` §7b). All
lines come in one refusal. `Chain()` never builds anything, so it is not an alias. `chain_rewrite` (the
`Chain` token replaced by `Path` at its `col_offset`, the import dropping `Chain` when `Path` is already
imported) stays a library function: the test suite applies it to temp copies of boards it does not own.
For the DS2 Addon:

```
ds2_addon.py line 97: Chain() is now Path(), with the same arguments
ds2_addon.py line 15: the import names Chain, which pcbc no longer exports
```

**The DS2 Addon is the owner's file and is not edited by this work** (code v1-R1). Nineteen test files
read `~/Documents/MaD/Hardware/DS2Addon/pcbc/ds2_addon.py` (`grep -rln 'ds2_addon.py\|DS2Addon'
tests`; the code review counted 21 by its own search), in place or by copying it whole, and the owner
cannot make his edit before PR 1 because `from pcbc import Path` does not import until then. So PR 1
adds one test helper (in `tests/conftest.py`) that copies the DS2 directory into a temp directory and,
when the copy still says `Chain(`, applies **the exact rewrite the PR 1 refusal prints** to the copy;
every DS2 reader goes through it. One test pins the refusal text against the real file, read only.
Measured on scratch copies (code v1-R1): without this, `Chain()` refusing turns 3 of those files into
8 failures and 10 errors, against 1 failure already present. `pcbc build` on the owner's file itself is
refused until **the owner makes these two edits**; nothing else in the suite or the four repo boards
depends on it.

**A name clash.** `language.py` imports `pathlib.Path` (line 16) and uses it at 50, 51, 69, 472, 2159,
2323, 2324, 2333, 2336, 2407, 2426, 2440, 2506, 2519, 2537, 2665, 2672, 2707, 2716 and 2723 (21 lines
match `\bPath\b`, code v1-R7); PR 1 imports it as `import pathlib`. A board doing both `from pcbc import
*` and `from pathlib import Path` gets whichever came last. Fourteen test sources in nine files use
`from pcbc import *` (`test_dru` 34, 68; `test_constraints` 38, 399, 535; `test_copper_rules` 75, 130;
`test_planes` 112, 142; `test_return_rules` 210; `test_source` 272; `test_route_checks` 469, 569;
`test_cli` 167); none uses `pathlib`. A `Path()` call with fewer than two stations is refused anyway;
when its one argument is a string, the refusal adds the clash (language v1-11):

```
Path("layout/out.txt") line 9: a Path needs a net and two or more stations; if this is pathlib's Path, `from pcbc import *` rebinds it: write `import pathlib` and call pathlib.Path
```

### 1.4 Net

`NetReq` is unchanged: every field keeps its meaning. The one new refusal in that direction is
`loop_mm2=` on a net whose loop a `Loop()` declares (§6). What changes for a `NetReq` that writes
`amps=` is how its own links are routed (width per layer, via groups, PR 9), not what it means.

---

## 2. Which field lives where

> **Superseded by `docs/physics-subjects.md` §2** (owner, 2026-10-01: each property has exactly one subject, and
> the tool accepts it nowhere else). In the built tool a field is never written in both places: current, width,
> vias, layers, reference, impedance and length are a Path's; pairing and matching are a Pair's or a Bus's; a
> loop's area is a Loop's; keep-away, voltage, span, `pf_max` and `rise_ps` are the NetReq's. A field on the wrong
> subject is refused with the facts (§7 of that document, E1-E23). The table below is the earlier design, kept
> for its reasoning; its "combine" column no longer applies.

A quantity that is the same at every pad of the node belongs to the net; one that varies along copper
between two pads belongs to the path. The last column says how a field written in both places combines
on the path's links.

| field | net (`NetReq`) | path (`Path`) | combine |
|---|---|---|---|
| `kind` | yes | only `"feedback"` or `"sense"` | the path kind gives the path's defaults |
| `volts`, clearance, creepage | yes | **no** | — |
| `reference` | yes | **no** | — |
| `pair`, `z_diff_ohm`, `match_mm`, `uncoupled_mm`, `Pair()` | yes | **no**; a path on a pair net takes no field | — |
| `rise_ps`, `clock`, `pf_max`, `class_name`, `autoroute` | yes | no | — |
| `amps`, `temp_rise_c` | yes, every branch | yes, that path | replace |
| width (derived, **per layer**) | yes (per layer from PR 9) | yes | from the merged `amps`/`kind`, on each allowed layer |
| `vias` | yes | yes | replaces a default; refused when it loosens a `vias=False` the `NetReq` wrote |
| `layers` | yes | yes | replaces a default; refused when it names a layer outside the `layers=` the `NetReq` wrote |
| `max_mm` | MST of the net's pads | sum of station-to-station airwire | both hold |
| `keep_clear_of`, `keep_clear_mm` | every branch | that path's copper | add: larger per other net, never below the clearance |
| `z_se_ohm`, `length_mm`, `vias_max` | yes, unchanged | not path fields in version 1 | the net's |
| `loop_mm2` | the guessed loop only | no | `Loop()` (§6) |

**One combine rule** (language v1-10). For each field, a link takes, in order: the path's written value;
else the path kind's default; else the net's value (written, or its kind's preset). **A path may loosen
a default, never a written value**: a path's written `vias=True` or `layers=` may widen a preset the
`NetReq` did not write, and is refused when it loosens what the `NetReq` wrote. A path kind's default is
not the author's, so where it conflicts with a value the `NetReq` wrote it **yields** instead of
refusing: `kind="sense"`'s `F.Cu` on a net whose `NetReq` writes `layers=("In1.Cu",)` gives the path
`In1.Cu`, with a report note. A kind default that is stricter than the net's (no vias where the net
allows them) holds; it loosens nothing.

**Gating, one field at a time** (code v1-R6, language v1-10). `amps` and `temp_rise_c` are each legal on
a path only when the net kind's own kwargs take that field (`_kwargs_of`, `constraints.py:1028-1030`):
`power` takes both (`constraints.py:862`); `switch_node` takes `amps` and not `temp_rise_c`
(`constraints.py:864`), as `NetReq(kind="switch_node", temp_rise_c=...)` is refused today, so a
`switch_node` path's current is sized at the default 10 °C rise. A path with `kind=` takes neither. A
net no `NetReq` names is `generic` and takes no current. The other six fields are legal on every path.

**A path kind** marks a sense branch: copper that carries a measurement, not a load. Its preset
(`constraints.py:871-872`: 0.2 mm, no vias, `F.Cu`, airwire 25 mm for `sense`, 15 mm for `feedback`)
gives the path's defaults in place of the net's, under the combine rule; clearance stays the net's.
`feedback` is a regulator's divider tap, from the output to the divider's top; `sense` is a measurement
line (a Kelvin line to a shunt, a reference's sense pin). A path kind and a net kind are separate
statements: `Path("5V", ..., kind="feedback")` marks the rail's branch toward the divider,
`NetReq("FB", kind=...)` describes the divider's own node, and writing one implies nothing about the
other (language R3-9). Where a path naming every pad would make them the same statement, the refusal
asks the author which kind the net is (§7) instead of choosing.

**A path that names every pad of its net takes no field and no kind** (§7): its fields would hold on
every branch, which is what the `NetReq` says. The rule the AI follows: **a current goes on the
`NetReq` when every branch may carry it without harm; a `Path` carries it when some branch must not**
(a tap that must leave from one point, a branch that must stay thin).

**What `amps=` on a `Path` adds over `amps=` on the `NetReq`** (language v1-3). From PR 9 both spellings
get the same width per layer and the same via groups. A path's current adds exactly three things:
rule 2 of §3.2 (nothing of the net meets the path between its stations); the ampacity measurement walks
consecutive stations at the path's current instead of every pad pair at the net's (§4.6); and the
path's copper carries its own role owner, so the fatal gate judges it against the link's width. Before
PR 9 lands, a `NetReq` current is laid at one width from `layers[0]` and one via per change; that gap is
why PR 9 comes before the buck migration and is not optional.

**How a link's numbers are computed.** The net's fields, with the path's written fields (or its kind's
defaults) replacing the replace-column entries, go through one helper lifted out of `_compile_req` so
the net and the link both call it. The code is four blocks, not one (code v1-R5): layers
(`constraints.py:1560-1581`), width (`1583-1616`), clearance and voltage (`1618-1635`, **not lifted**:
clearance stays per net), and via (`1637-1656`, whose `per_change` reads the width block's `current`).
The `z_se_ohm` branch (`1763-1792`) reads `plane` and `layer0` from the width block and is lifted with
it. Two changes go into the helper:

- **Width per layer** (physics R3-7). `current_width_mm(amps, dt, stack, plane_h)` is computed for each
  allowed layer with `plane_h` from `_reference_for(stack, layer, planes)` (`constraints.py:993`), not
  from `layers[0]` alone. A link with a current gets a width per layer, and the report names each layer
  and its plane. From PR 9 a net with a current does too.
- **Current against impedance** (code R3-10). When a link has a current and a `z_se_ohm` width, the
  helper compares `current_width_mm` (the larger of IPC-2221B and IPC-2152, the function the gate uses)
  with the impedance width and refuses when the current needs more, printing both. No board in the repo
  writes `z_se_ohm` (grep).

There is still no `width=` keyword; `docs/direction.md` §4 refuses a width below the compiled one.

---

## 3. Branches, junctions and current

### 3.1 The branch no path names

Every pad of a net with a `Path()` is a station of some path or **off-path**. Kruskal joins an off-path
pad as today, at the **net's** numbers:

- **`NetReq` gives `amps=`**: every branch can carry it until a `Path` names the branch. This is the
  only way existing boards compile unchanged.
- **`NetReq` gives no `amps=`**: on `power` the width is what `POWER_DEFAULT_AMPS = 0.2` gives
  (`constraints.py:884`), 0.4 mm (measured, §5); on `switch_node` it is the preset's 0.3 mm. An
  off-path branch never takes a path's current.

The report prints every off-path pad on a net whose paths carry a field or kind, **with what its branch
carries on each allowed layer** (physics v1-R7), so a forgotten load can be judged against a number
(predicted; capacities are `track_amps` on jlcpcb_2l_1oz at 10 °C):

```
5V: C_OUT2.1 is on no Path; its branch is 0.4 mm (NetReq line 56 gives no amps=; preset power 0.2 A), which carries 1.231 A on F.Cu (over the B.Cu pour) and 1.152 A on B.Cu, not Path line 62's 2 A
SW: C_BOOT.2 is on no Path; its branch is 0.3 mm (NetReq line 57 gives no amps=; preset switch_node), which carries 0.999 A on F.Cu, not Path line 60's 2 A
```

(Line numbers are the §5 buck file.) A path with `amps=` below 0.2 A on a `power` net gets the lower
floor, 0.25 mm (measured, round 2).

### 3.2 Where branches join

Same-net copper is never an obstacle today: `Slack.at` tests `same = ... it.net == self.net`
(`route_cost.py:246`), `merge_overlaps` drops a thin same-net segment inside a wider one
(`route_native.py:542`), and Kruskal pairs an off-path pad with the nearest terminal of any component
(`route_native.py:929-938`). So a sense tap may join its rail anywhere, and a load may tee onto a current
path between stations, where the path's copper is sized for its own current alone. Version 1 adds two
rules (both recorded in `docs/direction.md` §5b):

1. **A sense path touches the rest of its net only inside its first station's pad** (the **junction**).
   Nothing else of the net may touch its copper, its far station's pad, or that pad's station copper.
2. **A path that writes `amps=` touches the rest of its net only inside its own station pads.** No other
   copper of the net (off-path, another path, or another link of the same path) and no non-station pad
   may meet it between stations. This is "one `Path()` carries one current on every link" as a routing
   rule; without it a branch teed on mid-link puts its current through copper sized without it
   (physics R3-5).

**Contact inside an allowed pad is legal for both sides** (code R3-5): within the pad's copper dilated
by **the net's clearance**, one number for the router and the check (code v1-R4). The router predicate
reads it from the link's `NetCost`; `path_junctions` passes it as `chain_order`'s `floor`
(`route_verify.py:1107`, which defaults to `stackup.clearance_min`, 0.127 mm on jlcpcb_2l_1oz, against
0.2 mm on buck's `5V`). An off-path branch may therefore end on the junction pad or on any station pad
of a current path. This matters because the router lays links pad centre to pad centre (`_link`,
`route_native.py:949`), so a link ending on a pad touches the copper that starts there.

The pad is the finest point the rules can name. For a divider tap that is enough; for a milliohm shunt,
where on the pad the sense leaves is the measurement, a precise Kelvin point needs its own pad (a
four-terminal shunt).

**Which end is the junction** (language R3-1, v1-1). The first station written, so order matters. The
compiler refuses a sense path written backwards on either of two kinds of evidence (§7):

- **(a) its far station is a pad of a two-pad part whose other pad is on a net that takes current**
  (`power` or `switch_node`). Such a part sits on the load: the reversed buck tap (far station
  `C_OUT1.1`, other pad on `GND`) and the reversed shunt (far station `R_S.1`, other pad on `VLOAD`) are
  caught, and every correct example passes (`R_FB_TOP.1`'s other pad is on `FB`, `analog`; `U5.INP`
  and `U4.VOUT_S` are on parts with more than two pads). This is the general rule; it does not depend
  on what else the board declares.
- **(b) another path of the net names the sense path's last station but not its first** (the reversed
  `VREF` below, whose stations are on multi-pad parts that (a) cannot judge).

Otherwise the first station written is the junction, and the report says so. A sense path that
legitimately ends on a two-pad part bridging to a current net (a filter cap at the sense input, say) is
refused by (a); it is written to end at the IC pin instead. A reversed sense path that neither (a) nor
(b) sees is not caught in version 1; the ampacity rule below narrows what it can cost.

**In the router** (PR 5): one router-only predicate says when a same-net item is foreign.

- In `Slack.at` it replaces `same`, and a foreign same-net item is priced at the net's clearance (the
  judge's number), not the 0 mm `ClearanceTable.between(net, net)` returns (`constraints.py:2175`;
  code R3-6). After `blocked`, a new `route_scene.link_clashes(scene, pieces, net, link)` judges the
  candidate against the foreign items and the link's keep-aways. It is called at the `Router`'s two
  `blocked` sites (`route_cost.py:355, 373`), the relaxer's probe (`route_relax.py:874`) and `hop` for a
  declared link. `blocked`, `_pair_clashes`, `audit`, `verify_copper` and the core-line clash check are
  unchanged.
- The pad exemption needs a segment clipped against a convex hull plus radius; `route_geom` has none, so
  PR 5 adds it. A via is exempt whole when its centre is inside the region.
- `Piece.link` and `route_scene.Item.link` carry the link: `""` for off-path copper and for a core line
  (the author's copper names no link), `"@<pad>"` for fanout escapes and tap stubs, which count as that
  pad.
- **It can reject only on a net with a sense path or a path that writes `amps=`.** No board has one; the
  DS2 Addon's `VDDA` path has no field. So the five boards route as today (code R3-2).
- **`merge_overlaps`** skips only pairs the predicate makes foreign to each other and merges everything
  else as today; its docstring's cases (node's `GND` taps, ds2's `3V3` over a locked stub, c3_usb's
  `USB_DP`, `route_native.py:537-540`) are on nets where it never rejects (code R3-3).
- **`net_links`** never takes a sense path's far station (or its station copper) as a Kruskal terminal,
  and pairs an off-path pad with a current path's component only at a station pad.

These are legality rules, not cost terms.

**On the emitted board** (PR 5), `route_verify.path_junctions` reuses `chain_order`'s region removal
(`_judge`, `route_verify.py:1136`) at the net's clearance: for a sense path, remove the junction's pad
region and ask that the sense copper beyond it reach nothing of the net but the far station; for a
current path, remove its station regions and ask that its copper touch no copper of another owner. A
failure names the path, station, copper touched and distance, printed as pcbc's measurement.

**A force/sense reference** (illustrative part, written the Kelvin way: from the point sensed to the pin
that reads it; illustrative file, lines 80-82):

```python
NetReq("VREF", kind="analog")                               # line 80
Path("VREF", "U4.VOUT_F", "C_REF.1", "U5.REFP")             # line 81: force, out through the reservoir cap to the ADC pin
Path("VREF", "U5.REFP", "U4.VOUT_S", kind="sense")          # line 82: sense, from the point it senses back to the sense input
```

```
VREF: path U4.VOUT_F -> C_REF.1 -> U5.REFP (Path line 81)
VREF: path U5.REFP -> U4.VOUT_S (Path line 82, kind sense): width 0.2 mm (preset sense), no vias, layers F.Cu, airwire 25 mm; meets VREF only inside U5.REFP, its first station
```

Written the other way round, line 82 is refused by (b): line 81 names its last station but not its first.

**A shunt** (illustrative). A `Loop()` is a check only, so it adds no copper and no junction, and the
example the long draft refused itself on is legal (physics R3-2, language R3-4):

```python
NetReq("VBAT", "VLOAD", kind="power", volts=12, amps=5)
Path("VBAT", "R_S.1", "U5.INP", kind="sense")              # Kelvin from the shunt's supply side
Path("VLOAD", "R_S.2", "U5.INN", kind="sense")             # and from its load side
Loop("R_S.1", "U5.INP", "U5.INN", "R_S.2")                 # the two sense lines stay close: an area check
```

The load current is off-path on both nets; it may end inside `R_S.1` or `R_S.2`, never on sense copper.
Written `Path("VBAT", "U5.INP", "R_S.1", kind="sense")`, the line is refused by (a): `R_S.1` is a
two-pad part whose other pad is on `VLOAD`, `power` (language v1-1 found that this order routed the 5 A
load through 0.2 mm sense copper with every other v1 check passing).

### 3.3 Current at a shared station

Routed copper on an unpoured net is a tree (`net_links` skips a joined pair), and rule 2 keeps tees off a
current path's links, so current enters or leaves a path only at a station. The compiler checks the
declared currents there (language v1-6). At a station some path **enters** (it is that path's last
station), the currents of the paths that **leave** it (it is their first station) may not exceed the
currents that enter it; an off-path branch never carries a path's current, so it cannot make up the
difference. A station no path enters is a source (a connector) and is not judged. When exactly one path
enters, and when several enter, it prints the sums and names each line (illustrative file, lines 90-92):

```
3V3: Path line 91 and Path line 92 leave C1.1 with 1.2 A between them, and Path line 90 brings 1 A to it, the only Path that enters C1.1; currents at a station must balance, and an off-path branch carries none of a Path's
```

Two notes stay notes:

```
3V3: off-path pads may end on the stations of Path line 90 (J1.1, C1.1); its copper is sized for its 1.2 A, and an off-path branch's draw (up to the net's 0.2 A) is not added
SW: L1 is a two-pad part between 5V and SW; Path line 62 carries 2 A out of L1.2, so if L1 passes DC, L1.1 brings 2 A in, and SW gives no amps= (its 0.3 mm carries 0.999 A on F.Cu)
```

The second (physics v1-R1) fires when a path with `amps=` names a pad of a two-pad part whose other pad
is on a net that carries less (no path through it and a smaller net current). Parts made by
`Capacitor()` are skipped, since a capacitor passes no DC; anything else may, and the note says "if".
On the §5 buck it does not fire, because `SW` carries its own path; it fires on the §5 file with line 60
left out (the text above) and on any series resistor a current path names whose other net declares less.

---

## 4. What each stage reads

### 4.1 The compiled form (`constraints.py`)

```python
@dataclass(frozen=True)
class Link:
    net: str
    a: tuple[str, ...]            # resolved pad ids of the station
    b: tuple[str, ...]
    written: tuple[str, str]      # as the author wrote them
    source: str                   # "Path line 62", or "Path line N (station J1.VBUS)" for a join
    key: str                      # "C_OUT1.1~R_FB_TOP.1", from RESOLVED pad ids (first pad of each station)
    width: tuple[tuple[str, Derived], ...]   # per allowed layer
    via: ViaSpec                  # allowed and the group are the link's; diameter and drill the net's
    group: ViaGroup | None        # count, pitch, landing width, worst-barrel share (§4.3); None without a current
    layers: tuple[str, ...]
    current: CurrentSpec | None
    keep_away: tuple[KeepAway, ...]
    sense: bool
    join: bool                    # a multi-pad station's join (§1.2)

@dataclass(frozen=True)
class PathSpec:
    net: str
    stations: tuple[tuple[str, ...], ...]
    line: int
    kind: str | None
    links: tuple[Link, ...]       # station links, then joins
    airwire_max_mm: float | None
```

`Constraint` gains `paths` (every path in board order, which also mends the last-chain-only report) and
`off_path` (pad ids), both compiled since station resolution is pure. `ConstraintSet` gains
`link(net, pad_a, pad_b)` keyed by pad id through a `station_of` map (two pads of one multi-pad station
map to its join; `None` for non-consecutive stations); `link_by_key(key)` for the gates, which hold only
a role owner (code R3-8); `pad_width(net, pad, layer)`, the widest link at the pad or the net's width
where an off-path branch may end; and `widest(net)` / `narrowest(net)` over every layer of every link
and the net's width. `Constraint.width_mm` keeps its name and now means exactly the width of branches
no path names (on the layer the net's report line names).

**Via size is the net's** (code R3-14): router vias are sized from `nc.via_dia` / `nc.via_drill`
(`route_native.py:1158`), which a link takes from the net's class via, the size KiCad's class rule
names. Only `allowed` and the group are the link's.

### 4.2 The router

- **`route_cost.link_cost(cs, net, pad_a, pad_b) -> NetCost`** (new) returns the link's width per layer,
  layers, `via_allowed`, the via group and keep-aways, else `net_cost`. Clearance, via budget and
  length are always the net's. `NetCost` gains `link`, `group` and `keep_away`, all `compare=False`, so
  PR 2's `link_cost == net_cost` test holds (code R3-7), and `width_on(layer)`.
- **Every router read of a width reads `width_on(L)` for the searched layer** (code v1-R2). Today the
  `Router` reads `nc.width_mm` in seven places: `pitch_mm`, the bend term, `via_mm`, the free-cell test
  (`cell_free`, `route_cost.py:333`, `slack >= width/2`), `_lane_ok` (338), `legal` (349, which builds
  the candidate segments `blocked` judges) and `pad_exits` (502); and `_runs_to_pieces` lays the runs.
  All eight move to the layer's width in PR 2, so a `B.Cu` run is searched, judged and laid at the same
  number. For a net, or a link with no current, every layer has one width and nothing changes.
- **`route_nets`** (`route_native.py:956`): `lay(i, j)` asks `link_cost` for the two terminals' owners and
  computes `route_layers` per link (once per net today, `route_native.py:972`), passing both to `_link`
  and `_runs_to_pieces`, which stamp `Piece.link`. `Failed` carries its `NetCost`, so `move_lines` and
  `write_problems` print the link's numbers.
- **`net_links`**: path station links and joins first, then Kruskal with §3.2's terminal filter. On a net
  whose paths carry a field or kind it never pairs two non-consecutive stations of one path (that link
  would bypass a station and lay a path's current on copper sized for none); a failed station link
  prints its move and the net stays open. Elsewhere Kruskal is as today.
- **`net_order`** sorts by `-cs.widest(n)`.
- **Moves name the line a bound came from.** `_via_move` (`route_native.py:1241-1262`) reads `nc.prov`:
  when `via_allowed` or `layers` came from a `Path`, the edit is on that line. `_source_of`
  (`route_native.py:1205-1222`) names a blocker that is another link of the same net, with its line.
- **Role owners** (code R3-4, v1-R10). On every net whose paths carry a field or kind, a link's pieces
  are grouped `pcbc:route:<link key>` and the off-path links `pcbc:route:<net>`; elsewhere grouping is as
  today, so the five boards' group names do not move. **The group is built from `Piece.link`, and
  `Piece.owner` stays the net** for router pieces: `ViaTerminal.ref` (`route_native.py:684-686`) reads an
  owner containing "." as a footprint ref, so `C_OUT1.1~R_FB_TOP.1` as an owner would parse as `C_OUT1`
  and exempt its fanout lanes. The emitted-board checks find a path's copper by the group and
  `link_by_key`. Keying by "has a field or kind", not by "numbers differ", matters because the `analog`
  and `sense` presets give identical numbers.
- **Pairs**: `route_pairs` is unchanged; a path on a pair net carries no field.

### 4.3 Via groups at a layer change (answer 2)

`route_cost` already prices a change as `via_mm = per_change × π (via_dia/2 + clearance)² / pitch_mm`
(`route_cost.py:88-92`), with `per_change` compiled from `vias_per_change(amps, drill)`
(`stackup.py:922-926`). What is missing is legality and copper: `_via_legal` judges one via
(`route_cost.py:369-391`) and `_runs_to_pieces` drops one via per change (`route_native.py:1157-1158`).

**Why not the stitch ring.** The long draft hung `per_change - 1` twins on a ring around an anchor,
joined by rungs, within `VIA_PARALLEL_MM` = 1.0 mm. Three findings retire that shape:

- On buck it cannot be placed at all: a 0.8 mm ring needs 1.3 mm to the next not to merge antipads in
  the `B.Cu` GND pour, and the ring stops at 1.0 mm (§0, measured: the stitch carrier refused all 24
  sites at each `VIN` via). On node the class via needs 1.080 mm (`patterns/stitch.py:34`). The 1.0 mm
  reach is the cluster walk's linkage distance, not a property of the copper (physics v1-R5).
- An anchor-plus-twins group does not share current: the anchor's current crosses its barrel alone,
  a twin's crosses two rungs and its barrel. Physics v1-R2's lumped model puts the anchor at 89 to 111 %
  of 0.871 A at 2 A with 3 vias, and over its rating in every bracket at 2.5 A; the count gate rates
  every member at an equal share and would certify that (physics v1-R2).
- A rung "at the link's width" has two widths after §2 (0.781 mm `F.Cu`, 1.09 mm `B.Cu` on buck's
  trunk), and a `B.Cu` rung at 0.781 mm would fail pcbc's own per-layer gate (physics v1-R6).

**The group** (version 1): a **flow-through line**. `n` barrels in a straight line at the board's
**group pitch**, with **both layers' copper running over every barrel**: the arriving layer's copper
enters at the first barrel and runs to the last, the leaving layer's copper starts at the first and
leaves from the last. Both runs (the **landing**) are laid at the wider of the link's two layer widths,
so each is at least its own layer's width and the per-layer gate passes on pcbc's own copper. Every
barrel's current then crosses the same length of landing, and the barrels are joined by the link's own
copper, so no rung exists to be lost.

- **Pitch**: `max(ClearanceTable.via_pitch + grid, antipad_pitch)`. `via_pitch` is the hole-to-hole and
  ring arithmetic the stitch ring already uses, and its first radius is refused by `clears`, hence one
  grid step more (code v1-R11: 0.95 mm on buck, not 0.9); `antipad_pitch`
  (`patterns/stitch.py:2324`, judged by `route_scene.antipad_clash`, `route_scene.py:347`) is the
  distance at which antipads stop merging in a plane the via passes but does not join: 1.3 mm on buck's
  `B.Cu` pour. So on buck a group is 1.3 mm pitch, 2.6 mm long for 3 vias. `antipad_clash` is
  unchanged; a landing on the pour layer already cuts the pour between its barrels, so relaxing it there
  is a later change, not a version 1 one. Twin-to-twin pitch is the same number, since the line has
  only neighbours.
- **Count** (answer 2, physics v1-R2): the smallest `n`, starting at `vias_per_change(amps, drill)`,
  whose **worst barrel** carries at most `via_amps(drill)` by a lumped DC model of the flow-through
  ladder (barrel resistance from `via_barrel`'s drill, plating and board thickness; landing segments from
  the copper weight, width and pitch). The model is a new pure function `stackup.via_group(amps, drill,
  plating, dt, pitch, width)`, returning the count and the worst barrel's share; it is a lumped model,
  not a field solve, and the report says so. Measured with this model (jlcpcb_2l_1oz, 0.4 mm drill,
  0.018 mm plating, 1 oz, landing at the wider layer width; `scratchpad/netpath/v1r/ladder.py`,
  `sweep.py`, `sweep2.py`):

  | current | landing | equal-share count | worst-barrel count at 0.95 / 1.3 mm pitch | worst barrel |
  |---|---|---|---|---|
  | 1.0 A | 0.309 mm | 2 | 2 / 2 | 0.500 A |
  | 1.5 A | 0.646 mm | 2 | 2 / 2 | 0.750 A |
  | 2.0 A | 1.090 mm | 3 | 3 / 3 | 0.732 / 0.750 A |
  | 2.5 A | 1.634 mm | 3 | 4 / 4 | 0.748 / 0.782 A |
  | 3.0 A | 2.276 mm | 4 | 4 / 5 | 0.862 / 0.817 A |

  The same model shows why the landing matters: with each layer at its own width (0.781 / 1.09 mm) a
  3-via line at 2 A has a worst barrel of 0.820 to 0.853 A, and at 2.5 A no count up to 8 at 1.3 mm
  brings it under 0.871 A; current crowds into the end barrels. Arriving and leaving at the **same** end
  puts 139 to 150 % of the rating on the first barrel at 2 A, which is the anchor problem in another
  form. So the shape is not optional: flow-through, landing at the wider width.
- **Legality**: `_via_legal(at)` becomes "every barrel of the group passes today's tests (`antipad_clash`
  included) and both landings pass `blocked`" (and `link_clashes` from PR 5). A change is legal only
  where the whole group fits.
- **Cost**: `via_mm` scales with the group's count, and the landing is priced as length on both layers,
  so the search weighs the group against the single-layer detour and takes the cheaper: the most
  efficient route that meets every criterion.
- **Never a refusal for the count.** Nothing is refused because the count is more than one. If no group
  fits and no single-layer route exists, the link fails like any link, with a route move naming the
  group's count, pitch and length, and the net stays open.
- **By current, not by who drew the via** (physics v1-R3). Every layer change a link's current crosses
  carries the link's group: a link may start or end on a `ViaTerminal` (`route_native.py:672-686`: a
  fanout escape, a tap, an earlier link's change, a core via) only if that via's group already meets
  the link's count, else that terminal is not offered for it; a fanout escape of a station pad of a
  current link is laid as that link's group or not at all (the §4.4 fanout row), in which case the router
  leaves the pad directly.
- **Budget**: each via counts against `via_count_max` (`vias_used`, `route_native.py:994`). Only `clock`
  and `usb_hs` take `vias_max` (`constraints.py:865-866`) and neither takes a current, so a group never
  meets a budget.
- **Group pieces** carry their own reason, `route_group`, outside `RELAXABLE` (`route_relax.py:60`), so
  the relaxer cannot move one barrel; `merge_overlaps` and `prune_dangling` treat a group as one item
  (code v1-R11). `RouteOut.stats["groups"]` records every placed group (link key, count, pitch, length,
  worst barrel share), which PR 4's fixture and PR 10's record read (code v1-R3).
- **The stitch `parallel` carrier** skips vias in a `route_group` and counts group members by their
  landing (`route_verify.parallel_joined`, `route_verify.py:1611`), not by distance, so on node, where
  group pitch exceeds 1.0 mm, it does not see three lone vias and add rungs.

PR 4 applies this to links whose current comes from a path; PR 9 to the net's own links on unpoured
nets. A poured net's vias are taps that carry one pad's share (the exemption `_parallel_specs` already
makes, `patterns/stitch.py:1179`); sizing them is §10 A.

### 4.4 Patterns and other readers of a width

`patterns/spine.py` skips a net with more than one path, or a path with a field or kind, with a note
naming the line (on buck the spine lays the tap today, §0); a net with no path or one field-less path is
spined as before. `patterns/chain.py` is in no stage; its `ChainSpec.width` becomes one per link. The
rest read a stated number:

| reader | today | version 1 |
|---|---|---|
| `copper.power_ampacity_failures` (fatal, `fab.py:851,863`) | each pcbc piece against the class width, only nets with `kind == "power"` and `amps >= 0.2` (`copper.py:342`) | each piece against its owner's link width **on the piece's layer**; a **second loop** over the nets carrying a link with a current, since the migrated `5V` has no `NetReq` amps and `SW` is not `power` (code v1-R9) |
| `fab._owned_copper` (`fab.py:556-558`) | returns `(net, reason, width)`, dropping the role owner and the layer | returns `(net, reason, width, owner, layer)`; the layer is `roles_doc`'s `key[1]` (code v1-R9) |
| `layout_check.py:246` | `con.width_mm` | `cs.narrowest(net)` |
| `fanout._width` (`fanout.py:40-43`), `patterns/tap.py:211, 399-401` | class width | `cs.pad_width(net, pad, layer)` |
| `fanout` escape via (`fanout.py:53`, one stub and one via per closed-row pad) | one via | for a station pad of a current link, that link's group or no escape (§4.3) |
| `route_channel.class_widths` (`route_channel.py:938-949`) | class width | `cs.widest(net)` |
| `patterns/stitch.py:711, 1281` (guard) | net width | `cs.widest(net)` |
| `patterns/stitch.py:1185` (parallel rating) | the net's current | the current of the via's link, else the net's; skips `route_group` vias |
| `patterns/hop.py:85-87` | net width | the link's, which is the net's on every legal board: a two-pad net's path names every pad and takes no field (code R3-16) |
| `layout_emit.py:132` | net width | unchanged (a core line names no link) |
| `dru.py:84-85` class `track_width` | one rule per class | a net with a link narrower than its class leaves the class rule for `width_net_<net>` at `cs.narrowest(net)`; from PR 9 a per-layer rule where a class's width differs by layer |

**Necking into a station pad** (physics v1-R1). A link is laid at its width up to the pad; router copper
never necks (`route` is not in `PATTERN_NECK_OK`, `copper.py:293`). Buck's `SW` path leaves the
TPS54202's SOT-23-6 pin 2 (pad 0.532 mm wide, pins at 0.95 mm pitch): 0.781 mm straight out of the pad's
end leaves 0.95 − 0.39 − 0.266 = 0.29 mm to pins 1 and 3, above the 0.2 mm clearance (arithmetic on
physics v1-R1's pad width, not measured). Where a pad cannot take the link's width, the link fails with
a route move; a router neck is not in version 1.

### 4.5 Placement

The placer reads nothing new and a path implies no `to=`. The checks read links and the attachment the
author already wrote; every new message names parts, net, distance, the link's width and clearance, and
**the line to edit**, and carries no pose, no `Place()` line to paste and no number the tool chose
(language v1-13).

| check | version 1 |
|---|---|
| `check_chains` (`route_checks.py:613`) | corridor per link at `link width / 2 + clearance`; the order message rewritten (below, *changed*) |
| `check_corridors` (`route_checks.py:834`) | declared links at their width, then an MST over off-path components at the net width |
| `check_airwires` (`route_checks.py:469`) | also each path's station sum against `Path(max_mm=)` or its kind's preset |
| `check_keep_away` (`route_checks.py:1171`) | also each path's straight station-to-station line against the other net's pads |
| decap distance, `layout_report` (`pcb_place.py:620-676`) | when the cap's `Place(to=)` names a pin of a part with more than two pads on the cap's power net, that pin; else the guess (code v1-R13: `Place("C_OUT1", to="L1.2")` names a two-pad inductor and stays on the guess, so buck's output caps get no new note) |
| `place_solve.facing_notes` | a station pad measured to its path neighbours, a sense far station only to its junction; off-path pads as today (language R3-10). Built only if the owner lands Grok's uncommitted `place_solve` change (§9 PR 6) |

Formats of the two new messages (§5 buck file):

```
5V path L1.2 -> C_OUT1.1 -> J_OUT.1 (Path line 62): 11.6 mm between stations, over max_mm=8 on that line; 0.781 mm wide on F.Cu, clearance 0.2 mm; J_OUT.1 is the farthest station (Place line 71): raise max_mm on Path line 62, or move J_OUT
5V path C_OUT1.1 -> R_FB_TOP.1 (Path line 64): no way between the stations keeps keep_clear_mm=2 from SW inside the board: L1.1 closes it; 0.2 mm wide, clearance 0.2 mm: keep_clear_mm=1.2 on Path line 64, or move R_FB_TOP (Place line 79) or L1 (Place line 74)
```

(Refutation round 1, defect 7: the keep-away check first judged the straight chord between the
stations, so an inductor between the IC and the output cap always warned, at a negative distance no
`keep_clear_mm` could clear. It now asks whether any way around the other net's pads, each grown by the
keep-away and half the width, joins the stations inside the board (a visibility graph over the grown
pads' corners) and within the path's airwire budget; "lower keep_clear_mm" is offered only with the
largest value, at or over the clearance, that opens one. A station that itself sits inside the
keep-away is said as such. The illustrative numbers above are not measured.)

A third message (refutation round 1, defect 6), from `route_checks.check_station_escapes`: a track of
the link's width must be able to leave each station pad in one of 16 directions, against every
other-net pad at what the net owes it (`ClearanceTable.between`: clearance or keep-away). Measured on
the refuters' 3.3 V buck, where `NetReq("FB", kind="feedback", keep_clear_of="SW")` holds 3 mm around
U1's own FB pin, 1.30 mm from its SW pin:

```
SW path U1.SW -> L1.1 (Path line 37): no 0.525 mm track can leave U1.2 (U1.SW) on F.Cu: U1.FB (FB) is 1.30 mm from it and needs 3 mm (keep_clear_of SW on NetReq line 36); ...; U1 carries both SW and FB, so no Place() edit opens it: lower keep_clear_mm on NetReq line 36 (now 3 mm) or drop its keep_clear_of; or move R_TOP (Place line 62)
```

**The order message, rewritten** (language v1-5). `check_chains` prints today (`route_checks.py:644-647`)
`... must come first: Place("{ref_i}", to="{ref_j}.{pin}", toward="{toward}")`, a pasteable pose with a
direction taken from the placement being judged. In PR 6 it becomes:

```
Path line 62: C_OUT1.1 projects after J_OUT.1 along the feed (t = 9.2 vs 7.1 mm); Place line 75 attaches C_OUT1 to L1.2: the stations are fed in the order Path line 62 writes them
```

No `toward=`. Its lower-case corridor message (`route_checks.py:656`, `"{net} chain: ..."`) becomes
`"{net} path (Path line N): ..."`. The implicit-chain move (`route_checks.py:675`, `say the order:
Chain(...)`) prints an order fitted to the placement's principal axis; it becomes a statement with no
order chosen:

```
USB_DP has 3 pads (J1.A6, U1.27, U3.1): a third pad off the line is a stub, and no Path gives their feed order
```

**Existing messages that hand the AI a line to paste**, listed so they are not mistaken for this
design's style; version 1 changes only those marked *changed*. `route_checks.py`: `_place_line` (349),
`check_airwires` (506), `_even_move` (577), `check_chains` (646, *changed*, above; 675, *changed*),
`check_loops`' decap note (1079) and hot-loop move (1163, *changed*, §6), `check_keep_away` (1217),
`_keepout_escape` (1302-1308), `check_isolation` (1400), `check_bridge` (1558, 1570).
`pcb_place.layout_report`: the fanout-lane message (613), the decap move (675, *changed* when the cap's
`Place(to=)` names an IC pin), the `max_mm` move (702) and the two edge messages (717, 724) (code R3-12).
The constraint report's keep-away hint (`_report`, `constraints.py:2065-2070`, `FB: keep_clear_of none;
NetReq("FB", kind="analog", keep_clear_of="SW") holds 3 mm`), which §5 explains buck does not take
(language R3-11).

### 4.6 The emitted board

- **Width** (fatal): `copper.power_ampacity_failures` judges each piece against its link's width on the
  piece's layer, at that link's `temp_rise_c` (code R3-9), with the owner and layer `fab._owned_copper`
  now passes (§4.4).
- **Vias and layers** (fatal): in a link's owned copper, a via where the link has `vias=False`, or copper
  on a layer it does not allow, beside `copper.vias_on_no_via_nets` (`copper.py:261`, `fab.py:837`).
- **Via groups** (fatal on router copper): at each layer change on a link with a current (and from PR 9
  on a current net's own links), the group's members are found by its `route_group` owner and joined by
  its landings (`route_verify.parallel_joined`), never by distance; the check recomputes the **worst
  barrel's share** from the emitted count, pitch and landing widths with `stackup.via_group`, and fails
  when it exceeds `via_amps`. An `@<pad>` escape of a station pad and a `ViaTerminal` a link started on
  are judged at the link's current (physics v1-R3). The router placed the group, so a short one is
  pcbc's own defect.
- **Ampacity measurement** (non-fatal), on **a net with a path that writes `amps=` or `kind=`**, written
  once (language R3-3):
  - each path with `amps=` is checked for capacity between consecutive stations at its own amps and
    temperature rise (`_walk`'s widest series path, which already copes with a cycle; code R3-15); a
    multi-pad station is walked from its anchor to each other pad at its join's current;
  - each off-path pad is checked from the pad to the station pad or link copper it joins, at the net's
    amps;
  - on a net whose paths are **only** sense paths, today's all-pairs `_walk` still runs at the net's
    amps, over the net's copper with each sense path's copper beyond its junction removed, between every
    pad except the sense paths' far stations (language v1-1). This keeps today's protection for the load
    copper of a shunt. It does not catch a backwards sense path whose far station is the load point;
    the compile rule (a) of §3.2 does;
  - no pair of station pads is otherwise walked and nothing is summed. Every other net keeps today's
    all-pairs `_walk`. Each piece is rated at its own layer's plane height, not the net's one
    `plane_h_mm` (`ampacity.py:314`).
- **Junctions and keep-away**: `route_verify.path_junctions` and `path_keep_away` (§3.2), measurements.
- **KiCad** checks the per-net width rule of §4.4. A per-link rule could reach it through `A.fromTo()`
  (in KiCad 10's `pcbexpr_functions.cpp`); whether it selects the right tracks is not measured. PR 8
  carries the mutation spike; until it passes, per-link numbers are pcbc's measurements, never called
  verified.

---

## 5. Before and after: buck

Before, `examples/buck/buck.py:55-57`:

```python
NetReq("VIN", "5V", "GND", kind="power", volts=12, amps=2)
NetReq("SW", kind="switch_node", max_mm=6)
NetReq("FB", kind="analog")
```

After (**the §5 buck file**: lines 55-66; the import on line 4 adds `Loop, Path`; every later line moves
down by nine, so `Place("C_IN1", to="U1.VIN")` becomes line 72, `Place("L1", to="U1.SW")` line 74,
`Place("C_OUT1", to="L1.2")` line 75 and `Place("R_FB_TOP", to="U1.FB")` line 79):

```python
NetReq("VIN", "GND", kind="power", volts=12, amps=2)                # 55
NetReq("5V", kind="power", volts=5)                                 # 56
NetReq("SW", kind="switch_node", max_mm=6)                          # 57
NetReq("FB", kind="analog")                                         # 58
# The switch current: the regulator's SW pin to the inductor. The boot cap's branch stays thin.
Path("SW", "U1.SW", "L1.1", amps=2)                                 # 60
# The load current: the inductor's output, through the first output cap, to the connector.
Path("5V", "L1.2", "C_OUT1.1", "J_OUT.1", amps=2)                   # 62
# The divider's top: leaves the rail inside the output cap's pad and touches 5V nowhere else.
Path("5V", "C_OUT1.1", "R_FB_TOP.1", kind="feedback")               # 64
# The input hot loop, checked at placement.
Loop("C_IN1.1", "U1.VIN", "U1.GND", "C_IN1.2")                      # 66
```

Why:

- **`VIN` and `GND` keep `amps=2` on the `NetReq`; `5V` does not.** `VIN`'s light branch, `R_EN.1` (a
  0.12 mA pull-up), may carry 2 A-sized copper without harm; `5V`'s may not: the tap must leave at the
  output cap and stay thin. From PR 9, which lands before this migration, `VIN`'s own links get 1.09 mm
  on `B.Cu` and a 3-via group at any layer change, so the migrated example meets answer 2 on `VIN` too
  (physics v1-R4, language v1-4). The reviewers' other option, `Path("VIN", "J_IN.1", "C_IN1.1",
  "U1.VIN", amps=2)` with `R_EN.1` and `C_IN2.1` off-path at 0.4 mm, is equally valid physics; it is not
  taken because the doc's rule puts a current on the `NetReq` when every branch may carry it without
  harm, and one rule is what the AI copies. `GND` is poured and a `Path` there takes no field in
  version 1 (§7), so no return is written.
- **`SW` carries its switch current on a path** (physics v1-R1). The `U1.SW -> L1.1` copper carries the
  inductor current, 2 A DC plus 0.112 A rms ripple, and is 0.3 mm today, which carries 0.999 A. `amps=`
  is legal on `switch_node`; the path gets 0.781 mm on `F.Cu`, the only layer `switch_node` allows, no
  vias, and the net's `max_mm=6` still holds. `C_BOOT.2` stays off-path at the preset 0.3 mm and, by
  rule 2, joins only inside `U1.SW` or `L1.1` (on the 2026-09-27 build it already ends on `U1.SW`'s pad).
- **The trunk is `L1.2 -> C_OUT1.1 -> J_OUT.1`.** A trunk through both caps fails today's order check on
  the stock placement (`C_OUT1.1 projects after C_OUT2.1 along the feed`, measured round 2); this one
  passes.
- **`C_OUT2.1` is off-path at 0.4 mm.** It carries only AC, its share of the inductor ripple: (12 − 5) ×
  (5/12) / (15 µH × 500 kHz) = 0.389 A peak to peak, 0.112 A rms, about 0.056 A per cap (physics R3-9).
  Rule 2 makes it end on a trunk station pad, never mid-trunk. Its closeness is stated by `Place("C_OUT2",
  to="L1.2")` (line 76).
- **One `Loop()`, not two** (physics v1-R8). The draft's second loop, `C_OUT1.1 -> C_OUT2.1 -> C_OUT2.2
  -> C_OUT1.2`, encloses only the ripple two parallel caps exchange, about 0.056 A rms each with no
  switching edge, and was judged against the 6 mm² decap default, which has no physical basis for it.
  In the example the AI copies it would teach `Loop()` as a proximity tool. The output-side hot loop
  (L1 to C_OUT to GND to U1.GND) closes through the pour, which is §10 C.
- **The tap carries no number**: `kind="feedback"` gives 0.2 mm, `F.Cu`, no vias, 15 mm. `C_OUT1.1`, its
  first station, is the junction; written the other way round it is refused (§7).
- **The hot loop** names the four pads the guess chose, agrees with `Place("C_IN1", to="U1.VIN")`, and
  replaces the guess for `C_IN1` and `U1`.
- **No keep-away on `FB`**, though the report suggests one (§4.5): writing it makes the stock placement
  print `FB: R_FB_TOP.2 is 1.62 mm from C_BOOT.2 (SW); keep_clear_mm=3 ...` (measured, round 2), a move
  that cannot be made because the boot cap belongs at `BOOT` (`docs/constraints.md:346-351`).
- **`volts=5` on `5V`**: before, it took 12 V from sharing `VIN`'s line.

The compiler's report; lines marked *m* were measured on `scratchpad/netpath/v1/buck/buck_split.py`
(today's code, `NetReq` split only), the rest are predicted (and reflect PR 9 for `VIN`):

```
VIN: width 0.781 mm on F.Cu (ipc2221_ext 2 A 10 C 1 oz; plane 0.593 at 1.53 mm B.Cu pour), 1.09 mm on B.Cu (ipc2152_fit, no plane); 3 vias per layer change, flow-through at 1.3 mm pitch, worst barrel 0.750 A of 0.871 (via_barrel 0.4/0.018 mm at 10 C; lumped DC model)
5V: width 0.4 mm (pcbc_floor amps >= 0.2; ipc2221_ext 0.2 A 10 C 1 oz 0.150; ipc2152_fit x board 1.092 x plane 0.593 at 1.53 mm B.Cu pour 0.010; below 0.274 A the IPC-2152 fit extrapolates)   m
5V: clearance 0.2 mm (preset power; ipc2221_6_1 row 0-15 V B2 0.1)   m
5V: via 0.8/0.4 mm (preset power), 1 per layer change (via_barrel 0.4/0.018 mm 0.871 A at 10 C) [report only in R1]   m
5V: layers F.Cu, B.Cu (preset power)   m
5V: path L1.2 -> C_OUT1.1 -> J_OUT.1 (Path line 62): width 0.781 mm on F.Cu (ipc2221_ext 2 A 10 C 1 oz; plane 0.593 at 1.53 mm B.Cu pour), 1.09 mm on B.Cu (ipc2152_fit, no plane); 3 vias per layer change, flow-through at 1.3 mm pitch, worst barrel 0.750 A of 0.871 (via_barrel 0.4/0.018 mm at 10 C; lumped DC model)
5V: path C_OUT1.1 -> R_FB_TOP.1 (Path line 64, kind feedback): width 0.2 mm (preset feedback), no vias, layers F.Cu, airwire 15 mm; meets 5V only inside C_OUT1.1, its first station
5V: C_OUT2.1 is on no Path; its branch is 0.4 mm (NetReq line 56 gives no amps=; preset power 0.2 A), which carries 1.231 A on F.Cu (over the B.Cu pour) and 1.152 A on B.Cu, not Path line 62's 2 A
SW: path U1.SW -> L1.1 (Path line 60): width 0.781 mm on F.Cu (ipc2221_ext 2 A 10 C 1 oz; plane 0.593 at 1.53 mm B.Cu pour), no vias, layers F.Cu (preset switch_node)
SW: C_BOOT.2 is on no Path; its branch is 0.3 mm (NetReq line 57 gives no amps=; preset switch_node), which carries 0.999 A on F.Cu, not Path line 60's 2 A
FB: keep_clear_of none; NetReq("FB", kind="analog", keep_clear_of="SW") holds 3 mm   m
class Power_2 (Power is NetReq line 55 at 0.781 mm)   m
loop C_IN1.1 -> U1.VIN -> U1.GND -> C_IN1.2 (Loop line 66): 20 mm2 (pcbc default; U1 carries SW)
```

The predicted trunk widths are the measured numbers of §0; the group's count, pitch and worst barrel
are §4.3's measured model numbers. KiCad gets `width_net_5V` at 0.2 mm and `width_net_SW` at 0.3 mm;
`Power` keeps its rule over `VIN` and `GND`, per layer from PR 9.

What the router reads (before is the 2026-09-27 build):

| link | before | after |
|---|---|---|
| `U1.SW - L1.1` | router, 0.3 mm, `F.Cu` | 0.781 mm, `F.Cu`, no vias (Path line 60); `C_BOOT.2` joins only inside its stations |
| `L1.2 - C_OUT1.1` | spine trunk, 0.781 mm | 0.781 mm on `F.Cu`, 1.09 mm on `B.Cu`, 3-via flow-through group at any change (Path line 62); first |
| `C_OUT1.1 - J_OUT.1` | router, 0.781 mm | the same |
| `R_FB_TOP.1 - L1.2` | spine trunk, 0.781 mm, from the inductor's pad | not a link |
| `C_OUT1.1 - R_FB_TOP.1` | not a link | 0.2 mm, `F.Cu`, no vias, touching `5V` only inside `C_OUT1.1` (Path line 64) |
| `C_OUT2.1` | spine trunk, 0.781 mm | Kruskal at 0.4 mm, ending on a trunk station pad |
| `VIN` | 0.781 mm on `B.Cu`, three lone vias | from PR 9: 1.09 mm on `B.Cu`, a 3-via group at any change |
| `GND` | as built (routed copper on `F.Cu` at 0.781 mm; poured on `B.Cu`) | unchanged; its vias are taps (§4.3) |

The spine no longer takes `5V`. PR 10 records on the emitted board the tap's width and where it leaves
the rail (before: 0.781 mm from `L1.2`), the `SW` width (before: 0.3 mm), every placed group, the
off-path rows and the loop area.

**The backwards-compatible minimum** (a PR 5 fixture; **the minimum file**: buck.py with line 55 as it
is today and one line added at 58): `Path("5V", "C_OUT1.1", "R_FB_TOP.1", kind="feedback")`. The tap is
0.2 mm with junction `C_OUT1.1`; `L1.2`, `C_OUT2.1`, `J_OUT.1` are off-path at 0.781 mm and printed,
joined to each other or inside `C_OUT1.1`'s pad, never to the tap or `R_FB_TOP.1`; `pad_width("5V",
"C_OUT1.1")` is 0.781 mm; ampacity checks each off-path pad to its join at 2 A and holds the tap to
nothing; KiCad moves `5V` out of class `Power` to `width_net_5V` at 0.2 mm while `VIN` and `GND` stay
in `Power`. Written backwards, `Path("5V", "R_FB_TOP.1", "C_OUT1.1", kind="feedback")` is refused with
the swapped line (§7, rule (a)); round 3 found that form routing the output cap through the divider's
pad (language R3-1). It is a fixture too.

**The other examples.** blinky has no path; c3_usb and node have only pair paths, renamed in PR 1 and
otherwise unchanged. Read from the files, neither has a sense branch on an unpoured rail: node's
`T_DIV` divider hangs off `3V3` through `R_NTC` (`node.py:43`), and `3V3` is a plane (`node.py:96`); a
sense path on a poured net is §10 A.

### 5.1 A second worked example: a USB-C input (illustrative)

Language v1-2 wrote this from §1-§3 alone; it exercises the multi-pad end station. The connector is
node's `HRO/TYPE-C-31-M-12`: `VBUS` is pads `A4B9` (x = −2.4) and `B4A9` (x = +2.4), both on `F.Cu`,
with the eight 0.3 mm signal pads `A5`..`B8` between them. Illustrative file, lines 100-107, on
jlcpcb_2l_1oz:

```python
NetReq("VBUS", "VBUS_F", kind="power", volts=5)                       # 100
NetReq("GND", kind="power", volts=5, amps=1.5)                        # 101
NetReq("USB_DP", "USB_DN", kind="usb_hs", z_diff_ohm=90, pair=True)   # 102
Path("VBUS", "J1.VBUS", "F1.1", amps=1.5)                             # 103: J1.VBUS is an end station, pads A4B9 and B4A9
Path("VBUS_F", "F1.2", "C_IN.1", "U2.VIN", amps=1.5)                  # 104
Path("USB_DP", "J1.A6", "U3.1", "U1.IO19")                            # 105: one pad per station on a pair net
Path("USB_DN", "J1.A7", "U3.3", "U1.IO18")                            # 106
Loop("C_IN.1", "U2.VIN", "U2.GND", "C_IN.2")                          # 107
```

Predicted (widths and counts are §4.3's measured numbers at 1.5 A):

```
VBUS: path J1.VBUS (A4B9, B4A9) -> F1.1 (Path line 103): width 0.525 mm on F.Cu (plane at 1.53 mm B.Cu pour), 0.646 mm on B.Cu (no plane); 2 vias per layer change, flow-through, worst barrel 0.750 A of 0.871 (lumped DC model); J1.VBUS's pads joined at 1.5 A (join J1.A4B9~J1.B4A9, conservative: each pad carries part of the cable current)
VBUS: U3.VBUS is on no Path; its branch is 0.4 mm (NetReq line 100 gives no amps=; preset power 0.2 A), which carries 1.231 A on F.Cu (over the B.Cu pour) and 1.152 A on B.Cu, not Path line 103's 1.5 A
USB_DP: path J1.A6 -> U3.1 -> U1.IO19 (Path line 105)
```

The join cannot stay on `F.Cu` between the data pads, so it changes layer twice: two 2-via groups beside
0.5 mm-pitch pads, which `docs/direction.md` §5 names as where the router fails. If it fails, the
route move names the join and its line. If the board has no ESD `VBUS` pin, line 103 names every pad of
`VBUS` and is refused with the `NetReq` form (§7); from PR 9 that form gets the same width per layer and
the same groups, so nothing physical is lost.

---

## 6. `Loop()`: a check on named pads

```python
Loop("C_IN1.1", "U1.VIN", "U1.GND", "C_IN1.2")            # the hot loop, buck
Loop("C_MCU_HF.1", "U1.3V3", "U1.2", "C_MCU_HF.2")        # a decap loop, c3_usb (U1.2: the nearest GND pad, measured)
Loop("C_IN1.1", "U1.VIN", "U1.GND", "C_IN1.2", mm2=25)    # a budget the author chose
```

`Loop(*pads, mm2=None)` takes three or more stations and closes from the last back to the first. Each
step, the closing one included, stays on one net or crosses one part's body; anything else is refused.
A station is one pad; a multi-pad pin is refused naming its pads (c3_usb's `U1.GND` is 22). The budget is
`mm2=` when written, else `DEFAULT_LOOP_MM2` (`constraints.py:878`): 20 mm² when a part on a body step
has a pad on a `switch_node` net, else 6, both printed `pcbc default`. `docs/constraints.md` gets one
paragraph on where an author's own budget comes from and why it must not be fitted to the placement
being judged; the 6 mm² default is labelled a decap budget there, so it is not read as a proximity
check (physics v1-R8).

`check_loops` measures the shoelace area of the pads in the order written (`route_checks._loop_area`,
`route_checks.py:979`; a self-crossing loop is measured by its hull, `route_checks.py:979-990`, so a
reversed cap cannot cancel its own area). The shoelace of pad centres omits the pad-to-copper offsets
and the vertical leg through a pour, so it is a **lower bound** (physics v1-R9): over budget it is a
definite failure, under budget it is not proof. The pass line says so:

```
loop C_IN1.1 -> U1.VIN -> U1.GND -> C_IN1.2 (Loop line 66): pads enclose at least 3.3 mm2 of 20 (shoelace of pad centres; no vertical leg)
```

Over budget it is a warning naming the loop, area, budget, farthest station and the `Place()` lines
that put that station's part and its neighbour's where they are (refutation round 2: it named the anchor,
"no Place() attaches U1", instead of the line the author wrote). A declared loop replaces the guess for the caps and ICs it names, and the decap distance
check skips a cap a loop names. When a loop's step runs from a cap's pad to an IC pin on one net and the
cap's `Place(to=)` names another pin of that IC, the loop is refused: one attachment, one statement.

In version 1 a `Loop()` lays no copper, changes no routing, makes no pattern skip a net, adds no
junction, and is not measured on the emitted board (§10 C).

**The guessed hot-loop move** is one existing text version 1 rewrites. Today it ends in
`loop_mm2={suggested}` (`route_checks.py:1156-1163`), fitted to the placement being judged, and prints a
`Place()` even when the board has one. It becomes (format, §5 buck file):

```
SW: hot loop C_IN1.1 -> U1.VIN -> U1.GND -> C_IN1.2 encloses 22.9 mm2 over the 20 mm2 budget (preset switch_node, pcbc default); C_IN1.1 is 12.0 mm from U1.VIN (Place line 72 attaches C_IN1 there)
```

With no `Place(to=)` the parenthesis names the line that did place the part (`Place line 66 places U1`,
`Place line 70 puts J_IN on the left edge`) or says the part has no `Place()`. The pinned fixture,
`test_a_far_input_cap_opens_the_hot_loop` (`tests/test_route_checks.py:346-356`), places `C_IN1` with
`at=`, so its new string ends in that form (code R3-13).

`NetReq(loop_mm2=)` stays legal as the guessed loop's budget (test fixtures write it,
`tests/test_constraints.py:59`, `tests/test_dru.py:55`); on a net whose guess a `Loop()` replaced it is
refused.

---

## 7. Failure messages, verbatim

Refusals come from `compile_constraints` or the loader and fail the build. **Every refusal states what
failed and why, as facts — the file and line, the object, the measured numbers, the rule — and proposes no
edit** (owner, 2026-10-01: *"Remove suggestions in tool, just throw errors if unable to complete"*;
`docs/direction.md` §7b). This replaces language v1-7's "every refusal ends in `write:` and the rewritten
line": no message prints a rewritten line, a choice of lines, a `Place()` to change or a value to raise or
lower, and `tests/test_refutation_r2.py::test_no_message_of_the_tool_proposes_an_edit` scans every string
literal of `src/pcbc` for the shapes those proposals took. A fact about the board that is not an edit stays
(the part's pad that is on the path's net, the station a pad already is). Each example names its file
(code v1-R12): **(§5 buck)** is the §5 file, **(minimum)** the backwards-compatible minimum,
**(illustrative)** an addition to the §5 file at lines 80 and above. `Chain()`: §1.3.

**A station not on the net** (§5 buck):

```
Path("5V") line 64: R_FB_TOP.2 is on FB, not 5V; R_FB_TOP's 5V pad is R_FB_TOP.1
```

**A station named twice, a keep-away that names nothing** (illustrative; refutation round 2, each was
accepted silently):

```
Path("V5") line 42: L1.2 is station 1 (L1.2) again; a Path names each station once
Path("V5") line 44: keep_clear_of names 'SWN', and no net on this board matches it
Path("V5") line 44: keep_clear_of names 'V5', which is V5, this Path's own net
Path("V5") line 44: keep_clear_mm=2 sizes a keep-away, and this Path gives no keep_clear_of=
NetReq("FB") line 38: keep_clear_of names 'SWN', and no net on this board matches it
```

**A multi-pad station in the middle of a path, or on a pair net** (illustrative):

```
Path("VBUS") line 39: J1.VBUS is pads A4B9 and B4A9 of J1, and it is not an end of this Path; version 1 joins the pads of a multi-pad station only where a Path ends there (its join, §1.2), so in the middle of a Path nothing would carry the Path's current between J1.A4B9 and J1.B4A9
Path("USB_DP") line 42: U1.I/O1 is pads 1 and 6; a pair's Path names one pad per station
```

**A net field on `Path()`, or not a path field at all** (§5 buck; `_explain_type_error`):

```
Path("5V") line 62: volts= is a property of the whole net: every pad on 5V is one potential; it is a NetReq field (NetReq line 56, which already says volts=5)
Path("5V") line 62: vias_max= is not a path field; a Path takes amps, temp_rise_c, vias, layers, max_mm, keep_clear_of, keep_clear_mm, kind; a via budget, a routed length and an impedance target are NetReq fields
Path("SW") line 60: unexpected keyword 'amp'; it takes net=, amps=, temp_rise_c=, vias=, layers=, max_mm=, keep_clear_of=, keep_clear_mm=, kind=
```

**Kinds, pairs and currents** (§5 buck unless marked). A kind names what the net *is* (refutation round 2:
`kind="analog"` on a power net's Path used to be called "the net's" kind):

```
Path("5V") line 64: kind="analog" is a net kind, and 5V is kind power (NetReq line 56); on a Path, kind= names a sense branch and takes "feedback" or "sense"
Path("USB_DP") line 42 (illustrative): layers= on half of the pair USB_DN/USB_DP; a pair's numbers are on NetReq line 36, and a Path on a pair net names stations only
Path("5V") line 64: kind="feedback" takes no amps=; a sense branch carries a measurement, not a load
Path("FB") line 80 (illustrative): amps= on a path needs its net to take a current (kind power or switch_node), and FB is kind analog (NetReq line 58)
Path("SW") line 60: temp_rise_c= is not taken by kind switch_node (NetReq line 57 takes loop_mm2, amps and the common fields); a current on SW is sized at 10 C
Path("5V") line 62: temp_rise_c= sizes a current, and this Path gives no amps=
```

**A path naming every pad of its net** (illustrative). One statement, true whichever kind the NetReq
gives (refutation round 2: the kind case offered two NetReq lines, each of which was refused):

```
Path("SW") line 40 names every pad of SW (U1.2, L1.1, C5.2); its amps= hold on every branch, which makes them the net's, and SW's numbers are on NetReq line 37
(2026-10-02, refutation one-home r1: this FB line is now accepted; a kind= Path on every pad is the whole node's)
```

**Layers and vias that loosen a written value** (language R3-8, v1-10; illustrative, with `NetReq("FB",
kind="analog", layers=("F.Cu",), vias=False)` at line 84):

```
Path("FB") line 85: layers= names B.Cu, and NetReq line 84 writes layers=("F.Cu",) for FB; a path may loosen a default, never a written value
Path("FB") line 85: vias=True, and NetReq line 84 writes vias=False for FB; a path may loosen a default, never a written value
```

Loosening a default is not refused; the report says where the value came from (illustrative):

```
FB: path R_FB_TOP.2 -> U1.FB (Path line 87): layers F.Cu, B.Cu (Path line 87; preset analog has F.Cu); vias allowed (Path line 87; preset analog has none)
GATE: path U7.G -> R_G.1 (Path line 89, kind sense): layers In1.Cu (NetReq line 88 writes them; kind sense's F.Cu default yields to a written value)
```

**A current the impedance width cannot carry** (node's stackup, illustrative):

```
Path("VMOT") line 150: amps=2 needs 0.781 mm on F.Cu (current_width_mm: IPC-2221B over IPC-2152), and z_se_ohm=50 on NetReq line 149 holds 0.3244 mm, which carries 1.057 A
```

**An impedance with no reference plane** (refutation round 2: a two-layer board whose GND is named by no
power NetReq has no back pour, and widths and impedances used to be sized against it anyway):

```
Z50: 50 ohm on F.Cu (NetReq line 7) has no reference plane: this two-layer board pours B.Cu only when GND is a power net (route_scene.plane_targets), and no NetReq names GND with kind power
USB_DP: 90 ohm on F.Cu (NetReq line 143) has no reference plane: In1.Cu, the layer next to F.Cu, is not in Board(planes=)
```

**A sense path written backwards** (§3.2; (minimum) and illustrative). Since refutation round 2 also when
the far station is a connector's pad (reference designator `J` or `P`) or a pin its symbol types
`power_in`/`power_out`, where the net's current enters or leaves — the sense trace alone would feed it:

```
Path("5V") line 58 (minimum), kind feedback: its far station C_OUT1.1 is a pad of C_OUT1, whose other pad is on GND, which takes current, so it sits on the load; a sense Path starts where it leaves the rail
Path("5V") line 64, kind feedback: its far station J_OUT.1 is a pad of connector J_OUT, where its current comes onto or off the board; a sense Path's copper beyond its first station R_FB_TOP.1 reaches the rest of 5V only inside R_FB_TOP.1, so J_OUT.1's current would run in the sense trace
Path("VREF") line 82 (illustrative), kind sense: Path line 81 names U5.REFP, this Path's last station, and not U4.VOUT_S, its first; a sense Path starts at the station it shares
```

**Currents that do not balance at a station** (§3.3):

```
VBUS: Path line 40 leaves C1.1 with 1.5 A, and Path line 39 brings 1 A to it, the only Path that enters C1.1; currents at a station must balance, and an off-path branch carries none of a Path's
```

**A field or kind on a poured net's path**, naming the rule that poured it (code R3-1, language R3-6):

```
Path("GND") line 92: GND is poured (B.Cu; route_scene.plane_targets: NetReq line 55 is kind power and names GND), and a Path with amps= on a poured net is a return path, which version 1 does not build
Path("GND") line 35: GND is poured (B.Cu; route_scene.plane_targets: NetReq line 30 is kind power and names GND), and version 1 does not build a sense path on a poured net: the pour joins U1.REFN to GND wherever it reaches, so nothing can keep it off the rest of GND (a Kelvin tap); without kind= the line is not a Kelvin tap either, since U1.REFN still reaches the pour (net-path-physics.md §10 A: a sense path on a poured net is after version 1)
```

A field-less `Path` on a poured net keeps what a `Chain` does there today: its station links are routed
pad to pad at the net's numbers, `chain_order` returns `poured`, `check_chains` judges order and corridor,
and the report names the rule that poured the net.

**A link named twice** (illustrative):

```
5V: Path line 62 and Path line 93 both name C_OUT1.1 -> J_OUT.1; a link carries one width
```

**`Loop()`** (§5 buck; c3_usb for `U1.GND`; illustrative for line 94). A fact about a part (which of its
pads is on the next station's net) stays; the rewritten `Loop` does not:

```
Loop line 66: C_IN1.2 -> U1.VIN is neither one net (C_IN1.2 is on GND, U1.VIN on VIN) nor one part; each step of a loop stays on one net or crosses one part; C_IN1's VIN pad is C_IN1.1, station 4 of this loop
Loop line 271: U1.GND is 22 pads of U1 (1, 2, 11, 14, 36, 37, ...); a loop's station is one pad
Loop line 94: its step C1.1 -> U2.VDD2 lands on another pin of U2 (VDD2) than Place line 40, which attaches C1 to U2.VDD1; one attachment, one statement
SW: loop_mm2=25 on NetReq line 57 budgets the loop pcbc guesses through U1, and Loop line 66 declares that loop, so one loop has two budgets
```

**`Chain()`** (§1.3): `buck.py line 60: Chain() is now Path(), with the same arguments` and, for the import,
`buck.py line 4: the import names Chain, which pcbc no longer exports`. No rewritten line is printed;
`language.chain_rewrite` still exists (the test suite applies it to temp copies of boards it does not own).

**Notes, not refusals**: the off-path lines (§3.1, which now end at the measurement:
`5V: C_OUT2.1 is on no Path; its branch is 0.4 mm (...), which carries 1.231 A on F.Cu (over the B.Cu pour)
and 1.152 A on B.Cu, not Path line 62's 2 A`), the station notes and the two-pad series note (§3.3), the
junction the report names, and where a loosened default came from.

**Route errors** (§5 buck). One per link still missing: what did not route, at what numbers, what is in
the way and why each thing is (the line that placed a pad; for copper of the link's own net, the link it
belongs to and the junction rule), the rules that bound the link, and the problem file:

```
SW: U1.2 (16.46,9.96) -> L1.1 (19.71,13.996) (Path line 60) did not route at 0.781 mm on F.Cu + 0.2 mm clearance.
  In the way: VIN track (spine, written before this net, its NetReq board.py:55).
  Rules: vias are not allowed on this link (preset switch_node); layers F.Cu (preset switch_node).
  Problem file: layout/buck/routed/unrouted/SW-1.json
```

A link whose search was walled in names what holds the cells, the junction rule's own-net copper
included (refutation round 2: an off-path branch walled in by its own net's path copper was told
"nothing near them holds a cell"; `tests/fixtures/r2_b1`):

```
SW: C5.2 (15.47,7.464) cannot reach U1.2 (15.46,10.96) on F.Cu without vias at 0.3 mm + 0.2 mm clearance. The cells between the two ends are held by SW [SW, link U1.2~L1.1] at 0.2 mm (the junction rule: a path's copper meets the rest of its net only inside its station pads); U1.5 [EN] at 0.2 mm (class SwitchNode); U1.4 [FB] at 0.2 mm (class SwitchNode).
  In the way: SW track of link U1.2~L1.1 (route, laid earlier; a path's copper meets the rest of its net only inside its station pads); U1.5 pad [EN] — U1 is placed by board.py:62 Place('U1'); ...
  Rules: vias are not allowed on SW (preset switch_node).
```

**Emitted-board gates** (§4.6), fatal: a path link's copper against its width on its layer, its vias,
layers and via groups, the off-path copper against the net's width — on pcbc's copper **and on copper no
`pcbc:` role group owns** (`route_verify.gate_roles`, refutation round 2's blocker) — and the junction
rules (`route_verify.junction_lines`, fatal since round 2):

```
PWR core copper 0.75 mm on F.Cu < the 1.09 mm Path line 16's link R2.1~R3.1 needs on F.Cu (2 A)
PWR Path line 16's link R1.1~R2.1: the via group at (28.748,13.762) is 2 barrels at 2.032 mm pitch with a 1.09 mm landing; its worst barrel carries 1.000 A of 2 A, over the 0.871 A one holds (lumped DC model; the link needs 3)
PWR Path line 17's link R3.1~R5.1: copper on B.Cu, which the link does not allow (layers F.Cu, preset feedback)
a path's copper meets its net outside its stations: PWR: Path line 17's copper overlaps core copper of R2.1~R3.1 on F.Cu by 0.4905 mm, outside its first station R3.1's pad
```

---

## 8. Backwards compatibility

"Byte-identical" means the five boards' `layout.gen.py`, `.kicad_pcb`, `.kicad_dru` and report text; the
build JSON (`constraints.to_dict()`, `build.py:1072`) gains `paths` and `off_path`.

- **PR 1** (the rename) changes three boards' source lines and every message that said `Chain`; copper
  unchanged. The report's line for a field-less path is pinned from PR 1 on as
  `USB_DP: path J1.A6 -> U3.1 -> U1.27 (Path line 268)` (today `constraints.py:2077` prints `chain ...
  (Chain line 268)`); PR 2's "every path reported" uses the same format for a field-less path, so it
  changes no report text on the five boards (none has two chains on one net), and the `: width ...`
  suffix appears only on a path with a field or kind (PR 3 on). The DS2 Addon in place is refused until
  the owner's two-line edit (§1.3); the suite reads it through the PR 1 helper, so the DS2 rows of every
  test stay green (language v1-11, code v1-R1).
- **PRs 2-8** keep the five boards byte-identical: no path has a field or kind, so `link_cost ==
  net_cost` everywhere, the predicate never rejects, `merge_overlaps` and the role groups are unchanged,
  the spine runs as before, and no link has a group. Message changes, each pinned from its test diff:
  the decap check measures to a `Place(to=)` that names an IC pin (PR 6; buck's caps name `U1.VIN` for
  the input caps and `L1.2` for the output caps, so only the input caps' notes can change); the
  `check_chains` order, corridor and implicit-chain messages (PR 6); the guessed hot-loop move loses
  `loop_mm2={suggested}` (PR 7; it fires on no example as placed; one pinned string); the `Loop()` pass
  line (PR 7, only where a `Loop()` is written, so on no example before PR 10).
- **PR 9** changes every existing **unpoured** current net's copper that today breaks the two criteria.
  Measured by `perlayer.py` (§0): buck's `VIN` (0.781 mm on `B.Cu` where 1.09 mm is needed; three lone
  vias where a 3-via group is needed); node's `LOAD` and `VBUS` layer changes (2 vias each by equal
  share; their worst-barrel count on node's 4-layer stackup and drills is not measured and is part of
  the PR). Poured nets' vias are taps and unchanged; buck's `GND` routed
  copper is on `F.Cu` at 0.781 mm, which the per-layer width keeps. Floors set every width on c3_usb,
  node, blinky and the DS2 Addon, so their widths do not change (the per-layer width is below the
  compiled width on every net there). The PR measures all of this again.
- **PR 10** changes buck by design (§5).

---

## 9. PR plan (version 1, in build order)

Each PR leaves `pcbc build` green on the four repo boards and on the DS2 Addon through the PR 1 test
helper, and depends only on earlier PRs; `pcbc build` on the owner's DS2 file in place is refused from PR
1 until he makes his two-line edit (§1.3). No example writes a path field before PR 10.

**Before PR 4 (done this round).** Why the stitch `parallel` carrier left buck's `VIN` vias lone (code
v1-R14): measured on a scratch build, it refused every ring site at all three vias because the antipad
pitch (1.3 mm) exceeds `VIA_PARALLEL_MM` (1.0 mm) on buck (§0). §4.3's group shape follows from it.

**PR 1. `Chain` → `Path`.** `language.py`: `Path(net, *pads)`; `Chain` becomes the refusal of §1.3;
`pathlib` imported as a module (all 21 uses, §1.3); the one-argument clash hint; `load_board`'s
namespace (`"Chain": Chain`, `language.py:2348`), `_BOARD_NAMES` (2491) and `_CONSTRUCTORS` (2742) gain
`"Path"` and keep `"Chain"` pointing at the refusal. `model.py`: `PathReq`, `Design.paths`;
`__init__.py` exports `Path` and keeps `Chain` only as the refusal. Every reader of `design.chains`
(`constraints`, `route_native`, `route_checks`, `route_verify`, `build`, `patterns/chain`) moves to
`design.paths`. Every message that said `Chain` (code v1-R8, language v1-11): the report line
(`constraints.py:2077`, and `GroupSpec`'s source at 1351), `ChainVerdict.where` (`route_verify.py:871,
1114`), `route_verify._chain_call` (1199) and the two gate messages that print it (1165, 1194),
`patterns/chain.py:237`'s `where`, `route_checks.py:616`'s docstring, the corridor message (656) and
the implicit-chain move (675); the 16 pinned `Chain line` strings in five test files (`test_pattern_chain`
5, `test_route_checks` 4, `test_route_verify_chains` 3, `test_constraints` 2, `test_cli` 2). The rename
in `examples/c3_usb/c3_usb.py`, `examples/node/node.py`, `tests/fixtures/ds2_addon.baseline.py:97`,
`tests/fixtures/pair/pair.py:29-30`, the other `Chain(` lines in `tests/` (44 by grep), `README.md`,
`docs/constraints.md`. `tests/conftest.py`: the DS2 helper of §1.3, which every DS2 reader (19 files by
grep) and PR 2's `test_links.py` use. New tests: a `Chain()` board is refused with the exact rewritten
lines, including c3_usb's multi-line import; the refusal text pinned against the owner's real file, read
only. Copper byte-identical.

**PR 2. Stations and links at the net's numbers.** `constraints.py`: `station_pads` and the multi-pad
refusals with the split move; joins for multi-pad end stations; `Link`, `PathSpec`, `Constraint.paths`,
`off_path`; `ConstraintSet.link`, `link_by_key`, `pad_width`, `widest`, `narrowest`; every path
reported in PR 1's format. `route_verify.py` (`_station_pads`, `chain_order`), `patterns/chain.py`
(`_members`), `route_checks.py` (`_refpin_pad`) call `station_pads`. `route_native.py`: `_pad_owner`,
`lay` and `route_layers` per link, joins before Kruskal, `Failed` carries its `NetCost`, `net_order`
by `widest`. `route_cost.py`: `link_cost`, `NetCost.link`, `group` and `keep_away` with
`compare=False`, `width_on`, and **all eight width reads** of §4.2 (`pitch_mm`, the bend term, `via_mm`,
`cell_free` 333, `_lane_ok` 338, `legal` 349, `pad_exits` 502, `_runs_to_pieces`) on `width_on(L)`.
`route_emit.py` (`Piece.link`), `route_scene.py` (`Item.link`). Tests: `tests/test_links.py`
(`link_cost == net_cost` on every link of the five boards), `tests/test_station.py`. Byte-identical.

**PR 3. Numeric path fields and every reader of a width.** `model.py`, `language.py`: `amps`,
`temp_rise_c`, `vias`, `layers`, `max_mm`. `constraints.py`: the lifted helper (the four blocks of §2,
clearance left per net) with a width per layer for links; the current-against-impedance refusal; the
one combine rule and its refusals; gating field by field through `_kwargs_of`; the every-pad refusal;
the off-path report with capacities per layer; the station balance refusal and the two notes (§3.3).
`_explain_type_error`: the net-only and not-a-path-field tables with the AST `NetReq` scan; every
refusal ends in `write:`. `copper.py`, `fab.py`: the fatal gate per piece by owner and layer, at the
link's temperature rise, with the second loop over nets carrying a link current; `_owned_copper` passes
owner and layer. `route_native.py`: role owners grouped from `Piece.link` (owner stays the net);
`_via_move` and `_source_of` name the `Path` line. `patterns/spine.py` (skip), `hop.py`, `tap.py`,
`stitch.py`, `fanout.py`, `route_channel.py`, `layout_check.py`, `dru.py`: the §4.4 rows (except the
fanout via row, PR 4). `docs/constraints.md`: a "Paths" section. Tests in `test_constraints.py`,
`test_dru.py`, `test_ampacity.py`. Five boards byte-identical. Fixtures: a 2 A link on `B.Cu` beside a
foreign track, where the space is clear at 0.781 mm but not at 1.09 mm, must not be laid at 1.09 mm
there (code v1-R2). Mutation: a scratch buck with the §5 trunk and a tap written `amps=0.001` builds past
the fatal gate; widen the gate's number back to the class width and the build must stop on the tap.

**PR 4. Via groups at layer changes.** `stackup.py`: `via_group(amps, drill, plating, dt, pitch,
width)`, the lumped flow-through model, with tests reproducing §4.3's table. `route_cost.py`:
`_via_legal` judges the group (barrels with `antipad_clash`, both landings with `blocked`), cost counts
the group and its landing. `route_native.py`: `_runs_to_pieces` lays the group and its landings with
reason `route_group`; `ViaTerminal` offered to a current link only when its group meets the count;
`RouteOut.stats["groups"]`. `patterns/fanout.py`: a station pad's escape as its link's group or none.
`patterns/stitch.py`: `antipad_pitch` lifted to a helper both read; the parallel carrier skips
`route_group` vias. `route_relax.py`: `route_group` outside `RELAXABLE`; `merge_overlaps` and
`prune_dangling` treat a group as one item. Applies to links whose current comes from a path.
Byte-identical. Fixtures (code v1-R3): a scratch 2-layer board whose 2 A path must cross a blocking
track; the test calls `Router.link` (`route_cost.py:592`) twice on the same scene, once with
`via_allowed=False`, and asserts the chosen route's cost is the smaller of the two and that, when it
changes layer, `stats["groups"]` shows 3 barrels at 1.3 mm whose worst share is at most 0.871 A; a
second board where the single-layer route is cheaper and no group is placed; a 2 A link starting on a
lone fanout via, which must not take it as a terminal. `write_problems` stays the failure path.

**PR 5. Sense paths, the current rule, path keep-away.** `model.py`, `language.py`: `kind`,
`keep_clear_of`, `keep_clear_mm` (code R3-11). `constraints.py`: path kinds and gating, the
backwards-sense refusals (a) and (b), the poured-net field refusal, the keep-away union, kind defaults
yielding to written values. `route_geom.py`: the hull clip. `route_scene.py`: `link_clashes`.
`route_cost.py`: `Slack.at` with the predicate at the net's clearance, the `Router` calling
`link_clashes`, `Slack.need` with the keep-away. `route_native.py`: the `net_links` filters and the
scoped `merge_overlaps`. `route_relax.py`: runs keyed by link, the probe calling `link_clashes`;
`patterns/hop.py`. `route_verify.py`: `path_junctions` (calling `_judge` with `floor` = the net's
clearance), `path_keep_away`; `route_checks.py`: `check_keep_away` path lines. Tests:
`tests/test_junctions.py`; `test_route_scene.py` (the audit unchanged on the five boards). Byte-identical.
Fixtures: VREF in both orders (one accepted, one refused by (b)); the shunt in both orders (one
accepted, one refused by (a), language v1-1); buck's backwards-compatible minimum and its reversed form
(refused by (a)); contact 0.15 mm outside the junction pad, which router and check must judge the same
way (code v1-R4); an off-path pad nearest the tap's far station; an off-path load trying to tee mid-way
onto a 2 A path; a path keep-away held by route and relaxer; a mutation moving the tap's junction to
`L1.2` in `layout.gen.py`, which `path_junctions` must name.

**PR 6. Placement reads links and the written attachment.** `route_checks.py`: `check_chains` (corridor
per link; the order, corridor and implicit-chain messages rewritten, §4.5), `check_corridors`,
`check_airwires`, `check_keep_away`, the decap note, the two new messages with the line to edit.
`pcb_place.py`: the decap pin from a `Place(to=)` that names a pin of a part with more than two pads on
the cap's power net. Depends on PR 5 (the path kinds' airwire presets). **Owner gate** (code v1-R13): the
`place_solve.facing_notes` row depends on Grok's uncommitted `place_solve` change, which the owner has
said not to commit; that row is built only if he lands it, and PR 6 ships without it otherwise. Copper
byte-identical; changed notes listed from the test diff.

**PR 7. `Loop()` as a check.** `model.py` (`LoopReq`), `language.py` (`Loop`), `__init__.py`;
`constraints.py`: `LoopSpec`, step classification, one pad per station, default budget, refusals;
`route_checks.py`: declared loops first, the guess skipping named parts, the lower-bound pass line, the
new hot-loop text; `pcb_place.py`: decap skips caps a loop names; `docs/constraints.md`: the budget
paragraph. Tests: `tests/test_loops.py` and the one pinned string. Depends on PR 6, which rewrote the
same decap code (code R3-11). Copper byte-identical.

**PR 8. The emitted board.** `ampacity.py`: the condition, path capacity between consecutive stations,
multi-pad stations from the anchor, off-path pad to its join, the sense-only all-pairs walk, each piece
at its layer's plane height. `route_verify.py`: `path_vias_layers`, the via-group check by owner and
landing with the worst-barrel share. `fab.py`: the fatal path checks. `dru.py`: per-link `fromTo` rules
only for what the spike shows KiCad catches. Mutations on a scratch buck with the §5 lines (narrow a
trunk segment; a `B.Cu` trunk segment at 0.781 mm; drop one barrel of a group; narrow one landing; add a
via to the tap) must each stop the build; the spike asks `kicad-cli pcb drc` about the same mutations and
records the result here. Depends on PRs 3, 4, 5.

**PR 9. The net's own links meet the same two criteria** (was optional; now version 1, language v1-3,
v1-4, physics v1-R4). `constraints.py`: width per layer for a `NetReq` with a current, one report line
per layer, the group per net. `route_native.py`, `route_cost.py`: via groups on the net's own links on
unpoured nets. `ampacity.py`, `fab.py`: the per-layer width and group checks for net links.
`dru.py`: a per-layer width rule (`(layer "B.Cu")`) for a class whose width differs by layer,
mutation-tested like PR 8. Not byte-identical: §8 lists the changes, and the PR measures them again. On
node, the group pitch is not capped at 1.0 mm (§4.3), so node's 1 A links can take a 2-via group where
a 0.8 mm ring could not; whether each fits is measured, and a link that cannot fit one stays on one
layer or fails with a route move, which the PR records. Depends on PRs 4 and 8.

**PR 10. Buck migrates.** `examples/buck/buck.py` (the §5 lines), `tests/test_examples_fab.py` (pinned
numbers), `README.md` (the construct table). Records, measured on the emitted board: the tap's width and
where it leaves the rail, the `SW` path's width, every layer change and its group (from
`stats["groups"]`), `VIN`'s widths and groups, the off-path rows, the loop area. Depends on PRs 3-9.

---

## 10. After version 1

Each item keeps the long draft's reasoning (its round-3 text is `scratchpad/netpath/
net-path-physics.r3.md`) and the round-3 findings that argued for cutting it.

**A. Return paths on a pour** (long draft §3.4): a `Path()` on a poured net names the pads a return
passes, and the tool derives its current and sizes those pads' tap vias. Against it: `GND` on buck is in
`todo`, because only vias are unioned to the plane (`_net_groups`, `route_native.py:719-723`;
`components`, `route_scene.py:1107-1117`) and the through-hole `J_OUT.2` stays its own component, so
`net_links` would route the return's station links pad to pad; measured, `Seg("GND", (34.079, 13.996),
(30.032, 13.996))` ends on `C_OUT1.2`'s pad, not its tap via (code R3-1). On a pour a tap via carries its
own pad's current, not the return's: `C_OUT1.2` carries no DC, `U1.GND` carries the low-side 1.17 A DC /
1.53 A rms (2 vias), and the other 0.83 A goes to `J_IN.2`, so the per-terminal current is right on a
pour, deferred rather than wrong (physics R3-1). Deriving the current from "the path whose last station
is on the part" fails for a source connector and gives two answers on any two-rail part (language R3-2);
a `Path("GND", ...)` would change meaning with the implicit pour rule (language R3-6); two returns at one
pad have no rule (language R3-12). Also deferred: the Kelvin return of a divider to the IC's ground (a
sense path on a poured net), plane cuts under a path, and sizing a poured net's tap vias by current
(PR 9 leaves them as they are).

**B. The full junction rule set** (long draft §3.2 rules 1 and 3 in general): field-less paths meeting
only at shared stations, a junction derived from shared stations with a refusal when both ends are
shared, an off-path branch ending only on copper at least as wide as itself. Against it: clause (a)
applied to consecutive links of one path and would have changed the DS2 Addon's `VDDA` (code R3-2); the
derived junction refused the long draft's own shunt example (physics R3-2, language R3-4); rules 1 and 3
disagreed on an off-path tee onto a loop hop (physics R3-8, language R3-5). Version 1 keeps §3.2's two
rules and the two sense-direction refusals.

**C. Routed `Loop()` hops** (long draft §6): on an unpoured net a link; on an adjacent plane the tap
vias; elsewhere outer copper laid by its own pass. Against it: the stated reason for a separate pass was
wrong, only the pre-union at `route_native.py:1003-1004` forces it (code R3-1), and its place relative to
the FINAL stage was undecided (code R3-16). A hop carries current, the input-cap rms (0.99 A on buck, at
most Iout/2 in general), so it must be held to its net's amps capacity, and a zoned net is not measured
today (physics R3-4). A hop through a pour must add the vertical leg at the plane's `dielectric_between`
height (1.53 mm on buck), which today's guessed decap loop also omits (physics R3-10). A hop between
non-consecutive stations of a path runs in parallel with it (physics R3-13), and a hop on a path's link
must be that link (language R3-4). Also deferred: the emitted-board loop area, and the output-side hot
loop that closes through the pour (physics v1-R8).

**D. Impedance, routed length and a via budget on a path.** For `z_se_ohm`: a width per layer, refused on
a layer with no reference. The reference-change note already exists per net as
`route_verify.return_vias` (`route_verify.py:1315`, run in `build.py:160-177`), and a path's layer change
would be judged by it unchanged (physics R3-11).

**E. Pair skew along paths, and stubs.** Neither KiCad's total-length skew nor "along the chains, stubs
excluded" is the skew of a USB-C orientation: row A sees D+'s stub, row B sees D−'s; measure per entry
contact (A6 against A7, B6 against B7) or the stub asymmetry beside the chain skew (physics R3-3). The
USBLC6's second I/O pad closes a loop through the part and is not a stub (physics R3-12).

**F. Flow-through and multi-pad intermediate stations.** Entry and exit pads need a link between them at
the path's numbers (physics R3-6); version 1 gets that by splitting the path so both halves end at the
station (§1.2). The pair router still needs an entry and an exit pad per station (the ESD flow-through).

**G. Superposing declared path currents** on the emitted copper (long draft §3.3). Rule 2 removes the
tee it was mostly for; §3.3's balance refusal covers declared numbers at a station. A cycle in the copper
(code R3-15) applies if it returns.

**H. Via-group refinements.** Relax `antipad_clash` under a landing that already cuts the pour (buck's
group could then sit at 0.95 mm instead of 1.3 mm); a two-dimensional landing for currents where the
line's end barrels crowd (above 3 A on jlcpcb_2l_1oz, §4.3); a field solve in place of the lumped model;
a router neck into a pad that cannot take the link's width (§4.4).

**Recorded, not a defect** (physics v1-R10): the width block computes inner layers with IPC-2152 and that
layer's own plane modifier, floored by the IPC-2221 **external** curve. IPC-2152 replaced the 2221
internal chart, which was derived as half the external data and is known to be over-conservative; near
a plane an inner trace runs no hotter than an outer one. The 2221 external floor is a pcbc choice, and
IPC-2152 with the layer's plane modifier is the inner number. Do not switch to the 2221 internal curve:
it would make 2 A need 2.033 mm, 2.6× the external 0.781 mm.

---

## Round-3 majors: where each went

| finding | where |
|---|---|
| code R3-1, a return path is routed | §7: a field on a poured net is refused, field-less behaviour stated as measured; §10 A |
| code R3-2, clause (a) on consecutive links | §3.2: no clause (a); the predicate rejects only with a sense or `amps=` path |
| code R3-3, `merge_overlaps` scope | §3.2: skips only pairs the predicate makes foreign |
| code R3-4, owners only when numbers differ | §4.2: keyed by link on every net whose paths carry a field or kind |
| code R3-5, off-path cannot reach the junction pad | §3.2: contact inside an allowed pad is legal for both sides |
| physics R3-1, return-via current | §10 A |
| physics R3-2, shunt refused | §3.2: no hops, junction is the first station; example shown |
| physics R3-3, pair skew | §10 E |
| physics R3-4, a hop carries current | §10 C |
| physics R3-5, off-path tee splits a current link | §3.2 rule 2; §3.3 balance refusal and note |
| physics R3-6, multi-pad intermediate station | §1.2: refused with the split; §10 F |
| physics R3-7, width for one layer only | §2, §4.2, §4.6 for links; PR 9 for nets |
| language R3-1, sense order | §3.2, §7: first station, refusals (a) and (b), fixtures |
| language R3-2, return derivation | §10 A |
| language R3-3, off-path check written two ways | §4.6: one definition |
| language R3-4, shunt refused | as physics R3-2 |

Minors in scope: code R3-6 to R3-14 and R3-16 (hop row) in §3.2, §4, §6 and the PR plan; language R3-7
to R3-11 and R3-13 in §2, §4.5, §5, §7; physics R3-9 in §5. Minors on items now out of version 1 are
filed under their §10 item.

## Review v1: where each finding went

| finding | sev. | where |
|---|---|---|
| code v1-R1, DS2 turns the suite red after PR 1 | blocker | §1.3, §8, §9 PR 1: conftest helper applies the refusal's exact rewrite to a temp copy; refusal pinned against the real file |
| code v1-R2, four more router width reads | major | §4.2, PR 2 (all eight reads); PR 3 fixture |
| code v1-R3, `write_problems` writes only failures | major | §4.3, PR 4: `Router.link` twice in the test; `stats["groups"]` |
| code v1-R4, two dilations | major | §3.2, PR 5: the net's clearance for both; 0.15 mm fixture |
| code v1-R5, the helper is four blocks | minor | §2 |
| code v1-R6, `temp_rise_c` on `switch_node` | minor | §2 gating field by field; §7 refusal |
| code v1-R7, `pathlib` and star-import lists | minor | §1.3 |
| code v1-R8, pinned `Chain line` strings | minor | §9 PR 1 |
| code v1-R9, the gate needs owner and layer | minor | §4.4, §4.6, PR 3 |
| code v1-R10, link key as owner parses as a ref | minor | §4.2: role from `Piece.link`, owner stays the net |
| code v1-R11, groups by distance; pitch; window | minor | §4.3: landing-joined membership, own reason, pitch with one grid step |
| code v1-R12, line numbers in §6/§7 | minor | §7: every example labelled with its file; §5 line map |
| code v1-R13, PR 6 decap targets; uncommitted dep | minor | §4.5, §8, PR 6 owner gate |
| code v1-R14, parallel carrier question | minor | §0, §9: measured this round |
| physics v1-R1, SW at 0.3 mm carries 2 A | major | §0, §5 `Path("SW", ...)`, §3.3 series note, §4.4 neck |
| physics v1-R2, anchor-plus-twins shares unevenly | major | §4.3: flow-through line, landing, worst-barrel count (measured) |
| physics v1-R3, a link changes layer through a via it starts on | major | §4.3 by current; §4.4 fanout row; §4.6 |
| physics v1-R4, VIN left broken on the migrated example | major | §0 table; PR 9 made version 1 and moved before PR 10; §5 |
| physics v1-R5, 1.0 mm reach is the walk's constant | minor | §4.3: pitch from the copper, membership by landing |
| physics v1-R6, rungs per layer | minor | §4.3: no rungs; landing at the wider width |
| physics v1-R7, off-path line prints capacity | minor | §3.1 |
| physics v1-R8, second Loop not a hot loop | minor | §5: dropped; §6 budget paragraph; §10 C |
| physics v1-R9, Loop pass is a lower bound | minor | §6 pass line |
| physics v1-R10, inner-layer curve note | minor | §10 "Recorded, not a defect" |
| language v1-1, reversed shunt passes every check | blocker | §3.2 refusal (a); §4.6 sense-only walk; PR 5 fixtures |
| language v1-2, multi-pad end station | major | §1.2 join; §4.1; §5.1 worked example |
| language v1-3, two spellings of a current | major | §2 (what `Path(amps=)` adds); PR 9 in version 1; repeated-value note deleted |
| language v1-4, VIN on the migrated buck | major | as physics v1-R4 |
| language v1-5, order failure prints a pose | major | §4.5 rewritten order and implicit-chain messages; PR 6 |
| language v1-6, current mismatch only a note | major | §3.3 balance refusal |
| language v1-7, refusals in prose | minor | §7: `write:` throughout; split move first |
| language v1-8, refusal chains | minor | §7: final form once; `HTR` instead of blinky's `LED` |
| language v1-9, every-pad FB kind | minor | §7: the author chooses |
| language v1-10, combine rules | minor | §2: one rule; kind defaults yield |
| language v1-11, backward-compatibility statements | minor | §1.3, §8, §9 |
| language v1-12, `direction.md` §5b and rule 2 | minor | `docs/direction.md` §5b: one bullet added |
| language v1-13, placement warnings name no edit | minor | §4.5 formats |

---

## Revision Summary (v1)

The fourth review round (code, physics, language on version 1) raised 37 findings: 2 blockers, 12
majors, 23 minors. All 37 are addressed in this revision; none is marked wontfix. What changed:

- **Via groups were redesigned** (physics v1-R2, R3, R5, R6; code v1-R11, R14). A scratch build of buck
  showed the stitch ring can never place a twin there (antipad pitch 1.3 mm against a 1.0 mm reach), and
  a lumped DC model (`scratchpad/netpath/v1r/ladder.py`) showed anchor-plus-twins and same-end layouts
  overload the first barrel. The group is now a flow-through line with a landing on both layers at the
  wider layer width, pitched by the copper's own rules, counted by its worst barrel, joined by its own
  copper, and required at every layer change a link's current crosses.
- **PR 9 (the net's own links) is now version 1 and comes before the buck migration** (physics v1-R4,
  language v1-3, v1-4): the two spellings of a current differ only in the three things §2 lists, and
  the migrated buck meets answer 2 on `VIN`. The PRs were renumbered: old PR 10 is PR 9, old PR 9 is PR 10.
- **The buck migration gained `Path("SW", "U1.SW", "L1.1", amps=2)` and lost its second `Loop()`**
  (physics v1-R1, R8); §0's defect list now separates the declaration from the physics.
- **Sense direction**: a general refusal (a far station on a two-pad part bridging to a current net)
  catches the reversed shunt and the reversed buck tap; a sense-only net keeps an all-pairs walk
  (language v1-1).
- **Language**: one combine rule (loosen defaults, never written values); gating field by field; a
  multi-pad end station compiles to a join; a balance refusal at shared stations; every unique refusal
  ends in `write:`; the `check_chains` pose is gone; placement warnings name the line to edit; a USB-C
  worked example (§5.1).
- **Buildability**: the DS2 Addon is read through a test helper so PR 1 does not turn the suite red; the
  router's eight width reads, one dilation for router and check, owner and layer through the gate, link
  keys kept out of `Piece.owner`, a testable PR 4 fixture, corrected citations and inventories.
- **`docs/direction.md` §5b** gained one bullet recording rule 2 and that a net's own links meet the
  same two criteria (language v1-12).

Not done in this revision: no code was written; the full suite was not changed. The via-sharing numbers
are a lumped DC model, not a field solve or a measurement on copper; the node and DS2 numbers in §8 were
not re-measured; the pad distances in the `Loop line 271` message are a format.

---

## Build record (2026-09-30, working tree, uncommitted)

Version 1 as built, PR by PR, with what departs from the text above. Numbers carry their command;
scratch outputs are under the session scratchpad `netpath/` (`final/`, `PROGRESS.md`).

| PR | landed | departs from the text |
|---|---|---|
| 1 | `language.Path` (all eight fields parsed), `Loop`, `chain_rewrite`, `Chain` = the refusal; `import pathlib`; `model.PathReq`/`LoopReq`, `Design.paths`/`loops`; every reader and message renamed; c3_usb, node and the two fixtures rewritten **by `chain_rewrite` itself**; `tests/conftest.ds2_dir` (temp copy, rewrite applied); `tests/test_path.py` | the one-argument clash hint is prefixed `line N:` by the loader like every constructor's message |
| 2 | `constraints.Link`/`PathSpec`/`LoopSpec`/`ViaGroup`; `Constraint.paths`/`off_path`/`widths`/`via_group`; `ConstraintSet.link`/`link_by_key`/`links_of`/`pad_width`/`widest`/`narrowest`; `paths.station_pads` and the join of a multi-pad end station; `route_cost.link_cost` and every width read on `width_on(L)`; per-link costs in `route_nets`; `Piece.link`, `Item.link` | `chain_order`, `patterns/chain` and `route_checks` keep their own resolvers (no board has a multi-pad station; `station_pads` is what the compile and the router read) |
| 3 | `paths.py`: the combine rule, field-by-field gating, every §7 refusal (net-only and not-a-path-field keywords in `_explain_type_error`), the off-path report with capacities per layer, the balance refusal, the series note, the impedance refusal; `copper.power_ampacity_failures` per owner and layer with the second loop; `fab._owned_copper` 5-tuples; spine skip; `dru` per-net `width_net_<net>`; `layout_check` on `narrowest`; `route_channel` on `widest` | the §3.3 "off-path pads may end on the stations" note is not printed (the off-path lines carry it); `hop.py` and `tap.py` read the net width unchanged (a two-pad net's path takes no field; taps are on poured nets, which take none) |
| 4 | `stackup.via_group_shares`/`via_group` (reproduces §4.3's table exactly); the group as one search move (`route_cost.GroupPlan`: stretched to land on the lattice, count re-asked at the real pitch); `_runs_to_pieces` lays barrels and both landings as `route_group`; `stats["groups"]`; stitch parallel carrier skips group vias; `merge_overlaps`/`prune_dangling` treat a group as one item; fanout leaves station pads of current and sense paths bare | a fanout escape is never laid *as* a group: the pad gets none and the router leaves it directly |
| 5 | `route_scene.JunctionRule` (both rules, contact inside a pad dilated by the net's clearance), `junction_rule`, `link_clashes`; `route_geom.clip_outside`; `Slack.at` prices a foreign same-net item at the clearance; `Router.legal`/`_via_legal` ask the rule; Kruskal filters (far stations, same-path stations, a path's own vias) and **a Kruskal link may not join two components holding stations of one path** (found on buck: a failed SW station link was bypassed through C_BOOT.2 at 0.3 mm); relaxer runs keyed by link with the rule in its probe; path keep-away in the rule and in `Slack.need`; `route_verify.path_junctions` | `@<pad>` link stamping of fanout/tap stubs is not built (station pads get no escape; taps are on poured nets) |
| 6 | `check_chains` (order message with the line, no pose; corridor per link width; implicit-chain move with no order), `check_corridors` per link then off-path MST, path airwire, path keep-away; decap distance from a `Place(to=)` IC pin | the `place_solve.facing_notes` row (owner gate: Grok's uncommitted change, not touched) |
| 7 | `paths.compile_loops` (refusals: one pad per station, a step on one net or one part, one attachment one statement, `loop_mm2=` beside a declared loop); `check_loops` declared loops first (lower-bound pass line), the guess skipping named parts, the hot-loop move rewritten; decap skips loop caps | — |
| 8 | `route_verify.path_copper_failures` (vias, layers, every group's worst barrel by landing) fatal in `fab`; `ampacity` per-layer plane height, group barrels rated by the model, path rows `NET (Path line N)`, path-mode pairs; `via_parallelism` reads groups by landing | `dru` per-link `fromTo` rules not written: the spike below shows KiCad does not see per-link widths |
| 9 | width per layer for a net with a current; the net's own via group on unpoured nets; `dru` per-layer class rule (`A.Layer == 'B.Cu'`); lone vias are not Kruskal terminals on a grouped net | — |
| 10 | `examples/buck/buck.py` lines 55-66 exactly as §5 | **SW does not route on the stock placement** (below) |

**The switch path.** `Path("SW", "U1.SW", "L1.1", amps=2)` asks 0.781 mm on F.Cu with no vias. As placed,
`Place("C_IN1", to="U1.VIN")` (line 72) puts C_IN1's pads 0.455 mm under U1's bottom row with 0.9 mm between
them (1.181 mm needed), and U1.1's GND tap stub (0.781 mm on a 0.532 mm pad) reaches 0.124 mm past that
pad toward SW. `pcbc pcb` now says so before any copper exists (`SW path U1.SW -> L1.1 (Path line 60): no
0.78 mm channel ... C_IN1.1 and U1.VIN pinch it to 0.46 mm; move C_IN1 (Place line 72)`), and the build
leaves SW open with the route move and its `vias=True, layers=` alternative. Tried on scratch copies: that
alternative (a 4-via group on the 0.6/0.3 switch-node via, 1.1 mm pitch) finds no room either; C_IN1
`toward="right"`/`"up"`, C_IN1 attached to U1.GND, C_IN2 to the right: SW stays open in each. The 0.3 mm
copper the stock board shipped carried the 2 A at about twice its rating. The pipeline tests that used buck
as a complete board read it as shipped (`tests/fixtures/buck_netreq`, `conftest.vehicle_examples`).

**What the migrated buck's emitted copper carries** (routed board of `pcbc build` on a temp copy;
`roles_doc`):

| link | §5 says | emitted |
|---|---|---|
| `L1.2 -> C_OUT1.1` (line 62) | 0.781 F.Cu, 1.09 B.Cu, 3-via group at a change | 0.781 F.Cu, 2.16 mm, no layer change, 0 vias |
| `C_OUT1.1 -> J_OUT.1` (line 62) | as above | 0.781 F.Cu, 11.33 mm, 0 vias |
| `C_OUT1.1 -> R_FB_TOP.1` (line 64) | 0.2 F.Cu, no vias, leaves the rail inside C_OUT1.1 | 0.2 F.Cu, 11.99 mm, 0 vias, ends on C_OUT1.1's centre; `path_junctions` empty |
| `C_OUT2.1` off-path | 0.4, joins only at a station pad | 0.4 F.Cu, 2.25 mm, ends on C_OUT1.1 |
| `U1.SW -> L1.1` (line 60) | 0.781 F.Cu, no vias | **not routed** (above) |
| `VIN` own links | 1.09 B.Cu, 3-via groups | 0.781 F.Cu / 1.09 B.Cu, 25.88 mm; two 3-via flow-through groups (6 barrels, landings 1.09 both layers, 11.18 mm); no lone via |
| `loop` line 66 | 3.3 mm² of 20 | `pads enclose at least 3.3 mm2 of 20` |

`route_verify.path_copper_failures` and `path_junctions` are empty on it.

**Mutations (PR 8)**, on the migrated buck with its SW path line left out (which completes through fab):
narrow a trunk segment to 0.5 mm; move a trunk segment to B.Cu at 0.781 mm; a B.Cu VIN track at 0.781 mm;
narrow a landing to 0.6 and to 0.9 mm; drop one barrel of a group; add a via to the tap — **7 of 7 stop
pcbc's gates** (`tests/test_path_gates.py` held five of them, not six as first written; it holds all seven since refutation round 1, below). `kicad-cli pcb drc` sees **2 of 7**: the B.Cu VIN
track (`width_power_b_cu`) and the 0.6 mm landing (the class rule). A link narrower than its own width is
invisible to KiCad (`width_net_5v` is the tap's 0.2 mm), as is a dropped barrel and a via on a no-via link
of a net that allows vias; those are pcbc's measurements and are never called verified.

**PR 9 on the board as shipped** (`tests/fixtures/buck_netreq`): VIN's three lone 0.4 mm vias (0.871 A
each) became two 3-via groups; its worst path 0.871 -> 1.999 A; the stitch carrier stops looking for rungs.
Paid for with VIN's routed length 32.489 -> 53.765 mm (detour 1.53 -> 2.53), `track_angle` 4 -> 8, one more
pcbc via under a courtyard (2 -> 3), GND pour 903.64 -> 914.46 mm². blinky, c3_usb, node and the DS2 Addon
route byte-identical to before the work (node's LOAD/VBUS groups are compiled; no change of layer happens).

**Suite**, run last with nothing else building (`PCBC_REQUIRE_KICAD=1 .venv/bin/python -m pytest -q -p no:cacheprovider`):
**2 failed, 1112 passed in 2557.38 s**. Both failures were failing before the work (baseline on a pristine
copy of the tree: 3 failed, 1055 passed): `test_bridge::test_ds2_routed_board_has_two_grounds_and_nothing_joining_them`
and `test_route_geom::test_kicad_measures_the_same_air_around_a_real_pad_as_the_shape_model_does`; the third
baseline failure (`test_route_checks::test_ds2_chain_passes_as_placed...`) passes now that the DS2 Addon's own
line 97 is not declared twice.

**The owner's edit.** `~/Documents/MaD/Hardware/DS2Addon/pcbc/ds2_addon.py` was not touched. Until line 15
imports `Path` in place of `Chain` and line 97 reads `Path("VDDA", "J2.1", "C4.1", "U1.12")`, `pcbc build`
on it is refused with exactly those two lines; the suite reads it through `tests/conftest.ds2_dir`.

### Refutation round 1: what was fixed (2026-10-01, working tree, uncommitted)

Eleven defects from three refuters (physics, language, tests). Each is reproduced, fixed and pinned by a
test; numbers below carry their command. Scratch outputs: `netpath/fix/` in the session scratchpad.

| # | defect | fix | pinned by |
|---|---|---|---|
| 1 | buck's `GND` declares 2 A, reaches the pour through single 0.5/0.3 mm vias (0.707 A, U1's GND pin among them), and the fab row said `ok` | Claim 9 (the net's own via groups) is for **unpoured** nets; a poured net's vias are not sized in version 1 (§10 A, now also in `docs/direction.md` §5b). `ampacity._pour_joints`: a zoned row whose vias rate under its `amps=` says `not rated: pour` with the count and the weakest via, in `FAB_NOTES.md` and among the build's power moves (so `--strict-power` stops on it). On the routed buck: `GND: ... 6 of its 6 vias rate under 2 A (the weakest 0.707 A at 14.6329,11.676 on B.Cu/F.Cu) ... [not rated: pour]` | `test_ampacity::test_a_poured_net_that_declares_a_current_says_its_pour_vias_are_not_rated` |
| 2 | a Loop step refusal overwrote a station with one already named (`C_IN.1` twice), and the degenerate loop that followed passed at 0.0 mm² | the two stations are swapped when the pad a step needs is already a station (§7's own example); a pad named twice and a loop crossing fewer than two parts are refused; the pinned string corrected to §7 | `test_paths_compile` (`loop_step_crosses_nets*`, `loop_names_a_pad_twice`, `loop_that_crosses_no_part`) |
| 3 | the whole-net rewrite dropped `volts=` and the NetReq's other nets | `paths.netreq_call` renders the NetReq line with every field it wrote; the other nets keep a line of their own | `every_pad_amps_keeps_the_netreq_fields_and_its_other_nets` |
| 4 | the series note asked for `Path("GND", ..., amps=)` on a poured net, which §7 refuses | on a poured net the note says the current goes into the pour and version 1 does not size it | `test_the_series_note_does_not_ask_for_a_path_on_a_poured_net` |
| 5 | a Kelvin sense path on a poured net was refused as a "return path" and offered a field-less line that is not Kelvin | its own refusal: version 1 does not build a sense path on a poured net, the field-less line is not Kelvin either, drop it; listed out of version 1 | `sense_on_a_poured_net_is_not_offered_as_kelvin` |
| 6 | a station no track can leave was found only by the router, whose move said "no free cell" and named nothing | `route_checks.check_station_escapes` (pcbc pcb, 16 directions at the link width against `ClearanceTable.between`); `route_native._escape_blockers`/`_held_by` name the items holding the cells, the rule and its NetReq line, and say when the part carries both nets. Root cause on the refuters' 3.3 V buck: `NetReq("FB", keep_clear_of="SW")` holds 3 mm around U1's own FB pin, 1.30 mm from its SW pin | `tests/test_path_placement.py` (escape at placement; router on `tests/fixtures/buck33`; keep-away line on a reached blocker) |
| 7 | the path keep-away judged the straight chord, at negative distances no `keep_clear_mm` could clear; a raised `keep_clear_mm` gave no source | a way around the other net's pads (octagons circumscribing the rounded keep-away) inside the board and the airwire budget; a station inside the keep-away said as such; "lower keep_clear_mm" only with a value that opens a way; the raise is printed with its source | `tests/test_path_placement.py` |
| 8 | the lone-via gate could be disabled with every test green | | `test_path_gates::test_a_lone_via_where_a_group_belongs_stops_the_build` |
| 9 | the net's own per-layer width was unpinned; the docstring claimed KiCad was asked | | `test_a_b_cu_vin_segment_at_its_f_cu_width_stops_the_build` (pcbc gate and KiCad `width_power_b_cu`); docstring corrected. Since `docs/physics-subjects.md` PR 3 (no net current, no per-layer class rule): `test_path_gates.py::test_a_narrowed_b_cu_vin_segment_stops_the_build_and_kicad_cannot_see_it` |
| 10 | the off-path default width was enforced by the router only | `copper.power_ampacity_failures` third loop: copper no link owns on a net with fielded paths, against `Constraint.width_on(layer)` | `test_a_narrowed_off_path_branch_stops_the_build_and_kicad_cannot_see_it` (since physics-subjects PR 3 the off-path width is the class floor, which KiCad's class rule also holds: `..._and_kicad_sees_it`); `test_a_net_row_walks_each_off_path_pad_to_the_stations` |
| 11 | `Loop(mm2=)` could be ignored with every test green | | `test_loops::test_a_written_budget_is_the_one_judged` |

The mutation table of PR 8 now has all seven in `tests/test_path_gates.py` (the 0.6 mm landing with
KiCad's class rule; the B.Cu VIN track with `width_power_b_cu`), plus the lone via and the off-path branch.

Measured after the fixes. Five boards rebuilt from temp copies (DS2's `Chain` line rewritten in the copy
only): the routed `layout.gen.py`, `.kicad_pcb`, `.kicad_dru` and `.kicad_pro` of all five are
byte-identical to the builds before the fixes, and buck built twice from two directories is identical, so
the fixes change messages, checks and gates, not copper. Thirteen mutations of the fixed code
(`netpath/fix/mutrun/run.py`) are each killed by the test written for it. Full suite, run last with
nothing else building (`PCBC_REQUIRE_KICAD=1 .venv/bin/python -m pytest -q -p no:cacheprovider`):
**2 failed, 1133 passed in 2383.02 s**, the same two failures as before the work.

### Refutation round 2 and the owner's rule of 2026-10-01 (working tree, uncommitted)

Owner, 2026-10-01: *"Remove suggestions in tool, just throw errors if unable to complete."*
(`docs/direction.md` §7b). Every user-facing string of `src/pcbc` that proposed an edit — a rewritten line
to paste, a `Place()`/`NetReq()`/`Path()` to write, a value to raise or lower, a "try", a "did you mean" —
now states the facts and stops (§7 above). `moves.move_line` lost its fifth sentence (the 13 call sites
no longer pass one); `ReturnRule.move`, `ChainVerdict.move` and the bridge rows carry no edit; dead
helpers that only built edits are deleted (`paths.path_call`/`netreq_call`, `constraints._plane_move`,
`route_checks._place_line`/`_even_move`/`_keepout_escape`, `route_verify._chain_call`, the patterns'
`_moves`). User-facing vocabulary: `pcbc sch`/`pcbc pcb` print `N warnings` (was "N to fix by editing
...", "nothing to move"); the build's unrouted list is `result["errors"]`, with `result["moves"]` kept as
the same list for readers of the older key; `result["power"]`/`power_moves` keep their names and carry
warnings. `tests/test_refutation_r2.py::test_no_message_of_the_tool_proposes_an_edit` scans every string
literal of `src/pcbc` for the shapes those edits took.

| # | defect (refuter) | fix | pinned by (killed mutant) |
|---|---|---|---|
| blocker | path gates skipped copper no `pcbc:` group owns (a `layout.core.py` line): width, vias, layers, Kelvin junction and off-path width went unchecked, and the build said ok | `route_verify.core_link_items`: each such piece is given the links it carries (it lies on a simple path between the link's station pads in the net's touching copper, found on the block-cut tree; two pieces meeting inside a pad they both touch meet through it) and judged by every gate (`gate_roles`, used by `fab`); core vias are grouped by the core tracks that run over two or more of them within `count x pitch`; `path_junctions` reads them, and the junction rules are **fatal** in `build.py` (`junction_lines`) | `test_the_path_gates_judge_copper_no_role_group_owns` (KiCad: the routed fixture with its groups stripped passes every gate; a narrowed link, a narrowed off-path branch, a dropped barrel and a mid-rail tap are each named; the tap as a `layout.core.py` line stops the build) (`A1_core_items_off`, `A1_junction_gate_off`) |
| major | F.Cu sized for a B.Cu pour the board does not have (1.999 A on the gate, 1.665 A by the repo's model) | `constraints._reference_for` reads the pours the board will have (`cs.planes`: on two layers the back GND pour only when a power NetReq names GND), computed before any width; `paths` and `ampacity._planes_of` read the same; a controlled impedance with no reference plane is refused | `test_f_cu_is_sized_for_the_pour_the_board_has` (`A2_plane_assumed`); `test_return_rules` no-pour case |
| major | a 3 A link with a straight via-free route left unrouted, the error blaming placement | `route_geom.contact_outside`: a same-net contact whose witness is inside an allowed pad counts only when the copper wholly outside the pad touches (a wide track's round end reaches back past a small pad); used by the router's junction rule, `path_junctions` and `chain_order`; `Slack` prices foreign same-net copper by its part wholly outside the pads. fxC (3 A, no pour, 2.276 mm) and the same with the pour (1.367 mm) now complete | `test_a_3a_link_with_a_straight_via_free_route_is_routed`, `test_two_wide_tracks_meeting_inside_a_pad_do_not_touch_outside_it` (`A3_witness_off`, `A3_slack_full_copper`) |
| major | a multi-pad end station shared by two paths: two joins, one 2 A wide labelled 0.3 A | the join is built once (first path in file order) at the station's throughput, max(sum entering, sum leaving), with its own current (node probe: J1 2.3 A, 0.948 mm, 4-via group) | `test_a_shared_multi_pad_station_is_joined_once_at_its_throughput` (`A4_join_single_max`) |
| major (tests) | `layers=` enforced by nothing tested (router and gate) | — | `test_a_sense_link_on_a_layer_it_does_not_allow_is_named` (`B1_layer_gate_off`, `B1_via_gate_off`), `test_a_link_s_layers_bound_the_router` (`B1_router_layers_off`) |
| major (tests) | via-group checks on a link and the both-layers landing check untested | — | `test_a_path_links_via_groups_are_checked` (`B2_group_gate_off`, `B2_landing_gate_off`) |
| major (messages) | a sense path backwards to a connector or power pin accepted | `paths._carries_current`: a far station that is a connector's pad (`J`/`P`) or a `power_in`/`power_out` pin is refused | `test_a_sense_path_ending_at_a_connector_is_refused` (`C1_connector_sense_off`) |
| major (messages) | an off-path branch walled in by its own net's path copper: "nothing near them holds a cell" | `route_native._corridor_blockers` (`Slack.nearest`) names the items holding the cells between the two ends, own-net path copper with its link and the junction rule (`_held_by` words it) | `test_a_branch_walled_in_by_its_own_nets_path_copper_names_it` (`C2_corridor_blockers_off`) |
| major (messages) | a wrong kind on a Path: "kind=analog is the net's" of a power net | the net's real kind and line | `test_a_net_kind_on_a_path_is_refused_with_the_nets_real_kind` |
| major (messages) | station-escape warning named the part whose own pads block it | the pad in the way, its rule, "U1 carries both SW and FB", the Place lines; no mover | `test_a_station_whose_own_part_blocks_it_names_that_pad_and_no_mover` |
| major (messages) | Loop/Path-order warnings named the anchor ("no Place() attaches U1") | `route_checks._attached` names the line that placed each part (to=, edge=, CSS) and both parts' lines | `test_a_loop_over_budget_names_the_place_lines_that_put_its_parts_there` |
| major (messages) | every pad with the net's own kind: two choices, each refused | one statement (§7) | `test_a_path_on_every_pad_with_the_nets_own_kind_is_one_true_error` |

Found while re-running the language refuters' variants and fixed the same way (each was accepted
silently): a Path naming a station twice; `keep_clear_mm=` with no `keep_clear_of=` (Path and NetReq);
`keep_clear_of=` matching no net (Path and NetReq); `keep_clear_of=` naming the path's own net. Pinned in
`test_paths_compile.py`'s refusal table, which now asserts each whole message.

A router change tried and taken out: routing a net a second time with its failed links first, kept only
when fewer pads stayed apart. Measured on buck (SW), c3_usb (VBUS) and node (VBUS) it was reverted every
time, and fxC completes without it once `Slack` reads copper wholly outside the pads; untested and
unproven, it is not in the tree.

**Why the migrated buck does not complete** (`pcbc pcb` and `route_native` on a temp copy; the Place()
lines were not edited here). `Path("SW", "U1.SW", "L1.1", amps=2)` asks 0.781 mm on F.Cu, no vias (the
switch_node preset). U1.2 (SW) is the middle pad of U1's bottom row. Down, left and right, C_IN1
(`Place("C_IN1", to="U1.VIN")`, line 72) sits 0.455 mm under the row with 0.9 mm between its pads, where a
0.781 mm track needs 1.181 mm; the only other exit is up into the 1.228 mm between U1's two pin rows,
where a 0.781 mm track has 0.047 mm to spare (finer than the router's lattice step,
`track_min + clearance_min`), and that channel's ends are blocked by C_IN2 on the right and lead around
C_IN1 into the VIN spine on the left. So the error's "In the way: VIN track (spine)" is the router's
most-hit blocker and true, but the cause is the placement C_IN1's relation produced. Making this board
complete is either a placement change (the relation placer keeping a station's escape for its link's
width, which `check_station_escapes` already measures) or an `layout.core.py` lock of the SW copper by the
author; it belongs to the separate placement investigation and was not done here.

Five boards from temp copies (DS2's `Chain` line rewritten in the copy only), `netpath/build_board.sh new
<board>` and `netpath/metrics.py`: blinky ok; buck SW unrouted, 13/14 router links, KiCad 2 unconnected;
c3_usb VBUS 29/31, 6; node VBUS 33/35, 6; ds2 REFN_F 38/39, 3; DRC errors are the unconnected items only.
c3_usb, node, ds2 and blinky route byte-identical to the round-1 builds; buck's 5V differs (two fewer
segments: its C_OUT1.1 links leave the pad directly now that a junction inside the pad is not a contact
outside it), and its `path_junctions` stay empty.

### The owner's decisions 1-4 and buck's currents (2026-10-01, working tree, uncommitted)

Owner, 2026-10-01: *"ok do 1-4"* (`docs/direction.md` §5b), and buck declares the currents of TI's Table 4-1 roles.

1. **Pad neck** (`constraints.PAD_NECK_MAX_MM = 1.0`). Where a path meets a pad narrower than its width, its copper
   may run at no more than the pad's own width across the exit for at most 1.0 mm past the pad's edge, and must end
   in full-width copper of that path, a via of it, or a pour of the net. *Router*: `route_cost.Router.link` searches
   at full width first; only when that fails and a terminal pad is narrower than the link does it search again with
   neck stubs (`_neck_ends`: the pad's width, 0.1 mm steps to the limit, then full width to a lattice cell);
   `legal()` and `route_native._runs_to_pieces` read the neck's width per segment. *Emitted board*:
   `route_verify.pad_necks` accepts a narrower piece of a link only when one end is in a station pad of the link,
   it is no wider than that pad across the segment, its centreline runs at most the limit past the pad's edge, and
   its far end lies in full-width copper of the link, a via of the link or the net's pour; every other narrower
   piece fails `copper.power_ampacity_failures` with the fact that refused it (and `PATTERN_NECK_OK` no longer
   exempts link copper, a guard excepted). *Fab*: `ampacity.power_bottlenecks(necks=)` rates an accepted neck like
   its pad and leaves it out of `under_mm`. *Report*: the route step's `necks` / `neck_refusals` and
   `FAB_NOTES.md` "Pad necks". Fixture `tests/fixtures/d14/neck.py`: a 3 A path (2.276 mm, no pour) from an 0402
   pad 0.48 mm from its partner routes through a 0.64 mm neck 0.4 mm past R1.1's edge and builds to fab.
2. **Switch-node vias**: `PRESETS["switch_node"]` allows vias on every copper layer; a switch path's layer change
   is a via group sized for its current (buck's SW: 4 x 0.3 mm, 1.1 mm pitch). The copper area is priced as before
   (length at its own width, `via_mm` per change) and measured on the emitted board (`route_verify.switch_copper`,
   per layer, pads excluded; the route step's `switch_copper`). A switch node is still the sensitive net it was:
   `route_native.constrained_nets` keeps every `autoroute=False` preset's net (switch_node, analog) first in the net
   order and out of the relaxer, so the pre-migration buck the pipeline tests drive (`tests/fixtures/buck_netreq`)
   routes byte-identical to the checkpoint (its `.kicad_dru` loses only `novia_sw`).
3. **2 oz stackups**: `jlcpcb_2l_2oz` (core 1.46 = 1.6 - 2 x 0.07), `jlcpcb_4l_2oz` (JLC04162H-7628),
   `jlcpcb_4l_2oz_3313` (JLC04162H-3313), with JLC's 2 oz limits; sources in `docs/constraints.md` "Stackups".
   Every current width names its stackup (`stackup.cu_text`): 2 A is 0.781 mm at `1 oz jlcpcb_2l_1oz` and 0.391 mm
   at `2 oz jlcpcb_2l_2oz` over the pour.
4. **Exit corridor**: `pcb_place.pad_corridors` (the path's width, or the neck's for 1 mm at a narrower pad, plus two
   clearances, out along each exit `pad_exits` allows, to the content edge or the footprint's own other-net copper
   ahead); `_attach` treats a candidate whose pads enter a corridor of another path-end pad of the target as a clash;
   `corridor_errors` stops placement when declared parts stand in every exit of such a pad. Buck: the down corridor of
   U1.2 is x 15.994-16.926 for the first millimetre and x 15.8695-17.0505 beyond; C_IN1 moves from (16.46, 11.676)
   turned 180 to (17.91, 12.396) turned 270.
5. **Buck's currents** (`examples/buck/buck.py` line 55 cites them): `NetReq("VIN", kind="power", volts=12,
   amps=1.36)` (rms at D = 0.463, ~90 % efficient) and `NetReq("GND", kind="power", amps=1.53)` (rms at the ideal
   D = 5/12); SW and the 5V load path stay at 2 A. Every buck line number after 54 moved by 2 (Path 60 -> 62, 62 -> 64,
   64 -> 66, Loop 66 -> 68).

Found by the suite and fixed: `route_native._escape_blockers` asked `_ends` on every layer, so an F.Cu pad whose
link may use B.Cu was never "stuck" (decision 2 exposed it); a fielded net's failure whose legs were refused by other
items now also names its own path copper holding the cells (`_corridor_blockers`).

Boards from temp copies (`d14/bld.sh`; DS2's `Chain` rewritten in the copy only), KiCad 10 judging:

| board | ok | router links | KiCad links | unrouted | DRC errors |
|---|---|---|---|---|---|
| blinky | yes | 0/0 | 0/0 | — | none |
| buck | no | 12/13 | 11/13 | SW | unconnected 2 |
| c3_usb | no | 29/31 | 25/31 | VBUS | unconnected 6 |
| node | no | 33/35 | 29/35 | VBUS | unconnected 6 |
| ds2 | no | 38/39 | 36/39 | REFN_F | unconnected 3 |

blinky routes byte-identical to the checkpoint build (`layout.gen.py`, `.kicad_pcb`, `.kicad_dru`, `.kicad_pro`).
c3_usb, node and ds2 carry Paths and decision 4 moves parts on all three; built with the corridor rule switched off
(`pad_corridors` returning nothing, the rest of this round's source as is) all three are byte-identical to the
checkpoint, so every difference on them is decision 4's placement. On ds2 it costs a Path-order warning: C5 (to
U1.DVDD, placed before C4) keeps U1.12's exit clear and takes C4's old spot, so C4.1 projects 1.4 mm past U1.12 along
VDDA's feed.

Buck. With decisions 1-4 and the old shared 2 A NetReq, SW routes: a 0.532 mm neck from U1.2 for 0.9 mm past its edge
into 0.781 mm copper to L1.1, no via on the path (the switch node's two vias are the 0.3 mm C_BOOT branch on B.Cu);
EN is then left open by C_BOOT.2 and the SW and VIN tracks. With decision 5's currents VIN is 0.459 mm, its spine
runs under U1.2 from (15.081, 13.2546) to (17.41, 10.9256), and SW does not route: no layer change has room for its
4-via group (3.3 mm landing at 1.09 mm) and no single-layer route exists. The 1.0 mm limit was not raised. The 2 oz
variant (SW 0.391 mm, narrower than U1.2) stops at `check`: "L1 (SW) vs C_IN1 (GND) pad gap 0.000 mm < 0.16 mm",
which is `copper.py`'s pad box turning a pad by the footprint angle twice (a probe with the pad angle alone passes
`check` and then stops on a pattern's own U-turn self-check at (17.41, 9.96)); neither is fixed here.

Tests: `tests/test_d14_decisions.py` (14) and `test_stackup.py::test_the_2oz_stackups_are_jlcs_numbers_and_every_current_width_names_its_stackup`;
each new rule fails under the mutation that disables it (22 mutants, all killed). Ledgers that decision 4's placement
moved on c3_usb, node and ds2, and that decision 5 moved on buck, are re-recorded with a dated sentence; tests whose
subject is a historical geometry (the comb on ds2, the KRT `VDDA` splice, c3_usb's elbow, the refuters' walled-in
branch, the path-gate vehicle) declare the moved parts where the checkpoint placed them. Full suite
(`PCBC_REQUIRE_KICAD=1`): 2 failed, 1170 passed in 2860.44 s, the two failures the ones that failed before this round.

### Refuter round on decisions 1-5 (2026-10-02)

Sixteen defects from four refuters; each was reproduced, fixed and pinned (`tests/test_d14_decisions.py`, 2026-10-02
sections), and every rule here fails under the mutation that disables it (`d14/r2/mut`).

- **Neck only at a narrower pad.** `route_verify.pad_necks` accepted a narrow piece at a pad wider than its path (a
  0.6 mm piece at R2.1, 1.75 mm across, on a 1.09 mm path, passed fab). It now refuses it: "R2.1 is 1.75 mm across the
  segment, not narrower than the link's 1.09 mm on F.Cu". The length limit is pinned on both sides (1.0 mm accepted,
  1.01 and 1.05 mm refused); each of the four ways a neck may end — full-width copper of its link, a via of the link,
  the net's pour, on the neck's layer — is pinned against a case that breaks only that clause; a link's `fanout` or
  `tap` piece narrower than the link fails the gate (the exemption it had is gone), a `guard` is still not judged.
- **Pad angle.** `copper.pad_clearance_failures` turned a pad by the footprint angle plus the board file's pad angle,
  which already holds it; a 90-degree part's pads were turned 180. The 2 oz buck copy's `L1 (SW) vs C_IN1 (GND) pad
  gap 0.000 mm` was this (true gap 0.545 mm). Rebuilt, the copy then met two pcbc bugs, both fixed: the spine pattern
  drew C_IN1.1's rib through U1.3 over U1.3's own rib (a 180-degree self-check failure at (17.41, 8.81);
  `spine._uncontained` leaves out a straight rib lying wholly on another, on its layer and no wider), and
  `prune_dangling` removed the VIN trunk whose ends overhang its outermost ribs, and the ribs after it, leaving VIN
  open (`route_native._trim_overhang` cuts a track back to the outermost points it joins at when it joins two or more
  items that touch one another no other way). The 2 oz copy now routes VIN and SW (SW: 7.0 mm2 of switch-node
  copper, 2 vias) and stops on 5V and FB, which the router reports as unrouted.
- **Exit corridor** (decision 4). It ran to the board's content edge, so declared copper 5.7-7.7 mm from the pad
  closed an exit; it carried no layer, so bottom-side parts closed a top-side pad's exits; it ran past the path's own
  far station; and an exit that ended at the part's own pin within the neck counted as open (buck's U1.2 "up" exit
  stops at U1.5 1.028 mm past the edge). Now (`pcb_place.Corridor`): a corridor runs `reach` = the point where the
  path can first turn (the neck's end, or the stub past the part's own other-net neighbours, whichever is farther)
  plus half the path's width and its clearance — pcbc's length, the owner's text gives none; it is on the pad's own
  copper layers; it ends at a station pad of the same path; an exit stopped short by the part's own other-net copper
  or the board edge is `dead`, and a side whose stub meets the part's own pad is `closed`; both count as closed in
  the declared-placement error, which now names every side, each blocker's distance past the pad's edge, and the
  corridor's shape. Buck's U1.2 down corridor runs to 1.5905 mm past the edge; C_IN1 still sits at (17.91, 12.396).
  With the corridor bounded, c3_usb, node and ds2 place and route byte-identical to the checkpoint again (the
  2026-10-01 moves and ds2's C4 order warning are gone, and their re-recorded ledgers are back at the checkpoint's).
- **Errors, not suggestions** (§7b). `build_job`'s check stage now runs `pcb_place.validate`: `Place(to=)` naming no
  part or no pin is a check failure with its line ("Place line 75: the to= of Place('C_IN2') names U9.VIN, and there
  is no part U9 on the board"), not a pad-gap error or a raw `ValueError`. The CSS refusals (`z-index`, `flex`,
  `grid`, `gap`, `position:sticky/fixed`, `display`, `padding`), a Region whose size nothing determines, `SchPlace`'s
  `gap=`, a second `Place()` and fab's via-in-pad error state facts and what pcbc reads instead of an edit; a load
  refusal carries its board.py line in `build_job` too, and a Region refusal is a check failure instead of a
  traceback. `tests/test_refutation_r2.py`'s scanner also flags a sentence starting `Use`/`Put`/`Set`/`Pass`/`Keep`.
- **Fab's fiducials.** With fewer than three free corner spots fab put all three at fixed corners anyway, on routed
  copper, and failed on `kicad-cli DRC 26 copper error(s)`. A spot whose 2 mm mask opening takes in a courtyard, a
  track, a via or a pour is refused and listed in `FAB_NOTES.md`; a DRC copper failure lists every violation by type
  with its items, positions and which are fiducials fab inserted (`fab.drc_error_lines`).
