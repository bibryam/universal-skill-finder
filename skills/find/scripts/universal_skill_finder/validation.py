"""Anonymous, bounded public-destination proof collection.

This module deliberately does not use the authenticated source HTTP client.
Callers supply the frozen shared request-budget/permit-pool interfaces and an
injectable transport.  It is inert until federation wires it into a search.
"""
from __future__ import annotations

import ipaddress
import hashlib
import http.client
import json
import multiprocessing
import socket
import ssl
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from html.parser import HTMLParser
from typing import Any, Callable, Iterable, Mapping, Protocol
from urllib.parse import quote, unquote, urljoin, urlparse

from .models import LinkProof
from .runtime import Deadline, RequestBudget, RuntimeLimitError
from .text import parse_github_repository, safe_install_reference, safe_skill_path
from .frontmatter import parse_frontmatter


MAX_REDIRECTS = 3
MAX_HTML_BYTES = 512 * 1024
MAX_TARGET_BYTES = 256 * 1024
MAX_GITHUB_METADATA_BYTES = 64 * 1024
REVIEWED_REDIRECTS = {("https", "skills.sh", 443): ("https", "www.skills.sh", 443)}


@dataclass(frozen=True)
class AnonymousResponse:
    status: int
    headers: Mapping[str, str] = field(default_factory=dict)
    body: bytes = b""
    connection_address: str | None = None


@dataclass(frozen=True)
class Destination:
    candidate_id: str
    role: str
    url: str | None
    expected_identity: object | None = None
    profile: "ProviderProfile | None" = None


@dataclass(frozen=True)
class ProviderProfile:
    """Reviewed provider-specific proof, never remote response supplied."""
    name: str
    identity_check: Callable[[AnonymousResponse, object | None], bool | None]
    soft_error_check: Callable[[AnonymousResponse], bool] | None = None


@dataclass
class TargetResolutionCache:
    """Short-lived, per-search target facts shared by selected candidates.

    This is deliberately in-memory: it coalesces default-branch and exact-file
    checks within one bounded search/page/Inspect phase, while persistent proof
    caching remains the caller's explicit policy.  It never stores credentials
    or response bodies.
    """
    _defaults: dict[str, tuple[str | None, str | None]] = field(default_factory=dict)
    _default_waiters: dict[str, threading.Event] = field(default_factory=dict, repr=False)
    _proofs: dict[tuple[str, str, str, str], dict[str, Any]] = field(default_factory=dict)
    _lock: Any = field(default_factory=threading.RLock, repr=False)

    def default(self, repository: str) -> tuple[str | None, str | None] | None:
        with self._lock:
            return self._defaults.get(repository.casefold())

    def remember_default(self, repository: str, branch: str | None, detail: str | None) -> None:
        with self._lock:
            key = repository.casefold()
            self._defaults[key] = (branch, detail)
            waiter = self._default_waiters.pop(key, None)
            if waiter is not None:
                waiter.set()

    def claim_default(self, repository: str) -> tuple[bool, threading.Event | None, tuple[str | None, str | None] | None]:
        """Return leader/waiter state for one authoritative default-branch GET."""
        with self._lock:
            key = repository.casefold()
            cached = self._defaults.get(key)
            if cached is not None:
                return False, None, cached
            waiter = self._default_waiters.get(key)
            if waiter is not None:
                return False, waiter, None
            waiter = threading.Event()
            self._default_waiters[key] = waiter
            return True, waiter, None

    def proof(self, key: tuple[str, str, str, str]) -> dict[str, Any] | None:
        with self._lock:
            value = self._proofs.get(key)
            return dict(value) if value is not None else None

    def remember_proof(self, key: tuple[str, str, str, str], proof: Mapping[str, Any]) -> None:
        with self._lock:
            self._proofs[key] = dict(proof)


class _MetadataParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.meta: dict[str, str] = {}
        self.canonicals: list[str] = []
        self.anchors: list[str] = []
        self.headings: list[str] = []
        self.text: list[str] = []
        self._heading_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "meta":
            values = {name.lower(): value for name, value in attrs if value is not None}
            if tag.lower() == "link" and values.get("rel", "").casefold() == "canonical":
                href = values.get("href")
                if href and len(href) <= 4000:
                    self.canonicals.append(href)
            if tag.lower() == "a":
                href = values.get("href")
                if href and len(href) <= 4000:
                    self.anchors.append(href)
            if tag.lower() == "h1":
                self._heading_depth += 1
            return
        values = {name.lower(): value for name, value in attrs if value is not None}
        name = values.get("name") or values.get("property")
        content = values.get("content")
        if name and content is not None and len(content) <= 1000:
            self.meta[name.lower()] = content

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "h1" and self._heading_depth:
            self._heading_depth -= 1

    def handle_data(self, data: str) -> None:
        value = " ".join(data.split())
        if not value or len(value) > 2000:
            return
        self.text.append(value)
        if self._heading_depth:
            self.headings.append(value)


def _html_document(response: AnonymousResponse) -> _MetadataParser | None:
    """Parse only bounded HTML evidence, without accepting generic prose."""
    content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().casefold()
    if content_type and content_type not in {"text/html", "application/xhtml+xml"}:
        return None
    parser = _MetadataParser()
    try:
        parser.feed(response.body.decode("utf-8", "strict"))
        parser.close()
    except (UnicodeDecodeError, ValueError):
        return None
    return parser


