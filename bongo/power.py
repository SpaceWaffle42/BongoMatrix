"""Restart from the dashboard (Settings > Device > Restart): just the display software, or the whole Pi.

The dashboard answers first and the restart follows a moment later, so the page hears that it worked.
Only on the Matrix itself (root on Linux, as bongo/netname.py checks); elsewhere it says so and does nothing.
"""
from __future__ import annotations

import logging
import shutil
import subprocess
import threading

from bongo.netname import can_manage_system
from bongo.update import SERVICE

log = logging.getLogger("bongo.power")
_asked: list[str] = []   # a restart has been asked for: no update or second restart may start now

DELAY = 1.5   # seconds: time for the dashboard's reply to reach the page
COMMANDS = {
    "display": ["systemctl", "restart", "--no-block", SERVICE],   # the display software: back in about 10 s
    "device": ["systemctl", "reboot"],                            # the whole Pi: back in about a minute
}


def available() -> bool:
    return can_manage_system() and shutil.which("systemctl") is not None


def pending() -> bool:
    """Whether a restart is on its way (until this process ends, or the restart command fails)."""
    return bool(_asked)


def restart(what: str, who: str | None = None, run=subprocess.run, later=threading.Timer) -> None:
    """Restart ``what`` ("display" or "device") after DELAY. ValueError for anything else."""
    if what not in COMMANDS:
        raise ValueError(f"restart: one of {', '.join(COMMANDS)}")
    _asked.append(what)
    log.info("%s restart requested by %s", "Display" if what == "display" else "Device", who or "someone")

    def go():
        try:
            run(COMMANDS[what], check=True, capture_output=True, timeout=30)
        except (OSError, subprocess.SubprocessError) as error:
            _asked.clear()   # nothing is restarting after all
            log.warning("couldn't restart the %s: %s", what, error)
    timer = later(DELAY, go)
    timer.daemon = True
    timer.start()
