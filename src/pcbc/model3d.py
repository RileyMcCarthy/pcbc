"""Where a fetched part's 3D model sits on its footprint, measured by KiCad.

easyeda2kicad (1.0.1) writes `(offset (xyz 0 0 0))` on every part. It bakes EasyEDA's placement
`T` (the model's bounding-box centre and bottom go to `T`) into the `.wrl` mesh only, *before* KiCad's
rotation, and copies the STEP unchanged (`export_kicad_3d_model.py`, its own TODO says so). So a STEP
whose own origin is not its centre (the JST XH, the PZ254 2P/3P headers) or a part with a non-zero `T`
(the XH, the HRO USB-C) is drawn off its pads, and the `.wrl` is off too once the part's model
rotation is not 0 (the bake is turned with the model).

EasyEDA's placement is `R·(v - c) + T`: the model turned about its box centre `c` (x, y centre, z
bottom), then that point moved to `T`. KiCad draws `R·v + offset` (the model's `(rotate)` applied, then
its `(offset)`, both in the model frame, y up). So `offset = T - R·c`. Both terms are measured here
with KiCad's own model loader (`kicad-cli pcb export glb`), never computed from a rotation convention:

- `T` is the easyeda2kicad `.wrl`'s box at rotation 0 — the bake put its centre at `T` and its bottom
  at `T_z`;
- `R·c` is the kept model's box at the footprint's own `(rotate)`.

Measured on the twelve example parts (2026-10-03): XH `(1.25 2.7255 1.8)`, USB-C `(0 -2.45 0.033)`,
PZ254 2P `(1.265 0 0)`, 3P `(2.535 0 0.5)`; every part whose STEP is centred with `T = 0` moves by
under 0.07 mm (its bottom set on the board).
"""

from __future__ import annotations

import json
import re
import struct
import subprocess
import tempfile
from pathlib import Path

__all__ = ["glb_boxes", "model_offset", "model_rotate"]

Box = tuple[float, float, float, float, float, float]  # x0, y0, z0, x1, y1, z1 in the model frame (mm, y up)

_ROTATE = re.compile(r"\(rotate\s*\(xyz\s+([-0-9.eE+]+)\s+([-0-9.eE+]+)\s+([-0-9.eE+]+)\s*\)\s*\)")


def model_rotate(mod_text: str) -> tuple[float, float, float]:
    """The first `(model ...)` block's `(rotate (xyz a b c))` of a footprint text, (0, 0, 0) if none."""
    j = mod_text.find("(model ")
    if j < 0:
        return (0.0, 0.0, 0.0)
    m = _ROTATE.search(mod_text, j)
    return (float(m.group(1)), float(m.group(2)), float(m.group(3))) if m else (0.0, 0.0, 0.0)


