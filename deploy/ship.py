#!/usr/bin/env python3
"""One-command Matrix production delivery.

    python3 deploy/ship.py             # dry run: CI gate + diff vs production, no changes
    python3 deploy/ship.py --apply     # deploy origin/main
    python3 deploy/ship.py --rollback STAGE   # restore the code backed up by an earlier --apply

Gates (all must hold, otherwise nothing on the device changes):
  * the commit is origin/main and its newest GitLab pipeline succeeded;
  * the commit's APPLICATION_VERSION differs from what production serves;
  * the only differing files are runtime files (no state, config, media, secrets).

The device-side installer (deploy/ship_remote.py) stops matrix-web, backs up the replaced
code and an opaque copy of data/ + resources/media, publishes each file atomically with
the production owner/mode, restarts, and restores the old code by itself if the service
fails to come back. ship.py then requires the public pairing page to show the new version
and automatically rolls back if it does not. User state, config, drafts, uploads and the
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
HOST = os.environ.get('MATRIX_PROD_HOST', 'admin@192.168.1.4')
PUBLIC_URL = os.environ.get('MATRIX_PROD_URL', 'http://192.168.1.4:5000/pair')
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


def public_versions(html: bytes) -> list[str]:
    return sorted({v.decode() for v in re.findall(rb'\d+\.\d+\.\d+-preview', html)})


def run(argv, **kw):
    return subprocess.run(argv, capture_output=True, check=True, **kw).stdout


def git(*args) -> bytes:
    return run(['git', '-C', str(REPO), *args])


def ssh_argv() -> list[str]:
    return ['ssh', '-F', '/dev/null', '-i', SSH_KEY, '-o', 'IdentitiesOnly=yes', '-o', 'BatchMode=yes',
            '-o', 'StrictHostKeyChecking=yes', '-o', 'ConnectTimeout=10', '-o', 'ServerAliveInterval=10',
            '-o', 'ServerAliveCountMax=2', HOST]


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
    pipelines = gitlab(f'/pipelines?sha={sha}&order_by=id&sort=desc&per_page=5')
    if not pipelines:
        return 'missing', None
    return pipelines[0]['status'], pipelines[0]['id']


def fetch_public() -> bytes:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(PUBLIC_URL, timeout=10) as response:
        return response.read()


PROD_INVENTORY = (
    "import hashlib,json,pathlib;r=pathlib.Path(%r);o={}\n"
    "for p in r.rglob('*'):\n"
    " rel=p.relative_to(r).as_posix()\n"
    " if rel.startswith(('data/','resources/','venv/','.venv','backups/','.git/')) or '__pycache__' in rel:continue\n"
    " if p.is_file() and not p.is_symlink():o[rel]=hashlib.sha256(p.read_bytes()).hexdigest()\n"
    "print(json.dumps(o))"
) % REMOTE_ROOT


def build_plan(sha: str):
    tree = git('ls-tree', '-r', '--name-only', sha).decode().split('\n')
    main = {p: sha256(git('show', f'{sha}:{p}')) for p in tree if p and is_runtime(p)}
    prod = json.loads(remote_python(PROD_INVENTORY))
    files = plan_files(main, prod)
    version = version_of(git('show', f'{sha}:webapp.py'))
    return files, version, prod


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


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    group = parser.add_mutually_exclusive_group()
    group.add_argument('--apply', action='store_true', help='deploy origin/main to production')
    group.add_argument('--rollback', metavar='STAGE', help='restore code backed up by an earlier --apply')
    parser.add_argument('--allow-same-version', action='store_true',
                        help='deploy even if production already reports this version')
    args = parser.parse_args(argv)
    mode = 'rollback' if args.rollback else 'apply' if args.apply else 'dry-run'
    result: dict[str, Any] = {'mode': mode, 'started_utc': datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds')}
    try:
        if args.rollback:
            result['execution'] = stage_and_run([], '', rollback_stage=args.rollback)
            result['public_versions'] = public_versions(fetch_public())
            result['status'] = result['execution'].get('status')
            return 0 if result['status'] == 'ROLLED_BACK_NORMAL' else 2
        git('fetch', '-q', 'origin', 'main')
        sha = git('rev-parse', 'origin/main').decode().strip()
        status, pipeline = ci_status(sha)
        files, version, _ = build_plan(sha)
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
        if live == [version] and not args.allow_same_version:
            problems.append(f'production already reports {version}; bump APPLICATION_VERSION')
        if 'webapp.py' not in files and files:
            problems.append('webapp.py (APPLICATION_VERSION) unchanged; bump the visible version')
        result['problems'] = problems
        if problems:
            print('NOT DEPLOYABLE:\n  ' + '\n  '.join(problems))
            result['status'] = 'REFUSED'
            return 1
        if not args.apply:
            print('dry run OK; rerun with --apply to deploy')
            result['status'] = 'DRY_RUN_OK'
            return 0
        execution = stage_and_run(files, sha)
        result['execution'] = execution
        after = public_versions(fetch_public())
        result['public_versions_after'] = after
        if execution.get('status') != 'INSTALLED':
            print(f'installer failed: {execution.get("status")} {execution.get("primary_error")}')
            result.update(status='FAILED', public_versions_after_failure=after)
            return 2
        if after != [version]:
            print(f'public page shows {after}, expected {version}; rolling back')
            back = stage_and_run([], '', rollback_stage=execution['stage'])
            result.update(status='ROLLED_BACK', rollback=back, public_versions_after_rollback=public_versions(fetch_public()))
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
