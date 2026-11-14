"""Splash: a playlist item that plays one of your Splash screens (Settings > Splash).

Put it anywhere in the playlist, as often as you like. Each time it comes round the player
swaps in the next file from the Splash pool (a random order: every file plays once before any
repeats, never the same one twice running), for one GIF pass at the pool's speed, or the still's hold time. The entry's own
seconds aren't used. The style below only draws a stand-in for the dashboard's previews.
"""
from bongo.module import Module, Style


class Screen(Style):
    fps = 5
    defaults = {}

    def render(self, now):
        w, h = self.ctx.width, self.ctx.height
        img = self.ctx.canvas((10, 8, 18))
        font = self.ctx.fonts("5x7")
        text = "SPLASH"
        font.draw(img, (w - font.width(text)) // 2, h // 2 + 3, text, (255, 181, 71))
        tick = int(now.timestamp() * 2) % 4  # a little twinkle so the preview isn't a still
        for k, (x, y) in enumerate(((6, 6), (w - 8, 8), (10, h - 7), (w - 12, h - 6))):
            if k == tick:
                img.putpixel((min(x, w - 1), min(y, h - 1)), (255, 244, 222))
        return img


MODULE = Module(
    id="splash",
    name="Splash",
    description="Plays one of your Splash screens (pick them in Settings > Splash): a different one "
                "each time it comes round, one pass of a GIF.",
    styles={"screen": Screen},
)
