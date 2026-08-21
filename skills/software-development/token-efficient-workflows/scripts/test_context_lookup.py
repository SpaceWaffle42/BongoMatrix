#!/usr/bin/env python3
"""Regression tests for context_lookup.py."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).with_name("context_lookup.py")
SPEC = importlib.util.spec_from_file_location("context_lookup", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


@contextlib.contextmanager
def temporary_repository():
    with tempfile.TemporaryDirectory(prefix="hermes-context-test-") as directory:
        root = Path(directory)
        subprocess.run(["git", "init", "-q"], cwd=root, check=True)
        yield root


def stage_all(root: Path) -> None:
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)


class ContextLookupTests(unittest.TestCase):
    def test_heading_slice_stops_at_same_or_higher_level(self) -> None:
        lines = ["# Root", "intro", "## Wanted", "body", "### Child", "child", "## Next"]
        self.assertEqual(MODULE.heading_slice(lines, "Wanted"), (2, 6))
        self.assertEqual(MODULE.heading_slice(lines, "## wanted"), (2, 6))

    def test_missing_heading_fails_without_fallback(self) -> None:
        with self.assertRaisesRegex(SystemExit, "Heading not found"):
            MODULE.heading_slice(["# Existing"], "Missing")

    def test_query_slices_merge_overlapping_context(self) -> None:
        lines = ["zero", "match one", "middle", "match two", "four"]
        self.assertEqual(MODULE.query_slices(lines, "match", 1, 10), [(0, 5)])

    def test_query_respects_match_limit(self) -> None:
        lines = ["hit", "x", "hit", "x", "hit"]
        self.assertEqual(MODULE.query_slices(lines, "hit", 0, 2), [(0, 1), (2, 3)])

    def test_query_is_literal_and_not_a_regular_expression(self) -> None:
        pattern = "(a+)+$"
        self.assertEqual(MODULE.query_slices([f"prefix {pattern} suffix"], pattern, 0, 1), [(0, 1)])
        with self.assertRaisesRegex(SystemExit, r"No query matches: \(a\+\)\+\$"):
            MODULE.query_slices(["a" * 100_001 + "!"], pattern, 0, 1)

    def test_query_and_heading_errors_escape_terminal_controls(self) -> None:
        control = "missing\x1b]0;forged\x07"
        with self.assertRaises(SystemExit) as query_error:
            MODULE.query_slices(["safe"], control, 0, 1)
        with self.assertRaises(SystemExit) as heading_error:
            MODULE.heading_slice(["# Safe"], control)
        for error in (query_error.exception, heading_error.exception):
            rendered = str(error)
            self.assertNotIn("\x1b", rendered)
            self.assertNotIn("\x07", rendered)
            self.assertIn(r"\x1b]0;forged\x07", rendered)

    def test_emit_is_line_bounded_and_reports_truncation(self) -> None:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            MODULE.emit(["a", "b", "c"], "tracked.md", [(0, 3)], 2, 100)
        rendered = output.getvalue()
        self.assertIn("1|a", rendered)
        self.assertIn("2|b", rendered)
        self.assertNotIn("3|c", rendered)
        self.assertIn("TRUNCATED=max-lines:2,max-chars:100", rendered)
        self.assertIn("EMITTED_LINES=2", rendered)

    def test_emit_reports_omitted_later_slice(self) -> None:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            MODULE.emit(["first", "middle", "second"], "tracked.md", [(0, 1), (2, 3)], 1, 100)
        rendered = output.getvalue()
        self.assertIn("1|first", rendered)
        self.assertNotIn("3|second", rendered)
        self.assertIn("TRUNCATED=max-lines:1,max-chars:100", rendered)

    def test_emit_caps_single_long_line_by_characters(self) -> None:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            MODULE.emit(["x" * 200_000], "tracked.md", [(0, 1)], 1, 32)
        rendered = output.getvalue()
        self.assertIn("1|" + "x" * 32 + "\n", rendered)
        self.assertNotIn("x" * 33, rendered)
        self.assertIn("TRUNCATED=max-lines:1,max-chars:32", rendered)
        self.assertIn("EMITTED_CHARS=32", rendered)

    def test_display_path_escapes_framing_controls(self) -> None:
        self.assertEqual(MODULE.display_path("line\nname\x1b.txt"), r"line\nname\x1b.txt")

    def test_tracked_path_accepts_source_and_rejects_git_metadata(self) -> None:
        root = MODULE.repository_root(Path.cwd())
        path, relative = MODULE.tracked_relative_path(root, "docs/FUTURE_DEVELOPMENT.md")
        self.assertTrue(path.is_file())
        self.assertEqual(relative, "docs/FUTURE_DEVELOPMENT.md")
        with self.assertRaisesRegex(SystemExit, "Refusing untracked source"):
            MODULE.tracked_relative_path(root, ".git/HEAD")

    def test_outside_repository_is_rejected(self) -> None:
        root = MODULE.repository_root(Path.cwd())
        with self.assertRaisesRegex(SystemExit, "outside repository"):
            MODULE.tracked_relative_path(root, "/etc/hosts")

    def test_symlink_to_outside_repository_is_rejected(self) -> None:
        with temporary_repository() as root:
            outside = root.parent / f"{root.name}-outside.txt"
            try:
                outside.write_text("OUTSIDE_CONTENT\n")
                (root / "linked.txt").symlink_to(outside)
                stage_all(root)
                with self.assertRaisesRegex(SystemExit, "outside repository"):
                    MODULE.tracked_relative_path(root, str(root / "linked.txt"))
            finally:
                outside.unlink(missing_ok=True)

    def test_index_read_ignores_unstaged_worktree_substitution(self) -> None:
        with temporary_repository() as root:
            source = root / "tracked.txt"
            source.write_text("STAGED_CONTENT\n")
            stage_all(root)
            source.write_text("UNSTAGED_SECRET_CONTENT\n")
            self.assertEqual(MODULE.read_index_text(root, "tracked.txt"), "STAGED_CONTENT\n")

    def test_index_read_is_stable_after_path_validation(self) -> None:
        with temporary_repository() as root:
            source = root / "tracked.txt"
            source.write_text("STAGED_CONTENT\n")
            stage_all(root)
            _, relative = MODULE.tracked_relative_path(root, str(source))
            source.write_text("MUTATED_AFTER_VALIDATION\n")
            self.assertEqual(MODULE.read_index_text(root, relative), "STAGED_CONTENT\n")

    def test_index_read_ignores_hardlink_mutation_after_stage(self) -> None:
        with temporary_repository() as root:
            outside = root.parent / f"{root.name}-outside.txt"
            try:
                outside.write_text("STAGED_CONTENT\n")
                os.link(outside, root / "tracked.txt")
                stage_all(root)
                outside.write_text("OUTSIDE_SECRET_AFTER_STAGE\n")
                self.assertEqual(MODULE.read_index_text(root, "tracked.txt"), "STAGED_CONTENT\n")
            finally:
                outside.unlink(missing_ok=True)

    def test_literal_leading_colon_filename_is_supported(self) -> None:
        with temporary_repository() as root:
            source = root / ":colon.txt"
            source.write_text("COLON_CONTENT\n")
            stage_all(root)
            _, relative = MODULE.tracked_relative_path(root, str(source))
            self.assertEqual(relative, ":colon.txt")
            self.assertEqual(MODULE.read_index_text(root, relative), "COLON_CONTENT\n")

    def test_newline_filename_is_exactly_resolved_and_safely_framed(self) -> None:
        with temporary_repository() as root:
            source = root / "line\nname.txt"
            source.write_text("SAFE_CONTENT\n")
            stage_all(root)
            _, relative = MODULE.tracked_relative_path(root, str(source))
            self.assertEqual(MODULE.read_index_text(root, relative), "SAFE_CONTENT\n")
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                MODULE.emit(["SAFE_CONTENT"], relative, [(0, 1)], 1, 100)
            self.assertIn(r"SOURCE=line\nname.txt", output.getvalue())
            self.assertNotIn("SOURCE=line\nname.txt\nname", output.getvalue())

    def test_control_bearing_utf8_blob_is_rejected(self) -> None:
        with temporary_repository() as root:
            (root / "control.txt").write_text("safe\n\x1b]0;forged title\x07\n")
            stage_all(root)
            with self.assertRaisesRegex(SystemExit, "Refusing control-bearing text"):
                MODULE.read_index_text(root, "control.txt")

    def test_oversized_blob_is_rejected_before_read(self) -> None:
        with temporary_repository() as root:
            (root / "large.txt").write_text("12345")
            stage_all(root)
            with self.assertRaisesRegex(SystemExit, "max-source-bytes"):
                MODULE.read_index_text(root, "large.txt", max_source_bytes=4)

    def test_non_utf8_blob_is_rejected(self) -> None:
        with temporary_repository() as root:
            (root / "binary.bin").write_bytes(b"\xff\xfe")
            stage_all(root)
            with self.assertRaisesRegex(SystemExit, "non-UTF-8"):
                MODULE.read_index_text(root, "binary.bin")

    def test_activation_readme_uses_plain_external_directory_value(self) -> None:
        root = MODULE.repository_root(Path.cwd())
        readme = (root / "skills" / "README.md").read_text()
        command = "hermes config set skills.external_dirs /absolute/path/to/checkout/skills"
        self.assertIn(command, readme)
        self.assertNotIn("skills.external_dirs '[", readme)

    def test_repository_root_preserves_trailing_space(self) -> None:
        with tempfile.TemporaryDirectory(prefix="hermes-context-test-") as directory:
            root = Path(directory) / "repository-with-space "
            root.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            self.assertEqual(MODULE.repository_root(root), root.resolve())


if __name__ == "__main__":
    unittest.main()
