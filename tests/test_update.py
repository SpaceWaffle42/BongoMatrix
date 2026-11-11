import datetime as dt
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from bongo import update

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")


def git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True).stdout.strip()


@pytest.fixture
def repos(tmp_path):
    """A bare 'GitLab', an author clone that pushes prod_device, and the device's checkout."""
    origin, author, device = tmp_path / "origin.git", tmp_path / "author", tmp_path / "device"
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
    subprocess.run(["git", "init", "-q", "-b", "prod_device", str(author)], check=True)
    for repo in (author,):
        git(repo, "config", "user.email", "t@t"); git(repo, "config", "user.name", "t")
    (author / "requirements.txt").write_text("pillow\n")
    (author / "app.txt").write_text("v1\n")
    (author / ".gitignore").write_text("data/\n")
    git(author, "add", "-A"); git(author, "commit", "-qm", "v1"); git(author, "push", "-q", str(origin), "prod_device")
    subprocess.run(["git", "clone", "-q", "-b", "prod_device", str(origin), str(device)], check=True)
    (device / "data").mkdir()
    (device / "data" / "config.json").write_text("{}")  # the user's own state: never touched

    def release(text, requirements=None):
        (author / "app.txt").write_text(text)
        if requirements:
            (author / "requirements.txt").write_text(requirements)
        git(author, "commit", "-qam", text.strip()); git(author, "push", "-q", str(origin), "prod_device")
        return git(author, "rev-parse", "HEAD")

    return {"url": str(origin), "device": device, "release": release}


def run(repos, healthy=True, **kw):
    restarts, installs = [], []
    config = {"update": {"url": repos["url"], "branch": "prod_device"}}
    result = update.apply(config, repo=repos["device"], restart=lambda: restarts.append(1),
                          health=lambda: healthy, pip=installs.append, **kw)
    return result, restarts, installs


def test_pulls_the_new_version_and_restarts(repos):
    new = repos["release"]("v2\n")
    result, restarts, installs = run(repos)
    assert result["state"] == "updated" and result["current"] == new
    assert (repos["device"] / "app.txt").read_text() == "v2\n" and restarts == [1] and installs == []
    assert (repos["device"] / "data" / "config.json").read_text() == "{}"
    assert json.loads((repos["device"] / "data" / "update-status.json").read_text())["state"] == "updated"


def test_nothing_new_means_no_restart(repos):
    result, restarts, _ = run(repos)
    assert result["state"] == "up_to_date" and restarts == []


def test_packages_are_installed_only_when_requirements_change(repos):
    repos["release"]("v2\n", requirements="pillow\nnumpy\n")
    _, _, installs = run(repos)
    assert installs == [repos["device"] / "requirements.txt"]


def test_a_version_that_will_not_start_is_rolled_back(repos):
    old = git(repos["device"], "rev-parse", "HEAD")
    bad = repos["release"]("broken\n")
    result, restarts, _ = run(repos, healthy=False)
    assert result["state"] == "rolled_back" and result["current"] == old and result["failed"] == bad
    assert (repos["device"] / "app.txt").read_text() == "v1\n" and restarts == [1, 1]


def test_an_unreachable_source_changes_nothing(repos, tmp_path):
    config = {"update": {"url": str(tmp_path / "nope.git"), "branch": "prod_device"}}
    result = update.apply(config, repo=repos["device"], restart=lambda: pytest.fail("restarted"), health=lambda: True)
    assert result["state"] == "failed" and (repos["device"] / "app.txt").read_text() == "v1\n"


def test_version_and_latest(repos):
    new = repos["release"]("v2\n")
    assert update.latest(repos["url"], "prod_device", repo=repos["device"]) == new
    assert update.version(repos["device"])["commit"] != new
    with pytest.raises(RuntimeError):
        update.latest(repos["url"], "no-such-branch", repo=repos["device"])


@pytest.mark.parametrize("rule,ok", [
    (None, True), ({}, True),
    ({"url": "https://github.com/me/bongo.git", "branch": "prod_device"}, True),
    ({"url": "git@github.com:me/bongo.git"}, True),
    ({"url": "--upload-pack=evil"}, False), ({"url": "file:///etc"}, False), ({"url": "not a url"}, False),
    ({"branch": "-x"}, False), ({"branch": "a..b"}, False), ({"other": 1}, False),
])
def test_update_source_is_checked(rule, ok):
    assert (update.problems(rule) == []) is ok


def test_a_stale_request_does_not_block_a_new_one(tmp_path):
    update.ask("me", tmp_path)
    assert update.busy(tmp_path)
    later = dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=update.STALE_SECONDS + 1)
    assert not update.busy(tmp_path, now=later)


def test_the_dashboard_only_asks(tmp_path, monkeypatch):
    from bongo.live import Live
    from bongo.pairing import Pairing
    from bongo.registry import discover
    from bongo.web import create_app
    pairing = Pairing(tmp_path / "paired.json")
    app = create_app(modules=discover(), live=Live(), pairing=pairing, config_path=tmp_path / "config.json")
    client = app.test_client()
    assert client.post("/api/update", json={}).status_code == 401  # pairing needed
    pairing.show_pin()
    assert client.post("/api/pair", json={"pin": pairing.pin, "name": "t"}).status_code == 200
    info = client.get("/api/update").get_json()
    assert info["url"] == update.DEFAULT_URL and info["branch"] == "prod_device"
    assert client.post("/api/update", json={}).status_code == 202
    assert (tmp_path / "update-request.json").exists()
    assert client.post("/api/update", json={}).status_code == 409  # already asked
    bad = client.put("/api/config", json={"update": {"url": "--evil"}})
    assert bad.status_code == 422
