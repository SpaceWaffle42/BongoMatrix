"""Immutable bounded feed records. All text remains untrusted plain text, not HTML."""
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from hashlib import sha256
from html.parser import HTMLParser
from io import BytesIO
import re
from urllib.parse import urljoin, urlunsplit
from defusedxml import ElementTree as ET
from defusedxml.common import DefusedXmlException
from rss_transport import checked_url

MAX_FEED_BYTES = 2 * 1024 * 1024
MAX_ITEMS = 200


class _Text(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.hidden = [], []

    def handle_starttag(self, tag, attrs):
        if tag in ('script', 'style', 'iframe', 'object'):
            self.hidden.append(tag)
        elif not self.hidden:
            self.parts.append(' ')

    def handle_endtag(self, tag):
        if self.hidden and tag == self.hidden[-1]:
            self.hidden.pop()
        elif not self.hidden:
            self.parts.append(' ')

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def plain(value, limit):
    parser = _Text()
    parser.feed(value)
    parser.close()
    text = ''.join(c if c.isprintable() else ' ' for c in ''.join(parser.parts))
    return ' '.join(text.split())[:limit]


def canonical_link(value, base=''):
    if not value.strip():
        return ''
    try:
        parsed, host, port = checked_url(urljoin(base, value.strip()))
        authority = '[' + host + ']' if ':' in host else host
        if port != (443 if parsed.scheme == 'https' else 80):
            authority += ':' + str(port)
        return urlunsplit((parsed.scheme, authority, parsed.path or '/', parsed.query, ''))
    except ValueError:
        return ''


@dataclass(frozen=True)
class Story:
    keys: tuple[str, ...]
    title: str
    summary: str
    link: str
    published: float | None
    categories: tuple[str, ...] = ()
    media: tuple[str, ...] = ()  # Hints only; NOT security-approved downloads.


@dataclass(frozen=True)
class Feed:
    title: str
    stories: tuple[Story, ...]
    warnings: tuple[str, ...] = ()


def _local(node):
    return node.tag.rsplit('}', 1)[-1]


def _value(node):
    # Serialize XHTML children so the same markup stripping handles both dialects.
    return (node.text or '') + ''.join(ET.tostring(child, encoding='unicode') for child in node)


def _date(value):
    for parse in (lambda x: datetime.fromisoformat(x.replace('Z', '+00:00')), parsedate_to_datetime):
        try:
            value_dt = parse(value)
            if value_dt.tzinfo is not None:
                return value_dt.astimezone(timezone.utc).timestamp()
        except (ValueError, TypeError, OverflowError):
            pass
    return None


def parse_feed(payload, source):
    if not isinstance(payload, bytes) or len(payload) > MAX_FEED_BYTES:
        raise ValueError('Feed response too large')
    try:
        depth, nodes = 0, 0
        parser = ET.iterparse(BytesIO(payload), events=('start', 'end'), forbid_dtd=True,
                              forbid_entities=True, forbid_external=True)
        for event, _node in parser:
            if event == 'start':
                depth += 1
                nodes += 1
                if depth > 32 or nodes > 20000:
                    raise ValueError('Feed XML structure exceeds limits')
            else:
                depth -= 1
        root = parser.root
    except (ET.ParseError, DefusedXmlException):
        raise ValueError('Invalid or unsafe feed XML') from None
    atom = root.tag == '{http://www.w3.org/2005/Atom}feed'
    channel = root if atom else root.find('channel')
    if (not atom and root.tag != 'rss') or channel is None:
        raise ValueError('Expected RSS or Atom feed')
    def field(node, names):
        return next((_value(c) for name in names for c in node if _local(c) == name), '')
    title = plain(field(channel, ('title',)), 300)
    stories, warnings = [], []
    for item in channel:
        if _local(item) != ('entry' if atom else 'item'):
            continue
        if len(stories) == MAX_ITEMS:
            warnings.append('Results limited: 200 items')
            break
        headline = plain(field(item, ('title',)), 300)
        summary = plain(field(item, ('description', 'summary', 'encoded', 'content')), 2000)
        published = _date(field(item, ('pubDate', 'published', 'updated')))
        link = field(item, ('link',))
        if atom:
            link = next((c.get('href', '') for c in item if _local(c) == 'link' and c.get('rel', 'alternate') == 'alternate'), '')
        link = canonical_link(link, source) if link else ''
        identifier = field(item, ('guid', 'id')).strip()[:512]
        keys = []
        if identifier:
            keys.append('id:' + identifier)
        if link:
            keys.append('link:' + link)
        if not keys:
            # Missing dates never dedup across sources: avoiding false-positive loss.
            basis = (headline.casefold(), published, source if published is None else '')
            keys.append('fallback:' + sha256(repr(basis).encode()).hexdigest())
        categories = tuple(plain(c.get('term', _value(c)), 100) for c in item if _local(c) == 'category')[:20]
        media = []
        for child in item.iter():
            name = _local(child)
            is_media = child.tag.startswith('{http://search.yahoo.com/mrss/}') and name in ('thumbnail', 'content')
            if is_media or (name == 'enclosure' and child.get('type', '').startswith(('image/', 'video/'))):
                hint = canonical_link(child.get('url', ''), source)
                if hint and hint not in media:
                    media.append(hint)
                if len(media) == 8:
                    break
        stories.append(Story(tuple(keys), headline, summary, link, published, categories, tuple(media)))
    return Feed(title, tuple(stories), tuple(warnings))


def filter_words(values):
    if not isinstance(values, (list, tuple)) or len(values) > 50:
        raise ValueError('At most 50 filter words required')
    if any(not isinstance(v, str) or len(v) > 64 or not re.fullmatch(r'\w+', v.strip()) for v in values):
        raise ValueError('Filters must be whole words of at most 64 characters')
    return tuple(sorted({v.strip().casefold() for v in values}))


class Rotation:
    """In-memory pass cursor; constructing a new service deliberately resets seen."""
    def __init__(self):
        self.seen = set()
        self.material = None

    def select(self, feeds, *, count=5, include=(), exclude=(), material=()):
        if type(count) is not int or not 1 <= count <= 50:
            raise ValueError('Story count must be 1..50')
        include, exclude = filter_words(include), filter_words(exclude)
        signature = (material, include, exclude)
        if signature != self.material:
            self.seen.clear()
            self.material = signature
        def eligible(story):
            words = set(re.findall(r'\w+', ' '.join((story.title, story.summary, *story.categories)).casefold()))
            return (not include or bool(words.intersection(include))) and not words.intersection(exclude)
        queues = [iter(s for s in feed if eligible(s)) for feed in feeds]
        merged, identities = [], set()
        while queues:
            remaining = []
            for queue in queues:
                for story in queue:
                    duplicate = bool(identities.intersection(story.keys))
                    identities.update(story.keys)
                    if not duplicate:
                        merged.append(story)
                        remaining.append(queue)
                        break
            queues = remaining
        self.seen.intersection_update(identities)
        unseen = [s for s in merged if not self.seen.intersection(s.keys)]
        candidates = unseen or sorted(merged, key=lambda s: s.published if s.published is not None else float('-inf'), reverse=True)
        chosen = tuple(candidates[:count])
        self.seen.update(key for s in chosen for key in s.keys)
        return chosen