# R2 measurements

What the copper looks like before each R2 slice and after it. Every number comes from a fresh
build of all five boards (`pcbc build <board> --force` into a temp copy, never the checked-in
`layout/` artifacts, which are a stale generation), read by `copper_bar.copper_bar`.

Boards: the four examples plus the MaD DS2 Addon (`Hardware/DS2Addon/pcbc/ds2_addon.py`), which
is the only board here drawn for a real order.

## What each pattern would own

Every routed segment classified by the pattern that would own it, as a share of routed
millimetres (2026-09-20, before any R2 slice). This is the measurement R2 rests on: two thirds of
the copper is structured, so the maze router's real job is the remaining third.

| pattern | segments | mm | share |
|---|---|---|---|
| power spine | 486 | 554.4 | 37.0 % |
| general signal | 247 | 464.3 | 31.0 % |
| differential pair | 220 | 214.2 | 14.3 % |
| plane tap | 211 | 178.9 | 12.0 % |
| short hop | 97 | 84.8 | 5.7 % |

Per board, by share of that board's copper:

| board | power | signal | pair | plane tap | hop |
|---|---|---|---|---|---|
| blinky | — | 100 % | — | — | — |
| buck | 86 % | 9 % | — | — | 4 % |
| c3_usb | 66 % | 2 % | 27 % | — | 5 % |
| node | 5 % | 26 % | 26 % | 36 % | 8 % |
| ds2 | 38 % | 57 % | — | — | 5 % |

## S1 — the fresh baseline, and the fanout stubs straightened

The escape via's across-coordinate was being snapped to KRT's routing grid while the pad's own
coordinate was not, so every escape stub left its pad up to half a grid step sideways: 14 tilted
stubs, and with them most of pcbc's own contribution to the bar's off-45 count. Pattern copper
does not need to sit on the router's grid — it is locked, so KRT reads it as an obstacle and
never has to land on it — so the via now keeps the pad's across-coordinate exactly.

| board | vias | off 0/45/90 | under 0.2 mm | worst detour | routed mm |
|---|---|---|---|---|---|
| blinky | 0 | 0 | 2 | LED 1.03x | 22.5 |
| buck | 4 | 3 | 26 | VIN 2.09x | 142.5 |
| c3_usb | 16 | 7 → **5** | 183 | USB_DN 1.81x | 315.7 |
| node | 30 → **29** | 63 → **62** | 165 | T_OUT 1.72x | 511.1 |
| ds2 | 32 | 23 → **13** | 113 | REFP_F 3.23x | 514.6 |

Escape stubs off 0/45/90, before and after: c3_usb 2 → **0**, node 2 → **0**, ds2 10 → **0**.
Length and micro-segment counts move by a few because KRT routes around obstacles that shifted by
a few hundredths of a millimetre; the ceilings in `tests/test_examples_fab.py` are re-recorded
from this build, and ds2's are recorded for the first time in `test_copper_bar.py`.
