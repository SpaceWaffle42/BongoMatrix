"""Persistent, validated configuration for the Matrix display."""

from __future__ import annotations

import base64
import binascii
import copy
import datetime as dt
import json
import math
import os
import queue
import re
import signal
import stat
import subprocess
import sys
import tempfile
import threading
from decimal import Decimal
from pathlib import Path
from time import monotonic, sleep

from PIL import Image, ImageSequence

from module_options import LAVA_PALETTES
from font_catalog import available_font_names, text_width
from modules.scroll_timing import duration_milliseconds, scroll_frames
from media_resolver import candidate_media_references


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
MEDIA_PLAYLIST_MODULES = {"splash", "sleep"}
PLAYLIST_LIMIT = 256
CONFIG_SNAPSHOT_LIMIT = 1_048_576
CURRENT_CONFIG_SCHEMA_VERSION = 4
MINIMUM_CLIENT_CONTRACT = 3
MEDIA_PLAYLIST_CLIENT_CONTRACT = 4
GROUPED_SCREEN_CLIENT_CONTRACT = 5
AVAILABLE_FONTS = frozenset(available_font_names())
BASIC_BORDER_STYLES = ("none", "solid", "inset", "dotted", "dashed", "double")


def _serialize_config(config):
    return json.dumps(config, sort_keys=True, separators=(",", ":")).encode("utf-8")


def scrolling_text_duration(module, matrix_width=64):
    """Return the nominal time for every configured pixel traversal."""
    rendered_width = text_width(module["font"], module["text"])
    frames_per_loop = scroll_frames(rendered_width, matrix_width=matrix_width, direction=module["direction"])
    return duration_milliseconds(frames_per_loop, module["loops"], module["speed"]) / 1000


def _gif_physical_duration(path, loops, playback_rate):
    """Match gif_media_frames' embedded-delay scaling and 10 ms floor."""
    with Image.open(path) as image:
        one_loop = sum(
            max(0.01, frame.info.get("duration", 100) / 1000.0 / playback_rate)
            for frame in ImageSequence.Iterator(image)
        )
    return one_loop * loops


def _mp4_physical_duration(path, loops, playback_rate, physical_fps=20.0, *, timeout=15.0):
    """Clock the effective decoded source-frame count like show_video."""
    result = subprocess.run(
        [
            "/usr/bin/ffprobe", "-v", "error", "-count_frames",
            "-select_streams", "v:0", "-show_entries",
            "stream=nb_read_frames,nb_frames", "-of", "json", str(path),
        ],
        check=True, capture_output=True, text=True, timeout=timeout,
        env={"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"},
    )
    if len(result.stdout) > 64 * 1024:
        raise ValueError("MP4 metadata is too large")
    metadata = json.loads(result.stdout)
    streams = metadata.get("streams")
    if not isinstance(streams, list) or len(streams) != 1:
        raise ValueError("MP4 frame count is unavailable")
    stream = streams[0]
    frame_count = int(stream.get("nb_read_frames") or stream.get("nb_frames"))
    if frame_count < 1:
        raise ValueError("MP4 frame count is unavailable")
    return frame_count / physical_fps / playback_rate * loops

