# Pet art

The cat (all 43 coats), its room furniture and its animations come from
**Animated Pixel Kittens (Cats) 32x32** by **Last Tick**: https://last-tick.itch.io/animated-pixel-kittens-cats-32x32

The bunny (all 27 coats) comes from Last Tick's **Bunny 32x32** pack, under the same licence. It
lives in the cat's room and plays the cat's scenes: `tools/build_pet_atlas.py` names each row of
its sheet with the cat's animation it stands in for (hops for walks, nibbling for eating...).

The licence allows using and editing the art in personal and commercial projects. It does not
allow redistributing it as files (edited or not), using it to build other asset packs, or using
it for AI training. So:

- these files live on the private GitLab and the devices it updates, and nowhere public;
- a public (GitHub) build must leave this folder out and offer "Import your pet pack" instead,
  so each owner brings their own copy of the pack.

The atlases are built from the pack by `tools/build_pet_atlas.py` (it is not edited by hand).
