"""Messages between the Matrix and an imported module's child process, over its stdin and stdout:
a 4-byte length, a JSON header, and (for a frame) a 4-byte length and the raw pixels."""
from __future__ import annotations

import json
import struct

MOST_HEADER = 1 << 20          # 1 MB of JSON is far more than any message needs
MOST_BODY = 4096 * 4096 * 3    # a frame, however big a panel could be


class WireError(Exception):
    pass


def send(stream, header: dict, body: bytes | None = None) -> None:
    data = json.dumps(header, separators=(",", ":")).encode()
    out = struct.pack(">I", len(data)) + data + struct.pack(">I", len(body) if body is not None else 0xFFFFFFFF)
    stream.write(out + (body or b""))
    stream.flush()


def _exactly(stream, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        part = stream.read(n - len(buf))
        if not part:
            raise WireError("the other side closed")
        buf += part
    return bytes(buf)


def recv(stream, most_body: int = MOST_BODY) -> tuple[dict, bytes | None]:
    (size,) = struct.unpack(">I", _exactly(stream, 4))
    if size > MOST_HEADER:
        raise WireError("message too big")
    header = json.loads(_exactly(stream, size))
    if not isinstance(header, dict):
        raise WireError("not a message")
    (size,) = struct.unpack(">I", _exactly(stream, 4))
    if size == 0xFFFFFFFF:
        return header, None
    if size > most_body:
        raise WireError("frame too big")
    return header, _exactly(stream, size)
