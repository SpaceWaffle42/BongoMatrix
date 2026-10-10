"""Non-essential (quarantined) test marker.

Marked tests are skipped by the blocking ``full_test_suite`` job
(``MATRIX_CI_SKIP_NON_ESSENTIAL=1``) and run instead by the
``non_essential_browser_tests`` job, which may fail without blocking the
pipeline. ``scripts/ci_collection.py`` requires the marked set to equal
``ci/non-essential-tests.json`` exactly, so nothing is quarantined silently.
"""
import os
import unittest

MARKER = "__matrix_non_essential__"
SKIP_VARIABLE = "MATRIX_CI_SKIP_NON_ESSENTIAL"


def non_essential(reason):
    def mark(obj):
        setattr(obj, MARKER, reason)
        return unittest.skipIf(
            os.environ.get(SKIP_VARIABLE) == "1",
            f"non-essential ({reason}); runs in non_essential_browser_tests",
        )(obj)

    return mark
