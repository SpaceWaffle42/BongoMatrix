"""Device-side half of deploy/ship.py. Runs as root under a transient systemd unit.

Derived from the installer that delivered 0.3.66/0.3.67 on 2026-10-09. Spec: spec.json
lists runtime files and their sha256; payload/ holds the bytes. Steps:
  stop matrix-web -> back up replaced code + opaque data/ and resources/media tarball
  -> atomic publish (keeping each file's existing owner/mode; new files admin:matrix-web 0644)
  -> start matrix-web and require loopback HTTP 200/302.
Any failure after the first write restores the backed-up code and restarts the service.
`--rollback` restores the backed-up code of this stage. User state is never written.

Recovery rules:
  * a code restore validates every destination and backup before writing anything;
  * if a restore fails after its first write the tree may be mixed, so the service is left
    stopped and the result is FAILED_NEEDS_OPERATOR;
  * report persistence is best-effort and never prevents recovery;
  * once `systemctl stop` has been attempted the service is always restarted on failure
    unless the code tree is known to be incoherent.
"""
import datetime
import fcntl
import hashlib
import json
import os
import pathlib
import stat
import subprocess
import sys
import tarfile
import time

ROOT = pathlib.Path('/home/admin/matrix')
ROOT_OWNER = (1000, 985, 0o750)          # admin:matrix-web 0750; 0710 breaks fresh imports
NEW_FILE_META = {'uid': 1000, 'gid': 985, 'mode': 0o644}
STATE_PREFIXES = ('data/', 'resources/', 'venv/', '.git/', 'backups/')
DISPLAY_LOCK = '/run/matrix-display/matrix-display.lock'
STAGE = pathlib.Path(__file__).resolve().parent
ROLLBACK = False
SPEC: dict = {}
REPORT = STAGE / 'execution.json'
locks: list = []
log: list = []
save_errors: list = []


class IncoherentTree(RuntimeError):
    """A code restore failed after its first write; the checkout may be mixed."""


def utc():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds')


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def save(result):
    """Persist the report. Best-effort: a full disk must not stop recovery."""
    result.update(updated_utc=utc(), commands=log, stage=str(STAGE),
                  rollback_command=f'sudo -n /usr/bin/python3 -I -B {STAGE}/ship_remote.py --rollback')
    if save_errors:
        result['report_write_errors'] = save_errors[-5:]
    try:
        tmp = REPORT.with_suffix('.tmp')
        tmp.write_text(json.dumps(result, indent=2) + '\n')
        os.replace(tmp, REPORT)
        return True
    except OSError as error:
        save_errors.append(f'{type(error).__name__}: {error}')
        return False


def command(argv, timeout=120):
    done = subprocess.run(argv, capture_output=True, timeout=timeout)
    log.append({'argv': argv, 'exit': done.returncode})
    assert done.returncode == 0, 'administrative command failed: ' + argv[0]
    return done.stdout.decode()


def props():
    out = command(['systemctl', 'show', 'matrix-web.service',
                   '--property=MainPID,ControlPID,ControlGroup,ActiveState,SubState,PrivateNetwork'])
    return dict(line.split('=', 1) for line in out.splitlines() if '=' in line)


def stop():
    before = props()
    command(['systemctl', 'stop', 'matrix-web.service'])
    after = props()
    assert after['MainPID'] == after['ControlPID'] == '0' and after['ActiveState'] in ('inactive', 'failed')
    for group in {before['ControlGroup'], after['ControlGroup'], '/system.slice/matrix-web.service'}:
        cgroup = pathlib.Path('/sys/fs/cgroup') / group.lstrip('/') if group else None
        if cgroup and cgroup.exists():
            assert all(not f.read_text().strip() for f in cgroup.rglob('cgroup.procs')), 'service processes remain'


def take(path, create=False):
    fd = os.open(path, os.O_RDWR | os.O_NOFOLLOW | (os.O_CREAT if create else 0), 0o600)
    info = os.fstat(fd)
    assert stat.S_ISREG(info.st_mode) and info.st_nlink == 1
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    locks.append(fd)
    return fd


def release(fd):
    if fd is not None and fd in locks:
        locks.remove(fd)
        os.close(fd)


def safe(path):
    assert path.is_relative_to(ROOT)
    for parent in [path, *path.parents]:
        if parent.exists():
            assert not parent.is_symlink(), str(parent)
        if parent == ROOT:
            break
    return path