def _json_object(response: AnonymousResponse) -> Mapping[str, Any] | None:
    try:
        value = json.loads(response.body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _expected_object(expected: object | None) -> Mapping[str, str] | None:
    if isinstance(expected, str) and expected:
        return {"id": expected}
    if isinstance(expected, Mapping) and expected and all(isinstance(key, str) and isinstance(value, str) and value
                                                           for key, value in expected.items()):
        return expected
    return None


def _structured_identity(response: AnonymousResponse, expected: object | None) -> bool | None:
    """Require every caller-declared identity field from a small JSON object."""
    expected_values = _expected_object(expected)
    document = _json_object(response)
    if expected_values is None or document is None:
        return None
    values = document.get("data") if isinstance(document.get("data"), dict) else document
    observed_any = False
    for key, value in expected_values.items():
        # ``url`` is a locally-derived route guard for HTML pages, never a
        # provider JSON field.
        if key == "url":
            continue
        actual = values.get(key)
        if actual is None:
            return None
        observed_any = True
        if not isinstance(actual, str) or actual != value:
            return False
    return True if observed_any else None


def _normalise_identity(value: str) -> str:
    return "".join(character for character in value.casefold() if character.isalnum())


def _html_text(parser: _MetadataParser) -> str:
    return " ".join(parser.text).casefold()


def _route_segments(expected: Mapping[str, str]) -> list[str]:
    url = expected.get("url")
    if not url:
        return []
    return [unquote(part) for part in urlparse(url).path.split("/") if part]


def _contains_phrase(text: str, phrase: str) -> bool:
    """Literal, case-folded phrase only; no fuzzy page-prose matching."""
    return bool(phrase and phrase.casefold() in text)


def _heading_matches(parser: _MetadataParser, value: str) -> bool:
    wanted = _normalise_identity(value)
    return bool(wanted and any(_normalise_identity(heading) == wanted for heading in parser.headings))


def _html_or_structured_listing_identity(response: AnonymousResponse, expected: object | None,
                                         *, provider: str) -> bool | None:
    """Provider routes are HTML today; JSON remains a narrow compatibility path.

    A provider-owned route plus a bare 200 is deliberately insufficient.  The
    returned document must expose the deterministic identity carried by that
    route (and, where available, its creator/repository pair).
    """
    values = _expected_object(expected)
    if values is None:
        return None
    structured = _structured_identity(response, values)
    if structured is not None:
        return structured
    parser = _html_document(response)
    if parser is None:
        return None
    text = _html_text(parser)
    segments = _route_segments(values)
    if provider == "skills-sh":
        # /owner/repository/skill.  The public page has a matching h1; this
        # protects against a generic shell or an unrelated route returning 200.
        terminal = segments[-1] if len(segments) >= 3 else values.get("id", "").rsplit("/", 1)[-1]
        owner_repo = "/".join(segments[-3:-1]) if len(segments) >= 3 else ""
        return True if _heading_matches(parser, terminal) and (not owner_repo or _contains_phrase(text, owner_repo)) else None
    if provider == "skillsmp":
        # /creators/owner/repository/skill-name.  SkillMP renders the source
        # repository and the named skill in server HTML.
        if len(segments) < 4 or segments[0] != "creators":
            return None
        owner_repo = "/".join(segments[1:3])
        named_skill = segments[2] if segments[-1] == "skill" else segments[-1]
        return True if _contains_phrase(text, owner_repo) and _heading_matches(parser, named_skill) else None
    if provider in {"skillhub-public", "skillhub-pro"}:
        native_id = values.get("id", "")
        # Names are not identities: a generic shell or another owner's skill
        # can legitimately render the same terminal heading.  The complete
        # hierarchical SkillHub ID must be present in the bounded response.
        return True if _contains_phrase(text, native_id) else None
    return None


def _clawhub_identity(response: AnonymousResponse, expected: object | None) -> bool | None:
    values = _expected_object(expected)
    if values is None:
        return None
    parser = _html_document(response)
    if parser is None:
        return None
    owner, slug = values.get("owner"), values.get("slug")
    if not owner or not slug:
        return None
    # ClawHub's rendered install reference is its typed identity.  Requiring
    # both it and the route-derived heading rejects an arbitrary successful
    # HTML response or a same-origin route with a different skill.
    text = _html_text(parser)
    install_ref = f"@{owner}/{slug}"
    return True if _contains_phrase(text, install_ref) and _heading_matches(parser, slug) else None


def _tessl_bundle_identity(response: AnonymousResponse, expected: object | None) -> bool | None:
    values = _expected_object(expected)
    if values is None:
        return None
    parser = _html_document(response)
    if parser is None:
        return None
    package, version = values.get("package"), values.get("version")
    if not package or not version:
        return None
    text = _html_text(parser)
    canonical = parser.canonicals + [parser.meta.get("og:url", "")]
    exact_route = values.get("url") in canonical
    return True if exact_route and _contains_phrase(text, package) and _contains_phrase(text, version) else None


def _tessl_skill_identity(response: AnonymousResponse, expected: object | None) -> bool | None:
    """Require exact Tessl skill, GitHub repository, and visible name identity."""
    values = _expected_object(expected)
    if values is None:
        return None
    repository, name, expected_url = values.get("repository"), values.get("name"), values.get("url")
    if not repository or not name or not expected_url:
        return None
    parser = _html_document(response)
    if parser is None:
        return None
    canonical = parser.canonicals + [parser.meta.get("og:url", "")]
    repository_url = f"https://github.com/{repository}"
    return True if expected_url in canonical and repository_url in parser.anchors and _heading_matches(parser, name) else None


def _polyskill_identity(response: AnonymousResponse, expected: object | None) -> bool | None:
    """Polyskill's public singular detail route exposes its namespaced name."""
    values = _expected_object(expected)
    if values is None:
        return None
    expected_name, expected_url = values.get("name"), values.get("url")
    if not expected_name or not expected_url:
        return None
    parser = _html_document(response)
    if parser is None:
        return None
    canonical = parser.canonicals + [parser.meta.get("og:url", "")]
    return True if expected_url in canonical and _heading_matches(parser, expected_name) else None


def _skillhub_structured_missing(response: AnonymousResponse) -> bool:
    """A provider-reviewed JSON error code, never arbitrary page prose."""
    document = _json_object(response)
    if document is None:
        return False
    error = document.get("error")
    code = error.get("code") if isinstance(error, dict) else document.get("code")
    return code in {"SKILL_NOT_FOUND", "NOT_FOUND"}


def _github_identity(response: AnonymousResponse, expected: object | None) -> bool | None:
    values = _expected_object(expected)
    if values is None:
        return None
    repository, ref, skill_path, requested_url = (values.get("repository"), values.get("ref"),
                                                   values.get("skill_path"), values.get("url"))
    if not all((repository, ref, skill_path is not None, requested_url)):
        return None
    parsed = urlparse(requested_url)
    parts = [unquote(part) for part in parsed.path.strip("/").split("/") if part]
    expected_parts = repository.split("/")
    if (parsed.scheme != "https" or parsed.hostname not in {"github.com", "www.github.com"}
            or len(expected_parts) != 2 or parts[:2] != expected_parts or len(parts) < 4
            or parts[2] != "tree" or parts[3] != ref):
        return False
    actual_path = "/".join(parts[4:]) or "."
    if actual_path != skill_path:
        return False
    parser = _MetadataParser()
    try:
        parser.feed(response.body.decode("utf-8", "strict"))
    except (UnicodeDecodeError, ValueError):
        return None
    observed_repository = parser.meta.get("octolytics-dimension-repository_nwo")
    if observed_repository is None:
        return None
    return observed_repository.casefold() == repository.casefold()


SKILLS_SH_PROFILE = ProviderProfile(
    "skills-sh-html-route-v2", lambda response, expected: _html_or_structured_listing_identity(response, expected, provider="skills-sh"),
)
# These public catalogues render HTML, not their authenticated/search JSON.
# Their contracts intentionally demand provider-specific visible identity, so
# a generic 200 or a page containing the phrase "not found" grants nothing.
SKILLSMP_PROFILE = ProviderProfile(
    "skillsmp-html-route-v2", lambda response, expected: _html_or_structured_listing_identity(response, expected, provider="skillsmp"),
)
SKILLHUB_PUBLIC_PROFILE = ProviderProfile(
    "skillhub-public-html-route-v2", lambda response, expected: _html_or_structured_listing_identity(response, expected, provider="skillhub-public"),
    _skillhub_structured_missing,
)
SKILLHUB_PRO_PROFILE = ProviderProfile(
    "skillhub-pro-html-route-v2", lambda response, expected: _html_or_structured_listing_identity(response, expected, provider="skillhub-pro"),
    _skillhub_structured_missing,
)
CLAWHUB_PROFILE = ProviderProfile("clawhub-html-install-ref-v1", _clawhub_identity)
TESSL_BUNDLE_PROFILE = ProviderProfile("tessl-registry-html-version-v1", _tessl_bundle_identity)
TESSL_SKILL_PROFILE = ProviderProfile("tessl-registry-skill-html-v1", _tessl_skill_identity)
POLYSKILL_PROFILE = ProviderProfile("polyskill-html-route-v1", _polyskill_identity)
# Compatibility name for the public, anonymous SkillHub contract.
SKILLHUB_PROFILE = SKILLHUB_PUBLIC_PROFILE
GITHUB_SKILL_PROFILE = ProviderProfile("github-owner-repository-path-v1", _github_identity)


def _github_repository_identity(response: AnonymousResponse, expected: object | None) -> bool | None:
    values = _expected_object(expected)
    if values is None or not values.get("repository") or not values.get("url"):
        return None
    parsed = urlparse(values["url"])
    parts = [unquote(part) for part in parsed.path.strip("/").split("/") if part]
    if (parsed.scheme != "https" or parsed.hostname != "github.com" or parts != values["repository"].split("/")):
        return False
    parser = _MetadataParser()
    try:
        parser.feed(response.body.decode("utf-8", "strict"))
    except (UnicodeDecodeError, ValueError):
        return None
    observed = parser.meta.get("octolytics-dimension-repository_nwo")
    return None if observed is None else observed.casefold() == values["repository"].casefold()


GITHUB_REPOSITORY_PROFILE = ProviderProfile("github-owner-repository-v1", _github_repository_identity)

_PROFILES = {
    "skills-sh": SKILLS_SH_PROFILE,
    "skillsmp": SKILLSMP_PROFILE,
    "skillhub-public": SKILLHUB_PUBLIC_PROFILE,
    "skillhub-pro": SKILLHUB_PRO_PROFILE,
    "github-repo": GITHUB_SKILL_PROFILE,
    "github-code-search": GITHUB_SKILL_PROFILE,
    "github-repository-page": GITHUB_REPOSITORY_PROFILE,
    "clawhub": CLAWHUB_PROFILE,
    "tessl": TESSL_BUNDLE_PROFILE,
    "polyskill": POLYSKILL_PROFILE,
}


@dataclass(frozen=True)
class _ReviewedRoute:
    host: str
    role: str
    prefix: tuple[str, ...]
    exact_segments: int | None = None


_REVIEWED_ROUTES = {
    "skills-sh": _ReviewedRoute("skills.sh", "listing", ()),
    "skillsmp": _ReviewedRoute("skillsmp.com", "listing", ("creators",), 4),
    "skillhub-public": _ReviewedRoute("skills.palebluedot.live", "listing", ("skill",)),
    "skillhub-pro": _ReviewedRoute("www.skillhub.club", "listing", ("skill",)),
    "github-repo": _ReviewedRoute("github.com", "skill_destination", ()),
    "github-code-search": _ReviewedRoute("github.com", "skill_destination", ()),
    "github-repository-page": _ReviewedRoute("github.com", "repository", (), 2),
    "clawhub": _ReviewedRoute("clawhub.ai", "listing", (), 3),
    "tessl": _ReviewedRoute("tessl.io", "bundle_listing", ("registry",), 4),
    "tessl-skill": _ReviewedRoute("tessl.io", "listing", ("registry", "skills", "github"), 6),
    "polyskill": _ReviewedRoute("polyskill.ai", "listing", ("skill",)),
}


def _safe_path_segments(path: str) -> list[str] | None:
    if not path.startswith("/") or path == "/":
        return None
    raw_segments = path.split("/")[1:]
    if not raw_segments or any(not raw for raw in raw_segments):
        return None
    segments: list[str] = []
    for raw in raw_segments:
        decoded = unquote(raw)
        if (not decoded or decoded in {".", ".."} or "/" in decoded or "\\" in decoded or len(decoded) > 300
                or any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in decoded)):
            return None
        # Invalid percent escapes and literal percent are not part of reviewed
        # path grammar, and must not be normalized into a new authority.
        if "%" in decoded:
            return None
        segments.append(decoded)
    return segments


