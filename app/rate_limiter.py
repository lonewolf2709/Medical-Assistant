"""Rate limiting.

Two layers, because they guard different things:

* `limiter` (slowapi, keyed on IP) is a coarse guard on the endpoint itself.
  Every genuine update arrives from Telegram's own servers, so this cannot
  distinguish users — on its own it puts everybody in one bucket, letting a
  single chatty user throttle the whole bot.
* `user_limiter` is keyed on the Telegram user id and is the one that actually
  protects users from each other.
"""
import time
from collections import defaultdict, deque

from slowapi import Limiter
from slowapi.util import get_remote_address

from app.config import settings

# Coarse per-IP guard on the endpoint.
limiter = Limiter(key_func=get_remote_address, default_limits=["60/minute"])


class UserRateLimiter:
    """Sliding-window limiter keyed on the Telegram user id.

    State is per process, which is enough for a single instance. Running several
    API instances would need shared state (Redis) for a global limit; until then
    each instance enforces its own budget.
    """

    def __init__(self, max_events: int, window_seconds: float) -> None:
        self.max_events = max_events
        self.window_seconds = window_seconds
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def allow(self, key: str, now: float | None = None) -> bool:
        """Record a request and report whether it is within the user's budget."""
        now = time.monotonic() if now is None else now
        hits = self._hits[key]

        cutoff = now - self.window_seconds
        while hits and hits[0] <= cutoff:
            hits.popleft()

        if len(hits) >= self.max_events:
            return False

        hits.append(now)
        return True

    def reset(self) -> None:
        self._hits.clear()


user_limiter = UserRateLimiter(
    max_events=settings.user_rate_limit_per_minute, window_seconds=60.0
)
