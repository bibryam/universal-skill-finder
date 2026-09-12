from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import stat
import tempfile
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .adapters import ADAPTER_SPECS
from .text import SOURCE_ID_RE, parse_github_repository, safe_install_reference, slugify
from .versioning import SCHEMA_VERSION

# Compatibility export for callers; registration is defined only in adapters.
SUPPORTED_ADAPTERS = frozenset(ADAPTER_SPECS)
ENV_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{1,127}$")
HEADER_NAME_RE = re.compile(r"^[A-Za-z0-9!#$%&'*+.^_`|~-]+$")
MAX_CONFIG_BYTES = 2 * 1024 * 1024
_CREDENTIAL_HEADERS = {"authorization", "proxy-authorization", "x-api-key", "api-key", "cookie"}
_CURRENT_SNAPSHOT = object()


def _valid_id(value: object) -> bool:
    return isinstance(value, str) and SOURCE_ID_RE.fullmatch(value) is not None


def _control_characters(value: str) -> bool:
    return any(ord(char) < 32 or ord(char) == 127 for char in value)


class ConfigurationError(ValueError):
    pass


def default_config_path() -> Path:
    return Path(__file__).resolve().parent / "data" / "sources.default.json"


def user_config_path(explicit: str | None = None) -> Path:
    if explicit:
        return _checked_user_config_path(Path(explicit).expanduser())
    override = os.environ.get("UNIVERSAL_SKILL_FINDER_CONFIG")
    if override:
        return _checked_user_config_path(Path(override).expanduser())
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".config"
    return _checked_user_config_path(base / "universal-skill-finder" / "sources.json")


def _checked_user_config_path(path: Path) -> Path:
    # Resolve the parent only after checking the product-specific directory;
    # ordinary platform aliases such as macOS /tmp remain supported.
    if path.is_symlink() or path.parent.is_symlink():
        raise ConfigurationError(f"configuration path must not be a symlink: {path}")
    try:
        return path.parent.resolve() / path.name
    except (OSError, RuntimeError) as exc:
        raise ConfigurationError(f"cannot resolve configuration path: {path}") from exc


def empty_overlay() -> dict[str, Any]:
    return {"sources": []}


def _read_json_snapshot(path: Path, *, required: bool) -> tuple[dict[str, Any], str | None]:
    try:
        # Nonblocking open and fstat avoid hanging on a FIFO supplied as a pack.
        if path.is_symlink():
            raise ConfigurationError(f"configuration path must not be a symlink: {path}")
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(descriptor, "rb") as stream:
            file_stat = os.fstat(stream.fileno())
            if not stat.S_ISREG(file_stat.st_mode):
                raise ConfigurationError(f"configuration must be a regular file: {path}")
            if file_stat.st_size > MAX_CONFIG_BYTES:
                raise ConfigurationError(f"configuration exceeds {MAX_CONFIG_BYTES} bytes: {path}")
            raw = stream.read(MAX_CONFIG_BYTES + 1)
        if len(raw) > MAX_CONFIG_BYTES:
            raise ConfigurationError(f"configuration exceeds {MAX_CONFIG_BYTES} bytes: {path}")
        payload = json.loads(raw.decode("utf-8"))
        pending = [(payload, 0)]
        while pending:
            value, depth = pending.pop()
            if depth > 100:
                raise ValueError("configuration nesting exceeds 100 levels")
            children = value.values() if isinstance(value, dict) else value if isinstance(value, list) else ()
            pending.extend((child, depth + 1) for child in children if isinstance(child, (dict, list)))
    except FileNotFoundError:
        if required:
            raise ConfigurationError(f"configuration not found: {path}")
        return empty_overlay(), None
    except (json.JSONDecodeError, UnicodeError, RecursionError, ValueError) as exc:
        if isinstance(exc, ConfigurationError):
            raise
        raise ConfigurationError(f"invalid JSON in {path}") from exc
    except OSError as exc:
        raise ConfigurationError(f"cannot read configuration {path}: {exc.strerror}") from exc
    if not isinstance(payload, dict):
        raise ConfigurationError(f"configuration root must be an object: {path}")
    return payload, hashlib.sha256(raw).hexdigest()


def _read_json(path: Path, *, required: bool) -> dict[str, Any]:
    return _read_json_snapshot(path, required=required)[0]


