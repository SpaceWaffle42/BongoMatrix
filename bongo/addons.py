"""Modules imported from a public GitHub link or a .zip (Settings > Modules > Import).

A shared module is a folder built exactly like one in bongo/modules/ (docs/MODULES.md), plus a
``bongo-module.json`` at the top:

    {"id": "tube_departures", "name": "London Tube departures", "version": "1.2.0", "author": "sam-k",
     "description": "...", "network": ["api.tfl.gov.uk"], "homepage": "https://github.com/sam-k/..."}

Importing: fetch it (``fetch_github`` / a .zip), check the folder and the manifest (``unpack``), run every
style on a test screen in its child process (``try_out``) and show what it looks like. Nothing is kept until
the owner ticks "I trust <author>" and presses Install (``install``). Imported modules live in
data/modules/<id>/; what's known about each (where it came from, its description, its settings) is in
data/modules.json. They run only in their own child process (bongo/sandbox), never in the Matrix's.
"""
from __future__ import annotations

import base64
import datetime as dt
import io
import json
import logging
import os
import re
import shutil
import stat
import tempfile
import threading
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

log = logging.getLogger("bongo.addons")

DATA = Path(os.environ.get("BONGO_ADDONS", Path(__file__).resolve().parents[1] / "data"))
MANIFEST = "bongo-module.json"
MOST_DOWNLOAD = 5 << 20          # a .zip or GitHub download
MOST_UNPACKED = 20 << 20
MOST_FILES = 500
FIRST_UID = 61000                # each imported module runs as a user of its own: 61000 + n
ID = re.compile(r"[a-z][a-z0-9_]{1,31}")
SITE = re.compile(r"(\*\.)?[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)*\.[a-z][a-z0-9-]*[a-z0-9](:\d{1,5})?")
# "*." can't cover a whole public suffix or a hosting service where anyone can have a name
TOO_WIDE = {"co.uk", "org.uk", "ac.uk", "gov.uk", "com.au", "co.nz", "co.jp", "com.br", "co.za", "co.in",
            "github.io", "gitlab.io", "herokuapp.com", "vercel.app", "netlify.app", "pages.dev", "workers.dev",
            "cloudfront.net", "amazonaws.com", "azurewebsites.net", "appspot.com", "web.app", "firebaseapp.com",
            "blogspot.com", "glitch.me", "repl.co", "fly.dev", "onrender.com", "ngrok.io", "ngrok-free.app",
            "trycloudflare.com", "cloudflare.net", "r2.dev", "azureedge.net", "googleusercontent.com",
            "storage.googleapis.com", "digitaloceanspaces.com", "pythonanywhere.com", "railway.app", "deno.dev",
            "surge.sh", "neocities.org", "000webhostapp.com", "duckdns.org", "no-ip.org", "dyndns.org",
            "ngrok.app", "ngrok.dev", "loca.lt", "serveo.net", "localtunnel.me", "blob.core.windows.net",
            "web.core.windows.net", "cloudapp.net", "elasticbeanstalk.com", "run.app", "a.run.app", "lambda-url.on.aws",
            "ngrok-free.dev", "pinggy.link", "devtunnels.ms"}   # (not every such service: the review says what "*." means)
GITHUB = re.compile(r"https?://(?:www\.)?github\.com/([A-Za-z0-9-]{1,39})/([A-Za-z0-9._-]{1,100}?)(?:\.git)?"
                    r"(?:/(tree|releases/tag|releases|archive/refs/heads|archive/refs/tags|archive)(?:/(.+?))?)?/?(?:\.zip)?$")
USER_AGENT = "bongo-matrix (module import)"
TRY_WAIT = 60.0                  # s for the whole test screen


class ImportProblem(Exception):
    """Something the owner is told, as it is."""


# -- the store ---------------------------------------------------------------------------------------------
def folder() -> Path:
    return DATA / "modules"


def state_file() -> Path:
    return DATA / "modules.json"


def work_dir(module_id: str) -> Path:
    return DATA / "module-data" / module_id


_lock = threading.RLock()


def load_state() -> dict:
    try:
        state = json.loads(state_file().read_text())
        return state if isinstance(state.get("modules"), dict) else {"modules": {}}
    except (OSError, ValueError, AttributeError):
        return {"modules": {}}