def record(path):
    safe(path)
    if not path.exists():
        return {'absent': True}
    info = path.stat()
    assert stat.S_ISREG(info.st_mode) and info.st_nlink == 1
    return {'sha256': sha(path), 'size': info.st_size, 'uid': info.st_uid, 'gid': info.st_gid,
            'mode': stat.S_IMODE(info.st_mode)}


def publish(path, data, meta):
    safe(path)
    assert path.parent.is_dir(), f'missing directory for {path}'
    tmp = path.parent / f'.matrix-ship-{time.time_ns()}'
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, 'wb') as handle:
            handle.write(data)
            handle.flush()
            os.fchown(handle.fileno(), meta['uid'], meta['gid'])
            os.fchmod(handle.fileno(), meta['mode'])
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        dirfd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        os.fsync(dirfd)
        os.close(dirfd)
    finally:
        if tmp.exists():
            tmp.unlink()


def root_ok():
    info = ROOT.lstat()
    assert stat.S_ISDIR(info.st_mode)
    assert (info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode)) == ROOT_OWNER, 'checkout owner/mode drift'


def start_normal():
    command(['systemctl', 'start', 'matrix-web.service'])
    deadline, http = time.monotonic() + 45, None
    while time.monotonic() < deadline:
        state = props()
        probe = subprocess.run(['curl', '--noproxy', '*', '--max-time', '3', '-s', '-o', '/dev/null', '-w',
                                '%{http_code}', 'http://127.0.0.1:5000/'], capture_output=True)
        http = probe.stdout.decode()
        if (state['ActiveState'], state['SubState'], state['PrivateNetwork']) == ('active', 'running', 'no') \
                and probe.returncode == 0 and http in ('200', '302'):
            return {'service': state, 'loopback_http': http}
        time.sleep(1)
    raise RuntimeError(f'service did not come back (last HTTP {http})')


def restore_code(manifest, result):
    """Restore every manifest file or raise. Validates everything before the first write."""
    expected = {e['path']: e['sha256'] for e in SPEC['files']}
    assert set(manifest) == set(expected), 'backup manifest does not match spec'
    plan = []
    for name, before in manifest.items():
        path = ROOT / name
        current = record(path)
        assert current == before or current.get('sha256') == expected[name], \
            f'{name} changed after this deploy; refuse rollback'
        if before.get('absent'):
            plan.append((path, None, before))
        else:
            backup = STAGE / 'backup/code' / name
            data = backup.read_bytes()
            assert hashlib.sha256(data).hexdigest() == before['sha256'], f'backup of {name} is corrupt'
            assert path.parent.is_dir(), f'missing directory for {name}'
            plan.append((path, data, before))
    try:
        for path, data, before in plan:
            if record(path) == before:
                continue
            result['production_mutated'] = True
            if data is None:
                path.unlink()
            else:
                publish(path, data, before)
        for name, before in manifest.items():
            assert record(ROOT / name) == before, f'{name} not restored'
    except BaseException as error:
        raise IncoherentTree(f'code restore failed part-way: {type(error).__name__}: {error}') from error


def locked_and_quiet():
    take('/run/lock/matrix-scoped-release.lock', create=True)
    for optional in ('/run/matrix-ci-physical/dispatch.lock', '/run/matrix-ci-physical/report.commit.lock',
                     '/run/matrix-device-restart/transaction.lock'):
        if pathlib.Path(optional).exists():
            take(optional)
    busy = command(['systemctl', 'list-units', '--state=active,activating,deactivating', '--no-legend',
                    '--plain', 'matrix-scoped-*.service', 'matrix-ci-physical.service'])
    assert not busy.strip(), 'another release/CI unit is active'


def validate_spec():
    for entry in SPEC['files']:
        name = pathlib.PurePosixPath(entry['path'])
        assert not name.is_absolute() and '..' not in name.parts
        assert not str(name).startswith(STATE_PREFIXES)
        assert ROLLBACK or sha(STAGE / 'payload' / entry['path']) == entry['sha256']
        safe(ROOT / entry['path'])