@dataclass
class EffectiveConfig:
    settings: dict[str, Any]
    packs: list[dict[str, Any]]
    sources: list[dict[str, Any]]
    overlay_path: Path
    overlay: dict[str, Any]
    overlay_fingerprint: str | None = None

    def source(self, source_id: str) -> dict[str, Any] | None:
        return next((item for item in self.sources if item["id"] == source_id), None)

    def pack(self, pack_id: str) -> dict[str, Any] | None:
        return next((item for item in self.packs if item["id"] == pack_id), None)


def load_config(explicit_overlay: str | None = None) -> EffectiveConfig:
    base = _read_json(default_config_path(), required=True)
    if type(base.get("schema_version")) is not int or base.get("schema_version") != SCHEMA_VERSION:
        raise ConfigurationError(f"bundled catalogue must declare schema_version {SCHEMA_VERSION}")
    if not isinstance(base.get("settings"), dict):
        raise ConfigurationError("bundled catalogue settings must be an object")
    for field in ("packs", "sources"):
        entries = base.get(field)
        if not isinstance(entries, list) or any(not isinstance(item, dict) for item in entries):
            raise ConfigurationError(f"bundled catalogue {field} must be an array of objects")
        if any(not _valid_id(item.get("id")) for item in entries):
            raise ConfigurationError(f"every bundled {field} entry needs a valid string id")
    for source in base["sources"]:
        if source.get("pack") is not None and not _valid_id(source["pack"]):
            raise ConfigurationError("bundled source pack must be a valid string id")
    overlay_path = user_config_path(explicit_overlay)
    overlay, overlay_fingerprint = _read_json_snapshot(overlay_path, required=False)
    validate_overlay(overlay)
    packs = deepcopy(base.get("packs", []))
    sources = deepcopy(base.get("sources", []))

    seen_packs = {pack["id"] for pack in packs}
    for custom_pack in overlay.get("custom_packs", []):
        custom_pack_id = str(custom_pack.get("id", ""))
        if custom_pack_id in seen_packs:
            raise ConfigurationError(f"custom pack ID collides with bundled pack: {custom_pack_id}")
        packs.append(deepcopy(custom_pack))
        seen_packs.add(custom_pack_id)

    pack_overrides = overlay.get("pack_overrides", {})
    custom_pack_ids = {str(item.get("id", "")) for item in overlay.get("custom_packs", [])}
    for pack in packs:
        override = pack_overrides.get(pack["id"], {})
        if "enabled" in override:
            pack["enabled"] = override["enabled"]
        pack["origin"] = "user" if pack["id"] in custom_pack_ids else "bundled"

    seen = {source["id"] for source in sources}
    for custom in overlay.get("custom_sources", []):
        custom_id = str(custom.get("id", ""))
        if custom_id in seen:
            raise ConfigurationError(f"custom source ID collides with bundled source: {custom_id}")
        sources.append(deepcopy(custom))
        seen.add(custom_id)

    source_overrides = overlay.get("source_overrides", {})
    for field in ("sources", "repositories"):
        if field in overlay:
            source_overrides = {entry["id"]: {"enabled": entry["enabled"]} for entry in overlay[field]}
    unknown_pack_overrides = set(pack_overrides) - {str(pack.get("id", "")) for pack in packs}
    unknown_source_overrides = set(source_overrides) - {str(source.get("id", "")) for source in sources}
    if unknown_pack_overrides:
        raise ConfigurationError("unknown pack override(s): " + ", ".join(sorted(unknown_pack_overrides)))
    if unknown_source_overrides:
        raise ConfigurationError("unknown source id(s): " + ", ".join(sorted(unknown_source_overrides)))
    pack_states = {pack["id"]: pack.get("enabled", True) for pack in packs}
    custom_source_ids = {str(item.get("id", "")) for item in overlay.get("custom_sources", [])}
    for source in sources:
        override = source_overrides.get(source["id"], {})
        if "enabled" in override:
            source["enabled"] = override["enabled"]
        source["origin"] = "user" if source["id"] in custom_source_ids else "bundled"
        source["pack_enabled"] = pack_states.get(source.get("pack"), True)
        source["effective_enabled"] = source.get("enabled", True) is True and source["pack_enabled"] is True

    effective = EffectiveConfig(
        settings=deepcopy(base.get("settings", {})),
        packs=packs,
        sources=sources,
        overlay_path=overlay_path,
        overlay=overlay,
        overlay_fingerprint=overlay_fingerprint,
    )
    errors = validate_effective(effective)
    if errors:
        raise ConfigurationError("; ".join(errors))
    return effective


