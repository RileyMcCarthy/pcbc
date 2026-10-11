# Physics subjects: one home for every physical requirement

Design, 2026-10-01. Nothing here is built.

Owner, 2026-10-01: *"current is path and voltage is net for example. figure out the other properties what
they should use and make the tool only work for that, for example no current number for nets"*. Recorded in
`docs/direction.md` §5b. Read §5b (net and path), §5c (SPICE is the last stage) and §7b (errors, not
suggestions) first; they win over this document. `docs/net-path-physics.md` defines `Path()`, stations,
junction rules and via groups. Its §2 table ("which field lives where") is replaced by §2 below.

Line numbers are from a frozen copy of the working tree taken 2026-10-01
(`rsync -a pcbc/ <scratchpad>/subjects/frozen/ --exclude .venv --exclude layout`). Another workflow is
editing the tree, so a line may have moved. `ds2_addon.py` is
`~/Documents/MaD/Hardware/DS2Addon/pcbc/ds2_addon.py`. It belongs to the owner and was read only. Computed
numbers name the call that produced them. They were run as `PYTHONPATH=frozen/src .venv/bin/python`.
Routing numbers (§10.1) come from `cli.main(["build", <board>.py])` on scratch copies of the frozen
examples, today's code, with each file's requirement lines rewritten.

Revision 1 (2026-10-01) answers the BUILDABLE review (C1–C13). What changed is listed at the end, under
"Revision Summary".

---

## Summary

- **The rule.** Every physical requirement has exactly one subject. pcbc accepts the requirement only on
  that subject. Written anywhere else, it is an error that states facts (§7). Seven homes are used:
  - **NET**: the same at every pad, such as voltage and charge.
  - **PATH**: the copper between named pads, such as current.
  - **PAIR/BUS**: paths that travel or are timed together.
  - **LOOP**: an area closed through parts.
  - **PIN**: one pad's heat.
  - **NET-TO-NET**: a relation between two nodes.
  - **BOARD**: the medium. It carries no requirement.

  No new subject is needed.
- **The test used for every row.** Physics decides the subject:
  - A quantity that copper cannot change between two pads of one node belongs to the NET. Over a few
    millimetres of copper the potential differs by millivolts.
  - A quantity that differs from one piece of copper to the next belongs to the PATH. Examples are
    current, the layer it runs on, its length and its cross-section.
  - A quantity defined between two conductors belongs to a PAIR or a NET-TO-NET relation.
- **What leaves the net.** `amps`, `temp_rise_c`, `vias`, `vias_max`, `layers`, `reference`, `z_se_ohm` and
  `length_mm` leave the net for the PATH. `pair`, `z_diff_ohm`, `uncoupled_mm`, `match_mm` and `clock` leave
  for PAIR/BUS. `loop_mm2` leaves for LOOP.
- **What stays on the net.** `volts`, `rise_ps`, `pf_max`, `max_mm` (the span of the node), `keep_clear_of`
  and `class_name` stay. `keep_clear_of` is a net-to-net relation written on the victim.
- **What leaves the path.** `keep_clear_of`/`keep_clear_mm` leave the path. Every branch of a node is the
  same node. `max_mm` on a path folds into `length_mm`.
- **Current on any net.** A Path may carry a current on any net, whatever the net's kind. A Path may also
  name every pad of its net. Today both are refused (`paths.py:344-355`, `373-381`), and those refusals
  only existed to push a current back onto the net.
- **A rail with many loads is one line.** It is written as
  `Path(net, source, *loads, amps={load: A, ...})`. Each named station draws its own current, and by
  Kirchhoff's current law each link carries the sum of the draws beyond it. That is enough to size every
  width before routing, and the SPICE stage injects the same draws.
- **Kinds are split by subject.**
  - Net kinds: `generic`, `power`, `switch_node`, `analog`, `clock`, `i2c`.
  - Path kinds: `feedback`, `sense`.
  - Pair kind: `usb_hs`.
  - Bus kind: `spi`.

  A net kind keeps only what holds at every pad: the clearance number, the spacing, the span, and which
  loops it generates. It sets no width, via, layer or current.
- **Copper with no path** gets the class floor: 0.16 mm, which carries 0.633 A at 10 °C, and the stackup
  via, which carries 0.707 A on 2 layers and 0.527 A on 4. **No formula stage protects a current nobody
  declared.** `direction.md` §5b/§5c say the SPICE stage judges every piece of copper by its *solved*
  current. That solve sees a load nobody wrote only where the part has a vendor model that draws it. §6
  states what is left and §10 asks the owner. Poured rails cannot declare a current until PR 8.
- **KiCad checks what holds net-wide.** A KiCad rule selects copper by net, never by "the copper between
  pad A and pad B". Once current leaves the net, KiCad checks every net's floor width. pcbc checks each
  path's width, vias and impedance, and so does the SPICE stage once it exists (KD 12).
- **Migration.** All five boards change, and so does the pipeline test vehicle `tests/fixtures/buck_netreq`.
  The DS2 Addon gets two new errors (`amps=`, and `volts=` on Ground nets) on top of today's `Chain` error.
  The owner must edit his own file.
- **Routing is not preserved.** Measured on today's code (§10.1): buck's VIN, written as the Path this
  document migrates it to, no longer routes as placed. Junction rule 2 is the cause. No repo board
  completes routing on the frozen tree even before any change.

## Key decisions

1. **Current is a path's.** This covers `amps`, `temp_rise_c`, and everything sized from current: width per
   layer, via count, the via group and the pad neck. A `NetReq` accepts none of them.
   The *use* of `POWER_DEFAULT_AMPS` (`constraints.py:1048`) as the current of an unnamed power branch is
   deleted, because no current is invented for copper nobody described. The constant's other use is the
   0.2 A step of the KD 10 floor (`constraints.py:1744-1746`), and that use stays. It is renamed
   `CURRENT_FLOOR_STEP_AMPS`, so the old name cannot be read as a net default. Its two report prints
   (`paths.py:649`, `722`) go with the net current in PR 3.
2. **A Path takes a current on any net.** Neither the net kind nor naming every pad stands in the way. The
   only remaining every-pad refusal is `kind=` on every pad, since a sense branch needs a rail to branch
   from.
3. **`amps=` takes either form.**
   - A number means the current enters at the first station and leaves at the last. This is today's
     meaning.
   - `{station: A}` means each named station draws that current, and the first station supplies their sum.
4. **Copper fields belong to the path.** These are `vias`, `vias_max`, `layers`, `reference`, `z_se_ohm` and
   `length_mm`. A path's placement budget is its `length_mm`: placement checks the airwire against it, and
   pcbc measures the routed length after routing. `Path(max_mm=)` is refused.
5. **Node fields belong to the net.** These are `volts`, `rise_ps`, `pf_max`, `max_mm` (the span of the
   node) and `class_name`. A `Ground()` net takes no `volts=`, because it is the 0 V its domain is
   measured from.
6. **Keep-away is NET-TO-NET**, written on the victim's `NetReq`. Two facts settle it:
   - Capacitive pickup depends on the victim node's impedance and the aggressor node's dV/dt. Both are
     properties of nodes.
   - The only coupling that differs from branch to branch is magnetic, and magnetic coupling is set by a
     loop's area, which is a `Loop()`.

   `Path(keep_clear_of=)` is refused.
7. **A loop budget is written only on `Loop(mm2=)`.** Net kinds `power` and `switch_node` still generate
   their guessed loops: the decap loop at 6 mm² and the hot loop at 20 mm² (`constraints.py:1034`).
8. **PAIR and BUS are groups of paths.**
   - A `Pair()` owns `z_diff_ohm`, `match_mm`, `uncoupled_mm`, `gap_mm`, and `layers`/`reference` for
     both of its paths.
   - A `Bus()` owns `match_mm`, `clock` and `length_mm` for each member.
   - `usb_hs` becomes a Pair kind and `spi` a Bus kind.
   - A member net with more than two pads must have exactly one Path that names the stations being
     compared.
9. **Clearance stays as today: `max(row(Va), row(Vb))`.** Each net's voltage is taken against its domain's
   0 V. This is conservative whenever only one of the two voltages is known, which is the usual case.
   Creepage is added between two nets of one class, which today get none (finding F1, §3.9).
10. **Current-carrying paths share one width floor.** A path with a current has a minimum of 0.4 mm at
    0.2 A or more, and 0.25 mm below 0.2 A. This is today's power floor (`constraints.py:1743-1746`). It
    is a pcbc default with no source, kept so that no declared path changes copper. The `switch_node`
    floor of 0.3 mm folds into it. That changes copper only for a switch-node path below 0.2 A, which would
    go from 0.3 to 0.25 mm. No example or test writes one: every `Path("SW", ...)` in `examples/` and
    `tests/` carries 1.5 or 2 A (`grep -rn 'Path("SW' examples tests`), and no `switch_node` NetReq
    in them writes `amps=`.
11. **Poured rails can declare currents after PR 8.** A draws-Path on a poured net sizes each station's
    joint to the pour for its own draw. Until PR 8 those currents cannot be declared, and §6 says what
    that leaves unprotected.
12. **KiCad gets a per-net rule only where it is true net-wide.** A KiCad custom rule selects copper by
    net or netclass (`dru.py:85-182`). It cannot select the copper between two pads. Rules written:
    - The class `track_width` is the class floor. A net-wide minimum above the floor would be false, because
      an unnamed branch may legally run at the floor.
    - `width_net_`, `novia_`, `vias_` and the z_se class width are written for a net only when a single
      Path names every pad of that net. That Path is then the whole node, which is true of node `LOAD` after
      migration.
    - `length_` is written for a Path that names every pad, and for the i2c length that
      `constraints.py:2021-2038` derives from `pf_max`. That length bounds all of a node's copper, which is
      a net quantity (N17).
    - Clearance, creepage, keep-away and isolation rules are unchanged, because their subjects are nets.

    Everything else about a path is checked by pcbc: the second loop of `copper.py`'s fab gate, the
    ampacity gate, and the SPICE stage once it exists. The report prints each path requirement that KiCad
    does not check (§9, PR 3 and PR 5). The owner decides whether that is acceptable (§10, O3).
    Today's KiCad rule is already the net's narrowest copper whenever the net has a path (`dru.py:113-119`).
    For buck's 5V that is the 0.2 mm feedback tap (`copper.py`'s comment above its second loop).
