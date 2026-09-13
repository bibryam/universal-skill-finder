"""Release identity and explicitly versioned, dependency-free data contracts.

Revision hashes identify installed bytes/configuration, not Git commits or a
publisher signature. Credentials are never read from the environment here.
"""
from __future__ import annotations

import hashlib
import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

VERSION = "0.1.4"
# Source-list, overlay and source-pack JSON retain their v1 shapes. Search
# reports deliberately use their own v2 envelope below.
SCHEMA_VERSION = 1
SEARCH_REPORT_SCHEMA_VERSION = 2
REPORT_FORMAT_VERSION = 3
SNAPSHOT_SCHEMA_VERSION = 1
RANKING_ALGORITHM_VERSION = "discovery-70-20-10-v1"
ADAPTER_CONTRACT_VERSION = 2
CACHE_FORMAT_VERSION = 1

_MAX_REVISION_FILES = 512
_MAX_REVISION_FILE_BYTES = 4 * 1024 * 1024
_MAX_REVISION_BYTES = 32 * 1024 * 1024
_CREDENTIAL_FIELDS = frozenset({
    "authorization", "proxyauthorization", "cookie", "setcookie", "password",
    "passwd", "secret", "clientsecret", "token", "accesstoken", "refreshtoken",
    "apikey", "xapikey", "credential", "credentials",
})
_ENV_REFERENCE_FIELDS = frozenset({"env", "prefix", "optional"})


def _json_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode("utf-8")


def _digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_json_bytes(value)).hexdigest()


def _field_name(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.casefold())


def _revision_config(value: Any, *, field: str = "") -> Any:
    """Drop secret values, but retain names of configured environment references."""
    if _field_name(field) in _CREDENTIAL_FIELDS:
        if isinstance(value, dict) and isinstance(value.get("env"), str):
            return {key: value[key] for key in sorted(_ENV_REFERENCE_FIELDS & value.keys())}
        return "<credential omitted>"
    if isinstance(value, dict):
        return {key: _revision_config(item, field=key) for key, item in value.items() if key != "origin"}
    if isinstance(value, list):
        return [_revision_config(item) for item in value]
    if isinstance(value, str) and field in {"endpoint", "base_url", "provenance"}:
        # Validated configuration already rejects endpoint userinfo. Redaction
        # also protects diagnostics of programmatically constructed configs.
        parsed = urlsplit(value)
        if parsed.scheme in {"https", "http"}:
            query = parse_qsl(parsed.query, keep_blank_values=True)
            if parsed.username is not None or any(_field_name(key) in _CREDENTIAL_FIELDS for key, _ in query):
                host = parsed.netloc.rsplit("@", 1)[-1]
                query = [(key, "<credential omitted>" if _field_name(key) in _CREDENTIAL_FIELDS else item) for key, item in query]
                return urlunsplit((parsed.scheme, host, parsed.path, urlencode(query), parsed.fragment))
    return value


def effective_config_revision(settings: dict[str, Any], packs: list[dict[str, Any]], sources: list[dict[str, Any]]) -> str:
    """Hash effective source order/settings/state, not overlay location or secrets.

    Ordering is significant because the report preserves configured source
    order. Local source paths and environment variable *names* are significant.
    Credential contents and runtime environment values are not part of this ID.
    """
    return _digest(_revision_config({"settings": settings, "packs": packs, "sources": sources}))


def _read_revision_file(path: Path) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"revision input must be a regular, non-symlink file: {path.name}")
    with path.open("rb") as stream:
        raw = stream.read(_MAX_REVISION_FILE_BYTES + 1)
    if len(raw) > _MAX_REVISION_FILE_BYTES:
        raise ValueError(f"revision input exceeds size limit: {path.name}")
    return raw


def _release_metadata(package_root: Path) -> dict[str, Any]:
    files: list[tuple[str, Path]] = []
    for path in package_root.rglob("*.py"):
        relative = path.relative_to(package_root)
        if "__pycache__" in relative.parts:
            continue
        # Refuse symlinked intermediate directories as well as leaf files.
        if any(parent.is_symlink() for parent in path.parents if parent != package_root and package_root in parent.parents):
            raise ValueError("revision input includes a symlinked package directory")
        files.append(("engine/" + relative.as_posix(), path))
        if len(files) > _MAX_REVISION_FILES:
            raise ValueError("installed engine exceeds revision file limit")
    if not files:
        raise ValueError("installed engine contains no Python files")
    skill_root = package_root.parent.parent
    scope = "engine"
    if (skill_root / "SKILL.md").is_file():
        scope = "skill"
        for relative in ("SKILL.md", "agents/openai.yaml", "scripts/skill_finder.py", "scripts/run.sh", "scripts/run.ps1"):
            path = skill_root / relative
            if path.exists():
                files.append(("skill/" + relative, path))
        for directory, suffix in (("references", "*.md"), ("config/source-packs", "*.json")):
            for path in (skill_root / directory).glob(suffix):
                files.append(("skill/" + path.relative_to(skill_root).as_posix(), path))
    if len(files) > _MAX_REVISION_FILES:
        raise ValueError("installed skill exceeds revision file limit")
    revision = hashlib.sha256()
    revision.update(b"universal-skill-finder-installed-payload-v1\0")
    total = 0
    for relative, path in sorted(files):
        raw = _read_revision_file(path)
        total += len(raw)
        if total > _MAX_REVISION_BYTES:
            raise ValueError("installed skill exceeds revision byte limit")
        # Length-delimited records avoid ambiguous file-name/content boundaries.
        name = relative.encode("utf-8")
        revision.update(len(name).to_bytes(4, "big") + name)
        revision.update(len(raw).to_bytes(8, "big") + raw)
    catalogue = json.loads(_read_revision_file(package_root / "data" / "sources.default.json"))
    return {
        "release_version": VERSION,
        "code_revision": "sha256:" + revision.hexdigest(),
        "catalogue_revision": _digest(catalogue),
        "revision_scope": scope,
        "schema_version": SCHEMA_VERSION,
        "search_report_schema_version": SEARCH_REPORT_SCHEMA_VERSION,
        "report_format_version": REPORT_FORMAT_VERSION,
        "adapter_contract_version": ADAPTER_CONTRACT_VERSION,
        "cache_format_version": CACHE_FORMAT_VERSION,
    }


@lru_cache(maxsize=1)
def _installed_metadata() -> dict[str, Any]:
    return _release_metadata(Path(__file__).resolve().parent)


def release_metadata(package_root: Path | None = None) -> dict[str, Any]:
    """Return release identity, with no filesystem paths or volatile timestamps.

    The normal installed payload is read only once per process. Release tools
    can supply an explicit universal_skill_finder package directory for an uncached
    check of a copied payload. Wheels without adjacent SKILL.md use engine scope.
    """
    return dict(_installed_metadata() if package_root is None else _release_metadata(package_root.resolve()))