def save_state(state: dict) -> None:
    path = state_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".modules-")
    with os.fdopen(fd, "w") as f:
        json.dump(state, f, indent=1)
    os.replace(tmp, path)


def installed() -> dict[str, dict]:
    return load_state()["modules"]


def spec_for(module_id: str, info: dict) -> dict:
    """How the module's child is started (bongo.sandbox.remote)."""
    return {"id": module_id, "dir": str(folder() / module_id), "data": str(work_dir(module_id)), "uid": info["uid"],
            "net": "gate" if info.get("only_listed", True) else "open", "sites": info.get("sites", [])}


def register(modules: dict) -> None:
    """Add every installed module to ``modules`` (from its description: nothing of it runs until it's played)."""
    from bongo.sandbox import remote

    for module_id, info in installed().items():
        if module_id in modules and not getattr(modules[module_id], "_imported", False):
            log.warning("imported module %s clashes with a built-in module: skipped", module_id)
            continue
        try:
            module = remote.build(info["described"], spec_for(module_id, info))
        except (KeyError, TypeError, ValueError) as error:
            log.warning("imported module %s couldn't be listed: %s", module_id, error)
            continue
        object.__setattr__(module, "_imported", True)
        modules[module_id] = module


def is_imported(module) -> bool:
    return getattr(module, "_imported", False)


def public(module_id: str, info: dict, blocked: list[str] | None = None) -> dict:
    """What the dashboard shows of an imported module."""
    keys = ("name", "version", "author", "description", "homepage", "source", "sites", "installed", "update")
    return {"id": module_id, **{k: info.get(k) for k in keys}, "only_listed": info.get("only_listed", True),
            "blocked": blocked or []}


# -- checking a module's folder ----------------------------------------------------------------------------
def check_manifest(data, builtin_ids=()) -> dict:
    if not isinstance(data, dict):
        raise ImportProblem(f"{MANIFEST} isn't a JSON object")
    module_id = data.get("id")
    if not isinstance(module_id, str) or not ID.fullmatch(module_id):
        raise ImportProblem(f'{MANIFEST}: "id" must be 2 to 32 lower-case letters, digits or _, starting with a letter')
    if module_id in builtin_ids:
        raise ImportProblem(f'{MANIFEST}: "{module_id}" is the name of a module the Matrix already has; it needs an id of its own')
    out = {"id": module_id}
    for key, most, needed in (("name", 40, True), ("version", 20, True), ("author", 60, True), ("description", 300, False)):
        value = data.get(key)
        if value is None and not needed:
            continue
        if not isinstance(value, str) or not value.strip() or len(value) > most:
            raise ImportProblem(f'{MANIFEST}: "{key}" must be some text (up to {most} characters)')
        out[key] = value.strip()
    sites = data.get("network", [])
    if not isinstance(sites, list) or len(sites) > 20 or not all(isinstance(s, str) and SITE.fullmatch(s.lower()) for s in sites):
        raise ImportProblem(f'{MANIFEST}: "network" must be a list of up to 20 site names, like "api.example.com" or "*.example.com"')
    for site in sites:
        base = site.lower().split(":")[0]
        if base.startswith("*.") and (base.count(".") < 2 or any(base[2:] == w or base[2:].endswith("." + w) for w in TOO_WIDE)):
            raise ImportProblem(f'{MANIFEST}: "network": {site} is too wide; list the sites it uses')
    out["sites"] = sorted({s.lower() for s in sites})
    home = data.get("homepage")
    if home is not None:
        if not isinstance(home, str) or not home.startswith("https://") or len(home) > 200:
            raise ImportProblem(f'{MANIFEST}: "homepage" must be an https:// link')
        out["homepage"] = home
    return out