def _matches_reviewed_route(adapter: str, role: str, url: str | None) -> bool:
    rule = _REVIEWED_ROUTES.get(adapter)
    if rule is None or role != rule.role or not isinstance(url, str) or len(url) > 4000:
        return False
    try:
        parsed = urlparse(url)
        port = parsed.port
    except ValueError:
        return False
    allowed_hosts = {rule.host}
    if adapter == "skills-sh":
        allowed_hosts.add("www.skills.sh")
    if (parsed.scheme != "https" or parsed.hostname not in allowed_hosts or port not in {None, 443}
            or parsed.username or parsed.password or parsed.query or parsed.params or parsed.fragment):
        return False
    if adapter in {"github-repo", "github-code-search"}:
        # The ref is one URL segment even when it contains a percent-encoded
        # slash. Decode it independently from the directory path so a valid
        # ``feature/name`` ref is not mistaken for traversal.
        raw_segments = parsed.path.strip("/").split("/")
        if len(raw_segments) < 4 or raw_segments[2] != "tree":
            return False
        owner, project, ref = (unquote(raw_segments[0]), unquote(raw_segments[1]), unquote(raw_segments[3]))
        skill_path = "/".join(unquote(part) for part in raw_segments[4:]) or "."
        return (
            parse_github_repository(f"{owner}/{project}") == f"{owner}/{project}"
            and safe_install_reference(ref) == ref
            and safe_skill_path(skill_path) == skill_path
        )
    segments = _safe_path_segments(parsed.path)
    if segments is None:
        return False
    if adapter == "skills-sh":
        return True
    if adapter in {"skillsmp", "skillhub-public", "skillhub-pro"}:
        return tuple(segments[:len(rule.prefix)]) == rule.prefix and (rule.exact_segments is None or len(segments) == rule.exact_segments)
    if adapter == "polyskill":
        return tuple(segments[:1]) == rule.prefix and len(segments) >= 3
    if adapter == "clawhub":
        return len(segments) == 3 and segments[1] == "skills"
    if adapter in {"tessl", "tessl-skill"}:
        return tuple(segments[:len(rule.prefix)]) == rule.prefix and len(segments) == rule.exact_segments
    if adapter == "github-repository-page":
        return len(segments) == 2
    return False


_PROFILE_ADAPTERS = {profile.name: adapter for adapter, profile in _PROFILES.items()}
_PROFILE_ADAPTERS[TESSL_SKILL_PROFILE.name] = "tessl-skill"


def _matches_profile_route(profile: ProviderProfile, role: str, url: str) -> bool:
    adapter = _PROFILE_ADAPTERS.get(profile.name)
    # Test-only injected profiles have no authority rule; their transport is
    # already explicit and they cannot be selected from remote candidate data.
    return adapter is None or _matches_reviewed_route(adapter, role, url)


def reviewed_profile(adapter: str) -> ProviderProfile | None:
    """Return only a frozen adapter proof contract; unknown adapters get none."""
    return _PROFILES.get(adapter)


def _derived_expected_identity(adapter: str, role: str, url: str | None,
                               expected_identity: object | None) -> object | None:
    """Fill only route-encoded identity omitted by legacy public adapters."""
    values = dict(_expected_object(expected_identity) or {})
    if not isinstance(url, str):
        return expected_identity
    parsed = urlparse(url)
    segments = [unquote(part) for part in parsed.path.split("/") if part]
    if adapter in {"skills-sh", "skillsmp", "skillhub-public", "skillhub-pro"}:
        values.setdefault("url", url)
    elif adapter == "clawhub" and len(segments) == 3 and segments[1] == "skills":
        values.setdefault("owner", segments[0])
        values.setdefault("slug", segments[2])
    elif adapter == "tessl" and role == "bundle_listing" and len(segments) == 4 and segments[0] == "registry":
        values.setdefault("package", "/".join(segments[1:3]))
        values.setdefault("version", segments[3])
        values.setdefault("url", url)
    elif (adapter == "tessl" and role == "listing" and len(segments) == 6
          and segments[:3] == ["registry", "skills", "github"]):
        values.setdefault("repository", "/".join(segments[3:5]))
        values.setdefault("name", segments[5])
        values.setdefault("url", url)
    elif adapter == "tessl" and role == "repository" and parsed.hostname == "github.com" and len(segments) == 2:
        values.setdefault("repository", "/".join(segments))
        values.setdefault("url", url)
    elif adapter == "polyskill" and role == "listing" and len(segments) >= 3 and segments[0] == "skill":
        values.setdefault("name", "/".join(segments[1:]))
        values.setdefault("url", url)
    return values or expected_identity


def reviewed_destination(candidate_id: str, *, role: str, url: str | None, adapter: str,
                         expected_identity: object | None) -> Destination:
    """Federation-facing constructor for a candidate's reviewed listing proof."""
    expected = _derived_expected_identity(adapter, role, url, expected_identity)
    # Tessl individual skills preserve an exact source-provided GitHub
    # repository.  Its public proof is GitHub's repository metadata, not an
    # unvalidated Tessl API response.  Bundles use the Tessl registry page.
    if adapter == "tessl" and role == "repository" and isinstance(url, str):
        profile = GITHUB_REPOSITORY_PROFILE
        if not _matches_reviewed_route("github-repository-page", role, url):
            profile = None
    elif adapter == "tessl" and role == "listing":
        profile = TESSL_SKILL_PROFILE
        if not _matches_reviewed_route("tessl-skill", role, url):
            profile = None
    else:
        profile = reviewed_profile(adapter)
    tessl_special = adapter == "tessl" and role in {"repository", "listing"}
    if profile is None or not tessl_special and not _matches_reviewed_route(adapter, role, url):
        profile = None
    return Destination(candidate_id, role, url, expected, profile)


def skillhub_destination(candidate_id: str, *, base_url: str, native_id: str,
                         expected_identity: object | None = None, adapter: str = "skillhub-public") -> Destination:
    """Construct only the documented singular ``/skill/`` SkillHub route."""
    parsed = urlparse(base_url)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.port not in {None, 443}
            or parsed.path not in {"", "/"} or parsed.query or parsed.fragment or adapter not in {"skillhub-public", "skillhub-pro"}):
        raise ValueError("SkillHub base URL must be credential-free HTTPS")
    segments = native_id.strip("/").split("/")
    if not segments or any(not segment or segment in {".", ".."} or "\\" in segment for segment in segments):
        raise ValueError("SkillHub identity has unsafe path segments")
    url = base_url.rstrip("/") + "/skill/" + "/".join(quote(segment, safe="") for segment in segments)
    destination = reviewed_destination(candidate_id, role="listing", url=url, adapter=adapter,
                                       expected_identity=expected_identity or {"id": native_id})
    if destination.profile is None:
        raise ValueError("SkillHub base URL is not a reviewed public origin")
    return destination


