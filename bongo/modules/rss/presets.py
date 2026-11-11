"""Ready-made news feeds for the dashboard's "Quick add". Free, public RSS/Atom feeds only.

The first fifteen are the presets Bongo Matrix v2 offered (Sky and ESPN moved to https);
the rest are extras. The dashboard still fetches a feed when it's saved, so a feed that has
moved or died is caught there, not on the panel.
"""

_GROUPS = {
    "BBC headlines": [
        ("BBC Top Stories", "https://feeds.bbci.co.uk/news/rss.xml"),
        ("BBC World", "https://feeds.bbci.co.uk/news/world/rss.xml"),
        ("BBC UK", "https://feeds.bbci.co.uk/news/uk/rss.xml"),
    ],
    "BBC UK nations": [
        ("BBC England", "https://feeds.bbci.co.uk/news/england/rss.xml"),
        ("BBC Northern Ireland", "https://feeds.bbci.co.uk/news/northern_ireland/rss.xml"),
        ("BBC Scotland", "https://feeds.bbci.co.uk/news/scotland/rss.xml"),
        ("BBC Wales", "https://feeds.bbci.co.uk/news/wales/rss.xml"),
    ],
    "World and sport": [
        ("Reddit World News", "https://www.reddit.com/r/worldnews/.rss"),
        ("Sky News World", "https://feeds.skynews.com/feeds/rss/world.xml"),
        ("ESPN", "https://www.espn.com/espn/rss/news"),
        ("New York Times World", "https://rss.nytimes.com/services/xml/rss/nyt/World.xml"),
        ("The Guardian UK", "https://www.theguardian.com/uk/rss"),
        ("AllSides", "https://www.allsides.com/rss/news"),
        ("Reuters (via Google News)",
         "https://news.google.com/rss/search?q=when:7d+site:reuters.com&ceid=US:en&hl=en-US&gl=US"),
        ("Associated Press", "https://feedx.net/rss/ap.xml"),
    ],
    # extras (new in v3)
    "BBC sections": [
        ("BBC Business", "https://feeds.bbci.co.uk/news/business/rss.xml"),
        ("BBC Technology", "https://feeds.bbci.co.uk/news/technology/rss.xml"),
        ("BBC Science", "https://feeds.bbci.co.uk/news/science_and_environment/rss.xml"),
        ("BBC Entertainment", "https://feeds.bbci.co.uk/news/entertainment_and_arts/rss.xml"),
    ],
    "More news": [
        ("Sky News UK", "https://feeds.skynews.com/feeds/rss/uk.xml"),
        ("Al Jazeera", "https://www.aljazeera.com/xml/rss/all.xml"),
        ("NPR News", "https://feeds.npr.org/1001/rss.xml"),
        ("NASA News", "https://www.nasa.gov/news-release/feed/"),
    ],
    "Sport": [
        ("BBC Sport", "https://feeds.bbci.co.uk/sport/rss.xml"),
        ("BBC Football", "https://feeds.bbci.co.uk/sport/football/rss.xml"),
        ("Sky Sports", "https://www.skysports.com/rss/12040"),
    ],
}

FEEDS = [{"name": name, "value": url, "group": group}
         for group, feeds in _GROUPS.items() for name, url in feeds]
