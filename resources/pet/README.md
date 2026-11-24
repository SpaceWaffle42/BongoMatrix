# Pet art

## Our own pets, homes and places (ship in every build)

The built-in animals are our own pixel art in the house style, drawn in the Matrix Animator:
`cat` (Mochi, 14 coats), `bunny` (Clover), `dog` (Biscuit), `jackrussell` (Jack), `spaniel` (Cocker),
`hamster` (Nibbles), `lizard` (Ziggy), `flamingo` (Flo), `monster` (Krax) and `rock` (Pebble). Each
`<kind>.json` holds its frames, coats ("shape.colour" ids: one drawing recoloured, or an extra drawing
`<kind>-<coat>.webp.b64`), its outfit pieces (`<kind>-outfits.webp.b64`, each stamped at its own marker),
its tuning and its own home (day, empty bowl, night base), all in `<kind>.webp.b64`. The eight places
(garden, beach, snow, space, library, tatami, forest, moon) are in `places/`.

They are built from the saved Animator projects by `tools/build_house_pets.py`, not edited by hand.
Being ours, they may be passed on: a public build keeps them.

## Last Tick's packs (private build, or imported by the owner)

The pack cat (all 43 coats), its room furniture and its animations come from
**Animated Pixel Kittens (Cats) 32x32** by **Last Tick**: https://last-tick.itch.io/animated-pixel-kittens-cats-32x32

The pack bunny (all 27 coats) comes from Last Tick's **Bunny 32x32** pack, under the same licence. It
lives in the pack cat's room and plays the cat's scenes: `bongo/petpack.py` names each row of its
sheet with the cat's animation it stands in for (hops for walks, nibbling for eating...).

They are the extra kinds `packcat` and `packbunny` (`packcat*.json` / `packbunny*.json` and their
atlases), shown in Settings > Pet only where that art is. Their room keeps its own layout (the bowl on
the shelf at the back: tuning `"stage": "pack"`).

The furniture in the bigger pet places (`bongo/modules/pet/rooms.py`: beds, bowls, houses, cat
trees...) is the kittens pack's `extras/props`, kept in `pack-props.json` + `pack-props.webp.b64`; their
plants are from the **nature pack** bought with it (`pack-plants.json` + `pack-plants.webp.b64`, at
half size), and the toys (mouse, carrots, bee) in `pack-toys.*`, under the same terms. The floors, walls
and water in those rooms are drawn in code.

The licence allows using and editing the art in personal and commercial projects. It does not
allow redistributing it as files (edited or not), using it to build other asset packs, or using
it for AI training. So:

- these files live on the private GitLab and the devices it updates, and nowhere public;
- a public (GitHub) build leaves exactly these files out (`python tools/prod_tree.py --public`:
  `packcat*`, `packbunny*`, `pack-*`); its owner can use **Settings > Pet > Import your pet pack** to
  upload the zip they bought, and the Matrix builds the art into `data/pet` under the same names
  (`bongo/petpack.py`, with a room drawn in code from the pack's bed and ball).

The pack atlases are built from the pack by `bongo/petpack.py` (`tools/build_pet_atlas.py` for this
folder, with Billy's remastered room); they are not edited by hand.
A Matrix without `pack-props` or `pack-plants` simply doesn't offer the rooms that need them.
