# Placement in layout.core.py, and an optional outline

Design, 2026-10-03, revision 2 (the review responses are summarised at the end). Governed by
`docs/direction.md` §1, §2, §6 and §7b; when this file and `direction.md` disagree, `direction.md` wins.
In revision 2, `direction.md` §1's table and §2's outline bullet were brought into line with the
owner's 2026-10-03 decision: the outline is `Outline()` in `layout.core.py`, and `Board()` takes no size.
Every code reference is to a frozen copy of the tree, taken on 2026-10-03 while another workflow
(silk/3D) was still editing it. Every number comes with the command that produced it (appendix).

The owner, 2026-10-03:

- *"why is node.py selecting orientation and placement? shouldnt that be in the core and gen.place files?
  why do we put it in the main file?"* The only reason was the 2026-09-23 default, chosen so the boards
  would not need migrating. That default is reversed (`direction.md` §2), with *"ensure we remove the
  functionality so the tool doesnt support board.py layout changes"*.
- *"for #2 lets make board outline optional and we can make it based on the design, if we need an
  outline we can specify it"*. Before that, he asked that placement and routing *"optimize for pcba size"*.

Part A moves every geometric decision out of `board.py`. Part B makes the outline optional. A lands
first, because B changes the meaning of the anchors that A moves.

---

## Summary of the decisions

| Question | Decision |
|---|---|
| Where `Place`, `Region`, `Keepout` and a declared outline live | `layout.core.py`. `pcbc` no longer exports them. A call in `board.py`, by any import path, is refused, naming the line and the file it belongs in. |
| What `board.py` keeps | Parts, nets, `NetReq`, `Path`, `Pair`, `Bus`, `Loop`, `Isolation`, `Guard`, `Bridge`, `Thermal`, and `Board(layers=, stackup=, planes=, pcb=, net_order=)`. That is the circuit and its physics. |
| The declared outline | `Outline(width=, height=, at=(0, 0))` in `layout.core.py`. `at=` is the top-left corner in the frame. There is no `padding=` and no `radius=` in v1. `Board(width=…)` in `board.py` is refused. |
| Anchors | `at=`, `left=`, `top=` are frame coordinates in both modes. `right`/`bottom`/`auto`/`%` measure from a declared outline and are refused without one. |
| Loading | `load_board(path)` runs `board.py`, then the `layout.core.py` beside it, under a separate trace. The schematic stage loads `board.py` alone. |
| Gen | Gen writes one `Place` line per footprint and fiducial, and the `Edge.Cuts` `Rect`. A part's gen `Place` line can replace its core `Place` line as a lock. |
| No `Outline()` (derived mode) | Each edge part and its `to=` tree is shaped alone and docked against the interior. A side with an edge part, or a side the keepouts reach, is fixed. The free sides are fitted after routing. |
| Compactness in v1 | Fit plus docking. Groups are not packed, because that would move declared anchors, which `direction.md` §6 forbids. |
| Where it is written | The `Edge.Cuts` `Rect` in `layout.gen.py`; the route stage's stamp; one report line giving the size, the corner and what set each side. |
| Shape | A rectangle. Rounded corners, cutouts, mounting holes and enclosure fits can only be declared. |

---

## Part A: placement moves from board.py to layout.core.py

### A.1 What moves

| Construct | Today (frozen copy) | After |
|---|---|---|
| `Place(ref, …)`: anchors, CSS, `to=`, `edge=`, `rot=`, `overhang=`, `reason=` | `board.py` (`language.Place`). In core, `_core_place` refuses it. | `layout.core.py`. Refused in `board.py`. |
| `Region(name, …)` | `board.py`. In core, `_board_only` refuses it. | `layout.core.py`. |
| `Keepout(name, …)`, e.g. node's and c3_usb's `ANTENNA` | `board.py` | `layout.core.py`. Every `Keepout()` is a placement keep-out (placement reads them, `pcb_place.py:652`), so `direction.md` §2's "placement keep-outs" means all of them. Isolation is `Isolation()`, not a keepout. |
| The outline: `Board(width, height)` / `size_mm` | `board.py` | `Outline(width=, height=, at=)` in `layout.core.py`, only when the author declares it. |
| `Board(padding=)` | `board.py`; 0 uses in `src`, `tests` and `examples` (command 4) | Removed. `content_rect`'s padding stays for the schematic sheet only (`sch_place.py:12`, `SHEET`). |
| `Board(layers=, stackup=, planes=, pcb=, net_order=)` | `board.py` | Stays. The stackup, the copper count and which net is a plane on which layer are physics. |
| `SchPlace`, `SchRegion`, `SchStyle` | `board.py` | **Out of scope; they stay.** The same argument applies to schematic geometry, but it has its own output (`.kicad_sch`) and its own placer (`sch_place.py`). A schematic core file is a separate decision for the owner. |

**Isolation.** `Isolation(a, b, volts=, slot=, across=)` stays in `board.py`, because the voltage
between two areas is physics. Which side a part is on stays where it is today: it is the Region of
the part's `Place(parent=)`, or inherited through `to=` (`constraints._part_sides`, `constraints.py:2351`).
That `Place` is now in core. The split is accepted: the barrier is physics, and where a part stands is
geometry. Nothing is lost silently. A part with no side is already refused
(`constraints.py:2403`), and the refusal now names the file:
`Isolation HV/LV (ds2_addon.py:NN): R7 is on neither side: no Place(parent=) in layout.core.py puts it
in HV or LV, and it is not in across=`. An Isolation naming a Region that core does not declare:
`ds2_addon.py:NN: Isolation("HV", "LV") names Region "HV", and layout.core.py declares no Region "HV"`
(or `…and there is no layout.core.py next to ds2_addon.py`). §6's solved placement would have to keep
a part with no `Place` on a side; that is recorded for v2.

### A.2 How layout.core.py is loaded

- The core file is `layout_job.core_path(board)`: `layout.core.py` beside `board.py`. This is unchanged.
- `language.load_board(path, core=True)` runs `board.py` under `tracing("load")` (its reads go to
  `design.reads`, as today). It then runs `layout.core.py` through `load_layout(…, source="core")`
  under its own trace, whose reads go to a new `design.core_reads`.
- **Staleness.** The schematic stamp records `design.reads` only. A core edit therefore leaves the
  schematic and ERC current, which is right: `SchPlace` stays in `board.py`. The place stamp records
  the core file's sha and `design.core_reads`, so a core edit is stale from place. Today it is stale
  from route (`stamp["core"]`, `build.py:924`). The route stamp inherits it.
- **Who loads core.** The sch stage and `check_board(pcb=False)` (the schematic loop) call
  `load_board(path, core=False)`. A core error never blocks `pcbc sch`. With `core=False`,
  `compile_constraints` does not compile `Isolation` (its Regions are core's), and no reader of the
  outline is reached. PR 1 has a test that runs every `check_board(pcb=False)` and sch test with no
  `layout.core.py` present. `check_board(pcb=True)`, the place stage and everything after it load core.
- `layout_job.load_core` no longer executes the file. It validates the core copper and drawings that the
  load collected (KiCad round trip, hard minimums, clearances, foreign uuids), as today, because that
  validation needs the placement. Its second caller, the `route_native` replay
  (`route_native.py:1786-1799`), changes the same way.
- **Line numbers.** `_line()` (`language.py:93`) matches only `_board_path`, so a constructor called
  from core records line 0. It now matches the file being executed, and every spec records `source`
  (`"board"` or `"core"`) next to `line`. `RegionSpec` and `KeepoutSpec` gain both fields.
  The "Place line N" messages in `pcb_place.py` (lines 243, 712, 750 and 998) become
  `layout.core.py:N`. That rewrites 34 assertions in 7 test files (command 9).
- **The namespace.** The core namespace gains `Place` (the full `language.Place`, see A.4), `Region`,
  `Keepout` and `Outline`. `_BOARD_FIELDS`, the snapshot that refuses circuit changes made from a layout
  file, loses `places`, `keepouts` and `regions`. `board` stays in it, because `Board()` is still
  `board.py`'s. `Outline()` writes a new `design.outline` (an `OutlineSpec` with `width`, `height`,
  `at`, `line` and `source`), not `BoardSpec`. So the `repr(design.board)` check
  (`language.py:2975-2982`) is untouched.
- **PR 1's bridge to PR 2.** In PR 1 every reader of the size still reads `BoardSpec.size_mm` or
  `job.board_size_mm`. After core has run, `load_board` sets `size_mm` from `design.outline`. That one
  assignment is the only code that writes `size_mm` for a PCB, and PR 2 deletes it when the readers
  move to `job.outline`.
- **No outline before PR 3.** When core is loaded and declares no `Outline()`, PR 1 and PR 2 refuse:
  `node.py: layout.core.py (next to node.py) declares no Outline(), and this pcbc builds a declared
  outline only`. If the file is missing, the same fact names the missing file. PR 3 replaces the
  refusal with derived mode.

### A.3 What board.py may still hold, and the refusals

