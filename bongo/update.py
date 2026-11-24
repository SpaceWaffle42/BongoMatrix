"""Pull updates from a git repository, with an automatic rollback if the new version won't start.

The display service can only write to data/ (see deploy/bongo-matrix.service), so it never
replaces its own code. The dashboard writes data/update-request.json; deploy/bongo-update.path
sees it and starts deploy/bongo-update.service, which runs ``python -m bongo update --apply``:

    fetch <url> <branch>  ->  reset the checkout to it  ->  pip install if requirements.txt changed
    ->  restart bongo-matrix  ->  wait until the display has been showing frames for a while  ->  done
    (not within HEALTH_SECONDS: reset back to the old commit, restart, report "rolled back")

If the power goes between the reset and the end of that check, the status file still says so at the
next start: the display asks the updater to run again (``resume``), and it picks up where it stopped,
with the same rollback.

Progress and the outcome go to data/update-status.json, which the dashboard shows. Files under
data/ (config, uploads, pairing) are git-ignored, so an update never touches them.

The display also looks for a new version by itself: a few minutes after it starts, then once a day
(``keep_checking``). Nothing installs by itself; what it found is kept in data/update-check.json, the
Software card says so, and the Matrix's public card for the hub (``public``) carries "update available".
"""
from __future__ import annotations

import datetime as dt
import fcntl
import hashlib
import json
import logging
import os
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DATA = Path(os.environ.get("BONGO_DATA") or REPO / "data")   # (as bongo/paths.py: this file uses only Python's own library)
DEFAULT_URL = "http://gitlab.home.lan/matrix_display/bongo_matrix.git"
DEFAULT_BRANCH = "prod_device"   # only the files the display needs (see tools/prod_tree.py)
SERVICE = "bongo-matrix"
HEALTH_SECONDS = 150   # for the restarted display to come good (a Pi Zero takes a while to start)
HEALTH_HOLD = 20       # ...and how long it must stay good: a version that crashes at start-up restarts every 3 s
HEALTH_QUIET = 45      # the longest gap between frames that still counts as playing (a big file being decoded)
GIT_TIMEOUT = 120
LOOK_TIMEOUT = 30          # for the update source to say what its newest commit is
CHECK_FIRST = 300          # the display looks for a new version this long after it starts...
CHECK_EVERY = 24 * 3600    # ...then once a day (nothing installs by itself)
CHECK_RETRY = (15 * 60, 3600, 6 * 3600)   # after a look that failed: again this soon, then 6-hourly until one works
VERSION_SECONDS = 60       # how long ``running_version`` trusts what git said (the hub asks every few seconds)

log = logging.getLogger("bongo.update")

URL = re.compile(r"(https?|ssh)://[^\s]+|[\w.-]+@[\w.-]+:[^\s]+")
BRANCH = re.compile(r"[A-Za-z0-9._/-]{1,100}")


def request_path(data: Path = None) -> Path:
    return (data or DATA) / "update-request.json"


def status_path(data: Path = None) -> Path:
    return (data or DATA) / "update-status.json"


def source(config: dict) -> tuple[str, str]:
    rule = config.get("update") if isinstance(config, dict) else None
    url, branch = (rule.get("url"), rule.get("branch")) if isinstance(rule, dict) else (None, None)
    return (url if url and isinstance(url, str) else DEFAULT_URL,
            branch if branch and isinstance(branch, str) else DEFAULT_BRANCH)


def problems(rule) -> list[str]:
    """Readable problems with the ``update`` section of the config."""
    if rule is None:
        return []
    if not isinstance(rule, dict) or set(rule) - {"url", "branch"}:
        return ["update: must be an object with url and branch"]
    out = []
    url, branch = rule.get("url"), rule.get("branch")
    if url is not None and (not isinstance(url, str) or not URL.fullmatch(url) or url.startswith("-")):
        out.append("update.url: a git address such as https://host/group/repo.git or git@host:group/repo.git")
    if branch is not None and (not isinstance(branch, str) or not BRANCH.fullmatch(branch) or branch.startswith("-")
                               or ".." in branch):
        out.append("update.branch: a branch name such as prod_device")
    return out


