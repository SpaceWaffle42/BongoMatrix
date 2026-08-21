import datetime
import random
import time
from PIL import Image, ImageDraw, ImageFilter
from module_options import LAVA_PALETTES

WIDTH = 64
HEIGHT = 32
THRESHOLD = 1
FRAME_DELAY = 0.03

COLOUR_MATCH = LAVA_PALETTES

EDGE_TABLE = {
    1: [(0, 0.5), (0.5, 0)],
    2: [(0.5, 0), (1, 0.5)],
    3: [(0, 0.5), (1, 0.5)],
    4: [(1, 0.5), (0.5, 1)],
    5: [(0, 0.5), (0.5, 0), (1, 0.5), (0.5, 1)],
    6: [(0.5, 0), (0.5, 1)],
    7: [(0, 0.5), (0.5, 1)],
    8: [(0.5, 1), (0, 0.5)],
    9: [(0.5, 0), (0.5, 1)],
    10: [(0.5, 0), (1, 0.5), (0.5, 1), (0, 0.5)],
    11: [(1, 0.5), (0.5, 1)],
    12: [(1, 0.5), (0.5, 0)],
    13: [(0.5, 0), (0, 0.5)],
    14: [(0, 0.5), (0.5, 0)],
}

NUM_BLOBS = random.randint(3, 4)
blobs = [
    {
        "x": random.uniform(0, WIDTH),
        "y": random.uniform(0, HEIGHT),
        "vx": random.uniform(-0.25, 0.25),
        "vy": random.uniform(-0.25, 0.25),
        "r": random.uniform(5, 10),
    }
    for _ in range(NUM_BLOBS)
]


def make_blobs(settings=None):
    settings = settings or {}
    count = settings.get("blob_count", 4)
    minimum_radius = settings.get("blob_min_radius", 5)
    maximum_radius = settings.get("blob_max_radius", 10)
    speed = settings.get("blob_speed", 0.25)
    return [
        {
            "x": random.uniform(0, WIDTH),
            "y": random.uniform(0, HEIGHT),
            "vx": random.uniform(-speed, speed),
            "vy": random.uniform(-speed, speed),
            "r": random.uniform(minimum_radius, maximum_radius),
        }
        for _ in range(count)
    ]


def aligned_x(alignment, text_width):
    if alignment == "left":
        return 2
    if alignment == "right":
        return max(0, WIDTH - text_width - 2)
    return max(0, int((WIDTH - text_width) / 2))


def pick_palette(matrix, lava_config=None):
    """Pick from the checked presets and optional custom RGB palette."""
    lava_config = lava_config or {
        "enabled_palettes": list(COLOUR_MATCH),
        "custom_enabled": False,
        "custom_colours": [],
    }
    choices = [COLOUR_MATCH[name] for name in lava_config["enabled_palettes"]]
    if lava_config["custom_enabled"]:
        choices.append(tuple(lava_config["custom_colours"]))
    choices.extend(
        tuple(palette["colours"])
        for palette in lava_config.get("custom_palettes", [])
        if palette.get("enabled")
    )
    if not choices:
        return None
    # Non-cryptographic randomness selects visual colour variation only.
    selected = random.choice(choices)  # nosemgrep: bandit.B311
    return tuple(
        matrix.colour_rgb(value) if value.startswith("#") else matrix.colour(value)
        for value in selected
    )


def field(x, y, blob_values=None):
    """Work out how strong the lava is at one point."""
    total = 0.0
    for blob in blob_values or blobs:
        dx = x - blob["x"]
        dy = y - blob["y"]
        dist_sq = dx * dx + dy * dy + 0.01
        total += (blob["r"] ** 2) / dist_sq
    return total


def update_blobs(blob_values=None):
    """Move the blobs and bounce them off the edges."""
    for blob in blob_values or blobs:
        blob["x"] += blob["vx"]
        blob["y"] += blob["vy"]
        if blob["x"] < 0 or blob["x"] >= WIDTH:
            blob["vx"] = -blob["vx"]
            blob["x"] = max(0, min(blob["x"], WIDTH - 1))

        if blob["y"] < 0 or blob["y"] >= HEIGHT:
            blob["vy"] = -blob["vy"]
            blob["y"] = max(0, min(blob["y"], HEIGHT - 1))


def draw_lava(
    draw, c_outside, c_inside, c_line, blob_values=None,
    threshold=THRESHOLD, edge_width=4,
):
    """Draw the lava pattern into the image."""
    draw.rectangle(
        [0, 0, WIDTH, HEIGHT],
        fill=(c_outside.red, c_outside.green, c_outside.blue),
    )

    for y in range(HEIGHT - 1):
        for x in range(WIDTH - 1):
            corners = [
                field(x, y, blob_values) > threshold,
                field(x + 1, y, blob_values) > threshold,
                field(x + 1, y + 1, blob_values) > threshold,
                field(x, y + 1, blob_values) > threshold,
            ]

            square = 8 * corners[0] + 4 * corners[1] + 2 * corners[2] + 1 * corners[3]

            if square == 0:
                continue

            if square == 15:
                draw.rectangle(
                    [x, y, x + 1, y + 1],
                    fill=(c_inside.red, c_inside.green, c_inside.blue),
                )
                continue

            edges = EDGE_TABLE.get(square, [])
            for i in range(0, len(edges), 2):
                p1 = edges[i]
                p2 = edges[i + 1]
                x1, y1 = x + p1[0], y + p1[1]
                x2, y2 = x + p2[0], y + p2[1]
                draw.line(
                    (x1, y1, x2, y2),
                    fill=(c_line.red, c_line.green, c_line.blue),
                    width=edge_width,
                )


