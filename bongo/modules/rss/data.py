"""RSS 2.0 / Atom headlines, fetched in the background and cached per feed URL.

``latest(settings)`` never blocks: it returns the merged cached headlines (or None while
nothing has loaded) and starts a refresh for every stale feed. Set BONGO_OFFLINE=1 to
never start fetch threads (tests, previews).

A feed may have word filters (``filters.py``). They are applied when the stories are put together
(a feed has refreshed, or the settings are new), never per frame: ``latest`` keeps what it worked out.
"""
from __future__ import annotations

import datetime as dt
import email.utils
import html
import http.client
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

from . import filters

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
    summary: str = ""  # the story's text (description / summary), plain, shortened


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


SUMMARY_CHARS = 600  # more than a 64x32 panel can show while a story is up


def _summary(el, title: str) -> str:
    """The story text: RSS description, Atom summary or content, as plain text without the title."""
    node = _child(el, "description", "summary", "content", "encoded")
    text = clean("".join(node.itertext())) if node is not None else ""
    if text.lower().startswith(title.lower()):
        text = text[len(title):].lstrip(" -:.|")
    if len(text) > SUMMARY_CHARS:
        text = text[:SUMMARY_CHARS].rsplit(" ", 1)[0] + "..."
    return text


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
            items.append(Item(title, _when(stamp.text) if stamp is not None else None, source, _image(el),
                              _summary(el, title)))
    return Feed(source, tuple(items), time.time() if fetched is None else fetched)


def urls(settings: dict) -> list[str]:
    """Every feed's address (a feed with filters is saved as {"url": ..., "only": ..., "never": ...})."""
    return [url for url, _, _ in filters.entries(settings["feeds"])]


def check_urls(settings: dict) -> None:
    """Quick check: every feed is a web address (http or https), with or without its word filters."""
    feeds = settings["feeds"]
    if not isinstance(feeds, list):
        raise ValueError("feeds must be a list of web addresses")
    for feed in feeds:
        filters.check(feed)
    for n, url in enumerate(urls(settings), 1):
        try:
            parts = urllib.parse.urlsplit(url.strip())
            ok = parts.scheme in ("http", "https") and bool(parts.hostname)
        except ValueError:
            ok = False
        if not ok:   # said by its place in the list, never by what was typed: an address may have a password in it
            raise ValueError(f"Feed {n} isn't a web address; feeds start with https:// or http://")


def named(url: str) -> str:
    """A feed, for a message or the log: its site's name and nothing else. Never the whole address, which
    may hold a name and password or a private feed's key."""
    try:
        return urllib.parse.urlsplit(url.strip()).hostname or "no site"
    except ValueError:
        return "no site"


def why(error: Exception, url: str) -> str:
    """Why a feed couldn't be read, in words that never have its address in them."""
    try:
        netloc = urllib.parse.urlsplit(url.strip()).netloc
    except ValueError:
        netloc = ""
    if isinstance(error, urllib.error.HTTPError):
        text = f"the site answered {error.code} {error.reason}"
    elif isinstance(error, ET.ParseError):
        text = "not an RSS or Atom feed (not valid XML)"
    elif isinstance(error, OSError):   # (a URLError is one)
        text = f"couldn't reach it ({getattr(error, 'reason', error)})"
    elif type(error) is ValueError:    # parse()'s own words
        text = str(error)
    elif isinstance(error, http.client.InvalidURL):   # (its own words quote the address, password and all)
        text = "the Matrix can't fetch that address" + (": a name and password in an address aren't supported" if "@" in netloc else "")
    else:
        text = f"couldn't read it ({type(error).__name__})"
    for private in (url, url.strip(), netloc, netloc.rpartition("@")[0]):
        if private:
            text = text.replace(private, "...")
    return text


def verify(settings: dict, new: list[str] | None = None) -> list[str]:
    """Fetch each feed once and make sure it really is RSS or Atom (run when settings are saved). ``new``:
    only these addresses, the ones no saved News screen has yet (``Module.verify_parts``): a feed that was
    accepted before isn't fetched again here, so one that's down today never stops a save."""
    if os.environ.get("BONGO_OFFLINE"):
        return []
    problems, listed = [], urls(settings)
    asked = listed if new is None else new
    if len(asked) > 10:   # each one is fetched while the save waits: the rest would go in unchecked
        problems.append("That's more than 10 new feeds in one go: add up to 10, save, then add the rest")
    for url in asked[:10]:
        try:
            feed = parse(fetch_bytes(url))
        except Exception as error:  # noqa: BLE001 - whatever went wrong, the save is told in words, not stopped by a crash
            place = f"Feed {listed.index(url) + 1} " if url in listed else "Feed "
            problems.append(f"{place}({named(url)}): {why(error, url)}")
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
        log.warning("rss refresh failed for %s: %s", named(url), why(error, url))
        with _lock:
            _retry_at[url] = time.time() + RETRY_SECONDS
    finally:
        with _lock:
            _in_flight.discard(url)


