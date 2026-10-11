"""The picture manifest names the saved schematic sheets, two per board (docs/schematic-layout.md §7).

Vibes reads vibes.images.json on a pull request and lays each changed sheet
out as current, new, and difference. This check does not draw anything: it
only keeps the manifest pointed at the pictures that are actually committed:
<board>.png (with the board's schematic.core.py) and <board>.auto.png (the bare
auto-layout), each with its SVG and its score file.
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
    assert [row["id"] for row in images] == [i for b in BOARDS for i in (b, f"{b}-auto")]
    for row in images:
        # kind and board are this repo's metadata. Vibes prints any string
        # field and does not treat these names as special.
        assert row["kind"] == "schematic"
        auto = row["id"].endswith("-auto")
        assert row["board"] == (row["id"][: -len("-auto")] if auto else row["id"])
        assert row["title"] and (("auto, no core" in row["title"]) == auto)
        path = ROOT / row["path"]
        assert path == ROOT / "tests" / "benchmarks" / "schematics" / f"{row['board']}{'.auto' if auto else ''}.png"
        assert path.is_file()
        assert path.with_suffix(".svg").is_file()
        assert path.with_name(path.stem + ".score.json").is_file()
