"""Dashboard: a JSON API over the live player plus a static single-page app.

Everything except pairing needs a paired browser (cookie). State-changing requests must be
JSON, and the cookie is SameSite=Strict, so other sites can't drive the panel.
"""
from __future__ import annotations

import collections
import datetime as dt
import io
import json
import logging
import math
import threading
import time
from functools import lru_cache
from pathlib import Path

from flask import Flask, abort, jsonify, request, send_file, send_from_directory
from werkzeug.exceptions import HTTPException

from bongo import config as cfg
from bongo import media
from bongo.module import Context
from bongo.player import Entry
from bongo.schema import describe

STATIC = Path(__file__).with_name("static")
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


def create_app(*, modules, live, pairing, config_path, logs: LogBuffer | None = None,
               fonts=None, started=None) -> Flask:
    app = Flask(__name__, static_folder=None)
    app.config["MAX_CONTENT_LENGTH"] = 64 * 1024  # a whole config is a few KB
    logs = logs or LogBuffer()
    started = started or time.time()
    render_lock = threading.Lock()  # preview styles are cached and keep state between frames
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
        return cfg.load(config_path)

    # -- auth -----------------------------------------------------------------------------
    @app.before_request
    def guard():
        path = request.path
        if not path.startswith("/api/"):
            return None
        upload = request.method == "POST" and path == "/api/media"
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
                       uptime=round(time.time() - started), name=config.get("name", "Bongo Matrix"))

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

    @app.post("/api/push")
    def push():
        data = body()
        text = str(data.get("text", "")).strip()[:200]
        if not text:
            return jsonify(error="nothing to show"), 400
        seconds = number(data.get("seconds", 15), 3, 120, "seconds")
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
        problems = cfg.validate(merged, modules)
        if problems:
            return jsonify(problems=problems), 422
        if merged == current:
            return jsonify(ok=True, restart_needed=False, unchanged=True)
        problems = verify_new(current, merged, modules)
        if problems:
            return jsonify(problems=problems), 422
        cfg.save(merged, config_path)
        logging.getLogger("bongo.config").info("config saved by %s",
                                               (pairing.check(request.cookies.get(COOKIE)) or {}).get("name"))
        live.apply_config()
        return jsonify(ok=True, restart_needed=False)

    @lru_cache(maxsize=12)
    def preview_style(module, style, settings_json, width, height):
        return modules[module].create(style or None, json.loads(settings_json),
                                      Context(width=width, height=height, fonts=fonts or Context().fonts))

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
            area = cfg.safe_area(load())
            style = preview_style(name, request.args.get("style", ""), settings_json, area.width, area.height)
        except (ValueError, TypeError) as error:
            return jsonify(error=str(error)), 422
        t = number(request.args.get("t", time.time()), 0, 4_102_444_800, "t")  # up to 2100
        tz = style.ctx.now().tzinfo
        with render_lock:
            img = area.place(style.render(dt.datetime.fromtimestamp(t, tz)))
        out = io.BytesIO()
        img.save(out, "PNG")
        return send_file(io.BytesIO(out.getvalue()), mimetype="image/png", max_age=0)

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

    # -- the page -----------------------------------------------------------------------------
    @app.get("/")
    @app.get("/<path:path>")
    def page(path="index.html"):
        if path.startswith("api/"):
            abort(404)
        target = STATIC / path
        return send_from_directory(STATIC, path if target.is_file() else "index.html")

    return app
