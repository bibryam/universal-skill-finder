from __future__ import annotations

import json
import math
import re
import socket
import time
from dataclasses import dataclass
from email.utils import format_datetime, parsedate_to_datetime
from http.client import HTTPException
from typing import Any
from urllib.error import HTTPError as UrlHTTPError, URLError
from urllib.parse import urlencode, urlparse, urlunparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .runtime import Deadline, PermitPool, RequestBudget, RuntimeLimitError
from .text import safe_web_url
from .versioning import VERSION


def _safe_rate_headers(headers: Any) -> dict[str, str]:
    """Retain only bounded numeric/date diagnostics, never arbitrary header text."""
    safe: dict[str, str] = {}
    if headers is None:
        return safe
    for name in ("Retry-After", "X-RateLimit-Remaining", "X-RateLimit-Reset"):
        value = headers.get(name) or headers.get(name.lower())
        if not isinstance(value, str) or not value or len(value) > 64 or any(ord(char) < 32 or ord(char) > 126 for char in value):
            continue
        value = value.strip()
        if re.fullmatch(r"[0-9]{1,12}", value):
            safe[name.lower()] = str(int(value))
        elif name == "Retry-After":
            try:
                parsed = parsedate_to_datetime(value)
                if parsed.tzinfo is not None:
                    safe[name.lower()] = format_datetime(parsed)
            except (TypeError, ValueError, OverflowError):
                pass
    return safe


class FinderHttpError(RuntimeError):
    def __init__(self, message: str, *, status: int | None = None, retry_after: str | None = None,
                 rate_limited: bool = False, headers: dict[str, str] | None = None):
        super().__init__(message)
        self.status = status
        self.headers = _safe_rate_headers({**(headers or {}), **({"Retry-After": retry_after} if retry_after is not None else {})})
        self.retry_after = self.headers.get("retry-after")
        self.rate_limited = rate_limited is True or status == 429


@dataclass
class HttpResponse:
    data: bytes
    status: int
    headers: dict[str, str]
    final_url: str


