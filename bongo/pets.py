"""The Matrix's pet: one animal and one name, shared by everything that shows it.

    "pet": {"kind": "cat", "name": "Mochi", "coat": "10"}

The Pet module, the Wander overlay and (soon) the Pet clock all draw this pet; there is no way
to give them different ones. ``kind`` is an art set in resources/pet (``<kind>.json`` beside
``<kind>.webp.b64``), so adding an animal is adding its art. ``coat`` is one of the colourings
listed in ``<kind>.json`` (43 for the cat; left out, the kind's default). The name is drawn in the pet's
tiny 3x5 font (name tag, speech banners), so it keeps to the characters that font has.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

FOLDER = Path(__file__).resolve().parents[1] / "resources" / "pet"
NAME = re.compile(r"[A-Za-z0-9 '.!?-]{1,12}")


@dataclass(frozen=True)
class Pet:
    kind: str = "cat"
    name: str = "Mochi"
    coat: str = ""  # "" = the kind's default coat


@lru_cache(maxsize=8)
def _index(kind: str) -> dict:
    try:
        return json.loads((FOLDER / f"{kind}.json").read_text())
    except (OSError, ValueError):
        return {}


def coats(kind: str) -> list[str]:
    """The coats there is art for, default first (empty if the kind has only one look)."""
    return [c["id"] for c in _index(kind).get("coats", [])]


def coat_names(kind: str) -> list[dict]:
    """[{"id", "name"}] for the kind's coats, default first, for the dashboard."""
    return [{"id": c["id"], "name": c.get("name", c["id"])} for c in _index(kind).get("coats", [])]


def default_coat(kind: str) -> str:
    return _index(kind).get("default", "")


def kinds() -> list[str]:
    """The animals there is art for."""
    return sorted(p.stem for p in FOLDER.glob("*.json")) or ["cat"]


def of(config: dict) -> Pet:
    """The configured pet, or the default one (Mochi the cat) when none is set or it's unusable."""
    rule = config.get("pet")
    if not isinstance(rule, dict) or problems(rule):
        return Pet()
    kind = rule.get("kind", Pet.kind)
    return Pet(kind=kind, name=rule.get("name", Pet.name).strip(), coat=rule.get("coat") or "")


def problems(rule) -> list[str]:
    """Readable problems with the ``pet`` section of the config."""
    if rule is None:
        return []
    if not isinstance(rule, dict) or set(rule) - {"kind", "name", "coat"}:
        return ["pet: must be an object with kind, name and coat"]
    out = []
    if rule.get("kind", Pet.kind) not in kinds():
        out.append(f"pet.kind: one of {', '.join(kinds())}")
    elif rule.get("coat") not in (None, "") and rule.get("coat") not in coats(rule.get("kind", Pet.kind)):
        out.append(f"pet.coat: one of the {rule.get('kind', Pet.kind)}'s coats")
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