DEFAULT_CONFIG = {
    "schema_version": CURRENT_CONFIG_SCHEMA_VERSION,
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
    "screen_enabled": {"splash": True, "sleep": True},
    "media": {
        "splash": "built-in/splash_loading_squares.gif",
        "sleep": "built-in/sleep.gif",
    },
    "media_layout": {
        "splash": {"x": 0, "y": 0, "width": 64, "height": 32, "keep_aspect": True, "video_fps": 20, "playback_speed_percent": 100.0},
        "sleep": {"x": 0, "y": 0, "width": 64, "height": 32, "keep_aspect": True, "video_fps": 20, "playback_speed_percent": 100.0},
    },
    "announcement_board": {"announcements": []},
    "custom_modules": [],
    "playlist": [
        {
            "id": "splash_default", "module": "splash",
            "source": "built-in/splash_loading_squares.gif", "duration": 8,
            "sources": ["built-in/splash_loading_squares.gif"],
            "enabled": True,
            "layout": {
                "x": 0, "y": 0, "width": 64, "height": 32,
                "keep_aspect": True, "video_fps": 20,
                "playback_speed_percent": 100.0,
            },
        },
        {"module": "weather", "duration": 15, "enabled": True},
        {"module": "clock_lava", "duration": 300, "enabled": True},
        {"module": "openweather_overview", "duration": 15, "enabled": False},
        {"module": "openweather_forecast", "duration": 15, "enabled": False},
        {"module": "announcement_board", "duration": 15, "enabled": False},
        {
            "id": "sleep_default", "module": "sleep",
            "source": "built-in/sleep.gif", "duration": 60,
            "sources": ["built-in/sleep.gif"],
            "enabled": True,
            "layout": {
                "x": 0, "y": 0, "width": 64, "height": 32,
                "keep_aspect": True, "video_fps": 20,
                "playback_speed_percent": 100.0,
            },
        },
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


def _validate_required_config_keys(config, schema, prefix=""):
    if not isinstance(config, dict):
        raise ValueError(f"current configuration field {prefix or '<root>'} must be an object")
    missing = set(schema) - set(config)
    # Additive switches are optional in existing v4 snapshots; preserve bytes.
    if not prefix:
        missing.discard("screen_enabled")
    if missing:
        names = ", ".join(f"{prefix}{key}" for key in sorted(missing))
        raise ValueError(f"current configuration has missing fields: {names}")
    for key, expected in schema.items():
        if not prefix and key == "screen_enabled" and key not in config:
            continue
        if isinstance(expected, dict):
            _validate_required_config_keys(config[key], expected, f"{prefix}{key}.")


def _is_hex_colour(value):
    return bool(isinstance(value, str) and re.fullmatch(r"#[0-9a-fA-F]{6}", value))


def _validate_frame_delay(value, name):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0.01 <= value <= 1:
        raise ValueError(f"{name} frame delay must be between 0.01 and 1 second")


def _normalize_playback_speed(value, name="media"):
    """Validate and canonicalize one exact percentage-point speed."""
    message = (
        f"{name} playback speed must be 10.0-400.0 percent with at most one decimal place"
    )
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
    ):
        raise ValueError(message)
    decimal_value = Decimal(str(value))
    if (
        not Decimal("10.0") <= decimal_value <= Decimal("400.0")
        or decimal_value * 10 != (decimal_value * 10).to_integral_value()
    ):
        raise ValueError(message)
    return float(decimal_value)


def _validate_media_layout(settings, name):
    expected_fields = {
        "x", "y", "width", "height", "keep_aspect", "video_fps",
        "playback_speed_percent",
    }
    if not isinstance(settings, dict) or set(settings) != expected_fields:
        raise ValueError(f"media layout {name} has invalid fields")
    for key, low, high in (
        ("x", 0, 63), ("y", 0, 31), ("width", 1, 64),
        ("height", 1, 32), ("video_fps", 1, 60),
    ):
        value = settings[key]
        if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
            raise ValueError(f"media layout {name} {key} must be between {low} and {high}")
    if not isinstance(settings["keep_aspect"], bool):
        raise ValueError(f"media layout {name} keep_aspect must be a boolean")
    settings["playback_speed_percent"] = _normalize_playback_speed(
        settings["playback_speed_percent"], name,
    )


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
    if layer_type == "pixels":
        optional_fields.update({"bitmap_rgba", "offset_x", "offset_y"})
    if layer_type == "media":
        optional_fields.add("playback_speed_percent")
    allowed_fields = fields | optional_fields | {"present", "z_index"}
    if not isinstance(properties, dict) or not set(properties) <= allowed_fields:
        raise ValueError("custom canvas layer properties are invalid")
    if not partial:
        if layer_type == "pixels":
            legacy = {"points", "colour"} <= set(properties)
            bitmap = {"bitmap_rgba", "offset_x", "offset_y"} <= set(properties)
            if legacy == bitmap:
                raise ValueError("custom canvas pixels must use exactly one representation")
            if legacy and set(properties) & {"bitmap_rgba", "offset_x", "offset_y"}:
                raise ValueError("custom canvas pixel representations are mutually exclusive")
            if bitmap and set(properties) & {"points", "colour"}:
                raise ValueError("custom canvas pixel representations are mutually exclusive")
        elif not fields <= set(properties):
            raise ValueError("custom canvas layer properties are invalid")
    for key in ("x", "y", "x1", "y1", "x2", "y2", "offset_x", "offset_y"):
        if key in properties:
            value = properties[key]
            numeric = isinstance(value, int) if key in {"offset_x", "offset_y"} else isinstance(value, (int, float))
            if isinstance(value, bool) or not numeric or not -128 <= value <= 191:
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
    if "playback_speed_percent" in properties:
        properties["playback_speed_percent"] = _normalize_playback_speed(
            properties["playback_speed_percent"], "custom canvas media",
        )
    elif layer_type == "media" and not partial:
        properties["playback_speed_percent"] = 100.0
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
    if "bitmap_rgba" in properties:
        value = properties["bitmap_rgba"]
        if not isinstance(value, str):
            raise ValueError("custom canvas bitmap_rgba must be canonical base64")
        try:
            decoded = base64.b64decode(value, validate=True)
        except (binascii.Error, ValueError) as error:
            raise ValueError("custom canvas bitmap_rgba must be canonical base64") from error
        if len(decoded) != 64 * 32 * 4 or base64.b64encode(decoded).decode("ascii") != value:
            raise ValueError("custom canvas bitmap_rgba must encode exactly 8192 bytes")
        for red, green, blue, alpha in zip(decoded[0::4], decoded[1::4], decoded[2::4], decoded[3::4]):
            if alpha not in {0, 255} or (alpha == 0 and (red or green or blue)):
                raise ValueError("custom canvas bitmap_rgba must use canonical transparent or opaque cells")
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
        bitmap_pixels = layer["type"] == "pixels" and "bitmap_rgba" in layer["properties"]
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
            if bitmap_pixels and set(keyframe["properties"]) & {"points", "colour", "bitmap_rgba"}:
                raise ValueError("custom canvas bitmap keyframes may only change placement and common properties")
            if "bitmap_rgba" in keyframe["properties"]:
                raise ValueError("custom canvas bitmap_rgba is only valid in base properties")
    if keyframe_total > 512:
        raise ValueError("custom canvas supports at most 512 keyframes")
    overrides = item["overrides"]
    if not isinstance(overrides, list) or len(overrides) > 512:
        raise ValueError("custom canvas overrides must be a list with at most 512 items")
    seen_overrides = set()
    layer_types = {layer["id"]: layer["type"] for layer in layers}
    bitmap_layers = {
        layer["id"] for layer in layers
        if layer["type"] == "pixels" and "bitmap_rgba" in layer["properties"]
    }
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
        if layer_id in bitmap_layers and set(override["properties"]) & {"points", "colour", "bitmap_rgba"}:
            raise ValueError("custom canvas bitmap overrides may only change placement and common properties")
        if "bitmap_rgba" in override["properties"]:
            raise ValueError("custom canvas bitmap_rgba is only valid in base properties")
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
    required = legacy_required | {"enabled", "name", "playback_speed_percent"}
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
            item.setdefault("playback_speed_percent", 100.0)
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
        item["playback_speed_percent"] = _normalize_playback_speed(
            item["playback_speed_percent"], "announcement media",
        )

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
            "foreground", "background", "headline_font", "message_font",
            "alignment", "border", "border_colour",
        }:
            raise ValueError("announcement style has invalid fields")
        for field in ("foreground", "background", "border_colour"):
            if not _is_hex_colour(style[field]):
                raise ValueError(f"announcement {field} must use #RRGGBB format")
            style[field] = style[field].lower()
        for field in ("headline_font", "message_font"):
            if style[field] not in AVAILABLE_FONTS:
                raise ValueError(f"announcement {field} is not supported")
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


