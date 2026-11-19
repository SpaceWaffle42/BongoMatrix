"""What reads as a secret: the net a backup is strained through, under what modules say for themselves
(``Module.secrets``; see bongo/backup.py).

Two questions, both answered from the look of things alone (nothing here knows a module or opens a file):

- ``name(name, value)``: is a setting called this a secret? ``apiKey``, ``x-api-key``, ``wifi_password``, ``pwd``,
  ``otp``, ``ical_url``... The name is taken apart first (case, camelCase, hyphens, dots, underscores), so one
  list of words serves every spelling.
- ``text(value)``: does this text carry one, whatever it is called? A web address with anything before an ``@``
  in its host part, or with a parameter whose name reads as a secret (``?feed_token=``, ``&key=``, ``#access_token=``);
  a private calendar link (an ``.ics`` or ``webcal`` address with a long random part); a web address of one of the
  well-known kinds whose PATH is the key (``WEBHOOKS`` below: Slack, Discord, Telegram, IFTTT, smart-home
  hub webhooks, a published Google sheet); a ``WIFI:`` code with its password in; a JWT, or ``Bearer`` / ``Authorization:``
  followed by something token-like; an AWS access key id; one long run of random letters and digits. Percent-encoded
  addresses are read decoded (``user%3Apass%40host`` is ``user:pass@host``).

A name counts by its parts, not by the letters in it: ``secretary``, ``token_count``, ``session_length``,
``keys_layout`` and ``primary_key`` are ordinary settings (``_QUALIFIERS``, ``_NOT_SECRET``).

Both err on the side of leaving out: a backup that lacks a harmless setting says so when it is restored, and
one that holds a key can't be put right. What neither can tell stays a limit, and the dashboard says so: a key in
the path of an address that looks like any other path, and whatever is typed into free text (a Text screen's
words, an announcement, a date's title). E-mail addresses are left as they are.
"""
from __future__ import annotations

import re
import urllib.parse

# words that only look like one of these (checked as whole name parts first)
_NOT_SECRET = {"secretary", "secretaries", "secretariat", "passenger", "passengers", "passes", "passage", "bypass", "compass",
               "sessions", "authority", "author", "authors", "authentic", "authenticity", "keyboard", "keyword", "keywords",
               "keynote", "keystone", "pinned", "pins", "signage", "token_less"}
# a last part that makes the name about a secret's shape, not the secret: token_count, session_length, keys_layout
_QUALIFIERS = {"length", "len", "count", "counts", "num", "number", "size", "limit", "max", "min", "maximum", "minimum",
               "layout", "timeout", "duration", "seconds", "minutes", "hours", "days", "type", "kind", "format", "style",
               "colour", "color", "position", "mode", "enabled", "shown", "show", "visible", "label", "name", "names"}
# two parts together that mean something else entirely: primary_key, sort_key
_COMPOUNDS = {("primary", "key"), ("sort", "key"), ("foreign", "key"), ("unique", "key"), ("partition", "key"),
              ("hot", "key"), ("short", "key"), ("musical", "key"), ("key", "signature")}
# anywhere in one of the name's words: "passwd", "accesstoken", "sessionid", "authorization"
_INSIDE = ("passw", "passphrase", "passcode", "pincode", "secret", "token", "credential", "apikey", "privatekey", "signature",
           "session", "bearer", "cookie", "webhook", "authoriz", "authoris")
# one of the name's words, whole: "pin" but not "spin", "key" but not "keyboard"
_WORDS = {"pass", "pwd", "pswd", "pin", "otp", "totp", "jwt", "key", "keys", "auth", "oauth", "psk", "sig", "hmac", "creds",
          "ics", "ical", "webcal", "dsn", "llat", "salt"}
# two of its words together: "access_code", "calendar_link", "private_url"
_PAIRS = (({"access", "pass", "pin", "security", "auth", "door", "entry"}, {"code"}),
          ({"calendar", "private", "secret"}, {"url", "link", "address", "feed"}),
          ({"connection"}, {"string", "uri", "url"}))
_ALONE = {"refresh", "private"}   # only as the whole name ("refresh_minutes" is how often, not a refresh token)
# ...and as a parameter of a web address, these too
_PARAMETERS = {"access", "code", "appid", "sign", "signed", "password", "pass", "key"}
_COLOUR = re.compile(r"#[0-9a-fA-F]{6}")
_ADDRESS = re.compile(r"(?:\b([a-z][a-z0-9+.-]*):)?//([^\s\"'<>]+)", re.I)
_PARAMETER = re.compile(r"[?&;#]([^=&;#?\s/]{1,60})=([^&;#\s\"'<>]+)")
_WIFI = re.compile(r"\s*WIFI:(?:.*;)?P:(?!;|\"\";|$)", re.I | re.S)
# token-like: 12 or more of base64's or hex's letters, no spaces, with a digit or both cases in (not a plain word)
_C = r"[A-Za-z0-9._~+/=-]"
_TOKENISH = rf"(?={_C}*(?:[0-9]|[a-z]{_C}*[A-Z]|[A-Z]{_C}*[a-z])){_C}{{12,}}(?!{_C})"
_SAYS = re.compile(r"\beyJ[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{4,}"                                # a JWT (they all start {" in base64)
                   r"|\b(?i:bearer)\s+" + _TOKENISH +                                         # Bearer <token-like>
                   r"|(?i:\b(?:proxy-)?authori[sz]ation\s*[:=]\s*(?:(?:bearer|basic|token|digest)\s+)?)" + _TOKENISH +
                   r"|\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"                                           # an AWS access key id
                   r"|(?i:\bx-(?:api-key|auth-token|auth|access-token)\s*[:=]\s*\S)")        # X-Api-Key: ...
