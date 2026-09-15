import os

import pytest


def pytest_collection_modifyitems(config, items):
    if os.environ.get("PCBC_REQUIRE_KICAD"):
        return
    skip = pytest.mark.skip(reason="kicad-cli (set PCBC_REQUIRE_KICAD=1)")
    for item in items:
        if item.get_closest_marker("kicad"):
            item.add_marker(skip)
