
from pcbc.review import render_html


def test_review_html_has_schematic_layout_3d_tabs():
    page = render_html(
        title="blinky",
        board_mm=(40.0, 25.0),
        layers=2,
        stackup="jlcpcb_2l_1oz",
        pcb_name="layout.kicad_pcb",
        source_text="Board(width=40, height=25)\n",
        sch_svg='<svg xmlns="http://www.w3.org/2000/svg"></svg>',
        front_svg="<svg></svg>",
        back_svg=None,
        copper_svg="<svg></svg>",
        silk_svg=None,
        glb_b64="AAAA",
        bom_rows=[["Comment", "Designator"], ["1k", "R1"]],
        notes=["ok"],
    )
    assert 'data-tab="sch"' in page
    assert 'data-tab="front"' in page
    assert 'data-tab="three"' in page
    assert "model-viewer" in page
    assert "Board(width=40, height=25)" in page
    assert "default.net" not in page
    assert "pcb apply schematic" not in page
