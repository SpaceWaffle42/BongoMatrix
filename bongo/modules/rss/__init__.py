"""News headlines from RSS 2.0 / Atom feeds. Two styles share the feeds and colours."""
from bongo.module import Module

from .styles import Headline, Ticker

MODULE = Module(
    id="rss",
    name="News",
    description="Headlines from RSS and Atom feeds: a scrolling ticker or a rotating card.",
    styles={"ticker": Ticker, "headline": Headline},
    defaults=dict(
        feeds=["https://feeds.bbci.co.uk/news/rss.xml"], max_items=20, refresh_minutes=15,
        scroll_speed=24, rotate_seconds=8, accent="#FFB547", text_colour="#FFF4DE",
        background_top="#101216", background_bottom="#060708",
    ),
)
