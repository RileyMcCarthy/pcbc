# Direction

The decisions that govern pcbc's next stretch, in the order they were made, each with the reason and
the evidence. Written 2026-09-23 from the owner's conversation with the tool's authors that day. When
another document disagrees with this one, this one wins; the others are listed under "Superseded" at
the end so nobody has to guess.

Read this before `docs/roadmap.md`, `docs/fp-router-plan.md`, `docs/layout-properties.md` or
`docs/tscircuit-notes.md`.

---

## 1. Three files, and what each one is

| File | Who writes it | What it holds |
|---|---|---|
| `board.py` | the AI | The circuit and its requirements: parts, pins, nets, `NetReq` (current, voltage, impedance, pair, length, isolation, planes), the stackup, the outline, and every declared `Place()`. |
| `layout.core.py` | the AI | Locks. Copper and drawings the AI refuses to let the generator move. Partial by design: most boards will have a short one or none. |
| `layout.gen.py` | the generator, every build | One Python object per KiCad board primitive — segment, arc, via, zone, footprint pose, `gr_*` — with **every field explicit**. Regenerated every build. Never edited. |

The final `.kicad_pcb` is `emit(merge(core, gen))`. KiCad fills the pours, runs DRC and exports the
fab package from that one file.

The owner's words, which fixed this: *"we keep auto layout and auto router creating python for gen
and have it output kicad as final step."*

**The generator writes Python natively.** Owner, 2026-09-25: *"I want the auto layout and placement to
generate directly python natively. That is why we need to build our own tool."* Placement and routing
read the Python model (parts from their `.kicad_mod`, `board.py`, `layout.core.py`) and write `Place`,
`Seg`, `Arc`, `Via`, `Pour` objects into `layout.gen.py`. **No KiCad file is an intermediate in
generation**: the first `.kicad_pcb` is the one `emit` writes. Running an external router and
decompiling its board into Python (what step 1 built, 2026-09-23..25) is a bridge, not the design; the
decompiler stays only as an importer and a test tool.

## 2. Sources of truth: exactly two

`board.py` and `layout.core.py`. Everything else — `layout.gen.py`, the `.kicad_sch`, the
`.kicad_pcb`, Gerbers — is build output. The rules that keep it that way:

- **Never edited.** A fix always lands in `board.py` or `layout.core.py` and the outputs come out new.
- **Never read back as design input, and every output the build does read is stamped.** The route
  stage reads the placed board; the build records its sha so a hand edit is stale, never shipped.
  Same for the seed board, the `.kicad_pro`/`.kicad_dru` sidecars every stage reads, gen, the routed
  board and the fab package: **if a stage reads a file the build wrote, that file is stamped.**
  No stage may take a fact out of the `.kicad_pcb` or out of
  `layout.gen.py` to decide anything for this build. Checkers read the `.kicad_pcb`; fab reads it;
  nothing else does. (Today's route stage still chains the router's own `.kicad_pcb` files internally.
  That goes away when the in-house router replaces KRT; until then it is contained inside the route
  step; the emitted board's copper and drawings come only from Python, and its footprints,
  outline and setup come from the placed board, which is itself a stamped output.)
- **One writer of design content.** `emit` writes the final board from the two sources. Two later
  rewrites of the same file carry derived data only — KiCad's zone refill-and-save, and the uuid
  re-pin — and the build checks afterwards, field for field, that every layout object in the file
  is still the Python object, so neither can smuggle a design fact in. (Measured 2026-09-24: three
  writers, four writes.)
- **One home per fact.** Defaults chosen 2026-09-23, each reversible by changing one refusal:
  - Placement lives in `board.py`. A `Place()` in `layout.core.py` is refused, naming `board.py`. A
    `Place` in `layout.gen.py` is fine: that is the generator's output.
  - The outline lives in `board.py` (`Board(width, height)` today). `Edge.Cuts` drawings in core are
    refused. If cutouts are needed, `Board()` grows a field for them.
  - Nets live in `board.py` only. Core copper names an existing net or says `net=None` (KiCad's
    no-net, for a floating spreader, a coupon, a rule area). A net `board.py` does not have is
    refused at load, naming the line. Splitting `GND`/`AGND` is a circuit decision and goes in
    `board.py`. Owner's words: *"Does it make sense for it be able to invent nets?"* — no.
  - `layout.gen.py` is written under `layout/<name>/`, not next to `board.py`, so it never lands in
    a source tree or in the owner's real board checkout.

## 3. KiCad stays: backend and arbiter, never a source

The owner asked whether to drop KiCad and have Python write the fab files directly, to reduce the
number of places truth lives. Decision: keep it.

- **It is the only check we did not write.** The worst defects in this project have been our own
  checks grading the wrong thing: a pair's skew improved while the pair was destroyed; an orphan
  guard whose slack grew with how well the pass worked; a build reporting `copper: verified` on a
  board with no copper. If Python writes the Gerbers and Python checks them, nothing independent
  looks at the board before JLCPCB does.
