"""Calendar progress-bar animations (owner 2026-10-14: "add a variety of
animations for the bar that the user can choose from. ensure solid is one
option").

Every style paints only inside the fill rectangle, so the bar length always
tells the time; 'solid' is the original flat accent bar. Animated styles are
deterministic in elapsed seconds so preview and panel agree frame-for-frame.
"""
import colorsys
import math

STYLES = ('solid', 'gradient', 'shimmer', 'pulse', 'stripes', 'segments', 'dots', 'rainbow')
LABELS = (('solid', 'Solid'), ('gradient', 'Gradient'), ('shimmer', 'Shimmer (light sweeps along)'),
          ('pulse', 'Pulse (gently breathes)'), ('stripes', 'Moving stripes'), ('segments', 'Segments'),
          ('dots', 'Dots'), ('rainbow', 'Rainbow (colours flow)'))
SPEED = 20  # pixels per second for moving patterns


def _scale(colour, amount):
    return tuple(max(0, min(255, round(channel * amount))) for channel in colour)


def _mix(a, b, amount):
    return tuple(round(x + (y - x) * amount) for x, y in zip(a, b))


def draw(draw_context, style, left, top, fill, height, accent, background, elapsed):
    """Paint a progress fill of ``fill`` columns x ``height`` rows at (left, top)."""
    if fill <= 0:
        return
    accent = tuple(bytes.fromhex(accent[1:])) if isinstance(accent, str) else tuple(accent)
    background = tuple(bytes.fromhex(background[1:])) if isinstance(background, str) else tuple(background)
    right, bottom = left + fill - 1, top + height - 1
    if style == 'solid' or style not in STYLES:
        draw_context.rectangle((left, top, right, bottom), fill=accent)
        return
    shift = int(elapsed * SPEED)
    for column in range(fill):
        x = left + column
        if style == 'gradient':
            colour = _scale(accent, .3 + .7 * (column + 1) / fill)
        elif style == 'shimmer':
            # A bright 5px highlight sweeps from the start to the fill end, then repeats.
            span = fill + 10
            centre = shift % span - 5
            glow = max(0.0, 1 - abs(column - centre) / 3)
            colour = _mix(_scale(accent, .75), (255, 255, 255), .65 * glow)
        elif style == 'pulse':
            colour = _scale(accent, .45 + .55 * (.5 + .5 * math.sin(elapsed * 2 * math.pi / 1.6)))
        elif style == 'stripes':
            # Diagonal barber-pole stripes moving towards the fill end.
            for row in range(height):
                lit = ((column - shift + row) // 3) % 2 == 0
                draw_context.point((x, top + row), fill=accent if lit else _scale(accent, .4))
            continue
        elif style == 'segments':
            if column % 4 == 3 and column != fill - 1:
                continue  # 3px blocks with 1px gaps; the last column always shows the fill end
            colour = accent
        elif style == 'dots':
            if column % 2 and column != fill - 1:
                continue
            colour = accent
        else:  # rainbow
            hue = ((column * 4 + shift * 3) % 360) / 360
            colour = tuple(round(channel * 255) for channel in colorsys.hsv_to_rgb(hue, .85, 1))
        if colour == background:
            colour = _mix(colour, (255, 255, 255), .5)
        draw_context.line((x, top, x, bottom), fill=colour)
