"""The Matrix's pet: one animal and one name, shared by everything that shows it.

    "pet": {"kind": "cat", "name": "Mochi", "coat": "shorthair.ginger_tabby", "collar": "red", "dress_up": true}

The Pet module, the Wander overlay and (soon) the Pet clock all draw this pet; there is no way
to give them different ones. ``kind`` is an art set in resources/pet (``<kind>.json`` beside
``<kind>.webp.b64``), so adding an animal is adding its art. ``coat`` is one of the colourings
listed in ``<kind>.json`` ("shape.colour" ids; left out, the kind's default). The name is drawn in the pet's
tiny 3x5 font (name tag, speech banners), so it keeps to the characters that font has.

The built-in animals are our own art (tools/build_house_pets.py) and ship with every build. Last Tick's
cat and bunny, when their pack is on this Matrix, are the extra kinds ``packcat`` and ``packbunny``.

``collar`` ("" for none) is worn every day; with ``dress_up`` on (the default) she also dresses
for the season: a Santa hat or reindeer antlers through December to Boxing Day, a bow or heart
glasses in the days before Valentine's and cupid's on the day. Each animal has its own version of
each piece (a wreath for the dog's antlers, a Rudolph nose for the bunny's...).
"""
from __future__ import annotations

import datetime as dt
import json
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import os

from bongo import paths

BUILT_IN = Path(__file__).resolve().parents[1] / "resources" / "pet"
# a pack the owner imported from the dashboard (bongo/petpack.py); it wins over built-in art
IMPORTED = Path(os.environ.get("BONGO_PET_ART", paths.DATA / "pet"))
# a pack's toys and the bigger places' furniture and plants (and their names before 24 Nov 2026)
NOT_ANIMALS = {"pack-toys", "pack-props", "pack-plants", "toys", "props", "plants"}
# the built-in animals in the order Settings > Pet shows them; anything else found (a pack's kinds) follows
ORDER = ["cat", "bunny", "dog", "jackrussell", "spaniel", "hamster", "lizard", "flamingo", "monster", "rock"]
PACK_KINDS = {"cat": "packcat", "bunny": "packbunny"}   # our kind -> the Last Tick kind it replaced
PACK_ORDER = ["packcat", "packbunny"]
PACK_COAT = re.compile(r"\d+(\.\d+)?")                   # Last Tick coat ids ("10", "1.3"); ours are "shape.colour"


def art_file(name: str) -> Path:
    """Where an art file (``packcat.json``, ``packcat-2.webp.b64``...) is: imported first, then built in. Our own
    animals and places are always the built-in ones (an import only ever writes the pack's names, so a file of
    ours in data/pet is a leftover, never something to show)."""
    if "/" in name or name.split(".", 1)[0].split("-", 1)[0] in ORDER:
        return BUILT_IN / name
    imported = IMPORTED / name
    return imported if imported.exists() else BUILT_IN / name


def has_art() -> bool:
    return bool(kinds_found())


def kinds_found() -> list[str]:
    """Every animal with art here: ours first, then a pack's (only when its art has been imported)."""
    found = {p.stem for folder in (IMPORTED, BUILT_IN) for p in folder.glob("*.json")} - NOT_ANIMALS
    return [k for k in ORDER + PACK_ORDER if k in found] + sorted(found - set(ORDER + PACK_ORDER))


def reload() -> None:
    """Forget cached art (after an import)."""
    _index.cache_clear()
    try:
        from bongo.modules.pet import art
        art.forget()
    except ImportError:
        pass
NAME = re.compile(r"[A-Za-z0-9 '.!?-]{1,12}")


@dataclass(frozen=True)
class Pet:
    kind: str = "cat"
    name: str = "Mochi"
    coat: str = ""  # "" = the kind's default coat
    collar: str = ""  # "" = no collar
    dress_up: bool = True  # seasonal outfits


@lru_cache(maxsize=8)
def _index(kind: str) -> dict:
    try:
        return json.loads(art_file(f"{kind}.json").read_text())
    except (OSError, ValueError):
        return {}


def coats(kind: str) -> list[str]:
    """The coats there is art for, default first (empty if the kind has only one look)."""
    return [c["id"] for c in _index(kind).get("coats", [])]


def coat_names(kind: str) -> list[dict]:
    """[{"id", "name"}] for the kind's coats, default first, for the dashboard."""
    return [{"id": c["id"], "name": c.get("name", c["id"])} for c in _index(kind).get("coats", [])]


def collars(kind: str) -> list[str]:
    """The collar colours there is art for (none for an animal without outfits)."""
    return sorted(o.split("_", 1)[1] for o in _index(kind).get("outfits", {}) if o.startswith("collar_"))


