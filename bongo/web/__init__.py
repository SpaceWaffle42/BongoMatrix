"""Dashboard: a JSON API over the live player plus a static single-page app.

Everything except pairing needs a paired browser (cookie). State-changing requests must be
JSON, and the cookie is SameSite=Strict, so other sites can't drive the panel.
"""
from __future__ import annotations

import base64
import collections
import datetime as dt
import hashlib
import io
import ipaddress
import json
import logging
import math
import os
import shutil
import tempfile
import threading
import time
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo

from flask import Flask, Response, abort, jsonify, request, send_file, send_from_directory
from werkzeug.exceptions import HTTPException

from bongo import animator, config as cfg, logo, net, netname, pets, petpack, update
from bongo import addons, media, places
from bongo.fonts import Fonts, names as font_names, table as font_table
from bongo.module import Context
from bongo.player import Entry
from bongo.schema import describe

STATIC = Path(__file__).with_name("static")
HARDWARE = ("rows", "cols", "hardware_mapping", "gpio_slowdown")  # panel keys fixed until a restart
COOKIE = "bongo_device"
YEAR = 365 * 86400


class LogBuffer(logging.Handler):
    """Keeps the last few hundred log records for the Logs page."""

    def __init__(self, size=500):
        super().__init__(level=logging.DEBUG)
        self.records: collections.deque = collections.deque(maxlen=size)

    def emit(self, record):
        self.records.append({
            "t": record.created, "level": record.levelname, "source": record.name.rsplit(".", 1)[-1],
            "message": record.getMessage(),
            "trace": self.formatter.formatException(record.exc_info) if record.exc_info and self.formatter else
                     (logging.Formatter().formatException(record.exc_info) if record.exc_info else ""),
        })


def media_uses(config: dict):
    """(where, file) for every media file the config refers to, so in-use files aren't deleted."""
    for i, e in enumerate(config.get("playlist") or []):
        if isinstance(e, dict) and e.get("module") == "media":
            yield f"playlist entry {i + 1}", (e.get("settings") or {}).get("file")
    for screen in ("splash", "sleep"):
        for file in ((config.get(screen) or {}).get("sources") or []):
            yield f"the {screen} screen", file
    if boot := (config.get("splash") or {}).get("boot"):
        yield "the boot screen", boot


def _entries(config: dict):
    for i, e in enumerate(config.get("playlist") or []):
        if isinstance(e, dict):
            yield f"playlist[{i}]", e


def verify_new(current: dict, merged: dict, modules: dict) -> list[str]:
    """Run modules' slow checks (e.g. "is this really an RSS feed?") on entries whose settings are
    new in this save. Unchanged entries aren't re-checked, so a feed being down for a while
    doesn't stop you saving something else."""
    key_of = lambda e: (e.get("module"), e.get("style"), json.dumps(e.get("settings") or {}, sort_keys=True))
    seen = {key_of(e) for _, e in _entries(current)}
    jobs = {}
    for where, e in _entries(merged):
        module = modules.get(e.get("module"))
        key = key_of(e)
        if module and module.verify and key not in seen and key not in jobs.values():
            jobs[where] = key
    problems = []
    for where, (mid, style, settings) in jobs.items():
        module = modules[mid]
        merged_settings = module.settings_for(style, json.loads(settings))   # (its style: a setting only it has)
        problems += [f"{where}: {p}" for p in module.verify(merged_settings)]
    return problems


PACK_UPLOAD = 300 * 1024 * 1024  # a pet pack zip from itch.io


