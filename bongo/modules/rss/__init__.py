"""News from RSS 2.0 / Atom feeds. The headline and the story text have their own colours, so
you can always tell which one you're reading. Four styles share the feeds and colours.

A feed is its address, or ``{"url": address, "only": "words, ...", "never": "words, ..."}`` when it
has word filters (filters.py): both forms load, so a feed saved before filters existed is unchanged."""
from bongo.module import Module

from . import data
from .help import HELP
from .presets import FEEDS
from .styles import TEXT_SPEEDS, Headline, Picture, Story, Ticker


def migrate(settings: dict) -> list[str]:
    """Text speed was briefly slow / normal / fast / faster: now the number each of those is on its slider."""
    named = settings.get("text_speed")
    if isinstance(named, str):
        settings["text_speed"] = TEXT_SPEEDS.get(named, TEXT_SPEEDS["normal"])
        return [f"rss: text_speed {named!r} is now {settings['text_speed']}"]
    return []

MODULE = Module(
    id="rss",
    name="News",
    description="News from RSS and Atom feeds: a scrolling ticker, a headline card, the story (headline "
                "on top, its text moving up below, a line at a time or gliding, like v2) or the story's picture beside it.",
    styles={"ticker": Ticker, "headline": Headline, "story": Story, "picture": Picture},
    defaults=dict(
        feeds=["https://feeds.bbci.co.uk/news/rss.xml"], max_items=20, refresh_minutes=15,
        scroll_speed=24, rotate_seconds=8, show_summary=True,
        # Headline and story text in clearly different colours (warm vs cool), like v2.
        headline_colour="#FFD27A", summary_colour="#B8C7DC", accent="#FFB547",
        background_top="#101216", background_bottom="#060708",
    ),
    check=data.check_urls,   # every feed is an http(s) address (and its filters, if any, are two boxes of words)
    verify=data.verify,      # and, when saved from the dashboard, really is RSS or Atom
    verify_parts=data.urls,  # ...asked once for each address that's new: not again when words or colours change
    basic=("feeds", "show_summary", "headline_colour", "summary_colour"),
    limits={"max_items": (1, 100), "refresh_minutes": (5, 1440), "rotate_seconds": (1, 3600), "scroll_speed": (1, 200),
            "text_speed": (1, 100)},
    marks={"text_speed": [[v, name.capitalize()] for name, v in TEXT_SPEEDS.items()]},   # Story and Picture
    migrate=migrate,
    suggestions={"feeds": FEEDS},  # one-tap feeds in the dashboard (v2's presets and more)
    word_filters={"feeds": "/api/rss/filtered"},  # each feed's Only and Never show boxes, and where their counts come from
    help=HELP,
)
