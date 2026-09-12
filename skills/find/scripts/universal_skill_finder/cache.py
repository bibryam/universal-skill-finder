from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import secrets
import stat
import time
import threading
from dataclasses import dataclass
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable

from .text import clean_text
from .versioning import ADAPTER_CONTRACT_VERSION, CACHE_FORMAT_VERSION


MAX_CACHE_BYTES = 16 * 1024 * 1024
MAX_CACHE_ENTRIES = 10_000
_COMPONENT_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
# ``msvcrt.locking(LK_NBLCK)`` reports an already-locked region as EACCES.
# Do not treat unrelated I/O errors from the lock syscall as contention: they
# need to reach the outer storage-error classifier.
_WINDOWS_LOCK_CONTENTION_ERRNOS = frozenset({errno.EACCES})


@dataclass(frozen=True)
class CacheLease:
    """Boolean-compatible lease outcome without exposing filesystem error text."""

    status: str

    def __bool__(self) -> bool:
        return self.status == "acquired"


@dataclass
class FlightClaim:
    state: str
    namespace: str
    key: str
    _owner: "SingleFlight | None" = None

    @property
    def leader(self) -> bool:
        return self.state == "leader"

    def release(self) -> None:
        owner, self._owner = self._owner, None
        if owner and self.leader:
            owner.release(self.namespace, self.key)

    def __enter__(self) -> "FlightClaim":
        return self

    def __exit__(self, *_: Any) -> None:
        self.release()


class SingleFlight:
    """In-process scoped leases that coalesce identical foreground work."""

    def __init__(self, *, clock=time.monotonic, lease_seconds: float = 30.0):
        self.clock = clock
        self.lease_seconds = lease_seconds
        self._condition = threading.Condition()
        self._leaders: dict[tuple[str, str], float] = {}
        self._releases: dict[tuple[str, str], int] = {}

    def claim(self, namespace: str, key: str, scope: str, deadline: Any) -> FlightClaim:
        scoped = Cache.key(key, scope)
        flight = (namespace, scoped)
        with self._condition:
            observed_release = self._releases.get(flight, 0)
            while True:
                now = self.clock()
                expires = self._leaders.get(flight)
                if expires is None:
                    if self._releases.get(flight, 0) != observed_release:
                        return FlightClaim("waiter", namespace, scoped)
                    self._leaders[flight] = now + self.lease_seconds
                    return FlightClaim("leader", namespace, scoped, self)
                if expires <= now:
                    self._leaders[flight] = now + self.lease_seconds
                    return FlightClaim("leader", namespace, scoped, self)
                # Discovery leaders must stop at collection freeze. Proofs run
                # after freeze under the shared network deadline instead.
                remaining_method = (
                    deadline.collection_remaining
                    if namespace in {"queries", "catalogues"} and hasattr(deadline, "collection_remaining")
                    else deadline.remaining
                )
                remaining = remaining_method(now)
                if remaining <= 0:
                    return FlightClaim("expired", namespace, scoped)
                self._condition.wait(timeout=min(0.05, remaining, expires - now))

    def release(self, namespace: str, key: str) -> None:
        with self._condition:
            flight = (namespace, key)
            if self._leaders.pop(flight, None) is not None:
                self._releases[flight] = self._releases.get(flight, 0) + 1
            self._condition.notify_all()


