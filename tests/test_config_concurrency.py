from __future__ import annotations

import hashlib
import json
import multiprocessing
import sys
import unittest
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills" / "find" / "scripts"))

from universal_skill_finder.config import (
    ConfigurationError,
    _overlay_lock,
    empty_overlay,
    load_config,
    save_overlay,
    set_source_enabled,
)


def source_choices(payload):
    return {item["id"]: item["enabled"] for item in payload["sources"]}


def _competing_writer(path, source_id, ready, start, results):
    config = load_config(path)
    ready.put(source_id)
    if not start.wait(10):
        results.put((source_id, "timed out"))
        return
    try:
        set_source_enabled(config, source_id, False)
        results.put((source_id, "saved"))
    except ConfigurationError as exc:
        results.put((source_id, str(exc)))


class ConfigurationConcurrencyTests(unittest.TestCase):
    def test_missing_overlay_snapshot_creates_file_and_updates_fingerprint(self):
        with TemporaryDirectory() as temp:
            path = Path(temp) / "nested" / "sources.json"
            config = load_config(str(path))
            self.assertIsNone(config.overlay_fingerprint)
            self.assertFalse(path.exists())
            set_source_enabled(config, "skillsmp", False)
            self.assertEqual(config.overlay_fingerprint, hashlib.sha256(path.read_bytes()).hexdigest())
            self.assertFalse(source_choices(config.overlay)["skillsmp"])
            self.assertEqual(len(config.overlay["sources"]), len(config.sources))
            self.assertFalse(path.with_name(".sources.json.lock").exists())

    def test_stale_snapshot_cannot_overwrite_another_session(self):
        with TemporaryDirectory() as temp:
            path = Path(temp) / "sources.json"
            save_overlay(path, empty_overlay())
            first, second = load_config(str(path)), load_config(str(path))
            second_original = deepcopy(second.overlay)
            set_source_enabled(first, "skillsmp", False)
            saved = path.read_bytes()
            with self.assertRaisesRegex(ConfigurationError, "changed since it was loaded"):
                set_source_enabled(second, "clawhub", False)
            self.assertEqual(path.read_bytes(), saved)
            self.assertEqual(second.overlay, second_original)
            self.assertEqual(list(Path(temp).glob(".sources-*")), [])
            self.assertFalse(path.with_name(".sources.json.lock").exists())

    def test_two_missing_file_snapshots_cannot_overwrite_first_creation(self):
        with TemporaryDirectory() as temp:
            path = Path(temp) / "sources.json"
            first, second = load_config(str(path)), load_config(str(path))
            self.assertIsNone(first.overlay_fingerprint)
            self.assertIsNone(second.overlay_fingerprint)
            expected = {source["id"]: source["enabled"] for source in first.sources}
            expected["skillsmp"] = False
            set_source_enabled(first, "skillsmp", False)
            with self.assertRaisesRegex(ConfigurationError, "changed since it was loaded"):
                set_source_enabled(second, "clawhub", False)
            self.assertEqual(source_choices(json.loads(path.read_text())), expected)

    def test_successful_writes_refresh_snapshot_for_subsequent_changes(self):
        with TemporaryDirectory() as temp:
            path = Path(temp) / "sources.json"
            config = load_config(str(path))
            expected = {source["id"]: source["enabled"] for source in config.sources}
            expected.update(skillsmp=False, clawhub=False)
            set_source_enabled(config, "skillsmp", False)
            first_fingerprint = config.overlay_fingerprint
            set_source_enabled(config, "clawhub", False)
            self.assertNotEqual(config.overlay_fingerprint, first_fingerprint)
            self.assertEqual(source_choices(json.loads(path.read_text())), expected)

    def test_external_changes_during_temp_write_are_checked_before_replace(self):
        with TemporaryDirectory() as temp:
            path = Path(temp) / "sources.json"
            config = load_config(str(path))
            external = {"source_overrides": {"polyskill": {"enabled": True}}}
            content = json.dumps(external).encode("utf-8")
            with patch("universal_skill_finder.config.os.fsync", side_effect=lambda _: path.write_bytes(content)):
                with self.assertRaisesRegex(ConfigurationError, "changed since it was loaded"):
                    set_source_enabled(config, "skillsmp", False)
            self.assertEqual(path.read_bytes(), content)
            self.assertEqual(config.overlay, empty_overlay())

    def test_active_lock_fails_without_removing_owners_lock_or_writing(self):
        with TemporaryDirectory() as temp:
            path = Path(temp) / "sources.json"
            config = load_config(str(path))
            lock = path.with_name(".sources.json.lock")
            with _overlay_lock(path):
                token = lock.read_bytes()
                with self.assertRaisesRegex(ConfigurationError, "configuration is busy"):
                    set_source_enabled(config, "skillsmp", False)
                self.assertEqual(lock.read_bytes(), token)
                self.assertFalse(path.exists())
                self.assertEqual(config.overlay, empty_overlay())
            self.assertFalse(lock.exists())

    def test_abandoned_lock_is_not_automatically_removed(self):
        with TemporaryDirectory() as temp:
            path = Path(temp) / "sources.json"
            config = load_config(str(path))
            lock = path.with_name(".sources.json.lock")
            lock.write_bytes(b"another owner")
            with self.assertRaisesRegex(ConfigurationError, "confirm no finder is writing"):
                set_source_enabled(config, "skillsmp", False)
            self.assertEqual(lock.read_bytes(), b"another owner")
            self.assertFalse(path.exists())

    def test_lock_cleanup_does_not_delete_a_replacement_owners_lock(self):
        with TemporaryDirectory() as temp:
            path = Path(temp) / "sources.json"
            lock = path.with_name(".sources.json.lock")
            with _overlay_lock(path):
                lock.unlink()
                lock.write_bytes(b"replacement owner")
            self.assertEqual(lock.read_bytes(), b"replacement owner")

    def test_replace_failure_preserves_disk_and_memory_and_releases_lock(self):
        with TemporaryDirectory() as temp:
            path = Path(temp) / "sources.json"
            save_overlay(path, empty_overlay())
            config = load_config(str(path))
            content, original = path.read_bytes(), deepcopy(config.overlay)
            with patch("universal_skill_finder.config.os.replace", side_effect=OSError("disk full")) as replace:
                with self.assertRaises(ConfigurationError):
                    set_source_enabled(config, "skillsmp", False)
            replace.assert_called_once()
            self.assertEqual(path.read_bytes(), content)
            self.assertEqual(config.overlay, original)
            self.assertFalse(path.with_name(".sources.json.lock").exists())
            self.assertEqual(list(Path(temp).glob(".sources-*")), [])

    def test_two_processes_from_one_snapshot_have_exactly_one_winner(self):
        with TemporaryDirectory() as temp:
            path = Path(temp) / "sources.json"
            context = multiprocessing.get_context("spawn")
            expected = {source["id"]: source["enabled"] for source in load_config(str(path)).sources}
            ready, results, start = context.Queue(), context.Queue(), context.Event()
            workers = [context.Process(target=_competing_writer, args=(
                str(path), source_id, ready, start, results,
            )) for source_id in ("skillsmp", "clawhub")]
            try:
                for worker in workers:
                    worker.start()
                self.assertEqual({ready.get(timeout=10), ready.get(timeout=10)}, {"skillsmp", "clawhub"})
                start.set()
                outcomes = [results.get(timeout=10), results.get(timeout=10)]
                for worker in workers:
                    worker.join(timeout=10)
                    self.assertEqual(worker.exitcode, 0)
                winners = [source_id for source_id, result in outcomes if result == "saved"]
                self.assertEqual(len(winners), 1, outcomes)
                self.assertTrue(any("busy" in result or "changed since" in result for _, result in outcomes))
                expected[winners[0]] = False
                self.assertEqual(source_choices(json.loads(path.read_text())), expected)
                self.assertFalse(path.with_name(".sources.json.lock").exists())
            finally:
                for worker in workers:
                    if worker.is_alive():
                        worker.terminate()
                        worker.join(timeout=10)
                ready.close()
                results.close()


if __name__ == "__main__":
    unittest.main()
