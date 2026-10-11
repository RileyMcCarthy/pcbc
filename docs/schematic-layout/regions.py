"""Proposed region inference (docs/schematic-layout.md §2.2), on the frozen tool's parts, scored
against the regions the board's own SchRegion/SchPlace lines draw today.

usage: regions.py BOARD.py [naive|refined]
naive:   anchors = symbols with more than two pins; a two-pin part joins the anchor it reaches first
         over signal nets, else (a decoupling cap) the anchor with most pins on its supply; hubs =
         anchors with signal nets to 3+ other anchors; regions = each hub alone + components of the
         rest.
refined: connectors (ref J*/P*) are anchors at any pin count; a decoupling cap goes to
         the pad a board.py Path() names next to it, else round-robin over the non-connector ICs on
         its rail (most pins first) in board.py order; a component with more than two anchors is
         split by cutting its anchor with most signal neighbours (ties: most pins) until none has.
v2:      (revision 2026-10-03, after review) refined's anchors and hub cut, plus:
         - a passive UNIT (two-pin parts joined to each other by signal nets) joins as one: the
           anchors on any of its signal nets are its candidates; a connector wins, else the anchor
           with the fewest pins (the part the passives serve), ties in board.py order. An RC filter
           between a connector and an IC is therefore never cut across two regions;
         - a decoupling cap a board.py Path() names next to an anchor's pad goes with that anchor (an
           IC before a connector). The other caps whose pins are all power/ground form one BANK per
           set of rails and join as one: the rail's regulator (an IC on this rail and on another
           non-ground rail), else the IC on the rail with most pins, else an anchor on the rail, else
           the region of the first part the rail reaches; a bulk and an HF cap are never split;
         - a connector with power/ground nets only joins the region of the first part its rail
           reaches (buck's J_IN, J_OUT), so a power entry is not a region of its own.
Every mode also prints a pairwise score against the hand regions (pairs of parts that share a
region: precision = shared in both / shared in inferred, recall = shared in both / shared by hand),
and for each inferred region beside a hub, the side of the hub the region's hub pins are drawn on
(majority of pins, x < 0 in the library is left) next to the side the hand sheet puts it on.
"""
from __future__ import annotations

import sys
from collections import defaultdict, deque
from pathlib import Path

import os

# The tool under measurement: PCBC_SRC, else this tree's src/. The numbers in
# docs/schematic-layout.md were taken on a frozen copy of the tree at 9225fd3 + the 2026-10-03 working tree.
SRC = Path(os.environ.get("PCBC_SRC") or Path(__file__).resolve().parents[2] / "src")
sys.path.insert(0, str(SRC))
from pcbc import sch_emit as E  # noqa: E402
from pcbc.language import load_board  # noqa: E402
from pcbc.sch_place import apply_sch_places  # noqa: E402

board = Path(sys.argv[1])
mode = sys.argv[2] if len(sys.argv) > 2 else "refined"
d = load_board(board)
kinds = {n.name: n.kind for n in d.nets.values()}
parts = E._parts_from_design(d, kinds)
by = {p.ref: p for p in parts}
order = [p.ref for p in parts]


def sig(n):
    return bool(n) and kinds.get(n, "net") not in ("power", "ground") and not n.startswith("unconnected") and not n.endswith(".NC")


if mode == "naive":
    anchors = [p.ref for p in parts if len(p.pins) > 2]
else:
    anchors = [p.ref for p in parts if len(p.pins) > 2 or p.ref[:1] in "JP"]
if mode == "v2":
    # Power-only connectors are not anchors here; they join below.
    power_only = [a for a in anchors if a[:1] in "JP" and not any(sig(pn.net) for pn in by[a].pins)]
    anchors = [a for a in anchors if a not in power_only]
else:
    power_only = []
on_net = defaultdict(set)
for p in parts:
    for pin in p.pins:
        if pin.net:
            on_net[pin.net].add(p.ref)
home = {a: a for a in anchors}
rr: dict[str, int] = defaultdict(int)
path_next: dict[str, str] = {}
for pr in getattr(d, "paths", []) or []:
    pads = [s.split(".", 1)[0] for s in pr.pads]
    for a, b in zip(pads, pads[1:]):
        if a not in anchors and b in anchors:
            path_next.setdefault(a, b)
        if b not in anchors and a in anchors:
            path_next.setdefault(b, a)
        if mode == "v2":
            # A cap a Path() names between a connector and an IC goes with the IC (it decouples the pin).
            for cap, other in ((a, b), (b, a)):
                if cap in path_next and path_next[cap][:1] in "JP" and other in anchors and other[:1] not in "JP":
                    path_next[cap] = other
