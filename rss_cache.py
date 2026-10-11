"""Atomic normalized-feed cache in an application-owned private directory.

50 MB counts payload files including temporary write reservation, not filesystem
metadata. Cooperating processes serialize via flock; no pickle or trusted HTML.
Normalized feeds and prepared thumbnails share the same eviction budget.
"""
from contextlib import contextmanager
from dataclasses import asdict
import fcntl
from hashlib import sha256
import json
import math
import os
from pathlib import Path
import re
import stat
import tempfile
import threading
import time
import struct
from fractions import Fraction
from rss_data import Feed, Story

MAX_BYTES = 50_000_000
MAX_ENTRY_BYTES = 2_000_000
_ENTRY = re.compile(r'[0-9a-f]{64}\.(json|thumb)')


def _decode_thumbnail(payload):
    from modules.rss_renderer import PreparedThumbnail
    if len(payload) < 16 or payload[:8] != b'RSSTH001':
        raise ValueError('Invalid thumbnail record')
    saved = struct.unpack('!d', payload[8:16])[0]
    data = payload[16:]
    if not math.isfinite(saved) or not data or len(data) % 776 or len(data) > 300 * 776:
        raise ValueError('Invalid thumbnail bounds')
    return PreparedThumbnail(tuple(data[i+8:i+776] for i in range(0, len(data), 776)),
        tuple(Fraction(*struct.unpack('!II', data[i:i+8])) for i in range(0, len(data), 776))), saved


def _decode(payload):
    record = json.loads(payload)
    def text(value, limit):
        if not isinstance(value, str) or len(value) > limit:
            raise ValueError('Invalid cached text')
        return value
    def texts(value, count, limit):
        if not isinstance(value, list) or len(value) > count:
            raise ValueError('Invalid cached list')
        return tuple(text(v, limit) for v in value)
    def number(value):
        if type(value) not in (int, float) or not math.isfinite(value):
            raise ValueError('Invalid cached date')
        return float(value)
    if record['version'] != 1:
        raise ValueError('Unsupported cache version')
    saved = number(record['saved'])
    raw = record['feed']
    if not isinstance(raw['stories'], list) or len(raw['stories']) > 200:
        raise ValueError('Invalid cached stories')
    stories = []
    for s in raw['stories']:
        keys = texts(s['keys'], 2, 2053)
        if not keys:
            raise ValueError('Missing cached identity')
        stories.append(Story(keys, text(s['title'], 300), text(s['summary'], 2000),
                             text(s['link'], 2048), None if s['published'] is None else number(s['published']),
                             texts(s['categories'], 20, 100), texts(s['media'], 8, 2048)))
    return Feed(text(raw['title'], 300), tuple(stories), texts(raw['warnings'], 20, 300)), saved


class DiskFeedCache:
    def __init__(self, directory, *, max_bytes=MAX_BYTES, max_entries=256):
        if type(max_bytes) is not int or not 1 <= max_bytes <= MAX_BYTES:
            raise ValueError('Cache byte limit must be 1..50000000')
        if type(max_entries) is not int or not 1 <= max_entries <= 256:
            raise ValueError('Cache entry limit must be 1..256')
        self.root = Path(directory)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.root.is_symlink() or not self.root.is_dir():
            raise ValueError('Cache requires an owned directory')
        self.max_bytes, self.max_entries = max_bytes, max_entries
        self._lock = threading.RLock()

    @contextmanager
    def _locked(self):
        with self._lock:
            fd = os.open(self.root / '.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)
                # Recover interrupted atomic writes from this dedicated cache only.
                for path in self.root.glob('rss-*.tmp'):
                    path.unlink(missing_ok=True)
                yield
            finally:
                os.close(fd)

    def _entries(self):
        result = []
        for path in self.root.iterdir():
            if _ENTRY.fullmatch(path.name):
                info = path.lstat()
                if stat.S_ISREG(info.st_mode):
                    result.append((info.st_mtime_ns, path.name, info.st_size, path))
        return sorted(result)

    def _path(self, key):
        return self.root / (sha256(key.encode()).hexdigest() + '.json')

    def _touch(self, path):
        stamp = max(time.time_ns(), max((row[0] for row in self._entries()), default=0) + 1)
        os.utime(path, ns=(stamp, stamp), follow_symlinks=False)

    def get(self, key):
        return self._get(self._path(key), _decode)

    def get_thumbnail(self, key):
        return self._get(self._path(key).with_suffix('.thumb'), _decode_thumbnail)

    def _get(self, path, decode):
        with self._locked():
            try:
                fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                with os.fdopen(fd, 'rb') as stream:
                    info = os.fstat(stream.fileno())
                    if not stat.S_ISREG(info.st_mode) or info.st_size > min(MAX_ENTRY_BYTES, self.max_bytes):
                        return None
                    record = decode(stream.read(MAX_ENTRY_BYTES + 1))
                self._touch(path)
                return record
            except (OSError, ValueError, KeyError, TypeError, RecursionError, ArithmeticError, struct.error):
                return None

    def clear(self):
        """Remove only recognized RSS payloads in the owned cache directory."""
        with self._locked():
            for *_, path in self._entries():
                path.unlink(missing_ok=True)

    def put_thumbnail(self, key, thumbnail, saved):
        from modules.rss_renderer import PreparedThumbnail
        if type(thumbnail) is not PreparedThumbnail:
            raise ValueError('Expected a bounded prepared thumbnail')
        payload = b'RSSTH001' + struct.pack('!d', saved) + b''.join(
            struct.pack('!II', delay.numerator, delay.denominator) + frame
            for frame, delay in zip(thumbnail.frames, thumbnail.durations_ms))
        _decode_thumbnail(payload)
        return self._put(self._path(key).with_suffix('.thumb'), payload)

    def put(self, key, feed, saved):
        payload = json.dumps({'version': 1, 'saved': saved, 'feed': asdict(feed)},
                             ensure_ascii=False, allow_nan=False, separators=(',', ':')).encode()
        if len(payload) > min(MAX_ENTRY_BYTES, self.max_bytes):
            return False
        _decode(payload)  # Apply identical schema/size policy on write and read.
        return self._put(self._path(key), payload)

    def _put(self, path, payload):
        if len(payload) > min(MAX_ENTRY_BYTES, self.max_bytes):
            return False
        with self._locked():
            entries = self._entries()
            old_size = next((row[2] for row in entries if row[3] == path), 0)
            if old_size + len(payload) > self.max_bytes:
                return False  # Keep old success rather than break atomicity to fit.
            total, count = sum(row[2] for row in entries), len(entries)
            for _, _, size, victim in entries:
                if total + len(payload) <= self.max_bytes and count + (0 if old_size else 1) <= self.max_entries:
                    break
                if victim != path:
                    victim.unlink()
                    total -= size
                    count -= 1
            fd, temporary = tempfile.mkstemp(prefix='rss-', suffix='.tmp', dir=self.root)
            try:
                with os.fdopen(fd, 'wb') as stream:
                    stream.write(payload)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, path)
                self._touch(path)
                directory_fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            finally:
                Path(temporary).unlink(missing_ok=True)
        return True