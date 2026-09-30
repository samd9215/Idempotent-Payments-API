"""
app.py
------
A small "payments" API demonstrating idempotent writes.

The business logic (`process_payment`) is deliberately simple — the point of
this project is the idempotency behaviour around it, not the payment logic
itself. In a real system `process_payment` would call out to a card
processor; here it just simulates a charge and appends to a ledger.

Run: python app.py
Test: pytest tests/
"""
import os
import sqlite3
import time
import uuid

from flask import Flask, request, jsonify
from idempotency import IdempotencyStore, IdempotencyConflict

app = Flask(__name__)

# Swap FakeRedis for real redis.Redis(host=..., port=...) in production —
# nothing else in this file needs to change.
if os.environ.get("USE_REAL_REDIS") == "1":
    import redis
    redis_client = redis.Redis(host="localhost", port=6379)
else:
    import fakeredis
    redis_client = fakeredis.FakeStrictRedis()

store = IdempotencyStore(redis_client)

DB_PATH = os.path.join(os.path.dirname(__file__), "ledger.db")


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS payments (
            id TEXT PRIMARY KEY,
            amount_cents INTEGER NOT NULL,
            recipient TEXT NOT NULL,
            created_at REAL NOT NULL
        )
    """)
    return conn


def process_payment(amount_cents: int, recipient: str) -> dict:
    """The actual business logic — runs exactly once per idempotency key."""
    payment_id = str(uuid.uuid4())
    conn = get_db()
    conn.execute(
        "INSERT INTO payments (id, amount_cents, recipient, created_at) VALUES (?, ?, ?, ?)",
        (payment_id, amount_cents, recipient, time.time()),
    )
    conn.commit()
    conn.close()
    return {"payment_id": payment_id, "amount_cents": amount_cents, "recipient": recipient, "status": "completed"}


@app.route("/payments", methods=["POST"])
def create_payment():
    idempotency_key = request.headers.get("Idempotency-Key")
    if not idempotency_key:
        return jsonify({"error": "Idempotency-Key header is required"}), 400

    body = request.get_json(force=True)
    if not body or "amount_cents" not in body or "recipient" not in body:
        return jsonify({"error": "amount_cents and recipient are required"}), 400

    try:
        status, value = store.get_or_reserve(idempotency_key, "POST", "/payments", body)
    except IdempotencyConflict as e:
        return jsonify({"error": str(e)}), 409
    except ValueError as e:
        return jsonify({"error": str(e)}), 422

    if status == "cached":
        response = dict(value)
        response["idempotent_replay"] = True
        return jsonify(response), 200

    # status == "proceed": this is genuinely the first time we've seen this key
    fingerprint = value
    result = process_payment(body["amount_cents"], body["recipient"])
    store.save_response(idempotency_key, fingerprint, result)

    response = dict(result)
    response["idempotent_replay"] = False
    return jsonify(response), 201


@app.route("/payments/_ledger", methods=["GET"])
def list_ledger():
    """Debug endpoint: shows every row actually written — used in tests to
    prove a retried request did NOT create a second row."""
    conn = get_db()
    rows = conn.execute("SELECT id, amount_cents, recipient FROM payments").fetchall()
    conn.close()
    return jsonify([{"id": r[0], "amount_cents": r[1], "recipient": r[2]} for r in rows])


if __name__ == "__main__":
    app.run(debug=True, port=5000)
