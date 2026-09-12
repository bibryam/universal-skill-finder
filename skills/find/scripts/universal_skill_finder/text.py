from __future__ import annotations

import hashlib
import re
import unicodedata
from pathlib import PurePosixPath
from urllib.parse import unquote, urlparse, urlunparse

CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")
TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9+.#_-]*")
SOURCE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{1,63}$")


def clean_text(value: object, limit: int = 2000) -> str:
    text = "" if value is None else str(value)
    text = unicodedata.normalize("NFKC", text)
    # Registry metadata must not hide or reorder text in a terminal or an agent's
    # review. In particular, strip bidi overrides and zero-width format controls.
    text = "".join(char for char in text if unicodedata.category(char) != "Cf")
    text = CONTROL_RE.sub("", text).replace("\r", " ").replace("\n", " ")
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


def slugify(value: str) -> str:
    value = clean_text(value, 300).lower().replace("_", "-").replace(" ", "-")
    value = re.sub(r"[^a-z0-9.-]+", "-", value)
    return re.sub(r"-+", "-", value).strip("-.") or "skill"


def tokens(value: str) -> list[str]:
    return TOKEN_RE.findall(clean_text(value).lower())


def text_match_percent(query: str, *fields: str) -> int:
    query_tokens = list(dict.fromkeys(tokens(query)))
    if not query_tokens:
        return 0
    haystack = " ".join(fields).lower()
    field_tokens = {part for token in tokens(haystack) for part in (token, *re.split(r"[._-]", token)) if part}

    def singular(token: str) -> str:
        if len(token) > 4 and token.endswith("ies"):
            return token[:-3] + "y"
        return token[:-1] if len(token) > 4 and token.endswith("s") and not token.endswith("ss") else token

    field_stems = {singular(token) for token in field_tokens}
    matched = sum(1 for token in query_tokens if singular(token) in field_stems)
    score = round(100 * matched / len(query_tokens))
    phrase = clean_text(query).lower()
    if matched and phrase and phrase in haystack:
        score = min(100, score + 15)
    return score


def safe_web_url(value: object) -> str | None:
    """Validate an untrusted link without repairing deceptive input."""
    if not isinstance(value, str) or not value or len(value) > 4000:
        return None
    if any(char.isspace() or unicodedata.category(char) in {"Cc", "Cf"} for char in value) or "\\" in value:
        return None
    try:
        parsed = urlparse(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username is not None or parsed.password is not None:
            return None
        if parsed.port is not None and not 1 <= parsed.port <= 65535:
            return None
    except ValueError:
        return None
    return value


def safe_install_reference(value: object) -> str | None:
    """A conservative registry reference, safe as one argument and for review."""
    if not isinstance(value, str) or not re.fullmatch(r"@?[A-Za-z0-9][A-Za-z0-9._/-]{0,299}", value):
        return None
    if any(part in {"", ".", ".."} or part.startswith("-") for part in value.lstrip("@").split("/")):
        return None
    return value


def safe_skill_path(value: object) -> str | None:
    # A root-level SKILL.md is represented by '.'. Never pass absolute paths or
    # traversal from a remote registry to an installer.
    if value == ".":
        return "."
    # Hidden skill roots such as .agents/skills are valid repository paths.
    # Keep reference/flag syntax separate and reject traversal components.
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9.][A-Za-z0-9._/-]{0,299}", value):
        return None
    if any(part in {"", ".", ".."} or part.startswith("-") for part in value.split("/")):
        return None
    return value


def _github_parts(value: str) -> list[str] | None:
    if not safe_web_url(value):
        return None
    parsed = urlparse(value)
    if parsed.scheme != "https" or parsed.hostname not in {"github.com", "www.github.com"} or parsed.port not in {None, 443} or parsed.params:
        return None
    parts = parsed.path.strip("/").split("/")
    if len(parts) < 2:
        return None
    return parts


def parse_github_repository(value: str) -> str | None:
    if not isinstance(value, str):
        return None
    parts = _github_parts(value) if ":" in value else value.rstrip("/").split("/")
    if not parts or len(parts) < 2:
        return None
    if ":" not in value and len(parts) != 2:
        return None
    owner, repository = parts[0], parts[1].removesuffix(".git")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}", owner):
        return None
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", repository) or repository in {".", ".."} or repository.startswith("-"):
        return None
    return f"{owner}/{repository}"


def parse_github_tree_url(value: str) -> tuple[str | None, str | None, str | None]:
    parts = _github_parts(value)
    repository = parse_github_repository(value)
    if not parts or not repository:
        return None, None, None
    if len(parts) >= 4 and parts[2] in {"tree", "blob"}:
        ref = safe_install_reference(unquote(parts[3]))
        path = unquote("/".join(parts[4:])) or None
        if not ref or (path is not None and safe_skill_path(path) is None):
            return None, None, None
        if path and parts[2] == "blob":
            path = str(PurePosixPath(path).parent)
        return repository, path, ref
    return repository, None, None


def occurrence_aliases(candidate: object) -> set[str]:
    repository = clean_text(getattr(candidate, "repository", "") or "").lower().strip("/")
    skill_path = clean_text(getattr(candidate, "skill_path", "") or "").strip("/")
    slug = slugify(getattr(candidate, "slug", "") or getattr(candidate, "name", ""))
    publisher_raw = clean_text(getattr(candidate, "publisher", "") or "")
    namespace_raw = clean_text(getattr(candidate, "identity_namespace", "") or "")
    publisher = slugify(publisher_raw) if publisher_raw else ""
    namespace = slugify(namespace_raw) if namespace_raw else ""
    canonical_url = clean_text(getattr(candidate, "canonical_url", "") or "", 1000)
    aliases: set[str] = set()
    if repository and skill_path:
        aliases.add(f"git:github.com/{repository}#{skill_path}")
    elif repository and slug:
        aliases.add(f"git-slug:github.com/{repository}#{slug}")
    elif namespace and publisher and slug:
        aliases.add(f"hosted:{namespace}:{publisher}/{slug}")
    elif canonical_url:
        parsed = urlparse(canonical_url)
        if parsed.scheme in {"http", "https"} and parsed.hostname:
            # Query parameters and SPA fragments can identify different skills.
            # Do not guess that they are tracking noise and merge distinct targets.
            normalized = urlunparse((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path.rstrip("/"),
                                     parsed.params, parsed.query, parsed.fragment))
            aliases.add(f"url:{normalized}")
    if not aliases:
        source_id = clean_text(getattr(candidate, "source_id", "unknown"))
        native_id = clean_text(getattr(candidate, "native_id", "unknown"))
        aliases.add(f"native:{source_id}:{native_id}")
    return aliases


def weak_occurrence_aliases(candidate: object) -> set[str]:
    repository = clean_text(getattr(candidate, "repository", "") or "").lower().strip("/")
    slug = slugify(getattr(candidate, "slug", "") or getattr(candidate, "name", ""))
    return {f"git-slug:github.com/{repository}#{slug}"} if repository and slug else set()


def stable_result_id(aliases: set[str]) -> str:
    priorities = ("git:", "git-slug:", "hosted:", "url:", "native:", "sha256:")
    material_text = ""
    for prefix in priorities:
        matching = sorted(alias for alias in aliases if alias.startswith(prefix))
        if matching:
            material_text = matching[0]
            break
    material = (material_text or sorted(aliases)[0]).encode("utf-8")
    return "skill:" + hashlib.sha256(material).hexdigest()[:20]
