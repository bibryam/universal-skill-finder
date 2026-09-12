from __future__ import annotations

import hashlib
import json
import os
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills" / "find" / "scripts"))

from universal_skill_finder.installed import annotate_installed
from test_presentation import report, result


INSTRUCTIONS = b"---\nname: pdf\ndescription: Read PDF forms\n---\n\nRead the document.\n"
DIGEST = hashlib.sha256(INSTRUCTIONS).hexdigest()


class InstalledEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory(prefix="universal-skill-finder-installed-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.cwd = self.root / "project"
        self.home = self.root / "home"
        self.cwd.mkdir()
        self.home.mkdir()

    def write_skill(self, root: Path, name="pdf", content=INSTRUCTIONS) -> Path:
        location = root / name
        location.mkdir(parents=True, exist_ok=True)
        (location / "SKILL.md").write_bytes(content)
        return location

    def scan(self, found=None, assistant="codex", *, roots=None, environ=None):
        found = report(results=[result(content_sha256=DIGEST)]) if found is None else found
        with patch("socket.socket", side_effect=AssertionError("network forbidden")):
            annotate_installed(found, assistant, cwd=self.cwd, home=self.home,
                               environ={} if environ is None else environ, roots=roots)
        return found

    def test_matching_hash_proves_only_matching_instructions_not_whole_bundle(self):
        location = self.write_skill(self.cwd / ".agents" / "skills")
        (location / "scripts").mkdir()
        (location / "scripts" / "dangerous.py").write_text("raise AssertionError('must not execute')\n", encoding="utf-8")
        found = self.scan()
        self.assertEqual(found.results[0].installed, {
            "status": "matching_instructions", "evidence": ["skill_md_sha256"], "scopes": ["project"]})
        self.assertEqual(found.installed_scan["status"], "complete")
        self.assertEqual(found.installed_scan["skills_read"], 1)
        self.assertTrue(any("bundle" in text for text in found.installed_scan["limitations"]))

    def test_one_scan_annotates_unmaterialized_frozen_results_for_later_pages(self):
        self.write_skill(self.cwd / ".agents" / "skills")
        visible = result(id="skill:visible", name="other", content_sha256=None)
        later = result(id="skill:later", name="pdf", content_sha256=DIGEST)
        found = report(results=[visible])
        found.snapshot = {
            "result_records": {
                visible.id: visible.to_dict(),
                later.id: later.to_dict(),
            },
        }
        self.scan(found)
        self.assertEqual(found.results[0].installed["status"], "not_found")
        self.assertEqual(
            found.snapshot["result_records"][later.id]["installed"],
            {"status": "matching_instructions", "evidence": ["skill_md_sha256"], "scopes": ["project"]},
        )

    def test_same_name_with_different_hash_is_collision_and_keeps_install_proposal(self):
        self.write_skill(self.cwd / ".agents" / "skills", content=INSTRUCTIONS + b"Different workflow.\n")
        found = report(results=[result(content_sha256=DIGEST)])
        proposal = dict(found.results[0].install)
        self.scan(found)
        self.assertEqual(found.results[0].installed["status"], "name_collision")
        self.assertEqual(found.results[0].install, proposal)
        self.assertEqual(found.results[0].content_sha256, DIGEST)

    def test_directory_name_and_frontmatter_names_are_evidence_not_identity(self):
        self.write_skill(self.cwd / ".agents" / "skills", name="pdf", content=b"---\nname: unrelated\n---\nOther instructions.\n")
        found = self.scan()
        self.assertEqual(found.results[0].installed["status"], "name_collision")
        self.assertNotIn("canonical_local_directory", found.results[0].installed["evidence"])

    def test_unknown_hash_and_same_name_stays_unverified(self):
        self.write_skill(self.cwd / ".agents" / "skills")
        for digest in (None, "", "not-a-hash", "f" * 63):
            with self.subTest(digest=digest):
                found = self.scan(report(results=[result(content_sha256=digest)]))
                self.assertEqual(found.results[0].installed["status"], "unknown")
                self.assertEqual(found.results[0].installed["evidence"], ["name_only"])

    def test_same_names_in_multiple_scopes_are_not_assumed_same_source(self):
        self.write_skill(self.cwd / ".agents" / "skills", content=INSTRUCTIONS + b"Project variant")
        self.write_skill(self.home / ".agents" / "skills", content=INSTRUCTIONS + b"User variant")
        found = self.scan()
        self.assertEqual(found.results[0].installed["status"], "name_collision")
        self.assertEqual(found.results[0].installed["scopes"], ["project", "user"])

    def test_matching_instructions_and_a_conflicting_name_preserve_both_evidence_types(self):
        self.write_skill(self.cwd / ".agents" / "skills")
        self.write_skill(self.home / ".agents" / "skills", content=INSTRUCTIONS + b"Another version")
        found = self.scan()
        self.assertEqual(found.results[0].installed["status"], "matching_instructions")
        self.assertEqual(found.results[0].installed["collision_scopes"], ["user"])
        self.assertIn("same_name_different_instructions", found.results[0].installed["evidence"])

    def test_exact_local_directory_requires_local_occurrence_and_canonical_file_url(self):
        location = self.write_skill(self.cwd / ".agents" / "skills")
        local = result(repository=None, skill_path=None, ref=None, canonical_url=location.as_uri(),
                       install={}, occurrences=[{"adapter": "local-directory", "source_kind": "repository"}])
        found = self.scan(report(results=[local]))
        self.assertEqual(found.results[0].installed["status"], "exact_local")
        self.assertEqual(found.results[0].installed["evidence"], ["canonical_local_directory"])

    def test_remote_install_kind_and_path_do_not_establish_local_identity(self):
        location = self.write_skill(self.cwd / ".agents" / "skills")
        remote = result(content_sha256=None, repository=None, canonical_url=location.as_uri(),
                        install={"kind": "local", "path": str(location)},
                        occurrences=[{"adapter": "http-json-v1", "source_kind": "registry"}])
        found = self.scan(report(results=[remote]))
        self.assertEqual(found.results[0].installed["status"], "unknown")
        self.assertEqual(found.results[0].installed["evidence"], ["name_only"])

    def test_current_assistant_selects_only_its_project_and_user_roots(self):
        self.write_skill(self.cwd / ".agents" / "skills")
        self.write_skill(self.home / ".codex" / "skills", name="legacy")
        codex = self.scan()
        claude = self.scan(assistant="claude-code")
        self.assertEqual(codex.results[0].installed["status"], "matching_instructions")
        self.assertEqual(codex.results[0].installed["scopes"], ["project", "user_legacy"])
        self.assertEqual(claude.results[0].installed["status"], "not_found")
        self.write_skill(self.home / ".claude" / "skills")
        self.assertEqual(self.scan(assistant="claude-code").results[0].installed["scopes"], ["user"])

    def test_codex_home_override_isolated_and_does_not_read_other_environment_values(self):
        configured = self.root / "selected-codex"
        self.write_skill(configured / "skills")
        self.write_skill(self.home / ".codex" / "skills", name="ignored", content=INSTRUCTIONS + b"Ignored")
        environment = {"CODEX_HOME": str(configured), "EXAMPLE_SECRET": "must-not-appear"}
        found = self.scan(environ=environment)
        self.assertEqual(found.installed_scan["skills_read"], 1)
        self.assertEqual(found.results[0].installed["scopes"], ["user_legacy"])
        text = json.dumps(found.installed_scan) + json.dumps(found.results[0].installed)
        self.assertNotIn("must-not-appear", text)
        self.assertNotIn(str(self.root), text)

    def test_no_ancestor_directory_scan_and_explicit_roots_override_defaults(self):
        self.write_skill(self.root / ".agents" / "skills")
        found = self.scan()
        self.assertEqual(found.installed_scan["skills_read"], 0)
        self.assertEqual(found.results[0].installed["status"], "not_found")
        custom = self.root / "explicit"
        self.write_skill(custom)
        self.assertEqual(self.scan(roots=[("custom", custom)]).results[0].installed["scopes"], ["custom"])

    def test_unknown_assistant_and_no_results_do_not_touch_filesystem(self):
        for assistant, found in ((None, report()), ("unknown", report()), ("codex", report(results=[]))):
            with self.subTest(assistant=assistant), patch("universal_skill_finder.installed._open_root", side_effect=AssertionError("scan forbidden")):
                self.scan(found, assistant=assistant)
                self.assertEqual(found.installed_scan["status"], "not_checked")
                self.assertEqual(found.installed_scan["reason"], "no_results" if assistant == "codex" else "assistant_unknown")

    def test_missing_current_directory_or_home_never_discards_search_results(self):
        for method, options in (("cwd", {"home": self.home}), ("home", {"cwd": self.cwd})):
            for error in (OSError("private unavailable location"), RuntimeError("private unavailable location")):
                with self.subTest(method=method, error=type(error).__name__):
                    found = report(results=[result(content_sha256=DIGEST)])
                    with patch(f"universal_skill_finder.installed.Path.{method}", side_effect=error), \
                         patch("universal_skill_finder.installed._open_root", side_effect=AssertionError("scan forbidden")):
                        annotate_installed(found, "codex", environ={}, **options)
                    self.assertEqual(found.installed_scan["status"], "partial")
                    self.assertEqual(found.installed_scan["reason"], "root_resolution_failed")
                    self.assertEqual(found.results[0].installed["status"], "unknown")
                    self.assertEqual(found.results[0].id, "skill:test")
                    self.assertNotIn("private unavailable location", json.dumps(found.installed_scan))

    def test_root_normalization_failure_keeps_prior_positive_evidence(self):
        scanned = self.root / "existing"
        self.write_skill(scanned)
        with patch("universal_skill_finder.installed._absolute", side_effect=[scanned, OSError("private location")]):
            found = self.scan(roots=[("project", scanned), ("user", self.root / "unavailable")])
        self.assertEqual(found.installed_scan["status"], "partial")
        self.assertEqual(found.results[0].installed["status"], "matching_instructions")
        self.assertIn("scan_incomplete", found.results[0].installed["evidence"])
        self.assertEqual(found.installed_scan["roots"][1]["issues"], ["root_resolution_failed"])
        self.assertNotIn("private location", json.dumps(found.installed_scan))

    def test_symlinked_skill_directory_and_file_are_skipped_with_partial_scan(self):
        outside = self.write_skill(self.root / "outside")
        for kind in ("directory", "file", "root"):
            with self.subTest(kind=kind):
                scanned = self.root / f"scan-{kind}"
                scanned.mkdir()
                try:
                    if kind == "directory":
                        (scanned / "linked").symlink_to(outside, target_is_directory=True)
                    elif kind == "file":
                        (scanned / "SKILL.md").symlink_to(outside / "SKILL.md")
                    else:
                        (scanned / "linked-root").symlink_to(outside.parent, target_is_directory=True)
                        scanned = scanned / "linked-root"
                except OSError as exc:
                    if getattr(exc, "winerror", None) == 1314:
                        self.skipTest("Windows symlink privilege unavailable")
                    raise
                found = self.scan(roots=[("custom", scanned)])
                self.assertEqual(found.installed_scan["status"], "partial")
                self.assertEqual(found.installed_scan["skills_read"], 0)
                self.assertEqual(found.results[0].installed["status"], "unknown")

    def test_permission_errors_report_unknown_not_absent(self):
        with patch("universal_skill_finder.installed._open_root", side_effect=PermissionError("private path hidden")):
            found = self.scan()
        self.assertEqual(found.installed_scan["status"], "partial")
        self.assertEqual(found.results[0].installed["status"], "unknown")
        self.assertNotIn("private path", json.dumps(found.installed_scan))

    def test_unreadable_and_invalid_instruction_files_make_scan_partial(self):
        scanned = self.root / "unreadable"
        self.write_skill(scanned)
        with patch("universal_skill_finder.installed._read_skill", side_effect=PermissionError("hidden filename")):
            found = self.scan(roots=[("custom", scanned)])
        self.assertEqual(found.installed_scan["status"], "partial")
        self.assertEqual(found.results[0].installed["status"], "unknown")
        self.assertNotIn("hidden filename", json.dumps(found.installed_scan))
        self.write_skill(scanned, content=b"\xff\xfe\x00\x00")
        found = self.scan(roots=[("custom", scanned)])
        self.assertEqual(found.installed_scan["status"], "partial")
        self.assertIn("invalid_text", found.installed_scan["roots"][0]["issues"])

    def test_root_limit_marks_unscanned_roots_unknown(self):
        first = self.root / "first"
        second = self.root / "second"
        first.mkdir()
        self.write_skill(second)
        with patch("universal_skill_finder.installed.MAX_ROOTS", 1):
            found = self.scan(roots=[("first", first), ("second", second)])
        self.assertEqual(found.installed_scan["status"], "partial")
        self.assertEqual(found.installed_scan["roots"][1]["status"], "not_checked")
        self.assertEqual(found.results[0].installed["status"], "unknown")

    def test_portable_fallback_reads_regular_files_and_explains_weaker_boundary(self):
        self.write_skill(self.cwd / ".agents" / "skills")
        with patch("universal_skill_finder.installed._anchored_supported", return_value=False):
            found = self.scan()
        self.assertEqual(found.results[0].installed["status"], "matching_instructions")
        self.assertTrue(any("weaker protection" in text for text in found.installed_scan["limitations"]))

    def test_only_instruction_files_are_opened_not_credentials_or_companion_files(self):
        location = self.write_skill(self.home / ".codex" / "skills")
        (self.home / ".codex" / "auth.json").write_text("synthetic credential file", encoding="utf-8")
        (location / "secret.json").write_text("synthetic companion data", encoding="utf-8")
        (location / "nested").mkdir()
        (location / "nested" / "SKILL.md").write_bytes(INSTRUCTIONS)
        original_open = os.open

        def guarded_open(path, flags, *args, **kwargs):
            self.assertNotIn(Path(path).name, {"auth.json", "secret.json", "nested"})
            return original_open(path, flags, *args, **kwargs)

        with patch("universal_skill_finder.installed.os.open", side_effect=guarded_open):
            found = self.scan()
        self.assertEqual(found.installed_scan["skills_read"], 1)
        self.assertEqual(found.results[0].installed["status"], "matching_instructions")

    def test_file_total_entry_skill_and_depth_limits_leave_partial_evidence(self):
        scan_root = self.root / "bounded"
        self.write_skill(scan_root)
        limits = [("MAX_SKILL_BYTES", 1), ("MAX_TOTAL_BYTES", 1), ("MAX_ENTRIES", 0),
                  ("MAX_SKILLS", 0), ("MAX_DEPTH", 0)]
        for constant, value in limits:
            with self.subTest(limit=constant), patch(f"universal_skill_finder.installed.{constant}", value):
                found = self.scan(roots=[("custom", scan_root)])
                self.assertEqual(found.installed_scan["status"], "partial")
                self.assertEqual(found.results[0].installed["status"], "unknown")

    @unittest.skipUnless(hasattr(os, "mkfifo"), "FIFO test needs POSIX")
    def test_special_skill_file_is_never_read(self):
        scan_root = self.root / "fifo"
        scan_root.mkdir()
        os.mkfifo(scan_root / "SKILL.md")
        found = self.scan(roots=[("custom", scan_root)])
        self.assertEqual(found.installed_scan["status"], "partial")
        self.assertEqual(found.installed_scan["skills_read"], 0)
        self.assertEqual(found.results[0].installed["status"], "unknown")

    def test_scanning_preserves_ranking_results_and_writes_no_cache_or_configuration(self):
        self.write_skill(self.cwd / ".agents" / "skills")
        found = report(results=[result(id="first", name="other", content_sha256=None),
                                result(id="second", content_sha256=DIGEST)])
        before = [item.to_dict() for item in found.results]
        self.scan(found)
        self.assertEqual([item.id for item in found.results], ["first", "second"])
        after = [item.to_dict() for item in found.results]
        for original, changed in zip(before, after):
            original.pop("installed", None)
            changed.pop("installed", None)
            self.assertEqual(original, changed)
        self.assertFalse((self.home / ".cache").exists())
        self.assertFalse((self.home / ".config").exists())
        self.assertEqual([path.name for path in (self.cwd / ".agents" / "skills").iterdir()], ["pdf"])


if __name__ == "__main__":
    unittest.main()
