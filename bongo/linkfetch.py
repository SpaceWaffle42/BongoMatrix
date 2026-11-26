"""Importing a module from a link that isn't GitHub (Settings > Modules > Import): a GitLab project, on gitlab.com
or a GitLab of your own (https://git.example.org/group/project), or a direct link to a .zip.

GitHub links are bongo/addons.py's own (``fetch_github``) and are unchanged. Everything here is fetched by the
Matrix itself, so the fetch is careful:

- the link's name is looked up once per request and only the addresses that pass are dialled (``resolve``): never
  this Matrix itself (loopback, 0.0.0.0, any address of its own) and never multicast or broadcast. The home network
  is allowed (that is the point), and the dashboard says so (``describe``);
- a redirect is followed only on the same host, and never from https to http; one to another host is refused, with
  where it went, so the owner can decide to import from there;
- downloads have the same size cap as GitHub's, every request a timeout and the whole download a deadline;
- a project's access token (a read-only GitLab token, for a private project) is sent only as the PRIVATE-TOKEN
  header, only to the host it was given for, never in a link. It is kept by bongo/private.py (``SOURCES``) once a
  module from that project is installed, never in the config, a backup, the log or anything sent to a browser,
  and forgotten when the last module from that project is removed (``forget_token``).
"""
from __future__ import annotations

import http.client
import ipaddress
import json
import logging
import re
import socket
import ssl
import time
import urllib.parse

from bongo.addons import MANIFEST, MOST_DOWNLOAD, USER_AGENT, ImportProblem

log = logging.getLogger("bongo.addons")

SOURCES = "@sources"           # bongo/private.py's key for the tokens of import sources (never a module id: "@")
TIMEOUT = 20                   # s for each connection and each wait for an answer
DEADLINE = 120                 # s for a whole download
MOST_HOPS = 5                  # redirects followed (same host only)
LOOK_UP_WAIT = 5               # s to look a name up
HOME_NAMES = (".lan", ".local", ".home", ".internal", ".home.arpa", ".localdomain", ".localhost")
SEGMENT = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.+-]{0,99}")
REF = re.compile(r"[A-Za-z0-9_.][A-Za-z0-9_./+-]{0,199}")
HOST = re.compile(r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)*")

NOT_GITHUB = "This isn't GitHub. Only import from a place you run or trust: the module's code runs on this Matrix (in its sandbox)."
HOME = "This address is on your home network."
PLAIN = "This address isn't encrypted (http): someone on the network could change what's downloaded."
PLAIN_TOKEN = ("This address isn't encrypted: the access token is sent as plain text, and anyone on your network "
               "could read it.")


class NeedsSignIn(ImportProblem):
    """The project needs a sign-in: the dashboard offers the Access token box."""


# -- the link ----------------------------------------------------------------------------------------------------
def parse(url: str) -> dict:
    """What a link names: {"kind": "gitlab" | "zip", "base": "http://host:port", "host", "port", "scheme",
    "project", "ref", "folder"} (gitlab), or {"kind": "zip", "url"}. Raises ImportProblem in words."""
    url = (url or "").strip()
    try:
        parts = urllib.parse.urlsplit(url)
        port = parts.port
    except ValueError:
        raise ImportProblem("that link isn't right: check the address and the port") from None
    if port == 0:
        raise ImportProblem("that link's port (0) isn't one anything can be fetched from")
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ImportProblem("that isn't a web link. It should start with https:// or http://")
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        raise ImportProblem("leave any sign-in out of the link: for a private project, use the Access token box")
    host = parts.hostname.lower().rstrip(".")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        if not HOST.fullmatch(host):
            raise ImportProblem("that link's address isn't right") from None
    port = port or (443 if parts.scheme == "https" else 80)
    shown_port = "" if port == (443 if parts.scheme == "https" else 80) else f":{port}"
    shown_host = f"[{host}]" if ":" in host else host
    base = f"{parts.scheme}://{shown_host}{shown_port}"
    segments = [urllib.parse.unquote(s) for s in parts.path.split("/") if s]
    common = {"scheme": parts.scheme, "host": host, "port": port, "base": base}
    if segments and segments[-1].lower().endswith(".zip") and "-" not in segments:
        if parts.query or parts.fragment:
            raise ImportProblem("use a link to the .zip itself, without anything after a ? or #")
        if any(s in (".", "..") for s in segments):
            raise ImportProblem("that link isn't right")
        return {"kind": "zip", **common, "url": f"{base}/{'/'.join(urllib.parse.quote(s) for s in segments)}",
                "name": segments[-1][:80]}
    project, rest = (segments[:segments.index("-")], segments[segments.index("-") + 1:]) if "-" in segments else (segments, [])
    if project and project[-1].endswith(".git"):
        project[-1] = project[-1][:-4]
    if len(project) < 2 or not all(SEGMENT.fullmatch(s) and not s.endswith(".") for s in project):
        raise ImportProblem("that isn't a link to a project. It should look like gitlab.com/group/project, "
                            "or a link straight to a .zip file")
    ref = folder = None
    if rest:
        kind = rest[0]
        if kind not in ("tree", "tags", "commit", "commits", "archive", "releases", "blob") or len(rest) < 2:
            raise ImportProblem("use the link to the project, or to one branch or tag (…/-/tree/main)")
        ref = rest[1]
        if kind == "archive":                                 # …/-/archive/<ref>/<name>-<ref>.zip
            folder = None
        elif kind == "tree" and len(rest) > 2:                # …/-/tree/main/sports: that one folder
            folder = "/".join(rest[2:])
        if not REF.fullmatch(ref) or ".." in ref:
            raise ImportProblem("that branch or tag name isn't one this can fetch")
        if folder is not None and (any(s in (".", "..") for s in folder.split("/")) or len(folder) > 200):
            raise ImportProblem("that link isn't right")
    return {"kind": "gitlab", **common, "project": "/".join(project), "ref": ref, "folder": folder,
            "url": f"{base}/{'/'.join(project)}"}


