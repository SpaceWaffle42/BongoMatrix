"""Signing in to Spotify, and what's playing now.

Spotify's rules (2026): an app in Development Mode belongs to one developer, who needs Premium,
and lists the few accounts that may use it; its redirect address must be https or a loopback IP
literal. A Matrix has neither a public https address nor a browser of its own, so it signs in like
a TV app would, by hand:

1. you make an app at developer.spotify.com with the redirect URI ``http://127.0.0.1:8888/callback``
   and paste its Client ID into Settings > Spotify;
2. the dashboard opens Spotify's sign-in page; you approve;
3. your browser then goes to http://127.0.0.1:8888/callback?code=..., which doesn't load (nothing
   listens there): copy that address back into the dashboard, and the Matrix swaps the code for
   tokens (PKCE: no client secret is ever needed or stored).

The refresh token lives in ``data/spotify.json`` (never in the config or the dashboard). What's
playing is fetched in the background every few seconds while a Spotify screen is showing, so
rendering never waits on the network. BONGO_OFFLINE=1 stops all fetching.
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import logging
import os
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from PIL import Image

from bongo import paths, private

log = logging.getLogger(__name__)

REDIRECT = "http://127.0.0.1:8888/callback"
SCOPES = "user-read-currently-playing user-read-playback-state"
AUTH = "https://accounts.spotify.com/authorize"
TOKEN = "https://accounts.spotify.com/api/token"
PLAYING = "https://api.spotify.com/v1/me/player/currently-playing"
FILE = Path(os.environ.get("BONGO_SPOTIFY", paths.DATA / "spotify.json"))
POLL = 5.0                      # seconds between "what's playing?" checks while a screen asks
_lock = threading.Lock()
_pending: dict[str, str] = {}   # sign-in in progress: state -> PKCE verifier (and client id)


@dataclass(frozen=True)
class Track:
    title: str
    artist: str
    art_url: str
    progress_ms: int
    duration_ms: int
    playing: bool
    fetched: float              # time.time() of the reading

    def position(self, now: float) -> float:
        """Seconds into the song at ``now`` (it moves on while playing between checks)."""
        p = self.progress_ms / 1000 + (max(0.0, now - self.fetched) if self.playing else 0.0)
        return min(p, self.duration_ms / 1000)


# -- the saved account ---------------------------------------------------------------------------

def load() -> dict:
    try:
        return json.loads(FILE.read_text())
    except (OSError, ValueError):
        return {}


def save(data: dict) -> None:
    private.write(FILE, json.dumps(data))   # (yours alone from the start; two saves at once can't collide)


def connected() -> bool:
    return bool(load().get("refresh_token"))


def forget() -> None:
    FILE.unlink(missing_ok=True)
    with _lock:
        _now.clear()


# -- signing in ----------------------------------------------------------------------------------

def start(client_id: str) -> str:
    """The Spotify sign-in address for this client id (PKCE)."""
    client_id = client_id.strip()
    if not (len(client_id) == 32 and all(c in "0123456789abcdef" for c in client_id.lower())):
        raise ValueError("that doesn't look like a Spotify Client ID (32 letters and digits, from your app's page)")
    verifier = secrets.token_urlsafe(64)[:96]
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(16)
    with _lock:
        _pending.clear()
        _pending.update(state=state, verifier=verifier, client_id=client_id)
    query = {"client_id": client_id, "response_type": "code", "redirect_uri": REDIRECT, "scope": SCOPES,
             "code_challenge_method": "S256", "code_challenge": challenge, "state": state}
    return f"{AUTH}?{urllib.parse.urlencode(query)}"


def post_form(url: str, form: dict) -> dict:
    data = urllib.parse.urlencode(form).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=10) as response:  # noqa: S310 (fixed https host)
        return json.load(response)


def finish(pasted: str) -> None:
    """Swap the code in the pasted address (the one that didn't load) for tokens, and keep them."""
    query = urllib.parse.parse_qs(urllib.parse.urlparse(pasted.strip()).query)
    if "error" in query:
        raise ValueError(f"Spotify said: {query['error'][0]}")
    code, state = (query.get("code") or [""])[0], (query.get("state") or [""])[0]
    with _lock:
        pending = dict(_pending)
    if not code:
        raise ValueError("paste the whole address your browser went to after you approved (it has ?code= in it)")
    if not pending or state != pending.get("state"):
        raise ValueError("that sign-in has expired: press Sign in with Spotify again")
    try:
        tokens = post_form(TOKEN, {"grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT,
                                   "client_id": pending["client_id"], "code_verifier": pending["verifier"]})
    except urllib.error.HTTPError as error:
        raise ValueError(f"Spotify refused the sign-in ({error.code}); check the redirect URI is exactly {REDIRECT}") from None
    save({"client_id": pending["client_id"], "refresh_token": tokens["refresh_token"],
          "access_token": tokens["access_token"], "expires": time.time() + tokens.get("expires_in", 3600) - 60})
    with _lock:
        _pending.clear()


def access_token() -> str | None:
    data = load()
    if not data.get("refresh_token"):
        return None
    if data.get("access_token") and data.get("expires", 0) > time.time():
        return data["access_token"]
    tokens = post_form(TOKEN, {"grant_type": "refresh_token", "refresh_token": data["refresh_token"],
                               "client_id": data["client_id"]})
    data.update(access_token=tokens["access_token"], expires=time.time() + tokens.get("expires_in", 3600) - 60)
    if tokens.get("refresh_token"):                          # Spotify may hand out a new one
        data["refresh_token"] = tokens["refresh_token"]
    save(data)
    return data["access_token"]


# -- what's playing ------------------------------------------------------------------------------

def get_json(url: str, token: str) -> dict | None:
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=10) as response:  # noqa: S310 (fixed https host)
        body = response.read()
        return json.loads(body) if response.status == 200 and body else None