def github_skill_destination(candidate_id: str, *, repository: str, ref: str, skill_path: str) -> Destination:
    """Construct a GitHub tree URL and require exact owner/repository/ref/path proof."""
    pieces = repository.split("/")
    path_parts = [] if skill_path == "." else skill_path.split("/")
    if (parse_github_repository(repository) != repository or safe_install_reference(ref) != ref
            or safe_skill_path(skill_path) != skill_path or len(pieces) != 2
            or any(not part or part in {".", ".."} for part in path_parts)):
        raise ValueError("GitHub destination identity is invalid")
    url = "https://github.com/" + "/".join(quote(part, safe="") for part in pieces)
    url += "/tree/" + quote(ref, safe="")
    if path_parts:
        url += "/" + "/".join(quote(part, safe="") for part in path_parts)
    expected = {"repository": repository, "ref": ref, "skill_path": skill_path, "url": url}
    return Destination(candidate_id, "skill_destination", url, expected, GITHUB_SKILL_PROFILE)


def github_repository_destination(candidate_id: str, *, repository: str) -> Destination:
    """Construct an exact public GitHub repository-page proof request."""
    pieces = repository.split("/")
    if parse_github_repository(repository) != repository or len(pieces) != 2:
        raise ValueError("GitHub repository identity is invalid")
    url = "https://github.com/" + "/".join(quote(piece, safe="") for piece in pieces)
    return Destination(candidate_id, "repository", url, {"repository": repository, "url": url}, GITHUB_REPOSITORY_PROFILE)


class Resolver(Protocol):
    def __call__(self, host: str, port: int) -> Iterable[str]: ...


class Transport(Protocol):
    def request(self, method: str, url: str, *, timeout: float, max_bytes: int,
                allowed_addresses: tuple[str, ...], headers: Mapping[str, str]) -> AnonymousResponse: ...


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """HTTPS connection pinned to one resolver-approved public address.

    ``HTTPSConnection`` still receives the original hostname, so TLS SNI and
    certificate hostname verification remain intact while socket connection is
    never delegated to ambient DNS or a proxy.
    """
    def __init__(self, host: str, port: int, addresses: tuple[str, ...], timeout: float,
                 deadline_at: float | None = None):
        super().__init__(host, port=port, timeout=timeout, context=ssl.create_default_context())
        self._addresses = addresses
        self._deadline_at = deadline_at
        self.peer_address: str | None = None

    def connect(self) -> None:
        last_error: OSError | None = None
        raw_socket = None
        expires_at = self._deadline_at if self._deadline_at is not None else time.monotonic() + float(self.timeout or 0)
        for address in self._addresses:
            try:
                remaining = expires_at - time.monotonic()
                if remaining <= 0:
                    break
                raw_socket = socket.create_connection((address, self.port), remaining)
                self.peer_address = str(raw_socket.getpeername()[0])
                break
            except OSError as exc:
                last_error = exc
        if raw_socket is None:
            raise last_error or OSError("no approved address could be connected")
        try:
            raw_socket.settimeout(_remaining_request_timeout(expires_at))
            self.sock = self._context.wrap_socket(raw_socket, server_hostname=self.host)
        except Exception:
            raw_socket.close()
            raise


def _remaining_request_timeout(expires_at: float) -> float:
    remaining = expires_at - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("anonymous destination request deadline elapsed")
    return remaining


def _set_socket_timeout(connection: Any, expires_at: float) -> None:
    """Use an absolute request deadline, not a renewed idle-read timeout."""
    timeout = _remaining_request_timeout(expires_at)
    sock = getattr(connection, "sock", None)
    if sock is not None and callable(getattr(sock, "settimeout", None)):
        sock.settimeout(timeout)


def _read_response_body(response: Any, connection: Any, *, max_bytes: int, expires_at: float) -> bytes:
    """Bound every response-body chunk and check the shared absolute timeout."""
    body = bytearray()
    reader = getattr(response, "read1", None)
    if not callable(reader):
        _set_socket_timeout(connection, expires_at)
        value = response.read(max_bytes + 1)
        if not isinstance(value, bytes):
            raise ValueError("anonymous destination response body is invalid")
        if len(value) > max_bytes:
            raise ValueError("anonymous destination response exceeded byte bound")
        return value
    while True:
        _set_socket_timeout(connection, expires_at)
        chunk = reader(min(64 * 1024, max_bytes + 1 - len(body)))
        if not chunk:
            return bytes(body)
        if not isinstance(chunk, bytes):
            raise ValueError("anonymous destination response body is invalid")
        body.extend(chunk)
        if len(body) > max_bytes:
            raise ValueError("anonymous destination response exceeded byte bound")


def _pinned_https_get(url: str, addresses: tuple[str, ...], headers: Mapping[str, str], max_bytes: int,
                      expires_at: float) -> AnonymousResponse:
    """Child-safe direct GET. Parent supervision bounds hostile header trickles."""
    parsed = urlparse(url)
    connection = _PinnedHTTPSConnection(
        parsed.hostname or "", 443, addresses, _remaining_request_timeout(expires_at), deadline_at=expires_at,
    )
    try:
        target = parsed.path or "/"
        if parsed.query:
            target += "?" + parsed.query
        connection.request("GET", target, headers=dict(headers))
        _set_socket_timeout(connection, expires_at)
        response = connection.getresponse()
        body = _read_response_body(response, connection, max_bytes=max_bytes, expires_at=expires_at)
        peer = connection.peer_address
        if peer is None and getattr(connection, "sock", None) is not None:
            peer = connection.sock.getpeername()[0]
        actual = str(ipaddress.ip_address(peer))
        if actual not in addresses:
            raise ValueError("anonymous destination connected outside approved addresses")
        response_headers = {
            str(key).lower(): str(value)[:4000]
            for key, value in list(response.headers.items())[:100]
            if isinstance(key, str) and isinstance(value, str)
        }
        return AnonymousResponse(int(response.status), response_headers, body, actual)
    finally:
        connection.close()


def _https_child(url: str, addresses: tuple[str, ...], headers: dict[str, str], max_bytes: int,
                 expires_at: float, output: Any) -> None:
    """Spawn target for a whole-request watchdog; it emits only bounded data."""
    try:
        response = _pinned_https_get(url, addresses, headers, max_bytes, expires_at)
        output.send(("ok", response.status, dict(response.headers), response.body, response.connection_address))
    except (OSError, TimeoutError, ValueError, http.client.HTTPException, ssl.SSLError) as exc:
        output.send(("error", type(exc).__name__, str(exc)[:300]))
    finally:
        output.close()


def _supervised_https_get(url: str, addresses: tuple[str, ...], headers: Mapping[str, str], *, timeout: float,
                          max_bytes: int, _worker: Callable[..., None] = _https_child,
                          _on_start: Callable[[Any], None] | None = None) -> AnonymousResponse:
    """Kill/reap a complete HTTPS request that exceeds its one fixed deadline."""
    expires_at = time.monotonic() + timeout
    context = multiprocessing.get_context("spawn")
    parent, child = context.Pipe(duplex=False)
    process = context.Process(
        target=_worker, args=(url, addresses, dict(headers), max_bytes, expires_at, child), daemon=True,
    )
    try:
        process.start()
        if _on_start is not None:
            _on_start(process)
        child.close()
        remaining = _remaining_request_timeout(expires_at)
        if not parent.poll(remaining):
            raise TimeoutError("anonymous destination request deadline elapsed")
        try:
            payload = parent.recv()
        except EOFError as exc:
            raise ValueError("anonymous destination request process failed") from exc
        if not isinstance(payload, tuple) or not payload:
            raise ValueError("anonymous destination request process returned invalid data")
        if payload[0] != "ok":
            raise ValueError(payload[2] if len(payload) >= 3 and isinstance(payload[2], str) else "anonymous destination request failed")
        if len(payload) != 5 or not isinstance(payload[1], int) or not isinstance(payload[2], dict) or not isinstance(payload[3], bytes):
            raise ValueError("anonymous destination request process returned invalid data")
        return AnonymousResponse(payload[1], payload[2], payload[3], payload[4] if isinstance(payload[4], str) else None)
    finally:
        parent.close()
        child.close()
        if process.is_alive():
            process.terminate()
            process.join(timeout=0.2)
            if process.is_alive() and callable(getattr(process, "kill", None)):
                process.kill()
                process.join(timeout=0.2)
        else:
            process.join(timeout=0.2)


