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
"""
from __future__ import annotations

import datetime as dt
import fcntl
import json
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DATA = REPO / "data"
DEFAULT_URL = "http://gitlab.home.lan/matrix_display/bongo_matrix.git"
DEFAULT_BRANCH = "prod_device"   # only the files the display needs (see tools/prod_tree.py)
SERVICE = "bongo-matrix"
HEALTH_SECONDS = 150   # for the restarted display to come good (a Pi Zero takes a while to start)
HEALTH_HOLD = 20       # ...and how long it must stay good: a version that crashes at start-up restarts every 3 s
HEALTH_QUIET = 45      # the longest gap between frames that still counts as playing (a big file being decoded)
GIT_TIMEOUT = 120

URL = re.compile(r"(https?|ssh)://[^\s]+|[\w.-]+@[\w.-]+:[^\s]+")
BRANCH = re.compile(r"[A-Za-z0-9._/-]{1,100}")


def request_path(data: Path = None) -> Path:
    return (data or DATA) / "update-request.json"


def status_path(data: Path = None) -> Path:
    return (data or DATA) / "update-status.json"


def source(config: dict) -> tuple[str, str]:
    rule = config.get("update") or {}
    return rule.get("url") or DEFAULT_URL, rule.get("branch") or DEFAULT_BRANCH


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


def _git(*args, repo: Path = REPO, timeout: float = GIT_TIMEOUT) -> str:
    # Both services run as root but the checkout belongs to the Pi's user, and git refuses to work in
    # someone else's repository ("dubious ownership") unless told that this one folder is trusted.
    done = subprocess.run(["git", "-c", f"safe.directory={Path(repo).resolve()}", "-C", str(repo), *args],
                          capture_output=True, text=True, timeout=timeout)
    if done.returncode:
        raise RuntimeError((done.stderr or done.stdout).strip().splitlines()[-1] if (done.stderr or done.stdout)
                           else f"git {args[0]} failed")
    return done.stdout.strip()


def version(repo: Path = REPO) -> dict | None:
    """The running code's commit, date and subject, or None outside a git checkout."""
    try:
        sha, date, subject = _git("log", "-1", "--format=%H%x00%cI%x00%s", repo=repo, timeout=10).split("\x00")
    except (RuntimeError, OSError, subprocess.TimeoutExpired, ValueError):
        return None
    return {"commit": sha, "short": sha[:8], "date": date, "subject": subject}


def latest(url: str, branch: str, repo: Path = REPO) -> str:
    """The newest commit on the update branch (asks the server; changes nothing here)."""
    out = _git("ls-remote", "--", url, f"refs/heads/{branch}", repo=repo, timeout=30)
    if not out:
        raise RuntimeError(f"no branch {branch!r} at {url}")
    return out.split()[0]


def ask(by: str | None = None, data: Path = None) -> None:
    """Called by the dashboard: leave a request for the update service."""
    path = request_path(data)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"at": _now(), "by": by}))
    report("requested", "Waiting for the updater to start", data=data)


def report(state: str, message: str, data: Path = None, **extra) -> dict:
    status = {"state": state, "message": message, "at": _now(), **extra}
    path = status_path(data)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(status))
    tmp.replace(path)
    return status


def status(data: Path = None) -> dict | None:
    try:
        return json.loads(status_path(data).read_text())
    except (OSError, ValueError):
        return None


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


def _install(old: str, new: str, repo: Path, pip) -> None:
    if old and _git("diff", "--name-only", old, new, "--", "requirements.txt", repo=repo):
        pip(repo / "requirements.txt")


def pip_install(requirements: Path) -> None:
    subprocess.run([sys.executable, "-m", "pip", "install", "--quiet", "-r", str(requirements)],
                   check=True, timeout=900)


def apply(config: dict, repo: Path = REPO, data: Path = None, restart=restart_service, health=None,
          pip=pip_install) -> dict:
    """Update the checkout to the configured branch, restart, and roll back if it won't start."""
    data = data or repo / "data"
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
                report("fetching", f"Fetching {branch} from {url}", data=data, current=old)
            _git("fetch", "--quiet", "--", url, f"refs/heads/{branch}", repo=repo)
            new = _git("rev-parse", "FETCH_HEAD", repo=repo)
        except (RuntimeError, OSError, subprocess.TimeoutExpired) as error:
            if not cut_short:
                return report("failed", f"Couldn't fetch the update: {error}", data=data, current=old)
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
            reason = str(error) or type(error).__name__
        _git("reset", "--hard", "--quiet", old, repo=repo)
        try:
            _install(new, old, repo, pip)
            restart()
        except Exception:
            pass
        return report("rolled_back", f"Update to {new[:8]} rolled back: {reason}", data=data,
                      current=old, failed=new)
