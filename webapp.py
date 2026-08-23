"""Flask control panel and PIN-pairing routes."""

from __future__ import annotations

import base64
import hashlib
import io
import json
import math
import copy
import secrets
import logging
import os
import re
import stat
import subprocess
import sys
import tempfile
import threading
import time
import datetime as dt
from contextlib import ExitStack
from datetime import timedelta
from enum import Enum
from functools import wraps
from pathlib import Path

from flask import Flask, abort, jsonify, redirect, render_template, request, send_file, session, url_for
from PIL import Image, UnidentifiedImageError
from werkzeug.exceptions import HTTPException, RequestEntityTooLarge
from werkzeug.utils import secure_filename

from acceptance_artifacts import AcceptanceArtifactTransaction
from config_store import (
    ALLOWED_MODULES, DEFAULT_CONFIG, ConfigConflictError, ConfigDurabilityError,
    validate_config,
)
from font_catalog import available_font_names, font_advance_catalog
from module_options import COLOUR_HEX, LAVA_PALETTES
from modules.weather_app import OpenMeteoClient
from modules.scroll_timing_cache import ScrollTimingCache, TimingCacheBusy
from modules.scroll_timing import timing_javascript
from modules import announcement_board, weather_app, openweather_overview, openweather_forecast
from media_resolver import MediaResolutionError, pin_candidate_media
from pairing import PairedDeviceStore, PairingDurabilityError
from secret_store import SecretDurabilityError, SecretStore
from edit_preview import (
    CaptureMatrix, PreviewCapacityError, PreviewInstanceCapacityError,
    PreviewInstanceRegistry, PreviewPageSupersededError, PreviewSessionConflictError,
    PreviewSessionExpiredError, PreviewTokenConsumedError,
)
from preview_provider import (
    PreviewProvider, PreviewProviderCancelled, PreviewProviderCapacityError,
    PreviewProviderTimeout, PreviewProviderUnavailable,
)
from device_preview import (
    DevicePreviewOwner, DevicePreviewRequest, PreviewAdmissionStatus,
    HEARTBEAT_INTERVAL_SECONDS,
    PreviewHeartbeatStatus,
)

MEDIA_EXTENSIONS = {".gif", ".png", ".jpg", ".jpeg", ".webp", ".mp4"}
Image.MAX_IMAGE_PIXELS = 1_000_000
LOGGER = logging.getLogger(__name__)
DEVICE_REMOVAL_DURABILITY_WARNING = "The device was removed, but durable storage could not be confirmed."
MEDIA_METADATA_ERRORS = (
    OSError, ValueError, KeyError, IndexError, json.JSONDecodeError,
    subprocess.SubprocessError, UnidentifiedImageError, Image.DecompressionBombError,
)
CONFIG_REQUEST_LIMIT = 1_048_576
CONDITIONAL_HEADER_LIMIT = 4096
MINIMUM_CLIENT_CONTRACT = 2
APPLICATION_VERSION = "0.3.58-preview"
DEVICE_RESTART_PENDING_TTL_SECONDS = 60
PAIRED_ACTION_HEADER = "X-Matrix-Action"
PAIRED_ACTION_VALUE = "paired-request"
ETAG_ITEM = re.compile(r'(?P<weak>W/)?"(?P<opaque>[\x21\x23-\x7e\x80-\xff]*)"')
EDIT_PREVIEW_INSTANCE = re.compile(r"[0-9a-f]{32}")
EDIT_PREVIEW_SESSION = re.compile(r"[0-9a-f]{32}")
EDIT_PREVIEW_PROVIDER_DEADLINE_SECONDS = 8.0
EDIT_PREVIEW_TOKEN = re.compile(r"[0-9a-f]{64}")


class RestartRequestOutcome(Enum):
    CREATED = "created"
    ACTIVE_REQUEST = "active_request"
    MANUAL_RECOVERY_REQUIRED = "manual_recovery_required"


def _open_trusted_restart_directory(path):
    path = Path(path)
    if not path.is_absolute() or any(part in ("", ".", "..") for part in path.parts[1:]):
        raise RuntimeError("unsafe fixed restart request directory ancestry")
    parent_fd = os.open(
        "/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
    )
    try:
        components = path.parts[1:]
        for index, component in enumerate(components):
            if index:
                ancestor = os.fstat(parent_fd)
                if (
                    not stat.S_ISDIR(ancestor.st_mode)
                    or ancestor.st_uid != 0
                    or ancestor.st_gid != 0
                    or stat.S_IMODE(ancestor.st_mode) & 0o022
                ):
                    raise RuntimeError("unsafe fixed restart request directory ancestry")
            child_fd = os.open(
                component,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=parent_fd,
            )
            os.close(parent_fd)
            parent_fd = child_fd
        return parent_fd
    except BaseException:
        os.close(parent_fd)
        raise


class RestartRequest:
    """Publish the one fixed zero-byte marker accepted by the host broker."""

    def __init__(self, path):
        self.path = Path(path)

    def __call__(self):
        parent_fd = _open_trusted_restart_directory(self.path.parent)
        fd = None
        try:
            parent = os.fstat(parent_fd)
            if (
                not stat.S_ISDIR(parent.st_mode)
                or parent.st_uid != 0
                or parent.st_gid != os.getegid()
                or stat.S_IMODE(parent.st_mode) != 0o1730
            ):
                raise RuntimeError("unsafe fixed restart request directory")
            try:
                fd = os.open(
                    self.path.name,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                    0o600,
                    dir_fd=parent_fd,
                )
            except FileExistsError:
                info = os.stat(self.path.name, dir_fd=parent_fd, follow_symlinks=False)
                if (
                    not stat.S_ISREG(info.st_mode)
                    or (info.st_uid, info.st_gid)
                    not in ((os.geteuid(), os.getegid()), (0, 0))
                    or stat.S_IMODE(info.st_mode) != 0o600
                    or info.st_nlink != 1
                    or info.st_size != 0
                ):
                    raise RuntimeError("unsafe fixed restart request marker")
                return RestartRequestOutcome.ACTIVE_REQUEST
            os.fchmod(fd, 0o600)
            os.fsync(fd)
            os.fsync(parent_fd)
            return RestartRequestOutcome.CREATED
        finally:
            if fd is not None:
                os.close(fd)
            os.close(parent_fd)


class EditPreviewRenderError(RuntimeError):
    """Sanitized boundary for provider, decode, or render failures."""


class EditPreviewDecodeError(RuntimeError):
    """Preview frame decoding failed without exposing media details."""


def _provider_image(image):
    output = io.BytesIO()
    image.save(output, format="PNG")
    return base64.b64encode(output.getvalue()).decode("ascii")


def _edit_preview_provider_child(candidate, _credentials, timeout):
    """Fetch and serialize normalized provider data in the bounded child."""
    module_name = candidate.pop("_preview_provider_module")
    if module_name == "weather":
        settings = candidate["weather"]
        weather = OpenMeteoClient(timeout=timeout).fetch(
            settings["location"], latitude=settings.get("latitude"),
            longitude=settings.get("longitude"), units=settings["units"],
            deadline=time.monotonic() + timeout,
        )
        return vars(weather)

    api_key = candidate.pop("_preview_openweather_key", None)
    if not api_key:
        raise RuntimeError("OpenWeather preview is unavailable")
    if module_name == "openweather_overview":
        prepared = openweather_overview.prepare(candidate, api_key=api_key)
        if prepared.error or prepared.cancelled:
            raise RuntimeError("OpenWeather preview is unavailable")
        return {
            "weather": vars(prepared.weather),
            "icon": _provider_image(prepared.icon),
        }
    prepared = openweather_forecast.prepare(candidate, api_key=api_key)
    if prepared.error or prepared.cancelled:
        raise RuntimeError("OpenWeather preview is unavailable")
    return {
        "periods": [vars(period) for period in prepared.periods],
        "icons": {
            icon_code: _provider_image(image)
            for icon_code, image in prepared.icons.items()
        },
    }


def _provider_value(value):
    """Thaw immutable provider transport into renderer model inputs."""
    if isinstance(value, tuple):
        if all(
            isinstance(item, tuple) and len(item) == 2 and isinstance(item[0], str)
            for item in value
        ):
            return {key: _provider_value(item) for key, item in value}
        return [_provider_value(item) for item in value]
    return value


def _prepare_edit_preview_provider(provider, module_name, candidate, stop_event):
    """Prepare provider data through the app-owned process-global bounded seam."""
    if module_name not in {"weather", "openweather_overview", "openweather_forecast"}:
        return None
    provider_candidate = copy.deepcopy(candidate)
    provider_candidate["_preview_provider_module"] = module_name
    transported = provider.prepare(
        _edit_preview_provider_child,
        settings=provider_candidate, credentials=None,
        cancel_event=stop_event,
        request_deadline=EDIT_PREVIEW_PROVIDER_DEADLINE_SECONDS,
    )
    prepared = _provider_value(transported)
    if module_name == "weather":
        from modules.weather_app import WeatherSnapshot
        return WeatherSnapshot(**prepared)
    if module_name == "openweather_overview":
        from modules.openweather_common import OpenWeatherSnapshot
        icon = Image.open(io.BytesIO(base64.b64decode(prepared["icon"])))
        icon.load()
        return openweather_overview.PreparedOverview(
            weather=OpenWeatherSnapshot(**prepared["weather"]), icon=icon,
        )
    from modules.openweather_common import OpenWeatherForecastPeriod
    icons = {}
    for icon_code, encoded in prepared["icons"].items():
        icon = Image.open(io.BytesIO(base64.b64decode(encoded)))
        icon.load()
        icons[icon_code] = icon
    return openweather_forecast.PreparedForecast(
        periods=[OpenWeatherForecastPeriod(**period) for period in prepared["periods"]],
        icons=icons,
    )