class AnonymousPublicTransport:
    """Direct, HTTPS-only, credential-free transport for destination proof.

    It deliberately has no proxy discovery, cookie jar, authenticated client,
    redirect handler, or ambient request headers. Redirect authorization stays
    in ``validate_destination`` where every hop is re-resolved and budgeted.
    ``connection_factory`` exists solely for deterministic transport tests.
    """
    def __init__(self, connection_factory: Callable[[str, int, tuple[str, ...], float], Any] | None = None):
        self._supervise_requests = connection_factory is None
        self._connection_factory = connection_factory or _PinnedHTTPSConnection

    @staticmethod
    def _headers(headers: Mapping[str, str]) -> dict[str, str]:
        if not isinstance(headers, Mapping):
            raise ValueError("anonymous request headers are invalid")
        clean: dict[str, str] = {}
        for name, value in headers.items():
            if not isinstance(name, str) or not isinstance(value, str) or len(value) > 200:
                raise ValueError("anonymous request headers are invalid")
            if name.lower() != "accept" or any(ord(char) < 32 or ord(char) == 127 for char in name + value):
                raise ValueError("anonymous transport permits only a safe Accept header")
            clean["Accept"] = value
        return clean

    def request(self, method: str, url: str, *, timeout: float, max_bytes: int,
                allowed_addresses: tuple[str, ...], headers: Mapping[str, str]) -> AnonymousResponse:
        parsed = urlparse(url)
        if (method != "GET" or parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
                or parsed.fragment or parsed.port not in {None, 443}):
            raise ValueError("anonymous destination requests require credential-free HTTPS")
        if type(timeout) not in {int, float} or timeout <= 0 or type(max_bytes) is not int or not 1 <= max_bytes <= MAX_HTML_BYTES:
            raise ValueError("anonymous destination request bounds are invalid")
        addresses = tuple(sorted({str(ipaddress.ip_address(address)) for address in allowed_addresses
                                  if ipaddress.ip_address(address).is_global}))
        if not addresses:
            raise ValueError("anonymous destination has no approved public address")
        safe_headers = self._headers(headers)
        if self._supervise_requests:
            return _supervised_https_get(url, addresses, safe_headers, timeout=float(timeout), max_bytes=max_bytes)
        target = parsed.path or "/"
        if parsed.query:
            target += "?" + parsed.query
        expires_at = time.monotonic() + float(timeout)
        connection = self._connection_factory(parsed.hostname, 443, addresses, float(timeout))
        try:
            connection.request("GET", target, headers=safe_headers)
            _set_socket_timeout(connection, expires_at)
            response = connection.getresponse()
            body = _read_response_body(response, connection, max_bytes=max_bytes, expires_at=expires_at)
            peer = getattr(connection, "peer_address", None)
            if peer is None and getattr(connection, "sock", None) is not None:
                peer = connection.sock.getpeername()[0]
            try:
                actual = str(ipaddress.ip_address(peer))
            except ValueError as exc:
                raise ValueError("anonymous destination has no valid actual connection address") from exc
            if actual not in addresses:
                raise ValueError("anonymous destination connected outside approved addresses")
            return AnonymousResponse(int(response.status), {key.lower(): value for key, value in response.headers.items()},
                                     body, actual)
        finally:
            close = getattr(connection, "close", None)
            if callable(close):
                close()


def _origin(url: str) -> tuple[str, str, int]:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
        raise ValueError("destination must be a credential-free absolute HTTP(S) URL without fragment")
    return parsed.scheme.lower(), parsed.hostname.lower(), parsed.port or (443 if parsed.scheme == "https" else 80)


def _deadline_remaining(value: Deadline | _AbsoluteDeadline | float | None) -> float:
    if value is None:
        return 3.0
    if hasattr(value, "remaining"):
        return max(0.0, float(value.remaining()))
    return max(0.0, float(value) - time.monotonic())


def _dns_child(host: str, port: int, output: Any) -> None:
    """Child target is module-level so spawn works on macOS, Linux and Windows."""
    try:
        addresses = []
        for item in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM):
            address = item[4][0]
            if isinstance(address, str) and len(address) <= 64:
                addresses.append(address)
            if len(addresses) >= 64:
                break
        output.send(("ok", addresses))
    except (OSError, ValueError) as exc:
        output.send(("error", str(exc)[:300]))
    finally:
        output.close()


def _bounded_dns(host: str, port: int, deadline: Deadline | _AbsoluteDeadline | float | None, *,
                 _worker: Callable[[str, int, Any], None] = _dns_child,
                 _on_start: Callable[[Any], None] | None = None) -> tuple[str, ...]:
    """Run uninterruptible stdlib DNS in a reaped child, never a lingering thread."""
    timeout = min(3.0, _deadline_remaining(deadline))
    if timeout <= 0:
        raise ValueError("destination resolution deadline elapsed")
    context = multiprocessing.get_context("spawn")
    parent, child = context.Pipe(duplex=False)
    process = context.Process(target=_worker, args=(host, port, child), daemon=True)
    try:
        process.start()
        if _on_start is not None:
            _on_start(process)
        child.close()
        if not parent.poll(timeout):
            process.terminate()
            process.join(timeout=0.2)
            if process.is_alive():
                kill = getattr(process, "kill", None)
                if callable(kill):
                    kill()
                process.join(timeout=0.2)
            raise ValueError("destination resolution deadline elapsed")
        try:
            status, payload = parent.recv()
        except EOFError as exc:
            raise ValueError("destination resolution process failed") from exc
        process.join(timeout=0.2)
        if process.is_alive():
            process.terminate()
            process.join(timeout=0.2)
        if status != "ok" or not isinstance(payload, list):
            raise ValueError("destination resolution failed")
        return tuple(item for item in payload if isinstance(item, str))
    finally:
        parent.close()
        child.close()
        if process.is_alive():
            process.terminate()
            process.join(timeout=0.2)
            if process.is_alive() and callable(getattr(process, "kill", None)):
                process.kill()
                process.join(timeout=0.2)


def _public_addresses(resolver: Resolver, host: str, port: int,
                      deadline: Deadline | _AbsoluteDeadline | float | None = None) -> tuple[str, ...]:
    values: list[str] = []
    if _deadline_remaining(deadline) <= 0:
        raise ValueError("destination resolution deadline elapsed")
    if resolver is public_resolver:
        raw_values = public_resolver(host, port, deadline=deadline)
    else:
        raw_values = resolver(host, port)
    for raw in raw_values:
        try:
            address = ipaddress.ip_address(raw)
        except ValueError:
            continue
        if not address.is_global:
            continue
        values.append(str(address))
    if not values:
        raise ValueError("destination resolves only to non-public addresses")
    return tuple(sorted(set(values)))


def public_resolver(host: str, port: int, *, deadline: Deadline | _AbsoluteDeadline | float | None = None) -> tuple[str, ...]:
    """Resolve only public addresses in a bounded, reaped stdlib child process."""
    if not isinstance(host, str) or not host or type(port) is not int or not 1 <= port <= 65535:
        raise ValueError("destination resolver input is invalid")
    addresses = _bounded_dns(host, port, deadline)
    return _public_addresses(lambda _host, _port: addresses, host, port)


def _allowed_redirect(previous: str, target: str) -> bool:
    old, new = _origin(previous), _origin(target)
    if old[0] != "https" or new[0] != "https":
        return False
    if old == new:
        return True
    return REVIEWED_REDIRECTS.get(old) == new


