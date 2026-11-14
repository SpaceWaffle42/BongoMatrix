# Pet art

The cat (all 43 coats), its room furniture and its animations come from
**Animated Pixel Kittens (Cats) 32x32** by **Last Tick**: https://last-tick.itch.io/animated-pixel-kittens-cats-32x32

The bunny (all 27 coats) comes from Last Tick's **Bunny 32x32** pack, under the same licence. It
lives in the cat's room and plays the cat's scenes: `tools/build_pet_atlas.py` names each row of
its sheet with the cat's animation it stands in for (hops for walks, nibbling for eating...).

The furniture in the bigger pet places (`bongo/modules/pet/rooms.py`: beds, bowls, houses, cat
trees...) is the kittens pack's `extras/props`, kept in `props.json` + `props.webp.b64`; their plants
are from the **nature pack** bought with it (`plants.json` + `plants.webp.b64`, at half size), under
the same terms. The floors, walls and water in those rooms are drawn in code.

The licence allows using and editing the art in personal and commercial projects. It does not
allow redistributing it as files (edited or not), using it to build other asset packs, or using
it for AI training. So:

- these files live on the private GitLab and the devices it updates, and nowhere public;
- a public (GitHub) build leaves the art out (`python tools/prod_tree.py --public`); its owner
  uses **Settings > Pet > Import your pet pack** to upload the zip they bought, and the Matrix
  builds the art into `data/pet` itself (`bongo/petpack.py`, with a room drawn in code from the
  pack's bed and ball). Until then the Pet screens say where to import it.

The atlases are built from the pack by `bongo/petpack.py` (`tools/build_pet_atlas.py` for this
folder, with Billy's remastered room); they are not edited by hand.
A Matrix without `props` or `plants` (a public build whose owner hasn't imported those packs)
simply doesn't offer the rooms that need them.
