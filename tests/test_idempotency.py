"""
tests/test_idempotency.py
--------------------------
Proves the core claim of this project: a retried POST with the same
Idempotency-Key does not create a duplicate payment.

Run: pytest tests/ -v
"""
import os
import sys
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as app_module


@pytest.fixture
def client():
    # Fresh DB and fresh fake-Redis state per test
    if os.path.exists(app_module.DB_PATH):
        os.remove(app_module.DB_PATH)
    import fakeredis
    app_module.redis_client = fakeredis.FakeStrictRedis()
    app_module.store = app_module.IdempotencyStore(app_module.redis_client)
    app_module.app.config["TESTING"] = True
    with app_module.app.test_client() as c:
        yield c


def test_single_request_creates_one_payment(client):
    resp = client.post("/payments", json={"amount_cents": 5000, "recipient": "alice"},
                        headers={"Idempotency-Key": "key-1"})
    assert resp.status_code == 201
    assert resp.get_json()["idempotent_replay"] is False

    ledger = client.get("/payments/_ledger").get_json()
    assert len(ledger) == 1


def test_retried_request_does_not_duplicate_payment(client):
    """THE core test: same key sent twice (simulating a client retry after
    a timeout) must result in exactly ONE row in the ledger, not two."""
    headers = {"Idempotency-Key": "retry-key-abc"}
    body = {"amount_cents": 10000, "recipient": "bob"}

    first = client.post("/payments", json=body, headers=headers)
    assert first.status_code == 201
    first_id = first.get_json()["payment_id"]

    # Simulate the client not receiving the response (network blip) and retrying
    second = client.post("/payments", json=body, headers=headers)
    assert second.status_code == 200  # cached replay, not a new creation
    assert second.get_json()["idempotent_replay"] is True
    assert second.get_json()["payment_id"] == first_id  # same payment returned

    ledger = client.get("/payments/_ledger").get_json()
    assert len(ledger) == 1, f"Expected 1 payment, found {len(ledger)} — duplicate charge occurred!"


def test_different_keys_create_separate_payments(client):
    client.post("/payments", json={"amount_cents": 100, "recipient": "x"},
                headers={"Idempotency-Key": "key-A"})
    client.post("/payments", json={"amount_cents": 200, "recipient": "y"},
                headers={"Idempotency-Key": "key-B"})

    ledger = client.get("/payments/_ledger").get_json()
    assert len(ledger) == 2


def test_missing_idempotency_key_rejected(client):
    resp = client.post("/payments", json={"amount_cents": 100, "recipient": "x"})
    assert resp.status_code == 400


def test_reusing_key_with_different_body_is_rejected(client):
    """Guards against a client bug: reusing a key for a genuinely different request."""
    headers = {"Idempotency-Key": "shared-key"}
    client.post("/payments", json={"amount_cents": 100, "recipient": "alice"}, headers=headers)
    resp = client.post("/payments", json={"amount_cents": 999, "recipient": "mallory"}, headers=headers)
    assert resp.status_code == 422

    ledger = client.get("/payments/_ledger").get_json()
    assert len(ledger) == 1  # the second, mismatched request must NOT have gone through
