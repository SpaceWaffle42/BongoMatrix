# Pet art

## Our own pets, homes, places and rooms (ship in every build)

The built-in animals are our own pixel art in the house style, drawn in the Matrix Animator:
`cat` (Mochi, 14 coats), `bunny` (Clover), `dog` (Biscuit) and its breeds (each its own kind, shown under Dog:
`jackrussell` (Jack), `spaniel` (Cocker), `dachshund` (Frankie), `corgi` (Duke), `frenchie` (Gus), `pug` (Pickle),
`shepherd` (Rex), `cockapoo` (Teddy)), `hamster` (Nibbles), `lizard` (Ziggy), `flamingo` (Flo),
`monster` (Krax) and `rock` (Pebble). Each
`<kind>.json` holds its frames, coats ("shape.colour" ids: one drawing recoloured, or an extra drawing
`<kind>-<coat>.webp.b64`), its outfit pieces (`<kind>-outfits.webp.b64`, each stamped at its own marker in the picture that
follows her pose: side-on, curled up, head down... from `tools/house_pets_src/pets/<pN>/poses.json`),
its tuning and its own home (day, empty bowl, night base), all in `<kind>.webp.b64`. The eight places
(garden, beach, snow, space, library, tatami, forest, moon) are in `places/`; the four bigger rooms (living
room and big garden 96x48, playroom and oasis 128x64: floors, walls, props and plants) in `rooms/`.

Each home, place and room also lists the pieces that stand on its floor with their depth key (the floor row
they stand on; `tools/house_pets_src/homes/ground.json` says how each is worked out): the pet, her toys and those
pieces are drawn back to front by it, and up on a piece she counts as standing at its foot (her size too).

They are built from the saved Animator projects by `tools/build_house_pets.py`, not edited by hand.
All of it is our own art and may be passed on: every build ships it.
