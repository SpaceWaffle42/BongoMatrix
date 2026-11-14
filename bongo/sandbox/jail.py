"""Starting an imported module's child process (``spawn``, in the Matrix) and closing it in (``enter``, in the
child, before any of the module's code runs).

On the Matrix the display runs as root (for the panel's pins), so a child can be shut in properly:
  - its own user (a number given at install, ``addons.uid_for``), no groups, no way back to root;
  - its own view of the files: /home, /root, /etc/bongo, /tmp and the Matrix's folder are emptied, and only
    Python, the bongo package, the fonts (not the pet art), the module's own folder (read-only) and its own
    data folder (writable) are put back;
  - its own network with nothing in it but a relay to the gate (``gate.py``), unless its owner unticked
    "Only allow the sites it lists" (then it has the Matrix's network, but still none of its files);
  - limits on memory, files and processes.
Anywhere else (a computer running the tests, the demo) nothing is shut in: ``ENFORCED`` stays False, the child
still goes through the gate when it uses the usual web libraries, and the dashboard says sites aren't enforced.
"""
from __future__ import annotations

import ctypes
import os
import resource
import socket
import struct
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]       # the Matrix's folder (the bongo package, resources, venv)
ENFORCED = False                                 # in the child: set once it is shut in

CLONE_NEWNS, CLONE_NEWNET, CLONE_NEWPID = 0x00020000, 0x40000000, 0x20000000
MS_RDONLY, MS_NOSUID, MS_NODEV, MS_NOEXEC, MS_REMOUNT, MS_BIND, MS_REC, MS_PRIVATE = 1, 2, 4, 8, 32, 4096, 16384, 1 << 18
MNT_DETACH = 2
SIOCSIFFLAGS, IFF_UP, IFF_LOOPBACK, IFF_RUNNING = 0x8914, 0x1, 0x8, 0x40
PR_SET_NO_NEW_PRIVS, PR_SET_PDEATHSIG = 38, 1
LIMITS = {resource.RLIMIT_AS: 512 << 20, resource.RLIMIT_NOFILE: 128, resource.RLIMIT_NPROC: 32,
          resource.RLIMIT_FSIZE: 64 << 20, resource.RLIMIT_CORE: 0}


def can_shut_in() -> bool:
    """Whether children are shut in here (root on Linux, unless the tests turn it off)."""
    return sys.platform.startswith("linux") and os.geteuid() == 0 and os.environ.get("BONGO_SANDBOX", "on") != "off"


def run_dir() -> Path:
    """Where the gate's socket lives (and, when shutting in, the scratch folder for each child's view)."""
    if can_shut_in():
        path = Path("/run/bongo-modules")
    else:
        path = Path(tempfile.gettempdir()) / f"bongo-modules-{os.getuid()}"
    path.mkdir(mode=0o755, parents=True, exist_ok=True)
    return path


def spawn(spec: dict) -> subprocess.Popen:
    """Start a child for one module. ``spec``: id, dir (its code), data (its writable folder), uid, net ("gate"
    or "open"), gate (the gate's socket). The child is sent ``spec`` first, then the usual messages."""
    from bongo.sandbox import wire

    shut = can_shut_in()
    spec = {**spec, "jail": shut, "root": str(ROOT)}
    data = Path(spec["data"])
    data.mkdir(parents=True, exist_ok=True)
    if shut:
        os.chown(data, spec["uid"], spec["uid"])
        data.chmod(0o700)
    boot = f"import sys; sys.path.insert(0, {str(ROOT)!r}); from bongo.sandbox.host import main; main()"
    env = {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": "/tmp" if shut else str(data), "LANG": "C.UTF-8",
           "PYTHONDONTWRITEBYTECODE": "1", "PYTHONUNBUFFERED": "1"}
    if not shut and "BONGO_FONTS" in os.environ:
        env["BONGO_FONTS"] = os.environ["BONGO_FONTS"]
    child = subprocess.Popen([sys.executable, "-I", "-c", boot], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, env=env, cwd="/" if shut else str(data), close_fds=True,
                             start_new_session=True)
    wire.send(child.stdin, spec)
    return child


