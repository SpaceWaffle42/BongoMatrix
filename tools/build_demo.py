"""Render the ?demo dashboard's sprite sheets from the real module code.

    python tools/build_demo.py [--frames 24] [--seconds 0.25]

Writes bongo/web/static/demo/index.json (git-ignored): one horizontal PNG strip per
module/style, as data URIs, which demo.js plays back in place of the Pi's live frames.
"""
from __future__ import annotations

import argparse
import base64
import datetime as dt
import io
import json
import random
import sys
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bongo.module import Context  # noqa: E402
from bongo.registry import discover  # noqa: E402

OUT = ROOT / "bongo" / "web" / "static" / "demo"
START = dt.datetime(2026, 11, 11, 10, 21, 50)


def strip(frames: list[Image.Image]) -> str:
    sheet = Image.new("RGB", (64 * len(frames), 32))
    for k, f in enumerate(frames):
        sheet.paste(f.convert("RGB").resize((64, 32)), (64 * k, 0))
    buf = io.BytesIO()
    sheet.save(buf, "PNG", optimize=True)
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


def render(module, style: str, frames: int, seconds: float) -> list[Image.Image]:
    s = module.create(style, None, Context(rng=random.Random(3)))
    return [s.render(START + dt.timedelta(seconds=k * seconds)) for k in range(frames)]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--frames", type=int, default=24)
    ap.add_argument("--seconds", type=float, default=0.25)
    args = ap.parse_args(argv)
    sheets = {}
    for mid, module in discover().items():
        for style in module.styles:
            try:
                sheets[f"{mid}/{style}"] = strip(render(module, style, args.frames, args.seconds))
            except Exception as error:  # one broken style must not stop the rest
                print(f"skipped {mid}/{style}: {error}", file=sys.stderr)
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "index.json").write_text(json.dumps({"frames": args.frames, "seconds": args.seconds, "sheets": sheets}))
    print(f"{len(sheets)} sheets -> {OUT / 'index.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
