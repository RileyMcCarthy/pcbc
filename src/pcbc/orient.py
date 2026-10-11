"""Orientation marks: every part whose orientation matters carries one visible, unambiguous pin-1, cathode or
`+` mark on the front silkscreen, measured on the board (docs/direction.md §4c).

JLCPCB places by the silkscreen (their assembly FAQ: the placement "according to the silkscreen layer by
default"); a part with no mark stops the order, and a mark that names the wrong pin is worse than none. So:

**G1, need.** A part's pads *fit another way round* when a turn of 90, 180 or 270 degrees about the centre of
its pads lands every pad, an unnumbered peg included, on a pad of the same kind, shape and size. The turned
part *is the same part* when every numbered pad lands on a pad of the same symbol pin, or when it is a part
whose turned self is itself: every pin named only by its one pad's number, a body outline the turn maps onto
itself, and not a diode (`kind="led"`, prefix `D`) — a resistor, a ceramic capacitor, an inductor, a
two-terminal switch, a plain pin header. A part that fits another way round and is not then the same part
needs a mark (`required`). A polarised part (any part but those self-same ones) whose pads fit one way only
gets one where there is room (`optional`; JLC recommends a pin-1 dot on every IC). The rest need none.

**Which pin.** A two-terminal part with a pin named `K` (cathode) is marked at the cathode, with a bar; one
with a pin named `+`, at the `+`, with a `+`; every other part at pad `1` (or `A1`), with a dot.

**G2-G6, a good mark.** Every stroke at least the fab's silk line (0.15 mm), the mark at least `DOT_MIN`
across (G2); `AIM_MM`, else the fab's clearance, from every pad's mask opening and from all other silk, inside
the board edge (G3); outside every part's body — the `F.Fab` drawing's box, else the `F.CrtYd` box (a fetched
EasyEDA footprint's courtyard is its body) — so it is seen after assembly (G4); at most `GAP_MM` from the marked
pad's opening — from the body's edge when the pad sits under the body (a through-hole housing, an LGA) — and it
names its pad (G5): within the part, the marked pad the nearest of its pads by `MARGIN_MM` (or the mark in line
with it, `_in_line`); across parts, its part the nearest by `MARGIN_MM`, each part's distance its nearest pad's
opening or, for a pad under the body, the body's edge (`Scene.pad_dist`). A dot or a `+` is read against every pad
on the board (a resistor's, a fiducial's); a directional mark (a chevron, a cathode bar) against the parts that
take a mark. A part's printed marks that name another of its pads, another part, or (a dot) no pad contradict the
good one (G6): a library mark of that kind is not printed, and the fact says why; a `layout.core.py` drawing that
does, or that names its pin and breaks G2-G5, fails the build naming the rule. Which part a board drawing belongs
to is measured the same way, over every part (`Scene.owner_of`).

**Kept, generated, failed.** A part with a good printed mark (its library's, or a `layout.core.py` drawing)
keeps it and gets nothing. Otherwise a mark is generated where the measurement finds a legal spot — a filled
`gr_circle` dot `DOT_MIN`-`DOT_MAX` across (the marked pad's narrower side, then smaller), a filled `gr_poly`
cathode bar `BAR_W` wide and the pad's width plus 0.2 mm long past the cathode pad's outer end, or a filled
`gr_poly` `+` — a board primitive in `layout.gen.py` like any other (lockable by copying its line into
`layout.core.py`). A required part with no legal spot fails the build with the fact: which part, which pad,
and what blocked every spot tried. Only front-side parts are judged (pcbc prints no back silkscreen).
"""

from __future__ import annotations

import math

from .silk import _clip, _rot_pt, is_mark

DOT_MIN = 0.40  # a dot or mark at least this across (Tom Hausherr, pcblibraries.com: "the minimum Dot diameter should be 0.40 mm")
DOT_MAX = 1.00  # the largest dot generated (0.40-1.00 sized to the pad)
BAR_W = (0.30, 0.25, 0.20)  # cathode bar widths tried, widest first
BAR_PAST = 0.2  # a cathode bar is the cathode pad's width plus this long
PLUS_MM = 0.60  # a + sign's arms, ink end to end
GAP_MM = 1.0  # G5: a mark at most this far from its pad's opening (edge to edge)
MARGIN_MM = 0.2  # G5: every other pad at least this much further from the mark than its own pad
OWN_MM = 3.0  # a board drawing farther than this from every part (`Scene.pad_dist`: the body's edge for a pad under it) is no part's mark
AIM_MM = 0.20  # G3: the clearance tried first; the fab's own (0.15) only when no spot keeps this
STEP = 0.05  # the search grid
FIT_TOL = 0.03  # two pads coincide within this (and a pad's size matches another's)
BODY_TOL = 0.05  # a body outline maps onto itself within this
CATHODE = ("K", "KA", "CATHODE")
PLUS = ("+",)
ROTS = (90, 180, 270)
POLAR_PREFIX = ("D", "LED")
IC_PREFIX = ("U", "IC", "Q")  # the parts that get a pin-1 dot where there is room even when their pads fit one way only
_DEBUG = False


# ------------------------------------------------------------------ geometry (world frame, mm)


def _pt_seg(p, a, b) -> float:
    ax, ay = a
    dx, dy = b[0] - ax, b[1] - ay
    L2 = dx * dx + dy * dy
    t = 0.0 if L2 < 1e-18 else max(0.0, min(1.0, ((p[0] - ax) * dx + (p[1] - ay) * dy) / L2))
    return math.hypot(p[0] - ax - t * dx, p[1] - ay - t * dy)


def _cross(o, a, b) -> float:
    return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])


def _seg_seg(a, b, c, d) -> float:
    d1, d2, d3, d4 = _cross(c, d, a), _cross(c, d, b), _cross(a, b, c), _cross(a, b, d)
    if ((d1 > 0) != (d2 > 0)) and ((d3 > 0) != (d4 > 0)) and d1 and d2 and d3 and d4:
        return 0.0
    return min(_pt_seg(a, c, d), _pt_seg(b, c, d), _pt_seg(c, a, b), _pt_seg(d, a, b))


def _inside(p, poly) -> bool:
    x, y = p
    hit = False
    n = len(poly)
    for i in range(n):
        (x1, y1), (x2, y2) = poly[i], poly[(i + 1) % n]
        if (y1 > y) != (y2 > y) and x < x1 + (y - y1) * (x2 - x1) / (y2 - y1):
            hit = not hit
    return hit


def _is_plus(pts) -> bool:
    """A `+` drawn as one polygon: twelve corners, every edge level or plumb."""
    if len(pts) != 12:
        return False
    return all(abs(a[0] - b[0]) < 1e-6 or abs(a[1] - b[1]) < 1e-6 for a, b in zip(pts, pts[1:] + pts[:1]))


def _dotlike(ink) -> bool:
    """A mark read only by what it is nearest — a dot, a `+` — rather than by where it points."""
    return "disc" in ink or bool(ink.get("plus"))


def box_ink(b) -> dict:
    """A box (x0, y0, x1, y1) as filled ink: a pad's mask opening."""
    return {"pts": ((b[0], b[1]), (b[2], b[1]), (b[2], b[3]), (b[0], b[3])), "closed": True, "filled": True, "hw": 0.0}


def poly_ink(pts, hw: float = 0.0) -> dict:
    return {"pts": tuple(pts), "closed": True, "filled": True, "hw": hw}


def ink_of(shape: dict) -> dict:
    """The ink of a world-frame silk shape (`silk.drawings` moved to the board): a circle is a disc (its outer
    edge: a ring is judged by where it reaches), anything else its points with half its line width."""
    hw = shape["width"] / 2
    if shape["tag"] == "fp_circle":
        pts = shape["pts"]
        cx = sum(p[0] for p in pts) / len(pts)
        cy = sum(p[1] for p in pts) / len(pts)
        r = max(math.hypot(p[0] - cx, p[1] - cy) for p in pts)
        return {"disc": (cx, cy, r + hw), "hw": hw, "filled": bool(shape["fill"])}
    out = {"pts": tuple(shape["pts"]), "closed": bool(shape["closed"]), "filled": bool(shape["fill"]) and len(shape["pts"]) > 2, "hw": hw}
    if shape["tag"] == "fp_poly" and _is_plus(list(shape["pts"])):
        out["plus"] = True
    return out


