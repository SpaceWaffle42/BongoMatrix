"""The events log: what went wrong, kept on the card across restarts and power cuts (``data/events.log``).

The display's own log is in memory (journald is RAM-only on the image, and the dashboard's Logs page keeps
the last few hundred records), so it's gone after a power cut, which is exactly when it's wanted. This
keeps the little that matters on the card:

- warnings and errors (no more than ``MOST_PER_MINUTE`` a minute, so a fault that repeats can't wear the
  card), in two files of ``SIZE`` bytes at most (``events.log`` and ``events.log.1``);
- one line at every start saying how the last run ended: cleanly, or not (``ended``).

How the last run ended: a marker (``data/running``) is written at start with this boot's id and removed on
a clean exit (stopped, restarted, updated, Ctrl+C). Found at the next start, the last run didn't end
cleanly: with the same boot id the display itself stopped (a crash, or the watchdog ending a stuck
player); with another, the whole Pi went down without shutting down (a power cut, or a hard reset).
"""
from __future__ import annotations

import atexit
import json
import logging
import logging.handlers
import os
import time
from pathlib import Path

from bongo import durable

SIZE = 100_000          # bytes per file; two files at most
MOST_PER_MINUTE = 60    # records written a minute; the rest are counted, and the count is written later
log = logging.getLogger("bongo.events")


def boot_id(path: Path = Path("/proc/sys/kernel/random/boot_id")) -> str | None:
    """This boot of the Pi (a new one after every reboot or power cut), or None off Linux."""
    try:
        return path.read_text().strip() or None
    except OSError:
        return None


def ended(data: Path, boot: str | None) -> str:
    """How the last run ended: ``first`` (no run before), ``clean``, ``crash`` (the display stopped,
    the Pi stayed up) or ``power`` (the Pi went down without shutting down)."""
    marker = data / "running"
    if not marker.exists():
        return "clean" if (data / "events.log").exists() else "first"
    try:
        before = json.loads(marker.read_text()).get("boot")
    except (OSError, ValueError, AttributeError):
        before = None       # a marker cut off by the cut itself
    if boot and before == boot:
        return "crash"
    return "power"


WORDS = {
    "first": "started for the first time",
    "clean": "started (the last run ended cleanly)",
    "crash": "started again after the display stopped without shutting down (a crash, or a stuck screen ended by the watchdog)",
    "power": "started after the Matrix lost power or was reset without shutting down",
}


class _Gate(logging.Filter):
    """Warnings and errors, and this module's own lines, at most ``most`` a minute."""

    def __init__(self, most: int | None = None, clock=time.monotonic):
        super().__init__()
        self.most, self.clock = MOST_PER_MINUTE if most is None else most, clock
        self.window, self.count, self.dropped = clock(), 0, 0

    def filter(self, record: logging.LogRecord) -> bool:
        if record.levelno < logging.WARNING and not record.name.endswith(log.name):
            return False
        now = self.clock()
        if now - self.window >= 60:
            if self.dropped:   # say what was left out, on the first line let through
                record.msg = f"({self.dropped} more lines left out in the last minute) {record.msg}"
            self.window, self.count, self.dropped = now, 0, 0
        if self.count >= self.most:
            self.dropped += 1
            return False
        self.count += 1
        return True


def start(data: Path, boot: str | None = None, root: logging.Logger | None = None,
          register=atexit.register) -> str:
    """Called once as the display starts: open the events log, note how the last run ended, and leave the
    marker for this run. Returns how the last run ended (see ``ended``). Never raises: the display starts
    whatever happens here."""
    data = Path(data)
    boot = boot if boot is not None else boot_id()
    how = "first"
    try:
        data.mkdir(parents=True, exist_ok=True)
        how = ended(data, boot)
        handler = logging.handlers.RotatingFileHandler(data / "events.log", maxBytes=SIZE, backupCount=1,
                                                       encoding="utf-8")
        handler.setLevel(logging.INFO)
        handler.addFilter(_Gate())
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s",
                                               "%Y-%m-%d %H:%M:%S"))
        root = root or logging.getLogger()
        root.addHandler(handler)
        mine = root.getChild(log.name)   # (bongo.events itself, under the real root logger)
        (mine.warning if how in ("crash", "power") else mine.info)("Bongo Matrix %s", WORDS[how])
        durable.write_text(data / "running", json.dumps({"boot": boot, "pid": os.getpid()}))
        register(lambda: finish(data, mine))
    except OSError as error:   # a read-only or full card: the display still starts
        logging.getLogger(__name__).warning("events log not started: %s", error)
    return how


def finish(data: Path, logger: logging.Logger = log) -> None:
    """A clean exit: the marker goes, so the next start knows this run ended properly."""
    try:
        logger.info("Bongo Matrix stopped cleanly")
        (Path(data) / "running").unlink(missing_ok=True)
        durable.sync_folder(Path(data))
    except OSError:
        pass
