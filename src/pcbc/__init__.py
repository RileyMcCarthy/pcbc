"""pcbc: Python in, fab-ready KiCad out."""

from .css import AUTO
from .language import (
    Board,
    Bridge,
    Bus,
    Capacitor,
    Chain,
    Loop,
    Path,
    Component,
    Ground,
    Guard,
    Isolation,
    Led,
    NetReq,
    Net_,
    Pair,
    Power,
    Resistor,
    SchLabel,
    SchNet,
    SchPlace,
    SchPower,
    SchRegion,
    SchSheet,
    SchText,
    Seg,
    Arc,
    Via,
    Pour,
    Line,
    Rect,
    Circle,
    DrawArc,
    Poly,
    Curve,
    Text,
    TextBox,
    Dimension,
    Group,
    Image,
    Table,
    Barcode,
    Target,
    Point,
    Generated,
    Thermal,
    check_board,
    load,
    load_board,
    load_layout,
)
from .model import BoardSpec, Design, Instance, Part, Pin

Net = Net_

# `Place`, `Region`, `Keepout` and `Outline` are `layout.core.py`'s (docs/direction.md §2, 2026-10-03): its
# namespace provides them, and `board.py` holds the circuit and its physics only. `from pcbc import Place`
# is refused on the import line (PEP 562), naming the board file and line when a board is loading.
_CORE_ONLY = ("Place", "Region", "Keepout", "Outline")


def __getattr__(name: str):
    if name in _CORE_ONLY:
        from . import language as _lang

        if _lang._layout_path is not None and _lang._layout_source == "core":
            return getattr(_lang, name)  # `layout.core.py` itself: its own constructors, as its namespace has them
        board = _lang._board_path
        if board is not None and _lang._board_exec:
            line = _lang._board_line()
            where = f"{board.name}:{line}: " if line else f"{board.name}: "
            raise ValueError(f"{where}pcbc exports no {name}: {name} is layout.core.py's (next to {board.name}), and {board.name} holds the circuit and its physics only")
        raise ValueError(f"pcbc exports no {name}: {name} is layout.core.py's, and board.py holds the circuit and its physics only")
    raise AttributeError(f"module 'pcbc' has no attribute {name!r}")

__version__ = "0.1.0"
__all__ = [
    "AUTO",
    "Board",
    "BoardSpec",
    "Bridge",
    "Bus",
    "Capacitor",
    "Chain",  # the refusal only: Path() replaced it
    "Loop",
    "Path",
    "Component",
    "Design",
    "Ground",
    "Guard",
    "Instance",
    "Isolation",
    "Led",
    "Net",
    "NetReq",
    "Pair",
    "Part",
    "Pin",
    "Power",
    "Resistor",
    "SchLabel",
    "SchNet",
    "SchPlace",
    "SchPower",
    "SchRegion",
    "SchSheet",
    "SchText",
    "Seg",
    "Arc",
    "Via",
    "Pour",
    "Line",
    "Rect",
    "Circle",
    "DrawArc",
    "Poly",
    "Curve",
    "Text",
    "TextBox",
    "Dimension",
    "Group",
    "Image",
    "Table",
    "Barcode",
    "Target",
    "Point",
    "Generated",
    "Thermal",
    "check_board",
    "load",
    "load_board",
    "load_layout",
]