13. **A path with a current gets its own via size.** Via size is copper, so it is a path default. The
    default is 0.8/0.4 mm, or 0.6/0.3 mm on a `switch_node` net. That is today's pair of preset values,
    so no copper moves. The switch-node value reads the net because a switching node's copper area is the
    node's own (N9), and a via pad is part of that area. Like KD 10, it is a pcbc default with no
    published source. A path with no current uses the stackup via. `route_cost.link_nc` copies the link's
    via size (`route_cost.py:217-245`, whose docstring at 201-203 says the size is the net's). The link's
    `ViaSpec` and via group are built from it (`paths.py:537-540`).
14. **The spine pattern is deleted in PR 3.** `patterns/spine.py:285-296` takes a net only if it has no
    fielded path and its class width is at least `WIDE_MM` = 0.4 (`spine.py:78`). After PR 3 the class
    width of every net is the floor (0.16 mm). Every rail that carries a current has a fielded Path.
    Its population is therefore empty on every board, and dead code is deleted rather than kept. Whether
    a draws-Path should get a spine later is §10's O4.
15. **Path kinds get their own preset table** (`PATH_PRESETS`: feedback, sense), split from `PRESETS` in
    PR 5. Today paths read `PRESETS["sense"|"feedback"]` (`paths.py:496`, `515`, `530`, `597`), and PR 7
    refuses those net kinds.
16. **Spacing is a net multiplier times a link width.** `spacing_w` (3W or 5W) stays on the net kind,
    because whether a node is an aggressor or a victim is a node fact. W is each link's own width.

---

## 1. The subjects

| subject | what it is | written as |
|---|---|---|
| NET | one electrical node, the same at every pad | `NetReq(*nets, ...)` |
| PATH | the copper between named pads of one net, in feed order | `Path(net, *stations, ...)` |
| PAIR / BUS | two (or N) paths that travel or are timed together | `Pair(p, n, ...)`, `Bus(*nets, ...)` |
| LOOP | a cycle of named pads across nets, closed through parts | `Loop(*pads, mm2=)` |
| PIN | one part, or one of its pads | `Thermal(pad, ...)` |
| NET-TO-NET | a relation between two nodes, or two domains of nodes | `Isolation`, `Bridge`, `Guard`, `NetReq(keep_clear_of=)` |
| BOARD | the medium every formula reads. It is not a requirement subject. | `Board(...)` |

A `Bus` is a PAIR with N members. A `keep_clear_of=` is written on the victim's `NetReq` because the
victim's impedance decides it, but the requirement is a distance between two nets.
`ClearanceTable.between` applies it in both directions already (`constraints.py:2418-2428`).

## 2. Every property, one row

Legend:
- **today** means where pcbc accepts the property now.
- **wider / narrower** is the counterexample test. "Wider" says what the property would make false if
  written on a larger subject. "Narrower" says what would be lost if it were written on a smaller one.
- **E#** is the error text in §7 that the property gets when written anywhere else.

### 2.1 `NetReq` fields (`language.py:1842-1866`, `model.py:284-311`)

| # | property | physical meaning | subject: why | counterexample test | today | elsewhere |
|---|---|---|---|---|---|---|
| N1 | `volts` | the peak working potential of the node against its domain's 0 V. It sets the IPC-2221B 6-1 clearance row, and the IEC 60664-1 creepage at 60 V or more (`constraints.py:1827-1840`) | **NET**: copper of one node is one potential, since milliamps through milliohms make millivolts | narrower: the owner's case. A 400 V bus has a sense tap carrying microamps. Written on the 2 A path only, the tap gets the floor clearance at 400 V. | NetReq | `Path(volts=)`: today's `_NET_ONLY` text (`language.py:2331`). On a Ground net: **E3** |
| N2 | `amps` | the current through copper | **PATH**: Kirchhoff's current law makes current differ branch by branch, so a node has no single current | wider: buck `5V` declares 2 A. The `R_FB_TOP.1` branch carries about 44 µA (5 V / 113.3 kΩ, `docs/spice-stage.md` B3). Buck `GND` declares 1.53 A on every branch, including `R_FB_BOT.2`'s via (spice-stage B2 table). Narrower (pins only): the draws are kept inside the Path (§5), so nothing is lost. | NetReq, Path (`language.py:1991`) | NetReq: **E1** |
| N3 | `temp_rise_c` | how hot the copper may run above ambient while carrying its current | **PATH**: it is half of the current's statement, and copper carrying nothing does not rise | wider: on the net, a sense tap carrying nothing would get a rise budget it cannot use | NetReq (default 10), Path | NetReq: **E2** |
| N4 | `z_se_ohm` | the characteristic impedance of a conductor over its reference | **PATH**: an impedance belongs to the copper's cross-section over the plane under it | wider: on an RF feed with a DC bias choke (illustrative), only the antenna-to-LNA copper is a 50 Ω line. The choke branch is not, and a net-wide 50 Ω sizes it for no reason. | NetReq (`constraints.py:1975`) | NetReq: **E4** |
| N5 | `z_diff_ohm` | the impedance between two coupled conductors | **PAIR**: it is defined between two paths, never on one | wider: a single net has no differential impedance. Narrower: one path loses its partner. | NetReq (`kind="usb_hs"`), Pair | NetReq: **E5** |
| N6 | `pair` | these two nets are routed as one coupled object | **PAIR** | as N5 | NetReq | **E5** |
| N7 | `match_mm` | the skew budget between the lengths of two or more paths | **PAIR/BUS**: a length difference needs two lengths | wider: a net's total copper includes stubs. `USB_DP` on c3_usb has 4 pads (`J1.A6`, `J1.B6`, `U3.1`, `U1.27`), and the `J1.B6` stub is not on the signal's way. | NetReq (clock, usb_hs, spi, sense), Pair, Bus | NetReq: **E6** |
| N8 | `uncoupled_mm` | how much of a pair may run apart | **PAIR** | as N5 | NetReq, Pair | **E5** |
| N9 | `max_mm` | the span of the node: the MST airwire of all its pads. It stands in for its copper area at placement. | **NET**: the copper of a switching node radiates, and a high-impedance node's copper collects charge, over its whole area, including every stub | narrower: buck `SW`'s `C_BOOT.2` branch is part of the switch node's area. A budget on the path `U1.SW→L1.1` leaves it out. | NetReq (`constraints.py:2008`), Path (`paths.py:597`) | Path: **E9** |
| N10 | `length_mm` | the routed length between two pads | **PATH**: a length runs between two points | wider: KiCad's per-net length rule sums every stub (N7's `J1.B6`) | NetReq (usb_hs, spi) | NetReq: **E4** |
| N11 | `vias` | whether this copper may change layer | **PATH**: a via is a piece of the copper between two pads | wider: on buck `5V`, the 2 A path may change layer through a via group (owner, 2026-09-30). A net-wide `vias=False` forbids that for the sake of the feedback tap. | NetReq, Path | NetReq: **E4** |
| N12 | `vias_max` | how many vias this copper may pass | **PATH** | wider: on a multi-pad net it counts stub vias against the signal's budget | NetReq (clock, usb_hs) | NetReq: **E4** |
| N13 | `layers` | which layers this copper may run on | **PATH** | as N11 | NetReq, Path | NetReq: **E4** |
| N14 | `reference` | the plane carrying this copper's return current | **PATH, per layer** | wider: on node, a branch on F.Cu returns in In1.Cu (GND) and a branch on B.Cu returns in In2.Cu (3V3) (`node.py:96`). One net-wide plane is false for one of them. | NetReq, Pair | NetReq: **E4**. On a path with several layers: **E10** |
| N15 | `keep_clear_of`, `keep_clear_mm` | a minimum distance between the victim's copper and the aggressor's copper | **NET-TO-NET**: capacitive pickup is victim impedance × aggressor dV/dt, and both are node properties (§3.2) | narrower: buck `FB`'s `R_FB_BOT.1↔U1.FB` branch is as high-impedance as `R_FB_TOP.2↔U1.FB`. A keep-away on one path leaves the other branch exposed. | NetReq, Path (`paths.py:543-556`) | Path: **E8** |
| N16 | `clock` | which member of a group the others are matched to | **BUS** | it names a path of a group, so it is meaningless on one net | NetReq (spi) | NetReq: **E6** |
| N17 | `pf_max` | the I²C bus capacitance budget per line (UM10204 7.1) | **NET**: the charge stored on a node, C = Q/V, includes every pin and every stub | narrower: a path drops the stubs' and the other pins' capacitance (`constraints.py:2021-2038` already counts the pins of the whole net) | NetReq (i2c) | Path: today's `_NET_ONLY` text |
| N18 | `loop_mm2` | the area of a current loop | **LOOP**: a loop crosses two nets and two parts at least | wider/narrower: on one net it is ambiguous, which is why `paths.compile_loops` refuses a second Loop over the same pads (it did not until refutation round 3) | NetReq (power, switch_node), Loop | NetReq: **E7** |
| N19 | `rise_ps` | the fastest edge on the node | **NET**: an edge is dV/dt, and the node's voltage swings at every pad at once. Its consumer folds it across the board (`constraints.py:937`). | narrower (the driver pin): the pin is the source, but the stitch pitch needs the edge present in the copper of the cavity, which is the net's | NetReq | Path: today's `_NET_ONLY` text |
| N20 | `kind` | split, see §4 | net kind: what the node is. Path kind: what one branch does. | — | NetReq (10 kinds), Path (2) | **E5**, **E11**, today's `paths.py:357-366` |
| N21 | `class_name` | the name of the KiCad net class. Not physics. | NET (KiCad classes are per net) | — | NetReq | — |
| N22 | `autoroute` | the old router's switch. Not physics. Its `False` duplicates `vias`/`layers` and its `"diff_pair"` duplicates `Pair()`. | none | — | NetReq | NetReq: **E12** |

### 2.2 `Path` fields (`language.py:1983-2045`, `model.py:336-361`)

| # | property | subject | note |
|---|---|---|---|
| P1 | `amps` (number) | PATH | Enters at the first station and leaves at the last. This is today's meaning. |
| P2 | `amps` (`{station: A}`) | PATH | New (§5). These are the draws; each link carries the sum of the draws beyond it. |
| P3 | `temp_rise_c` | PATH | It is no longer gated on the net kind (`paths.py:382-385` today). |
| P4 | `vias`, `layers` | PATH | Defaults when not written: vias allowed at the stackup via; every copper layer that `Board(planes=)` does not give to a plane. On the five boards this equals today's `generic` default (`F.Cu`, `B.Cu`). |
| P5 | `vias_max`, `reference`, `z_se_ohm`, `length_mm` | PATH | New on Path. Today they are NetReq only (`language.py:2343`). |
| P6 | `max_mm` | **NET** (N9) | Refused on Path (**E9**). Today's `Path(max_mm=)` airwire budget becomes `length_mm=`: airwire at placement, routed length after. |
| P7 | `keep_clear_of`, `keep_clear_mm` | **NET-TO-NET** (N15) | Refused on Path (**E8**). |
| P8 | `kind` | PATH | `"feedback"` or `"sense"` only (§4.2). |

### 2.3 `Pair`, `Bus`, `Loop` (`language.py:1932-1981`, `2048-2059`)

| # | property | subject | note |
|---|---|---|---|
| G1 | `Pair(z_diff_ohm, match_mm, uncoupled_mm, gap_mm)` | PAIR | Unchanged. Gains `kind=` (the `usb_hs` preset, §4.3). |
| G2 | `Pair(layers, reference)` | PAIR | Applies to both of its paths, and `reference` is per layer (N14). |
| G3 | `Bus(match_mm, clock, length_mm)` | BUS | Members are paths. A member net with 3 or more pads must have exactly one Path, else **E13**. Gains `kind="spi"`. |
| G4 | `Loop(*pads, mm2=)` | LOOP | Unchanged. It is the only home of a loop budget. |

### 2.4 The other statements

| # | statement | subject: why | note |
|---|---|---|---|
| S1 | `Thermal(pad, watts, rise_c, across_planes, fill)` (`language.py:2226`) | **PIN**: the part's dissipation leaves through this pad's land. `rise_c` is the barrel's rise under that pad. | wider (net `GND`): false for every other GND pad. Narrower: none exists. Unchanged. |
| S2 | `Isolation(a, b, volts, slot, across, reinforced)` (`language.py:2132`) | **NET-TO-NET**, domain against domain: every net on side `a` against every net on side `b` at the barrier's working voltage (`constraints.py:2153-2231`) | Its `volts` is the voltage *across* the barrier, not a net's. Unchanged. |
| S3 | `Bridge(a, b, at, kind, why)` (`language.py:2167`) | **NET-TO-NET**: the one tie between two grounds | Unchanged. |
| S4 | `Guard(net, stitch_mm, ground)` (`language.py:2283`) | **NET-TO-NET**: a high-impedance victim node shielded by a ground node, all around its copper | narrower: a guard on one path leaves the node's other branches exposed. Unchanged. |
| S5 | `Power(name)`, `Ground(name)`, `Net(name)` (`language.py:144-156`, `model.py:427-434`) | **NET** identity. `Ground` is its domain's 0 V. | `Ground` is what makes `volts=` an error (**E3**). |
| S6 | `Board(size, padding)` | geometry | Not physics. |
| S7 | `Board(layers, stackup)` | **BOARD**, the medium: copper weight, dielectric and via plating, read by every formula | Not a requirement subject. |
| S8 | `Board(planes=[(net, layer)])` | **NET**: where a node's copper is a plane. It is also every path's reference (N14). | Unchanged. |
| S9 | `Keepout`, `Region`, `Place` | geometry | Not physics. `Region` is where `Isolation` draws its sides. |