**The vocabulary.** `Place`, `Region`, `Keepout` and `Outline` leave `pcbc`'s exports (`__all__`).
`pcbc.__getattr__` (PEP 562) raises for those four names, so `from pcbc import Place` fails on the
import line with a `ValueError` that `check_board` reports at that line:
`ds2_addon.py:15: pcbc exports no Place: Place is layout.core.py's (next to ds2_addon.py), and
ds2_addon.py holds the circuit and its physics only`. A core file does not import them, because its
namespace provides them. `from pcbc import *` in core (`tests/fixtures/r2/layout.core.py`) imports
none of them, so it overrides nothing.

**Any other path.** `import pcbc.language as L; L.Place(…)` from `board.py` reaches `language.Place`.
That function now records a refusal (line, ref) and nothing else whenever `board.py` is executing. The
code that put a `board.py` `Place` into `design.places` is deleted, not bypassed. After `board.py`
has run, `load_board` raises with every recorded refusal. Following §7b, each refusal is a fact naming
the line, the construct and the file it belongs in, and proposes no rewritten line:

| Construct in `board.py` | Refusal (form) |
|---|---|
| `Place(…)` | `ds2_addon.py:136: Place("J2") is placement; placement is layout.core.py's (next to ds2_addon.py), and ds2_addon.py holds the circuit and its physics only` plus `(and 27 more: lines 137-141, 144-165)` |
| `Region(…)` | `board.py:NN: Region("HV") is geometry; Regions are layout.core.py's (next to board.py)` |
| `Keepout(…)` | `node.py:66: Keepout("ANTENNA") is geometry; keepouts are layout.core.py's (next to node.py)` |
| `Board(width=, height=)` / `size_mm=` / `padding=` | `ds2_addon.py:94: Board(width=47, height=25.4): the outline is geometry; a declared outline is Outline() in layout.core.py (next to ds2_addon.py); Board() takes layers, stackup, planes, pcb and net_order` |

In `layout.core.py`:

| Construct | Behaviour |
|---|---|
| `Board()`, `NetReq`, a part, a net, … | Refused as today (`_board_only`), with the hint at `language.py:2901` cut: `…: NetReq() is board.py's, not layout.core.py's`. |
| Two `Place()` for one ref | Refused, naming both lines. |
| Two `Outline()` | Refused, naming both lines. |
| `Outline()` with no `width` or no `height` | `layout.core.py:1: Outline() has no height`. |
| `Place()` for a ref that `board.py` does not have | Refused at load, naming the line and the ref. Today this is the place stage's `missing refs` error. |
| `Place("FID1", …)` (a gen fiducial line) | `layout.core.py:N: Place("FID1"): FID1 is a fiducial the build places, and board.py has no part FID1`. |
| `Place(…, side=)` / `Place(…, rotate=)` | `side= is now layer= ("F.Cu" or "B.Cu")` / `rotate= is now rot=`. See A.4. |
| A drawing on `Edge.Cuts` | Refused as today, in `load_core` (`layout_job.py:352-356`). The message becomes `…: Rect on Edge.Cuts; the outline is Outline() in layout.core.py, or derived when there is none`. The `Board(w, h)` hint on the copper-layer refusal (`layout_job.py:341`) is cut to the fact. |
| `Pour(None, keepout=…, name=N)` where `N` is a `Keepout()` name | `layout.core.py:12: Pour(None, keepout=…) "ANTENNA" is the rule area gen draws from Keepout("ANTENNA") at layout.core.py:3`. See A.5. |

Stale texts that change in PR 1 are:

- `_core_place` and its docstring, which are deleted;
- the `load_layout` docstring and its `_BOARD_FIELDS` message ("that line belongs in board.py",
  `language.py:2983`), which now names `board.py` only for the fields that are still its own;
- the `_board_only` hint (`language.py:2901`);
- the two `layout_job` messages above;
- `gen.HEADER` (A.5).

### A.4 Place in core, and the gen form

The §7 loop copies a gen line into `layout.core.py`. Gen writes a pose as
`Place("C1", at=(36.8, 9.85), rot=180.0, layer="F.Cu", locked=True)`. Three fields change on
`language.Place` so that the gen form is also the core form:

- **`layer="F.Cu"|"B.Cu"`** replaces `side="F"`. `side=` is refused with the fact
  `side= is now layer= ("F.Cu" or "B.Cu")`, following the `Chain` → `Path` precedent. The frozen tree
  has no `Place(…, side=…)` call (command 4). Internally, `PlaceSpec` keeps its `side` field ("F"/"B"),
  so the compiled fixtures do not change.
- **`rot=`** is the one name for rotation. Today `rot=` and `rotate=` are aliases
  (`language.py:255-256, 295`). Gen writes `rot=`, so `rotate=` is refused with `rotate= is now rot=`.
  There are 45 `Place(…, rotate=…)` calls to migrate, all on lines that are moving to core anyway
  (command 11).
- **`locked=`** is KiCad's footprint `(locked yes)` flag. The emitter writes it as given
  (`layout_emit.py:547-552`), and the default stays `True`. It does **not** decide whether a pose is
  fixed: every `Place` in core fixes its part's placement, and its `locked=` only sets the flag.
  A pasted `locked=False` line therefore fixes the pose with the flag off, and gen writes the same
  line back.

A core `Place` keeps everything it means today: CSS anchors, `to=`, `toward=`, `gap=`, `edge=`,
`overhang=`, `reason=`, and a Region `parent`. Placement resolution (`pcb_place.resolve_places`)
does not change in Part A. It reads `job.places`, which now come from core.

### A.5 What gen writes

Gen already writes every resolved pose. `place_native.poses_of_places` produces one `Pose` per
footprint, fiducials included, and `gen.gen_source` writes them as `Place` lines (31 for DS2, command 5).
It also writes the `Edge.Cuts` `Rect`, and one `Pour(None, keepout=…)` rule area per `Keepout`
(`place_native.py:165-173`). Part A changes `gen.HEADER` (`gen.py:778-788`):

- **Locking a part's pose is "replace", not "copy".** After Part A every placed part already has a core
  `Place`, so a pasted pose replaces that part's `Place` line. A second `Place` for the ref is refused,
  naming both lines (A.3). The header says so.
