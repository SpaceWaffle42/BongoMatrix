"""Modules imported from a GitHub link, another link (a GitLab project, a .zip: bongo/linkfetch.py) or a .zip
upload (Settings > Modules > Import).

A shared module is a folder built exactly like one in bongo/modules/ (docs/MODULES.md), plus a
``bongo-module.json`` at the top:

    {"id": "tube_departures", "name": "London Tube departures", "version": "1.2.0", "author": "sam-k",
     "description": "...", "network": ["api.tfl.gov.uk"], "homepage": "https://github.com/sam-k/..."}

A repository may hold several modules, each in a folder of its own (at the top, or one folder down): ``scan``
finds them, the owner picks which (``choose``), and each is checked and tried on its own; each installed one
remembers its folder (``source.folder``), so its updates come from there.

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
import zlib
from pathlib import Path

from bongo import durable, paths

log = logging.getLogger("bongo.addons")

DATA = Path(os.environ.get("BONGO_ADDONS", paths.DATA))
MANIFEST = "bongo-module.json"
MOST_DOWNLOAD = 5 << 20          # a .zip or GitHub download
MOST_UNPACKED = 20 << 20
MOST_FILES = 500
MOST_LISTED = 5000               # entries in a whole repository's archive (each module in it still MOST_FILES)
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


class Busy(ImportProblem):
    """Another import is at work (one at a time)."""


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
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(state, f, indent=1)
        durable.replace(tmp, path)
    except OSError:   # (a full card: nothing half-written is left behind)
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def installed() -> dict[str, dict]:
    return load_state()["modules"]


def spec_for(module_id: str, info: dict) -> dict:
    """How the module's child is started (bongo.sandbox.remote)."""
    from bongo import abilities
    return {"id": module_id, "dir": str(folder() / module_id), "data": str(work_dir(module_id)), "uid": info["uid"],
            "net": "gate" if info.get("only_listed", True) else "open", "sites": info.get("sites", []),
            **({"settings": True} if abilities.has_card(info.get("abilities")) else {}),   # (it has settings of its own)
            "stay": bool((info.get("abilities") or {}).get("stay"))}   # (Style.stay: kept while live only if it asked, and was let)


def apply_home(module_id: str, info: dict | None) -> None:
    """Tell the gate the one home-network address a module may connect to: the one typed into its settings, if
    it asked for one ("abilities": "home_address") and it is installed; otherwise none."""
    from bongo import abilities
    from bongo.sandbox import gate as gates

    home = ((info or {}).get("abilities") or {}).get("home_address")
    values = abilities.STORE.values(module_id, info["abilities"]) if home else {}
    host, port = values.get("address") or None, values.get("port")
    if host and port not in home["ports"]:
        host = None
    if host is None and (gates._gate is None or module_id not in gates._gate.homes):
        return                                   # (nothing to take away: the gate needn't even be started)
    gates.gate().set_home(module_id, host, port)


def register(modules: dict) -> None:
    """Add every installed module to ``modules`` (from its description: nothing of it runs until it's played)."""
    from bongo.sandbox import remote

    if not JOBS:   # the display has just started: an import left unfinished by the last run can't be finished now
        shutil.rmtree(DATA / "module-staging", ignore_errors=True)

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
        try:
            apply_home(module_id, info)
        except OSError as error:
            log.warning("imported module %s: its home-network address couldn't be set: %s", module_id, error)


def is_imported(module) -> bool:
    return getattr(module, "_imported", False)


