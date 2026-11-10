"""Command line.

    python -m bongo list                                  modules and their styles
    python -m bongo preview lava_lamp --style metro       write an LED-look GIF
    python -m bongo run                                   drive the panel from data/config.json
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import sys

from bongo import config as cfg
from bongo.module import Context
from bongo.panel import RecordingPanel
from bongo.registry import discover


def _setting(text: str):
    key, _, value = text.partition("=")
    try:
        return key, json.loads(value)
    except json.JSONDecodeError:
        return key, value


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="bongo")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list")
    preview = sub.add_parser("preview")
    preview.add_argument("module")
    preview.add_argument("--style")
    preview.add_argument("--seconds", type=float, default=4)
    preview.add_argument("--out", default="preview.gif")
    preview.add_argument("--set", action="append", default=[], type=_setting, metavar="KEY=VALUE")
    preview.add_argument("--seed", type=int, default=1)
    run = sub.add_parser("run")
    run.add_argument("--config", default=str(cfg.DEFAULT_PATH))
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    modules = discover()

    if args.command == "list":
        for module in modules.values():
            print(f"{module.id:<16} {module.name:<20} styles: {', '.join(module.styles)}")
        return 0

    if args.command == "preview":
        import random

        style = modules[args.module].create(args.style, dict(args.set), Context(rng=random.Random(args.seed)))
        panel = RecordingPanel()
        start = dt.datetime.now()
        for i in range(int(args.seconds * style.fps)):
            panel.show(style.render(start + dt.timedelta(seconds=i / style.fps)))
        panel.save_gif(args.out, fps=style.fps)
        print(f"wrote {args.out} ({len(panel.frames)} frames)")
        return 0

    from bongo.panel import HardwarePanel
    from bongo.player import play

    config = cfg.load(args.config)
    problems = cfg.validate(config, modules)
    for problem in problems:
        logging.error(problem)
    panel = HardwarePanel(**config["panel"])
    try:
        play(cfg.playlist(config, modules), modules, panel, cfg.context_factory(config))
    except KeyboardInterrupt:
        panel.clear()
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