def _processes_of(uid: int):
    for entry in os.scandir("/proc"):
        if not entry.name.isdigit():
            continue
        try:
            with open(f"/proc/{entry.name}/status") as f:
                status = f.read()
        except OSError:
            continue
        uids = next((line.split()[1:] for line in status.splitlines() if line.startswith("Uid:")), [])
        state = next((line.split()[1] for line in status.splitlines() if line.startswith("State:")), "")
        if uids and int(uids[0]) == uid and state not in ("Z", "X"):   # (an ended one waiting to be tidied away)
            rss = next((int(line.split()[1]) for line in status.splitlines() if line.startswith("VmRSS:")), 0)
            yield int(entry.name), rss * 1024


def kill_user(uid: int) -> None:
    """End every process running as a module's user (anything it started, however it was started)."""
    if not can_shut_in() or uid < 61000:
        return
    import signal
    for _ in range(20):
        found = list(_processes_of(uid))
        if not found:
            return
        for pid, _ in found:
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
        import time
        time.sleep(0.05)


def memory_of(uid: int) -> int:
    """Bytes of memory in use by a module's user (all its processes)."""
    return sum(rss for _, rss in _processes_of(uid)) if can_shut_in() and uid >= 61000 else 0


# -- in the child ----------------------------------------------------------------------------------------
_libc = None


def _call(name, *args):
    global _libc
    _libc = _libc or ctypes.CDLL(None, use_errno=True)
    if getattr(_libc, name)(*args) != 0:
        err = ctypes.get_errno()
        raise OSError(err, f"{name}: {os.strerror(err)}")


def _mount(src, target, fstype, flags, data=None):
    enc = lambda s: None if s is None else str(s).encode()   # noqa: E731
    try:
        _call("mount", enc(src), enc(target), enc(fstype), ctypes.c_ulong(flags), enc(data))
    except OSError as error:
        raise OSError(error.errno, f"{error.strerror} ({target})") from None


def _views(spec: dict) -> list[tuple[str, str, bool]]:
    """(from, to, writable) for everything a child may see of the Matrix's own files."""
    root = Path(spec["root"])
    out = [(str(root / "bongo"), str(root / "bongo"), False)]
    resources = root / "resources"
    if resources.is_dir():                     # the fonts and the like, never the pet art (its licence)
        out += [(str(p), str(p), False) for p in sorted(resources.iterdir()) if p.is_dir() and p.name != "pet"]
    prefix = Path(sys.prefix)
    if prefix != Path(sys.base_prefix) or str(prefix).startswith(("/home", "/root")):
        out.append((str(prefix), str(prefix), False))   # the venv (Pillow and the rest)
    out.append((spec["dir"], spec["dir"], False))
    out.append((spec["data"], spec["data"], True))
    if spec.get("gate"):                       # the gate's socket alone (reaching it; it checks who's asking)
        out.append((spec["gate"], spec["gate"], False))
    return out


def _hidden(spec: dict) -> list[str]:
    """Folders emptied in a child's view (anything kept is put back by ``_views``)."""
    paths = ["/home", "/root", "/etc/bongo", "/var/lib/bongo", spec["root"], str(Path(spec["data"]).parent),
             "/run", "/var/run", "/var/lib/dbus",           # the host's sockets (D-Bus, journald...): none reachable
             "/tmp", "/var/tmp", "/dev/shm", "/run/lock"]  # shared places to write: each child gets empty ones
    out = []
    for p in sorted({os.path.realpath(p) for p in paths}, key=len):
        if os.path.isdir(p) and not any(p == h or p.startswith(h + "/") for h in out):
            out.append(p)
    return out


def _loopback_up():
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        import fcntl
        fcntl.ioctl(s, SIOCSIFFLAGS, struct.pack("16sH14x", b"lo", IFF_UP | IFF_LOOPBACK | IFF_RUNNING))


SCRATCH = ("/tmp", "/var/tmp", "/dev/shm")       # a child's own small places to write (empty, and gone with it)


