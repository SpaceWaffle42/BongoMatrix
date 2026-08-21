import unittest
from unittest.mock import Mock, patch

from modules import clock_lava


class ClockLavaOptionsTests(unittest.TestCase):
    def test_pick_palette_uses_only_checked_predefined_palettes(self):
        matrix = Mock()
        matrix.colour.side_effect = lambda name: name
        config = {
            "enabled_palettes": ["ocean_wave"],
            "custom_enabled": False,
            "custom_colours": ["#000000", "#ffffff", "#00ffff"],
        }

        palette = clock_lava.pick_palette(matrix, config)

        self.assertEqual(palette, ("black", "cyan", "blue"))

    def test_pick_palette_can_use_custom_hex_colours(self):
        matrix = Mock()
        matrix.colour_rgb.side_effect = lambda value: value.lower()
        config = {
            "enabled_palettes": [],
            "custom_enabled": True,
            "custom_colours": ["#102030", "#A0B0C0", "#ffffff"],
        }

        palette = clock_lava.pick_palette(matrix, config)

        self.assertEqual(palette, ("#102030", "#a0b0c0", "#ffffff"))

    def test_pick_palette_uses_only_enabled_saved_custom_palettes(self):
        matrix = Mock()
        matrix.colour_rgb.side_effect = lambda value: value.lower()
        config = {
            "enabled_palettes": [],
            "custom_enabled": False,
            "custom_colours": ["#000000", "#ffffff", "#00ffff"],
            "custom_palettes": [
                {
                    "id": "palette_disabled", "name": "Disabled",
                    "colours": ["#111111", "#222222", "#333333"], "enabled": False,
                },
                {
                    "id": "palette_saved", "name": "Saved",
                    "colours": ["#102030", "#A0B0C0", "#ffffff"], "enabled": True,
                },
            ],
        }

        palette = clock_lava.pick_palette(matrix, config)

        self.assertEqual(palette, ("#102030", "#a0b0c0", "#ffffff"))

    def test_zero_selected_palettes_runs_a_neutral_clock_without_random_choice(self):
        matrix = Mock()
        canvas = Mock()
        temporary = Mock()
        matrix.matrix.CreateFrameCanvas.side_effect = [canvas, temporary]
        matrix.font.side_effect = lambda name: name
        matrix.colour.side_effect = lambda name: name
        matrix.draw_text.return_value = 20
        matrix.swap.return_value = canvas
        stop = Mock()
        stop.is_set.return_value = False
        stop.wait.return_value = True
        settings = {
            "enabled_palettes": [], "custom_enabled": False,
            "custom_colours": ["#000000", "#ffffff", "#00ffff"],
            "custom_palettes": [], "show_time": True, "show_date": True,
        }

        with patch("modules.clock_lava.random.choice") as choose, \
             patch("modules.clock_lava.draw_lava") as draw_lava, \
             patch("modules.clock_lava.image_to_canvas") as image_to_canvas:
            clock_lava.clock(
                duration=1, matrix=matrix, stop_event=stop, lava_config=settings,
            )

        choose.assert_not_called()
        draw_lava.assert_not_called()
        image_to_canvas.assert_not_called()
        self.assertEqual(matrix.draw_text_with_border.call_count, 2)
        matrix.swap.assert_called_once_with(canvas)

    def test_lava_blob_and_clock_layout_settings_drive_runtime_helpers(self):
        settings = {
            "blob_count": 6, "blob_min_radius": 3, "blob_max_radius": 12,
            "blob_speed": 0.4,
        }
        with patch("modules.clock_lava.random.uniform", side_effect=lambda low, high: high):
            generated = clock_lava.make_blobs(settings)

        self.assertEqual(len(generated), 6)
        self.assertTrue(all(blob["r"] == 12 for blob in generated))
        self.assertTrue(all(blob["vx"] == 0.4 and blob["vy"] == 0.4 for blob in generated))
        self.assertEqual(clock_lava.aligned_x("left", 20), 2)
        self.assertEqual(clock_lava.aligned_x("center", 20), 22)
        self.assertEqual(clock_lava.aligned_x("right", 20), 42)

    def test_clock_applies_blob_shape_and_text_customization(self):
        matrix = Mock()
        canvas = Mock()
        temporary = Mock()
        matrix.matrix.CreateFrameCanvas.side_effect = [canvas, temporary]
        matrix.font.side_effect = lambda name: name
        matrix.colour_rgb.side_effect = lambda value: value
        matrix.swap.return_value = canvas
        stop = Mock()
        stop.is_set.return_value = False
        stop.wait.return_value = True
        settings = {
            "enabled_palettes": ["fire_burst"], "custom_enabled": False,
            "custom_colours": ["#000000", "#ffffff", "#00ffff"],
            "blob_count": 5, "blob_min_radius": 4, "blob_max_radius": 11,
            "blob_speed": 0.3, "field_threshold": 1.7,
            "blur_radius": 1, "edge_width": 2,
            "show_time": False, "show_date": True,
            "time_font": "7x13B", "date_font": "6x10",
            "time_align": "left", "date_align": "right",
            "time_y": 14, "date_y": 29,
            "text_colour": "#eeeeee", "border_colour": "#111111",
        }
        image = Mock()
        image.filter.return_value = image
        now = Mock()
        now.strftime.side_effect = ["12:34", "31 Jul"]
        generated = [{"x": 1, "y": 2, "vx": .1, "vy": .1, "r": 5}]

        with patch("modules.clock_lava.time.time", side_effect=[0, 0]), \
             patch("modules.clock_lava.datetime.datetime") as date_time, \
             patch("modules.clock_lava.Image.new", return_value=image), \
             patch("modules.clock_lava.ImageDraw.Draw", return_value=Mock()) as image_draw, \
             patch("modules.clock_lava.make_blobs", return_value=generated), \
             patch("modules.clock_lava.update_blobs") as update_blobs, \
             patch("modules.clock_lava.draw_lava") as draw_lava, \
             patch("modules.clock_lava.image_to_canvas"), \
             patch("modules.clock_lava.measure_text", side_effect=[20, 18]):
            date_time.now.return_value = now
            clock_lava.clock(duration=1, matrix=matrix, stop_event=stop, lava_config=settings)

        matrix.font.assert_any_call("7x13B")
        matrix.font.assert_any_call("6x10")
        update_blobs.assert_called_once_with(generated)
        self.assertEqual(draw_lava.call_args.kwargs["threshold"], 1.7)
        self.assertEqual(draw_lava.call_args.kwargs["edge_width"], 2)
        matrix.draw_text_with_border.assert_called_once_with(
            "6x10", 44, 29, "#eeeeee", "#111111", "31 Jul", canvas=canvas
        )

    def test_run_passes_saved_lava_options_to_clock(self):
        matrix = Mock()
        options = {
            "enabled_palettes": ["fire_burst"],
            "custom_enabled": False,
            "custom_colours": ["#000000", "#ffffff", "#00ffff"],
        }
        with patch("modules.clock_lava.clock", return_value=True) as clock:
            clock_lava.run(matrix, {"lava": options}, duration=12, stop_event="stop")

        clock.assert_called_once_with(
            duration=12,
            matrix=matrix,
            stop_event="stop",
            lava_config=options,
        )


if __name__ == "__main__":
    unittest.main()