def public(module_id: str, info: dict, blocked: list[str] | None = None) -> dict:
    """What the dashboard shows of an imported module."""
    from bongo import abilities
    keys = ("name", "version", "author", "description", "homepage", "source", "sites", "installed", "update", "sign_in")
    asked = info.get("abilities") or {}
    return {"id": module_id, **{k: info.get(k) for k in keys}, "only_listed": info.get("only_listed", True),
            "blocked": blocked or [],
            # what it was allowed at import, in words; whether it has a card of its own; what it last said of itself
            "abilities": abilities.words(asked), "background": bool(asked.get("background")),
            "card": abilities.has_card(asked), "screens": bool((info.get("described") or {}).get("styles")),
            "status": abilities.STORE.status(module_id) if asked.get("background") else None}


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
    from bongo import abilities
    try:   # what it asks for beyond drawing its screens: every name must be one this Matrix knows
        asked = abilities.check(data.get("abilities"), data.get("api"))
    except abilities.Newer as error:
        raise ImportProblem(str(error)) from None
    except abilities.Problem as error:
        raise ImportProblem(f"{MANIFEST}: {error}") from None
    if asked:
        out["abilities"] = asked
    return out


def _open(archive: Path) -> zipfile.ZipFile:
    try:
        return zipfile.ZipFile(archive)
    except (zipfile.BadZipFile, OSError):
        raise ImportProblem("that isn't a .zip file") from None


def _top(names: list[str]) -> str:
    """The one folder everything in an archive sits in ("BongoMatrix-imports-main/"), dropped; "" if there isn't one, or
    the module is at the very top."""
    if MANIFEST in names:
        return ""
    tops = {n.split("/", 1)[0] for n in names}
    return next(iter(tops)) + "/" if len(tops) == 1 and all("/" in n for n in names) else ""


def _safe_part(part: str) -> bool:
    return bool(part) and part not in (".", "..") and not any(c in part for c in "\\:\0") and part.isprintable()


def scan(archive: Path, builtin_ids=()) -> list[dict]:
    """Every module in an archive: [{"folder": "" (the archive is one module) or "sports", "manifest" or
    "problem"}]. A folder counts if it holds both a bongo-module.json and an __init__.py: the top of the archive
    itself, or one folder down (BongoMatrix-imports/sports/), never deeper (so a module's own folders are never other
    modules). Nothing is unpacked here, and only plain files count."""
    with _open(archive) as zf:
        infos = zf.infolist()
        if len(infos) > MOST_LISTED:
            raise ImportProblem(f"it has more than {MOST_LISTED} files")
        plain = {e.filename: e for e in infos if not e.is_dir() and stat.S_IFMT(e.external_attr >> 16) in (0, stat.S_IFREG)}
        top = _top([e.filename for e in infos if not e.is_dir()])
        found = []
        for name in sorted(plain):
            if not name.startswith(top) or not (name == top + MANIFEST or name.endswith("/" + MANIFEST)):
                continue
            parts = name[len(top):].split("/")[:-1]
            if len(parts) > 1 or not all(_safe_part(p) for p in parts):
                continue
            folder = "/".join(parts)
            if f"{top}{folder + '/' if folder else ''}__init__.py" not in plain:
                continue
            entry = {"folder": folder}
            try:
                with zf.open(plain[name]) as f:
                    text = f.read((64 << 10) + 1)
                if len(text) > 64 << 10:
                    raise ImportProblem(f"its {MANIFEST} is too big")
                entry["manifest"] = check_manifest(json.loads(text), builtin_ids)
            except ImportProblem as error:
                entry["problem"] = str(error)
            except (ValueError, UnicodeDecodeError):
                entry["problem"] = f"its {MANIFEST} isn't valid JSON"
            except (OSError, EOFError, zipfile.BadZipFile, zlib.error):
                entry["problem"] = f"its {MANIFEST} couldn't be read: the .zip may be damaged"
            if folder == "":
                return [entry]                         # the archive is one module, as before (its own folders aren't others)
            found.append(entry)
        ids = {}
        for entry in found:                            # two folders claiming one id: neither can be told apart
            if "manifest" in entry:
                ids.setdefault(entry["manifest"]["id"], []).append(entry)
        for same in ids.values():
            if len(same) > 1:
                for entry in same:
                    entry.pop("manifest")
                    entry["problem"] = f"another folder here has the same id ({', '.join(e['folder'] for e in same if e is not entry)})"
        return found


