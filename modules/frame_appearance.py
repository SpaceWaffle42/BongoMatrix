"""Shared frame appearance rendering for 64x32 Matrix canvases."""


def draw_frame_border(matrix, canvas, border, border_colour, x_offset=0):
    """Draw a clipped None/Solid/Inset border using Matrix coordinates."""
    if border == "none":
        return
    if border not in {"solid", "inset"}:
        raise NotImplementedError("frame border is not implemented")
    colour = matrix.colour_rgb(border_colour)
    offset = 1 if border == "inset" else 0
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
