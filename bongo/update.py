"""Pull updates from a git repository, with an automatic rollback if the new version won't start.

The display service can only write to data/ (see deploy/bongo-matrix.service), so it never
replaces its own code. The dashboard writes data/update-request.json; deploy/bongo-update.path
sees it and starts deploy/bongo-update.service, which runs ``python -m bongo update --apply``:

    fetch <url> <branch>  ->  reset the checkout to it  ->  pip install if requirements.txt changed
    ->  restart bongo-matrix  ->  wait for the dashboard to answer  ->  done
    (no answer within HEALTH_SECONDS: reset back to the old commit, restart, report "rolled back")

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
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DATA = REPO / "data"
DEFAULT_URL = "http://gitlab.home.lan/matrix_display/bongo_matrix.git"
DEFAULT_BRANCH = "prod_device"   # only the files the display needs (see tools/prod_tree.py)
SERVICE = "bongo-matrix"
HEALTH_SECONDS = 90
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
    done = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=timeout)
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


def busy(data: Path = None, now: dt.datetime | None = None) -> bool:
    s = status(data) or {}
    if s.get("state") not in ("requested", "fetching", "installing", "restarting"):
        return False
    try:
        age = ((now or dt.datetime.now(dt.timezone.utc)) - dt.datetime.fromisoformat(s["at"])).total_seconds()
    except (KeyError, TypeError, ValueError):
        return False
    return age < STALE_SECONDS


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def restart_service() -> None:
    subprocess.run(["systemctl", "restart", SERVICE], check=True, timeout=60)


def dashboard_answers(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/pair/status", timeout=3) as r:
            return r.status == 200
    except OSError:
        return False


def healthy(port: int, seconds: float = HEALTH_SECONDS, check=dashboard_answers, sleep=time.sleep) -> bool:
    """True once the restarted display's dashboard answers (it starts after the config loads)."""
    waited = 0.0
    while waited < seconds:
        if check(port):
            return True
        sleep(2)
        waited += 2
    return False


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
    health = health or (lambda: healthy(port))
    data.mkdir(parents=True, exist_ok=True)
    with open(data / "update.lock", "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return report("busy", "An update is already running", data=data)
        request_path(data).unlink(missing_ok=True)
        old = _git("rev-parse", "HEAD", repo=repo)
        try:
            report("fetching", f"Fetching {branch} from {url}", data=data, current=old)
            _git("fetch", "--quiet", "--", url, f"refs/heads/{branch}", repo=repo)
            new = _git("rev-parse", "FETCH_HEAD", repo=repo)
        except (RuntimeError, OSError, subprocess.TimeoutExpired) as error:
            return report("failed", f"Couldn't fetch the update: {error}", data=data, current=old)
        if new == old:
            return report("up_to_date", "Already up to date", data=data, current=old)
        try:
            report("installing", f"Installing {new[:8]}", data=data, current=old, target=new)
            _git("reset", "--hard", "--quiet", new, repo=repo)
            _install(old, new, repo, pip)
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
