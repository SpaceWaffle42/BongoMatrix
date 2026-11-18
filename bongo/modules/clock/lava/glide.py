"""Back to back: when a Lava screen follows another of the same style (one Lava screen alone in the
playlist, say), its colours glide across from the last one's over ``transition_seconds``, and the wax
carries on where it was instead of jumping. 0 seconds: a straight cut, as before."""
import copy

GLIDE = 2.5   # s, the default (``transition_seconds``)
MOST = 10.0


def ease(k: float) -> float:
    k = min(1.0, max(0.0, k))
    return k * k * (3 - 2 * k)


class Glides:
    """Mixed into the Lava styles: ``follow`` is called by the player with the screen just before."""
    BLOB_SETTINGS = ()   # the settings that shape the wax: the blobs carry over only if they match

    def follow(self, before) -> None:
        seconds = float(self.settings.get("transition_seconds", GLIDE) or 0)
        if type(before) is not type(self) or seconds <= 0:
            return
        self._glide = (before.colours_now(), seconds, None)   # (from, how long, when it started: its first frame)
        if all(self.settings.get(k) == before.settings.get(k) for k in self.BLOB_SETTINGS):
            self.carry(before)

    def glide(self, now, target):
        """How far into the glide (0 to 1, eased) at ``now``, and what to blend from (None: no glide)."""
        state = getattr(self, "_glide", None)
        if state is None:
            return 1.0, None
        start, seconds, began = state[0], state[1], state[2]
        t = now.timestamp()
        if began is None:
            self._glide = (start, seconds, t)
            began = t
        k = ease((t - began) / seconds)
        if k >= 1:
            self._glide = None
        return k, start

    def carry(self, before) -> None:
        self.blobs = copy.deepcopy(before.blobs)
