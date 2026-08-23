"""Shared fail-closed argparse action for release-critical value options."""
from __future__ import annotations

import argparse


class StoreOnce(argparse.Action):
    """Store one value and reject every repeated occurrence with argparse exit 2."""

    def __call__(self, parser, namespace, values, option_string=None):
        seen = getattr(namespace, "_matrix_store_once_seen", None)
        if seen is None:
            seen = set()
            setattr(namespace, "_matrix_store_once_seen", seen)
        if self.dest in seen:
            parser.error(f"{option_string} may be specified only once")
        seen.add(self.dest)
        setattr(namespace, self.dest, values)


def add_store_once_argument(parser: argparse.ArgumentParser, name: str, **kwargs: object) -> None:
    """Add one value-bearing option using :class:`StoreOnce`."""

    if kwargs.get("action") is not None:
        raise ValueError("store-once value options may not override their action")
    kwargs["action"] = StoreOnce
    parser.add_argument(name, **kwargs)