def unpack(archive: Path, dest: Path, builtin_ids=(), folder: str | None = None) -> dict:
    """Unpack a module's .zip into ``dest`` (its top folder, if it has one, dropped) and check it. ``folder``: the
    module is that folder of the archive (one of ``scan``'s), and nothing else in it is unpacked."""
    zf = _open(archive)
    with zf:
        entries = [e for e in zf.infolist() if not e.is_dir()]
        if folder:
            top = _top([e.filename for e in entries])
            if not all(_safe_part(p) for p in folder.split("/")):
                raise ImportProblem("that folder can't be unpacked safely")
            within = f"{top}{folder}/"
            entries = [e for e in entries if e.filename.startswith(within)]
            if f"{within}{MANIFEST}" not in {e.filename for e in entries}:
                raise ImportProblem(f"there's no module in its folder ({folder}) any more")
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
        if folder:
            top = within
        elif MANIFEST not in names:
            tops = {n.split("/", 1)[0] for n in names}
            if len(tops) == 1 and f"{next(iter(tops))}/{MANIFEST}" in names:
                top = next(iter(tops)) + "/"
            else:
                raise ImportProblem(f"it isn't a Matrix module: there's no {MANIFEST} at the top")
        dest.mkdir(parents=True, exist_ok=True)
        inside = dest.resolve()
        for e in entries:
            # checked again once the top folder is dropped: "top//etc/x" only becomes "/etc/x" then
            name = e.filename[len(top):]
            parts = name.split("/")
            kind = stat.S_IFMT(e.external_attr >> 16)
            if (not name or name.startswith("/") or any(p in ("", ".", "..") for p in parts)
                    or kind not in (0, stat.S_IFREG)):        # only plain files (no links, devices or pipes)
                raise ImportProblem(f"it has a file it can't unpack safely: {e.filename[:80]}")
            target = dest / name
            if not target.resolve().is_relative_to(inside):
                raise ImportProblem(f"it has a file it can't unpack safely: {e.filename[:80]}")
            try:                                       # (a damaged zip, or a file and a folder of the same name)
                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(e) as src, open(target, "wb") as out:
                    shutil.copyfileobj(src, out)
            except (OSError, EOFError, zipfile.BadZipFile, zlib.error):
                raise ImportProblem(f"couldn't unpack {e.filename[:80]}: the .zip may be damaged") from None
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

    from bongo import abilities

    module_id = manifest["id"]
    asked = manifest.get("abilities") or {}
    spec = {"id": f"try_{module_id}"[:40], "module_id": module_id, "dir": str(dest), "data": str(dest.parent / "work"), "uid": uid,
            "net": "gate", "sites": manifest["sites"],
            # not trusted yet: its screens are tried with its settings' defaults (never what was typed into an
            # installed version), its background task isn't run, and it has no home-network address
            **({"settings": {f["name"]: f.get("default", "") for f in abilities.fields(asked)}} if abilities.has_card(asked) else {})}
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
        if bool(described.get("background")) != bool(asked.get("background")):   # the review must say what it does
            raise ImportProblem(f'its {MANIFEST} asks for "background" but its MODULE has no background task' if asked.get("background")
                                else f'its MODULE has a background task, but its {MANIFEST} doesn\'t ask for one ("abilities": {{"background": true}})')
        # a setting of its own card that it names in MODULE.secrets is a secret: the review says so, and from here
        # on its value is only ever kept by bongo/private.py. One that is only on the card (not also one of its
        # screens' settings) is left out of the secrets its screens' settings are checked against
        secret = [str(n) for n in described.get("secrets") or ()]
        if wrong := abilities.mark_secrets(asked, secret):
            raise ImportProblem(f'its MODULE.secrets names {wrong[0]}, which its {MANIFEST} doesn\'t give as a "text" setting with nothing in it')
        screens = set(described.get("defaults") or {}).union(*((s.get("defaults") or {}) for s in (described.get("styles") or {}).values()))
        described["secrets"] = [n for n in secret if n in screens or n not in {f["name"] for f in abilities.fields(asked)}]
        if described.get("help") is not None:   # its Help guide is shown in the dashboard: text, and https links only
            from bongo.schema import help_problems
            guide = described["help"]
            wrong = help_problems(guide) if isinstance(guide, dict) else ["help: must be an object"]
            if wrong:
                raise ImportProblem(f"its Help guide isn't right ({wrong[0]})")
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
        return described, preview_gif(frames) if frames else None   # (None: it has no screens, only a background task)
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
        self.new_abilities = []      # an update: what this version asks for that the installed one doesn't
        self.only_listed = True
        self.running = True          # its thread is still at work (even if cancelled)
        self.cancelled = False
        self.made = time.monotonic()
        self.choices = None          # a repository of several modules: what's in it, for the owner to pick from
        self.archive = None
        self.needs_token = False     # the project wants a sign-in: the dashboard offers the Access token box
        self._secret = None          # that token (bongo/linkfetch.py): never in public(), never logged
        self.waiting = 0.0           # when it reached the picker

    def public(self) -> dict:
        out = {"token": self.token, "step": self.step, "state": self.state, "message": self.message, "source": self.source}
        if self.state == "failed" and self.needs_token:
            out["needs_token"] = True
        if self.origin and self.state in ("choose", "ready"):
            out["origin"] = self.origin
        if self.state == "choose":
            out["choices"] = self.choices
        if self.state == "ready":
            from bongo.sandbox.jail import can_shut_in
            from bongo import abilities
            out.update(manifest=self.manifest, preview=self.preview, updating=self.updating, new_sites=self.new_sites, folder=self.origin.get("folder"),
                       only_listed=self.only_listed, enforced=can_shut_in(),
                       styles=list(self.described["styles"]), origin=self.origin,
                       # what it asks for beyond drawing its screens, in plain words (the review lists them
                       # before "I trust" can be ticked), and what is new in an update
                       abilities=abilities.words(self.manifest.get("abilities")), new_abilities=self.new_abilities)
        return out


