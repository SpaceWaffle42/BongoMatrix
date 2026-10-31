"""Safe gzip response compression for the Flask app (stdlib only).

The dashboard page is ~500 KB of HTML with large inline JSON catalogues; gzip turns that
into ~100 KB, which matters on the Raspberry Pi's Wi-Fi link right after pairing. Only
complete (200, non-range) text-like responses are compressed. Dynamic pages and API
responses keep whatever Cache-Control they already send; compressed bytes are cached only
for /static files, keyed by (path, mtime, size).
"""

import gzip
import os
import threading
from collections import OrderedDict
from pathlib import Path

from werkzeug.security import safe_join

COMPRESSIBLE_MIMETYPES = frozenset({
    "text/html",
    "application/json",
    "text/css",
    "application/javascript",
    "text/javascript",
    "image/svg+xml",
})
MIN_COMPRESS_BYTES = 1024
COMPRESSION_LEVEL = 5
STATIC_CACHE_MAX_ENTRIES = 32
STATIC_CACHE_MAX_BYTES = 8 * 1024 * 1024
STATIC_FILE_MAX_BYTES = 4 * 1024 * 1024


class StaticCompressionCache:
    """Small LRU of gzip bytes for static files, bounded by entries and total size."""

    def __init__(self, max_entries=STATIC_CACHE_MAX_ENTRIES, max_bytes=STATIC_CACHE_MAX_BYTES):
        self._max_entries = max_entries
        self._max_bytes = max_bytes
        self._items = OrderedDict()
        self._bytes = 0
        self._lock = threading.Lock()

    def __len__(self):
        with self._lock:
            return len(self._items)

    def get(self, key):
        with self._lock:
            body = self._items.get(key)
            if body is not None:
                self._items.move_to_end(key)
            return body

    def put(self, key, body):
        with self._lock:
            previous = self._items.pop(key, None)
            if previous is not None:
                self._bytes -= len(previous)
            # Superseded revisions of the same path are dead weight.
            for stale in [k for k in self._items if k[0] == key[0]]:
                self._bytes -= len(self._items.pop(stale))
            self._items[key] = body
            self._bytes += len(body)
            while self._items and (
                len(self._items) > self._max_entries or self._bytes > self._max_bytes
            ):
                _, evicted = self._items.popitem(last=False)
                self._bytes -= len(evicted)


def accepts_gzip(header_value):
    """True when Accept-Encoding lists gzip with a non-zero quality."""
    for part in (header_value or "").split(","):
        name, _, params = part.partition(";")
        if name.strip().lower() not in ("gzip", "x-gzip"):
            continue
        quality = 1.0
        for param in params.split(";"):
            key, _, value = param.partition("=")
            if key.strip().lower() == "q":
                try:
                    quality = float(value.strip())
                except ValueError:
                    quality = 0.0
        return quality > 0
    return False


def _append_vary(headers, token):
    existing = [v.strip() for v in headers.get("Vary", "").split(",") if v.strip()]
    if "*" in existing or token.lower() in (v.lower() for v in existing):
        return
    headers["Vary"] = ", ".join(existing + [token])


def _gzip(data):
    return gzip.compress(data, compresslevel=COMPRESSION_LEVEL, mtime=0)


def register_http_compression(app):
    """Register the gzip after_request hook on ``app``; returns the static cache."""
    from flask import request

    static_cache = StaticCompressionCache()
    app.extensions["http_compression_static_cache"] = static_cache

    def static_source(response):
        """(path, stat) of the file behind a successful /static response, else None."""
        if request.endpoint != "static" or not app.static_folder:
            return None
        filename = (request.view_args or {}).get("filename", "")
        path = safe_join(app.static_folder, filename)
        if path is None:
            return None
        path = Path(path)
        try:
            stat = path.stat()
        except OSError:
            return None
        if not (MIN_COMPRESS_BYTES <= stat.st_size <= STATIC_FILE_MAX_BYTES):
            return None
        return path, stat

    def compressed_static(path, stat):
        key = (str(path), stat.st_mtime_ns, stat.st_size)
        body = static_cache.get(key)
        if body is not None:
            return body
        try:
            with path.open("rb") as handle:
                opened = os.fstat(handle.fileno())
                data = handle.read(STATIC_FILE_MAX_BYTES + 1)
        except OSError:
            return None
        if len(data) != opened.st_size or opened.st_mtime_ns != stat.st_mtime_ns:
            return None  # file changed under us; send it untouched this once
        body = _gzip(data)
        static_cache.put(key, body)
        return body

    @app.after_request
    def compress_response(response):
        if response.status_code != 200 or response.mimetype not in COMPRESSIBLE_MIMETYPES:
            return response
        headers = response.headers
        if (
            "Content-Encoding" in headers
            or "Content-Range" in headers
            or "no-transform" in headers.get("Cache-Control", "").lower()
        ):
            return response
        source = None
        data = None
        if response.direct_passthrough:
            source = static_source(response)
            if source is None:
                return response
        elif response.is_sequence:
            data = response.get_data()
            if len(data) < MIN_COMPRESS_BYTES:
                return response
        else:
            return response

        # Large enough to be compressed for gzip clients, so caches must key on the header.
        _append_vary(headers, "Accept-Encoding")
        if request.headers.get("Range") or not accepts_gzip(request.headers.get("Accept-Encoding")):
            return response

        if source is not None:
            body = compressed_static(*source)
            original_size = source[1].st_size
        else:
            body = _gzip(data)
            original_size = len(data)
        if body is None or len(body) >= original_size:
            return response
        if source is not None:
            close = getattr(response.response, "close", None)
            if close is not None:
                close()
            response.direct_passthrough = False
            etag = headers.get("ETag")
            if etag and not etag.startswith("W/"):
                headers["ETag"] = "W/" + etag  # same validators, different bytes
        response.set_data(body)
        headers["Content-Encoding"] = "gzip"
        return response

    return static_cache
