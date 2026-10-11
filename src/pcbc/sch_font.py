"""How wide KiCad draws a string in its stroke font: the schematic's text model (docs/schematic-layout.md §2.6).

The advance of every printable ASCII character at 1.27 mm, measured on `kicad-cli sch export svg` (KiCad 10): the
`textLength` KiCad writes over each drawn text, for ten of the character in a row, less the 0.2 mm a string carries
whatever its length. A string is the sum of its characters' advances plus that 0.2 mm, scaled by its size; checked on
mixed strings (R_TDIV 6.29 against KiCad's 6.37, AP2112K-3.3TRG1 18.08 against 17.98). The overbar markup `~{...}` draws
its contents only.
"""

from __future__ import annotations

import re

ADVANCE = {
    " ": 0.9679, "!": 0.6051, '"': 0.7865, "#": 1.2703, "$": 1.2098, "%": 1.4517, "&": 1.5727, "'": 0.6051, "(": 0.847,
    ")": 0.847, "*": 0.9679, "+": 1.5727, ",": 0.6051, "-": 1.5727, ".": 0.6051, "/": 1.3308, "0": 1.2098, "1": 1.2098,
    "2": 1.2098, "3": 1.2098, "4": 1.2098, "5": 1.2098, "6": 1.2098, "7": 1.2098, "8": 1.2098, "9": 1.2098, ":": 0.6051,
    ";": 0.6051, "<": 1.5727, "=": 1.5727, ">": 1.5727, "?": 1.0889, "@": 1.6332, "A": 1.0889, "B": 1.2703, "C": 1.2703,
    "D": 1.2703, "E": 1.1493, "F": 1.0889, "G": 1.2703, "H": 1.3308, "I": 0.6051, "J": 0.9679, "K": 1.2703, "L": 1.0284,
    "M": 1.4517, "N": 1.3308, "O": 1.3308, "P": 1.2703, "Q": 1.3308, "R": 1.2703, "S": 1.2098, "T": 0.9679, "U": 1.3308,
    "V": 1.0889, "W": 1.4517, "X": 1.2098, "Y": 1.0889, "Z": 1.2098, "[": 0.847, "\\": 1.3308, "]": 0.847, "^": 0.726,
    "_": 0.9679, "`": 0.4841, "a": 1.1493, "b": 1.1493, "c": 1.0889, "d": 1.1493, "e": 1.0889, "f": 0.726, "g": 1.1493,
    "h": 1.1493, "i": 0.6051, "j": 0.6051, "k": 1.0284, "l": 0.6655, "m": 1.6936, "n": 1.1493, "o": 1.1493, "p": 1.1493,
    "q": 1.1493, "r": 0.7865, "s": 1.0284, "t": 0.726, "u": 1.1493, "v": 0.9679, "w": 1.3308, "x": 1.0284, "y": 0.9679,
    "z": 1.0284, "{": 0.847, "|": 1.2098, "}": 0.847, "~": 0.9074,
}
_STRING = 0.2
_DEFAULT = 1.1493  # a character the table does not hold: a lower-case letter's advance
_OVERBAR = re.compile(r"~\{([^}]*)\}")


def text_width(s: str, size: float = 1.27) -> float:
    """The length KiCad draws `s` at `size` (mm), in mm."""
    s = _OVERBAR.sub(r"\1", s or "")
    if not s:
        s = " "
    return (sum(ADVANCE.get(c, _DEFAULT) for c in s) + _STRING) * size / 1.27