def _run(cmd: list[str], timeout: float) -> tuple[int, str, str]:
    """Run ``cmd`` to its end, or stop all of it at ``timeout``. git starts helpers of its own (git-remote-http):
    stopping git alone left them running, so it gets a process group of its own and the whole group is stopped."""
    proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            start_new_session=True, env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})   # never waits to be typed at
    try:
        out, err = proc.communicate(timeout=timeout)
    except BaseException:   # out of time (or stopped from outside): nothing of it is left behind
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:      # already gone
            pass
        try:
            proc.communicate(timeout=5)
        except (subprocess.TimeoutExpired, OSError, ValueError):
            proc.kill()
        raise
    return proc.returncode, out, err


class GitError(RuntimeError):
    """git failed. Its text is plain words (``_complaint``), never git's own output: that can quote the update
    address (which may carry a sign-in), the branch and the command. ``said`` is git's own last lines for the
    log, with the address, any sign-in and the branch taken out (``_scrub``)."""

    def __init__(self, words: str, said: str = ""):
        super().__init__(words)
        self.said = said


REMOTE = ("ls-remote", "fetch")   # the git commands that talk to the update source


def _complaint(command: str, said: str) -> str:
    """What went wrong with ``git <command>``, in plain words, worked out from what git said."""
    said = said.lower()

    def has(*bits):
        return any(bit in said for bit in bits)
    # the card and the checkout itself: whatever the command
    if has("no space left"):
        return "the SD card is full"
    if has("read-only file system"):
        return "the SD card is read-only"
    if has(".lock': file exists", ".lock: file exists", "another git process"):
        return "another git job left a lock behind"
    if has("dubious ownership"):
        return "the checkout belongs to another user, so git won't touch it"
    if has("is corrupt", "corrupt loose object", "object file", "bad object", "unable to read tree", "missing blob",
           "missing tree", "missing commit", "index file corrupt", "bad signature", "not a valid object"):
        return "the checkout is damaged"
    if command not in REMOTE:
        if has("not a git repository"):
            return "this isn't a git checkout"
        if has("local changes", "would be overwritten", "untracked working tree files"):
            return "local changes to its files are in the way"
        if has("permission denied"):
            return "the Matrix isn't allowed to change its own files"
        return f"git {command} failed"
    # the update source
    if has("could not resolve host", "name or service not known", "name resolution", "could not resolve hostname"):
        return "the update source's name wasn't found"
    if has("certificate", "ssl_error", "tls handshake", "gnutls"):
        return "the update source's certificate isn't trusted"
    if has("host key verification failed", "host key"):
        return "the update source's host key isn't recognised"
    status = re.search(r"(?:returned error|http code|http/[\d.]+|\bhttp)\D{0,3}(\d{3})\b", said)
    code = int(status.group(1)) if status else 0
    if code == 407:
        return "a proxy on the way wants a sign-in (error 407)"
    if code in (401, 403) or has("authentication failed", "could not read username", "could not read password",
                                 "permission denied", "access denied"):
        return "the update source refused the sign-in"
    if has("couldn't find remote ref", "no such ref", "remote ref does not exist"):
        return "the update source has no such branch"
    if code == 404 or has("not found", "does not appear to be a git repository", "does not exist"):
        return "the update source has no such repository"
    if code >= 400:
        return f"the update source answered with an error ({code})"
    if has("connection refused", "failed to connect", "couldn't connect", "could not connect", "unreachable",
           "no route to host", "timed out", "connection reset", "hung up", "empty reply"):
        return "couldn't reach the update source"
    return "the update source gave an answer git couldn't use"


URL_LIKE = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://[^\s'\"<>]+")


def shown(url: str) -> str:
    """An update address as it may be written down or shown: without the sign-in some carry
    (https://name:token@host/...), and without anything after a ``?``. Everything up to the last ``@`` of the
    part before the path goes (a password may hold an ``@`` of its own); if there is none there but one further
    on (a password with a ``/`` in it), everything up to that one goes too: a clipped address beats a shown token."""
    def clean(match):
        scheme, _, rest = match.group().partition("://")
        rest = re.split(r"[?#]", rest, maxsplit=1)[0]
        host, slash, path = rest.partition("/")
        if "@" in host:
            host = host.rsplit("@", 1)[1]
        elif "@" in path:
            host, slash, path = path.rsplit("@", 1)[1].partition("/")
        return f"{scheme}://{host}{slash}{path}"
    return URL_LIKE.sub(clean, str(url))


