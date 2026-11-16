"""Command line.

    python -m bongo list                                  modules and their styles
    python -m bongo preview clock --style lava_metro      write an LED-look GIF
    python -m bongo calibrate --margins 1,2,1,2           show the safe-area test card on the panel
    python -m bongo run                                   drive the panel from data/config.json
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import sys
import time

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
    logging.getLogger("werkzeug").setLevel(logging.WARNING)   # not a line per request: they would fill the Logs page in a minute
    from bongo import __version__, hub as hubs, net, netname, setup_screen

    network = None
    if net.available():   # the Pi with NetworkManager: keep to the saved Wi-Fi, open setup as a last resort
        if config.get("wifi_country"):          # chosen on the setup page: keep it set
            net.apply_country(config["wifi_country"])
        elif not net.current_country():         # none at all (not even from Raspberry Pi Imager): Wi-Fi stays off without one
            net.apply_country("GB")
        network = net.Manager(netname.mac_suffix())
        net.captive_rule()   # phones joining the setup hotspot are sent to the setup page
        threading.Thread(target=network.run_forever, args=(threading.Event(),), daemon=True, name="wifi").start()
    pairing = Pairing(config_path.parent / "paired-devices.json")
    fonts = Context().fonts
    address = netname.AddressScreen(fonts)   # the frame it gets is already the safe area
    setup = setup_screen.SetupScreen(fonts, lambda: network.status if network else None)
    live.overlay = netname.chain(pairing.overlay(fonts), setup.overlay, address.overlay)   # PIN, then setup, then address
    port = config["web"]["port"]
    started = float(round(time.time()))   # whole seconds: what it announces is what it compares (hub election)
    hub = None
    if hubs.available():   # every Matrix can be the hub at http://matrix.local (the longest-running one is)
        suffix = netname.mac_suffix()

        def whoami():
            name = (cfg.load(config_path).get("name") or netname.DEFAULT_NAME).strip() or netname.DEFAULT_NAME
            return hubs.Peer(suffix, name, netname.address() or "127.0.0.1", port, __version__, started)
        try:
            mdns = hubs.Zeroconf(netname.address, port, suffix)
            hub = hubs.Hub(whoami(), browse=mdns.browse, alias=mdns.alias, whoami=whoami)
            threading.Thread(target=hub.run, args=(threading.Event(),), daemon=True, name="hub").start()
        except Exception as error:   # no multicast here: the Matrix works fine without the hub
            logging.info("hub not started: %s", error)
    app = create_app(modules=modules, live=live, pairing=pairing, config_path=config_path, logs=logs,
                     network=network, hub=hub, started=started)
    server = make_server(config["web"]["host"], port, app, threaded=True)
    threading.Thread(target=server.serve_forever, daemon=True, name="dashboard").start()
    logging.info("dashboard on http://%s:%s", config["web"]["host"], port)
    plain = (config["web"].get("port80", True) and port != 80
             and netname.redirect_server(80, port, config["web"]["host"], override=lambda: (
                 f"http://{net.SETUP_ADDRESS}:{port}/setup"
                 if network and network.status.state in ("setup", "joining") else None)) is not None)
    watch = netname.Watch(address, lambda: cfg.load(config_path), port, plain, __version__, started=started)
    threading.Thread(target=watch.run, args=(threading.Event(),), daemon=True, name="network").start()


def _hardware(config):
    from bongo.panel import HardwarePanel, PanelBusy

    try:
        return HardwarePanel(**config["panel"])
    except PanelBusy as error:
        sys.exit(f"bongo: {error}")


PLAYED = 50       # frames a config must show before a failure is no longer blamed on it (a few seconds)...
PLAYED_FOR = 10.0  # ...or seconds it has been playing (a slow screen draws few frames)


def keep_playing(config: dict, path, modules, live, play_config, wait: float = 10) -> None:
    """Play ``config`` until the dashboard saves another (``live.reload``), then that one, for ever.

    If a config can't even start (it fails before it has shown ``PLAYED`` frames), the last one that did
    play is shown instead, so the panel isn't left dark; the saved file is left as it is, so nothing you
    saved is lost (the dashboard still shows it, to be put right). A config that has been playing
    fine and then hits a problem isn't to blame: it is simply started again."""
    good, first = None, True   # the last config that played; the boot screen only plays at start-up
    while True:
        shown, since = live.shown, time.monotonic()
        try:
            play_config(config, first)
            good = config
        except Exception:  # never exit: the dashboard stays up so the config can be fixed
            logging.exception("can't play this config")
            if live.shown - shown >= PLAYED or time.monotonic() - since >= PLAYED_FOR:
                good = config                     # it played: start it again, after a moment
                first = False
                live.reload.wait(1)
            elif good is not None and config is not good:
                logging.error("the saved config can't play: showing the last one that did (the saved "
                              "one is kept, so it can be put right in the dashboard)")
                config, first = good, False
                continue
            else:
                live.beat()           # still alive, just nothing to show: not for the watchdog
                live.reload.wait(wait)  # nothing better to play: wait for a fix from the dashboard
        first = False
        if not live.reload.is_set() and good is config:
            continue                  # (started again after a problem, not a new save)
        live.reload.clear()
        live.skip.clear()
        config = cfg.load(path)
        cfg.tidy(config, modules)
        logging.info("config reloaded")


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
    if args.command == "run":   # first of all, before anything in a half-installed version can stop us getting here
        try:
            from bongo import update

            if update.resume():
                logging.warning("an update was cut short (a power cut?): asked the updater to finish it, or put the old version back")
        except Exception as error:
            logging.warning("couldn't check for an unfinished update: %s", error)
    modules = discover()
    if args.command == "run":
        from bongo import addons
        from bongo.sandbox import remote

        addons.register(modules)   # imported modules (Settings > Modules > Import): each runs in its own child process
        addons.keep_checking()     # ...and a look for newer versions once a day (nothing installs by itself)
        import atexit
        atexit.register(remote.stop_all)

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
    import signal
    # systemctl stop/restart (and updates) send SIGTERM: treat it like Ctrl+C, so the panel is cleared
    # and exit handlers run (the hub says goodbye on mDNS, so another Matrix can take matrix.local)
    signal.signal(signal.SIGTERM, signal.default_int_handler)
    live = Live()
    if config["web"]["enabled"]:
        _start_dashboard(config, modules, live, Path(args.config))
    panel = Fader(_hardware(config))  # day <-> night brightness glides instead of jumping
    import threading
    live.panel_up = True
    live.beat()   # starting up doesn't count against it
    threading.Thread(target=live.watch, args=(threading.Event(),), daemon=True, name="watchdog").start()
    def play_config(config, first):
        schedule = Schedule.from_config(config)
        if not first:
            schedule.boot = None  # the boot screen plays once, not after a dashboard save
        factory = cfg.context_factory(config)
        play(cfg.playlist(config, modules), modules, panel, factory,
             place=cfg.safe_area(config).place, schedule=schedule, live=live,
             overlays=Overlays.from_config(config, modules, factory))

    try:
        keep_playing(config, Path(args.config), modules, live, play_config)
    except KeyboardInterrupt:
        panel.clear()
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