def validate_config(
    config, *, allow_legacy_announcements=True, allow_missing_custom_loops=True,
    enforce_save_bounds=False,
):

    schema_version = config["schema_version"]
    if (
        isinstance(schema_version, bool)
        or not isinstance(schema_version, int)
        or schema_version != CURRENT_CONFIG_SCHEMA_VERSION
    ):
        raise ValueError("schema_version must identify the current configuration schema")

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

    screen_enabled = config.get("screen_enabled", {"splash": True, "sleep": True})
    if (
        not isinstance(screen_enabled, dict)
        or set(screen_enabled) != {"splash", "sleep"}
        or any(not isinstance(value, bool) for value in screen_enabled.values())
    ):
        raise ValueError("screen_enabled requires independent splash and sleep booleans")

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
        _validate_media_layout(settings, module_name)

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
        "border", "border_colour", "x", "y", "direction", "speed", "loops",
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
        if item.get("type") == "media":
            required_media_keys = {
                "id", "name", "type", "source", "fit", "x", "y",
                "width", "height", "loops", "playback_speed_percent",
            }
            if set(item) != required_media_keys:
                raise ValueError("custom media modules have invalid fields")
            if (
                not _is_safe_media_reference(item["source"])
                or Path(item["source"]).suffix.lower() not in {".gif", ".mp4"}
            ):
                raise ValueError("custom media source must be a GIF or MP4")
            if item["fit"] not in {"contain", "cover", "stretch", "manual"}:
                raise ValueError("custom media fit must be contain, cover, stretch, or manual")
            for key, low, high in (
                ("x", -128, 63), ("y", -64, 31),
                ("width", 1, 192), ("height", 1, 96),
            ):
                value = item[key]
                if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
                    raise ValueError(f"custom media {key} must be between {low} and {high}")
            loops = item["loops"]
            if isinstance(loops, bool) or not isinstance(loops, int) or not 1 <= loops <= 10:
                raise ValueError("custom media loops must be between 1 and 10")
            item["playback_speed_percent"] = _normalize_playback_speed(
                item["playback_speed_percent"], "custom media",
            )
            continue
        if allow_missing_custom_loops:
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
        if (
            not _is_hex_colour(item["colour"])
            or not _is_hex_colour(item["background"])
            or not _is_hex_colour(item["border_colour"])
        ):
            raise ValueError("custom module colours must use #RRGGBB format")
        item["colour"] = item["colour"].lower()
        item["background"] = item["background"].lower()
        item["border_colour"] = item["border_colour"].lower()
        if item["border"] not in BASIC_BORDER_STYLES:
            raise ValueError("custom module border is not supported")
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
    media_item_ids = set()
    first_media_items = {}
    for item in playlist:
        if not isinstance(item, dict):
            raise ValueError("playlist items must be objects")
        module_name = item.get("module")
        if module_name in {"splash", "sleep"}:
            if "duration" not in item:
                raise ValueError("module duration is required")
            expected = {
                "id", "module", "source", "sources", "duration", "enabled", "layout",
            }
            if set(item) != expected:
                raise ValueError("splash and sleep playlist items have invalid fields")
            item_id = item.get("id")
            if (
                not isinstance(item_id, str)
                or not re.fullmatch(rf"{module_name}_[a-z0-9_-]{{1,40}}", item_id)
            ):
                raise ValueError(f"{module_name} playlist item id is invalid")
            if item_id in media_item_ids:
                raise ValueError("splash and sleep playlist item ids must be unique")
            media_item_ids.add(item_id)
            sources = item.get("sources")
            if not isinstance(sources, list) or len(sources) != len(set(sources)):
                raise ValueError(
                    f"{module_name} playlist sources must be a list of unique media references"
                )
            if not sources:
                if item.get("enabled") is not False or item.get("source") != "":
                    raise ValueError(f"enabled {module_name} screen requires selected media")
            elif any(not _is_safe_media_reference(source) for source in sources):
                raise ValueError(
                    f"{module_name} playlist sources must contain only safe media references"
                )
            else:
                item["source"] = sources[0]
            _validate_media_layout(item.get("layout"), f"playlist item {item_id}")
            first_media_items.setdefault(module_name, item)
        elif set(item) != {"module", "duration", "enabled"}:
            raise ValueError("playlist items require only module, duration, and enabled")
        if item.get("module") not in ALLOWED_MODULES and item.get("module") not in custom_ids:
            raise ValueError(f"Unknown module: {item.get('module')!r}")
        custom_module = custom_by_id.get(item["module"])
        if item["module"] in {
            "weather", "openweather_overview", "openweather_forecast", "announcement_board",
        }:
            duration = item.get("duration")
            if isinstance(duration, bool) or not isinstance(duration, (int, float)) or not 1 <= duration <= 3600:
                if enforce_save_bounds:
                    raise ValueError("module duration must be between 1 and 3600 seconds")
                item["duration"] = 15
        elif custom_module is not None and custom_module["type"] == "text_scroll":
            item["duration"] = scrolling_text_duration(custom_module)
        elif custom_module is not None and custom_module["type"] == "media":
            duration = item.get("duration")
            if (
                isinstance(duration, bool)
                or not isinstance(duration, (int, float))
                or not math.isfinite(duration)
                or not 0 < duration <= 3600
            ):
                raise ValueError("module duration must be greater than zero and at most 3600 seconds")
        else:
            duration = item.get("duration")
            if isinstance(duration, bool) or not isinstance(duration, (int, float)) or not 1 <= duration <= 3600:
                raise ValueError("module duration must be between 1 and 3600 seconds")
        if not isinstance(item.get("enabled"), bool):
            raise ValueError("module enabled must be a boolean")
    # Scalar slots remain backwards-compatible aliases for the first row.
    # ConfigStore synchronizes playlist-aware writes into these aliases before
    # validation; direct legacy callers can still update only the scalar slot.
    for module_name, item in first_media_items.items():
        if item["sources"]:
            item["source"] = config["media"][module_name]
            item["sources"] = [
                config["media"][module_name],
                *(source for source in item["sources"] if source != config["media"][module_name]),
            ]
        item["layout"] = copy.deepcopy(config["media_layout"][module_name])
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