def home_name(host: str) -> bool:
    return "." not in host or host.endswith(HOME_NAMES)


def home_ip(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address.split("%")[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return not ip.is_global or ip in ipaddress.ip_network("100.64.0.0/10")


# -- where it is: never this Matrix ------------------------------------------------------------------------------
def own_address(ip) -> bool:
    from bongo.sandbox.gate import own_address as own
    return own(ip)


def look_up(host: str, port: int) -> list[str] | None:
    from bongo.sandbox.gate import look_up as find
    return find(host, port, LOOK_UP_WAIT)


def self_reason(address: str) -> str | None:
    """Why the Matrix won't fetch from ``address`` (None: it may)."""
    try:
        ip = ipaddress.ip_address(address.split("%")[0])
    except ValueError:
        return "isn't an address"
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    if ip.is_loopback or ip.is_unspecified:
        return "is this Matrix itself"
    from bongo.sandbox.gate import TUNNELS
    if isinstance(ip, ipaddress.IPv6Address) and any(ip in net for net in TUNNELS):
        return "is an IPv4 address inside an IPv6 one, which isn't allowed"
    if ip.is_link_local:
        return "is a link-local address (169.254.x.x or fe80:), which isn't allowed"
    if ip.is_multicast or str(ip) == "255.255.255.255" or ip.is_reserved:
        return "isn't an address this can fetch from"
    if own_address(ip):
        return "is this Matrix itself"
    return None


def resolve(host: str, port: int) -> list[str]:
    """The addresses to dial for ``host``, looked up once; refused if any of them is this Matrix."""
    if host in ("localhost", "localhost.localdomain") or host.endswith(".localhost"):
        raise ImportProblem(f"{host} is this Matrix itself: it can't import from its own address")
    found = look_up(host, port)
    if not found:
        raise ImportProblem(f"couldn't find {host} on the network")
    for address in found:
        if why := self_reason(address):
            raise ImportProblem(f"{host} {why}: it can't import from there")
    return found


def describe(url: str) -> dict:
    """What the dashboard says before a non-GitHub link may be fetched: its kind and every warning that's true."""
    link = parse(url)
    found = resolve(link["host"], link["port"])
    home = home_name(link["host"]) or any(home_ip(a) for a in found)
    warnings = [{"key": "not_github", "text": NOT_GITHUB}]
    if home:
        warnings.append({"key": "home", "text": HOME})
    if link["scheme"] == "http":
        warnings.append({"key": "plain", "text": PLAIN})
    kind = (f"A GitLab project: {link['project']} on {link['host']}" if link["kind"] == "gitlab"
            else f"A .zip file on {link['host']}")
    return {"kind": link["kind"], "label": kind, "host": link["host"], "url": link["url"], "warnings": warnings,
            "home": home, "plain": link["scheme"] == "http", "token": link["kind"] == "gitlab",
            # said with the others, under the same tick, once a token is typed for an http link
            **({"token_warning": {"key": "plain_token", "text": PLAIN_TOKEN}} if link["kind"] == "gitlab" and link["scheme"] == "http" else {})}


# -- fetching, carefully ----------------------------------------------------------------------------------------
class _Pinned(http.client.HTTPConnection):
    def __init__(self, host, port, addresses, timeout):
        super().__init__(host, port, timeout=timeout)
        self.addresses = addresses

    def _dial(self):
        error = None
        for address in self.addresses:
            try:
                return socket.create_connection((address, self.port), self.timeout)
            except OSError as e:
                error = e
        raise error or OSError("no address")

    def connect(self):
        self.sock = self._dial()


class _PinnedTLS(_Pinned):
    default_port = 443

    def connect(self):
        sock = self._dial()
        self.sock = ssl.create_default_context().wrap_socket(sock, server_hostname=self.host)


def origin_of(url: str) -> tuple[str, str, int]:
    """(scheme, host, port) of a link: a redirect is followed, and a token sent, only within one of these."""
    parts = urllib.parse.urlsplit(url)
    scheme = (parts.scheme or "").lower()
    try:
        port = parts.port
    except ValueError:
        port = -1
    return scheme, (parts.hostname or "").lower().rstrip("."), port or (443 if scheme == "https" else 80)


def request(url: str, *, most: int = MOST_DOWNLOAD, token: str | None = None, token_base: str | None = None,
            accept: str | None = None) -> bytes:
    """GET ``url``: the body, at most ``most`` bytes. A redirect is followed only to the same scheme, host and port
    as ``url``. The token goes only to ``token_base``'s scheme, host and port, as the PRIVATE-TOKEN header. 404 raises
    FileNotFoundError; a sign-in, NeedsSignIn."""
    started = time.monotonic()
    first = origin_of(url)
    allowed = origin_of(token_base) if token_base else None
    for _ in range(MOST_HOPS + 1):
        parts = urllib.parse.urlsplit(url)
        scheme, host, port = origin_of(url)
        if scheme not in ("http", "https") or not host or port <= 0:
            raise ImportProblem("that link isn't one this can fetch")
        addresses = resolve(host, port)
        conn = (_PinnedTLS if scheme == "https" else _Pinned)(host, port, addresses, TIMEOUT)
        headers = {"User-Agent": USER_AGENT, **({"Accept": accept} if accept else {})}
        if token and allowed and (scheme, host, port) == allowed:
            headers["PRIVATE-TOKEN"] = token
        path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
        try:
            conn.request("GET", path, headers=headers)
            answer = conn.getresponse()
            status = answer.status
            if status in (301, 302, 303, 307, 308):
                where = urllib.parse.urljoin(url, answer.getheader("Location") or "")
                after = urllib.parse.urlsplit(where)
                if "/users/sign_in" in after.path:
                    raise NeedsSignIn(f"{host} wants a sign-in for that")
                there = origin_of(where)
                if there != first or after.username is not None or "@" in after.netloc:
                    if there[0] == "http" and first[0] == "https" and there[1] == host:
                        raise ImportProblem(f"{host} sent the download on to an unencrypted (http) address; it wasn't followed")
                    shown = f"{there[1] or after.netloc}{'' if there[2] in (80, 443) else f':{there[2]}'}"
                    raise ImportProblem(f"{host} sent the download on to another address ({shown}); it wasn't "
                                        "followed. If you trust that one, import from it directly")
                url = where
                continue
            if status in (401, 403):
                raise NeedsSignIn(f"{host} wants a sign-in for that")
            if status == 404:
                raise FileNotFoundError(url)
            if status == 429:
                raise ImportProblem(f"{host} is limiting requests for now; try again later")
            if status != 200:
                raise ImportProblem(f"{host} answered {status}")
            size = answer.getheader("Content-Length")
            if size and size.isdigit() and int(size) > most:
                raise ImportProblem(f"it's bigger than {most >> 20} MB")
            data = bytearray()
            while len(data) <= most:
                if time.monotonic() - started > DEADLINE:
                    raise ImportProblem(f"{host} took too long to send it")
                chunk = answer.read(min(65536, most + 1 - len(data)))
                if not chunk:
                    break
                data += chunk
            if len(data) > most:
                raise ImportProblem(f"it's bigger than {most >> 20} MB")
            return bytes(data)
        except (ImportProblem, FileNotFoundError):
            raise
        except ssl.SSLError as error:
            raise ImportProblem(f"couldn't make a secure connection to {host} ({getattr(error, 'reason', None) or 'its certificate'})") from None
        except (OSError, http.client.HTTPException) as error:
            raise ImportProblem(f"couldn't reach {host} ({type(error).__name__ if not str(error) else error})") from None
        finally:
            conn.close()
    raise ImportProblem(f"{first[1]} sent the download round too many times")


def _json(url: str, token: str | None, base: str):
    try:
        return json.loads(request(url, most=1 << 20, token=token, token_base=base, accept="application/json"))
    except ValueError:
        raise ImportProblem(f"{origin_of(base)[1]} didn't answer like a GitLab") from None


def api(link: dict) -> str:
    return f"{link['base']}/api/v4/projects/{urllib.parse.quote(link['project'], safe='')}"


def fetch_zip(link: dict) -> tuple[bytes, dict]:
    try:
        data = request(link["url"])
    except FileNotFoundError:
        raise ImportProblem("there's nothing at that link") from None
    except NeedsSignIn:
        raise ImportProblem(f"{link['host']} wants a sign-in for that .zip: this can only fetch .zip links anyone can open") from None
    return data, {"kind": "link", "url": link["url"], "name": link["name"], "host": link["host"]}


REFUSED = ("GitLab didn't accept that access token for this project. Add a new one (read-only: read_api, or "
           "read_repository)")


def fetch_gitlab(link: dict, token: str | None = None) -> tuple[bytes, dict]:
    """A GitLab project's archive: the ref in the link, or its default branch (from the API where it answers,
    otherwise main, then master). With a token, the archive comes from the API (which takes the header). A token
    that isn't accepted raises NeedsSignIn, so the dashboard offers the token box again."""
    host, base, home = link["host"], api(link), link["base"]
    info = None
    try:
        info = _json(base, token, home)
    except NeedsSignIn:
        if not token:
            raise NeedsSignIn("this GitLab project needs a sign-in. Add an access token (read-only) for it") from None
        # (a token with read_repository only can't look the project up: main, then master, are tried instead)
    except FileNotFoundError:
        if token:
            raise NeedsSignIn("there's no project there that this access token can see. Check the link, or add a new token") from None
    except ImportProblem:
        if token:
            raise
    default = info.get("default_branch") if isinstance(info, dict) and isinstance(info.get("default_branch"), str) else None
    ref, kind = link["ref"], "branch"
    if ref and info is not None:                         # a tag (newest tag followed), a branch, or a commit (not followed)
        kind = "ref"
        for what, name in (("tags", "tag"), ("branches", "branch")):
            try:
                _json(f"{base}/repository/{what}/{urllib.parse.quote(ref, safe='')}", token, home)
                kind = name
                break
            except (FileNotFoundError, ImportProblem):
                pass
    tries = [ref] if ref else [default] if default else ["main", "master"]
    name = link["project"].rsplit("/", 1)[-1]
    for attempt in tries:
        if token:
            url = f"{base}/repository/archive.zip?sha={urllib.parse.quote(attempt, safe='')}"
        else:
            url = f"{link['url']}/-/archive/{urllib.parse.quote(attempt, safe='/')}/{name}-{urllib.parse.quote(attempt.replace('/', '-'), safe='')}.zip"
        try:
            data = request(url, token=token, token_base=home)
        except FileNotFoundError:
            continue
        except NeedsSignIn:
            if token:
                raise NeedsSignIn(REFUSED) from None
            raise NeedsSignIn("this GitLab project needs a sign-in. Add an access token (read-only) for it") from None
        origin = {"kind": "gitlab", "url": link["url"], "host": host, "port": link["port"], "project": link["project"],
                  "ref": attempt, "ref_kind": kind, **({"branch": default} if default else {}), "private": bool(token)}
        return data, origin
    if link["ref"]:
        raise ImportProblem(f"couldn't find {link['ref']!r} in {link['project']}")
    if info is None and not token:
        raise NeedsSignIn("there's no public project there. If it's private, add an access token (read-only) for it")
    raise ImportProblem(f"couldn't find a branch to fetch in {link['project']}")


# -- its token -----------------------------------------------------------------------------------------------------
def clean_token(value) -> str:
    """A pasted access token, or ValueError in words (it goes in a header: one line of Latin-1, as HTTP allows)."""
    from bongo import private
    value = private.clean(value)
    try:
        value.encode("latin-1")
    except UnicodeEncodeError:
        raise ValueError("That token has characters a GitLab token never has: copy it again from GitLab.") from None
    return value


def _base_of(origin: dict) -> str:
    return str(origin.get("url", "")).rsplit("/" + str(origin.get("project", "")), 1)[0]


def token_key(origin: dict) -> str:
    """host:port/project: a token is kept for one project on one GitLab (scheme and port included in what it is
    sent to: ``request``)."""
    _, host, port = origin_of(_base_of(origin))
    return f"{host or origin.get('host', '')}:{port}/{origin.get('project', '')}"


def _old_key(origin: dict) -> str:   # (kept before the port was part of it)
    return f"{origin.get('host', '')}/{origin.get('project', '')}"


def token_for(origin: dict) -> str | None:
    from bongo import private
    if (origin or {}).get("kind") != "gitlab":
        return None
    kept = private._all().get(SOURCES, {})
    return kept.get(token_key(origin)) or kept.get(_old_key(origin)) or None


def keep_token(origin: dict, token: str) -> None:
    from bongo import private
    private.keep(SOURCES, token_key(origin), token)
    if _old_key(origin) in private._all().get(SOURCES, {}):
        private.forget(SOURCES, _old_key(origin))


def forget_token(origin: dict, still: dict) -> None:
    """Forget ``origin``'s token unless a module in ``still`` (the installed ones) came from the same project."""
    from bongo import private
    if (origin or {}).get("kind") != "gitlab":
        return
    key = token_key(origin)
    if any((i.get("source") or {}).get("kind") == "gitlab" and token_key(i["source"]) == key for i in still.values()):
        return
    for name in (key, _old_key(origin)):
        if name in private._all().get(SOURCES, {}):
            private.forget(SOURCES, name)


# -- updates ---------------------------------------------------------------------------------------------------
VERSION = re.compile(r"(\d+(?:\.\d+)*)(?:-([0-9A-Za-z.-]+))?")


def _version(text) -> tuple | None:
    """(numbers, pre-release) from "v1.2.0-rc.1+build"; None if it isn't a version this can compare."""
    if not isinstance(text, str):
        return None
    text = text.strip().split("+", 1)[0]
    if text[:1] in ("v", "V"):
        text = text[1:]
    m = VERSION.fullmatch(text)
    if not m:
        return None
    return tuple(int(n) for n in m.group(1).split(".")), m.group(2)


def _newer(new, old) -> bool:
    """Whether version ``new`` is higher than ``old``: numbers compared part by part (a missing part is 0, so 1.0 is
    1.0.0), a pre-release (1.0.0-rc.1) older than its release. Anything that can't be read is never newer."""
    a, b = _version(new), _version(old)
    if a is None or b is None or str(new).strip() == str(old).strip():
        return False
    width = max(len(a[0]), len(b[0]))
    na, nb = a[0] + (0,) * (width - len(a[0])), b[0] + (0,) * (width - len(b[0]))
    if na != nb:
        return na > nb
    if a[1] == b[1]:
        return False
    if a[1] is None or b[1] is None:
        return a[1] is None                               # the release is newer than its pre-release

    def pre(tag):
        return [(0, int(x), "") if x.isdigit() else (1, 0, x) for x in tag.split(".")]
    return pre(a[1]) > pre(b[1])


def gitlab_update(info: dict) -> dict | None:
    """The newer version of a module installed from GitLab, if there is one: the project's newest tag if it has
    tags, otherwise the latest commit of its branch; in either, the version in the module's own folder's
    bongo-module.json (so a tag for one module of a repository doesn't show as an update for the others). Raises
    NeedsSignIn when the project wants a sign-in (its token is missing, or no longer accepted)."""
    src = info.get("source") or {}
    home = _base_of(src)
    link = {"base": home, "project": src["project"], "url": src["url"]}
    base, token = api(link), token_for(src)
    ref, kind = None, None
    try:
        tags = _json(f"{base}/repository/tags?order_by=updated&sort=desc&per_page=1", token, home)
        if isinstance(tags, list) and tags and isinstance(tags[0], dict) and isinstance(tags[0].get("name"), str):
            ref, kind = tags[0]["name"], "tag"
    except (FileNotFoundError, NeedsSignIn):     # (a token with read_repository only can't list tags: its branch, then)
        pass
    if ref is None:
        branch = src.get("branch") or (src.get("ref") if src.get("ref_kind") == "branch" else None)
        if not branch:
            return None
        ref, kind = branch, "branch"
    path = "/".join(p for p in (src.get("folder") or "", MANIFEST) if p)
    try:
        manifest = _json(f"{base}/repository/files/{urllib.parse.quote(path, safe='')}/raw?ref={urllib.parse.quote(ref, safe='')}",
                         token, home)
    except FileNotFoundError:
        return None                                       # (not in that tag or branch: nothing to offer)
    version = manifest.get("version") if isinstance(manifest, dict) else None
    if isinstance(version, str) and _newer(version, str(info.get("version", ""))):
        return {"version": version.strip()[:20], "ref": ref, "ref_kind": kind}
    return None
