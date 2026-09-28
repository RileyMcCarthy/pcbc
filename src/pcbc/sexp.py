"""Tiny KiCad s-expression helpers. Matching-paren, not a full parser."""

from __future__ import annotations

import re
import uuid


def matching_paren(s: str, open_idx: int) -> int:
    depth = 0
    i = open_idx
    n = len(s)
    in_str = False
    while i < n:
        c = s[i]
        if in_str:
            if c == "\\" and i + 1 < n:
                i += 2
                continue
            if c == '"':
                in_str = False
            i += 1
            continue
        if c == '"':
            in_str = True
            i += 1
            continue
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    raise ValueError("unbalanced parentheses")


def board_footprint_spans(text: str) -> list[tuple[int, int]]:
    """Top-level (footprint ...) blocks: newline + single tab + (footprint."""
    spans = []
    start = 0
    needle = "\n\t(footprint"
    while True:
        j = text.find(needle, start)
        if j < 0:
            break
        open_at = j + 1  # tab
        open_at = text.find("(", open_at)
        end = matching_paren(text, open_at)
        spans.append((open_at, end + 1))
        start = end + 1
    return spans


def footprint_reference(block: str) -> str | None:
    m = re.search(r'\(property "Reference" "([^"]*)"', block)
    return m.group(1) if m else None


def footprint_at(block: str) -> tuple[float, float, float] | None:
    """Footprint placement, not a property (at ...)."""
    m = re.search(r"\n\t\t\(at ([0-9.+-]+) ([0-9.+-]+)(?: ([0-9.+-]+))?\)", block)
    if not m:
        m = re.search(r"\(at ([0-9.+-]+) ([0-9.+-]+)(?: ([0-9.+-]+))?\)", block)
    if not m:
        return None
    rot = float(m.group(3) or 0)
    return float(m.group(1)), float(m.group(2)), rot


def has_edge_cuts_shape(text: str) -> bool:
    """True if a board-level graphic is on Edge.Cuts (not the layer table).

    KiCad writes ``(layer "Edge.Cuts")`` several lines below ``(gr_rect``, so a
    short look-behind misses the outline. Footprint ``fp_*`` on Edge.Cuts (a
    USB-C shell notch) is not a board outline.
    """
    i = 0
    while True:
        j = text.find('(layer "Edge.Cuts")', i)
        if j < 0:
            return False
        window = text[max(0, j - 500) : j]
        gr = window.rfind("(gr_")
        fp = window.rfind("(fp_")
        if gr >= 0 and gr > fp:
            return True
        i = j + 1


def new_uuid() -> str:
    return str(uuid.uuid4())


_NS = uuid.UUID("7c9e6679-7425-40de-944b-e07fc1f90ae7")


def stable_uuid(*parts: object) -> str:
    """Deterministic UUIDv5 so rebuilds do not churn git."""
    return str(uuid.uuid5(_NS, "|".join(str(p) for p in parts)))


_ANY_UUID = re.compile(r'\(uuid\s+"([^"]*)"\)')


def pin_all_uuids(text: str, *key: object, keep: frozenset[str] = frozenset()) -> str:
    """Every uuid in file order, keyed by position. KiCad invents random ids for items that
    have none when it saves a board (pads, footprint fields, graphics); after the copper gate
    refills and saves, this puts the file back on a deterministic footing.

    `keep` is pcbc's own copper: those ids are derived from the piece's geometry
    (`route_emit.piece_key`) and named by `layout/<board>/routed/copper.json`, so re-keying them here
    would leave the sidecar pointing at ids that no longer exist and a piece untraceable in KiCad's
    UI. A kept id still consumes its position, so nothing else moves (`docs/r2-design.md` D.4)."""
    n = 0

    def sub(m: re.Match) -> str:
        nonlocal n
        n += 1
        return m.group(0) if m.group(1) in keep else f'(uuid "{stable_uuid(*key, "id", n)}")'

    return _ANY_UUID.sub(sub, text)


# ------------------------------------------------------------------ a real reader, for the decompiler


class Q(str):
    """A quoted atom. KiCad writes some values in double quotes (`(layer "F.Cu")`) and some bare
    (`(hatch edge 0.5)`); the difference is part of the file, so the tree keeps it."""


# KiCad's escapes inside a quoted string (`EscapeString(..., CTX_QUOTED_STR)` and its inverse), measured
# with `kicad-cli pcb upgrade` on a `gr_text` carrying every one of them (third review, P2): a newline
# is written `\n`, a carriage return `\r`, a double quote `\"`, a backslash `\\`; a tab is written as
# itself. The reader also accepts `\t`, which KiCad's reader takes, but never writes it.
_UNESCAPE = {"n": "\n", "r": "\r", "t": "\t", '"': '"', "\\": "\\"}
_ESCAPE = {"\\": "\\\\", '"': '\\"', "\n": "\\n", "\r": "\\r"}


def parse_tree(text: str) -> list:
    """The whole file as nested lists of atoms: bare atoms are `str`, quoted ones are `Q`.

    The escapes KiCad writes inside a string (`_UNESCAPE`) are decoded, so a text with a real newline
    in it reads back as that text and not as the two characters `\\n`."""
    stack: list[list] = [[]]
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c == "(":
            stack.append([])
            i += 1
        elif c == ")":
            node = stack.pop()
            stack[-1].append(node)
            i += 1
        elif c in " \t\r\n":
            i += 1
        elif c == '"':
            j = i + 1
            buf: list[str] = []
            while text[j] != '"':
                if text[j] == "\\" and j + 1 < n and text[j + 1] in _UNESCAPE:
                    buf.append(_UNESCAPE[text[j + 1]])
                    j += 2
                else:
                    buf.append(text[j])
                    j += 1
            stack[-1].append(Q("".join(buf)))
            i = j + 1
        else:
            j = i
            while j < n and text[j] not in " \t\r\n()":
                j += 1
            stack[-1].append(text[i:j])
            i = j
    if len(stack) != 1 or not stack[0]:
        raise ValueError("unbalanced parentheses")
    return stack[0][0]


def fmt_num(v: float) -> str:
    """A number the way KiCad 10 writes a length: at most six decimals (1 nm), no trailing zeros."""
    s = f"{float(v):.6f}".rstrip("0").rstrip(".")
    return "0" if s in ("-0", "") else s


def quote(text: str) -> str:
    """A string as KiCad's writer spells it (`_ESCAPE`): a newline is `\\n`, never a raw line break,
    which KiCad's parser cannot read inside a token."""
    return '"' + "".join(_ESCAPE.get(ch, ch) for ch in str(text)) + '"'


def fmt_exact(v: float) -> str:
    """A number KiCad keeps as a double rather than in nanometres — a ratio, an angle, an area, a
    percentage — written so it reads back as the very same float: the shortest decimal that
    round-trips (`repr`), in positional form, no exponent, no trailing zeros. `fmt_num` would cut a
    teardrop's `0.1234567891` to `0.123457`; this writes it back as KiCad wrote it."""
    from decimal import Decimal

    f = float(v)
    if f == 0:
        return "0"
    s = format(Decimal(repr(f)), "f")
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s
