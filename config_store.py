"""Persistent, validated configuration for the Matrix display."""

from __future__ import annotations

import copy
import datetime as dt
import json
import math
import os
import re
import tempfile
import threading
from pathlib import Path

from module_options import LAVA_PALETTES
from font_catalog import available_font_names, text_width
from modules.scroll_timing import duration_milliseconds, scroll_frames


class ConfigDurabilityError(OSError):
    """The config is visible after replace, but directory durability is uncertain."""

    def __init__(self, config):
        super().__init__("configuration committed but directory sync failed")
        self.config = copy.deepcopy(config)


class ConfigConflictError(ValueError):
    """A conditional update was based on an older configuration revision."""

    def __init__(self, current_revision):
        super().__init__("configuration revision conflict")
        self.current_revision = current_revision

ALLOWED_MODULES = {
    "clock_lava", "weather", "openweather_overview", "openweather_forecast",
    "announcement_board", "splash", "sleep",
}
PLAYLIST_LIMIT = 256
CONFIG_SNAPSHOT_LIMIT = 1_048_576
AVAILABLE_FONTS = frozenset(available_font_names())


def _serialize_config(config):
    return json.dumps(config, sort_keys=True, separators=(",", ":")).encode("utf-8")


def scrolling_text_duration(module, matrix_width=64):
    """Return the nominal time for every configured pixel traversal."""
    rendered_width = text_width(module["font"], module["text"])
    frames_per_loop = scroll_frames(rendered_width, matrix_width=matrix_width, direction=module["direction"])
    return duration_milliseconds(frames_per_loop, module["loops"], module["speed"]) / 1000

DEFAULT_CONFIG = {
    "revision": 0,
    "device": {"name": "Matrix"},
    "pairing": {"max_devices": 4},
    "display": {
        "day_brightness": 100,
        "night_brightness": 10,
        "active_start": "08:00",
        "active_end": "20:00",
    },
    "weather": {
        "location": "London, England, GB",
        "latitude": 51.5074,
        "longitude": -0.1278,
        "units": "celsius",
        "temperature_y": 17,
        "ticker_y": 31,
        "scroll_direction": "left",
        "scroll_speed": 0.05,
        "scroll_loops": 1,
        "now_x": 1,
        "now_y": 7,
        "temperature_x": 2,
        "high_x": 35,
        "high_y": 8,
        "low_x": 35,
        "low_y": 15,
        "line_y": 20,
        "background_colour": "#000000",
        "text_colour": "#dcd5d5",
        "high_colour": "#ff71ce",
        "low_colour": "#01cdfe",
        "line_colour": "#00ffff",
        "ticker_colour": "#ffb300",
    },
    "openweather_overview": {
        "location": "London, England, GB", "latitude": 51.5074, "longitude": -0.1278,
        "units": "celsius", "icon_x": 0, "icon_y": 0,
        "now_x": 2, "now_y": 22, "temperature_x": 26, "temperature_y": 15,
        "high_x": 50, "high_y": 11, "low_x": 50, "low_y": 20,
        "ticker_y": 28, "scroll_direction": "left", "scroll_speed": 0.05,
        "scroll_loops": 1,
        "background_colour": "#000000", "text_colour": "#dcd5d5",
        "high_colour": "#ff71ce", "low_colour": "#01cdfe",
        "ticker_colour": "#dcd5d5",
    },
    "openweather_forecast": {
        "location": "London, England, GB", "latitude": 51.5074, "longitude": -0.1278,
        "units": "celsius", "forecast_items": 6,
        "icon_x": 0, "icon_y": 0, "time_x": 2, "time_y": 22,
        "temperature_x": 26, "temperature_y": 15,
        "high_x": 50, "high_y": 11, "low_x": 50, "low_y": 20,
        "ticker_y": 28, "scroll_direction": "left", "scroll_speed": 0.05,
        "scroll_loops": 1,
        "background_colour": "#000000", "text_colour": "#dcd5d5",
        "high_colour": "#ff71ce", "low_colour": "#01cdfe",
        "ticker_colour": "#dcd5d5",
    },
    "lava": {
        "enabled_palettes": list(LAVA_PALETTES),
        "custom_enabled": False,
        "custom_colours": ["#000000", "#ff00ff", "#00ffff"],
        "custom_palettes": [],
        "blob_count": 4,
        "blob_min_radius": 5,
        "blob_max_radius": 10,
        "blob_speed": 0.25,
        "field_threshold": 1.0,
        "blur_radius": 2,
        "edge_width": 4,
        "show_time": True,
        "show_date": True,
        "time_font": "9x18B",
        "date_font": "clR6x12",
        "time_align": "center",
        "date_align": "center",
        "time_y": 15,
        "date_y": 27,
        "text_colour": "#ffffff",
        "border_colour": "#000000",
    },
    "media": {"splash": "built-in/squares_tb.gif", "sleep": "built-in/sleep.gif"},
    "media_layout": {
        "splash": {"x": 0, "y": 0, "width": 64, "height": 32, "keep_aspect": True, "video_fps": 20},
        "sleep": {"x": 0, "y": 0, "width": 64, "height": 32, "keep_aspect": True, "video_fps": 20, "gif_speed_percent": 100},
    },
    "announcement_board": {"announcements": []},
    "custom_modules": [],
    "playlist": [
        {"module": "splash", "duration": 8, "enabled": True},
        {"module": "weather", "duration": 15, "enabled": True},
        {"module": "clock_lava", "duration": 300, "enabled": True},
        {"module": "openweather_overview", "duration": 15, "enabled": False},
        {"module": "openweather_forecast", "duration": 15, "enabled": False},
        {"module": "announcement_board", "duration": 15, "enabled": False},
    ],
}