if mode == "v2":
    rails_of = {p.ref: tuple(sorted({pn.net for pn in p.pins if pn.net and not sig(pn.net)})) for p in parts}
    passives = [p.ref for p in parts if p.ref not in anchors and p.ref not in power_only]
    # Units: passives joined to each other through signal nets.
    unit_of: dict = {}
    for r in passives:
        if r in unit_of:
            continue
        unit, q = [r], [r]
        unit_of[r] = r
        while q:
            cur = q.pop()
            for pin in by[cur].pins:
                if not sig(pin.net):
                    continue
                for o in sorted(on_net[pin.net], key=order.index):
                    if o in passives and o not in unit_of:
                        unit_of[o] = r
                        unit.append(o)
                        q.append(o)
    units = defaultdict(list)
    for r in passives:
        units[unit_of[r]].append(r)
    for key, unit in units.items():
        nets = {pin.net for u in unit for pin in by[u].pins if sig(pin.net)}
        if not nets:
            continue  # a decoupling bank, below
        cands = sorted({o for n in nets for o in on_net[n] if o in anchors}, key=order.index)
        conns = [c for c in cands if c[:1] in "JP"]
        pick = conns[0] if conns else (min(cands, key=lambda a: (len(by[a].pins), order.index(a))) if cands else None)
        for u in unit:
            home[u] = pick or key
    # Banks: decoupling caps by rail set.
    banks = defaultdict(list)
    for r in passives:
        if r not in home:
            banks[rails_of[r]].append(r)
    power_rails = lambda a: {pn.net for pn in by[a].pins if pn.net and kinds.get(pn.net) == "power"}
    for rails, bank in banks.items():
        # A cap a Path() names next to an anchor's pad goes there on its own; the rest stay one bank.
        for c in [c for c in bank if c in path_next]:
            home[c] = path_next[c]
        bank = [c for c in bank if c not in path_next]
        if not bank:
            continue
        pick = None
        rail = [n for n in rails if kinds.get(n) == "power"]
        on_rail = [a for a in anchors if power_rails(a) & set(rail)]
        if pick is None:
            regs = [a for a in on_rail if a[:1] not in "JP" and len(power_rails(a)) >= 2]
            ics = sorted((a for a in on_rail if a[:1] not in "JP"), key=lambda a: (-len(by[a].pins), order.index(a)))
            pick = (regs or ics or on_rail or [None])[0]
        if pick is None:
            # No anchor on the rail: the first part the rail reaches that has a home.
            pick = next((home[o] for n in rail for o in sorted(on_net[n], key=order.index) if o in home), bank[0])
        for c in bank:
            home[c] = pick
    for c in power_only:
        rail = [pn.net for pn in by[c].pins if kinds.get(pn.net) == "power"]
        home[c] = next((home[o] for n in rail for o in sorted(on_net[n], key=order.index) if o in home and o != c), c)
for p in parts:
    if p.ref in home:
        continue
    seen, q, found = {p.ref}, deque([p.ref]), None
    while q and not found:
        cur = q.popleft()
        for pin in by[cur].pins:
            if not sig(pin.net):
                continue
            for o in sorted(on_net[pin.net], key=order.index):
                if o in anchors:
                    found = o
                    break
                if o not in seen:
                    seen.add(o)
                    q.append(o)
            if found:
                break
    if not found:
        rails = [pin.net for pin in p.pins if kinds.get(pin.net) == "power"]
        if mode == "naive":
            found = max(anchors, key=lambda a: (sum(1 for pn in by[a].pins if pn.net in rails), -anchors.index(a)), default=None)
        elif p.ref in path_next:
            found = path_next[p.ref]
        else:
            ics = [a for a in anchors if a[:1] not in "JP" and any(pn.net in rails for pn in by[a].pins)]
            ics.sort(key=lambda a: (-len(by[a].pins), order.index(a)))
            if ics:
                key = ",".join(rails)
                found = ics[rr[key] % len(ics)]
                rr[key] += 1
    home[p.ref] = found or p.ref
