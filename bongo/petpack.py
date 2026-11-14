"""Pet packs: turning Last Tick's pixel-art packs (bought by the Matrix's owner) into the atlases the
Pet screens use. Used two ways:

* ``tools/build_pet_atlas.py`` builds ``resources/pet`` for the private build (with Billy's
  remastered room);
* "Import your pet pack" on the dashboard: the owner uploads the zip they bought, and the Matrix
  builds the art into ``data/pet`` itself (the public build ships no pet art: the licence doesn't
  allow passing it on).

Every coat in the kittens pack is one 352x1696 sprite sheet with the same layout (a 32x32 cell per
frame), and the coats share their shapes almost exactly. So:

* which cells each animation plays, and for how long, is read once from Cat 10's GIFs (they are
  cut from its sheet; each GIF frame is found on the sheet);
* each frame is cut to the box that holds that cell in every coat, so one set of rectangles
  (``cat.json``) fits all of them;
* coats that differ only in colour share one atlas plus a colour swap.

Atlases are lossless WebP stored as base64 text.
"""
from __future__ import annotations

import base64
import io
import json
import shutil
import tempfile
import zipfile
from pathlib import Path

import numpy as np
from PIL import Image, ImageSequence


class PackError(ValueError):
    """The upload isn't a pack we can read (said in words the dashboard can show)."""


ANCHOR = (16, 25)  # the cat's feet inside a 32 px cell
DEFAULT = "10"     # Mochi's coat, and the one whose GIFs give the timings
# the animations the Pet module plays (bongo/modules/pet/scenes.py, plus Wander's walks); the
# others in the pack are left out to keep the atlases small. Add a tag here when a scene uses it.
USED = {
    "eat_right", "hiss_r", "meow_lie", "meow_sit", "meow_sit2", "meow_stand", "on_hind_legs",
    "paw_left", "paw_right", "scratch_r", "sleep1_l", "sleep2_l", "sleep3_l", "walk_left",
    "walk_left_d", "walk_right", "wash_lie", "wash_sit", "yawn_lie", "yawn_sit", "yawn_stand",
}
FACE = (0, 0)  # the cell the dashboard's coat picker crops a face from (sitting, facing out)
# what the dashboard calls each coat (the picker shows its face too, so these are just a hint)
NAMES = {
    "10": "Calico", "1": "Grey", "1.1": "Charcoal", "1.2": "Smoke", "1.3": "Mahogany", "1.4": "Chocolate",
    "1.5": "Ginger", "1.6": "Rust", "1.7": "Khaki", "1.8": "Cream", "1.9": "Pink", "1.10": "Lilac",
    "1.11": "Caramel", "1.12": "Peach", "1.13": "Plum", "2": "Silver tabby and white", "2.1": "Lilac tabby",
    "2.2": "Taupe tabby", "2.3": "Rose tabby", "2.4": "Brown tabby", "2.5": "Ginger tabby", "2.6": "Copper tabby",
    "2.7": "Sage tabby", "2.8": "Fawn tabby", "2.9": "Silver tabby", "2.10": "Dark tabby", "2.11": "Bronze tabby",
    "2.12": "Blush tabby", "3": "Smoky", "3.1": "Coral", "4": "Amber", "4.1": "Brown and white",
    "5": "Dark brown and white", "5.1": "Sand", "6": "Tuxedo", "6.1": "Tabby and white", "7": "Tortie and white",
    "7.1": "Grey and white", "8": "Stone", "9": "Blue and white", "11": "Light calico", "12": "Tortoiseshell",
    "13": "Piebald",
}


def tag(path: Path) -> str:
    """sleep3(l).gif -> sleep3_l, paw_att_right.gif -> paw_right."""
    return path.stem.replace("(l)", "_l").replace("(r)", "_r").replace("paw_att_", "paw_")