def unpack(archive: Path, dest: Path, builtin_ids=()) -> dict:
    """Unpack a module's .zip into ``dest`` (its top folder, if it has one, dropped) and check it."""
    try:
        zf = zipfile.ZipFile(archive)
    except (zipfile.BadZipFile, OSError):
        raise ImportProblem("that isn't a .zip file") from None
    with zf:
        entries = [e for e in zf.infolist() if not e.is_dir()]
        if len(entries) > MOST_FILES:
            raise ImportProblem(f"it has more than {MOST_FILES} files")
        if sum(e.file_size for e in entries) > MOST_UNPACKED:
            raise ImportProblem(f"it's more than {MOST_UNPACKED >> 20} MB unpacked")
        for e in entries:
            if (e.filename.startswith("/") or ".." in Path(e.filename).parts or "\\" in e.filename or ":" in e.filename
                    or stat.S_ISLNK(e.external_attr >> 16)):
                raise ImportProblem(f"it has a file it can't unpack safely: {e.filename[:80]}")
        names = [e.filename for e in entries]
        top = ""
        if MANIFEST not in names:
            tops = {n.split("/", 1)[0] for n in names}
            if len(tops) == 1 and f"{next(iter(tops))}/{MANIFEST}" in names:
                top = next(iter(tops)) + "/"
            else:
                raise ImportProblem(f"it isn't a Matrix module: there's no {MANIFEST} at the top")
        dest.mkdir(parents=True, exist_ok=True)
        for e in entries:
            target = dest / e.filename[len(top):]
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(e) as src, open(target, "wb") as out:
                shutil.copyfileobj(src, out)
    try:
        manifest = json.loads((dest / MANIFEST).read_text())
    except (OSError, ValueError):
        raise ImportProblem(f"its {MANIFEST} isn't valid JSON") from None
    manifest = check_manifest(manifest, builtin_ids)
    if not (dest / "__init__.py").is_file():
        raise ImportProblem("it has no __init__.py beside its " + MANIFEST)
    return manifest


# -- fetching from GitHub ----------------------------------------------------------------------------------
def _get(url: str, most: int = MOST_DOWNLOAD, accept: str | None = None) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **({"Accept": accept} if accept else {})})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            data = r.read(most + 1)
    except urllib.error.HTTPError as error:
        if error.code == 404:
            raise FileNotFoundError(url) from None
        if error.code in (403, 429):
            raise ImportProblem("GitHub is limiting requests from this network for now; try again in an hour") from None
        raise ImportProblem(f"GitHub answered {error.code}") from None
    except (urllib.error.URLError, OSError) as error:
        raise ImportProblem(f"couldn't reach GitHub ({getattr(error, 'reason', error)})") from None
    if len(data) > most:
        raise ImportProblem(f"it's bigger than {most >> 20} MB")
    return data


def parse_github(url: str) -> tuple[str, str, str | None]:
    """(owner, repo, ref) from a GitHub link; ref None = its latest release, or its main branch if it has none."""
    m = GITHUB.match(url.strip())
    if not m:
        raise ImportProblem("that isn't a GitHub link. It should look like github.com/owner/repo")
    owner, repo, kind, rest = m.groups()
    ref = None
    if kind and rest:
        ref = rest.removesuffix(".zip")
        if kind == "releases" and ref == "latest":
            ref = None
        elif kind == "releases":
            raise ImportProblem("use the link to one release (…/releases/tag/v1.0) or the repo")
    return owner, repo, ref


def fetch_github(url: str) -> tuple[bytes, dict]:
    owner, repo, ref = parse_github(url)
    api = f"https://api.github.com/repos/{owner}/{repo}"
    if ref is None:
        try:
            ref = json.loads(_get(f"{api}/releases/latest", 1 << 20, "application/vnd.github+json"))["tag_name"]
            kind = "release"
        except FileNotFoundError:
            try:
                ref = json.loads(_get(api, 1 << 20, "application/vnd.github+json"))["default_branch"]
            except FileNotFoundError:
                raise ImportProblem("there's no public GitHub repo there") from None
            kind = "branch"
    else:
        kind = "ref"
        try:                                             # a branch (followed for updates) rather than a tag or commit?
            _get(f"{api}/branches/{ref}", 1 << 20, "application/vnd.github+json")
            kind = "branch"
        except (FileNotFoundError, ImportProblem):
            pass
    try:
        data = _get(f"https://codeload.github.com/{owner}/{repo}/zip/{ref}")
    except FileNotFoundError:
        raise ImportProblem(f"couldn't find {ref!r} in {owner}/{repo}") from None
    return data, {"kind": "github", "url": f"https://github.com/{owner}/{repo}", "repo": f"{owner}/{repo}", "ref": ref, "ref_kind": kind}


