# The power-move review: 25 findings, 15 confirmed, all 15 fixed

`ampacity.power_moves` carries a rail that cannot carry its declared current out of `fab/report.json`
and into the build's own output. It is 60 lines of formatting over a measurement that already
existed, and it was reviewed the way a pattern is: three independent lenses (correctness, contract,
usefulness-to-an-AI) raised 25 findings, and each one went to an adversarial verifier required to
**refute** it by reading the code, defaulting to "not real" when it could not demonstrate the
failure. Fifteen survived. Every one is fixed, and what they were is the useful part of this file:
the same traps are available to any future move the tool prints.

The verifiers' work is in
`~/.claude/projects/.../subagents/workflows/wf_e407960f-b53/journal.jsonl`; the numbers and the
before/after lines are in `docs/r2-measurements.md` under "Power, in the build's own words".

| # | what it was | where | how it was fixed |
|---|---|---|---|
| 1 | `--strict-power` set `PCBC_STRICT_POWER` on the **process** and never cleared it, so the flag outlived its build. The suite proved it: `test_cli.py` collects before `test_examples_fab.py`, so buck, c3_usb and node each failed their own build hundreds of tests later with the move as their error. The same leak was latent in `--strict-patterns` from S4 and nothing had called it twice in one process | `cli.py` | `_env()` scopes both flags to the one `build_job` call and restores the previous value; two tests pin that the flag is gone on return and that a value the caller set survives |
| 2 | The pour move named a layer **another net already pours**. node pours `GND` on `In1.Cu` and `3V3` on `In2.Cu`, and its `VBUS` move said `In1.Cu` — a second plane over the ground plane, on a board whose own D.5 check then asks why `GND` is in two pieces | `ampacity.py` | `_plane_layer` takes the board's declared planes and returns a **free inner** layer or nothing |
| 3 | On two layers the pour move was worse than wrong: `route.krt_plan` reads `planes=` only when `job.layers > 2`, so `Board(planes=[("VIN", "B.Cu")])` on buck pours **no copper at all** — while `power_bottlenecks` marked `VIN` zoned on the strength of the declaration and stopped measuring it. The tool would have talked an author into silencing its own warning | `ampacity.py` | no pour clause below three layers; the exemption reads the zone in the routed file and never `job.planes`; and `Board()` now refuses `planes=` on a two-layer board outright |
| 4 | A net with fewer than two pads is never walked, leaving `carries` at `math.inf` and `width_mm` at 0.0 — and the floor test, `0.0 < need_mm`, was then always true. The ledger papered over it; the move turned it into a claim about copper nobody measured, with an empty location, and `--strict-power` failed the build on it | `ampacity.py` | `_verdict` returns an unwalked row `ok` |
| 5 | `NetReq("VIN", amps=...)` as a **new line** is two refusals before the board is drawn: `kind="generic"` takes no `amps=`, and the net is already named by another `NetReq`. `Board(planes=[...])` as a second bare `Board()` raises at load | `ampacity.py` | every edit is now an edit to a line that exists: *change* `amps=` on the NetReq that declares the net, *add* a tuple to the `Board(planes=...)` there |
| 6 | On an `under floor` row where `need_mm` is the `WIDTH_FLOOR_MM` constant, no current an author can declare clears the row — and the sentence argued with itself, saying the rail carries **more** than it declares and then offering "declare what it carries". Following it below 0.2 A would have narrowed the class pcbc writes from 0.4 mm to 0.25 mm while the same line kept printing | `ampacity.py` | the amps edit is dropped when the floor is the constant, and the row says so; with no edit left it says "No board.py edit widens it: the copper is the leftover router's and R3 owns its width" |
| 7 | `where` is a pad pair, and every pair whose path crosses one neck ties at that neck's amps — so the reported pair is the first in sorted order among the tied ones. buck's is `C_IN1.1->R_EN.1`, a 100 k enable pull-up drawing 0.12 mA, while its 2 A path is `J_IN.1->U1.3`. "Move those two parts closer" named two parts not on the rail, both already `Place(to=)`d to different regulator pins | `ampacity.py` | `Bottleneck.at_mm` carries the narrowest piece's own midpoint and layer; the move and the ledger name the coordinate and keep the pair as `path ...` |
| 8 | Nothing anywhere noticed a declared plane that produced no pour: `Board()` accepted it, the router ignored it, the ampacity exemption honoured it, `FAB_NOTES.md` reported the rail as poured | `language.py`, `ampacity.py` | two `Board()` refusals (`planes=` on two layers; a layer the stackup does not have) and an exemption that reads only the zone in the file |
| 9 | `PCBC_STRICT_POWER` matched `"1"` exactly while `PCBC_STRICT_PATTERNS` accepts 1/true/yes/on, so `PCBC_STRICT_POWER=true` was a silent no-op — the worst thing a strictness flag can be | `fab.py` | one `_ON` spelling, pinned against `patterns.hard_refusals` |
| 10 | The `under floor` docstring called `need_mm` pcbc's 0.15 mm constant when it is `max(IPC-2221, IPC-2152)` and only clamps at the bottom — which is exactly the distinction finding 6 turns on | `ampacity.py` | the docstring says which it is and why the difference decides whether an edit exists |

Ten of the refutations are worth as much as the confirmations, and two deserve recording because
they are the arguments a future reviewer will make again:

- **"`Place()` with empty parens reads as code to paste."** Refuted, and the refutation is the rule:
  `Place(ref, ...)` requires a positional ref, so a bare `Place()` cannot parse — it is the marker
  that prose, not a call, is being offered. The repo's real "write this call" messages always print
  a ref and a keyword.
- **"Re-declaring buck's `VIN` at 1.21 A trips `under floor` instead."** Refuted by the coupling the
  claim omitted: `class_mm` **is** the amps-derived width and the net is re-routed at it, so the
  0.3905 mm neck is not a fixed asset — it is half of whatever class the declaration produces.
