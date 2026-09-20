"""pcbc: Python in, fab-ready KiCad out."""

from .css import AUTO
from .language import (
    Board,
    Bus,
    Capacitor,
    Chain,
    Component,
    Ground,
    Guard,
    Isolation,
    Keepout,
    Led,
    NetReq,
    Net_,
    Pair,
    Place,
    Power,
    Region,
    Resistor,
    SchPlace,
    SchRegion,
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
    "Bus",
    "Capacitor",
    "Chain",
    "Component",
    "Design",
    "Ground",
    "Guard",
    "Instance",
    "Isolation",
    "Keepout",
    "Led",
    "Net",
    "NetReq",
    "Pair",
    "Part",
    "Pin",
    "Place",
    "Power",
    "Region",
    "Resistor",
    "SchPlace",
    "SchRegion",
    "check_board",
    "load",
    "load_board",
]