class SafeRedirectHandler(HTTPRedirectHandler):
    """Keep requests, queries, and credentials within the configured origin."""

    def redirect_request(self, req: Request, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> Request | None:
        # Validate before urllib normalizes whitespace in the Location header.
        HttpClient._validate_url(newurl)
        previous = urlparse(req.full_url)
        target = urlparse(newurl)
        if previous.scheme == "https" and target.scheme != "https":
            raise FinderHttpError("refused HTTPS downgrade redirect")
        previous_origin = (previous.scheme.lower(), previous.hostname, previous.port or (443 if previous.scheme == "https" else 80))
        target_origin = (target.scheme.lower(), target.hostname, target.port or (443 if target.scheme == "https" else 80))
        if previous_origin != target_origin:
            raise FinderHttpError("refused cross-origin redirect; configure the final endpoint explicitly")
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirected is None:
            return None
        return redirected


class HttpClient:
    def __init__(self, timeout: float = 10, max_bytes: int = 5_242_880, *,
                 deadline: Deadline | float | None = None, budget: RequestBudget | None = None,
                 permits: PermitPool | None = None):
        self.timeout = timeout
        self.max_bytes = max_bytes
        self.deadline = deadline
        self.budget = budget
        self.permits = permits

    @staticmethod
    def _validate_url(url: str) -> None:
        if not safe_web_url(url) or urlparse(url).fragment:
            # Do not echo URL credentials or query parameters into coverage.
            raise FinderHttpError("invalid network URL: expected HTTP(S), a host, and no credentials or fragment")

    @staticmethod
    def _endpoint_label(url: str) -> str:
        parsed = urlparse(url)
        return f"{parsed.scheme}://{parsed.netloc}"

    def request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, object] | None = None,
        headers: dict[str, str] | None = None,
        json_body: dict[str, Any] | None = None,
        max_bytes: int | None = None,
        timeout: float | None = None,
        deadline: Deadline | float | None = None,
        budget: RequestBudget | None = None,
        permits: PermitPool | None = None,
        phase: str = "source",
        quota_group: str | None = None,
    ) -> HttpResponse:
        deadline = self.deadline if deadline is None else deadline
        budget = self.budget if budget is None else budget
        permits = self.permits if permits is None else permits
        self._validate_url(url)
        durations = [self.timeout] + ([] if timeout is None else [timeout])
        try:
            if any(type(value) not in {int, float} or not math.isfinite(value) or value <= 0 for value in durations):
                raise ValueError("unsupported timeout")
            effective_timeout = min(float(value) for value in durations)
            if deadline is not None:
                remaining = deadline.remaining() if isinstance(deadline, Deadline) else float(deadline) - time.monotonic()
                if remaining <= 0:
                    raise RuntimeLimitError("deadline_exceeded", "shared network deadline elapsed")
                effective_timeout = min(effective_timeout, remaining)
        except (ValueError, OverflowError):
            raise FinderHttpError("request timeout must be finite and positive") from None
        if params:
            query = urlencode({key: value for key, value in params.items() if value is not None})
            parsed = urlparse(url)
            url = urlunparse(parsed._replace(query="&".join(part for part in (parsed.query, query) if part)))
        request_headers = {
            "Accept": "application/json",
            "User-Agent": f"universal-skill-finder/{VERSION} (+https://github.com/bibryam/universal-skill-finder)",
        }
        request_headers.update(headers or {})
        body = None
        if json_body is not None:
            body = json.dumps(json_body).encode("utf-8")
            request_headers["Content-Type"] = "application/json"
        for name, value in request_headers.items():
            if not isinstance(name, str) or not isinstance(value, str) or any(ord(char) < 32 or ord(char) == 127 for char in name + value):
                raise FinderHttpError("invalid HTTP header; control characters are not allowed")
        request = Request(url, data=body, headers=request_headers, method=method.upper())
        limit = self.max_bytes if max_bytes is None else max_bytes
        if limit <= 0:
            raise FinderHttpError("response byte limit must be positive")
        deadline = time.monotonic() + effective_timeout
        permit_lease = None
        try:
            if budget is not None:
                budget.reserve("request", 1, deadline)
                budget.reserve("bytes", limit, deadline)
                if urlparse(url).hostname == "api.github.com":
                    budget.reserve("github_api", 1, deadline)
            if permits is not None:
                permit_lease = permits.acquire(url, phase, deadline, quota_group=quota_group)
            with build_opener(SafeRedirectHandler()).open(request, timeout=effective_timeout) as response:
                final_url = response.geturl()
                self._validate_url(final_url)
                chunks: list[bytes] = []
                received = 0
                while True:
                    if time.monotonic() >= deadline:
                        raise FinderHttpError("response timeout exceeded")
                    # read1 performs at most one raw read, so a server that sends
                    # a byte before each socket timeout cannot hold read(n) open
                    # indefinitely. DNS/TLS still use platform socket timeouts.
                    chunk = response.read1(min(65_536, limit + 1 - received))
                    if not chunk:
                        break
                    chunks.append(chunk)
                    received += len(chunk)
                    if received > limit:
                        raise FinderHttpError(f"response exceeded {limit} bytes")
                data = b"".join(chunks)
                return HttpResponse(
                    data=data,
                    status=getattr(response, "status", 200),
                    headers={key.lower(): value for key, value in response.headers.items()},
                    final_url=final_url,
                )
        except UrlHTTPError as exc:
            rate_headers = _safe_rate_headers(exc.headers)
            parsed = urlparse(url)
            github_api = parsed.scheme == "https" and parsed.hostname == "api.github.com" and (parsed.port or 443) == 443
            rate_limited = exc.code == 429 or (
                exc.code == 403 and github_api and (
                    rate_headers.get("x-ratelimit-remaining") == "0" or "retry-after" in rate_headers
                )
            )
            exc.close()
            raise FinderHttpError(f"HTTP {exc.code} from {self._endpoint_label(url)}", status=exc.code,
                                  headers=rate_headers, rate_limited=rate_limited) from exc
        except (URLError, OSError, HTTPException) as exc:
            reason = getattr(exc, "reason", exc)
            detail = "timeout" if isinstance(reason, (socket.timeout, TimeoutError)) else type(reason).__name__
            raise FinderHttpError(f"network error for {self._endpoint_label(url)}: {detail}") from exc
        finally:
            if permit_lease is not None:
                permit_lease.release()

    def _parse_json(self, response: HttpResponse, url: str) -> Any:
        try:
            payload = json.loads(response.data.decode("utf-8"))
            pending = [(payload, 0)]
            while pending:
                value, depth = pending.pop()
                if depth > 100:
                    raise ValueError("JSON nesting exceeds 100 levels")
                children = value.values() if isinstance(value, dict) else value if isinstance(value, list) else ()
                pending.extend((child, depth + 1) for child in children if isinstance(child, (dict, list)))
            return payload
        except (UnicodeDecodeError, ValueError, RecursionError) as exc:
            raise FinderHttpError(f"invalid JSON from {self._endpoint_label(url)}") from exc

    def get_json(self, url: str, *, params: dict[str, object] | None = None, headers: dict[str, str] | None = None) -> Any:
        return self._parse_json(self.request("GET", url, params=params, headers=headers), url)

    def post_json(self, url: str, *, body: dict[str, Any], headers: dict[str, str] | None = None) -> Any:
        return self._parse_json(self.request("POST", url, headers=headers, json_body=body), url)

    def get_bytes(self, url: str, *, headers: dict[str, str] | None = None, max_bytes: int | None = None) -> bytes:
        return self.request("GET", url, headers=headers, max_bytes=max_bytes).data
