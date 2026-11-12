"""Dashboard: a JSON API over the live player plus a static single-page app.

Everything except pairing needs a paired browser (cookie). State-changing requests must be
JSON, and the cookie is SameSite=Strict, so other sites can't drive the panel.
"""
from __future__ import annotations

import base64
import collections
import datetime as dt
import io
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

from flask import Flask, abort, jsonify, request, send_file, send_from_directory
from werkzeug.exceptions import HTTPException

from bongo import animator, config as cfg, pets, petpack, update
from bongo import media, places
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
    seen = {(e.get("module"), json.dumps(e.get("settings") or {}, sort_keys=True)) for _, e in _entries(current)}
    jobs = {}
    for where, e in _entries(merged):
        module = modules.get(e.get("module"))
        key = (e.get("module"), json.dumps(e.get("settings") or {}, sort_keys=True))
        if module and module.verify and key not in seen and key not in jobs.values():
            jobs[where] = key
    problems = []
    for where, (mid, settings) in jobs.items():
        module = modules[mid]
        merged_settings = module.settings_for(None, json.loads(settings))
        problems += [f"{where}: {p}" for p in module.verify(merged_settings)]
    return problems


PACK_UPLOAD = 300 * 1024 * 1024  # a pet pack zip from itch.io


def create_app(*, modules, live, pairing, config_path, logs: LogBuffer | None = None,
               fonts=None, started=None) -> Flask:
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
        upload = request.method == "POST" and path in ("/api/media", "/api/pets/import")
        if upload and request.headers.get("X-Bongo-Upload") != "1":
            abort(415)  # a custom header: a cross-site form can't set it, and fetch would need CORS
        if request.method not in ("GET", "HEAD") and not upload and not request.is_json:
            abort(415)  # cross-site forms can't send JSON, so this is the CSRF check
        if path.startswith("/api/pair"):
            return None
        if pairing.check(request.cookies.get(COOKIE)) is None:
            return jsonify(error="not paired"), 401
        return None

    @app.post("/api/pair/show")
    def pair_show():
        if time.time() - last_show[0] < 5:
            abort(429, "Wait a moment before asking again.")
        last_show[0] = time.time()
        pairing.show_pin()
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

    # -- what's on --------------------------------------------------------------------------
    @app.get("/api/state")
    def state():
        config = load()
        area = cfg.safe_area(config)
        return jsonify(live=live.status(), panel={"width": area.panel_width, "height": area.panel_height},
                       safe_area=dict(zip(("top", "right", "bottom", "left"), area.margins)),
                       uptime=round(time.time() - started), name=config.get("name", "Bongo Matrix"),
                       pet_kinds=pets.kinds())

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
        png = live.frame_png()
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
            live.sleep_until = dt.datetime.now() + dt.timedelta(minutes=minutes)
            live.skip.set()
        elif action == "wake":
            live.sleep_until = None
            live.skip.set()
        elif action == "brightness":
            value = data.get("value")
            live.brightness = None if value is None else int(number(value, 1, 100, "brightness"))
        else:
            return jsonify(error=f"unknown action {action!r}"), 400
        return jsonify(live=live.status())

    @app.post("/api/animator")
    def animator_save():
        """Frames from the Animator: saved to the media library as a GIF, or just shown on the
        Matrix for 15 s (``show``) without adding anything to the library."""
        request.max_content_length = 16 * 1024 * 1024
        data = body()
        area = cfg.safe_area(load())
        width, height = area.panel_width, area.panel_height
        try:
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
        return jsonify([describe(m) for m in modules.values()])

    @app.get("/api/config")
    def get_config():
        return jsonify(load())

    @app.put("/api/config")
    def put_config():
        current, data = load(), body()
        if "web" in data:
            return jsonify(problems=["web: change the dashboard's address in data/config.json on the Pi"]), 422
        merged = {**current, **data}
        for key in ("panel", "safe_area"):  # these merge key by key, so a page can send just one
            if isinstance(data.get(key), dict):
                merged[key] = {**current[key], **data[key]}
        # only problems this save brings in block it: something already wrong elsewhere in the
        # saved config (fix it on its own page) mustn't stop you saving, say, the safe area
        before = set(cfg.validate(current, modules))
        problems = [p for p in cfg.validate(merged, modules) if p not in before]
        if problems:
            return jsonify(problems=problems), 422
        if merged == current:
            return jsonify(ok=True, restart_needed=restart_needed(current), unchanged=True)
        problems = verify_new(current, merged, modules)
        if problems:
            return jsonify(problems=problems), 422
        cfg.save(merged, config_path)
        logging.getLogger("bongo.config").info("config saved by %s",
                                               (pairing.check(request.cookies.get(COOKIE)) or {}).get("name"))
        live.apply_config()
        return jsonify(ok=True, restart_needed=restart_needed(merged))

    @lru_cache(maxsize=12)
    def preview_style(module, style, settings_json, width, height, pet):
        return modules[module].create(style or None, json.loads(settings_json),
                                      Context(width=width, height=height, fonts=fonts or Context().fonts, pet=pet))

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
                                  pets.of(config))
        except (ValueError, TypeError) as error:
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
        who = (pairing.check(request.cookies.get(COOKIE)) or {}).get("name")
        update.ask(who, data_dir)
        logging.getLogger("bongo.update").info("update requested by %s", who)
        return jsonify(ok=True), 202

    # -- the page -----------------------------------------------------------------------------
    @app.get("/")
    @app.get("/<path:path>")
    def page(path="index.html"):
        if path.startswith("api/"):
            abort(404)
        target = STATIC / path
        return send_from_directory(STATIC, path if target.is_file() else "index.html")

    return app