class _AbsoluteDeadline:
    """Compatibility adapter for isolated callers using a monotonic cutoff."""
    def __init__(self, cutoff: float): self.cutoff = cutoff
    def remaining(self, now: float | None = None) -> float:
        return max(0.0, self.cutoff - (time.monotonic() if now is None else now))


def _deadline(value: Deadline | float) -> Deadline | _AbsoluteDeadline:
    return value if isinstance(value, Deadline) else _AbsoluteDeadline(float(value))


def _checked_at() -> str:
    return datetime.now(timezone.utc).isoformat()


def _reserve(budget: Any, deadline: Deadline | _AbsoluteDeadline, *, max_bytes: int = MAX_HTML_BYTES,
             github_api: bool = False) -> tuple[bool, Any | None]:
    """Consume one validation request without refunding its bounded allowance.

    ``RequestBudget`` needs the validation sub-budget consumed explicitly.  A
    narrow fake budget used by contract tests may only expose ``reserve``;
    retain that surface while production consumes its related counters
    atomically.
    """
    leases: list[Any] = []
    try:
        if isinstance(budget, RequestBudget):
            reservations: tuple[tuple[str, int], ...] = (
                ("validation", 1), ("request", 1), ("bytes", max_bytes),
            ) + (("github_api", 1),) if github_api else (
                ("validation", 1), ("request", 1), ("bytes", max_bytes),
            )
            return True, [budget.reserve_many(reservations, deadline)]
        for kind, amount in (("request", 1), ("bytes", max_bytes)):
            lease = budget.reserve(kind, amount, deadline)
            status = getattr(lease, "status", None)
            if lease is None or status == "budget_exhausted" or lease == "budget_exhausted":
                _release(leases)
                return False, None
            leases.append(lease)
        if github_api:
            lease = budget.reserve("github_api", 1, deadline)
            status = getattr(lease, "status", None)
            if lease is None or status == "budget_exhausted" or lease == "budget_exhausted":
                _release(leases)
                return False, None
            leases.append(lease)
    except RuntimeLimitError:
        _release(leases)
        return False, None
    return True, leases


def _acquire(pool: Any, origin: tuple[str, str, int], phase: str, deadline: Deadline | _AbsoluteDeadline) -> tuple[bool, Any | None]:
    permit_phase = "provisional_validation" if phase == "provisional" else "final_validation"
    try:
        lease = pool.acquire(f"{origin[0]}://{origin[1]}:{origin[2]}", permit_phase, deadline)
    except RuntimeLimitError:
        return False, None
    status = getattr(lease, "status", None)
    if lease is None or status in {"budget_exhausted", "deadline_exceeded", "unavailable"} or lease in ("budget_exhausted", "deadline_exceeded"):
        return False, None
    return True, lease


def _release(lease: Any) -> None:
    if isinstance(lease, (list, tuple)):
        for item in lease:
            _release(item)
        return
    release = getattr(lease, "release", None)
    if callable(release):
        release()


def validate_destination(destination: Destination, *, transport: Transport, resolver: Resolver,
                         budget: Any, permits: Any, deadline: Deadline | float, phase: str = "final",
                         now: Callable[[], str] = _checked_at) -> LinkProof:
    """Return a disjoint proof status, never trusting generic page text."""
    if destination.role not in {
        "listing", "bundle_listing", "source_page", "repository", "skill_destination", "unavailable",
    }:
        return LinkProof(destination.role, destination.url, "not_checked", detail="unsupported destination role")
    if not destination.url or destination.role == "unavailable":
        return LinkProof(destination.role, destination.url, "not_checked", detail="no inspectable public destination")
    if destination.profile is None:
        return LinkProof(destination.role, destination.url, "not_checked", detail="no reviewed provider proof contract")
    try:
        resolved_deadline = _deadline(deadline)
        current = destination.url
        chain = [current]
        profile = destination.profile
        for redirect_count in range(MAX_REDIRECTS + 1):
            if profile is not None and not _matches_profile_route(profile, destination.role, current):
                return LinkProof(destination.role, destination.url, "not_checked", final_url=current,
                                 redirect_chain=list(chain), detail="destination is outside the reviewed provider route")
            origin = _origin(current)
            if origin[0] != "https":
                return LinkProof(destination.role, destination.url, "not_checked", final_url=current,
                                 redirect_chain=list(chain), detail="anonymous validation permits HTTPS destinations only")
            if resolved_deadline.remaining() <= 0:
                return LinkProof(destination.role, destination.url, "not_checked", final_url=current,
                                 redirect_chain=list(chain), detail="validation deadline exhausted before resolution")
            addresses = _public_addresses(resolver, origin[1], origin[2], resolved_deadline)
            reserved, request_lease = _reserve(budget, resolved_deadline)
            if not reserved:
                return LinkProof(destination.role, destination.url, "not_checked", final_url=current,
                                 redirect_chain=list(chain), detail="validation request budget exhausted")
            permitted, permit_lease = _acquire(permits, origin, phase, resolved_deadline)
            if not permitted:
                _release(request_lease)
                return LinkProof(destination.role, destination.url, "not_checked", final_url=current,
                                 redirect_chain=list(chain), detail="validation permit unavailable")
            try:
                remaining = resolved_deadline.remaining()
                if remaining <= 0:
                    return LinkProof(destination.role, destination.url, "not_checked", final_url=current,
                                     redirect_chain=list(chain), detail="validation deadline exhausted")
                response = transport.request("GET", current, timeout=min(3.0, remaining), max_bytes=MAX_HTML_BYTES,
                                             allowed_addresses=addresses, headers={"Accept": "text/html,application/xhtml+xml"})
            finally:
                _release(permit_lease)
                _release(request_lease)
            if response.connection_address not in addresses:
                return LinkProof(destination.role, destination.url, "inconclusive", http_status=response.status,
                                 final_url=current, redirect_chain=list(chain), detail="actual connection address was not approved")
            if response.status in {301, 302, 303, 307, 308}:
                if redirect_count >= MAX_REDIRECTS:
                    return LinkProof(destination.role, destination.url, "inconclusive", http_status=response.status,
                                     final_url=current, redirect_chain=list(chain), detail="redirect limit exceeded")
                location = response.headers.get("location") or response.headers.get("Location")
                if not location:
                    return LinkProof(destination.role, destination.url, "inconclusive", http_status=response.status,
                                     final_url=current, redirect_chain=list(chain), detail="redirect omitted location")
                target = urljoin(current, location)
                if not _allowed_redirect(current, target):
                    return LinkProof(destination.role, destination.url, "unavailable", http_status=response.status,
                                     final_url=current, redirect_chain=list(chain), detail="redirect is not a reviewed destination pair")
                current = target
                chain.append(current)
                continue
            if response.status in {404, 410}:
                return LinkProof(destination.role, destination.url, "unavailable", checked_at=now(), http_status=response.status,
                                 final_url=current, redirect_chain=list(chain), detail="destination confirmed missing")
            if response.status in {401, 403, 429} or response.status >= 500:
                return LinkProof(destination.role, destination.url, "inconclusive", checked_at=now(), http_status=response.status,
                                 final_url=current, redirect_chain=list(chain), detail="destination access blocked or transient")
            if response.status < 200 or response.status >= 300:
                return LinkProof(destination.role, destination.url, "unavailable", checked_at=now(), http_status=response.status,
                                 final_url=current, redirect_chain=list(chain), detail="destination rejected request")
            if profile is None:
                return LinkProof(destination.role, destination.url, "inconclusive", checked_at=now(), http_status=response.status,
                                 final_url=current, redirect_chain=list(chain), detail="provider identity proof is unavailable")
            if profile.soft_error_check and profile.soft_error_check(response):
                return LinkProof(destination.role, destination.url, "unavailable", checked_at=now(), http_status=response.status,
                                 final_url=current, redirect_chain=list(chain), detail=f"{profile.name} reviewed soft-error evidence")
            identity = profile.identity_check(response, destination.expected_identity)
            if identity is True:
                return LinkProof(destination.role, destination.url, "eligible", method="anonymous_public_get",
                                 checked_at=now(), http_status=response.status,
                                 final_url=current, redirect_chain=list(chain), identity_basis=profile.name)
            if identity is False:
                return LinkProof(destination.role, destination.url, "unavailable", checked_at=now(), http_status=response.status,
                                 final_url=current, redirect_chain=list(chain), detail=f"{profile.name} identity mismatch")
            return LinkProof(destination.role, destination.url, "inconclusive", checked_at=now(), http_status=response.status,
                             final_url=current, redirect_chain=list(chain), detail=f"{profile.name} identity evidence incomplete")
        raise AssertionError("bounded redirect loop exited unexpectedly")
    except (OSError, TimeoutError, ValueError, http.client.HTTPException, ssl.SSLError) as exc:
        return LinkProof(destination.role, destination.url, "inconclusive", final_url=destination.url, detail=str(exc))