def create_app(*, modules, live, pairing, config_path, logs: LogBuffer | None = None,
               fonts=None, started=None, network=None, hub=None) -> Flask:
    app = Flask(__name__, static_folder=None)
    app.config["MAX_CONTENT_LENGTH"] = 64 * 1024  # a whole config is a few KB
    logs = logs or LogBuffer()
    started = started or time.time()
    render_lock = threading.Lock()  # preview styles are cached and keep state between frames
    # the panel driver reads these once when it starts; a saved change waits for a restart
    running_panel = dict(cfg.load(config_path)["panel"])

    def restart_needed(config) -> bool:
        return any(config["panel"].get(k) != running_panel.get(k) for k in HARDWARE)
    last_show = [0.0]

    def body() -> dict:
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            abort(400, "send a JSON object")
        return data

    def number(value, low, high, name):
        try:
            value = float(value)
        except (TypeError, ValueError):
            abort(400, f"{name} must be a number")
        if not math.isfinite(value):
            abort(400, f"{name} must be a number")
        return max(low, min(high, value))

    @app.errorhandler(HTTPException)
    def http_error(error):
        return jsonify(error=error.description if error.code < 500 else "server error"), error.code

    @app.errorhandler(Exception)
    def crash(error):
        logging.getLogger("bongo.web").exception("dashboard request failed: %s", request.path)
        return jsonify(error="Something went wrong on the Matrix; see Logs."), 500

    def load():
        config = cfg.load(config_path)
        cfg.tidy(config, modules)  # an older build's settings never block a save
        return config

    # -- auth -----------------------------------------------------------------------------
    @app.before_request
    def guard():
        path = request.path
        if not path.startswith("/api/"):
            return None
        upload = request.method == "POST" and path in ("/api/media", "/api/pets/import", "/api/animator/import", "/api/addons/upload")
        if upload and request.headers.get("X-Bongo-Upload") != "1":
            abort(415)  # a custom header: a cross-site form can't set it, and fetch would need CORS
        if request.method not in ("GET", "HEAD") and not upload and not request.is_json:
            abort(415)  # cross-site forms can't send JSON, so this is the CSRF check
        if path.startswith("/api/pair"):
            return None
        if path.startswith("/api/setup") and setup_client():
            return None
        if request.method in ("GET", "HEAD") and (path.startswith("/api/public") or path in ("/api/hub", "/api/health")):
            return None   # read-only, nothing private: the hub page, other Matrices and the updater read these
        if pairing.check(request.cookies.get(COOKIE)) is None:
            return jsonify(error="not paired"), 401
        return None

    @app.post("/api/pair/show")
    def pair_show():
        if time.time() - last_show[0] < 5:
            abort(429, "Wait a moment before asking again.")
        last_show[0] = time.time()
        wait = pairing.show_pin(paired=pairing.check(request.cookies.get(COOKIE)) is not None)
        if wait:   # shown again and again with nobody pairing: the panel gets a rest (bongo/pairing.py)
            minutes = math.ceil(wait / 60)
            abort(429, f"The PIN has been shown several times and nobody paired. Try again in {minutes} minute{'s' if minutes > 1 else ''}.")
        return jsonify(ok=True, seconds=120)

    @app.post("/api/pair")
    def pair():
        data = body()
        try:
            token = pairing.pair(str(data.get("pin", ""))[:12], str(data.get("name", "")))
        except PermissionError as error:
            return jsonify(error=str(error)), 403
        response = jsonify(ok=True)
        response.set_cookie(COOKIE, token, max_age=YEAR, httponly=True, samesite="Strict")
        return response

    @app.get("/api/pair/status")
    def pair_status():
        return jsonify(paired=pairing.check(request.cookies.get(COOKIE)) is not None)

    @app.get("/api/health")
    def health():
        # asked by the updater on this device after it restarts the display; nothing private in it
        return jsonify(live.health())

    # -- what's on --------------------------------------------------------------------------
    @app.get("/api/state")
    def state():
        config = load()
        area = cfg.safe_area(config)
        # clock: the Matrix's own time (its time zone, not the phone's), for "Not now · next 20:00" on the playlist
        return jsonify(live=live.status(), panel={"width": area.panel_width, "height": area.panel_height},
                       clock=cfg.context_factory(config)().now().strftime("%H:%M"),
                       safe_area=dict(zip(("top", "right", "bottom", "left"), area.margins)),
                       uptime=round(time.time() - started), name=config.get("name", "Bongo Matrix"),
                       pet_kinds=pets.kinds(), network=network_info(config))

    # -- the public card and the hub (bongo/hub.py): read-only, open to the local network ------------
    @app.get("/api/public")
    def public_card():
        """What the hub shows for this Matrix: never settings, feeds or messages."""
        from bongo import __version__
        from bongo.hub import label
        config = load()
        status = live.status()
        entry = status.get("entry") or {}
        warnings = []
        if network is not None and network.status.state in ("connecting", "setup", "joining"):
            warnings.append({"connecting": "Looking for its Wi-Fi", "setup": "Wi-Fi setup is open",
                             "joining": "Joining a Wi-Fi network"}[network.status.state])
        picture = config["web"].get("hub_picture", True) is not False
        if picture and live.frame_png(public=True) is None:
            warnings.append("No picture yet")
        return jsonify(id=netname.mac_suffix(), name=config.get("name") or netname.DEFAULT_NAME, version=__version__,
                       playing=label(entry["module"], entry.get("style")) if entry.get("module") else None,
                       paused=status.get("paused", False), sleeping=bool(status.get("asleep")),
                       uptime=round(time.time() - started), warnings=warnings, picture=picture,
                       badge=logo.badge_colour(config["web"].get("badge"), netname.mac_suffix()),
                       panel={"width": cfg.safe_area(config).panel_width, "height": cfg.safe_area(config).panel_height})

    @app.get("/api/public/frame.png")
    def public_frame():
        if load()["web"].get("hub_picture", True) is False:
            abort(404)                       # Settings > Device: picture off for the hub
        png = live.frame_png(public=True)   # never the PIN, the setup password or the address screen
        if png is None:
            abort(404)
        response = send_file(io.BytesIO(png), mimetype="image/png", max_age=0)
        response.headers["Access-Control-Allow-Origin"] = "*"   # the hub page shows every Matrix's picture
        return response

    @app.get("/api/hub")
    def hub_listing():
        if hub is None:
            return jsonify(hub=False, leader=None, newest="", matrices=[], available=False)
        return jsonify(available=True, **hub.listing())

    def network_info(config):
        suffix = netname.mac_suffix()
        return {"host": netname.host_name(config.get("name"), suffix), "suffix": suffix,
                "address": netname.address(), "default_name": netname.DEFAULT_NAME,
                "auto_badge": logo.badge_colour(None, suffix)}   # its logo colour on the hub when none is picked

    @lru_cache(maxsize=128)
    def face_uri(kind, coat):
        from bongo.modules.pet.art import face  # the pet module's art, loaded only when asked for
        img = face(kind, coat)
        if img is None:
            return None
        out = io.BytesIO()
        img.save(out, "PNG")
        return "data:image/png;base64," + base64.b64encode(out.getvalue()).decode()

    @app.get("/api/pets")
    def pet_catalogue():
        """The animals there's art for and their coats, each with a little face for the picker."""
        return jsonify(kinds=[{"kind": k, "default": pets.default_coat(k), "collars": pets.collars(k),
                               "coats": [{**c, "face": face_uri(k, c["id"])} for c in pets.coat_names(k)]}
                              for k in pets.kinds()])

    pack_job = {"state": "idle", "message": "", "made": []}   # one pet pack import at a time

    @app.get("/api/pets/import")
    def pet_import_status():
        return jsonify({**pack_job, "has_art": pets.has_art()})

    @app.post("/api/pets/import")
    def pet_import():
        """The owner's own Last Tick pack zip: the Matrix builds the Pet art from it (it's licensed to
        them, so the public build can't ship it). Building takes a minute or two on a Pi."""
        if pack_job["state"] == "working":
            return jsonify(error="A pet pack is already being imported."), 409
        request.max_content_length = PACK_UPLOAD
        upload = request.files.get("file")
        if upload is None or not upload.filename:
            abort(400, "choose the pack's zip file")
        pets.IMPORTED.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(suffix=".zip", dir=pets.IMPORTED.parent)
        with os.fdopen(fd, "wb") as f:
            shutil.copyfileobj(upload.stream, f)
        pack_job.update(state="working", message=f"Building the pet art from {upload.filename}...", made=[])

        def build():
            try:
                made = petpack.import_zip(Path(tmp), pets.IMPORTED)
                pets.reload()
                face_uri.cache_clear()
                pack_job.update(state="done", made=made,
                                message="Imported. The Pet screens use it from the next time they come round.")
                logging.getLogger("bongo.web").info("pet pack imported: %s", "; ".join(made))
            except petpack.PackError as error:
                pack_job.update(state="failed", message=f"Couldn't import {upload.filename}: {error}.")
            except Exception:  # noqa: BLE001 - shown on the dashboard; details in the log
                logging.getLogger("bongo.web").exception("pet pack import failed")
                pack_job.update(state="failed", message="The import failed; see Logs.")
            finally:
                Path(tmp).unlink(missing_ok=True)

        threading.Thread(target=build, name="pet-pack", daemon=True).start()
        return jsonify(pack_job), 202

    @app.get("/api/frame.png")
    def frame():
        # the dashboard's live preview: what's playing, never a covering screen. The PIN in particular
        # must never be on a web page: a browser already paired on another address (http://10.0.0.4
        # vs http://dev-matrix.local are separate sites) would otherwise show it while pairing
        png = live.frame_png(public=True)
        if png is None:
            abort(404)
        return send_file(io.BytesIO(png), mimetype="image/png", max_age=0)

    @app.post("/api/control")
    def control():
        data = body()
        action = data.get("action")
        if action == "next":
            live.skip.set()
        elif action in ("pause", "resume"):
            live.paused = action == "pause"
        elif action == "sleep":
            minutes = number(data.get("minutes", 8 * 60), 1, 7 * 24 * 60, "minutes")
            # a moment in time, not a clock reading: the Pi's own zone may not be the one in the config
            live.sleep_until = dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=minutes)
            live.awake = False
            live.skip.set()
        elif action == "wake":
            live.sleep_until = None
            live.awake = True        # in the sleep hours too: awake until they end (tomorrow night sleeps as usual)
            live.skip.set()
        elif action == "brightness":
            value = data.get("value")
            live.brightness = None if value is None else int(number(value, 1, 100, "brightness"))
        else:
            return jsonify(error=f"unknown action {action!r}"), 400
        return jsonify(live=live.status())

    last_act = {}

    @app.post("/api/act")
    def act():
        """Press one of a module's buttons (``Module.actions``): only while that module is on the panel."""
        data = body()
        wanted, name = data.get("module"), data.get("name")
        module = modules.get(wanted) if isinstance(wanted, str) else None
        if module is None or not isinstance(name, str) or name not in module.actions:
            return jsonify(error="no such button"), 404
        on = live.entry
        if on is None or on.module != module.id:
            return jsonify(error=f"{module.name} isn't on the panel right now"), 409
        at = time.monotonic()
        if at - last_act.get((module.id, name), 0.0) < 0.25 or at - last_act.get(None, 0.0) < 0.1:   # one button held down,
            return jsonify(error="too fast: give it a moment"), 429                                    # or several in turn
        last_act[(module.id, name)] = last_act[None] = at
        live.act(module.id, name)
        return jsonify(ok=True)

    def canvas(data) -> tuple[int, int]:
        """The Animator's canvas size: what the page says it is, else the panel's."""
        area = cfg.safe_area(load())
        width, height = data.get("width", area.panel_width), data.get("height", area.panel_height)
        if isinstance(width, str) and isinstance(height, str) and width.isdigit() and height.isdigit():
            width, height = int(width), int(height)   # an upload's form fields are text
        return animator.side(width, "width"), animator.side(height, "height")

    @app.post("/api/animator")
    def animator_save():
        """Frames from the Animator: saved to the media library as a GIF, or just shown on the
        Matrix for 15 s (``show``) without adding anything to the library."""
        request.max_content_length = 16 * 1024 * 1024
        data = body()
        try:
            width, height = canvas(data)
            gif = animator.gif(data.get("frames"), width, height, data.get("background", "#000000"),
                               data.get("loop", "loop"))
            if data.get("show"):
                name = animator.save_preview(gif)
            else:
                name = animator.save(str(data.get("name", "animation")), gif)
        except ValueError as error:
            return jsonify(error=f"Couldn't make the GIF: {error}."), 422
        if data.get("show"):
            if "media" not in modules:
                return jsonify(error="no media module installed"), 409
            live.push(Entry("media", "pixel", 15, {"file": name}))
            return jsonify(ok=True, name=name, live=live.status())
        logging.getLogger("bongo.web").info("animation saved: %s", name)
        return jsonify(ok=True, name=name), 201

    # projects: the drawing itself, with its layers, to open again (and what modules draw sprites from)
    @app.get("/api/animator/projects")
    def animator_projects():
        return jsonify(projects=animator.projects())

    @app.get("/api/animator/projects/<name>")
    def animator_project(name):
        try:
            return jsonify(animator.load_project(name))
        except FileNotFoundError:
            abort(404, "no such project")
        except ValueError as error:
            return jsonify(error=f"Couldn't open {name}: {error}."), 422

    @app.put("/api/animator/projects/<name>")
    def animator_project_save(name):
        request.max_content_length = animator.MAX_PROJECT + 64 * 1024
        try:
            saved = animator.save_project({**body(), "name": name})
        except ValueError as error:
            return jsonify(error=f"Couldn't save it: {error}."), 422
        except OSError as error:
            logging.getLogger("bongo.web").warning("animator project not saved: %s", error)
            return jsonify(error="Couldn't save it: the Matrix couldn't write the file (its card may be full)."), 507
        logging.getLogger("bongo.web").info("animator project saved: %s", saved)
        return jsonify(ok=True, name=saved)

    @app.delete("/api/animator/projects/<name>")
    def animator_project_delete(name):
        try:
            animator.delete_project(name)
        except PermissionError as error:
            abort(403, str(error))
        except (FileNotFoundError, ValueError):
            abort(404, "no such project")
        logging.getLogger("bongo.web").info("animator project deleted: %s", name)
        return jsonify(ok=True)

    # the Matrix's own fonts, for the Animator's text tool
    @app.get("/api/animator/fonts")
    def animator_fonts():
        return jsonify(fonts=font_names((fonts or Fonts()).directory))

    @app.get("/api/animator/fonts/<name>")
    def animator_font(name):
        if name not in font_names((fonts or Fonts()).directory):
            abort(404, "no such font")
        return jsonify(font_table((fonts or Fonts())(name)))

    # -- Wi-Fi (bongo/net.py; only on a Pi where NetworkManager runs) ------------------------------
    def setup_client() -> bool:
        """A phone on the setup hotspot while setup is open: it may use the setup page unpaired
        (it can't pair: nobody may be near the panel, and the PIN would sit under the setup screens)."""
        if network is None or network.status.state not in ("setup", "joining"):
            return False
        try:
            return ipaddress.ip_address(request.remote_addr or "") in ipaddress.ip_network(net.SETUP_SUBNET)
        except ValueError:
            return False

    @lru_cache(maxsize=1)
    def current_country():
        return net.current_country()   # what Raspberry Pi Imager (or raspi-config) set, asked once

    @app.get("/api/setup")
    def setup_status():
        if network is None:
            return jsonify(managed=False)
        network.used()
        config = load()
        return jsonify(managed=True, **network.status.public(), networks=[vars(n) for n in network.nearby][:20],
                       name=config.get("name") or "", default_name=netname.DEFAULT_NAME,
                       country=config.get("wifi_country") or current_country() or "GB", countries=list(net.COUNTRIES))

    @app.post("/api/setup/join")
    def setup_join():
        if network is None:
            return jsonify(error="Wi-Fi isn't managed on this machine."), 409
        body = request.get_json(silent=True) or {}
        ssid, password = str(body.get("ssid") or ""), str(body.get("password") or "")
        problem = net.valid_join(ssid, password)
        if problem:
            abort(422, problem)
        country = str(body.get("country") or "").upper()
        if country and country not in net.COUNTRIES:
            abort(422, "Choose the Wi-Fi country from the list.")
        network.used()
        if not network.join(ssid, password, bool(body.get("hidden")), country):
            abort(409, "Setup isn't open, or it's already trying a network. Give it a minute.")
        name = str(body.get("name") or "").strip()[:40]
        if name or country:   # "Name this Matrix" is the same as Settings > Display name
            config = load()
            if name:
                config["name"] = name
            if country:
                config["wifi_country"] = country
            cfg.save(config, config_path)
        logging.getLogger("bongo.net").info("setup page: trying %s", ssid)
        return jsonify(network.status.public()), 202

    @app.post("/api/setup/rescan")
    def setup_rescan():
        if network is None or not network.rescan():
            abort(409, "It can look again only while setup is open.")
        network.used()
        return jsonify(network.status.public()), 202

    @app.get("/api/network")
    def network_status():
        if network is None:
            return jsonify(managed=False)
        scan = []
        if network.status.state != "setup":   # the radio is busy being the hotspot during setup
            try:
                scan = [vars(n) for n in network.scan()][:12]
            except RuntimeError:
                pass
        return jsonify(managed=True, **network.status.public(), networks=scan, saved=network.saved())

    @app.post("/api/network/setup")
    def network_setup():
        """Settings > Wi-Fi > Reopen setup: the Matrix leaves its network and opens its setup hotspot."""
        if network is None:
            return jsonify(error="Wi-Fi isn't managed on this machine."), 409
        if not network.reopen():
            abort(409, "It's trying a network from the setup page. Give it a minute.")
        logging.getLogger("bongo.net").info("setup reopened by %s", (pairing.check(request.cookies.get(COOKIE)) or {}).get("name"))
        return jsonify(network.status.public())

    @app.post("/api/animator/import")
    def animator_import():
        """A GIF or picture turned into Animator frames to touch up (nothing is saved)."""
        request.max_content_length = 8 * 1024 * 1024
        upload = request.files.get("file")
        if upload is None or not upload.filename:
            abort(400, "choose a GIF or picture")
        try:
            frames = animator.frames_from(upload.read(), *canvas(request.form))
        except ValueError as error:
            return jsonify(error=f"Couldn't import {upload.filename}: {error}."), 422
        return jsonify(frames=frames)

    # -- linked calendars (bongo/calendars.py): their links never leave the Matrix -----------------
    def calendar_list():
        from bongo import calendars
        try:
            import icalendar  # noqa: F401
            import recurring_ical_events  # noqa: F401
            ready = True
        except ImportError:   # an update that added them hasn't installed them yet
            ready = False
        return jsonify(calendars=[calendars.public(f) for f in calendars.load()], ready=ready,
                       colours=list(calendars.COLOURS), most=calendars.MOST_CALENDARS)

    @app.get("/api/calendars")
    def calendars_get():
        return calendar_list()

    @app.post("/api/calendars")
    def calendars_add():
        from bongo import calendars
        try:
            feed = calendars.add(body())
        except calendars.FeedError as error:
            return jsonify(error=f"Couldn't add it: {error}."), 422
        logging.getLogger("bongo.web").info("calendar %s added", feed["name"])
        return calendar_list()

    @app.patch("/api/calendars/<feed_id>")
    def calendars_change(feed_id):
        from bongo import calendars
        try:
            calendars.change(feed_id, body())
        except KeyError:
            abort(404, "no such calendar")
        except calendars.FeedError as error:
            return jsonify(error=f"Couldn't change it: {error}."), 422
        return calendar_list()

    @app.delete("/api/calendars/<feed_id>")
    def calendars_remove(feed_id):
        from bongo import calendars
        if not calendars.remove(feed_id):
            abort(404, "no such calendar")
        return calendar_list()

    @app.get("/api/calendars/upcoming")
    def calendars_upcoming():
        """Settings > Calendars > Coming up: what every Calendar screen will show, with its colours."""
        from bongo.modules.calendar.preview import coming_up
        config = load()
        tz = ZoneInfo(config["timezone"]) if config.get("timezone") else None
        days = number(request.args.get("days", 56), 1, 120, "days")
        return jsonify(coming_up(config, modules, tz, days=int(days)))

    @app.post("/api/calendars/refresh")
    def calendars_refresh():
        from bongo import calendars
        calendars.refresh_all(force=True)   # in the background, one at a time: the list shows them as waiting
        return calendar_list()

    # -- Spotify sign-in (bongo/modules/spotify/account.py) -------------------------------------
    @app.get("/api/spotify")
    def spotify_status():
        from bongo.modules.spotify import account
        return jsonify(connected=account.connected(), redirect=account.REDIRECT)

    @app.post("/api/spotify/start")
    def spotify_start():
        from bongo.modules.spotify import account
        try:
            return jsonify(url=account.start(str(body().get("client_id", ""))))
        except ValueError as error:
            return jsonify(error=str(error)), 422

    @app.post("/api/spotify/finish")
    def spotify_finish():
        from bongo.modules.spotify import account
        try:
            account.finish(str(body().get("url", "")))
        except ValueError as error:
            return jsonify(error=str(error)), 422
        except OSError:
            return jsonify(error="Couldn't reach Spotify from the Matrix; check its internet connection."), 502
        logging.getLogger("bongo.web").info("spotify connected")
        return jsonify(connected=True)

    @app.delete("/api/spotify")
    def spotify_forget():
        from bongo.modules.spotify import account
        account.forget()
        return jsonify(connected=False)

    @app.post("/api/push")
    def push():
        data = body()
        seconds = number(data.get("seconds", 15), 3, 120, "seconds")
        if data.get("file") is not None:  # "show it on the Matrix": try a media file on the real panel
            name, fit = data["file"], data.get("fit", "fit")
            if "media" not in modules:
                return jsonify(error="no media module installed"), 409
            if fit not in modules["media"].styles:
                return jsonify(error=f"fit: one of {', '.join(modules['media'].styles)}"), 422
            if media.resolve(name) is None or (name.startswith("built-in/") and media.screen_of(name) is None):
                return jsonify(error=f"no such media file: {name}"), 404
            speed = number(data.get("speed", 1), 0.1, 4, "speed")
            live.push(Entry("media", fit, seconds, {"file": name, "speed": speed}))
            logging.getLogger("bongo.web").info("showing %s on the panel for %s s", name, seconds)
            return jsonify(ok=True, live=live.status())
        text = str(data.get("text", "")).strip()[:200]
        if not text:
            return jsonify(error="nothing to show"), 400
        if "announcements" in modules:
            entry = Entry("announcements", "banner", seconds,
                          {"items": [{"title": str(data.get("title", ""))[:40], "text": text}]})
        elif "text" in modules:
            entry = Entry("text", "scroll", seconds, {"text": text})
        else:
            return jsonify(error="no text module installed"), 409
        live.push(entry)
        return jsonify(ok=True, live=live.status())

    # -- modules and config -------------------------------------------------------------------
    @app.get("/api/modules")
    def module_list():
        return jsonify([{**describe(m), **({"imported": True} if addons.is_imported(m) else {})} for m in modules.values()])

    # -- imported modules (Settings > Modules > Import; bongo/addons.py) ------------------------------
    def builtin_ids():
        return [k for k, m in modules.items() if not addons.is_imported(m)]

    def addon_list():
        from bongo.sandbox import gate as gates, jail
        blocked = gates._gate.blocked if gates._gate else {}
        return {"modules": [addons.public(k, info, blocked.get(k)) for k, info in sorted(addons.installed().items())],
                "enforced": jail.can_shut_in()}

    @app.get("/api/addons")
    def addons_get():
        return jsonify(addon_list())

    def one_at_a_time():
        if addons.busy():
            abort(409, "Another module is being checked; wait for it to finish.")

    @app.post("/api/addons/check")
    def addons_check():
        """Fetch a module from a GitHub link, check it and try it on a test screen (the page polls the job)."""
        url = str(body().get("url", "")).strip()
        one_at_a_time()
        try:
            addons.parse_github(url)
        except addons.ImportProblem as error:
            return jsonify(error=str(error)), 422
        return jsonify(addons.start(url, url=url, builtin_ids=builtin_ids()).public()), 202

    @app.post("/api/addons/upload")
    def addons_upload():
        """The same for a .zip someone sent."""
        request.max_content_length = addons.MOST_DOWNLOAD + 64 * 1024
        upload = request.files.get("file")
        if upload is None or not upload.filename:
            abort(400, "choose the module's .zip file")
        one_at_a_time()
        fd, tmp = tempfile.mkstemp(suffix=".zip", dir=addons.staging())
        with os.fdopen(fd, "wb") as f:
            shutil.copyfileobj(upload.stream, f)
        job = addons.start(upload.filename, upload=Path(tmp), upload_name=upload.filename, builtin_ids=builtin_ids())
        return jsonify(job.public()), 202

    @app.post("/api/addons/<module_id>/update")
    def addons_update(module_id):
        """Fetch a newer version of an imported module and try it; installing it is the same as at first."""
        info = addons.installed().get(module_id)
        if info is None:
            abort(404, "no such module")
        url = addons.update_link(info)
        if url is None:
            return jsonify(error="It wasn't installed from a GitHub release or branch: import the new version again."), 422
        one_at_a_time()
        return jsonify(addons.start(url, url=url, builtin_ids=builtin_ids(), update_of=module_id).public()), 202

    @app.post("/api/addons/updates")
    def addons_check_updates():
        """Look for updates now (the Matrix also looks once a day)."""
        addons.check_updates()
        return jsonify(addon_list())

    @app.get("/api/addons/check/<token>")
    def addons_job(token):
        job = addons.JOBS.get(token)
        if job is None:
            abort(404, "that import has expired; start it again")
        return jsonify(job.public())

    @app.delete("/api/addons/check/<token>")
    def addons_cancel(token):
        addons.drop(token)
        return jsonify(ok=True)

    @app.post("/api/addons/install")
    def addons_install():
        data = body()
        job = addons.JOBS.get(str(data.get("token", "")))
        if data.get("trust") is not True:
            return jsonify(error="Tick that you trust its author first."), 422
        if job is None or job.state != "ready":
            return jsonify(error="That import has expired; start it again."), 404
        if (job.manifest or {}).get("author") != data.get("author"):
            return jsonify(error="Tick that you trust its author first."), 422
        updating = job.manifest["id"] in modules
        try:
            info = addons.install(job.token, modules)
        except addons.ImportProblem as error:
            return jsonify(error=str(error)), 409
        preview_style.cache_clear()
        if updating:   # a new version: the panel reloads its screens, so a setting the new one no longer has is
            live.apply_config()   # tidied away (config.tidy) now, rather than that screen being skipped until a restart
        logging.getLogger("bongo.addons").info("module %s installed by %s", job.manifest["id"],
                                               (pairing.check(request.cookies.get(COOKIE)) or {}).get("name"))
        return jsonify({**addon_list(), "installed": job.manifest["id"], "name": info["name"]})

    @app.patch("/api/addons/<module_id>")
    def addons_change(module_id):
        data = body()
        if not isinstance(data.get("only_listed"), bool):
            return jsonify(error="send only_listed: true or false"), 422
        try:
            addons.set_only_listed(module_id, data["only_listed"], modules)
        except KeyError:
            abort(404, "no such module")
        preview_style.cache_clear()
        return jsonify(addon_list())

    @app.delete("/api/addons/<module_id>")
    def addons_remove(module_id):
        """Remove an imported module, and its screens from the playlist and overlays."""
        if module_id not in addons.installed():
            abort(404, "no such module")
        config = load()
        playlist = [e for e in config.get("playlist", []) if not (isinstance(e, dict) and e.get("module") == module_id)]
        overlays = [e for e in config.get("overlays", []) if not (isinstance(e, dict) and e.get("module") == module_id)]
        off = [m for m in config.get("modules_off", []) if m != module_id]
        addons.remove(module_id, modules)
        preview_style.cache_clear()
        changed = {"playlist": playlist, "modules_off": off, **({"overlays": overlays} if "overlays" in config else {})}
        if any(config.get(k) != v for k, v in changed.items()):
            cfg.save({**config, **changed}, config_path)
            live.apply_config()
        return jsonify({**addon_list(), "screens_removed": len(config.get("playlist", [])) - len(playlist)})

    @app.get("/api/config")
    def get_config():
        return jsonify(load())

    def revisions(config) -> dict:
        """A short mark for each section of the config as it is saved now. A page keeps the marks it
        loaded and sends them back with a save, so a save made from an old copy can be told apart."""
        return {key: hashlib.sha1(json.dumps(value, sort_keys=True).encode()).hexdigest()[:12]
                for key, value in config.items()}

    @app.get("/api/config/revisions")
    def get_revisions():
        return jsonify(revisions(load()))

    @app.put("/api/config")
    def put_config():
        current, data = load(), body()
        # "_base": the marks of the sections this page loaded. A section that has changed since (another
        # phone or tab saved it) isn't silently replaced: the page is told, and asks its user. A save
        # without "_base" (the page chose to overwrite; an older page; a script) goes through as before.
        base = data.pop("_base", None)
        if base is not None:
            if not isinstance(base, dict):
                return jsonify(problems=["_base: send an object"]), 422
            now = revisions(current)
            changed = sorted(key for key in data if key in base and base[key] != now.get(key, "-"))
            if changed:
                return jsonify(error="Changed on another device since this page loaded: " + ", ".join(changed),
                               conflict=changed, revisions=now), 409
        for key in ("panel", "safe_area", "web"):   # merged key by key below: never replaced by null or a list
            if key in data and not isinstance(data[key], dict):
                return jsonify(problems=[f"{key}: send an object"]), 422
        if "web" in data:   # only the hub picture and the badge colour can change here; the address stays in data/config.json
            web, now = data["web"], current["web"]
            if any(k not in ("hub_picture", "badge") and (k not in now or v != now[k] or type(v) is not type(now[k]))
                   for k, v in web.items()):
                return jsonify(problems=["web: change the dashboard's address in data/config.json on the Pi"]), 422
            if not isinstance(web.get("hub_picture", True), bool):
                return jsonify(problems=["web: hub_picture must be true or false"]), 422
            if web.get("badge") is not None and web["badge"] not in logo.COLOURS:
                return jsonify(problems=[f"web: badge is one of {', '.join(logo.COLOURS)}, or null for automatic"]), 422
            data = {**data, "web": {k: v for k, v in web.items() if k in ("hub_picture", "badge")}}
        merged = {**current, **data}
        for key in ("panel", "safe_area", "web"):  # these merge key by key, so a page can send just one
            if isinstance(data.get(key), dict):
                merged[key] = {**current[key], **data[key]}
        # only problems this save brings in block it: something already wrong elsewhere in the
        # saved config (fix it on its own page) mustn't stop you saving, say, the safe area
        before = set(cfg.validate(current, modules))
        problems = [p for p in cfg.validate(merged, modules) if p not in before]
        if problems:
            return jsonify(problems=problems), 422
        if merged == current:
            return jsonify(ok=True, restart_needed=restart_needed(current), unchanged=True, revisions=revisions(current))
        problems = verify_new(current, merged, modules)
        if problems:
            return jsonify(problems=problems), 422
        cfg.save(merged, config_path)
        logging.getLogger("bongo.config").info("config saved by %s",
                                               (pairing.check(request.cookies.get(COOKIE)) or {}).get("name"))
        live.apply_config()
        for module_id in set(merged.get("modules_off") or []) - set(current.get("modules_off") or []):
            if addons.is_imported(modules.get(module_id)):   # switched off: an imported module stops running at all
                from bongo.sandbox import remote
                remote.stop(module_id)
        return jsonify(ok=True, restart_needed=restart_needed(merged), revisions=revisions(load()))

    @lru_cache(maxsize=12)
    def preview_style(module, style, settings_json, width, height, pet, home_json="null", zone=None, calendar_json="null",
                      calendars_seen=None):   # calendars_seen: when the linked calendars last changed, so a new copy is drawn
        return modules[module].create(style or None, json.loads(settings_json),
                                      Context(width=width, height=height, fonts=fonts or Context().fonts, pet=pet,
                                              home=json.loads(home_json),    # the Matrix's location, as on the panel
                                              calendar=json.loads(calendar_json),   # ...its calendar colours
                                              tz=ZoneInfo(zone) if zone else None,    # ...and its time zone, not the Pi's
                                              preview=True))

    def calendars_changed():
        """When the linked calendars (their settings, or what was last read from them) last changed."""
        from bongo import calendars
        return tuple(p.stat().st_mtime_ns if p.exists() else 0 for p in (calendars.FILE, calendars.CACHE))

    @app.get("/api/preview.png")
    def preview():
        """One frame of any module/style/settings, drawn by the real module code, placed in the
        configured safe area. ``t`` (epoch seconds) picks the moment, so the page can animate."""
        name = request.args.get("module", "")
        if name not in modules:
            return jsonify(error="unknown module"), 404
        try:
            settings = json.loads(request.args.get("settings", "{}"))
            settings_json = json.dumps(settings, sort_keys=True)
            config = load()
            area = cfg.safe_area(config)
            style = preview_style(name, request.args.get("style", ""), settings_json, area.width, area.height,
                                  pets.of(config), json.dumps(config.get("location"), sort_keys=True),
                                  config.get("timezone") or None, json.dumps(cfg.calendar_look(config), sort_keys=True),
                                  calendars_changed())
        except (ValueError, TypeError, KeyError, OSError) as error:
            return jsonify(error=str(error)), 422
        t = number(request.args.get("t", time.time()), 0, 4_102_444_800, "t")  # up to 2100
        tz = style.ctx.now().tzinfo
        with render_lock:
            img = area.place(style.render(dt.datetime.fromtimestamp(t, tz)))
        out = io.BytesIO()
        img.save(out, "PNG")
        return send_file(io.BytesIO(out.getvalue()), mimetype="image/png", max_age=0)

    @app.get("/api/places")
    def place_search():
        """Real places matching what's typed, for location settings (name -> coordinates)."""
        query = request.args.get("q", "")
        try:
            return jsonify(places=places.search(query))
        except (OSError, ValueError) as error:
            logging.getLogger("bongo.web").warning("place search failed: %s", error)
            return jsonify(error="Couldn't search for places just now; check the Matrix's internet connection."), 502

    # -- devices and logs ---------------------------------------------------------------------
    @app.get("/api/media")
    def media_list():
        lib = media.library()
        return jsonify(folders=lib, used=sum(f["bytes"] for f in lib["uploaded"]),
                       limits={"file": media.MAX_UPLOAD, "library": media.MAX_LIBRARY},
                       types=sorted(media.IMAGES | media.VIDEOS))

    @app.post("/api/media")
    def media_upload():
        request.max_content_length = media.MAX_UPLOAD + 64 * 1024  # this route only: room for one file
        upload = request.files.get("file")
        if upload is None or not upload.filename:
            abort(400, "choose a file to upload")
        try:
            name = media.save_upload(upload.filename, upload.stream)
        except ValueError as error:
            return jsonify(error=f"Couldn't add {upload.filename}: {error}."), 422
        logging.getLogger("bongo.web").info("media uploaded: %s", name)
        return jsonify(ok=True, name=name), 201

    @app.delete("/api/media/<path:name>")
    def media_delete(name):
        in_use = [where for where, file in media_uses(load()) if file == name]
        if in_use:
            return jsonify(error=f"{name} is still used by {', '.join(in_use)}; change that first."), 409
        try:
            media.delete(name)
        except PermissionError as error:
            abort(403, str(error))
        except FileNotFoundError:
            abort(404, "no such file")
        logging.getLogger("bongo.web").info("media deleted: %s", name)
        return jsonify(ok=True)

    @app.get("/api/devices")
    def devices():
        me = pairing.check(request.cookies.get(COOKIE))
        return jsonify([{k: d[k] for k in ("id", "name", "paired", "seen")} | {"me": d["id"] == me["id"]}
                        for d in pairing.devices()])

    @app.delete("/api/devices/<device_id>")
    def remove_device(device_id):
        return jsonify(ok=pairing.remove(device_id))

    @app.get("/api/logs")
    def get_logs():
        since = number(request.args.get("since", 0), 0, 1e12, "since")
        return jsonify([r for r in logs.records if r["t"] > since])

    # -- software updates (bongo/update.py): the dashboard only asks; a separate service installs --
    data_dir = Path(config_path).parent

    @app.get("/api/update")
    def update_info():
        url, branch = update.source(load())
        return jsonify(version=update.version(), url=url, branch=branch, status=update.status(data_dir),
                       default_url=update.DEFAULT_URL, default_branch=update.DEFAULT_BRANCH)

    @app.post("/api/update/check")
    def update_check():
        url, branch = update.source(load())
        here = update.version()
        try:
            newest = update.latest(url, branch)
        except Exception as error:  # offline, wrong address, no such branch...
            return jsonify(error=f"Couldn't reach the update source: {error}"), 502
        return jsonify(latest=newest, current=here and here["commit"], available=not here or newest != here["commit"])

    @app.post("/api/update")
    def update_now():
        if update.busy(data_dir):
            return jsonify(error="An update is already under way"), 409
        from bongo import power
        if power.pending():
            return jsonify(error="The Matrix is restarting: try again when it's back"), 409
        who = (pairing.check(request.cookies.get(COOKIE)) or {}).get("name")
        update.ask(who, data_dir)
        logging.getLogger("bongo.update").info("update requested by %s", who)
        return jsonify(ok=True), 202

    # -- restart (Settings > Device > Restart, behind a confirmation) ---------------------------
    @app.post("/api/restart")
    def restart_now():
        from bongo import power
        what = body().get("what")
        if what not in power.COMMANDS:
            return jsonify(error=f"what: one of {', '.join(power.COMMANDS)}"), 422
        if not power.available():
            return jsonify(error="Restarting only works on the Matrix itself"), 409
        if update.busy(data_dir):
            return jsonify(error="An update is under way: it restarts the Matrix itself when it's done"), 409
        if power.pending():
            return jsonify(error="It's already restarting"), 409
        power.restart(what, (pairing.check(request.cookies.get(COOKIE)) or {}).get("name"))
        return jsonify(ok=True), 202

    # -- the logo (bongo/logo.py): the pages' icon, and a Matrix's badge on the hub. Open to all, like the page --
    @app.get("/logo.png")
    def logo_png():
        size = request.args.get("size", "32")
        size = int(size) if size.isascii() and size.isdigit() and len(size) < 6 else 32
        data = logo.png(request.args.get("style", "normal"), request.args.get("colour"), size,
                        request.args.get("tile", "0") not in ("0", "false", "no"))
        response = send_file(io.BytesIO(data), mimetype="image/png")
        response.headers["Cache-Control"] = "public, max-age=3600"
        response.set_etag(hashlib.sha1(data).hexdigest())
        return response.make_conditional(request)

    # -- the page -----------------------------------------------------------------------------
    @app.get("/")
    @app.get("/<path:path>")
    def page(path="index.html"):
        if path.startswith("api/"):
            abort(404)
        target = STATIC / path
        if path in ("index.html", "setup") and (path == "setup" or setup_client()):
            return send_from_directory(STATIC, "setup.html")   # a phone on the setup hotspot gets the setup page
        if path == "hub" or (path == "index.html" and request.host.split(":")[0].lower() == "matrix.local"):
            return send_from_directory(STATIC, "hub.html")     # http://matrix.local: every Matrix on the network
        if path.startswith("fonts/") and path.endswith(".woff2") and (STATIC / f"{path}.b64").is_file():
            # the dashboard's fonts are kept as base64 text, like the pet art (the repo holds no binary files)
            answer = Response(font_bytes(path), mimetype="font/woff2")
            answer.headers["Cache-Control"] = "public, max-age=604800"
            return answer
        return send_from_directory(STATIC, path if target.is_file() else "index.html")

    @lru_cache(maxsize=8)
    def font_bytes(path: str) -> bytes:
        return base64.b64decode((STATIC / f"{path}.b64").read_text())

    return app
