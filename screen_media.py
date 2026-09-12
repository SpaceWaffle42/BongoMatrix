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

    def __init__(self, media_root):
        self._root = Path(media_root)
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
            references = discover_screen_media(self._root, screen)
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


def screen_still_duration_seconds(row, selected_media) -> float:
    """Derive a still hold from exact one-pass timings of selected usable GIFs."""
    try:
        speed = row.get("layout", {}).get("playback_speed_percent", 100.0)
        if isinstance(speed, bool) or not math.isfinite(speed) or speed <= 0:
            return 7.0
        rate = Fraction(str(speed)) / 100
    except (TypeError, ValueError, ZeroDivisionError):
        return 7.0

    durations = []
    seen = set()
    for media in selected_media:
        if media.reference in seen or media.kind != "gif" or not media.frame_delays_ms:
            continue
        seen.add(media.reference)
        try:
            delays = [Fraction(str(delay)) for delay in media.frame_delays_ms]
        except (TypeError, ValueError, ZeroDivisionError):
            continue
        if any(not math.isfinite(float(delay)) for delay in delays):
            continue
        duration = sum((max(Fraction(10), delay / rate) for delay in delays), Fraction())
        if duration > 0:
            durations.append(duration)
    if not durations:
        return 7.0
    mean = sum(durations, Fraction()) / len(durations)
    return float(mean / 1000) if Fraction(5000) <= mean <= Fraction(10000) else 7.0


class ScreenMediaRuntime:
    """Own thread-safe Splash/Sleep selection state for one controller."""

    def __init__(self, *, catalog, shuffle=random.shuffle, choice=random.choice):
        self._catalog = catalog
        self._shuffle = shuffle
        self._choice = choice
        self._lock = threading.RLock()
        self._splash_key = ()
        self._splash_bag = []
        self._retired_splash = set()
        self._retired_sleep = set()
        self._sleep_active = False
        self._sleep_current = None

    @staticmethod
    def _row(config, screen):
        return next(
            (
                item for item in config.get("playlist", [])
                if isinstance(item, dict) and item.get("module") == screen
            ),
            None,
        )

    def _effective(self, config, screen):
        row = self._row(config, screen)
        if not row or row.get("enabled") is not True:
            return []
        catalog = self._catalog(screen)
        if screen == "splash":
            current_identities = {
                (item.reference, item.identity) for item in catalog.values()
            }
            self._retired_splash.intersection_update(current_identities)
        elif screen == "sleep":
            current_identities = {
                (item.reference, item.identity) for item in catalog.values()
            }
            self._retired_sleep.intersection_update(current_identities)
        return [
            catalog[reference] for reference in row.get("sources", ())
            if reference in catalog and (
                (
                    screen != "splash"
                    or (reference, catalog[reference].identity) not in self._retired_splash
                ) and (
                    screen != "sleep"
                    or (reference, catalog[reference].identity) not in self._retired_sleep
                )
            )
        ]

    def choose_splash(self, config) -> MediaInfo | None:
        with self._lock:
            effective = self._effective(config, "splash")
            key = tuple((item.reference, item.identity) for item in effective)
            if key != self._splash_key:
                self._splash_key = key
                self._splash_bag.clear()
            if not effective:
                return None
            if not self._splash_bag:
                self._splash_bag = [item.reference for item in effective]
                self._shuffle(self._splash_bag)
            reference = self._splash_bag.pop()
            return next(item for item in effective if item.reference == reference)

    def retire_splash(self, media: MediaInfo | None) -> None:
        if media is None:
            return
        with self._lock:
            self._retired_splash.add((media.reference, media.identity))

    def _select_sleep(self, config) -> MediaInfo | None:
        effective = self._effective(config, "sleep")
        if self._sleep_current is not None:
            for item in effective:
                if (
                    item.reference == self._sleep_current.reference
                    and item.identity == self._sleep_current.identity
                ):
                    return self._sleep_current
        self._sleep_current = self._choice(effective) if effective else None
        return self._sleep_current

    def begin_sleep(self, config) -> MediaInfo | None:
        with self._lock:
            self._sleep_active = True
            self._sleep_current = None
            return self._select_sleep(config)

    def current_sleep(self, config) -> MediaInfo | None:
        with self._lock:
            if not self._sleep_active:
                return None
            return self._select_sleep(config)

    def end_sleep(self) -> None:
        with self._lock:
            self._sleep_active = False
            self._sleep_current = None

    def retire_sleep(self, media: MediaInfo | None) -> None:
        if media is None:
            return
        with self._lock:
            self._retired_sleep.add((media.reference, media.identity))
            if self._sleep_current == media:
                self._sleep_current = None

    def duration(self, config, screen: str, media: MediaInfo) -> float:
        """Return the configured GIF hold or this screen's selected-GIF still hold."""
        with self._lock:
            row = self._row(config, screen)
            if row is None:
                return 7.0
            if media.kind == "gif":
                return row["duration"]
            return screen_still_duration_seconds(row, self._effective(config, screen))


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


def discover_screen_media(media_root, screen: str) -> tuple[str, ...]:
    """Return canonical decodable choices for one screen in stable order."""
    screen = _screen_name(screen)
    root = Path(media_root)
    references = []
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


def validate_screen_media_config(config, media_root) -> dict[str, tuple[str, ...]]:
    """Validate every persisted screen selection against current usable media."""
    validated = {}
    playlist = config.get("playlist", [])
    for screen in sorted(SCREENS):
        rows = [
            item for item in playlist
            if isinstance(item, dict) and item.get("module") == screen
        ]
        if len(rows) != 1:
            raise ValueError(f"playlist must contain exactly one {screen} screen")
        row = rows[0]
        sources = row.get("sources")
        if not isinstance(sources, list):
            raise ValueError(f"{screen} screen requires usable selected media")
        available = set(discover_screen_media(media_root, screen))
        if any(reference not in available for reference in sources):
            raise ValueError(f"{screen} screen requires usable selected media")
        if row.get("enabled") is True and not sources:
            raise ValueError(f"enabled {screen} screen requires usable selected media")
        validated[screen] = tuple(sources)
    return validated


def repair_saved_screen(config_store, config, screen: str, media_root) -> ScreenRepair:
    """Filter one stale saved selection and persist a revision-bound repair once."""
    from config_store import ConfigConflictError

    screen = _screen_name(screen)
    available = set(discover_screen_media(media_root, screen))
    playlist = copy.deepcopy(config.get("playlist", []))
    row = next(
        (
            item for item in playlist
            if isinstance(item, dict) and item.get("module") == screen
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
            ),
            {},
        )
        latest_sources = tuple(
            reference for reference in latest_row.get("sources", ())
            if reference in available
        )
        return ScreenRepair(latest, latest_sources, False)
    return ScreenRepair(updated, sources, True)