def _target_reported(value: Mapping[str, Any] | None) -> tuple[dict[str, Any], str | None, str | None, str | None, bool, bool]:
    """Normalize only a reported GitHub target; never infer a new location."""
    source = value if isinstance(value, Mapping) else {}
    repository = parse_github_repository(source.get("repository"))
    ref = safe_install_reference(source.get("ref"))
    path = safe_skill_path(source.get("skill_path"))
    name = source.get("name")
    if (not isinstance(name, str) or not name.strip() or len(name) > 300
            or any(ord(character) < 32 or ord(character) == 127 for character in name)):
        name = None
    # A reviewed occurrence must say that its path is exact before a stale ref
    # can be repaired.  An explicit source/user pin is never silently moved.
    path_is_exact = bool(source.get("path_is_exact"))
    ref_pinned = bool(source.get("ref_pinned", ref not in {None, "HEAD"}))
    reported: dict[str, Any] = {}
    if repository:
        reported["repository"] = repository
    if ref:
        reported["ref"] = ref
    if path:
        reported["skill_path"] = path
    if name:
        reported["name"] = name
    reported["path_is_exact"] = path_is_exact
    reported["ref_pinned"] = ref_pinned
    return reported, repository, ref, path, path_is_exact, ref_pinned


def _target_proof(*, status: str, reported: Mapping[str, Any], detail: str, now: Callable[[], str],
                  resolved: Mapping[str, Any] | None = None, content_sha256: str | None = None,
                  actual_name: str | None = None, url: str | None = None) -> dict[str, Any]:
    resolved_value = dict(resolved or {})
    if actual_name:
        resolved_value["actual_name"] = actual_name
    value: dict[str, Any] = {
        "kind": "github", "status": status, "reported": dict(reported),
        "resolved": resolved_value, "checked_at": now(), "detail": detail,
    }
    if resolved_value:
        value["ref"] = resolved_value.get("ref")
        value["skill_path"] = resolved_value.get("skill_path")
    if actual_name:
        value["actual_name"] = actual_name
    if content_sha256:
        value["content_sha256"] = content_sha256
    if url:
        # This is the exact anonymously fetched content destination. A browser
        # blob/tree URL is separate, unchecked metadata and cannot inherit it.
        value.update(url=url, method="anonymous_exact_skill_md_get", identity_basis="github-exact-skill-md-v1")
    return value


def _github_urls(repository: str, ref: str, skill_path: str) -> tuple[str, str]:
    owner, project = repository.split("/", 1)
    parts = [] if skill_path == "." else skill_path.split("/")
    quoted_path = "/".join(quote(part, safe="") for part in (*parts, "SKILL.md"))
    quoted_directory = "/".join(quote(part, safe="") for part in parts)
    quoted_ref = quote(ref, safe="")
    raw = f"https://raw.githubusercontent.com/{quote(owner, safe='')}/{quote(project, safe='')}/{quoted_ref}/{quoted_path}"
    browse = f"https://github.com/{quote(owner, safe='')}/{quote(project, safe='')}/tree/{quoted_ref}"
    if quoted_directory:
        browse += "/" + quoted_directory
    return raw, browse


def _target_get(url: str, *, transport: Transport, resolver: Resolver, budget: Any, permits: Any,
                deadline: Deadline | _AbsoluteDeadline, max_bytes: int, github_api: bool = False,
                phase: str = "final") -> tuple[AnonymousResponse | None, str | None]:
    """One direct, pinned GET for a hard-coded GitHub target endpoint."""
    try:
        origin = _origin(url)
        if origin[0] != "https":
            return None, "target proof requires HTTPS"
        if deadline.remaining() <= 0:
            return None, "target validation deadline exhausted before resolution"
        addresses = _public_addresses(resolver, origin[1], origin[2], deadline)
        reserved, request_lease = _reserve(budget, deadline, max_bytes=max_bytes, github_api=github_api)
        if not reserved:
            return None, "target validation budget exhausted"
        permitted, permit_lease = _acquire(permits, origin, phase, deadline)
        if not permitted:
            _release(request_lease)
            return None, "target validation permit unavailable"
        try:
            remaining = deadline.remaining()
            if remaining <= 0:
                return None, "target validation deadline exhausted"
            response = transport.request(
                "GET", url, timeout=min(3.0, remaining), max_bytes=max_bytes,
                allowed_addresses=addresses, headers={"Accept": "application/vnd.github+json" if github_api else "text/plain"},
            )
        finally:
            _release(permit_lease)
            _release(request_lease)
        if response.connection_address not in addresses:
            return None, "actual connection address was not approved"
        return response, None
    except (OSError, TimeoutError, ValueError, http.client.HTTPException, ssl.SSLError) as exc:
        return None, str(exc)


def _github_default_branch(repository: str, *, transport: Transport, resolver: Resolver, budget: Any,
                           permits: Any, deadline: Deadline | _AbsoluteDeadline,
                           cache: TargetResolutionCache | None, phase: str) -> tuple[str | None, str | None]:
    if cache is not None:
        leader, waiter, cached = cache.claim_default(repository)
        if cached is not None:
            return cached
        if not leader:
            if waiter is None or not waiter.wait(timeout=deadline.remaining()):
                return None, "authoritative default branch lookup is in flight"
            return cache.default(repository) or (None, "authoritative default branch lookup did not complete")
    url = "https://api.github.com/repos/" + "/".join(quote(piece, safe="") for piece in repository.split("/"))
    branch: str | None = None
    detail: str | None = None
    try:
        response, detail = _target_get(
            url, transport=transport, resolver=resolver, budget=budget, permits=permits,
            deadline=deadline, max_bytes=MAX_GITHUB_METADATA_BYTES, github_api=True, phase=phase,
        )
        if response is not None:
            if response.status == 404:
                detail = "repository default branch is missing"
            elif response.status in {401, 403, 429} or response.status >= 500:
                detail = "repository default branch is unavailable"
            elif not 200 <= response.status < 300:
                detail = "repository default branch request was rejected"
            else:
                document = _json_object(response)
                full_name = document.get("full_name") if document else None
                candidate = document.get("default_branch") if document else None
                candidate = safe_install_reference(candidate)
                if (not isinstance(full_name, str) or full_name.casefold() != repository.casefold()
                        or not candidate):
                    detail = "repository default branch identity evidence is incomplete"
                else:
                    branch, detail = candidate, None
    finally:
        if cache is not None:
            cache.remember_default(repository, branch, detail)
    return branch, detail


def _target_name(body: bytes, *, expected: str | None, skill_path: str, require_match: bool) -> tuple[str | None, str | None]:
    """Read bounded SKILL.md identity without executing its instructions."""
    try:
        text = body.decode("utf-8", "strict")
    except UnicodeDecodeError:
        return None, "SKILL.md is not valid UTF-8"
    if not text.strip():
        return None, "SKILL.md is empty"
    metadata = parse_frontmatter(text)
    actual = metadata.get("name") if isinstance(metadata, Mapping) else None
    if not isinstance(actual, str) or not actual.strip():
        return None, "SKILL.md has no frontmatter skill name"
    if require_match:
        if not expected or not actual or _normalise_identity(expected) != _normalise_identity(actual):
            return actual, "SKILL.md name does not match the reported skill identity"
    return actual, None


