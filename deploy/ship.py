#!/usr/bin/env python3
"""One-command Matrix production delivery.

    python3 deploy/ship.py             # dry run: CI gate + diff vs production, no changes
    python3 deploy/ship.py --apply     # deploy origin/main
    python3 deploy/ship.py --rollback STAGE   # restore the code backed up by an earlier --apply

Gates (all must hold, otherwise nothing on the device changes):
  * the commit is origin/main and its newest GitLab pipeline on ref main succeeded;
  * production serves exactly one identifiable version and the commit's APPLICATION_VERSION
    differs from it;
  * the only differing files are runtime files (no state, config, media, secrets): a changed
    requirements.txt or installed matrix-web.service unit, or a runtime file present on
    production but deleted on main, refuses because this tool cannot apply those.

The device-side installer (deploy/ship_remote.py) stops matrix-web, backs up the replaced
code and an opaque copy of data/ + resources/media, publishes each file atomically with
the production owner/mode, restarts, and restores the old code by itself if the service
fails to come back. ship.py then requires the public pairing page to show the new version
(or the check itself fails) and automatically rolls back if it does not; a rollback only
counts when the previous version is served again. User state, config, drafts, uploads and the
pairing PIN are never written.
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tarfile
import time
from typing import Any
import urllib.request

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
import locate_device  # noqa: E402  (deploy/locate_device.py, roadmap 1.10f)

# 1.10f: the device is found by its pinned SSH host key, never by a fixed IP.
# MATRIX_PROD_HOST is only a first guess (user@address); HOST/PUBLIC_URL are set by locate_target().
HOST_HINT = os.environ.get('MATRIX_PROD_HOST')
PROD_USER = os.environ.get('MATRIX_PROD_USER', 'admin')
PROD_PORT = int(os.environ.get('MATRIX_PROD_PORT', '5000'))
KNOWN_HOSTS = Path(os.environ.get('MATRIX_PROD_KNOWN_HOSTS', '/root/.config/matrix/device-known-hosts'))
HOST: str | None = None
PUBLIC_URL: str | None = None
HOST_KEY_OPTIONS: list[str] = []
SSH_KEY = os.environ.get('MATRIX_PROD_SSH_KEY', '/root/.ssh/id_ed25519_matrix')
GITLAB_API = os.environ.get('MATRIX_GITLAB_API', 'http://gitlab.home.lan/api/v4/projects/2')
TOKEN_FILE = Path(os.environ.get('MATRIX_GITLAB_TOKEN_FILE', '/root/.config/gitlab/token'))
RESULTS = Path(os.environ.get('MATRIX_SHIP_RESULTS', '/root/.local/state/matrix-ship'))
REMOTE_ROOT = '/home/admin/matrix'

# Runtime code the service imports or serves. Everything else on the device (data/,
# resources/media, config, venv, backups, tests, docs) is never deployed by this tool.
RUNTIME_TOP_LEVEL = re.compile(r'^[A-Za-z0-9_]+\.py$')
RUNTIME_DIRS = ('app/', 'modules/', 'setup/')
NEVER = ('data/', 'resources/', 'venv/', '.venv', 'backups/', '.git/')
VERSION_RE = re.compile(r'APPLICATION_VERSION\s*=\s*"([^"]+)"')
# Non-runtime files whose change needs work this tool does not do (pip install, unit reload).
DEPENDENCY_FILES = ('requirements.txt',)
UNIT_KEY = '@systemd/matrix-web.service'          # installed unit, compared with deploy/matrix-web.service
UNIT_SOURCE = 'deploy/matrix-web.service'


class ShipError(RuntimeError):
    pass


def is_runtime(path: str) -> bool:
    if path.startswith(NEVER) or '__pycache__' in path:
        return False
    return bool(RUNTIME_TOP_LEVEL.match(path)) or path.startswith(RUNTIME_DIRS)


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def version_of(webapp_source: bytes) -> str:
    match = VERSION_RE.search(webapp_source.decode('utf-8'))
    if not match:
        raise ShipError('APPLICATION_VERSION not found in webapp.py')
    return match.group(1)


def plan_files(main_files: dict[str, str], prod_files: dict[str, str]) -> list[str]:
    """Runtime paths whose bytes differ from production (or are new)."""
    return sorted(p for p, h in main_files.items() if is_runtime(p) and prod_files.get(p) != h)


def unsupported_changes(main_files: dict[str, str], prod_files: dict[str, str]) -> list[str]:
    """Differences this installer cannot apply; any entry refuses the deploy.

    main_files is the complete tree of the commit (all paths), prod_files the production
    inventory (state directories excluded) plus UNIT_KEY for the installed unit.
    """
    problems = []
    for name in DEPENDENCY_FILES:
        if main_files.get(name) != prod_files.get(name):
            problems.append(f'{name} differs from production (dependency install not supported)')
    if UNIT_SOURCE in main_files and main_files[UNIT_SOURCE] != prod_files.get(UNIT_KEY):
        problems.append(f'{UNIT_SOURCE} differs from the installed unit (unit install not supported)')
    stale = sorted(p for p in prod_files if is_runtime(p) and p not in main_files)
    if stale:
        problems.append('runtime file(s) on production are not in main (deletion not supported): '
                        + ', '.join(stale))
    return problems


def public_versions(html: bytes) -> list[str]:
    return sorted({v.decode() for v in re.findall(rb'\d+\.\d+\.\d+-preview', html)})


def run(argv, **kw):
    return subprocess.run(argv, capture_output=True, check=True, **kw).stdout


def git(*args) -> bytes:
    return run(['git', '-C', str(REPO), *args])


def locate_target() -> dict[str, str]:
    """Find the device by pinned host key; every later ssh accepts only that key."""
    global HOST, PUBLIC_URL, HOST_KEY_OPTIONS
    try:
        found = locate_device.locate(
            KNOWN_HOSTS, user=PROD_USER, hint=HOST_HINT, memory=RESULTS / 'device-address.json',
            scan=locate_device.parse_subnets(os.environ.get('MATRIX_PROD_SUBNETS')),
        )
    except (locate_device.LocateError, ValueError) as error:
        raise ShipError(str(error)) from error
    HOST = found['target']
    PUBLIC_URL = f"http://{found['address']}:{PROD_PORT}/pair"
    HOST_KEY_OPTIONS = locate_device.ssh_options(KNOWN_HOSTS, found['alias'])
    return found


def ssh_argv() -> list[str]:
    if not HOST or not HOST_KEY_OPTIONS:
        raise ShipError('device not located; refusing to connect')
    return ['ssh', '-F', '/dev/null', '-i', SSH_KEY, '-o', 'IdentitiesOnly=yes', '-o', 'BatchMode=yes',
            *HOST_KEY_OPTIONS, '-o', 'ConnectTimeout=10', '-o', 'ServerAliveInterval=10',
            '-o', 'ServerAliveCountMax=2', '--', HOST]


def remote(command: str, data: bytes | None = None, timeout: int = 120) -> bytes:
    done = subprocess.run(ssh_argv() + [command], input=data, capture_output=True, timeout=timeout,
                          stdin=None if data is not None else subprocess.DEVNULL)
    if done.returncode != 0:
        raise ShipError(f'remote command failed ({done.returncode}): {done.stderr.decode(errors="replace")[-400:]}')
    return done.stdout


def remote_python(code: str, data: bytes | None = None, timeout: int = 120) -> bytes:
    return remote('sudo -n /usr/bin/python3 -I -B -c ' + shlex.quote(code), data, timeout)


def gitlab(path: str):
    token = TOKEN_FILE.read_text(encoding='utf-8').strip()
    request = urllib.request.Request(GITLAB_API + path, headers={'PRIVATE-TOKEN': token})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request, timeout=20) as response:
        return json.load(response)


def ci_status(sha: str) -> tuple[str, int | None]:
    """Status of the newest pipeline for this SHA on ref main (other refs never count)."""
    pipelines = gitlab(f'/pipelines?sha={sha}&ref=main&order_by=id&sort=desc&per_page=20')
    pipelines = [p for p in pipelines if p.get('ref') == 'main' and p.get('sha') == sha]
    if not pipelines:
        return 'missing', None
    newest = max(pipelines, key=lambda p: p['id'])
    return newest['status'], newest['id']


def fetch_public() -> bytes:
    if not PUBLIC_URL:
        raise ShipError('device not located; no public URL')
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(PUBLIC_URL, timeout=10) as response:
        return response.read()


PROD_INVENTORY = (
    "import hashlib,json,pathlib;r=pathlib.Path(%r);o={}\n"
    "for p in r.rglob('*'):\n"
    " rel=p.relative_to(r).as_posix()\n"
    " if rel.startswith(('data/','resources/','venv/','.venv','backups/','.git/')) or '__pycache__' in rel:continue\n"
    " if p.is_file() and not p.is_symlink():o[rel]=hashlib.sha256(p.read_bytes()).hexdigest()\n"
    "u=pathlib.Path('/etc/systemd/system/matrix-web.service')\n"
    "if u.is_file():o[%r]=hashlib.sha256(u.read_bytes()).hexdigest()\n"
    "print(json.dumps(o))"
) % (REMOTE_ROOT, UNIT_KEY)


def build_plan(sha: str):
    """Returns (runtime files to publish, candidate version, unsupported differences)."""
    tree = git('ls-tree', '-r', '--name-only', sha).decode().split('\n')
    relevant = [p for p in tree if p and (is_runtime(p) or p in DEPENDENCY_FILES or p == UNIT_SOURCE)]
    main = {p: sha256(git('show', f'{sha}:{p}')) for p in relevant}
    tracked = {p: '' for p in tree if p}
    tracked.update(main)
    prod = json.loads(remote_python(PROD_INVENTORY))
    files = plan_files(main, prod)
    version = version_of(git('show', f'{sha}:webapp.py'))
    return files, version, unsupported_changes(tracked, prod)


def write_result(result: dict[str, Any]) -> Path:
    RESULTS.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = RESULTS / f"{result['started_utc'].replace(':', '')}-{result['mode']}.json"
    path.write_text(json.dumps(result, indent=2) + '\n', encoding='utf-8')
    return path


def stage_and_run(files: list[str], sha: str, rollback_stage: str | None = None) -> dict:
    installer = (REPO / 'deploy/ship_remote.py').read_bytes()
    if rollback_stage:
        if not re.fullmatch(r'/var/tmp/matrix-ship-\d+', rollback_stage):
            raise ShipError('rollback stage must be /var/tmp/matrix-ship-<n>')
        return run_installer(rollback_stage, rollback=True)
    stage = f'/var/tmp/matrix-ship-{time.time_ns()}'
    spec = {'source_sha': sha, 'files': [{'path': p, 'sha256': sha256(git('show', f'{sha}:{p}'))} for p in files]}
    items = {'ship_remote.py': installer, 'spec.json': json.dumps(spec, indent=2).encode()}
    for f in files:
        items['payload/' + f] = git('show', f'{sha}:{f}')
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode='w') as tar:
        for name, raw in sorted(items.items()):
            info = tarfile.TarInfo(name)
            info.size, info.mode = len(raw), 0o600
            tar.addfile(info, io.BytesIO(raw))
    inventory = {name: sha256(raw) for name, raw in items.items()}
    unpack = (
        "import hashlib,io,os,pathlib,sys,tarfile;os.umask(0o077);root=pathlib.Path(%r);inv=%r\n"
        "root.mkdir(mode=0o700)\n"
        "with tarfile.open(fileobj=io.BytesIO(sys.stdin.buffer.read())) as t:\n"
        " ms=t.getmembers();assert {m.name for m in ms}==set(inv)\n"
        " for m in ms:\n"
        "  assert m.isfile() and '..' not in pathlib.PurePosixPath(m.name).parts and not m.name.startswith('/')\n"
        "  raw=t.extractfile(m).read();assert hashlib.sha256(raw).hexdigest()==inv[m.name]\n"
        "  p=root/m.name;p.parent.mkdir(parents=True,exist_ok=True,mode=0o700);p.write_bytes(raw)\n"
        "print('staged')"
    ) % (stage, inventory)
    remote_python(unpack, data=buf.getvalue())
    return run_installer(stage, rollback=False)


def run_installer(stage: str, rollback: bool) -> dict:
    """Run the device installer; its JSON report is returned even when it fails."""
    unit_error = None
    try:
        remote('sudo -n systemd-run --wait --collect --service-type=exec --property=RuntimeMaxSec=600 '
               f'/usr/bin/python3 -I -B {stage}/ship_remote.py' + (' --rollback' if rollback else ''), timeout=700)
    except (ShipError, subprocess.TimeoutExpired) as error:
        unit_error = str(error)
    name = 'rollback-execution.json' if rollback else 'execution.json'
    report = json.loads(remote_python(f"import sys;sys.stdout.write(open({stage + '/' + name!r}).read())"))
    report.setdefault('stage', stage)
    if unit_error:
        report['unit_error'] = unit_error[-400:]
    return report


def roll_back(stage: str, previous: str) -> dict[str, Any]:
    """Automatic rollback; only ROLLED_BACK when the device and public page both confirm it."""
    out: dict[str, Any] = {}
    try:
        back = stage_and_run([], '', rollback_stage=stage)
        out['rollback'] = back
        restored = public_versions(fetch_public())
        out['public_versions_after_rollback'] = restored
        if back.get('status') == 'ROLLED_BACK_NORMAL' and restored == [previous]:
            out['status'] = 'ROLLED_BACK'
        else:
            out['status'] = 'ROLLBACK_FAILED'
            out['error'] = f'rollback status {back.get("status")}, public page shows {restored}, expected {previous}'
    except Exception as error:
        out.update(status='ROLLBACK_FAILED', error=f'{type(error).__name__}: {error}')
    print(f'{out["status"]}' + (f': {out["error"]}' if 'error' in out else f' to {previous}'))
    if out['status'] != 'ROLLED_BACK':
        print(f'operator action needed; retry: python3 deploy/ship.py --rollback {stage}', file=sys.stderr)
    return out


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    group = parser.add_mutually_exclusive_group()
    group.add_argument('--apply', action='store_true', help='deploy origin/main to production')
    group.add_argument('--rollback', metavar='STAGE', help='restore code backed up by an earlier --apply')
    args = parser.parse_args(argv)
    mode = 'rollback' if args.rollback else 'apply' if args.apply else 'dry-run'
    result: dict[str, Any] = {'mode': mode, 'started_utc': datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds')}
    try:
        found = locate_target()
        result['device'] = found
        print(f"device {found['target']} (pinned host key {found['alias']}, found by {found['found_by']})")
        if args.rollback:
            result['execution'] = stage_and_run([], '', rollback_stage=args.rollback)
            result['public_versions'] = public_versions(fetch_public())
            result['status'] = result['execution'].get('status')
            return 0 if result['status'] == 'ROLLED_BACK_NORMAL' else 2
        git('fetch', '-q', 'origin', 'main')
        sha = git('rev-parse', 'origin/main').decode().strip()
        status, pipeline = ci_status(sha)
        files, version, unsupported = build_plan(sha)
        live = public_versions(fetch_public())
        result.update(source_sha=sha, ci_pipeline=pipeline, ci_status=status, version=version,
                      production_versions=live, files=files)
        print(f'origin/main {sha[:10]}  CI pipeline {pipeline}: {status}')
        print(f'version {version}  (production serves {", ".join(live) or "unknown"})')
        print(f'{len(files)} runtime file(s) differ:' + ''.join(f'\n  {f}' for f in files))
        problems = []
        if status != 'success':
            problems.append(f'CI for {sha[:10]} is {status}, not success')
        if not files:
            problems.append('production already matches origin/main')
        if len(live) != 1:
            problems.append(f'cannot identify the production version (public page shows {live or "none"})')
        elif live == [version]:
            problems.append(f'production already reports {version}; bump APPLICATION_VERSION')
        if 'webapp.py' not in files and files:
            problems.append('webapp.py (APPLICATION_VERSION) unchanged; bump the visible version')
        problems.extend(unsupported)
        result['problems'] = problems
        if problems:
            print('NOT DEPLOYABLE:\n  ' + '\n  '.join(problems))
            result['status'] = 'REFUSED'
            return 1
        if not args.apply:
            print('dry run OK; rerun with --apply to deploy')
            result['status'] = 'DRY_RUN_OK'
            return 0
        previous = live[0]
        execution = stage_and_run(files, sha)
        result['execution'] = execution
        if execution.get('status') != 'INSTALLED':
            print(f'installer failed: {execution.get("status")} {execution.get("primary_error")}')
            result['status'] = 'FAILED'
            try:
                after = public_versions(fetch_public())
                result['public_versions_after_failure'] = after
            except Exception as error:      # service down: the installer report says why
                result['public_check_error'] = f'{type(error).__name__}: {error}'
                return 2
            if version in after:
                # The installer's own recovery did not happen (or was not recorded).
                print(f'candidate {version} still served after installer failure; rolling back')
                result.update(roll_back(execution['stage'], previous))
            return 2
        try:
            after = public_versions(fetch_public())
            result['public_versions_after'] = after
            check_failure = None if after == [version] else f'public page shows {after}, expected {version}'
        except Exception as error:
            check_failure = f'public version check failed: {type(error).__name__}: {error}'
        if check_failure:
            print(check_failure + '; rolling back')
            result['public_check_failure'] = check_failure
            result.update(roll_back(execution['stage'], previous))
            return 2
        result['status'] = 'DEPLOYED'
        print(f'DEPLOYED {version}; rollback: python3 deploy/ship.py --rollback {execution["stage"]}')
        return 0
    except (ShipError, subprocess.SubprocessError, OSError, ValueError, KeyError) as error:
        result.update(status='ERROR', error=f'{type(error).__name__}: {error}')
        print('ship failed:', result['error'], file=sys.stderr)
        return 2
    finally:
        print('result:', write_result(result))


if __name__ == '__main__':
    raise SystemExit(main())