def _validate_current_lava_selection(module_name, candidate, selected_palette):
    """Validate transient Lava inspection against the accepted current draft."""
    if selected_palette is None:
        return None
    if module_name != "clock_lava" or not isinstance(selected_palette, dict):
        raise ValueError("selected_lava_palette is invalid")
    kind = selected_palette.get("kind")
    palette_id = selected_palette.get("id")
    if not isinstance(kind, str) or not isinstance(palette_id, str):
        raise ValueError("selected_lava_palette is invalid")
    lava = candidate["lava"]
    if kind == "built_in":
        if (
            set(selected_palette) != {"kind", "id"}
            or palette_id not in LAVA_PALETTES
        ):
            raise ValueError("selected_lava_palette is invalid")
        return {"kind": kind, "id": palette_id}
    if kind == "custom" and set(selected_palette) == {"kind", "id", "colours"}:
        matched = next(
            (
                palette for palette in lava["custom_palettes"]
                if palette["id"] == palette_id
            ),
            None,
        )
        if matched is not None and selected_palette["colours"] == matched["colours"]:
            return {
                "kind": kind,
                "id": palette_id,
                "colours": copy.deepcopy(matched["colours"]),
            }
    raise ValueError("selected_lava_palette is invalid")


def _edit_preview_iterator(
    module_name, candidate, selected_palette, pinned_media, prepared, stop_event,
):
    """Construct a capture-only iterator without touching display-controller state."""
    from edit_preview import (
        advanced_canvas_preview_frames, announcement_preview_frames,
        continuous_lava_preview_frames, custom_text_preview_frames,
        lava_preview_frames, media_preview_frames, openweather_forecast_preview_frames,
        openweather_overview_preview_frames, preview_duration,
        weather_preview_frames,
    )

    matrix = CaptureMatrix()
    if module_name in {"splash", "sleep"}:
        filename = candidate["media"][module_name]
        return media_preview_frames(
            matrix, filename, preview_duration(candidate, module_name),
            candidate.get("media_layout", {}).get(module_name),
            stop_event=stop_event, pinned_handle=pinned_media.get(filename),
        )
    if module_name == "clock_lava":
        if selected_palette is not None:
            return continuous_lava_preview_frames(
                matrix, candidate, selected_palette, stop_event,
            )
        return lava_preview_frames(matrix, candidate)
    if module_name == "weather":
        return weather_preview_frames(matrix, candidate, prepared, stop_event)
    if module_name == "openweather_overview":
        return openweather_overview_preview_frames(
            matrix, candidate, prepared, stop_event,
        )
    if module_name == "openweather_forecast":
        return openweather_forecast_preview_frames(
            matrix, candidate, prepared, stop_event,
        )
    if module_name == "announcement_board":
        settings = candidate["announcement_board"]
        return announcement_preview_frames(
            matrix, settings["announcements"],
            preview_duration(candidate, module_name), stop_event,
            pinned_handles=pinned_media,
        )
    custom = next(
        (item for item in candidate["custom_modules"] if item["id"] == module_name),
        None,
    )
    if custom is None:
        raise ValueError("preview module is unavailable")
    if custom["type"] == "canvas":
        return advanced_canvas_preview_frames(
            matrix, custom, preview_duration(candidate, module_name), stop_event,
            pinned_handles=pinned_media,
        )
    return custom_text_preview_frames(matrix, custom)


def _revision_etag(revision):
    return f'"{revision}"'


def _parse_revision_validators(value):
    if value is None:
        return None
    try:
        encoded_length = len(value.encode("latin-1"))
    except UnicodeEncodeError as exc:
        raise ValueError("malformed conditional revision header") from exc
    if encoded_length > CONDITIONAL_HEADER_LIMIT:
        raise OverflowError("conditional header exceeds 4 KiB")
    if value.strip(" \t") == "*":
        return "*"
    validators = []
    position = 0
    while position < len(value):
        while position < len(value) and value[position] in " \t":
            position += 1
        match = ETAG_ITEM.match(value, position)
        if match is None:
            raise ValueError("malformed conditional revision header")
        validators.append((match.group("weak") is not None, match.group("opaque")))
        position = match.end()
        while position < len(value) and value[position] in " \t":
            position += 1
        if position == len(value):
            break
        if value[position] != ",":
            raise ValueError("malformed conditional revision header")
        position += 1
        if not value[position:].strip(" \t"):
            raise ValueError("malformed conditional revision header")
    if not validators:
        raise ValueError("malformed conditional revision header")
    return validators


class MediaSanitizerError(ValueError):
    """Sanitizer rejection with ownership of its private output path."""

    def __init__(self, output, cleanup_failed=False):
        super().__init__("invalid or unsafe media")
        self.output = output
        self.cleanup_failed = cleanup_failed