def _synchronize_media_playlist_changes(
    candidate, changes, current, *, client_contract,
):
    """Reconcile v3 scalar writes and v4 playlist-aware writes."""
    legacy_contract = (
        client_contract is not None
        and client_contract < MEDIA_PLAYLIST_CLIENT_CONTRACT
    )
    modern_contract = (
        client_contract is not None
        and client_contract >= MEDIA_PLAYLIST_CLIENT_CONTRACT
    )
    current_media_rows = {
        module_name: [
            copy.deepcopy(item) for item in current["playlist"]
            if isinstance(item, dict) and item.get("module") == module_name
        ]
        for module_name in MEDIA_PLAYLIST_MODULES
    }
    if legacy_contract:
        for module_name in MEDIA_PLAYLIST_MODULES:
            candidate["media"][module_name] = current["media"][module_name]
            candidate["media_layout"][module_name] = copy.deepcopy(
                current["media_layout"][module_name]
            )
    media_counts = {module_name: 0 for module_name in MEDIA_PLAYLIST_MODULES}
    legacy_modules = set()
    rich_candidate_modules = set()
    for index, item in enumerate(candidate["playlist"]):
        module_name = item.get("module") if isinstance(item, dict) else None
        if module_name not in MEDIA_PLAYLIST_MODULES:
            continue
        media_counts[module_name] += 1
        if legacy_contract:
            legacy_item = {
                key: copy.deepcopy(item[key])
                for key in ("module", "duration", "enabled")
                if key in item
            }
            current_rows = current_media_rows[module_name]
            occurrence = media_counts[module_name] - 1
            if occurrence < len(current_rows):
                upgraded = copy.deepcopy(current_rows[occurrence])
                upgraded.update(legacy_item)
            else:
                upgraded = legacy_item
                upgraded.update({
                    "id": f"{module_name}_{media_counts[module_name]}",
                    "source": current["media"][module_name],
                    "sources": [current["media"][module_name]],
                    "layout": copy.deepcopy(current["media_layout"][module_name]),
                })
            candidate["playlist"][index] = upgraded
            legacy_modules.add(module_name)
            continue
        if (
            modern_contract
            and isinstance(item.get("sources"), list)
            and item["sources"]
            and item.get("source") != item["sources"][0]
        ):
            raise ValueError(
                "playlist media source must be the first checked source"
            )
        if not legacy_contract and set(item) != {"module", "duration", "enabled"}:
            rich_candidate_modules.add(module_name)
            continue
        legacy_modules.add(module_name)
        current_rows = current_media_rows[module_name]
        occurrence = media_counts[module_name] - 1
        if occurrence < len(current_rows):
            upgraded = copy.deepcopy(current_rows[occurrence])
            upgraded.update(item)
        else:
            upgraded = copy.deepcopy(item)
            upgraded.update({
                "id": f"{module_name}_{media_counts[module_name]}",
                "source": candidate["media"][module_name],
                "sources": [candidate["media"][module_name]],
                "layout": copy.deepcopy(candidate["media_layout"][module_name]),
            })
        candidate["playlist"][index] = upgraded
    if modern_contract:
        for module_name, count in media_counts.items():
            if count == 0:
                raise ValueError(
                    f"playlist must contain at least one {module_name} row"
                )
    hidden_rows = []
    preserved_modules = (
        MEDIA_PLAYLIST_MODULES - rich_candidate_modules
        if (
            (legacy_contract and "playlist" in changes)
            or (client_contract is None and legacy_modules)
        )
        else set()
    )
    for module_name in preserved_modules:
        represented = media_counts[module_name]
        occurrence = 0
        for current_index, item in enumerate(current["playlist"]):
            if not isinstance(item, dict) or item.get("module") != module_name:
                continue
            if occurrence >= represented:
                hidden_rows.append((current_index, copy.deepcopy(item)))
            occurrence += 1
    for current_index, item in sorted(hidden_rows, key=lambda entry: entry[0]):
        candidate["playlist"].insert(min(current_index, len(candidate["playlist"])), item)
    for module_name in MEDIA_PLAYLIST_MODULES:
        if legacy_contract and current_media_rows[module_name]:
            first_current = current_media_rows[module_name][0]
            if first_current.get("sources"):
                candidate["media"][module_name] = first_current["source"]
        item = next(
            (
                entry for entry in candidate["playlist"]
                if isinstance(entry, dict) and entry.get("module") == module_name
            ),
            None,
        )
        if item is None:
            continue
        if (
            not legacy_contract
            and "playlist" in changes
            and module_name in rich_candidate_modules
            and "source" in item
            and "layout" in item
        ):
            # Disabled rows may intentionally have no selected media. Preserve
            # the valid legacy alias instead of copying an invalid empty scalar.
            if item.get("sources"):
                candidate["media"][module_name] = item["source"]
            candidate["media_layout"][module_name] = copy.deepcopy(item["layout"])
            continue
        if (
            module_name in changes.get("media", {})
            and (not legacy_contract or not current_media_rows[module_name])
        ):
            source = candidate["media"][module_name]
            item["source"] = source
            item["sources"] = (
                [source, *[value for value in item["sources"] if value != source]]
                if legacy_contract else [source]
            )
        if module_name in changes.get("media_layout", {}):
            item["layout"] = copy.deepcopy(candidate["media_layout"][module_name])
    if client_contract in {MINIMUM_CLIENT_CONTRACT, MEDIA_PLAYLIST_CLIENT_CONTRACT}:
        for screen in MEDIA_PLAYLIST_MODULES:
            current_ids = [
                item.get("id") for item in current["playlist"]
                if isinstance(item, dict) and item.get("module") == screen
            ]
            candidate_ids = [
                item.get("id") for item in candidate["playlist"]
                if isinstance(item, dict) and item.get("module") == screen
            ]
            if len(current_ids) <= 1 and len(candidate_ids) > 1:
                raise ValueError(
                    f"new {screen} screen state must contain exactly one row"
                )
            if (
                len(current_ids) > 1
                and all(isinstance(item_id, str) for item_id in candidate_ids)
                and (
                    len(candidate_ids) != len(current_ids)
                    or set(candidate_ids) != set(current_ids)
                )
            ):
                raise ValueError(
                    f"legacy {screen} screen row identities must be preserved"
                )
    if client_contract == GROUPED_SCREEN_CLIENT_CONTRACT:
        for screen in MEDIA_PLAYLIST_MODULES:
            current_ids = {
                item.get("id") for item in current["playlist"]
                if isinstance(item, dict) and item.get("module") == screen
            }
            candidate_ids = {
                item.get("id") for item in candidate["playlist"]
                if isinstance(item, dict) and item.get("module") == screen
            }
            if not current_ids.issubset(candidate_ids):
                raise ValueError(
                    f"legacy {screen} screen row identities must be preserved"
                )
    return candidate


