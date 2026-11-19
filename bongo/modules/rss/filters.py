"""Word filters for a feed: "Only stories that mention" and "Never show stories that mention".

A feed in the settings is its address, or ``{"url": ..., "only": "...", "never": "..."}`` once it has
filters. Each box is words or phrases with commas between them. A story is looked at as a person reads
it (its headline and its summary, already plain text): whole words, any case, with or without accents,
whichever apostrophe or dash was typed. Never show wins over Only.
"""
from __future__ import annotations

import re
import unicodedata
from functools import lru_cache

MAX_CHARS = 500   # one box of words: far more than anyone types, short enough to search with on a Pi Zero
KEYS = ("url", "only", "never")

# what a phone's keyboard types and what a feed sends, made the same: apostrophes, quotes, dashes; and the
# invisible characters (soft hyphen, zero-width space and joiners, emoji "variation selectors") dropped
_SAME = str.maketrans({**dict.fromkeys("‘’‚‛ʼ′`´", "'"), **dict.fromkeys("“”„″«»", '"'),
                       **dict.fromkeys("‐‑‒–—―−", "-"),
                       **dict.fromkeys("­​‌⁠﻿︎️")})
# scripts written without spaces between words (Thai, Lao, Burmese, Khmer, Japanese, Chinese) or with endings
# joined on (Korean): a word of theirs is looked for anywhere, as "whole words" would never find it
_JOINED = re.compile(r"[฀-໿က-႟ក-៿぀-ヿ㐀-䶿一-鿿가-힯]")


def fold(text: str) -> str:
    """``text`` as the filters compare it: any case, accents or none, one kind of apostrophe and dash,
    single spaces."""
    text = unicodedata.normalize("NFKD", str(text)).translate(_SAME)
    return " ".join("".join(c for c in text if not unicodedata.combining(c)).casefold().split())


def words(box: str) -> dict[str, str]:
    """The words and phrases in one box: {as compared: as typed}, each once, in the order typed.
    Stray commas and spaces mean nothing, and neither do "quotes" round a phrase."""
    out = {}
    for typed in unicodedata.normalize("NFKC", str(box)).replace("،", ",").replace("、", ",").split(","):
        word = fold(typed).strip(' "')
        if word:
            out.setdefault(word, " ".join(typed.split()).strip(' "“”'))
    return out


def _whole(ch: str) -> bool:
    """Is ``ch`` the edge of a word that must not run on into more letters ("art" isn't in "party")?"""
    return (ch.isalnum() or ch == "_") and not _JOINED.match(ch)


@lru_cache(maxsize=64)
def finder(box: str):
    """A search for any of a box's words in folded text, or None for an empty box."""
    found = [("(?<!\\w)" if _whole(w[0]) else "") + re.escape(w) + ("(?!\\w)" if _whole(w[-1]) else "")
             for w in words(box)]
    return re.compile("|".join(found)) if found else None


def readable(item) -> str:
    """A story's headline and summary as compared (on a line each, so no phrase runs from one into the other)."""
    return fold(item.title) + "\n" + fold(item.summary)


def left_out(item, only: str, never: str) -> tuple[str, str] | None:
    """Why a story isn't shown: ("never", the word it mentions) or ("only", "") when it mentions none of
    the Only words. None: it gets through."""
    no, yes = finder(never), finder(only)
    if no is None and yes is None:
        return None
    text = readable(item)
    hit = no.search(text) if no else None
    if hit:
        return "never", words(never).get(hit.group(0), hit.group(0))
    return ("only", "") if yes and not yes.search(text) else None


def entry(feed) -> tuple[str, str, str]:
    """(address, only, never) of one saved feed, whichever way it was saved."""
    if isinstance(feed, dict):
        return str(feed.get("url", "")), str(feed.get("only") or ""), str(feed.get("never") or "")
    return str(feed), "", ""


def entries(feeds) -> list[tuple[str, str, str]]:
    return [entry(feed) for feed in feeds]


def check(feed) -> None:
    """One saved feed is an address, or an address with its two boxes of words. Raises ValueError."""
    if isinstance(feed, str):
        return
    if not isinstance(feed, dict) or not isinstance(feed.get("url"), str):
        raise ValueError('each feed is a web address, or {"url": address, "only": words, "never": words}')
    unknown = sorted(set(feed) - set(KEYS))
    if unknown:
        raise ValueError(f"a feed has {', '.join(KEYS)}; not {', '.join(map(repr, unknown))}")
    for key in ("only", "never"):
        box = feed.get(key, "")
        if not isinstance(box, str):
            raise ValueError(f"a feed's {key!r} filter is words with commas between them")
        if len(box) > MAX_CHARS:
            raise ValueError(f"a feed's {key!r} filter is too long: keep it under {MAX_CHARS} characters (it has {len(box)})")
