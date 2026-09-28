"""A footprint as the router reads it: its `.kicad_mod` parsed once, posed by a `Pose`.

`docs/direction.md` §1: placement and routing read the Python model — the parts from their
`.kicad_mod`, `board.py`, `layout.core.py` — and no KiCad board text sits in between. This module is
the footprint half of that. `lib_foot` reads one `.kicad_mod` (the part's own file, the only
s-expression the router reads) into a `LibFoot`: every pad as a `pads.PadSpec` in the library frame
and the courtyard's local box. `posed_feet` pairs each `Pose` of `layout.gen.py` with its part's
`LibFoot` and the pad-to-net binding `board.py` makes; `route_scene.build_scene` turns the result into
obstacles (`pad_geoms_of`) and lanes (`foot_of`).

The pose arithmetic is KiCad's own and is written once here: a pad's position is its library
position turned by the footprint's angle, and its absolute angle is the library angle plus the
footprint's (`posed`). The fiducials the place stage adds have no `.kicad_mod`; their one definition
is `fab._fiducial_sexp`, read by the same parser.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path

from .pads import PadGeom, PadSpec, geoms_of, pad_specs

__all__ = ["LibFoot", "PosedFoot", "foot_of", "lib_foot", "lib_foot_text", "pad_geoms_of", "posed", "posed_feet"]


@dataclass(frozen=True)
class LibFoot:
    """One footprint definition: its pads in the library frame, and its courtyard's local box
    (None when it draws no courtyard) — and what placement asks of the part's own drawing, read once
    from the same `.kicad_mod` so the place stage reads no board text either (`docs/direction.md` §1;
    the third refutation round, native major):

    - `boxes`: the CSS border boxes `geom.footprint_box_local` gives (`courtyard`, `fab`, `pads`);
    - `silk_crtyd`, `silk_silk`: the `F.CrtYd` and `F.SilkS` extents the silkscreen pass keeps its
      references off (`silk._bbox_on_layer`'s reading);
    - `silk_pads`: every pad as the silkscreen pass reads it, `(x, y, library angle, w, h)`;
    - `locked`: the footprint's own `(locked yes)`.
    """

    pads: tuple[PadSpec, ...]
    courtyard: tuple[float, float, float, float] | None
    boxes: tuple[tuple[str, tuple[float, float, float, float]], ...] = ()
    silk_crtyd: tuple[float, float, float, float] | None = None
    silk_silk: tuple[float, float, float, float] | None = None
    silk_pads: tuple[tuple[float, float, float, float, float], ...] = ()
    locked: bool = False
    uuids: tuple[str, ...] = ()  # every `(uuid ...)` of the part's drawing the board copy keeps (`seed.instantiate`)
    props: frozenset[str] = frozenset()  # the `(property "...")` names the `.kicad_mod` already carries

    def box(self, kind: str) -> tuple[float, float, float, float]:
        """`geom.footprint_box_local(block, kind)` of this footprint."""
        if kind == "origin":
            return (0.0, 0.0, 0.0, 0.0)
        got = dict(self.boxes)
        if kind not in got:
            raise ValueError(f"unknown box kind {kind!r}")
        return got[kind]


@dataclass(frozen=True)
class PosedFoot:
    """One part on the board: a `Pose` and the `LibFoot` it poses. `rot` is the pose's angle in
    [0, 360). `nets` binds pad numbers to nets, from `board.py`."""

    ref: str
    at: tuple[float, float]
    rot: float
    layer: str
    lib: LibFoot
    nets: tuple[tuple[str, str], ...] = ()

    def net_map(self) -> dict[str, str]:
        return dict(self.nets)


def lib_foot_text(mod: str) -> LibFoot:
    """A `.kicad_mod`'s text (or any single footprint s-expression in the library frame) as a
    `LibFoot`. Zero-length and repeated `fp_line`s are dropped first, as `seed.instantiate` drops them
    from the copy it puts on the board (they draw nothing; one could stretch the courtyard box)."""
    import re

    from .geom import _graphics_bbox, footprint_box_local
    from .seed import drop_degenerate_lines
    from .sexp import matching_paren
    from .silk import _PAD_RE, _bbox_on_layer

    j = mod.find("(footprint ")
    body = mod[j : matching_paren(mod, j) + 1] if j >= 0 else mod
    body = drop_degenerate_lines(body)
    boxes = tuple((k, footprint_box_local(body, k)) for k in ("courtyard", "fab", "pads"))
    crtyd = _bbox_on_layer(body, "F.CrtYd", ("fp_line", "fp_rect", "fp_poly", "fp_circle"))
    silk = _bbox_on_layer(body, "F.SilkS", ("fp_line", "fp_rect", "fp_poly", "fp_circle", "fp_arc"))
    spads = tuple((float(m.group(1)), float(m.group(2)), float(m.group(3) or 0), float(m.group(4)), float(m.group(5))) for m in _PAD_RE.finditer(body))
    # The footprint's own lock, as `place_native` reads a pose's (a line of the footprint's head).
    head = body[: body.find("(pad ")] if "(pad " in body else body
    locked = re.search(r"\n\s*\(locked yes\)", head) is not None
    # What `seed.instantiate` keeps of the drawing (everything after the footprint's `(layer ...)`
    # line) carries these uuids onto the board; `place_native.foreign_uuids` names them.
    k = body.find("\n", body.find("(layer ")) if "(layer " in body else -1
    kept = body[k + 1 :] if k >= 0 else body
    uuids = tuple(re.findall(r'\(uuid\s+"([^"]+)"\)', kept))
    props = frozenset(re.findall(r'\(property\s+"([^"]+)"', body))
    return LibFoot(pad_specs(body), _graphics_bbox(body, "CrtYd"), boxes, crtyd, silk, spads, locked, uuids, props)


_CACHE: dict[tuple[str, int, int], LibFoot] = {}


def lib_foot(path: Path) -> LibFoot:
    """`lib_foot_text` of one `.kicad_mod`, cached by (path, mtime, size): a part file is a source."""
    st = Path(path).stat()
    key = (str(Path(path).resolve()), st.st_mtime_ns, st.st_size)
    got = _CACHE.get(key)
    if got is None:
        got = _CACHE[key] = lib_foot_text(Path(path).read_text())
    return got


_FID: list[LibFoot] = []


def _fiducial() -> LibFoot:
    """The fiducial's `LibFoot`, read once from its one definition (`fab._fiducial_sexp`, which is
    what a `.kicad_mod` is to a part)."""
    from .fab import _fiducial_sexp

    if not _FID:
        _FID.append(lib_foot_text(_fiducial_sexp("FID", 0.0, 0.0)))
    return _FID[0]


def posed_feet(design, poses) -> tuple[PosedFoot, ...]:
    """Every `Pose` with its part's `LibFoot`, in ref order. A pose whose ref names no part is a
    fiducial the place stage added (`FIDn`); any other is refused."""
    from .footprints import footprint_path
    from .pcb_place import _pins_of

    parts = {inst.ref: inst for inst in design.instances}
    nets_of = _pins_of(design)
    out: list[PosedFoot] = []
    for p in sorted(poses, key=lambda p: p.ref):
        inst = parts.get(p.ref)
        if inst is not None:
            lib = lib_foot(footprint_path(inst.part))
        elif p.ref.startswith("FID"):
            lib = _fiducial()
        else:
            raise ValueError(f"a Pose for {p.ref!r}, which is no part of board.py and no fiducial")
        out.append(PosedFoot(p.ref, (float(p.at[0]), float(p.at[1])), float(p.rot) % 360.0, p.layer, lib, tuple(sorted(nets_of.get(p.ref, {}).items()))))
    return tuple(out)


def posed(pf: PosedFoot) -> tuple[PadSpec, ...]:
    """The pads with their absolute angle: library angle plus the footprint's, in [0, 360) (KiCad's
    board-file convention, which `pads.geoms_of` reads)."""
    out = []
    for sp in pf.lib.pads:
        a = (sp.angle + pf.rot) % 360.0
        out.append(replace(sp, angle=0.0 if abs(a) < 1e-9 else a))
    return tuple(out)


def pad_geoms_of(pf: PosedFoot, layers: tuple[str, ...]) -> tuple[PadGeom, ...]:
    """Every pad of the posed footprint in world coordinates (`pads.PadGeom`). No `side`: a part's
    pads are its `.kicad_mod`'s layers as written (B-side placement does not mirror them yet)."""
    return geoms_of(posed(pf), (pf.at[0], pf.at[1], pf.rot), ref=pf.ref, nets=pf.net_map(), layers=layers)