def _validate_client_contract(client_contract):
    if client_contract is None:
        return
    if (
        isinstance(client_contract, bool)
        or not isinstance(client_contract, int)
        or client_contract not in {
            MINIMUM_CLIENT_CONTRACT, MEDIA_PLAYLIST_CLIENT_CONTRACT,
            GROUPED_SCREEN_CLIENT_CONTRACT,
        }
    ):
        raise ValueError("client contract is invalid")


class ConfigStore:
    METADATA_PROBE_BUDGET_SECONDS = 15.0
    _test_worker_started = None
    _test_before_media_commit_check = None
    _metadata_reaper_threads = ()
    _metadata_reaper_queues = ()
    _metadata_reaper_index = 0
    _metadata_reaper_emergency = {}
    _metadata_reaper_lock = threading.Lock()
    _METADATA_WORKER_SCRIPT = r'''
import json
import subprocess
import sys
from PIL import Image, ImageSequence

path, suffix = sys.argv[1], sys.argv[2]
descriptor = int(path.rsplit("/", 1)[-1])
if suffix == ".gif":
    with Image.open(path) as image:
        value = [frame.info.get("duration", 100) / 1000.0
                 for frame in ImageSequence.Iterator(image)]
    result = ["gif", value]
elif suffix == ".mp4":
    completed = subprocess.run(
        ["/usr/bin/ffprobe", "-v", "error", "-count_frames",
         "-select_streams", "v:0", "-show_entries",
         "stream=nb_read_frames,nb_frames", "-of", "json", path],
        check=True, capture_output=True, text=True,
        # Keep ffprobe in this worker's process group so the parent can kill
        # the complete probe tree, and preserve the already-open source fd.
        pass_fds=(descriptor,), close_fds=True,
        env={"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"},
    )
    if len(completed.stdout) > 65536:
        raise ValueError("MP4 metadata is too large")
    streams = json.loads(completed.stdout).get("streams")
    if not isinstance(streams, list) or len(streams) != 1:
        raise ValueError("MP4 frame count is unavailable")
    count = int(streams[0].get("nb_read_frames") or streams[0].get("nb_frames"))
    if count < 1:
        raise ValueError("MP4 frame count is unavailable")
    result = ["mp4", count]
else:
    raise ValueError("unsupported metadata source")
sys.stdout.write(json.dumps(result, separators=(",", ":")))
'''

    def __init__(self, path, media_dir=None):
        self.path = Path(path)
        self.media_root = Path(media_dir or Path(__file__).resolve().parent / "resources" / "media")
        self._lock = threading.RLock()
        self._metadata_workers = set()
        if not self.path.exists():
            self._write(DEFAULT_CONFIG)
        else:
            current = json.loads(self.path.read_text())
            if not isinstance(current, dict):
                raise ValueError("configuration file must contain a JSON object")
            schema_version = current.get("schema_version", 0)
            if (
                isinstance(schema_version, bool)
                or not isinstance(schema_version, int)
                or schema_version < 0
            ):
                raise ValueError("configuration schema_version must be a nonnegative integer")
            if schema_version > CURRENT_CONFIG_SCHEMA_VERSION:
                raise ValueError("configuration schema_version is not supported")
            if schema_version == CURRENT_CONFIG_SCHEMA_VERSION:
                _validate_update_keys(current, DEFAULT_CONFIG)
                _validate_required_config_keys(current, DEFAULT_CONFIG)
                validate_config(
                    copy.deepcopy(current),
                    allow_legacy_announcements=False,
                    allow_missing_custom_loops=False,
                    enforce_save_bounds=True,
                )
                return
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
            if (
                isinstance(current["revision"], bool)
                or not isinstance(current["revision"], int)
                or current["revision"] < 0
            ):
                raise ValueError("revision must be a nonnegative integer")
            if schema_version <= 1:
                for custom_module in current.get("custom_modules", []):
                    if isinstance(custom_module, dict) and custom_module.get("type") != "canvas":
                        custom_module.setdefault("border", "none")
                        custom_module.setdefault("border_colour", "#ffffff")
            if schema_version <= 2:
                media_layout = current.get("media_layout")
                if isinstance(media_layout, dict):
                    for settings in media_layout.values():
                        if isinstance(settings, dict):
                            settings.pop("gif_speed_percent", None)
                            settings["playback_speed_percent"] = 100.0
                board = current.get("announcement_board")
                if isinstance(board, dict):
                    for announcement in board.get("announcements", []):
                        if isinstance(announcement, dict):
                            announcement.setdefault("playback_speed_percent", 100.0)
                            style = announcement.get("style")
                            if isinstance(style, dict) and "font" in style:
                                legacy_font = style.pop("font")
                                style["headline_font"] = legacy_font
                                style["message_font"] = legacy_font
                for module in current.get("custom_modules", []):
                    if not isinstance(module, dict) or module.get("type") != "canvas":
                        continue
                    media_layer_ids = set()
                    for layer in module.get("layers", []):
                        if not isinstance(layer, dict) or layer.get("type") != "media":
                            continue
                        media_layer_ids.add(layer.get("id"))
                        property_sets = [layer.get("properties")]
                        property_sets.extend(
                            keyframe.get("properties")
                            for keyframe in layer.get("keyframes", [])
                            if isinstance(keyframe, dict)
                        )
                        for properties in property_sets:
                            if isinstance(properties, dict):
                                properties.setdefault("playback_speed_percent", 100.0)
                    for override in module.get("overrides", []):
                        if (
                            isinstance(override, dict)
                            and override.get("layer_id") in media_layer_ids
                            and isinstance(override.get("properties"), dict)
                        ):
                            override["properties"].setdefault("playback_speed_percent", 100.0)
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
            if schema_version <= 3 and isinstance(playlist, list):
                media = current.get("media", {})
                media_layout = current.get("media_layout", {})
                media_counts = {"splash": 0, "sleep": 0}
                for item in playlist:
                    if not isinstance(item, dict) or item.get("module") not in media_counts:
                        continue
                    module_name = item["module"]
                    media_counts[module_name] += 1
                    if set(item) == {"module", "duration", "enabled"}:
                        item.update({
                            "id": f"{module_name}_{media_counts[module_name]}",
                            "source": media.get(
                                module_name, DEFAULT_CONFIG["media"][module_name]
                            ),
                            "sources": [media.get(
                                module_name, DEFAULT_CONFIG["media"][module_name]
                            )],
                            "layout": copy.deepcopy(media_layout.get(
                                module_name, DEFAULT_CONFIG["media_layout"][module_name]
                            )),
                        })
                for module_name, count in media_counts.items():
                    if count == 0:
                        playlist.append(copy.deepcopy(next(
                            item for item in DEFAULT_CONFIG["playlist"]
                            if item["module"] == module_name
                        )))
            _replace_canvas_media_references(
                current.get("custom_modules", []), available_migrations
            )
            migrated = _merge(DEFAULT_CONFIG, current)
            migrated["schema_version"] = CURRENT_CONFIG_SCHEMA_VERSION
            migrated["revision"] = current["revision"] + 1
            validated = validate_config(copy.deepcopy(migrated))
            self._write(migrated if schema_version == 2 else validated)

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

    @staticmethod
    def _stat_identity(value):
        stat_result = os.fstat(value) if isinstance(value, int) else os.stat(value)
        return (
            stat_result.st_dev, stat_result.st_ino, stat_result.st_size,
            stat_result.st_mtime_ns, stat_result.st_ctime_ns,
        )

    @staticmethod
    def _annotate_cleanup_error(primary_error, cleanup_error):
        try:
            try:
                detail = str(cleanup_error)
            except BaseException:
                detail = f"<{type(cleanup_error).__name__}>"
            primary_error.add_note(f"additional descriptor cleanup failure: {detail}")
        except BaseException:
            pass

    @staticmethod
    def _attempt_descriptor_closes(descriptors):
        errors = []
        for descriptor in descriptors:
            if descriptor is None:
                continue
            try:
                os.close(descriptor)
            except BaseException as error:
                errors.append(error)
        return errors

    @classmethod
    def _resolve_descriptor_cleanup(cls, errors, primary_error=None):
        if primary_error is not None:
            for error in errors:
                cls._annotate_cleanup_error(primary_error, error)
            return
        if errors:
            primary_error, *secondary_errors = errors
            for error in secondary_errors:
                cls._annotate_cleanup_error(primary_error, error)
            raise primary_error

    def _open_media_source(self, reference, deadline):
        self._remaining(deadline)
        flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
        root_fd = os.open(self.media_root, flags)
        directory_fd = root_fd
        child_fd = None
        descriptor = None
        identity = None
        primary_error = None
        try:
            parts = Path(reference).parts
            if len(parts) == 2:
                child_fd = os.open(parts[0], flags, dir_fd=root_fd)
                directory_fd = child_fd
            descriptor = os.open(
                parts[-1], os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=directory_fd,
            )
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise ValueError("media source is not a regular file")
            identity = self._stat_identity(descriptor)
            self._remaining(deadline)
        except BaseException as error:
            primary_error = error

        cleanup_errors = self._attempt_descriptor_closes((child_fd, root_fd))

        if primary_error is not None or cleanup_errors:
            cleanup_errors.extend(self._attempt_descriptor_closes((descriptor,)))
            self._resolve_descriptor_cleanup(cleanup_errors, primary_error)
            if primary_error is not None:
                raise primary_error
        if descriptor is None or identity is None:
            raise RuntimeError("media descriptor ownership was not established")
        return descriptor, identity

    def _media_sources_unchanged(self, identities, deadline=None):
        deadline = deadline if deadline is not None else monotonic() + self.METADATA_PROBE_BUDGET_SECONDS
        try:
            for reference, descriptor, identity in identities:
                if self._stat_identity(descriptor) != identity:
                    return False
                current_fd, current_identity = self._open_media_source(reference, deadline)
                try:
                    if current_identity != identity:
                        return False
                finally:
                    os.close(current_fd)
            return True
        except (OSError, ValueError):
            return False

    @staticmethod
    def _remaining(deadline):
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise TimeoutError("media metadata probe exceeded aggregate budget")
        return remaining

    @classmethod
    def _release_metadata_worker(cls, process, workers, primary_error=None):
        errors = []
        try:
            for stream in (process.stdout, process.stderr):
                if stream is not None:
                    try:
                        stream.close()
                    except BaseException as error:
                        errors.append(error)
        finally:
            workers.discard(process)
        cls._resolve_descriptor_cleanup(errors, primary_error)

    @classmethod
    def _finish_metadata_worker(cls, process, workers):
        try:
            while True:
                try:
                    process.wait()
                    break
                except InterruptedError:
                    continue
                except ChildProcessError:
                    break
                except OSError:
                    sleep(0.01)
                    continue
        finally:
            cls._release_metadata_worker(process, workers)

    @classmethod
    def _metadata_reaper_main(cls, work_queue):
        while True:
            try:
                process, workers = work_queue.get(timeout=0.05)
            except queue.Empty:
                with cls._metadata_reaper_lock:
                    if not cls._metadata_reaper_emergency:
                        continue
                    _process, job = cls._metadata_reaper_emergency.popitem()
                    process, workers = job
            try:
                cls._finish_metadata_worker(process, workers)
            except BaseException:
                continue

    @classmethod
    def _ensure_metadata_reaper(cls):
        with cls._metadata_reaper_lock:
            if len(cls._metadata_reaper_threads) == 3 and all(
                thread.is_alive() for thread in cls._metadata_reaper_threads
            ):
                return
            queues = tuple(queue.Queue() for _index in range(3))
            threads = []
            for index, work_queue in enumerate(queues):
                reaper = threading.Thread(
                    target=cls._metadata_reaper_main,
                    args=(work_queue,),
                    name=f"metadata-reaper-{index}",
                    daemon=True,
                )
                reaper.start()
                threads.append(reaper)
            cls._metadata_reaper_queues = queues
            cls._metadata_reaper_threads = tuple(threads)
            cls._metadata_reaper_index = 0

    @classmethod
    def _handoff_metadata_worker(cls, process, workers):
        with cls._metadata_reaper_lock:
            queues = cls._metadata_reaper_queues
            start = cls._metadata_reaper_index
            if queues:
                cls._metadata_reaper_index = (start + 1) % len(queues)
        for offset in range(len(queues)):
            work_queue = queues[(start + offset) % len(queues)]
            try:
                work_queue.put_nowait((process, workers))
                return
            except BaseException:
                continue
        with cls._metadata_reaper_lock:
            cls._metadata_reaper_emergency[process] = (process, workers)

    def _probe_raw_metadata(self, descriptor, suffix, deadline):
        self._ensure_metadata_reaper()
        descriptor_path = f"/proc/self/fd/{descriptor}"
        process = subprocess.Popen(
            [sys.executable, "-c", self._METADATA_WORKER_SCRIPT, descriptor_path, suffix],
            pass_fds=(descriptor,), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, start_new_session=True, close_fds=True,
            env={"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"},
        )
        self._metadata_workers.add(process)
        output = error = ""
        primary_error = None
        try:
            if self._test_worker_started is not None:
                self._test_worker_started(process.pid)
            output, error = process.communicate(timeout=self._remaining(deadline))
        except subprocess.TimeoutExpired as timeout_error:
            primary_error = TimeoutError("media metadata probe exceeded aggregate budget")
            primary_error.__cause__ = timeout_error
        except BaseException as probe_error:
            primary_error = probe_error

        cleanup_errors = []
        cleanup_transferred = False
        running = True
        try:
            running = process.poll() is None
        except BaseException as poll_error:
            cleanup_errors.append(poll_error)
        if running:
            group_signalled = False
            try:
                os.killpg(process.pid, signal.SIGKILL)
                group_signalled = True
            except ProcessLookupError:
                group_signalled = True
            except BaseException as signal_error:
                cleanup_errors.append(signal_error)
            if not group_signalled:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
                except BaseException as kill_error:
                    cleanup_errors.append(kill_error)
            try:
                process.wait(timeout=self._remaining(deadline))
            except BaseException as wait_error:
                if not isinstance(wait_error, (subprocess.TimeoutExpired, TimeoutError)):
                    cleanup_errors.append(wait_error)
                self._handoff_metadata_worker(process, self._metadata_workers)
                cleanup_transferred = True

        active_error = primary_error
        if active_error is None and cleanup_errors:
            active_error, *cleanup_errors = cleanup_errors
        if active_error is not None:
            for cleanup_error in cleanup_errors:
                self._annotate_cleanup_error(active_error, cleanup_error)
        if not cleanup_transferred:
            self._release_metadata_worker(
                process, self._metadata_workers, primary_error=active_error,
            )
        if active_error is not None:
            raise active_error

        self._remaining(deadline)
        if process.returncode:
            raise ValueError(f"media metadata probe failed: {error[:200]}")
        if len(output) > 64 * 1024:
            raise ValueError("media metadata is too large")
        raw = json.loads(output)
        if not isinstance(raw, list) or len(raw) != 2:
            raise ValueError("media metadata is invalid")
        kind, value = raw
        if kind == "gif" and isinstance(value, list):
            return kind, tuple(float(delay) for delay in value)
        if kind == "mp4" and isinstance(value, int) and value > 0:
            return kind, value
        raise ValueError("media metadata is invalid")

    @staticmethod
    def _duration_from_raw_metadata(raw, loops, playback_rate):
        kind, value = raw
        if kind == "gif":
            return sum(max(0.01, delay / playback_rate) for delay in value) * loops
        if kind == "mp4":
            return value / 20.0 / playback_rate * loops
        raise ValueError("unsupported metadata result")

    def _canonicalize_basic_media_durations(
        self, config, *, return_identities=False, deadline=None,
    ):
        modules = {
            module["id"]: module
            for module in config.get("custom_modules", [])
            if isinstance(module, dict) and module.get("type") == "media"
        }
        deadline = deadline if deadline is not None else monotonic() + self.METADATA_PROBE_BUDGET_SECONDS
        metadata = {}
        opened = {}
        records = []
        failure = object()
        transfer_records = False
        try:
            for item in config.get("playlist", []):
                module = modules.get(item.get("module")) if isinstance(item, dict) else None
                if module is None:
                    continue
                reference = module["source"]
                opened_source = opened.get(reference)
                if opened_source is failure:
                    continue
                if opened_source is None:
                    try:
                        descriptor, source_identity = self._open_media_source(reference, deadline)
                    except TimeoutError:
                        raise
                    except (OSError, ValueError):
                        opened[reference] = failure
                        continue
                    opened_source = descriptor, source_identity
                    opened[reference] = opened_source
                    records.append((reference, descriptor, source_identity))
                descriptor, source_identity = opened_source
                suffix = Path(reference).suffix.lower()
                metadata_key = source_identity, suffix
                raw = metadata.get(metadata_key)
                if raw is failure:
                    continue
                if raw is None:
                    try:
                        raw = self._probe_raw_metadata(descriptor, suffix, deadline)
                        expected_kind = suffix.removeprefix(".")
                        if raw[0] != expected_kind:
                            raise ValueError("media metadata decoder kind is invalid")
                    except TimeoutError:
                        raise
                    except (
                        OSError, SyntaxError, ValueError, subprocess.SubprocessError,
                        json.JSONDecodeError,
                    ):
                        metadata[metadata_key] = failure
                        continue
                    metadata[metadata_key] = raw
                duration = self._duration_from_raw_metadata(
                    raw,
                    module["loops"],
                    module["playback_speed_percent"] / 100.0,
                )
                if math.isfinite(duration) and duration > 0:
                    if self._stat_identity(descriptor) != source_identity:
                        raise ValueError("media source changed during duration validation")
                    item["duration"] = duration
            self._remaining(deadline)
            if return_identities:
                transfer_records = True
                return config, records
            return config
        finally:
            if not transfer_records:
                self._close_media_records(records, primary_error=sys.exception())

    @classmethod
    def _close_media_records(cls, records, primary_error=None):
        errors = cls._attempt_descriptor_closes(
            descriptor for _reference, descriptor, _identity in records
        )
        cls._resolve_descriptor_cleanup(errors, primary_error)

    def validate_candidate(
        self, changes, *, canonicalize_media=True, client_contract=None,
    ):
        """Validate and canonicalize an unsaved draft without persisting it."""
        if not isinstance(changes, dict):
            raise ValueError("configuration candidate must be a JSON object")
        if "revision" in changes or "schema_version" in changes:
            raise ValueError("revision and schema_version are server-owned")
        _validate_client_contract(client_contract)
        _validate_update_keys(changes, DEFAULT_CONFIG)
        with self._lock:
            current = self.get()
            candidate = _merge(current, changes)
            candidate = _synchronize_media_playlist_changes(
                candidate, changes, current, client_contract=client_contract,
            )
            candidate["revision"] = current["revision"]
            candidate = validate_config(
                candidate,
                allow_legacy_announcements=False,
                enforce_save_bounds=True,
            )
        if not canonicalize_media:
            return copy.deepcopy(candidate)
        deadline = monotonic() + self.METADATA_PROBE_BUDGET_SECONDS
        identities = []
        try:
            candidate, identities = self._canonicalize_basic_media_durations(
                candidate, return_identities=True, deadline=deadline,
            )
            self._remaining(deadline)
            candidate = validate_config(
                candidate,
                allow_legacy_announcements=False,
                allow_missing_custom_loops=False,
                enforce_save_bounds=True,
            )
            self._remaining(deadline)
            if not self._media_sources_unchanged(identities, deadline):
                raise ValueError("media source changed during duration validation")
            self._remaining(deadline)
            return copy.deepcopy(candidate)
        finally:
            self._close_media_records(identities, primary_error=sys.exception())

    def _bind_admitted_media(self, candidate, admitted_media, records, deadline):
        references = candidate_media_references(candidate)
        if set(admitted_media) != set(references):
            raise ValueError("admitted media capabilities do not match candidate")
        commit_identities = {
            reference: identity for reference, _descriptor, identity in records
        }
        for reference in references:
            self._remaining(deadline)
            admitted_handle = admitted_media.get(reference)
            if admitted_handle is None:
                raise ValueError("admitted media capability is missing")
            try:
                admitted_identity = self._stat_identity(admitted_handle.fileno())
            except (AttributeError, OSError, ValueError):
                raise ValueError("admitted media capability is invalid") from None
            commit_identity = commit_identities.get(reference)
            if commit_identity is None:
                descriptor, commit_identity = self._open_media_source(reference, deadline)
                records.append((reference, descriptor, commit_identity))
                commit_identities[reference] = commit_identity
            if commit_identity != admitted_identity:
                raise ValueError("media source changed after admission")

    def update(
        self, changes, *, base_revision=None, client_contract=None,
        admitted_media=None,
    ):
        if not isinstance(changes, dict):
            raise ValueError("configuration update must be a JSON object")

        if "revision" in changes or "schema_version" in changes:
            raise ValueError("revision and schema_version are server-owned")
        _validate_client_contract(client_contract)
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
            updated = _synchronize_media_playlist_changes(
                updated, changes, current, client_contract=client_contract,
            )
            updated["revision"] = current["revision"] + 1
            updated = validate_config(
                updated,
                allow_legacy_announcements=False,
                enforce_save_bounds=True,
            )
        deadline = monotonic() + self.METADATA_PROBE_BUDGET_SECONDS
        identities = []
        acquired = False
        try:
            updated, identities = self._canonicalize_basic_media_durations(
                updated, return_identities=True, deadline=deadline,
            )
            self._remaining(deadline)
            updated = validate_config(
                updated,
                allow_legacy_announcements=False,
                allow_missing_custom_loops=False,
                enforce_save_bounds=True,
            )
            self._remaining(deadline)
            acquired = self._lock.acquire(timeout=self._remaining(deadline))
            if not acquired:
                raise TimeoutError("media metadata probe exceeded aggregate budget")
            latest = self.get()
            if latest["revision"] != current["revision"]:
                raise ConfigConflictError(latest["revision"])
            if self._test_before_media_commit_check is not None:
                self._test_before_media_commit_check()
            if admitted_media is not None:
                self._bind_admitted_media(updated, admitted_media, identities, deadline)
            if not self._media_sources_unchanged(identities, deadline):
                raise ValueError("media source changed during duration validation")
            # Exact admission boundary: no hook, await, or unchecked work follows this
            # final deadline/descriptor/directory-entry check before atomic persistence.
            self._remaining(deadline)
            self._write(updated)
            return copy.deepcopy(updated)
        finally:
            if acquired:
                self._lock.release()
            self._close_media_records(identities, primary_error=sys.exception())
