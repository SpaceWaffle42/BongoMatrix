"""Screen-specific media discovery and validated filesystem metadata."""

from __future__ import annotations

import copy
import math
import os
import random
import stat
import threading
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

from PIL import Image, ImageSequence, UnidentifiedImageError

from media_resolver import MediaResolutionError, MediaResolver


SCREENS = frozenset({"splash", "sleep"})
SCREEN_DEFAULTS = {
    "splash": "built-in/splash_loading_squares.gif",
    "sleep": "built-in/sleep.gif",
}
# Retained predecessor Splash default; never a choice for a new selection.
_LEGACY_SCREEN_MEDIA = {"splash": frozenset({"built-in/loading_squares.gif"})}
_STILL_EXTENSIONS = frozenset({".png", ".jpg", ".jpeg", ".webp"})
_SCREEN_EXTENSIONS = _STILL_EXTENSIONS | {".gif"}
_EXPECTED_FORMAT = {
    ".gif": "GIF",
    ".png": "PNG",
    ".jpg": "JPEG",
    ".jpeg": "JPEG",
    ".webp": "WEBP",
}


@dataclass(frozen=True)
class ScreenRepair:
    config: dict
    sources: tuple[str, ...]
    persisted: bool
    default_unavailable: bool = False


@dataclass(frozen=True)
class MediaInfo:
    reference: str
    identity: tuple[int, int, int, int, int]
    kind: str
    gif_duration_ms: int | None
    frame_delays_ms: tuple[int, ...] = ()


class ScreenMediaCatalog:
    """Thread-safe identity-aware metadata for usable screen media."""

    def __init__(self, media_root, *, saved_config=None):
        self._root = Path(media_root)
        self._saved_config = saved_config
        self._lock = threading.RLock()
        self._cache = {}

    @staticmethod
    def _identity(status):
        return (
            status.st_dev,
            status.st_ino,
            status.st_size,
            status.st_mtime_ns,
            status.st_ctime_ns,
        )

    @staticmethod
    def _read_info(reference, pinned, identity):
        suffix = Path(reference).suffix.lower()
        with pinned.dup_file() as source:
            with Image.open(source) as image:
                if image.format != _EXPECTED_FORMAT[suffix]:
                    raise ValueError("selected media format does not match its filename")
                if suffix == ".gif":
                    delays = []
                    for frame in ImageSequence.Iterator(image):
                        frame.load()
                        delays.append(int(frame.info.get("duration", 100)))
                    duration = sum(max(10, delay) for delay in delays)
                    return MediaInfo(
                        reference, identity, "gif", duration,
                        frame_delays_ms=tuple(delays),
                    )
                image.load()
                return MediaInfo(reference, identity, "still", None)

    def __call__(self, screen: str) -> dict[str, MediaInfo]:
        with self._lock:
            saved = self._saved_config() if self._saved_config is not None else None
            references = discover_screen_media(self._root, screen, saved_config=saved)
            resolver = MediaResolver(self._root)
            current = {}
            next_cache = {}
            try:
                for reference in references:
                    with resolver.resolve(reference) as pinned:
                        identity = self._identity(os.fstat(pinned.fileno()))
                        key = (reference, identity)
                        info = self._cache.get(key)
                        if info is None:
                            info = self._read_info(reference, pinned, identity)
                        current[reference] = info
                        next_cache[key] = info
            finally:
                resolver.close()
            self._cache = next_cache
            return current


def _screen_playback_rate(row):
    try:
        speed = row.get("layout", {}).get("playback_speed_percent", 100.0)
        if (
            isinstance(speed, bool)
            or not math.isfinite(speed)
            or not 10 <= speed <= 400
        ):
            return None
        rate = Fraction(str(speed)) / 100
    except (TypeError, ValueError, ZeroDivisionError):
        return None
    return rate


def _scaled_gif_duration_seconds(media, rate):
    if media.kind != "gif" or not media.frame_delays_ms or rate is None:
        return None
    try:
        delays = [Fraction(str(delay)) for delay in media.frame_delays_ms]
    except (TypeError, ValueError, ZeroDivisionError):
        return None
    if any(not math.isfinite(float(delay)) for delay in delays):
        return None
    duration = sum((max(Fraction(10), delay / rate) for delay in delays), Fraction())
    return duration if duration > 0 else None


def screen_gif_duration_seconds(row, media) -> float:
    """Return one scaled GIF pass, matching the physical renderer's delay floor."""
    duration = _scaled_gif_duration_seconds(media, _screen_playback_rate(row))
    if duration is None:
        return 7.0
    return float(duration / 1000)


