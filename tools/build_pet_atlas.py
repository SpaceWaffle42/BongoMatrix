"""Pack the Cat 10 art into resources/pet/: one atlas image (lossless WebP, as base64 text) and a JSON index.

    python tools/build_pet_atlas.py "<Cat 10 folder>"

Reads the reference GIFs (walk_right.gif, sleep3(l).gif ...) at their own frame durations,
the cleaned room (remastered/room_day.png) and the ball (remastered/ball.png). Each animation
frame is trimmed to its pixels and placed by its offset from the cat's feet, so the module can
put the feet anywhere. Re-run after editing the art in Aseprite.
"""
import base64
import io
import json
import sys
from pathlib import Path

from PIL import Image, ImageSequence

ANCHOR = (16, 25)  # the cat's feet inside a 32 px reference frame
# the animations the Pet module plays (bongo/modules/pet/scenes.py, plus Wander's walks); the
# others in the folder are left out to keep the atlas small. Add a tag here when a scene uses it.
USED = {
    "eat_right", "hiss_r", "meow_lie", "meow_sit", "meow_sit2", "meow_stand", "on_hind_legs",
    "paw_left", "paw_right", "scratch_r", "sleep1_l", "sleep2_l", "sleep3_l", "walk_left",
    "walk_left_d", "walk_right", "wash_lie", "wash_sit", "yawn_lie", "yawn_sit", "yawn_stand",
}
OUT = Path(__file__).resolve().parent.parent / "resources" / "pet"


def tag(path: Path) -> str:
    """sleep3(l).gif -> sleep3_l, paw_att_right.gif -> paw_right."""
    return path.stem.replace("(l)", "_l").replace("(r)", "_r").replace("paw_att_", "paw_")


def main(folder: Path) -> None:
    pieces = []  # (kind, name, index, image, extra)
    room = Image.open(folder / "remastered" / "room_day.png").convert("RGB")
    ball = Image.open(folder / "remastered" / "ball.png").convert("RGBA")
    pieces.append(("room", "room", 0, room.convert("RGBA"), {}))
    pieces.append(("ball", "ball", 0, ball, {}))
    for gif in sorted(folder.glob("*.gif")):
        if "Copy" in gif.name or tag(gif) not in USED:
            continue
        for i, frame in enumerate(ImageSequence.Iterator(Image.open(gif))):
            rgba = frame.convert("RGBA")
            box = rgba.getbbox()
            if not box:
                continue
            pieces.append(("frame", tag(gif), i, rgba.crop(box),
                           {"dx": box[0] - ANCHOR[0], "dy": box[1] - ANCHOR[1], "ms": int(frame.info.get("duration", 100))}))
    # identical frames (held poses, mirrored loops) are stored once
    seen, unique = {}, []
    for piece in pieces:
        key = (piece[3].size, piece[3].tobytes())
        if key not in seen:
            seen[key] = piece
            unique.append(piece)
    # shelf packing, tallest first, 256 px wide
    width, x, y, shelf, spot = 256, 0, 0, 0, {}
    for kind, name, i, img, extra in sorted(unique, key=lambda p: -p[3].height):
        if x + img.width > width:
            x, y, shelf = 0, y + shelf, 0
        spot[(img.size, img.tobytes())] = (x, y)
        x, shelf = x + img.width, max(shelf, img.height)
    atlas = Image.new("RGBA", (width, y + shelf))
    index = {"anchor": list(ANCHOR), "room": None, "ball": None, "animations": {}}
    for kind, name, i, img, extra in pieces:
        px, py = spot[(img.size, img.tobytes())]
        atlas.paste(img, (px, py))
        rect = [px, py, img.width, img.height]
        if kind in ("room", "ball"):
            index[kind] = rect
        else:
            index["animations"].setdefault(name, []).append(rect + [extra["dx"], extra["dy"], extra["ms"]])
    OUT.mkdir(parents=True, exist_ok=True)
    # lossless WebP (exact pixels, under half a PNG's size), stored as base64 text so the repo
    # holds no binary files and the atlas can be committed through GitLab's API
    buf = io.BytesIO()
    atlas.save(buf, "WEBP", lossless=True, quality=100, method=6)
    (OUT / "cat.webp.b64").write_text(base64.b64encode(buf.getvalue()).decode())
    (OUT / "cat.json").write_text(json.dumps(index, separators=(",", ":")) + "\n")
    print(f"{len(index['animations'])} animations, {sum(map(len, index['animations'].values()))} frames, "
          f"atlas {atlas.size}, {len(buf.getvalue())} bytes")


if __name__ == "__main__":
    main(Path(sys.argv[1]))
