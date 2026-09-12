"""Read-only, credential-safe views of configured discovery sources."""
from __future__ import annotations

import html
import os
import re
from collections.abc import Mapping
from typing import Any
from urllib.parse import quote, urlparse

from .config import ENV_NAME_RE, EffectiveConfig
from .text import clean_text, parse_github_repository, safe_web_url


def source_type(source: Mapping[str, object]) -> str:
    """Classify a discovery source without changing the adapter contract."""
    if source.get("adapter") == "local-directory":
        return "Local directory"
    if source.get("adapter") == "github-code-search":
        return "Search index"
    if source.get("kind") == "repository" or source.get("adapter") == "github-repo":
        return "Repository"
    return "Registry"


def public_source_url(source: Mapping[str, object]) -> str | None:
    """Return a browsable repository or registry origin, never a private endpoint.

    API paths, queries and fragments can contain credentials. The registry's
    origin is sufficient for browsing; do not include or attempt to redact those
    components. This only reads configuration and makes no network request.
    """
    if source.get("adapter") == "local-directory":
        return None
    if source.get("adapter") == "github-code-search":
        return "https://github.com/search?type=code"
    if source.get("adapter") == "tessl":
        return "https://tessl.io/registry"
    repository = parse_github_repository(source.get("repository"))
    if repository:
        return f"https://github.com/{repository}"
    for field in ("base_url", "endpoint"):
        url = safe_web_url(source.get(field))
        if not url:
            continue
        parsed = urlparse(url)
        if parsed.scheme != "https" and not (
            parsed.scheme == "http" and source.get("allow_insecure_local") is True
            and parsed.hostname in {"localhost", "127.0.0.1", "::1"}
        ):
            continue
        return f"{parsed.scheme}://{parsed.netloc}"
    return None


def _text(value: object) -> str:
    text = html.escape(clean_text(value, 2000), quote=False)
    text = re.sub(r"([\\`*_\[\]~])", r"\\\1", text)
    return text.replace("|", "&#124;")


def _credentials(source: Mapping[str, Any], environ: Mapping[str, str]) -> list[dict[str, Any]]:
    references: dict[str, bool] = {}
    auth_env = source.get("auth_env")
    if isinstance(auth_env, str) and ENV_NAME_RE.fullmatch(auth_env):
        references[auth_env] = not source.get("auth_optional", False)
    headers = source.get("headers", {})
    if isinstance(headers, Mapping):
        for header in headers.values():
            if not isinstance(header, Mapping):
                continue
            env = header.get("env")
            if isinstance(env, str) and ENV_NAME_RE.fullmatch(env):
                references[env] = references.get(env, False) or not header.get("optional", False)
    return [{"environment_variable": env, "required": required, "present": bool(environ.get(env))}
            for env, required in sorted(references.items())]


def source_rows(config: EffectiveConfig, *, environ: Mapping[str, str] | None = None) -> list[dict[str, Any]]:
    """Produce an allowlisted view; never serialize raw config or secret values."""
    environment = os.environ if environ is None else environ
    rows = []
    for source in config.sources:
        direct = source.get("enabled", True) is True
        pack_enabled = source.get("pack_enabled", True) is True
        source_id = clean_text(source["id"])
        pack_id = clean_text(source.get("pack")) or None
        effective = direct and pack_enabled
        if effective:
            action = f"Disable {source_id}"
        elif not pack_enabled:
            action = f"Enable pack {pack_id} (also affects its other enabled sources)"
            if not direct:
                action += f"; then Enable {source_id}"
        else:
            action = f"Enable {source_id}"
        rows.append({
            "id": source_id,
            "kind": "local directory" if source.get("adapter") == "local-directory" else clean_text(source.get("kind")),
            "type": source_type(source),
            "public_url": public_source_url(source),
            "enabled": effective,
            "direct_enabled": direct,
            "pack_id": pack_id,
            "pack_enabled": pack_enabled,
            "credentials": _credentials(source, environment),
            "suggested_request": action,
        })
    return rows


def render_sources_table(rows: list[dict[str, Any]], *, show_credential_presence: bool = True) -> str:
    """Use one table for effective settings and documented bundled defaults."""
    lines = ["| Source | Type | Requirements | Ask to change | Enabled |",
             "|---|---|---|---|---|"]
    for row in rows:
        name = _text(row["id"])
        if row["public_url"]:
            destination = quote(row["public_url"], safe="/:?#@!$&*+,;=%~._-")
            name = f"[{name}]({destination})"
        elif row["type"] != "Local directory":
            name += " (link unavailable)"
        state = "✅ Enabled" if row["enabled"] else "❌ Disabled"
        credentials = []
        for credential in row["credentials"]:
            requirement = "required" if credential["required"] else "optional"
            if show_credential_presence:
                state_label = "present, not verified" if credential["present"] else "missing"
                requirement += f", {state_label}"
            credentials.append(f"{_text(credential['environment_variable'])}: {requirement}")
        access = "; ".join(credentials) or "No configured key required"
        if not row["pack_enabled"]:
            access += f"; pack {_text(row['pack_id'])} disabled (blocks this source)"
        lines.append(f"| {name} | {row['type']} | {access} | {_text(row['suggested_request'])} | {state} |")
    return "\n".join(lines)


def render_sources_markdown(config: EffectiveConfig, *, environ: Mapping[str, str] | None = None) -> str:
    rows = source_rows(config, environ=environ)
    enabled = sum(row["enabled"] for row in rows)
    lines = [f"Configured sources: {enabled} enabled of {len(rows)}.", "", render_sources_table(rows)]
    lines.extend(["", "These are configuration states, not live availability. Listing sources sends no queries and changes nothing.",
                  'Say "Enable SOURCE-ID" or "Disable SOURCE-ID", or edit the enabled flags in your source configuration.'])
    if any(not row["pack_enabled"] for row in rows):
        lines.append("Enabling a source does not enable a disabled pack; changing a pack requires an explicit request and also affects its other enabled sources.")
    return "\n".join(lines)
