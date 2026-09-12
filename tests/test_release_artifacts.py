from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shutil
import stat
import sys
import unittest
import zipfile
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills" / "find" / "scripts"))
from universal_skill_finder.versioning import VERSION, release_metadata

spec = importlib.util.spec_from_file_location("universal_skill_finder_release_builder", ROOT / "scripts" / "build_release.py")
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)

UNKNOWN_GIT = {"commit": None, "dirty": None, "tags": []}
VERIFIED_GIT = {"commit": "a" * 40, "dirty": False, "tags": [f"v{VERSION}"]}


class ReleaseArtifactTests(unittest.TestCase):
    def fixture(self, root: Path) -> Path:
        root.mkdir()
        for relative in builder.REQUIRED_FILES:
            destination = root / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            origin = ROOT / relative
            destination.write_bytes(origin.read_bytes() if origin.exists() else b"# Architecture\n")
        for relative in builder.SOURCE_DIRECTORIES:
            (root / relative).mkdir(parents=True, exist_ok=True)
        skill = "skills/find"
        shutil.copytree(ROOT / skill, root / skill, dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.egg-info"))
        # A fixture is always an unlicensed candidate until a test explicitly
        # supplies synthetic release evidence. No real Git repository is changed.
        (root / "CHANGELOG.md").write_text(f"# Changelog\n\n## {VERSION} - Unreleased\n", encoding="utf-8")
        return root

    @staticmethod
    def finalize_fixture(root: Path) -> None:
        license_bytes = b"Synthetic test license, not a project license.\n"
        (root / "LICENSE").write_bytes(license_bytes)
        (root / "skills" / "find" / "LICENSE").write_bytes(license_bytes)
        (root / "CHANGELOG.md").write_text(f"# Changelog\n\n## {VERSION} - 2026-09-05\n", encoding="utf-8")

    @staticmethod
    def git_tree(files: dict[str, bytes], *, algorithm: str = "sha1") -> bytes:
        entries = []
        for path, content in sorted(files.items()):
            header = b"blob " + str(len(content)).encode("ascii") + b"\0"
            object_id = hashlib.new(algorithm, header + content, usedforsecurity=False).hexdigest()
            entries.append(b"100644 blob " + object_id.encode("ascii") + b"\t" + os.fsencode(path) + b"\0")
        return b"".join(entries)

    @contextmanager
    def git_objects(self, files: dict[str, bytes], *, algorithm: str = "sha1", tree: bytes | None = None):
        """Provide only immutable object-format/tree reads, never real Git writes."""
        records = self.git_tree(files, algorithm=algorithm) if tree is None else tree

        def read_only_run(arguments, **kwargs):
            command = arguments[3:]
            self.assertNotIn("shell", kwargs)
            if command == ["rev-parse", "--show-object-format"]:
                return SimpleNamespace(stdout=(algorithm + "\n").encode("ascii"))
            if command[:3] == ["ls-tree", "-rz", "--full-tree"] and len(command) == 4:
                self.assertRegex(command[3], r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
                return SimpleNamespace(stdout=records)
            self.fail(f"unexpected Git operation: {command}")

        with patch.object(builder.shutil, "which", return_value="git"), patch.object(builder.subprocess, "run", side_effect=read_only_run) as operation:
            yield operation

    def test_archive_manifest_checksums_are_deterministic_and_complete(self):
        with TemporaryDirectory() as temporary:
            work = Path(temporary)
            root = self.fixture(work / "source")
            with patch.object(builder, "git_metadata", return_value=UNKNOWN_GIT):
                first = builder.build_release(root, work / "first")
                os.utime(root / "README.md", (1, 1))
                second = builder.build_release(root, work / "second")
            first_archive = Path(first["archive"])
            second_archive = Path(second["archive"])
            self.assertEqual(first_archive.read_bytes(), second_archive.read_bytes())
            self.assertEqual(Path(first["manifest"]).read_bytes(), Path(second["manifest"]).read_bytes())
            self.assertEqual(first["checksum"], hashlib.sha256(first_archive.read_bytes()).hexdigest())
            checksum = first_archive.with_suffix(".zip.sha256").read_text(encoding="ascii")
            self.assertEqual(checksum, f"{first['checksum']}  {first_archive.name}\n")
            manifest = json.loads(Path(first["manifest"]).read_bytes())
            self.assertEqual(manifest["git"], UNKNOWN_GIT)
            self.assertEqual(manifest["release_status"], "candidate")
            self.assertEqual(manifest["project"], "universal-skill-finder")
            self.assertEqual(first_archive.name, f"universal-skill-finder-{VERSION}-candidate.zip")
            self.assertEqual(manifest["provenance"]["release_version"], VERSION)
            expected = builder.collect_files(root)
            self.assertEqual([item["path"] for item in manifest["files"]], sorted(expected))
            inventory_bytes = json.dumps(manifest["files"], sort_keys=True, separators=(",", ":")).encode("utf-8")
            self.assertEqual(manifest["source_revision"], "sha256:" + hashlib.sha256(inventory_bytes).hexdigest())
            with zipfile.ZipFile(first_archive) as archive:
                prefix = first_archive.stem + "/"
                self.assertEqual(archive.namelist(), sorted(archive.namelist()))
                self.assertEqual(set(archive.namelist()), {prefix + path for path in expected} | {prefix + "release-manifest.json"})
                self.assertEqual(archive.read(prefix + "release-manifest.json"), Path(first["manifest"]).read_bytes())
                self.assertIn("![Universal Skill Finder search example](img.png)", archive.read(prefix + "README.md").decode("utf-8"))
                self.assertEqual(archive.read(prefix + "img.png"), (root / "img.png").read_bytes())
                for item in manifest["files"]:
                    content = archive.read(prefix + item["path"])
                    self.assertEqual(content, expected[item["path"]])
                    self.assertEqual(item["bytes"], len(content))
                    self.assertEqual(item["sha256"], hashlib.sha256(content).hexdigest())
                for entry in archive.infolist():
                    self.assertEqual(entry.date_time, (1980, 1, 1, 0, 0, 0))
                    self.assertEqual(stat.S_IMODE(entry.external_attr >> 16), 0o644)

    def test_source_change_changes_source_revision(self):
        with TemporaryDirectory() as temporary:
            work = Path(temporary)
            root = self.fixture(work / "source")
            with patch.object(builder, "git_metadata", return_value=UNKNOWN_GIT):
                before = builder.build_release(root, work / "before")
                (root / "README.md").write_text("# Changed documentation\n", encoding="utf-8")
                after = builder.build_release(root, work / "after")
            self.assertNotEqual(before["source_revision"], after["source_revision"])
            self.assertNotEqual(before["checksum"], after["checksum"])

    def test_standalone_skill_license_is_included_in_candidate_archive(self):
        with TemporaryDirectory() as temporary:
            work = Path(temporary)
            root = self.fixture(work / "source")
            self.finalize_fixture(root)
            with patch.object(builder, "git_metadata", return_value=UNKNOWN_GIT):
                result = builder.build_release(root, work / "output")
            archive_path = Path(result["archive"])
            with zipfile.ZipFile(archive_path) as archive:
                prefix = archive_path.stem + "/"
                self.assertEqual(archive.read(prefix + "skills/find/LICENSE"), archive.read(prefix + "LICENSE"))

    def test_standalone_skill_requires_the_same_license_as_the_project(self):
        with TemporaryDirectory() as temporary:
            root = self.fixture(Path(temporary) / "source")
            self.finalize_fixture(root)
            files = builder.collect_files(root)
            for content in (None, b"", b"Different license\n"):
                with self.subTest(content=content):
                    changed = {path: data for path, data in files.items() if path != "skills/find/LICENSE"}
                    if content is not None:
                        changed["skills/find/LICENSE"] = content
                    with self.assertRaisesRegex(ValueError, "standalone skill license"):
                        builder.release_blockers(changed, UNKNOWN_GIT)

    def test_collection_omits_caches_ide_and_credentials(self):
        with TemporaryDirectory() as temporary:
            root = self.fixture(Path(temporary) / "source")
            excluded = [
                "docs/.idea/state.json", "scripts/__pycache__/state.json",
                "docs/.pytest_cache/state.json", "docs/.universal-skill-finder/state.json",
                "docs/.vscode/state.json", "docs/.venv/state.json", "docs/build/output.json",
                "docs/audit-output/output.json", "docs/generated.egg-info/state.json",
                "docs/.env", "docs/key.pem", ".idea/workspace.xml",
                "docs/.env.production.json", "docs/.env.example.json", "scripts/.env.local.py",
                "tests/nested/.env.fixture.yml", "docs/.env.directory/payload.json",
            ]
            for relative in excluded:
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("synthetic local artifact\n", encoding="utf-8")
            files = builder.collect_files(root)
            for relative in excluded:
                with self.subTest(relative=relative):
                    self.assertFalse(relative in files, f"local artifact included in release: {relative}")

    def test_payload_symlink_is_rejected(self):
        with TemporaryDirectory() as temporary:
            work = Path(temporary)
            root = self.fixture(work / "source")
            outside = work / "outside.md"
            outside.write_text("outside the release\n", encoding="utf-8")
            link = root / "docs" / "linked.md"
            try:
                link.symlink_to(outside)
            except OSError as exc:
                if getattr(exc, "winerror", None) == 1314:
                    self.skipTest("Windows symlink privilege unavailable")
                raise
            with self.assertRaisesRegex(ValueError, "symlink"):
                builder.collect_files(root)

    def test_plugin_and_marketplace_versions_must_match(self):
        with TemporaryDirectory() as temporary:
            root = self.fixture(Path(temporary) / "source")
            files = builder.collect_files(root)
            for relative in (".codex-plugin/plugin.json", ".claude-plugin/plugin.json", ".claude-plugin/marketplace.json", ".agents/plugins/marketplace.json"):
                with self.subTest(relative=relative):
                    metadata = json.loads(files[relative])
                    if "marketplace" in relative:
                        metadata["plugins"][0]["version"] = "99.0.0"
                    else:
                        metadata["version"] = "99.0.0"
                    changed = {**files, relative: json.dumps(metadata).encode("utf-8")}
                    with self.assertRaisesRegex(ValueError, "version"):
                        builder.release_blockers(changed, UNKNOWN_GIT)

    def test_project_plugin_skill_and_marketplace_have_independent_identities(self):
        self.assertEqual(builder.PROJECT, "universal-skill-finder")
        self.assertEqual(builder.PLUGIN, "skill")
        self.assertEqual(builder.SKILL, "find")
        self.assertEqual(builder.MARKETPLACE, "skill")
        self.assertNotEqual(builder.PROJECT, builder.PLUGIN)
        self.assertNotEqual(builder.PLUGIN, builder.SKILL)
        with TemporaryDirectory() as temporary:
            root = self.fixture(Path(temporary) / "source")
            files = builder.collect_files(root)
            self.assertIn("skills/find/SKILL.md", files)
            # The repository directory does not determine the plugin name.
            self.assertEqual(root.name, "source")
            self.assertEqual(len(builder.release_blockers(files, UNKNOWN_GIT)), 5)

    def test_release_rejects_mismatched_plugin_and_marketplace_identities(self):
        with TemporaryDirectory() as temporary:
            root = self.fixture(Path(temporary) / "source")
            files = builder.collect_files(root)
            for relative in (".codex-plugin/plugin.json", ".claude-plugin/plugin.json",
                             ".claude-plugin/marketplace.json", ".agents/plugins/marketplace.json"):
                for wrong_name in (builder.PROJECT, builder.SKILL):
                    with self.subTest(relative=relative, wrong_name=wrong_name):
                        metadata = json.loads(files[relative])
                        metadata["name"] = wrong_name
                        changed = {**files, relative: json.dumps(metadata).encode("utf-8")}
                        with self.assertRaisesRegex(ValueError, "plugin name|marketplace identity"):
                            builder.release_blockers(changed, UNKNOWN_GIT)
                if "marketplace" in relative:
                    for wrong_name in (builder.PROJECT, builder.SKILL):
                        with self.subTest(relative=relative, plugin_entry=wrong_name):
                            metadata = json.loads(files[relative])
                            metadata["plugins"][0]["name"] = wrong_name
                            changed = {**files, relative: json.dumps(metadata).encode("utf-8")}
                            with self.assertRaisesRegex(ValueError, "marketplace identity"):
                                builder.release_blockers(changed, UNKNOWN_GIT)

    def test_release_rejects_mismatched_missing_or_empty_skill_metadata(self):
        with TemporaryDirectory() as temporary:
            root = self.fixture(Path(temporary) / "source")
            files = builder.collect_files(root)
            path = "skills/find/SKILL.md"
            invalid = [b"", b"---\nname: find\n---\n", b"---\nname: find\ndescription: ''\n---\n"]
            invalid.extend(f"---\nname: {name}\ndescription: Search skills\n---\n".encode("utf-8")
                           for name in (builder.PLUGIN, builder.PROJECT))
            for content in invalid:
                with self.subTest(content=content):
                    with self.assertRaisesRegex(ValueError, "skill identity"):
                        builder.release_blockers({**files, path: content}, UNKNOWN_GIT)
            without_skill = {relative: data for relative, data in files.items() if relative != path}
            with self.assertRaisesRegex(ValueError, "skill identity"):
                builder.release_blockers(without_skill, UNKNOWN_GIT)

    def test_skill_metadata_version_matches_canonical_version(self):
        prefix = "---\nname: find\ndescription: Search skills\n"
        for value in (VERSION, f'"{VERSION}"', f"'{VERSION}'"):
            with self.subTest(value=value):
                builder.validate_skill_metadata(prefix + f"metadata:\n  version: {value}\n---\n")
        invalid = (
            "", "metadata:\n  author: Example\n", "metadata:\n  version: 99.0.0\n",
            f"version: {VERSION}\n", f"unrelated:\n  version: {VERSION}\n",
        )
        for metadata in invalid:
            with self.subTest(metadata=metadata):
                with self.assertRaisesRegex(ValueError, "metadata.version"):
                    builder.validate_skill_metadata(prefix + metadata + "---\n")

    def test_marketplace_source_still_points_at_plugin_root_not_skill_directory(self):
        with TemporaryDirectory() as temporary:
            root = self.fixture(Path(temporary) / "source")
            files = builder.collect_files(root)
            for relative in (".claude-plugin/marketplace.json", ".agents/plugins/marketplace.json"):
                with self.subTest(relative=relative):
                    metadata = json.loads(files[relative])
                    metadata["plugins"][0]["source"] = "./skills/find"
                    changed = {**files, relative: json.dumps(metadata).encode("utf-8")}
                    with self.assertRaisesRegex(ValueError, "marketplace source"):
                        builder.release_blockers(changed, UNKNOWN_GIT)

    def test_final_gates_require_license_git_clean_tag_and_dated_changelog(self):
        with TemporaryDirectory() as temporary:
            work = Path(temporary)
            root = self.fixture(work / "source")
            files = builder.collect_files(root)
            blockers = builder.release_blockers(files, UNKNOWN_GIT)
            self.assertEqual(len(blockers), 5)
            for fragment in ("license", "Git commit", "clean Git", "tagged", "changelog"):
                self.assertTrue(any(fragment in blocker for blocker in blockers))
            with patch.object(builder, "git_metadata", return_value=UNKNOWN_GIT):
                with self.assertRaisesRegex(ValueError, "final release blocked"):
                    builder.build_release(root, work / "output", final=True)
            self.assertFalse((work / "output").exists())
            self.finalize_fixture(root)
            finalized = builder.collect_files(root)
            self.assertEqual(builder.release_blockers(finalized, VERIFIED_GIT), [])
            for partial_git, expected in (({**VERIFIED_GIT, "dirty": True}, "clean Git"), ({**VERIFIED_GIT, "tags": []}, "tagged")):
                self.assertTrue(any(expected in item for item in builder.release_blockers(finalized, partial_git)))

    def test_verified_final_fixture_has_no_blockers(self):
        with TemporaryDirectory() as temporary:
            work = Path(temporary)
            root = self.fixture(work / "source")
            self.finalize_fixture(root)
            with patch.object(builder, "git_metadata", return_value=VERIFIED_GIT), self.git_objects(builder.collect_files(root)):
                result = builder.build_release(root, work / "output", final=True)
            self.assertEqual(result["release_status"], "final")
            self.assertEqual(result["promotion_blockers"], [])
            self.assertNotIn("candidate", Path(result["archive"]).name)
            manifest = json.loads(Path(result["manifest"]).read_bytes())
            self.assertEqual(manifest["git"], VERIFIED_GIT)
            self.assertIn("LICENSE", [item["path"] for item in manifest["files"]])

    def test_existing_artifact_prevents_any_overwrite_or_partial_set(self):
        with TemporaryDirectory() as temporary:
            work = Path(temporary)
            root = self.fixture(work / "source")
            output = work / "output"
            output.mkdir()
            existing = output / f"universal-skill-finder-{VERSION}-candidate.manifest.json"
            existing.write_bytes(b"user-owned existing artifact")
            with patch.object(builder, "git_metadata", return_value=UNKNOWN_GIT):
                with self.assertRaisesRegex(ValueError, "refusing to overwrite"):
                    builder.build_release(root, output)
            self.assertEqual(existing.read_bytes(), b"user-owned existing artifact")
            self.assertEqual(list(output.iterdir()), [existing])

    def test_missing_git_remains_unknown_and_parent_repository_is_rejected(self):
        with TemporaryDirectory() as temporary:
            work = Path(temporary)
            with patch.object(builder.shutil, "which", return_value=None):
                self.assertEqual(builder.git_metadata(work), UNKNOWN_GIT)
            completed = type("Completed", (), {"stdout": str(work.parent) + "\n"})()
            with patch.object(builder.shutil, "which", return_value="git"), patch.object(builder.subprocess, "run", return_value=completed) as run:
                self.assertEqual(builder.git_metadata(work), UNKNOWN_GIT)
                self.assertEqual(run.call_count, 1)
                self.assertIn("--show-toplevel", run.call_args.args[0])

    def test_provenance_describes_captured_payload_not_later_live_edits(self):
        with TemporaryDirectory() as temporary:
            work = Path(temporary)
            root = self.fixture(work / "source")
            package = root / "skills" / "find" / "scripts" / "universal_skill_finder"
            expected = release_metadata(package)
            collect = builder.collect_files

            def snapshot_then_edit(path: Path):
                files = collect(path)
                (package / "__init__.py").write_text("# File changed after archive snapshot\n", encoding="utf-8")
                (package / "data" / "sources.default.json").write_text('{"schema_version":1,"sources":[]}', encoding="utf-8")
                return files

            with patch.object(builder, "collect_files", side_effect=snapshot_then_edit), patch.object(builder, "git_metadata", return_value=UNKNOWN_GIT):
                result = builder.build_release(root, work / "output")
            manifest = json.loads(Path(result["manifest"]).read_bytes())
            self.assertEqual(manifest["provenance"], expected)
            self.assertNotEqual(release_metadata(package), expected)

    def test_final_requires_every_captured_file_to_be_tracked_even_when_status_is_clean(self):
        with TemporaryDirectory() as temporary:
            work = Path(temporary)
            root = self.fixture(work / "source")
            self.finalize_fixture(root)
            tracked = builder.collect_files(root)
            # Model a global Git exclude: status reports clean, but ls-tree has
            # no record of this otherwise allowlisted payload file.
            (root / "docs" / "ignored-private.json").write_text('{"synthetic":"private"}', encoding="utf-8")
            with patch.object(builder, "git_metadata", return_value=VERIFIED_GIT), self.git_objects(tracked):
                with self.assertRaisesRegex(ValueError, "not tracked.*docs/ignored-private.json"):
                    builder.build_release(root, work / "output", final=True)
            self.assertFalse((work / "output").exists())

    def test_final_rejects_bytes_hidden_by_assume_unchanged(self):
        with TemporaryDirectory() as temporary:
            work = Path(temporary)
            root = self.fixture(work / "source")
            self.finalize_fixture(root)
            tracked = builder.collect_files(root)
            (root / "README.md").write_text("Changed bytes hidden from Git status\n", encoding="utf-8")
            with patch.object(builder, "git_metadata", return_value=VERIFIED_GIT), self.git_objects(tracked):
                with self.assertRaisesRegex(ValueError, "bytes differ.*README.md"):
                    builder.build_release(root, work / "output", final=True)
            self.assertFalse((work / "output").exists())

    def test_final_rejects_git_state_change_during_capture(self):
        with TemporaryDirectory() as temporary:
            work = Path(temporary)
            root = self.fixture(work / "source")
            self.finalize_fixture(root)
            changed = {**VERIFIED_GIT, "commit": "b" * 40}
            with patch.object(builder, "git_metadata", side_effect=[VERIFIED_GIT, changed]):
                with self.assertRaisesRegex(ValueError, "Git state changed"):
                    builder.build_release(root, work / "output", final=True)
            self.assertFalse((work / "output").exists())

    def test_final_rechecks_git_state_after_blob_verification(self):
        with TemporaryDirectory() as temporary:
            work = Path(temporary)
            root = self.fixture(work / "source")
            self.finalize_fixture(root)
            changed = {**VERIFIED_GIT, "tags": []}
            with patch.object(builder, "git_metadata", side_effect=[VERIFIED_GIT, VERIFIED_GIT, changed]), self.git_objects(builder.collect_files(root)):
                with self.assertRaisesRegex(ValueError, "Git state changed during payload verification"):
                    builder.build_release(root, work / "output", final=True)
            self.assertFalse((work / "output").exists())

    def test_candidate_clears_unstable_git_identity_and_reports_blocker(self):
        with TemporaryDirectory() as temporary:
            work = Path(temporary)
            root = self.fixture(work / "source")
            changed = {**VERIFIED_GIT, "commit": "b" * 40}
            with patch.object(builder, "git_metadata", side_effect=[VERIFIED_GIT, changed]):
                result = builder.build_release(root, work / "output")
            manifest = json.loads(Path(result["manifest"]).read_bytes())
            self.assertEqual(manifest["git"], UNKNOWN_GIT)
            self.assertIn("Git state changed while capturing release files", manifest["promotion_blockers"])

    def test_blob_verification_supports_sha1_sha256_and_unquoted_paths(self):
        files = {"docs/unicode-ø.md": b"same bytes\n", "docs/tab\tname.md": b"tab filename\n"}
        with TemporaryDirectory() as temporary:
            for algorithm, length in (("sha1", 40), ("sha256", 64)):
                with self.subTest(algorithm=algorithm), self.git_objects(files, algorithm=algorithm) as operations:
                    builder.verify_tracked_payload(Path(temporary), files, "a" * length)
                    self.assertEqual(operations.call_count, 2)

    def test_blob_verification_rejects_nonregular_and_malformed_git_entries(self):
        files = {"README.md": b"release bytes\n"}
        valid = self.git_tree(files)
        cases = [
            valid.replace(b"100644 blob", b"120000 blob"),
            valid.replace(b"100644 blob", b"160000 commit"),
            valid + valid,
            valid.rstrip(b"\0"),
            b"invalid\0",
        ]
        with TemporaryDirectory() as temporary:
            for tree in cases:
                with self.subTest(tree=tree[:30]), self.git_objects(files, tree=tree):
                    with self.assertRaises(ValueError):
                        builder.verify_tracked_payload(Path(temporary), files, "a" * 40)
            with self.git_objects(files), patch.object(builder, "MAX_GIT_TREE_BYTES", 4):
                with self.assertRaisesRegex(ValueError, "oversized"):
                    builder.verify_tracked_payload(Path(temporary), files, "a" * 40)


if __name__ == "__main__":
    unittest.main()