def foot_of(pf: PosedFoot):
    """The placement view of the posed footprint (`pcb_place.Foot`): pads in the footprint's frame as
    `(size w h)` boxes (a custom pad's primitives' extent, w and h swapped for a pad turned 90 in its
    own frame), the box = courtyard (or the pads when there is none) joined with the pads' box, bound
    to `board.py`'s nets, posed."""
    from .css import rotate_local_bounds
    from .pcb_place import Foot, Pad

    nets = pf.net_map()
    pads = []
    xs: list[float] = []
    ys: list[float] = []
    for sp in pf.lib.pads:
        w, h = sp.w, sp.h
        if sp.custom is not None:
            w, h = max(w, sp.custom[0]), max(h, sp.custom[1])
        if round(sp.angle) % 180 == 90:
            w, h = h, w
        pads.append(Pad(sp.num, sp.x, sp.y, w, h, nets.get(sp.num, sp.net)))
        x0, y0, x1, y1 = rotate_local_bounds(-sp.w / 2.0, -sp.h / 2.0, sp.w / 2.0, sp.h / 2.0, sp.angle)
        xs += [sp.x + x0, sp.x + x1]
        ys += [sp.y + y0, sp.y + y1]
    pb = (min(xs), min(ys), max(xs), max(ys)) if xs else None
    crt = pf.lib.courtyard or pb or (-0.5, -0.5, 0.5, 0.5)
    pb = pb or (0.0, 0.0, 0.0, 0.0)
    box = (min(crt[0], pb[0]), min(crt[1], pb[1]), max(crt[2], pb[2]), max(crt[3], pb[3]))
    f = Foot(pf.ref, box, pads)
    f.at, f.rot, f.layer = pf.at, pf.rot, pf.layer
    return f
