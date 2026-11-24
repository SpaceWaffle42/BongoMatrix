"""Where the Matrix keeps what it writes: one folder, ``DATA``.

Settings, uploads, sign-ins, imported modules, the events log and the updater's notes all live under it. It is
the checkout's own data/ folder unless ``BONGO_DATA`` says otherwise: the power-loss image puts it on a partition
of its own, apart from the code, which is read-only there (claude/power-loss-design.md, section 5).

Nothing here imports anything else from bongo, so every module can use it. bongo/update.py works the same out for
itself, as it uses only Python's own library.
"""
from __future__ import annotations

import os
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DATA = Path(os.environ.get("BONGO_DATA") or REPO / "data")
