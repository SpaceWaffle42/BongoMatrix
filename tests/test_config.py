from bongo import config as cfg



def test_tidy_drops_what_an_older_build_saved():
    from bongo.registry import discover
    modules = discover()
    config = cfg.load("/nonexistent/config.json")
    config["playlist"] = [
        {"module": "lava_lamp", "style": "classic", "seconds": 30,
         "settings": {"palettes": ["mint_soda", "fire_burst"], "time_align": "middle", "gone_setting": 1}},
        {"module": "lava_lamp", "style": "metro", "seconds": 30, "settings": {"palettes": ["rose_gold"]}},
    ]
    notes = cfg.tidy(config, modules)
    assert len(notes) == 4
    assert config["playlist"][0]["settings"] == {"palettes": ["fire_burst"]}
    assert config["playlist"][1]["settings"] == {}
    assert cfg.validate(config, modules) == []