JOBS: dict[str, Job] = {}


def staging() -> Path:
    path = DATA / "module-staging"
    path.mkdir(parents=True, exist_ok=True)
    return path


def fetch(url: str, secret: str | None = None) -> tuple[bytes, dict]:
    """A GitHub link as before; any other link the careful way (bongo/linkfetch.py), its warnings in the origin."""
    if GITHUB.match(url.strip()):
        return fetch_github(url)
    from bongo import linkfetch
    link = linkfetch.parse(url)
    said = linkfetch.describe(url)
    warnings = said["warnings"] + ([said["token_warning"]] if secret and said.get("token_warning") else [])
    data, origin = linkfetch.fetch_gitlab(link, secret) if link["kind"] == "gitlab" else linkfetch.fetch_zip(link)
    return data, {**origin, "warnings": warnings}


def _choice(entry: dict, state: dict) -> dict:
    """One module of a repository, as the picker shows it."""
    from bongo import abilities
    out = {"folder": entry["folder"]}
    if "problem" in entry:
        return {**out, "problem": entry["problem"]}
    m = entry["manifest"]
    have = state["modules"].get(m["id"])
    return {**out, "id": m["id"], "name": m["name"], "version": m["version"], "author": m["author"],
            "description": m.get("description", ""), "sites": m["sites"], "abilities": abilities.words(m.get("abilities")),
            "installed": have.get("version") if have else None,
            "newer": bool(have) and _newer(m["version"], str(have.get("version", "")))}


def _newer(new: str, old: str) -> bool:
    from bongo.linkfetch import _newer as newer
    return newer(new, old)


