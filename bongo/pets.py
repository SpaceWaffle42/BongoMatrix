"""The Matrix's pet: one animal and one name, shared by everything that shows it.

    "pet": {"kind": "cat", "name": "Mochi", "coat": "10", "collar": "blue", "dress_up": true}

The Pet module, the Wander overlay and (soon) the Pet clock all draw this pet; there is no way
to give them different ones. ``kind`` is an art set in resources/pet (``<kind>.json`` beside
``<kind>.webp.b64``), so adding an animal is adding its art. ``coat`` is one of the colourings
listed in ``<kind>.json`` (43 for the cat; left out, the kind's default). The name is drawn in the pet's
tiny 3x5 font (name tag, speech banners), so it keeps to the characters that font has.

``collar`` ("" for none) is worn every day; with ``dress_up`` on (the default) she also dresses
for the season: a Santa hat or reindeer antlers through December to Boxing Day, a bow or heart
glasses in the days before Valentine's and cupid's wings on the day. Only animals with outfit art
(the cat) wear them.
"""
from __future__ import annotations

import datetime as dt
import json
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import os

BUILT_IN = Path(__file__).resolve().parents[1] / "resources" / "pet"
# a pack the owner imported from the dashboard (bongo/petpack.py); it wins over built-in art
IMPORTED = Path(os.environ.get("BONGO_PET_ART", Path(__file__).resolve().parents[1] / "data" / "pet"))
NOT_ANIMALS = {"toys"}


def art_file(name: str) -> Path:
    """Where an art file (``cat.json``, ``cat-2.webp.b64``...) is: imported first, then built in."""
    imported = IMPORTED / name
    return imported if imported.exists() else BUILT_IN / name


def has_art() -> bool:
    return bool(kinds_found())


def kinds_found() -> list[str]:
    return sorted({p.stem for folder in (IMPORTED, BUILT_IN) for p in folder.glob("*.json")} - NOT_ANIMALS)


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


def kinds() -> list[str]:
    """The animals there is art for."""
    return kinds_found() or ["cat"]


def of(config: dict) -> Pet:
    """The configured pet, or the default one (Mochi the cat) when none is set or it's unusable."""
    rule = config.get("pet")
    if not isinstance(rule, dict) or problems(rule):
        return Pet()
    kind = rule.get("kind", Pet.kind)
    return Pet(kind=kind, name=rule.get("name", Pet.name).strip(), coat=rule.get("coat") or "",
               collar=rule.get("collar") or "", dress_up=rule.get("dress_up", True) is not False)


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
