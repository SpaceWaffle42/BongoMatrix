"""Safe data-driven custom text and layered canvas animation modules."""

from __future__ import annotations

import base64
import copy
import math
import time

from PIL import Image

from modules.frame_appearance import draw_frame_border

CANVAS_MEDIA_PREPARE_TIMEOUT = 5
CANVAS_MEDIA_CACHE_BYTES = 32 * 1024 * 1024


def _wait(stop_event, seconds):
    if stop_event is not None:
        return stop_event.wait(seconds)
    time.sleep(seconds)
    return False


def _fill_background(matrix, canvas, colour):
    matrix.fill(colour.red, colour.green, colour.blue, canvas=canvas)


def _time_text(time_format):
    patterns = {
        "24h": "%H:%M",
        "24h_seconds": "%H:%M:%S",
        "12h": "%I:%M %p",
        "12h_seconds": "%I:%M:%S %p",
    }
    text = time.strftime(patterns[time_format])
    return text.lstrip("0") if time_format.startswith("12h") else text


def frame_sequence(frame_count, playback):
    """Return one bounded playback cycle for a validated canvas module."""
    forward = list(range(frame_count))
    if playback == "ping_pong" and frame_count > 1:
        return forward + list(range(frame_count - 2, 0, -1))
    return forward


def _ease(amount, easing):
    amount = max(0.0, min(1.0, amount))
    if easing == "step":
        return 0.0
    if easing == "ease_in":
        return amount * amount
    if easing == "ease_out":
        return 1 - (1 - amount) * (1 - amount)
    if easing == "ease_in_out":
        return 2 * amount * amount if amount < 0.5 else 1 - ((-2 * amount + 2) ** 2) / 2
    return amount


def _interpolate(start, end, amount):
    if isinstance(start, str) and isinstance(end, str) and start.startswith("#") and end.startswith("#"):
        channels = []
        for offset in (1, 3, 5):
            value = round(int(start[offset:offset + 2], 16) + (int(end[offset:offset + 2], 16) - int(start[offset:offset + 2], 16)) * amount)
            channels.append(max(0, min(255, value)))
        return "#" + "".join(f"{value:02x}" for value in channels)
    if isinstance(start, (int, float)) and not isinstance(start, bool) and isinstance(end, (int, float)) and not isinstance(end, bool):
        return start + (end - start) * amount
    if isinstance(start, list) and isinstance(end, list) and len(start) == len(end):
        return [_interpolate(left, right, amount) for left, right in zip(start, end)]
    return copy.deepcopy(start if amount < 1 else end)


def resolve_layer_properties(layer, frame, overrides):
    """Resolve base values, keyframe interpolation, then a discrete frame override."""
    resolved = copy.deepcopy(layer["properties"])
    keyframes = sorted(layer.get("keyframes", []), key=lambda item: item["frame"])
    property_names = {key for keyframe in keyframes for key in keyframe["properties"]}
    for name in property_names:
        values = [keyframe for keyframe in keyframes if name in keyframe["properties"]]
        previous = next((item for item in reversed(values) if item["frame"] <= frame), None)
        following = next((item for item in values if item["frame"] >= frame), None)
        if previous is None and following is not None:
            continue
        elif following is None and previous is not None:
            resolved[name] = copy.deepcopy(previous["properties"][name])
        elif previous is not None and following is not None:
            if previous["frame"] == following["frame"]:
                resolved[name] = copy.deepcopy(previous["properties"][name])
            elif name in {"source", "z_index"}:
                resolved[name] = copy.deepcopy(previous["properties"][name])
            else:
                amount = (frame - previous["frame"]) / (following["frame"] - previous["frame"])
                resolved[name] = _interpolate(
                    previous["properties"][name], following["properties"][name],
                    _ease(amount, following["easing"]),
                )
    for override in overrides:
        if override["frame"] == frame and override["layer_id"] == layer["id"]:
            resolved.update(copy.deepcopy(override["properties"]))
    return resolved


