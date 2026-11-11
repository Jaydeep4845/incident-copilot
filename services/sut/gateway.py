"""gateway: the public API. Forwards checkout and order-history requests to orders."""

from __future__ import annotations

import os
import time

import httpx
from fastapi import HTTPException

from sut.common import create_app, env_float, env_int, log

app = create_app()

ORDERS = os.environ.get("ORDERS_URL", "http://orders:8000")
RETRIES = env_int("CHECKOUT_RETRIES", 1)  # attempts after the first one
RETRY_BACKOFF_S = env_float("RETRY_BACKOFF_S", 0.2)
http = httpx.Client(timeout=env_float("DOWNSTREAM_TIMEOUT_S", 8.0))


@app.post("/checkout")
def checkout(body: dict) -> dict:
    # sync endpoint: FastAPI runs it in a worker thread, so blocking calls
    # do not stall other requests
    last_status = 502
    for attempt in range(RETRIES + 1):
        try:
            r = http.post(f"{ORDERS}/orders", json=body)
            if r.status_code < 500:
                if r.status_code >= 400:
                    raise HTTPException(status_code=r.status_code, detail=r.json().get("detail"))
                return r.json()
            last_status = r.status_code
        except httpx.HTTPError as exc:
            log.error(
                f"orders call failed: {exc}", extra={"dependency": "orders", "error": "HTTPError"}
            )
        if attempt < RETRIES:
            log.warning(
                f"retrying checkout (attempt {attempt + 2} of {RETRIES + 1})",
                extra={"dependency": "orders", "status": last_status},
            )
            if RETRY_BACKOFF_S:
                time.sleep(RETRY_BACKOFF_S * (2**attempt))
    raise HTTPException(status_code=502, detail="checkout failed")


@app.get("/orders/{user_id}")
def history(user_id: int) -> dict:
    try:
        r = http.get(f"{ORDERS}/orders/user/{user_id}")
    except httpx.HTTPError as exc:
        log.error(
            f"orders call failed: {exc}", extra={"dependency": "orders", "error": "HTTPError"}
        )
        raise HTTPException(status_code=502, detail="orders unreachable") from exc
    if r.status_code >= 500:
        raise HTTPException(status_code=502, detail="orders error")
    return r.json()
