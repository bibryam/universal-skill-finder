#!/bin/sh
# Internal plugin launcher. Probe runtimes before the engine can read configuration.
case "$0" in
    */*) finder_script_dir=${0%/*} ;;
    *) finder_script_dir=. ;;
esac
case "$finder_script_dir" in
    /*) ;;
    *) finder_script_dir=./$finder_script_dir ;;
esac
finder_script_dir=$(CDPATH= cd -P "$finder_script_dir" 2>/dev/null && pwd) || {
    printf '%s\n' 'Universal Skill Finder cannot start: the plugin directory is unavailable. Reinstall the plugin. No searches were run.' >&2
    exit 4
}
finder_entry_point=$finder_script_dir/skill_finder.py
if [ ! -r "$finder_entry_point" ]; then
    printf '%s\n' 'Universal Skill Finder cannot start: the plugin is incomplete. Reinstall the plugin. No searches were run.' >&2
    exit 4
fi

for finder_python in python3 python3.14 python3.13 python3.12 python3.11 python3.10 python; do
    if command -v "$finder_python" >/dev/null 2>&1 &&
        "$finder_python" "$finder_entry_point" --check >/dev/null 2>&1; then
        exec "$finder_python" "$finder_entry_point" "$@"
    fi
done

printf '%s\n' 'Universal Skill Finder cannot start: Python 3.10 or later with SSL support is required, but no compatible interpreter was found on PATH. Make a supported Python runtime available, restart the coding assistant, and retry. No searches were run.' >&2
exit 4