def validate_overlay(overlay: dict[str, Any]) -> None:
    if not isinstance(overlay, dict):
        raise ConfigurationError("overlay must be an object")
    if type(overlay.get("schema_version", SCHEMA_VERSION)) is not int or overlay.get("schema_version", SCHEMA_VERSION) != SCHEMA_VERSION:
        raise ConfigurationError("unsupported overlay schema_version")
    unknown = set(overlay) - {"schema_version", "sources", "repositories", "pack_overrides", "source_overrides", "custom_packs", "custom_sources"}
    if unknown:
        raise ConfigurationError("unknown overlay field(s): " + ", ".join(sorted(unknown)))
    preference_forms = set(overlay) & {"sources", "repositories", "source_overrides"}
    if len(preference_forms) > 1:
        raise ConfigurationError("use only one preference form: sources, legacy repositories, or legacy source_overrides")
    for field in ("sources", "repositories"):
        if field not in overlay:
            continue
        entries = overlay[field]
        if not isinstance(entries, list):
            raise ConfigurationError(f"{field} must be an array")
        seen: set[str] = set()
        for entry in entries:
            if not isinstance(entry, dict) or set(entry) != {"id", "enabled"}:
                raise ConfigurationError(f"every {field} entry must contain only id and enabled")
            source_id = entry["id"]
            if not _valid_id(source_id):
                raise ConfigurationError(f"every {field} entry needs a valid string id")
            if source_id in seen:
                raise ConfigurationError(f"duplicate source id: {source_id}")
            if type(entry["enabled"]) is not bool:
                raise ConfigurationError(f"{field}.{source_id}.enabled must be boolean")
            seen.add(source_id)
    for key in ("pack_overrides", "source_overrides"):
        overrides = overlay.get(key, {})
        if not isinstance(overrides, dict):
            raise ConfigurationError(f"{key} must be an object")
        for override_id, override in overrides.items():
            if not _valid_id(override_id):
                raise ConfigurationError(f"invalid override id in {key}")
            if not isinstance(override, dict):
                raise ConfigurationError(f"{key}.{override_id} must be an object")
            unknown = set(override) - {"enabled"}
            if unknown:
                raise ConfigurationError(f"unknown {key}.{override_id} field(s): " + ", ".join(sorted(unknown)))
            if "enabled" in override and not isinstance(override["enabled"], bool):
                raise ConfigurationError(f"{key}.{override_id}.enabled must be boolean")
    for key in ("custom_packs", "custom_sources"):
        if not isinstance(overlay.get(key, []), list):
            raise ConfigurationError(f"{key} must be an array")
        if any(not isinstance(item, dict) for item in overlay.get(key, [])):
            raise ConfigurationError(f"every {key} entry must be an object")
        for item in overlay.get(key, []):
            if not _valid_id(item.get("id")):
                raise ConfigurationError(f"every {key} entry needs a valid string id")
            if key == "custom_sources":
                if not isinstance(item.get("adapter"), str):
                    raise ConfigurationError("every custom source needs a string adapter")
                if item.get("pack") is not None and not _valid_id(item["pack"]):
                    raise ConfigurationError("custom source pack must be a valid string id")


