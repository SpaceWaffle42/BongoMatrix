"""The events log: what went wrong, kept on the card across restarts and power cuts (``data/events.log``).

The display's own log is in memory (journald is RAM-only on the image, and the dashboard's Logs page keeps
the last few hundred records), so it's gone after a power cut, which is exactly when it's wanted. This
keeps the little that matters on the card:

- warnings and errors (no more than ``MOST_PER_MINUTE`` a minute, so a fault that repeats can't wear the
  card), in two files of ``SIZE`` bytes at most (``events.log`` and ``events.log.1``);
- one line at every start saying how the last run ended: cleanly, or not (``ended``);
- the last ``HISTORY`` starts and how each came about (``data/starts.json``), for the dashboard's "Last start"
  (``summary``: Settings > Device and Logs).

``download`` puts it all in one text file for Logs > Download logs.

How the last run ended: a marker (``data/running``) is written at start with this boot's id and removed on
a clean exit (stopped, restarted, updated, Ctrl+C). Found at the next start, the last run didn't end
cleanly: with the same boot id the display itself stopped (a crash, or the watchdog ending a stuck
player); with another, the whole Pi went down without shutting down (a power cut, or a hard reset).
"""
from __future__ import annotations

import atexit
import datetime as dt
import json
import logging
import logging.handlers
import os
import time
from pathlib import Path

from bongo import durable

SIZE = 100_000          # bytes per file; two files at most
MOST_PER_MINUTE = 60    # records written a minute; the rest are counted, and the count is written later
HISTORY = 60            # starts remembered in starts.json
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
    "power": "started after being switched off without shutting down (the power went off, or it was unplugged)",
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
        record(data, how)
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


def _history(data: Path) -> list:
    try:
        said = json.loads((Path(data) / "starts.json").read_text())
    except (OSError, ValueError):
        return []
    return [s for s in said if isinstance(s, dict) and isinstance(s.get("at"), str) and s.get("how") in WORDS] \
        if isinstance(said, list) else []


def record(data: Path, how: str, now: dt.datetime | None = None) -> None:
    """Note this start in starts.json (the last ``HISTORY``). Never raises."""
    now = now or dt.datetime.now(dt.timezone.utc)
    try:
        kept = _history(data)[-(HISTORY - 1):] + [{"at": now.isoformat(timespec="seconds"), "how": how}]
        durable.write_text(Path(data) / "starts.json", json.dumps(kept))
    except OSError:
        pass


def summary(data: Path, now: dt.datetime | None = None, days: int = 7) -> dict:
    """For the dashboard: this start ({"at", "how"}, or None), and how many of the starts in the last ``days`` days
    came after being switched off without shutting down (``off``) or after the display stopped (``crash``)."""
    now = now or dt.datetime.now(dt.timezone.utc)
    starts, off, crash = _history(data), 0, 0
    for s in starts:
        try:
            at = dt.datetime.fromisoformat(s["at"])
        except ValueError:
            continue
        if at.tzinfo and now - at <= dt.timedelta(days=days):
            off += s["how"] == "power"
            crash += s["how"] == "crash"
    return {"last": starts[-1] if starts else None, "off": off, "crash": crash, "days": days}


def download(data: Path, records, name: str = "Bongo Matrix", now: dt.datetime | None = None) -> str:
    """Logs > Download logs: one text file with what's kept on the card (earlier starts, power cuts and crashes
    included) and this start's log from memory (``records``: the dashboard's Logs page records)."""
    now = now or dt.datetime.now().astimezone()
    out = [f"Bongo Matrix logs: {name}, {now.strftime('%Y-%m-%d %H:%M %Z').strip()}", ""]
    info = summary(data, now.astimezone(dt.timezone.utc))
    if info["last"]:
        out.append(f"This start: {info['last']['at']}, {WORDS[info['last']['how']]}")
    out += [f"Last {info['days']} days: switched off without shutting down {info['off']} times, "
            f"the display stopped {info['crash']} times", "", "== Kept on the card (warnings and errors) =="]
    kept = ""
    for name_ in ("events.log.1", "events.log"):
        try:
            kept += (Path(data) / name_).read_text(encoding="utf-8", errors="replace")
        except OSError:
            pass
    out.append(kept.rstrip() or "(nothing yet)")
    out += ["", "== This start (everything since the display started) =="]
    for r in records:
        when = dt.datetime.fromtimestamp(r.get("t", 0)).strftime("%Y-%m-%d %H:%M:%S")
        out.append(f"{when} {r.get('level', '')} {r.get('source', '')}: {r.get('message', '')}")
        if r.get("trace"):
            out.append(str(r["trace"]).rstrip())
    return "\n".join(out) + "\n"
