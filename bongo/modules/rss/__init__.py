"""News headlines from RSS 2.0 / Atom feeds. Three styles share the feeds and colours."""
from bongo.module import Module

from . import data
from .styles import Headline, Picture, Ticker

MODULE = Module(
    id="rss",
    name="News",
    description="Headlines from RSS and Atom feeds: a scrolling ticker, a rotating card, or the story's picture with its headline.",
    styles={"ticker": Ticker, "headline": Headline, "picture": Picture},
    defaults=dict(
        feeds=["https://feeds.bbci.co.uk/news/rss.xml"], max_items=20, refresh_minutes=15,
        scroll_speed=24, rotate_seconds=8, accent="#FFB547", text_colour="#FFF4DE",
        background_top="#101216", background_bottom="#060708",
    ),
    check=data.check_urls,   # every feed is an http(s) address
    verify=data.verify,      # and, when saved from the dashboard, really is RSS or Atom
)
