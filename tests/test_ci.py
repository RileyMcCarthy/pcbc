from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "test.yml"


def test_ci_workflow_exists():
    assert WORKFLOW.exists()
    text = WORKFLOW.read_text()
    assert "pytest -q -m \"not kicad and not krt\"" in text or "pytest -q -m 'not kicad and not krt'" in text
    assert "blinky-fab" in text
    assert "c3_usb" in text
    assert "pcbc build" in text
    assert "ppa:kicad/kicad-10.0-releases" in text