# -- trying it out -----------------------------------------------------------------------------------------
LAST_UID = 64999
_tried = [0]


def free_uid(state: dict, trying: bool = False) -> int:
    """A user for a module's child that no other module has, and that nothing is still running as. Tries (the
    test screen) get one of their own each time, so code that hasn't been trusted yet never runs as an
    installed module's user."""
    from bongo.sandbox import jail

    used = {info.get("uid") for info in state["modules"].values()}
    start = FIRST_UID + 1 + (2000 + _tried[0] % 1000 if trying else 0)
    if trying:
        _tried[0] += 1
    n = start
    while n in used or jail.can_shut_in() and any(True for _ in jail._processes_of(n)):
        n += 1
        if n > LAST_UID:
            n = FIRST_UID + 1
        if n == start:
            raise ImportProblem("too many modules")
    return n


def try_out(dest: Path, manifest: dict, uid: int) -> tuple[dict, str]:
    """Run every style on a test screen in the module's child: its description, and a GIF of its first style."""
    from bongo.module import Context
    from bongo.sandbox import remote

    module_id = manifest["id"]
    spec = {"id": f"try_{module_id}"[:40], "module_id": module_id, "dir": str(dest), "data": str(dest.parent / "work"), "uid": uid,
            "net": "gate", "sites": manifest["sites"]}
    child = remote.Child(spec)
    started = time.monotonic()
    try:
        with child.lock:
            child._start()
            described = child.described
        if described.get("id") != module_id:
            raise ImportProblem("its MODULE.id doesn't match its " + MANIFEST)
        if described.get("name") != manifest["name"]:
            raise ImportProblem(f"its MODULE.name ({described.get('name')!r}) doesn't match the name in its {MANIFEST}")
        module = remote.build(described, spec)
        remote.CHILDREN[spec["id"]] = child
        when = dt.datetime.now().astimezone()
        frames = []
        for size in ((64, 32), (56, 26)):                   # the whole panel, and a panel with a frame round it
            for name in module.styles:
                if time.monotonic() - started > TRY_WAIT:
                    raise ImportProblem("it took too long to draw its screens")
                style = module.create(name, None, Context(width=size[0], height=size[1], preview=True))
                if style.handle is None:
                    raise ImportProblem(f"its “{name.replace('_', ' ').capitalize()}” look didn't start ({child.error or 'see the Logs page'})")
                for k in range(30 if (size == (64, 32) and name == module.default_style) else 2):
                    frame = style.render(when + dt.timedelta(seconds=k / 10))
                    if style.handle is None:
                        raise ImportProblem(f"its “{name.replace('_', ' ').capitalize()}” look stopped working ({child.error or 'see the Logs page'})")
                    if size == (64, 32) and name == module.default_style:
                        frames.append(frame)
        return described, preview_gif(frames)
    except remote.ChildError as error:
        raise ImportProblem(f"it didn't load: {error}") from None
    finally:
        remote.CHILDREN.pop(spec["id"], None)
        with child.lock:
            child.stop()
        from bongo.sandbox.gate import gate
        gate().forget(spec["id"])


def preview_gif(frames, scale: int = 5) -> str:
    big = [f.resize((f.width * scale, f.height * scale), 0) for f in frames]
    buf = io.BytesIO()
    big[0].save(buf, "GIF", save_all=True, append_images=big[1:], duration=100, loop=0)
    return "data:image/gif;base64," + base64.b64encode(buf.getvalue()).decode()


# -- an import, step by step (the dashboard polls it) ------------------------------------------------------
class Job:
    STEPS = ("fetch", "check", "try", "ready")

    def __init__(self, token: str, source: str):
        self.token, self.source = token, source
        self.step, self.state, self.message = "fetch", "working", ""
        self.manifest = self.described = self.preview = self.origin = None
        self.dir = None
        self.updating = False
        self.new_sites = []
        self.only_listed = True
        self.running = True          # its thread is still at work (even if cancelled)
        self.cancelled = False
        self.made = time.monotonic()

    def public(self) -> dict:
        out = {"token": self.token, "step": self.step, "state": self.state, "message": self.message, "source": self.source}
        if self.state == "ready":
            from bongo.sandbox.jail import can_shut_in
            out.update(manifest=self.manifest, preview=self.preview, updating=self.updating, new_sites=self.new_sites,
                       only_listed=self.only_listed, enforced=can_shut_in(),
                       styles=list(self.described["styles"]), origin=self.origin)
        return out


