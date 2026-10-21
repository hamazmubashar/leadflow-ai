"""Per-client login rate limit (M11), in memory.

Each login attempt from a client address *reserves* a slot before the password is
checked; a successful login gives its slot back. So at most ``max_attempts`` failed
(or still running) attempts per address fit in any ``window``, even when they arrive
in parallel. Past that the client gets ``429`` with ``Retry-After`` and no password
work is done.

This sits in front of the per-account lockout (5 failures -> 15 minutes), which
protects each account no matter how many addresses an attacker uses.

Limits:
- State is per process and is lost on restart.
- The client address is the TCP peer. Behind a reverse proxy every request has the
  proxy's address, so the proxy must pass the real one (uvicorn ``--proxy-headers``
  with ``--forwarded-allow-ips`` set to the proxy only). That is deployment work (M12).
- At most ``max_clients`` addresses are tracked; when full, the address seen least
  recently is forgotten first.
"""

import math
import threading
import time
from collections import OrderedDict, deque
from collections.abc import Callable

LOGIN_ATTEMPTS_PER_CLIENT = 10
LOGIN_WINDOW_SECONDS = 15 * 60
MAX_TRACKED_CLIENTS = 10_000


class LoginRateLimitedError(Exception):
    """Too many recent login attempts from this client address."""

    def __init__(self, retry_after_seconds: int) -> None:
        super().__init__(retry_after_seconds)
        self.retry_after_seconds = retry_after_seconds


class LoginRateLimiter:
    def __init__(
        self,
        *,
        max_attempts: int = LOGIN_ATTEMPTS_PER_CLIENT,
        window_seconds: float = LOGIN_WINDOW_SECONDS,
        max_clients: int = MAX_TRACKED_CLIENTS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._max_attempts = max_attempts
        self._window = window_seconds
        self._max_clients = max_clients
        self._clock = clock
        self._lock = threading.Lock()
        self._attempts: OrderedDict[str, deque[float]] = OrderedDict()

    def reserve(self, client: str) -> float:
        """Count one attempt for ``client`` and return its stamp, or raise LoginRateLimitedError."""
        with self._lock:
            now = self._clock()
            attempts = self._attempts.get(client)
            if attempts is None:
                self._make_room(now)
                attempts = self._attempts[client] = deque()
            else:
                self._attempts.move_to_end(client)
            _drop_expired(attempts, now - self._window)
            if len(attempts) >= self._max_attempts:
                retry_after = attempts[0] + self._window - now
                raise LoginRateLimitedError(max(1, math.ceil(retry_after)))
            attempts.append(now)
            return now

    def release(self, client: str, stamp: float) -> None:
        """Give back the slot of an attempt that succeeded."""
        with self._lock:
            attempts = self._attempts.get(client)
            if attempts is not None and stamp in attempts:
                attempts.remove(stamp)

    def tracked_clients(self) -> int:
        with self._lock:
            return len(self._attempts)

    def _make_room(self, now: float) -> None:
        if len(self._attempts) < self._max_clients:
            return
        cutoff = now - self._window
        for client in [c for c, a in self._attempts.items() if not a or a[-1] <= cutoff]:
            del self._attempts[client]
        while len(self._attempts) >= self._max_clients:
            self._attempts.popitem(last=False)  # least recently seen


def _drop_expired(attempts: deque[float], cutoff: float) -> None:
    while attempts and attempts[0] <= cutoff:
        attempts.popleft()