def screen_still_duration_seconds(row, selected_media) -> float:
    """Derive a still hold from exact one-pass timings of selected usable GIFs."""
    rate = _screen_playback_rate(row)
    if rate is None:
        return 7.0

    durations = []
    seen = set()
    for media in selected_media:
        if media.reference in seen:
            continue
        seen.add(media.reference)
        duration = _scaled_gif_duration_seconds(media, rate)
        if duration is not None:
            durations.append(duration)
    if not durations:
        return 7.0
    mean = sum(durations, Fraction()) / len(durations)
    return float(mean / 1000) if Fraction(5000) <= mean <= Fraction(10000) else 7.0


def screen_preview_duration_seconds(
    config, screen, reference, pinned_media, *, selected_row=None, read_info=None,
) -> float:
    """Derive screen preview timing from pinned media without selection state."""
    read_info = read_info or ScreenMediaCatalog._read_info
    row = selected_row if (
        isinstance(selected_row, dict) and selected_row.get("module") == screen
    ) else next(
        (
            item for item in config.get("playlist", [])
            if isinstance(item, dict)
            and item.get("enabled") is True
            and item.get("module") == screen
        ),
        None,
    )
    if row is None:
        return 7.0
    media = []
    for source in row.get("sources", ()):
        pinned = pinned_media.get(source)
        if pinned is None:
            continue
        try:
            identity = ScreenMediaCatalog._identity(os.fstat(pinned.fileno()))
            media.append(read_info(source, pinned, identity))
        except (
            KeyError, OSError, SyntaxError, TypeError, ValueError,
            UnidentifiedImageError, Image.DecompressionBombError,
        ):
            continue
    selected = next((item for item in media if item.reference == reference), None)
    if selected is None:
        return 7.0
    if selected.kind == "gif":
        return screen_gif_duration_seconds(row, selected)
    return screen_still_duration_seconds(row, media)


