"""RSS 2.0 / Atom headlines, fetched in the background and cached per feed URL.

``latest(settings)`` never blocks: it returns the merged cached headlines (or None while
nothing has loaded) and starts a refresh for every stale feed. Set BONGO_OFFLINE=1 to
never start fetch threads (tests, previews).
"""
from __future__ import annotations

import datetime as dt
import email.utils
import html
import logging
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, replace

log = logging.getLogger(__name__)
USER_AGENT = "BongoMatrix/3 (+rss ticker)"
MAX_BYTES = 2_000_000
RETRY_SECONDS = 60
_MIN = dt.datetime.min.replace(tzinfo=dt.timezone.utc)
_ASCII = str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"', "–": "-",
                        "—": "-", "…": "...", " ": " "})


@dataclass(frozen=True)
class Item:
    title: str
    published: dt.datetime | None  # timezone-aware UTC
    source: str
    image: str | None = None  # thumbnail URL, if the feed gives one


@dataclass(frozen=True)
class Feed:
    title: str
    items: tuple[Item, ...]
    fetched: float = 0.0  # time.time() of the fetch


def fetch_bytes(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310 (user-configured feed)
        return response.read(MAX_BYTES)


def clean(text: str | None) -> str:
    text = html.unescape(re.sub(r"<[^>]*>", " ", text or ""))
    return " ".join(text.translate(_ASCII).split())


def _local(tag) -> str:
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _child(el, *names: str):
    return next((c for c in el if _local(c.tag) in names), None)


def _when(text: str | None) -> dt.datetime | None:
    if not text or not text.strip():
        return None
    try:
        when = email.utils.parsedate_to_datetime(text.strip())
    except (TypeError, ValueError):
        try:
            when = dt.datetime.fromisoformat(text.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    return when.replace(tzinfo=dt.timezone.utc) if when.tzinfo is None else when.astimezone(dt.timezone.utc)


def _image(el) -> str | None:
    """Thumbnail URL from media:thumbnail, media:content, an image enclosure or an Atom enclosure link."""
    best = None
    for node in el.iter():
        name, attrs = _local(node.tag), node.attrib
        url = attrs.get("url") or (attrs.get("href") if name == "link" else None)
        if not url or not url.startswith(("http://", "https://")):
            continue
        kind = attrs.get("type", "") + attrs.get("medium", "")
        if name == "thumbnail":
            return url
        if (name in ("content", "enclosure") and "image" in kind) or \
                (name == "link" and attrs.get("rel") == "enclosure" and "image" in kind):
            best = best or url
    return best


def parse(raw: bytes, fetched: float | None = None) -> Feed:
    if b"<!DOCTYPE" in raw.upper() or b"<!ENTITY" in raw.upper():
        raise ValueError("feed rejected: DOCTYPE/ENTITY declarations are not allowed")
    root = ET.fromstring(raw)
    if _local(root.tag) not in ("rss", "feed", "RDF"):  # RSS 2.0, Atom, RSS 1.0
        raise ValueError(f"not an RSS or Atom feed (the document is <{_local(root.tag)}>)")
    channel = _child(root, "channel")
    holder = root if channel is None else channel
    node = _child(holder, "title")
    source = clean("".join(node.itertext())) if node is not None else ""
    items = []
    for el in root.iter():
        if _local(el.tag) not in ("item", "entry"):
            continue
        node = _child(el, "title")
        title = clean("".join(node.itertext())) if node is not None else ""
        stamp = _child(el, "pubDate", "published", "updated", "date")
        if title:
            items.append(Item(title, _when(stamp.text) if stamp is not None else None, source, _image(el)))
    return Feed(source, tuple(items), time.time() if fetched is None else fetched)


def check_urls(settings: dict) -> None:
    """Quick check: every feed is a web address (http or https)."""
    feeds = settings["feeds"]
    if not isinstance(feeds, list) or not all(isinstance(u, str) for u in feeds):
        raise ValueError("feeds must be a list of web addresses")
    for url in feeds:
        parts = urllib.parse.urlsplit(url.strip())
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ValueError(f"{url!r} isn't a web address; feeds start with https:// or http://")


def verify(settings: dict) -> list[str]:
    """Fetch each feed once and make sure it really is RSS or Atom (run when settings are saved)."""
    if os.environ.get("BONGO_OFFLINE"):
        return []
    problems = []
    for url in settings["feeds"][:10]:
        try:
            feed = parse(fetch_bytes(url))
        except urllib.error.HTTPError as error:
            problems.append(f"{url}: the site answered {error.code} {error.reason}")
        except (urllib.error.URLError, OSError) as error:
            problems.append(f"{url}: couldn't reach it ({getattr(error, 'reason', error)})")
        except (ET.ParseError, ValueError) as error:
            reason = error if isinstance(error, ValueError) else "not an RSS or Atom feed (not valid XML)"
            problems.append(f"{url}: {reason}")
        else:
            seed(url, feed)  # it's good: show it straight away instead of waiting for the next refresh
    return problems


_cache: dict[str, Feed] = {}
_in_flight: set[str] = set()
_retry_at: dict[str, float] = {}
_lock = threading.Lock()


def _refresh(url: str) -> None:
    try:
        feed = parse(fetch_bytes(url))
        with _lock:
            _cache[url] = feed
            _retry_at.pop(url, None)
    except Exception as error:  # network down, bad XML: keep the last good feed, retry later
        log.warning("rss refresh failed for %s: %s", url, error)
        with _lock:
            _retry_at[url] = time.time() + RETRY_SECONDS
    finally:
        with _lock:
            _in_flight.discard(url)


def latest(settings: dict) -> list[Item] | None:
    now, feeds = time.time(), []
    with _lock:
        for url in settings["feeds"]:
            feed = _cache.get(url)
            if feed:
                feeds.append(feed)
            stale = feed is None or now - feed.fetched > settings["refresh_minutes"] * 60
            if stale and url not in _in_flight and now >= _retry_at.get(url, 0) \
                    and not os.environ.get("BONGO_OFFLINE"):
                _in_flight.add(url)
                threading.Thread(target=_refresh, args=(url,), daemon=True, name="rss").start()
    items, seen = [], set()
    for item in sorted((i for f in feeds for i in f.items), key=lambda i: i.published or _MIN, reverse=True):
        if item.title not in seen:
            seen.add(item.title)
            items.append(item)
    return items[:settings["max_items"]] or None


def seed(url: str, feed: Feed) -> None:
    """Put a feed in the cache (previews and tests)."""
    with _lock:
        _cache[url] = replace(feed, fetched=feed.fetched or time.time())


# -- thumbnails ------------------------------------------------------------------------------
MAX_IMAGE_BYTES = 1_500_000
_images: dict[str, object] = {}  # url -> small PIL image, or None after a failed fetch
_image_order: list[str] = []


def _fetch_image(url: str) -> None:
    from PIL import Image
    try:
        raw = fetch_bytes(url)[:MAX_IMAGE_BYTES]
        import io
        with Image.open(io.BytesIO(raw)) as im:
            if im.width * im.height > 16_000_000:
                raise ValueError("image too large")
            im.draft("RGB", (128, 128))  # JPEGs decode straight to a small size
            small = im.convert("RGB")
            small.thumbnail((128, 128))
    except Exception as error:
        log.info("no thumbnail from %s: %s", url, error)
        small = None
    with _lock:
        _images[url] = small
        _in_flight.discard(url)


def image(url: str | None):
    """The thumbnail for ``url`` if it has loaded, else None (and a fetch starts in the background)."""
    if not url:
        return None
    with _lock:
        if url in _images:
            return _images[url]
        if url not in _in_flight and not os.environ.get("BONGO_OFFLINE"):
            _in_flight.add(url)
            _image_order.append(url)
            while len(_image_order) > 40:  # keep the cache small
                _images.pop(_image_order.pop(0), None)
            threading.Thread(target=_fetch_image, args=(url,), daemon=True, name="rss-image").start()
    return None


def seed_image(url: str, img) -> None:
    """Put a thumbnail in the cache (previews and tests)."""
    with _lock:
        _images[url] = img
