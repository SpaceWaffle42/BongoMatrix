"""Plays a playlist: each entry is one module + style + settings for N seconds."""
from __future__ import annotations

import itertools
import logging
import threading
import time
from dataclasses import dataclass, field

from bongo.module import Context, Module, Style

log = logging.getLogger(__name__)


@dataclass
class Entry:
    module: str
    style: str | None = None
    seconds: float = 30
    settings: dict = field(default_factory=dict)
    enabled: bool = True

    def build(self, modules: dict[str, Module], ctx: Context) -> Style:
        if self.module not in modules:
            raise ValueError(f"unknown module {self.module!r}")
        return modules[self.module].create(self.style, self.settings, ctx)


def play_entry(style: Style, seconds: float, panel, stop: threading.Event | None = None,
               clock=time.monotonic, place=None) -> int:
    """Render one entry until its time is up; returns frames shown.
    ``place`` puts the module's safe-area frame onto the full panel (see SafeArea.place)."""
    stop = stop or threading.Event()
    frame_time = 1 / style.fps
    end = clock() + seconds
    shown = 0
    while clock() < end and not stop.is_set():
        started = clock()
        frame = style.render(style.ctx.now())
        panel.show(place(frame) if place else frame)
        shown += 1
        stop.wait(max(0.0, frame_time - (clock() - started)))
    return shown


def play(playlist: list[Entry], modules: dict[str, Module], panel, ctx_factory=Context,
         stop: threading.Event | None = None, loops: int | None = None, place=None) -> None:
    stop = stop or threading.Event()
    active = [e for e in playlist if e.enabled]
    if not active:
        raise ValueError("playlist has no enabled entries")
    cycle = itertools.cycle(active) if loops is None else itertools.chain.from_iterable([active] * loops)
    for entry in cycle:
        if stop.is_set():
            return
        try:
            style = entry.build(modules, ctx_factory())
        except Exception:
            log.exception("skipping playlist entry %s", entry.module)
            continue
        play_entry(style, entry.seconds, panel, stop, place=place)