# web addresses whose path is the key: "host/path" (lower case, no scheme) is matched
WEBHOOKS = re.compile(r"hooks\.slack\.com/(?:services|workflows|triggers)/"
                      r"|(?:^|\.)discord(?:app)?\.com/api/(?:v\d+/)?webhooks/\d+/"
                      r"|api\.telegram\.org/(?:file/)?bot\d+:"
                      r"|maker\.ifttt\.com/(?:trigger/[^/]+/(?:json/)?)?with/key/"
                      r"|/api/webhook/[^/?#]+"
                      r"|docs\.google\.com/.*/d/e/2pacx-"
                      r"|outlook\.office\.com/webhook/|\.webhook\.office\.com/")
_RANDOM = re.compile(r"[A-Za-z0-9_\-+/=~]+")


def words(name: str) -> list[str]:
    """``privateKey``, ``X-Api-Key``, ``wifi.password2`` -> their words, lower case: private key; x api key; wifi password."""
    name = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", name)
    name = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", name)
    return [w for w in (re.sub(r"\d+$", "", w) for w in re.split(r"[^a-z0-9]+", name.lower())) if w]


def _named(name, extra=()) -> bool:
    if not isinstance(name, str) or len(name) > 200:
        return False
    parts = words(name)
    if not parts:
        return False
    if len(parts) > 1 and parts[-1] in _QUALIFIERS:                        # token_count, session_length, keys_layout
        return False
    if any(pair in _COMPOUNDS for pair in zip(parts, parts[1:])):          # primary_key, sort_key
        return False
    parts = [w for w in parts if w not in _NOT_SECRET]                     # secretary, keyboard: whole words only
    if not parts:
        return False
    if any(inside in w for w in parts for inside in _INSIDE) or any(w in _WORDS or w in extra for w in parts):
        return True
    if len(parts) == 1 and parts[0] in _ALONE:
        return True
    return any(set(parts) & first and set(parts) & second for first, second in _PAIRS)


def name(name, value="x") -> bool:
    """Whether a setting called ``name`` reads as a secret, unless its ``value`` can't be one: a switch, a colour,
    nothing at all."""
    if value is None or isinstance(value, bool) or value == "" or (isinstance(value, str) and _COLOUR.fullmatch(value)):
        return False
    return _named(name)


def _random(part: str, least: int) -> bool:
    """Whether ``part`` looks made up by a machine: long, one run of letters and digits, the two well mixed."""
    if len(part) < least or not _RANDOM.fullmatch(part):
        return False
    kinds = [c.isdigit() for c in part if c.isalnum()]
    return sum(a != b for a, b in zip(kinds, kinds[1:])) >= 3


def _private_link(scheme: str, rest: str) -> bool:
    """A calendar's private link: an .ics, ical or webcal address with a long random part (or one marked private)."""
    host, _, tail = rest.partition("/")
    path, _, query = tail.partition("?")
    low = path.lower()
    if not (scheme.startswith("webcal") or low.split("#")[0].endswith(".ics") or "ical/" in low or "caldav" in host.lower()):
        return False
    for part in path.split("#")[0].split("/"):
        part = urllib.parse.unquote(part)
        if part.lower().startswith(("private-", "private_")) or _random(part.rsplit(".", 1)[0] if part.lower().endswith(".ics") else part, 16):
            return True
    return any(_random(urllib.parse.unquote(v), 16) for _, _, v in (pair.partition("=") for pair in re.split(r"[&;#]", query)))


def text(value) -> bool:
    """Whether ``value`` (a setting's text) carries a secret: see the top of this file."""
    if not isinstance(value, str) or len(value) < 6:
        return False
    if _WIFI.match(value) or _SAYS.search(value) or _random(value.strip(), 32):
        return True
    for found in _ADDRESS.finditer(value):
        scheme, rest = (found.group(1) or "").lower(), found.group(2)
        host = urllib.parse.unquote(re.split(r"[/?#]", rest, maxsplit=1)[0])   # (user%3Apass%40host is user:pass@host)
        if "@" in host:   # anything before an @ in the host part: a name, a password, a token
            return True
        if WEBHOOKS.search(rest.lower()):
            return True
        if _private_link(scheme, rest):
            return True
    for found in _PARAMETER.finditer(value):   # ?name=value, wherever it is (an address needn't say https://)
        if _named(urllib.parse.unquote(found.group(1)), _PARAMETERS):
            return True
    return False


def address(value) -> bool:
    """Whether ``value`` is or holds a web address at all (one that passed ``text`` may still have a key in its path)."""
    return isinstance(value, str) and bool(_ADDRESS.search(value) or re.search(r"\bwww\.[a-z0-9-]+\.[a-z]", value, re.I))
