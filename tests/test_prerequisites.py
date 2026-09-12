from __future__ import annotations

import base64
import json
import os
import shlex
import shutil
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "find" / "scripts"
BOOTSTRAP = SCRIPTS / "skill_finder.py"
SH = shutil.which("sh")
POWERSHELL = shutil.which("pwsh") or shutil.which("powershell")


def isolated_environment(root: Path, path: str) -> dict[str, str]:
    return {**os.environ, "PATH": path, "PYTHONDONTWRITEBYTECODE": "1",
            "XDG_CONFIG_HOME": str(root / "config"), "XDG_CACHE_HOME": str(root / "cache")}


class BootstrapPrerequisiteTests(unittest.TestCase):
    def run_bootstrap(self, path=BOOTSTRAP, setup="", arguments=("--check",), environment=None):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            code = "import runpy, sys; " + setup + "sys.argv = " + repr([str(path), *arguments]) + "; runpy.run_path(" + repr(str(path)) + ", run_name='__main__')"
            result = subprocess.run([sys.executable, "-c", code], text=True, encoding="utf-8", capture_output=True,
                                    env={**isolated_environment(root, os.environ.get("PATH", "")), **(environment or {})})
            self.assertFalse((root / "config").exists())
            self.assertFalse((root / "cache").exists())
            return result

    def test_supported_runtime_preflight_does_not_load_engine(self):
        result = self.run_bootstrap(setup="sys.modules['universal_skill_finder'] = None; ")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Prerequisites OK", result.stdout)

    def test_old_python_fails_before_loading_engine_for_both_entry_points(self):
        for path in (BOOTSTRAP, ROOT / "scripts" / "skill_finder.py"):
            with self.subTest(path=path):
                result = self.run_bootstrap(path=path, setup="sys.version_info = (3, 9, 99); sys.modules['universal_skill_finder'] = None; ", arguments=("search", "PDF forms"))
                self.assertEqual(result.returncode, 4)
                self.assertIn("Python 3.10 or later", result.stderr)
                self.assertIn("No searches were run", result.stderr)
                self.assertNotIn("Traceback", result.stderr)

    def test_missing_ssl_fails_without_traceback_or_engine_import(self):
        result = self.run_bootstrap(setup="sys.modules['ssl'] = None; sys.modules['universal_skill_finder'] = None; ")
        self.assertEqual(result.returncode, 4)
        self.assertIn("SSL support", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_broken_ssl_context_fails_without_traceback(self):
        result = self.run_bootstrap(setup="import ssl; ssl.create_default_context = lambda: 1 / 0; ")
        self.assertEqual(result.returncode, 4)
        self.assertIn("SSL support", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_missing_engine_dependency_fails_without_traceback(self):
        result = self.run_bootstrap(setup="sys.modules['universal_skill_finder'] = None; ", arguments=("search", "PDF forms"))
        self.assertEqual(result.returncode, 4)
        self.assertIn("bundled engine could not load", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_checkout_bootstrap_preflight(self):
        result = self.run_bootstrap(path=ROOT / "scripts" / "skill_finder.py")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Prerequisites OK", result.stdout)

    def test_encoded_launcher_arguments_preserve_quotes_spaces_and_empty_values(self):
        arguments = ["search", 'PDF forms with "quoted text" & more', "", "quote ' and \\path\\", "日本語"]
        encoded = base64.b64encode(json.dumps(arguments).encode("utf-8")).decode("ascii")
        setup = "import types, json; engine = types.ModuleType('universal_skill_finder.cli'); engine.main = lambda: (print(json.dumps(sys.argv[1:])), 0)[1]; sys.modules['universal_skill_finder.cli'] = engine; "
        result = self.run_bootstrap(setup=setup, arguments=("--launcher-args", encoded))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), arguments)

    def test_encoded_preflight_does_not_import_engine(self):
        encoded = base64.b64encode(b'["--check"]').decode("ascii")
        result = self.run_bootstrap(setup="sys.modules['universal_skill_finder'] = None; ", arguments=("--launcher-args", encoded))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Prerequisites OK", result.stdout)

    def test_invalid_encoded_arguments_fail_gracefully_before_engine_import(self):
        for payload in ("not base64!", base64.b64encode(b'{}').decode("ascii"), base64.b64encode(b'[1]').decode("ascii")):
            with self.subTest(payload=payload):
                result = self.run_bootstrap(setup="sys.modules['universal_skill_finder'] = None; ", arguments=("--launcher-args", payload))
                self.assertEqual(result.returncode, 4, result.stderr)
                self.assertIn("invalid arguments", result.stderr)
                self.assertNotIn("Traceback", result.stderr)

    def test_both_entry_points_emit_utf8_when_pipes_default_to_ascii(self):
        markers = "\u2705 \U0001f7e5 日本語"
        setup = "import types; engine = types.ModuleType('universal_skill_finder.cli'); engine.main = lambda: (print(" + repr(markers) + "), print(" + repr(markers) + ", file=sys.stderr), 0)[2]; sys.modules['universal_skill_finder.cli'] = engine; "
        for path in (BOOTSTRAP, ROOT / "scripts" / "skill_finder.py"):
            with self.subTest(path=path):
                result = self.run_bootstrap(path=path, setup=setup, arguments=("search", "PDF forms", "--markdown"),
                                            environment={"PYTHONIOENCODING": "ascii"})
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.strip(), markers)
                self.assertEqual(result.stderr.strip(), markers)

    def test_preflight_accepts_custom_streams_without_reconfigure(self):
        result = self.run_bootstrap(setup="import io; sys.stdout = io.StringIO(); ")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_preflight_accepts_custom_streams_that_reject_reconfigure(self):
        setup = "import types; stream = types.SimpleNamespace(write=sys.stdout.write, flush=sys.stdout.flush, reconfigure=lambda **kwargs: (_ for _ in ()).throw(OSError('unavailable'))); sys.stdout = stream; "
        result = self.run_bootstrap(setup=setup)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Prerequisites OK", result.stdout)