### 2.5 Constants that act as requirements (`constraints.py:1032-1049`)

| constant | today | becomes |
|---|---|---|
| `POWER_DEFAULT_AMPS = 0.2` | the current every branch of a `power` net with no `amps=` is sized for (0.4 mm); also the step of the power floor (`constraints.py:1744-1746`) | the net default is **deleted**: no current is invented. The step is kept as `CURRENT_FLOOR_STEP_AMPS` (KD 1, KD 10) |
| `PRESETS["sense"\|"feedback"]` | net kinds that paths also read (`paths.py:496`, `515`, `530`, `597`) | `PATH_PRESETS` (KD 15) |
| `CURRENT_KINDS = ("power", "switch_node")` (`paths.py:49`) | "this net takes current", read by `_backwards` (`paths.py:443`) to find a load | "this net has a Path that writes `amps=`" (PR 1) |
| `KEEP_AWAY_KINDS = ("analog", "sense", "feedback")` (`constraints.py:1049`, read at `2290`) | the kinds the report lists as victims | `("analog",)` in PR 7 |
| `DEFAULT_LOOP_MM2` (power 6, switch_node 20) | net-kind loop budgets | LOOP defaults for the loops a net kind generates (KD 7) |
| `DEFAULT_MATCH_MM` (clock 2.0, usb_hs 0.5, spi 2.5, sense 1.0) | net-kind skew | Pair/Bus kind defaults. The `sense` entry becomes a `Pair` of two sense paths. |
| `USB_UNCOUPLED_MM`, `USB_Z_DIFF` | usb_hs net kind | the `usb_hs` Pair kind |
| `I2C_PF_MAX = 400` | i2c net kind | i2c net kind (unchanged) |
| `KEEP_CLEAR_DEFAULT_MM = 3` | NetReq and Path | NET-TO-NET default |
| `PAD_NECK_MAX_MM = 1.0` | a current path's neck at a pad | PATH (unchanged) |

---

## 3. The hard cases

### 3.1 Reference plane: path, per layer
The return current of a trace flows in the plane next to the layer the trace is on. On node, F.Cu returns in
In1.Cu (GND) and B.Cu in In2.Cu (3V3). So `reference` is a property of a path's copper on one layer.

When `reference=` is not written, it is derived per layer, as `_reference_for` already does
(`constraints.py:1158-1175`). Written on a path that may use several layers, it is true on one of them and
false on the rest (**E10**). On a `Pair` it applies to both paths. The net keeps the planes it owns
(`Board(planes=)`). Every other net only has paths over those planes.

### 3.2 Keep-away: net to net, written on the victim
Two coupling mechanisms matter, and they point to different subjects.

**Capacitive coupling** injects a charge proportional to the aggressor's dV/dt. The victim's impedance to
its source turns that charge into a voltage.
- Both quantities belong to nodes. At the frequencies these boards care about a node is electrically short,
  so its voltage and its impedance are the same at every pad.
- `FB` is high-impedance at every pad. `SW` swings at every pad.

**Magnetic coupling** induces a voltage around a loop in proportion to the aggressor loop's di/dt and the
victim loop's area. That is a `Loop()`, as in `net-path-physics.md` §3.2's shunt example.

A sense tap branched off a low-impedance rail is held at the rail's voltage through milliohms. Capacitive
pickup into it is shunted, so a capacitive keep-away on that branch protects nothing.

Hence: `NetReq(victim, keep_clear_of=aggressors, keep_clear_mm=)` stays, and `Path(keep_clear_of=)` goes
(**E8**). No board in `examples/` writes either form. The tests write `Path(keep_clear_of=)` 9 times
(`grep -rn "Path(.*keep_clear" tests`).

### 3.3 Impedance `z_se_ohm`: path
An impedance is copper width over a reference at a dielectric height. All three are properties of one piece
of copper (N4). A path that runs on two layers gets a width per layer from one target, the way current
widths do today (`net-path-physics.md` §2, width per layer).

The "current against impedance" check (`net-path-physics.md` §2) becomes a per-link check. It runs only when
the same path writes both `amps=` and `z_se_ohm=`.

### 3.4 `max_mm` and `length_mm`: two properties, two subjects
- `NetReq(max_mm=)` is the **span of the node**. It is the MST airwire of all its pads
  (`route_checks.py:363`), standing in for the area of copper that radiates (`switch_node`, 8 mm preset;
  buck writes 6) or collects charge (`analog`, 25 mm; DS2 writes 30).
- A path's own length is `length_mm`. Placement checks the station-to-station airwire against it, because
  the airwire is a lower bound on the copper. After routing, pcbc measures the routed copper between the
  stations. KiCad's per-net `length` rule is written only when the path names every pad, since KiCad sums
  the whole net.
- Today's `Path(max_mm=)` and the sense/feedback `airwire_mm` defaults (`constraints.py:1027-1028`)
  become `length_mm` defaults on those path kinds.

### 3.5 `vias`, `vias_max`, `layers`: path
A via is a piece of copper between two pads. Its harm is specific to one branch:
- its current capacity, which comes from current;
- the return path it moves, which comes from reference;
- the plane it perforates.

A net-wide `vias=False` forbids the 2 A path's via group in order to keep a feedback tap on F.Cu (N11).

The `analog`, `sense` and `feedback` net presets that set `vias=False`, `F.Cu` and 0.2 mm today
(`constraints.py:1016, 1027-1028`) have no stated physical source. They leave the net kinds. The path
kinds `sense`/`feedback` keep them as path defaults.

**Measured consequence:** `analog` nets (buck `FB`, node `T_DIV`/`T_OUT`, DS2 `AIN*`/`REF*_F`) route at
0.16 mm on any layer that is not a plane, with vias allowed. An author who wants otherwise writes a Path
with `layers=`/`vias=`.

### 3.6 `rise_ps`, `clock`, `pf_max`: what each is really about
- **`rise_ps` is the node's.** An edge is the node's voltage changing, and it is present at every pad at
  once. Its only consumer is the plane-stitch pitch, which folds it across the board to the fastest edge
  in a cavity (`constraints.py:937-950`). The number comes from the driver pin's datasheet. Moving it to
  the pin would be possible, but the stitch would then need to know which nets each pin drives, which is
  exactly what the net already is. NET.
- **`clock` is a group's.** It names the member of a `Bus` the others are matched to. Matching compares
  the propagation delay of paths. BUS.
- **`pf_max` is the node's.** UM10204's Cb is everything that stores charge on the line: every pin and
  every stub. `constraints.py:2021-2038` already counts the pins of the whole net. NET, and only on the
  `i2c` kind.

### 3.7 `loop_mm2`: loop
A loop is a cycle through at least two nets and two parts (`paths.py:795-812`). One net's budget is
ambiguous: one net can sit in several loops.

`NetReq(loop_mm2=)` is refused (**E7**). The loops a net kind generates keep their default budgets:
- `power`: the decap loop, IC supply pin to cap to cap ground to IC ground (`route_checks.py:1300`);
- `switch_node`: the hot loop (`route_checks.py:1373`).

A different budget means declaring the loop with `Loop(mm2=)`.

### 3.8 `temp_rise_c`: path
It is half of a current statement: IPC-2152 width = f(current, rise). Copper that carries nothing does not
rise. `Thermal(rise_c=)` is a different quantity: the barrel's rise under one pad (S1).

### 3.9 Creepage and clearance: the net's voltage, applied between two nets
`volts` belongs to the net (N1). A distance, however, is always between two pieces of copper. pcbc combines
the two nets' facts like this, as `ClearanceTable.between` does today (`constraints.py:2401-2450`):

- **clearance(a, b)** = max(kind number of a, kind number of b, row(Va), row(Vb), stackup floor).
  - row(V) is IPC-2221B 6-1 at V: B2 on outer layers, B1 when the net may only run inner.
  - Each net's voltage is taken against its domain's 0 V, and a net with no `volts=` counts as 0 V.
  - That bounds |Va − Vb| from above as long as volts are non-negative, which `_num` enforces
    (`language.py:1876`).
- **creepage(a, b)**: the same, using IEC 60664-1 F.5 at the larger voltage when that voltage is 60 V or
  more. KiCad's `creepage` rule measures along the surface.
- **Across a domain barrier** the voltage is `Isolation(volts=)`'s, not either net's (S2).

Pairwise |Va − Vb| (two 400 V nets closer than a 400 V row) is **rejected**. Undeclared nets and other
domains have unknown potentials, and there is no signed `volts`.

**Finding F1.** Two nets named by one `NetReq` at 60 V or more get no creepage between them:
- The KiCad rule is `A.hasNetclass(X) && !B.hasNetclass(X)` (`dru.py:139`).
- The router's table skips a same-class pair (`constraints.py:2436`).

So `NetReq("HV_P", "HV_N", volts=400)` writes 400 V creepage from each of them to every other net, and none
between the two of them, which may be 800 V apart. No board here is at 60 V (buck's VIN, 12 V, is the
highest). PR 4 writes creepage per pair of nets of one class.

### 3.10 `Thermal`: pin
`watts` is what one part dissipates. It leaves through one land, so the subject is the pad (S1). Unchanged.

### 3.11 `kind=`: see §4

---

## 4. Kinds and presets

Today a `NetReq` kind is one `Preset` (`constraints.py:998-1029`) that sets current, voltage, width, via,
layers, airwire, spacing and loop together. The redesign keeps each default with its subject.

### 4.1 What happens to each default

| today's kind | net kind keeps | moves to | disappears |
|---|---|---|---|
| `generic` (`digital`, `default`) | class floor clearance | width floor → every copper (manufacturability); layers F/B → path default "every non-plane layer" | — |
| `power` | clearance 0.20; spacing 3W; GND's 2-layer back pour (`route_scene.py:242`); generates decap loops at 6 mm² | `amps`, `temp_rise_c` → Path. Via 0.8/0.4 and the 0.4/0.25 mm floor → defaults of a path with a current (KD 10, KD 13). `loop_mm2` → Loop. | the net default current `POWER_DEFAULT_AMPS` and the 0.4 mm width of every unnamed branch |
| `switch_node` | clearance 0.20; spacing 3W; span `max_mm` 8; generates the hot loop at 20 mm²; switch-copper area measured (`route_verify.py:3123`); the 0.6/0.3 via of its current paths (KD 13) | `amps` → Path; `loop_mm2` → Loop | the 0.3 mm width (folds into KD 10); `autoroute=False` (PR 7) |
| `analog` | clearance 0.20; spacing 5W; span `max_mm` 25; accepts `keep_clear_of`; reported among keep-away kinds (`constraints.py:1049`) | — | width 0.20, via 0.6/0.3, `vias=False`, `F.Cu`, `autoroute=False` (§3.5) |
| `clock` | clearance 0.20; spacing 5W (an aggressor) | `match_mm` 2.0 → Bus; `vias_max` 2 → Path | width 0.15 (no source) |
| `i2c` | clearance 0.20; `pf_max` 400 pF (UM10204 7.1) | — | — |
| `usb_hs` | **not a net kind** | → **Pair kind** `usb_hs`: 90 Ω ±15 %, skew 0.5, uncoupled 2.0, `vias_max` 2, `length_mm` reported none until written | — |
| `spi` | **not a net kind** | → **Bus kind** `spi`: `clock` required, `match_mm` 2.5; `z_se_ohm`, `length_mm` → Path | clearance 0.20 (no SPI-specific source) |
| `sense` (net) | **not a net kind** | → Path kind `sense`; its `match_mm` 1.0 → a `Pair` of two sense paths | — |
| `feedback` (net) | **not a net kind** | → Path kind `feedback`. A feedback divider's own node is `analog`, as buck already writes (`buck.py:60`). | — |

### 4.2 Kinds after the redesign

**Net kinds** describe what the node is:

| kind | meaning |
|---|---|
| `generic` | nothing beyond the floor |
| `power` | a low-impedance DC rail. Its clearance follows its `volts`. Its decap loops are checked. On 2 layers, `GND` of this kind is the back pour. |
| `switch_node` | a node whose voltage swings at the switching edge. It is an aggressor, with a span and a hot loop. |
| `analog` | a high-impedance node, so a victim, with a span and keep-aways |
| `clock` | a periodic fast aggressor at 5W spacing |
| `i2c` | an open-drain line with a capacitance budget |

**Path kinds** describe what a branch does:

| kind | meaning |
|---|---|
| `feedback` | a regulator's divider tap, from the rail to the divider's top |
| `sense` | a measurement line |

Both carry a measurement, never a load. Defaults: 0.20 mm, no vias, F.Cu, `length_mm` 15 for feedback and
25 for sense. These are pcbc defaults, carried over unchanged.

**No `load` path kind and no `switch` path kind are added.**
- A load is a branch with `amps=`, and a kind would say the same thing twice.
- The switch node's copper area is a node property, owned by the net kind `switch_node`. Its current is the
  path's `amps=`, as buck already writes (`buck.py:62`).

**Pair kind:** `usb_hs`. **Bus kind:** `spi`.

---

## 5. A rail with many loads

**Choice: one Path in feed order, with each load's draw at its station.**

```python
Path("3V3", "U1.VOUT", "U2.VDD", "U3.VDD", "U4.VDD", "U5.VDD", "U6.VDD", "U7.VDD", "U8.VDD", "U9.VDD", "U10.VDD", "U11.VDD", amps={"U2.VDD": 0.30, "U3.VDD": 0.12, "U4.VDD": 0.12, "U5.VDD": 0.05, "U6.VDD": 0.05, "U7.VDD": 0.05, "U8.VDD": 0.02, "U9.VDD": 0.02, "U10.VDD": 0.01, "U11.VDD": 0.01})
```

(Illustrative parts and currents.)

**The physics.** Current enters the rail's copper at the source pin and leaves at each load's pin. By
Kirchhoff's current law, the copper between two stations carries the sum of every draw beyond it. The
compiled link currents are:

| link | U1.VOUT→U2 | U2→U3 | U3→U4 | U4→U5 | U5→U6 | U6→U7 | U7→U8 | U8→U9 | U9→U10 | U10→U11 |
|---|---|---|---|---|---|---|---|---|---|---|
| A | 0.75 | 0.45 | 0.33 | 0.21 | 0.16 | 0.11 | 0.06 | 0.04 | 0.02 | 0.01 |

Each link is then sized as a path with that current, on each allowed layer. The pcbc floor (KD 10) means
every link here at or above 0.2 A is 0.4 mm, and the rest are 0.25 mm.

**Why not currents at pins alone.** A draw is a fact about a pin, but a pin draw does not fix the
topology. Without the feed order, no link has a current until the router has chosen a tree, and the formula
stage sizes widths before routing (`direction.md` §5c: *"Design stays with the formulas"*).

The dict keeps the pin fact (keyed by the pin) and adds the order. Nothing is lost:
- The SPICE stage's B1 bench (`spice-stage.md` §5 B1) becomes one deck per draws-Path, with a current
  source from the first station to each drawing station.
- Its "off-path copper carries none" test is unchanged.

**Rules:**
- A number, `amps=2`, is the one-draw case: the last station draws it.
- Every key must be a station other than the first (**E11**), and every value must be greater than 0.
- A station with no key draws nothing at DC. Decoupling caps are such stations, or off-path pads that meet
  the path inside a station pad (junction rule 2, `net-path-physics.md` §3.2).
- **A tree** is two Paths sharing their first station. Their join carries the larger of the sums entering
  and leaving (`paths.py:568-596`), and the balance rule holds at every shared station (`paths.py:461`).

**On a poured rail** (PR 8). Node's `3V3` on In2.Cu feeds `U1`, `U4`, `U5` and `J_LOAD`:
- The pour is the copper between the stations, so the order is not read.
- Each station's joint to the pour (its via group) is sized for that station's own draw, and the source's
  joint for the sum.
- The pour itself stays `not rated: pour` (`direction.md` §5b): no plane copper rating is declared or
  sourced.
- Today this line is refused (`paths.py:335-341`).

## 6. What a branch no path names gets, and what is not protected

**It gets:**
- the class floor width, `max(0.16, track_min)` (`constraints.py:1258`) = 0.16 mm on both stackups;
- the stackup via, one per layer change: 0.5/0.3 mm on `jlcpcb_2l_1oz`, 0.35/0.2 mm on `jlcpcb_4l_1oz`;
- the net's clearance (§3.9);
- every layer not given to a plane.

What that copper carries at 10 °C (computed with frozen `stackup.track_amps`/`via_amps`):

| copper | carries at 10 °C |
|---|---|
| 0.16 mm trace | 0.633 A, on either stackup, with or without a plane |
| 2-layer via | 0.707 A |
| 4-layer via | 0.527 A |

The off-path report line (`net-path-physics.md` §3.1) keeps its shape, with the net's preset current
replaced by "no declared current":

```
5V: C_OUT2.1 is on no Path; its branch is 0.16 mm (class floor) and carries no declared current; a 0.16 mm branch carries 0.633 A on F.Cu at 10 C
```

**A current nobody declared, stage by stage.**
- The formula stage sizes declared currents only.
- The ampacity gate walks only paths with a declared current (`ampacity.py:279-333`, net rows deleted in
  PR 3).
- The SPICE stage, as `direction.md` decides it: *"a via or track is judged by the **solved** current"*
  (§5c), and it *"checks every piece of copper against what it can carry"* (§5b). Its deck is built from
  what `board.py` declares plus each part's vendor model, with *"no new inputs"* (§5c). Two cases follow
  from that:
  - A load whose part has a usable vendor model draws its own current in the solve. The branch to it is
    judged by that solved current whether or not a Path names it. This is the case direction.md
    describes.
  - A load whose part has no usable model draws nothing in the deck. Its branch carries zero in the solve.
    Per §5c the report names that part as unmodelled, and the requirement it could not check. No error
    is raised against the branch.

So an author who forgets a load on a model-less part gets 0.16 mm and a SPICE report line that names
the unmodelled part, not an error. That is weaker than direction.md's wording read alone. The design
here does not reinterpret it: it is §10's owner question O1.

**Protections that go with the net current, and are not replaced:**

| removed protection | where it lives today | what it covered |
|---|---|---|
| net per-layer width at the net's current | `constraints.py:1815-1821` | a branch nobody named was sized for the net's declared current |
| net via groups | `paths.py:264-267` | the same, for layer changes |
| net ampacity rows | `ampacity.py:331` | the gate's check that every branch could carry the net's current |
| `power` floor on unnamed branches | `POWER_DEFAULT_AMPS` | a 0.4 mm minimum on any branch of a power net |
| SPICE bench B2, "every branch" | `spice-stage.md` §5 | its owner question Q1 becomes moot |

A branch that really carries current, and that the author did not name, was protected by these at the net's
number. After PR 3 no formula stage protects it. Only the SPICE stage does, and only when the part at its end
has a model.

**Return currents in a poured ground** are declared on no board after migration.
- Buck: `GND` on B.Cu. c3_usb: `GND` on B.Cu. Node: `GND` on In1.Cu and `3V3` on In2.Cu.
- The pour's joints are single stackup vias. Today they are reported `not rated: pour` (report only,
  `ampacity.py:403-421`).
- After PR 3 even that report disappears: there is no declared current to rate against. PR 8 restores a
  real check for rails, through draws. A ground's return path stays undeclared (`net-path-physics.md` §10 A).

**AC current in decoupling branches** is not declared.
- Buck's input capacitors carry the input ripple. Iout·√(D(1−D)) at the file's D = 0.463 and 2 A gives
  0.997 A rms (computed here from `buck.py:55`; a textbook formula, not measured). That is above the
  0.633 A of a 0.16 mm branch.
- The buck migration below therefore makes `C_IN2.1` and `C_IN1.1` stations of the VIN path.
- How the ripple splits between `C_IN1` and `C_IN2` is AC, and is `not checked` (`spice-stage.md` §5.2).

**Until the SPICE stage exists, nothing checks how a declared current splits among parallel copper.** This
covers a trace in parallel with a pour, the barrels of a via group, and a path's current escaping into
off-path copper through a pour. Only pcbc's own lumped models stand in: `via_group_shares`, and the gate's
widest-path walk.

---

## 7. Error texts

All errors follow §7b: facts only, no edit proposed, and they name the line. Placeholders are in `<>`. The
examples use real board lines.

| id | when | text |
|---|---|---|
| E1 | `NetReq(amps=)` | `NetReq("VIN") line 56: amps= is a current; a current flows in the copper between two pads, and a NetReq states what holds at every pad of VIN (U1.VIN, J_IN.1, C_IN1.1, C_IN2.1, R_EN.1); amps= is a Path field` |
| E2 | `NetReq(temp_rise_c=)` | `NetReq("<net>") line <n>: temp_rise_c= is the rise of copper under a current, and a current is a Path's; temp_rise_c= is a Path field` |
| E3 | `volts=` on a NetReq naming a `Ground()` net | `NetReq("VCC", "GND") line 54: volts=3.3 holds for every net the line names, and GND is Ground("GND") (blinky.py line 4), the 0 V its domain's voltages are measured from` |
| E4 | `vias=`, `vias_max=`, `layers=`, `reference=`, `z_se_ohm=`, `length_mm=` on a NetReq | `NetReq("<net>") line <n>: <kw>= is about the copper between two pads (<clause>), and <net>'s pads are joined by copper the router lays link by link; <kw>= is a Path field`. Clauses: vias "a via is a piece of that copper"; vias_max "the vias that copper passes"; layers "the layers that copper runs on"; reference "the plane under that copper, one per layer"; z_se_ohm "an impedance is that copper's cross-section over its plane"; length_mm "a length runs between two pads" |
| E5 | `kind="usb_hs"`, `pair=`, `z_diff_ohm=`, `uncoupled_mm=` on a NetReq | `NetReq("USB_DP", "USB_DN") line 263: kind="usb_hs" is a Pair kind and pair=, z_diff_ohm= are Pair fields: they hold for two paths at once; a NetReq takes kind generic, power, switch_node, analog, clock or i2c` |
| E6 | `match_mm=`, `clock=`, `kind="spi"` on a NetReq | `NetReq("<nets>") line <n>: match_mm= compares the lengths of two or more paths; it is a Pair or Bus field` (`clock=`: "names the path a Bus is matched to; it is a Bus field") |
| E7 | `NetReq(loop_mm2=)` | `NetReq("SW") line 59: loop_mm2= budgets the area of a loop, which runs through two nets and two parts at least; it is a Loop field` |
| E8 | `Path(keep_clear_of=)` / `keep_clear_mm=` | `Path("5V") line 66: keep_clear_of= keeps a node's copper from another node's, and every branch of 5V is the one node 5V; it is a NetReq field` |
| E9 | `Path(max_mm=)` | `Path("<net>") line <n>: max_mm= is the span of all of <net>'s pads, a NetReq field; a Path's own length between its stations is its length_mm=` |
| E10 | `Path(reference=)` on a path allowed on several layers | `Path("<net>") line <n>: reference="In1.Cu" is the plane under F.Cu only, and this Path may run on F.Cu and B.Cu` |
| E11 | a bad key in `amps={...}` | `Path("3V3") line <n>: amps= names U12.VDD, which is not a station of this Path (U1.VOUT, U2.VDD, ...)`. Also: `... amps= names U1.VOUT, its first station, where the current enters; a station in amps= is one the current leaves by` |
| E12 | `NetReq(autoroute=)` | `NetReq("<net>") line <n>: autoroute= was the old router's switch, and nothing reads it as a requirement` (the shape of the `net_order` refusal, `language.py:224-232`) |
| E13 | a `Pair`/`Bus` member with 3 or more pads and not exactly one Path | `Pair line <n>: USB_DP has 4 pads (J1.A6, J1.B6, U3.1, U1.27) and <k> Paths name its stations, so the length the pair compares is not defined` |
| E14 | a net kind `sense`/`feedback` on a NetReq | `NetReq("<net>") line <n>: kind="sense" is a Path kind: it names a branch of a net that carries a measurement; a NetReq takes kind generic, power, switch_node, analog, clock or i2c` |