def validate_effective(config: EffectiveConfig) -> list[str]:
    errors: list[str] = []
    pack_ids: set[str] = set()
    for pack in config.packs:
        pack_id = pack.get("id", "")
        if not _valid_id(pack_id):
            errors.append(f"invalid pack id: {pack_id!r}")
            continue
        if pack_id in pack_ids:
            errors.append(f"duplicate pack id: {pack_id}")
        if not isinstance(pack.get("enabled", True), bool):
            errors.append(f"enabled for pack {pack_id} must be boolean")
        pack_ids.add(pack_id)

    source_ids: set[str] = set()
    for source in config.sources:
        source_id = source.get("id", "")
        if not _valid_id(source_id):
            errors.append(f"invalid source id: {source_id!r}")
            continue
        if source_id in source_ids:
            errors.append(f"duplicate source id: {source_id}")
        source_ids.add(source_id)
        if not isinstance(source.get("enabled", True), bool):
            errors.append(f"enabled for source {source_id} must be boolean")
        adapter = source.get("adapter")
        kind = source.get("kind")
        if not isinstance(adapter, str) or adapter not in SUPPORTED_ADAPTERS:
            errors.append(f"unsupported adapter for {source_id}: {adapter}")
            continue
        spec = ADAPTER_SPECS[adapter]
        if not isinstance(kind, str) or kind not in {"registry", "repository"}:
            errors.append(f"invalid kind for {source_id}: {kind}")
        if kind != spec.kind:
            errors.append(f"{spec.kind} adapter {adapter} requires kind={spec.kind} for {source_id}")
        for field in spec.required_fields:
            if not source.get(field):
                errors.append(f"adapter {adapter} requires {field} for {source_id}")
        if source.get("pack") is not None and (not _valid_id(source["pack"]) or source["pack"] not in pack_ids):
            errors.append(f"unknown pack for {source_id}: {source['pack']}")
        auth_env = source.get("auth_env")
        if auth_env is not None and (not isinstance(auth_env, str) or not ENV_NAME_RE.fullmatch(auth_env)):
            errors.append(f"invalid auth_env for {source_id}")
        identity_namespace = source.get("identity_namespace")
        if identity_namespace is not None and not _valid_id(identity_namespace):
            errors.append(f"invalid identity_namespace for {source_id}")
        if adapter == "github-repo":
            repository = source.get("repository")
            if not isinstance(repository, str) or parse_github_repository(repository) != repository:
                errors.append(f"GitHub repository for {source_id} must be canonical owner/repository")
        if adapter == "github-repo" and not safe_install_reference(source.get("ref")):
            errors.append(f"missing or invalid ref for {source_id}")
        if adapter == "github-code-search":
            if source.get("base_url") != "https://api.github.com":
                errors.append(f"github-code-search source {source_id} requires base_url https://api.github.com")
            if "endpoint" in source or "headers" in source:
                errors.append(f"github-code-search source {source_id} does not allow endpoint or header overrides")
            if source.get("public_only", True) is not True:
                errors.append(f"github-code-search source {source_id} requires public_only=true")
            if source.get("auth_optional", False) is not False or source.get("allow_insecure_local", False) is not False:
                errors.append(f"github-code-search source {source_id} requires authentication and HTTPS")
        if adapter == "tessl":
            if source.get("base_url") != "https://api.tessl.io":
                errors.append(f"tessl source {source_id} requires base_url https://api.tessl.io")
            if any(field in source for field in ("endpoint", "headers", "auth_env", "auth_optional")):
                errors.append(f"tessl source {source_id} uses anonymous search and does not allow endpoint or credential overrides")
            if source.get("allow_insecure_local", False) is not False:
                errors.append(f"tessl source {source_id} requires HTTPS")
        if adapter == "local-directory":
            if not isinstance(source.get("path"), str) or not source["path"].strip():
                errors.append(f"missing path for {source_id}")
        for field in ("include", "exclude"):
            patterns = source.get(field, [])
            if not isinstance(patterns, list) or any(not isinstance(pattern, str) or not pattern for pattern in patterns):
                errors.append(f"{field} for {source_id} must be an array of non-empty strings")
        for boolean_field in ("auth_optional", "allow_insecure_local", "native_only", "non_suspicious_only"):
            if boolean_field in source and not isinstance(source[boolean_field], bool):
                errors.append(f"{boolean_field} for {source_id} must be boolean")
        if spec.kind == "registry":
            if not (source.get("endpoint") or source.get("base_url")):
                errors.append(f"missing endpoint or base_url for {source_id}")
            for field in ("endpoint", "base_url"):
                if field not in source:
                    continue
                url = source[field]
                if not isinstance(url, str) or not url or _control_characters(url) or any(char.isspace() for char in url):
                    errors.append(f"{field} for {source_id} must be a non-empty URL without whitespace")
                    continue
                try:
                    parsed = urlparse(url)
                    _ = parsed.port  # Access validates invalid/out-of-range ports.
                    if not parsed.hostname or parsed.username is not None or parsed.password is not None or parsed.fragment:
                        raise ValueError("invalid endpoint")
                    is_local = parsed.hostname in {"localhost", "127.0.0.1", "::1"}
                    if parsed.scheme != "https" and not (parsed.scheme == "http" and source.get("allow_insecure_local") is True and is_local):
                        errors.append(f"network source {source_id} must use HTTPS")
                except ValueError:
                    errors.append(f"invalid {field} for {source_id}; use a host URL without credentials or fragments")
        if adapter == "http-json-v1":
            if str(source.get("method", "GET")).upper() != "GET":
                errors.append(f"http-json-v1 source {source_id} must use GET")
            mapping = source.get("mapping")
            if not isinstance(mapping, dict) or not mapping.get("items") or not mapping.get("name"):
                errors.append(f"http-json-v1 source {source_id} needs mapping.items and mapping.name")
            elif any(not isinstance(value, str) for value in mapping.values()):
                errors.append(f"http-json-v1 mapping paths for {source_id} must be strings")
            headers = source.get("headers", {})
            if not isinstance(headers, dict):
                errors.append(f"headers for {source_id} must be an object")
            else:
                for name, value in headers.items():
                    if not isinstance(name, str) or not HEADER_NAME_RE.fullmatch(name):
                        errors.append(f"invalid header name for {source_id}: {name!r}")
                        continue
                    if name.lower() in _CREDENTIAL_HEADERS and not isinstance(value, dict):
                        errors.append(f"credential header {name} for {source_id} must reference an environment variable")
                    if isinstance(value, dict):
                        unknown = set(value) - {"env", "prefix", "optional"}
                        if unknown:
                            errors.append(f"unknown credential header fields for {source_id}.{name}: " + ", ".join(sorted(unknown)))
                        if not ENV_NAME_RE.fullmatch(str(value.get("env", ""))):
                            errors.append(f"credential header {name} for {source_id} needs a valid env name")
                        if not isinstance(value.get("prefix", ""), str) or not isinstance(value.get("optional", False), bool):
                            errors.append(f"invalid credential header options for {source_id}.{name}")
                        elif _control_characters(value.get("prefix", "")):
                            errors.append(f"credential header prefix for {source_id}.{name} must not contain control characters")
                    elif not isinstance(value, str):
                        errors.append(f"header {name} for {source_id} must be a string or environment reference")
                    elif _control_characters(value):
                        errors.append(f"header {name} for {source_id} must not contain control characters")
            for field in ("query_param", "limit_param"):
                if field in source and (not isinstance(source[field], str) or not source[field]):
                    errors.append(f"{field} for {source_id} must be a non-empty string")
    return errors


