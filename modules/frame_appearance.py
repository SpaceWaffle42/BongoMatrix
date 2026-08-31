"""Shared frame appearance rendering for 64x32 Matrix canvases."""


def _rectangle(matrix, canvas, colour, offset, x_offset):
    left = offset + x_offset
    right = matrix.matrix.width - 1 - offset + x_offset
    bottom = matrix.matrix.height - 1 - offset
    for x1, y1, x2, y2 in (
        (left, offset, right, offset),
        (right, offset, right, bottom),
        (right, bottom, left, bottom),
        (left, bottom, left, offset),
    ):
        if x1 == x2:
            if not 0 <= x1 < matrix.matrix.width:
                continue
        else:
            if max(x1, x2) < 0 or min(x1, x2) >= matrix.matrix.width:
                continue
            x1 = min(matrix.matrix.width - 1, max(0, x1))
            x2 = min(matrix.matrix.width - 1, max(0, x2))
        matrix.draw_line(x1, y1, x2, y2, colour, canvas=canvas)


def _perimeter(width, height, offset):
    left, right = offset, width - 1 - offset
    top, bottom = offset, height - 1 - offset
    return (
        [(x, top) for x in range(left, right + 1)]
        + [(right, y) for y in range(top + 1, bottom + 1)]
        + [(x, bottom) for x in range(right - 1, left - 1, -1)]
        + [(left, y) for y in range(bottom - 1, top, -1)]
    )


def _pattern(matrix, canvas, colour, on_pixels, period, x_offset):
    for index, (x, y) in enumerate(_perimeter(matrix.matrix.width, matrix.matrix.height, 0)):
        translated = x + x_offset
        if index % period < on_pixels and 0 <= translated < matrix.matrix.width:
            matrix.draw_line(translated, y, translated, y, colour, canvas=canvas)


def draw_frame_border(matrix, canvas, border, border_colour, x_offset=0):
    """Draw a clipped 64x32-safe border using Matrix coordinates."""
    if border == "none":
        return
    if border not in {"solid", "inset", "dotted", "dashed", "double"}:
        raise NotImplementedError("frame border is not implemented")
    colour = matrix.colour_rgb(border_colour)
    if border == "dotted":
        return _pattern(matrix, canvas, colour, 1, 2, x_offset)
    if border == "dashed":
        return _pattern(matrix, canvas, colour, 3, 5, x_offset)
    if border == "double":
        _rectangle(matrix, canvas, colour, 0, x_offset)
        return _rectangle(matrix, canvas, colour, 2, x_offset)
    return _rectangle(matrix, canvas, colour, 1 if border == "inset" else 0, x_offset)
