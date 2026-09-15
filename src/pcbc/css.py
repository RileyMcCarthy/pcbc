"""CSS-named box model. Unitless numbers are millimetres.

Locked parts are ``position: absolute`` inside the board (or a Region).
Footprints have intrinsic size from the courtyard, like a replaced element.
``margin: auto`` centers on the free axis. ``padding`` lives on Board/Region.

This is CSS 2.1 absolute positioning, not a browser. False friends are errors
so an AI that dumps web CSS cannot silently mean the wrong thing.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, fields, replace

AUTO = "auto"

# Properties we will never honor, with the PCB word to use instead.
_FORBIDDEN = {
    "z-index": "z-index is not a copper layer. Use side='F' or side='B'.",
    "z_index": "z-index is not a copper layer. Use side='F' or side='B'.",
    "color": "color is not a placement property.",
    "background": "background is not a placement property.",
    "background-color": "background is not a placement property.",
    "background_color": "background is not a placement property.",
    "font": "font is not a placement property.",
    "font-size": "font is not a placement property.",
    "font_size": "font is not a placement property.",
    "flex": "flex is not a placer. Put unlocked parts in a Region; the engine packs them.",
    "flex-direction": "flex is not a placer. Put unlocked parts in a Region; the engine packs them.",
    "flex_direction": "flex is not a placer. Put unlocked parts in a Region; the engine packs them.",
    "flex-grow": "flex is not a placer. Put unlocked parts in a Region; the engine packs them.",
    "flex_grow": "flex is not a placer. Put unlocked parts in a Region; the engine packs them.",
    "grid": "grid is not a placer. Put unlocked parts in a Region; the engine packs them.",
    "grid-template-columns": "grid is not a placer. Put unlocked parts in a Region; the engine packs them.",
    "grid_template_columns": "grid is not a placer. Put unlocked parts in a Region; the engine packs them.",
    "float": "float is not a placement property.",
    "gap": "gap is a packing knob for the engine, not a locked-part inset. Use margin.",
}

_FORBIDDEN_POSITION = {
    "sticky": "position:sticky needs a scrollport. Use position='absolute'.",
    "fixed": "position:fixed needs a viewport. Use position='absolute'.",
    "relative": "position:relative is not used. Locked parts are absolute; unlocked parts are static (engine).",
}

_INSET_KEYS = ("top", "right", "bottom", "left")
_MARGIN_KEYS = ("margin_top", "margin_right", "margin_bottom", "margin_left")
_PAD_KEYS = ("padding_top", "padding_right", "padding_bottom", "padding_left")


@dataclass
class Percent:
    frac: float  # 0.5 for 50%


@dataclass
class BoxStyle:
    position: str = "static"
    top: object | None = None
    right: object | None = None
    bottom: object | None = None
    left: object | None = None
    width: object | None = None
    height: object | None = None
    margin_top: object = 0
    margin_right: object = 0
    margin_bottom: object = 0
    margin_left: object = 0
    padding_top: object = 0
    padding_right: object = 0
    padding_bottom: object = 0
    padding_left: object = 0
    rotate: float = 0.0
    translate_x: object | None = None
    translate_y: object | None = None
    from_box: str = "courtyard"  # courtyard | origin | pads | fab
    parent: str | None = None

    def has_insets(self) -> bool:
        return any(getattr(self, k) is not None for k in _INSET_KEYS + ("width", "height"))

    def is_absolute(self) -> bool:
        return self.position == "absolute" or self.has_insets()


@dataclass
class Rect:
    x0: float
    y0: float
    x1: float
    y1: float

    @property
    def w(self) -> float:
        return self.x1 - self.x0

    @property
    def h(self) -> float:
        return self.y1 - self.y0

    @property
    def width(self) -> float:
        return self.w

    @property
    def height(self) -> float:
        return self.h

    def content_box(self, style: BoxStyle) -> Rect:
        """Padding edge → content box (CSS content-box inside padding)."""
        pt = _mm(style.padding_top, self.h)
        pr = _mm(style.padding_right, self.w)
        pb = _mm(style.padding_bottom, self.h)
        pl = _mm(style.padding_left, self.w)
        return Rect(self.x0 + pl, self.y0 + pt, self.x1 - pr, self.y1 - pb)


def parse_length(value) -> object | None:
    """None | AUTO | float mm | Percent. Rejects px/em/rem/vh."""
    if value is None:
        return None
    if value is AUTO or value == AUTO:
        return AUTO
    if isinstance(value, Percent):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    s = str(value).strip()
    if not s:
        return None
    if s.lower() == AUTO:
        return AUTO
    if s.endswith("%"):
        return Percent(float(s[:-1]) / 100.0)
    lower = s.lower()
    for unit in ("px", "em", "rem", "vh", "vw", "pt", "cm", "in"):
        if lower.endswith(unit):
            raise ValueError(
                f"length {value!r}: only millimetres and % (unitless = mm). "
                f"Do not use {unit}."
            )
    if lower.endswith("mm"):
        s = s[:-2].strip()
    return float(s)


def _mm(value, percent_of: float) -> float:
    v = parse_length(value)
    if v is None or v is AUTO:
        raise ValueError(f"expected a definite length, got {value!r}")
    if isinstance(v, Percent):
        return v.frac * percent_of
    return float(v)


def _maybe_mm(value, percent_of: float) -> object:
    """None | AUTO | float."""
    v = parse_length(value)
    if v is None or v is AUTO:
        return v
    if isinstance(v, Percent):
        return v.frac * percent_of
    return float(v)


def expand_shorthand(value) -> tuple:
    """CSS 1–4 value shorthand → (top, right, bottom, left)."""
    if value is None:
        return (0, 0, 0, 0)
    if isinstance(value, (list, tuple)):
        parts = list(value)
    else:
        if isinstance(value, str) and any(c in value for c in " ,"):
            parts = [p for p in re.split(r"[,\s]+", value.strip()) if p]
        else:
            parts = [value]
    if len(parts) == 1:
        t = r = b = l = parts[0]
    elif len(parts) == 2:
        t = b = parts[0]
        r = l = parts[1]
    elif len(parts) == 3:
        t, r, b = parts
        l = r
    elif len(parts) == 4:
        t, r, b, l = parts
    else:
        raise ValueError(f"shorthand expects 1–4 values, got {value!r}")
    return (t, r, b, l)


def parse_style_string(style: str) -> dict:
    """Parse a CSS declaration list into kwargs (kebab or snake)."""
    out: dict = {}
    for raw in style.split(";"):
        item = raw.strip()
        if not item:
            continue
        if ":" not in item:
            raise ValueError(f"bad CSS declaration {item!r}")
        key, val = item.split(":", 1)
        out[_norm_key(key)] = val.strip()
    return out


def _norm_key(key: str) -> str:
    return key.strip().lower().replace("-", "_")


def check_forbidden(kwargs: dict, *, who: str) -> None:
    for key, val in list(kwargs.items()):
        nk = _norm_key(key)
        if nk in _FORBIDDEN:
            raise ValueError(f"{who}: {_FORBIDDEN[nk]}")
        if nk == "display":
            dv = str(val).strip().lower()
            if dv in ("flex", "inline-flex", "grid", "inline-grid"):
                raise ValueError(f"{who}: {_FORBIDDEN['flex']}")
            if dv not in ("block", "contents", "none", ""):
                raise ValueError(
                    f"{who}: display={val!r} is not used. "
                    "Locked parts are position='absolute'."
                )


def parse_transform(transform: str | None) -> dict:
    """Subset: translate / translateX / translateY / rotate. Layout-affecting."""
    out: dict = {}
    if not transform:
        return out
    s = transform.strip()
    for m in re.finditer(r"([a-zA-Z]+)\(\s*([^)]*)\)", s):
        fn, args = m.group(1).lower(), m.group(2)
        parts = [p.strip() for p in args.split(",") if p.strip()]
        if fn == "translatex" and parts:
            out["translate_x"] = parts[0]
        elif fn == "translatey" and parts:
            out["translate_y"] = parts[0]
        elif fn == "translate" and parts:
            out["translate_x"] = parts[0]
            out["translate_y"] = parts[1] if len(parts) > 1 else 0
        elif fn == "rotate" and parts:
            out["rotate"] = _parse_angle(parts[0])
        else:
            raise ValueError(
                f"unsupported transform {fn}(). "
                "Supported: translate, translateX, translateY, rotate."
            )
    return out


def _parse_angle(value) -> float:
    s = str(value).strip().lower()
    if s.endswith("deg"):
        s = s[:-3].strip()
    elif s.endswith("rad"):
        return math.degrees(float(s[:-3].strip()))
    elif s.endswith("turn"):
        return float(s[:-4].strip()) * 360.0
    return float(s)


def box_style_from_kwargs(
    *,
    who: str,
    style: str | None = None,
    position: str | None = None,
    rotate: float | None = None,
    transform: str | None = None,
    box: str | None = None,
    parent: str | None = None,
    **kwargs,
) -> BoxStyle:
    """Build a BoxStyle from CSS kwargs / a style= string. kwargs win."""
    raw: dict = {}
    if style:
        raw.update(parse_style_string(style))
    for k, v in kwargs.items():
        raw[_norm_key(k)] = v
    if transform:
        raw["transform"] = transform
    if position is not None:
        raw["position"] = position
    if box is not None:
        raw["from_box"] = box
    if parent is not None:
        raw["parent"] = parent
    if "box" in raw:
        raw.setdefault("from_box", raw.pop("box"))
        raw.pop("box", None)
    if rotate is not None:
        raw["rotate"] = rotate

    check_forbidden(raw, who=who)

    pos = str(raw.get("position", "static") or "static").lower()
    if pos in _FORBIDDEN_POSITION:
        raise ValueError(f"{who}: {_FORBIDDEN_POSITION[pos]}")
    if pos not in ("static", "absolute"):
        raise ValueError(f"{who}: position={pos!r} is not static or absolute")

    if "transform" in raw:
        raw.update(parse_transform(str(raw.pop("transform"))))

    if "inset" in raw:
        t, r, b, l = expand_shorthand(raw.pop("inset"))
        raw.setdefault("top", t)
        raw.setdefault("right", r)
        raw.setdefault("bottom", b)
        raw.setdefault("left", l)

    if "margin" in raw:
        t, r, b, l = expand_shorthand(raw.pop("margin"))
        raw.setdefault("margin_top", t)
        raw.setdefault("margin_right", r)
        raw.setdefault("margin_bottom", b)
        raw.setdefault("margin_left", l)

    if "padding" in raw:
        t, r, b, l = expand_shorthand(raw.pop("padding"))
        raw.setdefault("padding_top", t)
        raw.setdefault("padding_right", r)
        raw.setdefault("padding_bottom", b)
        raw.setdefault("padding_left", l)

    known = {f.name for f in fields(BoxStyle)}
    leftover = [k for k in raw if k not in known and k not in ("rotate",)]
    # rotate is known. display we already handled.
    leftover = [
        k
        for k in leftover
        if k
        not in (
            "display",
            "side",
            "locked",
            "reason",
            "ref",
            "at",
            "rot",
            "box",
            "from_box",
        )
    ]
    if leftover:
        raise ValueError(
            f"{who}: unknown CSS property {leftover[0]!r}. "
            "Known: position, top, right, bottom, left, inset, width, height, "
            "margin, padding, transform, rotate, box, parent."
        )

    for k in _INSET_KEYS + _MARGIN_KEYS + _PAD_KEYS + ("width", "height", "translate_x", "translate_y"):
        if k in raw and raw[k] is not None:
            try:
                parse_length(raw[k])
            except ValueError as e:
                raise ValueError(f"{who}: {e}") from e

    bs = BoxStyle(position=pos)
    for k in known:
        if k in raw and raw[k] is not None:
            setattr(bs, k, raw[k])
    if "rotate" in raw and raw["rotate"] is not None:
        bs.rotate = _parse_angle(raw["rotate"]) if not isinstance(raw["rotate"], (int, float)) else float(raw["rotate"])
    if bs.has_insets() and bs.position == "static":
        bs.position = "absolute"
    return bs


def style_to_fields(style: BoxStyle) -> dict:
    return {f.name: getattr(style, f.name) for f in fields(BoxStyle)}


def resolve_rect(
    cb: Rect,
    style: BoxStyle,
    *,
    intrinsic_w: float | None = None,
    intrinsic_h: float | None = None,
    who: str = "box",
) -> Rect:
    """Resolve a CSS absolute box to a world-axis-aligned rect (border box).

    Footprints pass intrinsic courtyard size (replaced). Keepouts/Regions pass
    width/height or stretch via left+right / top+bottom.
    """
    if not style.is_absolute():
        raise ValueError(f"{who}: nothing to resolve (position is static and no insets)")

    left = _maybe_mm(style.left, cb.w)
    right = _maybe_mm(style.right, cb.w)
    top = _maybe_mm(style.top, cb.h)
    bottom = _maybe_mm(style.bottom, cb.h)
    ml = _maybe_mm(style.margin_left, cb.w)
    mr = _maybe_mm(style.margin_right, cb.w)
    mt = _maybe_mm(style.margin_top, cb.h)
    mb = _maybe_mm(style.margin_bottom, cb.h)

    width = None if style.width is None else _maybe_mm(style.width, cb.w)
    height = None if style.height is None else _maybe_mm(style.height, cb.h)
    if width is AUTO:
        width = None
    if height is AUTO:
        height = None

    used_w = _used_size(
        cb_size=cb.w,
        start=left,
        end=right,
        ms=ml,
        me=mr,
        specified=width,
        intrinsic=intrinsic_w,
        who=who,
        axis="width",
    )
    used_h = _used_size(
        cb_size=cb.h,
        start=top,
        end=bottom,
        ms=mt,
        me=mb,
        specified=height,
        intrinsic=intrinsic_h,
        who=who,
        axis="height",
    )

    x0, ml_u, mr_u = _resolve_axis(
        cb0=cb.x0,
        cb_size=cb.w,
        start=left,
        end=right,
        ms=ml,
        me=mr,
        used=used_w,
        who=who,
        axis="horizontal",
    )
    y0, mt_u, mb_u = _resolve_axis(
        cb0=cb.y0,
        cb_size=cb.h,
        start=top,
        end=bottom,
        ms=mt,
        me=mb,
        used=used_h,
        who=who,
        axis="vertical",
    )
    del ml_u, mr_u, mt_u, mb_u
    rect = Rect(x0, y0, x0 + used_w, y0 + used_h)

    # transform translate is layout-affecting here (unlike CSS), so centering
    # via translateY(-50%) actually moves the courtyard.
    dx = 0.0 if style.translate_x is None else _mm(style.translate_x, rect.w)
    dy = 0.0 if style.translate_y is None else _mm(style.translate_y, rect.h)
    if dx or dy:
        rect = Rect(rect.x0 + dx, rect.y0 + dy, rect.x1 + dx, rect.y1 + dy)
    return rect


def _used_size(
    *,
    cb_size: float,
    start,
    end,
    ms,
    me,
    specified,
    intrinsic,
    who: str,
    axis: str,
) -> float:
    if specified is not None and specified is not AUTO:
        return float(specified)
    if intrinsic is not None:
        return float(intrinsic)
    if start is not None and start is not AUTO and end is not None and end is not AUTO:
        msv = 0.0 if ms is None or ms is AUTO else float(ms)
        mev = 0.0 if me is None or me is AUTO else float(me)
        size = cb_size - float(start) - float(end) - msv - mev
        if size < 0:
            raise ValueError(f"{who}: {axis} would be {size:.3f} mm (negative)")
        return size
    raise ValueError(
        f"{who}: cannot determine {axis}. Set width/height, or both "
        f"{'left and right' if axis == 'width' else 'top and bottom'}, "
        "or place a footprint (intrinsic courtyard)."
    )


def _resolve_axis(
    *,
    cb0: float,
    cb_size: float,
    start,
    end,
    ms,
    me,
    used: float,
    who: str,  # noqa: ARG001 — kept for call-site symmetry with _used_size
    axis: str,
) -> tuple[float, float, float]:
    """Return (border_start, margin_start, margin_end).

    ``start`` is left/top, ``end`` is right/bottom. CSS 2.1 replaced absolute.
    """
    start_auto = start is None or start is AUTO
    end_auto = end is None or end is AUTO
    ms_auto = ms is None or ms is AUTO
    me_auto = me is None or me is AUTO
    msv = 0.0 if ms_auto else float(ms)
    mev = 0.0 if me_auto else float(me)
    sv = 0.0 if start_auto else float(start)
    ev = 0.0 if end_auto else float(end)

    # leftover inside the CB after the used size
    # constraint: start + ms + used + me + end = cb_size
    if start_auto and end_auto:
        # auto/auto: center if both margins auto, else start at 0
        if ms_auto and me_auto:
            extra = cb_size - used
            msv = mev = extra / 2.0
        elif ms_auto:
            msv = cb_size - used - mev
        elif me_auto:
            mev = cb_size - used - msv
        sv = 0.0
        ev = cb_size - sv - msv - used - mev
    elif start_auto:
        # end specified: pin to the end edge
        if ms_auto:
            msv = 0.0
        if me_auto:
            mev = 0.0
        sv = cb_size - ev - mev - used - msv
    elif end_auto:
        if ms_auto:
            msv = 0.0
        if me_auto:
            mev = 0.0
        ev = cb_size - sv - msv - used - mev
    else:
        # both start and end specified, size intrinsic → remaining goes to auto margins
        remaining = cb_size - sv - ev - used
        if ms_auto and me_auto:
            msv = mev = remaining / 2.0
        elif ms_auto:
            msv = remaining - mev
        elif me_auto:
            mev = remaining - msv
        else:
            # over-constrained LTR: ignore end
            ev = cb_size - sv - msv - used - mev

    border0 = cb0 + sv + msv
    return border0, msv, mev


def origin_from_border(
    border: Rect,
    local_box: tuple[float, float, float, float],
    rotate_deg: float,
) -> tuple[float, float]:
    """KiCad footprint origin that puts the rotated local box on ``border``."""
    ox0, oy0, ox1, oy1 = rotate_local_bounds(*local_box, rotate_deg)
    return (border.x0 - ox0, border.y0 - oy0)


def rotate_local_bounds(
    lmin_x: float,
    lmin_y: float,
    lmax_x: float,
    lmax_y: float,
    rotation_deg: float,
) -> tuple[float, float, float, float]:
    """AABB of a local box after KiCad footprint rotation (negated angle)."""
    rot = rotation_deg % 360.0
    if abs(rot) < 1e-12 or abs(rot - 360.0) < 1e-12:
        return lmin_x, lmin_y, lmax_x, lmax_y
    angle = math.radians(-rot)
    cos_a, sin_a = math.cos(angle), math.sin(angle)
    corners = [
        (lmin_x, lmin_y),
        (lmax_x, lmin_y),
        (lmin_x, lmax_y),
        (lmax_x, lmax_y),
    ]
    xs = [x * cos_a - y * sin_a for x, y in corners]
    ys = [x * sin_a + y * cos_a for x, y in corners]
    return min(xs), min(ys), max(xs), max(ys)


def apply_style_to_spec(spec, style: BoxStyle):
    """Copy BoxStyle fields onto a dataclass that has the same names."""
    data = {k: v for k, v in style_to_fields(style).items() if hasattr(spec, k)}
    return replace(spec, **data)
