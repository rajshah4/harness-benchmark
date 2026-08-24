# Task 8: Token-Bucket Rate Limiter

Implement a token-bucket rate limiter in `rate_limiter.py`.

Required public API:

- `TokenBucket(capacity, refill_rate)`
- `TokenBucket.allow() -> bool`
- `TokenBucket._refill()`
- `RateLimiter`, or equivalent module-level registry behavior
- `get_limiter(name, capacity, refill_rate) -> TokenBucket`
- `check(name) -> bool`
- `RateLimitExceeded`
- `@rate_limit(name, capacity, refill_rate)`

Required behavior:

1. A new bucket begins with `capacity` available tokens.
2. `allow()` consumes one token when available and returns `True`.
3. `allow()` returns `False` when no token is available.
4. Tokens refill according to elapsed monotonic time and `refill_rate` tokens per second.
5. Refilled tokens never exceed `capacity`.
6. A zero-capacity bucket always denies requests.
7. Repeated registry lookups for the same name return the same bucket.
8. The decorator calls the wrapped function when allowed and raises `RateLimitExceeded` when denied.

Do not create or modify tests. You may create small temporary checks, but remove them before finishing. Work only inside the current workspace.

Before finishing:

1. Inspect your final diff.
2. Run your own behavioral checks.
3. Report the files changed, checks run, and any remaining uncertainty.

