"""Preview on the panel: the screen editor's unsaved draft, held on the real panel for a while.

The hold lives here, on the Matrix, not in the page: the page renews it every few seconds, and it ends by
itself when the renewals stop (the tab closed, the phone locked, the network went) or when nothing has
been changed for a while, so a draft is never left on the panel.

It is a covering screen, like the pairing PIN (``Live.overlay``): the playlist carries on underneath, with
its own timing, and the player draws the draft over each frame (``cover``). So nothing is preempted and
nothing can be left stuck: when the hold ends, the next frame is simply the playlist's again. Unlike the PIN
it never lifts the brightness, and the picture the web shows (``Live.publish``'s ``public`` frame: the hub,
and the dashboard's Now page) stays the playlist's, because a draft can hold private text.

A draft is unsaved and may be slow or broken, so it is never drawn on the player's thread: each hold has a
worker thread of its own that draws the draft's frames, and the player takes the latest finished one, waiting
no longer than a frame for it. A frame that takes too long (``DRAW_MOST``) ends the hold, and the worker is
abandoned (whatever it draws later is thrown away), so the playlist underneath never loses a frame to it.

One browser previews at a time. Nothing about a preview is ever written to the config.
"""
from __future__ import annotations

import collections
import logging
import secrets
import threading
import time
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

LAPSE = 30.0    # s without a renewal from the page before the hold ends
IDLE = 300.0    # s without a change to the draft before it ends, renewed or not
RENEW = 10.0    # s between the page's renewals: two in a row can be lost before the hold lapses
DRAW_MOST = 1.0  # s one frame of the draft may take to draw before the hold ends ("took too long")
WAIT_MOST = 0.05  # s the player waits at most for a draft frame it asked for (then it shows the last one)
STUCK_MOST = 3   # abandoned workers still stuck in a draw: with this many, no new preview starts

# why a hold ended, in the editor's words (``ended``: the page that had it asks, and is told)
REASONS = {
    "off": "The preview was switched off.",
    "taken": "Another browser took over the preview.",
    "left": "The preview ended: this page was out of touch with the Matrix for 30 seconds.",
    "idle": "The preview ended after 5 minutes without a change.",
    "saved": "The preview ended: the Matrix's settings were saved.",
    "failed": "The preview ended: this screen failed on the panel. See Logs.",
    "slow": "The preview ended: this screen took too long to draw on the panel.",
    "unpaired": "The preview ended: this browser is no longer paired.",
    "gone": "The preview ended: the Matrix was restarted.",
}


class Held(Exception):
    """Someone else has the panel: ``name`` is their browser's, ``same`` whether it's this very browser (another tab)."""

    def __init__(self, name: str, same: bool):
        super().__init__("This browser is already previewing on the panel in another tab" if same
                         else "Someone else is previewing on the panel")
        self.name, self.same = name, same


class NotYours(Exception):
    """The hold named is another browser's."""

    def __init__(self):
        super().__init__("That's not your preview: it was started in another browser.")


class Stuck(Exception):
    """Earlier drafts are still stuck drawing (``STUCK_MOST``): no new preview until one of them finishes (most do
    within a few seconds; one that never does needs a restart)."""

    def __init__(self):
        super().__init__("Earlier previews are still drawing a slow screen. Try again in a few seconds; if this keeps "
                         "happening, restart the Matrix (Settings > Device).")


class Ended(Exception):
    """That hold is over (or never was): ``reason`` is a key of ``REASONS``."""

    def __init__(self, reason: str, message: str = ""):
        super().__init__(message or REASONS.get(reason, REASONS["gone"]))
        self.reason = reason


