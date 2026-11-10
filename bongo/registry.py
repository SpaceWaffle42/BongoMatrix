"""Finds modules: every package in bongo/modules/ that exposes ``MODULE``.

Add a module by adding a folder. Remove it by deleting the folder. A module that
fails to import is logged and skipped so it can never take the others down.
"""
from __future__ import annotations

import importlib
import logging
import pkgutil

import bongo.modules
from bongo.module import Module

log = logging.getLogger(__name__)


def discover(strict: bool = False) -> dict[str, Module]:
    found: dict[str, Module] = {}
    for info in pkgutil.iter_modules(bongo.modules.__path__):
        if not info.ispkg:
            continue
        try:
            module = importlib.import_module(f"bongo.modules.{info.name}").MODULE
            if not isinstance(module, Module):
                raise TypeError("MODULE is not a bongo.module.Module")
            if module.id in found:
                raise ValueError(f"duplicate module id {module.id!r}")
        except Exception:
            if strict:
                raise
            log.exception("skipping module %s", info.name)
            continue
        found[module.id] = module
    return found