def image_to_canvas(matrix, image, canvas):
    """Copy the image onto the matrix using framework pixels."""
    pixels = image.load()
    for y in range(HEIGHT):
        for x in range(WIDTH):
            r, g, b = pixels[x, y]
            matrix.pixel(x, y, r, g, b, canvas=canvas)


def measure_text(matrix, font, y, colour, text, canvas):
    """Draw off-screen to get the text width."""
    matrix.clear(canvas)
    return matrix.draw_text(font, 0, y, colour, text, canvas=canvas)


def prepare_lava_frame_state(matrix, lava_config=None):
    """Prepare one physical Lava invocation without changing palette selection."""
    settings = lava_config or {}
    return {
        "settings": settings,
        "colours": pick_palette(matrix, lava_config),
        "blobs": make_blobs(settings),
        "font_time": matrix.font(settings.get("time_font", "9x18B")),
        "font_date": matrix.font(settings.get("date_font", "clR6x12")),
        "text_colour": (
            matrix.colour_rgb(settings["text_colour"])
            if "text_colour" in settings else matrix.colour("white")
        ),
        "border_colour": (
            matrix.colour_rgb(settings["border_colour"])
            if "border_colour" in settings else matrix.colour("black")
        ),
    }


def render_lava_frame(matrix, state, canvas, temp_canvas):
    """Render one frame shared by physical playback and finite preview parity."""
    settings = state["settings"]
    blob_values = state["blobs"]
    matrix.clear(canvas)
    if state["colours"] is not None:
        c_outside, c_inside, c_line = state["colours"]
        update_blobs(blob_values)
        image = Image.new(
            "RGB", (WIDTH, HEIGHT),
            (c_outside.red, c_outside.green, c_outside.blue),
        )
        draw = ImageDraw.Draw(image)
        draw_lava(
            draw, c_outside, c_inside, c_line, blob_values,
            threshold=settings.get("field_threshold", THRESHOLD),
            edge_width=settings.get("edge_width", 4),
        )
        image_to_canvas(
            matrix,
            image.filter(ImageFilter.BoxBlur(radius=settings.get("blur_radius", 2))),
            canvas,
        )
    now = datetime.datetime.now()
    time_text = now.strftime("%H:%M")
    date_text = now.strftime("%d %b")
    time_y = settings.get("time_y", 15)
    date_y = settings.get("date_y", 27)
    time_width = measure_text(
        matrix, state["font_time"], time_y, state["text_colour"], time_text,
        temp_canvas,
    )
    date_width = measure_text(
        matrix, state["font_date"], date_y, state["text_colour"], date_text,
        temp_canvas,
    )
    if settings.get("show_time", True):
        matrix.draw_text_with_border(
            state["font_time"], aligned_x(settings.get("time_align", "center"), time_width),
            time_y, state["text_colour"], state["border_colour"], time_text,
            canvas=canvas,
        )
    if settings.get("show_date", True):
        matrix.draw_text_with_border(
            state["font_date"], aligned_x(settings.get("date_align", "center"), date_width),
            date_y, state["text_colour"], state["border_colour"], date_text,
            canvas=canvas,
        )
    return canvas


def clock(duration=300, matrix=None, stop_event=None, lava_config=None):
    """Run the lava clock for a set duration of seconds."""
    if matrix is None:
        from matrix_framework import Matrix

        matrix = Matrix(rows=HEIGHT, cols=WIDTH)
    state = prepare_lava_frame_state(matrix, lava_config)
    canvas = matrix.matrix.CreateFrameCanvas()
    temp_canvas = matrix.matrix.CreateFrameCanvas()
    end_time = time.time() + duration

    try:
        while time.time() < end_time and not (stop_event and stop_event.is_set()):
            render_lava_frame(matrix, state, canvas, temp_canvas)
            canvas = matrix.swap(canvas)
            if stop_event:
                if stop_event.wait(FRAME_DELAY):
                    break
            else:
                time.sleep(FRAME_DELAY)

    except KeyboardInterrupt:
        matrix.clear(canvas)
        matrix.swap(canvas)


def run(matrix, config, duration=300, stop_event=None):
    return clock(
        duration=duration,
        matrix=matrix,
        stop_event=stop_event,
        lava_config=config.get("lava"),
    )


if __name__ == "__main__":
    clock()
