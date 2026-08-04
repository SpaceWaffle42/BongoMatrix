"""Flask control panel and PIN-pairing routes."""

from __future__ import annotations

import json
import math
import secrets
import logging
import os
import stat
import subprocess
import sys
import tempfile
import threading
from datetime import timedelta
from functools import wraps
from pathlib import Path

from flask import Flask, abort, jsonify, redirect, render_template, request, send_file, session, url_for
from PIL import Image, UnidentifiedImageError
from werkzeug.exceptions import RequestEntityTooLarge
from werkzeug.utils import secure_filename

from config_store import ALLOWED_MODULES, DEFAULT_CONFIG
from module_options import COLOUR_HEX, LAVA_PALETTES
from modules.weather_app import OpenMeteoClient
from pairing import PairedDeviceStore

MEDIA_EXTENSIONS = {".gif", ".png", ".jpg", ".jpeg", ".webp", ".mp4"}
Image.MAX_IMAGE_PIXELS = 1_000_000
LOGGER = logging.getLogger(__name__)


class UnsafeMediaMove(ValueError):
    """A media path changed identity while it was being reorganized."""

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
):
    session_generation = session_generation or secrets.token_hex(16)
    media_root = Path(media_dir or Path(__file__).resolve().parent / "resources" / "media")
    media_root.mkdir(parents=True, exist_ok=True)
    built_in_media_root = media_root / "built-in"
    media_lock = threading.RLock()
    sanitizer = Path(sanitizer_path or Path(__file__).resolve().parent / "media_sanitizer.py")
    sanitizer_python = sanitizer_python or sys.executable
    location_search = location_search or OpenMeteoClient(timeout=5).search_locations
    device_store = device_store or PairedDeviceStore()
    app = Flask(__name__, template_folder="app/templates", static_folder="app/static")
    app.config.update(
        SECRET_KEY=secret_key,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Strict",
        PERMANENT_SESSION_LIFETIME=timedelta(days=365),
        MAX_CONTENT_LENGTH=128 * 1024 * 1024,
    )

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
                except (
                    OSError, ValueError, KeyError, IndexError, json.JSONDecodeError,
                    subprocess.SubprocessError, UnidentifiedImageError,
                    Image.DecompressionBombError,
                ):
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
            output.unlink(missing_ok=True)
            raise ValueError("invalid or unsafe media")
        finally:
            if source_fd is not None:
                os.close(source_fd)
            os.close(output_fd)

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

    def replace_canvas_media_references(modules, old_reference, new_reference):
        replacements = 0
        for module in modules if isinstance(modules, list) else []:
            if not isinstance(module, dict) or module.get("type") != "canvas":
                continue
            media_layer_ids = set()
            layers = module.get("layers", [])
            for layer in layers if isinstance(layers, list) else []:
                if not isinstance(layer, dict) or layer.get("type") != "media":
                    continue
                media_layer_ids.add(layer.get("id"))
                property_sets = [layer.get("properties", {})]
                property_sets.extend(
                    keyframe.get("properties", {})
                    for keyframe in layer.get("keyframes", [])
                    if isinstance(keyframe, dict)
                )
                for properties in property_sets:
                    if isinstance(properties, dict) and properties.get("source") == old_reference:
                        properties["source"] = new_reference
                        replacements += 1
            for override in module.get("overrides", []):
                if not isinstance(override, dict) or override.get("layer_id") not in media_layer_ids:
                    continue
                properties = override.get("properties", {})
                if isinstance(properties, dict) and properties.get("source") == old_reference:
                    properties["source"] = new_reference
                    replacements += 1
        return replacements

    def requires_pairing(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            if (
                not session.get("paired")
                or session.get("generation") != session_generation
                or not device_store.contains(session.get("device_id"))
            ):
                session.clear()
                if request.path.startswith("/api/"):
                    return jsonify({"error": "Pairing session expired."}), 401
                return redirect(url_for("pair"))
            return view(*args, **kwargs)

        return wrapped

    @app.errorhandler(RequestEntityTooLarge)
    def upload_too_large(_error):
        if request.path.startswith("/api/"):
            return jsonify({"error": "That file exceeds the 128 MB upload limit."}), 413
        return "That file exceeds the 128 MB upload limit.", 413

    def begin_additional_pairing():
        max_devices = config_store.get()["pairing"]["max_devices"]
        if len(device_store.list()) >= max_devices:
            return False
        pin = pairing.issue_pin()
        controller.show_pairing_pin(pin, ttl_seconds=pairing.remaining_seconds())
        return True

    @app.get("/pair")
    def pair():
        if session.get("paired") and device_store.contains(session.get("device_id")):
            return render_template("pair.html", paired=True)
        pin = pairing.active_pin()
        if pin is None:
            pin = pairing.issue_pin()
        controller.show_pairing_pin(pin, ttl_seconds=pairing.remaining_seconds())
        return render_template("pair.html")

    @app.post("/pair")
    def pair_submit():
        device_name = request.form.get("device_name", "").strip()
        if not 1 <= len(device_name) <= 40 or any(not character.isprintable() for character in device_name):
            return render_template("pair.html", error="Device name must be 1-40 characters."), 400
        max_devices = config_store.get()["pairing"]["max_devices"]
        if len(device_store.list()) >= max_devices:
            return render_template(
                "pair.html",
                error="The Matrix has reached its paired-device limit. Remove a device before pairing another.",
            ), 409
        if pairing.verify(request.form.get("pin", "").strip()):
            try:
                device = device_store.add(device_name, max_devices=max_devices)
            except ValueError:
                return render_template(
                    "pair.html",
                    error="The Matrix has reached its paired-device limit. Remove a device before pairing another.",
                ), 409
            session.clear()
            session["paired"] = True
            session["generation"] = session_generation
            session["device_id"] = device["id"]
            session.permanent = True
            controller.end_pairing()
            controller.start()
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
        if not device_store.remove(device_id):
            abort(404)
        if current:
            session.clear()
        return jsonify({"removed": device_id, "current": current})

    @app.post("/logout")
    def logout():
        device_id = session.get("device_id")
        if device_id:
            device_store.remove(device_id)
        session.clear()
        return redirect(url_for("pair"))

    @app.get("/")
    @requires_pairing
    def dashboard():
        controller.start()
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
        return render_template(
            "dashboard.html",
            config=config,
            modules=sorted(ALLOWED_MODULES) + custom_names,
            lava_palettes=lava_palettes,
            status=controller.status(),
            paired_devices=paired_devices,
            media_files=media_items(),
            built_in_defaults={
                "weather": DEFAULT_CONFIG["weather"],
                "clock_lava": DEFAULT_CONFIG["lava"],
                "splash": DEFAULT_CONFIG["media_layout"]["splash"],
                "sleep": DEFAULT_CONFIG["media_layout"]["sleep"],
            },
        )

    @app.get("/api/media")
    @requires_pairing
    def list_media():
        return jsonify(media_items())

    @app.post("/api/media")
    @requires_pairing
    def upload_media():
        upload = request.files.get("file")
        filename = safe_media_name(upload.filename if upload else "")
        if upload is None or filename is None:
            return jsonify({"error": "Choose a GIF, PNG, JPEG, WebP, or MP4 file with a safe filename."}), 400
        destination = media_root / filename
        temporary = sanitized = None
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
                    return jsonify({"error": "A media file with that name already exists."}), 409
                item = media_item(destination)
            except (OSError, ValueError):
                return jsonify({"error": "The uploaded file is not valid or exceeds the media safety limits."}), 400
            finally:
                temporary.unlink(missing_ok=True)
                if sanitized is not None:
                    sanitized.unlink(missing_ok=True)
        return jsonify(item), 201

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

    @app.post("/api/media/<path:filename>/classification")
    @requires_pairing
    def classify_media(filename):
        filename = safe_media_reference(filename)
        changes = request.get_json(silent=True)
        if (
            filename is None
            or not isinstance(changes, dict)
            or set(changes) != {"built_in"}
            or not isinstance(changes["built_in"], bool)
        ):
            return jsonify({"error": "Choose whether this file belongs in Built-in media or My media."}), 400
        target_name = Path(filename).name
        target_reference = f"built-in/{target_name}" if changes["built_in"] else target_name
        with media_lock:
            source = media_path(filename)
            if source is None:
                abort(404)
            try:
                source_info = source.lstat()
            except FileNotFoundError:
                abort(404)
            if not stat.S_ISREG(source_info.st_mode) or source.is_symlink():
                abort(404)
            if target_reference == filename:
                return jsonify({**media_item(source), "references_updated": 0, "display_reloaded": True})
            built_in_media_root.mkdir(mode=0o755, exist_ok=True)
            try:
                built_in_info = built_in_media_root.lstat()
            except OSError:
                abort(404)
            if not stat.S_ISDIR(built_in_info.st_mode) or built_in_media_root.is_symlink():
                abort(404)
            destination = media_path(target_reference)
            if destination is None:
                abort(404)

            config = config_store.get()
            original_media = dict(config["media"])
            original_modules = json.loads(json.dumps(config["custom_modules"]))
            references_updated = 0
            for slot, reference in config["media"].items():
                if reference == filename:
                    config["media"][slot] = target_reference
                    references_updated += 1
            references_updated += replace_canvas_media_references(
                config["custom_modules"], filename, target_reference
            )

            root_fd = built_in_fd = source_fd = destination_fd = None
            destination_info = None
            linked = config_updated = False
            source_directory_fd = destination_directory_fd = None

            def destination_still_matches():
                if destination_info is None or destination_directory_fd is None:
                    return False
                try:
                    current = os.stat(
                        target_name,
                        dir_fd=destination_directory_fd,
                        follow_symlinks=False,
                    )
                except FileNotFoundError:
                    return False
                return (
                    stat.S_ISREG(current.st_mode)
                    and (current.st_dev, current.st_ino)
                    == (destination_info.st_dev, destination_info.st_ino)
                )

            try:
                directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
                root_fd = os.open(media_root, directory_flags)
                built_in_fd = os.open("built-in", directory_flags, dir_fd=root_fd)
                source_directory_fd = built_in_fd if filename.startswith("built-in/") else root_fd
                destination_directory_fd = built_in_fd if target_reference.startswith("built-in/") else root_fd
                source_fd = os.open(target_name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=source_directory_fd)
                source_info = os.fstat(source_fd)
                if not stat.S_ISREG(source_info.st_mode):
                    raise UnsafeMediaMove("source is not a regular file")
                os.link(
                    target_name,
                    target_name,
                    src_dir_fd=source_directory_fd,
                    dst_dir_fd=destination_directory_fd,
                    follow_symlinks=False,
                )
                linked = True
                os.fsync(destination_directory_fd)
                destination_fd = os.open(
                    target_name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=destination_directory_fd
                )
                destination_info = os.fstat(destination_fd)
                if (
                    not stat.S_ISREG(destination_info.st_mode)
                    or (destination_info.st_dev, destination_info.st_ino)
                    != (source_info.st_dev, source_info.st_ino)
                ):
                    raise UnsafeMediaMove("destination does not match the opened source")
                item = media_item(destination)
                if references_updated:
                    config_store.update({
                        "media": config["media"],
                        "custom_modules": config["custom_modules"],
                    })
                    config_updated = True
                current_source = os.stat(
                    target_name, dir_fd=source_directory_fd, follow_symlinks=False
                )
                if (
                    not stat.S_ISREG(current_source.st_mode)
                    or (current_source.st_dev, current_source.st_ino)
                    != (source_info.st_dev, source_info.st_ino)
                    or not destination_still_matches()
                ):
                    raise UnsafeMediaMove("source or destination changed before removal")
                os.unlink(target_name, dir_fd=source_directory_fd)
                os.fsync(source_directory_fd)
            except FileExistsError:
                recovered_interrupted_move = False
                if not linked and source_fd is not None and destination_directory_fd is not None:
                    try:
                        destination_fd = os.open(
                            target_name,
                            os.O_RDONLY | os.O_NOFOLLOW,
                            dir_fd=destination_directory_fd,
                        )
                        destination_info = os.fstat(destination_fd)
                        current_source = os.stat(
                            target_name,
                            dir_fd=source_directory_fd,
                            follow_symlinks=False,
                        )
                        same_regular_file = (
                            stat.S_ISREG(destination_info.st_mode)
                            and stat.S_ISREG(current_source.st_mode)
                            and (destination_info.st_dev, destination_info.st_ino)
                            == (source_info.st_dev, source_info.st_ino)
                            == (current_source.st_dev, current_source.st_ino)
                        )
                        if same_regular_file:
                            if references_updated:
                                config_store.update({
                                    "media": config["media"],
                                    "custom_modules": config["custom_modules"],
                                })
                                config_updated = True
                            current_source = os.stat(
                                target_name,
                                dir_fd=source_directory_fd,
                                follow_symlinks=False,
                            )
                            if (
                                not stat.S_ISREG(current_source.st_mode)
                                or (current_source.st_dev, current_source.st_ino)
                                != (source_info.st_dev, source_info.st_ino)
                                or not destination_still_matches()
                            ):
                                raise UnsafeMediaMove("interrupted move paths changed during recovery")
                            item = media_item(destination)
                            os.unlink(target_name, dir_fd=source_directory_fd)
                            os.fsync(source_directory_fd)
                            recovered_interrupted_move = True
                    except (FileNotFoundError, OSError, UnsafeMediaMove):
                        recovered_interrupted_move = False
                if linked and destination_still_matches():
                    try:
                        os.unlink(target_name, dir_fd=destination_directory_fd)
                        os.fsync(destination_directory_fd)
                    except FileNotFoundError:
                        pass
                if not recovered_interrupted_move:
                    if config_updated:
                        config_store.update({
                            "media": original_media,
                            "custom_modules": original_modules,
                        })
                        config_updated = False
                    return jsonify({"error": "A media file with that name already exists in the destination."}), 409
            except (OSError, UnsafeMediaMove):
                if config_updated:
                    config_store.update({
                        "media": original_media,
                        "custom_modules": original_modules,
                    })
                if linked and destination_still_matches():
                    try:
                        os.unlink(target_name, dir_fd=destination_directory_fd)
                        os.fsync(destination_directory_fd)
                    except FileNotFoundError:
                        pass
                return jsonify({"error": "The media file changed or could not be moved safely."}), 409
            except Exception:
                if config_updated:
                    config_store.update({
                        "media": original_media,
                        "custom_modules": original_modules,
                    })
                if linked and destination_still_matches():
                    try:
                        os.unlink(target_name, dir_fd=destination_directory_fd)
                        os.fsync(destination_directory_fd)
                    except FileNotFoundError:
                        pass
                raise
            finally:
                for descriptor in (destination_fd, source_fd, built_in_fd, root_fd):
                    if descriptor is not None:
                        os.close(descriptor)
        display_reloaded = True
        try:
            controller.reload()
        except Exception:
            display_reloaded = False
            LOGGER.exception("Media moved successfully, but the display could not be reloaded")
        return jsonify({
            **item,
            "references_updated": references_updated,
            "display_reloaded": display_reloaded,
        })

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
        return jsonify({"removed": filename})

    @app.get("/api/config")
    @requires_pairing
    def get_config():
        return jsonify(config_store.get())

    @app.post("/api/config")
    @requires_pairing
    def update_config():
        try:
            changes = request.get_json(force=True)
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
                    for filename in changes["media"].values():
                        ensure_media_file(filename)
                if isinstance(changes, dict) and isinstance(changes.get("custom_modules"), list):
                    for reference in canvas_media_references(changes["custom_modules"]):
                        ensure_media_file(reference)
                config = config_store.update(changes)
        except (TypeError, ValueError, KeyError) as exc:
            return jsonify({"error": str(exc)}), 400
        controller.reload()
        return jsonify(config)

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
        return jsonify(controller.status())

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
            controller.stop()
        elif action == "reload":
            controller.reload()
        else:
            abort(404)
        return jsonify(controller.status())

    return app
