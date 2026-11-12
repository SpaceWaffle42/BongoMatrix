"""The Pet room for an imported pack: wooden floorboards drawn here, the pack's own bed and ball on
top, and a food bowl drawn to sit where the Pet scenes expect it (the scenes walk her to the bed
at the left and the bowl at the top right; see bongo/modules/pet/scenes.py)."""
from __future__ import annotations

import random
from pathlib import Path

from PIL import Image, ImageDraw

W, H = 64, 32
WOOD = [(226, 179, 137), (232, 185, 141), (218, 171, 130), (229, 182, 139)]
SEAM = (196, 148, 108)
RIM, FOOD, SHADE = (0x75, 0x64, 0x64), (140, 96, 88), (166, 140, 140)


def floor(seed: int = 7) -> Image.Image:
    """Planks 4 rows deep, each with its own tone and staggered joints, a few knots."""
    rng = random.Random(seed)
    img = Image.new("RGBA", (W, H))
    d = ImageDraw.Draw(img)
    for row, y in enumerate(range(0, H, 4)):
        tone = WOOD[row % len(WOOD)]
        d.rectangle([0, y, W - 1, y + 3], fill=tone + (255,))
        d.line([0, y + 3, W - 1, y + 3], fill=SEAM + (255,))
        x = rng.randrange(6, 22)
        while x < W:                                         # board ends
            d.line([x, y, x, y + 2], fill=SEAM + (255,))
            x += rng.randrange(18, 30)
        for _ in range(2):                                   # grain
            gx, gy = rng.randrange(W - 6), y + rng.randrange(0, 3)
            d.line([gx, gy, gx + rng.randrange(3, 7), gy], fill=tuple(c - 10 for c in tone) + (255,))
    return img


def bowl(img: Image.Image) -> None:
    """A food bowl, top right (the empty-bowl view darkens its food: art.empty_bowl)."""
    d = ImageDraw.Draw(img)
    d.ellipse([56, 8, 63, 14], fill=RIM + (255,))
    d.ellipse([57, 9, 62, 13], fill=SHADE + (255,))
    d.rectangle([57, 10, 61, 12], fill=FOOD + (255,))


def draw_room(props: Path) -> tuple[Image.Image, Image.Image]:
    """(room 64x32 RGBA, ball RGBA) from a kittens pack's ``extras/props`` folder."""
    room = floor()
    bed_file = props / "bed" / "bed.png"
    if bed_file.exists():
        bed = Image.open(bed_file).convert("RGBA").crop((0, 0, 32, 32))
        bed = bed.crop(bed.getbbox())
        room.alpha_composite(bed, (16 - bed.width // 2, min(H - bed.height, 20 - bed.height // 2)))
    bowl(room)
    ball_file = props / "toys" / "balls_1.png"
    if ball_file.exists():
        ball = Image.open(ball_file).convert("RGBA").crop((32, 0, 48, 16))
        ball = ball.crop(ball.getbbox())
    else:
        ball = Image.new("RGBA", (7, 7))
        ImageDraw.Draw(ball).ellipse([0, 0, 6, 6], fill=(200, 60, 50, 255), outline=(30, 20, 20, 255))
    return room, ball
