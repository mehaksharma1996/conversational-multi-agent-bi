"""Thread-safe, process-local fixed-window limits for expensive API operations."""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass
from threading import Lock
from time import monotonic

from apps.api.errors import ApiError

RateLimitObserver = Callable[[str, str, bool, int], None]


@dataclass(frozen=True)
class RateLimitDecision:
    allowed: bool
    remaining: int
    retry_after_seconds: int


@dataclass
class _Window:
    expires_at: float
    count: int


class InMemoryRateLimiter:
    """Bound expensive operations within one API process.

    This is an abuse/cost guard for the repository's single-process deployment, not a distributed
    quota. Identity is stored only in memory and telemetry receives the tenant through its normal
    hashing path.
    """

    def __init__(
        self,
        *,
        clock: Callable[[], float] = monotonic,
        observer: RateLimitObserver | None = None,
    ) -> None:
        self._clock = clock
        self._observer = observer
        self._lock = Lock()
        self._windows: dict[tuple[str, str, str], _Window] = {}

    def consume(
        self,
        *,
        operation: str,
        tenant_id: str,
        workspace_id: str,
        limit: int,
        window_seconds: int,
    ) -> RateLimitDecision:
        if limit <= 0 or window_seconds <= 0:
            raise ValueError("Rate limits and windows must be positive.")

        now = self._clock()
        key = (operation, tenant_id, workspace_id)
        with self._lock:
            self._discard_expired(now)
            window = self._windows.get(key)
            if window is None or now >= window.expires_at:
                window = _Window(expires_at=now + window_seconds, count=0)
                self._windows[key] = window

            allowed = window.count < limit
            if allowed:
                window.count += 1
            remaining = max(0, limit - window.count)
            retry_after = max(1, math.ceil(window.expires_at - now))

        if self._observer is not None:
            self._observer(tenant_id, operation, not allowed, remaining)
        return RateLimitDecision(
            allowed=allowed,
            remaining=remaining,
            retry_after_seconds=retry_after,
        )

    def delete_workspace(self, tenant_id: str, workspace_id: str) -> None:
        with self._lock:
            keys = [key for key in self._windows if key[1] == tenant_id and key[2] == workspace_id]
            for key in keys:
                del self._windows[key]

    def _discard_expired(self, now: float) -> None:
        expired = [key for key, window in self._windows.items() if now >= window.expires_at]
        for key in expired:
            del self._windows[key]


def enforce_rate_limit(
    limiter: InMemoryRateLimiter,
    *,
    operation: str,
    tenant_id: str,
    workspace_id: str,
    limit: int,
    window_seconds: int,
) -> None:
    decision = limiter.consume(
        operation=operation,
        tenant_id=tenant_id,
        workspace_id=workspace_id,
        limit=limit,
        window_seconds=window_seconds,
    )
    if not decision.allowed:
        raise ApiError(
            429,
            "rate_limit_exceeded",
            "Too many requests for this operation. Try again later.",
            headers={"Retry-After": str(decision.retry_after_seconds)},
        )