**Today's texts that the move makes false.** They are rewritten in the PR that moves their field, under
§7b's rule that an error is true:

| id | PR | site today | today's text says | after |
|---|---|---|---|---|
| E15 | 5, 6, 7 | `_NET_ONLY` (`language.py:2330-2342`), a non-path keyword on a `Path` | `z_diff_ohm`, `pair`, `match_mm`, `uncoupled_mm`: "belongs to two nets at once ... it is a NetReq field". `reference`: "is the plane under the whole net". `clock`: "names the net a whole bus is matched to". `pf_max`: "is a property of the whole bus". `autoroute`: "is the net's" | PR 5: `reference` leaves the table, because it is a Path field. PR 6: `Path("<net>") line <n>: <kw>= holds for two paths at once; it is a Pair field (Pair line <m>)`, with `match_mm` "a Pair or Bus field" and `clock` "names the path a Bus is matched to; it is a Bus field". `pf_max`: "is the charge budget of the whole node: every pin and stub on <net>" (N17). PR 7: `autoroute` takes E12's text |
| E16 | 5 | `_NOT_A_PATH_FIELD` (`language.py:2343`, text `2373-2377`) | "a via budget, a routed length and an impedance target are NetReq fields" | deleted: `vias_max`, `length_mm` and `z_se_ohm` become Path fields |
| E17 | 6 | `paths.py:320-326`, a field on a Path of a pair net | "a pair's numbers are on NetReq line N" | `Path("USB_DP") line <n>: <f>= on half of the pair USB_DN/USB_DP; the pair's numbers are on Pair line <m>, and a Path on a pair net names stations only` |
| E18 | 6 | `paths.py:357-366`, `Path(kind=<net kind>)` | `kind="usb_hs" is a net kind` | for `usb_hs`: `Path("<net>") line <n>: kind="usb_hs" is a Pair kind; on a Path, kind= names a sense branch and takes "feedback" or "sense"`. A real net kind keeps today's text |
| E19 | 6, 7 | `constraints.py:1340`, an unknown NetReq kind | lists `sorted(PRESETS)`, which includes usb_hs, spi, sense and feedback | lists the six net kinds and the aliases `digital` and `default`. A known Pair, Bus or Path kind written on a NetReq gets E5, E6 or E14 instead |
| E20 | 5, 6 | `constraints.py:1873-1893`, an impedance with no reference plane | "(NetReq line N)" | "(Path line N)" for `z_se_ohm`, and "(Pair line N)" for a pair. The two-layer text ("no NetReq names GND with kind power") stays: the back pour is a net-kind fact (`route_scene.py:242`) |
| E21 | 5, 6 | `route_checks.py:436-449`, `_unbudgeted_fast` | "preset <kind> sets no max_mm ... (NetReq line N)". The printed line proposes no edit. Its docstring (`418-433`) says "the edit is on the NetReq line" | `FAST_KINDS` (`route_checks.py:372`) keeps `clock` and `i2c`. `usb_hs` and `spi` are judged per Pair or Bus. The line names the subject that may carry a budget: `USB_DP: 20.7 mm of airwire (MST of 4 pads, edge to edge) and no budget checks it: no length_mm= on Path line 268 or Pair line 263`. The docstring's sentence is deleted |
| E22 | 5 | `copper.py:261-270`, `vias_on_no_via_nets` | "<net> has N via(s); NetReq vias=False" | `<net>: link <a>~<b> has N via(s), and Path line <n> says vias=False`. It is checked per link owner, not per net |
| E23 | 8 | new: a draws-Path joint on a pour | — | `Path("3V3") line <n>: J_LOAD.1 draws 1 A, and its joint to the 3V3 pour on In2.Cu is 1 via (0.35/0.2 mm; via_barrel 0.527 A at 10 C)` |

Where the errors are raised:
- E1, E2, E4–E7, E12 and E14 come from a `NetReq` keyword no longer in its signature. They arrive as a
  `TypeError`. `_explain_type_error` (`language.py:2294-2327`) routes them through a NetReq twin of
  `_explain_path_keyword` (`language.py:2346-2380`).
- E3 and E13 come from `compile_constraints`.
- E8–E11 come from `paths._path_refusals`.

Removed refusals:
- "amps= on a path needs its net to take a current" (`paths.py:373-381`).
- "temp_rise_c= is not taken by kind" (`paths.py:382-385`).
- "names every pad … its fields … are the net's" (`paths.py:351-354`). The `kind=` branch of it stays.

---

## 8. Migration: the five boards

Each old line gets the listed errors, and the after-lines compile. The currents are each file's own numbers.
Station orders follow each file's `Place()` lines.

### blinky (`examples/blinky/blinky.py`)

| line | before | errors | after |
|---|---|---|---|
| 54 | `NetReq("VCC", "GND", kind="power", volts=3.3, amps=0.05)` | E1; E3 (GND) | `NetReq("VCC", kind="power", volts=3.3)`<br>`NetReq("GND", kind="power")` |
| 55 | `NetReq("LED", kind="digital")` | — | unchanged |

`VCC` (`R1.1`) and `GND` (`D1.K`) each have one pad, so no copper joins them and no Path is needed. Copper
does not change. GND stays `power`, so the B.Cu pour stays (`route_scene.py:242`).

### buck (`examples/buck/buck.py`)

| line | before | errors | after |
|---|---|---|---|
| 56 | `NetReq("VIN", kind="power", volts=12, amps=1.36)` | E1 | `NetReq("VIN", kind="power", volts=12)`<br>`Path("VIN", "J_IN.1", "C_IN2.1", "C_IN1.1", "U1.VIN", amps=1.36)` |
| 57 | `NetReq("GND", kind="power", amps=1.53)` | E1 | `NetReq("GND", kind="power")` |
| 58 | `NetReq("5V", kind="power", volts=5)` | — | unchanged |
| 59 | `NetReq("SW", kind="switch_node", max_mm=6)` | — | unchanged (`max_mm` is the node span) |
| 60 | `NetReq("FB", kind="analog")` | — | unchanged |
| 62, 64, 66, 68 | the SW, 5V and feedback `Path` lines, and the `Loop` | — | unchanged |

Line 56: the VIN path.
- The caps are stations because their branches carry the input ripple (§6).
- `C_IN1` is next to `U1.VIN` because it is placed there first (`buck.py:74-75`). The input `Loop` (line 68)
  names it.
- `R_EN.1` (120 µA) is off-path and meets the path inside a station pad.

Line 57: the 1.53 A cannot be declared until PR 8, because GND is the B.Cu pour (`paths.py:335-341`).

Copper changes:

| branch | before | after |
|---|---|---|
| `C_OUT2.1` (5V), `R_EN.1` (VIN) | 0.4 mm (`POWER_DEFAULT_AMPS`) | 0.16 mm |
| `C_BOOT.2` (SW) | 0.3 mm | 0.16 mm |
| `FB` | 0.2 mm F.Cu, no vias | 0.16 mm, vias allowed |
| VIN path links | — | 0.459 mm on F.Cu over the pour (`current_width_mm(1.36, 10, jlcpcb_2l_1oz, 1.53)`). This is the width today's net-wide 1.36 A gives on F.Cu (`constraints.py:1815-1821`). |

### c3_usb (`examples/c3_usb/c3_usb.py`)

| line | before | errors | after |
|---|---|---|---|
| 263 | `NetReq("USB_DP", "USB_DN", kind="usb_hs", z_diff_ohm=90, pair=True)` | E5 | `Pair("USB_DP", "USB_DN", kind="usb_hs", z_diff_ohm=90)` |
| 264 | `NetReq("VBUS", "3V3", "GND", kind="power", volts=3.3, amps=0.5)` | E1; E3 (GND) | `NetReq("VBUS", kind="power", volts=5)`<br>`NetReq("3V3", kind="power", volts=3.3)`<br>`NetReq("GND", kind="power")`<br>`Path("VBUS", "J1.VBUS", "U2.VIN", amps=0.5)`<br>`Path("3V3", "U2.VOUT", "U1.3V3", amps=0.5)` |
| 265, 268, 269, 272 | digital NetReq, the USB Paths, `Thermal` | — | unchanged |

Line 263: the pair's paths are lines 268-269. The `usb_hs` Pair kind keeps the class name `USB` and the
±15 % tolerance. A bare `Pair` gives ±10 % today (`constraints.py:1930`).

Line 264:
- `VBUS` at `volts=5` is a correction this document makes from the schematic: USB VBUS, with 5.1 kΩ CC
  pull-downs and no PD. The tool cannot know it and raises nothing for `3.3`. Both voltages are on the
  same IPC row, so copper does not change.
- `J1.VBUS` is two pads (`A4B9`, `B4A9`), an end station joined as in `net-path-physics.md` §1.2.
- The links stay 0.4 mm (KD 10).
- The off-path branches move from 0.4 mm to 0.16 mm: `U3.VBUS`, `U2.EN`, `C_VBUS*`, `R_EN`, `R_BOOT`,
  `C_3V3*` and `C_MCU*`.
- GND (the B.Cu pour) is undeclared until PR 8.

### node (`examples/node/node.py`)

| line | before | errors | after |
|---|---|---|---|
| 143 | `NetReq("USB_DP", "USB_DN", kind="usb_hs", z_diff_ohm=90, pair=True)` | E5 | `Pair("USB_DP", "USB_DN", kind="usb_hs", z_diff_ohm=90)` |
| 144 | `NetReq("VBUS", "3V3", "GND", kind="power", volts=5, amps=1)` | E1; E3 (GND) | `NetReq("VBUS", kind="power", volts=5)`<br>`NetReq("3V3", kind="power", volts=3.3)`<br>`NetReq("GND", kind="power")`<br>`Path("VBUS", "J1.VBUS", "U2.VIN", amps=1)` |
| 145 | `NetReq("T_DIV", "T_OUT", kind="analog")` | — | unchanged (copper loses the analog width, via and layer defaults) |
| 146 | `NetReq("LOAD", kind="power", volts=3.3, amps=1)` | E1 | `NetReq("LOAD", volts=3.3)`<br>`Path("LOAD", "J_LOAD.2", "Q1.D", amps=1)` |
| 147, 148, 153 | the USB Paths, `Thermal` | — | unchanged |

Line 144:
- The old line gave `3V3` 5 V. No error is raised for that, and the after-line writes 3.3.
- `3V3` (In2.Cu) and `GND` (In1.Cu) are poured (`node.py:96`). Their 1 A cannot be declared until PR 8:
  3V3 would be `Path("3V3", "U2.VOUT", "J_LOAD.1", amps=1)` as joints.

Line 146:
- `LOAD` is the drain of the low-side switch `Q1`, and swings between 0 and 3.3 V. It was `power` only
  because a current needed that kind (`paths.py:373-381`).
- The Path names both of LOAD's pads. That is legal from PR 1; today it is refused (`paths.py:344-355`).
- The link stays 0.4 mm.
- LOAD's net facts change with its kind. Its clearance goes from 0.20 mm (preset power) to 0.18 mm. That
  is the class floor, "hole_clearance 0.25 - ring 0.075 + 0.005", above IPC-2221B B2's 0.1 mm at 3.3 V.
  Its class goes from `Power` to `Default`. Measured with `compile_constraints(load_board("node.py"))`
  on a frozen copy with line 146 rewritten. The copper of its one link does not change. Its 0.20 mm
  clearance had no source beyond the power preset. The Path names every pad, so KiCad keeps a
  `width_net_LOAD` rule (KD 12).

### DS2 Addon (owner's file; the tool cannot edit it)

