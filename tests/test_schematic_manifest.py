"""The picture manifest names the saved schematic sheets.

Vibes reads vibes.images.json on a pull request and lays each changed sheet
out as current, new, and difference. This check does not draw anything: it
only keeps the manifest pointed at the pictures that are actually committed.
"""

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "vibes.images.json"
BOARDS = ("blinky", "buck", "c3_usb", "node")


def test_manifest_names_each_saved_schematic():
    doc = json.loads(MANIFEST.read_text())
    assert doc["v"] == 1
    images = doc["images"]
    assert [row["id"] for row in images] == list(BOARDS)
    for row in images:
        assert row["kind"] == "schematic"
        assert row["board"] == row["id"]
        assert row["title"]
        path = ROOT / row["path"]
        assert path == ROOT / "tests" / "benchmarks" / "schematics" / f"{row['id']}.png"
        assert path.is_file()
        assert path.with_suffix(".svg").is_file()