class ScreenMediaRuntime:
    """Own thread-safe Splash/Sleep selection state for one controller."""

    def __init__(self, *, catalog, shuffle=random.shuffle, choice=random.choice):
        self._catalog = catalog
        self._shuffle = shuffle
        self._choice = choice
        self._lock = threading.RLock()
        self._splash_states = {}
        self._retired_splash = {}
        self._last_splash_row_key = None
        self._retired_sleep = {}
        self._last_sleep_row_key = None
        self._sleep_active = False
        self._sleep_current = {}

    @staticmethod
    def _row(config, screen, row=None):
        if isinstance(row, dict) and row.get("module") == screen:
            return row
        return next(
            (
                item for item in config.get("playlist", [])
                if isinstance(item, dict) and item.get("module") == screen
            ),
            None,
        )

    @staticmethod
    def _row_keys(config, screen):
        return {
            item.get("id") if isinstance(item.get("id"), str) else screen
            for item in config.get("playlist", [])
            if isinstance(item, dict) and item.get("module") == screen
        }

    def _prune_removed_rows(self, config, screen):
        current = self._row_keys(config, screen)
        if screen == "splash":
            self._splash_states = {
                row_key: state for row_key, state in self._splash_states.items()
                if row_key in current
            }
            self._retired_splash = {
                row_key: retired for row_key, retired in self._retired_splash.items()
                if row_key in current
            }
            if self._last_splash_row_key not in current:
                self._last_splash_row_key = None
        else:
            self._sleep_current = {
                row_key: media for row_key, media in self._sleep_current.items()
                if row_key in current
            }
            self._retired_sleep = {
                row_key: retired for row_key, retired in self._retired_sleep.items()
                if row_key in current
            }
            if self._last_sleep_row_key not in current:
                self._last_sleep_row_key = None

    def _effective(self, config, screen, row=None):
        row = self._row(config, screen, row)
        if not row or row.get("enabled") is not True:
            return []
        catalog = self._catalog(screen)
        row_key = row.get("id") if isinstance(row.get("id"), str) else screen
        if screen == "splash":
            current_identities = {
                (item.reference, item.identity) for item in catalog.values()
            }
            retired = self._retired_splash.setdefault(row_key, set())
            retired.intersection_update(current_identities)
        elif screen == "sleep":
            current_identities = {
                (item.reference, item.identity) for item in catalog.values()
            }
            retired = self._retired_sleep.setdefault(row_key, set())
            retired.intersection_update(current_identities)
        return [
            catalog[reference] for reference in row.get("sources", ())
            if reference in catalog and (
                (
                    screen != "splash"
                    or (reference, catalog[reference].identity) not in retired
                ) and (
                    screen != "sleep"
                    or (reference, catalog[reference].identity) not in retired
                )
            )
        ]

    def choose_splash(self, config, row=None) -> MediaInfo | None:
        with self._lock:
            self._prune_removed_rows(config, "splash")
            row = self._row(config, "splash", row)
            row_key = (
                row.get("id") if isinstance(row, dict) and isinstance(row.get("id"), str)
                else "splash"
            )
            self._last_splash_row_key = row_key
            effective = self._effective(config, "splash", row)
            key = tuple((item.reference, item.identity) for item in effective)
            state = self._splash_states.get(row_key)
            bag = state[1] if state is not None and state[0] == key else []
            if not effective:
                self._splash_states[row_key] = (key, bag)
                return None
            if not bag:
                bag = [item.reference for item in effective]
                self._shuffle(bag)
            reference = bag.pop()
            self._splash_states[row_key] = (key, bag)
            return next(item for item in effective if item.reference == reference)

    def retire_splash(self, media: MediaInfo | None, row=None) -> None:
        if media is None:
            return
        with self._lock:
            row_key = (
                row.get("id") if isinstance(row, dict) and isinstance(row.get("id"), str)
                else self._last_splash_row_key or "splash"
            )
            self._retired_splash.setdefault(row_key, set()).add(
                (media.reference, media.identity)
            )

    def restore_splash(self, media: MediaInfo | None, row=None) -> None:
        """Return an interrupted selection to its unchanged row-local bag."""
        if media is None:
            return
        with self._lock:
            row_key = (
                row.get("id") if isinstance(row, dict) and isinstance(row.get("id"), str)
                else self._last_splash_row_key or "splash"
            )
            state = self._splash_states.get(row_key)
            if state is not None and media.reference not in state[1]:
                state[1].append(media.reference)

    def _select_sleep(self, config, row=None) -> MediaInfo | None:
        self._prune_removed_rows(config, "sleep")
        row = self._row(config, "sleep", row)
        row_key = (
            row.get("id") if isinstance(row, dict) and isinstance(row.get("id"), str)
            else "sleep"
        )
        self._last_sleep_row_key = row_key
        effective = self._effective(config, "sleep", row)
        current = self._sleep_current.get(row_key)
        if current is not None:
            for item in effective:
                if (
                    item.reference == current.reference
                    and item.identity == current.identity
                ):
                    return current
        current = self._choice(effective) if effective else None
        if current is None:
            self._sleep_current.pop(row_key, None)
        else:
            self._sleep_current[row_key] = current
        return current

    def begin_sleep(self, config, row=None) -> MediaInfo | None:
        with self._lock:
            self._sleep_active = True
            self._sleep_current.clear()
            return self._select_sleep(config, row)

    def current_sleep(self, config, row=None) -> MediaInfo | None:
        with self._lock:
            if not self._sleep_active:
                return None
            return self._select_sleep(config, row)

    def end_sleep(self) -> None:
        with self._lock:
            self._sleep_active = False
            self._sleep_current.clear()

    def retire_sleep(self, media: MediaInfo | None, row=None) -> None:
        if media is None:
            return
        with self._lock:
            row_key = (
                row.get("id") if isinstance(row, dict) and isinstance(row.get("id"), str)
                else self._last_sleep_row_key or "sleep"
            )
            self._retired_sleep.setdefault(row_key, set()).add(
                (media.reference, media.identity)
            )
            if self._sleep_current.get(row_key) == media:
                self._sleep_current.pop(row_key, None)

    def duration(self, config, screen: str, media: MediaInfo, row=None) -> float:
        """Return one natural GIF pass or this screen's selected-GIF still hold."""
        with self._lock:
            row = self._row(config, screen, row)
            if row is None:
                return 7.0
            if media.kind == "gif":
                return screen_gif_duration_seconds(row, media)
            return screen_still_duration_seconds(
                row, self._effective(config, screen, row),
            )


def _screen_name(screen: str) -> str:
    if screen not in SCREENS:
        raise ValueError("screen must be splash or sleep")
    return screen


def _regular_names(directory: Path):
    try:
        entries = tuple(directory.iterdir())
    except (FileNotFoundError, NotADirectoryError, PermissionError):
        return ()
    names = []
    for path in entries:
        try:
            status = path.lstat()
        except OSError:
            continue
        if stat.S_ISREG(status.st_mode):
            names.append(path.name)
    return tuple(names)


def _decodes(resolver: MediaResolver, reference: str) -> bool:
    try:
        with resolver.resolve(reference) as pinned:
            with pinned.dup_file() as source:
                with Image.open(source) as image:
                    suffix = Path(reference).suffix.lower()
                    if image.format != _EXPECTED_FORMAT[suffix]:
                        return False
                    if suffix == ".gif":
                        for frame in ImageSequence.Iterator(image):
                            frame.load()
                    else:
                        image.load()
        return True
    except (
        KeyError,
        OSError,
        SyntaxError,
        ValueError,
        UnidentifiedImageError,
        Image.DecompressionBombError,
        MediaResolutionError,
    ):
        return False