def start(source: str, *, url: str | None = None, upload: Path | None = None, upload_name: str = "",
          builtin_ids=(), update_of: str | None = None, secret: str | None = None, folder: str | None = None) -> Job:
    """Fetch, check and try a module in the background; ``JOBS[token]`` says how it's going. A repository of
    several modules stops at "choose" (``choose`` carries on). ``folder``: an update's own folder."""
    for token, job in list(JOBS.items()):               # an hour later, an import nobody finished is dropped
        if time.monotonic() - job.made > 3600:
            drop(token)
    token = base64.urlsafe_b64encode(os.urandom(9)).decode()
    job = JOBS[token] = Job(token, source)
    job._secret = secret
    work = staging() / token
    work.mkdir()

    def run():
        try:
            archive = work / "download.zip"
            if url:
                from bongo import linkfetch
                try:
                    data, job.origin = fetch(url, secret)
                except linkfetch.NeedsSignIn:
                    job.needs_token = True
                    raise
                archive.write_bytes(data)
            else:
                shutil.move(str(upload), archive)
                job.origin = {"kind": "zip", "name": upload_name[:80]}
            job.step = "check"
            found = scan(archive, builtin_ids)
            folders = [e["folder"] for e in found]
            if update_of:
                pick = folder or ""
                if pick not in folders:
                    raise ImportProblem(f"its folder ({pick}) isn't in the repository any more" if pick
                                        else "it isn't a Matrix module any more: there's no bongo-module.json at the top")
            elif "" in folders:
                pick = ""
            elif _hint(url) in folders:
                pick = _hint(url)
            elif len(found) == 1:
                pick = folders[0]
            elif not found:
                raise ImportProblem(f"it isn't a Matrix module: there's no {MANIFEST} (with an __init__.py) at the top, "
                                    "or in a folder one down")
            else:                                        # several: the owner picks (``choose``)
                state = load_state()
                job.choices = [_choice(e, state) for e in found]
                job.archive = archive
                job.waiting = time.monotonic()
                job.state = "choose"
                return
            problem = next((e.get("problem") for e in found if e["folder"] == pick), None)
            if problem:
                raise ImportProblem(problem)
            _prepare(job, archive, work, pick, builtin_ids, update_of)
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


def _hint(url: str | None) -> str | None:
    """The folder a link names (…/-/tree/main/sports), if it names one."""
    if not url or GITHUB.match(url.strip()):
        return None
    from bongo import linkfetch
    try:
        return linkfetch.parse(url).get("folder")
    except ImportProblem:
        return None


def _prepare(job: Job, archive: Path, work: Path, folder: str, builtin_ids, update_of: str | None) -> None:
    """Unpack one module (``folder`` of the archive), check it against what's installed and try it out."""
    job.step = "check"
    if folder:
        job.origin = {**job.origin, "folder": folder}
    dest = work / "module"
    job.manifest = unpack(archive, dest, builtin_ids, folder or None)
    job.dir = dest
    state = load_state()
    have = state["modules"].get(job.manifest["id"])
    if update_of and job.manifest["id"] != update_of:
        raise ImportProblem(f"that's a different module ({job.manifest['id']}), not an update of {update_of}")
    old = (have or {}).get("source") or {}
    if update_of and have and old.get("kind") == "github" and old.get("ref_kind") == "branch":
        job.origin["ref_kind"] = "branch"        # still following its branch, for the next update
    if update_of and old.get("branch") and not job.origin.get("branch"):
        job.origin["branch"] = old["branch"]
    job.updating = have is not None
    job.only_listed = have.get("only_listed", True) if have else True
    job.new_sites = sorted(set(job.manifest["sites"]) - set(have.get("sites", []))) if have else []
    from bongo import abilities
    job.new_abilities = abilities.added(job.manifest.get("abilities"), have.get("abilities")) if have else []
    if job.cancelled:
        return
    job.step = "try"
    job.described, job.preview = try_out(dest, job.manifest, free_uid(state, trying=True))
    job.step, job.state = "ready", "ready"


