"""The dashboard's side of Preview on the panel (bongo/panelpreview.py): the screen editor puts its unsaved
draft on the real panel, renews that every few seconds, and gives the panel back.

    POST   /api/panel-preview          {entry, index, take_over}   start (409 if someone else is previewing)
    PUT    /api/panel-preview          {token, entry, index}       the draft changed
    POST   /api/panel-preview/renew    {token}                     the page is still there
    DELETE /api/panel-preview          {token}                     stop
    GET    /api/panel-preview                                      whether anyone is previewing

All behind pairing, like every other route that changes what the panel shows. A draft only reaches the panel
if a save would take it (``config.entry_problems``, the check a save makes on each screen); the slow online
checks a save also makes (is this really a news feed?) are left to the save. Nothing here writes the config.
"""
from __future__ import annotations

import dataclasses
import logging

from flask import jsonify, request

from bongo import fonts as font_tools
from bongo import addons, config as cfg
from bongo.panelpreview import Ended, Held, NotYours, Stuck

log = logging.getLogger("bongo.web")

MOST_INDEX = 999   # the place in the page's playlist draft (unsaved additions included), for wording a problem


def register(app, *, modules, live, pairing, load, cookie, fonts=None) -> None:
    preview = live.preview

    def data() -> dict:
        sent = request.get_json(silent=True)
        return sent if isinstance(sent, dict) else {}

    def stopped(style):
        """An imported module's screen whose child process has gone (bongo/sandbox/remote.py): nothing more will come."""
        return "the module stopped. See Logs." if getattr(style, "handle", 0) is None else None

    def build(sent: dict):
        """The draft as a screen ready to draw, as (style, module id, style name, gave_up) or the answer saying why not."""
        entry, index = font_tools.renamed(sent.get("entry")), sent.get("index", 0)   # (a removed font: its replacement)
        if not (isinstance(index, int) and not isinstance(index, bool) and 0 <= index <= MOST_INDEX):
            return None, (jsonify(problems=[f"index: the screen's place in the playlist, a whole number from 0 to {MOST_INDEX}"]), 422)
        where = f"playlist[{index}]"
        problems = cfg.entry_problems(where, entry, modules)
        if problems:
            return None, (jsonify(problems=problems), 422)
        if entry.get("enabled", True) is False:   # (switched off, it never plays: nothing to judge on the panel)
            return None, (jsonify(problems=[f"{where}: this screen is switched off (Rotation). Switch it on to preview it on the panel."]), 422)
        module = modules[entry["module"]]
        if module.id == "splash":   # (the player swaps a Splash file in for it, as bongo/player.py says: its own style is a stand-in)
            return None, (jsonify(error="A Splash screen plays the files picked in Settings > Splash: it has no draft to preview."), 409)
        config = load()
        imported = addons.is_imported(module)
        if imported and module.id in cfg.modules_off(config):
            return None, (jsonify(error="That module is switched off, so it can't be previewed."), 409)   # (off: its code doesn't run at all)
        ctx = cfg.context_factory(config)()      # the size, time zone, pet and location its screen on the panel gets
        ctx = dataclasses.replace(ctx, preview=True, **({"fonts": fonts} if fonts else {}))   # (a preview: the draft itself,
        try:                                     # at once, as the editor's own picture shows it)
            style = module.create(entry.get("style") or None, entry.get("settings") or {}, ctx)
        except (ValueError, TypeError, KeyError, OSError) as error:
            return None, (jsonify(problems=[f"{where}: {error}"]), 422)
        if imported and stopped(style):          # (an imported module's screens run in its own process, here as anywhere)
            return None, (jsonify(error=f"{module.name} didn't start, so it can't be previewed on the panel. See Logs."), 409)
        look = entry.get("style") or module.default_style
        return (style, module.id, look, stopped if imported else None), None

    def over(error: Ended):
        return jsonify(error=str(error), ended=error.reason), 410

    def mine(error: NotYours):
        return jsonify(error=str(error), not_yours=True), 403

    def me() -> str:
        return str((pairing.check(request.cookies.get(cookie)) or {}).get("id") or "")

    @app.after_request
    def unpaired_mid_preview(response):
        """A browser unpaired while previewing: its first renewal is refused (401), and its hold ends there and then."""
        if response.status_code == 401 and request.path.startswith("/api/panel-preview") and request.method != "GET":
            token = data().get("token")
            if isinstance(token, str) and token:
                preview.stop(token)
        return response

    @app.get("/api/panel-preview")
    def panel_preview_status():
        return jsonify(preview=preview.public(), renew=preview.renew_every, lapse=preview.lapse, idle=preview.idle)

    @app.post("/api/panel-preview")
    def panel_preview_start():
        sent = data()
        device = pairing.check(request.cookies.get(cookie)) or {}
        if not isinstance(sent.get("take_over", False), bool):
            return jsonify(problems=["take_over: true or false"]), 422
        built, refused = build(sent)
        if refused:
            return refused
        style, module, look, gave_up = built
        try:
            token = preview.start(str(device.get("id") or ""), str(device.get("name") or "A paired browser"), style,
                                  module, look, take_over=sent.get("take_over") is True, gave_up=gave_up)
        except Held as held:
            return jsonify(error=str(held), held=True, same_browser=held.same, by=held.name), 409
        except Stuck as stuck:
            return jsonify(error=str(stuck)), 503
        log.info("preview on the panel started by %s: %s/%s", device.get("name"), module, look)
        return jsonify(ok=True, token=token, **preview.renew(token))

    @app.put("/api/panel-preview")
    def panel_preview_change():
        sent = data()
        token = str(sent.get("token") or "")
        try:
            who = me()
            preview.renew(token, who=who)         # (a hold that's over, or not this browser's: said before any screen is built)
            built, refused = build(sent)
            if refused:                           # not valid (yet): the panel keeps the last good draft. Still a change,
                preview.renew(token, changed=True, who=who)   # so the five minutes without one start again
                return refused
            style, module, look, gave_up = built
            return jsonify(ok=True, **preview.change(token, style, module, look, gave_up=gave_up, who=who))
        except Ended as error:
            return over(error)
        except NotYours as error:
            return mine(error)

    @app.post("/api/panel-preview/renew")
    def panel_preview_renew():
        try:
            return jsonify(ok=True, **preview.renew(str(data().get("token") or ""), who=me()))
        except Ended as error:
            return over(error)
        except NotYours as error:
            return mine(error)

    @app.delete("/api/panel-preview")
    def panel_preview_stop():
        try:
            return jsonify(ok=True, stopped=preview.stop(str(data().get("token") or ""), who=me()))
        except NotYours as error:
            return mine(error)
