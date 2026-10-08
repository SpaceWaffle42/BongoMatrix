"""Test-only provisioned browser binding; production launcher bytes stay unchanged."""
import os
import subprocess
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

LEGACY_CHROME = '/opt/google/chrome/chrome'

def browser_executable():
    # Canonical CI explicitly supplies MATRIX_CHROMIUM; local Chrome is fallback.
    configured = os.environ.get('MATRIX_CHROMIUM')
    candidates = [configured] if configured else ['/usr/bin/chromium', LEGACY_CHROME]
    for candidate in candidates:
        if Path(candidate).is_file() and os.access(candidate, os.X_OK):
            return candidate
    raise FileNotFoundError('No executable provisioned MATRIX_CHROMIUM/local browser')

def bound_popen(real_popen, argv, **kwargs):
    if isinstance(argv, (list, tuple)) and argv and argv[0] == LEGACY_CHROME:
        argv = [browser_executable(), *argv[1:]]
    return real_popen(argv, **kwargs)

@contextmanager
def provisioned_browser(caller):
    """Keep real Popen/argv/env/lifecycle; verify every owned child was reaped."""
    real_popen = subprocess.Popen
    children = []
    def launch(argv, **kwargs):
        child = bound_popen(real_popen, argv, **kwargs)
        children.append(child)
        return child
    try:
        with patch.object(caller.subprocess, 'Popen', side_effect=launch):
            yield children
    finally:
        leaked = [child for child in children if child.poll() is None]
        for child in leaked:
            caller.stop_owned(child)
        if leaked:
            raise AssertionError('chrome_journey did not reap its owned children')
