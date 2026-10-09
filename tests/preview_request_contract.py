"""Fail-closed request admission for offline browser evidence, before dispatch.
Timing POSTs calculate drafts; may fill volatile caches/read providers, never Save.
No pairing credentials or production authorization are supplied by this policy.
"""
from urllib.parse import urlsplit

TIMING_POSTS = frozenset('/api/scroll-timing/' + name for name in (
    'announcement_board', 'weather', 'openweather_overview', 'openweather_forecast'))
READS = frozenset(('/', '/api/config', '/api/media', '/api/screen-media',
                   '/api/status', '/api/display-frame'))
PREVIEW_POSTS = frozenset('/api/edit-preview/' + name for name in ('splash', 'sleep', 'page', 'cancel'))

class RequestGate:
    def __init__(self, base, *, product_journey=False):
        self.origin = urlsplit(base)
        self.product_journey = product_journey
        self.save_armed = False
        self.allowed = []
        self.blocked = []

    def permits(self, method, url):
        parsed = urlsplit(url)
        if (parsed.scheme, parsed.netloc) != (self.origin.scheme, self.origin.netloc):
            return False
        path = parsed.path
        if method in ('GET', 'HEAD'):
            return path in READS or path.startswith(('/static/', '/media/', '/api/media-timeline/'))
        if method == 'POST' and path in TIMING_POSTS:
            return True
        if self.product_journey and method == 'POST':
            if path in PREVIEW_POSTS:
                return True
            if path == '/api/config' and self.save_armed:
                self.save_armed = False
                return True
        return False

    def dispatch(self, route):
        request = route.request
        entry = {'method': request.method, 'path': urlsplit(request.url).path}
        if self.permits(request.method, request.url):
            self.allowed.append(entry)
            route.continue_()
        else:
            self.blocked.append(entry)
            route.abort('blockedbyclient')

    def install(self, context):
        context.route('**/*', self.dispatch)
