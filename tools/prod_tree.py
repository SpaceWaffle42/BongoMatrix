"""Which files a display needs: the contents of the ``prod_device`` branch (and, later, GitHub).

    python tools/prod_tree.py            list the files that go to the device
    python tools/prod_tree.py --dropped  list the development-only files left out
    python tools/prod_tree.py --apply    delete the left-out files from this checkout (CI only)

Everything git tracks goes in, except what DEV_ONLY matches: tests, tools, docs, CI, notes for
coding agents, the dashboard's ?demo files (the device serves its real API instead) and v2
leftovers v3 doesn't use.
"""
from __future__ import annotations

import fnmatch
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

DEV_ONLY = (
    "tests/*",
    "tools/*",
    "docs/*",
    ".gitlab-ci.yml",
    "AGENTS.md",
    "bongo/web/static/demo-animator.html",
    "bongo/web/static/js/demo.js",
    "bongo/web/static/js/demo-modules.json",
    "bongo/web/static/js/demo-proposed.json",
    # left over from v2 on main and unused by v3 (the Sky module computes everything itself):
    # the 30 MB ephemeris and the Aseprite sources
    "resources/astronomy/*",
)


def tracked(root: Path = ROOT) -> list[str]:
    return subprocess.run(["git", "-C", str(root), "ls-files"], capture_output=True, text=True,
                          check=True).stdout.split()


def dev_only(path: str) -> bool:
    return any(fnmatch.fnmatch(path, pattern) for pattern in DEV_ONLY)


def split(files: list[str]) -> tuple[list[str], list[str]]:
    keep = [f for f in files if not dev_only(f)]
    drop = [f for f in files if dev_only(f)]
    return keep, drop


def main(argv: list[str]) -> int:
    keep, drop = split(tracked())
    if "--apply" in argv:
        if drop:
            subprocess.run(["git", "-C", str(ROOT), "rm", "-q", "--", *drop], check=True)
        print(f"kept {len(keep)} files, removed {len(drop)}")
    else:
        print("\n".join(drop if "--dropped" in argv else keep))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
