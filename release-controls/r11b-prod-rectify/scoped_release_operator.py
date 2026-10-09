"""Finding-bound Splash/Sleep packaging and target-local release supervision.

Not a deployment authorization or a substitute for independent review/main CI.
Private state bytes never appear in returned records or subprocess output.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import http.client
import importlib.util
import json
import os
import re
import secrets
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import time

RUNTIME_PATHS = (
    "app/static/custom_canvas.js", "app/static/screen_media.js",
    "app/templates/dashboard.html", "config_store.py", "display_controller.py",
    "edit_preview.py", "matrix_framework.py", "media_resolver.py",
    "modules/announcement_board.py", "modules/custom_text.py",
    "modules/media_player.py", "modules/sleep_screen.py",
    "modules/splash_screen.py", "screen_media.py", "webapp.py",
)
HELPER_HASHES = {
    "deploy/release_bundle.py": "6d1a98df08809b5292e85bd968f7f3b2582a189a854520ce636dc1018efca755",
    "release_argparse.py": "090e98340173d99605f9701c290e78cafc21da49e072dfd5242786685ee5a5f1",
}
HELPER_PREDECESSORS = {
    "deploy/release_bundle.py": {
        "sha256": "118d435da056b880f40dbcb4a3f0e3c1978c6e422f6508102edfd00b63e5b0e0",
        "size": 108049, "uid": 1000, "gid": 985, "mode": "0664",
    },
    "release_argparse.py": {
        "sha256": "090e98340173d99605f9701c290e78cafc21da49e072dfd5242786685ee5a5f1",
        "size": 1043, "uid": 1000, "gid": 985, "mode": "0664",
    },
}
HELPER_TRANSITION = {
    "version": 1,
    "retained_binding_sha256": "80d9c488f0d079b12a18b0859d7272ab095629fd019da6adb2b9ad6a33e06937",
    "predecessor": HELPER_PREDECESSORS, "candidate": HELPER_HASHES,
    "prepare_roles": ["initial", "final"], "rollback_role": "predecessor",
}
ABSENT_ADDITIONS = {"screen_media.py", "app/static/screen_media.js"}
FROZEN_IDENTITY = "9c09708e426594a24ba88a0e86a980b439d35a03389bdc11677a755fe8a441b2"
MUTABLE_ROOTS = ("data", "resources/media")


class OperatorError(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise OperatorError(message)


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def sha(data):
    return hashlib.sha256(data).hexdigest()


DIAGNOSTIC_SHA = '709c66be37fe70a0e91c5f3072afc4511bab242c16bd1c7d9275bdb7363bd5f5'


def failure_custody():
    path = Path(__file__).with_name("release_failure_custody.py")
    require(sha(path.read_bytes()) == DIAGNOSTIC_SHA, "diagnostic helper identity mismatch")
    spec = importlib.util.spec_from_file_location("release_failure_custody", path)
    require(spec is not None and spec.loader is not None, "diagnostic helper load unavailable")
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module


def diagnostic_context(stage, phase, binding=None):
    binding = binding if binding is not None else load_json(Path(stage) / "binding.json")
    context = {"stage": str(stage), "phase": phase,
               "binding_sha256": sha(read_at(stage, "binding.json")[0]),
               "source_identity": binding["source_identity"], "spec_sha256": binding["spec_sha256"],
               "operator_sha256": sha(Path(__file__).read_bytes())}
    request_path = Path(stage) / ("request-" + phase + ".json")
    if request_path.exists():
        request = load_json(request_path)
        context["caller_sha256"] = request["acceptance_sha256"]
    return context


def retain_diagnostic(stage, phase, slot, stage_name, exc, binding=None, exit_code=None):
    try:
        return failure_custody().retain(diagnostic_context(stage, phase, binding), slot, stage_name, exc, exit_code)
    except BaseException:
        print("MATRIX_FAILURE_CUSTODY_UNAVAILABLE; no acceptance", file=sys.stderr)
        return None


def primitives():
    path = Path(__file__).with_name("release_bundle.py")
    if not path.exists():
        path = Path(__file__).parent / "release/deploy/release_bundle.py"
    require(sha(path.read_bytes()) == HELPER_HASHES["deploy/release_bundle.py"],
            "release primitive identity mismatch")
    spec = importlib.util.spec_from_file_location("scoped_release_bundle", path)
    require(spec is not None and spec.loader is not None, "release primitive load unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def read_at(root, relative, missing=False):
    return primitives()._read_confined_regular(Path(root), relative, missing_ok=missing)


def record(content, info):
    return {"sha256": sha(content), "uid": info.st_uid, "gid": info.st_gid,
            "mode": f"{stat.S_IMODE(info.st_mode):04o}", "size": len(content)}


def directory_record(path):
    fd = primitives()._open_absolute_directory(Path(path).absolute())
    try:
        s = os.fstat(fd)
        return {"uid": s.st_uid, "gid": s.st_gid,
                "mode": f"{stat.S_IMODE(s.st_mode):04o}", "dev": s.st_dev, "ino": s.st_ino}
    finally:
        os.close(fd)


def publish(path, value):
    """Atomic/fsynced private journal; no untrusted symlink traversal."""
    path = Path(path)
    fd = primitives()._open_absolute_directory(path.parent.absolute())
    try:
        tmp = path.name + "." + secrets.token_hex(8) + ".new"
        out = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=fd)
        try:
            data = encoded(value) + b"\n"
            with os.fdopen(out, "wb", closefd=False) as stream:
                stream.write(data)
                stream.flush()
            os.fsync(out)
        finally:
            os.close(out)
        os.rename(tmp, path.name, src_dir_fd=fd, dst_dir_fd=fd)
        os.fsync(fd)
    finally:
        os.close(fd)


def is_runtime(path):
    return (path.startswith(("app/", "modules/")) or
            ("/" not in path and (path.endswith(".py") or path == "requirements.txt")))


def service_target(destination, stage, *, fixture_unit=None, fixture_port=None):
    if fixture_unit is None and fixture_port is None:
        return {"mode": "production", "service": "matrix-web.service", "port": 5000}
    require(isinstance(fixture_unit, str) and
            re.fullmatch(r"matrix-release-proof-[A-Za-z0-9][A-Za-z0-9_-]*\.service", fixture_unit),
            "unsafe fixture service unit")
    require(type(fixture_port) is int and 1024 < fixture_port <= 65535 and fixture_port != 5000,
            "unsafe fixture HTTP port")
    destination, stage = safe_stage(destination), safe_stage(stage)
    production = Path("/home/admin/matrix")
    for path in (destination, stage):
        require(path == path.resolve() and path != production and production not in path.parents and
                path not in production.parents, "fixture path shadows production or follows symlink")
    require(destination.name.startswith("matrix-release-proof-") and destination != stage and
            destination not in stage.parents and stage not in destination.parents,
            "fixture destination/stage must be separate owned paths")
    return {"mode": "fixture", "service": fixture_unit, "fixtureRoot": str(destination), "port": fixture_port}


def bound_target(binding):
    target = binding.get("target", {"mode": "production", "service": "matrix-web.service", "port": 5000})
    expected = service_target(binding["destination"], binding["stage"],
                              fixture_unit=target.get("service") if target.get("mode") == "fixture" else None,
                              fixture_port=target.get("port") if target.get("mode") == "fixture" else None)
    require(target == expected, "service target binding mismatch")
    return target


def assert_service_target(services, binding):
    target = bound_target(binding)
    require(getattr(services, "target", None) == target or
            (target["mode"] == "production" and not hasattr(services, "target")), "service target changed")


def prepare_package(source, manifest, route, destination, stage, *, expected_identity=FROZEN_IDENTITY, scoped=False,
                    fixture_unit=None, fixture_port=None):
    source, destination, stage = map(Path, (source, destination, stage))
    target = service_target(destination, stage, fixture_unit=fixture_unit, fixture_port=fixture_port)
    files = manifest["files"]
    require((len(files) in (192, 194) or
             (len(files) == 206 and manifest["head"] == "9315292170e9ee14a28c2c3c228bc05e981de9e4" and
              expected_identity == "f954ddfa4714f641f092f66cd940302b5236a79067af7cbe5cc6ef0b7ede1fdd") or
             successor_source_matches(manifest, expected_identity)) and
            len({e["path"] for e in files}) == len(files),
            "source closure must contain 192/194 unique entries or the exact reviewed source206 generation")
    source_identity = sha(encoded({"head": manifest["head"], "files": files}))
    require(source_identity == manifest["source_identity"] == expected_identity,
            "source identity mismatch")
    rb = primitives()
    contents = {}
    selected_paths = set(RUNTIME_PATHS) | set(HELPER_HASHES)
    if scoped:
        require(rb._release_files(source) == selected_paths, "transport payload must be exactly 17 entries")
    for entry in files:
        if scoped and entry["path"] not in selected_paths:
            continue
        data, info = rb._read_confined_regular(source, entry["path"], missing_ok=False)
        require(sha(data) == entry["sha256"] and len(data) == entry["size"] and
                (scoped or oct(stat.S_IMODE(info.st_mode)) == entry["mode"]), "source closure mismatch")
        contents[entry["path"]] = data
    plan = route["source_plan"]
    delta = {e["path"]: e for e in plan["runtime_delta"]}
    helpers = {e["path"]: e["sha256"] for e in plan["operational_package_entries"]}
    require(len(plan["runtime_delta"]) == 15 and set(delta) == set(RUNTIME_PATHS), "runtime scope mismatch")
    require(helpers == HELPER_HASHES, "helper scope/identity mismatch")
    require(successor_source_matches(manifest, expected_identity), "helper transition requires exact successor source pin")
    require(plan.get("helper_transition") == HELPER_TRANSITION, "helper transition pair/phase binding mismatch")
    selected = list(RUNTIME_PATHS) + list(HELPER_HASHES)
    predecessor, intended, parents = {}, [], {}
    for relative in selected:
        require(sha(contents[relative]) == (delta[relative]["sha256"] if relative in delta
                                           else HELPER_HASHES[relative]), "package identity mismatch")
        current = rb._read_confined_regular(destination, relative, missing_ok=True)
        parent = Path(relative).parent
        while str(parent) != ".":
            parents[str(parent)] = directory_record(destination / parent)
            parent = parent.parent
        if relative in delta:
            prior = delta[relative]["predecessor"]
            if prior.get("absent"):
                require(relative in ABSENT_ADDITIONS and current is None, "absent predecessor mismatch")
            else:
                require(current is not None, "predecessor missing")
                r = record(*current)
                require(all(r[k] == (f"{int(prior[k], 8):04o}" if k == "mode" else prior[k])
                            for k in r), "actual predecessor identity/metadata mismatch")
        require(current is not None or relative in ABSENT_ADDITIONS, "unexpected absent helper")
        if relative in HELPER_HASHES:
            require(record(*current) == HELPER_PREDECESSORS[relative],
                    "helper predecessor identity/metadata mismatch")
        predecessor[relative] = record(*current) if current else {"absent": True}
        metadata = {k: predecessor[relative][k] for k in ("uid", "gid", "mode")} if current else {
            "uid": 1000, "gid": 985, "mode": "0644"}
        intended.append({"path": relative, "sha256": sha(contents[relative]), **metadata})
    unaffected = {}
    for entry in files:
        relative = entry["path"]
        if is_runtime(relative) and relative not in selected:
            current = rb._read_confined_regular(destination, relative, missing_ok=False)
            require(sha(current[0]) == entry["sha256"], "unaffected source closure mismatch")
            unaffected[relative] = record(*current)
    roots = {".": directory_record(destination)}
    for relative in ("venv", ".git"):
        if (destination / relative).exists():
            roots[relative] = directory_record(destination / relative)
    require(not stage.exists(), "stage must be fresh")
    stage.mkdir(mode=0o700)
    release = stage / "release"
    release.mkdir(mode=0o700)
    for relative in selected:
        (release / relative).parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        rb._publish_new_path(release / relative, contents[relative], mode=0o400)
    spec = {"version": 1, "files": intended}
    publish(stage / "release-spec.json", spec)
    rb.verify_staged_release(release / "deploy/release_bundle.py", release, stage / "release-spec.json")
    binding = {"version": 1, "source_identity": source_identity, "source_head": manifest["head"],
               "source_closure_sha256": sha(encoded(files)), "route_sha256": sha(encoded(route)),
               "spec_sha256": sha((stage / "release-spec.json").read_bytes()),
               "predecessor": predecessor, "unaffected": unaffected, "parents": parents,
               "roots": roots, "destination": str(destination.absolute()), "stage": str(stage.absolute()), "target": target}
    binding["helper_transition"] = transition_binding(binding)
    publish(stage / "binding.json", binding)
    return binding


def load_json(path):
    value = read_at(Path(path).parent, Path(path).name)
    return json.loads(value[0])


def transition_binding(binding):
    """Closed reviewed pair, bound to this source plan and target filesystem.

    Preparation only admits the retained predecessor, including after rollback.
    Already-candidate targets are not a fresh initial/final preparation authority.
    """
    return {"plan": HELPER_TRANSITION, **{k: binding[k] for k in (
        "source_identity", "source_head", "source_closure_sha256", "route_sha256",
        "destination", "roots", "parents")}}


def assert_binding(stage, binding, *, predecessor=False):
    rb = primitives()
    stage = Path(stage)
    require(str(stage.absolute()) == binding["stage"], "stage binding mismatch")
    require(sha(read_at(stage, "release-spec.json")[0]) == binding["spec_sha256"], "spec binding mismatch")
    rb.verify_staged_release(stage / "release/deploy/release_bundle.py", stage / "release", stage / "release-spec.json")
    require(binding.get("helper_transition") == transition_binding(binding), "helper transition binding changed")
    spec_helpers = {e["path"]: e for e in load_json(stage / "release-spec.json")["files"]
                    if e["path"] in HELPER_HASHES}
    require(spec_helpers == {p: {"path": p, "sha256": HELPER_HASHES[p],
            **{k: prior[k] for k in ("uid", "gid", "mode")}}
            for p, prior in HELPER_PREDECESSORS.items()} and
            all(binding["predecessor"].get(p) == prior for p, prior in HELPER_PREDECESSORS.items()),
            "helper transition predecessor/candidate cross-binding mismatch")
    live = Path(binding["destination"])
    for relative, expected in {**binding["roots"], **binding["parents"]}.items():
        require(directory_record(live / relative) == expected, "preserved directory identity/metadata mismatch")
    for relative, expected in binding["unaffected"].items():
        require(record(*read_at(live, relative)) == expected, "unaffected source closure changed")
    if predecessor:
        for relative, expected in binding["predecessor"].items():
            current = read_at(live, relative, missing=True)
            require((record(*current) if current else {"absent": True}) == expected,
                    "predecessor changed before mutation")


def tree_snapshot(root):
    """Descriptor-confined byte/metadata inventory; rejects links/special files."""
    result = {}
    rb = primitives()
    fd = rb._open_absolute_directory(Path(root).absolute())

    def walk(directory, prefix):
        info = os.fstat(directory)
        result[prefix] = {"kind": "directory", "uid": info.st_uid, "gid": info.st_gid,
                          "mode": f"{stat.S_IMODE(info.st_mode):04o}", "mtime_ns": info.st_mtime_ns}
        for name in sorted(os.listdir(directory)):
            relative = name if prefix == "." else prefix + "/" + name
            info = os.stat(name, dir_fd=directory, follow_symlinks=False)
            if stat.S_ISDIR(info.st_mode):
                child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
                try:
                    walk(child, relative)
                finally:
                    os.close(child)
            else:
                require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1, "private state contains link/special file")
                data, opened = rb._read_confined_regular(directory, name, missing_ok=False)
                require((opened.st_dev, opened.st_ino, opened.st_mtime_ns, opened.st_size) ==
                        (info.st_dev, info.st_ino, info.st_mtime_ns, info.st_size), "state changed during snapshot")
                result[relative] = {"kind": "file", **record(data, opened), "mtime_ns": opened.st_mtime_ns}
    try:
        walk(fd, ".")
    finally:
        os.close(fd)
    return result


def checkpoint(stage, binding):
    rb = primitives()
    live = Path(binding["destination"])
    snapshots = {relative: tree_snapshot(live / relative) for relative in MUTABLE_ROOTS}
    sizes = [e["size"] for tree in snapshots.values() for e in tree.values() if e["kind"] == "file"]
    block = os.statvfs(stage).f_frsize
    allocated = sum(((n + block - 1) // block) * block for n in sizes)
    directories = sum(e["kind"] == "directory" for tree in snapshots.values() for e in tree.values())
    originals = sum(e.get("size", 0) for e in binding["predecessor"].values())
    needed = allocated + directories * block + originals + max(sizes, default=0) + 1024 * 1024
    require(shutil.disk_usage(stage).free >= needed, "insufficient storage for actual state/source checkpoint")
    require(shutil.disk_usage(live).free >= max(sizes, default=0) + 1024 * 1024,
            "insufficient restore scratch space")
    archive = Path(stage) / "checkpoint"
    archive.mkdir(mode=0o700)
    for relative, tree in snapshots.items():
        target = archive / relative
        target.mkdir(parents=True, mode=0o700)
        for path, e in tree.items():
            if e["kind"] == "directory":
                (target / path).mkdir(parents=True, mode=0o700, exist_ok=True)
            else:
                data, info = read_at(live / relative, path)
                require(record(data, info) == {k: e[k] for k in record(data, info)}, "state copy mismatch")
                rb._publish_new_path(target / path, data, mode=0o400)
        require(tree_snapshot(live / relative) == tree, "state changed during coherent checkpoint")
    publish(Path(stage) / "checkpoint.json", {"roots": snapshots, "required_bytes": needed})
    verify_checkpoint(stage)
    return sha(read_at(stage, "checkpoint.json")[0])


def verify_checkpoint(stage):
    checkpoint_record = load_json(Path(stage) / "checkpoint.json")
    require(set(checkpoint_record["roots"]) == set(MUTABLE_ROOTS), "checkpoint scope mismatch")
    for relative, tree in checkpoint_record["roots"].items():
        actual = tree_snapshot(Path(stage) / "checkpoint" / relative)
        require(set(actual) == set(tree), "checkpoint archive scope mismatch")
        for path, entry in tree.items():
            require(actual[path]["kind"] == entry["kind"], "checkpoint archive type mismatch")
            if entry["kind"] == "file":
                require(actual[path]["sha256"] == entry["sha256"] and actual[path]["size"] == entry["size"],
                        "checkpoint archive bytes mismatch")
    return checkpoint_record["roots"]


def restore_state(stage, binding):
    rb = primitives()
    snapshots = verify_checkpoint(stage)
    for relative, expected in snapshots.items():
        root = Path(binding["destination"]) / relative
        actual = tree_snapshot(root)
        fd = rb._open_absolute_directory(root.absolute())
        try:
            for path in sorted(set(actual) - set(expected), key=lambda p: (p.count("/"), p), reverse=True):
                parent_fd, name = rb._open_confined_parent(fd, path)
                try:
                    if actual[path]["kind"] == "directory":
                        os.rmdir(name, dir_fd=parent_fd)
                    else:
                        os.unlink(name, dir_fd=parent_fd)
                    os.fsync(parent_fd)
                finally:
                    os.close(parent_fd)
            for path, entry in expected.items():
                if entry["kind"] == "directory" and path != ".":
                    child = rb._open_or_create_confined_directory(fd, tuple(Path(path).parts))
                    os.close(child)
                elif entry["kind"] == "file":
                    data = read_at(Path(stage) / "checkpoint" / relative, path)[0]
                    rb._atomic_replace_confined(fd, path, data, entry["uid"], entry["gid"], entry["mode"])
                    parent_fd, name = rb._open_confined_parent(fd, path)
                    try:
                        os.utime(name, ns=(entry["mtime_ns"], entry["mtime_ns"]), dir_fd=parent_fd, follow_symlinks=False)
                    finally:
                        os.close(parent_fd)
            for path, entry in sorted(expected.items(), key=lambda pair: pair[0].count("/"), reverse=True):
                if entry["kind"] == "directory":
                    child = os.dup(fd) if path == "." else rb._open_or_create_confined_directory(fd, tuple(Path(path).parts))
                    try:
                        os.fchown(child, entry["uid"], entry["gid"])
                        os.fchmod(child, int(entry["mode"], 8))
                        os.utime(child, ns=(entry["mtime_ns"], entry["mtime_ns"]))
                        os.fsync(child)
                    finally:
                        os.close(child)
            os.fsync(fd)
        finally:
            os.close(fd)
        require(tree_snapshot(root) == expected, "restored private state verification failed")


def bundle_args(stage, binding):
    stage = Path(stage)
    return (stage / "release/deploy/release_bundle.py", stage / "release",
            Path(binding["destination"]), stage / "bundle",
            stage / "bundle/release-manifest.json", stage / "bundle/backup-manifest.json")


def deploy_source(stage, binding):
    primitives().deploy_release(*bundle_args(stage, binding), service=primitives()._NoServiceControl())


def journal(stage, value, phase, **updates):
    value.update(updates)
    value["phase"] = phase
    publish(Path(stage) / value.get("journal_name", "run.json"), value)


def locked(stage):
    return os.open(Path(stage) / "writer.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)


def recover_locked(stage, services, run, binding, *, mutation_boundary=None):
    assert_service_target(services, binding)
    if run.get("outcome") in ("committed", "recovered"):
        # Never restore an old snapshot after legitimate user access was reopened.
        if not run.get("keep_containment"):
            services.release()
        return
    require(run["binding_sha256"] == sha(read_at(stage, "binding.json")[0]), "recovery binding changed")
    services.contain()
    services.stop()
    services.prove_exclusion()
    assert_binding(stage, binding)
    if run.get("mutation_admitted"):
        require(run["checkpoint_sha256"] == sha(read_at(stage, "checkpoint.json")[0]), "checkpoint binding changed")
        verify_checkpoint(stage)
        journal(stage, run, "recovering-source")
        if mutation_boundary is not None:
            mutation_boundary("recovering-source")
        rb = primitives()
        rb.recover_release(*bundle_args(stage, binding), service=rb._NoServiceControl(), initially_active=False)
        rb.verify_release(*bundle_args(stage, binding), mode="rollback")
        journal(stage, run, "recovering-state")
        if mutation_boundary is not None:
            mutation_boundary("recovering-state")
        restore_state(stage, binding)
    assert_binding(stage, binding, predecessor=True)
    journal(stage, run, "predecessor-verified")
    services.start()
    readiness = services.readiness(30)
    require(readiness["status"] == 302 and readiness["redirects_followed"] is False,
            "predecessor readiness failed")
    assert_binding(stage, binding, predecessor=True)
    journal(stage, run, "recovered", outcome="recovered", rollback_http=readiness)
    if not run.get("keep_containment"):
        services.release()


def recover(stage, services, phase="initial"):
    if phase == "abort":
        return terminal_abort(Path(stage), services)
    fd = locked(stage)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        name = "run-rollback.json" if phase == "rollback" else "run.json"
        if not (Path(stage) / name).exists():
            return
        try:
            recover_locked(stage, services, load_json(Path(stage) / name), load_json(Path(stage) / "binding.json"))
        except BaseException as recovery_error:
            retain_diagnostic(stage, phase, "recovery-stoppost", "OPERATOR_RECOVER", recovery_error)
            raise
    finally:
        os.close(fd)


def execute(stage, services, acceptance, *, keep_containment=False):
    stage = Path(stage)
    fd = locked(stage)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        require(getattr(services, "phase", "initial") in HELPER_TRANSITION["prepare_roles"],
                "helper transition execution phase mismatch")
        require(not (stage / "run.json").exists(), "phase must have a fresh independently guarded stage")
        binding = load_json(stage / "binding.json")
        assert_binding(stage, binding, predecessor=True)
        assert_service_target(services, binding)
        services.validate()
        run = {"binding_sha256": sha(read_at(stage, "binding.json")[0]), "outcome": "armed",
               "mutation_admitted": False, "pid": os.getpid(), "keep_containment": keep_containment,
               "acceptance_phase": getattr(services, "phase", "initial")}
        journal(stage, run, "armed")  # PID1 ExecStopPost already installed by launch.
        try:
            services.contain()
            journal(stage, run, "containment-installed")
            services.stop()
            services.prove_exclusion()
            journal(stage, run, "stopped")
            assert_binding(stage, binding, predecessor=True)
            checkpoint_hash = checkpoint(stage, binding)
            rb = primitives()
            args = bundle_args(stage, binding)
            rb.backup_release(*args, stage / "release-spec.json")
            rb.preflight_release(*args)
            journal(stage, run, "checkpoint-verified", checkpoint_sha256=checkpoint_hash)
            services.prove_exclusion()
            journal(stage, run, "mutating", mutation_admitted=True)
            deploy_source(stage, binding)
            rb.verify_release(*args, mode="deploy")
            assert_binding(stage, binding)
            services.start()
            services.prove_exclusion()
            journal(stage, run, "candidate-started")
            readiness = services.readiness(30)
            evidence = acceptance({"binding_sha256": run["binding_sha256"], "source_identity": binding["source_identity"],
                                   "spec_sha256": binding["spec_sha256"], "stage": str(stage)})
            validate_acceptance(evidence, binding, run)
            services.prove_exclusion()
            services.stop()
            rb.verify_release(*args, mode="deploy")
            assert_binding(stage, binding)
            state_after = {r: tree_snapshot(Path(binding["destination"]) / r) for r in MUTABLE_ROOTS}
            publish(stage / "candidate-state.json", state_after)
            # Snapshot does not assert unchanged state: compare to checkpoint before commit.
            require(state_after == verify_checkpoint(stage), "candidate private state changed")
            services.start()
            services.prove_exclusion()
            final_http = services.readiness(30)
            require(readiness["status"] == final_http["status"] == 302 and
                    readiness["redirects_followed"] is final_http["redirects_followed"] is False,
                    "bounded readiness failed")
            rb.verify_release(*args, mode="deploy")
            assert_binding(stage, binding)
            require({r: tree_snapshot(Path(binding["destination"]) / r) for r in MUTABLE_ROOTS} == state_after,
                    "candidate state changed after final start")
            journal(stage, run, "committed", outcome="committed", caller=evidence,
                    http=final_http, state_sha256=sha(encoded(state_after)))
            if not keep_containment:
                services.release()
            return run
        except BaseException as primary:
            retain_diagnostic(stage, run["acceptance_phase"], "primary", "OPERATOR_EXECUTE", primary, binding)
            try:
                recover_locked(stage, services, run, binding)
            except BaseException as recovery_error:
                retain_diagnostic(stage, run["acceptance_phase"], "recovery-inline", "OPERATOR_RECOVER", recovery_error, binding)
            raise
    finally:
        os.close(fd)


def validate_acceptance(evidence, binding, run):
    require(isinstance(evidence, dict), "required identity-bound caller acceptance failed")
    fixture = bound_target(binding)["mode"] == "fixture"
    checks = ({"fixture_http": True, "fixture_source": True, "fixture_state": True} if fixture else
              {"browser": True, "framebuffer": True, "preservation": True})
    if fixture:
        require(evidence.get("acceptedScope") == "disposable-pid1-fixture-only" and
                evidence.get("phase") == run.get("acceptance_phase", "initial") and
                evidence.get("target") == bound_target(binding), "fixture acceptance scope/phase/target mismatch")
    require(isinstance(evidence, dict) and evidence.get("accepted") is True and
            evidence.get("binding_sha256") == run["binding_sha256"] and
            evidence.get("source_identity") == binding["source_identity"] and
            evidence.get("spec_sha256") == binding["spec_sha256"] and
            evidence.get("checks") == checks and
            isinstance(evidence.get("evidence_sha256"), str) and
            re.fullmatch("[0-9a-f]{64}", evidence["evidence_sha256"]),
            "required identity-bound caller acceptance failed")


def probe_http(timeout=30, *, fixture_port=None, connection=http.client.HTTPConnection, pause=time.sleep):
    require(0 < timeout <= 30, "readiness must be bounded to 30 seconds")
    require(fixture_port is None or (type(fixture_port) is int and 1024 < fixture_port <= 65535 and fixture_port != 5000),
            "unsafe fixture HTTP port")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        remaining = deadline - time.monotonic()
        client = connection("127.0.0.1", fixture_port if fixture_port is not None else 5000, timeout=min(5, remaining))
        try:
            client.request("GET", "/")
            if client.getresponse().status == 302:
                return {"status": 302, "redirects_followed": False, "timeout_seconds": timeout}
        except (OSError, http.client.HTTPException):
            pass
        finally:
            client.close()
        pause(min(0.1, max(0, deadline - time.monotonic())))
    raise OperatorError("bounded no-redirect HTTP302 readiness failed")


def command(argv, *, timeout=15):
    result = subprocess.run(argv, capture_output=True, timeout=timeout, check=False)
    require(result.returncode == 0, "target-local command failed (output withheld)")
    return result.stdout.decode()


class SystemServices:
    """Existing PID1 PrivateNetwork containment; target capability is a live gate.

    Only an explicit operator nsenter probe can reach the isolated loopback app.
    """
    unit = "matrix-web.service"
    phase = "initial"
    def __init__(self, stage, containment_stage=None, *, fixture_mode=False):
        self.stage = Path(stage)
        self.containment_stage = Path(containment_stage or stage)
        self.target = bound_target(load_json(self.stage / "binding.json"))
        require(fixture_mode == (self.target["mode"] == "fixture"), "explicit fixture mode/binding mismatch")
        require(bound_target(load_json(self.containment_stage / "binding.json")) == self.target, "containment target changed")
        self.unit = self.target["service"]
        self.fixture_port = self.target["port"] if fixture_mode else None
        self.token = sha(read_at(self.containment_stage, "binding.json")[0])[:16]
        self.dropin = Path("/run/systemd/system") / (self.unit + ".d") / ("90-scoped-" + self.token + ".conf")
        self.content = b"[Service]\nPrivateNetwork=yes\n"

    def properties(self):
        result = command(["/usr/bin/systemctl", "show", self.unit, "--property=ActiveState,MainPID,ControlPID,ControlGroup,PrivateNetwork"])
        return dict(line.split("=", 1) for line in result.splitlines() if "=" in line)

    def validate(self):
        if self.target["mode"] == "fixture":
            props = dict(line.split("=", 1) for line in command([
                "/usr/bin/systemctl", "show", self.unit, "--property=Id,Names,FragmentPath"
            ]).splitlines() if "=" in line)
            require(props.get("Id") == self.unit and props.get("Names", "").split() == [self.unit] and
                    props.get("FragmentPath") == "/run/systemd/system/" + self.unit,
                    "fixture unit must be its own non-aliased disposable runtime unit")
            read_at("/run/systemd/system", self.unit)
        require(Path("/proc/1/comm").read_text().strip() == "systemd", "target PID1 is not systemd")
        require(Path("/usr/bin/nsenter").is_file(), "existing nsenter capability unproved/unavailable")
        require(self.properties().get("ActiveState") == "active", "initial app must be active")
        if self.containment_stage != self.stage:
            self.prove_exclusion()
        else:
            require(not self.dropin.exists(), "fresh containment drop-in required")

    def contain(self):
        self.dropin.parent.mkdir(mode=0o755, exist_ok=True)
        if self.dropin.exists():
            require(read_at(self.dropin.parent, self.dropin.name)[0] == self.content, "containment ownership/content mismatch")
        else:
            primitives()._publish_new_path(self.dropin, self.content, mode=0o600)
        command(["/usr/bin/systemctl", "daemon-reload"])

    def cgroup_members(self, props):
        group = props.get("ControlGroup", "")
        if not group:
            return []
        require(group.startswith("/") and ".." not in Path(group).parts, "unsafe service cgroup")
        root = Path("/sys/fs/cgroup") / group.lstrip("/")
        if not root.exists():
            return []
        members = []
        for path in root.rglob("cgroup.procs"):
            members.extend(int(pid) for pid in path.read_text().split())
        return members

    def stopped(self, props, *, prior=None):
        require(props.get("ActiveState") in ("inactive", "failed") and
                props.get("MainPID") == "0" and props.get("ControlPID") == "0" and
                "ControlGroup" in props and isinstance(props["ControlGroup"], str),
                "app writer stopped properties incomplete or not drained")
        require(not self.cgroup_members(props) and (prior is None or not self.cgroup_members(prior)),
                "app writer cgroup not drained")

    def stop(self):
        before = self.properties()
        require(before.get("ActiveState") in ("inactive", "failed", "active") and
                re.fullmatch(r"0|[1-9][0-9]*", before.get("MainPID", "")) is not None and
                re.fullmatch(r"0|[1-9][0-9]*", before.get("ControlPID", "")) is not None and
                "ControlGroup" in before and isinstance(before["ControlGroup"], str),
                "prior app writer properties incomplete")
        group = before["ControlGroup"]
        require(not group or (group.startswith("/") and ".." not in Path(group).parts), "unsafe prior app cgroup")
        if before["ActiveState"] in ("inactive", "failed"):
            self.stopped(before)
        command(["/usr/bin/systemctl", "stop", self.unit])
        props = self.properties()
        self.stopped(props, prior=before)

    def prove_exclusion(self):
        require(self.dropin.exists() and read_at(self.dropin.parent, self.dropin.name)[0] == self.content, "containment not held")
        props = self.properties()
        require(props.get("PrivateNetwork") == "yes", "PID1 containment not loaded")
        if props.get("ActiveState") in ("inactive", "failed"):
            self.stopped(props)
            return
        require(props.get("ActiveState") == "active", "app service has unstable state")
        require(re.fullmatch(r"[1-9][0-9]*", props.get("MainPID", "")) is not None, "active app PID invalid")
        pid = int(props["MainPID"])
        private = os.stat(f"/proc/{pid}/ns/net").st_ino
        require(private != os.stat("/proc/1/ns/net").st_ino, "app still accepts host/LAN traffic")
        members = self.cgroup_members(props)
        require(pid in members and all(os.stat(f"/proc/{p}/ns/net").st_ino == private for p in members),
                "app writers not wholly in private network namespace")

    def start(self):
        command(["/usr/bin/systemctl", "start", self.unit])
        self.prove_exclusion()
        require(self.properties().get("ActiveState") == "active", "started app is not active")

    def namespace_command(self, argv):
        self.prove_exclusion()
        props = self.properties()
        require(props.get("ActiveState") == "active" and
                re.fullmatch(r"[1-9][0-9]*", props.get("MainPID", "")) is not None,
                "namespace command requires active positive PID")
        pid = int(props["MainPID"])
        return ["/usr/bin/nsenter", "--target", str(pid), "--net", "--"] + list(argv)

    def readiness(self, timeout):
        code = ("import importlib.util,json; s=importlib.util.spec_from_file_location('operator'," +
                repr(str(self.stage / "scoped_release_operator.py")) +
                ");m=importlib.util.module_from_spec(s);s.loader.exec_module(m);print(json.dumps(m.probe_http(30,fixture_port=" +
                repr(self.fixture_port) + ")))")
        result = json.loads(command(self.namespace_command(["/usr/bin/python3", "-I", "-B", "-c", code]), timeout=35))
        self.prove_exclusion()
        return result

    def release(self):
        assert_service_target(self, load_json(self.stage / "binding.json"))
        if self.phase == "abort":
            abort_admission(self.stage, self, live_state=self.dropin.exists())
        # Unlink is not completion: ExecStopPost may resume after any command.
        if self.dropin.exists():
            require(read_at(self.dropin.parent, self.dropin.name)[0] == self.content, "foreign containment drop-in")
            self.stop()
            if self.phase == "abort":
                abort_admission(self.stage, self, live_state=True)
                abort_observation(self.stage, self)  # Final blocking stop completed; live pre-unlink gate.
            self.dropin.unlink()
        else:
            props = self.properties()
            if props.get("ActiveState") == "active":
                pid = int(props["MainPID"])
                if os.stat(f"/proc/{pid}/ns/net").st_ino != os.stat("/proc/1/ns/net").st_ino:
                    # Still isolated after unlink/start interruption; no LAN writers yet.
                    self.stop()
                # Already uncontained may have real users: never blindly restart it.
        command(["/usr/bin/systemctl", "daemon-reload"])
        props = self.properties()
        if props.get("ActiveState") in ("inactive", "failed"):
            self.stopped(props)
            command(["/usr/bin/systemctl", "start", self.unit])
        props = self.properties()
        require(props.get("ActiveState") == "active" and props.get("PrivateNetwork") == "no",
                "app did not resume uncontained after accepted phase")
        pid = int(props["MainPID"])
        host = os.stat("/proc/1/ns/net").st_ino
        members = self.cgroup_members(props)
        require(pid > 0 and pid in members and
                all(os.stat(f"/proc/{p}/ns/net").st_ino == host for p in members),
                "reopened app cgroup/network namespace mismatch")
        self.verify_reopened_source()
        code = ("import importlib.util,json; s=importlib.util.spec_from_file_location('operator'," +
                repr(str(self.stage / "scoped_release_operator.py")) +
                ");m=importlib.util.module_from_spec(s);s.loader.exec_module(m);print(json.dumps(m.probe_http(30,fixture_port=" +
                repr(self.fixture_port) + ")))")
        result = json.loads(command(["/usr/bin/nsenter", "--target", str(pid), "--net", "--",
                                     "/usr/bin/python3", "-I", "-B", "-c", code], timeout=35))
        require(result["status"] == 302 and result["redirects_followed"] is False and
                self.properties() == props, "reopened app HTTP/context changed")
        self.verify_reopened_source()
        # Deliberately no checkpoint/state comparison after LAN access is open.
        publish(self.stage / "reopened.json", {"properties": props, "http": result})

    def verify_reopened_source(self):
        if self.phase == "abort":
            abort_admission(self.stage, self)
            return
        binding = load_json(self.stage / "binding.json")
        run = load_json(self.stage / "run.json")
        require(run.get("outcome") in ("committed", "recovered") and
                run["binding_sha256"] == sha(read_at(self.stage, "binding.json")[0]),
                "reopening requires terminal bound transaction")
        predecessor = run["outcome"] == "recovered"
        assert_binding(self.stage, binding, predecessor=predecessor)
        if not predecessor:
            for entry in load_json(self.stage / "release-spec.json")["files"]:
                actual = record(*read_at(binding["destination"], entry["path"]))
                require(all(actual[k] == entry[k] for k in ("sha256", "uid", "gid", "mode")),
                        "reopened current runtime fingerprint mismatch")


def private_record(stage, name):
    data, info = read_at(stage, name)
    require(info.st_uid == 0 and info.st_gid == 0 and stat.S_IMODE(info.st_mode) == 0o600 and
            info.st_nlink == 1, "terminal record metadata mismatch")
    return json.loads(data), sha(data)


def immutable_record(path, value):
    primitives()._publish_new_path(Path(path), encoded(value) + b"\n", mode=0o600)


def require_final_origin(origin):
    """Terminal intent is permanent, including incomplete or conflicting records.

    Called under the target lock at launch and guarded execution. No absence of
    containment or service inactivity is used as a substitute for this predicate.
    """
    for name in ("request-abort.json", "abort-recovery-admission.json", "abort-recovery-progress.json",
                 "terminal-reopen-admission.json", "run-abort.json"):
        if os.path.lexists(Path(origin) / name):
            # Valid records deny; malformed/orphaned/replaced records also deny.
            try:
                private_record(origin, name)
            except (ValueError, OSError):
                raise OperatorError("terminal origin denied: invalid terminal record")
            raise OperatorError("terminal origin denied: persistent terminal intent")


def related_final_descriptor(origin, final):
    """Read exact private provenance; no transient-unit absence inference."""
    origin, final = safe_stage(origin), safe_stage(final)
    origin_record, final_record = directory_record(origin), directory_record(final)
    require(all(r["uid"] == r["gid"] == 0 and r["mode"] == "0700"
                for r in (origin_record, final_record)), "related final stage not root-private")
    request, request_hash = private_record(final, "request-final.json")
    binding, binding_hash = private_record(final, "binding.json")
    previous, previous_hash = private_record(origin, "binding.json")
    origin_request, origin_request_hash = private_record(origin, "request-rollback.json")
    require(request["stage"] == str(final) and request["phase"] == "final" and
            request["origin_stage"] == str(origin) and request["binding_sha256"] == binding_hash and
            request["operator_sha256"] == sha(Path(__file__).read_bytes()) ==
            sha(read_at(final, "scoped_release_operator.py")[0]) and
            origin_request["stage"] == str(origin) and origin_request["phase"] == "rollback" and
            origin_request["origin_stage"] is None and origin_request["binding_sha256"] == previous_hash and
            all(request[k] == origin_request[k] for k in
                ("operator_sha256", "acceptance_program", "acceptance_sha256")) and
            binding["source_identity"] == previous["source_identity"] and
            binding["spec_sha256"] == previous["spec_sha256"] and
            binding["destination"] == previous["destination"] and bound_target(binding) == bound_target(previous),
            "related final provenance mismatch")
    program = Path(request["acceptance_program"])
    require(sha(read_at(program.parent, program.name)[0]) == request["acceptance_sha256"],
            "related final caller changed")
    return {"stage": str(final), "stage_record": final_record, "origin_stage": str(origin),
            "origin_record": origin_record, "request_sha256": request_hash, "request": request,
            "origin_request_sha256": origin_request_hash, "binding_sha256": binding_hash,
            "origin_binding_sha256": previous_hash, "source_identity": binding["source_identity"],
            "spec_sha256": binding["spec_sha256"], "target": bound_target(binding),
            "destination": binding["destination"]}


def register_related_final(origin, final):
    # Caller holds the existing target lock, before any final run/checkpoint producer.
    value = related_final_descriptor(origin, final)
    name = "related-final-" + sha(str(final).encode())[:16] + ".json"
    path = Path(origin) / name
    if os.path.lexists(path):
        require(private_record(origin, name)[0] == value, "related final registration changed")
    else:
        immutable_record(path, value)


def require_no_related_final_transaction(origin):
    for path in sorted(Path(origin).glob("related-final-*.json")):
        require(re.fullmatch(r"related-final-[0-9a-f]{16}\.json", path.name),
                "unknown related final registration")
        value, _ = private_record(origin, path.name)
        final = safe_stage(value["stage"])
        require(path.name == "related-final-" + sha(str(final).encode())[:16] + ".json" and
                value == related_final_descriptor(origin, final), "related final registration mismatch")
        # Both are real producer artifacts; no synthetic NOT_STARTED journal.
        if os.path.lexists(final / "run.json"):
            run, _ = private_record(final, "run.json")
            require(run["binding_sha256"] == value["binding_sha256"] and
                    run["acceptance_phase"] == "final", "related final run identity mismatch")
            raise OperatorError("related final transaction authority denies original abort fallback")
        if os.path.lexists(final / "checkpoint.json"):
            private_record(final, "checkpoint.json")
            raise OperatorError("related final checkpoint denies original abort fallback")


def terminal_selection(stage, phase, digest):
    """Only authenticated already-recovered terminals; incomplete restoration refuses.

    This successor slice does not admit new restoration or advance original journals.
    """
    require(phase in ("initial", "rollback") and isinstance(digest, str) and
            re.fullmatch("[0-9a-f]{64}", digest), "invalid terminal selection")
    binding, binding_hash = private_record(stage, "binding.json")
    initial, initial_hash = private_record(stage, "run.json")
    initial_request, initial_request_hash = private_record(stage, "request-initial.json")
    require(initial_request["stage"] == str(stage) and initial_request["phase"] == "initial" and
            initial_request["origin_stage"] is None and
            initial_request["binding_sha256"] == binding_hash and
            initial["binding_sha256"] == binding_hash and initial.get("keep_containment") is True,
            "initial terminal provenance mismatch")
    selected, selected_hash = (initial, initial_hash) if phase == "initial" else private_record(stage, "run-rollback.json")
    require(selected_hash == digest and selected.get("outcome") == "recovered" and
            selected.get("keep_containment") is True and selected["binding_sha256"] == binding_hash,
            "terminal must be authentic contained recovered predecessor; pending recovery unsupported")
    requests = {"initial": initial_request_hash}
    proofs = {}
    if phase == "rollback":
        require(initial.get("outcome") == "committed" and selected["origin_run_sha256"] == initial_hash and
                selected.get("acceptance_phase") == "rollback" and
                selected["checkpoint_sha256"] == initial["checkpoint_sha256"], "rollback origin mismatch")
        previous, previous_hash = private_record(stage, "request-rollback.json")
        require(previous["stage"] == str(stage) and previous["phase"] == "rollback" and
                previous["origin_stage"] is None and previous["binding_sha256"] == binding_hash,
                "rollback request mismatch")
        requests["rollback"] = previous_hash
        for role, run in (("initial", initial), ("rollback", selected)):
            if role == "rollback" and selected.get("rollback_accepted") is not True:
                continue  # Recovery is explicitly NOT release gate acceptance.
            validate_acceptance(run.get("caller"), binding, run)
            proof_hash = private_record(stage, "caller-proof-" + role + ".json")[1]
            require(run["caller"]["evidence_sha256"] == proof_hash, "terminal caller proof mismatch")
            proofs[role] = proof_hash
    checkpoint_hash = selected.get("checkpoint_sha256")
    unadmitted_checkpoint_hash = None
    if checkpoint_hash:
        require(private_record(stage, "checkpoint.json")[1] == checkpoint_hash, "terminal checkpoint link mismatch")
        verify_checkpoint(stage)
    else:
        require(phase == "initial" and selected.get("mutation_admitted") is False,
                "admitted mutation cannot authorize no-checkpoint abort")
        if os.path.lexists(Path(stage) / "checkpoint.json"):
            # Bind the artifact's identity, never its snapshot as recovery authority.
            unadmitted_checkpoint_hash = private_record(stage, "checkpoint.json")[1]
    return {"terminal_phase": phase, "terminal_sha256": selected_hash, "initial_sha256": initial_hash,
            "binding_sha256": binding_hash, "spec_sha256": binding["spec_sha256"],
            "source_identity": binding["source_identity"], "checkpoint_sha256": checkpoint_hash,
            "phase_requests": requests, "proofs": proofs, "target": bound_target(binding),
            "destination": binding["destination"], "source_role": "predecessor",
            "unadmitted_checkpoint_sha256": unadmitted_checkpoint_hash}


def abort_selection(stage, phase, digest):
    """Closed original selector; terminal path stays unchanged and simplest."""
    name = "run.json" if phase == "initial" else "run-rollback.json"
    run, actual = private_record(stage, name)
    if run.get("outcome") == "recovered":
        return terminal_selection(stage, phase, digest)
    armed_candidate = phase == "rollback" and run.get("phase") == "rollback-armed" and run.get("mutation_admitted") is False
    require(phase in ("initial", "rollback") and actual == digest and
            run.get("outcome") == "armed" and (run.get("mutation_admitted") is True or armed_candidate) and
            run.get("keep_containment") is True and run.get("acceptance_phase") == phase,
            "unsupported abort recovery origin")
    allowed = ("mutating", "candidate-started", "recovering-source", "recovering-state", "predecessor-verified") if phase == "initial" else (
        "rollback-armed", "rollback-mutating", "recovering-source", "recovering-state", "predecessor-verified")
    require(run.get("phase") in allowed, "unknown original recovery phase")
    binding, binding_hash = private_record(stage, "binding.json")
    initial, initial_hash = private_record(stage, "run.json")
    require(run["binding_sha256"] == initial["binding_sha256"] == binding_hash and
            initial.get("keep_containment") is True, "recovery binding mismatch")
    requests = {}
    proofs = {}
    for role in (("initial",) if phase == "initial" else ("initial", "rollback")):
        previous, previous_hash = private_record(stage, "request-" + role + ".json")
        require(previous["stage"] == str(stage) and previous["phase"] == role and
                previous["origin_stage"] is None and previous["binding_sha256"] == binding_hash,
                "recovery request mismatch")
        requests[role] = previous_hash
    if phase == "rollback":
        require(initial.get("outcome") == "committed" and run["origin_run_sha256"] == initial_hash and
                run["checkpoint_sha256"] == initial["checkpoint_sha256"] and
                run.get("journal_name") == name and not run.get("rollback_accepted"), "recovery rollback origin mismatch")
        validate_acceptance(initial.get("caller"), binding, initial)
        proofs["initial"] = private_record(stage, "caller-proof-initial.json")[1]
        require(initial["caller"]["evidence_sha256"] == proofs["initial"], "recovery initial proof mismatch")
    checkpoint_hash = private_record(stage, "checkpoint.json")[1]
    require(run["checkpoint_sha256"] == checkpoint_hash, "recovery checkpoint mismatch")
    verify_checkpoint(stage)
    assert_binding(stage, binding)
    if armed_candidate:
        primitives().verify_release(*bundle_args(stage, binding), mode="deploy")
        state = {r: tree_snapshot(Path(binding["destination"]) / r) for r in MUTABLE_ROOTS}
        require(sha(encoded(state)) == initial["state_sha256"] and state == verify_checkpoint(stage),
                "rollback armed candidate/state drift")
    return {"terminal_phase": phase, "terminal_sha256": digest, "initial_sha256": initial_hash,
            "binding_sha256": binding_hash, "spec_sha256": binding["spec_sha256"],
            "source_identity": binding["source_identity"], "checkpoint_sha256": checkpoint_hash,
            "phase_requests": requests, "proofs": proofs, "target": bound_target(binding),
            "destination": binding["destination"], "source_role": "predecessor",
            "recovery_kind": "armed-candidate" if armed_candidate else "original", "starting_run": run,
            "starting_source": {p: record(*r) if (r := read_at(binding["destination"], p, missing=True)) else {"absent": True}
                                for p in binding["predecessor"]},
            "starting_state": {r: tree_snapshot(Path(binding["destination"]) / r) for r in MUTABLE_ROOTS}}


def recovery_successor(stage, selection, *, journal_record=None):
    """Validate actual producer advancement, including journal-before-progress crash."""
    name = "run.json" if selection["terminal_phase"] == "initial" else "run-rollback.json"
    current, digest = private_record(stage, name) if journal_record is None else (
        journal_record, sha(encoded(journal_record) + b"\n"))
    start = selection["starting_run"]
    require(sha(encoded(start) + b"\n") == selection["terminal_sha256"], "starting journal digest mismatch")
    if selection["recovery_kind"] == "armed-candidate":
        require(digest == selection["terminal_sha256"], "original rollback-armed record changed")
        return current, digest
    if digest == selection["terminal_sha256"]:
        return current, digest
    require((Path(stage) / "abort-recovery-admission.json").exists(), "journal advanced without recovery admission")
    require({k: v for k, v in current.items() if k not in ("phase", "outcome", "rollback_http")} ==
            {k: v for k, v in start.items() if k not in ("phase", "outcome", "rollback_http")},
            "unknown recovery journal replacement")
    require(current.get("phase") in ("recovering-source", "recovering-state", "predecessor-verified", "recovered") and
            current.get("outcome") == ("recovered" if current["phase"] == "recovered" else "armed"),
            "unknown recovery transition")
    if current["phase"] == "recovered":
        ready = current.get("rollback_http", {})
        require(ready.get("status") == 302 and ready.get("redirects_followed") is False,
                "recovered successor readiness mismatch")
    return current, digest


def validate_recovery_selection(stage, selection):
    binding, binding_hash = private_record(stage, "binding.json")
    require(binding_hash == selection["binding_sha256"] and binding["spec_sha256"] == selection["spec_sha256"] and
            binding["source_identity"] == selection["source_identity"] and
            bound_target(binding) == selection["target"] and binding["destination"] == selection["destination"],
            "recovery selection identity changed")
    assert_binding(stage, binding)
    if selection["terminal_phase"] == "rollback":
        require(private_record(stage, "run.json")[1] == selection["initial_sha256"], "successful initial changed")
    for role, digest in selection["phase_requests"].items():
        require(private_record(stage, "request-" + role + ".json")[1] == digest, "recovery request changed")
    for role, digest in selection["proofs"].items():
        require(private_record(stage, "caller-proof-" + role + ".json")[1] == digest, "successful proof changed")
    require(private_record(stage, "checkpoint.json")[1] == selection["checkpoint_sha256"], "recovery checkpoint changed")
    verify_checkpoint(stage)
    return recovery_successor(stage, selection)


def abort_recovery(stage, services, request, request_hash, observation):
    selection = request["selection"]
    binding = load_json(Path(stage) / "binding.json")
    current, digest = validate_recovery_selection(stage, selection)
    path = Path(stage) / "abort-recovery-admission.json"
    if not path.exists():
        source = {p: record(*r) if (r := read_at(binding["destination"], p, missing=True)) else {"absent": True}
                  for p in binding["predecessor"]}
        require(source == selection["starting_source"] and
                {r: tree_snapshot(Path(binding["destination"]) / r) for r in MUTABLE_ROOTS} == selection["starting_state"],
                "recovery origin drift before admission")
        immutable_record(path, {"request_sha256": request_hash, "starting_sha256": selection["terminal_sha256"],
                               "selection": selection, "dropin": request["dropin"], "observation": observation})
    admission, admission_hash = private_record(stage, path.name)
    require({k: v for k, v in admission.items() if k != "observation"} ==
            {"request_sha256": request_hash, "starting_sha256": selection["terminal_sha256"],
                          "selection": selection, "dropin": request["dropin"]}, "recovery admission mismatch")
    validate_abort_observation(admission["observation"], stage)
    observation = abort_observation(stage, services)  # Live, not persisted absence.
    progress_path = Path(stage) / "abort-recovery-progress.json"
    prior = private_record(stage, progress_path.name)[0] if progress_path.exists() else None
    if prior:
        require(prior["request_sha256"] == request_hash and prior["admission_sha256"] == admission_hash and
                prior["starting_sha256"] == selection["terminal_sha256"], "recovery progress identity mismatch")
    if selection["recovery_kind"] == "armed-candidate":
        # Original rollback mutation/acceptance flags remain immutable. The abort
        # admission alone authorizes this source-before-state recovery.
        for p, predecessor in binding["predecessor"].items():
            r = read_at(binding["destination"], p, missing=True)
            actual = record(*r) if r else {"absent": True}
            require(actual in (predecessor, selection["starting_source"][p]), "abort recovery source drift")
        require({r: tree_snapshot(Path(binding["destination"]) / r) for r in MUTABLE_ROOTS} ==
                verify_checkpoint(stage), "abort recovery state drift")
        rb = primitives()
        observation = abort_observation(stage, services)
        publish(progress_path, {"request_sha256": request_hash, "admission_sha256": admission_hash,
                               "starting_sha256": selection["terminal_sha256"],
                               "previous_sha256": sha(encoded(prior) + b"\n") if prior else admission_hash,
                               "actual_journal_sha256": digest, "phase": "recovering-source", "outcome": "armed",
                               "observation": observation})
        rb.recover_release(*bundle_args(stage, binding), service=rb._NoServiceControl(), initially_active=False)
        rb.verify_release(*bundle_args(stage, binding), mode="rollback")
        assert_binding(stage, binding, predecessor=True)
        prior = private_record(stage, progress_path.name)[0]
        observation = abort_observation(stage, services)
        publish(progress_path, {**prior, "previous_sha256": sha(encoded(prior) + b"\n"), "phase": "recovering-state",
                               "observation": observation})
        restore_state(stage, binding)
        assert_binding(stage, binding, predecessor=True)
        require({r: tree_snapshot(Path(binding["destination"]) / r) for r in MUTABLE_ROOTS} == verify_checkpoint(stage),
                "abort restored state mismatch")
    else:
        def mutation_boundary(phase):
            nonlocal observation
            # Reconsume the immutable exact request and authentic producer successor;
            # the previous progress may predate the just-published original journal.
            require(private_record(stage, "request-abort.json")[1] == request_hash,
                    "abort recovery request changed at mutation boundary")
            actual_journal, actual_digest = validate_recovery_selection(stage, selection)
            previous = private_record(stage, progress_path.name)[0] if progress_path.exists() else None
            if previous:
                require(previous["request_sha256"] == request_hash and
                        previous["admission_sha256"] == admission_hash and
                        previous["starting_sha256"] == selection["terminal_sha256"],
                        "recovery boundary progress identity mismatch")
            observation = abort_observation(stage, services)
            publish(progress_path, {"request_sha256": request_hash, "admission_sha256": admission_hash,
                                   "starting_sha256": selection["terminal_sha256"],
                                   "previous_sha256": sha(encoded(previous) + b"\n") if previous else admission_hash,
                                   "actual_journal_sha256": actual_digest, "actual_journal": actual_journal,
                                   "phase": phase, "outcome": "armed",
                                   "observation": observation})
        recover_locked(stage, services, current, binding, mutation_boundary=mutation_boundary)
    current, digest = validate_recovery_selection(stage, selection)
    require(current.get("outcome") == "recovered" or selection["recovery_kind"] == "armed-candidate", "original recovery incomplete")
    prior = private_record(stage, progress_path.name)[0] if progress_path.exists() else None
    publish(progress_path, {"request_sha256": request_hash, "admission_sha256": admission_hash,
                           "starting_sha256": selection["terminal_sha256"], "previous_sha256":
                           sha(encoded(prior) + b"\n") if prior else admission_hash,
                           "actual_journal_sha256": digest, "phase": "recovered", "outcome": "recovered",
                           "observation": observation})
    return digest


def abort_request(stage):
    require_no_related_final_transaction(stage)
    request, digest = private_record(stage, "request-abort.json")
    require(request.get("final_origin_denied") is True and isinstance(request.get("drainage"), dict) and
            isinstance(request["drainage"].get("units"), list), "terminal intent/drainage record mismatch")
    require(request["stage"] == str(stage) and request["phase"] == "abort" and
            request["origin_stage"] is None and request["stage_record"] == directory_record(stage) and
            request["operator_sha256"] == sha(Path(__file__).read_bytes()) ==
            sha(read_at(stage, "scoped_release_operator.py")[0]), "abort stage/operator identity mismatch")
    program = Path(request["acceptance_program"])
    require(sha(read_at(program.parent, program.name)[0]) == request["acceptance_sha256"], "abort caller identity mismatch")
    if request["selection"].get("recovery_kind"):
        validate_recovery_selection(stage, request["selection"])
        selection = request["selection"]
    else:
        selection = terminal_selection(stage, request["selection"]["terminal_phase"], request["selection"]["terminal_sha256"])
    require(selection == request["selection"], "abort selection changed")
    for phase in selection["phase_requests"]:
        previous = private_record(stage, "request-" + phase + ".json")[0]
        require(previous["operator_sha256"] == request["operator_sha256"] and
                previous["acceptance_program"] == request["acceptance_program"] and
                previous["acceptance_sha256"] == request["acceptance_sha256"], "abort phase pins mismatch")
    if (Path(stage) / "abort-recovery-progress.json").exists():
        progress, _ = private_record(stage, "abort-recovery-progress.json")
        admission, admission_hash = private_record(stage, "abort-recovery-admission.json")
        require(progress["request_sha256"] == admission["request_sha256"] == digest and
                progress["admission_sha256"] == admission_hash and admission["selection"] == selection and
                progress["starting_sha256"] == admission["starting_sha256"] == selection["terminal_sha256"] and
                progress["phase"] in ("recovering-source", "recovering-state", "recovered") and
                progress["outcome"] == ("recovered" if progress["phase"] == "recovered" else "armed") and
                re.fullmatch("[0-9a-f]{64}", progress["previous_sha256"]) and
                (progress["actual_journal_sha256"] == recovery_successor(stage, selection)[1] or
                 (selection["recovery_kind"] == "original" and
                  isinstance(progress.get("actual_journal"), dict) and
                  progress["actual_journal"]["phase"] == progress["phase"] and
                  progress["actual_journal_sha256"] == recovery_successor(
                      stage, selection, journal_record=progress["actual_journal"])[1])),
                "unknown abort recovery progress")
        validate_abort_observation(progress["observation"], stage)
    abort_completion(stage, request, digest)
    return request, digest


def abort_completion(stage, request, request_hash):
    """Existing completion is authority to do nothing, never evidence to replace."""
    if not os.path.lexists(Path(stage) / "run-abort.json"):
        return None  # Legitimate post-unlink interruption remains finish-only.
    completion, _ = private_record(stage, "run-abort.json")
    admission, admission_hash = private_record(stage, "terminal-reopen-admission.json")
    require(admission["request_sha256"] == request_hash and
            admission["selection"] == request["selection"] and admission["dropin"] == request["dropin"] and
            admission["source_role"] == request["selection"]["source_role"] == "predecessor" and
            admission["feature_acceptance"] is False and
            completion["request_sha256"] == request_hash and
            completion["admission_sha256"] == admission_hash and
            completion["outcome"] == "TERMINAL_PREDECESSOR_REOPENED" and
            completion["feature_acceptance"] is False, "conflicting abort completion")
    validate_abort_observation(admission["observation"], stage)
    validate_abort_observation(completion["observation"], stage)
    return completion


def related_guards_drained(stage, services, *, current=None):
    """Read-only global guard inventory; unknown live related workers refuse."""
    rows = command(["/usr/bin/systemctl", "list-units", "--all", "--plain", "--no-legend", "matrix-scoped-*.service"])
    units = []
    for row in rows.splitlines():
        unit = row.split()[0]
        require(re.fullmatch(r"matrix-scoped-[0-9a-f]{16}-(initial|rollback|final|abort|abort-resume)\.service", unit),
                "unknown related guard")
        if unit == current:
            continue  # Current invocation is independently authenticated before locks.
        props = dict(line.split("=", 1) for line in command([
            "/usr/bin/systemctl", "show", unit, "--property=ActiveState,MainPID,ControlGroup"
        ]).splitlines() if "=" in line)
        require({"ActiveState", "MainPID", "ControlGroup"} <= props.keys(),
                "related guard properties incomplete; drainage unproved")
        members = services.cgroup_members(props)
        require(props.get("ActiveState") in ("inactive", "failed") and props.get("MainPID") == "0" and
                not members, "related guard/writer not drained")
        units.append({"unit": unit, "properties": props, "cgroup_members": members, "drained": True})
    return {"units": units}


def validate_abort_observation(observation, stage):
    guard = observation["guard"]
    require(guard["phase"] == "abort" and guard["stage"] == str(stage) and
            guard["InvocationID"] and guard["ControlGroup"] and guard["pid"] > 0 and
            guard["ExecStart"] and guard["ExecStopPost"], "abort guard evidence mismatch")
    for unit in observation["related"]["units"]:
        require(unit["drained"] is True and unit["properties"]["ActiveState"] in ("inactive", "failed") and
                unit["properties"]["MainPID"] == "0" and unit["cgroup_members"] == [],
                "abort drainage evidence mismatch")


def abort_observation(stage, services):
    guard = assert_pid1_guard(stage, "abort", fixture_mode=services.target["mode"] == "fixture",
                             resume=getattr(services, "abort_resume", False))
    value = {"guard": guard, "related": related_guards_drained(stage, services, current=guard["unit"])}
    validate_abort_observation(value, stage)
    return value


def owned_abort_dropin(services):
    data, info = read_at(services.dropin.parent, services.dropin.name)
    require(data == services.content and info.st_uid == 0 and info.st_gid == 0 and
            stat.S_IMODE(info.st_mode) == 0o600 and info.st_nlink == 1, "abort drop-in ownership mismatch")
    return {"path": str(services.dropin), **record(data, info), "dev": info.st_dev, "ino": info.st_ino}


def abort_admission(stage, services, *, live_state=False):
    request, request_hash = abort_request(stage)
    admission, _ = private_record(stage, "terminal-reopen-admission.json")
    require(admission["request_sha256"] == request_hash and admission["selection"] == request["selection"] and
            admission["dropin"] == request["dropin"] and admission["source_role"] == "predecessor" and
            admission["feature_acceptance"] is False, "abort admission mismatch")
    binding = load_json(Path(stage) / "binding.json")
    assert_service_target(services, binding)
    assert_binding(stage, binding, predecessor=True)
    if live_state:
        require(owned_abort_dropin(services) == admission["dropin"], "abort owned drop-in changed")
        services.prove_exclusion()
        actual = {r: tree_snapshot(Path(binding["destination"]) / r) for r in MUTABLE_ROOTS}
        require(actual == admission["state"], "terminal state drift before unlink")
    return admission


def abort_main(stage, args):
    services = SystemServices(stage, fixture_mode=args.fixture_mode)
    services.phase = "abort"
    services.abort_resume = args.resume
    global_fd = os.open("/run/lock/matrix-scoped-release.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(global_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if args.command == "launch":
            require(args.origin_stage is None, "abort cannot have final origin")
            fd = locked(stage)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                drainage = related_guards_drained(stage, services)
                if args.resume:
                    require(not any((args.acceptance_program, args.acceptance_sha256,
                                     args.terminal_phase, args.terminal_sha256)), "resume consumes exact sealed request only")
                    abort_request(stage)
                else:
                    require(args.acceptance_program and args.acceptance_sha256 and args.terminal_phase and
                            args.terminal_sha256, "fresh abort requires exact terminal and caller pins")
                    require(not (stage / "request-abort.json").exists(), "fresh abort request required; use --resume")
                    program = Path(args.acceptance_program).absolute()
                    require(sha(read_at(program.parent, program.name)[0]) == args.acceptance_sha256,
                            "abort caller identity mismatch")
                    require(sha(read_at(stage, "scoped_release_operator.py")[0]) == sha(Path(__file__).read_bytes()),
                            "staged abort operator mismatch")
                    require_no_related_final_transaction(stage)
                    selection = abort_selection(stage, args.terminal_phase, args.terminal_sha256)
                    binding = load_json(stage / "binding.json")
                    assert_binding(stage, binding, predecessor=not selection.get("recovery_kind"))
                    immutable_record(stage / "request-abort.json", {
                        "stage": str(stage), "phase": "abort", "origin_stage": None,
                        "final_origin_denied": True,
                        "operator_sha256": sha(Path(__file__).read_bytes()), "stage_record": directory_record(stage),
                        "acceptance_program": str(program), "acceptance_sha256": args.acceptance_sha256,
                        "selection": selection, "dropin": owned_abort_dropin(services), "drainage": drainage})
                    abort_request(stage)
            finally:
                os.close(fd)
        else:
            abort_request(stage)
            unit = next(a.split("=", 1)[1] for a in supervisor_command(
                stage, "abort", fixture_mode=args.fixture_mode, resume=args.resume) if a.startswith("--unit=")) + ".service"
            related_guards_drained(stage, services, current=unit)
            terminal_abort(stage, services)
            print("TERMINAL_PREDECESSOR_REOPENED feature_acceptance=false; final origin denied")
            return 0
    finally:
        os.close(global_fd)
    # Child takes target/stage locks only after launch has dropped its own descriptors.
    subprocess.run(supervisor_command(stage, "abort", fixture_mode=args.fixture_mode, resume=args.resume), check=True)
    return 0


def terminal_abort(stage, services):
    """End a safely recovered attempt, never a feature acceptance/final origin."""
    fd = locked(stage)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        request, request_hash = abort_request(stage)
        if abort_completion(stage, request, request_hash) is not None:
            require(not services.dropin.exists(), "completed abort still contained")
            abort_admission(stage, services)  # Read-only source/identity, never old mutable state.
            return
        services.phase = "abort"
        observation = abort_observation(stage, services)
        if (Path(stage) / "terminal-reopen-admission.json").exists():
            abort_admission(stage, services)
        else:
            require(owned_abort_dropin(services) == request["dropin"], "abort containment changed")
            binding = load_json(Path(stage) / "binding.json")
            services.prove_exclusion()
            services.stop()
            services.prove_exclusion()
            if request["selection"].get("recovery_kind"):
                abort_recovery(stage, services, request, request_hash, observation)
                services.stop()
                services.prove_exclusion()
            assert_binding(stage, binding, predecessor=True)
            state = {r: tree_snapshot(Path(binding["destination"]) / r) for r in MUTABLE_ROOTS}
            if request["selection"]["checkpoint_sha256"]:
                require(state == verify_checkpoint(stage), "terminal state drift before admission")
            services.start()
            ready = services.readiness(30)
            require(ready["status"] == 302 and ready["redirects_followed"] is False, "terminal predecessor readiness failed")
            services.stop()
            services.prove_exclusion()
            assert_binding(stage, binding, predecessor=True)
            require(state == {r: tree_snapshot(Path(binding["destination"]) / r) for r in MUTABLE_ROOTS},
                    "terminal state changed during readiness")
            require(owned_abort_dropin(services) == request["dropin"], "abort drop-in replaced before admission")
            immutable_record(Path(stage) / "terminal-reopen-admission.json", {
                "request_sha256": request_hash, "selection": request["selection"], "dropin": request["dropin"],
                "state": state, "readiness": ready, "source_role": "predecessor", "feature_acceptance": False,
                "observation": abort_observation(stage, services)})
        observation = abort_observation(stage, services)
        services.release()
        publish(Path(stage) / "run-abort.json", {"request_sha256": request_hash,
                "admission_sha256": private_record(stage, "terminal-reopen-admission.json")[1],
                "outcome": "TERMINAL_PREDECESSOR_REOPENED", "feature_acceptance": False,
                "observation": observation})
    finally:
        os.close(fd)



def safe_stage(stage):
    stage = Path(stage).absolute()
    require(re.fullmatch(r"/[A-Za-z0-9_./-]+", str(stage)) is not None and ".." not in stage.parts,
            "stage is not a canonical systemd-safe path")
    return stage


def supervisor_command(stage, phase, *, fixture_mode=False, resume=False):
    stage = safe_stage(stage)
    require(phase in ("initial", "rollback", "final", "abort"), "unknown release phase")
    require(not resume or phase == "abort", "resume only supports abort")
    operator = stage / "scoped_release_operator.py"
    unit = "matrix-scoped-" + sha(str(stage).encode())[:16] + "-" + phase + ("-resume" if resume else "")
    recovery = " ".join(["/usr/bin/python3", "-I", "-B", str(operator), "recover", "--stage", str(stage), "--phase", phase])
    if resume:
        recovery += " --resume"
    if fixture_mode:
        recovery += " --fixture-mode"
    return ["/usr/bin/systemd-run", "--unit=" + unit, "--wait", "--service-type=exec",
            "--property=KillMode=control-group", "--property=RuntimeMaxSec=180",
            "--property=TimeoutStopSec=120", "--property=SendSIGKILL=yes",
            "--property=ExecStopPost=" + recovery,
            "/usr/bin/python3", "-I", "-B", str(operator), "recover" if resume else "execute", "--stage", str(stage), "--phase", phase] + (["--resume"] if resume else []) + (
                ["--fixture-mode"] if fixture_mode else [])


def run_supervised(stage, phase, *, fixture_mode=False):
    try:
        subprocess.run(supervisor_command(stage, phase, fixture_mode=fixture_mode), check=True)
    except BaseException as transport_error:
        retain_diagnostic(stage, phase, "launcher", "OPERATOR_LAUNCH", transport_error,
                          exit_code=getattr(transport_error, "returncode", None))
        # Child cause has priority; transport metadata never substitutes for it.
        try:
            custody = failure_custody(); context = diagnostic_context(stage, phase)
            for slot in ("primary", "caller", "recovery-inline", "recovery-stoppost", "launcher"):
                try:
                    value = custody.read(context, slot)
                except (ValueError, OSError, KeyError, TypeError):
                    print("MATRIX_FAILURE_CUSTODY_UNAVAILABLE slot=" + slot)
                else:
                    print("MATRIX_FAILURE_CUSTODY_READBACK=" + encoded(value).decode())
        except BaseException:
            print("MATRIX_FAILURE_CUSTODY_UNAVAILABLE; no acceptance")
        raise


def rollback_proof(stage, services, acceptance):
    """Independent guarded rollback; never reuse a snapshot after reopening LAN."""
    stage = Path(stage)
    fd = locked(stage)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        initial = load_json(stage / "run.json")
        require(initial.get("outcome") == "committed" and initial.get("keep_containment") is True,
                "rollback forbidden after user access reopened or initial acceptance missing")
        services.prove_exclusion()
        require(not (stage / "run-rollback.json").exists(), "rollback phase must be fresh")
        binding = load_json(stage / "binding.json")
        require(initial["binding_sha256"] == sha(read_at(stage, "binding.json")[0]), "initial binding mismatch")
        assert_service_target(services, binding)
        assert_binding(stage, binding)
        primitives().verify_release(*bundle_args(stage, binding), mode="deploy")
        run = {"binding_sha256": initial["binding_sha256"], "outcome": "armed", "keep_containment": True,
               "checkpoint_sha256": initial["checkpoint_sha256"], "mutation_admitted": False,
               "journal_name": "run-rollback.json", "origin_run_sha256": sha(read_at(stage, "run.json")[0]),
               "acceptance_phase": "rollback"}
        journal(stage, run, "rollback-armed")
        services.stop()
        services.prove_exclusion()
        state = {r: tree_snapshot(Path(binding["destination"]) / r) for r in MUTABLE_ROOTS}
        require(sha(encoded(state)) == initial["state_sha256"], "private state changed since contained initial acceptance")
        journal(stage, run, "rollback-mutating", mutation_admitted=True)
        recover_locked(stage, services, run, binding)
        evidence = acceptance({"binding_sha256": run["binding_sha256"], "source_identity": binding["source_identity"],
                               "spec_sha256": binding["spec_sha256"], "stage": str(stage)})
        validate_acceptance(evidence, binding, run)
        assert_binding(stage, binding, predecessor=True)
        require({r: tree_snapshot(Path(binding["destination"]) / r) for r in MUTABLE_ROOTS} == verify_checkpoint(stage),
                "predecessor state changed after restart")
        journal(stage, run, "rollback-accepted", caller=evidence, rollback_accepted=True)
        return run
    finally:
        os.close(fd)


def assert_pid1_guard(stage, phase, *, fixture_mode=False, resume=False):
    """Reject direct ExecStart/ExecStopPost invocation without actual PID1 binding."""
    require(os.environ.get("INVOCATION_ID"), "execution requires an actual PID1 invocation")
    argv = supervisor_command(stage, phase, fixture_mode=fixture_mode, resume=resume)
    unit = next(a.split("=", 1)[1] for a in argv if a.startswith("--unit=")) + ".service"
    props = dict(line.split("=", 1) for line in command([
        "/usr/bin/systemctl", "show", unit,
        "--property=InvocationID,KillMode,ExecStart,ExecStopPost,SendSIGKILL,Transient,ControlGroup,RuntimeMaxUSec,TimeoutStopUSec"
    ]).splitlines() if "=" in line)
    require(props.get("InvocationID") == os.environ["INVOCATION_ID"] and
            props.get("Transient") == "yes" and props.get("KillMode") == "control-group" and
            str(Path(stage) / "scoped_release_operator.py") in props.get("ExecStopPost", "") and
            "recover" in props.get("ExecStopPost", "") and
            props.get("RuntimeMaxUSec") == "3min" and props.get("TimeoutStopUSec") == "2min",
            "actual PID1 guard properties mismatch")
    if phase == "abort":
        expected_stop = next(a[len("--property=ExecStopPost="):] for a in argv if a.startswith("--property=ExecStopPost="))
        index = argv.index("/usr/bin/python3")
        expected_start = " ".join(argv[index:])
        require(props.get("SendSIGKILL") == "yes" and
                re.findall(r"argv\[\]=([^;]+);", props.get("ExecStart", "")) == [expected_start + " "] and
                re.findall(r"argv\[\]=([^;]+);", props.get("ExecStopPost", "")) == [expected_stop + " "],
                "exact abort PID1 guard command mismatch")
    own_groups = Path("/proc/self/cgroup").read_text().splitlines()
    require("0::" + props.get("ControlGroup", "invalid") in own_groups, "operator not inside assigned PID1 cgroup")
    return {**props, "unit": unit, "phase": phase, "stage": str(stage), "pid": os.getpid()}


def caller_acceptance(stage, services, phase):
    request = load_json(Path(stage) / ("request-" + phase + ".json"))
    program = Path(request["acceptance_program"])
    require(sha(read_at(program.parent, program.name)[0]) == request["acceptance_sha256"], "caller program identity mismatch")
    def accept(context):
        context = {**context, "phase": phase, "app_pid": int(services.properties()["MainPID"])}
        if services.target["mode"] == "fixture":
            context.update(acceptedScope="disposable-pid1-fixture-only", target=services.target)
        try:
            result = subprocess.run(services.namespace_command(["/usr/bin/python3", "-I", "-B", str(program)]),
                                    input=encoded(context), capture_output=True, timeout=60)
        except BaseException as caller_error:
            retain_diagnostic(stage, phase, "caller", "OPERATOR_CALLER", caller_error)
            raise
        if result.returncode != 0:
            try:
                custody = failure_custody(); pins = diagnostic_context(stage, phase)
                pins["app_pid"] = context["app_pid"]
                request_path = Path(stage) / ("caller-request-" + phase + ".json")
                if request_path.exists():
                    raw = read_at(stage, request_path.name)[0]; journey = json.loads(raw)
                    for key in ("stage", "phase", "app_pid", "binding_sha256", "source_identity", "spec_sha256"):
                        require(journey[key] == pins[key], "diagnostic journey identity mismatch")
                    pins.update(nonce=journey["nonce"], driver_sha256=journey["driver_sha256"], request_sha256=sha(raw))
                value = custody.read(pins, "target")
                require(value["exit_code"] == result.returncode, "diagnostic caller exit mismatch")
                custody.retain_value(value, "caller")
            except BaseException:
                print("MATRIX_TARGET_DIAGNOSIS_UNAVAILABLE; no acceptance", file=sys.stderr)
                retain_diagnostic(stage, phase, "caller", "OPERATOR_CALLER", OperatorError("diagnosis unavailable"),
                                  exit_code=result.returncode)
        require(result.returncode == 0, "required caller acceptance command failed (output withheld)")
        evidence = json.loads(result.stdout)
        # Caller emits only a metadata receipt; retained proof bytes must actually match it.
        proof = read_at(stage, "caller-proof-" + phase + ".json")[0]
        require(evidence["evidence_sha256"] == sha(proof), "caller evidence digest mismatch")
        return evidence
    return accept


def successor_source_matches(manifest, expected_identity):
    """Separate exact successor pin is part of the independently reviewed controls.

    This is export compatibility, not review/CI or production acceptance.
    """
    path = Path(__file__).with_name("successor-source-pin.json")
    if not path.exists():
        return False
    pin = load_json(path)
    return (set(pin) == {"generation", "head", "source_identity", "source_count"} and
            pin["generation"] == "r11b-prod-rectify" and
            type(pin["source_count"]) is int and pin["source_count"] >= 209 and
            pin["source_count"] == len(manifest["files"]) and
            re.fullmatch("[0-9a-f]{40}", pin["head"]) is not None and
            pin["head"] == manifest["head"] and
            pin["source_identity"] == manifest["source_identity"] == expected_identity)


def export_source(source, manifest, route, output, *, expected_identity=FROZEN_IDENTITY):
    """Verify 192/194 or exact reviewed source206 bytes/modes; transport scoped 17."""
    rb = primitives()
    files = manifest["files"]
    require((len(files) in (192, 194) or
             (len(files) == 206 and manifest["head"] == "9315292170e9ee14a28c2c3c228bc05e981de9e4" and
              expected_identity == "f954ddfa4714f641f092f66cd940302b5236a79067af7cbe5cc6ef0b7ede1fdd") or
             successor_source_matches(manifest, expected_identity)) and
            len({e["path"] for e in files}) == len(files) and
            sha(encoded({"head": manifest["head"], "files": files})) == manifest["source_identity"] == expected_identity,
            "full source identity mismatch")
    selected = set(RUNTIME_PATHS) | set(HELPER_HASHES)
    approved = {e["path"]: e["sha256"] for e in route["source_plan"]["runtime_delta"]}
    approved.update({e["path"]: e["sha256"] for e in route["source_plan"]["operational_package_entries"]})
    require(set(approved) == selected and all(approved[p] == h for p, h in HELPER_HASHES.items()), "export scope mismatch")
    payload = {}
    for entry in files:
        data, info = read_at(source, entry["path"])
        require(sha(data) == entry["sha256"] and len(data) == entry["size"] and
                oct(stat.S_IMODE(info.st_mode)) == entry["mode"], "full source closure mismatch")
        if entry["path"] in selected:
            require(sha(data) == approved[entry["path"]], "route export identity mismatch")
            payload[entry["path"]] = data
    require(set(payload) == selected, "full source missing selected files")
    output = Path(output)
    require(not output.exists(), "fresh export directory required")
    output.mkdir(mode=0o700)
    for relative, data in payload.items():
        target = output / "release" / relative
        target.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        rb._publish_new_path(target, data, mode=0o400)
    publish(output / "source-manifest.json", manifest)
    publish(output / "route.json", route)
    publish(output / "export.json", {"source_identity": expected_identity, "verified_entries": len(files),
                                     "payload_entries": 17, "payload": {p: sha(b) for p, b in payload.items()}})
    return output


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    export = sub.add_parser("export")
    for name in ("source", "manifest", "route", "output"):
        export.add_argument("--" + name, required=True)
    prepare = sub.add_parser("prepare")
    for name in ("source", "manifest", "route", "destination", "stage"):
        prepare.add_argument("--" + name, required=True)
    prepare.add_argument("--scoped", action="store_true")
    prepare.add_argument("--fixture-unit", help="explicit matrix-release-proof-*.service only")
    prepare.add_argument("--fixture-port", type=int, help="fixture-only port >1024, never 5000")
    for p in (export, prepare):
        p.add_argument("--reviewed-head", required=True)
        p.add_argument("--source-identity", required=True)
    preactivation = sub.add_parser("preactivation")
    preactivation.add_argument("--stage", required=True)
    preactivation.add_argument("--fixture-mode", action="store_true")
    for name in ("launch", "execute", "recover"):
        p = sub.add_parser(name)
        p.add_argument("--stage", required=True)
        p.add_argument("--fixture-mode", action="store_true")
        p.add_argument("--phase", choices=("initial", "rollback", "final", "abort"), required=True)
        p.add_argument("--resume", action="store_true")
        if name == "launch":
            p.add_argument("--acceptance-program")
            p.add_argument("--acceptance-sha256")
            p.add_argument("--origin-stage")
            p.add_argument("--terminal-phase", choices=("initial", "rollback"))
            p.add_argument("--terminal-sha256")
    args = parser.parse_args(argv)
    manifest = None
    if args.command in ("export", "prepare"):
        manifest = load_json(args.manifest)
        require(re.fullmatch("[0-9a-f]{40}", args.reviewed_head) and
                re.fullmatch("[0-9a-f]{64}", args.source_identity) and
                manifest["head"] == args.reviewed_head and
                manifest["source_identity"] == args.source_identity,
                "explicit reviewed HEAD/source identity mismatch")
        if args.command == "export" or not args.scoped:
            require(command(["git", "-C", str(Path(args.source).absolute()), "rev-parse", "HEAD"]).strip()
                    == args.reviewed_head, "actual source HEAD mismatch")
    if args.command == "export":
        export_source(args.source, manifest, load_json(args.route), args.output,
                      expected_identity=args.source_identity)
        return 0
    if args.command == "prepare":
        prepare_package(args.source, manifest, load_json(args.route), args.destination, args.stage,
                        scoped=args.scoped, expected_identity=args.source_identity,
                        fixture_unit=args.fixture_unit, fixture_port=args.fixture_port)
        return 0
    stage = safe_stage(args.stage)
    if args.command in ("execute", "recover"):
        if args.phase == "abort":
            assert_pid1_guard(stage, args.phase, fixture_mode=args.fixture_mode, resume=args.resume)
        else:
            require(not args.resume, "resume only supports abort")
            assert_pid1_guard(stage, args.phase, fixture_mode=args.fixture_mode)  # Before opening stage or stopping anything.
    require(os.geteuid() == 0, "target-local operator requires existing sudo -n root")
    require(directory_record(stage)["uid"] == 0 and directory_record(stage)["mode"] == "0700", "stage not root-private")
    if args.command == "preactivation":
        assert_binding(stage, load_json(stage / "binding.json"), predecessor=True)
        SystemServices(stage, fixture_mode=args.fixture_mode).validate()
        # Read-only admission: not a namespace or process-loss proof.
        print("PREACTIVATION identity/tools checked; live containment/PID1 proof still required")
        return 0
    if args.phase == "abort":
        return abort_main(stage, args)
    require(not args.resume, "resume only supports abort")
    if args.command == "launch":
        require(args.acceptance_program and args.acceptance_sha256 and not args.terminal_phase and
                not args.terminal_sha256, "fresh release phase requires caller pins and no terminal selectors")
        SystemServices(stage, args.origin_stage, fixture_mode=args.fixture_mode)
        require(sha(read_at(stage, "scoped_release_operator.py")[0]) == sha(Path(__file__).read_bytes()), "staged operator mismatch")
        program = Path(args.acceptance_program).absolute()
        require(sha(read_at(program.parent, program.name)[0]) == args.acceptance_sha256, "caller program mismatch")
        origin = safe_stage(args.origin_stage) if args.origin_stage else None
        require((args.phase == "final") == (origin is not None), "final phase requires accepted rollback origin only")
        request = {"stage": str(stage), "phase": args.phase, "origin_stage": str(origin) if origin else None,
                   "acceptance_program": str(program), "acceptance_sha256": args.acceptance_sha256,
                   "operator_sha256": sha(Path(__file__).read_bytes()), "binding_sha256": sha(read_at(stage, "binding.json")[0])}
        global_fd = os.open("/run/lock/matrix-scoped-release.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(global_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if origin:
                require_final_origin(origin)
            require(not (stage / ("request-" + args.phase + ".json")).exists(), "fresh phase request required")
            publish(stage / ("request-" + args.phase + ".json"), request)
            if origin:
                register_related_final(origin, stage)
        finally:
            os.close(global_fd)
        run_supervised(stage, args.phase, fixture_mode=args.fixture_mode)
        return 0
    request = load_json(stage / ("request-" + args.phase + ".json"))
    require(request["operator_sha256"] == sha(Path(__file__).read_bytes()) and
            request["binding_sha256"] == sha(read_at(stage, "binding.json")[0]), "phase request identity mismatch")
    origin = Path(request["origin_stage"]) if request["origin_stage"] else None
    services = SystemServices(stage, origin, fixture_mode=args.fixture_mode)
    services.phase = args.phase
    global_fd = os.open("/run/lock/matrix-scoped-release.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(global_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if origin:
            require_final_origin(origin)
            register_related_final(origin, stage)
        if args.command == "recover":
            recover(stage, services, args.phase)
            return 0
        if origin:
            previous = load_json(origin / "run-rollback.json")
            require(previous.get("rollback_accepted") is True and previous.get("keep_containment") is True,
                    "final phase requires independently accepted contained rollback")
            require(load_json(origin / "binding.json")["source_identity"] == load_json(stage / "binding.json")["source_identity"] and
                    load_json(origin / "binding.json")["spec_sha256"] == load_json(stage / "binding.json")["spec_sha256"],
                    "final redeploy must use same exact package/spec")
            services.prove_exclusion()
        if args.phase == "rollback":
            try:
                accept = caller_acceptance(stage, services, args.phase)
                rollback_proof(stage, services, accept)
            except BaseException as primary:
                # Persist before PID1 ExecStopPost recovery or launcher readback.
                # Diagnosis is best-effort; it cannot replace the initiating error
                # or interfere with the existing independently guarded recovery.
                retain_diagnostic(stage, args.phase, "primary", "OPERATOR_EXECUTE", primary)
                raise
        else:
            accept = caller_acceptance(stage, services, args.phase)
            execute(stage, services, accept, keep_containment=args.phase == "initial")
        print("PHASE recorded; inspect retained private metadata journal")
        return 0
    finally:
        os.close(global_fd)


def fixture_exception_diagnostic(exc, argv):
    """Bounded causal identity only, for an explicitly bound fixture execution.

    Not admission/evidence authority: main still validates every live guard and
    schema. Never print exception text, source lines, data, locals or environment.
    """
    try:
        require(argv and argv[0] in ("execute", "recover") and argv.count("--fixture-mode") == 1 and
                argv.count("--stage") == 1, "diagnostic is fixture execution only")
        stage = safe_stage(argv[argv.index("--stage") + 1])
        require(directory_record(stage)["uid"] == 0 and directory_record(stage)["mode"] == "0700",
                "diagnostic stage not private")
        SystemServices(stage, fixture_mode=True)  # Full bound target/schema, no services invoked.
    except (OperatorError, OSError, KeyError, IndexError, json.JSONDecodeError):
        return None
    frames = []
    tb = exc.__traceback__
    while tb:
        frames.append({"function": tb.tb_frame.f_code.co_name[:80], "line": tb.tb_lineno,
                       "file": Path(tb.tb_frame.f_code.co_filename).name[:80]})
        tb = tb.tb_next
    return {"type": type(exc).__name__[:80], "trace": frames[-8:]}


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OperatorError, OSError, subprocess.SubprocessError, KeyError, json.JSONDecodeError) as exc:
        diagnostic = fixture_exception_diagnostic(exc, sys.argv[1:])
        if diagnostic is not None:
            print("fixture_exception=" + json.dumps(diagnostic, separators=(",", ":")), file=sys.stderr)
        print("scoped operator refused/failed; retain stage and recovery journal", file=sys.stderr)
        raise SystemExit(2)
