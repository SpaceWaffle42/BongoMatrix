import hashlib
import importlib.util
import json
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path


_REPOSITORY = Path(__file__).resolve().parents[1]
_VERIFIER_PATH = _REPOSITORY / "scripts" / "verify_priority_1_3_remote.py"
_FOCUSED_SELECTOR = (
    "tests.test_webapp.WebAppTests."
    "test_physical_draft_preview_api_is_paired_bounded_private_and_non_persistent"
)


def _load_verifier():
    spec = importlib.util.spec_from_file_location("priority_1_3_remote_verifier_test", _VERIFIER_PATH)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class ProductionTestVerifierTests(unittest.TestCase):
    def _real_app(self, directory: str) -> Path:
        root = Path(directory) / "app"
        root.mkdir()
        completed = subprocess.run(
            ["/usr/bin/python3", "-m", "venv", "--without-pip", str(root / "venv")],
            check=False, text=True, capture_output=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        (root / "actual_app.py").write_text(
            "def production_value():\n    return 73\n", encoding="utf-8"
        )
        tests = root / "tests"
        tests.mkdir()
        (tests / "__init__.py").write_text("", encoding="utf-8")
        (tests / "test_webapp.py").write_text(
            "import pathlib, unittest\n"
            "from actual_app import production_value\n"
            "ROOT = pathlib.Path(__file__).resolve().parents[1]\n"
            "class WebAppTests(unittest.TestCase):\n"
            "    def test_physical_draft_preview_api_is_paired_bounded_private_and_non_persistent(self):\n"
            "        self.assertEqual(production_value(), 73)\n"
            "        (ROOT / 'focused.marker').write_text('actual focused test ran')\n"
            "    def test_full_discovery_executes_actual_production_test(self):\n"
            "        self.assertEqual(production_value(), 73)\n"
            "        (ROOT / 'full.marker').write_text('actual discovery test ran')\n",
            encoding="utf-8",
        )
        return root

    def _command(self, root: Path) -> list[str]:
        return [
            str(_REPOSITORY / ".venv/bin/python"), "-I", "-B", str(_VERIFIER_PATH),
            "--check", "production-tests", "--mode", "deploy",
            "--app-root", str(root), "--attempt-id", "a" * 32,
            "--acceptance-operation-id", "c" * 64, "--evidence-id", "b" * 64,
            "--expected-sha", "1" * 40, "--expected-version", "9.8.7-test",
        ]

    def test_standard_venv_runs_exact_real_tests_and_binds_bounded_stderr(self):
        with tempfile.TemporaryDirectory(prefix="production-verifier-") as directory:
            root = self._real_app(directory)
            python = root / "venv/bin/python"
            self.assertTrue(python.is_symlink(), "fixture must retain python -m venv symlink chain")
            completed = subprocess.run(
                self._command(root), cwd=_REPOSITORY,
                check=False, text=True, capture_output=True,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            report = json.loads(completed.stdout)
            self.assertEqual(report["acceptance_operation_id"], "c" * 64)
            self.assertEqual((root / "focused.marker").read_text(), "actual focused test ran")
            self.assertEqual((root / "full.marker").read_text(), "actual discovery test ran")
            for key in ("focused", "full"):
                result = report[key]
                self.assertTrue(result["passed"])
                self.assertGreater(result["stderr_size"], 0)
                self.assertLessEqual(result["stderr_size"], 1024 * 1024)
                self.assertRegex(result["stderr_digest"], r"^[0-9a-f]{64}$")
            self.assertRegex(report["focused"]["command_digest"], r"^[0-9a-f]{64}$")

    def test_interpreter_admission_rejects_writable_hop_loop_and_untrusted_target(self):
        verifier = _load_verifier()
        with tempfile.TemporaryDirectory(prefix="interpreter-admission-") as directory:
            root = self._real_app(directory)
            trusted_uid = os.geteuid()
            admitted = verifier._validated_interpreter(
                root, trusted_uid=trusted_uid,
                trusted_system_roots=(Path("/usr/bin"),),
            )
            self.assertEqual(admitted, root / "venv/bin/python")

            python = root / "venv/bin/python"
            system_final = python.resolve()
            confined_final = python.parent / "python-confined"
            confined_final.write_bytes(system_final.read_bytes())
            confined_final.chmod(0o775)
            python.unlink()
            python.symlink_to(confined_final.name)
            with self.assertRaisesRegex(ValueError, "writable"):
                verifier._validated_interpreter(
                    root, trusted_uid=trusted_uid,
                    trusted_system_roots=(Path("/usr/bin"),),
                )
            confined_final.chmod(0o755)

            python.unlink()
            python.symlink_to("python-loop")
            (python.parent / "python-loop").symlink_to("python")
            with self.assertRaisesRegex(ValueError, "loop"):
                verifier._validated_interpreter(
                    root, trusted_uid=trusted_uid,
                    trusted_system_roots=(Path("/usr/bin"),),
                )

            python.unlink()
            (python.parent / "python-loop").unlink()
            outside = Path(directory) / "outside-python"
            outside.write_bytes(Path("/usr/bin/python3").read_bytes())
            outside.chmod(0o755)
            python.symlink_to(outside)
            with self.assertRaisesRegex(ValueError, "trusted interpreter roots"):
                verifier._validated_interpreter(
                    root, trusted_uid=trusted_uid,
                    trusted_system_roots=(Path("/usr/bin"),),
                )

    def test_generic_verifier_command_remains_stderr_strict(self):
        verifier = _load_verifier()
        with self.assertRaisesRegex(ValueError, "production command failed"):
            verifier._run(
                ["/usr/bin/python3", "-I", "-B", "-c",
                 "import sys; sys.stderr.write('ordinary diagnostic\\n')"],
                10,
            )


if __name__ == "__main__":
    unittest.main()