| line | before | errors | after (for the owner to write) |
|---|---|---|---|
| 15 | `from pcbc import Board, Capacitor, Chain, Ground, ...` | today's: the import names `Chain`, which pcbc no longer exports | `from pcbc import Board, Capacitor, Ground, Net, NetReq, Path, Place, ...` |
| 95 | `NetReq("3V3", "GND", "VDDA", "VSS", kind="power", volts=3.3, amps=0.1)` | E1; E3 (GND, `ds2_addon.py:19`); E3 (VSS, `ds2_addon.py:22`) | `NetReq("3V3", "VDDA", kind="power", volts=3.3)`<br>`NetReq("GND", "VSS", kind="power")` |
| 96 | `NetReq("AIN0", "AIN1", "AIN2", "AIN3", "REFP_F", "REFN_F", kind="analog", max_mm=30)` | — | unchanged. `max_mm=30` is the span of each node, as its comment means. |
| 97 | `Chain("VDDA", "J2.1", "C4.1", "U1.12")` | today's: `Chain() is now Path(), with the same arguments` | `Path("VDDA", "J2.1", "C4.1", "U1.12")` |

Line 95: no Path is needed. 0.1 A is below the 0.633 A a 0.16 mm branch carries, so `3V3`/`VDDA` copper
moves from 0.25 mm (today's floor below 0.2 A) to 0.16 mm. `GND` stays the B.Cu pour.

Line 96: the AIN and REF nodes route at 0.16 mm on any non-plane layer, with vias allowed (§3.5).

The suite reads DS2 through the conftest helper that rewrites a temp copy (`net-path-physics.md` §1.3).
Each PR that adds an error DS2 hits extends that rewrite. One test pins each new error's text against the
real file, read only.

### The pipeline test vehicle (`tests/fixtures/buck_netreq/buck.py`)

`tests/conftest.py:160-185` hands every pipeline test this pre-migration buck in place of
`examples/buck`. Fourteen test files read it through `vehicle_examples()` or `BUCK_NETREQ`
(`grep -rln "vehicle_examples\|BUCK_NETREQ" tests`). Its line 55 is
`NetReq("VIN", "5V", "GND", kind="power", volts=12, amps=2)`. It gets E1 in PR 3 and E3 in PR 4.

| line | before | errors | after (PR 3) |
|---|---|---|---|
| 55 | `NetReq("VIN", "5V", "GND", kind="power", volts=12, amps=2)` | E1; E3 (GND) | `NetReq("VIN", kind="power", volts=12)`<br>`NetReq("5V", kind="power", volts=5)`<br>`NetReq("GND", kind="power")`<br>`Path("VIN", "J_IN.1", "C_IN2.1", "C_IN1.1", "U1.VIN", amps=2)`<br>`Path("5V", "L1.2", "C_OUT1.1", "J_OUT.1", amps=2)` |

It keeps no SW Path, which is what lets it differ from the example. Measured on today's code (§10.1):
this rewrite leaves **EN, SW and VIN unrouted**, where the vehicle as it is today leaves EN unrouted. The
vehicle stops completing, so PR 3 cannot simply rewrite it. §10's O2 asks the owner which vehicle the
pipeline tests drive. The numbers the vehicle pins move in PR 3 in every case: `test_examples_fab.py`'s
`copper_bar` ceilings, `OWNS` and `stitch_mm`, and `test_patterns.py:1153-1230`'s spine pins (KD 14).

**Test fixtures** move too. Counts from `grep -rn '<pattern>' tests` on the frozen copy:

| fixture | lines | files | error, PR |
|---|---|---|---|
| `NetReq(...amps=` | 51 | 22 | E1, PR 3 |
| `NetReq(...temp_rise_c=` | 3 | 1 | E2, PR 3 |
| `NetReq(...` naming `"GND"` with `volts=` | 31 | 19 | E3, PR 4 |
| `NetReq(...layers=` | 13 | 5 | E4, PR 5 |
| `NetReq(...vias=` | 5 | 4 | E4, PR 5 |
| `NetReq(...vias_max=` | 3 | 1 | E4, PR 5 |
| `NetReq(...length_mm=` | 3 | 2 | E4, PR 5 |
| `NetReq(...z_se_ohm=` | 9 | 4 | E4, PR 5 |
| `NetReq(...reference=` | 1 | 1 | E4, PR 5 |
| `Path(...max_mm` | 1 | 1 | E9, PR 5 |
| `kind="usb_hs"` | 12 | 6 | E5, PR 6 |
| `NetReq(...clock=` | 4 | 2 | E6, PR 6 |
| `NetReq(...kind="spi"` | 4 | 2 | E6, PR 6 |
| `NetReq(...loop_mm2` | 6 | 4 | E7, PR 7 |
| `Path(...keep_clear` | 9 | 3 | E8, PR 7 |
| `NetReq(...autoroute=` | 1 | 1 | E12, PR 7 |
| `NetReq(...kind="feedback"` | 6 | 5 | E14, PR 7 |
| `NetReq(...kind="sense"` | 2 | 2 | E14, PR 7 |

`kind="feedback"` and `kind="sense"` on a `Path` (16 and 9 more lines) stay legal. `tests/test_constraints.py:160`
pins the whole NetReq keyword list, so it changes in PR 3, 5, 6 and 7. Each PR migrates the fixtures its
errors hit.

---

## 9. PR plan

Ordered. Every PR must leave all five boards and the vehicle compiling, with DS2 compiled through the
test helper. Routing completion is a separate measurement. Each PR measures it per board against §10.1's
baseline, and lists in its description every net that newly fails, quoting the router's own error. "The
boards build" in revision 0 was never measured, and §10.1 shows that today no board completes routing.

### PR 1: a Path's current does not need its net's kind
- Remove the kind gate on `amps=`/`temp_rise_c=` (`paths.py:373-385`). Allow a fielded Path that names
  every pad (`paths.py:351-354`). The `kind=` branch of that refusal stays.
- `constraints._current_width` (`1723-1752`): the floor (KD 10) applies to every path with `amps=`,
  stepping at `CURRENT_FLOOR_STEP_AMPS`. This restructure is required, not optional. Its `else` branch
  compares `cur.value > preset.width_mm`, and `width_mm` is `None` for generic, i2c and spi
  (`constraints.py:1014-1024`), so removing the kind gate alone would raise a `TypeError`.
- `CURRENT_KINDS` (`paths.py:49`, read at `443`) becomes "the other net has a Path that writes `amps=`".
- Copper: no change on any example or test (KD 10 gives the evidence).
- Errors: removes three refusals.
- Boards: none.

### PR 2: draws
- `amps={station: A}`. Link currents come from KCL. Each link gets its own via group at its own current.
  Today there is one group per path (`paths.py:540`).
- Every reader of the single `p.amps` moves to its link's current:
  - `ampacity.py:373-391`: walk each link at its own current and need, so a narrower downstream link is
    not judged at the sum.
  - `paths._impedance` (`669-680`): each link's width against the impedance width.
  - `paths._series_notes` (`689-703`): the current of the link at the named station.
  - `_path_lines`' `biggest` (`607`): the largest link current.
  - `PathSpec.amps` (`598`): keeps the source's total. The draws are added beside it.
  - The balance rule (`paths.py:461`) and the joins (`568-596`).
- `route_scene.py:504` (junction rule 2) needs no change. It reads *which* paths write `amps=`, never the
  number.
- The report prints each link's current.
- Errors: E11.
- Boards: none.

### PR 3: current leaves the net
`NetReq` loses `amps` and `temp_rise_c`. Every consumer of a net current, and what it reads after:

| site | reads today | after PR 3 |
|---|---|---|
| `constraints.py:1815-1821` | net width per layer at the net's current | deleted: the net width is the class floor |
| `compile.py:125`, `143` | `CompiledNet.amps`, filled from the NetReq | field deleted |
| `copper.py:372-402` (first loop), `434` (`judged`) | `net.amps` | first loop deleted, `judged` empty. The second loop (path links) judges every declared current. The "no copper/plane for N A" line moves to it: a path with `amps=` ≥ 1 whose net has no copper |
| `paths.py:264-267` | net via groups | deleted |
| `ampacity.py:331-368` | net rows | deleted (path rows from PR 2 stay) |
| `route_verify.py:1500-1504` | a lone via on a net's own links (`c.via_group`, `c.current`) | the link's group. A net with no current has no group, so this check does not run |
| `route_verify.py:1977-2001`, `2017-2020` | via parallelism and its report text at the net current | per path link group |
| `route_cost.py:182-183`, `193` | `net_cost`'s `via_per_change` and `group` from the net current | 1 and `None` for a net. A link gets its own through `link_nc` |
| `route_native.py:1000` | `c.via_group` | `None` for a net. A path's group is in its link terminals |
| `patterns/stitch.py:1179`, `2181-2182` | the population and coverage target from the net current | the path links that carry a current, each at its own. A net with none is not in the population |
| `patterns/tap.py:95` | share = net amps / pads | the current of the path link that ends at the tapped pad, else 0 |
| `patterns/spine.py` (`248`, `285-296`, `304`) | the spine | deleted (KD 14). `tests/test_patterns.py:1153-1230` and the citation at `patterns/__init__.py:537` go with it |
| `paths.py:521` | dt falls back to `req.temp_rise_c` | 10 °C, today's NetReq default |
| `paths.py:534-536`, `563` | a field-less Path link takes the net's width and current | the class floor and no current. This changes behaviour: DS2's `Path("VDDA", ...)` carries the net's 0.1 A today and none after |
| `paths.py:649`, `703-722` (`_series_notes`) | `POWER_DEFAULT_AMPS` and the other net's current | the note fires when no Path on the other net through that pad carries the current. The `through` test at `paths.py:700` already computes this. Text: `<other> declares no current through <ref>.<pin>` |
| `constraints.py:2273-2274` | the report's via line from `c.current` | per path link |
| `dru.py:85-119` | class and per-layer `track_width` from net widths | the class floor. KiCad keeps `width_net_` only where one Path names every pad (KD 12). The report lists each path width KiCad does not check |

Also in PR 3:
- `POWER_DEFAULT_AMPS` is renamed `CURRENT_FLOOR_STEP_AMPS` (KD 1).
- The off-path report text changes (§6).
- `spice-stage.md`: drop B2 and Q1. B1 takes draws.
- The pipeline vehicle is replaced as the owner decides (O2). Its pins move: `test_examples_fab.py`'s
  `copper_bar` ceilings, `OWNS` and `stitch_mm`.

Errors: E1, E2. Boards: blinky 54, buck 56-57, c3_usb 264 (the `amps` part), node 144 and 146, the
vehicle, and DS2 through the helper rewrite.

### PR 4: Ground has no volts, and creepage between nets of one class (F1)
- Refuse `volts=` on a NetReq that names a `Ground()` net.
- Write creepage per pair of nets of one class at 60 V or more, in `dru.py:126-141` and in
  `ClearanceTable.between` (`constraints.py:2430-2440`).
- Errors: E3.
- Boards: blinky 54, c3_usb 264, node 144 (the `volts` split), the vehicle, and DS2 through the helper.

### PR 5: copper fields leave the net
- `NetReq` loses `vias`, `vias_max`, `layers`, `reference`, `z_se_ohm` and `length_mm`.
- `Path` gains `vias_max`, `reference` (per layer), `z_se_ohm` and `length_mm`.
- `Path(max_mm=)` is refused. The sense/feedback `airwire_mm` becomes `length_mm`.
- Net presets lose width, via and layers (§4.1). `PATH_PRESETS` is split out (KD 15).
- Consumers of the net's `vias`, `layers` and `vias_max`:
  - `route_native.constrained_nets` (`54-61`), and the relaxer at `route_relax.py:812`: a net is
    constrained when it is a Pair member, or when a Path that names every pad has `vias=False` or one
    layer. Any other path's constraint rides on its links. Net order changes on boards whose analog
    nets lose `vias=False`/`F.Cu`: buck `FB`, node `T_DIV`/`T_OUT`, DS2 `AIN*`/`REF*_F`. The PR
    measures this.
  - `fanout._excluded` (`fanout.py:35`): the same predicate.
  - `place_solve.needs_of` (`54-75`): the pull weight doubles when any link of the net may not change
    layer.
  - `copper.vias_on_no_via_nets` (`261-270`): checked per link owner (E22).
  - `dru.py:172-182`: `novia_`, `vias_` and `length_` per KD 12. The i2c length from `pf_max` stays per net.
  - Via size: `route_cost.link_nc` copies the link's via size, and `paths.py:537-540` builds the link's
    `ViaSpec` and group from the path's via (KD 13).
- `tests/test_route_relax.py:136` ("FB analog routes on one layer with no vias") is rewritten to the
  measured result.
- Errors: E4, E9, E10. Retexts: E15 (`reference`), E16, E20 (`z_se_ohm`), E21 (the `length_mm` part),
  E22.
- Boards: none of the five write these fields. Their analog copper changes, and the PR measures and
  records it.

### PR 6: Pair and Bus own their numbers
- Net kinds `usb_hs` and `spi` are refused. `pair`, `z_diff_ohm`, `uncoupled_mm`, `match_mm` and `clock`
  leave NetReq.
- `Pair(kind="usb_hs")` and `Bus(kind="spi")`. Members are paths.
- `FAST_KINDS` loses `usb_hs` and `spi` (E21).
- Errors: E5, E6, E13. Retexts: E15, E17, E18, E19, E20 (pair), E21.
- Boards: c3_usb 263, node 143.

### PR 7: loops and keep-aways on their subjects
- Refused: `NetReq(loop_mm2=)`, `Path(keep_clear_of=/keep_clear_mm=)`, net kinds `sense`/`feedback`, and
  `NetReq(autoroute=)`.
- `Preset.autoroute` is deleted. Its `False` is read by nothing that routes: `compile.py:129` fills
  `skip_autoroute_patterns`, which nothing reads, and `route_native.py:1184` says so.
- `KEEP_AWAY_KINDS` becomes `("analog",)`.
- Errors: E7, E8, E12, E14. Retexts: E15 (`autoroute`), E19.
- Boards: none of the five.

### PR 8: poured rails declare draws
- A draws-Path on a poured net sizes each station's joint (its via group, pad to pour) for its draw.
- The pour stays `not rated: pour`.
- This replaces the refusal at `paths.py:335-341` for draws. A return path on a ground stays refused.
- Errors: E23.
- Boards: node 3V3 (`Path("3V3", "U2.VOUT", "J_LOAD.1", amps=1)`). The c3_usb and buck rails are optional.

Each PR also:
- updates `docs/constraints.md` and `net-path-physics.md` §2 for its fields;
- pins each new or rewritten error's whole text in `tests/test_paths_compile.py`'s refusal table, or a
  NetReq twin;
- updates `tests/test_constraints.py:160` (the full NetReq keyword list) when it removes a keyword;
- mutation-tests every check it adds (`direction.md` §8).

## 10. Not established, and the owner's decisions

### 10.1 Routing, measured on today's code
These are scratch copies of the frozen tree, built with `cli.main(["build", ...])`, with each board's
requirement lines rewritten to §8's after-lines. The proxy is **not** PR 3. Today's code still sizes
unnamed power branches at 0.4 mm (`POWER_DEFAULT_AMPS`), where PR 3 uses 0.16 mm, and it still runs the
spine. Node's `LOAD` line is left as it is, because its after-line needs PR 1.

| board | unrouted, as shipped | unrouted with §8's Path lines | what failed |
|---|---|---|---|
| vehicle `buck_netreq` | EN | EN, SW, VIN | `VIN: C_IN2.1 -> C_IN1.1 (Path line 58) did not route at 0.781 mm on F.Cu, 1.09 mm on B.Cu`. The cells are held by off-path VIN copper under junction rule 2 |
| buck | SW | VIN | `VIN: C_IN2.1 -> C_IN1.1 (Path line 57) did not route at 0.459 mm on F.Cu, 0.541 mm on B.Cu: no layer change has room for its 2-via group`. SW now routes |
| buck, VIN as `Path("VIN", "J_IN.1", "U1.VIN", amps=1.36)` | SW | VIN | `J_IN.1 -> U1.3 did not route at 0.459 mm`. Dropping the cap stations does not help |
| c3_usb | VBUS | VBUS | the `J1.VBUS` station's two pads (`A4B9`, `B4A9`) do not join at 0.4 mm |
| node | VBUS | VBUS | the same, at `J1.VBUS` |

The conftest says the vehicle "still completes" (`tests/conftest.py:163-166`). On the frozen tree it does
not, because EN fails. The other workflow's edits in progress may be the cause, and this was not separated.

### 10.2 Decisions only the owner can make
- **O1. A load nobody declared, on a part with no model.** `direction.md` §5b/§5c say SPICE judges every
  piece of copper by its solved current. The solve sees only declared currents and the loads of parts
  with a usable vendor model (§6). *Recommendation:* accept it, and state it in direction.md §5c. The
  branch is reported as "unmodelled part, not checked", never as an error. The alternative is an error
  for every power pin of a model-less part that is not a drawing station of some Path. That would make
  the author declare every load, and it is a requirement direction.md has not asked for.
- **O2. The pipeline vehicle after PR 3.** Rewritten with Paths, it stops completing (§10.1). Options:
  (a) Paths, plus placement edits to the fixture until it completes; (b) the vehicle declares no current
  (three plain NetReqs), and the current-dependent pins move to the tests that already hold the example
  buck's physics; (c) a legacy mode that still accepts `NetReq(amps=)`. *Recommendation:* (b). The
  conftest says the vehicle drives the pipeline, "not to test buck's own physics". Option (c) breaks
  "the tool only works for that". (b)'s completion is measured in PR 3.
- **O3. KiCad stops checking path widths, vias and impedance** except on a Path that names every pad
  (KD 12). KiCad cannot select copper between two pads. Today it already checks only a pathed net's
  narrowest copper. *Recommendation:* accept. pcbc's copper gate and, later, SPICE check paths, and the
  report lists what KiCad did not check.
- **O4. The spine pattern.** Its population is empty after PR 3 (KD 14). *Recommendation:* delete it.
  Re-source it from draws-Paths only if a measured board needs a shared trunk.
- **O5. Junction rule 2 costs routing.** buck's VIN, the vehicle's VIN and the vehicle's SW stop routing
  once VIN's current is a Path (§10.1). The rule is `direction.md` §5b's, not this document's.
  *Recommendation:* keep the rule, and let PR 3 merge with the failures stated as errors (§7b). Placement
  is the fix, and that is the AI's job, not the compiler's.
- **O6. `direction.md` §5b's bullets disagree with this table.** They list "the reference plane, pairing"
  under Net, and "keep-away" under both Net and Path. This document puts `reference` on the Path per layer
  (N14), pairing on `Pair` (N5-N8), and keep-away on NET-TO-NET only (N15). The bullet added 2026-10-01
  points to this table, but the older bullets were not changed. *Recommendation:* rewrite §5b's two
  bullets to match §2. Direction.md wins until then, and nothing here is built.
- **O7. Two pcbc defaults with no source:** KD 10's 0.4/0.25 mm current floor, and KD 13's 0.8/0.4 and
  0.6/0.3 via sizes. Both are kept only so that declared copper does not move. *Recommendation:* keep
  both until the SPICE stage can judge copper by solved current. Then drop them to the IPC curve and the
  stackup via.

### 10.3 Not established
- **The analog copper change of PR 5 is unmeasured** (0.20 mm F.Cu with no vias → 0.16 mm, any layer). It
  may change DS2's route, which today is 38/39 (`net-path-physics.md` round 2).
- **PR 3's real routing.** §10.1 is a proxy. The 0.16 mm unnamed branches and the missing spine are
  measured only in PR 3.
- **E13 on today's boards** depends on the USB nets having exactly one Path. They do (c3_usb lines
  268-269, node lines 147-148). A fourth pad on the stub side was not judged here.