def _scrub(said: str, url: str | None = None, branch: str | None = None) -> str:
    """git's own last lines, fit for the log: no sign-in from any address, the update address and its host as
    "the update source", its branch as "the update branch"."""
    text = shown(said)
    if url:
        plain = shown(url).rstrip("/")
        text = text.replace(plain + "/", "the update source").replace(plain, "the update source")
        host = re.search(r"(?:://|@)([^/:@\s]+)", plain)
        if host and len(host.group(1)) >= 4:
            text = text.replace(host.group(1), "the update source")
    if branch:
        text = re.sub(rf"(?:refs/heads/|(?<![\w/.-])){re.escape(branch)}(?![\w/.-])", "the update branch", text)
    lines = [line.strip()[:300] for line in text.splitlines() if line.strip()]
    telling = [line for line in lines if re.match(r"(fatal|error|remote|ssh|warning)\b", line, re.I)]   # not git's advice
    return " / ".join((telling or lines)[-3:])


def _git(*args, repo: Path = REPO, timeout: float = GIT_TIMEOUT, hide: tuple = ()) -> str:
    """Run git here. ``hide``: the update address and branch, when the command names them (for ``_scrub``)."""
    # Both services run as root but the checkout belongs to the Pi's user, and git refuses to work in
    # someone else's repository ("dubious ownership") unless told that this one folder is trusted.
    try:
        code, out, err = _run(["git", "-c", f"safe.directory={Path(repo).resolve()}", "-C", str(repo), *args], timeout)
    except subprocess.TimeoutExpired:   # (never passed on: its text is the whole command line)
        raise GitError(f"the update source didn't answer in {timeout:g} seconds" if args[0] in REMOTE
                       else f"git {args[0]} didn't finish in {timeout:g} seconds") from None
    except OSError as error:
        raise GitError("git isn't installed" if isinstance(error, FileNotFoundError)
                       else f"couldn't run git ({error.strerror or type(error).__name__})") from None
    if code:
        raise GitError(_complaint(args[0], err or out), _scrub(err or out, *hide))
    return out.strip()


ADDRESS = re.compile(r"(?:https?|ssh|git|file)://\S+|[\w.-]+@[\w.-]+:\S+")


def why(error: BaseException) -> str:
    """Why something to do with updating failed, in plain words: for the status file and the dashboard.
    The error's own text is used only when it is ours. git's output and a command line never are: they can hold
    the update address (with its sign-in) and the branch."""
    if isinstance(error, GitError):
        return str(error)
    if isinstance(error, (subprocess.TimeoutExpired, subprocess.CalledProcessError)):
        cmd = [str(c) for c in error.cmd] if isinstance(error.cmd, (list, tuple)) else str(error.cmd).split()
        step = ("installing its packages" if "pip" in cmd else "restarting the display" if cmd[:1] == ["systemctl"]
                else "a step")
        if isinstance(error, subprocess.TimeoutExpired):
            return f"{step} didn't finish in {error.timeout:g} seconds"
        return f"{step} failed"
    if isinstance(error, OSError):
        return error.strerror or type(error).__name__          # (not its file name)
    return ADDRESS.sub("(an address)", str(error)) or type(error).__name__


def logged(error: BaseException) -> str:
    """``why`` for a log line: with git's own last lines after it when there are any, scrubbed (``_scrub``), so
    Logs can explain a failure the plain words only name."""
    said = getattr(error, "said", "")
    return f"{why(error)} (git said: {said})" if said else why(error)


def version(repo: Path = REPO) -> dict | None:
    """The running code's commit, date and subject, or None outside a git checkout."""
    try:
        sha, date, subject = _git("log", "-1", "--format=%H%x00%cI%x00%s", repo=repo, timeout=10).split("\x00")
    except (RuntimeError, OSError, subprocess.TimeoutExpired, ValueError):
        return None
    return {"commit": sha, "short": sha[:8], "date": date, "subject": subject}


def latest(url: str, branch: str, repo: Path = REPO) -> str:
    """The newest commit on the update branch (asks the server; changes nothing here)."""
    out = _git("ls-remote", "--", url, f"refs/heads/{branch}", repo=repo, timeout=LOOK_TIMEOUT, hide=(url, branch))
    if not out:
        raise GitError("the update source has no such branch")
    return out.split()[0]


# -- what this Matrix already knows about a new version (no network): the Software card and the hub's card --
def check_path(data: Path = None) -> Path:
    return (data or DATA) / "update-check.json"


def _source_mark(url: str, branch: str) -> str:
    """Which update source a look was made at, without keeping its address a second time."""
    return hashlib.sha256(f"{url}\n{branch}".encode()).hexdigest()[:16]


