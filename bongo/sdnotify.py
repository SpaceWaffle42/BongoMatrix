"""Telling systemd how the display is (``sd_notify``), with Python's own library.

bongo-matrix.service is ``Type=notify`` with ``WatchdogSec=``: the display says ``READY=1`` once its panel
is up, and ``WATCHDOG=1`` while its player is alive (``Live.watch``, every few seconds). If the whole
process hangs, so that nothing says it any more, systemd restarts it; a stuck player is already ended by
``Live.watch`` itself. Where systemd didn't ask (``NOTIFY_SOCKET`` unset: an older unit, a laptop, CI),
nothing is sent.
"""
from __future__ import annotations

import os
import socket


def notify(message: str, environ=os.environ) -> bool:
    """Send ``message`` (``READY=1``, ``WATCHDOG=1``...) to systemd. True if it was sent."""
    where = environ.get("NOTIFY_SOCKET")
    if not where or not hasattr(socket, "AF_UNIX"):
        return False
    if where.startswith("@"):                 # an abstract socket (Linux)
        where = "\0" + where[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as s:
            s.connect(where)
            s.sendall(message.encode())
        return True
    except OSError:
        return False
