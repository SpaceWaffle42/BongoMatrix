"""The Matrix's pet: one animal and one name, shared by everything that shows it.

    "pet": {"kind": "cat", "name": "Mochi", "coat": "shorthair.ginger_tabby", "collar": "red", "dress_up": true}

The Pet module, the Wander overlay and (soon) the Pet clock all draw this pet; there is no way
to give them different ones. ``kind`` is an art set in resources/pet (``<kind>.json`` beside
``<kind>.webp.b64``), so adding an animal is adding its art. ``coat`` is one of the colourings
listed in ``<kind>.json`` ("shape.colour" ids; left out, the kind's default). The name is drawn in the pet's
tiny 3x5 font (name tag, speech banners), so it keeps to the characters that font has.

The animals are all our own art (tools/build_house_pets.py) and ship with every build.

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


from bongo import paths

BUILT_IN = Path(__file__).resolve().parents[1] / "resources" / "pet"
# the dog's breeds, each its own kind (Settings > Pet shows them under Dog, after Biscuit)
DOGS = ["jackrussell", "spaniel", "dachshund", "corgi", "frenchie", "pug", "shepherd", "cockapoo"]
# the animals in the order Settings > Pet shows them
ORDER = ["cat", "bunny", "dog", *DOGS, "hamster", "lizard", "flamingo", "monster", "rock"]


def art_file(name: str) -> Path:
    """An art file (``cat.json``, ``places/places.webp.b64``...)."""
    return BUILT_IN / name


def kinds_found() -> list[str]:
    """Every animal with art here, in ORDER."""
    return [k for k in ORDER if (BUILT_IN / f"{k}.json").exists()]


NAME = re.compile(r"[A-Za-z0-9 '.!?-]{1,12}")


@dataclass(frozen=True)
class Pet:
    kind: str = "cat"
    name: str = "Mochi"
    coat: str = ""  # "" = the kind's default coat
    collar: str = ""  # "" = no collar
    dress_up: bool = True  # seasonal outfits


@lru_cache(maxsize=len(ORDER) + 16)   # every kind, with room to grow
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
    name = _index(kind).get("name")
    return name if isinstance(name, str) and NAME.fullmatch(name) else Pet.name


GROUPS = {k: "dog" for k in DOGS}                              # the dog's other shapes: Settings shows them under Dog


def accessory(kind: str) -> str:
    """What its everyday accessory is ("collar", the bunny's "flower", Pebble's "bow tie"...)."""
    return _index(kind).get("accessory") or "collar"


def title(kind: str) -> str:
    """What Settings > Pet calls the animal ("Jack Russell")."""
    return _index(kind).get("title") or kind.replace("_", " ").title()


def group(kind: str) -> str:
    """The animal a kind is a shape of (Settings > Pet: pick the animal, then its shape, then its colour)."""
    return GROUPS.get(kind, kind)


def shape(coat: str) -> str:
    """The shape part of a coat id ("shorthair" of "shorthair.tuxedo")."""
    return coat.split(".", 1)[0] if "." in (coat or "") else ""


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


# -- Last Tick's pack art, removed 24 Nov 2026 ------------------------------------------------------------------
PACK = {"packcat": "cat", "packbunny": "bunny"}    # the pack's kinds -> ours
PACK_NAMES = {"packbunny": "Bun"}                   # (an unnamed pack bunny was called Bun; the pack cat Mochi, as ours)
OUR_COAT = re.compile(r"[a-z_]+\.[a-z_]+")         # ours are "shape.colour"; the pack's "10", "1.3"
# the files the pack import wrote (data/pet, and replaced/ in it; under the pack's names, or before 24 Nov 2026 ours)
# and a private build's pack files (resources/pet)
OLD_ART = re.compile(r"(packcat|packbunny|pack-\w+|cat|bunny|props|plants|toys)([-.][\w.]*)?\.(json|webp\.b64)")
PACK_FILES = re.compile(r"(packcat|packbunny|pack-)[\w.-]*")
UPLOAD = re.compile(r"tmp\w{8}\.zip")              # an upload left by an import cut short (tempfile.mkstemp in data/)
DONE = ".pack-art-removed"                            # in data/: written once nothing is left to remove


def migrate(config: dict) -> list[str]:
    """At config load: a pet of Last Tick's (``packcat`` / ``packbunny``, or a ``cat`` / ``bunny`` saved with one
    of the pack's coat ids, which aren't "shape.colour") becomes our own cat or bunny in its default coat, with
    the same name; its collar and dress_up stay where ours has them. A cat or bunny's collar colour only the pack
    had goes too. Running it again changes nothing."""
    rule = config.get("pet")
    if not isinstance(rule, dict) or PACK.get(rule.get("kind", Pet.kind), rule.get("kind", Pet.kind)) not in PACK.values():
        return []
    old, coat, collar = rule.get("kind", Pet.kind), rule.get("coat"), rule.get("collar")
    kind = PACK.get(old, old)
    pack_coat = coat not in (None, "") and not (isinstance(coat, str) and OUR_COAT.fullmatch(coat))
    pack_collar = collar not in (None, "") and collar not in collars(kind)
    if old not in PACK and not pack_coat:
        if not pack_collar:
            return []
        config["pet"] = {k: v for k, v in rule.items() if k != "collar"}
        return [f"pet: our {kind} has no {collar} collar; it wears none"]
    new = {k: v for k, v in rule.items() if k not in ("coat", "collar")} | {"kind": kind}
    if not pack_collar and collar not in (None, ""):
        new["collar"] = collar
    if "name" not in rule and old in PACK_NAMES:
        new["name"] = PACK_NAMES[old]
    config["pet"] = new
    return [f"pet: Last Tick's art is gone; {new.get('name', Pet.name)} is now our own {kind} in its default colour"]


def _pack_upload(f: Path) -> bool:
    """Is ``f`` (data/tmpXXXXXXXX.zip) a pack zip an interrupted import left? (its list of names only)"""
    import zipfile
    try:
        with zipfile.ZipFile(f) as z:
            return any("cats_animations/" in n or "/Bunny " in n or n.endswith("/shrubs_10.png") for n in z.namelist())
    except (OSError, zipfile.BadZipFile):
        return False


def remove_pack_files(data: Path | None = None, built: Path | None = None) -> list[str]:
    """Once, at start-up, until it has all gone: delete the pack's art wherever it was put (the files the dashboard's
    import wrote in data/pet and its replaced/ folder, an upload an interrupted import left in data/, and a private
    build's pack files in resources/pet), and nothing else. Logs what it removed. Never raises: what can't be
    removed is one warning, and on later starts a debug line, until it can be; then data/.pack-art-removed says
    there is nothing more to look for. Returns the notes it logged."""
    import logging
    log = logging.getLogger("bongo.pets")
    data, built = data or paths.DATA, built or BUILT_IN
    done = data / DONE
    if done.exists():
        return []
    found, failed, gone = [], [], []
    for folder, names in ((data / "pet", OLD_ART), (data / "pet" / "replaced", OLD_ART), (built, PACK_FILES),
                          (data, UPLOAD)):
        try:
            found += sorted(f for f in folder.iterdir() if names.fullmatch(f.name) and f.is_file()
                            and (names is not UPLOAD or _pack_upload(f)))
        except FileNotFoundError:
            pass
        except OSError as error:
            failed.append(f"{folder} ({error.strerror or error})")
    for f in found:
        try:
            f.unlink()
            gone.append(f.name)
        except FileNotFoundError:
            pass
        except OSError as error:
            failed.append(f"{f} ({error.strerror or error})")
    for folder in (data / "pet" / "replaced", data / "pet"):
        try:
            folder.rmdir()
        except OSError:
            pass                                      # gone, or something else is in it: left as it is
    notes = []
    if gone:
        notes.append(f"pet: removed Last Tick's pack art ({len(gone)} file{'s' * (len(gone) != 1)}: "
                     f"{', '.join(gone[:6])}{', ...' if len(gone) > 6 else ''})")
        log.warning(notes[-1])
    tried = data / (DONE + ".tried")
    if failed:
        notes.append(f"pet: couldn't remove Last Tick's art from {len(failed)} place{'s' * (len(failed) != 1)} "
                     f"(first: {failed[0]}); trying again next start")
        log.log(logging.DEBUG if tried.exists() else logging.WARNING, notes[-1])
    try:
        if failed:
            tried.touch()
        else:
            done.touch()
            tried.unlink(missing_ok=True)
    except OSError:
        pass                                          # (a read-only data/: it just looks again next start)
    return notes
