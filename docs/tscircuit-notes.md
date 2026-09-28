# tscircuit, read against pcbc

Study date: 2026-09-23. Repos were cloned depth-1 and read only (nothing executed) under
`scratchpad/tscircuit/repos/` (abbreviated `repos/` below): core 660d1edd, props 98fa6ad9,
checks db2237a5, capacity-autorouter eb7e607e (v0.0.928), circuit-json 0.0.503,
circuit-json-to-kicad, kicad-to-circuit-json, kicadts, cli, runframe, pcb-viewer,
circuit-json-to-gerber (0.0.107), copper-pour-solver, check-shorts, fabricator-drc,
jlcpcb-manufacturing-specs, rectdiff, circuit-json-util, circuit-json-to-gltf/step, docs.

Confidence tags: **[code]** = read in the source at the cited path; **[doc]** = stated in
their docs/README/issue; **[inferred]** = my conclusion from the above. Issue numbers are
GitHub issues in the named repo.

---

## 1. What tscircuit is

tscircuit is a React/TypeScript circuit compiler: you write a component tree
(`<board><resistor pcbX=…/><trace from=… to=…/></board>`), a custom React reconciler
renders it through ~20 ordered phases into **Circuit JSON**, a flat array of typed
elements (`source_*`, `schematic_*`, `pcb_*`, `cad_*`), and everything else is a pure
function of that array: their own DRC (`@tscircuit/checks`), their own copper-pour
solver, their own RS-274X/Excellon writer, PnP and BOM CSVs, GLTF/STEP, SVG, and — as
one of roughly fifteen sibling exporters — `.kicad_sch`/`.kicad_pcb`/`.kicad_pro`
**[code]** `repos/cli/lib/shared/export-snippet.ts:1-60`; `repos/core/lib/IsolatedCircuit.ts:107,259-265`.
So "a clone that outputs KiCad generated from Python" is wrong in the direction that
matters: KiCad is not their backend and never their checker. KiCad is a *library source*
(footprints and 3D models are fetched from KiCad's GitLab and converted) and an
*export target* (a lossy, inlined, human-facing snapshot). No file under `cli/`, `core/`,
`checks/` or `circuit-json-to-gerber/` invokes `kicad-cli` or `pcbnew` **[code]** (grep
across all clones hits only the two converter repos' test suites). Fab files, DRC and
zone fill are all in-house. It is also much bigger than pcbc: a 24-stage autorouter with
cloud-parallel nodes, a benchmark bot on every PR, a package registry, a hosted editor,
panelisation, board-on-board stacking. Where it overlaps pcbc (source of truth, KiCad,
requirements-driven copper) it has made different — and in several places, on its own
issue tracker's evidence, worse — choices.

---

## 2. Source-of-truth model, side by side

### Their data flow

```
  code (.tsx)  ──┐
                 ├─► core reconciler ──► Circuit JSON (db.toArray()) ──► checks  (*_error / *_warning elements, same array)
  manual-edits.json ┘        ▲                       │
        ▲                    │                       ├─► circuit-json-to-gerber   ──► gerbers + excellon
        │  browser drag,     │                       ├─► circuit-json-to-pnp-csv  ──► pnp.csv
        │  debounced 1 s,    │                       ├─► circuit-json-to-bom-csv  ──► bom.csv
        │  POST /files/upsert│                       ├─► circuit-json-to-kicad    ──► .kicad_pcb/.kicad_sch/.kicad_pro   (one-way)
        └────────────────────┘                       ├─► circuit-json-to-gltf/step
                                                     └─► svg, dsn, readable-netlist, ...
  back-edges: <subcircuit circuitJson={...}>   (frozen precompiled block; core skips layout + routing for it)
              pcbRouteCache={pcbTraces, cacheKey}  (unvalidated re-injection of prior routes)
              <fanout pcbTracePaths=[...]>        (validated, selector-keyed saved copper — see 2.3)
```

**[code]** `repos/core/lib/IsolatedCircuit.ts:20,107,259-265` (`db` *is* Circuit JSON);
`repos/runframe/lib/hooks/use-edit-event-controller.ts`; `repos/cli/lib/dev/DevServer.ts:274-277`
(CLI refuses to sync `manual-edits.json` from disk while dev runs — the browser owns it);
`repos/core/lib/components/primitive-components/Group/Subcircuit/Subcircuit.ts:107-129`.

### pcbc's data flow (for contrast)

```
  board.py (parts, nets, per-net requirements, stackup, outline, declared placement) ─┐
  layout.core.py (AI's locks: copper + poses the generator may not move)             ─┼─► generator ──► layout.gen.py (one object per KiCad primitive, every field explicit)
                                                                                      │                      │
                                                                                      └──── merge(core, gen) ─┴─► ONE writer ──► .kicad_pcb ──► KiCad: zone fill, DRC, gerbers/drill/pos
                                                                                                                                       │
                                                                                                                  checkers read the file; nothing reads design facts back out
```

### Point-by-point

| | tscircuit | pcbc |
|---|---|---|
| Authored inputs | `.tsx` tree + optional `manual-edits.json` **[doc]** `repos/docs/docs/guides/tscircuit-essentials/manual-edits.mdx` | `board.py` + `layout.core.py` |
| Single derived model | Circuit JSON; there is no second representation **[code]** `IsolatedCircuit.ts:259-265` | `layout.gen.py` merged with core |
| Independent arbiter | none; checks are in-house elements in the same array **[code]** `repos/checks/lib/run-all-checks.ts` | KiCad |
| Schema version in the artefact | none; npm version + additive fields + zod transforms of deprecated fields; unknown keys silently stripped **[code]** grep of `repos/circuit-json/src` for `*_version` = empty; `README.md:3281-3296` | (recommend a `schema` constant, see §7) |
| Provenance stamp | `source_project_metadata.software_used_string = "@tscircuit/core@<v>"`; `created_at` defined but never set **[code]** `repos/core/lib/IsolatedCircuit.ts:216-223` | — |
| Ids | `circuit-json-util` counters `${type}_${n}` per type, deterministic for identical insertion order; only hand-parsed JSON gets random ids **[code]** `repos/circuit-json-util/lib/cju.ts:122-134,199-206`; `repos/circuit-json/src/common/getZodPrefixedIdWithDefault.ts:14-19` | authored refdes / net names |

### 2.1 The manual-override mechanism, precisely

The persisted file is exactly three arrays **[code]** `repos/props/lib/manual-edits/manual_edits_file.ts:14-18`:

```
pcb_placements[]        {selector: string, relative_to: "group_center", center: {x,y}}
schematic_placements[]  same shape
manual_trace_hints[]    {pcb_port_selector: string, offsets: RouteHintPoint[]}
```

No rotation, no layer, no copper, no via, no zone. Rotation and layer must go in code
(`pcbRotation`, `layer`) **[code]** `repos/props/lib/manual-edits/manual_pcb_placement.ts:5-13`.

**What keys the override.** The live drag event keys on the ephemeral generated
`pcb_component_id` (and carries `original_center`, `new_center`, `edit_event_id`,
`created_at`); the writer resolves `pcb_component → source_component.name` and persists
`selector: "R1"` — the authored refdes. Upsert-by-selector: an existing entry with the same
selector is replaced in place; only the latest position per name survives, no history
**[code]** `repos/core/lib/utils/edit-events/apply-pcb-edit-events-to-manual-edits-file.ts:20-48`;
`repos/props/lib/manual-edits/manual-edit-events/edit_pcb_component_location_event.ts:8-16`.
Lookup accepts CSS-ish selectors (`.R1`, `#id`, `resistor.R1`) or bare `props.name`
**[code]** `repos/core/lib/components/base-components/PrimitiveComponent/PrimitiveComponent.ts:885-913`.
This is the same choice pcbc should make for `layout.core.py`: key on authored identity
from `board.py`, never on a generator index.

**What survives a code change.**
- Code adds `pcbX/pcbY` to a part that also has a manual placement → a
  `pcb_manual_edit_conflict_warning` element is emitted and **the prop wins**
  ("pcbX and pcbY will be used. Remove pcbX/pcbY or clear the manual placement.")
  **[code]** `repos/core/lib/components/base-components/NormalComponent/NormalComponent.ts:1043-1060`;
  `repos/circuit-json/src/pcb/pcb_manual_edit_conflict_warning.ts`.
- Part renamed or deleted → the entry simply never matches. No warning, no pruning, no
  orphan element anywhere in core or circuit-json; the stale entry rots silently
  **[code]** grep of `repos/core/lib` and `repos/circuit-json/src` for any unmatched-selector
  warning returns only the conflict warning above.
- No hash of the code is stored in the file, so nothing can detect drift **[code]** same schema.

**Partial overrides.** Per component yes, per group no: the moment
`manualEdits.pcb_placements` has one entry, the group's auto layout mode becomes `"none"`
instead of `"pack"` for *all* its unpositioned children
(`if (!hasManualEdits && unpositionedDirectChildrenCount > 1) return "pack"; return "none"`)
**[code]** `repos/core/lib/components/primitive-components/Group/Group.ts:2545-2572`. One
dragged part opts the whole subcircuit out of auto-placement. pcbc's core/gen split is
strictly better here: a locked pose constrains the placer for that part and the placer
must still place the rest — make that an explicit test.

**Trace-level edits in the file are hints, not locks.** The docs describe an "Edit Trace
Mode", but the current `pcb-viewer` has edit modes `"off" | "move_footprint"` only and a
single `EditPlacementOverlay`; `applyEditEventsToManualEditsFile` filters only the two
component-location event types and drops trace-hint events
**[code]** `repos/pcb-viewer/src/global-store.ts:50`; `repos/core/lib/utils/edit-events/apply-edit-events-to-manual-edits-file.ts:15-21`.
File hints become `TraceHint` children that feed the autorouter as waypoints
**[code]** `repos/core/lib/components/primitive-components/Group/Group.ts:726-746`.

### 2.2 Two paths to the same file (a small cautionary example)

runframe's inline `onEditEvent` handler calls `applyEditEventsToManualEditsFile(...)`,
discards the return value, and upserts the *unmodified* parsed file; the debounced store
path is what actually writes the updated content 1 s later
**[code]** `repos/runframe/lib/components/RunFrameWithApi/RunFrameWithApi.tsx:361-385` vs
`store.ts:225-252`. On tscircuit.com there is a third persistence path: package files via
the registry API, and for logged-out users the whole file map compressed into the URL hash
**[code]** `https://raw.githubusercontent.com/tscircuit/tscircuit.com/main/src/hooks/useFileManagement.ts` lines 107-145, 535-600.
pcbc's "one writer" rule is exactly the thing that prevents this class of bug.

### 2.3 Three tiers of saved copper — only the newest matches pcbc's contract

| Mechanism | Keyed on | Validated against current geometry | On drift |
|---|---|---|---|
| `pcbRouteCache={pcbTraces, cacheKey}` | nothing — `cacheKey` is never compared to anything; the one test uses `cacheKey: "test"` **[code]** `repos/core/lib/components/primitive-components/Trace/Trace_doInitialPcbTraceRender.ts:98-111`; `repos/core/tests/components/primitive-components/cached-trace.test.tsx:9-36` | no | stale copper silently inserted |
| `<trace pcbPath={[...]}/>` | code | it *is* the code | n/a |
| `<fanout pcbTracePaths={[{connection:"R1.1", route:[wire|via…]}]}>` (core ≥ 0.0.1884) | stable selectors ("Use stable selectors such as `R1.1`… rather than generated `pcb_port_id`") | yes | **fails** ("Core reports an error instead of stretching a route to fit a moved component"); duplicate exits, unavailable layers and uncovered connections rejected; "partial caches are not supported" **[doc]** `repos/docs/docs/guides/reusing-saved-fanout-trace-paths.mdx` |

The autorouter memo cache, by contrast, is keyed correctly:
`routes:core@<version>:solver:<fnv1a(options)>:srj:<fnv1a(srj)>` — includes the tool
version, so an upgrade invalidates it **[code]**
`repos/core/lib/components/primitive-components/Group/Group_localAutoroutingCache.ts:9-48`.

**[inferred]** The project converged on pcbc's design (selector-keyed, validated,
fail-loud, full-coverage locks) only after shipping the unvalidated ones. That is a useful
data point: pcbc is starting where they arrived.

### 2.4 Reuse and frozen blocks

- Reuse is code-level: a published design is an npm package `@tsci/<author>.<name>`
  from `npm.tscircuit.com`; a consumer re-renders the component inside its own tree, so
  placement and routing of the imported module are **redone** **[code]**
  `repos/cli/lib/shared/add-package.ts:96-130`; `repos/cli/lib/shared/push-snippet.ts:87-103`.
- "Render once, stamp N times" exists only as a private prop `_subcircuitCachingEnabled`:
  each subcircuit renders in its own `IsolatedCircuit`, the JSON is cached under a hash of
  props that **excludes** `name, key, pcbX, pcbY, schX, schY, pcb*EdgeX/Y, pcbRotation,
  schRotation`, so instances at different positions share one routed block **[code]**
  `repos/core/lib/components/primitive-components/Group/Subcircuit_getSubcircuitPropHash.ts:9-22,133-145`;
  zero hits in docs.
- `<subcircuit circuitJson={...}>` freezes a precompiled block: `_isInflatedFromCircuitJson`
  makes core skip layout and routing for it **[code]** `Subcircuit.ts:107-129`;
  `Trace_doInitialPcbTraceRender.ts:94-96`. This is the one sanctioned way a derived
  artefact becomes a source, and it is whole-block, not piecemeal.

---

## 3. KiCad's role: for them vs for us

### What they convert

Two staged pipelines on a shared S-expression object model (`kicadts`):
`circuit-json-to-kicad` (CJ → `.kicad_sch/.kicad_pcb/.kicad_pro/.kicad_mod/.kicad_sym`) and
`kicad-to-circuit-json` (`.kicad_pcb/.kicad_sch/.kicad_sym/.kicad_mod` → CJ). The older
`kicad-converter` and `kicad-pcb` repos are dead (last commits Jul/May 2025; no dependency
from core or cli) **[code]** `repos/circuit-json-to-kicad/lib/pcb/CircuitJsonToKicadPcbConverter.ts`
(stages: InitializePcb → AddNets → AddFootprints → AddTraces → AddVias → AddCopperPours →
AddKeepouts → AddStandalonePcbElements → AddGraphics); `git -C repos/kicad-converter log -1`.

Emitted format versions: `.kicad_pcb` `version = 20241229`, `.kicad_sch` `version = 20250114`
(KiCad 9 formats) while their CI runs KiCad 10.0.x **[code]**
`repos/circuit-json-to-kicad/lib/pcb/stages/InitializePcbStage.ts:31`; `lib/schematic/stages/InitializeSchematicStage.ts:21`.

**Footprints in:** `footprint="kicad:Resistor_SMD/R_0402_1005Metric"` is resolved at
render time by an HTTP fetch of `https://kicad-mod-cache.tscircuit.com/<Lib>/<Name>.circuit.json`
(a proxy over gitlab.com/kicad/libraries), converted to CJ pads/holes/silk; the original
`.kicad_mod` is not kept **[code]** `eval getPlatformConfig.ts` lines 175-205 (from
raw.githubusercontent.com/tscircuit/eval); `repos/kicad-mod-cache/app/[...anypath]/fetch-file.ts:51,69,88`.

**Footprints out (project export):** every footprint is inlined with a synthetic library
link `tscircuit:<mpn|footprinter|type>`; standalone pads become one-pad pseudo-footprints
like `tscircuit:platedhole_circle_holeDiameter1mm_outerDiameter2mm`; a pad reachable from
two nets throws **[code]** `repos/circuit-json-to-kicad/lib/pcb/stages/AddFootprintsStage.ts:54-89,151-169`.
**Footprints out (library export):** `tsci build --kicad-library` *does* write real
`symbols/*.kicad_sym`, `footprints/*.pretty`, `3dmodels/`, `fp-lib-table`/`sym-lib-table`,
and `--kicad-pcm` produces a Plugin-and-Content-Manager repo installable from inside KiCad
**[doc]** `repos/docs/docs/guides/kicad/exporting-kicad-library.md`;
`repos/cli/cli/build/build-kicad-pcm.ts`. KiCad `attr`/properties/3D refs round-trip for
components that came from KiCad via `kicadFootprintMetadata` props; they are lost for
footprinter-string/JSX footprints (issue #585) **[code]**
`repos/circuit-json-to-kicad/lib/pcb/stages/utils/applyMetadataToFootprint.ts`.

### What the PCB export loses or hard-codes **[code]** (read from the stages)

Preserved: segments (one `(segment)` per route-point pair, per-point width, net), vias
(layer span normalised to outermost pair), SMD pads incl. polygon-as-custom-pad, PTH/NPTH,
outline minus cutouts (polygon-clipping difference → `gr_line`s), silk/fab/courtyard, 3D refs,
keepouts as rule areas, pours as zones with `earcut`-triangulated `filled_polygon`s.

Lost or approximated:
- **arcs**: no `gr_arc`/`fp_arc` is ever emitted; the IR's `pcb_trace` route types are
  wire/via/through_pad/teardrop only; circles are polygonised (`grep -rn 'GrArc|FpArc' lib/pcb` = none;
  `repos/circuit-json/src/pcb/pcb_trace.ts`).
- **net classes**: a single `Default` class from `pcb_board.min_*`; per-net widths never
  become KiCad net classes (`repos/circuit-json-to-kicad/lib/project/CircuitJsonToKicadProConverter.ts:180-194,245-252`).
- **board thickness**: `general.thickness = 1.6 // Standard PCB thickness in mm` hard-coded
  while `pcb_board.thickness` defaults to 1.4 and is never read (`InitializePcbStage.ts:38-46`).
- **zone rules**: clearance 0.15 / thermal 0.5 / min_thickness 0.25 hard-coded regardless of
  the authored `<copperpour clearance>`; KiCad refills on open with different copper than the
  Gerbers the user reviewed (core issue #3281) (`AddCopperPoursStage.ts`).
- `pad_to_mask_clearance 0`; ratsnest from unrouted traces (#31); path-shaped cutouts as loose
  Edge.Cuts lines.
- Determinism is half-done: PCB UUIDs are `generateDeterministicUuid('segment:x,y:x,y:layer:net')`
  "so tests are reproducible", but the schematic exporter still calls `crypto.randomUUID()`
  (file uuid, sheets, graphics, net labels) and `.kicad_pro` embeds `new Date().toISOString()`
  (`lib/pcb/stages/utils/generateDeterministicUuid.ts`; `lib/schematic/stages/InitializeSchematicStage.ts:35`;
  `CircuitJsonToKicadProConverter.ts`).

The importer keeps geometry better (arcs on Edge.Cuts, custom pads, filled zones, KiCad 5
upgrade shim) but never reads `(setup …)`, net classes or `design_settings` (grep = 0 hits)
and its README still lists "Trace routing is basic" **[code]** `repos/kicad-to-circuit-json/lib/stages/pcb/*`;
`README.md` "MVP Limitations".

### Do they ever run KiCad?

Not in the product. Yes in two test suites, as an oracle:
- `circuit-json-to-kicad` CI runs inside `ghcr.io/kicad/kicad:10.0.1`; 90 of 163 test files
  call `takeKicadSnapshot`, which shells out to `kicad-cli pcb export svg` / `sch export svg` /
  `pcb render` and PNG-snapshots next to their own renderer. It never runs `kicad-cli pcb drc`
  or `sch erc` on its own output (grep = 0 hits) **[code]** `.github/workflows/bun-test.yml`;
  `tests/fixtures/take-kicad-snapshot.ts`.
- `kicad-to-circuit-json` CI (`ghcr.io/kicad/kicad:10.0.0`) cross-checks imported netlists of
  real boards against **four** KiCad-derived truths: `kicad-cli pcb export gencad`
  ($SIGNALS/NODE), `kicad-cli pcb drc --format json --severity-all --all-track-errors --refill-zones`
  (parsed into same-net/different-net assertions), `kicad-cli pcb export ipc2581`, and
  KiCad's bundled Python `pcbnew.LoadBoard(...).GetConnectivity().GetConnectedItems(pad, pcbnew.IGNORE_NETS)`
  **[code]** `tests/fixtures/kicad-gencad-netlist.ts`; `kicad-ipc2581-physical-connectivity.ts`;
  `kicad-pcbnew-physical-connectivity.ts`; `tests/pcb/kicad-connectivity/corne-keyboard.test.ts`.
- Round-trip (KiCad → CJ → KiCad → CJ) is tested on five sha256-pinned CERN-OHL boards, by
  counts, net-name sets and side-by-side renders, not by byte or DRC equality **[code]**
  `tests/fixtures/create-open-source-board-round-trip.ts`; `scripts/download-references.ts`.
- `kicadts` advertises deterministic formatting, but its `.kicad_pcb` demo round-trip tests are
  `test.skip` — introduced in commit `1e4376ce` (2026-05-07) "repro for unhandled kicad_pcb
  elements (#38)", i.e. a known fidelity gap, not flakiness **[code]** `repos/kicadts/tests/sexpr/KicadPcbDemos.test.ts:28,54`.

### Their rationale

None is written down. The "How tscircuit works" post frames Circuit JSON as "the middleground
between tscircuit and other file formats… a million converters to build out"; the 2024 "KiCad
Integration" post is about parsers and exporters; the import doc says "We're still building
KiCad import directly into tscircuit.com" **[doc]** blog.tscircuit.com posts;
`repos/docs/docs/guides/importing-modules-and-chips/importing-from-kicad.md`. **[inferred]**
the likely reasons are browser-first execution (tscircuit.com evaluates in the browser; no
KiCad binary) and a preference for permissive JS deps. Their users disagree with the
outcome: "KiCad is the only viable visual inspection path" (#284); a request for a
"schematic connectivity round-trip check, separate from PCB DRC" (#549).

**For pcbc**: KiCad is backend and arbiter, on purpose. Nothing here argues otherwise; §4
argues for it harder.

---

## 4. What they built to not need KiCad, and how good it is

### 4.1 DRC: `@tscircuit/checks` vs KiCad

`runAllChecks()` = placement + schematic + netlist + pin-spec + routing groups, ~30 checks
**[code]** `repos/checks/lib/run-all-checks.ts`. Every clearance reads an optional
`pcb_board.min_*` and falls back to a hand-typed JLCPCB table (trace 0.1, via hole 0.2, via pad
0.3, edge 0.2, various 0.1/0.15 mm) — **one clearance per board, no net classes**
**[code]** `repos/checks/lib/drc-defaults.ts:9-19`; `repos/jlcpcb-manufacturing-specs/lib/jlcpcb-manufacturing-specs.ts`
(the "preferred" table is marked `// TODO: Update these values`).

| Check | tscircuit `@tscircuit/checks` | KiCad DRC |
|---|---|---|
| Pad–pad, pad–trace, via–trace, via–pad clearance | yes, single board-wide value | yes, per net class + custom rules |
| Trace–trace overlap / self-short | yes (`checkEachPcbTraceNonOverlapping`) | yes |
| Copper pour shorts | yes (`checkCopperPourShorts`) | yes (zone fill + clearance) |
| Copper to board edge | yes | yes |
| Courtyard overlap / missing courtyard | yes (rect only; polygon/pill open #185) | yes |
| Component out of board / over cutout / over keepout | yes | yes |
| Via in pad | yes | n/a (rule) |
| Same-net / different-net via spacing | yes | yes (hole-to-hole) |
| Unconnected items (port↔trace, source↔pcb trace, contiguity) | yes | yes (ratsnest / unconnected_items) |
| Pin must be connected; same-name nets connected | yes | ERC |
| Trace length / via count / bus skew | yes (`max_length`, `max_via_count`, `maximum_length_skew`) | length via custom rules |
| Per-net-class clearance & width | **no** (schema has `min_trace_width` but no check reads it) | yes |
| Track width minimum vs fab | **no** (`checkSourceTracesMatchPcbTraceThickness` exists, is a warning, and is not in `runAllChecks`) | yes |
| Annular ring, min drill, hole-to-hole PTH | **no** (`min_plated_hole_drill_edge_to_drill_edge_clearance`, `min_via_hole_diameter`, `min_via_pad_diameter` are in the schema and unread) | yes |
| Silk over pad, silk clipped by mask, mask sliver/bridge | **no** | yes |
| Zone islands / unfilled zones | cleanup pass in core, not a check; **skipped whenever any `pcb_autorouting_error` exists** | yes |
| Pour vs pour on different nets | **no** (#189) | yes |
| Differential-pair gap/coupling post-route | **no** (only skew) | via custom rules |
| Creepage / voltage clearance | **no** | custom rules |

**[code]** greps over `repos/checks/lib` for `annular|silkscreen|soldermask|hole_to_hole|drill`
return only `drc-defaults.ts` and copper-to-edge; the unread schema fields appear only in
`drc-defaults.ts`; `repos/core/lib/components/normal-components/Board_doInitialPcbCopperPourCleanup.ts`.
Their umbrella issue "Add all the Design Rule Checks for JLCPCB" (checks #15, 2024) is open
with nothing ticked. `checks` and `copper-pour-solver` ship **no license** (no LICENSE file,
no `package.json` license, npm `license: None`) — do not copy code from them **[code]** per-repo listing.

It is also not independent of its own producers: it is built on `circuit-json-to-flattenjs`,
`circuit-json-to-connectivity-map`, `circuit-json-util` from the same org; tscircuit #4958
documents CLI and browser running *different checker versions* ("DRC implementation parity
lost") **[doc]**.

Error records are good: typed, deterministic ids built from element ids
(`pad_pad_clearance_<a>_<b>`), measured vs required distance, a centre point; candidates
sorted "so error output is deterministic across index layouts". No suggested move
**[code]** `repos/checks/lib/check-pad-pad-clearance.ts`; `check-copper-pour-shorts.ts`.

### 4.2 Gerbers, pours, thermals, PnP

- **Gerbers** (`circuit-json-to-gerber` 0.0.107): F/B/In1..In8_Cu, mask, paste, silk, fab,
  Edge_Cuts as RS-274X with `%TF.FileFunction`; Excellon with one plated file per unique
  layer span (`drill-L1-L2.drl`) plus `drill_npth.drl` (slots via G85). Pours: outer ring as
  G36/G37 region, inner rings as regions under LPC, then traces/pads/vias on top. No `.gbrjob`
  (the file exists, unreferenced). Verified in-repo only by snapshotting the output through
  `gerber-to-svg`, a JS renderer — nothing runs it through KiCad or a fab CAM **[code]**
  `src/convert-circuit-json-to-gerber-files.ts`; `src/gerber/convert-soup-to-gerber-commands/index.ts:723-846,1813-1860`;
  `package.json` (`gerber-to-svg`).
- **Pours**: computed by `@tscircuit/copper-pour-solver` (Manifold CSG in WASM): outline minus
  pads/traces/vias/cutouts with margins (default clearance 0.2), emitted as `brep` polygons;
  thermal reliefs opt-in, spoke width a constant `DEFAULT_THERMAL_RELIEF_SPOKE_WIDTH_MM = 0.3`;
  island removal by **area only**; connectivity-based floating-copper cleanup is a separate
  pass skipped when routing errored **[code]**
  `repos/core/lib/components/primitive-components/CopperPour/CopperPour_doInitialPcbCopperPourRender.ts:14,66-95`;
  `repos/copper-pour-solver/lib/solvers/copper-pour/process-obstacles.ts:121`.
- **Second short checker**: `@tscircuit/check-shorts` (`tsci check shorts`) rasterises each
  Gerber layer at 35 µm/pixel and flood-fills for different-net contact; its README admits
  gaps below the pixel size "may disappear during rasterization" **[doc]** `repos/check-shorts/README.md`.
  They built it because the geometric checker missed shorts.
- **Fab presets**: `@tscircuit/fabricator-drc` (published, 0.0.4, installed as the default
  `platform.fabricatorEngine` by `@tscircuit/eval`) with dated presets
  (`jlcpcb_economy_20260912`; undated names alias to newest). Its **only** rule today: via hole
  < 0.3 mm → surcharge *warning* **[code]** `repos/fabricator-drc/lib/index.ts:16-33,40-64`;
  `eval getPlatformConfig.ts:1,130`.
- **PnP**: `circuit-json-to-pnp-csv` writes Designator/Mid X/Mid Y/Layer/Rotation; supplier
  rotation correction only `console.warn`s when it cannot verify; two shipped regressions where
  it was silently skipped (tscircuit.com PR #4932, cli PR #4782) **[code]** `repos/circuit-json-to-pnp-csv/src/index.ts`.

### 4.3 Quality evidence from their trackers **[doc]**

Fab-fatal, last 12 months:
- cli #4346 (closed via #4419): non-through-via drill programs omitted from the ZIP;
  "confirmed production failure… 31 missing drills caused opens on 15 logical nets". Root cause
  (autorouter #2156, open): the 4-layer router emits blind/buried vias by default; JLCPCB does
  not fab them.
- circuit-json-to-gerber #78 (open): 4-layer boards exported without In1/In2 copper — "a fab
  building from these files produces a 2-layer board". Current source does enumerate inner
  layers; may be stale, not closed.
- circuit-json-to-gerber #68 (open, filed by the maintainer): outline "not created/interpreted
  by JLCPCB properly". #155: `hole_with_polygon_pad` copper exported unrotated. #127: export
  crashes on multilayer plated holes.
- circuit-json-to-kicad: #579 library symbols get pin numbers from library order (a KiCad
  "Update PCB from Schematic" would flip the part); #535 pad-number collisions; #595 power nets
  as non-power symbols splitting nets; #549 generated schematics fail native ERC (57
  `pin_not_connected`, 157 `endpoint_off_grid` on 39 parts) while their own net checks pass;
  #403/#290 literal `NaN` coordinates producing files KiCad cannot open.

Their own contributors reach for KiCad when the pipeline disagrees with itself:
- copper-pour-solver #91: orphan pour islands found only because `--kicad-project` + KiCad's
  filler removed them — "circuit JSON shows copper that KiCad never fabricates".
- tscircuit #5142: a 173-part design validated by running KiCad DRC on the export ("only 1
  clearance error and 0 unconnected items") while `circuit-json-to-gerber` throws "Aperture not found".
- cli #4716: KiCad export drops `min_trace_width`/`min_board_edge_clearance` so "native
  kicad-cli pcb drc therefore uses unrelated KiCad defaults"; reporter asks for verification
  "through native KiCad DRC/readback".
- autorouter PR #2571: one board, "311 traces, 333 Core errors and 53 Gerber-reported short
  regions", of which 2 verified real — their geometric and raster checkers disagree by an order
  of magnitude.
- Inside the router's repair stage they run their own DRC *and* a reference DRC and count
  `referenceDrcFalseNegativeCount` where the two disagree **[code]**
  `repos/capacity-autorouter/.../Pipeline9JointDrcRepairSolver.ts` ~1400-1430.

### 4.4 Verdict on "should our Python write fab files directly?"

**No.** tscircuit is the experiment already run. To leave KiCad they wrote ~30 geometric checks
on three home-grown geometry libraries, a WASM CSG pour solver with its own island and
thermal logic, a full RS-274X + Excellon writer, a *second* raster short detector because the
first missed shorts, and a fab-preset package that checks one thing — and the fab-fatal bugs
that shipped afterwards are exactly the class KiCad refuses or makes visible: drill files
missing for 31 vias, inner layers dropped, an outline JLCPCB could not read, pour islands
KiCad's filler deletes but their JSON keeps. When their checkers disagreed (333 vs 53 vs 2),
the contributors exported to KiCad and ran `kicad-cli pcb drc`. That is the lead's argument
("the only check we didn't write") demonstrated on someone else's budget. Keep KiCad as the
backend and arbiter; spend the effort tscircuit spent on a Gerber writer on making the
`.kicad_pcb` writer and the KiCad readback gate airtight instead.

---

## 5. Their autorouter vs pcbc's channel map

Read from `repos/capacity-autorouter` (v0.0.928) and core's wrapper.

**What it is [code].** The default is "Pipeline 9"
(`AutoroutingPipelineSolver9_PreloadedTraceGraph`), a 24-stage sequential pipeline of
solvers (each with `step()/solved/failed/error/visualize`): preprocess SRJ → component
detection → escape-via location → net-to-point-pairs → topology planning → merging → node
subdivision → edges → segment points → preloaded trace graph → global port-point pathing →
uniform port distribution → high-density (intra-node) route → force-improve → repair → stitch →
simplify ×2 → trace width → global DRC force-improve → joint DRC repair → length matching →
power-trace expansion **[code]** `lib/autorouter-pipelines/AutoroutingPipeline9_PreloadedTraceGraph/AutoroutingPipelineSolver9_PreloadedTraceGraph.ts:307-1030`.

**Mesh.** Not a quadtree, not a triangulation: rectangle subtraction. `@tscircuit/rectdiff`
("a 3D rectangle diffing algorithm… to quickly break apart a circuit board into capacity
nodes", grid mode; `minSingle = 2×traceWidth`, `minMulti = 4×traceWidth`, `preferMultiLayer:
true`, `maxAspectRatio 3`) plus hand-built QFP/SOIC/BGA topology templates around packages,
merged, then split at `maxNodeDimension 15 mm` / aspect > 6, dropping nodes < 0.1 × 0.1 mm
**[code]** `lib/solvers/TopologyPlanningSolver/MultiGraphTopologyPlannerSolver.ts`;
`repos/rectdiff/lib/solvers/RectDiffSeedingSolver/RectDiffSeedingSolver.ts:95-120`; pipeline ctor lines 991-993.
The older quadtree (`CapacityMeshNodeSolver1`) survives for older pipelines.

**Capacity.** In via-units: `span = sqrt(w·h)`, `viaLengthAcross = span·factor / (VIA_DIAMETER/2 + obstacleMargin)`
with 0.3 / 0.2 mm, factor clamped [0.85, 1.2], `capacity = (viaLengthAcross/2)^1.1 × maxCapacityFactor`,
single-layer nodes capped at 1 — "how many vias the node can fit, tuned for two layers"
**[code]** `lib/utils/getTunedTotalCapacity1.ts`.

**Global solve.** Hypergraph A* with rip-and-reroute over `tiny-hypergraph`: regions = nodes,
ports = discrete points along shared edges, duplicated per layer; weights `SHUFFLE_SEED 0`,
`MAX_RIPS 1000`, `RANDOM_RIP_FRACTION 0.3`, `LAYER_CHANGE_COST 0`, `NODE_PF_MAX_PENALTY 100`,
`GREEDY_MULTIPLIER 0.7`. Congestion is a **soft** cost via a per-node probability of failure,
not a hard capacity constraint; the pf map is then handed to the detailed stage
**[code]** `lib/solvers/PortPointPathingSolver/tinyhypergraph/TinyHypergraphPortPointPathingSolver.ts`.

**Detailed solve.** Per node, independent, A* over (x, y, z) cells at 0.05 mm with via
penalty 0.3, obstacle/edge repulsion and "future connection" proximity penalties; vias are
z-moves. On failure: a "B01" solver, then a regional fallback that re-solves a merged
region; if that fails, the pipeline fails with a joined string
**[code]** `lib/solvers/HighDensitySolver/SingleHighDensityRouteSolver.ts`; `Pipeline9HighDensitySolver.ts:902-1000`.
No separate layer-assignment pass; escape vias pre-planned near dense pads; routes found at
`minTraceWidth` and widened afterwards by `TraceWidthSolver` / `PowerTraceExpansionSolver`.

**Honest comparison with pcbc's gate-capacity triangulation [inferred].**
- Their rectangle decomposition is coarser than a Delaunay channel map and they compensate
  with package templates; a triangulation adapts to pad geometry without templates. Keep the
  triangulation.
- Their capacity normalisation (via-units) and their soft pf cost before hard failure are
  both worth adopting on top of pcbc's gates.
- Their detailed stage is a fine-grid A* per region, not a channel router; if pcbc's channel
  map already yields a topological route per gate, the detailed step is simpler than theirs.
- Their post-hoc widening (route thin, widen later) is the mechanism behind the silent
  narrowing in §6 — do not copy it.

**Determinism [code].** No `Math.random` in `lib/` outside `lib/testing`; all shuffles go
through a seeded xorshift128+ (`lib/utils/cloneAndShuffleArray.ts`, seed 0); `Date.now`/
`performance.now` feed stats only; repair budgets are counts derived from (routes, errors,
effort) not milliseconds. Caveats: no test asserts determinism directly (2 of 635 test files
mention the word); the gate is snapshot tests plus a `/update-snapshots` bot that auto-commits
churn; the Networked pipeline has wall-clock request deadlines and the docs merely assert it
does not change outputs; core memoises routes by hash so repeatability often comes from a
cache hit **[code]** `.github/workflows/benchmark-instructions.yml`; `Pipeline9NetworkedHighDensitySolver.ts:217`;
`Group_localAutoroutingCache.ts`.

**Failure reporting [code].** A free-form string: `'<Solver> error: <exception>'` or
`'<Solver> ran out of iterations (MAX_ITERATIONS=N)'`, wrapped as
`pcb_autorouting_error {error_type, message}` — no coordinates, no suggested move; the
subcircuit gets no traces; traces from earlier successful phases are **discarded** (the
per-phase catch rethrows before `_asyncAutoroutingResult` is set); with >50 connections
under the default preflight policy core refuses and tells you to add `<autoroutingphase/>`
elements ("The autorouter may hang unless you create autorouting phases incrementally.")
**[code]** `lib/solvers/BaseSolver.ts`; `repos/core/lib/components/primitive-components/Group/Group.ts:1950-1990`.
The real asset is the reproduction artefact: `tsci build --autorouter-dump-srj failed` writes
the exact `SimpleRouteJson` problem, `bun run bug-report-with-test <url>` turns it into a
fixture + snapshot test, and stage-by-stage `visualize()`/`solveUntilPhase` in a Cosmos
debugger **[doc]** `repos/docs/docs/contributing/report-autorouter-bugs.md`; `capacity-autorouter/README.md`.

**Published numbers [doc]** (from PR benchmark comments, not the website): dataset01 (85 small)
100 % completion, 0 DRC issues, P50 2.6 s; srj18 (16 KiCad-derived boards) 81 % completion,
98 DRC issues, P50 87 s / P90 238 s under a 360 s timeout; srj19 (200 BGA overlays) 92 %,
739 DRC issues; srj20 78 %, 330 DRC issues. "Completion" and "DRC issues" are separate columns
and srj33 is a curated dataset of boards that "complete routing with DRC issues"
(tscircuit-autorouter PRs #2413, #2660; `scripts/benchmark/scenarios.ts`).

**Placement [code].** Group mode defaults to `pack` (`calculate-packing` PackSolver2,
`largest_to_smallest`, placement strategy `minimum_sum_squared_distance_to_network` under an
`@ts-expect-error` because it is absent from the props enum, `minGap 1 mm`, per-copper-layer,
a platform wall-clock `pcbPackSolverTimeoutMs`). Declared `pcbX/pcbY` children become
immovable obstacles (`isStatic` with a courtyard/pad-bbox collision obstacle); a subgroup with
its own anchor freezes its whole subtree; `<constraint xDist|yDist|sameX|sameY>` clusters are
union-found, solved with Cassowary (`@lume/kiwi`) and packed as rigid bodies; positions may be
`calc` expressions over sibling geometry resolved in Kahn order with a hard error on cycles
**[code]** `Group_doInitialPcbLayoutPack/Group_doInitialPcbLayoutPack.ts:158-200,241`;
`applyComponentConstraintClusters.ts`; `Group_doInitialPcbCalcPlacementResolution.ts`.

**Alternative routers [code].** `freerouting` exists only as a legacy cloud job
(`POST /autorouting/jobs/create`, `provider: "freerouting"`); `krt` is real and default-registered
by `@tscircuit/eval` via `@tscircuit/krt-wasm` — a WASM build of `drandyhaas/KiCadRoutingTools`,
a third-party MIT Rust router shipped as a KiCad *plugin*, not KiCad's router; `sequential_trace`
is a legacy per-trace A* behind `allowLegacyAutorouters`. The pluggable contract is
`autorouter={{ algorithmFn }}` → `{start, stop, on, solveSync}` over the same SRJ, or
`platformConfig.autorouterMap[name]` **[code]** `eval getPlatformConfig.ts:8,131-141`;
`repos/core/lib/utils/autorouting/getPresetAutoroutingConfig.ts`; `Group.ts:925-1000`.

**Borrow / don't (routing).** Borrow: a serialised routing-problem file as the router
boundary (§7 #1); the three determinism rules; via-unit capacity + pf soft cost; pinned
dataset repos as regression fixtures; the pluggable router contract. Don't: free-string
failures, discarding earlier phases, wall-clock budgets, "look at the picture" as the
author-facing failure contract (fine as a developer tool), and route-thin-then-widen.

---

## 6. Requirements-driven routing: what they have, what they don't

**Have [code].**
- Length budgets, enforced both before and after routing: `maxLength` → `source_trace.max_length`;
  pre-route, straight-line endpoint distance > budget skips routing with a violation naming
  trace, distance and budget; post-route `checkPcbTraceLengths`, `checkPcbTraceViaCounts`,
  `checkPcbBusLengthSkew` (`actual_length_skew` vs `maximum_length_skew`)
  `repos/core/lib/utils/autorouting/should-skip-autorouting-because-of-trace-length-violations.ts:104-175`;
  `repos/checks/lib/run-all-checks.ts:100-102`.
- Component-carried requirements, hard-coded: a crystal forces `max_length = 10 mm` and
  `max_via_count = 0` on X1/X2; a capacitor's `maxDecouplingTraceLength` becomes the connected
  trace's `max_length` (min over caps) `repos/core/lib/components/normal-components/Crystal.ts:17-18,85-140`;
  `.../trace-utils/get-max-length-from-connected-components.ts:4-27`.
- Differential pairs: `maxLengthSkew` (default 0.1 mm), `pcbTraceGap`, `maxUncoupledLength`
  forwarded to the router `repos/core/lib/utils/autorouting/getDifferentialPairsForSimpleRouteJson.ts:279-300`.
- Dated fab presets (`jlcpcb_economy_20260912`) `repos/fabricator-drc/lib/index.ts:16-33`.
- Typed post-route errors carrying both numbers (`actual_clearance`/`minimum_clearance`,
  `actual_length_skew`/`maximum_length_skew`).

**Don't have [code].**
- Any derivation of copper from intent. Trace width is user-typed (`<trace thickness>`,
  `<net nominalTraceWidth>`, `<bus pcbTraceWidth>`, board `minTraceWidth`); the per-connection
  width is `max(net.trace_width, trace.min_trace_thickness)` else nominal else `min_trace_width`
  else 0.1 mm `repos/core/lib/utils/autorouting/getSimpleRouteJsonFromCircuitJson.ts:687-699,821-824`.
- Current/ampacity, voltage-to-clearance, creepage, IPC-2221/2152: zero hits in core,
  circuit-json, checks, autorouter. Component ratings (`currentRating`, `maxVoltageRating`)
  are BOM metadata that nothing reads for copper.
- "Power" is a name regex (`POWER_NET_REGEX`/`GROUND_NET_REGEX`) or a flag; the power-trace
  expansion pass widens connections whose *requested* width ≥ max(min+0.1, 2·min) — because you
  typed a wide number, not because of amps `repos/core/lib/components/primitive-components/Net.ts:32-41`;
  `.../getPowerTraceExpansionConnectionNames.ts:3-24`.
- **Nominal width is a wish.** `TraceWidthSolver` tries `[nominal, (nominal+min)/2, min]` and
  silently narrows to the fab minimum where clearance is tight; the check that would notice
  is a warning, not wired in, never called from core `repos/capacity-autorouter/lib/solvers/TraceWidthSolver/TraceWidthSolver.ts:56-67`.
- `targetDifferentialImpedance` is parsed by props and never read by core — a dead input with
  no warning (grep of `repos/core/lib` = 0 hits). No diff-pair gap/coupling check post-route.
- Stackup is three scalars (`layers` ∈ {1,2,4,6,8,10}, `material`, `thickness` default 1.4);
  no dielectric, Er, copper weight or impedance model. The DDR guide says the length props
  "do not calculate stackup impedance or differential-pair spacing" and points at datasheets
  `repos/props/lib/components/board.ts:84-86,220-231`; `repos/docs/docs/guides/routing-ddr.mdx:95-99,152-154`.
- Net classes: none in the IR; the KiCad export writes one `Default` class from the fab floor,
  so KiCad DRC on their export can never check a per-net requirement.
- Fab table: 8 hand-typed numbers, not keyed by layer count, copper weight or service tier.

On this angle pcbc is doing something tscircuit has not attempted; there is nothing to copy
for the requirements→numbers compiler itself. The anti-patterns are instructive: silent
downgrade, dead inputs, exporter literals overriding the IR (thickness 1.6 vs 1.4), and grading
with the router's own constants.

---

## 7. Borrow / avoid / think about (ranked by how much it would change pcbc)

### Borrow

1. **A serialised routing-problem file as the router boundary.** Everything good in their
   routing workflow hangs off `SimpleRouteJson`: dump-on-failure, one-command bug-report-to-fixture,
   25+ dataset repos that are just directories of these files, and a foreign router (`krt`)
   plugged in over the same file. Define one pcbc artefact (outline, stackup, obstacles with
   nets, nets as point sets, per-net width/clearance/pair/length constraints, `layout.core.py`
   copper as preloaded traces) that `board.py + layout.core.py` compile into and the generator
   consumes; ship it next to every MOVE. **[code]** `repos/docs/docs/contributing/report-autorouter-bugs.md`.
2. **KiCad as a test oracle through four surfaces, in a pinned image.** `kicad-cli pcb export gencad`
   for logical nets, `kicad-cli pcb drc --format json --refill-zones` parsed into same-net /
   different-net assertions, `kicad-cli pcb export ipc2581` for geometry, `pcbnew.GetConnectedItems(pad, IGNORE_NETS)`
   for physical copper — run as a permanent gate that diffs `board.py`'s declared nets against
   what KiCad computes from the emitted `.kicad_pcb`, inside `ghcr.io/kicad/kicad:<pinned>`.
   This catches "our checker graded the wrong thing" by construction. **[code]**
   `repos/kicad-to-circuit-json/tests/fixtures/kicad-gencad-netlist.ts` (its regex `pad N [..] of REF`
   → node keys shows how to turn KiCad's DRC items into machine-addressable identities a MOVE needs).
3. **Fail-loud locks (their `<fanout pcbTracePaths>` semantics).** Stable authored selectors,
   validated against current geometry, error instead of stretch, no partial coverage. Add:
   `merge(core, gen)` fails when a lock references a part/net absent from `board.py` — tscircuit's
   `manual-edits.json` has no stale detection and the entry rots silently. **[doc]**
   `reusing-saved-fanout-trace-paths.mdx`; **[code]** absence of any orphan warning in core.
4. **Three lint-enforced determinism rules**: no `random` outside a seeded generator with a fixed
   seed, wall-clock only in stats, budgets as counts derived from (routes, errors, effort). Their
   half-done KiCad side (random schematic UUIDs, a wall-clock timestamp in `.kicad_pro`) is the
   counter-example: UUID = hash(stable object id), never emit a timestamp. **[code]**
   `lib/utils/cloneAndShuffleArray.ts`; `generateDeterministicUuid.ts`; `InitializeSchematicStage.ts:35`.
5. **Dated fab presets.** `jlcpcb_economy_20260912`, undated aliasing to newest; a rule change
   is a diff, a rerun a year later is byte-identical. Key pcbc's by (service, layers, copper oz,
   finish) and cite the capability page + date — theirs is 8 numbers and a `TODO`. **[code]**
   `repos/fabricator-drc/lib/index.ts:16-33`; `repos/jlcpcb-manufacturing-specs/lib/jlcpcb-manufacturing-specs.ts`.
6. **Push the manufacturing minimums and every per-net requirement into the `.kicad_pro`/`.kicad_pcb`
   (net classes) and a `.kicad_dru` for what net classes cannot say** (creepage, pair gap, length),
   then assert they round-trip via kicad-cli. cli #4716 is what happens when you don't.
7. **Declared placement semantics**: declared poses as immovable obstacles for the packer; a
   subgroup anchor freezes its subtree; Cassowary constraint clusters packed as rigid bodies;
   `calc` positions over sibling geometry resolved topologically with a hard error on cycles.
   The last is a cheap, high-leverage addition to `board.py`. **[code]** `Group_doInitialPcbLayoutPack.ts:158-200`;
   `Group_doInitialPcbCalcPlacementResolution.ts`.
8. **Pre-route feasibility diagnostics** in MOVE shape ("these two pads are 14.2 mm apart, budget
   is 10 mm") for every length/skew budget before the router runs an impossible problem.
   **[code]** `should-skip-autorouting-because-of-trace-length-violations.ts:104-175`.
9. **Component-carried requirements as data, with provenance** (crystal: no vias, ≤10 mm;
   decoupling cap: loop length) merged into the net's requirement set — they hard-code these.
10. **Via-unit capacity and a probability-of-failure soft cost** on pcbc's gates before a hard
    channel failure. **[code]** `getTunedTotalCapacity1.ts`; `NODE_PF_MAX_PENALTY`.
11. **Deterministic error ids keyed on element ids with measured/required values** — pcbc's MOVE
    is one step stronger; keep the id discipline. **[code]** `repos/checks/lib/check-copper-pour-shorts.ts`.
12. **A raster short check on the final Gerbers** as a cheap third opinion independent of both
    the generator's geometry and KiCad. **[doc]** `repos/check-shorts/README.md`.
13. **sha256-pinned open-source `.kicad_pcb` reference boards** as writer fixtures.
    **[code]** `repos/circuit-json-to-kicad/scripts/download-references.ts`.
14. **Completion and DRC-clean as separate columns** in any router benchmark; "solved" must mean
    KiCad-DRC-clean. **[doc]** autorouter PR #2413 benchmark comment.

### Avoid

1. **Writing fab files, DRC, or a zone filler yourself** (§4.4). Ship zone *specs*; let KiCad fill.
   Do not ship pre-triangulated `filled_polygon`s — KiCad refills them anyway.
2. **Silent downgrade of a requirement-derived number.** If a width came from 3 A, a router that
   cannot meet it must fail with a MOVE; grade `.kicad_pcb` segment widths against `board.py`.
   **[code]** `TraceWidthSolver.ts:56-67`.
3. **Dead inputs.** `targetDifferentialImpedance` accepted and ignored. Reject any requirement
   field the compiler does not consume; list every input used in the compiled-numbers file.
4. **Exporter literals overriding the model** (thickness 1.6 vs 1.4; hard-coded zone rules). The
   writer emits every value from `merge(core, gen)`; add a readback check of thickness / layer
   count / net classes from the `.kicad_pcb` against `board.py` (that checks the writer, not KiCad).
5. **Unvalidated caches** (`pcbRouteCache` with a decorative `cacheKey`). If routing is cached, key
   it on a hash of the problem plus the tool version, as their `Group_localAutoroutingCache` does.
6. **Whole-group opt-out on one override** (`pack` → `none`). Test that a single lock leaves the
   placer working for everything else.
7. **Inlining footprints under a synthetic `tscircuit:<name>` link with no `fp-lib-table`** in the
   project export; pcbc owns the project, so write a real `.pretty` + `fp-lib-table` next to the board.
8. **Compensating in the writer for IR ambiguity** ("`source_trace_id` may actually be a
   `source_net` ID", four-deep fallback chains). Net identity must be unambiguous before the
   writer — the argument for every field explicit in `layout.gen.py`. **[code]** `AddViasStage.ts:955-1025`.
9. **Polyline-only copper.** No arcs in the IR meant no `gr_arc` ever and circles as line soups.
   If pcbc wants arcs, they go in the primitive model on day one.
10. **Skipping a check because an upstream stage errored** (pour cleanup skipped whenever routing
    failed — the broken boards get the least checking).
11. **Wall-clock budgets** (`pcbPackSolverTimeoutMs`, cloud request deadlines) — a nondeterminism
    hole even when the docs say outputs don't change.
12. **Copying code from `@tscircuit/checks` or `copper-pour-solver`** — no license. Everything else
    read here is MIT (circuit-json says ISC with no LICENSE file).

### Think about

- **Zone fill is the one place the `.kicad_pcb` contains copper the generator did not enumerate
  field-for-field.** State that explicitly in pcbc's model; the checker should treat the filled
  zone as KiCad's output, not the generator's.
- **A schema constant at the top of `layout.core.py` / `layout.gen.py`** that the writer refuses to
  mismatch. Circuit JSON has no in-band version and zod strips unknown keys silently; Python makes
  the loud version trivial.
- **Frozen opaque blocks.** If pcbc ever imports a reference design or a previously fabbed board,
  do it as tscircuit's `<subcircuit circuitJson>` does — whole block, no re-layout, no re-route —
  never by reading facts out of a `.kicad_pcb` piecemeal.
- **A pluggable router contract** over the routing-problem file (their `algorithmFn`/`autorouterMap`).
  With #1 it is the whole interface for a second router, including KiCad's own or `krt`.
- **The drag-to-file loop in spirit.** Their event is `{pcb_component_id, original_center, new_center}`
  and its persisted form is `{selector, center, relative_to}` — literally a MOVE. Emit router
  failures in the exact syntax of a `layout.core.py` lock line so they paste verbatim.
- **Stackup → impedance → width/gap is pcbc's differentiator.** tscircuit has no stackup model
  at all; do not water it down to match.

---

## 8. What we could not establish

| Open question | Cheapest way to answer |
|---|---|
| Does any tscircuit CI run `kicad-cli pcb drc` on its **own** export? None found in circuit-json-to-kicad, cli, core, runframe, tscircuit/tscircuit (`gh search code kicad-cli`). | Already near-certain; a final `gh search code "pcb drc" org:tscircuit` would close it. |
| Is `circuit-json-to-gerber` #78 (4-layer exported as 2-layer) still reproducible? Source now enumerates In1..In8. | Run their exporter on a 4-layer example in a sandbox and open the ZIP; or watch the issue. |
| Does tscircuit.com's hosted "download fabrication files" run a different checker version than the CLI (#4958 says yes)? | Not answerable from source; compare `@tscircuit/checks` versions pinned in `tscircuit.com` vs `cli` lockfiles. |
| What does JLCPCB's CAM do with LPC-based pour holes and per-span `drill-L1-L3.drl` files? (#68 hints at outline misreads.) | Upload one of their example Gerber ZIPs to JLCPCB's viewer (no order). |
| Does the CLI build cache key include the tool version, or only the source md5? | Read the rest of `repos/cli/lib/shared/circuit-json-build-cache.ts`. |
| Is `AutoroutingPipelineSolver9_Networked` byte-identical to the local Pipeline 9 on a timed-out node? | Trace the cache-miss/deadline branch in `Pipeline9NetworkedHighDensitySolver.ts`. |
| Is `packPlacementStrategy: "minimum_sum_squared_distance_to_network"` implemented in `calculate-packing`? | Clone `calculate-packing` and grep. |
| Is the registry `freerouting` job path still served? | `curl` the registry endpoint, or ask in their Discord. |
| Written rationale for keeping KiCad out of the runtime loop? | None in docs/blog; a Discord/discussion search or asking seveibar. |
| How often do autorouter snapshots churn on main (stability across versions vs across runs)? | `git log --stat -- tests/**/__snapshots__` in `tscircuit-autorouter`. |
| Does KiCad 10 honour their `.kicad_pro` rule mapping (e.g. `min_through_hole_diameter` set from `min_via_hole_diameter`)? | Open one export in KiCad 10 and read Board Setup → Constraints. |
| Does the copper-pour solver honour `min_same/different_net_trace_edge_to_trace_edge_clearance`? Has #91's connectivity-based island removal landed? | grep `repos/copper-pour-solver/lib` for the field names; check #91. |