JOBS: dict[str, Job] = {}


def staging() -> Path:
    path = DATA / "module-staging"
    path.mkdir(parents=True, exist_ok=True)
    return path


def start(source: str, *, url: str | None = None, upload: Path | None = None, upload_name: str = "",
          builtin_ids=(), update_of: str | None = None) -> Job:
    """Fetch, check and try a module in the background; ``JOBS[token]`` says how it's going."""
    for token, job in list(JOBS.items()):               # an hour later, an import nobody finished is dropped
        if time.monotonic() - job.made > 3600:
            drop(token)
    token = base64.urlsafe_b64encode(os.urandom(9)).decode()
    job = JOBS[token] = Job(token, source)
    work = staging() / token
    work.mkdir()

    def run():
        try:
            if url:
                data, job.origin = fetch_github(url)
                archive = work / "download.zip"
                archive.write_bytes(data)
            else:
                archive = upload
                job.origin = {"kind": "zip", "name": upload_name[:80]}
            job.step = "check"
            dest = work / "module"
            job.manifest = unpack(archive, dest, builtin_ids)
            job.dir = dest
            state = load_state()
            have = state["modules"].get(job.manifest["id"])
            if update_of and job.manifest["id"] != update_of:
                raise ImportProblem(f"that's a different module ({job.manifest['id']}), not an update of {update_of}")
            if update_of and have and (have.get("source") or {}).get("ref_kind") == "branch":
                job.origin["ref_kind"] = "branch"        # still following its branch, for the next update
            job.updating = have is not None
            job.only_listed = have.get("only_listed", True) if have else True
            job.new_sites = sorted(set(job.manifest["sites"]) - set(have.get("sites", []))) if have else []
            if job.cancelled:
                return
            job.step = "try"
            job.described, job.preview = try_out(dest, job.manifest, free_uid(state, trying=True))
            job.step, job.state = "ready", "ready"
        except ImportProblem as error:
            job.state, job.message = "failed", str(error)
        except Exception:  # noqa: BLE001 - shown as a plain failure; the details are in the log
            log.exception("module import failed")
            job.state, job.message = "failed", "something went wrong (see the Logs page)"
        finally:
            job.running = False
            if upload is not None:
                upload.unlink(missing_ok=True)
            if job.cancelled:                            # cancelled while it worked: tidy up now it's done
                shutil.rmtree(work, ignore_errors=True)

    threading.Thread(target=run, name="module-import", daemon=True).start()
    return job


def busy() -> bool:
    """An import is still at work (one at a time: each tries a module out in a child process)."""
    return any(job.running for job in JOBS.values()) or any(job.running for job in _cancelled)


_cancelled: list[Job] = []


def drop(token: str) -> None:
    job = JOBS.pop(token, None)
    if job is not None and job.running:                  # its thread tidies up when it finishes
        job.cancelled = True
        _cancelled[:] = [j for j in _cancelled if j.running] + [job]
        return
    shutil.rmtree(staging() / token, ignore_errors=True)


def install(token: str, modules: dict) -> dict:
    """Keep a module that's been tried out: it goes in data/modules/<id> and into ``modules``."""
    from bongo.sandbox import remote

    job = JOBS.get(token)
    if job is None or job.state != "ready":
        raise ImportProblem("that import has expired; start it again")
    module_id = job.manifest["id"]
    with _lock:
        state = load_state()
        have = state["modules"].get(module_id, {})
        if module_id in modules and not is_imported(modules[module_id]):
            raise ImportProblem(f"the Matrix already has a module called {module_id}")
        remote.stop(module_id)
        target = folder() / module_id
        folder().mkdir(parents=True, exist_ok=True)
        old = target.with_name(f".{module_id}-old")
        shutil.rmtree(old, ignore_errors=True)
        if target.exists():
            target.rename(old)
        shutil.move(str(job.dir), target)
        shutil.rmtree(old, ignore_errors=True)
        info = {**job.manifest, "source": job.origin, "described": job.described,
                "installed": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
                "uid": have.get("uid") or free_uid(state), "only_listed": have.get("only_listed", True)}
        info.pop("id")
        info.pop("update", None)
        state["modules"][module_id] = info
        save_state(state)
    drop(token)
    module = remote.build(info["described"], spec_for(module_id, info))
    object.__setattr__(module, "_imported", True)
    modules[module_id] = module
    log.info("module %s %s installed from %s", module_id, info["version"], (job.origin or {}).get("url") or (job.origin or {}).get("name"))
    return info


