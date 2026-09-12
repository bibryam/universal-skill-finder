#!/usr/bin/env python3
"""Convenience entry point for a source checkout; the skill is self-contained."""
import sys

if sys.version_info < (3, 10):
    sys.stderr.write("Universal Skill Finder cannot start: Python 3.10 or later is required; this interpreter is too old. No searches were run.\n")
    raise SystemExit(4)

import os
import runpy

_skill_scripts = os.path.join(os.path.dirname(os.path.dirname(os.path.realpath(__file__))), "skills", "find", "scripts")
sys.path.insert(0, _skill_scripts)

if __name__ == "__main__":
    runpy.run_path(os.path.join(_skill_scripts, "skill_finder.py"), run_name="__main__")