def glb_boxes(path: Path) -> dict[str, Box]:
    """Each top-level-named node's mesh box of a `kicad-cli pcb export glb` file, by node name (a
    footprint's reference), in the model frame: glTF is metres and y up with z toward the viewer, so
    model x = X, model y = -Z, model z = Y (board top as KiCad puts it; callers take differences)."""
    raw = Path(path).read_bytes()
    if raw[:4] != b"glTF":
        raise ValueError(f"{path}: not a GLB file")
    off, doc = 12, None
    while off + 8 <= len(raw):
        length, kind = struct.unpack_from("<II", raw, off)
        if kind == 0x4E4F534A:  # JSON
            doc = json.loads(raw[off + 8 : off + 8 + length])
        off += 8 + length
    if doc is None:
        raise ValueError(f"{path}: GLB without a JSON chunk")

    def local(n: dict) -> list[list[float]]:
        if "matrix" in n:
            m = n["matrix"]
            return [[m[c * 4 + r] for c in range(4)] for r in range(4)]
        t = n.get("translation", [0.0, 0.0, 0.0])
        x, y, z, w = n.get("rotation", [0.0, 0.0, 0.0, 1.0])
        s = n.get("scale", [1.0, 1.0, 1.0])
        rot = [
            [1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
            [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
            [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)],
        ]
        return [[rot[r][0] * s[0], rot[r][1] * s[1], rot[r][2] * s[2], t[r]] for r in range(3)] + [[0.0, 0.0, 0.0, 1.0]]

    def mul(a, b):
        return [[sum(a[r][k] * b[k][c] for k in range(4)) for c in range(4)] for r in range(4)]

    boxes: dict[str, list[list[float]]] = {}

    def walk(i: int, m, owner: str | None) -> None:
        n = doc["nodes"][i]
        m2 = mul(m, local(n))
        owner = owner or n.get("name")
        if "mesh" in n and owner:
            for prim in doc["meshes"][n["mesh"]]["primitives"]:
                acc = doc["accessors"][prim["attributes"]["POSITION"]]
                lo, hi = acc["min"], acc["max"]
                for cx in (lo[0], hi[0]):
                    for cy in (lo[1], hi[1]):
                        for cz in (lo[2], hi[2]):
                            v = (cx, cy, cz, 1.0)
                            boxes.setdefault(owner, []).append([sum(m2[r][k] * v[k] for k in range(4)) for r in range(3)])
        for c in n.get("children", []):
            walk(c, m2, owner)

    eye = [[1.0 if r == c else 0.0 for c in range(4)] for r in range(4)]
    for i in doc["scenes"][doc.get("scene", 0)]["nodes"]:
        walk(i, eye, None)
    out: dict[str, Box] = {}
    for name, pts in boxes.items():
        xs = [p[0] * 1000.0 for p in pts]
        ys = [-p[2] * 1000.0 for p in pts]
        zs = [p[1] * 1000.0 for p in pts]
        out[name] = (min(xs), min(ys), min(zs), max(xs), max(ys), max(zs))
    return out


def _footprint(ref: str, model: Path, rotate: tuple[float, float, float]) -> str:
    return (
        f'\t(footprint "pcbc:probe" (layer "F.Cu") (at 0 0 0) (attr smd)\n'
        f'\t\t(property "Reference" "{ref}" (at 0 0 0) (layer "F.Fab") (hide yes) (effects (font (size 1 1))))\n'
        f'\t\t(model "{model}" (offset (xyz 0 0 0)) (scale (xyz 1 1 1)) (rotate (xyz {rotate[0]:g} {rotate[1]:g} {rotate[2]:g})))\n'
        "\t)\n"
    )


def model_offset(model: Path, placed: Path, rotate: tuple[float, float, float], cli: Path) -> tuple[tuple[float, float, float] | None, str]:
    """`((dx, dy, dz), "")` — the `(offset (xyz ...))` that puts `model`, drawn with `rotate`, where
    EasyEDA places it — or `(None, why)` when KiCad loads no geometry from one of the files. `placed`
    is easyeda2kicad's `.wrl` (EasyEDA's placement baked in). Rounded to 0.001 mm."""
    model, placed = Path(model).resolve(), Path(placed).resolve()
    with tempfile.TemporaryDirectory() as td:
        pcb = Path(td) / "probe.kicad_pcb"
        glb = Path(td) / "probe.glb"
        pcb.write_text(
            "(kicad_pcb (version 20260206) (generator \"pcbc\") (general (thickness 1.6))\n"
            '\t(layers (0 "F.Cu" signal) (2 "B.Cu" signal) (25 "Edge.Cuts" user))\n'
            + _footprint("MODEL", model, rotate)
            + _footprint("PLACED", placed, (0.0, 0.0, 0.0))
            + '\t(gr_rect (start -100 -100) (end 100 100) (stroke (width 0.1) (type solid)) (fill no) (layer "Edge.Cuts"))\n'
            ")\n"
        )
        run = subprocess.run(
            [str(cli), "pcb", "export", "glb", "--no-board-body", "--min-distance", "0.0001mm", "-f", "-o", str(glb), str(pcb)],
            capture_output=True,
            text=True,
            timeout=300,
        )
        if run.returncode != 0 or not glb.exists():
            tail = (run.stderr or run.stdout).strip().splitlines()
            return None, f"kicad-cli pcb export glb of {model.name} and {placed.name} exited {run.returncode}" + (f": {tail[-1][-200:]}" if tail else "")
        boxes = glb_boxes(glb)
    missing = [p.name for ref, p in (("MODEL", model), ("PLACED", placed)) if ref not in boxes]
    if missing:
        return None, f"KiCad loaded no geometry from {', '.join(missing)}"
    m, p = boxes["MODEL"], boxes["PLACED"]
    dx = (p[0] + p[3]) / 2 - (m[0] + m[3]) / 2
    dy = (p[1] + p[4]) / 2 - (m[1] + m[4]) / 2
    dz = p[2] - m[2]
    return (round(dx, 3) + 0.0, round(dy, 3) + 0.0, round(dz, 3) + 0.0), ""