def coat_id(sheet: Path) -> str:
    """cat 1.3.png -> 1.3, cat 8 32x32 animation.png -> 8, cat 2 16x16 animation.png -> 2."""
    return sheet.stem.split(" ")[1]


def cell(sheet: np.ndarray, c: tuple[int, int]) -> np.ndarray:
    x, y = c
    return sheet[y * 32:(y + 1) * 32, x * 32:(x + 1) * 32]


def timings(folder: Path, sheet: np.ndarray) -> dict:
    """tag -> [(cell, ms)], from the default coat's GIFs matched against its sheet."""
    where = {}
    for y in range(sheet.shape[0] // 32):
        for x in range(sheet.shape[1] // 32):
            where.setdefault(cell(sheet, (x, y)).tobytes(), (x, y))
    out = {}
    for gif in sorted(folder.glob("*.gif")):
        if tag(gif) not in USED:
            continue
        frames = []
        for frame in ImageSequence.Iterator(Image.open(gif)):
            c = where.get(np.asarray(frame.convert("RGBA")).tobytes())
            if c is None:
                raise PackError(f"{gif.name}: a frame isn't on the sheet")
            frames.append((c, int(frame.info.get("duration", 100))))
        out[tag(gif)] = frames
    missing = USED - set(out)
    if missing:
        raise PackError(f"no GIF for {', '.join(sorted(missing))}")
    return out


def swap_from(base: np.ndarray, coat: np.ndarray, cells) -> dict | None:
    """The colour swap turning ``base`` into ``coat`` over ``cells``, or None if it isn't one."""
    a = np.concatenate([cell(base, c).reshape(-1, 4) for c in cells]).view("u4").ravel()
    b = np.concatenate([cell(coat, c).reshape(-1, 4) for c in cells]).view("u4").ravel()
    swap = {}
    for x, y in zip(*np.unique(np.stack([a, b], 1), axis=0).T):
        if swap.setdefault(int(x), int(y)) != int(y):
            return None
    return swap


def hexa(v: int) -> str:
    return np.array([v], "u4").view("u1").tobytes().hex()  # RGBA bytes


def pack(out, kind, sheets, anims, default, names, face, room=None, ball=None, airborne=(), room_from=None, tuning=None,
         outfits=None, outfit_side=None):
    """Write ``<kind>.json`` and the atlases for one animal.

    sheets: coat id -> sprite sheet (RGBA array); anims: tag -> [(cell, ms)]; face: (cell, w, h)
    cut from the top-left of that cell's box; room/ball: the scene art (or ``room_from`` another
    kind); airborne: animations whose frames keep their own height (hops), not set on the floor."""
    out.mkdir(parents=True, exist_ok=True)
    cells = sorted({c for frames in anims.values() for c, _ in frames} | {face[0]})

    # one box per cell that holds it in every coat
    boxes = {}
    for c in cells:
        alpha = np.any([cell(s, c)[..., 3] > 0 for s in sheets.values()], axis=0)
        ys, xs = np.nonzero(alpha)
        boxes[c] = (int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1)

    # group coats into drawings: a coat is a recolouring of the first drawing it matches
    order = sorted(sheets, key=lambda k: [int(p) for p in k.split(".")])
    order.remove(default)
    order.insert(0, default)
    drawings, coats = [], []
    for cid in order:
        for d in drawings:
            swap = swap_from(sheets[d], sheets[cid], cells)
            if swap is not None:
                changed = {hexa(k): hexa(v) for k, v in swap.items() if k != v}
                coats.append({"id": cid, "name": names.get(cid, f"Coat {cid}"), "drawing": d,
                              **({"swap": changed} if changed else {})})
                break
        else:
            drawings.append(cid)
            coats.append({"id": cid, "name": names.get(cid, f"Coat {cid}"), "drawing": cid})

    # shelf packing, tallest first, 256 px wide; room and ball first so they sit in the top-left
    pieces = ([("room", room.size), ("ball", ball.size)] if room is not None else []) + \
        sorted(((c, (b[2] - b[0], b[3] - b[1])) for c, b in boxes.items()), key=lambda p: -p[1][1])
    width, x, y, shelf, spot = 256, 0, 0, 0, {}
    for key, (w, h) in pieces:
        if x + w > width:
            x, y, shelf = 0, y + shelf, 0
        spot[key] = (x, y)
        x, shelf = x + w, max(shelf, h)
    size = (width, y + shelf)

    index = {"anchor": list(ANCHOR)}
    if room is not None:
        index.update(room=[*spot["room"], *room.size], ball=[*spot["ball"], *ball.size])
    else:
        index["room_from"] = room_from
    index.update(animations={}, default=default, coats=coats)
    if airborne:
        index["airborne"] = sorted(airborne)
    if tuning:
        index["tuning"] = tuning
    for name, frames in sorted(anims.items()):
        index["animations"][name] = [
            [*spot[c], boxes[c][2] - boxes[c][0], boxes[c][3] - boxes[c][1],
             boxes[c][0] - ANCHOR[0], boxes[c][1] - ANCHOR[1], ms] for c, ms in frames]
    fx, fy = spot[face[0]]
    index["face"] = [fx, fy, face[1], face[2]]
    if outfits:
        index["cells"] = {name: [f"{c[0]},{c[1]}" for c, _ in frames] for name, frames in sorted(anims.items())}
        asleep = {c for tag in ASLEEP for c, _ in anims.get(tag, ())}
        side = {c for tag in SIDE_ON for c, _ in anims.get(tag, ())} - asleep
        index["outfits"] = pack_outfits(out, kind, outfits, cells, asleep, side, outfit_side)
        index["outfit_under"] = [o for o in OUTFIT_UNDER if o in outfits]

    out.mkdir(parents=True, exist_ok=True)
    for old in out.glob(f"{kind}-*.webp.b64"):
        if not old.name.endswith("-outfits.webp.b64"):
            old.unlink()
    total = 0
    for d in drawings:
        atlas = Image.new("RGBA", size)
        if d == default and room is not None:
            atlas.paste(room, spot["room"])
            atlas.paste(ball, spot["ball"])
        for c, b in boxes.items():
            atlas.paste(Image.fromarray(np.ascontiguousarray(cell(sheets[d], c))).crop(b), spot[c])
        # lossless WebP (exact pixels), stored as base64 text so the repo holds no binary files
        # and the atlases can be committed through GitLab's API
        buf = io.BytesIO()
        atlas.save(buf, "WEBP", lossless=True, quality=100, method=6)
        name = f"{kind}.webp.b64" if d == default else f"{kind}-{d}.webp.b64"
        (out / name).write_text(base64.b64encode(buf.getvalue()).decode())
        total += len(buf.getvalue())
    (out / f"{kind}.json").write_text(json.dumps(index, separators=(",", ":")) + "\n")
    return f"{kind}: {len(coats)} coats ({len(drawings)} drawings), {len(anims)} animations, {total // 1024} KB"


# Outfits: Last Tick's accessory sheets share the cat sheet's layout, one accessory per cell, drawn
# to sit on the cat in that cell. Worn over every coat (bunnies have none).
OUTFITS = {
    "santa_hat": "winter_accessories/animated Santa hat 1.png",
    "antlers": "winter_accessories/animated reindeer antler headband red.png",
    "red_bow": "14_feb/animated red bow.png",
    "heart_glasses": "14_feb/animated red glasses hearts.png",
    "cupid": "14_feb/animated cupid.png",
    "collar_blue": "collars_animated/animated collar blue.png",
    "collar_green": "collars_animated/animated collar green.png",
    "collar_purple": "collars_animated/animated collar purple.png",
    "collar_violet": "collars_animated/animated collar violet.png",
}


# taken off to sleep (the glasses split into two blobs on a curled-up cat; the halo turns to a smudge)
OUTFIT_OFF_ASLEEP = {"heart_glasses", "cupid"}
OUTFIT_UNDER = ["cupid"]  # wings go behind her, so from the side they're wings, not a second head
ASLEEP = ("sleep1_l", "sleep2_l", "sleep3_l")
# side on, cupid's wing is a white blob on her back that reads as a second head: just the halo there
SIDE_ON = ("walk_right", "walk_left", "walk_left_d", "eat_right", "hiss_r", "paw_left", "paw_right")  # scratch_r faces front
HALO_ONLY = {"cupid": "14_feb/animated nimbus.png"}


def pack_outfits(out, kind, sheets, cells, asleep=(), side=(), side_sheets=None):
    """One atlas (``<kind>-outfits.webp.b64``) of every accessory piece the used cells need;
    returns {outfit: {"c,r": [x, y, w, h, dx, dy]}} (offsets from the feet, like the frames)."""
    unique, uses = {}, []  # identical pieces (held poses, shared shapes) are stored once
    for name, sheet in sheets.items():
        for c in cells:
            if name in OUTFIT_OFF_ASLEEP and c in asleep:
                continue
            img = cell((side_sheets or {}).get(name, sheet) if c in side else sheet, c)
            ys, xs = np.nonzero(img[..., 3] > 0)
            if not len(xs):
                continue
            x0, y0, x1, y1 = int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1
            crop = np.ascontiguousarray(img[y0:y1, x0:x1])
            key = (crop.shape, crop.tobytes())
            unique.setdefault(key, crop)
            uses.append((name, c, key, x0 - ANCHOR[0], y0 - ANCHOR[1]))
    width, x, y, shelf, spot = 256, 0, 0, 0, {}
    for key, crop in sorted(unique.items(), key=lambda kv: -kv[1].shape[0]):
        h, w = crop.shape[:2]
        if x + w > width:
            x, y, shelf = 0, y + shelf, 0
        spot[key] = (x, y)
        x, shelf = x + w, max(shelf, h)
    atlas = Image.new("RGBA", (width, y + shelf))
    for key, crop in unique.items():
        atlas.paste(Image.fromarray(crop, "RGBA"), spot[key])
    buf = io.BytesIO()
    atlas.save(buf, "WEBP", lossless=True, quality=100, method=6)
    (out / f"{kind}-outfits.webp.b64").write_text(base64.b64encode(buf.getvalue()).decode())
    found = {name: {} for name in sheets}
    for name, c, key, dx, dy in uses:
        h, w = unique[key].shape[:2]
        found[name][f"{c[0]},{c[1]}"] = [*spot[key], w, h, dx, dy]
    return found


# Toys for the scenes (Last Tick props, 16 x 16 each): name -> file in extras/props/toys
TOYS = {"mouse": "mouse_1.png", "carrot_1": "carrot_1.png", "carrot_2": "carrot_2.png",
        "carrot_3": "carrot_3.png", "carrot_4": "carrot_4.png",
        "bee": "bee_without_wings_1.png", "bee_wings": "bee_wings_1.png"}


def toys(folder: Path, out: Path) -> str:
    """``toys.json`` + ``toys.webp.b64``: each toy trimmed, placed by the middle of its bottom edge.
    (Bee and wings share one box, so the wings stay on the bee.)"""
    imgs = {name: Image.open(folder / file).convert("RGBA") for name, file in TOYS.items()}
    boxes = {name: img.getbbox() for name, img in imgs.items()}
    bee = boxes["bee"]
    union = (min(bee[0], boxes["bee_wings"][0]), min(bee[1], boxes["bee_wings"][1]),
             max(bee[2], boxes["bee_wings"][2]), max(bee[3], boxes["bee_wings"][3]))
    boxes["bee"] = boxes["bee_wings"] = union
    x, index, atlas = 0, {}, Image.new("RGBA", (sum(b[2] - b[0] for b in boxes.values()), 16))
    for name, img in imgs.items():
        b = boxes[name]
        atlas.paste(img.crop(b), (x, 0))
        # rect in the atlas, then the offset of its top-left from the toy's foot point
        index[name] = [x, 0, b[2] - b[0], b[3] - b[1], -((b[2] - b[0]) // 2), -(b[3] - b[1])]
        x += b[2] - b[0]
    buf = io.BytesIO()
    atlas.save(buf, "WEBP", lossless=True, quality=100, method=6)
    (out / "toys.webp.b64").write_text(base64.b64encode(buf.getvalue()).decode())
    (out / "toys.json").write_text(json.dumps(index, separators=(",", ":")) + "\n")
    return f"toys: {len(index)}"


# Furniture for the bigger pet places (bongo/modules/pet/rooms.py): name -> where it is in the
# kittens pack's extras/props. ("part", file, k): the k-th drawing in the file, top to bottom (most
# files hold a front view with a second view under it). ("cell", file, size, column, row): one
# cell of a sheet. ("bowl", column): a bowl from the bowl sheet, and the same bowl with food in it.
_HS = "house and scratching post/"
PROPS = {
    "house_1": ("part", _HS + "house_1.png", 0),
    "house_4": ("part", _HS + "house_4.png", 0), "carrier_1": ("part", _HS + "carrier_1.png", 0),
    "post_8": ("part", _HS + "scratching_post_8.png", 0),
    "tree_3": ("part", _HS + "scratching_post_3.png", 0), "tree_5": ("part", _HS + "scratching_post_5.png", 0),
    "food_2": ("part", "food/food_2.png", 0), "food_5": ("part", "food/food_5.png", 0),
        "bed_plain": ("cell", "bed/bed.png", 32, 1, 0), "bed_basket": ("cell", "bed/bed.png", 32, 0, 1),
    "bed_round": ("cell", "bed/bed.png", 32, 2, 0), "bed_blue": ("cell", "bed/bed.png", 32, 1, 1),
    "bowl_a": ("bowl", 0), "bowl_b": ("bowl", 6), "bowl_c": ("bowl", 9), "bowl_d": ("bowl", 13),
}
# Plants for them, from Last Tick's nature pack (png/<name>.png). They are drawn for a bigger
# world than the kittens: used at half size.
PLANTS = ("shrubs_10", "shrubs_11", "shrubs_12", "shrubs_20", "flowers_8", "flowers_12",
          "grasses_2", "grasses_3", "grasses_4", "bonsai_3", "palms_30", "palms_33", "palms_36",
          "palms_40", "palms_44", "cacti_13", "cacti_20", "cacti_24", "cacti_27", "cacti_31")


def drawings(img: Image.Image) -> list:
    """The separate drawings stacked in one file, top to bottom (split where rows are empty), trimmed."""
    alpha, out, top = np.asarray(img.getchannel("A")).any(axis=1), [], None
    for y in range(img.height + 1):
        inked = y < img.height and alpha[y]
        if inked and top is None:
            top = y
        elif not inked and top is not None:
            piece = img.crop((0, top, img.width, y))
            out.append(piece.crop(piece.getbbox()))
            top = None
    return out


def half(img: Image.Image) -> Image.Image:
    """Half size and still crisp: each 2 x 2 block becomes its most common solid colour (the lighter
    one on a tie), or stays clear if most of it is clear."""
    w, h = img.width // 2, img.height // 2
    out, src = Image.new("RGBA", (w, h)), img.load()
    dst = out.load()
    for y in range(h):
        for x in range(w):
            solid = [p for p in (src[x * 2 + a, y * 2 + b] for a in (0, 1) for b in (0, 1)) if p[3] > 127]
            if len(solid) >= 2:
                dst[x, y] = max(sorted(set(solid)), key=lambda p: (solid.count(p), sum(p[:3])))
    return out


def _sheet(out: Path, name: str, imgs: dict) -> None:
    """``<name>.json`` + ``<name>.webp.b64``: the drawings side by side, each with its rect."""
    atlas = Image.new("RGBA", (sum(i.width for i in imgs.values()), max(i.height for i in imgs.values())))
    x, index = 0, {}
    for key, img in imgs.items():
        atlas.paste(img, (x, 0))
        index[key] = [x, 0, img.width, img.height]
        x += img.width
    buf = io.BytesIO()
    atlas.save(buf, "WEBP", lossless=True, quality=100, method=6)
    out.mkdir(parents=True, exist_ok=True)
    (out / f"{name}.webp.b64").write_text(base64.b64encode(buf.getvalue()).decode())
    (out / f"{name}.json").write_text(json.dumps(index, separators=(",", ":")) + "\n")


def props(folder: Path, out: Path) -> str:
    """The kittens pack's ``extras/props`` folder -> ``props.json`` + ``props.webp.b64``."""
    imgs = {}
    for name, (kind, *spec) in PROPS.items():
        if kind == "part":
            imgs[name] = drawings(Image.open(folder / spec[0]).convert("RGBA"))[spec[1]]
        elif kind == "cell":
            file, size, i, j = spec
            cell = Image.open(folder / file).convert("RGBA").crop((i * size, j * size, (i + 1) * size, (j + 1) * size))
            imgs[name] = cell.crop(cell.getbbox())
        else:   # a bowl, empty and full: both cut to the empty bowl's box so they swap in place
            bowl = Image.open(folder / "bowl" / "bowl_1.png").convert("RGBA").crop((spec[0] * 16, 0, spec[0] * 16 + 16, 16))
            food = Image.open(folder / "bowl" / "food_progress_in_bowl_1.png").convert("RGBA").crop((16, 0, 32, 16))
            full = bowl.copy()
            full.alpha_composite(food)
            box = bowl.getbbox()
            imgs[name], imgs[name + "_full"] = bowl.crop(box), full.crop(box)
    _sheet(out, "props", imgs)
    return f"furniture for the bigger places: {len(imgs)} pieces"


def plants(folder: Path, out: Path) -> str:
    """The nature pack's ``png`` folder -> ``plants.json`` + ``plants.webp.b64`` (half size)."""
    imgs = {}
    for name in PLANTS:
        small = half(Image.open(folder / f"{name}.png").convert("RGBA"))
        imgs[name] = small.crop(small.getbbox())
    _sheet(out, "plants", imgs)
    return f"plants for the garden places: {len(imgs)}"


def cats(sheets_folder: Path, out: Path, remastered: Path | None = None) -> list[str]:
    """The kittens pack's ``cats_animations`` folder -> the cat art in ``out``. The room is
    ``remastered/room_day.png`` when given (Billy's remaster), else drawn here from the pack's
    props (``room.py``)."""
    sheets = {coat_id(p): np.asarray(Image.open(p).convert("RGBA")) for p in sorted(sheets_folder.rglob("*.png"))
              if len(p.stem.split(" ")) > 1 and Image.open(p).size == (352, 1696)}
    if DEFAULT not in sheets:
        raise PackError(f"no Cat {DEFAULT} sheet in {sheets_folder.name}")
    anims = timings(sheets_folder / f"Cat {DEFAULT}", sheets[DEFAULT])
    extras = sheets_folder.parent / "extras"
    if remastered is not None:
        room = Image.open(remastered / "room_day.png").convert("RGBA")
        ball = Image.open(remastered / "ball.png").convert("RGBA")
    else:
        from bongo.room import draw_room
        room, ball = draw_room(extras / "props")
    acc = extras / "accessories"
    have = {name: path for name, path in OUTFITS.items() if (acc / path).exists()}
    outfits = {name: np.asarray(Image.open(acc / path).convert("RGBA")) for name, path in have.items()}
    side = {name: np.asarray(Image.open(acc / path).convert("RGBA")) for name, path in HALO_ONLY.items()
            if (acc / path).exists()}
    done = [pack(out, "cat", sheets, anims, DEFAULT, NAMES, (FACE, 14, 12), room, ball,
                 outfits=outfits or None, outfit_side=side)]
    if (extras / "props" / "toys").is_dir():
        done.append(toys(extras / "props" / "toys", out))
    return done


# -- the bunnies (Last Tick "Bunny 32x32"): no GIFs, so the rows of the sheet are named here.
# Each row is one animation, frames left to right; directions read from which way the ears lean.
# They play the cat's scenes under the cat's tag names, so every Pet screen works for a bunny.
def row(r, n, ms, start=0):
    return [((c, r), ms) for c in range(start, start + n)]


BUNNY_DEFAULT = "1.4"
BUNNY = {
    "walk_right": row(4, 6, 85), "walk_left": row(5, 6, 85),
    "walk_left_d": row(6, 6, 95), "walk_right_d": row(7, 6, 95),
    "walk_right_up": row(8, 6, 95), "walk_left_up": row(9, 6, 95),
    "walk_down": row(2, 6, 95), "walk_up": row(3, 6, 95),
    "meow_sit": [((0, 0), 400)] + row(25, 4, 160)[1:],           # sitting, nose twitching
    "meow_sit2": row(27, 5, 160), "meow_stand": row(28, 6, 150),
    "meow_lie": [((3, 0), 400), ((3, 0), 400)],                   # sitting up, side on
    "on_hind_legs": row(25, 3, 220),                                # sitting up tall
    "yawn_sit": row(24, 9, 140), "yawn_stand": row(18, 9, 120), "yawn_lie": row(18, 7, 120, start=2),
    "wash_sit": row(32, 11, 120), "wash_lie": row(33, 11, 120),
    "scratch_r": row(26, 8, 130),
    "eat_right": row(19, 9, 120),                                   # side on, nose down in the bowl
    "paw_right": row(21, 5, 110), "paw_left": row(20, 5, 110),      # nudging the ball with her nose
    "hiss_r": row(30, 5, 160),                                      # a snub: back turned, ear flicked
    "sleep1_l": row(10, 2, 500), "sleep2_l": row(12, 2, 500), "sleep3_l": row(14, 2, 500),
}
BUNNY_AIRBORNE = {"walk_right", "walk_left", "walk_left_d", "walk_right_d", "walk_right_up", "walk_left_up",
                  "walk_down", "walk_up"}
BUNNY_NAMES = {
    "1.4": "Chocolate", "1.1": "Grey", "1.2": "Charcoal", "1.3": "Dusk", "1.5": "Fawn", "1.6": "Golden",
    "1.7": "Rose", "1.8": "Blush", "1.9": "Cream", "1.10": "Lilac", "1.11": "White", "2.1": "Slate",
    "2.2": "Black", "2.3": "Smoke", "2.4": "Chestnut", "2.5": "Sand", "2.6": "Honey", "2.7": "Rosewood",
    "2.8": "Peach", "2.9": "Oatmeal", "2.10": "Lavender", "2.11": "Silver", "3.1": "Slate patch",
    "3.2": "Otter", "3.3": "Patched brown", "3.4": "Wheaten", "3.5": "Brown and cream",
}


# she is smaller than the cat: nearer the bowl, closer to the ball, and the ball rests out of her way
BUNNY_TUNING = {"bowl": [-5, -2], "ball": [58, 28], "reach": 7, "zoomies": [1.7, 5.3], "toy": "carrot",
                "swat": "yawn_sit", "swat_frames": [0, 1, 5, 6, 7, 8], "bee_lift": 3}


def bunnies(folder: Path, out: Path) -> list[str]:
    """The bunny pack's ``animation`` folder -> the bunny art in ``out`` (she uses the cat's room)."""
    sheets = {p.stem.split(" ")[1]: np.asarray(Image.open(p).convert("RGBA")) for p in sorted(folder.rglob("Bunny *.png"))
              if len(p.stem.split(" ")) > 1}
    if BUNNY_DEFAULT not in sheets:
        raise PackError(f"no Bunny {BUNNY_DEFAULT} sheet in {folder.name}")
    return [pack(out, "bunny", sheets, BUNNY, BUNNY_DEFAULT, BUNNY_NAMES, ((0, 0), 13, 14), airborne=BUNNY_AIRBORNE,
                 room_from="cat", tuning=BUNNY_TUNING)]


MAX_ZIP = 300 * 1024 * 1024        # bytes an uploaded pack may unpack to


def find(root: Path) -> dict:
    """Where the packs are inside an unpacked upload: {"cat": cats_animations folder, "bunny": the
    bunny animation folder}, for whichever are there."""
    found = {}
    for d in sorted(p for p in root.rglob("*") if p.is_dir()):
        if d.name == "cats_animations" and (d / f"Cat {DEFAULT}").is_dir() and "cat" not in found:
            found["cat"] = d
        elif d.name == "animation" and any(d.glob("Bunny */Bunny *.png")) and "bunny" not in found:
            found["bunny"] = d
        elif d.name == "props" and (d / "bed" / "bed.png").is_file() and "props" not in found:
            found["props"] = d
        elif d.name == "png" and (d / f"{PLANTS[0]}.png").is_file() and "plants" not in found:
            found["plants"] = d
    return found


def unzip(upload: Path, into: Path) -> None:
    """Unpack ``upload`` (a zip, or a zip of zips) into ``into``, refusing anything that would land
    outside it or unpack too big."""
    with zipfile.ZipFile(upload) as z:
        total = sum(i.file_size for i in z.infolist())
        if total > MAX_ZIP:
            raise PackError(f"that zip unpacks to {total // 2**20} MB; packs are well under {MAX_ZIP // 2**20} MB")
        for info in z.infolist():
            target = (into / info.filename).resolve()
            if not str(target).startswith(str(into.resolve())):
                raise PackError(f"{info.filename!r} would unpack outside the folder")
        z.extractall(into)
    for inner in list(into.rglob("*.zip")):                 # itch.io downloads are sometimes zips in a zip
        unzip(inner, inner.with_suffix(""))
        inner.unlink()


def import_zip(upload: Path, out: Path) -> list[str]:
    """Build the pet art from an uploaded pack zip into ``out``. Returns what was made, one line
    each. Packs already imported stay unless this upload has a newer copy of the same animal."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        try:
            unzip(upload, root)
        except zipfile.BadZipFile:
            raise PackError("that file isn't a zip: upload the zip you downloaded from itch.io") from None
        packs = find(root)
        if not packs:
            raise PackError("no Last Tick kittens, bunny or nature pack found in that zip (looking for a "
                            "'cats_animations' folder with Cat 10, a bunny 'animation' folder, or the nature "
                            "pack's 'png' folder)")
        if "bunny" in packs and "cat" not in packs and not (out / "cat.json").exists():
            raise PackError("the bunny lives in the kittens pack's room: import the kittens pack first (or both together)")
        build = Path(tmp) / "build"
        made = []
        if "cat" in packs:
            made += cats(packs["cat"], build)
        if "bunny" in packs:
            made += bunnies(packs["bunny"], build)
        for kind, make in (("props", props), ("plants", plants)):   # for the bigger places; a pack without
            if kind in packs:                                       # every piece just goes without them
                try:
                    made.append(make(packs[kind], build))
                except (OSError, IndexError):
                    pass
        out.mkdir(parents=True, exist_ok=True)
        for f in build.iterdir():                            # only now, with everything built, replace the old art
            shutil.copy2(f, out / f.name)
        return made