@dataclass
class Hold:
    token: str               # the page's own: every later call must carry it
    who: str                 # the paired browser it belongs to (bongo.pairing: a device's id)
    name: str                # ...and its name, for the Now page
    style: object            # the draft, built and ready to draw (bongo.module.Style)
    module: str = ""
    look: str | None = None
    renewed: float = 0.0     # when the page last said it's still there
    changed: float = 0.0     # when the draft last changed
    frames: int = 0          # frames of it the player has drawn (0: it hasn't reached the panel yet)
    gave_up: object = None   # callable(style) -> a reason if the screen has stopped for good (an imported module's child died)
    last: object = field(default=None, repr=False)   # its latest frame: it is drawn at its own rate, whatever the screen underneath's
    last_at: float = -1e9
    # the worker that draws it (``_work``): asked for a frame (``want``), it leaves the result in ``result`` and
    # sets ``ready``; ``asked`` is when the frame it is drawing was asked for (None: it isn't drawing one)
    want: threading.Event = field(default_factory=threading.Event, repr=False)
    ready: threading.Event = field(default_factory=threading.Event, repr=False)
    done: threading.Event = field(default_factory=threading.Event, repr=False)
    result: tuple | None = field(default=None, repr=False)
    asked: float | None = None
    worker: threading.Thread | None = field(default=None, repr=False)


