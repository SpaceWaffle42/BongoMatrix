import unittest
from unittest.mock import Mock

from modules.frame_appearance import draw_frame_border


class FrameAppearanceTests(unittest.TestCase):
    def matrix(self):
        matrix = Mock()
        matrix.matrix.width = 64
        matrix.matrix.height = 32
        matrix.colour_rgb.return_value = "rgb"
        return matrix

    def test_solid_and_inset_draw_exact_64_by_32_edges(self):
        expected = {
            "solid": [(0, 0, 63, 0), (63, 0, 63, 31), (63, 31, 0, 31), (0, 31, 0, 0)],
            "inset": [(1, 1, 62, 1), (62, 1, 62, 30), (62, 30, 1, 30), (1, 30, 1, 1)],
        }
        for style, lines in expected.items():
            with self.subTest(style=style):
                matrix = self.matrix()
                draw_frame_border(matrix, "canvas", style, "#A1B2C3")
                matrix.colour_rgb.assert_called_once_with("#A1B2C3")
                self.assertEqual([call.args[:4] for call in matrix.draw_line.call_args_list], lines)

    @staticmethod
    def drawn_pixels(matrix):
        pixels = set()
        for call in matrix.draw_line.call_args_list:
            x1, y1, x2, y2 = call.args[:4]
            if y1 == y2:
                for x in range(min(x1, x2), max(x1, x2) + 1): pixels.add((x, y1))
            elif x1 == x2:
                for y in range(min(y1, y2), max(y1, y2) + 1): pixels.add((x1, y))
            else:
                raise AssertionError("border segment must be axis aligned")
        return pixels

    @staticmethod
    def perimeter(offset):
        left, right, top, bottom = offset, 63 - offset, offset, 31 - offset
        return (
            [(x, top) for x in range(left, right + 1)]
            + [(right, y) for y in range(top + 1, bottom + 1)]
            + [(x, bottom) for x in range(right - 1, left - 1, -1)]
            + [(left, y) for y in range(bottom - 1, top, -1)]
        )

    def test_dotted_dashed_and_double_have_exact_readable_64_by_32_geometry(self):
        cases = {
            "dotted": {point for index, point in enumerate(self.perimeter(0)) if index % 2 == 0},
            "dashed": {point for index, point in enumerate(self.perimeter(0)) if index % 5 < 3},
            "double": set(self.perimeter(0)) | set(self.perimeter(2)),
        }
        for border, expected in cases.items():
            with self.subTest(border=border):
                matrix = self.matrix()
                draw_frame_border(matrix, "canvas", border, "#abcdef")
                self.assertEqual(self.drawn_pixels(matrix), expected)
                self.assertTrue(all(0 <= x < 64 and 0 <= y < 32 for x, y in expected))
                self.assertNotIn((3, 3), expected)

    def test_none_draws_nothing(self):
        matrix = self.matrix()
        draw_frame_border(matrix, "canvas", "none", "#ffffff")
        matrix.colour_rgb.assert_not_called()
        matrix.draw_line.assert_not_called()

    def test_translated_border_clips_like_announcement_board(self):
        matrix = self.matrix()
        draw_frame_border(matrix, "canvas", "solid", "#ffffff", x_offset=48)
        self.assertEqual(
            [call.args[:4] for call in matrix.draw_line.call_args_list],
            [(48, 0, 63, 0), (63, 31, 48, 31), (48, 31, 48, 0)],
        )

    def test_unsupported_style_fails_closed(self):
        with self.assertRaises(NotImplementedError):
            draw_frame_border(self.matrix(), "canvas", "groove", "#ffffff")


if __name__ == "__main__":
    unittest.main()