def parse(payload: dict | None, fetched: float) -> Track | None:
    item = (payload or {}).get("item")
    if not item:
        return None                                          # nothing playing (or an ad, or a podcast gap)
    images = (item.get("album") or {}).get("images") or (item.get("show") or {}).get("images") or []
    art = min(images, key=lambda i: abs((i.get("width") or 300) - 300))["url"] if images else ""
    artists = ", ".join(a["name"] for a in item.get("artists", [])) or (item.get("show") or {}).get("name", "")
    return Track(item.get("name", ""), artists, art, int(payload.get("progress_ms") or 0),
                 int(item.get("duration_ms") or 1), bool(payload.get("is_playing")), fetched)


_now: dict = {}                 # {"track", "checked": started, "ok": last answered, "busy", "art": {url: Image}}


def _check() -> None:
    try:
        token = access_token()
        track = parse(get_json(PLAYING, token), time.time()) if token else None
        art = None
        if track and track.art_url and track.art_url not in _now.get("art", {}):
            with urllib.request.urlopen(track.art_url, timeout=10) as response:  # noqa: S310 (Spotify's image host)
                art = Image.open(io.BytesIO(response.read())).convert("RGB").resize((64, 64), Image.LANCZOS)
        with _lock:
            _now.update(track=track, ok=time.time(), refused=False)
            if art is not None:
                _now["art"] = {track.art_url: art}           # just the current cover
    except urllib.error.HTTPError as error:
        # 400/401/403: Spotify won't take our sign-in any more (revoked, or the app was removed). The
        # screen just looks like "nothing playing", so say it in the log, once, not every few seconds.
        with _lock:
            first, _now["refused"] = error.code in (400, 401, 403) and not _now.get("refused"), error.code in (400, 401, 403)
        if first:
            log.warning("Spotify refused the sign-in (%s %s): connect it again in the Spotify module's settings",
                        error.code, error.reason)
    except (OSError, ValueError, KeyError):
        pass                                                 # try again next time; the screen keeps the last reading
    finally:
        with _lock:
            _now["busy"] = False


def now_playing() -> tuple[Track | None, Image.Image | None]:
    """The latest reading and its cover (never blocks); starts a check when the last is old."""
    with _lock:
        due = time.time() - _now.get("checked", 0) > POLL
        if due and not _now.get("busy") and not os.environ.get("BONGO_OFFLINE") and connected():
            _now.update(busy=True, checked=time.time())
            threading.Thread(target=_check, daemon=True, name="spotify").start()
        track = _now.get("track")
        return track, (_now.get("art") or {}).get(track.art_url) if track else None


def known() -> bool:
    """Whether Spotify has answered at least once (or never will be asked: BONGO_OFFLINE), so
    ``now_playing`` is a real reading, however old, rather than "not asked yet"."""
    with _lock:
        return "ok" in _now or bool(os.environ.get("BONGO_OFFLINE"))


def seed(track: Track | None, art: Image.Image | None = None) -> None:
    """Put a reading in place (previews, tests and the demo)."""
    with _lock:
        _now.update(track=track, checked=time.time() + 10 ** 9, ok=time.time(), art={track.art_url: art} if track and art else {})