- **Draws on a poured net (PR 8)** assume a station's joint carries only its own draw. In a pour, current
  also enters a pin from its neighbours' joints, and only the SPICE stage could show by how much.

---

## Revision Summary

Revision 1, 2026-10-01, answers `review-code.md` (lens BUILDABLE). Each finding and what changed:

- **C1, the vehicle:** §8 gains the vehicle's before/after lines and the 14 test files that read it.
  Rewriting it with Paths was measured, and it stops completing (§10.1). The choice is the owner's (O2).
- **C2, net-current consumers:** PR 3 gains a table of every consumer with what it reads after. That
  includes `copper.py:372-402/434`, `compile.py:125/143`, `route_verify.py:1500-1504`, `route_cost`,
  `route_native:1000`, `stitch:2181`, `spine:304`, `paths.py:521/534-536/563/703-722` and
  `constraints.py:2273`. The VDDA behaviour change is stated.
- **C3, spine:** KD 14 deletes the spine in PR 3, with its tests. O4 is the owner's.
- **C4, `POWER_DEFAULT_AMPS`:** KD 1 deletes only the net default. The 0.2 A step stays as
  `CURRENT_FLOOR_STEP_AMPS` (KD 10, §2.5).
- **C5, `p.amps` readers:** PR 2 lists the ampacity walk, `_impedance`, `_series_notes`, `biggest`,
  `PathSpec.amps` and the per-link via group. Not done, with evidence: `route_scene.py:504` reads only
  which paths write `amps=`, so it needs no change.
- **C6, via, layer and via-size consumers:** PR 5 lists `constrained_nets`/relaxer, fanout, place_solve,
  `copper.vias_on_no_via_nets`, the DRU rules (the i2c length stays per net) and `test_route_relax.py:136`.
  KD 13 gives a current path its own via size, and `link_nc` copies it.
- **C7, KiCad rules:** KD 12 states which rules stay per net and why the others cannot be per path. O3
  is the owner's.
- **C8, errors:** E15–E22 rewrite today's texts that become false. E23 is PR 8's error. Correction: the
  printed `_unbudgeted_fast` line proposes no edit, and only its docstring does (`route_checks.py:418-449`).
  Its subject still becomes false, so it is E21.
- **C9, "boards keep building":** the claim is replaced by a compile gate plus a routing measurement per
  PR. §10.1 records today's baseline and a PR 3 proxy. No board completes today, and buck newly fails
  VIN. O5 is the owner's.
- **C10, fixture inventory:** §8's table is recounted on the frozen copy. Corrections to the review: the
  table counts NetReq `kind="feedback"`/`"sense"` only (6 and 2), and `temp_rise_c` splits 3 NetReq / 4
  Path. `test_constraints.py:160` is in each PR's checklist.
- **C11, PR 1 copper:** checked. No `switch_node` path or NetReq below 0.2 A exists in `examples/` or
  `tests/`, so the 0.3 → 0.25 mm change hits none (KD 10). The `TypeError` makes the restructure
  required, and PR 1 says so.
- **C12, kind-keyed readers:** `CURRENT_KINDS` (PR 1), `PATH_PRESETS` (KD 15), `Preset.autoroute`
  (read by nothing that routes, PR 7), `KEEP_AWAY_KINDS` (PR 7) and `spacing_w` (KD 16) each get a source.
  Node `LOAD`'s clearance change (0.20 → 0.18 mm, class Power → Default) is measured in §8.
- **C13, §6 and direction.md:** the "should be read" sentence is removed. §6 now follows direction.md:
  copper is judged by the solved current. It states what a model-less part leaves unchecked. O1 is the
  owner's.
- **Found while revising:** direction.md §5b's older bullets (reference and pairing on Net, keep-away on
  Path) disagree with §2 (O6). KD 13's via-size default has no source (O7).

## Owner decisions (2026-10-01): "Yes" to all seven