def back_up_and_publish(result):
    backup = STAGE / 'backup'
    backup.mkdir(mode=0o700)
    manifest = {e['path']: record(ROOT / e['path']) for e in SPEC['files']}
    for name, before in manifest.items():
        if before.get('absent'):
            continue
        target = backup / 'code' / name
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        target.write_bytes((ROOT / name).read_bytes())
        assert sha(target) == before['sha256']
    (backup / 'code-manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    with tarfile.open(backup / 'opaque-current-state.tar', 'x') as tar:
        for name in ('data', 'resources/media'):
            if (ROOT / name).is_dir():
                safe(ROOT / name)
                tar.add(ROOT / name, arcname=name)
    result.update(status='BACKUP_READY', replaced_files=manifest,
                  state_backup_sha256=sha(backup / 'opaque-current-state.tar'))
    save(result)
    for entry in SPEC['files']:
        before = manifest[entry['path']]
        meta = NEW_FILE_META if before.get('absent') else before
        result['production_mutated'] = True
        publish(ROOT / entry['path'], (STAGE / 'payload' / entry['path']).read_bytes(), meta)
    for entry in SPEC['files']:
        assert record(ROOT / entry['path'])['sha256'] == entry['sha256'], entry['path']
    result['status'] = 'PAYLOAD_INSTALLED'
    save(result)


def recover(result, flags):
    """Bring the previous code back up after a failure. Never raises."""
    try:
        if not flags['stop_attempted']:
            return
        if flags['restore_incoherent']:
            raise IncoherentTree('code tree may be mixed; service left stopped')
        if result['production_mutated'] and not ROLLBACK and not flags['opened']:
            if flags['opened_once']:
                stop()
            if flags['display'] is None:
                flags['display'] = take(DISPLAY_LOCK)
            try:
                restore_code(json.loads((STAGE / 'backup/code-manifest.json').read_bytes()), result)
            except IncoherentTree:
                flags['restore_incoherent'] = True
                raise
        release(flags['display'])
        flags['display'] = None
        result.update(start_normal())
        if ROLLBACK:
            result['status'] = 'ROLLBACK_FAILED_UNCHANGED_RESTARTED'
        else:
            result['status'] = 'FAILED_PREVIOUS_VERSION_RESTORED'
    except BaseException as cleanup:
        result.update(status='FAILED_NEEDS_OPERATOR', cleanup_error={'class': type(cleanup).__name__,
                                                                    'message': str(cleanup)})
    finally:
        save(result)


def run():
    result = {'status': 'STARTED', 'mode': 'rollback' if ROLLBACK else 'install',
              'source_sha': SPEC.get('source_sha'), 'production_mutated': False}
    flags = {'stop_attempted': False, 'opened': False, 'opened_once': False, 'display': None,
             'restore_incoherent': False}
    save(result)
    try:
        assert os.geteuid() == 0
        root_ok()
        locked_and_quiet()
        state = props()
        assert state['PrivateNetwork'] == 'no'
        result['before_service'] = state
        validate_spec()
        flags['stop_attempted'] = True        # recorded before stop(): a failed readback still recovers
        stop()
        flags['display'] = take(DISPLAY_LOCK)
        result['status'] = 'SERVICE_STOPPED'
        save(result)
        if ROLLBACK:
            try:
                restore_code(json.loads((STAGE / 'backup/code-manifest.json').read_bytes()), result)
            except IncoherentTree:
                flags['restore_incoherent'] = True
                raise
        else:
            back_up_and_publish(result)
        root_ok()
        release(flags['display'])
        flags['display'] = None
        flags['opened_once'] = True
        result.update(start_normal())
        flags['opened'] = True
        result.update(status='ROLLED_BACK_NORMAL' if ROLLBACK else 'INSTALLED',
                      installed={e['path']: record(ROOT / e['path']) for e in SPEC['files']})
        save(result)
        return 0
    except BaseException as primary:
        result.update(status='FAILED', primary_error={'class': type(primary).__name__, 'message': str(primary)})
        flags['opened'] = False
        save(result)
        recover(result, flags)
        return 2
    finally:
        for fd in reversed(locks):
            os.close(fd)
        locks.clear()


def main(argv=None):
    global ROLLBACK, SPEC, REPORT
    os.umask(0o077)
    ROLLBACK = '--rollback' in (sys.argv[1:] if argv is None else argv)
    SPEC = json.loads((STAGE / 'spec.json').read_bytes())
    REPORT = STAGE / ('rollback-execution.json' if ROLLBACK else 'execution.json')
    return run()


if __name__ == '__main__':
    raise SystemExit(main())
