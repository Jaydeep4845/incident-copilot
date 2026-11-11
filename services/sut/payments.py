"""payments: charges a card through a (simulated) external payment provider."""

from __future__ import annotations

import os
import random
import time

from fastapi import HTTPException
from pydantic import BaseModel

from sut.common import VERSION, create_app, env_float, log, timed_dependency

app = create_app()

PROVIDER_LATENCY_MS = env_float("PROVIDER_LATENCY_MS", 80)
PROVIDER_ERROR_RATE = env_float("PROVIDER_ERROR_RATE", 0.01)
# A bug shipped in a release: the new multi-currency code path looks up a fee table that is missing
# currencies other than USD.
CURRENCY_BUG = os.environ.get("CURRENCY_BUG", "false") == "true"
FEES = {"USD": 0.029} if CURRENCY_BUG else {"USD": 0.029, "EUR": 0.025, "INR": 0.02}


class Charge(BaseModel):
    order_ref: str
    amount: float
    currency: str = "USD"


@app.post("/charge")
def charge(c: Charge) -> dict:
    fee = c.amount * FEES[c.currency]  # KeyError for unsupported currencies when the bug is shipped
    with timed_dependency("payment-provider", "authorize"):
        time.sleep(max(0.0, random.gauss(PROVIDER_LATENCY_MS, PROVIDER_LATENCY_MS * 0.15)) / 1000)
        if random.random() < PROVIDER_ERROR_RATE:
            log.warning(
                "payment provider declined request: upstream 503",
                extra={"dependency": "payment-provider"},
            )
            raise HTTPException(status_code=502, detail="provider unavailable")
    return {"order_ref": c.order_ref, "status": "authorized", "fee": round(fee, 2), "v": VERSION}


@app.post("/__chaos/provider")
def set_provider(latency_ms: float | None = None, error_rate: float | None = None) -> dict:
    """Simulates the external provider degrading. Used only by the chaos runner."""
    global PROVIDER_LATENCY_MS, PROVIDER_ERROR_RATE
    if latency_ms is not None:
        PROVIDER_LATENCY_MS = latency_ms
    if error_rate is not None:
        PROVIDER_ERROR_RATE = error_rate
    return {"latency_ms": PROVIDER_LATENCY_MS, "error_rate": PROVIDER_ERROR_RATE}