_warned: set[str] = set()


def _left_out(item: Item, only: str, never: str):
    """``filters.left_out``, but a filter that breaks never stops the news: the story is shown, and it's logged once."""
    try:
        return filters.left_out(item, only, never)
    except Exception as error:  # noqa: BLE001
        if "filter" not in _warned:
            _warned.add("filter")
            log.warning("rss: a word filter failed, so stories are shown unfiltered: %s", type(error).__name__)
        return None


_merged: dict[tuple, tuple] = {}   # (the feeds as saved, max_items) -> (the fetched feeds it was made from, stories, how many before filters)


def _stories(entries: list, most: int, stale_after: float | None = None) -> tuple[list[Item] | None, int]:
    """(the stories to show, how many the fetched feeds had before their filters). Worked out again only
    when a feed has been fetched again or the settings are new. With ``stale_after`` (seconds), a refresh is
    started for every feed older than that."""
    now, got = time.time(), []
    with _lock:
        for url, only, never in entries:
            feed = _cache.get(url)
            if feed:
                got.append((feed, only, never))
            if stale_after is not None:
                stale = feed is None or now - feed.fetched > stale_after
                if stale and url not in _in_flight and now >= _retry_at.get(url, 0) \
                        and not os.environ.get("BONGO_OFFLINE"):
                    _in_flight.add(url)
                    threading.Thread(target=_refresh, args=(url,), daemon=True, name="rss").start()
        key = (tuple(entries), most)
        kept = _merged.get(key)
        if kept and len(kept[0]) == len(got) and all(a is b for a, (b, _, _) in zip(kept[0], got)):
            return kept[1], kept[2]
    passed = [i for feed, only, never in got for i in feed.items if _left_out(i, only, never) is None]
    items, seen = [], set()
    for item in sorted(passed, key=lambda i: i.published or _MIN, reverse=True):
        if item.title not in seen:   # the same story in two feeds is shown once (from a feed whose filters let it through)
            seen.add(item.title)
            items.append(item)
    stories, before = items[:most] or None, sum(len(feed.items) for feed, _, _ in got)
    with _lock:
        if len(_merged) >= 32:   # (every different filter typed in the editor is a new one: keep few)
            _merged.clear()
        _merged[key] = (tuple(feed for feed, _, _ in got), stories, before)
    return stories, before


def latest(settings: dict) -> list[Item] | None:
    return _stories(filters.entries(settings["feeds"]), settings["max_items"], stale_after=settings["refresh_minutes"] * 60)[0]


def trouble(settings: dict) -> str | None:
    """Why there are no stories, when it isn't just "still fetching": "none" (no feed is set), "offline"
    (the last try at every feed failed) or "filtered" (there are stories, and the filters leave none)."""
    entries = filters.entries(settings["feeds"])
    if not entries:
        return "none"
    with _lock:
        if all(url not in _cache and url in _retry_at for url, _, _ in entries):
            return "offline"
        coming = any(url not in _cache and url not in _retry_at for url, _, _ in entries)   # a feed not heard from yet
    stories, before = _stories(entries, settings["max_items"])
    return "filtered" if stories is None and before and not coming else None


LIST_MOST = 100   # headlines the dashboard is sent for "what's left out" (it's told how many more there are)


def filtered(feeds: list) -> dict:
    """For the dashboard's editor: how many of each feed's stories get through its filters, and the headlines
    left out, from the copy already fetched. Nothing is fetched here, however often it's asked."""
    entries = filters.entries(feeds)
    with _lock:
        state = [(_cache.get(url), url in _retry_at) for url, _, _ in entries]
    out, through, coming = [], 0, False
    for (url, only, never), (feed, failed) in zip(entries, state):
        if feed is None:
            why = "failed" if failed else "offline" if os.environ.get("BONGO_OFFLINE") else "waiting"
            coming = coming or not failed
            out.append({"loaded": False, "why": why})
            continue
        left = [(item.title, out_for) for item in feed.items if (out_for := _left_out(item, only, never))]
        through += len(feed.items) - len(left)
        out.append({"loaded": True, "name": feed.title, "total": len(feed.items), "shown": len(feed.items) - len(left),
                    "left_out": [{"title": title, **({"never": word} if kind == "never" else {})}
                                 for title, (kind, word) in left[:LIST_MOST]]})
    had = sum(f["total"] for f in out if f["loaded"])
    return {"feeds": out, "nothing": bool(had) and not through and not coming}


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
