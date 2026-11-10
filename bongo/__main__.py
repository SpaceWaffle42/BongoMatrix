"""Command line.

    python -m bongo list                                  modules and their styles
    python -m bongo preview lava_lamp --style metro       write an LED-look GIF
    python -m bongo calibrate --margins 1,2,1,2           show the safe-area test card on the panel
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
from bongo.safe_area import SafeArea
from bongo.schedule import Schedule


def _setting(text: str):
    key, _, value = text.partition("=")
    try:
        return key, json.loads(value)
    except json.JSONDecodeError:
        return key, value


def _margins(text: str) -> dict:
    """'2' or '1,2' or '1,2,1,2' (CSS order: top, right, bottom, left)."""
    values = [int(v) for v in text.split(",")]
    if len(values) not in (1, 2, 4):
        raise ValueError("give 1, 2 or 4 numbers")
    top, right, bottom, left = {1: values * 4, 2: values * 2, 4: values}[len(values)]
    return dict(top=top, right=right, bottom=bottom, left=left)


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
    preview.add_argument("--safe-area", type=_margins, metavar="T,R,B,L",
                         help="preview inside a safe area; the hidden border is tinted")
    calibrate = sub.add_parser("calibrate")
    calibrate.add_argument("--margins", type=_margins, metavar="T,R,B,L",
                           help="try margins without saving (default: the config's safe_area)")
    calibrate.add_argument("--save", action="store_true", help="write --margins into the config")
    calibrate.add_argument("--out", help="save the test card as an image instead of showing it")
    calibrate.add_argument("--config", default=str(cfg.DEFAULT_PATH))
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

        area = SafeArea(**(args.safe_area or {}))
        ctx = Context(width=area.width, height=area.height, rng=random.Random(args.seed))
        style = modules[args.module].create(args.style, dict(args.set), ctx)
        panel = RecordingPanel()
        start = dt.datetime.now()
        for i in range(int(args.seconds * style.fps)):
            frame = style.render(start + dt.timedelta(seconds=i / style.fps))
            panel.show(area.place(frame, border=(45, 12, 12) if args.safe_area else (0, 0, 0)))
        panel.save_gif(args.out, fps=style.fps)
        print(f"wrote {args.out} ({len(panel.frames)} frames)")
        return 0

    if args.command == "calibrate":
        config = cfg.load(args.config)
        if args.margins:
            config["safe_area"] = args.margins
        area = cfg.safe_area(config)
        font = Context().fonts("tom-thumb")
        if args.out:
            from bongo.panel import led_look

            led_look(area.calibration(font)).save(args.out)
            print(f"wrote {args.out} ({area.width}x{area.height} visible)")
        else:
            import time

            from bongo.panel import HardwarePanel

            panel = HardwarePanel(**config["panel"])
            print(f"showing safe area {area.margins} ({area.width}x{area.height}); Ctrl-C to stop")
            try:
                step = 0
                while True:
                    panel.show(area.calibration(font, step))
                    step += 1
                    time.sleep(0.6)
            except KeyboardInterrupt:
                panel.clear()
        if args.save and args.margins:
            cfg.save(config, args.config)
            print(f"saved safe_area to {args.config}")
        return 0

    from bongo.panel import HardwarePanel
    from bongo.player import play

    config = cfg.load(args.config)
    problems = cfg.validate(config, modules)
    for problem in problems:
        logging.error(problem)
    panel = HardwarePanel(**config["panel"])
    try:
        play(cfg.playlist(config, modules), modules, panel, cfg.context_factory(config),
             place=cfg.safe_area(config).place, schedule=Schedule.from_config(config))
    except KeyboardInterrupt:
        panel.clear()
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