def resolve_github_target(candidate_id: str, reported_target: Mapping[str, Any] | None, *, transport: Transport,
                          resolver: Resolver, budget: Any, permits: Any, deadline: Deadline | float,
                          phase: str = "final", cache: TargetResolutionCache | None = None,
                          now: Callable[[], str] = _checked_at) -> dict[str, Any]:
    """Resolve one exact GitHub ``SKILL.md`` target under shared bounds.

    A known path is fetched only at its reported ref.  An explicit unpinned
    stale ref gets one authoritative-default-branch retry of *that same path*;
    a pathless occurrence gets exactly one root ``SKILL.md`` attempt at the
    authoritative default branch.  There is no branch guessing, archive fetch,
    repository crawl, credential use, or remote command interpretation.
    """
    if phase not in {"provisional", "final"}:
        raise ValueError("target validation phase must be provisional or final")
    reported, repository, ref, skill_path, path_is_exact, ref_pinned = _target_reported(reported_target)
    if not repository:
        return _target_proof(status="not_checked", reported=reported, detail="GitHub repository is unresolved", now=now)
    resolved_deadline = _deadline(deadline)
    expected_name = reported.get("name") if isinstance(reported.get("name"), str) else None
    if not expected_name:
        return _target_proof(status="not_checked", reported=reported,
                             detail="target lacks a reported skill identity", now=now)
    # Missing paths have a deliberately narrow root-only repair.  A path from a
    # listing is otherwise evidence, not permission to scan sibling folders.
    pathless = skill_path is None
    if pathless:
        if ref_pinned and ref not in {None, "HEAD"}:
            skill_path = "."
        else:
            default_ref, detail = _github_default_branch(
                repository, transport=transport, resolver=resolver, budget=budget, permits=permits,
                deadline=resolved_deadline, cache=cache, phase=phase,
            )
            if not default_ref:
                return _target_proof(status="inconclusive", reported=reported, detail=detail or "default branch unresolved", now=now)
            ref, skill_path = default_ref, "."
    elif ref in {None, "HEAD"}:
        default_ref, detail = _github_default_branch(
            repository, transport=transport, resolver=resolver, budget=budget, permits=permits,
            deadline=resolved_deadline, cache=cache, phase=phase,
        )
        if not default_ref:
            return _target_proof(status="inconclusive", reported=reported, detail=detail or "default branch unresolved", now=now)
        ref = default_ref
    if ref is None or skill_path is None:
        return _target_proof(status="inconclusive", reported=reported,
                             detail="exact ref or skill path is unresolved", now=now)
    key = (repository.casefold(), ref, skill_path, expected_name or "")
    if cache is not None:
        cached = cache.proof(key)
        if cached is not None:
            return cached

    def fetch(target_ref: str, target_path: str, *, require_match: bool) -> dict[str, Any]:
        raw_url, browse_url = _github_urls(repository, target_ref, target_path)
        response, detail = _target_get(
            raw_url, transport=transport, resolver=resolver, budget=budget, permits=permits,
            deadline=resolved_deadline, max_bytes=MAX_TARGET_BYTES, phase=phase,
        )
        resolved = {
            "repository": repository, "ref": target_ref, "skill_path": target_path,
            "url": raw_url, "browser_url": browse_url, "browse_url": browse_url,
        }
        if response is None:
            return _target_proof(status="inconclusive", reported=reported, resolved=resolved,
                                 detail=detail or "exact SKILL.md request failed", now=now)
        if response.status in {404, 410}:
            return _target_proof(status="unavailable", reported=reported, resolved=resolved,
                                 detail="exact SKILL.md is missing", now=now)
        if response.status in {401, 403, 429} or response.status >= 500:
            return _target_proof(status="inconclusive", reported=reported, resolved=resolved,
                                 detail="exact SKILL.md access is blocked or transient", now=now)
        if not 200 <= response.status < 300:
            return _target_proof(status="unavailable", reported=reported, resolved=resolved,
                                 detail="exact SKILL.md request was rejected", now=now)
        actual_name, name_error = _target_name(response.body, expected=expected_name,
                                                skill_path=target_path, require_match=require_match)
        if name_error:
            return _target_proof(status="unavailable", reported=reported, resolved=resolved,
                                 actual_name=actual_name, detail=name_error, now=now)
        return _target_proof(
            status="eligible", reported=reported, resolved=resolved, actual_name=actual_name,
            content_sha256=hashlib.sha256(response.body).hexdigest(), url=raw_url,
            detail="exact SKILL.md anonymously fetched and identity-checked", now=now,
        )

    proof = fetch(ref, skill_path, require_match=pathless or bool(expected_name))
    # One and only one stale-ref repair. It is prohibited for pins and paths
    # that were not explicitly reported by a reviewed occurrence.
    if (proof["status"] == "unavailable" and proof.get("detail") == "exact SKILL.md is missing"
            and not pathless and path_is_exact and not ref_pinned):
        default_ref, detail = _github_default_branch(
            repository, transport=transport, resolver=resolver, budget=budget, permits=permits,
            deadline=resolved_deadline, cache=cache, phase=phase,
        )
        if default_ref and default_ref != ref:
            proof = fetch(default_ref, skill_path, require_match=bool(expected_name))
        elif not default_ref:
            proof = _target_proof(status="inconclusive", reported=reported, detail=detail or "default branch unresolved", now=now)
    if cache is not None:
        cache.remember_proof(key, proof)
    return proof


def target_proof_link(proof: Mapping[str, Any]) -> LinkProof:
    """Do not promote exact content evidence into a human navigation proof.

    The raw ``SKILL.md`` GET establishes install identity and content hashing.
    Its related GitHub tree destination must pass ``validate_destination`` as a
    separate anonymous browser-page request before it can be displayed.
    """
    if not isinstance(proof, Mapping) or proof.get("kind") != "github" or proof.get("status") != "eligible":
        return LinkProof("skill_destination", None, "not_checked", detail="exact target proof is unavailable")
    resolved = proof.get("resolved")
    if (not isinstance(proof.get("url"), str) or not isinstance(resolved, Mapping)
            or not all(isinstance(resolved.get(key), str) and resolved.get(key) for key in ("repository", "ref", "skill_path"))
            or proof.get("identity_basis") != "github-exact-skill-md-v1"):
        return LinkProof("skill_destination", None, "not_checked", detail="exact target proof is malformed")
    return LinkProof(
        "skill_destination", None, "not_checked", method=str(proof.get("method")), checked_at=proof.get("checked_at"),
        identity_basis="github-exact-skill-md-v1",
        detail="exact SKILL.md content proof is internal; GitHub tree requires separate validation",
    )


def validate_ranked(destinations: Iterable[Destination], *, phase: str, transport: Transport, resolver: Resolver,
                    budget: Any, permits: Any, deadline: Deadline | float, max_identities: int | None = None,
                    validate_one: Callable[[Destination], LinkProof] | None = None,
                    workers: int = 1) -> dict[str, LinkProof]:
    """Rank-order scheduler. Provisional work is bounded and final work replenishes."""
    if phase not in {"provisional", "final"}:
        raise ValueError("validation phase must be provisional or final")
    cap = min(3, max_identities or 3) if phase == "provisional" else (max_identities or 30)
    if type(workers) is not int or not 1 <= workers <= 4:
        raise ValueError("validation workers must be 1..4")
    grouped: dict[str, list[Destination]] = {}
    for destination in destinations:
        if destination.candidate_id not in grouped and len(grouped) >= cap:
            break
        grouped.setdefault(destination.candidate_id, []).append(destination)

    def check(group: list[Destination]) -> LinkProof:
        selected: LinkProof | None = None
        for destination in group:
            if selected is not None and selected.status == "eligible":
                break
            proof = validate_one(destination) if validate_one is not None else validate_destination(
                destination, transport=transport, resolver=resolver, budget=budget,
                permits=permits, deadline=deadline, phase=phase,
            )
            # A checked alternate can repair a missing/mismatched primary. An
            # inconclusive alternate never turns a known failure into eligibility.
            if selected is None or proof.status == "eligible" or (
                selected.status == "unavailable" and proof.status == "inconclusive"
            ):
                selected = proof
        return selected or LinkProof("unavailable", None, "not_checked", detail="no destination checked")

    identities = list(grouped)
    if workers == 1 or len(identities) < 2:
        return {identity: check(grouped[identity]) for identity in identities}
    with ThreadPoolExecutor(max_workers=min(workers, len(identities)), thread_name_prefix="skill-validation") as pool:
        futures = {identity: pool.submit(check, grouped[identity]) for identity in identities}
        return {identity: futures[identity].result() for identity in identities}
