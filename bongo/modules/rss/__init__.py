"""News from RSS 2.0 / Atom feeds. The headline and the story text have their own colours, so
you can always tell which one you're reading. Four styles share the feeds and colours."""
from bongo.module import Module

from . import data
from .help import HELP
from .presets import FEEDS
from .styles import Headline, Picture, Story, Ticker

MODULE = Module(
    id="rss",
    name="News",
    description="News from RSS and Atom feeds: a scrolling ticker, a headline card, the story (headline "
                "on top, its text crawling up below, like v2) or the story's picture beside it.",
    styles={"ticker": Ticker, "headline": Headline, "story": Story, "picture": Picture},
    defaults=dict(
        feeds=["https://feeds.bbci.co.uk/news/rss.xml"], max_items=20, refresh_minutes=15,
        scroll_speed=24, rotate_seconds=8, show_summary=True,
        # Headline and story text in clearly different colours (warm vs cool), like v2.
        headline_colour="#FFD27A", summary_colour="#B8C7DC", accent="#FFB547",
        background_top="#101216", background_bottom="#060708",
    ),
    check=data.check_urls,   # every feed is an http(s) address
    verify=data.verify,      # and, when saved from the dashboard, really is RSS or Atom
    basic=("feeds", "show_summary", "headline_colour", "summary_colour"),
    limits={"max_items": (1, 100), "refresh_minutes": (5, 1440), "rotate_seconds": (1, 3600), "scroll_speed": (1, 200)},
    suggestions={"feeds": FEEDS},  # one-tap feeds in the dashboard (v2's presets and more)
    help=HELP,
)
