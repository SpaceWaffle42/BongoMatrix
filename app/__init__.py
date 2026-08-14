"""Matrix Flask application package."""

import threading


# ``app.wsgi`` can also be imported as top-level ``wsgi`` by WSGI servers.
# Keep process-entrypoint ownership on the shared package, not either alias.
_wsgi_runtime_lock = threading.Lock()
_wsgi_runtime = None