def set_only_listed(module_id: str, only_listed: bool, modules: dict) -> dict:
    from bongo.sandbox import remote

    with _lock:
        state = load_state()
        info = state["modules"].get(module_id)
        if info is None:
            raise KeyError(module_id)
        info["only_listed"] = bool(only_listed)
        save_state(state)
    remote.stop(module_id)
    module = remote.build(info["described"], spec_for(module_id, info))
    object.__setattr__(module, "_imported", True)
    modules[module_id] = module
    log.warning("module %s: %s", module_id, "only the sites it lists" if only_listed else "ANY site, and the home network")
    return info


def remove(module_id: str, modules: dict) -> None:
    from bongo.sandbox import remote
    from bongo.sandbox.gate import gate

    with _lock:
        state = load_state()
        if state["modules"].pop(module_id, None) is None:
            raise KeyError(module_id)
        save_state(state)
    remote.stop(module_id)
    try:
        gate().forget(module_id)
    except OSError:
        pass
    if is_imported(modules.get(module_id)):
        modules.pop(module_id, None)
    shutil.rmtree(folder() / module_id, ignore_errors=True)
    shutil.rmtree(work_dir(module_id), ignore_errors=True)
    log.info("module %s removed", module_id)


# -- updates ----------------------------------------------------------------------------------------------
CHECK_EVERY = 24 * 3600


def update_link(info: dict) -> str | None:
    """Where an update of a module installed from GitHub comes from (None: it can't be updated from there)."""
    src = info.get("source") or {}
    if src.get("kind") != "github":
        return None
    if src.get("ref_kind") == "release":
        return src["url"]
    if src.get("ref_kind") == "branch":
        return f"{src['url']}/tree/{src['ref']}"
    return None


def check_updates() -> list[str]:
    """Look once for a newer version of each module installed from GitHub: a newer release, or a new version
    number on its branch. Nothing is installed; the dashboard offers Update. Returns the ids with one."""
    found = {}
    for module_id, info in installed().items():
        src = info.get("source") or {}
        if src.get("kind") != "github" or "/" not in src.get("repo", ""):
            continue
        try:
            if src.get("ref_kind") == "release":
                tag = json.loads(_get(f"https://api.github.com/repos/{src['repo']}/releases/latest", 1 << 20,
                                      "application/vnd.github+json"))["tag_name"]
                found[module_id] = {"version": str(tag)[:20], "ref": tag} if tag != src.get("ref") else None
            elif src.get("ref_kind") == "branch":
                manifest = json.loads(_get(f"https://raw.githubusercontent.com/{src['repo']}/{src['ref']}/{MANIFEST}", 64 << 10))
                version = str(manifest.get("version", ""))[:20]
                found[module_id] = {"version": version, "ref": src["ref"]} if version and version != info.get("version") else None
        except (ImportProblem, FileNotFoundError, ValueError, KeyError, TypeError) as error:
            log.info("couldn't check %s for an update: %s", module_id, error)
    with _lock:
        state = load_state()
        for module_id, update in found.items():
            if module_id in state["modules"]:
                state["modules"][module_id]["update"] = update
        state["checked"] = dt.datetime.now().astimezone().isoformat(timespec="seconds")
        save_state(state)
    return [k for k, v in found.items() if v]


def keep_checking(stop: threading.Event | None = None) -> threading.Thread:
    """Check for updates a few minutes after start-up, then once a day."""
    stop = stop or threading.Event()

    def run():
        if stop.wait(600):
            return
        while True:
            if installed():
                try:
                    if new := check_updates():
                        log.info("module updates available: %s", ", ".join(new))
                except Exception:  # noqa: BLE001 - never stops the display; tried again tomorrow
                    log.exception("module update check failed")
            if stop.wait(CHECK_EVERY):
                return
    thread = threading.Thread(target=run, name="module-updates", daemon=True)
    thread.start()
    return thread