@contextmanager
def _overlay_lock(path: Path):
    """Serialize cooperating writers; never guess whether an existing lock is stale."""
    lock_path = path.with_name(f".{path.name}.lock")
    token = f"pid={os.getpid()} owner={secrets.token_hex(16)}\n".encode("ascii")
    try:
        descriptor = os.open(lock_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise ConfigurationError(
            f"configuration is busy: {lock_path}; reload and retry. If this persists, "
            "inspect the lock and confirm no finder is writing before removing it"
        ) from exc
    identity = os.fstat(descriptor)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(token)
        yield
    finally:
        # Another process must not remove a lock it did not acquire. Check both
        # filesystem identity and an unpredictable ownership token before unlink.
        try:
            flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0)
            check_descriptor = os.open(lock_path, flags)
            with os.fdopen(check_descriptor, "rb") as stream:
                current = os.fstat(stream.fileno())
                owned = (
                    stat.S_ISREG(current.st_mode)
                    and (current.st_dev, current.st_ino) == (identity.st_dev, identity.st_ino)
                    and stream.read(len(token) + 1) == token
                )
            if owned:
                named = lock_path.lstat()
                if (named.st_dev, named.st_ino) == (identity.st_dev, identity.st_ino):
                    lock_path.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise ConfigurationError(
                f"could not release configuration lock {lock_path}; configuration may have been saved, "
                "so reload before retrying"
            ) from exc


def _serialize_overlay(overlay: dict[str, Any]) -> bytes:
    """Keep simple preferences scannable; retain advanced JSON formatting."""
    field = "sources" if "sources" in overlay else "repositories"
    if field in overlay and not set(overlay) - {field, "schema_version"}:
        lines = ["{"]
        if "schema_version" in overlay:
            lines.append(f'  "schema_version": {overlay["schema_version"]},')
        entries = overlay[field]
        if entries:
            lines.append(f'  "{field}": [')
            rows = [json.dumps(
                {"id": entry["id"], "enabled": entry["enabled"]},
                ensure_ascii=False, allow_nan=False,
            ) for entry in entries]
            lines.append(",\n".join("    " + row for row in rows))
            lines.append("  ]")
        else:
            lines.append(f'  "{field}": []')
        lines.append("}")
        return ("\n".join(lines) + "\n").encode("utf-8")
    return (json.dumps(overlay, indent=2, ensure_ascii=False, sort_keys=True, allow_nan=False) + "\n").encode("utf-8")