- **It already does the hard geometry**: zone fill (clearances, thermal spokes, island removal),
  every footprint's pad shapes and mask openings, silk text in a real font, the 3D model.
- **tscircuit ran the other experiment** (`docs/tscircuit-notes.md` §4): ~30 in-house checks, a WASM
  pour solver, a Gerber writer, a second short-checker because the first missed shorts — and the
  fab-fatal bugs that still shipped are the ones KiCad catches (dropped via drills, dropped inner
  layers, an outline the fab could not read). When their own checkers disagreed, their contributors
  exported to KiCad and ran `kicad-cli pcb drc`.

The `.kicad_pcb` is **the thing that gets checked, not a source of truth**: information flows into
it from the two sources and out of it only as a verdict. DRC runs on it because it is exactly what
becomes the Gerbers; a check on the Python model would let an emitter bug ship. Zone fills exist only
in the KiCad file, but they are derived from fields Python holds, so checks like "is this plane one
island" run there and that is fine.

## 4. The layout Python is a direct image of the KiCad primitives

Owner's words: *"make sure the layout python is directly migrated to kicad primitives."*

- One Python object = one KiCad primitive, field for field. No composite objects, no primitive
  split across objects, no intermediate format (not `copper.json`, not a router's own model).
- Every field KiCad 10 writes has a Python field. The bar is **zero unmapped**, measured by a census
  over the five boards and the probe fixtures, pinned by a test. Python names may stay
  (`Seg`/`Pour`/`DrawArc`) but the 1:1 table lives in `docs/layout-properties.md`.
- Lossless both ways: KiCad → Python → KiCad gives the same primitive; the emitted board decompiles
  back to `layout.gen.py` byte for byte.
- `layout.gen.py` writes every field. A hand-written `layout.core.py` line may omit values
  `constraints.py` derives (width, gap, drill, clearance); a value below the compiled minimum is
  refused, never silently widened — tscircuit's `TraceWidthSolver` is the cautionary example.

## 5. Routing: in-house, deterministic, driven by the net's requirements

- **KRT is deleted, now, and the build is native only.** Owner, 2026-09-25: *"Do delete today's path. Use
  native only."* No `Board(router="krt")` fallback, no parity gate. Until the native router can finish a
  board, the nets it cannot route are absent from `layout.gen.py` and printed as moves; the example boards
  and the DS2 Addon may not complete for a while, and that is accepted. (Superseded plan, kept for the
  reasoning:) KRT was to go once the in-house router reached parity — as a **whole-board switch**
  (`Board(router=...)`), never class by class. Reason, measured: pcbc passes `--keep-input-copper`
  on every KRT step, so `!NET` is a target-list filter and cannot remove copper's presence; the
  safe-mixing rule the tree already records (R-S1) is "safe iff no later router step is left
  contesting the space you took". Five pcbc pattern classes mix safely today because they obey it;
  `chain` and wide `spine` broke ds2 because they did not.
- **The relaxer is not a repair pass, it is the embedder.** Its string-pull is the second half of
  the router. It stops running last on someone else's copper.
- **The corridor / gate-word "phase A" is not the plan.** The proof run (2026-09-22/23) showed node's
  38.97 mm elbow and the 30.62 mm good path cross the *identical* gate word — phase A cannot tell them
  apart; the defect is a shape choice inside a wide-open corridor. The spike that built the corridor
  did worst of three (failed the elbow, illegal copper). `docs/fp-router-plan.md` §3 is superseded
  on this point.
- **What replaces it: a cost field made only of derived numbers.**
  `cost = length + Σ pitch·tan(θ/2) + vias × via_price`, with `pitch = width + clearance` from
  `constraints.py`. It prices the disputed corner at 0.41 mm against the 8.35 mm it bought. The
  cost-field spike routed **buck 21/21, 0 DRC errors, 15.36 deg/mm** — better than KRT+relaxer
  (18.32) and the hand-routed reference (17.2) — with neither KRT nor the relaxer.
- **Pairs are routed as one object**, never as two nets. Every spike that routed a pair half alone
  beat the length and destroyed the coupling (99.5 % → 59.7 %). Embedded as one object the same path
  saved 16.56 mm and held coupling at 99.4 %. Before pairs are owned, **coupled length becomes a
  first-class verified number**: every KiCad pair metric improves when a pair stops being one.
- **The hard part is the dense connector.** On node the cost-field spike completed 105/110 links;
  4 of the 5 failures are at the USB-C connector. That is what KRT's special-case rescues exist for,
  and where the AI's locks earn their keep.
- **Status: the spike results are unverified.** The adversarial verifiers never ran (usage limit).
  Nothing is built from them until they do.

## 6. Placement: declared is authoritative, undeclared is solved

- A `Place()` the AI wrote is never moved by the tool. Greedy, file order, refuse with a move —
  unchanged.
- Parts with no `Place()` are placed by the generator, and the objective is **the router itself,
  not a proxy** (judged 3–0). Measured: the channel map costs 12–70 ms per candidate and the search
  0.08–0.6 ms, so the router is a <2 % surcharge on a map any proxy also needs; an incremental map
  update runs in **2 ms median** on node with 0 mismatched gates in 81,907. This reverses the
  earlier "keep it greedy and fail" for undeclared parts only.
- First real test case, already measured: the shipped `rotate=90` on node and c3_usb is the worst of
  four rotations for the USB pair; `rotate=0` halves c3_usb's pair (20.7 → 9.3 mm) at the price of
  eight other moves. The machinery prices every alternative; nothing asks.

## 7. The AI is the driver; the generator does the bulk

The loop the tool exists for:

1. The AI writes `board.py`.
2. `pcbc build` places, routes, writes `layout.gen.py`, emits, and either passes every gate or
   prints **moves** — each a file:line edit to `board.py` or `layout.core.py`, never "look at the
   picture".
3. The AI reads `layout.gen.py` and the moves. Where judgement is needed — the connector escape, which
   way a pair goes, the micro's rotation — it copies a gen line into `layout.core.py` (or edits
   `board.py`) and rebuilds. The generator fills in around the locks.

