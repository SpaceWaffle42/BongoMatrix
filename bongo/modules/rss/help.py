"""The Help guide for adding news feeds, for someone who has never heard of RSS."""

HELP = {
    "title": "Finding news feeds",
    "intro": "A news feed (RSS or Atom) is a special web address a site publishes so apps can read its "
             "latest headlines. It isn't the site's normal address: it usually ends in /feed, /rss or .xml. "
             "You don't need an account or a key.",
    "steps": [
        "The quickest way: open Quick add under Feeds and tap any of the ready-made feeds.",
        "For another site, look on it for the orange RSS symbol or a link called RSS or Feeds (often at the "
        "very bottom of the page). Right-click it (long-press on a phone) and copy the link.",
        "No link? Try the site's address with `/feed` or `/rss` added, like `https://example.com/feed`. "
        "If your browser shows a page of text or code, says \"This XML file does not appear to have any "
        "style information\", or downloads a file, that's a feed. An error page means it isn't.",
        "Paste the address into Feeds and press Enter, then press Apply to Matrix. The Matrix tries each "
        "new feed then and tells you if one doesn't work.",
    ],
    "tips": [
        "News on any subject: Google News makes a feed for any search. Change football to your subject "
        "(use + between words): `https://news.google.com/rss/search?q=football&hl=en-GB&gl=GB&ceid=GB:en`",
        "A Reddit community: its address with `/.rss` on the end, like "
        "`https://www.reddit.com/r/worldnews/.rss`",
        "Feeds behind a login or a paywall won't work, and some sites don't offer a feed at all.",
    ],
}
