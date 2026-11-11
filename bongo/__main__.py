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
from bongo.fade import Fader
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


def _start_dashboard(config, modules, live, config_path) -> None:
    """Serve the dashboard on a background thread; the player keeps the main thread."""
    import threading

    from werkzeug.serving import make_server

    from bongo.pairing import Pairing
    from bongo.web import LogBuffer, create_app

    logs = LogBuffer()
    logging.getLogger().addHandler(logs)
    pairing = Pairing(config_path.parent / "paired-devices.json")
    live.overlay = pairing.overlay(Context().fonts)
    app = create_app(modules=modules, live=live, pairing=pairing, config_path=config_path, logs=logs)
    server = make_server(config["web"]["host"], config["web"]["port"], app, threaded=True)
    threading.Thread(target=server.serve_forever, daemon=True, name="dashboard").start()
    logging.info("dashboard on http://%s:%s", config["web"]["host"], config["web"]["port"])


def _hardware(config):
    from bongo.panel import HardwarePanel, PanelBusy

    try:
        return HardwarePanel(**config["panel"])
    except PanelBusy as error:
        sys.exit(f"bongo: {error}")


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
    update = sub.add_parser("update", help="check for, or install, a new version (see bongo/update.py)")
    update.add_argument("--apply", action="store_true", help="install it (the updater service runs this)")
    update.add_argument("--config", default=str(cfg.DEFAULT_PATH))
    run = sub.add_parser("run")
    run.add_argument("--config", default=str(cfg.DEFAULT_PATH))
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    modules = discover()

    if args.command == "list":
        for module in modules.values():
            print(f"{module.id:<16} {module.name:<20} styles: {', '.join(module.styles)}")
        return 0

    if args.command == "update":
        from bongo import update

        config = cfg.load(args.config)
        if args.apply:
            result = update.apply(config)
            print(result["message"])
            return 0 if result["state"] in ("updated", "up_to_date") else 1
        url, branch = update.source(config)
        here, there = update.version(), update.latest(url, branch)
        print(f"running {here['short'] if here else 'unknown'}; {branch} at {url} is {there[:8]}")
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

            panel = _hardware(config)
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

    from pathlib import Path

    from bongo.live import Live
    from bongo.overlays import Overlays
    from bongo.player import play

    config = cfg.load(args.config)
    for note in cfg.tidy(config, modules):
        logging.warning("old config: %s", note)
    try:
        from bongo import media

        if moved := media.adopt_old_uploads():
            logging.info("moved %d uploaded file(s) into %s", moved, media.UPLOADED)
    except OSError as error:  # read-only checkout under the service: leave them, log why
        logging.warning("couldn't move old uploads: %s", error)
    problems = cfg.validate(config, modules)
    for problem in problems:
        logging.error(problem)
    live = Live()
    if config["web"]["enabled"]:
        _start_dashboard(config, modules, live, Path(args.config))
    panel = Fader(_hardware(config))  # day <-> night brightness glides instead of jumping
    good, first = None, True  # last config that played; the boot screen only plays at start-up
    try:
        while True:  # play() returns when the dashboard saves a new config
            try:
                schedule = Schedule.from_config(config)
                if not first:
                    schedule.boot = None  # the boot screen plays once, not after a dashboard save
                first = False
                factory = cfg.context_factory(config)
                play(cfg.playlist(config, modules), modules, panel, factory,
                     place=cfg.safe_area(config).place, schedule=schedule, live=live,
                     overlays=Overlays.from_config(config, modules, factory))
                good = config
            except Exception:  # never exit: the dashboard stays up so the config can be fixed
                logging.exception("can't play this config")
                if good is not None and config is not good:
                    logging.error("going back to the last config that worked")
                    cfg.save(good, args.config)
                    config = good
                    continue
                live.reload.wait(10)  # nothing better to play: wait for a fix from the dashboard
            live.reload.clear()
            live.skip.clear()
            config = cfg.load(args.config)
            cfg.tidy(config, modules)
            logging.info("config reloaded")
    except KeyboardInterrupt:
        panel.clear()
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
