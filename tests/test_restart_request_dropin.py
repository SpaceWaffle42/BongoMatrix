"""The installed matrix-web unit must let the dashboard publish a restart request.

Production still runs the original installed unit (Group=root, no write access to
/run/matrix-device-restart/submit), so RestartRequest refused every press with
"unsafe fixed restart request directory". The reviewed drop-in below adds only the
identity and paths the fixed publisher needs, without the rest of the repo unit
(whose ProcSubset=pid hides /proc/cpuinfo from the RGB driver).
"""

import configparser
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import webapp

ROOT = Path(__file__).resolve().parents[1]
DROPIN = ROOT / "deploy" / "matrix-web.service.d" / "restart-request.conf"


def _directives(text):
    entries = []
    section = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", ";")):
            continue
        if line.startswith("["):
            section = line
            continue
        key, value = line.split("=", 1)
        entries.append((section, key, value))
    return entries


class RestartRequestDropInTests(unittest.TestCase):
    def test_dropin_grants_exactly_the_restart_publisher_identity_and_paths(self):
        self.assertTrue(DROPIN.is_file(), "deploy/matrix-web.service.d/restart-request.conf is missing")
        self.assertEqual(
            _directives(DROPIN.read_text(encoding="ascii")),
            [
                ("[Service]", "Group", "matrix-web"),
                ("[Service]", "ReadWritePaths", "/run/matrix-device-restart/submit"),
                ("[Service]", "InaccessiblePaths", "/run/matrix-device-restart/state"),
            ],
        )

    def test_dropin_never_widens_privilege_or_hides_hardware_detection(self):
        text = DROPIN.read_text(encoding="ascii")
        for forbidden in (
            "User=", "CapabilityBoundingSet", "AmbientCapabilities", "NoNewPrivileges",
            "ProcSubset", "ProtectProc", "ExecStart", "ReadWritePaths=/run/matrix-device-restart\n",
            "ReadWritePaths=/run/matrix-device-restart/state", "DeviceAllow", "DevicePolicy",
        ):
            self.assertNotIn(forbidden, text)

    def test_dropin_group_matches_the_tmpfiles_submit_directory_group(self):
        policy = (ROOT / "deploy" / "matrix-device-restart.conf").read_text(encoding="ascii")
        self.assertIn("d /run/matrix-device-restart/submit 1730 root matrix-web -", policy)
        group = dict((k, v) for _s, k, v in _directives(DROPIN.read_text(encoding="ascii")))["Group"]
        self.assertEqual(group, "matrix-web")

    def test_publisher_accepts_submit_directory_only_when_its_group_is_the_process_group(self):
        # Reproduces production: the submit directory is root:<group> 1730. With the
        # process egid different (Group=root) the publisher refuses; with the drop-in's
        # group it publishes the fixed marker.
        with tempfile.TemporaryDirectory(prefix="matrix-restart-dropin-", dir="/root") as directory:
            submit = Path(directory) / "submit"
            submit.mkdir()
            os.chmod(submit, 0o1730)
            request = webapp.RestartRequest(submit / "request")
            real_open = webapp._open_trusted_restart_directory

            def trusted(path):
                return os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)

            with patch.object(webapp, "_open_trusted_restart_directory", trusted):
                other_group = os.getegid() + 4242
                with patch("webapp.os.getegid", return_value=other_group):
                    with self.assertRaisesRegex(RuntimeError, "unsafe fixed restart request directory"):
                        request()
                self.assertFalse((submit / "request").exists())
                self.assertIs(request(), webapp.RestartRequestOutcome.CREATED)
            self.assertIs(real_open, webapp._open_trusted_restart_directory)
            info = (submit / "request").lstat()
            self.assertEqual(stat.S_IMODE(info.st_mode), 0o600)
            self.assertEqual(info.st_size, 0)

    def test_install_documentation_installs_and_verifies_the_dropin(self):
        install = (ROOT / "deploy" / "matrix-ci-physical-install.md").read_text(encoding="utf-8")
        self.assertIn(
            "install -o root -g root -m 0644 deploy/matrix-web.service.d/restart-request.conf "
            "/etc/systemd/system/matrix-web.service.d/restart-request.conf",
            install,
        )


if __name__ == "__main__":
    unittest.main()