- The lines that cannot be locked are: a fiducial `Place` (`FIDn`, the build's own); a `Group` named
  `"pcbc:…"`; the `Edge.Cuts` outline (it is `Outline()`, or derived); and a `Keepout`'s rule area
  (it is `Keepout()`).
- **The keep-out fact has one home.** It is `Keepout()`, which placement reads, and from which gen draws
  the rule area. A core `Pour(None, keepout=…)` stays what it is today: a KiCad rule area that the router
  and KiCad DRC obey and placement does not read. If one keeps footprints out
  (`footprints="not_allowed"`) and a part stands in it, KiCad's DRC fails the build, as it does today.
  The only overlap, a pasted gen rule area for a `Keepout`, is refused by name (A.3). Both count toward a
  derived outline in the same way (B.1).
- In declared mode the `Rect` is still gen's: it is derived from `Outline()`, the same way plane pours
  are derived from `planes=`. Core never holds an `Edge.Cuts` drawing.

### A.6 Migration

**Gate for Part A: no behaviour change.**

- Two fresh builds are made in scratch copies of the tree: PR 1's base and PR 1's head. The checked-in
  gens date from 2026-09-27, and buck's predates four current poses, so they are not the baseline.
- For each of the four examples and DS2, `layout.gen.py` must be byte-identical between the two
  builds, apart from the header comment, and `tests/fixtures/compiled/*.json` must be unchanged.
- This holds for two reasons:
  - `Outline(w, h)` resolves to the same `Rect` that `Board(w, h)` did, and every anchor keeps its
    numbers.
  - `compile.py:to_dict` drops `source` and `line` from `keepouts` and `regions`, as it already does
    from `places` (`compile.py:69-71`).
- The full suite runs for every PR, not only before PR 3. It has two known failures: `test_bridge`
  (ds2, two grounds) and `test_route_geom` (kicad, air).

Each example's `Place`, `Keepout` and size move unchanged into `examples/<b>/layout.core.py`, with
`rotate=` written as `rot=`. `Board(…)` keeps `layers`, `stackup` and `planes`:

| Board | Lines that move to `layout.core.py` | `Board()` left in board.py |
|---|---|---|
| blinky | lines 29–50 (`Place("R1", …)` and `Place("D1", …)`, multi-line), plus `Outline(width=40, height=25)` | `Board(layers=2, stackup="jlcpcb_2l_1oz")` |
| buck | line 37 (`U1`) and lines 72–82 (11 `Place`), 12 `Place` in all, plus `Outline(width=40, height=25)` | `Board(layers=2, stackup="jlcpcb_2l_1oz")` |
| c3_usb | lines 212–231 (19 `Place`, 1 `Keepout`), plus `Outline(width=40, height=30)` | `Board(layers=2, stackup="jlcpcb_2l_1oz")` |
| node | lines 64–95 (31 `Place`, 1 `Keepout`), plus `Outline(width=60, height=45)` | `Board(layers=4, stackup="jlcpcb_4l_1oz", planes=[("GND", "In1.Cu"), ("3V3", "In2.Cu")])` |

**Tests (command 9).**

| What | Count | Where | How it migrates |
|---|---|---|---|
| Board texts written by tests | PCB `Place(` calls in 28 test files | `tests/*.py` (215 lines) | PR 1 adds `write_board(dir, board=…, core=…)`, and each fixture's `Place`, `Keepout` and `Region` lines move into the `core=` text. |
| On-disk fixture boards | 12 boards, 106 `Place` lines, 12 `Board(width=…)` | `tests/fixtures/` | Each gets a `layout.core.py` beside it. Two cases need care: `buck_netreq`, the pipeline vehicle, and `r2/fxa.py`, which already has a core file, so its `Place` lines are appended to it. |
| `Board(width/size_mm/(w, h))` in test texts | 57 lines in 19 files | `tests/*.py`, e.g. `ROBUST_HEAD` (`test_constraints.py:603`) | The size becomes an `Outline()` in the `core=` text. |
| Sites that copy an example's `.py` and string-edit its `Place` args | about 21 | e.g. `test_layout_roundtrip.py:135/247/1046/1228/1270`, `test_copper_rules.py:159` | They copy the example's `layout.core.py` too, and edit that file. |
| Tests that pin the reversed default | 5 | `test_layout_roundtrip.py:139, 590, 1041, 1205, 1239` | Rewritten to pin the new refusals. |
| "Place line N" assertions | 34 in 7 files | — | Rewritten to `layout.core.py:N` (A.2). |
| New mutation tests | — | — | One per refusal in A.3, one per import path (`from pcbc import Place`, `L.Place`), and one `core=False` load per sch entry point. |

**The owner's DS2 board (`~/Documents/MaD/Hardware/DS2Addon/pcbc/ds2_addon.py`).** The tool cannot edit
that file. The owner's file and the migrated copy differ only on lines 15, 95 and 97: the
`Chain`/`NetReq(amps=)` migration, a separate change (command 6). The lines below have the same numbers
in both. The file ends at line 165.

The owner changes `ds2_addon.py` as follows:

- line 15: `Place` is removed from the import. This is now **required**, because `pcbc` no longer
  exports `Place` (A.3).
- line 93: the comment `# Rev 1's outline: …` goes to the core file.
- line 94: `Board(width=47, height=25.4, layers=2, stackup="jlcpcb_2l_1oz")` becomes
  `Board(layers=2, stackup="jlcpcb_2l_1oz")`.
- lines 135–165 are removed. These are the comment `# Copper: headers on their rev 1 edges; …`,
  the six anchor `Place` lines (136–141), the two comment lines (142–143) and the 22 relation
  `Place` lines (144–165).

The owner creates `~/Documents/MaD/Hardware/DS2Addon/pcbc/layout.core.py`:

```python
# Rev 1's outline: 47 x 25.4 mm, the headers on the edges they were on (the EdgeBoard mates to it).
Outline(width=47, height=25.4)

# Copper: headers on their rev 1 edges; the ADC between the gauge side and the EdgeBoard side.
Place("J2", edge="left", reason="gauge supply and bridge")
Place("J5", edge="right", reason="bridge, second pair")
Place("J4", edge="top", right=8, reason="reference, off to the side so the ADC's top pin row has room")
Place("J1", edge="bottom", left=12, reason="EdgeBoard")
Place("J3", edge="bottom", right=8, reason="GPIO")
Place("U1", position="absolute", left=20, top=10, reason="ADC in the middle")
# Only the decoupling and the pull-up crowd the ADC: a TSSOP's 0.65 mm pad rows need their
# escapes. The series resistors go at the MCU header, each filter at the connector it serves.
Place("C5", to="U1.DVDD")
Place("C4", to="U1.AVDD")
Place("C6", to="U1.DVDD")
Place("R10", to="U1.~{RESET}")
Place("R3", to="J1.3")
Place("R4", to="J1.4")
Place("R5", to="J1.5")
Place("R6", to="J2.3")
Place("R7", to="J2.4")
Place("C7", to="R6.2")
Place("C8", to="R6.2")
Place("C9", to="R7.2")
Place("R8", to="J5.1")
Place("R9", to="J5.2")
Place("C10", to="R8.2")
Place("C11", to="R8.2")
Place("C12", to="R9.2")
Place("R1", to="J4.1")
Place("R2", to="J4.2")
Place("C1", to="R1.2")
Place("C2", to="R1.2")
Place("C3", to="R2.2")
```

**The suite's DS2 copy.** `conftest.ds2_dir` copies the owner's real file and applies
`chain_rewrite` and `DS2_MIGRATIONS` (`conftest.py:136-176`). Until the owner makes the change above,
every DS2 reader would be refused at load. The DS2 readers are:

- `test_constraints`: the fixture compare and `test_ds2_addon_lines_are_pinned`;
- `test_guard`;
- `test_layout_roundtrip:215`;
- `test_cli`;
- `test_return_rules`;
- `test_route_verify_chains`;
- `test_bridge`.

PR 1 therefore adds a Part-A step to `ds2_dir`, applied only while the copy still has the old lines
(the `DS2_MIGRATIONS` precedent):

- write the `layout.core.py` above into the temp copy;
- in the board text, rewrite line 15's import without `Place`, and rewrite line 94 in place;
- blank lines 93 and 135–165, so every line keeps its number. That matters for
  `test_ds2_addon_lines_are_pinned`, which pins lines up to 134.

One new test pins the refusal against the real file, as was done for `Chain`.

DS2 **keeps a declared outline**. Its own comment records 47 × 25.4 as rev 1's outline, the one the
EdgeBoard mates with. That is an enclosure fit, and enclosure fits stay declared (B.6). Dropping it is
the owner's decision. If he drops it, `J4 … right=8` and `J3 … right=8` are refused as B.5-1, because
with no outline `right=` has nothing to measure from.

---

## Part B: the outline is optional

With no `Outline()` in `layout.core.py` the board is in **derived mode**. Everything below applies only to
derived mode. A declared outline behaves as Part A leaves it, with one generalisation:
`Outline(at=)` may move the declared rectangle's corner, and anchors stay in frame coordinates (B.1).

### B.1 What anchors mean with no outline

**The frame.** The coordinate system is the one core is written in: KiCad's origin, x to the right and
y down. It is the same frame as gen and the emitted board, so a gen line copied into core means the
same thing. **The derived outline is not translated to (0, 0).** Moving it would shift every
coordinate, and every core copper lock would stop matching gen. KiCad and the fab files accept any
origin, and JLC's CPL accepts any offset.

| Construct | Declared outline | Derived mode |
|---|---|---|
| `at=(x, y)` | frame coordinates | Same. |
| `left=` / `top=` (`position="absolute"`) | frame coordinates. With the default `Outline(at=(0, 0))` that is today's meaning exactly, because no board uses padding. | Same frame coordinates. Measured: node's `U1` resolves to (12.25, 7.85) and buck's `U1` to (16.46, 8.81), the same as today (`review-geometry.md`, "What holds"). |
| an axis with no `left`/`top`/`at` (`css._resolve_axis` reads an unset margin as `auto` and centres) | centred in the declared outline | **Refused (B.5-1)**, for parts, Regions and keepouts. |
| `right=` / `bottom=`, `margin: auto` centring, `%` lengths, against the board | from the declared outline's edges | **Refused (B.5-1).** They measure from an edge that is itself derived from the placement. |
| any of the above with a `parent` Region | the Region's box | Unchanged. A Region has its own box. |
| `Region(…)` | resolved in the frame | Must be given `left`/`top`/`width`/`height`. It **counts toward the outline** with margin 0, like a courtyard, so the fit never cuts an Isolation region. |
| `Keepout(box=…)` or CSS `left`/`top`/`width`/`height` | frame coordinates | The same. It **counts toward the outline** with margin 0, as does a core `Pour(None, keepout=…)` rule area. A side that the keepouts reach at least as far as every courtyard is a **fixed side at the keepout's face** (B.2 step 6). |
| `position="absolute"` | taken out of flow | Same. A static (in-flow) `Place` with no position is a part with no position (B.5-2). |
| `edge="left"…` | flush with that declared edge, centred on it unless told | Docked with its `to=` tree (B.2 step 5). Along the edge it is centred on the anchors' span, or placed by a frame `left`/`top`. An along-edge `right`/`bottom` is B.5-1. |
| `to=` | unchanged | Unchanged search. The search area is given in B.2. |

**What a `left`/`top` number means.** In derived mode, `left=1` is a coordinate, not a distance from an
edge. It stays a distance from the edge only where that edge is fixed at 0. On node and c3_usb the
ANTENNA keepout fixes the left and top sides at 0 (B.2 step 6), so `U1 left=1 top=1` is still 1 mm
from both edges. That 1 mm is the point for an antenna module.

**A part with no `Place()`.** Today it sits on the seed grid (`place_native.seed_feet`, `Board(width) // 8`
columns, with a note). In derived mode that is **error B.5-2**. A grid part would be the outermost
part, so the "derived" outline would be the grid's size, a number nobody chose. This makes derived mode
stricter than declared mode; that is accepted. When §6's solved placement lands, those parts are
placed by the generator and count like any other part. B.7 gives a complete derived-mode core file.

### B.2 How parts are placed compactly (v1)

The order in which `resolve_places` works in derived mode. Today, anchors and edge parts are taken in
file order, then fiducials, then relations, against the content box.

1. **Anchors.** `at=`, `left`/`top` and Region-relative parts, each at its frame pose, in file order.
2. **I0** is the union of the anchors' courtyards and the keepouts. If it is empty and there is an
   edge part, that is B.5-3.
3. **Relations rooted at an anchor**, in today's dependency and file order. The `_attach` limit is I0.
   `_attach` insets its limit by `EDGE_CLEAR`, so it is handed I0 grown by that amount. A relation
   with no clear spot inside I0 is searched again with no limit. No edge exists yet, and `_attach`'s
   own search reaches only `_REACH` past its target pad. This keeps relations inside the anchors' box
   when they fit. In the first revision, c3_usb's and node's `SW_BOOT` drifted to y = −1.75; under
   this step it stays inside I0. On buck, I0 is `U1`'s courtyard alone, so all nine of its relations
   take the second search (command 7). **Decoupling reach (2026-10-07).** A clear spot inside I0 wins only
   while it is within `DECAP_NEXT_MM` (5 mm) of the pin the relation attaches to; farther than that, the
   unlimited search runs too and its spot wins when it is nearer by more than `DECAP_MM` (2.5 mm)
   (`pcb_place._relate`). Without it c3_usb's U1.3V3 caps took a clear spot inside I0 8.9 and 9.5 mm from
   the pin and EN went unrouted; with it they stand 1.61 and 2.39 mm from it, as on the declared board.
4. **I** is the union of the courtyards placed so far, the keepouts and the Regions (a Region counts like a
   courtyard, B.1: an edge group docks outside it, so no fixed side cuts it).
5. **Edge groups**, in file order. A group is an `edge=` part and every relation whose `to=` chain ends
   at it.
   - **Shaped alone.** The edge part is placed on a provisional line 300 mm out from I. Its rotation is
     `_rot_for_face`, as today. Its along-edge position is the centre of I0's span on that axis, or its
     frame `left`/`top`. Its relations are then attached in today's order, with the `_attach` limit
     equal to the half-plane inside the provisional line. Nothing else is near, so the group's shape
     depends on the group alone.
   - **Docked.** The whole group then moves as one rigid body, perpendicular to its edge. It starts where
     its line (the edge part's outer face plus its own `_edge_css` inset) is at the farther of two
     places: I's side, or every placed pad plus `edge_clearance`. From there it steps outward by
     0.01 mm until every courtyard in the group clears every placed courtyard, fanout lane (`Foot.keep`)
     and keepout by `GAP` (`pcb_place._clashes`).
   - The pad floor keeps every non-group pad at least `edge_clearance` inside the line. The first
     revision's `R_SCL.1` sat 0.150 mm from node's right line. Under this rule, no pad on any of the
     four boards is within `edge_clearance` of a fixed line (command 7, the `pad … inside` check).
6. **Fixed lines.**
   - Each side that has edge groups is fixed at the outermost of their lines, rounded outward to the
     0.01 mm grid (2026-10-07: the report prints two decimals and B.6 freezes from them; node's right line
     at U4's pad floor, 56.6935, froze 0.0035 mm inside it and failed KiCad's edge clearance). Every group
     on that side is moved outward to it, keeping each part's own inset, so an `overhang=` part keeps its
     overhang. When the pad floor set the line, the report says which pad (`standing at U4.1 pad ...`).
   - Then each side with no edge part, where the keepouts reach at least as far out as every courtyard
     and every Region, is fixed at the keepout's face, rounded outward to the grid. A keep-out that reaches the placement's boundary stands at the
     board edge, which is what the antenna keep-out on node and c3_usb is for. It also stops routing
     from running copper beside the antenna on the outside and moving the antenna off the edge.
   - The remaining sides are free.
   - A part of one group that crosses another side's fixed line is B.5-4.
7. **Fiducials** go last, because their spots depend on everything else.
   - The candidates are today's eight (`fiducial_spots`: NE, SE, SW, NW, then the edge middles), set
     `FID_INSET` = 4.0 mm inside the placement box P (all courtyards, keepouts and Regions, within the
     fixed lines). A candidate is taken if its courtyard clears every courtyard and keepout by `GAP`.
   - If fewer than three are free, each remaining candidate that has a free side moves outward across
     that side until it is clear.
   - Every fiducial centre is then kept `FID_INSET` from the final edge, so a free side grows to
     reach it. This keeps fab's own rule, "≥ 3.35 mm from the edge" (`fab.py:1565`), with
     today's 4.0. The existing "only N fiducial spot(s) are free … JLC wants 3" line is unchanged.

**Stability (command 8, node).** Moving one anchor moves its own `to=` tree rigidly. It moves an edge
group only when that anchor sets I0's span (the group's centre) or I's side (its docking depth), and a
group always moves rigidly. Measured:

- Moving `U4` by +2 mm in x moves its tree by 2.0, the `J_LOAD` group by 2.0 (the right side follows
  `R_SCL`) and the `J1` group by 1.0 (half the span change). The bottom side does not move.
- Moving `U2` by +3 mm moves only its four capacitors.
- Moving `U2` by −6 mm moves its capacitors and docks the `J1` group 2.46 mm higher.

In the first revision, moving `U4` by +2 moved `R_CC1` by 5.16 mm and reshaped the `J1` side. The
derivation is deterministic: two runs of node gave identical output (`cmp`).

**What v1 does not do.** Relations rooted at an anchor are placed before any edge part exists, so they do
not lean toward an edge part's pads. Buck's `L1` lands at (13.21, 15.04) rot 90, away from `J_OUT`'s
VOUT pad. Today it is at (23.59, 14.00) rot 180. This is **not fixed**, with evidence: a second pass
that re-placed every relation after the edge groups changed no outline on buck, c3_usb or node. On buck
it changed no pose at all, and `L1` stayed at (13.21, 15.04) (command 7, `sim_rev2.py`
`--nopass2` against the default). The groups are positioned by the interior, so the interior cannot
also depend on them without iterating. Where the side matters, the AI locks the pose or writes
`toward=` (§7).

**No packing of groups in v1.** Pulling the anchor groups toward each other means moving declared
anchors, which §6 forbids. To do it, pcbc would need:

- (a) a declaration that an anchor's position is free, for example `Place("U4", near="U1")`, or §6's
  solved placement for undeclared parts with area added to its objective;
- (b) a search whose objective is the router (§6: "the router itself, not a proxy").

**Estimated cost, from §6's measured 12–70 ms per channel-map candidate:** node has 3 anchor groups and
2 edge groups. A 1 mm grid over 60 × 45 mm gives about 2,700 positions per group. One greedy pass over
5 groups is 5 × 2,700 × 12–70 ms, which is **162–945 s**. That is v2, after solved placement exists.
In v1 the AI compacts the board itself, using the report line (B.4), which names the part or copper
that set each side.

### B.3 How the outline is derived: route-then-fit

There are two circular dependencies:

- the pours fill the outline, and the copper the router lays decides the outline;
- edge parts need an edge before anything is routed.

The options:

- **Courtyards and a routing allowance before routing, never refitted.** The allowance is a guess. Too
  small and nets go unrouted; too large and the board is bigger than it needs to be, which works against
  "optimize for pcba size". Routed copper does leave the courtyard box: buck's reaches y = 4.98 against
  a courtyard top of 5.98 (+1.00 mm), and c3_usb's reaches x = 38.35 against 37.34 (+1.01 mm)
  (command 1).
- **Route with no boundary.** The route scene needs a bounded outline item and a grid, and the edge
  parts need their edges at placement time anyway.
- **Route-then-fit (chosen).** Each dependency runs one way only:
  1. **Placement** (B.2) fixes every side that has an edge group or a keepout at the boundary.
  2. **Routing box R**: the fixed lines, with each free side at P's extent plus an allowance
     `A = edge_clearance + 8 × the widest pitch` (pitch = track width + clearance over every compiled
     class and path). That is 3.18 mm on buck, c3_usb and DS2, and 3.57 mm on node (command 2). The
     largest overshoot measured on the four routed examples is 1.01 mm.
     - **The fact when copper meets R.** If routed copper comes within one pitch of R on a free side,
       the result carries `routed copper reached the routing box on the top side (y=…): the box is the
       placement plus 3.18 mm`. That is not an error, because the board is valid.
     - **When a net fails.** If a net is not routed and its search reached a free side of R, the router's
       error names that side, its coordinate and A, as the object in the way (§7b: the true cause).
     - **R bounds copper, not O.** On a free side O can lie up to `m − edge_clearance` outside R.
  3. **Route** inside R. Every site that uses `content_rect` gets R during placement and routing. These
     are the `route_scene` outline item (R inset by `edge_clearance`, as today), and
     `route_checks.build_ctx` (`route_checks.py:63`): its escape flood grid (`_Grid`, 774–944), the F.6
     reference-plane check (1495) and the isolation corridors (1602/1609). They also include
     `pad_corridors`/`corridor_errors` (`pcb_place.py:586/642`), which get the same rectangle as
     `_attach`. Plane targets (`plane_targets`) do not depend on the pour's shape.
  4. **Fit, giving O.** Fixed sides stay where they are. Each free side moves to the outermost of:
     - courtyards (excluding an edge part's `overhang`), keepouts, core rule areas, Regions, and
       fiducial centres plus `FID_INSET`, all at margin 0;
     - pads and routed and core copper (segs, arcs, vias), plus a margin `m`.

     Each free side is rounded outward to 0.01 mm. On a board with no pour, `m = edge_clearance`.
     After the fit, the F.6 reference-plane check (`route_checks.py:1495`) runs again against O, because
     at place time it saw R. On a free side every pad then sits at least `m − edge_clearance` inside the
     plane's edge (O inset by `edge_clearance`).
     With any pour, `m = edge_clearance + the largest clearance of any class + 2 × pour_min_mm`.
     That is 0.3 + 0.20 + 0.2 = **0.70 mm** on all four examples (command 7 prints `m`). The first
     revision used the Default class (0.56 mm). KiCad clears a pour from another net's item by the
     larger of the zone clearance and that item's class clearance, so on buck the ring outside a `Power`
     (0.20) segment was 0.06 mm, below `min_thickness` (`review-geometry.md` E3). With this `m`, the
     ring between the outermost copper's clearance halo and the edge pull-back is at least
     2 × `pour_min_mm`. Every pour region that reaches R's boundary still meets the ring in O, and
     regions inside the copper are unchanged.

     This is argued, not measured. PR 3's test builds one board derived and with `Outline(O + 5 mm)`.
     For each pour net it compares two things: the set of pads that each filled piece touches, and,
     along each free side, the narrowest filled width in KiCad's own fill, which must be greater than
     `min_thickness`. An island count alone cannot see a 0.1 mm strip joining two pieces.
  5. **Drawn from O after the fit:**
     - the `Edge.Cuts` `Rect`;
     - the plane `Pour`s (`route_native` plane pours: O inset by `edge_clearance`);
     - a core `Pour` with no points. Today `layout_emit.stamp` fills it at core load, before routing,
       from the `Board` size inset 0.5 mm (`layout_emit.py:125/139`), and it enters the route scene as a
       lock. In derived mode `stamp` fills it from R inset 0.5 for routing, and it is filled again from
       O inset 0.5 before emit. It is a pour, so KiCad's fill applies the clearances to the final shape;
     - the isolation corridor rule areas and slots. Compile (`constraints.py:2437`) records the
       corridor's across-span from the two Regions, and marks its along-span as "the outline". The route
       scene draws it to R. After the fit, the rule area is drawn to O. The slot is **recomputed from O**
       with today's webs: `SLOT_WEB_MM` = 3.0 from each end of O's along-span (`place_native.py:187/190`).
       It is not clipped from R, because clipping could leave a web of 0 (`review-geometry.md` E1).
       If O's along-span is not longer than 2 × 3.0 mm, no slot is left between the webs, and that is B.5-7.

     KiCad fills the pours at emit, after all of this, and DRC judges O. `min_copper_edge_clearance`
     holds for three reasons:
     - every copper item is at least `m ≥ edge_clearance` inside a free side;
     - routed copper is at least `edge_clearance` inside a fixed side, because that is the route
       scene's outline;
     - every non-edge pad is at least `edge_clearance` inside a fixed side, by B.2's pad floor.
   - **One home for the 0.3 mm.** `pcb_place.EDGE_CLEAR` (0.3, a constant) and
     `stackup.edge_clearance` are two homes for one number. PR 3 makes `pcb_place` read the stackup.
     Every stackup in the tree has 0.3 (command 10), so no declared board changes.

`pcbc pcb` (`--upto place`) has no routing. It fits P with the pad margin and reports
`placement-only fit; routing may move the free sides`. The full build fits after routing, so the two
Edge.Cuts differ by design.

**Determinism.** O is a function of the placement and the routing, which are themselves deterministic,
and of outward rounding. Two builds give the same bytes. PR 3's test builds the same board twice, from two
different working directories (§8).

### B.4 What is written where

- **`layout.gen.py`**: the `Edge.Cuts` `Rect` with O's corners (the line `place_objects` writes today,
  with O instead of `(0, 0)–(w, h)`). It also has one `Place` line per footprint and fiducial, as now.
  The header names the outline's source: `Edge.Cuts outline: derived (no Outline() in layout.core.py)`
  or `declared, layout.core.py:N`.
- **Report line** (`result["outline"]`, printed by build and `pcbc pcb`), for example:
  `outline: derived 26.81 x 18.00 mm at (2.31, 5.43); left: J_IN face (edge="left", layout.core.py:3);
  right: J_OUT face (layout.core.py:4); top: <ref>.<pad> pad at y=6.13 + 0.70 margin; bottom:
  <ref>.<pad> pad at y=22.73 + 0.70 margin`. These are buck's placement-level numbers under B.2, with
  B.7's core file; command 7 gives the sides' coordinates, not the pads' names. A routed build names
  the seg, arc, via or pad. The JSON form is `{"source": "derived"|"declared", "rect": […], "sides":
  {"left": {"by": "J_IN", "what": "edge face", "line": 3, "at": 2.31}, …}}`. The size and corner are stated in
  the same terms as `Outline(width=, height=, at=)`, which is how a derived outline is frozen (B.6).
- **The route stage's stamp** (`inputs.json`) records O. That makes the record that later stages read
  part of the existing stale checks.
- **Fab and review** take O from the emitted board's `Edge.Cuts` `gr_rect`. §2 lets fab and the checkers
  read the `.kicad_pcb`, and that rectangle is exactly what gets fabricated. They do not recompile it,
  and they do not re-read the board. Today fab compiles fresh (`build.py:1491`) and review re-reads
  `board.py` (`review.py:441-442`), and neither can know O.
  - `FAB_NOTES.md` (`fab.py:1555`) says `Board W×H mm (derived)` or `(declared)`.
  - The review page (`review.py:560`) says the same.
  - `insert_fiducials` (`fab.py:443-470`) takes O's rectangle instead of a size and the (0, 0)
    origin it assumes now. It is reached only when the routed board carries no `FIDn`.
- Nothing is written to `board.py` or `layout.core.py`. O is build output (§2).

The places that read the board's size or `content_rect` today all move to one `job.outline` (a `Rect`:
declared; or R during placement and routing, then O):

