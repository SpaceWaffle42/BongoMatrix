"""Opt-in, bounded RSS media preparation. Never call from the render loop.

Only SafeFeedClient downloads. Decoder receives anonymous file descriptors, never
publisher paths. A process group contains the decoder and any media-tool children.
"""
from dataclasses import dataclass
from fractions import Fraction
import os
from pathlib import Path
import signal
import struct
import subprocess
import sys
import tempfile
import threading
import time
from modules.rss_renderer import PreparedThumbnail
from rss_transport import SafeFeedClient

MAX_DOWNLOAD = 5_000_000
MAX_PREPARATION_SECONDS = 3.0
_SLOTS = threading.BoundedSemaphore(2)


@dataclass(frozen=True)
class ThumbnailResult:
    thumbnail: PreparedThumbnail | None = None
    warning: str = ''


class ThumbnailPreparer:
    def __init__(self, *, client=None, budget=MAX_PREPARATION_SECONDS):
        if not 0 < budget <= MAX_PREPARATION_SECONDS:
            raise ValueError('Preparation budget must be positive and at most three seconds')
        if client is not None and not isinstance(client, SafeFeedClient):
            raise ValueError('Media requires SafeFeedClient')
        source = client or SafeFeedClient()
        self.client = SafeFeedClient(resolver=source.resolver, connector=source.connector,
            max_bytes=min(source.max_bytes, MAX_DOWNLOAD) if client else MAX_DOWNLOAD,
            connect_timeout=min(source.connect_timeout, budget), read_timeout=min(source.read_timeout, budget),
            total_timeout=min(source.total_timeout, budget), max_redirects=min(source.max_redirects, 3))
        self.budget = budget

    def prepare(self, url, stop_event=None):
        stop = stop_event or threading.Event()
        deadline = time.monotonic() + self.budget
        if stop.is_set():
            return ThumbnailResult(warning='Thumbnail cancelled')
        if not _SLOTS.acquire(blocking=False):
            return ThumbnailResult(warning='Thumbnail preparation busy; using text only')
        child = None
        try:
            payload = self.client.fetch(url, stop_event=stop)
            if len(payload) > MAX_DOWNLOAD:
                raise ValueError('Download too large')
            if stop.is_set() or time.monotonic() >= deadline:
                raise TimeoutError
            with tempfile.TemporaryFile() as source, tempfile.TemporaryFile() as output:
                source.write(payload)
                source.flush()
                source.seek(0)
                child = subprocess.Popen([sys.executable, str(Path(__file__).with_name('rss_thumbnail_worker.py')),
                    str(source.fileno()), str(output.fileno())], pass_fds=(source.fileno(), output.fileno()),
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    start_new_session=True)
                while child.poll() is None:
                    if stop.is_set() or time.monotonic() >= deadline:
                        raise TimeoutError
                    stop.wait(min(.01, max(0, deadline - time.monotonic())))
                if child.returncode or stop.is_set() or time.monotonic() >= deadline:
                    raise ValueError('Media rejected')
                output.seek(0)
                data = output.read(300 * 776 + 1)
                if not data or len(data) % 776 or len(data) > 300 * 776:
                    raise ValueError('Invalid decoder output')
                thumbnail = PreparedThumbnail(tuple(data[i+8:i+776] for i in range(0, len(data), 776)),
                    tuple(Fraction(*struct.unpack('!II', data[i:i+8])) for i in range(0, len(data), 776)))
                return ThumbnailResult(thumbnail)
        except Exception:
            return ThumbnailResult(warning='Thumbnail cancelled' if stop.is_set() else 'Thumbnail rejected or timed out; using text only')
        finally:
            if child is not None:
                if child.poll() is None:
                    try:
                        os.killpg(child.pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass
                    try:
                        child.wait(timeout=.2)
                    except subprocess.TimeoutExpired:
                        pass
                try:
                    os.killpg(child.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                child.wait()
            _SLOTS.release()