def save_overlay(
    path: Path, overlay: dict[str, Any], *, expected_fingerprint: str | None | object = _CURRENT_SNAPSHOT,
) -> str:
    """Atomically save only if the inspected overlay still matches, under a writer lock.

    Callers editing an EffectiveConfig must pass its original fingerprint. The
    default preserves this low-level helper's direct-save API by taking a snapshot
    when called; it cannot infer an earlier inspection performed by its caller.
    """
    validate_overlay(overlay)
    payload = _serialize_overlay(overlay)
    if len(payload) > MAX_CONFIG_BYTES:
        raise ConfigurationError(f"configuration exceeds {MAX_CONFIG_BYTES} bytes")
    temp_name = None
    try:
        _checked_user_config_path(path)
        if expected_fingerprint is _CURRENT_SNAPSHOT:
            _, expected_fingerprint = _read_json_snapshot(path, required=False)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with _overlay_lock(path):
            handle, temp_name = tempfile.mkstemp(prefix=".sources-", dir=path.parent)
            with os.fdopen(handle, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            _, current_fingerprint = _read_json_snapshot(path, required=False)
            if current_fingerprint != expected_fingerprint:
                raise ConfigurationError("configuration changed since it was loaded; reload and retry; no changes were saved")
            os.replace(temp_name, path)
    except OSError as exc:
        raise ConfigurationError(f"cannot save configuration {path}: {exc.strerror}") from exc
    finally:
        if temp_name is not None:
            try:
                os.unlink(temp_name)
            except FileNotFoundError:
                pass
    return hashlib.sha256(payload).hexdigest()


def _repository_overlay(config: EffectiveConfig, overlay: dict[str, Any]) -> dict[str, Any]:
    """Expand preferences to direct flags, without flattening disabled-pack guards.

    Omitted source IDs inherit their catalogue defaults. Keep existing row
    order and append new catalogue entries, so explicit writes are predictable.
    Custom entries come from the proposed overlay, including additions/removals
    made since this EffectiveConfig snapshot was loaded.
    """
    normalized = deepcopy(overlay)
    catalogue = {source["id"]: source for source in config.sources if source.get("origin") != "user"}
    catalogue.update({source["id"]: source for source in overlay.get("custom_sources", [])})
    flags = {
        source_id: source.get("enabled", True)
        for source_id, source in catalogue.items()
    }
    for source_id, override in overlay.get("source_overrides", {}).items():
        if source_id in flags and "enabled" in override:
            flags[source_id] = override["enabled"]
    order = []
    for entry in overlay.get("sources", overlay.get("repositories", [])):
        source_id = entry["id"]
        if source_id in flags:
            flags[source_id] = entry["enabled"]
            order.append(source_id)
    order.extend(source_id for source_id in catalogue if source_id not in order)
    normalized.pop("source_overrides", None)
    normalized.pop("repositories", None)
    normalized["sources"] = [{"id": source_id, "enabled": flags[source_id]} for source_id in order]
    return normalized


def _save_config_overlay(config: EffectiveConfig, overlay: dict[str, Any]) -> None:
    overlay = _repository_overlay(config, overlay)
    fingerprint = save_overlay(config.overlay_path, overlay, expected_fingerprint=config.overlay_fingerprint)
    # Failed or stale writes must not mutate the caller's snapshot in memory.
    config.overlay = overlay
    config.overlay_fingerprint = fingerprint


def initialize_source_config(config: EffectiveConfig) -> bool:
    """Explicitly create or complete the editable source list, safely.

    Reading configuration never writes or migrates it. This explicit operation
    preserves the selected path, every direct flag, and advanced configuration.
    Return False without rewriting a complete unchanged file. Stale snapshots
    fail, including the no-op case, rather than claiming someone else's edits.
    """
    overlay = _repository_overlay(config, config.overlay)
    if config.overlay_fingerprint is not None and overlay == config.overlay:
        _checked_user_config_path(config.overlay_path)
        _, current = _read_json_snapshot(config.overlay_path, required=False)
        if current != config.overlay_fingerprint:
            raise ConfigurationError("configuration changed since it was loaded; reload and retry; no changes were saved")
        return False
    _save_config_overlay(config, overlay)
    return True


# Compatibility alias for repository-oriented callers.
initialize_repository_config = initialize_source_config


def _validate_added_source(config: EffectiveConfig, source: dict[str, Any]) -> None:
    prospective = EffectiveConfig(
        settings=config.settings,
        packs=config.packs,
        sources=config.sources + [source],
        overlay_path=config.overlay_path,
        overlay=config.overlay,
    )
    errors = validate_effective(prospective)
    if errors:
        raise ConfigurationError("; ".join(errors))


def set_source_enabled(config: EffectiveConfig, source_id: str, enabled: bool) -> None:
    if config.source(source_id) is None:
        raise ConfigurationError(f"unknown source: {source_id}")
    if type(enabled) is not bool:
        raise ConfigurationError(f"enabled for source {source_id} must be boolean")
    overlay = _repository_overlay(config, config.overlay)
    for source in overlay["sources"]:
        if source["id"] == source_id:
            source["enabled"] = enabled
    _save_config_overlay(config, overlay)


def set_pack_enabled(config: EffectiveConfig, pack_id: str, enabled: bool) -> None:
    if config.pack(pack_id) is None:
        raise ConfigurationError(f"unknown pack: {pack_id}")
    if type(enabled) is not bool:
        raise ConfigurationError(f"enabled for pack {pack_id} must be boolean")
    overlay = deepcopy(config.overlay)
    overlay.setdefault("pack_overrides", {}).setdefault(pack_id, {})["enabled"] = enabled
    _save_config_overlay(config, overlay)


def add_github_repository(
    config: EffectiveConfig,
    repository_or_url: str,
    *,
    source_id: str | None,
    ref: str,
    include: list[str] | None,
    exclude: list[str] | None,
    pack_id: str | None = None,
) -> dict[str, Any]:
    repository = parse_github_repository(repository_or_url)
    if not repository:
        raise ConfigurationError("expected owner/repository or a GitHub repository URL")
    generated_id = source_id or slugify(repository.replace("/", "-"))
    if not SOURCE_ID_RE.fullmatch(generated_id):
        raise ConfigurationError(f"invalid source id: {generated_id}")
    if config.source(generated_id):
        raise ConfigurationError(f"source already exists: {generated_id}")
    if pack_id and config.pack(pack_id) is None:
        raise ConfigurationError(f"unknown pack: {pack_id}")
    source = {
        "id": generated_id,
        "kind": "repository",
        "adapter": "github-repo",
        "enabled": True,
        "repository": repository,
        "ref": ref,
        "include": include or ["**/SKILL.md"],
        "exclude": exclude or [],
        "trust": "user-configured",
        "provenance": f"https://github.com/{repository}",
    }
    if pack_id:
        source["pack"] = pack_id
    _validate_added_source(config, source)
    overlay = deepcopy(config.overlay)
    overlay.setdefault("custom_sources", []).append(source)
    _save_config_overlay(config, overlay)
    return source


def add_local_directory(
    config: EffectiveConfig,
    path: str,
    *,
    source_id: str,
    include: list[str] | None,
    exclude: list[str] | None,
    pack_id: str | None = None,
) -> dict[str, Any]:
    if config.source(source_id):
        raise ConfigurationError(f"source already exists: {source_id}")
    if not SOURCE_ID_RE.fullmatch(source_id):
        raise ConfigurationError(f"invalid source id: {source_id}")
    if pack_id and config.pack(pack_id) is None:
        raise ConfigurationError(f"unknown pack: {pack_id}")
    source = {
        "id": source_id,
        "kind": "repository",
        "adapter": "local-directory",
        "enabled": True,
        "path": str(Path(path).expanduser().resolve()),
        "include": include or ["**/SKILL.md"],
        "exclude": exclude or [],
        "trust": "user-configured",
    }
    if pack_id:
        source["pack"] = pack_id
    _validate_added_source(config, source)
    overlay = deepcopy(config.overlay)
    overlay.setdefault("custom_sources", []).append(source)
    _save_config_overlay(config, overlay)
    return source


def remove_custom_source(config: EffectiveConfig, source_id: str) -> None:
    overlay = deepcopy(config.overlay)
    custom = overlay.setdefault("custom_sources", [])
    remaining = [source for source in custom if source.get("id") != source_id]
    if len(remaining) == len(custom):
        if config.source(source_id):
            raise ConfigurationError(f"bundled source cannot be removed; disable it instead: {source_id}")
        raise ConfigurationError(f"unknown source: {source_id}")
    overlay["custom_sources"] = remaining
    if "source_overrides" in overlay:
        overlay["source_overrides"].pop(source_id, None)
    for field in ("sources", "repositories"):
        if field in overlay:
            overlay[field] = [entry for entry in overlay[field] if entry["id"] != source_id]
    _save_config_overlay(config, overlay)


def import_source_pack(config: EffectiveConfig, path: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    pack_path = Path(path).expanduser().resolve()
    payload = _read_json(pack_path, required=True)
    if type(payload.get("schema_version")) is not int or payload.get("schema_version") != SCHEMA_VERSION:
        raise ConfigurationError(f"source pack must declare schema_version {SCHEMA_VERSION}")
    pack = payload.get("pack")
    sources = payload.get("sources")
    if not isinstance(pack, dict) or not isinstance(sources, list) or not sources:
        raise ConfigurationError("source pack needs a pack object and a non-empty sources array")
    pack_id = str(pack.get("id", ""))
    if not SOURCE_ID_RE.fullmatch(pack_id):
        raise ConfigurationError(f"invalid pack id: {pack_id!r}")
    if config.pack(pack_id):
        raise ConfigurationError(f"pack already exists: {pack_id}")

    existing_source_ids = {source["id"] for source in config.sources}
    imported_sources: list[dict[str, Any]] = []
    for raw_source in sources:
        if not isinstance(raw_source, dict):
            raise ConfigurationError("every source pack entry must be an object")
        source = deepcopy(raw_source)
        source_id = str(source.get("id", ""))
        if source_id in existing_source_ids:
            raise ConfigurationError(f"source already exists: {source_id}")
        if any(item.get("id") == source_id for item in imported_sources):
            raise ConfigurationError(f"duplicate source in pack: {source_id}")
        source["pack"] = pack_id
        adapter_name = source.get("adapter")
        spec = ADAPTER_SPECS.get(adapter_name) if isinstance(adapter_name, str) else None
        if spec is not None:
            source.setdefault("kind", spec.kind)
        source.setdefault("enabled", True)
        source.setdefault("trust", "user-configured")
        imported_sources.append(source)

    imported_pack = deepcopy(pack)
    imported_pack.setdefault("description", f"Imported from {pack_path.name}")
    imported_pack.setdefault("enabled", True)
    prospective = EffectiveConfig(
        settings=config.settings,
        packs=config.packs + [{**deepcopy(imported_pack), "origin": "user"}],
        sources=config.sources + [
            {
                **deepcopy(source),
                "origin": "user",
                "pack_enabled": bool(imported_pack["enabled"]),
                "effective_enabled": bool(source.get("enabled", True) and imported_pack["enabled"]),
            }
            for source in imported_sources
        ],
        overlay_path=config.overlay_path,
        overlay=config.overlay,
    )
    errors = validate_effective(prospective)
    if errors:
        raise ConfigurationError("; ".join(errors))

    overlay = deepcopy(config.overlay)
    overlay.setdefault("custom_packs", []).append(imported_pack)
    overlay.setdefault("custom_sources", []).extend(imported_sources)
    _save_config_overlay(config, overlay)
    return imported_pack, imported_sources


def remove_custom_pack(config: EffectiveConfig, pack_id: str) -> int:
    overlay = deepcopy(config.overlay)
    custom_packs = overlay.setdefault("custom_packs", [])
    remaining_packs = [pack for pack in custom_packs if pack.get("id") != pack_id]
    if len(remaining_packs) == len(custom_packs):
        if config.pack(pack_id):
            raise ConfigurationError(f"bundled pack cannot be removed; disable it instead: {pack_id}")
        raise ConfigurationError(f"unknown pack: {pack_id}")
    custom_sources = overlay.setdefault("custom_sources", [])
    removed_source_ids = {source.get("id") for source in custom_sources if source.get("pack") == pack_id}
    overlay["custom_packs"] = remaining_packs
    overlay["custom_sources"] = [source for source in custom_sources if source.get("pack") != pack_id]
    overlay.setdefault("pack_overrides", {}).pop(pack_id, None)
    for source_id in removed_source_ids:
        if "source_overrides" in overlay:
            overlay["source_overrides"].pop(source_id, None)
    for field in ("sources", "repositories"):
        if field in overlay:
            overlay[field] = [entry for entry in overlay[field] if entry["id"] not in removed_source_ids]
    _save_config_overlay(config, overlay)
    return len(removed_source_ids)