| Area | Sites |
|---|---|
| Compile, check, apply | `compile.py:171`, `check.py:46`, `apply.py:32` |
| Resolution | `layout.py:board_rect/content_rect` (the schematic `SHEET`, `sch_place.py:12`, keeps `BoardSpec`; it is the sheet, not the board) |
| Placement | `pcb_place.py:_board_of`, `fiducial_spots`, `layout_report` (edge checks, which move after the fit), `pcb_place.py:1030`, `pad_corridors`/`corridor_errors` (586/642) |
| Place stage | `place_native.py:154/187/190/213/237/326/415` |
| Seed | `seed.py:295` (not reached in derived mode: B.5-2) |
| Route checks | `route_checks.py:62-63` |
| Routing | `route_scene.py:680-687`, `route_native.py:159` |
| Core | `layout_job.py:329/341/356`, `route_native.py:1786-1799` |
| Emit | `layout_emit.py:125/139` |
| Constraints | `constraints.py:1857` (`resolve_regions`), `constraints.py:2437` |
| Silk | `silk.py:228-229` (`silk_plan`'s 0.4 mm edge box, from (0, 0) today) |
| Fab and review | `fab.py:443-470/730/1555`, `review.py:560` |
| Gen | `gen.py:778-788` (header) |

**Silk.** `silk_plan` gets O, after the fit, in place of `(0, 0)–(w, h)`. The first revision claimed
that the courtyard box always lies inside O. That claim is withdrawn as false: an edge part's courtyard
can pass its face (`overhang=`), and a courtyard can sit at margin 0 on a free side, where silk's
0.4 mm edge box then leaves 0 mm. Whether silk may come closer than 0.4 mm is the silk/3D workflow's
decision. It owns `silk.py`, so this site is coordinated with it.

### B.5 Errors (facts only, §7b)

1. **An anchor that needs an edge**: `layout.core.py:7: Place("D1") right=8 measures from the
   outline's right edge; this board has no Outline(), and its right edge is derived from the placement`.
   The same form covers `bottom`, `margin: auto`, `%`, an along-edge `right`/`bottom` on an `edge=`
   part, and an axis with no `left`/`top`/`at` (`…: Place("U2") has no left= or at=: with no Outline()
   there is no width to centre it in`). It applies to `Region` and `Keepout` too.
2. **A part with no position**: `R7, C9 have no Place() in layout.core.py; with no Outline() the
   outline is derived from where the parts are, and these have no position`.
3. **An `edge=` part with nothing to define its edge**: `layout.core.py:3: J1 (edge="bottom") has no
   edge to stand on: this board has no Outline(), and no part placed by at=, left/top or a Region and
   no keepout, so nothing places the bottom edge`.
4. **A group that crosses another side's fixed line**: `Q1 (to="J_LOAD.2", layout.core.py:30) reaches
   y=46.10, past the bottom edge at y=45.94 that J1 (edge="bottom", layout.core.py:1) sets`. None
   occurs on the four boards (command 7: P lies inside the fixed lines). Docking clears every earlier
   group, so the first revision's corner-overlap error cannot occur.
5. **Core copper outside a fixed side**: `layout.core.py:12: Seg on GND reaches x=-0.40, outside the
   left edge at x=0.00 that J2 (edge="left", layout.core.py:3) sets`. Core copper beyond a *free* side
   is design: the side grows to include it.
6. **A relation that cannot fit** is the existing `_attach` note (`no clear spot next to … within 8
   mm`), with the limit stated. On a fixed side it names the line and the part that set it. On a free
   side it names I0 or "no limit". No relation got stuck on any of the four boards (command 7).
7. **An isolation slot that does not fit O**: `Isolation HV/LV (board.py:NN): the slot needs 3.00 mm of
   board at each end, and the derived outline is 5.50 mm along y`.

`Outline()` with no `width` or `height` is refused at load (A.3), with the fact only.

### B.6 What stays declared-only, and how a derived outline is frozen

- **Freezing.** To keep a derived outline (an enclosure is now fitted to it), the AI declares it from
  the report line's numbers: `Outline(width=26.81, height=18.00, at=(2.31, 5.43))`. Every anchor keeps
  its pose, because anchors are frame coordinates in both modes and `at=` places the rectangle where O
  was. Edge parts and their relations then follow the declared-mode rules: flush with the declared edge,
  centred on it. They keep their derived poses only if their gen `Place` lines replace their core lines
  too. That is a fact of the two modes, and the doc states it so the AI is not surprised. An edge part's
  inset and its centring measure from the declared rectangle's own edges wherever `at=` puts it
  (2026-10-07: they were taken from the frame's origin, which put buck's J_IN 2.31 mm off a frozen board).
- **Rounded corners** (`radius=`): not in v1. A radius moves where the edge clearance bites at each
  corner, after routing has finished. When it is needed, `Outline()` grows `radius=`, declared only.
- **Cutouts and slots the author wants**: a field on `Outline()` when needed (`direction.md` §2).
  `Isolation(slot=True)` slots are derived (B.3 step 5).
- **Mounting holes at fixed positions and enclosure fits** (DS2's 47 × 25.4): mechanical facts the
  circuit cannot imply. A mounting hole written as a part with a `Place` is fine in derived mode, and its
  courtyard counts like any other.
- **Non-rectangular derived outlines** (an L shape, a hull): v2. The route scene's `outline` is a `Box`,
  `fiducial_spots` and the edge rule assume four sides, and `gr_rect` is one primitive.

### B.7 Migration of the example boards

All four examples drop their size **after Part A has landed** (PR 4). DS2 keeps its outline (A.6).

The numbers below come from a simulation of B.2 on the frozen code. It calls `pcb_place`'s own
`_attach`, `_edge_css`, `_face`, `_rot_for_face`, `_clashes` and `pad_corridors`, and was built on the
geometry reviewer's harness (command 7). It is placement-level, so it gives two bounds:

- **lower**: no routed copper past the pads plus `m`;
- **upper**: copper filling the whole allowance A on every free side.

PR 4 records the measured routed outlines.

| Board | Declared | Anchor changes needed | Sides (fixed by) | Derived, lower … upper (area change) |
|---|---|---|---|---|
| blinky | 40 × 25 = 1000 | `D1` right=8 + auto margins is B.5-1. Both `Place`s become `Place("R1", left=8, top=12.03)` (frame, same pose) and `Place("D1", to="R1.2")`. | all free; parts 5.02 × 1.46; three fiducials outside the parts | 10.98 × 14.54 = 160 … 12.18 × 14.54 = 177 (**−84 … −82 %**). The fiducials and their 4.0 mm are most of it. |
| buck | 40 × 25 = 1000 | None (`U1` is `left=15, top=8`). | left 2.31 (`J_IN`), right 29.12 (`J_OUT`); top and bottom free | 26.81 × 18.00 = 482 … 26.81 × 24.20 = 649 (**−52 … −35 %**) |
| c3_usb | 40 × 30 = 1200 | `U2` right=4 is B.5-1, so `Place("U2", at=(33.95, 11.7), reason="LDO")` (its resolved pose). | left 0 and top 0 (`ANTENNA`), bottom 28.21 (`J1` group); right free | 36.45 × 28.21 = 1028 … 39.58 × 28.21 = 1117 (**−14 … −7 %**) |
| node | 60 × 45 = 2700 | `U2` bottom=12 becomes `at=(28.05, 31.3)`; `U4` right=4 becomes `at=(54.75, 5.25)`. | left 0 and top 0 (`ANTENNA`), right 56.69 (`J_LOAD` group), bottom 45.94 (`J1` group); none free | 56.69 × 45.94 = **2604 (−3.6 %)**: narrower by 3.31, taller by 0.94 |

**Measured, PR 4 (2026-10-07).** The four examples built from fresh copies with no `Outline()` and B.7's anchors
(`scratchpad/outline/final_builds.sh`; KiCad 10.0.6); the placement-only fits equal the simulated lower bounds above to
the digit (`tests/test_board_outline.py::test_the_examples_derived_placement_matches_b7`).

| Board | Declared | Routed, derived O (area change) | Sides | Links routed/total; KiCad unconnected items |
|---|---|---|---|---|
| blinky | 40 × 25 | 10.98 × 14.54 at (5.02, 5.23) (−84 %) | all four by fiducials (FID_INSET 4.0) | its one link is a hop; 0 |
| buck | 40 × 25 | 26.81 × 19.79 at (2.31, 4.89) (−47 %) | left J_IN, right J_OUT; top an EN via, bottom a 5V seg (+0.70) | 15/16 (VIN open, as declared); 4 |
| c3_usb | 40 × 30 | 36.45 × 28.21 at (0, 0) (−14 %) | left, top ANTENNA; bottom J1; right U2.4 pad (+0.70) | 32/36 (EN and VBUS open; declared: VBUS only, 34/36); 9 |
| node | 60 × 45 | 56.69 × 45.94 at (0, 0) (−3.5 %) | left, top ANTENNA; right J_LOAD; bottom J1 | 43/45 (VBUS open, as declared); 6 |

Two facts the measurement adds. c3_usb's U1.3V3 caps: step 3 limits a relation rooted at an anchor to I0 (U1, U2 and
the antenna keep-out, y 0 to 15), and below U1 (y 14.9 to 17.9) they do not fit, so C_MCU_HF stands 8.9 mm and C_MCU
9.5 mm from U1.3V3 inside I0 (the layout report says so); the declared board had them under U1, and EN routed there.
blinky's outline is set by its three fiducials alone, 4.0 mm from every edge.

**Re-measured 2026-10-07 (the refutation fixes: step 3's decoupling reach, fixed lines on the 0.01 mm grid, Regions in
I).** Fresh copies, `scratchpad/osv/rebuild.sh`, KiCad 10.0.6; blinky and buck are unchanged.

| Board | Routed, derived O | Sides | Links routed/total; KiCad unconnected items |
|---|---|---|---|
| c3_usb | 36.45 × 28.89 at (0, 0) (−12 %) | left, top ANTENNA; bottom J1; right U2.4 pad (+0.70) | 35/37 (VBUS open, as declared; EN routes again); 6 |
| node | 56.70 × 45.94 at (0, 0) (−3.5 %) | left, top ANTENNA; right J_LOAD, standing at U4.1's pad floor; bottom J1 | 43/45 (VBUS open, as declared); 6 |

c3_usb's caps stand 1.61 (C_MCU_HF) and 2.39 mm (C_MCU) from U1.3V3, as on the declared board; its bottom grew 0.68 mm to
hold them. Node's right side is 56.70, not 56.6935: frozen from the report it keeps U4's pads 0.30 mm from the edge.

The first revision's hand estimates (c3_usb "−3 % or more", node "≈ 60 × 41.4") were not what its own
rules produce. The geometry review measured +2.5 … +32 % and −3.8 … +10.6 % under those rules, and they
are replaced by the table above.

The mechanical migration shrinks blinky and buck the most. It hardly shrinks node, because its anchors
(`U1` at the west edge with the antenna keep-out, `U2`/`U4` near the east) span the board between them,
and its `J1` group docks below `U2`. Bringing those in is a placement judgement, and §7 makes it the
AI's. The report line names exactly the parts that set each side.

**Buck's `layout.core.py` in derived mode** (complete; the answer to "what do I write without an
outline"):

```python
# buck: no Outline(); the outline is derived from the placement and the routing (docs/board-outline.md B).
Place("U1", position="absolute", left=15, top=8, locked=True, reason="buck IC in the middle")
Place("J_IN", edge="left", reason="input connector")
Place("J_OUT", edge="right", reason="output connector")
Place("C_IN1", to="U1.VIN")
Place("C_IN2", to="U1.VIN")
Place("L1", to="U1.SW")
Place("C_OUT1", to="L1.2")
Place("C_OUT2", to="L1.2")
Place("C_BOOT", to="U1.BOOT")
Place("R_EN", to="U1.EN")
Place("R_FB_TOP", to="U1.FB")
Place("R_FB_BOT", to="U1.FB")
```

---

## PR plan

Every PR runs the full suite on its own, with nothing else running:
`cd ~/pcbc && PCBC_REQUIRE_KICAD=1 .venv/bin/python -m pytest -q -p no:cacheprovider`. It has two known
failures: `test_bridge` (ds2, two grounds) and `test_route_geom` (kicad, air). The gen gates compare two
fresh builds in scratch copies, the PR's base and its head, never the checked-in gens.

1. **PR 1: placement moves to core (Part A), no change in behaviour.**
   - `load_board(path, core=)`: core runs under its own trace (`design.core_reads`). The sch stamp keeps
     `design.reads` only, and the place stamp records core. The sch stage and `check_board(pcb=False)`
     load with `core=False`.
   - `Place`, `Region`, `Keepout` and `Outline` go into the core namespace and leave `pcbc`'s exports,
     with the `pcbc.__getattr__` refusal. `_BOARD_FIELDS` shrinks, and `design.outline` is added.
   - Refusals in `board.py` for `Place`, `Region`, `Keepout` and `Board(width/height/size_mm/padding)`,
     on every import path. `language.Place` records nothing into the design while `board.py` runs.
   - `Place(layer=)` and `Place(rot=)`. `side=` and `rotate=` are refused. `locked=` is the KiCad flag.
   - Core refusals: fiducial refs, a pasted Keepout rule area, two `Outline()`, `Outline()` without
     size, and no `Outline()` (until PR 3).
   - `_line()` covers core, and specs carry `source`. The "Place line N" messages become
     `layout.core.py:N`.
   - `layout_job.load_core` and the `route_native` replay validate instead of running the file.
   - Isolation's Regions are resolved across files, and the side refusal names the file.
   - `compile.py:to_dict` drops `source`/`line` from keepouts and regions.
   - The gen header, the `layout_job` messages and the stale texts in A.3 are updated.
   - The examples and every test site in A.6's table move. `ds2_dir` gets its Part-A step, and the
     refusal is pinned against the real DS2 file.
   - Gate: gen byte-identical apart from the header, for the four examples and DS2, and the compiled
     fixtures unchanged.
2. **PR 2: the outline becomes job data**, with no change in behaviour. Every site in B.4's table reads
   `job.outline: Rect`. `insert_fiducials` takes a rectangle. PR 1's `size_mm` assignment is deleted.
   Gate: gen byte-identical again. `test_fifth_review.py:154/156` (`insert_fiducials(…, (40.0, 30.0))`)
   changes signature.
3. **PR 3: derived mode (Part B).**
   - The frame semantics and the B.5 errors.
   - The B.2 order: I0, edge groups shaped alone and docked, keepout-fixed sides, fiducials last.
   - The routing box R, the allowance fact, and the router's free-side fact.
   - The fit to O with `m` = e + the largest class clearance + 2 × `pour_min_mm`. Pours, rule areas,
     the core point-less pour and slots are drawn from O, and slots are recomputed with their webs.
   - `EDGE_CLEAR` reads the stackup. The F.6 reference-plane check runs again on O after the fit.
   - O goes into the route stamp. Fab and review read Edge.Cuts. The report line and `FAB_NOTES`.
   - Tests:
     - each B.5 error by mutation;
     - determinism from two working directories;
     - KiCad DRC `copper_edge_clearance` passes on a derived board;
     - the pour test from B.3 step 4 (pads per filled piece, narrowest ring wider than `min_thickness`);
     - the stability numbers of B.2, pinned on node.
   - **Gate: declared boards unchanged.** The four examples (still declared until PR 4) and DS2 must give
     a byte-identical `layout.gen.py` to PR 3's base, apart from the header's new
     `Edge.Cuts outline: declared, layout.core.py:N` line. The only expected `FAB_NOTES.md` diff is
     `(declared)`.
4. **PR 4: the examples drop their size.**
   - The B.7 changes to the anchors, and the measured outlines recorded in B.7 next to the simulated
     bounds.
   - Re-baselines, each listed in the PR with its old and new number:
     - the `places` and `board_size_mm` of `tests/fixtures/compiled/{blinky,buck,c3_usb,node}.json`
       (exact compare, `test_constraints.py:143-144`);
     - `test_circuit.py:32` (`size_mm == (40.0, 25.0)`);
     - `test_review.py:8`;
     - every failing test among the 31 files that read an example by path (command 9).
   - The owner gets the DS2 instructions (A.6), unchanged.
5. **Later (v2):** packing anchor groups, after §6's solved placement, and keeping a solved part on an
   Isolation side; derived rounded or non-rectangular outlines; `SchPlace`/`SchRegion` in a schematic
   core, if the owner wants it.

---

## Appendix: commands behind the numbers

`$S` is this session's scratchpad (`/private/tmp/claude-501/-Users-rileymccarthy-Documents-MaD/<session>/scratchpad`).
The frozen copy is `rsync -a ~/pcbc/ $FROZEN/ --exclude .venv --exclude layout`, taken 2026-10-03, with
`$FROZEN = $S/outline/frozen`. The routed gens are copies of
`examples/<b>/layout/<b>/routed/layout.gen.py` (2026-09-27) and of the DS2 migrated copy's
`layout/ds2_addon/routed/layout.gen.py`.

1. **Placement and copper extents.**
   `PYTHONPATH=$FROZEN/src ~/pcbc/.venv/bin/python outline_measure.py $FROZEN/examples/<b>/<b>.py <routed layout.gen.py>`.
   - The script loads the board, runs `place_native.place`, takes `pcb_place.posed_feet_of`, and prints:
     - the union of the courtyards (`Foot.world_box`);
     - the pads grown by `stackup.edge_clearance`;
     - the keepouts;
     - the interior (parts whose `to=` chain does not end at an `edge=` part);
     - the extent of every `Seg`/`Arc` (± width/2) and `Via` (± size/2) in the gen file.
   - Output, rounded:
     - blinky: 24.00 × 1.46 / 24.20 × 1.55 / copper 22.03 × 1.09.
     - buck: 40.00 × 11.31, interior 16.53 × 11.31 (15.00–31.53), copper (2.06, 4.98)–(37.94, 16.64).
     - c3_usb: 36.34 × 29.00, + keepout 37.39 × 30.00, interior x 1.00–37.34 / y to 22.95, copper to
       x = 38.35.
     - node: 59.00 × 44.00, interior to x = 58.45 / y = 33.00, copper 52.58 × 41.89.
     - ds2_addon: 46.97 × 25.37, copper 44.57 × 22.99.
   - Buck's routed gen places C_IN1, C_OUT1, C_OUT2 and L1 elsewhere than the frozen placement does.
2. **Pitches.** `compile_design(load_board(b)).classes`. The widest `track_width_mm + clearance_mm` is
   0.360 on blinky, buck, c3_usb and DS2, and 0.409 on node. `edge_clearance` is 0.3 and `pour_min_mm`
   is 0.1 on every example.
3. **Compact blinky.** `$S/outline/sim/blinky/blinky.py` is a scratch copy of `blinky.py`, outside
   `examples/`, with D1's `Place` replaced by `Place("D1", to="R1.2")`. Command 1 on it measures the
   parts at 5.02 × 1.46 (R1 8.00–9.86, D1 10.06–13.02).
4. **Absent and present arguments.**
   - `grep -rn "[^h]Place([^)]*side=" src tests examples | grep -v SchPlace` finds 0.
   - `grep -rn "Board([^)]*padding=" examples tests src | wc -l` gives 0.
5. **DS2 gen.** `grep -c '^Place' layout/ds2_addon/routed/layout.gen.py` gives 31 (28 parts + 3
   fiducials).
6. **DS2 diff.** `diff ~/Documents/MaD/Hardware/DS2Addon/pcbc/ds2_addon.py <migrated copy>` shows
   differences on lines 15, 95 and 97 only. `wc -l` on the migrated copy gives 165.
7. **Derived placement (B.2, B.7).** `cd $S/outline && PYTHONPATH=$FROZEN/src ~/pcbc/.venv/bin/python rev2/sim_group.py <board.py> floor [shift REF dx dy] --kofix [--poses]`.
   - `<board.py>` is `$FROZEN/examples/<b>/<b>.py`, or command 3's copy for blinky.
   - The script prints `m`, the fixed lines, the fiducials, P, the lower and upper bounds on O, any
     courtyard overlap, any part beyond the declared board, the `_attach` notes, and every non-group
     pad within `edge_clearance` of a fixed line. It printed none of the last kind on any board.
   - The pass-2 comparison is `rev2/sim_rev2.py <board.py> floor --kofix --poses [--nopass2]`. It has
     the same rules, with the edge part docked alone and an optional second relation pass. With and
     without `--nopass2` the outlines were identical on buck, c3_usb and node, and buck had 0 pose
     differences (`diff` of the `--poses` lines).
8. **Stability.** `cd $S/outline && bash rev2/shift.sh node U4 2 0` (and `U2 3 0`, `U2 -6 0`) prints
   the parts that move further than 0.01 mm, and by how much. Two runs of node gave identical output:
   `cmp rev2/r1.txt rev2/r2.txt`.
9. **Test inventory.** From `$FROZEN`:
   - fixture boards: `grep -rl "^Place(" tests/fixtures | grep "\.py$" | grep -v layout | wc -l` gives
     12, and `grep -rh "^Place(" tests/fixtures | wc -l` gives 106;
   - test texts: `grep -hE "Board\((size_mm=|width=|\()" tests/*.py | wc -l` gives 57, in 19 files;
   - `grep -c "Place line" tests/*.py | grep -v ":0"` gives 34 in 7 files;
   - `for f in tests/*.py; do n=$(grep "Place(" $f | grep -vc SchPlace); [ "$n" -gt 0 ] && echo $f; done | wc -l`
     gives 28, and `grep -rhn "Place(" tests/*.py | grep -v SchPlace | grep -c "Place("` gives 215;
   - `grep -lE "examples.{0,40}(blinky|buck|c3_usb|node)" tests/*.py | wc -l` gives 31.
   The copy-and-edit sites and the five default-pinning tests are cited by line in
   `$S/outline/review-build.md` item 5.
10. **Edge clearance.** `PYTHONPATH=$FROZEN/src ~/pcbc/.venv/bin/python -c "from pcbc import stackup as s; [print(n, s.get_stackup(n).edge_clearance) for n in s.STACKUPS]"`
    gives 0.3 for all eight stackups.
11. **`rotate=`.** `grep -rn "rotate=" examples tests | grep "Place(" | grep -v SchPlace | wc -l` gives 45.

---

## Revision 2 (2026-10-03): responses to the three reviews

Reviews: `$S/outline/review-ai.md` (AI), `review-build.md` (build), `review-geometry.md` (geometry).
Every item is addressed below. Nothing was marked wontfix without evidence.

| Item | Resolution |
|---|---|
| AI B1: `direction.md` still put the outline in `board.py` | `direction.md` §1's table and §2's outline bullet now say `Outline()` in `layout.core.py`, `Board()` with no size, and cutouts as an `Outline()` field. |
| AI M1: a derived outline cannot be frozen | `Outline(at=)`, with anchors in frame coordinates in both modes. The report line states the size and corner in `Outline`'s terms (B.4, B.6). |
| AI M2: not every gen `Place` can be pasted | Fiducial refs are refused, as a fact. `locked=` is KiCad's flag and every core `Place` fixes its pose. Locking is "replace the line" (A.3–A.5). |
| AI M3: two keep-out forms in core | `Keepout()` is the keep-out fact. A pasted derived rule area is refused by name. A core rule area stays a KiCad primitive that DRC enforces, and both count toward O (A.5, B.1). |
| AI M4: Isolation side membership in core | Accepted split, with the refusal naming `layout.core.py`. Recorded for §6/v2 (A.1). |
| AI M5 / build 16: no declared-board gate in PR 3; baseline | PR 3 gate added. All gates compare fresh base/head builds, and the full suite runs for every PR. |
| AI m1 / build 7: PR 1–2 with no `Outline()` | A refusal, as a fact, until PR 3. `core=False` loads reach no size reader, and that is tested (A.2). |
| AI m2: Board refusal untrue | The text lists `layers, stackup, planes, pcb and net_order`. |
| AI m3: `rot=`/`rotate=` | `rot=` only. `rotate=` is refused, and 45 calls migrate (A.4). |
| AI m4: vocabulary versus refuse-after-run | Removed from the exports, with `pcbc.__getattr__` refusing, and `language.Place` records nothing for `board.py`. "Placement keep-outs" equals every `Keepout()` (A.1, A.3). |
| AI m5: stale texts | Listed in A.3 and in PR 1. |
| AI m6 / build 11: padding moves anchors | `padding=` is removed from the outline (0 uses, command 4), so `at=` pastes verbatim in both modes. |
| AI m7: `Outline` signature | `(width, height, at)`. No `radius=` in v1. Missing size is refused with the fact only. |
| AI m8: derived mode stricter; no example | The strictness is kept, with the reason. Buck's complete derived core file is given (B.7). |
| AI m9 / build 14: "33 files" | 28 files hold a PCB `Place` (command 9). |
| AI m10: DS2 line 93 | It moves to core (A.6). |
| Build 1: PR 1 cannot store `Outline()` | `design.outline`, outside `_BOARD_FIELDS`, and one `size_mm` assignment as the bridge, deleted in PR 2 (A.2). |
| Build 2: DS2 tests break | `ds2_dir` Part-A step with blanked lines, plus a refusal test on the real file (A.6). |
| Build 3: core lines recorded as 0 | `_line()` covers the executing file, with `source`. 34 assertions are rewritten (A.2). |
| Build 4: staleness | A separate core trace. A core edit is stale from place and never from sch (A.2). |
| Build 5: inventory short | A.6's table, counted (command 9). |
| Build 6: compiled fixtures | `to_dict` drops `source`/`line` from keepouts and regions. `PlaceSpec.side` is kept internally. |
| Build 8: consumers before R/O | Compile resolves in the frame. The isolation corridor's along-span is "the outline", drawn to R and then O. The seed grid is not reached (B.3, B.5-2). |
| Build 9: fab/review have no O | The route stamp records O. Fab and review read Edge.Cuts. `insert_fiducials` takes the rectangle (B.4). |
| Build 10: core point-less Pour | `layout_emit.stamp` at core load; R inset 0.5 for routing, O inset 0.5 before emit (B.3 step 5). |
| Build 12: uncited sites | Added to B.4's table and A.2 (the replay caller). |
| Build 13: silk claim | Withdrawn as false. `silk_plan` gets O, and the 0.4 mm question goes to the silk workflow (B.4). |
| Build 15: fiducials, `side` field | As AI M2. `PlaceSpec.side` is kept, and the API is `layer=`. |
| Build 17: PR 4 re-baselines | Listed in PR 4. |
| Geometry 1: unbounded relation limit | Relations rooted at an anchor are limited to I0 first (B.2 step 3). |
| Geometry 2: order breaks lean (buck `L1`) | **Not fixed, with evidence.** A second relation pass changed no outline and did not move `L1` (command 7). The AI locks the pose or uses `toward=` (B.2). |
| Geometry 3: clearance with lanes | Docking uses `_clashes` with lanes and `GAP`, as written (B.2 step 5). |
| Geometry 4: I's side across the whole edge | The group docks from a floor and slides into free span. Node's right side is 56.69, not 66.05 (B.7). |
| Geometry E1: slot web | The slot is recomputed from O with 3.0 mm webs, and B.5-7 covers a short span (B.3). |
| Geometry E2: fixed line inside a pad's edge clearance | The pad floor in docking. No pad was within `edge_clearance` on any board (command 7). |
| Geometry E3: pour ring | `m` uses the largest class clearance + 2 × `pour_min_mm` (0.70 mm). The test compares pads per piece and the narrowest ring (B.3). |
| Geometry E4: copper beside an edge keep-out | A side the keepouts reach is fixed at the keepout's face (B.2 step 6). |
| Geometry E5: fiducials too close to the edge | Every centre is `FID_INSET` = 4.0 from the final edge (B.2 step 7). |
| Geometry E6: stability | Groups are shaped alone and move rigidly. The coupling is limited to I0's span and the docking depth, and is measured (B.2, command 8). |
| Geometry E7: implicit auto axis; lost edge meaning | B.5-1 covers an axis with no `left`/`top`/`at`. Keepout-fixed sides keep node's and c3_usb's `U1` at 1 mm from both edges (B.1). |
| Geometry E8: `route_checks` and corridors | They get R during placement and routing, the same rectangle as `_attach` (B.3 step 3). |
| Geometry E9: router failure at R; O versus R | The router's error names the free side of R. "R bounds copper, not O" is stated (B.3). |
| Geometry minor: overhang | The line is the face plus each part's own inset, and groups move rigidly (B.2 step 6). |
| Geometry minor: impedance near a plane edge | **Not changed in v1, with evidence.** On a fixed side copper may sit at `edge_clearance`, the same as declared boards today. A free side gives `m − e` = 0.40 mm, which is more. |
| Geometry minor: Regions | They count toward O at margin 0 (B.1). |
| Geometry minor: B.5-6 on a free side | B.5-6 states I0 or "no limit". No relation got stuck on the four boards (command 7). |
| Geometry minor: `EDGE_CLEAR` versus the stackup | One home. All eight stackups have 0.3 (command 10). |
| Geometry minor: "two build paths" | Two working directories. `pcbc pcb` and the build differ by design (B.3). |
| Geometry: B.7 numbers | Replaced by the simulated bounds under these rules (B.7). |