@unittest.skipUnless(SH and os.name != "nt", "POSIX shell is required")
class PosixPrerequisiteTests(unittest.TestCase):
    def run_launcher(self, root: Path, arguments=("--check",), path=None, launcher=None):
        result = subprocess.run([SH, str(launcher or SCRIPTS / "run.sh"), *arguments], text=True, capture_output=True,
                                env=isolated_environment(root, str(root) if path is None else path))
        self.assertFalse((root / "config").exists())
        self.assertFalse((root / "cache").exists())
        return result

    def fake_python(self, root: Path, name: str, *, check_status=0, run_status=0):
        fixture = root / (name + "-fixture.py")
        fixture.write_text("import json, sys\nif sys.argv[2:] == ['--check']:\n    raise SystemExit(" + str(check_status) + ")\nprint(json.dumps(sys.argv[2:]))\nraise SystemExit(" + str(run_status) + ")\n", encoding="utf-8")
        executable = root / name
        executable.write_text("#!/bin/sh\nexec " + shlex.quote(sys.executable) + " " + shlex.quote(str(fixture)) + ' "$@"\n', encoding="utf-8")
        executable.chmod(0o755)

    def test_missing_python_fails_gracefully(self):
        with TemporaryDirectory() as temp:
            result = self.run_launcher(Path(temp))
        self.assertEqual(result.returncode, 4)
        self.assertIn("Python 3.10 or later", result.stderr)
        self.assertIn("No searches were run", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_unsupported_python_fails_gracefully(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            self.fake_python(root, "python3", check_status=4)
            result = self.run_launcher(root)
        self.assertEqual(result.returncode, 4)
        self.assertIn("no compatible interpreter", result.stderr)

    def test_broken_interpreter_fails_gracefully(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "python3").write_text("#!/unavailable/interpreter\n", encoding="utf-8")
            (root / "python3").chmod(0o755)
            result = self.run_launcher(root)
        self.assertEqual(result.returncode, 4)
        self.assertIn("no compatible interpreter", result.stderr)
        self.assertNotIn("not found", result.stderr)

    def test_supported_fallback_preserves_arguments_and_exit_status(self):
        with TemporaryDirectory(prefix="finder prerequisite ") as temp:
            root = Path(temp)
            self.fake_python(root, "python3", check_status=4)
            self.fake_python(root, "python3.12", run_status=2)
            arguments = ["--config", str(root / "config with spaces.json"), "search", "PDF forms & quotes 'here'", "--json"]
            result = self.run_launcher(root, arguments)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(json.loads(result.stdout), arguments)

    def test_real_python_preflight_and_nonexecutable_launcher_in_spaced_directory(self):
        with TemporaryDirectory(prefix="finder prerequisite ") as temp:
            root = Path(temp)
            copied = root / "plugin scripts"
            copied.mkdir()
            shutil.copyfile(SCRIPTS / "run.sh", copied / "run.sh")
            shutil.copyfile(BOOTSTRAP, copied / "skill_finder.py")
            (root / "python3").symlink_to(sys.executable)
            result = self.run_launcher(root, launcher=copied / "run.sh")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Prerequisites OK", result.stdout)

    def test_incomplete_plugin_fails_gracefully(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            shutil.copyfile(SCRIPTS / "run.sh", root / "run.sh")
            result = self.run_launcher(root, launcher=root / "run.sh")
        self.assertEqual(result.returncode, 4)
        self.assertIn("plugin is incomplete", result.stderr)


@unittest.skipUnless(POWERSHELL, "PowerShell is not installed")
class PowerShellPrerequisiteTests(unittest.TestCase):
    def assert_product_state_absent(self, root: Path) -> None:
        # PowerShell may initialize its own files under the XDG base directories.
        # The launcher contract is that the finder itself creates no state during
        # preflight, so assert only against the product-owned subdirectories.
        self.assertFalse((root / "config" / "universal-skill-finder").exists())
        self.assertFalse((root / "cache" / "universal-skill-finder").exists())

    def test_missing_python_fails_gracefully(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            result = subprocess.run([POWERSHELL, "-NoProfile", "-File", str(SCRIPTS / "run.ps1"), "--check"],
                                    text=True, capture_output=True, env=isolated_environment(root, str(root)))
            self.assert_product_state_absent(root)
        self.assertEqual(result.returncode, 4, result.stdout + result.stderr)
        self.assertIn("no compatible interpreter", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_supported_python_preflight(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            path = str(Path(sys.executable).parent) + os.pathsep + os.environ.get("PATH", "")
            result = subprocess.run([POWERSHELL, "-NoProfile", "-File", str(SCRIPTS / "run.ps1"), "--check"],
                                    text=True, capture_output=True, env=isolated_environment(root, path))
            self.assert_product_state_absent(root)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Prerequisites OK", result.stdout)

    def test_arguments_and_engine_status_are_preserved(self):
        with TemporaryDirectory(prefix="finder prerequisite ") as temp:
            root = Path(temp)
            copied = root / "plugin scripts"
            copied.mkdir()
            shutil.copyfile(SCRIPTS / "run.ps1", copied / "run.ps1")
            shutil.copyfile(BOOTSTRAP, copied / "skill_finder.py")
            package = copied / "universal_skill_finder"
            package.mkdir()
            (package / "__init__.py").write_text("", encoding="utf-8")
            (package / "cli.py").write_text("import json, sys\ndef main():\n    print(json.dumps(sys.argv[1:]))\n    return 2\n", encoding="utf-8")
            arguments = ["--config", str(root / "config with spaces.json"), "search", 'PDF forms with "quotes" & more', "", "quote ' and \\path\\", "--json"]
            literals = ", ".join("'" + item.replace("'", "''") + "'" for item in arguments)
            # A literal @(...), unlike a named @array, is one nested argument to
            # a PowerShell script. Use real array splatting so this exercises the
            # launcher's native-process transport rather than an invalid fixture.
            command = "$finderFixtureArguments = @(" + literals + "); & '" + str(copied / "run.ps1").replace("'", "''") + "' @finderFixtureArguments; exit $LASTEXITCODE"
            path = str(Path(sys.executable).parent) + os.pathsep + os.environ.get("PATH", "")
            result = subprocess.run([POWERSHELL, "-NoProfile", "-Command", command], text=True, capture_output=True,
                                    env=isolated_environment(root, path))
            self.assert_product_state_absent(root)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertEqual(json.loads(result.stdout), arguments)

    @unittest.skipUnless(os.name == "nt", "Windows command fixtures are required")
    def test_old_windows_launcher_and_python3_fall_back_to_supported_python(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            for name in ("py.cmd", "python3.cmd"):
                (root / name).write_text("@echo off\nexit /b 4\n", encoding="ascii")
            path = str(root) + os.pathsep + str(Path(sys.executable).parent)
            result = subprocess.run([POWERSHELL, "-NoProfile", "-File", str(SCRIPTS / "run.ps1"), "--check"],
                                    text=True, capture_output=True, env=isolated_environment(root, path))
            self.assert_product_state_absent(root)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Prerequisites OK", result.stdout)


if __name__ == "__main__":
    unittest.main()
