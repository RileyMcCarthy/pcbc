import os

import pytest


def pytest_collection_modifyitems(config, items):
    skip_kicad = pytest.mark.skip(reason="kicad-cli (set PCBC_REQUIRE_KICAD=1)")
    skip_krt = pytest.mark.skip(reason="KiCadRoutingTools (set PCBC_REQUIRE_KRT=1, KRT_HOME)")
    for item in items:
        if item.get_closest_marker("kicad") and not os.environ.get("PCBC_REQUIRE_KICAD"):
            item.add_marker(skip_kicad)
        if item.get_closest_marker("krt") and not os.environ.get("PCBC_REQUIRE_KRT"):
            item.add_marker(skip_krt)