def wearing(pet: "Pet", day: dt.date) -> tuple:
    """What ``pet`` has on today: her collar, and a seasonal outfit if she dresses up."""
    out = [f"collar_{pet.collar}"] if pet.collar else []
    if pet.dress_up:
        if day.month == 12 and day.day <= 26:
            out.append("santa_hat" if day.day in (24, 25) or day.day % 2 == 0 else "antlers")
        elif day.month == 2 and 10 <= day.day <= 14:
            out.append("cupid" if day.day == 14 else "red_bow" if day.day % 2 == 0 else "heart_glasses")
    return tuple(out)


def default_coat(kind: str) -> str:
    return _index(kind).get("default", "")


def default_name(kind: str) -> str:
    """A new pet's name: the one its art comes with (Biscuit the dog, Krax the monster...)."""
    name = _index(kind).get("name") or NAMES.get(kind)
    return name if isinstance(name, str) and NAME.fullmatch(name) else Pet.name


TITLES = {"packcat": "Pack cat", "packbunny": "Pack bunny"}   # (the pack's art has no titles of its own)
NAMES = {"packbunny": "Bun"}                                    # (nor names: the pack cat is Mochi, as ever)
GROUPS = {"jackrussell": "dog", "spaniel": "dog"}               # the dog's other shapes: Settings shows them under Dog


def accessory(kind: str) -> str:
    """What its everyday accessory is ("collar", the bunny's "flower", Pebble's "bow tie"...)."""
    return _index(kind).get("accessory") or "collar"


def title(kind: str) -> str:
    """What Settings > Pet calls the animal ("Jack Russell")."""
    return _index(kind).get("title") or TITLES.get(kind) or kind.replace("_", " ").title()


def group(kind: str) -> str:
    """The animal a kind is a shape of (Settings > Pet: pick the animal, then its shape, then its colour)."""
    return GROUPS.get(kind, kind)


def shape(coat: str) -> str:
    """The shape part of a coat id ("shorthair" of "shorthair.tuxedo"; "" for a pack's coat ids)."""
    return "" if PACK_COAT.fullmatch(coat or "") or "." not in (coat or "") else coat.split(".", 1)[0]


def kinds() -> list[str]:
    """The animals there is art for."""
    return kinds_found() or ["cat"]


def of(config: dict) -> Pet:
    """The configured pet. Whatever of it is unusable falls back on its own (an unknown coat or collar is
    the kind's default, an unknown kind the cat) and the rest is kept: never a whole new pet."""
    rule = config.get("pet")
    if not isinstance(rule, dict):
        return Pet()
    kind = rule.get("kind", Pet.kind)
    if kind not in kinds():
        kind = Pet.kind
    name = rule.get("name")
    name = name.strip() if isinstance(name, str) and NAME.fullmatch(name.strip()) else default_name(kind)
    coat = rule.get("coat") if rule.get("coat") in coats(kind) else ""
    collar = rule.get("collar") if rule.get("collar") in collars(kind) else ""
    return Pet(kind=kind, name=name, coat=coat, collar=collar, dress_up=rule.get("dress_up", True) is not False)


def problems(rule) -> list[str]:
    """Readable problems with the ``pet`` section of the config."""
    if rule is None:
        return []
    if not isinstance(rule, dict) or set(rule) - {"kind", "name", "coat", "collar", "dress_up"}:
        return ["pet: must be an object with kind, name, coat, collar and dress_up"]
    out = []
    if rule.get("kind", Pet.kind) not in kinds():
        out.append(f"pet.kind: one of {', '.join(kinds())}")
    elif rule.get("coat") not in (None, "") and rule.get("coat") not in coats(rule.get("kind", Pet.kind)):
        out.append(f"pet.coat: one of the {rule.get('kind', Pet.kind)}'s coats")
    if rule.get("collar") not in (None, "") and rule.get("collar") not in collars(rule.get("kind", Pet.kind)):
        out.append(f"pet.collar: none, or one of {', '.join(collars(rule.get('kind', Pet.kind))) or 'no colours for this animal'}")
    if "dress_up" in rule and not isinstance(rule["dress_up"], bool):
        out.append("pet.dress_up: true or false")
    name = rule.get("name", Pet.name)
    if not isinstance(name, str) or not NAME.fullmatch(name.strip()):
        out.append("pet.name: 1 to 12 letters, digits, spaces or ' . ! ? -")
    return out


def adopt(config: dict) -> list[str]:
    """Before 11 Nov 2026 each Pet playlist entry had its own ``name``. Keep the first one as the
    Matrix's pet (the entries' own names are then dropped as settings the module no longer has)."""
    if "pet" in config:
        return []
    for key in ("playlist", "overlays"):
        for entry in config.get(key) or []:
            if isinstance(entry, dict) and entry.get("module") == "pet" and isinstance(entry.get("settings"), dict):
                name = entry["settings"].get("name")
                if isinstance(name, str) and NAME.fullmatch(name.strip()):
                    config["pet"] = {"kind": Pet.kind, "name": name.strip()}
                    return [f"pet: {name.strip()} is now the Matrix's pet (set in Settings > Pet)"]
    return []


