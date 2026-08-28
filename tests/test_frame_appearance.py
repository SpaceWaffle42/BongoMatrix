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
            draw_frame_border(self.matrix(), "canvas", "double", "#ffffff")


if __name__ == "__main__":
    unittest.main()