class PanelPreview:
    def __init__(self, clock=time.monotonic, on_end=None):
        self.clock = clock              # for the hold's own times (a test moves it); frames are paced by the real one
        self.on_end = on_end            # called (with no lock held) each time a hold ends
        self.lapse, self.idle, self.renew_every = LAPSE, IDLE, RENEW
        self._lock = threading.Lock()   # held for a moment at a time, never while a screen is built or drawn
        self._hold: Hold | None = None
        self._ended: collections.OrderedDict = collections.OrderedDict()   # token -> (reason, message), the last few
        self._stuck: list[threading.Thread] = []   # workers abandoned in the middle of a draw that never came back

    # -- the dashboard's side ----------------------------------------------------------------
    def start(self, who: str, name: str, style, module: str = "", look: str | None = None, take_over: bool = False,
              gave_up=None) -> str:
        """Hold the panel for a page of browser ``who`` with ``style`` on it. Someone else's hold stands
        (``Held``) unless ``take_over``. Returns the hold's token."""
        with self._lock:
            ended = self._expire()
            if self._hold is not None:
                if not take_over:
                    raise Held(self._hold.name, self._hold.who == who)
            self._stuck[:] = [t for t in self._stuck if t.is_alive()]
            if len(self._stuck) >= STUCK_MOST:
                raise Stuck()
            if self._hold is not None:
                ended = self._finish(self._hold, "taken")
            now = self.clock()
            hold = self._hold = Hold(secrets.token_urlsafe(16), who, name, style, module, look, now, now, gave_up=gave_up)
            hold.worker = threading.Thread(target=_work, args=(hold,), daemon=True, name="panel-preview")
            hold.worker.start()
        self._tell(ended)
        return hold.token

    def change(self, token: str, style, module: str = "", look: str | None = None, gave_up=None, who: str | None = None) -> dict:
        """A new draft for the hold ``token`` (``Ended`` if it's over, ``NotYours`` if ``who`` isn't its browser)."""
        def swap(hold):
            hold.style, hold.module, hold.look, hold.gave_up = style, module, look, gave_up
            hold.last_at = -1e9                       # drawn at once, whatever the last one's rate
            hold.renewed = hold.changed = self.clock()
        return self._with(token, swap, who)

    def renew(self, token: str, changed: bool = False, who: str | None = None) -> dict:
        """The page is still there (``Ended`` if its hold is over). ``changed``: and something was changed just
        now, though it couldn't be shown (a draft that isn't valid yet), so the five minutes start again."""
        def still(hold):
            hold.renewed = self.clock()
            if changed:
                hold.changed = hold.renewed
        return self._with(token, still, who)

    def stop(self, token: str, who: str | None = None) -> bool:
        """The page gives the panel back (the switch, Save, Cancel). True if it had it; ``NotYours`` if the hold
        ``token`` is another browser's."""
        with self._lock:
            ended = self._expire()
            mine = self._hold is not None and self._hold.token == token
            if mine and who is not None and self._hold.who != who:
                self._tell_later(ended)
                raise NotYours()
            if mine:
                ended = self._finish(self._hold, "off")
        self._tell(ended)
        return mine

    def unpaired(self, who: str) -> bool:
        """Browser ``who`` was unpaired (Settings > Devices): its hold, if it has one, ends now."""
        with self._lock:
            ended = self._expire()
            if self._hold is not None and self._hold.who == who:
                ended = self._finish(self._hold, "unpaired")
        self._tell(ended)
        return bool(ended and ended[0] == "unpaired")

    def end(self, reason: str, message: str = "") -> bool:
        """End whoever's hold it is (a config save: the screens are about to be built afresh)."""
        with self._lock:
            ended = self._expire() or (self._finish(self._hold, reason, message) if self._hold else None)
        self._tell(ended)
        return ended is not None

    @property
    def active(self) -> bool:
        if self._hold is None:
            return False
        with self._lock:
            ended = self._expire()
            on = self._hold is not None
        self._tell(ended)
        return on

    def public(self) -> dict | None:
        """Who is previewing what, for the dashboard's own pages (never the hub): no settings, no text."""
        if self._hold is None:
            return None
        with self._lock:
            ended = self._expire()
            hold = self._hold
            out = None if hold is None else {"module": hold.module, "style": hold.look, "by": hold.name,
                                             "on_panel": hold.frames > 0}
        self._tell(ended)
        return out

    def ended(self, token: str) -> tuple[str, str]:
        """Why the hold ``token`` ended, as (reason, the words for it)."""
        with self._lock:
            return self._ended.get(token, ("gone", REASONS["gone"]))

    # -- the player's side -------------------------------------------------------------------
    @property
    def pace(self) -> float:
        """Seconds between the draft's frames (the player waits no longer than this while it covers)."""
        hold = self._hold
        return _frame_time(hold.style) if hold else 1.0

    def cover(self, frame, wait: float = WAIT_MOST):
        """``frame`` if nothing is held, else the draft's latest frame. Called by the player for every frame, so it
        does next to nothing with no hold. It never draws the draft itself (``_work`` does) and never waits more
        than ``wait`` (at most ``WAIT_MOST``) for a frame it asks for: a slow or hung draft can't hold up the panel.
        A draft that fails, or is too slow, ends the hold (logged once) and the playlist's frame comes straight back."""
        hold = self._hold
        if hold is None:
            return frame
        with self._lock:
            ended = self._expire()
            hold = self._hold
        if ended:
            self._tell(ended)
        if hold is None:
            return frame
        if hold.ready.is_set() and not self._take(hold, frame):
            return frame
        now = time.monotonic()
        if hold.asked is None and now - hold.last_at >= _frame_time(hold.style) - 1e-3:
            hold.ready.clear()
            hold.asked = now
            hold.want.set()                     # the worker draws the next frame...
            if hold.ready.wait(max(0.0, min(wait, WAIT_MOST))) and not self._take(hold, frame):   # (...usually at once)
                return frame
        if hold.last is None:
            return frame                        # (its first frame isn't drawn yet: a moment of the playlist)
        hold.frames += 1
        return hold.last

    def _take(self, hold: Hold, frame) -> bool:
        """Use the frame the worker finished. False if that ended the hold (it failed, or isn't a frame)."""
        result, hold.result = hold.result, None
        hold.ready.clear()
        if result is None:
            return True
        style, image, error, why, asked = result
        if style is not hold.style:             # (a draft swapped in meanwhile: that one is drawn next)
            return True
        if error is None and getattr(image, "size", None) != frame.size:
            error = ValueError(f"a frame of the wrong size ({getattr(image, 'size', None)}, not {frame.size})")
        if error is not None:
            log.error("preview on the panel: %s/%s failed; back to the playlist", hold.module, hold.look, exc_info=error)
            self._fail(hold, REASONS["failed"])
            return False
        if why:
            log.warning("preview on the panel: %s/%s stopped (%s); back to the playlist", hold.module, hold.look, why)
            self._fail(hold, f"The preview ended: {why}")
            return False
        hold.last, hold.last_at = (image if image.mode == "RGB" else image.convert("RGB")), asked
        return True

    # -- inside ------------------------------------------------------------------------------
    def _with(self, token: str, do, who: str | None = None) -> dict:
        """``do(hold)`` on the hold ``token`` and how it stands after; ``Ended``, with why, if it isn't the one held;
        ``NotYours`` if ``who`` (when given) isn't the browser that started it."""
        with self._lock:
            ended = self._expire()
            hold = self._hold if self._hold is not None and token and self._hold.token == token else None
            if hold is not None and who is not None and hold.who != who:
                self._tell_later(ended)
                raise NotYours()
            if hold is not None:
                do(hold)
                out = self._describe(hold)
        self._tell(ended)
        if hold is None:
            raise Ended(*self.ended(token))
        return out

    def _expire(self):
        """End a hold whose page has gone quiet, or that nothing has changed for too long. With the lock held;
        returns what ``_tell`` needs (None: nothing ended)."""
        hold = self._hold
        if hold is None:
            return None
        asked = hold.asked                      # (by the real clock, like the frames: a test's clock never draws)
        if asked is not None and time.monotonic() - asked > DRAW_MOST:
            return self._finish(hold, "slow")
        now = self.clock()
        if now - hold.renewed >= self.lapse:
            return self._finish(hold, "left")
        if now - hold.changed >= self.idle:
            return self._finish(hold, "idle")
        return None

    def _finish(self, hold: Hold, reason: str, message: str = ""):
        """With the lock held: the hold is over, and its page can ask why."""
        if self._hold is hold:
            self._hold = None
        self._ended[hold.token] = (reason, message or REASONS.get(reason, REASONS["gone"]))
        while len(self._ended) > 8:
            self._ended.popitem(last=False)
        hold.last = None
        hold.done.set()                         # its worker stops; one in the middle of a draw is abandoned, and
        hold.want.set()                         # what it draws is thrown away
        if hold.asked is not None and hold.worker is not None:
            self._stuck.append(hold.worker)
        return (reason, hold)

    def _fail(self, hold: Hold, message: str) -> None:
        with self._lock:
            ended = self._finish(hold, "failed", message) if self._hold is hold else None
        self._tell(ended)

    def _tell(self, ended) -> None:
        """Say that a hold ended (``on_end``). Never called with the lock held."""
        if not ended:
            return
        reason, hold = ended
        if reason == "slow":
            log.warning("preview on the panel: %s/%s took over %g s to draw a frame; back to the playlist",
                        hold.module, hold.look, DRAW_MOST)
        if reason in ("taken", "left", "idle", "unpaired"):
            log.info("preview on the panel ended (%s): %s/%s, from %s", reason, hold.module, hold.look, hold.name)
        if not self.on_end:
            return
        try:
            self.on_end()
        except Exception:   # the player must never trip over this
            log.exception("preview on the panel: on_end failed")

    def _tell_later(self, ended) -> None:
        """``_tell`` for a hold that ended while the lock is held and an exception is on its way out."""
        if ended:
            threading.Thread(target=self._tell, args=(ended,), daemon=True, name="preview-ended").start()

    def _describe(self, hold: Hold) -> dict:
        now = self.clock()
        return {"idle_left": round(max(0.0, self.idle - (now - hold.changed)), 1), "on_panel": hold.frames > 0,
                "renew": self.renew_every, "lapse": self.lapse, "idle": self.idle}


def _frame_time(style) -> float:
    try:
        return 1 / max(1.0, min(60.0, float(style.fps)))
    except (TypeError, ValueError, AttributeError):
        return 1 / 20


def _work(hold: Hold) -> None:
    """A hold's worker: draws the draft's next frame each time the player asks, until the hold ends. A frame
    finished after that (an abandoned draw) is thrown away."""
    while True:
        hold.want.wait()
        hold.want.clear()
        if hold.done.is_set():
            return
        style, asked = hold.style, hold.asked
        image = error = why = None
        try:
            image = style.render(style.ctx.now())
            why = hold.gave_up(style) if hold.gave_up else None
        except Exception as problem:   # (said by the player's side, once: ``PanelPreview._take``)
            error = problem
        if hold.done.is_set():
            return
        hold.result = (style, image, error, why, asked if asked is not None else time.monotonic())
        hold.asked = None
        hold.ready.set()