def _saved_legacy_media(config, screen, *, row_id=None):
    """Only the finite predecessor default already selected by a saved row."""
    allowed = _LEGACY_SCREEN_MEDIA.get(screen, ())
    selected = set()
    for row in (config or {}).get("playlist", []):
        if not isinstance(row, dict) or row.get("module") != screen:
            continue
        if row_id is not None and row.get("id") != row_id:
            continue
        sources = row.get("sources", [row.get("source")])
        if isinstance(sources, list):
            selected.update(reference for reference in allowed if reference in sources)
    return selected


def discover_screen_media(media_root, screen: str, *, saved_config=None) -> tuple[str, ...]:
    """Return canonical decodable choices for one screen in stable order."""
    screen = _screen_name(screen)
    root = Path(media_root)
    references = list(_saved_legacy_media(saved_config, screen))
    for name in _regular_names(root / "built-in"):
        if name.startswith(screen) and Path(name).suffix.lower() == ".gif":
            references.append(f"built-in/{name}")
    for name in _regular_names(root):
        if Path(name).suffix.lower() in _SCREEN_EXTENSIONS:
            references.append(name)

    resolver = MediaResolver(root)
    try:
        return tuple(
            reference for reference in sorted(set(references))
            if _decodes(resolver, reference)
        )
    finally:
        resolver.close()


def validate_screen_media_config(config, media_root, *, saved_config=None) -> dict[str, tuple[str, ...]]:
    """Validate every persisted screen selection against current usable media."""
    validated = {}
    playlist = config.get("playlist", [])
    for screen in sorted(SCREENS):
        rows = [
            item for item in playlist
            if isinstance(item, dict) and item.get("module") == screen
        ]
        if not rows:
            raise ValueError(f"playlist must contain at least one {screen} screen")
        available = set(discover_screen_media(media_root, screen, saved_config=saved_config))
        legacy = _LEGACY_SCREEN_MEDIA.get(screen, ())
        selected = []
        for row in rows:
            sources = row.get("sources")
            if not isinstance(sources, list):
                raise ValueError(f"{screen} screen requires usable selected media")
            if any(reference not in available for reference in sources):
                raise ValueError(f"{screen} screen requires usable selected media")
            retained = _saved_legacy_media(saved_config, screen, row_id=row.get("id"))
            if any(reference in legacy and reference not in retained for reference in sources):
                raise ValueError(f"{screen} screen requires usable selected media")
            if row.get("enabled") is True and not sources:
                raise ValueError(f"enabled {screen} screen requires usable selected media")
            selected.extend(sources)
        validated[screen] = tuple(dict.fromkeys(selected))
    return validated


def repair_saved_screen(
    config_store, config, screen: str, media_root, *, row_id=None,
) -> ScreenRepair:
    """Filter one stale saved selection and persist a revision-bound repair once."""
    from config_store import ConfigConflictError

    screen = _screen_name(screen)
    available = set(discover_screen_media(media_root, screen, saved_config=config))
    playlist = copy.deepcopy(config.get("playlist", []))
    row = next(
        (
            item for item in playlist
            if isinstance(item, dict) and item.get("module") == screen
            and (row_id is None or item.get("id") == row_id)
        ),
        None,
    )
    if row is None:
        return ScreenRepair(copy.deepcopy(config), (), False, True)
    original = tuple(row.get("sources", ()))
    sources = tuple(reference for reference in original if reference in available)
    default_unavailable = False
    if row.get("enabled") is True and not sources:
        default = SCREEN_DEFAULTS[screen]
        if default in available:
            sources = (default,)
        else:
            default_unavailable = True
    if sources == original:
        return ScreenRepair(copy.deepcopy(config), sources, False, default_unavailable)
    if default_unavailable:
        return ScreenRepair(copy.deepcopy(config), (), False, True)

    row["sources"] = list(sources)
    row["source"] = sources[0] if sources else ""
    try:
        updated = config_store.update(
            {"playlist": playlist},
            base_revision=config.get("revision"),
            client_contract=4,
        )
    except ConfigConflictError:
        latest = config_store.get()
        latest_row = next(
            (
                item for item in latest.get("playlist", [])
                if isinstance(item, dict) and item.get("module") == screen
                and (row_id is None or item.get("id") == row_id)
            ),
            {},
        )
        latest_sources = tuple(
            reference for reference in latest_row.get("sources", ())
            if reference in available
        )
        return ScreenRepair(latest, latest_sources, False)
    return ScreenRepair(updated, sources, True)