def _shut_in(spec: dict) -> None:
    gate = spec.get("net") == "gate"
    _call("unshare", CLONE_NEWNS | CLONE_NEWPID | (CLONE_NEWNET if gate else 0))
    # Its own process list: this process stays outside, waiting; the module runs in a child that is the first
    # process of the new list. When the Matrix stops this one, the kernel ends every process the module made.
    pid = os.fork()
    if pid:
        _, status = os.waitpid(pid, 0)
        os._exit(os.waitstatus_to_exitcode(status) & 0xFF)
    _mount(None, "/", None, MS_REC | MS_PRIVATE)
    stage = Path(spec["stage"])                 # an empty folder made by the Matrix, for this moment only
    _mount("tmpfs", stage, "tmpfs", MS_NOSUID | MS_NODEV, "size=1m,mode=755")
    views = _views(spec)
    folders = [os.path.isdir(src) for src, _, _ in views]   # (asked now: once hidden, they can't be seen)
    held = []
    for k, (src, _, _) in enumerate(views):     # hold on to each, before the folders they're in are emptied
        target = stage / str(k)
        if folders[k]:
            target.mkdir()
        else:
            target.touch()
        _mount(src, target, None, MS_BIND | MS_REC)
        held.append(os.open(target, os.O_PATH | (os.O_DIRECTORY if folders[k] else 0)))
    for path in _hidden(spec):
        mode = "1777" if path in SCRATCH else "755"
        _mount("tmpfs", path, "tmpfs", MS_NOSUID | MS_NODEV, f"size=16m,mode={mode}")
    for fd, folder, (_, dst, writable) in zip(held, folders, views):
        if folder:
            os.makedirs(dst, exist_ok=True)
        else:
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            Path(dst).touch()
        _mount(f"/proc/self/fd/{fd}", dst, None, MS_BIND | MS_REC)
        _mount(None, dst, None, MS_BIND | MS_REMOUNT | MS_NOSUID | MS_NODEV | (0 if writable else MS_RDONLY))
        os.close(fd)
    try:
        _call("umount2", str(stage).encode(), MNT_DETACH)
    except OSError:
        pass                                    # (emptied with the folder it's in: nothing to see)
    _mount("proc", "/proc", "proc", MS_NOSUID | MS_NODEV | MS_NOEXEC)   # only its own processes
    if gate:
        _loopback_up()
    try:                                        # short of memory, the kernel ends the module first, not the Matrix
        with open("/proc/self/oom_score_adj", "w") as f:
            f.write("1000")
    except OSError:
        pass
    uid = int(spec["uid"])
    os.setgroups([])
    os.setresgid(uid, uid, uid)
    os.setresuid(uid, uid, uid)
    _call("prctl", PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0)
    _call("prctl", PR_SET_PDEATHSIG, 9, 0, 0, 0)          # (after the change of user, which clears it)
    for which, most in LIMITS.items():
        resource.setrlimit(which, (most, most))
    os.chdir(spec["data"])
    os.environ["HOME"] = os.environ["TMPDIR"] = "/tmp"


def enter(spec: dict) -> None:
    """Called first thing in the child, before the module's code is loaded."""
    global ENFORCED
    if spec.get("jail"):
        _shut_in(spec)
        ENFORCED = True
    else:
        os.environ["TMPDIR"] = spec["data"]
    if spec.get("net") == "gate":
        relay(spec["gate"], spec["id"])


def relay(gate_path: str, module_id: str) -> int:
    """A web proxy on the child's own 127.0.0.1 that hands every connection to the gate, so the usual web
    libraries (urllib, requests) just work. Shut in, it's the only way out: anything else finds no network."""
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("127.0.0.1", 0))
    server.listen(16)
    port = server.getsockname()[1]

    def pipe(a, b):
        try:
            while data := a.recv(65536):
                b.sendall(data)
        except OSError:
            pass
        finally:
            for s in (a, b):
                try:
                    s.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

    def handle(client):
        try:
            out = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            out.connect(gate_path)
            out.sendall(f"BONGO {module_id}\n".encode())
        except OSError:
            client.close()
            return
        threading.Thread(target=pipe, args=(out, client), daemon=True).start()
        pipe(client, out)
        client.close(), out.close()

    def accept():
        while True:
            try:
                client, _ = server.accept()
            except OSError:
                return
            threading.Thread(target=handle, args=(client,), daemon=True).start()

    threading.Thread(target=accept, daemon=True, name="gate-relay").start()
    url = f"http://127.0.0.1:{port}"
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
        os.environ[name] = url
    for name in ("NO_PROXY", "no_proxy"):
        os.environ.pop(name, None)
    return port