def ordered_layers_for_frame(module_config, frame):
    """Resolve and sort layers back-to-front for one frame.

    Legacy layers without an explicit depth retain their saved array order.
    """
    resolved = []
    for index, layer in enumerate(module_config["layers"]):
        properties = resolve_layer_properties(layer, frame, module_config["overrides"])
        resolved.append((properties.get("z_index", index), index, layer, properties))
    resolved.sort(key=lambda item: (item[0], item[1]))
    return [(layer, properties) for _, _, layer, properties in resolved]


def _rotated(points, angle, centre=None):
    if not angle:
        return [(round(x), round(y)) for x, y in points]
    if centre is None:
        centre = (
            sum(point[0] for point in points) / len(points),
            sum(point[1] for point in points) / len(points),
        )
    radians = math.radians(angle)
    sine, cosine = math.sin(radians), math.cos(radians)
    cx, cy = centre
    return [
        (
            round(cx + (x - cx) * cosine - (y - cy) * sine),
            round(cy + (x - cx) * sine + (y - cy) * cosine),
        )
        for x, y in points
    ]


def _draw_polygon(matrix, canvas, points, colour, filled=False):
    if filled:
        minimum = math.floor(min(point[1] for point in points))
        maximum = math.ceil(max(point[1] for point in points))
        for y in range(minimum, maximum + 1):
            intersections = []
            for index, first in enumerate(points):
                second = points[(index + 1) % len(points)]
                if first[1] == second[1] or not min(first[1], second[1]) <= y < max(first[1], second[1]):
                    continue
                intersections.append(first[0] + (y - first[1]) * (second[0] - first[0]) / (second[1] - first[1]))
            intersections.sort()
            for index in range(0, len(intersections) - 1, 2):
                matrix.draw_line(math.ceil(intersections[index]), y, math.floor(intersections[index + 1]), y, colour, canvas=canvas)
        return
    for index, first in enumerate(points):
        second = points[(index + 1) % len(points)]
        matrix.draw_line(first[0], first[1], second[0], second[1], colour, canvas=canvas)


def _ellipse_points(properties):
    cx, cy = properties["x"], properties["y"]
    points = [
        (
            cx + properties["radius_x"] * math.cos(2 * math.pi * index / 36),
            cy + properties["radius_y"] * math.sin(2 * math.pi * index / 36),
        )
        for index in range(36)
    ]
    return _rotated(points, properties["rotation"], (cx, cy))


def media_frame_index(layer, frame):
    """Return a media frame relative to when this layer first becomes present."""
    start_frame = 0
    if layer.get("properties", {}).get("present", True) is False:
        appearances = [
            keyframe["frame"] for keyframe in layer.get("keyframes", [])
            if keyframe.get("properties", {}).get("present") is True
        ]
        if appearances:
            start_frame = min(appearances)
    return max(0, frame - start_frame)


