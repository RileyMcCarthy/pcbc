from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "test.yml"


def test_ci_workflow_exists():
    assert WORKFLOW.exists()
    text = WORKFLOW.read_text()
    assert "pytest -q -m \"not kicad\"" in text or "pytest -q -m 'not kicad'" in text
    # The native generator needs no router install (docs/native-plan.md): no step names one.
    assert "KiCadRoutingTools" not in text and "KRT" not in text
    assert "blinky-fab" in text
    assert "c3_usb" in text
    assert "pcbc build" in text
    assert "ppa:kicad/kicad-10.0-releases" in text
