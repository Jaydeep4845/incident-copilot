"""Steady background traffic: an open-loop generator, so a slow system keeps receiving requests
the way a real website does (users do not wait for each other)."""

from __future__ import annotations

import asyncio
import os
import random

import httpx

GATEWAY = os.environ.get("GATEWAY_URL", "http://gateway:8000")
CHECKOUT_RPS = float(os.environ.get("CHECKOUT_RPS", 12))
HISTORY_RPS = float(os.environ.get("HISTORY_RPS", 20))
MAX_IN_FLIGHT = 300
CURRENCIES = ["USD"] * 7 + ["EUR"] * 2 + ["INR"]


async def fire(client: httpx.AsyncClient, sem: asyncio.Semaphore, method: str, url: str, **kw):
    if sem.locked():
        return  # shed load instead of growing without bound
    async with sem:
        try:
            await client.request(method, url, **kw)
        except httpx.HTTPError:
            pass


async def stream(client, sem, rps: float, make_request) -> None:
    while True:
        method, url, kw = make_request()
        asyncio.create_task(fire(client, sem, method, url, **kw))
        await asyncio.sleep(random.expovariate(rps))


def checkout():
    return (
        "POST",
        f"{GATEWAY}/checkout",
        {
            "json": {
                "user_id": random.randint(1, 50_000),
                "sku": f"SKU-{random.randint(1, 200):03d}",
                "qty": 1,
                "amount": round(random.uniform(5, 300), 2),
                "currency": random.choice(CURRENCIES),
            }
        },
    )


def history():
    return ("GET", f"{GATEWAY}/orders/{random.randint(1, 50_000)}", {})


async def main() -> None:
    sem = asyncio.Semaphore(MAX_IN_FLIGHT)
    async with httpx.AsyncClient(timeout=15) as client:
        await asyncio.gather(
            stream(client, sem, CHECKOUT_RPS, checkout), stream(client, sem, HISTORY_RPS, history)
        )


if __name__ == "__main__":
    asyncio.run(main())