def note(newest: str, url: str, branch: str, data: Path = None) -> None:
    """Remember the newest commit a look at the update source found (the daily look, Check for updates, or
    the updater's own fetch)."""
    _write(check_path(data), {"latest": newest, "source": _source_mark(url, branch), "at": _now()})


_versions: dict[str, tuple[float, dict | None]] = {}


def running_version(repo: Path = REPO) -> dict | None:
    """``version()``, asked of git at most once a minute: the hub asks for the public card every few seconds."""
    key, now = str(repo), time.monotonic()
    seen = _versions.get(key)
    if seen is None or now - seen[0] >= VERSION_SECONDS:
        seen = _versions[key] = (now, version(repo))
    return seen[1]


def known(config: dict, data: Path = None, repo: Path = REPO) -> dict:
    """What is known here about a new version, without asking anyone: the last look's answer, for the
    source set now. ``available`` is None when no look has been made (or it was at another source)."""
    out = {"latest": None, "available": None, "at": None}
    try:
        seen = json.loads(check_path(data).read_text())
        newest, mark, at = seen["latest"], seen["source"], seen.get("at")
        dt.datetime.fromisoformat(at)
    except Exception:   # no note, half a note or nonsense in it (a RecursionError for one nested too deep): not known
        return out
    here = running_version(repo)
    if not here or not isinstance(newest, str) or not re.fullmatch(r"[0-9a-f]{40}", newest) \
            or mark != _source_mark(*source(config)):
        return out
    return {"latest": newest, "available": newest != here["commit"], "at": at}


def public(config: dict, data: Path = None, repo: Path = REPO) -> dict:
    """The part of that anyone on the network may see (the hub's card, /api/public): which build is running,
    whether a newer one is ready, and whether that one was tried and rolled back. Never where updates come
    from, nor what the newer one is."""
    out = {}
    here = running_version(repo)
    if here:
        out["build"] = here["short"]
    if busy(data):
        out["updating"] = True
    elif here and (seen := known(config, data, repo))["available"] is not None:
        out["update"] = seen["available"]
        last = status(data) or {}
        if seen["available"] and last.get("state") == "rolled_back" and last.get("failed") == seen["latest"]:
            out["rolled_back"] = True   # this very version was tried and wouldn't start: the hub says so by the button
    return out


def look(config: dict, data: Path = None, repo: Path = REPO) -> bool | None:
    """Ask the update source once for its newest commit and remember it. True: a new version is ready."""
    here = version(repo)
    if here is None:
        return None                      # not a git checkout: there is nothing to update
    url, branch = source(config)
    newest = latest(url, branch, repo=repo)
    note(newest, url, branch, data)
    return newest != here["commit"]


def keep_checking(load_config, data: Path = None, stop: threading.Event | None = None, repo: Path = REPO,
                  first: float = CHECK_FIRST, every: float = CHECK_EVERY, retry: tuple = CHECK_RETRY) -> threading.Thread:
    """Look for a new version a few minutes after start-up, then once a day. Nothing is installed: the
    Software card and the hub say one is ready. A look that fails (no network yet, the source is down) is tried
    again sooner, after ``retry`` (the last of them over and over), until one works; a run of failures is
    logged once, in plain words."""
    stop = stop or threading.Event()

    def run():
        failures = 0
        if stop.wait(first):
            return
        while True:
            wait = every
            if not os.environ.get("BONGO_OFFLINE"):
                try:
                    found = look(load_config(), data, repo)
                    if found is None:
                        return           # not a git checkout: never anything to find
                    if found:
                        log.info("a new version is ready (Settings > Software)")
                    failures = 0
                except Exception as error:  # noqa: BLE001 - never stops the display
                    if not failures:
                        log.info("couldn't look for a new version: %s", logged(error))
                    if retry:
                        wait = min(every, retry[min(failures, len(retry) - 1)])
                    failures += 1
            if stop.wait(wait):
                return
    thread = threading.Thread(target=run, name="update-check", daemon=True)
    thread.start()
    return thread


def ask(by: str | None = None, data: Path = None) -> None:
    """Called by the dashboard: leave a request for the update service."""
    path = request_path(data)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"at": _now(), "by": by}))
    report("requested", "Waiting for the updater to start", data=data)


def report(state: str, message: str, data: Path = None, **extra) -> dict:
    status = {"state": state, "message": message, "at": _now(), **extra}
    _write(status_path(data), status)
    return status