def choose(token: str, folders, builtin_ids=()) -> list[Job]:
    """The owner's pick from a repository of several modules: one job for each, checked and tried in turn (in the
    order they're listed) while the first is being looked at. The repository's own job is done with."""
    with _lock:
        parent = JOBS.get(token)
        if parent is None or parent.state != "choose" or parent.archive is None:
            raise ImportProblem("that import has expired; start it again")
        if busy(but=token):
            raise Busy("Another module is being checked; wait for it to finish.")
        good = [c["folder"] for c in parent.choices if "problem" not in c]
        if not isinstance(folders, list) or not folders or not all(isinstance(f, str) for f in folders):
            raise ImportProblem("tick at least one module")
        if any(f not in good for f in folders):        # only ever a folder the scan found (never a path from a browser)
            raise ImportProblem("that module can't be imported from here")
        picked = [f for f in good if f in folders]
        jobs = []
        for folder in picked:
            child_token = base64.urlsafe_b64encode(os.urandom(9)).decode()
            child = JOBS[child_token] = Job(child_token, parent.source)
            child.origin, child._secret, child.step = dict(parent.origin), parent._secret, "check"
            (staging() / child_token).mkdir()
            shutil.copyfile(parent.archive, staging() / child_token / "download.zip")
            jobs.append((child, folder))
        parent.state = "split"
        drop(token)

    def run():
        for child, folder in jobs:
            work = staging() / child.token
            try:
                if child.cancelled:
                    continue
                _prepare(child, work / "download.zip", work, folder, builtin_ids, None)
            except ImportProblem as error:
                child.state, child.message = "failed", str(error)
            except Exception:  # noqa: BLE001 - one module failing never stops the others
                log.exception("module import failed")
                child.state, child.message = "failed", "something went wrong (see the Logs page)"
            finally:
                child.running = False
                if child.cancelled:
                    shutil.rmtree(work, ignore_errors=True)

    threading.Thread(target=run, name="module-import", daemon=True).start()
    return [child for child, _ in jobs]


PICKER_WAIT = 900                # s a repository's job waiting at the picker counts as an import at work


def busy(but: str | None = None) -> bool:
    """An import is still at work (one at a time: each tries a module out in a child process): one being fetched,
    checked or tried, the modules picked from a repository while any is still to try, and a repository waiting at
    the picker (for a quarter of an hour at most: a page closed on it doesn't hold imports up for long). ``but``:
    that repository's own job (picking from it is the import at work)."""
    now = time.monotonic()
    return (any(job.running or job.state == "choose" and job.token != but and now - job.waiting < PICKER_WAIT
                for job in JOBS.values())
            or any(job.running for job in _cancelled))


_cancelled: list[Job] = []


TOKEN = re.compile(r"[A-Za-z0-9_-]{12}")       # what ``start`` makes: 9 random bytes, base64 for links


def drop(token: str) -> None:
    if not isinstance(token, str) or not TOKEN.fullmatch(token):
        return                                       # never a path: "..", say, would empty the whole data folder
    with _lock:                                      # (never in the middle of an install: it moves this very folder)
        job = JOBS.get(token)
        if job is not None and job.state in ("installing", "installed"):
            return                                   # install tidies its own folder away
        JOBS.pop(token, None)
        if job is not None and job.running:              # its thread tidies up when it finishes
            job.cancelled = True
            _cancelled[:] = [j for j in _cancelled if j.running] + [job]
            return
        shutil.rmtree(staging() / token, ignore_errors=True)


