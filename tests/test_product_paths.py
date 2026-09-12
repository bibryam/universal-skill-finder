from __future__ import annotations

import io
import json
import os
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch


SCRIPTS = Path(__file__).resolve().parents[1] / "skills" / "find" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from universal_skill_finder.cache import Cache, default_cache_dir
from universal_skill_finder.cli import main as cli_main
from universal_skill_finder.config import ConfigurationError, load_config, user_config_path


class ProductPathTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.home = self.root / "home"
        environment = patch.dict(os.environ, {"HOME": str(self.home)}, clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        home_patch = patch.object(Path, "home", return_value=self.home)
        home_patch.start()
        self.addCleanup(home_patch.stop)
        self.config = self.home / ".config" / "universal-skill-finder" / "sources.json"
        self.cache = self.home / ".cache" / "universal-skill-finder"

    @staticmethod
    def _overlay(path: Path, *, enabled: bool = False) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "schema_version": 1,
            "sources": [{"id": "tessl", "enabled": enabled}],
        }), encoding="utf-8")

    def _symlink(self, path: Path, target: Path, *, directory: bool = False) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            path.symlink_to(target, target_is_directory=directory)
        except OSError:
            if os.name == "nt":
                self.skipTest("Windows symlink privilege unavailable")
            raise

    def test_default_paths_do_not_create_state(self):
        self.assertEqual(user_config_path(), self.config)
        self.assertEqual(default_cache_dir(), self.cache)
        self.assertEqual(Cache().root, self.cache)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_xdg_paths_are_honored_without_creating_state(self):
        config_base = self.root / "xdg-config"
        cache_base = self.root / "xdg-cache"
        os.environ.update(XDG_CONFIG_HOME=str(config_base), XDG_CACHE_HOME=str(cache_base))
        self.assertEqual(user_config_path(), config_base / "universal-skill-finder" / "sources.json")
        self.assertEqual(default_cache_dir(), cache_base / "universal-skill-finder")
        self.assertEqual(list(self.root.iterdir()), [])

    def test_explicit_paths_take_precedence(self):
        explicit_config = self.root / "explicit" / "sources.json"
        explicit_cache = self.root / "explicit-cache"
        os.environ.update(
            UNIVERSAL_SKILL_FINDER_CONFIG=str(self.root / "environment.json"),
            UNIVERSAL_SKILL_FINDER_CACHE=str(self.root / "environment-cache"),
        )
        self.assertEqual(user_config_path(str(explicit_config)), explicit_config)
        self.assertEqual(Cache(explicit_cache).root, explicit_cache)
        self.assertFalse(explicit_config.parent.exists())
        self.assertFalse(explicit_cache.exists())

    def test_environment_paths_take_precedence_even_when_missing(self):
        config = self.root / "environment.json"
        cache = self.root / "environment-cache"
        os.environ.update(
            UNIVERSAL_SKILL_FINDER_CONFIG=str(config),
            UNIVERSAL_SKILL_FINDER_CACHE=str(cache),
        )
        self.assertEqual(user_config_path(), config)
        self.assertEqual(default_cache_dir(), cache)
        self.assertFalse(config.exists())
        self.assertFalse(cache.exists())

    def test_existing_overlay_is_loaded_without_rewriting(self):
        self._overlay(self.config, enabled=False)
        before = self.config.read_bytes()
        loaded = load_config()
        self.assertEqual(loaded.overlay_path, self.config)
        self.assertFalse(loaded.source("tessl")["effective_enabled"])
        self.assertEqual(self.config.read_bytes(), before)

    def test_config_and_cache_symlinks_are_rejected(self):
        self._symlink(self.config, self.root / "missing.json")
        self._symlink(self.cache, self.root / "missing-cache", directory=True)
        with self.assertRaisesRegex(ConfigurationError, "symlink"):
            user_config_path()
        with self.assertRaisesRegex(ValueError, "symlink"):
            default_cache_dir()

    def test_symlinked_config_directory_is_rejected(self):
        self._symlink(self.config.parent, self.root / "missing-directory", directory=True)
        with self.assertRaisesRegex(ConfigurationError, "symlink"):
            user_config_path()

    def test_environment_paths_expand_user_without_accessing_real_home(self):
        os.environ.update(
            UNIVERSAL_SKILL_FINDER_CONFIG="~/custom/sources.json",
            UNIVERSAL_SKILL_FINDER_CACHE="~/custom-cache",
        )
        self.assertEqual(user_config_path(), self.home / "custom" / "sources.json")
        self.assertEqual(default_cache_dir(), self.home / "custom-cache")
        self.assertFalse(self.home.exists())

    def test_invalid_default_paths_fail_visibly(self):
        self.config.mkdir(parents=True)
        self.cache.parent.mkdir(parents=True, exist_ok=True)
        self.cache.write_text("not a directory", encoding="utf-8")
        with self.assertRaises(ConfigurationError):
            load_config()
        with self.assertRaisesRegex(ValueError, "directory"):
            default_cache_dir()

    def test_cli_reports_unsafe_config_without_traceback_or_side_effects(self):
        self._symlink(self.config, self.root / "missing.json")
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = cli_main(["sources", "list", "--json"])
        self.assertEqual(code, 3)
        self.assertIn("configuration error:", stderr.getvalue())
        self.assertIn("symlink", stderr.getvalue())
        self.assertNotIn("Traceback", stderr.getvalue())
        self.assertEqual(stdout.getvalue(), "")
        self.assertFalse(self.cache.exists())

    def test_cli_reports_unsafe_explicit_cache_without_traceback(self):
        link = self.root / "explicit-cache"
        self._symlink(link, self.root / "missing-cache", directory=True)
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = cli_main(["--cache-dir", str(link), "cache", "list", "--json"])
        self.assertEqual(code, 3)
        self.assertIn("symlink", stderr.getvalue())
        self.assertNotIn("Traceback", stderr.getvalue())
        self.assertEqual(stdout.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
