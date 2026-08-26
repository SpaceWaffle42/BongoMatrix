import tempfile
import unittest
from pathlib import Path

from font_catalog import available_font_names, font_advance_catalog, font_key, text_width


class FontCatalogTests(unittest.TestCase):
    def test_every_shipped_bdf_declares_its_exact_complete_glyph_count(self):
        fonts = Path(__file__).resolve().parents[1] / "resources" / "fonts"
        for path in sorted(fonts.glob("*.bdf")):
            with self.subTest(font=path.name):
                lines = path.read_text(encoding="ascii").splitlines()
                declarations = [line for line in lines if line.startswith("CHARS ")]
                self.assertEqual(len(declarations), 1)
                declared = int(declarations[0].split()[1])
                starts = sum(line.startswith("STARTCHAR ") for line in lines)
                ends = sum(line == "ENDCHAR" for line in lines)
                self.assertEqual((declared, starts, ends), (starts, starts, starts))

    def test_discovers_every_bdf_file_and_normalizes_keys_like_the_renderer(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "z-last.bdf").write_text("STARTFONT 2.1\nENDFONT\n")
            (root / "4x6.bdf").write_text("STARTFONT 2.1\nENDFONT\n")
            (root / "README").write_text("not a font")

            self.assertEqual(available_font_names(root), ("4x6", "z_last"))
            self.assertEqual(font_key(root / "z-last.bdf"), "z_last")

    def test_text_width_uses_bdf_glyph_advances_and_default_character(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "tiny.bdf").write_text(
                "STARTFONT 2.1\nDEFAULT_CHAR 65\nSTARTCHAR A\nENCODING 65\n"
                "DWIDTH 3 0\nBBX 1 1 0 0\nBITMAP\n80\nENDCHAR\nENDFONT\n"
            )

            self.assertEqual(text_width("tiny", "AB", root), 6)
            self.assertEqual(font_advance_catalog(root), {
                "tiny": {"default": 3, "ranges": [[65, 65, 3]]},
            })


if __name__ == "__main__":
    unittest.main()
