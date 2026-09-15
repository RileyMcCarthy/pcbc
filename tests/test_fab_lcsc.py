from pcbc.fab import jlc_bom, write_bom_csv


def test_bom_reads_lcsc_property(tmp_path):
    pcb = """(kicad_pcb
	(footprint "pcbc:R_0402_1005Metric"
		(layer "F.Cu")
		(uuid "x")
		(at 10 10 0)
		(property "Reference" "R1"
			(at 0 0 0)
			(layer "F.SilkS")
			(effects (font (size 1 1)))
		)
		(property "Value" "1k"
			(at 0 0 0)
			(layer "F.Fab")
			(effects (font (size 1 1)))
		)
		(property "Mpn" "0402WGF1001TCE"
			(at 0 0 0)
			(layer "F.Fab")
			(effects (font (size 1 1)))
		)
		(property "LCSC" "C21190"
			(at 0 0 0)
			(layer "F.Fab")
			(effects (font (size 1 1)))
		)
		(attr smd)
	)
)
"""
    rows, missing = jlc_bom(pcb, {})
    assert missing == []
    assert rows[0]["LCSC Part #"] == "C21190"
    assert rows[0]["Comment"] == "1k"
    write_bom_csv(rows, tmp_path / "bom.csv")
    text = (tmp_path / "bom.csv").read_text()
    assert "C21190" in text