def _merge(base, changes):
    result = copy.deepcopy(base)
    for key, value in changes.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def _validate_time(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{2}:[0-9]{2}", value):
        raise ValueError(f"Invalid time: {value!r}")
    hour, minute = (int(part) for part in value.split(":"))
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        raise ValueError(f"Invalid time: {value!r}")


def _validate_update_keys(changes, schema, prefix=""):
    for key, value in changes.items():
        if key not in schema:
            raise ValueError(f"Unknown configuration key: {prefix}{key}")
        if isinstance(value, dict):
            if not isinstance(schema[key], dict):
                raise ValueError(f"{prefix}{key} must not be an object")
            _validate_update_keys(value, schema[key], f"{prefix}{key}.")


def _is_hex_colour(value):
    return bool(isinstance(value, str) and re.fullmatch(r"#[0-9a-fA-F]{6}", value))


def _validate_frame_delay(value, name):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0.01 <= value <= 1:
        raise ValueError(f"{name} frame delay must be between 0.01 and 1 second")


def _validate_openweather_settings(settings, name, *, forecast=False):
    location = settings["location"]
    if not isinstance(location, str) or not location.strip() or len(location) > 100:
        raise ValueError(f"{name} location must be 1-100 characters")
    if settings["units"] not in {"celsius", "fahrenheit"}:
        raise ValueError(f"{name} units must be celsius or fahrenheit")
    latitude, longitude = settings["latitude"], settings["longitude"]
    if (latitude is None) != (longitude is None):
        raise ValueError(f"{name} latitude and longitude must both be set or both be empty")
    if latitude is not None:
        if isinstance(latitude, bool) or not isinstance(latitude, (int, float)) or not -90 <= latitude <= 90:
            raise ValueError(f"{name} latitude must be between -90 and 90")
        if isinstance(longitude, bool) or not isinstance(longitude, (int, float)) or not -180 <= longitude <= 180:
            raise ValueError(f"{name} longitude must be between -180 and 180")
    if settings["scroll_direction"] not in {"left", "right"}:
        raise ValueError(f"{name} scroll direction must be left or right")
    _validate_frame_delay(settings["scroll_speed"], name)
    loops = settings["scroll_loops"]
    if isinstance(loops, bool) or not isinstance(loops, int) or not 1 <= loops <= 10:
        raise ValueError(f"{name} scroll loops must be between 1 and 10")
    x_fields = ["icon_x", "temperature_x", "high_x", "low_x"]
    y_fields = ["icon_y", "temperature_y", "high_y", "low_y", "ticker_y"]
    if forecast:
        x_fields.append("time_x")
        y_fields.append("time_y")
        for key, low, high in (("forecast_items", 1, 8),):
            value = settings[key]
            if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
                raise ValueError(f"{name} {key} must be between {low} and {high}")
    else:
        x_fields.append("now_x")
        y_fields.append("now_y")
    for key in x_fields:
        value = settings[key]
        if isinstance(value, bool) or not isinstance(value, int) or not -63 <= value <= 63:
            raise ValueError(f"{name} {key} must be between -63 and 63")
    for key in y_fields:
        value = settings[key]
        if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 31:
            raise ValueError(f"{name} {key} must be between 0 and 31")
    for key in (
        "background_colour", "text_colour", "high_colour", "low_colour", "ticker_colour",
    ):
        if not _is_hex_colour(settings[key]):
            raise ValueError(f"{name} {key} must use #RRGGBB format")
        settings[key] = settings[key].lower()


def _is_safe_media_reference(value):
    if not isinstance(value, str):
        return False
    parts = Path(value).parts
    if len(parts) == 1:
        filename = parts[0]
    elif len(parts) == 2 and parts[0] == "built-in":
        filename = parts[1]
    else:
        return False
    return bool(
        re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", filename)
        and Path(filename).suffix.lower() in {".gif", ".png", ".jpg", ".jpeg", ".webp", ".mp4"}
    )


def _validate_canvas_properties(properties, layer_type, *, partial=False):
    fields = {
        "text": {"x", "y", "text", "font", "colour"},
        "time": {"x", "y", "format", "font", "colour"},
        "pixels": {"points", "colour"},
        "line": {"x1", "y1", "x2", "y2", "width", "colour"},
        "rectangle": {"x", "y", "width", "height", "rotation", "colour", "filled"},
        "polygon": {"points", "rotation", "colour", "filled"},
        "circle": {"x", "y", "radius", "colour", "filled"},
        "ellipse": {"x", "y", "radius_x", "radius_y", "rotation", "colour", "filled"},
        "media": {"x", "y", "width", "height", "rotation", "source", "keep_aspect"},
    }.get(layer_type)
    if fields is None:
        raise ValueError("custom canvas layer type is not supported")
    optional_fields = {"rotation"} if layer_type in {"text", "time", "pixels", "line"} else set()
    allowed_fields = fields | optional_fields | {"present", "z_index"}
    if (
        not isinstance(properties, dict)
        or (not partial and (not fields <= set(properties) or not set(properties) <= allowed_fields))
        or (partial and not set(properties) <= allowed_fields)
    ):
        raise ValueError("custom canvas layer properties are invalid")
    for key in ("x", "y", "x1", "y1", "x2", "y2"):
        if key in properties:
            value = properties[key]
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not -128 <= value <= 191:
                raise ValueError(f"custom canvas {key} must be between -128 and 191")
    for key in ("width", "height"):
        if key in properties:
            value = properties[key]
            high = 8 if layer_type == "line" and key == "width" else 128
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not 1 <= value <= high:
                raise ValueError(f"custom canvas {key} must be between 1 and {high}")
    for key in ("radius", "radius_x", "radius_y"):
        if key in properties:
            value = properties[key]
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not 1 <= value <= 128:
                raise ValueError(f"custom canvas {key} must be between 1 and 128")
    if "rotation" in properties:
        value = properties["rotation"]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not -3600 <= value <= 3600:
            raise ValueError("custom canvas rotation must be between -3600 and 3600")
    if "colour" in properties:
        if not _is_hex_colour(properties["colour"]):
            raise ValueError("custom canvas colours must use #RRGGBB format")
        properties["colour"] = properties["colour"].lower()
    if "filled" in properties and not isinstance(properties["filled"], bool):
        raise ValueError("custom canvas filled must be a boolean")
    if "keep_aspect" in properties and not isinstance(properties["keep_aspect"], bool):
        raise ValueError("custom canvas keep_aspect must be a boolean")
    if "present" in properties and not isinstance(properties["present"], bool):
        raise ValueError("custom canvas present must be a boolean")
    if "z_index" in properties:
        value = properties["z_index"]
        if isinstance(value, bool) or not isinstance(value, int) or not -64 <= value <= 64:
            raise ValueError("custom canvas z_index must be an integer between -64 and 64")
    if "source" in properties and not _is_safe_media_reference(properties["source"]):
        raise ValueError("custom canvas media source is invalid")
    if "text" in properties and (
        not isinstance(properties["text"], str)
        or not 1 <= len(properties["text"]) <= 200
        or any(ord(character) < 32 for character in properties["text"])
    ):
        raise ValueError("custom canvas text must be 1-200 printable characters")
    if "font" in properties and properties["font"] not in AVAILABLE_FONTS:
        raise ValueError("custom canvas font is not supported")
    if "format" in properties and properties["format"] not in {"24h", "24h_seconds", "12h", "12h_seconds"}:
        raise ValueError("custom canvas time format is not supported")
    if "points" in properties:
        points = properties["points"]
        minimum = 3 if layer_type == "polygon" else 1
        maximum = 64 if layer_type == "polygon" else 2048
        if not isinstance(points, list) or not minimum <= len(points) <= maximum:
            raise ValueError(f"custom canvas {layer_type} points count is invalid")
        for point in points:
            if (
                not isinstance(point, list) or len(point) != 2
                or any(isinstance(value, bool) or not isinstance(value, (int, float)) or not -128 <= value <= 191 for value in point)
            ):
                raise ValueError("custom canvas points must be numeric x/y pairs")
    return len(properties.get("points", []))


def _validate_canvas_module(item):
    required = {
        "id", "name", "type", "background", "frame_count", "fps",
        "playback", "layers", "overrides",
    }
    if set(item) != required:
        raise ValueError("custom canvas modules have invalid fields")
    if len(json.dumps(item, separators=(",", ":")).encode()) > 1_048_576:
        raise ValueError("custom canvas module must not exceed 1 MiB")
    if not _is_hex_colour(item["background"]):
        raise ValueError("custom canvas background must use #RRGGBB format")
    item["background"] = item["background"].lower()
    frame_count = item["frame_count"]
    if isinstance(frame_count, bool) or not isinstance(frame_count, int) or not 1 <= frame_count <= 120:
        raise ValueError("custom canvas frame_count must be between 1 and 120")
    fps = item["fps"]
    if isinstance(fps, bool) or not isinstance(fps, (int, float)) or not 1 <= fps <= 60:
        raise ValueError("custom canvas fps must be between 1 and 60")
    if item["playback"] not in {"loop", "ping_pong", "hold"}:
        raise ValueError("custom canvas playback must be loop, ping_pong, or hold")
    layers = item["layers"]
    if not isinstance(layers, list) or not 1 <= len(layers) <= 64:
        raise ValueError("custom canvas layers must contain 1-64 items")
    layer_ids = set()
    keyframe_total = 0
    point_total = 0
    for layer in layers:
        if not isinstance(layer, dict) or set(layer) != {"id", "name", "type", "visible", "properties", "keyframes"}:
            raise ValueError("custom canvas layers have invalid fields")
        layer_id = layer["id"]
        if not isinstance(layer_id, str) or not re.fullmatch(r"layer_[a-z0-9_-]{1,40}", layer_id) or layer_id in layer_ids:
            raise ValueError("custom canvas layer ids must be unique safe ids")
        layer_ids.add(layer_id)
        if not isinstance(layer["name"], str) or not 1 <= len(layer["name"].strip()) <= 40:
            raise ValueError("custom canvas layer name must be 1-40 characters")
        if not isinstance(layer["visible"], bool):
            raise ValueError("custom canvas layer visible must be a boolean")
        point_total += _validate_canvas_properties(layer["properties"], layer["type"])
        keyframes = layer["keyframes"]
        if not isinstance(keyframes, list):
            raise ValueError("custom canvas keyframes must be a list")
        keyframe_total += len(keyframes)
        seen_frames = set()
        for keyframe in keyframes:
            if not isinstance(keyframe, dict) or set(keyframe) != {"frame", "easing", "properties"}:
                raise ValueError("custom canvas keyframes have invalid fields")
            frame = keyframe["frame"]
            if isinstance(frame, bool) or not isinstance(frame, int) or not 0 <= frame < frame_count or frame in seen_frames:
                raise ValueError("custom canvas keyframe frame is invalid or duplicated")
            seen_frames.add(frame)
            if keyframe["easing"] not in {"linear", "ease_in", "ease_out", "ease_in_out", "step"}:
                raise ValueError("custom canvas keyframe easing is invalid")
            point_total += _validate_canvas_properties(keyframe["properties"], layer["type"], partial=True)
    if keyframe_total > 512:
        raise ValueError("custom canvas supports at most 512 keyframes")
    overrides = item["overrides"]
    if not isinstance(overrides, list) or len(overrides) > 512:
        raise ValueError("custom canvas overrides must be a list with at most 512 items")
    seen_overrides = set()
    layer_types = {layer["id"]: layer["type"] for layer in layers}
    for override in overrides:
        if not isinstance(override, dict) or set(override) != {"frame", "layer_id", "properties"}:
            raise ValueError("custom canvas overrides have invalid fields")
        frame = override["frame"]
        layer_id = override["layer_id"]
        key = (frame, layer_id)
        if isinstance(frame, bool) or not isinstance(frame, int) or not 0 <= frame < frame_count:
            raise ValueError("custom canvas override frame is invalid")
        if layer_id not in layer_types or key in seen_overrides:
            raise ValueError("custom canvas override layer is invalid or duplicated")
        seen_overrides.add(key)
        point_total += _validate_canvas_properties(override["properties"], layer_types[layer_id], partial=True)
    if point_total > 8192:
        raise ValueError("custom canvas supports at most 8192 aggregate points")


def _announcement_date(value, field):
    if not isinstance(value, str):
        raise ValueError(f"announcement {field} must be an ISO date")
    try:
        parsed = dt.date.fromisoformat(value)
    except ValueError as error:
        raise ValueError(f"announcement {field} must be an ISO date") from error
    if parsed.isoformat() != value:
        raise ValueError(f"announcement {field} must be an ISO date")
    return parsed


def _validate_announcement_board(board, *, allow_legacy=True):
    if not isinstance(board, dict) or set(board) != {"announcements"}:
        raise ValueError("announcement board has invalid fields")
    announcements = board["announcements"]
    if not isinstance(announcements, list) or len(announcements) > 50:
        raise ValueError("announcement board must contain at most 50 announcements")
    seen_ids = set()
    legacy_required = {
        "id", "layout", "headline", "message", "media", "schedule",
        "style", "timing", "transition",
    }
    required = legacy_required | {"enabled", "name"}
    for item in announcements:
        if (
            not isinstance(item, dict)
            or (
                set(item) != required
                if not allow_legacy
                else not legacy_required.issubset(item) or not set(item).issubset(required)
            )
        ):
            raise ValueError("announcement has invalid fields")
        if allow_legacy:
            item.setdefault("enabled", True)
            item.setdefault("name", item["headline"])
        if not isinstance(item["enabled"], bool):
            raise ValueError("announcement enabled must be a boolean")
        if (
            not isinstance(item["name"], str)
            or not 1 <= len(item["name"]) <= 100
            or any(not character.isprintable() for character in item["name"])
        ):
            raise ValueError("announcement name must be 1-100 printable characters")
        item_id = item["id"]
        if (
            not isinstance(item_id, str)
            or not re.fullmatch(r"announcement_[a-z0-9_-]{1,40}", item_id)
            or item_id in seen_ids
        ):
            raise ValueError("announcement ids must be unique safe ids")
        seen_ids.add(item_id)
        if item["layout"] not in {
            "text_notice", "icon_notice", "image_banner", "full_image_notice",
        }:
            raise ValueError("announcement layout is not supported")
        for field, minimum, maximum in (("headline", 1, 100), ("message", 0, 300)):
            value = item[field]
            if (
                not isinstance(value, str)
                or not minimum <= len(value) <= maximum
                or any(not character.isprintable() for character in value)
            ):
                raise ValueError(f"announcement {field} must be {minimum}-{maximum} printable characters")
        if item["media"] is not None and not _is_safe_media_reference(item["media"]):
            raise ValueError("announcement media must be empty or a safe media reference")

        schedule = item["schedule"]
        legacy_schedule_fields = {"start_date", "end_date", "start_time", "end_time"}
        schedule_fields = legacy_schedule_fields | {"days"}
        if (
            not isinstance(schedule, dict)
            or (
                set(schedule) != schedule_fields
                if not allow_legacy
                else not legacy_schedule_fields.issubset(schedule)
                or not set(schedule).issubset(schedule_fields)
            )
        ):
            raise ValueError("announcement schedule has invalid fields")
        if allow_legacy:
            schedule.setdefault("days", [0, 1, 2, 3, 4, 5, 6])
        days = schedule["days"]
        if (
            not isinstance(days, list)
            or not days
            or any(isinstance(day, bool) or not isinstance(day, int) or not 0 <= day <= 6 for day in days)
            or len(days) != len(set(days))
        ):
            raise ValueError("announcement schedule days must be unique weekdays 0-6")
        schedule["days"] = sorted(days)
        start_date = _announcement_date(schedule["start_date"], "start_date")
        end_date = _announcement_date(schedule["end_date"], "end_date")
        if end_date < start_date:
            raise ValueError("announcement end_date must not be before start_date")
        _validate_time(schedule["start_time"])
        _validate_time(schedule["end_time"])
        if schedule["start_time"] == schedule["end_time"]:
            raise ValueError("announcement start_time and end_time must differ")

        style = item["style"]
        if not isinstance(style, dict) or set(style) != {
            "foreground", "background", "font", "alignment", "border", "border_colour",
        }:
            raise ValueError("announcement style has invalid fields")
        for field in ("foreground", "background", "border_colour"):
            if not _is_hex_colour(style[field]):
                raise ValueError(f"announcement {field} must use #RRGGBB format")
            style[field] = style[field].lower()
        if style["font"] not in AVAILABLE_FONTS:
            raise ValueError("announcement font is not supported")
        if style["alignment"] not in {"left", "center", "right"}:
            raise ValueError("announcement alignment is not supported")
        if style["border"] not in {"none", "solid", "inset"}:
            raise ValueError("announcement border is not supported")

        timing = item["timing"]
        if not isinstance(timing, dict):
            raise ValueError("announcement timing must be an object")
        if timing.get("mode") == "scroll":
            if set(timing) != {"mode", "loops", "frame_delay"}:
                raise ValueError("scrolling announcement timing has invalid fields")
            loops = timing["loops"]
            frame_delay = timing["frame_delay"]
            if isinstance(loops, bool) or not isinstance(loops, int) or not 1 <= loops <= 10:
                raise ValueError("scrolling announcement loops must be between 1 and 10")
            if (
                isinstance(frame_delay, bool)
                or not isinstance(frame_delay, (int, float))
                or not 0.01 <= frame_delay <= 1
            ):
                raise ValueError("scrolling announcement frame_delay must be between 0.01 and 1 second")
        else:
            if set(timing) != {"mode", "duration_seconds"}:
                raise ValueError("static announcement timing has invalid fields")
            duration = timing["duration_seconds"]
            if (
                timing["mode"] != "static"
                or isinstance(duration, bool)
                or not isinstance(duration, (int, float))
                or not 1 <= duration <= 3600
            ):
                raise ValueError("static announcement duration must be between 1 and 3600 seconds")
        transition = item["transition"]
        if not isinstance(transition, dict):
            raise ValueError("announcement transition must be an object")
        if transition.get("type") == "cut":
            if transition != {"type": "cut"}:
                raise ValueError("cut announcement transition has invalid fields")
        elif transition.get("type") in {"fade", "pixel_dissolve"}:
            transition_label = transition["type"].replace("_", " ")
            if set(transition) != {"type", "duration_seconds"}:
                raise ValueError(f"{transition_label} announcement transition has invalid fields")
            transition_duration = transition["duration_seconds"]
            if (
                isinstance(transition_duration, bool)
                or not isinstance(transition_duration, (int, float))
                or not 0.1 <= transition_duration <= 3
            ):
                raise ValueError(
                    f"{transition_label} announcement transition duration must be between 0.1 and 3 seconds"
                )
        elif transition.get("type") in {"slide", "wipe"}:
            transition_type = transition["type"]
            if set(transition) != {"type", "direction", "duration_seconds"}:
                raise ValueError(f"{transition_type} announcement transition has invalid fields")
            if transition["direction"] not in {"left", "right"}:
                raise ValueError(
                    f"{transition_type} announcement transition direction must be left or right"
                )
            transition_duration = transition["duration_seconds"]
            if (
                isinstance(transition_duration, bool)
                or not isinstance(transition_duration, (int, float))
                or not 0.1 <= transition_duration <= 3
            ):
                raise ValueError(
                    f"{transition_type} announcement transition duration must be between 0.1 and 3 seconds"
                )
        else:
            raise ValueError("announcement transition is not supported")
        if timing["mode"] == "scroll" and (
            item["layout"] != "text_notice"
            or style["border"] != "none"
            or transition["type"] != "cut"
        ):
            raise ValueError(
                "scrolling announcement must use text_notice, no border, and a cut transition"
            )


def validate_config(config, *, allow_legacy_announcements=True, enforce_save_bounds=False):

    revision = config["revision"]
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
        raise ValueError("revision must be a nonnegative integer")

    name = config["device"]["name"]
    if not isinstance(name, str) or not name.strip() or len(name.strip()) > 40:
        raise ValueError("device name must be 1-40 characters")

    max_devices = config["pairing"]["max_devices"]
    if isinstance(max_devices, bool) or not isinstance(max_devices, int) or not 1 <= max_devices <= 10:
        raise ValueError("pairing max_devices must be an integer from 1 to 10")

    display = config["display"]
    for key in ("day_brightness", "night_brightness"):
        value = display[key]
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 100:
            raise ValueError(f"{key} must be an integer from 1 to 100")
    _validate_time(display["active_start"])
    _validate_time(display["active_end"])

    location = config["weather"]["location"]
    if not isinstance(location, str) or not location.strip() or len(location) > 100:
        raise ValueError("weather location must be 1-100 characters")
    if config["weather"]["units"] not in {"celsius", "fahrenheit"}:
        raise ValueError("weather units must be celsius or fahrenheit")
    latitude = config["weather"]["latitude"]
    longitude = config["weather"]["longitude"]
    if (latitude is None) != (longitude is None):
        raise ValueError("weather latitude and longitude must both be set or both be empty")
    if latitude is not None:
        if isinstance(latitude, bool) or not isinstance(latitude, (int, float)) or not -90 <= latitude <= 90:
            raise ValueError("weather latitude must be between -90 and 90")
        if isinstance(longitude, bool) or not isinstance(longitude, (int, float)) or not -180 <= longitude <= 180:
            raise ValueError("weather longitude must be between -180 and 180")
    if config["weather"]["scroll_direction"] not in {"left", "right"}:
        raise ValueError("weather scroll direction must be left or right")
    _validate_frame_delay(config["weather"]["scroll_speed"], "weather")
    weather_loops = config["weather"]["scroll_loops"]
    if (
        isinstance(weather_loops, bool)
        or not isinstance(weather_loops, int)
        or not 1 <= weather_loops <= 10
    ):
        raise ValueError("weather scroll loops must be between 1 and 10")
    for key in ("now_x", "temperature_x", "high_x", "low_x"):
        value = config["weather"][key]
        if isinstance(value, bool) or not isinstance(value, int) or not -63 <= value <= 63:
            raise ValueError(f"weather {key} must be between -63 and 63")
    for key in ("now_y", "temperature_y", "high_y", "low_y", "line_y", "ticker_y"):
        value = config["weather"][key]
        if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 31:
            raise ValueError(f"weather {key} must be between 0 and 31")
    for key in (
        "background_colour", "text_colour", "high_colour", "low_colour",
        "line_colour", "ticker_colour",
    ):
        if not _is_hex_colour(config["weather"][key]):
            raise ValueError(f"weather {key} must use #RRGGBB format")
        config["weather"][key] = config["weather"][key].lower()

    shared_location = {
        key: config["weather"][key]
        for key in ("location", "latitude", "longitude")
    }
    for module in ("openweather_overview", "openweather_forecast"):
        config[module].update(shared_location)

    _validate_openweather_settings(
        config["openweather_overview"], "openweather overview"
    )
    _validate_openweather_settings(
        config["openweather_forecast"], "openweather forecast", forecast=True
    )

    lava = config["lava"]
    for key, low, high in (
        ("blob_count", 1, 8), ("blob_min_radius", 2, 12),
        ("blob_max_radius", 3, 16), ("blur_radius", 0, 4),
        ("edge_width", 1, 6), ("time_y", 5, 31), ("date_y", 5, 31),
    ):
        value = lava[key]
        if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
            raise ValueError(f"lava {key} must be between {low} and {high}")
    if lava["blob_min_radius"] > lava["blob_max_radius"]:
        raise ValueError("lava blob minimum radius must not exceed maximum radius")
    for key, low, high in (("blob_speed", 0.05, 1.0), ("field_threshold", 0.2, 4.0)):
        value = lava[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not low <= value <= high:
            raise ValueError(f"lava {key} must be between {low} and {high}")
    for key in ("show_time", "show_date"):
        if not isinstance(lava[key], bool):
            raise ValueError(f"lava {key} must be a boolean")
    if lava["time_font"] not in AVAILABLE_FONTS:
        raise ValueError("lava time font is not supported")
    if lava["date_font"] not in AVAILABLE_FONTS:
        raise ValueError("lava date font is not supported")
    for key in ("time_align", "date_align"):
        if lava[key] not in {"left", "center", "right"}:
            raise ValueError(f"lava {key} must be left, center, or right")
    for key in ("text_colour", "border_colour"):
        if not _is_hex_colour(lava[key]):
            raise ValueError(f"lava {key} must use #RRGGBB format")
        lava[key] = lava[key].lower()
    enabled_palettes = lava["enabled_palettes"]
    if not isinstance(enabled_palettes, list) or len(enabled_palettes) != len(set(enabled_palettes)):
        raise ValueError("lava enabled palettes must be a list without duplicates")
    if any(name not in LAVA_PALETTES for name in enabled_palettes):
        raise ValueError("lava enabled palettes contain an unknown palette")
    if not isinstance(lava["custom_enabled"], bool):
        raise ValueError("lava custom enabled must be a boolean")
    colours = lava["custom_colours"]
    if (
        not isinstance(colours, list)
        or len(colours) != 3
        or any(
            not isinstance(colour, str)
            or len(colour) != 7
            or colour[0] != "#"
            or any(character not in "0123456789abcdefABCDEF" for character in colour[1:])
            for colour in colours
        )
    ):
        raise ValueError("lava custom colours must contain three #RRGGBB colours")
    lava["custom_colours"] = [colour.lower() for colour in colours]
    custom_palettes = lava["custom_palettes"]
    if not isinstance(custom_palettes, list) or len(custom_palettes) > 20:
        raise ValueError("lava custom palettes must be a list with at most 20 items")
    custom_palette_ids = set()
    custom_palette_names = set()
    required_palette_keys = {"id", "name", "colours", "enabled"}
    for palette in custom_palettes:
        if not isinstance(palette, dict) or set(palette) != required_palette_keys:
            raise ValueError("lava custom palettes have invalid fields")
        palette_id = palette["id"]
        if not isinstance(palette_id, str) or not re.fullmatch(r"palette_[a-z0-9_-]{1,40}", palette_id):
            raise ValueError("lava custom palette id must start with palette_")
        if palette_id in custom_palette_ids:
            raise ValueError("lava custom palette ids must be unique")
        custom_palette_ids.add(palette_id)
        palette_name = palette["name"].strip() if isinstance(palette["name"], str) else ""
        if (
            not 1 <= len(palette_name) <= 40
            or any(not character.isprintable() for character in palette_name)
        ):
            raise ValueError("lava custom palette name must be 1-40 printable characters")
        normalized_palette_name = palette_name.casefold()
        if normalized_palette_name in custom_palette_names:
            raise ValueError("lava custom palette names must be unique")
        custom_palette_names.add(normalized_palette_name)
        if not isinstance(palette["enabled"], bool):
            raise ValueError("lava custom palette enabled must be a boolean")
        palette_colours = palette["colours"]
        if (
            not isinstance(palette_colours, list)
            or len(palette_colours) != 3
            or any(not _is_hex_colour(colour) for colour in palette_colours)
        ):
            raise ValueError("lava custom palette colours must contain three #RRGGBB colours")
        palette["name"] = palette["name"].strip()
        palette["colours"] = [colour.lower() for colour in palette_colours]
    for key, filename in config["media"].items():
        if not _is_safe_media_reference(filename):
            raise ValueError(
                f"media.{key} must be a safe GIF, PNG, JPEG, WebP, or MP4 media reference"
            )

    for module_name, settings in config["media_layout"].items():
        expected_fields = {"x", "y", "width", "height", "keep_aspect", "video_fps"}
        if module_name == "sleep":
            expected_fields.add("gif_speed_percent")
        if set(settings) != expected_fields:
            raise ValueError(f"media layout {module_name} has invalid fields")
        for key, low, high in (
            ("x", 0, 63), ("y", 0, 31), ("width", 1, 64),
            ("height", 1, 32), ("video_fps", 1, 60),
        ):
            value = settings[key]
            if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
                raise ValueError(f"media layout {module_name} {key} must be between {low} and {high}")
        if not isinstance(settings["keep_aspect"], bool):
            raise ValueError(f"media layout {module_name} keep_aspect must be a boolean")
        if module_name == "sleep":
            speed = settings["gif_speed_percent"]
            if isinstance(speed, bool) or not isinstance(speed, int) or not 25 <= speed <= 400:
                raise ValueError("sleep GIF playback speed must be between 25 and 400 percent")

    _validate_announcement_board(
        config["announcement_board"],
        allow_legacy=allow_legacy_announcements,
    )

    custom_modules = config["custom_modules"]
    if not isinstance(custom_modules, list) or len(custom_modules) > 20:
        raise ValueError("custom modules must be a list with at most 20 items")
    custom_ids = set()
    required_simple_keys = {
        "id", "name", "type", "text", "font", "colour", "background",
        "x", "y", "direction", "speed", "loops",
    }
    for item in custom_modules:
        if not isinstance(item, dict):
            raise ValueError("custom modules must be objects")
        module_id = item.get("id")
        if not isinstance(module_id, str) or not re.fullmatch(r"custom_[a-z0-9_-]{1,40}", module_id):
            raise ValueError("custom module id must start with custom_ and use lowercase letters, numbers, _ or -")
        if module_id in custom_ids:
            raise ValueError("custom module ids must be unique")
        custom_ids.add(module_id)
        if not isinstance(item.get("name"), str) or not 1 <= len(item["name"].strip()) <= 40:
            raise ValueError("custom module name must be 1-40 characters")
        if item.get("type") == "canvas":
            _validate_canvas_module(item)
            continue
        item.setdefault("loops", 1)
        if set(item) != required_simple_keys:
            raise ValueError("custom text modules have invalid fields")
        if item["type"] not in {"text", "text_scroll"}:
            raise ValueError("custom module type must be text or text_scroll")
        if (
            not isinstance(item["text"], str)
            or not 1 <= len(item["text"]) <= 200
            or any(ord(character) < 32 for character in item["text"])
        ):
            raise ValueError("custom module text must be 1-200 printable characters")
        if item["font"] not in AVAILABLE_FONTS:
            raise ValueError("custom module font is not supported")
        if not _is_hex_colour(item["colour"]) or not _is_hex_colour(item["background"]):
            raise ValueError("custom module colours must use #RRGGBB format")
        item["colour"] = item["colour"].lower()
        item["background"] = item["background"].lower()
        if isinstance(item["x"], bool) or not isinstance(item["x"], int) or not -128 <= item["x"] <= 63:
            raise ValueError("custom module x must be between -128 and 63")
        if isinstance(item["y"], bool) or not isinstance(item["y"], int) or not 5 <= item["y"] <= 31:
            raise ValueError("custom module y must be between 5 and 31")
        if item["direction"] not in {"left", "right"}:
            raise ValueError("custom module direction must be left or right")
        speed = item["speed"]
        if isinstance(speed, bool) or not isinstance(speed, (int, float)) or not 0.01 <= speed <= 1:
            raise ValueError("custom module speed must be between 0.01 and 1 second")
        loops = item["loops"]
        if isinstance(loops, bool) or not isinstance(loops, int) or not 1 <= loops <= 10:
            raise ValueError("custom module loops must be between 1 and 10")

    playlist = config["playlist"]
    if not isinstance(playlist, list) or not playlist:
        raise ValueError("playlist must contain at least one module")
    if enforce_save_bounds and len(playlist) > PLAYLIST_LIMIT:
        raise ValueError("playlist must contain at most 256 modules")
    custom_by_id = {item["id"]: item for item in custom_modules}
    for item in playlist:
        if not isinstance(item, dict) or set(item) != {"module", "duration", "enabled"}:
            raise ValueError("playlist items require only module, duration, and enabled")
        if item.get("module") not in ALLOWED_MODULES and item.get("module") not in custom_ids:
            raise ValueError(f"Unknown module: {item.get('module')!r}")
        custom_module = custom_by_id.get(item["module"])
        if item["module"] in {
            "weather", "openweather_overview", "openweather_forecast", "announcement_board",
        }:
            duration = item.get("duration")
            if isinstance(duration, bool) or not isinstance(duration, (int, float)) or not 1 <= duration <= 3600:
                item["duration"] = 15
        elif custom_module is not None and custom_module["type"] == "text_scroll":
            item["duration"] = scrolling_text_duration(custom_module)
        else:
            duration = item.get("duration")
            if isinstance(duration, bool) or not isinstance(duration, (int, float)) or not 1 <= duration <= 3600:
                raise ValueError("module duration must be between 1 and 3600 seconds")
        if not isinstance(item.get("enabled"), bool):
            raise ValueError("module enabled must be a boolean")
    if (
        enforce_save_bounds
        and len(_serialize_config(config)) > CONFIG_SNAPSHOT_LIMIT
    ):
        raise ValueError("canonical configuration snapshot must not exceed 1 MiB")
    return config


def _replace_canvas_media_references(modules, replacements):
    for module in modules if isinstance(modules, list) else []:
        if not isinstance(module, dict) or module.get("type") != "canvas":
            continue
        media_layer_ids = set()
        for layer in module.get("layers", []):
            if not isinstance(layer, dict) or layer.get("type") != "media":
                continue
            media_layer_ids.add(layer.get("id"))
            property_sets = [layer.get("properties", {})]
            property_sets.extend(
                keyframe.get("properties", {})
                for keyframe in layer.get("keyframes", [])
                if isinstance(keyframe, dict)
            )
            for properties in property_sets:
                if isinstance(properties, dict) and properties.get("source") in replacements:
                    properties["source"] = replacements[properties["source"]]
        for override in module.get("overrides", []):
            if not isinstance(override, dict) or override.get("layer_id") not in media_layer_ids:
                continue
            properties = override.get("properties", {})
            if isinstance(properties, dict) and properties.get("source") in replacements:
                properties["source"] = replacements[properties["source"]]


class ConfigStore:
    def __init__(self, path, media_dir=None):
        self.path = Path(path)
        self.media_root = Path(media_dir or Path(__file__).resolve().parent / "resources" / "media")
        self._lock = threading.RLock()
        if not self.path.exists():
            self._write(DEFAULT_CONFIG)
        else:
            current = json.loads(self.path.read_text())
            if not isinstance(current, dict):
                raise ValueError("configuration file must contain a JSON object")
            current_playlist = current.get("playlist")
            if (
                isinstance(current_playlist, list)
                and len(current_playlist) > PLAYLIST_LIMIT
            ) or len(_serialize_config(current)) > CONFIG_SNAPSHOT_LIMIT:
                raise ValueError(
                    "legacy configuration exceeds current save bounds; file was not rewritten"
                )

            legacy_forecast = current.get("openweather_forecast")
            if isinstance(legacy_forecast, dict):
                legacy_forecast.pop("card_seconds", None)
            legacy_speed_delays = {"slow": 0.1, "normal": 0.05, "fast": 0.025}
            for module_name in ("weather", "openweather_overview", "openweather_forecast"):
                module_settings = current.get(module_name)
                if (
                    isinstance(module_settings, dict)
                    and module_settings.get("scroll_speed") in legacy_speed_delays
                ):
                    module_settings["scroll_speed"] = legacy_speed_delays[module_settings["scroll_speed"]]
            current.setdefault("revision", 0)
            _validate_update_keys(current, DEFAULT_CONFIG)
            lava = current.get("lava")
            if (
                isinstance(lava, dict)
                and "custom_palettes" not in lava
                and lava.get("custom_enabled") is True
            ):
                lava["custom_palettes"] = [{
                    "id": "palette_migrated",
                    "name": "My custom palette",
                    "colours": copy.deepcopy(lava.get("custom_colours")),
                    "enabled": True,
                }]
                lava["custom_enabled"] = False
            playlist = current.get("playlist")
            if isinstance(playlist, list):
                existing_modules = {
                    item.get("module") for item in playlist if isinstance(item, dict)
                }
                for item in DEFAULT_CONFIG["playlist"]:
                    if (
                        item["module"] in {
                            "openweather_overview", "openweather_forecast", "announcement_board",
                        }
                        and item["module"] not in existing_modules
                    ):
                        playlist.append(copy.deepcopy(item))
            legacy_built_ins = {
                "squares_tb.gif": "built-in/squares_tb.gif",
                "sleep.gif": "built-in/sleep.gif",
                "hermes_self_portrait.png": "built-in/hermes_self_portrait.png",
            }
            built_in_root = self.media_root / "built-in"
            available_migrations = {}
            if built_in_root.is_dir() and not built_in_root.is_symlink():
                for original, migrated in legacy_built_ins.items():
                    original_path = self.media_root / original
                    migrated_path = self.media_root / migrated
                    if (
                        migrated_path.is_file()
                        and not migrated_path.is_symlink()
                        and not original_path.exists()
                        and not original_path.is_symlink()
                    ):
                        available_migrations[original] = migrated
            media = current.get("media")
            if isinstance(media, dict):
                for slot, filename in list(media.items()):
                    if filename in available_migrations:
                        media[slot] = available_migrations[filename]
            _replace_canvas_media_references(
                current.get("custom_modules", []), available_migrations
            )
            self._write(validate_config(_merge(DEFAULT_CONFIG, current)))

    def _write(self, config):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = _serialize_config(config)
        if len(data) > CONFIG_SNAPSHOT_LIMIT:
            raise ValueError("canonical configuration snapshot must not exceed 1 MiB")
        fd, temp_name = tempfile.mkstemp(prefix=self.path.name, dir=self.path.parent)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, self.path)
            try:
                directory_fd = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            except OSError as error:
                raise ConfigDurabilityError(config) from error
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)

    def get(self):
        with self._lock:
            return json.loads(self.path.read_text())

    def validate_candidate(self, changes):
        """Validate and canonicalize an unsaved draft without persisting it."""
        if not isinstance(changes, dict):
            raise ValueError("configuration candidate must be a JSON object")
        if "revision" in changes:
            raise ValueError("revision is server-owned")
        _validate_update_keys(changes, DEFAULT_CONFIG)
        with self._lock:
            current = self.get()
            candidate = _merge(current, changes)
            candidate["revision"] = current["revision"]
            candidate = validate_config(
                candidate,
                allow_legacy_announcements=False,
                enforce_save_bounds=True,
            )
            return copy.deepcopy(candidate)

    def update(self, changes, *, base_revision=None):
        if not isinstance(changes, dict):
            raise ValueError("configuration update must be a JSON object")

        if "revision" in changes:
            raise ValueError("revision is server-owned")
        if (
            base_revision is not None
            and (isinstance(base_revision, bool) or not isinstance(base_revision, int) or base_revision < 0)
        ):
            raise ValueError("base revision must be a nonnegative integer")

        _validate_update_keys(changes, DEFAULT_CONFIG)
        with self._lock:
            current = self.get()
            if base_revision is not None and base_revision != current["revision"]:
                raise ConfigConflictError(current["revision"])
            updated = _merge(current, changes)
            updated["revision"] = current["revision"] + 1
            updated = validate_config(
                updated,
                allow_legacy_announcements=False,
                enforce_save_bounds=True,
            )
            self._write(updated)
            return copy.deepcopy(updated)