def _write(path: Path, what: dict) -> None:
    """Put a small file in place whole, and on the card for good: after a power cut the status file is all the
    updater has to go on (``unfinished``). Each writer has a temporary file of its own: the daily look, Check for
    updates and the updater can write at the same moment, and with one shared name the second found it gone."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.stem}-", suffix=".tmp")
    try:
        with os.fdopen(handle, "w") as file:
            file.write(json.dumps(what))
            file.flush()
            os.fsync(file.fileno())
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    _sync_folder(path.parent)


def _sync_folder(folder: Path) -> None:
    """Flush the rename to the card too (bongo/durable.py does the same; the updater uses only Python's own library)."""
    try:
        fd = os.open(folder, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def status(data: Path = None) -> dict | None:
    try:
        said = json.loads(status_path(data).read_text())
    except Exception:   # unreadable, half-written or nonsense (a file nested too deep to read is a RecursionError)
        return None
    return said if isinstance(said, dict) else None


STALE_SECONDS = 600  # a request or step older than this was never picked up (no updater installed?)


def running(data: Path = None) -> bool:
    """Whether the updater is at work right now (it holds data/update.lock from start to finish)."""
    try:
        with open((data or DATA) / "update.lock", "a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                return True
            fcntl.flock(lock, fcntl.LOCK_UN)
    except OSError:
        pass
    return False


def busy(data: Path = None, now: dt.datetime | None = None) -> bool:
    s = status(data) or {}
    if s.get("state") not in ("requested", "fetching", "installing", "restarting"):
        return False
    if running(data):   # however long it has been: pip on a Pi Zero can take more than ten minutes, and a second
        return True     # request then would wipe the note a power cut needs to pick the update up again
    try:
        age = ((now or dt.datetime.now(dt.timezone.utc)) - dt.datetime.fromisoformat(s["at"])).total_seconds()
    except (KeyError, TypeError, ValueError):
        return False
    return age < STALE_SECONDS


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def restart_service() -> None:
    subprocess.run(["systemctl", "restart", SERVICE], check=True, timeout=60)


def dashboard_answers(port: int):
    """What the display says of itself: its /api/health answer; True from a version too old to have
    one (its dashboard answering is all there is to go on); None when nothing answers."""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=3) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as error:
        return True if error.code == 404 else None
    except (OSError, ValueError):
        return None


def healthy(port: int, seconds: float = HEALTH_SECONDS, check=dashboard_answers, sleep=time.sleep,
            hold: float = HEALTH_HOLD, need_frames: bool = True) -> bool:
    """True once the restarted display has run for ``hold`` seconds on end: the same run of it (not
    one that crashes and is started again), its panel started, its player showing frames. The
    dashboard alone answering isn't enough: it starts before the panel does."""
    waited, good, run = 0.0, None, None
    while waited < seconds:
        answer = check(port)
        if isinstance(answer, dict):
            fine = answer.get("panel") is True and isinstance(answer.get("quiet"), (int, float)) \
                and answer["quiet"] < HEALTH_QUIET
            if fine and need_frames and "frames" in answer:
                # frames really shown, and lately: "quiet" alone stays low for 45 s after start-up with no frame
                # at all, and for ever while the player waits, alive, with nothing it can play
                age = answer.get("frame_age")
                fine = bool(answer["frames"]) and isinstance(age, (int, float)) and age < HEALTH_QUIET
            if not fine:
                good = None
            elif good is None or answer.get("boot") != run:
                good = waited                                # good from now; a new run starts the count again
            run = answer.get("boot")
        elif answer:                                         # an older version: its dashboard answers
            good, run = (waited if good is None or run is not None else good), None
        else:
            good = None
        if good is not None and waited - good >= hold:
            return True
        sleep(2)
        waited += 2
    return False


def unfinished(data: Path = None) -> tuple[str, str] | None:
    """(the commit before, the commit it was moving to) of an update that never reported how it ended."""
    s = status(data) or {}
    if s.get("state") == "installing":
        before, target = s.get("current"), s.get("target")
    elif s.get("state") == "restarting":
        before, target = s.get("previous"), s.get("current")
    else:
        return None
    ok = all(isinstance(c, str) and re.fullmatch(r"[0-9a-f]{40}", c) for c in (before, target))
    return (before, target) if ok else None


def resume(data: Path = None) -> bool:
    """Called as the display starts. If an update was cut short (the power went between the new code
    going in and the check that it runs), ask the updater to run again; it finishes the job or puts
    the old version back. True if it asked. While the updater itself is running (it has just
    restarted us), there is nothing to do."""
    data = data or DATA
    if unfinished(data) is None:
        return False
    with open(data / "update.lock", "a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return False                                     # the updater is at work, and watching us
        request_path(data).write_text(json.dumps({"at": _now(), "by": "resume"}))   # the status stays: it says where to pick up
    return True


def _noted(newest: str, url: str, branch: str, data: Path) -> None:
    try:
        note(newest, url, branch, data)
    except OSError as error:   # a note for the dashboard: never a reason for an update to fail
        log.info("couldn't note the newest version: %s", logged(error))


def _install(old: str, new: str, repo: Path, pip) -> None:
    if old and _git("diff", "--name-only", old, new, "--", "requirements.txt", repo=repo):
        pip(repo / "requirements.txt")


def pip_install(requirements: Path) -> None:
    subprocess.run([sys.executable, "-m", "pip", "install", "--quiet", "-r", str(requirements)],
                   check=True, timeout=900)


def apply(config: dict, repo: Path = REPO, data: Path = None, restart=restart_service, health=None,
          pip=pip_install) -> dict:
    """Update the checkout to the configured branch, restart, and roll back if it won't start."""
    data = data or (DATA if repo == REPO else repo / "data")
    url, branch = source(config)
    port = (config.get("web") or {}).get("port", 5000)
    if health is None:
        before = dashboard_answers(port)   # was the version being replaced showing frames? If it wasn't (a config
        dark = isinstance(before, dict) and before.get("frames") == 0   # that can't play), the new one needn't either
        health = lambda: healthy(port, need_frames=not dark)   # noqa: E731
    data.mkdir(parents=True, exist_ok=True)
    with open(data / "update.lock", "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return report("busy", "An update is already running", data=data)
        request_path(data).unlink(missing_ok=True)
        head = old = _git("rev-parse", "HEAD", repo=repo)
        cut_short = unfinished(data)   # the last update never finished (power cut): pick it up, with its rollback
        if cut_short:
            try:
                _git("cat-file", "-e", f"{cut_short[0]}^{{commit}}", repo=repo)
                old = cut_short[0]
            except RuntimeError:
                cut_short = None
        try:
            if not cut_short:          # (its status says where it stopped: keep that until this run gets further)
                report("fetching", f"Fetching {branch} from {shown(url)}", data=data, current=old)
            _git("fetch", "--quiet", "--", url, f"refs/heads/{branch}", repo=repo, hide=(url, branch))
            new = _git("rev-parse", "FETCH_HEAD", repo=repo)
            _noted(new, url, branch, data)   # what the Software card and the hub then say is ready (or isn't)
        except (RuntimeError, OSError, subprocess.TimeoutExpired) as error:
            log.info("couldn't fetch the update: %s", logged(error))
            if not cut_short:
                return report("failed", f"Couldn't fetch the update: {why(error)}", data=data, current=old)
            new = cut_short[1]         # no network now: finish with what was fetched then
        if new == head and not cut_short:
            return report("up_to_date", "Already up to date", data=data, current=old)
        try:
            report("installing", f"Installing {new[:8]}", data=data, current=old, target=new)
            _git("reset", "--hard", "--quiet", new, repo=repo)
            _install(old if new != old else head, new, repo, pip)
            report("restarting", "Restarting the display", data=data, current=new, previous=old)
            restart()
            if health():
                return report("updated", f"Updated to {new[:8]}", data=data, current=new, previous=old)
            reason = "the new version didn't start"
        except Exception as error:  # anything at all: put the old version back
            reason = why(error)
            log.info("the update to %s didn't go in: %s", new[:8], logged(error))
        try:
            _git("reset", "--hard", "--quiet", old, repo=repo)
            stuck = None
        except Exception as error:  # git can't even do that (a lock left behind, a read-only card): say so, never raise
            stuck = why(error)
            log.error("couldn't put the old version back: %s", logged(error))
        try:
            _install(new, old, repo, pip)
            restart()
        except Exception:
            pass
        if stuck is None:
            return report("rolled_back", f"Update to {new[:8]} rolled back: {reason}", data=data,
                          current=old, failed=new)
        try:
            now = _git("rev-parse", "HEAD", repo=repo)
        except Exception:
            now = None
        if now == old:   # (it never left the old version: the same trouble stopped the new one going in)
            return report("failed", f"Update to {new[:8]} failed: {reason}. The old version is still in place", data=data,
                          current=old, failed=new)
        return report("failed", f"Update to {new[:8]} failed: {reason}. The old version couldn't be put back: {stuck}",
                      data=data, current=now or new, failed=new)
