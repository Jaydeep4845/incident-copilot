"""orders: creates orders (inventory + payments + Postgres) and lists a user's order history."""

from __future__ import annotations

import os
import time
import uuid

import httpx
import psycopg
import psycopg_pool
from fastapi import HTTPException
from prometheus_client import Gauge
from pydantic import BaseModel

from sut.common import create_app, env_float, env_int, log, timed_dependency

app = create_app()

POOL_SIZE = env_int("DB_POOL_SIZE", 10)
POOL_IN_USE = Gauge("db_pool_connections_in_use", "Connections checked out of the pool")
POOL_WAITING = Gauge("db_pool_requests_waiting", "Requests waiting for a pool connection")
POOL_MAX = Gauge("db_pool_connections_max", "Pool size")
POOL_MAX.set(POOL_SIZE)

pool = psycopg_pool.ConnectionPool(
    os.environ["DATABASE_URL"], min_size=2, max_size=POOL_SIZE, timeout=5.0, open=False
)
http = httpx.Client(timeout=env_float("DOWNSTREAM_TIMEOUT_S", 2.0))
INVENTORY = os.environ.get("INVENTORY_URL", "http://inventory:8000")
PAYMENTS = os.environ.get("PAYMENTS_URL", "http://payments:8000")
SLOW_QUERY_MS = 500


@app.on_event("startup")
def open_pool() -> None:
    # Non-blocking: if the database is not reachable yet, the pool keeps reconnecting in the
    # background instead of failing permanently.
    pool.open(wait=False)


def update_pool_gauges() -> None:
    stats = pool.get_stats()
    POOL_IN_USE.set(stats.get("pool_size", 0) - stats.get("pool_available", 0))
    POOL_WAITING.set(stats.get("requests_waiting", 0))


def db(operation: str, sql: str, params: tuple, fetch: bool = False):
    update_pool_gauges()
    start = time.perf_counter()
    try:
        with timed_dependency("postgres", operation), pool.connection() as conn:
            cur = conn.execute(sql, params)
            rows = cur.fetchall() if fetch else None
    except psycopg_pool.PoolTimeout as exc:
        log.error(
            "couldn't get a database connection after 5.0 sec (pool exhausted)",
            extra={"dependency": "postgres", "query": operation, "error": "PoolTimeout"},
        )
        raise HTTPException(status_code=503, detail="database busy") from exc
    except Exception as exc:  # connection and authentication failures
        log.error(
            f"database error: {exc}",
            extra={"dependency": "postgres", "query": operation, "error": type(exc).__name__},
        )
        raise HTTPException(status_code=503, detail="database error") from exc
    finally:
        update_pool_gauges()
    elapsed_ms = (time.perf_counter() - start) * 1000
    if elapsed_ms > SLOW_QUERY_MS:
        log.warning(
            f"slow query {operation} took {elapsed_ms:.0f} ms",
            extra={"query": operation, "duration_ms": round(elapsed_ms)},
        )
    return rows


def call(service: str, method: str, url: str, **kw) -> dict:
    try:
        r = http.request(method, url, **kw)
    except httpx.TimeoutException as exc:
        log.error(
            f"timeout calling {service} after {http.timeout.read}s",
            extra={"dependency": service, "error": "Timeout"},
        )
        raise HTTPException(status_code=504, detail=f"{service} timeout") from exc
    except httpx.HTTPError as exc:
        log.error(
            f"error calling {service}: {exc}", extra={"dependency": service, "error": "HTTPError"}
        )
        raise HTTPException(status_code=502, detail=f"{service} unreachable") from exc
    if r.status_code >= 400:
        log.warning(
            f"{service} returned {r.status_code}",
            extra={"dependency": service, "status": r.status_code},
        )
        raise HTTPException(status_code=502, detail=f"{service} error {r.status_code}")
    return r.json()


class NewOrder(BaseModel):
    user_id: int
    sku: str
    qty: int = 1
    amount: float
    currency: str = "USD"


@app.post("/orders")
def create_order(o: NewOrder) -> dict:
    stock = call("inventory", "GET", f"{INVENTORY}/stock/{o.sku}")
    if stock["available"] < o.qty:
        raise HTTPException(status_code=409, detail="out of stock")
    ref = uuid.uuid4().hex[:12]
    # The order row and the payment are kept in one transaction, so the database connection stays
    # checked out while inventory and payments are called. Common in practice, and it means a slow
    # dependency can exhaust the connection pool.
    update_pool_gauges()
    try:
        with pool.connection() as conn, conn.transaction():
            with timed_dependency("postgres", "insert_order"):
                order_id = conn.execute(
                    "INSERT INTO orders (user_id, sku, qty, amount, currency, status) "
                    "VALUES (%s, %s, %s, %s, %s, 'pending') RETURNING id",
                    (o.user_id, o.sku, o.qty, o.amount, o.currency),
                ).fetchone()[0]
            update_pool_gauges()
            call("inventory", "POST", f"{INVENTORY}/stock/{o.sku}/reserve", params={"qty": o.qty})
            call(
                "payments",
                "POST",
                f"{PAYMENTS}/charge",
                json={"order_ref": ref, "amount": o.amount, "currency": o.currency},
            )
            with timed_dependency("postgres", "mark_paid"):
                conn.execute("UPDATE orders SET status = 'paid' WHERE id = %s", (order_id,))
    except psycopg_pool.PoolTimeout as exc:
        log.error(
            "couldn't get a database connection after 5.0 sec (pool exhausted)",
            extra={"dependency": "postgres", "query": "insert_order", "error": "PoolTimeout"},
        )
        raise HTTPException(status_code=503, detail="database busy") from exc
    except psycopg.Error as exc:
        log.error(
            f"database error: {exc}",
            extra={"dependency": "postgres", "query": "insert_order", "error": type(exc).__name__},
        )
        raise HTTPException(status_code=503, detail="database error") from exc
    finally:
        update_pool_gauges()
    return {"order_ref": ref, "status": "created"}


@app.get("/orders/user/{user_id}")
def user_orders(user_id: int) -> dict:
    rows = db(
        "select_user_orders",
        "SELECT id, sku, qty, amount, created_at FROM orders WHERE user_id = %s "
        "ORDER BY created_at DESC LIMIT 20",
        (user_id,),
        fetch=True,
    )
    return {"user_id": user_id, "orders": len(rows)}


def _report_export_job() -> None:
    log.info("scheduled report export job started", extra={"query": "report_export"})
    total = 0
    while True:  # runaway loop: the job never yields, starving request handling of CPU
        for i in range(1_000_000):
            total += i * i


@app.post("/__chaos/report-job")
def start_report_job(threads: int = 2) -> dict:
    import threading

    for _ in range(threads):
        threading.Thread(target=_report_export_job, daemon=True).start()
    return {"started": threads}
