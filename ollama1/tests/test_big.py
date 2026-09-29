"""The pairing code in large letters is readable: every character of the
code alphabet has its own glyph, and the rendering actually draws it."""
import unittest

import o1test_util as U  # noqa: F401
import o1big
from o1auth import CODE_ALPHABET


class TestBig(unittest.TestCase):
    def test_every_code_character_has_a_distinct_glyph(self):
        glyphs = [tuple(o1big.FONT[c]) for c in CODE_ALPHABET]
        self.assertEqual(len(set(glyphs)), len(CODE_ALPHABET))

    def test_rendering_draws_the_glyphs(self):
        rows = o1big.render("7K", on="#", scale=1)
        self.assertEqual(rows[0], o1big.FONT["7"][0] + " " + o1big.FONT["K"][0] + " ")
        self.assertTrue(any("#" in r for r in rows))

    def test_code_fits_the_width(self):
        code = "7K4M-2QXD-9FHT"
        for width in (200, 100, 60, 40):
            rows = o1big.render_code(code, width, on="#")
            self.assertLessEqual(max(len(r) for r in rows), max(width, 30), width)
            self.assertGreater(sum(r.count("#") for r in rows), 100)


if __name__ == "__main__":
    unittest.main()
