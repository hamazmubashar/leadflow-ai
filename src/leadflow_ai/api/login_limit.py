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


# --- per-username lockout (M11): 3 consecutive failures -> 15 minutes ---------------------

LOCKOUT_FAILURES = 3
LOCKOUT_SECONDS = 15 * 60


class SignInLockedError(Exception):
    """This username is locked after repeated failures (shown with a countdown)."""

    def __init__(self, retry_after_seconds: int) -> None:
        super().__init__(retry_after_seconds)
        self.retry_after_seconds = retry_after_seconds


class _KeyState:
    __slots__ = ("failures", "in_flight", "locked_until")

    def __init__(self) -> None:
        self.failures: deque[float] = deque()
        self.in_flight = 0
        self.locked_until: float | None = None


class UsernameLockout:
    """Lock a *submitted username* for 15 minutes after 3 consecutive failed sign-ins.

    Keyed by the name as typed (trimmed, lowercased), whether or not such an account
    exists, so a lockout never reveals which usernames are real. Each attempt is counted
    *before* the password is checked (``begin``), so parallel requests can't squeeze in a
    fourth guess. A success clears the count. Failures older than the window are forgotten.

    In memory, per process: it survives page refreshes and browser restarts, not an API
    restart. The database lockout on real accounts (same threshold) is the durable
    backstop; it answers with the generic invalid-credentials error.
    """

    def __init__(
        self,
        *,
        max_failures: int = LOCKOUT_FAILURES,
        lockout_seconds: float = LOCKOUT_SECONDS,
        window_seconds: float = LOCKOUT_SECONDS,
        max_keys: int = MAX_TRACKED_CLIENTS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._max = max_failures
        self._lockout = lockout_seconds
        self._window = window_seconds
        self._max_keys = max_keys
        self._clock = clock
        self._lock = threading.Lock()
        self._keys: OrderedDict[str, _KeyState] = OrderedDict()

    @staticmethod
    def key(username: str) -> str:
        return username.strip().lower()[:128]

    def begin(self, username: str) -> None:
        """Count one attempt as in flight, or raise SignInLockedError."""
        key = self.key(username)
        with self._lock:
            now = self._clock()
            state = self._state(key, now)
            if state.locked_until is not None:
                if state.locked_until > now:
                    raise SignInLockedError(_seconds(state.locked_until - now))
                state.locked_until = None  # expired: a fresh count
                state.failures.clear()
            _drop_expired(state.failures, now - self._window)
            if len(state.failures) + state.in_flight >= self._max:
                # Only reachable with attempts still in flight: refuse, don't queue.
                raise SignInLockedError(_seconds(self._lockout))
            state.in_flight += 1

    def failed(self, username: str) -> int | None:
        """Record a failure; return the lock's length in seconds if this one locked it."""
        key = self.key(username)
        with self._lock:
            now = self._clock()
            state = self._state(key, now)
            state.in_flight = max(0, state.in_flight - 1)
            _drop_expired(state.failures, now - self._window)
            state.failures.append(now)
            if len(state.failures) < self._max:
                return None
            state.failures.clear()
            state.locked_until = now + self._lockout
            return _seconds(self._lockout)

    def succeeded(self, username: str) -> None:
        """A correct password: the consecutive-failure count starts again."""
        with self._lock:
            self._keys.pop(self.key(username), None)

    def abandoned(self, username: str) -> None:
        """The attempt ended without an answer (e.g. database unavailable): uncount it."""
        with self._lock:
            state = self._keys.get(self.key(username))
            if state is not None:
                state.in_flight = max(0, state.in_flight - 1)

    def tracked(self) -> int:
        with self._lock:
            return len(self._keys)

    def _state(self, key: str, now: float) -> _KeyState:
        state = self._keys.get(key)
        if state is not None:
            self._keys.move_to_end(key)
            return state
        if len(self._keys) >= self._max_keys:
            idle = [
                k
                for k, s in self._keys.items()
                if s.in_flight == 0
                and (s.locked_until is None or s.locked_until <= now)
                and (not s.failures or s.failures[-1] <= now - self._window)
            ]
            for k in idle:
                del self._keys[k]
            while len(self._keys) >= self._max_keys:
                self._keys.popitem(last=False)
        state = self._keys[key] = _KeyState()
        return state


def _seconds(value: float) -> int:
    return max(1, math.ceil(value))
