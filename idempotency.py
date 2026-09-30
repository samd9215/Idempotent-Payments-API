"""
idempotency.py
---------------
Core idempotency logic, decoupled from Flask so it's independently testable.

How it works:
  1. Client sends a POST with an `Idempotency-Key` header (a UUID it generates
     once per logical operation, e.g. once per "checkout button click").
  2. Before processing, we check if that key has already been seen.
       - Not seen: process the request normally, store the (key -> response)
         pair with a TTL, return the response.
       - Seen, and the original request is still processing: return 409
         (Conflict) — this catches the case of two retries racing each other.
       - Seen, and a response is stored: return the STORED response, without
         re-running the business logic (so a retried "create payment" never
         charges twice).
  3. The stored key expires after a TTL (here 24h) — long enough to cover
     realistic retry windows, short enough not to grow the store unbounded.

Uses Redis for storage since it's the natural fit here: fast key lookups,
built-in TTL/expiry, and — if you ever run this API across multiple
instances behind a load balancer — a shared store all instances can see,
which a local in-memory dict couldn't give you.
"""
import hashlib
import json
import time

IN_PROGRESS_SENTINEL = "__IN_PROGRESS__"
KEY_PREFIX = "idempotency:"
TTL_SECONDS = 24 * 60 * 60  # 24 hours


class IdempotencyConflict(Exception):
    """Raised when the same key is currently being processed by another request."""
    pass


def _request_fingerprint(method: str, path: str, body: dict) -> str:
    """
    Hash the request shape alongside the idempotency key. This guards against
    a subtle bug: a client accidentally reusing the same Idempotency-Key for
    two genuinely different requests (e.g. a copy-pasted header). If the
    fingerprint doesn't match what's stored, we treat it as a client error
    rather than silently returning the wrong cached response.
    """
    payload = json.dumps({"method": method, "path": path, "body": body}, sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()


class IdempotencyStore:
    def __init__(self, redis_client):
        self.redis = redis_client

    def get_or_reserve(self, key: str, method: str, path: str, body: dict):
        """
        Attempts to reserve this idempotency key for processing.

        Returns:
            ("cached", response_dict)   -> a stored response already exists, return it as-is
            ("proceed", fingerprint)    -> no prior record; caller should process the
                                            request, then call `save_response`
        Raises:
            IdempotencyConflict         -> another request with this key is mid-flight
        """
        redis_key = KEY_PREFIX + key
        fingerprint = _request_fingerprint(method, path, body)

        existing = self.redis.get(redis_key)
        if existing is not None:
            existing = existing.decode() if isinstance(existing, bytes) else existing
            if existing == IN_PROGRESS_SENTINEL:
                raise IdempotencyConflict(
                    f"Request with key '{key}' is already being processed."
                )
            record = json.loads(existing)
            if record["fingerprint"] != fingerprint:
                raise ValueError(
                    "Idempotency-Key reused for a different request body/path. "
                    "Use a new key per distinct logical operation."
                )
            return "cached", record["response"]

        # Reserve the key so a concurrent retry sees IN_PROGRESS, not a gap
        # where it could start a second, duplicate execution.
        self.redis.set(redis_key, IN_PROGRESS_SENTINEL, ex=TTL_SECONDS, nx=True)
        return "proceed", fingerprint

    def save_response(self, key: str, fingerprint: str, response: dict):
        redis_key = KEY_PREFIX + key
        record = json.dumps({
            "fingerprint": fingerprint,
            "response": response,
            "stored_at": time.time(),
        })
        self.redis.set(redis_key, record, ex=TTL_SECONDS)
