#!/usr/bin/env python3
"""Fixed forced-command dispatcher for guarded Priority 1.3 release controls."""
from __future__ import annotations

import os
import re

ENDPOINT = "/usr/local/libexec/matrix-priority-1-3-remote"
PYTHON = "/home/admin/matrix/venv/bin/python"
HANDLER = "/usr/local/lib/matrix-priority-1-3/deploy/guarded_priority_1_3_release.py"
HELPERS = frozenset({
    "remote-start", "remote-operation-inspect", "remote-stop", "remote-recover",
    "remote-commit", "remote-transaction-create-or-query", "remote-transaction-claim",
    "remote-transaction-inspect", "remote-transaction-transition",
    "remote-transaction-advance", "remote-transaction-terminal",
})
_COMMAND = re.compile(
    re.escape(ENDPOINT) + r" ([a-z0-9-]+) --request-b64 ([A-Za-z0-9_-]+)\Z"
)
EXEC_ENVIRONMENT = {"LANG": "C", "LC_ALL": "C", "PATH": "/usr/bin:/bin"}


def build_exec(original_command: str) -> tuple[list[str], dict[str, str]]:
    if type(original_command) is not str or len(original_command.encode("utf-8")) > 70000:
        raise ValueError("SSH original command is invalid")
    match = _COMMAND.fullmatch(original_command)
    if match is None:
        raise ValueError("SSH original command is not canonical")
    helper, payload = match.groups()
    if helper not in HELPERS or len(payload) > 65536:
        raise ValueError("SSH helper request is invalid")
    return [PYTHON, "-I", "-B", HANDLER, helper, "--request-b64", payload], dict(EXEC_ENVIRONMENT)


def main(*, environment: dict[str, str] | None = None) -> int:
    source = os.environ if environment is None else environment
    try:
        argv, clean_environment = build_exec(source.get("SSH_ORIGINAL_COMMAND", ""))
        os.execve(argv[0], argv, clean_environment)
    except (OSError, ValueError, TypeError):
        return 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