O1 accepted (an undeclared load on a part with no SPICE model is reported "unmodelled part, not checked");
O2 option (b) (the pipeline test vehicle declares no current; current tests move to the example buck);
O3 accepted (KiCad cannot select copper between two pads; pcbc's gate checks paths, SPICE later, and the
report lists what KiCad did not check); O4 delete the spine pattern; O5 keep junction rule 2 and let the
failures stand as errors (placement is the fix); O6 rewrite direction.md §5b's older bullets to match this
table; O7 keep the two unsourced defaults until SPICE judges copper by solved current.

## Implementation notes (2026-10-02): where the build differs from the plan

The eight PRs are implemented on the working tree (uncommitted). Where the code departs from the text above:

- **KD 13 moved to PR 3.** The current-link via (0.8/0.4, 0.6/0.3 on a switch node) landed with PR 3, because node's
  `LOAD` leaves kind power in that PR and would otherwise have changed via size.
- **Two NetReqs on one line.** The E3 migration of a `volts=` line that names a Ground net splits it into two
  statements on the same line (`NetReq("3V3", "VDDA", kind="power", volts=3.3); NetReq("GND", "VSS", kind="power")`),
  so every later line number of the board is kept. The same holds for c3_usb's import (`NetReq, Pair,` on one line)
  and node's 3V3 Path (appended after the last line, line 160).
- **Pair.** `Pair` gained `length_mm=` (each member Path's budget, G1); a pair's default layers are the outer ones.
  A preset pair is matched in the old projection only when its line writes `match_mm=`, as the usb_hs NetReq was;
  a bare Pair always. Classes keep board order (a Pair line takes its place among the NetReq lines).
- **A pair net's node facts are a NetReq's** (refutation one-home r1, defect 1; this replaces "a pair net takes no
  NetReq"). A NetReq that names both nets of a Pair, and no other net, with kind generic, gives them `volts=`,
  `max_mm=`, `rise_ps=`, `keep_clear_of=`/`keep_clear_mm=` and `class_name=`; the two nets compile with the Pair (its
  geometry, skew, layers, reference and vias) and cite the NetReq line for those facts. Refused, as facts: a NetReq
  naming one member only, one naming a Pair's nets with another net, and a net kind other than generic on it (the
  pair's kind is not a net kind, and downstream readers of the net kind would not see one). `pf_max=` stays the i2c
  kind's. The pair fixture's `max_mm=60` can come back as `NetReq("<p>", "<n>", max_mm=60)` (not re-added here).
- **`Preset.autoroute` is deleted, its routing fact is not.** §9 says its `False` is read by nothing that routes;
  since decision 2 (owner, 2026-10-01) `route_native.constrained_nets` read it to route switch_node and analog
  nets first. The fact is now the kind's (`constraints.SENSITIVE_KINDS`), so routing order is unchanged.
- **The path keep-away is gone, not only refused.** With E8, the link keep-away, the router's per-path keep
  rule (`JunctionRule.keep_away`), the placement check `route_checks._path_keep_away` and its helpers were deleted.
  When nets of one class differ in their keep-away (FB and SENSE both analog), the KiCad rule names the nets that
  wrote it rather than the class.
- **E23 is checked twice.** At compile, a through-hole station whose barrel is short of its draw is refused (it
  cannot take a second hole). On the emitted board, the build gate counts each SMD station's `pcbc:tap:<pad>` vias
  against its draw ("a joint to a pour carries less than its station draws"). The tap pattern places
  `ceil(A / via_amps)` stackup vias on such a pad (node's `U2.VOUT`: 2). Node's `J_LOAD.1` is through-hole
  (`CONN-TH_B2B-XH-A`, 1.0 mm drill, 1.693 A), so §7's E23 example ("1 via") does not arise on node; the text
  form is the one in §7. A copper field (`layers=`, `vias=`, ...) on a current Path on a pour is refused: the
  copper between its stations is the pour.
- **Lost repro.** The r2_b1 board no longer reproduces the junction-rule wall message (SW routes after PR 3);
  the two tests that pinned it now assert SW routes.
- **Found in PR 3, fixed after PR 8: patterns drew current links at the class floor.** Once the current left the
  net, the class width became the 0.16 mm floor, and `hop` and `chain` (which write at the class width with no
  link of their own) drew node's 1 A `LOAD` link at 0.16 mm (`power_bottlenecks`: 0.633 A of 1 A). A net whose
  Path carries a field is now the router's (`hop.specs`, `chain.specs`); node's LOAD is 0.4 mm, 1.231 A.
- **The pipeline gates no longer see a net-level `vias=False`.** No net kind forbids vias since PR 5, so
  `patterns._no_banned_vias` and `verify_copper`'s "a via on a net whose NetReq forbids vias" cannot fire; they
  are kept as backstops on `Constraint.via.allowed`. A Path's `vias=False` is judged per link (E22).

## Refutation round 1 fixes (2026-10-02)

The one-home, physics and migration reviews of the eight PRs found nine defects; each is fixed on the working tree
(uncommitted) with a test, and each new check was mutation-tested. Where the code now differs from the text above:

- **Pair takes `vias=` and `vias_max=`** (G2 grows): a Path on a pair net names stations only (E17), so the pair's via
  fields are the Pair's. A Pair with no kind is compiled from its own line (no via budget; its layers, spacing and
  budget lines cite the Pair line or "a pair's default"), never as `usb_hs`. E17 now states the real reason for
  `z_se_ohm=` (the members are sized for the pair's z_diff) and for `amps=`/`temp_rise_c=` (version 1 does not size or
  check a current on a coupled pair; a DC current on a data pair, as in PoE, is not built). A node or current keyword
  written on `Pair()` names its home.
- **E10 holds on a Pair** as on a Path: `Pair(reference=)` must be the plane under its one layer.
- **A net is timed by one Pair or one Bus.** A Bus over a Pair's nets, and two Buses sharing a net, are refused.
- **A Bus owns each member's `length_mm=`** (KD 8): a member's Path writes none (E17's Bus twin). Still as before:
  `Bus(length_mm=)` compiles to a per-net KiCad length (which sums stubs), not to each member Path's budget as
  `Pair(length_mm=)` does.
- **A path kind on every pad** (departs from §7 "the kind= branch of it stays"): a `Path(kind=)` that names every pad
  of its net is the whole node's, so a two-pad measurement line (thermistor to ADC) has a home; `NetReq(kind="sense")`
  stays E14.
- **Balance at a middle station** (§5 "every shared station"): a Path enters each of its stations after the first and
  brings there only its draw there, so a Path leaving another Path's middle station with more than that draw is
  refused (the first link was sized for the entering Path's own draws only).
- **Multi-via taps (PR 8)** take one site per exit side, and two vias only in opposite directions (two stubs at a
  right angle out of one pad centre are a 90 degree corner); a joint left short is stated by the E23 gate.
- **DS2's `REFN_F`** completes since PR 5 because the analog kind lost `vias=False`/`F.Cu` (§3.5): on the routed board
  it is 0.16 mm, 7.41 mm on F.Cu and 11.89 mm on B.Cu (the layer that holds the board's only zone, GND) through 2 vias
  at (35.86, 11.476) and (25.446, 15.032). The other five analog nets of line 96 route on F.Cu only, with no via, at
  0.16 mm (the preset was 0.20 mm F.Cu, no vias, before PR 5). The old copper (no vias, F.Cu only) can now be stated
  only on a Path for REFN_F; before PR 5, with it, REFN_F did not route ("C1.1 (37.75,9.85) cannot reach U1.8
  (24.78,15.07) on F.Cu without vias", gate build). Whether to state it is the owner's decision.


### Refutation round 2 fixes (postfix, 2026-10-02)

The round-2 reviews of the subjects work and of the decision-1 to 4 work left 22 major defects; 21 are fixed on the
working tree (uncommitted) with a test each, every fix mutation-tested; one (`pcbc build` and a constructor's unknown
keyword) was fixed on `Board()` only; round 3 found it on every other statement (below). Where the code now differs from the text above:

- **Two spellings of one land are one land**: a pin name and its pad number are compared by the pads they resolve to.
  (Round 3: `Thermal("U1.GND")` is 22 pads and is refused on its own; a thermal land is one pad.)
- **`reference=` on two layers**: `"B.Cu"` names the B.Cu pour (the plane list prints it so); a reference no pour takes
  is refused on a Pair on any layer count, as on a Path. An impedance on the pour's own layer says that.
- **A two-layer `Board(planes=)` is the compiler's pour** (it was only the router's): its current Paths get joints and
  E23, and a sense Path on it is refused. A poured-net refusal names `Board(planes=)` when that is why the net is poured.
- **A station two current Paths share on a pour** is one joint sized for the sum of their currents.
- **Declared links that close a loop are refused** (the router laid one route between joined copper, and the other
  link's current ran in copper sized for less).
- **A current into a part with more than two pads** names the pour joint of its other pads that nothing checks (node's
  Q1.S takes up to LOAD's 1 A into the GND pour through a 0.16 mm stub and one via).
- **Each copper layer is sized and rated on its own copper**: an inner layer of a JLC four-layer stackup is 0.0152 mm
  (0.434 oz) on the IPC-2221 internal curve (4.681 mm for 2 A at 10 C, where 0.4 mm was compiled on 2 oz, 0.781 mm on
  1 oz); via-group landings use the thinnest of the link's layers.
- **The via-group gate rates the drill on the board**, not the planned one.
- **A generated plane pour's `min_thickness`** is the stackup's `pour_min_mm`: 0.1 mm on 1 oz (JLC 0.10/0.09), the 2 oz
  track minimum on 2 oz (0.16 two-layer, 0.15 four-layer); `group_pitch` keeps the same gap between antipads.
- **Errors**: a junction names both pieces of copper and where; a station of the same Path is not "off-path copper";
  R-X4 is not reported for an unrouted net (its copper is withheld); a link the pad-neck limit stops states the limit
  and the room each exit has; three refusals lost their proposed edit (`tenting=`, a `pcbc:` group name, a schematic
  attach).
- **Placement**: a `Place(to=)` part keeps out of the corridors of every target pad the to= does not name; when no spot
  is clear, the note names the corridor the fallback pose sits in.


### Refutation round 3 fixes (postfix, 2026-10-02)

The round-3 reviews left 12 major defects; all are fixed on the working tree (uncommitted), each with a test and a
mutation the test catches. Where the code now differs from the text above:

- **A station is its pads, not its spelling.** `J1.VBUS` (pads A4B9, B4A9) and `J1.A4B9` are one station: on a pour their
  joints are one, each pad's share added, every pad given the vias its worst pad needs, and the gate reads the worst
  pad; the balance rule groups stations the same way (`U3.I/O1` and `U3.1`).
- **A Path with no `amps=` entering a station brings 0 A** to the balance rule (it was skipped, so a field-less
  class-width feeder carried a declared 2 A unrated).
- **One land, one array; one loop, one budget; one net, one guard.** `Thermal()` on a pin of several pads is refused;
  a second `Loop()` over the same pads (any rotation, direction or spelling) is refused; a second `Guard()` on a net is
  refused.
- **A two-layer declared F.Cu pour** is the plane over B.Cu for `reference=`, `z_se_ohm=` and a Pair (named "the F.Cu
  pour"); the GND-kind sentence is no longer printed for a board that pours.
- **`Bus(length_mm=)`** is each member Path's budget, as `Pair(length_mm=)` is; the net carries it only for a member no
  Path names (two pads). KiCad gets a per-net length rule only when the member's Path is the whole node.
- **A lone via** on a link with a current is rated by its board drill even when the compiler planned no group.
- **The board carries pcbc's stackup** (`(setup (stackup ...))`, `(general (thickness))`), so the Gerber job file states
  the copper pcbc sized for (2 oz: 0.07 mm), and FAB_NOTES has an order line for the copper.
- **A via group's landing copper has one home**, `Stackup.landing_oz` (the thinnest landing layer): compiler, router,
  path gate, ampacity gate and census.
- **Every pcbc statement's unknown keyword** is stated with its line and the keywords it takes (Capacitor, Resistor,
  Power, Place, ...), never Python's traceback and "Did you mean".
