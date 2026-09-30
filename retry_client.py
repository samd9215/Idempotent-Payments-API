"""
retry_client.py
----------------
Demonstrates the client side of this pattern: a request that times out
gets retried with exponential backoff, reusing the SAME Idempotency-Key
each time — which is what makes the retries safe.

Run against a live server: python app.py, then in another terminal:
    python retry_client.py
"""
import time
import uuid
import random
import requests

BASE_URL = "http://localhost:5000"


def create_payment_with_retry(amount_cents: int, recipient: str, max_retries: int = 4):
    idempotency_key = str(uuid.uuid4())  # generated ONCE, reused across all retries
    headers = {"Idempotency-Key": idempotency_key}
    body = {"amount_cents": amount_cents, "recipient": recipient}

    for attempt in range(1, max_retries + 1):
        try:
            resp = requests.post(f"{BASE_URL}/payments", json=body, headers=headers, timeout=2)
            if resp.status_code in (200, 201):
                print(f"[attempt {attempt}] Success: {resp.json()}")
                return resp.json()
            elif resp.status_code == 409:
                # Another in-flight retry is still being processed — back off and retry
                print(f"[attempt {attempt}] Request in flight elsewhere, backing off...")
            else:
                print(f"[attempt {attempt}] Non-retryable error: {resp.status_code} {resp.json()}")
                return None
        except requests.exceptions.Timeout:
            print(f"[attempt {attempt}] Timed out — will retry with the SAME idempotency key")

        backoff = (2 ** attempt) + random.uniform(0, 1)
        time.sleep(min(backoff, 8))

    print("Max retries exceeded.")
    return None


if __name__ == "__main__":
    create_payment_with_retry(amount_cents=2500, recipient="charlie")