adj = defaultdict(set)
for net, refs in on_net.items():
    if not sig(net):
        continue
    hs = {home[r] for r in refs}
    for a in hs:
        adj[a] |= hs - {a}


def comps(nodes):
    out, done = [], set()
    for a in nodes:
        if a in done:
            continue
        comp, q = [], [a]
        done.add(a)
        while q:
            c = q.pop()
            comp.append(c)
            for o in adj[c]:
                if o in nodes and o not in done:
                    done.add(o)
                    q.append(o)
        out.append(comp)
    return out


if mode == "naive":
    hubs = [a for a in anchors if len(adj[a]) >= 3]
    regions = [[h] for h in hubs] + comps([a for a in anchors if a not in hubs])
else:
    regions, work = [], comps(list(anchors))
    while work:
        c = work.pop(0)
        if len(c) <= 2:
            regions.append(c)
            continue
        cut = max(c, key=lambda a: (len(adj[a] & set(c)), len(by[a].pins), -order.index(a)))
        regions.append([cut])
        work += comps([a for a in c if a != cut])
inferred = {}
for i, r in enumerate(regions):
    for ref, h in home.items():
        if h in r:
            inferred[ref] = i
# The regions today's hand lines draw.
hand = {}
if d.sch_regions:
    ps = E._parts_from_design(d, kinds)
    apply_sch_places(d, ps)
    hand = {p.ref: getattr(p, "sch_group", None) for p in ps}
agree = 0
if hand:
    # Each inferred region takes the hand region most of its members are in.
    label = {}
    for i in range(len(regions)):
        votes = defaultdict(int)
        for ref, j in inferred.items():
            if j == i:
                votes[hand.get(ref)] += 1
        label[i] = max(votes, key=votes.get) if votes else None
    agree = sum(1 for ref, j in inferred.items() if label[j] == hand.get(ref))
for i, r in enumerate(regions):
    members = sorted((ref for ref, j in inferred.items() if j == i and ref not in r), key=order.index)
    print(f"  [{' + '.join(r)}] <- {', '.join(members)}")
n_hand = len({v for v in hand.values() if v})
if hand:
    refs = [p.ref for p in parts]
    same_i = {(a, b) for i, a in enumerate(refs) for b in refs[i + 1 :] if inferred.get(a) == inferred.get(b)}
    same_h = {(a, b) for i, a in enumerate(refs) for b in refs[i + 1 :] if hand.get(a) and hand.get(a) == hand.get(b)}
    both = same_i & same_h
    prec = len(both) / len(same_i) if same_i else 1.0
    rec = len(both) / len(same_h) if same_h else 1.0
    print(f"  pairwise: precision {len(both)}/{len(same_i)} = {prec:.2f}, recall {len(both)}/{len(same_h)} = {rec:.2f}")
    unhomed = [r for r in refs if r not in inferred]
    if unhomed:
        print(f"  in no inferred region: {', '.join(unhomed)}")
    # Sides: for each region that shares a signal net with a hub, the side of the hub its pins are on.
    ps = E._parts_from_design(d, kinds)
    apply_sch_places(d, ps)
    pos = {p.ref: (p.x, p.y) for p in ps}
    hubs = [r[0] for r in regions if len(r) == 1 and len(adj[r[0]]) >= 2]
    for hub in hubs:
        hi = next(i for i, r in enumerate(regions) if r == [hub])
        for i, r in enumerate(regions):
            if i == hi:
                continue
            nets = {pin.net for ref, j in inferred.items() if j == i for pin in by[ref].pins if sig(pin.net)}
            xs = [pin.lx for pin in by[hub].pins if pin.net in nets]
            if not xs:
                continue
            left = sum(1 for x in xs if x < 0)
            right = len(xs) - left
            pin_side = "left" if left > right else "right" if right > left else "both"
            mem = [ref for ref, j in inferred.items() if j == i]
            cx = sum(pos[m][0] for m in mem) / len(mem)
            hand_side = "left" if cx < pos[hub][0] else "right"
            print(f"  beside hub {hub}: [{' + '.join(r)}] hub pins left {left} / right {right} -> {pin_side}; hand sheet: {hand_side}")
print(f"{board.stem} {mode}: {len(regions)} regions inferred, {n_hand} hand regions, parts in the matching hand region {agree}/{len(parts)}" if hand else f"{board.stem} {mode}: {len(regions)} regions inferred, no hand regions")
