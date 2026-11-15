"""Scrolling text that is read through, for any module.

A line too long for its space scrolls. Two rules make it readable:
- it starts from its beginning when the screen comes on (not from wherever the clock happens to be);
- the screen isn't handed over until every line that scrolls has been read through (once, or as many
  times as the module asks): ``busy`` tells the player to keep the screen up a little longer
  (``bongo.player.OVERRUN`` at most).

Things that take turns on one screen (news stories, notices) use ``turns``: the screen starts on the one
after whichever was on last time, and stays until the one it is showing has had its time.

A style mixes in ``Scrolls`` before ``Style``: ``class Ticker(Scrolls, Style)``.
"""

HOLD = 2.0        # s a line is held still at its start before it moves (and a marquee at its end after)
RECENT = 2.0      # s: a line drawn longer ago than this has gone from the screen
LAST: dict = {}   # what was showing when each set of turns last moved on (``turns``), by its key


class Scrolls:
    def _note(self, key, start, need, t):
        self.__dict__.setdefault("_lines", {})[key] = (start, need, t)

    def scrolled(self, key, now, need: float) -> float:
        """Seconds since this line (``key``: its text and place) was first drawn on this screen.
        ``need``: how long it takes to be read through (as many times as wanted)."""
        t = now.timestamp()
        lines = self.__dict__.setdefault("_lines", {})
        start = lines[key][0] if key in lines else None
        if start is None or not 0 <= t - lines[key][2] < 60 or t < start:
            start = t                    # new, gone for a while, or the clock jumped: from the beginning
        self._note(key, start, need, t)
        return t - start

    def turns(self, key, items, now, seconds, name=str):
        """(item, seconds into it): ``items`` take turns, each for ``seconds(item)``. A screen starts on the
        one after whichever was showing when this ``key`` last moved on (on any screen), so every item gets
        its go, and stays until the one it is showing has had its time. ``name(item)`` tells items apart:
        if the list changes (a feed refreshes, a notice's hours end), the one on screen carries on."""
        t = now.timestamp()
        names = [name(i) for i in items]
        on = self.__dict__.setdefault("_turn", {}).get(key)       # (name of the one on, when it came on, last drawn)
        if on is None or on[0] not in names or not 0 <= t - on[2] < 60 or t < on[1]:
            last = LAST.get(key) if on is None else on[0]            # new screen (or a gap, a jump, the one on gone)
            first = (names.index(last) + 1) % len(names) if last in names else 0
            on = (names[first], t, t)
        current, start = on[0], on[1]
        ends = getattr(self, "hand_over_at", None)                   # (Style.hand_over_at: when the player moves on)
        for _ in range(len(names) * 4 + 4):                          # (a slow frame can pass more than one)
            span = max(0.1, float(seconds(items[names.index(current)])))
            if t - start < span:
                break
            if ends is not None and start + span >= ends - 3:        # its turn is over and the screen's time all but
                if not getattr(self.ctx, "preview", False):          # up: hand over now, rather than begin another
                    self.finished = True                             # (or hold this one at its end)
                break
            start = start + span if t - (start + span) < 1.0 else t  # (a turn that got shorter: the next from now)
            current = names[(names.index(current) + 1) % len(names)]
        else:
            start = t                                                # (far behind: carry on from here)
        item = items[names.index(current)]
        span = max(0.1, float(seconds(item)))
        self._turn[key] = (current, start, t)
        self._note(("turn", key), start, span, t)                     # (only the turn on now holds the screen)
        if not getattr(self.ctx, "preview", False):                  # (a dashboard preview doesn't move the panel on)
            LAST[key] = current
        return item, min(t - start, span - 1e-3)

    def line(self, img, font, x, baseline, width, text, ink, now, speed=14, gap=16, times=1, outline=None, key=None):
        """``text`` in a ``width``-px window from ``x``: still if it fits; otherwise it starts at its first letter,
        holds, then goes round (a repeat ``gap`` px behind) at ``speed`` px/s, and the screen waits until it
        has gone round ``times`` times. Drawn in ``ink`` (with an ``outline`` colour if given). ``key``: a new
        one starts it again from its first letter (each time a screen shows it afresh, say)."""
        tw = font.width(text)
        if tw <= width:
            font.draw(img, x, baseline, text, ink, **({"outline": outline} if outline else {}))
            return
        period = tw + gap
        t = self.scrolled(key or ("line", baseline, text), now, HOLD + period / speed * max(1, int(times)))
        off = int(max(0.0, t - HOLD) * speed) % period
        lane = img.crop((x, 0, x + width, img.height))
        for k in (0, 1):
            font.draw(lane, -off + k * period, baseline, text, ink, **({"outline": outline} if outline else {}))
        img.paste(lane, (x, 0))

    @property
    def busy(self) -> bool:
        """A line drawn just now hasn't been read through yet."""
        lines = self.__dict__.get("_lines")
        if not lines:
            return False
        t = self.ctx.now().timestamp()
        ends = getattr(self, "hand_over_at", None)                    # (Style.hand_over_at: anything begun after the screen's time
        return any(0 <= t - last < RECENT and t - start < need   # was up, such as the next story, doesn't hold it)
                   and (ends is None or start < ends) for start, need, last in lines.values())
