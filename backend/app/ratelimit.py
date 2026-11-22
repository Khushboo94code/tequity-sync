"""A thread-safe token-bucket rate limiter.

This is the single place the Salesforce request cap is enforced. The worker calls acquire()
before every Salesforce request (including retries), so the outbound call rate can never
exceed the configured cap. The clock and sleep are injectable so the limiter is testable
without real time.
"""
import threading
import time


class TokenBucket:
    """Classic token bucket.

    `ratePerSecond` tokens refill continuously up to a one-second burst capacity. acquire()
    blocks until a token is available.

    Args:
        ratePerSecond: sustained token rate = the request cap (100 in the design; lower in a
            demo so the drain is visibly paced).
        timeFn: monotonic clock source (injectable for tests).
        sleepFn: sleep function (injectable for tests).
    """

    def __init__(self, ratePerSecond: float, timeFn=time.monotonic, sleepFn=time.sleep):
        self.rate = float(ratePerSecond)
        self.capacity = max(1.0, self.rate)
        self.tokens = self.capacity
        self._time = timeFn
        self._sleep = sleepFn
        self._last = timeFn()
        self._lock = threading.Lock()

    def _refill(self) -> None:
        now = self._time()
        elapsed = now - self._last
        self._last = now
        self.tokens = min(self.capacity, self.tokens + elapsed * self.rate)

    def acquire(self, tokens: float = 1.0) -> None:
        """Block until `tokens` are available, then consume them."""
        while True:
            with self._lock:
                self._refill()
                if self.tokens >= tokens:
                    self.tokens -= tokens
                    return
                waitFor = (tokens - self.tokens) / self.rate
            self._sleep(waitFor)
