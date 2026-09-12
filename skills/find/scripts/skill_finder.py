#!/usr/bin/env python3
"""Bootstrap deliberately compatible with old Python so prerequisites fail cleanly."""
import sys


def _prerequisite_error(message):
    sys.stderr.write("Universal Skill Finder cannot start: " + message + " No searches were run.\n")
    raise SystemExit(4)


if sys.version_info < (3, 10):
    _prerequisite_error("Python 3.10 or later is required; this interpreter is too old.")

# Coding assistants capture pipes, which can default to a legacy encoding on
# Windows. Keep Markdown status markers and skill names intact without changing
# the user's environment. Embedded hosts may supply non-reconfigurable streams.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="backslashreplace")
    except (AttributeError, OSError, ValueError, TypeError):
        pass

try:
    import ssl

    ssl.create_default_context()
except Exception:
    _prerequisite_error("This Python runtime does not have working SSL support. Make a supported Python runtime available and retry.")

if len(sys.argv) == 3 and sys.argv[1] == "--launcher-args":
    try:
        import base64
        import json

        _forwarded_arguments = json.loads(base64.b64decode(sys.argv[2], validate=True).decode("utf-8"))
        if not isinstance(_forwarded_arguments, list) or any(not isinstance(argument, str) for argument in _forwarded_arguments):
            raise ValueError("Expected a list of strings")
        sys.argv[1:] = _forwarded_arguments
    except (ImportError, ValueError, TypeError, UnicodeError, RecursionError):
        _prerequisite_error("The plugin launcher supplied invalid arguments. Reinstall the plugin.")

if sys.argv[1:] == ["--check"]:
    sys.stdout.write("Prerequisites OK: Python %s.%s with SSL support.\n" % sys.version_info[:2])
    raise SystemExit(0)

try:
    from universal_skill_finder.cli import main
except (ImportError, SyntaxError):
    _prerequisite_error("The bundled engine could not load. Check the Python runtime or reinstall the plugin.")


if __name__ == "__main__":
    raise SystemExit(main())
