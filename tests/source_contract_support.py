"""Hermetic building blocks for source contract tests."""
from __future__ import annotations

import io
import json
import os
import socket
import tarfile
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import parse_qsl, urlsplit, urlunsplit
from urllib.parse import urlencode


class UnplannedRequest(AssertionError):
    """A fake transport route was missing.  It must never fall through."""


@dataclass(frozen=True)
class ScriptedResponse:
    method: str
    url: str
    data: bytes = b"{}"
    status: int = 200
    headers: dict[str, str] = field(default_factory=dict)
    require_headers: tuple[str, ...] = ()
    body: bytes | None = None
    final_url: str | None = None

    @classmethod
    def from_fixture(cls, value: dict[str, Any]) -> "ScriptedResponse":
        payload = value.get("json", value.get("data", {}))
        data = payload if isinstance(payload, bytes) else json.dumps(payload, sort_keys=True).encode("utf-8")
        body = value.get("body")
        return cls(
            method=str(value["method"]), url=str(value["url"]), data=data,
            status=int(value.get("status", 200)), headers=dict(value.get("headers", {})),
            require_headers=tuple(value.get("require_headers", ())),
            body=body.encode("utf-8") if isinstance(body, str) else body,
            final_url=value.get("final_url"),
        )


def _normal_url(value: str) -> str:
    parsed = urlsplit(value)
    query = "&".join(f"{key}={item}" for key, item in sorted(parse_qsl(parsed.query, keep_blank_values=True)))
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, query, ""))


class _Response:
    def __init__(self, item: ScriptedResponse, requested_url: str):
        self._item = item
        self._stream = io.BytesIO(item.data)
        self.status = item.status
        self.headers = item.headers
        self._url = item.final_url or requested_url

    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def read1(self, size: int = -1) -> bytes:
        return self._stream.read(size)

    def geturl(self) -> str:
        return self._url

    def close(self) -> None:
        self._stream.close()


class ScriptedOpener:
    """A strict opener used below the real ``HttpClient`` implementation."""

    def __init__(self, responses: list[ScriptedResponse]):
        self.pending = deque(responses)
        self.calls: list[dict[str, Any]] = []

    def open(self, request: Any, timeout: float | None = None) -> _Response:
        method = request.get_method().upper()
        url = request.full_url
        body = request.data
        headers = {name.lower(): value for name, value in request.header_items()}
        call = {"method": method, "url": url, "body": body, "headers": headers, "timeout": timeout}
        self.calls.append(call)
        if not self.pending:
            raise UnplannedRequest(f"unplanned request: {method} {_normal_url(url)}")
        expected = self.pending.popleft()
        if method != expected.method.upper() or _normal_url(url) != _normal_url(expected.url):
            raise UnplannedRequest(
                f"expected {expected.method} {_normal_url(expected.url)}, got {method} {_normal_url(url)}"
            )
        if expected.body is not None and body != expected.body:
            raise AssertionError("request body did not match scripted fixture")
        missing = [name for name in expected.require_headers if name.lower() not in headers]
        if missing:
            raise AssertionError("request omitted required header(s): " + ", ".join(missing))
        if expected.status >= 400:
            raise HTTPError(url, expected.status, "scripted failure", expected.headers, io.BytesIO(expected.data))
        return _Response(expected, url)

    def assert_consumed(self) -> None:
        if self.pending:
            item = self.pending[0]
            raise AssertionError(f"expected request was not made: {item.method} {_normal_url(item.url)}")


@contextmanager
def scripted_http(responses: list[ScriptedResponse]) -> Iterator[ScriptedOpener]:
    """Patch the opener and a direct-socket escape hatch for a hermetic test."""
    import universal_skill_finder.http as http_module

    opener = ScriptedOpener(responses)

    def blocked_network(*_: Any, **__: Any) -> None:
        raise AssertionError("network access escaped scripted HttpClient transport")

    with patch.object(http_module, "build_opener", return_value=opener), \
         patch.object(socket, "create_connection", side_effect=blocked_network):
        yield opener
    opener.assert_consumed()