def default_cache_dir() -> Path:
    override = os.environ.get("UNIVERSAL_SKILL_FINDER_CACHE")
    if override:
        return _checked_cache_root(Path(override).expanduser())
    xdg = os.environ.get("XDG_CACHE_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".cache"
    return _checked_cache_root(base / "universal-skill-finder")


def _checked_cache_root(path: Path) -> Path:
    # Fail on redirected or broken cache roots instead of following them.
    if path.is_symlink():
        raise ValueError(f"cache root must not be a symlink: {path}")
    try:
        selected = path.parent.resolve() / path.name
        mode = selected.lstat().st_mode
    except FileNotFoundError:
        return path.parent.resolve() / path.name
    except RuntimeError as exc:
        raise ValueError(f"cannot resolve cache root: {path}") from exc
    if not stat.S_ISDIR(mode):
        raise ValueError(f"cache root must be a directory: {path}")
    return selected


class Cache:
    def __init__(self, root: Path | None = None):
        self.root = _checked_cache_root((root or default_cache_dir()).expanduser())
        self.singleflight = SingleFlight()

    @staticmethod
    def key(*parts: object) -> str:
        material = json.dumps(
            [CACHE_FORMAT_VERSION, ADAPTER_CONTRACT_VERSION, *[str(part) for part in parts]],
            ensure_ascii=True, separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(material).hexdigest()

    def _path(self, namespace: str, key: str) -> Path:
        if not _COMPONENT_RE.fullmatch(namespace) or not _COMPONENT_RE.fullmatch(key):
            raise ValueError("cache namespace and key must be safe filename components")
        return self.root / namespace / f"{key}.json"

    @contextmanager
    def _directory(self, namespace: str, *, create: bool = False):
        path = self._path(namespace, "entry").parent
        if create:
            self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if os.open in os.supports_dir_fd and hasattr(os, "O_NOFOLLOW"):
            flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
            root_fd = os.open(self.root, flags)
            directory_fd = None
            try:
                if create:
                    try:
                        os.mkdir(namespace, mode=0o700, dir_fd=root_fd)
                    except FileExistsError:
                        pass
                directory_fd = os.open(namespace, flags, dir_fd=root_fd)
                yield path, directory_fd
            finally:
                if directory_fd is not None:
                    os.close(directory_fd)
                os.close(root_fd)
        else:
            if path.is_symlink():
                raise OSError("symlinked cache namespace")
            if create:
                path.mkdir(mode=0o700, exist_ok=True)
            yield path, None

    @staticmethod
    def _read_entry(directory: Path, directory_fd: int | None, filename: str, max_age: int | None):
        if directory_fd is None and (directory / filename).is_symlink():
            return None
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        descriptor = os.open(filename, flags, dir_fd=directory_fd) if directory_fd is not None else os.open(directory / filename, flags)
        with os.fdopen(descriptor, "rb") as stream:
            file_stat = os.fstat(stream.fileno())
            if not stat.S_ISREG(file_stat.st_mode) or file_stat.st_size > MAX_CACHE_BYTES:
                return None
            age = max(0, int(time.time() - file_stat.st_mtime))
            if max_age is not None and age > max_age:
                return None
            raw = stream.read(MAX_CACHE_BYTES + 1)
        if len(raw) > MAX_CACHE_BYTES:
            return None
        payload = json.loads(raw.decode("utf-8"))
        pending = [(payload, 0)]
        while pending:
            value, depth = pending.pop()
            if depth > 100:
                raise ValueError("cached JSON nesting exceeds 100 levels")
            children = value.values() if isinstance(value, dict) else value if isinstance(value, list) else ()
            pending.extend((child, depth + 1) for child in children if isinstance(child, (dict, list)))
        if (
            not isinstance(payload, dict)
            or type(payload.get("cache_format_version")) is not int
            or payload["cache_format_version"] != CACHE_FORMAT_VERSION
            or type(payload.get("adapter_contract_version")) is not int
            or payload["adapter_contract_version"] != ADAPTER_CONTRACT_VERSION
            or "payload" not in payload
        ):
            # Old, unknown and malformed envelopes are misses, never guessed
            # migrations. Offline callers report the miss without networking.
            return None
        return payload["payload"], age

    def read(self, namespace: str, key: str, *, max_age: int | None = None) -> tuple[Any, int] | None:
        filename = self._path(namespace, key).name
        try:
            with self._directory(namespace) as (directory, directory_fd):
                return self._read_entry(directory, directory_fd, filename, max_age)
        except (OSError, ValueError, UnicodeError, RecursionError):
            return None

    def write(self, namespace: str, key: str, value: Any, *,
              can_publish: Callable[[], bool] | None = None) -> bool:
        """Atomically publish an optional cache entry when its caller still owns it.

        ``can_publish`` is deliberately checked again at the rename boundary.
        A deadline-aware child can spend material time encoding or writing a
        temporary entry; a check only at method entry would allow it to become
        visible after the parent has frozen collection. Ordinary callers keep
        best-effort cache semantics when no publication guard is supplied.
        """
        filename = self._path(namespace, key).name
        try:
            if can_publish is not None and not callable(can_publish):
                return False
            if can_publish is not None and not can_publish():
                return False
            envelope = {
                "cache_format_version": CACHE_FORMAT_VERSION,
                "adapter_contract_version": ADAPTER_CONTRACT_VERSION,
                "payload": value,
            }
            payload = json.dumps(envelope, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
            if len(payload) > MAX_CACHE_BYTES:
                return False
            with self._directory(namespace, create=True) as (directory, directory_fd):
                temp_name = ".cache-" + secrets.token_hex(16)
                temporary = temp_name if directory_fd is not None else directory / temp_name
                destination = filename if directory_fd is not None else directory / filename
                descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=directory_fd)
                try:
                    with os.fdopen(descriptor, "wb") as stream:
                        stream.write(payload)
                    # This is intentionally immediately adjacent to the
                    # atomic publication step. Do not move it above payload
                    # encoding or temporary-file I/O.
                    if can_publish is not None and not can_publish():
                        return False
                    os.replace(temporary, destination, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
                    return True
                finally:
                    try:
                        os.unlink(temporary, dir_fd=directory_fd)
                    except FileNotFoundError:
                        pass
        except (OSError, ValueError, UnicodeError, RecursionError):
            # Caching is optional. A read-only or full cache must not discard results.
            return False

    @contextmanager
    def exclusive_lease(self, namespace: str, key: str, *, timeout: float = 0.05,
                        stale_seconds: float = 5.0):
        """Acquire a bounded cross-process cache lease without following links.

        Advisory locks are released by the OS when a leader crashes.  Keeping a
        stable lock file avoids deciding that a live leader is stale from a
        wall-clock file timestamp, which is unsafe across clock adjustments.
        ``stale_seconds`` remains a validated compatibility argument; lock
        recovery no longer depends on it. The boolean-compatible outcome
        distinguishes contention from storage/access failure; neither grants
        ownership. Callers must not bypass a cooldown on either failure.
        """
        self._path(namespace, key)
        if timeout < 0 or stale_seconds <= 0:
            raise ValueError("cache lease bounds are invalid")
        started = time.monotonic()
        yielded = False
        filename = f".lease-{key}.lock"
        try:
            with self._directory(namespace, create=True) as (directory, directory_fd):
                target = filename if directory_fd is not None else directory / filename
                descriptor = os.open(
                    target, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
                    0o600, dir_fd=directory_fd,
                )
                try:
                    info = os.fstat(descriptor)
                    if not stat.S_ISREG(info.st_mode):
                        raise OSError("cache lease must be a regular file")
                    if os.name == "nt":
                        import msvcrt
                        if info.st_size == 0:
                            os.write(descriptor, b"0")

                        def try_lock() -> bool:
                            os.lseek(descriptor, 0, os.SEEK_SET)
                            try:
                                msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
                            except OSError as exc:
                                if exc.errno in _WINDOWS_LOCK_CONTENTION_ERRNOS:
                                    return False
                                raise
                            return True

                        def unlock() -> None:
                            os.lseek(descriptor, 0, os.SEEK_SET)
                            msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl

                        def try_lock() -> bool:
                            try:
                                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                            except BlockingIOError:
                                return False
                            return True

                        def unlock() -> None:
                            fcntl.flock(descriptor, fcntl.LOCK_UN)

                    while not try_lock():
                        remaining = timeout - (time.monotonic() - started)
                        if remaining <= 0:
                            yielded = True
                            yield CacheLease("busy")
                            return
                        time.sleep(min(0.005, remaining))
                    try:
                        yielded = True
                        yield CacheLease("acquired")
                    finally:
                        unlock()
                finally:
                    os.close(descriptor)
        except OSError as exc:
            if yielded:
                raise
            status = "permission_denied" if exc.errno in {errno.EACCES, errno.EPERM, errno.EROFS} else "storage_unavailable"
            yield CacheLease(status)

    def metadata(self, namespace: str) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        try:
            with self._directory(namespace) as (directory, directory_fd), os.scandir(
                directory_fd if directory_fd is not None else directory
            ) as entries:
                for index, entry in enumerate(entries):
                    if index >= MAX_CACHE_ENTRIES:
                        break
                    if not entry.name.endswith(".json") or not _COMPONENT_RE.fullmatch(entry.name[:-5]):
                        continue
                    try:
                        cached = self._read_entry(directory, directory_fd, entry.name, None)
                    except (OSError, ValueError, UnicodeError, RecursionError):
                        continue
                    if cached is None or not isinstance(cached[0], dict):
                        continue
                    payload, age = cached
                    metadata = {key: payload.get(key) for key in ("source_id", "query", "limit", "cached_at")}
                    if all(isinstance(metadata[key], str) and metadata[key] for key in ("source_id", "query")) and type(metadata["limit"]) is int and metadata["limit"] > 0:
                        metadata["source_id"] = clean_text(metadata["source_id"], 64)
                        metadata["query"] = clean_text(metadata["query"], 500)
                        metadata["cache_age_seconds"] = age
                        records.append(metadata)
        except OSError:
            return []
        return sorted(records, key=lambda item: (str(item["source_id"]), str(item["query"]), int(item["limit"])))
