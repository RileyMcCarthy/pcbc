"""Via groups at a layer change (`docs/net-path-physics.md` §4.3, PR 4; owner's answer 2).

A layer change carrying a current places the flow-through line its current needs: `n` barrels at the
board's group pitch, both layers' copper over every barrel, `n` the smallest whose worst barrel (lumped
DC model) is within `via_amps`. The router prices it like length and takes it only when it is the
cheaper of "change layers" and "stay on one layer". More than one barrel is never a reason to refuse.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from pcbc import stackup
from pcbc.language import load_board
from pcbc.layout_emit import stamp
from pcbc.layout_job import load_core
from pcbc.place_native import place
from pcbc.route_cost import Router, link_cost, run_cost
from pcbc.route_native import GROUP_REASON, net_links, route_stage
from pcbc.route_scene import build_scene
from conftest import put_core, write_split

BOARD = '''from pcbc import *
Board(width=30, height=20, layers=2, stackup="jlcpcb_2l_1oz")
P = Power("PWR")
Resistor("R1", "0R", package="0805", mpn="X", lcsc="C1", p1=P, p2="A")
Resistor("R2", "0R", package="0805", mpn="X", lcsc="C1", p1=P, p2="B")
Resistor("R3", "0R", package="0805", mpn="X", lcsc="C1", p1="A", p2="B")
Resistor("R4", "0R", package="0805", mpn="X", lcsc="C1", p1=P, p2="B")
NetReq("PWR", kind="power")
Path("PWR", "R1.1", "R2.1", amps=2)
Place("R1", at=(4, 4))
Place("R2", at=(26, 4))
Place("R3", at=(15, 19))
Place("R4", at=(26, 12))
'''


def _routed(tmp_path: Path, wall_to: float):
    """The board above with a no-net wall on F.Cu at x = 15 from y = 0.6 to `wall_to`: the 2 A path
    from R1.1 to R2.1 either changes layer twice or detours round the wall's end."""
    write_split(tmp_path / "b.py", BOARD)
    put_core(tmp_path / "layout.core.py", f'Seg(None, (15, 0.6), (15, {wall_to}), width=0.3, layer="F.Cu")\n')
    design = load_board(tmp_path / "b.py")
    pl = place(design, name="b", base=tmp_path / "layout" / "b" / "routed")
    assert not pl.error, pl.error
    raw, _gr = load_core(design, tmp_path / "b.py", pl, name="b")
    core = stamp(design, raw)
    out = route_stage(design, pl.job, pl, name="b", core_cu=core, relax=False)
    return design, pl, core, out


def test_the_model_reproduces_the_table():
    """§4.3's table, recomputed: (count, worst barrel) at 0.95 and 1.3 mm pitch, landing at the wider width."""
    got = {amps: [stackup.via_group(amps, 0.4, 0.018, 10, p, w, board_mm=1.6) for p in (0.95, 1.3)] for amps, w in ((1.0, 0.309), (1.5, 0.646), (2.0, 1.09), (2.5, 1.634), (3.0, 2.276))}
    assert got == {
        1.0: [(2, 0.5), (2, 0.5)],
        1.5: [(2, 0.75), (2, 0.75)],
        2.0: [(3, 0.732), (3, 0.75)],
        2.5: [(4, 0.748), (4, 0.782)],
        3.0: [(4, 0.862), (5, 0.817)],
    }
    # A narrower landing crowds the end barrels: the worst share grows as the landing narrows.
    assert max(stackup.via_group_shares(3, 2.0, 0.4, 0.018, 1.6, 1.3, 0.781)) > max(stackup.via_group_shares(3, 2.0, 0.4, 0.018, 1.6, 1.3, 1.09))


def test_a_wall_makes_the_path_change_layers_through_two_groups(tmp_path: Path):
    design, pl, core, out = _routed(tmp_path, 18.5)
    assert "PWR" not in out.unrouted, out.unrouted
    groups = [g for g in out.stats.get("groups", []) if g["net"] == "PWR"]
    assert len(groups) == 2, groups
    rating = stackup.via_amps(0.4, 0.018, 10)
    for g in groups:
        assert g["count"] == 3 and g["pitch"] >= 0.95 - 1e-9 and g["worst_a"] <= rating + 1e-9, g
        assert g["landing"] == 1.09 and g["link"] == "R1.1~R2.1", g
    barrels = [p for p in out.pieces if p.net == "PWR" and p.kind == "via"]
    assert len(barrels) == 6 and all(p.reason == GROUP_REASON for p in barrels), "no lone via carries the current"
    landing = [p for p in out.pieces if p.net == "PWR" and p.reason == GROUP_REASON and p.kind == "seg"]
    assert len(landing) == 4 and {p.layer for p in landing} == {"F.Cu", "B.Cu"} and all(p.w == 1.09 for p in landing)
    b_cu = [p for p in out.pieces if p.net == "PWR" and p.reason == "route" and p.layer == "B.Cu"]
    assert b_cu and all(p.w == 1.09 for p in b_cu), "B.Cu has no plane under it on two layers: 1.09 mm"


def test_the_router_takes_the_cheaper_of_group_and_detour(tmp_path: Path):
    """`Router.link` twice on the same scene, once with vias forbidden: the route it chose costs no more
    than the single-layer one, and where the wall is short the single-layer route is cheaper and no
    group is placed."""
    for wall_to, want_group in ((18.5, True), (7.0, False)):
        work = tmp_path / str(wall_to)
        work.mkdir()
        design, pl, core, out = _routed(work, wall_to)
        groups = [g for g in out.stats.get("groups", []) if g["net"] == "PWR"]
        assert bool(groups) == want_group, (wall_to, groups)
        cs = pl.job.constraints
        scene = build_scene(design, pl.job, cs, pl.feet)
        from pcbc.route_native import _core_piece

        scene.add(scene.item_of(p) for c in core for p in _core_piece(c))
        nc = link_cost(cs, "PWR", "R1.1", "R2.1")
        terms = {t.owner: t for t in net_links(design, scene, "PWR", (), cs)[0]}
        a, b = terms["R1.1"], terms["R2.1"]

        def cost(nc_):
            r = Router(scene, nc_, ("F.Cu", "B.Cu"), exempt=frozenset({"R1", "R2"}))
            runs = r.link(a.item.at(), b.item.at(), pa=a.item, pb=b.item)
            if runs is None:
                return None
            return sum(run_cost(pts, nc_, L) for L, pts in runs) + sum(g.cost for g in r.groups if g is not None)

        free = cost(nc)
        flat = cost(replace(nc, via_allowed=False, group=None))
        assert free is not None
        assert flat is None or free <= flat + 1e-9, (wall_to, free, flat)


def test_a_current_links_terminals_are_its_pads(tmp_path: Path):
    """A link carrying a current is laid pad to pad; a via already on the net is never its terminal
    (§4.3, by current)."""
    design, pl, core, out = _routed(tmp_path, 18.5)
    cs = pl.job.constraints
    scene = build_scene(design, pl.job, cs, pl.feet)
    terms, links, _uf = net_links(design, scene, "PWR", (), cs)
    first = links[0]
    assert first[2] is not None and first[2].key == "R1.1~R2.1"
    assert (terms[first[0]].owner, terms[first[1]].owner) == ("R1.1", "R2.1")


@pytest.mark.parametrize("amps", [0.5, 2.0])
def test_one_barrel_needs_no_group(amps):
    n, worst = stackup.via_group(amps, 0.4, 0.018, 10, 1.3, 0.781, board_mm=1.6)
    assert (n == 1) == (amps <= stackup.via_amps(0.4)), (amps, n, worst)