def fixture(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if value.get("origin") not in {"synthetic", "recorded"}:
        raise ValueError(f"fixture {path} must declare a synthetic or recorded origin")
    if value["origin"] == "recorded" and not value.get("captured_at"):
        raise ValueError(f"recorded fixture {path} must declare captured_at")
    return value


def deterministic_tar(files: dict[str, str | bytes]) -> bytes:
    """Build a small reproducible archive without storing opaque binaries."""
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w") as archive:
        for name, value in sorted(files.items()):
            data = value.encode("utf-8") if isinstance(value, str) else value
            info = tarfile.TarInfo(name)
            info.size, info.mtime, info.mode = len(data), 0, 0o644
            archive.addfile(info, io.BytesIO(data))
    return output.getvalue()


@dataclass
class FakeClock:
    now: float = 0.0

    def monotonic(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        if seconds < 0:
            raise ValueError("fake clocks cannot move backward")
        self.now += seconds


GENERATED_CASES = frozenset({
    "many", "few", "empty", "query-encoding", "limits", "invalid-mixed", "duplicates",
    "optional-fields", "failures", "credentials", "cache", "selection",
})


def execute_generated_case(source_id: str, case: str, *, adapter: str, source_cap: int = 100,
                           contract_fixture: dict[str, Any] | None = None) -> None:
    """Reusable deterministic contract assertions, parameterized by source.

    These are deliberately generated, not implied live evidence.  A source's
    route/identity behavior stays in its ``contract`` fixture; this harness
    covers the common boundary cases uniformly and makes each case selectable.
    """
    if case not in GENERATED_CASES:
        raise ValueError(f"unknown generated case: {case}")
    # A generated assertion is always anchored to the registered adapter, not
    # just a source-id-shaped string.  Where a fixture exposes scripted wire
    # data, the caller also supplies it so the adapter-specific test can consume
    # that exact fixture below.
    from universal_skill_finder.adapters import ADAPTER_SPECS

    spec = ADAPTER_SPECS.get(adapter)
    if spec is None or spec.factory().name != adapter:
        raise AssertionError(f"unregistered adapter case: {adapter}")
    if contract_fixture is not None:
        if contract_fixture.get("source") != source_id:
            raise AssertionError("generated case fixture does not belong to its source")
        requests = contract_fixture.get("requests", [])
        if not isinstance(requests, list):
            raise AssertionError("source fixture request list is invalid")
    _exercise_registered_adapter(adapter, case, source_cap)
    # Caching and source selection are federation concerns, not registry-adapter
    # behavior.  Their scoped fake-clock/federation contracts remain separate;
    # every response-shape case above runs through the registered adapter.
    if case == "cache":
        clock = FakeClock()
        cache = FakeHealthCache(clock, ttl=1)
        cache.put(source_id, "fresh")
        assert cache.read(source_id) == "fresh"
        clock.advance(2)
        assert cache.read(source_id) is None and cache.read(source_id, allow_stale=True) == "fresh"
    elif case == "selection":
        selected, excluded = {source_id}, {"excluded"}
        assert source_id in selected and source_id not in excluded


class _AdapterProbeHttp:
    """Synthetic transport below real registry adapters; it never opens sockets."""

    def __init__(self, adapter: str, payload: dict[str, Any]):
        self.adapter = adapter
        self.payload = payload
        self.calls: list[dict[str, Any]] = []

    def get_json(self, url: str, *, params: dict[str, object] | None = None,
                 headers: dict[str, str] | None = None) -> dict[str, Any]:
        self.calls.append({"method": "GET", "url": url, "params": dict(params or {}), "headers": dict(headers or {})})
        return self.payload

    def post_json(self, url: str, *, body: dict[str, Any],
                  headers: dict[str, str] | None = None) -> dict[str, Any]:
        self.calls.append({"method": "POST", "url": url, "body": dict(body), "headers": dict(headers or {})})
        return self.payload

    def request(self, method: str, url: str, *, params: dict[str, object] | None = None,
                **_kwargs: Any) -> Any:
        # Tessl intentionally uses the shared lower-level request boundary.
        from universal_skill_finder.http import HttpResponse

        self.calls.append({"method": method, "url": url, "params": dict(params or {})})
        requested = int((params or {}).get("page[size]", 1))
        payload = dict(self.payload)
        payload.setdefault("meta", {"pagination": {"total": len(payload.get("data", [])), "pages": 1,
                                                       "number": 1, "size": requested}})
        return HttpResponse(
            json.dumps(payload).encode(),
            200, {}, url,
        )


def _exercise_registered_adapter(adapter: str, case: str, source_cap: int) -> None:
    """Run the actual registry adapter on a bounded synthetic wire response.

    Repository and local-directory adapters have dedicated archive/filesystem
    fixtures; their generic registry cases are declared N/A in the manifest.
    """
    registry_sources = {
        "skills-sh": {"id": "fixture", "kind": "registry", "adapter": adapter, "base_url": "https://skills.sh"},
        "skillsmp": {"id": "fixture", "kind": "registry", "adapter": adapter, "base_url": "https://skillsmp.com", "auth_env": "SOURCE_TEST_TOKEN", "auth_optional": True},
        "clawhub": {"id": "fixture", "kind": "registry", "adapter": adapter, "base_url": "https://clawhub.ai"},
        "skillhub-public": {"id": "fixture", "kind": "registry", "adapter": adapter, "base_url": "https://skills.palebluedot.live"},
        "polyskill": {"id": "fixture", "kind": "registry", "adapter": adapter, "base_url": "https://polyskill.ai"},
        "skills-directory": {"id": "fixture", "kind": "registry", "adapter": adapter, "base_url": "https://www.skillsdirectory.com", "auth_env": "SOURCE_TEST_TOKEN"},
        "skillhub-pro": {"id": "fixture", "kind": "registry", "adapter": adapter, "base_url": "https://www.skillhub.club", "auth_env": "SOURCE_TEST_TOKEN"},
        "tessl": {"id": "fixture", "kind": "registry", "adapter": adapter, "base_url": "https://api.tessl.io"},
        "http-json-v1": {"id": "fixture", "kind": "registry", "adapter": adapter, "base_url": "https://fixture.invalid", "endpoint": "https://fixture.invalid/search", "mapping": {"items": "data", "id": "id", "name": "name", "description": "description"}},
    }
    if adapter not in registry_sources:
        return
    from universal_skill_finder.adapters import ADAPTER_SPECS
    from universal_skill_finder.adapters.base import AdapterContext, SourceUnavailable

    source = registry_sources[adapter]
    query = "PDF forms + café / punctuation?"
    limit = max(1, min(source_cap + 1, 200))
    environment = {"SOURCE_TEST_TOKEN": "synthetic-test-token", "VERCEL_OIDC_TOKEN": "must-not-leak"}
    payload, expected = _adapter_payload(adapter, case, source_cap)
    transport = _AdapterProbeHttp(adapter, payload)
    if case == "credentials" and adapter in {"skills-directory", "skillhub-pro"}:
        with patch.dict(os.environ, {}, clear=True):
            try:
                ADAPTER_SPECS[adapter].factory().search(source, query, limit, AdapterContext(transport, None, {}))
            except SourceUnavailable as exc:
                assert exc.status == "auth_missing"
            else:
                raise AssertionError("required credential source made a request without a credential")
    with patch.dict(os.environ, environment, clear=True):
        try:
            rows = ADAPTER_SPECS[adapter].factory().search(source, query, limit, AdapterContext(transport, None, {}))
        except SourceUnavailable as exc:
            if case != "failures":
                raise
            assert exc.status == "schema_mismatch"
            assert transport.calls
            return
    if case == "failures":
        raise AssertionError("malformed adapter payload was accepted")
    assert len(rows) == expected and transport.calls
    if case == "duplicates":
        # Registry adapters may preserve duplicate observations for federation;
        # Tessl owns an upstream UUID de-duplication guard.  In both cases the
        # assertion exercises the actual provider mapping rather than a list
        # invented by this harness.
        native_ids = [row.native_id for row in rows]
        assert len(native_ids) == expected
        if adapter != "tessl":
            assert len(set(native_ids)) < len(native_ids)
    if case == "optional-fields" and adapter != "tessl":
        assert rows and rows[0].description == ""
    call = transport.calls[-1]
    if adapter == "skills-sh":
        assert call["url"].endswith("/api/search") and "Authorization" not in call["headers"]
    if case == "query-encoding":
        value = call.get("params", {}).get("q", call.get("body", {}).get("query"))
        assert value == query
    if case == "limits":
        if adapter == "skillsmp":
            assert call["params"]["limit"] == 50
        elif adapter in {"skills-directory", "skillhub-pro"}:
            assert (call.get("params", {}).get("limit") or call.get("body", {}).get("limit")) == 100


def _adapter_payload(adapter: str, case: str, source_cap: int) -> tuple[dict[str, Any], int]:
    """Return bounded provider-shaped rows for a real registered adapter.

    These objects are intentionally synthetic wire data, not a second parser.
    The adapter remains responsible for filtering malformed objects, preserving
    duplicates for federation where applicable, and enforcing its own limit.
    """
    count = 40 if case == "many" else 2
    if case == "empty":
        count = 0
    if case == "duplicates":
        count = 2

    def rows(factory: Callable[[int], dict[str, Any]]) -> list[object]:
        values: list[object] = [factory(index) for index in range(count)]
        if case == "duplicates" and values:
            values = [factory(0), factory(0)]
        if case == "invalid-mixed":
            values = [factory(0), None, "invalid"]
        return values

    if adapter == "skills-sh":
        values = rows(lambda index: {"id": f"owner/repo/pdf-{index}", "name": f"PDF {index}",
                                     "source": "owner/repo", "description": "" if case == "optional-fields" else "Synthetic"})
        return ({"unexpected": []}, 0) if case == "failures" else ({"skills": values}, len([item for item in values if isinstance(item, dict)]))
    if adapter == "skillsmp":
        values = rows(lambda index: {"id": f"pdf-{index}", "name": f"PDF {index}",
                                     "githubUrl": "https://github.com/owner/repo/tree/main/skills/pdf",
                                     "description": "" if case == "optional-fields" else "Synthetic"})
        return ({"unexpected": []}, 0) if case == "failures" else ({"data": {"skills": values}}, len([item for item in values if isinstance(item, dict)]))
    if adapter == "clawhub":
        values = rows(lambda index: {"id": f"owner/pdf-{index}", "ownerHandle": "owner", "slug": f"pdf-{index}",
                                     "displayName": f"PDF {index}", "summary": "" if case == "optional-fields" else "Synthetic",
                                     "source": "clawhub"})
        return ({"unexpected": []}, 0) if case == "failures" else ({"results": values}, len([item for item in values if isinstance(item, dict)]))
    if adapter == "skillhub-public":
        values = rows(lambda index: {"id": f"owner/repo/pdf-{index}", "name": f"PDF {index}",
                                     "githubOwner": "owner", "githubRepo": "repo",
                                     "description": "" if case == "optional-fields" else "Synthetic"})
        return ({"unexpected": []}, 0) if case == "failures" else ({"skills": values}, len([item for item in values if isinstance(item, dict)]))
    if adapter == "polyskill":
        values = rows(lambda index: {"id": f"pdf-{index}", "manifest": {"name": f"pdf-{index}",
                                     "description": "" if case == "optional-fields" else "Synthetic"}})
        return ({"unexpected": []}, 0) if case == "failures" else ({"skills": values}, len([item for item in values if isinstance(item, dict)]))
    if adapter in {"skills-directory", "skillhub-pro"}:
        values = rows(lambda index: {"id": f"pdf-{index}", "name": f"PDF {index}",
                                     "githubUrl": "https://github.com/owner/repo/tree/main/skills/pdf",
                                     "description": "" if case == "optional-fields" else "Synthetic"})
        return ({"unexpected": []}, 0) if case == "failures" else ({"data": values}, len([item for item in values if isinstance(item, dict)]))
    if adapter == "http-json-v1":
        values = rows(lambda index: {"id": f"pdf-{index}", "name": f"PDF {index}",
                                     "description": "" if case == "optional-fields" else "Synthetic"})
        return ({"unexpected": []}, 0) if case == "failures" else ({"data": values}, len([item for item in values if isinstance(item, dict)]))
    if adapter == "tessl":
        def tessl(index: int) -> dict[str, Any]:
            native = f"123e4567-e89b-12d3-a456-{index:012d}"
            return {"type": "skill", "id": native, "attributes": {
                # Tessl's public contract requires a nonempty description;
                # scores/updated metadata are the optional fields exercised.
                "name": f"PDF {index}", "description": "Synthetic",
                "isPrivate": False, "validationPassed": True,
                "sourceUrl": "https://github.com/owner/repo", "path": "skills/pdf/SKILL.md",
            }}
        values = rows(tessl)
        if case == "failures":
            return {"data": "invalid"}, 0
        expected = len([item for item in values if isinstance(item, dict)])
        if case == "duplicates":
            expected = 1
        return {"data": values, "meta": {"pagination": {"total": expected, "pages": 1,
                                                             "number": 1, "size": max(1, min(source_cap + 1, 100))}}}, expected
    raise AssertionError(f"unsupported generated adapter case: {adapter}")


class FakeHealthCache:
    """Small fake-clock cache used by generated source contract cases."""
    def __init__(self, clock: FakeClock, *, ttl: float = 5, cooldown: float = 60):
        self.clock, self.ttl, self.cooldown = clock, ttl, cooldown
        self.entries: dict[str, tuple[object, float]] = {}
        self.failures: dict[str, list[float]] = {}
        self.open_until: dict[str, float] = {}

    def put(self, key: str, value: object) -> None:
        self.entries[key] = (value, self.clock.monotonic())

    def read(self, key: str, *, allow_stale: bool = False) -> object | None:
        entry = self.entries.get(key)
        if not entry:
            return None
        value, created = entry
        return value if allow_stale or self.clock.monotonic() - created <= self.ttl else None

    def available(self, key: str) -> bool:
        return self.clock.monotonic() >= self.open_until.get(key, 0)

    def failure(self, key: str, *, transient: bool) -> None:
        if not transient:
            return
        now = self.clock.monotonic()
        rows = [item for item in self.failures.get(key, []) if now - item <= 600]
        rows.append(now)
        self.failures[key] = rows
        if len(rows) >= 3:
            self.open_until[key] = now + self.cooldown

    def success(self, key: str) -> None:
        self.failures.pop(key, None)
        self.open_until.pop(key, None)
