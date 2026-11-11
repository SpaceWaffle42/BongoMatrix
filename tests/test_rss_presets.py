from bongo.modules.rss import MODULE, data
from bongo.modules.rss.presets import FEEDS
from bongo.schema import describe

V2 = {"BBC Top Stories", "BBC World", "BBC UK", "BBC England", "BBC Northern Ireland", "BBC Scotland",
      "BBC Wales", "Reddit World News", "Sky News World", "ESPN", "New York Times World",
      "The Guardian UK", "AllSides", "Reuters (via Google News)", "Associated Press"}


def test_every_v2_preset_is_offered():
    assert V2 <= {f["name"] for f in FEEDS}


def test_presets_are_unique_https_feeds_that_pass_the_module_check():
    urls = [f["value"] for f in FEEDS]
    assert len(urls) == len(set(urls)) and len({f["name"] for f in FEEDS}) == len(FEEDS)
    assert all(u.startswith("https://") for u in urls)
    data.check_urls({"feeds": urls})


def test_the_dashboard_gets_them_as_quick_add_suggestions():
    field = next(f for f in describe(MODULE)["settings"] if f["name"] == "feeds")
    assert field["type"] == "list" and field["suggestions"] == FEEDS
    assert MODULE.defaults["feeds"][0] in {f["value"] for f in FEEDS}
