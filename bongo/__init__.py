"""Bongo Matrix: a modular display for a 64x32 RGB LED panel."""
import re

# The public number, bumped by hand at a release. (Builds before the public numbering stamped "3.0.0.dev0" and
# other "3.x" strings: bongo.hub.version_key counts those as older than this.)
__version__ = "1.0.0"

PUBLIC_BRANCH = "main"
BRANCH_SHOWN = 24   # the most characters of a branch name the label carries


def version_label(branch: str | None = PUBLIC_BRANCH, number: str | None = None) -> str:
    """What people are shown as the version: "v1.0.0", plus ".<branch>" when this Matrix updates from any branch
    other than ``main`` (the update source's branch, see bongo.update.source, not the checkout's). The branch is
    made safe to show: ASCII letters, digits, ``.``, ``_`` and ``-`` stay, anything else (``/``, spaces, other
    scripts) becomes ``_``, and it is cut at BRANCH_SHOWN characters. Nothing else of the source is ever in it."""
    label = f"v{number or __version__}"
    if not isinstance(branch, str) or not branch or branch == PUBLIC_BRANCH:
        return label
    return f"{label}.{re.sub(r'[^A-Za-z0-9._-]', '_', branch)[:BRANCH_SHOWN]}"