The measurements say this split is right: the rule-based router handled the bulk of buck and failed
exactly where judgement is needed.

## 8. How anything gets believed

The recurring defect in this project is not geometry, it is instrumentation: a check that measures
something adjacent to what it claims. Standing rules:

- Only a KiCad-backed check earns the word `verified`.
- Every gate judges the **emitted** board. Proven by mutation: corrupt one object in
  `layout.gen.py` and the build must fail, naming the line and field.
- Every number carries the command that produced it. A moved recorded number needs its measurement
  and its sentence.
- Nothing built from a spike or a panel is believed until independent verifiers have tried to refute
  it with their own comparators, not the implementer's.
- Determinism byte for byte, including from two different build paths.
- A serialized routing-problem file is written next to every refusal (borrowed from tscircuit), so a
  failure becomes a fixture in one command.

## 9. Where things stand (2026-09-25)

**Step 1 finished and verified (bridge):** KRT still routes; its board is decompiled to `layout.gen.py`
and the final board is emitted from that Python. Seven adversarial rounds; 1011 tests green under
KiCad+KRT (independently re-run). Mechanisms that carry over to the native generator unchanged: every
KiCad field as a Python field, the emitter, load-time validation of core lines by a KiCad round trip,
stamps built by tracing what each stage opens, every gate on the emitted board, fab equality with exact
fills. Open small fixes: a timeout on every kicad-cli call (a 1e20 text angle hangs DRC), refuse a
rotated text box (crashes KiCad), classify "written by this stage" by what the stage wrote not by mtime,
stamp KRT by a hash of its source, check the fill the Gerber export makes. Open owner question: may
`board.py` / `layout.core.py` do I/O beyond importing sibling modules (recommended: no).

**Next: the native generator** — placement and routing that read the Python model and write Python
objects, with the cost-field router (§5) as its starting point.

### Earlier status (2026-09-23)

**Built and independently checked:** step 1 of §1 — route as before, decompile to `layout.gen.py`,
emit from Python, every gate on the emitted board, Seg/Via locks working, byte-identical builds from
different paths, 834 tests green under KiCad+KRT.

**Open, each with a reproduction, fix round running:** fab's ampacity gate still reading the router's
sidecar; core able to invent nets; the fab copy of the board losing Python uuids; two unmapped zone
fields (per-layer hatch offset, arcs inside polygon outlines); a pour lock on the plane net not
holding against KRT's pour step; a refused route leaving the build stuck until `--force`; a dangling
core via not traced to its line.

**Next:** verify the three router spikes adversarially; then step 2 — the generator becomes the
router, KRT swapped out underneath without changing the file format; then solved placement.

## Superseded

- `docs/fp-router-plan.md` §3 (corridor phase A) and its stage list built on it — see §5. Its §2
  (what pcbc does today, measured) and §6 open problems remain useful; §6.1's "29 %" has no source
  (measured 11.4 %, 6.6 % with the per-face test) and §6.2's ds2 figure does not reproduce (buck
  13/21 at 44.2 unrelaxed, ds2 37/51 at 30.1; relaxed, both beat the shipped boards).
- `docs/roadmap.md` item order — the layout language and the generator come before hierarchy and
  capability limits.
- Grok's session plan (`~/.grok/sessions/.../01a09872-.../plan.md`, 2026-09-22): its three-file model
  is adopted; its "out of scope: reusing route_geom / route_scene / route_relax / patterns" is
  rejected (the owner asked for the opposite, and those modules are the clearance judge and the
  embedder); its one-pass L-bend generator with no search is rejected (it would route almost nothing).
- Grok's `build.py` edit that disconnected the router and deleted the gates — restored in step 1.
