"""pcbc: Python in, fab-ready KiCad out."""

from .css import AUTO
from .language import (
    Board,
    Capacitor,
    Component,
    Ground,
    Keepout,
    Led,
    NetReq,
    Net_,
    Place,
    Power,
    Region,
    Resistor,
    check_board,
    load,
    load_board,
)
from .model import BoardSpec, Design, Instance, Part, Pin

Net = Net_

__version__ = "0.1.0"
__all__ = [
    "AUTO",
    "Board",
    "BoardSpec",
    "Capacitor",
    "Component",
    "Design",
    "Ground",
    "Instance",
    "Keepout",
    "Led",
    "Net",
    "NetReq",
    "Part",
    "Pin",
    "Place",
    "Power",
    "Region",
    "Resistor",
    "check_board",
    "load",
    "load_board",
]