def _edges(ink):
    pts = ink["pts"]
    if len(pts) == 1:
        return [(pts[0], pts[0])]
    pairs = list(zip(pts, pts[1:]))
    if ink["closed"]:
        pairs.append((pts[-1], pts[0]))
    return pairs


def ink_box(ink) -> tuple[float, float, float, float]:
    if "disc" in ink:
        cx, cy, R = ink["disc"]
        return (cx - R, cy - R, cx + R, cy + R)
    xs = [p[0] for p in ink["pts"]]
    ys = [p[1] for p in ink["pts"]]
    hw = ink["hw"]
    return (min(xs) - hw, min(ys) - hw, max(xs) + hw, max(ys) + hw)


def dist(a: dict, b: dict) -> float:
    """Edge-to-edge distance between two inks; negative (or 0) when they touch or overlap."""
    if "disc" in a and "disc" in b:
        (x1, y1, r1), (x2, y2, r2) = a["disc"], b["disc"]
        return math.hypot(x1 - x2, y1 - y2) - r1 - r2
    if "disc" in b:
        a, b = b, a
    if "disc" in a:
        cx, cy, R = a["disc"]
        if b["filled"] and _inside((cx, cy), b["pts"]):
            return -R
        return min(_pt_seg((cx, cy), p, q) for p, q in _edges(b)) - R - b["hw"]
    if a["filled"] and any(_inside(p, a["pts"]) for p in b["pts"]):
        return -1.0
    if b["filled"] and any(_inside(p, b["pts"]) for p in a["pts"]):
        return -1.0
    return min(_seg_seg(p, q, r, s) for p, q in _edges(a) for r, s in _edges(b)) - a["hw"] - b["hw"]


def _near(b1, b2, d: float) -> bool:
    return not (b1[2] + d < b2[0] or b2[2] + d < b1[0] or b1[3] + d < b2[1] or b2[3] + d < b1[1])


# ------------------------------------------------------------------ G1: does the part need a mark, and where


def _pin_names(op: dict) -> dict[str, str]:
    out: dict[str, str] = {}
    for pin, nums in op["pins"].items():
        for n in nums:
            out.setdefault(str(n), pin)
    return out


def _same_size(p: dict, ang: float, o: dict) -> bool:
    """Whether pad `p`, turned to `ang`, covers the land `o` does: the same size (a part's terminal does not see
    a land's corner radius, so the shape is not compared)."""
    if abs(p["w"] - p["h"]) < FIT_TOL and abs(o["w"] - o["h"]) < FIT_TOL:
        return abs(p["w"] - o["w"]) < FIT_TOL
    d = (ang - o["angle"]) % 180.0
    if min(d, 180.0 - d) < 1.0:
        return abs(p["w"] - o["w"]) < FIT_TOL and abs(p["h"] - o["h"]) < FIT_TOL
    if abs(d - 90.0) < 1.0:
        return abs(p["w"] - o["h"]) < FIT_TOL and abs(p["h"] - o["w"]) < FIT_TOL
    return False