def install(token: str, modules: dict) -> dict:
    """Keep a module that's been tried out: it goes in data/modules/<id> and into ``modules``."""
    from bongo.sandbox import remote

    with _lock:
        job = JOBS.get(token)
        if job is not None and job.state in ("installing", "installed"):
            raise ImportProblem("that module has already been installed")   # (Install pressed twice)
        if job is None or job.state != "ready" or job.dir is None or not job.dir.is_dir():
            raise ImportProblem("that import has expired; start it again")
        module_id = job.manifest["id"]
        state = load_state()
        have = state["modules"].get(module_id, {})
        if module_id in modules and not is_imported(modules[module_id]):
            raise ImportProblem(f"the Matrix already has a module called {module_id}")
        job.state = "installing"                       # claimed, under the lock: a second press can't move it in again
        target = folder() / module_id
        old = target.with_name(f".{module_id}-old")
        aside = False                                  # this install moved the version that was there aside
        try:
            from bongo import abilities
            from bongo.sandbox import background
            background.drop(module_id)                 # its background task rests until this is done (``sync``)
            remote.stop(module_id)
            folder().mkdir(parents=True, exist_ok=True)
            shutil.rmtree(old, ignore_errors=True)
            if target.exists():
                target.rename(old)
                aside = True
            shutil.move(str(job.dir), target)
            shutil.rmtree(old, ignore_errors=True)
            info = {**job.manifest, "source": job.origin, "described": job.described,
                    "installed": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
                    "uid": have.get("uid") or free_uid(state), "only_listed": have.get("only_listed", True)}
            info.pop("id")
            info.pop("update", None)
            info.pop("sign_in", None)                  # (a token that works again)
            state["modules"][module_id] = info
            # its own settings: what was typed before is kept where this version still has the field. Before the
            # state is saved, so nothing of it runs with settings that aren't there yet
            if not have:                               # new here: nothing typed for another module of the same id is its
                from bongo import private
                private.forget(module_id)
                abilities.STORE.forget(module_id)
            abilities.STORE.adopt(module_id, info.get("abilities") or {})
            save_state(state)
            if job._secret and (job.origin or {}).get("kind") == "gitlab":
                from bongo import linkfetch           # its project's access token, for its updates (bongo/private.py)
                linkfetch.keep_token(job.origin, job._secret)
            apply_home(module_id, info)                # (a version that no longer asks for one loses it)
            module = remote.build(info["described"], spec_for(module_id, info))
            object.__setattr__(module, "_imported", True)
            # ``modules`` is shared with the player and other requests: they read it through list(...) (a copy
            # taken in one go), and changes to it are made one at a time here
            modules[module_id] = module
        except BaseException:
            if aside and old.exists():                 # the move didn't finish: the version that was there goes back
                if target.exists() and job.dir.is_dir():
                    shutil.rmtree(target, ignore_errors=True)
                if not target.exists():
                    old.rename(target)
            job.state = "ready" if job.dir.is_dir() else "failed"
            raise
        job.state = "installed"                        # kept (not dropped) so a late second press is told so
    shutil.rmtree(staging() / token, ignore_errors=True)
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
    with _lock:
        modules[module_id] = module
    log.warning("module %s: %s", module_id, "only the sites it lists" if only_listed else "ANY site, and the home network")
    return info


def remove(module_id: str, modules: dict) -> None:
    from bongo.sandbox import remote
    from bongo.sandbox.gate import gate

    from bongo import abilities
    from bongo.sandbox import background

    with _lock:                                      # all of it: never half-removed while it's installed again
        state = load_state()
        gone = state["modules"].pop(module_id, None)
        if gone is None:
            raise KeyError(module_id)
        save_state(state)
        try:                                         # its project's access token, unless another module still uses it
            from bongo import linkfetch
            linkfetch.forget_token(gone.get("source") or {}, state["modules"])
        except OSError:
            pass
        background.drop(module_id)
        remote.stop(module_id)
        apply_home(module_id, None)
        abilities.STORE.forget(module_id)            # its own settings go with it (and, below, its secrets)
        try:
            gate().forget(module_id)
        except OSError:
            pass
        if is_imported(modules.get(module_id)):
            modules.pop(module_id, None)
        shutil.rmtree(folder() / module_id, ignore_errors=True)
        shutil.rmtree(work_dir(module_id), ignore_errors=True)
        try:
            from bongo import private
            private.forget(module_id)                # any secret settings it had (an API key) go with it
        except OSError:
            pass
    log.info("module %s removed", module_id)