def migrate(config: dict) -> list[str]:
    """At config load: our own art took over the kinds "cat" and "bunny" (24 Nov 2026), and Last Tick's became
    "packcat" / "packbunny". A pet saved before that with a Last Tick coat ("10", "1.3") becomes the pack's
    animal with the same coat and name when the pack is on this Matrix, else our own cat or bunny with the same
    name in its default coat. A collar colour only the pack had is dropped. Running it again changes nothing.
    (The files of a pack imported before then are renamed once at start-up: ``migrate_imported``.)
    The change is made in memory at load (tidy), so until the config is next saved its note comes back on every
    start; that also means a Matrix without the pack gets the pack cat back if the owner imports the pack later."""
    rule = config.get("pet")
    if not isinstance(rule, dict) or rule.get("kind", Pet.kind) not in PACK_KINDS:
        return []
    kind = rule.get("kind", Pet.kind)
    coat = rule.get("coat")
    if not (isinstance(coat, str) and PACK_COAT.fullmatch(coat)):
        collar = rule.get("collar")
        if collar not in (None, "") and collar not in collars(kind):   # (a coatless pet with the pack's collar)
            config["pet"] = {k: v for k, v in rule.items() if k != "collar"}
            return [f"pet: the {kind} has no {collar} collar any more; it wears none"]
        return []
    pack = PACK_KINDS[kind]
    if pack in kinds_found():
        config["pet"] = {**rule, "kind": pack}
        return [f"pet: {kind} with a Last Tick coat is now {pack} (the pack's {kind})"]
    keep = {k: v for k, v in rule.items() if k not in ("coat", "collar")}
    if rule.get("collar") in collars(kind):
        keep["collar"] = rule["collar"]
    config["pet"] = {**keep, "kind": kind}
    return [f"pet: the Last Tick {kind} art isn't here; {rule.get('name', Pet.name)} is now the built-in {kind}"]


OLD_PACK_NAMES = {"cat": "packcat", "bunny": "packbunny", "props": "pack-props", "plants": "pack-plants",
                  "toys": "pack-toys"}


def _old_pack_file(folder: Path, old: str) -> bool:
    """Is ``<old>.json`` in ``folder`` really what a pack import wrote (not some other art of that name)?"""
    try:
        index = json.loads((folder / f"{old}.json").read_text())
    except (OSError, ValueError):
        return False
    if not isinstance(index, dict):
        return False
    if old == "cat":
        coats = index.get("coats")
        return isinstance(coats, list) and bool(coats) and \
            all(isinstance(c, dict) and PACK_COAT.fullmatch(str(c.get("id", ""))) for c in coats)
    if old == "bunny":
        return index.get("room_from") in ("cat", "packcat")
    from bongo import petpack
    known = {"props": set(petpack.PROPS) | {f"{k}_full" for k in petpack.PROPS}, "plants": set(petpack.PLANTS),
             "toys": set(petpack.TOYS)}[old]
    return bool(index) and set(index) <= known


def migrate_imported(folder: Path | None = None) -> list[str]:
    """Once, at start-up: a pack imported before the rename wrote cat*/bunny*/props*/plants*/toys* into data/pet.
    Rename exactly the pack's own files to the pack names: renames only, nothing is rewritten (art.py knows the
    pack's kinds live in the pack room, whatever their JSON says). The .json files go last, so a start cut short
    still recognises the pack next time and finishes the job. Where a newer import already has the new name, the
    old file is moved aside into ``replaced/`` (never deleted). Never raises: what can't be done is said in a note."""
    folder = folder or IMPORTED
    if not folder.is_dir():
        return []
    notes, moved, failed = [], 0, set()
    packs = {old for old in OLD_PACK_NAMES if _old_pack_file(folder, old)}
    for f in sorted((p for p in folder.iterdir() if p.is_file()), key=lambda p: (p.name.endswith(".json"), p.name)):
        stem = f.name.split(".", 1)[0]
        old = stem.split("-", 1)[0] if stem.split("-", 1)[0] in ("cat", "bunny") else stem
        if old not in packs or not (f.name.endswith(".json") or f.name.endswith(".webp.b64")):
            continue
        target = folder / (OLD_PACK_NAMES[old] + f.name[len(old):])
        if f.name == f"{old}.json" and old in failed:
            continue                     # kept, so the next start still knows these files are the pack's
        try:
            if target.exists():
                aside = folder / "replaced"
                aside.mkdir(exist_ok=True)
                f.rename(aside / f.name)
            else:
                f.rename(target)
            moved += 1
        except OSError as error:
            failed.add(old)
            notes.append(f"pet: couldn't rename the imported {f.name} ({error}); the Pet screens may show the pack's art as ours")
    if moved:
        reload()
        notes.insert(0, f"pet: the imported Last Tick art is now the pack's own kinds ({moved} files renamed)")
    return notes
