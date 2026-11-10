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


def parse(raw: bytes, fetched: float | None = None) -> Feed:
    if b"<!DOCTYPE" in raw.upper() or b"<!ENTITY" in raw.upper():
        raise ValueError("feed rejected: DOCTYPE/ENTITY declarations are not allowed")
    root = ET.fromstring(raw)
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
            items.append(Item(title, _when(stamp.text) if stamp is not None else None, source))
    return Feed(source, tuple(items), time.time() if fetched is None else fetched)


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
