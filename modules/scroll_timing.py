"""Canonical runtime helpers for scrolling Matrix modules and browser editors."""

from __future__ import annotations

import math
from decimal import Decimal, ROUND_HALF_UP

LEGACY_FRAME_DELAYS = {
    "slow": 0.1,
    "normal": 0.05,
    "fast": 0.025,
}
MINIMUM_FRAME_DELAY = 0.01
MAXIMUM_FRAME_DELAY = 1.0
FRAME_DELAY_STEP = 0.000001
MATRIX_WIDTH = 64


def drawable_width(matrix, canvas):
    """Ticker width inside the Screen Safe Area (1.11): matrix.matrix reports the inner
    width; fall back to the canvas (full panel) for drivers/mocks without it."""
    width = getattr(getattr(matrix, "matrix", None), "width", None)
    if isinstance(width, int) and not isinstance(width, bool) and width > 0:
        return width
    return canvas.width


def frame_delay_seconds(value, default=0.05):
    """Return numeric frame-delay seconds, accepting legacy speed presets."""
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        return float(value)
    return LEGACY_FRAME_DELAYS.get(value, default)


def _positive_number(value, name):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be a finite positive number")
    return value


def _positive_integer(value, name):
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def scroll_frames(text_width, *, matrix_width=MATRIX_WIDTH, direction="left"):
    """Return inclusive frame submissions for one complete off-screen traversal."""
    if isinstance(text_width, bool) or not isinstance(text_width, int) or text_width < 0:
        raise ValueError("text_width must be a nonnegative integer")
    _positive_integer(matrix_width, "matrix_width")
    if direction not in {"left", "right"}:
        raise ValueError("direction must be left or right")
    return matrix_width + text_width + 1


def duration_milliseconds(frames_per_loop, loops, frame_delay):
    """Return exact integer milliseconds for validated frame count, loops, and delay."""
    _positive_integer(frames_per_loop, "frames_per_loop")
    _positive_integer(loops, "loops")
    _positive_number(frame_delay, "frame_delay")
    milliseconds = Decimal(frames_per_loop * loops) * Decimal(str(frame_delay)) * 1000
    return int(milliseconds.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def format_duration_seconds(milliseconds):
    """Format a duration with one decimal, or an explicit unavailable state."""
    if milliseconds is None:
        return "Unavailable"
    if isinstance(milliseconds, bool) or not isinstance(milliseconds, (int, float)) or not math.isfinite(milliseconds) or milliseconds < 0:
        raise ValueError("milliseconds must be finite and nonnegative")
    tenths = (Decimal(str(milliseconds)) / 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return f"{Decimal(tenths) / 10:.1f} s"


def frame_delay_for_seconds(
    seconds, frames_per_loop, loops, *, minimum=MINIMUM_FRAME_DELAY,
    maximum=MAXIMUM_FRAME_DELAY, step=FRAME_DELAY_STEP,
):
    """Resolve requested runtime to one supported delay without changing loops."""
    _positive_number(seconds, "seconds")
    _positive_integer(frames_per_loop, "frames_per_loop")
    _positive_integer(loops, "loops")
    for value, name in ((minimum, "minimum"), (maximum, "maximum"), (step, "step")):
        _positive_number(value, name)
    if minimum > maximum:
        raise ValueError("minimum must not exceed maximum")
    ideal_ms = Decimal(str(seconds)) * 1000 / Decimal(frames_per_loop * loops)
    step_ms = Decimal(str(step)) * 1000
    rounded_ms = (ideal_ms / step_ms).quantize(Decimal("1"), rounding=ROUND_HALF_UP) * step_ms
    minimum_ms = Decimal(str(minimum)) * 1000
    maximum_ms = Decimal(str(maximum)) * 1000
    if rounded_ms < minimum_ms:
        applied_ms = minimum_ms
        reason = "clamped to minimum frame delay"
    elif rounded_ms > maximum_ms:
        applied_ms = maximum_ms
        reason = "clamped to maximum frame delay"
    else:
        applied_ms = rounded_ms
        reason = None if applied_ms == ideal_ms else f"rounded to the nearest {step:.6f}-second frame delay"
    applied_delay = float(applied_ms / 1000)
    achieved_ms = duration_milliseconds(frames_per_loop, loops, applied_delay)
    return {
        "requested_seconds": float(seconds),
        "applied_delay": applied_delay,
        "achieved_seconds": float(Decimal(achieved_ms) / 1000),
        "reason": reason,
    }


def timing_javascript():
    """Return the browser implementation with constants emitted from this module."""
    return f"""
const MATRIX_SCROLL_WIDTH={MATRIX_WIDTH};
const MATRIX_DELAY_MIN={MINIMUM_FRAME_DELAY};
const MATRIX_DELAY_MAX={MAXIMUM_FRAME_DELAY};
const MATRIX_DELAY_STEP={FRAME_DELAY_STEP};
function scrollFrames(textWidth,matrixWidth=MATRIX_SCROLL_WIDTH,direction='left'){{
  if(!Number.isInteger(textWidth)||textWidth<0||!Number.isInteger(matrixWidth)||matrixWidth<=0||!['left','right'].includes(direction))throw new Error('invalid scroll geometry');
  return matrixWidth+textWidth+1;
}}
function durationMilliseconds(frames,loops,delay){{const [numerator,denominator]=positiveDecimalRatio(delay),scaled=BigInt(frames*loops)*numerator*1000n;return Number((2n*scaled+denominator)/(2n*denominator))}}
function positiveDecimalRatio(value){{
  const source=String(value).toLowerCase(),match=source.match(/^([0-9]+)(?:[.]([0-9]*))?(?:e([+-]?[0-9]+))?$/);if(!match)throw new Error('invalid decimal');
  const fraction=match[2]||'',exponent=Number(match[3]||0),digits=BigInt(match[1]+fraction);let scale=fraction.length-exponent;
  return scale<0?[digits*10n**BigInt(-scale),1n]:[digits,10n**BigInt(scale)];
}}
function frameDelayForSeconds(seconds,frames,loops){{
  if(typeof seconds!=='number'||!Number.isFinite(seconds)||seconds<=0||!Number.isInteger(frames)||frames<=0||!Number.isInteger(loops)||loops<=0)throw new Error('invalid timing');
  const [secondsNumerator,secondsDenominator]=positiveDecimalRatio(seconds),frameLoops=BigInt(frames*loops),stepUs=1n;
  const stepNumerator=secondsNumerator*1000000n,stepDenominator=secondsDenominator*frameLoops*stepUs;
  const roundedSteps=(2n*stepNumerator+stepDenominator)/(2n*stepDenominator),roundedUs=Number(roundedSteps*stepUs);
  let appliedUs=roundedUs,reason=null;
  if(roundedUs<MATRIX_DELAY_MIN*1000000){{appliedUs=MATRIX_DELAY_MIN*1000000;reason='clamped to minimum frame delay'}}
  else if(roundedUs>MATRIX_DELAY_MAX*1000000){{appliedUs=MATRIX_DELAY_MAX*1000000;reason='clamped to maximum frame delay'}}
  else if(BigInt(appliedUs)*secondsDenominator*frameLoops!==secondsNumerator*1000000n)reason='rounded to the nearest 0.000001-second frame delay';
  return {{requested_seconds:seconds,applied_delay:appliedUs/1000000,achieved_seconds:durationMilliseconds(frames,loops,appliedUs/1000000)/1000,reason}};
}}
function oneDecimalSeconds(milliseconds){{if(milliseconds===null)return 'Unavailable';return (Math.floor(milliseconds/100+0.5)/10).toFixed(1)+' s'}}
""".strip()