def _centre(pads) -> tuple[float, float]:
    xs = [p["x"] for p in pads]
    ys = [p["y"] for p in pads]
    return ((min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2)


def fit(pads: list[dict], deg: float) -> list[tuple[int, int]] | None:
    """The pad permutation when the part (library frame) turns `deg` about the centre of its pads, or None
    when some pad lands on no pad of the same kind, shape and size."""
    cx, cy = _centre(pads)
    perm = []
    for i, p in enumerate(pads):
        qx, qy = _rot_pt(p["x"] - cx, p["y"] - cy, deg)
        qx, qy = qx + cx, qy + cy
        hit = None
        for j, o in enumerate(pads):
            if abs(o["x"] - qx) > FIT_TOL or abs(o["y"] - qy) > FIT_TOL or o["kind"] != p["kind"]:
                continue
            if _same_size(p, p["angle"] + deg, o):
                hit = j
                break
        if hit is None:
            return None
        perm.append((i, hit))
    return perm


def _body_symmetric(body, deg: float, c) -> bool:
    if body is None:
        return True
    pts = [_rot_pt(x - c[0], y - c[1], deg) for x, y in ((body[0], body[1]), (body[2], body[1]), (body[2], body[3]), (body[0], body[3]))]
    xs = [p[0] + c[0] for p in pts]
    ys = [p[1] + c[1] for p in pts]
    return all(abs(a - b) < BODY_TOL for a, b in zip((min(xs), min(ys), max(xs), max(ys)), body))


def need(op: dict) -> dict:
    """G1 for one part (`op`: see `part_of`): `{"need": "required" | "optional" | "none", "pad": the pad the
    mark names, "pin": its pin, "shape": "dot" | "bar" | "plus", "wrong": [(deg, [(pad, pin, onto pad, its
    pin), ...]), ...] the turns that fit and change the part}`."""
    pads = op["pads"]
    numbered = [p for p in pads if p["num"]]
    out: dict = {"need": "none", "pad": None, "pin": None, "shape": None, "wrong": []}
    if len({p["num"] for p in numbered}) < 2:
        return out
    names = _pin_names(op)
    anon = bool(op["pins"]) and all(len(nums) == 1 and pin == str(nums[0]) for pin, nums in op["pins"].items()) and all(p["num"] in names for p in numbered)
    polar = op.get("kind") == "led" or str(op.get("prefix", "")).upper() in POLAR_PREFIX
    c = _centre(pads)
    selfsame_part = anon and not polar

    def pin_of(p):
        return names.get(p["num"], f"pad {p['num']}")

    for deg in ROTS:
        perm = fit(pads, deg)
        if perm is None:
            continue
        moved = [(pads[i]["num"], pin_of(pads[i]), pads[j]["num"], pin_of(pads[j])) for i, j in perm if pads[i]["num"] and pin_of(pads[i]) != pin_of(pads[j])]
        if not moved:
            continue
        if selfsame_part and _body_symmetric(op.get("body"), deg, c):
            continue
        out["wrong"].append((deg, moved))
    polarised = not (selfsame_part and _body_symmetric(op.get("body"), 180, c))
    ic = str(op.get("prefix", "")).upper() in IC_PREFIX
    out["need"] = "required" if out["wrong"] else ("optional" if polarised and ic else "none")
    if out["need"] == "none":
        return out
    two = len({p["num"] for p in numbered}) == 2
    cath = [pin for pin in op["pins"] if pin.upper() in CATHODE]
    plus = [pin for pin in op["pins"] if pin in PLUS]
    nums = {p["num"] for p in numbered}
    if two and cath:
        out["shape"], out["pin"] = "bar", cath[0]
        out["pad"] = str(op["pins"][cath[0]][0])
    elif two and plus:
        out["shape"], out["pin"] = "plus", plus[0]
        out["pad"] = str(op["pins"][plus[0]][0])
    else:
        out["shape"] = "dot"
        if "1" in nums:
            out["pad"] = "1"
        elif "A1" in nums:
            out["pad"] = "A1"
        else:
            digits = sorted((n for n in nums if n.isdigit()), key=int)
            out["pad"] = digits[0] if digits else sorted(nums)[0]
        out["pin"] = names.get(out["pad"])
    return out


def role(n: dict) -> str:
    if n["shape"] == "bar":
        return "cathode"
    if n["shape"] == "plus":
        return "+ terminal"
    return "pin 1"


# ------------------------------------------------------------------ the part as the rules read it


def part_of(ref: str, at, pads, *, body, pins: dict, kind: str = "", prefix: str = "", board_angle: bool = False) -> dict:
    """One part for these rules: its `(at x y rot)`, its `pads.PadSpec`s (library frame; `board_angle` when their
    angle is a board file's, library plus footprint), its body box in its own frame (`body_box`), its symbol pins
    (`{name: pads}`), its kind and reference prefix."""
    from .silk import pad_box

    rot = at[2]
    out = []
    for sp in pads:
        box = pad_box(sp, at, board_angle=board_angle)
        if box is None:
            continue
        w, h = sp.w, sp.h
        if sp.custom is not None:
            w, h = max(w, sp.custom[0]), max(h, sp.custom[1])
        ang = (sp.angle - rot) if board_angle else sp.angle
        out.append({"num": sp.num, "kind": sp.kind, "shape": sp.shape, "x": sp.x, "y": sp.y, "w": w, "h": h, "angle": ang % 360.0, "box": box, "mask": sp.mask_margin})
    return {"ref": ref, "at": tuple(at), "pads": out, "body": body, "pins": {k: tuple(str(x) for x in v) for k, v in pins.items()}, "kind": kind, "prefix": prefix}


def body_box(block: str):
    """A footprint's body in its own frame: its `F.Fab` drawings' box, else its `F.CrtYd` box, else None."""
    from .geom import _graphics_bbox
    from .silk import _bbox_on_layer

    return _graphics_bbox(block, ".Fab") or _bbox_on_layer(block, "F.CrtYd")


def body_poly(op: dict):
    if op.get("body") is None:
        return None
    b = op["body"]
    fx, fy, rot = op["at"]
    return tuple((fx + p[0], fy + p[1]) for p in (_rot_pt(x, y, rot) for x, y in ((b[0], b[1]), (b[2], b[1]), (b[2], b[3]), (b[0], b[3]))))


def under_body(op: dict, pad: dict) -> bool:
    """Whether `pad`'s opening lies wholly inside its part's body box (a through-hole housing, an LGA, a module):
    G5's reach is then counted from the body's edge, the nearest a visible mark can be."""
    b = op.get("body")
    if b is None:
        return False
    m = pad["mask"]
    ang = pad["angle"] % 180.0
    w, h = (pad["h"], pad["w"]) if abs(ang - 90.0) < 1.0 else (pad["w"], pad["h"])
    return pad["x"] - w / 2 - m >= b[0] - 1e-9 and pad["y"] - h / 2 - m >= b[1] - 1e-9 and pad["x"] + w / 2 + m <= b[2] + 1e-9 and pad["y"] + h / 2 + m <= b[3] + 1e-9


def reach_words(op: dict, n: dict) -> str:
    pad = next(p for p in op["pads"] if p["num"] == n["pad"])
    if under_body(op, pad):
        return f"within {GAP_MM:g} mm of {op['ref']}'s body (pad {n['pad']} is under it)"
    return f"within {GAP_MM:g} mm of pad {n['pad']}'s opening"


# ------------------------------------------------------------------ judging a mark


def ink_size(ink) -> tuple[float, float]:
    """(thickness, size): the narrowest ink across (a stroke's width, a filled shape's smaller side, a disc's
    diameter) and the widest."""
    if "disc" in ink:
        d = 2 * ink["disc"][2]
        return (d if ink.get("filled") else 2 * ink["hw"]), d
    b = ink_box(ink)
    w, h = b[2] - b[0], b[3] - b[1]
    return (min(w, h) if ink["filled"] else 2 * ink["hw"]), max(w, h)


class Scene:
    """Everything a mark is measured against: every pad's opening (`(ref, num, box)`), every part's body
    (`(ref, polygon)`), the silk already printed (`(key, who, owner, ink)`: owner the footprint it belongs to,
    None for a board drawing), the board's box, the fab's limits."""

    def __init__(self, pads, bodies, silk, board, limits, pins: set | None = None, under: set | None = None):
        self.pads = [(r, n, b, box_ink(b)) for r, n, b in pads]
        self.pins = pins  # the refs whose pads count as pins (G5), None: every ref on the board
        self.bodies = [(r, poly, poly_ink(poly), ink_box(poly_ink(poly))) for r, poly in bodies if poly]
        self.body_of = {r: pi for r, _poly, pi, _pb in self.bodies}
        self.under = under or set()  # (ref, pad number) of the pads that sit wholly under their part's body
        self.marked: set | None = None  # the parts that take a mark (`plan` sets it): what a directional mark is read against
        self.silk = [(k, who, owner, ink, ink_box(ink)) for k, who, owner, ink in silk]
        self.board = board
        self.limits = limits

    def local(self, box, ref: str | None = None, far: float = 3.0) -> "Scene":
        """This scene cut to what can touch, or be read against, a mark inside `box`: the silk and bodies there,
        the pads within `far` of it, every pad of a part whose body is, and every pad of part `ref`."""
        out = Scene.__new__(Scene)
        out.bodies = [e for e in self.bodies if _near(e[3], box, far)]
        near_body = {e[0] for e in out.bodies}
        out.pads = [e for e in self.pads if e[0] == ref or e[0] in near_body or _near(e[2], box, far)]
        out.body_of, out.under, out.marked = self.body_of, self.under, self.marked
        out.silk = [e for e in self.silk if _near(e[4], box, 0.0)]
        out.board, out.limits, out.pins = self.board, self.limits, self.pins
        return out

    def pad_dist(self, ink, r: str, num: str, bi) -> float:
        """How far `ink` is from pad `r.num` as a reader of the assembled board sees it: from its opening, or —
        for a pad under its part's body, the mark outside that body — from the body's edge, the nearest a
        visible mark can be (G5)."""
        if (r, num) in self.under and r in self.body_of:
            db = dist(ink, self.body_of[r])
            if db > 0:
                return db
        return dist(ink, bi)

    def parts_near(self, ink, reach: float = 30.0, among: set | None = None):
        """[(distance, ref, pad)] of every part with pins (`pins`; `among`: only these) — the distance its nearest
        pad's (`pad_dist`) — nearest first: which part a mark sits by."""
        bb = ink_box(ink)
        best: dict[str, tuple[float, str]] = {}
        for r, n, b, bi in self.pads:
            if (self.pins is not None and r not in self.pins) or (among is not None and r not in among):
                continue
            if not _near(bb, b, reach):
                continue
            d = self.pad_dist(ink, r, n, bi)
            if r not in best or d < best[r][0] - 1e-12:
                best[r] = (d, n)
        out = [(d, r, n) for r, (d, n) in best.items()]
        out.sort(key=lambda t: (round(t[0], 6), t[1], t[2]))
        return out

    def owner_of(self, ink, among: set | None = None) -> tuple[str | None, list]:
        """(the part a mark names — the nearest part (`among` them) by `MARGIN_MM` or more, else None —, `parts_near`)."""
        near = self.parts_near(ink, among=among)
        if not near:
            return None, near
        if len(near) == 1 or near[1][0] - near[0][0] >= MARGIN_MM - 1e-9:
            return near[0][1], near
        return None, near

    def add_silk(self, key, who: str, ink: dict) -> None:
        self.silk.append((key, who, None, ink, ink_box(ink)))

    def drop_silk(self, key) -> None:
        self.silk = [e for e in self.silk if e[0] != key]

    def nearest(self, ink, only: str | None = None, reach: float = 6.0):
        """[(distance, ref, num)] of the part pins within `reach` of `ink`, nearest first (`only`: one part's)."""
        bb = ink_box(ink)
        out = []
        for r, n, b, bi in self.pads:
            if (only is not None and r != only) or (self.pins is not None and r not in self.pins):
                continue
            if not _near(bb, b, reach):
                continue
            out.append((dist(ink, bi), r, n))
        out.sort(key=lambda t: (round(t[0], 6), t[1], t[2]))
        return out


def points_at(ink, op: dict, scene: Scene) -> tuple[str | None, float, float]:
    """(the pad of its own part a mark of `op` names — the nearest by `MARGIN_MM` or more, else None —, its
    distance, the next pad's)."""
    near = scene.nearest(ink, only=op["ref"], reach=50.0)
    if not near:
        return None, 0.0, 0.0
    if len(near) == 1:
        return near[0][2], near[0][0], math.inf
    if near[1][0] - near[0][0] >= MARGIN_MM - 1e-9:
        return near[0][2], near[0][0], near[1][0]
    return None, near[0][0], near[1][0]


def judge(ink, op: dict, n: dict, scene: Scene, *, key=None, owner: str | None = None) -> dict:
    """G2-G5 for one mark of part `op` naming pad `n["pad"]`: `{"ok", "why": [the rules it breaks, as facts],
    "gap", "margin", "next"}`. `key`: the mark's own silk entry; `owner`: the footprint it is drawn in (its
    own footprint's other drawings are that footprint's, as KiCad's silk clearance check counts them)."""
    lim = scene.limits
    why = []
    thick, size = ink_size(ink)
    if thick < lim["line_mm"] - 1e-6 or size < DOT_MIN - 1e-6:
        why.append(f"{thick:.2f} mm thick and {size:.2f} mm across (G2: {lim['line_mm']:g} mm strokes, {DOT_MIN:g} mm across)")
    bb = ink_box(ink)
    clr = lim["clearance_mm"]
    for r, num, b, bi in scene.pads:
        if _near(bb, b, clr) and dist(ink, bi) < clr - 1e-6:
            why.append(f"{max(0.0, dist(ink, bi)):.2f} mm from {r}.{num}'s opening (G3: {clr:g} mm)")
            break
    for k, who, own, s, sb in scene.silk:
        if k == key or (owner is not None and own == owner) or not _near(bb, sb, clr):
            continue
        if dist(ink, s) < clr - 1e-6:
            why.append(f"{max(0.0, dist(ink, s)):.2f} mm from {who} (G3: {clr:g} mm)")
            break
    for r, poly, pi, pb in scene.bodies:
        if _near(bb, pb, 0.0) and dist(ink, pi) < -1e-9:
            why.append(f"on {r}'s body (G4)")
            break
    x0, y0, x1, y1 = scene.board
    if bb[0] < x0 + clr - 1e-6 or bb[1] < y0 + clr - 1e-6 or bb[2] > x1 - clr + 1e-6 or bb[3] > y1 - clr + 1e-6:
        why.append(f"within {clr:g} mm of the board edge (G3)")
    pad = next(p for p in op["pads"] if p["num"] == n["pad"])
    # G5, within the part: the marked pad the nearest of the part's own pads (raw openings), or the mark in line with it.
    own = scene.nearest(ink, only=op["ref"], reach=60.0)
    mine = min((d for d, _r, num in own if num == n["pad"]), default=math.inf)
    nxt_own = next(((d, r, num) for d, r, num in own if num != n["pad"]), (math.inf, "", ""))
    gap = mine
    if under_body(op, pad):
        body = poly_ink(body_poly(op))
        if dist(ink, body) > GAP_MM + 1e-6:
            why.append(f"{dist(ink, body):.2f} mm from {op['ref']}'s body, under which pad {n['pad']} sits (G5: {GAP_MM:g} mm)")
    elif gap > GAP_MM + 1e-6:
        why.append(f"{gap:.2f} mm from {op['ref']}.{n['pad']}'s opening (G5: {GAP_MM:g} mm)")
    if nxt_own[0] - gap < MARGIN_MM - 1e-6 and not _in_line(ink, op, n, own, scene, gap):
        why.append(f"{nxt_own[1]}.{nxt_own[2]} is {nxt_own[0]:.2f} mm away and {op['ref']}.{n['pad']} {gap:.2f} mm, and the mark is not in line with {op['ref']}.{n['pad']} alone (G5: {MARGIN_MM:g} mm nearer its own pad)")
    # G5, across parts: the part the nearest of every part with pins — a resistor's, a fiducial's included — by MARGIN_MM,
    # each part's distance its nearest pad's opening, or its body's edge for a pad under the body (`Scene.pad_dist`).
    # A dot (or a +) says nothing but "the pin nearest me", so every pad on the board is read against it; a directional
    # mark — a triangle's apex, a bar across a cathode — names its pin by its shape, and is read only against the
    # parts that take a mark themselves (a decap hard by an ESP32's pin-1 chevron is not its pin 1).
    dotlike = _dotlike(ink)
    among = None if dotlike or scene.marked is None else scene.marked | {op["ref"]}
    near = scene.parts_near(ink, among=among)
    mine_part = next((d for d, r, _n in near if r == op["ref"]), math.inf)
    nxt_part = next(((d, r, num) for d, r, num in near if r != op["ref"]), (math.inf, "", ""))
    if nxt_part[0] - mine_part < MARGIN_MM - 1e-6:
        why.append(f"{nxt_part[1]}{'.' + nxt_part[2] if nxt_part[2] else ''} is {nxt_part[0]:.2f} mm away and {op['ref']} {mine_part:.2f} mm (G5: {MARGIN_MM:g} mm nearer its own part than any other part's pad)")
    nxt = min(((nxt_own[0] - gap, nxt_own), (nxt_part[0] - mine_part, nxt_part)), key=lambda e: e[0])
    return {"ok": not why, "why": why, "gap": gap, "margin": nxt[0], "next": f"{nxt[1][1]}.{nxt[1][2]}"}


def _centre_of(ink) -> tuple[float, float]:
    if "disc" in ink:
        return ink["disc"][0], ink["disc"][1]
    b = ink_box(ink)
    return (b[0] + b[2]) / 2, (b[1] + b[3]) / 2


def _in_line(ink, op: dict, n: dict, own, scene: Scene, gap: float) -> bool:
    """G5's second reading, within the part: the mark sits in line with its pad — its centre within the pad's span
    across one axis, as a chevron or a dot past the end of a row does — and nearer its pad by `MARGIN_MM` than every
    other of the part's pads in line with it, and none of the part's pads is nearer (`own`: `nearest(only=)`)."""
    cx, cy = _centre_of(ink)
    boxes = {(r, num): b for r, num, b, _bi in scene.pads if r == op["ref"]}

    def in_line(num) -> bool:
        b = boxes.get((op["ref"], num))
        return b is not None and (b[0] - 1e-6 <= cx <= b[2] + 1e-6 or b[1] - 1e-6 <= cy <= b[3] + 1e-6)

    if not in_line(n["pad"]):
        return False
    for d, _r, num in own:
        if num == n["pad"]:
            continue
        if in_line(num):
            if d - gap < MARGIN_MM - 1e-6:
                return False
        elif d < gap - 1e-6:
            return False
    return True


# ------------------------------------------------------------------ generating a mark


def _ring(box, d: float):
    """Points every `STEP` mm along the outline `d` mm out from `box` (rounded corners)."""
    x0, y0, x1, y1 = box
    pts = []
    for (ax, ay), (bx, by), (nx, ny) in (
        ((x0, y0 - d), (x1, y0 - d), (0, -1)),
        ((x1 + d, y0), (x1 + d, y1), (1, 0)),
        ((x1, y1 + d), (x0, y1 + d), (0, 1)),
        ((x0 - d, y1), (x0 - d, y0), (-1, 0)),
    ):
        L = math.hypot(bx - ax, by - ay)
        k = max(1, int(round(L / STEP)))
        pts += [(ax + (bx - ax) * i / k, ay + (by - ay) * i / k) for i in range(k + 1)]
    for cx, cy, a0 in ((x1, y0, -90), (x1, y1, 0), (x0, y1, 90), (x0, y0, 180)):
        k = max(1, int(round(d * math.pi / 2 / STEP)))
        pts += [(cx + d * math.cos(math.radians(a0 + 90 * i / k)), cy + d * math.sin(math.radians(a0 + 90 * i / k))) for i in range(1, k)]
    return pts


def _r4(v: float) -> float:
    return round(v + 0.0, 4)


def dot_prim(cx: float, cy: float, D: float, line: float) -> dict:
    cx, cy = _r4(cx), _r4(cy)
    rc = _r4(D / 2 - line / 2)
    return {"kind": "gr_circle", "center": (cx, cy), "end": (_r4(cx + rc), cy), "width": line, "fill": True}


def poly_prim(pts, line: float) -> dict:
    return {"kind": "gr_poly", "pts": tuple((_r4(x), _r4(y)) for x, y in pts), "width": line, "fill": True}


def prim_ink(prim: dict) -> dict:
    hw = prim["width"] / 2
    if prim["kind"] == "gr_circle":
        (cx, cy), (ex, ey) = prim["center"], prim["end"]
        return {"disc": (cx, cy, math.hypot(ex - cx, ey - cy) + hw), "hw": hw, "filled": True}
    out = poly_ink(prim["pts"], hw)
    if prim.get("plus"):
        out["plus"] = True
    return out


def prim_shape(prim: dict) -> dict:
    """A generated mark as a world silk shape (`silk.drawings`' form), for the passes after this one."""
    hw = prim["width"] / 2
    if prim["kind"] == "gr_circle":
        (cx, cy), (ex, ey) = prim["center"], prim["end"]
        r = math.hypot(ex - cx, ey - cy)
        pts = tuple((cx + r * math.cos(2 * math.pi * k / 32), cy + r * math.sin(2 * math.pi * k / 32)) for k in range(32))
        tag = "fp_circle"
    else:
        pts = tuple(prim["pts"])
        tag = "fp_poly"
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return {"tag": tag, "index": -1, "width": prim["width"], "fill": True, "pts": pts, "closed": True, "bbox": (min(xs) - hw, min(ys) - hw, max(xs) + hw, max(ys) + hw), "uuid": ""}


def _axes(op: dict, pad: dict):
    """(u, v): the unit vector from the other terminal's centre to `pad`'s (world), and its normal."""
    fx, fy, rot = op["at"]
    other = next(p for p in op["pads"] if p["num"] and p["num"] != pad["num"])
    ux, uy = _rot_pt(pad["x"] - other["x"], pad["y"] - other["y"], rot)
    L = math.hypot(ux, uy) or 1.0
    return (ux / L, uy / L), (-uy / L, ux / L)


def _candidates(op: dict, n: dict, line: float):
    """Every mark to try for part `op`, in the order tried within a clearance tier: (primitive, a label)."""
    pad = next(p for p in op["pads"] if p["num"] == n["pad"])
    box = pad["box"]
    limit = GAP_MM
    gaps = [round(g * STEP, 4) for g in range(int(round(0.15 / STEP)), int(math.floor((limit + 1e-9) / STEP)) + 1)]
    pcx, pcy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
    if under_body(op, pad):
        # Around the body, near the pad: the visible spots closest to it (G4, G5).
        poly = body_poly(op)
        xs = [q[0] for q in poly]
        ys = [q[1] for q in poly]
        ring_box = (min(xs), min(ys), max(xs), max(ys))
        far = max(0.0, min(pcx - ring_box[0], pcy - ring_box[1], ring_box[2] - pcx, ring_box[3] - pcy)) + GAP_MM + 2.5
        gaps = [round(g * STEP, 4) for g in range(0, int(math.floor((GAP_MM + 1e-9) / STEP)) + 1)]

        def ring(d):
            return [q for q in _ring(ring_box, d) if math.hypot(q[0] - pcx, q[1] - pcy) <= far + d]
    else:

        def ring(d):
            return _ring(box, d)

    if n["shape"] == "bar":
        (ux, uy), (vx, vy) = _axes(op, pad)
        cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
        corners = ((box[0], box[1]), (box[2], box[1]), (box[2], box[3]), (box[0], box[3]))
        end = max((x - cx) * ux + (y - cy) * uy for x, y in corners)
        across = 2 * max(abs((x - cx) * vx + (y - cy) * vy) for x, y in corners)
        length = across + BAR_PAST
        for w in BAR_W:
            for g in gaps:
                if g + w > limit + 1e-9:
                    break
                for s in (0.0, 0.05, -0.05, 0.1, -0.1, 0.15, -0.15, 0.2, -0.2):
                    mx = cx + ux * (end + g + w / 2) + vx * s
                    my = cy + uy * (end + g + w / 2) + vy * s
                    hu, hv = w / 2 - line / 2, length / 2 - line / 2
                    pts = [(mx + ux * a * hu + vx * b * hv, my + uy * a * hu + vy * b * hv) for a, b in ((-1, -1), (1, -1), (1, 1), (-1, 1))]
                    yield (w, 0), poly_prim(pts, line)
        return
    if n["shape"] == "plus":
        t = 0.05 / 2  # the polygon's half-thickness; the stroke adds the rest
        a = PLUS_MM / 2 - line / 2
        for g in gaps:
            for mx, my in ring(g + PLUS_MM / 2):
                pts = [(mx - t, my - a), (mx + t, my - a), (mx + t, my - t), (mx + a, my - t), (mx + a, my + t), (mx + t, my + t), (mx + t, my + a), (mx - t, my + a), (mx - t, my + t), (mx - a, my + t), (mx - a, my - t), (mx - t, my - t)]
                yield (PLUS_MM, g), {**poly_prim(pts, line), "plus": True}
        return
    narrow = min(box[2] - box[0], box[3] - box[1])
    first = min(DOT_MAX, max(DOT_MIN, narrow))
    sizes = []
    D = round(first, 2)
    while D >= DOT_MIN - 1e-9:
        sizes.append(D)
        D = round(D - 0.1, 2)
    if sizes[-1] > DOT_MIN + 1e-9:
        sizes.append(DOT_MIN)
    for D in sizes:
        for g in gaps:
            for mx, my in ring(g + D / 2):
                yield (D, g), dot_prim(mx, my, D, line)


def _blocker(ink, op, n, scene: Scene, tier: float) -> tuple[str, str] | None:
    """The first rule a candidate breaks, as (kind, what), or None when it is legal at clearance `tier`."""
    bb = ink_box(ink)
    x0, y0, x1, y1 = scene.board
    clr = scene.limits["clearance_mm"]
    if bb[0] < x0 + clr - 1e-6 or bb[1] < y0 + clr - 1e-6 or bb[2] > x1 - clr + 1e-6 or bb[3] > y1 - clr + 1e-6:
        return "edge", "the board edge"
    for r, num, b, bi in scene.pads:
        if _near(bb, b, tier) and dist(ink, bi) < tier - 1e-6:
            return "pad", f"{r}.{num}"
    for r, poly, pi, pb in scene.bodies:
        if _near(bb, pb, 0.0) and dist(ink, pi) < -1e-9:
            return "body", r
    for _k, who, _own, s, sb in scene.silk:
        if _near(bb, sb, tier) and dist(ink, s) < tier - 1e-6:
            return "silk", who
    j = judge(ink, op, n, scene)
    if not j["ok"]:
        return "pad near", j["next"]
    return None


def generate(op: dict, n: dict, scene: Scene) -> tuple[dict | None, dict]:
    """The best legal mark for `op` (a primitive) and a record `{"tried", "blocked": {kind: {what: count}},
    "tier", "judge"}`. Clearance `AIM_MM` first, then the fab's; within a tier the biggest mark, then the one
    clearest of every other pad (G5's margin, counted to 0.6 mm in `STEP`s), then the nearest its pad, then the
    first on the search grid."""
    line = scene.limits["line_mm"]
    clr = scene.limits["clearance_mm"]
    pad = next(p for p in op["pads"] if p["num"] == n["pad"])
    reach = GAP_MM + DOT_MAX + MARGIN_MM + 1.5
    if under_body(op, pad):
        bx = ink_box(poly_ink(body_poly(op)))
        reach += max(pad["box"][0] - bx[0], pad["box"][1] - bx[1], bx[2] - pad["box"][2], bx[3] - pad["box"][3])
    view = scene.local((pad["box"][0] - reach, pad["box"][1] - reach, pad["box"][2] + reach, pad["box"][3] + reach), op["ref"])
    cands = list(_candidates(op, n, line))
    blocked: dict[str, dict[str, int]] = {}
    tiers = [AIM_MM, clr] if AIM_MM > clr else [clr]
    for ti, tier in enumerate(tiers):
        best = None
        size_now = None
        for (size, _g), prim in cands:
            if best is not None and size != size_now:
                break
            ink = prim_ink(prim)
            why = _blocker(ink, op, n, view, tier)
            if why is not None:
                if ti == len(tiers) - 1:
                    blocked.setdefault(why[0], {}).setdefault(why[1], 0)
                    blocked[why[0]][why[1]] += 1
                continue
            j = judge(ink, op, n, view)
            key = (-min(round(min(j["margin"], 1.0) / STEP) * STEP, 0.6), round(j["gap"], 3))
            if best is None or key < best[0]:
                best = (key, prim, j)
                size_now = size
        if best is not None:
            return best[1], {"tried": len(cands), "blocked": blocked, "tier": tier, "judge": best[2]}
    return None, {"tried": len(cands), "blocked": blocked}


def words(prim_or_shape: dict) -> str:
    """A mark as printed: its kind and its ink's size (line width included)."""
    if prim_or_shape.get("kind") == "gr_circle":
        (cx, cy), (ex, ey) = prim_or_shape["center"], prim_or_shape["end"]
        return f"{2 * math.hypot(ex - cx, ey - cy) + prim_or_shape['width']:.2f} mm dot"
    if prim_or_shape.get("kind") == "gr_poly":
        b = ink_box(prim_ink(prim_or_shape))
        return f"{b[2] - b[0]:.2f} x {b[3] - b[1]:.2f} mm filled polygon"
    s = prim_or_shape
    kind = {"fp_circle": "circle", "fp_arc": "arc", "fp_poly": "polygon", "fp_rect": "rectangle", "fp_line": "line"}[s["tag"]]
    if s["tag"] == "fp_circle":
        return f"{'filled ' if s['fill'] else ''}{kind} {2 * ink_of(s)['disc'][2]:.2f} mm across (line {s['width']:g} mm)"
    b = ink_box(ink_of(s))
    return f"{'filled ' if s['fill'] else ''}{kind} {b[2] - b[0]:.2f} x {b[3] - b[1]:.2f} mm (line {s['width']:g} mm)"


def blocked_words(rec: dict) -> str:
    kinds = {"pad": "cross a pad opening", "silk": "cross other silk", "body": "sit on a part body", "edge": "leave the board edge", "pad near": "are as near another pad"}
    parts = []
    for k in ("pad", "silk", "body", "edge", "pad near"):
        got = rec["blocked"].get(k)
        if not got:
            continue
        names = ", ".join(sorted(got, key=lambda w: (-got[w], w))[:4])
        parts.append(f"{sum(got.values())} {kinds[k]} ({names})")
    return "; ".join(parts) or "none was tried"


def fail_line(op: dict, n: dict, rec: dict, limits: dict) -> str:
    """The fact for a required part the plan found no mark for (docs/direction.md §7b: no proposal)."""
    what = {"dot": "pin-1 dot", "bar": "cathode bar", "plus": "+ sign"}[n["shape"]]
    turns = "; ".join(f"turned {deg} degrees " + ", ".join(f"pad {a} ({pa}) lands on pad {b} ({pb})" for a, pa, b, pb in moved[:3]) for deg, moved in n["wrong"])
    return (
        f"{op['ref']}: no orientation mark: its pads fit {turns}, so the fab needs a {what} at pad {n['pad']}"
        f"{' (' + n['pin'] + ')' if n['pin'] else ''}; no spot is outside every part body, {limits['clearance_mm']:g} mm from every pad opening"
        f" and other silk, inside the board edge, {reach_words(op, n)} and {MARGIN_MM:g} mm nearer pad {n['pad']}"
        f" than any other pad: of {rec['tried']} spots, {blocked_words(rec)}"
    )


# ------------------------------------------------------------------ the plan (place stage) and the check (emitted board)


def _is_dot(s: dict) -> bool:
    return s["tag"] == "fp_circle"


def _part_words(e) -> str:
    d, r, num = e
    return f"{r}{'.' + num if num else ''} ({d:.2f} mm)"


def other_part_fact(ref: str, near) -> str | None:
    """What a footprint's dot names when it is not part `ref`'s (`Scene.owner_of`'s `near`): another part's pin, or
    no part — the nearest part (each its nearest pad's opening, or its body's edge for a pad under the body) is
    not `ref` by `MARGIN_MM` — else None."""
    mine = next((e for e in near if e[1] == ref), None)
    other = next((e for e in near if e[1] != ref), None)
    if other is None or (mine is not None and other[0] - mine[0] >= MARGIN_MM - 1e-9):
        return None
    own = f"{ref}'s nearest pad, {_part_words(mine)}" if mine else f"any pad of {ref}"
    if mine is not None and mine[0] <= other[0]:
        return f"names no part: {_part_words(other)} is within {MARGIN_MM:g} mm as near as {own} (G5)"
    return f"is nearer {_part_words(other)} than {own}: it names another part's pin (G5)"


def no_pin_fact(ref: str, s: dict, scene: "Scene") -> str | None:
    """What a dot of part `ref` (one that takes a mark) names when it names none of its pins — its two nearest pads
    within `MARGIN_MM` of each other — else None."""
    if not _is_dot(s):
        return None
    near = scene.nearest(ink_of(s), only=ref, reach=50.0)
    if len(near) < 2 or near[1][0] - near[0][0] >= MARGIN_MM - 1e-9:
        return None
    return f"names no pin: pads {near[0][2]} and {near[1][2]} are {near[0][0]:.2f} and {near[1][0]:.2f} mm from it (G5: {MARGIN_MM:g} mm nearer one)"


def plan(parts: list[dict], printed: list[tuple[str, str | None, dict]], pads, board, limits, *, make: bool = True) -> dict:
    """The orientation marks of `parts` (`part_of`, each with its `"layer"`).

    `printed`: every silk shape that will print, as (who: "<ref>'s library silk" or a `layout.core.py` line,
    the footprint it is drawn in or None for a board drawing, the world shape). `pads`: every pad opening on
    the board, `(ref, num, box)` — a fiducial's included. `board`: the box the silk stays inside. `make`:
    generate the missing marks (the place stage); False judges what is printed (the check of an emitted board).

    Which part a mark belongs to is measured, over every pad on the board: a footprint's drawing is its
    footprint's, but a dot that sits nearer another part (or as near two) names that part's pin and is not
    printed; a board drawing (a `layout.core.py` or `layout.gen.py` line) belongs to the part it sits by, by
    `MARGIN_MM`, each part's distance its nearest pad's opening or, for a pad under its body, the body's edge
    (`Scene.owner_of`), and no farther than `OWN_MM` so measured (farther, it is decoration).

    Returns `{"marks": [{"ref", "pad", "shape", "prim"}], "drop": [(index into printed, fact)], "rows": [{"ref",
    "need", "pad", "verdict": "kept" | "generated" | "failed" | "none", ...}], "errors": [facts], "notes":
    [facts]}`. A library mark that names another pin, another part or (a dot) no pin is in `drop` (the caller
    leaves it out); a board drawing of a part that names another of its pins, names none (a dot), or names its
    pin and breaks G2-G5 is an error naming the line and the rule it breaks — nothing is generated beside it."""
    bodies = [(p["ref"], body_poly(p)) for p in parts]
    needs = {p["ref"]: need(p) for p in parts}
    under = {(p["ref"], q["num"]) for p in parts for q in p["pads"] if under_body(p, q)}
    # G5 reads a mark against every pad on the board: its own part's, a resistor's, a fiducial's.
    scene = Scene(pads, bodies, [(i, who, owner, ink_of(s)) for i, (who, owner, s) in enumerate(printed)], board, limits, pins=None, under=under)
    scene.marked = {r for r, n in needs.items() if n["need"] != "none"}
    by_ref = {p["ref"]: p for p in parts}
    out: dict = {"marks": [], "drop": [], "rows": [], "errors": [], "notes": []}

    def refuse(i: int, fact: str, line_fact: str) -> None:
        """A library mark is left out with its fact (the place stage); on an emitted board, or a board drawing, the
        fact is an error."""
        _who, owner, _s = printed[i]
        if owner is not None and make:
            out["drop"].append((i, fact))
            scene.drop_silk(i)
        else:
            out["errors"].append(line_fact)

    owned: dict[str, list[int]] = {}
    for i, (who, owner, s) in enumerate(printed):
        if not is_mark(s):
            continue
        ink = ink_of(s)
        if owner is None:
            o, near = scene.owner_of(ink, among=None if _dotlike(ink) else scene.marked)
            if o in by_ref and near[0][0] <= OWN_MM + 1e-9:
                owned.setdefault(o, []).append(i)
            continue
        if _is_dot(s):
            _o, near = scene.owner_of(ink)
            tail = other_part_fact(owner, near)
            if tail is not None:
                refuse(i, f"{owner}: silkscreen mark not printed: the library's {words(s)} mark {tail}", f"{who}: a {words(s)} mark {tail}")
                continue
        if owner in by_ref:
            owned.setdefault(owner, []).append(i)
    todo = []
    for ref in sorted(by_ref):
        op, n = by_ref[ref], needs[ref]
        if n["need"] == "none":
            continue
        if op.get("layer", "F.Cu") != "F.Cu":
            out["notes"].append(f"{ref}: orientation mark not judged: the part is on the back, and pcbc prints no back silkscreen")
            continue
        good, bad = [], []
        for i in owned.get(ref, ()):
            who, owner, s = printed[i]
            ink = ink_of(s)
            at, d0, d1 = points_at(ink, op, scene)
            if at is not None and at != n["pad"]:
                fact = f"{ref}: silkscreen mark not printed: the library's {words(s)} mark is nearest pad {at} ({d0:.2f} mm; every other pad of {ref} at least {d1 - d0:.2f} mm further), and {ref}'s {role(n)} is pad {n['pad']}"
                refuse(i, fact, f"{who}: a {words(s)} mark of {ref} is nearest its pad {at} ({d0:.2f} mm; every other pad of {ref} at least {d1 - d0:.2f} mm further), and {ref}'s {role(n)} is pad {n['pad']}: it names another pin")
                continue
            tail = no_pin_fact(ref, s, scene) if at is None else None
            if tail is not None:
                refuse(i, f"{ref}: silkscreen mark not printed: the library's {words(s)} mark {tail}", f"{who}: a {words(s)} mark of {ref} {tail}")
                continue
            j = judge(ink, op, n, scene, key=i, owner=owner)
            if _DEBUG:
                print("ORIENT", ref, who, words(s), at, j)
            if j["ok"]:
                good.append((who, s, j))
            elif owner is None:
                bad.append((who, s, j))
        for who, s, j in bad:
            out["errors"].append(f"{who}: a {words(s)} mark of {ref} at pad {n['pad']} ({role(n)}) breaks " + "; ".join(j["why"]))
        if bad:
            out["rows"].append({"ref": ref, "need": n["need"], "pad": n["pad"], "verdict": "failed"})
            continue
        if good:
            who, s, j = good[0]
            out["rows"].append({"ref": ref, "need": n["need"], "pad": n["pad"], "verdict": "kept" if make else "printed", "mark": f"{who}: {words(s)}", "gap": round(j["gap"], 3), "margin": round(j["margin"], 3)})
            continue
        todo.append(ref)
    for ref in sorted(todo, key=lambda r: (needs[r]["need"] != "required", r)):
        op, n = by_ref[ref], needs[ref]
        if not make:
            if n["need"] == "required":
                out["errors"].append(f"{ref}: no orientation mark printed: no {role(n)} mark at pad {n['pad']} passes G2-G5 (docs/direction.md §4c), and {needs_words(n)}")
                out["rows"].append({"ref": ref, "need": n["need"], "pad": n["pad"], "verdict": "failed"})
            else:
                out["rows"].append({"ref": ref, "need": n["need"], "pad": n["pad"], "verdict": "none"})
            continue
        prim, rec = generate(op, n, scene)
        if prim is None:
            if n["need"] == "required":
                out["errors"].append(fail_line(op, n, rec, limits))
                out["rows"].append({"ref": ref, "need": n["need"], "pad": n["pad"], "verdict": "failed"})
            else:
                out["notes"].append(f"{ref}: no pin-1 dot printed (its pads fit one way only, so none is required): no spot names pad {n['pad']}: of {rec['tried']} spots, {blocked_words(rec)}")
                out["rows"].append({"ref": ref, "need": n["need"], "pad": n["pad"], "verdict": "none"})
            continue
        scene.add_silk(("mark", ref), f"{ref}'s orientation mark", prim_ink(prim))
        j = rec["judge"]
        out["marks"].append({"ref": ref, "pad": n["pad"], "shape": n["shape"], "prim": prim})
        out["rows"].append({"ref": ref, "need": n["need"], "pad": n["pad"], "verdict": "generated", "mark": words(prim), "gap": round(j["gap"], 3), "margin": round(j["margin"], 3), "clearance": rec["tier"]})
    return out


def needs_words(n: dict) -> str:
    return "; ".join(f"its pads fit turned {deg} degrees with " + ", ".join(f"pad {a} ({pa}) on pad {b} ({pb})" for a, pa, b, pb in moved[:3]) for deg, moved in n["wrong"])


def row_line(r: dict) -> str:
    if r["verdict"] == "printed":
        return f"{r['ref']}: orientation marked ({r['need']}): {r['mark']} at pad {r['pad']}, {r['gap']:.2f} mm from its opening, every other pad at least {r['margin']:.2f} mm further"
    if r["verdict"] == "kept":
        return f"{r['ref']}: orientation mark kept ({r['need']}): {r['mark']} at pad {r['pad']}, {r['gap']:.2f} mm from its opening, every other pad at least {r['margin']:.2f} mm further"
    if r["verdict"] == "generated":
        return f"{r['ref']}: orientation mark generated ({r['need']}): a {r['mark']} at pad {r['pad']}, {r['gap']:.2f} mm from its opening, every other pad at least {r['margin']:.2f} mm further"
    if r["verdict"] == "failed":
        return f"{r['ref']}: no orientation mark (required) at pad {r['pad']}"
    return f"{r['ref']}: no orientation mark ({r['need']}; none is required) at pad {r['pad']}"


def board_drawings(text: str) -> list[tuple[str, dict]]:
    """Every board-level `F.SilkS` circle, polygon, line, rectangle and arc of a board text as (its uuid, a world
    silk shape in `silk.drawings`' form)."""
    from .gen import decompile
    from .layout_prims import flat_points
    from .silk import _arc_points

    out = []
    for g in decompile(text).graphics:
        f = g.f
        if f.get("layer") != "F.SilkS" or g.kind not in ("gr_circle", "gr_poly", "gr_line", "gr_rect", "gr_arc"):
            continue
        if g.kind == "gr_circle":
            (cx, cy), (ex, ey) = f["center"], f["end"]
            r = math.hypot(ex - cx, ey - cy)
            pts = tuple((cx + r * math.cos(2 * math.pi * k / 32), cy + r * math.sin(2 * math.pi * k / 32)) for k in range(32))
        elif g.kind == "gr_poly":
            pts = tuple(flat_points(f["pts"]))
        elif g.kind == "gr_rect":
            (x0, y0), (x1, y1) = f["start"], f["end"]
            pts = ((x0, y0), (x1, y0), (x1, y1), (x0, y1))
        elif g.kind == "gr_arc":
            pts = tuple(_arc_points(f["start"], f["mid"], f["end"]))
        else:
            pts = (f["start"], f["end"])
        tag = {"gr_circle": "fp_circle", "gr_poly": "fp_poly", "gr_rect": "fp_rect", "gr_arc": "fp_arc", "gr_line": "fp_line"}[g.kind]
        out.append((g.uuid or "?", graphic_shape(tag, pts, float(f.get("width") or 0.0), f.get("fill") is True)))
    return out


def graphic_shape(tag: str, pts, width: float, fill: bool) -> dict:
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    hw = width / 2
    return {"tag": tag, "index": -1, "width": width, "fill": fill, "pts": tuple(pts), "closed": tag not in ("fp_line", "fp_arc"), "bbox": (min(xs) - hw, min(ys) - hw, max(xs) + hw, max(ys) + hw), "uuid": ""}


def pins_of(inst) -> dict:
    return {name: tuple(str(x) for x in pin.pads) for name, pin in inst.part.pins.items()}


def board_scene(text: str, design) -> dict:
    """A board text as these rules read it: `{"parts": the design's parts (`part_of`, each with its `"layer"`),
    "printed": every F.SilkS drawing (`plan`'s form: each footprint's own, then the board's), "pads": every pad
    opening `(ref, num, box)` — a fiducial's too —, "texts": every printed F.SilkS text as (who, its ink box),
    "at": {ref: (x, y, rot)}}`."""
    import re

    from .pads import pad_specs
    from .sexp import board_footprint_spans, footprint_at, footprint_reference
    from .silk import INK_H, INK_W, _rot_pt, _text_box, _world, drawings

    insts = {i.ref: i for i in design.instances} if design is not None else {}
    parts, printed, pads, texts, ats = [], [], [], [], {}
    for start, end in board_footprint_spans(text):
        block = text[start:end]
        ref = footprint_reference(block) or "?"
        at = footprint_at(block) or (0.0, 0.0, 0.0)
        ats[ref] = at
        lm = re.search(r'\(layer "([^"]+)"\)', block)
        inst = insts.get(ref)
        op = part_of(ref, at, pad_specs(block), body=body_box(block), pins=pins_of(inst) if inst else {}, kind=inst.part.kind if inst else "", prefix=inst.part.prefix if inst else "", board_angle=True)
        op["layer"] = lm.group(1) if lm else "F.Cu"
        pads += [(ref, p["num"], p["box"]) for p in op["pads"]]
        if inst is not None:
            parts.append(op)
        for s in drawings(block, "F.SilkS"):
            printed.append((f"{ref}'s footprint silk", ref, _world(s, at)))
        for m in re.finditer(r'\((?:property "(Reference)"|fp_text \w+) "([^"]*)"', block):
            chunk = block[m.start() : _close(block, m.start()) + 1]
            if '(layer "F.SilkS")' not in chunk or re.search(r"\(hide yes\)", chunk):
                continue
            am = re.search(r"\(at ([-0-9.eE+]+) ([-0-9.eE+]+)(?: ([-0-9.eE+]+))?\)", chunk)
            sm = re.search(r"\(size ([0-9.eE+-]+) ([0-9.eE+-]+)\)", chunk)
            if not am or not sm:
                continue
            word = m.group(2).replace("${REFERENCE}", ref) if m.group(2) else ref
            lx, ly, trot = float(am.group(1)), float(am.group(2)), float(am.group(3) or 0.0)
            wx, wy = _rot_pt(lx, ly, at[2])
            size = float(sm.group(2))
            texts.append((f"{ref}'s {'reference' if m.group(1) else 'text'} \"{word}\"", _text_box(at[0] + wx, at[1] + wy, INK_W * size * max(len(word), 1), INK_H * size, trot)))
    for uid, s in board_drawings(text):
        printed.append((f"board drawing {uid}", None, s))
    from .sexp import edge_outline

    return {"parts": parts, "printed": printed, "pads": pads, "texts": texts, "at": ats, "board": edge_outline(text)}


def _close(text: str, j: int) -> int:
    from .sexp import matching_paren

    return matching_paren(text, j)


def silk_mask(scene: dict, limits: dict, mask_extra: float = 0.0) -> list[str]:
    """JLC's silk-to-pad clearance on **all** of a board's F.SilkS ink — every drawing, mark or not, and every printed
    text's box — against every pad's mask opening (its own margin plus the board's `mask_extra`): one fact per
    drawing nearer than the fab's clearance (G3). KiCad's DRC flags silk on a mask opening only where they overlap
    (`silk_over_copper`), so this is the gate for the 0.15 mm (refute-rules 2026-10-08)."""
    clr = limits["clearance_mm"]
    pads = [(r, n, (b[0] - mask_extra, b[1] - mask_extra, b[2] + mask_extra, b[3] + mask_extra)) for r, n, b in scene["pads"]]
    pads = [(r, n, b, box_ink(b)) for r, n, b in pads]
    inks = [(who, ink_of(s)) for who, _owner, s in scene["printed"]] + [(who, box_ink(b)) for who, b in scene["texts"]]
    out = []
    for who, ink in inks:
        bb = ink_box(ink)
        worst = None
        for r, n, b, bi in pads:
            if not _near(bb, b, clr):
                continue
            d = dist(ink, bi)
            if d < clr - 5e-4 and (worst is None or d < worst[0]):
                worst = (d, r, n)
        if worst is not None:
            out.append(f"{who}: {max(0.0, worst[0]):.3f} mm from {worst[1]}{'.' + worst[2] if worst[2] else ''}'s mask opening (the fab's silk clearance {clr:g} mm)")
    return out


def check_text(text: str, design, limits: dict, board=None) -> dict:
    """G1-G6 measured on a board text — the emitted board, the fab board: `{"fails": [facts], "rows", "lines",
    "notes", "silk_mask": [facts]}`. A part's marks are its footprint's own `F.SilkS` marks and the board drawings
    it sits by (`plan`). A part fails when it needs a mark and no printed one is good, or when a printed mark names
    another of its pins, another part or no pin. `silk_mask`: every F.SilkS ink nearer a mask opening than the
    fab's clearance (`silk_mask`)."""
    import re

    from .sexp import edge_outline

    sc = board_scene(text, design)
    if board is None:
        board = edge_outline(text)
    m = re.search(r"\(pad_to_mask_clearance ([-0-9.eE+]+)\)", text)
    got = plan(sc["parts"], sc["printed"], sc["pads"], board, limits, make=False)
    return {"fails": got["errors"], "rows": got["rows"], "lines": [row_line(r) for r in got["rows"]], "notes": got["notes"], "silk_mask": silk_mask(sc, limits, float(m.group(1)) if m else 0.0)}


def _where_printed(s: dict, ref: str, sc: dict, limits: dict) -> dict:
    """A library mark (world frame) where the place stage's silk pass prints it: where the library draws it when that
    spot is legal, else moved whole as `silk._mark_move` moves it — against every pad opening, the other parts' silk
    on the board and its edge — so a fact about what it names is measured where it was judged."""
    from .silk import _grow, _mark_clear, _mark_move, _seg_boxes, _shifted

    clr = limits["clearance_mm"]
    board = sc.get("board") or (-1e9, -1e9, 1e9, 1e9)
    keepouts = [b for _r, _n, b in sc["pads"]]
    silk = [b for who, owner, o in sc["printed"] if owner != ref for b in _seg_boxes(o, clr)]
    hw = s["width"] / 2
    edge = _grow(board, -(clr + hw + 0.005))
    holes = [_grow(k, clr + hw + 0.005) for k in keepouts] + [_grow(k, hw + 0.005) for k in silk]
    if _mark_clear(s, edge, holes):
        return s
    mv = _mark_move(s, keepouts, silk, board, clr)
    return _shifted(s, *mv) if mv is not None else s


def library_mark_reason(design, feet, limits: dict, text: str | None = None):
    """`silk.unprinted_marks`' `reason`: for a part of `design` (`feet`: `place_native.seed_feet`), the fact for a
    library mark the place stage left out because of what it names — another pin of its part, another part's pin
    or no part, or (a dot) no pin — measured as `plan` measures it: on the board `text` (the mark put back at its
    footprint's pose), else in the footprint's own frame against its own pads; None for any other mark."""
    from .silk import _widen, _world

    insts = {i.ref: i for i in design.instances}
    by = {pf.ref: pf for pf in feet}
    sc = board_scene(text, design) if text is not None else None
    world = None
    if sc is not None:
        parts = sc["parts"]
        under = {(p["ref"], q["num"]) for p in parts for q in p["pads"] if under_body(p, q)}
        world = Scene(sc["pads"], [(p["ref"], body_poly(p)) for p in parts], [], (-1e9, -1e9, 1e9, 1e9), limits, pins=None, under=under)
        ops = {p["ref"]: p for p in parts}

    def reason(ref: str, shape: dict) -> str | None:
        inst, pf = insts.get(ref), by.get(ref)
        if inst is None or pf is None:
            return None
        s = sw = _widen({**shape, "bbox": ink_box(ink_of(shape))}, limits["line_mm"])
        if world is not None and ref in ops:
            op = ops[ref]
            scene = world
            s = _world(s, sc["at"][ref])
            s = _where_printed(s, ref, sc, limits)
            if _is_dot(s):
                tail = other_part_fact(ref, scene.owner_of(ink_of(s))[1])
                if tail is not None:
                    return f"{ref}: silkscreen mark not printed: the library's {words(sw)} mark {tail}"
        else:
            op = part_of(ref, (0.0, 0.0, 0.0), pf.lib.pads, body=pf.lib.body, pins=pins_of(inst), kind=inst.part.kind, prefix=inst.part.prefix)
            scene = Scene([(ref, q["num"], q["box"]) for q in op["pads"]], [], [], (-1e9, -1e9, 1e9, 1e9), {})
        n = need(op)
        if n["need"] == "none":
            return None
        at, d0, d1 = points_at(ink_of(s), op, scene)
        if at is not None and at != n["pad"]:
            return f"{ref}: silkscreen mark not printed: the library's {words(sw)} mark is nearest pad {at} ({d0:.2f} mm; every other pad of {ref} at least {d1 - d0:.2f} mm further), and {ref}'s {role(n)} is pad {n['pad']}"
        tail = no_pin_fact(ref, s, scene) if at is None else None
        if tail is not None:
            return f"{ref}: silkscreen mark not printed: the library's {words(sw)} mark {tail}"
        return None

    return reason
