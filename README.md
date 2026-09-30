# Idempotent Payments API

A payments API that safely handles the most common failure mode in
distributed systems: **the client doesn't know if its request succeeded**.

If a payment request times out, was the charge actually made? Blindly
retrying can double-charge a real user. This project implements the
industry-standard fix — idempotency keys — and proves it works with tests
that simulate the exact failure scenario.

## The problem, concretely

```
Client                          Server
  |--- POST /payments --------->|
  |                              | (processes payment, charges card)
  |<---------- (timeout) --------| response lost in transit
  |
  | Client doesn't know if it worked. Does it retry?
  |   - No retry  -> user is told "failed", but they WERE actually charged
  |   - Retry     -> if retried naively, user gets charged TWICE
```

## How this solves it

Every write request carries an `Idempotency-Key` header — a UUID the
**client** generates once per logical operation (e.g. once per "Pay Now"
button press, reused across all retries of that same attempt).

- **First time a key is seen**: the payment is processed normally, and the
  response is cached against that key with a 24h TTL.
- **Key seen again** (a retry): the cached response is returned immediately
  — the payment logic does not run a second time. No duplicate charge.
- **Key currently mid-flight** (two retries racing each other): the second
  one gets a `409 Conflict` rather than racing into a duplicate write.
- **Key reused with a different request body**: rejected with `422` — this
  catches a client bug (accidentally reusing a key for a different payment)
  rather than silently returning the wrong cached result.

## Why Redis

Idempotency keys need fast lookups and built-in expiry (TTL) — both native
to Redis. It also means the idempotency store is shared if this API is ever
scaled to multiple instances behind a load balancer, which a local
in-memory dict couldn't give you. This repo uses `fakeredis` (a real
Redis-compatible client, just in-memory) so it runs with zero external
services; swapping to a live Redis instance is a one-line change (see
`app.py`, `USE_REAL_REDIS` env var).

## Structure

| File | Purpose |
|---|---|
| `idempotency.py` | Core idempotency logic — storage-agnostic, independently testable |
| `app.py` | Flask API: `POST /payments`, with the idempotency check as middleware-style logic |
| `retry_client.py` | Demo client showing exponential-backoff retries using the same key |
| `tests/test_idempotency.py` | Proves retries don't duplicate — including the critical race/reuse cases |

## Running it

```bash
pip install -r requirements.txt

# Run the tests (proves the core guarantee)
pytest tests/ -v

# Run the API
python app.py

# In another terminal, see the retry client in action
python retry_client.py

# Inspect what actually got written
curl http://localhost:5000/payments/_ledger
```

## Key test

```python
def test_retried_request_does_not_duplicate_payment(client):
    # same Idempotency-Key sent twice, simulating a client retry
    ...
    assert len(ledger) == 1, "Expected 1 payment... duplicate charge occurred!"
```
All 5 tests pass, including this one — the whole point of the project.

## What I'd add next

- Real Redis in a docker-compose setup for a closer-to-production demo
- Idempotency key expiry configurable per-endpoint (24h is a reasonable
  default for payments, but not every operation needs the same window)
- Structured logging around cache hits vs. fresh processing, to make the
  retry behaviour visible in real request logs, not just in tests