def _draw_layer(matrix, canvas, layer, properties, *, media_cache=None, frame=0):
    layer_type = layer["type"]
    if layer_type == "media":
        cache_key = (
            properties["source"],
            properties.get("playback_speed_percent", 100.0) / 100.0,
        )
        images = (media_cache or {}).get(cache_key, [])
        if images:
            matrix.draw_image(
                images[media_frame_index(layer, frame) % len(images)], round(properties["x"]), round(properties["y"]),
                width=round(properties["width"]), height=round(properties["height"]),
                keep_aspect=properties["keep_aspect"], rotation=properties["rotation"],
                canvas=canvas,
            )
        return
    if layer_type == "pixels" and "bitmap_rgba" in properties:
        bitmap = base64.b64decode(properties["bitmap_rgba"], validate=True)
        points = []
        colours = []
        offset_x = properties.get("offset_x", 0)
        offset_y = properties.get("offset_y", 0)
        for index in range(64 * 32):
            red, green, blue, alpha = bitmap[index * 4:index * 4 + 4]
            if alpha:
                points.append((index % 64 + offset_x, index // 64 + offset_y))
                colours.append((red, green, blue))
        centre = None
        if points:
            centre = (
                (min(x for x, _ in points) + max(x for x, _ in points)) / 2,
                (min(y for _, y in points) + max(y for _, y in points)) / 2,
            )
        rotated = _rotated(points, properties.get("rotation", 0), centre) if points else []
        for (x, y), (red, green, blue) in zip(rotated, colours):
            if 0 <= x < 64 and 0 <= y < 32:
                matrix.pixel(x, y, red, green, blue, canvas=canvas)
        return
    colour = matrix.colour_rgb(properties["colour"])
    if layer_type == "text":
        text_options = {"canvas": canvas}
        if properties.get("rotation"):
            text_options["rotation"] = properties["rotation"]
        matrix.draw_text(
            matrix.font(properties["font"]), round(properties["x"]), round(properties["y"]),
            colour, properties["text"], **text_options,
        )
    elif layer_type == "time":
        text_options = {"canvas": canvas}
        if properties.get("rotation"):
            text_options["rotation"] = properties["rotation"]
        matrix.draw_text(
            matrix.font(properties["font"]), round(properties["x"]), round(properties["y"]),
            colour, _time_text(properties["format"]), **text_options,
        )
    elif layer_type == "pixels":
        for x, y in _rotated(properties["points"], properties.get("rotation", 0)):
            matrix.pixel(x, y, colour.red, colour.green, colour.blue, canvas=canvas)
    elif layer_type == "line":
        points = _rotated(
            [(properties["x1"], properties["y1"]), (properties["x2"], properties["y2"])],
            properties.get("rotation", 0),
        )
        matrix.draw_line(
            points[0][0], points[0][1], points[1][0], points[1][1], colour,
            width=round(properties["width"]), canvas=canvas,
        )
    elif layer_type == "rectangle":
        x, y = properties["x"], properties["y"]
        width, height = properties["width"], properties["height"]
        points = _rotated([(x, y), (x + width - 1, y), (x + width - 1, y + height - 1), (x, y + height - 1)], properties["rotation"])
        if properties["filled"] and width == 1 and height == 1:
            matrix.pixel(points[0][0], points[0][1], colour.red, colour.green, colour.blue, canvas=canvas)
        else:
            _draw_polygon(matrix, canvas, points, colour, properties["filled"])
    elif layer_type == "polygon":
        _draw_polygon(matrix, canvas, _rotated(properties["points"], properties["rotation"]), colour, properties["filled"])
    elif layer_type == "circle":
        matrix.draw_circle(round(properties["x"]), round(properties["y"]), round(properties["radius"]), colour, filled=properties["filled"], canvas=canvas)
    elif layer_type == "ellipse":
        _draw_polygon(matrix, canvas, _ellipse_points(properties), colour, properties["filled"])


def _canvas_media_cache(matrix, stop_event, module_config, pinned_handles=None):
    media_identities = set()
    for layer in module_config["layers"]:
        if layer["type"] != "media":
            continue
        for frame in range(module_config["frame_count"]):
            properties = resolve_layer_properties(
                layer, frame, module_config["overrides"],
            )
            if "source" in properties:
                media_identities.add((
                    properties["source"],
                    properties.get("playback_speed_percent", 100.0) / 100.0,
                ))

    media_cache = {}
    media_cache_bytes = 0
    prepare_deadline = (
        time.monotonic() + CANVAS_MEDIA_PREPARE_TIMEOUT if media_identities else None
    )
    sorted_media_identities = sorted(media_identities)
    for index, (source, playback_rate) in enumerate(sorted_media_identities):
        if prepare_deadline is None:
            raise RuntimeError("canvas media preparation deadline is unavailable")
        if stop_event is not None and stop_event.is_set() is True:
            return None
        remaining = prepare_deadline - time.monotonic()
        if remaining <= 0:
            media_cache[(source, playback_rate)] = []
            continue
        remaining_source_count = len(sorted_media_identities) - index
        pinned_handle = (pinned_handles or {}).get(source)
        if pinned_handle is None:
            raise ValueError("canvas media capability is missing")
        try:
            media_options = {
                "stop_event": stop_event,
                "preserve_size": True,
                "decode_timeout": min(
                    CANVAS_MEDIA_PREPARE_TIMEOUT,
                    remaining / remaining_source_count,
                ),
                "playback_rate": playback_rate,
            }
            media_options["pinned_handle"] = pinned_handle
            frames = matrix.media_frames(
                source, module_config["frame_count"], module_config["fps"],
                **media_options,
            )
        except (OSError, ValueError):
            frames = []
        if stop_event is not None and stop_event.is_set() is True:
            return None
        media_cache_bytes += sum(
            image.width * image.height * 3
            for image in {id(frame): frame for frame in frames}.values()
            if isinstance(image, Image.Image)
        )
        if media_cache_bytes > CANVAS_MEDIA_CACHE_BYTES:
            raise ValueError("canvas media cache exceeds its aggregate memory budget")
        media_cache[(source, playback_rate)] = frames
    return media_cache


def canvas_timeline_frames(
    matrix, duration, stop_event, module_config, *, monotonic=None,
    pinned_handles=None,
):
    """Yield rendered Advanced Canvas frames and deadline-clipped delays."""
    duration = max(0.0, float(duration))
    if duration == 0 or (stop_event is not None and stop_event.is_set() is True):
        return
    media_cache = _canvas_media_cache(
        matrix, stop_event, module_config, pinned_handles=pinned_handles,
    )
    if media_cache is None or (stop_event is not None and stop_event.is_set() is True):
        return

    background = matrix.colour_rgb(module_config["background"])
    sequence = frame_sequence(module_config["frame_count"], module_config["playback"])
    normal_delay = 1 / module_config["fps"]
    cycle_index = 0
    elapsed = 0.0
    started = monotonic() if monotonic is not None else None
    while True:
        clock_elapsed = 0.0 if monotonic is None else monotonic() - started
        remaining = duration - max(elapsed, clock_elapsed)
        if remaining <= 0 or (stop_event is not None and stop_event.is_set() is True):
            return
        frame = sequence[min(cycle_index, len(sequence) - 1)]
        canvas = matrix.matrix.CreateFrameCanvas()
        _fill_background(matrix, canvas, background)
        for layer, properties in ordered_layers_for_frame(module_config, frame):
            if layer["visible"] and properties.get("present", True):
                _draw_layer(
                    matrix, canvas, layer, properties,
                    media_cache=media_cache, frame=frame,
                )
        if stop_event is not None and stop_event.is_set() is True:
            return
        delay = min(normal_delay, remaining)
        yield canvas, delay
        elapsed += delay
        if module_config["playback"] == "hold":
            cycle_index = min(cycle_index + 1, len(sequence) - 1)
        else:
            cycle_index = (cycle_index + 1) % len(sequence)


def _run_canvas(matrix, duration, stop_event, module_config, pinned_handles=None):
    rendered = False
    for canvas, delay in canvas_timeline_frames(
        matrix, duration, stop_event, module_config, monotonic=time.monotonic,
        pinned_handles=pinned_handles,
    ):
        if stop_event is not None and stop_event.is_set() is True:
            break
        matrix.swap(canvas)
        rendered = True
        if _wait(stop_event, delay):
            break
    return rendered


def advance_left_scroll(position, text_width, canvas_width, completed_loops, loop_limit):
    """Advance one frame while preserving the physical ticker reset boundary."""
    position -= 1
    if position + text_width < 0:
        completed_loops += 1
        if completed_loops < loop_limit:
            position = canvas_width
    return position, completed_loops


def left_scroll_positions(canvas_width, text_width, loops):
    """Yield every physical left-ticker position through natural loop exhaustion."""
    yield from scroll_positions(canvas_width, text_width, loops, direction="left")


def scroll_positions(canvas_width, text_width, loops, direction="left"):
    """Yield every physical ticker position through natural loop exhaustion."""
    if direction == "right":
        for _ in range(loops):
            yield from range(-text_width, canvas_width + 1)
        return

    position = canvas_width
    completed_loops = 0
    while completed_loops < loops:
        yield position
        position, completed_loops = advance_left_scroll(
            position, text_width, canvas_width, completed_loops, loops,
        )


def basic_text_frames(matrix, module_config, duration, stop_event=None):
    """Yield the finite physical frame sequence for a basic custom module."""
    font = matrix.font(module_config["font"])
    foreground = matrix.colour_rgb(module_config["colour"])
    background = matrix.colour_rgb(module_config["background"])
    text = module_config["text"]
    y = module_config["y"]

    def frame(position, delay):
        canvas = matrix.matrix.CreateFrameCanvas()
        _fill_background(matrix, canvas, background)
        width = matrix.draw_text(font, position, y, foreground, text, canvas=canvas)
        draw_frame_border(
            matrix, canvas, module_config.get("border", "none"),
            module_config.get("border_colour", "#ffffff"),
        )
        if stop_event is not None and stop_event.is_set() is True:
            return None
        return canvas, delay, width

    if module_config["type"] == "text":
        rendered = frame(module_config["x"], duration)
        if rendered is not None:
            yield rendered[0], rendered[1]
        return

    direction = module_config["direction"]
    loops = module_config.get("loops", 1)
    if direction == "right":
        measure_canvas = matrix.matrix.CreateFrameCanvas()
        matrix.clear(measure_canvas)
        width = matrix.draw_text(font, 0, y, foreground, text, canvas=measure_canvas)
        positions = scroll_positions(matrix.matrix.width, width, loops, direction)
    else:
        positions = None
        position = matrix.matrix.width
        completed_loops = 0
        width = None
    while direction == "right" or completed_loops < loops:
        if stop_event is not None and stop_event.is_set() is True:
            return
        if direction == "right":
            try:
                position = next(positions)
            except StopIteration:
                return
        rendered = frame(position, module_config["speed"])
        if rendered is None:
            return
        yield rendered[0], rendered[1]
        if direction == "left":
            width = width or rendered[2]
            position, completed_loops = advance_left_scroll(
                position, width, matrix.matrix.width, completed_loops, loops,
            )


def run(
    matrix, config, duration=15, stop_event=None, module_config=None,
    *, pinned_handles=None,
):
    """Render one validated custom definition using capture-aware Matrix calls."""
    if module_config is None:
        raise ValueError("custom module configuration is required")
    if stop_event is not None and stop_event.is_set() is True:
        return False
    if module_config["type"] == "canvas":
        return _run_canvas(
            matrix, duration, stop_event, module_config,
            pinned_handles=pinned_handles,
        )
    if module_config["type"] == "media":
        source = module_config["source"]
        method = matrix.show_gif if source.lower().endswith(".gif") else matrix.show_video
        pinned_handle = (pinned_handles or {}).get(source)
        if pinned_handle is None:
            raise ValueError("custom media capability is missing")
        options = {
            "x": module_config["x"], "y": module_config["y"],
            "width": module_config["width"], "height": module_config["height"],
            "fit": module_config["fit"], "loops": module_config["loops"],
            "playback_rate": module_config["playback_speed_percent"] / 100.0,
            "stop_event": stop_event,
            "pinned_handle": pinned_handle,
        }
        return method(source, **options)

    rendered = False
    for canvas, delay in basic_text_frames(matrix, module_config, duration, stop_event):
        if stop_event is not None and stop_event.is_set() is True:
            break
        matrix.swap(canvas)
        rendered = True
        if _wait(stop_event, delay):
            break
    return rendered