def create_app(
    controller,
    config_store,
    pairing,
    secret_key,
    session_generation=None,
    media_dir=None,
    location_search=None,
    sanitizer_path=None,
    sanitizer_python=None,
    sanitizer_identity=None,
    device_store=None,
    openweather_secrets=None,
    scroll_timing_cache=None,
    device_restart_runner=None,
    acceptance_authorization_file=None,
    acceptance_runtime=None,
):
    session_generation = session_generation or secrets.token_hex(16)
    media_root = Path(media_dir or Path(__file__).resolve().parent / "resources" / "media")
    media_root.mkdir(parents=True, exist_ok=True)
    acceptance_authorization = None
    if (acceptance_authorization_file is None) != (acceptance_runtime is None):
        raise ValueError(
            "acceptance authorization and runtime must be configured together"
        )
    if acceptance_authorization_file is not None:
        authorization_path = Path(acceptance_authorization_file)
        info = authorization_path.lstat()
        if (not stat.S_ISREG(info.st_mode) or authorization_path.is_symlink()
                or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_nlink != 1 or info.st_size > 256):
            raise ValueError("acceptance authorization file is unsafe")
        descriptor = os.open(authorization_path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
        try:
            before = os.fstat(descriptor)
            raw_authorization = os.read(descriptor, 257)
            after = os.fstat(descriptor)
        finally:
            os.close(descriptor)
        if (len(raw_authorization) > 256 or
                (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) !=
                (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)):
            raise ValueError("acceptance authorization changed while read")
        try:
            acceptance_authorization = raw_authorization.decode("ascii").strip()
        except UnicodeDecodeError as error:
            raise ValueError("acceptance authorization is invalid") from error
        if re.fullmatch(r"[0-9a-f]{64}", acceptance_authorization or "") is None:
            raise ValueError("acceptance authorization is invalid")
        acceptance_runtime = Path(acceptance_runtime)
    built_in_media_root = media_root / "built-in"
    media_lock = threading.RLock()
    media_timeline_slots = threading.BoundedSemaphore(2)
    sanitizer = Path(sanitizer_path or Path(__file__).resolve().parent / "media_sanitizer.py")
    sanitizer_python = sanitizer_python or sys.executable
    location_search = location_search or OpenMeteoClient(timeout=5).search_locations
    device_store = device_store or PairedDeviceStore()
    if openweather_secrets is None:
        config_path = getattr(config_store, "path", None)
        secret_root = Path(config_path).parent if config_path is not None else media_root.parent
        openweather_secrets = SecretStore(secret_root / "secrets" / "openweather-api-key")
    scroll_timing_cache = scroll_timing_cache or ScrollTimingCache()
    default_built_in_media = frozenset(DEFAULT_CONFIG["media"].values())
    openweather_credential_generation = 0
    openweather_credential_lock = threading.Lock()
    edit_preview_registry = None
    edit_preview_provider = None
    edit_preview_services_lock = threading.Condition()
    edit_preview_services_initializing = False
    device_restart_lock = threading.Lock()
    device_restart_pending_until = 0.0

    def get_edit_preview_services():
        """Lazily publish one healthy registry/provider pair per app process."""
        nonlocal edit_preview_registry, edit_preview_provider
        nonlocal edit_preview_services_initializing
        with edit_preview_services_lock:
            while edit_preview_services_initializing:
                edit_preview_services_lock.wait()
            if (
                edit_preview_registry is not None
                and edit_preview_provider is not None
                and not edit_preview_registry.is_closed
                and not edit_preview_provider.is_closed
            ):
                return edit_preview_registry, edit_preview_provider
            edit_preview_services_initializing = True

        registry = None
        provider = None
        try:
            registry = PreviewInstanceRegistry()
            provider = PreviewProvider()
        except BaseException:
            # Constructors and cleanup are outside the lifecycle lock.
            if provider is not None:
                provider.shutdown()
            if registry is not None:
                registry.close()
            with edit_preview_services_lock:
                edit_preview_services_initializing = False
                edit_preview_services_lock.notify_all()
            raise

        with edit_preview_services_lock:
            edit_preview_registry = registry
            edit_preview_provider = provider
            app.extensions["edit_preview_registry"] = registry
            app.extensions["edit_preview_provider"] = provider
            edit_preview_services_initializing = False
            edit_preview_services_lock.notify_all()
            return registry, provider

    def close_edit_preview_services():
        """Close the published pair without preventing a later lazy restart."""
        with edit_preview_services_lock:
            registry = edit_preview_registry
            provider = edit_preview_provider
        # Never hold the pair lock during callbacks, provider work, or joins.
        if registry is not None:
            registry.close()
        if provider is not None:
            provider.shutdown()

    def current_openweather_credential_generation():
        with openweather_credential_lock:
            return openweather_credential_generation

    def rotate_openweather_credentials():
        nonlocal openweather_credential_generation
        with openweather_credential_lock:
            openweather_credential_generation += 1
            scroll_timing_cache.clear(provider="openweather")

    app = Flask(__name__, template_folder="app/templates", static_folder="app/static")
    app.jinja_env.globals["application_version"] = APPLICATION_VERSION
    app.extensions["get_edit_preview_services"] = get_edit_preview_services
    app.extensions["close_edit_preview_services"] = close_edit_preview_services
    app.extensions["media_timeline_slots"] = media_timeline_slots
    app.config.update(
        SECRET_KEY=secret_key,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Strict",
        PERMANENT_SESSION_LIFETIME=timedelta(days=365),
        MAX_CONTENT_LENGTH=128 * 1024 * 1024,
    )

    def pairing_session_is_current():
        return (
            session.get("paired")
            and session.get("generation") == session_generation
            and device_store.contains(session.get("device_id"))
        )

    def pairing_expired_response():
        device_id = session.get("device_id")
        if device_id:
            controller.cancel_device_drafts_for_device(device_id)
        session.clear()
        if request.path.startswith("/api/"):
            return jsonify({"error": "Pairing session expired."}), 401
        return redirect(url_for("pair"))

    @app.before_request
    def require_paired_action_signal():
        if request.method not in {"POST", "PUT", "PATCH", "DELETE"} or not session.get("paired"):
            return None
        if not pairing_session_is_current():
            return pairing_expired_response()
        expected = (
            "restart-whole-device"
            if request.endpoint == "restart_device"
            else PAIRED_ACTION_VALUE
        )
        if request.headers.get(PAIRED_ACTION_HEADER) != expected:
            if request.path.startswith("/api/"):
                return jsonify({"error": "Paired action authorization failed."}), 403
            return "Paired action authorization failed.", 403
        return None

    @app.after_request
    def advertise_client_contract(response):
        if request.path == "/api/config":
            response.headers["X-Matrix-Minimum-Client-Contract"] = str(MINIMUM_CLIENT_CONTRACT)
        if request.path.startswith(("/api/edit-preview", "/api/device-preview")):
            response.headers["Cache-Control"] = "no-store"
            response.headers.pop("ETag", None)
        return response

    def sync_media_directory():
        directory_fd = os.open(media_root, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)

    def identity_options():
        if sanitizer_identity is None:
            return {}
        uid, gid = sanitizer_identity
        return {"user": uid, "group": gid, "extra_groups": []}

    def video_metadata(fd):
        result = subprocess.run(
            [
                "/usr/bin/ffprobe", "-v", "error", "-select_streams", "v:0",
                "-show_entries", "stream=width,height,avg_frame_rate,r_frame_rate,nb_frames,duration:format=duration",
                "-of", "json",
                f"pipe:{fd}",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
            pass_fds=(fd,),
            **identity_options(),
        )
        metadata = json.loads(result.stdout)
        stream = metadata["streams"][0]
        width, height = int(stream["width"]), int(stream["height"])
        duration = float(stream.get("duration") or metadata.get("format", {}).get("duration"))
        rate_text = stream.get("avg_frame_rate") or stream.get("r_frame_rate") or "0/1"
        numerator, denominator = rate_text.split("/", 1)
        source_fps = float(numerator) / float(denominator)
        source_frames = int(stream.get("nb_frames") or round(duration * source_fps))
        duration_ms = max(1, round(duration * 1000))
        fps = max(1, min(60, round(source_fps)))
        if duration_ms * fps > 120_000:
            fps = max(1, min(fps, math.floor(120_000 / duration_ms)))
        return width, height, {
            "animated": True,
            "source_frames": max(1, source_frames),
            "frame_count": max(1, min(120, round(duration_ms * fps / 1000))),
            "fps": fps,
            "duration_ms": duration_ms,
        }

    def media_item(path):
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                raise OSError("not a regular file")
            animation = {
                "animated": False, "source_frames": 1,
                "frame_count": 1, "fps": 1, "duration_ms": None,
            }
            if path.suffix.lower() == ".mp4":
                width, height, animation = video_metadata(fd)
            else:
                with os.fdopen(fd, "rb", closefd=False) as handle:
                    with Image.open(handle) as image:
                        width, height = image.size
                        source_frames = int(getattr(image, "n_frames", 1))
                        if source_frames > 1:
                            durations = []
                            for frame_index in range(source_frames):
                                image.seek(frame_index)
                                durations.append(max(20, int(image.info.get("duration", 100) or 100)))
                            duration_ms = sum(durations)
                            preferred_fps = max(1, min(60, round(1000 / min(durations))))
                            fps = preferred_fps
                            if duration_ms * fps > 120_000:
                                fps = max(1, min(fps, math.floor(120_000 / duration_ms)))
                            animation = {
                                "animated": True,
                                "source_frames": source_frames,
                                "frame_count": max(1, min(120, round(duration_ms * fps / 1000))),
                                "fps": fps,
                                "duration_ms": duration_ms,
                            }
            relative_name = path.relative_to(media_root).as_posix()
            return {
                "name": relative_name,
                "size": info.st_size,
                "revision": hashlib.sha256(
                    ":".join(str(value) for value in (
                        info.st_dev, info.st_ino, info.st_size,
                        info.st_mtime_ns, info.st_ctime_ns,
                    )).encode("ascii")
                ).hexdigest()[:32],
                "width": width,
                "height": height,
                "kind": path.suffix.lower().lstrip("."),
                "built_in": relative_name.startswith("built-in/"),
                **animation,
            }
        finally:
            os.close(fd)

    def media_items():
        items = []
        with media_lock:
            try:
                paths = [path for path in media_root.iterdir() if path.is_file() or path.is_symlink()]
                if built_in_media_root.is_dir() and not built_in_media_root.is_symlink():
                    paths.extend(path for path in built_in_media_root.iterdir() if path.is_file() or path.is_symlink())
                paths.sort(key=lambda item: item.relative_to(media_root).as_posix().lower())
            except FileNotFoundError:
                return items
            for path in paths:
                if path.suffix.lower() not in MEDIA_EXTENSIONS:
                    continue
                try:
                    items.append(media_item(path))
                except MEDIA_METADATA_ERRORS:
                    continue
        return items

    def sanitize_in_helper(source, filename):
        output_fd, output_name = tempfile.mkstemp(prefix=".sanitized-", dir=media_root)
        output = Path(output_name)
        source_fd = None
        try:
            source_fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW)
            subprocess.run(
                [sanitizer_python, str(sanitizer), str(source_fd), str(output_fd), filename],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                timeout=60,
                check=True,
                pass_fds=(source_fd, output_fd),
                **identity_options(),
            )
            os.chmod(output, 0o600)
            return output
        except (OSError, subprocess.SubprocessError) as error:
            detail = getattr(error, "stderr", b"") or b""
            if isinstance(detail, bytes):
                detail = detail.decode("utf-8", errors="replace")
            LOGGER.warning(
                "Media sanitizer failed (%s, returncode=%s): %s",
                type(error).__name__,
                getattr(error, "returncode", "n/a"),
                str(detail).strip()[:500] or "no helper detail",
                exc_info=True,
            )
            cleanup_failed = False
            try:
                output.unlink(missing_ok=True)
            except OSError:
                cleanup_failed = True
                LOGGER.exception("Media sanitizer output cleanup failed")
            raise MediaSanitizerError(output, cleanup_failed) from error
        finally:
            for descriptor, label in ((source_fd, "source"), (output_fd, "output")):
                if descriptor is None:
                    continue
                try:
                    os.close(descriptor)
                except OSError:
                    LOGGER.exception("Media sanitizer %s descriptor close failed", label)

    def safe_media_name(filename):
        if (
            not filename
            or filename != Path(filename).name
            or secure_filename(filename) != filename
            or Path(filename).suffix.lower() not in MEDIA_EXTENSIONS
        ):
            return None
        return filename

    def safe_media_reference(filename):
        if not isinstance(filename, str):
            return None
        parts = Path(filename).parts
        if len(parts) == 1:
            return safe_media_name(filename)
        if len(parts) == 2 and parts[0] == "built-in" and safe_media_name(parts[1]):
            return f"built-in/{parts[1]}"
        return None

    def media_path(filename):
        reference = safe_media_reference(filename)
        if reference is None:
            return None
        candidate = media_root / reference
        try:
            if not candidate.resolve(strict=False).is_relative_to(media_root.resolve()):
                return None
        except OSError:
            return None
        if reference.startswith("built-in/") and built_in_media_root.is_symlink():
            return None
        return candidate

    def ensure_media_file(filename):
        safe_name = safe_media_reference(filename)
        path = media_path(safe_name)
        if safe_name is None or path is None:
            raise ValueError("media filename is invalid")
        fd = None
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            info = os.fstat(fd)
        except OSError as error:
            raise ValueError(f"media file does not exist: {safe_name}") from error
        finally:
            if fd is not None:
                os.close(fd)
        if not stat.S_ISREG(info.st_mode):
            raise ValueError(f"media file is not regular: {safe_name}")

    def canvas_media_references(modules):
        references = set()
        for module in modules if isinstance(modules, list) else []:
            if not isinstance(module, dict) or module.get("type") != "canvas":
                continue
            layers = module.get("layers", [])
            overrides = module.get("overrides", [])
            media_layer_ids = set()
            for layer in layers if isinstance(layers, list) else []:
                if not isinstance(layer, dict) or layer.get("type") != "media":
                    continue
                media_layer_ids.add(layer.get("id"))
                properties = layer.get("properties", {})
                if isinstance(properties, dict) and "source" in properties:
                    references.add(properties["source"])
                keyframes = layer.get("keyframes", [])
                for keyframe in keyframes if isinstance(keyframes, list) else []:
                    keyframe_properties = keyframe.get("properties", {}) if isinstance(keyframe, dict) else {}
                    if isinstance(keyframe_properties, dict) and "source" in keyframe_properties:
                        references.add(keyframe_properties["source"])
            for override in overrides if isinstance(overrides, list) else []:
                if not isinstance(override, dict) or override.get("layer_id") not in media_layer_ids:
                    continue
                properties = override.get("properties", {})
                if isinstance(properties, dict) and "source" in properties:
                    references.add(properties["source"])
        return references

    def requires_pairing(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            if not pairing_session_is_current():
                return pairing_expired_response()
            return view(*args, **kwargs)

        return wrapped

    @app.errorhandler(RequestEntityTooLarge)
    def upload_too_large(_error):
        if request.path.startswith("/api/"):
            return jsonify({"error": "That file exceeds the 128 MB upload limit."}), 413
        return "That file exceeds the 128 MB upload limit.", 413

    @app.errorhandler(HTTPException)
    def http_error(error):
        if request.path.startswith("/api/"):
            return jsonify({"error": error.description}), error.code
        return error

    @app.errorhandler(Exception)
    def unexpected_error(error):
        if request.path.startswith("/api/"):
            LOGGER.exception("Unhandled API error")
            return jsonify({"error": "Internal server error."}), 500
        LOGGER.exception("Unhandled request error")
        return "Internal server error.", 500

    def begin_additional_pairing():
        max_devices = config_store.get()["pairing"]["max_devices"]
        if len(device_store.list()) >= max_devices:
            return False
        pin = pairing.issue_pin()
        controller.show_pairing_pin(pin, ttl_seconds=pairing.remaining_seconds())
        return True

    @app.get("/pair")
    def pair():
        warning_code = request.args.get("warning")
        warning = (
            DEVICE_REMOVAL_DURABILITY_WARNING
            if warning_code == "device-removal-durability"
            else None
        )
        if session.get("paired") and device_store.contains(session.get("device_id")):
            return render_template("pair.html", paired=True, error=warning)
        pin = pairing.active_pin()
        testing_pin_available = pairing.has_testing_pin()
        if pin is None and device_store.list() and not testing_pin_available:
            pairing_gate = "Pairing another browser must be started from an already paired device."
            return render_template(
                "pair.html",
                error=f"{warning} {pairing_gate}" if warning else pairing_gate,
            ), 403
        if pin is None and not testing_pin_available:
            pin = pairing.issue_pin()
        if pin is not None:
            controller.show_pairing_pin(pin, ttl_seconds=pairing.remaining_seconds())
        return render_template("pair.html", error=warning)

    @app.post("/pair")
    def pair_submit():
        device_name = request.form.get("device_name", "").strip()
        if not 1 <= len(device_name) <= 40 or any(not character.isprintable() for character in device_name):
            return render_template("pair.html", error="Device name must be 1-40 characters."), 400
        durability_uncertain = False
        with device_store.transaction():
            max_devices = config_store.get()["pairing"]["max_devices"]
            if len(device_store.list()) >= max_devices:
                return render_template(
                    "pair.html",
                    error="The Matrix has reached its paired-device limit. Remove a device before pairing another.",
                ), 409
            if pairing.verify(request.form.get("pin", "").strip()):
                try:
                    device = device_store.add(device_name, max_devices=max_devices)
                except PairingDurabilityError as error:
                    if error.device is None:
                        raise
                    device = error.device
                    durability_uncertain = True
                except ValueError:
                    return render_template(
                        "pair.html",
                        error="The Matrix has reached its paired-device limit. Remove a device before pairing another.",
                    ), 409
            else:
                device = None
        if device is not None:
            session.clear()
            session["paired"] = True
            session["generation"] = session_generation
            session["device_id"] = device["id"]
            session.permanent = True
            controller.end_pairing()
            controller.start()
            if durability_uncertain:
                return render_template(
                    "pair.html",
                    paired=True,
                    error=(
                        "This browser is already paired, but storage durability could not be "
                        "confirmed. You can continue to Matrix settings; verify the device "
                        "remains listed after the next restart."
                    ),
                ), 503
            return redirect(url_for("dashboard"))
        error = (
            "Too many attempts. Wait a minute and try again."
            if pairing.is_locked()
            else "That PIN is invalid or expired."
        )
        return render_template("pair.html", error=error), 401

    @app.post("/pair/new")
    @requires_pairing
    def pair_new():
        if not begin_additional_pairing():
            return render_template(
                "pair.html", paired=True,
                error="The paired-device limit has been reached. Remove a device or raise the limit first.",
            ), 409
        return render_template("pair.html", paired=True, pairing_ready=True), 202

    @app.post("/api/devices/pair")
    @requires_pairing
    def pair_new_api():
        if not begin_additional_pairing():
            return jsonify({"error": "The paired-device limit has been reached."}), 409
        return jsonify({"pairing": "ready"}), 202

    @app.get("/api/devices")
    @requires_pairing
    def paired_devices():
        current_id = session["device_id"]
        devices = [
            {**device, "current": device["id"] == current_id}
            for device in device_store.list()
        ]
        return jsonify({
            "devices": devices,
            "max_devices": config_store.get()["pairing"]["max_devices"],
        })

    @app.delete("/api/devices/<device_id>")
    @requires_pairing
    def remove_paired_device(device_id):
        current = device_id == session["device_id"]
        try:
            if not device_store.remove(device_id):
                abort(404)
        except PairingDurabilityError:
            controller.cancel_device_drafts_for_device(device_id)
            if current:
                session.clear()
            return jsonify({
                "removed": device_id,
                "current": current,
                "committed": True,
                "durability_uncertain": True,
                "error": DEVICE_REMOVAL_DURABILITY_WARNING,
            }), 503
        controller.cancel_device_drafts_for_device(device_id)
        if current:
            session.clear()
        return jsonify({"removed": device_id, "current": current})

    @app.post("/logout")
    def logout():
        device_id = session.get("device_id")
        try:
            if device_id:
                device_store.remove(device_id)
        except PairingDurabilityError:
            if device_id:
                controller.cancel_device_drafts_for_device(device_id)
            session.clear()
            return redirect(url_for("pair", warning="device-removal-durability"))
        except OSError:
            LOGGER.exception("Paired-device removal failed before logout committed")
            return render_template(
                "pair.html", paired=True,
                error="This device could not be removed, so this browser remains signed in.",
            ), 503
        if device_id:
            controller.cancel_device_drafts_for_device(device_id)
        session.clear()
        return redirect(url_for("pair"))

    @app.get("/")
    @requires_pairing
    def dashboard():
        config = config_store.get()
        current_device_id = session["device_id"]
        paired_devices = [
            {**device, "current": device["id"] == current_device_id}
            for device in device_store.list()
        ]
        custom_names = [item["id"] for item in config["custom_modules"]]
        lava_palettes = [
            {
                "name": name,
                "colours": [COLOUR_HEX[colour] for colour in colours],
            }
            for name, colours in LAVA_PALETTES.items()
        ]
        try:
            openweather_secret_configured = openweather_secrets.configured()
            openweather_secret_unavailable = False
        except (OSError, RuntimeError) as exc:
            LOGGER.error(
                "OpenWeather secret storage unavailable (%s)", type(exc).__name__
            )
            openweather_secret_configured = False
            openweather_secret_unavailable = True
        response = app.make_response(render_template(
            "dashboard.html",
            config=config,
            modules=sorted(ALLOWED_MODULES) + custom_names,
            module_labels={
                "announcement_board": "Announcement Board",
                "weather": "Weather (Free · Open-Meteo)",
                "openweather_overview": "OpenWeather Overview",
                "openweather_forecast": "OpenWeather Forecast",
                "clock_lava": "Lava Clock",
                "splash": "Splash",
                "sleep": "Sleep",
            },
            lava_palettes=lava_palettes,
            status=controller.status(),
            paired_devices=paired_devices,
            media_files=media_items(),
            openweather_secret_configured=openweather_secret_configured,
            openweather_secret_unavailable=openweather_secret_unavailable,
            built_in_defaults={
                "weather": DEFAULT_CONFIG["weather"],
                "openweather_overview": DEFAULT_CONFIG["openweather_overview"],
                "openweather_forecast": DEFAULT_CONFIG["openweather_forecast"],
                "clock_lava": DEFAULT_CONFIG["lava"],
                "splash": DEFAULT_CONFIG["media_layout"]["splash"],
                "sleep": DEFAULT_CONFIG["media_layout"]["sleep"],
            },
            installed_fonts=available_font_names(),
            font_advances=font_advance_catalog(),
            scroll_timing_javascript=timing_javascript(),
            application_version=APPLICATION_VERSION,
        ))
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/api/media")
    @requires_pairing
    def list_media():
        return jsonify(media_items())

    @app.post("/api/scroll-timing/<module_name>")
    @requires_pairing
    def provider_scroll_timing(module_name):
        deadline = time.monotonic() + 5
        if module_name not in {"announcement_board", "weather", "openweather_overview", "openweather_forecast"}:
            abort(404)
        # One extra byte lets an unknown-length WSGI stream prove overflow;
        # known lengths are rejected by the explicit check below.
        request.max_content_length = 16 * 1024 + 1
        if request.content_length is not None and request.content_length > 16 * 1024:
            return jsonify({"error": "Timing draft exceeds the 16 KiB limit."}), 413
        body = request.get_data(cache=True)
        if len(body) > 16 * 1024:
            return jsonify({"error": "Timing draft exceeds the 16 KiB limit."}), 413
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict) or set(payload) - {"base_revision", "settings", "weather", "settings_correlation"}:
            return jsonify({"error": "Timing draft is invalid."}), 400
        correlation = payload.get("settings_correlation")
        if correlation is not None and (
            not isinstance(correlation, str)
            or re.fullmatch(r"[0-9a-f]{64}", correlation) is None
        ):
            return jsonify({"error": "Timing draft is invalid."}), 400
        current = config_store.get()
        base_revision = payload.get("base_revision")
        if isinstance(base_revision, bool) or not isinstance(base_revision, int) or base_revision != current["revision"]:
            return jsonify({"error": "Timing draft is based on an older configuration.", "revision": current["revision"]}), 412
        settings = payload.get("settings")
        if not isinstance(settings, dict):
            return jsonify({"error": "Timing draft is invalid."}), 400
        draft = copy.deepcopy(current)
        draft[module_name] = copy.deepcopy(settings)
        if module_name == "announcement_board":
            try:
                validate_config(draft, allow_legacy_announcements=False, enforce_save_bounds=True)
            except (KeyError, TypeError, ValueError):
                return jsonify({"error": "Timing draft is invalid."}), 400
            plan = announcement_board.announcement_timing_plan(
                draft["announcement_board"]["announcements"], now=dt.datetime.now(),
            )
            return jsonify({**plan, "refresh_at": plan["refresh_at"].isoformat()})
        if module_name != "weather" and "weather" in payload:
            if not isinstance(payload["weather"], dict):
                return jsonify({"error": "Timing draft is invalid."}), 400
            draft["weather"] = copy.deepcopy(payload["weather"])
        try:
            validate_config(draft, allow_legacy_announcements=False, enforce_save_bounds=True)
        except (KeyError, TypeError, ValueError):
            return jsonify({"error": "Timing draft is invalid."}), 400
        digest_source = {"module": module_name, "settings": draft[module_name]}
        if module_name != "weather":
            digest_source["weather"] = draft["weather"]
        digest = hashlib.sha256(json.dumps(
            digest_source, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")).hexdigest()
        provider = "open_meteo" if module_name == "weather" else "openweather"
        generation = 0 if provider == "open_meteo" else current_openweather_credential_generation()
        key = (provider, module_name, digest, generation)

        def fill():
            if time.monotonic() >= deadline:
                raise TimeoutError("provider timing deadline exceeded")
            if module_name == "weather":
                plan = weather_app.prepare_ticker_plan(draft, deadline=deadline)
            else:
                api_key = openweather_secrets.get()
                builder = openweather_overview.prepare_ticker_plan if module_name == "openweather_overview" else openweather_forecast.prepare_ticker_plan
                plan = builder(draft, api_key=api_key, deadline=deadline)
            if time.monotonic() >= deadline:
                raise TimeoutError("provider timing deadline exceeded")
            return plan
        try:
            plan = scroll_timing_cache.get_or_fill(key, fill, deadline=deadline)
        except TimingCacheBusy:
            response = jsonify({"error": "Timing preparation is busy.", "retryable": True})
            response.status_code = 429
            response.headers["Retry-After"] = "1"
            return response
        except Exception as exc:
            LOGGER.info("Provider timing unavailable (%s)", type(exc).__name__)
            return jsonify({"module": module_name, "settings_digest": digest,
                            **({"settings_correlation": correlation} if correlation is not None else {}),
                            "status": "unavailable", "display": "Unavailable"}), 503
        return jsonify({"module": module_name, "settings_digest": digest,
                        **({"settings_correlation": correlation} if correlation is not None else {}),
                        **plan})

    @app.get("/api/media-timeline/<path:filename>")
    @requires_pairing
    def media_timeline(filename):
        frame_count = request.args.get("frame_count", type=int)
        fps = request.args.get("fps", type=float)
        if (
            frame_count is None or fps is None or not 1 <= frame_count <= 120
            or not math.isfinite(fps) or not 1 <= fps <= 60
        ):
            return jsonify({"error": "Media timeline bounds are out of range."}), 400
        reference = safe_media_reference(filename)
        resolved = media_path(reference) if reference is not None else None
        if resolved is None:
            abort(404)
        try:
            if not stat.S_ISREG(os.lstat(resolved).st_mode):
                abort(404)
        except OSError:
            abort(404)
        if not media_timeline_slots.acquire(blocking=False):
            response = jsonify({"error": "Media timeline service is busy."})
            response.status_code = 429
            response.headers["Retry-After"] = "1"
            return response
        try:
            frames = controller.matrix.media_frames(
                reference, frame_count, fps,
                preserve_size=True, decode_timeout=5,
                resolved_path=resolved,
            )
        except (OSError, ValueError, subprocess.SubprocessError):
            LOGGER.exception("Canvas media timeline decode failed")
            return jsonify({"error": "Media timeline is unavailable."}), 422
        finally:
            media_timeline_slots.release()
        if len(frames) != frame_count:
            return jsonify({"error": "Media timeline is unavailable."}), 422
        width, height = frames[0].size
        if any(frame.size != (width, height) for frame in frames):
            return jsonify({"error": "Media timeline is unavailable."}), 422
        sprite = Image.new("RGB", (width, height * frame_count))
        for index, frame in enumerate(frames):
            sprite.paste(frame.convert("RGB"), (0, index * height))
        output = io.BytesIO()
        sprite.save(output, format="PNG")
        output.seek(0)
        response = send_file(output, mimetype="image/png", max_age=0)
        response.headers["Cache-Control"] = "private, no-cache"
        response.headers["X-Frame-Count"] = str(frame_count)
        response.headers["X-Frame-Width"] = str(width)
        response.headers["X-Frame-Height"] = str(height)
        return response

    @app.post("/api/media")
    @requires_pairing
    def upload_media():
        upload = request.files.get("file")
        filename = safe_media_name(upload.filename if upload else "")
        if upload is None or filename is None:
            return jsonify({"error": "Choose a GIF, PNG, JPEG, WebP, or MP4 file with a safe filename."}), 400
        destination = media_root / filename
        temporary = sanitized = None
        cleanup_failed = False
        response_body = None
        response_status = None

        def rollback_destination():
            try:
                destination.unlink(missing_ok=True)
            except OSError:
                LOGGER.exception("Published media rollback failed")
                return False, False
            try:
                sync_media_directory()
            except OSError:
                LOGGER.exception("Published media rollback directory sync failed")
                return True, True
            return True, False

        with media_lock:
            if destination.exists() or destination.is_symlink():
                return jsonify({"error": "A media file with that name already exists."}), 409
            fd, temporary_name = tempfile.mkstemp(prefix=".upload-", dir=media_root)
            os.close(fd)
            temporary = Path(temporary_name)
            try:
                upload.save(temporary)
                os.chmod(temporary, 0o644)
                sanitized = sanitize_in_helper(temporary, filename)
                try:
                    os.link(sanitized, destination, follow_symlinks=False)
                except FileExistsError:
                    response_body = {"error": "A media file with that name already exists."}
                    response_status = 409
                else:
                    publication_durability_uncertain = False
                    try:
                        sync_media_directory()
                    except OSError:
                        publication_durability_uncertain = True
                        LOGGER.exception("Published media directory sync failed")
                    try:
                        item = media_item(destination)
                    except MEDIA_METADATA_ERRORS:
                        LOGGER.exception("Published media failed metadata validation")
                        removed, rollback_durability_uncertain = rollback_destination()
                        if not removed:
                            response_body = {
                                "error": "The upload failed, but the published file could not be removed.",
                                "committed": True,
                                "filename": filename,
                            }
                            response_status = 500
                        elif rollback_durability_uncertain:
                            response_body = {
                                "error": "The invalid upload was removed, but durable storage could not be confirmed.",
                                "committed": False,
                                "durability_uncertain": True,
                            }
                            response_status = 503
                        else:
                            response_body = {
                                "error": "The uploaded file is not valid or exceeds the media safety limits."
                            }
                            response_status = 400
                    except Exception:
                        removed, rollback_durability_uncertain = rollback_destination()
                        if not removed:
                            response_body = {
                                "error": "The upload failed, but the published file could not be removed.",
                                "committed": True,
                                "filename": filename,
                            }
                            response_status = 500
                        elif rollback_durability_uncertain:
                            response_body = {
                                "error": "The upload failed and its removal could not be confirmed durable.",
                                "committed": False,
                                "durability_uncertain": True,
                            }
                            response_status = 503
                        else:
                            raise
                    else:
                        if publication_durability_uncertain:
                            response_body = {
                                "committed": True,
                                "durability_uncertain": True,
                                "item": item,
                                "error": "The media was saved, but durable storage could not be confirmed.",
                            }
                            response_status = 503
                        else:
                            response_body = item
                            response_status = 201
            except MediaSanitizerError as error:
                sanitized = error.output
                cleanup_failed = cleanup_failed or error.cleanup_failed
                response_body = {"error": "The uploaded file is not valid or exceeds the media safety limits."}
                response_status = 400
            except (OSError, ValueError):
                response_body = {"error": "The uploaded file is not valid or exceeds the media safety limits."}
                response_status = 400
            finally:
                for cleanup_path in (temporary, sanitized):
                    if cleanup_path is None:
                        continue
                    try:
                        cleanup_path.unlink(missing_ok=True)
                    except OSError:
                        cleanup_failed = True
                        LOGGER.exception("Media upload temporary cleanup failed")
        if cleanup_failed:
            committed = bool(response_body and response_body.get("committed", response_status == 201))
            if committed:
                if response_status == 201:
                    response_body = {
                        "committed": True,
                        "item": response_body,
                        "cleanup_failed": True,
                        "error": "The media was saved, but temporary cleanup could not be completed.",
                    }
                    response_status = 503
                else:
                    response_body = {
                        **response_body,
                        "cleanup_failed": True,
                        "error": response_body["error"] + " Temporary cleanup also failed.",
                    }
            else:
                response_body = {
                    **(response_body or {}),
                    "committed": False,
                    "cleanup_failed": True,
                    "error": "The upload was rejected, but temporary cleanup could not be completed.",
                }
                response_status = 500
        return jsonify(response_body), response_status

    @app.get("/media/<path:filename>")
    @requires_pairing
    def media_file(filename):
        filename = safe_media_reference(filename)
        path = media_path(filename)
        if filename is None or path is None:
            abort(404)
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                os.close(fd)
                abort(404)
        except (FileNotFoundError, OSError):
            abort(404)
        handle = os.fdopen(fd, "rb")
        try:
            response = send_file(handle, download_name=filename, conditional=False, max_age=0)
        except Exception:
            handle.close()
            raise
        response.call_on_close(handle.close)
        return response

    @app.post("/api/media/<path:filename>/preview")
    @requires_pairing
    def preview_media(filename):
        filename = safe_media_reference(filename)
        if filename is None:
            abort(404)
        with media_lock:
            try:
                ensure_media_file(filename)
            except ValueError:
                abort(404)
        controller.preview_media(filename)
        return jsonify({"previewing": filename}), 202

    @app.delete("/api/media/<path:filename>")
    @requires_pairing
    def remove_media(filename):
        filename = safe_media_reference(filename)
        if filename is None:
            abort(404)
        if filename.startswith("built-in/"):
            return jsonify({"error": "Built-in media cannot be removed."}), 403
        with media_lock:
            config = config_store.get()
            active_slots = [key for key, value in config["media"].items() if value == filename]
            if active_slots:
                return jsonify({"error": "Assign another file to " + " and ".join(active_slots) + " before removing this one."}), 409
            if filename in canvas_media_references(config["custom_modules"]):
                return jsonify({"error": "Remove this file from custom canvas modules before deleting it."}), 409
            path = media_path(filename)
            if path is None:
                abort(404)
            try:
                info = path.lstat()
            except FileNotFoundError:
                abort(404)
            if not stat.S_ISREG(info.st_mode) or path.is_symlink():
                abort(404)
            path.unlink()
            try:
                sync_media_directory()
            except OSError:
                LOGGER.exception("Removed media directory sync failed")
                return jsonify({
                    "removed": filename,
                    "committed": True,
                    "durability_uncertain": True,
                    "error": "The media was removed, but durable storage could not be confirmed.",
                }), 503
        return jsonify({"removed": filename})

    @app.get("/api/config")
    @requires_pairing
    def get_config():
        config = config_store.get()
        try:
            validators = _parse_revision_validators(request.headers.get("If-None-Match"))
        except OverflowError as exc:
            return jsonify({"error": str(exc)}), 431
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        if validators == "*" or (
            validators is not None
            and any(opaque == str(config["revision"]) for _weak, opaque in validators)
        ):
            response = app.response_class(status=304)
        else:
            response = jsonify(config)
        response.headers["ETag"] = _revision_etag(config["revision"])
        return response

    @app.post("/api/config")
    @requires_pairing
    def update_config():
        durability_uncertain = False
        try:
            request.max_content_length = CONFIG_REQUEST_LIMIT + 1
            if request.content_length is not None and request.content_length > CONFIG_REQUEST_LIMIT:
                return jsonify({"error": "Configuration request must not exceed 1 MiB."}), 413
            if len(request.get_data(cache=True)) > CONFIG_REQUEST_LIMIT:
                return jsonify({"error": "Configuration request must not exceed 1 MiB."}), 413
            validators = _parse_revision_validators(request.headers.get("If-Match"))
            current_revision = config_store.get()["revision"]
            if validators is None:
                response = jsonify({
                    "error": "A configuration base revision is required.",
                    "revision": current_revision,
                })
                response.status_code = 428
                response.headers["ETag"] = _revision_etag(current_revision)
                return response
            elif validators == "*":
                base_revision = None
            else:
                if not any(
                    not weak and opaque == str(current_revision)
                    for weak, opaque in validators
                ):
                    response = jsonify({
                        "error": "Configuration has changed since this client loaded it.",
                        "revision": current_revision,
                    })
                    response.status_code = 412
                    response.headers["ETag"] = _revision_etag(current_revision)
                    return response
                base_revision = current_revision
            if not request.is_json:
                abort(415, description="Content-Type must be application/json.")
            changes = request.get_json()
            with device_store.transaction():
                requested_max = (
                    changes.get("pairing", {}).get("max_devices")
                    if isinstance(changes, dict) and isinstance(changes.get("pairing", {}), dict)
                    else None
                )
                paired_count = len(device_store.list())
                if (
                    isinstance(requested_max, int) and not isinstance(requested_max, bool)
                    and requested_max < paired_count
                ):
                    raise ValueError(
                        f"maximum devices cannot be below the {paired_count} currently paired devices"
                    )
                with media_lock:
                    if isinstance(changes, dict) and "media" in changes:
                        media_changes = changes["media"]
                        if not isinstance(media_changes, dict):
                            raise ValueError("media must be an object")
                        for filename in media_changes.values():
                            if safe_media_reference(filename) is None:
                                raise ValueError("media filename is invalid")
                    candidate = config_store.validate_candidate(changes)
                    try:
                        with pin_candidate_media(
                            candidate, media_root, allow_missing=default_built_in_media,
                        ):
                            config = config_store.update(changes, base_revision=base_revision)
                    except MediaResolutionError as error:
                        reference = error.reference
                        if error.code == "not_regular":
                            raise ValueError(
                                f"media file is not regular: {reference}"
                            ) from None
                        if error.code == "not_found":
                            raise ValueError(
                                f"media file does not exist: {reference}"
                            ) from None
                        raise ValueError("media filename is invalid") from None
        except ConfigDurabilityError as exc:
            config = exc.config
            durability_uncertain = True
            LOGGER.exception("Configuration committed but directory sync failed")
        except ConfigConflictError as exc:
            response = jsonify({
                "error": "Configuration has changed since this client loaded it.",
                "revision": exc.current_revision,
            })
            response.status_code = 412
            response.headers["ETag"] = _revision_etag(exc.current_revision)
            return response
        except OverflowError as exc:
            return jsonify({"error": str(exc)}), 431
        except (TypeError, ValueError, KeyError) as exc:
            return jsonify({"error": str(exc)}), 400
        try:
            controller.reload()
        except Exception:
            LOGGER.exception("Configuration committed but display reload failed")
            response = jsonify({
                "committed": True,
                "config": config,
                "durability_uncertain": durability_uncertain,
                "error": (
                    "Configuration was saved without confirmed durability, and the display "
                    "could not be reloaded."
                    if durability_uncertain else
                    "Configuration was saved, but the display could not be reloaded."
                ),
            })
            response.status_code = 503
            response.headers["ETag"] = _revision_etag(config["revision"])
            return response
        if durability_uncertain:
            response = jsonify({
                "committed": True,
                "config": config,
                "durability_uncertain": True,
                "error": "Configuration was saved, but durable storage could not be confirmed.",
            })
            response.status_code = 503
            response.headers["ETag"] = _revision_etag(config["revision"])
            return response
        response = jsonify(config)
        response.headers["ETag"] = _revision_etag(config["revision"])
        return response

    @app.get("/api/display-frame")
    @requires_pairing
    def display_frame():
        frame, revision = controller.frame_png()
        response = send_file(frame, mimetype="image/png", conditional=False, max_age=0)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Matrix-Frame"] = str(revision)
        return response

    @app.get("/api/status")
    @requires_pairing
    def status():
        current = controller.status()
        payload = dict(current) if isinstance(current, dict) else {}
        payload["minimum_client_contract"] = MINIMUM_CLIENT_CONTRACT
        return jsonify(payload)

    @app.get("/api/locations")
    @requires_pairing
    def search_locations():
        query = request.args.get("q", "").strip()
        if not 2 <= len(query) <= 100:
            return jsonify({"error": "Location search must be 2-100 characters."}), 400
        try:
            return jsonify(location_search(query, count=6))
        except Exception:
            return jsonify({"error": "Location search is temporarily unavailable."}), 502

    @app.get("/api/secrets/openweather")
    @requires_pairing
    def openweather_secret_status():
        try:
            return jsonify(openweather_secrets.status())
        except (OSError, RuntimeError) as exc:
            LOGGER.error(
                "OpenWeather secret status unavailable (%s)", type(exc).__name__
            )
            return jsonify({"error": "Secret storage is unavailable."}), 503

    @app.put("/api/secrets/openweather")
    @requires_pairing
    def set_openweather_secret():
        payload = request.get_json(silent=True)
        try:
            openweather_secrets.set(payload.get("api_key") if isinstance(payload, dict) else None)
            rotate_openweather_credentials()
            return jsonify(openweather_secrets.status())
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        except SecretDurabilityError as exc:
            return jsonify({
                "configured": exc.configured,
                "error": str(exc),
            }), 503
        except (OSError, RuntimeError) as exc:
            LOGGER.error(
                "OpenWeather secret update unavailable (%s)", type(exc).__name__
            )
            return jsonify({"error": "Secret storage is unavailable."}), 503

    @app.delete("/api/secrets/openweather")
    @requires_pairing
    def clear_openweather_secret():
        try:
            openweather_secrets.clear()
            rotate_openweather_credentials()
            return jsonify(openweather_secrets.status())
        except SecretDurabilityError as exc:
            return jsonify({
                "configured": exc.configured,
                "error": str(exc),
            }), 503
        except (OSError, RuntimeError) as exc:
            LOGGER.error(
                "OpenWeather secret removal unavailable (%s)", type(exc).__name__
            )
            return jsonify({"error": "Secret storage is unavailable."}), 503

    def edit_preview_error(message, status, *, retry_after=None):
        response = jsonify({"error": message})
        response.status_code = status
        if retry_after is not None:
            response.headers["Retry-After"] = str(retry_after)
        return response

    def edit_preview_json(required_keys, optional_keys=()):
        if request.content_length is not None and request.content_length > CONFIG_REQUEST_LIMIT:
            raise RequestEntityTooLarge()
        if not request.is_json:
            raise ValueError("request must be JSON")
        body = request.stream.read(CONFIG_REQUEST_LIMIT + 1)
        if len(body) > CONFIG_REQUEST_LIMIT:
            raise RequestEntityTooLarge()
        try:
            payload = json.loads(body)
        except (TypeError, ValueError):
            raise ValueError("request payload has invalid JSON") from None
        keys = set(payload) if isinstance(payload, dict) else set()
        required = set(required_keys)
        optional = set(optional_keys)
        if (
            not isinstance(payload, dict)
            or not required <= keys
            or keys - required - optional
        ):
            raise ValueError("request payload has invalid fields")
        return payload

    def edit_preview_identity(payload):
        value = payload.get("preview_instance")
        if not isinstance(value, str) or EDIT_PREVIEW_INSTANCE.fullmatch(value) is None:
            raise ValueError("preview_instance is invalid")
        return (session["device_id"], value)

    @app.post("/api/edit-preview/<module_name>")
    @requires_pairing
    def start_edit_preview(module_name):
        stack = ExitStack()
        instance = None
        try:
            required = {"config", "generation", "preview_instance"}
            payload = edit_preview_json(required, {"selected_lava_palette"})
            generation = payload["generation"]
            if (
                isinstance(generation, bool) or not isinstance(generation, int)
                or generation < 0
            ):
                raise ValueError("generation is invalid")
            identity = edit_preview_identity(payload)
            selected_palette = payload.get("selected_lava_palette")
            if selected_palette is not None and not isinstance(selected_palette, dict):
                raise ValueError("selected_lava_palette is invalid")
            candidate = config_store.validate_candidate(payload["config"])
            custom_ids = {item["id"] for item in candidate["custom_modules"]}
            if module_name not in ALLOWED_MODULES and module_name not in custom_ids:
                stack.close()
                return edit_preview_error("Preview module was not found.", 404)
            stop_event = threading.Event()

            def iterator_factory():
                with media_lock:
                    pinned_media = stack.enter_context(pin_candidate_media(
                        candidate, media_root, allow_missing=default_built_in_media,
                    ))
                provider_candidate = candidate
                if module_name in {"openweather_overview", "openweather_forecast"}:
                    provider_candidate = copy.deepcopy(candidate)
                    provider_candidate["_preview_openweather_key"] = openweather_secrets.get()
                prepared = _prepare_edit_preview_provider(
                    provider, module_name, provider_candidate, stop_event,
                )

                def owned_iterator():
                    try:
                        try:
                            yield from _edit_preview_iterator(
                                module_name, candidate, selected_palette, pinned_media,
                                prepared, stop_event,
                            )
                        except Exception:
                            raise EditPreviewRenderError(
                                "preview rendering is unavailable"
                            ) from None
                    finally:
                        stack.close()
                return owned_iterator()

            registry, provider = get_edit_preview_services()
            instance = registry.register(
                identity, iterator_factory, stop_event=stop_event,
                generation=generation,
            )
            preview_session = instance.admit()
            page = preview_session.page(raise_on_superseded=True)
            return jsonify({
                "session": instance.instance_id,
                "generation": generation,
                **page,
            })
        except RequestEntityTooLarge:
            stack.close()
            return edit_preview_error("Preview request exceeds 1 MiB.", 413)
        except PreviewSessionExpiredError:
            stack.close()
            return edit_preview_error("Preview session expired.", 410)
        except PreviewPageSupersededError:
            stack.close()
            return edit_preview_error("Preview generation was superseded.", 409)
        except (
            PreviewInstanceCapacityError, PreviewCapacityError,
            PreviewProviderCapacityError,
        ):
            stack.close()
            if instance is not None:
                instance.close()
            return edit_preview_error(
                "Preview capacity is temporarily exhausted.", 429, retry_after=2,
            )
        except (
            PreviewProviderCancelled, PreviewProviderTimeout,
            PreviewProviderUnavailable, EditPreviewDecodeError,
            EditPreviewRenderError,
        ):
            stack.close()
            if instance is not None:
                instance.close()
            return edit_preview_error("Preview is temporarily unavailable.", 503)
        except RuntimeError:
            stack.close()
            if instance is not None:
                instance.close()
            return edit_preview_error("Preview is temporarily unavailable.", 503)
        except (KeyError, TypeError, ValueError):
            stack.close()
            if instance is not None:
                instance.close()
            return edit_preview_error("Preview request is invalid.", 400)
        except Exception:
            stack.close()
            if instance is not None:
                instance.close()
            return edit_preview_error("Preview is temporarily unavailable.", 503)

    @app.post("/api/edit-preview/page")
    @requires_pairing
    def page_edit_preview():
        try:
            payload = edit_preview_json({"session", "preview_instance", "token"})
            identity = edit_preview_identity(payload)
            session_id = payload["session"]
            token = payload["token"]
            if (
                not isinstance(session_id, str)
                or EDIT_PREVIEW_SESSION.fullmatch(session_id) is None
                or not isinstance(token, str)
                or EDIT_PREVIEW_TOKEN.fullmatch(token) is None
            ):
                raise ValueError("preview continuation is invalid")
            registry, _provider = get_edit_preview_services()
            instance = registry.lookup(session_id, identity)
            page = instance.admit().page(token, raise_on_superseded=True)
            if registry.is_closed:
                return edit_preview_error("Preview is temporarily unavailable.", 503)
            return jsonify({
                "session": session_id, "generation": instance.generation, **page,
            })
        except RequestEntityTooLarge:
            return edit_preview_error("Preview request exceeds 1 MiB.", 413)
        except PreviewSessionExpiredError:
            return edit_preview_error("Preview session expired.", 410)
        except PreviewPageSupersededError:
            return edit_preview_error("Preview generation was superseded.", 409)
        except PreviewTokenConsumedError:
            return edit_preview_error("Preview continuation was already used.", 409)
        except (PreviewSessionConflictError, StopIteration):
            return edit_preview_error("Preview session is no longer active.", 409)
        except (KeyError, TypeError, ValueError):
            return edit_preview_error("Preview request is invalid.", 400)
        except Exception:
            return edit_preview_error("Preview is temporarily unavailable.", 503)

    @app.post("/api/edit-preview/cancel")
    @requires_pairing
    def cancel_edit_preview():
        try:
            payload = edit_preview_json({"session", "preview_instance"})
            identity = edit_preview_identity(payload)
            session_id = payload["session"]
            if (
                not isinstance(session_id, str)
                or EDIT_PREVIEW_SESSION.fullmatch(session_id) is None
            ):
                raise ValueError("preview session is invalid")
            registry, _provider = get_edit_preview_services()
            instance = registry.lookup(session_id, identity)
            instance.close()
            return jsonify({"cancelled": True})
        except RequestEntityTooLarge:
            return edit_preview_error("Preview request exceeds 1 MiB.", 413)
        except PreviewSessionExpiredError:
            return edit_preview_error("Preview session expired.", 410)
        except PreviewSessionConflictError:
            return edit_preview_error("Preview session is no longer active.", 409)
        except (KeyError, TypeError, ValueError):
            return edit_preview_error("Preview request is invalid.", 400)
        except Exception:
            return edit_preview_error("Preview is temporarily unavailable.", 503)

    def require_acceptance_authorization():
        supplied = request.headers.get("X-Matrix-Acceptance-Authorization", "")
        if (acceptance_authorization is None
                or not secrets.compare_digest(supplied, acceptance_authorization)):
            abort(403)

    @app.post("/api/release-acceptance/transaction")
    @requires_pairing
    def stage_release_acceptance_transaction():
        require_acceptance_authorization()
        try:
            request.max_content_length = 8193
            raw = request.get_data(cache=True)
            if len(raw) > 8192 or not request.is_json:
                raise ValueError("acceptance request is invalid")
            payload = request.get_json()
            fields = {"attempt_id", "acceptance_operation_id", "evidence_id",
                      "expected_source_sha", "expected_version", "mode", "nonce",
                      "owner_revision", "generation", "preview_instance"}
            if type(payload) is not dict or set(payload) != fields:
                raise ValueError("acceptance request fields are invalid")
            generation = payload["generation"]
            operation_id = payload["acceptance_operation_id"]
            if (type(generation) is not int or generation < 0
                    or type(operation_id) is not str
                    or re.fullmatch(r"[0-9a-f]{64}", operation_id) is None):
                raise ValueError("acceptance identity is invalid")
            owner = DevicePreviewOwner(*edit_preview_identity(payload))
            transaction = {key: payload[key] for key in fields - {"generation", "preview_instance"}}
            publisher = AcceptanceArtifactTransaction(
                acceptance_runtime, transaction, owner_generation=generation,
            )
            try:
                controller.begin_acceptance_transaction(
                    publisher, owner, generation=generation,
                )
            except BaseException:
                publisher.close()
                raise
            return jsonify(publisher.status()), 202
        except RequestEntityTooLarge:
            return edit_preview_error("Acceptance request exceeds 8 KiB.", 413)
        except (KeyError, OSError, TypeError, ValueError, RuntimeError):
            return edit_preview_error("Acceptance transaction is unavailable.", 409)

    @app.post("/api/release-acceptance/transaction/status")
    @requires_pairing
    def release_acceptance_transaction_status():
        require_acceptance_authorization()
        try:
            payload = edit_preview_json(
                {"generation", "preview_instance", "nonce", "acceptance_operation_id"}
            )
            generation = payload["generation"]
            operation_id = payload["acceptance_operation_id"]
            if (type(generation) is not int or generation < 0
                    or type(payload["nonce"]) is not str
                    or re.fullmatch(r"[0-9a-f]{64}", payload["nonce"]) is None
                    or type(operation_id) is not str
                    or re.fullmatch(r"[0-9a-f]{64}", operation_id) is None):
                raise ValueError("acceptance status identity is invalid")
            owner = DevicePreviewOwner(*edit_preview_identity(payload))
            status = controller.acceptance_transaction_status(owner, generation=generation)
            if (status is None or status.get("nonce") != payload["nonce"]
                    or status.get("acceptance_operation_id") != operation_id):
                return edit_preview_error("Acceptance transaction was not found.", 404)
            return jsonify(status), (200 if status.get("state") == "SAVED_ROLLBACK_PUBLISHED" else 202)
        except (KeyError, TypeError, ValueError):
            return edit_preview_error("Acceptance status request is invalid.", 400)

    device_preview_operation = threading.BoundedSemaphore(1)

    @app.post("/api/device-preview/<module_name>")
    @requires_pairing
    def start_device_preview(module_name):
        if not device_preview_operation.acquire(blocking=False):
            return edit_preview_error(
                "Device preview capacity is temporarily exhausted.",
                429, retry_after=2,
            )
        stack = ExitStack()
        transferred = False
        try:
            payload = edit_preview_json(
                {"config", "generation", "preview_instance"},
                {"timeout_seconds", "selected_lava_palette"},
            )
            generation = payload["generation"]
            timeout_seconds = payload.get("timeout_seconds", 60)
            if (
                isinstance(generation, bool) or not isinstance(generation, int)
                or generation < 0
                or isinstance(timeout_seconds, bool)
                or not isinstance(timeout_seconds, int)
                or not 5 <= timeout_seconds <= 3600
            ):
                raise ValueError("invalid transient preview values")
            identity = edit_preview_identity(payload)
            candidate = config_store.validate_candidate(payload["config"])
            selected_palette = _validate_current_lava_selection(
                module_name, candidate, payload.get("selected_lava_palette"),
            )
            custom_ids = {item["id"] for item in candidate["custom_modules"]}
            if module_name not in ALLOWED_MODULES and module_name not in custom_ids:
                stack.close()
                return edit_preview_error("Device preview was not found.", 404)
            owner = DevicePreviewOwner(*identity)
            with media_lock:
                pinned_media = stack.enter_context(pin_candidate_media(
                    candidate, media_root, allow_missing=default_built_in_media,
                ))
            request_record = DevicePreviewRequest(
                owner=owner,
                generation=generation,
                config={
                    "module": module_name,
                    "candidate": candidate,
                    "selected_lava_palette": selected_palette,
                },
                timeout_seconds=timeout_seconds,
                cleanup=stack.close,
                pinned_media=pinned_media,
            )
            def render_device_draft(matrix, owned_request, *, stop_event):
                prepared = None
                if module_name in {
                    "weather", "openweather_overview", "openweather_forecast",
                }:
                    _registry, provider = get_edit_preview_services()
                    provider_candidate = copy.deepcopy(candidate)
                    if module_name in {
                        "openweather_overview", "openweather_forecast",
                    }:
                        provider_candidate["_preview_openweather_key"] = (
                            openweather_secrets.get()
                        )
                    prepared = _prepare_edit_preview_provider(
                        provider, module_name, provider_candidate, stop_event,
                    )
                    if stop_event.is_set():
                        return False
                return controller.render_device_draft(
                    matrix, owned_request,
                    stop_event=stop_event, prepared=prepared,
                )

            admission = controller.submit_device_draft(
                request_record, renderer=render_device_draft,
            )
            if admission.status is not PreviewAdmissionStatus.ACCEPTED:
                stack.close()
                status = 409
                return edit_preview_error("Device preview is not available.", status)
            transferred = True
            now = time.monotonic()
            lease = admission.lease
            return jsonify({
                "generation": generation,
                "active_module": module_name,
                "timeout_seconds": timeout_seconds,
                "heartbeat_after_seconds": HEARTBEAT_INTERVAL_SECONDS,
                "expires_in": max(0, lease.content_deadline - now),
                "connectivity_expires_in": max(0, lease.connectivity_deadline - now),
            }), 202
        except RequestEntityTooLarge:
            return edit_preview_error("Device preview request exceeds 1 MiB.", 413)
        except (KeyError, TypeError, ValueError):
            return edit_preview_error("Device preview request is invalid.", 400)
        except Exception:
            return edit_preview_error("Device preview is temporarily unavailable.", 503)
        finally:
            if not transferred:
                stack.close()
            device_preview_operation.release()

    def device_preview_control_payload():
        payload = edit_preview_json({"generation", "preview_instance"})
        generation = payload["generation"]
        if isinstance(generation, bool) or not isinstance(generation, int) or generation < 0:
            raise ValueError("generation is invalid")
        return DevicePreviewOwner(*edit_preview_identity(payload)), generation

    @app.post("/api/device-preview/heartbeat")
    @requires_pairing
    def heartbeat_device_preview():
        try:
            owner, generation = device_preview_control_payload()
            outcome = controller.heartbeat_device_draft(owner, generation=generation)
            if outcome.status is not PreviewHeartbeatStatus.ACCEPTED:
                controller.mark_acceptance_foreign_rejection(owner, generation=generation)
                return edit_preview_error("Device preview is no longer active.", 409)
            controller.mark_acceptance_result(
                owner, generation=generation, result="heartbeat_accepted",
            )
            now = time.monotonic()
            return jsonify({
                "active": True,
                "generation": generation,
                "expires_in": max(0, outcome.lease.content_deadline - now),
                "connectivity_expires_in": max(0, outcome.lease.connectivity_deadline - now),
            }), 202
        except RequestEntityTooLarge:
            return edit_preview_error("Device preview request exceeds 1 MiB.", 413)
        except (KeyError, TypeError, ValueError):
            return edit_preview_error("Device preview request is invalid.", 400)
        except Exception:
            return edit_preview_error("Device preview is temporarily unavailable.", 503)

    @app.post("/api/device-preview/cancel")
    @requires_pairing
    def cancel_device_preview():
        try:
            owner, generation = device_preview_control_payload()
            if not controller.cancel_device_draft(owner, generation=generation):
                return edit_preview_error("Device preview is no longer active.", 409)
            controller.mark_acceptance_result(
                owner, generation=generation, result="timeout_returned",
            )
            return jsonify({"cancelled": True}), 202
        except RequestEntityTooLarge:
            return edit_preview_error("Device preview request exceeds 1 MiB.", 413)
        except (KeyError, TypeError, ValueError):
            return edit_preview_error("Device preview request is invalid.", 400)
        except Exception:
            return edit_preview_error("Device preview is temporarily unavailable.", 503)

    @app.post("/api/preview/announcement")
    @requires_pairing
    def preview_announcement():
        candidate = json.loads(json.dumps(config_store.get()))
        candidate["announcement_board"] = {"announcements": [request.get_json()]}
        try:
            validated = validate_config(
                candidate,
                allow_legacy_announcements=False,
            )
        except ValueError as error:
            return jsonify({"error": str(error)}), 400
        announcement = validated["announcement_board"]["announcements"][0]
        controller.preview_announcement(announcement)
        return jsonify({"previewing": announcement["id"]}), 202

    @app.post("/api/preview/<module_name>")
    @requires_pairing
    def preview(module_name):
        custom_ids = {item["id"] for item in config_store.get()["custom_modules"]}
        if module_name not in ALLOWED_MODULES and module_name not in custom_ids:
            abort(404)
        controller.preview(module_name)
        return jsonify({"previewing": module_name}), 202

    @app.post("/api/control/<action>")
    @requires_pairing
    def control(action):
        if action == "start":
            controller.start()
        elif action == "stop":
            if controller.stop() is False:
                return jsonify({
                    **controller.status(),
                    "error": "Display did not stop within the timeout.",
                }), 503
        elif action == "reload":
            controller.reload()
        else:
            abort(404)
        return jsonify(controller.status())

    @app.post("/api/system/restart")
    @requires_pairing
    def restart_device():
        nonlocal device_restart_pending_until
        if request.headers.get("X-Matrix-Action") != "restart-whole-device":
            return jsonify({
                "error": "Restart request authorization failed."
            }), 403
        if device_restart_runner is None:
            return jsonify({
                "error": "Whole-device restart is not enabled on this installation."
            }), 503
        with device_restart_lock:
            requested_at = time.monotonic()
            locally_pending = device_restart_pending_until > requested_at
            try:
                outcome = device_restart_runner()
            except (OSError, RuntimeError):
                LOGGER.exception("Unable to publish fixed device restart request")
                return jsonify({"error": "Device restart could not be scheduled."}), 503
            if outcome is RestartRequestOutcome.MANUAL_RECOVERY_REQUIRED:
                device_restart_pending_until = 0.0
                return jsonify({
                    "error": "Device restart requires operator recovery.",
                    "manual_recovery_required": True,
                }), 409
            if outcome not in (
                RestartRequestOutcome.CREATED, RestartRequestOutcome.ACTIVE_REQUEST,
            ):
                LOGGER.error("Fixed device restart publisher returned an invalid outcome")
                device_restart_pending_until = 0.0
                return jsonify({"error": "Device restart could not be scheduled."}), 503
            if not locally_pending:
                device_restart_pending_until = (
                    requested_at + DEVICE_RESTART_PENDING_TTL_SECONDS
                )
        return jsonify({
            "restarting": True,
            "already_pending": (
                outcome is RestartRequestOutcome.ACTIVE_REQUEST or locally_pending
            ),
        }), 202

    return app