# -- updates ----------------------------------------------------------------------------------------------
CHECK_EVERY = 24 * 3600


def update_link(info: dict) -> str | None:
    """Where an update of a module installed from GitHub or GitLab comes from (None: it can't be updated from there)."""
    src = info.get("source") or {}
    if src.get("kind") == "gitlab":
        ref = (info.get("update") or {}).get("ref") or (src.get("ref") if src.get("ref_kind") in ("branch", "tag") else None)
        return f"{src['url']}/-/tree/{ref}" if ref else None
    if src.get("kind") != "github":
        return None
    if src.get("ref_kind") == "release":
        return src["url"]
    if src.get("ref_kind") == "branch":
        return f"{src['url']}/tree/{src['ref']}"
    return None


def check_updates() -> list[str]:
    """Look once for a newer version of each module installed from GitHub or GitLab (bongo/linkfetch.py): a newer release, or a new version
    number on its branch. Nothing is installed; the dashboard offers Update. Returns the ids with one."""
    found, signed = {}, {}
    for module_id, info in installed().items():
        src = info.get("source") or {}
        if src.get("kind") == "gitlab":
            from bongo import linkfetch
            try:
                found[module_id] = linkfetch.gitlab_update(info)
                signed[module_id] = False
            except linkfetch.NeedsSignIn:             # its token is gone or no longer accepted: said on its row, and no
                found[module_id], signed[module_id] = None, True   # stale "update available" is left behind
                log.info("couldn't check %s for an update: its GitLab project wants a sign-in", module_id)
            except (ImportProblem, FileNotFoundError, ValueError, KeyError, TypeError) as error:
                log.info("couldn't check %s for an update: %s", module_id, error)
            continue
        if src.get("kind") != "github" or "/" not in src.get("repo", ""):
            continue
        inside = f"{src['folder']}/" if src.get("folder") else ""
        try:
            if src.get("ref_kind") == "release":
                tag = json.loads(_get(f"https://api.github.com/repos/{src['repo']}/releases/latest", 1 << 20,
                                      "application/vnd.github+json"))["tag_name"]
                found[module_id] = {"version": str(tag)[:20], "ref": tag} if tag != src.get("ref") else None
                if found[module_id] and inside:        # one of several modules: only if its own version moved
                    manifest = json.loads(_get(f"https://raw.githubusercontent.com/{src['repo']}/{tag}/{inside}{MANIFEST}", 64 << 10))
                    version = str(manifest.get("version", ""))[:20]
                    found[module_id] = {"version": version, "ref": tag} if version and version != info.get("version") else None
            elif src.get("ref_kind") == "branch":
                manifest = json.loads(_get(f"https://raw.githubusercontent.com/{src['repo']}/{src['ref']}/{inside}{MANIFEST}", 64 << 10))
                version = str(manifest.get("version", ""))[:20]
                found[module_id] = {"version": version, "ref": src["ref"]} if version and version != info.get("version") else None
        except (ImportProblem, FileNotFoundError, ValueError, KeyError, TypeError) as error:
            log.info("couldn't check %s for an update: %s", module_id, error)
    with _lock:
        state = load_state()
        for module_id, update in found.items():
            if module_id in state["modules"]:
                state["modules"][module_id]["update"] = update
        for module_id, needs in signed.items():
            if module_id in state["modules"]:
                if needs:
                    state["modules"][module_id]["sign_in"] = True
                else:
                    state["modules"][module_id].pop("sign_in", None)
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
